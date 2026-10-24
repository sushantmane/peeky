"""The eleventh agent evaluation's Lows in the CLI's text (#306): what a probe's table and
its changes say, a `void` function's return, session paths, hits and reports, `queries
runs`, a candidate's line, `NO_STOP` after changes, and a Java `verify` without `--at`."""

from types import SimpleNamespace

import pytest

import peeky.probe as probe_module
from peeky import cli, evidence
from peeky.native import NativeSession
from peeky.probe import Probe, _returned_row, _table
from peeky.worker import DebugWorker


def _result(rows=(), interventions=(), language="cpp", **more):
    return {
        "rows": list(rows),
        "interventions": list(interventions),
        "status": "completed",
        "hits": 1,
        "seconds": 1.0,
        "target_identity": {"language": language},
        **more,
    }


def _row(selector, value, phase="observed", hit=1, **fields):
    row = {"hit": hit, "phase": phase, "line": 47, "function": "rota::plan"}
    return {**row, "selector": selector, "type": "u32", "value": value, **fields}


def _all(name, pairs, line=26, function="main"):
    """An @all's changes at hits 1, 2, …: (before, after) each."""
    return [
        {
            "name": name,
            "before": before,
            "after": after,
            "line": line,
            "hit": hit,
            "capture_hit": 1,
            "function": function,
            "at": f"{line}@{hit}",
            "all": True,
        }
        for hit, (before, after) in enumerate(pairs, 1)
    ]


def test_the_after_set_line_says_only_scalar_rows_are_left_out():
    # rota 4: `after-set: 1 row equal to the hit's observed value left out`, while `team`'s
    # after-set row, equal to its observed one, stayed: only whole scalars are left out.
    team = '[Engineer { name: "Ana", shifts: 1, .. }, …]'
    rows = [_row("week", "6"), _row("team", team)]
    rows += [_row("week", "6", "after-set"), _row("team", team, "after-set")]
    change = {"name": "pick", "before": "1", "after": "2", "hit": 1, "capture_hit": 1}
    text = _table(_result(rows, [change], language="rust"))
    assert "after-set  47    rota::plan  team" in text, text
    assert "after-set  47    rota::plan  week" not in text, text
    assert (
        "after-set: 1 scalar row equal to the hit's observed value left out (a number, flag or "
        "whole text; pointer, object and preview rows stay, as they may hide a change; "
        "`--json` has every row)"
    ) in text, text


def test_one_change_past_the_first_few_is_shown_whole():
    # grades 6 (prep 5): `set stats.lowest: 1 more change by --set-at line 61@all, at hits 4
    # to 4`, where the change itself fits on that line.
    changes = _all("stats.lowest", [("101", "101"), ("58", "101"), ("72", "101"), ("64", "101")])
    text = _table(_result(interventions=changes))
    assert "set stats.lowest: 64 -> 101  (--set-at line 26@all, hit 4, in main)" in text, text
    assert "more change" not in text and "more writes" not in text, text
    # Two or more past the first few are counted; at one hit, by its number.
    more = _all("n", [("1", "2")] * 5)
    text = _table(_result(interventions=more))
    assert "set n: 2 more changes by --set-at line 26@all, at hits 4 to 5 (" in text, text
    more[4]["hit"] = 4
    text = _table(_result(interventions=more))
    assert "set n: 2 more changes by --set-at line 26@all, at hit 4 (" in text, text


def test_a_write_of_the_value_already_there_is_marked_and_counted_apart():
    # seats 3: `set free: 0 -> 0` counted as a change; 35 of the @all's 42 writes were.
    pairs = [("3", "0"), ("0", "0"), ("3", "0"), ("0", "0"), ("2", "0"), ("0", "0"), ("1", "0")]
    changes = _all("free", pairs, function="Seats.block(boolean[],int)")
    text = _table(_result(interventions=changes, language="java"))
    assert "set free: 3 -> 0  (--set-at line 26@all, hit 1, in Seats.block(" in text, text
    assert "set free: 0 -> 0, no change  (--set-at line 26@all, hit 2, in Seats.block(" in text
    assert (
        "set free: 4 more writes by --set-at line 26@all, at hits 4 to 7: 2 changes and 2 with "
        "no change (`--json` lists each)"
    ) in text, text
    every = _all("free", [("0", "0")] * 5)
    text = _table(_result(interventions=every, language="java"))
    assert "set free: 2 more writes by --set-at line 26@all, at hits 4 to 5, each with no" in text
    # Only an exact value's text shows it unchanged: never a float's (it may be rounded),
    # a text, or a Python object other than True, False and None.
    for before, language, same in (
        ("0", "c", True),
        ("'a'", "cpp", True),
        ("true", "rust", True),
        ("null", "java", True),
        ("0.1", "cpp", False),
        ('"a"', "java", False),
        ("0x0000000100000000", "c", False),
        ("1000", "python", False),
        ("0", "", False),
        ("None", "python", True),
    ):
        change = {"name": "x", "before": before, "after": before}
        assert probe_module.unchanged(change, language) is same, (before, language)
    assert not probe_module.unchanged({"name": "x", "before": "1", "after": "2"}, "c")
    # One change, not an @all's, is marked too.
    single = [{"name": "x", "before": "5", "after": "5", "hit": 1, "capture_hit": 1}]
    assert "set x: 5 -> 5, no change" in _table(_result(interventions=single))


def test_a_return_row_names_the_line_it_returned_to():
    # seats 5: `returned 49 Seats.block(...)`, where line 49 is in `book`, the caller.
    rows = [
        {
            "hit": 1,
            "phase": "returned",
            "line": 49,
            "function": "Seats.block(boolean[],int)",
            "selector": "(return)",
            "type": "int",
            "value": "2",
        }
    ]
    text = _table(_result(rows, language="java"))
    assert "1    returned  to 49  Seats.block(boolean[],int)  (return)  int   2" in text, text


class _Void:
    """A native runtime whose step origin is a function returning nothing (or not)."""

    reports_unavailable_as_text = True
    target_identity = {"language": "cpp"}

    def __init__(self, void):
        self.void = void

    @staticmethod
    def return_entry(variable):
        return NativeSession.return_entry(variable)

    def returns_nothing(self, frame):
        return self.void and frame["name"] == "Stats::reset()"


def _stepped_out(tmp_path, runtime, frame=True):
    origin = [
        {"name": "Stats::reset()", "instructionPointerReference": "0x10", "line": 26},
        {"name": "main", "instructionPointerReference": "0x30", "line": 61},
    ]
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.thread_id, worker.stop_reason = runtime, 7, "step"
    worker.step_origin = {
        "thread": 7,
        "function": "Stats::reset()",
        "frames": [(f["name"], f["instructionPointerReference"], f["line"]) for f in origin],
        "kind": "out",
        **({"frame": origin[0]} if frame else {}),
    }
    return worker, origin[1:]


def test_a_void_function_returns_no_value_rather_than_a_withheld_one(tmp_path):
    # grades 4: `session step --out` from `void reset()` printed `(return):  = <withheld:
    # the debugger reported no value …>`, with an empty type.
    worker, frames = _stepped_out(tmp_path, _Void(True))
    returned = worker.returned_value(worker.runtime, frames, None)
    assert returned == {
        "function": "Stats::reset()",
        "type": "void",
        "unavailable": True,
        "reason": "the function returns nothing",
    }
    row = _returned_row(
        {"hit": 1, "phase": "returned", "selector": "(return)"},
        {"returned": returned, "frames": [frames[0]]},
    )
    assert row["type"] == "void" and "value" not in row and row["unavailable"] is True
    assert row["error"] == "no value: the function returns nothing"
    assert cli._get_lines(row) == ["(return): void (no value: the function returns nothing)"]
    assert cli._returned_text(returned) == (
        "Stats::reset() returned: void (no value: the function returns nothing)"
    )
    # Not proven void (another function, or no origin frame to judge): withheld as before.
    for worker, frames in (
        _stepped_out(tmp_path, _Void(False)),
        _stepped_out(tmp_path, _Void(True), frame=False),
    ):
        returned = worker.returned_value(worker.runtime, frames, None)
        assert "type" not in returned and "reported no value" in returned["reason"], returned


def test_only_a_c_or_cpp_function_its_build_gives_no_return_type_returns_nothing():
    class Session:
        returns_nothing = NativeSession.returns_nothing

        def __init__(self, language, found):
            self.target, self.found = SimpleNamespace(language=language), found

        def _function_at(self, frame):
            return self.found

    assert Session("cpp", {"returns_nothing": True}).returns_nothing({}) is True
    assert Session("c", {"returns_nothing": True}).returns_nothing({}) is True
    assert Session("cpp", {"returns_nothing": False}).returns_nothing({}) is False
    # A frame Peeky cannot place, or a Rust function (whose unit return is not `void`).
    assert Session("cpp", None).returns_nothing({}) is False
    assert Session("rust", {"returns_nothing": True}).returns_nothing({}) is False


def test_session_paths_are_named_from_the_directory_the_session_started_in(tmp_path, monkeypatch):
    # grades 5: `session start` printed `grades.cpp:61`; a `step` run from another
    # directory printed `outputs/peeky-eval11-cases/grades/grades.cpp:26`.
    case = tmp_path / "outputs" / "grades"
    case.mkdir(parents=True)
    source = case / "grades.cpp"
    source.write_text("")
    monkeypatch.chdir(tmp_path)
    state = {"state": "stopped", "location": f"{source}:26", "function": "Stats::reset()"}
    assert cli._state_text(state).startswith("state: stopped at outputs/grades/grades.cpp:26 ")
    assert cli._state_text(state, base=str(case)).startswith("state: stopped at grades.cpp:26 ")
    assert cli._relative_location(f"{source}:26", str(case)) == "grades.cpp:26"
    # Outside the start's directory: whole, never from the working directory.
    assert cli._relative_location(f"{source}:26", str(tmp_path / "elsewhere")) == f"{source}:26"


def test_a_session_command_names_paths_as_its_start_did(tmp_path, monkeypatch, capsys):
    case = tmp_path / "grades"
    case.mkdir()
    (case / "grades.cpp").write_text("")
    record = {
        "name": "g",
        "root": str(case),
        "program": "grades.cpp",
        "language": "cpp",
        "cwd": str(case),
        "line": 61,
        "session_id": "0" * 32,
        "stop_id": "t",
        "state": "stopped",
    }

    class Client:
        def call(self, operation, **args):
            if operation == "report":
                return {
                    "state": "stopped",
                    "location": f"{case / 'grades.cpp'}:26",
                    "function": "Stats::reset()",
                    "steps": 1,
                    "hits": 2,
                    "output": "",
                }
            return {}

    monkeypatch.setattr(cli, "_load", lambda name: (record, Client()))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["session", "report", "--name", "g"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("state: stopped at grades.cpp:26 in Stats::reset()\n"), out


def test_a_session_report_says_where_it_is_and_how_far_it_came(tmp_path):
    # seats 2: `session report --name sold` printed only `state: stopped` and its output.
    class Dap:
        def snapshot_output(self):
            return "", False

        def recent_events(self):
            return []

    worker = DebugWorker(tmp_path)
    worker.dap, worker.session, worker.state, worker.stop_id = Dap(), "s", "stopped", "t"
    worker.stop_location, worker.stop_function = "/w/app/Seats.java:26", "Seats.block(int)"
    worker.stop_reason, worker.stop_hit, worker.hits, worker.steps = "breakpoint", 2, 2, 3
    report = worker.report(SimpleNamespace(manifest_id=None))
    assert {key: report[key] for key in ("location", "function", "hit", "hits", "steps")} == {
        "location": "/w/app/Seats.java:26",
        "function": "Seats.block(int)",
        "hit": 2,
        "hits": 2,
        "steps": 3,
    }
    assert cli._report_text({**report, "output": "SEATS:x\n"}) == (
        "state: stopped at /w/app/Seats.java:26 in Seats.block(int), hit 2\n"
        "so far: 2 hits (stops at the session's breakpoint), 3 steps\n"
        "output:\nSEATS:x"
    )
    # Once a stop could not be numbered, no count of hits; once ended, no place.
    worker.hits = None
    worker.forget_stop()
    worker.state = "terminated"
    report = worker.report(SimpleNamespace(manifest_id=None))
    assert "hits" not in report and "location" not in report and report["steps"] == 3
    assert cli._report_text(report) == "state: terminated\nso far: 3 steps\noutput:\n(none)"
    # An older worker's report: as before.
    assert cli._report_text({"state": "running", "output": ""}) == "state: running\noutput:\n(none)"


def test_a_step_onto_the_breakpoint_line_says_whether_it_is_a_hit(tmp_path):
    # seats 6: a `step` that landed on the breakpoint's line 26 showed no `hit N`.
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "Seats.java").write_text("")
    record = {"root": str(tmp_path), "program": "app/Seats.java", "line": 26, "language": "java"}
    state = {"state": "stopped", "location": f"{tmp_path}/app/Seats.java:26", "reason": "step"}
    note = cli._step_onto_breakpoint(record, state)
    assert note.startswith("not a hit: java-debug reports this stop as the step's, and ")
    assert "runs on to the line's next pass" in note
    native = {**record, "language": "cpp"}
    assert "`session continue` stops here again first, as the next hit" in (
        cli._step_onto_breakpoint(native, state)
    )
    # Another line, or another file: no note.
    for elsewhere in ("app/Seats.java:25", "app/Seats.java:27", "Other.java:26"):
        assert (
            cli._step_onto_breakpoint(record, {**state, "location": f"{tmp_path}/{elsewhere}"})
            is None
        )
    assert cli._step_onto_breakpoint({**record, "line": None}, state) is None


def test_queries_runs_shows_the_end_of_a_long_output(tmp_path):
    # seats 7: output_start cut both runs at `…;FREE`, hiding `=39` against `=32`.
    store = tmp_path / "evidence.sqlite"
    outputs = (
        "SEATS:Kim=C3-6,Okafor=C5-7,Lund=C7-11,Ng=C1-2,Sato=D2-7;FREE=39\n",
        "SEATS:Kim=D4-7,Okafor=C1-3,Lund=B1-5,Ng=C6-7,Sato=B6-11;FREE=32\n",
        "PAGE: [1]\n",
    )
    for output in outputs:
        evidence.record(
            store,
            {
                "probe": {"program": "app/Seats.java", "line": 26, "capture": ["free"]},
                "status": "completed",
                "hits": 0,
                "seconds": 0.1,
                "rows": [],
                "interventions": [],
                "output": output,
            },
        )
    short, fixed, broken = evidence.run_query(store, "runs")["rows"]
    assert broken["output_start"].endswith(";FREE") and fixed["output_start"].endswith(";FREE")
    # Its last 40 characters, without the final newline.
    for run, output in ((broken, outputs[0]), (fixed, outputs[1])):
        assert run["output_end"] == "…" + output.rstrip("\n")[-40:]
        assert run["output_end"].endswith(output.strip()[-7:])
    assert short["output_start"] == "PAGE: [1]\n" and short["output_end"] is None


def test_a_candidate_says_which_of_its_lines_the_at_line_is_before_its_breakpoint(
    tmp_path, monkeypatch
):
    # tides 5: `breakpoint tides.c:37 in the candidate verified` for `--at tides.c:40`; the
    # mapping came only at the end.
    source = tmp_path / "tides.c"
    source.write_text(
        "".join(f"line {n}\n" for n in range(1, 50)).replace("line 33\n", "a\nb\nc\nd\n")
    )

    class Worker:
        adapter = None

        def path(self, program):
            return source

        def dispatch(self, operation, args):
            if operation == "launch":
                return {
                    "session_id": "s",
                    "breakpoints": [
                        {"id": 1, "verified": True, "line": args["breakpoints"][0]["line"]}
                    ],
                    "artifact_manifest_id": None,
                    "target_identity": {"language": "c"},
                    "source_sha256": "x",
                }
            if operation == "wait":
                return {"state": "terminated"}
            if operation == "report":
                return {"output": "", "output_truncated": False, "exit_code": 0}
            return {}

        def close(self):
            pass

    said = []
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: Worker())
    edit = ("a\nb\nc\nd\n", "line 33\n")
    probe_module.run_probe(
        str(tmp_path), Probe("tides.c", 40, ("turn",), edit=edit), progress=said.append
    )
    assert said[0] == (
        "started; line 40 is line 37 of the candidate; breakpoint tides.c:37 in the candidate "
        "verified"
    ), said


class _Stops:
    """Stops once at line 9 for each of ``count`` --set-at changes, never at the --at line,
    then ends with ``exit_code``."""

    def __init__(self, count, exit_code=0):
        self.left, self.exit_code, self.value = count, exit_code, "1"

    def dispatch(self, operation, args):
        if operation == "launch":
            return {
                "session_id": "s",
                "breakpoints": [
                    {"id": index, "verified": True, "line": point["line"]}
                    for index, point in enumerate(args["breakpoints"], 1)
                ],
                "artifact_manifest_id": None,
                "target_identity": {"language": "c"},
                "source_sha256": "x",
            }
        if operation == "wait":
            if not self.left:
                return {"state": "terminated"}
            self.left -= 1
            return {
                "state": "stopped",
                "stop_id": f"t{self.left}",
                "event": {"body": {"reason": "breakpoint"}},
                "location": "tides.c:9",
            }
        if operation == "inspect":
            return {
                "frames": [{"name": "find_waters", "source": {"path": "/w/tides.c"}, "line": 9}],
                "scopes": [
                    {
                        "name": "Locals",
                        "reference": 1,
                        "variables": [
                            {"name": "trend", "value": self.value, "variablesReference": 0}
                        ],
                    }
                ],
            }
        if operation == "set_variable":
            before, self.value = self.value, args["value"]
            return {"name": args["name"], "before": before, "after": args["value"]}
        if operation == "report":
            return {"output": "", "output_truncated": False, "exit_code": self.exit_code}
        return {}

    def close(self):
        pass


@pytest.mark.parametrize(
    "exit_code, message, hint",
    [
        (
            0,
            "The program finished without stopping at tides.c:8, after 2 changes by --set-at: "
            "they may have kept the line from running",
            "Probe the line without the changes to see whether it runs",
        ),
        (
            3,
            "The program failed (exit code 3) without stopping at tides.c:8, after 2 changes "
            "by --set-at, which may have made it fail; its output says why",
            "Read its output: a change may have made it fail.",
        ),
        (
            None,
            "The program ended without stopping at tides.c:8 (exit code unknown), after 2 "
            "changes by --set-at: they may have kept the line from running, or made it fail",
            "Read its output for an error",
        ),
    ],
)
def test_no_stop_after_changes_says_they_may_have_kept_the_line_from_running(
    monkeypatch, exit_code, message, hint
):
    # Prep 14: tides, `--at 33` with six change-and-restore --set-at: every change was made,
    # and NO_STOP said to pick a line the program executes.
    worker = _Stops(2, exit_code)
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    request = Probe("tides.c", 8, ("trend",), set_at=((9, 1, "trend", "0"), (9, 2, "trend", "1")))
    result = probe_module.run_probe("/w", request)
    assert result["status"] == "no-stop" and len(result["interventions"]) == 2, result
    assert result["error"]["code"] == "NO_STOP"
    assert result["error"]["message"] == message
    assert result["error"]["hint"].startswith(hint) and len(result["error"]["hint"]) <= 150
    # Without changes, the line is blamed as before.
    worker = _Stops(0, 0)
    result = probe_module.run_probe("/w", Probe("tides.c", 8, ("trend",)))
    assert result["error"]["hint"] == "Pick a line the program executes; see `peeky describe`."


def test_verify_takes_a_java_targets_one_source_without_at():
    # Prep 13: `verify` on the seats directory without `--at app/Seats.java` was refused,
    # though its config declares that one source.
    assert cli._default_program(None, {"sources": ["app/Seats.java"]}, "verify") == "app/Seats.java"
    assert cli._default_program("app/A.java", {"sources": ["app/B.java"]}, "verify") == "app/A.java"
    with pytest.raises(ValueError) as refused:
        cli._default_program(None, {"sources": ["a/One.java", "b/Two.java"]}, "verify")
    assert str(refused.value) == (
        "verify needs --at <source> (one of: a/One.java, b/Two.java) for this target: the file "
        "the edit changes, as `peeky describe` shows (`--at a/One.java`)"
    )
    with pytest.raises(ValueError, match=r"^verify needs --at <source> for this target$"):
        cli._default_program(None, {}, "verify")
