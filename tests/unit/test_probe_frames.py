"""#382, with fakes: a probe's `1:i` reads `i` in caller frame 1, as `session get --frame 1`
does, beside the stopped frame's values; its rows say which frame, and the store keeps it."""

import pytest

import peeky.probe as probe_module
from peeky import evidence
from peeky.probe import Probe, _by_hit, parse_selector

STOPPED = {"id": 1, "name": "add_reading", "line": 2, "source": {"path": "/w/main.py"}}
CALLER = {"id": 2, "name": "ward", "line": 9, "source": {"path": "/w/main.py"}}


class Worker:
    """One stop in add_reading, called from ward: frame 0 holds `reading`, frame 1 `i`
    (the hit's index) and `reading` (another one); there is no frame 2."""

    def __init__(self, hits=2):
        self.hits, self.stops, self.inspected = hits, 0, []
        self.sets = []

    def dispatch(self, operation, args):
        if operation == "launch":
            return {
                "session_id": "s",
                "breakpoints": [{"id": 1, "verified": True, "line": 2}],
                "artifact_manifest_id": None,
                "target_identity": {"language": "python"},
                "source_sha256": "x",
            }
        if operation == "wait":
            if self.stops == self.hits:
                return {"state": "terminated"}
            self.stops += 1
            return {"state": "stopped", "stop_id": f"t{self.stops}", "event": {"body": {}}}
        if operation == "inspect":
            frame = args.get("frame", 0)
            self.inspected.append(frame)
            if frame >= 2:
                raise ValueError(
                    f"Frame {frame} is out of range: this stop lists 2 frame(s), 0 to 1 (0 is "
                    "the innermost; at most 64 are listed)"
                )
            value = str(self.stops * 10 + (5 if self.sets else 0))
            variables = (
                [{"name": "reading", "type": "int", "value": value, "variablesReference": 0}]
                if frame == 0
                else [
                    {"name": "i", "type": "int", "value": str(self.stops), "variablesReference": 0},
                    {"name": "reading", "type": "int", "value": "3", "variablesReference": 0},
                ]
            )
            return {
                "frames": [STOPPED, CALLER],
                "frame": frame,
                **({"location": "/w/main.py:9"} if frame else {}),
                "scopes": [{"name": "Locals", "reference": 1, "variables": variables}],
            }
        if operation == "set_variable":
            self.sets.append(args)
            return {"value": args["value"]}
        if operation == "report":
            return {"output": "", "output_truncated": False}
        if operation == "continue":
            return {"state": "running", "resumed": True}
        return {}

    def close(self):
        pass


def probed(monkeypatch, capture, **options):
    worker = Worker()
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    probe = Probe("main.py", 2, capture, hits=2, **options)
    return worker, probe_module.run_probe("/w", probe)


def test_a_frame_prefix_names_a_caller_and_nothing_else_takes_one():
    from peeky.probe import frame_selector

    assert frame_selector("i") == (0, "i")
    assert frame_selector("1:i") == (1, "i")
    assert frame_selector("2:s.items[0]") == (2, "s.items[0]")
    assert frame_selector("63:x") == (63, "x")
    for text, said in [
        ("0:i", "Frame 0 is where the program stopped: capture i without the 0: prefix"),
        ("01:i", "A frame prefix is 1 to 63, with no leading zero"),
        ("64:i", "A frame prefix is 1 to 63"),
        ("1:return", "`return` takes no frame prefix"),
        ("1:(return)", "`return` takes no frame prefix"),
        ("1:", "Name what to read after it: 1:i"),
        ("1:a()", "Invalid selector: a()"),
        ("1:2:i", "Only a probe's --capture reads a caller's frame"),
    ]:
        with pytest.raises(ValueError, match="Invalid selector") as refused:
            Probe("main.py", 2, (text,))
        assert said in str(refused.value), (text, str(refused.value))
    # A change is made only where the program stopped; a session names a frame with
    # --frame. The guess `name@1` is told the spelling.
    with pytest.raises(ValueError) as refused:
        Probe("main.py", 2, ("x",), (("1:i", "0"),))
    assert str(refused.value) == (
        "Invalid selector: 1:i. Only a probe's --capture reads a caller's frame (1:i): a "
        "change is made only where the program stopped. A session reads it with `session get "
        "--frame 1 i`"
    )
    with pytest.raises(ValueError, match="a probe reads i in caller frame 1 as 1:i"):
        parse_selector("i@1")
    # `s @ main.rs:81` still names a local by where it is declared.
    assert parse_selector("s@main.rs:81.a") == ["s @ main.rs:81", "a"]
    assert Probe("main.py", 2, ("i", "1:i")).capture == ("i", "1:i")


def test_caller_rows_say_their_frame_and_keep_the_requested_order(monkeypatch):
    worker, result = probed(monkeypatch, ("1:i", "reading", "1:reading", "2:x"))
    assert result["status"] == "completed", result.get("error")
    first = [row for row in result["rows"] if row["hit"] == 1]
    assert [row["selector"] for row in first] == ["1:i", "reading", "1:reading", "2:x"]
    caller, own, other, past = first
    assert caller == {
        "hit": 1,
        "phase": "observed",
        "selector": "1:i",
        "line": 9,
        "function": "ward",
        "frame": 1,
        "location": "/w/main.py:9",
        "stop_line": 2,
        "value": "1",
        "type": "int",
    }
    assert (own["value"], own["line"], own["function"]) == ("10", 2, "add_reading")
    assert "frame" not in own and "stop_line" not in own
    assert (other["value"], other["frame"]) == ("3", 1)
    # A frame past the stack is that row's error, not the probe's.
    assert past == {
        "hit": 1,
        "phase": "observed",
        "selector": "2:x",
        "frame": 2,
        "stop_line": 2,
        "error": "Frame 2 is out of range: this stop lists 2 frame(s), 0 to 1 (0 is the "
        "innermost; at most 64 are listed)",
    }
    # The stop's inspection serves the stopped frame; each caller frame is read once.
    assert worker.inspected == [0, 1, 2, 0, 1, 2]
    # A hit's line and function are the stop's, whichever row comes first.
    assert [(hit["line"], hit["function"]) for hit in result["by_hit"]] == [
        (2, "add_reading"),
        (2, "add_reading"),
    ]
    assert result["by_hit"][1]["observed"] == {
        "1:i": "2",
        "reading": "20",
        "1:reading": "3",
        "2:x": {"error": past["error"]},
    }


def test_a_hit_with_only_caller_rows_still_says_where_it_stopped(monkeypatch):
    _, result = probed(monkeypatch, ("1:i",))
    assert [(hit["line"], hit["function"]) for hit in result["by_hit"]] == [
        (2, "add_reading"),
        (2, "add_reading"),
    ]
    assert _by_hit([{"hit": 1, "phase": "observed", "selector": "1:i", "frame": 1}]) == [
        {"hit": 1, "line": None, "function": None, "observed": {"1:i": {"error": None}}}
    ]


def test_after_a_change_the_caller_is_read_again(monkeypatch):
    worker, result = probed(monkeypatch, ("reading", "1:i"), set=(("reading", "0"),))
    after = [row for row in result["rows"] if row["phase"] == "after-set"]
    assert [(row["selector"], row.get("frame")) for row in after] == [("reading", None), ("1:i", 1)]
    assert after[1]["stop_line"] == 2


def test_the_store_keeps_the_frame_and_where_the_program_stopped(monkeypatch, tmp_path):
    store = tmp_path / "evidence.db"
    _, result = probed(monkeypatch, ("1:i",), set=(("reading", "0"),))
    # As the worker reads it back (the fake says only the value).
    assert result["interventions"][0]["line"] == 2
    result["interventions"][0].update(name="reading", before="10", after="0")
    evidence.record(store, result, language="python")
    rows = evidence.query(
        store,
        "SELECT hit, phase, line, function, frame, stop_line, selector, value FROM captures "
        "ORDER BY rowid",
    )["rows"]
    assert rows[0] == {
        "hit": 1,
        "phase": "observed",
        "line": 9,
        "function": "ward",
        "frame": 1,
        "stop_line": 2,
        "selector": "1:i",
        "value": "1",
    }
    assert evidence.query(store, "SELECT DISTINCT frame FROM last_run")["rows"] == [{"frame": 1}]
    # The hit's line is where it stopped, and its change was made there: at the captured
    # stop, before its after-set row, not a stop of its own.
    wide = evidence.run_query(store, "wide", {})["rows"]
    assert [(row["hit"], row["phase"], row["line"], row["1:i"]) for row in wide] == [
        (1, "observed", 2, "1"),
        (1, "after-set", 2, "1"),
        (2, "observed", 2, "2"),
    ]
    assert wide[1]["sets"] is not None and wide[2]["sets"] is None
    effect = evidence.run_query(store, "set_effect", {})["rows"]
    assert [(row["selector"], row["interventions"]) for row in effect] == [
        ("1:i", "reading: 10 -> 0")
    ]


def test_a_store_of_schema_20_gains_the_frame_columns(tmp_path):
    import sqlite3

    store = tmp_path / "evidence.db"
    probe = {"probe": {"program": "a.py", "line": 2, "capture": ["x"]}, "status": "completed"}
    evidence.record(store, {**probe, "rows": [{"hit": 1, "phase": "observed", "selector": "x"}]})
    connection = sqlite3.connect(store)
    connection.executescript(
        "DROP VIEW last_run; DROP VIEW evidence; ALTER TABLE captures DROP COLUMN frame; "
        "ALTER TABLE captures DROP COLUMN stop_line; PRAGMA user_version = 20;"
    )
    connection.close()
    assert evidence.query(store, "SELECT selector, frame FROM last_run")["rows"] == [
        {"selector": "x", "frame": None}
    ]
    assert sqlite3.connect(store).execute("PRAGMA user_version").fetchone() == (21,)
