"""Candidate probes (`probe --old/--new`): line mapping, validation, errors and evidence."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import peeky.probe as probe_module
from peeky import cli, evidence
from peeky.build import BuildCommandError
from peeky.errors import classify
from peeky.probe import Probe, candidate_line, combine_edits, keeps_lines

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
            statement = statement.replace(" c.note,", "")
        # Columns later versions added (8: captures.note; 9: runs.notes_kept; 16:
        # runs.set_outcomes).
        statement = statement.replace(", set_outcomes TEXT)", ")")
        statement = statement.replace(", note TEXT)", ")").replace(", notes_kept INTEGER)", ")")
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
    assert any("nothing above was copied" in warning for warning in result["warnings"])
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
        assert any("nothing above was copied" in w for w in result["warnings"]), locate
    # Verification of #109: a nested --at file in a root that is not the project's (a
    # `src/` layout) reaches above it too.
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "app.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "conf = Path(__file__).resolve().parents[2] / 'conf.json'\n"
        "rate = json.loads(conf.read_text())['rate'] if conf.exists() else 1\n"
        "acc = 0\nprint(acc * rate)\n"
    )
    nested = ["probe", str(tmp_path / "src"), "--at", "pkg/app.py:6", "--capture", "rate"]
    main([*nested, "--json", "--quiet", "--old", "acc = 0", "--new", "acc = 1"])
    result = json.loads(capsys.readouterr().out)
    assert any("nothing above was copied" in w for w in result["warnings"]), result
    # With the project as the target, the copy holds them, and a project root gets no note.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'p'\n")
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


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists any directory")
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


@pytest.mark.skipif(os.geteuid() == 0, reason="root enters any directory")
def test_a_python_candidate_stands_in_for_a_directory_nobody_can_open(
    tmp_path, capsys, monkeypatch
):
    # Verification of #109: refusing every unlistable directory refused candidates beside
    # another user's `pgdata` or `lost+found`, which the program cannot open either.
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    (proj / "pgdata").mkdir(parents=True)
    (proj / "app.py").write_text(
        "import os\ntry:\n    os.listdir('pgdata')\n    seen = 'listed'\n"
        "except PermissionError:\n    seen = 'denied'\nacc = 1\nprint(acc, seen)\n"
    )
    (proj / "pgdata").chmod(0o000)
    try:
        probe = ["probe", str(proj), "--at", "app.py:8", "--capture", "acc,seen", "--json"]
        code = main([*probe, "--quiet", "--old", "acc = 1", "--new", "acc = 2"])
        result = json.loads(capsys.readouterr().out)
    finally:
        (proj / "pgdata").chmod(0o700)
    assert code == 0, result.get("error")
    assert [row["value"] for row in result["rows"]] == ["2", "'denied'"], result["rows"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes any directory")
def test_a_python_candidate_keeps_times_and_directory_modes(tmp_path, capsys, monkeypatch):
    # Verification of #109: every copied file had the copy's mtime, in walk order, and
    # every directory was writable, so a freshness check or a read-only output directory
    # took another branch in the candidate.
    import time

    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    proj = tmp_path / "proj"
    (proj / "job").mkdir(parents=True)
    (proj / "out").mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = 'p'\n")
    for name, age in (("cache.json", 200), ("data.csv", 100)):
        (proj / "job" / name).write_text(name)
        os.utime(proj / "job" / name, (time.time() - age, time.time() - age))
    (proj / "app.py").write_text(
        "from pathlib import Path\njob = Path('job')\n"
        "fresh = (job / 'data.csv').stat().st_mtime > (job / 'cache.json').stat().st_mtime + 50\n"
        "try:\n    Path('out/x').write_text('x')\n    saved = 'saved'\n"
        "except PermissionError:\n    saved = 'read-only'\nscale = 1\nprint(fresh, saved, scale)\n"
    )
    (proj / "out").chmod(0o555)
    try:
        probe = ["probe", str(proj), "--at", "app.py:10", "--capture", "fresh,saved,scale"]
        code = main([*probe, "--json", "--quiet", "--old", "scale = 1", "--new", "scale = 2"])
        result = json.loads(capsys.readouterr().out)
    finally:
        (proj / "out").chmod(0o755)
    assert code == 0, result.get("error")
    assert [row["value"] for row in result["rows"]] == ["True", "'read-only'", "2"], result
    assert not result.get("warnings"), result["warnings"]
    assert not list(scratch.glob("peeky-candidate-*")), "a read-only directory blocked cleanup"


GIT_ENVIRONMENT = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git_in(where, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=where,
        env=GIT_ENVIRONMENT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def snapshot(top):
    """Every file and directory under ``top``, links followed: identity, mtime, bytes."""
    found = {}
    for here, directories, names in os.walk(top, followlinks=True):
        for name in [*directories, *names]:
            path = Path(here) / name
            status = path.stat()
            data = path.read_bytes() if path.is_file() else b""
            found[str(path.relative_to(top))] = (
                status.st_ino,
                status.st_mtime_ns,
                status.st_mode,
                hashlib.sha256(data).hexdigest(),
            )
    return found


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_python_candidate_never_writes_the_user_s_repository(tmp_path, capsys, monkeypatch):
    # Verification of #109: `.git` and `.pytest_cache` were links to the user's, so a
    # candidate's `git add` staged the edit in the user's repository, `git describe
    # --dirty` rewrote the user's index and pytest wrote the user's cache. Then objects
    # read through `alternates` had their mtimes touched by the copy's commits, a remote
    # branch named `objects/…` was taken for an object store, and a linked `venv` was
    # untracked though `venv/` is ignored.
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    proj = tmp_path / "proj"
    (proj / ".pytest_cache").mkdir(parents=True)
    (proj / ".pytest_cache" / "marker").write_text("user's")
    (proj / "venv").mkdir()
    (proj / "venv" / "pyvenv.cfg").write_text("home = /usr\n")
    (proj / ".gitignore").write_text("venv/\n.pytest_cache/\n")
    (proj / "app.py").write_text(
        "import subprocess\nfrom pathlib import Path\nacc = 1\n"
        "def git(*args):\n"
        "    return subprocess.run(['git', '-c', 'user.name=c', '-c', 'user.email=c@c', *args],"
        " capture_output=True, text=True).stdout.strip()\n"
        "changed = git('diff-files', '--name-only')\nmarker = git('config', 'work.marker')\n"
        "status = git('status', '--porcelain')\n"
        "remotes = git('for-each-ref', '--format=%(refname)', 'refs/remotes')\n"
        "git('add', '-A')\nstaged = git('diff', '--cached', '--name-only')\n"
        "described = git('describe', '--always', '--dirty')\n"
        "git('commit', '-qm', 'candidate')\ngit('commit', '-q', '--allow-empty', '-m', 'again')\n"
        "Path('.pytest_cache/marker').write_text('candidate')\n"
        "print(acc, changed, marker, status, remotes, staged, described)\n"
    )
    git_in(proj, "init", "-q")
    git_in(proj, "add", "app.py", ".gitignore")
    git_in(proj, "commit", "-qm", "base")
    git_in(proj, "update-ref", "refs/remotes/origin/objects/y", "HEAD")
    git_in(proj, "update-ref", "refs/remotes/origin/lfs/z", "HEAD")
    git_in(proj, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/objects/y")
    # Configuration the user's repository gets from a conditional include on its path.
    (tmp_path / "work.gitconfig").write_text("[work]\n\tmarker = yes\n")
    (tmp_path / "global.gitconfig").write_text(
        f'[includeIf "gitdir:{proj.resolve()}/"]\n\tpath = {tmp_path / "work.gitconfig"}\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global.gitconfig"))
    before = snapshot(proj / ".git")
    probe = ["probe", str(proj), "--at", "app.py:16"]
    capture = ["--capture", "changed,marker,status,remotes,staged,described", "--json", "--quiet"]
    code = main([*probe, *capture, "--old", "acc = 1", "--new", "acc = 2"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    changed, marker, status, remotes, staged, described = (row["value"] for row in result["rows"])
    # Verification of #109: the copy's index had the user's files' identities, so `git
    # diff-files` called every file changed. An `includeIf "gitdir:"` cannot match the
    # copy's path: the note says the configuration differs.
    assert changed == "'app.py'" and marker == "''", result["rows"]
    assert status == "'M app.py'", status
    assert "origin/objects/y" in remotes and "origin/lfs/z" in remotes, remotes
    assert "app.py" in staged and described.endswith("-dirty'"), result["rows"]
    assert len(result["warnings"]) == 1 and "includeIf" in result["warnings"][0], result

    # Without the conditional include, nothing differs and nothing is noted.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    code = main([*probe, *capture, "--old", "acc = 1", "--new", "acc = 3"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and not result.get("warnings"), result
    assert snapshot(proj / ".git") == before
    assert git_in(proj, "status", "--porcelain") == ""
    assert (proj / ".pytest_cache" / "marker").read_text() == "user's"
    assert "acc = 1" in (proj / "app.py").read_text()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_python_candidate_keeps_other_git_layouts_private(tmp_path, capsys, monkeypatch):
    # Verification of #109: an absolute `core.worktree` made the copy's `git stash` revert
    # the user's uncommitted work; a `git-new-workdir` layout (links into another
    # repository's `.git`) and a `.git` linked to a worktree's `.git` file committed into
    # the user's repository; a package inside a repository had no repository and no note.
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    program = (
        "import subprocess\nacc = 1\n"
        "def git(*args):\n"
        "    return subprocess.run(['git', '-c', 'user.name=c', '-c', 'user.email=c@c', *args],"
        " capture_output=True, text=True).stdout.strip()\n"
        "git('stash', '-q')\ngit('commit', '-q', '--allow-empty', '-m', 'candidate')\n"
        "head = git('rev-parse', '--short', 'HEAD')\nprint(acc, head)\n"
    )

    def repository(path):
        path.mkdir(parents=True)
        (path / "app.py").write_text(program)
        (path / "data.txt").write_text("v1\n")
        git_in(path, "init", "-q", "-b", "main")
        git_in(path, "add", "-A")
        git_in(path, "commit", "-qm", "base")
        return path

    def candidate(target, at="app.py", line=8):
        capture = "listed" if at == "repair.py" else "head"
        code = main(
            [
                "probe",
                str(target),
                "--at",
                f"{at}:{line}",
                "--capture",
                capture,
                "--json",
                "--quiet",
            ]
            + ["--old", "acc = 1", "--new", "acc = 2"]
        )
        result = json.loads(capsys.readouterr().out)
        assert code == 0, result.get("error")
        return result

    # An absolute `core.worktree` naming the root moves to the copy: as git writes it
    # (quoted when the path holds ` #`), on the section's line, or quoted before a comment.
    for name, config in (
        ("worktree", None),
        ("proj #2", None),
        ("one-line", "[core] worktree = {}\n"),
        ("commented", '[core]\n\tworktree = "{}"\t# the checkout\n'),
    ):
        worktree = repository(tmp_path / name)
        if config is None:
            git_in(worktree, "config", "core.worktree", str(worktree))
        else:
            with open(worktree / ".git" / "config", "a") as stream:
                stream.write(config.format(worktree))
        (worktree / "data.txt").write_text("v1\nuser's work\n")
        candidate(worktree)
        assert (worktree / "data.txt").read_text() == "v1\nuser's work\n", name
        assert git_in(worktree, "stash", "list") == "", name
    # `git-new-workdir`: the root's `.git` links into another repository's.
    main_repository = repository(tmp_path / "main")
    workdir = tmp_path / "workdir"
    (workdir / ".git").mkdir(parents=True)
    for name in ("config", "refs", "logs", "objects", "info", "hooks", "packed-refs"):
        if (main_repository / ".git" / name).exists():
            (workdir / ".git" / name).symlink_to(main_repository / ".git" / name)
    shutil.copy(main_repository / ".git" / "HEAD", workdir / ".git" / "HEAD")
    git_in(workdir, "checkout", "-qf")
    before = snapshot(main_repository / ".git")
    candidate(workdir)
    assert snapshot(main_repository / ".git") == before
    # A `.git` linked to a worktree's `.git` file outside the root: left out, with a note.
    git_in(main_repository, "worktree", "add", "-q", str(tmp_path / "linked-worktree"))
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "app.py").write_text(program)
    (linked / "pyproject.toml").write_text("")
    (linked / ".git").symlink_to(tmp_path / "linked-worktree" / ".git")
    before = snapshot(main_repository / ".git")
    result = candidate(linked)
    assert snapshot(main_repository / ".git") == before
    assert any("names git metadata outside the root" in w for w in result["warnings"])
    # `git worktree repair` in the copy rewrites the `.git` files of the worktrees the
    # copy's repository lists: never the user's, inside or outside the root.
    git_in(main_repository, "worktree", "add", "-q", str(main_repository / "inner"))
    (main_repository / "repair.py").write_text(
        "import os, subprocess\nacc = 1\nhere = os.path.dirname(os.path.abspath(__file__))\n"
        "subprocess.run(['git', 'worktree', 'repair'], capture_output=True, cwd=here)\n"
        "listed = subprocess.run(['git', 'worktree', 'list'], capture_output=True, text=True,"
        " cwd=here).stdout\nprint(acc, listed)\n"
    )
    pointers = {
        path: path.read_text()
        for path in (tmp_path / "linked-worktree" / ".git", main_repository / "inner" / ".git")
    }
    candidate(main_repository, at="repair.py", line=6)
    assert {path: path.read_text() for path in pointers} == pointers
    assert git_in(tmp_path / "linked-worktree", "status", "--porcelain") == ""
    # A bare repository beside its worktrees (`.git` says `gitdir: ./.bare`): its
    # `worktrees/*/gitdir` named the user's worktrees too.
    bare = tmp_path / "bare-layout"
    bare.mkdir()
    git_in(main_repository, "clone", "-q", "--bare", str(main_repository), str(bare / ".bare"))
    (bare / ".git").write_text("gitdir: ./.bare\n")
    (bare / "pyproject.toml").write_text("")
    git_in(bare, "worktree", "add", "-q", str(bare / "main"), "main")
    (bare / "main" / "repair.py").write_text((main_repository / "repair.py").read_text())
    before = (bare / "main" / ".git").read_text()
    candidate(bare, at="main/repair.py", line=6)
    assert (bare / "main" / ".git").read_text() == before
    assert git_in(bare / "main", "status", "--porcelain") == "?? repair.py\n"
    # Verification of #109: worktrees naming a shared git directory with no `.git` file
    # at the root (`.bare` beside them), and a bare repository as the root itself, with a
    # worktree outside the root each time.
    for layout in ("shared", "root"):
        top = tmp_path / f"{layout}-layout"
        gitdir = top / ".bare" if layout == "shared" else top
        git_in(tmp_path, "clone", "-q", "--bare", str(main_repository), str(gitdir))
        (top / "pyproject.toml").write_text("")
        bare = ("--git-dir", str(gitdir))  # `safe.bareRepository=explicit` may be set
        git_in(tmp_path, *bare, "worktree", "add", "-q", str(top / "inside"), "main")
        outside = tmp_path / f"{layout}-outside"
        git_in(
            tmp_path, *bare, "worktree", "add", "-q", "-b", f"{layout}-side", str(outside), "main"
        )
        (top / "inside" / "repair.py").write_text((main_repository / "repair.py").read_text())
        pointers = {path: path.read_text() for path in (top / "inside" / ".git", outside / ".git")}
        candidate(top, at="inside/repair.py", line=6)
        assert {path: path.read_text() for path in pointers} == pointers, layout
        assert git_in(outside, "status", "--porcelain") == "", layout
    # A package inside a repository: the copy has no repository, which the note says.
    package = main_repository / "pkg"
    package.mkdir()
    (package / "pyproject.toml").write_text("")
    (package / "app.py").write_text(program)
    result = candidate(package)
    assert any(f"inside the repository at {main_repository}" in w for w in result["warnings"])


def test_a_python_candidate_imports_the_copy_through_an_import_path(tmp_path, capsys, monkeypatch):
    # Verification of #109: with the project's `src` on the import path (an editable
    # install's `.pth`, or `PYTHONPATH`), `import shop` loaded the user's unedited module
    # (and wrote its bytecode into the user's tree).
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    shop = proj / "src" / "shop"
    shop.mkdir(parents=True)
    (proj / "pyproject.toml").write_text("[project]\nname = 'shop'\n")
    (shop / "__init__.py").write_text("")
    (shop / "report.py").write_text(
        "from shop import core\ndef render(x):\n    return core.price(x)\n"
    )
    (shop / "core.py").write_text(
        "TAX = 0.2\ndef price(x):\n    return x * (1 + TAX)\n"
        "if __name__ == '__main__':\n    from shop import report\n"
        "    total = report.render(100)\n    print(total)\n"
    )
    given = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(proj / "src"), given])))
    probe = ["probe", str(proj), "--at", "src/shop/core.py:7", "--capture", "total"]
    code = main([*probe, "--json", "--quiet", "--old", "TAX = 0.2", "--new", "TAX = 0.5"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert result["rows"][0]["value"] == "150.0", result["rows"]
    assert not (shop / "__pycache__").exists()
    # Verification of #109: under `PYTHONSAFEPATH`, the import check replaced the first
    # `PYTHONPATH` entry (the copy's `src`) with the script's directory: a false note.
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    code = main([*probe, "--json", "--quiet", "--old", "TAX = 0.2", "--new", "TAX = 0.6"])
    result = json.loads(capsys.readouterr().out)
    assert result["rows"][0]["value"] == "160.0", result["rows"]
    assert not any("`import shop`" in w for w in result.get("warnings", [])), result
    monkeypatch.delenv("PYTHONSAFEPATH")
    # Verification of #109: a root typed in another case on a volume that ignores case
    # did not match the import path's spelling, so nothing was moved and the user's
    # module ran (writing its bytecode into the user's tree).
    typed = Path(str(proj).swapcase())
    if typed.exists():
        probe[1] = str(typed)
        code = main([*probe, "--json", "--quiet", "--old", "TAX = 0.2", "--new", "TAX = 0.7"])
        result = json.loads(capsys.readouterr().out)
        assert code == 0, result.get("error")
        assert result["rows"][0]["value"] == "170.0", result["rows"]
        assert not (shop / "__pycache__").exists()


def test_a_python_candidate_redirects_an_editable_finder(tmp_path, monkeypatch):
    # An editable install's finder maps a package name to its directory in the user's
    # tree (setuptools); the copy's parent of that directory comes first on the path.
    from peeky.adapters import PythonAdapter, PythonCandidate
    from peeky.build import SourceEdit

    proj = tmp_path / "proj"
    (proj / "pkg").mkdir(parents=True)
    (proj / "pkg" / "__init__.py").write_text("")
    (proj / "pyproject.toml").write_text("")
    (proj / "tools").mkdir()
    (proj / "tools" / "run.py").write_text("x = 1\n")
    (proj / "lib").mkdir()
    (proj / ".venv" / "lib" / "site-packages").mkdir(parents=True)
    (proj / "shadow").mkdir()
    (proj / "shadow" / "json.py").write_text("")
    interpreter = tmp_path / "python"

    def candidate(path, mapped, pythonpath=None):
        reply = json.dumps({"path": path, "mapped": mapped})
        interpreter.write_text(f"#!/bin/sh\ncat <<'EOF'\n{reply}\nEOF\n")
        interpreter.chmod(0o755)
        if pythonpath is None:
            monkeypatch.delenv("PYTHONPATH", raising=False)
        else:
            monkeypatch.setenv("PYTHONPATH", pythonpath)
        made = PythonCandidate(
            PythonAdapter(str(interpreter)),
            proj / "tools" / "run.py",
            proj,
            SourceEdit("tools/run.py", "x = 1", "x = 2"),
        )
        try:
            launch = made.launch_args(proj / "tools" / "run.py", proj)
            return made.root, launch.get("env", {}).get("PYTHONPATH"), made.warnings
        finally:
            made.close()

    # setuptools' finder (`MAPPING`) and hatchling's exact mode (`_redirections`).
    for mapped in ([["pkg", str(proj / "pkg")]], [["pkg", str(proj / "pkg" / "__init__.py")]]):
        root, pythonpath, _ = candidate(["/usr/lib/python3"], mapped)
        assert pythonpath == str(root), (mapped, pythonpath)
    # Verification of #109: a package mapped to a directory of another name (`package-dir
    # = {pkga = "lib"}`) cannot be moved by its parent: a note, not a wrong redirect.
    root, pythonpath, warnings = candidate([], [["pkga", str(proj / "lib")]])
    assert pythonpath is None and any("`pkga`" in w for w in warnings), warnings
    # An environment inside the root is never moved ahead of the standard library.
    root, pythonpath, _ = candidate([str(proj / ".venv" / "lib" / "site-packages")], [])
    assert pythonpath is None, pythonpath
    # A moved directory holding a standard library name is noted.
    root, pythonpath, warnings = candidate([str(proj / "shadow")], [])
    assert pythonpath == str(root / "shadow") and any("json" in w for w in warnings), warnings
    # Relative entries mean the user's root (the program's working directory), and an
    # empty one the working directory itself.
    root, pythonpath, _ = candidate([], [], pythonpath=f":../shared:{proj / 'lib'}")
    assert pythonpath.split(os.pathsep) == [
        "",
        os.path.realpath(tmp_path / "shared"),
        str(root / "lib"),
    ], pythonpath
    # Verification of #109: a zip on the import path crashed the standard-library check,
    # and directories without `__init__.py` (`site/`, `json/` data) were called modules.
    (proj / "vendor").mkdir()
    (proj / "vendor" / "lib.zip").write_bytes(b"PK")
    (proj / "flat" / "site").mkdir(parents=True)
    (proj / "flat" / "json").mkdir()
    root, pythonpath, warnings = candidate(
        [str(proj / "vendor" / "lib.zip"), str(proj / "flat")], []
    )
    assert pythonpath.split(os.pathsep) == [str(root / "vendor" / "lib.zip"), str(root / "flat")]
    assert not any("standard library" in w for w in warnings), warnings


def test_a_python_candidate_notes_an_import_hook_into_the_tree(tmp_path, monkeypatch):
    # Verification of #109: an import hook ahead of the path (meson-python's editable
    # loader) serves the user's tree whatever `PYTHONPATH` says. What each name the root
    # offers imports as is checked the way the program finds it: a hook that serves the
    # user's file is named. One that only records environment paths (scikit-build-core,
    # a `.venv` inside the root) or whose attributes raise (six's lazy modules) is not.
    from peeky.adapters import PythonAdapter, PythonCandidate
    from peeky.build import SourceEdit

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text("x = 1\n")
    hooks = tmp_path / "hooks"
    hooks.mkdir()

    def candidate(serves):
        (hooks / "sitecustomize.py").write_text(
            "import importlib.util, sys\n"
            "class Lazy:\n"
            "    def __getattr__(self, name):\n        raise ModuleNotFoundError(name)\n"
            "    def find_spec(self, name, path=None, target=None):\n        return None\n"
            "class MesonpyMetaFinder:\n"
            "    def __init__(self, root):\n"
            "        self._build_cmd = [root + '/.venv/bin/ninja']\n        self._root = root\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            f"        if {serves!r} and name == 'app':\n"
            "            return importlib.util.spec_from_file_location(name, self._root + '/app.py')\n"
            "        return None\n"
            f"sys.meta_path.insert(0, MesonpyMetaFinder({str(proj)!r}))\n"
            "sys.meta_path.insert(0, Lazy())\n"
        )
        monkeypatch.setenv("PYTHONPATH", str(hooks))
        made = PythonCandidate(
            PythonAdapter(sys.executable),
            proj / "app.py",
            proj,
            SourceEdit("app.py", "x = 1", "x = 2"),
        )
        made.close()
        return made.warnings

    warnings = candidate(serves=True)
    assert any("`import app` loads" in w and str(proj / "app.py") in w for w in warnings), warnings
    # The hook may rebuild in your tree: the warning says so (review of #253).
    assert any(
        w.endswith("serves it ahead of every import path, and may rebuild there") for w in warnings
    ), warnings
    warnings = candidate(serves=False)
    assert not any("`import app`" in w or "could not ask" in w for w in warnings), warnings


def test_a_path_that_spells_the_root_otherwise_is_inside(tmp_path, monkeypatch):
    # Verification of #109: one flag for "this volume ignores case" missed a
    # case-sensitive volume mounted under a case-insensitive path (`/Volumes/ve24CS`
    # named `/Volumes/ve24cs`). The root is now found among a path's directories by its
    # identity, whatever spells it (here a second name for the same directory).
    from peeky.adapters import PythonAdapter, PythonCandidate
    from peeky.build import SourceEdit

    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "x.py").write_text("x = 1\n")
    (proj / "pyproject.toml").write_text("")
    other = tmp_path / "other-name"
    other.symlink_to(proj, target_is_directory=True)
    candidate = PythonCandidate(
        PythonAdapter(sys.executable),
        proj / "src" / "x.py",
        proj,
        SourceEdit("src/x.py", "x = 1", "x = 2"),
    )
    try:
        assert candidate._within(other / "src" / "x.py") == Path("src/x.py")
        assert candidate._within(other / "gone" / ".git") == Path("gone/.git")
        assert candidate._within(other) == Path(".")
        assert candidate._within(tmp_path / "elsewhere" / "x.py") is None
    finally:
        candidate.close()


@pytest.mark.skipif(
    not hasattr(os, "chflags") or os.geteuid() == 0, reason="needs BSD flags, not root"
)
def test_a_python_candidate_notes_access_the_copy_does_not_keep(tmp_path, monkeypatch):
    # Verification of #109: the copy is the prober's with the originals' mode bits, so a
    # locked file (or another owner's, or one an ACL denies) became writable there.
    import stat as stat_module

    from peeky.adapters import PythonAdapter, PythonCandidate
    from peeky.build import SourceEdit

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("")
    (proj / "app.py").write_text("x = 1\n")
    (proj / "state.txt").write_text("kept\n")
    os.chflags(proj / "state.txt", stat_module.UF_IMMUTABLE)
    try:
        candidate = PythonCandidate(
            PythonAdapter(sys.executable),
            proj / "app.py",
            proj,
            SourceEdit("app.py", "x = 1", "x = 2"),
        )
        candidate.close()
    finally:
        os.chflags(proj / "state.txt", 0)
    assert any("state.txt has other permissions than yours" in w for w in candidate.warnings), (
        candidate.warnings
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_python_candidate_notes_another_owner_and_a_refused_repository(tmp_path, monkeypatch):
    # Verification of #109: as root over another user's checkout, access is the same
    # in both trees but the copy is root's: git refuses the user's repository
    # (`safe.directory`) and not the copy, and the note blamed a conditional include.
    from peeky import adapters
    from peeky.adapters import PythonAdapter, PythonCandidate
    from peeky.build import SourceEdit

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text("x = 1\n")
    git_in(proj, "init", "-q")
    git_in(proj, "add", "app.py")
    git_in(proj, "commit", "-qm", "base")
    real_git = adapters._git

    def refusing(git, where, *args):
        if Path(where) == proj and args[:1] == ("rev-parse",):
            return None
        return real_git(git, where, *args)

    monkeypatch.setattr(adapters, "_git", refusing)
    owner = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    candidate = PythonCandidate(
        PythonAdapter(sys.executable), proj / "app.py", proj, SourceEdit("app.py", "x = 1", "x = 2")
    )
    candidate.close()
    assert any("Git refuses your repository" in w for w in candidate.warnings), candidate.warnings
    assert not any("includeIf" in w for w in candidate.warnings), candidate.warnings
    assert any("belong to another user" in w for w in candidate.warnings), candidate.warnings


def test_the_import_check_never_runs_project_code(tmp_path, capsys, monkeypatch):
    # Verification of #109: `pkg.bak/` (a backup with its own `__init__.py`) and an --at
    # file named `app.v2.py` gave dotted names, and `find_spec("pkg.bak")` imported and
    # ran `pkg` in the copy before the program did.
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    for package in ("pkg", "pkg.bak"):
        (proj / package).mkdir(parents=True)
        (proj / package / "__init__.py").write_text("open('runs.log', 'a').write('run\\n')\n")
    (proj / "pyproject.toml").write_text("")
    # A root module named like the standard library's ran in the probes' `-c`, whose
    # path starts with the copy's root (verification of #109).
    (proj / "json.py").write_text("open('runs.log', 'a').write('json\\n')\n")
    (proj / "app.v2.py").write_text(
        "import pkg\nacc = 1\nruns = len(open('runs.log').read().splitlines())\nprint(acc, runs)\n"
    )
    probe = ["probe", str(proj), "--at", "app.v2.py:4", "--capture", "runs", "--json", "--quiet"]
    code = main([*probe, "--old", "acc = 1", "--new", "acc = 2"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert result["rows"][0]["value"] == "1", result["rows"]
    assert not (proj / "runs.log").exists()


def test_the_import_check_keeps_within_the_launch_budget(tmp_path):
    # Verification of #109: the interpreter probes took up to 120 s each whatever the
    # launch's budget (a meson-python rebuild), so the launch always timed out.
    import time

    from peeky.adapters import PythonAdapter, PythonCandidate
    from peeky.build import SourceEdit

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text("x = 1\n")
    (proj / "pyproject.toml").write_text("")
    interpreter = tmp_path / "python"
    interpreter.write_text("#!/bin/sh\nsleep 30\n")
    interpreter.chmod(0o755)
    started = time.monotonic()
    candidate = PythonCandidate(
        PythonAdapter(str(interpreter)),
        proj / "app.py",
        proj,
        SourceEdit("app.py", "x = 1", "x = 2"),
        deadline=time.monotonic() + 16,
    )
    candidate.close()
    assert time.monotonic() - started < 10
    assert any("could not ask its interpreter" in w for w in candidate.warnings)


def test_a_python_candidate_refuses_an_at_file_inside_a_bare_repository(tmp_path):
    # Verification of #109: a bare repository below the root is copied whole as a private
    # repository, never walked, so an --at file inside it was "not in the copied root".
    from peeky.adapters import PythonAdapter, PythonCandidate
    from peeky.build import SourceEdit

    repository = tmp_path / "srv" / "repo.git"
    for name in ("objects", "refs", "hooks"):
        (repository / name).mkdir(parents=True)
    (repository / "HEAD").write_text("ref: refs/heads/main\n")
    (repository / "hooks" / "check.py").write_text("x = 1\n")
    with pytest.raises(ValueError, match="inside a git repository's own directory .repo.git."):
        PythonCandidate(
            PythonAdapter(sys.executable),
            repository / "hooks" / "check.py",
            tmp_path / "srv",
            SourceEdit("repo.git/hooks/check.py", "x = 1", "x = 2"),
        )
    # The target the refusal names works, and its note does not send the agent back.
    candidate = PythonCandidate(
        PythonAdapter(sys.executable),
        repository / "hooks" / "check.py",
        repository,
        SourceEdit("hooks/check.py", "x = 1", "x = 2"),
    )
    candidate.close()
    assert not any("nothing above was copied" in w for w in candidate.warnings), candidate.warnings


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
        ((("l2;", "x"), ("zz", "y")), "--old matched 0 times \\(edit 2\\); it must match"),
        ((("l2;", "x"), ("l3;", "l3;")), "--new equals --old \\(edit 2\\)"),
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


def test_a_python_candidates_copy_note_is_short_once_and_quiet(tmp_path, monkeypatch, capsys):
    # L6: a one-file, standard-library-only script got a 70-word note on its copied root,
    # printed twice (as progress and with the result), once even with --quiet.
    import json

    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    case = tmp_path / "case"
    case.mkdir()
    alone = case / "words.py"
    alone.write_text(
        "import re\nimport sys\n\n\ndef slug(t):\n    return '-'.join(re.split(r'\\W+', t))\n\n\n"
        "total = 0\nfor arg in sys.argv[1:]:\n    total += len(slug(arg))\nprint(total)\n"
    )
    probe = ["probe", str(alone), "--at", "11", "--capture", "total", "--json"]
    edit = ["--old", "total = 0", "--new", "total = 1"]
    assert main([*probe, *edit, "--", "a b"]) == 0
    result = json.loads(capsys.readouterr().out)
    # It reaches nothing outside its own file: no note.
    assert result["status"] == "completed" and not result.get("warnings"), result

    # One that does (`__file__`) keeps it: in text once, then not again for this root.
    reaching = case / "conf.py"
    reaching.write_text(
        "from pathlib import Path\nhere = Path(__file__).parent\ntotal = 0\nprint(total, here)\n"
    )
    probe = ["probe", str(reaching), "--at", "4", "--capture", "total"]
    edit = ["--old", "total = 0", "--new", "total = 1"]
    assert main([*probe, *edit]) == 0
    captured = capsys.readouterr()
    shown = captured.out + captured.err
    assert shown.count("ran from a copy") == 1 and len(shown.split("(no .git")) == 2, shown
    assert main([*probe, *edit]) == 0
    captured = capsys.readouterr()
    assert "ran from a copy" not in captured.out + captured.err, captured
    assert main([*probe, "--json", *edit]) == 0
    assert any("ran from a copy" in w for w in json.loads(capsys.readouterr().out)["warnings"])
    # --quiet never shows it, even the first time.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "quiet"))
    assert main([*probe, "--quiet", *edit]) == 0
    captured = capsys.readouterr()
    assert "ran from a copy" not in captured.out + captured.err, captured


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


def test_a_candidate_that_moves_no_line_says_so():
    # playlist 5: "locations name the candidate's copy, since the line numbers are the
    # candidate's" for an edit that changed one line in place.
    original = "a = 1\nb = 2\nc = 3\n"
    same = {"candidate": {}}
    cli._bind_candidate(same, original, ("b = 2", "b = 3"), "p.py")
    assert same["candidate"]["note"] == cli.CANDIDATE_SAME_LINES
    assert "line numbers" in cli.CANDIDATE_SAME_LINES
    moved = {"candidate": {}}
    cli._bind_candidate(moved, original, ("b = 2\n", "b = 2\nb += 1\n"), "p.py")
    assert moved["candidate"]["note"] == cli.CANDIDATE_LOCATIONS
    # Two edits that add a line and remove one move the lines between them.
    both = {"candidate": {}}
    cli._bind_candidate(both, original, (("a = 1\n", "a = 1\nz = 0\n"), ("c = 3\n", "")), "p.py")
    assert both["candidate"]["note"] == cli.CANDIDATE_LOCATIONS
    # Review of PR #350: removing a last line that no newline ends removes no newline.
    unended = {"candidate": {}}
    cli._bind_candidate(unended, "a = 1\nb = 2", ("b = 2", ""), "p.py")
    assert unended["candidate"]["note"] == cli.CANDIDATE_LOCATIONS
    # Ending the last line with a newline, or no longer, adds or removes no line.
    for text, edit in (("a\nb", ("b", "c\n")), ("a\nb\n", ("b\n", "c"))):
        assert keeps_lines(text, edit), (text, edit)
        kept = {"candidate": {}}
        cli._bind_candidate(kept, text, edit, "p.py")
        assert kept["candidate"]["note"] == cli.CANDIDATE_SAME_LINES
        assert candidate_line(text, edit, 2) == 2
    assert not keeps_lines("a\nb", ("b", "")) and not keeps_lines("a\nb\n", ("b\n", ""))


def test_a_line_an_edit_removes_at_the_files_end_is_refused():
    # Review of PR #350: removing `b` from `a\nb` mapped line 2 to a line 2 the candidate
    # does not have.
    with pytest.raises(ValueError, match=r"Line 2 is inside the edit \(line 2, which the "):
        candidate_line("a\nb", ("b", ""), 2)
    assert candidate_line("a\nb", ("b", ""), 1) == 1
    with pytest.raises(ValueError, match="Line 3 is inside the edit"):
        candidate_line("a\nb\nc", ("b\nc", "b\n"), 3)
    with pytest.raises(ValueError, match=r"\(line 2, which the candidate removes\)"):
        candidate_line("a\nb\nc\n", ("b\n", ""), 2)
    assert candidate_line("a\nb\nc", ("a", "z"), 3) == 3
