"""Regressions for the 2026-10-16 whole-code review (engine and CLI lifecycle), real debugpy."""

import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from peeky.cli import main
from peeky.client import Client
from peeky.errors import classify
from peeky.worker import MAX_HISTORY_BYTES, DebugWorker, make_server

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "review-findings-token-1234567890"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    shutil.copyfile(ROOT / "examples" / "pagination_bug.py", tmp_path / "pagination_bug.py")
    return tmp_path


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr()


def gone(pid, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return True
        except ChildProcessError:
            pass
        time.sleep(0.05)
    return False


def stopped(worker, session):
    for _ in range(30):
        state = worker.dispatch("wait", {**session, "timeout": 1})
        if state["state"] == "stopped":
            return {**session, "stop_id": state["stop_id"]}
    pytest.fail("program did not stop")


def pid_program(path, extra=""):
    path.write_text(
        "import os\n"
        "open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pid'), 'w')"
        ".write(str(os.getpid()))\n" + extra + "print('stop here')\n"
    )
    return len(path.read_text().splitlines())


def test_a_failed_transport_still_ends_the_launched_program(tmp_path):
    # A scope whose variables reply exceeds the 4 MiB frame limit fails the transport.
    huge = "".join(f"v{i} = 'x' * 100000\n" for i in range(100))
    line = pid_program(tmp_path / "huge.py", huge)
    worker = DebugWorker(tmp_path)
    launch = worker.dispatch("launch", {"program": "huge.py", "breakpoints": [{"line": line}]})
    stop = stopped(worker, {"session_id": launch["session_id"]})
    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(RuntimeError) as caught:
        worker.dispatch("inspect", stop)
    payload = classify(caught.value)
    assert payload["code"] == "DEBUGGER_ERROR" and "protocol stream" in payload["hint"]
    assert "closed the session: start a new one" in payload["hint"], payload["hint"]
    assert payload["session_closed"] is True
    assert gone(pid), "debugpy's debuggee (its own process group) survived the closed session"


def test_history_is_bounded_in_bytes(tmp_path):
    worker = DebugWorker(tmp_path)
    worker.record({"time": 0, "operation": "inspect", "arguments": {}, "result": "x" * 300_000})
    assert worker.audit[0]["result"]["omitted"] and worker.audit[0]["result"]["bytes"] > 300_000
    for _ in range(100):
        worker.record({"time": 0, "operation": "inspect", "arguments": {}, "result": "y" * 200_000})
    assert worker.audit_bytes <= MAX_HISTORY_BYTES
    assert len(json.dumps(worker.audit)) < MAX_HISTORY_BYTES + 100_000
    assert worker.audit_bytes == sum(len(json.dumps(e["result"])) for e in worker.audit)


@pytest.fixture
def served(tmp_path):
    server = make_server(tmp_path, TOKEN, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.worker.close()
    server.shutdown()
    server.server_close()


def test_worker_calls_never_use_an_http_proxy(served, monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    client = Client(f"http://127.0.0.1:{served.server_port}", TOKEN)
    assert client.call("version")["protocol"] == 1


def test_malformed_requests_are_request_errors(served):
    def post(headers, body):
        connection = http.client.HTTPConnection("127.0.0.1", served.server_port, timeout=10)
        connection.putrequest("POST", "/version")
        for key, value in headers.items():
            connection.putheader(key, value)
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    status, body = post({"Authorization": "Bearer \xe9".encode("latin-1")}, b"{}")
    assert status == 401 and body["error"]["code"] == "UNAUTHORIZED"
    status, body = post({"Authorization": f"Bearer {TOKEN}"}, b"[" * 30000 + b"]" * 30000)
    assert status == 400 and body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["session_closed"] is False


def test_two_starts_with_one_name_cannot_both_succeed(workspace):
    command = [
        sys.executable,
        "-m",
        "peeky.cli",
        "session",
        "start",
        str(workspace / "pagination_bug.py"),
        "--at",
        "8",
    ]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    pipes = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    starts = [subprocess.Popen(command, env=env, **pipes) for _ in "ab"]
    results = [(p.wait(timeout=60), *p.communicate()) for p in starts]
    codes = sorted(code for code, _, _ in results)
    assert codes == [0, 2], results
    assert "exists" in next(err for code, _, err in results if code == 2)
    record = json.loads((workspace / "state/sessions/default.json").read_text())
    subprocess.run([*command[:3], "session", "close"], env=env, check=True, capture_output=True)
    assert gone(record["pid"]), "the winning start's worker outlived its session"


def test_an_interrupted_start_leaves_no_worker_and_frees_the_name(workspace, capsys, monkeypatch):
    import peeky.cli as cli

    spawned = []
    real_popen = subprocess.Popen

    def recording(*args, **kwargs):
        spawned.append(real_popen(*args, **kwargs))
        return spawned[-1]

    real_call = cli._LocalClient.call

    def interrupted(self, operation, **arguments):
        if operation == "launch":
            raise KeyboardInterrupt
        return real_call(self, operation, **arguments)

    monkeypatch.setattr(cli.subprocess, "Popen", recording)
    monkeypatch.setattr(cli._LocalClient, "call", interrupted)
    with pytest.raises(KeyboardInterrupt):
        main(["session", "start", str(workspace / "pagination_bug.py"), "--at", "8"])
    assert spawned and spawned[0].poll() is not None
    assert not (workspace / "state/sessions/default.json").exists()
    monkeypatch.setattr(cli._LocalClient, "call", real_call)
    code, out = run(capsys, "session", "start", workspace / "pagination_bug.py", "--at", "8")
    assert code == 0, out.err
    closed = json.loads(run(capsys, "session", "close", "--json")[1].out)
    # The program, stopped at line 8, ended with its session (#143).
    assert closed["closed"] == "default" and closed["cleanup"].startswith("terminated pid ")


def spawn_worker(root, *extra):
    process = subprocess.Popen(
        [sys.executable, "-m", "peeky.worker", "--root", str(root), "--port", "0", *extra],
        env={**os.environ, "DEBUG_TOKEN": TOKEN, "PYTHONPATH": str(ROOT / "src")},
        stdout=subprocess.PIPE,
        text=True,
    )
    banner = process.stdout.readline()
    return process, Client(banner.split()[2].rstrip(";"), TOKEN)


def test_workers_close_their_program_on_sigterm(tmp_path):
    line = pid_program(tmp_path / "paused.py")
    process, client = spawn_worker(tmp_path)
    try:
        launch = client.call("launch", program="paused.py", breakpoints=[{"line": line}])
        session = {"session_id": launch["session_id"]}
        for _ in range(30):
            if client.call("wait", **session, timeout=1)["state"] == "stopped":
                break
        pid = int((tmp_path / "pid").read_text())
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)
        assert gone(pid), "SIGTERM left the paused program behind"
    finally:
        if process.poll() is None:
            process.kill()


def test_session_workers_exit_after_their_idle_timeout(tmp_path):
    line = pid_program(tmp_path / "paused.py")
    process, client = spawn_worker(tmp_path, "--idle-timeout", "1", "--exit-when-idle")
    try:
        client.call("launch", program="paused.py", breakpoints=[{"line": line}])
        process.wait(timeout=15)
        assert gone(int((tmp_path / "pid").read_text()))
    finally:
        if process.poll() is None:
            process.kill()


def test_relative_java_bundles_follow_the_configuration_file(tmp_path, monkeypatch):
    from peeky.cli import resolve

    configuration = tmp_path / "conf" / "target.json"
    configuration.parent.mkdir()
    configuration.write_text(
        json.dumps({"language": "java", "bundle": "adapter", "sources": ["A.java"]})
    )
    (tmp_path / "A.java").write_text("class A {}\n")
    monkeypatch.chdir(tmp_path / "conf")
    data = resolve(tmp_path, "A.java:1", configuration)[4]
    assert data["bundle"] == str((tmp_path / "conf" / "adapter").resolve())


@pytest.mark.parametrize(
    "reply",
    [
        {"status": "timeout", "error": {"code": "TIMEOUT", "message": "cap", "hint": "h"}},
        {"error": {"code": "DEBUGGER_ERROR", "message": "child crashed", "hint": "h"}},
    ],
)
def test_partial_remote_probe_replies_keep_their_error(workspace, capsys, monkeypatch, reply):
    class Worker:
        def probe(self, *args, **kwargs):
            return json.loads(json.dumps(reply))

    monkeypatch.setattr("peeky.cli._remote", lambda url: (Worker(), TOKEN))
    probe = ["probe", workspace / "pagination_bug.py", "--at", "8", "--no-save"]
    code, out = run(capsys, *probe, "--worker", "http://127.0.0.1:1", "--json")
    result = json.loads(out.out)  # exactly one JSON document
    assert code == 1 and result["error"]["code"] == reply["error"]["code"]
    assert result["status"] == reply.get("status", "failed") and result["rows"] == []
    code, out = run(capsys, *probe, "--worker", "http://127.0.0.1:1")
    assert code == 1 and f"error [{reply['error']['code']}]" in out.out


def test_session_get_reports_one_unreadable_selector_as_a_row(workspace, capsys):
    program = workspace / "wide.py"
    program.write_text(
        "class O:\n    pass\n"
        "o = O()\n"
        "for i in range(600):\n    setattr(o, f'a{i:03}', i)\n"
        "small = [1, 2]\n"
        "print('ready')\n"
    )
    assert run(capsys, "session", "start", program, "--at", "7")[0] == 0
    code, out = run(capsys, "session", "get", "small[0]", "o.a599", "small[1]", "--json")
    rows = {row["selector"]: row for row in json.loads(out.out)}
    # Every row is reported; the exit status says that one selector had no value.
    assert code == 1, out.err
    assert rows["small[0]"]["value"] == "1" and rows["small[1]"]["value"] == "2"
    assert rows["o.a599"].get("error")
    run(capsys, "session", "close")


def test_timeouts_are_checked_before_anything_runs(workspace, capsys):
    program = workspace / "pagination_bug.py"
    code, out = run(capsys, "session", "start", program, "--at", "8", "--timeout", "500")
    assert code == 2 and "--timeout" in out.err
    assert json.loads(run(capsys, "session", "list", "--json")[1].out) == []
    assert run(capsys, "session", "start", program, "--at", "8")[0] == 0
    code, out = run(capsys, "session", "continue", "--timeout", "0")
    assert code == 2 and "--timeout" in out.err
    code, out = run(capsys, "session", "get", "start", "--json")
    assert code == 0 and json.loads(out.out)[0]["value"] == "3"  # still stopped
    assert json.loads(run(capsys, "session", "list", "--json")[1].out) == ["default"]
    run(capsys, "session", "close")


def test_long_python_lists_are_not_reported_missing(workspace, capsys):
    program = workspace / "long.py"
    program.write_text("xs = list(range(200))\nprint(len(xs))\n")
    code, out = run(
        capsys, "probe", program, "--at", "2", "--capture", "xs[5],xs[150]", "--json", "--no-save"
    )
    rows = {row["selector"]: row for row in json.loads(out.out)["rows"]}
    assert code == 0 and rows["xs[5]"]["value"] == "5"
    assert "beyond the debugger's visible window" in rows["xs[150]"]["error"]


def test_describe_reports_python_syntax_errors_as_request_errors(workspace, capsys):
    program = workspace / "bad.py"
    program.write_text("x = 1\ndef broken(:\n")
    code, out = run(capsys, "describe", program, "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "INVALID_REQUEST" and "line 2" in error["message"]


def test_selectors_share_one_inspection_unless_budget_runs_out():
    from peeky.probe import _capture

    class Worker:
        def __init__(self):
            self.inspects, self.budget = 0, 2

        def dispatch(self, operation, args):
            if operation == "inspect":
                self.inspects += 1
                self.budget = 2
                child = {"name": "c", "value": "9", "variablesReference": 0}
                variables = [{"name": n, "value": n, "variablesReference": 0} for n in "abcde"] + [
                    {"name": "o", "value": "O", "variablesReference": 7}
                ]
                self.child = child
                return {"frames": [{"line": 3}], "scopes": [{"variables": variables}]}
            self.budget -= 1
            if self.budget < 0:
                raise ValueError("Expansion node budget exhausted; inspect again")
            return {"variables": [self.child], "truncated": False}

    worker = Worker()
    rows = _capture(worker, {"stop_id": "s"}, ("a", "b", "c", "d", "e"), 1, "observed")
    assert worker.inspects == 1 and [r["value"] for r in rows] == list("abcde")
    worker = Worker()
    rows = _capture(worker, {"stop_id": "s"}, ("o.c",) * 4, 1, "observed")
    assert [r.get("value") for r in rows] == ["9"] * 4 and worker.inspects == 2


def test_a_shared_inspection_never_makes_a_readable_value_an_error(workspace, capsys):
    program = workspace / "spent.py"
    program.write_text("a = list(range(300))\nitems = list(range(60))\nprint(len(a))\n")
    selectors = ",".join(["a[10]"] * 7 + ["items[-1]", "items[59]"])
    code, out = run(
        capsys, "probe", program, "--at", "3", "--capture", selectors, "--json", "--no-save"
    )
    rows = {row["selector"]: row for row in json.loads(out.out)["rows"]}
    assert code == 0 and rows["items[-1]"].get("value") == "59", rows["items[-1]"]
    assert rows["items[59]"]["value"] == "59"


def test_an_interrupted_start_never_blocks_its_name(workspace, capsys):
    sessions = workspace / "state" / "sessions"
    sessions.mkdir(parents=True, mode=0o700)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    record = sessions / "default.json"
    record.write_text(json.dumps({"name": "default", "starting": dead.pid}))
    record.chmod(0o600)
    code, out = run(capsys, "session", "inspect", "--json")
    assert code == 2 and json.loads(out.out)["error"]["code"] == "NO_SESSION"
    code, out = run(capsys, "session", "close", "--forget", "--json")
    assert code == 0 and json.loads(out.out) == {"forgot": "default", "worker_contacted": False}
    record.write_text(json.dumps({"name": "default", "starting": dead.pid}))
    record.chmod(0o600)
    # A new start takes over the abandoned claim.
    code, out = run(capsys, "session", "start", workspace / "pagination_bug.py", "--at", "8")
    assert code == 0, out.err
    run(capsys, "session", "close")
    record.write_text(json.dumps({"name": "default", "starting": os.getpid()}))
    record.chmod(0o600)
    code, out = run(capsys, "session", "start", workspace / "pagination_bug.py", "--at", "8")
    assert code == 2 and "exists" in out.err  # a live start keeps its claim
    assert run(capsys, "session", "close", "--forget")[0] == 0
