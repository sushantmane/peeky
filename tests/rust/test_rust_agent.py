"""Deterministic agent decisions, real HTTP debugger, Cargo builds, compiled tests and checker."""

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
from peeky.worker import make_server

ROOT = Path(__file__).resolve().parents[2]
BUG = "let mut start: i32 = request.page * request.page_size;"
FIX = "let mut start: i32 = (request.page - 1) * request.page_size;"
INPUTS = ("Cargo.toml", "Cargo.lock", "src/main.rs", "tests/pagination.rs", "verify_pagination.py")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def rust_investigation(tmp_path):
    names = ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER")
    if not all(os.environ.get("PEEKY_RUST_" + name) for name in names):
        pytest.skip("set PEEKY_RUST_* tool paths to enable Rust agent conformance")
    from peeky.rust import RustTarget, RustVerification

    root, worker_root = tmp_path / "inputs", tmp_path / "worker-inputs"
    for base in (root, worker_root):
        for name in INPUTS:
            (base / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / "examples/rust" / name, base / name)
    target = RustTarget(
        sources=("src/main.rs",),
        tests=("tests/pagination.rs",),
        cargo=os.environ["PEEKY_RUST_CARGO"],
        rustc=os.environ["PEEKY_RUST_RUSTC"],
        adapter=os.environ["PEEKY_RUST_DAP"],
        dwarfdump=os.environ["PEEKY_RUST_DWARFDUMP"],
        linker=os.environ["PEEKY_RUST_LINKER"],
        dsymutil=os.environ.get("PEEKY_RUST_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_RUST_SYSROOT"),
    )
    token = "rust-agent-tests-token-1234567"
    server = make_server(worker_root, token, 0, adapter=target)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_port}", token)
    source = root / "src/main.rs"
    line = next(
        i for i, text in enumerate(source.read_text().splitlines(), 1) if "// OBSERVE" in text
    )
    try:
        yield {
            "client": client,
            "execution": RustVerification(target, root),
            "root": root,
            "source": source,
            "check": root / "verify_pagination.py",
            "line": line,
            "output": tmp_path / "report",
        }
    finally:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def actions(line, finishes):
    return iter(
        [
            {"action": "observe", "line": line, "reason": "Inspect the computed first-page start"},
            {
                "action": "intervene",
                "variable": "start",
                "value": "0",
                "reason": "Test the one-based page hypothesis",
            },
            *[
                {"action": "finish", "old": BUG, "new": new, "reason": reason}
                for new, reason in finishes
            ],
        ]
    )


@pytest.mark.timeout(240)
def test_rust_agent_rejects_compile_failure_and_test_passing_overfit_before_general_fix(
    rust_investigation, monkeypatch
):
    data = rust_investigation
    originals = {name: digest(data["root"] / name) for name in INPUTS}
    sequence = actions(
        data["line"],
        [
            ("let mut start: i32 = ;", "Deliberate compile failure must be returned for retry"),
            ("let mut start: i32 = 0;", "First-page overfit passes the Cargo test only"),
            (FIX, "General one-based pagination fix"),
        ],
    )

    def decide(context):
        assert context["language"] == "rust"
        if len(context["steps"]) == 1:
            variables = {
                v["name"]: v
                for scope in context["steps"][0]["result"]["snapshot"]["scopes"]
                for v in scope["variables"]
            }
            assert variables["start"]["value"] == "3"
            assert variables["some"]["value"] == "Some(3)"
            assert variables["none"]["value"] == "None"
        return next(sequence)

    config = data["output"].parent / "rust-target.json"
    config.write_text(json.dumps(asdict(data["execution"].target)))
    real_which = shutil.which
    monkeypatch.setattr(
        agent_module.shutil,
        "which",
        lambda name, *a, **kw: "/fixture/codex" if name == "codex" else real_which(name, *a, **kw),
    )
    monkeypatch.setattr(agent_module, "CodexDecision", lambda *a, **kw: decide)
    monkeypatch.setenv("DEBUG_TOKEN", data["client"].token)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "peeky-agent",
            "--root",
            str(data["root"]),
            "--rust-config",
            str(config),
            "--program",
            "src/main.rs",
            "--check",
            "verify_pagination.py",
            "--model",
            "fixture-model",
            "--max-steps",
            "6",
            "--url",
            data["client"].url,
            "--output",
            str(data["output"]),
        ],
    )
    agent_module.main()
    report = json.loads((data["output"] / "report.json").read_text())
    assert report["status"] == "verified"
    baseline = report["baseline_check"]
    assert baseline["tests"]["passed"] is False and baseline["tests"]["summary"]["failed"] == 1
    assert "PAGE:[10, 20, 30]" in report["steps"][1]["result"]["output"]
    compile_failure = report["steps"][2]["result"]
    assert compile_failure["stage"] == "build" and not compile_failure["passed"]
    overfit = report["steps"][3]["result"]
    assert overfit["tests"]["passed"] is True, overfit["tests"]
    assert overfit["exit_code"] != 0 and overfit["passed"] is False
    verification = report["verification"]
    assert verification["passed"] and verification["exit_code"] == 0
    assert verification["tests"]["passed"] and verification["tests"]["summary"]["passed"] == 1
    assert verification["tests"]["stage"] == "cargo-test"
    assert "12 independent Rust pagination cases" in verification["stdout"]
    assert verification["artifacts_retained"] is False
    assert not Path(verification["descriptor"]["command"][0]).exists()
    assert not Path(verification["tests"]["command"][0]).exists()
    candidate = Path(verification["candidate"])
    assert candidate.as_posix().endswith("candidate/src/main.rs") and FIX in candidate.read_text()
    assert report["source_unchanged"] and report["check_unchanged"]
    assert {name: digest(data["root"] / name) for name in INPUTS} == originals


@pytest.mark.timeout(240)
def test_rust_agent_rejects_frozen_test_drift(rust_investigation):
    data = rust_investigation
    sequence = actions(data["line"], [(FIX, "General fix")])
    test_file = data["root"] / "tests/pagination.rs"

    def decide(context):
        decision = next(sequence)
        if decision["action"] == "finish":
            test_file.write_text(test_file.read_text().replace("[10, 20, 30]", "[40, 50, 60]"))
        return decision

    runner = Investigation(
        data["client"],
        "src/main.rs",
        data["source"],
        data["check"],
        data["output"],
        decide,
        max_steps=4,
        execution=data["execution"],
    )
    with pytest.raises(RuntimeError, match="changed"):
        runner.run("Fix pagination")
    report = json.loads((data["output"] / "report.json").read_text())
    assert report["status"] == "failed" and "verification" not in report
