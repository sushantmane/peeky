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

The proof for a local at a stop: in every MIR block holding code of the stop line
(including blocks codegen merged into it), the local is initialized at every place that
code can be. That is, before each code-emitting statement spanning the line, or spanning
another file (external macro code takes the invoking line), following moves, drops and
assignments statement by statement from the block's entry state, which a must dataflow
over the whole CFG (unwind edges included) gives. A block whose code of the line no
statement explains (other than a checked caller's result store at its start) must keep
the local throughout. Anything not understood gives no proof: an unparsed statement or
terminator, a coroutine, a debug binding through a projection, a name bound twice, a
body that cannot be identified positively, an IR block of unknown origin, a stop in
another file. Unsafe code that duplicates ownership (`ptr::read`) is outside the proof.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_MIR_BYTES = 64 * 1024 * 1024
MAX_ITERATIONS = 10_000

_HEADER = re.compile(r"^fn (.+?)\((.*)\) -> .* \{$")
_ARGUMENT = re.compile(r"(?:^|, )_([0-9]+): ")
_LOCAL = re.compile(
    r"^ +let (?:mut )?_([0-9]+): (.*?); +// (?:return place )?in scope [0-9]+ at "
    r"([^:]+):([0-9]+):[0-9]+"
)
_DEBUG = re.compile(r"^ +debug (.+?) => (.+?); +// in scope [0-9]+ at ([^:]+):([0-9]+):[0-9]+")
_BLOCK = re.compile(r"^    bb([0-9]+)(?: \(cleanup\))?: \{$")
# Greedy: the span comment is the line's last; a string constant may contain one too.
_STATEMENT = re.compile(r"^        (.+;) +// scope [0-9]+ at (.+)$")
_SPAN = re.compile(r"^([^:]+):([0-9]+):[0-9]+")
_WHOLE_LOCAL = re.compile(r"^_([0-9]+)$")
# A binding through a projection names its type: `(_1.0: std::boxed::Box<Pt>)`.
_PROJECTED = re.compile(r"\(.+: ((?:[^()]|\([^()]*\))+)\)")
_MOVE = re.compile(r"\bmove\s+[(*]*_([0-9]+)\b")
_ASSIGN = re.compile(r"^_([0-9]+) = ")
_STORAGE = re.compile(r"^(?:StorageLive|StorageDead|Deinit)\(_([0-9]+)\);$")
_DROP = re.compile(r"^drop\([(*]*_([0-9]+)\b")
_ANY_LOCAL = re.compile(r"\b_([0-9]+)\b")
_SUCCESSOR = re.compile(r"\bbb([0-9]+)\b")
_RETURN_EDGE = re.compile(r"\breturn: bb([0-9]+)\b")
_EXITS = re.compile(r"^(?:return|unreachable|resume|abort|terminate)\b")
_NO_CODE = re.compile(r"^(?:StorageLive|StorageDead|Deinit)\(_[0-9]+\);$|^nop;$")
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
    # {name: [(local or None, file, line, type of a projection or None)]}
    bindings: dict
    supported: bool = True
    # Lines of every span in the body's own file (its own code, and the definitions of
    # same-file macros it expands).
    lines: frozenset = frozenset()
    types: dict = field(default_factory=dict)  # {local: its type as MIR prints it}
    _entry: dict | None = None

    @property
    def name(self):
        return _segments(self.path)[-1]

    @property
    def parent(self):
        parts = _segments(self.path)
        return parts[-2] if len(parts) > 1 else None

    def predecessors(self):
        found = {}
        for number, block in self.blocks.items():
            for target in block.successors:
                found.setdefault(target, set()).add(number)
        return found

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

    def initialized_at(
        self, local, blocks, line, file, declared=None, shared=None, *, foreign=True
    ):
        """True if ``local`` is initialized wherever code of ``line`` in ``file`` can be
        in these blocks: a statement spanning that line, or spanning another file or
        none (external macro code takes the line that invoked it), and bb0's start when
        the line is the function's own (``declared``: its prologue). Moves, drops and
        assignments are followed statement by statement in between.

        Statements that emit no code at opt-level 0 (storage markers) are never
        positions. A call's result is stored at its target's start, at the call's line;
        then the call's own block has code of that line too and is checked before the
        call, which is stricter: built MIR passes locals to calls only through temps.

        ``foreign=False`` says the line cannot invoke a macro, so no code from another
        file is on it.
        ``shared`` maps a block to every block codegen emitted into the same IR block
        (``_merged``): the IR cannot say which of them a line's code came from, so a
        block without code of the line is passed over when another of them has some.
        """
        if not self.supported or not blocks:
            return False
        entry = self._dataflow()
        if entry is None:
            return False
        bit, predecessors = 1 << local, self.predecessors()

        def explained(number):
            """(prologue, statement positions, result store): this block's code of line."""
            block = self.blocks[number]
            positions = [
                index
                for index, (text, spanned, spanned_line) in enumerate(block.statements)
                if (spanned_line == line if spanned == file else spanned is None or foreign)
                and not _NO_CODE.match(text)
            ]
            stored = any(
                caller in blocks
                and self.blocks[caller].returns == number
                and (
                    self.blocks[caller].statements[-1][2] == line
                    if self.blocks[caller].statements[-1][1] == file
                    else self.blocks[caller].statements[-1][1] is None or foreign
                )
                for caller in predecessors.get(number, ())
            )
            return number == 0 and line == declared, positions, stored

        for number in blocks:
            state = entry.get(number)
            if state is None:
                return False
            block = self.blocks[number]
            starts_here, positions, stored = explained(number)
            if not starts_here and not positions:
                if not stored and any(
                    any(explained(other)) for other in (shared or {}).get(number, set()) - {number}
                ):
                    # The line's code in this IR block is another merged block's, and that
                    # block is checked itself.
                    continue
                if stored:
                    # Only a checked caller's result store, at this block's start.
                    if not state & bit:
                        return False
                    continue
                # The IR has code of this line here, yet no statement can have made it:
                # not understood, so the whole block must keep the local.
                if not state & bit or any(local in _kills(text) for text, _, _ in block.statements):
                    return False
                continue
            if starts_here and not state & bit:
                return False
            for index, (text, _, _) in enumerate(block.statements):
                if index in positions and not state & bit:
                    return False
                state = _transfer(text, state)
            # A stop at a call's or drop's return address (`step --out`) is still on
            # this line, after the terminator's moves and drops.
            last = len(block.statements) - 1
            if last in positions and (block.returns is not None or _DROP.match(block.terminator)):
                if not state & bit:
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


def _top_level(text):
    """``text`` split at the commas outside brackets (`_1: HashMap<K, V>, _2: u8`)."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char in "<([{":
            depth += 1
        elif char in ")]}" or char == ">" and text[index - 1 : index] != "-":
            # `->` in a function type closes nothing.
            depth -= 1
        if char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def parse_body(text):
    """One `-Zdump-mir` body, or None when the file holds no function body."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("fn ")), None)
    if start is None or not (header := _HEADER.match(lines[start])):
        return None
    arguments = frozenset(int(n) for n in _ARGUMENT.findall(header[2]))
    body = Body(header[1], arguments, None, {}, {})
    # Arguments are typed in the header only (`fn f(_1: &[u8], _2: usize)`).
    for argument in _top_level(header[2]):
        if found := re.fullmatch(r"_([0-9]+): (.+)", argument):
            body.types[int(found[1])] = found[2]
    block, spans = None, []
    for line in lines[start + 1 :]:
        if line == "}":
            break
        if line.lstrip().startswith("//") or not line.strip():
            continue
        if local := _LOCAL.match(line):
            if local[1] == "0":
                body.file = local[3]
            body.types[int(local[1])] = local[2]
            spans.append((local[3], int(local[4])))
            continue
        if debug := _DEBUG.match(line):
            whole = _WHOLE_LOCAL.match(debug[2])
            # A binding through a projection (a closure's capture, an async local) has no
            # whole local, but names its type: `(_1.0: std::boxed::Box<Pt>)`.
            typed = None if whole else _PROJECTED.fullmatch(debug[2])
            body.bindings.setdefault(debug[1], []).append(
                (
                    int(whole[1]) if whole else None,
                    debug[3],
                    int(debug[4]),
                    typed[1] if typed else None,
                )
            )
            spans.append((debug[3], int(debug[4])))
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
        if span:
            spans.append((span[1], int(span[2])))
    if block is not None or not body.blocks or 0 not in body.blocks or body.file is None:
        body.supported = False
    body.lines = frozenset(number for file, number in spans if file == body.file)
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
# A block codegen left unreachable (the dead arm of `if cfg!(debug_assertions)` in
# `debug_assert!`) is labeled `; No predecessors!`.
# LLVM quotes a label with characters such as `$` (an inlined `Box::new`'s exit block).
_LABEL = re.compile(r'^([-$._A-Za-z0-9]+|"[^"]+"):(?:\s+; (?:preds = (.*)|No predecessors!))?$')
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
    line: int | None = None  # the DISubprogram's: where the function is declared

    def labeled(self):
        """MIR blocks that have an IR block of their own (`start` is bb0)."""
        return {
            0 if label == "start" else int(label[2:])
            for label in self.blocks
            if label == "start" or re.fullmatch(r"bb[0-9]+", label)
        }

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
                name = label[1].strip('"')
                current[4][0] = name
                current[2].setdefault(name, [])
                if label[2]:
                    current[3][name] = [
                        p.strip().lstrip("%").strip('"') for p in label[2].split(",")
                    ]
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
            re.sub(r"<.*>$", "", fields.get("name", "")),
            parent,
            file,
            lines,
            preds,
            int(fields["line"]) if str(fields.get("line", "")).isdigit() else None,
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

    def __init__(self, bodies, functions, sources=(), *, precise=True, texts=None):
        self.bodies = list(bodies)
        # Statement-level positions rely on local macro code keeping its own lines in
        # debug info; `#[collapse_debuginfo]` changes that, and then whole blocks count.
        self.precise = precise
        self.functions = dict(functions)
        self.sources = tuple(sources)
        # {source: text}, to tell which lines can invoke a macro; absent means any can.
        self.texts = dict(texts or {})
        self._matched = {}
        # {(file, line): {body index}}: which bodies have a span on each line.
        self._owners = {}
        for index, body in enumerate(self.bodies):
            for number in body.lines:
                self._owners.setdefault((body.file, number), set()).add(index)

    def body(self, hashed):
        """The one MIR body the IR function ``hashed`` was generated from, else None."""
        if hashed in self._matched:
            return self._matched[hashed]
        function = self.functions.get(hashed)
        found = None
        if function is not None and function.lines():
            # Positive identification, never by elimination: exactly one body of this
            # name and file (and parent) has a span on the line the function is declared
            # on. Twins a local macro generates share that line (the `fn` is in the
            # macro), so they are refused rather than told apart by their other lines.
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
                and function.line in body.lines
            ]
            if len(candidates) == 1:
                chosen = next(i for i, b in enumerate(self.bodies) if b is candidates[0])
                # A line with this function's code that only other bodies have spans on
                # is theirs: the candidate is a twin whose `fn` line merely matches
                # (macro-generated, one with cleanup on that line, one without).
                foreign = any(
                    chosen not in owners
                    for number in function.lines()
                    if (owners := self._owners.get((function.file, number)))
                )
                if foreign or not function.labeled() <= set(candidates[0].blocks):
                    candidates = []
            found = candidates[0] if len(candidates) == 1 else None
            if found is not None and not _covered(found, function):
                found = None
        self._matched[hashed] = found
        return found

    @staticmethod
    def _local(body, variable_name):
        name, at, declared = str(variable_name).partition(" @ ")
        where = None
        if at:
            file, _, number = declared.rpartition(":")
            if not number.isdigit():
                return None
            where = (file, int(number))
        return body.local(name, where)

    def declared_type(self, hashed, variable_name):
        """The MIR type of the frame's local ``variable_name`` (`std::vec::Vec<usize>`),
        which LLDB's C-style names cannot tell apart (u64 and usize); else None."""
        body = self.body(hashed)
        local = self._local(body, variable_name) if body is not None else None
        return None if local is None else body.types.get(local)

    def binding_types(self, hashed, variable_name, path=None, line=None):
        """The MIR type of every binding lldb-dap could be showing as ``variable_name``
        (None for one whose type is not written out), in the frame's body. An async
        body is never matched for proofs; its types come from the bodies with a span on
        the stop line. Empty when no body binds the name."""
        name, at, declared = str(variable_name).partition(" @ ")
        body = self.body(hashed)
        shown = str(path or "").replace("\\", "/")
        bodies = (
            [body]
            if body is not None
            else [
                candidate
                for candidate in self.bodies
                if line in candidate.lines and shown.endswith("/" + str(candidate.file))
            ]
        )
        found = []
        for candidate in bodies:
            for local, file, number, projected in candidate.bindings.get(name, ()):
                if at and f"{Path(file).name}:{number}" != declared:
                    continue
                found.append(candidate.types.get(local) if local is not None else projected)
        return found

    def proven(self, hashed, variable_name, line, path):
        """True only if the frame's local ``variable_name`` (an lldb-dap name: `v`, or
        `v @ main.rs:8` when shadowed) is initialized at ``line`` of ``path``.

        The stop must be in the function's own file: a breakpoint in a macro defined in
        another file (or `include!`d code) stops in this frame at another file's line.
        """
        body = self.body(hashed)
        function = self.functions.get(hashed)
        if body is None or function is None or type(line) is not int:
            return False
        shown = str(path or "").replace("\\", "/")
        if [source for source in self.sources if shown.endswith("/" + source)] != [function.file]:
            return False
        local = self._local(body, variable_name)
        blocks = function.mir_blocks(line)
        if local is None or blocks is None:
            return False
        shared = _shared(body, function, blocks)
        blocks = set(shared)
        if not self.precise:
            return body.initialized_throughout(local, blocks)
        return body.initialized_at(
            local,
            blocks,
            line,
            function.file,
            function.line,
            shared,
            foreign=may_invoke_macro(self.texts.get(function.file), line),
        )


def _merge_target(body, function, number, predecessors):
    """The MIR block codegen emits inside ``number``'s IR block, if any.

    rustc appends a block to its predecessor's IR block (no label of its own) when the
    predecessor has that one successor and it has that one predecessor: after a
    `goto`, or a call that cannot unwind (an `extern "C"` function).
    """
    successors = body.blocks[number].successors
    if len(successors) != 1:
        return None
    target = successors[0]
    if target in function.labeled() or predecessors.get(target) != {number}:
        return None
    return target


def _merged(body, function, blocks):
    """``blocks`` plus every block codegen merged into them."""
    predecessors, found = body.predecessors(), set(blocks)
    for number in blocks:
        seen = {number}
        while (target := _merge_target(body, function, number, predecessors)) is not None:
            if target in seen:
                break
            seen.add(target)
            found.add(target)
            number = target
    return found


# A line that ends inside a path (an identifier, `::`, a raw identifier's `#`) before
# any line comment: a macro invocation's path may start here and reach its `!` later.
_PATH_END = re.compile(r"[A-Za-z0-9_#:]\s*(?://.*)?$")
# What may continue such a path: whitespace, line comments, `::` and ASCII identifiers.
_PATH_TAIL = re.compile(r"(?:\s+|//[^\n]*|::|[A-Za-z0-9_#])*")


def may_invoke_macro(text, line):
    """False only if source ``line`` cannot start a macro invocation.

    rustc collapses a macro from another crate onto the line where its invocation's
    path starts (#[collapse_debuginfo]), so code spanning another file can be on such a
    line only. Any `!`, block comment or non-ASCII character on the line counts; so does
    a line ending in a path that a later `!` may complete, or that continues past a
    block comment or a non-ASCII identifier. Unknown text means any line can.

    ``text`` must be split into lines as rustc does, on `\n` alone (never a lone `\r`).
    """
    lines = text.split("\n") if isinstance(text, str) else []
    if not 1 <= line <= len(lines):
        return True
    own = lines[line - 1]
    if "!" in own or "/*" in own or not own.isascii():
        return True
    if not _PATH_END.search(own):
        return False
    rest = "\n".join(lines[line:])
    following = rest[_PATH_TAIL.match(rest).end() :][:1]
    return following in ("!", "/") or not following.isascii()


def _shared(body, function, blocks):
    """{block: the blocks in its IR block} for ``blocks`` and every block codegen merged
    into them."""
    predecessors, shared = body.predecessors(), {}
    for number in blocks:
        chain = [number]
        while (target := _merge_target(body, function, chain[-1], predecessors)) is not None:
            if target in chain:
                break
            chain.append(target)
        for member in chain:
            shared.setdefault(member, set()).update(chain)
    return shared


def _covered(body, function):
    """Every reachable MIR block has its own IR block or was merged into one; any other
    layout is not understood, so nothing in the function is proven."""
    entry = body._dataflow()
    if entry is None:
        return False
    covered = _merged(body, function, function.labeled() & set(entry))
    return all(
        number in covered or body.blocks[number].terminator == "unreachable;" for number in entry
    )
