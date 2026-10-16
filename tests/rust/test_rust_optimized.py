"""#13: optimized Rust builds are opt-in and every result says values may be wrong.

Observed with Rust 1.96 at opt-level 2: `start` read 1 at the OBSERVE line where the
unoptimized build (and the program's logic) gives 3; LLDB does not flag it.
"""

import os
import shutil
from pathlib import Path

import pytest

from peeky.probe import Probe, run_probe

ROOT = Path(__file__).resolve().parents[2]
INPUTS = ("Cargo.toml", "Cargo.lock", "src/main.rs", "tests/pagination.rs")


@pytest.mark.timeout(240)
def test_optimized_rust_results_carry_an_explicit_warning(tmp_path):
    names = ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER")
    if not all(os.environ.get("PEEKY_RUST_" + name) for name in names):
        pytest.skip("set PEEKY_RUST_* tool paths to enable Rust conformance")
    from peeky.rust import RustTarget

    for name in INPUTS:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "examples/rust" / name, tmp_path / name)
    line = next(
        i
        for i, text in enumerate((tmp_path / "src/main.rs").read_text().splitlines(), 1)
        if "// OBSERVE" in text
    )

    def target(level):
        return RustTarget(
            sources=("src/main.rs",),
            tests=("tests/pagination.rs",),
            cargo=os.environ["PEEKY_RUST_CARGO"],
            rustc=os.environ["PEEKY_RUST_RUSTC"],
            adapter=os.environ["PEEKY_RUST_DAP"],
            dwarfdump=os.environ["PEEKY_RUST_DWARFDUMP"],
            linker=os.environ["PEEKY_RUST_LINKER"],
            dsymutil=os.environ.get("PEEKY_RUST_DSYMUTIL"),
            sysroot=os.environ.get("PEEKY_RUST_SYSROOT"),
            opt_level=level,
        )

    debug = run_probe(tmp_path, Probe("src/main.rs", line, ("start",)), adapter=target("0"))
    assert debug["status"] == "completed", debug.get("error")
    assert "optimized" not in debug and debug["rows"][0]["value"] == "3"
    optimized = run_probe(tmp_path, Probe("src/main.rs", line, ("start",)), adapter=target("2"))
    assert optimized["status"] == "completed", optimized.get("error")
    assert optimized["optimized"] == "opt-level=2"
    assert optimized["target_identity"]["profile"]["CARGO_PROFILE_DEV_OPT_LEVEL"] == "2"
    assert any("can still be stale or wrong" in w for w in optimized["warnings"])
    with pytest.raises(ValueError, match="opt_level"):
        target("3")
