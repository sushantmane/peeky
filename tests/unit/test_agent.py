import os
import sys
from pathlib import Path

import pytest

from peeky.agent import Investigation, execute, verify

ROOT = Path(__file__).resolve().parents[2]


def runner(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("x = 1\n")
    check = tmp_path / "check.py"
    check.write_text("assert True\n")
    result = Investigation(None, "source.py", source, check, tmp_path, None)
    result.interventions = 1
    return result


def test_verifier_rejects_premature_zero_exit(tmp_path):
    candidate = tmp_path / "candidate.py"
    candidate.write_text("raise SystemExit(0)\n")
    result = verify(ROOT / "examples/verify_pagination.py", candidate, tmp_path)
    assert not result["passed"]
    assert not result["checker"]["completed"]
    assert "SystemExit" in result["checker"]["error"]


def test_verifier_rejects_os_exit_without_completion(tmp_path):
    candidate = tmp_path / "candidate.py"
    candidate.write_text("import os; os._exit(0)\n")
    result = verify(ROOT / "examples/verify_pagination.py", candidate, tmp_path)
    assert result["exit_code"] == 0
    assert not result["passed"]


def test_modified_checker_cannot_be_accepted(tmp_path):
    investigation = runner(tmp_path)
    investigation.check.write_text("pass\n")
    with pytest.raises(RuntimeError, match="changed"):
        investigation.finish("x = 1", "x = 2")
    assert investigation.report["status"] != "verified"


def test_checker_mutation_during_verification_rejects_fix(tmp_path):
    investigation = runner(tmp_path)
    replacement = "from pathlib import Path\nPath(__file__).write_text('pass\\n')\n"
    investigation.check.write_text(replacement)
    from peeky.agent import digest

    investigation.report["check_sha256"] = digest(investigation.check)
    with pytest.raises(RuntimeError, match="changed"):
        investigation.finish("x = 1", "x = 2")
    assert investigation.report["status"] != "verified"


def test_no_fix_without_intervention(tmp_path):
    investigation = runner(tmp_path)
    investigation.interventions = 0
    with pytest.raises(ValueError, match="intervention"):
        investigation.finish("x = 1", "x = 2")


# The worker's token by the worker's name and by a client's (pre-release security review).
TOKENS = {"DEBUG_TOKEN": "never-inherit-me", "PEEKY_WORKER_TOKEN": "never-inherit-me-either"}
SHOW_TOKENS = 'import os; print(os.getenv("DEBUG_TOKEN"), os.getenv("PEEKY_WORKER_TOKEN"))'


def test_execute_bounds_output_and_strips_token(tmp_path, monkeypatch):
    for name, value in TOKENS.items():
        monkeypatch.setenv(name, value)
    result = execute([sys.executable, "-c", SHOW_TOKENS], tmp_path)
    assert result["stdout"].strip() == "None None"
    result = execute([sys.executable, "-c", SHOW_TOKENS], tmp_path, env=dict(os.environ))
    assert result["stdout"].strip() == "None None"
    result = execute(
        [sys.executable, "-c", 'while True: print("x"*10000)'], tmp_path, output_limit=32768
    )
    assert result["output_limit_exceeded"]
    assert result["exit_code"] is None
    assert len(result["stdout"]) <= 16000


def test_the_checker_and_the_candidate_get_no_token(tmp_path, monkeypatch):
    for name, value in TOKENS.items():
        monkeypatch.setenv(name, value)
    check = tmp_path / "check.py"
    check.write_text("import runpy, sys\nrunpy.run_path(sys.argv[1])\n")
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        "import os\nleft = sorted({'DEBUG_TOKEN', 'PEEKY_WORKER_TOKEN'} & set(os.environ))\n"
        "assert not left, left\n"
    )
    result = verify(check, candidate, tmp_path)
    assert result["passed"], result["checker"]


def test_the_model_process_gets_no_token(tmp_path, monkeypatch):
    from peeky.agent import CodexDecision

    for name, value in TOKENS.items():
        monkeypatch.setenv(name, value)
    # A stand-in for Codex CLI that answers with the token names it was given.
    fake = tmp_path / "fake_codex.py"
    fake.write_text(
        "import json, os, sys\nsys.stdin.read()\n"
        "answer = sys.argv[sys.argv.index('--output-last-message') + 1]\n"
        "names = sorted({'DEBUG_TOKEN', 'PEEKY_WORKER_TOKEN'} & set(os.environ))\n"
        "open(answer, 'w').write(json.dumps({'tokens': names}))\n"
    )
    (tmp_path / "bin").mkdir()
    codex = tmp_path / "bin" / "codex"
    codex.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    (tmp_path / "scratch").mkdir()
    assert CodexDecision(tmp_path / "scratch")({"task": "none"}) == {"tokens": []}


@pytest.mark.skipif(os.name != "posix", reason="MVP process-group ownership is POSIX")
def test_timeout_cleans_up_descendant(tmp_path):
    sentinel = tmp_path / "survived"
    child = (
        f"import time; from pathlib import Path; time.sleep(0.7); Path({str(sentinel)!r}).touch()"
    )
    script = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(30)'
    result = execute([sys.executable, "-c", script], tmp_path, timeout=0.15)
    assert result["timed_out"]
    import time

    time.sleep(0.8)
    assert not sentinel.exists()


def test_wait_polls_until_stopped(tmp_path):
    class Remote:
        calls = 0

        def call(self, operation, **kwargs):
            assert operation == "wait"
            assert kwargs["timeout"] <= 2
            self.calls += 1
            return {"state": "running" if self.calls < 3 else "stopped"}

    investigation = runner(tmp_path)
    investigation.client = Remote()
    investigation.session = "session"
    assert investigation.wait()["state"] == "stopped"
    assert investigation.client.calls == 3


def test_baseline_deletion_preserves_failure_report(tmp_path):
    import json

    investigation = runner(tmp_path)
    investigation.source.write_text("from pathlib import Path; Path(__file__).unlink()\n")
    from peeky.agent import digest

    investigation.report["source_sha256"] = digest(investigation.source)
    with pytest.raises(RuntimeError, match="changed"):
        investigation.run("test deletion")
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == "failed"
    assert not report["source_unchanged"]


def test_cleanup_preserves_original_exception(tmp_path):
    investigation = runner(tmp_path)

    class Remote:
        def call(self, *args, **kwargs):
            raise RuntimeError("cleanup failure")

    investigation.client = Remote()
    investigation.session = "old-session"
    with pytest.raises(RuntimeError, match="original failure"):
        try:
            raise RuntimeError("original failure")
        finally:
            investigation.close()
    assert investigation.report["cleanup_errors"] == ["cleanup failure"]


def test_worker_refusals_are_step_errors_but_uncertain_failures_stop(tmp_path):
    import json

    from peeky.errors import PeekyError

    investigation = runner(tmp_path)
    investigation.check.write_text("raise SystemExit(1)\n")
    investigation.report["check_sha256"] = __import__("peeky.agent").agent.digest(
        investigation.check
    )
    investigation.max_steps = 3
    codes = iter(["INVALID_BREAKPOINT", "MUTATION_REJECTED", "TIMEOUT"])

    class Remote:
        def call(self, operation, **kwargs):
            assert operation == "launch"
            code = next(codes)
            raise PeekyError("http://w", 400, {}, {"code": code, "message": code.lower()})

    investigation.client = Remote()
    investigation.decide = lambda context: {"action": "observe", "line": 1, "reason": "look"}
    with pytest.raises(PeekyError, match="TIMEOUT"):
        investigation.run("issue")
    report = json.loads((tmp_path / "report.json").read_text())
    assert [step.get("error_code") for step in report["steps"]] == [
        "INVALID_BREAKPOINT",
        "MUTATION_REJECTED",
        "TIMEOUT",
    ]
    assert report["status"] == "failed"
