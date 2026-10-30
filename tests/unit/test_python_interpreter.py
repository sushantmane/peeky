"""Which interpreter runs a Python program, and Peeky's readings of its code there.

Peeky ran a Python program on the interpreter running `peeky`. Installed as a tool
(pipx, uv), that one can't import the program's own dependencies, from its project's
`.venv`. Now a program runs on `--python`, else the root's virtual environment, else the
active one, else Peeky's own; and what Peeky reads of its code (scopes, stale locals, the
lines `describe` lists, the code a line's frames are named by) is read by that
interpreter, at the optimization level the program runs at, never by another Python.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from peeky import python_interpreter
from peeky.adapters import PythonAdapter
from peeky.errors import classify
from peeky.python_interpreter import (
    Interpreter,
    Readings,
    Unavailable,
    choose,
    find,
    identify,
    own_compiler,
)

SRC = Path(__file__).resolve().parents[2] / "src"


def fake_python(path, version="3.12.4", implementation="cpython", optimize=0, **said):
    """An interpreter that answers Peeky's question about itself as CPython ``version``
    would (``said["compiler"]``: what it reports as its compiler), or with ``said["text"]``
    and ``said["status"]``; it runs nothing else."""
    major, minor = (int(part) for part in version.split(".")[:2])
    compiler = said.get("compiler") or [
        implementation,
        f"{version} (fake)",
        "",
        f"cpython-{major}{minor}",
        "00000d0a",
    ]
    text = said.get("text") or repr(
        [implementation, version, [major, minor], compiler[1], *compiler[2:], optimize]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ncat <<'EOF'\n{text}\nEOF\nexit {said.get('status', 0)}\n")
    path.chmod(0o755)
    return path


def environment(root, name, **said):
    """A virtual environment in ``root``: its `pyvenv.cfg`, and a fake `bin/python`."""
    (root / name).mkdir(parents=True)
    (root / name / "pyvenv.cfg").write_text("home = /usr/bin\n")
    return str(fake_python(root / name / "bin" / "python", **said))


@pytest.fixture
def no_active(monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("PYTHONOPTIMIZE", raising=False)


@pytest.fixture
def helper(monkeypatch):
    """Readings of this interpreter made in a helper process, as another one's are."""
    monkeypatch.setattr(Interpreter, "same", property(lambda self: False))


def test_a_program_runs_on_its_roots_environment_then_venv(tmp_path, no_active):
    root = tmp_path / "proj"
    root.mkdir()
    assert choose(root) == (sys.executable, "peeky", None, None)
    venv = environment(root, "venv")
    assert choose(root) == (venv, "venv", None, None)
    dotted = environment(root, ".venv")
    assert choose(root) == (dotted, ".venv", None, None)
    found = find(root)
    assert (found.path, found.chosen, found.version, found.optimize) == (
        dotted,
        ".venv",
        "3.12.4",
        0,
    )
    assert found.identity() == {
        "interpreter": dotted,
        "version": "3.12.4",
        "optimize": 0,
        "chosen": ".venv",
    }


def test_a_directory_without_pyvenv_cfg_is_no_environment(tmp_path, no_active):
    root = tmp_path / "proj"
    fake_python(root / ".venv" / "bin" / "python")
    assert choose(root) == (sys.executable, "peeky", None, None)


def test_nothing_above_the_root_is_used_only_named(tmp_path, no_active):
    # The root is what the user named; a directory above it is not the program's.
    environment(tmp_path, ".venv")
    root = tmp_path / "proj" / "src"
    root.mkdir(parents=True)
    assert choose(root) == (sys.executable, "peeky", str(tmp_path / ".venv"), None)
    shown = python_interpreter.text(find(root).identity())
    assert f"{tmp_path / '.venv'} is above the root, where Peeky does not look" in shown
    assert f"--python {tmp_path / '.venv'}/bin/python uses it" in shown


def test_peekys_own_environment_above_the_root_is_not_called_unused(
    tmp_path, monkeypatch, no_active
):
    # Peeky run from a checkout's .venv, probing a file in the checkout's src/: that
    # environment is the one running the program, not one Peeky left unused.
    environment(tmp_path, ".venv")
    root = tmp_path / "src"
    root.mkdir()
    monkeypatch.setattr(sys, "prefix", str(tmp_path / ".venv"))
    assert choose(root) == (sys.executable, "peeky", None, str(tmp_path / ".venv"))
    identity = find(root).identity()
    assert identity["environment"] == str(tmp_path / ".venv") and "unused" not in identity
    shown = python_interpreter.text(identity)
    assert f"(Peeky's own, the virtual environment {tmp_path / '.venv'})" in shown, shown
    assert "above the root" not in shown


def test_the_active_environment_comes_after_the_roots(tmp_path, monkeypatch, no_active):
    root = tmp_path / "proj"
    root.mkdir()
    active = environment(tmp_path, "active")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "active"))
    assert choose(root) == (active, "VIRTUAL_ENV", None, None)
    dotted = environment(root, ".venv")
    assert choose(root) == (dotted, ".venv", None, None)


def test_peekys_own_active_environment_is_peekys_own(tmp_path, monkeypatch, no_active):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    assert choose(root)[:2] == (sys.executable, "peeky")


def test_python_option_wins_and_peeky_keeps_its_own(tmp_path, monkeypatch, no_active):
    root = tmp_path / "proj"
    root.mkdir()
    environment(root, ".venv")
    other = environment(tmp_path, "other")
    assert choose(root, other) == (other, "--python", None, None)
    assert choose(root, "peeky") == (sys.executable, "peeky", None, None)
    # Relative to where the command runs; a command name is looked for on PATH.
    monkeypatch.chdir(tmp_path)
    assert choose(root, "other/bin/python")[:2] == (other, "--python")
    monkeypatch.setenv("PATH", str(tmp_path / "other" / "bin"))
    assert choose(root, "python")[:2] == (other, "--python")
    with pytest.raises(ValueError, match="^--python nowhere/python: no executable file there") as e:
        choose(root, "nowhere/python")
    assert classify(e.value)["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "version, implementation, refused",
    [
        ("3.9.6", "cpython", "is CPython 3.9.6"),
        ("3.10.14", "cpython", "is CPython 3.10.14"),
        ("3.11.9", "pypy", "is pypy 3.11.9"),
    ],
)
def test_peeky_reads_only_cpython_3_11_and_later(tmp_path, version, implementation, refused):
    python = str(fake_python(tmp_path / "python", version, implementation))
    with pytest.raises(ValueError) as raised:
        identify(python, "--python")
    message = str(raised.value)
    assert message.startswith("Peeky runs Python programs on CPython 3.11 or later, and ")
    assert f"{python} (--python) {refused}" in message
    assert "--python peeky for Peeky's own (Python " in message
    error = classify(raised.value)
    assert error["code"] == "UNSUPPORTED" and len(error["hint"]) <= 150, error


def test_an_interpreter_that_cannot_say_its_version_is_refused(tmp_path):
    python = str(fake_python(tmp_path / "python", text="no such thing", status=3))
    with pytest.raises(ValueError, match="could not ask the Python interpreter .* its version"):
        identify(python, ".venv")
    assert identify(str(fake_python(tmp_path / "new", "3.14.0")), ".venv").version == "3.14.0"


def test_an_interpreter_is_asked_as_the_program_runs_without_the_token(tmp_path, monkeypatch):
    seen = tmp_path / "seen"
    python = tmp_path / "python"
    said = repr(["cpython", "3.12.1", [3, 12], "3.12.1 (x)", "", "cpython-312", "ab", 1])
    python.write_text(f'#!/bin/sh\necho "$@" > {seen}.args\nenv > {seen}\necho "{said}"\n')
    python.chmod(0o755)
    monkeypatch.setenv("DEBUG_TOKEN", "worker-side-token-1234567890-0123456789abcdef")
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", "client-side-token-1234567890-0123456789abcdef")
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    assert identify(str(python), "--python").optimize == 1
    text = seen.read_text()
    assert "DEBUG_TOKEN" not in text and "PEEKY_WORKER_TOKEN" not in text and "PATH=" in text
    # Not isolated: the program is not, and PYTHONOPTIMIZE reaches it.
    assert "PYTHONOPTIMIZE=1" in text and "PYTHONDONTWRITEBYTECODE=1" in text
    assert not Path(f"{seen}.args").read_text().startswith("-I")


def test_the_adapter_runs_debugpy_on_peeky_and_the_program_on_its_interpreter(tmp_path):
    found = Interpreter("/p/.venv/bin/python", ".venv", "3.12.4", 2)
    adapter = PythonAdapter.on(found)
    # Peeky's own debugpy runs the program on it: the project needs no debugpy.
    assert adapter.command == (sys.executable, "-m", "debugpy.adapter")
    assert adapter.launch_args(tmp_path / "a.py", tmp_path)["python"] == ["/p/.venv/bin/python"]
    assert adapter.target_identity == {
        "language": "python",
        "interpreter": "/p/.venv/bin/python",
        "version": "3.12.4",
        "optimize": 2,
        "chosen": ".venv",
    }
    assert "python" not in PythonAdapter().launch_args(tmp_path / "a.py", tmp_path)
    assert PythonAdapter().target_identity is None
    shown = python_interpreter.text(adapter.target_identity)
    assert shown == "/p/.venv/bin/python, Python 3.12.4 -OO (the root's .venv)"


SOURCE = """\
LIMIT = 3


def pick(rows):
    out = []
    for row in rows:
        best = [cell for cell in row if cell < LIMIT]
        out.append(best)
    return out


f = lambda v: v + 1; g = lambda v: v * 2  # noqa: E702, E731
print(pick([[1], [2]]))
"""


def asked(path):
    return [
        ("lines", str(path)),
        ("scopes", str(path)),
        ("classify", str(path), "pick", 7, ["LIMIT", "best"], ["out", "row", "rows"], None),
        ("stale_note", str(path), "pick", 7, "best", None, False),
        ("entry_lines", str(path), [8, 13]),
        ("self_contained", "import math\nprint(math.pi)\n"),
        ("anonymous", SOURCE, [12]),
    ]


def test_readings_in_a_helper_are_the_readings_here(tmp_path, monkeypatch, no_active):
    # Another interpreter reads through a helper process: the same readings, as JSON.
    path = tmp_path / "rows.py"
    path.write_text(SOURCE)
    here = Readings(sys.executable)
    local = [here(*each) for each in asked(path)]
    here.close()
    monkeypatch.setattr(Interpreter, "same", property(lambda self: False))
    helper = Readings(sys.executable)
    try:
        assert [helper(*each) for each in asked(path)] == local
        assert helper.process is not None and helper.process.poll() is None
        # A reading's own refusal is a ValueError, as it is here.
        (tmp_path / "bad.py").write_text("def f(:\n")
        with pytest.raises(ValueError, match="does not parse as Python"):
            helper("lines", str(tmp_path / "bad.py"))
    finally:
        helper.close()
    assert local[2] == [{"LIMIT": None, "best": ["local", None]}, None]
    assert local[4] == [[8, [5]], [13, [1]]]
    assert local[6] == [[12, {"<lambda>": 2}]]


OPTIMIZED = 'def f(x):\n    """Doc."""\n    assert x\n    return x\n\n\nprint(f(1))\n'


@pytest.mark.parametrize("level, lines", [("", [1, 3, 4, 7]), ("1", [1, 4, 7]), ("2", [1, 4, 7])])
def test_readings_are_made_at_the_programs_optimization_level(
    tmp_path, monkeypatch, no_active, level, lines
):
    # PYTHONOPTIMIZE reaches the program, which then leaves out an `assert`; the helper
    # runs isolated (-I), which ignored it, and so did Peeky's own compile.
    path = tmp_path / "opt.py"
    path.write_text(OPTIMIZED)
    if level:
        monkeypatch.setenv("PYTHONOPTIMIZE", level)
    found = find(tmp_path)
    assert found.optimize == int(level or 0)
    adapter = PythonAdapter.on(found)
    try:
        assert adapter.readings("lines", str(path)) == lines
        monkeypatch.setattr(Interpreter, "same", property(lambda self: False))
        assert adapter.readings("lines", str(path)) == lines
    finally:
        adapter.close()
    if level:
        # A probe's restore watches a function's first line: one with no code is refused.
        with pytest.raises(ValueError, match=r"line 3\) has no code at the program's optim"):
            adapter.entry_lines(path, [4])
    else:
        assert adapter.entry_lines(path, [4]) == {4: [3]}
    adapter.close()


def test_peekys_own_optimization_is_not_the_programs(tmp_path, no_active):
    # Peeky started with -O, the program without: the program keeps its asserts, and
    # describe lists their lines (Peeky's own compile left them out).
    path = tmp_path / "opt.py"
    path.write_text(OPTIMIZED)
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONOPTIMIZE", None)
    env["PEEKY_STATE_DIR"] = str(tmp_path / "state")
    ran = subprocess.run(
        [sys.executable, "-O", "-m", "peeky.cli", "describe", str(path), "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=tmp_path,
    )
    manifest = json.loads(ran.stdout)
    assert manifest["breakpoint_lines"] == {"opt.py": [1, 3, 4, 7]}, manifest
    assert manifest["python"]["optimize"] == 0


def test_a_launch_reads_with_the_interpreter_it_identified(tmp_path, no_active):
    # A .venv made again at the same path with another Python: the earlier launch's
    # verdict (this Python compiles alike) is not the new one's, and the earlier
    # launch reads nothing with the new file.
    path = tmp_path / "rows.py"
    path.write_text(SOURCE)
    own = own_compiler()
    environment(tmp_path, ".venv", version=own_compiler_version(), compiler=own)
    first = PythonAdapter.on(find(tmp_path))
    try:
        assert first.readings("lines", str(path)) == Readings(sys.executable)("lines", str(path))
        venv = tmp_path / ".venv" / "bin" / "python"
        venv.unlink()
        time.sleep(0.01)
        fake_python(venv, "3.12.4")
        second = PythonAdapter.on(find(tmp_path))
        assert second.target_identity["version"] == "3.12.4"
        # The new file is another compiler: read by its helper, which this fake can't run.
        with pytest.raises(Unavailable, match="it ended|is not the interpreter"):
            second.readings("lines", str(path))
        with pytest.raises(Unavailable, match="is not the interpreter Peeky asked at launch"):
            first.readings("lines", str(path))
    finally:
        first.close()


def own_compiler_version():
    return ".".join(map(str, sys.version_info[:3]))


def test_a_failed_reading_is_not_remembered(tmp_path, helper, no_active):
    path = tmp_path / "rows.py"
    path.write_text(SOURCE)
    readings = Readings(sys.executable)
    try:
        assert readings("lines", str(path))
        readings.process.kill()
        with pytest.raises(Unavailable, match="it ended"):
            readings("lines", str(path))
        # The next reading asks again, with a new helper.
        assert readings("lines", str(path)) and readings.process.poll() is None
    finally:
        readings.close()


def test_a_launchs_helper_ends_with_it(tmp_path, helper, no_active):
    path = tmp_path / "rows.py"
    path.write_text(SOURCE)
    adapter = PythonAdapter.on(find(tmp_path))
    assert adapter.readings("lines", str(path))
    process = adapter.readings.process
    adapter.close()
    assert process.wait(timeout=5) is not None and adapter.readings.process is None


def test_a_helper_is_bounded_by_the_readings_deadline(tmp_path, no_active):
    # One that says its compiler, then reads nothing: the reading ends at its deadline,
    # its start and its write (over a pipe's buffer) included; a request over the bound
    # is never sent.
    python = tmp_path / "python"
    compiler = ["cpython", "3.12.4 (x)", "", "cpython-312", "00"]
    said = repr(["cpython", "3.12.4", [3, 12], *compiler[1:], 0])
    hello = json.dumps({"id": 0, "compiler": compiler})
    python.write_text(
        f'#!/bin/sh\ncase "$1" in -c) echo "{said}";; *) echo \'{hello}\'; exec sleep 30;; esac\n'
    )
    python.chmod(0o755)
    readings = Readings(str(python), identify(str(python), "--python"))
    started = time.monotonic()
    with pytest.raises(Unavailable, match="did not take the request in time"):
        readings("anonymous", "x = 1\n" * 100_000, [1], timeout=1)
    assert time.monotonic() - started < 5
    with pytest.raises(Unavailable, match="over 4 MiB"):
        readings("anonymous", "x" * (5 * 1024 * 1024), [1])
    readings.close()


def test_an_interpreter_that_cannot_read_gives_no_reading(tmp_path, no_active):
    # A helper that ends at once: nothing is read, and nothing is claimed.
    python = str(fake_python(tmp_path / "python"))
    path = tmp_path / "rows.py"
    path.write_text(SOURCE)
    adapter = PythonAdapter(python)
    frame = {"source": {"path": str(path)}, "name": "pick", "line": 7}
    try:
        proof = adapter.globals_proof(frame, ["out", "row", "rows"], ["LIMIT"])
        assert proof["unproven"].startswith(
            "the program's interpreter could not read its scopes (it ended"
        ), proof
        assert adapter.not_yet_declared({"name": "best"}, frame) is None
        with pytest.raises(ValueError, match="could not read rows.py in the program's interp"):
            adapter.entry_lines(path, [8])
    finally:
        adapter.close()


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


def test_a_worker_refuses_an_interpreter_before_anything_runs(tmp_path, no_active):
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

    python = {
        "interpreter": "/p/.venv/bin/python",
        "version": "3.12.4",
        "optimize": 0,
        "chosen": ".venv",
    }
    result = {
        "status": "completed",
        "hits": 1,
        "seconds": 0.5,
        "rows": [],
        "interventions": [],
        "python": python,
    }
    assert "python: /p/.venv/bin/python, Python 3.12.4 (the root's .venv)" in _table(result)


def test_code_named_alike_is_read_by_the_programs_interpreter(tmp_path, monkeypatch, no_active):
    from peeky import probe as probe_module

    # Compiled at the program's level: a lambda in an `assert` -O leaves out has no frame.
    text = "f = lambda v: v + 1; assert (lambda: 1)()  # noqa: E702, E731\n"
    assert probe_module._python_alike(text, [1]) == {1: {"<lambda>": 2}}
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    adapter = PythonAdapter.on(find(tmp_path))
    try:
        assert probe_module._python_alike(text, [1], adapter.readings) == {}
    finally:
        adapter.close()


def test_unread_code_on_a_line_is_said_not_assumed_distinct(tmp_path, monkeypatch, no_active):
    # Code the program's interpreter could not read: two lambdas on the line may be
    # what the debugger names alike, and the warning says so.
    import importlib.util

    # The scripted worker of test_probe: stops at lines 8 and 9 of main.py.
    spec = importlib.util.spec_from_file_location(
        "probe_cases", Path(__file__).with_name("test_probe.py")
    )
    cases = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cases)
    Lines = cases.Lines
    from peeky import probe as probe_module
    from peeky.probe import Probe, run_probe

    lines = ["x = 0"] * 7 + ["x = 1", "f = lambda v: v + 1; g = lambda v: v * 2"]
    (tmp_path / "main.py").write_text("\n".join(lines) + "\n")
    worker = Lines(names={9: ["main", "<lambda>", "<lambda>"]})
    worker.adapter = PythonAdapter(str(fake_python(tmp_path / "python", text="nope", status=1)))
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = run_probe(tmp_path, Probe("main.py", 8, ("x",), set_at=((9, 3, "x", "5"),)))
    said = " ".join(result["warnings"])
    assert "Peeky could not read main.py with the program's interpreter (" in said, said
    assert (
        "cannot tell apart two lambdas, generator expressions or comprehensions on line 9" in said
    )


def test_the_worker_passes_its_python_to_its_probes(tmp_path, monkeypatch):
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
