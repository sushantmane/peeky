"""An analysed package of edition 2018 in a workspace's build (ADR 0008, phase 2), where 2018
reads otherwise than 2021: a closure captures whole variables, `array.into_iter()` iterates
by reference, `panic!` takes its payload and a lone literal as they are, and Cargo's resolver
1 unifies a dev-dependency's features into the build. Peeky reads the MIR, which rustc
lowers by the crate's edition, so its proofs hold as they are. Every value read matches the
program's own print of it, and every change Peeky makes is seen in its output."""

import hashlib
import json
import os
import subprocess

import pytest

from peeky import rust_workspace as rw
from peeky.cli import main

UTIL = """pub struct Pair {
    pub a: u32,
    pub b: u32,
}

pub struct Named {
    pub a: u32,
    pub name: String,
}

/// 2018: the closure captures all of `p`, by reference (2021: `p.a` alone).
pub fn captured(arg: u32) -> u32 {
    let mut p = Pair { a: std::hint::black_box(arg), b: std::hint::black_box(2) };
    let f = || p.a + 1;
    let k = std::hint::black_box(1); // CAPTURED
    let r = f();
    p.b += 1;
    k + r + p.b
}

/// 2018: the closure captures all of `p`, by `&mut` (2021: `p.a` alone).
pub fn mutated(arg: u32) -> u32 {
    let mut p = Pair { a: std::hint::black_box(arg), b: std::hint::black_box(4) };
    let mut f = || p.a += 1;
    let k = std::hint::black_box(1); // MUTATED
    f();
    k + p.a + p.b
}

/// 2018: the closure's body names all of `p`, its capture (2021: `p__a`).
pub fn inside(arg: u32) -> u32 {
    let p = Pair { a: std::hint::black_box(arg), b: std::hint::black_box(7) };
    let f = |n: u32| {
        let r = p.a + n; // INSIDE
        r
    };
    f(1) + p.b
}

/// 2018: a `move` closure moves all of `p` (2021: `p.a` and `p.name` alone).
pub fn moved(arg: u32) -> u32 {
    let p = Named { a: std::hint::black_box(arg), name: String::from("abc") };
    let f = move || p.a + p.name.len() as u32;
    let k = std::hint::black_box(1); // MOVED
    k + f()
}

/// 2018: `arr.into_iter()` iterates by reference, `v` a `&u32` (2021: by value).
pub fn summed(arg: u32) -> u32 {
    let arr = [std::hint::black_box(arg), 2, 3];
    let mut total = 0;
    for v in arr.into_iter() {
        total += v; // SUMMED
    }
    total
}

/// 2018: the iterator borrows `arr` (2021: it holds a copy of it).
pub fn borrowed(arg: u32) -> u32 {
    let arr = [std::hint::black_box(arg), 2, 3];
    let it = arr.into_iter();
    let k = std::hint::black_box(1); // BORROWED
    let mut total = 0;
    for v in it {
        total += v;
    }
    total + k
}

/// 2018: `panic!(x)` panics with `x` itself as its payload.
pub fn checked(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    let k = std::hint::black_box(1); // CHECKED
    std::hint::black_box(&x);
    if x > 1000 {
        panic!(x);
    }
    k + x
}

/// 2018: `panic!("{}")` panics with the literal as it is.
pub fn literal(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    let k = std::hint::black_box(1); // LITERAL
    std::hint::black_box(&x);
    if x == 999 {
        panic!("{}");
    }
    k + x
}

/// 2018: `panic!(text)` panics with the `String` itself.
pub fn message(arg: u32) -> u32 {
    let text = format!("bad {}", arg);
    let x = std::hint::black_box(arg);
    let k = std::hint::black_box(1); // MESSAGE
    std::hint::black_box(&x);
    if x > 1000 {
        panic!(text);
    }
    k + x
}

/// Resolver 1: `app`'s dev-dependency's feature `big` reaches `smallnum` in `app`'s build.
pub fn scaled(arg: u32) -> u32 {
    let s = smallnum::scale();
    let x = std::hint::black_box(arg);
    let k = std::hint::black_box(1); // SCALED
    std::hint::black_box(&s);
    k + s + x
}
"""
MAIN = """use std::panic::catch_unwind;

fn main() {
    println!("CAPTURED {}", util::captured(5));
    println!("MUTATED {}", util::mutated(6));
    println!("INSIDE {}", util::inside(9));
    println!("MOVED {}", util::moved(11));
    println!("SUMMED {}", util::summed(13));
    println!("BORROWED {}", util::borrowed(17));
    println!("COPIED {}", util21::borrowed(19));
    match catch_unwind(|| util::checked(5000)) {
        Ok(v) => println!("CHECKED ok {}", v),
        Err(p) => println!("CHECKED {}", p.downcast_ref::<u32>().unwrap()),
    }
    match catch_unwind(|| util::literal(999)) {
        Ok(v) => println!("LITERAL ok {}", v),
        Err(p) => println!("LITERAL {}", p.downcast_ref::<&str>().unwrap()),
    }
    match catch_unwind(|| util::message(2000)) {
        Ok(v) => println!("MESSAGE ok {}", v),
        Err(p) => println!("MESSAGE {}", p.downcast_ref::<String>().unwrap().replace(' ', "_")),
    }
    println!("SCALED {}", util::scaled(3));
}
"""
SMALLNUM = """pub fn scale() -> u32 {
    if cfg!(feature = "big") {
        1000
    } else {
        1
    }
}
"""
# The same function in edition 2021, where `arr.into_iter()` copies `arr`.
UTIL21 = UTIL[UTIL.index("/// 2018: the iterator borrows") : UTIL.index("/// 2018: `panic!(x)`")]
FILES = {
    # No `resolver`: a virtual workspace's is 1, as tokio's is.
    "Cargo.toml": '[workspace]\nmembers = ["app", "util", "util21"]\n',
    "app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2018"\n\n'
    '[dependencies]\nutil = { path = "../util" }\nutil21 = { path = "../util21" }\n\n'
    '[dev-dependencies]\nsmallnum = { version = "1", features = ["big"] }\n',
    "app/src/main.rs": MAIN,
    "util/Cargo.toml": '[package]\nname = "util"\nversion = "0.1.0"\nedition = "2018"\n\n'
    '[dependencies]\nsmallnum = "1"\n',
    "util/src/lib.rs": UTIL,
    "util21/Cargo.toml": '[package]\nname = "util21"\nversion = "0.1.0"\nedition = "2021"\n',
    "util21/src/lib.rs": UTIL21,
    "vendor/smallnum/Cargo.toml": '[package]\nname = "smallnum"\nversion = "1.0.0"\n'
    'edition = "2018"\n\n[features]\nbig = []\n',
    "vendor/smallnum/src/lib.rs": SMALLNUM,
}


def line_of(marker, text=UTIL):
    return next(i for i, line in enumerate(text.splitlines(), 1) if line.endswith(f"// {marker}"))


def make(tmp_path, monkeypatch, files, analyse=("util", "util21"), run_=None, features=()):
    names = ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER")
    if not all(os.environ.get("PEEKY_RUST_" + name) for name in names):
        pytest.skip("set PEEKY_RUST_* tool paths to enable Rust conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "ws"
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    copy = root / "vendor/smallnum"
    hashed = {
        path.relative_to(copy).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in copy.rglob("*")
        if path.is_file()
    }
    (copy / ".cargo-checksum.json").write_text(json.dumps({"files": hashed, "package": "ab" * 32}))
    home = tmp_path / "cargo-home"
    home.mkdir()
    subprocess.run(
        [os.environ["PEEKY_RUST_CARGO"], "generate-lockfile", "--offline"]
        + list(rw.vendor_arguments(root, "vendor")),
        cwd=root,
        env=rw.cargo_environment(os.environ["PEEKY_RUST_RUSTC"], home),
        check=True,
        capture_output=True,
    )
    config = root / "rust.json"
    data = {"analyse": list(analyse), "run": run_ or {"bin": "app"}, "vendor": "vendor"}
    config.write_text(json.dumps({**data, **({"features": list(features)} if features else {})}))
    return root, config


@pytest.fixture
def built(tmp_path, monkeypatch, capsys):
    root, config = make(tmp_path, monkeypatch, FILES)
    code, summary = run(capsys, "build", str(root), "--config", str(config), "--quiet")
    assert code == 0, summary
    return root, config


def run(capsys, *argv):
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def probe(capsys, built, marker, *more, file="util/src/lib.rs"):
    root, config = built
    text = UTIL21 if file.startswith("util21/") else UTIL
    at = f"{file}:{line_of(marker, text)}"
    return run(capsys, "probe", str(root), "--config", str(config), "--at", at, "--quiet", *more)


def changed(capsys, built, marker, selector, value, printed_as, *more):
    """Probe ``marker`` setting ``selector`` to ``value``: the change is made, read back,
    and the program prints ``printed_as`` for ``marker``."""
    code, result = probe(
        capsys, built, marker, "--capture", selector, "--set", f"{selector}={value}"
    )
    assert code == 0, (marker, selector, result.get("error"))
    assert rows(result, "after-set")[selector]["value"] == str(value), (marker, result["rows"])
    assert printed(result, marker) == [printed_as], (marker, selector, result["output"])
    return result


def rows(result, phase="observed"):
    return {row["selector"]: row for row in result["rows"] if row["phase"] == phase}


def printed(result, label):
    return [line.split()[1] for line in result["output"].splitlines() if line.startswith(label)]


@pytest.mark.timeout(900)
def test_a_closure_captures_whole_variables_as_edition_2018_does(built, capsys):
    # `|| p.a + 1` holds `&p`, all of it: a change of either field is seen, through it or by
    # `p.b += 1` (the program prints `k + (p.a + 1) + (p.b + 1)`).
    code, result = probe(capsys, built, "CAPTURED", "--capture", "p,p.a,p.b")
    assert code == 0, result
    found = rows(result)
    assert (found["p.a"]["value"], found["p.b"]["value"]) == ("5", "2")
    assert found["p"]["value"] == "Pair { a: 5, b: 2 }"
    assert printed(result, "CAPTURED") == ["10"]
    changed(capsys, built, "CAPTURED", "p.a", 100, "105")
    changed(capsys, built, "CAPTURED", "p.b", 100, "108")
    # `|| p.a += 1` holds `&mut p`, all of it: `k + (p.a + 1) + p.b`.
    code, result = probe(capsys, built, "MUTATED", "--capture", "p.a,p.b")
    assert (rows(result)["p.a"]["value"], rows(result)["p.b"]["value"]) == ("6", "4")
    assert printed(result, "MUTATED") == ["12"]
    changed(capsys, built, "MUTATED", "p.a", 100, "106")
    changed(capsys, built, "MUTATED", "p.b", 100, "108")
    # In the closure's body, its capture is all of `p` (2021 names only `p__a` there): read
    # with the note that Peeky proves nothing of a capture. The program prints `(p.a + 1) +
    # p.b`.
    code, result = probe(capsys, built, "INSIDE", "--capture", "p.a,p.b,n")
    assert code == 0, result
    found = rows(result)
    assert [found[name]["value"] for name in ("p.a", "p.b", "n")] == ["9", "7", "1"]
    for name in ("p.a", "p.b"):
        assert "does not prove initialized here" in found[name]["note"], found[name]
        assert found[name]["settable"] is False
    assert printed(result, "INSIDE") == ["17"]
    # `move || p.a + p.name.len()` moves all of `p` into `f`: `p` is moved, and a change of
    # `p.a` would not reach `f`'s copy (the program prints `k + 11 + 3` whatever `p.a` is).
    code, result = probe(capsys, built, "MOVED", "--capture", "p.a,p.name")
    assert code == 0, result
    for name in ("p.a", "p.name"):
        row = rows(result)[name]
        assert "It may be moved, partly moved, dropped or unassigned" in row["note"], row
        assert row["stale"] is True and row["settable"] is False
    assert printed(result, "MOVED") == ["15"]
    code, result = probe(capsys, built, "MOVED", "--set", "p.a=100")
    error = result["error"]
    assert error["code"] == "MUTATION_REJECTED", error
    assert (
        f"the program has already copied `p` into `f` (at line {line_of('MOVED') - 1})"
        in error["message"]
    ), error


@pytest.mark.timeout(900)
def test_array_into_iter_iterates_by_reference_as_edition_2018_does(built, capsys):
    # `for v in arr.into_iter()`: `v` is a `&u32` into `arr`, and the loop adds them up.
    code, result = probe(capsys, built, "SUMMED", "--capture", "arr[0],v,total")
    assert code == 0, result
    found = rows(result)
    assert found["v"]["type"] == "&u32"
    assert (found["arr[0]"]["value"], found["total"]["value"]) == ("13", "0")
    assert printed(result, "SUMMED") == ["18"]
    changed(capsys, built, "SUMMED", "total", 100, "118")
    # The iterator borrows `arr`: a change of `arr` after it is made is seen by the loop.
    code, result = probe(capsys, built, "BORROWED", "--capture", "arr[2],it")
    assert code == 0, result
    assert rows(result)["arr[2]"]["value"] == "3"
    assert rows(result)["it"]["type"].startswith("core::slice::iter::Iter<")
    changed(capsys, built, "BORROWED", "arr[2]", 100, "120")
    # In edition 2021 the same code copies `arr` into the iterator: refused, naming it.
    code, result = probe(capsys, built, "BORROWED", "--set", "arr[2]=100", file="util21/src/lib.rs")
    error = result["error"]
    assert error["code"] == "MUTATION_REJECTED", error
    assert "the program's own `it` (at line" in error["message"], error
    assert "may hold a copy of `arr` that it goes on with" in error["message"], error
    code, result = probe(capsys, built, "BORROWED", "--capture", "it", file="util21/src/lib.rs")
    assert rows(result)["it"]["type"].startswith("core::array::iter::IntoIter<")
    assert printed(result, "COPIED") == ["25"]


@pytest.mark.timeout(900)
def test_panic_takes_its_payload_and_a_lone_literal_as_edition_2018_does(built, capsys):
    # `panic!(x)` panics with `x` as its payload, which the program catches and prints.
    code, result = probe(capsys, built, "CHECKED", "--capture", "x")
    assert code == 0, result
    assert rows(result)["x"]["value"] == "5000"
    assert printed(result, "CHECKED") == ["5000"]
    changed(capsys, built, "CHECKED", "x", 4321, "4321")
    changed(capsys, built, "CHECKED", "x", 7, "ok")
    # `panic!("{}")` panics with `{}` as it is.
    code, result = probe(capsys, built, "LITERAL", "--capture", "x")
    assert rows(result)["x"]["value"] == "999"
    assert printed(result, "LITERAL") == ["{}"]
    result = changed(capsys, built, "LITERAL", "x", 5, "ok")
    assert "LITERAL ok 6" in result["output"]
    # `panic!(text)` panics with the `String` itself.
    code, result = probe(capsys, built, "MESSAGE", "--capture", "x,text")
    assert (rows(result)["x"]["value"], rows(result)["text"]["value"]) == ("2000", '"bad 2000"')
    assert printed(result, "MESSAGE") == ["bad_2000"]
    result = changed(capsys, built, "MESSAGE", "x", 7, "ok")
    assert "MESSAGE ok 8" in result["output"]


@pytest.mark.timeout(900)
def test_resolver_1_builds_a_dependency_with_a_dev_dependencys_features(built, capsys):
    # The workspace's resolver is 1, so `app`'s dev-dependency's `big` reaches `smallnum` in
    # `cargo build --bin app` (resolver 2 would build it without, and `scale()` return 1).
    code, result = probe(capsys, built, "SCALED", "--capture", "s,x")
    assert code == 0, result
    assert (rows(result)["s"]["value"], rows(result)["x"]["value"]) == ("1000", "3")
    assert printed(result, "SCALED") == ["1004"]
    changed(capsys, built, "SCALED", "s", 7, "11")


@pytest.mark.timeout(600)
def test_features_resolver_1_gives_another_package_are_refused(tmp_path, monkeypatch, capsys):
    # A workspace whose root is a package, at resolver 1 (its edition's default): Cargo gives
    # `--features fast` to `top`, and would build `app` without it, saying nothing.
    files = {
        **FILES,
        "Cargo.toml": '[package]\nname = "top"\nversion = "0.1.0"\nedition = "2018"\n\n'
        '[features]\nfast = []\n\n[workspace]\nmembers = ["app", "util", "util21"]\n',
        "src/main.rs": "fn main() {}\n",
        "app/Cargo.toml": FILES["app/Cargo.toml"] + "\n[features]\nfast = []\n",
    }
    root, config = make(
        tmp_path, monkeypatch, files, run_={"bin": "app", "package": "app"}, features=["fast"]
    )
    code, result = run(capsys, "build", str(root), "--config", str(config), "--quiet")
    error = result["error"]
    assert error["code"] == "UNSUPPORTED", error
    assert error["message"].startswith(
        'Peeky builds only with the features "features" names, and Cargo would build `app` '
        "without `fast`. Under resolver 1, Cargo gives --features to the package in the "
        "workspace's root directory"
    ), error
    # Named for `app`, Cargo builds it with them.
    config.write_text(config.read_text().replace('"fast"', '"app/fast"'))
    code, result = run(capsys, "build", str(root), "--config", str(config), "--quiet")
    assert code == 0, result


@pytest.mark.timeout(600)
def test_edition_2015_is_refused_and_text_2018_lexes_otherwise_is_refused(
    tmp_path, monkeypatch, capsys
):
    manifest = FILES["util/Cargo.toml"]
    root, config = make(
        tmp_path, monkeypatch, {**FILES, "util/Cargo.toml": manifest.replace("2018", "2015")}
    )
    code, result = run(capsys, "build", str(root), "--config", str(config), "--quiet")
    assert result["error"]["code"] == "UNSUPPORTED", result
    assert result["error"]["message"].startswith(
        "Peeky analyses only editions 2018, 2021 and 2024: package `util` is edition 2015."
    )
    # Edition 2018 reads `cr"\""` as `cr` and the string `"\""`; Peeky, as Rust 2021, a raw C
    # string that ends at the backslash, which could hide code from its scans.
    text = UTIL + 'macro_rules! ignore {\n    ($($t:tt)*) => {};\n}\nignore!(cr"\\"");\n'
    (root / "util/Cargo.toml").write_text(manifest)
    (root / "util/src/lib.rs").write_text(text)
    code, result = run(capsys, "build", str(root), "--config", str(config), "--quiet")
    assert code == 0, result
    code, result = probe(capsys, (root, config), "CHECKED", "--capture", "x")
    error = result["error"]
    assert error["code"] == "UNSUPPORTED", error
    assert error["message"] == (
        "Peeky can't read `util`'s text as edition 2018 does: util/src/lib.rs has a raw C "
        f'string (`cr"…"`) on line {len(text.splitlines())}. Peeky reads one as Rust 2021 '
        "does, and edition 2018 reads `cr` and an ordinary string. Analyse another package, "
        "or write the string otherwise"
    ), error
