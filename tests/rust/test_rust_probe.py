"""Engineering toolchain gate; does not advertise a Rust worker adapter."""

import json
import os
import sys
from pathlib import Path

import pytest

from peeky.agent import execute

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.timeout(120)
def test_pinned_rust_raw_debugger_gate(tmp_path):
    names = ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER")
    if not all(os.environ.get("PEEKY_RUST_" + name) for name in names):
        pytest.skip("set PEEKY_RUST_* tool paths to enable engineering Rust qualification")
    report_path = tmp_path / "rust-probe.json"
    command = [sys.executable, str(ROOT / "scripts/probe_rust.py"), "--output", str(report_path)]
    for name in names:
        command += [
            "--" + ("adapter" if name == "DAP" else name.lower()),
            os.environ["PEEKY_RUST_" + name],
        ]
    for name in ("DSYMUTIL", "SYSROOT"):
        if value := os.environ.get("PEEKY_RUST_" + name):
            command += ["--" + name.lower(), value]
    result = execute(command, ROOT, timeout=110)
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    assert result["exit_code"] == 0, (result, report.get("error"), report.get("frame"))
    assert report["passed"] and report["unchanged"] and report["temporary_build_removed"]
    assert report["qualification_only"] and report["raw_shape_qualified"]
    assert report["module_verified"] and report["debuggee_pids_reaped"]
    assert report["rust_version_pin"] == "1.96.0"
    assert report["raw_children"]["request"]
    assert set(report["raw_children"]) == {"request", "mode", "some", "none"}
