"""Which scalar field or element of a C, C++ or Rust local `--set` may change (#105).

Decided from the build's debug information, never from the type names a debugger
shows: a typedef can hide a pointer, and a bitfield shows as its declared type. The
path from the local to the value may only select fields of plain structs, classes and
tuples (`.field`, `.__0`) and elements of fixed-size arrays (`[2]`): nothing reached
through a pointer or a reference (what it points at may be gone, #101), and nothing
inside a union, an enum, a packed record or a standard-library type. The value must
be a 16-, 32- or 64-bit integer that is neither const nor volatile.
"""

import re

from peeky.native import _AUTOMATIC, _REFERENCE, _chain, _first, _qualified, _text

MAX_TYPES = 20_000
MAX_LOCALS = 4096
_QUALIFIERS = {
    "DW_TAG_const_type": "const",
    "DW_TAG_volatile_type": "volatile",
    "DW_TAG_restrict_type": "restrict",
    "DW_TAG_atomic_type": "atomic",
}
_RECORDS = {
    "DW_TAG_structure_type": "struct",
    "DW_TAG_class_type": "class",
    "DW_TAG_union_type": "union",
}
# The outermost namespace of a standard library's types: C++'s (a program may name its
# own namespace `core`), and Rust's crates.
CPP_LIBRARIES = frozenset({"std", "__gnu_cxx", "__gnu_debug", "__cxxabiv1"})
RUST_LIBRARIES = frozenset({"std", "core", "alloc"})
# Integer-encoded character types: a debugger shows their values as characters.
_CHARACTERS = frozenset({"wchar_t", "char8_t", "char16_t", "char32_t"})
_SELECTABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INDEX = re.compile(r"\[(0|[1-9][0-9]{0,9})\]")


class Unsettable(ValueError):
    """Why a field or element cannot be changed."""


def _target(value):
    found = _REFERENCE.fullmatch(value or "")
    return int(found[1], 16) if found else None


def _number(value):
    text = (value or "").strip()
    if re.fullmatch(r"0x[0-9a-fA-F]{1,16}", text):
        return int(text, 16)
    if re.fullmatch(r"[0-9]{1,19}", text):
        return int(text)
    return None


def local_types(dies, bodies):
    """{name: frozenset((type offset, storage, (file name, line)))} for the parameters
    and variables of a function's code (``bodies``: its concrete DIEs; nested blocks,
    but not the calls inlined into it). ``storage``: "automatic" (a variable in the
    frame's memory), "parameter" (a parameter there) or "other" (a static, a register,
    a location list, none); the file name (no directory) and line it is declared on, as
    LLDB names a local when several of its name are in scope (`s @ main.rs:81`), or
    None. None if there are too many to list them all."""
    found, pending, seen = {}, [child for body in bodies for child in dies[body]["children"]], 0
    while pending:
        child = pending.pop()
        die = dies[child]
        if die["tag"] == "DW_TAG_lexical_block":
            pending.extend(die["children"])
            continue
        if die["tag"] not in ("DW_TAG_formal_parameter", "DW_TAG_variable"):
            continue
        seen += 1
        if seen > MAX_LOCALS:
            return None
        chain = _chain(dies, child)
        name = _text(_first(dies, chain, "DW_AT_name"))
        if not name:
            continue
        location = die["attrs"].get("DW_AT_location") or ""
        framed = bool(_AUTOMATIC.search(location)) and not re.search(
            r"DW_OP_(?:addr|piece|reg[0-9]|regx|entry_value|stack_value)", location
        )
        storage = "other"
        if framed:
            storage = "parameter" if die["tag"] == "DW_TAG_formal_parameter" else "automatic"
        file = _text(_first(dies, chain, "DW_AT_decl_file"))
        line = _number(_first(dies, chain, "DW_AT_decl_line"))
        where = (file.replace("\\", "/").rpartition("/")[2], line) if file and line else None
        kind = _target(_first(dies, chain, "DW_AT_type"))
        found.setdefault(name, set()).add((kind, storage, where))
    return {name: frozenset(kinds) for name, kinds in found.items()}


def _library(dies, offset, libraries):
    """The outermost namespace holding a record, if one of ``libraries``."""
    outer, parent = None, dies[offset]["parent"]
    for _ in range(64):
        die = dies.get(parent)
        if die is None:
            break
        if die["tag"] == "DW_TAG_namespace":
            outer = _text(die["attrs"].get("DW_AT_name"))
        parent = die["parent"]
    return outer if outer in libraries else None


def _bound(dies, value):
    """A subrange's count or bound, or None if it is not a constant; and whether it is
    a variable-length array's, which names a DIE (an artificial variable,
    `__vla_expr0`, printed as an 8-digit reference) or is an expression."""
    number = _number(value)
    text = (value or "").strip()
    if number is not None and re.fullmatch(r"0x[0-9a-fA-F]{8}", text):
        target = dies.get(number)
        if target is not None and target["tag"] in (
            "DW_TAG_variable",
            "DW_TAG_formal_parameter",
            "DW_TAG_member",
            "DW_TAG_dwarf_procedure",
        ):
            return None, True
    return number, bool(text) and number is None


def _definitions(dies):
    """{(tag, qualified name): [offsets]} of every complete class, struct and union."""
    found = {}
    for offset, die in dies.items():
        if die["tag"] in _RECORDS and "DW_AT_declaration" not in die["attrs"]:
            if name := _qualified(dies, offset):
                found.setdefault((die["tag"], name), []).append(offset)
    return found


def type_graph(dies, offsets, libraries, limit=MAX_TYPES):
    """{offset: node} for the given types and every type they hold by value (through
    typedefs, qualifiers, arrays and record members, not pointers or references). A
    type the walk did not reach is absent, which ``settable_member`` refuses. A record
    in a namespace of ``libraries`` (`CPP_LIBRARIES`, `RUST_LIBRARIES`) is the
    library's, whose layout Peeky does not change. A class
    declared without its definition (clang's `-fno-standalone-debug`) stands for the
    one definition of its qualified name, if there is exactly one."""
    graph, pending, definitions = {}, [offset for offset in offsets if offset is not None], None
    while pending and len(graph) < limit:
        offset = pending.pop()
        if offset in graph:
            continue
        die = dies.get(offset)
        if die is None:
            graph[offset] = ("unknown", "")
            continue
        if die["tag"] in _RECORDS and "DW_AT_declaration" in die["attrs"]:
            if definitions is None:
                definitions = _definitions(dies)
            name = _qualified(dies, offset)
            complete = definitions.get((die["tag"], name), []) if name else []
            if len(complete) == 1:
                graph[offset] = ("typedef", "", complete[0])
                pending.append(complete[0])
                continue
        tag, attrs = die["tag"], die["attrs"]
        name = _text(attrs.get("DW_AT_name")) or ""
        target = _target(attrs.get("DW_AT_type"))
        size = _number(attrs.get("DW_AT_byte_size"))
        alignment = _number(attrs.get("DW_AT_alignment"))
        if tag == "DW_TAG_typedef":
            graph[offset] = ("typedef", name, target)
            pending.append(target)
        elif tag in _QUALIFIERS:
            graph[offset] = ("qualifier", _QUALIFIERS[tag], target)
            pending.append(target)
        elif tag == "DW_TAG_base_type":
            encoding = (attrs.get("DW_AT_encoding") or "").strip()
            graph[offset] = ("base", name, encoding, size)
        elif tag in ("DW_TAG_pointer_type", "DW_TAG_ptr_to_member_type"):
            graph[offset] = ("pointer", name)
        elif tag in ("DW_TAG_reference_type", "DW_TAG_rvalue_reference_type"):
            graph[offset] = ("reference", name)
        elif tag == "DW_TAG_enumeration_type":
            graph[offset] = ("enum", name, size)
        elif tag == "DW_TAG_array_type":
            counts, variable = [], False
            for child in die["children"]:
                bound = dies[child]
                if bound["tag"] != "DW_TAG_subrange_type":
                    continue
                count, counted = _bound(dies, bound["attrs"].get("DW_AT_count"))
                upper, bounded = _bound(dies, bound["attrs"].get("DW_AT_upper_bound"))
                lower, _ = _bound(dies, bound["attrs"].get("DW_AT_lower_bound") or "0")
                variable = variable or counted or bounded
                if count is None and upper is not None and lower == 0:
                    count = upper + 1
                counts.append(count if lower == 0 else None)
            known = bool(counts) and None not in counts
            graph[offset] = ("array", name, target, tuple(counts) if known else None, variable)
            pending.append(target)
        elif tag in _RECORDS:
            members, holds, variant = [], [], False
            for child in die["children"]:
                member = dies[child]
                if member["tag"] == "DW_TAG_variant_part":
                    variant = True
                    continue
                if member["tag"] == "DW_TAG_inheritance":
                    holds.append(_target(member["attrs"].get("DW_AT_type")))
                    continue
                if member["tag"] != "DW_TAG_member":
                    continue
                own = member["attrs"]
                if "DW_AT_declaration" in own or "DW_AT_external" in own:
                    continue  # a static data member: not part of the object
                kind = _target(own.get("DW_AT_type"))
                holds.append(kind)
                members.append(
                    (
                        _text(own.get("DW_AT_name")),
                        kind,
                        _number(own.get("DW_AT_data_member_location")),
                        "DW_AT_bit_size" in own or "DW_AT_bit_offset" in own,
                        _number(own.get("DW_AT_alignment")),
                    )
                )
            graph[offset] = (
                "record",
                name,
                _RECORDS[tag],
                size,
                alignment,
                tuple(members),
                tuple(holds),
                variant,
                "DW_AT_declaration" in attrs,
                _library(dies, offset, libraries),
            )
            pending.extend(kind for kind in holds if kind is not None)
        else:
            graph[offset] = ("other", tag)
    return graph


def _alignment(graph, offset, depth=0):
    """A type's alignment in bytes, or None if unknown."""
    node = graph.get(offset)
    if node is None or depth > 64:
        return None
    if node[0] in ("typedef", "qualifier"):
        return _alignment(graph, node[2], depth + 1)
    if node[0] == "base":
        size = node[3]
        if node[2] == "DW_ATE_complex_float" and size:
            size //= 2
        return size if size in (1, 2, 4, 8, 16) else None
    if node[0] in ("pointer", "reference"):
        return 8
    if node[0] == "enum":
        return node[2] if node[2] in (1, 2, 4, 8) else None
    if node[0] == "array":
        return _alignment(graph, node[2], depth + 1)
    if node[0] == "record":
        if node[4]:
            return node[4]
        found = [_alignment(graph, kind, depth + 1) for kind in node[6]]
        return None if None in found else max(found, default=1)
    return None


def _packed(graph, node):
    """True if a record's fields may sit misaligned (`__attribute__((packed))`,
    `#[repr(packed)]`), or its layout cannot be checked."""
    own = node[4]
    needed = []
    for _, kind, offset, bitfield, alignment in node[5]:
        if bitfield:
            continue
        natural = _alignment(graph, kind)
        if natural is None or offset is None:
            return True
        needed.append(natural)
        if offset % natural or (alignment is not None and alignment < natural):
            return True
    widest = max(needed, default=1)
    if own is not None and own < widest:
        return True
    return bool(node[3] is not None and node[3] % (own or widest))


def _strip(graph, offset, path):
    """The type under typedefs and qualifiers, and the typedef names on the way;
    refuses a const or volatile one."""
    names = []
    for _ in range(64):
        node = graph.get(offset)
        if node is None:
            raise Unsettable(f"Peeky did not read the type of `{path}` from the debug information")
        if node[0] == "typedef":
            if node[1]:
                names.append(node[1])
            offset = node[2]
            continue
        if node[0] == "qualifier":
            if node[1] == "const":
                raise Unsettable(f"`{path}` is const")
            if node[1] in ("volatile", "atomic"):
                raise Unsettable(
                    f"`{path}` is {node[1]}: the program may read it in ways a debugger "
                    "write does not reach as a plain store"
                )
            offset = node[2]
            continue
        return offset, node, names
    raise Unsettable(f"The type of `{path}` is nested too deeply")


def settable_member(graph, root, name, steps):
    """(signed, bits, names) for the integer that ``steps`` (the debugger's child names:
    `end`, `__0`, `[2]`) reach from the local ``name`` of type ``root``: whether it is
    signed, its width, and the type names (typedefs, then the base type) it has. Raises
    Unsettable with the reason when anything on the way is not a plain value."""
    offset, path = root, name
    pending = None  # the remaining dimensions of a multidimensional array
    for step in steps:
        if pending is not None:
            element, counts = pending
        else:
            offset, node, _ = _strip(graph, offset, path)
            if node[0] in ("pointer", "reference"):
                raise Unsettable(
                    f"`{path}` is a {node[0]}: Peeky changes only values the frame holds "
                    f"itself, never what a {node[0]} reaches (it may be freed or moved); "
                    "change it in the frame whose local holds the object, before the call: "
                    "stop on the caller's line that makes it (`session continue --to LINE`, "
                    "a probe's `--at`), not after `session step --out`, which returns "
                    "part-way through that line"
                )
            if node[0] == "record":
                _, record, kind, _, _, members, _, variant, incomplete, library = node
                shown = record or f"an unnamed {kind}"
                if library:
                    raise Unsettable(
                        f"`{path}` is a standard-library type ({shown}), whose layout "
                        "belongs to the library"
                    )
                if kind == "union":
                    raise Unsettable(
                        f"`{path}` is a union: a change to one member rewrites the others"
                    )
                if variant:
                    raise Unsettable(
                        f"`{path}` is an enum ({shown}): its layout (variants, niches) is "
                        "not written by Peeky"
                    )
                if incomplete:
                    raise Unsettable(
                        f"the debug information has no definition of {shown}, the type of `{path}`"
                    )
                if _packed(graph, node):
                    raise Unsettable(
                        f"`{path}` is a packed {kind} ({shown}): its fields may be misaligned"
                    )
                found = [member for member in members if member[0] == step]
                if len(found) != 1 or not _SELECTABLE.fullmatch(step):
                    raise Unsettable(
                        f"the debug information lists no field `{step}` in {shown}, the "
                        f"type of `{path}` (base classes and anonymous members are not "
                        "walked)"
                    )
                _, offset, _, bitfield, _ = found[0]
                path = f"{path}.{step}"
                if bitfield:
                    raise Unsettable(
                        f"`{path}` is a bitfield: it shares its storage with its neighbours"
                    )
                continue
            if node[0] != "array":
                raise Unsettable(f"`{path}` has no fields or elements Peeky can change")
            element, counts = node[2], node[3]
            if counts is None and node[4]:
                raise Unsettable(
                    f"`{path}` is a variable-length array, whose elements Peeky does not change"
                )
            if counts is None:
                raise Unsettable(f"`{path}` is an array whose size the debug information omits")
        index = _INDEX.fullmatch(step)
        if index is None:
            raise Unsettable(f"`{path}` is an array: select an element as `{path}[0]`")
        position = int(index[1])
        if position >= counts[0]:
            raise Unsettable(f"`{path}{step}` is out of range: `{path}` has {counts[0]} elements")
        path = f"{path}{step}"
        pending = (element, counts[1:]) if len(counts) > 1 else None
        offset = element
    if pending is not None:
        raise Unsettable(f"`{path}` is an array, not a single value")
    offset, node, names = _strip(graph, offset, path)
    if node[0] == "base":
        _, base, encoding, size = node
        if encoding in ("DW_ATE_signed_char", "DW_ATE_unsigned_char", "DW_ATE_UTF") or (
            base in _CHARACTERS
        ):
            raise Unsettable(f"`{path}` is a character ({base}); only integers can be changed")
        if encoding == "DW_ATE_boolean":
            raise Unsettable(f"`{path}` is a boolean ({base}); only integers can be changed")
        if encoding in ("DW_ATE_float", "DW_ATE_complex_float"):
            raise Unsettable(
                f"`{path}` is a floating-point value ({base}); only integers can be changed"
            )
        if encoding in ("DW_ATE_signed", "DW_ATE_unsigned") and size in (2, 4, 8):
            return encoding == "DW_ATE_signed", size * 8, (*names, base)
        raise Unsettable(
            f"`{path}` is {base or 'an unnamed type'} ({size or '?'} bytes); only 16-, 32- "
            "and 64-bit integers can be changed"
        )
    if node[0] == "enum":
        raise Unsettable(f"`{path}` is an enum; only integers can be changed")
    if node[0] in ("pointer", "reference"):
        raise Unsettable(f"`{path}` is a {node[0]}; only integers can be changed")
    raise Unsettable(f"`{path}` is not a single integer; select one of its fields or elements")


def integer_range(signed, bits):
    return (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if signed else (0, 2**bits - 1)
