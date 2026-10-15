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
