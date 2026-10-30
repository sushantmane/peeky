"""Python locals a loop's pass has not assigned yet, read from the bytecode."""

import ast
import collections
import dis
import os
import re
import sys
import time
import types

import pytest

from peeky.python_passes import _effects, _loops, stale_note, stale_pass

# `# stale: a, b` marks a line where each name is noted wherever the stop is on it,
# `# entered: c` one where it is noted only where the line begins (a breakpoint's stop),
# `# fresh: d` one where it is not noted.
CASES = """\
import asyncio
import re


def slug(title):
    return "-".join(re.split(r"[^a-z0-9]+", title.lower()))


def group(titles):
    counts = {}
    for title in titles:  # fresh: key, title
        key = slug(title)  # stale: key  # fresh: title, counts
        if key not in counts:  # fresh: key
            counts[key] = 0
        counts[key] += 1
    return counts


def whiles(n):
    i = 0
    total = 0
    while i < n:  # fresh: x
        x = slug(str(i))  # stale: x  # fresh: i, total
        total += len(x)
        i += 1
    return total


def walrus(items):
    it = iter(items)
    out = []
    while (x := next(it, None)) is not None:
        slug(str(x))  # fresh: x
        x = 0
        out.append(x)
    return out


def branch_before(ts):
    out = []
    for t in ts:
        if t > 1:  # stale: y
            y = t
        slug(str(t))  # fresh: y
        y = 0
        out.append(y)
    return out


def both_branches(ts):
    for t in ts:
        slug(str(t))  # stale: p
        if t:
            p = 1
        else:
            p = 2
        print(p)


def continues(ts):
    out = []
    for t in ts:
        slug(str(t))  # fresh: y
        if t % 2:
            continue
        y = t
        out.append(y)
    return out


def breaks(ts, keep):
    y = -1
    for t in ts:
        slug(str(t))  # fresh: y
        if t > 2:
            break
        y = t
        print(y)
    return y


def broken(ts):
    for t in ts:
        slug(str(t))  # fresh: y
        if t > 2:
            break
        y = t
        print(y)


def handler(ts):
    y = 0
    for t in ts:
        try:
            slug(str(t))  # fresh: y
            if t == 2:
                raise ValueError(t)
            y = t
        except ValueError:
            print("caught", y)
        print(y)


def caught(ts):
    for t in ts:
        try:
            slug(str(t))  # fresh: e
        except KeyError:
            e = t
        print(t)


def matched(ts):
    for t in ts:
        slug(str(t))  # fresh: m
        match 0:
            case 1:
                m = t
        print(t)


def nested(rows):
    total = 0
    for row in rows:
        acc = 0  # stale: acc  # fresh: cell, v
        for cell in row:
            slug(str(cell))  # stale: v  # fresh: row, acc, cell
            v = cell * 2  # stale: v
            acc += v
        total += acc
    return total


def last_line(rows):
    total = 0
    for row in rows:
        for cell in range(2):
            inner = row + cell  # entered: inner
        total += inner
    return total


def deletes(ts):
    for t in ts:
        w = t  # stale: w
        slug(str(t))  # fresh: w
        del w
        w = 3
        print(w)


def condition_reads(n):
    x = 0
    while x < n:
        slug(str(x))  # fresh: x
        x = x + 1
    return x


def same_line(ts):
    for t in ts:
        u = slug(str(t)); print(u)  # entered: u
        u = 2
        print(u)


def closure(ts):
    out = []
    for t in ts:
        slug(str(t))  # fresh: c
        c = t
        out.append(lambda: c)
    return [f() for f in out]


def multiline(titles):
    for title in titles:
        key = slug(  # stale: key
            title  # stale: key
        )
        print(key)


def gen(ts):
    for t in ts:
        yield t  # stale: g
        g = t * 2
        yield g


def asserted(ts):
    for t in ts:
        slug(str(t))  # fresh: z
        assert (z := t) or True
        z = 3
        print(z)


class Suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def fail():
    raise ValueError("no")


def suppressed(items):
    x = -1
    for item in items:
        with Suppress():
            print(item)  # fresh: x
            fail()
            x = item  # fresh: x
        print("next")


def handled_store(items):
    x = 0
    for item in items:
        try:
            print(item)  # stale: x
            fail()
            x = 1
        except ValueError:
            x = 2
        print(x)


async def agen(n):
    for i in range(n):
        yield i


async def drains(items):
    x = -1
    for item in items:
        print(item)  # fresh: x
        async for y in agen(item):
            x = y  # entered: x
        print(item)


def main():
    suppressed([1, 2])
    handled_store([1, 2])
    asyncio.run(drains([0, 0, 1]))
    group(["Hello World", "Hello, World!", "x y"])
    whiles(3)
    walrus([1, 2, 3])
    branch_before([1, 2, 3])
    both_branches([0, 1, 0])
    continues([1, 2, 3, 4])
    breaks([1, 2, 3, 4], 0)
    broken([1, 2, 3, 4])
    handler([1, 2, 3])
    caught([1, 2])
    matched([1, 2])
    nested([[1, 2], [3, 4], []])
    last_line([1, 2])
    deletes([1, 2])
    condition_reads(3)
    same_line([1, 2])
    closure([1, 2])
    multiline(["a b", "c"])
    list(gen([1, 2, 3]))
    asserted([1, 2])


if __name__ == "__main__":
    main()
"""


@pytest.fixture
def cases(tmp_path):
    path = tmp_path / "cases.py"
    path.write_text(CASES)
    return str(path)


def _function_at(line):
    tree = ast.parse(CASES)
    return next(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno <= line <= node.end_lineno
    )


def _marked():
    for number, text in enumerate(CASES.splitlines(), 1):
        for kind, names in re.findall(r"# (stale|entered|fresh): ([\w, ]+)", text):
            for name in names.split(","):
                yield number, kind, name.strip()


@pytest.mark.parametrize("line, kind, name", list(_marked()))
def test_a_local_is_noted_only_where_every_path_of_the_pass_assigns_it_first(
    cases, line, kind, name
):
    function = _function_at(line)
    anywhere = stale_pass(cases, function, line, name)
    entered = stale_pass(cases, function, line, name, entered=True)
    assert (anywhere is not None) == (kind == "stale"), (line, name, anywhere)
    assert (entered is not None) == (kind in ("stale", "entered")), (line, name, entered)


def test_the_note_names_the_loop_and_the_assignment(cases):
    line = next(n for n, text in enumerate(CASES.splitlines(), 1) if "key = slug(title)" in text)
    assert stale_pass(cases, "group", line, "key") == (line - 1, (line,))
    note = stale_note(cases, "group", line, "key")
    # Facts about the code: what ran may differ (a call can write a caller's locals).
    assert note.note == (
        "this pass's code has not assigned it before this line, and every path through "
        f"the rest of the pass assigns it (line {line}) before reading it: the value is "
        f"likely from an earlier pass (or from before the loop at line {line - 1})"
    )
    assert str(note) == (
        f"this function's code assigns it again (line {line}) before reading it, so a "
        "change made here would not reach that code"
    )
    assert note.refuses is True
    # Two assignments, one on each path: both named.
    line = next(n for n, text in enumerate(CASES.splitlines(), 1) if "if t > 1:  #" in text)
    assert "(lines " in stale_note(cases, "branch_before", line, "y").note
    # A handled exception's path assigns it too.
    lines = CASES.splitlines()
    stop = next(n for n, text in enumerate(lines, 1) if "print(item)  # stale: x" in text)
    assert stale_pass(cases, "handled_store", stop, "x") == (stop - 2, (stop + 2, stop + 4))
    assert f"(lines {stop + 2}, {stop + 4})" in stale_note(cases, "handled_store", stop, "x").note


def test_what_cannot_be_read_proves_nothing(cases, tmp_path):
    line = next(n for n, text in enumerate(CASES.splitlines(), 1) if "key = slug(title)" in text)
    assert stale_pass(cases, "group", line, "key") is not None
    # Not that function, not one name, not that line, not a local.
    assert stale_pass(cases, "slug", line, "key") is None
    assert stale_pass(cases, "<module>", line, "key") is None
    assert stale_pass(cases, "group", line, "slug") is None
    assert stale_pass(str(tmp_path / "missing.py"), "group", line, "key") is None
    broken = tmp_path / "broken.py"
    broken.write_text(CASES + "\ndef (:\n")
    assert stale_pass(str(broken), "group", line, "key") is None
    # A file changed after the program started may not be the code running.
    started = time.time()
    os.utime(cases, (started + 5, started + 5))
    assert stale_pass(cases, "group", line, "key", loaded_by=started) is None
    # Two functions of one name on the line: neither is judged.
    twins = tmp_path / "twins.py"
    twins.write_text(
        "def f(ts):\n"
        "    for t in ts:\n"
        "        print(t)\n"
        "        def f(): return 0\n"
        "        g = t\n"
        "        print(g)\n"
    )
    assert stale_pass(str(twins), "f", 3, "g") == (2, (5,))
    assert stale_pass(str(twins), "f", 4, "g") is None


LOOP = """\
def f(xs):
    for t in xs:
        print(t)
        x = t
        print(x)
"""


@pytest.mark.parametrize(
    "extra, why",
    [
        # The function that runs may not be the one on disk.
        (
            'exec(compile("\\n" * 2 + "    for t in xs:\\n        x = 1; print(t)", '
            '__file__, "exec"))\n',
            "exec",
        ),
        ("import types\ng = types.FunctionType(f.__code__, {})\n", "FunctionType"),
        ("f.__code__ = (lambda xs: None).__code__\n", "__code__"),
        ("from types import CodeType\n", "CodeType"),
        ("import marshal\n", "marshal"),
        ("print(eval('1'))\n", "eval"),
        # A call can read or write its caller's locals (`f_locals` writes through, 3.13).
        ("import sys\ndef h():\n    sys._getframe(1).f_locals['x'] = 5\n", "f_locals"),
        ("import sys\ndef h():\n    return sys._getframe(1)\n", "_getframe"),
        ("def h(frame):\n    return frame.f_back.f_locals\n", "f_locals"),
        ("def h(frame):\n    return frame.f_back\n", "f_back"),
        ("import inspect\n", "inspect"),
        ("from inspect import stack\n", "inspect"),
        ("import sys\nsys.settrace(None)\n", "settrace"),
        ("getter = getattr(__import__('sys'), '_getframe')\n", "_getframe"),
        ("def h():\n    return locals()\n", "locals"),
        ("def h():\n    return vars()\n", "vars"),
        ("show = dir\n", "dir"),
        # `vars(*args)` with no `args` is `vars()`.
        ("def h(args):\n    return vars(*args)\n", "vars"),
        ("def h(args):\n    return dir(*args)\n", "dir"),
        ("def h(o):\n    return vars(o, **{})\n", "vars"),
        # Aliases: what is read through them is read whatever their name.
        ("import builtins as b\nb.exec('y = 1')\n", "builtins"),
        ("from builtins import exec as run\n", "exec"),
        ("seen = __builtins__\n", "__builtins__"),
        ("found = __import__('sys')\n", "__import__"),
        ("import importlib\nfound = importlib.import_module('builtins')\n", "import_module"),
        ("import sys as s\ngrab = s._getframe\n", "_getframe"),
        ("from sys import _getframe as grab\n", "_getframe"),
        ("import types as t\nkind = t.FunctionType\n", "FunctionType"),
        ("from types import FunctionType as F\n", "FunctionType"),
        ("import inspect as i\n", "inspect"),
        ("import marshal as m\n", "marshal"),
        # A builtin reached by a computed name.
        ("table = globals()\n", "globals"),
        ("key = '__builtins__'\n", "__builtins__"),
        ("name = 'exec'\n", "exec"),
        ("owner = len.__self__\n", "__self__"),
        ("breakpoint()\n", "breakpoint"),
    ],
)
def test_a_module_that_may_run_other_code_or_reach_frames_gets_no_note(tmp_path, extra, why):
    from peeky.python_passes import opaque

    path = tmp_path / "m.py"
    path.write_text(LOOP)
    assert stale_pass(str(path), "f", 3, "x") == (2, (4,))
    path.write_text(LOOP + extra)
    assert opaque(ast.parse(path.read_text())) == why
    assert stale_pass(str(path), "f", 3, "x") is None


def test_a_builtin_reached_by_its_name_leaves_the_module_unjudged(tmp_path):
    # `globals()["__builtins__"]` and a string gave `exec`.
    from peeky.python_passes import opaque

    path = tmp_path / "m.py"
    reached = "b = globals()['__builtins__']\nrun = getattr(b, 'exec')\n"
    assert opaque(ast.parse(LOOP + reached)) is not None
    path.write_text(LOOP + reached)
    assert stale_pass(str(path), "f", 3, "x") is None


def test_ordinary_uses_of_those_names_leave_a_module_judged(tmp_path):
    from peeky.python_passes import opaque

    ordinary = (
        "import re\n"
        "pattern = re.compile('a')\n"
        "class Model:\n"
        "    def eval(self):\n"
        "        return vars(self), dir(self)\n"
        "Model().eval()\n"
    )
    assert opaque(ast.parse(ordinary)) is None
    path = tmp_path / "m.py"
    path.write_text(LOOP + ordinary)
    assert stale_pass(str(path), "f", 3, "x") == (2, (4,))


def test_only_a_program_interpreter_that_compiles_alike_is_judged(tmp_path, cases):
    # A 3.11 worker read a 3.12 program's inlined comprehension (its
    # `LOAD_FAST_AND_CLEAR x`) as code that never reads `x`. Only an interpreter that
    # reports this one's compiler is read here; another is read by a helper of its own,
    # which these scripts can't run, so they get no note.
    from peeky.adapters import PythonAdapter
    from peeky.python_interpreter import identify, own_compiler

    def interpreter(name, compiler):
        script = tmp_path / name
        version = sys.version.split()[0]
        said = repr([compiler[0], version, list(sys.version_info[:2]), *compiler[1:], 0])
        script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{said}\nEOF\n")
        script.chmod(0o755)
        return str(script)

    own = own_compiler()
    same = interpreter("same", own)
    other = interpreter("other", [*own[:4], "00000d0a"])
    # One magic number spans patch versions and builds; the exact version and
    # build must match too.
    build = interpreter("build", [own[0], own[1] + "+", *own[2:]])
    assert identify(sys.executable, "peeky").same is True
    assert identify(same, "--python").same is True
    assert identify(other, "--python").same is False
    assert identify(build, "--python").same is False
    line = next(n for n, text in enumerate(CASES.splitlines(), 1) if "key = slug(title)" in text)
    frame = {"source": {"path": cases}, "name": "group", "line": line}
    variable = {"name": "key", "type": "str", "value": "'a'"}
    assert PythonAdapter().not_yet_declared(variable, frame) is not None
    assert PythonAdapter(interpreter=same).not_yet_declared(variable, frame) is not None
    assert PythonAdapter(interpreter=other).not_yet_declared(variable, frame) is None
    with pytest.raises(ValueError, match="cannot be changed at this stop"):
        PythonAdapter().validate_mutation_target(variable, frame)
    PythonAdapter(interpreter=other).validate_mutation_target(variable, frame)


def test_a_breakpoints_stop_is_judged_where_its_line_begins(cases):
    # A loop body's last line also holds the jump back, after its
    # assignment; a breakpoint never stops there, but a caller's frame may.
    from peeky.adapters import PythonAdapter

    line = next(n for n, text in enumerate(CASES.splitlines(), 1) if "inner = row" in text)
    frame = {"source": {"path": cases}, "name": "last_line", "line": line}
    variable = {"name": "inner", "type": "int", "value": "1"}
    adapter = PythonAdapter()
    assert adapter.not_yet_declared(variable, frame, innermost=True, stop="breakpoint")
    assert adapter.not_yet_declared(variable, frame, innermost=True, stop="step") is None
    assert adapter.not_yet_declared(variable, frame, innermost=False, stop="breakpoint") is None


def test_a_module_level_loop_is_not_judged(tmp_path):
    path = tmp_path / "top.py"
    path.write_text("for t in range(3):\n    key = str(t)\n    print(key)\n")
    assert stale_pass(str(path), "<module>", 2, "key") is None


def _run_traced(path, functions):
    """{frame id: [(line, effects, function, offset)]}: every instruction each frame of
    ``functions`` ran, in order."""
    module = compile(CASES, path, "exec", dont_inherit=True)
    pending, codes = [module], []
    while pending:
        code = pending.pop()
        codes.append(code)
        pending.extend(each for each in code.co_consts if isinstance(each, types.CodeType))
    decoded = {code: {i.offset: i for i in dis.get_instructions(code)} for code in codes}
    lines = {}
    for code in codes:
        lines[code] = {}
        for start, end, line in code.co_lines():
            for offset in range(start, end, 2):
                lines[code][offset] = line
    timelines, frames = collections.defaultdict(list), {}

    def record(frame, code, offset):
        instruction = decoded[code].get(offset)
        effects = _effects(instruction) if instruction else ()
        timelines[id(frame)].append((lines[code].get(offset), effects, code.co_name, offset))
        frames[id(frame)] = frame

    if sys.version_info >= (3, 12):
        monitoring = sys.monitoring
        tool = next(
            (each for each in (3, 4) if monitoring.get_tool(each) is None),
            None,
        )
        if tool is None:
            pytest.skip("no free sys.monitoring tool id")
        monitoring.use_tool_id(tool, "peeky-test")
        monitoring.register_callback(
            tool,
            monitoring.events.INSTRUCTION,
            lambda code, offset: record(sys._getframe(1), code, offset),
        )
        for code in codes:
            if code.co_name in functions:
                monitoring.set_local_events(tool, code, monitoring.events.INSTRUCTION)
        try:
            exec(module, {"__name__": "__main__"})
        finally:
            for code in codes:
                monitoring.set_local_events(tool, code, 0)
            monitoring.register_callback(tool, monitoring.events.INSTRUCTION, None)
            monitoring.free_tool_id(tool)
    else:

        def tracer(frame, event, arg):
            if frame.f_code.co_filename != path or frame.f_code.co_name not in functions:
                return None

            def local(frame, event, arg):
                frame.f_trace_opcodes = True
                if event == "opcode":
                    record(frame, frame.f_code, frame.f_lasti)
                return local

            return local

        previous = sys.gettrace()
        sys.settrace(tracer)
        try:
            exec(module, {"__name__": "__main__"})
        finally:
            sys.settrace(previous)
    return timelines


def test_every_note_holds_wherever_the_program_runs_its_line(cases, capsys):
    # Each instruction of a noted line as the program ran it (for a note given only where
    # the line begins, each time the line began): since the loop's header last ran, the
    # local was not stored; what the frame does to it next is a store; and that store
    # comes before the pass ends (the header again, or out of the loop).
    loops = _loops(ast.parse(CASES))
    tree = ast.parse(CASES)
    notes = collections.defaultdict(dict)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "main":
            names = {each.id for each in ast.walk(node) if isinstance(each, ast.Name)} | {
                each.arg for each in ast.walk(node.args) if isinstance(each, ast.arg)
            }
            for line in range(node.lineno, node.end_lineno + 1):
                for name in names:
                    for entered in (False, True):
                        if stale_pass(cases, node.name, line, name, entered=entered):
                            notes[node.name].setdefault((line, name), entered)
    assert sum(map(len, notes.values())) >= 12, dict(notes)
    timelines = _run_traced(cases, set(notes))
    capsys.readouterr()
    checked = 0
    for timeline in timelines.values():
        for index, (line, _, function, offset) in enumerate(timeline):
            for (noted, name), entered in notes[function].items():
                if noted != line:
                    continue
                previous = timeline[index - 1] if index else None
                if entered and previous and previous[0] == line and previous[3] < offset:
                    # Part-way through the line: a breakpoint does not stop there.
                    continue
                first, last, start, _ = max(
                    (loop for loop in loops if loop[2] <= line <= loop[3]),
                    key=lambda loop: loop[2],
                )
                header = range(first, max(first, start - 1) + 1)
                for earlier, effects, _, _ in reversed(timeline[:index]):
                    if earlier in header:
                        break
                    assert ("store", name) not in effects, (function, line, name, index)
                for later, effects, _, _ in timeline[index:]:
                    kinds = [kind for kind, local in effects if local == name]
                    if kinds:
                        assert kinds[0] == "store", (function, line, name, index)
                        break
                    assert later not in header and first <= (later or first) <= last, (
                        "the pass ended first",
                        function,
                        line,
                        name,
                        index,
                    )
                else:
                    raise AssertionError(("never stored", function, line, name, index))
                checked += 1
    assert checked > 100
