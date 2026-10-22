"""#105: which scalar field or element of a C, C++ or Rust local `--set` may change,
decided from the debug information (fields.py), and how the worker writes and reads
it back."""

import re
from copy import deepcopy

import pytest

from peeky.errors import classify
from peeky.fields import (
    CPP_LIBRARIES,
    RUST_LIBRARIES,
    Unsettable,
    debugger_steps,
    local_types,
    referred_locals,
    settable_member,
    type_graph,
)
from peeky.native import NativeSession, _parse_dies
from peeky.worker import DebugWorker


def dump(*entries):
    """dwarfdump text from (offset, depth, tag, {attribute: value}) entries."""
    lines = []
    for offset, depth, tag, attributes in entries:
        lines.append(f"0x{offset:08x}:{' ' * (1 + 2 * depth)}{tag}")
        lines += [f"{' ' * (13 + 2 * depth)}{key}\t({value})" for key, value in attributes.items()]
        lines.append("")
    return "\n".join(lines)


def base(name, encoding, size):
    return {"DW_AT_name": f'"{name}"', "DW_AT_encoding": encoding, "DW_AT_byte_size": size}


def member(name, kind, at, **extra):
    return {"DW_AT_name": f'"{name}"', "DW_AT_type": f"0x{kind:08x}", **extra} | (
        {"DW_AT_data_member_location": f"0x{at:02x}"} if at is not None else {}
    )


INFO = dump(
    (0x0B, 0, "DW_TAG_compile_unit", {"DW_AT_name": '"t.c"'}),
    (0x10, 1, "DW_TAG_base_type", base("int", "DW_ATE_signed", "0x04")),
    (0x14, 1, "DW_TAG_base_type", base("long", "DW_ATE_signed", "0x08")),
    (0x18, 1, "DW_TAG_base_type", base("char", "DW_ATE_signed_char", "0x01")),
    (0x1C, 1, "DW_TAG_base_type", base("_Bool", "DW_ATE_boolean", "0x01")),
    (0x20, 1, "DW_TAG_base_type", base("double", "DW_ATE_float", "0x08")),
    (0x24, 1, "DW_TAG_base_type", base("unsigned short", "DW_ATE_unsigned", "0x02")),
    (0x28, 1, "DW_TAG_base_type", base("__int128", "DW_ATE_signed", "0x10")),
    (0x2C, 1, "DW_TAG_base_type", base("wchar_t", "DW_ATE_signed", "0x04")),
    (0x30, 1, "DW_TAG_typedef", {"DW_AT_type": "0x00000010", "DW_AT_name": '"myint"'}),
    (0x34, 1, "DW_TAG_const_type", {"DW_AT_type": "0x00000010"}),
    (0x38, 1, "DW_TAG_volatile_type", {"DW_AT_type": "0x00000010"}),
    (0x3C, 1, "DW_TAG_pointer_type", {"DW_AT_type": "0x00000060"}),
    (0x40, 1, "DW_TAG_typedef", {"DW_AT_type": "0x0000003c", "DW_AT_name": '"PairPtr"'}),
    (0x44, 1, "DW_TAG_reference_type", {"DW_AT_type": "0x00000060"}),
    (0x48, 1, "DW_TAG_enumeration_type", {"DW_AT_byte_size": "0x04", "DW_AT_name": '"color"'}),
    (0x50, 1, "DW_TAG_array_type", {"DW_AT_type": "0x00000010"}),
    (0x52, 2, "DW_TAG_subrange_type", {"DW_AT_count": "0x03"}),
    (0x58, 1, "DW_TAG_array_type", {"DW_AT_type": "0x00000010"}),
    (0x5A, 2, "DW_TAG_subrange_type", {"DW_AT_count": "0x02"}),
    (0x5C, 2, "DW_TAG_subrange_type", {"DW_AT_upper_bound": "0x01"}),
    (0x60, 1, "DW_TAG_structure_type", {"DW_AT_name": '"pair"', "DW_AT_byte_size": "0x10"}),
    (0x62, 2, "DW_TAG_member", member("x", 0x14, 0)),
    (0x64, 2, "DW_TAG_member", member("y", 0x14, 8)),
    (0x70, 1, "DW_TAG_structure_type", {"DW_AT_name": '"outer"', "DW_AT_byte_size": "0x38"}),
    (0x72, 2, "DW_TAG_member", member("start", 0x10, 0)),
    (0x74, 2, "DW_TAG_member", member("end", 0x30, 4)),
    (0x76, 2, "DW_TAG_member", member("in", 0x60, 8)),
    (0x78, 2, "DW_TAG_member", member("arr", 0x50, 24)),
    (0x7A, 2, "DW_TAG_member", member("grid", 0x58, 36)),
    (0x7C, 2, "DW_TAG_member", member("tiny", 0x24, 52)),
    (0x90, 1, "DW_TAG_structure_type", {"DW_AT_name": '"bits"', "DW_AT_byte_size": "0x08"}),
    (0x92, 2, "DW_TAG_member", member("a", 0x10, None, DW_AT_bit_size="0x03")),
    (0x94, 2, "DW_TAG_member", member("whole", 0x10, 4)),
    (0xA0, 1, "DW_TAG_structure_type", {"DW_AT_name": '"consts"', "DW_AT_byte_size": "0x08"}),
    (0xA2, 2, "DW_TAG_member", member("fixed", 0x34, 0)),
    (0xA4, 2, "DW_TAG_member", member("v", 0x38, 4)),
    (0xB0, 1, "DW_TAG_union_type", {"DW_AT_name": '"num"', "DW_AT_byte_size": "0x08"}),
    (0xB2, 2, "DW_TAG_member", member("i", 0x10, 0)),
    (0xB4, 2, "DW_TAG_member", member("d", 0x20, 0)),
    (0xC0, 1, "DW_TAG_structure_type", {"DW_AT_name": '"pk"', "DW_AT_byte_size": "0x05"}),
    (0xC2, 2, "DW_TAG_member", member("c", 0x18, 0)),
    (0xC4, 2, "DW_TAG_member", member("i", 0x10, 1)),
    (0xD0, 1, "DW_TAG_structure_type", {"DW_AT_name": '"misc"', "DW_AT_byte_size": "0x50"}),
    (0xD2, 2, "DW_TAG_member", member("flag", 0x1C, 0)),
    (0xD4, 2, "DW_TAG_member", member("ch", 0x18, 1)),
    (0xD6, 2, "DW_TAG_member", member("ratio", 0x20, 8)),
    (0xD8, 2, "DW_TAG_member", member("col", 0x48, 16)),
    (0xDA, 2, "DW_TAG_member", member("big", 0x28, 32)),
    (0xDC, 2, "DW_TAG_member", member("w", 0x2C, 48)),
    (0xDE, 2, "DW_TAG_member", member("p", 0x40, 56)),
    (0xE0, 2, "DW_TAG_member", member("r", 0x44, 64)),
    (0xF0, 1, "DW_TAG_namespace", {"DW_AT_name": '"std"'}),
    (
        0xF4,
        2,
        "DW_TAG_structure_type",
        {"DW_AT_name": '"pair<int, int>"', "DW_AT_byte_size": "0x08"},
    ),
    (0xF6, 3, "DW_TAG_member", member("first", 0x10, 0)),
    (0xF8, 3, "DW_TAG_member", member("second", 0x10, 4)),
    (0x100, 1, "DW_TAG_base_type", base("u8", "DW_ATE_unsigned", "0x01")),
    (0x104, 1, "DW_TAG_base_type", base("u32", "DW_ATE_unsigned", "0x04")),
    (
        0x110,
        1,
        "DW_TAG_structure_type",
        {"DW_AT_name": '"Packed"', "DW_AT_byte_size": "0x08", "DW_AT_alignment": "1"},
    ),
    (0x112, 2, "DW_TAG_member", member("a", 0x100, 0, DW_AT_alignment="1")),
    (0x114, 2, "DW_TAG_member", member("b", 0x104, 4, DW_AT_alignment="4")),
    (0x120, 1, "DW_TAG_structure_type", {"DW_AT_name": '"Shape"', "DW_AT_byte_size": "0x18"}),
    (0x122, 2, "DW_TAG_variant_part", {"DW_AT_discr": "0x00000124"}),
    (0x124, 3, "DW_TAG_member", {"DW_AT_type": "0x00000014", "DW_AT_data_member_location": "0x00"}),
    (0x130, 1, "DW_TAG_structure_type", {"DW_AT_name": '"Opaque"', "DW_AT_declaration": "true"}),
    (0x140, 1, "DW_TAG_structure_type", {"DW_AT_name": '"Derived"', "DW_AT_byte_size": "0x08"}),
    (0x142, 2, "DW_TAG_inheritance", {"DW_AT_type": "0x00000060"}),
    (0x144, 2, "DW_TAG_member", member("d", 0x10, 0)),
    (
        0x146,
        2,
        "DW_TAG_member",
        member("shared", 0x10, None, DW_AT_external="true", DW_AT_declaration="true"),
    ),
    (0x150, 1, "DW_TAG_array_type", {"DW_AT_type": "0x00000010"}),
    (0x152, 2, "DW_TAG_subrange_type", {}),
    (0x160, 1, "DW_TAG_const_type", {"DW_AT_type": "0x00000060"}),
)
ROOTS = (0x70, 0x40, 0x44, 0x90, 0xA0, 0xB0, 0xC0, 0xD0, 0xF4, 0x110, 0x120, 0x130, 0x140)
GRAPH = type_graph(_parse_dies(INFO), (*ROOTS, 0x150, 0x160), CPP_LIBRARIES)


@pytest.mark.parametrize(
    "root, steps, expected",
    [
        (0x70, ["end"], (True, 32, ("myint", "int"))),
        (0x70, ["in", "y"], (True, 64, ("long",))),
        (0x70, ["arr", "[2]"], (True, 32, ("int",))),
        (0x70, ["grid", "[1]", "[1]"], (True, 32, ("int",))),
        (0x70, ["tiny"], (False, 16, ("unsigned short",))),
        (0x90, ["whole"], (True, 32, ("int",))),
        (0xA0, [], None),
    ],
)
def test_plain_fields_and_elements_resolve_to_their_integer(root, steps, expected):
    if expected is None:
        with pytest.raises(Unsettable, match="not a single integer"):
            settable_member(GRAPH, root, "s", steps)
        return
    assert settable_member(GRAPH, root, "s", steps) == expected


@pytest.mark.parametrize(
    "root, steps, expected",
    [
        # Review of #138: an index as the stop's walk reads it, the debugger's `[N]`.
        (0x70, ["arr", "[01]"], ["arr", "[1]"]),
        (0x70, ["arr", "[-1]"], ["arr", "[2]"]),
        (0x70, ["arr", "[-3]"], ["arr", "[0]"]),
        (0x70, ["grid", "[-1]", "[-2]"], ["grid", "[1]", "[0]"]),
        (0x70, ["in", "y"], ["in", "y"]),
        # Past the start, or no length to count from: not told here.
        (0x70, ["arr", "[-4]"], None),
        (0x150, ["[-1]"], None),
        (0x150, ["[007]"], ["[7]"]),
        # Anything else is left for settable_member to judge.
        (0x40, ["[-1]", "x"], ["[-1]", "x"]),
        (0x70, ["nope", "[-1]"], ["nope", "[-1]"]),
        (0x70, ["arr", "x"], ["arr", "x"]),
    ],
)
def test_a_selectors_indexes_are_read_as_the_debugger_names_elements(root, steps, expected):
    assert debugger_steps(GRAPH, root, "s", steps) == expected


@pytest.mark.parametrize(
    "root, steps, words",
    [
        # Nothing reached through a pointer, a typedef'd one or a reference (#101).
        (0x40, ["x"], "`s` is a pointer"),
        (0x44, ["x"], "`s` is a reference"),
        (0xD0, ["p"], "`s.p` is a pointer; only integers"),
        (0x90, ["a"], "`s.a` is a bitfield"),
        (0xA0, ["fixed"], "`s.fixed` is const"),
        (0x160, ["x"], "`s` is const"),
        (0xA0, ["v"], "`s.v` is volatile"),
        (0xB0, ["i"], "`s` is a union"),
        (0xC0, ["i"], "`s` is a packed struct (pk)"),
        (0x110, ["b"], "`s` is a packed struct (Packed)"),
        (0x120, ["x"], "`s` is an enum (Shape)"),
        (0xF4, ["first"], "standard-library type (pair<int, int>)"),
        (0x130, ["x"], "no definition of Opaque"),
        (0xD0, ["flag"], "boolean (_Bool)"),
        (0xD0, ["ch"], "character (char)"),
        (0xD0, ["w"], "character (wchar_t)"),
        (0xD0, ["ratio"], "floating-point value (double)"),
        (0xD0, ["col"], "`s.col` is an enum"),
        (0xD0, ["big"], "only 16-, 32- and 64-bit integers"),
        (0x70, ["arr", "[3]"], "`s.arr[3]` is out of range: `s.arr` has 3 elements"),
        (0x70, ["grid", "[1]"], "`s.grid[1]` is an array, not a single value"),
        (0x70, ["arr", "x"], "select an element as `s.arr[0]`"),
        (0x70, ["nope"], "lists no field `nope` in outer"),
        # A base class's members and static members are not the object's own fields.
        (0x140, ["pair"], "lists no field `pair` in Derived"),
        (0x140, ["shared"], "lists no field `shared` in Derived"),
        (0x150, ["[0]"], "whose size the debug information omits"),
    ],
)
def test_everything_else_is_refused_with_its_reason(root, steps, words):
    with pytest.raises(Unsettable) as caught:
        settable_member(GRAPH, root, "s", steps)
    assert words in str(caught.value)


DECLARED = dump(
    (0x0B, 0, "DW_TAG_compile_unit", {"DW_AT_name": '"a.cpp"'}),
    (0x10, 1, "DW_TAG_base_type", base("int", "DW_ATE_signed", "0x04")),
    (0x14, 1, "DW_TAG_base_type", base("__ARRAY_SIZE_TYPE__", "DW_ATE_unsigned", "0x08")),
    (0x20, 1, "DW_TAG_namespace", {"DW_AT_name": '"app"'}),
    (0x24, 2, "DW_TAG_class_type", {"DW_AT_name": '"Widget"', "DW_AT_declaration": "true"}),
    (0x28, 2, "DW_TAG_class_type", {"DW_AT_name": '"Twice"', "DW_AT_declaration": "true"}),
    (0x30, 0, "DW_TAG_compile_unit", {"DW_AT_name": '"b.cpp"'}),
    (0x34, 1, "DW_TAG_namespace", {"DW_AT_name": '"app"'}),
    (0x38, 2, "DW_TAG_class_type", {"DW_AT_name": '"Widget"', "DW_AT_byte_size": "0x04"}),
    (0x3C, 3, "DW_TAG_member", member("n", 0x10, 0)),
    (0x40, 2, "DW_TAG_class_type", {"DW_AT_name": '"Twice"', "DW_AT_byte_size": "0x04"}),
    (0x44, 3, "DW_TAG_member", member("n", 0x10, 0)),
    (0x48, 2, "DW_TAG_class_type", {"DW_AT_name": '"Twice"', "DW_AT_byte_size": "0x04"}),
    (0x4C, 3, "DW_TAG_member", member("m", 0x10, 0)),
    (0x50, 1, "DW_TAG_subprogram", {"DW_AT_name": '"f"'}),
    (0x54, 2, "DW_TAG_variable", {"DW_AT_name": '"__vla_expr0"', "DW_AT_artificial": "true"}),
    (0x58, 2, "DW_TAG_variable", {"DW_AT_name": '"vla"', "DW_AT_type": "0x00000060"}),
    (0x60, 1, "DW_TAG_array_type", {"DW_AT_type": "0x00000010"}),
    (0x62, 2, "DW_TAG_subrange_type", {"DW_AT_type": "0x00000014", "DW_AT_count": "0x00000054"}),
    # A constant count printed with eight digits is a count, not a reference.
    (0x70, 1, "DW_TAG_array_type", {"DW_AT_type": "0x00000010"}),
    (0x72, 2, "DW_TAG_subrange_type", {"DW_AT_count": "0x00010000"}),
)


def test_a_declared_class_is_read_from_its_one_definition_and_a_vla_is_refused():
    graph = type_graph(_parse_dies(DECLARED), (0x24, 0x28, 0x60, 0x70), CPP_LIBRARIES)
    # clang's -fno-standalone-debug declares a class whose definition another unit has.
    assert settable_member(graph, 0x24, "w", ["n"]) == (True, 32, ("int",))
    # Two definitions of one name (an ODR violation): neither is chosen.
    with pytest.raises(Unsettable, match="no definition of Twice"):
        settable_member(graph, 0x28, "t", ["n"])
    # Review of #105: named as what it is, not as a value kept outside the frame.
    with pytest.raises(Unsettable, match="`vla` is a variable-length array"):
        settable_member(graph, 0x60, "vla", ["[0]"])
    assert settable_member(graph, 0x70, "big", ["[65535]"]) == (True, 32, ("int",))


NAMESPACES = dump(
    (0x0B, 0, "DW_TAG_compile_unit", {"DW_AT_name": '"a.cpp"'}),
    (0x10, 1, "DW_TAG_base_type", base("int", "DW_ATE_signed", "0x04")),
    (0x20, 1, "DW_TAG_namespace", {"DW_AT_name": '"core"'}),
    (0x24, 2, "DW_TAG_structure_type", {"DW_AT_name": '"Config"', "DW_AT_byte_size": "0x04"}),
    (0x28, 3, "DW_TAG_member", member("level", 0x10, 0)),
    (0x30, 1, "DW_TAG_namespace", {"DW_AT_name": '"alloc"'}),
    (0x34, 2, "DW_TAG_structure_type", {"DW_AT_name": '"Pool"', "DW_AT_byte_size": "0x04"}),
    (0x38, 3, "DW_TAG_member", member("size", 0x10, 0)),
)


def test_a_cpp_namespace_named_core_or_alloc_is_the_programs_own():
    # Review of #105: `core::Config cfg; --set cfg.level=60` was refused as std's.
    dies = _parse_dies(NAMESPACES)
    graph = type_graph(dies, (0x24, 0x34), CPP_LIBRARIES)
    assert settable_member(graph, 0x24, "cfg", ["level"]) == (True, 32, ("int",))
    assert settable_member(graph, 0x34, "pool", ["size"]) == (True, 32, ("int",))
    # In Rust they are the standard library's crates.
    graph = type_graph(dies, (0x24, 0x34), RUST_LIBRARIES)
    with pytest.raises(Unsettable, match="standard-library type"):
        settable_member(graph, 0x24, "cfg", ["level"])


def test_a_derived_classs_own_field_is_settable():
    assert settable_member(GRAPH, 0x140, "d", ["d"]) == (True, 32, ("int",))


def test_a_type_the_graph_did_not_reach_is_refused():
    with pytest.raises(Unsettable, match="did not read the type"):
        settable_member({}, 0x70, "s", ["end"])
    # The walk stops at pointers: what one points at is never read.
    assert 0x60 in GRAPH and type_graph(_parse_dies(INFO), (0x3C,), CPP_LIBRARIES).keys() == {0x3C}


def NAMED(name, line=None):
    return {"DW_AT_name": f'"{name}"', "DW_AT_type": "0x00000010"} | (
        {"DW_AT_decl_file": '"/w/src/t.c"', "DW_AT_decl_line": str(line)} if line else {}
    )


FUNCTION = dump(
    (0x0B, 0, "DW_TAG_compile_unit", {"DW_AT_name": '"t.c"'}),
    (0x10, 1, "DW_TAG_base_type", base("int", "DW_ATE_signed", "0x04")),
    (0x20, 1, "DW_TAG_subprogram", {"DW_AT_name": '"f"', "DW_AT_low_pc": "0x1000"}),
    (0x22, 2, "DW_TAG_formal_parameter", {"DW_AT_location": "DW_OP_fbreg -8"} | NAMED("p")),
    (0x24, 2, "DW_TAG_variable", {"DW_AT_location": "DW_OP_fbreg -16"} | NAMED("a", 3)),
    (0x26, 2, "DW_TAG_variable", {"DW_AT_location": "DW_OP_addr 0x4000"} | NAMED("kept")),
    (0x28, 2, "DW_TAG_variable", {"DW_AT_location": "DW_OP_reg0 W0"} | NAMED("held")),
    (0x2A, 2, "DW_TAG_variable", NAMED("nowhere")),
    (0x2C, 2, "DW_TAG_lexical_block", {}),
    (0x2E, 3, "DW_TAG_variable", {"DW_AT_location": "DW_OP_breg31 WSP+8"} | NAMED("a", 9)),
    (
        0x30,
        3,
        "DW_TAG_variable",
        {"DW_AT_location": "DW_OP_breg31 WSP+16, DW_OP_deref"} | NAMED("big"),
    ),
    (0x32, 2, "DW_TAG_inlined_subroutine", {}),
    (0x34, 3, "DW_TAG_variable", {"DW_AT_location": "DW_OP_fbreg -24"} | NAMED("inlined")),
)


def test_locals_are_listed_with_their_type_and_where_the_frame_keeps_them():
    dies = _parse_dies(FUNCTION)
    types = local_types(dies, [0x20])
    assert types == {
        "p": frozenset({(0x10, "parameter", None)}),
        # Two `a` in nested blocks, both in the frame: one type, each where it is declared.
        "a": frozenset({(0x10, "automatic", ("t.c", 3)), (0x10, "automatic", ("t.c", 9))}),
        "big": frozenset({(0x10, "automatic", None)}),
        "kept": frozenset({(0x10, "static", None)}),
        "held": frozenset({(0x10, "register", None)}),
        "nowhere": frozenset({(0x10, "other", None)}),
    }
    # A call inlined into the function keeps its own locals.
    assert "inlined" not in types


A, B = ("main.rs", 79), ("main.rs", 81)


@pytest.mark.parametrize(
    "declared, name, words",
    [
        ({}, "s", "declares no local `s`"),
        ({"s": frozenset({(0x60, "automatic", A), (0x70, "automatic", B)})}, "s", "different"),
        ({"s": frozenset({(None, "automatic", A)})}, "s", "gives the local `s` no type"),
        ({"s": frozenset({(0x60, "automatic", A)})}, "s @ main.rs:80", "no local `s @ main"),
    ],
)
def test_a_root_must_be_one_local_of_one_type(declared, name, words):
    with pytest.raises(Unsettable, match=words):
        NativeSession._declared_type(declared, name)


def test_a_shadowed_root_is_told_apart_by_where_it_is_declared():
    # Review of #105: LLDB names `s @ main.rs:81` when several `s` are in scope.
    both = {"s": frozenset({(0x60, "automatic", A), (0x70, "automatic", B)})}
    assert NativeSession._declared_type(both, "s @ main.rs:81") == (0x70, True)
    assert NativeSession._declared_type(both, "s @ main.rs:79") == (0x60, True)
    static = {"s": frozenset({(0x60, "automatic", A), (0x60, "other", B)})}
    assert NativeSession._declared_type(static, "s") == (0x60, False)
    assert NativeSession._declared_type(static, "s @ main.rs:79") == (0x60, True)


def test_the_declaration_the_debugger_shows_picks_among_types():
    # #158: one name declared with two types in two blocks: the declaration shown at the
    # stop (its file and line, as shown_declaration gives it) alone counts; two declared
    # on one line cannot be told apart, nor can any where none is shown.
    both = {"s": frozenset({(0x60, "automatic", A), (0x70, "automatic", B)})}
    assert NativeSession._declared_type(both, "s", ("src/main.rs", 81, True)) == (0x70, True)
    assert NativeSession._declared_type(both, "s", ("main.rs", 79, True)) == (0x60, True)
    with pytest.raises(Unsettable, match="Peeky cannot tell which one the debugger shows"):
        NativeSession._declared_type(both, "s", None)
    one_line = {"s": frozenset({(0x60, "automatic", A), (0x70, "automatic", A)})}
    with pytest.raises(Unsettable, match="different types on line 79, which Peeky cannot"):
        NativeSession._declared_type(one_line, "s", ("main.rs", 79, True))


class Backend:
    """An lldb-dap-shaped adapter: `s` (a struct) with `start` and `end`; ``stores``
    says whether a setVariable reaches what later reads show, ``shown`` overrides
    what they show after it."""

    def __init__(self, root, stores=True, shown=None):
        self.frames = [{"id": 1, "name": "main", "line": 4, "source": {"path": str(root)}}]
        self.fields = {"start": "1", "end": "2"}
        self.stores, self.shown, self.calls, self.writes = stores, shown, [], 0

    def request(self, operation, args):
        self.calls.append((operation, deepcopy(args)))
        if operation == "stackTrace":
            return {"stackFrames": deepcopy(self.frames)}
        if operation == "scopes":
            return {
                "scopes": [
                    {"name": "Locals", "presentationHint": "locals", "variablesReference": 5}
                ]
            }
        if operation == "variables" and args["variablesReference"] == 5:
            struct = {"name": "s", "type": "pair", "value": "pair @ 0x10", "variablesReference": 7}
            return {"variables": [struct | {"memoryReference": "0x10"}]}
        if operation == "variables" and args["variablesReference"] == 7:
            fields = dict(self.fields)
            if self.writes and self.shown is not None:
                fields["end"] = self.shown
            return {
                "variables": [
                    {"name": n, "type": "long", "value": v, "variablesReference": 0}
                    | {"memoryReference": "0x18"}
                    for n, v in fields.items()
                ]
            }
        if operation == "setVariable":
            assert args["variablesReference"] == 7
            self.writes += 1
            if self.stores:
                self.fields[args["name"]] = args["value"]
            return {"value": args["value"]}
        raise AssertionError(operation)

    def close(self):
        pass


class Runtime:
    """NativeSession's value hooks; ``validate_member`` records what it was given."""

    validate_value = NativeSession.validate_value
    mutation_literal = NativeSession.mutation_literal
    confirm_mutation = NativeSession.confirm_mutation
    requires_memory_location = True
    reports_unavailable_as_text = True

    def __init__(self, refuse=None):
        self.seen, self.refuse = [], refuse

    def local_scopes(self, scopes):
        return scopes

    def validate_stop(self, frame):
        pass

    def assert_current(self):
        pass

    def close(self):
        pass

    def validate_member(self, frame, root, steps, variable, value, stack=None, deeper=None):
        self.seen.append((frame["id"], root["name"], steps, variable["name"], value))
        if self.refuse:
            raise ValueError(self.refuse)


def worker_for(tmp_path, **options):
    refuse = options.pop("refuse", None)
    instance = DebugWorker(tmp_path)
    instance.dap, instance.runtime = Backend(tmp_path, **options), Runtime(refuse)
    instance.session, instance.stop_id, instance.state = "session", "stop", "stopped"
    instance.thread_id = 1
    instance.capabilities = {"supports_set_variable": True, "supports_member_set": True}
    return instance


STOP = {"session_id": "session", "stop_id": "stop"}


def handle_of(worker, name="s"):
    snapshot = worker.dispatch("inspect", {**STOP, "names": [name]})
    return next(v for s in snapshot["scopes"] for v in s["variables"] if v["name"] == name)[
        "variablesReference"
    ]


def test_a_field_is_written_then_read_back_from_a_fresh_walk(tmp_path):
    worker = worker_for(tmp_path)
    changed = worker.dispatch(
        "set_variable", {**STOP, "handle": handle_of(worker), "name": "end", "value": "+10"}
    )
    assert changed == {"name": "end", "before": "2", "after": "10"}
    assert worker.runtime.seen == [(1, "s", ["end"], "end", "+10")]
    # The canonical decimal is sent; the read-back walks the frame's scopes again.
    write = next(i for i, (op, _) in enumerate(worker.dap.calls) if op == "setVariable")
    assert worker.dap.calls[write][1]["value"] == "10"
    assert ("scopes", {"frameId": 1}) in worker.dap.calls[write:]


def test_a_refused_member_is_never_written(tmp_path):
    worker = worker_for(tmp_path, refuse="`s.end` cannot be changed: `s` is a union")
    with pytest.raises(ValueError, match="is a union") as caught:
        worker.dispatch(
            "set_variable", {**STOP, "handle": handle_of(worker), "name": "end", "value": "3"}
        )
    assert classify(caught.value)["code"] == "MUTATION_REJECTED"
    assert not any(op == "setVariable" for op, _ in worker.dap.calls)


def test_a_write_the_program_does_not_keep_is_never_reported_as_a_change(tmp_path):
    # The debugger echoes the value, but reading it again shows the old one.
    worker = worker_for(tmp_path, stores=False)
    with pytest.raises(ValueError, match="did not change 'end'"):
        worker.dispatch(
            "set_variable", {**STOP, "handle": handle_of(worker), "name": "end", "value": "10"}
        )
    # Another value than the one asked: the run's state is unknown, so it fails closed.
    worker = worker_for(tmp_path, shown="11")
    with pytest.raises(RuntimeError, match="stored 11 in 'end', not 10"):
        worker.dispatch(
            "set_variable", {**STOP, "handle": handle_of(worker), "name": "end", "value": "10"}
        )


def test_a_read_back_that_cannot_be_made_fails_closed(tmp_path):
    worker = worker_for(tmp_path)
    handle = handle_of(worker)
    request = worker.dap.request

    def moved(operation, args):
        if operation == "stackTrace" and worker.dap.writes:
            # The frame's identity changed after the write: the read-back walk fails.
            return {"stackFrames": [{**worker.dap.frames[0], "name": "other"}]}
        return request(operation, args)

    worker.dap.request = moved
    with pytest.raises(RuntimeError, match="was sent, but could not be read back"):
        worker.dispatch("set_variable", {**STOP, "handle": handle, "name": "end", "value": "10"})


def test_an_unavailable_root_or_a_value_view_is_refused_before_any_write(tmp_path):
    worker = worker_for(tmp_path)
    worker.children[99] = ("view", [])
    with pytest.raises(ValueError, match="inside a value Peeky decodes itself"):
        worker.dispatch("set_variable", {**STOP, "handle": 99, "name": "end", "value": "3"})
    worker = worker_for(tmp_path)
    handle = handle_of(worker)
    worker.dap.fields["end"] = "<error: variable not available>"
    with pytest.raises(ValueError, match="'end' has no readable value here"):
        worker.dispatch("set_variable", {**STOP, "handle": handle, "name": "end", "value": "3"})
    assert not any(op == "setVariable" for op, _ in worker.dap.calls)


class Previewing(Runtime):
    """A runtime that previews `s` from "memory" (#106): ``memory`` overrides what the
    preview reads after a write; ``writable`` is what ``view_writable`` says."""

    def __init__(self, backend, writable=True, memory=None):
        super().__init__()
        self.backend, self.writable, self.memory, self.views = backend, writable, memory, 0

    def view_variable(self, variable, fetch, read=None, **options):
        if variable.get("name") != "s":
            return None
        self.views += 1
        fields = dict(self.backend.fields)
        if self.backend.writes and self.memory is not None:
            fields["end"] = self.memory
        return {
            "name": "s",
            "type": "pair",
            "variablesReference": 0,
            "value": f"Pair {{ start: {fields['start']}, end: {fields['end']} }}",
            "rust_view": "memory",
            "children": [{"name": n, "type": "i64", "value": v} for n, v in fields.items()],
            "struct_layout": "struct:pair",
        }

    def view_writable(self, view):
        return self.writable


def previewing(tmp_path, **options):
    worker = worker_for(tmp_path)
    worker.runtime = Previewing(worker.dap, **options)
    return worker


def test_a_field_of_a_struct_preview_is_changed_as_the_debugger_lists_it(tmp_path):
    # #105 with #106: a struct local previewed from memory is its own storage; the
    # change walks the debugger's variable, and the preview is read again after it.
    worker = previewing(tmp_path)
    changed = worker.dispatch(
        "set_variable", {**STOP, "handle": handle_of(worker), "name": "end", "value": "10"}
    )
    assert changed == {"name": "end", "before": "2", "after": "10"}
    assert worker.runtime.seen == [(1, "s", ["end"], "end", "10")]
    write = next(args for op, args in worker.dap.calls if op == "setVariable")
    assert write["variablesReference"] == 7  # the debugger's own `s`
    assert worker.runtime.views == 2  # shown, then read again after the change


def test_a_preview_that_reads_otherwise_after_a_change_fails_closed(tmp_path):
    worker = previewing(tmp_path, memory="2")
    with pytest.raises(RuntimeError, match="reads back as 10 from the debugger but as 2 from"):
        worker.dispatch(
            "set_variable", {**STOP, "handle": handle_of(worker), "name": "end", "value": "10"}
        )


def test_a_previews_fields_say_why_no_stop_changes_them(tmp_path):
    # #138 with #106: the fields of a struct previewed from memory whose root the build
    # keeps in a register are marked so (`set` refuses them), nested fields too; a
    # settable field never is.
    worker = previewing(tmp_path)
    worker.runtime.refuse = "`s` is a parameter kept in a register"
    worker.runtime.never_settable_row = lambda variable, frame, member=False: (
        "register" if (variable["name"], member) == ("s", True) else None
    )
    worker.runtime.view_variable = lambda variable, fetch, read=None, **options: (
        {
            "name": "s",
            "type": "pair",
            "variablesReference": 0,
            "value": "Pair { start: 1, end: Inner { v: 2 } }",
            "rust_view": "memory",
            "children": [
                {"name": "start", "type": "i64", "value": "1"},
                {
                    "name": "end",
                    "type": "Inner",
                    "value": "Inner { v: 2 }",
                    "children": [{"name": "v", "type": "i64", "value": "2"}],
                },
            ],
        }
        if variable.get("name") == "s"
        else None
    )
    page = worker.dispatch("expand", {**STOP, "handle": handle_of(worker)})["variables"]
    assert [(row["name"], row.get("settable"), row.get("not_settable")) for row in page] == [
        ("start", False, "register"),
        ("end", False, "register"),
    ]
    nested = worker.dispatch("expand", {**STOP, "handle": page[1]["variablesReference"]})
    assert [row.get("not_settable") for row in nested["variables"]] == ["register"]
    # A field `set` would change is never marked so.
    worker = previewing(tmp_path)
    worker.runtime.never_settable_row = lambda variable, frame, member=False: "register"
    page = worker.dispatch("expand", {**STOP, "handle": handle_of(worker)})["variables"]
    assert [(row.get("settable"), row.get("not_settable")) for row in page] == [(True, None)] * 2


def test_a_value_view_that_is_no_structs_own_storage_is_never_written(tmp_path):
    # A Vec's, String's or Box's contents, an enum's variant: refused before any write.
    worker = previewing(tmp_path, writable=False)
    with pytest.raises(ValueError, match="inside a value Peeky decodes itself"):
        worker.dispatch(
            "set_variable", {**STOP, "handle": handle_of(worker), "name": "end", "value": "10"}
        )
    assert not any(op == "setVariable" for op, _ in worker.dap.calls)


def test_a_container_field_is_found_only_through_plain_records():
    # #156: `self.values` behind `&mut self`, `self.inner.items`, `self.arr[1]`; never
    # into a standard-library type (a RefCell's contents change behind `&self`), an
    # enum, or through a pointer on the way.
    from peeky.fields import held_container

    vec = "alloc::vec::Vec<i64, alloc::alloc::Global>"

    def record(name, members, library=None, variant=False, qualified=None):
        fields = tuple((field, kind, offset, False, 8, None) for field, kind, offset in members)
        holds = tuple(kind for _, kind, _ in members)
        size = 24 * max(len(members), 1)
        return ("record", name, "struct", size, 8, fields, holds, variant, False, library, ())

    graph = {
        1: ("pointer", "&mut app::Window", 2),
        2: record(
            "Window",
            [("values", 3, 0), ("inner", 4, 24), ("cell", 5, 48), ("maybe", 6, 72)]
            + [("arr", 7, 96), ("next", 8, 144)],
        )
        + ("app::Window",),
        3: record("Vec<i64, alloc::alloc::Global>", [], "alloc") + (vec,),
        4: record("Inner", [("items", 3, 0)]) + ("app::Inner",),
        5: record("RefCell<Vec<i64>>", [("value", 3, 0)], "core") + ("core::cell::RefCell",),
        6: record("Option<Vec<i64>>", [], variant=True) + ("core::option::Option",),
        7: ("array", "", 3, (2,), False),
        8: ("pointer", "*const app::Window", 2),
    }
    assert held_container(graph, 1, "self", ["values"], dereference=True) == vec
    assert held_container(graph, 1, "self", ["inner", "items"], dereference=True) == vec
    assert held_container(graph, 1, "self", ["arr", "[1]"], dereference=True) == vec
    assert held_container(graph, 2, "w", ["values"]) == vec
    # Any other record is named too: only a container's layout reads it.
    assert held_container(graph, 1, "self", ["inner"], dereference=True) == "app::Inner"
    refused = {
        ("cell", "value"): "`self.cell` is a standard-library type (RefCell<Vec<i64>>)",
        ("maybe", "__0"): "`self.maybe` is an enum",
        ("next", "values"): "`self.next` is a pointer",
        ("arr", "[2]"): "`self.arr` has no element [2]",
        ("arr",): "`self.arr` is not a container",
    }
    for steps, why in refused.items():
        with pytest.raises(Unsettable, match=re.escape(why)):
            held_container(graph, 1, "self", list(steps), dereference=True)
    with pytest.raises(Unsettable, match="`self` is a pointer"):
        held_container(graph, 1, "self", ["values"])
    # #260: a slice parameter is a data pointer and a length, which the debug information
    # gives a type; what it holds reads as its elements, not through `data_ptr`.
    graph[9] = record("&mut [app::Window]", [("data_ptr", 1, 0), ("length", 3, 8)]) + (
        "&mut [app::Window]",
    )
    with pytest.raises(Unsettable) as refused:
        held_container(graph, 9, "friends", ["data_ptr", "values"], dereference=True)
    assert "no type it points at" not in str(refused.value)
    assert "`friends` is a slice" in str(refused.value) and "`friends[0]`" in str(refused.value)


def test_a_rust_raw_pointer_is_told_from_a_reference_by_its_debug_information_name():
    # #255: LLDB names `*const i64` and `&i64` both `long *`; the debug information
    # names each. A fat pointer's address has no name: the fat pointer's says.
    from peeky.fields import rust_raw_pointer

    def record(name, members, variant=False):
        fields = tuple(
            (field, kind, 8 * at, False, 8, None) for at, (field, kind) in enumerate(members)
        )
        holds = tuple(kind for _, kind in members)
        return ("record", name, "struct", 8 * len(members), 8, fields, holds, variant, False, None)

    graph = {
        1: record(
            "Holder",
            [("n", 10), ("p", 11), ("r", 12), ("b", 13), ("ps", 14), ("text", 15)]
            + [("q", 17), ("slot", 18), ("odd", 24)],
        ),
        10: ("base", "i64", "DW_ATE_signed", 8),
        11: ("pointer", "*const i64", 10),
        12: ("pointer", "&i64", 10),
        13: ("pointer", "alloc::boxed::Box<i64, alloc::alloc::Global>", 10),
        14: ("array", "", 11, (2,), False),
        15: record("*const str", [("data_ptr", 20), ("length", 10)]),
        16: record("&str", [("data_ptr", 20), ("length", 10)]),
        17: record("NonNull<i64>", [("pointer", 21)]),
        18: record("Slot", [], variant=True),
        19: ("pointer", "&*const i64", 11),
        20: ("pointer", "", 22),
        21: ("pointer", "*mut i64", 10),
        22: ("base", "u8", "DW_ATE_unsigned", 1),
        23: ("pointer", "&&i64", 12),
        24: record("Odd", [("at", 20)]),
        25: ("pointer", "*mut Holder", 1),
        26: ("pointer", "&Holder", 1),
    }
    cases = [
        (1, ["p"], True),
        (1, ["r"], False),
        (1, ["b"], False),
        (1, ["ps", "[1]"], True),
        (1, ["text", "data_ptr"], True),
        (16, ["data_ptr"], False),
        (1, ["q", "pointer"], True),
        # LLDB lists a pointer to a scalar or a pointer as one child, `*p`.
        (19, ["*r"], True),
        (23, ["*r"], False),
        # A struct's fields LLDB lists under a pointer to it: read through that pointer.
        (26, ["p"], True),
        (26, ["r"], False),
        (25, ["r"], True),
        # Not a pointer, not reached, or not named: the debug information does not say.
        (1, ["n"], None),
        (1, ["ps"], None),
        (1, ["slot", "__0"], None),
        (1, ["missing"], None),
        (1, ["odd", "at"], None),
        (12, ["*r"], None),
        (1, [], None),
    ]
    for root, steps, expected in cases:
        assert rust_raw_pointer(graph, root, steps) is expected, (root, steps)
    assert rust_raw_pointer({}, 1, ["p"]) is None


CONTAINERS = dump(
    (0x0B, 0, "DW_TAG_compile_unit", {"DW_AT_name": '"c.cpp"'}),
    (0x10, 1, "DW_TAG_base_type", base("int", "DW_ATE_signed", "0x04")),
    (0x14, 1, "DW_TAG_structure_type", {"DW_AT_name": '"Pt"', "DW_AT_byte_size": "0x04"}),
    (0x18, 2, "DW_TAG_member", member("x", 0x10, 0)),
    (0x20, 1, "DW_TAG_namespace", {"DW_AT_name": '"std"'}),
    (0x24, 2, "DW_TAG_namespace", {"DW_AT_name": '"__1"'}),
    (0x28, 3, "DW_TAG_class_type", {"DW_AT_name": '"vector<Pt>"', "DW_AT_byte_size": "0x18"}),
    (
        0x2C,
        4,
        "DW_TAG_template_type_parameter",
        {"DW_AT_type": "0x00000014", "DW_AT_name": '"_Tp"'},
    ),
    (0x30, 4, "DW_TAG_member", member("__begin_", 0x40, 0)),
    (0x34, 3, "DW_TAG_class_type", {"DW_AT_name": '"array<int, 3UL>"', "DW_AT_byte_size": "0x0c"}),
    (
        0x38,
        4,
        "DW_TAG_template_type_parameter",
        {"DW_AT_type": "0x00000010", "DW_AT_name": '"_Tp"'},
    ),
    (
        0x3C,
        4,
        "DW_TAG_template_value_parameter",
        {"DW_AT_name": '"_Size"', "DW_AT_const_value": "3"},
    ),
    (0x40, 1, "DW_TAG_pointer_type", {"DW_AT_type": "0x00000014"}),
    (0x44, 1, "DW_TAG_structure_type", {"DW_AT_name": '"Hold<Pt>"', "DW_AT_byte_size": "0x04"}),
    (0x48, 2, "DW_TAG_template_type_parameter", {"DW_AT_type": "0x00000014", "DW_AT_name": '"T"'}),
    (0x4C, 2, "DW_TAG_member", member("n", 0x10, 0)),
)


def test_a_standard_containers_template_arguments_type_its_elements():
    # #181: a vector holds its elements through a pointer, so only its `_Tp` reaches them.
    graph = type_graph(_parse_dies(CONTAINERS), (0x28, 0x34, 0x44), CPP_LIBRARIES)
    vector, array = graph[0x28], graph[0x34]
    assert vector[11] == "std::__1::vector<Pt>" and vector[12] == (("type", "_Tp", 0x14, None),)
    assert array[12] == (("type", "_Tp", 0x10, None), ("value", "_Size", None, 3))
    assert 0x14 in graph and graph[0x14][0] == "record"
    # The program's own templates keep their arguments without walking them.
    assert graph[0x44][12] == (("type", "T", 0x14, None),)
    alone = type_graph(_parse_dies(CONTAINERS), (0x44,), CPP_LIBRARIES)
    assert 0x14 not in alone


REFERRED = dump(
    (0x0B, 0, "DW_TAG_compile_unit", {"DW_AT_name": '"r.cpp"'}),
    (0x10, 1, "DW_TAG_base_type", base("int", "DW_ATE_signed", "0x04")),
    (0x20, 1, "DW_TAG_subprogram", {"DW_AT_name": '"f"'}),
    (0x24, 2, "DW_TAG_formal_parameter", NAMED("plain") | {"DW_AT_location": "DW_OP_fbreg -8"}),
    (
        0x28,
        2,
        "DW_TAG_variable",
        NAMED("items") | {"DW_AT_location": "DW_OP_fbreg -24, DW_OP_deref"},
    ),
    (0x2C, 2, "DW_TAG_lexical_block", {}),
    (
        0x30,
        3,
        "DW_TAG_variable",
        NAMED("sized") | {"DW_AT_location": "DW_OP_breg29 W29-8, DW_OP_deref_size 0x8"},
    ),
    (0x34, 3, "DW_TAG_variable", NAMED("framed") | {"DW_AT_location": "DW_OP_breg29 W29-16"}),
    (0x38, 2, "DW_TAG_variable", NAMED("global") | {"DW_AT_location": "DW_OP_addr 0x1000"}),
    (0x3C, 2, "DW_TAG_variable", NAMED("gone")),
)


def test_locals_the_frame_reaches_through_a_pointer():
    # Review of #181: `auto &[items, dropped] = *owner` is typed as the vector itself, at
    # `DW_OP_fbreg -24, DW_OP_deref`: storage "automatic", so its container was taken for
    # the frame's own. Anything but one frame offset is read through a pointer.
    dies = _parse_dies(REFERRED)
    assert referred_locals(dies, [0x20]) == {"items", "sized", "global", "gone"}
    assert {k: {s for _, s, _ in v} for k, v in local_types(dies, [0x20]).items()}["items"] == {
        "automatic"
    }


THROUGH = dump(
    (0x0B, 0, "DW_TAG_compile_unit", {"DW_AT_name": '"t.cpp"'}),
    (0x10, 1, "DW_TAG_base_type", base("int", "DW_ATE_signed", "0x04")),
    (0x14, 1, "DW_TAG_array_type", {"DW_AT_type": "0x00000010"}),
    (0x20, 1, "DW_TAG_subprogram", {"DW_AT_name": '"f"'}),
    (
        0x24,
        2,
        "DW_TAG_formal_parameter",
        NAMED("big") | {"DW_AT_location": "DW_OP_fbreg +8, DW_OP_deref"},
    ),
    (
        0x28,
        2,
        "DW_TAG_variable",
        NAMED("dropped")
        | {"DW_AT_location": "DW_OP_fbreg -16, DW_OP_deref, DW_OP_plus_uconst 0x18"},
    ),
    (
        0x2C,
        2,
        "DW_TAG_variable",
        NAMED("b") | {"DW_AT_location": "DW_OP_breg31 WSP+16, DW_OP_plus_uconst 0x4"},
    ),
    (
        0x30,
        2,
        "DW_TAG_variable",
        {
            "DW_AT_name": '"vla"',
            "DW_AT_type": "0x00000014",
            "DW_AT_location": "DW_OP_fbreg -32, DW_OP_deref",
        },
    ),
)


def test_a_variable_read_through_a_pointer_is_stored_through_it():
    # #186's review, after round 1: `dropped` of `auto &[at, dropped] = *owner` was an
    # "automatic", so `--set dropped=50` wrote `o.dropped` through the reference. A
    # by-value parameter the ABI passes by a hidden pointer is the call's own copy, and
    # a by-value binding's constant offset is the frame's own storage.
    dies = _parse_dies(THROUGH)
    storages = {
        name: {storage for _, storage, _ in kinds}
        for name, kinds in local_types(dies, [0x20], through=True).items()
    }
    assert storages == {
        "big": {"parameter"},
        "dropped": {"through"},
        "b": {"automatic"},
        "vla": {"through"},
    }
    # Rust's walk keeps its own storages.
    assert {s for _, s, _ in local_types(dies, [0x20])["dropped"]} == {"automatic"}
    assert referred_locals(dies, [0x20]) == {"big", "dropped", "vla"}


def test_a_local_read_through_a_pointer_is_refused_as_a_root_and_by_its_fields():
    from peeky.errors import _MUTATION_HINTS
    from peeky.native import Everywhere, _not_framed

    session = NativeSession.__new__(NativeSession)
    dies = _parse_dies(THROUGH)
    types = local_types(dies, [0x20], through=True)
    session.fields = type_graph(dies, {0x10, 0x14}, CPP_LIBRARIES)
    session.declarations = [{"types": types}]
    session._function_at = lambda frame: session.declarations[0]
    session._shown_where = lambda name, pc: None
    session._unslid = lambda frame: None
    reason = session.mid_statement({"name": "dropped"}, {"id": 1})
    assert isinstance(reason, Everywhere) and "is reached through a pointer or reference" in reason
    assert reason == _not_framed("dropped", {"through"})
    assert session.never_settable_row({"name": "dropped"}, {"id": 1}, member=True) == "reference"
    # A variable-length array's elements are refused as such, never as a reference.
    assert session.never_settable_row({"name": "vla"}, {"id": 1}, member=True) is None
    assert "static" in _not_framed("g", {"static"})
    hint = next(hint for pattern, hint in _MUTATION_HINTS if re.search(pattern, reason))
    assert "the frame that owns it" in hint, hint
