"""#27: an attached JVM's breakpoint stop whose `stopped` event was lost fails fast,
with a diagnosis; nothing is ever served from it."""

import time

import pytest

import peeky.worker as worker_module
from peeky.errors import classify
from peeky.worker import DebugWorker, LostStop

JVM = {2: "Reference Handler", 3: "Finalizer", 4: "Signal Dispatcher", 5: "Common-Cleaner"}


def at(path, line, name="Demo.twice(int)"):
    return {"id": 100, "name": name, "line": line, "source": {"path": str(path)}}


class PausedDap:
    """java-debug as the worker sees it: threads, and one-frame stacks of the threads
    the debugger suspended (none for running ones). A frame may be a callable, asked
    on every request, for a thread whose state changes during a poll."""

    def __init__(self, frames, names=None):
        self.frames = {**{thread: None for thread in JVM}, **frames}
        self.names = {**JVM, **(names or {})}
        self.events, self.calls, self.closed = [], [], None
        self.during_stack = self.fail = None

    def add(self, event, **body):
        cursor = max((e["cursor"] for e in self.events), default=0) + 1
        self.events.append(
            {"event": event, "cursor": cursor, "received": time.monotonic(), "body": body}
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
            return {
                "threads": [
                    {"id": thread, "name": f"Thread [{self.names.get(thread, f't{thread}')}]"}
                    for thread in self.frames
                ]
            }
        if command == "stackTrace":
            if self.during_stack:
                self.during_stack, during = None, self.during_stack
                during()
            frame = self.frames.get(args["threadId"])
            frame = frame() if callable(frame) else frame
            return {"stackFrames": [frame] if frame else [], "totalFrames": int(bool(frame))}
        if command in ("continue", "next"):
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

    def snapshot_output(self):
        return "partial output\n", False

    def recent_events(self):
        return {"recent": [], "ignored": {}, "trace": [{"dir": "peeky", "name": "lost-stop poll"}]}

    def close(self, terminate=True):
        self.closed = {"terminate": terminate}


class AttachLike:
    detects_lost_stops = True
    terminate_debuggee = False
    manifest_id = "m"

    def __init__(self):
        self.closed = False

    def assert_current(self):
        pass

    def close(self):
        self.closed = True


class LaunchLike(AttachLike):
    detects_lost_stops = False


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "src" / "Demo.java"
    path.parent.mkdir()
    path.write_text("class Demo {}\n")
    return path


def paused_worker(source, frames, runtime=None, verified=True, lines=(5,), names=None):
    worker = DebugWorker(source.parent.parent)
    worker.dap, worker.runtime = PausedDap(frames, names), runtime or AttachLike()
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


def poll(worker, elapsed=worker_module.LOST_STOP_POLL_S):
    """One `wait` whose poll is due, as if ``elapsed`` seconds passed since the last."""
    worker.lost_stop_polled = 0.0
    if worker.candidate:
        worker.candidate["since"] -= elapsed
    return worker.dispatch("wait", {"session_id": "session", "timeout": 0.1})


def operations(worker):
    return [entry["operation"] for entry in worker.audit]


def test_a_thread_alone_at_a_verified_breakpoint_for_three_polls_over_four_seconds_fails_fast(
    source,
):
    worker = paused_worker(source, {7: at(source, 5), 8: None}, names={7: "main"})
    dap, runtime = worker.dap, worker.runtime
    for samples in (1, 2):
        assert poll(worker) == {"state": "running", "timed_out": True}
        assert worker.candidate["samples"] == samples
    with pytest.raises(LostStop) as failure:
        poll(worker)
    message = str(failure.value)
    assert message.startswith("Lost stop: java-debug never reported a stop it made")
    for evidence in ("'main' (id 7)", "Demo.twice(int) at Demo.java:5", "src/Demo.java:5"):
        assert evidence in message
    assert "suspended for 4.0 s" in message and "3 checks" in message
    error = classify(failure.value)
    assert (error["code"], error["session_closed"]) == ("DEBUGGER_ERROR", True)
    assert "state cannot be trusted" in error["hint"] and "close" in error["hint"]
    # Nothing was served; Peeky detached (an attached JVM is not terminated).
    assert worker.hits == 0 and worker.stop_id is None and "lost_stop" in operations(worker)
    assert dap.closed == {"terminate": False} and runtime.closed and worker.dap is None


def test_the_failed_session_repeats_its_error_reports_and_closes(source):
    worker = paused_worker(source, {7: at(source, 5)}, names={7: "main"})
    poll(worker), poll(worker)
    with pytest.raises(LostStop) as failure:
        poll(worker)
    session = {"session_id": "session"}
    for operation, extra in (
        ("wait", {"timeout": 1}),
        ("inspect", {"stop_id": "any"}),
        ("continue", {"stop_id": "any"}),
        ("step", {"stop_id": "any"}),
    ):
        with pytest.raises(LostStop) as again:
            worker.dispatch(operation, {**session, **extra})
        assert str(again.value) == str(failure.value)
    report = worker.dispatch("report", session)
    assert report["state"] == "detached" and report["output"] == "partial output\n"
    assert report["lost_stop"]["breakpoint"] == "src/Demo.java:5"
    assert [e["operation"] for e in report["history"]][-1] == "lost_stop"
    assert report["debugger_events"]["trace"]
    assert worker.dispatch("close", session) == {"state": "idle"}
    with pytest.raises(ValueError, match="Missing or stale session_id"):
        worker.dispatch("wait", {**session, "timeout": 1})


def test_a_new_session_clears_a_failed_one(source):
    worker = paused_worker(source, {7: at(source, 5)})
    worker.failed = {"session": "old", "message": "Lost stop: x", "report": {}}
    with pytest.raises(ValueError):
        worker.dispatch("launch", {"program": "missing.java", "breakpoints": []})
    assert worker.failed is None


def test_three_samples_must_span_four_seconds(source):
    worker = paused_worker(source, {7: at(source, 5)})
    for _ in range(4):
        assert poll(worker, elapsed=1.0)["state"] == "running"
    assert worker.candidate["samples"] == 4  # 3 s so far: not yet evidence
    with pytest.raises(LostStop):
        poll(worker, elapsed=1.0)


def test_two_samples_are_never_enough_however_far_apart(source):
    worker = paused_worker(source, {7: at(source, 5)})
    for _ in range(2):
        assert poll(worker, elapsed=10.0)["state"] == "running"
    with pytest.raises(LostStop):
        poll(worker, elapsed=10.0)


def jvm_suspended_on(calls):
    """A JVM thread suspended (a suspend-all event) only on its ``calls``-th check."""
    seen = []

    def frame():
        seen.append(1)
        return at("/jdk/Object.java", 1, "Object.wait(long)") if len(seen) in calls else None

    return frame


@pytest.mark.parametrize(
    "case",
    [
        "a JVM thread suspended",
        "a JVM thread suspended at the last check",
        "a JVM thread suspended after a recheck",
        "another thread suspended",
        "another thread at the breakpoint too",
        "the suspect running at a recheck",
    ],
)
def test_any_other_suspended_thread_makes_a_poll_no_evidence_and_restarts_the_count(source, case):
    worker = paused_worker(source, {7: at(source, 5), 8: None})
    poll(worker), poll(worker)
    assert worker.candidate["samples"] == 2
    dap = worker.dap
    if case == "a JVM thread suspended":
        dap.frames[2] = at("/jdk/Reference.java", 1, "Reference.waitForReferencePendingList()")
    elif case == "a JVM thread suspended at the last check":
        dap.frames[3] = jvm_suspended_on({2})  # before the window: running; after: not
    elif case == "a JVM thread suspended after a recheck":
        dap.frames[5] = jvm_suspended_on({3})
    elif case == "another thread suspended":
        dap.frames[8] = at(source.parent / "Other.java", 3, "Other.run()")
    elif case == "another thread at the breakpoint too":
        dap.frames[8] = at(source, 5)
    else:
        checks = []
        dap.frames[7] = lambda: checks.append(1) or (at(source, 5) if len(checks) < 3 else None)
    assert poll(worker) == {"state": "running", "timed_out": True}
    assert worker.candidate is None and "lost_stop" not in operations(worker)
    if case.startswith("another"):
        dap.frames[8] = None
    else:
        dap.frames = {**dap.frames, 2: None, 3: None, 5: None, 7: at(source, 5)}
    for samples in (1, 2):
        assert poll(worker)["state"] == "running" and worker.candidate["samples"] == samples
    with pytest.raises(LostStop):
        poll(worker)


def test_a_class_load_storm_never_counts(source):
    # The review's H1: java-debug's class-load requests suspend every thread, and a
    # thread blocked entering `synchronized` shows the breakpoint line inside it.
    waiting = {7: at(source, 5, "Storm.contend()"), 9: at("/jdk/Thread.java", 1, "sleep")}
    worker = paused_worker(source, {1: at("/jdk/Class.java", 1, "forName0"), **waiting})
    worker.dap.frames.update({2: at("/jdk/R.java", 1), 3: at("/jdk/O.java", 1)})
    for _ in range(8):
        assert poll(worker) == {"state": "running", "timed_out": True}
    assert worker.candidate is None and "lost_stop" not in operations(worker)


@pytest.mark.parametrize(
    "case",
    [
        "unverified",
        "other line",
        "other file",
        "running",
        "stepping",
        "a JVM thread",
        "breakpoints removed",
        "launched",
    ],
)
def test_nothing_else_is_ever_evidence(source, case):
    other = source.parent / "Other.java"
    other.write_text("class Other {}\n")
    frame = {"other line": at(source, 6), "other file": at(other, 5), "running": None}.get(
        case, at(source, 5)
    )
    thread = 3 if case == "a JVM thread" else 7
    worker = paused_worker(
        source,
        {thread: frame},
        runtime=LaunchLike() if case == "launched" else None,
        verified=case != "unverified",
    )
    if case == "stepping":
        worker.stepping.add(7)
    if case == "breakpoints removed":
        worker.note_placed(source, [], {"breakpoints": []})
    for _ in range(5):
        assert poll(worker) == {"state": "running", "timed_out": True}
    assert worker.candidate is None and "lost_stop" not in operations(worker)
    if case in ("launched", "unverified", "breakpoints removed"):
        assert worker.dap.calls == []  # nothing to look for: no poll at all


def test_only_the_breakpoint_the_thread_is_at_must_be_verified(source):
    worker = paused_worker(source, {7: at(source, 5)}, lines=(5, 9))
    worker.placed[str(source.resolve())][0]["verified"] = False
    for _ in range(4):
        assert poll(worker)["state"] == "running"
    assert worker.dap.calls.count("threads") == 4 and worker.candidate is None


def test_threads_with_a_step_in_flight_are_neither_suspects_nor_witnesses(source):
    # A step whose report was lost may sit anywhere, a breakpoint line included.
    worker = paused_worker(source, {7: at(source, 5), 9: at(source, 6)})
    worker.stepping.add(9)
    poll(worker), poll(worker)
    with pytest.raises(LostStop):
        poll(worker)


def test_a_step_marks_its_thread_until_that_thread_reports_a_stop(source):
    worker = paused_worker(source, {7: at(source, 5)})
    worker.dap.add("stopped", reason="breakpoint", threadId=7)
    state = worker.dispatch("wait", {"session_id": "session", "timeout": 0.1})
    worker.dispatch("step", {"session_id": "session", "stop_id": state["stop_id"]})
    assert worker.stepping == {7}
    for _ in range(5):
        assert poll(worker)["state"] == "running"  # its report was lost: never evidence
    worker.dap.add("stopped", reason="step", threadId=7)
    assert poll(worker)["reason"] == "step" and not worker.stepping


def test_a_breakpoint_verified_when_its_class_loads_becomes_eligible(source):
    worker = paused_worker(source, {7: at(source, 5)}, verified=False)
    assert poll(worker)["state"] == "running" and worker.dap.calls == []
    worker.dap.add(
        "breakpoint", reason="changed", breakpoint={"id": 1, "verified": True, "line": 5}
    )
    assert poll(worker)["breakpoint_events"] and worker.candidate["samples"] == 1
    poll(worker)
    with pytest.raises(LostStop):
        poll(worker)


def test_a_real_report_wins_and_is_served_as_usual(source):
    worker = paused_worker(source, {7: at(source, 5)})
    poll(worker), poll(worker)
    worker.dap.during_stack = lambda: worker.dap.add("stopped", reason="breakpoint", threadId=7)
    state = poll(worker)
    assert state["state"] == "stopped" and state["hit"] == 1 and worker.cursor == 1
    assert worker.candidate is None and "lost_stop" not in operations(worker)


def test_a_report_between_polls_is_served_and_restarts_the_count(source):
    worker = paused_worker(source, {7: at(source, 5)})
    poll(worker), poll(worker)
    worker.dap.add("stopped", reason="breakpoint", threadId=7)
    state = poll(worker)
    assert (state["state"], state["hit"], worker.candidate) == ("stopped", 1, None)
    worker.dispatch("continue", {"session_id": "session", "stop_id": state["stop_id"]})
    for _ in range(2):
        assert poll(worker)["state"] == "running"
    with pytest.raises(LostStop):
        poll(worker)


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


def test_each_poll_checks_a_bounded_rotating_window_and_the_jvms_own_threads(source, monkeypatch):
    monkeypatch.setattr(worker_module, "LOST_STOP_THREADS", 4)
    frames = {thread: None for thread in range(10, 20)}
    frames[19] = at(source, 5)
    worker = paused_worker(source, frames)
    counts = []
    for _ in range(3):
        before = len(worker.dap.calls)
        assert poll(worker)["state"] == "running"
        counts.append(worker.dap.calls[before:].count("stackTrace"))
    # The three JVM threads with Java frames before and after four others (Signal
    # Dispatcher, which has none, is one of them); the suspect, rechecked twice with the
    # JVM threads after each recheck, is found in the third window.
    assert counts == [10, 10, 10 + 2 * 4] and worker.candidate["thread"] == 19
    poll(worker)
    with pytest.raises(LostStop):
        poll(worker)


def test_a_poll_never_outlives_its_time_budget(source, monkeypatch):
    monkeypatch.setattr(worker_module, "LOST_STOP_BUDGET_S", 0.0)
    worker = paused_worker(source, {7: at(source, 5)})
    for _ in range(4):
        assert poll(worker)["state"] == "running"
    assert set(worker.dap.calls) == {"threads"} and worker.candidate is None


@pytest.mark.parametrize("failure", [TimeoutError("slow"), RuntimeError("error reply")])
def test_a_slow_or_failed_poll_is_no_evidence_and_never_closes(source, failure):
    worker = paused_worker(source, {7: at(source, 5)})
    poll(worker), poll(worker)
    worker.dap.fail = failure
    assert poll(worker) == {"state": "running", "timed_out": True}
    assert worker.dap is not None and worker.session == "session" and worker.candidate is None
    worker.dap.fail = None
    poll(worker), poll(worker)
    with pytest.raises(LostStop):
        poll(worker)


def test_an_unattended_session_with_a_pause_budget_detaches_on_a_lost_stop(source, capsys):
    worker = paused_worker(source, {7: at(source, 5)})
    dap, worker.max_pause = worker.dap, 10
    for _ in range(2):
        worker.lost_stop_polled = 0.0
        worker.guard_pause()
        worker.candidate["since"] -= 2
    worker.lost_stop_polled = 0.0
    worker.guard_pause()
    assert dap.closed == {"terminate": False} and worker.failed["session"] == "session"
    assert "Lost stop" in capsys.readouterr().err
    with pytest.raises(LostStop):
        worker.dispatch("wait", {"session_id": "session", "timeout": 1})


def test_nothing_carries_over_into_the_next_session(source):
    worker = paused_worker(source, {7: at(source, 5)})
    poll(worker)
    worker.stepping.add(9)
    worker.close()
    assert (worker.placed, worker.stepping, worker.candidate) == ({}, set(), None)


def test_only_attached_jvms_opt_in():
    from peeky.java import JavaSession
    from peeky.java_attach import JavaAttachSession

    assert JavaAttachSession.detects_lost_stops is True
    assert getattr(JavaSession, "detects_lost_stops", False) is False


def test_the_diagnosis_is_a_debugger_error_whatever_its_text_mentions():
    # The message names a breakpoint and a time: never INVALID_BREAKPOINT or TIMEOUT.
    error = classify(LostStop("Lost stop: thread 'main' at the breakpoint, timed out after 4 s"))
    assert error["code"] == "DEBUGGER_ERROR" and not error["retryable"]


class FailingWorker:
    """A probe's worker whose wait meets a lost stop (#27)."""

    def __init__(self):
        self.calls = []

    def dispatch(self, operation, args):
        self.calls.append(operation)
        if operation == "launch":
            return {
                "session_id": "s",
                "breakpoints": [{"id": 1, "verified": True, "line": 8}],
                "artifact_manifest_id": None,
                "target_identity": None,
                "source_sha256": "x",
            }
        if operation == "wait":
            raise LostStop("Lost stop: java-debug never reported a stop it made")
        if operation == "report":
            return {
                "state": "detached",
                "output": "so far\n",
                "output_truncated": False,
                "lost_stop": {"thread": 1},
                "debugger_events": {"trace": [{"name": "lost stop"}]},
            }
        raise AssertionError(operation)

    def close(self):
        self.calls.append("close")


def test_a_probe_fails_with_the_diagnosis_and_its_debugger_events(monkeypatch):
    import peeky.probe as probe_module
    from peeky.probe import Probe

    worker = FailingWorker()
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("Main.java", 8, ("x",)))
    assert result["status"] == "failed" and result["hits"] == 0 and result["rows"] == []
    assert (result["error"]["code"], result["error"]["session_closed"]) == ("DEBUGGER_ERROR", True)
    assert result["debugger_events"]["trace"] == [{"name": "lost stop"}]
    assert result["output"] == "so far\n" and worker.calls[-1] == "close"


def test_a_sighting_then_the_programs_exit_is_an_unreported_stop_not_a_line_never_run(source):
    worker = paused_worker(source, {7: at(source, 5)}, names={7: "main"})
    assert poll(worker)["state"] == "running" and worker.candidate["samples"] == 1
    worker.dap.frames[7] = None
    worker.dap.add("terminated")
    state = poll(worker)
    assert state["state"] == "terminated"
    unreported = state["unreported_stop"]
    assert (unreported["thread_name"], unreported["breakpoint"]) == ("main", "src/Demo.java:5")
    assert unreported["location"] == "Demo.java:5" and "unreported_stop" in operations(worker)
    # Repeated waits say the same; a new session starts clean.
    assert worker.dispatch("wait", {"session_id": "session"})["unreported_stop"] == unreported
    worker.close()
    assert worker.unreported is None and worker.sighted is None


def test_a_sighting_whose_report_then_arrives_is_not_unreported(source):
    worker = paused_worker(source, {7: at(source, 5)})
    poll(worker)
    worker.dap.add("stopped", reason="breakpoint", threadId=7)
    state = poll(worker)
    worker.dispatch("continue", {"session_id": "session", "stop_id": state["stop_id"]})
    worker.dap.add("terminated")
    state = poll(worker)
    assert state["state"] == "terminated" and "unreported_stop" not in state


class EndingWorker:
    """A probe's worker: ``hits`` reported stops, then the program ends after a sighting
    of a stop the debugger never reported."""

    def __init__(self, hits):
        self.stops = hits

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
            if self.stops:
                self.stops -= 1
                body = {"reason": "breakpoint"}
                return {"state": "stopped", "stop_id": "t", "event": {"body": body}}
            unreported = {
                "thread_name": "main",
                "breakpoint": "Main.java:8",
                "function": "Main.main(String[])",
                "location": "Main.java:8",
            }
            return {"state": "terminated", "unreported_stop": unreported}
        if operation == "inspect":
            frame = {"source": {"path": "/w/Main.java"}, "line": 8}
            variable = {"name": "x", "value": "1", "variablesReference": 0}
            scope = {"name": "Locals", "reference": 1, "variables": [variable]}
            return {"frames": [frame], "scopes": [scope]}
        if operation == "report":
            return {
                "state": "terminated",
                "output": "",
                "output_truncated": False,
                "debugger_events": {"trace": [{"name": "lost-stop poll"}]},
            }
        return {}

    def close(self):
        pass


@pytest.mark.parametrize("hits", [0, 1])
def test_a_probe_whose_stop_was_seen_but_never_reported_never_says_no_stop(monkeypatch, hits):
    import peeky.probe as probe_module
    from peeky.probe import Probe

    monkeypatch.setattr(probe_module, "DebugWorker", lambda *a, **k: EndingWorker(hits))
    result = probe_module.run_probe("/w", Probe("Main.java", 8, ("x",), hits=2))
    assert result["debugger_events"]["trace"]
    seen = "thread 'main' was seen paused at Main.java:8 (in Main.main(String[]) at Main.java:8)"
    # The fact, not an issue number: printed text never cites one.
    seen += ", a stop the debugger never reported (java-debug sometimes loses a stop event)"
    if hits:
        assert result["status"] == "completed" and result["hits"] == 1
        assert any(seen in warning for warning in result["warnings"])
        assert not any("#27" in warning for warning in result["warnings"])
    else:
        assert result["status"] == "failed" and result["error"]["code"] == "DEBUGGER_ERROR"
        assert result["error"]["message"].startswith(f"The program ended after {seen}")
        hint = result["error"]["hint"]
        assert "keep `debugger_events` for a bug report" in hint and "#27" not in hint
        assert len(hint) <= 150
        assert "describe" not in result["error"]["message"] + result["error"]["hint"]


def test_session_states_name_an_unreported_stop():
    from peeky.cli import _state_text

    state = {"state": "terminated"}
    unreported = {"thread_name": "main", "breakpoint": "src/Demo.java:5"}
    assert "never reported" not in _state_text(state)
    text = _state_text({**state, "unreported_stop": unreported})
    assert "thread 'main' was seen paused at src/Demo.java:5" in text
    assert "(java-debug sometimes loses a stop event)" in text and "#27" not in text


def guard(worker):
    """One pause-guard tick, as if the poll interval passed since the last poll."""
    worker.lost_stop_polled = 0.0
    if worker.candidate:
        worker.candidate["since"] -= worker_module.LOST_STOP_POLL_S
    worker.guard_pause()


def test_an_unattended_session_verifies_a_pending_breakpoint_and_fails_on_its_lost_stop(source):
    # Review round 4: attaching to a suspend=y JVM, the breakpoint is pending at launch
    # and verified only by a `breakpoint` event. With no client waiting, nothing took
    # that event, so the guard never polled and the lost stop held the JVM.
    worker = paused_worker(source, {7: at(source, 5)}, verified=False)
    worker.max_pause = 3
    dap = worker.dap
    dap.add("breakpoint", reason="changed", breakpoint={"id": 1, "verified": True, "line": 5})
    for _ in range(2):
        guard(worker)
        assert worker.state == "running" and worker.failed is None
    assert worker.cursor == 1 and worker.breakpoint_events[0]["reason"] == "changed"
    guard(worker)
    assert worker.failed["error"] is LostStop and dap.closed == {"terminate": False}
    with pytest.raises(LostStop):
        worker.dispatch("wait", {"session_id": "session", "timeout": 1})


@pytest.mark.parametrize("then", ["stopped", "terminated"])
def test_draining_takes_only_the_breakpoint_events_before_a_stop_or_exit(source, then):
    worker = paused_worker(source, {7: at(source, 5)}, verified=False)
    dap = worker.dap
    dap.add("breakpoint", reason="changed", breakpoint={"id": 1, "verified": True, "line": 5})
    dap.add(then, **({"reason": "breakpoint", "threadId": 7} if then == "stopped" else {}))
    dap.add("breakpoint", reason="changed", breakpoint={"id": 1, "verified": True, "line": 5})
    worker.drain_breakpoint_events()
    assert worker.cursor == 1 and worker.placed[str(source.resolve())][0]["verified"]
    # The stop or exit is still the client's, and it still sees the update.
    state = worker.dispatch("wait", {"session_id": "session", "timeout": 0.1})
    assert state["state"] == then and worker.cursor == 2
    assert state["breakpoint_events"] == [worker.breakpoint_events[0]]
    if then == "stopped":
        assert state["hit"] == 1


def test_the_guard_leaves_a_reported_stop_and_what_follows_it_to_the_client(source):
    worker = paused_worker(source, {7: at(source, 5)})
    worker.max_pause = 10
    worker.dap.add("stopped", reason="breakpoint", threadId=7)
    worker.dap.add(
        "breakpoint", reason="changed", breakpoint={"id": 1, "verified": True, "line": 5}
    )
    guard(worker)
    assert worker.cursor == 0 and worker.state == "running" and not worker.breakpoint_events
    assert worker.dispatch("wait", {"session_id": "session", "timeout": 0.1})["hit"] == 1
