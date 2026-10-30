"""At `let y = x.clone();`, LLVM inlines `<u32 as Clone>::clone` even at opt-level 0.
Its load of `x` carries `core/src/clone.rs`'s line, and comes before the line's first row of
its own, where LLDB puts the line's breakpoint. Peeky took the stop for one before the line's
code, so `x` was `[settable]`, and a change there never reached `y`. The DWARF's inlined call
(its call site and address range, against the stop's pc) proves what ran before the stop;
the MIR says which local the call was given."""

from pathlib import Path
from types import SimpleNamespace

from peeky.errors import classify
from peeky.rust import RustSession
from peeky.rust_mir import (
    IrFunction,
    OwnedLocals,
    _call_operands,
    _callee,
    _covers,
    macro_lines,
    parse_body,
)
from peeky.rust_symbols import CodeBlock

HASH = "h0123456789abcdef"
# The crate the fixtures below are rustc 1.96.0's MIR of (`-Zmir-opt-level=0
# -Zdump-mir=PreCodegen -Zmir-include-spans=yes`, as Peeky builds it; the comment lines of
# its constants left out). tests/rust/test_rust_inlined_reads.py runs it.
SOURCE = """use std::env;

struct P {
    a: u32,
    b: u32,
}

#[inline(always)]
fn twice(v: &u32) -> u32 {
    *v * 2
}

#[inline(always)]
fn size(s: &String) -> usize {
    s.len()
}

fn cloned(x: u32) -> u32 {
    let k = std::convert::identity(1u32); // BEFORE
    let y = x.clone(); // CLONE
    y + k
}

fn through(x: u32) -> u32 {
    let r = &x;
    let y = (*r).clone(); // THROUGH
    y
}

fn field(arg: u32) -> u32 {
    let p = P { a: arg, b: 2 };
    let y = p.a.clone(); // FIELD
    y + p.b
}

fn doubled(x: u32) -> u32 {
    let y = twice(&x); // TWICE
    y
}

fn sized(s: String) -> usize {
    let n = size(&s); // SIZE
    n + s.len()
}

fn mixed(x: u32, z: u32) -> u32 {
    let y = z.clone() + x; // MIXED
    std::hint::black_box(&x);
    y
}

fn listed(x: u32) -> u32 {
    let v = vec![x, 2, 3]; // VEC
    std::hint::black_box(&x);
    v[0]
}

fn main() {
    let x: u32 = env::args().nth(1).unwrap().parse().unwrap();
    let s = env::args().nth(2).unwrap();
    println!("CLONE {}", cloned(x));
    println!("THROUGH {}", through(x));
    println!("FIELD {}", field(x));
    println!("TWICE {}", doubled(x));
    println!("SIZE {}", sized(s));
    println!("MIXED {}", mixed(x, 10));
    println!("VEC {}", listed(x));
}
"""
CLONED = """// MIR for `cloned` after PreCodegen

fn cloned(_1: u32) -> u32 {
    debug x => _1;                       // in scope 0 at src/main.rs:18:11: 18:12
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:18:22: 18:25
    let _2: u32;                         // in scope 0 at src/main.rs:19:9: 19:10
    let mut _4: &u32;                    // in scope 0 at src/main.rs:20:13: 20:14
    let mut _5: u32;                     // in scope 0 at src/main.rs:21:5: 21:6
    let mut _6: u32;                     // in scope 0 at src/main.rs:21:9: 21:10
    let mut _7: (u32, bool);             // in scope 0 at src/main.rs:21:5: 21:10
    scope 1 {
        debug k => _2;                   // in scope 1 at src/main.rs:19:9: 19:10
        let _3: u32;                     // in scope 1 at src/main.rs:20:9: 20:10
        scope 2 {
            debug y => _3;               // in scope 2 at src/main.rs:20:9: 20:10
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:19:9: 19:10
        _2 = identity::<u32>(const 1_u32) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:19:13: 19:41
    }

    bb1: {
        StorageLive(_3);                 // scope 1 at src/main.rs:20:9: 20:10
        StorageLive(_4);                 // scope 1 at src/main.rs:20:13: 20:14
        _4 = &_1;                        // scope 1 at src/main.rs:20:13: 20:14
        _3 = <u32 as Clone>::clone(move _4) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:20:13: 20:22
    }

    bb2: {
        StorageDead(_4);                 // scope 1 at src/main.rs:20:21: 20:22
        StorageLive(_5);                 // scope 2 at src/main.rs:21:5: 21:6
        _5 = copy _3;                    // scope 2 at src/main.rs:21:5: 21:6
        StorageLive(_6);                 // scope 2 at src/main.rs:21:9: 21:10
        _6 = copy _2;                    // scope 2 at src/main.rs:21:9: 21:10
        _7 = AddWithOverflow(copy _5, copy _6); // scope 2 at src/main.rs:21:5: 21:10
        assert(!move (_7.1: bool), "attempt to compute `{} + {}`, which would overflow", move _5, move _6) -> [success: bb3, unwind continue]; // scope 2 at src/main.rs:21:5: 21:10
    }

    bb3: {
        _0 = move (_7.0: u32);           // scope 2 at src/main.rs:21:5: 21:10
        StorageDead(_6);                 // scope 2 at src/main.rs:21:9: 21:10
        StorageDead(_5);                 // scope 2 at src/main.rs:21:9: 21:10
        StorageDead(_3);                 // scope 1 at src/main.rs:22:1: 22:2
        StorageDead(_2);                 // scope 0 at src/main.rs:22:1: 22:2
        return;                          // scope 0 at src/main.rs:22:2: 22:2
    }
}
"""
MIXED = """// MIR for `mixed` after PreCodegen

fn mixed(_1: u32, _2: u32) -> u32 {
    debug x => _1;                       // in scope 0 at src/main.rs:46:10: 46:11
    debug z => _2;                       // in scope 0 at src/main.rs:46:18: 46:19
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:46:29: 46:32
    let _3: u32;                         // in scope 0 at src/main.rs:47:9: 47:10
    let mut _4: u32;                     // in scope 0 at src/main.rs:47:13: 47:22
    let mut _5: &u32;                    // in scope 0 at src/main.rs:47:13: 47:14
    let mut _6: u32;                     // in scope 0 at src/main.rs:47:25: 47:26
    let mut _7: (u32, bool);             // in scope 0 at src/main.rs:47:13: 47:26
    let _8: &u32;                        // in scope 0 at src/main.rs:48:5: 48:29
    let mut _9: &u32;                    // in scope 0 at src/main.rs:48:26: 48:28
    scope 1 {
        debug y => _3;                   // in scope 1 at src/main.rs:47:9: 47:10
    }

    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:47:9: 47:10
        StorageLive(_4);                 // scope 0 at src/main.rs:47:13: 47:22
        StorageLive(_5);                 // scope 0 at src/main.rs:47:13: 47:14
        _5 = &_2;                        // scope 0 at src/main.rs:47:13: 47:14
        _4 = <u32 as Clone>::clone(move _5) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:47:13: 47:22
    }

    bb1: {
        StorageDead(_5);                 // scope 0 at src/main.rs:47:21: 47:22
        StorageLive(_6);                 // scope 0 at src/main.rs:47:25: 47:26
        _6 = copy _1;                    // scope 0 at src/main.rs:47:25: 47:26
        _7 = AddWithOverflow(copy _4, copy _6); // scope 0 at src/main.rs:47:13: 47:26
        assert(!move (_7.1: bool), "attempt to compute `{} + {}`, which would overflow", move _4, move _6) -> [success: bb2, unwind continue]; // scope 0 at src/main.rs:47:13: 47:26
    }

    bb2: {
        _3 = move (_7.0: u32);           // scope 0 at src/main.rs:47:13: 47:26
        StorageDead(_6);                 // scope 0 at src/main.rs:47:25: 47:26
        StorageDead(_4);                 // scope 0 at src/main.rs:47:25: 47:26
        StorageLive(_8);                 // scope 1 at src/main.rs:48:5: 48:29
        StorageLive(_9);                 // scope 1 at src/main.rs:48:26: 48:28
        _9 = &_1;                        // scope 1 at src/main.rs:48:26: 48:28
        _8 = std::hint::black_box::<&u32>(move _9) -> [return: bb3, unwind continue]; // scope 1 at src/main.rs:48:5: 48:29
    }

    bb3: {
        StorageDead(_9);                 // scope 1 at src/main.rs:48:28: 48:29
        StorageDead(_8);                 // scope 1 at src/main.rs:48:29: 48:30
        _0 = copy _3;                    // scope 1 at src/main.rs:49:5: 49:6
        StorageDead(_3);                 // scope 0 at src/main.rs:50:1: 50:2
        return;                          // scope 0 at src/main.rs:50:2: 50:2
    }
}
"""


# Written by hand as rustc dumps it: `clone` inlined, inside a call of the crate's own.
NESTED_SOURCE = """fn keep(a: u32, b: &u32) -> u32 {
    a + *b
}

fn nested(x: u32, y: u32) -> u32 {
    keep(x.clone(), &y)
}
"""
NESTED = """// MIR for `nested` after PreCodegen

fn nested(_1: u32, _2: u32) -> u32 {
    debug x => _1;                       // in scope 0 at src/main.rs:5:11: 5:12
    debug y => _2;                       // in scope 0 at src/main.rs:5:19: 5:20
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:5:30: 5:33
    let mut _3: u32;                     // in scope 0 at src/main.rs:6:10: 6:19
    let mut _4: &u32;                    // in scope 0 at src/main.rs:6:10: 6:11
    let mut _5: &u32;                    // in scope 0 at src/main.rs:6:21: 6:23

    bb0: {
        StorageLive(_3);                 // scope 0 at src/main.rs:6:10: 6:19
        StorageLive(_4);                 // scope 0 at src/main.rs:6:10: 6:11
        _4 = &_1;                        // scope 0 at src/main.rs:6:10: 6:11
        _3 = <u32 as Clone>::clone(move _4) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:6:10: 6:19
    }

    bb1: {
        StorageDead(_4);                 // scope 0 at src/main.rs:6:18: 6:19
        StorageLive(_5);                 // scope 0 at src/main.rs:6:21: 6:23
        _5 = &_2;                        // scope 0 at src/main.rs:6:21: 6:23
        _0 = keep(move _3, copy _5) -> [return: bb2, unwind continue]; // scope 0 at src/main.rs:6:5: 6:24
    }

    bb2: {
        StorageDead(_5);                 // scope 0 at src/main.rs:6:23: 6:24
        StorageDead(_3);                 // scope 0 at src/main.rs:6:23: 6:24
        return;                          // scope 0 at src/main.rs:7:2: 7:2
    }
}
"""


def owned_of(text, name, inlined=None, source=SOURCE):
    """Peeky's proofs over the body of ``text`` (the function ``name``), its IR saying the
    calls ``inlined`` ({line: {column}}) were inlined."""
    body = parse_body(text)
    body.generics = ()
    blocks = {}
    for number, block in body.blocks.items():
        lines = {at for _, file, at in block.statements if file == "src/main.rs" and at}
        blocks["start" if number == 0 else f"bb{number}"] = lines
    declared = min(at for _, file, at in body.blocks[0].statements if file == "src/main.rs")
    function = IrFunction(
        name, "crate", "src/main.rs", blocks, {}, declared - 1, inlined=inlined or {}
    )
    texts = {"src/main.rs": source}
    return OwnedLocals([body], {HASH: function}, ("src/main.rs",), texts=texts)


AT = "/w/src/main.rs"
UNSAFE = "the crate has unsafe code (in src/main.rs)"


def test_a_call_inlined_before_the_stop_may_have_read_what_it_is_given():
    owned = owned_of(CLONED, "cloned", inlined={20: {15}})
    # The DWARF's inlined `clone` is called from column 15 of line 20 (`x.clone()`): it is
    # given `_4 = &_1`, a pointer to `x`; `k` it is not given.
    assert owned.inlined_reads(HASH, "x", 20, AT, ((15, "clone"),)) == (["clone"], "given")
    assert owned.inlined_reads(HASH, "k", 20, AT, ((15, "clone"),)) == ([], None)
    # Where DWARF gives no column, any call of the line may be it.
    assert owned.inlined_reads(HASH, "x", 20, AT, ((0, "clone"),)) == (["clone"], "given")
    # No call of the line spans the column: Peeky cannot tell which call it is.
    assert owned.inlined_reads(HASH, "x", 20, AT, ((40, "clone"),)) is None
    # Nothing inlined runs below the stop: nothing ran before it.
    assert owned.inlined_reads(HASH, "x", 20, AT, ()) == ([], None)
    # Where the stop's address is not known, every call the IR shows inlined on the line.
    assert owned.inlined_reads(HASH, "x", 20, AT, None) == (["clone"], "given")
    assert owned_of(CLONED, "cloned").inlined_reads(HASH, "x", 20, AT, None) == ([], None)
    # Then a line that may invoke a macro is not understood: its calls may be inlined from
    # the macro's own lines.
    macro = SOURCE.replace("let y = x.clone(); // CLONE", "let y = m!(x); // CLONE")
    assert owned_of(CLONED, "cloned", source=macro).inlined_reads(HASH, "x", 20, AT, None) is None


def test_the_debug_information_must_agree_with_the_ir_on_the_lines_inlined_calls():
    # DWARF with no inlined call of the line, where the IR shows one, said
    # "nothing ran before the stop". The two must name the same calls, wherever their code.
    owned = owned_of(CLONED, "cloned", inlined={20: {15}})
    agreed = owned.inlined_reads(HASH, "k", 20, AT, (), everywhere=frozenset({15}))
    assert agreed == ([], None)
    assert owned.inlined_reads(HASH, "k", 20, AT, (), everywhere=frozenset()) is None
    owned = owned_of(CLONED, "cloned")
    assert owned.inlined_reads(HASH, "k", 20, AT, (), everywhere=frozenset({15})) is None
    assert owned.inlined_reads(HASH, "k", 20, AT, (), everywhere=frozenset()) == ([], None)


def test_only_what_the_inlined_call_is_given_is_withheld():
    # `let y = z.clone() + x;`: the inlined `clone` reads `z`; the line's own code reads `x`
    # after the stop, so a change to `x` there reaches `y`.
    owned = owned_of(MIXED, "mixed")
    assert owned.inlined_reads(HASH, "z", 47, AT, ((15, "clone"),)) == (["clone"], "given")
    assert owned.inlined_reads(HASH, "x", 47, AT, ((15, "clone"),)) == ([], None)
    # In unsafe code a raw pointer may point anywhere: one given to the call may read `x`.
    pointer = MIXED.replace("let mut _5: &u32;", "let mut _5: *const u32;")
    owned = owned_of(pointer, "mixed")
    assert owned.inlined_reads(HASH, "x", 47, AT, ((15, "clone"),)) == ([], None)
    owned.unsafe_code = UNSAFE
    assert owned.inlined_reads(HASH, "x", 47, AT, ((15, "clone"),)) == (["clone"], "given")
    # In unsafe code any call may read it through a pointer it isn't given
    # (`static mut G: *const u32`): every local is withheld.
    owned = owned_of(MIXED, "mixed")
    owned.unsafe_code = UNSAFE
    assert owned.inlined_reads(HASH, "x", 47, AT, ((15, "clone"),)) == (["clone"], "unsafe")
    assert owned.inlined_reads(HASH, "z", 47, AT, ((15, "clone"),)) == (["clone"], "given")
    # Except the standard library's `ptr::eq`, which reads the two addresses, never what they
    # point at; given `z` itself, it reads it.
    clone = "_4 = <u32 as Clone>::clone(move _5)"
    assert MIXED.count(clone) == 1
    for given, verdict in (
        ("copy _5, copy _5", ([], None)),
        ("copy _2, copy _5", (["eq"], "given")),
    ):
        compared = MIXED.replace(clone, f"_4 = std::ptr::eq::<u32>({given})")
        owned = owned_of(compared, "mixed")
        assert owned._std_compares()
        owned.unsafe_code = UNSAFE
        assert owned.inlined_reads(HASH, "z", 47, AT, ((15, "eq"),)) == verdict
        assert owned.inlined_reads(HASH, "x", 47, AT, ((15, "eq"),)) == ([], None)
    # Each under the root the dump names it by: a crate's `mod std`
    # may be what `std::ptr::eq` names, and leaves `core::ptr::eq` the standard library's.
    for root, verdict in (("std", (["eq"], "unsafe")), ("core", ([], None))):
        compared = MIXED.replace(clone, f"_4 = {root}::ptr::eq::<u32>(copy _5, copy _5)")
        owned = owned_of(compared, "mixed", source=SOURCE + "mod std {}\n")
        assert owned._std_compares() == {"core"}
        owned.unsafe_code = UNSAFE
        assert owned.inlined_reads(HASH, "x", 47, AT, ((15, "eq"),)) == verdict


def test_an_enclosing_call_is_not_the_inlined_one_its_name_tells():
    # `keep(x.clone(), &y)`: the inlined `clone` is at column 12, which `keep`'s span holds
    # too; `keep` is given `&y`, but it runs after the stop. By the name DWARF gives it,
    # only `clone` is the inlined call.
    owned = owned_of(NESTED, "nested", source=NESTED_SOURCE)
    assert owned.inlined_reads(HASH, "x", 6, AT, ((12, "clone"),)) == (["clone"], "given")
    assert owned.inlined_reads(HASH, "y", 6, AT, ((12, "clone"),)) == ([], None)
    # Without a name, either may be it.
    assert owned.inlined_reads(HASH, "y", 6, AT, ((12, ""),)) == (["keep"], "given")
    # A name no call there has (a closure's call): any of them may be it.
    assert owned.inlined_reads(HASH, "y", 6, AT, ((12, "{closure#0}"),)) == (["keep"], "given")
    # `keep`'s own column.
    assert owned.inlined_reads(HASH, "y", 6, AT, ((5, "keep"),)) == (["keep"], "given")


def test_a_macros_definition_lines_are_read_from_the_source():
    text = (
        "use std::env;\n"
        "\n"
        "macro_rules! mclone {\n"
        "    ($v:expr) => {{ $v.clone() }};\n"
        "}\n"
        "// macro_rules! commented { }\n"
        "fn f() {\n"
        '    let s = "macro_rules! quoted {}";\n'
        "    macro_rules! inner ( () => { 1 } );\n"
        "}\n"
        "macro m($x:expr) {\n"
        "    $x\n"
        "}\n"
        "fn g() {}\n"
    )
    assert sorted(macro_lines(text)) == [3, 4, 5, 9, 11, 12, 13]
    # A source not read as rustc reads it, or not known: none is known.
    assert macro_lines('fn f() { "x') is None
    assert macro_lines(None) is None


def test_a_calls_operands_name_and_columns():
    call = "_3 = <u32 as Clone>::clone(move _4) -> [return: bb2, unwind continue];"
    assert _call_operands(call) == {4}
    assert _callee(call) == "clone"
    other = "_9 = f(copy (_1.0: u32), const 1_usize, move _12) -> [return: bb3, unwind: bb7];"
    assert _call_operands(other) == {1, 12}
    assert _callee(other) == "f"
    parsed = "_2 = core::str::<impl str>::parse::<u32>(copy _3) -> [return: bb5, unwind: bb12];"
    assert _callee(parsed) == "parse"
    assert _callee("_17 = Box::<[u32; 3]>::new_uninit() -> [return: bb10, unwind: bb74];") == (
        "new_uninit"
    )
    # A call's span holds the column (bytes), on its line or a later one it reaches; a call
    # spanning another file (a macro's) is the one at a macro's path on the line.
    facts = SimpleNamespace(macros=frozenset({13}), known=True, to_bytes=lambda column: column)
    block = SimpleNamespace(
        statements=[(call, "src/main.rs", 20), (call, "/rustc/a/macros.rs", 59)],
        spans=[(13, 20, 22), (52, 59, 84)],
    )
    assert [_covers(block, 0, column, 20, "src/main.rs", facts) for column in (12, 15, 22, 23)] == [
        False,
        True,
        True,
        False,
    ]
    assert _covers(block, 0, 0, 20, "src/main.rs", facts)
    assert _covers(block, 0, 99, 21, "src/main.rs", facts) is False
    block.spans[0] = (13, 21, 4)
    assert _covers(block, 0, 99, 20, "src/main.rs", facts)
    assert _covers(block, 0, 2, 21, "src/main.rs", facts)
    assert [_covers(block, 1, column, 20, "src/main.rs", facts) for column in (13, 15)] == [
        True,
        False,
    ]
    facts.known = False
    assert _covers(block, 1, 15, 20, "src/main.rs", facts)


# A source for the stops below: SOURCE with a macro's definition at lines 3 to 5.
STRUCT = "struct P {\n    a: u32,\n    b: u32,\n}"
STOPPED = SOURCE.replace(STRUCT, "macro_rules! mclone {\n    ($v:expr) => {{ $v.clone() }};\n}\n")
assert STRUCT in SOURCE and STOPPED.count("\n") == SOURCE.count("\n")


def stopped(tmp_path, pc, blocks, line=20, text=STOPPED):
    """A RustSession stopped at ``pc`` (unslid) on ``line`` of `cloned`, with its DWARF
    ``blocks``: line 19's own row at 0x1a0, line 20's at 0x1b8, line 21's at 0x1c0. The
    stop's source reads ``text``."""
    session = object.__new__(RustSession)
    session.code_blocks = {HASH: blocks}
    session.prepared = SimpleNamespace(workspace=tmp_path)
    session.owned = SimpleNamespace(texts={"src/main.rs": text} if text else {})
    session.load_bias = 0x4000
    session._rows = [
        (0x1A0, "src/main.rs", 19),
        (0x1B8, "src/main.rs", 20),
        (0x1C0, "src/main.rs", 21),
    ]
    frame = {
        "name": f"inlined::cloned::{HASH}",
        "line": line,
        "column": 15,
        "source": {"path": str(tmp_path / "src/main.rs")},
        "instructionPointerReference": hex(0x4000 + pc),
    }
    return session, frame


def inlined(tmp_path, low, high, line, column=0, name="", file=None):
    """An inlined call's DWARF block in `cloned`, made from ``line``."""
    site = (file or str(Path(tmp_path) / "src/main.rs"), line, column, name)
    return CodeBlock(0, ((low, high),), (), True, site)


def blocks_of(tmp_path, *more):
    return (
        CodeBlock(None, ((0x100, 0x200),), ("x",)),
        # `clone` inlined from line 20, column 15, below line 20's own row.
        inlined(tmp_path, 0x1B0, 0x1B8, 20, 15, "clone"),
        # `identity`'s from line 19, before line 19's own row.
        inlined(tmp_path, 0x190, 0x198, 19, 13, "identity"),
        *more,
    )


def test_the_stop_is_past_the_lines_inlined_code_where_its_breakpoint_is(tmp_path):
    blocks = blocks_of(tmp_path)
    clone = ((15, "clone"),)
    # LLDB's breakpoint on line 20: its first row of its own, past the inlined `clone`.
    session, frame = stopped(tmp_path, 0x1B8, blocks)
    assert session._inlined_below(frame) == (clone, None, {15})
    assert session._line_start(frame)
    # A step from line 19 stops at the inlined call's first instruction, before it runs.
    session, frame = stopped(tmp_path, 0x1B0, blocks)
    assert session._inlined_below(frame) == ((), None, {15})
    assert session._line_start(frame)
    # Inside the inlined code: neither where the line starts nor its breakpoint.
    session, frame = stopped(tmp_path, 0x1B4, blocks)
    assert session._inlined_below(frame) == (clone, None, {15})
    assert not session._line_start(frame)
    # Line 21's stop: line 20's inlined call is not its own.
    session, frame = stopped(tmp_path, 0x1C0, blocks, line=21)
    assert session._inlined_below(frame) == ((), None, frozenset())
    assert session._line_start(frame)
    # Without the function's DWARF blocks, nothing is known.
    session, frame = stopped(tmp_path, 0x1B8, ())
    assert session._inlined_below(frame) is None
    assert not session._line_start(frame)


def test_code_just_before_the_stop_that_peeky_cannot_place_is_a_doubt(tmp_path):
    # `let y = mclone!(x) + 1;` inlines `clone` from the macro's definition,
    # line 4, just before line 20's first row: its site is not line 20.
    session, frame = stopped(
        tmp_path, 0x1B8, blocks_of(tmp_path, inlined(tmp_path, 0x1A8, 0x1AC, 4))
    )
    assert session._inlined_below(frame) == (
        ((15, "clone"),),
        "code the build inlined from line 4 (a macro's definition)",
        {15},
    )
    # Its row of line 4 is no statement's: it does not end the code just before the stop.
    session, frame = stopped(
        tmp_path, 0x1B8, blocks_of(tmp_path, inlined(tmp_path, 0x1A8, 0x1AC, 4))
    )
    session._rows = sorted([*session._rows, (0x1AC, "src/main.rs", 4)])
    assert (
        session._inlined_below(frame)[1]
        == "code the build inlined from line 4 (a macro's definition)"
    )
    # Before line 19's row, the macro's code is line 19's statement's.
    session, frame = stopped(
        tmp_path, 0x1B8, blocks_of(tmp_path, inlined(tmp_path, 0x194, 0x198, 4))
    )
    assert session._inlined_below(frame)[1] is None
    # Another file's code (a macro from another module), and a site not given.
    elsewhere = inlined(tmp_path, 0x1A4, 0x1A8, 7, file=str(tmp_path / "src/macros.rs"))
    session, frame = stopped(tmp_path, 0x1B8, blocks_of(tmp_path, elsewhere))
    assert session._inlined_below(frame)[1] == (
        "code the build inlined from a call at src/macros.rs:7 (another file's code, a macro's)"
    )
    unread = CodeBlock(0, ((0x1A4, 0x1A8),), (), True, ())
    session, frame = stopped(tmp_path, 0x1B8, blocks_of(tmp_path, unread))
    assert session._inlined_below(frame)[1] == (
        "code the build inlined from a call its debug information gives no site for"
    )
    # A statement's call of line 19 that ends its code: line 19's, which the MIR judges.
    session, frame = stopped(
        tmp_path, 0x1B8, blocks_of(tmp_path, inlined(tmp_path, 0x1A4, 0x1A8, 19))
    )
    assert session._inlined_below(frame)[1] is None
    # A source not read: no line is known to be a statement's.
    session, frame = stopped(tmp_path, 0x1B8, blocks_of(tmp_path), text=None)
    assert session._inlined_below(frame)[1] == (
        "code the build inlined before the stop, where the line's source isn't read"
    )
    # A step onto line 20 that stops at the macro's code (as LLDB shows line 20 there)
    # stops where the line's code starts.
    session, frame = stopped(
        tmp_path, 0x1A8, blocks_of(tmp_path, inlined(tmp_path, 0x1A8, 0x1AC, 4))
    )
    assert session._line_start(frame)


def test_the_breakpoint_withholds_what_the_inlined_call_was_given(tmp_path):
    def at(pc, *more, owned=None):
        session, frame = stopped(tmp_path, pc, blocks_of(tmp_path, *more))
        session.owned = owned or owned_of(CLONED, "cloned", inlined={20: {15}}, source=STOPPED)
        session._optimized = lambda: False
        session._stop_block = lambda hashed, frame: None
        return session, frame

    session, frame = at(0x1B8)
    found = session.mid_statement({"name": "x"}, frame, "breakpoint")
    assert str(found) == (
        "line 20's code may already have read `x` before this stop: the build inlined `clone` "
        "there, which is given `x` or what may point into it, and that code runs before the "
        "line's first instruction of its own, where a line breakpoint stops"
    )
    assert found.row == "line 20's inlined `clone` may have read it"
    assert found.advice == (
        "Change it at a stop on an earlier line (a probe's `--at`), or reach line 20 with "
        "`session step` from the line before it, which stops before that code"
    )
    # `k` was not given to it; a step's stop before the inlined code withholds nothing so.
    assert session._inlined_read({"name": "k"}, frame, HASH, session.owned) is None
    session, frame = at(0x1B0)
    assert session._inlined_read({"name": "x"}, frame, HASH, session.owned) is None
    # A step that lands inside it is part-way through the line, as before.
    session, frame = at(0x1B4)
    assert session.mid_statement({"name": "x"}, frame, "step").row == "part-way through line 20"
    # Code just before the stop that Peeky can't place: every local, and no step offered.
    session, frame = at(0x1B8, inlined(tmp_path, 0x1A8, 0x1AC, 4))
    found = session._inlined_read({"name": "k"}, frame, HASH, session.owned)
    assert str(found) == (
        "line 20's code may already have read `k` before this stop: code the build inlined "
        "from line 4 (a macro's definition) runs just before the stop, and Peeky cannot tell "
        "whether it is this line's code or what it read"
    )
    assert found.row == "code inlined before the stop may have read it"
    assert found.advice == (
        "Change it at a stop on an earlier line (a probe's `--at`), where `inspect` marks it "
        "[settable]"
    )
    # A call the MIR cannot place, or DWARF and the IR at odds: withheld, saying so.
    for owned in (
        owned_of(CLONED, "cloned", inlined={20: {15, 40}}, source=STOPPED),
        owned_of(CLONED, "cloned", source=STOPPED),
    ):
        session, frame = at(0x1B8, owned=owned)
        found = session._inlined_read({"name": "k"}, frame, HASH, session.owned)
        assert found.row == "line 20's inlined code may have read it"
        assert "Peeky cannot tell from the build's MIR and IR which call it is" in found
    # Unsafe code may read `k` through a pointer `clone` isn't given.
    session, frame = at(0x1B8)
    session.owned.unsafe_code = UNSAFE
    found = session._inlined_read({"name": "k"}, frame, HASH, session.owned)
    assert str(found) == (
        "line 20's code may already have read `k` before this stop: the build inlined `clone` "
        "there, and that code runs before the line's first instruction of its own, where a "
        "line breakpoint stops. The crate has unsafe code, so that code may read `k` through "
        "a pointer it isn't given, such as one a `static mut` holds"
    )
    assert found.row == "line 20's inlined `clone` may have read it (unsafe code)"
    assert "with `session step` from the line before it" in found.advice


def test_the_refusal_hints_a_stop_before_the_inlined_code():
    found = classify(
        ValueError(
            "'x' cannot be changed here: line 20's code may already have read `x` before this "
            "stop: the build inlined `clone` there, which is given `x` or what may point into "
            "it, and that code runs before the line's first instruction of its own"
        )
    )
    assert found["code"] == "MUTATION_REJECTED", found
    assert found["hint"] == (
        "Change it at a stop on an earlier line (`--at`, `session continue --to LINE`), or "
        "`session step` onto the line from the one before it."
    )
    assert len(found["hint"]) <= 150
    # Code Peeky can't place: no step is offered.
    found = classify(
        ValueError(
            "'x' cannot be changed here: line 20's code may already have read `x` before this "
            "stop: code the build inlined from line 4 (a macro's definition) runs just before "
            "the stop, and Peeky cannot tell whether it is this line's code or what it read"
        )
    )
    assert found["hint"] == (
        "Change it at a stop on an earlier line (`--at`, `session continue --to LINE`), "
        "where `inspect` marks it [settable]."
    )
    assert len(found["hint"]) <= 150
