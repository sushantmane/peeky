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

from peeky.textlines import octal_string

# An identifier's characters: ASCII letters, digits and `_`, and every non-ASCII
# character but whitespace (Clang reads U+00A0 as a space), which outside comments and
# literals a program the compiler accepts holds only in identifiers (#195: `größe`, or
# an NFD `größe`, whose combining mark `\w` misses).
_ID = r"[^\s\x00-\x2f\x3a-\x40\x5b-\x5e\x60\x7b-\x7f]"
_NAME = r"[^\s\x00-\x40\x5b-\x5e\x60\x7b-\x7f]" + _ID + "*"
# An identifier, not the tail of a number or of another identifier (`1e5`).
_IDENTIFIER = re.compile(f"(?<!{_ID}){_NAME}")
# What leads a structured binding's `[`: `auto`, `const auto`, `auto const`, and its `&` or
# `&&` (group 1).
_BINDING_LEAD = re.compile(r"\bauto(?:\s+(?:const|volatile)\b)*\s*(&{0,2})\s*$")
# The members of a standard container whose call gives a value, never a pointer, an
# iterator or a reference to an element (#297): what code may call on it anywhere while
# a structured binding refers to one of its elements.
_VALUES = frozenset({"capacity", "contains", "count", "empty", "max_size", "size"})
# A token, as an expression's context is read (#297): a word, a number, an operator.
_LEXEME = re.compile(
    rf"(?<!{_ID}){_NAME}|\.?[0-9](?:[eEpP][-+]|[\w.'])*|<<=|>>=|->\*|<=>|\.\.\.|::|->|"
    r"\+\+|--|<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%&|^]=|\.\*|<%|%>|\S"
)
_ASSIGNING = frozenset(("=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="))
# Operators that are built in where every operand is a number or a character (#297 review
# round 2): no type of the program's is there for an overload of its own to take one.
_BUILT_IN = frozenset(
    ("+", "-", "*", "/", "%", "<", ">", "<=", ">=", "==", "!=", "&&", "||", "&", "|", "^")
    + ("<<", ">>", "!", "~")
)
# A number literal as the language spells it, with only its own suffixes (#297 review round
# 3): no user-defined literal (`1_sink`, `10s`, `2i`), which calls an operator of its own.
_DIGITS = r"[0-9](?:'?[0-9])*"
_HEX_DIGITS = r"[0-9a-fA-F](?:'?[0-9a-fA-F])*"
_NUMBER_LITERAL = re.compile(
    rf"(?:0[xX]{_HEX_DIGITS}|0[bB][01](?:'?[01])*|0(?:'?[0-7])*|[1-9](?:'?[0-9])*)"
    r"(?:[uU](?:ll|LL|[lLzZ])?|(?:ll|LL|[lLzZ])[uU]?)?"
    rf"|(?:(?:{_DIGITS})?\.{_DIGITS}(?:[eE][-+]?{_DIGITS})?|{_DIGITS}\.(?:[eE][-+]?{_DIGITS})?"
    rf"|{_DIGITS}[eE][-+]?{_DIGITS}"
    rf"|0[xX](?:{_HEX_DIGITS})?\.?(?:{_HEX_DIGITS})?[pP][-+]?{_DIGITS})[fFlL]?"
)
# The words of a number's or a character's type, as a local's declaration or a cast spells it.
_NUMBER_TYPES = frozenset(
    ("auto", "bool", "char", "char8_t", "char16_t", "char32_t", "const", "double", "float")
    + ("int", "long", "short", "signed", "unsigned", "volatile", "wchar_t")
)
# Words before `(` that make it no call's.
_NOT_CALLS = frozenset(
    {
        "alignof",
        "case",
        "catch",
        "co_await",
        "co_return",
        "co_yield",
        "decltype",
        "delete",
        "for",
        "if",
        "new",
        "noexcept",
        "return",
        "sizeof",
        "switch",
        "throw",
        "typeid",
        "while",
    }
)
# Where a statement starts: after one of these, or a control statement's header.
_BOUNDARIES = frozenset((";", "{", "}", "<%", "%>", "else", "do"))
# Words before a name that make it no declaration's: it is an expression's.
_NOT_TYPES = frozenset(
    {
        "alignof",
        "case",
        "co_await",
        "co_return",
        "co_yield",
        "decltype",
        "delete",
        "do",
        "else",
        "goto",
        "new",
        "operator",
        "return",
        "sizeof",
        "throw",
        "typeid",
    }
)
# A GNU `asm goto` (`asm volatile goto`, `__asm__ goto`, `__asm goto`): it may jump to
# any label it lists as an operand.
_ASM_GOTO = re.compile(rf"(?<!{_ID})(?:asm|__asm__|__asm)(?!{_ID})[^(;]*?(?<!{_ID})goto(?!{_ID})")
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
_WORDS = re.compile(f"(?<!{_ID}){_NAME}|" + _TOKENS.pattern)
_WORD = re.compile(_NAME)
_LAST_WORD = re.compile(_NAME + "$")
# Words and punctuators as a declaration is read: its `=` and `,` (not `==`, `+=`, ...).
_DECLARATION_TOKENS = re.compile(
    f"(?<!{_ID}){_NAME}|==|!=|[-+*/%&|^]=|<<=|>>=|" + _TOKENS.pattern + r"|[=,.]"
)
# Statements whose header (in parentheses) can declare a variable for their body.
_HEADERS = frozenset(("for", "if", "while", "switch", "catch"))
# Keywords a name follows where it is used, not declared.
_USES = frozenset(("return", "case", "sizeof", "delete", "throw", "co_return", "co_yield"))
_USES = _USES | {"else", "do", "goto", "typeid", "alignof", "_Alignof", "new"}
# What a statement or block follows: `else {`, `do {`, `try {`.
_LEADING = frozenset(("else", "do", "try"))
# A label a jump can reach, in a macro's body: at its start or after `;`, `{` or `}`.
_LABEL = re.compile(r"(?:^|[;{}])\s*" + _NAME + r"\s*:(?![:>])")
_INCLUDE = re.compile(r"[ \t\f\v]*(?:#|%:)[ \t\f\v]*(?:include|include_next|import)\b")
_INCLUDED = re.compile(r'(?:include|include_next|import)\s*(?:"([^"\n]+)"|<([^>\n]+)>)')
_NONBLANK = re.compile(r"\S")
# `??/` is a backslash and `??<` a brace where trigraphs are on (C before C23, as
# `-std=c11` builds it; C++17 has none).
_TRIGRAPH = re.compile(r"\?\?([=(/)'<!>-])")
_TRIGRAPHS = dict(zip("=(/)'<!>-", "#[\\]^{|}~"))
# Words a `(` follows in an expression, never in a function's head.
_EXPRESSIONS = _USES | {"decltype", "noexcept", "and", "or", "not", "static_assert", "requires"}
# What may stand between a function's (or a lambda's) parameters and its body
# (`Statements._qualified`): its qualifiers and specifiers (a lambda's `constexpr`,
# `consteval`, `static`), an exception specification, attributes, a trailing return type
# and a trailing `requires` clause.
_QUALIFIERS = frozenset(
    (
        "const",
        "volatile",
        "noexcept",
        "override",
        "final",
        "mutable",
        "constexpr",
        "consteval",
        "static",
    )
)
# A group of parentheses that may stand there: `noexcept(true)`, `__attribute__((cold))`,
# `-> decltype(x)`, `requires (N > 1)`.
_SPECIFYING = frozenset(
    ("noexcept", "throw", "__attribute__", "__declspec", "decltype", "requires")
)
# A directive's line (`%:` is `#`).
_DIRECTIVE = re.compile(r"[ \t\f\v]*(?:#|%:)")
# `#line 7` and a line marker (`# 7 "x.c"`) renumber the lines after them.
_RENUMBER = re.compile(r"^[ \t\f\v]*(?:#|%:)[ \t\f\v]*(?:line\b|[0-9])", re.M)
_DEFINE = re.compile(
    r"^[ \t\f\v]*(?:#|%:)[ \t\f\v]*define[ \t\f\v]+(" + _NAME + r")(\(?)(.*)$", re.M
)


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


def _word_char(char):
    """Whether ``char`` can be part of an identifier (``_ID``)."""
    return (char.isalnum() or char == "_") if char.isascii() else not char.isspace()


def _continues_identifier(text, i):
    # `fooR"x(` is an identifier then a string, not a raw string.
    return i > 0 and _word_char(text[i - 1])


def _digit_separator(text, i):
    """Whether the `'` at ``i`` is inside a number (`1'000`, C++14), not a literal."""
    start = i
    while start > 0 and (_word_char(text[start - 1]) or text[start - 1] == "."):
        start -= 1
    return start < i and text[start].isdigit()


def unsafe_macros(texts, raw_strings=True, trigraphs=None):
    r"""The names of the macros ``texts`` define that a statement scan cannot see
    through: an expansion that may hold a bracket it does not close, a `;` outside its
    brackets, a `<::`, or a pasted token (`<` ## `%` is a brace), or that names such a
    macro. Every macro a source with a splice before `\n\r` defines counts.
    ``trigraphs``: C's (by default, without ``raw_strings``: C)."""
    return macro_hazards(texts, raw_strings, trigraphs)["brackets"]


# What may return twice into the function calling it: setjmp's family (`_setjmp`,
# `sigsetjmp`, `__builtin_setjmp`, ...) and the ucontext switches.
_RETURNS_TWICE = re.compile(r"\w*setjmp\w*|getcontext|swapcontext|savectx")
# What declares a name with static or external storage, or brings one into a block.
_STORAGE = frozenset(
    ("static", "extern", "using", "thread_local", "_Thread_local", "__thread", "typedef")
)
# Of those, what a declaration's specifiers say: a block's name the code reads instead.
_HIDERS = frozenset(("static", "extern", "thread_local", "_Thread_local", "__thread", "typedef"))
# Statements a header's body may begin with whose end Peeky does not read (``_controlled``).
_UNREAD = frozenset(("else", "__try", "__except", "__finally", "__leave"))
# A body that belongs to the statement it is in, not a block of statements.
_TYPE_BODIES = frozenset(("struct", "union", "enum", "class"))
# What names a C++ class or enumeration (`enum` too); and what may come between that and
# the name (`struct alignas(8) q`).
_CLASS_KEYS = _TYPE_BODIES
_ATTRIBUTES = frozenset(("__attribute__", "alignas", "_Alignas", "__declspec"))
# A statement starting so names no function it declares (`return use2(1);`).
_NOT_DECLARING = frozenset(
    (
        "return",
        "case",
        "goto",
        "throw",
        "delete",
        "new",
        "sizeof",
        "else",
        "do",
        "co_return",
        "co_yield",
        "co_await",
    )
)
# Words after which a name is an operand, never a declarator: what only uses a name
# (`_USES`, `_NOT_DECLARING`), C++'s alternative tokens (`ok and t`, `not t`), and words that
# begin an expression or a statement.
_OPERAND_WORDS = (
    _USES
    | _NOT_DECLARING
    | {"and", "and_eq", "bitand", "bitor", "compl", "not", "not_eq", "or", "or_eq", "xor"}
    | {"xor_eq", "if", "while", "for", "switch", "this", "true", "false", "nullptr"}
    | {"requires", "static_assert", "_Static_assert", "__real__", "__imag__", "noexcept"}
)
# Keywords a declaration of variables may begin with (`long a = 1, t;`, `struct S *t;`).
# After one, a comma at the statement's own level separates declarators.
_SPECIFIERS = frozenset(
    (
        "auto",
        "bool",
        "_Bool",
        "char",
        "char8_t",
        "char16_t",
        "char32_t",
        "class",
        "const",
        "constexpr",
        "constinit",
        "double",
        "enum",
        "extern",
        "float",
        "inline",
        "int",
        "long",
        "register",
        "short",
        "signed",
        "static",
        "struct",
        "thread_local",
        "_Thread_local",
        "__thread",
        "typedef",
        "union",
        "unsigned",
        "void",
        "volatile",
        "wchar_t",
        "_Atomic",
    )
)
# Words a type's parentheses may follow (`decltype(x) t`, `__attribute__((unused)) long t`),
# and qualifiers that may stand between a declarator's `*` and its name.
_TYPE_GROUPS = frozenset(
    ("decltype", "typeof", "__typeof__", "__typeof", "_Atomic", "alignas", "_Alignas")
) | frozenset(("__attribute__", "__declspec"))
_CV = frozenset(("const", "volatile", "restrict", "__restrict", "__restrict__", "_Atomic"))
# A declaration's tokens before a name: a word, `::`, `->` (to tell it from `>`), or any
# other character.
_PREFIX = re.compile(f"{_NAME}|::|->|\\S")


def macro_hazards(texts, raw_strings=True, trigraphs=None):
    r"""The macros ``texts`` define, by what their expansion may hide from a scan of the
    text: ``brackets`` (`unsafe_macros`), ``blocks`` (the same but for a `;`: a bracket
    it does not close), ``labels`` (a `case`, `default` or label a jump can reach, `x:`),
    ``gotos`` (a `goto`), ``setjmp`` (a call that may return twice, `setjmp`) and
    ``declares`` (a `static`, `extern` or `using` declaration). A macro naming such a
    macro counts too, and every macro of a source with a splice before `\n\r` counts
    for all of them."""
    trigraphs = not raw_strings if trigraphs is None else trigraphs
    uses = {}
    found = {
        "brackets": set(),
        "blocks": set(),
        "labels": set(),
        "gotos": set(),
        "setjmp": set(),
        "declares": set(),
    }
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
            if unknown or any(_RETURNS_TWICE.fullmatch(word) for word in words):
                found["setjmp"].add(name)
            if unknown or words & _STORAGE:
                found["declares"].add(name)
    for names in found.values():
        while more := {name for name, used in uses.items() if name not in names and used & names}:
            names |= more
    # What each macro's expansion may name, through the macros it names.
    expands = {name: set(used) for name, used in uses.items()}
    for _ in range(len(expands)):
        changed = False
        for words in expands.values():
            more = set().union(*(expands[w] for w in words & expands.keys())) - words
            if more:
                words |= more
                changed = True
        if not changed:
            break
    return {
        **{kind: frozenset(names) for kind, names in found.items()},
        "expands": {name: frozenset(words) for name, words in expands.items()},
    }


def returning_twice(texts):
    """The macros ``texts`` (headers outside the program, read as they are) define that
    may expand to a call returning twice (`setjmp`, directly or through another), for a
    TRY macro a library header hides it in. Read without blanking comments: a comment
    naming one only counts too much."""
    uses, found = {}, set()
    for text in texts:
        for define in _DEFINE.finditer(_SPLICE.sub("", text)):
            name, body = define[1], define[3]
            words = {word[0] for word in _IDENTIFIER.finditer(body)}
            uses.setdefault(name, set()).update(words)
            if any(_RETURNS_TWICE.fullmatch(word) for word in words):
                found.add(name)
    while more := {name for name, used in uses.items() if name not in found and used & found}:
        found |= more
    return frozenset(found)


def storage_named(text, name):
    """Whether ``text`` may declare ``name`` so that it hides an outer local of that name:
    with static or external storage or as a typedef, by a C++ `using` declaration, as an
    enumerator, as a function a block declares or as a C++ class (a cheap read: an
    initializer's braces are skipped; a parameter of a function defined there is not
    counted)."""
    escaped = re.escape(str(name))
    bounded = f"(?<!{_ID}){escaped}(?!{_ID})"
    balanced = r"(?:[^;{}]|\{(?:[^{}]|\{[^{}]*\})*\})*"
    keyword = re.compile(
        r"\b(?:static|extern|thread_local|_Thread_local|__thread|using|typedef)\b"
        + balanced
        + bounded
    )
    for found in keyword.finditer(text):
        rest = re.match(r"[^;{]*", text[found.end() :])[0]
        following = text[found.end() + len(rest) : found.end() + len(rest) + 1]
        if not (following == "{" and ")" in rest and "(" in found[0]):
            return True
    if re.search(r"\benum\b[^;{}]*\{[^{}]*" + bounded, text):
        return True
    # A C++ structured binding: `auto [q, r] = s;`, `for (const auto& [q, r] : m)`.
    if re.search(r"\bauto\b[^;{}\[]*\[[^\]]*" + bounded, text):
        return True
    # A C++ class (or enumeration) a block declares by that name: `struct q { ... };`.
    if re.search(r"\b(?:struct|class|union|enum)\b[^;{}]*?" + bounded, text):
        return True
    # A function a block declares where an outer local's name is: `long use2(long);`
    # (or after a type's body: `struct S { long v; } use2(long);`).
    return bool(re.search(f"(?:{_NAME}|\\}})[\\s*&]+" + escaped + r"\s*\(", text))


def _braced(statement):
    """For each of a statement's tokens, whether braces in the statement hold it (a
    lambda's body, an initializer's braces)."""
    found, braces = [], 0
    for text, _, _ in statement:
        if text in ("}", "%>"):
            braces = max(braces - 1, 0)
        found.append(braces > 0)
        if text in ("{", "<%"):
            braces += 1
    return found


def _class_name(statement, index):
    """The name a class key (``statement[index]``: `struct`, `class`, `union`, `enum`)
    gives, past `enum class` and attributes (`struct [[deprecated]] q`), or None."""
    following = index + 1
    if statement[index][0] == "enum" and following < len(statement):
        if statement[following][0] in ("class", "struct"):
            following += 1
    while following < len(statement):
        text, _, depth = statement[following]
        if text in _ATTRIBUTES and following + 1 < len(statement):
            following += 1
            text, _, depth = statement[following]
            if text != "(":
                return None
        elif text not in ("[", "<:"):
            return text if _WORD.fullmatch(text) else None
        # Past the bracketed group: to the bracket closing it.
        following += 1
        while following < len(statement) and not (
            statement[following][0] in _CLOSE and statement[following][2] == depth - 1
        ):
            following += 1
        following += 1
    return None


def _parameters_follow(token):
    """Whether a `(` after ``token`` opens a declarator's parameters (after its name or
    a bracket), not a grouping (`long (*q)[3]` opens after a type's name, followed by
    `*`, which the caller checks)."""
    return bool(_WORD.fullmatch(token)) or token in (")", "]", ":>")


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


# A line marker in a preprocessor's output (`# 12 "dir/a.h" 2`, GCC's and Clang's): the
# next line is that line of that file (of the same file, without one). Any other line
# starting with `#` is a directive the output keeps (`#pragma`).
_MARKER = re.compile(rb'[ \t]*#[ \t]*(?:line[ \t]+)?([0-9]+)(?:[ \t]+"((?:[^"\\\n]|\\.)*)")?')
_DIRECTIVE_LINE = re.compile(r"[ \t]*(?:#|%:)")


class Preprocessed:
    """A translation unit as the compiler's preprocessor gave it (`-E`, with its line
    markers; ``lines``, as bytes), read back onto its sources. ``program(path)``: the
    name of the program's own file at ``path`` (as the markers give it), or None for
    another (a system header, the built-in definitions).

    ``text``: one line for each of the program's source lines in the order the output
    gives them (where the output splits one, around a `_Pragma`, its pieces joined),
    directives (`#pragma`) blank, and each run of another file's lines one `#include`
    line in their place. ``origins``: each of its lines' (name, line), None for those."""

    def __init__(self, lines, program):
        groups, names, name, number = [], {}, None, 0
        for raw in lines:
            raw = raw.rstrip(b"\r\n")
            marker = _MARKER.match(raw)
            if marker:
                number = int(marker[1])
                if marker[2] is not None:
                    # clang writes an unprintable byte (any non-ASCII one) in octal,
                    # `\342\200\250`, gcc as it is; both escape `\` and `"` (#179).
                    path = octal_string(marker[2]) or "<unread>"
                    if path not in names:
                        names[path] = program(path)
                    name = names[path]
                continue
            origin = None if name is None else (name, number)
            number += 1
            if origin is None:
                if not groups or groups[-1][0] is not None:
                    groups.append((None, ["#include <another file>"]))
                continue
            text = raw.decode("utf-8", "replace")
            text = "" if _DIRECTIVE_LINE.match(text) else text
            if groups and groups[-1][0] == origin:
                groups[-1][1].append(text)
            else:
                groups.append((origin, [text]))
        self.origins = [origin for origin, _ in groups]
        self.text = "".join(" ".join(pieces) + "\n" for _, pieces in groups)
        # Each (name, line) -> its line in ``text``; 0 where it is there more than once.
        self.places = {}
        for number, origin in enumerate(self.origins, 1):
            if origin is not None:
                self.places[origin] = 0 if origin in self.places else number
        self.files = {origin[0] for origin in self.places}

    def line(self, name, number):
        """The line of ``text`` holding line ``number`` of ``name``: None if the output
        does not give it, 0 if it gives it more than once (a file included twice)."""
        return self.places.get((name, number))


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
        # What may call setjmp, or declare a static, an extern or a `using` name.
        self.setjmps = frozenset(hazards.get("setjmp", ()))
        self.declarers = frozenset(hazards.get("declares", ()))
        # What each of the program's macros may expand to name (macro_hazards).
        self.expansions = dict(hazards.get("expands", {}))
        # Where the compiler may read any line differently from the scan.
        self.unreadable = bool(
            _SPLICED_PAIR.search(text)
            or _SPLIT.search(self.text)
            or _RENUMBER.search(_SPLICE.sub("", self.blanked))
        )
        self._ends = {}
        # The keyword of each statement header that declares a name, by where the
        # declaration ends (`_declaration`'s ``found[0]``).
        self._header_words = {}

    def header(self, line, name):
        """The keyword (`for`, `while`, `if`, `switch` or `catch`) of the statement header
        that declares ``name`` on ``line``, from the one statement that declares it there;
        None if no header does, or if Peeky cannot tell (`declaration`)."""
        found = self.declaration(line, name, declaring=True)
        return None if found is None or not found[2] else self._header_words.get(found[0])

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

    def declaration(self, line, name, declaring=False):
        """``(position, depth, header, settled)``: where the declaration of ``name`` on
        ``line`` ends (the `;` or `)` after it), how many brackets of its statement are
        open there, whether a statement's header declares it (`for (int i = 0; ...)`),
        and whether that header sets it once, with no call (`_settled`); None if unknown.
        Of the statements starting on the line, the last naming ``name`` counts: a later
        end only withholds more (`long t; t = f();` has run only after the second).

        ``declaring``: the one statement that declares ``name`` instead (`long t = 11;`
        in `long t = 11; while (t > 20) t--;`, whose header only uses it), for where its
        scope and its block are; None unless just one does (review of PR #159)."""
        key = ("declaration", line, name, declaring)
        if key not in self._ends:
            self._ends[key] = self._declaration(line, name, declaring)
        return self._ends[key]

    def _declaration(self, line, name, declaring=False):
        blanked, starts = self.blanked, self.starts
        if self.unreadable or type(line) is not int or not 1 <= line <= len(starts):
            return None
        first = position = starts[line - 1]
        end_of_line = starts[line] if line < len(starts) else len(blanked)
        found, last = None, first
        # Each statement on the line that names the name, and each that declares it, as
        # ``found`` gives them.
        named, declared = [], []
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
                        declares = self._declares(opening.end(), closing, name)
                        if self._declares(closing + 1, end_of_line, name):
                            # Maybe declared again after the header: read it whole.
                            pass
                        elif semicolon is not None and self._names(opening.end(), semicolon, name):
                            settled = self._settled(word, opening.end(), semicolon, name)
                            found = (semicolon, 1, True, settled)
                            self._header_words[semicolon] = word
                            named.append(found)
                            if declares:
                                declared.append(found)
                            position = closing + 1
                            continue
                        else:
                            part = opening.end() if semicolon is None else semicolon + 1
                            settled = self._settled(word, part, closing, name)
                            found = (closing, 0, True, settled)
                            self._header_words[closing] = word
                            named.append(found)
                            if declares:
                                declared.append(found)
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
            if self._names(start, end, name):
                named.append((end, 0, False, False))
                # Where each plain statement starts, by its end: what it follows.
                self.__dict__.setdefault("statement_starts", {})[end] = start
                if self._declares(start, end, name):
                    declared.append(named[-1])
            position = end + 1
        if declaring:
            # One that reads as a declaration; else the one naming it at all (a structured
            # binding's `[a, b]`, a macro's argument), the line being where it is declared.
            found = (
                declared[0]
                if len(declared) == 1
                else named[0]
                if not declared and len(named) == 1
                else None
            )
        if found is None or not self._readable(line, first, max(last, found[0])):
            return None
        return found

    def scope_lines(self, line, name):
        """``(first, last)``: the lines on which the declaration of ``name`` on ``line``
        is in scope, as C and C++ read it: from ``line`` to the `}` closing the block
        around it, or, for one a statement's header declares (`while (long t = f())`,
        `switch (short t = g(); t)`, `if (auto p = f())`, a `for`'s, a range-`for`'s, a
        `catch`'s), to the end of that statement: its body, and an `if`'s `else`; for
        one that is a control statement's whole body, without braces (`while (c) long t
        = 8;`, `do long t = 9; while (0);`, `switch (c) case 1: long t = 4;`), its own
        statement. None if unknown (see ``declaration``; what a labelled statement
        follows)."""
        found = self.declaration(line, name, declaring=True)
        code, pairs = self._code()
        if found is None or pairs is None:
            return None
        position, depth, header, _ = found
        starts = self.starts
        if not header:
            sole = self._sole_statement(self.__dict__.get("statement_starts", {}).get(position))
            if sole is None:
                return None
            if sole:
                # A body of its own: in scope to its `;` (clang gives a `while`'s, a
                # `do`'s or a `switch`'s no block, so its block runs on after it).
                return line, bisect.bisect_right(starts, position)
            around = [
                opened
                for opened, closed in pairs.items()
                if opened < position < closed and code.startswith(("{", "<%"), opened)
            ]
            if not around:
                return None
            return line, bisect.bisect_right(starts, pairs[max(around)])
        if depth == 0:
            closing, opening = position, self._opening(position)
        else:
            inside = [
                opened
                for opened, closed in pairs.items()
                if opened < position < closed and code.startswith("(", opened)
            ]
            opening = max(inside) if inside else None
            closing = pairs.get(opening) if opening is not None else None
        if opening is None or closing is None:
            return None
        keyword = self._word_before(opening)
        if keyword == "constexpr":
            before = self._before(opening)
            keyword = self._word_before(before[1]) if before else None
        end = self._controlled(closing + 1, keyword)
        if end is None:
            return None
        return min(line, bisect.bisect_right(starts, opening)), bisect.bisect_right(starts, end)

    def _sole_statement(self, start):
        """Whether the statement at ``start`` is a control statement's whole body, with
        no braces of its own: after a header's `)`, `else` or `do`, or a label there
        (`switch (c) case 1: long t = 4;`). False after a block's edge or a `;`; None if
        unknown."""
        if start is None:
            return None
        at = start
        for _ in range(256):
            before = self._before(at)
            if before is None:
                return False
            token, position = before
            if token in ("{", "}", "<%", "%>", ";"):
                return False
            if token in ("else", "do"):
                return True
            if token == ")":
                opening = self._opening(position)
                if opening is None:
                    return None
                keyword = self._word_before(opening)
                if keyword in _HEADERS or keyword == "constexpr":
                    # A statement after a header's `)` is its body.
                    return True
                # Part of the statement (`long (*f)(int)`): back past its brackets.
                at = opening
                continue
            if token == ":":
                at = self._label_begin(position)
                if at is None:
                    return None
                continue
            # A word or a punctuator of the statement itself, which may have begun on
            # an earlier line (`long` / `t = f();`): back to where it begins.
            at = position
        return None

    def _label_begin(self, colon):
        """Where the label whose `:` is at ``colon`` begins (`case 1:`, `default:`,
        `again:`), or None."""
        before = self._before(colon)
        if before is None:
            return None
        if before[0] == "default":
            return before[1]
        if _WORD.fullmatch(before[0]):
            previous = self._before(before[1])
            if previous is None or previous[0] in ("{", "}", "<%", "%>", ";", ")", ":"):
                return before[1]
            if previous[0] in ("else", "do"):
                return before[1]
        # `case <expression>:`: back to its `case`, past brackets.
        depth, at = 0, colon
        for _ in range(64):
            before = self._before(at)
            if before is None:
                return None
            token, at = before
            if token in (")", "]", ":>"):
                depth += 1
            elif token in ("(", "[", "<:"):
                depth -= 1
            elif depth == 0 and token == "case":
                return at
            elif depth == 0 and token in ("{", "}", "<%", "%>", ";"):
                return None
        return None

    def _controlled(self, start, keyword=None):
        """Where the statement a header controls, from ``start``, ends (its `;` or `}`),
        an `if`'s `else` included (``keyword`` "if"), a `do` loop's `while (...);` and a
        `try` block's handlers too, and past any label (`again:`, `case 1:`, `default:`)
        or attribute (`[[likely]]`) before it; None if Peeky cannot tell: a statement it
        does not read (`else`, `__try`), or one that begins with no word, nor a bracket
        or `-`, `--` or `::` that can begin an expression."""
        code, pairs = self._code()
        token = _WORDS.search(code, start)
        if token is None:
            return None
        word = token[0]
        if word in ("{", "<%"):
            end = pairs.get(token.start())
        elif word in _HEADERS:
            opening = _WORDS.search(code, token.end())
            if opening is not None and opening[0] == "constexpr":
                opening = _WORDS.search(code, opening.end())
            closing = pairs.get(opening.start()) if opening and opening[0] == "(" else None
            end = None if closing is None else self._controlled(closing + 1, word)
        elif word == "do":
            end = self._do_end(token.end())
        elif word == "try":
            end = self._try_end(token.end())
        elif word == ";":
            end = token.start()
        elif word in ("case", "default") or self._labelled(token):
            colon = self._label_colon(token)
            end = None if colon is None else self._controlled(colon + 1)
        elif code.startswith("[[", token.start()):
            closing = pairs.get(token.start())
            end = None if closing is None else self._controlled(closing + 1)
        elif (_WORD.fullmatch(word) and word not in _UNREAD) or word in ("(", "[", "-", "--", "::"):
            end = self._statement(token.start())
        else:
            return None
        if end is None or keyword != "if":
            return end
        following = _WORDS.search(code, end + 1)
        if following is None or following[0] != "else":
            return end
        # `else if (...)` is read as an `if`, with its own `else`.
        return self._controlled(following.end())

    def _labelled(self, token):
        """Whether the word ``token`` (a match in ``_code``) is a label's: a `:` follows
        it, not `::`."""
        code = self._code()[0]
        following = _WORDS.search(code, token.end())
        return bool(
            _WORD.fullmatch(token[0])
            and following is not None
            and following[0] == ":"
            and not code[token.end() : following.start()].strip()
        )

    def _label_colon(self, token):
        """Where the `:` of the label that starts with the word ``token`` is (`again:`,
        `default:`, `case N::k ? 1 : 2:`); None if Peeky cannot tell."""
        code = self._code()[0]
        if token[0] != "case":
            following = _WORDS.search(code, token.end())
            if following is None or following[0] != ":":
                return None
            return following.start() if not code[token.end() : following.start()].strip() else None
        # A `case`'s expression, to its `:`: past brackets, `::`, and each `?`'s own `:`.
        depth, asked, at = 0, 0, token.end()
        while at < min(len(code), token.end() + 4096):
            char = code[at]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
                if depth < 0:
                    return None
            elif depth == 0 and char == ";":
                return None
            elif char == "?":
                asked += 1
            elif char == ":":
                if code.startswith("::", at):
                    at += 2
                    continue
                if depth == 0 and not code.startswith(":>", at):
                    if not asked:
                        return at
                    asked -= 1
            at += 1
        return None

    def _do_end(self, start):
        """Where a `do` loop whose body starts at ``start`` ends: the `;` after its
        `while (...)`; None if Peeky cannot tell."""
        code, pairs = self._code()
        body = self._controlled(start)
        if body is None:
            return None
        word = _WORDS.search(code, body + 1)
        if word is None or word[0] != "while":
            return None
        opening = _WORDS.search(code, word.end())
        closing = pairs.get(opening.start()) if opening and opening[0] == "(" else None
        if closing is None:
            return None
        semicolon = _WORDS.search(code, closing + 1)
        return semicolon.start() if semicolon and semicolon[0] == ";" else None

    def _try_end(self, start):
        """Where a `try` block that starts at ``start`` ends: the `}` of its last
        `catch (...) { … }`; None if Peeky cannot tell."""
        code, pairs = self._code()
        block = _WORDS.search(code, start)
        if block is None or block[0] not in ("{", "<%"):
            return None
        end, handlers = pairs.get(block.start()), 0
        while end is not None:
            word = _WORDS.search(code, end + 1)
            if word is None or word[0] != "catch":
                break
            opening = _WORDS.search(code, word.end())
            closing = pairs.get(opening.start()) if opening and opening[0] == "(" else None
            body = _WORDS.search(code, closing + 1) if closing is not None else None
            if body is None or body[0] not in ("{", "<%"):
                return None
            end, handlers = pairs.get(body.start()), handlers + 1
        return end if handlers else None

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

    def _settled(self, keyword, start, end, name):
        """Whether ``name``, a header's variable declared between ``start`` and ``end``,
        is set once, by no call: not a `while`'s (run again on each pass), nor one whose
        header may call (a `(`, a `{` that may construct, or a range-`for`'s `:`, whose
        iterator calls). Its own declarator needs an initializer (`for (int i; …)` sets
        nothing), and it and each declarator before it may hold only literals and
        operators: a name may be a macro that calls (`#define INIT make_i()`, or a
        library's `errno`), which the text cannot rule out (review of PR #214). Those
        after it run once it is set: `int i = 0, j = f` keeps `i`. In C++ only the first
        declarator counts: constructing one before it may call."""
        part = self.blanked[start:end]
        if keyword == "while" or re.search(r"[({]|<%|(?<!:):(?![:>])", part):
            return False
        for index, piece in enumerate(part.split(",")):
            before, equals, initializer = piece.partition("=")
            if not all(
                word[0] in ("true", "false", "nullptr") and word[0] not in self.expansions
                for word in _IDENTIFIER.finditer(initializer)
            ):
                return False
            declared = [word[0] for word in _IDENTIFIER.finditer(before)]
            if declared and declared[-1] == name:
                # In C++ constructing one before it may call (`X j = 0, *i = nullptr`
                # runs `X::X(int)` before `i` is set): only the first counts there.
                return bool(equals) and (index == 0 or not self.raw_strings)
        return False

    def jumped_into(self, line, name, span=None):
        """For a variable a statement's header on ``line`` declares: the line of a label
        after its declaration, before the block holding that statement closes, that a
        jump can reach without running the declaration (as `jumped_past` judges one: a
        `case` of a `switch` around it, or a label any `goto` of the function names),
        False if there is none, or None if unknown: an `#include` or a macro that may
        hold a label or a bracket there, or a block that never closes. A `goto` to a
        label in a one-line loop's body skips `int i = 0` (review of PR #214)."""
        key = ("into", line, name, span)
        if key not in self._ends:
            self._ends[key] = self._into(line, name, span)
        return self._ends[key]

    def _into(self, line, name, span):
        found = self.declaration(line, name, declaring=True)
        if found is None or self.unreadable:
            return None
        position, depth, header = found[0], found[1], found[2]
        blanked, starts = self.blanked, self.starts
        marks = list(_WORDS.finditer(blanked, position + 1))
        for index, mark in enumerate(marks):
            word = mark[0]
            after = marks[index + 1][0] if index + 1 < len(marks) else None
            if word in self.opaque or word == "<::":
                return None
            if word in _OPEN:
                depth += 1
            elif word in _CLOSE:
                depth -= 1
                if depth < 0:
                    end = bisect.bisect_right(starts, mark.start())
                    break
            elif word == "case" or (word == "default" and after == ":"):
                body = self._switch_body(mark.start())
                if body is None or body < position:
                    return bisect.bisect_right(starts, mark.start())
            elif (
                _WORD.fullmatch(word)
                and after == ":"
                and self._jumped_to(word, position, header, span)
            ):
                return bisect.bisect_right(starts, mark.start())
        else:
            return None
        if any(
            _INCLUDE.match(blanked, starts[number - 1]) and not self._transparent(number)
            for number in range(bisect.bisect_right(starts, position) + 1, end + 1)
        ):
            return None
        return False

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

    def _declares(self, start, end, name, bindings=True):
        """Whether ``name`` may be declared between ``start`` and ``end``: not inside an
        expression (after `=`, `?`, or within parentheses or brackets), and following a
        name other than a keyword that only uses it (`int i`, not `return i`), or `*`,
        `&` or `>` (`T *i`, `vector<int> i`), or a `,` of a declarator list; or, with
        ``bindings``, in a structured binding's brackets (`auto& [i, j]`)."""
        return any(
            self._declarator(start, word.start(), bindings)
            for word in _IDENTIFIER.finditer(self.blanked, start, end)
            if word[0] == name
        )

    def _declarator(self, start, position, bindings=True):
        """Whether the name at ``position`` may be declared there, read back no further
        than ``start`` (see ``_declares``)."""
        blanked = self.blanked
        at, depth, listed = position - 1, 0, False
        while at >= start:
            char = blanked[at]
            if char in ")]":
                depth += 1
            elif char in "([":
                if depth == 0:
                    if (
                        bindings
                        and char == "["
                        and self.raw_strings
                        and re.search(r"\bauto\s*&{0,2}\s*$", blanked[max(start, at - 64) : at])
                    ):
                        # A structured binding: `auto [i, j]`, `const auto& [i, j]`.
                        return True
                    # An argument or a subscript.
                    return False
                depth -= 1
            elif depth == 0 and char == ",":
                listed = True
            elif depth == 0 and char in "=?" and not listed:
                return False
            elif depth == 0 and char in ";{}:":
                break
            at -= 1
        if listed:
            # A declarator list's (or, over-cautiously, a comma operator's).
            return True
        at = position - 1
        while at >= start and blanked[at].isspace():
            at -= 1
        if at < start:
            return False
        if blanked[at] in "*&>":
            return True
        previous = self._word_before(position)
        if previous == "template" and self._member(position):
            # A member template's name (`P::template t<0>()`, #283), not a declaration.
            return False
        return previous is not None and previous not in _USES

    def _declaring(self, low, position):
        """Whether the name at ``position`` is surely one a declaration declares, in a
        statement that begins at or after ``low``: after its type (`long t = 3;`, `struct S
        *t;`, `std::vector<long> t;`, `P t;`), or after a `,` of a declaration that begins
        with a type's keyword (`long a = 1, *t;`). Unlike ``_declarator``, never where an
        expression may hold it: in brackets (an argument, a parameter, a header's), after
        a comma that may be an operator (`return use(0), t;`), an operator word (`ok and
        t`), a `*` or `&` after a lone name (`a * t`), or a template's arguments with no
        keyword or `::` beside them (`a < side() > t`; any `<` in C). Else False."""
        code = self._code()[0]
        at, depth = position - 1, 0
        while at >= low:
            char = code[at]
            if char in ")]":
                depth += 1
            elif char in "([":
                if depth == 0:
                    return False
                depth -= 1
            elif depth == 0 and char in ";{}":
                break
            elif (
                depth == 0 and char == ":" and ":" not in code[at - 1 : at] + code[at + 1 : at + 2]
            ):
                # A label's, or a conditional's (`c ? a : t`).
                break
            at -= 1
        else:
            # The statement must begin at ``low`` (its line's start, or a body's).
            before = self._before(low)
            if before is not None and before[0] not in (";", "{", "}", "<%", "%>"):
                return False
        tokens = [mark[0] for mark in _PREFIX.finditer(code, at + 1, position)]
        # Split at the commas outside brackets: a list of declarators, or an expression's.
        segments, current, nesting = [], [], 0
        for token in tokens:
            nesting += (token in ("(", "[", "<")) - (token in (")", "]", ">"))
            if nesting < 0:
                return False
            if token == "," and nesting == 0:
                segments.append(current)
                current = []
            else:
                current.append(token)
        segments.append(current)
        if len(segments) > 1:
            head = segments[0]
            if not head or head[0] not in _SPECIFIERS or head[1:2] == ["("]:
                return False
            return all(token in ("*", "&") or token in _CV for token in segments[-1])
        return self._typed(tokens)

    def _typed(self, tokens):
        """Whether ``tokens``, a statement's from its start up to a name, are surely a
        declaration's type for it (see ``_declaring``)."""
        typed, angled, names, last, index = False, False, 0, None, 0
        while index < len(tokens):
            token = tokens[index]
            if token in ("<", "[", "("):
                if token == "<" and not self.raw_strings:
                    # C has no templates: `a < side() > t` compares.
                    return False
                if token == "(" and last not in _TYPE_GROUPS:
                    return False
                if token == "[" and tokens[index + 1 : index + 2] != ["["]:
                    return False
                # A template's arguments, an attribute (`[[maybe_unused]]`), or a type's
                # parentheses: to the bracket closing it.
                closer, depth = {"<": ">", "[": "]", "(": ")"}[token], 0
                for index in range(index, len(tokens)):
                    depth += (tokens[index] == token) - (tokens[index] == closer)
                    if depth == 0:
                        break
                else:
                    return False
                angled = angled or token == "<"
                typed = typed or (token == "(" and last not in _ATTRIBUTES)
                last, index = closer, index + 1
                continue
            if token == "::":
                typed = True
            elif token not in ("*", "&"):
                if not _WORD.fullmatch(token) or token in _OPERAND_WORDS:
                    return False
                if token in _SPECIFIERS:
                    typed = True
                elif token not in _CV and token not in _TYPE_GROUPS:
                    names += 1
            last, index = token, index + 1
        if last is None or last == "::" or not (names or typed):
            return False
        if angled and not typed:
            # A template's arguments only beside other proof of a type (`std::vector<long>
            # t`, `const A<B> t`): `a < b > t` may compare.
            return False
        # A pointer's or a reference's declarator only after a type that is surely one
        # (`long *t`, `S::T &t`, `const P *t`, `P Q *t`); `a * t` may be an expression.
        return last not in ("*", "&") or typed or names > 1

    def _named_once(self, line, name):
        """Whether ``name`` appears exactly once on ``line`` (outside comments and
        strings)."""
        if type(line) is not int or not 1 <= line <= len(self.starts):
            return False
        begin = self.starts[line - 1]
        finish = self.starts[line] if line < len(self.starts) else len(self.blanked)
        found = _IDENTIFIER.finditer(self.blanked, begin, finish)
        return sum(word[0] == name for word in found) == 1

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
            # From the statement that declares it: a label after it on the line counts.
            found = self.declaration(line, name, declaring=True)
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
        found = self.declaration(line, name, declaring=True)
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
        unknown, any. Also a macro that may `goto` named there, or a computed `goto`, or
        a GNU `asm goto` anywhere in the source, whose labels are its operands
        (`__asm__ goto("…" : : : : inside)`, review of PR #214): then any label counts.
        The function is its lines ``span`` (first, last) when given, else the outermost
        braces around the declaration (in C++ maybe a namespace's: more counts)."""
        blanked = self.blanked
        if "computed" not in self.__dict__:
            self.computed = (
                re.search(r"(?<!\w)goto\s*\*", blanked) is not None
                or _ASM_GOTO.search(blanked) is not None
            )
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

        target = re.compile(rf"(?<!{_ID})goto\s+{re.escape(label)}(?!{_ID})")
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

    def returns_twice(self, first, last, extra=frozenset()):
        """``(lines, unknown)``: the lines from ``first`` to ``last`` (a function's own)
        that may call a function returning twice (`setjmp`, `sigsetjmp`, a macro that
        expands to one, ``extra`` naming those of headers outside the program), and
        whether Peeky cannot tell for some: the source unreadable, or a directive among
        them, which may bring one in."""
        if type(first) is not int or type(last) is not int or self.unreadable:
            return (), True
        first, last = max(first, 1), min(last, len(self.starts))
        if first > last:
            return (), True
        begin = self.starts[first - 1]
        finish = self.starts[last] if last < len(self.starts) else len(self.blanked)
        names = self.setjmps | frozenset(extra)
        lines = {
            bisect.bisect_right(self.starts, word.start())
            for word in _IDENTIFIER.finditer(self.blanked, begin, finish)
            if _RETURNS_TWICE.fullmatch(word[0]) or word[0] in names
        }
        unknown = any(
            _DIRECTIVE.match(self.blanked, self.starts[number - 1])
            for number in range(first, last + 1)
        )
        return tuple(sorted(lines)), unknown

    def block_storage(
        self,
        line,
        name,
        after=None,
        elsewhere=False,
        body=False,
        entering=False,
        own=None,
        until=None,
        others=False,
        scope=None,
    ):
        """For the local ``name`` the debugger shows at a stop on ``line`` (declared on
        line ``after``; None for a parameter): ``(kind, its line)`` of a declaration of
        ``name`` the code there names instead, which is not that local: one with
        `static`, `extern` or `thread_local` storage, a `typedef`, a C++ `using`
        declaration ("using"), an enumerator ("enumerator"; in C also one of an enum in
        a struct's body or a cast), a function a block declares ("function"), or a C++
        class or enumeration a block within the local's declares ("class"). In a block
        around the line or opening on it, after ``after`` and up to the line's end (one
        later on the line counts too), not inside a block closed before it, nor in text
        an `#if 0` leaves out. False if there is none; None if Peeky cannot tell: the
        source is unreadable or its brackets do not balance, one is under another
        conditional (`#ifdef X`), a macro that may declare one names it (given it, or in
        its expansion), or an `#include` is there, where ``elsewhere`` (what it brings
        in may declare one).

        ``body``: the stop is in a function wholly on the line (a lambda in a statement
        there, #169): where the line opens one function's or lambda's body, it is that
        one, and only its statements count, with those of each block in it (#290), not
        the statement holding it (a range-`for`'s binding names nothing inside a lambda
        in its range); where it opens several, the line is read as any other.

        ``("bodies", line)``: with ``body``, where the line holds several functions' bodies
        and a declaration there would hide the local: which function the frame runs, and
        so whether the declaration is its own, cannot be told.

        ``others``: the stop is in a known function that goes on past the line: a
        function's or a lambda's body wholly on the line is another function's code,
        and no block in it counts, as none of its statements does (#290).

        ``entering``: the stop is the first instruction of the statements that start on
        the line, so none of them has run: a range-`for` there whose header ends the
        line with its body's `{` is about to run its range's expression, where its
        binding is not declared yet (#169), so the binding names nothing at the stop.

        ``own``: the line of the shown local's own declaration where it may be a `static`
        (or `extern`, or a thread's own): an optimized build keeps a `static` it never
        changes as a constant, which lldb-dap lists among the frame's locals (#173). A
        declaration with that storage whose declarator for ``name`` is on that line is
        then the local itself, not one hiding it; only where ``name`` appears once on
        the line, so that no other declaration there can be the local.

        ``until``: a position in the text read for the line (the end of a name in a
        statement's header, ``header_before``): only what is declared up to it counts, as
        the line is otherwise read, so False there means nothing hides the local yet.

        ``scope``: a position on the line (a name there, ``named_outside``): only the
        blocks around it count, so what a block closed before it declares does not; and a
        header's structured binding counts only where the header holds the position, or
        opens a block around it. With ``until``, that is what hides the local there.

        Peeky reads a translation unit as the compiler preprocessed it
        (``Preprocessed``), where no macro or conditional is left and an `#include`
        line stands for a system header's text; the rest reads a source as written."""
        named = self.__dict__.setdefault("storage_named", {})
        if name not in named:
            named[name] = storage_named(self.text, name)
        if "include_lines" not in self.__dict__:
            self.include_lines = [
                number
                for number, start in enumerate(self.starts, 1)
                if _INCLUDE.match(self.blanked, start)
            ]
        # An `#include` from the local's declaration (or the source's start) to the line.
        first = after if type(after) is int else 1
        included = type(line) is not int or bisect.bisect_left(
            self.include_lines, first
        ) < bisect.bisect_right(self.include_lines, line)
        if not named[name] and not (elsewhere and included) and not self._names_declarers():
            # Nothing in the source can declare it so (the common case, read cheaply).
            return False
        if self.unreadable or type(line) is not int or not 1 <= line <= len(self.starts):
            return None
        code, pairs = self._code()
        if pairs is None:
            return None
        begin = self.starts[line - 1]
        finish = self.starts[line] if line < len(self.starts) else len(code)
        first = _NONBLANK.search(code, begin, finish)
        position = first.start() if first else begin
        lower = (
            self.starts[after - 1] if type(after) is int and 1 <= after <= len(self.starts) else 0
        )
        fresh = self._range_headers(begin, finish) if entering else frozenset()
        if own is not None and not self._named_once(own, name):
            own = None
        bodies = [
            opened
            for opened in (pairs if body else ())
            if begin <= opened < finish
            and code.startswith(("{", "<%"), opened)
            and self._brace(opened) in ("function", "lambda")
        ]
        cut = finish if until is None else until
        # Where the statement that holds the stop (or the position read) is.
        within = position if scope is None else scope
        if len(bodies) == 1:
            # The body's own statements, and those of each block in it (#290).
            opened = bodies[0]
            closed = min(pairs[opened], finish)
            blocks = [opened] + sorted(
                inner
                for inner in pairs
                if opened < inner < closed
                and code.startswith(("{", "<%"), inner)
                and self._opens_statements(inner)
            )
            if scope is not None:
                blocks = [opening for opening in blocks if opening < scope < pairs[opening]]
            return self._blocks_storage(
                code, blocks, closed, cut, name, after, lower, within, fresh, own
            )
        # Every block around the line, and every block of statements opening on it
        # before its end (`{ extern long gcount; total += use(gcount); }`, a statement
        # expression's `({`); not a type's body or an initializer there, read as part of
        # its statement, nor a lambda's, which another function runs.
        around = sorted(
            opened
            for opened, closed in pairs.items()
            if opened < finish
            and closed > position
            and code.startswith(("{", "<%"), opened)
            and (opened < position or self._opens_statements(opened))
        )
        if others:
            # A block in a function's or lambda's body wholly on the line is that
            # function's: the frame's goes on past the line (#290).
            wholly = [
                (opened, closed)
                for opened, closed in pairs.items()
                if begin <= opened
                and closed < finish
                and code.startswith(("{", "<%"), opened)
                and self._brace(opened) in ("function", "lambda")
            ]
            around = [
                block
                for block in around
                if not any(opened < block < closed for opened, closed in wholly)
            ]
        if scope is not None:
            around = [opened for opened in around if opened < scope < pairs[opened]]
        if not around:
            return False
        if elsewhere and any(
            _INCLUDE.match(self.blanked, self.starts[number - 1])
            for number in range(bisect.bisect_right(self.starts, max(around[0], lower)), line + 1)
        ):
            # An `#include` in a block (a fragment) may declare it so.
            return None
        found = self._blocks_storage(
            code, around, finish, cut, name, after, lower, within, fresh, own
        )
        if found and len(bodies) > 1:
            # A frame wholly on a line holding several functions' bodies: which one it
            # runs, and so whether what is found is its own, cannot be told (#299 review).
            return ("bodies", found[1])
        return found

    def _blocks_storage(self, code, blocks, finish, cut, name, after, lower, position, fresh, own):
        """``block_storage``'s verdict over ``blocks`` (where each opens, in order): each
        one's own statements, up to the next one's opening (the last's up to ``finish``),
        none past ``cut``."""
        for index, opened in enumerate(blocks):
            start = max(opened + 1, lower)
            stop = min(blocks[index + 1] if index + 1 < len(blocks) else finish, cut)
            if start >= stop:
                continue
            # A C++ class declared in the local's own block leaves the local named
            # (the variable hides it); one in a block within it hides the local.
            found = self._storage_declaration(
                code, start, stop, name, after, opened >= lower, position, fresh, own
            )
            if found is not False:
                return found
        return False

    def _range_headers(self, begin, finish):
        """Where each range-`for` (its `for`) starts on the line from ``begin`` to
        ``finish`` whose header ends the line with its body's `{` (nothing after it):
        `for (auto [t, u] : make(t)) {`. Not one whose parentheses hold a `?` (a `:`
        Peeky may misread) or run onto another line."""
        code, pairs = self._code()
        found = set()
        for opened in pairs or {}:
            if not (begin <= opened < finish and code.startswith(("{", "<%"), opened)):
                continue
            width = 2 if code.startswith("<%", opened) else 1
            if _NONBLANK.search(code, opened + width, finish):
                continue
            before = self._unattributed(opened)
            if before is None or before[0] != ")":
                continue
            parenthesis = self._opening(before[1])
            keyword = self._before(parenthesis) if parenthesis is not None else None
            if keyword is None or keyword[0] != "for" or keyword[1] < begin:
                continue
            if "?" in code[parenthesis : before[1]]:
                continue
            depth = 0
            for mark in _TOKENS.finditer(code, parenthesis + 1, before[1]):
                if mark[0] in _OPEN:
                    depth += 1
                elif mark[0] in _CLOSE:
                    depth -= 1
                elif mark[0] == ":" and depth == 0:
                    found.add(keyword[1])
                    break
        return frozenset(found)

    def range_header(self, line, name):
        """The lines (first, last) of a C++ range-`for`'s header that holds ``line``, whose
        structured binding declares ``name`` and whose header also names ``name`` outside
        the binding, in its range's expression or an init-statement: `for (const auto &[t,
        u] : make(t))`. That code runs before the binding exists (#283), where ``name`` is
        an outer one; but a mention need not be a read (`sizeof(t)`, a lambda's body), so
        a caller can only say the header names it. A member's or a qualified name does not
        count (`p.t`, `p->t`, `n::t`, `p.template t<0>()`). Else None."""
        if not self.raw_strings:
            return None
        code = self._code()[0]
        for keyword, at, start, closing in self._headers(line):
            if keyword != "for":
                continue
            binding = self._range_binding(start, closing)
            if binding is None or not any(
                word[0] == name for word in _IDENTIFIER.finditer(code, *binding)
            ):
                continue
            for word in _IDENTIFIER.finditer(code, start + 1, closing):
                if word[0] != name or binding[0] < word.start() < binding[1]:
                    continue
                if not self._member(word.start()):
                    first = bisect.bisect_right(self.starts, at)
                    return first, bisect.bisect_right(self.starts, closing)
        return None

    def header_before(self, line, name, after=None, entering=False, own=None, others=False):
        """(keyword, first line, last line, always) of a statement header (`if`, `switch`,
        `while` or `for`) holding ``line`` that names ``name`` where nothing hides the
        local of that name yet, in its condition, an init-statement (`if (long k = t;
        k)`) or a range's expression: `if (t) { static long t = 5; use(t); }`, where only
        the `static` the line declares later hides it (``block_storage``, read with
        ``after``, ``entering``, ``own`` and ``others`` as it is). Running that header may
        read the outer local, or not (a mention need not be a read, #283). A member's or a
        qualified name does not count (`p.t`, `n::t`, `p.template t<0>()`), nor one in a
        range-`for`'s structured binding. None if no header does, or Peeky cannot tell.

        The scan is lexical: any other `t` the header spells is a mention, also one it
        declares itself (`for (long t = 0; ...)`) or a lambda's parameter there, which may
        weaken a true note into a hedge.

        ``always``: whether the hedge holds at every stop on the line, not only until the
        header has run: a part naming ``name`` runs on each pass (a `while`'s condition, a
        `for`'s condition or increment), or the declaration is in the header itself (a
        block of a lambda there, which another function runs, never this one)."""
        if not self.block_storage(line, name, after, True, False, entering, own, others=others):
            return None
        code = self._code()[0]
        found = []
        for keyword, at, start, closing in self._headers(line):
            binding = self._range_binding(start, closing) if keyword == "for" else None
            # A `while`'s condition runs on each pass, as a classic `for`'s condition and
            # increment do (after the first of its two `;`); the rest runs once.
            again = closing if keyword != "while" else start
            if keyword == "for" and binding is None:
                depth, semicolons = 0, []
                for mark in _TOKENS.finditer(code, start + 1, closing):
                    depth += (mark[0] in _OPEN) - (mark[0] in _CLOSE)
                    if depth == 0 and mark[0] == ";":
                        semicolons.append(mark.start())
                if len(semicolons) == 2:
                    again = semicolons[0]
            named = []
            for word in _IDENTIFIER.finditer(code, start + 1, closing):
                if word[0] != name or self._member(word.start()):
                    continue
                if binding is not None and binding[0] < word.start() < binding[1]:
                    continue
                # Read up to the name's end: a declaration it completes (a `static`'s
                # own) is no mention of the outer local.
                hidden = self.block_storage(
                    line, name, after, True, False, entering, own, word.end(), others
                )
                if hidden is False:
                    named.append(word.start() > again)
            if not named:
                continue
            inner = self.block_storage(
                line, name, after, True, False, entering, own, closing + 1, others
            )
            lines = bisect.bisect_right(self.starts, at), bisect.bisect_right(self.starts, closing)
            found.append((keyword, *lines, any(named) or inner is not False))
        if not found:
            return None
        # The first such header names the statement.
        return (
            found[0][0],
            min(first for _, first, _, _ in found),
            max(last for _, _, last, _ in found),
            any(always for *_, always in found),
        )

    def named_outside(
        self, line, name, after=None, entering=False, own=None, others=False, body=False
    ):
        """Where ``line`` also names ``name`` outside the scope of the declaration that
        hides the local of that name there (``block_storage``, read with ``after``,
        ``entering``, ``own``, ``others`` and ``body`` as it is): "before" that
        declaration (`use(t); { static long t = 5; use(t); }`), "after" the block (or
        statement) holding it ends (`do { static long t = 5; use(t); } while (t--);`),
        "both", or "unsure": where Peeky cannot tell whether that declaration is in scope
        (where a statement a header controls ends is unknown: unbalanced code). There the
        code may read the outer local, or another variable. None if nowhere.

        A member's or a qualified name does not count, nor a name a declaration there
        surely declares (``_declaring``: `long t = 3;`, the local's own; not `return
        use(0), t;` or `ok and t`), nor one a statement's header names before the
        declaration, which ``header_before`` tells (except with ``body``, where it is not
        asked). What a header declares is in scope through all it controls, an `if`'s
        `else` too. With ``body``, only the frame's own body counts. As in
        ``header_before``, the scan is lexical: any other `t` the line spells is a
        mention, also a lambda's own, which may weaken a true note into a hedge."""
        found = self.block_storage(line, name, after, True, body, entering, own, others=others)
        if not found or found[0] == "bodies":
            return None
        code, pairs = self._code()
        begin = self.starts[line - 1]
        finish = self.starts[line] if line < len(self.starts) else len(code)
        low, high = begin, finish
        if body:
            bodies = [
                opened
                for opened in pairs
                if begin <= opened < finish
                and code.startswith(("{", "<%"), opened)
                and self._brace(opened) in ("function", "lambda")
            ]
            if len(bodies) != 1:
                return None
            low, high = bodies[0] + 1, min(pairs[bodies[0]], finish)
        headers = [] if body else self._headers(line)

        def read(until, scope=None):
            return self.block_storage(
                line, name, after, True, body, entering, own, until, others, scope
            )

        places = set()
        for word in _IDENTIFIER.finditer(code, low, high):
            if word[0] != name or self._member(word.start()):
                continue
            if self._declaring(low, word.start()):
                continue
            # Whatever is declared up to the name, and what of it is in scope there.
            earlier, hidden = read(word.end()), False
            if earlier is False:
                if any(opening < word.start() < closing for _, _, opening, closing in headers):
                    # A header's, before the declaration: ``header_before``'s.
                    continue
            else:
                hidden = read(word.end(), word.start())
                if hidden:
                    continue
            # A declarator read to its end: a function's (`long t(long);`), or a C++
            # enumerator's, declared once its enum's body closes (`enum { t = 5 };`).
            later = read(self._statement_end(word.end(), high), word.start())
            if later and later[0] in ("function", "enumerator"):
                continue
            if hidden is None or earlier is None:
                # Whether the declaration is in scope there, Peeky cannot tell.
                places.add("unsure")
            else:
                places.add("before" if earlier is False else "after")
        if not places:
            return None
        if "unsure" in places:
            return "unsure"
        return places.pop() if len(places) == 1 else "both"

    def _statement_end(self, position, end):
        """Where the statement going on at ``position`` ends, no further than ``end``: past
        its `;`, or past the bracket that closes around it (a header's `)`, an enum's
        `}`)."""
        code = self._code()[0]
        depth = 0
        for mark in _TOKENS.finditer(code, position, end):
            token = mark[0]
            if token in _OPEN:
                depth += 1
            elif token in _CLOSE:
                depth -= 1
                if depth < 0:
                    return mark.end()
            elif token == ";" and depth == 0:
                return mark.end()
        return end

    def _headers(self, line):
        """(keyword, where it starts, where its `(` opens, where its `)` closes) of each
        statement header (`if`, `if constexpr`, `switch`, `while`, `for`) whose
        parentheses hold part of ``line``."""
        if self.unreadable or type(line) is not int or not 1 <= line <= len(self.starts):
            return []
        code, pairs = self._code()
        if pairs is None:
            return []
        begin = self.starts[line - 1]
        finish = self.starts[line] if line < len(self.starts) else len(code)
        found = []
        for start, closing in pairs.items():
            if not (start < finish and closing >= begin and code.startswith("(", start)):
                continue
            keyword = self._before(start)
            if keyword is not None and keyword[0] == "constexpr" and self.raw_strings:
                keyword = self._before(keyword[1])
            if keyword is not None and keyword[0] in ("if", "switch", "while", "for"):
                found.append((keyword[0], keyword[1], start, closing))
        return sorted(found, key=lambda header: header[2])

    def _range_binding(self, start, closing):
        """Where a range-`for` header's structured binding's brackets open and close, for
        the header's parentheses from ``start`` to ``closing``: `for (const auto &[t, u] :
        v)`; else None (another `for`, or a C source)."""
        if not self.raw_strings:
            return None
        code, pairs = self._code()
        # The binding's brackets at the header's own depth (after its last `;`, an
        # init-statement's end), then the range's `:`.
        binding, depth = None, 0
        for mark in _TOKENS.finditer(code, start + 1, closing):
            token = mark[0]
            if token in _OPEN:
                if (
                    depth == 0
                    and token in ("[", "<:")
                    and re.search(r"\bauto\s*&{0,2}\s*$", code[start + 1 : mark.start()])
                ):
                    binding = (mark.start(), pairs[mark.start()])
                depth += 1
            elif token in _CLOSE:
                depth -= 1
            elif depth == 0 and token == ";":
                binding = None
            elif depth == 0 and token == ":" and binding is not None:
                return binding
        return None

    def _range_colon(self, start, closing):
        """(where its declaration starts, where its `:` is) of a C++ range-`for` header
        whose parentheses run from ``start`` to ``closing`` (`for (const auto &[t, u] :
        v)`, also after an init-statement's `;`); else None. Not a classic `for`, whose
        condition may hold a `?` and a `:`."""
        if not self.raw_strings:
            return None
        code = self._code()[0]
        depth, begin, semicolons = 0, start + 1, 0
        for mark in _TOKENS.finditer(code, start + 1, closing):
            token = mark[0]
            if token in _OPEN:
                depth += 1
            elif token in _CLOSE:
                depth -= 1
            elif depth == 0 and token == ";":
                begin, semicolons = mark.end(), semicolons + 1
            elif depth == 0 and token == ":":
                if semicolons > 1 or "?" in code[begin : mark.start()]:
                    return None
                first = _NONBLANK.search(code, begin, mark.start())
                return (first.start() if first else mark.start()), mark.start()
        return None

    def _member(self, position):
        """Whether the name at ``position`` is a member's or a qualified one: after `.`,
        `->` or `::`, also with `template` between (`p->template t<0>()`)."""
        before = self._before(position)
        if before is not None and before[0] == "template":
            before = self._before(before[1])
        return before is not None and before[0] in (".", "->", "::")

    def _opens_statements(self, opening):
        """Whether the brace at ``opening`` opens statements this function runs: a block,
        or a statement expression (`({`)."""
        if self._brace(opening) == "statement":
            return True
        before = self._unattributed(opening)
        return before is not None and before[0] == "("

    def _names_declarers(self):
        """Whether the source names a macro that may declare a static or extern."""
        if "declarer_named" not in self.__dict__:
            self.declarer_named = bool(self.declarers) and any(
                word[0] in self.declarers for word in _IDENTIFIER.finditer(self.blanked)
            )
        return self.declarer_named

    def _conditions(self):
        """{line: "dead" or "maybe"}: lines an `#if 0` (or the `#else` of an `#if 1`)
        leaves out, and lines under any other conditional, which may be compiled or not."""
        if "conditions" not in self.__dict__:
            states, stack = {}, []
            for number, start in enumerate(self.starts, 1):
                end = self.starts[number] if number < len(self.starts) else len(self.blanked)
                found = re.match(
                    r"[ \t\f\v]*(?:#|%:)[ \t\f\v]*(if|ifdef|ifndef|elif|else|endif)\b(.*)",
                    self.blanked[start:end],
                )
                if found:
                    kind, rest = found[1], found[2].strip()
                    if kind in ("if", "ifdef", "ifndef"):
                        literal = {"0": "dead", "1": "live"}.get(rest) if kind == "if" else None
                        stack.append(literal or "maybe")
                    elif kind == "elif" and stack:
                        stack[-1] = "maybe"
                    elif kind == "else" and stack:
                        stack[-1] = {"dead": "live", "live": "dead"}.get(stack[-1], "maybe")
                    elif kind == "endif" and stack:
                        stack.pop()
                    continue
                if "dead" in stack:
                    states[number] = "dead"
                elif "maybe" in stack:
                    states[number] = "maybe"
            self.conditions = states
        return self.conditions

    def _storage_declaration(
        self,
        code,
        start,
        stop,
        name,
        after=None,
        nested=True,
        within=None,
        fresh=frozenset(),
        own=None,
    ):
        """(kind, line) of the first declaration of ``name`` that hides an outer local
        (see ``block_storage``) among the statements from ``start`` to ``stop`` at their
        own depth: a block closed before ``stop`` is skipped, and an initializer's
        braces and a type's body (`static struct { long v; } q`) are kept in their
        statement; False if none; None if Peeky cannot tell (see ``block_storage``).
        ``nested``: whether they are in a block within the local's own (where a C++
        class of that name hides it); ``within``: where the stop's line begins (a
        header's structured binding counts only in the statement holding the stop);
        ``fresh``: where range-`for` statements start whose binding is not declared yet
        at the stop (see ``block_storage``'s ``entering``); ``own``: see
        ``block_storage``."""
        conditions = self._conditions()
        depth, statement, blocks, hidden = 0, [], [], set()
        body = None  # a type's body in the statement: [its depth, enum?, previous, names]
        # C gives the enumerators of an enum anywhere in a block's statements (in a
        # struct's body, a cast, another enum's value) the block's scope: each enum body
        # open there, as [its depth, the token before at its level]; and where an `enum`
        # specifier began, until its body opens or it ends.
        enums, specifier = [], None

        def judged(statement, last=False):
            # A macro given ``name``, or expanding to name it, may declare it.
            given = any(text == name for text, _, _ in statement)
            if any(given or name in self.expansions.get(macro, {name}) for macro in hidden):
                return None
            # Whether the stop is in the statement: it reaches the stop's line, or is the
            # header of a block around it, or of what holds the stop past its `)` (an
            # `if`'s `else` too: `if (auto [t, u] = f(); ok) { ... } else use(t);`).
            last = last or bool(statement and within is not None and statement[-1][1] >= within)
            unsure = False
            if not last and statement and within is not None:
                controls = self._controls(statement, within)
                unsure, last = controls is None, bool(controls)
            if statement and statement[0][1] in fresh:
                last = unsure = False
            found = self._storage_of(statement, name, after, code, nested, last, own)
            if not found and unsure:
                # What the header declares may be in scope there or not: Peeky cannot
                # tell where the statement it controls ends.
                if self._storage_of(statement, name, after, code, nested, True, own):
                    return None
            if found:
                state = conditions.get(found[1])
                if state == "maybe":
                    return None
            return found

        for token in _DECLARATION_TOKENS.finditer(code, start, stop):
            text = token[0]
            state = conditions.get(bisect.bisect_right(self.starts, token.start()))
            if state == "dead":
                continue
            if not self.raw_strings and not blocks:
                if enums and depth == enums[-1][0] + 1:
                    if text in _CLOSE:
                        enums.pop()
                    elif text == name and enums[-1][1] in ("{", "<%", ","):
                        at = bisect.bisect_right(self.starts, token.start())
                        return None if state == "maybe" else ("enumerator", at)
                    else:
                        enums[-1][1] = text
                if text == "enum":
                    specifier = depth
                elif specifier is not None:
                    if text in ("{", "<%"):
                        enums.append([depth, text])
                        specifier = None
                    elif (text in (";", ",", "=") and depth == specifier) or (
                        text in _CLOSE and depth <= specifier
                    ):
                        specifier = None
            if body is not None:
                # Inside a type's body: only an enum's enumerators can hide the name.
                if text in _OPEN:
                    depth += 1
                elif text in _CLOSE:
                    depth -= 1
                    if depth == body[0]:
                        if body[1] and name in body[3]:
                            at = bisect.bisect_right(self.starts, body[3][name])
                            return None if conditions.get(at) == "maybe" else ("enumerator", at)
                        body = None
                        # The body, in its statement's place (`struct { long v; } q`
                        # names no class `q`).
                        statement.append(("{}", token.start(), depth))
                        continue
                elif body[1] and depth == body[0] + 1 and body[2] in ("{", ",", "<%"):
                    if _WORD.fullmatch(text):
                        body[3].setdefault(text, token.start())
                if depth == body[0] + 1:
                    body[2] = text
                continue
            if depth == 0 and not blocks and text in self.declarers:
                # A macro that may declare a static: Peeky cannot see what, beyond the
                # names its expansion and its arguments give.
                if not code[token.end() : token.end() + 64].lstrip().startswith("("):
                    if name in self.expansions.get(text, {name}):
                        return None
                else:
                    hidden.add(text)
            if text in _OPEN:
                if (
                    text in ("{", "<%")
                    and depth == len(blocks)
                    and not any(word == "=" and level == 0 for word, _, level in statement)
                ):
                    words = [word for word, _, level in statement if level == 0]
                    if not blocks and set(words) & _TYPE_BODIES and words[-1:] != [")"]:
                        # A type's body: its statement goes on after it (`} q = { 5 };`).
                        scoped = "enum" in words and words[words.index("enum") + 1 :][:1] in (
                            ["class"],
                            ["struct"],
                        )
                        body = [depth, "enum" in words and not scoped, "{", {}]
                        depth += 1
                        continue
                    # A nested block (or a function's body): what came before it is
                    # judged as a statement, and what is in it is out of scope here.
                    if (found := judged(statement)) is not False:
                        return found
                    statement, hidden = [], set()
                    blocks.append(depth)
                    depth += 1
                    continue
                depth += 1
            elif text in _CLOSE:
                depth = max(depth - 1, 0)
                if blocks and depth == blocks[-1] and text in ("}", "%>"):
                    blocks.pop()
                    statement, hidden = [], set()
                    continue
            if blocks:
                continue
            if depth == 0 and text == ";":
                if (found := judged(statement)) is not False:
                    return found
                statement, hidden = [], set()
                continue
            statement.append((text, token.start(), depth))
        # The statement the region ends in: the stop's, or the header of a block around it.
        return judged(statement, True)

    def _controls(self, statement, position):
        """Whether ``position`` is in what the statement header among a statement's
        tokens (as ``_storage_declaration`` reads them) controls, past its `)`: its body,
        and an `if`'s `else` (``_controlled``), where what the header declares is in
        scope. None where Peeky cannot tell."""
        pairs = self._code()[1] or {}
        for index, (text, _, depth) in enumerate(statement):
            if depth != 0 or text not in _HEADERS:
                continue
            following = statement[index + 1 : index + 3]
            if following and following[0][0] == "constexpr":
                following = following[1:]
            if not following or following[0][0] != "(":
                return False
            closing = pairs.get(following[0][1])
            if closing is None:
                return None
            if position <= closing:
                return False
            end = self._controlled(closing + 1, text)
            return None if end is None else position <= end
        return False

    def _storage_of(
        self, statement, name, after=None, code=None, nested=True, last=False, own=None
    ):
        """(kind, line) if the tokens of one statement declare ``name`` so that it hides
        an outer local (see ``block_storage``): with static or external storage or as a
        typedef (`static long q = 2`, `extern long a, gcount`), by a C++ `using`
        declaration, as a function (`long use2(long);`, not on line ``after``, the
        local's own), or, in C++ and ``nested`` (a block within the local's), as a class
        or an enumeration (`struct q { ... };`, "class"), or as a structured binding
        (`auto [q, r] = s;`; in a header, `for (auto [q, r] : pairs)`, only where the
        statement is ``last``, the one the stop is in; "binding"); else False. Names in
        a function declarator's parameters (`extern long helper(long q);`) are not
        declared there. One whose declarator for ``name`` is on line ``own`` is the
        local itself (see ``block_storage``)."""
        words = [text for text, _, depth in statement if depth == 0]
        if not words:
            return False
        at = bisect.bisect_right(self.starts, statement[0][1])
        # A header's binding (also after `else` or a label: `else if (auto [q, r] = s; q)`).
        if self.raw_strings and (last or not _HEADERS.intersection(words)):
            if (bound := self._binding(statement, name, after)) is not None:
                return ("binding", bound)
        # A lambda's body (or an initializer's braces) in the statement is a scope of its
        # own: what it declares names nothing in the block (review of PR #159).
        braced = _braced(statement)
        if self.raw_strings and nested:
            for index, (text, position, _) in enumerate(statement):
                if (
                    not braced[index]
                    and text in _CLASS_KEYS
                    and _class_name(statement, index) == name
                ):
                    return ("class", bisect.bisect_right(self.starts, position))
        if words[0] == "using" and self.raw_strings:
            if len(words) > 1 and words[1] == "namespace":
                return False
            return ("using", at) if name in words else False
        code = self._code()[0] if code is None else code
        # A thread's own (`static thread_local`) is listed apart, so it is said first.
        storage = next(
            (word for word in words if word in ("thread_local", "_Thread_local", "__thread")),
            next((word for word in words if word in _HIDERS), None),
        )
        if storage is not None:
            # Each declarator, without its initializer (`long a = q, q = 2` declares the
            # second) or a function declarator's parameters.
            declarator, initializing, skipping, lines = [], False, None, set()
            for index, (text, position, depth) in enumerate(statement):
                if skipping is not None:
                    if text in (")", ":>", "]") and depth == skipping - 1:
                        skipping = None
                    continue
                if depth == 0 and text == ",":
                    initializing = False
                    continue
                if depth == 0 and text == "=":
                    initializing = True
                    continue
                if initializing:
                    continue
                if text == "(" and index and _parameters_follow(statement[index - 1][0]):
                    following = code[position + 1 : position + 64].lstrip()[:1]
                    if following not in ("*", "&", "^", "("):
                        skipping = depth
                        continue
                declarator.append(text)
                if text == name:
                    lines.add(bisect.bisect_right(self.starts, position))
            if name not in declarator:
                return False
            if own is not None and storage != "typedef" and lines == {own}:
                # The shown local's own declaration (a `static` kept as a constant).
                return False
            return (storage, at)
        if at == after or words[0] in _NOT_DECLARING or "=" in words:
            return False
        for index, (text, _, depth) in enumerate(statement):
            if (
                depth == 0
                and text == name
                and 0 < index < len(statement) - 1
                and statement[index + 1][0] == "("
                and statement[index - 1][2] == 0
                # After a type's name, or its body (`struct S { ... } use2(long);`).
                and (_WORD.fullmatch(statement[index - 1][0]) or statement[index - 1][0] == "{}")
                and statement[index - 1][0] not in _NOT_DECLARING
            ):
                return ("function", at)
        return False

    def _binding(self, statement, name, after=None):
        """The line of a structured binding naming ``name`` among a statement's tokens
        (`auto [q, r]`, `const auto& [q, r]`), or None. One on line ``after`` that is
        the only declaration of ``name`` there is the local's own."""
        braced = _braced(statement)
        for index, (text, position, depth) in enumerate(statement[:-1]):
            if text != "auto" or statement[index + 1][0] not in ("[", "<:") or braced[index]:
                continue
            names = []
            for following, _, level in statement[index + 2 :]:
                if following in ("]", ":>") and level == depth:
                    break
                if level == depth + 1 and _WORD.fullmatch(following):
                    names.append(following)
            if name not in names:
                continue
            line = bisect.bisect_right(self.starts, position)
            if line == after and not self._declared_beside(line, name):
                continue
            return line
        return None

    def _declared_beside(self, line, name):
        """Whether ``line`` declares ``name`` otherwise than by a structured binding,
        outside the bodies of lambdas (or functions) there (#169): a local of the name
        in one is that function's own, whose frame no binding outside it names."""
        return any(
            self._declares(low, high, name, bindings=False) for low, high in self._outside(line)
        )

    def _outside(self, line):
        """The (start, end) spans of ``line``'s code outside the bodies of lambdas (or
        functions) there, in order."""
        code, pairs = self._code()
        begin = self.starts[line - 1]
        finish = self.starts[line] if line < len(self.starts) else len(self.blanked)
        cuts = []
        for opened, closed in (pairs or {}).items():
            if begin <= opened < finish:
                cut = (opened, min(closed + 1, finish))
            elif opened < begin < closed < finish:
                cut = (begin, closed + 1)
            else:
                continue
            if code.startswith(("{", "<%"), opened) and self._brace(opened) in (
                "function",
                "lambda",
            ):
                cuts.append(cut)
        spans, position = [], begin
        for low, high in sorted(cuts):
            if low > position:
                spans.append((position, low))
            position = max(position, high)
        if position < finish:
            spans.append((position, finish))
        return spans

    def binds(self, line, name):
        """Whether ``line`` declares ``name`` as a C++ structured binding's name (`auto [t,
        u] = make(i)[0];`, `const auto &[t, u]`, a range-`for`'s) and in no other way,
        outside the bodies of lambdas (or functions) there (#291). A variable of reference
        type that the build declares there is then the reference it holds to a pair's or
        tuple's element, which the binding names."""
        if (
            not self.raw_strings
            or self.unreadable
            or type(line) is not int
            or not 1 <= line <= len(self.starts)
        ):
            return False
        key = ("binds", line, name)
        if key not in self._ends:
            spans = self._outside(line)
            self._ends[key] = any(
                self._bracketed(low, high, name) for low, high in spans
            ) and not any(self._declares(low, high, name, bindings=False) for low, high in spans)
        return self._ends[key]

    def _bracketed(self, start, end, name):
        """Whether ``name`` between ``start`` and ``end`` is in a structured binding's
        brackets (`auto [t, u]`, `const auto &[t, u]`), which may open on an earlier line
        (`auto [first,` / `second] = p;`)."""
        return any(
            self._binding_bracket(word.start()) is not None
            for word in _IDENTIFIER.finditer(self.blanked, start, end)
            if word[0] == name
        )

    def _binding_bracket(self, position):
        """(where the `[` of the structured binding whose brackets hold the name at
        ``position`` opens, its `&` or `&&` or ""), or None where none does."""
        blanked = self.blanked
        at, depth = position - 1, 0
        while at >= max(0, position - 4096):
            char = blanked[at]
            if char in ")]":
                depth += 1
            elif char in "([":
                if depth == 0:
                    found = _BINDING_LEAD.search(blanked[max(0, at - 64) : at])
                    return (at, found[1]) if char == "[" and found else None
                depth -= 1
            elif depth == 0 and char in ";{}=?":
                return None
            at -= 1
        return None

    def binding(self, line, name):
        """``(reference, initializer, ranged)`` of the C++ structured binding that names
        ``name`` on ``line`` (as ``binds`` finds it, #291): whether it binds by reference
        (`auto &`, `const auto &`, `auto &&`), the text of what it binds (`make(0)[0]` in
        `auto &[t, u] = make(0)[0];`, `p` in `auto [t, u](p)`, the range `m` of `for (const
        auto &[k, v] : m)`), and whether it is a range-`for`'s. None where no binding names
        it there, or its initializer does not end."""
        found = self._initializer(line, name)
        if found is None:
            return None
        reference, low, high, mark = found
        return reference, " ".join(self.blanked[low:high].split()), mark == ":"

    def _initializer(self, line, name):
        """``(reference, start, end, mark)`` of the structured binding naming ``name`` on
        ``line`` (``binding``'s): whether it binds by reference, where the text of what it
        binds starts and ends in ``blanked``, and the mark before it (`=`, `:`, `(` or
        `{`); None as for ``binding``."""
        if not self.binds(line, name):
            return None
        blanked = self.blanked
        for low, high in self._outside(line):
            for word in _IDENTIFIER.finditer(blanked, low, high):
                found = self._binding_bracket(word.start()) if word[0] == name else None
                if found is None:
                    continue
                opened, reference = found
                closing = self._closing_bracket(opened)
                if closing is None:
                    return None
                lead = re.compile(r"\s*").match(blanked, closing + 1).end()
                mark = blanked[lead : lead + 1]
                if mark in ("(", "{"):
                    shut = self._closing_bracket(lead)
                    span = None if shut is None else (lead + 1, shut)
                elif mark in ("=", ":"):
                    text = self._expression(lead + 1)
                    span = None if text is None else (lead + 1, lead + 1 + len(text))
                else:
                    span = None
                if span is None:
                    return None
                return bool(reference), *span, mark
        return None

    def _closing_bracket(self, opened):
        """Where the bracket that opens at ``opened`` closes, or None."""
        blanked, depth = self.blanked, 0
        for at in range(opened, min(len(blanked), opened + 65536)):
            char = blanked[at]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
                if depth == 0:
                    return at
        return None

    def _expression(self, start):
        """The text from ``start`` to the `;` or the unopened `)` that ends it (a
        range-`for`'s range), or None."""
        blanked, depth = self.blanked, 0
        for at in range(start, min(len(blanked), start + 65536)):
            char = blanked[at]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                if depth == 0:
                    return blanked[start:at] if char == ")" else None
                depth -= 1
            elif char == ";" and depth == 0:
                return blanked[start:at]
        return None

    def leaves_elements(self, name, scope, region, line, bound, scalar=None, passes=None):
        """Whether the code leaves the element of the standard container local ``name``
        that the structured binding ``bound``, declared on ``line``, refers to alive while
        the binding is in scope (#297): ``scope`` is the lines the local is in scope on,
        from its declaration (a parameter's: its function's), ``region`` the binding's
        (Statements.scope_lines).

        The binding's own initializer is ``name`` alone, a range `for`'s range (`for
        (auto &[k, v] : m)`), or one subscript of it (`= v[0]`, `(v[0])`). In ``region``,
        ``name`` is only that, or a call of a member that gives a value (`m.size()`:
        ``_VALUES``). Elsewhere in ``scope`` it is only its declaration, such a call, a
        statement that calls a member and drops what it gives (`m.insert(…);`), assigns
        it or an element (`m = …;`, `v[1].x = 2;`), or another range `for` over it by
        value, or by a binding whose names are kept as ours are. So nothing keeps a
        pointer, iterator or reference to it, or to an element, to change or end one
        through. The binding's names are kept too (``_names_kept``). Never in the body of
        a lambda or of a local class's function, which may run anywhere, and with no
        directive or `#include` in ``scope``. False where any of that may not hold, or
        Peeky cannot tell.

        ``scalar(name, line)``: whether the binding's name declared on ``line`` refers to a
        number, a character or a pointer; ``passes(function, index)``: whether every
        function of that name takes its argument ``index`` by value (``_names_kept``)."""
        key = ("leaves", name, tuple(scope), tuple(region), line, bound)
        if key not in self._ends:
            self._ends[key] = self._leaves_elements(
                name, scope, region, line, bound, scalar, passes
            )
        return self._ends[key]

    def _offsets(self, lines):
        """Where the text of the lines ``lines`` (first, last) starts and ends, or None."""
        starts = self.starts
        low, high = lines
        if type(low) is not int or type(high) is not int or not 1 <= low <= high <= len(starts):
            return None
        return starts[low - 1], (starts[high] if high < len(starts) else len(self.blanked))

    def _running_bodies(self, low, high, own):
        """The (opening, closing) of the bodies of lambdas and of local classes' functions
        that open between ``low`` and ``high``, which may run anywhere: not the one
        ``own`` is in (a parameter's scope holds its own function's body)."""
        code, pairs = self._code()
        return [
            (opened, closed)
            for opened, closed in (pairs or {}).items()
            if low <= opened < high
            and not opened < own < closed
            and code.startswith(("{", "<%"), opened)
            and self._brace(opened) in ("function", "lambda")
        ]

    def _leaves_elements(self, name, scope, region, line, bound, scalar, passes):
        found = self._initializer(line, bound) if type(line) is int else None
        code, pairs = self._code()
        if found is None or pairs is None or not name or self.unreadable:
            return False
        within, binding = self._offsets(scope), self._offsets(region)
        if within is None or binding is None or not within[0] <= binding[0] <= binding[1]:
            return False
        if binding[1] > within[1]:
            return False
        own = self._own(found, name)
        if own is None:
            return False
        starts = self.starts
        if any(
            _DIRECTIVE.match(self.blanked, starts[number - 1])
            for number in range(scope[0], scope[1] + 1)
        ):
            return False
        bodies = self._running_bodies(*within, own)
        if not self._names_kept(line, bound, binding, bodies, scalar, passes):
            return False
        lexemes, places = self._lexemes()
        first = True
        for word in _IDENTIFIER.finditer(code, *within):
            if word[0] != name:
                continue
            at = word.start()
            index = bisect.bisect_left(places, at)
            before = lexemes[index - 1][1] if index > 0 else None
            if before in (".", "->", "::", ".*", "->*"):
                continue  # a member's or another scope's name
            if any(opened < at < closed for opened, closed in bodies):
                return False
            declaration, first = first, False
            if at == own:
                continue
            after = lexemes[index + 1][1] if index + 1 < len(lexemes) else None
            member = lexemes[index + 2][1] if after == "." and index + 2 < len(lexemes) else None
            called = index + 3 < len(lexemes) and lexemes[index + 3][1] == "("
            if member in _VALUES and called:
                continue
            if binding[0] <= at < binding[1]:
                return False
            if (
                declaration
                and bisect.bisect_right(starts, at) == scope[0]
                and before is not None
                and (before in (">", ">>") or _WORD.fullmatch(before))
                and before not in _NOT_TYPES
            ):
                continue
            statement = self._starts_statement(index)
            if statement and member is not None and member != "operator" and called:
                # A call whose result the statement drops: `m.insert({1, 2});`.
                shut = pairs.get(lexemes[index + 3][0])
                ending = bisect.bisect_left(places, shut + 1) if shut is not None else None
                if ending is not None and ending < len(lexemes) and lexemes[ending][1] == ";":
                    continue
            if statement and self._assigned(index + 1):
                continue
            if self._other_keeps(index, name, bodies, scalar, passes):
                continue
            return False
        return True

    def _own(self, found, name):
        """Where ``name`` stands in a structured binding's initializer (``found``,
        ``_initializer``'s) that is a range `for`'s range ``name`` alone, or one subscript
        of it (`v[i]`, not `v[i].x`, `v[i][j]` or `v`); else None."""
        code = self._code()[0]
        _, low, high, mark = found
        whole = re.compile(rf"\s*({re.escape(name)})(?!{_ID})\s*").match(code, low, high)
        if whole is None or (mark == ":" and whole.end() != high):
            return None
        if mark != ":":
            closing = self._closing_bracket(whole.end()) if code[whole.end()] == "[" else None
            if closing is None or closing >= high or code[closing + 1 : high].strip():
                return None
        return whole.start(1)

    def _lexemes(self):
        """The tokens of ``_code``: ([(where it starts, its text)], [where each starts])."""
        if "lexemes" not in self.__dict__:
            found = [(mark.start(), mark[0]) for mark in _LEXEME.finditer(self._code()[0])]
            self.lexemes = (found, [at for at, _ in found])
        return self.lexemes

    def _starts_statement(self, index):
        """Whether the token ``index`` of ``_lexemes`` starts a statement: it follows `;`, a
        brace, `else`, `do`, or a control statement's header (`if (c)`)."""
        lexemes, _ = self._lexemes()
        if index == 0:
            return True
        before = lexemes[index - 1]
        if before[1] in _BOUNDARIES:
            return True
        if before[1] == ")":
            opening = self.closes.get(before[0])
            return opening is not None and self._word_before(opening) in (
                "if",
                "while",
                "for",
                "switch",
            )
        return False

    def _assigned(self, index):
        """Whether the tokens from ``index`` are only subscripts and members' names (`[1]`,
        `.x`; none at all) and then an assignment: `= 2`, `+= 1`."""
        lexemes, places = self._lexemes()
        pairs = self._code()[1] or {}
        while index < len(lexemes):
            at, text = lexemes[index]
            if text == "[" and at in pairs:
                index = bisect.bisect_left(places, pairs[at] + 1)
            elif (
                text == "."
                and index + 1 < len(lexemes)
                and _WORD.fullmatch(lexemes[index + 1][1])
                and lexemes[index + 1][1] not in ("operator", "template")
            ):
                index += 2
            else:
                return text in _ASSIGNING
        return False

    def _other_keeps(self, index, name, bodies, scalar, passes):
        """Whether the container ``name`` at token ``index`` of ``_lexemes`` is what another
        structured binding walks or binds (`for (auto &[a, b] : m)`, `auto &[r, s] =
        v[1];`) or a range `for` copies (`for (auto e : m)`), leaving no pointer or
        reference to an element behind: it takes each by value, or its names are kept
        (``_names_kept``)."""
        lexemes, _ = self._lexemes()
        code = self._code()[0]
        if index < 2 or lexemes[index - 1][1] not in (":", "=", "(", "{", "<%"):
            return False
        if lexemes[index - 1][1] == ":" and index + 1 < len(lexemes):
            # A range `for` over it that copies each element: `for (auto e : m)`.
            opening = self.closes.get(lexemes[index + 1][0])
            if (
                lexemes[index + 1][1] == ")"
                and opening is not None
                and self._word_before(opening) == "for"
                and not re.search(r"[&\[]", code[opening + 1 : lexemes[index][0]])
            ):
                return True
        shut = lexemes[index - 2]
        opening = self.closes.get(shut[0]) if shut[1] == "]" else None
        first = _IDENTIFIER.search(code, opening + 1, shut[0]) if opening is not None else None
        bracket = self._binding_bracket(first.start()) if first is not None else None
        if bracket is None or bracket[0] != opening:
            return False
        line = bisect.bisect_right(self.starts, opening)
        found = self._initializer(line, first[0]) if self.binds(line, first[0]) else None
        if found is None or self._own(found, name) != lexemes[index][0]:
            return False
        if not found[0]:
            return True  # by value: each name its own copy
        region = self.scope_lines(line, first[0])
        span = self._offsets(region) if region is not None else None
        return span is not None and self._names_kept(line, first[0], span, bodies, scalar, passes)

    def _names_kept(self, line, bound, span, bodies, scalar, passes):
        """Whether every name of the structured binding naming ``bound`` on ``line`` is a
        number, a character or a pointer (``scalar(name, line)``), and each of its uses in
        ``span`` (offsets) only reads it, or assigns it as a statement (`b += 1;`): never
        `&b`, a reference bound to it, an argument a call may take by reference
        (``passes(function, index)`` says which take theirs by value), a branch of `?:`,
        nor in the body of a lambda or a local class's function (``bodies``). So no
        pointer or reference to an element is made through them (#297)."""
        names = self._bound_names(line, bound)
        if not names or scalar is None:
            return False
        lexemes, places = self._lexemes()
        brackets = self._binding_bracket(self._name_at(line, bound))
        shut = self._closing_bracket(brackets[0])
        for name in names:
            if not scalar(name, line):
                return False
            for word in _IDENTIFIER.finditer(self._code()[0], *span):
                if word[0] != name or brackets[0] < word.start() < shut:
                    continue
                index = bisect.bisect_left(places, word.start())
                if index > 0 and lexemes[index - 1][1] in (".", "->", "::", ".*", "->*"):
                    continue  # a member's or another scope's name
                if any(opened < word.start() < closed for opened, closed in bodies):
                    return False
                if not self._only_read(index, lambda word: scalar(word, None), passes):
                    return False
        return True

    def _name_at(self, line, name):
        """Where ``name`` stands in the brackets of the structured binding on ``line``."""
        for low, high in self._outside(line):
            for word in _IDENTIFIER.finditer(self.blanked, low, high):
                if word[0] == name and self._binding_bracket(word.start()) is not None:
                    return word.start()
        return None

    def bound_names(self, line, bound):
        """The names in the brackets of the structured binding naming ``bound`` on
        ``line`` (`[k, v]`), or [] where Peeky cannot read them."""
        return list(self._bound_names(line, bound))

    def _bound_names(self, line, bound):
        """The names in the brackets of the structured binding naming ``bound`` on
        ``line``: `[k, v]`; [] where Peeky cannot read them."""
        at = self._name_at(line, bound) if self.binds(line, bound) else None
        found = self._binding_bracket(at) if at is not None else None
        shut = self._closing_bracket(found[0]) if found is not None else None
        if shut is None:
            return []
        text = self.blanked[found[0] + 1 : shut]
        names = [part.strip() for part in text.split(",")]
        return names if all(_WORD.fullmatch(each) for each in names) else []

    def _only_read(self, index, scalar, passes):
        """Whether the use of a binding's name (a number or a character) at token ``index``
        of ``_lexemes`` makes no pointer or reference to it, read from the expression
        around it (``_operand``) and what takes that expression (#297 review rounds 1, 2).

        The expression may hold only numbers and characters: the binding's names and
        locals of such types (``scalar(name)``), number literals, casts to such types
        (`(long)`), parentheses and the built-in operators, which any operator between
        such operands is: no call, member, subscript, `&k`, `?:` or overloaded operator.
        Such an expression, past the name alone, is a new value, which nothing can refer
        to the name through. The name alone may be: a statement (`k;`, `++k;`), the
        target of one that assigns such an expression (`b += 1;`), what initializes a
        local of such a type (`long w = k;`, `auto w = k;`) or is assigned to one (`w +=
        k;`), a condition (`if (k)`), returned, or an argument of a call whose every
        function takes it by value (``passes(function, index)``)."""
        lexemes, _ = self._lexemes()

        def text(at):
            return lexemes[at][1] if 0 <= at < len(lexemes) else None

        found = self._operand(index, scalar)
        if found is None:
            return False
        first, last, alone = found
        before, after = text(first - 1), text(last + 1)
        statement = self._starts_statement(first)
        if text(first) in ("++", "--"):
            return statement and after == ";"  # `++k;`: `++k` is the name itself
        if statement and after == ";":
            return True  # its value dropped
        if after in _ASSIGNING:
            # `b += 1;`: the target of a statement that assigns it a number.
            ending = self._operand(last + 2, scalar, value=True)
            return (
                alone
                and statement
                and ending is not None
                and ending[0] == last + 2
                and text(ending[1] + 1) == ";"
            )
        if not alone:
            # A new value: whatever takes it holds no pointer or reference to the name.
            return before not in ("&", "*") and after not in _ASSIGNING
        if before in _ASSIGNING and after in (";", ","):
            back = first - 2
            while back >= 0 and not self._starts_statement(back + 1):
                back -= 1
            target = [text(at) for at in range(back + 1, first - 1)]
            if before != "=":
                return len(target) == 1 and bool(scalar(target[0]))
            if len(target) == 1:
                return bool(scalar(target[0]))
            # `long w = k;`: a local of a number's type, no reference or pointer.
            return all(word in _NUMBER_TYPES for word in target[:-1]) and bool(
                _WORD.fullmatch(target[-1] or "")
            )
        if before == "return" and after == ";":
            return True
        if after == "?" and before in (";", "{", "}", "(", "=", "return", ","):
            return True  # a condition
        if before == "(" and after == ")":
            opening = lexemes[first - 1][0]
            if self._word_before(opening) in ("if", "while", "switch"):
                return True  # a condition
        if before in ("(", ",") and after in (")", ","):
            return self._passed_by_value(first, passes)
        return False

    def _operand(self, index, scalar, value=False):
        """(first, last, alone) tokens of the expression of numbers around token ``index``
        (``_only_read``'s, ``value``: from there to its end), and whether it is a name
        alone (in parentheses or not); None where a token in it is none such an expression
        may hold, or it is not well formed."""
        lexemes, _ = self._lexemes()
        closes, pairs = self.closes, self._code()[1] or {}

        def text(at):
            return lexemes[at][1] if 0 <= at < len(lexemes) else None

        def grouping(at):
            # A `(` that groups: no call's, header's or operator's (`sizeof(`).
            word = text(at - 1)
            return not (
                word in (")", "]", ">")
                or (word is not None and _WORD.fullmatch(word) and word not in ("return",))
            )

        first, last, depth = index, index, 0
        if not value:
            # Out to where the expression starts: a token no operand or operator is, or
            # a bracket around it that does not group.
            while first > 0:
                word = text(first - 1)
                if word in (")", "]"):
                    opened = closes.get(lexemes[first - 1][0])
                    if (
                        word == "]"
                        or opened is None
                        or self._word_before(opened)
                        in (
                            "if",
                            "while",
                            "for",
                            "switch",
                        )
                    ):
                        break
                    first = bisect.bisect_left(self._lexemes()[1], opened)
                    if not grouping(first) and text(first + 1) not in _NUMBER_TYPES:
                        return None  # a call's: `f(x) * k`
                    continue
                if word == "(":
                    if not grouping(first - 1):
                        break
                    depth += 1
                    first -= 1
                    continue
                if word in _BUILT_IN or word in ("++", "--") or self._number(word, scalar):
                    first -= 1
                    continue
                break
        # Out to where it ends, past the parentheses it opened on the way.
        while last + 1 < len(lexemes):
            word = text(last + 1)
            if word == "(":
                shut = pairs.get(lexemes[last + 1][0])
                if shut is None:
                    return None
                last = bisect.bisect_left(self._lexemes()[1], shut)
                continue
            if word == ")":
                if depth == 0:
                    break
                depth -= 1
                last += 1
                continue
            if word in _BUILT_IN or word in ("++", "--") or self._number(word, scalar):
                last += 1
                continue
            break
        if depth:
            return None
        before = text(first - 1)
        if text(first) in _BUILT_IN | {"++", "--"} and (
            before in (")", "]", '"', "'")
            or (before is not None and before[0].isdigit())
            or (
                before is not None
                and _WORD.fullmatch(before)
                and before not in _NOT_CALLS | _NOT_TYPES
            )
        ):
            return None  # `e + k`: an operator between the name and an operand of another type
        # Well formed, of numbers alone: operand, operator, operand…
        operand, stack, names = True, 0, []
        at = first
        while at <= last:
            word = text(at)
            if operand:
                if word == "(" and text(at + 1) in _NUMBER_TYPES:
                    # A cast to a number's type: `(long)`, `(unsigned long)`.
                    shut = bisect.bisect_left(self._lexemes()[1], pairs.get(lexemes[at][0], -1))
                    if not all(text(inner) in _NUMBER_TYPES for inner in range(at + 1, shut)):
                        return None
                    at = shut + 1
                    continue
                if word == "(":
                    stack += 1
                elif word in ("-", "+", "!", "~"):
                    pass
                elif word in ("++", "--") and at == first and last == at + 1 and not value:
                    pass  # `++k`, a statement's alone (``_only_read``)
                elif self._number(word, scalar):
                    names.append(word)
                    operand = False
                else:
                    return None
            elif word == ")" and stack:
                stack -= 1
            elif word in ("++", "--"):
                pass
            elif word in _BUILT_IN:
                operand = True
            else:
                return None
            at += 1
        if operand or stack:
            return None
        alone = len(names) == 1 and all(
            text(at) in ("(", ")") or at == index for at in range(first, last + 1)
        )
        return first, last, alone

    def _number(self, word, scalar):
        """Whether ``word`` is a number literal of the language's own syntax and suffixes
        (``_NUMBER_LITERAL``), or names a number or a character (``scalar(word)``)."""
        if word is None:
            return False
        if word[0].isdigit() or (word[0] == "." and word[1:2].isdigit()):
            # A literal with a suffix of the program's (`1_sink`) is a call of its operator.
            return bool(_NUMBER_LITERAL.fullmatch(word))
        return bool(_WORD.fullmatch(word)) and word not in _NUMBER_TYPES and bool(scalar(word))

    def _passed_by_value(self, first, passes):
        """Whether the name at token ``first``, a whole argument of a call, goes to a
        parameter every function of the call's name takes by value (``passes``)."""
        lexemes, _ = self._lexemes()
        depth, position, at = 0, 0, first - 1
        while at >= 0:
            word = lexemes[at][1]
            if word in (")", "]", "}"):
                depth += 1
            elif word in ("(", "[", "{"):
                if depth == 0:
                    break
                depth -= 1
            elif word == "," and depth == 0:
                position += 1
            elif word in ("<", "?") and depth == 0:
                return False
            at -= 1
        if at < 1 or lexemes[at][1] != "(" or passes is None:
            return False
        callee = lexemes[at - 1][1]
        if not _WORD.fullmatch(callee) or callee in _NOT_CALLS or callee in _NOT_TYPES:
            return False
        return bool(passes(callee, position))

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
        `({`, `new T[1]{`, `decltype(W{}){`, a compound literal). The block of an `if
        constexpr (...)`, `if consteval` or `if !consteval` is a statement's (#289)."""
        before = self._unattributed(opening)
        if before is None:
            return "other"
        token, at = before
        if token == "consteval" and self.raw_strings:
            # `if consteval {`, `if !consteval {` (also `if not consteval {`).
            previous = self._before(at)
            if previous is not None and previous[0] in ("!", "not"):
                previous = self._before(previous[1])
            if previous is not None and previous[0] == "if":
                return "statement"
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
        parameters = self._parameters(opening)
        word = self._before(parameters) if parameters is not None else None
        if word is None:
            return "other"
        if word[0] == "constexpr" and self.raw_strings:
            # `if constexpr (...) {`: the `if`'s block, not a function's body.
            previous = self._before(word[1])
            if previous is not None and previous[0] == "if":
                return "statement"
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
            # A template's: `f<int>(int a) {` (C++ only), or a generic lambda's with its
            # own template parameters: `[]<class T>(T v) {`.
            angle = self._angle(word[1])
            before = self._before(angle) if angle is not None else None
            if before is not None and before[0] in ("]", ":>"):
                if self._introducer(self._opening(before[1])):
                    return "lambda"
            return "function"
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
        return not (_word_char(token[0]) or token in (")", "]", ":>", ">", "}"))

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
        if _word_char(code[at]):
            start = at
            while start > 0 and _word_char(code[start - 1]):
                start -= 1
            return code[start : at + 1], start
        for mark in _TOKENS.finditer(code, max(0, at - 3), at + 1):
            if mark.end() == at + 1:
                return mark[0], mark.start()
        return code[at], at

    def _parameters(self, opening):
        """Where the parentheses before the brace at ``opening`` open, when only what may
        follow a function's parameters stands between them (`const`, `noexcept(true)`,
        `mutable`, `override`, a lambda's `constexpr`, attributes, a trailing return type:
        `_qualified`), else None. A header's or a macro's parentheses count too (`if (c)
        {`, `FOR_EACH(x) {`): the caller tells them apart."""
        code = self._code()[0]
        at = opening
        for _ in range(64):
            before = self._before(at)
            if before is None or before[0] in (";", "{", "}", "<%", "%>"):
                return None
            token, position = before
            if token in ("]", ":>"):
                # Past an attribute, `[[deprecated("x")]]`; a lambda's introducer or a
                # subscript has no parameters before it.
                bracket = self._opening(position)
                if bracket is None or not code.startswith(("[[", "<:<:", "[<:", "<:["), bracket):
                    return None
                at = bracket
                continue
            if token != ")":
                # A specifier's or a trailing return type's word or punctuator.
                at = position
                continue
            start = self._opening(position)
            if start is None:
                return None
            word = self._before(start)
            if word is None or word[0] not in _SPECIFYING:
                closing = position
                break
            # Past `noexcept(...)`, `__attribute__((...))`, `decltype(...)`, read to their
            # paired parentheses.
            at = word[1]
        else:
            return None
        if not self._qualified(closing + 1, opening):
            return None
        return start

    def _qualified(self, start, end):
        """Whether only what may follow a function's parameters stands from ``start`` to
        ``end`` (its brace): qualifiers and specifiers (``_QUALIFIERS``), `&`, `&&`,
        `noexcept(...)`, `throw(...)`, `__attribute__((...))`, `__declspec(...)` and
        `[[...]]` (each group read to its paired bracket, however deep), then maybe a
        trailing return type or `requires` clause, with no `;`, `{` or `}`."""
        code, pairs = self._code()
        at = start
        while at < end:
            found = _NONBLANK.search(code, at, end)
            if found is None:
                return True
            at = found.start()
            if code.startswith(("->", "requires"), at):
                if code.startswith("requires", at) and _word_char(code[at + 8 : at + 9] or " "):
                    return False
                return not any(char in ";{}" for char in code[at:end])
            if code[at] == "&":
                at += 1
                continue
            if code.startswith("[[", at):
                closing = (pairs or {}).get(at)
                if closing is None or closing >= end:
                    return False
                at = closing + 1
                continue
            word = _WORD.match(code, at)
            if word is None:
                return False
            at = word.end()
            if word[0] in ("noexcept", "throw", "__attribute__", "__declspec"):
                group = _NONBLANK.search(code, at, end)
                if group is not None and code[group.start()] == "(":
                    closing = (pairs or {}).get(group.start())
                    if closing is None or closing >= end:
                        return False
                    at = closing + 1
                    continue
                if word[0] != "noexcept":
                    return False
            elif word[0] not in _QUALIFIERS:
                return False
        return True

    def _angle(self, closing):
        """Where the `<` matching the `>` at ``closing`` is (`]<class T>`), or None."""
        code = self._code()[0]
        depth = 0
        for at in range(closing, max(closing - 512, -1), -1):
            char = code[at]
            if char == ">" and not code.startswith("->", at - 1):
                depth += 1
            elif char == "<":
                depth -= 1
                if depth == 0:
                    return at
            elif char in ";{}":
                return None
        return None

    def _label(self, colon):
        """Whether the `:` at ``colon`` ends a label (`case 1:`, `default:`, `out:`)."""
        code = self._code()[0]
        start = max(code.rfind(mark, 0, colon) for mark in ";{}:")
        head = code[start + 1 : colon].strip()
        return bool(re.fullmatch(r"case\b[\s\S]*|default|" + _NAME, head)) and "?" not in head

    def _word_before(self, position):
        """The identifier just before ``position`` (spaces skipped), or None."""
        blanked, at = self.blanked, position - 1
        while at >= 0 and blanked[at].isspace():
            at -= 1
        end = at + 1
        while at >= 0 and _word_char(blanked[at]):
            at -= 1
        word = blanked[at + 1 : end]
        return word if _WORD.fullmatch(word) else None
