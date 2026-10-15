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
