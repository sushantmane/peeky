"""A stored value's elements and text, for SQL: `elements(value, language)` and
`text(value, language, type)`.

A captured value is stored as Peeky shows it, in its language's notation: a Java array is
`int[3]@55 [490, 484, 497]`, a Python string `'Göteborg'`, with its quotes. So
`json_each(substr(value, instr(value, '[')))` fails (the first `[` is the type's), and
`value = 'Göteborg'` matches nothing. These read the notation back, and only where it says
exactly what the program holds; anything else is NULL, never a guess:

- a preview cut short (`…`, `...`) or a count that disagrees with its elements;
- an element that is not a number, string, char, boolean, null or nested list (an object,
  a struct, a pointer, an enumerator);
- a string that may be cut (a Java element's 38th character `…`) or that is not text (a C
  byte `\\xff`, a lone surrogate);
- a C or C++ character array (`char[32]`) whose note disagrees with its size: its text is
  its bytes up to its first NUL (`"Adeyemi" (then a NUL and 24 bytes not shown)` gives
  `Adeyemi`), or all of them where it holds none;
- an integer outside SQLite's 64 bits, or a decimal that a double does not hold exactly as
  written (a `long double`'s `1e-4000` or `1.0000000000000000001`);
- any Python value: debugpy names a value's type by its `__class__` and shows it by its
  own `__repr__`, and a class may set both (a class named `str`, or whose `__class__` is
  `str`, with a repr of `'Göteborg'`; a list of such objects shows `['Göteborg']`), so
  nothing Peeky stores proves a value is a builtin `str`, `int` or `list`;
- a value of 2000 characters or more, as the worker keeps a value's first 2000 and marks no
  cut there.
"""

import decimal
import json
import math
import re

from peeky.char_arrays import NOTE, array_size

KEPT = 2000
_DEPTH = 32
# A number as JSON writes it, which SQLite's JSON functions read: an integer, or a decimal
# with an exponent (`1.0E10`, `1e-7`). `5.`, `.5`, `inf` and `NaN` are not.
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?")
# Java's preview of an array (`int[3]@55 [`) and of an ArrayList (`ArrayList@142 size=2 [`).
_JAVA_ARRAY = re.compile(r"[A-Za-z_$][\w$.]*(?:\[\])*\[([0-9]{1,10})\](?:\[\])*@[0-9]+ (?=\[)")
_JAVA_LIST = re.compile(r"[A-Za-z_$][\w$.]*@[0-9]+ size=(0|[1-9][0-9]{0,9}) (?=\[)")
# A C++ container's count before its elements (`size=2 [1, 2]`, a set's `size=1 {7}`).
_SIZE = re.compile(r"size=(0|[1-9][0-9]{0,9}) (?=[\[{])")
# A C or C++ reference's address before what it refers to (`0x… size=2 [1, 2]`, `0x… "Maya"`).
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{1,16} ")
_FAMILIES = {
    "java": "java",
    "java-attach": "java",
    "c": "c",
    "cpp": "c",
    "rust": "rust",
}
# Each family's escapes that stand for one character.
_SIMPLE = {
    "java": {"\\": "\\", "'": "'", '"': '"', "n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"},
    "c": {
        "\\": "\\",
        "'": "'",
        '"': '"',
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "v": "\v",
        "e": "\x1b",
    },
    # Rust's `{:?}` (`\0`, `\u{1b}`) and JSON's (`\b`, `\u001b`): Peeky writes both.
    "rust": {
        "\\": "\\",
        "'": "'",
        '"': '"',
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "0": "\0",
        "b": "\b",
        "f": "\f",
    },
}
# Each family's escapes that give a character's code by its hex digits: `\u00e9` (a
# Java String's UTF-16 unit).
_HEX = {
    "java": {"u": 4},
    "c": {"u": 4, "U": 8},
    "rust": {"u": 4},
}
_OCTAL = re.compile(r"[0-7]{1,3}")
_BRACED = re.compile(r"u\{([0-9a-fA-F]{1,6})\}")
_PREFIX = re.compile(r"u8|[LuU]")
# Each family's words for true, false and null.
_WORDS = {
    "java": (("true", "true"), ("false", "false"), ("null", "null")),
    "c": (("true", "true"), ("false", "false")),
    "rust": (("true", "true"), ("false", "false")),
}
# Peeky's Java preview cuts a String element after 37 characters, with `…` (java._brief).
_JAVA_INNER = 38


class _Unsure(Exception):
    """Text that does not say exactly what the program holds."""


class _Raw(str):
    """A JSON token kept as written: a number, true, false or null."""


def _family(value, language):
    if not isinstance(value, str) or not isinstance(language, str) or len(value) >= KEPT:
        return None
    return _FAMILIES.get(language)


class _Reader:
    def __init__(self, text, family):
        self.text, self.family, self.at = text, family, 0

    def expect(self, token):
        if not self.text.startswith(token, self.at):
            raise _Unsure()
        self.at += len(token)

    def sequence(self, depth):
        """The elements of the list that starts here."""
        if depth > _DEPTH:
            raise _Unsure()
        text, count = self.text, None
        if self.family == "c" and (size := _SIZE.match(text, self.at)):
            count, self.at = int(size[1]), size.end()
        opening = text[self.at : self.at + 1]
        if not opening or opening not in ("[{" if count is not None else "["):
            raise _Unsure()
        closing = "]" if opening == "[" else "}"
        self.at += 1
        items = []
        while not text.startswith(closing, self.at):
            if items:
                self.expect(", ")
            items.append(self.element(depth))
        self.at += 1
        if count is not None and count != len(items):
            raise _Unsure()
        return items

    def element(self, depth):
        text, at = self.text, self.at
        if self.family != "java" and (
            text.startswith("[", at) or (self.family == "c" and _SIZE.match(text, at))
        ):
            return self.sequence(depth + 1)
        decoded = self.literal(inner=True)
        if decoded is not None:
            if self.family == "c" and (note := NOTE.match(text, self.at)):
                # A character array's text, up to its first NUL, and what follows it.
                if "\0" in decoded:
                    raise _Unsure()
                self.at = note.end()
            return decoded
        if number := _NUMBER.match(text, at):
            self.at = number.end()
            return _number(number)
        for word, token in _WORDS[self.family]:
            if text.startswith(word, at):
                self.at += len(word)
                return _Raw(token)
        raise _Unsure()

    def literal(self, inner):
        """The decoded string or char literal that starts here, else None."""
        text, at = self.text, self.at
        if self.family == "c" and (prefix := _PREFIX.match(text, at)):
            at = prefix.end()
        quote = text[at : at + 1]
        if quote not in ("'", '"'):
            return None
        units, at = self._units(at + 1, quote)
        decoded = _joined(units, self.family)
        if quote == "'" and len(decoded) != 1:
            # A char literal holds one character.
            raise _Unsure()
        if inner and self.family == "java" and quote == '"':
            if len(decoded) == _JAVA_INNER and decoded.endswith("…"):
                raise _Unsure()
        self.at = at
        return decoded

    def byte_literal(self, at, quote):
        """(the bytes of a C literal of a character array, up to the closing ``quote``;
        where it ends): a character as its UTF-8, an escape as its byte (`\\x01`,
        `\\303`), `\\U` and `\\u` as their character's UTF-8."""
        text, simple, data = self.text, _SIMPLE["c"], bytearray()
        while True:
            if at >= len(text):
                raise _Unsure()
            char = text[at]
            if char == quote:
                return bytes(data), at + 1
            if char != "\\":
                try:
                    data += char.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise _Unsure() from exc
                at += 1
                continue
            escape = text[at + 1 : at + 2]
            if escape in simple:
                data.append(ord(simple[escape]))
                at += 2
            elif octal := _OCTAL.match(text, at + 1):
                if int(octal[0], 8) > 0xFF:
                    raise _Unsure()
                data.append(int(octal[0], 8))
                at = octal.end()
            elif escape == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", text[at + 2 : at + 4]):
                # C reads every hex digit after `\x`: Peeky writes octal before one.
                if re.match(r"[0-9a-fA-F]", text[at + 4 : at + 5]):
                    raise _Unsure()
                data.append(int(text[at + 2 : at + 4], 16))
                at += 4
            elif escape in _HEX["c"]:
                digits = text[at + 2 : at + 2 + _HEX["c"][escape]]
                if not re.fullmatch(rf"[0-9a-fA-F]{{{_HEX['c'][escape]}}}", digits):
                    raise _Unsure()
                try:
                    data += chr(int(digits, 16)).encode("utf-8")
                except (ValueError, UnicodeEncodeError) as exc:
                    raise _Unsure() from exc
                at += 2 + len(digits)
            else:
                raise _Unsure()

    def _units(self, at, quote):
        """(code points, or a Java String's UTF-16 units, up to the closing ``quote``; where
        the literal ends)."""
        text, family, units = self.text, self.family, []
        simple, hexadecimal = _SIMPLE[family], _HEX[family]
        while True:
            if at >= len(text):
                raise _Unsure()
            char = text[at]
            if char == quote:
                return units, at + 1
            if char != "\\":
                units.append(ord(char))
                at += 1
                continue
            escape = text[at + 1 : at + 2]
            if escape in simple:
                units.append(ord(simple[escape]))
                at += 2
            elif family == "c" and (octal := _OCTAL.match(text, at + 1)):
                # A byte past ASCII is part of a character's UTF-8, or no text at all.
                if int(octal[0], 8) >= 0x80:
                    raise _Unsure()
                units.append(int(octal[0], 8))
                at = octal.end()
            elif family == "rust" and (braced := _BRACED.match(text, at + 1)):
                units.append(int(braced[1], 16))
                at = braced.end()
            elif escape in hexadecimal:
                # Not C's `\xff`, a byte that is no character.
                digits = text[at + 2 : at + 2 + hexadecimal[escape]]
                if not re.fullmatch(rf"[0-9a-fA-F]{{{hexadecimal[escape]}}}", digits):
                    raise _Unsure()
                units.append(int(digits, 16))
                at += 2 + len(digits)
            else:
                raise _Unsure()


def _joined(units, family):
    """The text of ``units``: code points, or (Java) UTF-16 units; _Unsure for a lone
    surrogate or a code point past Unicode's."""
    if any(unit > 0x10FFFF for unit in units):
        raise _Unsure()
    joined = "".join(map(chr, units))
    try:
        if family == "java":
            joined = joined.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
        joined.encode("utf-8")
    except UnicodeError as exc:
        raise _Unsure() from exc
    return joined


def _number(found):
    """The number token, where SQLite reads it as written: an integer of 64 bits, or a
    decimal a double holds to as many digits as it has (a C `long double`'s `1e-4000`
    would read as 0, and `1.0000000000000000001` as 1)."""
    token = found[0]
    if found[1] is None and found[2] is None:
        if not -(2**63) <= int(token) < 2**63:
            raise _Unsure()
        return _Raw(token)
    number = float(token)
    digits = re.split("[eE]", token.lstrip("-"))[0].replace(".", "").lstrip("0")
    try:
        held = decimal.Decimal(f"{number:.{max(len(digits), 1) - 1}e}")
        if math.isinf(number) or held != decimal.Decimal(token):
            raise _Unsure()
    except (decimal.DecimalException, ValueError, OverflowError) as exc:
        # An exponent past what Decimal compares (`1e-999…`).
        raise _Unsure() from exc
    return _Raw(token)


def _json(item):
    if isinstance(item, list):
        return "[" + ", ".join(map(_json, item)) + "]"
    if isinstance(item, _Raw):
        return str(item)
    return json.dumps(item, ensure_ascii=False)


def elements(value, language):
    """A list's or array's elements as a JSON array (`[490, 484, 497]`), where ``value``
    is a preview Peeky reads exactly for ``language`` (the run's: runs.language), else
    None; never a Python value's (see the module's docstring). Registered as the SQL
    function elements(value, language)."""
    family = _family(value, language)
    if family is None:
        return None
    reader, count = _Reader(value, family), None
    if family == "java":
        head = _JAVA_ARRAY.match(value) or _JAVA_LIST.match(value)
        if head is None:
            return None
        count, reader.at = int(head[1]), head.end()
    elif family == "c" and (address := _ADDRESS.match(value)):
        reader.at = address.end()
    try:
        items = reader.sequence(0)
    except _Unsure:
        return None
    if reader.at != len(value) or (count is not None and count != len(items)):
        return None
    return _json(items)


def text(value, language, kind):
    """A string's or char's text without its quotes and escapes (`"say \\"hi\\""` gives
    `say "hi"`), where ``value`` is a literal Peeky decodes exactly for ``language`` and
    ``kind`` (the row's type), else None; never a Python value's (see the module's
    docstring). A C or C++ character array's is its text up to its first NUL
    (``_array_text``). Registered as the SQL function text(value, language, type)."""
    family = _family(value, language)
    if family is None:
        return None
    if family == "java":
        # java-debug shows a char bare (`+`), and a String's text between quotes, unescaped.
        if kind == "char" and len(value) == 1:
            shown = value
        elif kind != "char" and len(value) >= 2 and value[0] == value[-1] == '"':
            shown = value[1:-1]
        else:
            return None
        try:
            shown.encode("utf-8")
        except UnicodeError:
            return None
        return shown
    reader = _Reader(value, family)
    if family == "c" and (size := array_size(kind)) is not None:
        return _array_text(reader, size)
    if family == "c" and (address := _ADDRESS.match(value)):
        # The text a C or C++ pointer or reference points at.
        reader.at = address.end()
    try:
        decoded = reader.literal(inner=False)
    except _Unsure:
        return None
    return decoded if decoded is not None and reader.at == len(value) else None


def _array_text(reader, size):
    """A narrow C or C++ character array's text (a `char[N]` of ``size`` bytes): its bytes
    up to its first NUL, or all of them where it holds none, read as UTF-8. Its value is
    Peeky's (`"Adeyemi" (then a NUL and 24 bytes not shown)`), whose note must agree with
    the size; or, as runs before that stored it, LLDB's literal of the bytes it read up to
    the last that is no NUL (`"Adeyemi\\0\\x80"`). That one proves a NUL only where it
    shows one, and no NUL only where it shows every byte: LLDB formats what part of its
    read succeeded, so fewer bytes may end at an unreadable page, not a NUL. None for a
    cut or other value."""
    value = reader.text
    if not value.startswith('"'):
        return None
    try:
        data, end = reader.byte_literal(1, '"')
    except _Unsure:
        return None
    nul, rest = data.find(b"\0"), value[end:]
    if rest:
        note = NOTE.fullmatch(rest)
        if note is None or nul >= 0:
            return None
        after, nuls, whole = (None if count is None else int(count) for count in note.groups())
        if (
            (after is not None and len(data) + 1 + after != size)
            or (nuls is not None and len(data) + nuls != size)
            or (whole is not None and not len(data) == whole == size)
        ):
            return None
    elif len(data) > size or (nul < 0 and len(data) != size):
        return None
    try:
        return (data[:nul] if nul >= 0 else data).decode("utf-8")
    except UnicodeDecodeError:
        return None
