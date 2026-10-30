"""libstdc++ `std::basic_string` values as LLDB's libc++ summary shows them.

LLDB's summary of a libstdc++ string is a C string: it ends at the first NUL, so
`"a\\0b"` shows as `"a"`. A `std::string` LLDB names by that typedef gets the summary
string `${var._M_dataplus._M_p}`, which fails where the build leaves `_Alloc_hider`
incomplete (clang's default on Linux): `error: summary string parsing error`. The
string's `_M_string_length` member says how many characters it holds; where the debugger
reads memory, its bytes are read and shown as LLDB prints a libc++ string (every byte,
a NUL as `\\0`); elsewhere the text is shown with a note, never as if complete.

Every string literal LLDB or Peeky shows is also spelled so that C reads it back as
the same bytes (``tidy``): LLDB writes a NUL as `\\0` and an invalid byte as `\\xff`
even before a digit, where C would read `"\\064"` as one character `4` and `"\\xffa"` as
one; there the three-digit octal form is used (`\\000`, `\\377`), and a text longer than
the worker keeps is cut between characters, with LLDB's `...`.
"""

import re
import struct
import unicodedata

# LLDB's `target.max-string-summary-length`: a longer string's summary ends `"...`.
SUMMARY_UNITS = 1024
# The worker keeps a value's first 2000 characters: a literal, its address and a note fit.
_SHOWN = 1700
_VALUE = 2000

# A standard string class (`std::string`, `std::basic_string<char>`, libstdc++'s
# `std::__cxx11::basic_string<char, ...>`), by value, reference or pointer.
_STRING = re.compile(
    r"(?:(?:const|volatile)\s+)*(?:::)?std::(?:__\w+::)*"
    r"(?:(?P<alias>string|wstring|u16string|u32string)"
    r"|basic_string<\s*(?P<unit>char|wchar_t|char16_t|char32_t)\s*(?:,.*)?>)"
    r"(?:\s+(?:const|volatile))*\s*(?P<indirect>\*|&{1,2})?\s*(?:const)?\s*",
    re.S,
)
# (bytes per character, the literal's prefix) for each character type, as on Linux.
_UNITS = {"char": (1, ""), "wchar_t": (4, "L"), "char16_t": (2, "u"), "char32_t": (4, "U")}
_ALIASES = {
    "string": "char",
    "wstring": "wchar_t",
    "u16string": "char16_t",
    "u32string": "char32_t",
}
_ESCAPES = {0: "\\0", 7: "\\a", 8: "\\b", 9: "\\t", 10: "\\n", 11: "\\v", 12: "\\f", 13: "\\r"}
_ESCAPES.update({ord('"'): '\\"', ord("\\"): "\\\\"})
_SIMPLE = {"0", "a", "b", "t", "n", "v", "f", "r", '"', "\\", "e"}
_ADDRESS = re.compile(r"(0x[0-9a-fA-F]+)(?:\s+|$)")


def string_class(kind):
    """(bytes per character, literal prefix, `*`/`&`/``"``) for a standard string type,
    or None."""
    found = _STRING.fullmatch(str(kind or "").strip())
    if not found:
        return None
    unit = found["unit"] or _ALIASES[found["alias"]]
    width, prefix = _UNITS[unit]
    return width, prefix, found["indirect"] or ""


def _code_units(code, width):
    if width == 1:
        return len(chr(code).encode("utf-8", "surrogatepass"))
    if width == 2:
        return 2 if code > 0xFFFF else 1
    return 1


def _lex(text, at=0, spelled=False):
    """The string literal LLDB prints at ``text[at:]`` (`"a\\tb"`, `L"x"`, `"yyy"...`):
    (prefix, [(spelling, code point or None for a raw byte `\\xNN`, raw)], whether LLDB
    cut it at its length limit, where it ends), or None for other text. ``raw``: written
    as the character itself, not an escape. ``spelled``: a literal Peeky wrote, which C
    reads (`\\000` and `\\377` are one character each, see ``tidy``)."""
    found = re.compile(r'(L|u|U)?"').match(text, at)
    if not found:
        return None
    tokens, at = [], found.end()
    while at < len(text):
        char = text[at]
        if char == '"':
            capped = text.startswith("...", at + 1)
            return found[1] or "", tokens, capped, at + 1 + 3 * capped
        if char != "\\":
            tokens.append((char, ord(char), True))
            at += 1
            continue
        escape = text[at + 1 : at + 2]
        octal = re.compile(r"[0-7]{1,3}").match(text, at + 1) if spelled else None
        if octal:
            code = int(octal[0], 8)
            tokens.append(("\\" + octal[0], code if code < 0x80 else None, False))
            at = octal.end()
        elif escape in _SIMPLE:
            code = {"0": 0, "a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, "e": 27}
            tokens.append((text[at : at + 2], code.get(escape, ord(escape)), False))
            at += 2
        elif escape == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", text[at + 2 : at + 4]):
            tokens.append((text[at : at + 4], None, False))
            at += 4
        elif escape in ("u", "U"):
            digits = 4 if escape == "u" else 8
            code = text[at + 2 : at + 2 + digits]
            if not re.fullmatch(rf"[0-9a-fA-F]{{{digits}}}", code) or int(code, 16) > 0x10FFFF:
                return None
            tokens.append((text[at : at + 2 + digits], int(code, 16), False))
            at += 2 + digits
        else:
            return None
    return None


def summary_units(text, width, prefix):
    """(characters shown, whether LLDB cut the summary at its length limit) for a string
    summary as LLDB prints it (`"a\\tb"`, `L"x"`, `"yyy"...`), or None for other text.
    A character is a byte for `char` (UTF-8), a code unit for the wider types."""
    lexed = _lex(text)
    if lexed is None or lexed[0] != prefix or lexed[3] != len(text):
        return None
    count = sum(1 if code is None else _code_units(code, width) for _, code, _ in lexed[1])
    return count, lexed[2]


def shows_text(value, found):
    """Whether the debugger's ``value`` for a standard string (``found``: its
    ``string_class``) is its text: a literal, after the address of a pointer or
    reference to it. Such a value is shown whole; its members are the library's."""
    width, prefix, indirect = found
    value = str(value or "")
    if indirect:
        located = _ADDRESS.match(value)
        if not located:
            return False
        value = value[located.end() :]
    return summary_units(value, width, prefix) is not None


def _spell(tokens):
    """The tokens' text, each escape spelled so that C reads it back alone: LLDB's `\\0`
    before an octal digit is `\\000`, its `\\xNN` before a hex digit `\\NNN` (octal)."""
    shown = []
    for at, (spelling, code, _) in enumerate(tokens):
        after = tokens[at + 1] if at + 1 < len(tokens) else None
        follows = after[0] if after and after[2] else ""
        if spelling == "\\0" and follows in "01234567" and follows:
            spelling = "\\000"
        elif spelling.startswith("\\x") and follows and follows in "0123456789abcdefABCDEF":
            spelling = f"\\{int(spelling[2:], 16):03o}"
        shown.append(spelling)
    return shown


def _literal(prefix, tokens, capped, room):
    """A literal of ``tokens`` within ``room`` characters, cut between characters with
    LLDB's `...` where it does not fit (or LLDB already cut it)."""
    size = len(prefix) + 2
    spelled = _spell(tokens)
    for at, spelling in enumerate(spelled):
        if size + len(spelling) + 3 > room:
            capped = True
            spelled = spelled[:at]
            break
        size += len(spelling)
    return f'{prefix}"{"".join(spelled)}"' + ("..." if capped else "")


# LLDB's text where a summary failed, after a pointer's or reference's address.
_FAILED = re.compile(
    r"(?:(0x[0-9a-fA-F]+)\s+)?(error: (?:summary string parsing error|no target|no "
    r"ScriptInterpreter))\s*"
)


def tidy(text, spell=True):
    """What to show instead of the debugger's text ``text`` for a native value, or None
    to keep it: never LLDB's own `error: summary string parsing error` as if it were
    the value (an unavailable value, or `<no text: ...>` after a pointer's address);
    and, with ``spell`` (C and C++, not Rust, whose `\0` is never followed by more
    digits), a string literal (after an address, before a note) spelled as C reads it
    and bounded to the worker's 2000 characters. Only for the debugger's own text: a
    literal Peeky spelled reads differently (its `\000` is one NUL, LLDB's three
    characters)."""
    text = str(text)
    if failed := _FAILED.fullmatch(text):
        reason = f"LLDB's summary of this value failed ({failed[2]}); select its members"
        if failed[1]:
            return {"value": f"{failed[1]} <no text: {reason}>"}
        return {"value": None, "unavailable": True, "reason": reason}
    if not spell:
        return None
    located = _ADDRESS.match(text)
    start = located.end() if located else 0
    lexed = _lex(text, start)
    if lexed is None:
        return None
    prefix, tokens, capped, end = lexed
    note = text[end:]
    if note and not note.startswith(" ("):
        return None
    room = _VALUE - start - min(len(note), 200)
    shown = text[:start] + _literal(prefix, tokens, capped, room) + note[:200]
    return None if shown == text else {"value": shown}


def shorten(text, room):
    """``text``, as the worker shows it (a literal spelled as C reads it, after an
    address, before a note), within ``room`` characters: a literal is cut between
    characters with LLDB's `...`, its note kept whole; other text is cut with `…`."""
    text = str(text)
    if len(text) <= room:
        return text
    located = _ADDRESS.match(text)
    start = located.end() if located else 0
    lexed = _lex(text, start, spelled=True)
    if lexed is not None and (not text[lexed[3] :] or text[lexed[3] :].startswith(" (")):
        prefix, tokens, capped, end = lexed
        note = text[end:]
        # A note is never cut: the literal keeps a few characters before it, and is
        # marked cut only where it is.
        room = max(room - start - len(note), 12 if note else 0)
        return text[:start] + _literal(prefix, tokens, capped, room) + note
    return text[: max(room - 1, 0)] + "…"


def _printable(code):
    if code < 0x80:
        return 0x20 <= code < 0x7F
    if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
        return False
    return unicodedata.category(chr(code)) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


def _character(code):
    if code in _ESCAPES:
        return _ESCAPES[code]
    return chr(code) if _printable(code) else f"\\U{code:08x}"


def _characters(data, width):
    """Each character of ``data`` as LLDB prints it: UTF-8 (a byte that starts no valid
    sequence is `\\xNN`), UTF-16 or UTF-32, little-endian."""
    if width == 1:
        at = 0
        while at < len(data):
            lead = data[at]
            size = 1 if lead < 0x80 else 2 if lead >> 5 == 6 else 3 if lead >> 4 == 14 else 4
            try:
                text = data[at : at + size].decode("utf-8") if lead >= 0x80 else chr(lead)
            except UnicodeDecodeError:
                text = None
            if text is None or len(text) != 1 or (lead >= 0x80 and lead >> 3 == 31):
                yield f"\\x{lead:02x}"
                at += 1
                continue
            yield _character(ord(text))
            at += size
        return
    codes = struct.unpack(f"<{len(data) // width}{'H' if width == 2 else 'I'}", data)
    at = 0
    while at < len(codes):
        code = codes[at]
        if width == 2 and 0xD800 <= code < 0xDC00 and at + 1 < len(codes):
            low = codes[at + 1]
            if 0xDC00 <= low < 0xE000:
                code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                at += 1
        yield _character(code) if code <= 0x10FFFF else f"\\U{code:08x}"
        at += 1


def render(data, width, prefix, more):
    """``data`` as LLDB's libc++ summary prints a string: quoted, escaped, every NUL as
    `\\0` (spelled as C reads it back: see ``tidy``); ``more`` adds LLDB's `...`."""
    tokens = []
    for character in _characters(data, width):
        tokens.append((character, None, not character.startswith("\\")))
        if len(tokens) > _SHOWN:
            more = True
            break
    return _literal(prefix, tokens, more, _SHOWN)


# The class a libstdc++ string's `_M_dataplus` member names (`std::basic_string<char>::
# _Alloc_hider`): its character type, whatever name the value itself goes by.
_MEMBER_OF = re.compile(
    r"(?:(?:const|volatile)\s+)*(?:::)?std::(?:__\w+::)*basic_string<\s*"
    r"(char|wchar_t|char16_t|char32_t)\s*(?:,.*)?>::_Alloc_hider\s*",
    re.S,
)
# What LLDB shows for a value it gives a string summary, or whose summary failed.
_TEXT_LIKE = re.compile(r'(?:0x[0-9a-fA-F]+\s+)?(?:(?:L|u|U)?"|error: summary string)')
# C's own text (`char *`, `char[16]`): LLDB shows it as a literal, as C has it.
_C_TEXT = re.compile(
    r"(?:(?:const|volatile|signed|unsigned)\s+)*(?:char|char8_t|char16_t|char32_t|wchar_t)"
    r"(?:\s+(?:const|volatile))*\s*(?:\*|\[).*",
    re.S,
)


def text_like(variable):
    """Whether a value LLDB shows as a string literal (or as a failed summary) may be a
    libstdc++ string under a type name Peeky cannot resolve: worth reading its members."""
    raw = variable.get("variablesReference")
    return (
        type(raw) is int
        and raw > 0
        and bool(_TEXT_LIKE.match(str(variable.get("value", ""))))
        and not _C_TEXT.fullmatch(str(variable.get("type") or "").strip())
    )


def libstdcxx_string(variable, fetch, read, found="type"):
    """The value to show for a libstdc++ string ``variable`` (a DAP variable whose
    children are its members), as {"value": ...} (with "view": "memory" where its bytes
    were read) or {"unavailable": True, "reason": ...}; {"string_length": N} alone where
    the debugger's text is whole (N characters); None for anything else.
    ``found``: its ``string_class`` (by default its type's), or that of the class a
    typedef names; None for a value told by its members alone (`_M_dataplus`,
    `_M_string_length`), whatever its type's name. ``fetch(reference)`` lists a
    debugger variable's children;
    ``read(address, count)`` reads memory, or is None where the debugger cannot."""
    if found == "type" and (found := string_class(variable.get("type"))) is None:
        return None
    width, prefix, indirect = found or (None, None, None)
    value = str(variable.get("value", ""))
    address = ""
    located = _ADDRESS.match(value)
    if indirect or (found is None and located):
        # A pointer or reference shows its address, then what its target's summary says.
        if not located:
            return None
        address, value = located[1] + " ", value[located.end() :]
    raw = variable.get("variablesReference")
    try:
        members = (
            {member.get("name"): member for member in fetch(raw)}
            if type(raw) is int and raw > 0
            else {}
        )
    except ValueError as exc:
        # Includes the view budget: the text is not checked, so it is not vouched for.
        if found is None:
            return None
        if _FAILED.fullmatch(value):
            return _no_text(address, f"the string's summary failed ({value[:60]}): {exc}"[:300])
        return _noted(address, value, f"not checked for a NUL byte: {str(exc)[:80]}")
    if found is None:
        # Told by its members: libstdc++'s, with the character type `_M_dataplus` names.
        owner = _MEMBER_OF.fullmatch(str((members.get("_M_dataplus") or {}).get("type") or ""))
        if owner is None or "_M_string_length" not in members:
            return None
        width, prefix = _UNITS[owner[1]]
    length = members.get("_M_string_length", {}).get("value")
    if not isinstance(length, str) or not re.fullmatch(r"[0-9]{1,19}", length):
        if members and not any(str(name).startswith("_M_") for name in members):
            # libc++'s members: LLDB's summary of its strings shows every byte.
            return None
        # No length to check: clang leaves out the members of a class it expects
        # libstdc++'s own debug information to give (`std::wstring` here, used only by
        # name), and libstdc++'s old ABI keeps the length out of view.
        if value.startswith("error:"):
            return _no_text(
                address,
                f"the string's summary failed ({value[:60]}), and the build's debug "
                "information gives no length for it",
            )
        if summary_units(value, width, prefix):
            return _noted(
                address,
                value,
                "not checked for a NUL byte: the debugger shows no length for this string",
            )
        return None
    length = int(length)
    shown = summary_units(value, width, prefix)
    unit = "byte" if width == 1 else "character"
    if shown:
        count, capped = shown
        # LLDB's summary stops at the first NUL; a `char` one also at SUMMARY_UNITS
        # bytes, where it adds `...` (as it does, too, after a NUL in a longer string).
        whole = count == length and not capped
        limited = width == 1 and capped and count == SUMMARY_UNITS < length
        if whole or limited:
            # The debugger's own text, checked: map keys compare it.
            return {"string_length": length}
    if read is not None:
        text = _read(members, length, width, prefix, read)
        if text is not None:
            return {"value": address + text, "view": "memory", "string_length": length}
    if shown:
        count, capped = shown
        if count < length and (not capped or width == 1):
            note = f"shown up to a NUL at {unit} {count} of {length}"
        elif count < length:
            note = f"the debugger shows its first {count} of {length} {unit}s"
        else:
            note = f"the debugger shows {count} {unit}s of a string of {length}"
        return _noted(address, value, note)
    if value.startswith("error:"):
        return _no_text(
            address,
            f"the string's summary failed ({value[:60]}), and this debugger cannot read "
            f"memory (lldb-dap 20 can); it holds {length} {unit}s",
        )
    return None


def _noted(address, value, note):
    """The debugger's literal (bounded, as ``tidy`` keeps it) and a note on it."""
    shown = f"{address}{value} ({note})"
    return {"value": (tidy(shown) or {"value": shown})["value"]}


def _no_text(address, reason):
    """No text for a string: an unavailable value, or after a pointer's address."""
    if address:
        return {"value": f"{address}<no text: {reason}>"}
    return {"value": None, "unavailable": True, "reason": reason}


def _read(members, length, width, prefix, read):
    """The string's first characters, read from memory: its data pointer and length
    (libstdc++'s first two words, the length checked against `_M_string_length`), then
    up to SUMMARY_UNITS characters. A string in its own 16-byte buffer (right after
    those words) is read in the same request. None where any read fails."""
    reference = (members.get("_M_dataplus") or {}).get("memoryReference")
    if not isinstance(reference, str) or not re.fullmatch(r"0x[0-9A-Fa-f]{1,16}", reference):
        return None
    try:
        head = read(reference, 32)
        data, stored = struct.unpack("<QQ", head[:16])
        if stored != length:
            return None
        count = min(length, SUMMARY_UNITS) * width
        if data == int(reference, 16) + 16 and count <= 16:
            body = head[16 : 16 + count]
        else:
            body = read(f"0x{data:x}", count) if count else b""
    except (ValueError, struct.error):
        return None
    return render(body, width, prefix, length > SUMMARY_UNITS)
