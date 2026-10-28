"""#356 review: a debugger stalled at a stop. A read of the stop that times out is never
dropped as if the stop were whole, and no read keeps a stop paused past its pause budget
(--max-pause), which the pause guard cannot enforce while a request holds the lock."""

import threading
import time

import pytest

from peeky.dap import ResponseTimeout
from peeky.errors import classify
from peeky.worker import STOP_READ_S, DebugWorker

FRAME = {"id": 1, "name": "main", "line": 5, "instructionPointerReference": "0x104"}


class StalledDap:
    """lldb-dap at a breakpoint stop, as the worker sees it. With ``stall``, it never
    answers `stackTrace`: each such request waits its whole timeout (``sleep``) or times
    out at once, as DAP.request does. Without, it answers with FRAME."""

    def __init__(self, stall=True, sleep=False):
        self.stall, self.sleep = stall, sleep
        self.events, self.calls, self.closed = [], [], None

    def add(self, event, **body):
        cursor = len(self.events) + 1
        self.events.append(
            {"event": event, "cursor": cursor, "received": time.monotonic(), "body": body}
        )

    def wait_event(self, names, after, timeout):
        for event in self.events:
            if event["cursor"] > after and event["event"] in names:
                return event
        time.sleep(min(timeout, 0.05))
        raise TimeoutError("poll expired")

    def request(self, command, args=None, timeout=15, **kwargs):
        self.calls.append((command, timeout, time.monotonic()))
        if command == "stackTrace":
            if not self.stall:
                return {"stackFrames": [FRAME], "totalFrames": 1}
            if self.sleep:
                time.sleep(timeout)
            raise ResponseTimeout("Debugger response timed out; outcome may be uncertain")
        if command == "continue":
            return {"allThreadsContinued": True}
        raise AssertionError(command)

    def event_cursor(self):
        return len(self.events)

    def events_between(self, after, upto, names):
        return [e for e in self.events if after < e["cursor"] <= upto and e["event"] in names]

    def pending_stop(self, after):
        for event in self.events:
            if event["cursor"] > after and event["event"] == "stopped":
                return event["received"], event["body"].get("reason")
        return None

    def snapshot_output(self):
        return "", False

    def recent_events(self):
        return {}

    def close(self, terminate=True):
        self.closed = {"terminate": terminate}


class Runtime:
    manifest_id = "m"

    def __init__(self):
        self.asked = []

    def assert_current(self):
        pass

    def close(self):
        pass


def stopped(tmp_path, dap, max_pause=None):
    """A session whose program just stopped at its breakpoint on line 5."""
    worker = DebugWorker(tmp_path)
    worker.dap, worker.runtime = dap, Runtime()
    worker.session, worker.state, worker.cursor = "session", "running", 0
    worker.own_lines, worker.max_pause = {5}, max_pause
    dap.add("stopped", reason="breakpoint", threadId=7, allThreadsStopped=True)
    return worker


@pytest.mark.parametrize("stalled", ["frame", "places"])
def test_a_stop_the_debugger_does_not_describe_in_time_fails_and_is_never_shown_whole(
    tmp_path, stalled
):
    # Review of #356: a `stackTrace` that timed out was dropped, so `wait` returned a live
    # stop with no line, function or `place` (a probe's row with no `stop`), as if that
    # were all of it; so was where the stop's breakpoint locations are, asked again at it.
    dap = StalledDap(stall=stalled == "frame")
    worker = stopped(tmp_path, dap)
    deadlines = []
    if stalled == "places":

        def locations(dap, ids, deadline=None):
            deadlines.append(deadline - time.monotonic())
            raise ResponseTimeout("Debugger response timed out; outcome may be uncertain")

        worker.runtime.breakpoint_locations = locations
        # Listed before the program loaded: the first stop asks again.
        worker.places, worker.places_unresolved = {5: [("main", (0x100, 0x104))]}, True
        placed = {"breakpoints": [{"id": 1, "line": 5, "verified": True}]}
        worker.places_asked = (worker.runtime, [{"line": 5}], placed)
    with pytest.raises(TimeoutError) as caught:
        worker.dispatch("wait", {"session_id": "session", "timeout": 1})
    error = classify(caught.value)
    assert error["code"] == "TIMEOUT" and error["session_closed"] is True, error
    assert worker.dap is None and dap.closed == {"terminate": True}
    (asked,) = [timeout for command, timeout, _ in dap.calls if command == "stackTrace"]
    assert asked == STOP_READ_S == 15
    if stalled == "places":
        assert len(deadlines) == 1 and 14 < deadlines[0] <= 15


def test_a_stalled_read_never_holds_a_stop_past_its_pause_budget(tmp_path):
    # Review of #356: `wait` holds the worker's lock while it reads the stop, and the pause
    # guard needs that lock, so a stalled `stackTrace` kept the program paused for the
    # whole read (2 s, then 15) past --max-pause, then returned it as a live stop.
    dap = StalledDap(sleep=True)
    worker = stopped(tmp_path, dap, max_pause=0.4)
    received = dap.events[0]["received"]
    done = threading.Event()

    def guard():
        while not done.is_set():
            worker.guard_pause()
            time.sleep(0.02)

    guarding = threading.Thread(target=guard, daemon=True)
    guarding.start()
    try:
        state = worker.dispatch("wait", {"session_id": "session", "timeout": 1})
    finally:
        done.set()
        guarding.join(timeout=5)
    (resumed,) = [at for command, _, at in dap.calls if command == "continue"]
    assert resumed - received < 0.4 + 0.5, resumed - received
    # The read waited no longer than the budget left.
    ((_, asked, _),) = [call for call in dap.calls if call[0] == "stackTrace"]
    assert asked <= 0.4
    # Resumed, never returned as a live stop; the record says the place was not read.
    assert state["state"] == "running" and "stop_id" not in state, state
    assert state["auto_resumed"] == 1 and state["last_auto_resumed"]["location"] is None
    assert worker.failed is None and worker.dap is dap
    from peeky.cli import _state_text

    assert "latest at a place not read in time, after" in _state_text(state)
