"""Owned Rust Vec and String locals read only where the build's MIR proves them live (#53)."""

import json
import os
import shutil
from pathlib import Path

import pytest

from peeky.cli import main

ROOT = Path(__file__).resolve().parents[2]

PROGRAM = """fn consume(v: Vec<i32>) -> usize {
    v.len()
}

fn deferred(first: bool) -> usize {
    let label: String;
    let n = 1; // BEFORE
    if first {
        label = String::from("first-call-label");
    } else {
        label = String::from("x");
    }
    label.len() + n // AFTER
}

fn main() {
    let dropped = vec![7, 8, 9];
    drop(dropped);
    let reused = vec![1, 2, 3];
    let moved = vec![4, 5];
    let n = consume(moved);
    let maybe = vec![6];
    if n > 100 {
        consume(maybe);
    }
    let mut again = vec![10];
    consume(again);
    again = vec![11, 12];
    let mut source = vec![20, 21];
    let taken = std::mem::take(&mut source);
    let text = String::from("héllo");
    println!("{n} {reused:?} {again:?} {source:?} {taken:?} {text}"); // MAIN
    let mut acc = Vec::new();
    for i in 0..3 {
        acc.push(i); // LOOP
    }
    let s = String::from("first");
    let s = s + "-second";
    println!("{s}"); // SHADOW
    println!("{} {} {:?}", deferred(true), deferred(false), acc);
}
"""


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


def line(marker):
    return next(i for i, text in enumerate(PROGRAM.splitlines(), 1) if f"// {marker}" in text)


def probe(capsys, crate, marker, selectors, *extra):
    at = f"src/main.rs:{line(marker)}"
    code = main(
        ["probe", str(crate), "--at", at, "--capture", selectors, *extra, "--json", "--quiet"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    return result


def memory_views(rows):
    """Whether this debugger reads memory (lldb-dap from LLVM 20); lanes that must
    qualify the memory path set PEEKY_EXPECT_READ_MEMORY."""
    readable = any(row.get("view") == "memory" for row in rows)
    if os.environ.get("PEEKY_EXPECT_READ_MEMORY"):
        assert readable, rows
    return readable


@pytest.mark.timeout(300)
def test_owned_locals_are_read_only_where_they_are_initialized(crate, capsys):
    result = probe(
        capsys, crate, "MAIN", "dropped,reused,moved,maybe,again,source,taken,text,reused[1]"
    )
    rows = {row["selector"]: row for row in result["rows"]}
    # Dropped (its buffer reused by `reused`), moved, moved on one path: never read.
    for name in ("dropped", "moved", "maybe"):
        assert rows[name].get("view") != "memory", rows[name]
        assert "(#53)" in rows[name]["note"], rows[name]
    if memory_views(result["rows"]):
        value = {name: row["value"] for name, row in rows.items()}
        assert value["reused"] == "[1, 2, 3]" and value["reused[1]"] == "2"
        # Moved and then assigned again; emptied by mem::take; its replacement.
        assert (value["again"], value["source"], value["taken"]) == ("[11, 12]", "[]", "[20, 21]")
        assert value["text"] == '"héllo"'
    assert "2 [1, 2, 3] [11, 12] [] [20, 21] héllo" in result["output"]


@pytest.mark.timeout(300)
def test_a_vector_pushed_in_a_loop_is_read_at_each_hit(crate, capsys):
    result = probe(capsys, crate, "LOOP", "acc,i", "--hits", "all")
    acc = [row for row in result["rows"] if row["selector"] == "acc"]
    assert len(acc) == 3
    if memory_views(result["rows"]):
        assert [row["value"] for row in acc] == ["[]", "[0]", "[0, 1]"]


@pytest.mark.timeout(300)
def test_a_deferred_local_is_read_only_after_its_assignment(crate, capsys):
    before = probe(capsys, crate, "BEFORE", "label,n", "--hits", "all")
    labels = [row for row in before["rows"] if row["selector"] == "label"]
    # Not yet assigned: on the second call it still holds the first call's freed header.
    assert len(labels) == 2 and all(row.get("view") != "memory" for row in labels), labels
    after = probe(capsys, crate, "AFTER", "label", "--hits", "all")
    if memory_views(after["rows"]):
        assert [row["value"] for row in after["rows"]] == ['"first-call-label"', '"x"']


@pytest.mark.timeout(300)
def test_a_shadowed_string_is_read_only_through_its_live_binding(crate, capsys):
    assert main(["session", "start", str(crate), "--at", f"src/main.rs:{line('SHADOW')}"]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "inspect", "--json"]) == 0
        snapshot = json.loads(capsys.readouterr().out)
    finally:
        main(["session", "close"])
    variables = {v["name"]: v for scope in snapshot["scopes"] for v in scope["variables"]}
    first, second = (f"s @ main.rs:{line('SHADOW') - n}" for n in (2, 1))
    # The first `s` was moved into `+`; its buffer now belongs to the second.
    assert variables[first].get("view") != "memory", variables[first]
    if os.environ.get("PEEKY_EXPECT_READ_MEMORY") or variables[second].get("view") == "memory":
        assert variables[second]["value"] == '"first-second"'
