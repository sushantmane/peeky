r"""Where a C or C++ declaration statement ends, from the source text alone (#104).

A caller frame is inside a call made from its line. When that line is within a local's
declaration (`struct trio t = { seed,` / `{ twice(3),` / `twice(4) } };`), the
declaration is still running and the local may hold leftovers, whatever code its first
line has. The statement's end is its first `;` outside parentheses, brackets and braces
(digraphs too), once comments and string and character literals are blanked.

Lines are counted as the compilers count them (`normalized`). Nothing is guessed: an
end that cannot be found is None, and so is one where the text the scan reads may differ
from what the compiler reads. That is anywhere in a file with a trigraph, a `#line`, a
line splice inside a token or before `\n\r`; and, from the declaration's line
to its end, a directive (an `#include` can hold the rows), a line splice, a `<::`, or
a macro whose expansion may hold an unclosed bracket or a `;` (`unsafe_macros`).
"""

import bisect
import re

# An identifier, not the tail of a number or of another identifier (`1e5`).
_IDENTIFIER = re.compile(r"(?<!\w)[A-Za-z_]\w*")
# Where a comment or a literal may start.
_OPENING = re.compile(r"""//|/\*|["']|(?:u8|u|U|L)?R\"""")
# A raw string's prefix and delimiter: `R"x(`, `u8R"(`.
_RAW = re.compile(r'(?:u8|u|U|L)?R"([^ ()\\\t\v\f\n]{0,16})\(')
# A line break as Clang (its line table, so DWARF, since Clang 9) and GCC count them:
# `\r\n`, a lone `\r`, or `\n`; `\n\r` is two.
_BREAK = re.compile(r"\r\n|\r|\n")
# A splice before `\n\r`: Clang's lexer takes the pair as one newline, joining the line
# after the `\r`, and GCC does not.
_SPLICED_PAIR = re.compile(r"\\[ \t\f\v]*\n\r")
# A line splice: a backslash, the spaces GCC and Clang also allow, and a newline.
_SPLICE = re.compile(r"\\[ \t\f\v]*\n")
# A splice inside a token (`/\` then `*` is a comment): a file the scan cannot read.
_SPLIT = re.compile(r"(?<=\S)\\[ \t\f\v]*\n(?=\S)")
_LITERAL = {
    '"': re.compile(r'"(?:[^"\\\n]|\\[ \t\f\v]*\n|\\[\s\S])*"?'),
    "'": re.compile(r"'(?:[^'\\\n]|\\[ \t\f\v]*\n|\\[\s\S])*'?"),
}
_VISIBLE = re.compile(r"[^\n]")
# Punctuators as the compiler reads them, longest first (`<<:` is `<<` and `:`), so that
# a digraph (`<%` `%>` `<:` `:>`) is read as the bracket it is; and `;`.
_TOKENS = re.compile(
    r"<::|<<=?|<=>?|<%|<:|>>=?|>=|--|-=|->\*?|::|:>|%:%:|%:|%=|%>|[-<>:%()\[\]{};]"
)
_OPEN = frozenset(("(", "[", "{", "<%", "<:"))
_CLOSE = frozenset((")", "]", "}", "%>", ":>"))
_NONBLANK = re.compile(r"\S")
# `??/` is a backslash and `??<` a brace where trigraphs are on (C before C23).
_TRIGRAPH = re.compile(r"\?\?[=(/)'<!>-]")
# A directive's line (`%:` is `#`).
_DIRECTIVE = re.compile(r"[ \t\f\v]*(?:#|%:)")
# `#line 7` and a line marker (`# 7 "x.c"`) renumber the lines after them.
_RENUMBER = re.compile(r"^[ \t\f\v]*(?:#|%:)[ \t\f\v]*(?:line\b|[0-9])", re.M)
_DEFINE = re.compile(r"^[ \t\f\v]*(?:#|%:)[ \t\f\v]*define[ \t\f\v]+([A-Za-z_]\w*)(\(?)(.*)$", re.M)


def normalized(text):
    r"""``text`` with each line break, as the compilers count them, written `\n`."""
    return _BREAK.sub("\n", text) if "\r" in text else text


def blank(text, raw_strings=True):
    """``text`` (`normalized`) with comments and the insides of string and character
    literals (their quotes too) replaced by spaces; line breaks stay, so lines and
    columns are unchanged. An unterminated comment blanks to the end; an unterminated
    literal to its line's. C (``raw_strings`` false, as `-std=c11` reads it) has no raw
    strings: `R"x(` is a name and a string."""
    text = normalized(text)
    pieces, done, size = [], 0, len(text)
    position = 0
    while (found := _OPENING.search(text, position)) is not None:
        start, token = found.start(), found[0]
        if token == "//":
            end = text.find("\n", start)
            while end > 0 and _continued(text, end):
                end = text.find("\n", end + 1)
            end = size if end < 0 else end
        elif token == "/*":
            end = text.find("*/", start + 2)
            end = size if end < 0 else end + 2
        elif token in _LITERAL:
            if token == "'" and _digit_separator(text, start):
                position = start + 1
                continue
            end = _LITERAL[token].match(text, start).end()
        else:
            raw = _RAW.match(text, start) if raw_strings else None
            if raw is None or _continues_identifier(text, start):
                position = start + 1
                continue
            closing = text.find(f'){raw[1]}"', raw.end())
            end = size if closing < 0 else closing + len(raw[1]) + 2
        pieces += [text[done:start], _VISIBLE.sub(" ", text[start:end])]
        done = position = end
    pieces.append(text[done:])
    return "".join(pieces)


def _continued(text, newline):
    """Whether a backslash, maybe then spaces, before the newline at ``newline`` splices
    the next line onto it."""
    at = newline - 1
    while at >= 0 and text[at] in " \t\f\v":
        at -= 1
    return at >= 0 and text[at] == "\\"


def _continues_identifier(text, i):
    # `fooR"x(` is an identifier then a string, not a raw string.
    return i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_")


def _digit_separator(text, i):
    """Whether the `'` at ``i`` is inside a number (`1'000`, C++14), not a literal."""
    start = i
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_."):
        start -= 1
    return start < i and text[start].isdigit()


def unsafe_macros(texts, raw_strings=True):
    r"""The names of the macros ``texts`` define that a statement scan cannot see
    through: an expansion that may hold a bracket it does not close, a `;`, a `<::`, or
    a pasted token (`<` ## `%` is a brace), or that names such a macro. Every macro a
    source with a trigraph, or a splice before `\n\r`, defines counts."""
    uses, unsafe = {}, set()
    for text in texts:
        # Spliced as the compiler splices lines, before comments and literals.
        code = blank(_SPLICE.sub("", normalized(text)), raw_strings)
        trigraphs = _TRIGRAPH.search(text) is not None or _SPLICED_PAIR.search(text)
        for define in _DEFINE.finditer(code):
            name, body = define[1], define[3]
            if define[2]:
                close = body.find(")")
                if close < 0:
                    unsafe.add(name)
                    continue
                body = body[close + 1 :]
            uses.setdefault(name, set()).update(word[0] for word in _IDENTIFIER.finditer(body))
            if trigraphs or not _balanced(body):
                unsafe.add(name)
    while more := {name for name, used in uses.items() if name not in unsafe and used & unsafe}:
        unsafe |= more
    return frozenset(unsafe)


def _balanced(body):
    """Whether a macro's blanked body closes each bracket it opens and holds no `;`,
    `<::` or token paste."""
    depth = 0
    for token in _TOKENS.finditer(body):
        if token[0] in _OPEN:
            depth += 1
        elif token[0] in _CLOSE:
            depth -= 1
            if depth < 0:
                return False
        elif token[0] in (";", "<::", "%:%:"):
            return False
    return depth == 0 and "##" not in body


class Statements:
    """A source's statements, for finding where a declaration ends. ``macros``: the
    names `unsafe_macros` gives for the build's sources and headers."""

    def __init__(self, text, raw_strings=True, macros=frozenset()):
        self.text = normalized(text)
        self.blanked = blank(self.text, raw_strings)
        self.starts = [0] + [mark.end() for mark in re.finditer("\n", self.blanked)]
        self.macros = frozenset(macros)
        # Where the compiler may read any line differently from the scan.
        self.unreadable = bool(
            _TRIGRAPH.search(text)
            or _SPLICED_PAIR.search(text)
            or _SPLIT.search(self.text)
            or _RENUMBER.search(_SPLICE.sub("", self.blanked))
        )
        self._ends = {}

    def end(self, line, name):
        """The line (from 1) of the `;` ending the last statement that starts on
        ``line`` and names ``name``, or None if it cannot be found: the line is not in
        the source, no statement starting on it names ``name`` and ends, the brackets do
        not balance, or the compiler may read the text differently (see the module)."""
        key = (line, name)
        if key not in self._ends:
            self._ends[key] = self._scan(line, name)
        return self._ends[key]

    def _scan(self, line, name):
        blanked, starts = self.blanked, self.starts
        if self.unreadable or type(line) is not int or not 1 <= line <= len(starts):
            return None
        first = start = starts[line - 1]
        end_of_line = starts[line] if line < len(starts) else len(blanked)
        depth, found, last = 0, None, None
        for mark in _TOKENS.finditer(blanked, first):
            token = mark[0]
            if token in _OPEN:
                depth += 1
            elif token in _CLOSE:
                depth -= 1
                if depth < 0:
                    # The block around the line closes; inside a statement, it is unread.
                    if blanked[start : mark.start()].strip():
                        return None
                    last = mark.start()
                    break
            elif token == "<::":
                # C++11 reads `<` `::` unless a `:` or `>` follows; C reads `<:` `:`.
                return None
            elif token == ";" and depth == 0:
                position = mark.start()
                if any(word[0] == name for word in _IDENTIFIER.finditer(blanked, start, position)):
                    found = position
                following = _NONBLANK.search(blanked, position + 1)
                if following is None or following.start() >= end_of_line:
                    last = position
                    break
                start = following.start()
        if found is None or last is None or not self._readable(line, first, last):
            return None
        return bisect.bisect_right(starts, found)

    def _readable(self, line, first, last):
        """Whether the compiler reads the text from ``line`` (at ``first``) to ``last``
        as the scan did: no directive or line splice on its lines (nor a splice onto
        its first line), and no unsafe macro named in it."""
        text, starts = self.text, self.starts
        for number in range(max(line - 1, 1), bisect.bisect_right(starts, last) + 1):
            begin = starts[number - 1]
            finish = starts[number] - 1 if number < len(starts) else len(text)
            if text[begin:finish].rstrip(" \t\f\v").endswith("\\"):
                return False
            if number >= line and _DIRECTIVE.match(self.blanked, begin):
                return False
        names = {word[0] for word in _IDENTIFIER.finditer(self.blanked, first, last)}
        return not names & self.macros
