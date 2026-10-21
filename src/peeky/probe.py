"""Stateless one-request probes over the guarded worker engine (ADR 0004).

A probe is one complete transaction: prepare a fresh build, launch, stop at one
declared line, capture declared selectors, apply optional declared interventions,
run to exit or a bound, report and clean up. No session, stop or handle identity
leaves the call, so retrying is always a whole new run. Every read and write goes
through DebugWorker.dispatch, so session/stop identity, provenance checks, value
views and mutation rules are identical to session mode. Runtime interventions are
evidence about one execution, never a verified fix.
"""

import argparse
import ast
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from peeky.errors import Refused, classify
from peeky.java import _JAVA_OBJECT, JAVA_ARRAY, JAVA_LIST, java_list, java_preview
from peeky.names import display_function, function_labels, shortened_note
from peeky.worker import (
    DebugWorker,
    load_java_target,
    load_native_target,
    load_rust_target,
    optimization_level,
)

MAX_SELECTORS = 32
MAX_SETS = 8
MAX_HITS = 100
# The highest hit a --set-at may name: its line's stops are counted, not captured.
MAX_SET_AT_HIT = 1000
MAX_ROWS = 2000
MAX_SECONDS = 300
# HTTP probes must finish inside Client.call's 45 s timeout.
HTTP_PROBE_SECONDS = 40
_INDEX = re.compile(r"\[?([0-9]{1,6})\]?")
# A quoted string key, as written in a selector and as debugpy names mapping entries.
# Escapes Python accepts without a warning; anything else (`\d`, `\777`) is refused.
_ESCAPE = r"""\\(?:[\\'"abfnrtv]|[0-3][0-7]{2}|[0-7]{1,2}(?![0-7])|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|N\{[A-Z0-9 -]{1,88}\})"""
_KEY_LITERAL = rf"'(?:[^'\\\n]|{_ESCAPE}){{0,256}}'" + "|" + rf'"(?:[^"\\\n]|{_ESCAPE}){{0,256}}"'
# One selector step: `.name`, `[3]`, `[-1]`, `[#3]` (a C++ map's or set's entry by
# position), or a quoted string key `['id']` / `["id"]`.
_STEP = re.compile(
    r"\.(?P<name>[A-Za-z_][A-Za-z0-9_]{0,127})"
    r"|\[(?P<index>-?[0-9]{1,6})\]"
    r"|\[#(?P<entry>[0-9]{1,6})\]"
    rf"|\[(?P<key>{_KEY_LITERAL})\]"
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
# A local LLDB shows with where it is declared, when several of that name are in scope
# (`s @ main.rs:81`, `p @ s1.c:38`): the spaces around `@` may be left out.
_SHADOWED_ROOT = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]{0,127}) ?@ ?([^\s:@\[\]'\"]{1,255}):([0-9]{1,9})"
)


def parse_selector(text):
    """`request.page` -> ['request', 'page']; `args[0]` -> ['args', '[0]'];
    `order['id']` -> ['order', "['id']"]; `m[#0]` -> ['m', '[#0]']; `s @ main.rs:81.a`
    -> ['s @ main.rs:81', 'a'] (a local LLDB names by its declaration, shadowing).

    Negative indexes (`items[-1]`) count from the end of a sequence whose elements
    all fit in the bounded inspection window. A quoted key reads one entry of a
    mapping; it is kept in its canonical Python form, `[` + repr(key) + `]`. `[#N]` is
    the Nth entry of a C++ map or set in iteration order, where `[N]` is key N (#119).
    """
    if not isinstance(text, str) or not text or len(text) > 512:
        raise ValueError("Selectors must be nonempty dotted paths")
    first = _SHADOWED_ROOT.match(text) or _NAME.match(text)
    if not first:
        raise ValueError(f"Invalid selector: {text}{_SELECTOR_HELP}")
    root = f"{first[1]} @ {first[2]}:{int(first[3])}" if first.re is _SHADOWED_ROOT else first[0]
    segments, position = [root], first.end()
    while position < len(text):
        step = _STEP.match(text, position)
        if not step:
            # A C pointer's members are read with `.`, as the debugger shows them.
            arrow = (
                f"; follow a pointer with . (not ->): {text.replace('->', '.')}"
                if "->" in text
                else ""
            )
            # LLDB names a Rust tuple's (or tuple struct's) fields `__0`, `__1`.
            if re.search(r"\.[0-9]", text):
                arrow += (
                    "; a tuple's fields are .__0, .__1 in Rust (as LLDB names them) and "
                    "[0] in Python: " + re.sub(r"[.]([0-9]+)", r".__\1", text)
                )
            raise ValueError(f"Invalid selector: {text}{_SELECTOR_HELP}{arrow}")
        if step["name"]:
            segments.append(step["name"])
        elif step["index"]:
            segments.append(f"[{step['index']}]")
        elif step["entry"]:
            segments.append(f"[#{step['entry']}]")
        else:
            try:
                key = _literal(step["key"])
            except ValueError:
                raise ValueError(f"Invalid selector: {text}{_SELECTOR_HELP}") from None
            if not isinstance(key, str):
                raise ValueError(f"Invalid selector: {text}{_SELECTOR_HELP}")
            segments.append(f"[{key!r}]")
        position = step.end()
    if len(segments) > 16:
        raise ValueError(f"Invalid selector: {text} (at most 16 steps)")
    return segments


_SELECTOR_HELP = (
    " (selectors are paths, never expressions: name, a.b, a[0], a[-1], a['key'] or a[\"key\"];"
    " a[#0] is a C++ map's or set's first entry; 's @ main.rs:81.a' starts at a local the"
    " debugger names with where it is declared)"
)


def split_selectors(text):
    """`--capture` text -> selectors, splitting on commas outside quoted keys."""
    parts, current, quote, escaped = [], [], None, False
    for char in text:
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == ",":
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return tuple(part.strip() for part in parts if part.strip())


def _literal(text):
    """The value of a quoted string literal that matched _KEY_LITERAL.

    Its escapes are pre-validated by the pattern, so evaluation never warns; the
    process-global warnings filters are left alone (HTTP handlers run in threads).
    """
    if not _KEY_NAME.fullmatch(text):
        raise ValueError(f"not a plain quoted string: {text[:40]}")
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError) as exc:
        raise ValueError(str(exc)) from None


def _key(segment):
    """The canonical repr of a string-key segment (`'id'` for `['id']`), else None."""
    return segment[1:-1] if segment[:2] in ("['", '["') else None


def _check_args(args):
    if (
        not isinstance(args, tuple)
        or len(args) > 64
        or not all(isinstance(a, str) and len(a) <= 4096 and "\0" not in a for a in args)
    ):
        raise ValueError("Program arguments are at most 64 strings of up to 4096 characters")


MAX_EDIT_BYTES = 1024 * 1024
CANDIDATE_NEEDS_BUILD = (
    "A candidate edit (--old/--new) needs a fresh launch, and an attached JVM is already "
    "running: check the edit with `peeky verify` on a launched target"
)


MAX_EDITS = 32


def edit_pairs(edit):
    """A candidate edit as a tuple of (old, new) pairs: one pair, or several."""
    if edit is None:
        return ()
    if isinstance(edit, tuple) and len(edit) == 2 and all(isinstance(t, str) for t in edit):
        return (edit,)
    return tuple(tuple(pair) for pair in edit)


def _edit_spans(text, pairs):
    """[(start, old, new)] in file order. Each old must match exactly once in the
    original text, and no two may overlap: every --old quotes the file as it is."""
    spans = []
    for number, (old, new) in enumerate(pairs, 1):
        which = f" (edit {number})" if len(pairs) > 1 else ""
        if not old:
            raise ValueError(f"--old is empty{which}")
        if (count := text.count(old)) != 1:
            raise ValueError(
                f"--old matched {count} times{which}; it must match the --at file exactly once"
            )
        if old == new:
            raise ValueError(f"--new equals --old{which}, so the edit changes nothing")
        spans.append((text.index(old), old, new, number))
    spans.sort()
    for (start, old, _, first), (after, _, _, second) in zip(spans, spans[1:]):
        if after < start + len(old):
            raise ValueError(
                f"Edits {first} and {second} overlap: each --old must quote its own part of "
                "the original file"
            )
    return spans


def combine_edits(text, edit):
    """One (old, new) with the effect of applying every pair of ``edit`` at once: the
    original text from the first edit's start to the last one's end, and that text
    edited. It matches exactly once, since it contains the first, unique, old."""
    spans = _edit_spans(text, edit_pairs(edit))
    if len(spans) == 1:
        return spans[0][1], spans[0][2]
    first, (last, old_last, _, _) = spans[0][0], spans[-1]
    pieces, cursor = [], first
    for start, old, new, _ in spans:
        pieces += [text[cursor:start], new]
        cursor = start + len(old)
    return text[first : last + len(old_last)], "".join(pieces)


def _through(text, start, old, new, line, head, which=""):
    """How one edit at ``start`` moves original line ``line``: None if the line is
    before it, else the lines it adds or removes. ``head`` is the edited text up to the
    edit's end. Lines inside an edit that changes the line count are refused."""
    first = text.count("\n", 0, start) + 1
    last = text.count("\n", 0, start + len(old) - 1) + 1
    delta = new.count("\n") - old.count("\n")
    if line < first:
        return None
    if line > last:
        if line == last + 1 and old.endswith("\n") and head and not head.endswith("\n"):
            # --old ends a line but --new does not: this line is joined onto the edit's
            # last line, and a breakpoint there would stop before the edited code runs.
            raise ValueError(
                f"Line {line} starts right after the edit{which}, and --new does not end "
                f"with a newline as --old does, so the candidate joins it onto line "
                f"{head.count(chr(10)) + 1}; end --new with a newline, or probe another line"
            )
        return delta
    if delta == 0:
        return 0
    span = (
        f"line {first}, which becomes" if first == last else f"lines {first}-{last}, which become"
    )
    after = last + delta
    became = f"line {first}" if after <= first else f"lines {first}-{after}"
    raise ValueError(
        f"Line {line} is inside the edit{which} ({span} {became} of the candidate). An edit "
        "that changes the number of lines has no matching line inside it: probe a line "
        "before or after the edit"
    )


def candidate_line(text, edit, line):
    """The line of the edited text that holds original line ``line``.

    Lines before an edit keep their number and lines after it shift by the lines it
    adds or removes. A line inside an edit maps only when the edit keeps its line
    count; otherwise there is no faithful answer, so it is refused.
    """
    spans = _edit_spans(text, edit_pairs(edit))
    shift, cursor, head = 0, 0, ""
    for start, old, new, number in spans:
        head += text[cursor:start] + new
        cursor = start + len(old)
        which = f" {number}" if len(spans) > 1 else ""
        moved = _through(text, start, old, new, line, head, which)
        if moved is None:
            break
        shift += moved
    return line + shift


def parse_location(text):
    """`src/main.rs:36` -> ('src/main.rs', 36)."""
    program, separator, line = (text or "").rpartition(":")
    if not separator or not program or not re.fullmatch(r"[1-9][0-9]{0,6}", line):
        raise ValueError("Location must be <relative-source-path>:<line>")
    return program, int(line)


_SET_AT = re.compile(r"(?:(?P<path>.+):)?(?P<line>[1-9][0-9]{0,6})(?:@(?P<hit>[1-9][0-9]{0,6}))?")


def parse_set_at(where, assignment):
    """`--set-at [<path>:]<line>[@<hit>] <name>=<value>` -> (path or None, line, hit,
    name, value); the hit defaults to 1 and counts that line's own stops."""
    found = _SET_AT.fullmatch(where or "")
    if not found:
        raise ValueError(
            f"--set-at expects [<path>:]<line>[@<hit>] then name=value, got {where!r} "
            "(e.g. --set-at stock.cpp:22@3 colon=6)"
        )
    name, separator, value = (assignment or "").partition("=")
    if not separator or not name.strip():
        raise ValueError(f"--set-at {where} expects name=value, got {assignment!r}")
    hit = int(found["hit"] or 1)
    return found["path"], int(found["line"]), hit, name.strip(), value.strip()


@dataclass(frozen=True)
class Probe:
    program: str
    line: int
    capture: tuple[str, ...] = ()
    set: tuple[tuple[str, str], ...] = ()
    hits: int = 1
    timeout: float = 60
    # "exit": run to completion for the program's output; "hits": end (detach or
    # terminate) right after the last requested hit.
    until: str = "exit"
    # The hit at which interventions are applied (once).
    set_hit: int = 1
    # Python: the script to run when the breakpoint is in a module it imports.
    entry: str | None = None
    # Python: command-line arguments for the launched script.
    args: tuple[str, ...] = ()
    # Remote clients: (role, sha256) pairs the worker checks before launching.
    expect_sha256: tuple[tuple[str, str], ...] = ()
    # A candidate fix: (old, new) applied once to `program` in a fresh build; `line` is
    # in the original's numbering. The files on disk are never changed.
    edit: tuple[str, str] | None = None
    # Interventions at their own stops (#137): (line, hit, selector, value), each applied
    # once at that hit of that line of `program` (its own stops counted, in the original's
    # numbering), whether or not it is the capture line; `set`/`set_hit` stay as they are.
    set_at: tuple[tuple[int, int, str, str], ...] = ()

    @property
    def limit(self):
        """The most hits this probe captures: --hits N, or the cap for --hits all.

        `all` stops at MAX_HITS, or earlier when that many hits of every selector (plus
        one set of after-set rows) would exceed the MAX_ROWS row budget.
        """
        if self.hits != "all":
            return self.hits
        # One set of after-set rows per captured stop that gets interventions.
        stops = {self.set_hit} if self.set else set()
        stops |= {hit for line, hit, _, _ in self.set_at if line == self.line}
        after_set = len(self.capture) * len(stops)
        return max(1, min(MAX_HITS, (MAX_ROWS - after_set) // max(1, len(self.capture))))

    def __post_init__(self):
        if not isinstance(self.program, str) or not self.program:
            raise ValueError("Probe program must be a relative source path")
        if type(self.line) is not int or self.line < 1:
            raise ValueError("Probe line must be a positive integer")
        if not isinstance(self.capture, tuple) or len(self.capture) > MAX_SELECTORS:
            raise ValueError(f"At most {MAX_SELECTORS} capture selectors")
        for selector in self.capture:
            parse_selector(selector)
        if len(set(self.capture)) != len(self.capture):
            # A repeated selector adds no evidence and would multiply rows in comparisons.
            object.__setattr__(self, "capture", tuple(dict.fromkeys(self.capture)))
        if not isinstance(self.set, tuple) or not isinstance(self.set_at, tuple):
            raise ValueError("Interventions are tuples")
        if len(self.set) + len(self.set_at) > MAX_SETS:
            raise ValueError(f"At most {MAX_SETS} interventions (--set and --set-at together)")
        for point in self.set_at:
            if (
                not isinstance(point, tuple)
                or len(point) != 4
                or type(point[0]) is not int
                or not 1 <= point[0] <= 9_999_999
                or type(point[1]) is not int
                or not 1 <= point[1] <= MAX_SET_AT_HIT
                or not all(isinstance(part, str) for part in point[2:])
            ):
                raise ValueError(
                    "A --set-at intervention is (line, hit, selector, value): a positive "
                    f"line, a hit within 1..{MAX_SET_AT_HIT}, and strings"
                )
        for change in (*self.set, *(point[2:] for point in self.set_at)):
            if (
                not isinstance(change, tuple)
                or len(change) != 2
                or not all(isinstance(part, str) for part in change)
            ):
                raise ValueError("Interventions are (selector, value) string pairs")
            if (last := parse_selector(change[0])[-1]).startswith("[") and (
                _key(last) is not None or last.startswith("[#")
            ):
                raise Refused(
                    "MUTATION_REJECTED",
                    f"Intervention target {change[0]!r}: map and dict entries cannot be changed; "
                    "--set changes a local, or a field or array element (Java, Rust, C, "
                    "C++)",
                )
        if self.hits != "all" and (type(self.hits) is not int or not 1 <= self.hits <= MAX_HITS):
            raise ValueError(f"Probe hits must be 1..{MAX_HITS} or 'all'")
        if type(self.timeout) not in (int, float) or not 0 < self.timeout <= MAX_SECONDS:
            raise ValueError(f"Probe timeout must be within (0, {MAX_SECONDS}] seconds")
        if self.until not in ("exit", "hits"):
            raise ValueError("Probe until must be 'exit' or 'hits'")
        if self.hits == "all" and self.until == "hits":
            # Only the program's exit (or the cap) ends `all`; an attached JVM never exits.
            raise ValueError(
                "--hits all runs until the program exits; with --until hits (attached JVMs) "
                "give a number of hits"
            )
        if type(self.set_hit) is not int or not 1 <= self.set_hit <= self.limit:
            raise ValueError(
                f"Probe set_hit must be within 1..{self.limit}"
                + (" (the --hits all cap for these selectors)" if self.hits == "all" else "")
            )
        if self.entry is not None and (not isinstance(self.entry, str) or not self.entry):
            raise ValueError("Probe entry must be a relative script path")
        if self.edit is not None:
            edit = self.edit
            single = (
                isinstance(edit, tuple) and len(edit) == 2 and all(isinstance(t, str) for t in edit)
            )
            several = isinstance(edit, tuple) and all(isinstance(pair, tuple) for pair in edit)
            pairs = (edit,) if single else edit if several else ()
            if (
                not pairs
                or len(pairs) > MAX_EDITS
                or not all(
                    len(pair) == 2
                    and all(isinstance(text, str) for text in pair)
                    and pair[0]
                    and pair[0] != pair[1]
                    for pair in pairs
                )
                or sum(len(text.encode()) for pair in pairs for text in pair) > MAX_EDIT_BYTES
            ):
                raise ValueError(
                    f"A candidate edit is (old, new), or up to {MAX_EDITS} such pairs: each "
                    f"old nonempty, each new different, {MAX_EDIT_BYTES // (1024 * 1024)} MiB "
                    "in all"
                )
        _check_args(self.args)
        if not isinstance(self.expect_sha256, tuple) or not all(
            isinstance(pair, tuple)
            and len(pair) == 2
            and pair[0] in ("program", "entry")
            and isinstance(pair[1], str)
            and re.fullmatch(r"[0-9a-f]{64}", pair[1])
            for pair in self.expect_sha256
        ):
            raise ValueError("Probe expect_sha256 holds (program|entry, sha256) pairs")


def parse_hits(text):
    """`--hits`: a count, or `all` (every stop up to the cap)."""
    if text == "all":
        return "all"
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number or 'all', got {text!r}") from None


# Progress for repeated hits: the first and the last requested, otherwise at most one
# line every PROGRESS_SECONDS, so long runs show they are alive without flooding logs.
PROGRESS_SECONDS = 2.0


def _report_hit(hit, limit, last_said):
    return hit in (1, limit) or time.monotonic() - last_said >= PROGRESS_SECONDS


def _listing(scopes):
    """The names these scopes list, for a not-found message, and what the bound left out."""
    # lldb-dap names a nameless slot (a closure's environment) "<null>": not selectable.
    names = sorted({v["name"] for scope in scopes for v in scope["variables"]} - {"<null>"})
    grouped = [name for name in names if name in _GROUPS]
    names = [name for name in names if name not in _GROUPS]
    parts = []
    if names:
        parts.append(
            ", ".join(names[:40]) + (f", ... ({len(names)} total)" if len(names) > 40 else "")
        )
    if grouped:
        parts.append(f"the names in debugpy's {', '.join(grouped)} (select them by name)")
    shown = " and ".join(parts)
    for scope in scopes:
        if scope.get("truncated"):
            shown += (
                f" ({scope['name']} lists {len(scope['variables'])} of its "
                f"{scope.get('total')} entries, debugpy's groups included)"
            )
    return shown


def _visible(snapshot, module=True):
    """The names a selector's first part can take: the frame's own, then (Python)
    the module's globals, when they were listed."""
    own = [scope for scope in snapshot["scopes"] if not scope.get("globals")]
    shown = _listing(own) or "(none)"
    globals_ = [scope for scope in snapshot["scopes"] if scope.get("globals")]
    if module and globals_:
        shown += f"; module globals: {_listing(globals_) or '(none)'}"
    return shown


def _member_hint(worker, stop, snapshot, name):
    """`; select it as this.name` when the stopped method's object (C++, Java) has a
    member of that name. Never read implicitly: the debugger's list of locals can be
    incomplete (no local-variable table, statics, truncated scopes), so a missing local
    does not prove the code means the member."""
    receiver = snapshot.get("receiver")
    holder = next(
        (
            v
            for scope in snapshot["scopes"]
            for v in scope["variables"]
            if receiver and v["name"] == receiver
        ),
        None,
    )
    if holder and holder.get("withheld") and not name.startswith("["):
        return (
            f"; if the code means a member, {receiver} has no value here: "
            f"{holder.get('reason') or 'withheld'}"
        )
    if name.startswith("[") or not holder or not holder.get("variablesReference"):
        return ""
    try:
        page = worker.dispatch("expand", {**stop, "handle": holder["variablesReference"]})
    except ValueError:
        return ""
    if any(child["name"] == name for child in page["variables"]):
        return (
            f"; {receiver} has a member {name!r}: select it as {receiver}.{name} if the "
            "code means the member"
        )
    return ""


def _page_find(worker, stop, handle, segment=None, first_page=None):
    """Pages of a value's children (64 at a time, at most 512) until one is ``segment``
    (all of them without one): (the child or None, the children read, whether the bound
    stopped the search). ``first_page(handle)``, if given, serves the first page (fetched
    once per selector)."""
    found, start, incomplete, children = None, 0, False, []
    while found is None:
        page = (
            first_page(handle)
            if first_page is not None and start == 0
            else worker.dispatch("expand", {**stop, "handle": handle, "start": start})
        )
        children.extend(page["variables"])
        if segment is not None:
            found = next((v for v in page["variables"] if _names(v, segment)), None)
        if found is not None or not page["truncated"]:
            break
        if start + 64 > 511 or page.get("remaining_nodes", 1) <= 0:
            incomplete = True
            break
        start += 64
    return found, children, incomplete


def _grouped(worker, stop, children, name, cache):
    """Members named ``name`` that debugpy filed under one of its groups among
    ``children``, by its own rule: `__dunder__` names in `special variables`, `_x` in
    `protected variables`, functions and classes in `function` or `class variables`.
    A group is read once per inspection (``cache``: its handle -> its members)."""
    if name.startswith("__") and name.endswith("__"):
        wanted = {"special variables"}
    elif name.startswith("_") or name.endswith("__"):
        wanted = {"protected variables"}
    else:
        wanted = {"function variables", "class variables"}
    found = []
    for group in children:
        handle = group.get("variablesReference")
        if group["name"] not in wanted or group.get("type") or not handle:
            continue
        if handle not in cache:
            _, members, incomplete = _page_find(worker, stop, handle)
            cache[handle] = (members, incomplete)
        members, incomplete = cache[handle]
        member = next((m for m in members if m["name"] == name), None)
        if member is None and incomplete:
            raise Refused(
                "BUDGET_EXHAUSTED",
                f"{name!r} is beyond the bounded inspection window of debugpy's {group['name']}",
            )
        if member is not None:
            found.append(member)
    return found


def _groups(snapshot):
    """The groups read at this inspection; a new inspection starts afresh."""
    return snapshot.setdefault("_groups", {})


def _scope_lookup(worker, stop, snapshot, scopes, name):
    """``name`` in these scopes: listed directly, or in one of debugpy's groups; with
    the scope it is in. Listed more than once (LLDB names two locals declared on one
    line alike, `p @ s.c:10`), it is refused: which is which cannot be told."""
    listed = [(v, scope) for scope in scopes for v in scope["variables"] if v["name"] == name]
    if len(listed) > 1:
        raise Refused(
            "NOT_FOUND",
            f"{name!r} is ambiguous: the debugger lists {len(listed)} locals of that name here "
            "(declared on the same line), which Peeky cannot tell apart",
        )
    for variable, scope in listed:
        return variable, scope
    for scope in scopes:
        if found := _grouped(worker, stop, scope["variables"], name, _groups(snapshot)):
            return found[0], scope
    return None, None


def _with_globals(worker, stop, snapshot, name):
    """Inspect the same frame again, asking for the module globals its function reads
    among the selectors' names missing from its locals. The snapshot is replaced in
    place: the new inspection revokes the old one's handles."""
    own = {
        variable["name"]
        for scope in snapshot["scopes"]
        if not scope.get("globals")
        for variable in scope["variables"]
    }
    asked = [name, *(n for n in snapshot.get("names") or () if n != name and n not in own)]
    request = {
        **stop,
        "names": sorted({name, *(snapshot.get("names") or ())})[:32],
        "globals": asked[:32],
    }
    request["previews"] = request["names"]
    if snapshot.get("frame"):
        request["frame"] = snapshot["frame"]
    fresh = worker.dispatch("inspect", request)
    snapshot.clear()
    snapshot.update(fresh)


def _bound(name, how, function):
    """Why ``name`` in ``function`` is not a module global (python_scopes.classify)."""
    kind, owner = how.get("kind"), how.get("owner") or "a function around it"
    return {
        "local": f"{name!r} is a local of {function} with no value at this line (not yet "
        "assigned, or deleted): reading it here raises UnboundLocalError",
        "parameter": f"{name!r} is a parameter of {function}, deleted before this line: "
        "reading it here raises UnboundLocalError",
        "comprehension": f"{name!r} is the variable of a comprehension in {function}, "
        "which has no value outside that comprehension",
        "free": f"{name!r} is a variable of {owner} that {function} uses (a closure "
        "variable), with no value there yet: reading it here raises NameError",
        "enclosing": f"{name!r} is a local of {owner}, the function around {function}, "
        f"which {function} does not use",
        "type parameter": f"{name!r} is a type parameter of {owner} (PEP 695), which "
        f"{function} does not use",
        "class cell": f"'__class__' is the class cell Python gives only a method that uses "
        f"super() or __class__, which {function} does not",
        "class cell used": f"'__class__' is {function}'s implicit class cell, with no value here",
    }.get(kind, f"{name!r} is bound in {function} ({kind})")


def _root(worker, stop, snapshot, name):
    """The variable a selector's first name reads, and whether it is a module global.

    The frame's own scopes come first. A Python name missing there is read from the
    module's globals only when the worker proved, from the function's own scoping, that
    the function reads that name from its globals: a local not yet assigned is missing
    from the locals too, and its global namesake is not its value. The globals are
    listed (running their reprs) only then."""
    own = [scope for scope in snapshot["scopes"] if not scope.get("globals")]
    if (found := _scope_lookup(worker, stop, snapshot, own, name)[0]) is not None:
        return found, False
    if any(scope.get("truncated") for scope in own) and name not in (snapshot.get("names") or ()):
        # A worker that predates `names` lists no name past the bound.
        raise ValueError(
            f"not found among the names listed (visible: {_visible(snapshot)}): {name!r} "
            "may be past them"
        )
    if not snapshot.get("globals_available"):
        raise ValueError(
            f"not found; visible: {_visible(snapshot)}" + _member_hint(worker, stop, snapshot, name)
        )
    proof = snapshot.get("globals_proof") or {}
    if "unproven" not in proof and name not in (proof.get("names") or {}):
        _with_globals(worker, stop, snapshot, name)
        proof = snapshot.get("globals_proof") or {"unproven": "the worker gave no proof"}
    function = display_function(snapshot.get("function")) or "this function"
    if "unproven" in proof or name not in (proof.get("names") or {}):
        raise ValueError(
            f"not found: {name!r} is not a local here. It may be a module global, but Peeky "
            f"could not prove that {function} reads it from there "
            f"({str(proof.get('unproven') or 'no proof for this name')[:300]}), so module "
            "globals are not read"
        )
    if (how := proof["names"][name]) is not None:
        raise ValueError(
            f"not found: {_bound(name, how, function)}. A module global of that name is not "
            f"what {function} reads here, so it is not read; visible: {_visible(snapshot)}"
        )
    module = [scope for scope in snapshot["scopes"] if scope.get("globals")]
    found = _scope_lookup(worker, stop, snapshot, module, name)[0] if module else None
    if found is None:
        raise ValueError(
            f"not found: {name!r} is neither a local nor a module global here; visible: "
            f"{_visible(snapshot)}"
        )
    return found, True


def _keyed(variable, children):
    """Whether debugpy lists ``variable`` by key (a dict or set: `len()`, and not a
    list, tuple or deque), whose entries' names are their keys' reprs, not attributes."""
    return variable.get("type") not in _PY_SEQUENCES and any(
        child["name"] == "len()" for child in children
    )


def _resolve(worker, stop, snapshot, segments, pointers=False):
    """The variable a parsed selector names. ``pointers``: a C/C++ target, where `p[0]`
    reads a scalar pointer's pointee (not Rust, whose references and Boxes LLDB also
    shows as pointers, and whose target may have been freed). A value read through a
    module global carries ``"scope": "globals"``."""
    variable, module = _root(worker, stop, snapshot, segments[0])
    path = segments[0]
    pages = {}  # {handle: its first page}, fetched once per selector

    def first_page(handle):
        if handle not in pages:
            pages[handle] = worker.dispatch("expand", {**stop, "handle": handle, "start": 0})
        return pages[handle]

    for segment in segments[1:]:
        handle = variable.get("variablesReference", 0)
        entry = segment.startswith("[#")
        if entry and not pointers:
            raise ValueError(
                f"{segment} not read: [#N] is the Nth entry of a C++ map or set; index a "
                "sequence with [N], and select a mapping's entry by its key"
            )
        index_segment = pointers and segment.startswith("[") and _key(segment) is None and not entry
        if index_segment and not handle and _null_pointer(variable):
            raise ValueError(f"{path} is NULL: it points at nothing")
        if (
            index_segment
            and variable.get("text")
            and not variable.get("unavailable")
            and (pointee := _c_pointee(variable)) is not None
        ):
            # A pointer to a C++ string, shown with its text (`0x… "apple"`): p[0] is
            # that string, as C++ reads it; the worker lists no members (eval 6).
            if _first_index(segment):
                variable = _dereferenced(variable, pointee, path == segments[0])
                path += segment
                continue
            raise ValueError(_beyond_pointee(variable, path, pointee, segment, []))
        if index_segment and handle and not variable.get("unavailable"):
            pointee = _c_pointee(variable)
            unresolved = pointee is None and _unresolved_indirection(variable)
            first = first_page(handle) if pointee is not None or unresolved else None
            # A single `*p` child is a pointer to one scalar (#100), whatever its type's name.
            if first is not None and _pointee(variable, first["variables"]) is None:
                listed = [child for child in first["variables"] if child["name"] not in _GROUPS]
                if unresolved:
                    raise ValueError(
                        f"{_shown(segment)} not read: {path} ({variable.get('type')}) shows "
                        "only an address, under a type name the build's debug information "
                        "does not resolve to a pointer or a reference, which C indexes "
                        "differently; index the array where it is declared, or select "
                        "fields with `.`"
                    )
                # #102: the debugger lists what a pointer to an array, a container or a
                # struct points at as the pointer's own children. In C, p[0] is that
                # pointee and p[1] the one after it, which no pointer's length tells.
                _readable(variable, path, pointee, listed)
                if _first_index(segment):
                    variable = _dereferenced(variable, pointee, path == segments[0])
                    path += segment
                    continue
                raise ValueError(_beyond_pointee(variable, path, pointee, segment, listed))
        if not handle and variable.get("unavailable"):
            raise ValueError(f"{variable['name']!r} has no value here: {variable.get('reason')}")
        if not handle:
            if variable.get("text"):
                value = str(variable.get("value"))
                what = (
                    "points at a C++ string, shown with its address and text"
                    if _c_pointee(variable) is not None
                    else "is a C++ string, shown whole as its text"
                )
                raise ValueError(
                    f"{path} ({variable.get('type')}) {what} ({value[:80]}): Peeky selects "
                    "neither its characters nor the library's members inside it; capture "
                    f"{path} and compare its text (in SQL, substr() and instr() over the "
                    "captured value)"
                )
            if variable.get("depth_limited"):
                raise ValueError(
                    f"cannot expand {variable['name']!r}: it is at the inspection depth limit "
                    f"(4 levels below a local)"
                )
            value = variable.get("value")
            if "view" not in variable and value in ("null", "None"):
                # Java's null and Python's None have no members.
                raise ValueError(f"{variable['name']!r} is {value}: it has no members to select")
            shown = (
                f"; value {value[:80]}" if isinstance(value, str) and "view" not in variable else ""
            )
            if variable.get("view") == "unproven":
                raise ValueError(f"cannot expand {variable['name']!r}: {_UNPROVEN_RUST}")
            keyed = (
                f"; {path} is the value for that key, as in C++ (an entry's key and value are "
                f"{variable['_map']}[#N].first and .second)"
                if variable.get("_map")
                else ""
            )
            raise ValueError(
                f"cannot expand {variable['name']!r} ({variable.get('view', 'leaf')}{shown})"
                + keyed
            )
        if pointers and segment.startswith("["):
            # #119: LLDB lists a C++ map's or set's entries as [0], [1], ... in iteration
            # order, where C++'s m[k] is the value for key k.
            kind = _associative(variable)
            if kind is None and (variable.get("aliased_type") == "?" or _unplaced(variable)):
                kind = _undecided(variable, first_page(handle)["variables"])
            if kind is not None:
                whole = _whole_strings(snapshot)
                variable = _entry(worker, stop, variable, path, segment, kind, first_page, whole)
                path += segment
                continue
            if entry:
                raise ValueError(_not_associative(variable, path, segment))
        if segment.startswith("[-"):
            variable = _from_end(worker, stop, variable, int(segment[2:-1]), segment)
            path += segment
            continue
        key = _key(segment)
        length = variable.get("indexedVariables")
        if (index := _INDEX.fullmatch(segment)) and type(length) is int and length > 512:
            # An element past one listing's bound (`es[699].a`): fetched alone (#105).
            if int(index[1]) >= length:
                raise ValueError(f"{segment} is out of range: {path} has {length} elements")
            page = worker.dispatch("expand", {**stop, "handle": handle, "index": int(index[1])})
            found = next((v for v in page["variables"] if v["name"] == segment), None)
            if found is None:
                raise ValueError(f"{segment} not found: the debugger lists no such element")
            variable = found
            path += segment
            continue
        found, children, incomplete = _page_find(worker, stop, handle, segment, first_page)
        if found is None and incomplete:
            raise Refused(
                "BUDGET_EXHAUSTED",
                f"{_shown(segment)} is beyond the bounded inspection window (512 children)",
            )
        if found is None and not segment.startswith("[") and not _keyed(variable, children):
            # An attribute debugpy filed under a group (`f.__defaults__` is in `special
            # variables`). A dict's or set's groups also hold entries named by their keys'
            # reprs, which an attribute name must never read.
            grouped = _grouped(worker, stop, children, segment, _groups(snapshot))
            if len(grouped) > 1:
                raise ValueError(
                    f"{_shown(segment)} is ambiguous: debugpy lists {len(grouped)} members of "
                    f"{variable['name']!r} with that name"
                )
            found = grouped[0] if grouped else None
        if (
            found is None
            and segment.startswith("[")
            and key is None
            and (listed := java_list(variable, children))
        ):
            found = _list_element(worker, stop, *listed, int(segment[1:-1]), segment)
        if (
            pointers
            and found is None
            and segment == "[0]"
            and (pointee := _pointee(variable, children))
        ):
            # C and C++: p[0] is *p, the one element a pointer shows.
            found = pointee
        if found is None and segment.startswith("[") and key is None:
            length = _declared_length(variable, children)
            if any(child["name"] == "more" for child in children) or (
                length is not None and int(segment[1:-1]) < length
            ):
                # debugpy lists only the first 100 items, then a `more` group: absence
                # from the listing is not absence from the sequence.
                raise ValueError(
                    f"{_shown(segment)} is beyond the debugger's visible window"
                    + (f" (the sequence has {length} elements)" if length is not None else "")
                    + "; capture a smaller index or a slice-sized field instead"
                )
        if found is None and key is not None and _partial(children):
            raise ValueError(
                f"{_shown(segment)} is not among the entries the debugger lists for "
                f"{variable['name']!r} (it shows only part of a large mapping)"
            )
        if found is None:
            raise ValueError(_not_found(variable, segment, children, path, pointers))
        if segment.startswith("[") and not _genuine(found, children, key is not None):
            # Keys and indexes are matched by the entry's displayed name, which is its key's
            # repr. debugpy marks an entry evaluable only when that repr evaluates back to
            # the key, so an object whose __repr__ mimics `'a'` or `1` is refused rather
            # than read as that key.
            raise ValueError(
                f"{_shown(segment)} is ambiguous: an entry displays as {found['name']} but its "
                "key is not that plain value (an object whose repr looks like it)"
            )
        variable = found
        path += segment if segment.startswith("[") else f".{segment}"
    return {**variable, "scope": "globals"} if module else variable


_RUST_TEXT = frozenset({"&str", "&mut str", "alloc::string::String *"})


# Owned Rust containers are raw: a moved or dropped local can still point at freed memory.
def _owned_rust_types():
    from peeky.rust_symbols import OWNED_CONTAINERS

    return frozenset(OWNED_CONTAINERS)


_OWNED_RUST_TYPES = _owned_rust_types()


def _read_rust_types():
    from peeky.rust_symbols import QUALIFIED_CONTAINERS

    return frozenset(QUALIFIED_CONTAINERS) | _OWNED_RUST_TYPES | _RUST_TEXT


# Containers whose contents Peeky can read somewhere (their element type is qualified).
_READ_RUST_TYPES = _read_rust_types()


def _rust_composite(kind):
    """Whether a Rust Vec or slice holds tuples or structs Peeky may read: a tuple of
    integer, bool, float or char elements, or a struct (only the build's debug info says
    whether its fields are all such scalars). Nested containers, references, enums and
    tuples of anything else are never read. `&Vec<T>` is `Vec<T> *`."""
    from peeky.rust_symbols import ELEMENT_TYPES, composite_container

    composite = composite_container(kind.removesuffix(" *"))
    if composite is None:
        return False
    element = composite[1]
    if element.startswith("("):
        parts = element.removeprefix("(").removesuffix(")").removesuffix(",").split(", ")
        return all(part in ELEMENT_TYPES for part in parts)
    return not element.startswith(("alloc::", "core::", "std::", "&", "*", "[", "("))


def _lldb_type(variable):
    """The debugger's name for a value's type, where a Rust one is shown (`Vec<T> *` for
    a `&Vec<T>` parameter shown as such)."""
    return str(variable.get("debugger_type") or variable.get("type") or "")


def _unread_rust(kind):
    """Whether a Rust container's elements are never read: `&Vec<T>` is `Vec<T> *`."""
    return (
        kind.startswith(_RUST_CONTAINERS)
        and kind.removesuffix(" *") not in _READ_RUST_TYPES
        and not _rust_composite(kind)
    )


_OWNED_RUST = (
    "This owned Rust Vec or String shows its raw fields: Peeky reads an owned local's "
    "contents only where the build's MIR proves it initialized at the stop line, in the "
    "innermost frame, and here it may be moved, dropped or not yet assigned around this "
    "line (#53). Capture `.len` for the length, probe a line where it is live, or read it "
    "where it is passed as &Vec, &[T], &String or &str."
)
_COMPOSITE_RUST = (
    "This Rust container of tuples or structs shows LLDB's summary, not its elements. "
    "Peeky reads them only when every field is an integer, bool, float or char (the "
    "build's debug info decides; no nested containers, references or enums), and an owned "
    "Vec only where the build's MIR proves it initialized at the stop line, in the "
    "innermost frame (#53). Capture `.len` for the length, probe a line where it is live "
    "or read it where it is passed as &Vec or &[T], and check contents with `peeky verify`."
)
_UNPROVEN_RUST = (
    "this Rust Box (or a pointer the build's MIR does not identify, a reference into a "
    "value that may be moved or dropped, or a pointer inside a struct, tuple or array "
    "that may be) shows only its pointer: Peeky reads what it "
    "points at only where the build's MIR proves it initialized (a reference: still used "
    "at or after the stop, or no call or drop since its last use, or into a shared "
    "parameter's target or a live enclosing variable; a raw pointer: never) in the "
    "innermost frame of an unoptimized build. Here it "
    "may be moved or dropped, or the MIR cannot say (a generic `T`, `Self` or `impl "
    "Trait`, an `async fn`'s parameter, a closure's capture, an unmangled `#[no_mangle]` "
    "function), so "
    "its pointer can reach freed memory (#101, #108, #110). A plain function's own local reads "
    "on a line where it is live; otherwise capture what it holds where it is created"
)
_STALE_RUST = (
    "This is (or is inside) a Rust local that may own memory through a pointer (a "
    "struct, tuple, array or enum holding a Box, Vec, String or Rc) and that the build's "
    "MIR does not prove initialized here: it may be moved, dropped, partly moved or not "
    "yet assigned, or nothing is proven (an optimized build, a caller frame's call that "
    "may move it, a frame the MIR cannot match). Its fields show the bytes left in its "
    "own storage, which may be a moved-from copy, and its pointers show only their "
    "address (#108). Probe a line where it is live, or read it where it is passed"
)
_UNREAD_RUST = (
    "Peeky reads Rust contents only for String, &str and containers of integer, bool, "
    "float and char elements (or of tuples of them, and structs of them your crate "
    "defines); this value is LLDB's "
    "summary (its type and address), not the contents. "
    "Capture `.len` for the length, and check contents with `peeky verify`."
)
_RUST_CONTAINERS = (
    "alloc::vec::Vec<",
    "alloc::string::String",
    "alloc::collections::",
    "&[",
    "&mut [",
    "&str",
)

# Groups debugpy files some mapping entries under (by value type); not reachable.
_VALUE_GROUPS = frozenset({"function variables", "class variables"})
# debugpy's grouping nodes: not entries, fields or elements of the value itself.
_GROUPS = frozenset(
    {"special variables", "function variables", "class variables", "protected variables"}
)


def _shown(segment):
    """A path step as the user wrote it: `'name'`, `[0]`, `['key']`."""
    return segment if segment.startswith("[") else repr(segment)


# debugpy lists at most 500 mapping entries, then a child with one of these names.
_PARTIAL_MARKERS = ("Unable to handle:", "Too large", "more")


def _genuine(child, siblings, key):
    """Whether a name-matched entry is the plain key it displays as.

    A key must be evaluable. An index must be evaluable when its siblings are (debugpy
    lists and dicts mark plain keys and elements; other debuggers mark nothing).
    """
    if child.get("evaluable"):
        return True
    return not key and not any(sibling.get("evaluable") for sibling in siblings)


def _partial(children):
    """Whether debugpy listed only part of a mapping: its truncation marker, or fewer
    entries than the mapping's own `len()`."""
    if any(child["name"].startswith(_PARTIAL_MARKERS) for child in children):
        return True
    if any(child["name"] in _GROUPS for child in children):
        # Grouped entries (function or class values) count in len() but are not listed
        # at this level: the comparison would say "partial" for a complete mapping.
        return False
    entries = [c for c in children if c["name"] not in _GROUPS and c["name"] != "len()"]
    for child in children:
        if child["name"] == "len()" and re.fullmatch(r"[0-9]{1,9}", str(child.get("value"))):
            return int(child["value"]) > len(entries)
    return False


def _not_found(variable, segment, children, path=None, pointers=False):
    """Why `segment` is absent, with the selectors that would work."""
    # Attribute names were looked for in the groups already; keys and indexes never are.
    grouped = sorted({child["name"] for child in children} & _VALUE_GROUPS)
    if not segment.startswith("["):
        grouped = []
    reach = (
        f" (debugpy files entries whose values are functions or classes under "
        f"{' and '.join(repr(group) for group in grouped)}, which selectors cannot reach)"
        if grouped
        else ""
    )
    return _absence(variable, segment, children, path, pointers) + reach


def _absence(variable, segment, children, path=None, pointers=False):
    names = [child["name"] for child in children if child["name"] not in _GROUPS]
    shown = ", ".join(names[:20]) + (f", ... ({len(names)} total)" if len(names) > 20 else "")
    kind = variable.get("type") or ""
    label = repr(variable["name"]) + (f" ({kind})" if kind else "")
    if not segment.startswith("[") and repr(segment) in names:
        keys = [name for name in names if name != "len()"]
        listed = ", ".join(keys[:20]) + (f", ... ({len(keys)} total)" if len(keys) > 20 else "")
        return (
            f"{_shown(segment)} not found: {label} is a mapping and {segment!r} is one of its "
            f"keys; select it with [{segment!r}], not .{segment} (keys: {listed})"
        )
    if (
        segment.startswith("[")
        and _key(segment) is None
        and not any(_INDEX.fullmatch(name) for name in names)
    ):
        message = (
            f"{_shown(segment)} not found: {label} has no indexed elements in the debugger's view "
            f"(children: {shown or 'none'})"
        )
        length = next((name for name in ("len", "length") if name in names), None)
        if pointers and names == [f"*{variable['name']}"]:
            # A C/C++ pointer: the debugger knows what it points at, never how many.
            shown_path = path or variable["name"]
            message += (
                f"; {shown_path} is a pointer, which carries no length: {shown_path}[0] "
                "reads what it points at; index the array where it is declared (in the "
                "caller: `peeky session step --out`, or a probe there)"
            )
        elif variable.get("view") == "memory":
            if kind in _RUST_TEXT:
                message += "; text read from memory is shown whole, not by element"
        elif _unread_rust(_lldb_type(variable)):
            message += (
                f"; Peeky does not read {kind}'s elements: it reads Rust contents only for "
                "String, &str and containers of integer, bool, float and char elements, "
                "or of tuples, and structs your crate defines, whose fields are all such "
                "scalars"
                + (f". Capture {variable['name']}.{length} for the length" if length else "")
            )
        elif _rust_composite(_lldb_type(variable)):
            message += (
                f"; Peeky reads {kind}'s elements only when every field of the tuple or "
                "struct is an integer, bool, float or char (the build's debug info decides), "
                "and an owned Vec only where the build's MIR proves it initialized at the stop "
                "line (innermost frame), with a debugger that supports readMemory"
                + (f". Capture {variable['name']}.{length} for the length" if length else "")
                + ", and check contents with `peeky verify`"
            )
        elif _lldb_type(variable).startswith(_RUST_CONTAINERS):
            message += (
                "; Peeky reads Rust contents only for reference parameters (&[T], &str, "
                "&Vec<T>, &String, or &mut) and for owned Vec<T>/String locals the "
                "build's MIR proves initialized at the stop line (innermost frame), where "
                "T is an integer, bool, float or char type (or a tuple, or a struct your "
                "crate defines, of them), "
                "with a "
                "debugger that supports readMemory (lldb-dap from LLVM 20). This one stays "
                "raw: a moved, dropped or not-yet-assigned local can still point at freed "
                "memory"
                + (f". Capture {variable['name']}.{length} for the length" if length else "")
                + ", and check contents with `peeky verify`"
            )
        return message
    return f"{_shown(segment)} not found in {label}; children: {shown or 'none'}"


def _names(child, segment):
    """LLDB names elements `[1]`; debugpy `1`, or `001` in long sequences. debugpy
    names mapping entries by the key's repr (`'id'`), which a key segment holds."""
    name = child["name"]
    if (key := _key(segment)) is not None:
        return name == key
    if not segment.startswith("["):
        return name == segment
    match = _INDEX.fullmatch(name)
    return name in (segment, segment[1:-1]) or bool(match and int(match[1]) == int(segment[1:-1]))


# A one-dimensional fixed array type such as `int[4]` or `unsigned char[16]`.
_ARRAY = re.compile(r"[^\[\]]*\w\[([0-9]{1,9})\]")
_PY_SEQUENCES = {"list", "tuple", "deque"}


def _declared_length(variable, children):
    """A length known independently of how many children the debugger chose to list.

    Only two sources qualify: debugpy's `len()` for real Python sequences, and a
    fixed array length in a C/C++ type (`int[4]`) or JVM array value (`int[4]@12`).
    Container summaries such as LLDB's `size=N` and DAP `indexedVariables` can equal a
    capped child count (e.g. `target.max-children-count`), so they never count.
    """
    if variable.get("view") == "memory" and type(variable.get("length")) is int:
        # A container Peeky read from memory states its own length; text states its
        # length in bytes and has no elements.
        return None if variable.get("type") in _RUST_TEXT else variable["length"]
    if variable.get("type") in _PY_SEQUENCES:
        for child in children:
            if child["name"] == "len()" and re.fullmatch(r"[0-9]{1,9}", str(child.get("value"))):
                return int(child["value"])
        return None
    if match := _ARRAY.fullmatch(str(variable.get("type") or "").strip()):
        return int(match[1])
    # The length is java-debug's; Peeky may append its own element preview (` [1, 2]`).
    shown = str(variable.get("value") or "")
    if match := re.fullmatch(r"\w+\[([0-9]{1,9})\]@[0-9a-fx]+(?: \[.*\])?", shown, re.S):
        return int(match[1])
    return None


def _from_end(worker, stop, variable, offset, segment):
    """Element ``len - offset``, only when the debugger states the length and every
    element is individually visible; a partial view (e.g. debugpy's `more` group for
    long lists) never stands in for the end of the sequence."""
    handle = variable["variablesReference"]
    elements, children, start = {}, [], 0
    while True:
        page = worker.dispatch("expand", {**stop, "handle": handle, "start": start})
        children.extend(page["variables"])
        for child in page["variables"]:
            if match := _INDEX.fullmatch(child["name"]):
                elements[int(match[1])] = child
        if not page["truncated"]:
            break
        if start + 64 > 511 or page.get("remaining_nodes", 1) <= 0:
            raise Refused(
                "BUDGET_EXHAUSTED",
                f"{segment!r} needs the sequence length, but it has more elements than the "
                "bounded inspection window (512); use a non-negative index",
            )
        start += 64
    count = len(elements)
    if not count and (listed := java_list(variable, children)):
        # An ArrayList states its size; `[-1]` is its last element.
        backing, size = listed
        return _list_element(worker, stop, backing, size, size - offset, segment)
    if not count or sorted(elements) != list(range(count)):
        raise ValueError(f"{segment!r}: not an indexed sequence; use a non-negative index")
    length = _declared_length(variable, children)
    if length is None:
        raise ValueError(
            f"{segment!r}: the debugger does not state an independent length for this type; "
            "use a non-negative index"
        )
    if length != count:
        raise ValueError(
            f"{segment!r}: only {count} of {length} elements are individually visible; use a "
            "non-negative index"
        )
    if offset < 1 or offset > count:
        raise ValueError(f"{segment!r} is out of range for {count} elements")
    return elements[count - offset]


def _expander(worker, stop):
    """fetch(handle, count) for java_preview: a value's first children."""

    def fetch(handle, count):
        page = worker.dispatch("expand", {**stop, "handle": handle, "start": 0, "count": count})
        if page.get("truncated") and len(page["variables"]) < count:
            # Cut short by the shared node budget, not the value's end: read it again.
            raise ValueError("A preview is beyond the bounded inspection window")
        return page["variables"]

    return fetch


def _list_element(worker, stop, backing, size, index, segment):
    """Element ``index`` of an ArrayList: ``elementData[index]``, within its size."""
    if not 0 <= index < size:
        raise ValueError(f"{_shown(segment)} is out of range: the list has {size} elements")
    if index > 511:
        raise Refused(
            "BUDGET_EXHAUSTED",
            f"{_shown(segment)} is beyond the bounded inspection window (512 children)",
        )
    try:
        page = worker.dispatch(
            "expand", {**stop, "handle": backing["variablesReference"], "start": index // 64 * 64}
        )
    except ValueError as exc:
        if "exceeds bounded expansion" not in str(exc):
            raise
        # The backing array's capacity, not the list's size, is over the bound.
        raise Refused(
            "BUDGET_EXHAUSTED",
            f"{_shown(segment)} cannot be read: the list's backing array holds more than 512 "
            "slots, Peeky's inspection bound (read a field of an element you can reach)",
        ) from None
    found = next((child for child in page["variables"] if _names(child, f"[{index}]")), None)
    if found is None and (page.get("truncated") or page.get("remaining_nodes", 1) <= 0):
        # Cut short by the shared node budget: a fresh inspection can read it.
        raise Refused(
            "BUDGET_EXHAUSTED", f"{_shown(segment)} is beyond the bounded inspection window"
        )
    if found is None:
        raise ValueError(f"{_shown(segment)} is not in the list's backing array")
    return found


# Errors a fresh inspection can cure: it restores the expansion node budget.
_BUDGET = re.compile(
    r"budget|beyond the bounded|bounded inspection window|exceeds bounded expansion"
)


def _previewed(worker, stop, variable):
    """A field's or element's value with its preview (#136): a selector walks to it
    without previewing the listings on its way. A worker that cannot preview (one that
    predates it), or a value it has none for, keeps the debugger's text."""
    handle = variable.get("variablesReference")
    if variable.get("unavailable") or type(handle) is not int or handle <= 0:
        return variable
    # What a pointer points at (`p[0]`, #102), which lldb-dap lists as the pointer's
    # children: the selector asked for it, so it is read as the pointer's target. Only
    # a value `_dereferenced` made: lldb-dap's own `*pp` child is a value of its own (a
    # pointer, where `pp` is a pointer to one), and never a pointee's preview.
    pointee = {"pointee": True} if variable.get("dereferenced") is True else {}
    from peeky.errors import PeekyError

    try:
        shown = worker.dispatch("preview", {**stop, "handle": handle, **pointee})
    except (ValueError, PeekyError) as exc:
        if getattr(exc, "session_closed", False):
            raise
        # The value is read; only its preview is not (an older worker, a spent budget).
        return variable
    return {**variable, "value": shown["value"]} if shown.get("value") else variable


def _capture(
    worker,
    stop,
    selectors,
    hit,
    phase,
    snapshot=None,
    children=False,
    preview=False,
    pointers=False,
    frame=0,
):
    """Rows for each selector at one stop.

    Selectors share one inspection (its handles stay valid until the next inspect or
    change); a selector that runs out of expansion budget is retried once on a fresh
    inspection, so sharing never turns a readable value into an error. With
    ``children``, an expandable value also lists its first page of children, each with
    the selector that reads it. ``frame`` is the index of the stack frame every selector
    reads (0, the innermost, by default).
    """

    # Asked for by name, a local or global past the listing bound is read too; only
    # those roots are previewed (#136).
    roots = _roots(selectors)
    named = {**({"names": roots} if roots else {}), "previews": roots}

    def inspect():
        if not frame:
            return worker.dispatch("inspect", {**stop, **named})
        shown = worker.dispatch("inspect", {**stop, "frame": frame, **named})
        if shown.get("frame") != frame:
            # A worker that predates `frame` ignores it and reads the innermost frame.
            raise ValueError(
                "This worker cannot read a caller's frame (it predates --frame); update "
                "Peeky on the worker host"
            )
        return shown

    rows = []
    for selector in selectors:
        row = {"hit": hit, "phase": phase, "selector": selector}
        fresh = snapshot is None
        if fresh:
            snapshot = inspect()
        index = snapshot.get("frame", 0)
        read = snapshot["frames"][index] if len(snapshot["frames"]) > index else {}
        row["line"] = read.get("line")
        if read.get("name"):
            # Tells a function body from a nested generator/lambda frame on the same line.
            row["function"] = read["name"]
        if index:
            row.update(frame=index, location=snapshot.get("location"))
        try:
            try:
                variable = _resolve(worker, stop, snapshot, parse_selector(selector), pointers)
            except ValueError as exc:
                if fresh or not _BUDGET.search(str(exc)):
                    raise
                snapshot = inspect()
                variable = _resolve(worker, stop, snapshot, parse_selector(selector), pointers)
        except ValueError as exc:
            row["error"] = str(exc)
            if _in_withheld(snapshot, selector):
                # Inside a local withheld here (no value yet): `--set` refuses it too.
                row["settable"] = False
        else:
            if len(parse_selector(selector)) > 1:
                variable = _previewed(worker, stop, variable)
            if variable.get("unavailable"):
                # A placeholder such as "<error: variable not available>" is not a value.
                reason = variable.get("reason", "unavailable")
                row.update(
                    type=variable.get("type"),
                    unavailable=True,
                    error=(
                        f"{variable.get('withheld_as') or 'no value yet'}: {reason}"
                        if variable.get("withheld")
                        else f"no value here: optimized out or not live (debugger: {reason})"
                        if snapshot.get("optimized")
                        else f"the debugger could not read a value (debugger: {reason})"
                    ),
                )
            else:
                row.update(value=variable["value"], type=variable.get("type"))
                if preview:
                    # Only this value's own expansions, never its siblings', so the same
                    # value always shows the same way; a preview cut short by the shared
                    # budget is read again on a fresh inspection, else left out.
                    try:
                        try:
                            shown = java_preview(variable, _expander(worker, stop))
                        except ValueError:
                            snapshot = inspect()
                            variable = _resolve(
                                worker, stop, snapshot, parse_selector(selector), pointers
                            )
                            shown = java_preview(variable, _expander(worker, stop))
                    except ValueError:
                        shown = None
                    if shown:
                        row["value"] = shown
                if variable.get("view") == "unproven":
                    row["note"] = _UNPROVEN_RUST[0].upper() + _UNPROVEN_RUST[1:] + "."
                elif "view" not in variable and _lldb_type(variable) in _OWNED_RUST_TYPES:
                    row["note"] = _OWNED_RUST
                elif variable.get("stale"):
                    row["note"] = _STALE_RUST + "."
                elif "view" not in variable and _unread_rust(_lldb_type(variable)):
                    row["note"] = _UNREAD_RUST
                elif "view" not in variable and _rust_composite(_lldb_type(variable)):
                    row["note"] = _COMPOSITE_RUST
                elif isinstance(variable.get("note"), str):
                    # A value the runtime shows but cannot vouch for (C, C++: a line a loop
                    # returns to; initialization not judged), or one read through it (#148).
                    row["note"] = variable["note"]
            if "view" in variable:
                row["view"] = variable["view"]
            if variable.get("stale"):
                row["stale"] = True
            if variable.get("scope") == "globals":
                row["scope"] = "globals"
            if variable.get("identity") and not variable.get("unavailable"):
                # Which object the value is (Python): see PythonAdapter.object_identity.
                row["identity"] = variable["identity"]
            if type(variable.get("settable")) is bool:
                # Whether --set can change this value here: a local (C, C++, Rust, Java),
                # or a Java field or array element.
                row["settable"] = variable["settable"]
            if isinstance(variable.get("not_settable"), str):
                # Why no stop of this build can change it (#138: `register`).
                row["not_settable"] = variable["not_settable"]
            if variable.get("debugger_type"):
                row["debugger_type"] = variable["debugger_type"]
            if children and variable.get("variablesReference"):
                try:
                    try:
                        listing = _children(
                            worker, stop, selector, variable, pointers, _whole_strings(snapshot)
                        )
                    except ValueError as exc:
                        if not _BUDGET.search(str(exc)):
                            raise
                        # As for values: a fresh inspection restores the node budget.
                        snapshot = inspect()
                        variable = _resolve(
                            worker, stop, snapshot, parse_selector(selector), pointers
                        )
                        listing = _children(
                            worker, stop, selector, variable, pointers, _whole_strings(snapshot)
                        )
                    if row.get("note") and listing.get("note"):
                        # How to read a map's entries never replaces why the value
                        # itself may not be current (#148).
                        listing["note"] = f"{row['note']}; {listing['note']}"
                    row.update(listing)
                except ValueError as exc:
                    row["children_error"] = str(exc)
                listed = row.get("children", ())
                if "note" not in row and any(c.get("view") == "unproven" for c in listed):
                    # A caller's struct reads, but not what its pointers reach (#108).
                    row["note"] = _UNPROVEN_RUST[0].upper() + _UNPROVEN_RUST[1:] + "."
                elif "note" not in row and any(c.get("stale") for c in listed):
                    row["note"] = _STALE_RUST + "."
        rows.append(row)
    return rows


# How the worker prefixes the note of what is read through a local shown with one (#148).
_THROUGH = re.compile(r"^read through `[^`\n]+`: ")


def _whole_strings(snapshot):
    """Whether LLDB's summary of a std::string shows all of it at this stop (libc++)."""
    return snapshot.get("whole_string_summaries") is True


def _roots(selectors):
    """The distinct first names of these selectors (at most 32)."""
    roots = []
    for selector in selectors:
        try:
            first = parse_selector(selector)[0]
        except ValueError:
            continue
        if first.isidentifier() and first not in roots:
            roots.append(first)
    return roots[:32]


def _in_withheld(snapshot, selector):
    """True if ``selector`` reads inside a local of the snapshot's own scopes that every
    row of its name withholds (``withheld``: no value yet)."""
    try:
        segments = parse_selector(selector)
    except ValueError:
        return False
    if len(segments) < 2:
        return False
    rows = [
        row
        for scope in snapshot.get("scopes", ())
        if not scope.get("globals")
        for row in scope.get("variables", ())
        if row.get("name") == segments[0]
    ]
    return bool(rows) and all(row.get("withheld") for row in rows)


def _settable_here(snapshot):
    """`; settable here: a, b` (or none) when the debugger reports settability."""
    known = [
        variable
        for scope in snapshot["scopes"]
        for variable in scope["variables"]
        if type(variable.get("settable")) is bool
    ]
    if not known:
        return ""
    names = [variable["name"] for variable in known if variable["settable"]]
    return f"; settable here: {', '.join(names[:20]) if names else 'none'}"


class _Coded:
    """The worker's own errors, given their codes from the worker's message alone: a
    selector or the names an error lists are added later and never decide the code."""

    def __init__(self, worker):
        self.worker = worker

    def dispatch(self, operation, args):
        try:
            return self.worker.dispatch(operation, args)
        except Refused:
            raise
        except ValueError as exc:
            raise Refused(_code(exc), str(exc)) from None


def _element(worker, stop, snapshot, segments, target, negative):
    """The debugger's name (`[3]`) of the element a selector whose last index is not
    plain (`xs[03]`, `xs[-1]`) reads, which `set` changes as the read shows it."""
    try:
        element = _resolve(worker, stop, snapshot, segments)
    except Refused as exc:
        raise Refused(exc.code, f"{target}: {exc}") from None
    except ValueError as exc:
        raise Refused(
            "MUTATION_REJECTED" if negative else "NOT_FOUND", f"{target}: {exc}"
        ) from None
    name = str(element.get("name"))
    if not re.fullmatch(r"\[(?:0|[1-9][0-9]{0,9})\]", name):
        raise Refused(
            "MUTATION_REJECTED",
            f"{target}: {segments[-1]} reads {name!r}, not an element of "
            f"{''.join(segments[:-1])} the debugger lists by index",
        )
    return name


def _launch_set(line, name, value):
    """A change as the check before the run judges it (#138): its line and name, and
    whether its value is a text (#137: a string's, not an integer's)."""
    return {"line": line, "name": name, **({"text": True} if value.startswith('"') else {})}


def apply_set(worker, stop, name, value):
    """Apply one `--set name=value` at this stop: a root local, or a field or array
    element a selector names (Java: `current.end`, `this.count`, `xs[0]`, `xs[-1]`; C,
    C++ and Rust: `s.end`, `s.in.lo`, `xs[2]`, `t.__0`, which the worker proves the
    frame holds itself, #105)."""
    worker = _Coded(worker)
    segments = parse_selector(name)
    snapshot = worker.dispatch("inspect", {**stop, "names": [segments[0]], "previews": False})
    if len(segments) == 1:
        # As LLDB names it (`s @ main.rs:81` however written).
        name = segments[0]
        own = [scope for scope in snapshot["scopes"] if not scope.get("globals")]
        # Listed directly, or in a group of debugpy's (a local holding a function).
        found, scope = _scope_lookup(worker, stop, snapshot, own, name)
        if found is not None and found not in scope["variables"]:
            # Reading the group revoked the scope's authority to change: inspect again.
            fresh = worker.dispatch("inspect", {**stop, "names": [segments[0]], "previews": False})
            scope = next((s for s in fresh["scopes"] if s["name"] == scope["name"]), None)
        if scope is None:
            raise Refused(
                "NOT_FOUND",
                f"Intervention target {name!r} is not a visible local; "
                f"visible: {_visible(snapshot, module=False)}"
                + _member_hint(worker, stop, snapshot, name).replace(
                    "select it as", "it can be read as"
                )
                + "; only the stopped function's own locals can be changed",
            )
        try:
            return worker.dispatch(
                "set_variable",
                {**stop, "reference": scope["reference"], "name": name, "value": value},
            )
        except Refused as exc:
            # Classified by the debugger's reason, never by the names listed after it.
            raise Refused(exc.code, f"{exc}{_settable_here(snapshot)}") from None
    target = f"Intervention target {name!r}"
    for scope in snapshot["scopes"]:
        for row in scope["variables"] if not scope.get("globals") else ():
            if row.get("name") == segments[0] and row.get("unavailable"):
                # Withheld (declared below the stop, an initializer still running) or
                # optimized out: nothing in it holds a value a change could replace.
                raise Refused(
                    "MUTATION_REJECTED",
                    f"{target}: {segments[0]!r} has no value here ({row.get('reason')}); "
                    "nothing in it can be changed",
                )
    try:
        parent = _resolve(worker, stop, snapshot, segments[:-1])
    except Refused as exc:
        # The worker's own error (a node budget, a stale handle) keeps its code.
        raise Refused(exc.code, f"{target}: {exc}") from None
    except ValueError as exc:
        # The path names nothing here; the names the error lists never decide its code.
        raise Refused(
            "NOT_FOUND", f"{target} was not found in the inspected values: {exc}"
        ) from None
    last, value_shown = segments[-1], str(parent.get("value"))[:80]
    member = last
    if last.startswith("["):
        index = int(last[1:-1])
        if JAVA_LIST.fullmatch(str(parent.get("type"))):
            raise Refused(
                "MUTATION_REJECTED",
                f"{target}: an ArrayList's elements are objects (boxed) and cannot be "
                f"changed; change a field of one ({name}.field), or an array's element",
            )
        array = JAVA_ARRAY.fullmatch(str(parent.get("value")))
        java = array or _JAVA_OBJECT.fullmatch(str(parent.get("value")))
        if index < 0 and not array and java:
            raise Refused(
                "MUTATION_REJECTED",
                f"{target}: a negative index can be changed only in a Java array",
            )
        if not java and (index < 0 or last != f"[{index}]"):
            # The element a read of the selector shows, and marks [settable] (review of
            # #138): `[03]` is `[3]`, and `[-1]` counts from the end of a sequence whose
            # length the debugger states; the debugger names it `[N]`.
            member = _element(worker, stop, snapshot, segments, target, negative=index < 0)
        if array and not -int(array[1]) <= index < int(array[1]):
            raise Refused(
                "INVALID_REQUEST",
                f"{target}: {last} is out of range: the array has {array[1]} elements",
            )
        if array and index < 0:
            index += int(array[1])
        # java-debug names an element by its index; LLDB as `[2]`.
        if array:
            member = str(index)
    if not parent.get("variablesReference"):
        if parent.get("unavailable"):
            raise Refused(
                "MUTATION_REJECTED",
                f"{target}: the value has no value here: {parent.get('reason')}",
            )
        if parent.get("depth_limited"):
            raise Refused(
                "MUTATION_REJECTED",
                f"{target}: it is below the inspection depth limit (4 levels below a local)",
            )
        raise Refused(
            "MUTATION_REJECTED",
            f"{target}: the value it is in has no fields or elements ({value_shown})",
        )
    try:
        changed = worker.dispatch(
            "set_variable",
            {**stop, "handle": parent["variablesReference"], "name": member, "value": value},
        )
    except Refused as exc:
        raise Refused(exc.code, f"{target}: {exc}") from None
    return {**changed, "name": name}


def _code(exc):
    return classify(exc)["code"]


_KEY_NAME = re.compile(_KEY_LITERAL)


_C_QUALIFIERS = r"(?:\s*\b(?:const|volatile|restrict|__restrict|__restrict__)\b)*"


def _c_pointee_type(kind):
    """What a C/C++ pointer type points at (`int (*)[3]` is `int[3]`, `Point *const` is
    `Point`), or None for another type (an array of pointers, a function pointer)."""
    kind = str(kind or "").strip()
    if found := re.fullmatch(rf"(.+?)\s*\(\*{_C_QUALIFIERS}\s*\)\s*(\[.*\])", kind):
        return f"{found[1]}{found[2]}"
    if found := re.fullmatch(rf"(.+?)\s*\*{_C_QUALIFIERS}\s*", kind):
        return found[1]
    return None


_BARE_ADDRESS = re.compile(r"0x([0-9a-fA-F]+)(?:\s|$)")


def _c_pointee(variable):
    """What a C/C++ pointer variable points at (its type's name), or None: from its type
    as written, or for a typedef'd pointer from the build's debug information."""
    if (pointee := _c_pointee_type(variable.get("type"))) is not None:
        return pointee
    if variable.get("typedef_kind") == "pointer":
        found = variable.get("pointee_type")
        return found if isinstance(found, str) else ""
    return None


def _unresolved_indirection(variable):
    """True for a value that shows as a bare address (a pointer or a reference) under a
    type name with no `*` or `&` that the build's debug information does not resolve
    (`std::vector<T>::pointer`, a typedef two units define differently)."""
    from peeky.native import _without_templates

    kind = str(variable.get("type") or "")
    return (
        bool(kind)
        # The outermost declarator: `X<int *>::pointer` names no `*` of its own.
        and not re.search(r"[*&\[(]", _without_templates(kind))
        and variable.get("typedef_kind") not in ("pointer", "reference", "other")
        and bool(_BARE_ADDRESS.match(str(variable.get("value") or "")))
    )


def _null_pointer(variable):
    address = _BARE_ADDRESS.match(str(variable.get("value") or ""))
    return bool(address) and int(address[1], 16) == 0 and _c_pointee(variable) is not None


def _readable(variable, path, pointee, children):
    """Refuse a pointer that points nowhere: NULL, memory the debugger cannot read, or a
    value it shows without an address."""
    address = _BARE_ADDRESS.match(str(variable.get("value") or ""))
    if address and int(address[1], 16) == 0:
        raise ValueError(f"{path} is NULL: it points at nothing")
    if not address:
        raise ValueError(f"cannot tell where {path} points: the debugger shows no address")
    if children and all(child.get("unavailable") for child in children):
        reason = children[0].get("reason") or "unreadable"
        raise ValueError(f"cannot read what {path} points at (debugger: {reason})")
    if not children and re.sub(r"\b(?:const|volatile)\b", "", pointee).strip() in ("", "void"):
        raise ValueError(f"{path} points at nothing the debugger can show")


def _pointee_type(variable, pointee):
    """What a C/C++ pointer points at as a value's type: its name, and the class a
    typedef by that name stands for (`IntSet *`: `std::set<int>`)."""
    return {"type": pointee, "aliased_type": variable.get("pointee_aliased_type")}


def _first_index(segment):
    """Whether an index ``segment`` is element 0 (`[0]`, `[00]`; never `[-0]`): what a
    pointer points at (#102, #117)."""
    return segment[1] != "-" and int(segment[1:-1]) == 0


def _dereferenced(variable, pointee, root=False):
    """`*p` for a C/C++ pointer whose children the debugger lists from its pointee; it
    carries the pointer's note (#148), said as read through it where the pointer is a
    ``root`` local."""
    value = str(variable.get("value") or "")
    address = re.match(r"0x[0-9a-fA-F]+", value)
    # The debugger's summary of the pointee follows the address (`0x… size=3`).
    summary = value[address.end() :].strip() if address else ""
    shown = {
        "name": f"*{variable['name']}",
        "value": summary or f"{pointee or '*' + variable['name']} @ {address[0]}",
        "type": pointee or None,
        "variablesReference": variable["variablesReference"],
        # Its handle is the pointer's: a preview reads what that points at (#136).
        "dereferenced": True,
    }
    if variable.get("pointee_aliased_type"):
        shown["aliased_type"] = variable["pointee_aliased_type"]
    if summary and variable.get("view") == "memory":
        # A libstdc++ string's text, which the worker read from memory (#128).
        shown["view"] = "memory"
    if variable.get("text"):
        # A C++ string's: its text, with no members to list (eval 6).
        shown.update(text=True, variablesReference=0)
    for key in ("indexedVariables", "namedVariables", "string_length"):
        if type(variable.get(key)) is int:
            shown[key] = variable[key]
    if isinstance(note := variable.get("note"), str):
        shown["note"] = f"read through `{variable['name']}`: {note}" if root else note
    return shown


def _beyond_pointee(variable, path, pointee, segment, children):
    elements = any(_INDEX.fullmatch(child["name"]) for child in children)
    label = f"{path} ({variable.get('type')})"
    what = pointee or "value"
    message = (
        f"{_shown(segment)} not read: {label} points at one {what}, and in C and C++ "
        f"{path}{segment} is not inside it; a pointer carries no length. {path}[0] is the "
        f"{what} it points at"
    )
    kind = _associative(_pointee_type(variable, pointee))
    if kind in _KEYED and _key(segment) is None:
        return message + f": read {path}[0]{segment}, its value for key {segment[1:-1]}"
    if kind is not None:
        return message + f": its entries are {path}[0][#N]"
    # Counting from the end needs a length the type states (`int[3]`).
    stated = re.search(r"\[[0-9]+\]$", pointee or "")
    if elements and segment != "[-0]" and (segment[1] != "-" or stated):
        return message + f": read {path}[0]{segment} for its element"
    if elements:
        return message + f": read its elements from {path}[0][0]"
    return message + (
        "; index the array where it is declared (in the caller: `peeky session step --out`, "
        "or a probe there)"
    )


def _pointee(variable, children):
    """The one child a C/C++ scalar pointer shows (`*values`), or None. The child's name
    is the sign, whatever the type's spelling (`int *const`, `float *__restrict`, a
    typedef): no C or C++ member can be named `*values`."""
    names = [child["name"] for child in children if child["name"] not in _GROUPS]
    if names == [f"*{variable.get('name')}"]:
        return next(child for child in children if child["name"] == names[0])
    return None


# C++ associative containers as LLDB names them, their template arguments left out
# (libc++'s `std::__1::map`, libstdc++'s debug mode `std::__debug::set`, `std::pmr::map`).
_ASSOCIATIVE = re.compile(
    r"(?:(?:const|volatile)\s+)*(?:::)?std::(?:__\w+::|pmr::)*"
    r"((?:unordered_|flat_)?(?:multi)?(?:map|set))(?:\s*\b(?:const|volatile)\b|\s*&)*\s*"
)
# The containers whose keys are unique and that C++ indexes by key (`m[k]`).
_KEYED = frozenset({"map", "unordered_map", "flat_map"})
_ENTRY_NAME = re.compile(r"\[([0-9]{1,9})\]")
_STD_STRING = re.compile(r"(?:::)?std::(?:__\w+::)*(?:string|basic_string<\s*char\s*(?:,.*)?>)\s*")
# Keys compared as text: what LLDB shows of such a std::string is `"` + the key + `"`.
_PLAIN_KEY = re.compile(r"[ !#-\[\]-~]{0,256}")
_STRING_SUMMARY = re.compile(r'"(?:[^"\\]|\\.)*"(?:\.\.\.)?', re.S)
# A libstdc++ string the worker could not read whole (#128): the debugger's text, then a
# note (`"a" (shown up to a NUL at byte 1 of 3)`). It equals no key a selector writes.
_NOTED_STRING = re.compile(r'"(?:[^"\\]|\\.)*"(?:\.\.\.)? \(.+\)', re.S)
# GCC's spellings of the integer types (`long unsigned int`) as LLDB's.
_GCC_INTEGERS = {
    "short unsigned int": "unsigned short",
    "long unsigned int": "unsigned long",
    "long long unsigned int": "unsigned long long",
}


def _unqualified(kind):
    """A type without its own `const` and `volatile` (`const int` is `int`, `char *const`
    is `char *`), keeping a pointee's (`const char *`)."""
    kind = re.sub(r"(?:\s*\b(?:const|volatile)\b)+\s*$", "", str(kind or "").strip())
    if not re.search(r"[*&]", kind):
        kind = re.sub(r"^(?:(?:const|volatile)\s+)+", "", kind)
    return kind


def _associative(variable):
    """The kind of C++ map or set a value is (`map`, `unordered_set`, ...), by its type
    or, for a typedef, the class it names in the build's debug information; or None."""
    from peeky.native import _without_templates

    for kind in (variable.get("type"), variable.get("aliased_type")):
        if isinstance(kind, str) and (found := _ASSOCIATIVE.fullmatch(_without_templates(kind))):
            return found[1]
    return None


def _unplaced(variable):
    """Whether a value's type is a name Peeky cannot place: not the standard library's,
    not a pointer, array or address, and no class the debug information names (a typedef
    it could not resolve), which a map or a sequence alike could be behind."""
    from peeky.native import _without_templates

    kind = re.sub(
        r"^(?:(?:const|volatile)\s+)+|(?:\s*(?:\bconst\b|\bvolatile\b|&))+$",
        "",
        _without_templates(str(variable.get("type") or "")).strip(),
    )
    return (
        bool(kind)
        and not variable.get("aliased_type")
        # A standard class itself (`std::vector<int>`), not a member typedef of one
        # (`std::map<int, std::set<int>>::mapped_type`).
        and not re.fullmatch(r"(?:::)?std::(?:__\w+::|pmr::)*\w+", kind)
        and not re.search(r"[*\[(]", kind)
        and not _BARE_ADDRESS.match(str(variable.get("value") or ""))
    )


def _undecided(variable, children):
    """For a value whose type names no map or set Peeky knows of: "unsettled" where the
    debug information says it may be a C++ map or set ("?": typedefs of one name give
    several classes) and LLDB lists entries [0], [1], ...; "unresolved" where its type
    is a name Peeky cannot place and LLDB lists pairs with a const key, as a map's
    entries; else None."""
    entries = [child for child in children if child["name"] not in _GROUPS]
    if variable.get("aliased_type") == "?":
        return (
            "unsettled"
            if entries and all(_ENTRY_NAME.fullmatch(c["name"]) for c in entries)
            else None
        )
    return "unresolved" if _unplaced(variable) and _map_like(children) else None


def _map_entry(kind):
    """Whether a type is what LLDB lists as a map's entry: `std::pair<const K, V>` (GCC's
    debug information spells it `std::pair<K const, V>`), or libc++'s
    `std::__value_type<K, V>` in older LLDBs."""
    kind = _unqualified(kind)
    if _template_arguments(kind, "__value_type") is not None:
        return True
    arguments = _template_arguments(kind, "pair")
    return bool(arguments) and bool(re.search(r"^const\b|\bconst$", arguments[0]))


def _map_like(children):
    """Whether LLDB lists these children as it lists a C++ map's entries: [0], [1], ...,
    each a pair with a const key."""
    entries = [child for child in children if child["name"] not in _GROUPS]
    return bool(entries) and all(
        _ENTRY_NAME.fullmatch(child["name"]) and _map_entry(child.get("type")) for child in entries
    )


def _not_associative(variable, path, segment):
    pointee = _c_pointee(variable)
    if pointee is not None and _associative(_pointee_type(variable, pointee)):
        return (
            f"{segment} not read: {path} ({variable.get('type')}) is a pointer; "
            f"{path}[0]{segment} is that entry of the {pointee} it points at"
        )
    return (
        f"{segment} not read: [#N] is the Nth entry of a C++ map or set, and {path} "
        f"({variable.get('type')}) is not one; index a sequence with [N]"
    )


def _listed_entries(worker, stop, variable, path, first_page):
    """The entries LLDB lists for a C++ map or set, in iteration order, 64 a page (at
    most 512)."""
    handle, start = variable["variablesReference"], 0
    while True:
        page = (
            first_page(handle)
            if start == 0
            else worker.dispatch("expand", {**stop, "handle": handle, "start": start})
        )
        for child in page["variables"]:
            if child["name"] in _GROUPS:
                continue
            if not _ENTRY_NAME.fullmatch(child["name"]):
                raise ValueError(
                    f"the debugger shows {path}'s own members ({child['name']}, ...), not "
                    "its entries, so no entry can be selected; select a member with `.`"
                )
            yield child
        if not page["truncated"]:
            return
        if start + 64 > 511 or page.get("remaining_nodes", 1) <= 0:
            raise Refused(
                "BUDGET_EXHAUSTED",
                f"{path} has more entries than the bounded inspection window (512 nodes) holds",
            )
        start += 64


def _entry_part(worker, stop, child, part, path):
    """`first` or `second` of one entry LLDB lists for a map (`first` alone is fetched
    first: one node, not two)."""
    if not child.get("variablesReference"):
        why = (
            "it is at the inspection depth limit (4 levels below a local)"
            if child.get("depth_limited")
            else f"the debugger shows it as {child.get('value')!r}"
        )
        raise ValueError(f"cannot read the keys of {path}'s entries: {why}")
    request = {**stop, "handle": child["variablesReference"], "start": 0}
    shown = worker.dispatch("expand", {**request, "count": 1})["variables"]
    if not any(field["name"] == part for field in shown):
        shown = worker.dispatch("expand", request)["variables"]
    found = next((field for field in shown if field["name"] == part), None)
    if found is None:
        names = ", ".join(field["name"] for field in shown[:8]) or "none"
        raise ValueError(
            f"cannot read the keys of {path}'s entries: the debugger shows no {part} in one "
            f"(it shows {names})"
        )
    return found


def _key_kind(first):
    """("integer", (low, high)) or ("string", None) for a key Peeky compares, else
    (the key's type, None)."""
    from peeky.native import INTEGER_RANGES

    kind = _unqualified(first.get("type"))
    if (span := INTEGER_RANGES.get(_GCC_INTEGERS.get(kind, kind))) is not None:
        return "integer", span
    if _STD_STRING.fullmatch(kind):
        return "string", None
    return kind or "unknown", None


def _entry(worker, stop, variable, path, segment, kind, first_page, whole=False):
    """What `segment` names in a C++ map or set (#119). LLDB lists the entries as
    [0], [1], ... in iteration order, where C++'s `m[k]` is the value for key k: so
    `[#N]` is the Nth entry, `[k]` the value for key k (a map with unique keys that
    Peeky compares exactly), and anything else is refused, never read by position.
    ``whole``: LLDB's summary of a std::string shows all of it (libc++)."""
    label = f"{path} ({variable.get('type')})"
    if segment.startswith("[#"):
        position = int(segment[2:-1])
        if (found := _entry_at(worker, stop, variable, position)[0]) is not None:
            return {**found, "name": segment}
        count = sum(1 for _ in _listed_entries(worker, stop, variable, path, first_page))
        span = f": {path}[#0] to {path}[#{count - 1}]" if count else ""
        raise ValueError(f"{segment} not found: {path} has {count} entries{span}")
    how = f"select an entry by position with {path}[#N] (`peeky session get {path}` lists them)"
    if kind.endswith("set"):
        raise ValueError(
            f"{segment} not read: {label} is a C++ set, which has no [] in C++, and the "
            f"debugger lists its elements in iteration order, not by index; "
            + how.replace("an entry", "an element")
        )
    if kind not in _KEYED:
        what = {
            "unresolved": "listed as a C++ map is (pairs with a const key), under a type name "
            "the build's debug information does not resolve, so it may be a map read by key "
            "or a sequence read by position",
            "unsettled": "of a type the build's debug information gives several classes, "
            "which may be a C++ map or set (read by key, or not at all) or a sequence read by "
            "position",
        }.get(kind, "a C++ multimap, which has no [] in C++ (a key can repeat)")
        raise ValueError(
            f"{segment} not read: {label} is {what}; {how} and read its .first and .second"
            if kind != "unsettled"
            else f"{segment} not read: {label} is {what}; {how}"
        )
    return _value_for_key(worker, stop, variable, path, segment, first_page, how, whole)


def _summary_size(variable):
    """The entry count LLDB's summary of a container states (`size=3`), or None."""
    found = re.search(r"(?:^|\s)size=([0-9]{1,9})$", str(variable.get("value") or ""))
    return int(found[1]) if found else None


def _entry_at(worker, stop, variable, position):
    """Entry ``position`` of a C++ map as LLDB lists it (one node), with whether more
    follow it; (None, False) past the last."""
    page = worker.dispatch(
        "expand", {**stop, "handle": variable["variablesReference"], "start": position, "count": 1}
    )
    child = next((c for c in page["variables"] if c["name"] == f"[{position}]"), None)
    return child, bool(page["truncated"])


def _template_arguments(kind, name):
    """The template arguments of the class ``name`` (`map`) a type names, split at its
    top-level commas, or None."""
    found = re.match(
        rf"(?:(?:const|volatile)\s+)*(?:::)?std::(?:__\w+::|pmr::)*{name}<", kind or ""
    )
    if not found:
        return None
    arguments, depth, current = [], 1, []
    for char in kind[found.end() :]:
        if char in "<([":
            depth += 1
        elif char in ">)]":
            depth -= 1
            if not depth:
                return [*arguments, "".join(current).strip()]
        elif char == "," and depth == 1:
            arguments.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    return None


def _order(variable):
    """1 when a std::map's type says its entries are in ascending key order (its
    comparator is std::less, the default LLDB leaves out), -1 for std::greater, else
    None (a comparator of the program's own)."""
    for kind in (variable.get("type"), variable.get("aliased_type")):
        arguments = _template_arguments(kind, "map")
        if arguments is None or len(arguments) < 2:
            continue
        if len(arguments) == 2:
            return 1
        compare = re.fullmatch(r"(?:::)?std::(?:__\w+::)*(less|greater)<\s*(.*?)\s*>", arguments[2])
        if compare and compare[2] in ("", "void", arguments[0]):
            return 1 if compare[1] == "less" else -1
        return None
    return None


def _comparison(variable):
    """None when C++'s `m[K]` finds only a key equal to K: a std::map ordered by
    std::less or std::greater, or a std::unordered_map using std::hash and std::equal_to
    (the defaults LLDB leaves out of a type's name). Else why not (`comparator (ByAbs) is
    not ...`): the map's own comparison may treat another key as equal to K, which `m[K]`
    then reads (review of #119)."""
    if _order(variable) is not None:
        return None
    for kind in (variable.get("type"), variable.get("aliased_type")):
        arguments = _template_arguments(kind, "unordered_map")
        if arguments is None or len(arguments) < 2:
            continue
        key = re.escape(arguments[0])
        hashing = len(arguments) < 3 or re.fullmatch(
            rf"(?:::)?std::(?:__\w+::)*hash<\s*{key}\s*>", arguments[2]
        )
        equal = len(arguments) < 4 or re.fullmatch(
            rf"(?:::)?std::(?:__\w+::)*equal_to<\s*(?:{key}|void)?\s*>", arguments[3]
        )
        if hashing and equal:
            return None
        return f"hash or equality ({', '.join(arguments[2:4])}) is not std::hash and std::equal_to"
    for kind in (variable.get("type"), variable.get("aliased_type")):
        arguments = _template_arguments(kind, "map")
        if arguments is not None and len(arguments) > 2:
            return f"comparator ({arguments[2]}) is not std::less or std::greater"
    return "comparator (or hash and equality) cannot be told from its type"


def _string_bytes(worker, stop, first, whole):
    """How much of a std::string key LLDB's summary shows: True for all of it (libc++,
    whose summary LLDB prints from the string's size, a NUL as `\\0`), else its length in
    bytes from libstdc++'s `_M_string_length` (that summary is a C string, which ends at
    the first NUL), or None where neither can be read."""
    if whole or first.get("view") == "memory":
        # A libstdc++ string the worker read from memory is shown whole too (#128).
        return True
    if type(first.get("string_length")) is int:
        # One whose summary the worker checked against its `_M_string_length` (#128).
        return first["string_length"]
    if not first.get("variablesReference"):
        return None
    shown = worker.dispatch("expand", {**stop, "handle": first["variablesReference"], "start": 0})[
        "variables"
    ]
    length = next((field for field in shown if field["name"] == "_M_string_length"), None)
    if length is not None:
        value = str(length.get("value"))
        return int(value) if re.fullmatch(r"[0-9]{1,12}", value) else None
    if shown and not any(field["name"].startswith("_M_") for field in shown):
        # No libstdc++ member: LLDB's quoted summary came from its libc++ formatter.
        return True
    return None


_LISTED_KEYS = 20


def _value_for_key(worker, stop, variable, path, segment, first_page, how, whole=False):
    """The value for one key of a C++ map with unique keys: the `second` of the one
    entry whose `first` the debugger shows as exactly that key (a std::string key also
    of its length). A std::map with integer keys in a standard order is bisected; any
    other map is read entry by entry. That no entry has the key is said only where the
    map's comparison is the standard one, under which C++'s `m[K]` finds no other."""
    key = _key(segment)
    wanted = _literal(key) if key is not None else int(segment[1:-1])
    shown = f'"{wanted}"' if key is not None else str(wanted)
    written = f"[{shown}]" if key is None or _PLAIN_KEY.fullmatch(wanted) else segment
    if key is not None and not _PLAIN_KEY.fullmatch(wanted):
        raise ValueError(
            f"{written} not read: Peeky compares a string key only when it is printable ASCII "
            f'without " or \\ (as the debugger shows such keys); {how}'
        )
    found = _entry_at(worker, stop, variable, 0)[0]
    size = _summary_size(variable)
    if found is None:
        # Nothing at [0]: an empty map, or one LLDB shows by its own members (refused).
        count = sum(1 for _ in _listed_entries(worker, stop, variable, path, first_page))
        if count or size:
            raise ValueError(
                f"{written} not read: the debugger lists {count} of {path}'s {size} entries; {how}"
            )
        raise ValueError(
            f"{written} not found: {path} is empty; in C++, {path}{written} would insert the "
            "key with a default value"
        )
    first = _entry_part(worker, stop, found, "first", path)
    compared, span = _key_kind(first)
    named = _unqualified(first.get("type")) or "unknown"
    if compared not in ("integer", "string"):
        raise ValueError(
            f"{written} not read: {path}'s keys are {compared}, which Peeky does not compare "
            "(it reads a map by key only for integer and std::string keys, compared as the "
            f"debugger shows them); {how} and read its .first and .second"
        )
    if compared == "string" and key is None:
        raise ValueError(
            f'{written} not read: {path}\'s keys are strings; select one as {path}["key"], or {how}'
        )
    if compared == "integer" and key is not None:
        raise ValueError(
            f"{written} not read: {path}'s keys are {named}; select one as {path}[N], or {how}"
        )
    if compared == "integer" and not span[0] <= wanted <= span[1]:
        raise ValueError(
            f"{written} not read: {wanted} is outside the range of {path}'s key type "
            f"({named}), so C++ would convert it to another key"
        )

    def key_of(child, shown_first=None):
        """(the key as compared, as shown, its `first`)."""
        part = shown_first or _entry_part(worker, stop, child, "first", path)
        value = part.get("value")
        if (
            compared == "integer"
            and isinstance(value, str)
            and re.fullmatch(r"-?[0-9]{1,20}", value)
        ):
            return int(value), value, part
        if (
            compared == "string"
            and isinstance(value, str)
            and (_STRING_SUMMARY.fullmatch(value) or _NOTED_STRING.fullmatch(value))
        ):
            return value, value, part
        raise ValueError(
            f"{written} not read: the debugger shows the key of {path}[#{child['name'][1:-1]}] "
            f"as {value!r}, which Peeky cannot compare; {how}"
        )

    def value_of(child):
        return {**_entry_part(worker, stop, child, "second", path), "name": segment, "_map": path}

    target = wanted if compared == "integer" else f'"{wanted}"'
    order = _order(variable)
    if compared == "integer" and order and size and size > _LISTED_KEYS:
        # Sorted by key: a bisection reads about log2(size) entries, not all of them. A
        # smaller map is read whole, so that a missing key's error lists the keys.
        last, more = _entry_at(worker, stop, variable, size - 1)
        if last is not None and not more:
            low, high, known = 0, size - 1, {0: (found, first)}
            while low <= high:
                middle = (low + high) // 2
                child, shown_first = known.get(middle) or (
                    _entry_at(worker, stop, variable, middle)[0],
                    None,
                )
                if child is None:
                    break
                number = key_of(child, shown_first)[0]
                if number == target:
                    return value_of(child)
                if (number < target) == (order > 0):
                    low = middle + 1
                else:
                    high = middle - 1
            else:
                ordering = "ascending (std::less)" if order > 0 else "descending (std::greater)"
                raise ValueError(
                    f"{written} not found: {path} has no key {shown} (its {size} keys are in "
                    f"{ordering} order); in C++, {path}{written} would insert it with a "
                    "default value"
                )
    keys, remaining, checked = [], None, 0
    for child in _listed_entries(worker, stop, variable, path, first_page):
        if remaining is None:
            page = first_page(variable["variablesReference"])
            remaining = page.get("remaining_nodes")
            listed = len(page["variables"])
            if size is not None and type(remaining) is int and 2 * size - listed + 4 > remaining:
                # Each entry is a node, and so is its key: refused before any is read.
                raise Refused(
                    "BUDGET_EXHAUSTED",
                    f"{written} not read: comparing the keys of {path}'s {size} entries reads "
                    f"{2 * size} nodes, beyond the bounded inspection window (512 nodes); {how}",
                )
        number, value, part = key_of(child, first if checked == 0 else None)
        checked += 1
        if number == target and compared == "string":
            # libstdc++'s summary ends at a NUL: "a\0b" shows as "a" (review of #119).
            length = _string_bytes(worker, stop, part, whole)
            if length is None:
                raise ValueError(
                    f"{written} not read: the key of {path}[#{child['name'][1:-1]}] shows as "
                    f"{value}, but the debugger shows this build's std::string only up to its "
                    "first NUL byte, and Peeky cannot read this key's length (at the "
                    f"inspection depth limit, or not shown); {how}"
                )
            if length is not True and length != len(wanted):
                keys.append(f"{value} ({length} bytes)")
                continue
        if number == target:
            return value_of(child)
        keys.append(value)
    if size is not None and size != len(keys):
        raise ValueError(
            f"{written} not read: the debugger lists {len(keys)} of {path}'s {size} entries, "
            f"none with key {shown}; {how}"
        )
    listed = ", ".join(keys[:_LISTED_KEYS]) + (
        f", ... ({len(keys)} total)" if len(keys) > _LISTED_KEYS else ""
    )
    if compared == "string" and not whole:
        listed += "; each as the debugger shows it, up to a NUL byte"
    if (why := _comparison(variable)) is not None:
        raise ValueError(
            f"{written} not read: no key equals {shown} exactly (keys of {path}: {listed}), "
            f"but {path}'s {why}, so C++'s {path}{written} may read an entry whose key it "
            f"treats as equal to {shown}, which Peeky cannot tell from the keys; {how}"
        )
    raise ValueError(
        f"{written} not found: {path} has no key {shown} (keys: {listed}); in C++, "
        f"{path}{written} would insert it with a default value"
    )


_ENTRY_NOTES = {
    "keyed": (
        "{0} is a C++ map: {0}[K] is the value for key K, as in C++ (integer and "
        "std::string keys), and {0}[#N] its Nth entry in iteration order, a pair with "
        ".first and .second"
    ),
    "uncompared": (
        "{0} is a C++ map whose keys Peeky does not compare (it reads by key only integer and "
        "std::string keys): {0}[#N] is its Nth entry in iteration order, a pair with .first "
        "and .second, and {0}[K] is refused"
    ),
    "set": (
        "{0} is a C++ set, which has no [] in C++: {0}[#N] is its Nth element in iteration order"
    ),
    "multi": (
        "{0} is a C++ multimap, which has no [] in C++ (a key can repeat): {0}[#N] is its "
        "Nth entry in iteration order, a pair with .first and .second"
    ),
    "unresolved": (
        "{0} lists pairs with a const key, as a C++ map does, under a type name the build's "
        "debug information does not resolve: {0}[#N] is its Nth entry in iteration order, "
        "and [N] is refused, since it could be a key or a position"
    ),
    "unsettled": (
        "{0}'s type is one the build's debug information gives several classes, which may "
        "be a C++ map or set: {0}[#N] is its Nth entry in iteration order, and [N] is refused, "
        "since it could be a key, nothing, or a position"
    ),
}


def _keyed_child(worker, stop, base, child, whole):
    """The key selector (`[2]`, `["a"]`) that reads a map entry's value, with that value,
    when its key is one Peeky compares and a selector can write exactly; None when this
    entry's is not, and False when the map's key type is not one Peeky compares.
    ``whole``: LLDB's summary of a std::string shows all of it (libc++)."""
    if not child.get("variablesReference"):
        return None
    shown = worker.dispatch("expand", {**stop, "handle": child["variablesReference"], "start": 0})[
        "variables"
    ]
    first = next((field for field in shown if field["name"] == "first"), None)
    second = next((field for field in shown if field["name"] == "second"), None)
    if first is None or second is None:
        return None
    compared, _ = _key_kind(first)
    value = str(first.get("value"))
    if compared not in ("integer", "string"):
        return False
    # The selector grammar's own bounds: six digits, a quoted key of plain characters.
    if compared == "integer" and re.fullmatch(r"-?[0-9]{1,6}", value):
        segment = f"[{int(value)}]"
    elif (
        compared == "string"
        and len(value) >= 2
        and value[0] == value[-1] == '"'
        and _PLAIN_KEY.fullmatch(value[1:-1])
        and _string_bytes(worker, stop, first, whole) in (True, len(value) - 2)
    ):
        segment = f'["{value[1:-1]}"]'
    else:
        return None
    entry = {"name": segment, "type": second.get("type"), "value": second.get("value")}
    if second.get("unavailable"):
        entry.update(unavailable=True, reason=second.get("reason"))
    entry["selector"] = base + segment
    if second.get("variablesReference"):
        entry["expandable"] = True
    return entry


def _positional_child(base, child):
    position = child["name"][1:-1]
    entry = {"name": f"[#{position}]", "type": child.get("type"), "value": child.get("value")}
    if child.get("unavailable"):
        entry.update(unavailable=True, reason=child.get("reason"))
    entry["selector"] = f"{base}[#{position}]"
    if child.get("variablesReference"):
        entry["expandable"] = True
    return entry


def _entry_children(worker, stop, selector, variable, page, whole=False):
    """A C++ map's or set's first page of entries (#119), each with the selector that
    reads it: `m[K]` (the value for key K) for a map whose keys Peeky compares, else
    `m[#N]` (the Nth entry in iteration order), never `m[N]`, which is key N in C++.
    None for any other value. ``whole``: LLDB's summary of a std::string shows all of
    it (libc++)."""
    base, container, kind = selector, variable, _associative(variable)
    pointee = _c_pointee(variable)
    if kind is None and pointee is not None and _pointee(variable, page["variables"]) is None:
        # A pointer lists what it points at (#102): its entries are p[0][...].
        base, container = f"{selector}[0]", _pointee_type(variable, pointee)
        kind = _associative(container)
    if kind is None:
        kind = _undecided(container, page["variables"])
    entries = [child for child in page["variables"] if child["name"] not in _GROUPS]
    if kind is None or not all(_ENTRY_NAME.fullmatch(child["name"]) for child in entries):
        # Not a map or set, or one LLDB shows by its own members: listed as they are.
        return None
    listed, keys = [], kind in _KEYED
    for child in entries:
        keyed = _keyed_child(worker, stop, base, child, whole) if keys else None
        if keyed is False:
            keys, keyed = False, None
        listed.append(keyed or _positional_child(base, child))
    # One label, one entry: two keys a selector cannot tell apart are listed by position.
    labels = [entry["selector"] for entry in listed]
    for at, (entry, child) in enumerate(zip(listed, entries)):
        if labels.count(entry["selector"]) > 1:
            listed[at] = _positional_child(base, child)
    for entry, child in zip(listed, entries):
        if isinstance(child.get("note"), str) and not entry.get("unavailable"):
            # Read through a local shown with a note (#148).
            entry["note"] = child["note"]
    for entry in listed:
        try:
            parse_selector(entry["selector"])
        except ValueError:
            # Past the selector grammar's bounds (16 steps, 512 characters).
            del entry["selector"]
    note = (
        ("keyed" if keys else "uncompared")
        if kind in _KEYED
        else "set"
        if kind.endswith("set")
        else kind
        if kind in ("unresolved", "unsettled")
        else "multi"
    )
    shown = _ENTRY_NOTES[note].format(base)
    if note == "keyed" and (why := _comparison(container)) is not None:
        shown += f"; its {why}, so only a key equal to K reads"
    return {
        "children": listed,
        **({"children_truncated": True} if page["truncated"] else {}),
        "note": shown,
    }


def _child_selector(parent, name):
    """The selector that reads child `name` of `parent`, or None if no path names it."""
    if _KEY_NAME.fullmatch(name):
        try:
            key = _literal(name)
        except ValueError:
            return None
        return f"{parent}[{key!r}]" if isinstance(key, str) else None
    if match := _INDEX.fullmatch(name):
        return f"{parent}[{int(match[1])}]"
    if _NAME.fullmatch(name):
        return f"{parent}.{name}"
    return None


def _list_children(worker, stop, selector, backing, size):
    """An ArrayList's first elements (`workers[0]`, ...), not its internal fields."""
    elements = (
        worker.dispatch(
            "expand",
            {**stop, "handle": backing["variablesReference"], "start": 0, "count": min(size, 64)},
        )["variables"]
        if size
        else []
    )
    listed = []
    for child in elements:
        match = re.fullmatch(r"\[?([0-9]+)\]?", str(child.get("name")))
        if not match or int(match[1]) >= size:
            continue
        entry = {
            "name": f"[{int(match[1])}]",
            "type": child.get("type"),
            "value": child.get("value"),
            "selector": f"{selector}[{int(match[1])}]",
        }
        if child.get("variablesReference"):
            entry["expandable"] = True
        listed.append(entry)
    return {"children": listed, **({"children_truncated": True} if size > len(listed) else {})}


def _children(worker, stop, selector, variable, pointers=False, whole_strings=False):
    """One page (64) of a value's children, for a person or agent deciding what to read.
    ``whole_strings``: LLDB's summary of a std::string shows all of it (libc++)."""
    page = worker.dispatch(
        "expand",
        {**stop, "handle": variable["variablesReference"], "start": 0, "previews": True},
    )
    if listed := java_list(variable, page["variables"]):
        try:
            return _list_children(worker, stop, selector, *listed)
        except ValueError as exc:
            if "exceeds bounded expansion" not in str(exc):
                raise
            # A backing array over 512 slots: its fields (size, elementData) instead.
    if pointers and (
        entries := _entry_children(worker, stop, selector, variable, page, whole_strings)
    ):
        return entries
    listed, grouped = [], []
    # A container's groups hold its type's methods and dunders: named only for objects.
    container = any(child["name"] == "len()" for child in page["variables"])
    for child in page["variables"]:
        if child["name"] in _GROUPS:
            if not child.get("type") and not container:
                grouped.append(child["name"])
            continue
        entry = {"name": child["name"], "type": child.get("type"), "value": child.get("value")}
        if child.get("unavailable"):
            entry.update(unavailable=True, reason=child.get("reason"))
        if child.get("identity"):
            entry["identity"] = child["identity"]
        if child.get("view"):
            entry["view"] = child["view"]
        if child.get("stale"):
            entry["stale"] = True
        if isinstance(child.get("note"), str) and not child.get("unavailable"):
            # Read through a local shown with a note (#148).
            entry["note"] = child["note"]
        if type(child.get("settable")) is bool:
            entry["settable"] = child["settable"]
        if isinstance(child.get("not_settable"), str):
            entry["not_settable"] = child["not_settable"]
        path = _child_selector(selector, child["name"])
        if pointers and path is None and _pointee(variable, page["variables"]) is child:
            path = f"{selector}[0]"
        elif (
            pointers
            and path is not None
            and _INDEX.fullmatch(child["name"])
            and _c_pointee(variable) is None
            and _unresolved_indirection(variable)
            and _pointee(variable, page["variables"]) is None
        ):
            # Neither p[N] (a pointer's) nor r[N] (a reference's) can be told (#102).
            path = None
        elif (
            pointers
            and path is not None
            and _INDEX.fullmatch(child["name"])
            and _c_pointee(variable) is not None
            and _pointee(variable, page["variables"]) is None
        ):
            # A pointer to an array or container lists its pointee's elements: in C,
            # element N is p[0][N] (#102).
            path = _child_selector(f"{selector}[0]", child["name"])
        if path and (
            not path.endswith("]") or _genuine(child, page["variables"], path[-2] in "'\"")
        ):
            entry["selector"] = path
        if child.get("variablesReference"):
            entry["expandable"] = True
        listed.append(entry)
    return {
        "children": listed,
        **({"children_truncated": True} if page["truncated"] else {}),
        # Members debugpy files under its groups, selected by name (`f.__defaults__`).
        **({"children_grouped": grouped} if grouped else {}),
    }


# A probe that completed, but whose requested change (--set, --set-at) never came: its
# values are evidence, but the experiment asked for did not happen (#137).
UNAPPLIED_EXIT = 4


def probe_exit_code(result):
    """`peeky probe`'s and `peeky-probe`'s exit code: 0, 1 for a probe that did not
    complete, or UNAPPLIED_EXIT."""
    if result.get("status") != "completed":
        return 1
    return UNAPPLIED_EXIT if result.get("unapplied") else 0


_HASHED = re.compile(r"::(h[0-9a-f]{16})$")
# What Python's debugger names alike, by the node making the code: two lambdas on a line
# are both `<lambda>`, and its frames give no column (debugpy reports 1).
_PYTHON_ALIKE = {
    ast.Lambda: "<lambda>",
    ast.GeneratorExp: "<genexpr>",
    ast.ListComp: "<listcomp>",
    ast.SetComp: "<setcomp>",
    ast.DictComp: "<dictcomp>",
}


def _function_labels(names, instances):
    """{frame name: label} for warnings and changes: a frame by the function its build
    names (`instances`: a generic's instance, a Rust closure's number) where known, else
    as `display_function` shows it; where two different frames would read alike (an
    instance cut short, overloads), the label with the frame's symbol hash, else the
    frame's whole name."""
    labels = {name: instances.get(name) or display_function(name) for name in names}
    shown = list(labels.values())
    distinct = {}
    for name, label in labels.items():
        if shown.count(label) > 1:
            hashed = _HASHED.search(str(name))
            label = f"{label} ({hashed[1]})" if hashed and instances.get(name) else str(name)
        distinct[name] = label
    return distinct


def _python_alike(text, lines):
    """{line: {code name: how many}} of the code on ``lines`` of a Python source that
    Python's debugger names alike (two lambdas on one line), whose stops it cannot tell
    apart; {} where none, or the source does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    found = {}
    for node in ast.walk(tree):
        name = _PYTHON_ALIKE.get(type(node))
        if name is None:
            continue
        for number in set(range(node.lineno, (node.end_lineno or node.lineno) + 1)) & set(lines):
            counts = found.setdefault(number, {})
            counts[name] = counts.get(name, 0) + 1
    return {
        number: {name: count for name, count in counts.items() if count > 1}
        for number, counts in found.items()
        if any(count > 1 for count in counts.values())
    }


def _stops_by_function(functions, labels, word, alike=None):
    """`main (stops 1, 3); tf<rc::A> (stop 2)` for {frame name: [stops]}, each frame by
    its label (`_function_labels`); `alike`: {code name: how many} the line has that the
    debugger names alike, whose stops are counted together."""
    shown = []
    for name, numbers in list(functions.items())[:8]:
        many = (alike or {}).get(name)
        shown.append(
            f"{labels.get(name) or '?'} ({word}{'s' if len(numbers) > 1 else ''} "
            f"{', '.join(map(str, numbers[:8]))}{', ...' if len(numbers) > 8 else ''}"
            + (
                f"; {many} on the line, which the debugger names alike, so their stops are "
                "counted together"
                if many
                else ""
            )
            + ")"
        )
    if len(functions) > 8:
        shown.append(f"and {len(functions) - 8} more")
    return "; ".join(shown)


def _moved(placed, state):
    """Apply this wait's `breakpoint` events to the placed breakpoints with their ids."""
    for update in state.get("breakpoint_events", []):
        point = update.get("breakpoint") or {}
        if point.get("id") is None:
            continue
        for number, known in placed.items():
            if known.get("id") == point["id"]:
                placed[number] = {**known, **point}


def _set_at_moved(points, placed, program):
    """Refuse a --set-at whose line the debugger moved (no code of its own there): a
    change is made where it was asked, never at a line nearby."""
    for point in points:
        number = point["build_line"]
        where = (placed.get(number) or {}).get("line", number)
        if point["change"] is None and type(where) is int and where != number:
            raise Refused(
                "INVALID_REQUEST",
                f"--set-at {program}:{point['line']}@{point['hit']}: line {number} has no "
                f"code of its own (the debugger moved its breakpoint to line {where}); a "
                "change is made only where it is asked. Choose a line from `peeky describe`",
            )


def _expected_line(breakpoint, state, requested):
    """The adapter-resolved breakpoint line, including later breakpoint updates."""
    line = breakpoint.get("line", requested)
    for update in state.get("breakpoint_events", []):
        point = update.get("breakpoint", {})
        if point.get("id") == breakpoint.get("id") and type(point.get("line")) is int:
            line = point["line"]
    return line


def run_probe(root, probe, *, adapter=None, progress=None):
    """Run one self-contained probe; the worker is always closed before returning.

    ``progress(message)`` receives human-readable milestones (never parsed).
    """
    say = progress or (lambda message: None)
    worker = DebugWorker(root, adapter=adapter)
    started = time.monotonic()
    deadline = started + probe.timeout
    result = {
        "probe": {
            **asdict(probe),
            "set": [list(change) for change in probe.set],
            "set_at": [list(point) for point in probe.set_at],
        }
    }
    # The edit's texts are not evidence of the run; the CLI adds the diff.
    result["probe"].pop("edit")
    rows, changes, hits, warnings, relocated, pauses = [], [], 0, [], False, []
    # Pauses at stops that only apply a --set-at (no capture there).
    set_pauses = []
    unreported = None
    # The breakpoint line in the build that runs: the candidate's numbering with an edit.
    line = probe.line
    # Each --set-at, with its line in the build that runs (#137).
    points = [
        {"line": at, "build_line": at, "hit": hit, "name": name, "value": value, "change": None}
        for at, hit, name, value in probe.set_at
    ]
    # Stops seen per breakpoint line (the build's numbering): a --set-at's hit counts its
    # own line's stops, in any function.
    seen = {}
    # {--set-at line: {function: [its stops]}}: a lambda, closure or recursive call on
    # the line stops there too, and its stops count. Keyed, like `functions`, by the
    # frame's whole name: a Rust generic's instances differ only in its symbol's hash.
    set_functions = {}
    # {frame name: a generic's instance with its type arguments} (Rust: `tf<rc::A>`).
    instances = {}
    # {function: [hits]}: a line shared by a function and a closure or lambda stops in each.
    functions = {}
    last_said = 0.0
    session = None
    try:
        if probe.edit:
            text = worker.path(probe.program).read_text()
            line = candidate_line(text, probe.edit, probe.line)
            for point in points:
                point["build_line"] = candidate_line(text, probe.edit, point["line"])
            # The worker builds one edit with the same effect as every pair.
            old, new = combine_edits(text, probe.edit)
            result["candidate"] = {"line": line, "original_line": probe.line}
        # The capture line first, then each --set-at line, each once.
        lines = list(dict.fromkeys([line, *(point["build_line"] for point in points)]))
        launch = worker.dispatch(
            "launch",
            {
                "program": probe.program,
                "breakpoints": [{"line": number} for number in lines],
                **({"edit": {"old": old, "new": new}} if probe.edit else {}),
                **({"entry": probe.entry} if probe.entry else {}),
                **({"args": list(probe.args)} if probe.args else {}),
                **({"expect_sha256": dict(probe.expect_sha256)} if probe.expect_sha256 else {}),
                # What no stop of the build can change is refused before it runs (#138).
                # Each with the line whose stops make it: a --set-at's own (#137).
                **(
                    {
                        "sets": [_launch_set(line, name, value) for name, value in probe.set]
                        + [_launch_set(p["build_line"], p["name"], p["value"]) for p in points]
                    }
                    if probe.set or points
                    else {}
                ),
            },
        )
        session = {"session_id": launch["session_id"]}
        # Where the debugger placed each line's breakpoint (later `breakpoint` events
        # update it): {build line: DAP breakpoint}.
        placed = dict(zip(lines, [*(launch["breakpoints"] or []), *([{}] * len(lines))]))
        active = list(lines)
        result.update(
            breakpoint=(launch["breakpoints"] or [{}])[0],
            artifact_manifest_id=launch["artifact_manifest_id"],
            target_identity=launch["target_identity"],
            source_sha256=launch["source_sha256"],
        )
        # A launched JVM's captured objects show their fields; an attached one (often a
        # live service) does not, since every read lengthens its pause.
        identity = launch["target_identity"] if isinstance(launch["target_identity"], dict) else {}
        previews = identity.get("language") == "java" and identity.get("mode") != "attach"
        pointers = identity.get("language") in ("c", "cpp")
        if not launch.get("supports_member_set") and any(
            len(parse_selector(name)) > 1
            for name, _ in (*probe.set, *((p["name"], p["value"]) for p in points))
        ):
            raise ValueError(
                "--set changes only the stopped function's own locals here; "
                + (
                    "this Java adapter bundle predates field and element changes (rebuild it "
                    "with scripts/setup_java_adapter.py)"
                    if identity.get("language") == "java"
                    else "fields and array elements can be changed only in Java, Rust, C and C++"
                )
            )
        if probe.edit:
            result["candidate"]["sha256"] = launch.get("candidate_sha256")
        if "program_args" in launch:
            result["program_args"] = launch["program_args"]
        for note in launch.get("warnings", []):
            warnings.append(note)
            say(note)
        if launch.get("entry_sha256"):
            result["entry_sha256"] = launch["entry_sha256"]
        point = result["breakpoint"]
        verified = point.get("verified") is True
        say(
            f"started; breakpoint {probe.program}:{point.get('line', line)} "
            + ("in the candidate " if probe.edit else "")
            + ("verified" if verified else "pending until its class or module loads")
            + (
                f", and {len(lines) - 1} more for --set-at"
                if len(lines) > 1
                else " (with --set-at)"
                if points
                else ""
            )
        )
        _set_at_moved(points, placed, probe.program)
        say("waiting for the program to reach it...")
        status, noted = "timeout", time.monotonic()
        while (remaining := deadline - time.monotonic()) > 0:
            state = worker.dispatch("wait", {**session, "timeout": min(2, remaining)})
            if not verified and any(
                update.get("breakpoint", {}).get("verified") is True
                and update.get("breakpoint", {}).get("id") == point.get("id")
                for update in state.get("breakpoint_events", [])
            ):
                verified = True
                say("breakpoint verified")
            _moved(placed, state)
            _set_at_moved(points, placed, probe.program)
            if state["state"] == "running":
                if time.monotonic() - noted >= 10:
                    noted = time.monotonic()
                    pending = hits < probe.limit or any(p["change"] is None for p in points)
                    phase = "for the next stop" if pending else "for the program to exit"
                    say(f"still waiting {phase} ({int(noted - started)} s elapsed)")
                continue
            if state["state"] == "terminated":
                if state.get("exit_inferred"):
                    warnings.append(
                        "The debugger never reported the program's exit; Peeky inferred it "
                        "because no threads were left (java-debug, issue #27)."
                    )
                # A thread was seen paused at the breakpoint, but the debugger never
                # reported that stop (#27): the line did run.
                unreported = state.get("unreported_stop")
                status = "completed" if hits else "failed" if unreported else "no-stop"
                say(f"program finished after {hits} hit(s)")
                break
            # From the moment the stop arrived, so debugger reads before capture count too.
            paused = state.get("event", {}).get("received", time.monotonic())
            stop = {**session, "stop_id": state["stop_id"]}
            # Only the requested breakpoints count; a crash, signal or other stop fails
            # closed before any capture or intervention.
            # Every line placed counts: another thread's stop at a line whose breakpoint
            # was just removed may still be served (LLDB stops all threads together).
            expected = {number: _expected_line(placed[number], state, number) for number in lines}
            roots = _roots([*probe.capture, *(p["name"] for p in points if p["change"] is None)])
            # Previews (#136) of the captured roots only: each costs debugger requests. A
            # stop the wait places at another line (a --set-at's) reads none.
            where = str(state.get("location") or "").rpartition(":")[2]
            capturing = hits < probe.limit and (not where.isdigit() or int(where) == expected[line])
            snapshot = worker.dispatch(
                "inspect",
                {
                    **stop,
                    **({"names": roots} if roots else {}),
                    "previews": _roots(probe.capture) if capturing else False,
                },
            )
            frame = snapshot["frames"][0] if snapshot["frames"] else {}
            reason = state.get("event", {}).get("body", {}).get("reason")
            path = str(frame.get("source", {}).get("path", "")).replace("\\", "/")
            matched = [number for number in lines if expected[number] == frame.get("line")]
            if (
                reason not in (None, "breakpoint")
                or not matched
                or not (path == probe.program or path.endswith("/" + probe.program))
            ):
                wanted = ", ".join(str(expected[number]) for number in lines)
                raise ValueError(
                    f"Stopped at {path}:{frame.get('line')} ({reason}), not the requested "
                    f"{probe.program}:{wanted}"
                )
            if snapshot.get("instance"):
                instances[frame.get("name")] = snapshot["instance"]
            for number in matched:
                seen[number] = seen.get(number, 0) + 1
                if any(p["build_line"] == number for p in points):
                    named = set_functions.setdefault(number, {})
                    named.setdefault(frame.get("name"), []).append(seen[number])
            # A stop of the capture line is captured until --hits are; later ones (kept
            # for a --set-at on that line) only apply it.
            captured = line in matched and hits < probe.limit
            if captured:
                hits += 1
                functions.setdefault(frame.get("name"), []).append(hits)
                if expected[line] != line and not relocated:
                    relocated = True
                    warnings.append(
                        f"The debugger moved the breakpoint from line {line} to line "
                        f"{expected[line]} (line {line} has no code of its own); values are "
                        f"from line {expected[line]}"
                        + (" of the candidate" if probe.edit else "")
                        + ". Choose a line from `peeky describe`."
                    )
                    say(warnings[-1])
                if _report_hit(hits, probe.limit, last_said):
                    last_said = time.monotonic()
                    of = (
                        f" (all, up to {probe.limit})" if probe.hits == "all" else f"/{probe.limit}"
                    )
                    say(f"hit {hits}{of} at {probe.program}:{expected[line]}; capturing")
                # Which of the line's breakpoint locations in this function the stop is at,
                # where it has several (#157): each pass stops at each.
                place = (
                    {"stop": state["place"], "stops": state["places"]}
                    if type(state.get("place")) is int and type(state.get("places")) is int
                    else {}
                )
                first = len(rows)
                # The stop-validation inspection is reused for the first selectors.
                rows.extend(
                    _capture(
                        worker,
                        stop,
                        probe.capture,
                        hits,
                        "observed",
                        snapshot,
                        preview=previews,
                        pointers=pointers,
                    )
                )
            # This stop's interventions, in order: --set at its --set-hit, then each
            # --set-at whose line stopped its hit-th time here.
            due = []
            if captured and hits == probe.set_hit and probe.set:
                due += [(name, value, hits, None) for name, value in probe.set]
            for point in points:
                number = point["build_line"]
                if point["change"] is None and number in matched and seen[number] == point["hit"]:
                    due.append((point["name"], point["value"], point["hit"], point))
            for name, value, hit, point in due:
                if point is not None:
                    say(
                        f"--set-at {probe.program}:{point['line']}@{hit}: setting {name}"
                        + (
                            f" (line {point['build_line']} of the candidate)"
                            if point["build_line"] != point["line"]
                            else ""
                        )
                    )
                try:
                    applied = apply_set(worker, stop, name, value)
                except Refused as exc:
                    if point is None:
                        raise
                    # One refusal fails the probe, as a --set's does: a linked change
                    # (shorten, then restore) half applied is not the experiment asked.
                    raise Refused(
                        exc.code,
                        f"--set-at {probe.program}:{point['line']}@{hit} {name}={value} was "
                        "refused at that stop (in "
                        + (snapshot.get("instance") or display_function(frame.get("name")) or "?")
                        + ")"
                        f": {exc}"
                        + (
                            f" ({len(changes)} earlier intervention(s) applied; the run is "
                            "not the experiment asked for)"
                            if changes
                            else ""
                        ),
                    ) from None
                change = {
                    **applied,
                    "line": frame.get("line"),
                    "hit": hit,
                    # The capture line's hits so far (this one, at a captured stop): where
                    # the change falls among the captured values.
                    "capture_hit": hits,
                    # Which function's variable: the line's stops may be in several.
                    "function": frame.get("name"),
                    **({"instance": snapshot["instance"]} if snapshot.get("instance") else {}),
                    # Which of the line's breakpoint locations there (#157): @HIT and
                    # --set-hit count each.
                    **(
                        {"stop": state["place"], "stops": state["places"]}
                        if type(state.get("place")) is int and type(state.get("places")) is int
                        else {}
                    ),
                }
                if point is not None:
                    change["at"] = f"{point['line']}@{hit}"
                    point["change"] = change
                changes.append(change)
            if captured and due:
                rows.extend(
                    _capture(
                        worker,
                        stop,
                        probe.capture,
                        hits,
                        "after-set",
                        preview=previews,
                        pointers=pointers,
                    )
                )
            if captured:
                for row in rows[first:]:
                    row.update(place)
            if captured and snapshot.get("instance"):
                # The function as the build names it (`tf<rc::A>`, `main::{closure#1}`),
                # for the table: the frame's own name keeps the symbol hash.
                for row in rows:
                    if row["hit"] == hits and row.get("function") == frame.get("name"):
                        row.setdefault("instance", snapshot["instance"])
            if len(rows) > MAX_ROWS:
                raise ValueError("Probe row budget exceeded")
            # Keep only the breakpoints a later stop still needs.
            needed = [line] if hits < probe.limit else []
            needed += [p["build_line"] for p in points if p["change"] is None]
            needed = [number for number in lines if number in needed]
            if needed != active:
                answer = worker.dispatch(
                    "breakpoints",
                    {
                        **session,
                        "program": probe.program,
                        "breakpoints": [{"line": number} for number in needed],
                    },
                )
                for number, reply in zip(needed, (answer or {}).get("breakpoints") or []):
                    if isinstance(reply, dict):
                        placed[number] = reply
                active = needed
            worker.dispatch("continue", stop)
            (pauses if captured else set_pauses).append(
                round((time.monotonic() - paused) * 1000, 1)
            )
            if not active:
                if probe.until == "hits":
                    status = "completed"
                    say(
                        "all requested hits captured"
                        + (" and --set-at applied" if points else "")
                        + "; ending the probe now"
                    )
                    break
                say(
                    f"reached the {probe.limit}-hit cap; letting the program finish"
                    if probe.hits == "all"
                    else "all requested hits captured"
                    + (" and --set-at applied" if points else "")
                    + "; letting the program finish"
                )
            elif captured and hits >= probe.limit:
                say("all requested hits captured; waiting for the --set-at stops")
        report = worker.dispatch("report", session)
        result.update(
            status=status,
            ended=(
                "hits"
                if probe.until == "hits" and status == "completed" and hits >= probe.limit
                else "exit"
                if report.get("state") == "terminated"
                else None
            ),
            output=report["output"],
            output_truncated=report["output_truncated"],
            # False when the probe ended before the program did (--until hits).
            output_complete=report.get("state") == "terminated",
        )
        if type(report.get("exit_code")) is int:
            result["exit_code"] = report["exit_code"]
        if unreported:
            seen = (
                f"thread {unreported.get('thread_name')!r} was seen paused at "
                f"{unreported.get('breakpoint')} (in {unreported.get('function')} at "
                f"{unreported.get('location')}), a stop the debugger never reported "
                "(java-debug, issue #27)"
            )
            if events := report.get("debugger_events"):
                result["debugger_events"] = events
            if hits:
                warnings.append(f"Before the program ended, {seen}; values from it were not read.")
            else:
                result["error"] = {
                    "code": "DEBUGGER_ERROR",
                    "message": f"The program ended after {seen}; no values were read",
                    "hint": "The line did run; the debugger failed to report its stop. Retry "
                    "the probe, and keep `debugger_events` for issue #27.",
                    "retryable": False,
                    "session_closed": True,
                }
        if status == "timeout":
            from peeky.errors import classify

            result["error"] = classify(
                TimeoutError(f"Probe exceeded its {probe.timeout} s timeout"), session_closed=True
            )
            if events := report.get("debugger_events"):
                # What the debugger last said: a stop it never reported shows here (#27).
                result["debugger_events"] = events
        if status == "no-stop":
            code = result.get("exit_code")
            where = f"{probe.program}:{line}" + (
                f" of the candidate (line {probe.line} before the edit)" if probe.edit else ""
            )
            if code == 0:
                message = (
                    f"The program finished without stopping at {where}; check `peeky "
                    "describe` for executable lines"
                )
                hint = "Pick a line the program executes; see `peeky describe`."
            elif code is not None:
                message = (
                    f"The program failed (exit code {code}) without stopping at {where}; "
                    "its output says why"
                )
                hint = (
                    "The program failed before it reached the line: read its output, and "
                    "check its arguments (everything after `--`) and input."
                )
            else:
                # java-debug reports no exit code: a crash and a normal exit look alike.
                message = f"The program ended without stopping at {where} (exit code unknown)"
                hint = (
                    "If the output shows an error, the program failed before the line: check "
                    "its arguments and input. Otherwise pick a line the program executes; "
                    "see `peeky describe`."
                )
            result["error"] = {
                "code": "NO_STOP",
                "message": message,
                "hint": hint,
                "retryable": False,
                "session_closed": True,
            }
        elif status == "completed" and probe.hits == "all" and hits >= probe.limit:
            warnings.append(
                f"Captured the first {probe.limit} hits, the cap for --hits all"
                + (
                    f" with {len(probe.capture)} selectors ({MAX_ROWS}-row budget)"
                    if probe.limit < MAX_HITS
                    else ""
                )
                + "; the program may have stopped here again later. To see later hits, probe "
                "a smaller input, fewer selectors, or a line that runs less often."
            )
        elif status == "completed" and probe.hits != "all" and hits < probe.hits:
            warnings.append(
                f"Only {hits} of {probe.hits} requested hits occurred before the program exited."
            )
    except Exception as exc:
        from peeky.errors import classify

        result.update(status="failed", error=classify(exc, session_closed=True))
        if getattr(exc, "before_launch", False):
            # Nothing ran (#138): no run to keep as evidence.
            result["refused_before_launch"] = True
        if session is not None:
            # What the program printed before the failure says how far it got.
            try:
                report = worker.dispatch("report", session)
            except Exception:
                report = {}
            if isinstance(report.get("output"), str) and report["output"]:
                result.update(
                    output=report["output"],
                    output_truncated=report.get("output_truncated", False),
                    output_complete=report.get("state") == "terminated",
                )
            failed_for_good = report.get("lost_stop") or report.get("adapter_failure")
            if failed_for_good and (events := report.get("debugger_events")):
                # The evidence for #27: the last DAP messages, the polls, the adapter's log.
                result["debugger_events"] = events
    finally:
        cleanup = worker.close()
    if cleanup and isinstance(result.get("error"), dict) and "cleanup" not in result["error"]:
        # What became of the program once the probe failed or timed out (#143).
        result["error"] = {**result["error"], "cleanup": cleanup}
    if level := optimization_level(result.get("target_identity")):
        result["optimized"] = level
        warnings.append(
            f"Optimized build ({level}): values that are shown can still be stale or wrong "
            "(the debugger cannot tell). Confirm with an unoptimized build before trusting them."
        )
    if any(row.get("unavailable") for row in rows):
        warnings.append(
            "Some locals have no value at this line (optimized out or not live)."
            if result.get("optimized")
            else "Some selectors could not be read by the debugger; see each row's reason."
        )
    labels = _function_labels(
        dict.fromkeys(
            [
                *functions,
                *(name for named in set_functions.values() for name in named),
                *(change.get("function") for change in changes),
            ]
        ),
        instances,
    )
    # Python code the debugger names alike on a line (two lambdas), which no frame tells
    # apart: said, never listed as one function.
    alike = {}
    if probe.program.endswith(".py") and (functions or set_functions):
        try:
            source = (Path(root) / probe.program).read_text()
            if probe.edit:
                old, new = combine_edits(source, probe.edit)
                source = source.replace(old, new, 1)
            alike = _python_alike(source, [line, *set_functions])
        except (OSError, ValueError):
            alike = {}
    for change in changes:
        number = next((p["build_line"] for p in points if p["change"] is change), line)
        if many := alike.get(number, {}).get(change.get("function")):
            change["alike"] = many
        if change.get("instance"):
            # As the warnings label it: with its hash where another instance reads alike
            # (a name cut short).
            change["instance"] = labels.get(change.get("function")) or change["instance"]
    # The --at line's own warning, unless it is a --set-at line too: one warning then.
    shared = line in set_functions and (
        len(set_functions[line]) > 1 or set(alike.get(line, ())) & set(set_functions[line])
    )
    if (len(functions) > 1 or set(alike.get(line, ())) & set(functions)) and not shared:
        warnings.append(
            f"Line {line} stopped in {len(functions)} function{'s' if len(functions) > 1 else ''}: "
            + _stops_by_function(functions, labels, "hit", alike.get(line))
            + ". --hits, --set-hit and --set-at's @HIT count every stop; the function column "
            "says which is which."
        )
    # A change's own caveat (a std::string's text, #164), once for the names it covers.
    caveats = {}
    for change in changes:
        if change.get("note"):
            caveats.setdefault(change["note"], []).append(change["name"])
    for note, names in caveats.items():
        named = ", ".join(f"`{name}`" for name in dict.fromkeys(names))
        warnings.append(f"Changed {named}: {note}")
    unproven = _UNPROVEN_RUST[0].upper() + _UNPROVEN_RUST[1:] + "."
    for note in (_OWNED_RUST, _UNREAD_RUST, _COMPOSITE_RUST, unproven, _STALE_RUST + "."):
        if any(row.get("note") == note for row in rows):
            warnings.append(note)
    # Any other row's note, once per selector: rows marked `[note]` in the table. What
    # is read through a local shares that local's (#148).
    noted = {}
    for row in rows:
        if row.get("note") and row["note"] not in _rust_notes():
            note = _THROUGH.sub("", row["note"], count=1)
            noted.setdefault(note, []).append(row["selector"])
    for note, selectors in noted.items():
        names = ", ".join(dict.fromkeys(selectors))
        warnings.append(f"{names}: {note[0].upper()}{note[1:]}.")
    if probe.set and any("at" not in change for change in changes) and probe.limit > 1:
        warnings.append(
            f"Interventions are applied once, at hit {probe.set_hit}; other hits observe "
            "without re-applying."
        )
    elif (
        probe.set
        and not any("at" not in change for change in changes)
        and hits < probe.set_hit
        and result.get("status") != "failed"
    ):
        # Only the --set: a --set-at may have been applied (#151 review).
        named = ", ".join(f"--set {name}={value}" for name, value in probe.set)
        warnings.append(
            f"{named} {'was' if len(probe.set) == 1 else 'were'} not applied: the program "
            f"stopped {hits} time(s) at line {line}, before hit {probe.set_hit}."
        )
    for number, named in set_functions.items():
        if len(named) > 1 or set(alike.get(number, ())) & set(named):
            asked = next(p["line"] for p in points if p["build_line"] == number)
            warnings.append(
                (f"Line {line}" if number == line else f"--set-at line {asked}")
                + f" stopped in {len(named)} function{'s' if len(named) > 1 else ''}: "
                + _stops_by_function(named, labels, "stop", alike.get(number))
                + (
                    ". --hits, --set-hit and --set-at's @HIT count every stop of the line, "
                    "in any function; the function column and each change say which is "
                    "which."
                    if number == line
                    else ". @HIT counts every stop of the line, in any function; each "
                    "change says which function's variable it changed."
                )
            )
    unapplied = [point for point in points if point["change"] is None]
    if probe.set and not any("at" not in change for change in changes) and hits < probe.set_hit:
        # A --set whose hit never came: reported like a --set-at's (exit code 4).
        result["unapplied"] = [
            {
                "option": "--set",
                "at": f"{probe.line}@{probe.set_hit}",
                "name": name,
                "value": value,
                "stops": hits,
            }
            for name, value in probe.set
        ]
    if unapplied:
        # Never silently: a --set-at whose stop never came (#137).
        result.setdefault("unapplied", []).extend(
            {
                "option": "--set-at",
                "at": f"{point['line']}@{point['hit']}",
                "name": point["name"],
                "value": point["value"],
                "stops": seen.get(point["build_line"], 0),
            }
            for point in unapplied
        )
        if result.get("status") != "failed":
            ended = (
                "before the probe timed out"
                if result.get("status") == "timeout"
                else "before the probe ended"
            )
            for point in unapplied:
                stops = seen.get(point["build_line"], 0)
                warnings.append(
                    f"--set-at {probe.program}:{point['line']}@{point['hit']} "
                    f"{point['name']}={point['value']} was not applied: line "
                    f"{point['build_line']} stopped {stops} time(s) {ended}, so its hit "
                    f"{point['hit']} never came."
                )
    result.update(
        warnings=warnings,
        hits=hits,
        pauses_ms=pauses,
        **({"set_pauses_ms": set_pauses} if set_pauses else {}),
        by_hit=_by_hit(rows),
        rows=rows,
        interventions=changes,
        seconds=round(time.monotonic() - started, 3),
        note="Interventions are runtime evidence for this run only, not a verified fix.",
    )
    return result


def _by_hit(rows):
    """The same rows regrouped per hit: {hit, line, observed: {...}, after-set: {...}}."""
    grouped = {}
    for row in rows:
        entry = grouped.setdefault(
            row["hit"],
            {
                "hit": row["hit"],
                "line": row.get("line"),
                "function": row.get("function"),
                **({"stop": row["stop"], "stops": row["stops"]} if "stops" in row else {}),
            },
        )
        value = row["value"] if "value" in row else {"error": row.get("error")}
        entry.setdefault(row["phase"], {})[row["selector"]] = value
    return list(grouped.values())


def _rust_notes():
    """The Rust rows' notes, each said once among a probe's warnings, unmarked."""
    unproven = _UNPROVEN_RUST[0].upper() + _UNPROVEN_RUST[1:] + "."
    return {_OWNED_RUST, _UNREAD_RUST, _COMPOSITE_RUST, unproven, _STALE_RUST + "."}


def _table(result):
    lines = []
    # Which of a line's breakpoint locations each stop is at, where it has several (#157).
    stops = any("stops" in row for row in result["rows"])
    header = ("hit", *(("stop",) if stops else ()), "phase", "line", "function", "selector")
    header += ("type", "value")
    # A C++ frame's signature (`trim_left(std::__1::basic_string<...> const&)`) would
    # double the table's width; Rust's ends with a symbol hash. JSON keeps the name whole.
    functions = function_labels(row.get("function") for row in result["rows"])

    def short_instance(row):
        # A short instance name (`main::{closure#1}`, `tf<rc::A>`); JSON has every one.
        return (
            row["instance"]
            if len(row.get("instance") or "") <= 60 and row.get("instance")
            else None
        )

    rows = [
        (
            str(row["hit"]),
            *((f"{row['stop']}/{row['stops']}" if "stops" in row else "",) if stops else ()),
            row["phase"],
            str(row.get("line", "")),
            short_instance(row) or functions.get(row.get("function"), ""),
            row["selector"],
            row.get("type") or "",
            row.get("value", "<" + row.get("error", "") + ">")
            + (f"  [{row['view']}]" if row.get("view") else "")
            + ("  [settable]" if row.get("settable") else "")
            + (f"  [not settable: {row['not_settable']}]" if row.get("not_settable") else "")
            + (f"  [id {row['identity']}]" if row.get("identity") else "")
            + ("  [module global]" if row.get("scope") == "globals" else "")
            + ("  [note]" if row.get("note") and row["note"] not in _rust_notes() else ""),
        )
        for row in result["rows"]
    ]
    widths = [max(len(item[i]) for item in [header, *rows]) for i in range(len(header) - 1)]
    for item in [header, *rows]:
        lines.append(
            "  ".join(text.ljust(width) for text, width in zip(item, widths)) + "  " + item[-1]
        )
    # Rows shown by their instance are found in SQL by `instance`, not short_function.
    shown = next((label for row in result["rows"] if (label := short_instance(row))), None)
    if note := shortened_note(
        {
            row.get("function"): functions.get(row.get("function"))
            for row in result["rows"]
            if not short_instance(row)
        },
        shown,
    ):
        lines.append(note)
    named = function_labels(change.get("function") for change in result["interventions"])
    for change in result["interventions"]:
        where = change.get("instance") or named.get(change.get("function"), change.get("function"))
        if change.get("alike"):
            where = f"{where}, one of {change['alike']} on the line the debugger names alike"
        # Which of the line's breakpoint locations the stop was at, where it has several.
        stop = f", stop {change['stop']} of {change['stops']}" if change.get("stops") else ""
        lines.append(
            f"set {change['name']}: {change['before']} -> {change['after']}"
            + (
                f"  (--set-at line {change['at']}{stop}" + (f", in {where})" if where else ")")
                if change.get("at")
                else f"  ({stop[2:]})"
                if stop
                else ""
            )
        )
    unmade = len(result.get("unapplied") or ())
    lines.append(
        f"status: {result['status']}"
        + (
            f" ({unmade} requested change{'s' if unmade > 1 else ''} not made)"
            if unmade and result["status"] == "completed"
            else ""
        )
        + f"  hits: {result['hits']}"
        + (
            {"failed": " before it failed", "timeout": " before it timed out"}.get(
                result["status"],
                " (all)" if (result.get("probe") or {}).get("hits") == "all" else "",
            )
        )
        + (f"  exit code: {result['exit_code']}" if result.get("exit_code") else "")
        + f"  seconds: {result['seconds']}"
        + (
            f"  max pause: {max(paused)} ms"
            if (paused := [*result.get("pauses_ms", ()), *result.get("set_pauses_ms", ())])
            else ""
        )
    )
    for warning in result.get("warnings", ()):
        lines.append(f"warning: {warning}")
    if error := result.get("error"):
        lines.append(f"error [{error['code']}]: {error['message']}")
        lines.append(f"hint: {error['hint']}")
        if error.get("cleanup"):
            lines.append(f"cleanup: {error['cleanup']}")
    if result.get("output"):
        complete = result.get("output_complete", True)
        lines.append(
            "output:"
            if complete
            else "output so far (the program was stopped when the probe failed):"
            if result.get("status") == "failed"
            else "output so far (the program was still running):"
        )
        lines.extend("  " + line for line in result["output"].splitlines()[-20:])
    return "\n".join(lines)


def _own_set_at(program, where, assignment):
    path, line, hit, name, value = parse_set_at(where, assignment)
    if path is not None and path != program:
        raise ValueError(f"--set-at {where}: give a line of the --at file ({program})")
    return line, hit, name, value


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="peeky-probe",
        description="One-shot stateless probe: stop at a line, capture values, optionally "
        "change locals, run to completion and report. Example: peeky-probe --root examples "
        "--at pagination_bug.py:7 --capture page,page_size --set page=0",
    )
    parser.add_argument("--root", default=".", help="Source root containing the program")
    parser.add_argument("--at", required=True, help="Probe location <path>:<line>")
    parser.add_argument("--capture", default="", help="Comma-separated selectors, e.g. a,b.c")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        help="Intervention name=value: a local, or a Java field or array element (repeatable)",
    )
    parser.add_argument(
        "--hits", type=parse_hits, default=1, help="Stops to capture, or 'all' (default 1)"
    )
    parser.add_argument("--timeout", type=float, default=60, help="Overall seconds (default 60)")
    parser.add_argument("--until", choices=("exit", "hits"), default="exit")
    parser.add_argument("--set-hit", type=int, default=1, help="Hit that gets --set (default 1)")
    parser.add_argument(
        "--set-at",
        nargs=2,
        action="append",
        default=[],
        metavar=("LINE[@HIT]", "NAME=VALUE"),
        help="Intervention at hit HIT (default 1) of LINE of the --at file (repeatable)",
    )
    parser.add_argument("--run", help="Python script to run when --at is in an imported module")
    parser.add_argument("--arg", action="append", default=[], help="Python program argument")
    parser.add_argument("--expect-sha256", action="append", default=[], help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="Print the full JSON result")
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument("--java-config", type=Path)
    targets.add_argument("--native-config", type=Path)
    targets.add_argument("--rust-config", type=Path)
    args = parser.parse_args(argv)
    try:
        program, line = parse_location(args.at)
        changes = []
        for item in args.set:
            name, separator, value = item.partition("=")
            if not separator:
                raise ValueError("--set expects name=value")
            changes.append((name.strip(), value.strip()))
        probe = Probe(
            program,
            line,
            split_selectors(args.capture),
            tuple(changes),
            args.hits,
            args.timeout,
            args.until,
            args.set_hit,
            entry=args.run,
            args=tuple(args.arg),
            expect_sha256=tuple(tuple(item.split("=", 1)) for item in args.expect_sha256),
            set_at=tuple(_own_set_at(program, *item) for item in args.set_at),
        )
        adapter = None
        if args.java_config:
            adapter = load_java_target(args.java_config)
        elif args.native_config:
            adapter = load_native_target(args.native_config)
        elif args.rust_config:
            adapter = load_rust_target(args.rust_config)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    result = run_probe(args.root, probe, adapter=adapter)
    print(json.dumps(result, indent=2) if args.json else _table(result))
    return probe_exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
