"""Previews of C and C++ structs, classes and fixed-size arrays (#136).

lldb-dap shows a struct, class, union or array that has no summary of its own as its
type and address (`bin[4] @ 0x16bb9f278`), so an agent had to list every field by hand.
A preview is written from the debugger's own children, in the C-like form LLDB's
`frame variable` uses: `{lo = 0, hi = 2, count = 0}`, an array `[5, 4, 3]`, an array of
structs `[{lo = 0, hi = 2, count = 0}, {...}]`, a 2-D array `[[1, 2, 3], [4, 5, 6]]`.

Bounded as the Java and Rust previews are: ``ITEMS`` members or elements per level,
then `…`; ``DEPTH`` levels opened (deeper ones show `{…}` or `[…]`); ``TEXT``
characters of one member (a string cut between characters with LLDB's `...`, its note
kept whole); ``LENGTH`` characters in all, cut between members. Each level is one
`variables` request, counted against the worker's preview budget.

Nothing is guessed: a union is not previewed (`<union>` inside another value: only one
member holds what was stored); a pointer, or a reference, inside shows its address and
is never followed; a member with no value shows `<no value>`; a standard-library value
keeps the debugger's summary (`size=2`, a string's text, checked as the worker checks
every string, #128) or shows `{…}`; the vptr is left out; a base class shows as a
member named by its class (`{Base = {b = 1}, d = 2}`), as LLDB lists it; an anonymous
struct shows its members in braces, with no name.
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
_INDEX = re.compile(r"\[(?:0|[1-9][0-9]*)\]")
# A dynamic class's hidden pointer to its vtable (`_vptr$Base`, GCC's `_vptr.Base`).
_VPTR = re.compile(r"_vptr[.$]|__vptr")
_ANONYMOUS_UNION = re.compile(r"\((?:anonymous|unnamed) union\b")
_COUNT = re.compile(r"\[([0-9]{1,9})\]")


class _Room:
    """The characters a preview has left."""

    def __init__(self, length):
        self.left = length


def preview(variable, fetch, kind, text):
    """The preview of a C or C++ value, or None to keep the debugger's text: a struct,
    class or fixed-size array lldb-dap shows only as `Type @ 0x…`, or a C++ reference
    to one (`0x… {x = 1, y = 2}`, its address first, as for a string).

    ``fetch(reference, count)``: the first ``count`` children the debugger lists for a
    value; raises ValueError where it cannot (the preview budget is spent).
    ``kind(type)``: "union", "library", "pointer", "reference", "unknown" or None for a
    type name, as the build's debug information tells it. ``text(child)``: a member's
    text as the worker shows it (a string checked and spelled), or None for no value."""
    reference = variable.get("variablesReference")
    if type(reference) is not int or reference <= 0:
        return None
    value = str(variable.get("value") or "")
    sort = kind(variable.get("type"))
    if sort == "reference" and _ADDRESS.fullmatch(value):
        prefix = value + " "
    elif sort is None and _PLACEHOLDER.fullmatch(value):
        prefix = ""
    else:
        return None
    try:
        shown = _open(variable, fetch, kind, text, 1, _Room(LENGTH - len(prefix)))
    except ValueError:
        # The budget is spent, or the debugger cannot list it: its own text stays.
        return None
    return None if shown is None else prefix + shown


def _array(kind_name):
    return str(kind_name or "").rstrip().endswith("]")


def _elements(kind_name):
    """How many elements an array type holds (`bin[4]`: 4; `int[2][3]`: 2), or None."""
    found = _COUNT.search(str(kind_name or ""))
    return int(found[1]) if found else None


def _open(variable, fetch, kind, text, depth, room):
    """`{name = value, ...}` for a struct, `[value, ...]` for an array."""
    array = _array(variable.get("type"))
    total = _elements(variable.get("type")) if array else None
    wanted = min(total, ITEMS) if total is not None else ITEMS + 1 if array else MEMBERS
    children = fetch(variable["variablesReference"], max(wanted, 0)) if wanted else []
    if array and not all(_INDEX.fullmatch(str(c.get("name"))) for c in children):
        # Not elements as lldb-dap lists them: not read as an array.
        return None
    if not array and any(str(c.get("name", "")).startswith("*") for c in children):
        # What a pointer points at (`*p`), a scalar: no struct to show.
        return None
    shown, cut = [], False
    for child in children:
        name = str(child.get("name") or "")
        # An anonymous struct or union: lldb-dap 18 names it `<null>`, 20 and later "".
        name = "" if name == "<null>" else name
        if not array and _VPTR.match(name):
            continue
        if len(shown) == ITEMS:
            cut = True
            break
        member = _member(child, fetch, kind, text, depth, room)
        item = member if array or not name else f"{name} = {member}"
        if len(item) + 2 > room.left:
            cut = True
            break
        room.left -= len(item) + 2
        shown.append(item)
    if array:
        cut = cut or len(children) > len(shown) or (total is not None and total > len(shown))
    else:
        cut = cut or len(children) >= MEMBERS
    body = ", ".join(shown + (["…"] if cut else []))
    return f"[{body}]" if array else f"{{{body}}}"


def _member(child, fetch, kind, text, depth, room):
    """One member's or element's text."""
    from peeky.worker import is_unavailable

    value = str(child.get("value") or "")
    kind_name = child.get("type")
    if is_unavailable(value):
        return "<no value>"
    sort = kind(kind_name)
    if sort == "union" or _ANONYMOUS_UNION.search(str(kind_name or "")):
        return "<union>"
    if sort in ("pointer", "reference") or _ADDRESS.match(value):
        # Never followed: what it points at may be gone.
        address = _ADDRESS.match(value)
        return address[0] if address else "<pointer>"
    if str(kind_name or "").rstrip().endswith("[]"):
        # A flexible array member: the debugger knows no length for it.
        return "[…]"
    reference = child.get("variablesReference")
    expandable = type(reference) is int and reference > 0
    if expandable and _PLACEHOLDER.fullmatch(value):
        opened = "[…]" if _array(kind_name) else "{…}"
        if sort is not None or depth >= DEPTH:
            return opened
        # Bounded by what is left; the caller counts what it takes.
        return _open(child, fetch, kind, text, depth + 1, _Room(room.left)) or opened
    shown = text(child)
    return "<no value>" if shown is None else shorten(shown, TEXT)
