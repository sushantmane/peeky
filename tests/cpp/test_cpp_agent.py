"""Deterministic agent decisions, real HTTP debugger, C++ builds and fresh executable checks."""

import hashlib
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

import peeky.agent as agent_module
from peeky.agent import Investigation

ROOT = Path(__file__).resolve().parents[2]
BUG = "int start = request.page * request.page_size;"
FIX = "int start = (request.page - 1) * request.page_size;"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def actions(line, finishes):
    return iter(
        [
            {"action": "observe", "line": line, "reason": "Inspect computed start for first page"},
            {
                "action": "intervene",
                "variable": "start",
                "value": "0",
                "reason": "Test one-based page hypothesis",
            },
            *[
                {"action": "finish", "old": BUG, "new": new, "reason": reason}
                for new, reason in finishes
            ],
        ]
    )


def make_runner(data, decisions, steps=6):
    client, execution, source, check, _, output = data
    return Investigation(
        client,
        "pagination_bug.cpp",
        source,
        check,
        output,
        decisions,
        max_steps=steps,
        execution=execution,
    )


def test_cpp_agent_retries_compile_failure_and_overfit_before_general_fix(
    cpp_investigation, monkeypatch
):
    _, _, source, check, line, _ = cpp_investigation
    header = source.parent / "pagination.hpp"
    original = digest(source), digest(check), digest(header)
    sequence = actions(
        line,
        [
            ("int start = ;", "Deliberate compile failure must be returned for retry"),
            ("int start = 0;", "Deliberate first-page overfit must fail independent cases"),
            (FIX, "General one-based pagination fix"),
        ],
    )

    def decide(context):
        if len(context["steps"]) == 1:
            snapshot = context["steps"][0]["result"]["snapshot"]
            assert any(
                v["name"] == "start" and v["value"] == "3"
                for scope in snapshot["scopes"]
                for v in scope["variables"]
            )
        return next(sequence)

    # Run the real CLI wiring with an injected decision provider; no model process is invoked.
    client, execution, _, _, _, output = cpp_investigation
    config = output.parent / "native-target.json"
    config.write_text(json.dumps(asdict(execution.target)))
    real_which = shutil.which
    monkeypatch.setattr(
        agent_module.shutil,
        "which",
        lambda name, *a, **kw: "/fixture/codex" if name == "codex" else real_which(name, *a, **kw),
    )
    monkeypatch.setattr(agent_module, "CodexDecision", lambda *a, **kw: decide)
    monkeypatch.setenv("DEBUG_TOKEN", client.token)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "peeky-agent",
            "--root",
            str(source.parent),
            "--native-config",
            str(config),
            "--program",
            "pagination_bug.cpp",
            "--check",
            "verify_pagination.py",
            "--model",
            "fixture-model",
            "--max-steps",
            "6",
            "--url",
            client.url,
            "--output",
            str(output),
        ],
    )
    agent_module.main()
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "verified"
    assert "PAGE:[10, 20, 30]" in report["steps"][1]["result"]["output"]
    compile_failure = report["steps"][2]["result"]
    assert compile_failure["stage"] == "build" and not compile_failure["passed"]
    overfit = report["steps"][3]["result"]
    assert not overfit["passed"] and overfit["exit_code"] != 0
    verification = report["verification"]
    assert verification["passed"] and verification["exit_code"] == 0
    assert verification["artifact_manifest_id"]
    assert verification["build_manifest"]
    assert verification["artifacts_retained"] is False
    assert not Path(verification["descriptor"]["command"][0]).exists()
    assert "12 independent C++ pagination cases" in verification["stdout"]
    assert Path(verification["candidate"]).suffix == ".cpp"
    assert FIX in Path(verification["candidate"]).read_text()
    assert report["source_unchanged"] and report["check_unchanged"]
    assert (digest(source), digest(check), digest(header)) == original


def test_cpp_agent_requires_runtime_intervention(cpp_investigation):
    runner = make_runner(
        cpp_investigation,
        lambda _: {"action": "finish", "old": BUG, "new": FIX, "reason": "Skip evidence"},
        steps=1,
    )
    report = runner.run("Fix pagination")
    assert report["status"] == "insufficient_evidence"
    assert "runtime intervention" in report["steps"][0]["error"]
    assert not (cpp_investigation[-1] / "candidate").exists()


@pytest.mark.parametrize("name", ["pagination_bug.cpp", "pagination.hpp"])
def test_cpp_remote_manifest_mismatch_closes_session(cpp_investigation, name):
    _, _, source, _, line, output = cpp_investigation
    remote = source.parent.parent / "worker-inputs" / name
    remote.write_text(remote.read_text() + "\n// remote drift\n")
    runner = make_runner(cpp_investigation, lambda _: {}, steps=1)
    with pytest.raises(ValueError, match="differs"):
        runner.observe(line)
    assert runner.session is None
    assert not (output / "candidate").exists()
