"""Agent-speed debugging: bounded pauses, early-ending probes and background jobs."""

import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from peeky.cli import main
from peeky.client import Client
from peeky.errors import PeekyError
from peeky.probe import Probe, run_probe
from peeky.worker import DebugWorker, make_server

TOKEN = "agent-speed-test-token-123456"
LOOP = """import time


def work(n):
    total = 0
    for i in range(n):
        total += i
    return total


print("TOTAL", work(3))
time.sleep(SLEEP)
print("DONE")
"""


@pytest.fixture
def target(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "loop.py").write_text(LOOP.replace("SLEEP", "0"))
    (tmp_path / "slow.py").write_text(LOOP.replace("SLEEP", "8"))
    return tmp_path


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr()


def test_probe_reports_pause_per_hit_and_can_end_at_the_last_hit(target):
    started = time.monotonic()
    result = run_probe(target, Probe("slow.py", 7, ("i",), hits=2, until="hits"))
    assert result["status"] == "completed" and result["ended"] == "hits"
    assert time.monotonic() - started < 6 and "DONE" not in result["output"]
    assert len(result["pauses_ms"]) == 2 and all(0 <= p < 5000 for p in result["pauses_ms"])
    result = run_probe(target, Probe("loop.py", 7, ("i",), hits=2))
    assert result["ended"] == "exit" and "DONE" in result["output"]
    with pytest.raises(ValueError, match="until"):
        Probe("loop.py", 7, until="never")


def launch(worker, max_pause):
    session = {
        "session_id": worker.dispatch(
            "launch", {"program": "loop.py", "breakpoints": [{"line": 7}], "max_pause": max_pause}
        )["session_id"]
    }
    return session


def wait_stopped(worker, session):
    for _ in range(30):
        state = worker.dispatch("wait", {**session, "timeout": 2})
        if state["state"] != "running":
            return state
    raise AssertionError("did not stop")


def test_a_stop_older_than_the_pause_budget_is_auto_resumed(target):
    worker = DebugWorker(target)
    try:
        session = launch(worker, 0.5)
        state = wait_stopped(worker, session)
        assert state["state"] == "stopped" and 0 < state["resume_in_s"] <= 0.5
        stop = {**session, "stop_id": state["stop_id"]}
        assert "paused_s" in worker.dispatch("inspect", stop)
        worker.guard_pause()
        assert worker.state == "stopped"
        time.sleep(0.7)
        worker.guard_pause()
        with pytest.raises(ValueError, match="auto-resumed after its 0.5 s pause budget"):
            worker.dispatch("inspect", stop)
        state = wait_stopped(worker, session)
        assert state["auto_resumed"] == 1 and state["location"].endswith("loop.py:7")
        assert any(entry["operation"] == "auto_resume" for entry in worker.audit)
        events = worker.dispatch("report", session)["debugger_events"]
        assert [e["event"] for e in events["recent"]].count("stopped") >= 2
    finally:
        worker.close()


def test_unobserved_stops_are_auto_resumed_too(target):
    worker = DebugWorker(target)
    try:
        session = launch(worker, 0.3)
        deadline = time.monotonic() + 20
        # The client never calls wait: the guard alone must release every stop.
        while len(worker.auto_resumed) < 3 and time.monotonic() < deadline:
            time.sleep(0.1)
            worker.guard_pause()
        assert len(worker.auto_resumed) == 3
        state = wait_stopped(worker, session)
        report = worker.dispatch("report", session)
        assert state["state"] == "terminated" and "TOTAL 3" in report["output"]
    finally:
        worker.close()


@pytest.mark.parametrize("value", [0, -1, 301, "5", float("nan")])
def test_pause_budgets_are_validated(target, value):
    worker = DebugWorker(target)
    with pytest.raises(ValueError, match="max_pause"):
        launch(worker, value)
    assert worker.dap is None


def test_without_a_budget_stops_are_never_auto_resumed(target):
    worker = DebugWorker(target)
    try:
        session = launch(worker, None)
        state = wait_stopped(worker, session)
        time.sleep(0.5)
        worker.guard_pause()
        assert worker.state == "stopped" and "resume_in_s" not in worker.pause_status()
        worker.dispatch("inspect", {**session, "stop_id": state["stop_id"]})
    finally:
        worker.close()


def test_worker_server_enforces_the_budget_over_http(target):
    server = make_server(target, TOKEN, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    guard = threading.Event()

    def tick():
        while not guard.is_set():
            server.worker.guard_pause()
            time.sleep(0.1)

    threading.Thread(target=tick, daemon=True).start()
    client = Client(f"http://127.0.0.1:{server.server_port}", TOKEN)
    try:
        launched = client.call("launch", program="loop.py", breakpoints=[{"line": 7}], max_pause=1)
        session = {"session_id": launched["session_id"]}
        state = client.call("wait", **session, timeout=2)
        while state["state"] == "running":
            state = client.call("wait", **session, timeout=2)
        time.sleep(1.5)
        with pytest.raises(PeekyError) as stale:
            client.call("inspect", **session, stop_id=state["stop_id"])
        assert stale.value.error_code == "STALE_STOP" and "auto-resumed" in str(stale.value)
    finally:
        guard.set()
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cli_session_reports_pause_and_honors_max_pause(target, capsys):
    code, out = run(capsys, "session", "start", target / "loop.py", "--at", "7", "--max-pause", "1")
    assert code == 0 and "auto-resume in" in out.out
    time.sleep(1.6)
    code, out = run(capsys, "session", "inspect", "--json")
    assert code == 2 and json.loads(out.out)["error"]["code"] == "STALE_STOP"
    assert run(capsys, "session", "close")[0] == 0
    code, out = run(capsys, "session", "start", target / "loop.py", "--at", "7")
    assert code == 0 and "paused" in out.out and "auto-resume" not in out.out
    assert run(capsys, "session", "close")[0] == 0
    code, out = run(
        capsys, "session", "start", target / "loop.py", "--at", "7", "--max-pause", "500"
    )
    assert code == 2 and "--max-pause" in out.err


def test_background_probe_survives_short_waits_and_returns_the_same_result(target, capsys):
    code, out = run(
        capsys, "probe", target / "slow.py", "--at", "7", "--capture", "i", "--background", "--json"
    )
    job = json.loads(out.out)["job"]
    assert code == 0 and json.loads(out.out)["status"] == "running"
    code, out = run(capsys, "job", "wait", job, "--timeout", "0", "--json")
    assert code == 3 and json.loads(out.out)["status"] == "running"
    code, out = run(capsys, "job", "list", "--json")
    assert [j["job"] for j in json.loads(out.out)] == [job]
    code, out = run(capsys, "job", "wait", job, "--timeout", "60", "--json")
    result = json.loads(out.out)
    assert code == 0 and result["status"] == "completed" and "DONE" in result["output"]
    assert result["rows"][0]["value"] == "0"
    assert run(capsys, "job", "wait", job, "--timeout", "0")[0] == 0


def test_background_verify_preserves_exit_codes(target, capsys):
    root = Path(__file__).resolve().parents[2] / "examples"
    for name in ("pagination_bug.py", "verify_pagination.py"):
        shutil.copyfile(root / name, target / name)
    args = ["verify", target / "pagination_bug.py", "--check", target / "verify_pagination.py"]
    code, out = run(capsys, *args, "--old", "page * page_size", "--new", "0", "--background")
    job = out.out.split()[1]
    code, out = run(capsys, "job", "wait", job, "--timeout", "60")
    assert code == 1 and "passed: False" in out.out
    # `job wait --json` printed the text a job submitted without --json recorded.
    code, out = run(capsys, "job", "wait", job, "--json")
    assert code == 1 and json.loads(out.out)["passed"] is False
    code, out = run(capsys, *args, "--old", "no such text", "--new", "x", "--background")
    job = out.out.split()[1]
    code, out = run(capsys, "job", "wait", job, "--timeout", "60", "--json")
    assert code == 2 and json.loads(out.out)["error"]["code"] == "INVALID_REQUEST"
    code, out = run(capsys, "job", "wait", job)
    assert code == 2 and out.out == "" and "[INVALID_REQUEST]" in out.err


def test_a_background_verify_keeps_several_edits_read_from_files(target, capsys):
    root = Path(__file__).resolve().parents[2] / "examples"
    for name in ("pagination_bug.py", "verify_pagination.py"):
        shutil.copyfile(root / name, target / name)
    files = []
    for number, (old, new) in enumerate(
        [
            ("start = page * page_size", "start = (page - 1) * page_size"),
            ("end = start", "end = 0 + start"),
        ]
    ):
        for name, text in (("old", old), ("new", new)):
            (target / f"{name}{number}").write_text(text)
            files += [f"--{name}-file", target / f"{name}{number}"]
    args = ["verify", target / "pagination_bug.py", "--check", target / "verify_pagination.py"]
    code, out = run(capsys, *args, *files, "--background", "--json")
    job = json.loads(out.out)["job"]
    for name in ("old0", "new0", "old1", "new1"):
        (target / name).write_text("changed after submitting")
    code, out = run(capsys, "job", "wait", job, "--timeout", "60", "--json")
    result = json.loads(out.out)
    assert code == 0 and result["passed"] and "+    end = 0 + start" in result["diff"]


def test_unknown_and_lost_jobs_fail_with_codes(target, capsys):
    code, out = run(capsys, "job", "wait", "../../etc", "--json")
    assert code == 2 and json.loads(out.out)["error"]["code"] == "NOT_FOUND"
    jobs = target / "state" / "jobs"
    lost = jobs / ("a" * 16)
    lost.mkdir(parents=True)
    (lost / "job.json").write_text(
        json.dumps({"id": "a" * 16, "command": "probe", "argv": [], "cwd": "/", "started": 1})
    )
    (lost / "lock").write_text("")
    code, out = run(capsys, "job", "wait", "a" * 16, "--json")
    assert code == 2 and json.loads(out.out)["error"]["code"] == "JOB_LOST"


def test_failed_background_jobs_replay_their_error_on_stderr(target, capsys):
    root = Path(__file__).resolve().parents[2] / "examples"
    for name in ("pagination_bug.py", "verify_pagination.py"):
        shutil.copyfile(root / name, target / name)
    args = ["verify", target / "pagination_bug.py", "--check", target / "verify_pagination.py"]
    code, out = run(capsys, *args, "--old", "no such text", "--new", "x", "--background")
    job = out.out.split()[1]
    code, out = run(capsys, "job", "wait", job, "--timeout", "60")
    assert code == 2 and "[INVALID_REQUEST]" in out.err and "matched 0 times" in out.err
    files = target / "state" / "jobs" / job
    for name in ("lock", "started", "out", "log", "status.json", "job.json"):
        assert (files / name).stat().st_mode & 0o077 == 0, name


def test_background_jobs_work_with_a_relative_state_dir(target, capsys, monkeypatch):
    monkeypatch.chdir(target)
    monkeypatch.setenv("PEEKY_STATE_DIR", "relative-state")
    code, out = run(capsys, "probe", "loop.py", "--at", "7", "--capture", "i", "--background")
    job = out.out.split()[1]
    code, out = run(capsys, "job", "wait", job, "--timeout", "60")
    assert code == 0 and "status: completed" in out.out and "TOTAL 3" in out.out
    assert (target / "relative-state" / "jobs" / job / "status.json").is_file()


def test_a_job_not_yet_holding_its_lock_is_running_not_lost(target, capsys):
    jobs = target / "state" / "jobs"
    pending = jobs / ("b" * 16)
    pending.mkdir(parents=True)
    spec = {"id": "b" * 16, "command": "probe", "argv": [], "cwd": "/", "started": time.time()}
    (pending / "job.json").write_text(json.dumps(spec))
    (pending / "lock").write_text("")
    code, out = run(capsys, "job", "wait", "b" * 16, "--timeout", "0", "--json")
    assert code == 3 and json.loads(out.out)["status"] == "running"
    (pending / "started").write_text("")
    code, out = run(capsys, "job", "wait", "b" * 16, "--timeout", "0", "--json")
    assert code == 2 and json.loads(out.out)["error"]["code"] == "JOB_LOST"


def test_timed_out_probes_do_not_claim_to_have_ended_at_their_hits(target):
    result = run_probe(target, Probe("slow.py", 7, ("i",), hits=5, until="hits", timeout=3))
    assert result["ended"] in ("exit", None) and result["hits"] == 3


RAN_ON = """import sys
import time

print("BEFORE", flush=True)
total = 0
for i in range(3):
    total += i
print("AFTER", total, flush=True)
time.sleep(float(sys.argv[1]))
"""


@pytest.mark.parametrize("sleep", ["30", "0"])
def test_until_hits_says_the_output_can_follow_the_last_hit(target, sleep):
    # Resumed after its last hit, the program runs on until the probe ends it; its
    # output read meanwhile was "so far (the program was still running)", with
    # output_complete false, even when the program had exited.
    from peeky.probe import _table

    (target / "ran_on.py").write_text(RAN_ON)
    probe = Probe("ran_on.py", 7, ("i",), hits=2, until="hits", args=(sleep,))
    result = run_probe(target, probe)
    assert result["status"] == "completed" and result["ended"] == "hits"
    text = _table(result)
    assert "still running" not in text and result["output"].startswith("BEFORE\n")
    assert result.get("output_after_last_hit") is True
    if result["output_complete"]:
        # It exited before Peeky disconnected, and the adapter then ended its stream: all
        # of its output, and its exit.
        assert sleep == "0" and result["exit_code"] == 0 and result["output"] == "BEFORE\nAFTER 3\n"
        assert not result["output_truncated"] and "program_ended" not in result
        assert "output (all of it: the program ran on after the last hit, to its exit):" in text
    elif result.get("program_ended"):
        # It ended, but the adapter kept its stream open: more output may have been coming.
        assert sleep == "0" and "after the last hit and ended, but its output may still" in text
    else:
        # Peeky ended it: no exit of its own to report.
        assert "exit_code" not in result
        assert (
            "output so far (the program ran on after the last hit, so this can include text "
            "written after it):\n  BEFORE"
        ) in text


def test_continue_to_exit_never_clears_breakpoints_for_a_stale_stop(target):
    worker = DebugWorker(target)
    try:
        session = launch(worker, 0.3)
        state = wait_stopped(worker, session)
        time.sleep(0.5)
        worker.guard_pause()
        with pytest.raises(ValueError, match="auto-resumed"):
            worker.dispatch("continue", {**session, "stop_id": state["stop_id"], "to_exit": True})
        # Breakpoints survived: the loop stops again at the same line.
        again = wait_stopped(worker, session)
        assert again["state"] == "stopped" and again["stop_id"] != state["stop_id"]
        with pytest.raises(ValueError, match="to_exit"):
            worker.dispatch("continue", {**session, "stop_id": again["stop_id"], "to_exit": 1})
        worker.dispatch("continue", {**session, "stop_id": again["stop_id"], "to_exit": True})
        assert wait_stopped(worker, session)["state"] == "terminated"
    finally:
        worker.close()


def test_background_verify_fixes_edit_files_at_submission(target, capsys):
    root = Path(__file__).resolve().parents[2] / "examples"
    for name in ("pagination_bug.py", "verify_pagination.py"):
        shutil.copyfile(root / name, target / name)
    (target / "old.txt").write_text("page * page_size")
    (target / "new.txt").write_text("(page - 1) * page_size")
    args = ["verify", target / "pagination_bug.py", "--check", target / "verify_pagination.py"]
    code, out = run(
        capsys,
        *args,
        "--old-file",
        target / "old.txt",
        "--new-file",
        target / "new.txt",
        "--background",
        "--json",
    )
    job = json.loads(out.out)["job"]
    (target / "new.txt").write_text("0")
    code, out = run(capsys, "job", "wait", job, "--timeout", "60")
    result = json.loads(out.out)
    assert code == 0 and result["passed"] and "(page - 1) * page_size" in result["diff"]


def test_verify_takes_a_timeout_and_reports_its_duration(target, capsys):
    # Verify had no --timeout or --quiet, and no duration.
    root = Path(__file__).resolve().parents[2] / "examples"
    for name in ("pagination_bug.py", "verify_pagination.py"):
        shutil.copyfile(root / name, target / name)
    args = ["verify", target / "pagination_bug.py", "--check", target / "verify_pagination.py"]
    edit = ["--old", "start = page * page_size", "--new", "start = (page - 1) * page_size"]
    code, out = run(capsys, *args, *edit, "--timeout", "60", "--quiet", "--json")
    result = json.loads(out.out)
    assert code == 0 and result["passed"] and 0 < result["seconds"] < 60
    code, out = run(capsys, *args, *edit)
    assert code == 0 and out.out.startswith("passed: True  stage: verification  seconds: ")
    code, out = run(capsys, *args, *edit, "--timeout", "0.000001", "--json")
    assert code == 2 and json.loads(out.out)["error"]["code"] == "TIMEOUT"
    code, out = run(capsys, *args, *edit, "--timeout", "601")
    assert code == 2 and "--timeout must be within" in out.err


def test_a_background_verify_with_its_target_after_a_double_dash(target, capsys):
    # The job's added --json landed after `--`, a second target.
    root = Path(__file__).resolve().parents[2] / "examples"
    for name in ("pagination_bug.py", "verify_pagination.py"):
        shutil.copyfile(root / name, target / name)
    args = ["verify", "--check", target / "verify_pagination.py"]
    edit = ["--old", "start = page * page_size", "--new", "start = (page - 1) * page_size"]
    code, out = run(capsys, *args, *edit, "--background", "--", target / "pagination_bug.py")
    job = out.out.split()[1]
    code, out = run(capsys, "job", "wait", job, "--timeout", "60")
    assert code == 0 and out.out.startswith("passed: True"), out


def test_a_timeout_during_the_checker_is_timeout_not_a_failed_fix(target, capsys):
    # A correct fix whose checker the deadline cut short read as failed.
    root = Path(__file__).resolve().parents[2] / "examples"
    shutil.copyfile(root / "pagination_bug.py", target / "pagination_bug.py")
    checker = (root / "verify_pagination.py").read_text()
    (target / "slow_check.py").write_text("import time\ntime.sleep(5)\n" + checker)
    args = ["verify", target / "pagination_bug.py", "--check", target / "slow_check.py"]
    edit = ["--old", "start = page * page_size", "--new", "start = (page - 1) * page_size"]
    code, out = run(capsys, *args, *edit, "--timeout", "1", "--json")
    assert code == 2 and json.loads(out.out)["error"]["code"] == "TIMEOUT", out.out
