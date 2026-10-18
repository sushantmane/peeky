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


FFI_FREED = """extern "C" {
    fn getpid() -> i32;
}

#[allow(improper_ctypes_definitions)]
extern "C" fn consume(v: Vec<i32>) {
    drop(v);
    std::mem::forget(vec![7, 7, 7]);
}

fn main() {
    let v = vec![1, 2, 3];
    let pid = unsafe { getpid() };
    consume(v);
    let n = pid; // FFI_FREED
    println!("{n}");
}
"""

FFI_MOVED = """extern "C" {
    fn getpid() -> i32;
}

fn main() {
    let v = vec![1, 2, 3];
    let pid = unsafe { getpid() };
    let w = v;
    let n = pid; // FFI_MOVED
    println!("{n} {w:?}");
}
"""

SAME_NAME = """#[inline(always)]
fn tick() {}

fn consume(v: Vec<i32>) -> usize {
    let n = v.len();
    drop(v);
    std::mem::forget(vec![7, 7, 7]);
    n
}

struct A {
    n: usize,
}

struct B {
    n: usize,
}

impl A {
    fn new() -> A {
        let v = vec![1, 2, 3];
        tick();
        let n = consume(v);
        let m = n; // SAME
        A { n: m }
    }
}

impl B {
    fn new(k: usize) -> B {
        let v = vec![4, 5, 6];
        let a = k + 1;
        let b = a + 1;
        let c = b + 1;
        let d = c + 1;
        let e = d + 1;
        let n = e + 1;
        let w = &v;
        B { n: n + 1 }
    }
}

fn main() {
    let a = A::new();
    let b = B::new(a.n);
    println!("{} {}", a.n, b.n);
}
"""


@pytest.mark.parametrize(
    ("program", "marker", "selectors"),
    [(FFI_FREED, "FFI_FREED", "v"), (FFI_MOVED, "FFI_MOVED", "v"), (SAME_NAME, "SAME", "v")],
    ids=["freed-after-a-call-that-cannot-unwind", "moved-after-it", "same-named-method"],
)
@pytest.mark.timeout(300)
def test_review_false_proofs_stay_refused(crate, capsys, program, marker, selectors):
    (crate / "src/main.rs").write_text(program)
    at = next(i for i, text in enumerate(program.splitlines(), 1) if f"// {marker}" in text)
    code = main(
        ["probe", str(crate), "--at", f"src/main.rs:{at}", "--capture", selectors, "--json"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    # Moved (or moved and freed, its buffer reused by [7, 7, 7]): never read. Before the
    # fix these read [7, 7, 7] or [1, 2, 3] from memory.
    [row] = result["rows"]
    assert row["selector"] == "v" and row.get("view") != "memory", row


MACRO_USER = """#[macro_use]
mod macros;
fn tick() {}
fn main() {
    let v = vec![1, 2, 3];
    let k = v.len();
    let n = eat!(v);
    println!("{k} {n}");
}
"""

MACROS = """macro_rules! eat {
    ($v:expr) => {{
        tick();
        drop($v);
        std::mem::forget(vec![7, 7, 7]);
        let n = std::hint::black_box(1); // IN_MACRO
        n
    }};
}
"""

MACRO_ABOVE = """macro_rules! twice {
    ($e:expr) => {
        $e * 2
    };
}

fn main() {
    let v = vec![1, 2, 3];
    let n = twice!(v.len());
    let m = n + 1; // BELOW
    println!("{v:?} {m}");
}
"""


@pytest.mark.timeout(300)
def test_a_stop_in_a_macro_from_another_file_is_never_proven_with_this_files_lines(crate, capsys):
    (crate / "src/main.rs").write_text(MACRO_USER)
    (crate / "src/macros.rs").write_text(MACROS)
    at = next(i for i, text in enumerate(MACROS.splitlines(), 1) if "IN_MACRO" in text)
    code = main(["probe", str(crate), "--at", f"src/macros.rs:{at}", "--capture", "v,k", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    rows = {row["selector"]: row for row in result["rows"]}
    # Dropped on the macro's line 4, its buffer reused: main.rs's line 6 says nothing.
    assert rows["v"].get("view") != "memory", rows["v"]


@pytest.mark.timeout(300)
def test_a_macro_defined_above_the_function_does_not_hide_its_proofs(crate, capsys):
    (crate / "src/main.rs").write_text(MACRO_ABOVE)
    at = next(i for i, text in enumerate(MACRO_ABOVE.splitlines(), 1) if "BELOW" in text)
    code = main(["probe", str(crate), "--at", f"src/main.rs:{at}", "--capture", "v", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    if memory_views(result["rows"]):
        assert result["rows"][0]["value"] == "[1, 2, 3]"


MACRO_TWIN_METHODS = """macro_rules! method {
    ($($b:tt)*) => {
        fn run() { $($b)* }
    };
}

struct A;
struct B;

impl A {
    method! {
        let v = vec![1, 2, 3];
        let mut u = v;
        u.clear();
        u.shrink_to_fit();
        let w = vec![7, 7, 7];
        let k = w.len(); // STOP
        println!("a");
    }
}

impl B {
    method! {
        let x = vec![4, 5, 6];
        let mut u = x;
        u.clear();
        u.shrink_to_fit();
        let v = vec![7, 7, 7];
        let k = v.len();
        println!("b");
    }
}

fn main() {
    A::run();
    B::run();
}
"""

MACRO_TWIN_CLOSURES = """macro_rules! scoped {
    ($($b:tt)*) => {
        (|| { $($b)* })()
    };
}

mod a {
    pub fn work() {
        scoped! {
            let v = vec![1, 2, 3];
            let mut u = v;
            u.clear();
            u.shrink_to_fit();
            let w = vec![7, 7, 7];
            let k = w.len(); // STOP
            println!("a");
        }
    }
}

mod b {
    pub fn work() {
        scoped! {
            let x = vec![4, 5, 6];
            let mut u = x;
            u.clear();
            u.shrink_to_fit();
            let v = vec![7, 7, 7];
            let k = v.len();
            println!("b");
        }
    }
}

fn main() {
    a::work();
    b::work();
}
"""

WRAPPER_MACRO = """macro_rules! say {
    ($($t:tt)*) => {
        println!($($t)*)
    };
}

fn main() {
    let v = vec![1, 2, 3];
    say!("start");
    let m = v.len() + 1; // BELOW
    println!("{v:?} {m}");
}
"""


@pytest.mark.parametrize(
    "program", [MACRO_TWIN_METHODS, MACRO_TWIN_CLOSURES], ids=["methods", "closures"]
)
@pytest.mark.timeout(300)
def test_functions_a_macro_generates_twice_are_never_proven_with_each_other(crate, capsys, program):
    (crate / "src/main.rs").write_text(program)
    at = next(i for i, text in enumerate(program.splitlines(), 1) if "// STOP" in text)
    code = main(["probe", str(crate), "--at", f"src/main.rs:{at}", "--capture", "v", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    # `v` was moved into `u` and freed by shrink_to_fit; its buffer went to `w`.
    assert result["rows"][0].get("view") != "memory", result["rows"][0]


@pytest.mark.timeout(300)
def test_a_wrapper_macro_around_println_keeps_the_functions_proofs(crate, capsys):
    (crate / "src/main.rs").write_text(WRAPPER_MACRO)
    at = next(i for i, text in enumerate(WRAPPER_MACRO.splitlines(), 1) if "BELOW" in text)
    code = main(["probe", str(crate), "--at", f"src/main.rs:{at}", "--capture", "v", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    if memory_views(result["rows"]):
        assert result["rows"][0]["value"] == "[1, 2, 3]"
