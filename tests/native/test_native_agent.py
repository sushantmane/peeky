"""Deterministic agent decisions, real HTTP debugger, C builds and fresh executable checks."""

import hashlib
import json
import os
import shutil
import sys
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

import peeky.agent as agent_module
from peeky.agent import Investigation
from peeky.client import Client
from peeky.native import NativeTarget
from peeky.native_verification import NativeVerification
from peeky.worker import make_server

ROOT = Path(__file__).resolve().parents[2]
BUG = "int start = page * page_size;"
FIX = "int start = (page - 1) * page_size;"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def native_investigation(tmp_path):
    bundle = os.environ.get("PEEKY_NATIVE_DAP")
    if not bundle:
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    root = tmp_path / "inputs"
    shutil.copytree(ROOT / "examples/c", root)
    source = root / "pagination_bug.c"
    check = root / "verify_pagination.py"
    target = NativeTarget(
        sources=("pagination_bug.c",),
        headers=("pagination.h",),
        compiler=os.environ.get("PEEKY_NATIVE_CC", "clang"),
        adapter=bundle,
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
    )
    token = "native-agent-tests-token-123456-0123456789abcdef"
    worker_root = tmp_path / "worker-inputs"
    shutil.copytree(root, worker_root)
    server = make_server(worker_root, token, 0, adapter=target)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_port}", token)
    execution = NativeVerification(target, root)
    observe_line = next(
        i for i, line in enumerate(source.read_text().splitlines(), 1) if "/* OBSERVE */" in line
    )
    yield client, execution, source, check, observe_line, tmp_path / "report"
    server.worker.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


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
        "pagination_bug.c",
        source,
        check,
        output,
        decisions,
        max_steps=steps,
        execution=execution,
    )


def test_native_agent_retries_compile_failure_and_overfit_before_general_fix(
    native_investigation, monkeypatch
):
    _, _, source, check, line, _ = native_investigation
    header = source.parent / "pagination.h"
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
    client, execution, _, _, _, output = native_investigation
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
            "pagination_bug.c",
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
    assert "12 independent C pagination cases" in verification["stdout"]
    assert Path(verification["candidate"]).suffix == ".c"
    assert FIX in Path(verification["candidate"]).read_text()
    assert report["source_unchanged"] and report["check_unchanged"]
    assert (digest(source), digest(check), digest(header)) == original


def test_native_agent_requires_runtime_intervention(native_investigation):
    runner = make_runner(
        native_investigation,
        lambda _: {"action": "finish", "old": BUG, "new": FIX, "reason": "Skip evidence"},
        steps=1,
    )
    report = runner.run("Fix pagination")
    assert report["status"] == "insufficient_evidence"
    assert "runtime intervention" in report["steps"][0]["error"]
    assert not (native_investigation[-1] / "candidate").exists()


@pytest.mark.parametrize(
    "tamper",
    [
        "candidate",
        "original-source",
        "header",
        "prepared-header",
        "saved-candidate",
        "checker",
        "descriptor",
        "binary",
    ],
)
def test_native_verifier_tampering_cannot_be_accepted(native_investigation, tamper):
    _, execution, source, check, _, output = native_investigation
    injection = {
        "candidate": 'Path(spec["source"]["path"]).write_text("changed by checker")',
        "saved-candidate": f"Path({str(output / 'candidate/pagination_bug.c')!r}).write_text('changed by checker')",
        "checker": 'Path(__file__).write_text("changed by checker")',
        "descriptor": 'Path(sys.argv[1]).write_text("{}")',
        "original-source": f"Path({str(source)!r}).write_text('changed original')",
        "header": f"Path({str(source.parent / 'pagination.h')!r}).write_text('changed header')",
        "prepared-header": 'Path(spec["source"]["path"]).with_name("pagination.h").write_text("changed header")',
        "binary": 'Path(spec["command"][0]).write_bytes(b"changed")',
    }[tamper]
    marker = output / "tamper-executed"
    check.write_text(
        check.read_text()
        + "\n"
        + injection
        + "\n"
        + f"Path({str(marker)!r}).write_text('executed')\n"
    )
    output.mkdir()
    try:
        result = execution.candidate(source, check, BUG, FIX, output)
    except (RuntimeError, ValueError) as error:
        assert any(word in str(error).lower() for word in ("changed", "mismatch", "integrity")), (
            error
        )
    else:
        assert not result["passed"], result
    assert marker.read_text() == "executed"
