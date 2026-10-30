"""Against real debugpy: caller frames, module globals and object identity."""

import json
import os
import re
import shutil
import sys
import time

import pytest

from peeky.cli import main
from peeky.errors import classify

PROGRAM = """\
LIMIT = [1, 2]


def normalize(word, seen=[]):
    key = word.lower()
    seen.append(key)
    return key


def tags(words):
    same = [0]
    alias = same
    equal = [0]
    out = []
    for word in words:
        out.append(normalize(word))
    return out, alias, equal


def shadow():
    print(LIMIT)
    LIMIT = 1
    return LIMIT


if __name__ == "__main__":
    print(tags(["A", "B"]))
    try:
        shadow()
    except UnboundLocalError:
        print("unbound")
"""
RETURN_KEY, CALL, PRINT_LIMIT = 7, 16, 21


@pytest.fixture
def program(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "tags.py"
    path.write_text(PROGRAM)
    yield path
    main(["session", "close"])


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr()


def rows(capsys, *selectors, frame=0):
    code, out = run(capsys, "session", "get", *selectors, "--frame", frame, "--json")
    return code, {row["selector"]: row for row in json.loads(out.out)}


def test_frame_option_reads_a_callers_locals_and_refuses_past_the_stack(program, capsys):
    assert run(capsys, "session", "start", program, "--at", RETURN_KEY)[0] == 0
    code, out = run(capsys, "session", "inspect", "--frame", 1)
    assert code == 0, out.err
    lines = out.out.splitlines()
    assert lines[0].startswith("stopped at ") and "tags.py:7 in normalize, hit 1" in lines[0]
    assert lines[1] == f"frame 1: tags ({program}:{CALL})"
    assert "  same: list = [0]" in out.out and "  key:" not in out.out
    snapshot = json.loads(run(capsys, "session", "inspect", "--frame", 1, "--json")[1].out)
    assert snapshot["frame"] == 1 and snapshot["function"] == "tags"
    assert snapshot["innermost"]["function"] == "normalize"
    assert [frame["name"] for frame in snapshot["frames"]] == ["normalize", "tags", "<module>"]
    code, found = rows(capsys, "words", "word", "out", frame=1)
    assert code == 0 and found["words"]["value"] == "['A', 'B']"
    assert found["word"]["value"] == "'A'" and found["out"]["value"] == "[]"
    assert found["words"]["frame"] == 1 and found["words"]["function"] == "tags"
    assert found["words"]["line"] == CALL
    code, out = run(capsys, "session", "get", "--frame", 1, "words")
    assert out.out.startswith(f"frame 1: tags ({program}:{CALL})\nwords: list = ['A', 'B']")
    # The innermost frame is still the default.
    assert rows(capsys, "key")[1]["key"]["value"] == "'a'"
    for action in (("inspect",), ("get", "words")):
        code, out = run(capsys, "session", *action, "--frame", 3)
        assert code == 2 and "[INVALID_REQUEST] Frame 3 is out of range" in out.err
        assert "this stop lists 3 frame(s), 0 to 2" in out.err
    # The session is still usable.
    assert rows(capsys, "word", frame=2)[1]["word"]["error"].startswith("not found")


def test_python_inspect_lists_module_globals_on_request_and_once(program, capsys):
    assert run(capsys, "session", "start", program, "--at", RETURN_KEY)[0] == 0
    # Listing globals runs their reprs, and a client that predates them
    # resolves names across every scope it gets: they come only on request.
    snapshot = json.loads(run(capsys, "session", "inspect", "--json")[1].out)
    assert [scope["name"] for scope in snapshot["scopes"]] == ["Locals"]
    assert snapshot["globals_available"] is True
    snapshot = json.loads(run(capsys, "session", "inspect", "--globals", "--json")[1].out)
    locals_, globals_ = snapshot["scopes"]
    assert (locals_["name"], globals_["name"]) == ("Locals", "Globals")
    assert globals_["globals"] is True and "globals" not in locals_
    shown = {variable["name"]: variable for variable in globals_["variables"]}
    assert shown["LIMIT"]["value"] == "[1, 2]"
    # debugpy files functions and dunders under its groups; they are not values.
    assert {"special variables", "function variables"} <= set(shown)
    assert all(
        "identity" not in shown[name] for name in ("special variables", "function variables")
    )
    text = run(capsys, "session", "inspect", "--globals")[1].out
    assert "[Globals] (module globals, read-only)" in text
    assert "[Globals]" not in run(capsys, "session", "inspect")[1].out
    # Module code runs with its globals as its locals: listed once.
    code, out = run(capsys, "session", "inspect", "--frame", 2, "--globals", "--json")
    module = json.loads(out.out)
    assert [scope["name"] for scope in module["scopes"]] == ["Locals"]
    code, out = run(capsys, "session", "inspect", "--frame", 2, "--globals")
    assert "note: module globals not listed: at module level the locals are" in out.out
    code, out = run(capsys, "session", "set", "LIMIT=[3]")
    assert code == 2 and "[NOT_FOUND]" in out.err and "'LIMIT' is not a visible local" in out.err


def test_globals_are_read_when_proven_and_never_for_an_unassigned_local(program, capsys):
    assert run(capsys, "session", "start", program, "--at", RETURN_KEY)[0] == 0
    code, found = rows(capsys, "LIMIT", "normalize.__defaults__", "normalize.__name__")
    assert code == 0, found
    assert found["LIMIT"]["value"] == "[1, 2]" and found["LIMIT"]["scope"] == "globals"
    # A function's dunder attributes sit in debugpy's `special variables` group.
    assert found["normalize.__defaults__"]["value"] == "(['a'],)"
    assert found["normalize.__name__"]["value"] == "'normalize'"
    code, out = run(capsys, "session", "get", "LIMIT")
    assert re.search(r"^LIMIT: list = \[1, 2\]  \[id \d+\]  \[module global\]$", out.out, re.M)
    # A source edited after the start may not be the code running: nothing is proven.
    later = time.time() + 5
    os.utime(program, (later, later))
    code, found = rows(capsys, "LIMIT")
    assert code == 1 and "value" not in found["LIMIT"]
    assert "It may be a module global" in found["LIMIT"]["error"]
    assert "changed after the program started" in found["LIMIT"]["error"]
    run(capsys, "session", "close")
    earlier = time.time() - 5
    os.utime(program, (earlier, earlier))
    assert run(capsys, "session", "start", program, "--at", PRINT_LIMIT)[0] == 0
    # `print(LIMIT)` before `LIMIT = 1` raises UnboundLocalError: the global is not it.
    code, found = rows(capsys, "LIMIT")
    assert code == 1 and "value" not in found["LIMIT"]
    error = found["LIMIT"]["error"]
    assert error.startswith("not found: 'LIMIT' is a local of shadow with no value at this line")
    assert "UnboundLocalError" in error


def test_a_source_replaced_by_an_older_file_proves_nothing(program, capsys, tmp_path):
    # `cp -p` gives the replacement its old mtime, so the running `f`
    # (which binds X) was judged from a file in which it did not.
    running = "X = 'global'\n\n\ndef f():\n    print(1)\n    X = 2\n    return X\n\n\nf()\n"
    alternative = running.replace("    X = 2\n    return X\n", "    return 2\n")
    source, copy = tmp_path / "mt.py", tmp_path / "mt_alt.py"
    source.write_text(running)
    copy.write_text(alternative)
    old = time.time() - 86400
    os.utime(copy, (old, old))
    os.utime(source, (old, old))
    assert run(capsys, "session", "start", source, "--at", 5)[0] == 0
    code, found = rows(capsys, "X")
    assert "UnboundLocalError" in found["X"]["error"]
    time.sleep(0.05)
    shutil.copy2(copy, source)
    assert source.stat().st_mtime < time.time() - 3600
    code, found = rows(capsys, "X")
    assert code == 1 and "value" not in found["X"]
    assert "changed after the program started" in found["X"]["error"]


def test_reading_a_local_runs_no_globals_repr_and_parses_no_source(tmp_path, monkeypatch):
    # Every inspection listed the globals (running each one's __repr__
    # in the program) and parsed the module to prove names nobody asked for.
    import symtable

    from peeky.probe import Probe, run_probe

    (tmp_path / "r1.py").write_text(LOUD)
    parsed = []
    original = symtable.symtable
    monkeypatch.setattr(symtable, "symtable", lambda *a: parsed.append(a) or original(*a))
    result = run_probe(tmp_path, Probe("r1.py", 18, ("total",), hits=3))
    assert result["status"] == "completed", result.get("error")
    assert [row["value"] for row in result["rows"]] == ["1", "2", "3"]
    assert "repr calls: 0" in result["output"] and not parsed
    # A global asked for by name is proven, then read, as reading a local runs its repr.
    result = run_probe(tmp_path, Probe("r1.py", 18, ("HANDLE",), hits=1))
    assert result["rows"][0]["value"] == "<Loud>" and result["rows"][0]["scope"] == "globals"
    assert len(parsed) == 1


LOUD = """\
import sys


class Loud:
    calls = 0

    def __repr__(self):
        Loud.calls += 1
        return "<Loud>"


HANDLE = Loud()


def work(n):
    total = n + 1
    return total


for i in range(3):
    work(i)
print("repr calls:", Loud.calls)
"""


def test_a_local_debugpy_groups_with_functions_is_set_as_a_local(tmp_path, monkeypatch, capsys):
    # `set callback=None` said "'callback' is a module global".
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "s1.py"
    path.write_text(
        'callback = "global-cb"\n\n\ndef f():\n    callback = print\n    return 1\n\n\nf()\n'
    )
    assert run(capsys, "session", "start", path, "--at", 6)[0] == 0
    try:
        code, out = run(capsys, "session", "set", "callback=None")
        assert code == 0, out.err
        assert out.out.strip() == "set callback: <built-in function print> -> None"
        assert rows(capsys, "callback")[1]["callback"]["value"] == "None"
    finally:
        run(capsys, "session", "close")


def test_names_past_the_listing_bound_are_read_by_name(tmp_path, monkeypatch, capsys):
    # `get zz_last` said "not found" for a global past the 50-entry cut.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "many.py"
    globals_ = "".join(f"g{i:02} = 'global-{i}'\n" for i in range(60))
    locals_ = "".join(f"    l{i:02} = {i}\n" for i in range(55))
    path.write_text(
        f"{globals_}zz_last = 'global-zz'\n\ndef f():\n{locals_}"
        "    zz_local = 'local-zz'\n    return l00\n\nf()\n"
    )
    assert run(capsys, "session", "start", path, "--at", 120)[0] == 0
    try:
        code, found = rows(capsys, "zz_last", "zz_local", "l54", "g59", "nowhere")
        assert found["zz_last"]["value"] == "'global-zz'"
        assert found["zz_last"]["scope"] == "globals"
        assert found["zz_local"]["value"] == "'local-zz'" and found["l54"]["value"] == "54"
        assert found["g59"]["value"] == "'global-59'"
        assert found["nowhere"]["error"].startswith("not found: 'nowhere' is neither")
        text = run(capsys, "session", "inspect")[1].out
        assert "  ... and 6 more, not listed (at most 50 per scope)" in text
    finally:
        run(capsys, "session", "close")


CALLER = "def callee():\n    return 0\n\n\ndef caller():\n    n = 2\n    r = n + callee()\n    return n * 10, r\n\n\nprint(caller())\n"


def test_a_callers_locals_are_read_only_and_globals_come_only_on_request(tmp_path):
    # set_variable on `inspect {"frame": 1}` changed the caller's `n`;
    # and a client that predates globals resolved names across every scope it got.
    from peeky.worker import DebugWorker

    (tmp_path / "m1.py").write_text(CALLER)
    worker = DebugWorker(tmp_path)
    launched = worker.dispatch("launch", {"program": "m1.py", "breakpoints": [{"line": 2}]})
    session = {"session_id": launched["session_id"]}
    try:
        state = worker.dispatch("wait", {**session, "timeout": 10})
        stop = {**session, "stop_id": state["stop_id"]}
        own = worker.dispatch("inspect", stop)
        assert [scope["name"] for scope in own["scopes"]] == ["Locals"]
        caller = worker.dispatch("inspect", {**stop, "frame": 1})
        assert [scope["name"] for scope in caller["scopes"]] == ["Locals"]
        reference = caller["scopes"][0]["reference"]
        with pytest.raises(ValueError, match="'n' is in a caller's frame") as caught:
            worker.dispatch(
                "set_variable", {**stop, "reference": reference, "name": "n", "value": "5"}
            )
        assert classify(caught.value)["code"] == "MUTATION_REJECTED"
        worker.dispatch("continue", stop)
        assert worker.dispatch("wait", {**session, "timeout": 10})["state"] == "terminated"
        assert "(20, 2)" in worker.dispatch("report", session)["output"]
    finally:
        worker.dispatch("close", session)


def test_python_identity_tells_the_same_object_from_an_equal_one(program, capsys):
    assert run(capsys, "session", "start", program, "--at", RETURN_KEY)[0] == 0
    code, caller = rows(capsys, "same", "alias", "equal", "words", frame=1)
    assert code == 0
    assert caller["same"]["identity"] == caller["alias"]["identity"]
    assert type(caller["same"]["identity"]) is int
    assert caller["same"]["value"] == caller["equal"]["value"] == "[0]"
    assert caller["same"]["identity"] != caller["equal"]["identity"]
    code, here = rows(capsys, "seen", "normalize.__defaults__[0]", "key", "word")
    first = here["seen"]["identity"]
    assert here["normalize.__defaults__[0]"]["identity"] == first
    assert "identity" not in here["key"] and "identity" not in here["word"]
    # The default list is read again on the second call: the same object.
    code, out = run(capsys, "session", "continue")
    assert code == 0 and "hit 2" in out.out
    code, again = rows(capsys, "seen")
    assert again["seen"]["value"] == "['a', 'b']" and again["seen"]["identity"] == first
    # Children carry theirs too.
    code, out = run(capsys, "session", "get", "--frame", 1, "same", "--json")
    assert json.loads(out.out)[0]["children"][0] == {
        "name": "0",
        "type": "int",
        "value": "0",
        "selector": "same[0]",
    }


def test_probe_rows_store_identity_for_sql(program, capsys, tmp_path):
    store = tmp_path / "evidence.db"
    capture = "seen,normalize.__defaults__[0],key"
    args = ("probe", program, "--at", RETURN_KEY, "--capture", capture, "--hits", 2)
    code, out = run(capsys, *args, "--store", store)
    assert code == 0, out.out + out.err
    assert re.search(r"\[id \d+\]", out.out)
    code, out = run(
        capsys,
        "sql",
        "SELECT COUNT(DISTINCT identity) AS objects, COUNT(identity) AS refs, "
        "SUM(identity IS NULL) AS scalars FROM last_run",
        "--store",
        store,
        "--json",
    )
    assert json.loads(out.out)["rows"] == [{"objects": 1, "refs": 4, "scalars": 2}]
    code, out = run(
        capsys,
        "sql",
        "SELECT s.hit, s.identity = d.identity AS same FROM last_run s JOIN last_run d "
        "ON d.hit = s.hit AND d.selector = 'normalize.__defaults__[0]' "
        "WHERE s.selector = 'seen' ORDER BY s.hit",
        "--store",
        store,
        "--json",
    )
    assert json.loads(out.out)["rows"] == [{"hit": 1, "same": 1}, {"hit": 2, "same": 1}]
    code, out = run(
        capsys, "probe", program, "--at", 14, "--capture", "same,alias,equal", "--store", store
    )
    assert code == 0
    code, out = run(
        capsys,
        "sql",
        "SELECT a.identity = b.identity AS aliased, a.identity = e.identity AS equal_is_same "
        "FROM last_run a JOIN last_run b ON b.selector = 'alias' "
        "JOIN last_run e ON e.selector = 'equal' WHERE a.selector = 'same'",
        "--store",
        store,
        "--json",
    )
    assert json.loads(out.out)["rows"] == [{"aliased": 1, "equal_is_same": 0}]


GENERATED = """\
X = "global-x"
Y = "global-y"
G = "global-g"


def f(data):
    squares = [X for X in range(3)]
    total = sum(Y for Y in range(3))
    inside = [
        X * 2
        for X in data
    ]
    return X, Y, G, squares, total, inside


print(f([1, 2]))
"""


def test_a_global_read_beside_a_generator_of_that_name_is_read(tmp_path):
    # `sum(Y for Y in ...)` runs in its own frame, and f reads
    # the global Y (the program prints it), but the probe refused it as a comprehension's.
    from peeky.probe import Probe, run_probe

    (tmp_path / "c2.py").write_text(GENERATED)
    result = run_probe(tmp_path, Probe("c2.py", 13, ("Y", "X", "squares"), hits=1))
    assert result["status"] == "completed", result.get("error")
    values = {row["selector"]: row.get("value", row.get("error")) for row in result["rows"]}
    assert values == {"Y": "'global-y'", "X": "'global-x'", "squares": "[0, 1, 2]"}
    assert "'global-x', 'global-y'" in result["output"]


@pytest.mark.skipif(sys.version_info < (3, 12), reason="comprehensions run in their own frame")
def test_a_global_read_from_inside_an_inlined_comprehension(tmp_path):
    # Inside `[X * 2 for X in data]` (inlined from 3.12), X is
    # listed though f reads the global X, and `G` was refused as "the file may have
    # changed since it was loaded".
    from peeky.probe import Probe, run_probe

    (tmp_path / "c4.py").write_text(GENERATED)
    result = run_probe(tmp_path, Probe("c4.py", 10, ("G", "X"), hits=1))
    assert result["status"] == "completed", result.get("error")
    values = [row.get("value", row.get("error")) for row in result["rows"]]
    assert values == ["'global-g'", "1"], result["rows"]
