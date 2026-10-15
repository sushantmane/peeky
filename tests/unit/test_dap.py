import collections
import io
import json
import threading

import pytest

from peeky.dap import DAP, MAX_CONTROL_EVENTS, MAX_FRAME_BYTES, read_message


def transport():
    dap = DAP.__new__(DAP)
    dap.condition = threading.Condition()
    dap.responses = {}
    dap.abandoned = set()
    dap.events = collections.deque(maxlen=MAX_CONTROL_EVENTS)
    dap.ignored_events = {}
    dap.output = collections.deque(maxlen=500)
    dap.output_truncated = False
    dap.event_sequence = 0
    dap.error = None
    return dap


@pytest.mark.parametrize(
    "data",
    [
        b"Content-Length: -1\r\n\r\n",
        f"Content-Length: {MAX_FRAME_BYTES + 1}\r\n\r\n".encode(),
        b"X: " + b"a" * 8192 + b"\r\n",
        b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        b"Content-Length: 5\r\n\r\n{}",
    ],
)
def test_rejects_unbounded_or_incomplete_frames(data):
    with pytest.raises((ValueError, EOFError)):
        read_message(io.BytesIO(data))


def test_reads_frame():
    payload = json.dumps({"type": "event", "event": "stopped"}).encode()
    assert (
        read_message(io.BytesIO(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload))[
            "event"
        ]
        == "stopped"
    )


def test_failed_launch_response_interrupts_initialized_wait():
    dap = transport()
    dap._accept(
        {"type": "response", "request_seq": 7, "success": False, "message": "invalid launch"}
    )
    with pytest.raises(RuntimeError, match="invalid launch"):
        dap.wait_event({"initialized"}, timeout=0, failed_request=7)


def test_output_flood_does_not_evict_lifecycle_events():
    dap = transport()
    dap._accept({"type": "event", "event": "stopped"})
    for _ in range(3000):
        dap._accept(
            {"type": "event", "event": "output", "body": {"category": "stdout", "output": "x"}}
        )
    dap._accept({"type": "event", "event": "terminated"})
    assert dap.wait_event({"stopped"})["cursor"] == 1
    assert dap.wait_event({"terminated"}, after=1)["cursor"] == 2
    assert dap.snapshot_output() == ("x" * 500, True)


def test_control_overflow_is_explicit():
    dap = transport()
    for _ in range(MAX_CONTROL_EVENTS + 1):
        dap._accept({"type": "event", "event": "breakpoint"})
    with pytest.raises(RuntimeError, match="overflow"):
        dap.wait_event({"stopped"})


@pytest.fixture
def wedged_adapter(monkeypatch):
    """A real child that keeps both pipes open but never reads a request."""
    import os
    import subprocess
    import sys

    if os.name != "posix":
        pytest.skip("Pipe deadline support is POSIX-only")
    popen = subprocess.Popen

    def spawn(_command, **kwargs):
        return popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)

    monkeypatch.setattr("peeky.dap.subprocess.Popen", spawn)
    dap = DAP()
    try:
        yield dap
    finally:
        dap.close()


def test_wedged_pipe_times_out_and_poisoned_transport_cannot_retry(wedged_adapter):
    import time

    dap = wedged_adapter
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="pipe write timed out"):
        dap.send("setVariable", {"value": "x" * (MAX_FRAME_BYTES // 2)}, timeout=0.1)
    assert time.monotonic() - started < 2
    with pytest.raises(RuntimeError, match="pipe write timed out"):
        dap.send("continue")
    dap.close()
    assert dap.process.poll() is not None
    assert not dap.reader.is_alive()
    assert "pipe write timed out" in dap.error


def test_close_has_deadline_even_when_disconnect_pipe_is_full(wedged_adapter):
    import os
    import time

    dap = wedged_adapter
    while True:
        try:
            os.write(dap.process.stdin.fileno(), b"x" * 65536)
        except BlockingIOError:
            break
    started = time.monotonic()
    dap.close()
    assert time.monotonic() - started < 2
    assert dap.process.poll() is not None
    assert not dap.reader.is_alive()
    dap.close()  # Idempotent cleanup must not signal an already-reaped process group.


def test_waiting_writer_does_not_block_event_delivery(wedged_adapter, monkeypatch):
    import os
    import time

    dap = wedged_adapter
    errors = []
    started = threading.Event()
    original_write = os.write

    def partial_write(fd, data):
        count = original_write(fd, data)
        if fd == dap.process.stdin.fileno():
            started.set()
        return count

    monkeypatch.setattr("peeky.dap.os.write", partial_write)

    def send():
        try:
            dap.send("setVariable", {"value": "x" * (MAX_FRAME_BYTES // 2)}, timeout=0.5)
        except TimeoutError as exc:
            errors.append(exc)

    sender = threading.Thread(target=send)
    sender.start()
    assert started.wait(1)
    beginning = time.monotonic()
    dap._accept({"type": "event", "event": "stopped"})
    assert dap.wait_event({"stopped"}, timeout=0.1)["event"] == "stopped"
    assert time.monotonic() - beginning < 0.25
    sender.join(timeout=2)
    assert not sender.is_alive() and len(errors) == 1


def test_oversized_outgoing_frame_is_rejected_before_pipe_write(wedged_adapter):
    with pytest.raises(ValueError, match="Outgoing DAP frame"):
        wedged_adapter.send("launch", {"data": "x" * MAX_FRAME_BYTES})
    assert wedged_adapter.error is None


def test_large_frame_is_delivered_intact_across_partial_pipe_writes(monkeypatch):
    import os
    import subprocess
    import sys

    if os.name != "posix":
        pytest.skip("Pipe deadline support is POSIX-only")
    script = """
import json, sys
while True:
    first = sys.stdin.buffer.readline()
    if not first:
        break
    size = int(first.split(b":")[1])
    assert sys.stdin.buffer.readline() == b"\\r\\n"
    request = json.loads(sys.stdin.buffer.read(size))
    body = {"type": "response", "request_seq": request["seq"], "success": True,
            "body": {"length": len(request["arguments"].get("value", ""))}}
    data = json.dumps(body).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(data)}\\r\\n\\r\\n".encode()+data)
    sys.stdout.buffer.flush()
"""
    popen = subprocess.Popen

    def spawn(_command, **kwargs):
        return popen([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr("peeky.dap.subprocess.Popen", spawn)
    dap = DAP()
    try:
        assert dap.request("setVariable", {"value": "x" * 1048576}) == {"length": 1048576}
    finally:
        dap.close()


def test_timed_out_responses_are_discarded_and_drain_is_observable():
    from peeky.dap import ResponseTimeout

    dap = transport()
    with pytest.raises(ResponseTimeout):
        dap.response(9, timeout=0)
    assert dap.abandoned == {9} and dap.drained(0) is False
    dap._accept({"type": "response", "request_seq": 9, "success": True, "body": {}})
    assert 9 not in dap.responses and dap.drained(0) is True


def test_thread_churn_cannot_evict_lifecycle_events():
    dap = transport()
    dap._accept({"type": "event", "event": "stopped", "body": {"threadId": 1}})
    for _ in range(MAX_CONTROL_EVENTS * 3):
        dap._accept({"type": "event", "event": "thread", "body": {"reason": "started"}})
    assert dap.wait_event({"stopped"}, timeout=0)["cursor"] == 1
    assert dap.ignored_events == {"thread": MAX_CONTROL_EVENTS * 3}


def test_ignored_event_names_are_bounded():
    dap = transport()
    for index in range(200):
        dap._accept({"type": "event", "event": f"custom{index}"})
    assert len(dap.ignored_events) == 65 and dap.ignored_events["<other>"] == 136


def test_non_dap_lines_on_the_protocol_stream_fail_with_a_clear_message():
    import io

    from peeky.dap import read_message

    stream = io.BytesIO(b"Picked up JAVA_TOOL_OPTIONS -Xmx1g\r\n\r\n")
    with pytest.raises(ValueError, match="non-DAP line .*Picked up"):
        read_message(stream)
    stream = io.BytesIO(b"Warning: something odd\r\n\r\n")
    with pytest.raises(ValueError, match="no Content-Length header"):
        read_message(stream)
