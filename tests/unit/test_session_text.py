"""What `peeky session` commands print: a stop's state and report, its paths, `get`'s
children and hints, and notes on where a step stopped."""

from types import SimpleNamespace

import pytest

from peeky import cli
from peeky.cli import _get_lines
from peeky.worker import DebugWorker


def test_session_hints_name_the_session():
    # L1: hints in `session get` children and state lines omitted `--name X`, so a pasted
    # one acted on the `default` session.
    from peeky.cli import _get_lines, _state_text

    running = _state_text({"state": "running"}, "calc")
    assert "`peeky session wait --name calc`" in running, running
    noted = _state_text(
        {
            "state": "stopped",
            "location": "Calc.java:24",
            "note": "stopped at the session's breakpoint (line 24, hit 2) before reaching "
            "line 30; `session continue --to 30` again",
        },
        "calc",
    )
    assert "`session continue --to 30 --name calc` again" in noted, noted
    assert "--name" not in _state_text({"state": "running"}), "default keeps the short hint"
    row = {"selector": "xs", "type": "int[]", "value": "[1, 2]"}
    row["children"] = [{"selector": "xs[0]", "name": "[0]", "value": "1", "expandable": True}]
    (line, child) = _get_lines(row, "calc")
    assert child.endswith("(children: peeky session get 'xs[0]' --name calc)"), child


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


VISIBLE = "count, e, expenses, friends, iter @ main.rs:64, iter @ main.rs:74, k, name, payer, share"


def test_session_get_lists_the_visible_names_once():
    from peeky.cli import _repeats_shortened

    rows = [
        {"selector": "nosuch", "error": f"not found: 'nosuch' …; visible: {VISIBLE}"},
        {"selector": "nosuch2", "error": f"not found: 'nosuch2' …; visible: {VISIBLE}"},
        {"selector": "k", "type": "usize", "value": "1"},
    ]
    shown = _repeats_shortened(rows)
    assert shown[0] == rows[0] and shown[2] == rows[2]
    assert shown[1]["error"] == "not found: 'nosuch2' …; visible: as listed above"
    assert rows[1]["error"].endswith(VISIBLE)


def test_session_paths_are_named_from_the_directory_the_session_started_in(tmp_path, monkeypatch):
    # grades 5: `session start` printed `grades.cpp:61`; a `step` run from another
    # directory printed `<cases directory>/grades/grades.cpp:26`.
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
    worker.stop_location, worker.stop_function = (
        "/peeky-test-src/app/Seats.java:26",
        "Seats.block(int)",
    )
    worker.stop_reason, worker.stop_hit, worker.hits, worker.steps = "breakpoint", 2, 2, 3
    report = worker.report(SimpleNamespace(manifest_id=None))
    assert {key: report[key] for key in ("location", "function", "hit", "hits", "steps")} == {
        "location": "/peeky-test-src/app/Seats.java:26",
        "function": "Seats.block(int)",
        "hit": 2,
        "hits": 2,
        "steps": 3,
    }
    assert cli._report_text({**report, "output": "SEATS:x\n"}) == (
        "state: stopped at /peeky-test-src/app/Seats.java:26 in Seats.block(int), hit 2\n"
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


def test_session_get_lists_an_arrays_first_elements():
    # playlist 4: `session get queue` listed all 32 elements, each with its hint.
    row = {
        "selector": "levels",
        "type": "Level[32]",
        "value": "[{n = 1}, …]",
        "children": [
            {"name": f"[{i}]", "selector": f"levels[{i}]", "type": "Level", "value": "{n = 1}"}
            | {"expandable": True}
            for i in range(32)
        ],
    }
    lines = cli._get_lines(row)
    assert len(lines) == 1 + 8 + 1, lines
    assert lines[8].startswith("  levels[7]: Level = {n = 1} (children:"), lines
    assert lines[-1] == (
        "  ... 24 more elements, levels[8] to levels[31]: `peeky session get 'levels[8]'` reads "
        "one; `--json` lists them all"
    )
    assert "--name s --frame 1` reads one" in cli._get_lines(row | {"frame": 1}, "s")[-1]
    # Review of PR #350: the debugger lists one page (64) of a longer array, and JSON has
    # no more than that page.
    page = {
        **row,
        "type": "Level[100]",
        "children": [{**row["children"][0], "selector": f"levels[{i}]"} for i in range(64)],
        "children_truncated": True,
    }
    lines = cli._get_lines(page)
    assert lines[-1] == (
        "  ... 56 more listed, levels[8] to levels[63], then more (100 elements in all): "
        "`--json` has only the first 64; read any element by index, as `peeky session get "
        "'levels[8]'`"
    ), lines
    assert "more children" not in "\n".join(lines) and len(lines) == 10
    # The whole length only where the type or value gives it: Rust `[T; N]`, Java's value.
    for kind, value, size in (
        ("[u32; 70]", "[1, 2, …]", " (70 elements in all)"),
        ("int[]", "int[90]@55 [1, 2, …]", " (90 elements in all)"),
        ("Vec<u32>", "vec![1, 2, …]", ""),
    ):
        shown = cli._get_lines({**page, "type": kind, "value": value})[-1]
        assert f"then more{size}: `--json` has only the first 64" in shown, shown
    # A struct's fields, or a short array, are listed whole.
    fields = {
        **row,
        "children": [{**c, "selector": f"levels.f{n}"} for n, c in enumerate(row["children"])],
    }
    assert len(cli._get_lines(fields)) == 33
    assert len(cli._get_lines({**row, "children": row["children"][:8]})) == 9


@pytest.mark.parametrize(
    ("language", "source", "note"),
    [
        ("rust", "            continue; // the same pass", "line 4 is `continue`, not run yet"),
        ("python", "            continue  # no reading", "line 4 is `continue`, not run yet"),
        ("cpp", "        } else {", "line 4 is `} else {`: its code is often the jump"),
        ("java", "        } else if (cmd == '<') {", "line 4 starts `} else if`: its code may"),
        ("python", "        else:", None),
        ("cpp", "            continue_with(x);", None),
        ("cpp", "        if (x) {", None),
    ],
)
def test_a_step_onto_a_jump_line_says_what_it_does(tmp_path, language, source, note):
    # laps 7, playlist 7: a step onto `continue;` or `} else {` came with no word.
    program = tmp_path / "main.src"
    program.write_text("a\nb\nc\n" + source + "\ne\n")
    record = {"root": str(tmp_path), "program": "main.src", "language": language}
    state = {"state": "stopped", "location": f"{program}:4", "reason": "step"}
    said = cli._step_line_note(record, state)
    assert (said is None) if note is None else said.startswith(note), said
    if said:
        assert len(said) <= 150, said
    assert cli._step_line_note(record, {**state, "location": "main.src:4"}) == said
    assert cli._step_line_note(record, {**state, "location": f"{program}:9"}) is None
    assert cli._step_line_note(record, {**state, "state": "terminated"}) is None
