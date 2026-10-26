"""A crash stop's summary and refusal in the worker (#342): nothing of a frame outside the
program's sources is read, the nearest frame of its own code is named, and a mismatch of
that frame with the prepared build or its module still fails closed. Only a fatal signal
that the program neither handles nor ignores is a crash (review of #342)."""

import signal
from types import SimpleNamespace

import pytest

import peeky.worker as worker_module
from peeky.cli import _state_text
from peeky.errors import classify
from peeky.probe import crash_message, crash_text
from peeky.worker import DebugWorker, _signal_dispositions, _stop_signals


class Stack:
    def __init__(self, frames, exception="signal", pid=4242):
        self.frames, self.exception, self.process_pid = frames, exception, pid

    def request(self, command, arguments, timeout=None):
        if command == "exceptionInfo":
            return {"exceptionId": self.exception}
        assert command == "stackTrace", command
        return {"stackFrames": self.frames[: arguments["levels"]]}


class Runtime:
    def __init__(self, workspace, lines, module=True):
        self.prepared = SimpleNamespace(workspace=workspace, source_root=workspace)
        self.lines, self.module, self.checked = lines, module, []

    def validate_stop(self, frame):
        self.checked.append(frame["name"])
        if frame["line"] not in self.lines:
            raise RuntimeError("Native stopped line does not match prepared symbols")

    def validate_debugger(self, dap, frame):
        self.checked.append(f"module of {frame['name']}")
        if not self.module:
            raise RuntimeError("Native module identity or symbols do not match prepared build")


def bit(name):
    return 1 << (int(getattr(signal, name)) - 1)


@pytest.fixture(autouse=True)
def no_handlers(monkeypatch):
    """The program handles and ignores no signal, unless a test says otherwise."""
    monkeypatch.setattr(worker_module, "_signal_dispositions", lambda pid: (0, 0))


def frames(workspace, line=12):
    library = [
        {"id": i, "name": name, "source": {"path": f"/usr/lib/libc.so.6`{name}"}, "line": 0}
        for i, name in enumerate(
            ["__pthread_kill", "abort", "std::terminate()", "__cxa_throw", "reserve"]
        )
    ]
    main = {"id": 9, "name": "main", "source": {"path": str(workspace / "abort.cpp")}, "line": line}
    return [*library, main]


@pytest.fixture
def worker(tmp_path):
    (tmp_path / "abort.cpp").write_text("int main() {}\n")
    worker = DebugWorker(tmp_path)
    worker.thread_id, worker.stop_reason = 1, "exception"
    return worker


def test_a_crash_names_its_description_and_the_programs_nearest_frame(worker, tmp_path):
    runtime = Runtime(tmp_path, [12])
    worker.dap, worker.runtime = Stack(frames(tmp_path)), runtime
    crash, crashed = worker.read_crash(runtime, {"description": "signal SIGABRT"})
    assert crashed is True
    assert crash == {
        "description": "signal SIGABRT",
        "kind": "signal",
        "outside_program": True,
        "uncaught_exception": True,
        "frame": 5,
        "function": "main",
        "location": f"{tmp_path}/abort.cpp:12",
    }
    # Only the program's own frame is checked against the build and its module; no library
    # frame is.
    assert runtime.checked == ["main", "module of main"]
    worker.stop_signal = worker.stop_crash = crash
    assert worker.crash_refusal(frames(tmp_path)[0], 0) == (
        "Nothing in frame 0 (__pthread_kill) is read: it is outside the program's sources, "
        "where the program crashed (signal SIGABRT). Frame 5, main at abort.cpp:12, is the "
        "nearest of your code: `session inspect --frame 5`"
    )
    assert crash_text(crash).endswith(
        "signal SIGABRT, outside the program's sources, in a call from main at "
        f"{tmp_path}/abort.cpp:12, after a C++ exception that nothing caught"
    )
    shown = {**crash, "location": "abort.cpp:12"}
    assert crash_message(shown, 0, "abort.cpp:20") == (
        "The program crashed before it stopped at abort.cpp:20: signal SIGABRT, outside the "
        "program's sources, in a call from main at abort.cpp:12, after a C++ exception that "
        "nothing caught. Peeky ended it there, so it has no exit status"
    )
    state = {"state": "stopped", "location": "/usr/lib/libc.so.6`__pthread_kill:3", "crash": shown}
    assert _state_text({**state, "reason": "exception"}).endswith(
        "after a C++ exception that nothing caught; `session inspect --frame 5` reads that "
        "frame of your code]"
    )


def test_a_mismatch_in_the_programs_own_frame_still_fails_closed(worker, tmp_path):
    # A stale build: the program's frame is on a line the prepared build doesn't have.
    runtime = Runtime(tmp_path, [12])
    worker.dap, worker.runtime = Stack(frames(tmp_path, line=13)), runtime
    with pytest.raises(RuntimeError, match="does not match prepared symbols"):
        worker.read_crash(runtime, {"description": "signal SIGABRT"})


def test_a_module_mismatch_of_the_programs_own_frame_is_a_debugger_error(worker, tmp_path):
    # The frame's line is the build's, but its module isn't the prepared binary (review of
    # #342: `read_crash` named it after `validate_stop` alone).
    runtime = Runtime(tmp_path, [12], module=False)
    worker.dap = Stack(frames(tmp_path))
    with pytest.raises(RuntimeError) as failure:
        worker.read_crash(runtime, {"description": "signal SIGABRT"})
    error = classify(failure.value)
    assert error["code"] == "DEBUGGER_ERROR" and error["session_closed"] is True, error
    assert error["message"] == "Native module identity or symbols do not match prepared build"
    assert runtime.checked == ["main", "module of main"]


@pytest.mark.parametrize(
    "description, exception, handled, crashed",
    [
        ("signal SIGABRT", "signal", None, True),
        ("hit program assert", "signal", None, True),
        ("EXC_BAD_ACCESS (code=1, address=0x0)", "exception", None, True),
        ("signal SIGSEGV: address not mapped to object (fault address: 0x0)", None, None, True),
        # A handler, or an ignored signal, may let the program go on.
        ("signal SIGABRT", "signal", ("caught", "SIGABRT"), False),
        (
            "signal SIGSEGV: invalid address (fault address: 0x0)",
            "signal",
            ("ignored", "SIGSEGV"),
            False,
        ),
        ("EXC_BAD_ACCESS (code=2, address=0x10)", "exception", ("caught", "SIGBUS"), False),
        # Masks that can't be read prove nothing.
        ("signal SIGABRT", "signal", "unknown", False),
        # Not a fatal signal, or not a signal: never a crash.
        ("signal SIGINT", "signal", None, False),
        ("signal SIGPIPE", "signal", None, False),
        ("signal SIGTRAP", "signal", None, False),
        ("EXC_BREAKPOINT (code=1, subcode=0x1)", "exception", None, False),
        ("C++ Throw", "cpp_throw", None, False),
        ("C++ Catch", "cpp_catch", None, False),
        # A filter's stop, whatever its text says.
        ("signal SIGABRT", "cpp_throw", None, False),
    ],
)
def test_only_a_fatal_signal_the_program_neither_handles_nor_ignores_is_a_crash(
    worker, tmp_path, monkeypatch, description, exception, handled, crashed
):
    if handled == "unknown":
        monkeypatch.setattr(worker_module, "_signal_dispositions", lambda pid: None)
    elif handled:
        kind, name = handled
        masks = (bit(name), 0) if kind == "caught" else (0, bit(name))
        monkeypatch.setattr(worker_module, "_signal_dispositions", lambda pid: masks)
    runtime = Runtime(tmp_path, [12])
    worker.dap = Stack(frames(tmp_path), exception=exception)
    crash, verdict = worker.read_crash(runtime, {"description": description})
    assert verdict is crashed
    assert crash["description"] == description and crash["frame"] == 5, crash
    # The refusal says the program crashed only where it did.
    worker.stop_signal = crash
    worker.stop_crash = crash if crashed else None
    refusal = worker.crash_refusal(frames(tmp_path)[0], 0)
    happened = "crashed" if crashed else "stopped"
    assert f"outside the program's sources, where the program {happened} (" in refusal


def test_signal_names_and_this_processs_own_masks():
    assert _stop_signals("signal SIGABRT") == ("SIGABRT",)
    assert _stop_signals("signal SIGSEGV: invalid address (fault address: 0x0)") == ("SIGSEGV",)
    assert _stop_signals("hit program assert") == ("SIGABRT",)
    assert _stop_signals("EXC_BAD_ACCESS (code=1, address=0x0)") == ("SIGSEGV", "SIGBUS")
    assert _stop_signals("EXC_ARITHMETIC (code=1)") == ("SIGFPE",)
    assert _stop_signals("C++ Throw") == _stop_signals("breakpoint 1.1") == ()
    assert _signal_dispositions(None) is None and _signal_dispositions(1) is None


def test_the_masks_of_a_real_process_say_what_it_handles_and_ignores():
    import os
    import subprocess
    import sys

    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        pytest.skip("Peeky reads signal masks on Linux and macOS")
    script = (
        "import signal, sys\n"
        "signal.signal(signal.SIGUSR1, lambda *a: None)\n"
        "signal.signal(signal.SIGUSR2, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "sys.stdin.read()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline() == "ready\n"
        masks = _signal_dispositions(child.pid)
    finally:
        child.stdin.close()
        child.wait(timeout=10)
    assert masks is not None, os.uname()
    caught, ignored = masks
    assert caught & bit("SIGUSR1") and not ignored & bit("SIGUSR1")
    assert ignored & bit("SIGUSR2") and not caught & bit("SIGUSR2")
    assert not (caught | ignored) & bit("SIGSEGV")


def test_terminate_alone_or_a_crash_with_no_frame_of_the_program_says_so(worker, tmp_path):
    runtime = Runtime(tmp_path, [12])
    called = [f for f in frames(tmp_path) if f["name"] != "__cxa_throw"]
    worker.dap = Stack(called)
    crash, crashed = worker.read_crash(runtime, {})
    assert "uncaught_exception" not in crash and "description" not in crash, crash
    assert crashed is False
    worker.dap = Stack(called[:-1])
    crash, crashed = worker.read_crash(runtime, {"description": "signal SIGSEGV"})
    worker.stop_signal = worker.stop_crash = crash
    assert crashed is True
    assert crash == {"description": "signal SIGSEGV", "kind": "signal", "outside_program": True}
    assert worker.crash_refusal(called[1], 1) == (
        "Nothing in frame 1 (abort) is read: it is outside the program's sources, on the stack "
        "of its stop (signal SIGSEGV), and none of its first 64 frames is in your code: "
        "`session report` shows its output"
    )
    assert crash_text(crash) == (
        "signal SIGSEGV, outside the program's sources, with no frame of its code among the "
        "first 64"
    )


class Events:
    """A DAP whose all-threads stop also stopped thread 2 at a breakpoint (or didn't)."""

    def __init__(self, queued):
        self.queued, self.sent = queued, []

    def event_cursor(self):
        return 7

    def events_between(self, start, end, kinds):
        stops = [{"event": "stopped", "cursor": 6, "body": {"reason": "breakpoint", "threadId": 2}}]
        return [e for e in (stops if self.queued else []) if e["event"] in kinds]

    def request(self, command, arguments, timeout=None):
        self.sent.append(command)
        return {"allThreadsContinued": True}


@pytest.mark.parametrize("queued", [True, False])
def test_continue_says_whether_it_resumed_the_program(tmp_path, queued):
    # Review of #348: a probe counts a signal stop as let go on only where it did.
    worker = DebugWorker(tmp_path)
    worker.runtime = SimpleNamespace(assert_current=lambda: None)
    worker.dap = Events(queued)
    worker.state, worker.stop_id, worker.thread_id, worker.cursor = "stopped", "s1", 1, 5
    worker.stop_all_threads, worker.breakpoint_counts = True, {"main.c": 1}
    result = worker._dispatch("continue", {"stop_id": "s1"})
    # A queued stop of another thread is served next, and nothing is sent to resume.
    assert result["resumed"] is (not queued), result
    assert ("continue" in worker.dap.sent) is (not queued), worker.dap.sent
