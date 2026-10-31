"""A binary the project built (ADR 0008, phase 1): Peeky runs it as it is, where it is,
and reads only the compile units it can judge from the binary's own debug information.

A unit whose main file is under the target directory is judged when clang built it at
`-O0` without LTO, with DWARF 5 and `-grecord-command-line` (``native_units``), and its
line table names the unit as file 0 and holds an MD5 of every file. Each of those files
must match its MD5 before launch, or the binary is stale (PROVENANCE_CHANGED, by name);
and it is checked again before every operation, as a build's inputs are. With
``"optimized": true``, a unit whose only failing is its `-O` level or LTO is read as
Peeky's optimized builds are: every frame then carries the optimized warning.

Frames in any other unit show nothing (``not shown``, with why), and a breakpoint in a
file no unit Peeky reads has code in is refused (UNSUPPORTED, with the flag to add).

A unit's text is its preprocessing replayed by the config's compiler, with only the
recorded options that choose what the preprocessor reads or defines
(native_units.replay_arguments), `--no-default-config` and an environment of Peeky's
own. Every file the replay reads must be one the unit's line table names, with its MD5:
on Linux, `-fdebug-macro` names a header of macros alone. Where a unit's text can't be
read, the rules that need it withhold, saying why. Any call may return twice where a
unit isn't read, is assembly, or its text holds `asm` or can't be read, as the program's
own assembly can (MicroPython's `nlr_push`), unless the config names the functions that
do (``returns_twice``, a trusted claim).
"""

import contextlib
import hashlib
import json
import os
import posixpath
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

from peeky import native_startup
from peeky.adapters import PythonAdapter
from peeky.assembler import _C_KEYWORD
from peeky.build import (
    INHERITED_ENV,
    FileIdentity,
    ToolIdentity,
    UnreadDependencies,
    dependency_paths,
    jobs,
    private_base,
)
from peeky.c_source import (
    _RETURNS_TWICE,
    RETURNS_TWICE_NAMES,
    Preprocessed,
    Statements,
    blank,
    macro_hazards,
    normalized,
    optimizes,
    returning_twice,
)
from peeky.digest import sha256_file
from peeky.errors import Refused
from peeky.native import (
    _FOREIGN_TAGS,
    MAX_LINE_TEXT,
    MAX_STATEMENT_SOURCE,
    MAX_UNIT,
    VERSION_TIMEOUT,
    CodeIndex,
    NativeSession,
    NativeTarget,
    ValueNote,
    Withholding,
    _foreign,
    _metadata,
    _remaining,
    _symbol_text,
    _tool,
    elf_executable_ranges,
    has_nonlocal_jumps,
    mentions_nonlocal_jumps,
    module_identity,
    parse_lines,
    read_line_table,
    read_program_symbols,
)
from peeky.native_units import (
    LineFiles,
    NotRecorded,
    compiler_of,
    optimization,
    read_units,
    recorded_command,
    replay_arguments,
)
from peeky.processes import without_credentials

# Options that change what a unit's code computes. Peeky's proofs aren't checked under
# them, so a unit built with any is never read, opted in to optimized code or not: each
# of -ffast-math's own (clang's UsersManual), and those other options imply (``changes``).
# -fno-trapping-math is clang's default, and changes no result.
SEMANTICS = frozenset(
    """-ffast-math -ffinite-math-only -fno-honor-nans -fno-honor-infinities
    -funsafe-math-optimizations -fassociative-math -freciprocal-math -fapprox-func
    -fno-signed-zeros -fcx-limited-range -fcx-fortran-rules -ffp-contract=fast
    -ffp-contract=fast-honor-pragmas -mrecip""".split()
)
# An option of another name that implies some of them: what it means, as a phrase.
_IMPLIES = {
    "-ffp-model=fast": "implies -ffast-math's options",
    "-ffp-model=aggressive": "implies -ffast-math",
    "-Ofast": "implies -ffast-math",
}


# An -mrecip= item clang reads: an operation, negated or not, with a refinement count.
_RECIP_ITEM = re.compile(r"(!?)(?:all|default|(?:vec-)?(?:div|sqrt)[df]?)(?::[0-9]+)?")


def _reciprocals(value):
    """What `-mrecip=` ``value`` does: None where it estimates nothing (`none`, or each
    item negated), "estimates" where it may, "unclassified" for a value Peeky doesn't
    read."""
    if value == "none":
        return None
    items = [_RECIP_ITEM.fullmatch(item) for item in value.split(",")]
    if not all(items):
        return "unclassified"
    return None if all(item[1] for item in items) else "estimates"


def changes(word):
    """Why ``word`` (a recorded option) leaves its unit unjudged, as a refusal, or None:
    an option of SEMANTICS, one implying some (``_IMPLIES``), reciprocal estimates
    (`-mrecip`, `-mrecip=` but `none`), denormals flushed (`-fdenormal-fp-math=` but
    `ieee`), a sanitizer's instrumentation, which Peeky's proofs aren't checked under; or
    an `-mrecip=` value Peeky hasn't classified."""
    how = None
    if word in SEMANTICS:
        how = ""
    elif word in _IMPLIES:
        how = _IMPLIES[word]
    elif word.startswith("-mrecip="):
        found = _reciprocals(word.removeprefix("-mrecip="))
        if found == "unclassified":
            return (
                f"built with {word}, whose value Peeky hasn't classified (its recorded "
                "flags): rebuild without it"
            )
        how = "estimates reciprocals" if found else None
    elif re.fullmatch(r"-fdenormal-fp-math(?:-f32)?=(?!ieee(?:,ieee)?$).*", word):
        how = "flushes denormals"
    elif word.startswith("-fsanitize="):
        how = "instruments the code"
    if how is None:
        return None
    said = f", which {how}" if how else ""
    return (
        f"built with {word}{said}, under which Peeky's proofs aren't checked (its recorded "
        "flags): rebuild without it"
    )


# The most files the units Peeky reads may name (each hashed, and checked before every
# operation), and the largest one.
MAX_UNIT_FILES = 16384
MAX_UNIT_FILE = 256 * 1024 * 1024
# Where the reasons a unit isn't read are listed together, at most this many.
LISTED_REASONS = 3
# The `-O` levels, least first: a session reads as its highest.
_LEVELS = ("0", "g", "1", "s", "z", "2", "3")
# What the text rules say where they can't read a unit's text.
TEXT_UNREAD = "Peeky can't read {unit} as compiled: {why}"
JUDGED, OPTIMIZED, NOT_JUDGED = "judged", "optimized", "not judged"
# How many replays run at once, at most (each may hold a unit's text, up to MAX_UNIT).
MAX_REPLAYS = 4
# The least time left of the launch's deadline for another replay to start.
REPLAY_MARGIN = 2.0
# The most diagnostics a replay may write before it is ended.
MAX_REPLAY_ERRORS = 16000
# How many names a config's `returns_twice` may give.
MAX_RETURNS_TWICE = 100
# How long a worker takes, at most, to list a binary's inputs for a remote client; and how
# long another client's request waits for that listing before it is BUSY.
REMOTE_INPUTS_SECONDS = 40
# The last binary listed so: {(binary, root, size, time, inode, target): its inputs}.
_REMOTE_INPUTS = {}
# One listing at a time: it runs dsymutil and llvm-dwarfdump over the whole binary.
_LISTING = threading.Lock()


class BinaryChanged(RuntimeError):
    """A file the binary's line table names, or the binary, isn't as it was built: the
    binary is stale. PROVENANCE_CHANGED, refused before anything runs."""

    before_launch = True


class BinaryRefused(ValueError):
    """What Peeky doesn't read of a binary, refused before anything runs: UNSUPPORTED by
    its message (errors._RULES), which says what to rebuild with."""

    before_launch = True


def _refused(code, message):
    if code == "UNSUPPORTED":
        return BinaryRefused(message)
    refused = Refused(code, message)
    refused.before_launch = True
    return refused


# The keys a binary target's config may hold (BinaryTarget's fields).
BINARY_KEYS = frozenset(
    {
        "binary",
        "language",
        "compiler",
        "adapter",
        "dwarfdump",
        "dsymutil",
        "arguments",
        "optimized",
        "returns_twice",
    }
)


def binary_name(root, binary):
    """``binary``, a config's or `--binary`'s, by its name relative to ``root``, as it is
    written: no link is followed, and the file needn't exist on this host (a remote
    client's). INVALID_REQUEST where the name is outside ``root``."""
    root = Path(root).resolve(strict=True)
    given = Path(binary)
    path = Path(os.path.normpath(given if given.is_absolute() else root / given))
    if not path.is_relative_to(root) or path == root:
        raise _refused(
            "INVALID_REQUEST",
            f"The binary {binary} is outside the target directory, {root}: give the "
            "directory its sources are under as the target",
        )
    return path.relative_to(root).as_posix()


@dataclass(frozen=True)
class Judged:
    """One unit as Peeky reads it. ``name``: its main file, under the root where it is
    there. ``status``: JUDGED, OPTIMIZED (read as an optimized build, opted in) or
    NOT_JUDGED; ``why``: why it isn't judged, as a phrase (`built with -O2 (its recorded
    flags)`). ``level``: its `-O` level where read (`0`, `2`), ``lto`` whether it asks
    for LTO. ``files``: ((absolute path, MD5), …) of every file its line table names,
    where it is read. ``language``: `c` or `cpp`, where it is either."""

    unit: object
    name: str
    status: str
    why: str | None = None
    level: str | None = None
    lto: bool = False
    files: tuple = ()
    language: str | None = None
    # Its recorded words (recorded_command), where read: its text's replay takes some.
    words: tuple = ()

    @property
    def read(self):
        return self.status != NOT_JUDGED

    def describe(self):
        if self.status == JUDGED:
            return {"unit": self.name, "judged": True}
        return {"unit": self.name, "judged": False, "why": self.why, "read": self.read}


def _language(unit):
    found = re.fullmatch(r"DW_LANG_(C_plus_plus\w*|C\d*)", unit.language or "")
    if not found:
        return None
    return "cpp" if found[1].startswith("C_plus_plus") else "c"


def _no_md5s(table, language):
    """What a table without MD5s shows, and the likely remedies, as likely."""
    stdin = any(entry.name == "<stdin>" and not entry.checksum for entry in table.files.values())
    said = "its line table records no MD5s"
    if stdin:
        said += ", and names <stdin>, which clang adds for an inheriting constructor"
    remedies = "a #line naming another file, as bison and flex write (bison -l, flex -L)"
    if language == "cpp":
        remedies += ", or libc++'s system-header pragma (-D_LIBCPP_HAS_NO_PRAGMA_SYSTEM_HEADER)"
    return f"{said}: the likely cause is {remedies}"


def judge(unit, tables, version, root, optimized=False):
    """``unit`` (a native_units.Unit) as Peeky reads it: a Judged. ``tables``: the line
    tables' headers by offset (native_units.LineFiles); ``version``: the first line of
    the config's compiler's `--version`; ``root``: the target directory, resolved;
    ``optimized``: the config's opt-in. Nothing is read from disk but where paths lead:
    whether each file still has its MD5 is checked apart (``check_files``)."""
    main = posixpath.join(unit.comp_dir or "", unit.name or "")
    name = unit.name or f"the unit at 0x{unit.offset:08x}"

    def refused(why, shown=name):
        return Judged(unit, shown, NOT_JUDGED, why)

    if not unit.name or not posixpath.isabs(main):
        return refused(
            "its file has no absolute path (-fdebug-compilation-dir): rebuild without it"
        )
    resolved = Path(os.path.realpath(main))
    if not resolved.is_relative_to(root):
        return refused("its file is outside the target directory", main)
    name = resolved.relative_to(root).as_posix()
    language = _language(unit)
    if language is None:
        what = (unit.language or "none").removeprefix("DW_LANG_")
        return refused(f"its language is {what}: Peeky reads C and C++ units")
    family = compiler_of(unit.producer)
    if family == "gcc":
        return refused(
            "built by GCC, which writes no checksum of its files: rebuild with clang "
            "(CC=clang CXX=clang++)"
        )
    if family != "clang":
        return refused(f"built by {unit.producer or 'an unnamed compiler'}: rebuild with clang")
    if unit.version < 5:
        return refused(
            f"its debug information is DWARF {unit.version}, which records no checksum of "
            "its files: rebuild with -gdwarf-5"
        )
    try:
        words = recorded_command(unit, version)
    except NotRecorded as exc:
        why = str(exc)
        if why.startswith("built by "):
            why += ": set the config's compiler to the clang that built it"
        return refused(why)
    for word in words[1:]:
        if (why := changes(word)) is not None:
            return refused(why)
    try:
        level, lto = optimization(words)
    except NotRecorded as exc:
        return refused(str(exc))
    if level == "fast":
        # An alias -Xclang passed on, which the words above don't hold.
        return refused(
            "built with -Ofast, which implies -ffast-math, under which Peeky's proofs "
            "aren't checked (its recorded flags): rebuild without it"
        )
    status, why = JUDGED, None
    if level != "0" or lto:
        built = " ".join(([f"-O{level}"] if level != "0" else []) + (["-flto"] if lto else []))
        why = f"built with {built} (its recorded flags)"
        if not optimized:
            return refused(
                f'{why}: rebuild it with -O0 and without -flto, or set "optimized": true '
                "in the config to read it as an optimized build"
            )
        status = OPTIMIZED
    table = tables.get(unit.stmt_list)
    if table is None:
        return refused("it has no line table")
    if not table.checksummed():
        return refused(_no_md5s(table, language))
    if differs := table.differs(unit):
        return refused(differs)
    files = []
    for index, entry in sorted(table.files.items()):
        path = table.path(index, unit.comp_dir)
        if path is None:
            return refused(
                f"its line table names {entry.name} with no absolute path "
                "(-fdebug-compilation-dir): rebuild without it"
            )
        # As the line tables' rows are read (_LineParser): links resolved.
        files.append((os.path.realpath(path), entry.checksum))
    return Judged(unit, name, status, why, level, lto, tuple(files), language, tuple(words))


def _hashes(path):
    """(MD5, SHA-256) of the file at ``path``, from one read of it, or None where it is
    no regular file of at most MAX_UNIT_FILE bytes."""
    try:
        with open(path, "rb") as stream:
            if not os.path.isfile(path) or os.fstat(stream.fileno()).st_size > MAX_UNIT_FILE:
                return None
            md5 = hashlib.new("md5", usedforsecurity=False)
            sha = hashlib.sha256()
            while chunk := stream.read(1 << 20):
                md5.update(chunk)
                sha.update(chunk)
    except OSError:
        return None
    return md5.hexdigest(), sha.hexdigest()


def check_files(units, root):
    """{absolute path: SHA-256} of every file the units read name, each checked against
    the MD5 its line table records. One that differs or is gone makes the binary stale:
    BinaryChanged, naming each (at most LISTED_REASONS)."""
    wanted = {}
    for judged in units:
        for path, md5 in judged.files:
            if wanted.setdefault(path, md5) != md5:
                raise BinaryChanged(
                    f"Provenance: two of the binary's units recorded different MD5s for "
                    f"{_shown(path, root)}: it changed between their compiles; rebuild the "
                    "binary"
                )
    if len(wanted) > MAX_UNIT_FILES:
        raise _refused(
            "BUDGET_EXHAUSTED",
            f"The units Peeky would read name {len(wanted):,} files, and it reads at most "
            f"{MAX_UNIT_FILES:,}",
        )
    found, stale = {}, []
    for path, md5 in sorted(wanted.items()):
        hashes = _hashes(path)
        if hashes is None:
            stale.append(f"{_shown(path, root)} is gone or unreadable")
        elif hashes[0] != md5:
            stale.append(f"{_shown(path, root)} changed")
        else:
            found[path] = hashes[1]
    if stale:
        listed = "; ".join(stale[:LISTED_REASONS])
        more = f" (and {len(stale) - LISTED_REASONS} more)" if len(stale) > LISTED_REASONS else ""
        raise BinaryChanged(
            f"Provenance: the binary is stale: {listed}{more}, since it was built (its line "
            "tables record each file's MD5). Rebuild it (make), then probe again"
        )
    return found


@dataclass(frozen=True)
class UnitText:
    """A read unit's text, as the config's compiler preprocesses it again: its
    c_source.Preprocessed and Statements, whether it may optimize a function beyond its
    level (c_source.optimizes), why its text may return twice by assembly (``asm``:
    asm_uses', with where, or None), and ``reads``, the files under the root its
    preprocessing reads (relative)."""

    preprocessed: object
    statements: object
    optimizes: bool
    asm: bool
    reads: tuple
    cpp: bool


# What asks the preprocessor whether a file exists (or may be embedded), and `#embed`,
# which reads a file no line table names.
_PROBE = re.compile(r"(?<![\w$])(__has_include_next|__has_include|__has_embed)(?![\w$])")
_EMBED = re.compile(r"^[ \t]*#[ \t]*embed(?![\w$])", re.M)
_DIRECTIVE = re.compile(r"[ \t]*#[ \t]*(\w*)")
_DEFINE_NAME = re.compile(r"[ \t]*#[ \t]*define[ \t]+(\w+)")
_DEFINED_BEFORE = re.compile(r"defined[ \t]*\(?[ \t]*$")
_OPERAND = re.compile(r"(?:[ \t]|\\\n)*\(")
_GAP = re.compile(r"(?:[ \t]|\\\n)*")


@dataclass(frozen=True)
class Probe:
    """One availability probe in a file, or an `#embed`: its line, its kind
    (`__has_include`, `__has_include_next`, `__has_embed`, `#embed`), its operand where
    it is written out (``angled``: `<x.h>`; else `"x.h"`; ``header`` None where it isn't),
    and the macro whose body holds it (``macro``; "" where Peeky can't tell which)."""

    line: int
    kind: str
    angled: bool
    header: str | None
    macro: str | None


def probes(text):
    """The availability probes of a file's ``text``, and its `#embed`s, outside comments
    and literals. One that only asks whether the name exists (`defined(__has_include)`,
    `#ifdef`, `#undef`), or defines it (`#define __has_include(x) 0`), is none."""
    import bisect

    original = normalized(text)
    blanked = blank(original)
    starts = [0] + [found.end() for found in re.finditer("\n", blanked)]
    found = []
    for match in _PROBE.finditer(blanked):
        at = match.start()
        index = bisect.bisect_right(starts, at) - 1
        # A line continues after a backslash at its end.
        while index > 0 and blanked[starts[index] - 2 : starts[index] - 1] == "\\":
            index -= 1
        begin = starts[index]
        directive = _DIRECTIVE.match(blanked, begin)
        name = directive[1] if directive else None
        if name in ("ifdef", "ifndef", "elifdef", "elifndef", "undef"):
            continue
        if _DEFINED_BEFORE.search(blanked, begin, at):
            continue
        macro = None
        if name == "define":
            defined = _DEFINE_NAME.match(blanked, begin)
            if defined is not None and defined.start(1) == at:
                continue
            macro = defined[1] if defined is not None else ""
        angled, header = False, None
        if (operand := _OPERAND.match(blanked, match.end())) is not None:
            # In the text itself: a literal's inside is blank.
            start = _GAP.match(original, operand.end()).end()
            close = re.compile(r'<([^>\n]*)>|"([^"\n]*)"').match(original, start)
            if close is not None:
                angled = close[1] is not None
                header = close[1] if angled else close[2]
        line = bisect.bisect_right(starts, at)
        found.append(Probe(line, match[1], angled, header, macro))
    for match in _EMBED.finditer(blanked):
        found.append(Probe(bisect.bisect_right(starts, match.start()), "#embed", False, None, None))
    return found


_PROBE_NAMES = ("__has_include_next", "__has_include", "__has_embed")
# Every three characters of a probe's name: any longer part holds one.
_PROBE_PIECES = tuple(
    dict.fromkeys(name[at : at + 3] for name in _PROBE_NAMES for at in range(len(name) - 2))
)


def _defined_probe(arguments):
    """Why a macro the recorded command defines (`-D`) may ask whether a file exists, or
    None: its value names a probe, may paste one together (`#`, `##`), or holds any three
    characters of a probe's name, which another macro could paste to the rest."""
    values = [
        arguments[index + 1] if word == "-D" else word[2:]
        for index, word in enumerate(arguments)
        if (word == "-D" and index + 1 < len(arguments)) or (word.startswith("-D") and word != "-D")
    ]
    for value in values:
        name, _, body = value.partition("=")
        name = name.split("(", 1)[0]
        said = f"its command defines {name} as {body[:80]}"
        # A literal's inside is no token, and can't be pasted into one.
        code = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', " ", body)
        if any(probe in code for probe in _PROBE_NAMES):
            return (
                f"{said}, which asks whether a file exists: the build may have answered otherwise"
            )
        if "#" in code:
            return f"{said}, which may paste together a probe such as __has_include"
        piece = next((part for part in _PROBE_PIECES if part in code), None)
        if piece is not None:
            return (
                f"{said}, which holds {piece}, part of a probe's name (__has_include) that a "
                "macro may paste together"
            )
    return None


def _search_lists(said):
    """The header search lists clang's `-v` writes: (quoted, angled), each a list of
    (directory, whether Peeky can look in it: not a framework directory or header map),
    or None where it wrote none."""
    quoted, angled, current = [], [], None
    for line in said.split("\n"):
        if line.startswith('#include "..." search starts here:'):
            current = quoted
        elif line.startswith("#include <...> search starts here:"):
            current = angled
        elif line.startswith("End of search list."):
            return quoted, angled
        elif current is not None and line.startswith(" "):
            entry = line[1:]
            plain = not entry.endswith((" (framework directory)", " (headermap)"))
            current.append((entry.rsplit(" (", 1)[0] if not plain else entry, plain))
    return None


def _found(probe, includer, lists):
    """The file a probe of ``includer`` (its path as the compiler opened it) finds, looked
    up as clang does on its search ``lists``; None where it finds none, or where Peeky
    can't tell where its search starts. Never a file clang's search would not reach: a
    `__has_include_next` searches on after the last directory holding the includer."""
    header = probe.header
    if posixpath.isabs(header):
        return header if os.path.isfile(header) else None
    quoted, angled = lists
    combined = [*quoted, *angled]
    first = len(quoted) if probe.angled else 0
    if probe.kind == "__has_include_next":
        real = os.path.realpath(includer)
        holding = [
            index
            for index, (directory, plain) in enumerate(combined)
            if plain and real.startswith(os.path.realpath(directory).rstrip("/") + "/")
        ]
        if not holding:
            return None
        first = max(first, holding[-1] + 1)
        directories = [directory for directory, plain in combined[first:] if plain]
    else:
        directories = [directory for directory, plain in combined[first:] if plain]
        if not probe.angled:
            directories.insert(0, posixpath.dirname(includer))
    for directory in directories:
        if os.path.isfile(path := posixpath.join(directory, header)):
            return path
    return None


def _unproven_probe(read, lists, root, cache):
    """Why a probe the replay's inputs (``read``: their paths as the compiler opened them)
    hold may have been answered otherwise when the program was built, or None.

    A probe that finds a file now (``_found``, never further than clang's own search)
    is proven, or never ran: where it runs, clang lists the file it finds among the
    replay's dependencies, each of which the unit's line table must name. So the build
    read that file: it existed then, and the build's search held the replay's (its
    environment only adds directories), so the build found it too. A probe that finds
    none may have been answered otherwise: the build's environment may have added a
    directory holding the file, or the file may since be gone. Peeky can't tell whether
    it ran (its group may have been skipped), so it counts. A probe in a macro counts
    where any input names the macro."""
    contents = {}
    for path in read:
        real = os.path.realpath(path)
        if real not in cache:
            data = Path(real).read_bytes()
            listed = []
            if b"__has_" in data or b"embed" in data:
                listed = probes(data.decode("utf-8", "replace"))
            cache[real] = (data, listed)
        contents[str(path)] = cache[real]
    for path, (data, listed) in contents.items():
        for probe in listed:
            where = f"{probe.kind}, in {_shown(path, root)}:{probe.line}"
            if probe.macro:
                token = probe.macro.encode()
                if data.count(token) < 2 and not any(
                    token in other for name, (other, _) in contents.items() if name != path
                ):
                    continue
            if probe.kind in ("#embed", "__has_embed"):
                return (
                    f"its preprocessing reads a file to embed ({where}), which no line table names"
                )
            if probe.header is None:
                return (
                    f"its preprocessing asks whether a file exists ({where}), naming it in a "
                    "way Peeky doesn't read"
                )
            asked = f"<{probe.header}>" if probe.angled else f'"{probe.header}"'
            if probe.macro is not None and (not probe.angled or probe.kind != "__has_include"):
                # Where a macro's probe searches from depends on where it is expanded.
                return (
                    f"its preprocessing asks whether {asked} exists ({where}), in a macro, "
                    "whose search Peeky can't follow"
                )
            if lists is None:
                return (
                    f"its preprocessing asks whether {asked} exists ({where}), and Peeky "
                    "can't read the compiler's search path"
                )
            if _found(probe, path, lists) is None:
                return (
                    f"its preprocessing asks whether {asked} exists ({where}), and finds "
                    "none: the build may have found one (its environment can add header "
                    "paths, and a file may since be gone)"
                )
    return None


_ASM_QUALIFIERS = frozenset(
    "volatile __volatile__ __volatile goto inline __inline __inline__".split()
)
# What a statement may follow: a `(` after one of these closes its condition.
_STATEMENT_HEADS = frozenset("if while for switch".split())
_STATEMENT_WORDS = frozenset("else do return case default".split())
_WORD = re.compile(r"[A-Za-z_$][\w$]*")
_SPACE = re.compile(r"\s*")


def _closing(blanked, at):
    """The index of the bracket matching ``blanked[at]`` (`(` forward, `)` back), or -1."""
    step, opening = (1, blanked[at]) if blanked[at] == "(" else (-1, blanked[at])
    closing = ")" if opening == "(" else "("
    depth = 0
    while 0 <= at < len(blanked):
        if blanked[at] == opening:
            depth += 1
        elif blanked[at] == closing:
            depth -= 1
            if depth == 0:
                return at
        at += step
    return -1


def _before(blanked, at):
    """The last token before ``at``: (its start, its text), or (-1, "")."""
    at -= 1
    while at >= 0 and blanked[at].isspace():
        at -= 1
    if at < 0:
        return -1, ""
    if blanked[at] == "_" or blanked[at] == "$" or blanked[at].isalnum():
        end = at + 1
        while at >= 0 and (blanked[at] == "_" or blanked[at] == "$" or blanked[at].isalnum()):
            at -= 1
        return at + 1, blanked[at + 1 : end]
    return at, blanked[at]


def asm_uses(text, cpp):
    """What of the preprocessed ``text`` (all of it, headers too) may make a call return
    twice by assembly: an `asm` statement or one at file scope, whose code Peeky can't
    read, or an asm label giving a declaration a symbol of RETURNS_TWICE_NAMES' family.
    Each as (its offset, a phrase); a label giving another symbol (glibc's and Apple's
    `__asm__("" "fopen")`) is none. Read cautiously: where Peeky can't tell a label, it
    counts as a statement."""
    original = normalized(text)
    blanked = blank(original, cpp)
    found = []
    for keyword in _C_KEYWORD.finditer(blanked):
        at = _SPACE.match(blanked, keyword.end()).end()
        token = _WORD.match(blanked, at)
        qualified = False
        while token is not None and token[0] in _ASM_QUALIFIERS:
            qualified = True
            at = _SPACE.match(blanked, token.end()).end()
            token = _WORD.match(blanked, at)
        close = _closing(blanked, at) if blanked.startswith("(", at) else -1
        label = (
            not qualified
            and close > at
            and not blanked[at + 1 : close].strip()
            and _declares(blanked, keyword.start())
        )
        if not label:
            found.append((keyword.start(), "holds asm"))
            continue
        symbol = "".join(re.findall(r'"([^"\n]*)"', original[at + 1 : close]))
        if any(_RETURNS_TWICE.fullmatch(name) for name in (symbol, symbol[1:]) if name):
            found.append((keyword.start(), f"gives a function the symbol {symbol} in an asm label"))
    return found


def _declares(blanked, at):
    """Whether the `asm` at ``at`` follows a declarator, as an asm label does: a name (not
    `else`, `do`, ...), a `]`, or a `)` closing a declarator's parameters, an attribute or
    a `noexcept`, not an `if`'s or a loop's condition."""
    start, token = _before(blanked, at)
    if token == "]":
        return True
    if token == ")":
        opening = _closing(blanked, start)
        if opening <= 0:
            return False
        _, head = _before(blanked, opening)
        return bool(_WORD.fullmatch(head)) and head not in _STATEMENT_HEADS
    return bool(_WORD.fullmatch(token)) and token not in _STATEMENT_WORDS


def replay_environment(scratch):
    """The environment of a unit's replay: Peeky's own. None of what may add options or
    header paths (CCC_*, CL, _CL_, CPATH, C_INCLUDE_PATH, CPLUS_INCLUDE_PATH,
    OBJC_INCLUDE_PATH, SDKROOT), which no unit records; its home and temporary
    directory Peeky's."""
    env = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
    env.update(HOME=str(scratch), TMPDIR=str(scratch), LC_ALL="C")
    return without_credentials(env)


def _marked(text, at, root):
    """Where offset ``at`` of preprocessed ``text`` comes from, by its line markers:
    `file:line`."""
    import bisect

    starts = [0] + [found.end() for found in re.finditer("\n", text[:at])]
    line = len(starts)
    for marker in reversed(list(re.finditer(r'^# (\d+) "((?:[^"\\]|\\.)*)"', text[:at], re.M))):
        number = int(marker[1]) + line - 1 - bisect.bisect_right(starts, marker.start())
        return f"{_shown(marker[2], root)}:{number}"
    return f"line {line}"


def replay(judged, compiler, env, scratch, root, timeout, probed=None):
    """The UnitText of ``judged`` (a read unit), or why it can't be read: its
    preprocessing replayed (`-E`, with its dependencies) by ``compiler``, in its
    compilation directory, with the options replay_arguments passes and
    `--no-default-config`. Every file it reads must be one the unit's line table names
    (and so checked against its MD5): a header of macros alone is named only with
    `-fdebug-macro`."""
    unit, name = judged.unit, judged.name

    def unread(why):
        return TEXT_UNREAD.format(unit=name, why=why)

    comp_dir = unit.comp_dir
    main = posixpath.join(comp_dir, unit.name)
    try:
        arguments = replay_arguments(list(judged.words), comp_dir, main)
    except NotRecorded as exc:
        return unread(str(exc))
    if (defined := _defined_probe(arguments)) is not None:
        return unread(defined)
    if unit.sysroot and not any(
        word in ("-isysroot", "--sysroot") or word.startswith("--sysroot=") for word in arguments
    ):
        # Apple's clang records the SDK it compiled against apart from its options.
        arguments = ["-isysroot", unit.sysroot, *arguments]
    if not os.path.isdir(comp_dir):
        return unread(f"its compilation directory, {comp_dir}, is gone")
    work = Path(tempfile.mkdtemp(prefix="unit-", dir=scratch))
    try:
        output, depends, errors = work / "unit.i", work / "unit.d", work / "errors.txt"
        argv = [compiler, "--no-default-config", *arguments[:-1], "-E", "-v", "-MD", "-MF"]
        argv += [str(depends), arguments[-1], "-o", str(output)]
        with errors.open("wb") as stderr:
            process = subprocess.Popen(
                argv,
                cwd=comp_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stderr,
                stderr=stderr,
                start_new_session=True,
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
                return unread(f"its preprocessing took over {timeout:.0f} s")
        with errors.open("rb") as diagnostics:
            said = diagnostics.read(MAX_REPLAY_ERRORS).decode("utf-8", "replace")
        if process.returncode:
            first = next((line for line in said.split("\n") if "error" in line), said)
            return unread(f"its preprocessing failed: {' '.join(first.split())[:200]}")
        if not output.is_file() or output.stat().st_size > MAX_UNIT:
            return unread(f"its preprocessed text is over {MAX_UNIT >> 20} MiB")
        try:
            read = dependency_paths(depends.read_text(errors="replace"), comp_dir, "make")
        except (OSError, UnreadDependencies) as exc:
            return unread(f"its dependencies can't be read ({exc})")
        named = {path for path, _ in judged.files}
        # Apple's driver reads the SDK's own settings, no header: its version, which the
        # recorded -mmacos-version-min holds, for the macros.
        roots = [
            value
            for option, value in zip(arguments, arguments[1:])
            if option in ("-isysroot", "--sysroot")
        ]
        roots += [word.split("=", 1)[1] for word in arguments if word.startswith("--sysroot=")]
        settings = {
            os.path.realpath(posixpath.join(comp_dir, value, "SDKSettings.json")) for value in roots
        }
        reads = []
        for path in read:
            real = os.path.realpath(path)
            if real in settings:
                continue
            if real not in named:
                return unread(
                    f"its preprocessing reads {_shown(real, root)}, which its line table "
                    "doesn't name (-fdebug-macro names each header, on Linux)"
                )
            if Path(real).is_relative_to(root):
                reads.append(Path(real).relative_to(root).as_posix())
        inputs = [path for path in read if os.path.realpath(path) not in settings]
        unproven = _unproven_probe(
            inputs, _search_lists(said), root, {} if probed is None else probed
        )
        if unproven is not None:
            return unread(unproven)
        own = set(reads)

        def program(path):
            if path.startswith("<"):
                return None
            real = Path(os.path.realpath(posixpath.join(comp_dir, path)))
            if not real.is_relative_to(root):
                return None
            shown = real.relative_to(root).as_posix()
            return shown if shown in own else None

        cpp = judged.language == "cpp"
        text = output.read_bytes()
        with output.open("rb") as lines:
            preprocessed = Preprocessed(lines, program)
        statements = Statements(preprocessed.text, cpp, trigraphs=False)
        decoded = text.decode("utf-8", "replace")
        uses = asm_uses(decoded, cpp)
        asm = None
        if uses:
            at, what = uses[0]
            asm = f"{name}'s text {what} ({_marked(decoded, at, root)})"
        return UnitText(
            preprocessed,
            statements,
            optimizes(text, cpp),
            asm,
            tuple(sorted(own)),
            cpp,
        )
    except OSError as exc:
        return unread(f"its preprocessing couldn't run ({exc.strerror or exc})")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def replay_units(read, compiler, scratch, root, deadline):
    """{unit name: UnitText, or why its text can't be read} of each unit read, replayed
    at most MAX_REPLAYS at once, while the launch's deadline leaves REPLAY_MARGIN
    seconds."""
    import time

    env = replay_environment(scratch)

    def one(judged, probed):
        left = 15 if deadline is None else deadline - time.monotonic() - REPLAY_MARGIN
        if left <= 0:
            return TEXT_UNREAD.format(
                unit=judged.name, why="there was no time left for it before the launch"
            )
        return replay(judged, compiler, env, scratch, root, min(15, left), probed)

    # Peeky reads a unit's text by its file's name: units built from one file with other
    # commands (`-D`s) would have one's text stand for all.
    groups = {}
    for each in read:
        groups.setdefault(each.name, []).append(each)
    texts, replayed = {}, []
    for name, units in groups.items():
        alike = {_built_as(each) for each in units}
        if len(alike) > 1:
            texts[name] = TEXT_UNREAD.format(
                unit=name,
                why=f"{len(units)} units are built from it, with different commands, and "
                "Peeky reads a unit's text by its file's name",
            )
        else:
            replayed.append(units[0])
    probed = {}
    workers = max(1, min(MAX_REPLAYS, jobs(), len(replayed) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        texts.update(
            zip(
                (each.name for each in replayed), pool.map(lambda each: one(each, probed), replayed)
            )
        )
    return texts


def _built_as(judged):
    """What a unit's text depends on, of what it records: its directory, its file, its
    command, its language, its SDK and each file its line table names, with its MD5."""
    unit = judged.unit
    return (
        unit.comp_dir,
        unit.name,
        tuple(judged.words),
        judged.language,
        unit.sysroot,
        frozenset(judged.files),
    )


# The code the toolchain links into a program with no debug information of its own:
# glibc's crt1.o, crti.o and crtn.o and GCC's crtbegin.o and crtend.o (seen in clang
# 18's programs on Linux; glibc before 2.34 adds __libc_csu_*, a static PIE
# _dl_relocate_static_pie), and the terminate handler clang emits in a C++ unit with none
# (on Linux and in Apple clang 21's). By name alone only where C and C++ reserve the name
# for the implementation (it begins with `_`): a program defining one is outside what
# Peeky checks. Any other code no unit covers may return twice.
_RESERVED_STARTUP = frozenset(
    """_start _init _fini __do_global_dtors_aux __libc_csu_init __libc_csu_fini
    _dl_relocate_static_pie __clang_call_terminate""".split()
)
# libgcc's AArch64 atomics, called out of line (-moutline-atomics, Linux's default there).
_ATOMICS = re.compile(
    r"__aarch64_(?:cas|swp|ldadd|ldclr|ldeor|ldset)(?:1|2|4|8|16)_(?:relax|acq|rel|acq_rel|sync)"
)
# The toolchain's startup functions a program may name too (no name of them is
# reserved): each is the toolchain's only where its bytes are a startup object's that
# the config's compiler links (``toolchain_startup``).
_MATCHED_STARTUP = frozenset(
    {"deregister_tm_clones", "register_tm_clones", "frame_dummy", "init_have_lse_atomics"}
    | {"call_weak_fn"}
)


@dataclass(frozen=True)
class CodeSection:
    """A section of an executable holding code: its name, its addresses [low, high) and
    where its bytes are in the file."""

    name: str
    low: int
    high: int
    offset: int


@dataclass(frozen=True)
class CodeSymbol:
    """A symbol at code: its name (a Mach-O one without its leading `_`), its address,
    its size (an ELF symbol's, else 0), and whether it is local to its object."""

    name: str
    address: int
    size: int
    local: bool


@dataclass(frozen=True)
class CodeMap:
    """What code_symbols reads of an executable: its symbols at code (None where it has
    no symbol table) and its CodeSections."""

    symbols: list | None
    sections: list


# Sections of stubs the linker makes: never the program's code.
_TOOLCHAIN_SECTIONS = frozenset(
    {".plt", ".plt.got", ".plt.sec", ".iplt", "__stubs", "__auth_stubs", "__stub_helper"}
)
# Sections whose code crti.o begins and crtn.o ends, around what other objects add.
_FRAMED_SECTIONS = {".init": "_init", ".fini": "_fini"}


def code_symbols(binary):
    """The CodeMap of an ELF or 64-bit Mach-O executable: each symbol at code in its own
    sections (functions, and any symbol an assembler writes without a type: a label of
    hand-written assembly). ValueError where it can't be read."""
    with open(binary, "rb") as stream:
        data = stream.read(64)
        if data[:4] == b"\x7fELF":
            return _elf_symbols(stream, data)
        return _macho_symbols(stream, data)


def _elf_symbols(stream, header):
    if len(header) < 64 or header[4:6] != b"\x02\x01":
        raise ValueError("Expected a 64-bit little-endian ELF executable")
    size = os.fstat(stream.fileno()).st_size
    offset = struct.unpack_from("<Q", header, 40)[0]
    entry, count, names_at = struct.unpack_from("<HHH", header, 58)
    if entry != 64 or not 0 < count <= 65535 or names_at >= count or offset + count * 64 > size:
        raise ValueError("Invalid ELF section headers")
    stream.seek(offset)
    sections = [struct.unpack_from("<IIQQQQIIQQ", stream.read(64)) for _ in range(count)]

    def read(at, length, limit):
        if at + length > size or length > limit:
            raise ValueError("Invalid ELF section")
        stream.seek(at)
        return stream.read(length)

    def string(table, at):
        end = table.find(b"\0", at)
        return os.fsdecode(table[at:end]) if 0 <= at and end >= 0 else ""

    titles = read(sections[names_at][4], sections[names_at][5], 1 << 24)
    code = [
        CodeSection(string(titles, name), address, address + length, at)
        for name, kind, flags, address, at, length, *_ in sections
        if flags & 0x4 and kind == 1  # SHF_EXECINSTR, SHT_PROGBITS
    ]
    tables = [section for section in sections if section[1] == 2]  # SHT_SYMTAB
    if not tables:
        return CodeMap(None, code)
    _, _, _, _, table, length, link, _, _, width = tables[0]
    if width != 24 or link >= count:
        raise ValueError("Invalid ELF symbol table")
    symbols = read(table, length, MAX_SYMBOLS * 24)
    strings = read(sections[link][4], sections[link][5], 1 << 28)
    found = []
    for at in range(0, length - length % 24, 24):
        name, info, _, index, value, extent = struct.unpack_from("<IBBHQQ", symbols, at)
        if info & 0xF not in (0, 2, 10) or not 0 < index < count or not value:
            continue
        if not sections[index][2] & 0x4:  # SHF_EXECINSTR
            continue
        text = string(strings, name)
        # ARM's mapping symbols ($x, $d) and local labels mark no code of their own.
        if text and not text.startswith(("$", ".L")):
            found.append(CodeSymbol(text, value, extent, info >> 4 == 0))  # STB_LOCAL
    return CodeMap(found, code)


def _macho_symbols(stream, header):
    if len(header) < 32 or struct.unpack_from("<I", header)[0] != 0xFEEDFACF:
        raise ValueError("Expected a 64-bit Mach-O executable")
    size = os.fstat(stream.fileno()).st_size
    count, length = struct.unpack_from("<II", header, 16)
    if not 0 < count <= 4096 or not 0 < length <= 1 << 20:
        raise ValueError("Invalid Mach-O load commands")
    stream.seek(32)
    commands = stream.read(length)
    cursor, sections, table = 0, [], None
    for _ in range(count):
        if cursor + 8 > len(commands):
            raise ValueError("Truncated Mach-O load command")
        kind, extent = struct.unpack_from("<II", commands, cursor)
        if extent < 8 or cursor + extent > len(commands):
            raise ValueError("Invalid Mach-O load command size")
        if kind == 0x19 and extent >= 72:  # LC_SEGMENT_64
            number = struct.unpack_from("<I", commands, cursor + 64)[0]
            for index in range(number):
                at = cursor + 72 + index * 80
                if at + 80 > cursor + extent:
                    raise ValueError("Truncated Mach-O section")
                name = commands[at : at + 16].rstrip(b"\0").decode("latin-1")
                address, length_of, offset = struct.unpack_from("<QQI", commands, at + 32)
                flags = struct.unpack_from("<I", commands, at + 64)[0]
                # S_ATTR_PURE_INSTRUCTIONS or S_ATTR_SOME_INSTRUCTIONS: code.
                sections.append(
                    (CodeSection(name, address, address + length_of, offset), flags & 0x80000400)
                )
        elif kind == 0x2:  # LC_SYMTAB
            table = struct.unpack_from("<IIII", commands, cursor + 8)
        cursor += extent
    code = [section for section, is_code in sections if is_code]
    if table is None:
        return CodeMap(None, code)
    offset, symbols, strings, strings_size = table
    if symbols > MAX_SYMBOLS or offset + symbols * 16 > size or strings + strings_size > size:
        raise ValueError("Invalid Mach-O symbol table")
    stream.seek(offset)
    entries = stream.read(symbols * 16)
    stream.seek(strings)
    names = stream.read(strings_size)
    found = []
    for index in range(symbols):
        start, kind, number, _, value = struct.unpack_from("<IBBHQ", entries, index * 16)
        # Not a stab, and defined in a section (N_SECT) holding code, at its address.
        if kind & 0xE0 or kind & 0x0E != 0x0E or not 0 < number <= len(sections):
            continue
        section, is_code = sections[number - 1]
        if not is_code or not section.low <= value < section.high:
            continue
        end = names.find(b"\0", start)
        text = os.fsdecode(names[start:end]) if end >= 0 else ""
        if text and not text.startswith(("ltmp", "l_", "L")):
            found.append(CodeSymbol(text.removeprefix("_"), value, 0, not kind & 0x01))  # N_EXT
    return CodeMap(found, code)


# What fills the space between functions: zeros, AArch64's `nop`, x86's `nop`s and `int3`.
_AARCH64_PADDING = re.compile(rb"(?:\x1f\x20\x03\xd5|\0\0\0\0)*")
_X86_NOPS = (
    b"\x66\x2e\x0f\x1f\x84\x00\x00\x00\x00\x00",
    b"\x66\x0f\x1f\x84\x00\x00\x00\x00\x00",
    b"\x0f\x1f\x84\x00\x00\x00\x00\x00",
    b"\x0f\x1f\x80\x00\x00\x00\x00",
    b"\x66\x0f\x1f\x44\x00\x00",
    b"\x0f\x1f\x44\x00\x00",
    b"\x0f\x1f\x40\x00",
    b"\x0f\x1f\x00",
    b"\x66\x90",
    b"\x90",
    b"\xcc",
    b"\x66",
)


def _padded(data):
    """How many of ``data``'s first bytes are what a linker or assembler pads code with:
    AArch64's words, or x86's instructions."""
    words = _AARCH64_PADDING.match(data).end()
    if words == len(data):
        return words
    at = 0
    while at < len(data):
        if data[at] == 0:
            at += 1
            continue
        found = next((nop for nop in _X86_NOPS if data.startswith(nop, at)), None)
        if found is None:
            break
        at += len(found)
    return max(words, at)


def _padding(data):
    """Whether ``data`` is only what a linker or assembler pads code with."""
    return _padded(data) == len(data)


def _code_between(stream, section, low, high):
    """The address of the first byte of ``section`` in [low, high) that isn't padding, or
    None; read a MiB at a time."""
    stream.seek(section.offset + low - section.low)
    while low < high:
        data = stream.read(min(high - low, 1 << 20))
        if not data:
            return low
        padded = _padded(data)
        if padded < len(data):
            return low + padded
        low += len(data)
    return None


def uncovered_code(binary, units, startup=None):
    """What of ``binary``'s code no unit of its debug information covers (``units``:
    native_units.Unit, each with its code ranges), but the toolchain's own: each symbol
    there (hand-written assembly built without -g, a library linked without debug
    information), and code at an address no symbol names (a symbol table stripped of
    it), but what pads the space between functions. The toolchain's own: the linker's
    stubs (``_TOOLCHAIN_SECTIONS``); startup code by a name reserved to the
    implementation (``_RESERVED_STARTUP``, ``_ATOMICS``), to its size, or to the next
    symbol where it has none; and code whose bytes are a startup object's
    (``startup``: a callable giving toolchain_startup's), as other startup names and
    crti.o's and crtn.o's parts of `.init` and `.fini` must be. Each as a phrase, for a
    note; where the binary has no symbol table, or one Peeky can't read, that instead."""
    import bisect

    try:
        code = code_symbols(binary)
    except (OSError, ValueError, struct.error) as exc:
        return [
            f"Peeky can't read the binary's symbol table ({exc}), so it can't tell what code "
            "no unit covers"
        ]
    symbols, sections = code.symbols, code.sections
    if symbols is None:
        return ["the binary has no symbol table, so Peeky can't tell what code no unit covers"]
    covered = sorted(span for unit in units for span in unit.ranges)
    framed = {
        section.name: section
        for section in sections
        if section.name in _FRAMED_SECTIONS and section.high > section.low
    }
    matched, unframed, pieces, why = set(), {}, [], None
    if framed or any(symbol.name in _MATCHED_STARTUP for symbol in symbols):
        pieces, why = startup() if startup is not None else ([], "Peeky has no compiler to ask")
    image = None
    if pieces:
        try:
            image = native_startup.Image(binary)
        except (OSError, ValueError, struct.error) as exc:
            pieces, why = [], f"Peeky can't read the binary's sections ({exc})"
    for symbol in symbols:
        if symbol.name not in _MATCHED_STARTUP or symbol.address in matched:
            continue
        for piece in pieces:
            if symbol.name not in piece.symbols:
                continue
            base = symbol.address - piece.symbols[symbol.name]
            if native_startup.place(piece, base, image):
                covered.append((base, base + len(piece.data)))
                matched |= {base + at for at in piece.symbols.values()}
                break
    for name, section in framed.items():
        # crti.o begins the section and crtn.o ends it; others add what lies between.
        head = [piece for piece in pieces if piece.origin == "crti.o" and piece.section == name]
        tail = [piece for piece in pieces if piece.origin == "crtn.o" and piece.section == name]
        if not head or not tail:
            unframed[name] = why or "Peeky found no crti.o or crtn.o"
            continue
        start, end = len(head[0].data), len(tail[0].data)
        if native_startup.place(head[0], section.low, image):
            covered.append((section.low, section.low + start))
        if native_startup.place(tail[0], section.high - end, image):
            covered.append((section.high - end, section.high))

    def reserved(symbol):
        return symbol.name in _RESERVED_STARTUP or bool(_ATOMICS.fullmatch(symbol.name))

    starts = sorted(symbol.address for symbol in symbols)
    for symbol in symbols:
        section = next((each for each in sections if each.low <= symbol.address < each.high), None)
        if not reserved(symbol) or (section is not None and section.name in framed):
            continue
        # A startup symbol's code is its size, where its symbol gives one; else it runs to
        # the next symbol, or its section's end.
        at = bisect.bisect_right(starts, symbol.address)
        end = starts[at] if at < len(starts) else section.high if section else symbol.address
        end = symbol.address + symbol.size if symbol.size else end
        covered.append((symbol.address, min(end, section.high) if section else end))
    spans = []
    for low, high in sorted(covered):
        if spans and low <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], high))
        else:
            spans.append((low, high))
    lows = [low for low, _ in spans]

    def held(address):
        at = bisect.bisect_right(lows, address) - 1
        return at >= 0 and address < spans[at][1]

    named, found = [], []
    for symbol in symbols:
        if reserved(symbol) or symbol.address in matched or held(symbol.address):
            continue
        named.append(symbol)
        said = f"{symbol.name} is code no unit's debug information covers"
        if symbol.name not in _MATCHED_STARTUP:
            found.append(f"{said} (assembly built without -g?)")
            continue
        origins = sorted({piece.origin for piece in pieces if symbol.name in piece.symbols})
        if origins:
            found.append(
                f"{said}, and its bytes are none of the toolchain's ({', '.join(origins)})"
            )
        else:
            found.append(
                f"{said}, and Peeky found no toolchain startup object holding it "
                f"({why or 'none names it'})"
            )
    with open(binary, "rb") as stream:
        for section in sections:
            if section.name in _TOOLCHAIN_SECTIONS or section.high <= section.low:
                continue
            cursor = section.low
            for low, high in [*spans, (section.high, section.high)]:
                if high <= cursor:
                    continue
                gap_end = min(low, section.high)
                if cursor < gap_end:
                    inside = any(cursor <= symbol.address < gap_end for symbol in named)
                    at = None if inside else _code_between(stream, section, cursor, gap_end)
                    if at is not None and section.name in framed:
                        reason = unframed.get(section.name)
                        found.append(
                            f"the code at 0x{at:x}, in {section.name}, may be the program's: "
                            + (
                                f"Peeky can't compare it with crti.o's and crtn.o's ({reason})"
                                if reason
                                else "it isn't crti.o's or crtn.o's"
                            )
                        )
                    elif at is not None:
                        found.append(
                            f"the code at 0x{at:x} has no symbol, and no unit's debug "
                            "information covers it"
                        )
                cursor = max(cursor, high)
                if cursor >= section.high:
                    break
    return list(dict.fromkeys(found))


def any_call_returns_twice(judged, texts, uncovered=()):
    """Why any call may return twice, or None: code no unit covers (``uncovered``:
    uncovered_code's), a unit Peeky doesn't read, which may be assembly or hold `asm`
    (MicroPython's `nlr_push`), or one whose text can't be read or holds `asm`. Peeky
    finds such calls only by name otherwise (setjmp's)."""
    found = list(uncovered)
    for each in judged:
        text = texts.get(each.name) if each.read else None
        if not each.read:
            found.append(f"{each.name} is a unit Peeky doesn't read")
        elif not isinstance(text, UnitText):
            found.append(f"Peeky can't read {each.name}'s text")
        elif text.asm:
            found.append(text.asm)
    if not found:
        return None
    more = f", and {len(found) - 1} more" if len(found) > 1 else ""
    return f"{found[0]}{more}"


def _shown(path, root):
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return path


def _version_line(compiler):
    """The first line ``compiler --version`` writes, as clang writes it in each unit's
    `DW_AT_producer`; ValueError if it doesn't answer within VERSION_TIMEOUT."""
    process = subprocess.Popen(
        [compiler, "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="replace",
        env={**without_credentials(os.environ), "LC_ALL": "C"},
        start_new_session=True,
    )
    try:
        out, _ = process.communicate(timeout=VERSION_TIMEOUT)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        raise ValueError(f"{compiler} gave no --version within {VERSION_TIMEOUT} s") from None
    # As clang ends it (at `\n`), never at another of str.splitlines's breaks.
    line = out.split("\n", 1)[0].rstrip("\r")
    if process.returncode or not line:
        raise ValueError(f"{compiler} --version failed")
    return line


def _executable(path, shown):
    """Refuses what isn't an executable Peeky can run on this host (UNSUPPORTED)."""
    with open(path, "rb") as stream:
        head = stream.read(64)
    if head[:2] == b"#!":
        raise _refused(
            "UNSUPPORTED",
            f"{shown} is a script, not an executable: libtool writes one in place of a "
            f"program built with shared libraries. Probe the program it runs "
            f"({posixpath.join(posixpath.dirname(shown), '.libs', posixpath.basename(shown))}),"
            " or build with --disable-shared",
        )
    if head[:4] == b"\x7fELF":
        if sys.platform != "linux":
            raise _refused("UNSUPPORTED", f"{shown} is an ELF executable, which runs on Linux")
        if len(head) < 18 or head[4:6] != b"\x02\x01":
            raise _refused("UNSUPPORTED", f"{shown} isn't a 64-bit little-endian ELF executable")
        if struct.unpack_from("<H", head, 16)[0] not in (2, 3):
            raise _refused("UNSUPPORTED", f"{shown} is an ELF object or core file: not a program")
        return
    magic = struct.unpack_from("<I", head)[0] if len(head) >= 16 else None
    if magic == 0xFEEDFACF:
        if sys.platform != "darwin":
            raise _refused("UNSUPPORTED", f"{shown} is a Mach-O executable, which runs on macOS")
        if struct.unpack_from("<I", head, 12)[0] != 2:
            raise _refused("UNSUPPORTED", f"{shown} is a Mach-O library or object: not a program")
        return
    if magic in (0xBEBAFECA, 0xCAFEBABE):
        raise _refused(
            "UNSUPPORTED",
            f"{shown} is a universal binary: build it for one architecture (-arch)",
        )
    raise _refused("UNSUPPORTED", f"{shown} is not an ELF or Mach-O executable")


@dataclass(frozen=True)
class BinaryManifest:
    """What a binary target's run rests on, as a build's manifest is (build.BuildManifest):
    the binary, its symbols (Peeky's dSYM on macOS), the tools, and each file the units
    read name. ``original_inputs``: those under the root, by their relative names."""

    binary: FileIdentity
    symbols: tuple
    tools: tuple
    files: tuple
    original_inputs: tuple
    effective_environment: tuple

    @property
    def inputs(self):
        # The program runs where it is: what it reads is the user's own files.
        return self.original_inputs

    @property
    def manifest_id(self):
        return _digest(asdict(self))

    @property
    def build_id(self):
        return _digest({k: v for k, v in asdict(self).items() if k != "symbols"})


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass
class BinaryRun:
    """A binary target's run: ``workspace`` and ``source_root`` are the target directory,
    where its line tables name the sources, and where the program runs; ``scratch`` is
    Peeky's own directory (the dSYM, lldb's home, the tools' scratch), the only one it
    writes or removes."""

    workspace: Path
    source_root: Path
    scratch: Path
    manifest: BinaryManifest

    def cleanup(self):
        if self.scratch.exists():
            shutil.rmtree(self.scratch)


@dataclass(frozen=True)
class Program:
    """The program a binary target's session reads: what NativeSession takes of a target.
    ``sources``: the main files under the root of the units read; ``headers``: the other
    files under the root they name."""

    target: object
    language: str
    optimization: str
    sources: tuple
    headers: tuple
    identity: dict

    @property
    def arguments(self):
        return self.target.arguments

    @property
    def dwarfdump(self):
        return self.target.dwarfdump

    @property
    def verification_inputs(self):
        return self.sources + self.headers

    @property
    def target_identity(self):
        return self.identity

    def validate_breakpoints(self, points):
        self.target.validate_breakpoints(points)

    def preprocess_argv(self, source):
        # A unit's text is its replay's (BinarySession._preprocessed), never a build's.
        raise ValueError(TEXT_UNREAD.format(unit=source, why="Peeky built no unit of it"))


@dataclass(frozen=True)
class BinaryTarget:
    """A config's `binary`: relative to the target directory, the root of the sources
    its debug information names. ``language``: `c` or `cpp`; None takes it from the units
    read (`cpp` where any is C++). ``compiler``: the clang that built it, run only for its
    `--version`. ``optimized``: read units built with an `-O` level or LTO as optimized."""

    binary: str
    language: str | None = None
    compiler: str | None = None
    adapter: str = "lldb-dap"
    dwarfdump: str = "llvm-dwarfdump"
    dsymutil: str | None = None
    arguments: tuple[str, ...] = ()
    optimized: bool = False
    # A trusted claim: the functions of the program that return twice, besides setjmp's
    # (MicroPython's `nlr_push`). None: any call may, where Peeky can't rule it out.
    returns_twice: tuple[str, ...] | None = None

    accepts_arguments = True
    supports_deadline = True
    # The binary is the project's own build: an edit needs a rebuild Peeky doesn't run.
    supports_edits = False
    validate_arguments = staticmethod(NativeTarget.validate_arguments)

    def __post_init__(self):
        if self.language not in (None, "c", "cpp"):
            raise ValueError("Native language must be c or cpp")
        if not isinstance(self.binary, str) or not self.binary or "\x00" in self.binary:
            raise ValueError("A binary target's binary must be a path in the target directory")
        if type(self.optimized) is not bool:
            raise ValueError('A binary target\'s "optimized" must be true or false')
        if self.compiler is None:
            object.__setattr__(self, "compiler", "clang++" if self.language == "cpp" else "clang")
        self.validate_arguments(self.arguments)
        if self.returns_twice is not None and (
            not isinstance(self.returns_twice, tuple)
            or len(self.returns_twice) > MAX_RETURNS_TWICE
            or not all(
                isinstance(name, str) and re.fullmatch(r"[A-Za-z_]\w{0,200}", name)
                for name in self.returns_twice
            )
        ):
            raise ValueError(
                'A binary target\'s "returns_twice" must be a list of at most '
                f"{MAX_RETURNS_TWICE} function names"
            )

    @property
    def target_identity(self):
        return {
            "language": self.language,
            "binary": self.binary,
            "arguments": list(self.arguments),
            "optimized": self.optimized,
            **(
                {"returns_twice": sorted(self.returns_twice)}
                if self.returns_twice is not None
                else {}
            ),
        }

    def validate_breakpoints(self, points):
        PythonAdapter().validate_breakpoints(points)
        if any(set(point) != {"line"} for point in points):
            raise ValueError("Native targets support unconditional line breakpoints only")

    def located(self, root):
        """The binary's resolved path, inside ``root`` (INVALID_REQUEST otherwise)."""
        root = Path(root).resolve(strict=True)
        given = Path(self.binary)
        path = (given if given.is_absolute() else root / given).resolve()
        if not path.is_relative_to(root):
            raise _refused(
                "INVALID_REQUEST",
                f"The binary {self.binary} is outside the target directory, {root}: give the "
                "directory its sources are under as the target",
            )
        if not path.is_file():
            raise _refused("INVALID_REQUEST", f"No binary {self.binary} in {root}")
        return path

    def _judge(
        self, root, binary, shown, compiler, dwarfdump, tools, env, scratch, deadline, hashed=True
    ):
        """The binary's units, each judged, and the files those Peeky reads name, each
        checked against its MD5 ({absolute path: SHA-256}); on macOS, through the dSYM
        Peeky makes in ``scratch``. Unless ``hashed`` (a listing), no file is hashed: the
        files are None, and the debug map's objects are checked by their stat identities,
        and the dSYM's files not at all."""
        version = _version_line(compiler)
        symbols, dsym = binary, ()
        if sys.platform == "darwin":
            bundle = scratch / f"{binary.name}.dSYM"
            # dsymutil reads the debug information from the objects the binary's debug map
            # names, as they are now: each must be the one linked, before and after.
            try:
                entries = debug_map(binary)
            except ValueError as exc:
                raise _refused(
                    "UNSUPPORTED", f"Peeky can't read {shown}'s debug map: {exc}"
                ) from None
            objects = check_debug_map(entries, root, binary, hashed, deadline)
            said = _run(
                [tools[-1], str(binary), "-o", str(bundle)],
                scratch,
                env,
                _remaining(deadline, 40),
                "dsymutil",
            )
            if warned := next(
                (line.strip() for line in said.split("\n") if "warning" in line.lower()), None
            ):
                raise BinaryChanged(
                    "Provenance: the binary's objects may not be what it was linked from: "
                    f"dsymutil warned: {warned[:300]}. Rebuild the binary (make), then probe "
                    "again"
                )
            if check_debug_map(debug_map(binary), root, binary, hashed, deadline) != objects:
                raise BinaryChanged(
                    "Provenance: an object the binary's debug map names changed while "
                    "dsymutil read it"
                )
            symbols = bundle / "Contents/Resources/DWARF" / binary.name
            if not symbols.is_file():
                raise _refused(
                    "UNSUPPORTED",
                    f"{shown} has no debug information (dsymutil found none): rebuild with -g",
                )
            # A listing keeps no dSYM: nothing of it is hashed.
            dsym = (
                tuple(
                    FileIdentity(item.relative_to(scratch).as_posix(), sha256_file(item), 0)
                    for item in sorted(bundle.rglob("*"))
                    if item.is_file()
                )
                if hashed
                else ()
            )
        index = _metadata(
            dwarfdump,
            ["--debug-info", "--recurse-depth=0", str(symbols)],
            scratch,
            env,
            max_bytes=16 * 1024 * 1024,
            timeout=_remaining(deadline),
        )
        units = read_units(index)
        if not units:
            raise _refused(
                "UNSUPPORTED", f"{shown} has no debug information: rebuild with -g -gdwarf-5"
            )
        for unit in units:
            if unit.tag != "DW_TAG_compile_unit" or unit.dwo_name:
                # As the whole read refuses them (native.ForeignUnits), and before it.
                kind = "DW_UT_skeleton" if unit.dwo_name else _FOREIGN_TAGS.get(unit.tag, unit.tag)
                raise _foreign(kind, unit.offset, unit.dwo_name)
        headers = LineFiles()
        with _symbol_text(
            dwarfdump,
            [["--debug-line", str(symbols)]],
            scratch,
            env,
            limit=MAX_LINE_TEXT,
            timeout=_remaining(deadline),
            what="line tables",
        ) as lines:
            headers.feed(lines)
        tables = headers.close()
        judged = tuple(judge(unit, tables, version, root, self.optimized) for unit in units)
        read = [each for each in judged if each.read]
        if not read:
            raise _refused("UNSUPPORTED", _none_read(shown, judged))
        languages = {each.language for each in read}
        language = self.language or ("cpp" if "cpp" in languages else "c")
        if language == "c" and "cpp" in languages:
            raise _refused(
                "INVALID_REQUEST",
                f"{shown}'s units include C++ ones ("
                + ", ".join(each.name for each in read if each.language == "cpp")[:200]
                + '): set "language": "cpp"',
            )
        files = check_files(read, root) if hashed else None
        return SimpleNamespace(
            version=version,
            symbols=symbols,
            dsym=dsym,
            units=units,
            judged=judged,
            read=read,
            language=language,
            files=files,
        )

    def remote_inputs(self, root):
        """The files under ``root`` a remote client's copies are compared with: those the
        units Peeky reads name, by their relative names, as the binary is now. Listed once
        per binary (cached by its size, time and inode), one listing at a time: another
        caller waits for it, or is BUSY. Within REMOTE_INPUTS_SECONDS, and Python's bounds
        (sources.MAX_PYTHON_INPUTS files, MAX_PYTHON_BYTES in all), checked before any file
        is read: no file is hashed (preparation checks each against its MD5). UNSUPPORTED
        past them, as for a binary Peeky doesn't read."""
        root = Path(root).resolve(strict=True)
        binary = self.located(root)
        shown = binary.relative_to(root).as_posix()
        _executable(binary, shown)
        info = binary.stat()
        key = (str(binary), str(root), info.st_size, info.st_mtime_ns, info.st_ino, self)
        if key in _REMOTE_INPUTS:
            return _REMOTE_INPUTS[key]
        if not _LISTING.acquire(timeout=REMOTE_INPUTS_SECONDS):
            raise Refused(
                "BUSY",
                f"The worker is still listing {shown}'s files for another client: nothing ran",
                "Retry in a moment: the worker lists a binary's files once, then answers at once.",
            )
        try:
            if key not in _REMOTE_INPUTS:
                names = self._list_inputs(root, binary, shown)
                _REMOTE_INPUTS.clear()
                _REMOTE_INPUTS[key] = names
            return _REMOTE_INPUTS[key]
        finally:
            _LISTING.release()

    def _list_inputs(self, root, binary, shown):
        from peeky.sources import MAX_PYTHON_BYTES, MAX_PYTHON_INPUTS

        compiler, dwarfdump = _tool(self.compiler), _tool(self.dwarfdump)
        tools = [compiler, dwarfdump]
        if sys.platform == "darwin":
            tools.append(_tool(self.dsymutil or "dsymutil"))
        elif sys.platform != "linux":
            raise ValueError("Native targets support Linux and macOS hosts only")
        env = without_credentials(
            {key: os.environ[key] for key in INHERITED_ENV if key in os.environ}
        )
        deadline = time.monotonic() + REMOTE_INPUTS_SECONDS
        scratch = Path(tempfile.mkdtemp(prefix="peeky-binary-", dir=private_base())).resolve()
        try:
            found = self._judge(
                root, binary, shown, compiler, dwarfdump, tools, env, scratch, deadline, False
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        names = sorted(
            {
                Path(path).relative_to(root).as_posix()
                for each in found.read
                for path, _ in each.files
                if Path(path).is_relative_to(root)
            }
        )

        def refused(size):
            return Refused(
                "UNSUPPORTED",
                f"{shown}'s units name {len(names):,} files under the target directory"
                + (f" ({size / (1 << 20):.1f} MiB or more)" if size is not None else "")
                + f". A worker compares at most {MAX_PYTHON_INPUTS:,} files, "
                f"{MAX_PYTHON_BYTES >> 20} MiB in all, with a remote client's copies: run Peeky "
                "on the binary's own host",
                "Nothing ran. Run Peeky on the binary's own host, where no file is compared "
                "with a client's.",
            )

        if len(names) > MAX_PYTHON_INPUTS:
            raise refused(None)
        size = 0
        for name in names:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Listing {shown}'s files took over {REMOTE_INPUTS_SECONDS} s: nothing ran"
                )
            try:
                size += (root / name).stat().st_size
            except OSError:
                # Preparation refuses it, by name, as gone.
                continue
            if size > MAX_PYTHON_BYTES:
                raise refused(size)
        return tuple(names)

    def prepare(self, program, root, *, edit=None, deadline=None):
        if edit is not None:
            raise _refused(
                "UNSUPPORTED",
                "A binary target can't take a candidate edit: the binary is the project's own "
                "build. Edit the source, rebuild it, then probe",
            )
        root = Path(root).resolve(strict=True)
        binary = self.located(root)
        shown = binary.relative_to(root).as_posix()
        wanted = Path(program).resolve() if program is not None else binary
        if not wanted.is_relative_to(root):
            raise _refused("INVALID_REQUEST", f"{program} is outside the target directory")
        wanted = None if wanted == binary else wanted.relative_to(root).as_posix()
        _executable(binary, shown)
        compiler, adapter, dwarfdump = map(_tool, (self.compiler, self.adapter, self.dwarfdump))
        tools = [compiler, adapter, dwarfdump]
        if sys.platform == "darwin":
            dsymutil = _tool(self.dsymutil or "dsymutil")
            tools.append(dsymutil)
        elif sys.platform != "linux":
            raise ValueError("Native targets support Linux and macOS hosts only")
        env = without_credentials(
            {key: os.environ[key] for key in INHERITED_ENV if key in os.environ}
        )
        scratch = Path(tempfile.mkdtemp(prefix="peeky-binary-", dir=private_base())).resolve()
        try:
            return self._prepare(
                root,
                binary,
                shown,
                wanted,
                compiler,
                adapter,
                dwarfdump,
                tools,
                env,
                scratch,
                deadline,
            )
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise

    def _prepare(
        self,
        root,
        binary,
        shown,
        wanted,
        compiler,
        adapter,
        dwarfdump,
        tools,
        env,
        scratch,
        deadline,
    ):
        identity_before = sha256_file(binary)
        tool_ids = tuple(ToolIdentity(tool, sha256_file(tool)) for tool in sorted(set(tools)))
        found = self._judge(root, binary, shown, compiler, dwarfdump, tools, env, scratch, deadline)
        symbols, dsym, units = found.symbols, found.dsym, found.units
        judged, read, language, files = found.judged, found.read, found.language, found.files
        declared = tuple(
            sorted(
                {
                    Path(path).relative_to(root).as_posix()
                    for path in files
                    if Path(path).is_relative_to(root)
                }
            )
        )
        offsets = frozenset(each.unit.stmt_list for each in read)
        text = read_line_table(
            dwarfdump,
            [["--debug-line", str(symbols)]],
            root,
            env,
            declared,
            timeout=_remaining(deadline),
            only=offsets,
            cwd=scratch,
        )
        lines = parse_lines(text, root, declared)
        if wanted is not None and not lines.get(wanted):
            raise _refused("UNSUPPORTED", _unread_file(wanted, shown, judged, root))
        try:
            identity = module_identity(dwarfdump, binary, symbols, scratch, env, deadline=deadline)
        except ValueError as exc:
            flag = "-Wl,--build-id" if sys.platform == "linux" else "a UUID (not -Wl,-no_uuid)"
            raise _refused(
                "UNSUPPORTED",
                f"{shown} has no build ID Peeky can read ({exc}): link it with {flag}",
            ) from None
        texts = replay_units(read, compiler, scratch, root, deadline)
        any_call = None
        if self.returns_twice is None:
            # The toolchain's own environment, never the program's (``env``).
            tools = replay_environment(scratch)
            uncovered = uncovered_code(
                binary,
                units,
                lambda: native_startup.toolchain_startup(compiler, tools, _remaining(deadline, 10)),
            )
            any_call = any_call_returns_twice(judged, texts, uncovered)
        returns_twice = ()
        if any_call is None:
            # Calls that return twice are found by name: setjmp's, a macro of a header
            # outside the program that may expand to one, and those the config names.
            outside = sorted(path for path in files if not Path(path).is_relative_to(root))
            jumps = (
                has_nonlocal_jumps(binary)
                or mentions_nonlocal_jumps(root / name for name in declared)
                or mentions_nonlocal_jumps(outside, (b"__builtin_setjmp",))
            )
            macros = frozenset()
            if jumps:
                macros = returning_twice(
                    Path(path).read_bytes().decode("utf-8", errors="replace") for path in outside
                )
            # The family's names are always looked for: the check above only spares
            # reading the headers outside the program.
            returns_twice = tuple(sorted(set(self.returns_twice or ()) | macros))
        found = read_program_symbols(
            dwarfdump,
            symbols,
            root,
            env,
            text,
            declared,
            language,
            lambda info: returns_twice,
            deadline=deadline,
            cwd=scratch,
        )
        if sha256_file(binary) != identity_before:
            raise BinaryChanged(f"Provenance: {shown} changed while Peeky read it")
        mains = {each.name for each in read}
        sources = tuple(name for name in declared if name in mains)
        level = max(
            ("flto" if each.lto and each.level == "0" else each.level for each in read),
            key=lambda found: _LEVELS.index(found) if found in _LEVELS else len(_LEVELS),
        )
        # The highest of any unit read: every -O0 rule holds only where all are -O0.
        level = "O0" if level == "0" else level if level == "flto" else f"O{level}"
        program = Program(
            self,
            language,
            level,
            sources,
            tuple(name for name in declared if name not in mains),
            {
                **self.target_identity,
                "language": language,
                "binary": shown,
                "optimization": level,
                "units": {
                    status: sum(1 for each in judged if each.status == status)
                    for status in (JUDGED, OPTIMIZED, NOT_JUDGED)
                },
            },
        )
        manifest = BinaryManifest(
            FileIdentity(shown, identity_before, binary.stat().st_size),
            dsym,
            tool_ids,
            tuple(sorted(files.items())),
            tuple(
                FileIdentity(Path(path).relative_to(root).as_posix(), digest, 0)
                for path, digest in sorted(files.items())
                if Path(path).is_relative_to(root)
            ),
            tuple(sorted(env.items())),
        )
        run = BinaryRun(root, root, scratch, manifest)
        session = BinarySession(
            program,
            run,
            adapter,
            lines,
            identity,
            binary,
            symbols,
            judged=judged,
            files=files,
            line_tables=offsets,
            texts=texts,
            any_call=any_call,
            # Whether a unit may optimize a function beyond its level: unknown where any
            # read unit's text isn't.
            optimized=(
                any(text.optimizes for text in texts.values())
                if all(isinstance(text, UnitText) for text in texts.values())
                else None
            ),
            **found,
        )
        session.assert_current()
        return session


# How many of the debugger's modules a frame's library is looked for among.
MAX_MODULES = 1024


def _segments_hold(module, address):
    """Whether the debugger's ``module``, an ELF file loaded where its `addressRange`
    says (its header's address: decimal from LLVM 18's lldb-dap, hex from later ones),
    holds ``address`` in an executable segment of its file as it is now."""
    path, loaded = module.get("path"), module.get("addressRange")
    if not isinstance(path, str) or not isinstance(loaded, str):
        return False
    if re.fullmatch(r"0x[0-9a-fA-F]{1,16}", loaded):
        base = int(loaded, 16)
    elif re.fullmatch(r"[0-9]{1,20}", loaded):
        base = int(loaded)
    else:
        return False
    try:
        header, ranges = elf_executable_ranges(path)
    except (OSError, ValueError, struct.error):
        return False
    bias = base - header
    return bias >= 0 and any(start + bias <= address < end + bias for start, end in ranges)


def _run(argv, cwd, env, timeout, what):
    """Runs ``argv``; what it wrote (its output and diagnostics), or RuntimeError."""
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"{what} took over {timeout:.0f} s") from None
    if done.returncode:
        detail = done.stderr.decode("utf-8", "replace").strip()[:500]
        raise RuntimeError(f"{what} failed: exit={done.returncode}, {detail}")
    return (done.stdout + b"\n" + done.stderr).decode("utf-8", "replace")


# A Mach-O debug map's entry naming an object the linker read (a stab, `N_OSO`): its
# value is the object's modification time, or an archive member's.
N_OSO = 0x66
# The most symbols Peeky reads of a binary's symbol table, and archive members of one.
MAX_SYMBOLS = 16 * 1024 * 1024
MAX_MEMBERS = 1 << 20


def debug_map(binary):
    """[(path, time)] of each object a 64-bit Mach-O executable's debug map names (its
    `N_OSO` stabs, as `nm -ap` lists them), in order: an object's path, or an archive's
    member as `lib.a(member.o)`, and the modification time the linker read of it.
    ValueError where the symbol table can't be read."""
    with open(binary, "rb") as stream:
        header = stream.read(32)
        size = os.fstat(stream.fileno()).st_size
        if len(header) < 32 or struct.unpack_from("<I", header)[0] != 0xFEEDFACF:
            raise ValueError("Expected a 64-bit Mach-O executable")
        count, length = struct.unpack_from("<II", header, 16)
        if not 0 < count <= 4096 or not 0 < length <= 1 << 20:
            raise ValueError("Invalid Mach-O load commands")
        commands = stream.read(length)
        cursor, table = 0, None
        for _ in range(count):
            if cursor + 8 > len(commands):
                raise ValueError("Truncated Mach-O load command")
            kind, extent = struct.unpack_from("<II", commands, cursor)
            if extent < 8 or cursor + extent > len(commands):
                raise ValueError("Invalid Mach-O load command size")
            if kind == 0x2:  # LC_SYMTAB
                table = struct.unpack_from("<IIII", commands, cursor + 8)
            cursor += extent
        if table is None:
            return []
        offset, symbols, strings, strings_size = table
        if symbols > MAX_SYMBOLS or offset + symbols * 16 > size or strings + strings_size > size:
            raise ValueError("Invalid Mach-O symbol table")
        stream.seek(offset)
        entries = stream.read(symbols * 16)
        stream.seek(strings)
        names = stream.read(strings_size)
    found = []
    for index in range(symbols):
        start, kind, _, _, value = struct.unpack_from("<IBBHQ", entries, index * 16)
        if kind != N_OSO:
            continue
        end = names.find(b"\0", start)
        if start >= len(names) or end < 0:
            raise ValueError("A debug map entry without its name")
        found.append((os.fsdecode(names[start:end]), value))
    return found


def _members(archive, deadline=None):
    """{member name: [its modification times]} of a BSD or System V `ar` archive; read
    within ``deadline`` (time.monotonic()), where one is given: TimeoutError past it."""
    found = {}
    with open(archive, "rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            raise ValueError("not an ar archive")
        size = os.fstat(stream.fileno()).st_size
        at = 8
        for count in range(MAX_MEMBERS):
            if at >= size:
                return found
            if deadline is not None and count % 4096 == 0 and time.monotonic() > deadline:
                raise TimeoutError(f"Reading {archive}'s members ran past Peeky's deadline")
            stream.seek(at)
            header = stream.read(60)
            if len(header) != 60 or header[58:60] != b"`\n":
                raise ValueError("a member header Peeky can't read")
            name, length = header[:16].decode("latin-1").rstrip(), int(header[48:58])
            moment = int(header[16:28].strip() or b"0")
            if name.startswith("#1/"):
                extended = int(name[3:])
                name = os.fsdecode(stream.read(extended).rstrip(b"\0"))
            found.setdefault(name.rstrip("/"), []).append(moment)
            at += 60 + length + (length & 1)
    raise ValueError("too many members")


def check_debug_map(entries, root, binary, hashed=True, deadline=None):
    """{object: SHA-256} of each file the debug map ``entries`` (debug_map) name (where not
    ``hashed``, its size, modification time and inode: nothing is read of it but an
    archive's member headers), within ``deadline`` (time.monotonic(), where given:
    TimeoutError past it). Each is checked by the linker's timestamps, as LLDB and
    dsymutil check them: there, with the
    modification time the linker recorded, in seconds (an archive's member by its
    header's), and the file itself no newer than ``binary``, to the nanosecond: the linker
    wrote the binary after reading it. One rebuilt after the link, gone, or changed makes
    the binary stale (BinaryChanged, naming it). Timestamps forged on purpose aren't
    caught, as no same-user tampering is. A time of 0 (`ZERO_AR_DATE`) can't be checked:
    refused (UNSUPPORTED). So is a file written in the binary's second where either time
    has no part of a second, as a filesystem keeping whole seconds gives: which came
    first can't be told."""
    hashes, stale, unordered, archives = {}, [], None, {}
    linked = os.stat(binary).st_mtime_ns
    for name, moment in entries:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError(
                "Checking the objects the binary's debug map names ran past Peeky's deadline"
            )
        member = re.fullmatch(r"(.+)\(([^()]+)\)", name)
        path = member[1] if member else name
        shown = _shown(os.path.realpath(path), Path(root))
        shown = f"{shown}({member[2]})" if member else shown
        if moment == 0:
            raise _refused(
                "UNSUPPORTED",
                f"The binary's debug map records no time of {shown} (as ZERO_AR_DATE "
                "leaves it), so Peeky can't tell it is what was linked: rebuild without it",
            )
        try:
            if member:
                if path not in archives:
                    archives[path] = _members(path, deadline)
                times = archives[path].get(member[2])
                if not times:
                    stale.append(f"{shown} is gone")
                    continue
                current = moment if moment in times else times[0]
            else:
                current = int(os.stat(path).st_mtime)
            info = os.stat(path)
            written = info.st_mtime_ns
            # In one second, where either time has no part of a second (it may be rounded
            # down), neither order is proven: refused as such, never as newer or older.
            ambiguous = written // 10**9 == linked // 10**9 and (
                written % 10**9 == 0 or linked % 10**9 == 0
            )
            newer = not ambiguous and written > linked
            if ambiguous:
                unordered = unordered or _shown(os.path.realpath(path), Path(root))
            hashes[path] = (
                sha256_file(path) if hashed else (info.st_size, info.st_mtime_ns, info.st_ino)
            )
        except (OSError, ValueError):
            stale.append(f"{shown} is gone or unreadable")
            continue
        if current != moment:
            stale.append(f"{shown} changed since the binary was linked")
        elif newer:
            # Within the second the debug map records, rebuilt after the link all the same.
            named = _shown(os.path.realpath(path), Path(root))
            stale.append(f"{named} is newer than {Path(binary).name}: rebuilt after the link?")
    if stale:
        listed = "; ".join(stale[:LISTED_REASONS])
        more = f" (and {len(stale) - LISTED_REASONS} more)" if len(stale) > LISTED_REASONS else ""
        raise BinaryChanged(
            f"Provenance: the binary is stale: {listed}{more} (its debug map records each "
            "object it was linked from, and when). Rebuild it (make), then probe again"
        )
    if unordered is not None:
        name = Path(binary).name
        raise _refused(
            "UNSUPPORTED",
            f"{unordered} and {name} were written in the same second, and this filesystem's "
            f"timestamps can't show which came first; relink {name} (or touch it) and retry",
        )
    return hashes


def _none_read(shown, judged):
    """The refusal of a binary none of whose units Peeky reads: the reasons, the
    commonest first, each with how many units it holds for."""
    counts = {}
    for each in judged:
        if each.status == NOT_JUDGED and each.why != "its file is outside the target directory":
            counts.setdefault(each.why, []).append(each.name)
    if not counts:
        return (
            f"No unit of {shown} has its file in the target directory: give the directory "
            "its sources are under as the target"
        )
    ranked = sorted(counts.items(), key=lambda item: -len(item[1]))
    listed = "; ".join(
        f"{names[0]}" + (f" and {len(names) - 1} more" if len(names) > 1 else "") + f": {why}"
        for why, names in ranked[:LISTED_REASONS]
    )
    return f"Peeky judges no unit of {shown}: {listed}"


def _unread_file(wanted, shown, judged, root):
    """Why a breakpoint in ``wanted`` is refused: no unit Peeky reads has code there."""
    for each in judged:
        if each.status == NOT_JUDGED and each.name == wanted:
            return f"{wanted} is a unit Peeky doesn't judge: {each.why}"
    return (
        f"No unit of {shown} Peeky judges has code in {wanted}: `peeky describe` lists each "
        "unit, judged or not and why"
    )


class BinarySession(NativeSession):
    """A NativeSession of a binary the project built (BinaryTarget): frames of units
    Peeky doesn't read show nothing, no unit's text is read, and the binary and every file
    its units name are checked before each operation."""

    # A C frame at an address several functions share is withheld too: identical code
    # folding is a link option no debug information records.
    placed_languages = frozenset({"c", "cpp"})

    def __init__(self, *args, judged, files, line_tables, texts=None, any_call=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.judged, self.files, self.line_tables = judged, dict(files), line_tables
        # {unit name: UnitText, or why its text can't be read}.
        self.texts = dict(texts or {})
        # Why any call may return twice (any_call_returns_twice), or None.
        self.any_call = any_call
        # {file under the root: the units read that name it}.
        root = self.prepared.source_root
        self.naming = {}
        for each in judged:
            for path, _ in each.files if each.read else ():
                if Path(path).is_relative_to(root):
                    shown = Path(path).relative_to(root).as_posix()
                    self.naming.setdefault(shown, []).append(each.name)

    @property
    def described_units(self):
        """Each unit as `describe` lists it: judged, or not and why; and, of one read
        whose text can't be, why (``text``). (Not `units`: a RustSession's are its
        application units.)"""
        listed = []
        for each in self.judged:
            entry = each.describe()
            if each.read and isinstance(text := self.texts.get(each.name), str):
                entry["text"] = text
            listed.append(entry)
        return listed

    @property
    def returning_twice(self):
        """Which calls may return twice, for `describe`: any, and why; or the family's
        (c_source.RETURNS_TWICE_NAMES), and those the config or a macro names."""
        if self.any_call is not None:
            return {"any": True, "why": self.any_call}
        return {
            "any": False,
            "family": list(RETURNS_TWICE_NAMES),
            "named": sorted(getattr(self, "returns_twice", None) or ()),
        }

    def library_frame(self, dap, frame):
        """Why nothing of ``frame`` is read where it runs a shared library, never the
        binary (library_path): `this frame runs <library>; Peeky reads only <binary>'s own
        units`. None where it runs the binary's code, or which module it runs can't be told."""
        shown = self.library_path(dap, frame)
        if shown is None:
            return None
        binary = self.prepared.manifest.binary.path
        return f"this frame runs {shown}; Peeky reads only {binary}'s own units"

    def library_path(self, dap, frame):
        """The shared library ``frame`` runs, where the debugger's modules prove it runs
        one, never the binary: its path, relative to the root where it is under it. Its
        module is the frame's moduleId where the debugger gives one (macOS); else, on Linux
        (LLVM 18's lldb-dap gives none), the one module whose loaded executable segments
        hold the frame's address. None where the frame runs the binary's code, or which
        module it runs can't be told."""
        try:
            listed = dap.request("modules", {"startModule": 0, "moduleCount": MAX_MODULES})
        except (RuntimeError, TimeoutError, ValueError, OSError):
            return None
        modules = [each for each in (listed or {}).get("modules") or () if isinstance(each, dict)]
        if "moduleId" in frame:
            found = [each for each in modules if each.get("id") == frame["moduleId"]]
        elif sys.platform == "linux":
            address = (frame or {}).get("instructionPointerReference")
            if not isinstance(address, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,16}", address):
                return None
            found = [each for each in modules if _segments_hold(each, int(address, 16))]
        else:
            return None
        if len(found) != 1 or self._own_module(found[0]):
            return None
        path = found[0].get("path")
        if not isinstance(path, str) or not path or "\n" in path:
            return None
        root = self.prepared.source_root
        shown = Path(path).relative_to(root).as_posix() if Path(path).is_relative_to(root) else path
        return shown[:500]

    def _own_module(self, module):
        """Whether the debugger's ``module`` is the binary: by its build ID or UUID, or by
        its path."""
        identity = module.get("id")
        if isinstance(identity, str) and identity.replace("-", "").upper() == self.module_identity:
            return True
        try:
            return Path(module["path"]).resolve(strict=True) == self.binary
        except (OSError, KeyError, TypeError, ValueError, RuntimeError):
            return False

    def _holding(self, address):
        return [
            each
            for each in self.judged
            if any(low <= address < high for low, high in each.unit.ranges)
        ]

    def frame_withheld(self, frame):
        address = self._block_pc(frame)
        if address is None:
            why = f"Peeky can't tell which unit this frame runs ({self._unknown_address(frame)})"
        else:
            holding = self._holding(address)
            if len(holding) == 1 and holding[0].read:
                return self._folded(address)
            if not holding:
                why = (
                    "this frame runs code no unit of the binary's debug information holds, "
                    "so Peeky doesn't judge it"
                )
            elif len(holding) > 1:
                why = "this frame runs code more than one unit holds, so Peeky can't judge it"
            else:
                why = (
                    f"this frame runs {holding[0].name}, a unit Peeky doesn't judge: "
                    f"{holding[0].why}"
                )
        return Withholding(why, "not shown", f"Nothing in this frame can be changed: {why}")

    def _folded(self, address):
        """Why a frame at ``address`` (unslid) shows nothing where several functions' code
        holds it, or None. A linker's identical code folding gives them one copy, and no
        debug information records it: LLDB may list one's locals at the other's line."""
        code = getattr(self, "code", None) or {}
        if "dies" not in code:
            return None
        if "index" not in code:
            code["index"] = CodeIndex(code["dies"])
        functions = [each for each in code["index"].holding(address) if not each.inline]
        if len(functions) < 2:
            return None
        why = (
            f"{len(functions)} functions share the code this frame runs (identical code "
            "folding, a link option no debug information records), so Peeky can't tell "
            "which one runs: rebuild without --icf"
        )
        return Withholding(why, "not shown", f"Nothing in this frame can be changed: {why}")

    def validate_stop(self, frame):
        place = self._place(frame)
        if place is not None and place[1] in self.lines.get(place[0], ()):
            return super().validate_stop(frame)
        if place is not None:
            # A line of a unit Peeky doesn't read: nothing of the frame is shown
            # (frame_withheld), as it says.
            return None
        return super().validate_stop(frame)

    def unplaceable_line(self, program, line):
        """Why a breakpoint on ``line`` of ``program`` is refused before anything runs: no
        unit Peeky judges has code there (the line has none, or only a unit it doesn't
        judge has, where a frame would show nothing)."""
        try:
            relative = Path(program).resolve().relative_to(self.prepared.source_root).as_posix()
        except (OSError, ValueError):
            return None
        if line in self.lines.get(relative, ()):
            return None
        return (
            f"Line {line} of {relative} is not among the breakpoint lines of the units Peeky "
            "judges (`peeky describe` lists them): it has no code, or only a unit Peeky "
            "doesn't judge has code there"
        )

    def launch_args(self, program, root, args=None):
        launched = super().launch_args(program, root, args)
        if sys.platform == "darwin":
            # Peeky's own dSYM, in its own directory, never one the project left by the
            # binary: lldb-dap would read that one (validate_debugger checks which).
            quoted = str(self.symbols.parents[3]).replace("\\", "\\\\").replace('"', '\\"')
            launched["preRunCommands"] = [f'target symbols add "{quoted}"']
        return launched

    def assert_current(self):
        manifest = self.prepared.manifest
        checks = [(self.binary, manifest.binary.sha256, manifest.binary.path)]
        checks += [
            (self.scratch / item.path, item.sha256, "Peeky's dSYM of the binary")
            for item in manifest.symbols
        ]
        checks += [(Path(tool.path), tool.sha256, tool.path) for tool in manifest.tools]
        checks += [
            (Path(path), digest, _shown(path, self.prepared.source_root))
            for path, digest in manifest.files
        ]
        for path, expected, shown in checks:
            try:
                current = sha256_file(path)
            except (OSError, ValueError):
                current = None
            if current != expected:
                raise RuntimeError(
                    f"Provenance: {shown} changed during the run (or is gone); rebuild if "
                    "needed, and start a new session"
                )

    def _text_reason(self, source):
        """Why the units holding ``source`` can't give its text, or None."""
        units = self.naming.get(str(source), ())
        if not units:
            return TEXT_UNREAD.format(unit=source, why="no unit Peeky reads names it")
        return next((text for unit in units if isinstance(text := self.texts.get(unit), str)), None)

    def _source_text(self, source):
        """``source``'s text as it is, where every unit holding it reads it so (its MD5
        checked before launch, and its SHA-256 before every operation)."""
        if self._text_reason(source) is not None:
            return None
        try:
            path = self.prepared.source_root / source
            if path.stat().st_size > MAX_STATEMENT_SOURCE:
                return None
            return path.read_bytes().decode("utf-8", "replace")
        except OSError:
            return None

    def _statements(self, source):
        """``source``'s Statements, with the macros of each file under the root the
        units holding it read: what `-M` lists, never a guess at the program's headers."""
        cache = self.__dict__.setdefault("statements", {})
        key = str(source)
        if key not in cache:
            cache[key] = None
            texts = [self.texts.get(unit) for unit in self.naming.get(key, ())]
            if texts and all(isinstance(text, UnitText) for text in texts):
                cpp = any(text.cpp for text in texts)
                reads = tuple(sorted({name for text in texts for name in text.reads}))
                hazards = self._hazards(reads, cpp)
                own = self._source_text(key)
                if hazards is not None and own is not None:
                    statements = Statements(own, cpp, hazards=hazards)
                    statements.included = lambda line, at=key: self._included_text(at, line)
                    cache[key] = statements
        return cache[key]

    def _hazards(self, reads, cpp):
        cache = self.__dict__.setdefault("hazards", {})
        if (reads, cpp) not in cache:
            inputs = []
            for name in reads:
                try:
                    path = self.prepared.source_root / name
                    if path.stat().st_size > MAX_STATEMENT_SOURCE:
                        inputs = None
                        break
                    inputs.append(path.read_bytes().decode("utf-8", "replace"))
                except OSError:
                    inputs = None
                    break
            cache[(reads, cpp)] = None if inputs is None else macro_hazards(inputs, cpp)
        return cache[(reads, cpp)]

    def _preprocessed(self, unit):
        found = self.texts.get(str(unit))
        if isinstance(found, UnitText):
            return found.preprocessed, found.statements
        if isinstance(found, str):
            return found
        return TEXT_UNREAD.format(unit=unit, why="it is no unit Peeky reads")

    def _setjmp_above(self, frame, line):
        if self.any_call is not None:
            # Any call above the stop may be one that returns twice: where, Peeky can't tell.
            return 0
        return super()._setjmp_above(frame, line)

    def _setjmp_note(self, at, jump, declared):
        reason = declared.format(at)
        if self.any_call is None:
            if not self.target.target.returns_twice:
                return super()._setjmp_note(at, jump, declared)
            # Calls the config names count as setjmp's do.
            where = f"line {jump}" if jump else "a line Peeky cannot tell"
            why = (
                "this function calls setjmp, another function that returns twice, or one "
                f'the config\'s "returns_twice" names ({where}), above the stop'
            )
        else:
            why = (
                f"any call above the stop may return twice ({self.any_call}; a config's "
                '"returns_twice" can name the functions that do)'
            )
        return ValueNote(
            f"{reason}, and {why}, so a `longjmp` can bring control here past its declaration",
            f"may be leftovers, or its value from before a `longjmp`: {reason}, and {why}, "
            "so Peeky cannot tell whether its declaration has run on this pass; read it "
            "after its declaration",
            refuses=True,
        )

    def _read_units(self, source):
        return list(dict.fromkeys(self.naming.get(str(source), self.target.sources)))
