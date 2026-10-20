"""#104 against real debugpy: caller frames, module globals and object identity."""

import json
import os
import time

import pytest

from peeky.cli import main

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


def test_python_inspect_lists_module_globals_once(program, capsys):
    assert run(capsys, "session", "start", program, "--at", RETURN_KEY)[0] == 0
    snapshot = json.loads(run(capsys, "session", "inspect", "--json")[1].out)
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
    assert "[Globals] (module globals, read-only)" in run(capsys, "session", "inspect")[1].out
    # Module code runs with its globals as its locals: listed once.
    module = json.loads(run(capsys, "session", "inspect", "--frame", 2, "--json")[1].out)
    assert [scope["name"] for scope in module["scopes"]] == ["Locals"]
    code, out = run(capsys, "session", "set", "LIMIT=[3]")
    assert code == 2 and "[MUTATION_REJECTED]" in out.err and "module global" in out.err


def test_globals_are_read_when_proven_and_never_for_an_unassigned_local(program, capsys):
    assert run(capsys, "session", "start", program, "--at", RETURN_KEY)[0] == 0
    code, found = rows(capsys, "LIMIT", "normalize.__defaults__", "normalize.__name__")
    assert code == 0, found
    assert found["LIMIT"]["value"] == "[1, 2]" and found["LIMIT"]["scope"] == "globals"
    # A function's dunder attributes sit in debugpy's `special variables` group.
    assert found["normalize.__defaults__"]["value"] == "(['a'],)"
    assert found["normalize.__name__"]["value"] == "'normalize'"
    code, out = run(capsys, "session", "get", "LIMIT")
    assert "LIMIT: list = [1, 2]  [id list@" in out.out and "[module global]" in out.out
    # A source edited after the start may not be the code running: nothing is proven.
    later = time.time() + 5
    os.utime(program, (later, later))
    code, found = rows(capsys, "LIMIT")
    assert code == 1 and "value" not in found["LIMIT"]
    assert "may be the module global 'LIMIT'" in found["LIMIT"]["error"]
    assert "changed after the program started" in found["LIMIT"]["error"]
    run(capsys, "session", "close")
    earlier = time.time() - 5
    os.utime(program, (earlier, earlier))
    assert run(capsys, "session", "start", program, "--at", PRINT_LIMIT)[0] == 0
    # `print(LIMIT)` before `LIMIT = 1` raises UnboundLocalError: the global is not it.
    code, found = rows(capsys, "LIMIT")
    assert code == 1 and "value" not in found["LIMIT"]
    error = found["LIMIT"]["error"]
    assert error.startswith("not found: 'LIMIT' has no value in this frame at this line")
    assert "UnboundLocalError" in error and "shadow binds it" in error


def test_python_identity_tells_the_same_object_from_an_equal_one(program, capsys):
    assert run(capsys, "session", "start", program, "--at", RETURN_KEY)[0] == 0
    code, caller = rows(capsys, "same", "alias", "equal", "words", frame=1)
    assert code == 0
    assert caller["same"]["identity"] == caller["alias"]["identity"]
    assert caller["same"]["identity"].startswith("list@")
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
    assert "[id list@" in out.out
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
