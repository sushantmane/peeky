"""A C or C++ character array's text, up to its first NUL.

LLDB shows a `char[N]` (`signed char`, `unsigned char`, a `char8_t` typedef) as one
literal of the bytes it read: the first N, at most SUMMARY_UNITS (1024, where it adds
`...`). It drops the trailing NULs (`CopyStringDataToBufferSP`'s `rtrim('\\0')`), and
writes a NUL inside as `\\0`, a byte that starts no UTF-8 character as `\\xNN` and a
control character as `\\U000000NN`. So a `char name[32]` holding "Adeyemi", a NUL and
leftovers shows `"Adeyemi\\0\\x80\\xf8\\xe6\\xef\\U00000001..."`: the leftovers read as part
of the text, and a byte has two spellings.

LLDB's summary also formats whatever part of its read succeeded, and the DAP value
says nothing of how much that was: an array that runs into an unreadable page shows the
bytes before it, as if NULs followed. So Peeky takes the bytes the literal shows
(``summary_bytes``: every escape LLDB writes stands for one byte or one UTF-8 character,
and Peeky's lldb-dap sources no init file, so its escaping and its limit are LLDB's
defaults), and proves the bytes after them itself (``proven_rest``): read from memory,
every byte, or as the debugger lists the array's elements. It shows the text before the
first NUL, then a note of what follows it, never as text:

- `"Adeyemi" (then a NUL and 24 bytes not shown)`: a proven NUL, then bytes Peeky doesn't
  show (not all NULs, or not all read);
- `"hello" (then 59 NUL bytes)`: every byte after the text is a NUL, each one read;
- `"abcdefgh" (no NUL in its 8 bytes)`: every byte read, none a NUL;
- `"abcdef" (then 10 bytes not read)`: no NUL among the bytes read, and the rest unread.

A printable UTF-8 character shows as itself; any other byte as `\\xNN` (or the escape C
gives it: `\\n`, `\\t`), spelled as C reads it back (``cxx_strings._spell``). Every
proven byte, NULs included, is kept as ``buffer``, which ends `...` where the array holds
more. Where LLDB read only the first 1024 bytes and shows no NUL among them, the text
ends `...`, as LLDB's did, and has no note.
"""

import re

from peeky.cxx_strings import _ESCAPES, SUMMARY_UNITS, _lex, _literal, _printable
from peeky.text_sets import parse_char

# A one-dimensional narrow character array, as LLDB names its type.
NARROW_ARRAY = re.compile(
    r"(?:(?:const|volatile)\s+)*(?:(?:signed|unsigned)\s+)?(?:char|char8_t)"
    r"(?:\s+(?:const|volatile))*\s*\[([0-9]{1,9})\]"
)
# The notes ``shown`` writes after the text, which text() reads back.
NOTE = re.compile(
    r" \((?:then a NUL and ([0-9]+) bytes? not shown|then ([0-9]+) NUL bytes?"
    r"|no NUL in its ([0-9]+) bytes?)\)"
)
# The characters a value's literal keeps, with room for its note within the worker's 2000.
_SHOWN = 1700
# The elements, at most, that prove an array's bytes after its summary where the debugger
# reads no memory: one request.
ELEMENTS = 64
_REFERENCE = re.compile(r"0x[0-9a-fA-F]{1,16}")


def array_size(kind):
    """N for a narrow character array's type name (`char[32]`, `const unsigned
    char[8]`), else None."""
    found = NARROW_ARRAY.fullmatch(str(kind or "").strip())
    return int(found[1]) if found else None


def _lldb_printable(code):
    """Whether LLDB shows the code point as itself in a summary (its `isprint32`)."""
    return not (
        code <= 0x1F
        or 0x7F <= code <= 0x9F
        or code in (0x2028, 0x2029, 0x200E, 0x200F)
        or 0x202A <= code <= 0x202E
        or 0xFFF9 <= code <= 0xFFFF
    )


def summary_bytes(text):
    """(the bytes, whether LLDB cut them at its limit) of LLDB's summary of a narrow
    character array (`"a\\0\\xff\\U00000001"...`), or None for any other text, or a
    character LLDB would have escaped, shown as itself."""
    text = str(text or "")
    lexed = _lex(text)
    if lexed is None:
        return None
    prefix, tokens, capped, end = lexed
    if prefix or end != len(text):
        return None
    data = bytearray()
    for spelling, code, raw in tokens:
        if code is None:
            data.append(int(spelling[2:], 16))
            continue
        if raw and not _lldb_printable(code):
            return None
        try:
            data += chr(code).encode("utf-8")
        except UnicodeEncodeError:
            return None
    return bytes(data), capped


def _tokens(data):
    """Each piece of ``data`` as a C literal spells it: a printable UTF-8 character as
    itself, `\\"`, `\\\\` and C's own escapes (`\\0`, `\\n`), else each byte as `\\xNN`."""
    at = 0
    while at < len(data):
        lead = data[at]
        if lead in _ESCAPES:
            yield _ESCAPES[lead], None, False
            at += 1
            continue
        size = 1 if lead < 0x80 else 2 if lead >> 5 == 6 else 3 if lead >> 4 == 14 else 4
        try:
            char = data[at : at + size].decode("utf-8")
        except UnicodeDecodeError:
            char = ""
        if len(char) == 1 and _printable(ord(char)):
            yield char, None, True
            at += size
        else:
            yield f"\\x{lead:02x}", None, False
            at += 1


def literal(data, capped=False, room=_SHOWN):
    """``data`` as a C literal (see ``_tokens``), within ``room`` characters (by default,
    what a value keeps); `...` after it where ``capped`` or cut here."""
    return _literal("", list(_tokens(data)), capped, room)


def _count(number, unit):
    return f"{number} {unit}" + ("" if number == 1 else "s")


def proven_rest(variable, known, window, fetch, read):
    """The bytes of a narrow character array (the DAP ``variable``) after ``known``, the
    bytes its summary shows, up to ``window``, as far as Peeky proves them: from memory,
    where ``read(reference, count)`` is given and returns every byte, which must agree
    with ``known``; else from the elements the debugger lists (``fetch(reference,
    start)``: up to 64 from ``start``), each a char literal, up to ELEMENTS of them. What
    a read can't give proves nothing: an unreadable element ends them."""
    start = len(known)
    reference = variable.get("memoryReference")
    if read is not None and isinstance(reference, str) and _REFERENCE.fullmatch(reference):
        try:
            data = read(reference, window)
        except ValueError:
            # A partial read, a refusal or the spent budget.
            data = None
        if isinstance(data, bytes) and len(data) == window:
            return data[start:] if data[:start] == known else b""
    raw = variable.get("variablesReference")
    if fetch is None or type(raw) is not int or raw <= 0:
        return b""
    try:
        listed = fetch(raw, start)
    except ValueError:
        return b""
    found = bytearray()
    for index, child in enumerate(listed[: min(ELEMENTS, window - start)], start):
        named = isinstance(child, dict) and child.get("name") == f"[{index}]"
        byte = parse_char(child.get("value")) if named else None
        if byte is None:
            break
        found.append(byte)
    return bytes(found)


def shown(text, size, rest=None):
    """{"value": the text before the first NUL and a note, "buffer": every byte proven,
    as a literal} for LLDB's summary ``text`` of a narrow character array of ``size``
    bytes; None where it is not such a summary, or does not fit the size. ``rest(known,
    window)``: the bytes after ``known`` up to ``window`` that Peeky proves
    (``proven_rest``), else none are."""
    if size < 1:
        return None
    read = summary_bytes(text)
    if read is None:
        return None
    data, capped = read
    if capped:
        # LLDB read its first SUMMARY_UNITS bytes at most.
        if size <= SUMMARY_UNITS or len(data) > SUMMARY_UNITS:
            return None
        window = SUMMARY_UNITS
    else:
        if size > SUMMARY_UNITS or len(data) > size:
            return None
        window = size
    known = data
    if len(data) < window and rest is not None:
        # The summary ends where LLDB's read did, or where NULs began: not told apart.
        known += rest(data, window)[: window - len(data)]
    nul = known.find(b"\0")
    whole = len(known) == size
    # At most 4 characters a byte, so never cut.
    buffer = literal(known, not whole, 4 * len(known) + 8)
    if nul < 0 and len(known) < window:
        note = f"then {_count(size - len(known), 'byte')} not read"
    elif nul < 0 and capped:
        # No NUL in the first 1024 bytes: the rest was not read.
        return {"value": literal(known, True), "buffer": buffer}
    elif nul < 0:
        note = f"no NUL in its {_count(size, 'byte')}"
    elif whole and not any(known[nul:]):
        note = f"then {_count(size - nul, 'NUL byte')}"
    else:
        note = f"then a NUL and {_count(size - nul - 1, 'byte')} not shown"
    return {"value": f"{literal(known if nul < 0 else known[:nul])} ({note})", "buffer": buffer}
