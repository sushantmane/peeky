"""A breakpoint in an installed package, against real debugpy.

debugpy skips library code by default (`justMyCode`), so a probe at a line of a package
installed in the root's `.venv` said its breakpoint was pending, then ended `NO_STOP`
without saying why. Now debugpy stops there too, and the stop follows the rules of any
other: its readings are the program's interpreter's, and the file is hashed as it ran.
"""

import hashlib
import json

import pytest
from python_packages import TALLY, install

from peeky.cli import main

APP = """\
from tally.outer import twice

print("ran", twice([1, 2]))
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text(APP)
    library = install(root, tmp_path)
    return root, library


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr()


def test_a_breakpoint_in_an_installed_package_stops_there(project, capsys):
    root, library = project
    code, out = run(
        capsys,
        *("probe", root, "--at", f"{library}:4", "--run", "app.py"),
        *("--capture", "step,x,1:doubled", "--hits", "all", "--json", "--no-save"),
    )
    result = json.loads(out.out)
    assert code == 0 and result["status"] == "completed", result
    rows = {(row["hit"], row["selector"]): row for row in result["rows"]}
    # The installed file's own stop, its loop's rules, and its caller in the same package.
    assert rows[1, "x"]["value"] == "2", rows
    assert "value" not in rows[1, "step"], rows[1, "step"]
    assert rows[2, "step"]["value"] == "4", rows[2, "step"]
    assert "this pass's code has not assigned it" in rows[2, "step"]["note"], rows[2, "step"]
    assert (rows[1, "1:doubled"]["function"], rows[1, "1:doubled"]["value"]) == ("twice", "[2, 4]")
    # Run on the root's environment, and the installed file hashed as it ran.
    assert result["python"]["chosen"] == ".venv", result["python"]
    assert result["source_sha256"] == hashlib.sha256(TALLY.encode()).hexdigest()
    assert "ran 12" in result["output"], result["output"]


def test_a_candidate_edit_of_an_installed_file_is_refused(project, capsys):
    root, library = project
    code, out = run(
        capsys,
        *("probe", root, "--at", f"{library}:4", "--run", "app.py", "--capture", "x"),
        *("--old", "acc = 0", "--new", "acc = 10", "--json", "--no-save"),
    )
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "INVALID_REQUEST", error
    assert f"cannot edit {library}: it is in " in error["message"], error
    assert "would change your installed file" in error["message"], error
    assert (root / library).read_text() == TALLY


def test_a_file_debugpy_skips_as_library_code_is_refused_before_it_runs(
    project, tmp_path, capsys, monkeypatch
):
    # debugpy's own setting: everything under the root is library code. The refusal said
    # nothing of the program's had run, but the interpreter's startup had: a `.pth` file
    # of its environment (project code) ran in the process debugpy started.
    root, library = project
    marks = tmp_path / "marks.txt"
    (root / library).parent.parent.joinpath("peeky_marks.pth").write_text(
        f"import sys; open({str(marks)!r}, 'a').write(' '.join(sys.argv) + '\\n')\n"
    )
    monkeypatch.setenv("LIBRARY_ROOTS", str(root.resolve()))
    code, out = run(
        capsys, "probe", root, "--at", "app.py:3", "--capture", "twice", "--json", "--no-save"
    )
    result = json.loads(out.out)
    message = result["error"]["message"]
    assert code == 1 and result["error"]["code"] == "INVALID_BREAKPOINT", result
    assert "A breakpoint in app.py would never stop: debugpy counts" in message
    assert (
        "Refused before your script or module started; the interpreter's startup (.pth "
        "files, sitecustomize) had already run" in message
    ), message
    assert "Probe the code that calls it" in result["error"]["hint"]
    assert "ran" not in (result.get("output") or ""), result["output"]
    # The program's own interpreter, which debugpy started, ran the `.pth` file.
    started = [line for line in marks.read_text().splitlines() if "debugpy" in line]
    assert started, marks.read_text()


def test_installed_is_what_the_programs_interpreter_says(project, tmp_path):
    # A path component named site-packages decided it: an ordinary directory of that
    # name was installed code. Now the program's interpreter says (#439's choice).
    from python_packages import make_env

    from peeky.cli import python_adapter

    root, library = project
    (root / "site-packages").mkdir()
    (root / "site-packages" / "tool.py").write_text("x = 1\n")
    # An editable install: its files stay in the project, which a `.pth` file names.
    (root / "src" / "edit").mkdir(parents=True)
    (root / "src" / "edit" / "__init__.py").write_text("x = 1\n")
    (root / library).parent.parent.joinpath("edit.pth").write_text(f"{root / 'src'}\n")
    chosen = python_adapter(root)
    other = python_adapter(root, str(make_env(tmp_path / "other")))
    try:
        assert chosen.identity["chosen"] == ".venv"
        assert chosen.installed(root / library)
        assert not chosen.installed(root / "site-packages" / "tool.py")
        assert not chosen.installed(root / "src" / "edit" / "__init__.py")
        # Another environment's interpreter (--python) did not install it.
        assert not other.installed(root / library)
    finally:
        chosen.close()
        other.close()


def test_outside_the_target_is_installed_only_where_the_interpreter_says(project, tmp_path, capsys):
    root, library = project
    other = tmp_path / "other"
    other.mkdir()
    (other / "main.py").write_text("print(1)\n")
    (tmp_path / "work" / "site-packages").mkdir(parents=True)
    (tmp_path / "work" / "site-packages" / "tool.py").write_text("x = 1\n")
    probe = ["probe", other, "--json", "--no-save", "--at"]
    python = ["--python", root / ".venv" / "bin" / "python"]
    code, out = run(capsys, *probe, f"{root / library}:4", *python)
    error = json.loads(out.out)["error"]
    assert code == 2 and "is installed code outside it" in error["message"], error
    assert "install the project editable (`pip install -e .`)" in error["message"], error
    # An ordinary directory named site-packages, and a file the program's interpreter
    # (here Peeky's own) did not install: outside the target, and nothing more is said.
    for at in (f"{tmp_path / 'work' / 'site-packages' / 'tool.py'}:1", f"{root / library}:4"):
        code, out = run(capsys, *probe, at, *(python if "work" in at else ()))
        error = json.loads(out.out)["error"]
        assert code == 2 and error["message"].endswith("Give a target directory that holds it")
