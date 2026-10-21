"""Which Rust locals live in memory, read from the compiler's own LLVM IR.

At opt-level 0 rustc still keeps a local that is assigned once and never borrowed
in an SSA register. For the debugger it stores a copy in a stack slot named
`<name>.dbg.spill`; LLDB shows and writes that copy, but the program never reads it,
so changing it would report a successful intervention that changed nothing.
Locals the program really keeps in memory are declared on an ordinary alloca.
`#[inline(always)]` functions are inlined even at opt-level 0: their declarations
carry an `inlinedAt` location and renamed slots (`x.dbg.spill.i`), and are never
treated as provably in memory.

The same declarations prove when a reference parameter's contents can be read.
rustc stores every parameter (in its own slot or a `<name>.dbg.spill` copy) in the
entry block, before any statement. A reference parameter's lifetime outlives the
function body, and the borrow checker rejects assigning it anything shorter-lived
(even through `&mut` or in a closure), so past its declaration lines it points at
live, initialized memory. Locals have no such proof here: a moved, dropped or
not-yet-initialized local can still hold a stale pointer. Owned locals get theirs from
the compiler's MIR instead (rust_mir).
"""

import re

MAX_IR_BYTES = 256 * 1024 * 1024
_ENTRY = re.compile(
    r"^(![0-9]+) = (?:distinct )?!(DILocalVariable|DISubprogram|DILexicalBlock"
    r"|DILexicalBlockFile|DILocation|DICompositeType|DIDerivedType)\((.*)\)\s*$"
)
_FIELD = {
    "name": re.compile(r'(?:^|, )name: "((?:[^"\\]|\\.)*)"'),
    "scope": re.compile(r"(?:^|, )scope: (![0-9]+)"),
    "linkage": re.compile(r'(?:^|, )linkageName: "([^"]*)"'),
    "inlined": re.compile(r"(?:^|, )inlinedAt: (![0-9]+)"),
    "arg": re.compile(r"(?:^|, )arg: ([0-9]+)"),
    "line": re.compile(r"(?:^|, )line: ([0-9]+)"),
    "scope_line": re.compile(r"(?:^|, )scopeLine: ([0-9]+)"),
    "type": re.compile(r"(?:^|, )type: (![0-9]+)"),
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
_IR_ESCAPE = re.compile(rb"\\([0-9A-Fa-f]{2})")


def ir_string(text):
    """An LLVM IR string's text: LLVM writes a quote, a backslash and every unprintable
    byte (any non-ASCII one) as `\\` and two hex digits (`src/x\\E2\\80\\A8y.rs`, a
    variable `gr\\C3\\B6\\C3\\9Fe`; #179)."""
    raw = _IR_ESCAPE.sub(lambda found: bytes([int(found[1], 16)]), text.encode("utf-8"))
    return raw.decode("utf-8", errors="replace")


_HASH = re.compile(r"17(h[0-9a-f]{16})E$")


def memory_locals(path):
    """{(function hash, local name): True only if every declaration is a real slot}."""
    return ir_locals(path)[0]


def ir_locals(path, declarations=None):
    """(memory locals, reference parameters), read in one pass over the IR.

    Memory locals: {(function hash, local name): True only if every declaration is a
    real slot}. Reference parameters: {(function hash, name): {"type": declared type,
    "line": last line before the body}} for parameters whose only declaration is a
    non-inlined slot of their own (`<name>` or `<name>.dbg.spill`).

    ``declarations``, a dict if given, receives what the memory proof covers, per name:
    {(function hash, local name): [(line declared, "slot" | "copy" | "inlined")]}: a
    real slot, a `.dbg.spill` copy of a value kept in a register, or a declaration in a
    copy inlined into another function (#138: why a name is not proven in memory).

    The function hash is the `h<16 hex>` of the legacy-mangled linkage name, which
    LLDB also ends Rust frame names with. Locals without any declaration, or whose
    function cannot be identified, are absent: absent means "not provably in memory"
    and "no proof", respectively.
    """
    if path.stat().st_size > MAX_IR_BYTES:
        return {}, {}
    entries, declares = {}, []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if line.startswith("!"):
                match = _ENTRY.match(line)
                if match:
                    fields = {key: pattern.search(match[3]) for key, pattern in _FIELD.items()}
                    entries[match[1]] = (
                        match[2],
                        {
                            key: ir_string(found[1]) if key in ("name", "linkage") else found[1]
                            for key, found in fields.items()
                            if found
                        },
                    )
                continue
            if "dbg" not in line:
                continue
            for pattern in _DECLARES:
                if found := pattern.search(line):
                    declares.append((found[1], found[2], found[3]))
                    break
    memory, parameters = {}, {}
    for pointer, variable, location in declares:
        kind, fields = entries.get(variable, (None, {}))
        if kind != "DILocalVariable" or "name" not in fields:
            continue
        function = _function(entries, fields.get("scope"))
        hashed = _HASH.search((function or {}).get("linkage", ""))
        if not hashed:
            continue
        where = entries.get(location, (None, {}))
        located = where[0] == "DILocation" and "inlined" not in where[1]
        real = not _SPILL.search(pointer) and located
        key = (hashed[1], fields["name"])
        memory[key] = memory.get(key, True) and real
        if declarations is not None:
            kind = "slot" if real else "copy" if located else "inlined"
            line = int(fields["line"]) if fields.get("line", "").isdigit() else None
            declarations.setdefault(key, []).append((line, kind))
        declared = entries.get(fields.get("type"), (None, {}))[1].get("name")
        lines = [function.get("line"), function.get("scope_line"), fields.get("line")]
        proven = (
            pointer in (fields["name"], fields["name"] + ".dbg.spill")
            and located
            and "arg" in fields
            and declared
            and all(lines)
        )
        # A second declaration of the name (shadowing, an inlined copy) proves nothing.
        parameters[key] = (
            {"type": declared, "line": max(map(int, lines))}
            if proven and key not in parameters
            else None
        )
    return memory, {key: proof for key, proof in parameters.items() if proof}


def _function(entries, scope):
    for _ in range(256):
        kind, fields = entries.get(scope, (None, {}))
        if kind == "DISubprogram":
            return fields
        if kind not in ("DILexicalBlock", "DILexicalBlockFile"):
            return None
        scope = fields.get("scope")
    return None
