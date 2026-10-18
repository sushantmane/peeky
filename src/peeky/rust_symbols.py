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

import codecs
import json
import math
import re
import struct
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from peeky.adapters import ViewBudgetExhausted
from peeky.native import MAX_METADATA, _metadata, parse_lines

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
BUDGET = "<Rust enum view budget exhausted; expand or inspect a narrower scope>"

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
    try:
        value = json.loads(match[1])
    except json.JSONDecodeError as exc:
        raise ValueError("Unsupported DWARF string escape") from exc
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
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
    if len(text.encode()) > MAX_METADATA:
        raise ValueError("Rust debug information exceeds limit")
    roots, stack, pending, last, seen, count = [], [], None, None, False, 0
    for raw in text.splitlines():
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
            continue
        if last is not None and last.attributes and _CONTINUATION.fullmatch(raw):
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
}


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


def _vec_parts(vec, element, by_offset):
    """(data pointer offset, length offset) of a Vec<T, Global> from its member chain."""
    name, size, encoding = element
    params = [
        child
        for child in vec.children
        if child.tag == "DW_TAG_template_type_parameter"
        and child.attributes.get("DW_AT_name") == '"T")'
    ]
    if len(params) != 1 or not _sized_base(_target(params[0], by_offset), name, size, encoding):
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


def container_layouts(text, unit):
    """Layouts of the qualified containers (`Vec<i32>`, `&[i32]`, `String`, `&str`) that
    one application unit defines, validated member by member; anything else refuses."""
    (root,) = parse_dies(text, units=False)
    if root.offset != unit.die or root.tag != "DW_TAG_compile_unit":
        raise ValueError("Rust debug information does not match its application unit")
    by_offset = {}
    _walk(root, by_offset, unit)
    layouts = {}

    def visit(die, prefix):
        for child in die.children:
            name = child.attributes.get("DW_AT_name")
            if child.tag == "DW_TAG_namespace" and name:
                visit(child, (*prefix, _string(name)))
            elif child.tag == "DW_TAG_structure_type" and name is not None:
                qualified = "::".join((*prefix, _string(name)))
                if qualified in QUALIFIED_CONTAINERS:
                    layout = _container_layout(child, qualified, by_offset)
                    if layouts.get(qualified, layout) != layout:
                        layout = {"kind": "unqualified"}
                    layouts[qualified] = layout

    visit(root, ())
    return layouts


def enum_layouts(text, unit):
    """Map qualified names of data-carrying enums in one application unit to layouts."""
    (root,) = parse_dies(text, units=False)
    if root.offset != unit.die or root.tag != "DW_TAG_compile_unit":
        raise ValueError("Rust debug information does not match its application unit")
    by_offset = {}
    _walk(root, by_offset, unit)
    layouts = {}

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
    """Return declared-source lines and enum layouts from application units only."""
    workspace = Path(workspace).resolve(strict=True)
    if type(budget) not in (int, float) or not 0 < budget <= SYMBOL_DEADLINE_S:
        raise ValueError("Rust symbol extraction has no remaining time budget")
    deadline = _clock() + budget

    def dump(arguments):
        remaining = deadline - _clock()
        if remaining <= 0:
            raise ValueError("Rust symbol extraction exceeded its shared deadline")
        return _metadata(dwarfdump, arguments, workspace, env, timeout=min(15, remaining))

    index = dump(["--debug-info", "--recurse-depth=0", str(symbols)])
    units = application_units(index, workspace, entrypoint=entrypoint, crate=crate)
    tables, layouts = [], {}
    for unit in units:
        table = dump([f"--debug-line=0x{unit.stmt_list:08x}", str(symbols)])
        offsets = re.findall(r"^debug_line\[0x([0-9a-f]{8,16})\]$", table, re.MULTILINE)
        if [int(offset, 16) for offset in offsets] != [unit.stmt_list]:
            raise ValueError("Rust line table does not match its application unit")
        tables.append(table)
        info = dump([f"--debug-info=0x{unit.die:08x}", "--show-children", str(symbols)])
        found = {**enum_layouts(info, unit), **container_layouts(info, unit)}
        for name, layout in found.items():
            if layouts.get(name, layout) != layout:
                layout = {"kind": "unqualified"}
            layouts[name] = layout
    lines = parse_lines("\n".join(tables), workspace, declared)
    return lines, layouts, units


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


def active_variant(variable, fetch, layouts):
    """Worker view for a compiled Rust enum value, or None for non-enum values.

    ``fetch(reference)`` must return bounded raw DAP children. Only the active
    variant's payload is read; inactive storage is never interpreted or exposed.
    """
    kind = variable.get("type")
    layout = layouts.get(kind) if isinstance(kind, str) else None
    if layout is None or layout["kind"] in CONTAINER_KINDS:
        return None
    view = {"name": variable.get("name"), "type": kind, "variablesReference": 0}
    if layout["kind"] != "option":
        return {**view, "value": UNQUALIFIED, "rust_view": "opaque"}
    try:
        value = _option_value(variable, fetch, layout, kind)
    except ViewBudgetExhausted:
        return {**view, "value": BUDGET, "rust_view": "budget-exhausted"}
    except ValueError:
        return {**view, "value": UNVERIFIED, "rust_view": "opaque"}
    return {**view, "value": value, "rust_view": "active-variant"}


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
    signed = encoding == "DW_ATE_signed"
    return lambda raw: str(int.from_bytes(raw, "little", signed=signed))


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


def container_view(
    variable, read, layouts, *, optimized, parameter=None, owned=False, declared=None
):
    """A proven container read from memory, or None to leave it raw.

    ``parameter`` is the declared type of a parameter whose liveness the compiler's IR
    proves (rust_ir); ``owned`` says the compiler's MIR proves this owned local
    initialized at the stop (rust_mir). Anything else stays raw: a moved, dropped or
    not-yet-initialized local can hold a pointer into freed memory.
    ``read(address, count)`` returns exactly ``count`` bytes or raises. The container
    is read at its compiler-validated offsets, then its first MAX_ELEMENTS elements (or
    MAX_TEXT_BYTES of text). A length above the capacity, or a null or misaligned
    buffer, is never shown as contents.
    """
    reference_type = REFERENCE_PARAMETERS.get(parameter)
    if reference_type is not None and variable.get("type") in reference_type[0]:
        _, name, thin = reference_type
        layout = layouts.get(name)
    elif owned and variable.get("type") in OWNED_CONTAINERS:
        candidates = [c for c in OWNED_CONTAINERS[variable["type"]] if c in layouts]
        # The MIR's declared type names the one candidate (`std::vec::Vec<usize>`).
        exact = re.fullmatch(r"std::vec::Vec<(\w+)>", str(declared or ""))
        if exact and _vec_name(exact[1]) in candidates:
            candidates = [_vec_name(exact[1])]
        layout, thin = _one_layout(candidates, layouts), False
    else:
        return None
    if layout is None:
        return None
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
        if length and (data == 0 or data % width):
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
            # An incomplete character at a truncation point is not an error.
            text = codecs.getincrementaldecoder("utf-8")().decode(payload, final=count == length)
            return {
                **view,
                "value": _shown_text(text, length),
                "rust_view": "memory",
                "length": length,
                "children": sizes,
            }
        count = min(length, MAX_ELEMENTS)
        payload = read(f"0x{data:X}", count * width) if count else b""
        decode = _decoder(layout)
        elements = [decode(payload[i * width : (i + 1) * width]) for i in range(count)]
        children = [
            {"name": f"[{index}]", "type": layout["element"], "value": element}
            for index, element in enumerate(elements)
        ]
        return {
            **view,
            "value": _shown_list(elements, length),
            "rust_view": "memory",
            "length": length,
            "children": sizes + children,
        }
    except ViewBudgetExhausted:
        return {**view, "value": BUDGET, "rust_view": "budget-exhausted"}
    except (ValueError, UnicodeDecodeError):
        return {**view, "value": UNVERIFIED_CONTAINER, "rust_view": "opaque"}
