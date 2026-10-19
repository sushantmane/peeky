"""Candidate probes (`probe --old/--new`): line mapping, validation, errors and evidence."""

import hashlib
import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

from peeky import evidence
from peeky.build import BuildCommandError
from peeky.errors import classify
from peeky.probe import Probe, candidate_line, combine_edits

TEXT = "a;\nb;\nc;\nd;\n"


@pytest.mark.parametrize(
    ("old", "new", "line", "expected"),
    [
        ("b;\n", "b;\nx;\n", 1, 1),  # before the edit
        ("b;\n", "b;\nx;\n", 3, 4),  # after an edit that adds a line
        ("b;\nc;\n", "bc;\n", 4, 3),  # after an edit that removes a line
        ("b;", "B;", 2, 2),  # inside an edit that keeps the line count
        ("b;\nc;", "B;\nC;", 3, 3),
        ("c;\n", "c;\n\n\n", 2, 2),  # the edit ends with a newline: the next line is outside
        ("c;\n", "c;\n\n\n", 4, 6),
    ],
)
def test_original_lines_map_into_the_candidate(old, new, line, expected):
    assert candidate_line(TEXT, (old, new), line) == expected


@pytest.mark.parametrize(
    ("old", "new", "line", "words"),
    [
        ("b;", "b;\nx;", 2, "line 2, which becomes lines 2-3"),
        ("b;\nc;", "bc;", 3, "lines 2-3, which become line 2"),
    ],
)
def test_a_line_inside_an_edit_that_changes_the_line_count_is_refused(old, new, line, words):
    with pytest.raises(ValueError, match="inside the edit") as caught:
        candidate_line(TEXT, (old, new), line)
    assert words in str(caught.value) and "before or after the edit" in str(caught.value)


@pytest.mark.parametrize("new", ["B;", "B;\nx;"])
def test_a_line_joined_onto_the_edit_is_refused(new):
    # --old ends with a newline, --new does not: original line 3 joins the edit's last line.
    with pytest.raises(ValueError, match="joins it onto line") as caught:
        candidate_line(TEXT, ("b;\n", new), 3)
    assert "end --new with a newline" in str(caught.value)
    assert candidate_line(TEXT, ("b;\n", new), 4) == 4 + new.count("\n") - 1


def test_an_edit_that_empties_the_start_of_the_file_keeps_the_next_line():
    assert candidate_line(TEXT, ("a;\n", ""), 2) == 1


def test_an_edit_must_match_exactly_once():
    with pytest.raises(ValueError, match="exactly once"):
        candidate_line("x;\nx;\n", ("x;", "y;"), 1)
    with pytest.raises(ValueError, match="exactly once"):
        candidate_line(TEXT, ("zz", "y"), 1)


@pytest.mark.parametrize("edit", [("", "x"), ("a", "a"), ("a",), ["a", "b"], ("a", 1)])
def test_probe_edits_are_validated(edit):
    with pytest.raises(ValueError, match="candidate edit"):
        Probe("x.c", 1, edit=edit)


@pytest.mark.parametrize(
    ("result", "words"),
    [
        ({"exit_code": 1, "stderr": "x.c:5:9: error: expected expression\n"}, "exited with code 1"),
        ({"exit_code": None, "timed_out": True, "stderr": ""}, "timed out"),
        (
            {"exit_code": 1, "output_limit_exceeded": True, "stdout": "y" * 5000},
            "wrote more output",
        ),
    ],
)
def test_build_failures_are_build_failed_with_the_compilers_words(result, words):
    error = classify(BuildCommandError(["/usr/bin/clang", "-g"], result), session_closed=False)
    assert error["code"] == "BUILD_FAILED" and error["session_closed"] is True
    assert error["message"].startswith(f"The build failed: clang {words}")
    assert "{" not in error["message"] and len(error["message"]) <= 2000
    if result.get("stderr"):
        assert "expected expression" in error["message"]


def test_a_cargo_build_failure_shows_rustcs_errors_not_only_cargos_footer():
    messages = [
        {"reason": "compiler-artifact"},
        {
            "reason": "compiler-message",
            "message": {"level": "warning", "rendered": "src/main.rs:2:9: warning: unused\n"},
        },
        {
            "reason": "compiler-message",
            "message": {
                "level": "error",
                "rendered": "src/main.rs:3:22: error[E0308]: mismatched types\n",
            },
        },
        {"reason": "build-finished", "success": False},
    ]
    result = {
        "exit_code": 101,
        "stdout": "".join(json.dumps(message) + "\n" for message in messages),
        "stderr": "   Compiling app v0.1.0 (/tmp/peeky-build-x)\n"
        'error: could not compile `app` (bin "app") due to 1 previous error\n',
    }
    error = classify(BuildCommandError(["/x/cargo", "build"], result), session_closed=False)
    assert error["message"].splitlines() == [
        "The build failed: cargo exited with code 101",
        "src/main.rs:3:22: error[E0308]: mismatched types",
        'error: could not compile `app` (bin "app") due to 1 previous error',
    ]


def v1_store(path, version=1, views=True):
    """A store as an older Peeky wrote it, holding one run with a capture.

    Version 1 has no runs.edit; version 2 (an early candidate-probe build) has the column
    but views without `candidate`.
    """
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    for statement in evidence._DDL:
        if statement.startswith("CREATE VIEW"):
            if not views:
                continue
            statement = statement.replace(", r.edit IS NOT NULL AS candidate", "")
        if version == 1:
            statement = statement.replace(", edit TEXT)", ")")
        connection.execute(statement)
    connection.execute(f"PRAGMA user_version = {version}")
    connection.execute("INSERT INTO runs (started_at, program, line) VALUES ('t', 'x.c', 3)")
    connection.execute(
        "INSERT INTO captures (run_id, hit, phase, line, selector, value, unavailable) "
        "VALUES (1, 1, 'observed', 3, 'n', '7', 0)"
    )
    connection.commit()
    connection.close()


@pytest.mark.parametrize(("version", "views"), [(1, True), (2, True), (1, False)])
def test_older_stores_are_upgraded_in_place_keeping_their_runs(tmp_path, version, views):
    store = tmp_path / "evidence" / "evidence.db"
    v1_store(store, version, views)
    rows = evidence.run_query(store, "values", {"selector": "n"})["rows"]
    assert [(row["program"], row["value"]) for row in rows] == [("x.c", "7")]
    connection = sqlite3.connect(store)
    assert connection.execute("PRAGMA user_version").fetchone() == (evidence.SCHEMA_VERSION,)
    assert connection.execute("SELECT candidate FROM evidence").fetchall() == [(0,)]
    run = evidence.record(store, {"probe": {"program": "y.c", "line": 4}, "status": "completed"})
    assert connection.execute("SELECT COUNT(*), MAX(run_id) FROM runs").fetchone() == (2, run)


def test_a_save_upgrades_a_schema_1_store(tmp_path):
    store = tmp_path / "evidence" / "evidence.db"
    v1_store(store)
    evidence.record(store, {"probe": {"program": "y.c", "line": 4}, "status": "completed"})
    connection = sqlite3.connect(store)
    assert connection.execute("PRAGMA user_version").fetchone() == (evidence.SCHEMA_VERSION,)
    assert connection.execute("SELECT edit FROM runs").fetchall() == [(None,), (None,)]


def test_a_read_only_schema_1_store_is_refused_unchanged(tmp_path, monkeypatch):
    store = tmp_path / "evidence" / "evidence.db"
    v1_store(store)
    before = store.read_bytes()
    # Simulated: file modes do not stop root (CI's Linux image runs as root).
    real = evidence.os.access
    monkeypatch.setattr(
        evidence.os, "access", lambda path, mode: mode != evidence.os.W_OK and real(path, mode)
    )
    with pytest.raises(ValueError, match="read-only"):
        evidence.query(store, "SELECT * FROM runs")
    assert store.read_bytes() == before


def test_candidate_runs_are_marked_and_kept_apart(tmp_path):
    store = tmp_path / "evidence.db"
    rows = [{"hit": 1, "phase": "observed", "line": 5, "selector": "n", "value": "1"}]
    probe = {"program": "x.c", "line": 5, "capture": ["n"]}
    base = {"probe": probe, "status": "completed", "rows": rows}
    evidence.record(store, {**base, "output": "A\n"})
    candidate = {"sha256": "e", "diff": "--- x.c\n", "line": 7, "original_line": 5}
    moved = [{**rows[0], "line": 7, "value": "2"}]
    evidence.record(store, {**base, "rows": moved, "output": "B\n", "candidate": candidate})
    runs = evidence.run_query(store, "runs", {})["rows"]
    assert [row["at"] for row in runs] == ["x.c:5 (candidate)", "x.c:5"]
    values = evidence.run_query(store, "values", {"selector": "n"})["rows"]
    assert [(row["line"], row["value"]) for row in values] == [(5, "1")]
    assert [
        row["candidate"] for row in evidence.query(store, "SELECT * FROM last_run")["rows"]
    ] == [1]
    diff = evidence.run_query(store, "diff", {})["rows"]
    edit = next(row for row in diff if row["selector"] == "(edit)")
    assert (edit["value_a"], edit["value_b"]) == ("none", "candidate")
    # Captures pair by hit and selector, so a candidate whose lines moved still compares.
    assert any(
        row["selector"] == "n" and row["value_b"] == "2" and row["note"] is None for row in diff
    )


def test_a_candidate_run_records_the_source_that_ran_and_its_edit(tmp_path):
    store = tmp_path / "evidence.db"
    result = {
        "probe": {"program": "x.c", "line": 4},
        "status": "completed",
        "source_sha256": "original",
        "candidate": {"sha256": "edited", "diff": "--- x.c\n+++ x.c\n", "line": 5},
    }
    evidence.record(store, result)
    evidence.record(store, {**result, "candidate": None})
    rows = evidence.query(store, "SELECT source_sha256, edit FROM runs ORDER BY run_id")["rows"]
    assert rows == [
        {"source_sha256": "edited", "edit": "--- x.c\n+++ x.c\n"},
        {"source_sha256": "original", "edit": None},
    ]


def test_python_candidates_run_an_edited_copy(tmp_path, capsys, monkeypatch):
    # Eval 5 (Python): `probe --old/--new` was refused, so a fix could not be seen
    # before `verify`.
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "helper.py").write_text("def double(x):\n    return x * 2\n")
    (tmp_path / "rates.json").write_text('{"rate": 5}')
    script = tmp_path / "app.py"
    script.write_text(
        "import json, sys\nfrom pathlib import Path\nimport helper\n"
        "here = Path(__file__).parent / 'rates.json'\n"
        "rate = json.loads(here.read_text())['rate'] if here.exists() else 1\n"
        "acc = 0\nfor x in (1, 2):\n    acc += helper.double(x) * rate\n"
        "print('ACC:', acc, Path(sys.argv[0]).name, __name__)\n"
    )
    original = script.read_text()
    probe = ["probe", str(script), "--at", "8", "--capture", "acc,rate", "--json", "--quiet"]
    code = main([*probe, "--old", "acc = 0", "--new", "acc = 100"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert [row["value"] for row in result["rows"]] == ["100", "5"]
    # Review of #109: it runs as the original script would (`__file__`, `argv[0]`, its
    # directory's modules and data files), so only the edit differs.
    assert "ACC: 130 app.py __main__" in result["output"] and script.read_text() == original
    assert "+acc = 100" in result["candidate"]["diff"]
    assert not list(Path(tempfile.gettempdir()).glob("peeky-candidate-*/edited/app.py"))
    # An entry script would import the original module, so --run is refused.
    (tmp_path / "run.py").write_text("import app\n")
    code = main([*probe, "--run", str(tmp_path / "run.py"), "--old", "acc = 0", "--new", "acc = 1"])
    error = json.loads(capsys.readouterr().out)["error"]
    assert "with --run, the entry script would import the original module" in error["message"]


def test_an_edit_needs_both_halves(tmp_path, capsys):
    from peeky.cli import main

    (tmp_path / "app.c").write_text("int main(void) { return 0; }\n")
    code = main(["probe", str(tmp_path / "app.c"), "--at", "1", "--old", "return 0", "--json"])
    assert code == 2 and "--new" in json.loads(capsys.readouterr().out)["error"]["message"]


def bound(sha256, hits=1):
    from peeky.cli import _bind_candidate

    result = {
        "probe": {"program": "x.c", "line": 1, "edit": ["a", "b"]},
        "status": "completed",
        "hits": hits,
        "rows": [{"value": "1"}] if hits else [],
        "output": "",
        "candidate": {"line": 1, "original_line": 1, **({"sha256": sha256} if sha256 else {})},
    }
    _bind_candidate(result, "a;\n", ("a", "b"), "x.c")
    return result


def test_values_from_any_other_build_than_the_candidate_are_discarded():
    expected = hashlib.sha256(b"b;\n").hexdigest()
    kept = bound(expected)
    assert kept["status"] == "completed" and kept["rows"] and "edit" not in kept["probe"]
    assert "+b;" in kept["candidate"]["diff"]
    for reported in ("0" * 64, None):
        dropped = bound(reported)
        assert dropped["status"] == "failed" and dropped["rows"] == []
        assert dropped["error"]["code"] == "PROVENANCE_CHANGED" and dropped["results_discarded"]
    # A probe that failed before anything ran keeps its own error.
    assert bound(None, hits=0)["status"] == "completed"


def test_the_worker_refuses_edits_it_cannot_build(tmp_path):
    from peeky.adapters import PythonAdapter
    from peeky.worker import DebugWorker

    class Attached(PythonAdapter):
        supports_edits = False

    (tmp_path / "app.py").write_text("x = 1\n")
    worker = DebugWorker(tmp_path, adapter=Attached())
    with pytest.raises(ValueError, match="needs a fresh launch"):
        worker.dispatch("launch", {"program": "app.py", "edit": {"old": "x", "new": "y"}})
    assert worker.dap is None


def test_the_http_probe_passes_edits_as_files(tmp_path, monkeypatch):
    from peeky import worker as worker_module
    from peeky.probe import Probe as Request

    seen = {}

    class Process:
        pid = 0

        def __init__(self, command, **kwargs):
            flags = {command[i]: command[i + 1] for i in range(len(command) - 1)}
            seen["old"] = open(flags["--old-file"], "rb").read()
            seen["new"] = open(flags["--new-file"], "rb").read()
            seen["command"] = command

        def communicate(self, timeout=None):
            return b'{"status": "completed"}', b""

    monkeypatch.setattr(subprocess, "Popen", Process)
    request = Request("x.c", 3, edit=("a\r\nb", "c\n"))
    assert worker_module._run_probe_process(tmp_path, request, None, 60) == {"status": "completed"}
    assert seen["old"] == b"a\r\nb" and seen["new"] == b"c\n"
    assert "a\r\nb" not in " ".join(seen["command"])


@pytest.mark.parametrize("edit", [{"old": "a"}, {"old": "a", "new": 2}, ["a", "b"], "a"])
def test_the_http_probe_refuses_malformed_edits(tmp_path, edit):
    import threading

    from peeky.client import Client
    from peeky.worker import make_server

    server = make_server(tmp_path, "unit-test-secret-token-1234", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", "unit-test-secret-token-1234")
        with pytest.raises(Exception, match="edit"):
            client.call("probe", at="x.c:1", edit=edit)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_the_cli_refuses_a_line_the_candidate_joins_onto_the_edit(tmp_path, capsys):
    from peeky.cli import main

    source = tmp_path / "page.c"
    source.write_text("int main(void) {\n    int start = 6;\n    int end = start + 3;\n}\n")
    code = main(
        [
            "probe",
            str(source),
            "--at",
            "3",
            "--old",
            "    int start = 6;\n",
            "--new",
            "    int start = 3;",
            "--json",
        ]
    )
    error = json.loads(capsys.readouterr().out)["error"]
    assert (
        code == 2
        and error["code"] == "INVALID_REQUEST"
        and "joins it onto line 2" in error["message"]
    )


LONG = "".join(f"l{n};\n" for n in range(1, 11))  # l1; .. l10;


def test_several_edits_combine_into_one_with_the_same_effect():
    # #76: a field added at the top and a function changed further down.
    edit = (("l2;\n", "l2;\nfield;\n"), ("l8;", "L8;"))
    old, new = combine_edits(LONG, edit)
    assert LONG.count(old) == 1 and old.startswith("l2;") and old.endswith("l8;")
    assert LONG.replace(old, new, 1) == LONG.replace("l2;\n", "l2;\nfield;\n").replace("l8;", "L8;")
    # Pairs may come in any order; each quotes the original file.
    assert combine_edits(LONG, edit[::-1]) == (old, new)
    assert combine_edits(LONG, ("l2;", "x;")) == ("l2;", "x;")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (1, 1),
        (2, 2),  # inside the first edit, which keeps l2 on its line... and adds one after
        (5, 6),  # between the edits: shifted by the first one's added line
        (8, 9),  # inside the second edit, which keeps the line count
        (10, 11),
    ],
)
def test_lines_map_through_every_edit(line, expected):
    edit = (("l2;\n", "l2;\nfield;\n"), ("l8;", "L8;"))
    if line == 2:
        with pytest.raises(ValueError, match="inside the edit 1"):
            candidate_line(LONG, edit, line)
        return
    assert candidate_line(LONG, edit, line) == expected


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ((("l2;", "x"), ("l2;\nl3", "y")), "Edits 1 and 2 overlap"),
        ((("l2;", "x"), ("zz", "y")), "exactly once \\(edit 2\\)"),
        ((("l2;", "x"), ("l3;", "l3;")), "must change it \\(edit 2\\)"),
    ],
)
def test_several_edits_are_refused_when_they_overlap_or_miss(edit, message):
    with pytest.raises(ValueError, match=message):
        combine_edits(LONG, edit)


def test_a_probe_takes_one_edit_or_several_pairs():
    assert Probe("x.c", 1, edit=("a", "b")).edit == ("a", "b")
    assert Probe("x.c", 1, edit=(("a", "b"), ("c", "d"))).edit == (("a", "b"), ("c", "d"))
    for bad in ((), (("a", "b"), ("c",)), (("a", "a"),), tuple(("a", "b") for _ in range(33))):
        with pytest.raises(ValueError, match="candidate edit"):
            Probe("x.c", 1, edit=bad)
