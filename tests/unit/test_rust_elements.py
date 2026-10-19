"""Rust containers of every scalar element type: integers, bool and floats (f32, f64)."""

import decimal
import math
import random
import struct

import pytest

from peeky.rust_symbols import (
    ELEMENT_TYPES,
    OWNED_CONTAINERS,
    REFERENCE_PARAMETERS,
    UNQUALIFIED_CONTAINER,
    UNVERIFIED_CONTAINER,
    container_view,
    lldb_float_debug,
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


def _lldb_text(value, precision, positional):
    """LLDB's rendering: APFloat natural precision, a dropped half rounded up."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    sign = "-" if math.copysign(1.0, value) < 0 else ""
    if value == 0:
        return sign + "0"
    rounded = decimal.Context(prec=precision, rounding=decimal.ROUND_HALF_UP).plus(
        decimal.Decimal(abs(value))
    )
    digits = "".join(map(str, rounded.as_tuple().digits)).rstrip("0") or "0"
    exponent = rounded.adjusted()
    if positional and -6 <= exponent < precision:
        if exponent >= 0:
            whole = digits[: exponent + 1].ljust(exponent + 1, "0")
            rest = digits[exponent + 1 :]
            return sign + whole + ("." + rest if rest else "")
        return sign + "0." + "0" * (-exponent - 1) + digits
    rest = digits[1:]
    return f"{sign}{digits[0]}{'.' + rest if rest else ''}E{exponent:+d}"


@pytest.mark.parametrize("bits,kind,precision", [(64, "double", 17), (32, "float", 9)])
def test_lldb_float_text_shows_as_rust_debug(bits, kind, precision):
    pack, raw, _ = {32: ("<f", "<I", 0), 64: ("<d", "<Q", 0)}[bits]
    generator = random.Random(bits)
    patterns = [generator.getrandbits(bits) for _ in range(4000)]
    patterns += [0, 1, 2 ** (bits - 1), 2 ** (bits - 1) + 1, (2 ** (bits - 1)) - 1]
    for pattern in patterns:
        value = struct.unpack(pack, struct.pack(raw, pattern))[0]
        for positional in (True, False):
            text = _lldb_text(value, precision, positional)
            assert lldb_float_debug(text, kind) == rust_float_debug(value, bits), text


@pytest.mark.parametrize(
    "text,kind",
    [
        ("0.1", "double"),  # LLDB would print 0.10000000000000001: not its rendering
        ("10.000000000000002", "float"),  # more digits than an f32 has
        ("3.5E+38", "float"),  # beyond f32
        ("1.8E+309", "double"),
        ("0x4024000000000000", "double"),  # a user's type format
        ("<variable not available>", "double"),
        ("1e5", "double"),
        ("10", "int"),
        ("10", "unsigned long"),
        (None, "double"),
        (" 10", "double"),
        ("10.", "double"),
    ],
)
def test_lldb_float_text_that_is_not_an_exact_rendering_stays_raw(text, kind):
    assert lldb_float_debug(text, kind) is None


def test_lldb_float_text_keeps_sign_and_specials():
    assert lldb_float_debug("-3", "float") == "-3.0"
    assert lldb_float_debug("-0", "double") == "-0.0"
    assert lldb_float_debug("-NaN", "double") == "NaN"
    assert lldb_float_debug("-Inf", "float") == "-inf"
    assert lldb_float_debug("9.9999999999999995E-8", "double") == "1e-7"
    assert lldb_float_debug("0.100000001", "float") == "0.1"


def _lldb_char(byte):
    """LLDB's rendering of a char scalar (observed on Apple LLDB and LLVM 18/20)."""
    named = {0: "\\0", 7: "\\a", 8: "\\b", 9: "\\t", 10: "\\n", 11: "\\v", 12: "\\f", 13: "\\r"}
    named[27] = "\\e"
    if byte in named:
        return f"'{named[byte]}'"
    if 0x20 <= byte <= 0x7E:
        return f"'{chr(byte)}'"
    return f"'\\x{byte:02x}'"


def test_lldb_byte_literals_show_as_rust_integers():
    from peeky.rust_symbols import lldb_byte

    for byte in range(256):
        assert lldb_byte(_lldb_char(byte), "unsigned char") == str(byte)
        assert lldb_byte(_lldb_char(byte), "char") == str(byte if byte < 128 else byte - 256)
    for text, kind in [("'AB'", "char"), ("65", "char"), ("'A'", "int"), ("'\\x100'", "char")]:
        assert lldb_byte(text, kind) is None
    assert lldb_byte("'\\x1B'", "signed char") == "27" and lldb_byte("'\\033'", "char") == "27"
