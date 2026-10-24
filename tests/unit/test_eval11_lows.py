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


def _proven(changes):
    """The worker's proof that a write left the value as it was (worker._written)."""
    return [
        {**change, "unchanged": True} if change["before"] == change["after"] else change
        for change in changes
    ]


def test_a_write_of_the_value_already_there_is_marked_and_counted_apart():
    # seats 3: `set free: 0 -> 0` counted as a change; 35 of the @all's 42 writes were.
    pairs = [("3", "0"), ("0", "0"), ("3", "0"), ("0", "0"), ("2", "0"), ("0", "0"), ("1", "0")]
    changes = _proven(_all("free", pairs, function="Seats.block(boolean[],int)"))
    text = _table(_result(interventions=changes, language="java"))
    assert "set free: 3 -> 0  (--set-at line 26@all, hit 1, in Seats.block(" in text, text
    assert "set free: 0 -> 0, no change  (--set-at line 26@all, hit 2, in Seats.block(" in text
    assert (
        "set free: 4 more writes by --set-at line 26@all, at hits 4 to 7: 2 changes and 2 with "
        "no change (`--json` lists each)"
    ) in text, text
    every = _proven(_all("free", [("0", "0")] * 5))
    text = _table(_result(interventions=every, language="java"))
    assert "set free: 2 more writes by --set-at line 26@all, at hits 4 to 5, each with no" in text
    # Review of PR #314: java-debug shows a char bare, so five `+ -> +` writes were "2 more
    # changes". The worker's proof decides it, for a root local (`sign`) and a field alike.
    for name in ("sign", "chars.mark"):
        chars = _proven(_all(name, [("+", "+")] * 5))
        text = _table(_result(interventions=chars, language="java"))
        assert f"set {name}: + -> +, no change  (--set-at line 26@all, hit 3, in main)" in text
        assert (
            f"set {name}: 2 more writes by --set-at line 26@all, at hits 4 to 5, each with no "
            "change (`--json` lists each)"
        ) in text, text
    # Round 2: the text alone is never proof (a Java String, a Python `1000`, a float, or
    # an older worker's result): such writes are counted as changes, never as no-ops.
    alike = _all("x", [("1000", "1000")] * 5)
    text = _table(_result(interventions=alike, language="python"))
    assert "set x: 1000 -> 1000  (--set-at line 26@all, hit 1, in main)" in text, text
    assert "set x: 2 more changes by --set-at line 26@all, at hits 4 to 5 (" in text, text
    assert not probe_module.unchanged({"before": "1", "after": "1"})
    assert not probe_module.unchanged({"before": "1", "after": "2", "unchanged": True})
    assert probe_module.unchanged({"before": "1", "after": "1", "unchanged": True})
    # One change, not an @all's, is marked too.
    single = [{"name": "x", "before": "5", "after": "5", "hit": 1, "unchanged": True}]
    assert "set x: 5 -> 5, no change" in _table(_result(interventions=single))


class _Runtime:
    def __init__(self, exact):
        self.exact, self.asked = exact, []

    def exact_text(self, variable, frame=None, root=None, steps=()):
        self.asked.append((variable["name"], frame, root, steps))
        return self.exact


def test_a_change_says_its_type_after_the_write_and_whether_it_changed_anything():
    # Round 2 of PR #314: the type came from the value read before the write, so a Python
    # `callback` set from `print` to `None` said `builtin_function_or_method`.
    from peeky.worker import _written

    callback = {"name": "callback", "type": "builtin_function_or_method", "value": "<print>"}
    reply = {"value": "None", "type": "NoneType"}
    assert _written(_Runtime(True), callback, reply, "<print>", "None") == {"type": "NoneType"}
    # No type in the reply: none is said.
    assert _written(_Runtime(True), callback, {"value": "None"}, "<print>", "None") == {}
    # The same text proves nothing until the runtime proves it exact for the value's type.
    flag = {"name": "flag", "type": "bool", "value": "True"}
    proof = _Runtime(True)
    assert _written(proof, flag, {"type": "bool"}, "True", "True", "frame", flag, ("a",)) == {
        "type": "bool",
        "unchanged": True,
    }
    assert proof.asked == [("flag", "frame", flag, ("a",))]
    assert _written(_Runtime(False), flag, {"type": "bool"}, "True", "True") == {"type": "bool"}
    assert _written(object(), flag, {"type": "bool"}, "True", "True") == {"type": "bool"}
    assert _written(_Runtime(True), {"name": "t"}, {"type": "T" * 400}, "1", "2") == {
        "type": "T" * 300
    }


def test_each_runtime_proves_a_no_op_by_the_values_type_never_its_name():
    from peeky.adapters import PythonAdapter
    from peeky.java import JavaSession

    # Java: a primitive but a float or double (a primitive's name is a keyword).
    for kind, exact in (
        ("char", True),
        ("int", True),
        ("boolean", True),
        ("long", True),
        ("double", False),
        ("float", False),
        ("String", False),
        ("Integer", False),
    ):
        assert JavaSession.exact_text({"type": kind}) is exact, kind
    # Python: True, False and None, which are always the same objects.
    for kind, value, exact in (
        ("bool", "True", True),
        ("NoneType", "None", True),
        ("int", "1000", False),
        ("str", "'a'", False),
        ("MyFlag", "True", False),
    ):
        assert PythonAdapter.exact_text({"type": kind, "value": value}) is exact, (kind, value)

    # C, C++ and Rust: the type the debug information gives, through typedefs. Round 2:
    # `typedef float int64_t;` (tests/cpp/test_cpp_cli.py) was taken for an integer.
    graph = {
        1: ("typedef", "int64_t", 2),
        2: ("base", "float", "DW_ATE_float", 4),
        3: ("typedef", "count_t", 4),
        4: ("qualifier", "restrict", 5),
        5: ("base", "long", "DW_ATE_signed", 8),
        6: ("base", "bool", "DW_ATE_boolean", 1),
        7: ("base", "char", "DW_ATE_signed_char", 1),
        8: ("pointer", "", 5, 8),
        9: ("base", "double", "DW_ATE_float", 8),
    }

    class Session:
        exact_text = NativeSession.exact_text

        def __init__(self, kind):
            self.kind = kind

        def _root_type(self, frame, root):
            if self.kind is None:
                from peeky.fields import Unsettable

                raise Unsettable("not read")
            return self.kind, True, graph

    lossy = {"name": "lossy", "type": "int64_t", "value": "1"}
    for kind, exact in ((1, False), (3, True), (6, True), (7, True), (8, False), (9, False)):
        assert Session(kind).exact_text(lossy, frame={}, root=lossy) is exact, kind
    # Where Peeky cannot read the type, or has no frame: never proven.
    assert Session(None).exact_text(lossy, frame={}, root=lossy) is False
    assert Session(3).exact_text(lossy) is False


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
    """Stops once at each of ``lines`` (a --set-at line's stops), never at the --at line,
    then ends with ``exit_code``. `trend` holds "1" until a change."""

    def __init__(self, lines, exit_code=0, proven=True):
        self.lines, self.exit_code, self.value, self.line = list(lines), exit_code, "1", None
        # Whether the worker proves a write of the same text a no-op (worker._written).
        self.proven = proven

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
            if not self.lines:
                return {"state": "terminated"}
            self.line = self.lines.pop(0)
            return {
                "state": "stopped",
                "stop_id": f"t{len(self.lines)}",
                "event": {"body": {"reason": "breakpoint"}},
                "location": f"tides.c:{self.line}",
            }
        if operation == "inspect":
            frame = {"name": "find_waters", "source": {"path": "/w/tides.c"}, "line": self.line}
            return {
                "frames": [frame],
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
            same = self.proven and before == args["value"]
            return {
                "name": args["name"],
                "before": before,
                "after": args["value"],
                "type": "int",
                **({"unchanged": True} if same else {}),
            }
        if operation == "report":
            return {"output": "", "output_truncated": False, "exit_code": self.exit_code}
        return {}

    def close(self):
        pass


RAN = "The run made 2 changes by --set-at (at line 9): compare with a run without them"


@pytest.mark.parametrize(
    "exit_code, message, hint",
    [
        (
            0,
            f"The program finished without stopping at tides.c:8. {RAN}",
            "Probe the line without the changes to compare; if it never stops there either, "
            "pick a line the program executes (`peeky describe`).",
        ),
        (
            3,
            f"The program failed (exit code 3) without stopping at tides.c:8; its output says "
            f"why. {RAN}",
            "Read its output, and check its arguments and input; probe without the changes to "
            "compare.",
        ),
        (
            None,
            f"The program ended without stopping at tides.c:8 (exit code unknown). {RAN}",
            "If the output shows an error, the program failed before the line; else probe "
            "without the changes to compare, or pick a line it executes.",
        ),
    ],
)
def test_no_stop_after_changes_counts_them_and_claims_no_cause(
    monkeypatch, exit_code, message, hint
):
    # Prep 14: tides, `--at 33` with six change-and-restore --set-at: every change was made,
    # and NO_STOP said to pick a line the program executes. Review of PR #314: nothing proves
    # a change came before the line's chance to run, so the message claims no cause.
    worker = _Stops([9, 9], exit_code)
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    request = Probe("tides.c", 8, ("trend",), set_at=((9, 1, "trend", "0"), (9, 2, "trend", "1")))
    result = probe_module.run_probe("/w", request)
    assert result["status"] == "no-stop" and len(result["interventions"]) == 2, result
    assert result["error"]["code"] == "NO_STOP"
    assert result["error"]["message"] == message
    assert result["error"]["hint"] == hint and len(hint) <= 150
    # Without changes, the line is blamed as before.
    worker = _Stops([], 0)
    result = probe_module.run_probe("/w", Probe("tides.c", 8, ("trend",)))
    assert result["error"]["hint"] == "Pick a line the program executes; see `peeky describe`."


def _no_stop(monkeypatch, lines, set_at, proven=True):
    worker = _Stops(lines, proven=proven)
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("tides.c", 8, ("trend",), set_at=set_at))
    assert result["status"] == "no-stop", result
    return result


def test_no_stop_after_writes_proven_to_change_nothing_blames_the_line(monkeypatch):
    # Review of PR #314: `trend: 1 -> 1` counted as a change ("after 1 change by --set-at:
    # they may have kept the line from running"). A write proven to change nothing is not
    # one: where every write is such, the line is blamed, as with none.
    result = _no_stop(monkeypatch, [9, 9], ((9, 1, "trend", "1"), (9, 2, "trend", "1")))
    assert [(c["before"], c["after"]) for c in result["interventions"]] == [("1", "1")] * 2
    assert result["error"]["message"] == (
        "The program finished without stopping at tides.c:8; check `peeky describe` for "
        "executable lines"
    )
    assert result["error"]["hint"] == "Pick a line the program executes; see `peeky describe`."
    # One proven no-op and one change: the change alone is counted.
    result = _no_stop(monkeypatch, [9, 9], ((9, 1, "trend", "1"), (9, 2, "trend", "5")))
    assert result["error"]["message"] == (
        "The program finished without stopping at tides.c:8. The run made 1 change by "
        "--set-at (at line 9): compare with a run without them"
    )


def test_no_stop_after_writes_of_the_same_text_not_proven_no_ops_names_them(monkeypatch):
    # Round 2 of PR #314: the same text is not always a no-op (a Java String written alike
    # is a new object; a Python `1000` may be another). Such writes are named, never taken
    # for no-ops, and never blamed on the line.
    result = _no_stop(
        monkeypatch, [9, 9], ((9, 1, "trend", "1"), (9, 2, "trend", "1")), proven=False
    )
    assert result["error"]["message"] == (
        "The program finished without stopping at tides.c:8. The run made 2 writes by "
        "--set-at that kept the same text, not proven to change nothing (at line 9): "
        "compare with a run without them"
    )
    # A change, a write not proven a no-op, and one proven: the first two are named.
    worker = _Stops([9, 20, 31])

    def dispatch(operation, args, plain=worker.dispatch):
        reply = plain(operation, args)
        if operation == "set_variable" and worker.line == 20:
            reply.pop("unchanged", None)
        return reply

    worker.dispatch = dispatch
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    request = Probe(
        "tides.c",
        8,
        ("trend",),
        set_at=((9, 1, "trend", "0"), (20, 1, "trend", "0"), (31, 1, "trend", "0")),
    )
    result = probe_module.run_probe("/w", request)
    assert [c.get("unchanged") for c in result["interventions"]] == [None, None, True]
    assert result["error"]["message"] == (
        "The program finished without stopping at tides.c:8. The run made 1 change by "
        "--set-at (at line 9), and 1 write by --set-at that kept the same text, not proven to "
        "change nothing (at line 20): compare with a run without them"
    )


def test_no_stop_after_a_change_past_the_line_claims_no_cause(monkeypatch):
    # Review of PR #314: a --set-at on a line after the --at line got the same causal
    # wording. Where they were is said; that they kept the line from running is not.
    result = _no_stop(monkeypatch, [20], ((20, 1, "trend", "0"),))
    message = result["error"]["message"]
    assert message == (
        "The program finished without stopping at tides.c:8. The run made 1 change by "
        "--set-at (at line 20): compare with a run without them"
    )
    result = _no_stop(
        monkeypatch,
        [9, 20, 31],
        ((9, 1, "trend", "0"), (20, 1, "trend", "2"), (31, 1, "trend", "3")),
    )
    assert (
        "The run made 3 changes by --set-at (at lines 9, 20 and 31)" in result["error"]["message"]
    )
    for text in (message, result["error"]["message"], result["error"]["hint"]):
        assert "kept" not in text and "may have" not in text, text


def test_verify_takes_a_java_targets_one_source_without_at():
    # Prep 13: `verify` on the seats directory without `--at app/Seats.java` was refused,
    # though its config declares that one source.
    assert cli._default_program(None, {"sources": ["app/Seats.java"]}, "verify") == "app/Seats.java"
    assert cli._default_program("app/A.java", {"sources": ["app/B.java"]}, "verify") == "app/A.java"
    with pytest.raises(ValueError) as refused:
        cli._default_program(None, {"sources": ["a/One.java", "b/Two.java"]}, "verify")
    # The edited file is the one holding --old, whichever of them that is.
    assert str(refused.value) == (
        "verify needs --at <source> (one of: a/One.java, b/Two.java) for this target: pass "
        "`--at <the source containing --old>`, the file the edit changes"
    )
    with pytest.raises(ValueError, match=r"^verify needs --at <source> for this target$"):
        cli._default_program(None, {}, "verify")
