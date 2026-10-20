r"""Where a C or C++ declaration statement ends, from the source text alone (#104).

A caller frame is inside a call made from its line. When that line is within a local's
declaration (`struct trio t = { seed,` / `{ twice(3),` / `twice(4) } };`), the
declaration is still running and the local may hold leftovers, whatever code its first
line has. The statement's end is its first `;` outside parentheses, brackets and braces
(digraphs too), once comments and string and character literals are blanked.

Lines are counted as the compilers count them (`normalized`), and C's trigraphs are
read as C reads them (`-std=c11`; C++17, Peeky's C++, has none). Nothing is guessed: an
end that cannot be found is None, and so is one where the text the scan reads may differ
from what the compiler reads. That is anywhere in a file with a `#line`, a line splice
inside a token or before `\n\r`; and, from the declaration's line
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
_INCLUDED = re.compile(r'(?:include|include_next|import)\s*(?:"([^"\n]+)"|<([^>\n]+)>)')
_NONBLANK = re.compile(r"\S")
# `??/` is a backslash and `??<` a brace where trigraphs are on (C before C23, as
# `-std=c11` builds it; C++17 has none).
_TRIGRAPH = re.compile(r"\?\?([=(/)'<!>-])")
_TRIGRAPHS = dict(zip("=(/)'<!>-", "#[\\]^{|}~"))
# Words a `(` follows in an expression, never in a function's head.
_EXPRESSIONS = _USES | {"decltype", "noexcept", "and", "or", "not", "static_assert", "requires"}
# What may stand between a function's (or a lambda's) parameters and its body.
_QUALIFIED = re.compile(
    r"(?:\s|const\b|volatile\b|noexcept\b|override\b|final\b|mutable\b|&|->[^;{}()]*)*"
)
# A directive's line (`%:` is `#`).
_DIRECTIVE = re.compile(r"[ \t\f\v]*(?:#|%:)")
# `#line 7` and a line marker (`# 7 "x.c"`) renumber the lines after them.
_RENUMBER = re.compile(r"^[ \t\f\v]*(?:#|%:)[ \t\f\v]*(?:line\b|[0-9])", re.M)
_DEFINE = re.compile(r"^[ \t\f\v]*(?:#|%:)[ \t\f\v]*define[ \t\f\v]+([A-Za-z_]\w*)(\(?)(.*)$", re.M)


def normalized(text):
    r"""``text`` with each line break, as the compilers count them, written `\n`."""
    return _BREAK.sub("\n", text) if "\r" in text else text


def translated(text, trigraphs):
    r"""``text`` `normalized`, and with each trigraph replaced as C's first translation
    phase does (also inside literals and comments) when ``trigraphs``; lines keep their
    numbers."""
    text = normalized(text)
    if trigraphs and "??" in text:
        text = _TRIGRAPH.sub(lambda found: _TRIGRAPHS[found[1]], text)
    return text


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


def unsafe_macros(texts, raw_strings=True, trigraphs=None):
    r"""The names of the macros ``texts`` define that a statement scan cannot see
    through: an expansion that may hold a bracket it does not close, a `;` outside its
    brackets, a `<::`, or a pasted token (`<` ## `%` is a brace), or that names such a
    macro. Every macro a source with a splice before `\n\r` defines counts.
    ``trigraphs``: C's (by default, without ``raw_strings``: C)."""
    return macro_hazards(texts, raw_strings, trigraphs)["brackets"]


def macro_hazards(texts, raw_strings=True, trigraphs=None):
    r"""The macros ``texts`` define, by what their expansion may hide from a scan of the
    text: ``brackets`` (`unsafe_macros`), ``blocks`` (the same but for a `;`: a bracket
    it does not close), ``labels`` (a `case`, `default` or label a jump can reach, `x:`)
    and ``gotos`` (a `goto`). A macro naming such a macro counts too, and every macro of
    a source with a splice before `\n\r` counts for all of them."""
    trigraphs = not raw_strings if trigraphs is None else trigraphs
    uses = {}
    found = {"brackets": set(), "blocks": set(), "labels": set(), "gotos": set()}
    for text in texts:
        # Spliced as the compiler splices lines, before comments and literals.
        code = blank(_SPLICE.sub("", translated(text, trigraphs)), raw_strings)
        unknown = _SPLICED_PAIR.search(text) is not None
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
            if unknown or not _balanced(body):
                found["brackets"].add(name)
            if unknown or not _balanced(body.replace(";", " ")):
                found["blocks"].add(name)
            if unknown or words & {"case", "default"} or _LABEL.search(body):
                found["labels"].add(name)
            if unknown or "goto" in words:
                found["gotos"].add(name)
    for names in found.values():
        while more := {name for name, used in uses.items() if name not in names and used & names}:
            names |= more
    return {kind: frozenset(names) for kind, names in found.items()}


def _balanced(body):
    """Whether a macro's blanked body closes each bracket it opens and holds no `;`
    outside them (a statement expression's `({ a; b; })` is fine), `<::` or token
    paste."""
    depth = 0
    for token in _TOKENS.finditer(body):
        if token[0] in _OPEN:
            depth += 1
        elif token[0] in _CLOSE:
            depth -= 1
            if depth < 0:
                return False
        elif token[0] in ("<::", "%:%:") or (token[0] == ";" and depth == 0):
            return False
    return depth == 0 and "##" not in body


class Statements:
    """A source's statements, for finding where a declaration ends. ``macros``: the
    names `unsafe_macros` gives for the build's sources and headers; ``hazards``: what
    `macro_hazards` gives for them (its ``brackets`` replace ``macros``)."""

    def __init__(self, text, raw_strings=True, macros=frozenset(), hazards=None, trigraphs=None):
        # C (`-std=c11`) has trigraphs and no raw strings; C++17 the other way round.
        self.raw_strings = raw_strings
        self.text = translated(text, not raw_strings if trigraphs is None else trigraphs)
        self.blanked = blank(self.text, raw_strings)
        self.starts = [0] + [mark.end() for mark in re.finditer("\n", self.blanked)]
        hazards = hazards or {"brackets": macros}
        self.macros = frozenset(hazards.get("brackets", ()))
        # What `jumped_past` cannot see through: a label, or a bracket left open.
        self.opaque = frozenset(hazards.get("labels", ())) | frozenset(hazards.get("blocks", ()))
        # What may hide a bracket from `begins`: an unclosed one (a `;` alone hides none).
        self.blocks = frozenset(hazards.get("blocks", ())) if "blocks" in hazards else self.macros
        self.gotos = frozenset(hazards.get("gotos", ()))
        # Where the compiler may read any line differently from the scan.
        self.unreadable = bool(
            _SPLICED_PAIR.search(text)
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
        """``(position, depth, header, settled)``: where the declaration of ``name`` on
        ``line`` ends (the `;` or `)` after it), how many brackets of its statement are
        open there, whether a statement's header declares it (`for (int i = 0; ...)`),
        and whether that header sets it once, with no call (`_settled`); None if unknown.
        Of the statements starting on the line, the last naming ``name`` counts: a later
        end only withholds more."""
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
                            settled = self._settled(word, opening.end(), semicolon)
                            found = (semicolon, 1, True, settled)
                            position = closing + 1
                            continue
                        else:
                            part = opening.end() if semicolon is None else semicolon + 1
                            settled = self._settled(word, part, closing)
                            found = (closing, 0, True, settled)
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
            # After a header declaring it, its body on the line only uses it.
            if self._names(start, end, name) and not (found is not None and found[2]):
                found = (end, 0, False, False)
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

    def _settled(self, keyword, start, end):
        """Whether a header's variable, declared between ``start`` and ``end``, is set
        once, by no call: not a `while`'s (run again on each pass), nor one whose
        initializer may call (a `(`, a `{` that may construct, or a range-`for`'s `:`,
        whose iterator calls)."""
        part = self.blanked[start:end]
        return keyword != "while" and not re.search(r"[({]|<%|(?<!:):(?![:>])", part)

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
        """Whether ``name`` may be declared between ``start`` and ``end``: not inside an
        expression (after `=`, `?`, or within parentheses or brackets), and following a
        name other than a keyword that only uses it (`int i`, not `return i`), or `*`,
        `&` or `>` (`T *i`, `vector<int> i`), or a `,` of a declarator list."""
        blanked = self.blanked
        for word in _IDENTIFIER.finditer(blanked, start, end):
            if word[0] != name:
                continue
            at, depth, used, listed = word.start() - 1, 0, False, False
            while at >= start:
                char = blanked[at]
                if char in ")]":
                    depth += 1
                elif char in "([":
                    if depth == 0:
                        # An argument or a subscript.
                        used = True
                        break
                    depth -= 1
                elif depth == 0 and char == ",":
                    listed = True
                elif depth == 0 and char in "=?" and not listed:
                    used = True
                    break
                elif depth == 0 and char in ";{}:":
                    break
                at -= 1
            if used:
                continue
            if listed:
                # A declarator list's (or, over-cautiously, a comma operator's).
                return True
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

    def jumped_past(self, line, name, stop, span=None):
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
        key = ("jumped", line, name, stop, span)
        if key not in self._ends:
            found = self.declaration(line, name)
            self._ends[key] = (
                None
                if found is None
                else self._labels(found[0], found[1], found[2], line, stop, span)
            )
        return self._ends[key]

    def jumped_past_include(self, line, stop, span=None):
        """`jumped_past` for a local an `#include` on ``line`` declares: a label between
        that line and the stop's that a jump can reach past it, False, or None."""
        starts = self.starts
        if type(line) is not int or not 1 <= line < len(starts):
            return None
        return self._labels(starts[line] - 1, 0, False, line, stop, span)

    def _labels(self, position, depth, header, line, stop, span):
        """See `jumped_past`; ``position`` and ``depth``: where the declaration ends."""
        starts = self.starts
        if type(stop) is not int or not line < stop <= len(starts):
            return None
        blanked = self.blanked
        limit = starts[stop] if stop < len(starts) else len(blanked)
        # After the line the declaration ends on (an `#include` that declares it, too).
        for number in range(bisect.bisect_right(starts, position) + 1, stop + 1):
            if _INCLUDE.match(blanked, starts[number - 1]) and not self._transparent(number):
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
            elif (
                _WORD.fullmatch(word)
                and after == ":"
                and self._jumped_to(word, position, header, span)
            ):
                return bisect.bisect_right(starts, mark.start())
        return False

    def class_extent(self, line):
        """(first, last): the lines from ``line``, where a class, struct or union is
        defined (`struct Local {`), to its body's closing brace; None if no such head
        and body follow from that line (a macro, a declaration only, unread text)."""
        blanked, starts = self.blanked, self.starts
        if self.unreadable or type(line) is not int or not 1 <= line <= len(starts):
            return None
        end_of_line = starts[line] if line < len(starts) else len(blanked)
        head = next(
            (
                word
                for word in _WORDS.finditer(blanked, starts[line - 1], end_of_line)
                if word[0] in ("struct", "class", "union")
            ),
            None,
        )
        if head is None:
            return None
        # The head runs to its body's brace: attributes, a name, `final`, bases.
        depth, opening = 0, None
        for mark in re.compile(r"[;=(){}\[\]]|<%").finditer(blanked, head.end()):
            if mark[0] in "([":
                depth += 1
            elif mark[0] in ")]":
                depth -= 1
            elif depth == 0:
                opening = mark if mark[0] in ("{", "<%") else None
                break
            if depth < 0:
                break
        if opening is None:
            return None
        if "braces" not in self.__dict__:
            self.braces = self._braces()
        if self.braces is None:
            return None
        opens, closes, _, _ = self.braces
        index = bisect.bisect_left(opens, opening.start())
        if index >= len(opens) or opens[index] != opening.start():
            return None
        return line, bisect.bisect_right(starts, closes[index])

    def includes(self):
        """``(line, name)`` for each `#include "name"` (or `<name>`) directive; name
        None where it is not written plainly (a macro)."""
        found = []
        for number, start in enumerate(self.starts, 1):
            if _INCLUDE.match(self.blanked, start):
                end = self.starts[number] if number < len(self.starts) else len(self.text)
                named = _INCLUDED.search(self.text, start, end)
                found.append((number, (named[1] or named[2]) if named else None))
        return found

    def included_after(self, line, name):
        """Whether an `#include` may bring code between the declaration of ``name`` on
        ``line`` and the end of its block (a `case` there can skip it): True, False, or
        None if the declaration's end is unknown. Unknown braces reach the file's end."""
        found = self.declaration(line, name)
        if found is None:
            return None
        blanked, starts = self.blanked, self.starts
        block, _ = self._enclosing(found[0])
        end = block[1] if block is not None else len(blanked)
        for number in range(line, bisect.bisect_right(starts, end) + 1):
            if _INCLUDE.match(blanked, starts[number - 1]) and not self._transparent(number):
                return True
        return False

    def _transparent(self, line):
        """Whether the `#include` on ``line`` brings in only text a jump cannot use
        (``self.included(line)`` gives that file's Statements, or None)."""
        reader = getattr(self, "included", None)
        found = reader(line) if reader is not None else None
        return found is not None and found.jumpless()

    def jumpless(self):
        """Whether this text, `#include`d inside a function, can hold no label, `goto` or
        unbalanced brace: readable, no `case`, `default:`, `goto`, `name:`, `#include`,
        nor a macro that may hold a label, a jump or an open bracket."""
        if "jumpless_text" not in self.__dict__:
            blanked = self.blanked
            words = list(_WORDS.finditer(blanked))
            labelled = any(
                word[0] in ("case", "goto")
                or word[0] in self.opaque
                or word[0] in self.gotos
                or (
                    _WORD.fullmatch(word[0])
                    and index + 1 < len(words)
                    and words[index + 1][0] == ":"
                )
                for index, word in enumerate(words)
            )
            self.jumpless_text = not (
                self.unreadable
                or labelled
                or self._braces() is None
                or any(_INCLUDE.match(blanked, start) for start in self.starts)
            )
        return self.jumpless_text

    def _enclosing(self, position):
        """((open, close) of the innermost brace pair around ``position``, (open, close)
        of the outermost), or (None, None) if there is none or the braces do not
        balance."""
        if "braces" not in self.__dict__:
            self.braces = self._braces()
        if self.braces is None:
            return None, None
        opens, closes, parents, _ = self.braces
        index = bisect.bisect_right(opens, position) - 1
        while index >= 0 and closes[index] < position:
            index = parents[index]
        if index < 0:
            return None, None
        outer = index
        while parents[outer] >= 0:
            outer = parents[outer]
        return (opens[index], closes[index]), (opens[outer], closes[outer])

    def _jumped_to(self, label, position, header=False, span=None):
        """Whether a `goto` can reach ``label`` (after a declaration ending at
        ``position``) without running the declaration: one before its end, or one
        outside its block in the same function, since a jump back into a block starts the
        local's lifetime anew (C11 6.2.4); for a header's variable, or with the braces
        unknown, any. Also a macro that may `goto` named there, or a computed `goto`.
        The function is its lines ``span`` (first, last) when given, else the outermost
        braces around the declaration (in C++ maybe a namespace's: more counts)."""
        blanked = self.blanked
        if "computed" not in self.__dict__:
            self.computed = re.search(r"(?<!\w)goto\s*\*", blanked) is not None
        if self.computed:
            return True
        block, function = self._enclosing(position)
        starts = self.starts
        if span is not None and 1 <= span[0] <= span[1] <= len(starts):
            low = starts[span[0] - 1]
            high = (starts[span[1]] if span[1] < len(starts) else len(blanked)) - 1
        else:
            low, high = function if function is not None else (0, len(blanked))

        def counts(at):
            return block is None or header or at < position or not block[0] <= at <= block[1]

        target = re.compile(rf"(?<!\w)goto\s+{re.escape(label)}(?!\w)")
        if any(counts(found.start()) for found in target.finditer(blanked, low, high + 1)):
            return True
        return bool(self.gotos) and any(
            counts(word.start())
            for word in _IDENTIFIER.finditer(blanked, low, high + 1)
            if word[0] in self.gotos
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

    def begins(self, line):
        """Whether the code of ``line`` starts a statement (True), or continues one that
        an earlier line started (False: `long t = s.a` then `+ f();`, a call's arguments,
        a `for` header's parts, an initializer's rows), as a stop at the line's start
        finds it (#105). Part-way through a statement, what it has read may be held in a
        register or a temporary, which a change to the variable does not reach.

        Every bracket around the line, up to its function's (or lambda's) body, must be
        a block of statements: an `if`, `for`, `while`, `switch` or `catch` body, an
        `else`, `do` or `try` block, a block after a statement or a label. Anything
        else continues a statement: parentheses, brackets, an initializer, `new T[1]{`,
        `decltype(W{}){`, a compound literal, a class body, a GNU statement expression
        `({`. None if unknown: the compiler may read the text differently (see the
        module), the line is a directive or holds no code, the brackets do not balance,
        a directive lies between the line and the code before it, or a macro that may
        hide a bracket is named in its function before it."""
        if self.unreadable or type(line) is not int or not 1 <= line <= len(self.starts):
            return None
        code, pairs = self._code()
        if pairs is None:
            return None
        begin = self.starts[line - 1]
        finish = self.starts[line] if line < len(self.starts) else len(code)
        first = _NONBLANK.search(code, begin, finish)
        if first is None:
            return None
        first = first.start()
        if line > 1 and code[self.starts[line - 2] : begin - 1].rstrip(" \t\f\v").endswith("\\"):
            # Spliced onto the line above.
            return None
        # A line starting with `}` is the last of the block it closes.
        verdict, body = self._function_body(first, code.startswith(("}", "%>"), first))
        if verdict is not True:
            return verdict
        if {word[0] for word in _IDENTIFIER.finditer(code, body, first)} & self.blocks:
            return None
        before = self._unattributed(first)
        if before is None or before[1] <= body:
            return True
        token, at = before
        if any(
            _DIRECTIVE.match(self.blanked, self.starts[number - 1])
            for number in range(bisect.bisect_right(self.starts, at) + 1, line)
        ):
            # `t = p.x` / `#if 0` / `;` / `#endif` / `+ g();`: what the compiler reads
            # before the line is not known.
            return None
        if token == ";" or token in _LEADING:
            return True
        if token in ("{", "<%"):
            return True  # a block of statements: `_function_body` checked it
        if token in ("}", "%>"):
            opening = self._opening(at)
            return opening is not None and self._brace(opening) == "statement"
        if token == ")":
            opening = self._opening(at)
            return opening is not None and self._word_before(opening) in _HEADERS - {"catch"}
        if token == ":":
            return self._label(at)
        return False

    def statement_span(self, line):
        """(``line``, the last line of the statements that start on it): where the last
        one's `;` is (a statement's header, `if (...)`, ends at its `)`). None if
        unknown: no statement starts on the line, an end is not found, or the compiler
        may read the text differently (a directive or splice on its lines, a macro that
        may hide a bracket or a `;` in it; see the module)."""
        if self.unreadable or type(line) is not int or not 1 <= line <= len(self.starts):
            return None
        if self._code()[1] is None:
            return None
        return self._span_from(line, self.starts[line - 1])

    def opening_statements(self, line):
        """For a function's first line, where its body opens (`long one(struct P p) {
        long t =` / `p.x + g();`, a one-line lambda): (``line``, the last line of the
        statements that start on it after the body's `{`), as ``statement_span`` reads
        them. None if unknown: no body (a function's or a lambda's) opens on the line,
        more than one does, no code follows it on the line, an end is not found, or the
        compiler may read the text differently (see ``statement_span``)."""
        if self.unreadable or type(line) is not int or not 1 <= line <= len(self.starts):
            return None
        code, pairs = self._code()
        if pairs is None:
            return None
        begin = self.starts[line - 1]
        finish = self.starts[line] if line < len(self.starts) else len(code)
        bodies = [
            found.start()
            for found in re.compile(r"\{|<%").finditer(code, begin, finish)
            if self._brace(found.start()) in ("function", "lambda")
        ]
        if len(bodies) != 1:
            return None
        opened = bodies[0] + (1 if code.startswith("{", bodies[0]) else 2)
        following = _NONBLANK.search(code, opened, finish)
        if following is None or code.startswith(("}", "%>"), following.start()):
            return None
        verdict, body = self._function_body(following.start())
        if verdict is not True or body != bodies[0]:
            return None
        return self._span_from(line, opened)

    def _span_from(self, line, begin):
        """(``line``, the last line of the statements that start on it from ``begin``), or
        None (see ``statement_span``)."""
        code, pairs = self._code()
        finish = self.starts[line] if line < len(self.starts) else len(code)
        position, last = begin, begin
        while (token := _WORDS.search(code, position)) is not None and token.start() < finish:
            word, start = token[0], token.start()
            following = _WORDS.search(code, token.end())
            after = following[0] if following else None
            if word in ("{", "<%", "}", "%>", ";") or word in _LEADING:
                position = token.end()
                continue
            if word in ("[", "<:") and after in ("[", "<:"):
                # An attribute (`[[likely]]`).
                closing = pairs.get(start)
                if closing is None:
                    return None
                position, last = closing + 1, max(last, closing)
                continue
            if word == "case" or (_WORD.fullmatch(word) and after == ":"):
                colon = re.compile(r"(?<!:):(?![:>])").search(code, token.end())
                if colon is None:
                    return None
                position, last = colon.end(), max(last, colon.start())
                continue
            opening = following
            if word in _HEADERS and after == "constexpr":
                opening = _WORDS.search(code, following.end())
            if word in _HEADERS and opening is not None and opening[0] == "(":
                closing = pairs.get(opening.start())
                if closing is None:
                    return None
                position, last = closing + 1, max(last, closing)
                continue
            end = self._statement(start)
            if end is None:
                return None
            position, last = end + 1, max(last, end)
        if not self._readable(line, begin, last):
            return None
        return line, bisect.bisect_right(self.starts, last)

    def old_style(self, line):
        """Whether ``line`` is in the body of a K&R-style definition (`long f(a) long a;
        {`), whose parameters are declared between its `)` and its body: the outermost
        brace around the line's code opens after a `;` at file scope. Such a body is no
        function's for `begins`, which then says False."""
        if self.unreadable or type(line) is not int or not 1 <= line <= len(self.starts):
            return False
        code, pairs = self._code()
        if pairs is None:
            return False
        begin = self.starts[line - 1]
        finish = self.starts[line] if line < len(self.starts) else len(code)
        first = _NONBLANK.search(code, begin, finish)
        if first is None:
            return False
        braces = [
            opened
            for opened, closed in pairs.items()
            if opened < first.start() <= closed and code.startswith(("{", "<%"), opened)
        ]
        if not braces:
            return False
        before = self._unattributed(min(braces))
        return before is not None and before[0] == ";"

    def _function_body(self, position, closing=False):
        """(verdict, where the body opens) for ``position``: True if every bracket around
        it, up to the body of its function or lambda, is a block of statements (see
        `begins`); False if one is not, or there is no function around it. ``closing``:
        the position is a `}`, of the block it closes."""
        code, pairs = self._code()
        around = sorted(
            (
                opened
                for opened, closed in pairs.items()
                if opened < position < closed or (closing and closed == position)
            ),
            reverse=True,
        )
        # Inside a GNU statement expression, `({ ... })`, anywhere around it: what reads
        # a value there is part of an enclosing statement, whose start is unknown.
        if any(
            code.startswith(("{", "<%"), opening)
            and (before := self._before(opening)) is not None
            and before[0] == "("
            for opening in around
        ):
            return False, None
        for index, opening in enumerate(around):
            if not code.startswith(("{", "<%"), opening):
                return False, None
            kind = self._brace(opening)
            if kind == "lambda":
                return True, opening
            if kind == "function":
                outer = around[index + 1] if index + 1 < len(around) else None
                if outer is None or self._container(outer):
                    return True, opening
                # A function is defined only at file scope or in a class's or namespace's
                # body: this one is a statement's block (a macro's: `FOR_EACH(x) {`, a
                # `for` header `REPEAT(k, 1) {` hides), and the walk goes on outwards.
                continue
            if kind != "statement":
                return False, None
        return False, None

    def _container(self, opening):
        """Whether the brace at ``opening`` opens a body functions are defined in: a
        class's, struct's or union's (`struct S : B {`, `template <class T> class C {`),
        a namespace's or a linkage specification's (`extern "C" {`). Not a function's
        (`struct P make(void) {`), an initializer's (`struct P p = {`) or anything in
        brackets."""
        code = self._code()[0]
        if not code.startswith(("{", "<%"), opening):
            return False
        head, at, angles = [], opening, 0
        for _ in range(256):
            before = self._before(at)
            if before is None:
                break
            token, start = before
            if token in (";", "{", "}", "<%", "%>"):
                break
            if token in (")", "]", ":>"):
                # A bracketed group: an attribute's or `alignas(8)` (fine), parameters.
                opened = self._opening(start)
                if opened is None:
                    return False
                head.append("()" if token == ")" else "[]")
                at = opened
                continue
            if set(token) <= {"<", ">"}:
                # Read backwards, a template's arguments close first (`<int N = 3>`).
                angles += token.count(">") - token.count("<")
            elif token in ("(", "[", "<:") or (token == "=" and angles <= 0):
                return False
            head.append(token)
            at = start
        else:
            return False
        head.reverse()
        groups = [i for i, token in enumerate(head) if token == "()"]
        if "namespace" in head:
            return not groups
        if head[:1] == ["extern"]:
            return not groups and len(head) <= 3
        if not {"class", "struct", "union"} & set(head):
            return False
        allowed = {"alignas", "_Alignas", "__attribute__", "__attribute", "__declspec"}
        return all(i > 0 and head[i - 1] in allowed for i in groups)

    def _brace(self, opening):
        """What the brace at ``opening`` opens: "statement" (a block of statements: a
        statement's body, a block after a statement or a label), "function" (a body
        after parameters: `f(int a) {`, `operator()(int a) const {`, maybe a macro's
        `FOR_EACH(x) {`; a function-try-block's, `f(P p) try {`, and its handlers'),
        "lambda" (`[&] {`, `[](int v) -> int {`), or "other" (a class, an initializer,
        `({`, `new T[1]{`, `decltype(W{}){`, a compound literal)."""
        before = self._unattributed(opening)
        if before is None:
            return "other"
        token, at = before
        if token == "try" and self.raw_strings:
            # `long f(P p) try {`: the function's body, as a brace in the `try`'s place
            # would be; else a `try` statement's block.
            return "function" if self._brace(at) == "function" else "statement"
        if token in (";", "{", "}", "<%", "%>") or token in _LEADING:
            return "statement"
        if token == ":":
            return "statement" if self._label(at) else "other"
        if token in ("]", ":>"):
            return "lambda" if self._introducer(self._opening(at)) else "other"
        parameters = self._opening(at) if token == ")" else self._parameters(opening)
        word = self._before(parameters) if parameters is not None else None
        if word is None:
            return "other"
        if word[0] == "catch" and self.raw_strings:
            # A handler of a function-try-block is its function's too (after the `}` of
            # its `try {` or of another such handler); else a `try` statement's.
            previous = self._unattributed(word[1])
            block = (
                self._opening(previous[1])
                if previous is not None and previous[0] in ("}", "%>")
                else None
            )
            return (
                "function"
                if block is not None and self._brace(block) == "function"
                else "statement"
            )
        if word[0] in _HEADERS:
            return "statement"
        if word[0] in ("]", ":>"):
            bracket = self._opening(word[1])
            if bracket is not None and self._word_before(bracket) == "operator":
                return "function"  # `operator[](int i) {`
            return "lambda" if self._introducer(bracket) else "other"
        if word[0] == ")":
            # Only `operator()(...)`; `(long)(struct W){` is a compound literal.
            inner = self._opening(word[1])
            return (
                "function"
                if inner is not None and self._word_before(inner) == "operator"
                else "other"
            )
        if word[0] == ">" and self.raw_strings:
            return "function"  # a template's: `f<int>(int a) {` (C++ only)
        if self._word_before(word[1] + len(word[0])) == "operator":
            return "function"  # `operator+(...) {`
        if _WORD.fullmatch(word[0]) and word[0] not in _EXPRESSIONS:
            return "function"
        return "other"

    def _introducer(self, bracket):
        """Whether the `[` at ``bracket`` starts a lambda (`= [&]`, `([](int v)`), not a
        subscript or an array's bound (`new long[1]`, `a[1]`) or an attribute."""
        if bracket is None:
            return False
        code = self._code()[0]
        if code.startswith(("[[", "<:<:", "[<:", "<:["), bracket):
            return False
        before = self._before(bracket)
        if before is None:
            return True
        token = before[0]
        if token in ("return", "co_return", "co_yield", "throw"):
            return True
        return not (token[0].isalnum() or token[0] == "_" or token in (")", "]", ":>", ">", "}"))

    def _unattributed(self, position):
        """The token before ``position`` (``_before``), skipping attributes
        (`[[likely]]`)."""
        code = self._code()[0]
        before = self._before(position)
        for _ in range(16):
            if before is None or before[0] not in ("]", ":>"):
                break
            opening = self._opening(before[1])
            if opening is None or not code.startswith(("[[", "<:<:", "[<:", "<:["), opening):
                break
            before = self._before(opening)
        return before

    def _code(self):
        """(the blanked text with directive lines blanked too, {position of each bracket
        opening: position of the one closing it}, or None if they do not balance)."""
        if "code" not in self.__dict__:
            lines = self.blanked.split("\n")
            directive = False
            for index, text in enumerate(lines):
                if directive or _DIRECTIVE.match(text):
                    directive = text.rstrip(" \t\f\v").endswith("\\")
                    lines[index] = " " * len(text)
            code = "\n".join(lines)
            stack, pairs = [], {}
            for mark in _TOKENS.finditer(code):
                token = mark[0]
                if token in _OPEN:
                    stack.append((token, mark.start()))
                elif token in _CLOSE:
                    if not stack or _PAIRS[stack[-1][0]] != _PAIRS[token]:
                        pairs = None
                        break
                    pairs[stack.pop()[1]] = mark.start()
                elif token == "<::":
                    pairs = None
                    break
            if stack:
                pairs = None
            self.code = (code, pairs)
            self.closes = (
                {} if pairs is None else {closed: opened for opened, closed in pairs.items()}
            )
        return self.code

    def _opening(self, closing):
        """Where the bracket closing at ``closing`` opens, or None."""
        self._code()
        return self.closes.get(closing)

    def _before(self, position):
        """(the token just before ``position`` in ``_code``, where it starts), spaces
        skipped, or None. A word, a punctuator `_TOKENS` reads, or one other character."""
        code = self._code()[0]
        at = position - 1
        while at >= 0 and code[at].isspace():
            at -= 1
        if at < 0:
            return None
        if code[at].isalnum() or code[at] == "_":
            start = at
            while start > 0 and (code[start - 1].isalnum() or code[start - 1] == "_"):
                start -= 1
            return code[start : at + 1], start
        for mark in _TOKENS.finditer(code, max(0, at - 3), at + 1):
            if mark.end() == at + 1:
                return mark[0], mark.start()
        return code[at], at

    def _parameters(self, opening):
        """Where the parentheses before the brace at ``opening`` open, when only what may
        follow a function's parameters stands between them (`const`, `noexcept`,
        `mutable`, `override`, a trailing return type), else None."""
        code = self._code()[0]
        closing = code.rfind(")", 0, opening)
        if closing < 0 or not _QUALIFIED.fullmatch(code, closing + 1, opening):
            return None
        return self._opening(closing)

    def _label(self, colon):
        """Whether the `:` at ``colon`` ends a label (`case 1:`, `default:`, `out:`)."""
        code = self._code()[0]
        start = max(code.rfind(mark, 0, colon) for mark in ";{}:")
        head = code[start + 1 : colon].strip()
        return bool(re.fullmatch(r"case\b[\s\S]*|default|[A-Za-z_]\w*", head)) and "?" not in head

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
