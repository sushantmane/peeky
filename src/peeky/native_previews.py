"""Previews of C and C++ structs, classes and fixed-size arrays (#136).

lldb-dap shows a struct, class, union or array that has no summary of its own as its
type and address (`bin[4] @ 0x16bb9f278`), so an agent had to list every field by hand.
A preview is written from the debugger's own children, in the C-like form LLDB's
`frame variable` uses: `{lo = 0, hi = 2, count = 0}`, an array `[5, 4, 3]`, an array of
structs `[{lo = 0, hi = 2, count = 0}, {...}]`, a 2-D array `[[1, 2, 3], [4, 5, 6]]`.

What a value is comes from the build's debug information (``Typed``, over
fields.type_graph), never from the type names lldb-dap shows: a typedef can hide an
array or a pointer, a template's arguments hold brackets (`Hold<int[1]>[5]`), and a
variable-length array's bound is not a constant (LLDB reads another's). The debugger's
children give the values, matched to the type's members by name. When anything does
not match, there is no preview: the value keeps the debugger's `T @ 0x…`.

Bounded as the Java and Rust previews are: ``ITEMS`` members or elements per level,
then `…`; ``DEPTH`` levels opened (deeper ones show `{…}` or `[…]`); ``TEXT``
characters of one member (a string cut between characters with LLDB's `...`, its note
kept whole); ``LENGTH`` characters in all, cut between members. Each level is one
`variables` request, counted against the worker's preview budget.

Nothing is guessed: a union is not previewed (`<union>` inside another value: only one
member holds what was stored); a pointer, or a reference, inside shows its address and
is never followed; a member with no value shows `<no value>`; a variable-length array
shows `[…]`, and is not previewed itself; an incomplete type (declared, never defined:
an opaque handle) is not previewed; a standard-library value keeps the debugger's
summary (`size=2`, a string's text, checked as the worker checks every string, #128)
or shows `{…}`; a base class shows as a member named by its class
(`{Base = {b = 1}, d = 2}`), as LLDB lists it; an anonymous struct shows its members in
braces, with no name; the unnamed bitfields LLDB adds to fill gaps are left out. LLDB
reads a char-typed bitfield as an unsigned character (`'\\x05'` for `signed char sc:3`
holding -3): it shows as the number its bits hold, sign-extended where the type is
signed. A wide character array (`wchar_t`, `char16_t`, `char32_t`) shows element by
element: LLDB's text of one ends at its first NUL.
"""

import hashlib
import re

from peeky.cxx_strings import shorten
from peeky.fields import CXX_CONTAINER

ITEMS = 8
DEPTH = 2
TEXT = 40
LENGTH = 240
# Children read for a struct: its members (64 is one view request's bound).
MEMBERS = 64

# lldb-dap's text for a value with neither a value nor a summary of its own.
_PLACEHOLDER = re.compile(r".+ @ 0x[0-9a-fA-F]+")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]+(?=\s|$)")
_INDEX = re.compile(r"\[(0|[1-9][0-9]{0,9})\]")
# A one-byte character, as a char-typed bitfield is: its value as a code.
_NARROW = frozenset({"DW_ATE_signed_char", "DW_ATE_unsigned_char"})
# Never shown as LLDB's literal, whatever their size (C++'s own types).
_WIDE = frozenset({"wchar_t", "char8_t", "char16_t", "char32_t"})
_CHARACTER = re.compile(r"(?:u8|[LuU])?'(.+)'")
_WIDE_LITERAL = re.compile(r"(?<=\s)(?:u8|[LuU])'(?:[^'\\]|\\[^']+|\\')'$")
_ESCAPES = {"0": 0, "a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, "e": 27}
# The standard containers previewed (#181), by how LLDB lists them: elements [0], [1], ...
# in order (a sequence's, a set's, in iteration order) or a map's entries, each a pair.
_CONTAINERS = {
    "vector": "sequence",
    "array": "sequence",
    "deque": "sequence",
    "list": "sequence",
    "map": "map",
    "unordered_map": "map",
    "set": "set",
    "multiset": "set",
    "unordered_set": "set",
    "unordered_multiset": "set",
}
_CXX_ARRAY = re.compile(r"std::(?:__\w+::)*array<")
_UNORDERED = re.compile(r"std::(?:__\w+::|pmr::)*unordered_(?:multi)?(?:map|set)<")
# An unordered container whose count is not proven (#167): never its debugger's summary.
UNLISTED = "<not listed>"
# LLDB's summary of a standard container: its element count.
_SIZE = re.compile(r"size=(0|[1-9][0-9]{0,9})")


class Typed:
    """What the build's debug information says a value is: a type in
    ``graph`` (fields.type_graph), with the array dimensions still to index (a
    multi-dimensional array is one type) and, for a bitfield member, its width. Or one
    entry LLDB lists of a standard map (``entry``: its key's and value's Typed), which
    the program's types do not name (#181)."""

    def __init__(self, graph, offset, dims=None, bits=None, entry=None):
        self.graph, self.offset, self.dims, self.bits = graph, offset, dims, bits
        self.entry = entry

    def node(self):
        """The type under typedefs and qualifiers, or None if the walk did not read it."""
        offset = self.offset
        for _ in range(64):
            node = self.graph.get(offset)
            if node is None or node[0] not in ("typedef", "qualifier"):
                return node
            offset = node[2]
        return None

    def names(self):
        """The type's names on the way to what it is: typedefs (`wchar_t` in C, where
        it is a typedef of `int`), then a base type's own."""
        found, offset = [], self.offset
        for _ in range(64):
            node = self.graph.get(offset)
            if node is None:
                break
            if node[0] == "typedef":
                found.append(node[1])
            elif node[0] != "qualifier":
                if node[0] == "base":
                    found.append(node[1])
                break
            offset = node[2]
        return found

    def wide_character(self):
        """Whether it is a character type wider than a byte (`wchar_t`, `char16_t`,
        `char32_t`, or UTF): LLDB shows one as a character, never as a number."""
        scalar = self.scalar()
        return scalar is not None and (
            scalar[1] == "DW_ATE_UTF" or any(name in _WIDE for name in self.names())
        )

    def kind(self):
        """ "record", "union", "array", "pointer", "reference", "scalar", "library",
        "entry" (a map's), "incomplete" or "unknown"."""
        if self.entry:
            return "entry"
        if self.dims:
            return "array"
        node = self.node()
        if node is None:
            return "unknown"
        if node[0] == "record":
            if node[9]:
                return "library"
            if node[8]:
                return "incomplete"
            if node[7]:
                return "unknown"
            return "union" if node[2] == "union" else "record"
        if node[0] in ("array", "pointer", "reference"):
            return node[0]
        if node[0] in ("base", "enum"):
            return "scalar"
        return "unknown"

    def _array(self):
        """(element type, dimensions) of an array; a dimension is None where the bound is
        not a constant (a variable-length array) or not given."""
        if self.dims:
            return self.offset, self.dims
        node = self.node()
        if node is None or node[0] != "array":
            return None
        return node[2], tuple(node[3]) if node[3] else (None,)

    def count(self):
        """How many elements an array holds, or None where the debug information gives
        no constant (a variable-length array)."""
        found = self._array()
        return found[1][0] if found else None

    def element(self):
        offset, dims = self._array()
        return Typed(self.graph, offset, dims[1:] or None)

    def target(self):
        """What a pointer or reference refers to, or None if not read."""
        node = self.node()
        if node is None or node[0] not in ("pointer", "reference") or node[2] is None:
            return None
        if node[2] not in self.graph:
            return None
        return Typed(self.graph, node[2])

    def members(self):
        """[(name or None, Typed)] of a record's members, in order; then its bases as
        [(class name, Typed)]."""
        node = self.node()
        members = [
            (member[0], Typed(self.graph, member[1], bits=member[5] if len(member) > 5 else None))
            for member in node[5]
        ]
        bases = []
        for base in node[10] if len(node) > 10 else ():
            inner = Typed(self.graph, base).node()
            bases.append((inner[1] if inner is not None else None, Typed(self.graph, base)))
        return members, bases

    def placed_members(self):
        """[(name, Typed, byte offset)] of a record's or union's named members that the
        debug information places (#336): never a bitfield, nor a struct member it gives
        no offset (a union's are at 0)."""
        node = self.node()
        if node is None or node[0] != "record":
            return []
        found = []
        for member in node[5]:
            offset = member[2]
            if offset is None and node[2] == "union":
                offset = 0
            if member[0] and not member[3] and type(offset) is int and offset >= 0:
                found.append((member[0], Typed(self.graph, member[1]), offset))
        return found

    def placed_bases(self):
        """[(name, Typed, byte offset)] of a class's base classes that the debug information
        places (#339): never a virtual one, whose place only the running object says."""
        node = self.node()
        if node is None or node[0] != "record" or len(node) < 14:
            return []
        found = []
        for base, offset in zip(node[10], node[13]):
            inner = Typed(self.graph, base)
            name = (inner.node() or ("", None))[1]
            if name and type(offset) is int and offset >= 0:
                found.append((name, inner, offset))
        return found

    def byte_size(self):
        """How many bytes one value of the type takes, from the debug information, or
        None where it does not say (#302): a base type's, an enum's, a complete
        record's, a pointer's (8 where it leaves that to the target, as clang's does,
        for one that names what it points at: a pointer to a member may be 16), or an
        array's of a constant length. Never a bitfield's, a reference's or a map
        entry's."""
        if self.entry or self.bits is not None:
            return None
        found = self._array()
        if found is not None:
            count, element = found[1][0], self.element().byte_size()
            return count * element if type(count) is int and count > 0 and element else None
        node = self.node()
        size = None
        if node is None:
            return None
        if node[0] == "base":
            size = node[3]
        elif node[0] == "enum":
            size = node[2]
        elif node[0] == "record" and not node[8]:
            size = node[3]
        elif node[0] == "pointer":
            size = 8 if node[3] is None and node[2] is not None else node[3]
        return size if type(size) is int and size > 0 else None

    def scalar(self):
        """(base type name, DWARF encoding, size) of a base type, or None."""
        if self.dims:
            return None
        node = self.node()
        return (node[1], node[2], node[3]) if node is not None and node[0] == "base" else None

    def narrow(self):
        """Whether an array's elements are one-byte characters, which LLDB shows as a
        literal bounded by the array's size."""
        element = self.element().scalar()
        return (
            element is not None
            and element[1] in _NARROW
            and element[2] == 1
            and element[0] not in _WIDE
        )

    def child(self, name):
        """The type of the child the debugger names ``name`` (`end`, `[2]`, `*p`, a base
        class, a standard container's element or a map entry's `first`), or None: the
        steps of a selector's path."""
        sort = self.kind()
        if sort == "entry":
            return {"first": self.entry[0], "second": self.entry[1]}.get(str(name))
        if sort == "library":
            return self._contained(str(name))
        if sort in ("pointer", "reference"):
            # The debugger lists what a pointer points at as its children (#102).
            target = self.target()
            if target is None:
                return None
            return target if str(name).startswith("*") else target.child(name)
        if sort == "array":
            index = _INDEX.fullmatch(str(name))
            count = self.count()
            if index is None or count is None or int(index[1]) >= count:
                return None
            return self.element()
        if sort in ("record", "union"):
            members, bases = self.members()
            named = [typed for member, typed in members if member == name]
            based = [typed for base, typed in bases if base is not None and _names(name, base)]
            if len(named) + len(based) != 1:
                # A member named as a base class (`struct B2 : A2 { int A2; }`): lldb-dap
                # lists both by one name, so neither can be told.
                return None
            return (named or based)[0]
        return None

    def container(self):
        """A standard container's kind and what its elements are (#181), or None:
        ("sequence", element) for `std::vector`, `std::deque`, `std::list` and
        `std::array`; ("map", key, value) for `std::map` and `std::unordered_map`;
        ("set", key) for the sets. The types are the class template's own arguments in
        the build's debug information (`_Tp`, `_Key`), never LLDB's names."""
        if self.dims or self.entry:
            return None
        node = self.node()
        if node is None or node[0] != "record" or not node[9] or len(node) < 13:
            return None
        found = CXX_CONTAINER.match(node[11] or "")
        if found is None:
            return None
        types = [p[2] for p in node[12] if p[0] == "type"]
        sort = _CONTAINERS.get(found[1])
        wanted = 2 if sort == "map" else 1
        if sort is None or len(types) < wanted or any(t not in self.graph for t in types[:wanted]):
            return None
        return (sort, *(Typed(self.graph, offset) for offset in types[:wanted]))

    def unordered(self):
        """Whether it is a C++ unordered container (`std::unordered_map` and its kin, the
        multimap too), whose count LLDB's libstdc++ formatter can get wrong (#167)."""
        node = None if self.dims or self.entry else self.node()
        return (
            node is not None
            and node[0] == "record"
            and bool(node[9])
            and len(node) > 11
            and _UNORDERED.match(node[11] or "") is not None
        )

    def resolved(self):
        """The offset of the type under typedefs and qualifiers (a multi-dimensional
        array's element as it is), or None."""
        if self.dims or self.entry:
            return None
        offset = self.offset
        for _ in range(64):
            node = self.graph.get(offset)
            if node is None:
                return None
            if node[0] not in ("typedef", "qualifier"):
                return offset
            offset = node[2]
        return None

    def identity(self):
        """The type under typedefs and qualifiers as every compile unit of a build names it
        alike (#336), so that a pointer in one source file matches an array in another: a
        base type by its name, encoding and size; a struct, class or union by its layout
        (``_layout``: its name, size, and each member's name, offset and type, those of
        its members too), never by its name alone, which two files may give two layouts.
        A pointer is told by what it points at, by name (#339: an `int **` in one file at
        an `int *` local in another). Any other type (an enum, whose values the walk does
        not read), or a record or pointer whose layout was not all read, is its offset in
        the debug information: the same type only within one unit. None where it was not
        read."""
        offset = self.resolved()
        node = self.graph.get(offset) if offset is not None else None
        if node is None:
            return None
        if node[0] == "base":
            return f"base {node[1]} {node[2]} {node[3]}"
        if node[0] == "pointer" and (layout := _layout(self.graph, offset)) is not None:
            return f"pointer {layout}"
        if node[0] == "record" and (layout := _layout(self.graph, offset)) is not None:
            digest = hashlib.sha256(layout.encode()).hexdigest()[:16]
            return f"{node[2]} {node[11] if len(node) > 11 else node[1]} {node[3]} {digest}"
        return offset

    def array_member(self):
        """(name, Typed) of a `std::array`'s one member, the C array of its elements
        (libc++'s `__elems_`, libstdc++'s `_M_elems`), which LLDB lists where it has no
        formatter of its own for the class; or None."""
        found = self.container()
        node = self.node()
        if found is None or found[0] != "sequence" or not _CXX_ARRAY.match(node[11] or ""):
            return None
        members, bases = self.members()
        if len(members) != 1 or bases or not members[0][0]:
            return None
        name, member = members[0]
        if member.kind() != "array" or member.count() is None:
            return None
        element = member.element()
        size = [p[3] for p in node[12] if p[0] == "value"]
        if element.dims or element.resolved() is None:
            return None
        if element.resolved() != found[1].resolved() or size[:1] != [member.count()]:
            # Not an array of the class's `_Tp`, `_Size` of them (libc++'s
            # `std::array<T, 0>` holds bytes).
            return None
        return name, member

    def _contained(self, name):
        """``child`` of a standard container (#181): an element (`[2]`), a map's entry
        (its `first` and `second`), or a `std::array`'s C array."""
        found = self.container()
        if found is None:
            return None
        if _INDEX.fullmatch(name):
            if found[0] == "map":
                return Typed(self.graph, None, entry=(found[1], found[2]))
            return found[1]
        member = self.array_member()
        return member[1] if member is not None and member[0] == name else None


def _layout(graph, offset, depth=0):
    """A type's layout as text that two compile units write alike (``Typed.identity``),
    under typedefs and qualifiers: a base type's name, encoding and size; an enum's name
    and size; a pointer's size and what it points at, by name alone; an array's lengths
    and element; a record's kind, name and size, then each member's name, offset, bit
    width and layout, and its base classes'. None where any part was not read, or is
    deeper than 8 levels."""
    for _ in range(64):
        node = graph.get(offset)
        if node is None or node[0] not in ("typedef", "qualifier"):
            break
        offset = node[2]
    if node is None or depth > 8:
        return None
    if node[0] == "base":
        return f"{node[1]}/{node[2]}/{node[3]}"
    if node[0] == "enum":
        return f"enum {node[1]}/{node[2]}"
    if node[0] == "pointer":
        aimed = _named(graph, node[2])
        return None if aimed is None else f"*{aimed}/{node[3]}"
    if node[0] == "array":
        inner = _layout(graph, node[2], depth + 1) if node[3] else None
        return None if inner is None else f"{list(node[3])}{inner}"
    if node[0] != "record" or node[7] or node[8] or len(node) < 11:
        return None
    parts = []
    for member in node[5]:
        inner = _layout(graph, member[1], depth + 1)
        at = 0 if member[2] is None and node[2] == "union" else member[2]
        if inner is None or at is None:
            return None
        bits = member[5] if len(member) > 5 else None
        parts.append(f"{member[0]}@{at}:{bits}:{inner}")
    bases = [_layout(graph, base, depth + 1) for base in node[10]]
    if None in bases:
        return None
    name = node[11] if len(node) > 11 else node[1]
    return f"{node[2]} {name}/{node[3]}{{{';'.join(parts)}}}:{','.join(bases)}"


def _named(graph, offset, depth=0):
    """What a pointer points at, as ``_layout`` names it without following it: a base
    type's ``_layout``, a record's or enum's kind and name, a pointer's own target and an
    array's element so named, `fn` for a function, `void`; None where unread."""
    if offset is None:
        return "void"
    for _ in range(64):
        node = graph.get(offset)
        if node is None or node[0] not in ("typedef", "qualifier"):
            break
        offset = node[2]
    if node is None or depth > 4:
        return None
    if node[0] == "base":
        return _layout(graph, offset)
    if node[0] == "record":
        return f"{node[2]} {node[11] if len(node) > 11 else node[1]}"
    if node[0] == "enum":
        return f"enum {node[1]}"
    if node[0] == "pointer":
        aimed = _named(graph, node[2], depth + 1)
        return None if aimed is None else f"*{aimed}"
    if node[0] == "array" and node[3]:
        inner = _named(graph, node[2], depth + 1)
        return None if inner is None else f"{list(node[3])}{inner}"
    if node[0] == "other" and node[1] == "DW_TAG_subroutine_type":
        # A function, whose parameters the walk does not read (#339: a callback member,
        # a C++ class's vtable pointer).
        return "fn"
    return None


def _names(shown, base):
    """Whether lldb-dap's name for a base-class child (`Base`, `ns::Base`, `Base<int>`)
    is the class ``base`` (its debug information's name)."""
    return shown == base or str(shown).endswith("::" + base)


WIDE_BITFIELD = (
    "a bitfield of a wide character type (`char16_t`, `char32_t`, `wchar_t`), which LLDB "
    "reads at other bits than the program's (u'a' in a `char16_t w : 9` shows as U+030D); "
    "print it from the program to see it"
)


def bitfield_text(value, typed):
    """A char-typed bitfield's value as the number its bits hold, sign-extended where its
    type is signed, from LLDB's character (`'\\x05'`) or number, or None to keep the
    debugger's text (any other member). `<bitfield>` where LLDB's text cannot be read as
    one character, or the field is a wide character type's (``WIDE_BITFIELD``), whose
    bits LLDB reads wrongly."""
    if not typed.bits:
        return None
    if typed.wide_character():
        return "<bitfield>"
    scalar = typed.scalar()
    if scalar is None or scalar[1] not in _NARROW:
        return None
    if re.fullmatch(r"-?[0-9]{1,4}", str(value).strip()):
        return _bits(int(value), typed.bits, scalar[1] == "DW_ATE_signed_char")
    found = _CHARACTER.fullmatch(str(value))
    if found is None:
        return "<bitfield>"
    text = found[1]
    if len(text) == 1:
        code = ord(text)
    elif re.fullmatch(r"\\x[0-9a-fA-F]{1,2}", text):
        code = int(text[2:], 16)
    elif re.fullmatch(r"\\[0-7]{1,3}", text):
        code = int(text[1:], 8)
    elif len(text) == 2 and text[0] == "\\" and text[1] in _ESCAPES:
        code = _ESCAPES[text[1]]
    elif len(text) == 2 and text[0] == "\\" and text[1] in "'\"\\?":
        code = ord(text[1])
    else:
        return "<bitfield>"
    return _bits(code, typed.bits, scalar[1] == "DW_ATE_signed_char")


def _bits(code, bits, signed):
    """The number ``bits`` low bits of ``code`` hold, sign-extended where ``signed``."""
    code &= (1 << bits) - 1
    if signed and code >> (bits - 1):
        code -= 1 << bits
    return str(code)


class _Room:
    """The characters a preview has left, and whether it may show a standard
    container's elements (#181): ``containers``, a bool or a function asked only when a
    container is met. ``unlisted(at, child)``: whether an unordered container (``at``:
    the debugger's names from the preview's value to it) is shown as ``UNLISTED`` (#167),
    or (the container, a fetch) as read from its nodes (#360)."""

    def __init__(self, length, containers=True, unlisted=None):
        self.left, self.allows, self.unlisted = length, containers, unlisted

    @property
    def containers(self):
        return self.allows() if callable(self.allows) else self.allows

    def nested(self):
        return _Room(self.left, self.allows, self.unlisted)


class _Unsure(Exception):
    """The debugger's children do not match the type: no preview."""


def preview(variable, fetch, typed, text, containers=True, unlisted=None):
    """The preview of a C or C++ value, or None to keep the debugger's text: a struct,
    class, fixed-size array (a wide character array too) or standard container
    (`size=2 [1, 2]`, #181), or a C++ reference to one (`0x… {x = 1, y = 2}`, its
    address first, as for a string).

    ``typed``: its ``Typed``. ``fetch(reference, count)``: the first ``count`` children
    the debugger lists for a value; raises ValueError where it cannot (the preview
    budget is spent). ``text(child)``: a member's text as the worker shows it (a string
    checked and spelled), or None for no value. Without ``containers`` (a bool, or a
    function asked when a container is met), a standard container keeps the debugger's
    summary (`size=2`): a library call may be part-way through changing it.
    ``unlisted(at, child)``: whether an unordered container, the value itself (``at``
    empty) or one inside it, is withheld (#167): the value then has no preview, and one
    inside shows `<not listed>`, never the debugger's count. Or, in a tuple, (the
    container, a fetch for its entries) as Peeky read them from its nodes (#360), which
    are shown in place of the debugger's."""
    reference = variable.get("variablesReference")
    if type(reference) is not int or reference <= 0 or typed is None:
        return None
    value = str(variable.get("value") or "")
    prefix = ""
    if typed.kind() == "reference":
        located, target = _ADDRESS.match(value), typed.target()
        if located and target is not None and target.container() is not None:
            # A reference to a standard container: its address, then the debugger's
            # summary of the container (`0x… size=2`), if any (#181).
            summary = value[located.end() :].strip()
            shown = summary or f"{variable.get('type')} @ {located[0]}"
            prefix, typed, variable = located[0] + " ", target, {**variable, "value": shown}
        elif not _ADDRESS.fullmatch(value):
            return None
        else:
            prefix, typed = value + " ", target
        if typed is None:
            return None
    sort = typed.kind()
    if sort == "library" and typed.unordered() and unlisted is not None:
        held = unlisted((), variable)
        if isinstance(held, tuple):
            # Its entries as Peeky read them from its nodes (#360): listed as the
            # debugger lists a container it reads right; a multimap by its count.
            variable, fetch = held
            if typed.container() is None:
                return prefix + str(variable.get("value"))
        elif held:
            return None
    if sort == "library" and typed.container() is not None:
        # A standard container (#181): `size=3 [1, 2, 3]`.
        if not (containers() if callable(containers) else containers):
            return None
        room = _Room(LENGTH - len(prefix), unlisted=unlisted)
        try:
            shown = _container(variable, typed, fetch, text, 1, room, ())
        except (ValueError, _Unsure):
            return None
        return prefix + shown
    if sort == "array" and typed.narrow():
        # A char array: LLDB's literal, bounded by the array's size.
        return None
    if sort == "record" and not prefix and not _PLACEHOLDER.fullmatch(value):
        # A summary of the debugger's own (a formatter's): kept.
        return None
    if sort not in ("record", "array"):
        return None
    # Any other array is shown element by element, whatever the debugger's text: LLDB
    # ends a wide character array's (`wchar_t`, `char16_t`, `char32_t`) at its first NUL.
    try:
        room = _Room(LENGTH - len(prefix), containers, unlisted)
        shown = _open(variable, typed, fetch, text, 1, room, ())
    except (ValueError, _Unsure):
        # The budget is spent, or the debugger's children are not the type's.
        return None
    return None if shown is None else prefix + shown


def _open(variable, typed, fetch, text, depth, room, at):
    """`{name = value, ...}` for a struct, `[value, ...]` for an array. ``at``: the
    debugger's names from the preview's value to this one."""
    if typed.kind() == "array":
        return _elements(variable, typed, fetch, text, depth, room, at)
    members, bases = typed.members()
    children = fetch(variable["variablesReference"], MEMBERS)
    anonymous = [typed for name, typed in members if not name and not typed.bits]
    shown, cut = [], len(children) >= MEMBERS
    for child in children:
        name = str(child.get("name") or "")
        # An anonymous struct or union: lldb-dap 18 names it `<null>`, 20 and later "".
        name = "" if name == "<null>" else name
        reference = child.get("variablesReference")
        if not name and not (type(reference) is int and reference > 0):
            # An unnamed bitfield LLDB adds to fill a gap: not a member of the program's.
            continue
        if not name:
            if not anonymous:
                raise _Unsure()
            member = anonymous.pop(0)
        else:
            member = typed.child(name)
            if member is None:
                raise _Unsure()
        if len(shown) == ITEMS:
            cut = True
            break
        rendered = _member(child, member, fetch, text, depth, room, (*at, name))
        item = rendered if not name else f"{name} = {rendered}"
        if len(item) + 2 > room.left:
            cut = True
            break
        room.left -= len(item) + 2
        shown.append(item)
    if not children and (members or bases):
        # A defined type the debugger lists nothing of.
        raise _Unsure()
    return "{" + ", ".join(shown + (["…"] if cut else [])) + "}"


def _elements(variable, typed, fetch, text, depth, room, at):
    count = typed.count()
    if count is None:
        raise _Unsure()
    wanted = min(count, ITEMS)
    children = fetch(variable["variablesReference"], wanted) if wanted else []
    names = [str(child.get("name")) for child in children]
    if names != [f"[{index}]" for index in range(wanted)]:
        # The debugger lists other elements than the type holds.
        raise _Unsure()
    element = typed.element()
    shown, cut = [], count > wanted
    for child in children:
        item = _member(child, element, fetch, text, depth, room, (*at, str(child.get("name"))))
        if len(item) + 2 > room.left:
            cut = True
            break
        room.left -= len(item) + 2
        shown.append(item)
    return "[" + ", ".join(shown + (["…"] if cut else [])) + "]"


def _container(variable, typed, fetch, text, depth, room, at):
    """`size=N [e0, e1, …]` of a standard sequence, `size=N {k0: v0, …}` of a map and
    `size=N {e0, …}` of a set (#181), in the order LLDB lists them (a map's or set's
    iteration order), from the debugger's children, which its formatter reads from the
    container's own storage, `size()` of them: [0], [1], ... Each is shown as the class
    template's argument in the build's debug information says (`_Tp`). A `std::array`
    LLDB lists by its one member, the C array, shows that array's elements."""
    found = typed.container()
    value = str(variable.get("value") or "")
    size = _SIZE.fullmatch(value)
    if size is None:
        member = typed.array_member()
        if member is None or not _PLACEHOLDER.fullmatch(value):
            raise _Unsure()
        children = fetch(variable["variablesReference"], 2)
        if [str(child.get("name")) for child in children] != [member[0]]:
            raise _Unsure()
        reference = children[0].get("variablesReference")
        if type(reference) is not int or reference <= 0:
            raise _Unsure()
        head = f"size={member[1].count()} "
        room.left -= len(head)
        return head + _elements(children[0], member[1], fetch, text, depth, room, (*at, member[0]))
    count = int(size[1])
    wanted = min(count, ITEMS)
    children = fetch(variable["variablesReference"], wanted) if wanted else []
    if [str(child.get("name")) for child in children] != [f"[{at}]" for at in range(wanted)]:
        # Not the formatter's elements (the class's own members), or fewer than it says.
        raise _Unsure()
    head = f"size={count} "
    room.left -= len(head)
    shown, cut = [], count > wanted
    for child in children:
        place = (*at, str(child.get("name")))
        if found[0] == "map":
            item = _entry(child, found[1], found[2], fetch, text, depth, room, place)
        else:
            item = _member(child, found[1], fetch, text, depth, room, place)
        if len(item) + 2 > room.left:
            cut = True
            break
        room.left -= len(item) + 2
        shown.append(item)
    opening, closing = "[]" if found[0] == "sequence" else "{}"
    return head + opening + ", ".join(shown + (["…"] if cut else [])) + closing


def _entry(child, key, value, fetch, text, depth, room, at):
    """`key: value` of one entry LLDB lists of a map: a pair of `first` and `second`."""
    reference = child.get("variablesReference")
    if type(reference) is not int or reference <= 0:
        raise _Unsure()
    parts = fetch(reference, 3)
    if [str(part.get("name")) for part in parts] != ["first", "second"]:
        raise _Unsure()
    shown = _member(parts[0], key, fetch, text, depth, room, (*at, "first"))
    return f"{shown}: {_member(parts[1], value, fetch, text, depth, room, (*at, 'second'))}"


def _member(child, typed, fetch, text, depth, room, at):
    """One member's or element's text (``at``: the debugger's names from the preview's
    value to it)."""
    from peeky.worker import is_unavailable

    value = str(child.get("value") or "")
    if is_unavailable(value):
        return "<no value>"
    sort = typed.kind()
    if sort == "library" and typed.unordered() and room.unlisted:
        held = room.unlisted(at, child)
        if isinstance(held, tuple):
            # Its entries as Peeky read them from its nodes (#360).
            child, fetch = held
            value = str(child.get("value") or "")
        elif held:
            # Its count is not proven (#167): neither its summary nor its elements.
            return UNLISTED
    if sort == "union":
        return "<union>"
    if sort in ("pointer", "reference"):
        # Never followed: what it points at may be gone.
        address = _ADDRESS.match(value)
        return address[0] if address else "<pointer>"
    reference = child.get("variablesReference")
    expandable = type(reference) is int and reference > 0
    if sort == "array":
        if typed.count() is None:
            # A variable-length array: the debugger cannot tell its length.
            return "[…]"
        if typed.narrow() and not _PLACEHOLDER.fullmatch(value):
            return shorten(text(child) or "<no value>", TEXT)
        if not expandable:
            return "[…]"
        if depth >= DEPTH:
            return "[…]"
        return _open(child, typed, fetch, text, depth + 1, room.nested(), at)
    if sort == "record":
        if not expandable or not _PLACEHOLDER.fullmatch(value) or depth >= DEPTH:
            return "{…}"
        return _open(child, typed, fetch, text, depth + 1, room.nested(), at)
    if (
        sort == "library"
        and expandable
        and depth < DEPTH
        and typed.container() is not None
        and room.containers
    ):
        # A standard container inside (#181): its elements, or else its summary.
        try:
            return _container(child, typed, fetch, text, depth + 1, room.nested(), at)
        except _Unsure:
            pass
    if sort in ("library", "incomplete", "unknown") and _PLACEHOLDER.fullmatch(value):
        return "{…}"
    if sort == "scalar" and expandable and _PLACEHOLDER.fullmatch(value):
        # A struct or array where the type says a number: not this member.
        raise _Unsure()
    if (number := bitfield_text(value, typed)) is not None:
        return number
    shown = text(child)
    scalar = typed.scalar()
    if shown and scalar and scalar[2] in (2, 4) and (literal := _WIDE_LITERAL.search(shown)):
        # A wide character, which LLDB writes after its code or bytes (`97 L'a'`,
        # `U+0078 U+0078 u'x'`): its literal alone.
        shown = literal[0]
    return "<no value>" if shown is None else shorten(shown, TEXT)
