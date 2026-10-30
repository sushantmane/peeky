"""Inline assembly that can read a file is refused before building.

The assembler's `.incbin` and `.include` read a file that no compiler lists in its
dependency file (`-MD`, rustc's `.d`), so the provenance check, which hashes every file
the build reads, would not cover it. GNU as and LLVM's assembler (Clang's and rustc's)
have no other directive that reads one (Darwin's `.load` and `.dump` are ignored), but
their macros (`.macro`, `.irp`, `.irpc`) can spell one from pieces, and so can an
operand where a directive's name is. Peeky refuses each of these wherever the compiled
code holds it: in a C or C++ `asm` (in the preprocessed translation unit, adjacent
literals joined) or a Rust `asm!`, `global_asm!` or `naked_asm!`. A string outside
inline assembly is never judged. Peeky builds no `.s` or `.S` file and passes the
assembler no option (`-Wa,`), so inline assembly is all the assembler reads.
"""

import bisect
import re

from peeky.c_source import _digit_separator, blank
from peeky.rust_lexer import lexemes, scan
from peeky.textlines import octal_string

ASSEMBLER_REFUSAL = "Inline assembly that can read a file is not supported"

# A directive that reads a file (names are case-insensitive).
_READS = re.compile(r"\.(incbin|include)(?![\w$])", re.IGNORECASE)
# A macro, which can spell one from pieces (`.irpc c,n` then `.incbi\c`).
_MACRO = re.compile(r"(?<![\w$])\.(macro|irpc|irp)(?![\w$])", re.IGNORECASE)
# Where an operand may spell a directive's name: in a word that starts with a dot
# (`.inc%c0`), or starting a statement other than a label (`{} "file"`, a symbol named
# `.incbin`).
_LABEL = re.compile(r"[ \t]*:")
_C_OPERAND = re.compile(r"(?<!%)%[a-zA-Z]*(?:[0-9]+|\[[^\]\n]*\])")
_RUST_OPERAND = re.compile(r"(?<!\{)\{(?!\{)[^{}\n]*\}")


def judged(text, operand=None):
    """What in the assembler text ``text`` can read a file, or None: a directive that
    reads one, a macro, or an ``operand`` (a regex) where a directive's name is."""
    if found := _READS.search(text):
        return f"uses `.{found[1].lower()}`, which reads a file"
    if found := _MACRO.search(text):
        return (
            f"uses `.{found[1].lower()}`, an assembler macro, which can spell a directive "
            "that reads a file (`.incbin`)"
        )
    for found in operand.finditer(text) if operand is not None else ():
        word = found.start()
        while word and (text[word - 1].isalnum() or text[word - 1] in "_.$"):
            word -= 1
        line = word
        while line and text[line - 1] in " \t":
            line -= 1
        statement = word == found.start() and (not line or text[line - 1] in "\n;")
        if text.startswith(".", word, found.start()) or (
            statement and not _LABEL.match(text, found.end())
        ):
            return (
                f"spells a directive's name with an operand (`{found[0]}`), which can make "
                "it one that reads a file (`.incbin`)"
            )
    return None


def refusal(where, what):
    """The error for inline assembly at ``where`` that ``what`` (judged's reason)."""
    refused = ValueError(
        f"{ASSEMBLER_REFUSAL}: {where} {what}. No dependency file lists what the "
        "assembler reads, so Peeky could not tell whether it changed"
    )
    # Nothing was compiled or run: no run is kept as evidence.
    refused.before_launch = True
    return refused


# C and C++ (a translation unit as `-E` gives it).
_C_KEYWORD = re.compile(r"(?<![\w$])(?:asm|__asm|__asm__)(?![\w$])")
# Where a keyword may be: each `asm` (a regex with a lookbehind first scans slowly).
_C_ASM = re.compile("asm")
_C_QUALIFIERS = re.compile(
    r"(?:\s|(?<![\w$])(?:volatile|__volatile__|__volatile|inline|__inline__|__inline|goto)"
    r"(?![\w$]))*"
)
_C_LINE = re.compile(r"[^\n]*\n?")
# A line of the output that is a directive or a line marker (`# 12 "a.c" 2`, `#pragma`).
_C_DIRECTIVE = re.compile(r"[ \t]*#")
# The same, to the line's end; and one after a newline (a regex that starts with a
# character scans fast).
_C_DIRECTIVE_LINE = re.compile(r"[ \t]*#[^\n]*")
_C_NEXT_DIRECTIVE = re.compile(r"\n([ \t]*#[^\n]*)")
_C_MARKER = re.compile(r'[ \t]*#[ \t]*(?:line[ \t]+)?([0-9]+)(?:[ \t]+"((?:[^"\\\n]|\\.)*)")?')
_C_ESCAPE = re.compile(
    r"\\(?:([0-7]{1,3})|x([0-9a-fA-F]+)|u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8})|(.))", re.DOTALL
)
_C_SIMPLE = dict(zip("ntrabfve", "\n\t\r\a\b\f\v\x1b"))
_C_RAW = re.compile(r'"([^ ()\\\t\v\f\n]{0,16})\(')
_C_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"?')
_C_CHAR = re.compile(r"'(?:[^'\\\n]|\\.)*'?")


def _character(code):
    return chr(code) if code <= 0x10FFFF else "\ufffd"


def _c_unescaped(body):
    def one(found):
        octal, hexadecimal, short, long, other = found.groups()
        if other is not None:
            return _C_SIMPLE.get(other, other)
        return _character(int(octal, 8) if octal else int(hexadecimal or short or long, 16))

    return _C_ESCAPE.sub(one, body)


def _c_group(text, opening, cpp):
    """(the end of the parenthesized group whose `(` is at ``opening``, its string
    literals as [(start, end, value)]), read as the compiler reads them: a `(` or `)` in
    a string or character literal is none."""
    depth, at, size, literals = 0, opening, len(text), []
    while at < size:
        char = text[at]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if not depth:
                return at + 1, literals
        elif char == '"':
            start = at
            while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
                start -= 1
            prefix = text[start:at]
            raw = (
                _C_RAW.match(text, at) if cpp and prefix in ("R", "LR", "u8R", "uR", "UR") else None
            )
            if raw is not None:
                closing = text.find(f'){raw[1]}"', raw.end())
                end = size if closing < 0 else closing + len(raw[1]) + 2
                value = text[raw.end() : closing if closing >= 0 else size]
            else:
                end = _C_STRING.match(text, at).end()
                value = _c_unescaped(text[at + 1 : end - 1 if text[end - 1] == '"' else end])
            literals.append((start, end, value))
            at = end
            continue
        elif char == "'" and not _digit_separator(text, at):
            at = _C_CHAR.match(text, at).end()
            continue
        at += 1
    return size, literals


def _c_places(text, blanked):
    """For each line of ``text`` (a preprocessor's output; ``blanked``, the same with its
    literals blanked), (file, line) as its line markers give it; and both texts with
    their directive lines blanked (offsets kept). A line in a raw string is none."""
    places, kept, name, number, at = [], ([], []), "<unknown>", 1, 0
    for line in _C_LINE.findall(blanked):
        if not line:
            break
        original, at = text[at : at + len(line)], at + len(line)
        places.append((name, number))
        if _C_DIRECTIVE.match(line):
            if marker := _C_MARKER.match(original):
                number = int(marker[1])
                if marker[2] is not None:
                    # clang writes a non-ASCII byte in octal (`\342\200\250`).
                    name = octal_string(marker[2]) or "<unknown>"
            else:
                number += 1
            original = line = re.sub(r"[^\n]", " ", line)
        else:
            number += 1
        kept[0].append(original)
        kept[1].append(line)
    return places, "".join(kept[0]), "".join(kept[1])


def _c_keywords(text):
    """``_C_KEYWORD.finditer(text)``'s matches, found faster: from each `asm`."""
    for found in _C_ASM.finditer(text):
        at = found.start()
        for start in (at - 2, at) if at >= 2 and text.startswith("__", at - 2) else (at,):
            if (keyword := _C_KEYWORD.match(text, start)) is not None:
                yield keyword
                break


def _c_without_directives(text, blanked):
    """``text`` and ``blanked`` with their directive lines blanked, as `_c_places` gives
    them where neither has a lone carriage return (``blank`` reads one as a line break)."""
    spans = [first.span()] if (first := _C_DIRECTIVE_LINE.match(blanked)) else []
    spans += [found.span(1) for found in _C_NEXT_DIRECTIVE.finditer(blanked)]
    pieces, kept, done = [], [], 0
    for start, end in spans:
        spaces = " " * (end - start)
        pieces += [text[done:start], spaces]
        kept += [blanked[done:start], spaces]
        done = end
    return "".join(pieces) + text[done:], "".join(kept) + blanked[done:]


def c_refusal(output, cpp):
    """The refusal (a ValueError) for inline assembly that can read a file in a
    translation unit as the compiler's `-E` gives it (``output``, bytes), or None.
    Blanking a literal, comment or directive only makes spaces, so a keyword in the
    blanked text is one in the text too: a text without one is passed at once."""
    text = output.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if next(_c_keywords(text), None) is None:
        return None
    original, literals_blanked = text, blank(text, cpp)
    if "\r" in text:
        places, text, blanked = _c_places(original, literals_blanked)
    else:
        # Where each line is from is read only for a refusal.
        places = None
        text, blanked = _c_without_directives(original, literals_blanked)
    starts = []

    def where(offset):
        nonlocal places
        if places is None:
            places = _c_places(original, literals_blanked)[0]
        if not starts:
            starts.append(0)
            starts.extend(found.end() for found in re.finditer("\n", text))
        name, line = places[min(bisect.bisect_right(starts, offset) - 1, len(places) - 1)]
        return f"the asm at {name}:{line}"

    for keyword in _c_keywords(blanked):
        opening = _C_QUALIFIERS.match(blanked, keyword.end()).end()
        if blanked[opening : opening + 1] != "(":
            continue
        _, literals = _c_group(text, opening, cpp)
        first = re.compile(r"\s*").match(text, opening + 1).end()
        if cpp and (not literals or literals[0][0] != first):
            # Clang's constant-expression templates (`asm((s()))`).
            return refusal(
                where(keyword.start()),
                "has a template that is not a string literal, which Peeky does not evaluate",
            )
        # Adjacent literals are one string (`".inc" "bin"`).
        runs = []
        for start, end, value in literals:
            if runs and not text[runs[-1][1] : start].strip():
                runs[-1] = (runs[-1][0], end, runs[-1][2] + value)
            else:
                runs.append((start, end, value))
        for start, _, value in runs:
            if what := judged(value, _C_OPERAND):
                return refusal(where(start), what)
    return None


# Rust (a crate's declared files).
_RUST_ASM = re.compile(r"(?<!\w)(asm|global_asm|naked_asm)\b")
_RUST_OPEN = re.compile(r"\s*!\s*([(\[{])")
_RUST_RENAMED = re.compile(r"\s+as\b")
_RUST_CALL = re.compile(r"(?<!\w)(?:r#)?([A-Za-z_]\w*)\s*!\s*[(\[{]")
_RUST_NAMED_CALL = re.compile(r"\$\s*\w+\s*!")
_RUST_ESCAPE = re.compile(
    r"\\(?:x([0-9a-fA-F]{2})|u\{([0-9a-fA-F_]{1,8})\}|\n[ \t\n\r]*|(.))", re.S
)
_RUST_SIMPLE = {"n": "\n", "r": "\r", "t": "\t", "0": "\0"}
_RUST_RAW = re.compile(r'[bc]?r(#*)"')
# A directive's name outside literals (`stringify!(.incbin "f")`), not `include!`.
_RUST_WORD = re.compile(r"(?<![\w$])(incbin|include)\b(?!\s*!)", re.IGNORECASE)


def _rust_value(text, start, end):
    """The value of the Rust literal ``text[start:end]`` (a string or char literal)."""
    raw = _RUST_RAW.match(text, start)
    if raw is not None:
        return text[raw.end() : end - 1 - len(raw[1])]

    def one(found):
        byte, code, other = found.groups()
        if byte or code:
            return _character(int(byte or code.replace("_", ""), 16))
        return "" if other is None else _RUST_SIMPLE.get(other, other)

    return _RUST_ESCAPE.sub(one, text[start + 1 : end - 1])


def _closing(code, opening):
    """Where the bracket at ``opening`` in ``code`` (literals blanked, paired) closes."""
    depth = 0
    for found in re.compile(r"[()\[\]{}]").finditer(code, opening):
        depth += 1 if found[0] in "([{" else -1
        if not depth:
            return found.start()
    return len(code)


def _line(text, offset):
    return text.count("\n", 0, offset) + 1


def rust_refusal(texts, rust):
    """The refusal (a ValueError) for inline assembly that can read a file in a crate
    whose declared files are ``texts`` ({name: text}; ``rust``, its Rust sources), or
    None. Each `asm!`, `global_asm!` and `naked_asm!` is judged by its literals (joined
    as rustc joins them) and the words outside them. Where a template may come from
    elsewhere (a macro's argument, a renamed or included asm macro, `env!`), any
    directive in any declared file counts."""
    unread, mentioned = None, False
    for name in rust:
        text = texts.get(name, "")
        code, why = scan(text)
        if code is None:
            if _RUST_ASM.search(text):
                mentioned = True
                unread = unread or f"Peeky cannot read {name} as rustc does ({why})"
            continue
        literals = [(start, end) for start, end, comment in lexemes(text)[0] if not comment]
        for found in _RUST_ASM.finditer(code):
            mentioned = True
            here = f"{name}:{_line(text, found.start())}"
            opening = _RUST_OPEN.match(code, found.end())
            if opening is None:
                if _RUST_RENAMED.match(code, found.end()):
                    unread = unread or f"{found[1]} is renamed at {here}"
                continue
            start = opening.start(1)
            end = _closing(code, start)
            inside = code[start + 1 : end]
            calls = {call[1] for call in _RUST_CALL.finditer(inside)} - {"concat", "stringify"}
            if "$" in inside or calls:
                what = f"`{sorted(calls)[0]}!`" if calls else "a macro's argument"
                unread = unread or f"the {found[1]}! at {here} takes its template from {what}"
            # Template strings are lines; `concat!`'s pieces join.
            joined = ""
            for at, stop in literals:
                if start < at < end:
                    depth = len(re.findall(r"[(\[{]", code[start:at])) - len(
                        re.findall(r"[)\]}]", code[start:at])
                    )
                    joined += ("\n" if depth == 1 and joined else "") + _rust_value(text, at, stop)
            what = judged(joined, _RUST_OPERAND)
            if what is None and (word := _RUST_WORD.search(inside)):
                what = f"uses `.{word[1].lower()}`, which reads a file"
            if what is None and (macro := _MACRO.search(inside)):
                what = judged(macro[0])
            if what:
                return refusal(f"the {found[1]}! at {here}", what)
        if (named := _RUST_NAMED_CALL.search(code)) is not None:
            unread = unread or (
                f"a macro at {name}:{_line(text, named.start())} calls a macro its caller names"
            )
    if not mentioned or unread is None:
        return None
    # The template may be any literal or word in the crate.
    tail = f", and inline assembly may take it ({unread})"
    for name, text in texts.items():
        code = scan(text)[0] if name in rust else None
        if code is None:
            if found := _READS.search(text) or _MACRO.search(text):
                return refusal(f"{name}:{_line(text, found.start())}", judged(found[0]) + tail)
            continue
        values = [
            (start, _rust_value(text, start, end))
            for start, end, comment in lexemes(text)[0]
            if not comment
        ]
        words = [(word.start(), "." + word[1]) for word in _RUST_WORD.finditer(code)]
        for start, value in [*values, *words]:
            if what := judged(value):
                return refusal(f"{name}:{_line(text, start)}", what + tail)
        # `concat!`'s pieces, joined.
        joined, offsets = "", []
        for start, value in values:
            offsets.append((len(joined), start))
            joined += value
        if what := judged(joined):
            found = (_READS.search(joined) or _MACRO.search(joined)).start()
            at = offsets[bisect.bisect_right(offsets, (found, len(text))) - 1][1]
            return refusal(f"{name}:{_line(text, at)}", what + tail)
    return None
