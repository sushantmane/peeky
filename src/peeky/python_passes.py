"""Locals a loop's pass has not assigned yet (#182).

Stopped in a loop, a local that the pass assigns below the stop still holds what an
earlier pass, or the code before the loop, left there: back from `slug` with `step
--out` on `key = slug(title)`, `key` is the previous title's key. The value is real, so
it is shown, with a note that states facts about the code. Peeky reads the function's
bytecode, compiled from its source, and notes a local only where all of these hold:

- the stop is in a loop: the debugger gives a line, not an instruction, so the stop may
  be at any instruction of the line (only where the line begins, for a breakpoint's
  stop), and all must be in the same innermost loop of the function's control flow,
  which a `for` or `while` statement around the line makes;
- this pass's code has not assigned it before the line: no path from the loop's head to
  any of them stores it (a path through an exception handler counts);
- the code reads it only after assigning it: no path from any of them reads it (or
  deletes it) before storing it (exception paths count);
- every path through the rest of the pass assigns it: on every path from them to the
  pass's end (back to the head, out of the loop, a return or a raise) that runs no
  exception, a store comes first. An exception's path can only take a note away.

It is compiled by this interpreter, so only where the program runs under one that
compiles alike (the same implementation and bytecode magic number), both as `python`
and as `python -O` compile it (an `assert` is left out under -O), which must agree. Not
judged: a module (the whole file) that may run code other than its source says (`exec`,
`eval`, `compile`, `types.FunctionType`, `__code__`) or may read or change a frame's
locals (`locals()`, `sys._getframe`, `f_locals`, `f_back`, the `inspect` module), a local
a closure shares, a source changed after the program started. Code in other modules
that reaches into this frame is not seen: hence the note's "likely". Nothing is
evaluated in the program.
"""

import ast
import dis
import importlib.util
import inspect
import os
import subprocess
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
# Builtins that run code other than the source's, or read a frame's locals, by name.
_RUNS = frozenset({"exec", "eval", "compile", "breakpoint", "locals"})
# Names (attributes, imports, `getattr` strings) that replace a function's code, or reach
# a frame and its locals.
_REACHES = frozenset(
    {
        "FunctionType",
        "LambdaType",
        "CodeType",
        "__code__",
        "co_code",
        "_getframe",
        "currentframe",
        "f_locals",
        "f_back",
        "tb_frame",
        "gi_frame",
        "cr_frame",
        "ag_frame",
        "settrace",
        "setprofile",
        "capture_locals",
    }
)
# Modules whose use does either.
_MODULES = frozenset({"inspect", "marshal"})
# Interpreters checked to compile as this one does: {path: bool}.
_COMPILERS = {}


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
        normal, targets = [], set()
        for number, each in enumerate(instructions):
            following = []
            if each.opname not in _NO_FALLTHROUGH and number + 1 < len(instructions):
                following.append(number + 1)
            if each.opcode in _JUMPS:
                if type(each.argval) is not int or each.argval not in index:
                    raise Unread(each.opname)
                following.append(index[each.argval])
                targets.add(index[each.argval])
            normal.append(following)
        successors = [list(each) for each in normal]
        for entry in dis.Bytecode(code).exception_entries:
            if entry.target not in index:
                raise Unread("exception table")
            targets.add(index[entry.target])
            for number, each in enumerate(instructions):
                if entry.start <= each.offset < entry.end:
                    successors[number].append(index[entry.target])
        # The paths a run takes without an exception, and every path.
        self.normal = [tuple(dict.fromkeys(each)) for each in normal]
        self.successors = [tuple(dict.fromkeys(each)) for each in successors]
        # Where a line begins, as the interpreter reports a line (a breakpoint stops
        # there): an instruction of another line than the one before it, a jump's
        # target, or an exception handler.
        self.entries = {
            number
            for number, line in enumerate(self.lines)
            if line is not None
            and (number == 0 or self.lines[number - 1] != line or number in targets)
        } | {number for number in targets if self.lines[number] is not None}
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

    def judge(self, line, name, entered=False):
        """(the loop's head, the lines whose stores of ``name`` come first on every path
        through the rest of the pass) where a stop on ``line`` is in a loop whose current
        pass has not stored ``name`` before the line, and the code reads it only after
        storing it; else None. ``entered``: the stop is where the line begins (a
        breakpoint's), not part-way through it."""
        positions = [
            node
            for node in self.rank
            if self.lines[node] == line and (not entered or node in self.entries)
        ]
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
        stores = self._certain_stores(head, positions, name)
        if not stores:
            return None
        return head, stores

    def _stored_in_pass(self, head, positions, name):
        """Whether a path from the loop's head to one of ``positions`` (not through the
        head again; exception paths too) stores ``name`` before reaching it."""
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
        """Whether no path from ``positions`` (exception paths too) reads (or deletes)
        ``name`` before storing it: on each, it is stored first, or never named again."""
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

    def _certain_stores(self, head, positions, name):
        """The lines of the stores of ``name`` that come first on the paths from
        ``positions`` that run no exception, where each such path stores it before the
        pass ends (back at the loop's head, out of the loop, a return or a raise); else
        None. A store reached only through an exception's handler, or on some branches,
        proves nothing (review of PR #185)."""
        body, stores, seen, pending = self.loops[head], set(), set(), list(positions)
        while pending:
            node = pending.pop()
            if node in seen:
                continue
            seen.add(node)
            if self._effect(node, name) == "store":
                stores.add(self.lines[node])
                continue
            following = self.normal[node]
            if not following or any(each == head or each not in body for each in following):
                return None
            pending.extend(following)
        return stores


def opaque(tree):
    """Why no function of a module (its syntax ``tree``) is judged, or None: it may run
    code other than its source says (`exec`, `eval`, `compile`, `types.FunctionType`,
    `__code__`, `marshal`), so a frame may not run the code on disk, or may read or change
    a frame's locals other than by its own code (`locals()`, `vars()`, `dir()`,
    `sys._getframe`, `f_locals`, `f_back`, a tracer, the `inspect` module)."""
    # `vars(x)` and `dir(x)` read an object, not the frame: only those calls are left.
    given = {
        id(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("vars", "dir")
        and node.args
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in _RUNS or node.id in _REACHES:
                return node.id
            if node.id in ("vars", "dir") and id(node) not in given:
                return node.id
        elif isinstance(node, ast.Attribute):
            if node.attr in _REACHES:
                return node.attr
            if (
                node.attr in _RUNS
                and isinstance(node.value, ast.Name)
                and node.value.id in ("builtins", "__builtins__")
            ):
                return node.attr
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
                if any(alias.name in _REACHES or alias.name in _RUNS for alias in node.names):
                    return next(
                        alias.name
                        for alias in node.names
                        if alias.name in _REACHES or alias.name in _RUNS
                    )
            for module in modules:
                if module.split(".")[0] in _MODULES:
                    return module.split(".")[0]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # `getattr(sys, "_getframe")`.
            if node.value in _REACHES:
                return node.value
    return None


def same_compiler(interpreter):
    """Whether ``interpreter`` (the program's) compiles a source as this one does: the
    same file, or the same implementation and bytecode magic number, asked once; False
    where it cannot be asked (review of PR #185: a 3.11 worker read a 3.12 program's
    inlined comprehension as a loop of its own)."""
    if not isinstance(interpreter, str) or not interpreter:
        return False
    if interpreter not in _COMPILERS:
        try:
            same = os.path.realpath(interpreter) == os.path.realpath(sys.executable)
            if not same:
                found = subprocess.run(
                    [
                        interpreter,
                        "-I",
                        "-c",
                        "import importlib.util, sys; "
                        "print(sys.implementation.name, importlib.util.MAGIC_NUMBER.hex())",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                same = found.returncode == 0 and found.stdout.split() == [
                    sys.implementation.name,
                    importlib.util.MAGIC_NUMBER.hex(),
                ]
        except (OSError, ValueError, subprocess.SubprocessError):
            same = False
        _COMPILERS[interpreter] = same
    return _COMPILERS[interpreter]


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
    """{"loops", "codes": {optimize: module code}, "flows", "judged", "opaque": why no
    function of it is judged (see ``opaque``)} for a source, or None where it cannot be
    read or compiled."""
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
            tree = ast.parse(data, str(path))
            loops, why = _loops(tree), opaque(tree)
            del tree
            codes = {
                optimize: compile(data, str(path), "exec", dont_inherit=True, optimize=optimize)
                for optimize in (0, 1)
            }
    except (SyntaxError, ValueError, UnicodeDecodeError, RecursionError, MemoryError):
        return None
    source = {"loops": loops, "codes": codes, "flows": {}, "judged": {}, "opaque": why}
    _SOURCES[key] = source
    while len(_SOURCES) > MAX_SOURCES:
        _SOURCES.popitem(last=False)
    return source


def _judge(source, function, line, name, entered):
    if source["opaque"] is not None:
        return None
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
                flow = Flow(code)
            except (Unread, RecursionError, MemoryError):
                flow = None
            source["flows"][(optimize, id(code))] = flow
        flow = source["flows"][(optimize, id(code))]
        if flow is None:
            return None
        verdict = flow.judge(line, name, entered)
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


def stale_pass(path, function, line, name, loaded_by=None, entered=False):
    """``(loop's line, (lines whose stores come first, ...))`` where this pass's code has
    not assigned the local ``name`` of ``function``, stopped at ``line`` of ``path``,
    before the line, and every path through the rest of the pass assigns it before the
    code reads it (see the module's docstring); else None. ``entered``: the stop is where
    the line begins (a breakpoint's). A file changed after ``loaded_by`` (a
    time.time()) proves nothing."""
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
    judged, asked = source["judged"], (function, line, name, bool(entered))
    if asked not in judged:
        if len(judged) >= MAX_JUDGED:
            judged.clear()
        try:
            verdict = _judge(source, function, line, name, bool(entered))
        except Exception:
            # A note is only ever added: code Peeky does not read gets none.
            verdict = None
        judged[asked] = verdict
    return judged[asked]


def stale_note(path, function, line, name, loaded_by=None, entered=False):
    """A ``PassNote`` for the local ``name`` (see ``stale_pass``), or None. It says what
    the code does, not what ran: code elsewhere may still reach the frame (a call that
    writes `sys._getframe(1).f_locals`), so the value is only likely an earlier pass's."""
    found = stale_pass(path, function, line, name, loaded_by, entered)
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
        f"this function's code assigns it again ({where}) before reading it, so a change "
        "made here would not reach that code",
        "this pass's code has not assigned it before this line, and every path through "
        f"the rest of the pass assigns it ({where}) before reading it: the value is likely "
        f"from an earlier pass (or from before the loop at line {loop})",
    )
