"""A libstdc++ unordered container's entries, read from its own nodes (#360).

LLDB's libstdc++ formatter takes an unordered container's element type from its
allocator's first template argument. GCC's debug information gives `std::allocator<T>`
none, so the formatter counts each one as empty and lists nothing (#167). Peeky then
reads the entries itself, where the build's debug information proves how:

- the hashtable's `_Value` is the type each node holds. Its `_Traits` say whether a node
  also caches its hash code, which picks the node's type: `_Hash_node<_Value, cache>`;
- that node type is in the debug information, with its `_Hash_node_base` (one 8-byte
  pointer, `_M_nxt`) at offset 0, and its value's storage (`_M_storage`, an
  `__aligned_buffer` of `_Value`) after it, aligned for `_Value`, inside the node;
- the entries hold only integers, bools and `std::string`s, in pairs and structs.

The nodes form one singly-linked chain from `_M_before_begin._M_nxt`, in iteration
order. The chain must end (a null `_M_nxt`) after exactly `_M_element_count` nodes, at
most ``MAX_NODES``. Memory is read with the debugger's `readMemory` (lldb-dap 20), or
without it as 8-byte words through variable paths that the debugger reads as `frame
variable` does: `us._M_h._M_before_begin._M_nxt[1]._M_nxt` is the word 8 bytes into the
first node. Nothing is evaluated in the program. Without `readMemory`, nothing outside
the nodes is read, so a string's text must be in its own buffer, inside the node.

The entries come back as a listing shaped as the debugger lists a container it reads
right (`[0]`, `[1]`, ...; a pair's `first` and `second`; a struct's members), which
``native_previews`` shows as it shows any other: ``read_entries``.
"""

import re
import struct

from peeky.cxx_strings import SUMMARY_UNITS, render

# The most nodes read from one container.
MAX_NODES = 256
# How many bytes one `readMemory` request reads, so that nodes close together cost one.
WINDOW = 4096
# Struct members read in one entry, and how deep structs nest in one.
MEMBERS = 64
DEPTH = 4

_TABLE = re.compile(r"std::_Hashtable<")
_TRAITS = re.compile(r"std::__detail::_Hashtable_traits<")
NODE = re.compile(r"std::__detail::_Hash_node<")
_LINK = re.compile(r"std::__detail::_Hash_node_base$")
_HOLDER = re.compile(r"std::__detail::_Hash_node_value_base<")
_BUFFER = re.compile(r"__gnu_cxx::__aligned_buffer<")
_STRING = re.compile(r"std::__cxx11::basic_string<char,")
_PAIR = re.compile(r"std::pair<")
# A pointer as the debugger shows a variable path's value: lldb-dap 20's `0x…`, or
# lldb-dap 18's `(T *) _M_nxt = 0x…`; `nullptr` for zero.
_WORD = re.compile(r"(?:.*\s=\s)?(0x[0-9a-fA-F]{1,16}|nullptr)\s*", re.S)
# Integer types the debugger shows as characters.
_CHARACTERS = frozenset({"wchar_t", "char8_t", "char16_t", "char32_t"})


class Unread(ValueError):
    """The entries are not read from the nodes: the debug information does not prove
    how, they hold a type Peeky does not read there, or the chain is not whole."""


def _under(graph, offset):
    """The node under typedefs and qualifiers, or None."""
    for _ in range(64):
        node = graph.get(offset)
        if node is None or node[0] not in ("typedef", "qualifier"):
            return node
        offset = node[2]
    return None


def _record(graph, offset, pattern=None):
    """A defined struct or class (under typedefs and qualifiers) whose qualified name
    ``pattern`` matches, as fields.type_graph records it; else None."""
    node = _under(graph, offset)
    if node is None or node[0] != "record" or node[2] == "union" or node[7] or node[8]:
        return None
    if len(node) < 14 or (pattern is not None and not pattern.match(node[11] or "")):
        return None
    return node


def _member(node, name):
    found = [member for member in node[5] if member[0] == name]
    return found[0] if len(found) == 1 else None


def _parameter(node, sort, name):
    found = [parameter for parameter in node[12] if parameter[:2] == (sort, name)]
    return found[0] if len(found) == 1 else None


def _same(graph, one, two):
    """Whether two type offsets are one type: the same, or alike in every unit
    (``Typed.identity``: a base type by name, encoding and size, a record by layout)."""
    from peeky.native_previews import Typed

    if one is None or two is None:
        return False
    first, second = Typed(graph, one), Typed(graph, two)
    if first.resolved() is not None and first.resolved() == second.resolved():
        return True
    identity = first.identity()
    return isinstance(identity, str) and identity == second.identity()


def _size(graph, offset):
    from peeky.native_previews import Typed

    return Typed(graph, offset).byte_size()


def _link(graph, offset):
    """Whether a type is `_Hash_node_base` as Peeky reads it: 8 bytes, its one member
    `_M_nxt` an 8-byte pointer at offset 0."""
    node = _record(graph, offset, _LINK)
    if node is None or node[3] != 8 or node[10]:
        return False
    link = _member(node, "_M_nxt")
    if link is None or link[2] != 0 or link[3]:
        return False
    pointer = _under(graph, link[1])
    return pointer is not None and pointer[0] == "pointer" and pointer[3] in (8, None)


def _placed(graph, node, value, size, alignment):
    """Where a node type ``node`` holds its value: (its offset in the node, the node's
    size), or None. The node holds exactly one `_Hash_node_base`, at offset 0, and
    exactly one `_Hash_node_value_base` of ``value``, whose `_M_storage` is an
    `__aligned_buffer` of it, at least ``size`` bytes, past the link, aligned."""
    links, holders = [], []

    def walk(record, at, depth):
        if depth > 8 or len(record) < 14 or len(record[10]) != len(record[13]):
            return False
        for base, offset in zip(record[10], record[13]):
            inner = _record(graph, base)
            if inner is None or type(offset) is not int:
                # A virtual base, or one the walk did not read.
                return False
            if _LINK.match(inner[11] or ""):
                links.append((base, at + offset))
            elif _HOLDER.match(inner[11] or ""):
                holders.append((inner, at + offset))
            if not walk(inner, at + offset, depth + 1):
                return False
        return True

    if not walk(node, 0, 0) or len(links) != 1 or len(holders) != 1:
        return None
    if links[0][1] != 0 or not _link(graph, links[0][0]):
        return None
    holder, at = holders[0]
    held = _parameter(holder, "type", "_Value")
    storage = _member(holder, "_M_storage")
    if held is None or not _same(graph, held[2], value) or storage is None:
        return None
    buffer = _record(graph, storage[1], _BUFFER)
    if buffer is None or type(storage[2]) is not int or storage[3]:
        return None
    kind = _parameter(buffer, "type", "_Tp")
    if kind is None or not _same(graph, kind[2], value):
        return None
    if type(buffer[3]) is not int or buffer[3] < size:
        return None
    at += storage[2]
    extent = node[3]
    if type(extent) is not int or at < 8 or at % alignment or at + size > extent:
        return None
    return at, extent


def layout(graph, container):
    """(the type offset of the entries, their byte offset in a node, their size, their
    alignment) for an unordered container's record ``container`` (fields.type_graph's),
    proven from the build's debug information; else Unread. The graph must hold the node
    types too (``node_types``): those of the hashtable's `_Value` and cache, one per
    compile unit, must all place it alike."""
    from peeky.fields import _alignment

    table = _member(container, "_M_h")
    table = _record(graph, table[1], _TABLE) if table is not None else None
    if table is None:
        raise Unread("no hashtable")
    value = _parameter(table, "type", "_Value")
    traits = _parameter(table, "type", "_Traits")
    traits = _record(graph, traits[2], _TRAITS) if traits is not None else None
    cache = _parameter(traits, "value", "_Cache_hash_code") if traits is not None else None
    if value is None or value[2] is None or cache is None or cache[3] not in (0, 1):
        raise Unread("no value type or traits")
    before = _member(table, "_M_before_begin")
    if before is None or not _link(graph, before[1]):
        raise Unread("no chain")
    size, alignment = _size(graph, value[2]), _alignment(graph, value[2])
    if not size or not alignment:
        raise Unread("no value size")
    placed = set()
    for node in graph.values():
        if node[0] != "record" or len(node) < 14 or not NODE.match(node[11] or ""):
            continue
        held = _parameter(node, "type", "_Value")
        cached = _parameter(node, "value", "_Cache_hash_code")
        if held is None or cached is None or cached[3] != cache[3] or node[8]:
            continue
        if _same(graph, held[2], value[2]):
            placed.add(_placed(graph, node, value[2], size, alignment))
    if len(placed) != 1 or None in placed:
        raise Unread("no node type")
    at, _ = placed.pop()
    return value[2], at, size, alignment


def node_types(dies):
    """The offsets of every `std::__detail::_Hash_node` the debug information defines
    (dies: native._dies), at most 4,096: the types a container's nodes are, which no
    local's type reaches but through a pointer."""
    from peeky.native import _qualified, _text

    found = []
    for offset, die in dies.items():
        if die["tag"] not in ("DW_TAG_structure_type", "DW_TAG_class_type"):
            continue
        if "DW_AT_declaration" in die["attrs"]:
            continue
        name = _text(die["attrs"].get("DW_AT_name")) or ""
        if name.startswith("_Hash_node<") and NODE.match(_qualified(dies, offset) or ""):
            found.append(offset)
            if len(found) >= 4096:
                break
    return found


def word(text):
    """The number a pointer's text holds (`0x0000aaaaaaab12f0`, `nullptr`), or Unread."""
    found = _WORD.fullmatch(str(text or ""))
    if found is None:
        raise Unread(f"not a pointer: {str(text)[:40]}")
    return 0 if found[1] == "nullptr" else int(found[1], 16)


class Memory:
    """A container's nodes as the debugger reads them with `readMemory`: ``first`` is
    the address of the first node (or 0); ``read(reference, count)`` reads memory."""

    def __init__(self, first, read):
        self.first, self.read, self.windows = first, read, []

    def bytes(self, address, count):
        for start, data in self.windows:
            if start <= address and address + count <= start + len(data):
                return data[address - start : address - start + count]
        try:
            data = self.read(f"0x{address:x}", max(count, WINDOW))
        except ValueError:
            # Past the end of what the program maps: just what is asked for.
            data = self.read(f"0x{address:x}", count)
        if len(data) < count:
            raise Unread("short read")
        self.windows.append((address, data))
        return data[:count]

    def node(self, index, address, count):
        return self.bytes(address, count)

    at = bytes


class Words:
    """A container's nodes as 8-byte words through variable paths, for a debugger with
    no `readMemory` (lldb-dap 18): node N is `base` then N times `->_M_nxt`, and its
    word K is that path's `[K]._M_nxt` (``evaluate(path)``: the debugger's text for a
    path, read as `frame variable` reads it). Only memory inside the nodes is read."""

    def __init__(self, base, evaluate):
        self.paths, self.evaluate, self.nodes = [base], evaluate, []
        self.first = word(evaluate(base))

    def node(self, index, address, count):
        path = self.paths[index]
        self.paths.append(f"{path}->_M_nxt")
        words = [word(self.evaluate(self.paths[-1]))]
        words += [word(self.evaluate(f"{path}[{k}]._M_nxt")) for k in range(1, -(-count // 8))]
        data = struct.pack(f"<{len(words)}Q", *words)
        self.nodes.append((address, data))
        return data[:count]

    def at(self, address, count):
        for start, data in self.nodes:
            if start <= address and address + count <= start + len(data):
                return data[address - start : address - start + count]
        raise Unread("outside the nodes, and this debugger cannot read memory")


def _type_name(graph, offset):
    """A type as the debugger names it: its qualifiers, then a typedef's name, a base
    type's (as LLDB spells an integer: `unsigned long`), or a class's qualified name."""
    from peeky.fields import _spelled

    words = []
    for _ in range(64):
        node = graph.get(offset)
        if node is None:
            return None
        if node[0] == "qualifier":
            if node[1] in ("const", "volatile"):
                words.append(node[1])
            offset = node[2]
            continue
        if node[0] == "typedef" and not node[1]:
            offset = node[2]
            continue
        if node[0] == "typedef":
            name = node[1]
        elif node[0] == "base":
            name = _spelled(node[1])
        elif node[0] == "record" and len(node) > 11:
            name = node[11] or node[1]
        else:
            return None
        return " ".join([*words, name]) if name else None
    return None


def _empty(graph, offset, depth=0):
    """Whether a base class holds nothing: no members, and only such bases."""
    node = _record(graph, offset)
    return (
        node is not None
        and depth < 8
        and not node[5]
        and all(_empty(graph, base, depth + 1) for base in node[10])
    )


class _Reader:
    """Decodes a node's value into the listing the debugger would give."""

    def __init__(self, graph, memory):
        self.graph, self.memory, self.listed = graph, memory, {}

    def add(self, children):
        reference = len(self.listed) + 1
        self.listed[reference] = children
        return reference

    def fetch(self, reference, count):
        return list(self.listed.get(reference, ()))[:count]

    def value(self, offset, data, address, depth=0):
        """{"type", "value", "variablesReference", "view": "memory"} of the value of type
        ``offset`` held in ``data`` at ``address``; a struct's or pair's members are its
        children, and its value is `T @ 0x…`, as lldb-dap shows a struct."""
        graph = self.graph
        node, name = _under(graph, offset), _type_name(graph, offset)
        if node is None or not name:
            raise Unread("a type the walk did not read")
        shown = {"type": name, "variablesReference": 0, "view": "memory", "offset": offset}
        if node[0] == "base":
            return {**shown, "value": _scalar(node, data)}
        if node[0] != "record" or node[2] == "union" or node[7] or node[8] or depth > DEPTH:
            raise Unread(f"`{name}`")
        qualified = node[11] if len(node) > 11 else ""
        if type(node[3]) is not int or node[3] != len(data):
            raise Unread(f"`{name}`: no size")
        if node[9] and _STRING.match(qualified or ""):
            return {**shown, "value": self.string(node, data, address), "text": True}
        if node[9] and not (
            _PAIR.match(qualified or "") and all(_empty(graph, b) for b in node[10])
        ):
            raise Unread(f"`{name}`")
        if not node[9] and node[10]:
            raise Unread(f"`{name}`: a base class")
        if len(node[5]) > MEMBERS:
            raise Unread(f"`{name}`: too many members")
        children = []
        for member in node[5]:
            label, kind, at, bitfield = member[:4]
            size = _size(graph, kind)
            if not label or bitfield or type(at) is not int or not size or at + size > len(data):
                raise Unread(f"`{name}`: a member Peeky does not place")
            part = self.value(kind, data[at : at + size], address + at, depth + 1)
            children.append({"name": label, **part})
        if node[9] and [child["name"] for child in children] != ["first", "second"]:
            raise Unread(f"`{name}`")
        reference = self.add(children)
        return {**shown, "value": f"{name} @ 0x{address:x}", "variablesReference": reference}

    def string(self, node, data, address):
        """A libstdc++ string's text, as Peeky shows one read from memory (#128): its
        data pointer and length, then up to SUMMARY_UNITS bytes, from its own buffer
        right after those words, or from where the pointer points."""
        from peeky.native import local_buffer

        buffer = local_buffer(self.graph, node)
        plus = _member(node, "_M_dataplus")
        hider = _record(self.graph, plus[1]) if plus is not None else None
        pointer = _member(hider, "_M_p") if hider is not None else None
        aimed = _under(self.graph, pointer[1]) if pointer is not None else None
        if buffer is None or pointer is None or pointer[2] != 0 or aimed is None:
            raise Unread("a string Peeky does not place")
        character = _under(self.graph, aimed[2]) if aimed[0] == "pointer" else None
        if (
            character is None
            or character[0] != "base"
            or character[2] not in ("DW_ATE_signed_char", "DW_ATE_unsigned_char")
            or character[3] != 1
        ):
            raise Unread("a string Peeky does not place")
        text, length = struct.unpack_from("<QQ", data)
        if text == address + 16:
            if length >= buffer:
                raise Unread("a string longer than its own buffer")
            body = data[16 : 16 + length]
        else:
            if not text or length > 1 << 40:
                raise Unread("a string with no text")
            count = min(length, SUMMARY_UNITS)
            body = self.memory.at(text, count) if count else b""
        return render(body, 1, "", length > SUMMARY_UNITS)


def _scalar(node, data):
    """An integer's or a bool's text, as the debugger shows it; else Unread."""
    _, name, encoding, size = node
    if size != len(data):
        raise Unread(f"`{name}`: no size")
    if encoding == "DW_ATE_boolean" and size == 1 and data[0] in (0, 1):
        return "true" if data[0] else "false"
    if encoding in ("DW_ATE_signed", "DW_ATE_unsigned") and size in (2, 4, 8):
        if name not in _CHARACTERS:
            return str(int.from_bytes(data, "little", signed=encoding == "DW_ATE_signed"))
    raise Unread(f"`{name}`")


def readable(graph, offset, depth=0):
    """Whether ``read_entries`` decodes a value of type ``offset`` at all, judged by its
    type alone, as ``_Reader.value`` judges it: an integer or a bool, a libstdc++ string
    Peeky places, or a pair or a struct of them. A string's text may still be too long
    to read without `readMemory`. For a row's advice to read a container once nothing
    may be changing it (eval 13 prep, finding 3)."""
    node, name = _under(graph, offset), _type_name(graph, offset)
    if node is None or not name:
        return False
    if node[0] == "base":
        _, base, encoding, size = node
        if encoding == "DW_ATE_boolean":
            return size == 1
        return (
            encoding in ("DW_ATE_signed", "DW_ATE_unsigned")
            and size in (2, 4, 8)
            and (base not in _CHARACTERS)
        )
    if node[0] != "record" or node[2] == "union" or node[7] or node[8] or depth > DEPTH:
        return False
    qualified = node[11] if len(node) > 11 else ""
    if type(node[3]) is not int:
        return False
    if node[9] and _STRING.match(qualified or ""):
        from peeky.native import local_buffer

        plus = _member(node, "_M_dataplus")
        hider = _record(graph, plus[1]) if plus is not None else None
        pointer = _member(hider, "_M_p") if hider is not None else None
        aimed = _under(graph, pointer[1]) if pointer is not None else None
        character = _under(graph, aimed[2]) if aimed is not None and aimed[0] == "pointer" else None
        return (
            local_buffer(graph, node) is not None
            and pointer[2] == 0
            and character is not None
            and character[0] == "base"
            and character[2] in ("DW_ATE_signed_char", "DW_ATE_unsigned_char")
            and character[3] == 1
        )
    if node[9] and not (_PAIR.match(qualified or "") and all(_empty(graph, b) for b in node[10])):
        return False
    if (not node[9] and node[10]) or len(node[5]) > MEMBERS:
        return False
    for member in node[5]:
        label, kind, at, bitfield = member[:4]
        size = _size(graph, kind)
        if not label or bitfield or type(at) is not int or not size or at + size > node[3]:
            return False
        if not readable(graph, kind, depth + 1):
            return False
    return not node[9] or [member[0] for member in node[5]] == ["first", "second"]


def read_entries(graph, container, count, memory):
    """(the container as the debugger lists one it reads right: {"value": "size=N",
    "variablesReference": ...}, ``fetch(reference, count)`` for its listing) read from
    the nodes of the unordered container ``container`` (a fields.type_graph record),
    which counts ``count`` entries itself (its `_M_element_count`); ``memory``: a
    ``Memory`` or ``Words``. Unread where any of it is not proven."""
    value, at, size, alignment = layout(graph, container)
    if not 0 <= count <= MAX_NODES:
        raise Unread("too many entries")
    reader, entries = _Reader(graph, memory), []
    address = memory.first
    while address:
        if len(entries) == count:
            raise Unread("a longer chain than its count")
        if address % 8 or (address + at) % alignment:
            raise Unread("a misaligned node")
        data = memory.node(len(entries), address, at + size)
        following = struct.unpack_from("<Q", data)[0]
        part = reader.value(value, data[at : at + size], address + at)
        entries.append({"name": f"[{len(entries)}]", **part})
        address = following
    if len(entries) != count:
        raise Unread("a shorter chain than its count")
    listed = {"value": f"size={count}", "variablesReference": reader.add(entries), "view": "memory"}
    return listed, reader.fetch


def view_children(graph, root, fetch):
    """The entries of ``read_entries``'s listing as a value view's children: {"name",
    "type", "value"}, "text" for a string, and a pair's or struct's members as its
    "children". A struct's value is its preview (`{x = 1, y = 2}`), as a listing shows
    it; a pair's stays `std::pair<…> @ 0x…`, as lldb-dap shows one."""
    from peeky.native_previews import Typed, preview

    def element(variable):
        shown = {key: variable[key] for key in ("name", "type", "value")}
        if variable.get("text"):
            shown["text"] = True
        reference = variable.get("variablesReference")
        if reference:
            shown["children"] = [element(part) for part in fetch(reference, MEMBERS)]
            typed = Typed(graph, variable["offset"])
            previewed = preview(variable, fetch, typed, lambda child: child.get("value"))
            if previewed is not None:
                shown["value"] = previewed
        return shown

    return [element(entry) for entry in fetch(root["variablesReference"], MAX_NODES)]
