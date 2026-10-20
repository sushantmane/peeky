"""The adapter's stderr: kept (bounded) for diagnosis, and a dead java-debug event thread
(#27) ends the session at once instead of a silent wait."""

import json
import sys
import time

import pytest

from peeky.adapters import PythonAdapter
from peeky.dap import DAP, STDERR_TAIL
from peeky.errors import classify
from peeky.java import JavaSession
from peeky.worker import DebugWorker, SessionFailed

DEATH = (
    'Exception in thread "Event Hub" com.sun.jdi.ObjectCollectedException\n'
    "\tat jdk.jdi/com.sun.tools.jdi.ThreadReferenceImpl.name(ThreadReferenceImpl.java:192)\n"
    "\tat com.microsoft.java.debug.core.EventHub.lambda$start$0(EventHub.java:71)\n"
    "\tat java.base/java.lang.Thread.run(Thread.java:840)\n"
)

# A minimal java-debug stand-in: answers every request, and after `configurationDone`
# (or at once, with "now") writes the given text to its stderr. "lines:N" makes it write
# N log lines itself: Linux caps one argument at 128 KiB (MAX_ARG_STRLEN). How it writes:
# "plain" (one write), "bytewise" (a byte at a time), "exit" (then exits at once), or
# "orphan" (a descendant outside its process group keeps writing to the same stderr).
FAKE_ADAPTER = r"""
import json, os, subprocess, sys, threading, time
mode, stderr = sys.argv[1], sys.argv[2]
how = sys.argv[3] if len(sys.argv) > 3 else "plain"
if how == "orphan":
    subprocess.Popen(
        ["sh", "-c", "i=0; while [ $i -lt 30 ]; do i=$((i+1)); echo tick >&2; sleep 0.1; done"],
        start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    )
if stderr.startswith("lines:"):
    stderr = "".join(f"line {n:06d} " + "x" * 90 + "\n" for n in range(int(stderr[6:])))
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
def complain():
    time.sleep(0.3)
    if how == "bytewise":
        for byte in stderr.encode():
            sys.stderr.buffer.write(bytes([byte]))
            sys.stderr.buffer.flush()
            time.sleep(0.001)
        return
    sys.stderr.write(stderr)
    sys.stderr.flush()
    if how == "exit":
        os._exit(3)
if mode == "now":
    threading.Thread(target=complain).start()
while True:
    request = read()
    command, body = request["command"], {}
    if command in ("launch", "attach"):
        write({"seq": 0, "type": "event", "event": "initialized", "body": {}})
    if command == "setBreakpoints":
        points = request["arguments"].get("breakpoints", [])
        body = {"breakpoints": [{"id": 1, "verified": True, "line": p["line"]} for p in points]}
    write({"seq": 0, "type": "response", "request_seq": request["seq"], "success": True,
           "command": command, "body": body})
    if command == "configurationDone" and mode == "after-start":
        threading.Thread(target=complain).start()
"""


def adapter(stderr, mode="now", fatal=JavaSession.fatal_stderr, how="plain"):
    command = [sys.executable, "-c", FAKE_ADAPTER, mode, stderr, how]
    return DAP(command=command, fatal_stderr=fatal)


HUB_DIED = (
    'java-debug\'s event thread died (Exception in thread "Event Hub" '
    "com.sun.jdi.ObjectCollectedException at "
    "com.microsoft.java.debug.core.EventHub.lambda$start$0(EventHub.java:71)); it "
    "reports no more stops, steps or exits, so the session cannot continue: close it "
    "and retry"
)


def test_a_dead_event_hub_fails_a_waiting_transport_at_once_with_its_stack():
    dap = adapter("INFO: attached\n" + DEATH)
    try:
        began = time.monotonic()
        with pytest.raises(RuntimeError) as failure:
            dap.wait_event({"stopped", "terminated"}, 0, 10)
        assert time.monotonic() - began < 2
        message = str(failure.value)
        # The line and java-debug's own frame (the JDK's frames come first).
        assert message == HUB_DIED and dap.fatal == HUB_DIED
        error = classify(failure.value)
        assert (error["code"], error["session_closed"]) == ("DEBUGGER_ERROR", True)
        assert "rebuild the adapter with scripts/setup_java_adapter.py" in error["hint"]
        # Nothing but `disconnect` goes out any more, and the log is kept for diagnosis.
        with pytest.raises(RuntimeError, match="event thread died"):
            dap.send("threads")
        stderr = dap.recent_events()["stderr"]
        assert "INFO: attached" in stderr and "EventHub.java:71" in stderr
    finally:
        dap.close()
    assert dap.process.poll() is not None and not dap.stderr_reader.is_alive()


def test_a_death_line_without_stack_frames_still_fails_the_transport():
    dap = adapter('Exception in thread "Event Hub" java.lang.NullPointerException\n')
    try:
        with pytest.raises(RuntimeError, match=r"died \(Exception in thread .*NullPointer"):
            dap.wait_event({"stopped"}, 0, 10)
    finally:
        dap.close()


@pytest.mark.parametrize(
    "stderr, fatal",
    [
        # Other threads' failures and java-debug's own logged (caught) errors are noise.
        (
            'Exception in thread "main" java.lang.Error\nSEVERE: Exception on recording event\n',
            None,
        ),
        # Only runtimes that declare the line fatal (Java) react to it.
        (DEATH, {}),
    ],
)
def test_other_stderr_never_ends_the_session(stderr, fatal):
    dap = adapter(stderr, fatal=JavaSession.fatal_stderr if fatal is None else fatal)
    try:
        with pytest.raises(TimeoutError):
            dap.wait_event({"stopped"}, 0, 1.5)
        assert dap.error is None and stderr.splitlines()[0] in dap.recent_events()["stderr"]
    finally:
        dap.close()


def test_the_stderr_tail_is_bounded():
    dap = adapter("lines:2000")  # about 200 KB, written by the adapter itself
    try:
        deadline = time.monotonic() + 5
        while "line 001999" not in dap.recent_events()["stderr"] and time.monotonic() < deadline:
            time.sleep(0.05)
        events = dap.recent_events()
        assert len(events["stderr"]) <= STDERR_TAIL and events["stderr_truncated"] is True
        assert events["stderr"].endswith("line 001999 " + "x" * 90)
    finally:
        dap.close()


def fake_java(mode):
    class Runtime(PythonAdapter):
        fatal_stderr = JavaSession.fatal_stderr
        dap_request = mode
        terminate_debuggee = mode == "launch"

        @property
        def command(self):
            return (sys.executable, "-c", FAKE_ADAPTER, "after-start", DEATH)

    return Runtime()


@pytest.mark.parametrize("mode", ["attach", "launch"])
def test_a_dead_event_hub_ends_the_wait_and_closes_the_session_as_errors_do(
    tmp_path, monkeypatch, mode
):
    closes = []
    real = DAP.close
    monkeypatch.setattr(
        DAP, "close", lambda dap, terminate=True: closes.append(terminate) or real(dap, terminate)
    )
    (tmp_path / "Main.py").write_text("pass\n")
    worker = DebugWorker(tmp_path, adapter=fake_java(mode))
    started = worker.dispatch("launch", {"program": "Main.py", "breakpoints": [{"line": 1}]})
    dap, began = worker.dap, time.monotonic()
    with pytest.raises(RuntimeError) as failure:
        while time.monotonic() - began < 10:
            worker.dispatch("wait", {"session_id": started["session_id"], "timeout": 2})
    assert time.monotonic() - began < 2
    assert classify(failure.value)["code"] == "DEBUGGER_ERROR"
    assert str(failure.value).startswith("java-debug's event thread died")
    # Detached (attach) or terminated (launch), exactly as any debugger failure.
    assert worker.dap is None and worker.session is None and closes == [mode == "launch"]
    assert dap.process.poll() is not None
    # The session is kept as a failed one (as for a lost stop): it repeats its error,
    # reports its final state with the adapter's log, and closes.
    session = {"session_id": started["session_id"]}
    with pytest.raises(SessionFailed, match="event thread died") as again:
        worker.dispatch("wait", {**session, "timeout": 1})
    assert str(again.value) == str(failure.value)
    report = worker.dispatch("report", session)
    assert report["state"] == "closed" and report["adapter_failure"] == str(failure.value)
    assert "EventHub.java:71" in report["debugger_events"]["stderr"]
    assert [e["operation"] for e in report["history"]][-1] == "adapter_failed"
    assert worker.dispatch("close", session) == {"state": "idle"}
    with pytest.raises(ValueError, match="Missing or stale session_id"):
        worker.dispatch("wait", {**session, "timeout": 1})


def test_reports_carry_the_adapters_log(tmp_path):
    (tmp_path / "Main.py").write_text("pass\n")

    class Quiet(PythonAdapter):
        fatal_stderr = JavaSession.fatal_stderr
        dap_request = "attach"

        @property
        def command(self):
            return (sys.executable, "-c", FAKE_ADAPTER, "now", "INFO: Attaching succeeded.\n")

    worker = DebugWorker(tmp_path, adapter=Quiet())
    started = worker.dispatch("launch", {"program": "Main.py", "breakpoints": []})
    try:
        time.sleep(0.6)
        report = worker.dispatch("report", {"session_id": started["session_id"]})
        assert report["debugger_events"]["stderr"] == "INFO: Attaching succeeded."
        assert json.dumps(report)
    finally:
        worker.close()


def test_a_probe_on_a_dead_event_hub_fails_with_the_adapters_log(tmp_path):
    from peeky.probe import Probe, run_probe

    (tmp_path / "Main.py").write_text("pass\n")
    result = run_probe(
        tmp_path, Probe("Main.py", 1, ("x",), timeout=20), adapter=fake_java("attach")
    )
    assert result["status"] == "failed" and result["seconds"] < 5
    assert (result["error"]["code"], result["error"]["message"]) == ("DEBUGGER_ERROR", HUB_DIED)
    stderr = result["debugger_events"]["stderr"]
    assert 'Exception in thread "Event Hub"' in stderr and "EventHub.java:71" in stderr


@pytest.mark.parametrize(
    "stderr, how",
    [
        # Deep in an overlong line, past the 4 KiB kept per line, in one write.
        ("z" * 6000 + DEATH, "plain"),
        # Written a byte at a time: the line is only ever complete across reads.
        (DEATH, "bytewise"),
        # The last line has no newline, and the adapter exits right after it.
        (DEATH.rstrip("\n"), "exit"),
    ],
    ids=["after-6000-bytes-of-the-same-line", "byte-by-byte", "no-final-newline-then-exit"],
)
def test_the_death_is_found_however_it_is_written(stderr, how):
    dap = adapter(stderr, how=how)
    try:
        with pytest.raises(RuntimeError) as failure:
            dap.wait_event({"stopped"}, 0, 10)
        assert str(failure.value) == HUB_DIED
    finally:
        dap.close()


def test_an_adapter_that_exits_after_its_death_line_reports_the_death_not_eof():
    # Without a trace either: the stdout EOF ("adapter closed") must not win.
    dap = adapter('Exception in thread "Event Hub" java.lang.IllegalStateException', how="exit")
    try:
        with pytest.raises(RuntimeError, match=r"^java-debug's event thread died \(Exception"):
            dap.wait_event({"stopped"}, 0, 10)
    finally:
        dap.close()


def test_a_last_line_without_a_newline_is_kept():
    dap = adapter("INFO: one\nINFO: last words", how="exit")
    try:
        with pytest.raises(RuntimeError, match="Debugger adapter closed"):
            dap.wait_event({"stopped"}, 0, 10)
        dap.stderr_reader.join(timeout=2)
        assert dap.recent_events()["stderr"] == "INFO: one\nINFO: last words"
    finally:
        dap.close()


def test_close_never_leaves_a_reader_on_a_descriptor_a_descendant_still_holds():
    # Review round 3: a process outside the adapter's group kept the inherited stderr
    # open, close() gave up joining the blocked reader and closed the descriptor under
    # it, and once the number was reused the stale reader took the new owner's data.
    import os

    dap = adapter("INFO: started\n", how="orphan")
    dap.request("threads", {}, timeout=5)
    fd = dap.process.stderr.fileno()
    began = time.monotonic()
    dap.close()
    assert time.monotonic() - began < 2 and not dap.stderr_reader.is_alive()
    held, (read_end, write_end) = [], os.pipe()
    try:
        while read_end < fd and len(held) < 64:
            held.append((read_end, write_end))
            read_end, write_end = os.pipe()
        if read_end != fd:
            pytest.skip(f"descriptor {fd} was not reused (got {read_end})")
        time.sleep(0.3)  # the descendant keeps writing to the old pipe meanwhile
        os.write(write_end, b"PAYLOAD-FOR-THE-NEW-OWNER\n")
        time.sleep(0.3)
        os.set_blocking(read_end, False)
        assert os.read(read_end, 100) == b"PAYLOAD-FOR-THE-NEW-OWNER\n"
        assert "PAYLOAD" not in dap.recent_events()["stderr"]
    finally:
        for pair in [*held, (read_end, write_end)]:
            for end in pair:
                os.close(end)
