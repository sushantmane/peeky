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
_PAIRS = {"(": "(", ")": "(", "[": "[", "]": "[", "<:": "[", ":>": "[", "{": "{", "}": "{"}
_PAIRS.update({"<%": "{", "%>": "{"})
# A word or a punctuator, as statements are read: keywords, labels and brackets.
_WORDS = re.compile(r"(?<!\w)[A-Za-z_]\w*|" + _TOKENS.pattern)
_WORD = re.compile(r"[A-Za-z_]\w*")
_LAST_WORD = re.compile(r"[A-Za-z_]\w*$")
# Statements whose header (in parentheses) can declare a variable for their body.
_HEADERS = frozenset(("for", "if", "while", "switch", "catch"))
# Keywords a name follows where it is used, not declared.
_USES = frozenset(("return", "case", "sizeof", "delete", "throw", "co_return", "co_yield"))
_USES = _USES | {"else", "do", "goto", "typeid", "alignof", "_Alignof", "new"}
# What a statement or block follows: `else {`, `do {`, `try {`.
_LEADING = frozenset(("else", "do", "try"))
# A label a jump can reach, in a macro's body: at its start or after `;`, `{` or `}`.
_LABEL = re.compile(r"(?:^|[;{}])\s*[A-Za-z_]\w*\s*:(?![:>])")
_INCLUDE = re.compile(r"[ \t\f\v]*(?:#|%:)[ \t\f\v]*(?:include|include_next|import)\b")
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
    return macro_hazards(texts, raw_strings)["brackets"]


def macro_hazards(texts, raw_strings=True):
    r"""The macros ``texts`` define, by what their expansion may hide from a scan of the
    text: ``brackets`` (`unsafe_macros`), ``blocks`` (the same but for a `;`: a bracket
    it does not close), ``labels`` (a `case`, `default` or label a jump can reach, `x:`)
    and ``gotos`` (a `goto`). A macro naming such a macro counts too, and every macro of
    a source with a trigraph, or a splice before `\n\r`, counts for all of them."""
    uses = {}
    found = {"brackets": set(), "blocks": set(), "labels": set(), "gotos": set()}
    for text in texts:
        # Spliced as the compiler splices lines, before comments and literals.
        code = blank(_SPLICE.sub("", normalized(text)), raw_strings)
        trigraphs = _TRIGRAPH.search(text) is not None or _SPLICED_PAIR.search(text)
        for define in _DEFINE.finditer(code):
            name, body = define[1], define[3]
            if define[2]:
                close = body.find(")")
                if close < 0:
                    for names in found.values():
                        names.add(name)
                    continue
                body = body[close + 1 :]
            words = {word[0] for word in _IDENTIFIER.finditer(body)}
            uses.setdefault(name, set()).update(words)
            if trigraphs or not _balanced(body):
                found["brackets"].add(name)
            if trigraphs or not _balanced(body.replace(";", " ")):
                found["blocks"].add(name)
            if trigraphs or words & {"case", "default"} or _LABEL.search(body):
                found["labels"].add(name)
            if trigraphs or "goto" in words:
                found["gotos"].add(name)
    for names in found.values():
        while more := {name for name, used in uses.items() if name not in names and used & names}:
            names |= more
    return {kind: frozenset(names) for kind, names in found.items()}


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
    names `unsafe_macros` gives for the build's sources and headers; ``hazards``: what
    `macro_hazards` gives for them (its ``brackets`` replace ``macros``)."""

    def __init__(self, text, raw_strings=True, macros=frozenset(), hazards=None):
        self.text = normalized(text)
        self.blanked = blank(self.text, raw_strings)
        self.starts = [0] + [mark.end() for mark in re.finditer("\n", self.blanked)]
        hazards = hazards or {"brackets": macros}
        self.macros = frozenset(hazards.get("brackets", ()))
        # What `jumped_past` cannot see through: a label, or a bracket left open.
        self.opaque = frozenset(hazards.get("labels", ())) | frozenset(hazards.get("blocks", ()))
        self.gotos = frozenset(hazards.get("gotos", ()))
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

    def declared_through(self, line, name):
        """For the frame stopped in: the line (from 1) by which the declaration of
        ``name`` that starts on ``line`` has run, or None if unknown (as for `end`).
        Unlike `end`, a variable a statement's header declares (`for (int i = 0; ...)`,
        `if (auto p = f())`, `catch (E& e)`) has run with its part of the header, so the
        lines of its body are past it; a plain declaration runs to its `;`."""
        found = self.declaration(line, name)
        return None if found is None else bisect.bisect_right(self.starts, found[0])

    def declaration(self, line, name):
        """``(position, depth)``: where the declaration of ``name`` on ``line`` ends
        (the `;` or `)` after it) and how many brackets of its statement are open there,
        or None if unknown. Of the statements starting on the line, the last naming
        ``name`` counts: a later end only withholds more."""
        key = ("declaration", line, name)
        if key not in self._ends:
            self._ends[key] = self._declaration(line, name)
        return self._ends[key]

    def _declaration(self, line, name):
        blanked, starts = self.blanked, self.starts
        if self.unreadable or type(line) is not int or not 1 <= line <= len(starts):
            return None
        first = position = starts[line - 1]
        end_of_line = starts[line] if line < len(starts) else len(blanked)
        found, last = None, first
        while (token := _WORDS.search(blanked, position)) is not None:
            word, start = token[0], token.start()
            if start >= end_of_line:
                break
            following = _WORDS.search(blanked, token.end())
            after = following[0] if following else None
            if word in ("{", "<%", "}", "%>", ";") or word in _LEADING:
                # A block's edge, an empty statement, or what a statement follows.
                position = token.end()
                continue
            if (
                word == "case"
                or (word == "default" and after == ":")
                or (_WORD.fullmatch(word) and after == ":")
            ):
                colon = re.compile(r"(?<!:):(?![:>])").search(blanked, token.end())
                if colon is None:
                    return None
                position = colon.end()
                continue
            if word in _HEADERS:
                opening = following
                if after == "constexpr":
                    opening = _WORDS.search(blanked, following.end())
                if opening is not None and opening[0] == "(":
                    closed = self._closing(opening.start())
                    if closed is None:
                        return None
                    closing, semicolon = closed
                    last = max(last, closing)
                    if self._names(opening.end(), closing, name):
                        if self._declares(closing + 1, end_of_line, name):
                            # Maybe declared again after the header: read it whole.
                            pass
                        elif semicolon is not None and self._names(opening.end(), semicolon, name):
                            found = (semicolon, 1)
                            position = closing + 1
                            continue
                        else:
                            found = (closing, 0)
                            position = closing + 1
                            continue
                    else:
                        # The body is a statement of its own.
                        position = closing + 1
                        continue
            end = self._statement(start)
            if end is None:
                return None
            last = max(last, end)
            if self._names(start, end, name):
                found = (end, 0)
            position = end + 1
        if found is None or not self._readable(line, first, max(last, found[0])):
            return None
        return found

    def _statement(self, start):
        """Where the plain statement at ``start`` ends (its `;`), or None."""
        depth = 0
        for mark in _TOKENS.finditer(self.blanked, start):
            token = mark[0]
            if token in _OPEN:
                depth += 1
            elif token in _CLOSE:
                depth -= 1
                if depth < 0:
                    return None
            elif token == "<::":
                return None
            elif token == ";" and depth == 0:
                return mark.start()
        return None

    def _closing(self, opening):
        """(position of the bracket closing the one at ``opening``, position of the
        first `;` directly inside it or None), or None if they do not balance."""
        depth, semicolon = 0, None
        for mark in _TOKENS.finditer(self.blanked, opening):
            token = mark[0]
            if token in _OPEN:
                depth += 1
            elif token in _CLOSE:
                depth -= 1
                if depth == 0:
                    return mark.start(), semicolon
                if depth < 0:
                    return None
            elif token == "<::":
                return None
            elif token == ";" and depth == 1 and semicolon is None:
                semicolon = mark.start()
        return None

    def _declares(self, start, end, name):
        """Whether ``name`` may be declared between ``start`` and ``end``: it follows a
        name other than a keyword that only uses it (`int i`, `return i`), or `*`, `&`
        or `>` (`T *i`, `vector<int> i`)."""
        blanked = self.blanked
        for word in _IDENTIFIER.finditer(blanked, start, end):
            if word[0] != name:
                continue
            at = word.start() - 1
            while at >= start and blanked[at].isspace():
                at -= 1
            if at < start:
                continue
            if blanked[at] in "*&>":
                return True
            previous = self._word_before(word.start())
            if previous is not None and previous not in _USES:
                return True
        return False

    def _names(self, start, end, name):
        return any(word[0] == name for word in _IDENTIFIER.finditer(self.blanked, start, end))

    def jumped_past(self, line, name, stop):
        """For a local whose declaration starts on ``line``, read at a stop on line
        ``stop`` below it: the line of a label between the declaration's end and the
        end of the stop's line that a jump can reach without running the declaration,
        False if there is none, or None if unknown.

        Such a label is a `case` or `default` of a `switch` whose body holds the
        declaration (the switch jumps there), or a label a `goto` before the
        declaration's end names (or any label, with a computed `goto` or a macro that
        may `goto` named before it). Unknown: the declaration's end, a block closing
        before the stop, an `#include`, or a macro that may hold a label or a bracket in
        between."""
        key = ("jumped", line, name, stop)
        if key not in self._ends:
            self._ends[key] = self._jumped_past(line, name, stop)
        return self._ends[key]

    def _jumped_past(self, line, name, stop):
        found = self.declaration(line, name)
        starts = self.starts
        if found is None or type(stop) is not int or not line < stop <= len(starts):
            return None
        position, depth = found
        blanked = self.blanked
        limit = starts[stop] if stop < len(starts) else len(blanked)
        for number in range(line, stop + 1):
            if _INCLUDE.match(blanked, starts[number - 1]):
                return None
        marks = list(_WORDS.finditer(blanked, position + 1, limit))
        for index, mark in enumerate(marks):
            word = mark[0]
            after = marks[index + 1][0] if index + 1 < len(marks) else None
            if word in self.opaque:
                return None
            if word in _OPEN:
                depth += 1
            elif word in _CLOSE:
                depth -= 1
                if depth < 0:
                    # Its block closes before the stop: not the scope Peeky reads.
                    return None
            elif word == "<::":
                return None
            elif word == "case" or (word == "default" and after == ":"):
                body = self._switch_body(mark.start())
                # A switch whose body opens before the declaration ends holds it.
                if body is None or body < position:
                    return bisect.bisect_right(starts, mark.start())
            elif _WORD.fullmatch(word) and after == ":" and self._jumped_to(word, position):
                return bisect.bisect_right(starts, mark.start())
        return False

    def _jumped_to(self, label, before):
        """Whether a `goto` before ``before`` may jump to ``label``."""
        blanked = self.blanked
        if "computed" not in self.__dict__:
            self.computed = re.search(r"(?<!\w)goto\s*\*", blanked) is not None
        if self.computed:
            return True
        if re.compile(rf"(?<!\w)goto\s+{re.escape(label)}(?!\w)").search(blanked, 0, before):
            return True
        return bool(self.gotos) and any(
            word[0] in self.gotos for word in _IDENTIFIER.finditer(blanked, 0, before)
        )

    def _switch_body(self, position):
        """Where the body (`{`) of the innermost `switch` around ``position`` opens, or
        None if there is none or the source's brackets do not balance."""
        if "braces" not in self.__dict__:
            self.braces = self._braces()
        if self.braces is None:
            return None
        opens, closes, parents, switches = self.braces
        index = bisect.bisect_right(opens, position) - 1
        while index >= 0 and closes[index] < position:
            index = parents[index]
        while index >= 0:
            if switches[index]:
                return opens[index]
            index = parents[index]
        return None

    def _braces(self):
        """Every brace pair: (opens, closes, parent index or -1, whether it is a
        `switch`'s body), by where they open; None if the brackets do not balance."""
        blanked = self.blanked
        stack, braces, switches = [], [], {}
        closed_paren = None
        for mark in _TOKENS.finditer(blanked):
            token = mark[0]
            if token in _OPEN:
                if token in ("{", "<%"):
                    # `switch (...) {`: the brace follows the header's `)` directly.
                    switches[mark.start()] = (
                        closed_paren is not None
                        and not blanked[closed_paren[0] : mark.start()].strip()
                        and self._word_before(closed_paren[1]) == "switch"
                    )
                stack.append((token, mark.start()))
            elif token in _CLOSE:
                if not stack or _PAIRS[stack[-1][0]] != _PAIRS[token]:
                    return None
                kind, opened = stack.pop()
                if kind in ("{", "<%"):
                    braces.append((opened, mark.start()))
                closed_paren = (mark.end(), opened) if token == ")" else None
                continue
            elif token == "<::":
                return None
            closed_paren = None
        if stack:
            return None
        braces.sort()
        opens = [opened for opened, _ in braces]
        closes = [closed for _, closed in braces]
        parents, open_stack = [], []
        for index, (opened, _) in enumerate(braces):
            while open_stack and closes[open_stack[-1]] < opened:
                open_stack.pop()
            parents.append(open_stack[-1] if open_stack else -1)
            open_stack.append(index)
        return opens, closes, parents, [switches[opened] for opened in opens]

    def _word_before(self, position):
        """The identifier just before ``position`` (spaces skipped), or None."""
        blanked, at = self.blanked, position - 1
        while at >= 0 and blanked[at].isspace():
            at -= 1
        end = at + 1
        while at >= 0 and (blanked[at].isalnum() or blanked[at] == "_"):
            at -= 1
        word = blanked[at + 1 : end]
        return word if _WORD.fullmatch(word) else None
