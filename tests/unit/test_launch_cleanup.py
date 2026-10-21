"""A failed or timed-out launch ends every process the session started (#143).

A fake adapter starts the program the way debugpy does: a launcher in the adapter's
process group starts it in a group of its own, and it never reaches the debugger (it
never sends `initialized`). Ending the adapter's group does not reach it; Peeky must.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError

import pytest

from peeky import processes
from peeky.adapters import PythonAdapter
from peeky.client import Client
from peeky.errors import classify
from peeky.worker import DebugWorker, make_server

# argv: how, record. "hang": a launcher starts the program (and the program a child of
# its own) and nothing ever initializes; "stubborn": the same, and the program ignores
# SIGTERM; "announce-die": the adapter starts the program, announces its pid (`process`
# event) and dies; "attach": the adapter announces a program it did not start (pid in
# argv 3). Every process started writes "role pid" to the record.
FAKE_ADAPTER = r"""
import json, os, subprocess, sys, time
# The program's standard streams are never the adapter's DAP pipes.
how, record = sys.argv[1], sys.argv[2]
PROGRAM = r'''
import os, signal, subprocess, sys, time
os.setpgrp()
if sys.argv[2] == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
with open(sys.argv[1], "a") as out:
    out.write(f"program {os.getpid()}\nchild {child.pid}\n")
time.sleep(600)
'''
LAUNCHER = '''
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]]).wait()
'''
def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
def write(body):
    data = json.dumps(body).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    sys.stdout.buffer.flush()
def started():
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        lines = open(record).read().splitlines() if os.path.exists(record) else []
        if any(line.startswith("child ") for line in lines):
            return int(next(l for l in lines if l.startswith("program ")).split()[1])
        time.sleep(0.02)
    raise SystemExit("the program never started")
while True:
    request = read()
    command = request["command"]
    if command in ("launch", "attach"):
        if how == "attach":
            write({"seq": 0, "type": "event", "event": "process",
                   "body": {"name": "app", "systemProcessId": int(sys.argv[3])}})
            continue
        if how == "announce-die":
            subprocess.Popen([sys.executable, "-c", PROGRAM, record, how], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            write({"seq": 0, "type": "event", "event": "process",
                   "body": {"name": "app", "systemProcessId": started()}})
            time.sleep(0.5)
            os._exit(1)
        subprocess.Popen([sys.executable, "-c", LAUNCHER, PROGRAM, record, how], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        started()
        continue  # debugpy answers `launch` only once the program has connected
    write({"seq": 0, "type": "response", "request_seq": request["seq"], "success": True,
           "command": command, "body": {}})
"""


def target(how, record, *extra, attach=False):
    class Runtime(PythonAdapter):
        dap_request = "attach" if attach else "launch"
        terminate_debuggee = not attach

        @property
        def command(self):
            return (sys.executable, "-c", FAKE_ADAPTER, how, str(record), *map(str, extra))

    return Runtime()


def started(record):
    lines = record.read_text().splitlines() if record.exists() else []
    return {role: int(pid) for role, pid in (line.split() for line in lines)}


def running(pid):
    table = processes.table()
    return processes.alive(table.get(pid)) if table is not None else True


@pytest.fixture
def leftovers():
    """Pids a failing (pre-#143) run leaves behind: killed after the test, by pid."""
    pids = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def launch(tmp_path, runtime, deadline=2.0):
    (tmp_path / "main.py").write_text("print('never runs')\n")
    worker = DebugWorker(tmp_path, adapter=runtime)
    worker.launch_deadline_s = deadline
    began = time.monotonic()
    with pytest.raises(Exception) as failure:
        worker.dispatch("launch", {"program": "main.py", "breakpoints": [{"line": 1}]})
    assert worker.dap is None and worker.session is None
    return classify(failure.value), time.monotonic() - began


@pytest.mark.parametrize("how", ["hang", "stubborn"])
def test_a_program_that_never_starts_is_ended_with_its_group(tmp_path, leftovers, how):
    record = tmp_path / "started.txt"
    error, took = launch(tmp_path, target(how, record))
    pids = started(record)
    leftovers.extend(pids.values())
    assert set(pids) == {"program", "child"}
    assert not running(pids["program"]) and not running(pids["child"])
    assert error["code"] == "TIMEOUT" and error["session_closed"] is True
    assert error["message"].startswith("The debuggee did not start: the debugger never ")
    assert "check that it can read its own path" in error["hint"]
    # The program and what it started, which shared its group; SIGKILL after the grace
    # for one that ignores SIGTERM.
    assert error["cleanup"] == f"terminated pids {', '.join(map(str, sorted(pids.values())))}"
    assert took < 2.0 + 1.25 + (2.5 if how == "stubborn" else 1.0)


def test_a_program_whose_adapter_died_is_ended_by_its_announced_pid(tmp_path, leftovers):
    record = tmp_path / "started.txt"
    error, _ = launch(tmp_path, target("announce-die", record), deadline=10)
    pids = started(record)
    leftovers.extend(pids.values())
    # Reparented once the adapter died: listed (as the adapter announced the program)
    # while their ancestry still led to it, they are known by pid and start time.
    assert not running(pids["program"]) and not running(pids["child"])
    assert error["code"] == "DEBUGGER_ERROR" and "adapter closed" in error["message"]
    listed = ", ".join(map(str, sorted(pids.values())))
    assert error["cleanup"].startswith(f"terminated pids {listed}; the debugger was gone")


def test_an_attached_program_is_never_ended(tmp_path, leftovers):
    program = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    leftovers.append(program.pid)
    try:
        error, _ = launch(tmp_path, target("attach", tmp_path / "x", program.pid, attach=True))
        # Announced, but Peeky did not start it: a failed attach leaves it running.
        assert error["code"] == "TIMEOUT" and "did not start" not in error["message"]
        assert error["cleanup"] == "detached; the attached program keeps running"
        assert program.poll() is None and running(program.pid)
    finally:
        program.kill()
        program.wait()


def test_the_http_error_says_what_became_of_the_program(tmp_path, leftovers):
    record = tmp_path / "started.txt"
    (tmp_path / "main.py").write_text("print('never runs')\n")
    token = "cleanup-test-token-123456"
    server = make_server(tmp_path, token, 0, adapter=target("hang", record))
    server.worker.launch_deadline_s = 2.0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", token)
        with pytest.raises(HTTPError) as caught:
            client.call("launch", program="main.py", breakpoints=[{"line": 1}])
        error = json.load(caught.value)["error"]
        pids = started(record)
        leftovers.extend(pids.values())
        assert error["code"] == "TIMEOUT" and not running(pids["program"])
        assert error["cleanup"].startswith("terminated pids ")
        assert str(pids["program"]) in error["cleanup"]
    finally:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_the_process_table_names_each_process_once_by_its_start():
    table = processes.table()
    me = table[os.getpid()]
    assert me[:2] == (os.getppid(), os.getpgid(0)) and processes.alive(me)
    # The identity close() compares: one process's start, as the whole table reads it.
    assert processes.start_time(os.getpid()) == me[2]
    assert processes.start_time(2**22 + 12345) is None
    tree = {
        10: (1, 10, "a", "S"),
        11: (10, 11, "b", "S"),
        12: (11, 11, "c", "Z"),
        13: (1, 13, "d", "S"),
    }
    assert processes.descendants(tree, 10) == {11, 12} and processes.descendants(tree, 12) == set()
    assert not processes.alive(tree[12]) and not processes.alive(None)


def test_a_pid_announced_then_reused_is_never_ended():
    from peeky.dap import DAP

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        dap = DAP(command=(sys.executable, "-c", "import time; time.sleep(60)"))
        # The adapter announced this pid when it named another process (another start).
        dap.process_pid = other.pid
        dap.family.announce(other.pid, "an earlier process's start")
        assert dap.close() == "nothing Peeky could identify was still running"
        assert other.poll() is None and running(other.pid)
    finally:
        other.kill()
        other.wait()


# A fake adapter whose launch runs: it starts the program, answers every request and
# announces the program (`process`). argv: how, record. "family": the program, in a
# group of its own, starts a child in that group and one in a session of its own (as
# `c2/hang.c` of PR #147's review does); "leave": it starts a child in a session of its
# own and exits 1.5 s later; "graceful": it stays in the adapter's group (as java-debug's
# JVM does) and takes 0.3 s to end on SIGTERM. Each writes "role pid" to the record.
RUNNING_PROGRAM = r"""
import os, signal, sys, time
how, record = sys.argv[1], sys.argv[2]
def note(role, pid):
    with open(record, "a") as out:
        out.write(f"{role} {pid}\n")
def graceful(*_):
    time.sleep(0.3)
    note("graceful", os.getpid())
    os._exit(0)
if how == "graceful":
    signal.signal(signal.SIGTERM, graceful)
else:
    os.setpgrp()
if how == "family" and (child := os.fork()) == 0:
    time.sleep(600)
    os._exit(0)
if how == "family":
    note("same-group", child)
if how in ("family", "leave") and (child := os.fork()) == 0:
    os.setsid()
    time.sleep(600)
    os._exit(0)
if how in ("family", "leave"):
    note("setsid", child)
note("program", os.getpid())
time.sleep(1.5 if how == "leave" else 600)
"""
RUNNING_ADAPTER = r"""
import json, os, subprocess, sys, time
how, record, program_source = sys.argv[1], sys.argv[2], sys.argv[3]
def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
def write(body):
    data = json.dumps(body).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    sys.stdout.buffer.flush()
def program():
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        lines = open(record).read().splitlines() if os.path.exists(record) else []
        if mine := [l for l in lines if l.startswith("program ")]:
            return int(mine[0].split()[1])
        time.sleep(0.02)
    raise SystemExit("the program never started")
while True:
    request = read()
    command, body = request["command"], {}
    if command == "launch":
        subprocess.Popen([sys.executable, "-c", program_source, how, record],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        pid = program()
        write({"seq": 0, "type": "event", "event": "initialized", "body": {}})
        write({"seq": 0, "type": "event", "event": "process",
               "body": {"name": "app", "systemProcessId": pid}})
    if command == "setBreakpoints":
        points = request["arguments"].get("breakpoints", [])
        body = {"breakpoints": [{"id": 1, "verified": True, "line": p["line"]} for p in points]}
    write({"seq": 0, "type": "response", "request_seq": request["seq"], "success": True,
           "command": command, "body": body})
"""


def running_target(how, record):
    class Runtime(PythonAdapter):
        @property
        def command(self):
            return (sys.executable, "-c", RUNNING_ADAPTER, how, str(record), RUNNING_PROGRAM)

    return Runtime()


def until(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def start_running(tmp_path, how, leftovers, roles):
    record = tmp_path / "started.txt"
    (tmp_path / "main.py").write_text("pass\n")
    worker = DebugWorker(tmp_path, adapter=running_target(how, record))
    started_session = worker.dispatch("launch", {"program": "main.py", "breakpoints": []})
    until(lambda: roles <= set(started(record)))
    pids = started(record)
    leftovers.extend(pids.values())
    # The session's processes were listed while their ancestry still showed them.
    until(lambda: set(pids.values()) <= set(worker.dap.family.kin))
    return worker, {"session_id": started_session["session_id"]}, pids, record


def fail_after_adapter_death(worker, session):
    worker.dap.process.kill()
    worker.dap.process.wait(timeout=3)
    with pytest.raises(RuntimeError) as failure:
        for _ in range(10):
            worker.dispatch("wait", {**session, "timeout": 1})
    return classify(failure.value)


def test_what_the_program_started_is_ended_after_its_debugger_dies(tmp_path, leftovers):
    worker, session, pids, _ = start_running(
        tmp_path, "family", leftovers, {"program", "same-group", "setsid"}
    )
    error = fail_after_adapter_death(worker, session)
    # Before, the child in a session of its own outlived the program's group; now every
    # process listed is ended, and the message says what may have escaped the listing.
    assert not any(running(pid) for pid in pids.values())
    listed = ", ".join(map(str, sorted(pids.values())))
    assert error["code"] == "DEBUGGER_ERROR"
    assert error["cleanup"].startswith(f"terminated pids {listed}; the debugger was gone: ")
    assert (
        "in a process group of its own and whose parent had exited, may survive"
        in (error["cleanup"])
    )


def test_children_a_program_left_behind_end_with_its_session(tmp_path, leftovers):
    worker, session, pids, _ = start_running(tmp_path, "leave", leftovers, {"program", "setsid"})
    until(lambda: not running(pids["program"]))
    # Reparented once the program exited: no ancestry leads to it any more.
    assert running(pids["setsid"])
    closed = worker.dispatch("close", session)
    assert closed["cleanup"] == f"terminated pid {pids['setsid']}"
    assert not running(pids["setsid"])


def test_a_program_in_the_adapters_group_gets_its_grace(tmp_path, leftovers):
    worker, session, pids, record = start_running(tmp_path, "graceful", leftovers, {"program"})
    began = time.monotonic()
    error = fail_after_adapter_death(worker, session)
    # A launched JVM shares java-debug's group: SIGTERM, then SIGKILL only after the
    # grace, so its shutdown hooks run.
    assert started(record).get("graceful") == pids["program"]
    assert error["cleanup"].startswith(f"terminated pid {pids['program']}; ")
    assert not running(pids["program"]) and time.monotonic() - began < 3


def test_the_family_is_known_by_ancestry_identity_and_group():
    family = processes.Family(root=100)
    own = os.getpgrp()
    table = {
        100: (1, 100, "s100", "S"),  # the adapter
        101: (100, 100, "s101", "S"),  # its helper, in its group
        200: (101, 200, "s200", "S"),  # the debuggee, in a group of its own
        201: (200, 200, "s201", "S"),  # its child, same group
        202: (200, 202, "s202", "S"),  # its child, in a session of its own
        300: (1, 300, "s300", "S"),  # unrelated
        301: (1, own, "s301", "S"),  # unrelated, in Peeky's own group
        302: (1, 302, "s302", "S"),  # announced, but never seen descended from the adapter
    }
    family.announce(302, "s302")
    found, groups = family.record(table, root_running=True)
    assert set(found) == {101, 200, 201, 202, 302} and groups == {200, 202}
    # The adapter and the debuggee die: the children are reparented, and one more is
    # started in the debuggee's group after the listing (never listed itself).
    later = {
        201: (1, 200, "s201", "S"),
        202: (1, 202, "s202", "S"),
        203: (1, 200, "s203", "S"),
        204: (1, 204, "s204", "S"),  # its own group, parent gone: cannot be told apart
        300: (1, 300, "s300", "S"),
        301: (1, own, "s301", "S"),
        302: (1, 302, "s302", "S"),
        305: (302, 302, "s305", "S"),  # the announced process's group is no proof
    }
    found, groups = family.record(later, root_running=False)
    assert set(found) == {201, 202, 203, 302} and groups == {200, 202}
    assert family.groups([201, 302], later) == {200}
    # A pid reused by another process (another start) is no longer the session's.
    reused = {**later, 202: (1, 202, "another start", "S"), 203: (1, 200, "s203", "Z")}
    found, groups = family.record(reused, root_running=False)
    assert set(found) == {201, 302} and groups == {200}
    assert family.groups([202, 203], reused) == set()
