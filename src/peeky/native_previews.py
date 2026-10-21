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

import re

from peeky.cxx_strings import shorten

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


class Typed:
    """What the build's debug information says a value is: a type in
    ``graph`` (fields.type_graph), with the array dimensions still to index (a
    multi-dimensional array is one type) and, for a bitfield member, its width."""

    def __init__(self, graph, offset, dims=None, bits=None):
        self.graph, self.offset, self.dims, self.bits = graph, offset, dims, bits

    def node(self):
        """The type under typedefs and qualifiers, or None if the walk did not read it."""
        offset = self.offset
        for _ in range(64):
            node = self.graph.get(offset)
            if node is None or node[0] not in ("typedef", "qualifier"):
                return node
            offset = node[2]
        return None

    def kind(self):
        """ "record", "union", "array", "pointer", "reference", "scalar", "library",
        "incomplete" or "unknown"."""
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
        class), or None: the steps of a selector's path."""
        sort = self.kind()
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
            if len(named) == 1:
                return named[0]
            based = [typed for base, typed in bases if base is not None and _names(name, base)]
            return based[0] if len(based) == 1 and not named else None
        return None


def _names(shown, base):
    """Whether lldb-dap's name for a base-class child (`Base`, `ns::Base`, `Base<int>`)
    is the class ``base`` (its debug information's name)."""
    return shown == base or str(shown).endswith("::" + base)


def bitfield_text(value, typed):
    """A char-typed bitfield's value as the number its bits hold, sign-extended where its
    type is signed, from LLDB's character (`'\\x05'`), or None to keep the debugger's
    text (any other member). None where LLDB's text cannot be read as one character."""
    scalar = typed.scalar() if typed.bits else None
    if scalar is None or scalar[1] not in _NARROW:
        return None
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
    bits = typed.bits
    code &= (1 << bits) - 1
    if scalar[1] == "DW_ATE_signed_char" and code >> (bits - 1):
        code -= 1 << bits
    return str(code)


class _Room:
    """The characters a preview has left."""

    def __init__(self, length):
        self.left = length


class _Unsure(Exception):
    """The debugger's children do not match the type: no preview."""


def preview(variable, fetch, typed, text):
    """The preview of a C or C++ value, or None to keep the debugger's text: a struct,
    class or fixed-size array (a wide character array too), or a C++ reference to one
    (`0x… {x = 1, y = 2}`, its address first, as for a string).

    ``typed``: its ``Typed``. ``fetch(reference, count)``: the first ``count`` children
    the debugger lists for a value; raises ValueError where it cannot (the preview
    budget is spent). ``text(child)``: a member's text as the worker shows it (a string
    checked and spelled), or None for no value."""
    reference = variable.get("variablesReference")
    if type(reference) is not int or reference <= 0 or typed is None:
        return None
    value = str(variable.get("value") or "")
    prefix = ""
    if typed.kind() == "reference":
        if not _ADDRESS.fullmatch(value):
            return None
        prefix, typed = value + " ", typed.target()
        if typed is None:
            return None
    sort = typed.kind()
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
        shown = _open(variable, typed, fetch, text, 1, _Room(LENGTH - len(prefix)))
    except (ValueError, _Unsure):
        # The budget is spent, or the debugger's children are not the type's.
        return None
    return None if shown is None else prefix + shown


def _open(variable, typed, fetch, text, depth, room):
    """`{name = value, ...}` for a struct, `[value, ...]` for an array."""
    if typed.kind() == "array":
        return _elements(variable, typed, fetch, text, depth, room)
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
        rendered = _member(child, member, fetch, text, depth, room)
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


def _elements(variable, typed, fetch, text, depth, room):
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
        item = _member(child, element, fetch, text, depth, room)
        if len(item) + 2 > room.left:
            cut = True
            break
        room.left -= len(item) + 2
        shown.append(item)
    return "[" + ", ".join(shown + (["…"] if cut else [])) + "]"


def _member(child, typed, fetch, text, depth, room):
    """One member's or element's text."""
    from peeky.worker import is_unavailable

    value = str(child.get("value") or "")
    if is_unavailable(value):
        return "<no value>"
    sort = typed.kind()
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
        return _open(child, typed, fetch, text, depth + 1, _Room(room.left))
    if sort == "record":
        if not expandable or not _PLACEHOLDER.fullmatch(value) or depth >= DEPTH:
            return "{…}"
        return _open(child, typed, fetch, text, depth + 1, _Room(room.left))
    if sort in ("library", "incomplete", "unknown") and _PLACEHOLDER.fullmatch(value):
        return "{…}"
    if (number := bitfield_text(value, typed)) is not None:
        return number
    shown = text(child)
    scalar = typed.scalar()
    if shown and scalar and scalar[2] in (2, 4) and (literal := _WIDE_LITERAL.search(shown)):
        # A wide character, which LLDB writes after its code or bytes (`97 L'a'`,
        # `U+0078 U+0078 u'x'`): its literal alone.
        shown = literal[0]
    return "<no value>" if shown is None else shorten(shown, TEXT)
