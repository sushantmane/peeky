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
file no unit Peeky reads has code in is refused (UNSUPPORTED, with the flag to add). A
unit's text is not read: the rules that need it withhold, and any call may return twice
(the program's own assembly can, as MicroPython's `nlr_push` does).
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
from dataclasses import asdict, dataclass
from pathlib import Path

from peeky.adapters import PythonAdapter
from peeky.build import INHERITED_ENV, FileIdentity, ToolIdentity, private_base
from peeky.digest import sha256_file
from peeky.errors import Refused
from peeky.native import (
    _FOREIGN_TAGS,
    MAX_LINE_TEXT,
    VERSION_TIMEOUT,
    CodeIndex,
    NativeSession,
    NativeTarget,
    Withholding,
    _foreign,
    _metadata,
    _remaining,
    _symbol_text,
    _tool,
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
TEXT_UNREAD = (
    "Peeky can't read {unit} as compiled: it reads no unit's text of a binary it didn't build"
)
JUDGED, OPTIMIZED, NOT_JUDGED = "judged", "optimized", "not judged"


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
    return Judged(unit, name, status, why, level, lto, tuple(files), language)


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
        raise ValueError(TEXT_UNREAD.format(unit=source))


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

    @property
    def target_identity(self):
        return {
            "language": self.language,
            "binary": self.binary,
            "arguments": list(self.arguments),
            "optimized": self.optimized,
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
            objects = check_debug_map(entries, root, binary)
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
            if check_debug_map(debug_map(binary), root, binary) != objects:
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
            dsym = tuple(
                FileIdentity(item.relative_to(scratch).as_posix(), sha256_file(item), 0)
                for item in sorted(bundle.rglob("*"))
                if item.is_file()
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
        files = check_files(read, root)
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
        found = read_program_symbols(
            dwarfdump,
            symbols,
            root,
            env,
            text,
            declared,
            language,
            # Its units' text isn't read, and its own assembly can return twice: any call
            # may (c_source.returning_twice's names alone are none of them).
            lambda info: (),
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
            **found,
        )
        session.assert_current()
        return session


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


def _members(archive):
    """{member name: [its modification times]} of a BSD or System V `ar` archive."""
    found = {}
    with open(archive, "rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            raise ValueError("not an ar archive")
        size = os.fstat(stream.fileno()).st_size
        at = 8
        for _ in range(MAX_MEMBERS):
            if at >= size:
                return found
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


def check_debug_map(entries, root, binary):
    """{object: SHA-256} of each file the debug map ``entries`` (debug_map) name, each
    checked by the linker's timestamps, as LLDB and dsymutil check them: there, with the
    modification time the linker recorded, in seconds (an archive's member by its
    header's), and the file itself no newer than ``binary``, to the nanosecond: the linker
    wrote the binary after reading it. One rebuilt after the link, gone, or changed makes
    the binary stale (BinaryChanged, naming it). Timestamps forged on purpose aren't
    caught, as no same-user tampering is. A time of 0 (`ZERO_AR_DATE`) can't be checked:
    refused (UNSUPPORTED)."""
    hashes, stale = {}, []
    linked = os.stat(binary).st_mtime_ns
    for name, moment in entries:
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
                times = _members(path).get(member[2])
                if not times:
                    stale.append(f"{shown} is gone")
                    continue
                current = moment if moment in times else times[0]
            else:
                current = int(os.stat(path).st_mtime)
            newer = os.stat(path).st_mtime_ns > linked
            hashes[path] = sha256_file(path)
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

    def __init__(self, *args, judged, files, line_tables, **kwargs):
        super().__init__(*args, optimized=None, **kwargs)
        self.judged, self.files, self.line_tables = judged, dict(files), line_tables
        # {file under the root: the units read that name it}.
        root = self.prepared.source_root
        self.naming = {}
        for each in judged:
            for path, _ in each.files if each.read else ():
                if Path(path).is_relative_to(root):
                    shown = Path(path).relative_to(root).as_posix()
                    self.naming.setdefault(shown, []).append(each.name)

    @property
    def units(self):
        """Each unit as `describe` lists it: judged, or not and why."""
        return [each.describe() for each in self.judged]

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

    def _source_text(self, source):
        return None

    def _preprocessed(self, unit):
        return TEXT_UNREAD.format(unit=unit)

    def _read_units(self, source):
        return list(dict.fromkeys(self.naming.get(str(source), self.target.sources)))
