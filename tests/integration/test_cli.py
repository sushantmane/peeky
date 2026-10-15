"""The standalone `peeky` CLI against real debugpy: no model, no mode switch."""

import json
import shutil
from pathlib import Path

import pytest

from peeky.cli import main

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    for name in ("pagination_bug.py", "verify_pagination.py"):
        shutil.copyfile(ROOT / "examples" / name, tmp_path / name)
    return tmp_path


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr()


def test_describe_lists_breakpoint_lines_and_commands(workspace, capsys):
    code, out = run(capsys, "describe", workspace / "pagination_bug.py", "--json")
    manifest = json.loads(out.out)
    assert code == 0 and manifest["language"] == "python"
    assert 7 in manifest["breakpoint_lines"]["pagination_bug.py"]
    assert {"probe", "session", "verify"} <= set(manifest["commands"])


def test_probe_detects_python_and_needs_no_mode(workspace, capsys):
    code, out = run(
        capsys,
        "probe",
        workspace / "pagination_bug.py",
        "--at",
        "8",
        "--capture",
        "start",
        "--set",
        "start=0",
    )
    assert code == 0 and "set start: 3 -> 0" in out.out and "PAGE: [10, 20, 30]" in out.out


def test_session_commands_share_state_across_processes(workspace, capsys):
    program = workspace / "pagination_bug.py"
    assert run(capsys, "session", "start", program, "--at", "8")[0] == 0
    code, out = run(capsys, "session", "get", "start", "items[1]", "--json")
    rows = {row["selector"]: row for row in json.loads(out.out)}
    assert code == 0 and rows["start"]["value"] == "3" and rows["items[1]"]["value"] == "20"
    assert "start: int = 3" in run(capsys, "session", "inspect")[1].out
    assert "3 -> 0" in run(capsys, "session", "set", "start=0")[1].out
    assert "terminated" in run(capsys, "session", "continue")[1].out
    assert "PAGE: [10, 20, 30]" in run(capsys, "session", "report")[1].out
    assert run(capsys, "session", "list")[1].out.strip() == "default"
    assert run(capsys, "session", "close")[0] == 0
    assert run(capsys, "session", "list")[1].out.strip() == "no sessions"
    assert not list((workspace / "state" / "sessions").iterdir())
    code, out = run(capsys, "session", "inspect")
    assert code == 2 and "No session" in out.err


def test_verify_runs_the_independent_checker_on_a_candidate(workspace, capsys):
    args = [
        "verify",
        workspace / "pagination_bug.py",
        "--check",
        workspace / "verify_pagination.py",
    ]
    code, out = run(capsys, *args, "--old", "page * page_size", "--new", "(page - 1) * page_size")
    assert code == 0 and "passed: True" in out.out
    code, out = run(capsys, *args, "--old", "page * page_size", "--new", "0")
    assert code == 1 and "passed: False" in out.out
    assert "page * page_size" in (workspace / "pagination_bug.py").read_text()


def test_unknown_targets_and_bad_locations_fail_clearly(workspace, capsys):
    (workspace / "notes.txt").write_text("x")
    code, out = run(capsys, "probe", workspace / "notes.txt", "--at", "1")
    assert code == 2 and "Cannot detect" in out.err
    code, out = run(capsys, "probe", workspace / "pagination_bug.py", "--at", "x:y")
    assert code == 2 and "--at" in out.err
    code, out = run(capsys, "probe", workspace / "pagination_bug.py", "--at", "pagination_bug.py")
    assert code == 2 and "requires --at" in out.err


def test_session_names_cannot_escape_the_state_directory(workspace, capsys):
    for name in ("../escape", "a/b", "", "x" * 40):
        code, out = run(capsys, "session", "inspect", "--name", name)
        assert code == 2 and "Session names" in out.err


def stale_record(workspace, **changes):
    import os

    state = workspace / "state" / "sessions"
    state.mkdir(parents=True, exist_ok=True)
    record = {
        "name": "stale",
        "url": "http://127.0.0.1:9",
        "token": "t" * 32,
        "session_id": "a" * 32,
        "pid": os.getpid(),
        **changes,
    }
    path = state / "stale.json"
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    return path


def test_close_uses_authenticated_shutdown_and_never_signals_pids(workspace, capsys, monkeypatch):
    path = stale_record(workspace)
    signalled = []
    monkeypatch.setattr("os.killpg", lambda *args: signalled.append(args))
    assert run(capsys, "session", "close", "--name", "stale")[0] == 2
    assert signalled == [] and not path.exists()


@pytest.mark.parametrize(
    "changes, mode",
    [({"url": "http://10.0.0.5:8765"}, 0o600), ({"session_id": "../x"}, 0o600), ({}, 0o644)],
)
def test_malformed_or_shared_session_records_are_refused(workspace, capsys, changes, mode):
    path = stale_record(workspace, **changes)
    path.chmod(mode)
    code, out = run(capsys, "session", "inspect", "--name", "stale")
    assert code == 2 and ("malformed" in out.err or "private" in out.err)


def test_verify_cannot_write_outside_a_scratch_copy(workspace, capsys):
    source = workspace / "pagination_bug.py"
    original = source.read_text()
    check = workspace / "verify_pagination.py"
    for at in (str(source), "../pagination_bug.py"):
        code, out = run(
            capsys,
            "verify",
            workspace,
            "--at",
            at,
            "--check",
            check,
            "--old",
            "page * page_size",
            "--new",
            "(page - 1) * page_size",
        )
        assert source.read_text() == original
        if at.startswith(".."):
            assert code == 2 and "inside the target" in out.err
        else:
            assert code == 0 and "/candidate/" in out.out


def test_probe_that_never_stops_is_not_success(workspace, capsys):
    code, out = run(capsys, "probe", workspace / "pagination_bug.py", "--at", "6", "--capture", "x")
    assert code == 1 and "status: no-stop" in out.out and "finished without stopping" in out.out


def test_explicit_file_types_win_over_cargo_ancestors(workspace, capsys):
    (workspace / "Cargo.toml").write_text('[package]\nname = "x"\n')
    code, out = run(capsys, "describe", workspace / "pagination_bug.py", "--json")
    assert code == 0 and json.loads(out.out)["language"] == "python"


def test_session_timeouts_are_finite(workspace, capsys):
    stale_record(workspace)
    code, out = run(capsys, "session", "wait", "--name", "stale", "--timeout", "inf")
    assert code == 2 and "--timeout" in out.err


def test_probe_reports_progress_on_stderr_only(workspace, capsys):
    code, out = run(
        capsys,
        "probe",
        workspace / "pagination_bug.py",
        "--at",
        "8",
        "--capture",
        "start",
        "--json",
    )
    assert code == 0 and json.loads(out.out)["status"] == "completed"
    assert "peeky: hit 1/1" in out.err and "program finished" in out.err
    code, out = run(capsys, "probe", workspace / "pagination_bug.py", "--at", "8", "--quiet")
    assert code == 0 and out.err == ""
