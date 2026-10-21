"""Locals a loop's pass has not assigned yet (#182).

Stopped in a loop, a local that the pass assigns below the stop still holds what an
earlier pass, or the code before the loop, left there: back from `slug` with `step
--out` on `key = slug(title)`, `key` is the previous title's key. The value is real, so
it is shown, with a note. Peeky reads the function's bytecode, compiled from its source
by this interpreter (the one that runs the program), and notes a local only where it
proves all of this:

- the stop is in a loop: the debugger gives a line, not an instruction, so every
  instruction of the line is taken as where the stop may be, and all must be in the same
  innermost loop of the function's control flow (exception handlers included), which a
  `for` or `while` statement around the line makes;
- this pass has not assigned it: no path from the loop's head to any of them stores it;
- the code assigns it again before reading it: no path from any of them reads it (or
  deletes it) before storing it, and some path stores it.

The source is compiled both as `python` and as `python -O` compile it (an `assert`, or
`if __debug__:`, is left out under -O), and both must agree. A function that may read
its locals another way (`locals()`, `vars()`, `eval`, `exec`, `dir()`, frame objects)
is not judged, nor a local a closure shares (a cell), nor a source changed after the
program started. Nothing is evaluated in the program.
"""

import ast
import dis
import inspect
import sys
import types
import warnings
from collections import OrderedDict

from peeky.python_scopes import read_source, source_key

# Functions larger than this are not judged.
MAX_INSTRUCTIONS = 20_000
# Compiled sources kept (each with its code objects and what was judged in them).
MAX_SOURCES = 4
MAX_JUDGED = 4096
_SOURCES = OrderedDict()
# Instructions after which the next one does not run.
_NO_FALLTHROUGH = frozenset(
    {
        "RETURN_VALUE",
        "RETURN_CONST",
        "RAISE_VARARGS",
        "RERAISE",
        "JUMP_FORWARD",
        "JUMP_BACKWARD",
        "JUMP_BACKWARD_NO_INTERRUPT",
        "JUMP",
        "JUMP_NO_INTERRUPT",
    }
)
_JUMPS = frozenset(dis.hasjrel) | frozenset(dis.hasjabs) | frozenset(getattr(dis, "hasjump", ()))
_LOCALS = frozenset(dis.haslocal)
# Names through which code may read a frame's locals other than by its own loads.
_INTROSPECTION = frozenset(
    {
        "locals",
        "vars",
        "eval",
        "exec",
        "dir",
        "breakpoint",
        "_getframe",
        "currentframe",
        "f_locals",
        "f_back",
        "tb_frame",
        "gi_frame",
        "cr_frame",
        "ag_frame",
        "stack",
        "trace",
        "getouterframes",
        "getinnerframes",
    }
)


class Unread(Exception):
    """Code Peeky does not read (an instruction it cannot place)."""


class PassNote(str):
    """A local shown with a note (see the worker's ``not_yet_declared``): as a string,
    why a change to it is refused; ``note`` for its row."""

    refuses = True

    def __new__(cls, reason, note):
        made = super().__new__(cls, reason)
        made.note = note
        return made


def _effects(instruction):
    """((kind, local), ...) an instruction does to the frame's own locals, in order:
    `store`, or `load` (a read, a `del`, or anything else that names one)."""
    if instruction.opcode not in _LOCALS:
        return ()
    names = instruction.argval if isinstance(instruction.argval, tuple) else (instruction.argval,)
    if not names or not all(isinstance(name, str) for name in names):
        raise Unread(instruction.opname)
    if instruction.opname == "STORE_FAST_LOAD_FAST" and len(names) == 2:
        return (("store", names[0]), ("load", names[1]))
    if instruction.opname.startswith("STORE_FAST"):
        return tuple(("store", name) for name in names)
    return tuple(("load", name) for name in names)


class Flow:
    """One code object's control flow, by instruction: each one's line, successors
    (jumps, the next instruction, and the handler of every exception-table range it
    is in, since any instruction may raise), predecessors, effects on locals,
    dominators and natural loops."""

    def __init__(self, code):
        instructions = list(dis.get_instructions(code))
        if not instructions or len(instructions) > MAX_INSTRUCTIONS:
            raise Unread("size")
        index = {each.offset: number for number, each in enumerate(instructions)}
        lines = {}
        for start, end, line in code.co_lines():
            for offset in range(start, end, 2):
                lines[offset] = line
        self.lines = [lines.get(each.offset) for each in instructions]
        successors = []
        for number, each in enumerate(instructions):
            following = []
            if each.opname not in _NO_FALLTHROUGH and number + 1 < len(instructions):
                following.append(number + 1)
            if each.opcode in _JUMPS:
                if type(each.argval) is not int or each.argval not in index:
                    raise Unread(each.opname)
                following.append(index[each.argval])
            successors.append(following)
        for entry in dis.Bytecode(code).exception_entries:
            if entry.target not in index:
                raise Unread("exception table")
            for number, each in enumerate(instructions):
                if entry.start <= each.offset < entry.end:
                    successors[number].append(index[entry.target])
        self.successors = [tuple(dict.fromkeys(each)) for each in successors]
        self.effects = [_effects(each) for each in instructions]
        self._loops()

    def _loops(self):
        # Reverse postorder from the entry, then dominators (Cooper, Harvey, Kennedy).
        order, seen, stack = [], {0}, [(0, iter(self.successors[0]))]
        while stack:
            node, children = stack[-1]
            for child in children:
                if child not in seen:
                    seen.add(child)
                    stack.append((child, iter(self.successors[child])))
                    break
            else:
                stack.pop()
                order.append(node)
        order.reverse()
        rank = {node: number for number, node in enumerate(order)}
        predecessors = {node: [] for node in order}
        for node in order:
            for child in self.successors[node]:
                predecessors[child].append(node)
        dominator = {0: 0}
        changed = True
        while changed:
            changed = False
            for node in order[1:]:
                done = [each for each in predecessors[node] if each in dominator]
                chosen = done[0]
                for other in done[1:]:
                    left, right = other, chosen
                    while left != right:
                        while rank[left] > rank[right]:
                            left = dominator[left]
                        while rank[right] > rank[left]:
                            right = dominator[right]
                    chosen = left
                if dominator.get(node) != chosen:
                    dominator[node] = chosen
                    changed = True
        self.rank, self.predecessors, self.dominator = rank, predecessors, dominator
        # Natural loops: each edge to a node that dominates its source closes one.
        self.loops = {}
        for node in order:
            for child in self.successors[node]:
                if self.dominates(child, node):
                    body = self.loops.setdefault(child, {child})
                    pending = [node]
                    while pending:
                        each = pending.pop()
                        if each not in body:
                            body.add(each)
                            pending.extend(predecessors[each])

    def dominates(self, head, node):
        while node != head:
            parent = self.dominator[node]
            if parent == node:
                return False
            node = parent
        return True

    def innermost(self, node):
        found = [head for head, body in self.loops.items() if node in body]
        return min(found, key=lambda head: len(self.loops[head])) if found else None

    def judge(self, line, name):
        """(the loop's head, lines that store ``name`` next) where a stop on ``line`` is
        in a loop whose current pass has not stored ``name``, which the code stores again
        before reading it; else None."""
        positions = [node for node in self.rank if self.lines[node] == line]
        if not positions:
            return None
        heads = {self.innermost(node) for node in positions}
        if len(heads) != 1 or None in heads:
            return None
        head = heads.pop()
        if not all(self.dominates(head, node) for node in positions):
            return None
        if self._stored_in_pass(head, positions, name):
            return None
        if not self._stored_before_read(positions, name):
            return None
        stores = self._pass_stores(head, positions, name)
        if not stores:
            return None
        return head, stores

    def _stored_in_pass(self, head, positions, name):
        """Whether a path from the loop's head to one of ``positions`` (not through the
        head again) stores ``name`` before reaching it."""
        after = {head}
        pending = [each for each in self.successors[head] if each != head]
        while pending:
            node = pending.pop()
            if node not in after:
                after.add(node)
                pending.extend(each for each in self.successors[node] if each != head)
        for position in positions:
            if position == head:
                continue
            before, pending = set(), list(self.predecessors[position])
            while pending:
                node = pending.pop()
                if node in before:
                    continue
                before.add(node)
                if node != head:
                    pending.extend(self.predecessors[node])
            if any(("store", name) in self.effects[node] for node in after & before):
                return True
        return False

    def _effect(self, node, name):
        return next((kind for kind, local in self.effects[node] if local == name), None)

    def _stored_before_read(self, positions, name):
        """Whether no path from ``positions`` reads (or deletes) ``name`` before storing
        it: on each, it is stored first, or never named again."""
        seen, pending = set(), list(positions)
        while pending:
            node = pending.pop()
            if node in seen:
                continue
            seen.add(node)
            effect = self._effect(node, name)
            if effect == "load":
                return False
            if effect != "store":
                pending.extend(self.successors[node])
        return True

    def _pass_stores(self, head, positions, name):
        """The lines of the stores of ``name`` the rest of this pass reaches first from
        ``positions`` (in the loop, not through its head again)."""
        body, stores, seen, pending = self.loops[head], set(), set(), list(positions)
        while pending:
            node = pending.pop()
            if node in seen:
                continue
            seen.add(node)
            if self._effect(node, name) == "store":
                stores.add(self.lines[node])
                continue
            pending.extend(each for each in self.successors[node] if each != head and each in body)
        return stores


def _introspects(code):
    """Whether the code, or a function it defines, names a way to read a frame's locals
    (`locals()`, `sys._getframe(1).f_locals`)."""
    for each in _codes(code):
        for instruction in dis.get_instructions(each):
            value = instruction.argval
            if any(
                isinstance(name, str) and name in _INTROSPECTION
                for name in (value if isinstance(value, tuple) else (value,))
            ):
                return True
    return False


def _codes(module):
    pending = [module]
    while pending:
        code = pending.pop()
        yield code
        pending.extend(each for each in code.co_consts if isinstance(each, types.CodeType))


def _loops(tree):
    """(first, last, body's first, body's last) of each `for` and `while` statement."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)) and node.body:
            first = node.body[0]
            start = min([first.lineno, *(d.lineno for d in getattr(first, "decorator_list", ()))])
            found.append((node.lineno, node.end_lineno, start, node.body[-1].end_lineno))
    return found


def _source(path, key):
    """{"loops", "codes": {optimize: module code}, "flows", "judged"} for a source, or
    None where it cannot be read or compiled."""
    if key in _SOURCES:
        _SOURCES.move_to_end(key)
        return _SOURCES[key]
    data, _ = read_source(path, key)
    if data is None:
        return None
    try:
        with warnings.catch_warnings():
            # The program's own compile already said it (`invalid escape sequence`).
            warnings.simplefilter("ignore")
            loops = _loops(ast.parse(data, str(path)))
            codes = {
                optimize: compile(data, str(path), "exec", dont_inherit=True, optimize=optimize)
                for optimize in (0, 1)
            }
    except (SyntaxError, ValueError, UnicodeDecodeError, RecursionError, MemoryError):
        return None
    source = {"loops": loops, "codes": codes, "flows": {}, "judged": {}}
    _SOURCES[key] = source
    while len(_SOURCES) > MAX_SOURCES:
        _SOURCES.popitem(last=False)
    return source


def _judge(source, function, line, name):
    verdicts = set()
    for optimize, module in source["codes"].items():
        found = [
            code
            for code in _codes(module)
            if code.co_name == function and any(at == line for _, _, at in code.co_lines())
        ]
        if len(found) != 1:
            return None
        code = found[0]
        if (
            not code.co_flags & inspect.CO_OPTIMIZED
            or name not in code.co_varnames
            or name in code.co_cellvars
            or name in code.co_freevars
        ):
            return None
        if (optimize, id(code)) not in source["flows"]:
            try:
                flow = None if _introspects(code) else Flow(code)
            except (Unread, RecursionError, MemoryError):
                flow = None
            source["flows"][(optimize, id(code))] = flow
        flow = source["flows"][(optimize, id(code))]
        if flow is None:
            return None
        verdict = flow.judge(line, name)
        if verdict is None:
            return None
        head, stores = verdict
        # The innermost `for` or `while` whose body holds the line makes the loop: its
        # head is on the statement's own lines (a `for`'s next item), or is the body's
        # first instruction (a `while` whose condition the compiler repeats at the end
        # of each pass, which so runs before the head: one storing the local, as a
        # walrus does, is never judged). Not a comprehension's loop in the body.
        around = [loop for loop in source["loops"] if loop[2] <= line <= loop[3]]
        if not around:
            return None
        first, _, start, _ = max(around, key=lambda loop: loop[2])
        header = range(first, max(first, start - 1) + 1)
        opening = min((node for node in flow.rank if flow.lines[node] == start), default=None)
        if flow.lines[head] not in header and head != opening:
            return None
        if any(
            ("store", name) in effects and at in header
            for effects, at in zip(flow.effects, flow.lines)
        ):
            return None
        verdicts.add((first, tuple(sorted(each for each in stores if type(each) is int))))
    if len(verdicts) != 1:
        return None
    return verdicts.pop()


def stale_pass(path, function, line, name, loaded_by=None):
    """``(loop's line, (lines that assign it next, ...))`` where the local ``name`` of
    ``function``, stopped at ``line`` of ``path``, holds a value from before this pass of
    the loop around the line, and the code assigns it again before reading it (see the
    module's docstring); else None. A file changed after ``loaded_by`` (a time.time())
    proves nothing."""
    if sys.implementation.name != "cpython" or sys.version_info < (3, 11):
        return None
    if not (
        isinstance(name, str)
        and name.isidentifier()
        and isinstance(function, str)
        and function.isidentifier()
        and type(line) is int
        and isinstance(path, str)
        and path
    ):
        return None
    key, _ = source_key(path, loaded_by)
    if key is None:
        return None
    source = _source(path, key)
    if source is None:
        return None
    judged = source["judged"]
    if (function, line, name) not in judged:
        if len(judged) >= MAX_JUDGED:
            judged.clear()
        try:
            verdict = _judge(source, function, line, name)
        except Exception:
            # A note is only ever added: code Peeky does not read gets none.
            verdict = None
        judged[(function, line, name)] = verdict
    return judged[(function, line, name)]


def stale_note(path, function, line, name, loaded_by=None):
    """A ``PassNote`` for the local ``name`` (see ``stale_pass``), or None."""
    found = stale_pass(path, function, line, name, loaded_by)
    if found is None:
        return None
    loop, stores = found
    where = (
        f"line {stores[0]}"
        if len(stores) == 1
        else f"lines {', '.join(map(str, stores))}"
        if stores
        else "below"
    )
    return PassNote(
        f"the code assigns it again ({where}) before it reads it, so a change made here "
        "would never be seen",
        f"an earlier pass's value (or one from before the loop at line {loop}): this pass "
        f"has not assigned it yet, and the code assigns it again ({where}) before reading "
        "it; stop after that to see this pass's value",
    )
