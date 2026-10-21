"""Lines as the tools and compilers Peeky reads count them (#179).

`str.splitlines()` also breaks at U+2028, U+2029, U+0085, `\\x0b`, `\\x0c` and `\\x1c`
to `\\x1e`, which may be part of a file name (`main\\u2028x.c` in dwarfdump's
`DW_AT_decl_file`, a span in a MIR dump) or of a source line (a form feed in a Python
file, a U+2028 in a C string). No tool or compiler Peeky reads ends a line there: they
end one at `\\n` (`\\r\\n` too; `read_text` has already turned a C, Java or Python
source's lone `\\r` into `\\n`).
"""


def split_lines(text, keepends=False):
    """``text``'s lines, each ended by `\\n` (or `\\r\\n`) alone; as with
    `str.splitlines`, a final line break opens no empty line."""
    parts = text.split("\n")
    last = parts.pop()
    if keepends:
        found = [part + "\n" for part in parts]
    else:
        found = [part[:-1] if part.endswith("\r") else part for part in parts]
    if last:
        found.append(last[:-1] if not keepends and last.endswith("\r") else last)
    return found
