"""Bounded Rust symbol evidence scoped to the application's own compile units.

Rust executables link precompiled standard-library line tables whose combined
dump exceeds the native parser budget (4.8 MB on the pinned Linux pair). This
module does not loosen that budget. It selects only compile units whose recorded
compilation directory is the fresh workspace and whose name binds them to the
declared crate root, dumps exactly their line tables, and reuses the unchanged
fail-closed DWARF4 line parser. Units that claim one property but not the other
are ambiguous and rejected.

Enum layouts come from the same units, so active-variant inspection follows the
compiler-emitted discriminant values for this build rather than an assumed ABI.
Only explicitly qualified layouts are interpreted; every other data-carrying enum
is opaque. This is trusted-code evidence, not a sandbox or formatter.
"""

import bisect
import codecs
import decimal
import json
import math
import re
import struct
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from peeky.adapters import ViewBudgetExhausted
from peeky.native import _metadata, parse_lines
from peeky.rust_chars import ESCAPED
from peeky.rust_lexer import code_only
from peeky.textlines import octal_string, split_lines

# An application unit's own DWARF: a program using std's collections has megabytes of
# monomorphized types (4.4 MB for one using HashMap); dumping and parsing it takes
# tens of milliseconds, inside the symbol deadline.
RUST_METADATA = 16 * 1024 * 1024

MAX_UNITS = 4096
MAX_APPLICATION_UNITS = 16
MAX_DIES = 200_000
MAX_DEPTH = 64
MAX_LAYOUTS = 4096
SYMBOL_DEADLINE_S = 30
_clock = time.monotonic
# Exact qualified enum type -> (DWARF payload base type, debugger display type).
# Other Option payloads are opaque until separately qualified on each pinned pair.
QUALIFIED_OPTIONS = {"core::option::Option<i32>": ("i32", "int")}
# DWARF discriminant base type -> debugger display type on qualified pairs.
DISCRIMINANT_TYPES = {"u32": "unsigned int"}
UNQUALIFIED = "<unqualified Rust enum layout>"
UNVERIFIED = "<unverified Rust enum value>"
BUDGET = "<Rust value view budget exhausted; expand or inspect a narrower scope>"


def _budget(exc):
    """What a view the worker's budget stopped shows: which limit ran out (#156 review:
    Peeky's own checks once took its whole time, and a `&str` blamed an enum's view)."""
    cause = str(exc).partition(": ")[2]
    return BUDGET.replace("exhausted;", f"exhausted ({cause});") if cause else BUDGET


_UNIT = re.compile(
    r"0x([0-9a-f]{8,16}): ([A-Za-z ]{1,32}) Unit: (.{1,512}) \(next unit at 0x([0-9a-f]{8,16})\)"
)
_DIE = re.compile(r"(0x[0-9a-f]{8,16}:)( +)(DW_TAG_[A-Za-z0-9_]+|NULL)")
_ATTRIBUTE = re.compile(r"( +)(DW_AT_[A-Za-z0-9_]+)\t\((.*)")
_CONTINUATION = re.compile(r" +\[0x[0-9a-f]+, 0x[0-9a-f]+\).*")
_HEADER = re.compile(r"\S.*:\s+file format \S.*")


@dataclass
class _Die:
    offset: int
    tag: str
    attributes: dict
    children: list
    unit: dict | None = None
    depth: int = 0
    closed: bool = False
    # {attribute: its continuation rows} (a `DW_AT_ranges` list's `[low, high)` rows).
    continued: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RustUnit:
    unit: int
    die: int
    end: int
    stmt_list: int
    name: str


def _string(raw):
    match = re.fullmatch(r'(".*")\)', raw or "")
    if not match:
        raise ValueError("Expected a quoted DWARF string attribute")
    # dwarfdump writes an unprintable byte (any non-ASCII one) in octal (#179).
    value = octal_string(match[1][1:-1])
    if value is None:
        raise ValueError("Unsupported DWARF string escape")
    if any(ord(char) < 32 for char in value):
        raise ValueError("Unsupported DWARF string attribute")
    return value


def _hex(raw):
    match = re.fullmatch(r"0x([0-9a-f]{1,16})\)", raw or "")
    if not match:
        raise ValueError("Expected a hexadecimal DWARF attribute")
    return int(match[1], 16)


def _reference(raw):
    match = re.fullmatch(r'0x([0-9a-f]{1,16})(?: (".*"))?\)', raw or "")
    if not match:
        raise ValueError("Expected a DWARF reference attribute")
    name = _string(match[2] + ")") if match[2] else None
    return int(match[1], 16), name


def _close_deeper(stack, depth):
    """Entries leaving the open path must not abandon an unterminated child list."""
    if any(die.children and not die.closed for die in stack[depth:]):
        raise ValueError("Unterminated DWARF child list")
    del stack[depth:]


def parse_dies(text, *, units):
    """Parse llvm-dwarfdump DIE output by indentation; unknown rows fail closed.

    Attributes print at 12 + 2 + 2*depth columns independent of offset width.
    Every child list in --show-children output must end with its own NULL.
    """
    if len(text.encode()) > RUST_METADATA:
        raise ValueError("Rust debug information exceeds limit")
    roots, stack, pending, last, seen, count = [], [], None, None, False, 0
    attribute = None
    for raw in split_lines(text):
        if not raw.strip():
            continue
        if not seen and (raw == ".debug_info contents:" or _HEADER.fullmatch(raw)):
            continue
        if match := _UNIT.fullmatch(raw):
            if not units or pending is not None:
                raise ValueError("Unexpected DWARF unit header")
            version = re.search(r"(?:^|, )version = 0x([0-9a-f]{4})(?:,|$)", match[3])
            if not version:
                raise ValueError("DWARF unit header has no version")
            pending = {
                "offset": int(match[1], 16),
                "kind": match[2],
                "version": int(version[1], 16),
                "end": int(match[4], 16),
            }
            _close_deeper(stack, 0)
            stack, last, seen = [], None, True
            continue
        if match := _DIE.fullmatch(raw):
            seen = True
            spaces = len(match[2])
            if spaces % 2 != 1 or (spaces - 1) // 2 > MAX_DEPTH:
                raise ValueError("Malformed DWARF entry indentation")
            depth = (spaces - 1) // 2
            if match[3] == "NULL":
                if not 0 < depth <= len(stack) or stack[depth - 1].closed:
                    raise ValueError("Unbalanced DWARF entry list")
                _close_deeper(stack, depth)
                stack[depth - 1].closed = True
                last = None
                continue
            count += 1
            if count > MAX_DIES:
                raise ValueError("Too many DWARF entries")
            die = _Die(int(match[1][2:-1], 16), match[3], {}, [], depth=depth)
            if depth == 0:
                if units:
                    if pending is None:
                        raise ValueError("DWARF unit entry has no unit header")
                    die.unit, pending = pending, None
                elif roots:
                    raise ValueError("Expected exactly one DWARF unit entry")
                _close_deeper(stack, 0)
                roots.append(die)
                stack = [die]
            else:
                if depth > len(stack) or stack[depth - 1].closed:
                    raise ValueError("DWARF entry has no open parent")
                _close_deeper(stack, depth)
                stack[-1].children.append(die)
                stack.append(die)
            last = die
            continue
        if match := _ATTRIBUTE.fullmatch(raw):
            if last is None or len(match[1]) != 14 + 2 * last.depth:
                raise ValueError("DWARF attribute is not attached to an entry")
            if match[2] in last.attributes:
                raise ValueError("Duplicate DWARF attribute")
            last.attributes[match[2]] = match[3]
            attribute = match[2]
            continue
        if last is not None and last.attributes and _CONTINUATION.fullmatch(raw):
            last.continued.setdefault(attribute, []).append(raw.strip())
            continue
        raise ValueError("Unrecognized DWARF entry output")
    if pending is not None or not roots:
        raise ValueError("Missing DWARF unit entry")
    _close_deeper(stack, 0)
    if not units and roots[0].children and not roots[0].closed:
        raise ValueError("Unterminated DWARF unit entry")
    return roots


def application_units(text, workspace, *, entrypoint, crate):
    """Select compile units that are both local to the workspace and named for the crate."""
    workspace = Path(workspace).resolve(strict=True)
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", crate or ""):
        raise ValueError("Expected a plain Rust crate name")
    roots = parse_dies(text, units=True)
    if len(roots) > MAX_UNITS:
        raise ValueError("Too many DWARF units")
    expected = re.compile(
        re.escape(entrypoint) + "/@/" + re.escape(crate) + r"\.[0-9a-z]{8,32}-cgu\.[0-9]{1,4}"
    )
    selected = []
    for die in roots:
        comp_dir = die.attributes.get("DW_AT_comp_dir")
        name = die.attributes.get("DW_AT_name")
        directory = _string(comp_dir) if comp_dir is not None else None
        # Compare the recorded string exactly; resolving it would accept aliases.
        local = directory == str(workspace)
        named = name is not None and _string(name).startswith(entrypoint + "/@/")
        if not local and not named:
            continue
        unit = die.unit
        if not (local and named):
            raise ValueError("Ambiguous Rust application compile unit")
        name = _string(name)
        producer = die.attributes.get("DW_AT_producer")
        if (
            unit["kind"] != "Compile"
            or unit["version"] != 4
            or die.tag != "DW_TAG_compile_unit"
            or die.offset <= unit["offset"]
            or die.attributes.get("DW_AT_language") != "DW_LANG_Rust)"
            or producer is None
            or "(rustc version " not in _string(producer)
            or not expected.fullmatch(name)
        ):
            raise ValueError("Unsupported Rust application compile unit")
        selected.append(
            RustUnit(
                unit["offset"],
                die.offset,
                unit["end"],
                _hex(die.attributes.get("DW_AT_stmt_list")),
                name,
            )
        )
    if not 0 < len(selected) <= MAX_APPLICATION_UNITS:
        raise ValueError("Expected a bounded set of Rust application compile units")
    if len({unit.stmt_list for unit in selected}) != len(selected):
        raise ValueError("Duplicate Rust application line table")
    return tuple(selected)


def _walk(die, by_offset, unit):
    if die.offset in by_offset or not unit.die <= die.offset < unit.end:
        raise ValueError("DWARF entry is duplicated or outside its application unit")
    by_offset[die.offset] = die
    for child in die.children:
        _walk(child, by_offset, unit)


def _base_type(reference, by_offset, name, encoding):
    """Follow a reference to an in-unit four-byte base type; labels are not trusted."""
    offset, label = _reference(reference)
    die = by_offset.get(offset)
    return (
        die is not None
        and label == name
        and die.tag == "DW_TAG_base_type"
        and not die.children
        and die.attributes.get("DW_AT_name") == f'"{name}")'
        and die.attributes.get("DW_AT_encoding") == f"{encoding})"
        and die.attributes.get("DW_AT_byte_size") == "0x04)"
    )


def _option_layout(struct, qualified, by_offset):
    """Return the compiler-emitted layout of a qualified Option or an unqualified marker."""
    payload = QUALIFIED_OPTIONS.get(qualified)
    parts = [child for child in struct.children if child.tag == "DW_TAG_variant_part"]
    if payload is None or len(parts) != 1:
        return {"kind": "unqualified"}
    part = parts[0]
    discriminant = _reference(part.attributes.get("DW_AT_discr"))[0]
    members = [child for child in part.children if child.tag == "DW_TAG_member"]
    variants = [child for child in part.children if child.tag == "DW_TAG_variant"]
    if len(members) + len(variants) != len(part.children) or len(members) != 1:
        return {"kind": "unqualified"}
    tag = members[0]
    kind = "u32"
    if (
        tag.offset != discriminant
        or tag.attributes.get("DW_AT_artificial") != "true)"
        or not _base_type(tag.attributes.get("DW_AT_type"), by_offset, kind, "DW_ATE_unsigned")
        or "DW_AT_data_member_location" not in tag.attributes
    ):
        return {"kind": "unqualified"}
    mapping = []
    for variant in variants:
        if "DW_AT_discr_value" not in variant.attributes or len(variant.children) != 1:
            return {"kind": "unqualified"}
        member = variant.children[0]
        if member.tag != "DW_TAG_member":
            return {"kind": "unqualified"}
        target = by_offset.get(_reference(member.attributes.get("DW_AT_type"))[0])
        mapping.append(
            (
                _hex(variant.attributes["DW_AT_discr_value"]),
                _string(member.attributes.get("DW_AT_name")),
                target,
            )
        )
    if (
        [name for _, name, _ in mapping] != ["None", "Some"]
        or len({value for value, _, _ in mapping}) != len(mapping)
        or any(value >= 2**32 for value, _, _ in mapping)
    ):
        return {"kind": "unqualified"}
    (_, _, none), (_, _, some) = mapping
    if (
        none is None
        or none.tag != "DW_TAG_structure_type"
        or any(child.tag == "DW_TAG_member" for child in none.children)
        or some is None
        or some.tag != "DW_TAG_structure_type"
    ):
        return {"kind": "unqualified"}
    fields = [child for child in some.children if child.tag == "DW_TAG_member"]
    if (
        len(fields) != 1
        or _string(fields[0].attributes.get("DW_AT_name")) != "__0"
        or not _base_type(
            fields[0].attributes.get("DW_AT_type"), by_offset, payload[0], "DW_ATE_signed"
        )
    ):
        return {"kind": "unqualified"}
    return {
        "kind": "option",
        "discriminant": kind,
        "variants": [[value, name] for value, name, _ in mapping],
        "payload": payload[0],
    }


# An `Option` of a tuple or struct the crate defines, read from memory by its DWARF layout
# (`Some(Engineer { name: "Björn", shifts: 1, .. })`).
OPTION_COMPOSITE = "option-composite"
_OPTION_NAME = re.compile(r"core::option::Option<(.+)>")
_WORD = 8
_MAX_NICHE_DEPTH = 16


def _type_size(die, by_offset, depth=0):
    """A member type's size in bytes: its `DW_AT_byte_size`; a thin pointer's is a word
    (Peeky reads 64-bit targets), an array's its count of elements. Anything else
    raises."""
    if die is None or depth > _MAX_NICHE_DEPTH:
        raise ValueError("Rust field has an unknown type")
    if "DW_AT_byte_size" in die.attributes:
        return _hex(die.attributes["DW_AT_byte_size"])
    if die.tag == "DW_TAG_pointer_type":
        return _WORD
    ranges = [child for child in die.children if child.tag == "DW_TAG_subrange_type"]
    if die.tag == "DW_TAG_array_type" and len(ranges) == 1:
        count = _hex(ranges[0].attributes.get("DW_AT_count"))
        return count * _type_size(_target(die, by_offset), by_offset, depth + 1)
    raise ValueError("Rust field's size is not known")


def _niche_range(die, offset, size, by_offset, names, depth=0):
    """(lowest, highest): the values the field of the struct ``die`` that spans exactly
    ``size`` bytes at ``offset`` may hold, where its type fixes them (a Vec's or
    String's capacity, a NonNull's, a reference's or a Box's pointer, a bool, a char),
    found through nested structs. Raises for any other field: its valid values are not
    known, so a value outside them cannot be told from `None`."""
    if depth > _MAX_NICHE_DEPTH or die is None or die.tag != "DW_TAG_structure_type":
        raise ValueError("Rust niche is not inside a plain struct")
    if any(child.tag in ("DW_TAG_variant_part", "DW_TAG_variant") for child in die.children):
        raise ValueError("Rust niche is inside an enum")
    holders = []
    for member in die.children:
        if member.tag != "DW_TAG_member":
            continue
        start = _hex(member.attributes.get("DW_AT_data_member_location"))
        target = _target(member, by_offset)
        width = _type_size(target, by_offset)
        if start <= offset < start + width:
            holders.append((start, member, target, width))
    if len(holders) != 1:
        raise ValueError("Rust niche is not inside exactly one field")
    start, member, target, width = holders[0]
    at = offset - start
    if at + size > width:
        raise ValueError("Rust niche spans more than one field")
    if target.tag == "DW_TAG_structure_type":
        return _niche_range(target, at, size, by_offset, names, depth + 1)
    if at != 0 or size != width:
        raise ValueError("Rust niche is part of a field")
    holder, field = names.get(die.offset), _string(member.attributes.get("DW_AT_name"))
    if target.tag == "DW_TAG_base_type":
        if (
            holder == "core::num::niche_types::UsizeNoHighBit"
            and field == "__0"
            and _sized_base(target, "usize", 8, "DW_ATE_unsigned")
        ):
            return 0, 2**63 - 1
        if _sized_base(target, "bool", 1, "DW_ATE_boolean"):
            return 0, 1
        if _sized_base(target, "char", 4, "DW_ATE_UTF"):
            return 0, 0x10FFFF
    elif target.tag == "DW_TAG_pointer_type" and size == _WORD:
        pointer = names.get(target.offset) or ""
        if (
            (field == "pointer" and re.fullmatch(r"core::ptr::non_null::NonNull<.+>", holder or ""))
            or pointer.startswith("&")
            or re.fullmatch(r"alloc::boxed::Box<.+>", pointer)
        ):
            return 1, 2**64 - 1
    raise ValueError("Rust niche field's valid values are not known")


def _composite_option_layout(struct, qualified, by_offset, names, crate):
    """The layout of an `Option<T>` whose T is a tuple or struct of the crate's (see
    _composite: its integer, bool, float, char and String fields read), proven from the
    compiler's DWARF variant part; {"kind": "unqualified"} for anything else.

    Either a tag of its own whose `DW_AT_discr_value`s name both variants, or a niche: a
    field of T whose type fixes its valid values holds `None`'s value, which is outside
    them, and `Some` is the variant without a value. A tag holding anything else is no
    `Option`, so it is never decoded as one."""
    unqualified = {"kind": "unqualified"}
    match = _OPTION_NAME.fullmatch(qualified)
    if not match or not (match[1].startswith("(") or match[1].startswith(f"{crate}::")):
        return unqualified
    try:
        size = _hex(struct.attributes.get("DW_AT_byte_size"))
        parts = [child for child in struct.children if child.tag == "DW_TAG_variant_part"]
        if len(parts) != 1:
            return unqualified
        part = parts[0]
        discriminant = _reference(part.attributes.get("DW_AT_discr"))[0]
        members = [child for child in part.children if child.tag == "DW_TAG_member"]
        variants = [child for child in part.children if child.tag == "DW_TAG_variant"]
        if len(members) != 1 or len(variants) != 2 or len(part.children) != 3:
            return unqualified
        (tag,) = members
        kind = _target(tag, by_offset)
        if (
            tag.offset != discriminant
            or tag.attributes.get("DW_AT_artificial") != "true)"
            or kind is None
            or kind.tag != "DW_TAG_base_type"
            or kind.children
            or kind.attributes.get("DW_AT_encoding") != "DW_ATE_unsigned)"
        ):
            return unqualified
        at = _hex(tag.attributes.get("DW_AT_data_member_location"))
        width = _type_size(kind, by_offset)
        if width not in (1, 2, 4, 8) or kind.attributes.get("DW_AT_name") != f'"u{width * 8}")':
            return unqualified
        found = {}
        for variant in variants:
            (member,) = variant.children
            name = _string(member.attributes.get("DW_AT_name"))
            target = _target(member, by_offset)
            value = variant.attributes.get("DW_AT_discr_value")
            if (
                member.tag != "DW_TAG_member"
                or name in found
                or _hex(member.attributes.get("DW_AT_data_member_location")) != 0
                or target is None
                or not any(child is target for child in struct.children)
                or target.tag != "DW_TAG_structure_type"
                or target.attributes.get("DW_AT_name") != f'"{name}")'
                or _hex(target.attributes.get("DW_AT_byte_size")) != size
            ):
                return unqualified
            found[name] = (None if value is None else _hex(value), target)
        if set(found) != {"None", "Some"}:
            return unqualified
        (none, none_type), (some, some_type) = found["None"], found["Some"]
        if none is None or any(child.tag == "DW_TAG_member" for child in none_type.children):
            return unqualified
        fields = [child for child in some_type.children if child.tag == "DW_TAG_member"]
        if len(fields) != 1 or fields[0].attributes.get("DW_AT_name") != '"__0")':
            return unqualified
        payload = _hex(fields[0].attributes.get("DW_AT_data_member_location"))
        element = _target(fields[0], by_offset)
        if element is None or names.get(element.offset) != match[1]:
            return unqualified
        composite = _composite(element, by_offset, names)
        end = payload + composite["element_size"]
        if end > size or payload % composite["alignment"] or not at + width <= size:
            return unqualified
        limit = 2 ** (8 * width)
        if some is None:
            # A niche: the tag is a field of the payload's.
            if not payload <= at < at + width <= end:
                return unqualified
            valid = _niche_range(element, at - payload, width, by_offset, names)
            if not none < limit or valid[0] <= none <= valid[1] or valid[1] >= limit:
                return unqualified
        else:
            # A tag of its own, beside the payload.
            overlaps = at < end and payload < at + width
            if none == some or not (none < limit and some < limit) or overlaps:
                return unqualified
            valid = None
    except (KeyError, ValueError, IndexError, TypeError):
        return unqualified
    return {
        "kind": OPTION_COMPOSITE,
        "size": size,
        "tag": [at, width],
        "none": none,
        "some": some,
        "valid": list(valid) if valid else None,
        "payload": payload,
        "element": match[1],
        **composite,
    }


# Element types of the containers Peeky reads: {Rust name: (size, DWARF encoding,
# LLDB's names for it)}. LLDB names scalars as C types, so i64 and isize (u64 and usize)
# share a name; owned locals are told apart by their layouts, which must agree.
ELEMENT_TYPES = {
    "i8": (1, "DW_ATE_signed", ("char", "signed char")),
    "u8": (1, "DW_ATE_unsigned", ("unsigned char",)),
    "i16": (2, "DW_ATE_signed", ("short",)),
    "u16": (2, "DW_ATE_unsigned", ("unsigned short",)),
    "i32": (4, "DW_ATE_signed", ("int",)),
    "u32": (4, "DW_ATE_unsigned", ("unsigned int",)),
    "i64": (8, "DW_ATE_signed", ("long",)),
    "u64": (8, "DW_ATE_unsigned", ("unsigned long",)),
    "isize": (8, "DW_ATE_signed", ("long",)),
    "usize": (8, "DW_ATE_unsigned", ("unsigned long",)),
    "bool": (1, "DW_ATE_boolean", ("bool",)),
    "f32": (4, "DW_ATE_float", ("float",)),
    "f64": (8, "DW_ATE_float", ("double",)),
    "char": (4, "DW_ATE_UTF", ("char32_t",)),
}


# Each Rust primitive and the C-style names LLDB gives it (TypeSystemClang reads Rust's
# DWARF base types by encoding and size), for showing a local by its Rust type.
RUST_SCALARS = {
    **{name: names for name, (_, _, names) in ELEMENT_TYPES.items()},
    "i128": ("__int128",),
    "u128": ("unsigned __int128",),
}


# LLDB names only one Rust primitive renders as: a struct field or parameter of these
# types is that primitive. `long` and `unsigned long` are i64/isize and u64/usize.
UNIQUE_SCALARS = {
    lldb: rust
    for rust, names in RUST_SCALARS.items()
    for lldb in names
    if sum(lldb in others for others in RUST_SCALARS.values()) == 1
}
_BYTE_TYPES = {"unsigned char": "u8", "char": "i8", "signed char": "i8"}
_BYTE_ESCAPES = {"0": 0, "a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, "e": 27}


def lldb_byte(text, kind):
    """LLDB's C character literal for a Rust u8 (`unsigned char`) or i8 (`char`) as Rust
    prints the integer (`'A'` -> `65`, `'\\xfd'` -> `253` or `-3`), else None."""
    rust = _BYTE_TYPES.get(kind)
    if rust is None or not isinstance(text, str) or len(text) < 3:
        return None
    if text[0] != "'" or text[-1] != "'":
        return None
    body = text[1:-1]
    if len(body) == 1 and " " <= body <= "~":
        value = ord(body)
    elif match := re.fullmatch(r"\\x([0-9a-fA-F]{1,2})", body):
        value = int(match[1], 16)
    elif match := re.fullmatch(r"\\([0-7]{1,3})", body):
        value = int(match[1], 8)
    elif len(body) == 2 and body[0] == "\\" and body[1] in _BYTE_ESCAPES:
        value = _BYTE_ESCAPES[body[1]]
    else:
        return None
    if value > 255:
        return None
    return str(value if rust == "u8" or value < 128 else value - 256)


_CHAR_ESCAPES = {0: "\\0", 9: "\\t", 10: "\\n", 13: "\\r", 39: "\\'", 92: "\\\\"}
_ESCAPED_STARTS = tuple(low for low, _ in ESCAPED)


def rust_char_debug(point):
    """A Unicode scalar value as Rust's `{:?}` prints it (`'b'`, `'\\n'`, `'\\u{200d}'`),
    from the pinned Rust's own table (rust_chars); None for a value that is no char."""
    if not 0 <= point <= 0x10FFFF or 0xD800 <= point <= 0xDFFF:
        return None
    if point in _CHAR_ESCAPES:
        return f"'{_CHAR_ESCAPES[point]}'"
    index = bisect.bisect_right(_ESCAPED_STARTS, point) - 1
    if index >= 0 and point <= ESCAPED[index][1]:
        return f"'\\u{{{point:x}}}'"
    return f"'{chr(point)}'"


def lldb_char(text, kind):
    """LLDB's `U+0062 U+0062 U'b'` (Apple) or `U+0x00000062 U+0x00000062 U'b'` (LLVM)
    for a Rust char (`char32_t`) as Rust's `{:?}` prints it, else None."""
    if kind != "char32_t" or not isinstance(text, str):
        return None
    match = re.fullmatch(r"U\+((?:0x)?[0-9A-Fa-f]{4,8}) U\+\1 U'.*'", text, re.S)
    return rust_char_debug(int(match[1].removeprefix("0x"), 16)) if match else None


def _vec_name(element):
    return f"alloc::vec::Vec<{element}, alloc::alloc::Global>"


# Containers whose contents Peeky reads from memory at compiler-validated offsets
# (LLDB's Rust formatter scripts are never loaded). Element: (DWARF name, size, encoding).
QUALIFIED_CONTAINERS = {
    **{
        name: (kind, (element, size, encoding))
        for element, (size, encoding, _) in ELEMENT_TYPES.items()
        for name, kind in (
            (_vec_name(element), "vec"),
            (f"&[{element}]", "slice"),
            (f"&mut [{element}]", "slice"),
        )
    },
    "alloc::string::String": ("string", ("u8", 1, "DW_ATE_unsigned")),
    "&str": ("str", ("u8", 1, "DW_ATE_unsigned")),
    "&mut str": ("str", ("u8", 1, "DW_ATE_unsigned")),
    # Read only as a field (#156): a `Box<str>` local is the Box gate's (#101).
    "alloc::boxed::Box<str, alloc::alloc::Global>": ("str", ("u8", 1, "DW_ATE_unsigned")),
}
# Reference parameters whose contents Peeky reads, by their declared (DWARF/IR) type:
# (LLDB's names for the parameter's type, qualified container, read through a pointer).
# Only parameters qualify: their referent outlives the call (see rust_ir).
_VEC = _vec_name("i32")
REFERENCE_PARAMETERS = {
    **{
        declared: entry
        for element, (_, _, lldb) in ELEMENT_TYPES.items()
        for declared, entry in (
            (f"&[{element}]", ((f"&[{element}]",), f"&[{element}]", False)),
            (f"&mut [{element}]", ((f"&mut [{element}]",), f"&mut [{element}]", False)),
            (
                f"&{_vec_name(element)}",
                (tuple(f"{_vec_name(n)} *" for n in lldb), _vec_name(element), True),
            ),
            (
                f"&mut {_vec_name(element)}",
                (tuple(f"{_vec_name(n)} *" for n in lldb), _vec_name(element), True),
            ),
        )
    },
    "&str": (("&str",), "&str", False),
    "&mut str": (("&mut str",), "&mut str", False),
    # A `for cell in cells` item of a `&[&str]` (#205): LLDB's `&str *`. What the `&str`
    # borrows outlives the reference to it (`&'b &'a str` needs `'a: 'b`).
    "&&str": (("&str *",), "&str", True),
    "&alloc::string::String": (("alloc::string::String *",), "alloc::string::String", True),
    "&mut alloc::string::String": (
        ("alloc::string::String *",),
        "alloc::string::String",
        True,
    ),
}
# Owned locals read when rust_mir proves them initialized: {LLDB type: qualified
# containers it can be}.
OWNED_CONTAINERS = {"alloc::string::String": ("alloc::string::String",)}
for _element, (_, _, _names) in ELEMENT_TYPES.items():
    for _name in _names:
        OWNED_CONTAINERS.setdefault(_vec_name(_name), ())
        OWNED_CONTAINERS[_vec_name(_name)] += (_vec_name(_element),)
CONTAINER_KINDS = frozenset(kind for kind, _ in QUALIFIED_CONTAINERS.values())


def _sized_base(die, name, size, encoding):
    return (
        die is not None
        and die.tag == "DW_TAG_base_type"
        and not die.children
        and die.attributes.get("DW_AT_name") == f'"{name}")'
        and die.attributes.get("DW_AT_encoding") == f"{encoding})"
        and die.attributes.get("DW_AT_byte_size") == f"0x{size:02x})"
    )


def _target(die, by_offset):
    return by_offset.get(_reference(die.attributes.get("DW_AT_type"))[0])


def _member(struct, name, by_offset):
    """(offset, type DIE) of the only member called ``name``; anything else refuses."""
    members = [
        child
        for child in struct.children
        if child.tag == "DW_TAG_member" and child.attributes.get("DW_AT_name") == f'"{name}")'
    ]
    if len(members) != 1:
        raise ValueError(f"Rust container member {name} is missing or repeated")
    offset = _hex(members[0].attributes.get("DW_AT_data_member_location"))
    target = _target(members[0], by_offset)
    if target is None:
        raise ValueError(f"Rust container member {name} has an unknown type")
    return offset, target


def _named_struct(die, name):
    if die.tag != "DW_TAG_structure_type" or die.attributes.get("DW_AT_name") != f'"{name}")':
        raise ValueError(f"Expected the Rust struct {name}")
    return die


def _pointer_to(die, by_offset, element):
    pointee = _target(die, by_offset) if die.tag == "DW_TAG_pointer_type" else None
    if not _sized_base(pointee, *element):
        raise ValueError("Rust container data is not a pointer to its element type")


def _usize(die):
    if not _sized_base(die, "usize", 8, "DW_ATE_unsigned"):
        raise ValueError("Rust container length is not a usize")


def _vec_parts(vec, element, by_offset, check=None):
    """(data pointer offset, length offset) of a Vec<T, Global> from its member chain.
    ``check(die)`` validates a composite element type instead of a scalar one."""
    name, size, encoding = element
    params = [
        child
        for child in vec.children
        if child.tag == "DW_TAG_template_type_parameter"
        and child.attributes.get("DW_AT_name") == '"T")'
    ]
    target = _target(params[0], by_offset) if len(params) == 1 else None
    if not (check(target) if check else _sized_base(target, name, size, encoding)):
        raise ValueError("Rust Vec element type is not the qualified one")
    buf, raw_vec = _member(vec, "buf", by_offset)
    length, length_type = _member(vec, "len", by_offset)
    _usize(length_type)
    _named_struct(raw_vec, f"RawVec<{name}, alloc::alloc::Global>")
    inner, raw_inner = _member(raw_vec, "inner", by_offset)
    _named_struct(raw_inner, "RawVecInner<alloc::alloc::Global>")
    ptr, unique = _member(raw_inner, "ptr", by_offset)
    cap, cap_type = _member(raw_inner, "cap", by_offset)
    _named_struct(cap_type, "UsizeNoHighBit")
    cap_value, cap_usize = _member(cap_type, "__0", by_offset)
    _usize(cap_usize)
    _named_struct(unique, "Unique<u8>")
    first, non_null = _member(unique, "pointer", by_offset)
    _named_struct(non_null, "NonNull<u8>")
    second, raw = _member(non_null, "pointer", by_offset)
    _pointer_to(raw, by_offset, ("u8", 1, "DW_ATE_unsigned"))
    return buf + inner + ptr + first + second, length, buf + inner + cap + cap_value


def _container_layout(struct, qualified, by_offset):
    kind, element = QUALIFIED_CONTAINERS[qualified]
    try:
        size = _hex(struct.attributes.get("DW_AT_byte_size"))
        capacity = None
        if kind == "vec":
            data, length, capacity = _vec_parts(struct, element, by_offset)
        elif kind == "string":
            offset, vec = _member(struct, "vec", by_offset)
            _named_struct(vec, "Vec<u8, alloc::alloc::Global>")
            data, length, capacity = (part + offset for part in _vec_parts(vec, element, by_offset))
        else:
            data, pointer = _member(struct, "data_ptr", by_offset)
            _pointer_to(pointer, by_offset, element)
            length, length_type = _member(struct, "length", by_offset)
            _usize(length_type)
    except (KeyError, ValueError):
        return {"kind": "unqualified"}
    fields = sorted((start, start + 8) for start in (data, length, capacity) if start is not None)
    if any(a[1] > b[0] for a, b in zip(fields, fields[1:])) or fields[-1][1] > size or size > 64:
        return {"kind": "unqualified"}
    return {
        "kind": kind,
        "size": size,
        "data": data,
        "length": length,
        **({"capacity": capacity} if capacity is not None else {}),
        "element": element[0],
        "element_size": element[1],
    }


def composite_container(name):
    """(kind, element) for a `Vec<T>` or `&[T]` whose element is not a scalar
    (`Vec<(char, usize)>`, `&[demo::Interval]`): read only when the DWARF shows T is a
    tuple or struct of qualified scalars. None for anything else, including a scalar
    container in LLDB's spelling (`Vec<int>`, `Vec<char32_t>`)."""
    if not isinstance(name, str) or name in QUALIFIED_CONTAINERS:
        return None
    if match := re.fullmatch(r"alloc::vec::Vec<(.+), alloc::alloc::Global>", name):
        kind, element = "vec", match[1]
    elif match := re.fullmatch(r"&(?:mut )?\[(.+)\]", name):
        kind, element = "slice", match[1]
    else:
        return None
    return None if _PRIMITIVE.fullmatch(element) else (kind, element)


_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


_STRING = "alloc::string::String"


def _qualified_names(root):
    """{offset: name} of the unit's named types: a struct, enum or union by its whole
    path (`alloc::string::String`), anything else (a pointer, a base type) by its own
    name (`*const kitty::Friend`, `i128`)."""
    names = {}

    def visit(die, prefix):
        for child in die.children:
            name = child.attributes.get("DW_AT_name")
            if child.tag == "DW_TAG_namespace" and name:
                visit(child, (*prefix, _string(name)))
            elif child.tag in ("DW_TAG_structure_type", "DW_TAG_union_type") and name:
                names[child.offset] = "::".join((*prefix, _string(name)))
                visit(child, (*prefix, _string(name)))
            elif name and child.tag in ("DW_TAG_pointer_type", "DW_TAG_base_type"):
                names[child.offset] = _string(name)

    try:
        visit(root, ())
    except ValueError:
        return {}
    return names


def _element_field(target, by_offset, names):
    """How a field of a container's element is read (#260): a `String`'s text, by its
    validated layout ({"string": layout}), or not at all ({"unread": its type's name or
    None})."""
    if target is not None and names.get(target.offset) == _STRING:
        layout = _container_layout(target, _STRING, by_offset)
        if layout["kind"] == "string":
            return {"string": {key: layout[key] for key in ("size", "data", "length", "capacity")}}
    return {"unread": names.get(target.offset) if target is not None else None}


def _composite(die, by_offset, names=None):
    """A tuple or struct whose fields are all qualified scalars, from its DWARF: its
    size, fields (name, offset, scalar) in declaration order, and how Rust's derived
    `{:?}` shows it (`(a, b)`, `Point(a, b)`, `Interval { start: a, end: b }`).

    With ``names`` (_qualified_names), a container's element (#260): a field may also
    be a `String` (name, offset, `alloc::string::String`; its layout under "string"),
    read as text, or any other type (name, offset, None, its type's name), not read;
    at least one field must be read."""
    if die is None or die.tag != "DW_TAG_structure_type":
        raise ValueError("Rust element is not a tuple or struct")
    if any(
        child.tag not in ("DW_TAG_member", "DW_TAG_template_type_parameter")
        # rustc declares each inherent method the program calls (`fn width(&self)`)
        # inside its struct; that adds no storage.
        and not (child.tag == "DW_TAG_subprogram" and "DW_AT_declaration" in child.attributes)
        for child in die.children
    ):
        raise ValueError("Rust element has more than plain fields (an enum's variants)")
    name, size = (
        _string(die.attributes.get("DW_AT_name")),
        _hex(die.attributes.get("DW_AT_byte_size")),
    )
    members = [child for child in die.children if child.tag == "DW_TAG_member"]
    if not 0 < size <= 256 or not 0 < len(members) <= 16:
        raise ValueError("Rust element size or field count is out of bounds")
    fields, text, widths = [], None, {}
    for member in members:
        target = _target(member, by_offset)
        scalar = next(
            (
                element
                for element, (width, encoding, _) in ELEMENT_TYPES.items()
                if _sized_base(target, element, width, encoding)
            ),
            None,
        )
        field = _string(member.attributes.get("DW_AT_name"))
        offset = _hex(member.attributes.get("DW_AT_data_member_location"))
        if scalar is not None:
            fields.append([field, offset, scalar])
            widths[field] = ELEMENT_TYPES[scalar][0]
            continue
        if names is None:
            raise ValueError("Rust element field is not a qualified scalar")
        found = _element_field(target, by_offset, names)
        if "string" in found and text not in (None, found["string"]):
            raise ValueError("Rust element's String fields disagree on their layout")
        if "string" in found:
            text = found["string"]
            fields.append([field, offset, _STRING])
            # A String is three words, aligned as one.
            widths[field] = 8
        else:
            fields.append([field, offset, None, found["unread"]])
    if not widths:
        raise ValueError("Rust element has no field Peeky reads")
    spans = sorted(
        (offset, offset + (text["size"] if kind == _STRING else widths[field]))
        for field, offset, kind, *_ in fields
        if field in widths
    )
    if any(a[1] > b[0] for a, b in zip(spans, spans[1:])) or spans[-1][1] > size:
        raise ValueError("Rust element fields overlap or exceed its size")
    if any(offset > size for _, offset, *_ in fields):
        raise ValueError("Rust element field is past its end")
    numbered = all(field == f"__{index}" for index, (field, *_) in enumerate(fields))
    if name.startswith("("):
        # rustc names a 1-tuple `(i8)` here (its MIR and `{:?}` say `(i8,)`).
        parts = [kind or (rest[0] if rest else None) for _, _, kind, *rest in fields]
        if None in parts:
            raise ValueError("Rust tuple part has no name to check")
        expected = "(" + ", ".join(parts) + ")"
        if not numbered or name != expected:
            raise ValueError("Rust tuple does not match its fields")
        shape = "tuple"
    elif not _FIELD_NAME.fullmatch(name.split("<")[0]):
        raise ValueError("Rust struct name is not plain")
    elif numbered:
        shape = "tuple-struct"
    elif all(_FIELD_NAME.fullmatch(field) for field, *_ in fields):
        shape = "struct"
    else:
        raise ValueError("Rust struct field names are not plain")
    # A plain struct or tuple is aligned as its widest field (each a power of two).
    alignment = max(widths.values())
    if size % alignment or any(
        offset % widths[field] for field, offset, *_ in fields if field in widths
    ):
        raise ValueError("Rust element is not laid out as its fields' alignment requires")
    declared = re.fullmatch(r"([0-9]+)\)", die.attributes.get("DW_AT_alignment") or "")
    if declared and int(declared[1]) < alignment:
        # `#[repr(packed)]`, even where its fields happen to fall aligned.
        raise ValueError("Rust struct is packed")
    return {
        "element_size": size,
        "alignment": alignment,
        "fields": fields,
        "shape": shape,
        # Derived Debug prints the struct's own name, without its path or generics.
        "shown": name.split("<")[0],
        **({"string": text} if text else {}),
    }


# A container of composites whose element is not readable: it stays raw, and a readable
# container of the same name (another struct with that path, in a function's block or
# another unit) can then not be told apart from it.
_UNREAD = {"kind": "unread"}


def _composite_layout(struct, kind, element, by_offset, names=None):
    """The layout of a `Vec<T>` or `&[T]` of composites; _UNREAD when T is not a
    tuple or struct with a field Peeky reads (see _composite: ``names`` lets fields be
    `String`s, or not read); None for a declaration without a body."""
    params = [
        child
        for child in struct.children
        if child.tag == "DW_TAG_template_type_parameter"
        and child.attributes.get("DW_AT_name") == '"T")'
    ]
    target = _target(params[0], by_offset) if len(params) == 1 else None
    if target is not None and "DW_AT_declaration" in target.attributes:
        return None
    try:
        size = _hex(struct.attributes.get("DW_AT_byte_size"))
        capacity = None
        if kind == "vec":
            params = [
                child
                for child in struct.children
                if child.tag == "DW_TAG_template_type_parameter"
                and child.attributes.get("DW_AT_name") == '"T")'
            ]
            target = _target(params[0], by_offset) if len(params) == 1 else None
            composite = _composite(target, by_offset, names)
            data, length, capacity = _vec_parts(
                struct,
                (element, composite["element_size"], None),
                by_offset,
                check=lambda die: die is target,
            )
        else:
            data, pointer = _member(struct, "data_ptr", by_offset)
            pointee = _target(pointer, by_offset) if pointer.tag == "DW_TAG_pointer_type" else None
            composite = _composite(pointee, by_offset, names)
            length, length_type = _member(struct, "length", by_offset)
            _usize(length_type)
    except (KeyError, ValueError, IndexError):
        return _UNREAD
    fields = sorted((start, start + 8) for start in (data, length, capacity) if start is not None)
    if any(a[1] > b[0] for a, b in zip(fields, fields[1:])) or fields[-1][1] > size or size > 64:
        return _UNREAD
    return {
        "kind": kind,
        "size": size,
        "data": data,
        "length": length,
        **({"capacity": capacity} if capacity is not None else {}),
        "element": element,
        **composite,
    }


# Struct and tuple layouts are kept beside the containers', under their own keys: an enum
# is a DWARF structure too, and its layout is keyed by its bare name.
STRUCT_PREFIX = "struct:"


def _struct_layout(die, by_offset):
    """A tuple's or struct's layout for its preview (#106): its fields are qualified
    scalars (see _composite); a unit struct (`struct Marker;`) has none. _UNREAD for any
    other, so that a struct of the same name is not read either."""
    plain = all(
        child.tag == "DW_TAG_template_type_parameter"
        or (child.tag == "DW_TAG_subprogram" and "DW_AT_declaration" in child.attributes)
        for child in die.children
    )
    try:
        name = _string(die.attributes.get("DW_AT_name"))
        if plain and _hex(die.attributes.get("DW_AT_byte_size")) == 0:
            if name.startswith("(") or not _FIELD_NAME.fullmatch(name.split("<")[0]):
                return _UNREAD
            return {"kind": "unit", "shown": name.split("<")[0]}
        return {"kind": "composite", **_composite(die, by_offset)}
    except (KeyError, ValueError, IndexError, TypeError):
        return _UNREAD


def container_layouts(text, unit, root=None):
    """Layouts of the qualified containers (`Vec<i32>`, `&[i32]`, `String`, `&str`) that
    one application unit defines, validated member by member; anything else refuses.
    ``root``: the unit's entries, if already parsed from ``text``."""
    (root,) = parse_dies(text, units=False) if root is None else (root,)
    if root.offset != unit.die or root.tag != "DW_TAG_compile_unit":
        raise ValueError("Rust debug information does not match its application unit")
    by_offset = {}
    _walk(root, by_offset, unit)
    layouts = {}
    # Structs are read only from the crate itself: the standard library's (`Range`,
    # `Wrapping`) implement their own Debug, which a derived rendering would misstate.
    crate = unit.name.rpartition("/@/")[2].split(".")[0]

    structs = {}
    names = _qualified_names(root)

    def visit(die, prefix):
        for child in die.children:
            name = child.attributes.get("DW_AT_name")
            if child.tag == "DW_TAG_namespace" and name:
                visit(child, (*prefix, _string(name)))
            elif child.tag == "DW_TAG_structure_type" and name is not None:
                qualified = "::".join((*prefix, _string(name)))
                if (
                    qualified.startswith("(") or qualified.startswith(f"{crate}::")
                ) and "DW_AT_declaration" not in child.attributes:
                    # A tuple or a struct of the crate's own, for its preview (#106).
                    layout = _struct_layout(child, by_offset)
                    if structs.get(qualified, layout) != layout:
                        layout = {"kind": "unqualified"}
                    structs[qualified] = layout
                if qualified in QUALIFIED_CONTAINERS:
                    layout = _container_layout(child, qualified, by_offset)
                    if layouts.get(qualified, layout) != layout:
                        layout = {"kind": "unqualified"}
                    layouts[qualified] = layout
                elif (composite := composite_container(qualified)) and (
                    composite[1].startswith("(") or composite[1].startswith(f"{crate}::")
                ):
                    layout = _composite_layout(child, *composite, by_offset, names)
                    if layout is not None and layouts.get(qualified, layout) != layout:
                        layout = {"kind": "unqualified"}
                    if layout is not None:
                        layouts[qualified] = layout

    visit(root, ())
    if len(structs) <= MAX_LAYOUTS:
        # Too many to keep: none is previewed (a twin left out could share a name).
        layouts.update({STRUCT_PREFIX + name: layout for name, layout in structs.items()})
    return layouts


def enum_layouts(text, unit, root=None):
    """Map qualified names of data-carrying enums in one application unit to layouts.
    ``root``: the unit's entries, if already parsed from ``text``."""
    (root,) = parse_dies(text, units=False) if root is None else (root,)
    if root.offset != unit.die or root.tag != "DW_TAG_compile_unit":
        raise ValueError("Rust debug information does not match its application unit")
    by_offset = {}
    _walk(root, by_offset, unit)
    layouts = {}
    names = _qualified_names(root)
    crate = unit.name.rpartition("/@/")[2].split(".")[0]

    def visit(die, prefix, named):
        for child in die.children:
            name = child.attributes.get("DW_AT_name")
            if child.tag == "DW_TAG_namespace":
                visit(child, (*prefix, _string(name)) if name else prefix, named and bool(name))
            elif child.tag == "DW_TAG_structure_type" and name is not None:
                qualified = "::".join((*prefix, _string(name)))
                if any(part.tag == "DW_TAG_variant_part" for part in child.children):
                    layout = (
                        _option_layout(child, qualified, by_offset)
                        if named
                        else {"kind": "unqualified"}
                    )
                    if named and layout["kind"] == "unqualified":
                        layout = _composite_option_layout(child, qualified, by_offset, names, crate)
                    if layouts.get(qualified, layout) != layout:
                        layout = {"kind": "unqualified"}
                    layouts[qualified] = layout
                    if len(layouts) > MAX_LAYOUTS:
                        raise ValueError("Too many Rust enum layouts")
                visit(child, (*prefix, _string(name)), named)
            elif child.children:
                visit(child, prefix, False)

    visit(root, (), True)
    return layouts


def application_symbols(
    dwarfdump, symbols, workspace, env, *, entrypoint, crate, declared, budget=SYMBOL_DEADLINE_S
):
    """Return declared-source lines, enum layouts, the application units and their
    functions' lexical blocks (``code_blocks``) from application units only."""
    workspace = Path(workspace).resolve(strict=True)
    if type(budget) not in (int, float) or not 0 < budget <= SYMBOL_DEADLINE_S:
        raise ValueError("Rust symbol extraction has no remaining time budget")
    deadline = _clock() + budget

    def dump(arguments):
        remaining = deadline - _clock()
        if remaining <= 0:
            raise ValueError("Rust symbol extraction exceeded its shared deadline")
        return _metadata(
            dwarfdump,
            arguments,
            workspace,
            env,
            max_bytes=RUST_METADATA,
            timeout=min(15, remaining),
        )

    index = dump(["--debug-info", "--recurse-depth=0", str(symbols)])
    units = application_units(index, workspace, entrypoint=entrypoint, crate=crate)
    tables, layouts, blocks = [], {}, {}
    for unit in units:
        table = dump([f"--debug-line=0x{unit.stmt_list:08x}", str(symbols)])
        offsets = re.findall(r"^debug_line\[0x([0-9a-f]{8,16})\]$", table, re.MULTILINE)
        if [int(offset, 16) for offset in offsets] != [unit.stmt_list]:
            raise ValueError("Rust line table does not match its application unit")
        tables.append(table)
        info = dump([f"--debug-info=0x{unit.die:08x}", "--show-children", str(symbols)])
        (root,) = parse_dies(info, units=False)
        found = {**enum_layouts(info, unit, root), **container_layouts(info, unit, root)}
        for name, layout in found.items():
            if layouts.get(name, layout) != layout:
                layout = {"kind": "unqualified"}
            layouts[name] = layout
        try:
            functions = code_blocks(root, unit)
        except ValueError:
            # Only a refinement (#115): without them every stop of a line counts.
            functions = {}
        for hashed, function in functions.items():
            blocks[hashed] = None if hashed in blocks else function
    lines = parse_lines("\n".join(tables), workspace, declared, limit=RUST_METADATA, rust=True)
    return lines, layouts, units, blocks


_LEGACY_HASH = re.compile(r"17(h[0-9a-f]{16})E$")
_RANGE = re.compile(r"\[(0x[0-9a-f]+), (0x[0-9a-f]+)\)")
# Linkers write these for discarded code (COMDAT copies, dead functions).
_TOMBSTONE = 0xFFFF_FFFF_0000_0000


@dataclass(frozen=True)
class CodeBlock:
    """A function's own scope, one of its DWARF lexical blocks, or an inlined call in
    it: where its code is, and the names of the variables it declares (#115)."""

    parent: int | None  # the enclosing block's index; None for the function's own
    ranges: tuple  # ((low, high), ...), addresses as the symbols number them
    names: tuple  # sorted
    inlined: bool = False


def code_blocks(root, unit):
    """{legacy hash: (CodeBlock, ...)} for the functions an application unit's code
    defines, each function's own scope first, then its lexical blocks and inlined calls
    in DWARF order (an inlined call's own blocks are left out). A block whose code
    cannot be read gives the function none (an empty tuple)."""
    if root.offset != unit.die or root.tag != "DW_TAG_compile_unit":
        raise ValueError("Rust debug information does not match its application unit")
    found, by_offset = {}, {}

    def index(die):
        by_offset[die.offset] = die
        for child in die.children:
            index(child)

    index(root)

    def visit(die):
        for child in die.children:
            if child.tag == "DW_TAG_subprogram":
                linkage = child.attributes.get("DW_AT_linkage_name")
                if linkage is None and "DW_AT_specification" in child.attributes:
                    # A method's code completes its declaration in its type.
                    declared = by_offset.get(_reference(child.attributes["DW_AT_specification"])[0])
                    linkage = declared and declared.attributes.get("DW_AT_linkage_name")
                hashed = _LEGACY_HASH.search(_string(linkage)) if linkage else None
                if hashed and "DW_AT_low_pc" in child.attributes:
                    blocks = []
                    if not _code_block(child, None, blocks):
                        blocks = []
                    found[hashed[1]] = None if hashed[1] in found else tuple(blocks)
            elif child.tag in ("DW_TAG_namespace", "DW_TAG_structure_type"):
                visit(child)

    visit(root)
    return found


def _code_block(die, parent, blocks):
    """Add ``die`` and the blocks in it to ``blocks``; False if its code is not read."""
    ranges = _code_ranges(die)
    if not ranges:
        return False
    inlined = die.tag == "DW_TAG_inlined_subroutine"
    names = []
    if not inlined:
        for child in die.children:
            if child.tag in ("DW_TAG_variable", "DW_TAG_formal_parameter"):
                name = child.attributes.get("DW_AT_name")
                if name is not None:
                    names.append(_string(name))
    index = len(blocks)
    blocks.append(CodeBlock(parent, ranges, tuple(sorted(names)), inlined))
    if inlined:
        return True
    return all(
        _code_block(child, index, blocks)
        for child in die.children
        if child.tag in ("DW_TAG_lexical_block", "DW_TAG_inlined_subroutine")
    )


def _code_ranges(die):
    attributes = die.attributes
    if "DW_AT_low_pc" in attributes and "DW_AT_high_pc" in attributes:
        low = attributes["DW_AT_low_pc"].removesuffix(")")
        high = attributes["DW_AT_high_pc"].removesuffix(")")
        if not re.fullmatch(r"0x[0-9a-f]{16}", low) or not re.fullmatch(r"0x[0-9a-f]{1,16}", high):
            return ()
        # llvm-dwarfdump prints an address with all its 16 digits, and DWARF 4's high_pc
        # as the end it computes; one it cannot compute stays the size (a constant).
        end = int(high, 16) if len(high) == 18 else int(low, 16) + int(high, 16)
        spans = [(int(low, 16), end)]
    elif "DW_AT_ranges" in attributes:
        rows = [attributes["DW_AT_ranges"], *die.continued.get("DW_AT_ranges", ())]
        spans = [(int(low, 16), int(high, 16)) for row in rows for low, high in _RANGE.findall(row)]
    else:
        return ()
    if any(not 0 < low < high or high >= _TOMBSTONE for low, high in spans):
        return ()
    return tuple(spans)


def _named(values, names):
    if (
        not isinstance(values, list)
        or [value.get("name") if isinstance(value, dict) else None for value in values] != names
    ):
        raise ValueError("Rust enum debugger shape does not match its compiled layout")
    return values


def _reference_of(variable):
    reference = variable.get("variablesReference")
    if type(reference) is not int or reference <= 0:
        raise ValueError("Rust enum debugger value is not expandable")
    return reference


def _option_value(variable, fetch, layout, kind):
    (inner,) = _named(fetch(_reference_of(variable)), ["$variants$"])
    if inner.get("type") != f"{kind}::{kind}$Inner":
        raise ValueError("Rust enum storage type mismatch")
    variants = _named(
        fetch(_reference_of(inner)),
        # LLDB names each variant field after its DW_AT_discr_value, in DWARF order.
        [f"$variant${value}" for value, _ in layout["variants"]],
    )
    discriminants, values = set(), {}
    for (_, name), child in zip(layout["variants"], variants):
        if child.get("type") != f"{kind}::{kind}$Inner::{name}$Variant":
            raise ValueError("Rust enum variant type mismatch")
        tag, value = _named(fetch(_reference_of(child)), ["$discr$", "value"])
        if value.get("type") != f"{kind}::{name}<{QUALIFIED_OPTIONS[kind][1]}>":
            raise ValueError("Rust enum variant payload type mismatch")
        text = tag.get("value")
        if (
            tag.get("type") != DISCRIMINANT_TYPES[layout["discriminant"]]
            or tag.get("variablesReference", 0) != 0
            or not isinstance(text, str)
            or not re.fullmatch(r"0|[1-9][0-9]{0,19}", text)
        ):
            raise ValueError("Rust enum discriminant is not a bounded unsigned integer")
        discriminants.add(int(text))
        values[name] = value
    if len(discriminants) != 1:
        raise ValueError("Rust enum discriminant views disagree")
    active = {value: name for value, name in layout["variants"]}.get(discriminants.pop())
    if active is None:
        raise ValueError("Rust enum discriminant is not a compiled variant")
    if active == "None":
        return "None"
    (payload,) = _named(fetch(_reference_of(values[active])), ["__0"])
    text = payload.get("value")
    if (
        payload.get("type") != QUALIFIED_OPTIONS[kind][1]
        or payload.get("variablesReference", 0) != 0
        or not isinstance(text, str)
        or not re.fullmatch(r"-?(?:0|[1-9][0-9]{0,9})", text)
        or not -(2**31) <= int(text) < 2**31
    ):
        raise ValueError("Rust Option payload is not a qualified i32")
    return f"Some({int(text)})"


_IDENTIFIER = re.compile(r"[^\W\d]\w*")
# Why an enum is not decoded, when its layout is not one Peeky reads.
UNQUALIFIED_WHY = (
    "Peeky decodes `Option<i32>`, and an `Option` of a tuple or struct your crate defines "
    "whose layout the debug information proves; it does not decode this `{kind}`"
)


def enum_address(variable):
    """Where the debugger says a value is stored (its memoryReference, and the `T @ 0x…`
    it shows, which must agree), as a pointer prints (`0x000000016fbfab10`); None if it
    says neither or they disagree."""
    found = set()
    reference = variable.get("memoryReference")
    if isinstance(reference, str) and _ADDRESS.fullmatch(reference):
        found.add(int(reference, 16))
    shown, at, place = str(variable.get("value") or "").rpartition(" @ ")
    if at and _ADDRESS.fullmatch(place):
        found.add(int(place, 16))
    if len(found) != 1 or 0 in found:
        return None
    return f"0x{found.pop():016x}"


_RESULT_NAME = re.compile(r"core::result::Result<.+>")


def binding_advice(variable, top):
    """What to read instead of an enum Peeky does not decode, where it holds a value:
    what an `if let` on it binds (`Some(x)` of an `Option`, `Ok(x)` or `Err(e)` of a
    `Result`), or a `match` arm, naming a root local (``top``)."""
    name = str(variable.get("name") or "").partition(" @ ")[0]
    kind = str(variable.get("type") or "")
    named = top and _IDENTIFIER.fullmatch(name)
    if _OPTION_NAME.fullmatch(kind):
        if named:
            return f"read what `if let Some(x) = &{name}` binds instead"
        return "read what an `if let Some(x)` on it binds instead"
    if _RESULT_NAME.fullmatch(kind):
        if named:
            return f"read what `if let Ok(x) = &{name}` or `Err(e)` binds instead"
        return "read what an `if let Ok(x)` or `Err(e)` on it binds instead"
    return "read what a `match` arm on it binds instead"


def uninitialized_advice(name):
    """What to do where the MIR does not prove a local initialized: there may be nothing
    in it to borrow, so read it where it is."""
    return (
        f"read it on a line where `{name}` is assigned and not yet moved or dropped, such as "
        "the one after its assignment"
    )


CALLER_ADVICE = "stop at a line of the function it belongs to, to read it there"
OPTIMIZED_ADVICE = 'read it in an unoptimized build (`opt_level` "0", the default)'


def enum_withheld(variable, value, why, *, top=False, instead=None):
    """An enum shown as ``value`` (UNQUALIFIED or UNVERIFIED), its storage never read,
    with ``why``, the address it is stored at (which a pointer into it can be compared
    with) and what to do: ``instead``, else what an `if let` or a `match` on it binds
    (binding_advice), which needs it to hold a value."""
    address = enum_address(variable)
    where = f"It is stored at {address}; " if address else "Its address is not known; "
    advice = instead if instead is not None else binding_advice(variable, top)
    return {
        "name": variable.get("name"),
        "type": variable.get("type"),
        "variablesReference": 0,
        "value": value,
        "rust_view": "opaque",
        "why": f"{why}. {where}{advice}",
    }


def option_view(variable, read, layout):
    """A local `Option` of a tuple or struct read from memory by its proven layout (see
    _composite_option_layout), as Rust's derived `{:?}` shows it: `None`, or
    `Some(Engineer { name: "Björn", shifts: 1, .. })` with the payload as its child
    `__0`, whose fields are its children as a container element's are (#260). A tag
    holding neither variant's value, or a payload that does not read (a String's length
    past its capacity), is withheld as UNVERIFIED, never a guess.

    ``read(address, count)`` returns exactly ``count`` bytes or raises; None where the
    debugger cannot read memory."""
    from peeky.rust import rust_spelling

    kind = variable.get("type")
    name = str(variable.get("name") or "").partition(" @ ")[0]
    view = {"name": variable.get("name"), "type": kind, "variablesReference": 0}
    if read is None:
        why = (
            "Peeky reads this `Option` from memory, and this debugger cannot read memory "
            "(lldb-dap 20 or later can)"
        )
        return enum_withheld(variable, UNVERIFIED, why, top=True)
    reference, address = variable.get("memoryReference"), enum_address(variable)
    if not isinstance(reference, str):
        why = (
            "Peeky reads this `Option` from memory, and the debugger gives no memory "
            "reference for it (lldb-dap 20 or later does)"
        )
        return enum_withheld(variable, UNVERIFIED, why, top=True)
    if address is None:
        why = "Peeky reads this `Option` from memory, and the debugger's address for it is unclear"
        return enum_withheld(variable, UNVERIFIED, why, top=True)
    try:
        raw = read(address, layout["size"])
    except ViewBudgetExhausted as exc:
        return {**view, "value": _budget(exc), "rust_view": "budget-exhausted"}
    except ValueError as exc:
        detail = str(exc)
        why = (
            "Peeky reads this `Option` from memory, and the debugger did not read it "
            f"({detail[:1].lower() + detail[1:200]})"
        )
        return enum_withheld(variable, UNVERIFIED, why, top=True)
    try:
        at, width = layout["tag"]
        tag = int.from_bytes(raw[at : at + width], "little")
        if tag == layout["none"]:
            return {**view, "value": "None", "rust_view": "active-variant"}
        valid = layout["valid"]
        if not (valid[0] <= tag <= valid[1] if valid else tag == layout["some"]):
            raise ValueError(f"its tag holds {tag}, the value of neither `None` nor `Some`")
        start = layout["payload"]
        through = (
            f"`x` of `if let Some(x) = &{name}`"
            if _IDENTIFIER.fullmatch(name)
            else "what an `if let Some(x)` on it binds"
        )
        values, children = _partial_elements(
            layout, [raw[start : start + layout["element_size"]]], read, through=through
        )
    except ViewBudgetExhausted as exc:
        return {**view, "value": _budget(exc), "rust_view": "budget-exhausted"}
    except ValueError as exc:
        detail = str(exc)
        why = (
            f"its bytes here do not hold a valid `{rust_spelling(kind)}`, so Peeky shows none "
            f"of it ({detail[:1].lower() + detail[1:200]})"
        )
        return enum_withheld(variable, UNVERIFIED, why, top=True)
    return {
        **view,
        "value": f"Some({values[0]})",
        "rust_view": "active-variant",
        "children": [{**children[0], "name": "__0"}],
    }


def option_layouts(kind, layouts):
    """The names of the `Option` layouts read from memory (OPTION_COMPOSITE) that LLDB's
    type name ``kind`` can be (see _spelled)."""
    if not isinstance(kind, str) or not _OPTION_NAME.fullmatch(kind):
        return []
    return [
        name
        for name, layout in layouts.items()
        if layout.get("kind") == OPTION_COMPOSITE and (name == kind or _spelled(name, kind))
    ]


def _without_lifetimes(name):
    """A MIR type without its lifetimes, which the debug information never names:
    `Pick<'_>` is `Pick`, `Pair<'a, T>` is `Pair<T>`, `&'a T` is `&T`."""
    name = re.sub(r"<(?:'\w+(?:, )?)+>", "", name)
    name = re.sub(r"(?<=<)(?:'\w+, )+", "", name)
    return re.sub(r"&'\w+ ", "&", name)


def option_name(declared, shown, layouts):
    """The `Option` layout an owned local is, by the MIR's declared type
    (`std::option::Option<Engineer>`) and confirmed by LLDB's name (see struct_name);
    None unless exactly one of the crate's tuples and structs is its payload."""
    exact = re.fullmatch(r"(?:std|core)::option::Option<(.+)>", str(declared or ""))
    lldb = _OPTION_NAME.fullmatch(str(shown or ""))
    if not exact or not lldb:
        return None
    element = struct_name(_without_lifetimes(exact[1]), lldb[1], layouts)
    name = f"core::option::Option<{element}>" if element else None
    return name if name in option_layouts(shown, layouts) else None


def active_variant(variable, fetch, layouts, *, top=False):
    """Worker view for a compiled Rust enum value, or None for non-enum values.

    ``fetch(reference)`` must return bounded raw DAP children. Only the active
    variant's payload is read; inactive storage is never interpreted or exposed.
    An enum it does not decode says why, where it is stored, and what to read instead
    (``top``: it is a root local, which the advice names).
    """
    kind = variable.get("type")
    layout = layouts.get(kind) if isinstance(kind, str) else None
    # A Vec or slice of composites is never an enum, whatever its layout's state.
    if layout is None or layout["kind"] in CONTAINER_KINDS or composite_container(kind):
        return None
    view = {"name": variable.get("name"), "type": kind, "variablesReference": 0}
    if kind in QUALIFIED_CONTAINERS:
        return {**view, "value": UNQUALIFIED, "rust_view": "opaque"}
    if layout["kind"] == OPTION_COMPOSITE:
        why = (
            "Peeky reads an `Option` of a tuple or struct only as a local of the innermost "
            "frame, where the build's MIR proves it initialized"
        )
        return enum_withheld(variable, UNVERIFIED, why, top=top)
    if layout["kind"] != "option":
        from peeky.rust import rust_spelling

        why = UNQUALIFIED_WHY.format(kind=rust_spelling(kind))
        return enum_withheld(variable, UNQUALIFIED, why, top=top)
    try:
        value = _option_value(variable, fetch, layout, kind)
    except ViewBudgetExhausted as exc:
        return {**view, "value": _budget(exc), "rust_view": "budget-exhausted"}
    except ValueError:
        why = "the debugger's view of it does not match its compiled layout"
        return enum_withheld(variable, UNVERIFIED, why, top=top)
    return {**view, "value": value, "rust_view": "active-variant"}


# Why a caller frame's container is not trusted (rust.py's reasons name it in these
# words): the call under way may be part-way through changing it, so a note never offers
# one of its fields as its length (#260).
CALLER_CHANGING = "the call under way"
MAX_ELEMENTS = 256
MAX_TEXT_BYTES = 4096
# A shown value stays well inside the worker's 2000-character value limit, so its
# "(first k of N)" marker is never cut off.
MAX_SHOWN = 1000
UNVERIFIED_CONTAINER = "<unverified Rust container value>"
UNQUALIFIED_CONTAINER = "<unqualified Rust container layout>"
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{1,16}")


def _word(raw, offset):
    return int.from_bytes(raw[offset : offset + 8], "little")


def _shown_list(elements, length):
    """`[a, b, c]`, or the longest whole-element prefix that fits plus its marker."""
    texts = [str(element) for element in elements]
    whole = "[" + ", ".join(texts) + "]"
    if len(texts) == length and len(whole) <= MAX_SHOWN:
        return whole
    room = MAX_SHOWN - len(f", ...] (first {len(texts)} of {length})") - 1
    kept, used = [], 0
    for text in texts:
        used += len(text) + (2 if kept else 0)
        if used > room:
            break
        kept.append(text)
    return "[" + ", ".join(kept + ["..."]) + f"] (first {len(kept)} of {length})"


def _shown_text(text, length):
    """The quoted text, or its longest whole-character prefix that fits plus a marker."""
    whole = json.dumps(text, ensure_ascii=False)
    if len(text.encode()) == length and len(whole) <= MAX_SHOWN:
        return whole
    room = MAX_SHOWN - len(f" (first {length} of {length} bytes)")
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(text[:middle], ensure_ascii=False)) <= room:
            low = middle
        else:
            high = middle - 1
    prefix = text[:low]
    shown = json.dumps(prefix, ensure_ascii=False)
    return f"{shown} (first {len(prefix.encode())} of {length} bytes)"


def _one_layout(names, layouts):
    """The layout an owned local's LLDB type names: one candidate, or several (i64 and
    isize) whose layouts agree in everything but the element's name; else None."""
    if not names:
        return None
    if len(names) == 1:
        return layouts[names[0]]
    if any(layouts[n].get("kind") == "unqualified" for n in names):
        return None
    shapes = {
        json.dumps({k: v for k, v in layouts[n].items() if k != "element"}, sort_keys=True)
        for n in names
    }
    encodings = {ELEMENT_TYPES[QUALIFIED_CONTAINERS[n][1][0]][:2] for n in names}
    if len(shapes) != 1 or len(encodings) != 1:
        return None
    return {**layouts[names[0]], "element": " or ".join(layouts[n]["element"] for n in names)}


def _decoder(layout):
    """bytes -> the element as Rust's `{:?}` shows it, for the layout's element type."""
    if "fields" in layout:
        return _composite_decoder(layout)
    element = layout["element"].split(" or ")[0]
    size, encoding, _ = ELEMENT_TYPES[element]
    if encoding == "DW_ATE_boolean":

        def boolean(raw):
            if raw not in (b"\x00", b"\x01"):
                raise ValueError("Rust bool is neither 0 nor 1")
            return "true" if raw == b"\x01" else "false"

        return boolean
    if encoding == "DW_ATE_float":
        pack = "<f" if size == 4 else "<d"
        return lambda raw: rust_float_debug(struct.unpack(pack, raw)[0], size * 8)
    if encoding == "DW_ATE_UTF":

        def character(raw):
            if (shown := rust_char_debug(int.from_bytes(raw, "little"))) is None:
                raise ValueError("Rust char is not a Unicode scalar value")
            return shown

        return character
    signed = encoding == "DW_ATE_signed"
    return lambda raw: str(int.from_bytes(raw, "little", signed=signed))


def composite_fields(layout):
    """bytes -> [(field, type, value)] of a tuple or struct element, each field shown as
    Rust's `{:?}` shows its scalar."""
    parts = [
        (field, scalar, offset, ELEMENT_TYPES[scalar][0], _decoder({"element": scalar}))
        for field, offset, scalar in layout["fields"]
    ]
    return lambda raw: [
        (field, scalar, decode(raw[offset : offset + width]))
        for field, scalar, offset, width, decode in parts
    ]


def _composite_decoder(layout):
    """bytes -> a tuple or struct as Rust's derived `{:?}` shows it (whatever Debug the
    type itself implements)."""
    fields = composite_fields(layout)
    shape, shown = layout["shape"], layout["shown"]

    def composite(raw):
        values = [(field, value) for field, _, value in fields(raw)]
        if shape == "struct":
            return f"{shown} {{ " + ", ".join(f"{field}: {value}" for field, value in values) + " }"
        inner = ", ".join(value for _, value in values)
        if shape == "tuple":
            return f"({inner}{',' if len(values) == 1 else ''})"
        return f"{shown}({inner})"

    return composite


MAX_READ = 4096
# The text fields of a container's elements read per view (#260): each is a memory read of
# its own, out of the 128 one inspection has for every value it shows.
MAX_ELEMENT_TEXTS = 16
MAX_FIELD_TEXT = 1024


_NOT_UTF8 = (
    "the {}'s bytes here are not valid UTF-8, though Rust text always is, so Peeky shows "
    "none of its text"
)


def _partial(layout):
    """Whether a container's element has a field other than a scalar (#260)."""
    return any(kind not in ELEMENT_TYPES for _, _, kind, *_ in layout.get("fields", ()))


def rust_str_debug(text):
    """Text as Rust's `{:?}` prints a `str`: `"Zoë"`, `"a\\"b"`, `"\\u{200d}"`."""
    shown = []
    for char in text:
        point = ord(char)
        if char == '"':
            shown.append('\\"')
        elif char == "'":
            shown.append(char)
        elif point in _CHAR_ESCAPES:
            shown.append(_CHAR_ESCAPES[point])
        else:
            index = bisect.bisect_right(_ESCAPED_STARTS, point) - 1
            escaped = index >= 0 and point <= ESCAPED[index][1]
            shown.append(f"\\u{{{point:x}}}" if escaped else char)
    return '"' + "".join(shown) + '"'


def _withheld_field(field, kind, reason):
    return {
        "name": field,
        "type": kind,
        "value": None,
        "unavailable": True,
        "withheld": True,
        "withheld_as": "not read",
        "reason": reason,
    }


def _through_element(field, item, place=None):
    """Where to read an element's ``field`` that a container's view does not (#260): through
    a reference to the element, such as the `for` loop's item ``item`` in scope where there
    is one (eleventh evaluation: `e.leave` read where `team[1].leave` did not). Nothing
    proves the loop goes over this container, nor which element ``item`` is at a stop, so
    the note says so; ``place`` names the element (`team[1]`)."""
    if item is None:
        return "read it through a reference to the element, such as a `for` loop's item"
    return (
        f"read it through a reference to the element: if the `for` loop's item `{item}` is "
        f"`{place or 'this element'}` at a stop, read `{item}.{field}`; Peeky can't prove "
        "it is"
    )


def _text_field(field, raw, layout, read, left, item=None, place=None):
    """A `String` field of an element (its bytes ``raw``, at ``layout``'s offsets) read as
    text, and its text as `{:?}` shows it (None where not read whole); ``left``: how
    many more texts this view may read (MAX_ELEMENT_TEXTS); ``item``, ``place``: see
    _through_element."""
    data, length, capacity = (_word(raw, layout[key]) for key in ("data", "length", "capacity"))
    if not length <= capacity < 2**63:
        raise ValueError("Rust String length exceeds its capacity")
    if length and data == 0:
        raise ValueError("Rust String buffer is null")
    if length and left[0] <= 0:
        reason = (
            f"Peeky reads the text of only the first {MAX_ELEMENT_TEXTS} String fields of a "
            f"container's elements (each is a read of its own), and this is past them; it "
            f"is {length} bytes long. R{_through_element(field, item, place)[1:]}"
        )
        return _withheld_field(field, "String", reason), None
    count = min(length, MAX_FIELD_TEXT)
    left[0] -= 1 if length else 0
    payload = read(f"0x{data:X}", count) if count else b""
    try:
        # An incomplete character at a truncation point is not an error.
        text = codecs.getincrementaldecoder("utf-8")().decode(payload, final=count == length)
    except UnicodeDecodeError:
        # Withheld whole, never a prefix decoded before the bad bytes.
        reason = _NOT_UTF8.format("String") + ": check it with `peeky verify`"
        return _withheld_field(field, "String", reason), None
    child = {
        "name": field,
        "type": "String",
        "value": _shown_text(text, length),
        "length": length,
        "children": [
            {"name": "len", "type": "usize", "value": str(length)},
            {"name": "capacity", "type": "usize", "value": str(capacity)},
        ],
    }
    return child, rust_str_debug(text) if count == length else None


def _unread_field(field, kind, through=None, item=None, place=None):
    """A field of an element Peeky does not read (#260), with why and what to do; or of
    what an `Option` holds, read ``through`` a binding (`x` of `if let Some(x) = &last`).
    ``item``, ``place``: see _through_element."""
    from peeky.rust import rust_spelling

    shown = rust_spelling(kind) if kind else None
    what = f"has type `{shown}`" if shown else "has a type the debug information does not name"
    if through is not None:
        reason = (
            f"Peeky reads the integer, bool, float, char and String fields of what an "
            f"`Option` holds, and `{field}` {what}: read it through {through}"
        )
    else:
        reason = (
            f"Peeky reads an element's integer, bool, float, char and String fields, and "
            f"`{field}` {what}: {_through_element(field, item, place)}"
        )
    return _withheld_field(field, shown, reason)


def _partial_debug(shape, shown, parts):
    """An element as Rust's derived `{:?}` shows it, its fields not shown whole (None)
    left out: `Expense { cents: 4500, .. }`, `(_, 3)`."""
    if shape == "struct":
        listed = [f"{field}: {value}" for field, value in parts if value is not None]
        if len(listed) < len(parts):
            listed.append("..")
        return f"{shown} {{ " + ", ".join(listed) + " }"
    values = ["_" if value is None else value for _, value in parts]
    inner = ", ".join(values) + ("," if len(values) == 1 else "")
    return f"({inner})" if shape == "tuple" else f"{shown}({inner})"


def _partial_elements(layout, raws, read, through=None, item=None, holder=None):
    """(values, children) of elements with a `String` field or one Peeky does not read
    (#260): scalars as for any element, each String's text read from memory (the first
    MAX_ELEMENT_TEXTS), and every other field withheld with why (``through``, ``item``:
    see _unread_field; ``holder``: the container's name, which names each element)."""
    left = [MAX_ELEMENT_TEXTS]
    values, children = [], []
    for index, raw in enumerate(raws):
        members, parts = [], []
        place = f"{holder}[{index}]" if holder else None
        for name, offset, kind, *rest in layout["fields"]:
            if kind in ELEMENT_TYPES:
                width = ELEMENT_TYPES[kind][0]
                value = _decoder({"element": kind})(raw[offset : offset + width])
                members.append({"name": name, "type": kind, "value": value})
            elif kind == _STRING:
                text = layout["string"]
                member, value = _text_field(
                    name, raw[offset : offset + text["size"]], text, read, left, item, place
                )
                members.append(member)
            else:
                members.append(_unread_field(name, rest[0] if rest else None, through, item, place))
                value = None
            parts.append((name, value, kind))
        value = _partial_debug(layout["shape"], layout["shown"], [part[:2] for part in parts])
        if len(value) > MAX_SHOWN:
            # Long texts: the element's scalars, each text left to its own row.
            shorter = [(name, None if kind == _STRING else shown) for name, shown, kind in parts]
            value = _partial_debug(layout["shape"], layout["shown"], shorter)
        values.append(value)
        children.append(
            {"name": f"[{index}]", "type": layout["element"], "value": value, "children": members}
        )
    return values, children


_FLOATS = {32: ("<f", "<I", 23), 64: ("<d", "<Q", 52)}


def _float_bounds(value, bits):
    """(lower, upper, inclusive): the interval of reals that round to ``value``."""
    pack, word, mantissa = _FLOATS[bits]
    raw = struct.unpack(word, struct.pack(pack, value))[0]
    below = Fraction(struct.unpack(pack, struct.pack(word, raw - 1))[0])
    exact = Fraction(value)
    top = (1 << (bits - 1 - mantissa)) - 1
    if ((raw + 1) >> mantissa) & top == top:
        upper = exact + (exact - below) / 2  # the largest finite value
    else:
        upper = (exact + Fraction(struct.unpack(pack, struct.pack(word, raw + 1))[0])) / 2
    return (below + exact) / 2, upper, raw % 2 == 0


def _shortest_digits(value, bits):
    """(digits, exponent) with value ~ d.ddd * 10**exponent: the fewest digits that read
    back as ``value``, the closest of them, ties away from zero (Rust's shortest)."""
    lower, upper, inclusive = _float_bounds(value, bits)
    exact = Fraction(value)
    exponent = math.floor(math.log10(value))
    while Fraction(10) ** exponent > exact:
        exponent -= 1
    while Fraction(10) ** (exponent + 1) <= exact:
        exponent += 1
    for count in range(1, 40):
        scale = Fraction(10) ** (exponent - count + 1)
        floor = math.floor(exact / scale)
        options = [
            q
            for q in (floor, floor + 1)
            if q > 0 and (lower <= q * scale <= upper if inclusive else lower < q * scale < upper)
        ]
        if options:
            best = min(options, key=lambda q: (abs(q * scale - exact), -q))
            digits = str(best)
            return digits.rstrip("0") or "0", exponent + len(digits) - count
    raise ValueError("Rust float has no shortest representation")


def rust_float_debug(value, bits):
    """``value`` as Rust's `{:?}` prints an f32 (bits=32) or f64: shortest round-trip
    digits, in exponent form below 1e-4 or from 1e16 (checked against rustc 1.96)."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    sign = "-" if math.copysign(1.0, value) < 0 else ""
    value = abs(value)
    if value == 0:
        return sign + "0.0"
    digits, exponent = _shortest_digits(value, bits)
    pack = _FLOATS[bits][0]
    small, large = (struct.unpack(pack, struct.pack(pack, x))[0] for x in (1e-4, 1e16))
    if value < small or value >= large:
        rest = digits[1:]
        return f"{sign}{digits[0]}{'.' + rest if rest else ''}e{exponent}"
    if exponent >= 0:
        whole = digits[: exponent + 1].ljust(exponent + 1, "0")
        return f"{sign}{whole}.{digits[exponent + 1 :] or '0'}"
    return f"{sign}0.{'0' * (-exponent - 1)}{digits}"


# LLDB prints a `double`/`float` with APFloat's natural precision, 17 or 9 significant
# digits with trailing zeros trimmed, which always round-trips (LLDB 18, 20, Apple 2103).
_LLDB_FLOAT = re.compile(r"(-?)([0-9]{1,330})(?:\.([0-9]{1,400}))?(?:E([+-][0-9]{1,3}))?")
_LLDB_FLOAT_BITS = {"double": (64, 17), "float": (32, 9)}


def lldb_float_debug(text, kind):
    """LLDB's text for an f64 (`double`) or f32 (`float`) as Rust's `{:?}` shows it, or
    None when the text is not LLDB's exact round-trip rendering of a value of that type."""
    bits, precision = _LLDB_FLOAT_BITS.get(kind, (None, None))
    if bits is None or not isinstance(text, str):
        return None
    if text in ("NaN", "-NaN"):
        return "NaN"
    if text in ("+Inf", "-Inf"):
        return "inf" if text[0] == "+" else "-inf"
    match = _LLDB_FLOAT.fullmatch(text)
    if not match:
        return None
    sign, whole, fraction, power = match.groups()
    shown = (whole + (fraction or "")).lstrip("0")
    if not shown:
        return rust_float_debug(-0.0 if sign else 0.0, bits)
    exponent = len(whole) - 1 - (len(whole + (fraction or "")) - len(shown)) + int(power or 0)
    pack = _FLOATS[bits][0]
    try:
        value = struct.unpack(pack, struct.pack(pack, float(text)))[0]
    except (OverflowError, struct.error):
        return None
    if math.isinf(value):
        return None
    exact = decimal.Decimal(abs(value))
    # APFloat rounds a dropped half up; accept either tie rule, never a lossy rendering.
    for rounding in (decimal.ROUND_HALF_UP, decimal.ROUND_HALF_EVEN):
        rounded = decimal.Context(prec=precision, rounding=rounding).plus(exact)
        digits = "".join(map(str, rounded.as_tuple().digits)).rstrip("0")
        if (digits, rounded.adjusted()) == (shown.rstrip("0"), exponent):
            return rust_float_debug(value, bits)
    return None


# LLDB spells a generic's primitive arguments the C way (`Pair<unsigned short>`, and
# `Pair<char>` for Pair<i8>), and a 1-tuple `(i8)`: its names can collide, so they
# only confirm a type the compiler's own name (IR, MIR) selected, compared with every
# primitive blanked.
_PRIMITIVE = re.compile(
    r"\b(?:(?:unsigned|signed) )?(?:long long|long|short|int|char)\b"
    r"|\b(?:[iu](?:8|16|32|64|128|size)|f32|f64|bool|char(?:8|16|32)_t|float|double)\b"
)


def _lossy(name):
    return _PRIMITIVE.sub("#", str(name)).replace(",)", ")")


def _composite_parameter(parameter):
    """(LLDB's type, as a lossy name; container; read through a pointer) for a reference
    parameter of composites by its IR type: `&[T]` is read as itself, `&Vec<T>` through
    the pointer LLDB shows (`Vec<T> *`)."""
    if not isinstance(parameter, str):
        return None
    if match := re.fullmatch(r"&(mut )?(\[.+\])", parameter):
        name = f"&{match[1] or ''}{match[2]}"
        return (_lossy(name), name, False) if composite_container(name) else None
    if match := re.fullmatch(r"&(?:mut )?(alloc::vec::Vec<.+, alloc::alloc::Global>)", parameter):
        return (_lossy(f"{match[1]} *"), match[1], True) if composite_container(match[1]) else None
    return None


def _composite_owned(shown, declared, layouts):
    """The composite Vec layout of an owned local, by the MIR's declared type
    (`std::vec::Vec<Pair<i8>>`, crate-relative) and confirmed by LLDB's (lossy) name;
    None unless exactly one of the crate's layouts is that type."""
    exact = re.fullmatch(r"std::vec::Vec<(.+)>", str(declared or ""))
    if not exact or not composite_container(shown):
        return None
    # The MIR's 1-tuple `(i8,)` is `(i8)` in the DWARF.
    element = re.escape(exact[1].replace(",)", ")"))
    names = [
        name
        for name in layouts
        if (composite := composite_container(name))
        and composite[0] == "vec"
        and re.fullmatch(rf"(?:[A-Za-z_][A-Za-z0-9_]*::)?{element}", composite[1])
        # A tuple's name has no path; a struct's is its crate's.
        and (composite[1].startswith("(") == exact[1].startswith("("))
    ]
    if len(names) != 1 or _lossy(shown) != _lossy(names[0]):
        return None
    return names[0]


_RUST_PRIMITIVE = re.compile(r"(?<![\w:])(?:[iu](?:8|16|32|64|128|size)|f32|f64|bool|char)(?![\w])")


def _spelled(name, shown):
    """Whether LLDB's type name ``shown`` can be the type the DWARF names ``name`` (or a
    pointer to it, ``name *``): LLDB names a tuple as the DWARF does (`(char, char)`),
    and a generic's primitive arguments the C way (`Pair<char>` is Pair<i8> and
    `Pair<char32_t>` Pair<char>), each of which can be only some Rust primitives."""
    pointer = name.endswith(" *")
    base = name.removesuffix(" *")
    if base.startswith("("):
        return shown == name
    parts, at = [], 0
    for found in _RUST_PRIMITIVE.finditer(base):
        lldb = RUST_SCALARS.get(found[0])
        if lldb is None:
            return False
        parts += [re.escape(base[at : found.start()]), "(?:" + "|".join(map(re.escape, lldb)) + ")"]
        at = found.end()
    parts.append(re.escape(base[at:]))
    return bool(re.fullmatch("".join(parts) + (r" \*" if pointer else ""), str(shown)))


# An identifier named like a tuple's field in the DWARF (`__0`). Rust code never writes a
# tuple's field so (`t.0`): it is always one of the program's own names.
_NUMBERED_FIELD = re.compile(r"(?<![\w])__[0-9]+(?![\w])")


# A nightly feature gate: `#![feature(…)]`, `#![r#feature(…)]` or one inside `cfg_attr`.
# Rust's whitespace also has U+200E and U+200F, which `\s` lacks.
_FEATURE = re.compile(r"(?<![\w])feature[\s\u200e\u200f]*\(")


def without_numbered_structs(layouts, texts, others=()):
    """``layouts`` with every tuple struct unread where the crate could declare a struct
    with named fields `__0`, `__1`: the DWARF names a tuple struct's fields so too, and
    such a struct prints `Num { __0: 1 }`, not `Num(1)`. ``texts``: the crate's Rust
    sources' and tests'; ``others``: every other file the build may read (rustc reads no
    undeclared one: _require_declared_dependencies), Cargo.toml and Cargo.lock, which
    `include!("../Cargo.toml")` reads as Rust: searched as plain text, as they need not
    lex as Rust.

    On stable Rust no identifier is made without its token in the crate's source:
    `macro_rules!` cannot join identifiers (`concat_idents!` is gone, rustc 1.96), and a
    procedural macro needs a proc-macro crate, which a binary cannot define for itself
    and Peeky's builds cannot get (no dependencies, library target, build.rs, Cargo
    configuration or RUSTFLAGS of the crate's; the sysroot's crates need
    `rustc_private`). Peeky's MIR builds set RUSTC_BOOTSTRAP, so a crate may enable
    nightly features (`${concat(__, "0")}`). So an identifier `__0` (`fields!(Num, __0,
    __1)`), a feature gate, or a source not lexed leaves every tuple struct unread. A
    feature gate is only ever the crate root's, a declared Rust source: an `include!`d
    file may hold no inner attribute, and a `#[path]` module's `#![feature]` is ignored."""

    def refused(text):
        code = code_only(text)
        return code is None or _NUMBERED_FIELD.search(code) or _FEATURE.search(code)

    if not any(refused(text) for text in texts) and not any(
        _NUMBERED_FIELD.search(text) for text in others
    ):
        return layouts
    return {
        name: _UNREAD if layout.get("shape") == "tuple-struct" else layout
        for name, layout in layouts.items()
    }


def struct_name(declared, shown, layouts):
    """The tuple or struct an owned local is, by the MIR's declared type (`Span`,
    `Pair<i8>`, `(i64, char)`), confirmed by LLDB's (lossy) name; None unless exactly
    one of the crate's tuples and structs is that type.

    The MIR names a type by its crate-relative path (`Span`, `m::Point`), or from its
    nearest ancestor of a name no other item has (`shapes::Point` for
    `geo::shapes::Point` beside `other::Point`), and a raw identifier with `r#`."""
    if not isinstance(declared, str) or not declared:
        return None
    # The MIR's 1-tuple `(i8,)` is `(i8)` in the DWARF.
    plain = re.sub(r"(?<![\w#])r#", "", declared.replace(",)", ")"))
    segment = "[A-Za-z_][A-Za-z0-9_]*::"
    wanted = re.compile(rf"(?:{segment})*{re.escape(plain)}")
    names = [
        name
        for key in layouts
        if key.startswith(STRUCT_PREFIX)
        and (name := key.removeprefix(STRUCT_PREFIX)).endswith(plain)
        and wanted.fullmatch(name)
        # A tuple's name has no path; a struct's is its crate's.
        and name.startswith("(") == plain.startswith("(")
    ]
    if len(names) > 1:
        # A path that is also a suffix of others' is the crate-relative one.
        relative = re.compile(rf"(?:{segment})?{re.escape(plain)}")
        names = [name for name in names if relative.fullmatch(name)]
    if len(names) != 1 or not _spelled(names[0], shown):
        return None
    return names[0]


def struct_view(variable, read, layouts, *, parameter=None, owned=False, declared=None):
    """A tuple or struct of qualified scalars read from memory, as Rust's derived `{:?}`
    shows it (`Span { start: 2, end: 3 }`, `Point(1, 2)`, `(7,)`, `Marker`), with its
    fields as children (#106); None leaves it raw.

    Under the same proofs as containers: ``parameter`` is the declared (IR) type of a
    reference parameter the IR proves live (`&demo::Span`, read through the pointer
    LLDB shows), ``owned`` says the MIR proves this local initialized at the stop, and
    ``declared`` is its MIR type, which picks the layout (LLDB's names collide). A
    struct another of the same name shares its path with (one in a function's body),
    a std struct (its own Debug), packed or with other fields is never read.
    """
    name = thin = None
    if match := re.fullmatch(r"&(?:mut )?(.+)", str(parameter or "")):
        if _spelled(f"{match[1]} *", variable.get("type")):
            name, thin = match[1], True
    elif owned:
        name, thin = struct_name(declared, variable.get("type"), layouts), False
    layout = layouts.get(STRUCT_PREFIX + name) if name else None
    if layout is None or layout["kind"] not in ("composite", "unit"):
        return None
    view = {"name": variable.get("name"), "type": variable.get("type"), "variablesReference": 0}
    if layout["kind"] == "unit":
        return {**view, "value": layout["shown"], "rust_view": "memory"}
    reference = variable.get("memoryReference")
    if read is None or not isinstance(reference, str):
        return None
    try:
        shown_value = str(variable.get("value") or "")
        if thin:
            target = _word(read(reference, 8), 0)
            if not _ADDRESS.fullmatch(shown_value) or int(shown_value, 16) != target:
                raise ValueError("Rust reference does not match the debugger's pointer")
            if target == 0 or target % layout["alignment"]:
                raise ValueError("Rust reference is null or misaligned")
            reference = f"0x{target:X}"
        elif (at := shown_value.rpartition(" @ ")[2]) and _ADDRESS.fullmatch(at):
            if int(at, 16) != int(reference, 16):
                raise ValueError("Rust value is not at the debugger's address")
        raw = read(reference, layout["element_size"])
        shown = _composite_decoder(layout)(raw)
        if len(shown) > MAX_SHOWN:
            # Long field names: whole or not at all (the worker cuts values at 2000).
            return None
        return {
            **view,
            "value": shown,
            "rust_view": "memory",
            "children": [
                {"name": field, "type": scalar, "value": value}
                for field, scalar, value in composite_fields(layout)(raw)
            ],
            # The layout the fields were read with: the value's own storage (#105 may
            # change a field there, under its own rules), not a container's contents.
            "struct_layout": STRUCT_PREFIX + name,
        }
    except (ViewBudgetExhausted, ValueError):
        # Never worse than before: LLDB's own value and fields.
        return None


def unread_elements(name):
    """Why a `Vec` or slice of type ``name`` (its debug information's) is not read by
    element (#183, #260)."""
    from peeky.rust import rust_spelling

    return (
        f"Peeky does not read the elements of a {rust_spelling(name)}: it reads a Vec's or "
        "slice's elements where they are integers, bools, floats or chars, or tuples or "
        "structs your crate defines with such a field or a String one (not an enum or a "
        "packed struct)"
    )


def container_view(
    variable,
    read,
    layouts,
    *,
    optimized,
    parameter=None,
    owned=False,
    declared=None,
    field=None,
    loop_item=None,
):
    """A proven container read from memory, or None to leave it raw.

    ``parameter`` is the declared type of a parameter whose liveness the compiler's IR
    proves (rust_ir); ``owned`` says the compiler's MIR proves this owned local
    initialized at the stop (rust_mir); ``field`` is the qualified (DWARF) type of a
    field the caller proved held by such a value (#156), which picks its layout.
    Anything else stays raw: a moved, dropped or not-yet-initialized local can hold a
    pointer into freed memory.
    ``read(address, count)`` returns exactly ``count`` bytes or raises. The container
    is read at its compiler-validated offsets, then its first MAX_ELEMENTS elements (or
    MAX_TEXT_BYTES of text). A length above the capacity, or a null or misaligned
    buffer, is never shown as contents. ``loop_item(element type)``: the `for` loop's
    item in scope that a field the view does not read may be read through, or None.
    """
    reference_type = REFERENCE_PARAMETERS.get(parameter)
    composite = None if reference_type else _composite_parameter(parameter)
    name = None
    if field is not None:
        # LLDB's name only confirms the DWARF's (`Vec<long>` is `Vec<i64>`).
        if _lossy(variable.get("type")) != _lossy(field):
            return None
        layout, thin = layouts.get(field), False
    elif reference_type is not None and variable.get("type") in reference_type[0]:
        _, name, thin = reference_type
        layout = layouts.get(name)
    elif composite is not None and _lossy(variable.get("type")) == composite[0]:
        _, name, thin = composite
        layout = layouts.get(name)
    elif owned and (name := _composite_owned(variable.get("type"), declared, layouts)):
        layout, thin = layouts[name], False
    elif owned and variable.get("type") in OWNED_CONTAINERS:
        candidates = [c for c in OWNED_CONTAINERS[variable["type"]] if c in layouts]
        # The MIR's declared type names the one candidate (`std::vec::Vec<usize>`).
        exact = re.fullmatch(r"std::vec::Vec<(\w+)>", str(declared or ""))
        if exact and _vec_name(exact[1]) in candidates:
            candidates = [_vec_name(exact[1])]
        layout, thin = _one_layout(candidates, layouts), False
    else:
        return None
    if layout is None or (layout["kind"] == "unread" and field is not None):
        return None
    if layout["kind"] == "unread":
        # A proven container whose elements Peeky does not read: why, on its row (#260).
        return {"raw_reason": unread_elements(name)} if name else None
    view = {"name": variable.get("name"), "type": variable["type"], "variablesReference": 0}
    if layout["kind"] == "unqualified":
        return {**view, "value": UNQUALIFIED_CONTAINER, "rust_view": "opaque"}
    if optimized:
        return {**view, "value": UNVERIFIED_CONTAINER, "rust_view": "opaque"}
    reference = variable.get("memoryReference")
    if read is None or not isinstance(reference, str):
        return None
    try:
        shown_value = str(variable.get("value") or "")
        if thin:
            # memoryReference is the parameter's own slot; the debugger shows the
            # pointer it holds, and the two must agree.
            target = _word(read(reference, 8), 0)
            if not _ADDRESS.fullmatch(shown_value) or int(shown_value, 16) != target:
                raise ValueError("Rust reference does not match the debugger's pointer")
            if target == 0 or target % 8:
                raise ValueError("Rust reference is null or misaligned")
            reference = f"0x{target:X}"
        elif (at := shown_value.rpartition(" @ ")[2]) and _ADDRESS.fullmatch(at):
            if int(at, 16) != int(reference, 16):
                raise ValueError("Rust slice is not at the debugger's address")
        raw = read(reference, layout["size"])
        data, length = _word(raw, layout["data"]), _word(raw, layout["length"])
        width = layout["element_size"]
        capacity = _word(raw, layout["capacity"]) if "capacity" in layout else None
        if capacity is not None and not length <= capacity < 2**63:
            raise ValueError("Rust container length exceeds its capacity")
        if length * width >= 2**63:
            raise ValueError("Rust container is larger than isize::MAX bytes")
        if length and (data == 0 or data % layout.get("alignment", width)):
            raise ValueError("Rust container buffer is null or misaligned")
        sizes = [
            {
                "name": "len" if capacity is not None else "length",
                "type": "usize",
                "value": str(length),
            },
            *(
                [{"name": "capacity", "type": "usize", "value": str(capacity)}]
                if capacity is not None
                else []
            ),
        ]
        if layout["kind"] in ("string", "str"):
            count = min(length, MAX_TEXT_BYTES)
            payload = read(f"0x{data:X}", count) if count else b""
            try:
                # An incomplete character at a truncation point is not an error.
                text = codecs.getincrementaldecoder("utf-8")().decode(
                    payload, final=count == length
                )
            except UnicodeDecodeError:
                # Never a prefix of it: LLDB's summary, and why (#260).
                return {
                    "raw_reason": _NOT_UTF8.format(
                        "String" if layout["kind"] == "string" else "&str"
                    )
                }
            return {
                **view,
                "value": _shown_text(text, length),
                "rust_view": "memory",
                "length": length,
                "children": sizes,
            }
        # One read holds at most MAX_READ bytes: of wide elements, the first that fit.
        count = min(length, MAX_ELEMENTS, MAX_READ // width)
        payload = read(f"0x{data:X}", count * width) if count else b""
        raws = [payload[i * width : (i + 1) * width] for i in range(count)]
        if _partial(layout):
            item = loop_item(layout["element"]) if loop_item else None
            elements, children = _partial_elements(
                layout, raws, read, item=item, holder=variable.get("name")
            )
        else:
            decode = _decoder(layout)
            elements = [decode(raw) for raw in raws]
            children = [
                {"name": f"[{index}]", "type": layout["element"], "value": element}
                for index, element in enumerate(elements)
            ]
        if "fields" in layout and not _partial(layout):
            # A tuple's or struct's fields, named as LLDB names them (`__0`, `start`).
            fields = composite_fields(layout)
            for child, raw in zip(children, raws):
                child["children"] = [
                    {"name": field, "type": scalar, "value": value}
                    for field, scalar, value in fields(raw)
                ]
        return {
            **view,
            "value": _shown_list(elements, length),
            "rust_view": "memory",
            "length": length,
            "children": sizes + children,
        }
    except ViewBudgetExhausted as exc:
        return {**view, "value": _budget(exc), "rust_view": "budget-exhausted"}
    except ValueError:
        return {**view, "value": UNVERIFIED_CONTAINER, "rust_view": "opaque"}
