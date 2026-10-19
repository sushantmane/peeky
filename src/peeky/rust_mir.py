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
_DEBUG = re.compile(r"^ +debug (.+?) => (.+?); +// in scope ([0-9]+) at ([^:]+):([0-9]+):[0-9]+")
_BLOCK = re.compile(r"^    bb([0-9]+)(?: \(cleanup\))?: \{$")
# Greedy: the span comment is the line's last; a string constant may contain one too.
_STATEMENT = re.compile(r"^        (.+;) +// scope ([0-9]+) at (.+)$")
_SCOPE = re.compile(r"^ +scope ([0-9]+)(?: \(inlined .*\))? \{$")
_SPAN = re.compile(r"^([^:]+):([0-9]+):[0-9]+")
_SPAN_END = re.compile(r"^[^:]+:[0-9]+:[0-9]+: ([0-9]+):[0-9]+")
# Statements that emit no code: a block's `()` value spans the whole block, and must not
# put its scope on the closing `}` (`} else if b.x > 0`, where only the outer one is).
_SPAN_ONLY = re.compile(
    r"(?:StorageLive|StorageDead|FakeRead|PlaceMention|AscribeUserType|Retag|Coverage|nop)\b"
    r"|_[0-9]+ = const \(\);|goto "
)
_WHOLE_LOCAL = re.compile(r"^_([0-9]+)$")
_WHOLE_ASSIGNMENT = re.compile(r"^_([0-9]+) = (.+);$")
_PART_ASSIGNMENT = re.compile(r"^\(+\*?_([0-9]+)[.: )].*? = (.+);$")
# Statements that never use a local, though they name it.
_NOT_A_USE = re.compile(r"^(?:StorageLive|FakeRead|PlaceMention|AscribeUserType|Retag|Coverage)\b")
# A local's mention in a statement (`move _11`, `(*_43)`); never part of a number or
# a name (`0_usize`, `bar_2`).
_MENTIONED = re.compile(r"(?<![\w])_([0-9]+)\b")
_RAW_POINTER = re.compile(r"\*const |\*mut ")
_FN_POINTER = re.compile(r"(?:for<[^<>]*> )?(?:unsafe )?(?:extern \S+ )?fn\(")
# A type that borrows: a reference, a raw pointer, one holding a lifetime, or a closure
# or coroutine (whose captures may be references).
_BORROWING = re.compile(r"&|\*const |\*mut |'|\{closure@|\{coroutine@|\{async")


def _projected_type(text):
    """The type a binding through a projection names, `(_1.0: std::boxed::Box<Pt>)` ->
    `std::boxed::Box<Pt>`: split at the projection's own `: `, outside its parentheses
    (`(((*_36) as variant#3).0: T)`), since a type can hold `: ` itself
    (`{closure@src/main.rs:27:22: 27:35}`). None for anything else."""
    if text.startswith("(*(") and text.endswith("))"):
        # A capture by reference, dereferenced: `(*((*_1).0: &T))` binds a `T`.
        referent = _projected_type(text[2:-1])
        found = re.fullmatch(r"&(?:mut )?(.+)", referent or "")
        return found[1] if found else None
    if not (text.startswith("(") and text.endswith(")")):
        return None
    inner, depth = text[1:-1], 0
    for index, char in enumerate(inner):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and inner.startswith(": ", index):
            return inner[index + 2 :] or None
    return None


_MOVE = re.compile(r"\bmove\s+[(*]*_([0-9]+)\b")
_ASSIGN = re.compile(r"^_([0-9]+) = ")
_STORAGE = re.compile(r"^(?:StorageLive|StorageDead|Deinit)\(_([0-9]+)\);$")
_DROP = re.compile(r"^drop\([(*]*_([0-9]+)\b")
# Terminators that branch on a value at run time: an instruction at the line's address.
_BRANCHES = re.compile(r"^(?:assert\(!?(?:move|copy) |switchInt\((?:move|copy) )")
# A borrow of a local's own storage: no `*` before the local in the place.
_DIRECT_BORROW = re.compile(r"&(?:mut |raw const |raw mut |fake \w+ )?\(*_([0-9]+)\b")
_ZERO_SIZED = re.compile(r"^\(\)$|^\{closure@|^\{coroutine@|^fn\(.*\{|PhantomData<|; 0\]$")
_ANY_LOCAL = re.compile(r"\b_([0-9]+)\b")
_SUCCESSOR = re.compile(r"\bbb([0-9]+)\b")
_RETURN_EDGE = re.compile(r"\breturn: bb([0-9]+)\b")
_EXITS = re.compile(r"^(?:return|unreachable|resume|abort|terminate)\b")
_NO_CODE = re.compile(r"^(?:StorageLive|StorageDead|Deinit)\(_[0-9]+\);$|^nop;$")
_UNSUPPORTED = re.compile(r"^(?:yield|coroutine_drop|falseEdge|falseUnwind|tailcall|asm!)\b")


@dataclass
class Block:
    statements: list = field(default_factory=list)  # (text, file, line)
    scopes: list = field(default_factory=list)  # each statement's MIR scope
    terminator: str = ""
    successors: tuple = ()
    returns: int | None = None  # a call's return edge, the only edge that sets its result


@dataclass
class Body:
    path: str
    arguments: frozenset
    file: str | None
    blocks: dict  # {number: Block}
    # {name: [(local or None, file, line, type of a projection or None, scope)]}
    bindings: dict
    supported: bool = True
    # Lines of every span in the body's own file (its own code, and the definitions of
    # same-file macros it expands).
    lines: frozenset = frozenset()
    types: dict = field(default_factory=dict)  # {local: its type as MIR prints it}
    scopes: dict = field(default_factory=lambda: {0: None})  # {scope: its parent}
    # {(file, line): the scopes of the statements with a span on that line}
    line_scopes: dict = field(default_factory=dict)
    _assigned: dict | None = None
    _entry: dict | None = None
    _direct: set | None = None

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

    def local(self, name, declared=None, visible=None):
        """The MIR local an lldb-dap variable shows: `name`, or `name @ file:line` when
        several are in scope. Of a name bound more than once, the one binding in the
        ``visible`` scopes, if there is one. None without a single whole-local binding."""
        bindings = self.bindings.get(name, [])
        if declared is not None:
            file, line = declared
            bindings = [b for b in bindings if Path(b[1]).name == file and b[2] == line]
        bindings = self.in_scopes(bindings, visible)
        if len(bindings) != 1 or bindings[0][0] is None:
            return None
        return bindings[0][0]

    def _assignments(self):
        """{local: [what is assigned to it]}, whole (`_5 = &(*_43);`, calls) or in part
        (`(_3.0: *const Pt) = move _7;`)."""
        if self._assigned is None:
            assigned = {}
            for block in self.blocks.values():
                for text, _, _ in block.statements:
                    found = _WHOLE_ASSIGNMENT.match(text) or _PART_ASSIGNMENT.match(text)
                    if found:
                        assigned.setdefault(int(found[1]), []).append(found[2])
            self._assigned = assigned
        return self._assigned

    def stop_positions(self, blocks, line, file, foreign, breakpoint):
        """(block, index) where a stop on ``line`` of ``file`` can be in ``blocks``,
        before that statement runs; None if a block's code of the line is not understood.
        A statement is the line's when it spans that line (or another file, or none, when
        the line may invoke a macro: ``foreign``).

        At a breakpoint, LLDB stops only at the lowest address of the line in each
        lexical block: the line's first code in a block, unless every block branching
        there ends with the line's own code in the same scope, after code of the line of
        that scope. rustc appends a block to the function when first branched to, and
        opt-level 0 keeps that order, so the branching code is at a lower address. A
        call that may be inlined is passed over: after it is a place too.
        Anywhere else (a step, a pause) the stop can be before any statement of the line,
        or at a call's or drop's return (`step --out`)."""
        predecessors, found = self.predecessors(), []

        def on_line(spanned, spanned_line):
            return spanned_line == line if spanned == file else spanned is None or foreign

        def coded(block):
            return [
                index
                for index, (text, spanned, spanned_line) in enumerate(block.statements)
                if on_line(spanned, spanned_line) and not _NO_CODE.match(text)
            ]

        def covers(number, scope):
            # A block ending in the line's branch or call, with code of the line in
            # ``scope`` before it (a call to an `#[inline(always)]` function leaves no
            # call instruction behind).
            block = self.blocks.get(number)
            if block is None or not block.statements:
                return False
            text, spanned, spanned_line = block.statements[-1]
            if not on_line(spanned, spanned_line) or _scope(block, -1) != scope:
                return False
            if _BRANCHES.match(text):
                return True
            return any(
                on_line(spanned, spanned_line)
                and _scope(block, index) == scope
                and _emits_code(text, self.types)
                for index, (text, spanned, spanned_line) in enumerate(block.statements[:-1])
            )

        for number in sorted(blocks):
            block = self.blocks.get(number)
            if block is None:
                return None
            indexes = coded(block)
            if not breakpoint:
                found.extend((number, index) for index in indexes)
                terminator = len(block.statements) - 1
                if terminator in indexes and (
                    block.returns is not None or _DROP.match(block.terminator)
                ):
                    found.append((number, len(block.statements)))
                continue
            if not indexes:
                continue
            first = indexes[0]
            # Code of another line earlier in this block runs first: the line starts here.
            later = all(
                _SPAN_ONLY.match(text) or _NO_CODE.match(text)
                for text, _, _ in block.statements[:first]
            )
            froms = predecessors.get(number, set())
            if (
                later
                and number != 0
                and froms
                and all(covers(other, _scope(block, first)) for other in froms)
            ):
                continue
            found.append((number, first))
            # A call with no code of the line before it in its scope: inlined (an
            # `#[inline(always)]` callee, `Option::unwrap`, at every opt-level), its code
            # carries the callee's lines, and the line's first address is after it.
            terminator = len(block.statements) - 1
            if block.returns is not None and terminator in indexes:
                scope = _scope(block, terminator)
                if not any(
                    _scope(block, index) == scope
                    and _emits_code(block.statements[index][0], self.types)
                    for index in indexes[:-1]
                ):
                    found.append((number, len(block.statements)))
        return found

    def used_ahead(self, local, start):
        """True if ``local`` is used on some path from ``start`` ((block, index): before
        that statement) before it is assigned again or its storage ends. NLL keeps every
        borrow it holds live there: what it borrows has not been moved, dropped or
        written since it was made."""
        pending, seen = [start], set()
        while pending:
            number, index = pending.pop()
            if (number, index) in seen or number not in self.blocks:
                continue
            seen.add((number, index))
            block = self.blocks[number]
            ended = False
            for text, _, _ in block.statements[index:]:
                event = _event(text, local)
                if event == "use":
                    return True
                if event == "end":
                    ended = True
                    break
            if not ended:
                pending.extend((successor, 0) for successor in block.successors)
        return False

    def used_before(self, local, start):
        """True if every path into ``start`` passes a use of ``local`` (or its assignment)
        with nothing since that can move, drop, free or write what it points into: no
        call, drop, write to a place's part or a borrowed local, nor end of a borrowed
        local's storage. At a use the borrow checker has kept its target; only branches,
        assertions and whole assignments to locals nothing borrows run between."""
        predecessors, borrowed = self.predecessors(), self._borrowed()
        pending, seen = [start], set()
        while pending:
            number, index = pending.pop()
            if (number, index) in seen:
                continue
            seen.add((number, index))
            block = self.blocks.get(number)
            if block is None:
                return False
            used = False
            for text, _, _ in reversed(block.statements[:index]):
                event = _event(text, local)
                assigned = _WHOLE_ASSIGNMENT.match(text)
                if event == "use" or (assigned and int(assigned[1]) == local):
                    used = True
                    break
                if event == "end" or not _harmless(text, borrowed):
                    return False
            if used:
                continue
            froms = predecessors.get(number, ())
            if number == 0 or not froms:
                return False
            pending.extend((other, len(self.blocks[other].statements)) for other in froms)
        return True

    def _borrowed(self):
        """Locals whose own storage is borrowed somewhere (`&_3`, `&mut (_3.0: Pt)`,
        `&raw const _3`), not through a pointer they hold (`&(*_3)`)."""
        if self._direct is None:
            self._direct = {
                int(number)
                for block in self.blocks.values()
                for text, _, _ in block.statements
                for number in _DIRECT_BORROW.findall(text)
            }
        return self._direct

    def visible_scopes(self, line, path):
        """The scopes of the statements on ``line`` of the frame's source ``path``, and
        the scopes enclosing them: where a binding lldb-dap shows there is declared
        (rustc's DWARF lexical blocks are these scopes). None if no statement is on it."""
        shown = str(path or "").replace("\\", "/")
        found = set()
        for (file, number), scopes in self.line_scopes.items():
            if number != line or not (shown == file or shown.endswith("/" + file)):
                continue
            for scope in scopes:
                while scope is not None and scope not in found:
                    found.add(scope)
                    scope = self.scopes.get(scope)
        return found or None

    @staticmethod
    def in_scopes(bindings, visible):
        """The bindings declared in ``visible`` scopes; all of them if none is (or no
        scope is known): never fewer than lldb-dap could be showing."""
        kept = [binding for binding in bindings if visible is None or binding[4] in visible]
        return kept or list(bindings)

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


_CLOSURE_SEGMENT = re.compile(r"_?\{+closure(?:#[0-9]+)?\}+")


def _shape(segments):
    """(the nearest named function, how many closures deep) of a path's segments."""
    depth = 0
    for segment in reversed(segments):
        if _CLOSURE_SEGMENT.fullmatch(segment):
            depth += 1
            continue
        return segment, depth
    return None, depth


def frame_shape(name):
    """The shape of lldb-dap's frame name (`vb::outer::{{closure}}::{{closure}}`, or
    escaped `_$u7b$$u7b$closure$u7d$$u7d$`, with a legacy hash or not)."""
    text = str(name or "").replace("$u7b$", "{").replace("$u7d$", "}")
    # lldb-dap marks an optimized frame's name ` [opt]` (macOS).
    text = re.sub(r"\s*\[opt\]$", "", text)
    text = re.sub(r"::h[0-9a-f]{16}$", "", text)
    return _shape(_segments(text))


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


def _event(text, local):
    """What ``text`` does to ``local``: "use" (reads it), "end" (assigns it whole, or
    ends its storage), or None."""
    if text.startswith(f"StorageDead(_{local})"):
        return "end"
    # Drop glue keeps no loan alive (`#[may_dangle]`, a struct without `Drop` holding a
    # reference beside a String); a type whose own `Drop` reads it would be over-gated.
    if _NOT_A_USE.match(text) or _DROP.match(text):
        return None
    if found := _WHOLE_ASSIGNMENT.match(text):
        if local in {int(n) for n in _MENTIONED.findall(found[2])}:
            return "use"
        if int(found[1]) == local:
            return "end"
        return None
    if local in {int(n) for n in _MENTIONED.findall(text)}:
        return "use"
    return None


def _scope(block, index):
    try:
        return block.scopes[index]
    except IndexError:
        return None


def _emits_code(text, types):
    """True if the statement surely leaves an instruction at opt-level 0: an assignment
    of a value that is not zero-sized (`()`, a closure, a fn item, `PhantomData`, a unit
    struct's literal names no local)."""
    found = _WHOLE_ASSIGNMENT.match(text) or _PART_ASSIGNMENT.match(text)
    if not found or "->" in text or _SPAN_ONLY.match(text):
        return False
    kind = types.get(int(found[1]))
    if kind is None or _ZERO_SIZED.search(kind):
        return False
    return bool(_MENTIONED.search(found[2]) or re.match(r"const -?[0-9]", found[2]))


def _harmless(text, borrowed):
    """True if the statement cannot move, drop, free or write what a reference points
    into: a branch, an assertion, a storage marker of a local never borrowed itself, a
    whole assignment (not a call) to a local never borrowed itself."""
    if _BRANCHES.match(text) or text.startswith("goto ") or _NOT_A_USE.match(text):
        return True
    if storage := re.match(r"^StorageDead\(_([0-9]+)\);$", text):
        return int(storage[1]) not in borrowed
    if text == "nop;":
        return True
    found = _WHOLE_ASSIGNMENT.match(text)
    return bool(found) and "->" not in text and int(found[1]) not in borrowed


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
    block, spans, opened_scopes = None, [], [0]
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
            typed = None if whole else _projected_type(debug[2])
            body.bindings.setdefault(debug[1], []).append(
                (
                    int(whole[1]) if whole else None,
                    debug[4],
                    int(debug[5]),
                    typed,
                    int(debug[3]),
                )
            )
            spans.append((debug[4], int(debug[5])))
            continue
        if block is None and (scope := _SCOPE.match(line)):
            body.scopes[int(scope[1])] = opened_scopes[-1]
            opened_scopes.append(int(scope[1]))
            continue
        if block is None and len(opened_scopes) > 1 and re.fullmatch(r" +\}", line):
            opened_scopes.pop()
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
        span = _SPAN.match(statement[3])
        block.statements.append(
            (statement[1], span[1] if span else None, int(span[2]) if span else None)
        )
        block.scopes.append(int(statement[2]))
        if span:
            spans.append((span[1], int(span[2])))
            # Every line the span covers: rustc may put the code on a later one (a method
            # call's on its name's line, `K\n.get()`, while its span starts at `K`).
            end = _SPAN_END.match(statement[3])
            last = int(span[2])
            if end and int(end[1]) > last and not _SPAN_ONLY.match(statement[1]):
                last = int(end[1])
            for number in range(int(span[2]), last + 1):
                body.line_scopes.setdefault((span[1], number), set()).add(int(statement[2]))
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

    def _visible(self, body, line, path):
        """The scopes a binding shown at ``line`` can be in, or None to keep them all.
        A line that may invoke a macro runs code whose spans are in another file
        (`eprintln!`'s, collapsed onto the line), in scopes no statement of this file
        there shows: the stop can be in one of those."""
        if line is None:
            return None
        # The stop's own file: a macro defined in another module, or `include!`d code,
        # stops in this frame at another file's line.
        shown = str(path or "").replace("\\", "/")
        matched = [name for name in self.texts if shown == name or shown.endswith("/" + name)]
        if len(matched) != 1 or may_invoke_macro(self.texts[matched[0]], line):
            return None
        return body.visible_scopes(line, path)

    def _local(self, body, variable_name, line=None, path=None):
        name, at, declared = str(variable_name).partition(" @ ")
        where = None
        if at:
            file, _, number = declared.rpartition(":")
            if not number.isdigit():
                return None
            where = (file, int(number))
        return body.local(name, where, self._visible(body, line, path))

    def declared_type(self, hashed, variable_name, line=None, path=None):
        """The MIR type of the frame's local ``variable_name`` (`std::vec::Vec<usize>`),
        which LLDB's C-style names cannot tell apart (u64 and usize); else None. With the
        stop's ``line`` and ``path``, a name bound more than once is the binding in scope
        there, if only one is."""
        body = self.body(hashed)
        local = self._local(body, variable_name, line, path) if body is not None else None
        return None if local is None else body.types.get(local)

    def binding_types(self, hashed, variable_name, path=None, line=None, closure=None, frame=None):
        """The MIR type of every binding lldb-dap could be showing as ``variable_name``
        (None for one whose type is not written out), in the frame's body. An async
        body or an unmangled function is never matched for proofs; its types come from
        the bodies whose code covers the stop line, of the frame's own shape when one
        is (``frame``, lldb-dap's name: `outer::{{closure}}::{{closure}}` is the MIR's
        `outer::{closure#0}::{closure#0}`). Empty when no body binds the name."""
        name, at, declared = str(variable_name).partition(" @ ")
        body = self.body(hashed)
        shown = str(path or "").replace("\\", "/")
        bodies = [body] if body is not None else []
        if body is None:

            def covers(candidate):
                # A span covering the line counts, not only one starting there (`K\n.get()`
                # has none starting on `.get()`'s line, while a nested item may); and in
                # the stop's own file, which a `macro_rules!` from another module puts
                # in a body of this file.
                if line in candidate.lines and shown.endswith("/" + str(candidate.file)):
                    return True
                return any(
                    number == line and (shown == file or shown.endswith("/" + file))
                    for file, number in candidate.line_scopes
                )

            bodies = [
                candidate
                for candidate in self.bodies
                if covers(candidate)
                # A closure defined on the line is a body of its own, with its own
                # captures of the frame's names: only bodies of the frame's kind count.
                and (closure is None or candidate.name.startswith("{closure") == closure)
            ]
            # An outer async body's spans cover its nested `async { }` block's lines too
            # (the coroutine's construction): the frame's own shape picks among them.
            wanted = frame_shape(frame) if frame else None
            shaped = [c for c in bodies if wanted and _shape(_segments(c.path)) == wanted]
            bodies = shaped or bodies
        found = []
        for candidate in bodies:
            # Only the bindings in scope on the stop line: a name `println!` or a sibling
            # block binds (`args`, `q`) is not the one lldb-dap shows.
            visible = self._visible(candidate, line, path)
            bindings = candidate.in_scopes(candidate.bindings.get(name, ()), visible)
            for local, file, number, projected, _ in bindings:
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
        site = self._site(hashed, line, path)
        if site is None:
            return False
        local = self._local(site[0], variable_name, line, path)
        return local is not None and self._initialized(site, local, line)

    def reference_valid(self, hashed, variable_name, line, path, breakpoint=False):
        """For the frame's local ``variable_name`` whose type borrows (a reference, `&str`,
        a tuple, `Vec` or closure holding one, a raw pointer): True if what it points into
        is shown to stand at ``line`` of ``path``, False if not (#110), None if this does
        not apply (another type, the site or the local not understood). ``breakpoint``:
        the stop is a line breakpoint's, not a step's or a pause's.

        True where, from every place the stop can be on the line, the code still uses it
        ahead, or used it last with nothing since that can move, drop or write (no call,
        no drop): the borrow checker keeps what it borrows alive and unchanged until its
        last use. Past a call or drop after its last use, what it pointed into may be
        moved, dropped or reallocated, which no simpler reading of the MIR can rule out
        (interior mutability, `&mut` aliases, loops), so then it is False. A raw pointer keeps no
        borrow, so it is False (one inside a tuple reads with the tuple). A fn pointer, a reference argument never reassigned (its
        target outlives the call) and a local only ever assigned constants (`"text"`, a
        promoted `&[1, 2]`) are True; a value not yet assigned (its declaration line) is
        False."""
        site = self._site(hashed, line, path)
        if site is None:
            return None
        body, function, blocks, shared = site
        local = self._local(body, variable_name, line, path)
        kind = body.types.get(local) if local is not None else None
        if kind is None or not _BORROWING.search(kind):
            return None
        if _FN_POINTER.match(kind):
            return True
        # A raw pointer (or an array of them) keeps no borrow. One inside a tuple or a
        # generic reads with the value that holds it, as one in a struct does (the MIR
        # names no field types).
        if _RAW_POINTER.match(kind.lstrip("[")):
            return False
        assigned = body._assignments().get(local, [])
        if local in body.arguments and kind.startswith("&") and not assigned:
            return True
        if assigned and all(value.startswith("const ") for value in assigned):
            return True
        file = function.file
        foreign = may_invoke_macro(self.texts.get(file), line)
        positions = body.stop_positions(blocks, line, file, foreign, breakpoint)
        if not positions:
            return None
        # Not yet assigned (its declaration line), it holds whatever its slot last held.
        if not self._initialized(site, local, line):
            return False
        return all(
            body.used_ahead(local, position) or body.used_before(local, position)
            for position in positions
        )

    def _site(self, hashed, line, path):
        """(body, function, the line's blocks, their merges) for a proof at ``line``."""
        body = self.body(hashed)
        function = self.functions.get(hashed)
        if body is None or function is None or type(line) is not int:
            return None
        shown = str(path or "").replace("\\", "/")
        if [source for source in self.sources if shown.endswith("/" + source)] != [function.file]:
            return None
        blocks = function.mir_blocks(line)
        if blocks is None:
            return None
        shared = _shared(body, function, blocks)
        return body, function, set(shared), shared

    def _initialized(self, site, local, line):
        body, function, blocks, shared = site
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
    # A trailing `// comment` ends no path (`let n = x * 2; // doubled`), unless a quote
    # before it may hold the `//`.
    code, comment, after = own.partition("//")
    # A quote before it may hold the `//`; a quote or `*/` after it, a string or block
    # comment that began on an earlier line and closes here.
    plain = comment and not any(mark in code for mark in "\"'") and '"' not in after
    if not _PATH_END.search(code if plain and "*/" not in after else own):
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
