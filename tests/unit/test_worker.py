import json

import pytest

from peeky.adapters import PythonAdapter
from peeky.worker import DebugWorker, make_server


class Adapter:
    closed = False
    calls = []

    def request(self, command, args):
        self.calls.append(command)
        if command == "variables":
            return {"variables": [{"name": "value", "value": "1"}]}
        raise TimeoutError("reply lost after operation applied")

    def close(self):
        self.closed = True

    def wait_event(self, names, cursor, timeout):
        assert timeout <= 2
        raise TimeoutError("poll expired")


def stopped_worker(tmp_path):
    worker = DebugWorker(tmp_path)
    worker.dap = Adapter()
    worker.session = "session"
    worker.state = "stopped"
    worker.stop_id = "stop"
    worker.thread_id = 1
    worker.capabilities = {"supports_set_variable": True}
    worker.references = {7}
    worker.reference_adapter = {7: 7}
    return worker


@pytest.mark.parametrize(
    "operation,extra",
    [("continue", {}), ("set_variable", {"reference": 7, "name": "value", "value": "2"})],
)
def test_uncertain_mutation_invalidates_session(tmp_path, operation, extra):
    worker = stopped_worker(tmp_path)
    adapter = worker.dap
    with pytest.raises(TimeoutError):
        worker.dispatch(operation, {"session_id": "session", "stop_id": "stop", **extra})
    assert adapter.closed
    assert worker.dap is None and worker.session is None and worker.stop_id is None
    with pytest.raises(ValueError, match="session"):
        worker.dispatch("continue", {"session_id": "session", "stop_id": "stop"})


def test_wait_poll_preserves_session(tmp_path):
    worker = stopped_worker(tmp_path)
    worker.state = "running"
    worker.cursor = 0
    assert worker.dispatch("wait", {"session_id": "session", "timeout": 20}) == {
        "state": "running",
        "timed_out": True,
    }
    assert worker.session == "session"


def test_report_requires_current_session(tmp_path):
    worker = stopped_worker(tmp_path)
    with pytest.raises(ValueError, match="session"):
        worker.dispatch("report", {})


def test_unsupported_mutation_rejected_before_adapter(tmp_path):
    worker = stopped_worker(tmp_path)
    worker.capabilities = {"supports_set_variable": False}
    worker.dap.calls = []
    with pytest.raises(ValueError, match="does not support"):
        worker.dispatch(
            "set_variable",
            {"session_id": "session", "stop_id": "stop", "reference": 7, "name": "x", "value": "2"},
        )
    assert worker.dap.calls == []


@pytest.mark.parametrize(
    "operation,args",
    [
        ("launch", {"program": "a.py", "breakpoints": [{}]}),
        ("launch", {"program": "a.py", "breakpoints": [{"line": True}]}),
        ("launch", {"program": "a.py", "breakpoints": [{"line": 1, "condition": []}]}),
        ("wait", {"timeout": float("nan")}),
        ("wait", {"timeout": "2"}),
        ("set_variable", {"reference": [], "name": "x", "value": "3"}),
        ("inspect", {"frame_id": []}),
    ],
)
def test_invalid_input_rejected_before_adapter(tmp_path, operation, args):
    with pytest.raises(ValueError):
        DebugWorker(tmp_path).dispatch(operation, args)


def test_server_rejects_empty_token(tmp_path):
    with pytest.raises(ValueError, match="Token"):
        make_server(tmp_path, "")


def test_symlink_cannot_escape_root(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.py")
    outside.write_text("pass")
    try:
        (tmp_path / "escape.py").symlink_to(outside)
        with pytest.raises(ValueError, match="inside"):
            DebugWorker(tmp_path).path("escape.py")
    finally:
        outside.unlink()


def test_failed_adapter_spawn_cleans_prepared_runtime(tmp_path, monkeypatch):
    closed = []

    class Runtime(PythonAdapter):
        def close(self):
            closed.append(True)

    def fail_spawn(*args, **kwargs):
        raise OSError("adapter missing")

    monkeypatch.setattr("peeky.worker.DAP", fail_spawn)
    (tmp_path / "target.py").write_text("pass\n")
    worker = DebugWorker(tmp_path, adapter=Runtime())
    with pytest.raises(OSError, match="adapter missing"):
        worker.dispatch("launch", {"program": "target.py"})
    assert closed == [True]
    assert worker.runtime is None and worker.session is None


def test_provenance_failure_prevents_mutation_and_cleans_session(tmp_path):
    closed = []

    class Runtime(PythonAdapter):
        def assert_current(self):
            raise RuntimeError("Prepared build provenance mismatch")

        def close(self):
            closed.append(True)

    worker = stopped_worker(tmp_path)
    worker.runtime = Runtime()
    backend = worker.dap
    backend.calls = []
    with pytest.raises(RuntimeError, match="provenance"):
        worker.dispatch(
            "set_variable",
            {
                "session_id": "session",
                "stop_id": "stop",
                "reference": 7,
                "name": "value",
                "value": "2",
            },
        )
    assert backend.calls == [] and backend.closed
    assert closed == [True]
    assert worker.runtime is None and worker.session is None


@pytest.mark.parametrize("field,value", [("arguments", "hello"), ("sources", {"Main.java": True})])
def test_java_cli_rejects_non_array_inputs_before_starting_server(
    tmp_path, monkeypatch, capsys, field, value
):
    from peeky.worker import main

    config = {"bundle": "adapter", "sources": ["Main.java"], "main_class": "Main"}
    config[field] = value
    path = tmp_path / "target.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("DEBUG_TOKEN", "test-token-at-least-sixteen")
    monkeypatch.setattr("sys.argv", ["peeky-worker", "--java-config", str(path)])
    monkeypatch.setattr("peeky.worker.make_server", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "JSON arrays" in capsys.readouterr().err


class CrashStop:
    """A stop whose stack cannot be read (e.g. a native signal stop)."""

    closed = False

    def wait_event(self, names, cursor, timeout):
        import time

        body = {"reason": "signal", "threadId": 3}
        return {"event": "stopped", "cursor": 1, "received": time.monotonic(), "body": body}

    def request(self, command, args, timeout=None):
        raise RuntimeError("stack unavailable at crash")

    def close(self, *args, **kwargs):
        self.closed = True


def test_location_read_failure_never_closes_the_session(tmp_path):
    worker = DebugWorker(tmp_path)
    worker.dap = CrashStop()
    worker.session, worker.state, worker.cursor = "session", "running", 0
    state = worker.dispatch("wait", {"session_id": "session", "timeout": 1})
    assert state["state"] == "stopped" and "location" not in state
    assert worker.dap is not None and not worker.dap.closed and worker.session == "session"
    again = worker.dispatch("wait", {"session_id": "session", "timeout": 1})
    assert again["stop_id"] == state["stop_id"]


class QuietDap:
    """An adapter that resumed the program but never reports its exit (java-debug #27)."""

    def __init__(self, threads, pid=None, late_event=None, fail=None):
        self.threads, self.calls, self.closed = threads, [], False
        self.debuggee_pid, self.late_event, self.fail = pid, late_event, fail

    def wait_event(self, names, cursor, timeout):
        if timeout == 0 and self.late_event:
            return self.late_event
        raise TimeoutError("poll expired")

    def request(self, command, args, timeout=None):
        self.calls.append(command)
        if self.fail:
            raise self.fail
        return {"threads": self.threads} if self.threads is not None else {}

    def close(self, *args, **kwargs):
        self.closed = True


class JavaLike:
    liveness_by_threads = True

    def assert_current(self):
        pass


def quiet_worker(tmp_path, dap, runtime=None):
    import time

    worker = DebugWorker(tmp_path)
    worker.dap, worker.runtime = dap, runtime or JavaLike()
    worker.session, worker.state, worker.cursor = "session", "running", 0
    worker.resumed_at = time.monotonic() - 5
    return worker


def poll(worker):
    worker.liveness_checked = 0.0  # as if two seconds passed since the last check
    return worker.dispatch("wait", {"session_id": "session", "timeout": 0.1})


@pytest.mark.parametrize("threads", [None, []])
def test_one_empty_threads_reply_is_a_hint_two_are_an_inferred_exit(tmp_path, threads):
    worker = quiet_worker(tmp_path, QuietDap(threads))
    assert poll(worker) == {"state": "running", "timed_out": True}
    state = poll(worker)
    assert state["state"] == "terminated" and state["exit_inferred"] is True
    assert worker.dap.calls == ["threads", "threads"]
    assert any(entry["operation"] == "exit_inferred" for entry in worker.audit)


def test_live_threads_reset_the_count(tmp_path):
    worker = quiet_worker(tmp_path, QuietDap([]))
    poll(worker)
    worker.dap.threads = [{"id": 1}]
    poll(worker)
    worker.dap.threads = []
    assert poll(worker)["state"] == "running"


def test_a_gone_launched_jvm_process_confirms_exit_on_the_first_empty_reply(tmp_path):
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    worker = quiet_worker(tmp_path, QuietDap([], pid=child.pid))
    assert poll(worker)["exit_inferred"] is True
    alive = quiet_worker(tmp_path, QuietDap([], pid=__import__("os").getpid()))
    assert poll(alive)["state"] == "running"


@pytest.mark.parametrize("failure", [TimeoutError("slow"), RuntimeError("error reply")])
def test_a_slow_or_failed_check_means_still_running_and_never_closes(tmp_path, failure):
    worker = quiet_worker(tmp_path, QuietDap([], fail=failure))
    for _ in range(3):
        assert poll(worker) == {"state": "running", "timed_out": True}
    assert worker.dap is not None and not worker.dap.closed and worker.session == "session"


def test_a_real_event_arriving_during_the_check_wins(tmp_path):
    late = {"event": "stopped", "cursor": 7, "body": {"reason": "breakpoint", "threadId": 1}}
    worker = quiet_worker(tmp_path, QuietDap([], late_event=late))
    poll(worker)
    state = poll(worker)
    assert state["state"] == "stopped" and "exit_inferred" not in state
    assert worker.cursor == 7


def test_exit_is_never_inferred_for_attach_other_adapters_or_right_after_resuming(tmp_path):
    import time

    from peeky.java_attach import JavaAttachSession

    assert JavaAttachSession.liveness_by_threads is False
    for runtime, resumed in ((PythonAdapter(), 5), (JavaLike(), 0)):
        worker = quiet_worker(tmp_path, QuietDap(None), runtime)
        worker.resumed_at = time.monotonic() - resumed
        state = poll(worker)
        assert state["state"] == "running" and worker.dap.calls == []
