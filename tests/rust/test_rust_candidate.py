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


@pytest.mark.timeout(300)
def test_a_candidate_with_many_compile_errors_is_build_failed_with_rustcs_words(crate, capsys):
    # Several errors and warnings pass 16 KB of Cargo JSON, which is no truncated
    # diagnostic: BUILD_FAILED with the compiler's output, not DEBUGGER_ERROR.
    source = crate / "src/main.rs"
    line = next(i for i, text in enumerate(source.read_text().splitlines(), 1) if "OBSERVE" in text)
    broken = "".join(f"let unused_{n} = 0usize; let bad_{n}: i64 = unused_{n};\n" for n in range(8))
    code = main(
        [
            "probe",
            str(crate),
            "--at",
            f"src/main.rs:{line}",
            "--capture",
            "start",
            "--old",
            "fn main() {",
            "--new",
            "fn main() {\n" + broken,
            "--json",
            "--quiet",
            "--no-save",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    # Refused before the launch request: exit 2, no table.
    assert code == 2 and result["error"]["code"] == "BUILD_FAILED", result["error"]
    assert result["refused_before_launch"] is True
    message = result["error"]["message"]
    assert "error[E0308]: mismatched types" in message and "Compiling" not in message


@pytest.mark.timeout(300)
def test_a_program_using_std_collections_is_debuggable(crate, capsys):
    # Its own DWARF is megabytes of monomorphized HashMap types (4.4 MB here); a 2 MiB
    # cap made every probe of it a DEBUGGER_ERROR.
    (crate / "src/main.rs").write_text(
        "use std::collections::{BTreeMap, HashMap, HashSet};\n"
        "fn main() {\n"
        "    let mut words: HashMap<String, usize> = HashMap::new();\n"
        "    for w in \"a b a c\".split(' ') {\n"
        "        *words.entry(w.to_string()).or_default() += 1;\n"
        "    }\n"
        "    let sorted: BTreeMap<_, _> = words.iter().collect();\n"
        '    let seen: HashSet<&str> = ["a"].into_iter().collect();\n'
        "    let total = words.len() + seen.len();\n"
        '    println!("{sorted:?} {total}"); // HERE\n'
        "}\n"
    )
    code = main(
        ["probe", str(crate), "--at", "src/main.rs:10", "--capture", "total", "--json", "--quiet"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert result["rows"][0]["value"] == "4" and '{"a": 2, "b": 1, "c": 1} 4' in result["output"]
