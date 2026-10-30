"""A libstdc++ unordered container's entries, read from its own nodes (#360, #374).

LLDB's libstdc++ formatter takes an unordered container's element type from its
allocator's first template argument. GCC's debug information gives `std::allocator<T>`
none, so the formatter counts each one as empty and lists nothing (#167). Peeky then
reads the entries itself, where the build's debug information proves how:

- the hashtable's `_Value` is the type each node holds. Its `_Traits` say whether a node
  also caches its hash code, which picks the node's type: `_Hash_node<_Value, cache>`;
- that node type is in the debug information, with its `_Hash_node_base` (one 8-byte
  pointer, `_M_nxt`) at offset 0, and its value's storage (`_M_storage`, an
  `__aligned_buffer` of `_Value`) after it, aligned for `_Value`, inside the node;
- the entries hold integers, bools, characters, `float`s and `double`s, enums, pointers
  (their addresses, never followed) and `std::string`s (their text only from the node),
  in pairs and structs. A
  `std::array` inside one is read from the node; any other standard container in one is
  withheld on its own, with why (``_Reader.nested``): a vector's buffer or a nested
  table's nodes are heap blocks, which nothing proves still allocated (a value destroyed
  in place, before a new one is built there, keeps its old pointers).

The nodes form one singly-linked chain from `_M_before_begin._M_nxt`, in iteration
order. The chain must end (a null `_M_nxt`) after exactly `_M_element_count` nodes, at
most ``MAX_CHAIN`` with `readMemory`, ``MAX_WORDS`` without it. The first ``MAX_NODES``
entries are read with the listing; the rest
when a page, a position or a key asks for them (``Paged``), from the nodes the walk
placed. Memory is read with the debugger's `readMemory` (lldb-dap 20), or without it as
8-byte words through variable paths that the debugger reads as `frame variable` does:
`us._M_h._M_before_begin._M_nxt[1]._M_nxt` is the word 8 bytes into the first node.
Nothing is evaluated in the program, and nothing outside the nodes is read: a string's
text is read only from its own buffer, inside the node. One whose text is in heap memory,
which nothing proves still allocated, is withheld on its own.

The entries come back as a listing shaped as the debugger lists a container it reads
right (`[0]`, `[1]`, ...; a pair's `first` and `second`; a struct's members), which
``native_previews`` shows as it shows any other: ``read_entries``. Each value is shown
as LLDB shows one it reads itself (``float_text``, ``character_text``).
"""

import collections.abc
import re
import struct
import unicodedata

from peeky.cxx_strings import render

# The entries read with a container's listing, and the most elements or entries read
# from a container held in a node.
MAX_NODES = 256
# The most nodes walked to prove a container's count, by their addresses (`readMemory`);
# past the first MAX_NODES, only each one's link is read until an entry is asked for.
MAX_CHAIN = 16384
# The most walked through variable paths (``Words``, lldb-dap 18): node N's path holds N
# steps, which the debugger walks for each word, so a walk costs O(N²) in path text and
# steps (about 1 MB of paths at 512).
MAX_WORDS = 512
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
# The standard containers, adaptors and views a node may hold: read on their own, or
# withheld on their own (``_Reader.nested``).
_CONTAINER = re.compile(
    r"(std::(?:__\w+::)*(?:vector|array|deque|list|forward_list|(?:unordered_)?(?:multi)?"
    r"(?:map|set)|stack|queue|priority_queue|span|valarray))<"
)
_VECTOR = re.compile(r"std::vector<")
_ARRAY = re.compile(r"std::array<")
_UNORDERED = re.compile(r"std::unordered_(?:multi)?(?:map|set)<")
# A pointer as the debugger shows a variable path's value: lldb-dap 20's `0x…`, or
# lldb-dap 18's `(T *) _M_nxt = 0x…`; `nullptr` for zero.
_WORD = re.compile(r"(?:.*\s=\s)?(0x[0-9a-fA-F]{1,16}|nullptr)\s*", re.S)
# Integer types the debugger shows as characters.
_CHARACTERS = frozenset({"wchar_t", "char8_t", "char16_t", "char32_t"})
# A wide character's literal prefix, by its type's name.
_WIDE = {"wchar_t": "L", "char16_t": "u", "char32_t": "U"}
# The escapes LLDB writes for a character (`'\n'`, its `\e` for ESC).
_ESCAPES = {
    0: "\\0",
    7: "\\a",
    8: "\\b",
    9: "\\t",
    10: "\\n",
    11: "\\v",
    12: "\\f",
    13: "\\r",
    27: "\\e",
}
# LLDB's `target.max-zero-padding-in-float-format`: past it, a number is in scientific
# notation (`1.0E+7`).
_PADDING = 6


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
    the address of the first node (or 0); ``read(reference, count)`` reads memory, a
    ``WINDOW``-aligned window at a time, so that nodes close together cost one read."""

    def __init__(self, first, read):
        self.first, self.read = first, read
        # {window start: its bytes}; and bytes read alone where a window isn't mapped whole.
        self.pages, self.windows = {}, []

    def _window(self, start):
        data = self.pages.get(start)
        if data is None:
            data = self.read(f"0x{start:x}", WINDOW)
            if len(data) != WINDOW:
                raise Unread("short read")
            self.pages[start] = data
        return data

    def bytes(self, address, count):
        """``count`` bytes from ``address`` (at most ``WINDOW`` of them)."""
        for start, data in self.windows:
            if start <= address and address + count <= start + len(data):
                return data[address - start : address - start + count]
        try:
            first = address - address % WINDOW
            data = b"".join(self._window(start) for start in range(first, address + count, WINDOW))
            return data[address - first : address - first + count]
        except ValueError:
            pass
        # Part of a window the program doesn't map: just what is asked for.
        data = self.read(f"0x{address:x}", count)
        if len(data) < count:
            raise Unread("short read")
        self.windows.append((address, data))
        return data[:count]

    def node(self, index, address, count):
        return self.bytes(address, count)

    def link(self, index, address):
        """The address node ``index`` (at ``address``) links to: its `_M_nxt`."""
        return struct.unpack("<Q", self.bytes(address, 8))[0]


class Words:
    """A container's nodes as 8-byte words through variable paths, for a debugger with
    no `readMemory` (lldb-dap 18): node N is `base` then N times `->_M_nxt`, and its
    word K is that path's `[K]._M_nxt` (``evaluate(path)``: the debugger's text for a
    path, read as `frame variable` reads it). Only memory inside the nodes is read."""

    def __init__(self, base, evaluate):
        self.paths, self.evaluate = [base], evaluate
        self.first = word(evaluate(base))

    def link(self, index, address):
        """Node ``index``'s first word, its `_M_nxt`: the path to the next node."""
        path = self.paths[index]
        following = word(self.evaluate(f"{path}->_M_nxt"))
        if len(self.paths) == index + 1:
            self.paths.append(f"{path}->_M_nxt")
        return following

    def node(self, index, address, count):
        path = self.paths[index]
        words = [self.link(index, address)]
        words += [word(self.evaluate(f"{path}[{k}]._M_nxt")) for k in range(1, -(-count // 8))]
        data = struct.pack(f"<{len(words)}Q", *words)
        return data[:count]


class Paged(collections.abc.Sequence):
    """``length`` items, the first of them ``head``; ``page(start, stop)`` makes the
    items [start, stop) the first time any of them is asked for (a container's entries
    past the first ``MAX_NODES``: read when a page, a position or a key needs them)."""

    def __init__(self, length, head, page):
        self.length, self.items, self.page = length, dict(enumerate(head)), page

    def __len__(self):
        return self.length

    def __getitem__(self, index):
        if isinstance(index, slice):
            wanted = range(*index.indices(self.length))
            missing = [at for at in wanted if at not in self.items]
            if missing:
                made = self.page(missing[0], missing[-1] + 1)
                for at, item in zip(range(missing[0], missing[-1] + 1), made):
                    self.items.setdefault(at, item)
            return [self.items[at] for at in wanted]
        if type(index) is not int or not -self.length <= index < self.length:
            raise IndexError(index)
        return self[index % self.length : index % self.length + 1][0]


def _type_name(graph, offset, depth=0):
    """A type as the debugger names it: its qualifiers, then a typedef's name, a base
    type's (as LLDB spells an integer: `unsigned long`), an enum's or a class's qualified
    name, or a pointer's (`const char *`, `int *const`, `Pt **`)."""
    from peeky.fields import _spelled

    words = []
    for _ in range(64):
        node = graph.get(offset)
        if node is None or depth > 8:
            return None
        if node[0] == "qualifier":
            if node[1] in ("const", "volatile"):
                words.append(node[1])
            offset = node[2]
            continue
        if node[0] == "typedef" and not node[1]:
            offset = node[2]
            continue
        if node[0] == "pointer":
            aimed = _type_name(graph, node[2], depth + 1) if node[2] is not None else None
            if not aimed:
                return None
            return aimed + ("*" if aimed.endswith("*") else " *") + " ".join(words)
        if node[0] == "typedef":
            name = node[1]
        elif node[0] == "base":
            name = _spelled(node[1])
        elif node[0] == "enum":
            name = (node[5] if len(node) > 5 else None) or node[1]
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


class _Withheld(Exception):
    """A value in an entry that Peeky withholds on its own, with why (a container held
    in a node whose elements it doesn't read): the entry around it is still read."""


def _withheld(reason):
    return {
        "value": None,
        "unavailable": True,
        "withheld": True,
        "withheld_as": "not read",
        "reason": reason[:300],
    }


def _kind(qualified):
    """A standard container's class template, as code names it (`std::vector`)."""
    found = _CONTAINER.match(qualified or "")
    return found[1] if found else qualified


class _Reader:
    """Decodes a node's value into the listing the debugger would give."""

    def __init__(self, graph, memory):
        self.graph, self.memory, self.listed = graph, memory, {}

    def add(self, children):
        reference = len(self.listed) + 1
        self.listed[reference] = children
        return reference

    def fetch(self, reference, count, start=0):
        return list(self.listed.get(reference, ())[start : start + count])

    def chain(self, placed, memory, count, depth, decode):
        """(the entries of the first ``decode`` nodes, every node's address) of a
        chain from ``memory.first`` that must end after exactly ``count`` nodes, each
        aligned; ``placed``: ``layout``'s. Past ``decode``, only each node's link is
        read."""
        value, at, size, alignment = placed
        entries, addresses = [], []
        address = memory.first
        while address:
            if len(addresses) == count:
                raise Unread("a longer chain than its count")
            if address % 8 or (address + at) % alignment:
                raise Unread("a misaligned node")
            index = len(addresses)
            addresses.append(address)
            if index < decode:
                data = memory.node(index, address, at + size)
                following = struct.unpack_from("<Q", data)[0]
                part = self.value(value, data[at : at + size], address + at, depth)
                entries.append({"name": f"[{index}]", **part})
            else:
                following = memory.link(index, address)
            address = following
        if len(addresses) != count:
            raise Unread("a shorter chain than its count")
        return entries, addresses

    def later(self, placed, addresses, index):
        """Entry ``index`` past those read with the listing, from the node the chain's
        walk placed (``chain``); withheld, with why, where it doesn't read."""
        from peeky.adapters import ViewBudgetExhausted

        value, at, size, _ = placed
        address = addresses[index]
        following = addresses[index + 1] if index + 1 < len(addresses) else 0
        try:
            data = self.memory.node(index, address, at + size)
            if struct.unpack_from("<Q", data)[0] != following:
                raise Unread("its link is not the one the walk read")
            return {"name": f"[{index}]", **self.value(value, data[at : at + size], address + at)}
        except ViewBudgetExhausted:
            raise
        except Unread as exc:
            why = f"Peeky can't read this entry from its node ({exc}); print it from the program"
        except ValueError as exc:
            why = f"the debugger didn't read this entry's node ({exc}); print it from the program"
        shown = {"type": _type_name(self.graph, value), "view": "memory", "offset": value}
        return {"name": f"[{index}]", **shown, "variablesReference": 0, **_withheld(why)}

    def value(self, offset, data, address, depth=0):
        """{"type", "value", "variablesReference", "view": "memory"} of the value of type
        ``offset`` held in ``data`` at ``address``; a struct's or pair's members are its
        children, and its value is `T @ 0x…`, as lldb-dap shows a struct. A container
        held in it is listed (`size=2`, its elements its children), or withheld on its
        own (``nested``)."""
        graph = self.graph
        node, name = _under(graph, offset), _type_name(graph, offset)
        if node is None or not name:
            raise Unread("a type the walk did not read")
        shown = {"type": name, "variablesReference": 0, "view": "memory", "offset": offset}
        if node[0] == "base":
            return {**shown, "value": _scalar(node, data)}
        if node[0] == "enum":
            return {**shown, "value": _enumerated(node, data)}
        if node[0] == "pointer":
            # Its address alone: what it points at is never read.
            return {**shown, "value": _address(node, data)}
        if node[0] != "record" or node[2] == "union" or node[7] or node[8] or depth > DEPTH:
            raise Unread(f"`{name}`")
        qualified = node[11] if len(node) > 11 else ""
        if type(node[3]) is not int or node[3] != len(data):
            raise Unread(f"`{name}`: no size")
        if node[9] and _STRING.match(qualified or ""):
            try:
                return {**shown, "value": self.string(node, data, address), "text": True}
            except _Withheld as exc:
                return {**shown, **_withheld(str(exc))}
        if node[9] and _CONTAINER.match(qualified or ""):
            try:
                children = self.nested(node, data, address, depth)
            except _Withheld as exc:
                return {**shown, **_withheld(str(exc))}
            reference = self.add(children)
            return {**shown, "value": f"size={len(children)}", "variablesReference": reference}
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

    def nested(self, node, data, address, depth):
        """The elements of a standard container held in a node (#374): a `std::array`'s,
        inside it. Any other is _Withheld, with why: a `std::vector`'s buffer, or a nested
        unordered container's nodes, are heap blocks that nothing proves still allocated.
        A value destroyed in place (`std::destroy_at(&m[1])`), before a new one is built
        there, keeps its old pointers, so its old elements would read as if they were
        its own."""
        qualified = node[11]
        kind = _kind(qualified)
        if _ARRAY.match(qualified):
            return self.array(node, data, address, depth)
        if _VECTOR.match(qualified) or _UNORDERED.match(qualified):
            raise _Withheld(
                f"Peeky doesn't read a `{kind}` held in an unordered container's node: its "
                "elements are in a heap block, which Peeky can't show is still allocated; "
                "print it from the program"
            )
        raise _Withheld(
            f"Peeky doesn't read a `{kind}` held in an unordered container's node; print it "
            "from the program"
        )

    def elements(self, kind, data, address, size, count, depth):
        try:
            return [
                {
                    "name": f"[{at}]",
                    **self.value(
                        kind, data[at * size : (at + 1) * size], address + at * size, depth + 1
                    ),
                }
                for at in range(count)
            ]
        except Unread as exc:
            raise _Withheld(
                f"Peeky doesn't read its elements from memory ({exc}); print it from the program"
            ) from None

    def array(self, node, data, address, depth):
        graph = self.graph
        held, count = _parameter(node, "type", "_Tp"), _parameter(node, "value", "_Nm")
        elements = _member(node, "_M_elems")
        kind = _under(graph, elements[1]) if elements is not None else None
        size = _size(graph, held[2]) if held is not None else None
        if (
            count is None
            or type(count[3]) is not int
            or kind is None
            or kind[0] != "array"
            or kind[3] != (count[3],)
            or not _same(graph, kind[2], held[2])
            or elements[2] != 0
            or elements[3]
            or node[10]
            or len(node[5]) != 1
            or not size
            or size * count[3] != len(data)
        ):
            raise _Withheld(
                f"Peeky can't place this `{_kind(node[11])}`'s elements in the build's debug "
                "information; print it from the program"
            )
        if count[3] > MAX_NODES:
            raise _Withheld(
                f"it holds {count[3]} elements, more than the {MAX_NODES} Peeky reads in a "
                "container held in a node; print it from the program"
            )
        return self.elements(held[2], data, address, size, count[3], depth)

    def string(self, node, data, address):
        """A libstdc++ string's text, as Peeky shows one read from memory (#128), where its
        data pointer points into its own buffer, right after its pointer and length,
        inside the node. Else _Withheld: its text is in heap memory, which nothing proves
        still allocated. A string destroyed in place (`std::destroy_at(&m[1])`), before a
        new one is built there, keeps a pointer to its freed text (#374 review)."""
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
        if text != address + 16:
            raise _Withheld(
                "Peeky reads a string in an unordered container's node only where its text "
                "is in the node: this one's is outside it, in heap memory Peeky can't show "
                "is still allocated; print it from the program"
            )
        if length >= buffer or 16 + buffer > len(data):
            raise Unread("a string longer than its own buffer")
        return render(data[16 : 16 + length], 1, "", False)


def _wide(name, encoding, size):
    """A wide character type's literal prefix (`L`, `u`, `U`), or None."""
    prefix = _WIDE.get(name)
    if prefix == "L" and encoding in ("DW_ATE_signed", "DW_ATE_unsigned") and size in (2, 4):
        return prefix
    if prefix in ("u", "U") and encoding in ("DW_ATE_UTF", "DW_ATE_unsigned"):
        return prefix if size == (2 if prefix == "u" else 4) else None
    return None


def _scalar(node, data):
    """A number's, a bool's or a character's text, as the debugger shows it; else
    Unread."""
    _, name, encoding, size = node
    if size != len(data):
        raise Unread(f"`{name}`: no size")
    if encoding == "DW_ATE_boolean" and size == 1 and data[0] in (0, 1):
        return "true" if data[0] else "false"
    if encoding == "DW_ATE_float" and size in (4, 8):
        return float_text(data)
    if encoding in ("DW_ATE_signed_char", "DW_ATE_unsigned_char") and size == 1:
        if name not in _CHARACTERS:
            return character_text(data[0])
    if (prefix := _wide(name, encoding, size)) is not None:
        return character_text(int.from_bytes(data, "little"), prefix)
    if encoding in ("DW_ATE_signed", "DW_ATE_unsigned") and size in (2, 4, 8):
        if name not in _CHARACTERS:
            return str(int.from_bytes(data, "little", signed=encoding == "DW_ATE_signed"))
    if encoding == "DW_ATE_float":
        raise Unread(f"a `{name}`, whose bytes the debug information doesn't say the format of")
    raise Unread(f"`{name}`")


def _enumerated(node, data):
    """An enum's text: the one enumerator whose value it holds, else the number (which
    no enumerator, or several, hold), as signed as the enum is."""
    size = node[2]
    values, signed = (node[3], node[4]) if len(node) > 4 else (None, None)
    if size != len(data) or size not in (1, 2, 4, 8) or values is None:
        raise Unread(f"enum `{node[1]}`, whose enumerators the walk did not read")
    raw = int.from_bytes(data, "little")
    negative = raw >> (8 * size - 1)
    if negative and signed is None:
        raise Unread(f"enum `{node[1]}`, which the debug information doesn't say is signed")
    number = raw - (1 << (8 * size)) if negative and signed else raw
    named = [name for name, value in values if value == number]
    return named[0] if len(named) == 1 else str(number)


def _address(node, data):
    """A pointer's address, as LLDB writes one (`0x0000aaaaaaab12f0`)."""
    if node[2] is None or node[3] not in (8, None) or len(data) != 8:
        raise Unread("a pointer Peeky does not read")
    return f"0x{int.from_bytes(data, 'little'):016x}"


def character_text(code, prefix=""):
    """A character as LLDB writes a `char` (`'a'`, `'\\n'`, `'\\xc8'`): printable ASCII as
    itself, LLDB's escapes, else its code in hex. A wide one (``prefix`` `L`, `u` or `U`)
    likewise, and a printable character past ASCII as itself; never LLDB's text of an
    invalid one (`U'\\U0000fffd'` for 0x110000, `u''` for a lone surrogate)."""
    if code in _ESCAPES:
        shown = _ESCAPES[code]
    elif 0x20 <= code < 0x7F:
        shown = chr(code)
    elif (
        prefix
        and 0xA0 <= code <= 0x10FFFF
        and not 0xD800 <= code < 0xE000
        and unicodedata.category(chr(code))[0] in "LMNPS"
    ):
        shown = chr(code)
    else:
        shown = f"\\x{code:02x}"
    return f"{prefix}'{shown}'"


def float_text(data):
    """A `float` or `double` (4 or 8 bytes, little-endian) as LLDB writes one: LLVM's
    `APFloat::toString` with its own precision (9 or 17 significant digits, so the text
    reads back as the same number) and LLDB's 6 zeros of padding, past which it is in
    scientific notation: `0.10000000000000001`, `100`, `1.0E+10`, `-0`, `+Inf`, `NaN`."""
    exponent_bits, fraction_bits = (8, 23) if len(data) == 4 else (11, 52)
    bits = int.from_bytes(data, "little")
    negative = bits >> (exponent_bits + fraction_bits)
    exponent = (bits >> fraction_bits) & ((1 << exponent_bits) - 1)
    fraction = bits & ((1 << fraction_bits) - 1)
    sign = "-" if negative else ""
    if exponent == (1 << exponent_bits) - 1:
        return "NaN" if fraction else ("-Inf" if negative else "+Inf")
    if not exponent and not fraction:
        return sign + "0"
    bias = (1 << (exponent_bits - 1)) - 1
    if exponent:
        significand, power = fraction | (1 << fraction_bits), exponent - bias - fraction_bits
    else:
        significand, power = fraction, 1 - bias - fraction_bits
    return sign + _decimal(significand, power, fraction_bits + 1)


def _decimal(significand, power, precision):
    """``significand`` times 2 to the ``power``, as `APFloat::toString` writes it with
    no precision given (the semantics' ``precision`` in bits) and LLDB's padding: step
    by step as LLVM does, its truncation and its rounding half up too."""
    wanted = 2 + precision * 59 // 196
    zeros = (significand & -significand).bit_length() - 1
    significand >>= zeros
    power += zeros
    if power > 0:
        significand <<= power
        power = 0
    elif power < 0:
        significand *= 5**-power
    required = (wanted * 196 + 58) // 59
    if significand.bit_length() > required:
        removable = (significand.bit_length() - required) * 59 // 196
        if removable:
            power += removable
            significand //= 10**removable
    digits = []
    while significand:
        significand, digit = divmod(significand, 10)
        if digit or digits:
            digits.append(digit)
        else:
            power += 1
    if len(digits) > wanted:
        first = len(digits) - wanted
        if digits[first - 1] < 5:
            while first < len(digits) and digits[first] == 0:
                first += 1
        else:
            while first < len(digits) and digits[first] == 9:
                first += 1
            if first == len(digits):
                # Carried through every digit kept: one digit of precision, a 1.
                digits = digits + [0]
            digits[first] += 1
        power += first
        digits = digits[first:]
    count, text = len(digits), "".join(map(str, reversed(digits)))
    if power >= 0:
        scientific = power > _PADDING or count + power > wanted
    else:
        scientific = -(power + count - 1) > _PADDING
    if scientific:
        exponent = power + count - 1
        return f"{text[0]}.{text[1:] or '0'}E{'+' if exponent >= 0 else '-'}{abs(exponent)}"
    if power >= 0:
        return text + "0" * power
    whole = power + count
    if whole > 0:
        return f"{text[:whole]}.{text[whole:]}"
    return "0." + "0" * -whole + text


def readable(graph, offset, depth=0):
    """Whether ``read_entries`` decodes a value of type ``offset`` at all, judged by its
    type alone, as ``_Reader.value`` judges it: a number, a bool, a character, an enum
    whose enumerators were read, a pointer, a libstdc++ string Peeky places, a standard
    container or string (each read, or withheld on its own: a string whose text is
    outside its node), or a pair or a struct of them. For a row's advice to read a
    container once nothing may be changing it (eval 13 prep, finding 3)."""
    node, name = _under(graph, offset), _type_name(graph, offset)
    if node is None or not name:
        return False
    if node[0] == "base":
        _, base, encoding, size = node
        if encoding == "DW_ATE_boolean":
            return size == 1
        if encoding == "DW_ATE_float":
            return size in (4, 8)
        if encoding in ("DW_ATE_signed_char", "DW_ATE_unsigned_char") and size == 1:
            return base not in _CHARACTERS
        if _wide(base, encoding, size) is not None:
            return True
        return (
            encoding in ("DW_ATE_signed", "DW_ATE_unsigned")
            and size in (2, 4, 8)
            and (base not in _CHARACTERS)
        )
    if node[0] == "enum":
        return len(node) > 4 and node[3] is not None and node[2] in (1, 2, 4, 8)
    if node[0] == "pointer":
        return node[2] is not None and node[3] in (8, None)
    if node[0] != "record" or node[2] == "union" or node[7] or node[8] or depth > DEPTH:
        return False
    qualified = node[11] if len(node) > 11 else ""
    if type(node[3]) is not int:
        return False
    if node[9] and _CONTAINER.match(qualified or ""):
        return True
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
    "variablesReference": ..., "length": N}, ``fetch(reference, count, start=0)`` for
    its listing) read from the nodes of the unordered container ``container`` (a
    fields.type_graph record), which counts ``count`` entries itself (its
    `_M_element_count`); ``memory``: a ``Memory`` or ``Words``. The whole chain is
    walked, at most ``MAX_CHAIN`` nodes by address or ``MAX_WORDS`` through variable
    paths; the first ``MAX_NODES`` entries are read with it, and any later one when it is
    fetched. Unread where any of it is not proven."""
    placed = layout(graph, container)
    if not 0 <= count <= (MAX_CHAIN if isinstance(memory, Memory) else MAX_WORDS):
        raise Unread("too many entries")
    reader = _Reader(graph, memory)
    entries, addresses = reader.chain(placed, memory, count, 0, MAX_NODES)
    if count > MAX_NODES:
        entries = Paged(
            count,
            entries,
            lambda start, stop: [reader.later(placed, addresses, at) for at in range(start, stop)],
        )
    reference = reader.add(entries)
    listed = {"value": f"size={count}", "variablesReference": reference, "view": "memory"}
    return {**listed, "length": count}, reader.fetch


def view_children(graph, root, fetch):
    """The entries of ``read_entries``'s listing as a value view's children: {"name",
    "type", "value"}, "text" for a string, why for a value withheld on its own, and a
    pair's, struct's or container's members or elements as its "children". A struct's
    value is its preview (`{x = 1, y = 2}`), and a container's (`size=2 [1, 2]`), as a
    listing shows them; a pair's stays `std::pair<…> @ 0x…`, as lldb-dap shows one. A
    ``Paged`` past ``MAX_NODES`` entries, each read when asked for."""
    from peeky.native_previews import Typed, preview

    def element(variable):
        shown = {key: variable[key] for key in ("name", "type", "value")}
        for key in ("text", "unavailable", "withheld", "withheld_as", "reason"):
            if variable.get(key):
                shown[key] = variable[key]
        reference = variable.get("variablesReference")
        if reference:
            shown["children"] = [element(part) for part in fetch(reference, MAX_NODES)]
            typed = Typed(graph, variable["offset"])
            previewed = preview(variable, fetch, typed, lambda child: child.get("value"))
            if previewed is not None:
                shown["value"] = previewed
        return shown

    reference = root["variablesReference"]
    head = [element(entry) for entry in fetch(reference, MAX_NODES)]
    length = root.get("length", len(head))
    if length <= len(head):
        return head
    return Paged(
        length,
        head,
        lambda start, stop: [element(entry) for entry in fetch(reference, stop - start, start)],
    )
