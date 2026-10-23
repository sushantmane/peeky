"""`--set-at LINE@all` (#265): a change at every stop of a line, each made and checked as
one `--set-at LINE@N` is, up to the probe's hit cap (tenth evaluation, debounce: six
hand-computed hits, near the cap of eight changes)."""

import json

import pytest

import peeky.probe as probe_module
from peeky import evidence
from peeky.errors import Refused
from peeky.probe import ALL, MAX_HITS, MAX_SETS, Probe, parse_set_at, probe_exit_code


class Stops:
    """Stops at the lines of main.py in ``order``, each in `main`, then ends. Only lines
    with a breakpoint stop. `set_variable` refuses its calls numbered in ``refuse``."""

    def __init__(self, order, refuse=()):
        self.order, self.refuse = list(order), set(refuse)
        self.placed, self.calls, self.sets, self.line, self.value = [], [], 0, None, "1"

    def _keep(self, points):
        self.placed.append([point["line"] for point in points])
        asked = {point["line"] for point in points}
        self.order = [line for line in self.order if line in asked]

    def dispatch(self, operation, args):
        self.calls.append(operation)
        if operation == "launch":
            self._keep(args["breakpoints"])
            return {
                "session_id": "s",
                "breakpoints": [
                    {"id": index, "verified": True, "line": point["line"]}
                    for index, point in enumerate(args["breakpoints"], 1)
                ],
                "artifact_manifest_id": None,
                "target_identity": None,
                "source_sha256": "x",
            }
        if operation == "breakpoints":
            self._keep(args["breakpoints"])
            return {"breakpoints": [{"line": point["line"]} for point in args["breakpoints"]]}
        if operation == "wait":
            if not self.order:
                return {"state": "terminated"}
            self.line, self.value = self.order.pop(0), "1"
            return {
                "state": "stopped",
                "stop_id": "t",
                "event": {"body": {"reason": "breakpoint"}},
                "location": f"main.py:{self.line}",
            }
        if operation == "inspect":
            return {
                "frames": [{"name": "main", "source": {"path": "/w/main.py"}, "line": self.line}],
                "scopes": [
                    {
                        "name": "Locals",
                        "reference": 1,
                        "variables": [{"name": "x", "value": self.value, "variablesReference": 0}],
                    }
                ],
            }
        if operation == "set_variable":
            self.sets += 1
            if self.sets in self.refuse:
                raise Refused("MUTATION_REJECTED", "'x' cannot be changed here")
            before, self.value = self.value, args["value"]
            return {"name": args["name"], "before": before, "after": args["value"]}
        if operation == "report":
            return {"output": "", "output_truncated": False}
        return {}

    def close(self):
        self.calls.append("close")


def run(monkeypatch, worker, request):
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    return probe_module.run_probe("/w", request)


def made(result):
    return [
        (change["hit"], change["at"], change.get("all"), change["capture_hit"])
        for change in result["interventions"]
    ]


def test_all_parses_as_every_stop_and_counts_as_one_option():
    assert parse_set_at("36@all", "db.count=0") == (None, 36, ALL, "db.count", "0")
    assert parse_set_at("debounce.c:36@all", "n = 0") == ("debounce.c", 36, ALL, "n", "0")
    for where in ("36@ALL", "36@al", "36@all@2", "36@every", "@all", "36@0"):
        with pytest.raises(ValueError, match=r"--set-at expects .*or <line>@all"):
            parse_set_at(where, "x=1")
    assert Probe("a.c", 3, set_at=((4, ALL, "x", "1"),)).set_at == ((4, ALL, "x", "1"),)
    for hit in ("every", "All", None, 0, 1.0):
        with pytest.raises(ValueError, match="or 'all'"):
            Probe("a.c", 3, set_at=((4, hit, "x", "1"),))
    # One @all is one of the eight changes a probe may ask for, however often it is made.
    singles = tuple((4, hit, "x", "1") for hit in range(1, MAX_SETS))
    assert len(Probe("a.c", 3, set_at=(*singles, (5, ALL, "x", "0"))).set_at) == MAX_SETS
    with pytest.raises(ValueError, match="--set and --set-at together"):
        Probe("a.c", 3, set=(("x", "1"),), set_at=(*singles, (5, ALL, "x", "0")))
    with pytest.raises(Exception, match="map and dict entries cannot be changed"):
        Probe("a.c", 3, set_at=((4, ALL, "m['k']", "1"),))


def test_an_all_on_the_capture_line_leaves_room_for_after_set_rows_at_every_hit():
    many = tuple(f"a{i}" for i in range(20))
    # 20 observed and 20 after-set rows per hit, in the 2000-row budget.
    assert Probe("a.c", 3, many, hits="all", set_at=((3, ALL, "a0", "1"),)).limit == 50
    assert Probe("a.c", 3, many, hits="all", set_at=((4, ALL, "a0", "1"),)).limit == 100
    assert Probe("a.c", 3, ("a",), hits="all", set_at=((3, ALL, "a", "1"),)).limit == MAX_HITS


def test_an_all_is_made_at_every_stop_of_its_line(monkeypatch):
    worker = Stops([8, 9, 9, 8, 9])
    result = run(monkeypatch, worker, Probe("main.py", 8, ("x",), set_at=((9, ALL, "x", "5"),)))
    assert result["status"] == "completed" and "unapplied" not in result
    # Each with its own hit of line 9, after the one hit of line 8 that was captured.
    assert made(result) == [(1, "9@1", True, 1), (2, "9@2", True, 1), (3, "9@3", True, 1)]
    assert [change["function"] for change in result["interventions"]] == ["main"] * 3
    # Line 9's breakpoint stays for its every stop; line 8's goes after its one hit.
    assert worker.placed == [[8, 9], [9]] and worker.calls.count("set_variable") == 3
    assert probe_exit_code(result) == 0
    text = probe_module._table(result)
    assert "set x: 1 -> 5  (--set-at line 9@all, hit 2, in main)" in text
    assert "more change" not in text and "status: completed  hits: 1" in text


def test_an_all_on_the_capture_line_gets_after_set_rows_at_each_hit(monkeypatch):
    worker = Stops([8, 8, 8])
    request = Probe("main.py", 8, ("x",), hits="all", set_at=((8, ALL, "x", "5"),))
    result = run(monkeypatch, worker, request)
    assert made(result) == [(1, "8@1", True, 1), (2, "8@2", True, 2), (3, "8@3", True, 3)]
    assert [(row["hit"], row["phase"]) for row in result["rows"]] == [
        (hit, phase) for hit in (1, 2, 3) for phase in ("observed", "after-set")
    ]


def test_an_all_whose_line_never_stops_is_reported_never_dropped(monkeypatch):
    worker = Stops([8])
    result = run(monkeypatch, worker, Probe("main.py", 8, ("x",), set_at=((9, ALL, "x", "5"),)))
    assert result["status"] == "completed" and result["interventions"] == []
    assert result["unapplied"] == [
        {"option": "--set-at", "at": "9@all", "name": "x", "value": "5", "stops": 0}
    ]
    assert (
        "--set-at main.py:9@all x=5 was not applied: line 9 never stopped before the probe "
        "ended." in result["warnings"]
    )
    assert probe_exit_code(result) == 4
    assert "status: completed (1 requested change not made)" in probe_module._table(result)


def test_an_all_stops_at_the_cap_and_says_so(monkeypatch):
    worker = Stops([8, *[9] * (MAX_HITS + 5)])
    result = run(monkeypatch, worker, Probe("main.py", 8, ("x",), set_at=((9, ALL, "x", "5"),)))
    assert result["status"] == "completed"
    assert [change["hit"] for change in result["interventions"]] == list(range(1, MAX_HITS + 1))
    # The stop past the cap is not changed, and line 9's breakpoint goes with it.
    assert worker.calls.count("set_variable") == MAX_HITS and worker.placed[-1] == []
    assert result["unapplied"] == [
        {
            "option": "--set-at",
            "at": "9@all",
            "name": "x",
            "value": "5",
            "stops": MAX_HITS + 1,
            "made": MAX_HITS,
        }
    ]
    assert (
        f"--set-at main.py:9@all x=5 was made only at the first {MAX_HITS} stops of line 9, the "
        "cap: the line stopped again, and the program ran on from there without the change. "
        "Probe a smaller input, or a line that runs less often." in result["warnings"]
    )
    assert probe_exit_code(result) == 4
    text = probe_module._table(result)
    # The first three changes, then one line for the rest.
    assert "set x: 1 -> 5  (--set-at line 9@all, hit 3, in main)" in text
    assert "hit 4," not in text
    assert (
        f"set x: {MAX_HITS - 3} more changes by --set-at line 9@all, at hits 4 to {MAX_HITS} "
        "(`--json` lists each)" in text
    )
    assert "status: completed (1 --set-at @all cut short at the cap)  hits: 1" in text


def test_a_refusal_at_one_stop_ends_the_probe_and_names_that_hit(monkeypatch):
    # As for one --set-at: a change made at some stops only is not the experiment asked.
    worker = Stops([8, 9, 9, 9, 9], refuse=(3,))
    result = run(monkeypatch, worker, Probe("main.py", 8, ("x",), set_at=((9, ALL, "x", "5"),)))
    assert result["status"] == "failed" and result["error"]["code"] == "MUTATION_REJECTED"
    message = result["error"]["message"]
    assert message.startswith(
        "--set-at main.py:9@all x=5 was refused at hit 3 of line 9 (in main): 'x' cannot be "
        "changed here"
    ), message
    assert message.endswith(
        "(2 earlier intervention(s) applied; the run is not the experiment asked for)"
    )
    assert made(result) == [(1, "9@1", True, 1), (2, "9@2", True, 1)]
    assert worker.calls.count("set_variable") == 3 and probe_exit_code(result) == 1


def test_an_all_never_keeps_an_until_hits_probe_running(monkeypatch):
    worker = Stops([8, 9, 8, 9, 9])
    request = Probe("main.py", 8, ("x",), hits=2, until="hits", set_at=((9, ALL, "x", "5"),))
    result = run(monkeypatch, worker, request)
    assert result["status"] == "completed" and result["ended"] == "hits"
    assert made(result) == [(1, "9@1", True, 1)] and worker.order == [9, 9]
    assert "unapplied" not in result and probe_exit_code(result) == 0
    assert (
        "--set-at main.py:9@all x=5 was made at each of the 1 stops of line 9 before the probe "
        "ended after its last hit (--until hits). Any later execution of the line ran without "
        "the change." in result["warnings"]
    )


DEBOUNCE_REFUSAL = (
    "`db.count` cannot be changed at debounce.c:36, at any stop of this build: in settle, "
    "`db` is a pointer: Peeky does not write through a pointer, since what it reaches may be "
    "freed or moved; feed has one caller: debounce.c:80 in main, which passes `&db`, where "
    "`db.count` is settable: a probe's `--set-at 80@N db.count=<value>`, N counting line 80's "
    "own stops (find N with `--at 80 --capture db.count --hits all`). Refused before the "
    "program runs: nothing ran"
)


def test_an_all_refused_in_a_callee_says_what_all_does_in_the_owning_frame(monkeypatch):
    # debounce: line 36 runs only when the input is back at the accepted level, but there
    # `db` is a pointer; line 80, in main, runs once per sample, so `80@all` is another
    # experiment.
    class Refusing(Stops):
        def dispatch(self, operation, args):
            if operation == "launch":
                refused = Refused("MUTATION_REJECTED", DEBOUNCE_REFUSAL)
                refused.before_launch = True
                raise refused
            return super().dispatch(operation, args)

    request = Probe("debounce.c", 36, set_at=((36, ALL, "db.count", "0"),))
    result = run(monkeypatch, Refusing([]), request)
    assert result["refused_before_launch"] and probe_exit_code(result) == 2
    assert result["error"]["message"] == DEBOUNCE_REFUSAL.replace(
        "--hits all`).",
        "--hits all`); `--set-at 80@all` would make the change at every stop of line 80, "
        "whether or not the call made there reaches line 36: to make it only where line 36 "
        "runs, try it in a candidate edit (`--old`/`--new`).",
    )
    # Not for one --set-at at a hit, nor an @all of another line or name.
    for set_at in (
        ((36, 5, "db.count", "0"),),
        ((80, ALL, "db.count", "0"),),
        ((36, ALL, "db.candidate", "0"),),
    ):
        result = run(monkeypatch, Refusing([]), Probe("debounce.c", 36, set_at=set_at))
        assert result["error"]["message"] == DEBOUNCE_REFUSAL


def test_the_http_probe_passes_an_all_and_refuses_another_word(tmp_path, monkeypatch):
    import subprocess
    import threading

    from peeky import worker as worker_module
    from peeky.client import Client

    seen = {}

    class Process:
        pid = 0

        def __init__(self, command, **kwargs):
            seen["command"] = command

        def communicate(self, timeout=None):
            return b'{"status": "completed"}', b""

    monkeypatch.setattr(subprocess, "Popen", Process)
    request = Probe("x.c", 3, set_at=((4, ALL, "n", "0"),))
    worker_module._run_probe_process(tmp_path, request, None, 60)
    command = seen["command"]
    at = command.index("--set-at")
    assert command[at + 1 : at + 3] == ["4@all", "n=0"]
    # The probe child reads it back as the same request.
    assert probe_module._own_set_at("x.c", "4@all", "n=0") == (4, ALL, "n", "0")

    server = worker_module.make_server(tmp_path, "unit-test-secret-token-1234", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", "unit-test-secret-token-1234")
        for set_at in ([[4, "every", "x", "1"]], [[4, "All", "x", "1"]], [["4", "all", "x", "1"]]):
            with pytest.raises(Exception, match="set_at"):
                client.call("probe", at="x.c:1", set_at=set_at)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_the_evidence_keeps_each_change_and_says_how_the_request_went(monkeypatch, tmp_path):
    store = tmp_path / "evidence" / "evidence.db"
    workers = [
        Stops([8, 9, 9, 9, 9, 9]),
        Stops([8]),
        Stops([8, *[9] * (MAX_HITS + 1)]),
        Stops([8]),
    ]
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: workers.pop(0))
    for set_at in (((9, ALL, "x", "5"),),) * 3 + ((),):
        evidence.record(
            store, probe_module.run_probe("/w", Probe("main.py", 8, ("x",), set_at=set_at))
        )
    changes = evidence.query(
        store, "SELECT run_id, hit, line, name, after FROM interventions WHERE run_id = 1", {}
    )["rows"]
    assert [(row["hit"], row["line"], row["after"]) for row in changes] == [
        (hit, 9, "5") for hit in range(1, 6)
    ]
    sets = {
        row["run_id"]: json.loads(row["sets"])
        for row in evidence.run_query(store, "outputs", {"a": 1, "b": 2})["rows"]
        + evidence.run_query(store, "outputs", {"a": 3, "b": 4})["rows"]
    }
    assert sets == {
        1: [["x", "5", "requested at 9@all, made at 5 stops, to the program's end"]],
        2: [["x", "5", "requested at 9@all, not made: its line never stopped"]],
        3: [["x", "5", f"requested at 9@all, made at the first {MAX_HITS} stops: the cap"]],
        4: [],
    }
    # `diff`'s (sets) row counts an @all as one request, however many changes it made.
    (row,) = [
        r
        for r in evidence.run_query(store, "diff", {"a": 4, "b": 1})["rows"]
        if r["selector"] == "(sets)"
    ]
    assert (
        evidence._sets_text(row["value_b"])
        == "1 request: x=5 (requested at 9@all, made at 5 stops, to the program's end)"
    )
    assert evidence._sets_text(row["value_a"]) == "none"
    two = [["x", "5", "requested at 9@2"], ["y", "1", "requested at 9@3"]]
    assert evidence._sets_text(json.dumps(two)) == "2 changes: x=5 (requested at 9@2), …"
    # Against the other run's, the first change that differs.
    other = [two[0], ["y", "1", "requested at 9@3, not made: that hit never came"]]
    assert (
        evidence._sets_text(json.dumps(two), json.dumps(other))
        == "2 changes: …, y=1 (requested at 9@3)"
    )


def test_diff_shows_many_alike_changes_as_one_row(tmp_path, monkeypatch):
    # 50 changes at captured stops: one row, its first hit's, says how many there were.
    store = tmp_path / "evidence" / "evidence.db"
    workers = [Stops([8] * 50), Stops([8] * 50), Stops([8] * 3), Stops([8] * 3)]
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: workers.pop(0))
    for count, set_at in (
        (50, ()),
        (50, ((8, ALL, "x", "5"),)),
        (3, ()),
        (3, ((8, ALL, "x", "5"),)),
    ):
        request = Probe("main.py", 8, ("x",), hits="all", set_at=set_at)
        evidence.record(store, probe_module.run_probe("/w", request))
    rows = evidence.run_query(store, "diff", {"a": 1, "b": 2})["rows"]
    changed = [row for row in rows if row["phase"] == "after-set"]
    assert [(row["hit"], row["value_b"], row["note"]) for row in changed] == [
        (
            1,
            "5",
            "intervention only in run b, at 50 hits (one row per selector and value: its "
            "first hit)",
        )
    ]
    # Three or fewer stay one row each.
    rows = evidence.run_query(store, "diff", {"a": 3, "b": 4})["rows"]
    assert [(row["hit"], row["note"]) for row in rows if row["phase"] == "after-set"] == [
        (hit, "intervention only in run b") for hit in (1, 2, 3)
    ]


# Review of PR #278: comparisons hid how far an @all got. A run refused at hit 7, two runs
# refused at different hits, and a run --until hits ended after one change, each read
# like a run that changed every stop of the line.
def recorded(monkeypatch, store, *runs):
    """Run and save each (worker, request) in turn; their results."""
    workers = [worker for worker, _ in runs]
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: workers.pop(0))
    results = []
    for _, request in runs:
        results.append(probe_module.run_probe("/w", request))
        evidence.record(store, results[-1])
    return results


def run_rows(store, query, a, b):
    rows = evidence.run_query(store, query, {"a": a, "b": b})["rows"]
    return {
        row["selector"]: (row["value_a"], row["value_b"], row["differs"], row["note"])
        for row in rows
        if row["selector"] in ("(status)", "(sets)")
    }


ALL_AT_9 = Probe("main.py", 8, ("x",), set_at=((9, ALL, "x", "5"),))
REFUSED_7 = "--set-at main.py:9@all x=5 was refused at hit 7 of line 9 (in main)"


def test_a_refusal_at_hit_7_says_so_wherever_the_run_is_compared(monkeypatch, tmp_path, capsys):
    store = tmp_path / "evidence" / "evidence.db"
    full, refused = recorded(
        monkeypatch,
        store,
        (Stops([8, *[9] * 10]), ALL_AT_9),
        (Stops([8, *[9] * 10], refuse=(7,)), ALL_AT_9),
    )
    assert full["set_outcomes"] == [
        {
            "option": "--set-at",
            "at": "9@all",
            "name": "x",
            "value": "5",
            "made": 10,
            "ended": "exit",
            "complete": True,
        }
    ]
    assert refused["set_outcomes"] == [
        {
            "option": "--set-at",
            "at": "9@all",
            "name": "x",
            "value": "5",
            "made": 6,
            "ended": "refused",
            "hit": 7,
            "complete": False,
        }
    ]
    (outputs,) = evidence.run_query(store, "outputs", {"a": 2, "b": 2})["rows"]
    assert json.loads(outputs["sets"]) == [
        ["x", "5", "requested at 9@all, made at 6 stops; refused at hit 7"]
    ]
    for query in ("diff", "compare"):
        rows = run_rows(store, query, 1, 2)
        status_a, status_b, differs, note = rows["(status)"]
        assert (status_a, differs, note) == ("completed", 1, "outcome differs"), query
        assert status_b.startswith(f"failed [MUTATION_REJECTED]: {REFUSED_7}"), status_b
        sets_a, sets_b, differs, _ = rows["(sets)"]
        assert differs == 1 and "made at 6 stops; refused at hit 7" in sets_b, sets_b
        assert "made at 10 stops, to the program's end" in sets_a, sets_a
    # Each run against itself: `compare` still says it did not complete, and how far the
    # change got; `diff` names the outcome too.
    rows = run_rows(store, "compare", 2, 2)
    assert rows["(status)"][2:] == (0, "neither run completed, and both ended the same way")
    assert rows["(sets)"][2:] == (0, "the same interventions, made alike")
    assert set(run_rows(store, "diff", 2, 2)) == {"(status)"}
    assert main_text(capsys, store, "compare", 1, 2).count("refused at hit 7") >= 2


def test_two_runs_refused_at_different_hits_differ(monkeypatch, tmp_path):
    store = tmp_path / "evidence" / "evidence.db"
    recorded(
        monkeypatch,
        store,
        (Stops([8, *[9] * 10], refuse=(7,)), ALL_AT_9),
        (Stops([8, *[9] * 10], refuse=(9,)), ALL_AT_9),
    )
    for query in ("diff", "compare"):
        rows = run_rows(store, query, 1, 2)
        status_a, status_b, differs, note = rows["(status)"]
        assert (differs, note) == (1, "outcome differs"), query
        assert "refused at hit 7 of line 9" in status_a and "hit 9 of line 9" in status_b
        sets_a, sets_b, differs, _ = rows["(sets)"]
        assert differs == 1, query
        assert "made at 6 stops; refused at hit 7" in sets_a, sets_a
        assert "made at 8 stops; refused at hit 9" in sets_b, sets_b


def test_a_run_ended_by_until_hits_differs_from_one_that_ran_to_its_end(
    monkeypatch, tmp_path, capsys
):
    # Both completed, with the same values at their one captured hit: only how far the
    # @all got tells them apart.
    store = tmp_path / "evidence" / "evidence.db"
    every = ((8, ALL, "x", "5"),)
    to_end, until = recorded(
        monkeypatch,
        store,
        (Stops([8, 8, 8]), Probe("main.py", 8, ("x",), set_at=every)),
        (Stops([8, 8, 8]), Probe("main.py", 8, ("x",), until="hits", set_at=every)),
    )
    assert (to_end["status"], until["status"]) == ("completed", "completed")
    assert to_end["rows"] == until["rows"]
    assert [o["ended"] for o in (*to_end["set_outcomes"], *until["set_outcomes"])] == [
        "exit",
        "until_hits",
    ]
    for query in ("diff", "compare"):
        rows = run_rows(store, query, 1, 2)
        assert "(status)" not in rows, query
        sets_a, sets_b, differs, _ = rows["(sets)"]
        assert differs == 1, query
        assert sets_a == '[["x","5","requested at 8@all, made at 3 stops, to the program\'s end"]]'
        assert sets_b == (
            '[["x","5","requested at 8@all, made at 1 stop before --until hits ended the probe"]]'
        )
    # Only the (sets) row differs, and the text table shows how far each got.
    rows = evidence.run_query(store, "diff", {"a": 1, "b": 2})["rows"]
    assert [row["selector"] for row in rows] == ["(sets)"]
    shown = main_text(capsys, store, "diff", 1, 2)
    assert "made at 1 stop before --until hits ended the probe" in shown, shown


def main_text(capsys, store, query, a, b):
    from peeky.cli import main

    capsys.readouterr()
    argv = ["queries", query, "--param", f"a={a}", "--param", f"b={b}", "--store", str(store)]
    assert main(argv) == 0
    return capsys.readouterr().out


def outcome(at="9@all", name="x", value="5", option="--set-at", **said):
    """A runs.set_outcomes entry as a probe gives it; ``said``: made, ended[, hit]."""
    every = option == "--set-at" and at.endswith("@all")
    complete = said["ended"] == "made" or (said["ended"] == "exit" and every and said["made"])
    return {"option": option, "at": at, "name": name, "value": value, **said} | {
        "complete": bool(complete)
    }


def said(entry=None, **fields):
    """How `outputs` says ``fields``' outcome, of the request ``entry`` (default: its own)."""
    made = outcome(**fields)
    entry = entry or [made["name"], made["value"], made["at"]]
    return evidence._set_outcome(json.dumps(made), json.dumps(entry), 8, None)


def test_each_outcome_reads_as_how_far_the_change_got():
    assert said(made=1, ended="made", at="9@2") == ""
    assert said(made=0, ended="exit", at="9@4") == ", not made: that hit never came"
    assert said(made=0, ended="refused", hit=4, at="9@4") == ", refused at that hit"
    assert said(made=0, ended="timeout", at="9@4") == ", not made: the probe timed out first"
    assert said(made=32, ended="exit") == ", made at 32 stops, to the program's end"
    assert said(made=1, ended="until_hits") == (
        ", made at 1 stop before --until hits ended the probe"
    )
    assert said(made=0, ended="until_hits") == (
        ", not made: its line never stopped before --until hits ended the probe"
    )
    assert said(made=6, ended="refused", hit=7) == ", made at 6 stops; refused at hit 7"
    assert said(made=6, ended="failed", hit=7) == ", made at 6 stops; failed at hit 7"
    assert said(made=6, ended="failed") == ", made at 6 stops before the probe failed"
    assert said(made=100, ended="cap") == ", made at the first 100 stops: the cap"
    assert said(made=3, ended="lost_stop") == (
        ", made at 3 stops; the debugger lost a stop, which may have run without it"
    )
    # A --set: at its probe line's set_hit (1 where runs.set_hit is NULL).
    assert said(
        ["x", "2"], made=0, ended="refused", hit=1, at="8@1", option="--set", value="2"
    ) == (", refused at that hit")
    assert evidence._set_outcome("not json", '["x", "5", "9@all"]', 8, None) is None
    assert evidence._set_outcome("[]", '["x", "5", "9@all"]', 8, None) is None


def test_an_outcome_is_read_only_where_it_is_one_for_its_request():
    # Review 3 of PR #278: `[{"made": 2, "ended": "refused", "hit": 3}]` for a `9@all`
    # read "refused at that hit", and counted as recorded.
    bare = {"made": 2, "ended": "refused", "hit": 3}
    assert evidence._set_outcome(json.dumps(bare), '["x", "5", "9@all"]', 8, None) is None
    good = outcome(made=2, ended="refused", hit=3)
    for broken in (
        {key: value for key, value in good.items() if key != "at"},
        {key: value for key, value in good.items() if key != "complete"},
        good | {"at": "9@2"},
        good | {"name": "y"},
        good | {"value": "6"},
        good | {"option": "--set"},
        good | {"complete": 0},
        good | {"complete": True},
        good | {"made": -1},
        good | {"made": True},
        good | {"made": 1},
        good | {"ended": "stopped"},
        good | {"hit": "3"},
        good | {"hit": 0},
        good | {"extra": 1},
        {key: value for key, value in good.items() if key != "hit"},
        outcome(made=2, ended="exit") | {"hit": 3},
        outcome(made=1, ended="made"),
        outcome(made=0, ended="cap"),
        outcome(made=1, ended="exit", at="9@4"),
        outcome(made=0, ended="cap", at="9@4"),
        outcome(made=0, ended="lost_stop", at="9@4"),
    ):
        assert (
            evidence._checked_outcome(json.dumps(broken), '["x", "5", "9@all"]', 8, None) is None
        ), broken
    assert evidence._checked_outcome(json.dumps(good), '["x", "5", "9@all"]', 8, None) == good


def test_an_outcome_of_another_type_or_hit_is_not_read():
    # Review 4 of PR #278: an `ended` that is a list raised TypeError (failing the query),
    # and a `9@4` change "refused at hit 7" read as recorded.
    for ended in ([], {}, 3, None):
        broken = outcome(made=2, ended="refused", hit=3) | {"ended": ended}
        assert evidence._checked_outcome(json.dumps(broken), '["x", "5", "9@all"]', 8, None) is None
    fixed = '["x", "5", "9@4"]'
    right = outcome(made=0, ended="refused", hit=4, at="9@4")
    assert evidence._checked_outcome(json.dumps(right), fixed, 8, None) == right
    for hit in (7, 3):
        wrong = right | {"hit": hit}
        assert evidence._checked_outcome(json.dumps(wrong), fixed, 8, None) is None
        failed = outcome(made=0, ended="failed", hit=hit, at="9@4")
        assert evidence._checked_outcome(json.dumps(failed), fixed, 8, None) is None
    # A --set at the probe's line, at its --set-hit (default 1).
    plain = '["x", "5"]'
    at_two = outcome(made=0, ended="refused", hit=2, at="8@2", option="--set")
    assert evidence._checked_outcome(json.dumps(at_two), plain, 8, 2) == at_two
    assert evidence._checked_outcome(json.dumps(at_two | {"hit": 5}), plain, 8, 2) is None
    at_one = outcome(made=0, ended="refused", hit=1, at="8@1", option="--set")
    assert evidence._checked_outcome(json.dumps(at_one), plain, 8, None) == at_one
    assert evidence._checked_outcome(json.dumps(at_one | {"hit": 2}), plain, 8, None) is None


def test_outcomes_follow_the_runs_sets_order_and_a_set_refused_is_said(monkeypatch):
    # A --set refused at its hit, after a --set-at made at line 9's first stop.
    worker = Stops([9, 8, 9], refuse=(2,))
    request = Probe(
        "main.py", 8, ("x",), (("x", "2"),), set_at=((9, 1, "x", "5"), (9, 2, "x", "6"))
    )
    result = run(monkeypatch, worker, request)
    assert result["status"] == "failed"
    assert [
        (o["option"], o["at"], o["made"], o["ended"], o.get("hit"), o["complete"])
        for o in result["set_outcomes"]
    ] == [
        ("--set", "8@1", 0, "refused", 1, False),
        ("--set-at", "9@1", 1, "made", None, True),
        ("--set-at", "9@2", 0, "failed", None, False),
    ]


def test_a_schema_15_store_gains_outcomes_and_its_runs_keep_their_warnings(tmp_path):
    import sqlite3

    store = tmp_path / "evidence.db"
    older = {
        "probe": {"program": "a.c", "line": 8, "set_at": [[9, 4, "x", "7"]]},
        "status": "completed",
        "warnings": [
            "--set-at a.c:9@4 x=7 was not applied: line 9 stopped 3 time(s) before the probe "
            "ended, so its hit 4 never came."
        ],
    }
    evidence.record(store, older)
    connection = sqlite3.connect(store)
    connection.executescript("ALTER TABLE runs DROP COLUMN set_outcomes; PRAGMA user_version = 15;")
    connection.close()
    newer = {
        **older,
        "warnings": [],
        "set_outcomes": [
            {"option": "--set-at", "at": "9@4", "name": "x", "value": "7", "made": 0,
             "ended": "refused", "hit": 4, "complete": False}
        ],
    }  # fmt: skip
    evidence.record(store, newer)
    connection = sqlite3.connect(store)
    assert connection.execute("PRAGMA user_version").fetchone() == (16,)
    assert [row[0] is None for row in connection.execute("SELECT set_outcomes FROM runs")] == [
        True,
        False,
    ]
    connection.close()
    rows = evidence.run_query(store, "outputs", {"a": 1, "b": 2})["rows"]
    assert [json.loads(row["sets"]) for row in rows] == [
        [["x", "7", "requested at 9@4, not made: that hit never came"]],
        [["x", "7", "requested at 9@4, refused at that hit"]],
    ]


# Review 2 of PR #278: runs saved before schema 16 have no outcomes. Their `@all` warnings
# still say how it went where they can, and two such runs with the same request are never
# said to have made it alike.
UNRECORDED = (
    "the same requested interventions; how they were applied wasn't recorded for one or both "
    "older runs"
)


def as_schema_15(store):
    """The store as schema 15 kept these runs: no runs.set_outcomes."""
    import sqlite3

    connection = sqlite3.connect(store)
    connection.executescript("ALTER TABLE runs DROP COLUMN set_outcomes; PRAGMA user_version = 15;")
    connection.close()


def test_a_schema_15_capped_or_never_stopped_all_keeps_what_its_warning_said(monkeypatch, tmp_path):
    store = tmp_path / "evidence" / "evidence.db"
    recorded(
        monkeypatch,
        store,
        (Stops([8, *[9] * (MAX_HITS + 1)]), ALL_AT_9),
        (Stops([8]), ALL_AT_9),
    )
    as_schema_15(store)
    rows = evidence.run_query(store, "outputs", {"a": 1, "b": 2})["rows"]
    assert [json.loads(row["sets"]) for row in rows] == [
        [["x", "5", f"requested at 9@all, made only at the first {MAX_HITS} stops: the cap"]],
        [["x", "5", "requested at 9@all, not made: its line never stopped"]],
    ]
    import sqlite3

    connection = sqlite3.connect(store)
    assert connection.execute("SELECT COUNT(set_outcomes) FROM runs").fetchone() == (0,)
    # An entry that is no outcome reads as its warning did too.
    connection.execute("UPDATE runs SET set_outcomes = 'not json' WHERE run_id = 1")
    connection.commit()
    connection.close()
    (row,) = evidence.run_query(store, "outputs", {"a": 1, "b": 1})["rows"]
    assert json.loads(row["sets"])[0][2].endswith(
        f"made only at the first {MAX_HITS} stops: the cap"
    )


def test_two_schema_15_runs_with_the_same_request_are_not_said_to_be_made_alike(
    monkeypatch, tmp_path
):
    # One changed every stop to the program's end, the other one stop before --until hits
    # ended it: schema 15 kept neither, and their requests read alike.
    store = tmp_path / "evidence" / "evidence.db"
    every = ((8, ALL, "x", "5"),)
    recorded(
        monkeypatch,
        store,
        (Stops([8, 8, 8]), Probe("main.py", 8, ("x",), set_at=every)),
        (Stops([8, 8, 8]), Probe("main.py", 8, ("x",), until="hits", set_at=every)),
        (Stops([8, 8, 8]), Probe("main.py", 8, ("x",), set_at=every)),
    )
    as_schema_15(store)
    for query in ("compare", "diff"):
        sets_a, sets_b, differs, note = run_rows(store, query, 1, 2)["(sets)"]
        assert sets_a == sets_b == '[["x","5","requested at 8@all"]]', (query, sets_a)
        assert (differs, note) == (0, UNRECORDED), query
    # A run saved since (schema 16) against an older one: still not recorded for both.
    recorded(monkeypatch, store, (Stops([8, 8, 8]), Probe("main.py", 8, ("x",), set_at=every)))
    for query in ("compare", "diff"):
        # Review 3: the same request, so the note is that it wasn't recorded; the values
        # differ, as one says how it went and the other cannot.
        assert run_rows(store, query, 1, 4)["(sets)"][2:] == (1, UNRECORDED), query
    recorded(monkeypatch, store, (Stops([8, 8, 8]), Probe("main.py", 8, ("x",), set_at=every)))
    # Two runs that recorded it, made alike: `compare` says so, and `diff` has no row.
    assert run_rows(store, "compare", 4, 5)["(sets)"][2:] == (
        0,
        "the same interventions, made alike",
    )
    assert "(sets)" not in run_rows(store, "diff", 4, 5)


RECORDED_APART = (
    "the runs requested different interventions (--set, --set-at), or made different ones"
)


def test_an_unrecorded_run_is_never_counted_as_recorded():
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.create_function("set_outcome", 4, evidence._set_outcome)
    made = outcome(made=2, ended="exit")
    single = outcome(made=1, ended="made", at="9@2", name="y", value="1")
    two = '[["x", "5", "9@all"], ["y", "1", "9@2"]]'
    for sets, outcomes, expected in (
        ('[["x", "5", "9@all"]]', json.dumps([made]), 1),
        (two, json.dumps([made, single]), 1),
        ('[["x", "5", "9@all"]]', None, 0),
        ('[["x", "5", "9@all"]]', "not json", 0),
        ('[["x", "5", "9@all"]]', "{}", 0),
        ('[["x", "5", "9@all"]]', "[]", 0),
        ('[["x", "5", "9@all"]]', json.dumps([{"made": "2"}]), 0),
        (two, json.dumps([made]), 0),
        # Swapped: each entry is another request's.
        (two, json.dumps([single, made]), 0),
    ):
        sql = (
            "SELECT "
            + evidence._RECORDED.format(r="r")
            + " FROM (SELECT ? AS sets, ? AS set_outcomes, 8 AS line, NULL AS set_hit) r"
        )
        assert connection.execute(sql, (sets, outcomes)).fetchone() == (expected,), outcomes


def tamper(store, run_id, outcomes):
    import sqlite3

    connection = sqlite3.connect(store)
    connection.execute(
        "UPDATE runs SET set_outcomes = ? WHERE run_id = ?", (json.dumps(outcomes), run_id)
    )
    connection.commit()
    connection.close()


def test_an_outcome_that_is_not_its_requests_falls_back_to_the_warning(monkeypatch, tmp_path):
    # Review 3 of PR #278: a capped run whose outcome lacks `at`, names another request, or
    # whose entries are swapped reads as its warning did, and is not counted as recorded.
    store = tmp_path / "evidence" / "evidence.db"
    capped = Probe("main.py", 8, ("x",), set_at=((9, ALL, "x", "5"), (9, 1, "x", "1")))
    workers = [Stops([8, *[9] * (MAX_HITS + 1)]) for _ in range(5)]
    results = recorded(monkeypatch, store, *((worker, capped) for worker in workers))
    kept = results[0]["set_outcomes"]
    assert [o["ended"] for o in kept] == ["cap", "made"]
    no_at = {key: value for key, value in kept[0].items() if key != "at"}
    tamper(store, 2, [no_at, kept[1]])
    tamper(store, 3, [kept[0] | {"name": "z"}, kept[1]])
    tamper(store, 4, [kept[1], kept[0]])
    # The review's own: no option, location, name, value or `complete`.
    tamper(store, 5, [{"made": 2, "ended": "refused", "hit": 3}, kept[1]])
    cap = f"requested at 9@all, made only at the first {MAX_HITS} stops: the cap"
    for run_id in (2, 3, 4, 5):
        (row,) = evidence.run_query(store, "outputs", {"a": run_id, "b": run_id})["rows"]
        assert json.loads(row["sets"]) == [["x", "5", cap], ["x", "1", "requested at 9@1"]]
        for query in ("compare", "diff"):
            assert run_rows(store, query, 1, run_id)["(sets)"][2:] == (1, UNRECORDED), query
    (row,) = evidence.run_query(store, "outputs", {"a": 1, "b": 1})["rows"]
    assert json.loads(row["sets"])[0][2] == (
        f"requested at 9@all, made at the first {MAX_HITS} stops: the cap"
    )
