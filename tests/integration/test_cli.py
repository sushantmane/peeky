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
    assert "PAGE: [10, 20, 30]" in run(capsys, "session", "report")[1].out
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
    # #76: a general fix often touches two places; each pair is checked against the
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
    ):
        with pytest.raises(ValueError, match=message):
            DebugWorker.validate(operation, args)


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


def test_remote_workers_without_step_are_refused_before_anything_runs():
    from peeky.cli import _require_stepping

    class Old:
        def call(self, operation, **args):
            assert operation == "version"
            return {"operations": ["continue", "inspect", "version"]}

    with pytest.raises(ValueError, match="predates `session step`"):
        _require_stepping({"remote": True}, Old())
    _require_stepping({"remote": False}, Old())


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
        # #93 (eval 4): inspect names the hit too, as start and continue do.
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
