"""Protocol v1 error codes and lifecycle conformance against real debugpy (issue #6)."""

import json
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from peeky.client import Client
from peeky.errors import PROTOCOL_VERSION, PeekyError, classify
from peeky.worker import make_server

TOKEN = "lifecycle-test-token-123456"


@pytest.fixture
def worker(tmp_path):
    server = make_server(tmp_path, TOKEN, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_port}", TOKEN)
    try:
        yield tmp_path, client, server
    finally:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def error(call):
    with pytest.raises(PeekyError) as caught:
        call()
    return caught.value


def stopped(client, session):
    for _ in range(30):
        state = client.call("wait", **session, timeout=2)
        if state["state"] != "running":
            return state
    raise AssertionError("did not stop")


def test_version_and_error_codes_are_machine_readable(worker):
    root, client, server = worker
    version = client.call("version")
    assert version["protocol"] == PROTOCOL_VERSION and "probe" in version["operations"]
    assert {"STALE_STOP", "INSPECT_REQUIRED", "PROVENANCE_CHANGED"} <= set(version["error_codes"])
    (root / "loop.py").write_text("for i in range(3):\n    x = i\n    print(x)\n")
    launch = client.call("launch", program="loop.py", breakpoints=[{"line": 2}])
    assert launch["protocol"] == PROTOCOL_VERSION
    session = {"session_id": launch["session_id"]}
    first = stopped(client, session)
    scope = client.call("inspect", **session, stop_id=first["stop_id"])["scopes"][0]
    client.call("continue", **session, stop_id=first["stop_id"])
    second = stopped(client, session)
    stale = error(lambda: client.call("inspect", **session, stop_id=first["stop_id"]))
    assert (stale.code, stale.error_code, stale.retryable, stale.session_closed) == (
        400,
        "STALE_STOP",
        False,
        False,
    )
    old = error(
        lambda: client.call(
            "set_variable",
            **session,
            stop_id=second["stop_id"],
            reference=scope["reference"],
            name="x",
            value="5",
        )
    )
    assert old.error_code == "INSPECT_REQUIRED" and old.hint
    busy = error(lambda: client.call("launch", program="loop.py"))
    assert busy.error_code == "SESSION_ACTIVE"
    assert error(lambda: client.call("evaluate", **session)).error_code == "UNSUPPORTED"
    client.call("close", **session)
    assert error(lambda: client.call("report", **session)).error_code == "NO_SESSION"
    request = Request(
        client.url + "/version", data=b"{}", headers={"Authorization": "Bearer wrong-token-x"}
    )
    with pytest.raises(HTTPError) as unauthorized:
        urlopen(request, timeout=10)
    body = json.loads(unauthorized.value.read())
    assert unauthorized.value.code == 401 and body["error"]["code"] == "UNAUTHORIZED"
    assert unauthorized.value.headers["X-Peeky-Protocol"] == str(PROTOCOL_VERSION)


def test_uncaught_debuggee_exception_terminates_with_traceback(worker):
    root, client, _ = worker
    (root / "boom.py").write_text("print('before')\nraise ValueError('boom')\n")
    session = {"session_id": client.call("launch", program="boom.py")["session_id"]}
    assert stopped(client, session)["state"] == "terminated"
    report = client.call("report", **session)
    assert "before" in report["output"] and "ValueError: boom" in report["output"]
    client.call("close", **session)


def test_close_under_output_flood_returns_promptly_and_reaps(worker):
    root, client, _ = worker
    (root / "flood.py").write_text("while True:\n    print('x' * 200)\n")
    session = {"session_id": client.call("launch", program="flood.py")["session_id"]}
    time.sleep(1)
    started = time.monotonic()
    client.call("close", **session)
    assert time.monotonic() - started < 10
    time.sleep(0.5)
    leftover = subprocess.run(["pgrep", "-f", str(root / "flood.py")], capture_output=True)
    assert leftover.stdout == b""


def test_concurrent_wait_and_close_do_not_deadlock(worker):
    root, client, _ = worker
    (root / "sleepy.py").write_text("import time\ntime.sleep(30)\n")
    session = {"session_id": client.call("launch", program="sleepy.py")["session_id"]}
    outcomes = []

    def waiter():
        try:
            outcomes.append(client.call("wait", **session, timeout=2)["state"])
        except PeekyError as exc:
            outcomes.append(exc.error_code)

    threads = [threading.Thread(target=waiter) for _ in range(3)]
    for thread in threads:
        thread.start()
    started = time.monotonic()
    client.call("close", **session)
    for thread in threads:
        thread.join(timeout=10)
    assert time.monotonic() - started < 10 and all(not t.is_alive() for t in threads)
    assert set(outcomes) <= {"running", "idle", "NO_SESSION"} and len(outcomes) == 3


@pytest.mark.parametrize(
    "exception, code, closed",
    [
        (ValueError("Stale stop_id or program is not stopped"), "STALE_STOP", False),
        (RuntimeError("Prepared build provenance mismatch"), "PROVENANCE_CHANGED", True),
        (TimeoutError("Debugger response timed out"), "TIMEOUT", True),
        (ValueError("Only writable i32 locals support Rust mutation"), "MUTATION_REJECTED", False),
        (ValueError("Value view budget exhausted"), "BUDGET_EXHAUSTED", False),
        (RuntimeError("adapter exploded"), "DEBUGGER_ERROR", True),
        (ValueError("something else"), "INVALID_REQUEST", False),
    ],
)
def test_classification_is_stable(exception, code, closed):
    payload = classify(exception)
    assert payload["code"] == code and payload["session_closed"] is closed and payload["hint"]
