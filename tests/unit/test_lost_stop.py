"""#27: recovering an attached JVM's breakpoint stop whose `stopped` event was lost."""

import time

import pytest

import peeky.worker as worker_module
from peeky.worker import RECOVERED_NOTE, DebugWorker


def at(path, line, name="Demo.twice"):
    return {"id": 100, "name": name, "line": line, "source": {"path": str(path)}}


class PausedDap:
    """java-debug as the worker sees it: threads, one-frame stacks of suspended threads
    (none for running ones), and the stop reports that did arrive."""

    def __init__(self, frames):
        self.frames, self.events, self.calls = dict(frames), [], []
        self.during_stack = self.fail = None

    def add(self, event, **body):
        self.events.append(
            {
                "event": event,
                "cursor": self.event_cursor() + 1,
                "received": time.monotonic(),
                "body": body,
            }
        )

    def wait_event(self, names, after, timeout):
        for event in self.events:
            if event["cursor"] > after and event["event"] in names:
                return event
        raise TimeoutError("poll expired")

    def request(self, command, args=None, timeout=None):
        self.calls.append(command)
        if self.fail:
            raise self.fail
        if command == "threads":
            return {"threads": [{"id": thread, "name": f"t{thread}"} for thread in self.frames]}
        if command == "stackTrace":
            if self.during_stack:
                self.during_stack, during = None, self.during_stack
                during()
            frame = self.frames.get(args["threadId"])
            return {"stackFrames": [frame] if frame else [], "totalFrames": int(bool(frame))}
        if command in ("continue", "next"):
            # java-debug resumes only the given thread.
            self.frames[args["threadId"]] = None
            return {"allThreadsContinued": False}
        raise AssertionError(command)

    def event_cursor(self):
        return max((event["cursor"] for event in self.events), default=0)

    def events_between(self, after, upto, names):
        return [e for e in self.events if after < e["cursor"] <= upto and e["event"] in names]

    def pending_stop(self, after):
        for event in self.events:
            if event["cursor"] > after and event["event"] == "stopped":
                return event["received"], event["body"].get("reason")
        return None

    def close(self, *args, **kwargs):
        pass


class AttachLike:
    recovers_lost_stops = True

    def assert_current(self):
        pass

    def close(self):
        pass


class LaunchLike(AttachLike):
    recovers_lost_stops = False


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "Demo.java"
    path.write_text("class Demo {}\n")
    return path


def paused_worker(source, frames, runtime=None, verified=True, lines=(5,)):
    worker = DebugWorker(source.parent)
    worker.dap, worker.runtime = PausedDap(frames), runtime or AttachLike()
    worker.session, worker.state, worker.cursor = "session", "running", 0
    worker.note_placed(
        source,
        [{"line": line} for line in lines],
        {
            "breakpoints": [
                {"id": number, "line": line, "verified": verified}
                for number, line in enumerate(lines, 1)
            ]
        },
    )
    worker.own_lines = set(lines)
    return worker


def poll(worker):
    worker.lost_stop_polled = 0.0  # as if the poll interval passed since the last one
    return worker.dispatch("wait", {"session_id": "session", "timeout": 0.1})


def operations(worker):
    return [entry["operation"] for entry in worker.audit]


def test_a_thread_paused_at_a_verified_breakpoint_in_two_polls_is_a_recovered_stop(source):
    worker = paused_worker(source, {7: at(source, 5), 8: None})
    assert poll(worker) == {"state": "running", "timed_out": True}
    state = poll(worker)
    assert state["state"] == "stopped" and state["recovered"] is True
    assert (state["reason"], state["hit"], state["numbered"]) == ("breakpoint", 1, True)
    assert state["location"] == f"{source}:5" and worker.thread_id == 7
    assert state["event"]["body"] == {
        "reason": "breakpoint",
        "threadId": 7,
        "allThreadsStopped": False,
        "peeky": RECOVERED_NOTE,
    }
    # No event was consumed: the cursor stays, and one late report is now expected.
    assert worker.cursor == 0 and worker.owed == {7: 1}
    recovery = next(e for e in worker.audit if e["operation"] == "stop_recovered")
    assert recovery["result"]["thread"] == 7 and recovery["result"]["location"] == f"{source}:5"
    # Poll one: threads and both stacks; poll two confirms the suspect first.
    assert worker.dap.calls[:6] == [
        "threads",
        "stackTrace",
        "stackTrace",
        "threads",
        "stackTrace",
        "stackTrace",
    ]
    # Waiting again shows the same stop; a repeat is never a new one.
    again = worker.dispatch("wait", {"session_id": "session", "timeout": 0.1})
    assert again["stop_id"] == state["stop_id"] and again["recovered"] is True


def test_the_pause_counts_from_the_first_sighting(source):
    worker = paused_worker(source, {7: at(source, 5)})
    poll(worker)
    first = worker.suspects[7]["since"]
    state = poll(worker)
    assert state["event"]["received"] == first and worker.stopped_at == first


def test_a_suspect_that_moves_must_be_seen_twice_at_its_new_breakpoint(source):
    worker = paused_worker(source, {7: at(source, 5)}, lines=(5, 9))
    poll(worker)
    worker.dap.frames[7] = at(source, 9)
    assert poll(worker)["state"] == "running"
    assert poll(worker)["location"] == f"{source}:9"


@pytest.mark.parametrize(
    "case",
    [
        "unverified",
        "other line",
        "other file",
        "running",
        "stepping",
        "breakpoints removed",
        "launched",
    ],
)
def test_nothing_else_is_ever_taken_for_a_lost_breakpoint_stop(source, case):
    other = source.parent / "Other.java"
    other.write_text("class Other {}\n")
    frame = {
        "other line": at(source, 6),
        "other file": at(other, 5),
        "running": None,
    }.get(case, at(source, 5))
    worker = paused_worker(
        source,
        {7: frame},
        runtime=LaunchLike() if case == "launched" else None,
        verified=case != "unverified",
    )
    if case == "stepping":
        worker.stepping.add(7)
    if case == "breakpoints removed":
        worker.note_placed(source, [], {"breakpoints": []})
    for _ in range(4):
        assert poll(worker) == {"state": "running", "timed_out": True}
    assert "stop_recovered" not in operations(worker) and not worker.owed
    if case in ("launched", "unverified", "breakpoints removed"):
        assert worker.dap.calls == []  # nothing to look for: no poll at all


def test_only_the_breakpoint_the_thread_is_at_must_be_verified(source):
    # Line 9's breakpoint is verified; line 5's is still pending (its class not loaded).
    worker = paused_worker(source, {7: at(source, 5)}, lines=(5, 9))
    worker.placed[str(source.resolve())][0]["verified"] = False
    for _ in range(3):
        assert poll(worker) == {"state": "running", "timed_out": True}
    assert worker.dap.calls.count("threads") == 3 and not worker.suspects


def test_a_breakpoint_verified_when_its_class_loads_becomes_eligible(source):
    worker = paused_worker(source, {7: at(source, 5)}, verified=False)
    assert poll(worker)["state"] == "running" and worker.dap.calls == []
    worker.dap.add(
        "breakpoint", reason="changed", breakpoint={"id": 1, "verified": True, "line": 5}
    )
    assert poll(worker)["breakpoint_events"]
    assert poll(worker)["recovered"] is True


def test_a_real_report_arriving_during_the_poll_wins(source):
    worker = paused_worker(source, {7: at(source, 5)})
    poll(worker)
    worker.dap.during_stack = lambda: worker.dap.add("stopped", reason="breakpoint", threadId=7)
    state = poll(worker)
    assert state["state"] == "stopped" and "recovered" not in state and state["hit"] == 1
    assert worker.cursor == 1 and not worker.owed
    assert "stop_recovered" not in operations(worker)


def recovered(worker):
    poll(worker)
    state = poll(worker)
    assert state["recovered"] is True
    return {"session_id": "session", "stop_id": state["stop_id"]}


def test_a_late_report_after_the_thread_resumed_is_absorbed_never_a_second_stop(source):
    worker = paused_worker(source, {7: at(source, 5)})
    worker.dispatch("continue", recovered(worker))
    worker.dap.add("stopped", reason="breakpoint", threadId=7)  # the lost report, late
    assert poll(worker) == {"state": "running", "timed_out": True}
    assert worker.hits == 1 and worker.cursor == 1 and not worker.owed
    assert operations(worker).count("late_stop_report") == 1
    # The next report is a real new stop.
    worker.dap.frames[7] = at(source, 5)
    worker.dap.add("stopped", reason="breakpoint", threadId=7)
    state = poll(worker)
    assert (state["hit"], "recovered" in state, worker.cursor) == (2, False, 2)


def test_a_late_report_while_the_recovered_stop_is_current_waits_for_the_next_wait(source):
    worker = paused_worker(source, {7: at(source, 5)})
    stop = recovered(worker)
    worker.dap.add("stopped", reason="breakpoint", threadId=7)
    # The stop stays the recovered one; the report is consumed only once running again.
    assert (
        worker.dispatch("wait", {"session_id": "session", "timeout": 0.1})["stop_id"]
        == (stop["stop_id"])
    )
    worker.dispatch("continue", stop)
    assert poll(worker)["state"] == "running" and worker.hits == 1


def test_a_late_report_of_another_thread_never_blocks_a_step(source):
    worker = paused_worker(source, {8: at(source, 5)})
    worker.dispatch("continue", recovered(worker))  # thread 8, recovered and resumed
    worker.dap.frames[7] = at(source, 5)
    worker.dap.add("stopped", reason="breakpoint", threadId=7)
    state = poll(worker)
    assert worker.thread_id == 7 and "recovered" not in state
    worker.dap.add("stopped", reason="breakpoint", threadId=8)  # 8's late report
    assert not worker.pending_stop_elsewhere()
    worker.dap.add("stopped", reason="breakpoint", threadId=8)  # a real new stop of 8
    assert worker.pending_stop_elsewhere()


def test_a_report_with_another_reason_means_the_late_one_never_comes(source):
    worker = paused_worker(source, {7: at(source, 5)})
    worker.dispatch("step", recovered(worker))
    assert worker.stepping == {7}
    worker.dap.frames[7] = at(source, 6)
    worker.dap.add("stopped", reason="step", threadId=7)
    state = poll(worker)
    assert state["reason"] == "step" and not worker.owed and not worker.stepping
    assert "late_stop_report" not in operations(worker)


def test_a_step_from_a_recovered_stop_survives_its_late_report(source):
    worker = paused_worker(source, {7: at(source, 5)})
    worker.dispatch("step", recovered(worker))
    worker.dap.add("stopped", reason="breakpoint", threadId=7)  # late, before the step's
    worker.dap.frames[7] = at(source, 6)
    worker.dap.add("stopped", reason="step", threadId=7)
    state = poll(worker)
    assert (state["reason"], worker.cursor, worker.hits) == ("step", 2, 1)


def test_polls_wait_after_a_resume_and_run_at_most_once_per_interval(source):
    worker = paused_worker(source, {7: at(source, 5)})
    worker.mark_resumed()
    for _ in range(3):
        worker.dispatch("wait", {"session_id": "session", "timeout": 0.1})
    assert worker.dap.calls == []
    worker.lost_stop_polled -= worker_module.LOST_STOP_POLL_S
    for _ in range(3):
        worker.dispatch("wait", {"session_id": "session", "timeout": 0.1})
    assert worker.dap.calls.count("threads") == 1


def test_each_poll_checks_a_bounded_rotating_window_of_threads(source, monkeypatch):
    monkeypatch.setattr(worker_module, "LOST_STOP_THREADS", 4)
    frames = {thread: None for thread in range(1, 11)}
    frames[10] = at(source, 5)
    worker = paused_worker(source, frames)
    counts = []
    for _ in range(3):
        before = len(worker.dap.calls)
        assert poll(worker)["state"] == "running"
        counts.append(worker.dap.calls[before:].count("stackTrace"))
    assert counts == [4, 4, 4] and 10 in worker.suspects  # threads 9, 10, 1, 2
    assert poll(worker)["recovered"] is True


def test_a_poll_never_outlives_its_time_budget(source, monkeypatch):
    monkeypatch.setattr(worker_module, "LOST_STOP_BUDGET_S", 0.0)
    worker = paused_worker(source, {7: at(source, 5)})
    for _ in range(3):
        assert poll(worker)["state"] == "running"
    assert set(worker.dap.calls) == {"threads"}


@pytest.mark.parametrize("failure", [TimeoutError("slow"), RuntimeError("error reply")])
def test_a_slow_or_failed_poll_is_no_evidence_and_never_closes(source, failure):
    worker = paused_worker(source, {7: at(source, 5)})
    poll(worker)
    worker.dap.fail = failure
    assert poll(worker) == {"state": "running", "timed_out": True}
    assert worker.dap is not None and worker.session == "session" and not worker.suspects
    worker.dap.fail = None
    assert poll(worker)["state"] == "running"  # two fresh sightings are needed again
    assert poll(worker)["recovered"] is True


def test_an_unobserved_lost_stop_is_recovered_and_auto_resumed_by_the_pause_budget(source):
    worker = paused_worker(source, {7: at(source, 5)})
    worker.max_pause = 10
    worker.lost_stop_polled = 0.0
    worker.guard_pause()
    assert worker.state == "running" and 7 in worker.suspects
    worker.lost_stop_polled = 0.0
    worker.guard_pause()
    assert worker.state == "stopped" and worker.stop_recovered
    worker.guard_pause()
    assert worker.state == "stopped"  # paused for less than the budget
    worker.stopped_at -= 10
    worker.guard_pause()
    assert worker.state == "running" and worker.dap.calls[-1] == "continue"
    assert worker.auto_resumed[-1]["reason"] == "breakpoint"


def test_nothing_carries_over_into_the_next_session(source):
    worker = paused_worker(source, {7: at(source, 5)})
    worker.dispatch("continue", recovered(worker))
    worker.stepping.add(9)
    worker.close()
    assert (worker.placed, worker.owed, worker.stepping, worker.suspects) == ({}, {}, set(), {})
    assert not worker.stop_recovered


def test_only_attached_jvms_opt_in():
    from peeky.java import JavaSession
    from peeky.java_attach import JavaAttachSession

    assert JavaAttachSession.recovers_lost_stops is True
    assert getattr(JavaSession, "recovers_lost_stops", False) is False


class ScriptedWorker:
    """One stop at the breakpoint (recovered or reported), then the program's exit."""

    def __init__(self, recovered):
        self.recovered, self.waits = recovered, 0

    def dispatch(self, operation, args):
        if operation == "launch":
            return {
                "session_id": "s",
                "breakpoints": [{"id": 1, "verified": True, "line": 8}],
                "artifact_manifest_id": None,
                "target_identity": None,
                "source_sha256": "x",
            }
        if operation == "wait":
            self.waits += 1
            if self.waits > 1:
                return {"state": "terminated"}
            stop = {"state": "stopped", "stop_id": "t", "event": {"body": {"reason": "breakpoint"}}}
            return {**stop, "recovered": True} if self.recovered else stop
        if operation == "inspect":
            frame = {"source": {"path": "/w/Main.java"}, "line": 8}
            variable = {"name": "x", "value": "1", "variablesReference": 0}
            return {
                "frames": [frame],
                "scopes": [{"name": "Locals", "reference": 1, "variables": [variable]}],
            }
        if operation == "report":
            return {"output": "", "output_truncated": False}
        return {}

    def close(self):
        pass


@pytest.mark.parametrize("recovered", [True, False])
def test_probes_count_recovered_stops_and_warn(monkeypatch, recovered):
    import peeky.probe as probe_module
    from peeky.probe import Probe

    worker = ScriptedWorker(recovered)
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("Main.java", 8, ("x",)))
    assert result["status"] == "completed" and result["rows"][0]["value"] == "1"
    warned = any("never reported 1 stop(s)" in warning for warning in result["warnings"])
    assert (result.get("recovered_stops"), warned) == ((1, True) if recovered else (None, False))


def test_session_states_say_a_stop_was_recovered():
    from peeky.cli import _state_text

    state = {"state": "stopped", "location": "Demo.java:5", "reason": "breakpoint"}
    assert "never reported" not in _state_text(state)
    assert "never reported this stop" in _state_text({**state, "recovered": True})
