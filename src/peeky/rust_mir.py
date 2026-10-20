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
terminator, a coroutine whose state is not understood, a debug binding through a
projection, a name bound twice, a body that cannot be identified positively, an IR
block of unknown origin, a stop in another file. Unsafe code that duplicates ownership
(`ptr::read`) is outside the proof.

A coroutine (an async body) is a body like any other once each local its state keeps
across a suspension is renamed a local of its own, and each suspension flows into the
block resuming it (#116). A function LLVM inlined everywhere (`#[inline(always)]`) has
no IR of its own: its MIR body is found by the lines of its inlined copies, and every
statement of the stop line is a place the stop can be.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_MIR_BYTES = 64 * 1024 * 1024
MAX_ITERATIONS = 10_000
# The dataflow's budget per reachable block, beyond MAX_ITERATIONS in all.
STEPS_PER_BLOCK = 50

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
_FULL_SPAN = re.compile(r"^([^:]+):([0-9]+):([0-9]+): ([0-9]+):([0-9]+)")
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
_RAW_POINTER = re.compile(
    r"\*const |\*mut |(?:(?:std|core)::ptr::)?NonNull<|(?:(?:std|core)::sync::atomic::)?AtomicPtr<"
)
_FN_POINTER = re.compile(r"(?:for<[^<>]*> )?(?:unsafe )?(?:extern \S+ )?fn\(")
# A type that borrows: a reference, a raw pointer, one holding a lifetime, or a closure
# or coroutine (whose captures may be references).
_BORROWING = re.compile(r"&|\*const |\*mut |NonNull<|AtomicPtr<|'|\{closure@|\{coroutine@|\{async")


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
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_DROP_PLACE = re.compile(r"^drop\((.+)\) -> ")
# A place of a local's own, no deref in it: a field (`(_1.1: Box<Pt>)`, `((_1.0: S).2:
# T)`) or an element (`_1[_5]`) of the local `_1` itself (#108).
_DIRECT_PLACE = re.compile(r"^\(*_([0-9]+)[.\[]")
# A constant stored in a local (`_20 = const false;`, a drop flag).
_CONSTANT_STORE = re.compile(r"^_([0-9]+) = const [^;]*;$")
_STORE = re.compile(r"^(\(+\*_[0-9]+\).*?|\(\*_[0-9]+\)) = ")
# `_23 = copy ((BOX.0: Unique<T>).0: NonNull<T>) as *const T (Transmute)`: BOX is a Box
# place (`_1`, `(_1.2: Box<T>)`, `((*_13).1: Box<Inner>)`).
_BOX_POINTER = re.compile(
    r"^_([0-9]+) = copy \(\((.+?)\.0: (?:std::ptr::|core::ptr::)?Unique<.*\bNonNull<.*\) "
    r"as \*(?:const|mut) .*\(Transmute\);$"
)
# A copy of a Box held in a place (`_17 = copy ((*_37).1: Box<Inner>)`) or of such a
# copy (`_18 = copy _17`): the built MIR derefs a nested Box through it.
_BOX_COPY = re.compile(
    r"^_([0-9]+) = copy (\(.*: (?:std::boxed::|alloc::boxed::)?Box<.*>\)|_[0-9]+);$"
)
# A drop through a pointer: `drop((*_1))`, `drop(((*_8).1: T))`.
_DROP_THROUGH = re.compile(r"^drop\(\(*\*\(*_([0-9]+)\b")
# A call's arguments (`f(move _7, copy _8) -> [return: ...]`).
_CALL_ARGUMENTS = re.compile(r"^(?:.*? = )?[^ (][^(]*?\((.*)\) -> ")
_NOT_A_CALL = re.compile(
    r"(?:drop|assert|switchInt|goto|falseEdge|falseUnwind|yield|return|resume|unreachable)\b"
)
# Types whose values hold no pointer, so are never an alias.
_POINTERLESS = re.compile(
    r"[iu](?:8|16|32|64|128|size)|f32|f64|bool|char|\(\)|(?:std|alloc)::string::String"
)
# A move or drop through a pointer: `move ((*_23).1: T)`, `drop(((*_23).1: T))`.
_THROUGH = re.compile(r"(?:\bmove\s+|^drop\()\(*\*_([0-9]+)\b")
_WRITABLE_BORROW = re.compile(r"&(?:mut |raw const |raw mut )\(*_([0-9]+)\b")
_DIRECT_BORROW = re.compile(r"&(?:mut |raw const |raw mut |fake \w+ )?\(*_([0-9]+)\b")
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
    # each statement's (start column, end line, end column), as the MIR prints them
    # (characters, 1-based), or None
    spans: list = field(default_factory=list)
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
    # {(name, file, line, scope): place} for bindings through a projection (a closure's
    # capture `(*((*_1).0: &mut T))`, an async local).
    places: dict = field(default_factory=dict)
    scopes: dict = field(default_factory=lambda: {0: None})  # {scope: its parent}
    # {(file, line): the scopes of the statements with a span on that line}
    line_scopes: dict = field(default_factory=dict)
    inlined_scopes: set = field(default_factory=set)
    # A coroutine's (#116): the local pointing at its state (`_34 = copy (_1.0: &mut
    # {async fn body of f()})`; -1 if not found), the locals standing for what its state
    # saves across a suspension (`(((*_34) as variant#3).0: &Pt)`), and {suspending
    # block: the block resuming it}, which the dispatch at bb0 jumps to on a later poll.
    coroutine: int | None = None
    # The type parameters of a generic body (IrFunction.generics, set when an IR function
    # is matched to it): a type named like a primitive (`i8`) may be one of them. None
    # until then (a twin a macro generates): any may be.
    generics: tuple | None = None
    saved: frozenset = frozenset()
    resumes: dict = field(default_factory=dict)
    _lent_cache: frozenset | None = None
    _written_fields: frozenset = frozenset()
    _into: dict | None = None
    _declared_names: dict | None = None
    _assigned: dict | None = None
    _entry: dict | None = None
    _direct: set | None = None
    _writable: set | None = None
    _preds: dict | None = None
    _box_temps: dict | None = None
    _box_paths: dict | None = None
    _replaced: dict | None = None
    _alias_cache: dict | None = None
    _alias_edges: dict | None = None
    # The ``generics`` the alias graph was built for: it is rebuilt when they change.
    _alias_generics: tuple | None = ()
    _dropped: set | None = None
    _offset: int | None = None

    @property
    def name(self):
        return _segments(self.path)[-1]

    @property
    def parent(self):
        parts = _segments(self.path)
        return parts[-2] if len(parts) > 1 else None

    def predecessors(self):
        if self._preds is None:
            found = {}
            for number, block in self.blocks.items():
                for target in block.successors:
                    found.setdefault(target, set()).add(number)
            self._preds = found
        return self._preds

    def lexical(self, scope):
        """The DWARF lexical block of MIR ``scope``: rustc gives one only to a scope that
        declares a variable (or is inlined); a statement elsewhere is in its parent's.
        0 is the function's own."""
        declared = {binding[4] for found in self.bindings.values() for binding in found}
        for _ in range(4096):
            if scope in (None, 0) or scope in declared or scope in self.inlined_scopes:
                return scope or 0
            scope = self.scopes.get(scope)
        return 0

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
                    # A write through it (`((*_1).0: i64) = ...`) changes what it points
                    # at, not where.
                    found = _WHOLE_ASSIGNMENT.match(text) or (
                        None if re.match(r"\(+\*_", text) else _PART_ASSIGNMENT.match(text)
                    )
                    if found:
                        assigned.setdefault(int(found[1]), []).append(found[2])
            self._assigned = assigned
        return self._assigned

    def scope_chain(self, scope):
        """The names of the variables each DWARF lexical block declares, sorted, from the
        block of MIR ``scope`` (see ``lexical``) out to the function's own (#115)."""
        declared = self._declared()
        chain, scope = [], self.lexical(scope)
        for _ in range(4096):
            if scope in (None, 0):
                break
            chain.append(declared.get(scope, ()))
            scope = self.lexical(self.scopes.get(scope))
        chain.append(declared.get(0, ()))
        return tuple(chain)

    def _declared(self):
        """{scope: the names of the variables it declares, sorted}."""
        if self._declared_names is None:
            found = {}
            for name, bindings in self.bindings.items():
                for binding in bindings:
                    found.setdefault(binding[4], []).append(name)
            self._declared_names = {scope: tuple(sorted(names)) for scope, names in found.items()}
        return self._declared_names

    def chain_scopes(self, block, function=None):
        """The MIR scopes whose bindings lldb-dap shows at a stop in ``block`` (#115): the
        scopes with its chain of names and those enclosing them, where the function's
        debug info and IR (``function``) have as many blocks with that chain as the body
        has scopes; else None."""
        if function is None or not function.scoped:
            return None
        declared = self._declared()
        matched = [
            scope
            for scope in {0, *declared}
            if (scope == 0 or scope == self.lexical(scope))
            and self.scope_chain(scope) == block.chain
        ]
        count = block.chains.get(block.chain)
        if not matched or count != len(matched) or function.chains.get(block.chain) != count:
            return None
        found = set()
        for scope in matched:
            for _ in range(4096):
                if scope is None:
                    break
                found.add(scope)
                scope = self.scopes.get(scope)
        return found

    def _in_block(self, found, block, line, function, breakpoint=False):
        """The places of ``found`` ({position: its statements, or None}) whose code is in
        the lexical block the stop's pc is in (#115), or all of them where that block is
        not told apart: every MIR scope a statement placed there is in must have as many
        DWARF blocks with its chain of names as the body has scopes with it, just one of
        them the stop's, and the IR (``function``) must have as many blocks with the
        stop's chain as the DWARF, just one with code of ``line``. Code no statement
        explains stays.

        A line breakpoint stops at the line's lowest address in that block, so at or
        before the IR's first instruction of the line there that surely leaves code
        (``_cut``): places past it go.

        Returns (the places kept, the MIR scope told apart or None)."""
        scopes = {
            self.lexical(_scope(self.blocks[number], index))
            for owners in found.values()
            if owners
            for number, index in owners
        }
        if (
            not scopes
            or scopes & self.inlined_scopes
            or function is None
            or not function.agrees(block, line)
        ):
            return found, None
        declared = self._declared()
        counts = {}
        for scope in {0, *declared}:
            if scope == 0 or scope == self.lexical(scope):
                chain = self.scope_chain(scope)
                counts[chain] = counts.get(chain, 0) + 1
        chains = {scope: self.scope_chain(scope) for scope in scopes}
        if any(block.chains.get(chain) != counts.get(chain) for chain in chains.values()):
            return found, None
        matched = [scope for scope, chain in chains.items() if chain == block.chain]
        if len(matched) != 1 or block.chains.get(block.chain) != counts.get(block.chain):
            return found, None
        kept = {
            position: owners
            for position, owners in found.items()
            if owners is None
            or any(self.lexical(_scope(self.blocks[n], i)) == matched[0] for n, i in owners)
        }
        if kept and breakpoint and (cut := self._cut(function, line, block.chain)) is not None:
            kept = {
                position: owners
                for position, owners in kept.items()
                if (order := self._address_order(position, function)) is None or order <= cut
            }
        if not kept:
            return found, None
        return kept, matched[0]

    def _cut(self, function, line, chain):
        """The last place (``_address_order``) a line breakpoint's stop in the lexical
        block with ``chain`` can be, or None if unknown (#115). LLDB stops at the line's
        lowest address in the block. The IR's first instruction of the line there that
        surely leaves code (a call, a store of bytes, a branch on a value) is at or after
        it: opt-level 0 keeps the IR's order for addresses. Codegen emits the MIR blocks
        merged into an IR block in order, each call terminator one call there or none
        (inlined), so that instruction is before the MIR block of the IR's next call
        ends, which is that many calls later in the MIR or a few more."""
        for place, label in enumerate(function.blocks):
            for number, named, calls in function.sure.get(label, ()):
                if number != line or named != chain:
                    continue
                found = re.fullmatch(r"bb([0-9]+)", label)
                if label != "start" and found is None:
                    return None
                head = int(found[1]) if found else 0
                ir = self._ir_chain(head, function) if head in self.blocks else None
                if ir is None or ir[0] != label:
                    return None
                members = ir[1]
                callers = [
                    member
                    for member in members
                    if _call(self.blocks[member], len(self.blocks[member].statements) - 1)
                    or _DROP.match(self.blocks[member].terminator)
                ]
                # A call LLVM inlined (or a drop with no glue) leaves no call of its own:
                # the IR's call after the instruction is then the MIR's at most that many
                # calls later.
                missing = len(callers) - function.calls.get(label, -1)
                if missing < 0 or missing > len(callers):
                    return None
                end = callers[calls + missing] if calls + missing < len(callers) else members[-1]
                return place, members.index(end), len(self.blocks[end].statements) - 1
        return None

    def _address_order(self, position, function):
        """Where ``position``'s code is in ``function``'s IR (the label's place, the MIR
        block's place among those merged into it, the statement's), or None."""
        chain = self._ir_chain(position[0], function)
        if chain is None:
            return None
        label, members = chain
        labels = {name: index for index, name in enumerate(function.blocks)}
        if label not in labels:
            return None
        return labels[label], members.index(position[0]), position[1]

    def stop_positions(
        self, blocks, line, file, foreign, facts=None, shared=None, within=None, function=None
    ):
        """[(block, index)]: where a stop on ``line`` of ``file`` can be in ``blocks``,
        before that statement runs; None if a block is missing. A statement is the
        line's when it starts on it (a call: spans it, `w\n.len()`'s is on `.len()`'s
        line), or spans another file or none when the line may invoke a macro
        (``foreign``). After a call or drop of the line is its target's start; code of
        the line that no statement of a block explains (a result store) is anywhere in
        that block, unless another block codegen merged into the same IR block
        (``shared``: {block: the blocks of its IR block}) has statements of the line.

        ``facts`` (a ``LineFacts``) says the stop is a line breakpoint's: LLDB stops at
        the line's lowest address in each DWARF lexical block, and the stop's column is
        known. Code of the line in a block every predecessor branches to from the line's
        own code of the same lexical block is never the lowest: rustc appends an IR
        block when first branched to, and opt-level 0 keeps that order. Only a call or
        branch the IR shows there, not inlined, counts as such code. Where the column
        cannot be a statement's (its own, a macro's on the line, or within a call), the
        stop is not before it.

        ``within`` (a ``StopBlock``): the DWARF lexical block the stop's pc is in, which
        keeps only the places in its MIR scope, where it and the IR's ``function`` tell
        that apart, and at a line breakpoint, only those up to ``_cut`` (#115)."""
        found = self._placed(blocks, line, file, foreign, facts, shared)
        if found is None:
            return None
        if within is not None:
            found, _ = self._in_block(found, within, line, function, facts is not None)
        if facts is None or facts.column is None:
            return sorted(found)
        kept = [
            position
            for position, owners in found.items()
            if owners is None or any(self._could_be(o, i, line, file, facts) for o, i in owners)
        ]
        return sorted(kept or found)

    def stop_scope(self, blocks, line, file, foreign, within, shared=None, function=None):
        """The MIR scope of the DWARF lexical block a stop on ``line`` is in (``within``,
        a ``StopBlock``), where the line's places tell it apart and no code of the line
        there goes unexplained (#115); else None. Its bindings and those of the scopes
        around it are the ones lldb-dap shows, where two scopes have the same names
        (two `for item in items` loops)."""
        found = self._placed(blocks, line, file, foreign, None, shared)
        if found is None:
            return None
        kept, scope = self._in_block(found, within, line, function)
        if scope is None or None in kept.values():
            return None
        return scope

    def _placed(self, blocks, line, file, foreign, facts=None, shared=None):
        """{position: [(block, index) of each statement whose code is there], or None
        for code of the line no statement explains} (see ``stop_positions``)."""
        predecessors, found = self.predecessors(), {}
        explained = set()

        def owned(number, index):
            text, spanned, spanned_line = self.blocks[number].statements[index]
            if _NO_CODE.match(text):
                return False
            if spanned != file:
                return spanned is None or foreign
            if spanned_line == line:
                return True
            span = _span(self.blocks[number], index)
            return (
                _call(self.blocks[number], index)
                and span is not None
                and spanned_line < line <= span[1]
            )

        def place(position, number, index):
            # The statement whose code is at ``position``: its scope and possible columns.
            if position not in found:
                found[position] = []
            if found[position] is not None:
                found[position].append((number, index))

        lines = {}
        for number in sorted(blocks):
            block = self.blocks.get(number)
            if block is None:
                return None
            indexes = [index for index in range(len(block.statements)) if owned(number, index)]
            # A predecessor's call of the line returning here stores its result here
            # (after an `invoke`, or an inlined body).
            stores = [
                other
                for other in sorted(predecessors.get(number, ()))
                if _target(self.blocks[other]) == number
                and _call(self.blocks[other], len(self.blocks[other].statements) - 1)
                and owned(other, len(self.blocks[other].statements) - 1)
            ]
            lines[number] = (indexes, stores)
            if indexes or stores:
                explained.add(number)
        for number in sorted(blocks):
            block = self.blocks[number]
            terminator = len(block.statements) - 1
            indexes, stores = lines[number]
            if not indexes and not stores:
                if not (set((shared or {}).get(number, ())) - {number}) & explained:
                    for index in range(len(block.statements)):
                        found[(number, index)] = None
                continue
            candidates = [((number, index), number, index) for index in indexes]
            candidates += [
                ((number, 0), other, len(self.blocks[other].statements) - 1) for other in stores
            ]
            for index in indexes:
                if index == terminator and (_call(block, index) or _DROP.match(block.terminator)):
                    target = _target(block)
                    if target is not None and (
                        facts is None or not self._sure(number, index, line, file, facts)
                    ):
                        candidates.append(((target, 0), number, index))
            for position, owner, index in candidates:
                if facts is not None and number != 0:
                    lexical = self.lexical(_scope(self.blocks[owner], index))
                    froms = predecessors.get(position[0], ())
                    if (
                        position[0] == number
                        and froms
                        and all(
                            other in blocks and self._sure_end(other, lexical, line, file, facts)
                            for other in froms
                        )
                    ):
                        continue
                place(position, owner, index)
        return found

    def anywhere(self, blocks):
        """Every place in ``blocks``, and after each call or drop ending one."""
        found = set()
        for number in blocks:
            block = self.blocks.get(number)
            if block is None:
                return None
            found.update((number, index) for index in range(len(block.statements)))
            terminator = len(block.statements) - 1
            if _call(block, terminator) or _DROP.match(block.terminator):
                if (target := _target(block)) is not None:
                    found.add((target, 0))
        return sorted(found)

    def covering(self, line, file, foreign):
        """The blocks holding code of ``line`` that no IR places (#116): with a statement
        that emits code there (one starting on it, or a call spanning it: a method call's
        code is on its name's line), or that spans another file where the line may
        invoke a macro (``foreign``)."""
        found = set()
        for number, block in self.blocks.items():
            for index, (text, spanned, spanned_line) in enumerate(block.statements):
                if _NO_CODE.match(text) or spanned is None:
                    continue
                if spanned != file:
                    if not foreign:
                        continue
                elif spanned_line != line:
                    span = _span(block, index)
                    if not (_call(block, index) and span and spanned_line < line <= span[1]):
                        continue
                found.add(number)
                break
        return found

    def initialized_before(self, local, positions):
        """True if ``local`` is initialized at each of ``positions`` ((block, index):
        before that statement), following the dataflow into each block."""
        if not self.supported or not positions:
            return False
        entry = self._dataflow()
        if entry is None:
            return False
        bit = 1 << local
        for number, index in positions:
            state = entry.get(number)
            if state is None or number not in self.blocks:
                return False
            for text, _, _ in self.blocks[number].statements[:index]:
                state = self._transfer(text, state)
            if not state & bit:
                return False
        return True

    def _sure(self, number, index, line, file, facts):
        """True if the IR shows code of statement ``index`` (a terminator) of block
        ``number`` on ``line``, at a column the statement can have: its own call (a
        call's, a drop's glue), not inlined, or its conditional branch (an assert's, a
        switch's). Codegen emits the MIR blocks merged into an IR block in order, each
        call terminator one call there, so the calls are told apart by order, where
        their number matches (an inlined call leaves none of its own)."""
        block = self.blocks[number]
        text, spanned, spanned_line = block.statements[index]
        if index != len(block.statements) - 1:
            return False
        if spanned == file:
            span = _span(block, index)
            if spanned_line != line or span is None or span[1] != line:
                return False
            low, high = facts.to_bytes(span[0]), facts.to_bytes(span[2])
        elif facts.known:
            low = high = None
        else:
            return False

        def columns(found):
            # Its own (within its span), or a macro's on the line it is collapsed onto.
            return any(c in facts.macros or low is not None and low <= c <= high for c in found)

        chain = self._ir_chain(number, facts.function)
        if chain is None:
            return False
        label, members = chain
        calls, branches = facts.function.events.get(label, ((), ()))
        if _call(block, index) or _DROP.match(text):
            if "intrinsics::" in text or columns(facts.inlined):
                return False
            callers = [
                member
                for member in members
                if _call(self.blocks[member], len(self.blocks[member].statements) - 1)
                or _DROP.match(self.blocks[member].terminator)
            ]
            if len(callers) != len(calls):
                return False
            found = calls[callers.index(number)]
        elif _BRANCHES.match(text):
            if members[-1] != number or len(branches) != 1:
                return False
            found = branches[0]
        else:
            return False
        return found[0] == line and columns({found[1]})

    def _ir_chain(self, number, function):
        """(label, [MIR blocks in it, in order]) of the IR block holding ``number``."""
        predecessors, labeled = self.predecessors(), function.labeled()
        head = number
        for _ in range(4096):
            if head in labeled:
                break
            froms = predecessors.get(head, set())
            if len(froms) != 1:
                return None
            (head,) = froms
        else:
            return None
        members = [head]
        while (target := _merge_target(self, function, members[-1], predecessors)) is not None:
            if target in members or len(members) > 4096:
                return None
            members.append(target)
        if number not in members:
            return None
        return ("start" if head == 0 else f"bb{head}"), members

    def _sure_end(self, number, lexical, line, file, facts):
        block = self.blocks.get(number)
        if block is None or not block.statements:
            return False
        index = len(block.statements) - 1
        return self.lexical(_scope(block, index)) == lexical and self._sure(
            number, index, line, file, facts
        )

    def _could_be(self, number, index, line, file, facts):
        """True if the stop's column can be that of statement ``index``'s code."""
        block = self.blocks[number]
        text, spanned, spanned_line = block.statements[index]
        column = facts.column
        if column in facts.macros:
            return True
        if spanned != file:
            # Another file's code is collapsed onto a macro's path on the line.
            return not facts.known
        span = _span(block, index)
        if span is None:
            return True
        start = facts.to_bytes(span[0]) if spanned_line == line else 1
        if _call(block, index):
            return start <= column and (span[1] > line or column <= facts.to_bytes(span[2]))
        return spanned_line == line and column == start

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
                # Through a suspension too: what it borrows stays borrowed while it is.
                resumed = (self.resumes[number],) if number in self.resumes else ()
                pending.extend((successor, 0) for successor in (*block.successors, *resumed))
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

    def _written_through(self):
        """Locals borrowed `&mut` or raw somewhere (`&mut _3`, `&raw const _3`): what
        they hold can change by a write no assignment of their own shows."""
        if self._writable is None:
            self._writable = {
                int(number)
                for block in self.blocks.values()
                for text, _, _ in block.statements
                for number in _WRITABLE_BORROW.findall(_STRING.sub('""', text))
            }
        return self._writable

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
            if any(local in self._kills(text) for text, _, _ in block.statements):
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
                if not state & bit or any(
                    local in self._kills(text) for text, _, _ in block.statements
                ):
                    return False
                continue
            if starts_here and not state & bit:
                return False
            for index, (text, _, _) in enumerate(block.statements):
                if index in positions and not state & bit:
                    return False
                state = self._transfer(text, state)
            # A stop at a call's or drop's return address (`step --out`) is still on
            # this line, after the terminator's moves and drops.
            last = len(block.statements) - 1
            if last in positions and (block.returns is not None or _DROP.match(block.terminator)):
                if not state & bit:
                    return False
        return True

    def _boxes(self):
        """{temp: Box local}: the raw pointers built from a Box's own pointer
        (`_23 = copy ((_1.0: Unique<T>).0: NonNull<T>) as *const T (Transmute)`), which
        the built MIR derefs in place of the Box. A Box inside one (`bx.inner.data`) is
        reached through such a pointer (`((*_23).1: Box<Inner>)`): its own pointers map
        to the outermost local too, as do pointers made from a copy of such a Box
        (`_17 = copy ((*_37).1: Box<Inner>)`). ``_box_paths`` names each pointer's Box
        by a place from that local."""
        if self._box_temps is None:
            made = []
            for block in self.blocks.values():
                for text, _, _ in block.statements:
                    if found := _BOX_POINTER.match(text):
                        made.append((False, int(found[1]), found[2]))
                    elif found := _BOX_COPY.match(text):
                        made.append((True, int(found[1]), found[2]))
            # A pointer made from a copy waits for the copy to be told apart; a copy
            # whose Box never is (copied out of a reference) is a Box of its own.
            waiting = {temp for copy, temp, _ in made if copy}
            boxes, paths, copies, changed = {}, {}, {}, True
            while changed or waiting - copies.keys():
                if not changed:
                    waiting = set(copies)
                changed = False
                for copy, temp, place in made:
                    if temp in (copies if copy else boxes):
                        continue
                    root = re.match(r"\(*(\*?)_([0-9]+)\b", place)
                    if root is None:
                        continue
                    inner = int(root[2])
                    if root[1]:
                        if inner not in boxes:
                            continue
                        box = boxes[inner]
                        path = place.replace(f"(*_{inner})", f"(*{paths[inner]})", 1)
                    elif inner in copies:
                        box, held = copies[inner]
                        path = re.sub(rf"\b_{inner}\b", lambda _, held=held: held, place, count=1)
                    elif inner in waiting or (copy and place == f"_{inner}"):
                        continue
                    else:
                        box, path = inner, f"B{place}"
                    if copy:
                        copies[temp] = (box, path)
                    else:
                        boxes[temp], paths[temp] = box, path
                    changed = True
            self._box_temps, self._box_paths = boxes, paths
        return self._box_temps

    def _kills(self, text):
        """Locals ``text`` moves out of, drops or ends; moving or dropping through a
        Box's pointer (`move ((*_23).1: Box<Pt>)`) leaves the Box partly moved."""
        killed = _kills(text)
        if boxes := self._boxes():
            killed |= {boxes[int(n)] for n in _THROUGH.findall(text) if int(n) in boxes}
        return killed

    def _transfer(self, text, state):
        state = self._terminate(text, state)
        if assigned := _ASSIGN.match(text):
            state |= 1 << int(assigned[1])
        elif (local := self._replacements()[1].get(text)) is not None:
            # The store of `bx.data = new` (or `h.b = new`) makes the Box (the local)
            # whole again only if it was whole before the drop (no field moved out
            # earlier, nor since).
            pending = 1 << self._pending(local)
            if state & pending:
                state |= 1 << local
            state &= ~pending
        return state

    def _terminate(self, text, state):
        """``text``'s kills; the drop of a drop-and-replace remembers (in the local's
        pending bit) whether the local was whole. Any other move or drop of the local
        forgets it: the store then leaves it partly moved."""
        killed = self._kills(text)
        box = self._replacements()[0].get(text)
        whole = box is not None and state & (1 << box)
        state &= ~_mask(killed) & ~_mask(self._pending(local) for local in killed)
        if whole:
            state |= 1 << self._pending(box)
        return state

    def _pending(self, box):
        if self._offset is None:
            self._offset = 1 + max(map(_max_local, self.blocks.values()), default=0)
        return self._offset + box

    def _replacements(self):
        """({drop: local}, {store: local}) for a drop-and-replace: `bx.data = new` or
        `*bx = new` through a Box (`drop(((*_13).1: T))`, then the store to the same
        place through another pointer of the same Box), and `h.b = new` on a local's
        own field or element (`drop((_1.1: Box<Pt>))`, then `(_1.1: Box<Pt>) = move
        _4;` with nothing but storage markers and drop flags between, #108). The store
        fills what the drop emptied."""
        if self._replaced is None:
            boxes, drops, found = self._boxes(), {}, {}
            for block in self.blocks.values():
                dropped = _DROP_PLACE.match(block.terminator)
                target = _target(block)
                if not dropped or target is None or target not in self.blocks:
                    continue
                place = _through_box(dropped[1], boxes, self._box_paths)
                direct = _DIRECT_PLACE.match(dropped[1])
                if place is None and direct is None:
                    continue
                text, passed = self._next_code(target)
                if place is not None:
                    store = _STORE.match(text or "")
                    if store and _through_box(store[1], boxes, self._box_paths) == place:
                        found[text] = place[0]
                        drops[block.terminator] = place[0]
                    continue
                named = {int(n) for n in _MENTIONED.findall(_STRING.sub('""', dropped[1]))}
                if (text or "").startswith(dropped[1] + " = ") and all(
                    (flag := _CONSTANT_STORE.match(statement)) and int(flag[1]) not in named
                    for statement in passed
                ):
                    # The very place, with nothing between that could change what it
                    # names (an index local) or move out of the local: at most drop
                    # flags set (`arr[i] = new`).
                    found[text] = int(direct[1])
                    drops[block.terminator] = int(direct[1])
            self._replaced = (drops, found)
        return self._replaced

    def _next_code(self, number):
        """(the next statement from block ``number`` that is not passed over, the code
        passed over to reach it): storage markers and `goto`s are passed over, and so
        are other assignments (new pointers to a Box, drop flags) and the debug checks
        on them (`assert(copy _44, "misaligned pointer dereference...")`), which run."""
        seen, text, passed = set(), None, []
        while number in self.blocks and number not in seen and text is None:
            seen.add(number)
            for statement, _, _ in self.blocks[number].statements:
                whole = _WHOLE_ASSIGNMENT.match(statement)
                if _NO_CODE.match(statement):
                    continue
                if whole and "->" not in statement:
                    passed.append(statement)
                    continue
                if goto := re.fullmatch(r"goto -> bb([0-9]+);", statement):
                    number = int(goto[1])
                elif statement.startswith("assert(") and (
                    success := re.search(r"\bsuccess: bb([0-9]+)", statement)
                ):
                    passed.append(statement)
                    number = int(success[1])
                else:
                    text = statement
                break
            else:
                break
        return text, passed

    def _aliases(self, local):
        """``local`` and the locals made from it (`&mut (*_1)`, `&((*_1).0: T)`,
        `copy _1`, `move _1`, and so on): pointers into what it points at."""
        if self._alias_edges is not None and self._alias_generics != self.generics:
            # Built while its type parameters were unknown (or others): build it again.
            self._alias_edges = self._alias_cache = None
        if self._alias_cache is None:
            self._alias_cache = {}
        if local in self._alias_cache:
            return self._alias_cache[local]
        if self._alias_edges is None:
            # {local: the locals linked to it}. A value made from another, whole or in
            # part (a reborrow, a copy, a struct or closure holding it, a call's result),
            # may point where it does, and what is stored through either may land in the
            # other (`*sl.get() = &mut *v`, `Hold { slot: &mut slot }`, a closure setting
            # `slot`), so links go both ways; never through a value that cannot hold a
            # pointer (`v.len()`).
            edges = {}

            def link(a, b):
                if a != b and all(
                    not _POINTERLESS.fullmatch(kind := self.types.get(n, ""))
                    # `i64` in a generic body may be a type parameter holding a pointer.
                    or self.generics is None
                    or mentions(kind, self.generics)
                    for n in (a, b)
                ):
                    edges.setdefault(a, set()).add(b)
                    edges.setdefault(b, set()).add(a)

            for block in self.blocks.values():
                for text, _, _ in block.statements:
                    text = _STRING.sub('""', text)
                    assigned = _WHOLE_ASSIGNMENT.match(text) or _PART_ASSIGNMENT.match(text)
                    if assigned:
                        for used in _MENTIONED.findall(assigned[2]):
                            link(int(used), int(assigned[1]))
                    # A call may store any argument through a mutable one
                    # (`mem::swap(&mut slot, &mut w)`, `Cell::set(&c, &mut *v)`).
                    if " -> " in text and not _NOT_A_CALL.match(text):
                        arguments = _CALL_ARGUMENTS.match(text)
                        used = (
                            {int(n) for n in _MENTIONED.findall(arguments[1])}
                            if arguments
                            else set()
                        )
                        for n in used:
                            for writable in used:
                                if re.search(
                                    r"&mut |\*mut |NonNull<|Cell<", self.types.get(writable, "")
                                ):
                                    link(n, writable)
            self._alias_edges, self._alias_generics = edges, self.generics
        found, pending = {local}, [local]
        while pending:
            for made in self._alias_edges.get(pending.pop(), ()):
                if made not in found:
                    found.add(made)
                    pending.append(made)
        self._alias_cache[local] = found
        return found

    def drops_through(self, local):
        """True if the body drops what ``local`` or an alias of it points at anywhere
        (`drop((*_1))`, `drop(((*_8).1: T))`)."""
        if self._dropped is None:
            self._dropped = {
                int(found[1])
                for block in self.blocks.values()
                if (found := _DROP_THROUGH.match(block.terminator))
            }
        return bool(self._dropped & self._aliases(local))

    def dropped_through(self, local, start):
        """True if some path into ``start`` (block, index) drops what ``local`` points at
        (`drop((*_1))`, the first half of `*v = new`) with no write through it since:
        its target is a dropped value there."""
        # Drops and stores through a reborrow or a copy of it (`let w = &mut *v;`) count.
        names = "|".join(str(alias) for alias in sorted(self._aliases(local)))
        if not self.drops_through(local):
            return False
        dropped = re.compile(rf"^drop\(\(*\*\(*_(?:{names})\b")
        written = re.compile(rf"^\(+\*\(*_(?:{names})\b[^=]*= |^_{local} = ")
        predecessors = self.predecessors()
        pending, seen = [start], set()
        while pending:
            number, index = pending.pop()
            if (number, index) in seen or number not in self.blocks:
                continue
            seen.add((number, index))
            ended = False
            for text, _, _ in reversed(self.blocks[number].statements[:index]):
                if dropped.match(text):
                    return True
                if written.match(text):
                    ended = True
                    break
            if not ended:
                pending.extend(
                    (other, len(self.blocks[other].statements))
                    for other in {*predecessors.get(number, ()), *self._flows_into(number)}
                )
        return False

    def _flows(self, number):
        """The blocks control flows to from ``number``: a suspending block's flows to the
        block resuming it, which the dispatch at bb0 jumps to only on a later poll."""
        if number in self.resumes:
            return (self.resumes[number],)
        successors = self.blocks[number].successors
        if number == 0 and self.resumes:
            resumed = set(self.resumes.values())
            return tuple(target for target in successors if target not in resumed)
        return successors

    def _flows_into(self, number):
        """The blocks control flows into ``number`` from (``_flows``, reversed)."""
        if self._into is None:
            found = {}
            for other in self.blocks:
                for target in self._flows(other):
                    found.setdefault(target, set()).add(other)
            self._into = found
        return self._into.get(number, ())

    def _lent(self):
        """A coroutine's locals that only ever hold what its caller lent it: an `async
        fn`'s parameters, which its state keeps from the call (`_3 = move ((*_34).0:
        &mut Vec<Pt>)`), and the poll's `&mut Context`."""
        if self._lent_cache is None:
            found = set()
            if self.coroutine is not None:
                state = self.coroutine
                # A field written in the body (`((*_34).0: &Pt) = &b`, `&mut` to it) may
                # hold what the body made, not what the caller lent.
                assigned = re.compile(rf"^\(*\(\(\*_{state}\)\.([0-9]+): .* = ")
                borrowed = re.compile(
                    rf"&(?:mut |raw const |raw mut )\(*\(\(\*_{state}\)\.([0-9]+): "
                )
                written = set()
                for block in self.blocks.values():
                    for text, _, _ in block.statements:
                        text = _STRING.sub('""', text)
                        written.update(int(number) for number in borrowed.findall(text))
                        if field := assigned.match(text):
                            written.add(int(field[1]))
                kept = re.compile(rf"(?:move|copy) \(\(\*_{state}\)\.([0-9]+): .+\)")
                found = {
                    local
                    for local, values in self._assignments().items()
                    if local != 2
                    and all(
                        (held := kept.fullmatch(value)) and int(held[1]) not in written
                        for value in values
                    )
                }
                # The poll's context: on resuming, rustc moves it out and back
                # (`_32 = move _2; _2 = move _32;`), which leaves the same pointer.
                assigned = self._assignments()
                if re.fullmatch(
                    r"&mut (?:(?:std|core)::task::)?Context<'_>", self.types.get(2, "")
                ) and all(
                    (back := re.fullmatch(r"move _([0-9]+)", value))
                    and assigned.get(int(back[1])) == ["move _2"]
                    for value in assigned.get(2, [])
                ):
                    found.add(2)
                self._written_fields = frozenset(written)
            self._lent_cache = frozenset(found)
        return self._lent_cache

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
            pending.extend(self._flows(number))
        # Each local's bit, and a pending bit per Box local above them (`_pending`).
        top = (1 << (2 * (1 + max(map(_max_local, self.blocks.values()), default=0)))) - 1
        entry = {number: top for number in reachable}
        entry[0] = _mask(self.arguments)
        # Across a suspension, only what the state saves (and the next poll's arguments).
        kept = _mask(self.saved | self.arguments)
        work = sorted(reachable, reverse=True)  # every block is processed at least once
        queued, steps = set(work), 0
        limit = max(MAX_ITERATIONS, STEPS_PER_BLOCK * len(reachable))
        while work:
            steps += 1
            if steps > limit:
                self._entry = {}
                return None
            number = work.pop()
            queued.discard(number)
            block = self.blocks[number]
            state = entry[number]
            for text, _, _ in block.statements[:-1]:
                state = self._transfer(text, state)
            # The terminator moves and drops on every edge; a call sets its result only
            # on its return edge.
            state = self._terminate(block.terminator, state)
            for target in self._flows(number):
                out = state if number not in self.resumes else state & kept
                if target == block.returns:
                    out |= 1 << int(_ASSIGN.match(block.terminator)[1])
                merged = entry[target] & out
                if merged != entry[target]:
                    entry[target] = merged
                    if target not in queued:
                        queued.add(target)
                        work.append(target)
        self._entry = entry
        return entry


_CLOSURE_SEGMENT = re.compile(r"_?\{+closure(?:#[0-9]+)?\}+")
# What rustc's debug info names a coroutine by its kind; its MIR path says `{closure#N}`
# with the same disambiguator (#116).
_COROUTINE_NAME = re.compile(
    r"\{(?:async_fn|async_block|async_closure|coroutine|gen_fn|gen_block|gen_closure"
    r"|async_gen_fn|async_gen_block|async_gen_closure)#([0-9]+)\}"
)


def _mir_name(name):
    """The MIR's name for an IR function named ``name``: `{async_fn#0}` -> `{closure#0}`."""
    found = _COROUTINE_NAME.fullmatch(name)
    return f"{{closure#{found[1]}}}" if found else name


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
    # `const "_3"` names no local.
    text = _STRING.sub('""', text)
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


@dataclass(frozen=True)
class StopBlock:
    """Where a stop's pc is (#115): the names of the variables each lexical block
    declares (sorted), from the innermost DWARF block holding the pc out to the
    function's own scope; and how many of the function's blocks have each such chain."""

    chain: tuple
    chains: dict


def stop_block(blocks, pc):
    """The ``StopBlock`` of ``pc`` (unslid) in a function's ``CodeBlock``s, or None where
    the pc is outside its own code, in an inlined call, or in two blocks at once."""
    if not blocks or type(pc) is not int or blocks[0].parent is not None:
        return None

    def holds(block):
        return any(low <= pc < high for low, high in block.ranges)

    if not holds(blocks[0]):
        return None
    children = {}
    for index, block in enumerate(blocks[1:], 1):
        children.setdefault(block.parent, []).append(index)
    current = 0
    for _ in range(len(blocks)):
        inside = [index for index in children.get(current, ()) if holds(blocks[index])]
        if not inside:
            break
        if len(inside) > 1 or blocks[inside[0]].inlined:
            return None
        current = inside[0]

    def chain(index):
        names = []
        while index is not None:
            names.append(blocks[index].names)
            index = blocks[index].parent
        return tuple(names)

    counts = {}
    for index, block in enumerate(blocks):
        if not block.inlined:
            counts[chain(index)] = counts.get(chain(index), 0) + 1
    return StopBlock(chain(current), counts)


@dataclass(frozen=True)
class LineFacts:
    column: int | None
    function: object  # the IrFunction
    inlined: frozenset  # columns of the calls inlined on the line
    macros: frozenset  # columns where a macro's path starts on the line
    to_bytes: object  # a column of the line in characters (the MIR's) to bytes (DWARF's)
    known: bool = True  # the line's text is known: ``macros`` holds all its macros


# Where a macro's path starts (`println!(`, `std::vec![`); `if !(x)` too, which only
# widens what a column can be.
_MACRO_CALL = re.compile(r"(?<![\w:])((?:::)?(?:\w+::)*\w+)\s*!\s*[(\[{]")


def _binding_scopes(body, local):
    return [b[4] for found in body.bindings.values() for b in found if b[0] == local]


def _encloses(body, outer, inner):
    for _ in range(4096):
        if inner == outer:
            return True
        if inner is None:
            return False
        inner = body.scopes.get(inner)
    return False


def _through_box(place, boxes, paths):
    """(Box local, the place with the pointer named by its Box) for a place through a
    pointer made from a Box (`((*_13).1: String)`), else None."""
    found = re.search(r"\(\*_([0-9]+)\)", place)
    if found is None or int(found[1]) not in boxes or not place.lstrip("(").startswith("*"):
        return None
    temp = int(found[1])
    return boxes[temp], place[: found.start()] + f"(*{paths[temp]})" + place[found.end() :]


def _span(block, index):
    try:
        return block.spans[index]
    except IndexError:
        return None


def _call(block, index):
    """True if statement ``index`` is the block's terminator and a call."""
    if index != len(block.statements) - 1 or "->" not in block.terminator:
        return False
    return not re.match(
        r"(?:drop|assert|switchInt|goto|falseEdge|falseUnwind|yield)\b", block.terminator
    )


def _target(block):
    """The block a call or drop returns to, if any. A lone `-> bbN` is a drop's return
    (in cleanup code) but a diverging call's unwind."""
    edges = block.terminator.rpartition("->")[2]
    if found := _RETURN_EDGE.search(edges):
        return int(found[1])
    found = re.fullmatch(r" bb([0-9]+);", edges)
    return int(found[1]) if found and _DROP.match(block.terminator) else None


def _scope(block, index):
    try:
        return block.scopes[index]
    except IndexError:
        return None


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
    lines, saved = _saved_locals(body, lines, start)
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
            if not whole:
                body.places[(debug[1], debug[4], int(debug[5]), int(debug[3]))] = debug[2]
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
            if "(inlined " in line:
                body.inlined_scopes.add(int(scope[1]))
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
        full = _FULL_SPAN.match(statement[3])
        block.spans.append((int(full[3]), int(full[4]), int(full[5])) if full else None)
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
    if body.coroutine is not None and body.coroutine >= 0:
        body.types.update(saved)
        body.saved = frozenset(saved)
        _suspensions(body)
    return body


# `_34 = copy (_1.0: &mut {async fn body of f()})`: bb0 of a coroutine's body takes the
# pointer to its state from its pinned `self`.
_STATE = re.compile(r"^        _([0-9]+) = copy \(_1\.0: &mut \{.+\}\); +// ")


def _saved_locals(body, lines, start):
    """A coroutine's body (an `async fn`'s or block's, #116) keeps the locals it holds
    across a suspension in its state, as fields of the suspension's variant (`(((*_34)
    as variant#3).0: &Pt)`), which the debug info binds and the code reads and writes as
    it would the local. Each is renamed a local of its own, numbered past the body's,
    so that it is judged as one: (the lines renamed, {local: its type})."""
    if not re.match(r"_1: (?:(?:std|core)::pin::)?Pin<&mut \{", _HEADER.match(lines[start])[2]):
        return lines, {}
    opened = next((i for i in range(start, len(lines)) if lines[i] == "    bb0: {"), None)
    state = (
        _STATE.match(lines[opened + 1]) if opened is not None and opened + 1 < len(lines) else None
    )
    if state is None:
        # A coroutine whose state is not found: what it keeps is not followed.
        body.coroutine, body.supported = -1, False
        return lines, {}
    body.coroutine = number = int(state[1])
    place = re.compile(rf"\(\(\(\*_{number}\) as variant#([0-9]+)\)\.([0-9]+): ")
    last = max((int(n) for line in lines[start:] for n in _MENTIONED.findall(line)), default=0)
    named, saved, renamed = {}, {}, list(lines[: start + 1])
    for line in lines[start + 1 :]:
        for found in reversed(list(place.finditer(line))):
            depth, end = 0, None
            for index in range(found.start(), len(line)):
                depth += {"(": 1, ")": -1}.get(line[index], 0)
                if depth == 0:
                    end = index
                    break
            if end is None:
                body.supported = False
                break
            kind = line[found.end() : end]
            key = (int(found[1]), int(found[2]))
            if key not in named:
                last += 1
                named[key] = last
            local = named[key]
            if saved.setdefault(local, kind) != kind:
                body.supported = False
            line = f"{line[: found.start()]}_{local}{line[end + 1 :]}"
        # Any other place in a variant of the state is not understood.
        if f"(*_{number}) as variant#" in line:
            body.supported = False
        renamed.append(line)
    return renamed, saved


def _suspensions(body):
    """Each block that suspends the coroutine (sets its state's variant to a suspension,
    3 or more, and returns), with the block its dispatch resumes it at."""
    entry = body.blocks.get(0)
    state = body.coroutine
    dispatch = re.fullmatch(
        r"switchInt\(move _([0-9]+)\) -> \[(.*)\];", entry.terminator if entry else ""
    )
    texts = [text for text, _, _ in entry.statements] if entry else []
    if (
        dispatch is None
        or len(texts) != 3
        or texts[1] != f"_{dispatch[1]} = discriminant((*_{state}));"
    ):
        body.supported = False
        return
    targets = {
        int(value): int(target)
        for value, target in re.findall(r"\b([0-9]+): bb([0-9]+)", dispatch[2])
    }
    for number, block in body.blocks.items():
        if block.terminator != "return;":
            continue
        texts = [text for text, _, _ in block.statements[:-1] if not _NO_CODE.match(text)]
        found = re.fullmatch(
            rf"discriminant\(\(\*_{state}\)\) = ([0-9]+);", texts[-1] if texts else ""
        )
        if found is None or int(found[1]) < 3:
            continue
        if int(found[1]) not in targets:
            body.supported = False
            return
        body.resumes[number] = targets[int(found[1])]


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
    r"|DIFile|DINamespace|DICompositeType|DITemplateTypeParameter|DITemplateValueParameter"
    r"|DILocalVariable)\((.*)\)\s*$"
)
_METAFIELDS = {
    key: re.compile(rf'(?:^|, ){key}: ("(?:[^"\\]|\\.)*"|![0-9]+|[0-9]+)')
    for key in (
        "name",
        "linkageName",
        "scope",
        "file",
        "line",
        "column",
        "inlinedAt",
        "filename",
        "templateParams",
    )
}
# A short metadata list (`!2313 = !{!2314}`): a subprogram's template parameters.
_META_LIST = re.compile(r"^(![0-9]+) = (?:distinct )?!\{((?:![0-9]+(?:, )?){0,64})\}\s*$")
# An instruction that surely leaves code at its location: a call (not an LLVM intrinsic,
# which may lower to nothing) or a conditional branch.
_IR_CALL = re.compile(r"^\s*(?:%\S+ = )?(?:tail |musttail |notail )?(?:call|invoke) (?!.*@llvm\.)")
_IR_BRANCH = re.compile(r"^\s*(?:br i1 |switch )")
# Code opt-level 0 surely emits at the instruction's location (#115): a branch on a value
# (not a constant), a store of bytes (not of nothing, nor of an undefined value).
_IR_SURE_BRANCH = re.compile(r"^\s*br i1 %")
_IR_STORE = re.compile(r"^\s*store (?:volatile )?(.+) (\S+), ptr ")


def _stores_bytes(found):
    kind, value = found[1].strip(), found[2]
    return (
        value not in ("undef", "poison")
        and kind not in ("{}", "{ }", "<{}>", "<{ }>")
        and not kind.startswith(("[0 x", "<0 x"))
    )


@dataclass
class IrFunction:
    name: str
    parent: str | None
    file: str
    blocks: dict  # {label: {line, ...}} for code of this function (not inlined)
    preds: dict  # {label: [label, ...]}
    line: int | None = None  # the DISubprogram's: where the function is declared
    # {label: ((line, column) of each own call in order, of each conditional branch)}:
    # a MIR block's call or branch is its terminator. {line: {column}} of the calls LLVM
    # inlined (where their code's `inlinedAt` chain leaves this function).
    events: dict = field(default_factory=dict)
    inlined: dict = field(default_factory=dict)
    # Lines LLVM inlined code of this function into other functions on (#116): a frame
    # with this hash may be such a copy (`#[inline(always)]`), whose layout ``blocks``
    # does not describe. ``inline_only``: it has no IR function of its own at all.
    copies: frozenset = frozenset()
    inline_only: bool = False
    # The names of the namespaces it is in, outermost first (its crate's, then its
    # modules', functions', closures'): `parent` is the last.
    ancestors: tuple = ()
    # The names of the type parameters this instantiation fills (a generic function's,
    # its impl's or its enclosing function's, for a closure): () for none, None when it
    # is generic in a way not understood (a const parameter). Its MIR body is generic,
    # so any type it declares may name one of them, whatever the name (`i8`).
    generics: tuple | None = ()
    # Its own lexical blocks (#115), each named as the DWARF names a block holding a stop
    # (`StopBlock.chain`): {chain: how many blocks with it have code, LLVM emitting a
    # DWARF block for each}, {(line, chain): how many have code of the line}, {label:
    # ((line, chain, the calls before it in the label), ...) for each instruction that
    # surely leaves code there: a call, a store of bytes, a branch on a value}, and
    # {label: its calls}. ``scoped``: every block's names are known.
    chains: dict = field(default_factory=dict)
    line_chains: dict = field(default_factory=dict)
    sure: dict = field(default_factory=dict)
    calls: dict = field(default_factory=dict)
    scoped: bool = False

    def agrees(self, block, line):
        """True if the IR's blocks with the names of a stop's DWARF block (``block``, a
        ``StopBlock``) are as many as the DWARF's, and just one has code of ``line``:
        that one is the stop's."""
        return (
            self.scoped
            and block.chains.get(block.chain) == self.chains.get(block.chain)
            and self.line_chains.get((line, block.chain)) == 1
        )

    def labeled(self):
        """MIR blocks that have an IR block of their own (`start` is bb0)."""
        if getattr(self, "_labeled", None) is None:
            self._labeled = {
                0 if label == "start" else int(label[2:])
                for label in self.blocks
                if label == "start" or re.fullmatch(r"bb[0-9]+", label)
            }
        return self._labeled

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
    raw, meta, current, pending = [], {}, None, ""
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if line.startswith("!"):
                if found := _META_LIST.match(line):
                    meta[found[1]] = ("list", {"items": found[2].split(", ") if found[2] else []})
                elif found := _META.match(line):
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
            if _IR_CALL.match(line):
                kind = "c"
            elif _IR_BRANCH.match(line):
                kind = "B" if _IR_SURE_BRANCH.match(line) else "b"
            elif (store := _IR_STORE.match(line)) and _stores_bytes(store):
                kind = "s"
            else:
                kind = ""
            if dbg := _DBG.search(line):
                # An `invoke`'s location is on its `to label ...` line.
                current[2].setdefault(current[4][0], []).append((dbg[1], kind or pending))
                pending = ""
            elif kind == "c" and not re.search(r"\binvoke ", line):
                # A call with no location: still one of the block's calls.
                current[2].setdefault(current[4][0], []).append((None, kind))
            elif kind in ("c", "b", "B"):
                pending = kind
    wanted = set(files)
    # {lexical block or subprogram: the names of the variables it declares} (#115).
    names = {}
    for kind, fields in meta.values():
        if kind == "DILocalVariable" and "name" in fields:
            block = _lexical_block(meta, fields.get("scope"))
            if block is not None:
                names.setdefault(block, []).append(fields["name"])
    # {subprogram: lines} of code LLVM inlined from each function into another, in the
    # inlined function's own file (#116).
    copies = {}
    for _, _, blocks, _, _ in raw:
        for refs in blocks.values():
            for ref, _ in refs:
                kind, location = meta.get(ref, (None, {}))
                if kind != "DILocation" or "inlinedAt" not in location:
                    continue
                subprogram = _subprogram(meta, location.get("scope"))
                number = int(location.get("line") or 0)
                if (
                    subprogram is not None
                    and number > 0
                    and _file(meta, location.get("scope")) == _file(meta, subprogram)
                ):
                    copies.setdefault(subprogram, set()).add(number)
    functions = {}
    for hashed, subprogram, blocks, preds, _ in raw:
        kind, fields = meta.get(subprogram, (None, {}))
        if hashed is None or kind != "DISubprogram":
            continue
        file = _file(meta, subprogram)
        if file not in wanted:
            continue
        lines, events, inlined = {}, {}, {}
        # #115: the named blocks with code (an inlined call's site counts), those with
        # code of each line, and each label's sure instructions and calls.
        active, line_blocks, sure, calls = set(), {}, {}, {}
        for label, refs in blocks.items():
            lines[label] = set()
            found_events = ([], [])
            found_sure, before = [], 0
            for ref, instruction in refs:
                kind, location = meta.get(ref, (None, {}))
                if kind != "DILocation":
                    before += instruction == "c"
                    continue
                if "inlinedAt" in location:
                    site = _call_site(meta, location)
                    if site is not None:
                        active.add(_named_block(meta, names, site.get("scope")))
                    if site is not None and _file(meta, site.get("scope")) == file:
                        found = (int(site.get("line") or 0), int(site.get("column") or 0))
                        inlined.setdefault(found[0], set()).add(found[1])
                    continue
                calls_before = before
                before += instruction == "c"
                block = _named_block(meta, names, location.get("scope"))
                active.add(block)
                if _file(meta, location.get("scope")) != file or not location.get("line"):
                    continue
                number = int(location["line"])
                if number > 0:
                    lines[label].add(number)
                    line_blocks.setdefault(number, set()).add(block)
                    if instruction in ("c", "B", "s"):
                        found_sure.append((number, block, calls_before))
                slot = {"c": 0, "b": 1, "B": 1}.get(instruction)
                if slot is not None:
                    found_events[slot].append((number, int(location.get("column") or 0)))
            events[label] = (tuple(found_events[0]), tuple(found_events[1]))
            sure[label], calls[label] = found_sure, before
        chain = _chains(meta, names, active)
        parent = meta.get(fields.get("scope"), (None, {}))[1].get("name")
        functions[hashed] = IrFunction(
            re.sub(r"<.*>$", "", fields.get("name", "")),
            parent,
            file,
            lines,
            preds,
            int(fields["line"]) if str(fields.get("line", "")).isdigit() else None,
            events,
            inlined,
            frozenset(copies.get(subprogram, ())),
            ancestors=_namespaces(meta, fields.get("scope")),
            generics=_generics(meta, fields),
            chains=_count(chain[block] for block in chain) if chain else {},
            line_chains=_count(
                (number, chain[block]) for number, found in line_blocks.items() for block in found
            )
            if chain
            else {},
            sure={
                label: tuple((number, chain[block], at) for number, block, at in found)
                for label, found in sure.items()
            }
            if chain
            else {},
            calls=calls,
            scoped=chain is not None,
        )
    # A function LLVM inlined everywhere (`#[inline(always)]`, even at opt-level 0) has
    # no IR function, yet its frames carry its own hash; its MIR body is the one before
    # inlining.
    for subprogram, lines in copies.items():
        kind, fields = meta.get(subprogram, (None, {}))
        hashed = _LEGACY_HASH.search(fields.get("linkageName") or "")
        if kind != "DISubprogram" or hashed is None or hashed[1] in functions:
            continue
        file = _file(meta, subprogram)
        if file not in wanted:
            continue
        functions[hashed[1]] = IrFunction(
            re.sub(r"<.*>$", "", fields.get("name", "")),
            meta.get(fields.get("scope"), (None, {}))[1].get("name"),
            file,
            {},
            {},
            int(fields["line"]) if str(fields.get("line", "")).isdigit() else None,
            copies=frozenset(lines),
            inline_only=True,
            ancestors=_namespaces(meta, fields.get("scope")),
            generics=_generics(meta, fields),
        )
    return functions


# A const generic's value as rustc names an instantiation (`sum<3>`, `cgt<u8, 2>`).
# A char's is a hash (`ch<{CONST#27d96aba666aa2f7}>` for `ch::<'a'>`, rustc 1.96).
_CONST_ARGUMENT = re.compile(r"-?[0-9]+|true|false|\{CONST#[0-9a-f]+\}")


def _generics(meta, fields):
    """A DISubprogram's type parameters, by name (see IrFunction.generics).

    rustc lists each type parameter (its impl's and enclosing function's too) and never
    a const one, and its name gives every argument (`cgt<u8, 2>`) but no lifetime: the
    arguments beyond the listed types must all be const values, which are no types.
    """
    kind, listed = meta.get(fields.get("templateParams"), (None, {}))
    items = listed.get("items", []) if kind == "list" else None
    named = fields.get("name", "")
    arguments = _name_arguments(named)
    if items is None:
        # No list read: generic only if its name says so (`show<u64>`), or if the name
        # cannot be read.
        return () if arguments == [] else None
    names = []
    for item in items:
        kind, parameter = meta.get(item, (None, {}))
        if kind != "DITemplateTypeParameter" or not parameter.get("name"):
            return None
        names.append(parameter["name"])
    if arguments is None:
        return None
    consts = sum(1 for argument in arguments if _CONST_ARGUMENT.fullmatch(argument))
    if len(arguments) - len(names) != consts and arguments:
        return None
    return tuple(names)


def _name_arguments(name):
    """The generic arguments a DISubprogram's name ends with (`cgt<u8, 2>`: ["u8", "2"]),
    [] for none, None if they cannot be read."""
    if not name.endswith(">"):
        return []
    # `->` would close a bracket (`show<fn(u8) -> u8>`).
    text, depth = name.replace("->", "  "), 0
    for at in range(len(text) - 1, -1, -1):
        depth += {">": 1, "<": -1}.get(text[at], 0)
        if depth == 0:
            inner = name[at + 1 : -1]
            break
    else:
        return None
    parts, depth, start = [], 0, 0
    masked = inner.replace("->", "  ")
    for at, char in enumerate(masked):
        if char in "<([{":
            depth += 1
        elif char in ">)]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(inner[start:at].strip())
            start = at + 1
    parts.append(inner[start:].strip())
    return None if depth or not all(parts) else parts


def mentions(kind, names):
    """Whether the MIR type ``kind`` names any of ``names`` (type parameters) as a type."""
    return any(
        re.search(rf"(?<![\w:]){re.escape(name)}(?![\w])", str(kind or "")) for name in names
    )


def _namespaces(meta, scope):
    """The names of the namespaces ``scope`` is in, outermost first."""
    found = []
    for _ in range(256):
        kind, fields = meta.get(scope, (None, {}))
        if kind not in ("DINamespace", "DICompositeType") or "name" not in fields:
            break
        found.append(fields["name"])
        scope = fields.get("scope")
    return tuple(reversed(found))


def _subprogram(meta, scope):
    """The DISubprogram a scope (a lexical block in it) belongs to, if any."""
    for _ in range(256):
        kind, fields = meta.get(scope, (None, {}))
        if kind == "DISubprogram":
            return scope
        if kind not in ("DILexicalBlock", "DILexicalBlockFile"):
            return None
        scope = fields.get("scope")
    return None


def _lexical_block(meta, scope):
    """The DILexicalBlock or DISubprogram ``scope`` is (a DILexicalBlockFile only gives
    its block another file), or None."""
    for _ in range(256):
        kind, fields = meta.get(scope, (None, {}))
        if kind in ("DILexicalBlock", "DISubprogram"):
            return scope
        if kind != "DILexicalBlockFile":
            return None
        scope = fields.get("scope")
    return None


def _named_block(meta, names, scope):
    """The block DWARF puts code of ``scope`` in: LLVM emits no DWARF block for a lexical
    block that declares no variable, so its code is its parent's."""
    for _ in range(4096):
        block = _lexical_block(meta, scope)
        kind, fields = meta.get(block, (None, {}))
        if kind != "DILexicalBlock" or names.get(block):
            return block
        scope = fields.get("scope")
    return None


def _chains(meta, names, blocks):
    """{block: its chain}: the sorted names of the variables it and each block around it
    declare, out to its function's (`StopBlock.chain`), for ``blocks`` and the blocks
    around them; None if one is not a function's lexical block."""
    found = {}
    for block in blocks:
        path = []
        for _ in range(4096):
            kind, fields = meta.get(block, (None, {}))
            if kind not in ("DILexicalBlock", "DISubprogram"):
                return None
            path.append(block)
            if kind == "DISubprogram":
                break
            block = _named_block(meta, names, fields.get("scope"))
        else:
            return None
        chain = ()
        for block in reversed(path):
            chain = (tuple(sorted(names.get(block, ()))), *chain)
            found[block] = chain
    return found


def _count(items):
    counts = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts


def _call_site(meta, location):
    """The DILocation in the function itself that inlined code at ``location`` was called
    from: the last of its `inlinedAt` chain."""
    for _ in range(256):
        parent = location.get("inlinedAt")
        if parent is None:
            return location
        kind, location = meta.get(parent, (None, {}))
        if kind != "DILocation":
            return None
    return None


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
        self._unanalyzed = {}
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
        # A function inlined everywhere has only the lines of its inlined copies (#116).
        lines = (function.copies if function.inline_only else function.lines()) if function else ()
        if lines:
            # A coroutine's debug info names its kind (`{async_fn#0}`, `{async_block#1}`);
            # its MIR names it as a closure, by the same disambiguator (#116).
            name = _mir_name(function.name)
            # Positive identification, never by elimination: exactly one body of this
            # name and file (and parent) has a span on the line the function is declared
            # on. Twins a local macro generates share that line (the `fn` is in the
            # macro), so they are refused rather than told apart by their other lines.
            candidates = [
                body
                for body in self.bodies
                if body.file == function.file
                and body.name == name
                and _same_path(body, function)
                and function.line in body.lines
            ]
            if len(candidates) == 1:
                chosen = next(i for i, b in enumerate(self.bodies) if b is candidates[0])
                # A line with this function's code that only other bodies have spans on
                # is theirs: the candidate is a twin whose `fn` line merely matches
                # (macro-generated, one with cleanup on that line, one without).
                foreign = any(
                    chosen not in owners
                    for number in lines
                    if (owners := self._owners.get((function.file, number)))
                )
                if foreign or not function.labeled() <= set(candidates[0].blocks):
                    # A twin: the one generated from it has spans on those lines.
                    candidates = [
                        *candidates,
                        *(
                            self.bodies[index]
                            for number in lines
                            for index in sorted(self._owners.get((function.file, number), ()))
                            if self.bodies[index].name == name
                        ),
                    ]
            elif not candidates and function.inline_only:
                # Not identified, and no IR describes its code either: its borrowing
                # bindings are those of the bodies it may be (#116).
                self._unanalyzed[hashed] = [
                    body for body in self.bodies if body.file == function.file and body.name == name
                ]
            found = candidates[0] if len(candidates) == 1 else None
            if found is not None and not (
                found.supported and found._dataflow() is not None
                if function.inline_only
                # A coroutine whose state is not understood can hold anything (#116).
                else (found.supported or found.coroutine is None) and _covered(found, function)
            ):
                # Identified, but its layout or dataflow is not understood.
                found.generics = function.generics
                self._unanalyzed[hashed] = [found]
                found = None
            elif len(candidates) > 1:
                # Twins (a macro's methods): one of them, not told apart.
                self._unanalyzed[hashed] = list({id(body): body for body in candidates}.values())
        if found is not None:
            found.generics = function.generics
        self._matched[hashed] = found
        return found

    def _visible(self, body, line, path, block=None, site=None, bindings=None):
        """The scopes a binding shown at ``line`` can be in, or None to keep them all.
        A line that may invoke a macro runs code whose spans are in another file
        (`eprintln!`'s, collapsed onto the line), in scopes no statement of this file
        there shows: the stop can be in one of those. Where the DWARF lexical block the
        stop's pc is in (``block``) is told apart, only it and its enclosing scopes
        (#115): by the stop line's places (of ``site``) where several scopes have its
        names, else every scope with them. A name none of whose ``bindings`` are there
        (lldb-dap shows none at the stop) is judged as without the block."""
        visible = self._line_visible(body, line, path)
        scopes = self._block_scopes(site, line, block) if block is not None else None
        if scopes is None:
            return visible
        narrowed = scopes if visible is None else (visible & scopes) or visible
        if bindings is not None and not any(b[4] in narrowed for b in bindings):
            return visible
        return narrowed

    def _block_scopes(self, site, line, block):
        if site is None:
            return None
        body, function, blocks, shared = site
        named = body.chain_scopes(block, function)
        if named is None:
            return None
        text = self.texts.get(function.file)
        if function.inline_only or not (self.precise or not may_invoke_macro(text, line)):
            return named
        scope = body.stop_scope(
            blocks, line, function.file, may_invoke_macro(text, line), block, shared, function
        )
        if scope not in named:
            return named
        found = set()
        for _ in range(4096):
            if scope is None:
                break
            found.add(scope)
            scope = body.scopes.get(scope)
        return found

    def _line_visible(self, body, line, path):
        if line is None:
            return None
        # The stop's own file: a macro defined in another module, or `include!`d code,
        # stops in this frame at another file's line.
        shown = str(path or "").replace("\\", "/")
        matched = [name for name in self.texts if shown == name or shown.endswith("/" + name)]
        if len(matched) != 1 or may_invoke_macro(self.texts[matched[0]], line):
            return None
        return body.visible_scopes(line, path)

    def _local(self, body, variable_name, line=None, path=None, block=None, site=None):
        name, at, declared = str(variable_name).partition(" @ ")
        where = None
        if at:
            file, _, number = declared.rpartition(":")
            if not number.isdigit():
                return None
            where = (file, int(number))
        bindings = [
            b
            for b in body.bindings.get(name, [])
            if where is None or (Path(b[1]).name, b[2]) == where
        ]
        return body.local(name, where, self._visible(body, line, path, block, site, bindings))

    def declared_type(self, hashed, variable_name, line=None, path=None):
        """The MIR type of the frame's local ``variable_name`` (`std::vec::Vec<usize>`),
        which LLDB's C-style names cannot tell apart (u64 and usize); else None. With the
        stop's ``line`` and ``path``, a name bound more than once is the binding in scope
        there, if only one is."""
        body = self.body(hashed)
        local = self._local(body, variable_name, line, path) if body is not None else None
        kind = None if local is None else body.types.get(local)
        # In a generic body any type may name a type parameter, whatever its name
        # (`fn show<i8>(p: Pair<i8>)` called with chars): only the others are this frame's.
        generics = self.type_parameters(hashed)
        return None if generics is None or mentions(kind, generics) else kind

    def type_parameters(self, hashed):
        """The type parameters of the frame's function (see IrFunction.generics): () for
        a function that has none, None if unknown."""
        function = self.functions.get(hashed)
        return None if function is None else function.generics

    def binding_types(self, hashed, variable_name, path=None, line=None, closure=None, frame=None):
        """The MIR type of every binding lldb-dap could be showing as ``variable_name``
        (None for one whose type is not written out), in the frame's body. A frame
        whose body is not matched for proofs (an unmangled function) takes its types
        from the bodies whose code covers the stop line, of the frame's own shape when
        one is (``frame``, lldb-dap's name: `outer::{{closure}}::{{closure}}` is the
        MIR's `outer::{closure#0}::{closure#0}`). Empty when no body binds the name."""
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

    def reference_valid(
        self, hashed, variable_name, line, path, breakpoint=False, column=None, block=None
    ):
        """For the frame's local ``variable_name`` whose type borrows (a reference, `&str`,
        a tuple, `Vec` or closure holding one, a raw pointer): True if what it points into
        is shown to stand at ``line`` of ``path``, False if not (#110), None if this does
        not apply (another type, the site or the local not understood). ``breakpoint``:
        the stop is a line breakpoint's, at ``column`` (lldb-dap's, in bytes) if known.

        True where, from every place the stop can be on the line, the code still uses it
        ahead, or used it last with nothing since that can move, drop or write (no call,
        no drop): the borrow checker keeps what it borrows alive and unchanged until its
        last use. Past a call or drop after its last use, what it pointed into may be
        moved, dropped or reallocated, which no simpler reading of the MIR can rule out
        (interior mutability, `&mut` aliases, loops), so then it is False. Also True: a
        reference only ever made into a shared reference argument (the caller's loan
        freezes it for the call) or into a variable whose scope encloses its own and
        that is initialized at the stop (it points at that variable's storage), a fn
        pointer, a reference argument never reassigned (its target outlives the call),
        and a local only ever assigned constants (`"text"`, a promoted `&[1, 2]`); a
        local never borrowed itself, for the last two. A raw pointer keeps no borrow, so
        it is False (one inside a tuple reads with the tuple). A value not yet assigned
        (its declaration line) is False. A name bound more than once that the stop
        cannot tell apart is judged by every binding. ``block`` (a ``StopBlock``): the
        DWARF lexical block the stop's pc is in, if known (#115)."""
        site = self._site(hashed, line, path)
        if site is None:
            return self._elsewhere(hashed, variable_name, path)
        body, function = site[0], site[1]
        local = self._local(body, variable_name, line, path, block, site)
        captures = []
        if local is not None:
            candidates = [local]
        else:
            name, at, declared = str(variable_name).partition(" @ ")
            bindings = body.bindings.get(name, [])
            if at:
                file, _, number = declared.rpartition(":")
                bindings = [b for b in bindings if Path(b[1]).name == file and str(b[2]) == number]
            bindings = body.in_scopes(
                bindings, self._visible(body, line, path, block, site, bindings)
            )
            candidates = [b[0] for b in bindings]
            # A capture (`(*((*_1).0: &mut Holder))`) has no local of its own: what it
            # points at is dropped through the environment it is reached by. A value a
            # coroutine's state holds itself (`((*_34).1: i64)`, an `async fn`'s
            # parameter) points nowhere unless its type borrows (#116).
            captures = [
                int(root[1])
                for b in bindings
                if b[0] is None
                and (place := body.places.get((name, b[1], b[2], b[4]), ""))
                and (
                    body.coroutine is None
                    or place.startswith("(*(")
                    or _BORROWING.search(b[3] or "&")
                )
                and (root := _ANY_LOCAL.search(place))
            ]
        # A breakpoint's place in the line comes from the IR function's layout, which an
        # inlined copy of it (#116) need not share: then any place of the line counts.
        facts = self._facts(function, line, column) if breakpoint and not function.copies else None
        if captures:
            positions = self._positions(site, line, facts, block) or ()
            if any(
                body.dropped_through(root, position) for root in captures for position in positions
            ):
                return False
        verdicts = [self._judge(site, candidate, line, facts, block) for candidate in candidates]
        if False in verdicts:
            return False
        if verdicts and all(verdict is True for verdict in verdicts):
            return True
        return None

    def lent_copies(
        self, hashed, variable_name, line, path, breakpoint=False, column=None, block=None
    ):
        """For a name an `async fn`'s body binds twice to one parameter (#116): the
        field its state keeps it in (`debug v => ((*_34).0: &Pt)`), which holds what the
        caller lent for as long as the future lives, and the poll's own copy of it (`_3 =
        copy ((*_34).0: &Pt)`), which a later poll leaves unassigned. lldb-dap shows both
        under one name, and ``reference_valid`` is False when the copy is not proven. Then
        (bytes each holds, how many bindings): where every row of the name holds the same
        bytes, each shows what the caller lent. None where they are not just that."""
        site = self._site(hashed, line, path)
        if site is None or site[0].coroutine is None:
            return None
        body, function = site[0], site[1]
        name, at, declared = str(variable_name).partition(" @ ")
        bindings = body.bindings.get(name, [])
        if at:
            file, _, number = declared.rpartition(":")
            bindings = [b for b in bindings if Path(b[1]).name == file and str(b[2]) == number]
        bindings = body.in_scopes(bindings, self._visible(body, line, path, block, site, bindings))
        fields = [b for b in bindings if b[0] is None]
        copies = [b[0] for b in bindings if b[0] is not None]
        if len(fields) != 1 or not copies:
            return None
        place = body.places.get((name, *fields[0][1:3], fields[0][4]), "")
        found = re.fullmatch(rf"\(\(\*_{body.coroutine}\)\.([0-9]+): (&.+)\)", place)
        lent = body._lent()
        if found is None or int(found[1]) in body._written_fields or _RAW_POINTER.search(found[2]):
            return None
        facts = self._facts(function, line, column) if breakpoint and not function.copies else None
        positions = self._positions(site, line, facts, block)
        if not positions or any(
            body.dropped_through(local, position)
            for local in (body.coroutine, *copies)
            for position in positions
        ):
            return None
        for local in copies:
            values = body._assignments().get(local, [])
            if (
                local not in lent
                or body.types.get(local) != found[2]
                or local in body._written_through()
                or any(value not in (f"copy {place}", f"move {place}") for value in values)
            ):
                return None
        wide = re.match(r"&(?:mut )?(?:\[|str$|dyn )", found[2])
        return (16 if wide else 8), len(bindings)

    def _elsewhere(self, hashed, variable_name, path):
        """For a stop no line of the function's own can place: in another of the crate's
        files than its function's (a `macro_rules!` from another module, `include!`d
        code), at a line its IR does not explain, or anywhere in a function whose body
        was identified but not analyzed (a dataflow past its budget) or not told apart
        from its twins (a macro's methods). False if a binding of the name may borrow,
        else None. A reference parameter never reassigned, written through or dropped
        through (`*v = new`, `*cell.borrow_mut() = new`) is the caller's, so exempt."""
        body, function = self.body(hashed), self.functions.get(hashed)
        bodies = [body] if body is not None else self._unanalyzed.get(hashed, [])
        if not bodies or function is None:
            return None
        shown = str(path or "").replace("\\", "/")
        if not any(shown.endswith("/" + source) for source in self.sources):
            return None
        name = str(variable_name).partition(" @ ")[0]
        for body in bodies:
            for local, _, _, projected, _ in body.bindings.get(name, ()):
                kind = body.types.get(local) if local is not None else projected
                if not kind or not _BORROWING.search(kind) or _FN_POINTER.match(kind):
                    continue
                if (
                    local in body.arguments
                    and kind.startswith("&")
                    and not _RAW_POINTER.search(kind)
                    and local not in body._assignments()
                    and local not in body._written_through()
                    and not body.drops_through(local)
                ):
                    continue
                return False
        return None

    def _positions(self, site, line, facts, block=None):
        """Where in the line's blocks the stop can be; in the DWARF lexical block the
        stop's pc is in, where ``block`` tells it apart (#115)."""
        body, function, blocks, shared = site
        if function.inline_only:
            # Before any statement of the line, after any call or drop of it: more
            # places only make a proof stricter. The prologue is the declaration line's.
            text = self.texts.get(function.file)
            if self.precise or not may_invoke_macro(text, line):
                found = body.stop_positions(
                    blocks, line, function.file, may_invoke_macro(text, line)
                )
            else:
                found = body.anywhere(blocks)
            reachable = body._dataflow() or {}
            found = {position for position in found or () if position[0] in reachable}
            if line == function.line:
                found.add((0, 0))
            return sorted(found)
        text = self.texts.get(function.file)
        if self.precise or not may_invoke_macro(text, line):
            foreign = may_invoke_macro(text, line)
            return body.stop_positions(
                blocks, line, function.file, foreign, facts, shared, block, function
            )
        # `#[collapse_debuginfo]` puts a local macro's code on the line invoking it, while
        # its statements keep the macro's lines: any statement can be the stop.
        return body.anywhere(blocks)

    def _judge(self, site, local, line, facts, block=None):
        body, function, blocks, _ = site
        if local is None:
            return None
        kind = body.types.get(local)
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
        # Written through a `&mut` to it (`*p = owned.as_str()`), it holds what no
        # assignment of its own shows.
        free = local not in body._written_through()
        # A parameter's references point at what the caller lent for the whole call; an
        # `async fn`'s, for as long as its future lives (#116), though a copy of one in
        # a local of the body's own frame is gone at a later poll.
        lent = local in body._lent()
        argument = (
            free
            and not _RAW_POINTER.search(kind)
            and (lent or local in body.arguments and not assigned)
        )
        positions = self._positions(site, line, facts, block)
        if not positions:
            return None
        # Between `drop((*v))` and the store of `*v = new`, it points at a dropped value.
        if any(body.dropped_through(local, position) for position in positions):
            return False
        if (
            argument
            and kind.startswith("&")
            and (not lent or local in body.arguments or self._initialized(site, local, line))
        ):
            return True
        if free and assigned and all(value.startswith("const ") for value in assigned):
            return True
        # Not yet assigned (its declaration line), it holds whatever its slot last held.
        # Where the stop's pc tells its places apart (#115), initialized at those.
        if not self._initialized(site, local, line) and not (
            block is not None
            and positions != self._positions(site, line, facts)
            and body.initialized_before(local, positions)
        ):
            return False
        if argument:
            return True
        if free and assigned and all(self._frozen(site, local, value, line) for value in assigned):
            return True
        return all(
            body.used_ahead(local, position) or body.used_before(local, position)
            for position in positions
        )

    def _frozen(self, site, local, value, line):
        """True if ``local`` assigned ``value`` points where nothing can move or free
        while it is in scope: into what a shared reference argument (never reassigned or
        borrowed) points at, or at a variable (or a field or element of it) declared in a
        scope enclosing its own and initialized at the stop."""
        body = site[0]
        if value.startswith(("&raw ", "&fake ")):
            return False
        if found := re.fullmatch(r"(?:copy|move) _([0-9]+)", value):
            return self._shared_argument(body, int(found[1]))
        if not value.startswith("&") or " as " in value:
            return False
        place = re.sub(r"\[_[0-9]+\]", "", value[1:].removeprefix("mut "))
        mentioned = {int(n) for n in _MENTIONED.findall(place)}
        if len(mentioned) != 1:
            return False
        (base,) = mentioned
        if place.count("(*") == 1 and f"(*_{base})" in place:
            return self._shared_argument(body, base)
        if "(*" in place:
            return False
        kind = body.types.get(base)
        if kind is None or _BORROWING.search(kind):
            return False
        own = _binding_scopes(body, local)
        theirs = _binding_scopes(body, base)
        return (
            len(own) == 1
            and len(theirs) == 1
            and _encloses(body, theirs[0], own[0])
            and self._initialized(site, base, line)
        )

    @staticmethod
    def _shared_argument(body, local):
        kind = body.types.get(local, "")
        return (
            (local in body._lent() or local in body.arguments and local not in body._assignments())
            and kind.startswith("&")
            and not kind.startswith("&mut ")
            and local not in body._written_through()
        )

    def _facts(self, function, line, column):
        """What the stop's line holds, for ``stop_positions``: the IR's calls, branches
        and inlined calls on it, its macros' columns, and the stop's column (bytes), when
        the source text is known."""
        text = self.texts.get(function.file)
        lines = text.split("\n") if isinstance(text, str) else []
        own = lines[line - 1] if 1 <= line <= len(lines) else None

        def to_bytes(character):
            if own is None:
                return character
            return len(own[: max(character - 1, 0)].encode("utf-8")) + 1

        macros = frozenset(
            to_bytes(found.start(1) + 1) for found in _MACRO_CALL.finditer(own or "")
        )
        return LineFacts(
            column if own is not None and type(column) is int and column > 0 else None,
            function,
            frozenset(function.inlined.get(line, ())),
            macros,
            to_bytes,
            own is not None,
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
        if function.inline_only:
            # No IR shows where its inlined copies' code is (#116): every block with a
            # statement of the line, or with code another file's macro may put on it,
            # and with `#[collapse_debuginfo]` any block, on a line that may invoke one.
            foreign = may_invoke_macro(self.texts.get(function.file), line)
            if foreign and not self.precise:
                blocks = set(body._dataflow() or ())
            else:
                blocks = body.covering(line, function.file, foreign)
            return (body, function, blocks, None) if blocks or line == function.line else None
        blocks = function.mir_blocks(line)
        if blocks is None:
            return None
        shared = _shared(body, function, blocks)
        return body, function, set(shared), shared

    def _initialized(self, site, local, line):
        body, function, blocks, shared = site
        if function.inline_only:
            return body.initialized_before(local, self._positions(site, line, None))
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


def _same_path(body, function):
    """True if the body's path ends as the function's namespaces do, up to an impl (the
    MIR's `<impl at src/a.rs:2:1: 2:11>`, the debug info's `{impl#0}`): a closure in an
    async block (`serve::{closure#0}::{closure#0}::{closure#0}`) is not the block."""
    ancestors = function.ancestors or ((function.parent,) if function.parent else ())
    for mine, theirs in zip(reversed(_segments(body.path)[:-1]), reversed(ancestors)):
        if mine.startswith("<"):
            return True
        if mine != _mir_name(theirs):
            return False
    return True


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
