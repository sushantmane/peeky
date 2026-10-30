"""A Python program runs on its project's interpreter, against real debugpy.

Installed as a tool (pipx, uv), Peeky ran a program on its own interpreter, which can't
import what the program's project installs in its `.venv`: the program failed with
`ModuleNotFoundError`. Now it runs on the root's virtual environment, or on `--python`,
and Peeky reads its code (stale locals, scopes, `describe`'s lines) with that interpreter.
"""

import glob
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from peeky.cli import main

APP = """\
import greet


def main():
    word = greet.hello("ada")
    print(word)


main()
"""
CHECK = """\
import runpy
import sys

import greet

runpy.run_path(sys.argv[1], run_name="__main__")
assert greet.hello("bob") == "hello bob"
"""
TOKEN = "python-interpreter-token-1234567890abcdef"


def make_venv(where, base=None):
    """A virtual environment at ``where``, made by ``base`` (this Python's base), with no
    pip: nothing is downloaded."""
    subprocess.run(
        [str(base or sys.executable), "-m", "venv", "--without-pip", str(where)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return where / "bin" / "python"


def site_packages(python):
    return Path(
        subprocess.run(
            [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
    )


def install_greet(python, lib):
    """`greet` importable by ``python`` only: a `.pth` file in its site-packages naming
    ``lib``, outside any project root."""
    (site_packages(python) / "greet.pth").write_text(f"{lib}\n")


@pytest.fixture
def lib(tmp_path):
    package = tmp_path / "lib" / "greet"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("def hello(name):\n    return 'hello ' + name\n")
    return tmp_path / "lib"


@pytest.fixture
def project(tmp_path, lib, monkeypatch):
    """A project whose program imports `greet`, which only its `.venv` installs."""
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text(APP)
    python = make_venv(root / ".venv")
    install_greet(python, lib)
    return root, python


def compiled_lines(text, optimize):
    """The lines this interpreter (the program's in these tests) gives code at
    ``optimize``: they differ by version."""
    lines, pending = set(), [compile(text, "<t>", "exec", dont_inherit=True, optimize=optimize)]
    while pending:
        code = pending.pop()
        lines.update(line for _, _, line in code.co_lines() if line)
        pending.extend(each for each in code.co_consts if hasattr(each, "co_lines"))
    return sorted(lines)


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr()


def test_a_probe_imports_what_the_projects_environment_installs(project, capsys):
    root, python = project
    argv = ["probe", root / "app.py", "--at", "6", "--capture", "word", "--no-save"]
    code, out = run(capsys, *argv, "--json")
    result = json.loads(out.out)
    assert "ModuleNotFoundError" not in (result.get("output") or ""), result.get("output")
    assert code == 0 and result["status"] == "completed", result
    assert result["rows"][0].get("value") == "'hello ada'", result
    assert result["python"]["interpreter"] == str(python), result["python"]
    assert result["python"]["chosen"] == ".venv"
    assert result["python"]["version"] == ".".join(map(str, sys.version_info[:3]))
    assert result["target_identity"] == {"language": "python", **result["python"]}
    code, out = run(capsys, *argv, "--quiet")
    assert code == 0 and f"python: {python}, Python " in out.out, out.out
    assert "(the root's .venv)" in out.out
    # `--python peeky` keeps Peeky's own, which can't import it.
    code, out = run(capsys, *argv, "--python", "peeky", "--json")
    result = json.loads(out.out)
    assert "ModuleNotFoundError: No module named 'greet'" in result["output"], result
    assert result["python"]["interpreter"] == sys.executable
    assert result["python"]["chosen"] == "peeky"


def test_python_names_an_interpreter_outside_the_root(tmp_path, lib, capsys, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    python = make_venv(tmp_path / "envs" / "app")
    install_greet(python, lib)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text(APP)
    argv = ["probe", root / "app.py", "--at", "6", "--capture", "word", "--no-save", "--json"]
    code, out = run(capsys, *argv, "--python", python)
    result = json.loads(out.out)
    assert code == 0 and result["rows"][0]["value"] == "'hello ada'", result
    assert result["python"]["chosen"] == "--python"
    assert result["python"]["interpreter"] == str(python)


def test_a_session_runs_on_the_projects_interpreter(project, capsys):
    root, python = project
    code, out = run(capsys, "session", "start", root / "app.py", "--at", "5", "--name", "venv")
    try:
        assert code == 0 and f"python: {python}, Python " in out.out, out
        code, out = run(capsys, "session", "step", "--name", "venv")
        assert code == 0, out
        code, out = run(capsys, "session", "get", "word", "--name", "venv")
        assert code == 0 and "word: str = 'hello ada'" in out.out, out
    finally:
        run(capsys, "session", "close", "--name", "venv")


def test_verify_checks_on_the_projects_interpreter(project, tmp_path, capsys):
    root, python = project
    check = tmp_path / "check.py"
    check.write_text(CHECK)
    argv = ["verify", root / "app.py", "--check", check, "--json"]
    code, out = run(capsys, *argv, "--old", '"ada"', "--new", '"ana"')
    result = json.loads(out.out)
    assert code == 0 and result["passed"] is True, result
    assert result["python"]["interpreter"] == str(python)


def test_describe_reads_with_the_projects_interpreter(project, capsys):
    root, python = project
    code, out = run(capsys, "describe", root / "app.py", "--json")
    manifest = json.loads(out.out)
    assert code == 0 and manifest["python"]["chosen"] == ".venv", manifest
    assert manifest["breakpoint_lines"] == {"app.py": compiled_lines(APP, 0)}
    assert {1, 5, 6, 9} <= set(manifest["breakpoint_lines"]["app.py"])


def test_a_worker_runs_its_roots_environment(tmp_path, lib, capsys, monkeypatch):
    host, local = tmp_path / "host", tmp_path / "local"
    for root in (host, local):
        root.mkdir()
        (root / "app.py").write_text(APP)
    python = make_venv(host / ".venv")
    install_greet(python, lib)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", TOKEN)
    env = {**os.environ, "DEBUG_TOKEN": TOKEN, "PEEKY_STATE_DIR": str(tmp_path / "worker")}
    env.pop("PEEKY_WORKER_TOKEN", None)
    env.pop("VIRTUAL_ENV", None)
    command = [sys.executable, "-B", "-m", "peeky.worker", "--root", str(host), "--port", "0"]
    worker = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        banner = worker.stdout.readline()
        url = f"http://127.0.0.1:{int(banner.rsplit(':', 1)[1].split(';')[0])}"
        argv = ["probe", local / "app.py", "--at", "6", "--capture", "word", "--json"]
        code, out = run(capsys, *argv, "--worker", url)
        result = json.loads(out.out)
        assert code == 0 and result["rows"][0]["value"] == "'hello ada'", result
        assert result["python"]["interpreter"] == str(python), result["python"]
        assert result["python"]["chosen"] == ".venv"
        # Its interpreter is the worker's to choose.
        code, out = run(capsys, *argv, "--worker", url, "--python", python)
        assert code == 2 and "--python is for a program run here" in out.out, out
        code, out = run(capsys, "session", "start", local / "app.py", "--at", "5", "--worker", url)
        assert code == 0 and f"python: {python}, Python " in out.out, out
        run(capsys, "session", "close")
    finally:
        os.killpg(worker.pid, signal.SIGTERM)
        worker.wait(timeout=10)


def _other_python():
    """A CPython 3.11 or later of another minor version than this one, or None."""
    own = f"{sys.version_info[0]}.{sys.version_info[1]}"
    found = [shutil.which(f"python3.{minor}") for minor in range(11, 16)]
    found += sorted(
        glob.glob(os.path.expanduser("~/.local/share/uv/python/cpython-3.*/bin/python3.*"))
    )
    for path in found:
        if not path or path.endswith(("-config", own)) or not os.access(path, os.X_OK):
            continue
        # The interpreter itself: a venv made through a link elsewhere (`~/.local/bin`)
        # may not find its standard library.
        path = os.path.realpath(path)
        said = subprocess.run(
            [path, "-I", "-c", "import sys; print(sys.implementation.name, *sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.split()
        if (
            said[:1] == ["cpython"]
            and (3, 11) <= (int(said[1]), int(said[2])) != sys.version_info[:2]
        ):
            return path, (int(said[1]), int(said[2]))
    return None


ROWS = """\
def pick(rows):
    out = []
    for row in rows:
        best = [cell for cell in row]
        out.append(best)
    return out


print(pick([[1], [2]]))
"""
GENERIC = "def first[T](items: list[T]) -> T:\n    return items[0]\n\n\nprint(first([1]))\n"


def test_peeky_reads_a_program_with_its_own_interpreters_version(tmp_path, capsys, monkeypatch):
    # Python 3.11 compiles `[cell for cell in row]` to a function of its own, called on
    # line 4; 3.12 and later run it inline, a loop inside the line. So at line 4 of a
    # second pass, 3.11's code has not yet assigned `best`, which 3.12's may have. A
    # reading made by Peeky's own Python would be the other version's.
    other = _other_python()
    if other is None:
        pytest.skip("no CPython 3.11 or later of another version than Peeky's")
    base, version = other
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "rows.py").write_text(ROWS)
    (root / "generic.py").write_text(GENERIC)
    python = make_venv(root / ".venv", base)
    argv = ["probe", root / "rows.py", "--at", "4", "--capture", "best", "--hits", "all"]
    code, out = run(capsys, *argv, "--no-save", "--json")
    result = json.loads(out.out)
    held = [row for row in result["rows"] if row["function"] == "pick" and "value" in row]
    assert code == 0 and held, result
    noted = [bool(row.get("note")) for row in held]
    # Inline from 3.12 on: no note. Its own function on 3.11: the earlier pass's value.
    assert noted == [version < (3, 12)] * len(held), (version, held)
    assert result["python"]["interpreter"] == str(python), result
    assert result["python"]["version"].startswith(f"{version[0]}.{version[1]}.")
    # Peeky's own Python (3.12 or later reads no note there) for the same program.
    code, out = run(capsys, *argv, "--no-save", "--json", "--python", "peeky")
    own = json.loads(out.out)
    assert not any(row.get("note") for row in own["rows"]), own["rows"]
    # `describe` parses with the program's interpreter: 3.11 has no `def f[T]`.
    code, out = run(capsys, "describe", root / "generic.py", "--json")
    described = json.loads(out.out)
    if version < (3, 12):
        assert code == 2 and "generic.py does not parse as Python" in described["error"]["message"]
    else:
        assert code == 0 and described["breakpoint_lines"]["generic.py"], described
    # The .venv made again at the same path, now with Peeky's own Python: this launch reads
    # with it, not with the earlier launch's interpreter or verdict.
    shutil.rmtree(root / ".venv")
    python = make_venv(root / ".venv")
    code, out = run(capsys, *argv, "--no-save", "--json")
    again = json.loads(out.out)
    assert code == 0 and again["python"]["version"] == own_version(), again["python"]
    assert not any(row.get("note") for row in again["rows"]), again["rows"]
    # And back to the other version: its reading again.
    shutil.rmtree(root / ".venv")
    python = make_venv(root / ".venv", base)
    code, out = run(capsys, *argv, "--no-save", "--json")
    back = json.loads(out.out)
    held = [row for row in back["rows"] if row["function"] == "pick" and "value" in row]
    assert [bool(row.get("note")) for row in held] == [version < (3, 12)] * len(held), held


def own_version():
    return ".".join(map(str, sys.version_info[:3]))


OPTIMIZED = """\
def check(total):
    \"\"\"Checked in development only.\"\"\"
    assert total < 0, "a total is never negative"
    return total


print(check(3))
"""


def test_peeky_reads_the_program_at_its_optimization_level(project, capsys, monkeypatch):
    # PYTHONOPTIMIZE reaches the program, which leaves out the `assert`, and every
    # reading of its code is made at that level, not at Peeky's own.
    root, python = project
    (root / "opt.py").write_text(OPTIMIZED)
    code, out = run(capsys, "describe", root / "opt.py", "--json")
    assert json.loads(out.out)["breakpoint_lines"] == {"opt.py": compiled_lines(OPTIMIZED, 0)}
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    code, out = run(capsys, "describe", root / "opt.py", "--json")
    manifest = json.loads(out.out)
    assert manifest["breakpoint_lines"] == {"opt.py": compiled_lines(OPTIMIZED, 1)}, manifest
    assert 3 not in manifest["breakpoint_lines"]["opt.py"]
    assert manifest["python"]["optimize"] == 1
    argv = ["probe", root / "opt.py", "--at", "4", "--capture", "total", "--no-save"]
    code, out = run(capsys, *argv, "--json")
    result = json.loads(out.out)
    # The assert would fail: it is left out, as Peeky said.
    assert code == 0 and result["rows"][0]["value"] == "3", result
    assert result["python"]["optimize"] == 1 and result["target_identity"]["optimize"] == 1
    code, out = run(capsys, *argv, "--quiet")
    assert f"python: {python}, Python {own_version()} -O (the root's .venv)" in out.out, out.out


def test_an_interpreter_too_old_for_peeky_is_refused_before_anything_runs(
    tmp_path, capsys, monkeypatch
):
    old = "/usr/bin/python3"
    said = (
        subprocess.run(
            [old, "-c", "import sys; print(*sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if os.access(old, os.X_OK)
        else None
    )
    if said is None or said.returncode or tuple(map(int, said.stdout.split())) >= (3, 11):
        pytest.skip("no Python older than 3.11 at /usr/bin/python3")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "app.py").write_text("open('RAN', 'w').close()\nx = 1\n")
    argv = ["probe", tmp_path / "app.py", "--at", "2", "--capture", "x", "--json"]
    code, out = run(capsys, *argv, "--python", old)
    result = json.loads(out.out)
    assert code == 2 and result["refused_before_launch"] is True, result
    assert result["error"]["code"] == "UNSUPPORTED"
    assert f"{old} (--python) is CPython {said.stdout.split()[0]}." in result["error"]["message"]
    assert not (tmp_path / "RAN").exists()


def test_a_shim_runs_the_python_it_picks_where_debugpy_starts_the_program(
    tmp_path, capsys, monkeypatch
):
    # A pyenv-like shim picks its Python from the directory it runs in. Peeky asked it
    # in its own directory, and debugpy then ran it in the root: another Python ran the
    # program than the one Peeky read its code with.
    other = _other_python()
    if other is None:
        pytest.skip("no CPython 3.11 or later of another version than Peeky's")
    base, version = other
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    shim = tmp_path / "shims" / "python"
    shim.parent.mkdir()
    shim.write_text(
        f'#!/bin/sh\nif grep -qs other .python-version; then exec "{base}" "$@"; fi\n'
        f'exec "{sys.executable}" "$@"\n'
    )
    shim.chmod(0o755)
    root, elsewhere = tmp_path / "proj", tmp_path / "elsewhere"
    root.mkdir()
    elsewhere.mkdir()
    (root / "rows.py").write_text(ROWS)
    (root / ".python-version").write_text("other\n")
    monkeypatch.chdir(elsewhere)
    argv = ["probe", root / "rows.py", "--at", "4", "--capture", "best", "--hits", "all"]
    code, out = run(capsys, *argv, "--python", shim, "--no-save", "--json")
    result = json.loads(out.out)
    held = [row for row in result["rows"] if row["function"] == "pick" and "value" in row]
    assert code == 0 and held, result
    assert [bool(row.get("note")) for row in held] == [version < (3, 12)] * len(held), held
    assert result["python"]["version"].startswith(f"{version[0]}.{version[1]}.")
    # debugpy runs the file the shim picked, not the shim.
    assert os.path.samefile(result["python"]["executable"], base), result["python"]


NOISY = (
    "import sys, atexit; sys.stdout.write('pth says hi'); "
    "atexit.register(sys.stdout.write, 'bye from atexit')\n"
)


def test_an_environment_that_prints_at_startup_and_exit_is_read(project, capsys, monkeypatch):
    # A `.pth` file's output with no newline, and an exit handler's: Peeky took the last
    # line its interpreter printed for its answer.
    root, python = project
    (site_packages(python) / "noise.pth").write_text(NOISY)
    (root / "opt.py").write_text(OPTIMIZED)
    # Read by a helper too: the program's level is not Peeky's.
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    code, out = run(capsys, "describe", root / "opt.py", "--json")
    manifest = json.loads(out.out)
    assert code == 0, manifest
    assert manifest["breakpoint_lines"] == {"opt.py": compiled_lines(OPTIMIZED, 1)}, manifest
    argv = ["probe", root / "app.py", "--at", "6", "--capture", "word", "--no-save", "--json"]
    code, out = run(capsys, *argv)
    result = json.loads(out.out)
    assert code == 0 and result["rows"][0]["value"] == "'hello ada'", result
    assert "pth says hi" in result["output"]


def test_a_wrapper_that_adds_options_is_refused_before_anything_runs(tmp_path, capsys, monkeypatch):
    # `exec python -O "$@"`: debugpy runs the file the wrapper starts, without its -O, and
    # Peeky read the program's code at -O.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    python = os.path.realpath(sys.executable)
    wrapper = tmp_path / "python"
    wrapper.write_text(f'#!/bin/sh\nexec "{python}" -O "$@"\n')
    wrapper.chmod(0o755)
    (tmp_path / "opt.py").write_text("open('RAN', 'w').close()\n" + OPTIMIZED)
    argv = ["probe", tmp_path / "opt.py", "--at", "5", "--capture", "total", "--json"]
    code, out = run(capsys, *argv, "--python", wrapper)
    result = json.loads(out.out)
    assert code == 2 and result["refused_before_launch"] is True, result
    assert "is a wrapper that changes how Python runs" in result["error"]["message"]
    assert "sys.flags.optimize 1, not 0" in result["error"]["message"]
    assert not (tmp_path / "RAN").exists()
