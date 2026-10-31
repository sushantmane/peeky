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
    `Partial`). ``stmt_list``: its line table's offset, or None. ``ranges``: the
    addresses of its code, [low, high) each, before any slide (``code_ranges``)."""

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
    ranges: tuple = ()
    # `DW_AT_LLVM_sysroot`: the SDK Apple's clang compiled against, where it says.
    sysroot: str | None = None


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
                ranges=code_ranges(attributes, die.continued),
                sysroot=string("DW_AT_LLVM_sysroot"),
            )
        )
    return found


_RANGE = re.compile(r"\[0x([0-9a-f]{1,16}), 0x([0-9a-f]{1,16})\)")
# What a linker writes for code it discarded (a COMDAT copy, a folded function).
_TOMBSTONE = 0xFFFF_FFFF_0000_0000


def code_ranges(attributes, continued):
    """The [low, high) addresses of a unit's code, from its entry's attributes and the
    rows of a range list over several lines (``continued``): its `DW_AT_low_pc` and
    `DW_AT_high_pc` (an address, or the size past the low one: dwarfdump writes either),
    or its `DW_AT_ranges`. A range a linker left for discarded code (at 0, or a
    tombstone) holds none. Unreadable values fail closed (ValueError)."""
    found = []
    if "DW_AT_ranges" in attributes:
        rows = [attributes["DW_AT_ranges"], *continued.get("DW_AT_ranges", ())]
        for row in rows:
            found.extend((int(low, 16), int(high, 16)) for low, high in _RANGE.findall(row))
        if len(rows) > 1 and not rows[-1].rstrip().endswith("))"):
            raise ValueError("A unit's range list without its end")
    elif "DW_AT_low_pc" in attributes and "DW_AT_high_pc" in attributes:
        from peeky.rust_symbols import _hex

        low, high = _hex(attributes["DW_AT_low_pc"]), _hex(attributes["DW_AT_high_pc"])
        found.append((low, high if high > low else low + high))
    return tuple((low, high) for low, high in found if 0 < low < high and low < _TOMBSTONE)


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
# CommaJoined): an -O option, a warning (`-Wall`, `-Wl,-O1`), an -f, -m or -std option
# joining its value with `=`, or the driver's mode, which clang++ records first
# (`--driver-mode=g++`). The driver checks a word of each form too. Another mode reads
# every word its own way (`cl`'s `/clang:-O2`): refused (_MODES).
_COMPLETE = re.compile(r"-O\w*|-W[\w,=+.-]*|-std=\S+|-[fm][\w-]+=\S*")
_MODES = frozenset({"--driver-mode=gcc", "--driver-mode=g++", "--driver-mode=cpp"})
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
        if word.startswith("--driver-mode"):
            if word not in _MODES:
                refuse(word)
            continue
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


# What Peeky's compiler is given of a unit's recorded words when it replays the unit's
# preprocessing (`-E`, with its dependencies), never the words as they are: the options
# that choose what the preprocessor reads or defines, each safe to pass (``_REPLAY_*``);
# never one that loads code, or reads a file but a header. Options that change no
# predefined macro are left out (``_INERT_*``): each is checked against clang's own
# `-dM -E` (tests/native/test_native_units.py). Any other option leaves the unit's text
# unread, naming it.
_REPLAY_SEPARATE = frozenset(
    """-I -D -U -include -imacros -isystem -iquote -idirafter -iprefix -iwithprefix
    -iwithprefixbefore -isystem-after -cxx-isystem -isysroot --sysroot -F -iframework
    -iframeworkwithsysroot -resource-dir -x -target -arch""".split()
)
_REPLAY_WORDS = frozenset(
    """-ansi -pthread -nostdinc -nostdinc++ -nostdlibinc -nobuiltininc -undef -trigraphs
    -fPIC -fpic -fPIE -fpie -fno-PIC -fno-pic -fno-PIE -fno-pie -fexceptions
    -fno-exceptions -fcxx-exceptions -fno-cxx-exceptions -frtti -fno-rtti -fsigned-char
    -funsigned-char -fno-signed-char -fno-unsigned-char -fshort-wchar -fno-short-wchar
    -fshort-enums -fno-short-enums -fstack-protector -fstack-protector-strong
    -fstack-protector-all -fno-stack-protector -ffreestanding -fhosted -fbuiltin
    -fno-builtin -fms-extensions -fno-ms-extensions -fms-compatibility
    -fno-ms-compatibility -fgnu-keywords -fno-gnu-keywords -fgnu89-inline
    -fno-gnu89-inline -fblocks -fno-blocks -fchar8_t -fno-char8_t
    -fdollars-in-identifiers -fno-dollars-in-identifiers -ftrigraphs -fno-trigraphs
    -fdeclspec -fno-declspec -fcoroutines -fno-coroutines -fsized-deallocation
    -fno-sized-deallocation -faligned-allocation -fno-aligned-allocation -fmath-errno
    -fno-math-errno -fno-fast-math -fhonor-nans -fhonor-infinities -fcf-protection
    -fopenmp -fno-openmp -fno-inline -finline-functions -fno-inline-functions
    -finline-hint-functions -fno-operator-names -fdelayed-template-parsing
    -fno-delayed-template-parsing -fthreadsafe-statics -fno-threadsafe-statics
    -fno-elide-constructors -fsigned-wchar -fno-signed-wchar -fasynchronous-unwind-tables
    -fno-asynchronous-unwind-tables -funwind-tables -fno-unwind-tables -fgnu-inline-asm
    -fno-gnu-inline-asm -fasm-blocks -fno-asm-blocks -fasm -fno-asm -fpascal-strings
    -fno-access-control""".split()
)
_REPLAY_FORMS = re.compile(
    r"-[DUI].+|-std=\S+|-O\w*|--target=\S+|--sysroot=\S+|-resource-dir=\S+|-stdlib=\S+"
    r"|-F.+|-m(?!llvm)[A-Za-z0-9][\w.=,+-]*|-fno-builtin-\w+"
    r"|-f(?:gnuc-version|macro-prefix-map|file-prefix-map|ms-compatibility-version"
    r"|cf-protection|openmp|wchar-type|constexpr-steps|constexpr-depth|template-depth"
    r"|bracket-depth)=\S+"
)
_INERT_SEPARATE = frozenset(
    """-o -MF -MT -MQ -MJ -Xlinker -Xassembler -L -l -u -z -T -filelist -install_name
    -framework -weak_framework -serialize-diagnostics -dependency-file -dependency-dot
    -index-store-path""".split()
)
_INERT_WORDS = frozenset(
    """-c -S -w -v -pipe -pedantic -pedantic-errors -Qunused-arguments -MD -MMD -MP -MG
    -g -g0 -g1 -g2 -g3 -ggdb -ggdb0 -ggdb1 -ggdb2 -ggdb3 -gdwarf -gdwarf-4 -gdwarf-5
    -grecord-command-line -gno-record-gcc-switches -grecord-gcc-switches
    -gline-tables-only -gline-directives-only -gcolumn-info -gno-column-info -gsplit-dwarf
    -gembed-source -gstrict-dwarf -gfull -gno-inline-line-tables -fdebug-macro
    -fno-debug-macro -fstandalone-debug -fno-standalone-debug -fno-limit-debug-info
    -flimit-debug-info -fno-omit-frame-pointer -fomit-frame-pointer -ffunction-sections
    -fno-function-sections -fdata-sections -fno-data-sections -fno-common -fcommon
    -fno-strict-aliasing -fstrict-aliasing -fno-plt -fplt
    -fno-semantic-interposition -fsemantic-interposition -fno-delete-null-pointer-checks
    -fvisibility-inlines-hidden -fcolor-diagnostics -fno-color-diagnostics
    -fdiagnostics-color -fno-diagnostics-color -fansi-escape-codes -fno-caret-diagnostics
    -fno-diagnostics-fixit-info -fno-spell-checking -fno-elide-type -flto -fno-lto -fwrapv
    -fno-wrapv -fno-strict-overflow -fstack-clash-protection -fno-stack-clash-protection
    -fzero-initialized-in-bss -fno-zero-initialized-in-bss -fno-jump-tables -fjump-tables
    -fno-ident -fident -fno-unique-section-names -funique-section-names -fno-addrsig
    -faddrsig""".split()
)
_INERT_FORMS = re.compile(
    r"-W(?!p,)[\w,=+.:/-]*|-L.+|-l.+|-f(?:visibility|diagnostics-color|diagnostics-format"
    r"|message-length|error-limit|template-backtrace-limit|macro-backtrace-limit|lto"
    r"|lto-jobs|trivial-auto-var-init|debug-prefix-map|debug-compilation-dir|fp-contract)"
    r"=\S+"
)


def replay_arguments(words, comp_dir, main):
    """The arguments Peeky's compiler replays the preprocessing of a unit with, of the
    words ``recorded_command`` read (its driver first): the options ``_REPLAY_*`` allow,
    in their order, and the unit's own input, the word naming ``main`` (its absolute
    path) under ``comp_dir``, last. Raises NotRecorded naming what it can't pass: an
    option it doesn't know, one passed on to another tool (`-Xclang` but Apple's own
    and warnings, `-Xpreprocessor`, `-Wp,`), no input naming the unit's file, or `-x`
    after it, which applies to later inputs only."""
    passed, at, given, ended = [], 1, None, False

    def unread(what):
        raise NotRecorded(f"its command uses {what}, which Peeky doesn't pass to its compiler")

    def own(word):
        return posixpath.normpath(posixpath.join(comp_dir, word)) == posixpath.normpath(main)

    while at < len(words):
        word = words[at]
        at += 1
        if not ended and word == "--":
            ended = True
            continue
        if word.startswith("@"):
            # A response file the recorded words should hold expanded.
            unread(word)
        if ended or not word.startswith("-") or word == "-":
            if word == "-":
                unread("its standard input as a file")
            if given is None and own(word):
                given = word
            continue
        if word in _REPLAY_SEPARATE or word in _INERT_SEPARATE or word == "-Xclang":
            if at >= len(words):
                unread(f"{word} without its value")
            value = words[at]
            at += 1
            if word == "-Xclang":
                if not (_INERT.fullmatch(value) or value in _APPLE):
                    unread(f"-Xclang {value}")
            elif word in _REPLAY_SEPARATE:
                if word == "-x" and given is not None:
                    unread(f"-x {value} after its input, for later inputs only")
                passed += [word, value]
            continue
        if word in _REPLAY_WORDS or word in _MODES or _REPLAY_FORMS.fullmatch(word):
            passed.append(word)
        elif not (word in _INERT_WORDS or _INERT_FORMS.fullmatch(word)):
            unread(word)
    if given is None:
        raise NotRecorded("its command names no input that is its own file")
    return [*passed, given]


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
