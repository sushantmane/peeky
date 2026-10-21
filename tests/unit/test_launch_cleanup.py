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
# event) and dies ("announce-setsid": the program starts a session of its own, so only
# the announcement shows it is the session's), and Peeky reads the end of its output only
# once it has exited (a
# holder keeps the pipe open until the adapter's children are handed on, #168: else
# Peeky could list them while the adapter was still exiting); "attach": the adapter
# announces a program it did not start (pid in argv 3). Every process started writes
# "role pid" to the record.
FAKE_ADAPTER = r"""
import json, os, subprocess, sys, time
# The program's standard streams are never the adapter's DAP pipes.
how, record = sys.argv[1], sys.argv[2]
PROGRAM = r'''
import os, signal, subprocess, sys, time
# "announce-setsid": the program leaves the adapter's session; else only its group.
os.setsid() if sys.argv[2] == "announce-setsid" else os.setpgrp()
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
HOLDER = '''
import os, sys, time
while os.getppid() == int(sys.argv[1]):
    time.sleep(0.01)
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
        if how in ("announce-die", "announce-setsid"):
            subprocess.Popen([sys.executable, "-c", PROGRAM, record, how], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            write({"seq": 0, "type": "event", "event": "process",
                   "body": {"name": "app", "systemProcessId": started()}})
            time.sleep(0.5)
            subprocess.Popen([sys.executable, "-c", HOLDER, str(os.getpid())], stdin=subprocess.DEVNULL)
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
    # Reparented once the adapter died: the program is known by the pid the adapter
    # announced and its start, its child by the program's group, both still in the
    # adapter's session.
    assert not running(pids["program"]) and not running(pids["child"])
    assert error["code"] == "DEBUGGER_ERROR" and "adapter closed" in error["message"]
    listed = ", ".join(map(str, sorted(pids.values())))
    assert error["cleanup"].startswith(f"terminated pids {listed}; the debugger was gone")


def test_a_program_no_listing_saw_before_its_adapter_died_is_ended_with_its_child(
    tmp_path, leftovers, monkeypatch
):
    # #168: under load, the listing the announcement asks for can come only after the
    # adapter died, when no ancestry leads to the program any more. It was then ended
    # by its announced pid alone, and the child in its group outlived it. Here no
    # listing runs before the end: what proves them the session's is the adapter's
    # session, which a process can only inherit.
    from peeky.dap import DAP

    monkeypatch.setattr(DAP, "_watch_family", lambda self: None)
    record = tmp_path / "started.txt"
    error, _ = launch(tmp_path, target("announce-die", record), deadline=10)
    pids = started(record)
    leftovers.extend(pids.values())
    assert not running(pids["program"]) and not running(pids["child"])
    listed = ", ".join(map(str, sorted(pids.values())))
    assert error["cleanup"].startswith(f"terminated pids {listed}; the debugger was gone")


def test_a_program_only_its_announcement_shows_is_ended_with_its_group(
    tmp_path, leftovers, monkeypatch
):
    # PR #170 review: the program leaves the adapter's session (`setsid`) and no listing
    # runs before the adapter dies, so only its announced pid (and start) says it is the
    # session's. Its child, in its group, outlived it: the announced program's group is
    # ended too.
    from peeky.dap import DAP

    monkeypatch.setattr(DAP, "_watch_family", lambda self: None)
    record = tmp_path / "started.txt"
    error, _ = launch(tmp_path, target("announce-setsid", record), deadline=10)
    pids = started(record)
    leftovers.extend(pids.values())
    assert not running(pids["program"]) and not running(pids["child"])
    listed = ", ".join(map(str, sorted(pids.values())))
    assert error["cleanup"].startswith(f"terminated pids {listed}; the debugger was gone")


def test_without_its_announcement_such_a_program_is_not_known(tmp_path, leftovers, monkeypatch):
    # The property the test above relies on: with the announcement ignored, nothing else
    # names the program (it left the adapter's session, and no listing saw its ancestry).
    from peeky.dap import DAP

    monkeypatch.setattr(DAP, "_watch_family", lambda self: None)
    monkeypatch.setattr(processes.Family, "announce", lambda self, pid, start: None)
    record = tmp_path / "started.txt"
    error, _ = launch(tmp_path, target("announce-setsid", record), deadline=10)
    pids = started(record)
    leftovers.extend(pids.values())
    assert running(pids["program"]) and running(pids["child"])
    assert error["cleanup"].startswith("nothing Peeky could identify was still running")


def test_a_dead_adapter_stays_unreaped_until_close(tmp_path):
    # PR #170 review (High): `poll()` reaped the dead adapter at the next listing, and
    # its pid, and so its group and session ids, could then name an unrelated session's
    # leader, whose processes the session rule and close() would end. Unreaped, a zombie
    # keeps the pid; only close() reaps it, after its last signal.
    from peeky.dap import DAP

    dap = DAP(command=(sys.executable, "-c", "import time; time.sleep(0.2)"))
    pid = dap.process.pid
    polled = []
    real_poll = dap.process.poll
    dap.process.poll = lambda: polled.append(1) or real_poll()
    try:
        assert dap.family.session == pid
        until(lambda: (processes.table() or {}).get(pid, (0, 0, "", "R"))[3].startswith("Z"))
        dap.family_wake.set()
        time.sleep(1.5)  # listings after its death
        entry = processes.table()[pid]
        assert entry[3].startswith("Z") and dap.process.returncode is None
        assert dap.family.session == pid and not polled
    finally:
        dap.close()
    assert dap.process.returncode is not None and pid not in (processes.table() or {})


def test_an_adapter_reaped_elsewhere_proves_no_session_and_signals_no_group(tmp_path, monkeypatch):
    from peeky.dap import DAP

    dap = DAP(command=(sys.executable, "-c", "pass"))
    dap.process.wait(timeout=10)  # reaped by someone else than close()
    signalled = []
    monkeypatch.setattr(os, "killpg", lambda group, sig: signalled.append(group))
    dap._list_family()
    dap._kill_group()
    assert dap.family.session is None and signalled == []
    dap.close()
    # Unless a process Peeky recorded is still in its group and session: their ids are
    # then still the adapter's, never reused while they have members (a launched JVM
    # shares java-debug's group).
    member = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=False
    )
    try:
        dap = DAP(command=(sys.executable, "-c", "import time; time.sleep(30)"))
        group = dap.process.pid
        table = {
            group: (1, group, "s", "S", group),
            member.pid: (1, group, "m", "S", group),
        }
        monkeypatch.setattr(processes, "table", lambda: table)
        dap.family.kin[member.pid] = "m"
        dap.process.kill()
        dap.process.wait(timeout=10)
        dap._list_family()
        dap._kill_group(signal.SIGTERM)
        assert dap.family.session == group and signalled == [group]
        monkeypatch.undo()
        dap.close()
    finally:
        member.kill()
        member.wait()


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
    assert me[4] == os.getsid(0) == processes.session_id(os.getpid())
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
# JVM does) and takes 0.3 s to end on SIGTERM; "stop": the adapter reports a breakpoint
# stop once configured. Each writes "role pid" to the record.
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
    if command == "configurationDone" and how == "stop":
        write({"seq": 0, "type": "event", "event": "stopped",
               "body": {"reason": "breakpoint", "threadId": 1}})
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


def start_running(tmp_path, how, leftovers, roles, **options):
    record = tmp_path / "started.txt"
    (tmp_path / "main.py").write_text("pass\n")
    worker = DebugWorker(tmp_path, adapter=running_target(how, record))
    started_session = worker.dispatch(
        "launch", {"program": "main.py", "breakpoints": [], **options}
    )
    until(lambda: roles <= set(started(record)))
    pids = started(record)
    leftovers.extend(pids.values())
    # The session's processes were listed while their ancestry still showed them.
    until(lambda: set(pids.values()) <= set(worker.dap.family.kin))
    return worker, {"session_id": started_session["session_id"]}, pids, record


def kill_adapter(dap):
    """`kill -9` of the adapter, left unreaped as a crash leaves it: Peeky reaps it only
    at close (PR #170 review)."""
    os.kill(dap.process.pid, signal.SIGKILL)
    until(lambda: not dap._adapter_running())


def fail_after_adapter_death(worker, session):
    kill_adapter(worker.dap)
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
    assert "in a session of its own and whose parent had exited, may survive" in error["cleanup"]


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
    # The announced debuggee counts with its group (PR #170 review: it may have left
    # the adapter's session).
    assert set(found) == {101, 200, 201, 202, 302} and groups == {200, 202, 302}
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
        305: (302, 302, "s305", "S"),  # in the announced process's group
    }
    found, groups = family.record(later, root_running=False)
    assert set(found) == {201, 202, 203, 302, 305} and groups == {200, 202, 302}
    assert family.groups([201, 302], later) == {200, 302}
    # A pid reused by another process (another start) is no longer the session's.
    reused = {**later, 202: (1, 202, "another start", "S"), 203: (1, 200, "s203", "Z")}
    found, groups = family.record(reused, root_running=False)
    assert set(found) == {201, 302, 305} and groups == {200, 302}
    assert family.groups([202, 203], reused) == set()
    # An announced pid that names another process now: neither it nor its group.
    family = processes.Family(root=100)
    family.announce(302, "s302")
    other = {302: (1, 302, "another start", "S"), 306: (302, 302, "s306", "S")}
    assert family.record(other, root_running=False) == ({}, set())
    assert family.groups([302], other) == set()


def test_what_stays_in_the_adapters_session_is_the_sessions_whatever_its_ancestry():
    own = os.getsid(0)
    family = processes.Family(root=100, session=100)
    table = {
        100: (1, 100, "s100", "Z", 100),  # the adapter, exiting
        200: (1, 200, "s200", "S", 100),  # the debuggee, handed to init, never listed
        201: (200, 200, "s201", "S", 100),  # its child, in its group
        202: (1, 202, "s202", "S", 202),  # left the session (setsid), parent gone: unknown
        203: (201, 203, "s203", "S", 203),  # left it too, but started by a member
        300: (1, 300, "s300", "S", 300),  # someone else's
        301: (1, 301, "s301", "S", own),  # in Peeky's own session
        302: (1, 302, "s302", "Z", 100),  # exited
    }
    found, groups = family.record(table, root_running=False)
    assert set(found) == {200, 201, 203} and groups == {200, 203}
    # Peeky's own session never counts (an adapter that could not lead its own), and
    # neither does an unknown one.
    for session in (own, None):
        family = processes.Family(root=100, session=session)
        assert family.record(table, root_running=False) == ({}, set())


def test_a_session_failed_with_no_request_in_flight_keeps_its_cleanup(tmp_path, leftovers):
    from peeky.worker import SessionFailed

    worker, session, pids, _ = start_running(
        tmp_path, "stop", leftovers, {"program"}, max_pause=0.2
    )
    until(lambda: worker.dap.pending_stop(worker.cursor) is not None)
    kill_adapter(worker.dap)
    time.sleep(0.3)
    # The pause guard, with no client waiting, fails resuming the stop: the session is
    # kept as a failed one, and ended (PR #147 review 2: `--max-pause`, `kill -9`).
    worker.guard_pause()
    assert worker.dap is None and worker.failed and not running(pids["program"])
    cleanup = worker.dispatch("report", session)["cleanup"]
    assert cleanup.startswith(f"terminated pid {pids['program']}; the debugger was gone")
    # Before, the error replayed to the next request, and `close`, said nothing of it.
    with pytest.raises(SessionFailed) as again:
        worker.dispatch("inspect", {**session, "stop_id": "any"})
    assert classify(again.value)["cleanup"] == cleanup
    assert worker.dispatch("close", session) == {"state": "idle", "cleanup": cleanup}
