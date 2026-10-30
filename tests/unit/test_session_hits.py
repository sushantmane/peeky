"""Session hit numbers: classified from where the session's breakpoints are, never guessed."""

import time

import pytest

from peeky import cli
from peeky.worker import DebugWorker


def stop(worker, line, reason="breakpoint"):
    worker.stop_reason, worker.stop_line, worker.stop_hit = reason, line, None
    worker.classify_hit()
    return worker.stop_hit


def test_hits_count_the_sessions_own_breakpoints_and_never_a_to_line(tmp_path):
    worker = DebugWorker(tmp_path)
    worker.record_own_points([{"line": 14}], {"breakpoints": [{"id": 1, "line": 14}]})
    assert stop(worker, 14) == 1
    # `continue --to 9`: threads stop at line 9. LLDB serves the others before
    # resuming; java-debug serves them after the next continue. Neither is a hit.
    worker.temporary_lines.add(9)
    assert [stop(worker, 9) for _ in range(4)] == [None, None, None, None]
    assert stop(worker, 14) == 2
    # A crash first, then a co-stop at the --to line: still not a hit.
    assert stop(worker, 30, reason="exception") is None and stop(worker, 9) is None
    assert stop(worker, 14) == 3 and stop(worker, 9, reason="step") is None


def test_a_to_line_where_the_debugger_placed_the_sessions_breakpoint_is_a_hit(tmp_path):
    worker = DebugWorker(tmp_path)
    # Requested on a comment line (3); the debugger placed it on line 2.
    worker.record_own_points([{"line": 3}], {"breakpoints": [{"id": 7, "line": 2}]})
    worker.temporary_lines.add(2)
    assert stop(worker, 2) == 1


def test_an_unclassifiable_stop_ends_numbering_never_guessed(tmp_path):
    worker = DebugWorker(tmp_path)
    worker.record_own_points([{"line": 5}], {"breakpoints": [{"id": 1, "line": 5}]})
    # No --to line ever used: a stop whose line could not be read is the session's own.
    assert stop(worker, None) == 1 and stop(worker, 5) == 2
    worker.temporary_lines.add(8)
    assert stop(worker, None) is None and worker.hits is None
    assert stop(worker, 5) is None  # never numbered again in this session


def test_a_stop_at_a_line_peeky_did_not_place_ends_numbering(tmp_path):
    # debugpy reports a program's own `breakpoint()` call with reason "breakpoint".
    worker = DebugWorker(tmp_path)
    worker.record_own_points([{"line": 3}], {"breakpoints": [{"id": 1, "line": 3}]})
    assert stop(worker, 3) == 1
    assert stop(worker, 6) is None and worker.hits is None and stop(worker, 3) is None


class Scripted:
    """A worker client whose `wait` replies come from a script."""

    def __init__(self, *states):
        self.states, self.calls = list(states), []

    def call(self, operation, **arguments):
        self.calls.append(operation)
        return self.states.pop(0) if operation == "wait" else {}


def numbered(stop_id, hit=None, reason="breakpoint"):
    state = {"state": "stopped", "stop_id": stop_id, "reason": reason, "numbered": True}
    return {**state, **({"hit": hit} if hit is not None else {})}


@pytest.fixture
def record(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return {"name": "t", "session_id": "s", "stop_id": "a", "state": "stopped"}


def test_counting_stops_at_a_crash_and_never_resumes_it(record):
    client = Scripted(numbered("b", hit=3), numbered("c", reason="exception"))
    deadline = time.monotonic() + 30
    state, reached, complete = cli._run_on(
        record, client, numbered("a", hit=2), 3, deadline, "stop 4"
    )
    assert (state["reason"], reached, complete) == ("exception", 1, False)
    assert state["note"] == "stopped for another reason before stop 4 (shown above); last hit 3"
    assert client.calls == ["continue", "wait", "continue", "wait"]


def test_a_deadline_short_of_the_target_says_how_far_it_got(record):
    client = Scripted()
    state, reached, complete = cli._run_on(
        record, client, numbered("a", hit=2), 5, time.monotonic() - 1, "hit 7"
    )
    assert not complete and client.calls == []
    assert state["note"] == "reached 0 of 5 stops before --timeout (hit 7); last hit 2"
