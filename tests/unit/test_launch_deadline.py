"""Launch preparation and DAP stages share a budget; cleanup gets its own grace.

Ported from 339def3 and adapted to main's single worker-owned launch deadline
(``DebugWorker.launch_deadline_s``, passed to ``supports_deadline`` adapters and
to every DAP launch phase as the remaining budget) instead of an adapter-declared
``launch_budget_s`` and a transport-level ``DAP.deadline``.
"""

import json
import sys
import threading
import time
from urllib.error import HTTPError

import pytest

from peeky.adapters import PythonAdapter
from peeky.client import Client
from peeky.dap import DAP
from peeky.worker import DebugWorker, make_server


@pytest.mark.parametrize("returns_late", [False, True])
def test_http_prepare_deadline_rejects_without_session_or_runtime_leak(
    tmp_path, monkeypatch, returns_late
):
    source = tmp_path / "target.py"
    source.write_text("print('never launched')\n")
    prepared = []
    cleaned = []
    deadlines = []

    class Runtime(PythonAdapter):
        def close(self):
            cleaned.append(self)

    class SlowTarget(PythonAdapter):
        supports_deadline = True

        def prepare(self, program, root, *, deadline):
            deadlines.append(deadline)
            time.sleep(max(0, deadline - time.monotonic()) + 0.01)
            if not returns_late:
                raise TimeoutError("bounded preparation timed out")
            runtime = Runtime()
            prepared.append(runtime)
            return runtime

    def forbidden_dap(*args, **kwargs):
        pytest.fail("An expired preparation must never start the debugger")

    monkeypatch.setattr("peeky.worker.DAP", forbidden_dap)
    server = make_server(tmp_path, "deadline-test-token-123456", 0, adapter=SlowTarget())
    server.worker.launch_deadline_s = 0.08
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", "deadline-test-token-123456")
        started = time.monotonic()
        with pytest.raises(HTTPError) as caught:
            client.call("launch", program="target.py", breakpoints=[])
        elapsed = time.monotonic() - started
        assert caught.value.code == 500
        assert "error" in json.load(caught.value)
        assert 0 < deadlines[0] - started < 0.3
        assert elapsed < 2, "HTTP launch must fail well before the client's 45-second timeout"
        assert server.worker.session is None
        assert server.worker.runtime is None and server.worker.dap is None
        assert cleaned == prepared
    finally:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


FAKE_ADAPTER = r"""
import json, sys, threading, time
from pathlib import Path

log, disconnected, stage = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
lock = threading.Lock()

def emit(value):
    body = json.dumps(value).encode()
    with lock:
        sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        sys.stdout.buffer.flush()

def later(value):
    # Reply long after the launch budget, while still serving later requests promptly.
    timer = threading.Timer(3.0, emit, (value,))
    timer.daemon = True
    timer.start()

while True:
    header = sys.stdin.buffer.readline()
    if not header:
        break
    length = int(header.split(b":")[1])
    assert sys.stdin.buffer.readline() == b"\r\n"
    request = json.loads(sys.stdin.buffer.read(length))
    command = request["command"]
    with log.open("a") as stream:
        stream.write(command + "\n")
    if command == "disconnect":
        disconnected.write_text("disconnect received after budget expiry")
    if command == "initialize":
        time.sleep(0.7)  # An earlier stage consumes most of the shared 1.0 s budget.
    response = {"type": "response", "request_seq": request["seq"], "success": True,
                "command": command, "body": {}}
    if command == "setBreakpoints" and stage == "response":
        later(response)
        continue
    emit(response)
    if command == "launch":
        event = {"type": "event", "event": "initialized"}
        later(event) if stage == "event" else emit(event)
"""


@pytest.mark.parametrize("second_stage", ["response", "event"])
def test_real_dap_process_shares_absolute_deadline_and_disconnects_after_expiry(
    tmp_path, monkeypatch, second_stage
):
    script = tmp_path / "adapter.py"
    script.write_text(FAKE_ADAPTER)
    log, disconnected = tmp_path / "requests.log", tmp_path / "disconnected"
    closed, instances = [], []

    class Target(PythonAdapter):
        @property
        def command(self):
            return (sys.executable, str(script), str(log), str(disconnected), second_stage)

        def close(self):
            closed.append(self)

    class RecordingDAP(DAP):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

    monkeypatch.setattr("peeky.worker.DAP", RecordingDAP)
    (tmp_path / "target.py").write_text("pass\n")
    worker = DebugWorker(tmp_path, adapter=Target())
    worker.launch_deadline_s = 1.0
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        worker.dispatch("launch", {"program": "target.py"})
    # Shared budget: the later stage gets only the ~0.3 s left, so the launch fails
    # near the 1.0 s deadline. A per-stage budget would take at least 0.7 + 1.0 s.
    assert time.monotonic() - started < 1.5
    # A new stage cannot restart the budget once the absolute deadline expired.
    expired = ["initialize", "launch"] + (["setBreakpoints"] if second_stage == "response" else [])
    assert log.read_text().split() == [*expired, "disconnect"]
    (dap,) = instances
    assert disconnected.read_text() == "disconnect received after budget expiry"
    assert dap.process.poll() is not None
    assert not dap.reader.is_alive()
    assert len(closed) == 1
    assert worker.session is None and worker.runtime is None and worker.dap is None


def test_a_debuggee_slow_to_start_has_the_launchs_whole_deadline(tmp_path, monkeypatch):
    # #356: each launch step was capped at 15 s inside the launch's 40 s deadline, so a
    # debuggee that a loaded machine started in 20 s failed the launch with half of its
    # time left: "did not start: the debugger never initialized it within the launch's
    # 15 s" (reproduced with debugpy on one CPU shared with 32 busy loops).
    script = tmp_path / "adapter.py"
    script.write_text(FAKE_ADAPTER)
    log, disconnected = tmp_path / "requests.log", tmp_path / "disconnected"

    class Target(PythonAdapter):
        @property
        def command(self):
            return (sys.executable, str(script), str(log), str(disconnected), "prompt")

    waits = []

    class SlowStart(DAP):
        def wait_event(self, names, after=0, timeout=15, **kwargs):
            if "initialized" in names:
                waits.append(timeout)
                # The debuggee initializes 20 s after the launch request: a shorter wait
                # times out (the fake adapter sends the event at once).
                if timeout < 20:
                    raise TimeoutError("No matching debugger event before deadline")
            return super().wait_event(names, after, timeout, **kwargs)

    monkeypatch.setattr("peeky.worker.DAP", SlowStart)
    (tmp_path / "target.py").write_text("pass\n")
    worker = DebugWorker(tmp_path, adapter=Target())
    assert worker.launch_deadline_s == 40
    try:
        launched = worker.dispatch("launch", {"program": "target.py"})
        assert launched["state"] == "running"
        assert len(waits) == 1 and 35 < waits[0] <= 40
        assert log.read_text().split() == [
            "initialize",
            "launch",
            "setBreakpoints",
            "configurationDone",
        ]
    finally:
        worker.close()
