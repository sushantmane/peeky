"""The owned-local proof (rust_mir): MIR parsing, initialization dataflow and IR blocks."""

import subprocess
import sys

import pytest

from peeky.rust_mir import (
    IrFunction,
    OwnedLocals,
    _segments,
    ir_functions,
    may_invoke_macro,
    parse_body,
)


def mir(blocks, header="fn main() -> () {", bindings=""):
    """A `-Zdump-mir` body: blocks are {number: [statement, ..., terminator]}."""
    lines = [
        "// MIR for `main` after PreCodegen",
        "",
        header,
        "    let mut _0: ();                      // return place in scope 0 at src/main.rs:1:10: 1:10",
        "    let _1: std::vec::Vec<i32>;          // in scope 0 at src/main.rs:2:9: 2:10",
        "    scope 1 {",
        *(f"        {line}" for line in bindings.splitlines() if line),
        "    }",
        "",
    ]
    for number, statements in blocks.items():
        lines.append(f"    bb{number}: {{")
        for index, statement in enumerate(statements):
            lines.append(f"        {statement}  // scope 1 at src/main.rs:{10 + index}:5: 10:9")
        lines += ["    }", ""]
    return parse_body("\n".join([*lines, "}", ""]))


SOURCES = ("src/main.rs", "src/macros.rs")
MAIN = "/w/src/main.rs"
V = "debug v => _1;   // in scope 1 at src/main.rs:2:9: 2:10"


def test_moves_drops_and_storage_end_a_local():
    body = mir(
        {
            0: ["_1 = make() -> [return: bb1, unwind: bb5];"],
            1: ["_2 = &_1;", "goto -> bb2;"],
            2: ["_3 = move _1;", "goto -> bb3;"],
            3: ["_1 = make() -> [return: bb4, unwind: bb5];"],
            4: ["drop(_1) -> [return: bb6, unwind: bb5];"],
            5: ["resume;"],
            6: ["StorageDead(_1);", "return;"],
        },
        bindings=V,
    )
    assert body.supported and body.local("v") == 1
    assert body.initialized_throughout(1, {1})
    assert not body.initialized_throughout(1, {0})  # assigned by the call: not yet
    assert not body.initialized_throughout(1, {2})  # moved inside the block
    assert not body.initialized_throughout(1, {3})  # moved before it
    assert body.initialized_throughout(1, {4}) is False  # dropped inside the block
    assert not body.initialized_throughout(1, {5})  # the unwind edge never set it
    assert not body.initialized_throughout(1, {6})
    assert not body.initialized_throughout(1, {1, 2})  # every block must hold


def test_a_move_on_one_path_leaves_the_local_maybe_moved():
    body = mir(
        {
            0: ["_1 = make() -> [return: bb1, unwind continue];"],
            1: ["switchInt(copy _5) -> [0: bb2, otherwise: bb3];"],
            2: ["_6 = consume(move _1) -> [return: bb4, unwind continue];"],
            3: ["goto -> bb4;"],
            4: ["_7 = const ();", "return;"],
        },
        bindings=V,
    )
    assert body.initialized_throughout(1, {3}) and not body.initialized_throughout(1, {4})


def test_a_loop_reaches_its_greatest_fixpoint():
    body = mir(
        {
            0: ["_1 = make() -> [return: bb1, unwind continue];"],
            1: ["_2 = &mut _1;", "_3 = push(move _2) -> [return: bb2, unwind continue];"],
            2: ["switchInt(copy _4) -> [0: bb1, otherwise: bb3];"],
            3: ["return;"],
        },
        bindings=V,
    )
    assert body.initialized_throughout(1, {1, 2, 3})


def test_unreachable_blocks_and_unknown_shapes_prove_nothing():
    body = mir(
        {0: ["_1 = make() -> [return: bb1, unwind continue];"], 1: ["return;"], 2: ["return;"]}
    )
    assert not body.initialized_throughout(1, {2})
    assert not mir({0: ["goto -> bb7;"]}).initialized_throughout(1, {0})
    assert not mir({0: ["yield(move _1) -> [resume: bb1, drop: bb1];"], 1: ["return;"]}).supported
    no_span = parse_body(
        "fn main() -> () {\n"
        "    let mut _0: ();  // return place in scope 0 at src/main.rs:1:10: 1:10\n"
        "    bb0: {\n        return;\n    }\n}\n"
    )
    assert not no_span.supported


def test_a_string_constant_cannot_hide_a_move():
    tricky = '_2 = foo(const "a;  // scope 9 at x.rs:1:1: 1:2", move _1) -> [return: bb1, unwind continue];'
    body = mir(
        {0: ["_1 = make() -> [return: bb1, unwind continue];"], 1: [tricky], 2: ["return;"]},
        bindings=V,
    )
    assert body.blocks[1].terminator == tricky and not body.initialized_throughout(1, {1})


def test_names_map_to_one_whole_local_binding():
    body = mir(
        {0: ["return;"]},
        bindings="\n".join(
            [
                "debug s => _1;  // in scope 1 at src/main.rs:7:9: 7:10",
                "debug s => _2;  // in scope 1 at src/main.rs:8:9: 8:10",
                "debug p => (_3.0: i32);  // in scope 1 at src/main.rs:9:9: 9:10",
                "debug only => _4;  // in scope 1 at src/main.rs:9:9: 9:10",
            ]
        ),
    )
    assert body.local("s") is None  # shadowed: lldb-dap names each `s @ main.rs:N`
    assert body.local("s", ("main.rs", 8)) == 2 and body.local("s", ("main.rs", 9)) is None
    assert body.local("p") is None and body.local("only") == 4 and body.local("gone") is None


def test_paths_split_on_their_own_separators():
    assert _segments("<impl at src/a.rs:2:1: 2:11>::push_all") == [
        "<impl at src/a.rs:2:1: 2:11>",
        "push_all",
    ]
    assert _segments("main::{closure#0}") == ["main", "{closure#0}"]


def test_ir_blocks_map_to_mir_blocks_through_codegen_helpers():
    function = IrFunction(
        "main",
        "app",
        "src/main.rs",
        {"start": {3}, "bb4": {5}, "panic": {5, 9}, "cleanup": {9}, "bb7": {6}},
        {"panic": ["bb4"], "cleanup": ["panic2"], "panic2": []},
    )
    assert function.mir_blocks(3) == {0} and function.mir_blocks(5) == {4}
    assert function.mir_blocks(6) == {7} and function.mir_blocks(42) is None
    assert function.mir_blocks(9) is None  # a helper of unknown origin proves nothing


IR = """define internal void @_ZN3app4main17h0123456789abcdefE() unnamed_addr #0 !dbg !10 {
start:
  %x = alloca [24 x i8], align 8
  call void @make(ptr %x), !dbg !20
  br label %bb1, !dbg !21

bb1:                                              ; preds = %start
  call void @llvm.dbg.declare(metadata ptr %x, metadata !30, metadata !DIExpression()), !dbg !22
  call void @inlined(), !dbg !23
  br i1 %c, label %bb2, label %panic, !dbg !22

panic:                                            ; preds = %bb1
  call void @panic(), !dbg !22

bb2:                                              ; preds = %bb1
  ret void, !dbg !24
}

define internal void @_ZN4core3ptr13drop_in_place17hfedcba9876543210E() !dbg !40 {
start:
  ret void, !dbg !41
}

!2 = !DIFile(filename: "src/main.rs", directory: "/w")
!3 = !DIFile(filename: "/rustc/x/library/core/src/ptr/mod.rs", directory: "")
!9 = !DINamespace(name: "app", scope: null)
!10 = distinct !DISubprogram(name: "main", linkageName: "_ZN3app4main17h0123456789abcdefE", scope: !9, file: !2, line: 1, unit: !1)
!11 = distinct !DILexicalBlock(scope: !10, file: !2, line: 2, column: 5)
!20 = !DILocation(line: 3, column: 13, scope: !11)
!21 = !DILocation(line: 0, scope: !10)
!22 = !DILocation(line: 4, column: 5, scope: !11)
!23 = !DILocation(line: 99, column: 5, scope: !10, inlinedAt: !22)
!24 = !DILocation(line: 5, column: 1, scope: !10)
!40 = distinct !DISubprogram(name: "drop_in_place<i32>", linkageName: "_ZN4core3ptr13drop_in_place17hfedcba9876543210E", scope: !9, file: !3, line: 1, unit: !1)
!41 = !DILocation(line: 1, scope: !40)
"""


def test_ir_functions_record_the_lines_of_each_block(tmp_path):
    path = tmp_path / "app.ll"
    path.write_text(IR)
    functions = ir_functions(path, ["src/main.rs"])
    assert list(functions) == ["h0123456789abcdef"]  # drop_in_place is not in a source
    function = functions["h0123456789abcdef"]
    assert (function.name, function.parent, function.file) == ("main", "app", "src/main.rs")
    # Line 0, inlined code and debug intrinsics are not code of this function's lines.
    assert function.blocks == {"start": {3}, "bb1": {4}, "panic": {4}, "bb2": {5}}
    assert function.preds["panic"] == ["bb1"] and function.mir_blocks(4) == {1}


def test_a_function_is_proven_only_against_a_single_matching_body(tmp_path):
    path = tmp_path / "app.ll"
    path.write_text(IR)
    functions = ir_functions(path, ["src/main.rs"])
    body = mir(
        {0: ["_1 = make() -> [return: bb1, unwind continue];"], 1: ["return;"], 2: ["return;"]},
        bindings=V,
    )
    body.blocks[0].statements[-1] = (body.blocks[0].terminator, "src/main.rs", 3)
    owned = OwnedLocals([body], functions, SOURCES)
    assert owned.body("h0123456789abcdef") is body
    assert owned.proven("h0123456789abcdef", "v", 4, MAIN) and not owned.proven(
        "h0123456789abcdef", "v", 3, MAIN
    )
    assert not owned.proven("h0123456789abcdef", "v @ main.rs:x", 4, MAIN)
    assert not owned.proven("hffffffffffffffff", "v", 4, MAIN)
    assert OwnedLocals([body, body], functions, SOURCES).body("h0123456789abcdef") is None


def test_a_local_assigned_earlier_in_the_block_is_proven_at_a_later_line():
    body = parse_body(
        "fn main() -> () {\n"
        "    let mut _0: ();  // return place in scope 0 at src/main.rs:1:10: 1:10\n"
        "    scope 1 {\n"
        "        debug w => _2;  // in scope 1 at src/main.rs:3:9: 3:10\n"
        "    }\n"
        "    bb0: {\n"
        "        _1 = make() -> [return: bb1, unwind continue];  // scope 0 at src/main.rs:2:13: 2:19\n"
        "    }\n"
        "    bb1: {\n"
        "        _2 = move _1;  // scope 1 at src/main.rs:3:13: 3:14\n"
        "        _3 = &_2;  // scope 1 at src/main.rs:4:13: 4:14\n"
        "        _4 = len(move _3) -> [return: bb2, unwind continue];  // scope 1 at src/main.rs:4:13: 4:20\n"
        "    }\n"
        "    bb2: {\n"
        '        _5 = core::panicking::panic(const "x") -> unwind continue;  // scope 1 at /rustc/x/library/core/src/macros.rs:9:9: 9:20\n'
        "    }\n"
        "}\n"
    )
    assert body.supported and body.local("w") == 2
    assert body.initialized_at(2, {1}, 4, "src/main.rs")  # after `let w = v;`
    assert not body.initialized_at(2, {1}, 3, "src/main.rs")  # on that line: not yet
    assert not body.initialized_at(1, {1}, 4, "src/main.rs")  # `v` was moved into `w`
    # Code from an external macro may carry any invoking line: checked where it is.
    assert body.initialized_at(2, {2}, 4, "src/main.rs")
    assert not body.initialized_at(3, {2}, 4, "src/main.rs")


def test_a_block_holding_only_a_callers_result_store_is_checked_at_its_start():
    body = parse_body(
        "fn main() -> () {\n"
        "    let mut _0: ();  // return place in scope 0 at src/main.rs:1:10: 1:10\n"
        "    scope 1 {\n"
        "        debug acc => _1;  // in scope 1 at src/main.rs:2:9: 2:12\n"
        "    }\n"
        "    bb0: {\n"
        "        _1 = make() -> [return: bb1, unwind continue];  // scope 0 at src/main.rs:2:15: 2:21\n"
        "    }\n"
        "    bb1: {\n"
        "        _3 = &_1;  // scope 1 at src/main.rs:3:13: 3:16\n"
        "        _2 = len(move _3) -> [return: bb2, unwind continue];  // scope 1 at src/main.rs:3:13: 3:22\n"
        "    }\n"
        "    bb2: {\n"
        "        StorageDead(_3);  // scope 1 at src/main.rs:3:21: 3:22\n"
        "        _4 = move _1;  // scope 1 at src/main.rs:4:15: 4:18\n"
        "        _5 = consume(move _4) -> [return: bb3, unwind continue];  // scope 1 at src/main.rs:4:5: 4:19\n"
        "    }\n"
        "    bb3: {\n"
        "        return;  // scope 0 at src/main.rs:5:2: 5:2\n"
        "    }\n"
        "}\n"
    )
    # Line 3 has code in bb1 and, through len's result store, at bb2's start: the
    # move on line 4 comes after it.
    assert body.initialized_at(1, {1, 2}, 3, "src/main.rs", 1)
    # Without the checked caller, bb2's line-3 code is unexplained: the whole block,
    # move included, must keep the local.
    assert not body.initialized_at(1, {2}, 3, "src/main.rs", 1)
    # bb0's start is the prologue only on the function's own line.
    assert not body.initialized_at(1, {0}, 1, "src/main.rs", 1)


def test_a_stop_after_a_drop_on_the_line_is_never_proven():
    body = parse_body(
        "fn main() -> () {\n"
        "    let mut _0: ();  // return place in scope 0 at src/main.rs:1:10: 1:10\n"
        "    scope 1 {\n"
        "        debug v => _1;  // in scope 1 at src/main.rs:2:9: 2:10\n"
        "    }\n"
        "    bb0: {\n"
        "        _1 = make() -> [return: bb1, unwind continue];  // scope 0 at src/main.rs:2:13: 2:19\n"
        "    }\n"
        "    bb1: {\n"
        "        drop(_1) -> [return: bb2, unwind continue];  // scope 1 at src/main.rs:3:5: 3:6\n"
        "    }\n"
        "    bb2: {\n"
        "        _2 = other() -> [return: bb3, unwind continue];  // scope 0 at src/main.rs:4:5: 4:12\n"
        "    }\n"
        "    bb3: {\n"
        "        return;  // scope 0 at src/main.rs:5:2: 5:2\n"
        "    }\n"
        "}\n"
    )
    # Before the drop `v` is valid, but `step --out` from drop_in_place returns to
    # line 3 after it: the line as a whole proves nothing.
    assert not body.initialized_at(1, {1}, 3, "src/main.rs", 1)


def chain_body():
    """bb1 calls a function that cannot unwind; codegen merges bb2 (a move) into bb1."""
    body = mir(
        {
            0: ["_1 = make() -> [return: bb1, unwind continue];"],
            1: ["_2 = getpid() -> [return: bb2, unwind unreachable];"],
            2: ["_3 = move _1;", "goto -> bb3;"],
            3: ["return;"],
        },
        bindings=V,
    )
    return body


def test_blocks_merged_into_a_labeled_block_are_checked_too():
    body = chain_body()
    # bb2 has no IR label: its move runs inside bb1's IR block, at line 5.
    function = IrFunction(
        "main", "app", "src/main.rs", {"start": {2}, "bb1": {4, 5}, "bb3": {6}}, {}
    )
    function.line = 1
    owned = OwnedLocals([body], {"h0": function}, SOURCES)
    assert owned.body("h0") is body
    assert not owned.proven("h0", "v", 5, MAIN) and not owned.proven("h0", "v", 4, MAIN)
    # Labeled on its own, bb1 alone holds the line, and nothing in it moves `v`.
    labeled = IrFunction(
        "main", "app", "src/main.rs", {"start": {2}, "bb1": {4}, "bb2": {5}, "bb3": {6}}, {}, 1
    )
    assert OwnedLocals([chain_body()], {"h1": labeled}, SOURCES).proven("h1", "v", 4, MAIN)


def test_an_unlabeled_block_codegen_could_not_have_merged_proves_nothing():
    body = mir(
        {
            0: ["_1 = make() -> [return: bb1, unwind continue];"],
            1: ["switchInt(copy _4) -> [0: bb2, otherwise: bb3];"],
            2: ["goto -> bb4;"],
            3: ["goto -> bb4;"],
            4: ["_3 = move _1;", "return;"],  # two predecessors, yet no IR block
        },
        bindings=V,
    )
    function = IrFunction(
        "main", "app", "src/main.rs", {"start": {2}, "bb1": {4}, "bb2": {5}, "bb3": {5}}, {}, 1
    )
    owned = OwnedLocals([body], {"h0": function}, SOURCES)
    assert owned.body("h0") is None and not owned.proven("h0", "v", 4, MAIN)


def test_a_body_is_identified_by_its_own_lines_not_by_elimination():
    def new(first, calls):
        body = mir(
            {0: ["_1 = make() -> [return: bb1, unwind continue];"], 1: ["return;"]},
            header="fn <impl at src/main.rs:1:1: 1:7>::new() -> () {",
            bindings=V,
        )
        body.lines = frozenset(range(first, first + 9))
        body.blocks[0].statements[-1] = (body.blocks[0].terminator, "src/main.rs", calls)
        return body

    a, b = new(19, 22), new(29, 31)
    # A's line 22 (an always-inlined call) left no code of its own in the IR.
    function = IrFunction(
        "new", "A", "src/main.rs", {"start": {20}, "bb1": {21, 23, 24, 26}}, {}, 19
    )
    assert OwnedLocals([a, b], {"h": function}, SOURCES).body("h") is a
    assert OwnedLocals([b], {"h": function}, SOURCES).body("h") is None


def test_a_stop_in_another_file_is_never_checked_against_this_files_line(tmp_path):
    path = tmp_path / "app.ll"
    path.write_text(IR)
    functions = ir_functions(path, ["src/main.rs"])
    body = mir(
        {0: ["_1 = make() -> [return: bb1, unwind continue];"], 1: ["return;"], 2: ["return;"]},
        bindings=V,
    )
    owned = OwnedLocals([body], functions, SOURCES)
    assert owned.proven("h0123456789abcdef", "v", 4, MAIN)
    # A macro defined in macros.rs, expanded here, stops this frame at macros.rs:4.
    assert not owned.proven("h0123456789abcdef", "v", 4, "/w/src/macros.rs")
    assert not owned.proven("h0123456789abcdef", "v", 4, None)
    # A path that two declared sources end with is ambiguous.
    nested = OwnedLocals([body], functions, ("src/main.rs", "x/src/main.rs"))
    assert not nested.proven("h0123456789abcdef", "v", 4, "/w/x/src/main.rs")


def test_spans_of_a_macro_defined_above_do_not_hide_the_body():
    body = mir({0: ["_1 = make() -> [return: bb1, unwind continue];"], 1: ["return;"]}, bindings=V)
    body.lines = frozenset({2, 3, 8, 9, 10, 11, 12})  # 2-3: a macro_rules! above `fn`
    # Line 3: code of the local macro, which keeps its own lines in debug info.
    function = IrFunction("main", "app", "src/main.rs", {"start": {8}, "bb1": {3, 9, 12}}, {}, 7)
    assert OwnedLocals([body], {"h": function}, SOURCES).body("h") is None  # no span on 7
    body.lines |= {7}
    assert OwnedLocals([body], {"h": function}, SOURCES).body("h") is body
    # External macros a local macro expands report the local macro's line: still it.
    function.blocks["bb1"].add(1)
    assert OwnedLocals([body], {"h": function}, SOURCES).body("h") is body


def test_twins_a_macro_generates_are_refused_not_told_apart():
    def run(lines):
        body = mir(
            {0: ["_1 = make() -> [return: bb1, unwind continue];"], 1: ["return;"]},
            header="fn <impl at src/main.rs:8:1: 8:7>::run() -> () {",
            bindings=V,
        )
        body.lines = frozenset(lines)
        return body

    # Both expansions put their `fn`, return place and closing brace on the macro's
    # line 3; only their bodies' tokens differ.
    a, b = run({3, 12, 13, 14}), run({3, 21, 22, 23, 24})
    function = IrFunction("run", "A", "src/main.rs", {"start": {3, 12}, "bb1": {13, 14}}, {}, 3)
    assert OwnedLocals([a, b], {"h": function}, SOURCES).body("h") is None
    assert OwnedLocals([a], {"h": function}, SOURCES).body("h") is a


def test_a_twin_whose_fn_line_matches_alone_is_refused_by_the_other_twins_lines():
    def numbers(lines):
        body = mir(
            {0: ["_1 = make() -> [return: bb1, unwind continue];"], 1: ["return;"]},
            header="fn <impl at src/main.rs:3:9: 3:16>::numbers(_1: i32) -> Vec<i32> {",
            bindings=V,
        )
        body.lines = frozenset(lines)
        return body

    # A (lines 15-21) has no cleanup, so no span on the shared `fn` line 4; B has one.
    a, b = numbers({15, 16, 17, 18, 19, 21}), numbers({4, 24, 25, 26, 27})
    function = IrFunction(
        "numbers", "A", "src/main.rs", {"start": {15, 16}, "bb1": {17, 18, 21}}, {}, 4
    )
    assert OwnedLocals([a, b], {"h": function}, SOURCES).body("h") is None
    # Identical twins are told apart by identity, never by equality.
    twin = numbers({4, 15, 16, 17, 18, 21})
    alone = OwnedLocals([twin], {"h": function}, SOURCES)
    assert alone.body("h") is twin


WRAPPER_RUSTC = (
    "import os, sys; print(os.environ.get('RUSTC_BOOTSTRAP', '-'), "
    "os.environ.get('CARGO_BIN_EXE_my-app', '-'), *sys.argv[1:])"
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shebang wrapper")
@pytest.mark.parametrize(
    ("args", "flagged"),
    [
        (["--crate-name", "app", "--crate-type", "bin", "src/main.rs"], True),
        (["--crate-name", "app", "--crate-type", "lib", "src/lib.rs"], False),
        (["--crate-name", "pagination", "--test", "tests/p.rs"], False),
        (["--crate-name", "serde", "--crate-type", "bin", "build.rs"], False),
    ],
)
def test_the_rustc_wrapper_flags_only_the_debugged_binary_crate(tmp_path, args, flagged):
    from peeky.rust import mir_wrapper

    wrapper, text = mir_wrapper(tmp_path)
    assert wrapper.read_text() == text and text.startswith(f"#!{tmp_path}/python -I\n")
    # A variable that is not a shell identifier (dash would drop it) reaches rustc.
    env = {"PATH": "/usr/bin:/bin", "PEEKY_MIR_CRATE": "app", "CARGO_BIN_EXE_my-app": "/b"}
    out = subprocess.run(
        [str(wrapper), sys.executable, "-c", WRAPPER_RUSTC, *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout.split()
    assert out[0] == ("app" if flagged else "-") and out[1] == "/b"
    assert ("-Zmir-opt-level=0" in out) is flagged
    assert ("-Csymbol-mangling-version=legacy" in out) is flagged
    assert out[2 : 2 + len(args)] == args


STD = "/rustc/0000/library/alloc/src/macros.rs"


def vec_then_loop():
    """`let v = vec![..];` then `for x in values {`: `vec!` is std code (another file);
    from_elem cannot unwind here, so codegen emits bb1 (line 3) inside bb0's IR block."""
    return parse_body(
        "fn main() -> () {\n"
        "    let mut _0: ();  // return place in scope 0 at src/main.rs:1:10: 1:10\n"
        "    scope 1 {\n"
        "        debug v => _1;  // in scope 1 at src/main.rs:2:9: 2:10\n"
        "    }\n"
        "    bb0: {\n"
        f"        _1 = from_elem(const 0_usize, const 3_usize) -> [return: bb1, unwind continue];  // scope 0 at {STD}:47:36: 47:66\n"
        "    }\n"
        "    bb1: {\n"
        "        _3 = copy _2;  // scope 1 at src/main.rs:3:14: 3:20\n"
        "        _4 = into_iter(move _3) -> [return: bb2, unwind: bb3];  // scope 1 at src/main.rs:3:14: 3:20\n"
        "    }\n"
        "    bb2: {\n"
        "        drop(_1) -> [return: bb4, unwind continue];  // scope 0 at src/main.rs:5:1: 5:2\n"
        "    }\n"
        "    bb3 (cleanup): {\n"
        "        drop(_1) -> [return: bb5, unwind terminate(cleanup)];  // scope 0 at src/main.rs:5:1: 5:2\n"
        "    }\n"
        "    bb4: {\n"
        "        return;  // scope 0 at src/main.rs:5:2: 5:2\n"
        "    }\n"
        "    bb5 (cleanup): {\n"
        "        resume;  // scope 0 at src/main.rs:1:1: 5:2\n"
        "    }\n"
        "}\n"
    )


LOOP = "fn main() {\n    let v = vec![0usize; 3];\n    for x in values {\n    }\n}\n"


@pytest.mark.parametrize(
    "texts, proven",
    [
        ({"src/main.rs": LOOP}, True),
        # Unknown source: std code may be on any line, so bb0's call counts as line 3's.
        (None, False),
        # Line 3 invokes a macro: std code may be collapsed onto it.
        ({"src/main.rs": LOOP.replace("in values", "in mk!(values)")}, False),
    ],
)
def test_std_macro_code_counts_only_on_a_line_that_can_invoke_a_macro(texts, proven):
    # The IR's start block holds bb0 and bb1: lines 2 (the collapsed vec!) and 3.
    function = IrFunction(
        "main",
        "app",
        "src/main.rs",
        {"start": {2, 3}, "bb2": {5}, "bb3": {5}, "bb4": {5}, "bb5": {5}},
        {},
        1,
    )
    owned = OwnedLocals([vec_then_loop()], {"h0": function}, SOURCES, texts=texts)
    assert owned.body("h0") is not None
    assert owned.proven("h0", "v", 3, MAIN) is proven
    # On the vec! line itself the local is never initialized before the call returns.
    assert not owned.proven("h0", "v", 2, MAIN)


def test_a_merged_block_with_no_code_of_the_line_defers_to_the_one_with_code():
    body = vec_then_loop()
    # Without knowing bb0 and bb1 share an IR block, bb0's line-3 code is unexplained.
    assert not body.initialized_at(1, {0, 1}, 3, "src/main.rs", 1, foreign=False)
    shared = {0: {0, 1}, 1: {0, 1}}
    assert body.initialized_at(1, {0, 1}, 3, "src/main.rs", 1, shared, foreign=False)
    # A merged block that does not explain the line is never skipped for a block that
    # also explains nothing: then the whole IR block must keep the local.
    assert not body.initialized_at(1, {0, 1}, 4, "src/main.rs", 1, shared, foreign=False)


@pytest.mark.parametrize(
    "line, possible",
    [
        ("    let v = vec![0; n];", True),
        ("    for &x in values {", False),
        ("    let a = x != y;", True),  # any `!` counts
        ("    let z = 1; /* c */", True),
        ("    counts[i] += 1; // note", False),  # the next line starts no path
        ("    let v = std::", True),  # its path ends on the next line with `!`
        ("    let w = vec", True),
    ],
)
def test_lines_that_can_start_a_macro_invocation(line, possible):
    text = f"fn f() {{\n{line}\n        vec![1];\n    }}\n"
    if not line.endswith(("std::", "vec")):
        text = f"fn f() {{\n{line}\n    }}\n"
    assert may_invoke_macro(text, 2) is possible
    assert may_invoke_macro(None, 2) and may_invoke_macro(text, 99)


@pytest.mark.parametrize(
    "text",
    [
        # Review of #80: each made the line read as macro-free while rustc put `vec!`
        # code on it.
        "fn f() {\n    let u = std::\n        /* see /* nested */ note */ vec![1];\n}\n",
        "fn f() {\n    let u = crate::\n        m\u00f3dulo::vec![1];\n}\n",
        "fn f() {\n    let u = crate::m\u00f3dulo\n        ::vec![1];\n}\n",
        "fn f() {\n    let u = std::\n        /* c */ vec![1];\n}\n",
    ],
)
def test_a_path_continued_past_a_block_comment_or_non_ascii_text_may_invoke(text):
    assert may_invoke_macro(text, 2)


def test_a_lone_carriage_return_is_not_a_line_break():
    # rustc counts `\n` only: line 4 has the vec!, line 3 does not.
    text = "fn main() {\n    // a\r(note)\n    let x = [1];\n    let v = vec![0]; for y in x {\n}\n"
    assert may_invoke_macro(text, 4) and not may_invoke_macro(text, 3)


def test_a_block_codegen_left_without_predecessors_keeps_its_own_label(tmp_path):
    # `debug_assert!` branches on a constant; codegen leaves the dead arm as
    # `bbN: ; No predecessors!`. Dropping that label gave its code to the block before
    # it, and left the MIR block uncovered, so nothing in the function was proven.
    path = tmp_path / "app.ll"
    path.write_text(
        IR.replace(
            "bb2:                                              ; preds = %bb1\n  ret void, !dbg !24\n",
            "bb2:                                              ; preds = %bb1\n  ret void, !dbg !24\n"
            "\nbb3:                                              ; No predecessors!\n"
            "  br label %bb2, !dbg !24\n",
        )
    )
    function = ir_functions(path, ["src/main.rs"])["h0123456789abcdef"]
    assert function.blocks["bb2"] == {5} and function.blocks["bb3"] == {5}
    assert 3 in function.labeled() and "bb3" not in function.preds


def test_arguments_take_their_types_from_the_header():
    # Review of #84: MIR declares arguments in `fn f(_1: ...)` only, never as `let`.
    body = parse_body(
        "fn sum(_1: &[u8], _2: std::collections::HashMap<K, V>, _3: fn(u8) -> u8, _4: usize)"
        " -> usize {\n"
        "    let mut _0: usize;  // return place in scope 0 at src/main.rs:1:10: 1:15\n"
        "    bb0: {\n"
        "        return;  // scope 0 at src/main.rs:2:2: 2:2\n"
        "    }\n"
        "}\n"
    )
    assert body.types == {
        0: "usize",
        1: "&[u8]",
        2: "std::collections::HashMap<K, V>",
        3: "fn(u8) -> u8",
        4: "usize",
    }


def test_projected_bindings_and_quoted_labels():
    # Review of #103: a closure's or async body's binding through a projection names
    # its type; LLVM quotes labels with `$` (an inlined `Box::new`'s exit block).
    from peeky.rust_mir import _LABEL, _PROJECTED

    assert _PROJECTED.fullmatch("(_1.0: std::boxed::Box<Pt>)")[1] == "std::boxed::Box<Pt>"
    assert _PROJECTED.fullmatch("(((*_36) as variant#3).0: (i32, char))")[1] == "(i32, char)"
    assert _PROJECTED.fullmatch("_3") is None
    label = _LABEL.match('"_ZN5alloc5boxed12Box$LT$T$GT$3new17h01E.exit47":   ; preds = %start')
    assert label[1] == '"_ZN5alloc5boxed12Box$LT$T$GT$3new17h01E.exit47"' and label[2] == "%start"
