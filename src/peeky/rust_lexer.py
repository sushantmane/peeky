"""Where a Rust source's comments, strings and char literals are, for the checks that
read source text (`#[collapse_debuginfo]`, `macro_rules!` definitions, unsafe code).
Where Peeky cannot be sure it reads a source as rustc does, it says so (``scan``), and
each check then assumes the worst."""

import re

# Where a comment, string or char literal may start: a raw string (`r"`, `br#"`, `cr"`)
# only after a non-word.
_LEXEME = re.compile(r"//|/\*|(?<![\w])[bc]?r(#*)\"|\"|'")
_BLOCK_EDGE = re.compile(r"/\*|\*/")
_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_CHAR = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F]{1,6}\}|x[0-9a-fA-F]{2}|.)|[^\\'\n])'")


# Rust's whitespace (Pattern_White_Space, what rustc skips between tokens), which
# Python's `\s` is not.
_SPACES = re.compile("[\t\n\x0b\x0c\r \x85\u200e\u200f\u2028\u2029]*")
_BRACKET = re.compile(r"[()\[\]{}]")
_OPENING = {")": "(", "]": "[", "}": "{"}


def _preamble(text):
    """(where ``text``'s tokens start, why that cannot be told or None): past a byte-order
    mark and a shebang line, as rustc skips them (#156 review: `#!/usr/bin/env -S echo "`
    is no string). A shebang is a `#!` at the very start that whitespace does not part
    from a `[` (`#![allow(x)]` is an attribute); a comment after the `#!` is not judged,
    nor is a frontmatter block (`---`, a nightly feature)."""
    at = 1 if text.startswith("\ufeff") else 0
    if text.startswith("#!", at):
        after = _SPACES.match(text, at + 2).end()
        if text.startswith(("//", "/*"), after):
            return at, "a comment follows its `#!`, which Peeky does not judge"
        if not text.startswith("[", after):
            end = text.find("\n", at)
            at = len(text) if end < 0 else end
    if text.startswith("---", _SPACES.match(text, at).end()):
        return at, "it opens with a `---` frontmatter block, which Peeky does not read"
    return at, None


def lexemes(text):
    """([(start, end, comment)], complete): each comment (`//`, nested `/* */`), string
    or char literal in ``text``, in order. A `'` that starts no char literal is a lifetime
    (`'a`). One that does not end runs to the end of ``text``, and ``complete`` is False.
    A shebang line (and a byte-order mark) is a comment; where the tokens start
    cannot be told (_preamble), none is found and ``complete`` is False."""
    at, unknown = _preamble(text)
    if unknown:
        return [], False
    found = [(0, at, True)] if at else []
    while (match := _LEXEME.search(text, at)) is not None:
        start, token = match.start(), match[0]
        if token == "//":
            end = text.find("\n", start)
            found.append((start, len(text) if end < 0 else end, True))
        elif token == "/*":
            depth, end = 1, start + 2
            while depth:
                edge = _BLOCK_EDGE.search(text, end)
                if edge is None:
                    return [*found, (start, len(text), True)], False
                depth += 1 if edge[0] == "/*" else -1
                end = edge.end()
            found.append((start, end, True))
        elif token == "'":
            literal = _CHAR.match(text, start)
            if literal is None:
                at = start + 1
                continue
            found.append((start, literal.end(), False))
        elif token == '"':
            literal = _QUOTED.match(text, start)
            if literal is None:
                return [*found, (start, len(text), False)], False
            found.append((start, literal.end(), False))
        else:
            close = text.find('"' + match[1], match.end())
            if close < 0:
                return [*found, (start, len(text), False)], False
            found.append((start, close + 1 + len(match[1]), False))
        at = found[-1][1]
    return found, True


def without_comments(text):
    """``text`` with its comments blanked; strings and char literals stay whole, so a
    `//` or `/*` inside one starts no comment. An unterminated one keeps the rest."""
    kept, at = [], 0
    for start, end, comment in lexemes(text)[0]:
        if comment:
            kept.append(text[at:start] + " ")
            at = end
    kept.append(text[at:])
    return "".join(kept)


def code_only(text):
    """``text`` with comments, strings and char literals blanked (newlines kept, so lines
    keep their numbers), or None if Peeky cannot be sure it reads it as rustc does
    (see scan)."""
    return scan(text)[0]


def scan(text):
    """(code_only's text, None), or (None, why not): one that does not end, a start not
    judged (_preamble), or brackets that do not pair up once comments and literals are
    blanked, which every source rustc accepts does (its tokens are trees): a literal
    Peeky misread could hide code."""
    spans, complete = lexemes(text)
    if not complete:
        return None, _preamble(text)[1] or "a comment, string or char literal does not end"
    kept, at = [], 0
    for start, end, _ in spans:
        kept.append(text[at:start])
        kept.append(re.sub(r"[^\n]", " ", text[start:end]))
        at = end
    kept.append(text[at:])
    code, opened = "".join(kept), []
    for bracket in _BRACKET.finditer(code):
        if bracket[0] in "([{":
            opened.append(bracket[0])
        elif not opened or opened.pop() != _OPENING[bracket[0]]:
            return None, "its brackets do not pair up"
    if opened:
        return None, "its brackets do not pair up"
    return code, None
