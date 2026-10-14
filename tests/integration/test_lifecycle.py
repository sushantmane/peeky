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
    monkeypatch.setenv("DEBUG_TOKEN", "secret-control-token")
    program = tmp_path / "target.py"
    program.write_text(
        "import os\nimport time\npid = os.getpid()\nsecret = os.environ.get('DEBUG_TOKEN', 'absent')\ntime.sleep(60)\n"
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
        assert "absent" in values["secret"]
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
