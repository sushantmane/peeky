"""#182: Python locals a loop's pass has not assigned yet, read from the bytecode."""

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

# `# stale: a, b` marks a line where each name is noted, `# fresh: c` one where it is not.
CASES = """\
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


def continues(ts):
    out = []
    for t in ts:
        slug(str(t))  # stale: y
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
        slug(str(t))  # stale: y
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


def nested(rows):
    total = 0
    for row in rows:
        acc = 0  # stale: acc, cell, v
        for cell in row:
            slug(str(cell))  # stale: v  # fresh: row, acc, cell
            v = cell * 2
            acc += v
        total += acc
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
        u = slug(str(t)); print(u)  # fresh: u
        u = 2
        print(u)


def closure(ts):
    out = []
    for t in ts:
        slug(str(t))  # fresh: c
        c = t
        out.append(lambda: c)
    return [f() for f in out]


def introspect(ts):
    for t in ts:
        slug(str(t))  # fresh: k
        k = t
        print(len(locals()))
        print(k)


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


def main():
    group(["Hello World", "Hello, World!", "x y"])
    whiles(3)
    walrus([1, 2, 3])
    branch_before([1, 2, 3])
    continues([1, 2, 3, 4])
    breaks([1, 2, 3, 4], 0)
    broken([1, 2, 3, 4])
    handler([1, 2, 3])
    nested([[1, 2], [3, 4], []])
    deletes([1, 2])
    condition_reads(3)
    same_line([1, 2])
    closure([1, 2])
    introspect([1, 2])
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
        if isinstance(node, ast.FunctionDef) and node.lineno <= line <= node.end_lineno
    )


def _marked():
    for number, text in enumerate(CASES.splitlines(), 1):
        for kind, names in re.findall(r"# (stale|fresh): ([\w, ]+)", text):
            for name in names.split(","):
                yield number, kind, name.strip()


@pytest.mark.parametrize("line, kind, name", list(_marked()))
def test_a_local_is_noted_only_where_this_pass_assigns_it_before_reading_it(
    cases, line, kind, name
):
    found = stale_pass(cases, _function_at(line), line, name)
    assert (found is not None) == (kind == "stale"), (line, name, found)


def test_the_note_names_the_loop_and_the_assignment(cases):
    line = next(n for n, text in enumerate(CASES.splitlines(), 1) if "key = slug(title)" in text)
    assert stale_pass(cases, "group", line, "key") == (line - 1, (line,))
    note = stale_note(cases, "group", line, "key")
    assert note.note == (
        f"an earlier pass's value (or one from before the loop at line {line - 1}): this "
        f"pass has not assigned it yet, and the code assigns it again (line {line}) before "
        "reading it; stop after that to see this pass's value"
    )
    assert str(note) == (
        f"the code assigns it again (line {line}) before it reads it, so a change made here "
        "would never be seen"
    )
    assert note.refuses is True
    # Two assignments, on two paths: both named.
    line = next(n for n, text in enumerate(CASES.splitlines(), 1) if "if t > 1:  #" in text)
    assert "(lines " in stale_note(cases, "branch_before", line, "y").note


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


def test_a_module_level_loop_is_not_judged(tmp_path):
    path = tmp_path / "top.py"
    path.write_text("for t in range(3):\n    key = str(t)\n    print(key)\n")
    assert stale_pass(str(path), "<module>", 2, "key") is None


def _run_traced(path, functions):
    """{frame id: [(line, effects, function)]}: every instruction each frame of
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
        timelines[id(frame)].append((lines[code].get(offset), effects, code.co_name))
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
    # Each instruction of a noted line, as the program ran it: since the loop's header
    # last ran, the local was not stored, and what the frame does to it next is a store.
    loops = _loops(ast.parse(CASES))
    tree = ast.parse(CASES)
    notes = collections.defaultdict(dict)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name != "main":
            names = {each.id for each in ast.walk(node) if isinstance(each, ast.Name)} | {
                each.arg for each in ast.walk(node.args) if isinstance(each, ast.arg)
            }
            for line in range(node.lineno, node.end_lineno + 1):
                for name in names:
                    if found := stale_pass(cases, node.name, line, name):
                        notes[node.name][(line, name)] = found
    assert sum(map(len, notes.values())) >= 15, dict(notes)
    timelines = _run_traced(cases, set(notes))
    capsys.readouterr()
    checked = 0
    for timeline in timelines.values():
        for index, (line, _, function) in enumerate(timeline):
            for (noted, name), _ in notes[function].items():
                if noted != line:
                    continue
                first, _, start, _ = max(
                    (loop for loop in loops if loop[2] <= line <= loop[3]),
                    key=lambda loop: loop[2],
                )
                header = range(first, max(first, start - 1) + 1)
                for earlier, effects, _ in reversed(timeline[:index]):
                    if earlier in header:
                        break
                    assert ("store", name) not in effects, (function, line, name, index)
                for _, effects, _ in timeline[index:]:
                    kinds = [kind for kind, local in effects if local == name]
                    if kinds:
                        assert kinds[0] == "store", (function, line, name, index)
                        break
                checked += 1
    assert checked > 100
