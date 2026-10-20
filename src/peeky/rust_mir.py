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
    scopes: dict = field(default_factory=lambda: {0: None})  # {scope: its parent}
    # {(file, line): the scopes of the statements with a span on that line}
    line_scopes: dict = field(default_factory=dict)
    inlined_scopes: set = field(default_factory=set)
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

    def stop_positions(self, blocks, line, file, foreign, facts=None, shared=None):
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
        stop is not before it."""
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
        if facts is None or facts.column is None:
            return sorted(found)
        kept = [
            position
            for position, owners in found.items()
            if owners is None or any(self._could_be(o, i, line, file, facts) for o, i in owners)
        ]
        return sorted(kept or found)

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
        elif (box := self._replacements()[1].get(text)) is not None:
            # The store of `bx.data = new` makes the Box whole again only if it was
            # whole before the drop (no field moved out earlier).
            pending = 1 << self._pending(box)
            if state & pending:
                state |= 1 << box
            state &= ~pending
        return state

    def _terminate(self, text, state):
        """``text``'s kills; the drop of a drop-and-replace through a Box remembers (in
        the Box's pending bit) whether the Box was whole."""
        if (box := self._replacements()[0].get(text)) is not None:
            pending = 1 << self._pending(box)
            state = state | pending if state & (1 << box) else state & ~pending
        return state & ~_mask(self._kills(text))

    def _pending(self, box):
        if self._offset is None:
            self._offset = 1 + max(map(_max_local, self.blocks.values()), default=0)
        return self._offset + box

    def _replacements(self):
        """({drop: Box local}, {store: Box local}) for `bx.data = new` or `*bx = new`
        through a Box: `drop(((*_13).1: T))` then, next in the drop's target, the store
        to the same place through another pointer of the same Box. The store fills what
        the drop emptied."""
        if self._replaced is None:
            boxes, drops, found = self._boxes(), {}, {}
            for block in self.blocks.values():
                dropped = _DROP_PLACE.match(block.terminator)
                target = _target(block)
                if not dropped or target is None or target not in self.blocks:
                    continue
                place = _through_box(dropped[1], boxes, self._box_paths)
                if place is None:
                    continue
                # The next thing it does, past storage markers, new pointers to the Box,
                # the debug checks on them (`assert(copy _44, "misaligned pointer
                # dereference...")` and its arithmetic) and `goto`s.
                number, seen, text = target, set(), None
                while number in self.blocks and number not in seen and text is None:
                    seen.add(number)
                    for statement, _, _ in self.blocks[number].statements:
                        whole = _WHOLE_ASSIGNMENT.match(statement)
                        if _NO_CODE.match(statement) or (whole and "->" not in statement):
                            continue
                        if goto := re.fullmatch(r"goto -> bb([0-9]+);", statement):
                            number = int(goto[1])
                        elif statement.startswith("assert(") and (
                            success := re.search(r"\bsuccess: bb([0-9]+)", statement)
                        ):
                            number = int(success[1])
                        else:
                            text = statement
                        break
                    else:
                        break
                store = _STORE.match(text or "")
                if store and _through_box(store[1], boxes, self._box_paths) == place:
                    found[text] = place[0]
                    drops[block.terminator] = place[0]
            self._replaced = (drops, found)
        return self._replaced

    def _aliases(self, local):
        """``local`` and the locals made from it (`&mut (*_1)`, `&((*_1).0: T)`,
        `copy _1`, `move _1`, and so on): pointers into what it points at."""
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
                    not _POINTERLESS.fullmatch(self.types.get(n, "")) for n in (a, b)
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
            self._alias_edges = edges
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
                    for other in predecessors.get(number, ())
                )
        return False

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
        # Each local's bit, and a pending bit per Box local above them (`_pending`).
        top = (1 << (2 * (1 + max(map(_max_local, self.blocks.values()), default=0)))) - 1
        entry = {number: top for number in reachable}
        entry[0] = _mask(self.arguments)
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
            for target in block.successors:
                out = state
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
    for key in ("name", "linkageName", "scope", "file", "line", "column", "inlinedAt", "filename")
}
# An instruction that surely leaves code at its location: a call (not an LLVM intrinsic,
# which may lower to nothing) or a conditional branch.
_IR_CALL = re.compile(r"^\s*(?:%\S+ = )?(?:tail |musttail |notail )?(?:call|invoke) (?!.*@llvm\.)")
_IR_BRANCH = re.compile(r"^\s*(?:br i1 |switch )")


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
            kind = "c" if _IR_CALL.match(line) else "b" if _IR_BRANCH.match(line) else ""
            if dbg := _DBG.search(line):
                # An `invoke`'s location is on its `to label ...` line.
                current[2].setdefault(current[4][0], []).append((dbg[1], kind or pending))
                pending = ""
            elif kind:
                pending = kind
    wanted = set(files)
    functions = {}
    for hashed, subprogram, blocks, preds, _ in raw:
        kind, fields = meta.get(subprogram, (None, {}))
        if hashed is None or kind != "DISubprogram":
            continue
        file = _file(meta, subprogram)
        if file not in wanted:
            continue
        lines, events, inlined = {}, {}, {}
        for label, refs in blocks.items():
            lines[label] = set()
            found_events = ([], [])
            for ref, instruction in refs:
                kind, location = meta.get(ref, (None, {}))
                if kind != "DILocation":
                    continue
                if "inlinedAt" in location:
                    site = _call_site(meta, location)
                    if site is not None and _file(meta, site.get("scope")) == file:
                        found = (int(site.get("line") or 0), int(site.get("column") or 0))
                        inlined.setdefault(found[0], set()).add(found[1])
                    continue
                if _file(meta, location.get("scope")) != file or not location.get("line"):
                    continue
                number = int(location["line"])
                if number > 0:
                    lines[label].add(number)
                slot = {"c": 0, "b": 1}.get(instruction)
                if slot is not None:
                    found_events[slot].append((number, int(location.get("column") or 0)))
            events[label] = (tuple(found_events[0]), tuple(found_events[1]))
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
        )
    return functions


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
                    # A twin: the one generated from it has spans on those lines.
                    candidates = [
                        *candidates,
                        *(
                            self.bodies[index]
                            for number in function.lines()
                            for index in sorted(self._owners.get((function.file, number), ()))
                            if self.bodies[index].name == function.name
                        ),
                    ]
            found = candidates[0] if len(candidates) == 1 else None
            if found is not None and not _covered(found, function):
                # Identified, but its layout or dataflow is not understood.
                self._unanalyzed[hashed] = [found]
                found = None
            elif len(candidates) > 1:
                # Twins (a macro's methods): one of them, not told apart.
                self._unanalyzed[hashed] = list({id(body): body for body in candidates}.values())
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

    def reference_valid(self, hashed, variable_name, line, path, breakpoint=False, column=None):
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
        cannot tell apart is judged by every binding."""
        site = self._site(hashed, line, path)
        if site is None:
            return self._elsewhere(hashed, variable_name, path)
        body, function = site[0], site[1]
        local = self._local(body, variable_name, line, path)
        if local is not None:
            candidates = [local]
        else:
            name, at, declared = str(variable_name).partition(" @ ")
            bindings = body.bindings.get(name, [])
            if at:
                file, _, number = declared.rpartition(":")
                bindings = [b for b in bindings if Path(b[1]).name == file and str(b[2]) == number]
            bindings = body.in_scopes(bindings, self._visible(body, line, path))
            candidates = [b[0] for b in bindings]
        facts = self._facts(function, line, column) if breakpoint else None
        verdicts = [self._judge(site, candidate, line, facts) for candidate in candidates]
        if False in verdicts:
            return False
        if verdicts and all(verdict is True for verdict in verdicts):
            return True
        return None

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

    def _judge(self, site, local, line, facts):
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
        # A parameter's references point at what the caller lent for the whole call.
        argument = (
            free and local in body.arguments and not assigned and not _RAW_POINTER.search(kind)
        )
        file = function.file
        text = self.texts.get(file)
        if self.precise or not may_invoke_macro(text, line):
            foreign = may_invoke_macro(text, line)
            positions = body.stop_positions(blocks, line, file, foreign, facts, site[3])
        else:
            # `#[collapse_debuginfo]` puts a local macro's code on the line invoking it,
            # while its statements keep the macro's lines: any statement can be the stop.
            positions = body.anywhere(blocks)
        if not positions:
            return None
        # Between `drop((*v))` and the store of `*v = new`, it points at a dropped value.
        if any(body.dropped_through(local, position) for position in positions):
            return False
        if argument and kind.startswith("&"):
            return True
        if free and assigned and all(value.startswith("const ") for value in assigned):
            return True
        # Not yet assigned (its declaration line), it holds whatever its slot last held.
        if not self._initialized(site, local, line):
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
            local in body.arguments
            and kind.startswith("&")
            and not kind.startswith("&mut ")
            and local not in body._assignments()
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
