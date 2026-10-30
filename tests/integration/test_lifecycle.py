import os
import time

import pytest

from peeky.worker import DebugWorker

pytestmark = pytest.mark.integration


def wait_stopped(worker, session):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = worker.dispatch("wait", {"session_id": session, "timeout": 2})
        if result["state"] != "running":
            return result
    raise AssertionError("target failed to stop")


def test_real_target_secret_stripping_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBUG_TOKEN", "secret-control-token-0123456789abcdef")
    # A client's name for the same secret (pre-release security review).
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", "secret-client-token-0123456789abcdef")
    program = tmp_path / "target.py"
    program.write_text(
        "import os\nimport time\npid = os.getpid()\nsecret = [os.environ.get(n, 'absent') for n in ('DEBUG_TOKEN', 'PEEKY_WORKER_TOKEN')]\ntime.sleep(60)\n"
    )
    worker = DebugWorker(tmp_path)
    pid = None
    try:
        result = worker.dispatch("launch", {"program": "target.py", "breakpoints": [{"line": 5}]})
        session = result["session_id"]
        stop = wait_stopped(worker, session)
        context = {"session_id": session, "stop_id": stop["stop_id"]}
        snapshot = worker.dispatch("inspect", context)
        values = {v["name"]: v["value"] for scope in snapshot["scopes"] for v in scope["variables"]}
        assert values["secret"] == "['absent', 'absent']", values["secret"]
        pid = int(values["pid"])
        worker.dispatch("close", {"session_id": session})
        assert worker.session is None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"debuggee {pid} survived close")
    finally:
        worker.close()


def test_second_stop_rejects_old_identity_and_requires_fresh_inspection(tmp_path):
    (tmp_path / "target.py").write_text("value = 1\nvalue += 1\nprint(value)\n")
    worker = DebugWorker(tmp_path)
    try:
        launched = worker.dispatch(
            "launch", {"program": "target.py", "breakpoints": [{"line": 2}, {"line": 3}]}
        )
        session = {"session_id": launched["session_id"]}
        first = wait_stopped(worker, launched["session_id"])
        old = dict(session, stop_id=first["stop_id"])
        observed = worker.dispatch("inspect", old)
        scope = next(
            s for s in observed["scopes"] if any(v["name"] == "value" for v in s["variables"])
        )
        mutation = {"reference": scope["reference"], "name": "value", "value": "99"}
        worker.dispatch("continue", old)
        second = wait_stopped(worker, launched["session_id"])
        assert second["state"] == "stopped"
        assert second["stop_id"] != first["stop_id"]
        with pytest.raises(ValueError, match="Stale stop"):
            worker.dispatch("set_variable", dict(old, **mutation))
        current = dict(session, stop_id=second["stop_id"])
        with pytest.raises(ValueError, match="Inspect the scope"):
            worker.dispatch("set_variable", dict(current, **mutation))
        observed = worker.dispatch("inspect", current)
        values = {v["name"]: v["value"] for s in observed["scopes"] for v in s["variables"]}
        assert values["value"] == "2"
    finally:
        worker.close()
