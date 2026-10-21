"""#182 against real debugpy: a local the loop's current pass has not assigned yet."""

import json

import pytest

from peeky.cli import main

PROGRAM = """\
import re

key = "module"


def slug(title):
    words = re.split(r"[^a-z0-9]+", title.lower())
    return "-".join(words)


def group(titles):
    counts = {}
    for title in titles:
        key = slug(title)
        if key not in counts:
            counts[key] = 0
        counts[key] += 1
    return counts


if __name__ == "__main__":
    print(group(["Hello World", "Hello, World!", "x y"]))
"""
SLUG, CALL, LOOP = 8, 14, 13
NOTE = (
    "this pass's code has not assigned it before this line, and every path through the "
    f"rest of the pass assigns it (line {CALL}) before reading it: the value is likely "
    f"from an earlier pass (or from before the loop at line {LOOP})"
)


@pytest.fixture
def program(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "articles.py"
    path.write_text(PROGRAM)
    yield path
    main(["session", "close"])


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr()


def rows(capsys, *selectors, frame=0):
    code, out = run(capsys, "session", "get", *selectors, "--frame", frame, "--json")
    return code, {row["selector"]: row for row in json.loads(out.out)}


def test_a_local_the_pass_assigns_below_the_stop_is_noted_as_an_earlier_passs(program, capsys):
    # Eighth evaluation: back in `group` with `step --out` on its second pass, `key`
    # showed the first title's key, with nothing to say it is not this pass's.
    assert run(capsys, "session", "start", program, "--at", SLUG)[0] == 0
    assert run(capsys, "session", "continue")[0] == 0
    # In `slug`, the caller's frame is on the call: the same holds there.
    code, found = rows(capsys, "key", "title", frame=1)
    assert code == 0 and found["key"]["value"] == "'hello-world'", found
    assert found["key"]["note"] == NOTE and "note" not in found["title"]
    code, out = run(capsys, "session", "step", "--out")
    assert code == 0 and f"articles.py:{CALL} in group" in out.out, out
    code, found = rows(capsys, "key", "title", "counts")
    assert code == 0, found
    assert found["key"]["value"] == "'hello-world'" and found["key"]["note"] == NOTE
    assert found["title"]["value"] == "'Hello, World!'" and "note" not in found["title"]
    assert "note" not in found["counts"]
    code, out = run(capsys, "session", "get", "key")
    assert out.out == f"key: str = 'hello-world'\n  note: {NOTE}\n", out.out
    # inspect shows it on the local, never on the module global of that name.
    code, out = run(capsys, "session", "inspect", "--globals", "--json")
    scopes = {scope["name"]: scope for scope in json.loads(out.out)["scopes"]}
    local = next(v for v in scopes["Locals"]["variables"] if v["name"] == "key")
    module = next(v for v in scopes["Globals"]["variables"] if v["name"] == "key")
    assert local["note"] == NOTE and module["value"] == "'module'" and "note" not in module
    code, out = run(capsys, "session", "inspect")
    assert f"  key: str = 'hello-world'\n    note: {NOTE}\n" in out.out, out.out
    # A change would be overwritten before the code reads it: refused, session kept.
    code, out = run(capsys, "session", "set", "key='zz'")
    assert code == 2 and "[MUTATION_REJECTED] 'key' cannot be changed at this stop" in out.err
    assert f"this function's code assigns it again (line {CALL}) before reading it" in out.err
    assert "Change it after that assignment has run" in out.err
    code, out = run(capsys, "session", "set", "title='Rust'")
    assert code == 0, out.err
    # After the assignment, the value is this pass's.
    assert run(capsys, "session", "step")[0] == 0
    code, found = rows(capsys, "key")
    assert found["key"]["value"] == "'hello-world-'" and "note" not in found["key"]


def test_probe_rows_carry_the_note_on_later_passes(program, capsys):
    code, out = run(
        capsys, "probe", program, "--at", CALL, "--capture", "key", "--hits", 3, "--json"
    )
    assert code == 0, out.err
    result = json.loads(out.out)
    first, second, third = result["rows"]
    # The first pass: not assigned yet at all.
    assert "UnboundLocalError" in first["error"] and "note" not in first
    assert second["value"] == "'hello-world'" and second["note"] == NOTE
    assert third["value"] == "'hello-world-'" and third["note"] == NOTE


def test_a_candidates_copy_is_judged_as_the_code_that_runs(program, capsys):
    # The copy a candidate runs is written once the run has begun: it read as "changed
    # after the program started", so nothing was noted (nor any global proven, #104).
    code, out = run(
        capsys,
        "probe",
        program,
        "--at",
        CALL,
        "--capture",
        "key",
        "--hits",
        2,
        "--old",
        "counts[key] += 1",
        "--new",
        "counts[key] += 2",
        "--json",
    )
    assert code == 0, out.err
    first, second = json.loads(out.out)["rows"]
    assert "UnboundLocalError" in first["error"], first
    assert second["value"] == "'hello-world'" and second["note"] == NOTE


# Review of PR #185: each program shows, at the probe's second hit, a value the note
# would have called an earlier pass's.
REVIEWED = {
    # The function that runs is not the one on disk: here `x` is this pass's (20).
    "replaced": (
        """\
def f(xs):
    x = 0
    for t in xs:
        print(t)
        print(t)
        x = t
        print(x)


REPLACEMENT = "def f(xs):\\n    x = 0\\n    for t in xs:\\n        x = t * 10\\n        print(t)\\n        print(x)\\n"
exec(compile(REPLACEMENT, __file__, "exec"))
f([1, 2])
""",
        5,
        "20",
    ),
    # A call wrote its caller's `x` this pass (it writes through from Python 3.13).
    "reached": (
        """\
import sys


def helper(t):
    sys._getframe(1).f_locals["x"] = t * 100


def f(xs):
    x = 0
    for t in xs:
        helper(t)
        print(t)
        x = t
        print(x)


f([1, 2])
""",
        12,
        None,
    ),
    # Stored only if `print` raised a KeyError: not this pass.
    "handled": (
        """\
def f(xs):
    e = 0
    for t in xs:
        try:
            print(t)
        except KeyError:
            e = t
        print("next")


f([1, 2])
""",
        5,
        "0",
    ),
    # Stored only on a branch never taken.
    "unmatched": (
        """\
def f(xs):
    m = 0
    for t in xs:
        print(t)
        match 0:
            case 1:
                m = t
        print("next")


f([1, 2])
""",
        4,
        "0",
    ),
}


@pytest.mark.parametrize("name", sorted(REVIEWED))
def test_no_note_where_the_code_does_not_prove_it(tmp_path, monkeypatch, capsys, name):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    source, line, value = REVIEWED[name]
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    selector = {"handled": "e", "unmatched": "m"}.get(name, "x")
    code, out = run(
        capsys, "probe", path, "--at", line, "--capture", selector, "--hits", 2, "--json"
    )
    assert code == 0, out.err
    second = json.loads(out.out)["rows"][1]
    if value is not None:
        assert second["value"] == value, second
    assert "note" not in second, second


def test_a_loop_bodys_last_line_is_noted_where_a_breakpoint_stops(tmp_path, monkeypatch, capsys):
    # Review of PR #185: `inner = x + y` also holds the jump back to its loop, after its
    # assignment; a breakpoint stops only where the line begins.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "nested.py"
    path.write_text(
        "def f(xs):\n"
        "    total = 0\n"
        "    for x in xs:\n"
        "        for y in range(2):\n"
        "            inner = x + y\n"
        "        total += inner\n"
        "    return total\n"
        "\n"
        "\n"
        "print(f([1, 2]))\n"
    )
    code, out = run(capsys, "probe", path, "--at", 5, "--capture", "inner", "--hits", 3, "--json")
    assert code == 0, out.err
    first, second, third = json.loads(out.out)["rows"]
    assert "note" not in first
    assert (
        second["value"] == "1"
        and "every path through the rest of the pass assigns it (line 5)" in second["note"]
    )
    assert third["value"] == "2" and "before the loop at line 4" in third["note"]
