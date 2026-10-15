"""Stateless probes against real debugpy: CLI, in-process API and HTTP."""

import shutil
import threading
from pathlib import Path
from urllib.error import HTTPError

import pytest

from peeky.client import Client
from peeky.probe import Probe, main, run_probe
from peeky.worker import make_server

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def program(tmp_path):
    shutil.copyfile(ROOT / "examples/pagination_bug.py", tmp_path / "pagination_bug.py")
    return tmp_path


def test_probe_captures_intervenes_and_reports_in_one_call(program):
    result = run_probe(
        program,
        Probe("pagination_bug.py", 8, ("start", "page", "items[1]", "missing"), (("start", "0"),)),
    )
    assert result["status"] == "completed" and result["hits"] == 1
    observed = {r["selector"]: r for r in result["rows"] if r["phase"] == "observed"}
    assert observed["start"]["value"] == "3" and observed["page"]["value"] == "1"
    assert observed["items[1]"]["value"] == "20"
    assert "not found" in observed["missing"]["error"]
    after = {r["selector"]: r for r in result["rows"] if r["phase"] == "after-set"}
    assert after["start"]["value"] == "0"
    assert result["interventions"] == [{"name": "start", "before": "3", "after": "0"}]
    assert "PAGE: [10, 20, 30]" in result["output"]
    assert "session_id" not in result and "stop_id" not in str(result["rows"])


def test_probe_without_intervention_observes_original_behavior(program):
    result = run_probe(program, Probe("pagination_bug.py", 8, ("start",), hits=5))
    assert result["status"] == "completed" and result["hits"] == 1
    assert "PAGE: [40, 50, 60]" in result["output"]


def test_probe_failure_is_reported_and_cleaned(program):
    result = run_probe(program, Probe("pagination_bug.py", 8, ("start",), (("nope", "1"),)))
    assert result["status"] == "failed" and "nope" in result["error"]["message"]
    assert result["error"]["code"] == "NOT_FOUND" and "visible: " in result["error"]["message"]
    assert "page_size" in result["error"]["message"]
    assert result["rows"][0]["value"] == "3"


@pytest.mark.parametrize("value", ["min(items)", "__import__('os').getpid()", "page", "x.y", ""])
def test_python_interventions_accept_only_inert_literals(program, value):
    result = run_probe(program, Probe("pagination_bug.py", 8, ("start",), (("start", value),)))
    assert result["status"] == "failed" and result["error"]["code"] == "MUTATION_REJECTED"
    assert result["interventions"] == [] and result["rows"][0]["value"] == "3"


def test_python_container_literals_are_valid_interventions(program):
    probe = Probe("pagination_bug.py", 9, ("items",), (("items", "[1, 2, 3, 4, 5, 6, -7]"),))
    result = run_probe(program, probe)
    assert result["status"] == "completed" and "PAGE: [4, 5, 6]" in result["output"]


def test_relocated_breakpoints_and_short_runs_are_flagged(program):
    result = run_probe(program, Probe("pagination_bug.py", 8, ("start",), hits=3))
    assert result["status"] == "completed" and result["hits"] == 1
    assert any("Only 1 of 3 requested hits" in w for w in result["warnings"])
    result = run_probe(program, Probe("pagination_bug.py", 10, ("start",)))
    if result["status"] == "completed":
        line = result["rows"][0]["line"]
        assert line != 10 and any("moved the breakpoint" in w for w in result["warnings"])
    else:
        assert result["error"]["code"] == "NO_STOP"


def test_probe_cli_prints_a_table_and_exit_status(program, capsys):
    code = main(
        ["--root", str(program), "--at", "pagination_bug.py:8", "--capture", "start,page_size"]
        + ["--set", "start=0"]
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert "start" in printed and "set start: 3 -> 0" in printed
    assert "PAGE: [10, 20, 30]" in printed and "status: completed" in printed


def test_http_probe_is_stateless_and_single_flight(program, monkeypatch):
    server = make_server(program, "probe-test-token-123456", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_port}", "probe-test-token-123456")
    try:
        result = client.probe("pagination_bug.py:8", ["start"], {"start": "0"})
        assert result["status"] == "completed" and "PAGE: [10, 20, 30]" in result["output"]
        assert server.worker.dap is None and server.worker.session is None
        for bad in ({"at": "x.py"}, {"at": "pagination_bug.py:8", "evaluate": "1+1"}):
            with pytest.raises(HTTPError) as rejected:
                client.call("probe", **bad)
            assert rejected.value.code == 400
        gate = threading.Event()
        release = threading.Event()
        import peeky.worker as worker_module

        real = worker_module._run_probe_process

        def slow(*args, **kwargs):
            gate.set()
            release.wait(10)
            return real(*args, **kwargs)

        monkeypatch.setattr(worker_module, "_run_probe_process", slow)
        first = threading.Thread(target=client.probe, args=("pagination_bug.py:8",))
        first.start()
        assert gate.wait(10)
        with pytest.raises(HTTPError) as busy:
            client.probe("pagination_bug.py:8")
        assert busy.value.code == 400
        release.set()
        first.join(30)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_probe_process_is_hard_capped_and_cleaned(tmp_path):
    import subprocess
    import time

    from peeky.worker import _run_probe_process

    (tmp_path / "slow.py").write_text("import time\ntime.sleep(60)\nprint('done')\n")
    started = time.monotonic()
    result = _run_probe_process(tmp_path, Probe("slow.py", 3, ("time",), timeout=30), None, 3)
    assert result["status"] == "timeout" and time.monotonic() - started < 12
    time.sleep(1)
    leftover = subprocess.run(
        ["pgrep", "-f", str(tmp_path / "slow.py")], capture_output=True, text=True
    ).stdout
    assert leftover == ""
