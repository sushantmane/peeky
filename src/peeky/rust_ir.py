"""Which Rust locals live in memory, read from the compiler's own LLVM IR.

At opt-level 0 rustc still keeps a local that is assigned once and never borrowed
in an SSA register. For the debugger it stores a copy in a stack slot named
`<name>.dbg.spill`; LLDB shows and writes that copy, but the program never reads it,
so changing it would report a successful intervention that changed nothing.
Locals the program really keeps in memory are declared on an ordinary alloca.
`#[inline(always)]` functions are inlined even at opt-level 0: their declarations
carry an `inlinedAt` location and renamed slots (`x.dbg.spill.i`), and are never
treated as provably in memory.
"""

import re

MAX_IR_BYTES = 256 * 1024 * 1024
_ENTRY = re.compile(
    r"^(![0-9]+) = (?:distinct )?!(DILocalVariable|DISubprogram|DILexicalBlock"
    r"|DILexicalBlockFile|DILocation)\((.*)\)\s*$"
)
_FIELD = {
    "name": re.compile(r'(?:^|, )name: "((?:[^"\\]|\\.)*)"'),
    "scope": re.compile(r"(?:^|, )scope: (![0-9]+)"),
    "linkage": re.compile(r'(?:^|, )linkageName: "([^"]*)"'),
    "inlined": re.compile(r"(?:^|, )inlinedAt: (![0-9]+)"),
}
# (pointer, variable, location); the location is the record's last operand.
_DECLARES = (
    re.compile(r"#dbg_declare\(ptr %([-\w.$]+), (![0-9]+), .*, (![0-9]+)\)\s*$"),
    re.compile(
        r"@llvm\.dbg\.declare\(metadata ptr %([-\w.$]+), metadata (![0-9]+), .*\), "
        r"!dbg (![0-9]+)"
    ),
)
_SPILL = re.compile(r"\.dbg\.spill")
_HASH = re.compile(r"17(h[0-9a-f]{16})E$")


def memory_locals(path):
    """{(function hash, local name): True only if every declaration is a real slot}.

    The function hash is the `h<16 hex>` of the legacy-mangled linkage name, which
    LLDB also ends Rust frame names with. Locals without any declaration, or whose
    function cannot be identified, are absent: absent means "not provably in memory".
    """
    if path.stat().st_size > MAX_IR_BYTES:
        return {}
    entries, declares = {}, []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if line.startswith("!"):
                match = _ENTRY.match(line)
                if match:
                    fields = {key: pattern.search(match[3]) for key, pattern in _FIELD.items()}
                    entries[match[1]] = (
                        match[2],
                        {key: found[1] for key, found in fields.items() if found},
                    )
                continue
            if "dbg" not in line:
                continue
            for pattern in _DECLARES:
                if found := pattern.search(line):
                    declares.append((found[1], found[2], found[3]))
                    break
    result = {}
    for pointer, variable, location in declares:
        kind, fields = entries.get(variable, (None, {}))
        if kind != "DILocalVariable" or "name" not in fields:
            continue
        function = _function(entries, fields.get("scope"))
        hashed = _HASH.search(function or "")
        if not hashed:
            continue
        where = entries.get(location, (None, {}))
        real = not _SPILL.search(pointer) and where[0] == "DILocation" and "inlined" not in where[1]
        key = (hashed[1], fields["name"])
        result[key] = result.get(key, True) and real
    return result


def _function(entries, scope):
    for _ in range(256):
        kind, fields = entries.get(scope, (None, {}))
        if kind == "DISubprogram":
            return fields.get("linkage")
        if kind not in ("DILexicalBlock", "DILexicalBlockFile"):
            return None
        scope = fields.get("scope")
    return None
