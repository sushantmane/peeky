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
        self.text = translated(text, not raw_strings if trigraphs is None else trigraphs)
        self.blanked = blank(self.text, raw_strings)
        self.starts = [0] + [mark.end() for mark in re.finditer("\n", self.blanked)]
        hazards = hazards or {"brackets": macros}
        self.macros = frozenset(hazards.get("brackets", ()))
        # What `jumped_past` cannot see through: a label, or a bracket left open.
        self.opaque = frozenset(hazards.get("labels", ())) | frozenset(hazards.get("blocks", ()))
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
