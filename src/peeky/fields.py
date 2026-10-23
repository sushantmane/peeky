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

from peeky.identifiers import is_name
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
# The C++ standard containers Peeky previews by their elements (#181), by qualified name
# (libc++'s `std::__1::vector<…>`, libstdc++'s `std::vector<…>`, `std::__debug::map<…>`).
CXX_CONTAINER = re.compile(
    r"std::(?:__\w+::)*(vector|array|deque|list|map|unordered_map|set|multiset|"
    r"unordered_set|unordered_multiset)<"
)
_PARAMETERS = {
    "DW_TAG_template_type_parameter": "type",
    "DW_TAG_template_value_parameter": "value",
}
# Integer-encoded character types: a debugger shows their values as characters.
_CHARACTERS = frozenset({"wchar_t", "char8_t", "char16_t", "char32_t"})
_INDEX = re.compile(r"\[(0|[1-9][0-9]{0,9})\]")
# A location that is one offset in the frame, nothing more but constants added to it
# (a by-value structured binding's `DW_OP_breg31 WSP+16, DW_OP_plus_uconst 0x8`): a
# local's own storage.
_PLAIN_FRAME = re.compile(
    r"\(?\s*DW_OP_(?:fbreg|breg[0-9]+)\b[^,()]*(?:,\s*DW_OP_plus_uconst\s+[^,()]+)*\)?\s*"
)
# A location read through a pointer.
_DEREF = re.compile(r"\bDW_OP_(?:GNU_)?x?deref")


class Unsettable(ValueError):
    """Why a field or element cannot be changed."""


CALL_ADVICE = (
    "change the value where it is owned, before the call that passed it: `--at <call line> "
    "--hits N --set-hit N`, where N counts that line's own stops, not the called function's "
    "(find N with `--capture <argument> --hits all` there)"
)
# What else can change a pointer's target, where nothing proves a frame holds it.
_ELSEWHERE = (
    "if what it points at is also a root local in some frame, change it there, where its "
    "row is [settable]; otherwise edit the source with `--old`/`--new`"
)


def pointer_advice(root, kind):
    """settable_member's advice for the pointer or reference root local ``root``, by
    what it is in the frame (tenth evaluation: kitty's `e.payer`): "parameter", proven,
    gets the call advice; "local" (a `for` loop's item, `let r = &mut x;`), which no
    call passed, and None (not proven either: a static, a shadowed or unclear
    declaration) get no call claim; "element", a local proven to point at a slice's or
    Vec's element, which nothing changes, is told to edit the source."""
    if kind == "parameter":
        return CALL_ADVICE
    if kind == "element":
        return (
            f"`{root}` is a local here, not a parameter, so no call passed it: it points at "
            "an element of a slice or Vec, which Peeky never changes, so edit the source "
            "with `--old`/`--new`"
        )
    if kind == "local":
        return f"`{root}` is a local here, not a parameter, so no call passed it: {_ELSEWHERE}"
    return f"`{root}` isn't proven a parameter here, so Peeky names no call: {_ELSEWHERE}"


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


def referred_locals(dies, bodies):
    """The names of the parameters and variables of a function's code (as
    ``local_types`` walks them) whose storage the frame reaches through a pointer:
    any location other than one frame offset (`DW_OP_fbreg -24`), such as clang's
    `DW_OP_fbreg -24, DW_OP_deref` for a structured binding of a reference
    (`auto& [items, n] = *owner`), a class passed by value through a hidden pointer, or
    a named return value. Their values may be what a caller's library call is changing
    (#181). None if there are too many to list them all."""
    found, pending, seen = set(), [child for body in bodies for child in dies[body]["children"]], 0
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
        name = _text(_first(dies, _chain(dies, child), "DW_AT_name"))
        location = die["attrs"].get("DW_AT_location") or ""
        if name and not _PLAIN_FRAME.fullmatch(location):
            found.add(name)
    return frozenset(found)


def local_types(dies, bodies, through=False):
    """{name: frozenset((type offset, storage, (file name, line)))} for the parameters
    and variables of a function's code (``bodies``: its concrete DIEs; nested blocks,
    but not the calls inlined into it). ``storage``: "automatic" (a variable in the
    frame's memory), "parameter" (a parameter there), "register" (a register only, no
    memory of its own), "static" (a fixed address) or "other" (a location list, none);
    with ``through`` (C, C++), "through" for a variable the frame reaches through a
    pointer (`DW_OP_fbreg -16, DW_OP_deref`: a C++ structured binding of a reference, a
    named return value, a C `__block` variable), which a change would write through;
    the file name (no directory) and line it is declared on, as
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
        if (
            framed
            and through
            and _DEREF.search(location)
            and die["tag"] != "DW_TAG_formal_parameter"
        ):
            # A parameter read through a pointer is one the ABI passes by a hidden
            # pointer to the call's own copy (a struct over 16 bytes on arm64, a class
            # with a destructor): the parameter itself, as any other.
            storage = "through"
        elif framed:
            storage = "parameter" if die["tag"] == "DW_TAG_formal_parameter" else "automatic"
        elif re.fullmatch(r"\(?\s*DW_OP_(?:reg[0-9]+|regx)\b[^,]*\)?\s*", location):
            storage = "register"
        elif re.fullmatch(r"\(?\s*DW_OP_addr\b[^,]*\)?\s*", location):
            storage = "static"
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


def type_graph(dies, offsets, libraries, limit=MAX_TYPES, pointees=False):
    """{offset: node} for the given types and every type they hold by value (through
    typedefs, qualifiers, arrays and record members, not pointers or references, unless
    ``pointees``: what they point at too, for display only). A
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
            # What it points at, never walked here: previews read one level (#136).
            aimed = target if tag == "DW_TAG_pointer_type" else None
            graph[offset] = ("pointer", name, aimed)
            if pointees and aimed is not None:
                pending.append(aimed)
        elif tag in ("DW_TAG_reference_type", "DW_TAG_rvalue_reference_type"):
            graph[offset] = ("reference", name, target)
            if pointees and target is not None:
                pending.append(target)
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
            members, holds, variant, bases, parameters = [], [], False, [], []
            for child in die["children"]:
                member = dies[child]
                if member["tag"] == "DW_TAG_variant_part":
                    variant = True
                    continue
                if member["tag"] in _PARAMETERS:
                    # A class template's arguments: a standard container's element type
                    # (#181), the `_Tp` of `std::vector<_Tp, _Allocator>`.
                    own = member["attrs"]
                    parameters.append(
                        (
                            _PARAMETERS[member["tag"]],
                            _text(own.get("DW_AT_name")),
                            _target(own.get("DW_AT_type")),
                            _number(own.get("DW_AT_const_value")),
                        )
                    )
                    continue
                if member["tag"] == "DW_TAG_inheritance":
                    holds.append(_target(member["attrs"].get("DW_AT_type")))
                    bases.append(holds[-1])
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
                        # A bitfield's width, for its preview (#136).
                        _number(own.get("DW_AT_bit_size")),
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
                # Its base classes, in order (#136).
                tuple(bases),
                # Its qualified name (`alloc::vec::Vec<i64, alloc::alloc::Global>`), which
                # picks a Rust container's layout (#156).
                _qualified(dies, offset) or name,
                # Its template arguments: ("type", name, type offset, None) or ("value",
                # name, type offset, constant) (#181).
                tuple(parameters),
            )
            pending.extend(kind for kind in holds if kind is not None)
            if graph[offset][9] and CXX_CONTAINER.match(graph[offset][11]):
                # A standard container's elements (and a map's keys and values), which
                # its members reach only through pointers: their previews' types (#181).
                pending.extend(p[2] for p in parameters[:2] if p[0] == "type" and p[2])
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
    for _, kind, offset, bitfield, alignment, *_ in node[5]:
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


def debugger_steps(graph, root, name, steps):
    """A selector's ``steps`` from the local ``name`` of type ``root`` as the debugger
    names the children they reach, which is how the stop's walk reads them (#138): an
    index with leading zeros is that element (`[01]` is `[1]`), and a negative one
    counts from the end of a fixed-size array (`[-1]` of an `int[3]` is `[2]`). None
    where that cannot be told here (a negative index into an array whose length the
    debug information does not fix, or past its start). From a step that is neither a
    field of a record nor an index of an array on, the steps are kept as they are, for
    settable_member to judge."""
    found, offset, path, pending = [], root, name, None
    for position, step in enumerate(steps):
        index = re.fullmatch(r"\[(-?[0-9]+)\]", step)
        if pending is not None:
            element, counts = pending
        else:
            try:
                offset, node, _ = _strip(graph, offset, path)
            except Unsettable:
                return [*found, *steps[position:]]
            if node[0] == "record" and index is None:
                members = [member for member in node[5] if member[0] == step]
                if len(members) != 1:
                    return [*found, *steps[position:]]
                found.append(step)
                offset, path = members[0][1], f"{path}.{step}"
                continue
            if node[0] != "array" or index is None:
                return [*found, *steps[position:]]
            element, counts = node[2], node[3]
        if index is None:
            return [*found, *steps[position:]]
        number = int(index[1])
        if number < 0:
            if not counts or number + counts[0] < 0:
                return None
            number += counts[0]
        found.append(f"[{number}]")
        path += found[-1]
        pending = (element, counts[1:]) if counts and len(counts) > 1 else None
        offset = element
    return found


def member_indexes(graph, root, name, steps):
    """The projection ``steps`` (the debugger's child names, as ``debugger_steps`` gives
    them) make from the local ``name`` of type ``root``, as MIR numbers it (#192 review):
    ("field", its position among its record's members, which rustc's debug information
    lists in declaration order, as MIR numbers fields) or ("index", n) of a fixed-size
    array. None where a step is neither."""
    found, offset, path = [], root, name
    for step in steps:
        offset, node, _ = _strip(graph, offset, path)
        index = re.fullmatch(r"\[([0-9]+)\]", step)
        if node[0] == "record" and index is None:
            positions = [at for at, member in enumerate(node[5]) if member[0] == step]
            if len(positions) != 1:
                return None
            found.append(("field", positions[0]))
            offset, path = node[5][positions[0]][1], f"{path}.{step}"
        elif node[0] == "array" and index is not None and len(node[3] or ()) <= 1:
            found.append(("index", int(index[1])))
            offset, path = node[2], f"{path}{step}"
        else:
            return None
    return tuple(found)


def held_container(graph, root, name, steps, dereference=False):
    """The qualified type name of the record (a Rust `Vec`, `String`, `&str`) that
    ``steps`` (the debugger's child names: `values`, `__1`, `[2]`) reach from the local
    ``name`` of type ``root`` (#156), held in that local's own storage or, with
    ``dereference``, in what the local (a reference or a Box) points at. On the way:
    fields of plain structs and tuples and elements of fixed-size arrays only; never a
    pointer, a reference, a union, an enum (which variant it holds is not read), a
    packed record or a standard-library type (a `RefCell`'s, a `Mutex`'s contents change
    behind a shared reference). Raises Unsettable with the reason."""
    offset, node, _ = _strip(graph, root, name)
    if dereference:
        if node[0] == "record" and re.fullmatch(r"&(?:mut )?\[.*\]", str(node[1] or "")):
            # A slice reference is a data pointer and a length, not a pointer (#260).
            raise Unsettable(
                f"`{name}` is a slice, a data pointer and a length: Peeky reads what it holds "
                f"only as its elements (`{name}[0]`), not through `{name}.data_ptr`"
            )
        if node[0] not in ("pointer", "reference") or node[2] is None:
            raise Unsettable(f"the debug information gives `{name}` no type it points at")
        offset = node[2]
    elif node[0] in ("pointer", "reference"):
        raise Unsettable(f"`{name}` is a {node[0]} Peeky does not read through here")
    path, pending = name, None
    for step in steps:
        if pending is not None:
            element, counts = pending
        else:
            offset, node, _ = _strip(graph, offset, path)
            if node[0] in ("pointer", "reference"):
                raise Unsettable(f"`{path}` is a {node[0]}, which Peeky does not read through here")
            if node[0] == "record":
                _, record, kind, _, _, members, _, variant, incomplete, library = node[:10]
                shown = record or f"an unnamed {kind}"
                if library:
                    raise Unsettable(
                        f"`{path}` is a standard-library type ({shown}), whose contents Peeky "
                        "does not read into"
                    )
                if kind == "union":
                    raise Unsettable(f"`{path}` is a union")
                if variant:
                    raise Unsettable(
                        f"`{path}` is an enum ({shown}): which variant it holds is not read here"
                    )
                if incomplete:
                    raise Unsettable(
                        f"the debug information has no definition of {shown}, the type of `{path}`"
                    )
                if _packed(graph, node):
                    raise Unsettable(f"`{path}` is a packed {kind} ({shown})")
                found = [member for member in members if member[0] == step]
                if len(found) != 1 or not is_name(step):
                    raise Unsettable(
                        f"the debug information lists no field `{step}` in {shown}, the "
                        f"type of `{path}`"
                    )
                offset, path = found[0][1], f"{path}.{step}"
                continue
            if node[0] != "array" or node[3] is None:
                raise Unsettable(f"`{path}` has no fields or elements Peeky reads into")
            element, counts = node[2], node[3]
        index = _INDEX.fullmatch(step)
        if index is None or int(index[1]) >= counts[0]:
            raise Unsettable(f"`{path}` has no element {step}")
        path = f"{path}{step}"
        pending = (element, counts[1:]) if len(counts) > 1 else None
        offset = element
    if pending is not None:
        raise Unsettable(f"`{path}` is an array")
    offset, node, _ = _strip(graph, offset, path)
    if node[0] != "record" or len(node) < 12 or node[2] == "union" or node[7] or node[8]:
        raise Unsettable(f"`{path}` is not a container Peeky reads")
    return node[11]


def settable_member(graph, root, name, steps, owner=None):
    """(signed, bits, names) for the integer that ``steps`` (the debugger's child names:
    `end`, `__0`, `[2]`) reach from the local ``name`` of type ``root``: whether it is
    signed, its width, and the type names (typedefs, then the base type) it has. Raises
    Unsettable with the reason when anything on the way is not a plain value.
    ``owner()``, where the local is itself a pointer: where to change the value
    instead (``pointer_advice``, or the one call that passed it), else None: then no
    call is claimed."""
    offset, path = root, name
    pending = None  # the remaining dimensions of a multidimensional array
    for step in steps:
        if pending is not None:
            element, counts = pending
        else:
            offset, node, _ = _strip(graph, offset, path)
            if node[0] in ("pointer", "reference"):
                advice = owner() if owner is not None and path == name else None
                raise Unsettable(
                    f"`{path}` is a {node[0]}: Peeky does not write through a {node[0]}, "
                    "since what it reaches may be freed or moved; "
                    # The call advice only where the caller proved a parameter.
                    + (advice or (pointer_advice(path, None) if path == name else _ELSEWHERE))
                )
            if node[0] == "record":
                _, record, kind, _, _, members, _, variant, incomplete, library = node[:10]
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
                if len(found) != 1 or not is_name(step):
                    raise Unsettable(
                        f"the debug information lists no field `{step}` in {shown}, the "
                        f"type of `{path}` (base classes and anonymous members are not "
                        "walked)"
                    )
                _, offset, _, bitfield, _ = found[0][:5]
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


def settable_parts(graph, root, name, limit=64, visits=4096):
    """The paths (`board.entries`, `bins[*].hi`, `board.cells[*].n`, `xs[*]`; `[*]`: any
    element) of the integer fields and elements of the struct, class, tuple or array
    local ``name`` of type ``root`` that ``settable_member`` allows, found by a walk
    through records and fixed-size arrays at any depth (#192 review), each leaf judged
    by ``settable_member`` as `set` judges it; at most ``limit``, then `… and N more`
    (`… and more` past ``visits`` types walked); else None. What `describe` lists
    (seventh evaluation, finding 16; #183: the paths, not the local). A Rust reference
    or slice (`&str`, a record the debug information names `&…` or `*…`), a union, an
    enum and a library's type are never walked into: `set` changes nothing there."""

    def stripped(offset, path):
        try:
            return _strip(graph, offset, path)[1]
        except Unsettable:
            return None

    node = stripped(root, name)
    if node is None or node[0] not in ("record", "array"):
        return None
    if node[0] == "record" and str(node[1]).startswith(("&", "*")):
        return None
    found, extra, walked = [], 0, 0
    # (type offset, steps from the local, the record offsets on the way: no cycle).
    pending = [(root, [], frozenset())]
    while pending:
        offset, steps, seen = pending.pop()
        walked += 1
        if walked > visits:
            return (*found, "… and more")
        shown = name + "".join("[*]" if step == "[0]" else f".{step}" for step in steps)
        node = stripped(offset, shown)
        if node is None:
            continue
        if node[0] == "record":
            if (
                offset in seen
                or node[2] == "union"
                or node[7]
                or node[8]
                or node[9]
                or str(node[1]).startswith(("&", "*"))
            ):
                continue
            members = [member for member in node[5] if member[0]]
            pending.extend(
                (member[1], [*steps, member[0]], seen | {offset}) for member in reversed(members)
            )
        elif node[0] == "array":
            if node[3]:
                pending.append((node[2], [*steps, *["[0]"] * len(node[3])], seen))
        elif node[0] == "base" and steps:
            try:
                settable_member(graph, root, name, steps)
            except Unsettable:
                continue
            if shown in found:
                continue
            if len(found) < limit:
                found.append(shown)
            else:
                extra += 1
    if not found:
        return None
    return (*found, f"… and {extra} more") if extra else tuple(found)


def member_base(graph, root, steps):
    """The DWARF name of the base type (`usize`, `i64`) that the debugger's child names
    ``steps`` (`k`, `[2]`, `__0`) reach from a local of type ``root``, through what a
    pointer or reference points at as the debugger shows it (`self.k`), or None. For
    display only, from a graph built with ``pointees``."""
    offset = root
    for step in [*steps, None]:
        node = None
        for _ in range(64):
            node = graph.get(offset)
            if node is None:
                return None
            if node[0] in ("typedef", "qualifier"):
                offset = node[2]
                continue
            if node[0] in ("pointer", "reference") and step is not None and node[2] is not None:
                # LLDB lists what a pointer or reference points at as its children.
                offset = node[2]
                continue
            break
        if step is None:
            return node[1] if node[0] == "base" and node[1] else None
        if node[0] == "record" and not node[7]:
            found = [member for member in node[5] if member[0] == step]
            if len(found) != 1:
                return None
            offset = found[0][1]
        elif node[0] == "array" and _INDEX.fullmatch(step) and node[3] and len(node[3]) == 1:
            offset = node[2]
        else:
            return None
    return None


def member_type(graph, root, steps):
    """The node of the type that the debugger's child names ``steps`` reach from a local
    of type ``root``, as ``member_base`` walks them (typedefs and qualifiers kept, what a
    pointer or reference points at when a step goes below it), or None. For display
    only (#183: a field's Rust name), from a graph built with ``pointees``."""
    offset = root
    for step in steps:
        node = None
        for _ in range(64):
            node = graph.get(offset)
            if node is None:
                return None
            if node[0] in ("typedef", "qualifier") or (
                node[0] in ("pointer", "reference") and node[2] is not None
            ):
                offset = node[2]
                continue
            break
        if node[0] == "record" and not node[7]:
            found = [member for member in node[5] if member[0] == step]
            if len(found) != 1:
                return None
            offset = found[0][1]
        elif node[0] == "array" and _INDEX.fullmatch(step) and node[3] and len(node[3]) == 1:
            offset = node[2]
        else:
            return None
    return graph.get(offset)


# Rust's names for its pointer types: a raw pointer's, and a reference's or a Box's.
_RUST_RAW = re.compile(r"\*(?:const|mut) ")
_RUST_LENT = re.compile(r"&|alloc::boxed::Box<")


def _unqualified(graph, offset):
    """(offset, node) of the type under typedefs and qualifiers; node None if unknown."""
    for _ in range(64):
        node = graph.get(offset)
        if node is None or node[0] not in ("typedef", "qualifier"):
            return offset, node
        offset = node[2]
    return offset, None


def _rust_raw(node, holder):
    """True for a Rust raw pointer type's node, False for a reference's or a Box's, None
    if unknown. The address inside a fat pointer (`data_ptr` of a `&str` or `*const
    [u8]`, `pointer` of a `&dyn Trait`) has no name of its own: its ``holder``'s says."""
    name = node[1] or (holder[1] if holder is not None else "")
    if _RUST_RAW.match(name):
        return True
    return False if _RUST_LENT.match(name) else None


def rust_raw_pointer(graph, root, steps):
    """#255: for a pointer the debugger's child names ``steps`` reach from a Rust local
    of type ``root``: True if it is a raw pointer (`*const T`, `*mut T`, or the address in
    a fat one, `*const str`) or is read through one, False if it is a reference or a Box
    (`&T`, `Box<T>`, or the address in a fat one, `&str`), None where the debug
    information does not say. The steps are walked as LLDB lists them: a pointer to a
    struct or array lists what it points at (`r.x`); one to anything else has one
    child, `*p`. From a graph built with ``pointees``."""
    offset, holder, through = root, None, False
    for step in steps:
        offset, node = _unqualified(graph, offset)
        if node is not None and node[0] in ("pointer", "reference"):
            raw = _rust_raw(node, holder)
            if raw is None or node[2] is None:
                return None
            through, holder = through or raw, None
            if step.startswith("*"):
                offset = node[2]
                continue
            offset, node = _unqualified(graph, node[2])
        if node is None:
            return None
        if node[0] == "record" and not node[7]:
            found = [member for member in node[5] if member[0] == step]
            if len(found) != 1:
                return None
            offset, holder = found[0][1], node
        elif node[0] == "array" and _INDEX.fullmatch(step) and node[3] and len(node[3]) == 1:
            offset, holder = node[2], None
        else:
            return None
    _, node = _unqualified(graph, offset)
    if node is None or node[0] not in ("pointer", "reference"):
        return None
    raw = _rust_raw(node, holder)
    return True if through or raw else raw


# Where a reason names its function (`in \x00, ...`).
FUNCTION = "\x00"


def by_function(reasons):
    """A refusal's reasons, [(function, reason with FUNCTION where it is named)], each
    given once for the functions it holds in (`in fc and fd, `a.pp` is a pointer: ...`),
    in order (#138: a line several functions share)."""
    grouped = {}
    for function, reason in reasons:
        named = grouped.setdefault(reason, [])
        if function not in named:
            named.append(function)
    shown = []
    for reason, names in grouped.items():
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        if len(names) > 1 and reason.startswith(FUNCTION + " has "):
            # `fe and fg have no local `a``.
            reason = FUNCTION + " have " + reason[len(FUNCTION) + len(" has ") :]
        shown.append(reason.replace(FUNCTION, joined))
    return "; ".join(shown)


def integer_range(signed, bits):
    return (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if signed else (0, 2**bits - 1)
