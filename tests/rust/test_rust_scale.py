"""A larger Rust crate: 607 lines using HashMap,
BTreeMap, BTreeSet, VecDeque and BinaryHeap (examples/large/rust-shop), whose own unit is
33 MB of llvm-dwarfdump text. Peeky reads it as it is written."""

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
    root = tmp_path / "shop"
    shutil.copytree(
        ROOT / "examples/large/rust-shop", root, ignore=shutil.ignore_patterns("target")
    )
    return root


def run(capsys, *arguments):
    code = main([*arguments, "--json"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(300)
def test_a_crate_using_btreemap_is_probed_and_changed(crate, capsys):
    # Every probe of it was a DEBUGGER_ERROR: "Native symbol command failed or exceeded its
    # budget", at 16 MiB of its unit's text.
    source = crate / "src/pricing.rs"
    line = next(i for i, text in enumerate(source.read_text().splitlines(), 1) if "OBSERVE" in text)
    code, result = run(
        capsys,
        "probe",
        str(crate),
        "--at",
        f"src/pricing.rs:{line}",
        "--capture",
        "tier_discount,coupon_discount,quote.subtotal",
        "--set",
        "coupon_discount=5000",
        "--quiet",
        "--no-save",
    )
    assert code == 0, result.get("error")
    rows = [(row["selector"], row["value"], row.get("note")) for row in result["rows"]]
    assert rows[:3] == [
        ("tier_discount", "648", None),
        ("coupon_discount", "0", None),
        ("quote.subtotal", "16200", None),
    ]
    assert result["set_outcomes"][0]["ended"] == "made"
    assert "order 1001: $162.00 - $50.00 + $0.00 = $112.00" in result["output"]
    code, described = run(capsys, "describe", str(crate))
    assert code == 0, described
    assert "scopes_unavailable" not in described, described["scopes_unavailable"]
    scopes = {scope["scope"] for scope in described["scopes"]}
    assert "shop::pricing::Pricing::quote" in scopes, sorted(scopes)


@pytest.mark.timeout(300)
def test_a_unit_over_peekys_limit_is_a_budget_refusal_that_says_what_to_do(
    tmp_path, monkeypatch, capsys
):
    names = ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER")
    if not all(os.environ.get("PEEKY_RUST_" + name) for name in names):
        pytest.skip("set PEEKY_RUST_* tool paths to enable Rust conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "crate"
    shutil.copytree(ROOT / "examples/rust", root, ignore=shutil.ignore_patterns("target"))
    monkeypatch.setattr("peeky.rust_symbols.MAX_DIES", 50)
    code, result = run(
        capsys, "probe", str(root), "--at", "src/main.rs:36", "--capture", "start", "--quiet"
    )
    assert code == 2 and result["error"]["code"] == "BUDGET_EXHAUSTED", result
    assert result["error"]["message"] == (
        "Peeky reads at most 50 entries of a build's debug information, and this build has "
        "more: probe a smaller program (fewer source files, standard headers or std "
        "collections)."
    )
