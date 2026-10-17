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

from peeky.names import display_function
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
MAX_ROWS = 2000
MAX_SECONDS = 300
# HTTP probes must finish inside Client.call's 45 s timeout.
HTTP_PROBE_SECONDS = 40
_INDEX = re.compile(r"\[?([0-9]{1,6})\]?")
# A quoted string key, as written in a selector and as debugpy names mapping entries.
# Escapes Python accepts without a warning; anything else (`\d`, `\777`) is refused.
_ESCAPE = r"""\\(?:[\\'"abfnrtv]|[0-3][0-7]{2}|[0-7]{1,2}(?![0-7])|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|N\{[A-Z0-9 -]{1,88}\})"""
_KEY_LITERAL = rf"'(?:[^'\\\n]|{_ESCAPE}){{0,256}}'" + "|" + rf'"(?:[^"\\\n]|{_ESCAPE}){{0,256}}"'
# One selector step: `.name`, `[3]`, `[-1]`, or a quoted string key `['id']` / `["id"]`.
_STEP = re.compile(
    r"\.(?P<name>[A-Za-z_][A-Za-z0-9_]{0,127})"
    r"|\[(?P<index>-?[0-9]{1,6})\]"
    rf"|\[(?P<key>{_KEY_LITERAL})\]"
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def parse_selector(text):
    """`request.page` -> ['request', 'page']; `args[0]` -> ['args', '[0]'];
    `order['id']` -> ['order', "['id']"].

    Negative indexes (`items[-1]`) count from the end of a sequence whose elements
    all fit in the bounded inspection window. A quoted key reads one entry of a
    mapping; it is kept in its canonical Python form, `[` + repr(key) + `]`.
    """
    if not isinstance(text, str) or not text or len(text) > 512:
        raise ValueError("Selectors must be nonempty dotted paths")
    first = _NAME.match(text)
    if not first:
        raise ValueError(f"Invalid selector: {text}{_SELECTOR_HELP}")
    segments, position = [first[0]], first.end()
    while position < len(text):
        step = _STEP.match(text, position)
        if not step:
            raise ValueError(f"Invalid selector: {text}{_SELECTOR_HELP}")
        if step["name"]:
            segments.append(step["name"])
        elif step["index"]:
            segments.append(f"[{step['index']}]")
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
    " (selectors are paths, never expressions: name, a.b, a[0], a[-1], a['key'] or a[\"key\"])"
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
    "A candidate edit (--old/--new) needs a target Peeky builds (C, C++, Rust or Java). Python "
    "runs its files as they are: edit the file and probe again, or check the edit with "
    "`peeky verify`. An attached JVM is already running"
)


def candidate_line(text, old, new, line):
    """The line of the edited text that holds original line ``line``.

    Lines before the edit keep their number and lines after it shift by the lines the
    edit adds or removes. A line inside the edit maps only when the edit keeps its line
    count; otherwise there is no faithful answer, so it is refused.
    """
    start = text.find(old)
    if start < 0 or text.count(old) != 1:
        raise ValueError("--old must match the --at file exactly once")
    first = text.count("\n", 0, start) + 1
    last = text.count("\n", 0, start + len(old) - 1) + 1
    delta = new.count("\n") - old.count("\n")
    if line < first:
        return line
    if line > last:
        return line + delta
    if delta == 0:
        return line
    span = (
        f"line {first}, which becomes" if first == last else f"lines {first}-{last}, which become"
    )
    after = last + delta
    became = f"line {first}" if after <= first else f"lines {first}-{after}"
    raise ValueError(
        f"Line {line} is inside the edit ({span} {became} of the candidate). An edit that "
        "changes the number of lines has no matching line inside it: probe a line before or "
        "after the edit"
    )


def parse_location(text):
    """`src/main.rs:36` -> ('src/main.rs', 36)."""
    program, separator, line = (text or "").rpartition(":")
    if not separator or not program or not re.fullmatch(r"[1-9][0-9]{0,6}", line):
        raise ValueError("Location must be <relative-source-path>:<line>")
    return program, int(line)


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

    @property
    def limit(self):
        """The most hits this probe captures: --hits N, or the cap for --hits all.

        `all` stops at MAX_HITS, or earlier when that many hits of every selector (plus
        one set of after-set rows) would exceed the MAX_ROWS row budget.
        """
        if self.hits != "all":
            return self.hits
        after_set = len(self.capture) if self.set else 0
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
        if not isinstance(self.set, tuple) or len(self.set) > MAX_SETS:
            raise ValueError(f"At most {MAX_SETS} interventions")
        for change in self.set:
            if (
                not isinstance(change, tuple)
                or len(change) != 2
                or not all(isinstance(part, str) for part in change)
                or len(parse_selector(change[0])) != 1
            ):
                raise ValueError("Interventions are (local-name, value) string pairs")
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
        if self.edit is not None and (
            not isinstance(self.edit, tuple)
            or len(self.edit) != 2
            or not all(isinstance(text, str) for text in self.edit)
            or not self.edit[0]
            or self.edit[0] == self.edit[1]
            or any(len(text.encode()) > MAX_EDIT_BYTES for text in self.edit)
        ):
            raise ValueError(
                "A candidate edit is (old, new): old nonempty, new different, each at most "
                f"{MAX_EDIT_BYTES // (1024 * 1024)} MiB"
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


def _visible(snapshot):
    # lldb-dap names a nameless slot (a closure's environment) "<null>": not selectable.
    names = sorted(
        {v["name"] for scope in snapshot["scopes"] for v in scope["variables"]} - {"<null>"}
    )
    shown = ", ".join(names[:40]) + (f", ... ({len(names)} total)" if len(names) > 40 else "")
    return shown or "(none)"


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


def _resolve(worker, stop, snapshot, segments):
    matches = [
        variable
        for scope in snapshot["scopes"]
        for variable in scope["variables"]
        if variable["name"] == segments[0]
    ]
    if not matches:
        raise ValueError(
            f"not found; visible: {_visible(snapshot)}"
            + _member_hint(worker, stop, snapshot, segments[0])
        )
    variable = matches[0]
    for segment in segments[1:]:
        handle = variable.get("variablesReference", 0)
        if not handle:
            if variable.get("depth_limited"):
                raise ValueError(
                    f"cannot expand {variable['name']!r}: it is at the inspection depth limit "
                    f"(4 levels below a local)"
                )
            raise ValueError(f"cannot expand {variable['name']!r} ({variable.get('view', 'leaf')})")
        if segment.startswith("[-"):
            variable = _from_end(worker, stop, variable, int(segment[2:-1]), segment)
            continue
        key = _key(segment)
        found, start, incomplete, children = None, 0, False, []
        while found is None:
            page = worker.dispatch("expand", {**stop, "handle": handle, "start": start})
            children.extend(page["variables"])
            found = next((v for v in page["variables"] if _names(v, segment)), None)
            if found is not None or not page["truncated"]:
                break
            if start + 64 > 511 or page.get("remaining_nodes", 1) <= 0:
                incomplete = True
                break
            start += 64
        if found is None and incomplete:
            raise ValueError(
                f"{_shown(segment)} is beyond the bounded inspection window (512 children)"
            )
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
            raise ValueError(_not_found(variable, segment, children))
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
    return variable


_RUST_TEXT = frozenset({"&str", "&mut str", "alloc::string::String *"})
# Owned Rust containers are raw: a moved or dropped local can still point at freed memory.
_OWNED_RUST_TYPES = frozenset(
    {"alloc::vec::Vec<int, alloc::alloc::Global>", "alloc::string::String"}
)
_OWNED_RUST = (
    "Owned Rust Vec and String locals show their raw fields; Peeky reads contents only "
    "through reference parameters (#53). Capture `.len` for the length, or read the value "
    "where it is passed as &Vec, &[T], &String or &str."
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


def _not_found(variable, segment, children):
    """Why `segment` is absent, with the selectors that would work."""
    grouped = sorted({child["name"] for child in children} & _VALUE_GROUPS)
    reach = (
        f" (debugpy files entries whose values are functions or classes under "
        f"{' and '.join(repr(group) for group in grouped)}, which selectors cannot reach)"
        if grouped
        else ""
    )
    return _absence(variable, segment, children) + reach


def _absence(variable, segment, children):
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
        if variable.get("view") == "memory":
            if kind in _RUST_TEXT:
                message += "; text read from memory is shown whole, not by element"
        elif kind.startswith(_RUST_CONTAINERS):
            message += (
                "; Peeky reads Rust contents only for reference parameters (&[i32], &str, "
                "&Vec<i32>, &String, or &mut), with a debugger that supports readMemory "
                "(lldb-dap from LLVM 20). Owned Vec and String locals stay raw: a moved or "
                "dropped local can still point at freed memory"
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
    if match := re.fullmatch(r"\w+\[([0-9]{1,9})\]@[0-9a-fx]+", str(variable.get("value") or "")):
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
            raise ValueError(
                f"{segment!r} needs the sequence length, but it has more elements than the "
                "bounded inspection window (512); use a non-negative index"
            )
        start += 64
    count = len(elements)
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


# Errors a fresh inspection can cure: it restores the expansion node budget.
_BUDGET = re.compile(
    r"budget|beyond the bounded|bounded inspection window|exceeds bounded expansion"
)


def _capture(worker, stop, selectors, hit, phase, snapshot=None, children=False):
    """Rows for each selector at one stop.

    Selectors share one inspection (its handles stay valid until the next inspect or
    change); a selector that runs out of expansion budget is retried once on a fresh
    inspection, so sharing never turns a readable value into an error. With
    ``children``, an expandable value also lists its first page of children, each with
    the selector that reads it.
    """
    rows = []
    for selector in selectors:
        row = {"hit": hit, "phase": phase, "selector": selector}
        fresh = snapshot is None
        if fresh:
            snapshot = worker.dispatch("inspect", stop)
        frame = snapshot["frames"][0] if snapshot["frames"] else {}
        row["line"] = frame.get("line")
        if frame.get("name"):
            # Tells a function body from a nested generator/lambda frame on the same line.
            row["function"] = frame["name"]
        try:
            try:
                variable = _resolve(worker, stop, snapshot, parse_selector(selector))
            except ValueError as exc:
                if fresh or not _BUDGET.search(str(exc)):
                    raise
                snapshot = worker.dispatch("inspect", stop)
                variable = _resolve(worker, stop, snapshot, parse_selector(selector))
        except ValueError as exc:
            row["error"] = str(exc)
        else:
            if variable.get("unavailable"):
                # A placeholder such as "<error: variable not available>" is not a value.
                reason = variable.get("reason", "unavailable")
                row.update(
                    type=variable.get("type"),
                    unavailable=True,
                    error=(
                        f"no value here: optimized out or not live (debugger: {reason})"
                        if snapshot.get("optimized")
                        else f"the debugger could not read a value (debugger: {reason})"
                    ),
                )
            else:
                row.update(value=variable["value"], type=variable.get("type"))
                if "view" not in variable and str(variable.get("type")) in _OWNED_RUST_TYPES:
                    row["note"] = _OWNED_RUST
            if "view" in variable:
                row["view"] = variable["view"]
            if children and variable.get("variablesReference"):
                try:
                    try:
                        row.update(_children(worker, stop, selector, variable))
                    except ValueError as exc:
                        if not _BUDGET.search(str(exc)):
                            raise
                        # As for values: a fresh inspection restores the node budget.
                        snapshot = worker.dispatch("inspect", stop)
                        variable = _resolve(worker, stop, snapshot, parse_selector(selector))
                        row.update(_children(worker, stop, selector, variable))
                except ValueError as exc:
                    row["children_error"] = str(exc)
        rows.append(row)
    return rows


_KEY_NAME = re.compile(_KEY_LITERAL)


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


def _children(worker, stop, selector, variable):
    """One page (64) of a value's children, for a person or agent deciding what to read."""
    page = worker.dispatch("expand", {**stop, "handle": variable["variablesReference"], "start": 0})
    listed = []
    for child in page["variables"]:
        if child["name"] in _GROUPS:
            continue
        entry = {"name": child["name"], "type": child.get("type"), "value": child.get("value")}
        if child.get("unavailable"):
            entry.update(unavailable=True, reason=child.get("reason"))
        path = _child_selector(selector, child["name"])
        if path and (
            not path.endswith("]") or _genuine(child, page["variables"], path[-2] in "'\"")
        ):
            entry["selector"] = path
        if child.get("variablesReference"):
            entry["expandable"] = True
        listed.append(entry)
    return {"children": listed, **({"children_truncated": True} if page["truncated"] else {})}


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
    result = {"probe": {**asdict(probe), "set": [list(change) for change in probe.set]}}
    # The edit's texts are not evidence of the run; the CLI adds the diff.
    result["probe"].pop("edit")
    rows, changes, hits, warnings, relocated, pauses = [], [], 0, [], False, []
    # The breakpoint line in the build that runs: the candidate's numbering with an edit.
    line = probe.line
    # {function: [hits]}: a line shared by a function and a closure or lambda stops in each.
    functions = {}
    last_said = 0.0
    try:
        if probe.edit:
            old, new = probe.edit
            line = candidate_line(worker.path(probe.program).read_text(), old, new, probe.line)
            result["candidate"] = {"line": line, "original_line": probe.line}
        launch = worker.dispatch(
            "launch",
            {
                "program": probe.program,
                "breakpoints": [{"line": line}],
                **({"edit": {"old": old, "new": new}} if probe.edit else {}),
                **({"entry": probe.entry} if probe.entry else {}),
                **({"args": list(probe.args)} if probe.args else {}),
                **({"expect_sha256": dict(probe.expect_sha256)} if probe.expect_sha256 else {}),
            },
        )
        session = {"session_id": launch["session_id"]}
        result.update(
            breakpoint=(launch["breakpoints"] or [{}])[0],
            artifact_manifest_id=launch["artifact_manifest_id"],
            target_identity=launch["target_identity"],
            source_sha256=launch["source_sha256"],
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
        )
        say("waiting for the program to reach it...")
        status, noted = "timeout", time.monotonic()
        while (remaining := deadline - time.monotonic()) > 0:
            state = worker.dispatch("wait", {**session, "timeout": min(2, remaining)})
            if not verified and any(
                update.get("breakpoint", {}).get("verified") is True
                for update in state.get("breakpoint_events", [])
            ):
                verified = True
                say("breakpoint verified")
            if state["state"] == "running":
                if time.monotonic() - noted >= 10:
                    noted = time.monotonic()
                    phase = "for the next stop" if hits < probe.limit else "for the program to exit"
                    say(f"still waiting {phase} ({int(noted - started)} s elapsed)")
                continue
            if state["state"] == "terminated":
                if state.get("exit_inferred"):
                    warnings.append(
                        "The debugger never reported the program's exit; Peeky inferred it "
                        "because no threads were left (java-debug, issue #27)."
                    )
                status = "completed" if hits else "no-stop"
                say(f"program finished after {hits} hit(s)")
                break
            # From the moment the stop arrived, so debugger reads before capture count too.
            paused = state.get("event", {}).get("received", time.monotonic())
            stop = {**session, "stop_id": state["stop_id"]}
            # Only the requested breakpoint counts; a crash, signal or other stop fails
            # closed before any capture or intervention.
            expected = _expected_line(result["breakpoint"], state, line)
            snapshot = worker.dispatch("inspect", stop)
            frame = snapshot["frames"][0] if snapshot["frames"] else {}
            reason = state.get("event", {}).get("body", {}).get("reason")
            path = str(frame.get("source", {}).get("path", "")).replace("\\", "/")
            if (
                reason not in (None, "breakpoint")
                or frame.get("line") != expected
                or not (path == probe.program or path.endswith("/" + probe.program))
            ):
                raise ValueError(
                    f"Stopped at {path}:{frame.get('line')} ({reason}), not the requested "
                    f"{probe.program}:{expected}"
                )
            hits += 1
            functions.setdefault(display_function(frame.get("name")), []).append(hits)
            if expected != line and not relocated:
                relocated = True
                warnings.append(
                    f"The debugger moved the breakpoint from line {line} to line "
                    f"{expected} (line {line} has no code of its own); values are from "
                    f"line {expected}"
                    + (" of the candidate" if probe.edit else "")
                    + ". Choose a line from `peeky describe`."
                )
                say(warnings[-1])
            if _report_hit(hits, probe.limit, last_said):
                last_said = time.monotonic()
                of = f" (all, up to {probe.limit})" if probe.hits == "all" else f"/{probe.limit}"
                say(f"hit {hits}{of} at {probe.program}:{expected}; capturing")
            # The stop-validation inspection is reused for the first selectors.
            rows.extend(_capture(worker, stop, probe.capture, hits, "observed", snapshot))
            if hits == probe.set_hit and probe.set:
                for name, value in probe.set:
                    snapshot = worker.dispatch("inspect", stop)
                    scope = next(
                        (
                            s
                            for s in snapshot["scopes"]
                            if any(v["name"] == name for v in s["variables"])
                        ),
                        None,
                    )
                    if scope is None:
                        raise ValueError(
                            f"Intervention target {name!r} is not a visible local; "
                            f"visible: {_visible(snapshot)}"
                            + _member_hint(worker, stop, snapshot, name).replace(
                                "select it as", "it can be read as"
                            )
                            + "; only the stopped function's own locals can be changed"
                        )
                    changes.append(
                        worker.dispatch(
                            "set_variable",
                            {**stop, "reference": scope["reference"], "name": name, "value": value},
                        )
                    )
                    say(f"set {name}: {changes[-1].get('before')} -> {changes[-1].get('after')}")
                rows.extend(_capture(worker, stop, probe.capture, hits, "after-set"))
            if len(rows) > MAX_ROWS:
                raise ValueError("Probe row budget exceeded")
            if hits >= probe.limit:
                worker.dispatch(
                    "breakpoints", {**session, "program": probe.program, "breakpoints": []}
                )
            worker.dispatch("continue", stop)
            pauses.append(round((time.monotonic() - paused) * 1000, 1))
            if hits >= probe.limit:
                if probe.until == "hits":
                    status = "completed"
                    say("all requested hits captured; ending the probe now")
                    break
                say(
                    f"reached the {probe.limit}-hit cap; letting the program finish"
                    if probe.hits == "all"
                    else "all requested hits captured; letting the program finish"
                )
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
        if status == "timeout":
            from peeky.errors import classify

            result["error"] = classify(
                TimeoutError(f"Probe exceeded its {probe.timeout} s timeout"), session_closed=True
            )
        if status == "no-stop":
            result["error"] = {
                "code": "NO_STOP",
                "message": f"The program finished without stopping at {probe.program}:"
                f"{line}"
                + (f" of the candidate (line {probe.line} before the edit)" if probe.edit else "")
                + "; check `peeky describe` for executable lines",
                "hint": "Pick a line the program executes; see `peeky describe`.",
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
    finally:
        worker.close()
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
    if len(functions) > 1:
        warnings.append(
            f"Line {line} stopped in {len(functions)} functions: "
            + "; ".join(
                f"{name or '?'} (hit{'s' if len(numbers) > 1 else ''} "
                f"{', '.join(map(str, numbers[:8]))}{', ...' if len(numbers) > 8 else ''})"
                for name, numbers in list(functions.items())[:4]
            )
            + ". --hits and --set-hit count every stop; the function column says which "
            "is which."
        )
    if any(row.get("note") == _OWNED_RUST for row in rows):
        warnings.append(_OWNED_RUST)
    if changes and probe.limit > 1:
        warnings.append(
            f"Interventions are applied once, at hit {probe.set_hit}; other hits observe "
            "without re-applying."
        )
    elif probe.set and not changes and hits < probe.set_hit and result.get("status") != "failed":
        warnings.append(
            f"No intervention was applied: the program stopped {hits} time(s), before hit "
            f"{probe.set_hit}."
        )
    result.update(
        warnings=warnings,
        hits=hits,
        pauses_ms=pauses,
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
            {"hit": row["hit"], "line": row.get("line"), "function": row.get("function")},
        )
        value = row["value"] if "value" in row else {"error": row.get("error")}
        entry.setdefault(row["phase"], {})[row["selector"]] = value
    return list(grouped.values())


def _table(result):
    lines = []
    header = ("hit", "phase", "line", "function", "selector", "type", "value")
    rows = [
        (
            str(row["hit"]),
            row["phase"],
            str(row.get("line", "")),
            # Rust frame names end with a symbol hash (`main::h56a2…`); JSON keeps it.
            display_function(row.get("function")),
            row["selector"],
            row.get("type") or "",
            row.get("value", "<" + row.get("error", "") + ">")
            + (f"  [{row['view']}]" if row.get("view") else ""),
        )
        for row in result["rows"]
    ]
    widths = [max(len(item[i]) for item in [header, *rows]) for i in range(len(header) - 1)]
    for item in [header, *rows]:
        lines.append(
            "  ".join(text.ljust(width) for text, width in zip(item, widths)) + "  " + item[-1]
        )
    for change in result["interventions"]:
        lines.append(f"set {change['name']}: {change['before']} -> {change['after']}")
    lines.append(
        f"status: {result['status']}  hits: {result['hits']}"
        + (" (all)" if (result.get("probe") or {}).get("hits") == "all" else "")
        + f"  seconds: {result['seconds']}"
        + (f"  max pause: {max(result['pauses_ms'])} ms" if result.get("pauses_ms") else "")
    )
    for warning in result.get("warnings", ()):
        lines.append(f"warning: {warning}")
    if error := result.get("error"):
        lines.append(f"error [{error['code']}]: {error['message']}")
        lines.append(f"hint: {error['hint']}")
    if result.get("output"):
        complete = result.get("output_complete", True)
        lines.append("output:" if complete else "output so far (the program was still running):")
        lines.extend("  " + line for line in result["output"].splitlines()[-20:])
    return "\n".join(lines)


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
        "--set", action="append", default=[], help="Local intervention name=value (repeatable)"
    )
    parser.add_argument(
        "--hits", type=parse_hits, default=1, help="Stops to capture, or 'all' (default 1)"
    )
    parser.add_argument("--timeout", type=float, default=60, help="Overall seconds (default 60)")
    parser.add_argument("--until", choices=("exit", "hits"), default="exit")
    parser.add_argument("--set-hit", type=int, default=1, help="Hit that gets --set (default 1)")
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
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
