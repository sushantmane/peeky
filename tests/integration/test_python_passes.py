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
    f"an earlier pass's value (or one from before the loop at line {LOOP}): this pass has "
    f"not assigned it yet, and the code assigns it again (line {CALL}) before reading it; "
    "stop after that to see this pass's value"
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
    assert f"the code assigns it again (line {CALL}) before it reads it" in out.err
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
