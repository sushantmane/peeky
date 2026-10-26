"""A crash stop's summary and refusal in the worker (#342): nothing of a frame outside the
program's sources is read, the nearest frame of its own code is named, and a mismatch of
that frame with the prepared build still fails closed."""

from types import SimpleNamespace

import pytest

from peeky.cli import _state_text
from peeky.probe import crash_message, crash_text
from peeky.worker import DebugWorker


class Stack:
    def __init__(self, frames):
        self.frames = frames

    def request(self, command, arguments, timeout=None):
        assert command == "stackTrace", command
        return {"stackFrames": self.frames[: arguments["levels"]]}


class Runtime:
    def __init__(self, workspace, lines):
        self.prepared = SimpleNamespace(workspace=workspace, source_root=workspace)
        self.lines, self.checked = lines, []

    def validate_stop(self, frame):
        self.checked.append(frame["name"])
        if frame["line"] not in self.lines:
            raise RuntimeError("Native stopped line does not match prepared symbols")


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
    crash = worker.read_crash(runtime, {"description": "signal SIGABRT"})
    assert crash == {
        "description": "signal SIGABRT",
        "outside_program": True,
        "uncaught_exception": True,
        "frame": 5,
        "function": "main",
        "location": f"{tmp_path}/abort.cpp:12",
    }
    # Only the program's own frame is checked against the build; no library frame is.
    assert runtime.checked == ["main"]
    worker.stop_crash = crash
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


def test_terminate_alone_or_a_crash_with_no_frame_of_the_program_says_so(worker, tmp_path):
    runtime = Runtime(tmp_path, [12])
    called = [f for f in frames(tmp_path) if f["name"] != "__cxa_throw"]
    worker.dap = Stack(called)
    crash = worker.read_crash(runtime, {})
    assert "uncaught_exception" not in crash and "description" not in crash, crash
    worker.dap = Stack(called[:-1])
    worker.stop_crash = crash = worker.read_crash(runtime, {"description": "signal SIGSEGV"})
    assert crash == {"description": "signal SIGSEGV", "outside_program": True}
    assert worker.crash_refusal(called[1], 1) == (
        "Nothing in frame 1 (abort) is read: it is outside the program's sources, on the stack "
        "of its crash (signal SIGSEGV), and none of its first 64 frames is in your code: "
        "`session report` shows its output"
    )
    assert crash_text(crash) == (
        "signal SIGSEGV, outside the program's sources, with no frame of its code among the "
        "first 64"
    )
