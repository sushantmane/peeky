"""Where a Rust source's comments, strings and char literals are, for the checks that
read source text (`#[collapse_debuginfo]`, `macro_rules!` definitions)."""

import re

# Where a comment, string or char literal may start: a raw string (`r"`, `br#"`, `cr"`)
# only after a non-word.
_LEXEME = re.compile(r"//|/\*|(?<![\w])[bc]?r(#*)\"|\"|'")
_BLOCK_EDGE = re.compile(r"/\*|\*/")
_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_CHAR = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F]{1,6}\}|x[0-9a-fA-F]{2}|.)|[^\\'\n])'")


def lexemes(text):
    """([(start, end, comment)], complete): each comment (`//`, nested `/* */`), string
    or char literal in ``text``, in order. A `'` that starts no char literal is a lifetime
    (`'a`). One that does not end runs to the end of ``text``, and ``complete`` is False."""
    found, at = [], 0
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
    keep their numbers), or None if one does not end."""
    spans, complete = lexemes(text)
    if not complete:
        return None
    kept, at = [], 0
    for start, end, _ in spans:
        kept.append(text[at:start])
        kept.append(re.sub(r"[^\n]", " ", text[start:end]))
        at = end
    kept.append(text[at:])
    return "".join(kept)
