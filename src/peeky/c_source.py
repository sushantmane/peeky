"""Where a C or C++ declaration statement ends, from the source text alone (#104).

A caller frame is inside a call made from its line. When that line is within a local's
declaration (`struct trio t = { seed,` / `{ twice(3),` / `twice(4) } };`), the
declaration is still running and the local may hold leftovers, whatever code its first
line has. The statement's end is its first `;` outside parentheses, brackets and braces,
once comments and string and character literals are blanked. Nothing is guessed: an
end that cannot be found is None.
"""

import bisect
import re

# An identifier, not the tail of a number or of another identifier (`1e5`).
_IDENTIFIER = re.compile(r"(?<!\w)[A-Za-z_]\w*")
# Where a comment or a literal may start.
_OPENING = re.compile(r"""//|/\*|["']|(?:u8|u|U|L)?R\"""")
# A raw string's prefix and delimiter: `R"x(`, `u8R"(`.
_RAW = re.compile(r'(?:u8|u|U|L)?R"([^ ()\\\t\v\f\n]{0,16})\(')
_LITERAL = {
    '"': re.compile(r'"(?:[^"\\\n]|\\[\s\S])*"?'),
    "'": re.compile(r"'(?:[^'\\\n]|\\[\s\S])*'?"),
}
_VISIBLE = re.compile(r"[^\n]")
_MARKS = re.compile(r"[()\[\]{};]")
_NONBLANK = re.compile(r"\S")
# A conditional directive can hide brackets or a `;` from a scan of the text.
_CONDITIONAL = re.compile(
    r"^[ \t]*#[ \t]*(?:if|ifdef|ifndef|elif|elifdef|elifndef|else|endif)\b", re.M
)


def blank(text):
    """``text`` with comments and the insides of string and character literals (their
    quotes too) replaced by spaces; newlines stay, so lines and columns are unchanged.
    An unterminated comment blanks to the end; an unterminated literal to its line's."""
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
            raw = _RAW.match(text, start)
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
    """Whether a backslash before the newline at ``newline`` continues the line."""
    return text[newline - 1 - (text[newline - 1] == "\r")] == "\\"


def _continues_identifier(text, i):
    # `fooR"x(` is an identifier then a string, not a raw string.
    return i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_")


def _digit_separator(text, i):
    """Whether the `'` at ``i`` is inside a number (`1'000`, C++14), not a literal."""
    start = i
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_."):
        start -= 1
    return start < i and text[start].isdigit()


class Statements:
    """A source's statements, for finding where a declaration ends."""

    def __init__(self, text):
        self.blanked = blank(text)
        self.starts = [0] + [mark.end() for mark in re.finditer("\n", self.blanked)]
        self._ends = {}

    def end(self, line, name):
        """The line (from 1) of the `;` ending the last statement that starts on
        ``line`` and names ``name``, or None if it cannot be found: the line is not in
        the source, no statement starting on it names ``name`` and ends, the brackets do
        not balance, or a conditional directive lies in between."""
        key = (line, name)
        if key not in self._ends:
            self._ends[key] = self._scan(line, name)
        return self._ends[key]

    def _scan(self, line, name):
        blanked, starts = self.blanked, self.starts
        if type(line) is not int or not 1 <= line <= len(starts):
            return None
        first = start = starts[line - 1]
        end_of_line = starts[line] if line < len(starts) else len(blanked)
        depth, found = 0, None
        for mark in _MARKS.finditer(blanked, start):
            if mark[0] in "([{":
                depth += 1
            elif mark[0] in ")]}":
                depth -= 1
                if depth < 0:
                    # The block around the line closes: no later statement starts on it.
                    break
            elif depth == 0:
                position = mark.start()
                if any(word[0] == name for word in _IDENTIFIER.finditer(blanked, start, position)):
                    found = position
                following = _NONBLANK.search(blanked, position + 1)
                if following is None or following.start() >= end_of_line:
                    break
                start = following.start()
        if found is None or _CONDITIONAL.search(blanked, first, found):
            return None
        return bisect.bisect_right(starts, found)
