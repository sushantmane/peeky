"""#382 against real debugpy: a probe's `1:i` reads `i` in the caller's frame, beside the
stopped frame's values, with the rules `session get --frame 1` applies, in one run."""

import json

import pytest

from peeky.cli import main

PROGRAM = """\
def add_reading(totals, reading):
    totals.append(reading)


def ward(readings):
    totals = []
    for i, reading in enumerate(readings):
        add_reading(totals, reading * 2)
        last = reading
    return totals, last


print(ward([3, 5, 7]))
"""
APPEND, CALL, MODULE = 2, 8, 13


@pytest.fixture
def program(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "ward.py"
    path.write_text(PROGRAM)
    return path


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr()


def test_one_probe_reads_the_callers_iteration_beside_the_callees_value(program, capsys):
    # Thirteenth evaluation: which pass of ward's loop each add_reading hit belonged to took
    # a second probe at ward's line and a join of the two runs by hit number.
    code, out = run(
        capsys, "probe", program, "--at", APPEND, "--hits", "all", "--json",
        "--capture", "reading,1:i,1:reading,1:last,2:readings,3:x",
    )  # fmt: skip
    result = json.loads(out.out)
    assert code == 0 and result["status"] == "completed", result.get("error")
    assert result["output"].strip() == "([6, 10, 14], 7)"
    rows = {(row["hit"], row["selector"]): row for row in result["rows"]}
    assert [rows[hit, "1:i"]["value"] for hit in (1, 2, 3)] == ["0", "1", "2"]
    assert [rows[hit, "1:reading"]["value"] for hit in (1, 2, 3)] == ["3", "5", "7"]
    assert [rows[hit, "reading"]["value"] for hit in (1, 2, 3)] == ["6", "10", "14"]
    caller = rows[1, "1:i"]
    assert (caller["frame"], caller["function"], caller["line"]) == (1, "ward", CALL)
    assert caller["stop_line"] == APPEND and caller["location"].endswith(f"ward.py:{CALL}")
    assert "frame" not in rows[1, "reading"] and rows[1, "reading"]["function"] == "add_reading"
    # The caller's rules hold: `last` is unassigned at the first pass, and later holds the
    # pass before's value, with the note `session get --frame 1 last` gives.
    assert "reading it here raises UnboundLocalError" in rows[1, "1:last"]["error"]
    assert rows[2, "1:last"]["value"] == "3"
    assert "likely from an earlier pass" in rows[2, "1:last"]["note"]
    # Frame 2 is the module, which names no `readings`; frame 3 is past the stack. Each is
    # its row's error, and the probe goes on.
    assert rows[1, "2:readings"]["function"] == "<module>"
    assert rows[1, "2:readings"]["line"] == MODULE
    assert "not found" in rows[1, "2:readings"]["error"]
    assert rows[1, "3:x"]["error"].startswith("Frame 3 is out of range: this stop lists 3")
    assert [(hit["line"], hit["function"]) for hit in result["by_hit"]] == [
        (APPEND, "add_reading")
    ] * 3
    # As a session reads the same stop's caller.
    assert run(capsys, "session", "start", program, "--at", APPEND, "--hit", 2)[0] == 0
    try:
        code, out = run(capsys, "session", "get", "--frame", 1, "i", "last", "--json")
        read = {row["selector"]: row for row in json.loads(out.out)}
        assert (read["i"]["value"], read["last"]["value"]) == ("1", "3")
        assert read["last"]["note"] == rows[2, "1:last"]["note"]
    finally:
        run(capsys, "session", "close")
    # The store keeps each row's frame; `wide` gives each hit the stopped frame's line.
    code, out = run(
        capsys, "sql", "--json",
        "SELECT hit, frame, function, line, value FROM last_run WHERE selector = '1:i'",
    )  # fmt: skip
    assert json.loads(out.out)["rows"] == [
        {"hit": hit, "frame": 1, "function": "ward", "line": CALL, "value": str(hit - 1)}
        for hit in (1, 2, 3)
    ]
    code, out = run(capsys, "queries", "wide", "--json")
    wide = json.loads(out.out)["rows"]
    assert [(row["line"], row["1:i"], row["reading"]) for row in wide] == [
        (APPEND, "0", "6"),
        (APPEND, "1", "10"),
        (APPEND, "2", "14"),
    ]


def test_the_text_table_shows_the_frame_each_value_came_from(program, capsys):
    code, out = run(capsys, "probe", program, "--at", APPEND, "--capture", "reading,1:i")
    assert code == 0, out.err
    lines = out.out.splitlines()
    assert lines[1].split() == ["1", "observed", str(APPEND), "add_reading", "reading", "int", "6"]
    assert lines[2].split() == ["1", "observed", str(CALL), "ward", "1:i", "int", "0"]


def test_a_caller_is_never_changed_and_a_guess_is_told_the_spelling(program, capsys):
    code, out = run(capsys, "probe", program, "--at", APPEND, "--set", "1:i=5", "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "INVALID_REQUEST", error
    assert "a change is made only where the program stopped" in error["message"]
    code, out = run(capsys, "probe", program, "--at", APPEND, "--capture", "i@1", "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and "a probe reads i in caller frame 1 as 1:i" in error["message"], error
