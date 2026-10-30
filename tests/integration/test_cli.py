"""The standalone `peeky` CLI against real debugpy: no model, no mode switch."""

import json
import os
import shutil
import subprocess
import sys
import time
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
    get_page = next(scope for scope in manifest["scopes"] if scope["scope"] == "get_page")
    assert get_page["names"] == ["end", "items", "page", "page_size", "start"]
    assert get_page["lines"][0] == 4
    assert 7 in manifest["breakpoint_lines"]["pagination_bug.py"]
    assert {"probe", "session", "verify"} <= set(manifest["commands"])
    # The file target is the file to edit: verify needs no --at.
    assert manifest["commands"]["verify"].startswith(
        f"peeky verify {workspace / 'pagination_bug.py'} --check "
    )


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
    report = run(capsys, "session", "report")[1].out
    # No unexplained count of the worker's requests.
    assert "PAGE: [10, 20, 30]" in report and "operations" not in report
    assert run(capsys, "session", "list")[1].out.strip() == "default"
    assert run(capsys, "session", "close")[0] == 0
    assert run(capsys, "session", "list")[1].out.strip() == "no sessions"
    left = [p.name for p in (workspace / "state" / "sessions").iterdir()]
    # The reservation lock is shared by every start; nothing of the session remains.
    assert sorted(name for name in left if name != ".reserve.lock") == ["default.last.log"]
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


def test_verify_takes_several_edits_each_quoting_the_original(workspace, capsys):
    # A general fix often touches two places; each pair is checked against the
    # original file, and the candidate gets every edit.
    args = [
        "verify",
        workspace / "pagination_bug.py",
        "--check",
        workspace / "verify_pagination.py",
    ]
    two = [
        "--old",
        "start = page * page_size",
        "--new",
        "start = (page - 1) * page_size",
        "--old",
        "end = start + page_size",
        "--new",
        "end = start + page_size  # exclusive",
    ]
    code, out = run(capsys, *args, *two, "--json")
    result = json.loads(out.out)
    assert code == 0 and result["passed"], result
    assert "+    start = (page - 1) * page_size" in result["diff"]
    assert "+    end = start + page_size  # exclusive" in result["diff"]
    assert "page * page_size" in (workspace / "pagination_bug.py").read_text()
    code, out = run(capsys, *args, "--old", "start = page", "--new", "x", "--old", "y")
    assert code == 2 and "got 2 --old and 1 --new" in out.err
    code, out = run(
        capsys, *args, "--old", "start = page", "--new", "x", "--old", "page * page", "--new", "y"
    )
    assert code == 2 and "overlap" in out.err
    code, out = run(capsys, *args, "--old", "start", "--new", "x", "--old", "zz", "--new", "y")
    assert code == 2 and "(edit 1)" in out.err and "matched" in out.err
    old_files = []
    for number, (old, new) in enumerate(zip(two[1::4], two[3::4])):
        for name, text in (("old", old), ("new", new)):
            path = workspace / f"{name}{number}.txt"
            path.write_text(text)
            old_files += [f"--{name}-file", path]
    code, out = run(capsys, *args, *old_files, "--json")
    assert code == 0 and json.loads(out.out)["passed"]


def _peeky_verify_dirs():
    import tempfile

    return set(Path(tempfile.gettempdir()).glob("peeky-verify-*"))


def test_verify_reports_stage_reason_diff_and_cleans_up(workspace, capsys):
    before = _peeky_verify_dirs()
    args = [
        "verify",
        workspace / "pagination_bug.py",
        "--check",
        workspace / "verify_pagination.py",
    ]
    code, out = run(capsys, *args, "--old", "page * page_size", "--new", "0", "--json")
    result = json.loads(out.out)
    assert code == 1 and result["stage"] == "verification" and not result["passed"]
    assert "candidate" not in result and "+    start = 0" in result["diff"]
    code, out = run(capsys, *args, "--old", "page * page_size", "--new", "0")
    assert code == 1 and "reason: " in out.out and "diff:" in out.out
    assert _peeky_verify_dirs() == before
    code, out = run(
        capsys, *args, "--old", "page * page_size", "--new", "(page-1)*page_size", "--keep"
    )
    kept = _peeky_verify_dirs() - before
    assert code == 0 and len(kept) == 1 and "candidate: " in out.out
    shutil.rmtree(kept.pop())


def test_verify_explains_why_old_text_does_not_match(workspace, capsys):
    args = [
        "verify",
        workspace / "pagination_bug.py",
        "--check",
        workspace / "verify_pagination.py",
    ]
    code, out = run(capsys, *args, "--old", "page_size", "--new", "n")
    assert code == 2 and "matched 5 times (lines " in out.err
    code, out = run(capsys, *args, "--old", "start = page * page_size\\n", "--new", "x")
    assert code == 2 and "literal backslash-n" in out.err
    code, out = run(capsys, *args, "--old", "nothing like this", "--new", "x")
    assert code == 2 and "matched 0 times" in out.err
    # Which condition failed: an edit that changes nothing.
    code, out = run(capsys, *args, "--old", "page * page_size", "--new", "page * page_size")
    assert code == 2 and "--new equals --old" in out.err and "matched" not in out.err
    assert "--baseline" in out.err
    code, out = run(capsys, *args, "--old", "page_size", "--new", "page_size")
    assert code == 2 and "--old matched 5 times" in out.err and "--new equals --old" in out.err


def test_verify_baseline_checks_the_unedited_program(workspace, capsys):
    # Whether the checker fails on the program as it is.
    args = [
        "verify",
        workspace / "pagination_bug.py",
        "--check",
        workspace / "verify_pagination.py",
    ]
    code, out = run(capsys, *args, "--baseline")
    assert code == 1 and "passed: False  stage: verification" in out.out
    assert "baseline: the unedited program, no edit (the checker rejects it" in out.out
    assert "diff:" not in out.out and "checker exit: 1" in out.out
    code, out = run(capsys, *args, "--baseline", "--json")
    result = json.loads(out.out)
    assert code == 1 and result["baseline"] is True and result["passed"] is False
    assert result["diff"] == "" and result["exit_code"] == 1
    code, out = run(capsys, *args, "--baseline", "--old", "page", "--new", "x")
    assert code == 2 and "leave out --old/--new" in out.err
    code, out = run(capsys, *args)
    assert code == 2 and "or --baseline" in out.err
    # A checker that crashes rejects nothing; only the failure is reported.
    broken = workspace / "broken_check.py"
    broken.write_text("import no_such_module_anywhere\n")
    code, out = run(capsys, *args[:2], "--check", broken, "--baseline")
    assert code == 1 and "ModuleNotFoundError" in out.out, out.out
    assert "baseline: the unedited program, no edit\n" in out.out + "\n", out.out
    assert "rejects it" not in out.out and "fails on it" not in out.out
    with pytest.raises(SystemExit):
        run(capsys, "verify", "--help")
    assert "(--old OLD --new NEW [--old OLD --new NEW ...] | --baseline)" in capsys.readouterr().out


def test_session_continue_reports_where_it_stopped(workspace, capsys):
    target = workspace / "pagination_bug.py"
    code, out = run(capsys, "session", "start", target, "--at", "8")
    assert code == 0 and "pagination_bug.py:8" in out.out
    code, out = run(capsys, "session", "continue", "--json")
    assert code == 0 and json.loads(out.out)["state"] == "terminated"
    assert run(capsys, "session", "close")[0] == 0


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
            assert code == 0 and "-    start = page * page_size" in out.out


def test_probe_that_never_stops_is_not_success(workspace, capsys):
    code, out = run(capsys, "probe", workspace / "pagination_bug.py", "--at", "6", "--capture", "x")
    assert code == 1 and "status: no-stop" in out.out and "finished without stopping" in out.out


def test_a_program_that_fails_before_the_line_says_so_and_why(workspace, capsys):
    program = workspace / "parse.py"
    program.write_text("import sys\nx = float(sys.argv[1])\nprint(x)\n")
    code, out = run(
        capsys, "probe", program, "--at", "3", "--capture", "x", "--json", "--", "--old", "1"
    )
    result = json.loads(out.out)
    assert code == 1 and result["status"] == "no-stop" and result["exit_code"] == 1
    assert "failed (exit code 1) without stopping" in result["error"]["message"]
    # The hint names the real cause: Peeky's own option went to the program.
    assert "including --old, which Peeky also takes" in result["error"]["hint"]
    assert "could not convert string to float: '--old'" in result["output"]
    code, out = run(capsys, "probe", program, "--at", "3", "--capture", "x", "--", "2.5")
    assert code == 0 and "exit code" not in out.out and "warning" not in out.out


def test_options_after_the_separator_are_flagged_even_when_the_probe_succeeds(workspace, capsys):
    program = workspace / "echo.py"
    program.write_text("import sys\nargs = sys.argv[1:]\nprint(args)\n")
    code, out = run(capsys, "probe", program, "--at", "3", "--capture", "args", "--", "--hits=2")
    assert code == 0 and "including --hits, which Peeky also takes" in out.out


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


def test_cli_errors_are_machine_readable_with_json(workspace, capsys):
    code, out = run(capsys, "session", "inspect", "--name", "missing", "--json")
    payload = json.loads(out.out)["error"]
    assert code == 2 and payload["code"] == "NO_SESSION" and payload["hint"]
    assert "[NO_SESSION]" in out.err and "hint:" in out.err


def test_verify_refuses_when_the_source_changes_before_the_candidate_is_built(
    workspace, capsys, monkeypatch
):
    import peeky.cli as cli

    real = cli._check_edit
    source = workspace / "pagination_bug.py"

    def racing(text, old, new, *which):
        real(text, old, new, *which)
        source.write_text(source.read_text().replace("end = start + page_size", "end = start"))

    monkeypatch.setattr(cli, "_check_edit", racing)
    code, out = run(
        capsys,
        "verify",
        source,
        "--check",
        workspace / "verify_pagination.py",
        "--old",
        "page * page_size",
        "--new",
        "(page - 1) * page_size",
        "--json",
    )
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "PROVENANCE_CHANGED"
    assert "does not match the reported edit" in error["message"]


def test_unexpected_worker_error_bodies_render_cleanly(workspace, capsys, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from peeky.client import Client

    class Broken(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b"<html>bad gateway</html>"
            self.send_response(502)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Broken)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", "t" * 32)
        with pytest.raises(OSError) as caught:
            client.call("version")
        payload = caught.value.payload
        assert {"code", "message", "hint", "retryable", "session_closed"} <= set(payload)
        import peeky.cli as cli

        def boom(args):
            raise caught.value

        monkeypatch.setattr(cli, "describe", boom)
        code, out = run(capsys, "describe", workspace / "pagination_bug.py")
        assert code == 2 and "[INVALID_REQUEST]" in out.err and "hint:" in out.err
    finally:
        server.shutdown()
        server.server_close()


def test_multi_source_targets_describe_all_sources_and_never_traceback(
    workspace, capsys, monkeypatch
):
    config = workspace / "attach.json"
    sources = ["a/One.java", "b/Two.java"]
    config.write_text(json.dumps({"bundle": "x", "port": 5005, "sources": sources}))
    args = ["--config", config, "--check", workspace / "verify_pagination.py"]
    code, out = run(capsys, "verify", workspace, *args, "--old", "a", "--new", "b")
    assert code == 2 and "Attached JVMs cannot verify" in out.err
    config.write_text(json.dumps({"main_class": "demo.Main", "sources": sources}))
    code, out = run(capsys, "verify", workspace, *args, "--old", "a", "--new", "b", "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and "needs --at <source> (one of: a/One.java, b/Two.java)" in error["message"]
    code, out = run(capsys, "describe", workspace, "--config", workspace / "missing.json")
    assert code == 2 and "[INVALID_REQUEST]" in out.err and "A path does not exist" in out.err

    import peeky.cli as cli

    def broken(args):
        raise TypeError("internal bug")

    monkeypatch.setattr(cli, "probe", broken)
    code, out = run(capsys, "probe", workspace / "pagination_bug.py", "--at", "7", "--json")
    assert code == 2 and json.loads(out.out)["error"]["message"] == "internal bug"


def test_probes_save_evidence_that_sql_and_canned_queries_read(workspace, capsys):
    program = workspace / "pagination_bug.py"
    probe = ["probe", program, "--at", "8", "--capture", "start,items[1]", "--quiet"]
    code, out = run(capsys, *probe, "--label", "baseline", "--json")
    saved = json.loads(out.out)["evidence"]
    assert code == 0 and saved["run_id"] == 1 and saved["next"].startswith("peeky queries run")
    code, out = run(capsys, *probe, "--set", "start=0")
    assert code == 0 and "evidence: saved as run 2" in out.out
    code, out = run(
        capsys,
        "sql",
        "SELECT run_id, label, selector, value, num FROM evidence WHERE phase = 'observed' "
        "AND selector = :name ORDER BY run_id",
        "--param",
        "name=start",
        "--json",
    )
    rows = json.loads(out.out)["rows"]
    assert code == 0 and rows == [
        {"run_id": 1, "label": "baseline", "selector": "start", "value": "3", "num": 3.0},
        {"run_id": 2, "label": None, "selector": "start", "value": "3", "num": 3.0},
    ]
    code, out = run(capsys, "queries", "outputs", "--json")
    outputs = [row["output"] for row in json.loads(out.out)["rows"]]
    assert code == 0 and outputs == ["PAGE: [40, 50, 60]\n", "PAGE: [10, 20, 30]\n"]
    code, out = run(capsys, "queries", "set_effect")
    assert code == 0 and "start: 3 -> 0" in out.out
    code, out = run(capsys, "sql", "DELETE FROM runs", "--json")
    assert code == 2 and json.loads(out.out)["error"]["code"] == "INVALID_QUERY"
    code, out = run(capsys, "sql", "--schema", "--json")
    assert code == 0 and "captures" in json.loads(out.out)["tables"]
    assert "diff" in run(capsys, "queries")[1].out


def test_unsaved_and_unsaveable_probes_still_answer(workspace, capsys):
    program = workspace / "pagination_bug.py"
    probe = ["probe", program, "--at", "8", "--capture", "start", "--quiet", "--json"]
    code, out = run(capsys, *probe, "--no-save")
    assert code == 0 and "evidence" not in json.loads(out.out)
    assert not (workspace / "state" / "evidence").exists()
    broken = workspace / "broken.db"
    broken.write_text("not a database")
    code, out = run(capsys, *probe, "--store", broken)
    result = json.loads(out.out)
    assert code == 0 and result["rows"][0]["value"] == "3" and "evidence" not in result
    assert any(w.startswith("Evidence was not saved") for w in result["warnings"])
    assert broken.read_text() == "not a database"
    code, out = run(capsys, "probe", program, "--at", "8", "--label", "x" * 201)
    assert code == 2 and "--label" in out.err


def test_background_probes_save_evidence_too(workspace, capsys):
    program = workspace / "pagination_bug.py"
    code, out = run(capsys, "probe", program, "--at", "8", "--capture", "page", "--background")
    job = out.out.split()[1]
    for _ in range(60):
        code, out = run(capsys, "job", "wait", job, "--timeout", "5")
        if code != 3:
            break
    assert code == 0 and "evidence: saved as run 1" in out.out
    code, out = run(capsys, "queries", "run", "--param", "run=1", "--json")
    assert json.loads(out.out)["rows"][0]["value"] == "1"


def test_describe_points_agents_at_the_evidence_store(workspace, capsys):
    code, out = run(capsys, "describe", workspace / "pagination_bug.py", "--json")
    assert code == 0 and "peeky sql" in json.loads(out.out)["commands"]["evidence"]


def test_auto_detected_c_targets_link_one_program(tmp_path):
    from peeky.cli import _native_sources

    program = "#include <stdio.h>\nint helper(void);\nint\nmain(void) { return helper(); }\n"
    (tmp_path / "one.c").write_text(program)
    (tmp_path / "two.c").write_text("int main(int argc, char **argv) { return 0; }\n")
    (tmp_path / "util.c").write_text("/* not main( */\nint helper(void) { return 0; }\n")
    (tmp_path / "notes.txt").write_text("int main(")
    assert _native_sources(tmp_path, "one.c", (".c",)) == ("one.c", "util.c")
    assert _native_sources(tmp_path, "two.c", (".c",)) == ("two.c", "util.c")
    with pytest.raises(ValueError, match="several programs .*one.c, two.c"):
        _native_sources(tmp_path, "util.c", (".c",))
    (tmp_path / "two.c").unlink()
    # One program split over several files still builds them all.
    assert _native_sources(tmp_path, "util.c", (".c",)) == ("one.c", "util.c")


ORDERS = """def total(order):
    amount = order["price"] * order["qty"]
    return amount


print(total({"id": 18, "price": 14, "qty": 1, "it's": 2}))
"""


def test_mapping_keys_are_selectors_in_probes_and_sessions(workspace, capsys):
    (workspace / "orders.py").write_text(ORDERS)
    program = workspace / "orders.py"
    selectors = "order['id'],order[\"it's\"],order.id,amount"
    code, out = run(
        capsys, "probe", program, "--at", "3", "--capture", selectors, "--json", "--no-save"
    )
    rows = {row["selector"]: row for row in json.loads(out.out)["rows"]}
    assert code == 0 and rows["order['id']"]["value"] == "18"
    assert rows['order["it\'s"]']["value"] == "2" and rows["amount"]["value"] == "14"
    assert "select it with ['id'], not .id" in rows["order.id"]["error"]
    assert "'price'" in rows["order.id"]["error"] and "len()" not in rows["order.id"]["error"]

    assert run(capsys, "session", "start", program, "--at", "3")[0] == 0
    assert "(children: peeky session get order)" in run(capsys, "session", "inspect")[1].out
    code, out = run(capsys, "session", "get", "order", "amount")
    assert code == 0
    assert "  order['price']: int = 14" in out.out and '  order["it\'s"]: int = 2' in out.out
    assert "len() [not selectable]: int = 4" in out.out
    code, out = run(capsys, "session", "get", "order", "--json")
    [row] = json.loads(out.out)
    assert "hit" not in row and row["phase"] == "observed"
    assert {"name": "'qty'", "type": "int", "value": "1", "selector": "order['qty']"} in row[
        "children"
    ]
    # Every row is printed, and the exit status says that one selector failed.
    code, out = run(capsys, "session", "get", "amount", "missing")
    assert code == 1 and "amount: int = 14" in out.out and "not found" in out.out
    assert run(capsys, "session", "close")[0] == 0


MAPPINGS = """class K:
    def __repr__(self):
        return "'a'"


class N:
    def __repr__(self):
        return "1"


def look():
    big = {f"k{i}": i for i in range(505)}
    twins = {K(): "weird", "a": "real-a"}
    longkey = {"x" * 3000: 1, "short": 2}
    commas = {"a,b": 7}
    intimp = {N(): "weird-int", 1: "real-int"}
    return big, twins, longkey, commas, intimp


look()
"""


def test_mapping_keys_never_read_the_wrong_entry(workspace, capsys):
    (workspace / "maps.py").write_text(MAPPINGS)
    program = workspace / "maps.py"
    selectors = "big['k3'],big['k499'],big['k502'],twins['a'],commas['a,b'],intimp[1]"
    code, out = run(
        capsys, "probe", program, "--at", "17", "--capture", selectors, "--json", "--no-save"
    )
    rows = {row["selector"]: row for row in json.loads(out.out)["rows"]}
    assert code == 0 and rows["big['k3']"]["value"] == "3" and rows["big['k499']"]["value"] == "499"
    # debugpy lists 500 entries: a later key is unknown, never "not found".
    assert "only part of a large mapping" in rows["big['k502']"]["error"]
    # Two keys share the repr 'a': refused instead of reading whichever is listed first.
    assert "ambiguous" in rows["twins['a']"]["error"]
    assert "ambiguous" in rows["intimp[1]"]["error"]
    assert rows["commas['a,b']"]["value"] == "7"

    assert run(capsys, "session", "start", program, "--at", "17", "--name", "maps")[0] == 0
    # The listing never pairs an impostor's value with the real key's selector.
    code, out = run(capsys, "session", "get", "twins", "intimp", "--name", "maps")
    assert "twins['a']" not in out.out and "intimp[1]" not in out.out
    assert "'a' [not selectable]: str = 'weird'" in out.out
    assert "(children: peeky session get big --name maps)" in (
        run(capsys, "session", "inspect", "--name", "maps")[1].out
    )
    # A child whose name is too long to carry is shown, not fatal to the session.
    code, out = run(capsys, "session", "get", "longkey", "--name", "maps")
    assert code == 0 and "<3002-character name> [not selectable]: int = 1" in out.out
    assert "longkey['short']: int = 2" in out.out
    assert run(capsys, "session", "get", "longkey['short']", "--name", "maps")[0] == 0
    assert run(capsys, "session", "close", "--name", "maps")[0] == 0


BUSY = """class K:
    def __repr__(self):
        return "'a'"


def look():
    a1 = a2 = a3 = a4 = a5 = a6 = a7 = a8 = a9 = 0
    big = {f"k{i}": i for i in range(505)}
    mid = {f"m{i}": i for i in range(470)}
    outer = {f"o{i}": i for i in range(300)}
    outer["a"] = {f"k{i}": i for i in range(250)}
    tw = {K(): "weird", **{f"p{i}": i for i in range(500)}, "a": "real-a"}
    return big, mid, outer, tw, a1, a2, a3, a4, a5, a6, a7, a8, a9


look()
"""


def test_keys_read_in_busy_frames_and_never_from_an_impostor(workspace, capsys):
    (workspace / "busy.py").write_text(BUSY)
    selectors = "big['k3'],mid['m0'],outer['a']['k1'],tw['a'],tw['p0']"
    code, out = run(
        capsys,
        "probe",
        workspace / "busy.py",
        "--at",
        "13",
        "--capture",
        selectors,
        "--json",
        "--no-save",
    )
    rows = {row["selector"]: row for row in json.loads(out.out)["rows"]}
    assert code == 0
    assert rows["big['k3']"]["value"] == "3" and rows["mid['m0']"]["value"] == "0"
    assert rows["outer['a']['k1']"]["value"] == "1" and rows["tw['p0']"]["value"] == "0"
    # The impostor is listed under the plain name and the real "a" is past debugpy's
    # 500-entry cap: refused, never read as 'weird'.
    assert "ambiguous" in rows["tw['a']"]["error"] and "value" not in rows["tw['a']"]


def test_sessions_step_over_into_and_out_and_run_to_a_line(workspace, capsys):
    program = workspace / "pagination_bug.py"
    assert run(capsys, "session", "start", program, "--at", "13")[0] == 0
    code, out = run(capsys, "session", "step", "--into")
    assert code == 0 and "pagination_bug.py:5" in out.out
    assert "pagination_bug.py:7" in run(capsys, "session", "step")[1].out
    code, out = run(capsys, "session", "step", "--json")
    state = json.loads(out.out)
    assert state["location"].endswith("pagination_bug.py:8") and state["reason"] == "step"
    assert "outside_program" not in state
    assert "start: int = 3" in run(capsys, "session", "get", "start")[1].out
    assert "pagination_bug.py:13" in run(capsys, "session", "step", "--out")[1].out
    assert "terminated" in run(capsys, "session", "step")[1].out
    assert "PAGE: [40, 50, 60]" in run(capsys, "session", "report")[1].out
    assert run(capsys, "session", "close")[0] == 0

    # continue --to stops once at another line, then only the session's own breakpoint.
    assert run(capsys, "session", "start", program, "--at", "7")[0] == 0
    code, out = run(capsys, "session", "continue", "--to", "9")
    assert code == 0 and "pagination_bug.py:9" in out.out
    assert "end: int = 6" in run(capsys, "session", "get", "end")[1].out
    assert "terminated" in run(capsys, "session", "continue")[1].out
    assert run(capsys, "session", "close")[0] == 0
    code, out = run(capsys, "session", "continue", "--to", "9", "--to-exit")
    assert code == 2


def test_step_and_continue_requests_are_validated(tmp_path):
    from peeky.worker import DebugWorker

    for operation, args, message in (
        ("step", {"kind": "sideways"}, "over, in or out"),
        ("continue", {"to_line": 0}, "positive line"),
        ("continue", {"to_line": "9"}, "positive line"),
        ("continue", {"to_line": 9, "to_exit": True}, "not both"),
        ("step", {"calls": "yes"}, "calls must be a boolean"),
    ):
        with pytest.raises(ValueError, match=message):
            DebugWorker.validate(operation, args)


CALLS = """def rename(items, at, text):
    items[at] = text


def walk(depth):
    if depth == 0:
        return 0
    return 1 + walk(depth - 1)


def run(names):
    rename(names, 0, "b")
    total = walk(2)

    return names, total


print("CALLS:", run(["a"]))
"""


def session_at(tmp_path, monkeypatch, name, text):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    program = tmp_path / name
    program.write_text(text)
    return program


def test_session_steps_say_when_they_stop_in_another_function(tmp_path, capsys, monkeypatch):
    # A step off `rename`'s last line lands in `run`, and says so, and says why the next
    # `get` of rename's names finds none.
    program = session_at(tmp_path, monkeypatch, "calls.py", CALLS)
    try:
        assert run(capsys, "session", "start", program, "--at", "2")[0] == 0
        code, out = run(capsys, "session", "step")
        assert code == 0 and out.out.splitlines()[1:] == ["note: left `rename`; now in `run`"]
        code, out = run(capsys, "session", "get", "items")
        place = "note: this stop is in run at calls.py:12, after a step from `rename`"
        assert code == 1 and place in out.out, out.out
        code, out = run(capsys, "session", "get", "items", "--json")
        (row,) = json.loads(out.out)
        assert row["call_change"] == {"kind": "left", "from": "rename", "to": "run", "calls": 1}
        code, out = run(capsys, "session", "step", "--json")
        state = json.loads(out.out)
        assert state["location"].endswith("calls.py:13") and "call_change" not in state
        code, out = run(capsys, "session", "step", "--into", "--json")
        state = json.loads(out.out)
        assert state["call_change"] == {"kind": "deeper", "from": "run", "to": "walk", "calls": 1}
        assert "note: now in `walk`, one call deeper, called from `run`" in (
            run(capsys, "session", "wait")[1].out
        )
        assert "note:" not in run(capsys, "session", "step")[1].out
        # A recursive call is one call deeper, though the function's name is the same.
        code, out = run(capsys, "session", "step", "--into")
        assert "calls.py:6 in walk" in out.out
        assert "note: now in `walk`, one call deeper, called from `walk`" in out.out
        code, out = run(capsys, "session", "step", "--out")
        assert "calls.py:8 in walk" in out.out and "note: left `walk`; now in `walk`" in out.out
        assert "returned" not in out.out
    finally:
        run(capsys, "session", "close")


RECURSION = """def f(d):
    if d == 0:
        marker = d
    else:
        f(0)


for d in (0, 1):
    f(d)
"""


def test_a_continue_says_nothing_of_calls(tmp_path, capsys, monkeypatch):
    # From the first `f(0)`, `continue` reached another `f(0)` that `f(1)`
    # called, and the state said "entered `f`, called from `f`". The stacks cannot tell
    # one activation from another, so a continue's stop is never compared.
    program = session_at(tmp_path, monkeypatch, "recursion.py", RECURSION)
    try:
        assert run(capsys, "session", "start", program, "--at", "3")[0] == 0
        code, out = run(capsys, "session", "continue", "--json")
        state = json.loads(out.out)
        assert code == 0 and state["hit"] == 2 and "call_change" not in state, state
        assert "note" not in run(capsys, "session", "wait")[1].out
        code, out = run(capsys, "session", "get", "nosuch")
        assert code == 1 and "after a step" not in out.out
    finally:
        run(capsys, "session", "close")
    program.write_text(CALLS)
    try:
        assert run(capsys, "session", "start", program, "--at", "2")[0] == 0
        code, out = run(capsys, "session", "continue", "--to", "6", "--json")
        state = json.loads(out.out)
        assert code == 0 and state["location"].endswith(":6") and "call_change" not in state
    finally:
        run(capsys, "session", "close")


GENERATOR = """def numbers():
    first = 1
    yield first
    yield 2


def main():
    total = 0
    for n in numbers():
        total += n
    return total


print("GEN:", main())
"""


def test_a_step_out_of_a_generator_at_yield_never_says_it_returned(tmp_path, capsys, monkeypatch):
    # The generator only yielded, and the state said "returned from".
    program = session_at(tmp_path, monkeypatch, "gen.py", GENERATOR)
    try:
        assert run(capsys, "session", "start", program, "--at", "2")[0] == 0
        assert "gen.py:3 in numbers" in run(capsys, "session", "step")[1].out
        code, out = run(capsys, "session", "step", "--out", "--json")
        state = json.loads(out.out)
        assert state["location"].endswith("gen.py:9") and state["function"] == "main", state
        assert state["call_change"] == {"kind": "left", "from": "numbers", "to": "main", "calls": 1}
        out = run(capsys, "session", "wait")[1].out
        assert "note: left `numbers`; now in `main`" in out and "returned" not in out, out
    finally:
        run(capsys, "session", "close")


DEEP = """def deep(n):
    if n == 0:
        return 0
    return 1 + deep(n - 1)


print("DEEP:", deep(300))
"""


def test_a_stack_deeper_than_the_bound_gives_no_note_and_steps_on(tmp_path, capsys, monkeypatch):
    # A stack read whole (`levels: 0`) could pass the debugger connection's 4 MiB message
    # limit for a deep recursion. At most 256 frames are read, and a stack not known to be
    # whole gets no note.
    program = session_at(tmp_path, monkeypatch, "deep.py", DEEP)
    try:
        # 302 frames deep: each step leaves one `deep` for its caller.
        assert run(capsys, "session", "start", program, "--at", "3")[0] == 0
        for n, args in enumerate(((), ("--into",), ("--out",)), 1):
            code, out = run(capsys, "session", "step", *args, "--json")
            state = json.loads(out.out)
            assert code == 0 and state["location"].endswith("deep.py:4"), (args, state)
            assert "call_change" not in state, state
            assert f"n: int = {n}" in run(capsys, "session", "get", "n")[1].out
        assert run(capsys, "session", "continue", "--to-exit")[0] == 0
        assert "DEEP: 300" in run(capsys, "session", "report")[1].out
    finally:
        run(capsys, "session", "close")


def test_session_start_warns_when_it_stopped_at_a_moved_breakpoint(tmp_path, capsys, monkeypatch):
    # `session start --at` a blank line stops at the line before it, and warns, as a probe
    # at that line does.
    program = session_at(tmp_path, monkeypatch, "calls.py", CALLS)
    moved = (
        "The debugger moved the breakpoint from line 14 to line 13 (line 14 has no code of its "
        "own); values are from line 13. Choose a line from `peeky describe`."
    )
    code, out = run(
        capsys, "probe", program, "--at", "14", "--capture", "names", "--json", "--no-save"
    )
    assert code == 0 and moved in json.loads(out.out)["warnings"]
    try:
        code, out = run(capsys, "session", "start", program, "--at", "14")
        assert code == 0 and "calls.py:13 in run, hit 1" in out.out
        assert f"\nwarning: {moved}" in out.out, out.out
    finally:
        run(capsys, "session", "close")
    try:
        code, out = run(capsys, "session", "start", program, "--at", "14", "--json")
        assert code == 0 and json.loads(out.out)["warnings"] == [moved]
    finally:
        run(capsys, "session", "close")
    try:
        # Where it is not moved, no warning.
        code, out = run(capsys, "session", "start", program, "--at", "13", "--json")
        assert code == 0 and "warnings" not in json.loads(out.out)
    finally:
        run(capsys, "session", "close")
    # A breakpoint moved into a function that never runs. The program
    # ends, and "values are from line 2" would say nothing true.
    program.write_text('def never():\n    value = 1\n\n\nprint("done")\n')
    try:
        code, out = run(capsys, "session", "start", program, "--at", "3", "--json")
        state = json.loads(out.out)
        assert state["state"] == "terminated" and "warnings" not in state, state
    finally:
        run(capsys, "session", "close")


def test_steps_into_code_outside_the_root_read_and_change_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "app").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "helper.py").write_text("def scale(value):\n    return value * 3\n")
    (tmp_path / "app" / "main.py").write_text(
        "import sys\n"
        "sys.path.insert(0, __file__.rsplit('/', 2)[0] + '/lib')\n"
        "import helper\n\n"
        "total = 5\n"
        "print('RESULT:', helper.scale(total))\n"
    )
    assert run(capsys, "session", "start", tmp_path / "app" / "main.py", "--at", "6")[0] == 0
    code, out = run(capsys, "session", "step", "--into", "--json")
    state = json.loads(out.out)
    assert state["outside_program"] is True and state["returns_to_program"] is True
    for command in (("inspect",), ("get", "value"), ("set", "value=100")):
        code, out = run(capsys, "session", *command)
        assert code == 2 and "outside the program's sources" in out.err
    assert "main.py:6" in run(capsys, "session", "step", "--out")[1].out
    # A line the debugger would move is refused, and the stop is kept.
    code, out = run(capsys, "session", "continue", "--to", "500")
    assert code == 2 and "has no code to stop at" in out.err
    assert "total: int = 5" in run(capsys, "session", "get", "total")[1].out
    assert "terminated" in run(capsys, "session", "continue")[1].out
    assert "RESULT: 15" in run(capsys, "session", "report")[1].out
    assert run(capsys, "session", "close")[0] == 0


def test_a_new_session_on_a_worker_starts_with_no_stop_state(workspace):
    from peeky.worker import DebugWorker

    worker = DebugWorker(workspace)
    worker.co_served = worker.stop_outside = worker.stop_returns = True
    launch = worker.dispatch(
        "launch", {"program": "pagination_bug.py", "breakpoints": [{"line": 8}]}
    )
    try:
        assert not (worker.co_served or worker.stop_outside or worker.stop_returns)
    finally:
        worker.dispatch("close", {"session_id": launch["session_id"]})


def test_program_arguments_can_follow_a_double_dash(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    script = tmp_path / "args.py"
    script.write_text("import sys\ncount = len(sys.argv)\nprint('ARGS:', sys.argv[1:])\n")
    code, out = run(
        capsys,
        "probe",
        script,
        "--at",
        "3",
        "--capture",
        "count",
        "--json",
        "--no-save",
        "--",
        "a",
        "--b",
        "-5",
    )
    result = json.loads(out.out)
    assert code == 0, result.get("error")
    assert result["program_args"] == ["a", "--b", "-5"]
    assert "ARGS: ['a', '--b', '-5']" in result["output"]
    assert result["rows"][0]["value"] == "4"


def test_sessions_number_their_hits_and_run_on_to_the_nth(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    script = tmp_path / "loop.py"
    script.write_text("total = 0\nfor i in range(5):\n    total += i\nprint('TOTAL:', total)\n")
    try:
        code, out = run(capsys, "session", "start", script, "--at", "3", "--hit", "3")
        assert code == 0 and ", hit 3 (paused" in out.out, out
        assert run(capsys, "session", "get", "i")[1].out.strip() == "i: int = 2"
        # Inspect names the hit too, as start and continue do.
        code, out = run(capsys, "session", "inspect")
        assert code == 0 and "loop.py:3 in <module>, hit 3" in out.out.splitlines()[0], out
        code, out = run(capsys, "session", "continue", "--count", "2")
        assert code == 0 and ", hit 5 (paused" in out.out, out
        # A one-shot --to line is not a hit, and has no hit number.
        code, out = run(capsys, "session", "continue", "--to", "4")
        assert code == 0 and "loop.py:4 in <module> (paused" in out.out, out
        code, out = run(capsys, "session", "continue", "--count", "3")
        assert code == 3 and "note: the program exited before stop 3" in out.out, out
        code, out = run(capsys, "session", "continue", "--count", "2", "--to-exit")
        assert code == 2 and "cannot go with --to" in out.err
    finally:
        run(capsys, "session", "close")
    try:
        code, out = run(capsys, "session", "start", script, "--at", "3", "--hit", "9")
        assert code == 3 and "note: the program exited before hit 9; last hit 5" in out.out, out
    finally:
        run(capsys, "session", "close")
    with pytest.raises(SystemExit) as exit_:
        run(capsys, "session", "start", script, "--at", "3", "--hit", "0")
    assert exit_.value.code == 2 and "whole number from 1" in capsys.readouterr().err


def test_a_programs_own_breakpoint_call_ends_hit_numbering(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    script = tmp_path / "bp.py"
    script.write_text(
        "total = 0\nfor i in range(3):\n    total += i\n    if i == 1:\n"
        "        breakpoint()\n        x = i\nprint(total)\n"
    )
    states = []
    try:
        code, out = run(capsys, "session", "start", script, "--at", "3", "--json")
        states.append(json.loads(out.out))
        for _ in range(3):
            code, out = run(capsys, "session", "continue", "--json")
            assert code == 0, out
            states.append(json.loads(out.out))
    finally:
        run(capsys, "session", "close")
    lines = [(s["location"].rsplit(":", 1)[-1], s.get("hit"), s.get("numbered")) for s in states]
    # breakpoint() is not the session's: no number from there on, never a shifted one.
    assert lines[:2] == [("3", 1, True), ("3", 2, True)], lines
    assert lines[2][0] != "3" and lines[2][1:] == (None, False), lines
    assert lines[3] == ("3", None, False), lines


def test_a_python_string_takes_a_text_as_its_debugger_evaluates_it(workspace, capsys):
    # The same `--set name="..."` as C, C++, Rust and Java; debugpy
    # evaluates it as a Python literal (JSON's escapes are Python's too), and the name is
    # rebound to the new str.
    program = workspace / "greet.py"
    program.write_text(
        'def main():\n    name = "apple "\n    alias = name\n'
        '    print(f"NAME:[{name}] ALIAS:[{alias}]")\n\n\nmain()\n'
    )
    probe = ["probe", program, "--at", "4", "--capture", "name", "--json", "--quiet"]
    code, out = run(capsys, *probe, "--set", 'name="caf\\u00e9 \\"x\\""')
    result = json.loads(out.out)
    assert code == 0, result.get("error")
    assert 'NAME:[café "x"] ALIAS:[apple ]' in result["output"], result["output"]
    assert [(c["before"], c["after"]) for c in result["interventions"]] == [
        ("'apple '", "'café \"x\"'")
    ]


TOKEN = "cli-test-token-1234567890abcdef"


def gone(pid, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return True
        except ChildProcessError:
            pass
        time.sleep(0.05)
    return False


def test_two_starts_with_one_name_cannot_both_succeed(workspace):
    command = [
        sys.executable,
        "-m",
        "peeky.cli",
        "session",
        "start",
        str(workspace / "pagination_bug.py"),
        "--at",
        "8",
    ]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    pipes = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    starts = [subprocess.Popen(command, env=env, **pipes) for _ in "ab"]
    results = [(p.wait(timeout=60), *p.communicate()) for p in starts]
    codes = sorted(code for code, _, _ in results)
    assert codes == [0, 2], results
    assert "exists" in next(err for code, _, err in results if code == 2)
    record = json.loads((workspace / "state/sessions/default.json").read_text())
    subprocess.run([*command[:3], "session", "close"], env=env, check=True, capture_output=True)
    assert gone(record["pid"]), "the winning start's worker outlived its session"


def test_an_interrupted_start_leaves_no_worker_and_frees_the_name(workspace, capsys, monkeypatch):
    import peeky.cli as cli

    spawned = []
    real_popen = subprocess.Popen

    def recording(*args, **kwargs):
        spawned.append(real_popen(*args, **kwargs))
        return spawned[-1]

    real_call = cli._LocalClient.call

    def interrupted(self, operation, **arguments):
        if operation == "launch":
            raise KeyboardInterrupt
        return real_call(self, operation, **arguments)

    monkeypatch.setattr(cli.subprocess, "Popen", recording)
    monkeypatch.setattr(cli._LocalClient, "call", interrupted)
    with pytest.raises(KeyboardInterrupt):
        main(["session", "start", str(workspace / "pagination_bug.py"), "--at", "8"])
    assert spawned and spawned[0].poll() is not None
    assert not (workspace / "state/sessions/default.json").exists()
    monkeypatch.setattr(cli._LocalClient, "call", real_call)
    code, out = run(capsys, "session", "start", workspace / "pagination_bug.py", "--at", "8")
    assert code == 0, out.err
    closed = json.loads(run(capsys, "session", "close", "--json")[1].out)
    # The program, stopped at line 8, ended with its session.
    assert closed["closed"] == "default" and closed["cleanup"].startswith("terminated pid ")


def test_relative_java_bundles_follow_the_configuration_file(tmp_path, monkeypatch):
    from peeky.cli import resolve

    configuration = tmp_path / "conf" / "target.json"
    configuration.parent.mkdir()
    configuration.write_text(
        json.dumps({"language": "java", "bundle": "adapter", "sources": ["A.java"]})
    )
    (tmp_path / "A.java").write_text("class A {}\n")
    monkeypatch.chdir(tmp_path / "conf")
    data = resolve(tmp_path, "A.java:1", configuration)[4]
    assert data["bundle"] == str((tmp_path / "conf" / "adapter").resolve())


@pytest.mark.parametrize(
    "reply",
    [
        {"status": "timeout", "error": {"code": "TIMEOUT", "message": "cap", "hint": "h"}},
        {"error": {"code": "DEBUGGER_ERROR", "message": "child crashed", "hint": "h"}},
    ],
)
def test_partial_remote_probe_replies_keep_their_error(workspace, capsys, monkeypatch, reply):
    class Worker:
        sources = {"kind": "python"}

        def probe(self, *args, **kwargs):
            return json.loads(json.dumps(reply))

    monkeypatch.setattr("peeky.cli._remote", lambda url: (Worker(), TOKEN))
    probe = ["probe", workspace / "pagination_bug.py", "--at", "8", "--no-save"]
    code, out = run(capsys, *probe, "--worker", "http://127.0.0.1:1", "--json")
    result = json.loads(out.out)  # exactly one JSON document
    assert code == 1 and result["error"]["code"] == reply["error"]["code"]
    assert result["status"] == reply.get("status", "failed") and result["rows"] == []
    code, out = run(capsys, *probe, "--worker", "http://127.0.0.1:1")
    assert code == 1 and f"error [{reply['error']['code']}]" in out.out


def test_session_get_reports_one_unreadable_selector_as_a_row(workspace, capsys):
    program = workspace / "wide.py"
    program.write_text(
        "class O:\n    pass\n"
        "o = O()\n"
        "for i in range(600):\n    setattr(o, f'a{i:03}', i)\n"
        "small = [1, 2]\n"
        "print('ready')\n"
    )
    assert run(capsys, "session", "start", program, "--at", "7")[0] == 0
    code, out = run(capsys, "session", "get", "small[0]", "o.a599", "small[1]", "--json")
    rows = {row["selector"]: row for row in json.loads(out.out)}
    # Every row is reported; the exit status says that one selector had no value.
    assert code == 1, out.err
    assert rows["small[0]"]["value"] == "1" and rows["small[1]"]["value"] == "2"
    assert rows["o.a599"].get("error")
    run(capsys, "session", "close")


def test_timeouts_are_checked_before_anything_runs(workspace, capsys):
    program = workspace / "pagination_bug.py"
    code, out = run(capsys, "session", "start", program, "--at", "8", "--timeout", "500")
    assert code == 2 and "--timeout" in out.err
    assert json.loads(run(capsys, "session", "list", "--json")[1].out) == []
    assert run(capsys, "session", "start", program, "--at", "8")[0] == 0
    code, out = run(capsys, "session", "continue", "--timeout", "0")
    assert code == 2 and "--timeout" in out.err
    code, out = run(capsys, "session", "get", "start", "--json")
    assert code == 0 and json.loads(out.out)[0]["value"] == "3"  # still stopped
    assert json.loads(run(capsys, "session", "list", "--json")[1].out) == ["default"]
    run(capsys, "session", "close")


def test_long_python_lists_are_not_reported_missing(workspace, capsys):
    program = workspace / "long.py"
    program.write_text("xs = list(range(200))\nprint(len(xs))\n")
    code, out = run(
        capsys, "probe", program, "--at", "2", "--capture", "xs[5],xs[150]", "--json", "--no-save"
    )
    rows = {row["selector"]: row for row in json.loads(out.out)["rows"]}
    assert code == 0 and rows["xs[5]"]["value"] == "5"
    assert "beyond the debugger's visible window" in rows["xs[150]"]["error"]


def test_describe_reports_python_syntax_errors_as_request_errors(workspace, capsys):
    program = workspace / "bad.py"
    program.write_text("x = 1\ndef broken(:\n")
    code, out = run(capsys, "describe", program, "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "INVALID_REQUEST" and "line 2" in error["message"]


def test_selectors_share_one_inspection_unless_budget_runs_out():
    from peeky.probe import _capture

    class Worker:
        def __init__(self):
            self.inspects, self.budget = 0, 2

        def dispatch(self, operation, args):
            if operation == "inspect":
                self.inspects += 1
                self.budget = 2
                child = {"name": "c", "value": "9", "variablesReference": 0}
                variables = [{"name": n, "value": n, "variablesReference": 0} for n in "abcde"] + [
                    {"name": "o", "value": "O", "variablesReference": 7}
                ]
                self.child = child
                return {"frames": [{"line": 3}], "scopes": [{"variables": variables}]}
            self.budget -= 1
            if self.budget < 0:
                raise ValueError("Expansion node budget exhausted; inspect again")
            return {"variables": [self.child], "truncated": False}

    worker = Worker()
    rows = _capture(worker, {"stop_id": "s"}, ("a", "b", "c", "d", "e"), 1, "observed")
    assert worker.inspects == 1 and [r["value"] for r in rows] == list("abcde")
    worker = Worker()
    rows = _capture(worker, {"stop_id": "s"}, ("o.c",) * 4, 1, "observed")
    assert [r.get("value") for r in rows] == ["9"] * 4 and worker.inspects == 2


def test_a_shared_inspection_never_makes_a_readable_value_an_error(workspace, capsys):
    program = workspace / "spent.py"
    program.write_text("a = list(range(300))\nitems = list(range(60))\nprint(len(a))\n")
    selectors = ",".join(["a[10]"] * 7 + ["items[-1]", "items[59]"])
    code, out = run(
        capsys, "probe", program, "--at", "3", "--capture", selectors, "--json", "--no-save"
    )
    rows = {row["selector"]: row for row in json.loads(out.out)["rows"]}
    assert code == 0 and rows["items[-1]"].get("value") == "59", rows["items[-1]"]
    assert rows["items[59]"]["value"] == "59"


def test_an_interrupted_start_never_blocks_its_name(workspace, capsys):
    sessions = workspace / "state" / "sessions"
    sessions.mkdir(parents=True, mode=0o700)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    record = sessions / "default.json"
    record.write_text(json.dumps({"name": "default", "starting": dead.pid}))
    record.chmod(0o600)
    code, out = run(capsys, "session", "inspect", "--json")
    assert code == 2 and json.loads(out.out)["error"]["code"] == "NO_SESSION"
    code, out = run(capsys, "session", "close", "--forget", "--json")
    assert code == 0 and json.loads(out.out) == {"forgot": "default", "worker_contacted": False}
    record.write_text(json.dumps({"name": "default", "starting": dead.pid}))
    record.chmod(0o600)
    # A new start takes over the abandoned claim.
    code, out = run(capsys, "session", "start", workspace / "pagination_bug.py", "--at", "8")
    assert code == 0, out.err
    run(capsys, "session", "close")
    record.write_text(json.dumps({"name": "default", "starting": os.getpid()}))
    record.chmod(0o600)
    code, out = run(capsys, "session", "start", workspace / "pagination_bug.py", "--at", "8")
    assert code == 2 and "exists" in out.err  # a live start keeps its claim
    assert run(capsys, "session", "close", "--forget")[0] == 0
