"""Launch preparation and DAP stages share a budget; cleanup gets its own grace."""

import json
import sys
import threading
import time
from urllib.error import HTTPError

import pytest

from peeky.adapters import PythonAdapter
from peeky.client import Client
from peeky.dap import DAP
from peeky.worker import make_server


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
        launch_budget_s = 0.08

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
import json, sys, time
from pathlib import Path

def emit(value):
    body = json.dumps(value).encode()
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()

while True:
    header = sys.stdin.buffer.readline()
    if not header:
        break
    length = int(header.split(b":")[1])
    assert sys.stdin.buffer.readline() == b"\r\n"
    request = json.loads(sys.stdin.buffer.read(length))
    command = request["command"]
    if command == "disconnect":
        Path(sys.argv[1]).write_text("disconnect received after budget expiry")
    else:
        time.sleep(0.08 if command == "first" else 1.0)
    emit({"type": "response", "request_seq": request["seq"], "success": True, "body": {"command": command}})
    if command == "event":
        emit({"type": "event", "event": "initialized"})
"""


@pytest.mark.parametrize("second_stage", ["response", "event"])
def test_real_dap_process_shares_absolute_deadline_and_disconnects_after_expiry(
    tmp_path, second_stage
):
    script = tmp_path / "adapter.py"
    script.write_text(FAKE_ADAPTER)
    disconnected = tmp_path / "disconnected"
    dap = DAP((sys.executable, str(script), str(disconnected)))
    try:
        started = time.monotonic()
        dap.deadline = started + 0.3
        assert dap.request("first")["command"] == "first"
        sequence = dap.send(second_stage)
        with pytest.raises(TimeoutError):
            if second_stage == "event":
                dap.wait_event({"initialized"}, timeout=10)
            else:
                dap.response(sequence, timeout=10)
        assert time.monotonic() - started < 0.8
        # A new stage cannot restart the budget once the absolute deadline expired.
        with pytest.raises(TimeoutError):
            dap.send("must-not-send", timeout=10)
    finally:
        dap.close()
    assert disconnected.read_text() == "disconnect received after budget expiry"
    assert dap.process.poll() is not None
    assert not dap.reader.is_alive()
    assert dap.deadline is None
