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
    # before `verify`. Reviews of #109: the candidate must run the edit wherever the
    # program reaches its own code, and find the user's files beside it.
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    package = tmp_path / "sub"
    package.mkdir()
    (package / "helper.py").write_text(
        "from app import RATE\n\ndef double(x):\n    return x * 2 * RATE\n"
    )
    (package / "rates.json").write_text('{"bonus": 5}')
    script = package / "app.py"
    script.write_text(
        "import json, subprocess, sys\nfrom pathlib import Path\nRATE = 1\n"
        "if __name__ == '__main__':\n"
        "    import helper\n"
        "    bonus = json.loads((Path(__file__).parent / 'rates.json').read_text())['bonus']\n"
        "    if sys.argv[1:] == ['child']:\n"
        "        print(helper.double(1))\n"
        "        sys.exit(0)\n"
        "    child = subprocess.run(f'{sys.executable} {__file__} child', shell=True,\n"
        "                           capture_output=True, text=True).stdout.split()[0]\n"
        "    acc = helper.double(1) + bonus\n"
        "    print('ACC:', acc, child, Path(sys.argv[0]).name)\n"
    )
    original = script.read_text()
    probe = [
        "probe",
        str(tmp_path),
        "--at",
        "sub/app.py:13",
        "--capture",
        "acc",
        "--json",
        "--quiet",
    ]
    code = main([*probe, "--old", "RATE = 1", "--new", "RATE = 10"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    # A nested --at file; an import of the script by name, a data file beside it and a
    # shell re-run of `__file__` all see the edit (RATE 10 everywhere).
    assert [row["value"] for row in result["rows"]] == ["25"]
    assert "ACC: 25 20 app.py" in result["output"] and script.read_text() == original
    assert "+RATE = 10" in result["candidate"]["diff"]
    assert not list(Path(tempfile.gettempdir()).glob(f"peeky-candidate-*/{tmp_path.name}"))
    # An entry script would import the original module, so --run is refused.
    (tmp_path / "run.py").write_text("import sub.app\n")
    code = main(
        [*probe, "--run", str(tmp_path / "run.py"), "--old", "RATE = 1", "--new", "RATE = 2"]
    )
    error = json.loads(capsys.readouterr().out)["error"]
    assert "with --run, the entry script would import the original module" in error["message"]


def test_a_python_candidates_copy_keeps_links_specials_and_the_users_bytecode(
    tmp_path, capsys, monkeypatch
):
    # Review of #109: the linked __pycache__ let the edit's bytecode reach the user's
    # tree; links were recreated with their old text (pointing elsewhere); a FIFO in the
    # root failed every candidate; `--at APP.py` broke imports of `app`.
    import os

    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "conf.json").write_text('{"rate": 5}')
    proj = tmp_path / "proj"
    (proj / "lib").mkdir(parents=True)
    (proj / "conf.json").symlink_to("../shared/conf.json")
    (proj / "lib" / "__init__.py").write_text("")
    (proj / "lib" / "const.py").write_text("RATE = 1\n")
    (proj / "libabs").symlink_to(proj / "lib")
    os.mkfifo(proj / "pipe")
    (proj / "helper.py").write_text(
        "from libabs import const\nfrom app import RATE\ndef both():\n    return (const.RATE, RATE)\n"
    )
    script = proj / "app.py"
    script.write_text(
        "import json\nfrom pathlib import Path\nRATE = 1\nif __name__ == '__main__':\n"
        "    import helper\n"
        "    rate = json.loads((Path(__file__).parent / 'conf.json').read_text())['rate']\n"
        "    pair = helper.both()\n    print('OUT', rate, pair)\n"
    )
    cache = proj / "__pycache__"
    cache.mkdir()
    (cache / "marker").write_text("user's")
    typed = "APP.py" if (proj / "APP.py").exists() else "app.py"
    probe = [
        "probe",
        str(proj),
        "--at",
        f"{typed}:8",
        "--capture",
        "rate,pair",
        "--json",
        "--quiet",
    ]
    code = main([*probe, "--old", "RATE = 1", "--new", "RATE = 7"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert [row["value"] for row in result["rows"]] == ["5", "(1, 7)"]
    assert sorted(path.name for path in cache.iterdir()) == ["marker"]
    assert "RATE = 1" in script.read_text()


def test_a_python_candidate_edits_the_named_file_and_its_hard_links(tmp_path, capsys, monkeypatch):
    # Review of #109: with a hard link below the root (`backup/app.py`), the walk's last
    # name got the edit and ran with the wrong neighbours; a write-only file was linked,
    # so the candidate wrote the user's file.
    import os

    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "backup").mkdir()
    script = tmp_path / "app.py"
    script.write_text(
        "RATE = 1\nimport alias\nif __name__ == '__main__':\n    import helper\n"
        "    open('audit.log', 'a').write('candidate')\n"
        "    a = helper.value()\n    print('OUT', a, RATE, alias.RATE)\n"
    )
    os.link(script, tmp_path / "alias.py")
    os.link(script, tmp_path / "backup" / "app.py")
    (tmp_path / "helper.py").write_text("def value():\n    return 10\n")
    (tmp_path / "backup" / "helper.py").write_text("def value():\n    return 99\n")
    audit = tmp_path / "audit.log"
    audit.write_text("x")
    audit.chmod(0o200)
    probe = ["probe", str(tmp_path), "--at", "app.py:7", "--capture", "a", "--json", "--quiet"]
    code = main([*probe, "--old", "RATE = 1", "--new", "RATE = 7"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert result["rows"][0]["value"] == "10" and "OUT 10 7 7" in result["output"]
    audit.chmod(0o600)
    assert audit.read_text() == "x" and "RATE = 1" in script.read_text()


def test_a_python_candidate_refuses_names_that_differ_only_by_case(tmp_path, monkeypatch):
    # Review of #109: on a case-sensitive volume `Helper.py` and `helper.py` collapse
    # into one name in a case-insensitive temporary directory.
    import os

    from peeky.adapters import PythonAdapter, PythonCandidate
    from peeky.build import SourceEdit

    (tmp_path / "app.py").write_text("x = 1\n")
    real_walk = os.walk

    def walk(top, *args, **kwargs):
        for here, directories, names in real_walk(top, *args, **kwargs):
            yield here, directories, [*names, "Helper.py", "helper.py"]

    monkeypatch.setattr(os, "walk", walk)
    with pytest.raises(ValueError, match="differs only by case or Unicode form"):
        PythonCandidate(
            PythonAdapter(), tmp_path / "app.py", tmp_path, SourceEdit("app.py", "x = 1", "x = 2")
        )


def test_a_python_candidate_warns_when_the_script_reaches_above_its_root(
    tmp_path, capsys, monkeypatch
):
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "conf.json").write_text('{"rate": 5}')
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "run.py"
    script.write_text(
        "import json\nfrom pathlib import Path\n"
        "conf = Path(__file__).parents[1] / 'conf.json'\n"
        "rate = json.loads(conf.read_text())['rate'] if conf.exists() else 1\n"
        "acc = 0\nprint(acc * rate)\n"
    )
    probe = ["probe", str(script), "--at", "6", "--capture", "rate", "--json", "--quiet"]
    main([*probe, "--old", "acc = 0", "--new", "acc = 1"])
    result = json.loads(capsys.readouterr().out)
    assert any("nothing above it was copied" in warning for warning in result["warnings"])
    # Verification of #109: idioms no reading of the source catches still get the note.
    for locate in (
        "conf = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / 'conf.json'",
        "HERE = Path(__file__).resolve().parent\nconf = HERE.parent / 'conf.json'",
    ):
        script.write_text(
            f"import json, os\nfrom pathlib import Path\n{locate}\n"
            "rate = json.loads(conf.read_text())['rate'] if conf.exists() else 1\n"
            "acc = 0\nprint(acc * rate)\n"
        )
        line = str(script.read_text().splitlines().index("acc = 0") + 2)
        main([*probe[:3], line, *probe[4:], "--old", "acc = 0", "--new", "acc = 1"])
        result = json.loads(capsys.readouterr().out)
        assert any("nothing above it was copied" in w for w in result["warnings"]), locate
    # With the project as the target, the copy holds them.
    main(
        [
            "probe",
            str(tmp_path),
            "--at",
            "scripts/run.py:6",
            "--capture",
            "rate",
            "--json",
            "--quiet",
            "--old",
            "acc = 0",
            "--new",
            "acc = 1",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert result["rows"][0]["value"] == "5" and not result.get("warnings")


def test_a_python_candidate_refuses_a_directory_it_cannot_list(tmp_path, capsys, monkeypatch):
    # Verification of #109: `os.walk` skipped a directory it could not list (`--x`), so
    # the candidate ran without it and its errors read as the edit's effect.
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    (proj / "conf").mkdir(parents=True)
    (proj / "conf" / "rates.json").write_text('{"rate": 5}')
    (proj / "app.py").write_text(
        "import json\nrate = json.load(open('conf/rates.json'))['rate']\nacc = 0\nprint(acc)\n"
    )
    (proj / "conf").chmod(0o100)
    try:
        probe = ["probe", str(proj), "--at", "app.py:4", "--capture", "rate", "--json"]
        code = main([*probe, "--quiet", "--old", "acc = 0", "--new", "acc = 1"])
        result = json.loads(capsys.readouterr().out)
    finally:
        (proj / "conf").chmod(0o700)
    assert code != 0 and "could not copy the target root" in result["error"]["message"], result


def test_a_hard_linked_name_of_the_edited_file_keeps_its_own_directory(
    tmp_path, capsys, monkeypatch
):
    # Verification of #109: a hard-linked twin became a symlink to the edited copy, so
    # `python backup/app.py` resolved to the --at file's directory and imported the root's
    # `helper.py` instead of its own.
    import os

    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "backup").mkdir()
    script = tmp_path / "app.py"
    script.write_text(
        "import subprocess, sys\nimport helper\nRATE = 1\n"
        "if sys.argv[1:] == ['child']:\n    print('CHILD', helper.value(), RATE)\n"
        "else:\n"
        "    out = subprocess.run([sys.executable, 'backup/app.py', 'child'],"
        " capture_output=True, text=True).stdout.strip()\n"
        "    print('OUT', out)\n"
    )
    os.link(script, tmp_path / "backup" / "app.py")
    (tmp_path / "helper.py").write_text("def value():\n    return 10\n")
    (tmp_path / "backup" / "helper.py").write_text("def value():\n    return 99\n")
    probe = ["probe", str(tmp_path), "--at", "app.py:8", "--capture", "out", "--json", "--quiet"]
    code = main([*probe, "--old", "RATE = 1", "--new", "RATE = 7"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert result["rows"][0]["value"] == "'CHILD 99 7'", result["rows"]
    assert "RATE = 1" in script.read_text()


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
