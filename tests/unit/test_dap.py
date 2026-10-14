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
    dap.events = collections.deque(maxlen=MAX_CONTROL_EVENTS)
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
        dap._accept({"type": "event", "event": "thread"})
    with pytest.raises(RuntimeError, match="overflow"):
        dap.wait_event({"stopped"})
