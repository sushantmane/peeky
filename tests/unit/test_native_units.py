"""native_units: what a binary's debug information says of each compile unit (ADR 0008,
phase 1). The texts are llvm-dwarfdump 18's, of a unit clang 18 and GCC 13 built on Linux,
and Apple clang 21 on macOS."""

import hashlib
import re

import pytest

from peeky.native_units import (
    LineFiles,
    NotRecorded,
    compiler_of,
    md5_of,
    optimization,
    read_units,
    recorded_command,
)

CLANG = "Ubuntu clang version 18.1.3 (1ubuntu1)"
APPLE = "Apple clang version 21.0.0 (clang-2100.3.34.2)"

LINUX_UNITS = r"""prog:	file format elf64-littleaarch64

.debug_info contents:
0x00000000: Compile Unit: length = 0x0000005a, format = DWARF32, version = 0x0005, unit_type = DW_UT_compile, abbr_offset = 0x0000, addr_size = 0x08 (next unit at 0x0000005e)

0x0000000c: DW_TAG_compile_unit
              DW_AT_producer	("Ubuntu clang version 18.1.3 (1ubuntu1) /usr/lib/llvm-18/bin/clang -O0 -g -gdwarf-5 -grecord-command-line -I inc -I inc\\ sp -isystem sys -include pre.h -D SP=a\\ b -D Q=\"x\\ y\" -D BS=a\\\\b -D TAB=a\tb -U ZZ -c main.c -o m.o")
              DW_AT_language	(DW_LANG_C11)
              DW_AT_name	("main.c")
              DW_AT_str_offsets_base	(0x00000008)
              DW_AT_stmt_list	(0x00000000)
              DW_AT_comp_dir	("/w")
              DW_AT_low_pc	(0x0000000000000718)
              DW_AT_high_pc	(0x0000000000000760)
              DW_AT_addr_base	(0x00000008)
0x0000005e: Compile Unit: length = 0x0000008e, format = DWARF32, version = 0x0005, unit_type = DW_UT_compile, abbr_offset = 0x0000, addr_size = 0x08 (next unit at 0x000000f0)

0x0000006a: DW_TAG_compile_unit
              DW_AT_producer	("GNU C17 13.3.0 -mlittle-endian -mabi=lp64 -g -gdwarf-5 -fasynchronous-unwind-tables")
              DW_AT_language	(DW_LANG_C11)
              DW_AT_name	("gcc.c")
              DW_AT_comp_dir	("/w")
              DW_AT_low_pc	(0x0000000000000760)
              DW_AT_high_pc	(0x000000000000079c)
              DW_AT_stmt_list	(0x00000093)
"""

APPLE_UNITS = r"""prog.dSYM/Contents/Resources/DWARF/prog:	file format Mach-O arm64

.debug_info contents:
0x00000000: Compile Unit: length = 0x0000005d, format = DWARF32, version = 0x0005, unit_type = DW_UT_compile, abbr_offset = 0x0000, addr_size = 0x08 (next unit at 0x00000061)

0x0000000c: DW_TAG_compile_unit
              DW_AT_producer	("Apple clang version 21.0.0 (clang-2100.3.34.2)")
              DW_AT_language	(DW_LANG_C11)
              DW_AT_name	("main.c")
              DW_AT_LLVM_sysroot	("/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk")
              DW_AT_APPLE_sdk	("MacOSX.sdk")
              DW_AT_str_offsets_base	(0x00000008)
              DW_AT_stmt_list	(0x00000000)
              DW_AT_comp_dir	("/home/me/p\342\200\250q")
              DW_AT_APPLE_flags	("/Library/Developer/CommandLineTools/usr/bin/clang -O0 -g -gdwarf-5 -grecord-command-line -I inc -D ONE=1 -include pre.h -c main.c -o m.o -Xclang -fno-odr-hash-protocols -mmacos-version-min=27.0")
              DW_AT_low_pc	(0x0000000100000328)
              DW_AT_high_pc	(0x0000000100000370)
              DW_AT_addr_base	(0x00000008)
"""

LINES = """prog:	file format elf64-littleaarch64

.debug_line contents:
debug_line[0x00000000]
Line table prologue:
    total_length: 0x0000008f
          format: DWARF32
         version: 5
    address_size: 8
 seg_select_size: 0
 prologue_length: 0x00000050
     opcode_base: 13
standard_opcode_lengths[DW_LNS_copy] = 0
include_directories[  0] = "/w"
include_directories[  1] = "inc"
file_names[  0]:
           name: "main.c"
      dir_index: 0
   md5_checksum: d78a4c753a502a3a43f8f721a7f12da4
file_names[  1]:
           name: "a.h"
      dir_index: 1
   md5_checksum: 67fa149688f3de530af68560e7707f18

Address            Line   Column File   ISA Discriminator OpIndex Flags
------------------ ------ ------ ------ --- ------------- ------- -------------
0x0000000000000718      4      0      0   0             0       0  is_stmt
0x0000000000000760      2     33      1   0             0       0  end_sequence

debug_line[0x00000093]
Line table prologue:
    total_length: 0x0000005c
          format: DWARF32
         version: 4
 prologue_length: 0x00000029
include_directories[  1] = "inc"
file_names[  1]:
           name: "gcc.c"
      dir_index: 0
       mod_time: 0x00000000
         length: 0x00000000
file_names[  2]:
           name: "a.h"
      dir_index: 1
       mod_time: 0x00000000
         length: 0x00000000

Address            Line   Column File   ISA Discriminator OpIndex Flags
------------------ ------ ------ ------ --- ------------- ------- -------------
0x0000000000000000      2     31      2   0             0       0  is_stmt

debug_line[0x000000f8]
Line table prologue:
         version: 5
include_directories[  0] = "/w"
file_names[  0]:
           name: "gram.c"
      dir_index: 0
file_names[  1]:
           name: "/abs/gram.y"
      dir_index: 0
"""


def test_units_and_their_strings():
    clang, gcc = read_units(LINUX_UNITS)
    assert (clang.offset, clang.die, clang.version, clang.kind) == (0, 0xC, 5, "Compile")
    assert (clang.tag, clang.name, clang.comp_dir, clang.stmt_list) == (
        "DW_TAG_compile_unit",
        "main.c",
        "/w",
        0,
    )
    assert clang.language == "DW_LANG_C11" and clang.apple_flags is clang.dwo_name is None
    assert "\t" in clang.producer and gcc.stmt_list == 0x93
    assert [compiler_of(unit.producer) for unit in (clang, gcc)] == ["clang", "gcc"]
    (apple,) = read_units(APPLE_UNITS)
    # dwarfdump writes a non-ASCII byte in octal.
    assert apple.comp_dir == "/home/me/p\u2028q" and apple.producer == APPLE


def test_malformed_units_fail_closed():
    with pytest.raises(ValueError):
        read_units(LINUX_UNITS.replace('("main.c")', '("main.c)'))
    with pytest.raises(ValueError):
        read_units(LINUX_UNITS.replace("(DW_LANG_C11)", "(C11 or so)", 1))
    with pytest.raises(ValueError):
        read_units("prog:\tfile format elf64-littleaarch64\n\nsomething else\n")


def test_a_binary_without_debug_information_has_no_units():
    assert read_units("prog:\tfile format elf64-littleaarch64\n\n.debug_info contents:\n") == []
    assert read_units("") == []


def test_clang_records_each_argument_as_one_word():
    clang, _ = read_units(LINUX_UNITS)
    words = recorded_command(clang, CLANG)
    assert words[0] == "/usr/lib/llvm-18/bin/clang"
    # The driver writes `-Iinc` as two words; a space or backslash in one is escaped.
    assert words[words.index("-I") + 1] == "inc"
    assert ["-I", "inc sp", "-isystem", "sys", "-include", "pre.h"] == words[7:13]
    defined = [words[at + 1] for at, word in enumerate(words) if word == "-D"]
    assert defined == ["SP=a b", 'Q="x y"', "BS=a\\b", "TAB=a\tb"]
    assert words[-6:] == ["-U", "ZZ", "-c", "main.c", "-o", "m.o"]


def test_apple_clang_records_its_flags_apart():
    (apple,) = read_units(APPLE_UNITS)
    words = recorded_command(apple, APPLE)
    assert words[:3] == ["/Library/Developer/CommandLineTools/usr/bin/clang", "-O0", "-g"]
    assert words[-3:] == ["-Xclang", "-fno-odr-hash-protocols", "-mmacos-version-min=27.0"]


@pytest.mark.parametrize(
    "change, version, why",
    [
        (None, "Ubuntu clang version 20.1.2 (0ubuntu1~24.04.3)", "not by Ubuntu clang version 20"),
        (None, "Ubuntu clang version 18.1", "not by"),
        # dwarfdump doubles each backslash clang wrote: `\\\\` is clang's escaped one.
        (("-o m.o", "-o m\\\\\\\\q.o"), CLANG, None),
        (("-o m.o", "-o m\\\\q.o"), CLANG, "can't read"),
        (("-o m.o", "-o m.o\\\\"), CLANG, "can't read"),
        ((") /usr/lib/llvm-18/bin/clang", ") usr/lib/llvm-18/bin/clang"), CLANG, "can't read"),
    ],
)
def test_a_command_is_read_only_as_the_named_clang_writes_it(change, version, why):
    clang = read_units(LINUX_UNITS.replace(*change) if change else LINUX_UNITS)[0]
    if why is None:
        assert recorded_command(clang, version)[-1] == "m\\q.o"
        return
    with pytest.raises(NotRecorded, match=why):
        recorded_command(clang, version)


def test_no_recorded_command_line():
    text = LINUX_UNITS.replace(
        LINUX_UNITS[LINUX_UNITS.index("(1ubuntu1) ") + 10 : LINUX_UNITS.index('")\n')], ""
    )
    clang = read_units(text)[0]
    assert clang.producer == CLANG
    with pytest.raises(NotRecorded, match="rebuild with -grecord-command-line"):
        recorded_command(clang, CLANG)
    (apple,) = read_units(APPLE_UNITS.replace("DW_AT_APPLE_flags", "DW_AT_APPLE_other"))
    with pytest.raises(NotRecorded, match="rebuild with -grecord-command-line"):
        recorded_command(apple, APPLE)
    # Apple's flags are its own: never after the version, as Linux's are.
    (apple,) = read_units(APPLE_UNITS)
    with pytest.raises(NotRecorded, match="not by"):
        recorded_command(apple, "Apple clang version 20.0.0 (clang-2000.0.0)")


def test_gcc_and_unknown_compilers_record_no_command_peeky_reads():
    _, gcc = read_units(LINUX_UNITS)
    with pytest.raises(NotRecorded, match="GCC, which records no -D, -I or -include"):
        recorded_command(gcc, "gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0")
    (other,) = read_units(LINUX_UNITS.split("0x0000005e: Compile")[0].replace("Ubuntu clang", "X"))
    assert compiler_of(other.producer) == "unknown"
    with pytest.raises(NotRecorded, match="rebuild with clang"):
        recorded_command(other, CLANG)


def test_line_tables_name_their_files_with_md5s():
    files = LineFiles()
    files.feed(LINES)
    clang, gcc, lined = files.close().values()
    assert (clang.offset, clang.version, gcc.offset, gcc.version) == (0, 5, 0x93, 4)
    assert clang.checksummed() and not gcc.checksummed() and not lined.checksummed()
    assert clang.files[1].checksum == "67fa149688f3de530af68560e7707f18"
    assert [clang.path(index, "/elsewhere") for index in clang.files] == [
        "/w/main.c",
        "/w/inc/a.h",
    ]
    # DWARF 4 counts files from 1; directory 0 is the unit's own.
    assert [gcc.path(index, "/w") for index in gcc.files] == ["/w/gcc.c", "/w/inc/a.h"]
    assert lined.path(1, "/w") == "/abs/gram.y"
    units = read_units(LINUX_UNITS)
    assert clang.differs(units[0]) is None
    assert gcc.differs(units[1]) == "its line table is DWARF 4, which names no file of its own"


def _table(text):
    files = LineFiles()
    files.feed(text)
    (table,) = files.close().values()
    return table


ONE = LINES.split("\n\ndebug_line[0x00000093]")[0]


def test_a_relative_compilation_directory_leaves_paths_unresolved():
    # `-fdebug-compilation-dir=rel`: DWARF 5's directory 0 is the compilation directory,
    # never joined to itself; nothing says where `rel` is, so no path is given.
    table = _table(
        ONE.replace('include_directories[  0] = "/w"', 'include_directories[  0] = "rel"')
    )
    assert [table.path(index, "rel") for index in table.files] == [None, None]
    assert table.path(0, "/w") is None
    (unit,) = read_units(LINUX_UNITS.split("0x0000005e: Compile")[0].replace('("/w")', '("rel")'))
    assert unit.comp_dir == "rel"
    assert table.differs(unit) == "its line table or its unit names no absolute path"
    four = _table(
        'debug_line[0x00000000]\n version: 4\nfile_names[  1]:\n name: "a.c"\n dir_index: 0\n'
    )
    assert four.path(1, None) is None and four.path(1, "rel") is None
    assert four.path(1, "/w") == "/w/a.c"


def test_a_dwarf_5_table_must_name_its_unit_as_file_0():
    clang = read_units(LINUX_UNITS)[0]
    table = _table(ONE)
    assert table.checksummed() and table.differs(clang) is None
    # Without file 0, a table names none of its unit's own lines: not judged.
    lacking = _table(
        ONE.replace(
            'file_names[  0]:\n           name: "main.c"',
            'file_names[  2]:\n           name: "main.c"',
        )
    )
    assert 0 not in lacking.files and not lacking.checksummed()
    assert lacking.differs(clang) == "its line table names no file 0"
    # The same file, written another way, is the unit's own.
    for change in (
        ('include_directories[  0] = "/w"', 'include_directories[  0] = "/w/."'),
        (
            'include_directories[  1] = "inc"',
            'include_directories[  1] = "."\ninclude_directories[  2] = "inc"',
        ),
    ):
        same = ONE.replace(*change, 1)
        if "[  2]" in change[1]:
            same = same.replace("      dir_index: 0", "      dir_index: 1", 1)
            same = same.replace(
                "      dir_index: 1\n   md5_checksum: 67", "      dir_index: 2\n   md5_checksum: 67"
            )
        assert _table(same).differs(clang) is None, change
    # Another file is named, both paths given.
    assert _table(ONE.replace('name: "main.c"', 'name: "other.c"', 1)).differs(clang) == (
        "its line table's file 0 is /w/other.c, but the unit names /w/main.c"
    )
    renamed = read_units(LINUX_UNITS.replace('("main.c")', '("m.c")', 1))[0]
    assert (
        table.differs(renamed) == "its line table's file 0 is /w/main.c, but the unit names /w/m.c"
    )


@pytest.mark.parametrize(
    "arguments, found",
    [
        ("-c a.c", ("0", False)),
        ("-O2 -c a.c -O0", ("0", False)),
        ("-O0 -O2 -c a.c", ("2", False)),
        ("-O -c a.c", ("1", False)),
        ("-O4 -Os -Oz", ("z", False)),
        ("-Ofast", ("fast", False)),
        ("-O0 -Xclang -O2", ("2", False)),
        ("-Xclang -O2 -O0", ("0", False)),
        ("-O0 -flto=thin", ("0", True)),
        ("-flto=full -fno-lto", ("0", False)),
        ("-fno-lto -flto", ("0", True)),
        ("-flto-jobs=4 -O0", ("0", False)),
        ("-O0 -Xclang -flto=thin", ("0", True)),
        ("-O0 -o out -I inc -D X=1", ("0", False)),
        # Exactly those Apple clang 21's driver adds of its own.
        (
            "-O0 -Xclang -clang-vendor-feature=+thisNoNullAttr -Xclang -fno-odr-hash-protocols",
            ("0", False),
        ),
        ("-O0 -Xclang -Wno-error=allocator-wrappers -mmacos-version-min=27.0", ("0", False)),
        ("-O2 -Wl,-O1 -Wall -fno-omit-frame-pointer -O0", ("0", False)),
        # clang++ records its driver's mode first (Linux's clang 18, Apple's clang 21).
        ("--driver-mode=g++ -g -gdwarf-5 -grecord-command-line -O0 -c a.cpp -o l.o", ("0", False)),
        ("--driver-mode=g++ -O2 -c a.cpp", ("2", False)),
        ("--driver-mode=gcc -O0 -c a.c", ("0", False)),
        ("--driver-mode=cpp -O0 -c a.c", ("0", False)),
        # An unknown option after the last -O and LTO option changes neither.
        ("-O0 -c main.c -frobnicate a b", ("0", False)),
        # After `--`, every word is an input, whatever it looks like.
        ("-O0 -c -- -O2", ("0", False)),
        ("-O0 -c -- -mllvm foo", ("0", False)),
        ("-O0 -c -- -Xclang -load", ("0", False)),
        ("-O0 -c -- -Xarch_arm64=-O2", ("0", False)),
        ("-O0 -c -- @args.rsp", ("0", False)),
        ("-O0 -c -- -include", ("0", False)),
    ],
)
def test_optimization_is_the_last_option_s(arguments, found):
    assert optimization(["/usr/bin/clang", *arguments.split()]) == found


@pytest.mark.parametrize(
    "arguments, why",
    [
        ("-O0 -mllvm -inline-threshold=0", "-mllvm -inline-threshold=0"),
        ("-O0 -Xclang -disable-O0-optnone", "-Xclang -disable-O0-optnone"),
        ("-O0 -Xclang -load -Xclang x.so", "-Xclang -load"),
        ("-O0 -Xarch_arm64 -O2", "-Xarch_arm64"),
        ("-O2 -o -O0", "-o -O0"),
        ("-O2 -Xlinker -O0", "-Xlinker -O0"),
        # An option not known to take no value may take the next word as one.
        ("-O2 -frobnicate-next -O0", "-O0 after -frobnicate-next"),
        ("-O2 --unknown -O0", "-O0 after --unknown"),
        ("-O0 -ffast-math -flto", "-flto after -ffast-math"),
        # clang takes `-O0` as this option's value, and builds at -O2.
        ("-O2 -fmodules-user-build-path -O0", "-fmodules-user-build-path -O0"),
        ("-O0 -flto=thin -fmodules-user-build-path -fno-lto", "-fmodules-user-build-path -fno-lto"),
        ("-O0 -Xclang -clang-vendor-feature=+x", "-Xclang -clang-vendor-feature=+x"),
        # clang takes `-O0` as the resource directory, and builds at -O2.
        ("-O2 -nostdinc -resource-dir -O0", "-resource-dir -O0"),
        ("-O2 -nostdinc -frobnicate -O0", "-O0 after -frobnicate"),
        # An option whose number of values isn't known may take any words after it.
        ("-O2 -fdepscan-prefix-map /old -O0 -nostdinc", "-O0 after -fdepscan-prefix-map"),
        ("-O0 -flto=thin -fdepscan-prefix-map /old -fno-lto", "-fno-lto after -fdepscan"),
        ("-O2 -frobnicate a b c -O0", "-O0 after -frobnicate"),
        ("-O2 -frobnicate -c main.c -Xclang -O0", "-O0 after -frobnicate"),
        # `--` ends the options only where no such option may take it.
        ("-O0 -fdepscan-prefix-map /old -- -O2 -nostdinc", "-O2 after -fdepscan-prefix-map"),
        ("-O0 -fdepscan-prefix-map /old -- -mllvm foo", "-mllvm foo"),
        ("-O0 -fdepscan-prefix-map /old -- -Xclang -O2", "-O2 after -fdepscan-prefix-map"),
        # Forwarded options joined to their forwarder, and an option without its value.
        ("-O0 -Xclang=-O2", "-Xclang=-O2"),
        ("-O0 -mllvm=-x", "-mllvm=-x"),
        ("-O0 -Xarch_arm64=-O2", "-Xarch_arm64=-O2"),
        ("-O0 -include", "-include without its value"),
        ("-O0 -Xclang", "-Xclang "),
        # What the recorded words should hold expanded can't be read from them.
        ("-O0 @args.rsp", "@args.rsp"),
        # clang-cl's mode reads its own options: `/clang:-O2` builds at -O2.
        ("--driver-mode=cl /clang:-O2 /c main.c", "--driver-mode=cl"),
        ("--driver-mode=flang -O0", "--driver-mode=flang"),
        ("-O0 --driver-mode cl", "--driver-mode"),
        ("--config=x.cfg -O0", "--config=x.cfg"),
        ("-O5", "-O5"),
        ("-Xclang", "-Xclang "),
    ],
)
def test_an_option_that_may_change_optimization_unread_is_refused(arguments, why):
    words = ["/usr/bin/clang", *arguments.split()]
    with pytest.raises(NotRecorded, match=re.escape(why) + ".*which Peeky can't judge"):
        optimization(words)


def test_line_tables_fed_in_pieces_read_alike():
    whole, pieces = LineFiles(), LineFiles()
    whole.feed(LINES)
    half = LINES.splitlines()
    pieces.feed(iter(half[:40]))
    pieces.feed(iter(half[40:]))
    assert whole.close() == pieces.close()


@pytest.mark.parametrize(
    "change",
    [
        (
            'include_directories[  1] = "inc"\nfile',
            'include_directories[  1] = "inc"\ninclude_directories[  1] = "inc"\nfile',
        ),
        ('file_names[  1]:\n           name: "a.h"', 'file_names[  0]:\n           name: "a.h"'),
        ("67fa149688f3de530af68560e7707f18", "67FA149688f3de530af68560e7707f18"),
        ("67fa149688f3de530af68560e7707f18", "67fa"),
        ('name: "a.h"', 'name: "a\\x41.h"'),
        ("      dir_index: 1\n   md5", "      dir_index: 7\n   md5"),
        ("debug_line[0x000000f8]", "debug_line[0x00000000]"),
        (
            'include_directories[  0] = "/w"\nfile_names[  0]:\n           name: "gram.c"',
            'file_names[  0]:\n           name: "gram.c"',
        ),
    ],
)
def test_malformed_line_tables_fail_closed(change):
    text = LINES.replace(*change, 1)
    assert text != LINES
    files = LineFiles()
    with pytest.raises(ValueError):
        files.feed(text)
        files.close()


def test_md5_of(tmp_path):
    source = tmp_path / "a.h"
    source.write_bytes(b"#define A_H 2\n" * 100_000)
    assert md5_of(source) == hashlib.md5(source.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="over 1000 bytes"):
        md5_of(source, limit=1000)


LINUX_WORDS = (
    "/usr/lib/llvm-18/bin/clang -O0 -g -gdwarf-5 -grecord-command-line -fdebug-macro -I inc "
    "-I inc\\ sp -isystem sys -include pre.h -D SP=a\\ b -U ZZ -std=gnu11 -Wall -MD -MF m.d "
    "-c main.c -o m.o"
)
APPLE_WORDS = (
    "/Library/Developer/CommandLineTools/usr/bin/clang --driver-mode=g++ -g -gdwarf-5 "
    "-grecord-command-line -O0 -isysroot /SDK -c a.cpp -Wno-elaborated-enum-base -Xclang "
    "-clang-vendor-feature=+thisNoNullAttr -Xclang -fno-odr-hash-protocols -Xclang "
    "-Wno-error=allocator-wrappers -mlinker-version=1166.2 -mmacos-version-min=27.0"
)


def _replayed(words, main="/w/main.c"):
    from peeky.native_units import _words, replay_arguments

    return replay_arguments(_words(words), "/w", main)


def test_a_unit_s_preprocessing_is_replayed_with_the_options_that_choose_what_it_reads():
    assert _replayed(LINUX_WORDS) == [
        "-O0",
        "-I",
        "inc",
        "-I",
        "inc sp",
        "-isystem",
        "sys",
        "-include",
        "pre.h",
        "-D",
        "SP=a b",
        "-U",
        "ZZ",
        "-std=gnu11",
        "main.c",
    ]
    # Apple's own options pass on, but those it gives its compiler, which define nothing.
    assert _replayed(APPLE_WORDS, "/w/a.cpp") == [
        "--driver-mode=g++",
        "-O0",
        "-isysroot",
        "/SDK",
        "-mlinker-version=1166.2",
        "-mmacos-version-min=27.0",
        "a.cpp",
    ]
    # The unit's own input, by its path, whatever others the command names.
    words = "/usr/bin/clang -x c -c ./src/../src/b.c ./src/a.c -fPIC -o x"
    assert _replayed(words, "/w/src/a.c") == ["-x", "c", "-fPIC", "./src/a.c"]
    assert _replayed("/usr/bin/clang -O0 -c -- -odd.c", "/w/-odd.c") == ["-O0", "-odd.c"]


@pytest.mark.parametrize(
    "words, why",
    [
        ("-fplugin=x.so -c main.c", "-fplugin=x.so"),
        ("-Xclang -load -Xclang x.so -c main.c", "-Xclang -load"),
        ("-Wp,-DX=1 -c main.c", "-Wp,-DX=1"),
        ("-Xpreprocessor -DX -c main.c", "-Xpreprocessor"),
        ("-ivfsoverlay o.yaml -c main.c", "-ivfsoverlay"),
        ("-B /tools -c main.c", "-B"),
        ("-working-directory /x -c main.c", "-working-directory"),
        ("-fmodules -c main.c", "-fmodules"),
        ("-A system(linux) -c main.c", "-A"),
        ("@args.rsp -c main.c", "@args.rsp"),
        ("-c main.c -- @args.rsp", "@args.rsp"),
        ("-c -", "its standard input as a file"),
        ("-c main.c -x c++ other.c", "-x c++ after its input"),
        ("-c other.c", "names no input that is its own file"),
        ("-c main.c -include", "-include without its value"),
        ("-mllvm -x -c main.c", "-mllvm"),
        ("--driver-mode=cl -c main.c", "--driver-mode=cl"),
    ],
)
def test_what_can_t_be_passed_leaves_the_unit_s_text_unread(words, why):
    with pytest.raises(NotRecorded, match=re.escape(why)):
        _replayed(f"/usr/bin/clang {words}")
