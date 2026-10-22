"""The ninth agent evaluation's Lows that need no debugger: CLI text, `describe`, and
the evidence store's queries."""

import json

import pytest

from peeky import evidence
from peeky.cli import _get_lines, main
from peeky.probe import Probe

SESSIONS = """import sys
from dataclasses import dataclass

MAX_GAP = 30
LIMIT: int = 5


@dataclass
class Session:
    start: int
    last: int


def sessionize(events):
    current = {}
    for event in events:
        session = current.get(event)
        gap = 1 if session else None
        if session is None or gap > MAX_GAP:
            current[event] = Session(start=0, last=0)
        print(len(current), file=sys.stderr)
    return current


def counter():
    global LIMIT
    LIMIT += 1
    return LIMIT
"""


def test_describe_lists_the_module_globals_a_function_reads(tmp_path, capsys):
    # sessions 5: `describe` listed `sessionize`'s locals but not `MAX_GAP`, which a
    # probe there reads (`MAX_GAP [module global]`). Names the module itself assigns:
    # not `sys`, `Session`, `print` or `len`.
    path = tmp_path / "sessions.py"
    path.write_text(SESSIONS)
    assert main(["describe", str(path)]) == 0
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("names in sessionize"))
    assert line.endswith(": current, event, events, gap, session, MAX_GAP (module global)"), line
    line = next(line for line in out.splitlines() if line.startswith("names in counter"))
    assert line.endswith(": LIMIT (module global)"), line
    assert main(["describe", str(path), "--json"]) == 0
    scopes = {s["scope"]: s for s in json.loads(capsys.readouterr().out)["scopes"]}
    assert scopes["sessionize"]["globals"] == ["MAX_GAP"]
    assert "globals" not in scopes["<module>"]


def test_session_get_hints_quote_a_selector_as_people_would():
    # sessions 6: `peeky session get 'history['"'"'alice'"'"'][0]'`.
    row = {
        "selector": "history['alice']",
        "type": "list",
        "value": "[Session(start=540, last=560)]",
        "children": [
            {"name": "[0]", "selector": "history['alice'][0]", "value": "S", "expandable": True},
            {"name": "[1]", "selector": 'm["$x"]', "value": "S", "expandable": True},
            {"name": "x", "selector": "plain.x", "value": "1", "expandable": True},
        ],
    }
    lines = _get_lines(row)
    assert lines[1].endswith("(children: peeky session get \"history['alice'][0]\")"), lines
    # A `$` or a `"` keeps shlex's quoting; a plain selector none.
    assert lines[2].endswith("(children: peeky session get 'm[\"$x\"]')"), lines
    assert lines[3].endswith("(children: peeky session get plain.x)"), lines


def test_debugpys_grouping_note_comes_only_where_nothing_else_is_listed():
    # sessions 7: every dataclass `get` said that debugpy groups more members under
    # `special variables`.
    grouped = {"selector": "s", "type": "Session", "value": "Session(start=1, last=2)"}
    grouped["children_grouped"] = ["special variables"]
    with_fields = {
        **grouped,
        "children": [{"name": "start", "selector": "s.start", "value": "1"}],
    }
    assert not any("debugpy groups" in line for line in _get_lines(with_fields))
    assert any("debugpy groups more members" in line for line in _get_lines(grouped))


def test_a_set_hit_past_the_captured_hits_says_what_to_do():
    # Preparing the ninth evaluation: `--set gap=20 --set-hit 4` gave `Probe set_hit
    # must be within 1..1`, an internal name, with no way out.
    with pytest.raises(ValueError) as caught:
        Probe("sessions.py", 34, ("gap",), set=(("gap", "20"),), set_hit=4)
    assert str(caught.value) == (
        "--set-hit 4 is past the last hit this probe captures (1, --hits 1): raise --hits "
        "to 4 or more, or use --hits all; or --set-at 34@4 NAME=VALUE changes it at that "
        "hit without capturing there"
    )
    with pytest.raises(ValueError, match=r"^--set-hit must be within 1\.\.1$"):
        Probe("sessions.py", 34, ("gap",), set=(("gap", "20"),), set_hit=0)


def result(sets=(), set_at=(), set_hit=1, output="out\n"):
    return {
        "probe": {
            "program": "a.py",
            "line": 34,
            "capture": ["gap"],
            "set": [list(change) for change in sets],
            "set_at": [list(point) for point in set_at],
            "set_hit": set_hit,
            "hits": 6,
        },
        "status": "completed",
        "hits": 6,
        "rows": [dict(hit=1, phase="observed", line=34, selector="gap", value="1")],
        "output": output,
    }


@pytest.fixture
def store(tmp_path):
    return tmp_path / "evidence" / "evidence.db"


def test_outputs_and_diff_say_where_each_change_was_requested(store):
    # sessions 4: `queries outputs` showed `set_hit NULL` for `--set-at` runs, which
    # reads as no change; rechnung 5: `diff`'s `(sets)` row gave `[["gleich", "true"]]`
    # with no hit, so the same --set at hits 3 and 4 differed in nothing it showed.
    # runs.sets holds requests (review of PR #214): see test_probe for one not made.
    evidence.record(store, result(set_at=((35, 4, "gap", "20"), (35, 6, "gap", "20"))))
    evidence.record(store, result(sets=(("gap", "20"),), set_hit=4))
    outputs = evidence.run_query(store, "outputs")["rows"]
    assert [json.loads(row["sets"]) for row in outputs] == [
        [["gap", "20", "requested at 35@4"], ["gap", "20", "requested at 35@6"]],
        [["gap", "20", "requested at 34@4"]],
    ]
    assert "set_hit" not in outputs[0]
    evidence.record(store, result(sets=(("gap", "20"),), set_hit=3))
    (row,) = [r for r in evidence.run_query(store, "diff")["rows"] if r["selector"] == "(sets)"]
    assert (json.loads(row["value_a"]), json.loads(row["value_b"])) == (
        [["gap", "20", "requested at 34@4"]],
        [["gap", "20", "requested at 34@3"]],
    )
    # No intervention: `[]`, as before.
    evidence.record(store, result())
    (row,) = [r for r in evidence.run_query(store, "diff")["rows"] if r["selector"] == "(sets)"]
    assert row["value_b"] == "[]"


def test_session_states_name_files_as_probes_do(tmp_path, monkeypatch):
    # rechnung 6: a session's state said `stopped at /…/rechnung/app/Rechnung.java:47`
    # where its probes said `app/Rechnung.java:47`. Text only; JSON keeps the path whole.
    from peeky.cli import _read_frame, _state_text

    (tmp_path / "app").mkdir()
    source = tmp_path / "app" / "Rechnung.java"
    source.write_text("class Rechnung {}\n")
    monkeypatch.chdir(tmp_path)
    state = {"state": "stopped", "location": f"{source}:47", "function": "Rechnung.main()"}
    assert _state_text(state).startswith("state: stopped at app/Rechnung.java:47 in Rechnung.main")
    frame = {"frame": 1, "function": "Rechnung.main()", "location": f"{source}:12"}
    assert _read_frame(frame) == "frame 1: Rechnung.main() (app/Rechnung.java:12)"
    # A file outside the working directory keeps its whole path.
    outside = {**state, "location": "/elsewhere/Other.java:3"}
    assert "stopped at /elsewhere/Other.java:3" in _state_text(outside)
