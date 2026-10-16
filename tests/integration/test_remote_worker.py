"""#8: the client/worker boundary across processes and a forwarded TCP hop (ssh -L).

The relay process stands in for an SSH local forward: it listens on loopback and
pipes bytes to the worker's loopback port. Killing it models a dropped tunnel.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from peeky.cli import main

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "remote-worker-token-1234567890abcdef"
RELAY = """
import socket, sys, threading
target = int(sys.argv[1]); port = int(sys.argv[2]) if len(sys.argv) > 2 else 0
server = socket.socket(); server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port)); server.listen(16)
print(server.getsockname()[1], flush=True)
def pipe(a, b):
    try:
        while data := a.recv(65536):
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try: s.shutdown(socket.SHUT_RDWR)
            except OSError: pass
while True:
    client, _ = server.accept()
    upstream = socket.create_connection(("127.0.0.1", target))
    threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
    threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
"""


def spawn(argv, env=None):
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        start_new_session=True,
    )
    line = process.stdout.readline()
    assert line, "process did not start"
    return process, line


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A worker host directory and a separate client checkout with the same sources."""
    host, local = tmp_path / "host", tmp_path / "local"
    for root in (host, local):
        root.mkdir()
        shutil.copyfile(ROOT / "examples/pagination_bug.py", root / "pagination_bug.py")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", TOKEN)
    processes = []

    def worker(*extra):
        env = {**os.environ, "DEBUG_TOKEN": TOKEN}
        env.pop("PEEKY_WORKER_TOKEN", None)
        command = [sys.executable, "-m", "peeky.worker", "--root", str(host), "--port", "0"]
        process, banner = spawn([*command, *extra], env)
        processes.append(process)
        return process, int(banner.rsplit(":", 1)[1].split(";")[0])

    def relay(target, port=0):
        process, line = spawn([sys.executable, "-c", RELAY, str(target), str(port)])
        processes.append(process)
        return process, int(line)

    relay.processes = processes
    yield host, local, worker, relay
    for process in processes:
        stop(process)


def remote_processes(remote):
    return remote[3].processes


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr()


def test_probe_through_a_forwarded_port_checks_source_identity(remote, capsys):
    host, local, worker, relay = remote
    _, worker_port = worker()
    _, port = relay(worker_port)
    url = f"http://127.0.0.1:{port}"
    args = ["probe", local / "pagination_bug.py", "--at", "8", "--capture", "start", "--json"]
    code, out = run(capsys, *args, "--worker", url)
    result = json.loads(out.out)
    assert code == 0 and result["status"] == "completed" and result["worker"] == url
    assert result["rows"][0]["value"] == "3" and "PAGE: [40, 50, 60]" in result["output"]
    assert TOKEN not in out.out and TOKEN not in out.err
    # The worker host has different source text: the worker refuses before running it.
    (host / "pagination_bug.py").write_text(
        "open('RAN', 'w').close()\n"
        + (host / "pagination_bug.py").read_text().replace("page * page_size", "page")
    )
    code, out = run(capsys, *args, "--set", "start=0", "--worker", url)
    result = json.loads(out.out)
    assert code == 1 and result["error"]["code"] == "PROVENANCE_CHANGED"
    assert result["rows"] == [] and not result.get("output")
    assert not (host / "RAN").exists()  # never executed, so never mutated either
    code, out = run(
        capsys, "session", "start", local / "pagination_bug.py", "--at", "8", "--worker", url
    )
    assert code == 2 and "PROVENANCE_CHANGED" in out.err and not (host / "RAN").exists()


def test_remote_session_survives_a_dropped_tunnel_and_closing_keeps_the_worker(remote, capsys):
    host, local, worker, relay = remote
    worker_process, worker_port = worker()
    tunnel, port = relay(worker_port)
    url = f"http://127.0.0.1:{port}"
    code, out = run(capsys, "session", "start", local / "pagination_bug.py", "--at", "8")
    assert code == 0  # local mode still works alongside remote options
    assert run(capsys, "session", "close")[0] == 0
    code, out = run(
        capsys, "session", "start", local / "pagination_bug.py", "--at", "8", "--worker", url
    )
    assert code == 0 and "remote" in out.out and "stopped" in out.out
    record = json.loads((Path(os.environ["PEEKY_STATE_DIR"]) / "sessions/default.json").read_text())
    assert record["remote"] is True and "pid" not in record
    stop(tunnel)
    code, out = run(capsys, "session", "get", "start", "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "UNREACHABLE"
    assert error["retryable"] is True and error["session_closed"] is False
    relay(worker_port, port)  # the tunnel comes back on the same local port
    code, out = run(capsys, "session", "get", "start")
    assert code == 0 and "start: int = 3" in out.out
    # A close that cannot reach the worker keeps the record so it can be retried.
    tunnel = next(p for p in reversed(remote_processes(remote)) if p.poll() is None)
    stop(tunnel)
    code, out = run(capsys, "session", "close", "--json")
    assert code == 2 and json.loads(out.out)["error"]["code"] == "UNREACHABLE"
    assert (Path(os.environ["PEEKY_STATE_DIR"]) / "sessions/default.json").exists()
    relay(worker_port, port)
    assert run(capsys, "session", "close")[0] == 0
    # Closing a remote session never stops the shared worker.
    assert worker_process.poll() is None
    from peeky.client import Client

    assert Client(url, TOKEN).call("version")["protocol"] >= 1


def test_idle_remote_sessions_are_reaped_with_their_debuggee(remote, capsys):
    host, local, worker, relay = remote
    _, worker_port = worker("--idle-timeout", "2")
    url = f"http://127.0.0.1:{worker_port}"
    code, _ = run(
        capsys, "session", "start", local / "pagination_bug.py", "--at", "8", "--worker", url
    )
    assert code == 0
    time.sleep(5)
    code, out = run(capsys, "session", "inspect", "--json")
    assert code == 2 and json.loads(out.out)["error"]["code"] == "NO_SESSION"
    leftover = subprocess.run(
        ["pgrep", "-f", str(host / "pagination_bug.py")], capture_output=True, text=True
    ).stdout
    assert leftover == ""


def test_remote_options_fail_closed(remote, capsys, monkeypatch):
    host, local, worker, relay = remote
    _, worker_port = worker()
    target = ["probe", local / "pagination_bug.py", "--at", "8", "--json"]
    code, out = run(capsys, *target, "--worker", "http://10.0.0.5:8765")
    assert code == 2 and "loopback URL" in json.loads(out.out)["error"]["message"]
    code, out = run(capsys, *target, "--worker", f"http://127.0.0.1:{worker_port}")
    assert code == 0
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", "wrong-token-but-long-enough")
    code, out = run(capsys, *target, "--worker", f"http://127.0.0.1:{worker_port}")
    assert code == 2 and json.loads(out.out)["error"]["code"] == "UNAUTHORIZED"
    monkeypatch.delenv("PEEKY_WORKER_TOKEN")
    code, out = run(capsys, *target, "--worker", f"http://127.0.0.1:{worker_port}")
    assert code == 2 and "PEEKY_WORKER_TOKEN" in json.loads(out.out)["error"]["message"]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", TOKEN)
    code, out = run(capsys, *target, "--worker", f"http://127.0.0.1:{free}")
    assert code == 2 and json.loads(out.out)["error"]["code"] == "UNREACHABLE"


def test_a_dead_local_worker_is_a_lost_session_not_a_retry(remote, capsys):
    host, local, worker, relay = remote
    code, _ = run(capsys, "session", "start", local / "pagination_bug.py", "--at", "8")
    assert code == 0
    record = json.loads((Path(os.environ["PEEKY_STATE_DIR"]) / "sessions/default.json").read_text())
    os.killpg(record["pid"], signal.SIGKILL)
    time.sleep(0.5)
    code, out = run(capsys, "session", "inspect", "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "DEBUGGER_ERROR" and error["session_closed"] is True
    assert "has exited" in error["message"]
    assert run(capsys, "session", "close")[0] == 2
    assert not (Path(os.environ["PEEKY_STATE_DIR"]) / "sessions/default.json").exists()


def test_an_empty_worker_url_never_falls_back_to_running_locally(remote, capsys):
    host, local, worker, relay = remote
    code, out = run(
        capsys, "probe", local / "pagination_bug.py", "--at", "8", "--worker", "", "--json"
    )
    assert code == 2 and "loopback URL" in json.loads(out.out)["error"]["message"]


def test_results_without_the_entry_hash_are_discarded(remote, capsys, monkeypatch):
    # An older worker reports no entry_sha256: never accept --run results unverified.
    host, local, worker, relay = remote
    (local / "main.py").write_text("import pagination_bug\n")
    from peeky import client as client_module

    def old_worker_probe(self, at, *args, **kwargs):
        digest = __import__("hashlib").sha256((local / "pagination_bug.py").read_bytes())
        return {
            "status": "completed",
            "hits": 1,
            "rows": [{"value": "3"}],
            "output": "x",
            "source_sha256": digest.hexdigest(),
        }

    monkeypatch.setattr(client_module.Client, "probe", old_worker_probe)
    monkeypatch.setattr(client_module.Client, "call", lambda self, op, **kw: {"protocol": 1})
    code, out = run(
        capsys,
        "probe",
        local,
        "--at",
        "pagination_bug.py:8",
        "--run",
        "main.py",
        "--worker",
        "http://127.0.0.1:9",
        "--json",
    )
    result = json.loads(out.out)
    assert code == 1 and result["results_discarded"] is True and result["rows"] == []
