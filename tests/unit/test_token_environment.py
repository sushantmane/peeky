"""The worker's token reaches no process Peeky starts (security review before going public).

`peeky-worker`, `peeky` and `peeky-agent` read the token from their environment and left
it there, and a few calls started a tool with that whole environment: the compiler's
`--version`, the C preprocessor that finds `main`, `ps`, `xcrun`, `rustup`, git and a
Python program's interpreter. Each command now takes both names out of its environment
first, and those calls pass an environment without them.
"""

import os
import subprocess
import sys

import pytest

CREDENTIALS = {
    "DEBUG_TOKEN": "worker-side-token-1234567890",
    "PEEKY_WORKER_TOKEN": "client-side-token-1234567890",
}


@pytest.fixture
def credentials(monkeypatch):
    for name, value in CREDENTIALS.items():
        monkeypatch.setenv(name, value)


class Started(Exception):
    """Raised by a stub once it has seen what a command would start."""


def sdk(monkeypatch):
    from peeky import build

    with monkeypatch.context() as patch:
        patch.setattr(build.os.path, "exists", lambda path: True)
        return build.default_sdk()


def calls(tmp_path, monkeypatch):
    from peeky import adapters, build, cli, native, processes, python_passes

    (tmp_path / "cc").write_text("")
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli.shutil, "which", lambda name, **_: f"/usr/bin/{name}")
    build._xcrun_find.cache_clear()
    build.default_sdk.cache_clear()
    python_passes._COMPILERS.clear()
    return {
        "compiler --version": lambda: native.compiler_family(str(tmp_path / "cc")),
        "preprocessor": lambda: cli._defines_main(tmp_path / "main.c", {"compiler": "cc"}, "c"),
        "ps": lambda: processes._ps("-A"),
        "xcrun --find": lambda: build._xcrun_find("clang"),
        "xcrun --show-sdk-path": lambda: sdk(monkeypatch),
        "cli xcrun": lambda: cli._xcrun("clang"),
        "rustup": lambda: cli._rustup("cargo"),
        "native sdk": lambda: cli.native_tools("c"),
        "git": lambda: adapters._git("git", tmp_path, "status"),
        "interpreter": lambda: python_passes.same_compiler(str(tmp_path / "python3")),
    }


def test_no_tool_peeky_asks_gets_the_token(tmp_path, monkeypatch, credentials):
    from peeky import build, processes

    seen = []

    def popen(command, *args, env=None, **kwargs):
        seen.append(env)
        raise OSError("not started")

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(processes, "_POPEN", popen)
    leaked = []
    try:
        for name, call in calls(tmp_path, monkeypatch).items():
            seen.clear()
            try:
                call()
            except (OSError, ValueError):
                pass
            assert seen, f"{name} started nothing"
            # None inherits this process's environment, and both names with it.
            if any(env is None or set(CREDENTIALS) & set(env) for env in seen):
                leaked.append(name)
            assert all(env is None or "PATH" in env for env in seen), name
    finally:
        build._xcrun_find.cache_clear()
        build.default_sdk.cache_clear()
    assert leaked == []


def test_the_worker_takes_its_token_out_of_its_environment(monkeypatch, credentials):
    from peeky import worker

    seen = {}

    def make_server(root, token, port, adapter=None):
        seen.update(token=token, names=set(CREDENTIALS) & set(os.environ))
        raise Started

    monkeypatch.setattr(sys, "argv", ["peeky-worker", "--port", "0"])
    monkeypatch.setattr(worker, "make_server", make_server)
    with pytest.raises(Started):
        worker.main()
    assert seen == {"token": CREDENTIALS["DEBUG_TOKEN"], "names": set()}


def test_peeky_takes_both_names_out_while_it_runs(tmp_path, monkeypatch, credentials):
    from peeky import cli

    seen = {}

    class Remote:
        def __init__(self, url, token):
            seen.update(token=token, names=set(CREDENTIALS) & set(os.environ))
            raise Started

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_RemoteClient", Remote)
    (tmp_path / "main.py").write_text("x = 1\n")
    argv = ["probe", str(tmp_path / "main.py"), "--at", "1", "--worker", "http://127.0.0.1:9"]
    assert cli.main(argv) == 2
    assert seen == {"token": CREDENTIALS["PEEKY_WORKER_TOKEN"], "names": set()}
    # A caller in this process gets them back.
    assert {name: os.environ.get(name) for name in CREDENTIALS} == CREDENTIALS


def test_a_background_job_gets_only_the_clients_token(tmp_path, monkeypatch, credentials):
    from peeky import cli

    seen = {}

    def popen(command, env=None, **kwargs):
        seen.update(env)
        raise Started

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    (tmp_path / "main.py").write_text("x = 1\n")
    argv = ["probe", str(tmp_path / "main.py"), "--at", "1", "--background"]
    argv += ["--worker", "http://127.0.0.1:9"]
    assert cli.main(argv) == 2
    # The job is this command, run later: a remote probe needs the client's token.
    assert seen["PEEKY_WORKER_TOKEN"] == CREDENTIALS["PEEKY_WORKER_TOKEN"]
    assert "DEBUG_TOKEN" not in seen


def test_the_agent_takes_its_token_out_of_its_environment(tmp_path, monkeypatch, credentials):
    from peeky import agent

    seen = {}

    def client(url, token):
        seen.update(token=token, names=set(CREDENTIALS) & set(os.environ))
        raise Started

    argv = ["peeky-agent", "--url", "http://127.0.0.1:9", "--output", str(tmp_path / "out")]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/true")
    monkeypatch.setattr(agent, "Client", client)
    with pytest.raises(Started):
        agent.main()
    assert seen == {"token": CREDENTIALS["DEBUG_TOKEN"], "names": set()}


@pytest.mark.parametrize("url", ["http://10.0.0.5:8765", "http://localhost:8765"])
def test_the_agent_sends_the_token_only_to_loopback(tmp_path, monkeypatch, capsys, url):
    from peeky import agent
    from peeky.client import Client

    monkeypatch.setenv("DEBUG_TOKEN", CREDENTIALS["DEBUG_TOKEN"])
    argv = ["peeky-agent", "--url", url, "--output", str(tmp_path / "out")]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/true")
    monkeypatch.setattr(agent, "Client", lambda *a: pytest.fail("a client was made for " + url))
    with pytest.raises(SystemExit) as stopped:
        agent.main()
    assert stopped.value.code == 2
    assert "--url must be a loopback URL (http://127.0.0.1:PORT)" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()
    with pytest.raises(ValueError, match="must be loopback"):
        Client(url, CREDENTIALS["DEBUG_TOKEN"])
