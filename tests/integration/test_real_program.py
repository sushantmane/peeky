"""Issue #7: a non-synthetic program (CPython 3.9.6 ipaddress) with a real, upstream-fixed
bug (CVE-2024-4032), debugged through a separate entry script with `--run`."""

import json
import shutil
import threading
from pathlib import Path

import pytest

from peeky.agent import verify
from peeky.cli import main
from peeky.client import Client
from peeky.worker import make_server

CASE = Path(__file__).resolve().parents[2] / "examples/real/ipaddress_mapped"


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "case"
    shutil.copytree(CASE, root)
    return root, json.loads((root / "case.json").read_text())


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr()


def candidate(root, edit, tmp_path):
    text = (root / "ipaddress39.py").read_text()
    assert text.count(edit["old"]) == 1
    path = tmp_path / "candidate" / "ipaddress39.py"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text.replace(edit["old"], edit["new"], 1))
    return path


def test_checker_rejects_original_partial_and_overfit_and_accepts_the_fix(case, tmp_path):
    root, data = case
    check = root / "check.py"
    assert verify(check, root / "ipaddress39.py", root)["passed"] is False
    for name, passes in (("fix", True), ("partial", False), ("overfit", False)):
        result = verify(check, candidate(root, data[name], tmp_path), root)
        assert result["passed"] is passes, (name, result["stdout"], result["stderr"])


def test_probe_breaks_in_the_library_while_running_the_entry_script(case, capsys):
    root, data = case
    at = f"{data['program']}:{data['observe_line']}"
    code, out = run(
        capsys,
        "probe",
        root,
        "--at",
        at,
        "--run",
        data["driver"],
        "--capture",
        "self",
        "--hits",
        "100",
        "--json",
    )
    result = json.loads(out.out)
    assert code == 0 and result["status"] == "completed", result.get("error")
    # Line events per call differ across Python versions (3.11 also stops in the
    # generator expression on this line), so look at every hit, not the first few.
    seen = {row["value"] for row in result["rows"]}
    assert {"IPv6Address('::ffff:a00:1')", "IPv6Address('::ffff:808:808')"} <= seen
    assert "::ffff:8.8.8.8: private" in result["output"]


def test_verify_accepts_the_multi_line_upstream_fix_and_reports_its_diff(case, capsys):
    root, data = case
    args = ["verify", root, "--at", data["program"], "--check", root / "check.py"]
    code, out = run(
        capsys, *args, "--old", data["fix"]["old"], "--new", data["fix"]["new"], "--json"
    )
    result = json.loads(out.out)
    assert (
        code == 0
        and result["passed"]
        and "+        ipv4_mapped = self.ipv4_mapped" in result["diff"]
    )
    old = "        return any(self in net for net in self._constants._private_networks)"
    code, out = run(capsys, *args, "--old", old, "--new", "        return False")
    assert code == 2 and "matched 2 times (lines 1345, 2018)" in out.err


def test_run_is_confined_and_python_only(case, capsys, tmp_path):
    root, data = case
    at = f"{data['program']}:{data['observe_line']}"
    code, out = run(capsys, "probe", root, "--at", at, "--run", "../outside.py")
    assert code == 2 and "inside the target" in out.err
    from peeky.cli import _entry

    with pytest.raises(ValueError, match="only for Python"):
        _entry(root, "main.c", "c")


def test_http_probe_and_worker_accept_an_entry(case):
    root, data = case
    server = make_server(root, "real-program-token-123456", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_port}", "real-program-token-123456")
    try:
        result = client.call(
            "probe",
            at=f"{data['program']}:{data['observe_line']}",
            capture=["self"],
            entry=data["driver"],
        )
        assert result["status"] == "completed" and "::ffff:8.8.8.8: private" in result["output"]
        launched = client.call(
            "launch",
            program=data["program"],
            breakpoints=[{"line": data["observe_line"]}],
            entry=data["driver"],
        )
        assert launched["entry_sha256"] != launched["source_sha256"]
        client.call("close", session_id=launched["session_id"])
    finally:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_program_is_byte_for_byte_cpython_3_9_6():
    import hashlib

    # Below the notice of its origin and licence, the file is upstream's.
    notice, upstream = (CASE / "ipaddress39.py").read_bytes().split(b"\n\n", 1)
    assert all(line.startswith(b"#") for line in notice.splitlines()), notice
    assert b"PSF License Agreement" in notice and b"LICENSE-PSF" in notice, notice
    digest = hashlib.sha256(upstream).hexdigest()
    assert digest == "a7a7a939af9f74613b4f278f81d3d5e1f55ec6c573825c70d902ae3c0bdd3469"
    assert digest in (CASE / "README.md").read_text()
    licence = (CASE / "LICENSE-PSF").read_text()
    assert "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2" in licence


def test_agent_feedback_args_function_names_and_partial_output(case, capsys):
    root, data = case
    at = f"{data['program']}:{data['observe_line']}"
    code, out = run(
        capsys,
        "probe",
        root,
        "--at",
        at,
        "--run",
        data["driver"],
        "--arg",
        "::ffff:8.8.8.8",
        "--capture",
        "self",
        "--hits",
        "3",
        "--json",
    )
    result = json.loads(out.out)
    assert code == 0 and result["output"] == "::ffff:8.8.8.8: private\n"
    assert {row["value"] for row in result["rows"]} == {"IPv6Address('::ffff:808:808')"}
    assert {row["function"] for row in result["rows"]} >= {"is_private"}
    assert result["output_complete"] is True and result["by_hit"][0]["function"]
    code, out = run(capsys, "probe", root, "--at", at, "--run", data["driver"], "--until", "hits")
    # Resumed after its last hit, the program ran on: never "still running" (#203).
    assert code == 0 and "the program ran on after the last hit" in out.out
    assert "still running" not in out.out


def test_directory_targets_explain_what_to_pass(case, capsys):
    root, _ = case
    code, out = run(capsys, "describe", root)
    assert code == 2 and "pass --at <module.py>" in out.err and "ipaddress39.py" in out.err
    code, out = run(capsys, "describe", root, "--at", "ipaddress39.py", "--json")
    manifest = json.loads(out.out)
    assert "--run <script.py>" in manifest["commands"]["probe"]
    assert "--at ipaddress39.py" in manifest["commands"]["verify"]
    scope = next(s for s in manifest["scopes"] if s["scope"] == "IPv6Address.is_private")
    assert scope["lines"][0] <= 2018 <= scope["lines"][1]


def test_verify_reads_edits_verbatim_from_files(case, capsys, tmp_path):
    root, data = case
    (tmp_path / "old.txt").write_text(data["fix"]["old"])
    (tmp_path / "new.txt").write_text(data["fix"]["new"])
    args = ["verify", root, "--at", data["program"], "--check", root / "check.py"]
    code, out = run(
        capsys, *args, "--old-file", tmp_path / "old.txt", "--new-file", tmp_path / "new.txt"
    )
    assert code == 0 and "passed: True" in out.out
    code, out = run(capsys, *args, "--old", "x", "--old-file", tmp_path / "old.txt", "--new", "y")
    assert code == 2 and "either --old or --old-file, not both" in out.err


def test_session_continue_to_exit_runs_the_program_to_completion(case, capsys):
    root, data = case
    at = f"{data['program']}:{data['observe_line']}"
    assert run(capsys, "session", "start", root, "--at", at, "--run", data["driver"])[0] == 0
    code, out = run(capsys, "session", "continue", "--to-exit", "--json")
    assert code == 0 and json.loads(out.out)["state"] == "terminated"
    code, out = run(capsys, "session", "report", "--json")
    assert "::ffff:8.8.8.8: private" in json.loads(out.out)["output"]
    assert run(capsys, "session", "close")[0] == 0
