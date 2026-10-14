import pytest

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
    worker.references = {7}
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
