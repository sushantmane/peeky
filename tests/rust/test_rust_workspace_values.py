"""Values and changes in a Rust workspace's build: a path dependency analysed, a vendored
registry dependency whose code Peeky reads none of (ADR 0008, phase 2). Every value read
matches the program's own print of it; a change a dependency may keep a copy of is refused
with a note naming the call, its line and its crate."""

import hashlib
import json
import os
import subprocess

import pytest

from peeky import rust_workspace as rw
from peeky.cli import main

UTIL = """use std::cell::Cell;

pub fn double(x: u32) -> u32 {
    let mut v = vec![x, smallnum::one()];
    let held = smallnum::Holder::new(x);
    smallnum::observe(&v);
    let c = Cell::new(x);
    c.set(x + 1);
    v.push(c.get());
    let total: u32 = v.iter().sum();
    drop(held); // TOTAL
    total
}

pub fn kept(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    smallnum::keep(&x);
    let k = std::hint::black_box(1); // KEPT
    std::hint::black_box(&x);
    k + smallnum::kept()
}

pub fn std_only(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    let y = smallnum::one();
    let k = std::hint::black_box(y); // STD
    std::hint::black_box(&x);
    k + x
}

pub fn handed(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    let h = smallnum::spawn(move || x * 2);
    let k = std::hint::black_box(1); // HANDED
    std::hint::black_box(&x);
    k + h.join()
}

pub fn called_back(arg: u32) -> u32 {
    let mut total = arg;
    smallnum::apply(&mut |n| {
        total += n; // BACK
    });
    total
}

#[inline(never)]
fn forward(p: &u32) {
    smallnum::keep(p);
}

pub fn via_helper(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    forward(&x); // FORWARD
    let k = std::hint::black_box(1); // HELPER
    std::hint::black_box(&x);
    k + smallnum::kept()
}

pub fn mapped(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    let s: u32 = (0..1)
        .map(|i| {
            smallnum::keep(&x);
            i
        })
        .sum();
    let k = std::hint::black_box(s); // MAPPED
    std::hint::black_box(&x);
    k + smallnum::kept()
}

struct Wrap(u32);

impl Drop for Wrap {
    fn drop(&mut self) {
        smallnum::keep(&self.0);
    }
}

pub fn wrapped(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    {
        let w = Wrap(x);
        std::hint::black_box(&w);
    } // DROPPED
    let k = std::hint::black_box(1); // WRAPPED
    std::hint::black_box(&x);
    k + smallnum::kept()
}

pub fn unrelated(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    let y = std::hint::black_box(2);
    forward(&y);
    let k = std::hint::black_box(1); // UNRELATED
    std::hint::black_box(&x);
    k + x
}

pub fn later(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    let k = std::hint::black_box(1); // LATER
    smallnum::keep(&x);
    k + x + smallnum::kept()
}

pub fn pointed(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    let f: fn(&u32) = smallnum::maker();
    f(&x); // CALLED
    let k = std::hint::black_box(1); // POINTED
    std::hint::black_box(&x);
    k + smallnum::kept()
}

pub fn closed(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    let f = smallnum::keeper();
    f(&x); // CLOSURE
    let k = std::hint::black_box(1); // CLOSED
    std::hint::black_box(&x);
    k + smallnum::kept()
}

struct Mine<'a>(&'a u32);

impl smallnum::Keep for Mine<'_> {
    fn keep_it(&self) {
        std::hint::black_box(*self.0);
    }

    fn value(&self) -> u32 {
        *self.0
    }
}

trait Show {
    fn show(&self, v: &u32);
}

impl Show for smallnum::Holder {
    fn show(&self, v: &u32) {
        std::hint::black_box(v);
    }
}

pub fn own_impl(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    smallnum::Keep::keep_it(&Mine(&x));
    smallnum::Holder::new(1).show(&x);
    let k = std::hint::black_box(1); // OWN_IMPL
    std::hint::black_box(&x);
    k + x
}

pub fn defaulted(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    smallnum::Keep::noted(&Mine(&x)); // NOTED
    let k = std::hint::black_box(1); // DEFAULTED
    std::hint::black_box(&x);
    k + smallnum::kept()
}

/// A trait the program's binary implements: its `impl` is the binary's code.
pub trait Sink {
    fn sink(&self, x: &u32);
}

pub fn generic<T: Sink + ?Sized>(t: &T, arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    t.sink(&x); // SINK_GENERIC
    let k = std::hint::black_box(1); // GENERIC
    std::hint::black_box(&x);
    k + smallnum::kept()
}

pub fn via_generic(t: &dyn Sink, arg: u32) -> u32 {
    generic(t, arg)
}

pub fn dynamic(t: &dyn Sink, arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    t.sink(&x); // SINK_DYN
    let k = std::hint::black_box(1); // DYNAMIC
    std::hint::black_box(&x);
    k + smallnum::kept()
}

pub fn blanket(arg: u32) -> u32 {
    use smallnum::Stash;
    let x = std::hint::black_box(arg);
    x.stash(); // STASH
    let k = std::hint::black_box(1); // BLANKET
    std::hint::black_box(&x);
    k + smallnum::kept()
}
"""
MAIN = """struct Keeper;

impl util::Sink for Keeper {
    fn sink(&self, x: &u32) {
        smallnum::keep(x);
    }
}

fn main() {
    println!("DOUBLE {}", util::double(smallnum::one()));
    println!("KEPT {}", util::kept(5));
    println!("STD {}", util::std_only(5));
    println!("HANDED {}", util::handed(5));
    println!("BACK {}", util::called_back(5));
    println!("HELPER {}", util::via_helper(7));
    println!("MAPPED {}", util::mapped(9));
    println!("WRAPPED {}", util::wrapped(11));
    println!("UNRELATED {}", util::unrelated(15));
    println!("LATER {}", util::later(5));
    println!("POINTED {}", util::pointed(17));
    println!("CLOSED {}", util::closed(19));
    println!("OWN_IMPL {}", util::own_impl(21));
    println!("DEFAULTED {}", util::defaulted(23));
    println!("BLANKET {}", util::blanket(25));
    println!("GENERIC {}", util::via_generic(&Keeper, 27));
    println!("DYNAMIC {}", util::dynamic(&Keeper, 29));
}
"""
SMALLNUM = """use std::sync::atomic::{AtomicU32, Ordering};

static KEPT: AtomicU32 = AtomicU32::new(0);

pub fn one() -> u32 {
    1
}

pub struct Holder(pub u32);

impl Holder {
    pub fn new(x: u32) -> Holder {
        Holder(x)
    }
}

impl Drop for Holder {
    fn drop(&mut self) {}
}

pub fn observe(v: &[u32]) -> usize {
    v.len()
}

pub fn keep(v: &u32) {
    KEPT.store(*v, Ordering::SeqCst);
}

pub fn kept() -> u32 {
    KEPT.load(Ordering::SeqCst)
}

pub struct Handle(std::thread::JoinHandle<u32>);

impl Handle {
    pub fn join(self) -> u32 {
        self.0.join().unwrap()
    }
}

pub fn spawn<F: FnOnce() -> u32 + Send + 'static>(f: F) -> Handle {
    Handle(std::thread::spawn(f))
}

pub fn apply(f: &mut dyn FnMut(u32)) {
    f(3);
}

pub fn maker() -> fn(&u32) {
    keep
}

pub fn keeper() -> impl Fn(&u32) {
    |v| keep(v)
}

pub trait Keep {
    fn keep_it(&self);

    fn value(&self) -> u32;

    fn noted(&self) -> u32 {
        keep(&self.value());
        0
    }
}

pub trait Stash {
    fn stash(&self);
}

impl<T: Copy + Into<u32>> Stash for T {
    fn stash(&self) {
        keep(&(*self).into());
    }
}

#[macro_export]
macro_rules! first {
    ($v:expr) => {
        unsafe { *$v.as_ptr() }
    };
}
"""
FILES = {
    "Cargo.toml": '[workspace]\nmembers = ["app", "util"]\nresolver = "2"\n',
    "app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n\n'
    '[dependencies]\nutil = { path = "../util" }\nsmallnum = "1"\n',
    "app/src/main.rs": MAIN,
    "util/Cargo.toml": '[package]\nname = "util"\nversion = "0.1.0"\nedition = "2021"\n\n'
    '[dependencies]\nsmallnum = "1"\n',
    "util/src/lib.rs": UTIL,
    "vendor/smallnum/Cargo.toml": '[package]\nname = "smallnum"\nversion = "1.0.0"\n'
    'edition = "2021"\n',
    "vendor/smallnum/src/lib.rs": SMALLNUM,
}
# A local procedural macro, whose code is the project's: it writes unsafe code into `util`.
MACRO = """use proc_macro::TokenStream;

#[proc_macro]
pub fn first(input: TokenStream) -> TokenStream {
    format!("unsafe {{ *({}).as_ptr() }}", input).parse().unwrap()
}
"""


def line_of(marker, text=UTIL):
    return next(i for i, line in enumerate(text.splitlines(), 1) if line.endswith(f"// {marker}"))


def make(tmp_path, monkeypatch, files):
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
    config.write_text(json.dumps({"analyse": ["util"], "run": {"bin": "app"}, "vendor": "vendor"}))
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


def probe(capsys, built, marker, *more, text=UTIL):
    root, config = built
    at = f"util/src/lib.rs:{line_of(marker, text)}"
    return run(capsys, "probe", str(root), "--config", str(config), "--at", at, "--quiet", *more)


def rows(result, phase="observed"):
    return {row["selector"]: row for row in result["rows"] if row["phase"] == phase}


def unread_memory(row):
    """Whether the debugger reads no memory (lldb-dap before LLVM 20 has no readMemory): a
    Vec shows LLDB's summary, saying so."""
    return row["value"].startswith("alloc::vec::Vec<") and "readMemory" in (row.get("note") or "")


def printed(result, label):
    return [line.split()[1] for line in result["output"].splitlines() if line.startswith(label)]


@pytest.mark.timeout(600)
def test_values_read_match_the_programs_own_print(built, capsys):
    code, result = probe(capsys, built, "TOTAL", "--capture", "v,total,x")
    assert code == 0, result
    found = rows(result)
    assert (found["x"]["value"], found["total"]["value"]) == ("1", "4")
    assert unread_memory(found["v"]) or found["v"]["value"] == "[1, 1, 2]", found["v"]
    # The program's own print: `double` returns `total`.
    assert printed(result, "DOUBLE") == ["4"]


@pytest.mark.timeout(600)
def test_a_change_a_dependency_may_keep_a_copy_of_is_refused_naming_the_call(built, capsys):
    # `smallnum::keep(&x)` keeps a copy in the dependency's static, which `kept()` returns
    # after the stop: a change of `x` there would leave `KEPT` printing 1 + 5.
    lead = "Peeky reads no dependency's code, so it may keep a copy"
    cases = {
        "KEPT": f"a call to `smallnum::keep` (dependency `smallnum`) on line "
        f"{line_of('KEPT') - 1} is given a pointer into `x`: {lead}",
        "HANDED": "a call to `smallnum::spawn::<{closure@util/src/lib.rs:"
        f"{line_of('HANDED') - 1}:29: {line_of('HANDED') - 1}:36}}>` (dependency `smallnum`) on "
        f"line {line_of('HANDED') - 1} is given what may hold a copy of `x`, or a value "
        f"computed from it: {lead}",
        # The crate's own `forward` hands it on.
        "HELPER": f"a call to `forward` on line {line_of('FORWARD')} is given a pointer into "
        f"`x`, and may hand it on to `smallnum::keep` (dependency `smallnum`): {lead}",
        # A closure the standard library's `map` runs.
        "MAPPED": "and may hand it on to `smallnum::keep` (dependency `smallnum`)",
        # The crate's own `Drop`.
        "WRAPPED": f"the drop of a `Wrap` on line {line_of('DROPPED')} drops what may hold a "
        "copy of `x`, or a value computed from it, and its `Drop` may hand it on to "
        "`smallnum::keep` (dependency `smallnum`)",
        # A function pointer and a closure the dependency returned.
        "POINTED": "a call through a function pointer (whose target Peeky can't identify, so "
        f"it may run a dependency's code) on line {line_of('CALLED')} is given a pointer into "
        "`x`",
        # A closure of the dependency's is named by its path.
        "CLOSED": "a call to `<{closure@smallnum::keeper::{closure#0}} as "
        "std::ops::Fn<(&u32,)>>::call` (dependency `smallnum`) on line "
        f"{line_of('CLOSURE')} is given",
        # The dependency's trait's default method, which the crate's `impl` leaves as it is.
        "DEFAULTED": "a call to `<Mine as smallnum::Keep>::noted` (dependency `smallnum`) on "
        f"line {line_of('NOTED')} is given what may hold a pointer into `x`",
        # The program's binary implements the crate's trait: through a type parameter and a
        # trait object, its code.
        "GENERIC": "a call to `<T as Sink>::sink` (code another crate's type may supply: a "
        f"trait object or a type parameter) on line {line_of('SINK_GENERIC')} is given a "
        "pointer into `x`",
        "DYNAMIC": "a call to `<dyn Sink as Sink>::sink` (code another crate's type may "
        f"supply: a trait object or a type parameter) on line {line_of('SINK_DYN')} is given "
        "a pointer into `x`",
        # The dependency's blanket `impl`.
        "BLANKET": "a call to `<u32 as smallnum::Stash>::stash` (dependency `smallnum`) on line "
        f"{line_of('STASH')} is given a pointer into `x`",
    }
    for marker, reason in cases.items():
        code, result = probe(capsys, built, marker, "--set", "x=100")
        error = result["error"]
        assert error["code"] == "MUTATION_REJECTED", (marker, error)
        assert reason in error["message"], (marker, error["message"])
        assert "Change it before the program hands it to that code" in error["message"]
        assert result.get("refused_before_launch") is True, marker
    # A read there shows why on the row.
    code, result = probe(capsys, built, "KEPT", "--capture", "x")
    assert code == 0, result
    assert rows(result)["x"]["settable"] is False
    assert printed(result, "KEPT") == ["6"]


@pytest.mark.timeout(600)
def test_a_change_no_dependency_was_given_is_made_and_the_program_prints_it(built, capsys):
    # Only the standard library's code was given `x` before the stop (`smallnum::one()` is
    # given nothing, `forward(&y)` another local), or a dependency is given it after the stop:
    # each sees the change.
    # The crate's own `impl` of the dependency's trait, and its own trait's `impl` for the
    # dependency's type: both its code, read.
    observed = {"STD": "5", "UNRELATED": "15", "LATER": "5", "OWN_IMPL": "21"}
    for marker, shown in (
        ("STD", "101"),
        ("UNRELATED", "101"),
        ("LATER", "201"),
        ("OWN_IMPL", "101"),
    ):
        code, result = probe(capsys, built, marker, "--capture", "x", "--set", "x=100")
        assert code == 0, (marker, result.get("error"))
        assert rows(result)["x"]["value"] == observed[marker]
        assert rows(result, "after-set")["x"]["value"] == "100"
        assert printed(result, marker) == [shown], (marker, result["output"])


@pytest.mark.timeout(600)
def test_a_dependencys_frame_and_another_members_are_read_as_a_librarys(built, capsys):
    root, config = built
    at = f"util/src/lib.rs:{line_of('BACK')}"
    code, started = run(capsys, "session", "start", str(root), "--config", str(config), "--at", at)
    assert code == 0, started
    try:
        # smallnum::apply's frame, then util::called_back's, then app's main.
        code, result = run(capsys, "session", "inspect", "--frame", "1")
        assert result["error"]["code"] == "INVALID_REQUEST", result
        assert "Frame 1 (smallnum::apply::" in result["error"]["message"]
        assert "is not in the prepared program's sources" in result["error"]["message"]
        code, result = run(capsys, "session", "inspect", "--frame", "2")
        assert code == 0, result
        (total,) = [
            row
            for scope in result["scopes"]
            for row in scope["variables"]
            if row["name"] == "total"
        ]
        assert total["value"] == "5"
        code, result = run(capsys, "session", "inspect", "--frame", "3")
        assert "Frame 3 (app::main::" in result["error"]["message"]
        # The session stays usable.
        code, result = run(capsys, "session", "inspect")
        assert code == 0, result
    finally:
        run(capsys, "session", "close")


@pytest.mark.timeout(600)
def test_unsafe_code_a_local_procedural_macro_writes_withholds_memory_reads(
    tmp_path, monkeypatch, capsys
):
    # `smallnum::first!` writes unsafe code too: a dependency's macro, its own code, taken as
    # sound as std's `thread_local!` is. The local `pm`'s is the project's.
    files = {
        **FILES,
        "Cargo.toml": '[workspace]\nmembers = ["app", "util", "pm"]\nresolver = "2"\n',
        "pm/Cargo.toml": '[package]\nname = "pm"\nversion = "0.1.0"\nedition = "2021"\n\n'
        "[lib]\nproc-macro = true\n",
        "pm/src/lib.rs": MACRO,
    }
    text = UTIL.replace(
        "drop(held); // TOTAL", "drop(held);\n    let f = smallnum::first!(v); // TOTAL"
    )
    files["util/src/lib.rs"] = text
    root, config = make(tmp_path, monkeypatch, files)
    built = (root, config)
    assert run(capsys, "build", str(root), "--config", str(config), "--quiet")[0] == 0
    code, result = probe(capsys, built, "TOTAL", "--capture", "v", text=text)
    assert code == 0, result
    if unread_memory(rows(result)["v"]):
        pytest.skip("this lldb-dap has no readMemory: no Vec's contents are read")
    assert rows(result)["v"]["value"] == "[1, 1, 2]"
    # Now with `pm`.
    manifest = root / "util/Cargo.toml"
    manifest.write_text(manifest.read_text() + 'pm = { path = "../pm" }\n')
    (root / "util/src/lib.rs").write_text(text.replace("smallnum::first!(v)", "pm::first!(v)"))
    subprocess.run(
        [os.environ["PEEKY_RUST_CARGO"], "generate-lockfile", "--offline"]
        + list(rw.vendor_arguments(root, "vendor")),
        cwd=root,
        env=rw.cargo_environment(os.environ["PEEKY_RUST_RUSTC"], tmp_path / "cargo-home"),
        check=True,
        capture_output=True,
    )
    code, summary = run(capsys, "build", str(root), "--config", str(config), "--quiet")
    assert code == 0, summary
    code, result = probe(capsys, built, "TOTAL", "--capture", "v", text=text)
    assert code == 0, result
    row = rows(result)["v"]
    assert row["value"].startswith("alloc::vec::Vec<unsigned int, alloc::alloc::Global> @ 0x")
    assert (
        "not its contents: the crate has unsafe code (in `util` as rustc expands it, with the "
        "local procedural macros `pm`), which can free a buffer without Peeky seeing it"
    ) in row["note"], row


# A build script's code: `util` includes what it generates, with unsafe code in it.
GENERATING = """fn main() {
    let out = std::env::var("OUT_DIR").unwrap();
    std::fs::write(
        format!("{out}/generated.rs"),
        "pub fn first(v: &[u32]) -> u32 { unsafe { *v.as_ptr() } }\\n",
    )
    .unwrap();
    println!("cargo::rerun-if-changed=build.rs");
}
"""


@pytest.mark.timeout(600)
def test_unsafe_code_a_build_script_generates_withholds_memory_reads(tmp_path, monkeypatch, capsys):
    text = (
        UTIL.replace(
            "drop(held); // TOTAL",
            "drop(held);\n    let f = first(&v); // TOTAL",
        )
        + '\ninclude!(concat!(env!("OUT_DIR"), "/generated.rs"));\n'
    )
    files = {**FILES, "util/src/lib.rs": text, "util/build.rs": GENERATING}
    root, config = make(tmp_path, monkeypatch, files)
    code, summary = run(capsys, "build", str(root), "--config", str(config), "--quiet")
    assert code == 0, summary
    code, result = probe(capsys, (root, config), "TOTAL", "--capture", "v", text=text)
    assert code == 0, result
    row = rows(result)["v"]
    assert row["value"].startswith("alloc::vec::Vec<unsigned int, alloc::alloc::Global> @ 0x")
    assert "not its contents: the crate has unsafe code (in " in row["note"], row
    assert "/out/generated.rs, which the build generated)" in row["note"], row


# A function of the crate's given a dependency's type, and a static it never touches.
TYPED = """
static SEEN: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

pub fn seen() -> u32 {
    SEEN.fetch_add(1, std::sync::atomic::Ordering::SeqCst)
}

#[inline(never)]
fn weigh(h: &smallnum::Holder, v: u32) -> u32 {
    h.0 + v
}

pub fn typed(arg: u32) -> u32 {
    let x = std::hint::black_box(arg);
    let h = smallnum::Holder::new(2);
    let w = weigh(&h, x);
    let k = std::hint::black_box(w); // TYPED
    std::hint::black_box(&x);
    k + x + seen()
}
"""


@pytest.mark.timeout(600)
def test_a_crate_function_given_a_dependencys_type_is_the_crates_code(
    tmp_path, monkeypatch, capsys
):
    # `weigh` is read: it touches no static, and keeps nothing it is given, whatever type. So
    # `seen()` after the stop reads no copy of `x`.
    text = UTIL + TYPED
    main = MAIN.replace(
        '    println!("DOUBLE',
        '    println!("TYPED {} {}", util::typed(31), util::seen());\n    println!("DOUBLE',
    )
    root, config = make(
        tmp_path, monkeypatch, {**FILES, "util/src/lib.rs": text, "app/src/main.rs": main}
    )
    assert run(capsys, "build", str(root), "--config", str(config), "--quiet")[0] == 0
    built = (root, config)
    code, result = probe(capsys, built, "TYPED", "--capture", "x", "--set", "x=100", text=text)
    assert code == 0, result.get("error")
    assert rows(result)["x"]["value"] == "31"
    assert rows(result, "after-set")["x"]["value"] == "100"
    # `w` is 2 + 31, then `k + x` with the change.
    assert printed(result, "TYPED") == ["133"], result["output"]
