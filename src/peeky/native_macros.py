"""A unit's availability probes, proven by the build's own record of its macros.

A probe (`__has_include`) that finds no file now may have found one when the program was
built: the build's environment may have added a header path, or a file may since be gone.
With `-fdebug-macro`, clang records in `.debug_macro` each file the unit's preprocessing
entered, by the line including it, and each `#define` and `#undef` it ran, by line
(``build_records``). Peeky replays the preprocessing with `-dD`, which writes the same
(``replay_record``), and compares them (``difference``). Where they are equal, the build
entered the files the replay did and ran the same macro directives, on the same lines.

A probe chooses a branch of its group (``conditionals``). Each branch's directives leave
records on its own lines, so two branches leave the same record only where neither leaves
any. A branch surely leaves one where every way through it that the build may take does
(``unsettled``): it holds a `#define` or an `#undef`, or an `#error` (a build can't get
past it), or a group within it every branch of which surely leaves one, with an `#else`. An
`#include` may leave none: a file already included under `#pragma once` or a guard is
skipped. So where at most one branch may leave none, an absent `#else` counting as one,
the build took the replay's branch. A probe whose branches differ in code or includes alone
stays unproven, as does one whose group holds a `#line` or a line marker, under which two
branches' records may be alike. A directive may begin with `#`, the digraph `%:`, or the
trigraph `??=`; a file holding `??=` or `??/`, which -trigraphs makes `#` and a line splice,
is read conservatively: none of its probes is proven.
"""

import os
import posixpath
import re
from dataclasses import dataclass, field

from peeky.c_source import blank, normalized

END = ("end",)
_HEADER = re.compile(r"macro header: .*\bdebug_line_offset = (0x[0-9a-fA-F]+)")
_EVENT = re.compile(r"DW_MACRO_(\w+) - lineno: (\d+) (?:filenum: (\d+)|macro: ?(.*))$")
_MARKER = re.compile(r'# (\d+) "((?:[^"\\]|\\.)*)"((?: \d)*)$')
# A directive's introducer: `#`, the digraph `%:`, or the trigraph `??=` (under -trigraphs).
_DIRECTIVE = re.compile(r"[ \t]*(?:#|%:|\?\?=)[ \t]*(\w*)")
# Trigraphs that change which lines are directives: `??=` (`#`) and `??/` (`\`, which
# splices lines). Whether they apply depends on -trigraphs and the standard.
_TRIGRAPHS = ("??=", "??/")
_LONE = re.compile(r"(__has_include_next|__has_include)[ \t]*\(")
# Directives that always leave a record, or fail the build.
_RECORDED = frozenset(("define", "undef", "error"))


def build_records(lines, files):
    """{line table offset: [event], or why it can't be read}: the record of macros of each
    unit whose line table is at an offset of ``files`` ({offset: {file index: its path,
    resolved}}), from llvm-dwarfdump's `--debug-macro` ``lines``. Events: ("start", line,
    path), END, ("define", line, text), ("undef", line, text). A unit with no record has
    none."""
    records, twice, offset, events = {}, set(), None, None
    for line in lines:
        text = line.strip()
        if text.startswith("macro header:"):
            if offset is not None:
                records[offset] = events
            header = _HEADER.match(text)
            offset = int(header[1], 16) if header else None
            if offset in records:
                twice.add(offset)
            offset = offset if offset in files and offset not in twice else None
            events = []
            continue
        if offset is None or not isinstance(events, list) or not text.startswith("DW_MACRO_"):
            continue
        found = _EVENT.match(text)
        kind = found[1] if found else text.split()[0].removeprefix("DW_MACRO_")
        if kind == "end_file":
            events.append(END)
        elif found and kind == "start_file" and found[3] is not None:
            path = files[offset].get(int(found[3]))
            if path is None:
                events = f"its record of macros names file {found[3]}, which its line table doesn't"
            else:
                events.append(("start", int(found[2]), path))
        elif found and found[4] is not None and kind.split("_")[0] in ("define", "undef"):
            events.append((kind.split("_")[0], int(found[2]), found[4].rstrip()))
        else:
            events = f"its record of macros holds DW_MACRO_{kind}, which Peeky doesn't read"
    if offset is not None:
        records[offset] = events
    for each in twice:
        records[each] = "its line table has more than one record of macros"
    return records


def _unescaped(path):
    return re.sub(r"\\(.)", r"\1", path)


def replay_record(lines, comp_dir):
    """The record of macros clang's `-E -dD` text (``lines``) holds, as build_records reads
    a build's: each file it enters, by the line including it, and each `#define` and
    `#undef` by line; then those predefined and on the command line, at line 0, after the
    main file ends. None where its line markers leave a file Peeky didn't see entered.
    Paths resolved under ``comp_dir``. Also the files it entered, as each marker names
    them and resolved."""
    events, builtins, stack, entered = [], [], [], set()
    line, pseudo = 0, False
    for raw in lines:
        raw = raw.rstrip("\n")
        marker = _MARKER.match(raw) if raw.startswith("# ") else None
        if marker is not None:
            number, path, flags = int(marker[1]), _unescaped(marker[2]), marker[3].split()
            if path.startswith("<"):  # <built-in>, <command line>
                pseudo = True
                continue
            pseudo = False
            real = os.path.realpath(posixpath.join(comp_dir, path))
            entered |= {path, real}
            if not stack:
                events.append(("start", 0, real))
                stack.append(real)
            elif "1" in flags:
                events.append(("start", line, real))
                stack.append(real)
            elif "2" in flags:
                if real not in stack:
                    return None, entered
                while stack[-1] != real:
                    events.append(END)
                    stack.pop()
            line = number
            continue
        if not stack and not pseudo:
            continue
        for kind in ("define", "undef"):
            if raw.startswith(f"#{kind} "):
                kept = (kind, 0 if pseudo else line, raw[len(kind) + 2 :].rstrip())
                (builtins if pseudo else events).append(kept)
        if not pseudo:
            line += 1
    events += [END] * len(stack)
    return events + builtins, entered


def difference(build, replay, shown):
    """None where the build's record of macros (build_records') is the replay's
    (replay_record's); else the first difference, as a phrase. ``shown``: a path as the
    note names it."""
    files = []
    for at, (ours, theirs) in enumerate(zip(build, replay, strict=False)):
        if ours != theirs:
            where = shown(files[-1]) if files else "its file"
            return f"at {where}, the build recorded {_said(ours, shown)}, the replay {_said(theirs, shown)}"
        if ours[0] == "start":
            files.append(ours[2])
        elif ours == END and files:
            files.pop()
    if len(build) != len(replay):
        more, who = (build, "build") if len(build) > len(replay) else (replay, "replay")
        return f"only the {who} recorded {_said(more[min(len(build), len(replay))], shown)}"
    return None


def _said(event, shown):
    if event == END:
        return "a file's end"
    if event[0] == "start":
        return f"{shown(event[2])}, included on line {event[1]}"
    name = event[2].split(" ", 1)[0].split("(", 1)[0]
    return f"#{event[0]} {name} on line {event[1]}"


@dataclass
class Branch:
    """One branch of a conditional group: its directive's lines (``line`` to ``end``),
    which (`if`, `elif`, `else`, …), its condition (blanked), and what of it may leave a
    record of macros, in order: `define`, `undef`, `error`, or a Group within it."""

    line: int
    end: int
    kind: str
    condition: str
    holds: list = field(default_factory=list)


@dataclass
class Group:
    """A conditional group: its branches, whether a `#line` or a line marker is within it
    (``renumbered``), under which two branches' records may be alike, and whether its file
    holds a trigraph that may make a directive (``trigraphed``), where Peeky can't be sure
    of its lines."""

    branches: list
    renumbered: bool = False
    trigraphed: bool = False


def conditionals(text):
    """{line: (group, index)}: for each line of an `#if` or `#elif` directive of ``text``,
    its Group and which branch it begins. A group left unclosed is none. A directive may
    begin `#`, `%:` or `??=`; a file holding `??=` or `??/` has each group ``trigraphed``."""
    original = normalized(text)
    blanked = blank(original).split("\n")
    stack, groups, at = [], [], 0
    while at < len(blanked):
        first, logical = at, blanked[at]
        while logical.endswith("\\") and at + 1 < len(blanked):
            at += 1
            logical = logical[:-1] + blanked[at]
        at += 1
        directive = _DIRECTIVE.match(logical)
        if directive is None:
            continue
        name = directive[1]
        branch = Branch(first + 1, at, name, logical[directive.end() :])
        if name in ("if", "ifdef", "ifndef"):
            group = Group([branch])
            if stack:
                stack[-1].branches[-1].holds.append(group)
            stack.append(group)
        elif name in ("elif", "elifdef", "elifndef", "else") and stack:
            stack[-1].branches.append(branch)
        elif name == "endif" and stack:
            groups.append(stack.pop())
        elif name == "line" or name.isdigit():
            for group in stack:
                group.renumbered = True
        elif stack and name in _RECORDED:
            stack[-1].branches[-1].holds.append(name)
    trigraphed = any(each in original for each in _TRIGRAPHS)
    where = {}
    for group in groups:
        group.trigraphed = trigraphed
        for index, branch in enumerate(group.branches):
            if branch.kind in ("if", "elif"):
                for line in range(branch.line, branch.end + 1):
                    where[line] = (group, index)
    return where


def _sure(holds):
    """Whether every way the build may take through what a branch ``holds`` leaves a record
    of macros."""
    return any(
        held in _RECORDED
        if isinstance(held, str)
        else held.branches[-1].kind == "else"
        and all(_sure(branch.holds) for branch in held.branches)
        for held in holds
    )


def unsettled(group):
    """Why the build's record of its macros may not tell which branch of ``group`` the
    build took, or None: at most one of its branches, an absent `#else` counting as one,
    may leave no record, no `#line` or line marker is within it, and its file holds no
    trigraph that may make a directive."""
    if group.renumbered:
        return (
            "its group holds a #line or a line marker, under which two of its branches may "
            "leave the same record of macros"
        )
    if group.trigraphed:
        return (
            "its file holds a trigraph (??= or ??/) that may make or join a directive, so Peeky "
            "can't be sure which lines are directives"
        )
    silent = sum(not _sure(branch.holds) for branch in group.branches)
    if silent + (group.branches[-1].kind != "else") > 1:
        return (
            "two of its group's branches may leave no record of macros (only a #define or an "
            "#undef surely does: an #include may be skipped), so the build's record can't "
            "tell them apart"
        )
    return None


def lone(branch):
    """Where ``branch``'s condition is a probe alone (`__has_include(<x.h>)`, within
    parentheses or not): its kind; else None."""
    condition = branch.condition.strip()
    while condition.startswith("(") and _close(condition, 0) == len(condition) - 1:
        condition = condition[1:-1].strip()
    found = _LONE.match(condition)
    if found is None or _close(condition, found.end() - 1) != len(condition) - 1:
        return None
    return found[1]


def _close(text, at):
    depth = 0
    for index in range(at, len(text)):
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        if depth == 0:
            return index
    return -1
