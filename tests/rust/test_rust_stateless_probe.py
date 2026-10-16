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
def test_rust_stateless_probe_views_intervention_and_cleanup(
    tmp_path, tmp_path_factory, monkeypatch
):
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
    # A private temp root: other Peeky runs on this machine cannot disturb the check.
    # Outside the crate: a temp root inside it would (rightly) trip the ancestor check.
    private = tmp_path_factory.mktemp("rust-tmp")
    monkeypatch.setattr(tempfile, "tempdir", str(private))
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
    assert list(private.glob("peeky-rust-*")) == []


@pytest.mark.timeout(180)
def test_peeky_cli_detects_rust_and_runs_probe_session_and_verify(tmp_path, monkeypatch, capsys):
    import json

    from peeky.cli import main

    names = ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER")
    if not all(os.environ.get("PEEKY_RUST_" + name) for name in names):
        pytest.skip("set PEEKY_RUST_* tool paths to enable Rust CLI conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    crate = tmp_path / "crate"
    for name in (*INPUTS, "verify_pagination.py"):
        (crate / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "examples/rust" / name, crate / name)
    line = next(
        i
        for i, text in enumerate((crate / "src/main.rs").read_text().splitlines(), 1)
        if "// OBSERVE" in text
    )
    assert main(["describe", str(crate), "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["language"] == "rust" and line in manifest["breakpoint_lines"]["src/main.rs"]
    assert manifest["value_views"]["core::option::Option<i32>"] == "option"
    scopes = {scope["scope"]: scope for scope in manifest["scopes"]}
    assert {"start", "request", "some", "none"} <= set(scopes["peeky_pagination::main"]["names"])
    assert scopes["peeky_pagination::main"]["source"] == "src/main.rs"
    assert line in range(
        scopes["peeky_pagination::main"]["lines"][0],
        scopes["peeky_pagination::main"]["lines"][1] + 1,
    )
    assert main(["session", "start", str(crate), "--at", str(line)]) == 0
    # Locations name the user's source, not the private build workspace's copy.
    started = capsys.readouterr().out
    assert f"{(crate / 'src/main.rs').resolve()}:{line}" in started, started
    assert "peeky-build-" not in started
    assert main(["session", "get", "some", "request.page", "--json"]) == 0
    rows = {row["selector"]: row["value"] for row in json.loads(capsys.readouterr().out)}
    assert rows == {"some": "Some(3)", "request.page": "1"}
    assert "peeky-build-" not in (main(["session", "inspect"]), capsys.readouterr().out)[1]
    # Unreadable Rust container elements are explained, not reported as missing.
    assert main(["session", "get", "args[0]"]) == 1
    assert "Rust Vec, slice and String contents are not readable yet" in capsys.readouterr().out
    assert main(["session", "set", "args=1"]) == 2
    assert "'args' has debugger type 'alloc::vec::Vec<" in capsys.readouterr().err
    assert main(["session", "set", "start=0"]) == 0
    assert main(["session", "continue"]) == 0 and main(["session", "report"]) == 0
    assert "PAGE:[10, 20, 30]" in capsys.readouterr().out
    assert main(["session", "close"]) == 0
    check = ["--check", str(crate / "verify_pagination.py")]
    fix = ["--old", "request.page * request.page_size", "--new"]
    assert main(["verify", str(crate), *check, *fix, "(request.page - 1) * request.page_size"]) == 0
    assert main(["verify", str(crate), *check, *fix, "0"]) == 1
