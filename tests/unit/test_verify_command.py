"""`verify --check-command`: the project's own test command, run on a fresh copy of the
project with the edit applied.

A Python checker was given only the edited file, so it could not run the project's
pytest suite on the candidate, the natural check of a fix.
"""

import json
import os
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
    assert command["argv"] == PYTEST.split() and command["ran"][1:] == PYTEST.split()[1:]
    # `python` is the program's interpreter, the one `python:` names.
    assert Path(command["ran"][0]).resolve() == Path(sys.executable).resolve()
    assert command["copy_of"] == str(project.resolve()) and command["timeout_s"] == 20
    # The root is a project's (pyproject.toml): no note on what is above it.
    assert "warnings" not in result and "candidate" not in result
    code, out = verify(capsys, project, "--check-command", PYTEST, "--baseline")
    assert code == 1
    assert f"command: {PYTEST}\nran: " in out.out and "\ncommand exit: 1\n" in out.out
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
    assert result["reason"] == "the check command ran past 1 s and was stopped"
    code, out = verify(capsys, project, "--check-command", slow, "--baseline")
    # Stopped, it failed for no reason the bug gives.
    assert out.out.rstrip().endswith("baseline: the unedited program, no edit"), out.out
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
        "the check command printed over 1 MiB and was stopped"
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
