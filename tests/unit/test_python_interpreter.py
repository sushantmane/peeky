"""Which interpreter runs a Python program, and Peeky's readings of its code there.

Peeky ran a Python program on the interpreter running `peeky`. Installed as a tool
(pipx, uv), that one can't import the program's own dependencies, from its project's
`.venv`. Now a program runs on `--python`, else the root's virtual environment, else the
active one, else Peeky's own; and what Peeky reads of its code (scopes, stale locals, the
lines `describe` lists) is read by that interpreter, never by another Python version.
"""

import json
import os
import sys
from pathlib import Path

import pytest

from peeky import python_interpreter
from peeky.adapters import PythonAdapter
from peeky.errors import classify
from peeky.python_interpreter import choose, find, identify, readings


def fake_python(path, said, status=0):
    """An interpreter that answers Peeky's question about itself with ``said``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ncat <<'EOF'\n{said}\nEOF\nexit {status}\n")
    path.chmod(0o755)
    return path


def environment(root, name, version="3.12.4"):
    """A virtual environment in ``root``: its `pyvenv.cfg`, and a `bin/python` that says
    it is CPython ``version``."""
    (root / name).mkdir(parents=True)
    (root / name / "pyvenv.cfg").write_text("home = /usr/bin\n")
    major, minor = version.split(".")[:2]
    said = json.dumps(["cpython", version, [int(major), int(minor)]])
    return str(fake_python(root / name / "bin" / "python", said))


@pytest.fixture
def no_active(monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)


def test_a_program_runs_on_its_roots_environment_then_venv(tmp_path, no_active):
    root = tmp_path / "proj"
    root.mkdir()
    assert choose(root) == (sys.executable, "peeky", None)
    venv = environment(root, "venv")
    assert choose(root) == (venv, "venv", None)
    dotted = environment(root, ".venv")
    assert choose(root) == (dotted, ".venv", None)
    found = find(root)
    assert (found.path, found.chosen, found.version) == (dotted, ".venv", "3.12.4")
    assert found.identity() == {
        "interpreter": dotted,
        "version": "3.12.4",
        "chosen": ".venv",
    }


def test_a_directory_without_pyvenv_cfg_is_no_environment(tmp_path, no_active):
    root = tmp_path / "proj"
    fake_python(root / ".venv" / "bin" / "python", "[]")
    assert choose(root) == (sys.executable, "peeky", None)


def test_nothing_above_the_root_is_used_only_named(tmp_path, no_active):
    # The root is what the user named; a directory above it is not the program's.
    above = environment(tmp_path, ".venv")
    root = tmp_path / "proj" / "src"
    root.mkdir(parents=True)
    path, chosen, unused = choose(root)
    assert (path, chosen, unused) == (sys.executable, "peeky", str(tmp_path / ".venv"))
    assert above != path
    shown = python_interpreter.text(find(root).identity())
    assert f"{tmp_path / '.venv'} is above the root, where Peeky does not look" in shown
    assert f"--python {tmp_path / '.venv'}/bin/python uses it" in shown


def test_the_active_environment_comes_after_the_roots(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    active = environment(tmp_path, "active")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "active"))
    assert choose(root) == (active, "VIRTUAL_ENV", None)
    dotted = environment(root, ".venv")
    assert choose(root) == (dotted, ".venv", None)


def test_peekys_own_active_environment_is_peekys_own(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    assert choose(root) == (sys.executable, "peeky", None)


def test_python_option_wins_and_peeky_keeps_its_own(tmp_path, monkeypatch, no_active):
    root = tmp_path / "proj"
    root.mkdir()
    environment(root, ".venv")
    other = environment(tmp_path, "other")
    assert choose(root, other) == (other, "--python", None)
    assert choose(root, "peeky") == (sys.executable, "peeky", None)
    # Relative to where the command runs; a command name is looked for on PATH.
    monkeypatch.chdir(tmp_path)
    assert choose(root, "other/bin/python") == (other, "--python", None)
    monkeypatch.setenv("PATH", str(tmp_path / "other" / "bin"))
    assert choose(root, "python") == (other, "--python", None)
    with pytest.raises(ValueError, match="^--python nowhere/python: no executable file there"):
        choose(root, "nowhere/python")
    try:
        choose(root, "nowhere/python")
    except ValueError as exc:
        assert classify(exc)["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "said, refused",
    [
        (["cpython", "3.9.6", [3, 9]], "is CPython 3.9.6"),
        (["cpython", "3.10.14", [3, 10]], "is CPython 3.10.14"),
        (["pypy", "3.11.9", [3, 11]], "is pypy 3.11.9"),
    ],
)
def test_peeky_reads_only_cpython_3_11_and_later(tmp_path, said, refused):
    python = str(fake_python(tmp_path / "python", json.dumps(said)))
    with pytest.raises(ValueError) as raised:
        identify(python, "--python")
    message = str(raised.value)
    assert message.startswith("Peeky runs Python programs on CPython 3.11 or later, and ")
    assert f"{python} (--python) {refused}" in message
    assert "--python peeky for Peeky's own (Python " in message
    error = classify(raised.value)
    assert error["code"] == "UNSUPPORTED" and len(error["hint"]) <= 150, error


def test_an_interpreter_that_cannot_say_its_version_is_refused(tmp_path):
    python = str(fake_python(tmp_path / "python", "no such thing", status=3))
    with pytest.raises(ValueError, match="could not ask the Python interpreter .* its version"):
        identify(python, ".venv")
    assert (
        identify(
            str(fake_python(tmp_path / "new", json.dumps(["cpython", "3.14.0", [3, 14]]))), ".venv"
        ).version
        == "3.14.0"
    )


def test_an_interpreter_is_asked_without_the_token(tmp_path, monkeypatch):
    seen = tmp_path / "seen"
    python = tmp_path / "python"
    python.write_text(f'#!/bin/sh\nenv > {seen}\necho \'["cpython", "3.12.1", [3, 12]]\'\n')
    python.chmod(0o755)
    monkeypatch.setenv("DEBUG_TOKEN", "worker-side-token-1234567890-0123456789abcdef")
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", "client-side-token-1234567890-0123456789abcdef")
    assert identify(str(python), "--python").version == "3.12.1"
    text = seen.read_text()
    assert "DEBUG_TOKEN" not in text and "PEEKY_WORKER_TOKEN" not in text and "PATH=" in text


def test_the_adapter_runs_debugpy_on_peeky_and_the_program_on_its_interpreter(tmp_path):
    found = python_interpreter.Interpreter("/p/.venv/bin/python", ".venv", "3.12.4")
    adapter = PythonAdapter.on(found)
    # Peeky's own debugpy runs the program on it: the project needs no debugpy.
    assert adapter.command == (sys.executable, "-m", "debugpy.adapter")
    assert adapter.launch_args(tmp_path / "a.py", tmp_path)["python"] == ["/p/.venv/bin/python"]
    assert adapter.target_identity == {
        "language": "python",
        "interpreter": "/p/.venv/bin/python",
        "version": "3.12.4",
        "chosen": ".venv",
    }
    assert "python" not in PythonAdapter().launch_args(tmp_path / "a.py", tmp_path)
    assert PythonAdapter().target_identity is None


SOURCE = """\
LIMIT = 3


def pick(rows):
    out = []
    for row in rows:
        best = [cell for cell in row if cell < LIMIT]
        out.append(best)
    return out


print(pick([[1], [2]]))
"""


def test_readings_in_a_helper_are_the_readings_here(tmp_path, monkeypatch):
    # Another interpreter reads through a helper process: the same readings, as JSON.
    path = tmp_path / "rows.py"
    path.write_text(SOURCE)
    here = readings(sys.executable)
    asked = [
        ("lines", str(path)),
        ("scopes", str(path)),
        ("classify", str(path), "pick", 7, ["LIMIT", "best"], ["out", "row", "rows"], None),
        ("stale_note", str(path), "pick", 7, "best", None, False),
        ("entry_lines", str(path), [8, 12]),
        ("self_contained", "import math\nprint(math.pi)\n"),
    ]
    local = [here(*each) for each in asked]
    from peeky import python_passes

    monkeypatch.setattr(python_passes, "same_compiler", lambda interpreter, timeout=15: False)
    helper = python_interpreter.Readings(sys.executable)
    try:
        assert [helper(*each) for each in asked] == local
        assert helper.process is not None and helper.process.poll() is None
        # A reading's own refusal is a ValueError, as it is here.
        (tmp_path / "bad.py").write_text("def f(:\n")
        with pytest.raises(ValueError, match="does not parse as Python"):
            helper("lines", str(tmp_path / "bad.py"))
    finally:
        helper.close()
    assert local[2] == [{"LIMIT": None, "best": ["local", None]}, None]
    assert local[4] == [[8, [5]], [12, [1]]]


def test_an_interpreter_that_cannot_read_gives_no_reading(tmp_path, monkeypatch):
    # A helper that ends at once: nothing is read, and nothing is claimed.
    from peeky import python_passes

    python = str(fake_python(tmp_path / "python", "[]"))
    monkeypatch.setattr(python_passes, "same_compiler", lambda interpreter, timeout=15: False)
    path = tmp_path / "rows.py"
    path.write_text(SOURCE)
    adapter = PythonAdapter(python)
    frame = {"source": {"path": str(path)}, "name": "pick", "line": 7}
    try:
        proof = adapter.globals_proof(frame, ["out", "row", "rows"], ["LIMIT"])
        assert proof == {
            "unproven": "the program's interpreter could not read its scopes (it ended "
            "(exit status 0))"
        }, proof
        # Given up once: later readings are not asked again.
        assert python_interpreter.readings(python).failure == "it ended (exit status 0)"
        assert adapter.not_yet_declared({"name": "best"}, frame) is None
        with pytest.raises(ValueError, match="could not read rows.py in the program's interp"):
            adapter.entry_lines(path, [7])
    finally:
        python_interpreter.readings(python).close()


def test_peeky_refuses_python_where_it_is_not_used(tmp_path, capsys):
    from peeky.cli import main

    (tmp_path / "main.py").write_text("x = 1\n")
    argv = ["probe", str(tmp_path / "main.py"), "--at", "1", "--python", "peeky", "--json"]
    assert main([*argv, "--worker", "http://127.0.0.1:9"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["message"].startswith("--python is for a program run here; a worker runs ")
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n")
    argv = ["describe", str(tmp_path / "main.c"), "--python", "peeky", "--json"]
    assert main(argv) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["message"] == "--python is for Python programs; this target is c", error


def test_a_worker_refuses_an_interpreter_before_anything_runs(tmp_path, monkeypatch, no_active):
    from peeky.worker import DebugWorker

    (tmp_path / "main.py").write_text("open('RAN', 'w').close()\n")
    environment(tmp_path, ".venv", version="3.9.6")
    worker = DebugWorker(tmp_path)
    with pytest.raises(
        ValueError, match=r"\.venv/bin/python \(the root's \.venv\) is CPython 3\.9"
    ):
        worker.dispatch("launch", {"program": "main.py", "breakpoints": [{"line": 1}]})
    assert worker.dap is None and not (tmp_path / "RAN").exists()


def test_a_probe_says_which_interpreter_ran_the_program():
    from peeky.probe import _table

    python = {"interpreter": "/p/.venv/bin/python", "version": "3.12.4", "chosen": ".venv"}
    result = {
        "status": "completed",
        "hits": 1,
        "seconds": 0.5,
        "rows": [],
        "interventions": [],
        "python": python,
    }
    assert "python: /p/.venv/bin/python, Python 3.12.4 (the root's .venv)" in _table(result)


def test_the_worker_passes_its_python_to_its_probes(tmp_path, monkeypatch):
    import subprocess

    from peeky import worker as worker_module
    from peeky.probe import Probe

    seen = []

    class Started(Exception):
        pass

    def popen(command, *args, **kwargs):
        seen.append(command)
        raise Started

    monkeypatch.setattr(subprocess, "Popen", popen)
    request = Probe("main.py", 1)
    with pytest.raises(Started):
        worker_module._run_probe_process(tmp_path, request, None, 60, python="/p/bin/python")
    with pytest.raises(Started):
        worker_module._run_probe_process(tmp_path, request, None, 60)
    flags = [command[command.index("probe") :] for command in seen]
    assert "--python" in flags[0] and flags[0][flags[0].index("--python") + 1] == "/p/bin/python"
    assert "--python" not in flags[1]


def test_absolute_option_is_read_where_the_worker_runs(tmp_path, monkeypatch):
    other = environment(tmp_path, "other")
    monkeypatch.chdir(tmp_path)
    assert python_interpreter.absolute_option("other/bin/python") == other
    assert python_interpreter.absolute_option("peeky") == "peeky"
    assert os.path.isabs(python_interpreter.absolute_option("other/bin/python"))
    assert Path(other).is_file()
