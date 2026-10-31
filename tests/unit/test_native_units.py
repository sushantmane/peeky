"""native_units: what a binary's debug information says of each compile unit (ADR 0008,
phase 1). The texts are llvm-dwarfdump 18's, of a unit clang 18 and GCC 13 built on Linux,
and Apple clang 21 on macOS."""

import hashlib

import pytest

from peeky.native_units import (
    LineFiles,
    NotRecorded,
    compiler_of,
    md5_of,
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
