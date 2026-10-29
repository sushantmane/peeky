"""A C or C++ character array's text, up to its first NUL (thirteenth evaluation).

LLDB shows a `char[N]` (`signed char`, `unsigned char`, a `char8_t` typedef) as one
literal of the bytes it read: the first N, at most SUMMARY_UNITS (1024, where it adds
`...`). It drops the trailing NULs (`CopyStringDataToBufferSP`'s `rtrim('\\0')`), and
writes a NUL inside as `\\0`, a byte that starts no UTF-8 character as `\\xNN` and a
control character as `\\U000000NN`. So a `char name[32]` holding "Adeyemi", a NUL and
leftovers showed `"Adeyemi\\0\\x80\\xf8\\xe6\\xef\\U00000001..."`: the leftovers read as part
of the text, and a byte had two spellings.

Peeky reads the bytes back from that literal (``summary_bytes``: every escape LLDB
writes stands for one byte or one UTF-8 character, and Peeky's lldb-dap sources no
init file, so its escaping and its limit are LLDB's defaults). It shows the text before
the first NUL, then a note of what follows it, never as text:

- `"Adeyemi" (then a NUL and 24 bytes not shown)`: some byte after the NUL is not one, or
  LLDB did not read them all;
- `"hello" (then 59 NUL bytes)`: every byte after the text is a NUL;
- `"abcdefgh" (no NUL in its 8 bytes)`.

A printable UTF-8 character shows as itself; any other byte as `\\xNN` (or the escape C
gives it: `\\n`, `\\t`), spelled as C reads it back (``cxx_strings._spell``). Every byte
read, NULs included, is kept as ``buffer``. Where LLDB read only the first 1024 bytes and
shows no NUL among them, the text ends `...`, as LLDB's did, and has no note.
"""

import re

from peeky.cxx_strings import _ESCAPES, SUMMARY_UNITS, _lex, _literal, _printable

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


def shown(text, size):
    """{"value": the text before the first NUL and a note, "buffer": every byte LLDB
    read, as a literal} for LLDB's summary ``text`` of a narrow character array of
    ``size`` bytes; None where it is not such a summary, or does not fit the size."""
    if size < 1:
        return None
    read = summary_bytes(text)
    if read is None:
        return None
    data, capped = read
    if capped:
        # LLDB read its first SUMMARY_UNITS bytes, and only those.
        if size <= SUMMARY_UNITS or len(data) > SUMMARY_UNITS:
            return None
        window = SUMMARY_UNITS
    else:
        if size > SUMMARY_UNITS or len(data) > size:
            return None
        window = size
    # At most 4 characters a byte, so never cut: at most SUMMARY_UNITS bytes were read.
    buffer = literal(data + bytes(window - len(data)), capped, 4 * window + 8)
    nul = data.find(b"\0")
    if nul < 0 and len(data) == window:
        if capped:
            # No NUL in the first 1024 bytes: the rest was not read.
            return {"value": literal(data, True), "buffer": buffer}
        note = f"no NUL in its {_count(size, 'byte')}"
    elif nul < 0 and not capped:
        # LLDB dropped only NULs after the text.
        note = f"then {_count(size - len(data), 'NUL byte')}"
    else:
        # A byte after the NUL is not one (else LLDB would have dropped it), or was not
        # read.
        nul = len(data) if nul < 0 else nul
        note = f"then a NUL and {_count(size - nul - 1, 'byte')} not shown"
    return {"value": f"{literal(data if nul < 0 else data[:nul])} ({note})", "buffer": buffer}
