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
    dap.output_count = 0
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


def test_a_run_of_continued_events_is_one_entry_and_never_evicts_stops():
    # LLDB says `continued` after each of its own stops in a step out of a
    # deep recursion (a breakpoint each deeper call returns past); 2,000 of them evicted
    # the step's own stop and failed the session.
    dap = transport()
    dap._accept({"type": "event", "event": "stopped", "body": {"reason": "breakpoint"}})
    for _ in range(3 * MAX_CONTROL_EVENTS):
        dap._accept({"type": "event", "event": "continued"})
    dap._accept({"type": "event", "event": "stopped", "body": {"reason": "step"}})
    assert len(dap.events) == 3
    run = dap.events[1]
    assert (run["since"], run["cursor"], run["repeated"]) == (2, 3 * MAX_CONTROL_EVENTS + 1, 6000)
    assert dap.wait_event({"stopped"}, after=1)["body"]["reason"] == "step"
    # A resume after a consumer's cursor is still seen as one.
    assert dap.events_between(5, dap.event_sequence, {"continued"}) == [run]
    assert dap.recent_events()["recent"][1]["repeated"] == 6000


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


def test_a_refusal_carries_the_debuggers_own_reason():
    from peeky.dap import RequestFailed

    dap = transport()
    dap._accept(
        {
            "type": "response",
            "request_seq": 3,
            "success": False,
            "message": "Debugger request failed",
            "body": {"error": {"id": 1, "format": "Cannot change the value of a constant"}},
        }
    )
    with pytest.raises(RequestFailed, match="^Cannot change the value of a constant$"):
        dap.response(3, timeout=0)
    dap._accept({"type": "response", "request_seq": 4, "success": False, "message": "notStopped"})
    with pytest.raises(RequestFailed, match="^notStopped$"):
        dap.response(4, timeout=0)


ECHO_ADAPTER = r"""
import json, sys
def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
def write(body):
    data = json.dumps(body).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    sys.stdout.buffer.flush()
while True:
    request = read()
    # java-debug follows every stackTrace with a telemetry event.
    write({"seq": 0, "type": "event", "event": "telemetry", "body": {}})
    write({"seq": 0, "type": "response", "request_seq": request["seq"], "success": True,
           "command": request["command"], "body": {}})
"""


def test_bounded_polls_and_their_notes_never_flood_the_trace(monkeypatch):
    # The trace is how a lost event is diagnosed; Peeky's own lost-stop polls
    # (hundreds of requests each) must not evict it.
    import subprocess
    import sys

    popen = subprocess.Popen

    def spawn(_command, **kwargs):
        return popen([sys.executable, "-c", ECHO_ADAPTER], **kwargs)

    monkeypatch.setattr("peeky.dap.subprocess.Popen", spawn)
    dap = DAP()
    try:
        dap.request("threads", timeout=5)
        for poll in range(3):
            for thread in range(100):
                dap.request("stackTrace", {"threadId": thread}, timeout=5, trace=False)
            dap.annotate("lost-stop poll", checked=100, poll=poll)
        names = [(entry["dir"], entry["name"]) for entry in dap.trace]
        assert names == [("out", "threads"), ("in", "threads"), ("peeky", "lost-stop poll")]
        assert (dap.trace[-1]["times"], dap.trace[-1]["poll"]) == (3, 2)
        assert dap.ignored_events["telemetry"] == 301 and not dap.untraced
    finally:
        dap.close()


def test_threads_a_thread_event_says_started_are_kept():
    # A second thread started since launch means the program runs
    # several, whatever a `threads` reply says.
    dap = transport()
    for thread in (1, 2, 2):
        dap._accept(
            {"type": "event", "event": "thread", "body": {"reason": "started", "threadId": thread}}
        )
    dap._accept({"type": "event", "event": "thread", "body": {"reason": "exited", "threadId": 3}})
    assert dap.started_threads == {1, 2}


# A debugger whose last messages follow the program's end. `late`: `exited`,
# `terminated`, then output, all before Peeky disconnects; `exit-later`: after
# `terminated`, `exited` comes once it answered; `noisy`: after it answered, text of its
# own (lldb-dap 18 on Linux prints its crash as `stderr` output); `answering`: that text
# while it handles the disconnect, before its answer; `crash`: it exits with status 2 on
# its own once the program ended, before Peeky ends its input (it may have dropped output
# it held), and `crash1` with status 1; `dies`: it answers the disconnect and exits with
# status 2 at once, as Peeky checks it and ends its input; `provoked`: it exits 1 only
# once Peeky ended its input, as debugpy can while it still handles the disconnect;
# `provoked-bare` exits so with nothing on stderr, `provoked-other` with another failure
# there (not debugpy's known one, whose signature `provoked` prints); `open`: its stream
# stays open after the end; `running`: the program never ends. `lldb` reports the end as
# lldb-dap does (`exited`, then `terminated`), and `lldb-terminated` with `terminated`
# alone; both then print a crash on `stderr` once they answered the disconnect, and abort,
# as lldb-dap 18 does on Linux. Any adapter can: none is named.
ENDING_ADAPTER = r"""
import json, sys, time
mode = sys.argv[1]
def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
def write(body):
    data = json.dumps(body).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    sys.stdout.buffer.flush()
def event(name, **body):
    write({"seq": 0, "type": "event", "event": name, "body": body})
def output(text, category="stdout"):
    event("output", category=category, output=text)
output("early\n")
if mode == "late":
    event("exited", exitCode=0)
    event("terminated")
    time.sleep(0.3)
    output("late\n")
elif mode == "lldb":
    event("exited", exitCode=0)
    event("terminated")
elif mode != "running":
    event("terminated")
if mode in ("crash", "crash1"):
    sys.exit(2 if mode == "crash" else 1)
while (request := read()) is not None:
    if mode == "answering" and request["command"] == "disconnect":
        output("free(): invalid pointer\n", "stderr")
    write({"seq": 0, "type": "response", "request_seq": request["seq"], "success": True,
           "command": request["command"], "body": {}})
    if request["command"] == "disconnect":
        if mode == "dies":
            sys.exit(2)
        time.sleep(0.3)
        if mode == "exit-later":
            event("exited", exitCode=7)
        elif mode == "noisy":
            output("free(): invalid pointer\n", "stderr")
        elif mode.startswith("lldb"):
            output("free(): invalid pointer\n", "stderr")
            import os
            os.abort()
if mode == "open":
    time.sleep(60)
if mode.startswith("provoked"):
    with open(sys.argv[2]) as said:
        sys.stderr.write(said.read())
    sys.stderr.flush()
    sys.exit(1)
"""


# What debugpy 1.8.22's adapter prints on stderr when Peeky ends its input as it answers
# `disconnect` and it exits 1 (Linux, Docker), the lines its signature checks kept.
DEBUGPY_PROVOKED = """E+00001.227: /handling #11 request "disconnect" from Client[1]/
             Handler 'Client.disconnect_request' (file '/v/debugpy/adapter/components.py', line 700)
             couldn't handle #11 request "disconnect" from Client[1]:

             Traceback (most recent call last):
               File "/v/debugpy/adapter/components.py", line 97, in lock_and_handle
                 return f(self, message)
               File "/v/debugpy/adapter/clients.py", line 715, in disconnect_request
                 servers.stop_serving()
               File "/v/debugpy/adapter/servers.py", line 435, in stop_serving
                 sessions.report_sockets()
               File "/v/debugpy/adapter/sessions.py", line 298, in report_sockets
                 client.report_sockets()
               File "/v/debugpy/adapter/clients.py", line 743, in report_sockets
                 self.channel.send_event(
               File "/v/debugpy/common/messaging.py", line 296, in write_json
                 raise NoMoreMessages(stream=self)
             debugpy.common.messaging.NoMoreMessages: No more messages

             During handling of the above exception, another exception occurred:

             Traceback (most recent call last):
               File "/v/debugpy/common/messaging.py", line 717, in respond
                 assert self.response is None
             AssertionError
"""


@pytest.mark.parametrize(
    "mode, debugpy, ended, drained, output, code, after, status",
    [
        ("late", False, True, True, "early\nlate\n", 0, "", 0),
        ("exit-later", False, True, True, "early\n", 7, "", 0),
        ("noisy", False, True, False, "early\n", None, "free(): invalid pointer\n", None),
        # What comes from the disconnect on is not the program's, even
        # before the adapter's answer.
        ("answering", False, True, False, "early\n", None, "free(): invalid pointer\n", None),
        # A clean end of its stream, then a failed exit, is not whole: on
        # its own before Peeky ends its input, or in the moment it does so (any adapter's
        # status 2, debugpy's included), or after (status 1, not debugpy's).
        ("crash", False, True, False, "early\n", None, "", 2),
        ("crash", True, True, False, "early\n", None, "", 2),
        ("crash1", True, True, False, "early\n", None, "", 1),
        ("dies", False, True, False, "early\n", None, "", 2),
        ("dies", True, True, False, "early\n", None, "", 2),
        ("provoked", False, True, False, "early\n", None, "", 1),
        # The one exit Peeky is known to provoke: debugpy's 1 as its input ends, with the
        # failure it prints then. Without that signature, a status 1 is a crash.
        ("provoked", True, True, True, "early\n", None, "", 1),
        ("provoked-bare", True, True, False, "early\n", None, "", 1),
        ("provoked-other", True, True, False, "early\n", None, "", 1),
        ("open", False, True, False, "early\n", None, "", None),
        # An `exited` event alone never makes the output whole, whatever
        # the adapter; only a clean end of its stream does. lldb-dap 18 on Linux, and any
        # other adapter that prints after the disconnect and aborts, leave it partial.
        ("lldb", False, True, False, "early\n", 0, "free(): invalid pointer\n", None),
        ("lldb-terminated", False, True, False, "early\n", None, "free(): invalid pointer\n", None),
        ("running", False, False, False, "early\n", None, "", None),
    ],
)
def test_close_reads_the_output_to_the_adapters_end(
    tmp_path, monkeypatch, mode, debugpy, ended, drained, output, code, after, status
):
    # Output can follow `terminated`, and `exited` can come after it: what close
    # leaves is whole only once the adapter ended its stream with nothing more after
    # Peeky's disconnect, never a guess; what came after is kept apart.
    import subprocess
    import sys
    import time

    import peeky.dap as dap_module

    monkeypatch.setattr(dap_module, "DRAIN_TIMEOUT", 1.0 if mode == "open" else 10.0)
    popen = subprocess.Popen

    from peeky.adapters import PythonAdapter

    said = tmp_path / "stderr.txt"
    said.write_text(
        DEBUGPY_PROVOKED
        if mode == "provoked"
        else DEBUGPY_PROVOKED.replace("report_sockets", "propagate_output")
        if mode == "provoked-other"
        else ""
    )

    def spawn(_command, **kwargs):
        return popen([sys.executable, "-c", ENDING_ADAPTER, mode, str(said)], **kwargs)

    monkeypatch.setattr("peeky.dap.subprocess.Popen", spawn)
    dap = DAP(provoked_exit=PythonAdapter.provoked_exit if debugpy else None)
    try:
        dap.wait_event({"terminated"} if ended else {"never"}, 0, 5)
    except TimeoutError:
        assert not ended
    # Closed once what the program wrote has come: in `late`, output after `terminated`.
    deadline = time.monotonic() + 5
    while dap.snapshot_output()[0] != output and time.monotonic() < deadline:
        time.sleep(0.01)
    if mode in ("crash", "crash1"):
        # Its exit comes before Peeky closes the session: its own (never reaped here).
        while dap._adapter_state() == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
    started = time.monotonic()
    dap.close(terminate=False)
    final = dap.final
    assert (final["ended"], final["drained"]) == (ended, drained)
    assert (final["output"], final["exit_code"], final["after_disconnect"]) == (output, code, after)
    # The stream ended cleanly in each case with an exit status the adapter chose; only
    # an exit before Peeky ended its input counts (debugpy exits 1 when its
    # input ends while it still handles the disconnect).
    assert final["stream_drained"] is (status is not None)
    if status is not None:
        assert final["adapter_exit"] == status
    if mode.startswith(("crash", "provoked")):
        assert final["own_exit"] is mode.startswith("crash")
    assert final["exit_provoked"] is (mode == "provoked" and debugpy)
    # The wait is for the adapter's end, never a fixed sleep: it stops when the end comes.
    assert time.monotonic() - started < (6 if mode == "open" else 8)
    assert dap.process.poll() is not None and not dap.reader.is_alive()


def test_only_an_end_between_frames_is_the_streams_end():
    from peeky.dap import StreamEnd

    with pytest.raises(StreamEnd):
        read_message(io.BytesIO(b""))
    for cut in (b"Content-Length: 2\r\n", b"Content-Length: 5\r\n\r\n{}"):
        with pytest.raises(EOFError) as raised:
            read_message(io.BytesIO(cut))
        assert not isinstance(raised.value, StreamEnd)


def test_an_announcing_thread_is_listed_only_once_started(monkeypatch):
    # close(), on another thread, joins every listed one: an unstarted one can't be joined.
    import types

    from peeky import dap as dap_module

    dap = DAP.__new__(DAP)
    dap.announcing = []
    dap.announce_lock = threading.Lock()
    listed_at_start = []

    class Recording:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            listed_at_start.append(self in dap.announcing)

    monkeypatch.setattr(dap_module, "threading", types.SimpleNamespace(Thread=Recording))
    dap._announce(12345)
    assert listed_at_start == [False]
    assert len(dap.announcing) == 1
