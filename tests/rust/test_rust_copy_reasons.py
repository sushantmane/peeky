"""`--set request.page=2` at examples/rust's line 36 was refused with two hedged reasons
joined in one sentence (a statement under way may hold a copy, or the program's own `start`
may), and a hint ("Observe the value instead…") that contradicted the message's own advice
("Change it before the program copies it"). `start` holds what line 35 computed from it at
every place the line can stop at: that is the one reason, and the hint agrees."""

import json
import os
import shutil
from pathlib import Path

import pytest

from peeky.cli import main

ROOT = Path(__file__).resolve().parents[2]
INPUTS = ("Cargo.toml", "Cargo.lock", "src/main.rs", "tests/pagination.rs")
HINT = (
    "Change it before the program copies it (a stop on an earlier line, or `--set-at` there), "
    "or change the copy the message names instead."
)


@pytest.fixture
def crate(tmp_path, monkeypatch):
    names = ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER")
    if not all(os.environ.get("PEEKY_RUST_" + name) for name in names):
        pytest.skip("set PEEKY_RUST_* tool paths to enable Rust conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "pagination"
    for name in INPUTS:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "examples/rust" / name, root / name)
    return root


def probe(capsys, crate, *arguments):
    code = main(["probe", str(crate), "--at", "36", *arguments, "--quiet", "--json"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(400)
def test_a_copy_the_program_holds_at_every_stop_is_the_refusals_one_reason(crate, capsys):
    code, result = probe(capsys, crate, "--capture", "request.page", "--set", "request.page=2")
    error = result["error"]
    assert code != 0 and error["code"] == "MUTATION_REJECTED", result
    message = error["message"]
    # The rule, then the one reason, then what to do.
    assert message.startswith(
        "`request.page` cannot be changed at src/main.rs:36, at any stop of this build: in "
        "peeky_pagination::main, at every place line 36 can stop at, the program's own "
        "`start` (at line 35) may hold a copy of `request` that it goes on with, so a change "
        "there may never be seen: "
    ), message
    assert "statement under way" not in message and "may not reach it" not in message, message
    assert "Change it before the program copies it (`--set-at` an earlier line)" in message
    # The hint agrees with that advice.
    assert error["hint"] == HINT and len(HINT) <= 150, error
    # And the advice works: set before line 35 copies it, `start` follows.
    code, result = probe(
        capsys, crate, "--capture", "request.page,start", "--set-at", "35", "request.page=2"
    )
    assert code == 0, result.get("error")
    rows = {row["selector"]: row.get("value") for row in result["rows"]}
    assert rows == {"request.page": "2", "start": "6"}, result["rows"]
