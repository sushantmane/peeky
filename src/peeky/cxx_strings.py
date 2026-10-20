"""libstdc++ `std::basic_string` values as LLDB's libc++ summary shows them (#128).

LLDB's summary of a libstdc++ string is a C string: it ends at the first NUL, so
`"a\\0b"` shows as `"a"`. A `std::string` LLDB names by that typedef gets the summary
string `${var._M_dataplus._M_p}`, which fails where the build leaves `_Alloc_hider`
incomplete (clang's default on Linux): `error: summary string parsing error`. The
string's `_M_string_length` member says how many characters it holds; where the debugger
reads memory, its bytes are read and shown as LLDB prints a libc++ string (every byte,
a NUL as `\\0`); elsewhere the text is shown with a note, never as if complete.
"""

import re
import struct
import unicodedata

# LLDB's `target.max-string-summary-length`: a longer string's summary ends `"...`.
SUMMARY_UNITS = 1024
# The worker keeps a value's first 2000 characters.
_SHOWN = 1900

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


def summary_units(text, width, prefix):
    """(characters shown, whether LLDB cut the summary at its length limit) for a string
    summary as LLDB prints it (`"a\\tb"`, `L"x"`, `"yyy"...`), or None for other text.
    A character is a byte for `char` (UTF-8), a code unit for the wider types."""
    found = re.fullmatch(rf'{re.escape(prefix)}"(.*)"(\.\.\.)?', text, re.S)
    if not found:
        return None
    body, count, at = found[1], 0, 0
    while at < len(body):
        char = body[at]
        if char == '"':
            return None
        if char != "\\":
            count += _code_units(ord(char), width)
            at += 1
            continue
        escape = body[at + 1 : at + 2]
        if escape in _SIMPLE:
            count, at = count + 1, at + 2
        elif escape == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", body[at + 2 : at + 4]):
            count, at = count + 1, at + 4
        elif escape in ("u", "U"):
            digits = 4 if escape == "u" else 8
            code = body[at + 2 : at + 2 + digits]
            if not re.fullmatch(rf"[0-9a-fA-F]{{{digits}}}", code) or int(code, 16) > 0x10FFFF:
                return None
            count, at = count + _code_units(int(code, 16), width), at + 2 + digits
        else:
            return None
    return count, bool(found[2])


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
    `\\0`; ``more`` (or the shown text's own bound) adds LLDB's `...`."""
    shown = []
    size = 0
    cut = more
    for character in _characters(data, width):
        if size + len(character) > _SHOWN:
            cut = True
            break
        shown.append(character)
        size += len(character)
    return f'{prefix}"{"".join(shown)}"' + ("..." if cut else "")


def libstdcxx_string(variable, fetch, read):
    """The value to show for a libstdc++ string ``variable`` (a DAP variable whose
    children are its members), as {"value": ...} (with "view": "memory" where its bytes
    were read) or {"unavailable": True, "reason": ...}; {"string_length": N} alone where
    the debugger's text is whole (N characters); None for anything else.
    ``fetch(reference)`` lists a debugger variable's children; ``read(address, count)``
    reads memory, or is None where the debugger cannot."""
    found = string_class(variable.get("type"))
    if found is None:
        return None
    width, prefix, indirect = found
    value = str(variable.get("value", ""))
    address = ""
    if indirect:
        # A pointer or reference shows its address, then what its target's summary says.
        located = _ADDRESS.match(value)
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
        return {"value": f"{address}{value} (not checked for a NUL byte: {str(exc)[:80]})"}
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
            return {
                "value": f"{address}{value} (not checked for a NUL byte: the debugger shows "
                "no length for this string)"
            }
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
            # The debugger's own text, checked: map keys compare it (#119).
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
        return {"value": f"{address}{value} ({note})"}
    if value.startswith("error:"):
        return _no_text(
            address,
            f"the string's summary failed ({value[:60]}), and this debugger cannot read "
            f"memory (lldb-dap 20 can); it holds {length} {unit}s",
        )
    return None


def _no_text(address, reason):
    """No text for a string: an unavailable value, or after a pointer's address."""
    if address:
        return {"value": f"{address}<no text: {reason}>"}
    return {"value": None, "unavailable": True, "reason": reason}


def _read(members, length, width, prefix, read):
    """The string's first characters, read from memory: its data pointer and length
    (libstdc++'s first two words, the length checked against `_M_string_length`), then
    up to SUMMARY_UNITS characters. None where any read fails."""
    reference = (members.get("_M_dataplus") or {}).get("memoryReference")
    if not isinstance(reference, str) or not re.fullmatch(r"0x[0-9A-Fa-f]{1,16}", reference):
        return None
    try:
        data, stored = struct.unpack("<QQ", read(reference, 16))
        if stored != length:
            return None
        count = min(length, SUMMARY_UNITS)
        body = read(f"0x{data:x}", count * width) if count else b""
    except (ValueError, struct.error):
        return None
    return render(body, width, prefix, length > SUMMARY_UNITS)
