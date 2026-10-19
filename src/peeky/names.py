"""Function names as people read them."""

import re

_RUST_FRAME = re.compile(r"::h[0-9a-f]{16}$")
_RUST_ESCAPES = {
    "$LT$": "<",
    "$GT$": ">",
    "$RF$": "&",
    "$BP$": "*",
    "$C$": ",",
    "$SP$": "@",
    "$LP$": "(",
    "$RP$": ")",
}


def unicode_escape(match):
    """A legacy `$u<hex>$` escape as its character; an impossible one stays as written."""
    code = int(match[1], 16)
    return chr(code) if code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else match[0]


def display_function(name):
    """A debugger frame name for display. Rust frames (`…::h<16 hex>`) lose their hash
    and legacy escapes, so `top::f::_$u7b$$u7b$closure$u7d$$u7d$::h0123…` reads
    `top::f::{{closure}}`, as `describe` shows it. Other languages are unchanged (Java's
    `lambda$run$0` keeps its `$`). lldb-dap on macOS marks an optimized frame
    `… [opt]`; Linux's never does, so the mark is dropped."""
    name = str(name or "")
    if name.endswith(" [opt]") and _RUST_FRAME.search(name[: -len(" [opt]")]):
        name = name[: -len(" [opt]")]
    if not _RUST_FRAME.search(name):
        return name
    parts = []
    for part in _RUST_FRAME.sub("", name).split("::"):
        if part.startswith("_$"):
            part = part[1:]
        if "$" in part:
            part = part.replace("..", "::")
            for escape, character in _RUST_ESCAPES.items():
                part = part.replace(escape, character)
            part = re.sub(r"\$u([0-9a-f]{1,6})\$", unicode_escape, part)
        parts.append(part)
    return "::".join(parts)
