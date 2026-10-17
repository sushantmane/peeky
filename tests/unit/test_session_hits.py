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
    # `continue --to 9`: four threads stop at line 9 together; only the first stop sees
    # the temporary line, the others are served after it is cleared.
    worker.temporary_line = 9
    assert stop(worker, 9) is None
    worker.temporary_line = None
    assert [stop(worker, 9) for _ in range(3)] == [None, None, None]
    # A thread from the same stop that is at the session's line is a hit.
    assert stop(worker, 14) == 2
    worker.served_temporary = None  # a real resume
    assert stop(worker, 14) == 3 and stop(worker, 9, reason="step") is None


def test_a_to_line_where_the_debugger_placed_the_sessions_breakpoint_is_a_hit(tmp_path):
    worker = DebugWorker(tmp_path)
    # Requested on a comment line (3); the debugger placed it on line 2.
    worker.record_own_points([{"line": 3}], {"breakpoints": [{"id": 7, "line": 2}]})
    worker.temporary_line = 2
    assert stop(worker, 2) == 1
    # A later `breakpoint` event moving an own breakpoint adds its new line.
    worker.own_lines.discard(2)
    worker.record_own_points([], {"breakpoints": [{"id": 7, "line": 2}]})
    assert 2 in worker.own_lines


def test_an_unreadable_location_ends_numbering_only_when_it_could_be_a_to_line(tmp_path):
    worker = DebugWorker(tmp_path)
    worker.record_own_points([{"line": 5}], {"breakpoints": [{"id": 1, "line": 5}]})
    # No `--to` line pending: every breakpoint stop is the session's.
    assert stop(worker, None) == 1
    worker.temporary_line = 8
    assert stop(worker, None) is None and worker.hits is None
    worker.temporary_line = None
    assert stop(worker, 5) is None  # never guessed again in this session


class Scripted:
    """A worker client whose `wait` replies come from a script."""

    def __init__(self, *states):
        self.states, self.calls = list(states), []

    def call(self, operation, **arguments):
        self.calls.append(operation)
        return self.states.pop(0) if operation == "wait" else {}


def numbered(stop_id, hit=None, reason="breakpoint", numbered=True):
    state = {"state": "stopped", "stop_id": stop_id, "reason": reason}
    if numbered is not None:
        state["numbered"] = numbered
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


def test_workers_without_hit_numbers_count_by_reason_and_refuse_hit_n(record):
    client = Scripted(numbered("b", numbered=None), numbered("c", numbered=None))
    deadline = time.monotonic() + 30
    state, reached, complete = cli._run_on(
        record, client, numbered("a", numbered=None), 2, deadline, "stop 3"
    )
    assert (reached, complete) == (2, True)
    client = Scripted(numbered("a", numbered=None))
    state, complete = cli._until_hit(record, client, 4, 30)
    assert not complete and "the worker predates hit numbers" in state["note"]
    assert client.calls == ["wait"]


def test_a_deadline_short_of_the_target_says_how_far_it_got(record):
    client = Scripted()
    state, reached, complete = cli._run_on(
        record, client, numbered("a", hit=2), 5, time.monotonic() - 1, "hit 7"
    )
    assert not complete and client.calls == []
    assert state["note"] == "reached 0 of 5 stops before --timeout (hit 7); last hit 2"
