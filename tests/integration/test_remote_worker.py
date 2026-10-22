"""#8: the client/worker boundary across processes and a forwarded TCP hop (ssh -L).

The relay process stands in for an SSH local forward: it listens on loopback and
pipes bytes to the worker's loopback port. Killing it models a dropped tunnel.
"""

import hashlib
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
        env = {
            **os.environ,
            "DEBUG_TOKEN": TOKEN,
            "PEEKY_STATE_DIR": str(tmp_path / "worker-state"),
        }
        env.pop("PEEKY_WORKER_TOKEN", None)
        # -B: the worker writes no bytecode of its own; what it runs is up to the test.
        command = [sys.executable, "-B", "-m", "peeky.worker", "--root", str(host), "--port", "0"]
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


def test_a_file_the_worker_root_lacks_is_refused_before_launch(remote, capsys):
    # #190: it printed an empty table and exited 1, as if the probe had run.
    host, local, worker, _ = remote
    _, port = worker()
    (local / "extra.py").write_text("x = 1\nprint(x)\n")
    args = ["probe", local / "extra.py", "--at", "2", "--capture", "x", "--worker"]
    code, out = run(capsys, *args, f"http://127.0.0.1:{port}")
    assert code == 2 and "error [INVALID_REQUEST]" in out.out, out
    assert "hit  phase" not in out.out and "evidence:" not in out.out, out.out
    code, out = run(capsys, *args, f"http://127.0.0.1:{port}", "--json")
    result = json.loads(out.out)
    assert code == 2 and result["error"]["code"] == "INVALID_REQUEST", result
    assert result["refused_before_launch"] is True
    assert "inside the worker root" in result["error"]["message"]


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
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED"
    assert result["refused_before_launch"] is True
    assert result["rows"] == [] and not result.get("output")
    assert not (host / "RAN").exists()  # never executed, so never mutated either
    code, out = run(
        capsys, "session", "start", local / "pagination_bug.py", "--at", "8", "--worker", url
    )
    assert code == 2 and "PROVENANCE_CHANGED" in out.err and not (host / "RAN").exists()


MAIN = "import helper\nx = helper.page()\nprint('PAGE', x)\n"
HELPER = "def page():\n    return 3\n"


def test_an_imported_module_that_differs_on_the_host_is_refused_before_launch(remote, capsys):
    # #249: only the program's file was compared, so the host ran another helper.py and
    # the values came from it without a note.
    host, local, worker, relay = remote
    for root in (host, local):
        (root / "main.py").write_text(MAIN)
        (root / "helper.py").write_text(HELPER)
    _, worker_port = worker()
    _, port = relay(worker_port)
    url = f"http://127.0.0.1:{port}"
    args = ["probe", local / "main.py", "--at", "3", "--capture", "x", "--json"]
    code, out = run(capsys, *args, "--worker", url)
    result = json.loads(out.out)
    assert code == 0 and result["rows"][0]["value"] == "3", result
    (host / "helper.py").write_text("open('RAN', 'w').close()\n" + HELPER.replace("3", "4"))
    code, out = run(capsys, *args, "--worker", url)
    result = json.loads(out.out)
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert (
        "for helper.py (the copy on the worker host differs from your checkout)"
        in (result["error"]["message"])
    )
    assert result["refused_before_launch"] is True and result["rows"] == []
    assert not (host / "RAN").exists()
    code, out = run(capsys, "session", "start", local / "main.py", "--at", "3", "--worker", url)
    assert code == 2 and "helper.py (the copy on the worker host differs" in out.err, out.err
    assert not (host / "RAN").exists()


def test_a_module_on_one_side_only_is_refused_before_launch(remote, capsys):
    host, local, worker, relay = remote
    _, worker_port = worker()
    url = f"http://127.0.0.1:{worker_port}"
    args = ["probe", local / "pagination_bug.py", "--at", "8", "--json", "--worker", url]
    (local / "pagination_helpers.py").write_text("STEP = 1\n")
    code, out = run(capsys, *args)
    result = json.loads(out.out)
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert "pagination_helpers.py (only in your checkout)" in result["error"]["message"]
    (local / "pagination_helpers.py").unlink()
    (host / "sitecustom").mkdir()
    (host / "sitecustom" / "hook.py").write_text("STEP = 2\n")
    code, out = run(capsys, *args)
    result = json.loads(out.out)
    assert (
        code == 2 and "sitecustom/hook.py (only on the worker host)" in (result["error"]["message"])
    )


def test_a_python_session_launch_reports_the_sources_it_accepted(remote):
    # #254 review, blocking 1: a Python session's launch answered inputs_checked but no
    # inputs_sha256, and the client accepted the missing digest.
    from peeky.client import Client
    from peeky.sources import digest, python_digest, python_sources

    host, local, worker, relay = remote
    _, port = worker()
    client = Client(f"http://127.0.0.1:{port}", TOKEN)
    expected = dict(python_sources(local))
    launch = client.call(
        "launch",
        program="pagination_bug.py",
        breakpoints=[{"line": 8}],
        expect_sha256={"program": expected["pagination_bug.py"]},
        expect_inputs=expected,
    )
    try:
        assert launch["inputs_checked"] == digest(expected)
        assert launch["inputs_sha256"] == python_digest(local)
    finally:
        client.call("close", session_id=launch["session_id"])


def test_a_python_session_that_meets_a_changed_source_is_closed_without_values(remote, capsys):
    # #254 review, blocking 5: the program imported a helper.py changed after the start.
    host, local, worker, relay = remote
    program = "x = 0\nimport helper\nx = helper.page()\nprint('PAGE', x)\n"
    for root in (host, local):
        (root / "main.py").write_text(program)
        (root / "helper.py").write_text(HELPER)
    _, port = worker()
    url = f"http://127.0.0.1:{port}"
    code, out = run(capsys, "session", "start", local / "main.py", "--at", "2", "--worker", url)
    assert code == 0, out.err
    (host / "helper.py").write_text(HELPER.replace("3", "4"))
    code, out = run(capsys, "session", "continue", "--to-exit", "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "PROVENANCE_CHANGED", error
    assert "helper.py changed on the worker host after launch" in error["message"]
    assert error["session_closed"] is True
    code, out = run(capsys, "session", "report", "--json")
    assert "PAGE" not in out.out


def test_a_module_in_a_hidden_directory_is_compared(remote, capsys):
    # #254 review, blocking 6a: .hidden/ was skipped on both sides, so the host ran a
    # different .hidden/helper.py (x=4) and the probe exited 0.
    host, local, worker, relay = remote
    for root in (host, local):
        (root / ".hidden").mkdir()
        (root / ".hidden" / "main.py").write_text(MAIN)
        (root / ".hidden" / "helper.py").write_text(HELPER)
    (host / ".hidden" / "helper.py").write_text(HELPER.replace("3", "4"))
    _, port = worker()
    code, out = run(
        capsys,
        "probe",
        local,
        "--at",
        ".hidden/main.py:3",
        "--capture",
        "x",
        "--worker",
        f"http://127.0.0.1:{port}",
        "--json",
    )
    result = json.loads(out.out)
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert ".hidden/helper.py (the copy on the worker host differs" in result["error"]["message"]


def test_a_remote_python_run_writes_no_bytecode_into_the_root(remote, capsys, monkeypatch):
    host, local, worker, relay = remote
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)  # the worker's own setting
    for root in (host, local):
        (root / "main.py").write_text(MAIN)
        (root / "helper.py").write_text(HELPER)
    _, port = worker()
    code, out = run(
        capsys, "probe", local / "main.py", "--at", "3", "--worker", f"http://127.0.0.1:{port}"
    )
    assert code == 0, out
    assert not (host / "__pycache__").exists()
    assert "Not compared: installed packages, any directory holding a pyvenv.cfg" in out.out


FRAMED = "import hook\ndef page():\n    x = 3\n    return x\nprint('PAGE', hook.run(page))\n"


def sourceless(root, text):
    """hook.pyc beside main.py, with no hook.py: its code names root/hook.py."""
    import py_compile

    (root / "hook_src.py").write_text(text)
    py_compile.compile(
        str(root / "hook_src.py"), cfile=str(root / "hook.pyc"), dfile=str(root / "hook.py")
    )
    (root / "hook_src.py").unlink()


def test_a_stack_frame_in_an_uncompared_file_under_the_root_is_refused(remote, capsys):
    # #254 review round 1, 6(c) option A: hook is loaded from a .pyc, which is never
    # compared, and its frame is on the stack at the stop (it calls page()).
    host, local, worker, relay = remote
    for root in (host, local):
        (root / "main.py").write_text(FRAMED)
        sourceless(root, "def run(f):\n    return f()\n")
    _, port = worker()
    url = f"http://127.0.0.1:{port}"
    args = ["probe", local / "main.py", "--at", "4", "--capture", "x", "--worker", url, "--json"]
    code, out = run(capsys, *args)
    result = json.loads(out.out)
    assert code == 1 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert "a stack frame is in hook.py" in result["error"]["message"]
    assert result["rows"] == [] and not result.get("output") and result["results_discarded"]
    code, out = run(capsys, "session", "start", local / "main.py", "--at", "4", "--worker", url)
    assert code == 2 and "a stack frame is in hook.py" in out.err, out.err


# Round 2 of the #254 review: each passed on 7a37d9d.

GIT_HOOKED = (
    "import os, sys\n"
    "sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '.git'))\n"
    "import hook\n"
    "x = hook.page()\n"
    "print('PAGE', x)\n"
)


def test_code_under_git_is_compared_like_any_other(remote, capsys):
    # .git was left out, and once hook.page() returned no frame of it was on the stack:
    # the host's hook returned 4, and the probe printed 4 with exit 0.
    host, local, worker, relay = remote
    for root in (host, local):
        (root / "main.py").write_text(GIT_HOOKED)
        (root / ".git").mkdir()
        (root / ".git" / "hook.py").write_text(HELPER)
    (host / ".git" / "hook.py").write_text(HELPER.replace("3", "4"))
    _, port = worker()
    args = ["probe", local / "main.py", "--at", "5", "--capture", "x", "--json", "--worker"]
    code, out = run(capsys, *args, f"http://127.0.0.1:{port}")
    result = json.loads(out.out)
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert ".git/hook.py (the copy on the worker host differs" in result["error"]["message"]


def test_your_checkout_changing_after_the_start_closes_the_session(remote, capsys):
    host, local, worker, relay = remote
    for root in (host, local):
        (root / "main.py").write_text(MAIN)
        (root / "helper.py").write_text(HELPER)
    _, port = worker()
    url = f"http://127.0.0.1:{port}"
    code, out = run(capsys, "session", "start", local / "main.py", "--at", "3", "--worker", url)
    assert code == 0, out.err
    (local / "helper.py").write_text(HELPER.replace("3", "4"))
    code, out = run(capsys, "session", "get", "x", "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "PROVENANCE_CHANGED", error
    assert "a source in your checkout changed after the worker compared it" in error["message"]
    code, out = run(capsys, "session", "get", "x", "--json")
    assert code == 2 and "No session named" in json.loads(out.out)["error"]["message"]


def test_a_worker_source_changed_before_a_stop_is_refused_by_wait(remote):
    # wait returned the second stop, and its hit, from the changed main.py.
    from peeky.client import Client
    from peeky.errors import PeekyError
    from peeky.sources import python_sources

    host, local, worker, relay = remote
    program = "import time\nx = 1\ntime.sleep(1.5)\nx = 2\nprint(x)\n"
    for root in (host, local):
        (root / "main.py").write_text(program)
    _, port = worker()
    client = Client(f"http://127.0.0.1:{port}", TOKEN)
    expected = dict(python_sources(local))
    launch = client.call(
        "launch",
        program="main.py",
        breakpoints=[{"line": 2}, {"line": 4}],
        expect_sha256={"program": expected["main.py"]},
        expect_inputs=expected,
    )
    session = {"session_id": launch["session_id"]}
    try:
        state = client.call("wait", **session, timeout=10)
        assert state["state"] == "stopped" and state["location"].endswith("main.py:2"), state
        client.call("continue", **session, stop_id=state["stop_id"])
        (host / "main.py").write_text(program.replace("x = 2", "x = 9"))  # during the sleep
        with pytest.raises(PeekyError) as refused:
            client.call("wait", **session, timeout=10)
        assert refused.value.error_code == "PROVENANCE_CHANGED", refused.value.payload
        assert "main.py changed on the worker host" in refused.value.payload["message"]
    finally:
        try:
            client.call("close", **session)
        except PeekyError:
            pass


def test_later_session_commands_carry_the_note(remote, capsys):
    host, local, worker, relay = remote
    _, port = worker()
    url = f"http://127.0.0.1:{port}"
    start = ["session", "start", local / "pagination_bug.py", "--at", "8", "--worker", url]
    assert run(capsys, *start)[0] == 0
    code, out = run(capsys, "session", "get", "start", "--json")
    rows = json.loads(out.out)
    assert code == 0 and "any directory holding a pyvenv.cfg" in rows[0]["warnings"][-1], rows
    code, out = run(capsys, "session", "get", "start")
    assert code == 0 and "warning: Only the .py files under the target's directory" in out.out
    code, out = run(capsys, "session", "inspect", "--json")
    assert code == 0 and any("pyvenv.cfg" in w for w in json.loads(out.out)["warnings"])
    run(capsys, "session", "close")


def test_a_stale_pyc_in_the_tree_is_never_used(remote, capsys):
    # The worker host's __pycache__ holds a helper.pyc compiled from other code, marked
    # unchecked: Python loads it without looking at helper.py.
    import py_compile
    import sys

    host, local, worker, relay = remote
    for root in (host, local):
        (root / "main.py").write_text(MAIN)
        (root / "helper.py").write_text(HELPER)
    (host / "stale.py").write_text(HELPER.replace("3", "4"))
    py_compile.compile(
        str(host / "stale.py"),
        cfile=str(host / "__pycache__" / f"helper.{sys.implementation.cache_tag}.pyc"),
        dfile=str(host / "helper.py"),
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    (host / "stale.py").unlink()
    _, port = worker()
    args = ["probe", local / "main.py", "--at", "3", "--capture", "x", "--json", "--worker"]
    code, out = run(capsys, *args, f"http://127.0.0.1:{port}")
    result = json.loads(out.out)
    assert code == 0 and result["rows"][0]["value"] == "3", result
    assert "PAGE 3" in result["output"]


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
    from peeky.errors import PROTOCOL_VERSION

    hello = {"protocol": PROTOCOL_VERSION, "sources": {"kind": "python"}}
    monkeypatch.setattr(client_module.Client, "call", lambda self, op, **kw: hello)
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


def test_a_local_launch_failure_is_a_closed_session_and_gone_workers_can_be_forgotten(
    remote, capsys
):
    from peeky.errors import classify

    payload = classify(
        RuntimeError(
            "The local worker failed while launching and was stopped; no session exists. "
            "Start again"
        )
    )
    assert payload["code"] == "DEBUGGER_ERROR" and payload["session_closed"] is True
    assert payload["retryable"] is False
    host, local, worker, relay = remote
    _, worker_port = worker()
    tunnel, port = relay(worker_port)
    url = f"http://127.0.0.1:{port}"
    start = ["session", "start", local / "pagination_bug.py", "--at", "8", "--worker", url]
    assert run(capsys, *start)[0] == 0
    stop(tunnel)  # the remote worker is now unreachable for good
    code, out = run(capsys, "session", "close", "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and "--forget" in error["hint"]
    code, out = run(capsys, "session", "close", "--forget")
    assert code == 0 and "forgot default" in out.out
    assert not (Path(os.environ["PEEKY_STATE_DIR"]) / "sessions/default.json").exists()


def test_remote_probes_are_recorded_by_the_client_only(remote, capsys, tmp_path):
    host, local, worker, relay = remote
    _, worker_port = worker()
    url = f"http://127.0.0.1:{worker_port}"
    probe = ["probe", local / "pagination_bug.py", "--at", "8", "--capture", "start"]
    code, out = run(capsys, *probe, "--worker", url, "--no-save")
    assert code == 0 and "evidence:" not in out.out
    code, out = run(capsys, *probe, "--worker", url, "--json")
    result = json.loads(out.out)
    assert code == 0 and result["evidence"]["run_id"] == 1
    assert not any("Evidence" in warning for warning in result["warnings"])
    code, out = run(capsys, "sql", "SELECT worker, status FROM runs", "--json")
    assert json.loads(out.out)["rows"] == [{"worker": url, "status": "completed"}]
    # The worker host keeps no evidence: its HTTP probes stay stateless.
    assert not (tmp_path / "worker-state" / "evidence").exists()


def test_worker_side_evidence_never_reaches_the_client_result(remote, capsys, monkeypatch):
    host, local, worker, relay = remote
    digest = hashlib.sha256((local / "pagination_bug.py").read_bytes()).hexdigest()

    class Worker:
        sources = {"kind": "python"}

        def probe(self, *args, **kwargs):
            from peeky.sources import digest as inputs_digest

            return {
                "inputs_checked": inputs_digest(kwargs["expect_inputs"]),
                "probe": {"program": "pagination_bug.py", "line": 8},
                "status": "completed",
                "hits": 1,
                "rows": [],
                "interventions": [],
                "output": "",
                "source_sha256": digest,
                "evidence": {"run_id": 41, "store": "/worker/evidence.db"},
                "warnings": ["Evidence was not saved: worker disk full", "kept"],
            }

    monkeypatch.setattr("peeky.cli._remote", lambda url: (Worker(), TOKEN))
    probe = ["probe", local / "pagination_bug.py", "--at", "8", "--json"]
    result = json.loads(run(capsys, *probe, "--worker", "http://127.0.0.1:1", "--no-save")[1].out)
    assert "evidence" not in result and result["warnings"] == ["kept"]
    result = json.loads(run(capsys, *probe, "--worker", "http://127.0.0.1:1")[1].out)
    assert result["evidence"]["run_id"] == 1 and result["warnings"] == ["kept"]
