"""Lines and quoted names as the tools and compilers Peeky reads write them (#179).

`str.splitlines()` also breaks at U+2028, U+2029, U+0085, `\\x0b`, `\\x0c` and `\\x1c`
to `\\x1e`, which may be part of a file name (`main\\u2028x.c` in dwarfdump's
`DW_AT_decl_file`, a span in a MIR dump) or of a source line (a form feed in a Python
file, a U+2028 in a C string). No tool or compiler Peeky reads ends a line there:
tools end one at `\\n`; rustc at `\\n` alone (`\\r\\n` too); clang, javac and Python also
at a lone `\\r` (#188 review). So a source is read as it is (``read_source``: no newline
translation), and split by its own compiler's rule (``source_lines``).
"""

import re
from pathlib import Path


def split_lines(text, keepends=False):
    """``text``'s lines, each ended by `\\n` (or `\\r\\n`) alone; as with
    `str.splitlines`, a final line break opens no empty line. A `\\r` not followed by
    `\\n` is part of its line, at the end too."""
    parts = text.split("\n")
    last = parts.pop()
    if keepends:
        found = [part + "\n" for part in parts]
    else:
        found = [part[:-1] if part.endswith("\r") else part for part in parts]
    if last:
        found.append(last)
    return found


def read_source(path, errors="strict"):
    """``path``'s text as it is: `Path.read_text` would turn a lone `\\r` into `\\n`,
    a line break rustc does not count."""
    return Path(path).read_bytes().decode("utf-8", errors)


def source_lines(text, rust):
    """The lines of a source's text (read by ``read_source``) as its compiler numbers
    them: rustc (``rust``) ends a line at `\\n` alone (`\\r\\n` too), clang, javac and
    Python also at a lone `\\r`. A form feed or vertical tab ends none."""
    return split_lines(text if rust else re.sub(r"\r\n?", "\n", text))


# One escape of LLVM's `raw_ostream::write_escaped`, which dwarfdump's strings and
# clang's `-E` line markers use: `\\`, `\"`, `\t`, `\n`, or an unprintable byte
# (any non-ASCII one) as exactly three octal digits. gcc's markers escape `\` and `"`
# alone, and write any other byte as it is.
_OCTAL_ESCAPE = re.compile(rb'\\(?:([\\"tn])|([0-3][0-7]{2}))')
_SIMPLE = {b"\\": b"\\", b'"': b'"', b"t": b"\t", b"n": b"\n"}


def octal_string(raw):
    """The name LLVM's tools (or gcc) wrote as ``raw`` (str or bytes, without its
    quotes), or None where it is not one they write: any other escape (`\\x41`, `\\q`,
    `\\400`), a `"` not escaped, or bytes that are not UTF-8 (#188 review)."""
    data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    found, at = [], 0
    while (backslash := data.find(b"\\", at)) >= 0:
        escape = _OCTAL_ESCAPE.match(data, backslash)
        if escape is None:
            return None
        found.append(data[at:backslash])
        found.append(_SIMPLE[escape[1]] if escape[1] else bytes([int(escape[2], 8)]))
        at = escape.end()
    found.append(data[at:])
    if any(b'"' in piece for piece in found[::2]):
        return None
    try:
        return b"".join(found).decode("utf-8")
    except UnicodeDecodeError:
        return None
