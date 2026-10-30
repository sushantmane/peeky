"""Protocol v1 error codes and lifecycle conformance against real debugpy (issue #6)."""

import json
import os
import signal
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from peeky.client import Client
from peeky.errors import PROTOCOL_VERSION, PeekyError, classify
from peeky.worker import make_server

TOKEN = "lifecycle-test-token-123456-0123456789abcdef"


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
        (FileNotFoundError(2, "No such file or directory", "x.json"), "INVALID_REQUEST", True),
        (AttributeError("'X' object has no attribute 'lines'"), "INTERNAL_ERROR", False),
        (TypeError("unsupported operand"), "INVALID_REQUEST", False),
    ],
)
def test_classification_is_stable(exception, code, closed):
    payload = classify(exception)
    assert payload["code"] == code and payload["session_closed"] is closed and payload["hint"]


def test_a_crashed_adapter_reaches_the_client_as_debugger_error_with_a_pause_budget(worker):
    # Review round 5: the pause guard met the dead transport first, closed the session,
    # and the client got NO_SESSION ("Missing or stale session_id") instead.
    import os
    import signal

    root, client, server = worker
    (root / "sleepy.py").write_text(
        "import time\nfor i in range(30):\n    time.sleep(1)\n    if i > 1000:\n        print(i)\n"
    )
    launch = client.call("launch", program="sleepy.py", breakpoints=[{"line": 5}], max_pause=5)
    session = {"session_id": launch["session_id"]}
    time.sleep(1)
    os.kill(server.worker.dap.process.pid, signal.SIGKILL)
    for _ in range(8):
        server.worker.guard_pause()
        time.sleep(0.25)
    failure = error(lambda: client.call("wait", **session, timeout=2))
    assert failure.error_code == "DEBUGGER_ERROR" and "Debugger adapter closed" in str(failure)


# Loaded by every Python process given its directory on PYTHONPATH: the debuggee
# (`debugpy --connect …`) records its pid and hangs before pydevd can send `initialized`,
# as a pydevd blocked listing a directory above the program does (#143).
HANG_BEFORE_INITIALIZED = """
import os, sys, time
if os.environ.get("PEEKY_TEST_HANG") and "--connect" in sys.argv:
    with open(os.environ["PEEKY_TEST_HANG"], "a") as record:
        record.write(f"{os.getpid()}\\n")
    time.sleep(600)
"""


def test_a_python_program_that_never_starts_is_not_left_running(worker, monkeypatch):
    from peeky import processes

    root, client, server = worker
    site, record = root / "site", root / "debuggee.pid"
    site.mkdir()
    (site / "sitecustomize.py").write_text(HANG_BEFORE_INITIALIZED)
    (root / "main.py").write_text("print('never runs')\n")
    monkeypatch.setenv("PYTHONPATH", str(site))
    monkeypatch.setenv("PEEKY_TEST_HANG", str(record))
    server.worker.launch_deadline_s = 8
    pids = []
    try:
        failure = error(lambda: client.call("launch", program="main.py", breakpoints=[{"line": 1}]))
        pids = [int(pid) for pid in record.read_text().split()]
        table = processes.table()
        # debugpy's launcher started it in a process group of its own: ending the
        # adapter's group never reached it, and it was left to PID 1 before #143.
        assert len(pids) == 1 and not processes.alive(table.get(pids[0]))
        assert failure.error_code == "TIMEOUT" and failure.payload["session_closed"] is True
        assert failure.payload["message"].startswith("The debuggee did not start")
        assert "can read its path and every directory above it" in failure.hint
        assert failure.payload["cleanup"] == f"terminated pid {pids[0]}"
    finally:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


# The program starts a child in its process group and one in a session of its own, then
# loops at the breakpoint (PR #147 review, `py1.py`).
FAMILY = """import subprocess, time
same = subprocess.Popen(["sleep", "1001"])
own = subprocess.Popen(["sleep", "1002"], start_new_session=True)
with open(RECORD, "w") as record:
    record.write(f"{same.pid} {own.pid}")
total = 0
for i in range(100000):
    total += i  # STOP
    time.sleep(0.01)
"""


def test_what_a_python_program_started_is_ended_when_its_debugger_dies(worker):
    from peeky import processes

    root, client, server = worker
    record = root / "children.txt"
    (root / "family.py").write_text(FAMILY.replace("RECORD", repr(str(record))))
    line = next(i for i, text in enumerate(FAMILY.splitlines(), 1) if "# STOP" in text)
    session = {
        "session_id": client.call("launch", program="family.py", breakpoints=[{"line": line}])[
            "session_id"
        ]
    }
    children = []
    try:
        stop = stopped(client, session)
        children = [int(pid) for pid in record.read_text().split()]
        dap = server.worker.dap
        deadline = time.monotonic() + 5
        # Listed at the stop, while their ancestry still led to the adapter.
        while not set(children) <= set(dap.family.kin):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        # `kill -9`, unreaped as a crash leaves it: Peeky reaps it at close (PR #170).
        os.kill(dap.process.pid, signal.SIGKILL)
        while dap._adapter_running():
            assert time.monotonic() < deadline + 5
            time.sleep(0.05)
        failure = error(lambda: client.call("inspect", **session, stop_id=stop["stop_id"]))
        # debugpy's launcher ends the program's group when the adapter dies; before, the
        # child in a session of its own survived, and `cleanup` said nothing was left.
        table = processes.table()
        assert not any(processes.alive(table.get(pid)) for pid in children)
        assert failure.error_code == "DEBUGGER_ERROR"
        assert str(children[1]) in failure.payload["cleanup"]
        assert failure.payload["cleanup"].startswith("terminated pid")
    finally:
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
