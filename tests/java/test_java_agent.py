"""Deterministic agent decisions, real HTTP debugger, javac builds and fresh JVM checks."""

import hashlib
import os
import shutil
import threading
from pathlib import Path

import pytest

from peeky.agent import Investigation
from peeky.client import Client
from peeky.java import JavaTarget
from peeky.java_verification import JavaVerification
from peeky.worker import make_server

ROOT = Path(__file__).resolve().parents[2]
BUG = "int start = page * pageSize;"
FIX = "int start = (page - 1) * pageSize;"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def java_investigation(tmp_path):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a prepared adapter bundle")
    root = tmp_path / "inputs"
    shutil.copytree(ROOT / "examples/java", root)
    source = root / "demo/PaginationBug.java"
    check = root / "verify_pagination.py"
    target = JavaTarget(Path(bundle), ("demo/PaginationBug.java",), "demo.PaginationBug")
    token = "java-agent-tests-token-123456"
    server = make_server(root, token, 0, adapter=target)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_port}", token)
    execution = JavaVerification(target, root)
    observe_line = next(
        i for i, line in enumerate(source.read_text().splitlines(), 1) if "// OBSERVE" in line
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
        "demo/PaginationBug.java",
        source,
        check,
        output,
        decisions,
        max_steps=steps,
        execution=execution,
    )


def test_java_agent_retries_compile_failure_and_overfit_before_general_fix(java_investigation):
    _, _, source, check, line, _ = java_investigation
    original = digest(source), digest(check)
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

    report = make_runner(java_investigation, decide).run("Fix one-based pagination for all pages")
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
    assert "12 independent Java pagination cases" in verification["stdout"]
    assert Path(verification["candidate"]).suffix == ".java"
    assert FIX in Path(verification["candidate"]).read_text()
    assert report["source_unchanged"] and report["check_unchanged"]
    assert (digest(source), digest(check)) == original


def test_java_agent_requires_runtime_intervention(java_investigation):
    runner = make_runner(
        java_investigation,
        lambda _: {"action": "finish", "old": BUG, "new": FIX, "reason": "Skip evidence"},
        steps=1,
    )
    report = runner.run("Fix pagination")
    assert report["status"] == "insufficient_evidence"
    assert "runtime intervention" in report["steps"][0]["error"]
    assert not (java_investigation[-1] / "candidate").exists()


@pytest.mark.parametrize(
    "tamper", ["candidate", "saved-candidate", "checker", "descriptor", "class"]
)
def test_java_verifier_tampering_cannot_be_accepted(java_investigation, tamper):
    _, execution, source, check, _, output = java_investigation
    injection = {
        "candidate": 'Path(spec["source"]["path"]).write_text("changed by checker")',
        "saved-candidate": f"Path({str(output / 'candidate/demo/PaginationBug.java')!r}).write_text('changed by checker')",
        "checker": 'Path(__file__).write_text("changed by checker")',
        "descriptor": 'Path(sys.argv[1]).write_text("{}")',
        "class": 'next(Path(spec["command"][2]).rglob("*.class")).write_bytes(b"changed")',
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
