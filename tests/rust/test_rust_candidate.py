"""Probing a candidate fix (`probe --old/--new`) on a real Rust build."""

import json
import os
import shutil
from pathlib import Path

import pytest

from peeky.cli import main

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def crate(tmp_path, monkeypatch):
    names = ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER")
    if not all(os.environ.get("PEEKY_RUST_" + name) for name in names):
        pytest.skip("set PEEKY_RUST_* tool paths to enable Rust conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "crate"
    shutil.copytree(ROOT / "examples/rust", root, ignore=shutil.ignore_patterns("target"))
    return root


@pytest.mark.timeout(300)
def test_a_rust_candidate_is_built_and_probed_without_touching_the_crate(crate, capsys):
    source = crate / "src/main.rs"
    before = source.read_bytes()
    line = next(i for i, text in enumerate(source.read_text().splitlines(), 1) if "OBSERVE" in text)
    common = ["probe", str(crate), "--at", f"src/main.rs:{line}", "--capture", "start"]
    assert main([*common, "--json", "--quiet", "--no-save"]) == 0
    original = json.loads(capsys.readouterr().out)
    code = main(
        [
            *common,
            "--old",
            "request.page * request.page_size;",
            "--new",
            "(request.page - 1) * request.page_size;",
            "--json",
            "--quiet",
            "--no-save",
        ]
    )
    candidate = json.loads(capsys.readouterr().out)
    assert code == 0, candidate.get("error")
    assert original["rows"][0]["value"] == "3" and candidate["rows"][0]["value"] == "0"
    assert "PAGE:[10, 20, 30]" in candidate["output"]
    assert "PAGE:[40, 50, 60]" in original["output"]
    assert candidate["candidate"]["line"] == line and source.read_bytes() == before
