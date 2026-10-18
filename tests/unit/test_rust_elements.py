"""Rust containers of every scalar element type: integers, bool and floats (f32, f64)."""

import math
import struct

import pytest

from peeky.rust_symbols import (
    ELEMENT_TYPES,
    OWNED_CONTAINERS,
    REFERENCE_PARAMETERS,
    UNQUALIFIED_CONTAINER,
    UNVERIFIED_CONTAINER,
    container_view,
    rust_float_debug,
)


def vec(element, size):
    return {
        "kind": "vec",
        "size": 24,
        "data": 8,
        "length": 16,
        "capacity": 0,
        "element": element,
        "element_size": size,
    }


def slice_(element, size):
    return {
        "kind": "slice",
        "size": 16,
        "data": 0,
        "length": 8,
        "element": element,
        "element_size": size,
    }


def words(*values):
    return b"".join(value.to_bytes(8, "little") for value in values)


def memory(blocks):
    return lambda address, count: blocks[address][:count]


def read_slice(element, payload, count):
    size = ELEMENT_TYPES[element][0]
    layouts = {f"&[{element}]": slice_(element, size)}
    read = memory({"0x10": words(0x1000, count), "0x1000": payload})
    variable = {"name": "xs", "type": f"&[{element}]", "memoryReference": "0x10"}
    return container_view(variable, read, layouts, optimized=False, parameter=f"&[{element}]")


@pytest.mark.parametrize(
    ("element", "values", "shown"),
    [
        ("u8", [1, 255], "[1, 255]"),
        ("i8", [-128, 127], "[-128, 127]"),
        ("u16", [65535], "[65535]"),
        ("i16", [-300], "[-300]"),
        ("u32", [4_000_000_000], "[4000000000]"),
        ("u64", [2**64 - 1], "[18446744073709551615]"),
        ("i64", [-(2**63)], "[-9223372036854775808]"),
        ("usize", [7], "[7]"),
        ("isize", [-7], "[-7]"),
    ],
)
def test_integers_decode_with_their_own_width_and_sign(element, values, shown):
    size, encoding, _ = ELEMENT_TYPES[element]
    signed = encoding == "DW_ATE_signed"
    payload = b"".join(v.to_bytes(size, "little", signed=signed) for v in values)
    view = read_slice(element, payload, len(values))
    assert view["value"] == shown and view["rust_view"] == "memory"
    assert view["children"][1] == {"name": "[0]", "type": element, "value": str(values[0])}


def test_bools_are_true_or_false_and_anything_else_is_refused():
    assert read_slice("bool", b"\x01\x00", 2)["value"] == "[true, false]"
    assert read_slice("bool", b"\x01\x02", 2)["value"] == UNVERIFIED_CONTAINER


def test_floats_show_as_rust_debug_prints_them():
    payload = struct.pack("<4d", 1.5, -0.0, math.inf, 1e16)
    assert read_slice("f64", payload, 4)["value"] == "[1.5, -0.0, inf, 1e16]"
    payload = struct.pack("<3f", 0.1, 3e38, math.nan)
    assert read_slice("f32", payload, 3)["value"] == "[0.1, 3e38, NaN]"


@pytest.mark.parametrize(
    ("value", "bits", "shown"),
    [
        (0.0, 64, "0.0"),
        (-0.0, 64, "-0.0"),
        (1.0, 64, "1.0"),
        (0.1, 64, "0.1"),
        (0.1 + 0.2, 64, "0.30000000000000004"),
        (123.456, 64, "123.456"),
        (1e15, 64, "1000000000000000.0"),
        (1e16, 64, "1e16"),
        (0.0001, 64, "0.0001"),
        (0.00009999, 64, "9.999e-5"),
        (5e-324, 64, "5e-324"),
        (1.7976931348623157e308, 64, "1.7976931348623157e308"),
        (struct.unpack("<f", struct.pack("<f", 0.1))[0], 32, "0.1"),
        (3061734.25, 32, "3061734.3"),  # a tie: away from zero, as Rust does
        (0.000244140625, 32, "0.00024414063"),
        (struct.unpack("<f", struct.pack("<f", 3.4028234663852886e38))[0], 32, "3.4028235e38"),
        (math.nan, 32, "NaN"),
        (-math.inf, 64, "-inf"),
    ],
)
def test_rust_float_debug(value, bits, shown):
    assert rust_float_debug(value, bits) == shown


def owned(type_, layouts, declared=None):
    read = memory({"0x10": words(1, 0x1000, 1), "0x1000": (5).to_bytes(8, "little")})
    variable = {"name": "v", "type": type_, "memoryReference": "0x10"}
    return container_view(variable, read, layouts, optimized=False, owned=True, declared=declared)


def test_owned_locals_whose_debugger_names_collide_are_told_apart():
    name = "alloc::vec::Vec<unsigned long, alloc::alloc::Global>"
    u64, usize = (
        "alloc::vec::Vec<u64, alloc::alloc::Global>",
        "alloc::vec::Vec<usize, alloc::alloc::Global>",
    )
    assert OWNED_CONTAINERS[name] == (u64, usize)
    both = {u64: vec("u64", 8), usize: vec("usize", 8)}
    # The MIR's declared type names the one it is.
    shown = owned(name, both, "std::vec::Vec<usize>")
    assert shown["value"] == "[5]" and shown["children"][2]["type"] == "usize"
    # Without it, identical layouts still read, and say both names.
    assert owned(name, both)["children"][2]["type"] == "u64 or usize"
    # Layouts that differ (or one that is unqualified) are never guessed between.
    assert owned(name, {u64: vec("u64", 8), usize: {**vec("usize", 8), "data": 0}}) is None
    assert owned(name, {u64: vec("u64", 8), usize: {"kind": "unqualified"}}) is None
    assert owned(name, {usize: {"kind": "unqualified"}})["value"] == UNQUALIFIED_CONTAINER


def test_every_element_type_is_reachable_through_each_kind_of_container():
    for element, (_, _, lldb_names) in ELEMENT_TYPES.items():
        vec_name = f"alloc::vec::Vec<{element}, alloc::alloc::Global>"
        assert REFERENCE_PARAMETERS[f"&[{element}]"][1] == f"&[{element}]"
        assert REFERENCE_PARAMETERS[f"&mut {vec_name}"][1] == vec_name
        for lldb in lldb_names:
            assert vec_name in OWNED_CONTAINERS[f"alloc::vec::Vec<{lldb}, alloc::alloc::Global>"]
