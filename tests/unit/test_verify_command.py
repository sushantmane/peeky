"""`verify --check-command`: the project's own test command, run on a fresh copy of the
project with the edit applied.

A Python checker was given only the edited file, so it could not run the project's
pytest suite on the candidate, the natural check of a fix.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from peeky import adapters, agent
from peeky.cli import main

CALC = "def add(a, b):\n    return a - b\n"
TESTS = """\
import pytest
from calc import add


@pytest.mark.parametrize("a,b,want", [(1, 2, 3), (2, 2, 4)])
def test_add(a, b, want):
    assert add(a, b) == want
"""
PYTEST = "python -m pytest -q -p no:cacheprovider tests/test_calc.py"
FIX = ("--old", "a - b", "--new", "a + b")


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A pytest project whose test of `add` fails until `a - b` becomes `a + b`."""
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "calc"\nversion = "0"\n')
    (root / "calc.py").write_text(CALC)
    (root / "tests" / "test_calc.py").write_text(TESTS)
    return root


def verify(capsys, root, *argv):
    code = main(["verify", str(root), "--at", "calc.py", *map(str, argv)])
    return code, capsys.readouterr()


def files(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_the_projects_tests_fail_at_baseline_and_pass_with_the_fix(project, capsys):
    before = files(project)
    code, out = verify(capsys, project, "--check-command", PYTEST, "--baseline", "--json")
    result = json.loads(out.out)
    assert code == 1 and result["passed"] is False and result["exit_code"] == 1, result
    assert result["baseline"] is True and "2 failed" in result["stdout"]
    assert result["reason"] == (
        "FAILED tests/test_calc.py::test_add[1-2-3] - assert -1 == 3 (and 1 more)"
    )
    command = result["check_command"]
    # pytest runs with Peeky's report plugin, which saw one test file, from the copy.
    words = PYTEST.split()
    assert command["argv"] == words
    assert command["ran"][1:4] == [*words[1:3], "-p"] and command["ran"][5:] == words[3:]
    # Its name is the run's own: no module of the project's stands in for it.
    assert re.fullmatch(r"peeky_check_[0-9a-f]{16}", command["ran"][4]), command["ran"]
    assert result["pytest_report"] == {"reports": 1, "test_files": 1, "problems": []}
    assert result["verdict"] == "failed"
    # `python` is the program's interpreter, the one `python:` names.
    assert Path(command["ran"][0]).resolve() == Path(sys.executable).resolve()
    assert command["copy_of"] == str(project.resolve()) and command["timeout_s"] == 20
    # The root is a project's (pyproject.toml): no note on what is above it.
    assert "warnings" not in result and "candidate" not in result
    code, out = verify(capsys, project, "--check-command", PYTEST, "--baseline")
    assert code == 1
    assert f"command: {PYTEST}\nran: " in out.out and "\ncommand exit: 1\n" in out.out
    assert out.out.startswith("verdict: failed  passed: False  stage: verification")
    assert "\npytest report: 1 test file, and nothing loaded from your project\n" in out.out
    assert (
        "baseline: the unedited program, no edit (its tests fail on it, as they should fail on "
        "the bug)"
    ) in out.out
    # Without -p no:cacheprovider pytest writes its cache, in the copy.
    fix = PYTEST.replace(" -p no:cacheprovider", "")
    code, out = verify(capsys, project, "--check-command", fix, *FIX, "--json")
    result = json.loads(out.out)
    assert code == 0 and result["passed"] is True and result["exit_code"] == 0, result
    assert "2 passed" in result["stdout"] and "+    return a + b" in result["diff"]
    assert "reason" not in result
    # Your files don't change: no edit, no cache, no bytecode.
    assert (project / "calc.py").read_text() == CALC and files(project) == before


def test_a_wrong_fix_fails_on_the_test_it_breaks(project, capsys):
    code, out = verify(
        capsys, project, "--check-command", PYTEST, "--old", "a - b", "--new", "a * b"
    )
    assert code == 1 and "passed: False" in out.out, out.out
    assert "reason: FAILED tests/test_calc.py::test_add[1-2-3] - assert 2 == 3\n" in out.out
    assert "1 failed, 1 passed" in out.out and "-    return a - b\n+    return a * b" in out.out


def test_a_command_past_its_time_is_stopped(project, capsys, monkeypatch):
    # The same bound as a checker's, which the test lowers to 1 s.
    monkeypatch.setattr(agent, "CHECK_S", 1)
    (project / "tests" / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(60)\n"
    )
    slow = "python -m pytest -q -p no:cacheprovider tests/test_slow.py"
    started = time.monotonic()
    code, out = verify(capsys, project, "--check-command", slow, "--baseline", "--json")
    result = json.loads(out.out)
    assert time.monotonic() - started < 30
    assert code == 1 and result["timed_out"] is True and result["exit_code"] is None, result
    # Peeky stopped it: no verdict on the program, which a failure would claim.
    stopped = "the check command ran past 1 s and was stopped"
    assert result["verdict"] == "unknown" and result["no_verdict"] == stopped, result
    assert result["reason"] == stopped
    code, out = verify(capsys, project, "--check-command", slow, "--baseline")
    assert out.out.startswith("verdict: unknown  passed: False  stage: verification"), out.out
    assert out.out.rstrip().endswith(
        f"baseline: the unedited program, no edit ({stopped}, so this says nothing about the bug)"
    ), out.out
    # Any command, not only pytest.
    sleep = ["python", "-c", "import time; time.sleep(60)"]
    code, out = verify(capsys, project, *FIX, "--json", "--check-command", "--", *sleep)
    result = json.loads(out.out)
    assert code == 1 and result["verdict"] == "unknown" and result["no_verdict"] == stopped
    # A --timeout shorter than the command's own bound: TIMEOUT, as with a checker.
    monkeypatch.setattr(agent, "CHECK_S", 20)
    code, out = verify(capsys, project, "--check-command", slow, *FIX, "--timeout", "3", "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "TIMEOUT", error
    assert error["message"] == "verify ran past its --timeout while the check command was running"


def test_a_test_runners_status_that_is_no_verdict_says_so(project, capsys):
    # pytest's 5 (no tests ran) is no failure of the program.
    (project / "tests" / "test_none.py").write_text("X = 1\n")
    none = "python -m pytest -q -p no:cacheprovider tests/test_none.py"
    code, out = verify(capsys, project, "--check-command", none, "--baseline")
    assert code == 1 and "command exit: 5\n" in out.out, out.out
    assert "; pytest exited 5: no tests ran\n" in out.out
    assert (
        "baseline: the unedited program, no edit (pytest exited 5: no tests ran, so this says "
        "nothing about the bug)"
    ) in out.out
    # A command that passes on the bug can't tell a fix from it.
    (project / "tests" / "test_ok.py").write_text("def test_ok():\n    pass\n")
    ok = "python -m pytest -q -p no:cacheprovider tests/test_ok.py"
    code, out = verify(capsys, project, "--check-command", ok, "--baseline")
    assert (
        code == 0 and "(the command passes on it, so it can't tell a fix from the bug)" in out.out
    )


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (
            "python -m pytest -q 'tests/test_calc.py::test_add[2-2-4]'",
            "--check-command takes no shell syntax (`[`, `]`): Peeky runs the command itself",
        ),
        ("python -m pytest -q | tail -3", "--check-command takes no shell syntax (`|`)"),
        ("python -m pytest $HOME > out.txt", "--check-command takes no shell syntax (`$`, `>`)"),
        ("FOO=1 python -m pytest", "--check-command's first word, FOO=1, must be the program"),
        ("nonesuch-tests -q", "--check-command's program nonesuch-tests is not on PATH"),
        ("../run_tests.sh", "--check-command's program ../run_tests.sh is outside the root"),
        ("", "--check-command needs a command"),
    ],
)
def test_a_command_peeky_wont_run_as_given_is_refused(project, capsys, command, message):
    code, out = verify(capsys, project, "--check-command", command, "--baseline", "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "INVALID_REQUEST", error
    assert error["message"].startswith(message), error["message"]


def test_words_after_check_command_dashes_are_passed_as_they_are(project, capsys):
    argv = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    test = "tests/test_calc.py::test_add[2-2-4]"
    code, out = verify(
        capsys, project, "--baseline", "--json", "--check-command", "--", *argv, test
    )
    result = json.loads(out.out)
    assert code == 1 and result["check_command"]["argv"] == [*argv, test], result
    assert result["reason"] == f"FAILED {test} - assert 0 == 4"
    # In the background too: the job keeps the words.
    code, out = verify(capsys, project, *FIX, "--background", "--check-command", "--", *argv, test)
    job = out.out.split()[1]
    for _ in range(60):
        code = main(["job", "wait", job, "--timeout", "5"])
        if code != 3:
            break
    shown = capsys.readouterr().out
    assert code == 0 and "passed: True" in shown and f"command: {' '.join(argv)} '{test}'" in shown


def test_a_check_is_one_of_check_or_check_command(project, capsys, tmp_path):
    (tmp_path / "check.py").write_text("pass\n")
    for argv, message in (
        ((), "verify needs a check: --check <checker.py>, or for Python, --check-command"),
        (("--check", tmp_path / "check.py", "--check-command", PYTEST), "Pass either --check"),
        (("--check-command", PYTEST, "--check-command", "--", "x"), "Pass --check-command once"),
    ):
        code, out = verify(capsys, project, "--baseline", "--json", *argv)
        error = json.loads(out.out)["error"]
        assert code == 2 and error["message"].startswith(message), error


def test_other_languages_are_refused_before_a_build(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n")
    argv = ["verify", str(tmp_path / "main.c"), "--check-command", "make test", "--baseline"]
    assert main([*argv, "--json"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "UNSUPPORTED", error
    assert error["message"].startswith(
        "--check-command supports Python targets for now, and this target is C: pass a --check"
    )
    assert "COOKBOOK 6.5" in error["hint"] and len(error["hint"]) <= 150


SHOW_ENV = """\
import json, os, sys
print(json.dumps({
    "tokens": [name for name in ("DEBUG_TOKEN", "PEEKY_WORKER_TOKEN") if name in os.environ],
    "path": os.environ["PATH"].split(os.pathsep)[0],
    "venv": os.environ.get("VIRTUAL_ENV"),
    "bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),
    "cwd": os.getcwd(),
    "pwd": os.environ.get("PWD"),
}))
"""


def test_the_command_runs_in_the_copy_on_the_interpreters_environment_without_the_token(
    project, capsys, monkeypatch
):
    monkeypatch.setenv("DEBUG_TOKEN", "d" * 40)
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", "w" * 40)
    (project / "show_env.py").write_text(SHOW_ENV)
    code, out = verify(
        capsys, project, "--check-command", "python show_env.py", "--baseline", "--json"
    )
    result = json.loads(out.out)
    seen = json.loads(result["stdout"])
    assert code == 0 and seen["tokens"] == [], seen
    # The interpreter's directory first on PATH, and its virtual environment, if any.
    python = Path(result["check_command"]["ran"][0])
    assert seen["path"] == str(python.parent)
    home = python.parent.parent
    assert seen["venv"] == (str(home) if (home / "pyvenv.cfg").is_file() else None)
    assert seen["bytecode"] == "1"
    # The working directory is the copy's root, not the project's.
    assert Path(seen["cwd"]).name == "proj" and Path(seen["cwd"]).resolve() != project.resolve()
    assert seen["pwd"] == seen["cwd"]
    assert os.environ["DEBUG_TOKEN"] == "d" * 40


def test_a_root_that_is_no_projects_is_noted_though_the_edited_file_imports_nothing(
    project, capsys
):
    # The tests may read or import anything above the copied root: a self-contained
    # edited file, which a candidate probe leaves unnoted, says nothing about them.
    (project / "pyproject.toml").unlink()
    code, out = verify(capsys, project, "--check-command", PYTEST, *FIX)
    assert code == 0 and "passed: True" in out.out, out.out
    assert f"warning: The check command ran in a copy of {project.resolve()}/ only (no .git" in (
        out.out
    )
    assert "give that directory as the target (`peeky verify <project> --at" in out.out


def test_keep_leaves_the_copy_the_command_ran_in(project, capsys):
    code, out = verify(capsys, project, "--check-command", PYTEST, *FIX, "--keep", "--json")
    result = json.loads(out.out)
    kept, candidate = Path(result["kept"]), Path(result["candidate"])
    try:
        assert code == 0 and candidate.is_relative_to(kept) and candidate.name == "calc.py"
        assert candidate.read_text() == CALC.replace("a - b", "a + b")
        assert (kept / "proj" / "tests" / "test_calc.py").read_text() == TESTS
    finally:
        shutil.rmtree(kept)


def test_an_edit_the_copys_imports_may_not_load_is_refused(project, capsys, monkeypatch):
    # The candidate can't ask the interpreter where imports come from, so an editable
    # install's `import calc` might load the unedited module: a verdict on the edit would
    # be unproven. The unedited program (--baseline) is the same either way.
    real = subprocess.run

    def run(command, *args, **kwargs):
        if list(command)[1:2] == ["-c"]:
            raise subprocess.TimeoutExpired(command, 1)
        return real(command, *args, **kwargs)

    monkeypatch.setattr(adapters.subprocess, "run", run)
    code, out = verify(capsys, project, "--check-command", PYTEST, *FIX, "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "INVALID_REQUEST", error
    assert error["message"].startswith(
        "verify --check-command can't test this edit: Peeky could not ask the interpreter "
        "where imports come from"
    )
    assert error["message"].endswith(
        "Otherwise, check the edit with a --check checker, which is given the edited file"
    )
    code, out = verify(capsys, project, "--check-command", PYTEST, "--baseline", "--json")
    result = json.loads(out.out)
    assert code == 1 and result["exit_code"] == 1, result
    assert any("could not ask its interpreter" in note for note in result["warnings"])


def test_a_command_keeps_the_end_of_its_output(tmp_path):
    # The failures pytest summarises come last: a check command keeps each stream's
    # last bytes, from a line's start, not its first.
    lines = "".join(f"line {n}\n" for n in range(5000))
    command = [sys.executable, "-c", f"import sys; sys.stdout.write({lines!r})"]
    result = agent.execute(command, tmp_path, 20, retain=1000, tail=True)
    assert result["stdout"].endswith("line 4999\n") and len(result["stdout"]) <= 1000
    assert result["stdout"].startswith("line ") and result["exit_code"] == 0
    head = agent.execute(command, tmp_path, 20, retain=1000)
    assert head["stdout"].startswith("line 0\n") and len(head["stdout"]) == 1000


def test_a_commands_reason_is_the_line_that_names_the_failed_test():
    from peeky.cli import _command_reason

    def reason(stdout="", stderr="", argv=("python", "check.py"), **extra):
        command = {"argv": list(argv), "timeout_s": 20}
        return _command_reason({"check_command": command, "exit_code": 1, "stdout": stdout,
                                "stderr": stderr, **extra})  # fmt: skip

    pytest_output = (
        ">       assert add(1, 2) == 3\nE       assert -1 == 3\n\n"
        "FAILED tests/t.py::test_add - assert -1 == 3\n1 failed in 0.01s\n"
    )
    # pytest's summary names the test; its `E` line does not.
    pytest = ("python", "-m", "pytest")
    assert reason(pytest_output, argv=pytest) == "FAILED tests/t.py::test_add - assert -1 == 3"
    # A pytest exit 1 with no failed test is said to be one.
    assert reason("E   fixture 'db' not found\n1 error in 0.01s\n", argv=pytest) == (
        "fixture 'db' not found; pytest exited 1, and its output shows no failed test"
    )
    # unittest's (on stderr): the failed test with its exception, not `FAILED (failures=1)`.
    unittest_output = (
        "F\n======\nFAIL: test_add (t.T.test_add)\n------\nTraceback (most recent call last):\n"
        '  File "t.py", line 5, in test_add\n    self.assertEqual(add(1, 2), 3)\n'
        "AssertionError: -1 != 3\n\n------\nRan 1 test in 0.000s\n\nFAILED (failures=1)\n"
    )
    unittest = ("python", "-m", "unittest")
    expected = "FAIL: test_add (t.T.test_add): AssertionError: -1 != 3"
    assert reason(stderr=unittest_output, argv=unittest) == expected
    # Both streams are searched for what failed before any last line: unittest's report on
    # stderr wins over what the tests print on stdout.
    assert reason("setup completed\n", unittest_output, argv=unittest) == expected
    assert reason("connecting\nsetup completed\n", "Traceback (most recent call last):\n"
                  '  File "x.py", line 1\nValueError: bad port\n') == "ValueError: bad port"  # fmt: skip
    assert reason("checking\nFAIL first case\n    expected 3\n") == "FAIL first case; expected 3"
    assert reason("no failure line here\nlast line\n") == "last line"
    assert reason() == "the check command exited 1"
    assert reason(output_limit_exceeded=True, exit_code=None) == (
        "the check command printed over 1 MiB of output, both streams together, and was stopped"
    )


def test_a_script_of_the_project_runs_from_the_copy(project, capsys):
    script = project / "run_tests.sh"
    script.write_text('#!/bin/sh\necho "in $(pwd)"\nexit 3\n')
    script.chmod(0o755)
    code, out = verify(capsys, project, "--check-command", "./run_tests.sh", *FIX, "--json")
    result = json.loads(out.out)
    ran = Path(result["check_command"]["ran"][0])
    assert code == 1 and result["exit_code"] == 3 and ran.name == "run_tests.sh", result
    assert not ran.is_relative_to(project) and result["stdout"] == f"in {ran.parent}\n"
    assert result["reason"] == f"in {ran.parent}"


PKG = "VALUE = 1\n"
IMPORT_PKG = ["python", "-c", "import pkg; assert pkg.VALUE == 2, pkg.__file__"]
SET_VALUE = ("--at", "src/pkg/__init__.py", "--old", "VALUE = 1", "--new", "VALUE = 2")


@pytest.fixture
def src_project(tmp_path, monkeypatch):
    """A src-layout project whose edit sets `pkg.VALUE` to 2."""
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    root = tmp_path / "srcproj"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "pkg"\nversion = "0"\n')
    (root / "src" / "pkg" / "__init__.py").write_text(PKG)
    return root


def test_an_outside_pythonpath_entry_with_the_projects_package_refuses_the_edit(
    src_project, capsys, monkeypatch, tmp_path
):
    # Its `import pkg` found the outside copy, not the edit: `passed: true` for a
    # command that never ran the edit.
    outside = tmp_path / "outside"
    (outside / "pkg").mkdir(parents=True)
    (outside / "pkg" / "__init__.py").write_text(PKG)
    given = os.environ.get("PYTHONPATH")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(outside), given])))
    code = main(["verify", str(src_project), *SET_VALUE, "--json", "--check-command", "--"]
                + IMPORT_PKG)  # fmt: skip
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 2 and error["code"] == "INVALID_REQUEST", error
    assert error["message"].startswith(
        f"verify --check-command can't test this edit: `import pkg` finds "
        f"{outside / 'pkg' / '__init__.py'}, outside the copy"
    ), error["message"]
    # The unedited program runs, with the note.
    code = main(["verify", str(src_project), "--at", "src/pkg/__init__.py", "--baseline",
                 "--json", "--check-command", "--", *IMPORT_PKG])  # fmt: skip
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and any("`import pkg` finds" in note for note in result["warnings"])
    # The project's own `src` on PYTHONPATH (relative: the root's) is the copy's.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, ["src", str(outside), given])))
    code = main(["verify", str(src_project), *SET_VALUE, "--json", "--check-command", "--"]
                + IMPORT_PKG)  # fmt: skip
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["passed"] is True, result


def make_venv(where):
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(where)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    python = where / "bin" / "python"
    found = subprocess.run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return python, Path(found.stdout.strip())


def test_a_stale_installed_copy_of_the_projects_package_refuses_the_edit(src_project, capsys):
    # A non-editable install puts its own copy in site-packages, which `import pkg` finds.
    python, site = make_venv(src_project / ".venv")
    (site / "pkg").mkdir()
    (site / "pkg" / "__init__.py").write_text(PKG)
    code = main(["verify", str(src_project), *SET_VALUE, "--json", "--check-command", "--"]
                + IMPORT_PKG)  # fmt: skip
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 2 and error["code"] == "INVALID_REQUEST", error
    assert error["message"].startswith("verify --check-command can't test this edit: `import pkg` "
                                       "finds "), error["message"]  # fmt: skip
    assert f"{os.sep.join(site.parts[-2:])}/pkg/__init__.py, outside the copy" in error["message"]
    assert "uninstall that, or install the project editable" in error["message"]


RUNTESTS = """\
#!/usr/bin/env python3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import calc

print(__file__)
assert calc.add(1, 2) == 3, calc.__file__
"""


def test_a_program_on_path_inside_the_project_runs_as_the_copys(project, capsys, monkeypatch):
    # With `PATH=<project>/bin:...`, the project's own script ran, importing the
    # unedited `calc` beside it.
    (project / "bin").mkdir()
    script = project / "bin" / "runtests"
    script.write_text(RUNTESTS)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{project / 'bin'}{os.pathsep}{os.environ['PATH']}")
    code, out = verify(capsys, project, "--check-command", "runtests", *FIX, "--json")
    result = json.loads(out.out)
    ran = Path(result["check_command"]["ran"][0])
    assert code == 0 and result["passed"] is True, result
    assert ran.name == "runtests" and not ran.is_relative_to(project.resolve())
    assert result["stdout"] == f"{ran}\n"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["python", "-m", "pytest", "{root}/tests/test_calc.py"],
            "--check-command's argument {root}/tests/test_calc.py names a path in your project",
        ),
        (
            ["python", "-m", "pytest", "--rootdir={root}", "tests"],
            "--check-command's argument --rootdir={root} names a path in your project",
        ),
        (["tox", "-e", "py"], "--check-command doesn't run tox: tox and nox build their own"),
        (["python", "-I", "-m", "nox"], "--check-command doesn't run nox: tox and nox"),
        (["python3.99", "-m", "pytest"], "--check-command's program python3.99 is not on PATH"),
        (["bin/other-tests"], "--check-command's program bin/other-tests ("),
        (["bin/long-tests"], "--check-command's program bin/long-tests ("),
    ],
)
def test_a_command_that_reads_your_files_or_another_python_is_refused(
    project, capsys, argv, message
):
    (project / "bin").mkdir()
    for name, head in (
        ("other-tests", "#!/opt/other/bin/python3\n"),
        # pip's form for an interpreter path too long for `#!`.
        ("long-tests", "#!/bin/sh\n'''exec' \"/opt/other/bin/python\" \"$0\" \"$@\"\n' '''\n"),
    ):
        (project / "bin" / name).write_text(head + "print('ran')\n")
        (project / "bin" / name).chmod(0o755)
    root = project.resolve()
    code, out = verify(capsys, project, "--baseline", "--json", "--check-command", "--",
                       *(word.format(root=root) for word in argv))  # fmt: skip
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "INVALID_REQUEST", error
    assert error["message"].startswith(message.format(root=root)), error["message"]
    if argv[0].startswith("bin/"):
        assert "runs on /opt/other/bin/python" in error["message"]


def test_tox_nox_and_eggs_directories_are_left_out_of_the_copy(project, capsys):
    # They were links into your tree: a command writing `.tox/py/marker` wrote yours.
    for name in (".tox", ".nox", ".eggs"):
        (project / name / "py").mkdir(parents=True)
    write = "import os, sys; sys.exit(sum(os.path.exists(d) for d in ('.tox', '.nox', '.eggs')))"
    code, out = verify(capsys, project, *FIX, "--json", "--check-command", "--", "python", "-c",
                       write)  # fmt: skip
    result = json.loads(out.out)
    assert code == 0 and result["exit_code"] == 0, result
    assert sorted(path.name for path in (project / ".tox").iterdir()) == ["py"]


def test_python_options_before_m_still_name_the_runner(project, capsys):
    # `python -I -m pytest` that ran no test was said to fail on the bug.
    (project / "tests" / "test_none.py").write_text("X = 1\n")
    none = "python -I -m pytest -q -p no:cacheprovider tests/test_none.py"
    code, out = verify(capsys, project, "--check-command", none, "--baseline")
    assert code == 1 and "command exit: 5\n" in out.out, out.out
    assert "(pytest exited 5: no tests ran, so this says nothing about the bug)" in out.out


def test_pytests_console_script_is_its_runner(project, capsys):
    # Found on PATH in the interpreter's directory, and run on that interpreter. Its
    # script directory is that `bin`, so `import calc` fails: a collection error.
    command = "pytest -q -p no:cacheprovider tests/test_calc.py"
    code, out = verify(capsys, project, "--baseline", "--json", "--check-command", command)
    result = json.loads(out.out)
    ran = Path(result["check_command"]["ran"][0])
    assert code == 1 and ran.name == "pytest" and result["exit_code"] == 2, result
    assert os.path.samefile(ran.parent, Path(sys.executable).parent)
    code, out = verify(capsys, project, "--baseline", "--check-command", command)
    assert (
        "(pytest exited 2: it was interrupted, as by a test file it could not import, so this "
        "says nothing about the bug)"
    ) in out.out, out.out
    # A root conftest.py puts the root on pytest's path: the tests run, and fail.
    (project / "conftest.py").write_text("")
    code, out = verify(capsys, project, "--baseline", "--check-command", command)
    assert code == 1 and "command exit: 1\n" in out.out, out.out
    assert "(its tests fail on it, as they should fail on the bug)" in out.out


UNITTESTS = """\
import unittest

from calc import add


class AddTest(unittest.TestCase):
    def setUp(self):
        print("setup completed")

    def test_add(self):
        self.assertEqual(add(1, 2), 3)
"""


def test_unittest_failures_are_a_verdict_and_its_import_errors_are_not(project, capsys):
    (project / "tests" / "test_unit.py").write_text(UNITTESTS)
    command = "python -m unittest tests/test_unit.py"
    code, out = verify(capsys, project, "--check-command", command, "--baseline")
    # What the test printed on stdout doesn't hide the failure unittest reports on stderr.
    assert code == 1 and "reason: FAIL: test_add (" in out.out, out.out
    assert "AssertionError: -1 != 3\n" in out.out
    assert "(its tests fail on it, as they should fail on the bug)" in out.out
    (project / "tests" / "test_broken.py").write_text("import nonesuch_module\n")
    command = "python -m unittest tests/test_broken.py"
    code, out = verify(capsys, project, "--check-command", command, "--baseline")
    assert (
        code == 1
        and (
            "(unittest exited 1: it could not import a test module, so this says nothing about the "
            "bug)"
        )
        in out.out
    ), out.out


def test_a_script_that_crashes_is_no_verdict(project, capsys):
    (project / "check.py").write_text("raise SystemExit('no database')\n")
    code, out = verify(capsys, project, "--check-command", "python check.py", "--baseline")
    assert code == 1 and "reason: no database\n" in out.out, out.out
    assert (
        "baseline: the unedited program, no edit (the command exited 1, but Peeky reads failed "
        "tests only from pytest and unittest: check that its output shows the bug)"
    ) in out.out


@pytest.mark.parametrize(
    ("words", "module"),
    [
        (["python", "-m", "pytest"], "pytest"),
        (["python", "-I", "-B", "-m", "pytest", "-q"], "pytest"),
        (["/v/bin/python3.12", "-X", "dev", "-W", "error", "-m", "unittest"], "unittest"),
        (["python", "-Wignore", "-mpytest"], "pytest"),
        (["python", "-IBm", "pytest"], "pytest"),
        (["python", "-c", "import pytest"], None),
        (["python", "tests/run.py", "-m", "pytest"], None),
        (["python", "--check-hash-based-pycs", "always", "-m", "pytest"], None),
        (["pytest", "-m", "unittest"], None),
    ],
)
def test_the_module_python_runs_is_read_past_its_options(words, module):
    from peeky.cli import _module_run

    assert _module_run(words) == module


def test_the_output_limit_counts_both_streams(tmp_path):
    # 700,000 bytes on each stream passed under a 1 MiB limit per stream.
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('o' * 700000); sys.stdout.flush(); "
        "sys.stderr.write('e' * 700000)",
    ]
    result = agent.execute(command, tmp_path, 20, tail=True)
    assert result["output_limit_exceeded"] is True and result["exit_code"] is None, result


# Review round 2: the import check is the command's own start; pytest reports what it
# loaded; relative PATH entries are the copy's.

FLAT_IMPORT = ["python", "-c", "import pkg; assert pkg.VALUE == 2, pkg.__file__"]


def test_python_dash_i_ignores_pythonpath_so_a_stale_install_refuses_the_edit(
    src_project, capsys, monkeypatch
):
    # PYTHONPATH=src made the check find the copy, but `python -I` ignores it, and the
    # stale install answered: `passed: true` for a command that never ran the edit.
    _, site = make_venv(src_project / ".venv")
    (site / "pkg").mkdir()
    (site / "pkg" / "__init__.py").write_text(PKG)
    monkeypatch.setenv("PYTHONPATH", "src")
    stale = ["python", "-I", "-c", "import pkg; assert pkg.VALUE == 1"]
    code = main(["verify", str(src_project), *SET_VALUE, "--json", "--check-command", "--"]
                + stale)  # fmt: skip
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 2 and "site-packages/pkg/__init__.py, outside the copy" in error["message"]
    # Without -I, PYTHONPATH=src puts the copy's `src` first: the edit is tested.
    code = main(["verify", str(src_project), *SET_VALUE, "--json", "--check-command", "--",
                 "python", "-c", "import pkg; assert pkg.VALUE == 2"])  # fmt: skip
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["passed"] is True, result


def test_a_flat_package_first_in_the_working_directory_is_the_copys(tmp_path, capsys, monkeypatch):
    # `python -c` puts the working directory, the copy's root, first: its `pkg` wins over
    # a stale install, which was refused.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    root = tmp_path / "flat"
    (root / "pkg").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "pkg"\nversion = "0"\n')
    (root / "pkg" / "__init__.py").write_text(PKG)
    _, site = make_venv(root / ".venv")
    (site / "pkg").mkdir()
    (site / "pkg" / "__init__.py").write_text(PKG)
    argv = ["verify", str(root), "--at", "pkg/__init__.py", "--old", "VALUE = 1", "--new",
            "VALUE = 2", "--json", "--check-command", "--"]  # fmt: skip
    code = main([*argv, *FLAT_IMPORT])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["passed"] is True, result
    # A script in a subdirectory has that directory first, not the root: the stale one.
    (root / "checks").mkdir()
    (root / "checks" / "run.py").write_text("import pkg\nassert pkg.VALUE == 2, pkg.__file__\n")
    code = main([*argv, "python", "checks/run.py"])
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 2 and "site-packages/pkg/__init__.py, outside the copy" in error["message"]


NAMESPACE = ("--at", "src/acme/foo/mod.py", "--old", "VALUE = 1", "--new", "VALUE = 2")
IMPORT_MOD = ["python", "-c", "import acme.foo.mod as m; assert m.VALUE == 2, m.__file__"]


@pytest.fixture
def namespace_project(tmp_path, monkeypatch):
    """`acme`, a namespace package split across distributions: the project supplies
    `acme.foo.mod`, another `acme.bar`."""
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    root = tmp_path / "ns"
    (root / "src" / "acme" / "foo").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "acme-foo"\nversion = "0"\n')
    (root / "src" / "acme" / "foo" / "mod.py").write_text("VALUE = 1\n")
    other = tmp_path / "other"
    (other / "acme").mkdir(parents=True)
    (other / "acme" / "bar.py").write_text("BAR = 1\n")
    return root, other


def test_a_namespace_package_split_across_distributions_is_not_refused(
    namespace_project, capsys, monkeypatch
):
    # `acme` has a part outside the copy, but `acme.foo.mod` is the copy's: refused.
    root, other = namespace_project
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(other), "src"]))
    code = main(["verify", str(root), *NAMESPACE, "--json", "--check-command", "--", *IMPORT_MOD])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["passed"] is True, result
    # Another distribution's `acme/foo/mod.py`, first on the path, shadows the copy's.
    (other / "acme" / "foo").mkdir()
    (other / "acme" / "foo" / "mod.py").write_text("VALUE = 1\n")
    code = main(["verify", str(root), *NAMESPACE, "--json", "--check-command", "--", *IMPORT_MOD])
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 2, error
    assert (
        f"`import acme.foo.mod` finds {other / 'acme' / 'foo' / 'mod.py'}, outside the copy"
        in (error["message"])
    )


def test_pytest_addopts_naming_your_project_is_refused(project, capsys, monkeypatch):
    # PYTEST_ADDOPTS=<root>/tests/test_original.py collected your test, not the copy's.
    monkeypatch.setenv("PYTEST_ADDOPTS", f"-q {project.resolve()}/tests/test_calc.py")
    code, out = verify(capsys, project, "--check-command", PYTEST, *FIX, "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and error["message"].startswith(
        f"--check-command's PYTEST_ADDOPTS word {project.resolve()}/tests/test_calc.py names a "
        "path in your project"
    ), error


def test_pytest_tests_collected_from_your_project_are_no_verdict(project, capsys):
    # A config's absolute `testpaths` reaches your tests from the copy: they pass, but
    # they are not the copy's.
    (project / "pyproject.toml").write_text(
        '[project]\nname = "calc"\nversion = "0"\n\n[tool.pytest.ini_options]\n'
        f'testpaths = ["{project.resolve()}/tests"]\n'
    )
    command = "python -m pytest -q -p no:cacheprovider"
    code, out = verify(capsys, project, "--check-command", command, *FIX, "--json")
    result = json.loads(out.out)
    assert code == 1 and result["exit_code"] == 0 and result["passed"] is False, result
    collected = f"pytest collected {project.resolve()}/tests/test_calc.py from your project"
    assert result["no_verdict"].startswith(collected), result["no_verdict"]
    assert result["reason"] == f"no verdict: {result['no_verdict']}"
    assert result["pytest_report"]["problems"][0] == result["no_verdict"]


def test_a_stale_package_pytest_loads_is_no_verdict(src_project, capsys):
    # The tests import `pkg`, which the stale install serves: they pass on it.
    import _pytest

    _, site = make_venv(src_project / ".venv")
    (site / "pkg").mkdir()
    (site / "pkg" / "__init__.py").write_text(PKG)
    # pytest, from Peeky's own environment, after the stale `pkg`.
    (site / "with_pytest.pth").write_text(f"{Path(_pytest.__file__).parents[1]}\n")
    (src_project / "tests").mkdir()
    (src_project / "tests" / "test_pkg.py").write_text(
        "import pkg\n\n\ndef test_value():\n    assert pkg.VALUE == 1\n"
    )
    code = main(["verify", str(src_project), *SET_VALUE, "--json", "--check-command",
                 "python -m pytest -q -p no:cacheprovider tests"])  # fmt: skip
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["exit_code"] == 0 and result["passed"] is False, result
    assert result["no_verdict"].startswith("`pkg` was loaded from "), result["no_verdict"]
    assert "site-packages/pkg/__init__.py, outside the copy" in result["no_verdict"]


def test_a_pytest_run_without_peekys_report_is_no_verdict(project, capsys):
    # A conftest that ends the process before the session's end: no report.
    (project / "tests" / "conftest.py").write_text("import os\n\nos._exit(0)\n")
    code, out = verify(capsys, project, "--check-command", PYTEST, *FIX)
    assert code == 1 and out.out.startswith("verdict: unknown  passed: False"), out.out
    assert (
        "reason: no verdict: pytest wrote no report from Peeky's plugin (-p no:<its name> "
        "turned it off, or pytest stopped first), so Peeky can't tell whether the tests loaded "
        "your project's files instead of the copy's\n"
    ) in out.out
    (project / "tests" / "conftest.py").unlink()
    # Under -I, which ignores PYTHONPATH, pytest runs without it.
    (project / "conftest.py").write_text("")
    isolated = "python -I -m pytest -q -p no:cacheprovider tests/test_calc.py"
    code, out = verify(capsys, project, "--check-command", isolated, *FIX, "--json")
    result = json.loads(out.out)
    assert code == 1 and result["exit_code"] == 0, result
    assert "peeky_check_report" not in result["check_command"]["ran"]
    assert "(-I ignores PYTHONPATH, where the plugin is)" in result["no_verdict"]


def test_relative_path_entries_are_the_copys_root(project, capsys, monkeypatch, tmp_path):
    # `PATH=bin:$PATH` from another directory: `bin` was Peeky's working directory's.
    (project / "bin").mkdir()
    script = project / "bin" / "runtests"
    script.write_text(RUNTESTS)
    script.chmod(0o755)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("PATH", f"bin{os.pathsep}{os.environ['PATH']}")
    code, out = verify(capsys, project, "--check-command", "runtests", *FIX, "--json")
    result = json.loads(out.out)
    ran = Path(result["check_command"]["ran"][0])
    assert code == 0 and result["passed"] is True, result
    assert ran.parent.name == "bin" and not ran.is_relative_to(project.resolve())


# Review round 3: the report is this run's plugin's; processes the tests start; a run that
# loaded outside code is no verdict whatever its status; the tri-state verdict.

FORGED = """\
import json
import os


def pytest_sessionfinish(session):
    folder = os.environ.get("PEEKY_CHECK_REPORTS")
    if folder:
        with open(os.path.join(folder, "1.json"), "w") as stream:
            json.dump({"tests": [], "conftests": [], "modules": {}}, stream)
"""


def stale_pytest_project(root):
    """``root``'s `.venv` with a stale `pkg` in site-packages, and pytest (Peeky's) after it."""
    import _pytest

    _, site = make_venv(root / ".venv")
    (site / "pkg").mkdir()
    (site / "pkg" / "__init__.py").write_text(PKG)
    (site / "with_pytest.pth").write_text(f"{Path(_pytest.__file__).parents[1]}\n")
    (root / "tests").mkdir()


def test_a_module_named_like_peekys_plugin_cant_forge_its_report(src_project, capsys):
    # A `peeky_check_report.py` in the root stood in for the plugin and reported nothing
    # loaded: `passed: true`, though the tests imported a stale outside `pkg`.
    stale_pytest_project(src_project)
    (src_project / "peeky_check_report.py").write_text(FORGED)
    (src_project / "tests" / "test_pkg.py").write_text(
        "import pkg\n\n\ndef test_value():\n    assert pkg.VALUE == 1\n"
    )
    code = main(["verify", str(src_project), *SET_VALUE, "--json", "--check-command",
                 "python -m pytest -q -p no:cacheprovider tests"])  # fmt: skip
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["exit_code"] == 0 and result["verdict"] == "unknown", result
    assert result["no_verdict"].startswith("`pkg` was loaded from "), result["no_verdict"]


def test_a_stale_package_makes_a_failing_run_no_verdict_too(src_project, capsys):
    # The tests failed on the stale `pkg`, not on the edit: that was reported as the
    # edit failing.
    stale_pytest_project(src_project)
    (src_project / "tests" / "test_pkg.py").write_text(
        "import pkg\n\n\ndef test_value():\n    assert pkg.VALUE == 2\n"
    )
    command = ["--check-command", "python -m pytest -q -p no:cacheprovider tests"]
    code = main(["verify", str(src_project), *SET_VALUE, *command])
    out = capsys.readouterr().out
    assert code == 1 and out.startswith("verdict: unknown  passed: False"), out
    assert "reason: no verdict: `pkg` was loaded from " in out and "command exit: 1\n" in out
    # Nor does the unedited program's failure say the tests catch the bug.
    code = main(["verify", str(src_project), "--at", "src/pkg/__init__.py", "--baseline", *command])
    out = capsys.readouterr().out
    assert code == 1 and "baseline: the unedited program, no edit (`pkg` was loaded from " in out
    assert "outside the copy, so this says nothing about the bug)" in out


CHILD = """\
import subprocess
import sys


def test_child():
    subprocess.run({argv}, check=True)
"""


def test_a_python_process_the_tests_start_is_no_verdict(project, capsys):
    # A test that ran `python -I -c "import pkg; ..."` imported what Peeky never saw.
    (project / "tests" / "test_child.py").write_text(
        CHILD.format(argv='[sys.executable, "-I", "-c", "print(1)"]')
    )
    command = "python -m pytest -q -p no:cacheprovider tests"
    code, out = verify(capsys, project, "--check-command", command, *FIX, "--json")
    result = json.loads(out.out)
    assert code == 1 and result["exit_code"] == 0 and result["verdict"] == "unknown", result
    assert result["no_verdict"].startswith("the tests started "), result
    assert result["no_verdict"].endswith(
        "-I -c 'print(1)'; Peeky can't check what it ran or loaded"
    ), result["no_verdict"]


def test_another_process_the_tests_start_is_a_note(project, capsys):
    (project / "tests" / "test_child.py").write_text(CHILD.format(argv='["/bin/echo", "hi"]'))
    command = "python -m pytest -q -p no:cacheprovider tests"
    code, out = verify(capsys, project, "--check-command", command, *FIX, "--json")
    result = json.loads(out.out)
    assert code == 0 and result["verdict"] == "passed" and result["passed"] is True, result
    assert (
        "The tests started 1 process of programs that start no other (/bin/echo hi)"
        in result["warnings"]
    )


class _Candidate:
    """What _pytest_reports asks of a candidate: its copy, and your tree's files."""

    def __init__(self, root, yours):
        self.root, self.yours = root, yours

    def copy_path(self, path):
        return str(path) if Path(path).is_relative_to(self.yours) else None


def write_reports(folder, *reports):
    folder.mkdir(parents=True, exist_ok=True)
    for number, report in enumerate(reports, 1):
        (folder / f"{number}.json").write_text(json.dumps(report))


def test_a_report_counts_only_from_this_runs_plugin_and_every_xdist_worker(tmp_path):
    from peeky.cli import _pytest_reports

    copy, yours, plugin = tmp_path / "copy", tmp_path / "yours", tmp_path / "plugin.py"
    candidate = _Candidate(copy, yours)

    def report(**extra):
        return {"nonce": "n1", "plugin": str(plugin), "role": "main", "worker": None,
                "workers": [], "tests": [str(copy / "tests/test_x.py")], "conftests": [],
                "modules": {}, "started": {"count": 0, "unknown": 0, "examples": []},
                **extra}  # fmt: skip

    def read(*reports):
        folder = tmp_path / f"reports{len(list(tmp_path.glob('reports*')))}"
        write_reports(folder, *reports)
        return _pytest_reports((folder, "n1", plugin), candidate, {"pkg"})

    assert read(report()) == (1, 1, 0, [], [])
    # Another nonce, or another plugin file: not this run's plugin's.
    assert read(report(nonce="n2"))[3] == ["1.json in pytest's reports isn't from Peeky's plugin"]
    other = report(plugin=str(tmp_path / "peeky_check_report.py"))
    assert read(report(), other)[3] == ["2.json in pytest's reports isn't from Peeky's plugin"]
    # pytest-xdist: the main process names its workers; each must report.
    main_report = report(workers=["gw0", "gw1"], tests=[])
    gw0 = report(role="worker", worker="gw0")
    assert read(main_report, gw0)[3] == ["pytest-xdist's worker gw1 wrote no report"]
    assert read(main_report, gw0, report(role="worker", worker="gw1"))[3] == []
    assert read(gw0)[3] == ["pytest's main process wrote no report from Peeky's plugin"]
    # Files from your tree, and the project's modules from outside the copy.
    loaded = report(
        conftests=[str(yours / "conftest.py")],
        modules={"pkg": str(tmp_path / "site/pkg/__init__.py"), "json": "/usr/lib/json.py"},
    )
    assert read(loaded)[3] == [
        f"pytest loaded {yours / 'conftest.py'} from your project, not the copy",
        f"`pkg` was loaded from {tmp_path / 'site/pkg/__init__.py'}, outside the copy",
    ]
    # A process of a program that starts no other is a note; any other a problem.
    started = {"count": 3, "unknown": 1, "examples": [
        {"args": ["echo", "hi"], "kind": "other", "why": None},
        {"args": ["/v/bin/python", "-I", "-c", "import pkg"], "kind": "unknown",
         "why": None}]}  # fmt: skip
    _, _, _, problems, notes = read(report(started=started))
    assert problems == [
        "the tests started /v/bin/python -I -c 'import pkg'; Peeky can't check what it ran or "
        "loaded"
    ]
    assert notes == ["The tests started 2 processes of programs that start no other (echo hi)"]
    started = {"count": 2, "unknown": 2, "examples": [
        {"args": ["os.fork"], "kind": "unknown", "why": "a copy of the test process"}]}  # fmt: skip
    assert read(report(started=started))[3] == [
        "the tests started 2 programs, such as os.fork (a copy of the test process); Peeky "
        "can't check what they ran or loaded"
    ]


def test_a_pass_that_ran_no_test_is_no_verdict(project, capsys):
    # `--collect-only` exits 0 having run no test.
    code, out = verify(capsys, project, "--check-command", f"{PYTEST} --collect-only", *FIX,
                       "--json")  # fmt: skip
    result = json.loads(out.out)
    assert code == 1 and result["exit_code"] == 0 and result["verdict"] == "unknown", result
    assert result["no_verdict"] == "pytest ran no test, so its pass shows nothing of the edit"
    assert result["pytest_report"]["test_files"] == 1


# Review rounds 4 and 5: a process the tests start leaves the verdict only if it is of a
# program that starts no other (an allowlist, by the file's real name); multiprocessing is
# seen where it starts a process.

FORK = """\
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)"""


def start_process(method):
    return f"""\
    import multiprocessing

    process = multiprocessing.get_context("{method}").Process(target=os.getpid)
    process.start()
    process.join()"""


POOL = """\
    import concurrent.futures
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(1, mp_context=context) as pool:
        assert pool.submit(os.getpid).result()"""
CANT = "; Peeky can't check what it ran or loaded"


@pytest.mark.parametrize(
    ("body", "verdict", "said"),
    [
        # Launchers, shells and other programs that run what they are given.
        ('subprocess.run(["/usr/bin/env", "python3", "-c", "pass"], check=True)', "unknown",
         "the tests started /usr/bin/env python3 -c pass" + CANT),
        ('subprocess.run("python3 -c pass", shell=True, check=True)', "unknown",
         "the tests started /bin/sh -c 'python3 -c pass'" + CANT),
        ('subprocess.run(["/usr/bin/find", ".", "-maxdepth", "0", "-exec", "/bin/echo", ";"],'
         " check=True)", "unknown",
         "the tests started /usr/bin/find . -maxdepth 0 -exec /bin/echo" + CANT),
        (FORK, "unknown",
         "the tests started os.fork (a copy of the test process, which Peeky doesn't follow)"
         + CANT),
        # multiprocessing, once for each process it starts, whatever the method.
        (start_process("spawn"), "unknown",
         "the tests started multiprocessing spawn (a multiprocessing process)" + CANT),
        (start_process("fork"), "unknown",
         "the tests started multiprocessing fork (a multiprocessing process)" + CANT),
        (POOL, "unknown",
         "the tests started multiprocessing spawn (a multiprocessing process)" + CANT),
        # Importing what starts a process starts none.
        ("    import multiprocessing.popen_spawn_posix", "passed", None),
        # echo, found on the child's own PATH, or from its own working directory.
        ('subprocess.run(["mytool", "x"], env={"PATH": TOOLS}, check=True)', "passed",
         "(mytool x)"),
        ('subprocess.run(["./mytool", "x"], cwd=TOOLS, check=True)', "passed", "(./mytool x)"),
        ('subprocess.run(["/usr/bin/true"], check=True)', "passed", "(/usr/bin/true)"),
        # A link to Python, by its target's name; a script, whatever its `#!` names.
        ('subprocess.run(["pylink", "-c", "pass"], env={"PATH": TOOLS}, check=True)', "unknown",
         "the tests started pylink -c pass" + CANT),
        ('subprocess.run([TOOLS + "/echoscript"], check=True)', "unknown", "/echoscript" + CANT),
        ('subprocess.run(["no-such-program-x"])', "unknown",
         "the tests started no-such-program-x (Peeky can't find it)" + CANT),
    ],
)  # fmt: skip
def test_only_a_program_that_starts_no_other_leaves_the_verdict(
    project, capsys, tmp_path, body, verdict, said
):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "mytool").symlink_to("/bin/echo")
    (tools / "pylink").symlink_to(os.path.realpath(sys.executable))
    (tools / "echoscript").write_text("#!/bin/echo\n")
    (tools / "echoscript").chmod(0o755)
    call = body if body.startswith("    ") else f"    {body}"
    if "no-such" in body:
        call = f"    try:\n    {call}\n    except FileNotFoundError:\n        pass"
    (project / "tests" / "test_child.py").write_text(
        f"import os\nimport subprocess\nimport sys\n\nTOOLS = {str(tools)!r}\n\n\n"
        f"def test_child():\n{call}\n"
    )
    command = "python -m pytest -q -p no:cacheprovider tests"
    code, out = verify(capsys, project, "--check-command", command, *FIX, "--json")
    result = json.loads(out.out)
    assert result["exit_code"] == 0 and result["verdict"] == verdict, result
    if verdict == "unknown":
        # One start, counted once: the message names it, not a count.
        assert result["no_verdict"].startswith("the tests started "), result["no_verdict"]
        assert result["no_verdict"].endswith(said), result["no_verdict"]
        assert " programs, such as " not in result["no_verdict"]
    elif said:
        assert any(said in warning for warning in result["warnings"]), result["warnings"]
    else:
        assert not any("started" in warning for warning in result.get("warnings", ()))


def test_multiprocessing_preloaded_at_start_up_still_counts(project, capsys, monkeypatch, tmp_path):
    # A `.pth` or sitecustomize that imported multiprocessing's spawn module before the
    # plugin loaded hid a spawn started later.
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text("import multiprocessing.popen_spawn_posix\n")
    given = os.environ.get("PYTHONPATH")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(site), given])))
    (project / "tests" / "test_child.py").write_text(
        f"import os\n\n\ndef test_child():\n{start_process('spawn')}\n"
    )
    command = "python -m pytest -q -p no:cacheprovider tests"
    code, out = verify(capsys, project, "--check-command", command, *FIX, "--json")
    result = json.loads(out.out)
    assert result["exit_code"] == 0 and result["verdict"] == "unknown", result
    assert result["no_verdict"] == (
        "the tests started multiprocessing spawn (a multiprocessing process)" + CANT
    )


def test_a_command_past_its_output_bound_is_no_verdict(project, capsys):
    loud = ["python", "-c", "import sys; sys.stdout.write('x' * 2000000)"]
    code, out = verify(capsys, project, *FIX, "--json", "--check-command", "--", *loud)
    result = json.loads(out.out)
    assert code == 1 and result["output_limit_exceeded"] is True, result
    assert result["verdict"] == "unknown" and result["no_verdict"] == (
        "the check command printed over 1 MiB of output, both streams together, and was stopped"
    )


def test_the_cookbook_lists_the_programs_that_start_no_other():
    import ast

    from peeky.cli import _PYTEST_PLUGIN_TEXT

    text = _PYTEST_PLUGIN_TEXT.format(nonce="n", folder="f")
    leaves = ast.literal_eval(re.search(r"_LEAVES = frozenset\((\{.*?\})\)", text, re.S)[1])
    cookbook = (Path(__file__).resolve().parents[2] / "docs" / "COOKBOOK.md").read_text()
    listed = cookbook.split("The programs that start no other, by their file's real name:")[1]
    assert set(listed.split("```")[1].split()) == leaves
