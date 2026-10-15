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

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

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
    dwarfdump, symbols, workspace, env, *, entrypoint, crate, declared, deadline=None
):
    """Return declared-source lines and enum layouts from application units only."""
    workspace = Path(workspace).resolve(strict=True)
    deadline = (
        min(deadline, _clock() + SYMBOL_DEADLINE_S)
        if deadline is not None
        else _clock() + SYMBOL_DEADLINE_S
    )

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
        for name, layout in enum_layouts(info, unit).items():
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
    if layout is None:
        return None
    view = {"name": variable.get("name"), "type": kind, "variablesReference": 0}
    if layout["kind"] != "option":
        return {**view, "value": UNQUALIFIED, "rust_view": "opaque"}
    try:
        value = _option_value(variable, fetch, layout, kind)
    except ValueError:
        return {**view, "value": UNVERIFIED, "rust_view": "opaque"}
    return {**view, "value": value, "rust_view": "active-variant"}
