"""DWARF 5 as Peeky reads it (ADR 0008): line tables of either version, indexed range and
location lists, address indexes resolved through `--debug-addr`, and any DWARF 5 form it
doesn't know refused, never dropped. The texts are llvm-dwarfdump 18's, of clang 18's
builds on Linux."""

import pytest

from peeky.native import (
    DieMap,
    _line_rows,
    _LineParser,
    _parse_dies,
    _prologue_kept,
)

V5_HEADER = (
    "0x00000000: Compile Unit: length = 0x00000381, format = DWARF32, version = 0x0005, "
    "unit_type = DW_UT_compile, abbr_offset = 0x0000, addr_size = 0x08 "
    "(next unit at 0x00000385)"
)
V4_HEADER = (
    "0x00000000: Compile Unit: length = 0x000004f6, format = DWARF32, version = 0x0004, "
    "abbr_offset = 0x0000, addr_size = 0x08 (next unit at 0x000004fa)"
)


def _unit(header, body, base="0x00000008"):
    """A unit's text: its header, its compile unit and ``body``, its children."""
    based = f"              DW_AT_addr_base\t({base})\n" if base else ""
    return (
        f"{header}\n\n"
        "0x0000000c: DW_TAG_compile_unit\n"
        '              DW_AT_name\t("c.c")\n'
        f"{based}"
        "              DW_AT_low_pc\t(0x0000000000000800)\n"
        "              DW_AT_high_pc\t(0x00000000000009d0)\n\n"
        f"{body}"
    )


FUNCTION = """0x00000030:   DW_TAG_subprogram
                DW_AT_low_pc\t(0x00000000000008a0)
                DW_AT_high_pc\t(0x00000000000009d0)
                DW_AT_frame_base\t(DW_OP_reg29 W29)
                DW_AT_name\t("main")

0x00000040:     DW_TAG_formal_parameter
                  DW_AT_location\t({start}
                     [0x00000000000008a0, 0x00000000000008bc): DW_OP_reg0 W0
                     [0x00000000000008bc, 0x00000000000009c4): DW_OP_reg19 W19
                     [0x00000000000009c4, 0x00000000000009d0): {entry}(DW_OP_reg0 W0), DW_OP_stack_value)
                  DW_AT_name\t("argc")

0x00000050:     DW_TAG_variable
                  DW_AT_location\t(DW_OP_fbreg -12)
                  DW_AT_name\t("framed")

0x00000060:     DW_TAG_variable
                  DW_AT_name\t("gone")

0x00000070:     DW_TAG_lexical_block
                  DW_AT_ranges\t({ranges}
                     [0x00000000000008c0, 0x00000000000008d0)
                     [0x0000000000000900, 0x0000000000000910))

0x00000080:       DW_TAG_variable
                    DW_AT_location\t({static})
                    DW_AT_name\t("calls")

0x00000090:       NULL

0x00000091:     NULL

0x00000092:   NULL
"""
V4_FUNCTION = FUNCTION.format(
    start="0x00000039: ",
    entry="DW_OP_GNU_entry_value",
    ranges="0x00000000",
    static="DW_OP_addr 0x20068",
)
V5_FUNCTION = FUNCTION.format(
    start="indexed (0x1) loclist = 0x00000052: ",
    entry="DW_OP_entry_value",
    ranges="indexed (0x0) rangelist = 0x00000010",
    static="DW_OP_addrx 0x1",
)
ADDRESSES = """
p:\tfile format elf64-littleaarch64

.debug_addr contents:
Address table header: length = 0x00000024, format = DWARF32, version = 0x0005, addr_size = 0x08, seg_size = 0x00
Addrs: [
0x0000000000020050
0x0000000000020068
0x0000000000020030
0x0000000000000718
]
Address table header: length = 0x0000001c, format = DWARF32, version = 0x0005, addr_size = 0x08, seg_size = 0x00
Addrs: [
0x00000000000200a0
0x0000000000020034
0x00000000000007b8
]
"""
PREFIX = "p:\tfile format elf64-littleaarch64\n\n.debug_info contents:\n"


def test_dwarf_5_reads_as_dwarf_4_does():
    four = _parse_dies(PREFIX + _unit(V4_HEADER, V4_FUNCTION, base=None))
    five = _parse_dies(PREFIX + _unit(V5_HEADER, V5_FUNCTION) + ADDRESSES)
    assert four.keys() == five.keys()
    for offset in four:
        assert four[offset]["ranges"] == five[offset]["ranges"], offset
        assert set(four[offset]["attrs"]) - {"DW_AT_addr_base"} == set(five[offset]["attrs"]) - {
            "DW_AT_addr_base"
        }
    # The block's range list, indexed in DWARF 5.
    assert five[0x70]["ranges"] == [(0x8C0, 0x8D0), (0x900, 0x910)]
    # The static's address index, from its unit's address table.
    assert five[0x80]["attrs"]["DW_AT_location"] == "DW_OP_addr 0x20068"
    assert four[0x80]["attrs"]["DW_AT_location"] == "DW_OP_addr 0x20068"
    # A location list is kept, never as a location for all of the code.
    for dies, entry in ((four, "DW_OP_GNU_entry_value"), (five, "DW_OP_entry_value")):
        assert "DW_AT_location" not in dies[0x40]["attrs"]
        assert dies[0x40]["locations"]["DW_AT_location"] == [
            (0x8A0, 0x8BC, "DW_OP_reg0 W0"),
            (0x8BC, 0x9C4, "DW_OP_reg19 W19"),
            (0x9C4, 0x9D0, f"{entry}(DW_OP_reg0 W0), DW_OP_stack_value"),
        ]
        assert dies[0x50]["attrs"]["DW_AT_location"] == "DW_OP_fbreg -12"


def test_a_location_list_still_counts_as_kept_by_the_prologue():
    # `_prologue_kept`'s meaning, either version: a parameter or local whose location is a
    # list (an optimized build's own account of each address) or none at all is kept; one
    # at one place in the frame is not, nor is a static's address read as a frame's.
    for dies in (
        _parse_dies(PREFIX + _unit(V4_HEADER, V4_FUNCTION, base=None)),
        _parse_dies(PREFIX + _unit(V5_HEADER, V5_FUNCTION) + ADDRESSES),
    ):
        assert _prologue_kept(dies, 0x30) == frozenset({"argc", "gone", "calls"})


def test_each_unit_reads_its_own_address_table():
    second = V5_HEADER.replace("0x00000000: Compile", "0x00000385: Compile")
    body = V5_FUNCTION.replace("0x000000", "0x000004")
    dies = _parse_dies(
        PREFIX
        + _unit(V5_HEADER, V5_FUNCTION)
        + _unit(second, body, base="0x00000030").replace(
            "0x0000000c: DW_TAG_compile_unit", "0x00000391: DW_TAG_compile_unit"
        )
        + ADDRESSES
    )
    assert dies[0x80]["attrs"]["DW_AT_location"] == "DW_OP_addr 0x20068"
    assert dies[0x480]["attrs"]["DW_AT_location"] == "DW_OP_addr 0x20034"


def test_a_constant_s_index_and_a_list_s_addresses_are_read_too():
    body = V5_FUNCTION.replace("DW_OP_reg19 W19\n", "DW_OP_addrx 0x2, DW_OP_stack_value\n").replace(
        "DW_OP_addrx 0x1)", "DW_OP_constx 0x3, DW_OP_form_tls_address)"
    )
    dies = _parse_dies(PREFIX + _unit(V5_HEADER, body) + ADDRESSES)
    assert dies[0x40]["locations"]["DW_AT_location"][1][2] == (
        "DW_OP_addr 0x20030, DW_OP_stack_value"
    )
    assert dies[0x80]["attrs"]["DW_AT_location"] == "DW_OP_constu 0x718, DW_OP_form_tls_address"


def test_a_dwarf64_address_table_is_placed_by_its_own_header():
    tables = ADDRESSES.replace("format = DWARF32", "format = DWARF64", 1)
    # The first table's header is 16 bytes: its addresses start at 0x10.
    dies = _parse_dies(PREFIX + _unit(V5_HEADER, V5_FUNCTION, base="0x00000010") + tables)
    assert dies[0x80]["attrs"]["DW_AT_location"] == "DW_OP_addr 0x20068"


@pytest.mark.parametrize(
    "change, why",
    [
        # An index with no table, or past its table's end.
        ((ADDRESSES, ""), "address index without its address"),
        (("DW_OP_addrx 0x1)", "DW_OP_addrx 0x9)"), "address index without its address"),
        (("DW_AT_addr_base\t(0x00000008)", "DW_AT_addr_base\t(0x00000099)"), "without its"),
        # A line, a value or a row dwarfdump writes where it can't read the DWARF.
        (('DW_AT_name\t("gone")', "DW_AT_something odd"), "doesn't know"),
        (("DW_OP_fbreg -12", "<decoding error> f4 28"), "decoding error"),
        (("DW_OP_fbreg -12", "indexed (0x3) string = 0x10"), "indexed"),
        (
            ("[0x00000000000008bc, 0x00000000000009c4): DW_OP_reg19", "<default>: DW_OP_reg19"),
            "location list's row",
        ),
        (("DW_OP_reg19 W19\n", "<decoding error> a0 61\n"), "location list's row"),
        (("[0x0000000000000900, 0x0000000000000910))", "[0x900, ...))"), "range list's row"),
        (("Addrs: [\n0x0000000000020050", "Addrs: [\nzero"), "address table"),
    ],
)
def test_a_dwarf_5_form_it_doesn_t_know_fails_the_parse(change, why):
    text = PREFIX + _unit(V5_HEADER, V5_FUNCTION) + ADDRESSES
    assert change[0] in text
    with pytest.raises(ValueError, match=why):
        _parse_dies(text.replace(*change, 1))


def test_dwarf_4_reading_is_as_lenient_as_before():
    # What a DWARF 4 unit's text holds that the parse doesn't know is passed over, as before.
    odd = V4_FUNCTION.replace('DW_AT_name\t("gone")', "DW_AT_something odd")
    dies = _parse_dies(PREFIX + _unit(V4_HEADER, odd, base=None))
    assert "DW_AT_name" not in dies[0x60]["attrs"]
    # A list's row it can't read ends the list unread, and is read as itself.
    broken = V4_FUNCTION.replace("DW_OP_reg19 W19\n", "DW_OP_reg19 (W19\n")
    assert "locations" not in _parse_dies(PREFIX + _unit(V4_HEADER, broken, base=None))[0x40]


def test_a_dwarf_5_value_naming_its_list_by_index_reads_as_dwarf_4_s():
    body = V5_FUNCTION.replace(
        "DW_AT_location\t(DW_OP_fbreg -12)",
        "DW_AT_location\t(indexed (0x2) loclist = 0x00000060: )",
    )
    dies = _parse_dies(PREFIX + _unit(V5_HEADER, body) + ADDRESSES)
    assert dies[0x50]["attrs"]["DW_AT_location"] == "0x00000060: "
    assert isinstance(dies, DieMap)


LINE_TABLE = """debug_line[0x00000000]
Line table prologue:
    total_length: 0x0000008f
          format: DWARF32
         version: 5
include_directories[  0] = "{root}"
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
0x0000000000000728      4     26      0   0             0       0  is_stmt prologue_end
0x0000000000000748      2      0      1   0             0       0  is_stmt
0x0000000000000760      2     33      1   0             0       0  end_sequence
"""


def _sources(root):
    (root / "inc").mkdir()
    (root / "main.c").write_text('#include "a.h"\n\n\nint main(void) { return twice(2); }\n')
    (root / "inc" / "a.h").write_text("static int twice(int x)\n{ return 2 * x; }\n")
    return ("main.c", "inc/a.h")


def test_a_dwarf_5_line_table_names_its_files_from_directory_0(tmp_path):
    declared = _sources(tmp_path)
    text = LINE_TABLE.format(root=tmp_path)
    assert _line_rows(text, tmp_path, declared) == [
        (0x718, "main.c", 4),
        (0x728, "main.c", 4),
        (0x748, "inc/a.h", 2),
    ]
    # Relative directories are under directory 0, wherever the workspace is.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    parser = _LineParser(tmp_path, declared)
    parser.feed(text.splitlines())
    assert parser.table().rows == tuple(_line_rows(text, tmp_path, declared))


@pytest.mark.parametrize(
    "change",
    [
        ('include_directories[  0] = "{root}"', 'include_directories[  0] = "rel"'),
        ('include_directories[  0] = "{root}"\n', ""),
        ("version: 5", "version: 6"),
        ('include_directories[  1] = "inc"', 'include_directories[  0] = "inc"'),
    ],
)
def test_a_dwarf_5_line_table_without_its_own_directory_fails(tmp_path, change):
    declared = _sources(tmp_path)
    text = LINE_TABLE.replace(*change, 1).format(root=tmp_path)
    with pytest.raises(ValueError):
        _line_rows(text, tmp_path, declared)
