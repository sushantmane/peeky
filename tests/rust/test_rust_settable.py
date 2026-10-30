"""Which locals `--set` can change, before and at the refusal."""

import json
import os
import shutil
from pathlib import Path

import pytest

from peeky.cli import main

ROOT = Path(__file__).resolve().parents[2]
PROGRAM = 'fn histogram(values: &[f64], buckets: usize) -> Vec<usize> {\n    let mut counts = vec![0usize; buckets];\n    for &x in values {\n        let idx = (x as usize).min(buckets);\n        if idx < counts.len() { // HERE\n            counts[idx] += 1;\n        }\n    }\n    let mut total: usize = 0;\n    total += counts.len();\n    println!("{total}");\n    counts\n}\n\nfn main() {\n    println!("{:?}", histogram(&[1.0, 3.0, 5.0], 4));\n}\n'


@pytest.fixture
def crate(tmp_path, monkeypatch):
    names = ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER")
    if not all(os.environ.get("PEEKY_RUST_" + name) for name in names):
        pytest.skip("set PEEKY_RUST_* tool paths to enable Rust conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "crate"
    shutil.copytree(ROOT / "examples/rust", root, ignore=shutil.ignore_patterns("target"))
    (root / "src/main.rs").write_text(PROGRAM)
    return root


def probe(capsys, crate, *argv):
    code = main(["probe", str(crate), "--at", "src/main.rs:5", *argv, "--json", "--quiet"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(300)
def test_rows_say_whether_set_can_change_a_local_and_a_refusal_lists_those_it_can(crate, capsys):
    # `--set idx=4 --set-hit 4` ran to hit 4 and was refused; nothing said
    # beforehand that no local in that function could be changed.
    code, result = probe(capsys, crate, "--capture", "idx,buckets,x,counts.len")
    assert code == 0, result.get("error")
    rows = {row["selector"]: row for row in result["rows"]}
    assert all(type(rows[name].get("settable")) is bool for name in ("idx", "buckets", "x"))
    # A field of a std type, raw (lldb-dap 18) or decoded from memory: never changed, and
    # said so: fields carry `settable` too.
    assert rows["counts.len"]["settable"] is False, rows["counts.len"]
    code, refused = probe(capsys, crate, "--capture", "idx", "--set", "counts.len=1")
    # Refused before the run, a refusal exits 2.
    assert code == (2 if refused.get("refused_before_launch") else 1), refused
    assert refused["error"]["code"] == "MUTATION_REJECTED", refused
    code, result = probe(capsys, crate, "--capture", "idx", "--set", "idx=0")
    if code != 0:
        assert code == (2 if result.get("refused_before_launch") else 1), result
        assert result["error"]["code"] == "MUTATION_REJECTED"
        listed = [name for name, row in rows.items() if row.get("settable")]
        expected = "settable here: " + (", ".join(listed) if listed else "none")
        if result.get("refused_before_launch"):
            # Refused before the run, from what the build keeps in memory there.
            expected = (
                f"settable here: {', '.join(listed)} (root locals; fields too where rows "
                "mark them [settable])"
                if listed
                else "settable here: no root locals (fields where rows mark them [settable])"
            )
        assert result["error"]["message"].endswith(expected), result["error"]["message"]
    else:
        assert rows["idx"]["settable"] is True
