"""One-request stateless Rust probe through the guarded worker (ADR 0004)."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from peeky.probe import Probe, run_probe

ROOT = Path(__file__).resolve().parents[2]
INPUTS = ("Cargo.toml", "Cargo.lock", "src/main.rs", "tests/pagination.rs")


@pytest.mark.timeout(120)
def test_rust_stateless_probe_views_intervention_and_cleanup(tmp_path):
    names = ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER")
    if not all(os.environ.get("PEEKY_RUST_" + name) for name in names):
        pytest.skip("set PEEKY_RUST_* tool paths to enable Rust probe conformance")
    from peeky.rust import RustTarget

    for name in INPUTS:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "examples/rust" / name, tmp_path / name)
    line = next(
        i
        for i, text in enumerate((tmp_path / "src/main.rs").read_text().splitlines(), 1)
        if "// OBSERVE" in text
    )
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
    before = set(Path(tempfile.gettempdir()).glob("peeky-rust-*"))
    result = run_probe(
        tmp_path,
        Probe(
            "src/main.rs",
            line,
            ("start", "some", "none", "mode", "request.page", "request.page_size"),
            (("start", "0"),),
        ),
        adapter=target,
    )
    assert result["status"] == "completed", result.get("error")
    rows = {(r["phase"], r["selector"]): r for r in result["rows"]}
    assert rows["observed", "start"]["value"] == "3"
    assert rows["observed", "some"]["value"] == "Some(3)"
    assert rows["observed", "some"]["view"] == "active-variant"
    assert rows["observed", "none"]["value"] == "None"
    assert rows["observed", "mode"]["value"] == "First"
    assert rows["observed", "request.page"]["value"] == "1"
    assert rows["after-set", "start"]["value"] == "0"
    assert "PAGE:[10, 20, 30]" in result["output"]
    assert result["target_identity"]["language"] == "rust" and result["artifact_manifest_id"]
    assert set(Path(tempfile.gettempdir()).glob("peeky-rust-*")) == before
