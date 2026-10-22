"""The names a selector can write (#195): a Unicode identifier, matched exactly as the
debugger lists it.

A name is Python's identifier (`str.isidentifier()`: XID_Start or `_`, then
XID_Continue), with no invisible character: no control or format character (U+200B,
U+200D, bidi controls) and nothing Unicode calls default-ignorable (U+3164, variation
selectors), some of which `str.isidentifier()` accepts. It never holds an operator,
quote or space; a name a compiler accepts beyond these (`$`, Java's `€`, U+200D) is
not selectable.

Names are never normalized: compilers differ (Python applies NFKC, Rust NFC, while
javac and clang keep NFC and NFD spellings as two variables), so a name selects only
the variable the debugger lists with exactly those code points. A spelling that differs
only by normalization is refused with the listed spelling (`twin_hint`).
"""

import re
import unicodedata

MAX_NAME = 128

# Default_Ignorable_Code_Point (Unicode's DerivedCoreProperties.txt).
_IGNORABLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
    "\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)


def _part(char, first):
    if _IGNORABLE.match(char) or unicodedata.category(char)[0] == "C":
        return False
    return char.isidentifier() if first else ("_" + char).isidentifier()


def name_end(text, start=0, limit=MAX_NAME):
    """Where the longest name at ``start`` of ``text`` ends (``start`` if none starts
    there): at most ``limit`` characters (None: no limit). For ASCII text this is
    `[A-Za-z_][A-Za-z0-9_]*`."""
    end = start
    while (
        end < len(text)
        and (limit is None or end - start < limit)
        and _part(text[end], end == start)
    ):
        end += 1
    return end


def is_name(text, limit=MAX_NAME):
    """Whether ``text`` is a whole name a selector can write."""
    return isinstance(text, str) and bool(text) and name_end(text, 0, limit) == len(text)


def is_located(text, place, limit=MAX_NAME):
    """Whether ``text`` is a name followed by where LLDB says it is declared
    (`s @ main.rs:81`, when several of that name are in scope): the rest of ``text``
    after the name fullmatches the regex ``place``."""
    if not isinstance(text, str) or not (end := name_end(text, 0, limit)):
        return False
    return re.fullmatch(place, text[end:]) is not None


# Where LLDB says a local is declared (`s @ main.rs:81`), as `parse_selector` writes it
# back: a name listed so is one a selector reads whole.
_LISTED_PLACE = re.compile(r" @ [^\s:@\[\]'\"]{1,255}:(?:0|[1-9][0-9]{0,8})")
# An element's index as a debugger names it: LLDB `[3]`, java-debug `3`.
_INDEX = re.compile(r"\[(?:0|[1-9][0-9]{0,9})\]|0|[1-9][0-9]{0,9}")


# Where LLDB says a local is declared, as LLDB writes it: any file name with no colon
# (`t @ my file.c:3`), any line (`#line 1000000000`): what Peeky's judgments read.
_ANY_PLACE = re.compile(r" @ ([^:]+):([0-9]+)")
# The same within the bounds a selector or a change may name (`changeable`).
LLDB_PLACE = r" @ ([^:]{1,255}):([0-9]{1,9})"
# A row that looks so: ` @ ` in it, and a line at its end.
_LOOKS_LOCATED = re.compile(r" @ .*:[0-9]+\Z", re.S)


def selectable(name):
    """Whether a row the debugger lists as ``name`` is one a selector reads whole, as
    `parse_selector` reads it: a name, or one with where LLDB says it is declared."""
    return is_name(name) or is_located(name, _LISTED_PLACE)


def split_located(name):
    """``(base, file, line)`` of a name LLDB gives with where it is declared, as LLDB
    writes it (`t @ my file.c:3`), else None: the one parser of such names. The base is
    all before the first ` @ ` and holds no space, as no name does, so `t @ one @ two.c:5`
    is `t` in `one @ two.c` everywhere; never a greedy prefix. Any file name with no colon
    and any line parse (`#line 1000000000`, a long `#line` file name), and so does a base
    no selector writes (`a$b`, one past 128 characters): Peeky's judgments read what the
    debugger lists. Whoever selects or changes applies a selector's bounds
    (``changeable``). A row that ``looks_located`` and does not parse is withheld."""
    if not isinstance(name, str):
        return None
    base, at, _ = name.partition(" @ ")
    if not at or not base or any(char.isspace() for char in base):
        return None
    found = _ANY_PLACE.fullmatch(name, len(base))
    return (base, found[1], int(found[2])) if found else None


def looks_located(name):
    """Whether ``name`` looks like one LLDB gives with where it is declared (` @ ` in it,
    a line at its end), whether or not ``split_located`` reads it."""
    return isinstance(name, str) and _LOOKS_LOCATED.search(name) is not None


def _bounded(name):
    """``split_located``'s base, where ``name`` is a name with where it is declared
    within a selector's bounds (``LLDB_PLACE``), else None."""
    found = split_located(name)
    if found is None or not is_name(found[0]):
        return None
    return found[0] if re.fullmatch(LLDB_PLACE, name[len(found[0]) :]) else None


def reachable(row):
    """Whether a selector reads the root row ``row`` whole: by its own name
    (``selectable``), or by the plain name the worker gives it (``selected_as``, #158)
    where the row is that name with where LLDB says it is declared (`t` for
    `t @ my file.c:3`). What `inspect` offers, and what alone may be marked [settable]."""
    name, alias = row.get("name"), row.get("selected_as")
    if selectable(name):
        return True
    return is_name(alias) and _bounded(name) == alias


def changeable(name):
    """Whether a root local's ``name`` is one a change may give the debugger: a name, or
    one LLDB lists with where it is declared (``split_located``) whose base is a name,
    within a selector's bounds (``LLDB_PLACE``: a file name of at most 255 characters, a
    line of at most nine digits), which a selector reaches through its plain name
    (``reachable``). The debugger's rows must still list exactly that name."""
    return is_name(name) or _bounded(name) is not None


def member_name(name):
    """Whether ``name`` is a field or element a change can name: a name, or an
    element's index as the debugger names it (`[3]`, `3`)."""
    return is_name(name) or (isinstance(name, str) and _INDEX.fullmatch(name) is not None)


def invisible(text):
    """The first invisible character in ``text`` (format or default-ignorable, never
    ASCII) as `U+200B`, else None."""
    for char in text:
        if _IGNORABLE.match(char) or unicodedata.category(char) == "Cf":
            return f"U+{ord(char):04X}"
    return None


def look_alike(listed):
    """The names in ``listed`` that differ from another of them only by Unicode
    normalization (`größe` in NFC and in NFD, which javac and clang keep apart)."""
    groups = {}
    for name in listed:
        if isinstance(name, str):
            base = name.partition(" @ ")[0]
            groups.setdefault(unicodedata.normalize("NFKC", base), set()).add(base)
    return {
        name
        for name in listed
        if isinstance(name, str)
        and len(groups[unicodedata.normalize("NFKC", name.partition(" @ ")[0])]) > 1
    }


def twin_hint(name, listed):
    """`; the debugger lists ...` when names in ``listed`` differ from ``name`` only by
    Unicode normalization (NFC, NFD or NFKC), else "": each spelled with escapes, since
    the two look alike."""
    if not isinstance(name, str):
        return ""
    wanted = unicodedata.normalize("NFKC", name)
    twins = sorted(
        {
            other
            for other in listed
            if isinstance(other, str)
            and (base := other.partition(" @ ")[0]) != name
            and unicodedata.normalize("NFKC", base) == wanted
        }
    )
    if not twins:
        return ""
    shown = ", ".join(f"{twin!r} (written {ascii(twin)})" for twin in twins[:4])
    return (
        f"; the debugger lists {shown}, which {'differs' if len(twins) == 1 else 'differ'} "
        f"from {ascii(name)} only by Unicode normalization: names are matched exactly as "
        "the debugger lists them (Peeky never normalizes one), so select "
        f"{'it' if len(twins) == 1 else 'one'} as listed if that is the one you mean"
    )
