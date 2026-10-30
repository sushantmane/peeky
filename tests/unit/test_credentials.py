"""The worker's token, by either of its names, reaches no child that does not need it.

Removing only `DEBUG_TOKEN` (the worker's name for the token) from children's
environments isn't enough: `PEEKY_WORKER_TOKEN` (a client's name for the same secret)
would reach a session's local worker, and through it the debugger and the debuggee,
the probe process, a Python candidate's interpreter, checkers, candidates and the model.
"""

import argparse
import os
import subprocess
import sys

import pytest

CREDENTIALS = {
    "DEBUG_TOKEN": "worker-side-token-1234567890-0123456789abcdef",
    "PEEKY_WORKER_TOKEN": "client-side-token-1234567890-0123456789abcdef",
}


@pytest.fixture
def credentials(monkeypatch):
    for name, value in CREDENTIALS.items():
        monkeypatch.setenv(name, value)


class Started(Exception):
    """Raised by a fake Popen once it has seen the child's environment."""


def test_both_names_of_the_token_are_credentials():
    from peeky.cli import WORKER_TOKEN_ENV
    from peeky.processes import CREDENTIAL_ENV, without_credentials

    assert CREDENTIAL_ENV == set(CREDENTIALS) == {"DEBUG_TOKEN", WORKER_TOKEN_ENV}
    assert without_credentials({**CREDENTIALS, "PATH": "/bin"}) == {"PATH": "/bin"}
    assert without_credentials((*CREDENTIALS.items(), ("PATH", "/bin"))) == {"PATH": "/bin"}


def test_the_java_adapter_setup_strips_the_same_names():
    from peeky import java_setup

    assert java_setup.CREDENTIAL_ENV == set(CREDENTIALS)


def test_a_sessions_local_worker_gets_only_its_own_token(tmp_path, monkeypatch, credentials):
    from peeky import cli

    seen = {}

    def popen(command, env=None, **kwargs):
        seen.update(env)
        raise Started

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    (tmp_path / "main.py").write_text("x = 1\nprint(x)\n")
    args = argparse.Namespace(name="default", run=None, arg=[])
    with pytest.raises(Started):
        cli._start_local(args, tmp_path, "main.py", 2, "python", None, None)
    assert "PEEKY_WORKER_TOKEN" not in seen
    assert seen["DEBUG_TOKEN"] not in CREDENTIALS.values()
    assert len(seen["DEBUG_TOKEN"]) >= 32


def test_the_http_probes_process_gets_no_token(tmp_path, monkeypatch, credentials):
    from peeky import worker as worker_module
    from peeky.probe import Probe

    seen = {}

    def popen(command, env=None, **kwargs):
        seen.update(env)
        raise Started

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(Started):
        worker_module._run_probe_process(tmp_path, Probe("main.py", 1), None, 60)
    assert seen and not set(CREDENTIALS) & set(seen)


def test_a_python_candidates_interpreter_gets_no_token(tmp_path, credentials):
    # It runs in the candidate's copy, where the project's import hooks run.
    from peeky.adapters import PythonAdapter, PythonCandidate
    from peeky.build import SourceEdit

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("")
    (proj / "app.py").write_text("x = 1\n")
    candidate = PythonCandidate(
        PythonAdapter(sys.executable), proj / "app.py", proj, SourceEdit("app.py", "x = 1", "x = 2")
    )
    try:
        script = "import json, os; print(json.dumps(sorted(os.environ)))"
        names = candidate._interpreter(script, None)
    finally:
        candidate.close()
    assert names and not set(CREDENTIALS) & set(names)


# A debug adapter that answers each request with the names in its environment, which a
# debuggee it launched would inherit.
ENVIRONMENT_ADAPTER = r"""
import json, os, sys
while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    request = json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
    data = json.dumps({"seq": 0, "type": "response", "request_seq": request["seq"],
                       "success": True, "command": request["command"],
                       "body": {"names": sorted(os.environ)}}).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    sys.stdout.buffer.flush()
"""


@pytest.mark.parametrize("given", [None, "environment"])
def test_the_debug_adapter_and_so_the_debuggee_get_no_token(credentials, given):
    from peeky.dap import DAP

    env = None if given is None else {**os.environ, "PEEKY_GIVEN": "1"}
    dap = DAP(command=[sys.executable, "-c", ENVIRONMENT_ADAPTER], env=env)
    try:
        names = dap.request("threads", timeout=10)["names"]
    finally:
        dap.close()
    assert "PATH" in names and ("PEEKY_GIVEN" in names) is (given is not None)
    assert not set(CREDENTIALS) & set(names)


def test_the_tunnel_hint_keeps_ssh_from_sharing_a_taken_port():
    from peeky.cli import _remote

    with pytest.raises(ValueError) as refused:
        _remote("http://10.0.0.5:8765")
    message = str(refused.value)
    assert "loopback URL" in message and "-o ExitOnForwardFailure=yes" in message
    assert len(message) <= 150


def test_declared_build_environment_cannot_carry_either_name(tmp_path):
    from peeky.build import ArtifactSpec, BuildRecipe, Command, prepare

    (tmp_path / "Main.java").write_text("class Main {}\n")
    for name in sorted(CREDENTIALS):
        recipe = BuildRecipe(
            language="fixture",
            inputs=("Main.java",),
            build_commands=(Command((sys.executable, "-c", "pass")),),
            artifacts=(ArtifactSpec("out.bin"),),
            environment=((name, "declared-token-1234567890"),),
        )
        with pytest.raises(ValueError, match="Worker credentials"):
            prepare(recipe, tmp_path)
