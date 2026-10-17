"""Which owned Rust locals are initialized at a stop, from the compiler's own MIR (#53).

A dropped, moved or not-yet-assigned `Vec`/`String` local keeps a header that passes
every sanity check while its pointer reaches freed or reused memory, so its contents
are read only with a proof. rustc's MIR has the facts: every move (`move _n`), drop
(`drop(_n)`), assignment and storage marker is explicit, which LLVM IR cannot show
(it passes `ptr %x` for a move and a borrow alike).

Peeky builds the debugged binary crate with `-Zmir-opt-level=0` (through a rustc
wrapper, for that crate only). No MIR pass then turns a move into a copy, the dumped
`PreCodegen` MIR is exactly what codegen used, and its basic blocks keep their names in
the LLVM IR (`bb0` is `start`). The IR's debug locations say which blocks hold code of
the stop line, with macro-expanded code already attributed to the line that invoked
the macro.

The proof for a local at a stop: in every MIR block holding code of the stop line, the
local is definitely initialized on entry (a must dataflow over the whole CFG, unwind
edges included), and nothing in the block moves, drops or ends it. Anything not
understood gives no proof: an unparsed statement or terminator, a coroutine, a debug
binding through a projection, a name bound twice, an IR block of unknown origin.
Unsafe code that duplicates ownership (`ptr::read`) is outside the proof.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_MIR_BYTES = 64 * 1024 * 1024
MAX_ITERATIONS = 10_000

_HEADER = re.compile(r"^fn (.+?)\((.*)\) -> .* \{$")
_ARGUMENT = re.compile(r"(?:^|, )_([0-9]+): ")
_LOCAL = re.compile(
    r"^ +let (?:mut )?_([0-9]+): .*?; +// (?:return place )?in scope [0-9]+ at "
    r"([^:]+):([0-9]+):[0-9]+"
)
_DEBUG = re.compile(r"^ +debug (.+?) => (.+?); +// in scope [0-9]+ at ([^:]+):([0-9]+):[0-9]+")
_BLOCK = re.compile(r"^    bb([0-9]+)(?: \(cleanup\))?: \{$")
# Greedy: the span comment is the line's last; a string constant may contain one too.
_STATEMENT = re.compile(r"^        (.+;) +// scope [0-9]+ at (.+)$")
_SPAN = re.compile(r"^([^:]+):([0-9]+):[0-9]+")
_WHOLE_LOCAL = re.compile(r"^_([0-9]+)$")
_MOVE = re.compile(r"\bmove\s+[(*]*_([0-9]+)\b")
_ASSIGN = re.compile(r"^_([0-9]+) = ")
_STORAGE = re.compile(r"^(?:StorageLive|StorageDead|Deinit)\(_([0-9]+)\);$")
_DROP = re.compile(r"^drop\([(*]*_([0-9]+)\b")
_ANY_LOCAL = re.compile(r"\b_([0-9]+)\b")
_SUCCESSOR = re.compile(r"\bbb([0-9]+)\b")
_RETURN_EDGE = re.compile(r"\breturn: bb([0-9]+)\b")
_EXITS = re.compile(r"^(?:return|unreachable|resume|abort|terminate)\b")
_UNSUPPORTED = re.compile(r"^(?:yield|coroutine_drop|falseEdge|falseUnwind|tailcall|asm!)\b")


@dataclass
class Block:
    statements: list = field(default_factory=list)  # (text, file, line)
    terminator: str = ""
    successors: tuple = ()
    returns: int | None = None  # a call's return edge, the only edge that sets its result


@dataclass
class Body:
    path: str
    arguments: frozenset
    file: str | None
    blocks: dict  # {number: Block}
    bindings: dict  # {name: [(local or None, file, line)]}
    supported: bool = True
    _entry: dict | None = None

    @property
    def name(self):
        return _segments(self.path)[-1]

    @property
    def parent(self):
        parts = _segments(self.path)
        return parts[-2] if len(parts) > 1 else None

    def call_lines(self):
        """Lines of call terminators in the body's own file: each is a call instruction."""
        return {
            block.statements[-1][2]
            for block in self.blocks.values()
            if block.returns is not None and block.statements[-1][1] == self.file
        }

    def local(self, name, declared=None):
        """The MIR local an lldb-dap variable shows: `name`, or `name @ file:line` when
        several are in scope. None without a single whole-local binding."""
        bindings = self.bindings.get(name, [])
        if declared is not None:
            file, line = declared
            bindings = [b for b in bindings if Path(b[1]).name == file and b[2] == line]
        if len(bindings) != 1 or bindings[0][0] is None:
            return None
        return bindings[0][0]

    def initialized_throughout(self, local, blocks):
        """True if ``local`` is initialized on entry to each block and none of them
        moves, drops or ends it."""
        if not self.supported or not blocks:
            return False
        entry = self._dataflow()
        if entry is None:
            return False
        bit = 1 << local
        for number in blocks:
            state = entry.get(number)
            if state is None or not state & bit:
                return False
            block = self.blocks[number]
            if any(local in _kills(text) for text, _, _ in block.statements):
                return False
        return True

    def _dataflow(self):
        """{block: initialized-locals bitset on entry} for blocks reachable from bb0, by
        a must analysis from "everything" down to its greatest fixpoint."""
        if self._entry is not None:
            return self._entry or None
        reachable, pending = set(), [0]
        while pending:
            number = pending.pop()
            if number in reachable:
                continue
            if number not in self.blocks:
                self._entry = {}
                return None
            reachable.add(number)
            pending.extend(self.blocks[number].successors)
        top = (1 << (1 + max(map(_max_local, self.blocks.values()), default=0))) - 1
        entry = {number: top for number in reachable}
        entry[0] = _mask(self.arguments)
        work = sorted(reachable, reverse=True)  # every block is processed at least once
        steps = 0
        while work:
            steps += 1
            if steps > MAX_ITERATIONS:
                self._entry = {}
                return None
            number = work.pop()
            block = self.blocks[number]
            state = entry[number]
            for text, _, _ in block.statements[:-1]:
                state = _transfer(text, state)
            # The terminator moves and drops on every edge; a call sets its result only
            # on its return edge.
            state &= ~_mask(_kills(block.terminator))
            for target in block.successors:
                out = state
                if target == block.returns:
                    out |= 1 << int(_ASSIGN.match(block.terminator)[1])
                merged = entry[target] & out
                if merged != entry[target]:
                    entry[target] = merged
                    if target not in work:
                        work.append(target)
        self._entry = entry
        return entry


def _segments(path):
    """`<impl at src/a.rs:2:1: 2:11>::push` -> ['<impl at ...>', 'push']."""
    parts, depth, current = [], 0, ""
    for index, char in enumerate(path):
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        if depth == 0 and path.startswith("::", index):
            parts.append(current)
            current = ""
            continue
        if depth == 0 and current == "" and char == ":" and path[index - 1 : index] == ":":
            continue
        current += char
    parts.append(current)
    return [part for part in parts if part]


def _mask(locals_):
    return sum(1 << local for local in locals_)


def _max_local(block):
    found = [int(n) for text, _, _ in block.statements for n in _ANY_LOCAL.findall(text)]
    return max(found, default=0)


def _kills(text):
    """Locals a statement or terminator moves out of, drops or ends."""
    killed = {int(n) for n in _MOVE.findall(text)}
    if storage := _STORAGE.match(text):
        killed.add(int(storage[1]))
    if drop := _DROP.match(text):
        killed.add(int(drop[1]))
    return killed


def _transfer(text, state):
    state &= ~_mask(_kills(text))
    if assigned := _ASSIGN.match(text):
        state |= 1 << int(assigned[1])
    return state


def parse_body(text):
    """One `-Zdump-mir` body, or None when the file holds no function body."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("fn ")), None)
    if start is None or not (header := _HEADER.match(lines[start])):
        return None
    arguments = frozenset(int(n) for n in _ARGUMENT.findall(header[2]))
    body = Body(header[1], arguments, None, {}, {})
    block = None
    for line in lines[start + 1 :]:
        if line == "}":
            break
        if line.lstrip().startswith("//") or not line.strip():
            continue
        if local := _LOCAL.match(line):
            if local[1] == "0":
                body.file = local[2]
            continue
        if debug := _DEBUG.match(line):
            whole = _WHOLE_LOCAL.match(debug[2])
            body.bindings.setdefault(debug[1], []).append(
                (int(whole[1]) if whole else None, debug[3], int(debug[4]))
            )
            continue
        if opened := _BLOCK.match(line):
            block = body.blocks.setdefault(int(opened[1]), Block())
            continue
        if line == "    }":
            if block is not None and not _finish(block):
                body.supported = False
            block = None
            continue
        if block is None:
            # Locals, scopes and their closing braces come before the blocks.
            if not re.match(r"^ +(?:let |scope [0-9]+ \{$|\}$)", line):
                body.supported = False
            continue
        statement = _STATEMENT.match(line)
        if not statement:
            body.supported = False
            continue
        span = _SPAN.match(statement[2])
        block.statements.append(
            (statement[1], span[1] if span else None, int(span[2]) if span else None)
        )
    if block is not None or not body.blocks or 0 not in body.blocks or body.file is None:
        body.supported = False
    return body


def _finish(block):
    """Classify the block's last statement as its terminator; False if not understood."""
    if not block.statements:
        return False
    text = block.statements[-1][0]
    block.terminator = text
    if _UNSUPPORTED.match(text):
        return False
    if _EXITS.match(text):
        block.successors = ()
        return True
    if "->" not in text:
        return False
    edges = text.rpartition("->")[2]
    block.successors = tuple(int(n) for n in _SUCCESSOR.findall(edges))
    if _ASSIGN.match(text):
        returned = _RETURN_EDGE.search(edges)
        block.returns = int(returned[1]) if returned else None
    return True


def load_bodies(directory, crate):
    """Every `PreCodegen` body rustc dumped for ``crate``; empty when over budget."""
    directory = Path(directory)
    files = sorted(directory.glob(f"{crate}.*.PreCodegen.after.mir"))
    if sum(path.stat().st_size for path in files) > MAX_MIR_BYTES:
        return []
    bodies = []
    for path in files:
        body = parse_body(path.read_text(encoding="utf-8", errors="replace"))
        if body is not None:
            bodies.append(body)
    return bodies


_DEFINE = re.compile(r'^define .*?@("?)([^"(\s]+)\1\(.*!dbg (![0-9]+) \{$')
_LEGACY_HASH = re.compile(r"17(h[0-9a-f]{16})E$")
_LABEL = re.compile(r"^([-$._A-Za-z0-9]+):(?:\s+; preds = (.*))?$")
_DBG = re.compile(r"!dbg (![0-9]+)")
_META = re.compile(
    r"^(![0-9]+) = (?:distinct )?!(DILocation|DISubprogram|DILexicalBlock|DILexicalBlockFile"
    r"|DIFile|DINamespace|DICompositeType)\((.*)\)\s*$"
)
_METAFIELDS = {
    key: re.compile(rf'(?:^|, ){key}: ("(?:[^"\\]|\\.)*"|![0-9]+|[0-9]+)')
    for key in ("name", "linkageName", "scope", "file", "line", "inlinedAt", "filename")
}


@dataclass
class IrFunction:
    name: str
    parent: str | None
    file: str
    blocks: dict  # {label: {line, ...}} for code of this function (not inlined)
    preds: dict  # {label: [label, ...]}

    def lines(self):
        return set().union(*self.blocks.values()) if self.blocks else set()

    def mir_blocks(self, line):
        """MIR blocks holding code of ``line``; None if a block's origin is unknown."""
        found, seen = set(), set()
        pending = [label for label, lines in self.blocks.items() if line in lines]
        if not pending:
            return None
        while pending:
            label = pending.pop()
            if label in seen:
                continue
            seen.add(label)
            if label == "start":
                found.add(0)
            elif match := re.fullmatch(r"bb([0-9]+)", label):
                found.add(int(match[1]))
            elif self.preds.get(label):
                # A codegen helper (panic, landing pad): it runs at the end of the MIR
                # block that branches to it.
                pending.extend(self.preds[label])
            else:
                return None
        return found


def _unquote(value):
    return value[1:-1] if value and value.startswith('"') else value


def ir_functions(path, files):
    """{legacy hash: IrFunction} for functions defined in ``files`` (relative paths)."""
    path = Path(path)
    if path.stat().st_size > 256 * 1024 * 1024:
        return {}
    raw, meta, current = [], {}, None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if line.startswith("!"):
                if found := _META.match(line):
                    meta[found[1]] = (
                        found[2],
                        {
                            key: _unquote(m[1])
                            for key, pattern in _METAFIELDS.items()
                            if (m := pattern.search(found[3]))
                        },
                    )
                continue
            if current is None:
                if line.startswith("define ") and (found := _DEFINE.match(line.rstrip())):
                    hashed = _LEGACY_HASH.search(found[2])
                    current = (hashed[1] if hashed else None, found[3], {}, {}, ["start"])
                continue
            if line.startswith("}"):
                raw.append(current)
                current = None
                continue
            if label := _LABEL.match(line.rstrip()):
                current[4][0] = label[1]
                current[2].setdefault(label[1], [])
                if label[2]:
                    current[3][label[1]] = [p.strip().lstrip("%") for p in label[2].split(",")]
                continue
            if "@llvm.dbg." in line or line.lstrip().startswith("#dbg_"):
                continue
            if dbg := _DBG.search(line):
                current[2].setdefault(current[4][0], []).append(dbg[1])
    wanted = set(files)
    functions = {}
    for hashed, subprogram, blocks, preds, _ in raw:
        kind, fields = meta.get(subprogram, (None, {}))
        if hashed is None or kind != "DISubprogram":
            continue
        file = _file(meta, subprogram)
        if file not in wanted:
            continue
        lines = {}
        for label, refs in blocks.items():
            lines[label] = set()
            for ref in refs:
                kind, location = meta.get(ref, (None, {}))
                if kind != "DILocation" or "inlinedAt" in location:
                    continue
                if _file(meta, location.get("scope")) != file or not location.get("line"):
                    continue
                if (number := int(location["line"])) > 0:
                    lines[label].add(number)
        parent = meta.get(fields.get("scope"), (None, {}))[1].get("name")
        functions[hashed] = IrFunction(
            re.sub(r"<.*>$", "", fields.get("name", "")), parent, file, lines, preds
        )
    return functions


def _file(meta, scope):
    for _ in range(256):
        kind, fields = meta.get(scope, (None, {}))
        if kind is None:
            return None
        if kind in ("DISubprogram", "DILexicalBlock", "DILexicalBlockFile") and "file" in fields:
            return meta.get(fields["file"], (None, {}))[1].get("filename")
        scope = fields.get("scope")
    return None


class OwnedLocals:
    """Proofs that an owned local is initialized at a stop (see the module docstring)."""

    def __init__(self, bodies, functions):
        self.bodies = list(bodies)
        self.functions = dict(functions)
        self._matched = {}

    def body(self, hashed):
        """The one MIR body the IR function ``hashed`` was generated from, else None."""
        if hashed in self._matched:
            return self._matched[hashed]
        function = self.functions.get(hashed)
        found = None
        if function is not None:
            lines = function.lines()
            numbers = {
                int(m[1]) for label in function.blocks if (m := re.fullmatch(r"bb([0-9]+)", label))
            }
            candidates = [
                body
                for body in self.bodies
                if body.file == function.file
                and body.name == function.name
                and (
                    body.parent is None
                    or body.parent.startswith("<")
                    or body.parent == function.parent
                )
                and body.call_lines() <= lines
                and numbers <= set(body.blocks)
            ]
            found = candidates[0] if len(candidates) == 1 else None
        self._matched[hashed] = found
        return found

    def proven(self, hashed, variable_name, line):
        """True only if the frame's local ``variable_name`` (an lldb-dap name: `v`, or
        `v @ main.rs:8` when shadowed) is initialized at ``line``."""
        body = self.body(hashed)
        function = self.functions.get(hashed)
        if body is None or function is None or type(line) is not int:
            return False
        name, at, declared = str(variable_name).partition(" @ ")
        where = None
        if at:
            file, _, number = declared.rpartition(":")
            if not number.isdigit():
                return False
            where = (file, int(number))
        local = body.local(name, where)
        blocks = function.mir_blocks(line)
        return (
            local is not None and blocks is not None and body.initialized_throughout(local, blocks)
        )
