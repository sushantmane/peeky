"""What a binary's debug information says of each compile unit in it: the compiler that
built it, the command line that compiler recorded, and each file its line table names,
with the MD5 the compiler took of that file as it read it.

A binary the project built (ADR 0008, phase 1) is judged by these facts:

- clang's `-grecord-command-line` records the driver's arguments. On Linux they follow
  the version in `DW_AT_producer`; Apple's clang writes them to `DW_AT_APPLE_flags`.
  Each argument is one word, with a space or backslash in it escaped by a backslash.
  Response files, configuration files and CCC_OVERRIDE_OPTIONS are recorded as their
  arguments; CPATH and C_INCLUDE_PATH are not.
- clang's DWARF 5 line tables hold an MD5 of each file they name. A `#line` that names
  a file leaves the whole table without them, as does GCC, and DWARF 4 has no place
  for them.

This module only reads: it judges nothing, and runs no tool.
"""

import hashlib
import posixpath
import re
from dataclasses import dataclass

from peeky.textlines import octal_string, split_lines


@dataclass(frozen=True)
class Unit:
    """One unit of `llvm-dwarfdump --debug-info --recurse-depth=0`, its strings as the
    compiler wrote them. ``kind``: the header's (`Compile`, `Skeleton`, `Type`,
    `Partial`). ``stmt_list``: its line table's offset, or None."""

    offset: int
    die: int
    version: int
    kind: str
    tag: str
    name: str | None
    comp_dir: str | None
    producer: str | None
    apple_flags: str | None
    language: str | None
    stmt_list: int | None
    dwo_name: str | None


def _string(raw):
    match = re.fullmatch(r'"(.*)"\)', raw)
    value = octal_string(match[1]) if match else None
    if value is None:
        raise ValueError("Expected a quoted DWARF string attribute")
    return value


def read_units(text):
    """The units of llvm-dwarfdump's `--debug-info --recurse-depth=0` text, in order:
    none where the binary has no debug information. Malformed text fails closed
    (ValueError)."""
    from peeky.rust_symbols import _HEADER, _UNIT, _hex, parse_dies

    lines = split_lines(text)
    if not any(_UNIT.fullmatch(line) for line in lines):
        for line in lines:
            if line.strip() and line != ".debug_info contents:" and not _HEADER.fullmatch(line):
                raise ValueError("Unrecognized DWARF unit index")
        return []
    found = []
    for die in parse_dies(lines, units=True):
        attributes = die.attributes

        def string(name, attributes=attributes):
            raw = attributes.get(name)
            return None if raw is None else _string(raw)

        language = attributes.get("DW_AT_language")
        if language is not None and not re.fullmatch(r"DW_LANG_\w+\)", language):
            raise ValueError("Unexpected DWARF language attribute")
        stmt_list = attributes.get("DW_AT_stmt_list")
        found.append(
            Unit(
                offset=die.unit["offset"],
                die=die.offset,
                version=die.unit["version"],
                kind=die.unit["kind"],
                tag=die.tag,
                name=string("DW_AT_name"),
                comp_dir=string("DW_AT_comp_dir"),
                producer=string("DW_AT_producer"),
                apple_flags=string("DW_AT_APPLE_flags"),
                language=language[:-1] if language else None,
                stmt_list=None if stmt_list is None else _hex(stmt_list),
                dwo_name=string("DW_AT_dwo_name") or string("DW_AT_GNU_dwo_name"),
            )
        )
    return found


def compiler_of(producer):
    """ "clang", "gcc" or "unknown": who a unit's `DW_AT_producer` says built it."""
    if producer and re.search(r"\bclang version\b", producer):
        return "clang"
    if producer and re.match(r"GNU (?:C|C\+\+|Assembler|Fortran)\S* \d", producer):
        return "gcc"
    return "unknown"


def _words(flags):
    """The arguments clang recorded as ``flags``: one word each, separated by one
    space, a space or backslash in a word escaped by a backslash. None where the text
    holds any other escape, so isn't clang's."""
    words, word, escaped = [], [], False
    for char in flags:
        if escaped:
            if char not in " \\":
                return None
            word.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == " ":
            words.append("".join(word))
            word = []
        else:
            word.append(char)
    if escaped:
        return None
    words.append("".join(word))
    return words


class NotRecorded(ValueError):
    """A unit whose command line Peeky can't read. The message says why, and what to
    do, in a few words, for a note."""


def recorded_command(unit, version):
    """The arguments ``unit``'s compiler recorded, its driver's path first.

    ``version``: the first line of the `--version` of the compiler Peeky would run,
    which must be the one that built the unit. clang on Linux writes the arguments
    after that line in `DW_AT_producer`; Apple's clang writes them alone to
    `DW_AT_APPLE_flags`. Raises NotRecorded where they can't be read: another compiler,
    another version of clang, no `-grecord-command-line`, or text clang doesn't write.
    """
    producer = unit.producer
    family = compiler_of(producer)
    if family == "gcc":
        raise NotRecorded("built by GCC, which records no -D, -I or -include: rebuild with clang")
    if family != "clang":
        raise NotRecorded(f"built by {producer or 'an unnamed compiler'}: rebuild with clang")
    if unit.apple_flags is not None:
        flags = unit.apple_flags if producer == version else None
    elif producer == version:
        flags = ""
    else:
        flags = producer[len(version) + 1 :] if producer.startswith(version + " ") else None
    if flags is None:
        raise NotRecorded(f"built by {producer.split(' /')[0]}, not by {version}")
    if not flags:
        raise NotRecorded("records no command line: rebuild with -grecord-command-line")
    words = _words(flags)
    if words is None or not words[0].startswith("/") or len(words) < 2:
        raise NotRecorded("records a command line Peeky can't read")
    return words


# clang's driver options whose value it records as the next word (`-I inc`), as its
# Options.td gives them (Separate and JoinedOrSeparate).
_SEPARATE = frozenset(
    """-o -I -D -U -include -imacros -isystem -iquote -idirafter -iprefix -iwithprefix
    -iwithprefixbefore -isysroot -ivfsoverlay -iframework -iframeworkwithsysroot
    -isystem-after -cxx-isystem -include-pch -MF -MT -MQ -MJ -x -Xlinker -Xassembler
    -Xpreprocessor -Xanalyzer -Xopenmp-target -Xcuda-fatbinary -Xcuda-ptxas -arch -target
    -F -L -l -u -z -A -B -T -dependency-file -dependency-dot -serialize-diagnostics
    -install_name -framework -weak_framework -filelist -working-directory --sysroot
    -index-store-path -fmodules-user-build-path -resource-dir""".split()
)
# Options known to take no separate value: the word after one is its own. Each is
# checked against clang's driver itself (tests/native/test_native_units.py). An -O or
# LTO word after any other option can't be read.
_COMPLETE_WORDS = frozenset(
    """-g -g0 -g1 -g2 -g3 -ggdb -gdwarf-4 -gdwarf-5 -grecord-command-line
    -gno-record-gcc-switches -gline-tables-only -gcolumn-info -fdebug-macro
    -fno-omit-frame-pointer -fomit-frame-pointer -fPIC -fpic -fPIE -fpie
    -fno-strict-aliasing -fno-common -fexceptions -fno-exceptions -fno-rtti
    -fvisibility-inlines-hidden -fstack-protector -fstack-protector-strong
    -fno-stack-protector -ffunction-sections -fdata-sections -fcolor-diagnostics
    -fno-builtin -fsigned-char -funsigned-char -fno-delete-null-pointer-checks
    -fasynchronous-unwind-tables -pthread -pedantic -ansi -c -S -E -w -v -pipe -MD -MMD
    -MP -flto -fno-lto -nostdinc""".split()
)
# And by form, each a kind of option that joins its value (Options.td's Joined and
# CommaJoined): an -O option, a warning (`-Wall`, `-Wl,-O1`), or an -f, -m or -std option
# joining its value with `=`. The driver checks a word of each form too.
_COMPLETE = re.compile(r"-O\w*|-W[\w,=+.-]*|-std=\S+|-[fm][\w-]+=\S*")
# What the driver's -O options mean (`-O` is -O1, `-O4` is -O3: clang's own aliases).
_LEVELS = {"": "1", "0": "0", "1": "1", "2": "2", "3": "3", "4": "3", "s": "s", "z": "z"}
_LEVELS |= {"g": "g", "fast": "fast"}
# Options clang passes on to its compiler (`-Xclang`) that change no code it makes:
# warnings, and exactly those Apple clang 21's driver adds of its own.
_INERT = re.compile(r"-W[\w=-]*")
_APPLE = frozenset(
    """-clang-vendor-feature=+disableNonDependentMemberExprInCurrentInstantiation
    -fno-odr-hash-protocols -clang-vendor-feature=+enableAggressiveVLAFolding
    -clang-vendor-feature=+revert09abecef7bbf -clang-vendor-feature=+thisNoAlignAttr
    -clang-vendor-feature=+thisNoNullAttr
    -clang-vendor-feature=+disableAtImportPrivateFrameworkInImplementationError""".split()
)


def _optimizing(word):
    return word.startswith("-O") or word == "-fno-lto" or re.fullmatch(r"-flto(?:=.*)?", word)


def optimization(words):
    """(level, lto) of the arguments ``recorded_command`` read: the last `-O` option's
    level (`"0"` without one: clang's default), and whether the last of `-flto`,
    `-flto=…` and `-fno-lto` asks for LTO. An `-O` or LTO option passed to clang's
    compiler (`-Xclang -O2`) counts too. Only these: whether an option such as
    `-ffast-math` changes what the code computes is judged apart.

    Read from the words alone, never by running the compiler on them. Each option's
    number of values is known: none (``_COMPLETE_WORDS``, ``_COMPLETE``) or one
    (``_SEPARATE``). A word that is no option is an input. Raises NotRecorded naming what
    can't be read, rather than guess: an option passed with `-mllvm` or `-Xarch_…`, or
    with `-Xclang` but not known; a response or configuration file (`@file`,
    `--config`), which the recorded words should hold expanded; an unknown `-O` level; a
    word like an `-O` or LTO option that is a known option's value (`-o -O0`); and any
    `-O` or LTO word after an option whose number of values isn't known
    (`-fdepscan-prefix-map /old -O0`): it may take that word, and any between. Words
    after `--` are inputs, read as none of these, unless such an option came before it:
    it may take `--` too."""
    level, lto, at, unknown = "0", False, 1, None

    def refuse(what):
        raise NotRecorded(f"records {what}, which Peeky can't judge")

    while at < len(words):
        word = words[at]
        at += 1
        if word == "--" and not unknown:
            break  # Inputs only, after it. Not after an option that may take it.
        if word.startswith(("@", "--config", "-Xarch_", "-Xclang=", "-mllvm=")):
            refuse(word)
        if word in ("-Xclang", "-mllvm", *_SEPARATE):
            if at >= len(words):
                refuse(f"{word} without its value")
            value = words[at]
            at += 1
            if word in _SEPARATE:
                if _optimizing(value):
                    refuse(f"{word} {value}")
                continue
            if word == "-mllvm" or not (
                _INERT.fullmatch(value)
                or value in _APPLE
                or (value.startswith("-O") and value[2:] in _LEVELS)
                or re.fullmatch(r"-flto(?:=(?:full|thin))?", value)
            ):
                refuse(f"{word} {value}")
            if _optimizing(value) and unknown:
                refuse(f"{value} after {unknown}")
            if value.startswith("-O"):
                level = _LEVELS[value[2:]]
            elif value.startswith("-flto"):
                lto = True
            continue
        if _optimizing(word):
            if unknown:
                refuse(f"{word} after {unknown}")
            if word.startswith("-O"):
                if word[2:] not in _LEVELS:
                    refuse(word)
                level = _LEVELS[word[2:]]
            else:
                lto = word != "-fno-lto"
            continue
        if (
            word.startswith("-")
            and word != "-"
            and word not in _COMPLETE_WORDS
            and not _COMPLETE.fullmatch(word)
        ):
            unknown = unknown or word
    return level, lto


@dataclass(frozen=True)
class LineFile:
    """A file a line table names: its directory's index, its name, and the MD5 the
    compiler took of it (``checksum``, 32 hex digits), or None."""

    directory: int
    name: str
    checksum: str | None


@dataclass(frozen=True)
class LineHeader:
    """The header of one line table: its offset in `.debug_line`, its DWARF version,
    its directories and its files, each by its index."""

    offset: int
    version: int
    directories: dict
    files: dict

    def checksummed(self):
        """Whether the table holds an MD5 of every file it names. A DWARF 5 table must
        name file 0, its unit's own (``differs``)."""
        return (
            bool(self.files)
            and (self.version < 5 or 0 in self.files)
            and all(entry.checksum for entry in self.files.values())
        )

    def path(self, index, comp_dir):
        """The path of file ``index`` as the compiler named it: its name, under its
        directory. A DWARF 5 table's directory 0 is the unit's compilation directory,
        and its other relative directories are under it. A DWARF 4 table's are under
        ``comp_dir`` (the unit's `DW_AT_comp_dir`). None where that leaves the path
        relative: nothing says where it is (`-fdebug-compilation-dir=rel`).
        Not resolved: the caller resolves it on this machine, if at all."""
        entry = self.files[index]
        if posixpath.isabs(entry.name):
            return entry.name
        if self.version >= 5:
            directory = self.directories[entry.directory]
            if entry.directory != 0:
                directory = posixpath.join(self.directories[0], directory)
        else:
            directory = comp_dir or ""
            if entry.directory != 0:
                directory = posixpath.join(directory, self.directories[entry.directory])
        found = posixpath.join(directory, entry.name)
        return found if posixpath.isabs(found) else None

    def differs(self, unit):
        """None where this DWARF 5 table's file 0 is ``unit``'s own file: the same
        absolute path, normalized, as its `DW_AT_name` under its `DW_AT_comp_dir`.
        Otherwise why not, in a few words naming both paths. A DWARF 4 table has no
        file 0."""
        if self.version < 5:
            return f"its line table is DWARF {self.version}, which names no file of its own"
        if 0 not in self.files:
            return "its line table names no file 0"
        own = self.path(0, unit.comp_dir)
        named = posixpath.join(unit.comp_dir or "", unit.name or "")
        if own is None or not posixpath.isabs(named):
            return "its line table or its unit names no absolute path"
        if posixpath.normpath(own) != posixpath.normpath(named):
            return f"its line table's file 0 is {own}, but the unit names {named}"
        return None


_TABLE = re.compile(r"debug_line\[0x([0-9a-f]{8,16})\]")
_DIRECTORY = re.compile(r"include_directories\[\s*(\d+)\]\s*=\s*\"(.*)\"")
_FILE = re.compile(r"file_names\[\s*(\d+)\]:")
_FIELD = re.compile(r"(name|dir_index|md5_checksum|mod_time|length|source):\s*(.*)")


class LineFiles:
    """The files each line table names, from llvm-dwarfdump's `--debug-line` text as it
    comes (``feed``, once per dump), its rows skipped. ``tables``: {offset: LineHeader}.
    Malformed or repeated entries fail closed (ValueError)."""

    def __init__(self):
        self.tables = {}
        self._offset = self._version = self._file = None
        self._directories, self._files = {}, {}

    def _close(self):
        if self._offset is None:
            return
        if self._version is None:
            raise ValueError("A line table has no version")
        for entry in self._files.values():
            if "name" not in entry or "directory" not in entry:
                raise ValueError("A line table's file has no name or directory")
            if entry["directory"] not in self._directories and not (
                self._version < 5 and entry["directory"] == 0
            ):
                raise ValueError("A line table's file names no directory it has")
        if self._version >= 5 and 0 not in self._directories:
            raise ValueError("A DWARF 5 line table has no compilation directory")
        self.tables[self._offset] = LineHeader(
            self._offset,
            self._version,
            dict(self._directories),
            {
                index: LineFile(entry["directory"], entry["name"], entry.get("checksum"))
                for index, entry in self._files.items()
            },
        )
        self._offset = self._version = self._file = None
        self._directories, self._files = {}, {}

    def feed(self, lines):
        for raw in split_lines(lines) if isinstance(lines, str) else lines:
            line = raw.strip()
            if not line or line.startswith("0x"):
                continue
            if table := _TABLE.fullmatch(line):
                self._close()
                offset = int(table[1], 16)
                if offset in self.tables:
                    raise ValueError("A line table is listed twice")
                self._offset = offset
                continue
            if self._offset is None:
                continue
            if match := re.fullmatch(r"version:\s+(\d+)", line):
                if self._version is not None:
                    raise ValueError("A line table has two versions")
                self._version = int(match[1])
            elif match := _DIRECTORY.fullmatch(line):
                index = int(match[1])
                if index in self._directories:
                    raise ValueError("A line table lists a directory twice")
                if (name := octal_string(match[2])) is None:
                    raise ValueError("Unsupported DWARF line table name")
                self._directories[index] = name
            elif match := _FILE.fullmatch(line):
                self._file = int(match[1])
                if self._file in self._files:
                    raise ValueError("A line table lists a file twice")
                self._files[self._file] = {}
            elif self._file is not None and (match := _FIELD.fullmatch(line)):
                entry, field, value = self._files[self._file], match[1], match[2]
                if field == "name":
                    quoted = re.fullmatch(r'"(.*)"', value)
                    name = octal_string(quoted[1]) if quoted else None
                    if name is None or "name" in entry:
                        raise ValueError("Unsupported DWARF line table name")
                    entry["name"] = name
                elif field == "dir_index":
                    if not value.isdigit() or "directory" in entry:
                        raise ValueError("Unsupported DWARF line table directory")
                    entry["directory"] = int(value)
                elif field == "md5_checksum":
                    if not re.fullmatch(r"[0-9a-f]{32}", value) or "checksum" in entry:
                        raise ValueError("Unsupported DWARF line table checksum")
                    entry["checksum"] = value
            elif line.startswith(("Address", "-----")):
                # The rows follow: no file is named after them.
                self._file = None

    def close(self):
        """Ends the last table: call once every dump is fed."""
        self._close()
        return self.tables


def md5_of(path, limit=1 << 30):
    """The MD5 of the file at ``path``, as 32 hex digits, read at most ``limit`` bytes
    (ValueError past it)."""
    digest, size = hashlib.new("md5", usedforsecurity=False), 0
    with open(path, "rb") as source:
        while chunk := source.read(1 << 20):
            size += len(chunk)
            if size > limit:
                raise ValueError(f"{path} is over {limit} bytes")
            digest.update(chunk)
    return digest.hexdigest()
