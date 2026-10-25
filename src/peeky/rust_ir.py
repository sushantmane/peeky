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
import unicodedata

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
# A local's IR name: plain (`%x.dbg.spill`), or quoted where it holds another character
# (`%"gr\C3\B6\C3\9Fe"` for `größe`, its bytes in hex; #191); ir_string reads the
# quoted one, or refuses it.
_POINTER = r'%([-\w.$]+|"(?:[^"\\]|\\.)*")'
# (pointer, variable, expression, location); the location is the record's last operand.
_DECLARES = (
    re.compile(rf"#dbg_declare\(ptr {_POINTER}, (![0-9]+), (.*), (![0-9]+)\)\s*$"),
    re.compile(
        rf"@llvm\.dbg\.declare\(metadata ptr {_POINTER}, metadata (![0-9]+), "
        r"metadata (.*)\), !dbg (![0-9]+)"
    ),
)
# A declaration's expression that says the variable is the slot itself, or a piece of it:
# `!DIExpression()`, `!DIExpression(DW_OP_LLVM_fragment, 0, 32)`. Anything else (a closure's
# capture, `DW_OP_deref, DW_OP_deref`: through its environment's pointer) is somewhere else.
_PLAIN_EXPRESSION = re.compile(r"!DIExpression\((?:DW_OP_LLVM_fragment, [0-9]+, [0-9]+)?\)")
_SPILL = re.compile(r"\.dbg\.spill")
# A name field, read or not.
_NAMED = re.compile(r"(?:\(|, )name: ")
# What LLVM IR writes as it is in a string: printable ASCII but `"` and `\\`.
_IR_PLAIN = re.compile(r"[ !#-\[\]-~]*")
_IR_ESCAPE = re.compile(r"\\([0-9A-Fa-f]{2})")


def ir_string(text):
    """An LLVM IR string's text (without its quotes), or None where it is none LLVM
    writes: LLVM writes a quote, a backslash and every unprintable byte (any non-ASCII
    one) as `\\` and two hex digits (`src/x\\E2\\80\\A8y.rs`, a variable
    `gr\\C3\\B6\\C3\\9Fe`; #179), so any other `\\` (`\\q`), anything else unescaped, and
    bytes that are not UTF-8 (`\\FF`) are refused, never read as another name (#188
    review)."""
    found, at = bytearray(), 0
    while True:
        plain = _IR_PLAIN.match(text, at)
        found += plain[0].encode("ascii")
        if (at := plain.end()) == len(text):
            break
        if (escape := _IR_ESCAPE.match(text, at)) is None:
            return None
        found.append(int(escape[1], 16))
        at = escape.end()
    try:
        return found.decode("utf-8")
    except UnicodeDecodeError:
        return None


_HASH = re.compile(r"17(h[0-9a-f]{16})E$")


def memory_locals(path):
    """{(function hash, local name): True only if every declaration is a real slot}."""
    return ir_locals(path)[0]


def ir_locals(path, declarations=None):
    """(memory locals, reference parameters), read in one pass over the IR.

    Memory locals: {(function hash, local name): True only if every declaration is a
    real slot: the variable itself, or a piece of it, in an alloca of its own}. Reference parameters: {(function hash, name): {"type": declared type,
    "line": last line before the body}} for parameters whose only declaration is a
    non-inlined slot of their own (`<name>` or `<name>.dbg.spill`), and the only
    variable so named in the function's debug information, declared or not: a shadow
    never stored (`let é: &Vec<i64>;`) has no declaration (#194 review).

    ``declarations``, a dict if given, receives what the memory proof covers, per name:
    {(function hash, local name): [(line declared, "slot" | "copy" | "inlined" |
    "unknown")]}, one per declaration (a piece of a variable is one): a real slot, a
    `.dbg.spill` copy of a value kept in a register, a declaration in a copy inlined into
    another function (#138: why a name is not proven in memory), or one that says
    nothing Peeky reads of where the variable is (reached through a pointer, as a
    closure's capture is; no slot it can name; a location it doesn't recognize).

    The function hash is the `h<16 hex>` of the legacy-mangled linkage name, which
    LLDB also ends Rust frame names with. Locals without any declaration, or whose
    function cannot be identified, are absent: absent means "not provably in memory"
    and "no proof", respectively.
    """
    if path.stat().st_size > MAX_IR_BYTES:
        return {}, {}
    entries, declares = {}, []
    # Bytes not UTF-8 stay apart (surrogates ir_string refuses), never one `\ufffd`.
    with path.open(encoding="utf-8", errors="surrogateescape", newline="") as stream:
        for line in stream:
            if line.startswith("!"):
                match = _ENTRY.match(line)
                if match:
                    fields = {key: pattern.search(match[3]) for key, pattern in _FIELD.items()}
                    values = {
                        key: ir_string(found[1]) if key in ("name", "linkage") else found[1]
                        for key, found in fields.items()
                        if found
                    }
                    # A name LLVM does not write proves nothing (#188 review), and
                    # could be any variable's (`refused`).
                    kept = {key: value for key, value in values.items() if value is not None}
                    if len(kept) != len(values) or ("name" not in kept and _NAMED.search(line)):
                        kept["refused"] = True
                    entries[match[1]] = (match[2], kept)
                continue
            if "dbg" not in line:
                continue
            for pattern in _DECLARES:
                if found := pattern.search(line):
                    pointer = found[1]
                    if pointer.startswith('"'):
                        # As its source names it; a name LLVM does not write, none,
                        # which still declares the variable, in no slot it can name.
                        pointer = ir_string(pointer[1:-1])
                    declares.append((pointer, found[2], found[3].strip(), found[4]))
                    break
    # How many variables of each name each function's debug information holds, per
    # function hash: one whose name cannot be read could be any (name None), one whose
    # function cannot be told any function's ("?"). An unnamed one (`_: i32`) is none.
    # Names count as one whichever Unicode form spells them (rustc writes NFC).
    named = {}
    for kind, fields in entries.values():
        if kind == "DILocalVariable" and ("name" in fields or "refused" in fields):
            function = _function(entries, fields.get("scope"))
            hashed = _HASH.search((function or {}).get("linkage", ""))
            if hashed or function is None:
                key = (hashed[1] if hashed else "?", _form(fields.get("name")))
                named[key] = named.get(key, 0) + 1
    memory, parameters = {}, {}
    for pointer, variable, expression, location in declares:
        kind, fields = entries.get(variable, (None, {}))
        if kind != "DILocalVariable" or "name" not in fields:
            continue
        function = _function(entries, fields.get("scope"))
        hashed = _HASH.search((function or {}).get("linkage", ""))
        if not hashed:
            continue
        where = entries.get(location, (None, {}))
        located = where[0] == "DILocation" and "inlined" not in where[1]
        plain = bool(_PLAIN_EXPRESSION.fullmatch(expression))
        if pointer is None or not plain:
            kind = "unknown"
        elif where[0] == "DILocation" and "inlined" in where[1]:
            kind = "inlined"
        elif not located:
            kind = "unknown"
        else:
            kind = "copy" if _SPILL.search(pointer) else "slot"
        key = (hashed[1], fields["name"])
        memory[key] = memory.get(key, True) and kind == "slot"
        if declarations is not None:
            line = int(fields["line"]) if fields.get("line", "").isdigit() else None
            declarations.setdefault(key, []).append((line, kind))
        declared = entries.get(fields.get("type"), (None, {}))[1].get("name")
        lines = [function.get("line"), function.get("scope_line"), fields.get("line")]
        proven = (
            pointer in (fields["name"], fields["name"] + ".dbg.spill")
            and named.get((hashed[1], _form(fields["name"]))) == 1
            and not {(hashed[1], None), ("?", _form(fields["name"])), ("?", None)} & named.keys()
            and located
            and plain
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


def _form(name):
    return None if name is None else unicodedata.normalize("NFC", name)


def _function(entries, scope):
    for _ in range(256):
        kind, fields = entries.get(scope, (None, {}))
        if kind == "DISubprogram":
            return fields
        if kind not in ("DILexicalBlock", "DILexicalBlockFile"):
            return None
        scope = fields.get("scope")
    return None
