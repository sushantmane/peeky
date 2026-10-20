"""The owned-local proof (rust_mir): MIR parsing, initialization dataflow and IR blocks."""

import subprocess
import sys

import pytest

from peeky.rust_mir import (
    IrFunction,
    LineFacts,
    OwnedLocals,
    _event,
    _mir_name,
    _same_path,
    _segments,
    frame_shape,
    ir_functions,
    may_invoke_macro,
    parse_body,
    stop_block,
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
    # #110: each IR block's first own call and conditional branch, and where LLVM
    # inlined a call (the inlined code's `inlinedAt`): debug intrinsics are no calls.
    assert function.events == {
        "start": (((3, 13),), ()),
        "bb1": ((), ((4, 5),)),
        "panic": (((4, 5),), ()),
        "bb2": ((), ()),
    }
    assert function.inlined == {4: {5}}
    # An `invoke` (here through a pointer) has its location on its `to label` line.
    path.write_text(
        IR.replace(
            "  ret void, !dbg !24\n",
            "  %r = invoke ptr %f(ptr %x)\n          to label %bb2 unwind label %cleanup, !dbg !25\n",
        ).replace("!24 = ", "!25 = !DILocation(line: 5, column: 9, scope: !10)\n!24 = ")
    )
    assert ir_functions(path, ["src/main.rs"])["h0123456789abcdef"].events["bb2"][0] == ((5, 9),)


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
    from peeky.rust_mir import _LABEL, _projected_type

    assert _projected_type("(_1.0: std::boxed::Box<Pt>)") == "std::boxed::Box<Pt>"
    assert _projected_type("(((*_36) as variant#3).0: (i32, char))") == "(i32, char)"
    assert _projected_type("_3") is None
    # Verification of #103: a closure's type holds `: ` itself.
    closure = "(_1.0: std::boxed::Box<{closure@src/main.rs:27:22: 27:35}>)"
    assert _projected_type(closure) == "std::boxed::Box<{closure@src/main.rs:27:22: 27:35}>"
    # A capture by reference, dereferenced, binds the referent's type.
    assert _projected_type("(*((*_1).0: &std::vec::Vec<&Pt>))") == "std::vec::Vec<&Pt>"
    assert _projected_type("(*((*_1).1: &mut Pt))") == "Pt"
    label = _LABEL.match('"_ZN5alloc5boxed12Box$LT$T$GT$3new17h01E.exit47":   ; preds = %start')
    assert label[1] == '"_ZN5alloc5boxed12Box$LT$T$GT$3new17h01E.exit47"' and label[2] == "%start"


def test_a_trailing_comment_ends_no_path():
    # Verification of #103: `// doubled` read as a path that `println!` on the next line
    # might complete, which turned off the scope narrowing for the line.
    text = 'fn f() {\n    let n = args.count * 2; // doubled\n    println!("{}", n);\n}\n'
    assert not may_invoke_macro(text, 2)
    quoted = 'fn f() {\n    let s = "a // b"; // c\n    println!("{}", s);\n}\n'
    assert may_invoke_macro(quoted, 2)


MACRO_SIBLING = "\n".join(
    [
        "fn f(_1: &Pt) -> () {",
        "    debug q => _1;                       // in scope 0 at src/main.rs:1:6: 1:7",
        "    let mut _0: ();                      // return place in scope 0 at src/main.rs:1:20: 1:22",
        "    let _2: std::boxed::Box<Pt>;         // in scope 0 at src/macros.rs:4:17: 4:18",
        "    let _3: &Pt;                         // in scope 0 at src/macros.rs:9:50: 9:51",
        "    scope 1 {",
        "        debug b => _2;                   // in scope 1 at src/macros.rs:4:17: 4:18",
        "    }",
        "    scope 2 {",
        "        debug b => _3;                   // in scope 2 at src/macros.rs:9:50: 9:51",
        "    }",
        "",
        "    bb0: {",
        "        _3 = copy _1;                    // scope 0 at src/macros.rs:9:54: 9:56",
        "        _0 = const ();                   // scope 2 at src/macros.rs:9:60: 9:70",
        "        return;                          // scope 0 at src/main.rs:12:2: 12:2",
        "    }",
        "}",
        "",
    ]
)


def test_scope_narrowing_reads_the_stop_s_own_file():
    # Verification of #103: the macro check read the function's file at the stop's line,
    # though a stop in a `macro_rules!` from another module (or `include!`d code) is at
    # another file's line, where `eprintln!` may run in the dropped Box's scope.
    body = parse_body(MACRO_SIBLING)
    main = "\n" * 8 + "    let k = 1;\n" + "\n" * 4
    shown = "/w/src/macros.rs"
    for line_nine, narrowed in (
        ('        eprintln!("dropped") } else { let b = q; }', False),
        ("        let x = 1; } else { let b = q; }", True),
    ):
        macros = "\n" * 8 + line_nine + "\n" * 4
        owned = OwnedLocals(
            [body], {}, SOURCES, texts={"src/main.rs": main, "src/macros.rs": macros}
        )
        visible = owned._visible(body, 9, shown)
        assert (visible is not None) is narrowed, (line_nine, visible)
        kept = body.in_scopes(body.bindings["b"], visible)
        assert len(kept) == (1 if narrowed else 2), kept


@pytest.mark.parametrize(
    "opening",
    ['        let s = "x', '        let s = r#"x', "        /* x"],
)
def test_a_comment_mark_inside_a_multi_line_string_is_not_a_comment(opening):
    # Verification of #103: a line that starts inside a string or block comment from the
    # line before has a `//` that comments nothing out; its path end still counts.
    closing = '"#' if "r#" in opening else ('"' if '"' in opening else " */")
    text = f'fn f() {{\n{opening}\n// y{closing}; let b = 1; eprintln\n        !("{{}}", b);\n}}\n'
    assert may_invoke_macro(text, 3)


def test_a_statement_s_scope_counts_on_every_line_of_its_span():
    # Verification of #103: a method call on its own line (`K\n.get()`) runs there, while
    # its span starts at the receiver on the line before. A block's `()` value emits no
    # code: its span (the whole block) never puts its scope on the closing `}` line.
    body = parse_body(
        "\n".join(
            [
                "fn f() -> () {",
                "    let mut _0: ();                      // return place in scope 0 at src/main.rs:1:10: 1:12",
                "    let _1: i64;                         // in scope 0 at src/main.rs:15:5: 15:6",
                "    let _2: ();                          // in scope 0 at src/main.rs:20:5: 22:6",
                "    scope 2 {",
                "    }",
                "    scope 3 {",
                "    }",
                "",
                "    bb0: {",
                "        _2 = const ();                   // scope 3 at src/main.rs:20:28: 22:6",
                "        _1 = W::get(const K) -> [return: bb1, unwind continue]; // scope 2 at src/main.rs:15:78: 16:15",
                "    }",
                "",
                "    bb1: {",
                "        return;                          // scope 0 at src/main.rs:30:2: 30:2",
                "    }",
                "}",
                "",
            ]
        )
    )
    assert 2 in body.line_scopes[("src/main.rs", 16)]
    assert 2 in body.line_scopes[("src/main.rs", 15)]
    assert 3 in body.line_scopes[("src/main.rs", 20)]
    assert ("src/main.rs", 22) not in body.line_scopes


@pytest.mark.parametrize(
    "frame, shape",
    [
        ("vb::outer::{{closure}}::{{closure}}", ("outer", 2)),
        ("outer::_$u7b$$u7b$closure$u7d$$u7d$::h0123456789abcdef", ("outer", 1)),
        ("exported", ("exported", 0)),
    ],
)
def test_a_frame_s_shape_is_its_function_and_closure_depth(frame, shape):
    # Verification of #103: a fallback frame (async, unmangled) takes the bodies of its
    # own shape, not an enclosing body whose spans cover a nested block's lines.
    assert frame_shape(frame) == shape


OWNERS = "\n".join(
    [
        "fn f(_1: &[Pt], _2: Vec<Pt>) -> () {",
        "    debug pts => _1;                     // in scope 0 at src/main.rs:1:6: 1:9",
        "    debug v => _2;                       // in scope 0 at src/main.rs:1:20: 1:21",
        "    let mut _0: ();                      // return place in scope 0 at src/main.rs:1:30: 1:32",
        "    let _3: &Pt;                         // in scope 0 at src/main.rs:2:9: 2:10",
        "    let _4: usize;                       // in scope 0 at src/main.rs:2:13: 2:20",
        "    let _5: &Pt;                         // in scope 0 at src/main.rs:3:9: 3:14",
        "    let _6: &Vec<Pt>;                    // in scope 0 at src/main.rs:3:17: 3:18",
        "    let _7: std::boxed::Box<Pt>;         // in scope 0 at src/main.rs:4:9: 4:10",
        "    let _8: &Pt;                         // in scope 0 at src/main.rs:5:9: 5:15",
        "    let _9: {closure@src/main.rs:5:30: 5:33}; // in scope 0 at src/main.rs:5:30: 5:33",
        "    let _10: &Pt;                        // in scope 0 at src/main.rs:6:9: 6:10",
        "    scope 1 {",
        "        debug p => _3;                   // in scope 1 at src/main.rs:2:9: 2:10",
        "        debug first => _5;               // in scope 1 at src/main.rs:3:9: 3:14",
        "        debug b => _7;                   // in scope 1 at src/main.rs:4:9: 4:10",
        "        debug leaked => _8;              // in scope 1 at src/main.rs:5:9: 5:15",
        "        debug r => _10;                  // in scope 1 at src/main.rs:6:9: 6:10",
        "    }",
        "",
        "    bb0: {",
        "        _4 = const 0_usize;              // scope 0 at src/main.rs:2:17: 2:18",
        "        _3 = &(*_1)[_4];                 // scope 0 at src/main.rs:2:13: 2:20",
        "        _6 = &_2;                        // scope 1 at src/main.rs:3:17: 3:18",
        "        _5 = <Vec<Pt> as Index<usize>>::index(move _6, const 0_usize) -> [return: bb1, unwind continue]; // scope 1 at src/main.rs:3:17: 3:21",
        "    }",
        "",
        "    bb1: {",
        "        _9 = {closure@src/main.rs:5:30: 5:33}; // scope 1 at src/main.rs:5:30: 5:33",
        "        _8 = Box::<Pt>::leak(move _7, move _9) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:5:18: 5:40",
        "    }",
        "",
        "    bb2: {",
        "        _10 = &(*_7);                    // scope 1 at src/main.rs:6:13: 6:16",
        "        return;                          // scope 0 at src/main.rs:7:2: 7:2",
        "    }",
        "}",
        "",
    ]
)


def test_a_reference_reads_while_used_ahead_or_just_used_on_the_line():
    # #110: a reference reads only while the code still uses it ahead (its borrow then
    # keeps the target), or after a use on the stop line with nothing since that can
    # move, drop or write. A drop is not a use: drop glue keeps no loan alive.
    body = parse_body(OWNERS)
    p, first, r = body.local("p"), body.local("first"), body.local("r")
    assert body.used_ahead(p, (0, 0)) is False
    assert body.used_ahead(first, (0, 0)) is False
    # Its own assignment comes first: the slot's old value is not a use.
    assert body.used_ahead(r, (2, 0)) is False
    body.blocks[2].statements.insert(1, ("_3 = copy _10;", "src/main.rs", 6))
    assert body.used_ahead(r, (2, 1)) is True
    body.blocks[2].statements[1] = (
        "drop(_10) -> [return: bb3, unwind continue];",
        "src/main.rs",
        6,
    )
    assert body.used_ahead(r, (2, 1)) is False
    # `_3 = &(*_1)[_4]` uses `_4`; `_6 = &_2` assigns a local nothing borrows.
    assert body.used_before(4, (0, 2)) is True
    assert body.used_before(4, (0, 3)) is True
    # A call in between can free it.
    assert body.used_before(4, (1, 0)) is False
    # Writing a borrowed local can change it.
    body.blocks[0].statements.insert(3, ("_2 = move _9;", "src/main.rs", 3))
    assert body.used_before(4, (0, 4)) is False
    assert body._borrowed() == {2}


def facts(column=None, firsts=None, inlined=(), macros=()):
    # ``firsts``: {label: (call, branch)}; every MIR block has an IR block of its own.
    events = {
        label: (() if call is None else (call,), () if branch is None else (branch,))
        for label, (call, branch) in (firsts or {}).items()
    }
    labels = {"start", *(f"bb{n}" for n in range(1, 20))}
    function = IrFunction("f", None, "src/main.rs", {label: set() for label in labels}, {})
    function.events = events
    return LineFacts(column, function, frozenset(inlined), frozenset(macros), int)


def test_a_breakpoint_stops_only_where_its_line_starts_in_each_lexical_block():
    # #110: rustc lays out a block after the block first branching to it, so where a line
    # continues from its own branch (an overflow check the IR shows) in the same lexical
    # block, LLDB puts no breakpoint location: only steps and pauses stop there.
    body = parse_body(CONTINUED)
    every = body.stop_positions({0, 1}, 3, "src/main.rs", False)
    assert every == [(0, 2), (0, 3), (0, 4), (1, 0)]
    branch = facts(firsts={"start": (None, (3, 5))})
    assert body.stop_positions({0, 1}, 3, "src/main.rs", False, branch) == [(0, 2), (0, 3), (0, 4)]
    # Unconfirmed by the IR (its IR block's first branch elsewhere), the next block may
    # be first.
    elsewhere = facts(firsts={"start": (None, (4, 5))})
    assert (1, 0) in body.stop_positions({0, 1}, 3, "src/main.rs", False, elsewhere)
    assert (1, 0) in body.stop_positions({0, 1}, 3, "src/main.rs", False, facts())
    # The stop's column picks the statements whose code it can be.
    at_load = facts(column=12, firsts={"start": (None, (3, 5))})
    assert body.stop_positions({0, 1}, 3, "src/main.rs", False, at_load) == [(0, 2)]
    # A macro's column on the line can be any statement's.
    in_macro = facts(column=5, firsts={"start": (None, (3, 5))}, macros={5})
    assert body.stop_positions({0, 1}, 3, "src/main.rs", False, in_macro) == [
        (0, 2),
        (0, 3),
        (0, 4),
    ]
    # A column no statement can have tells nothing.
    nowhere = facts(column=40, firsts={"start": (None, (3, 5))})
    assert body.stop_positions({0, 1}, 3, "src/main.rs", False, nowhere) == [(0, 2), (0, 3), (0, 4)]
    item = body.local("item")
    assert not body.used_ahead(item, (1, 0))
    assert all(body.used_ahead(item, at) or body.used_before(item, at) for at in every)


def test_an_inlined_call_s_result_is_stored_after_its_body():
    # #110: LLVM inlines `#[inline(always)]` callees even at opt-level 0; the caller's
    # code after the inlined body is where its line's breakpoint may be.
    body = parse_body(OWNERS)
    inlined = facts(column=18, inlined={20})
    assert body.stop_positions({1}, 5, "src/main.rs", False, inlined) == [(1, 1), (2, 0)]
    real = facts(column=18, firsts={"bb1": ((5, 18), None)})
    assert body.stop_positions({1}, 5, "src/main.rs", False, real) == [(1, 1)]
    # Steps and pauses can stop after any call of the line.
    assert body.stop_positions({1}, 5, "src/main.rs", False) == [(1, 0), (1, 1), (2, 0)]
    # A call's result is stored at its target's start (an `invoke`'s).
    assert body.stop_positions({2}, 5, "src/main.rs", False, real) == [(2, 0)]
    # Code of the line no statement explains can be anywhere in its block.
    assert body.stop_positions({0}, 9, "src/main.rs", False, real) == [
        (0, 0),
        (0, 1),
        (0, 2),
        (0, 3),
    ]


def test_a_statement_s_lexical_block_is_its_nearest_scope_declaring_a_variable():
    body = parse_body(OWNERS)
    body.scopes.update({2: 1, 3: 2})
    assert body.lexical(3) == 1 and body.lexical(1) == 1 and body.lexical(0) == 0
    body.inlined_scopes.add(2)
    assert body.lexical(3) == 2


def test_string_constants_name_no_local():
    assert _event('_5 = <String as From<&str>>::from(const "_3") -> bb1;', 3) is None
    assert _event("_5 = copy _3;", 3) == "use"


CONTINUED = "\n".join(
    [
        "fn f(_1: &Item, _2: i64) -> i64 {",
        "    debug item => _1;                    // in scope 0 at src/main.rs:1:6: 1:10",
        "    debug sum => _2;                     // in scope 0 at src/main.rs:1:19: 1:22",
        "    let mut _0: i64;                     // return place in scope 0 at src/main.rs:1:30: 1:33",
        "    let mut _3: i64;                     // in scope 0 at src/main.rs:3:12: 3:22",
        "    let mut _4: (i64, bool);             // in scope 0 at src/main.rs:3:5: 3:22",
        "",
        "    bb0: {",
        "        _0 = copy _2;                    // scope 1 at src/main.rs:2:5: 2:8",
        "        StorageLive(_3);                 // scope 1 at src/main.rs:3:12: 3:22",
        "        _3 = copy ((*_1).0: i64);        // scope 1 at src/main.rs:3:12: 3:22",
        "        _4 = AddWithOverflow(copy _0, copy _3); // scope 1 at src/main.rs:3:5: 3:22",
        '        assert(!move (_4.1: bool), "attempt to compute `{} + {}`, which would overflow", copy _0, move _3) -> [success: bb1, unwind continue]; // scope 1 at src/main.rs:3:5: 3:22',
        "    }",
        "",
        "    bb1: {",
        "        _0 = move (_4.0: i64);           // scope 1 at src/main.rs:3:5: 3:22",
        "        StorageDead(_3);                 // scope 1 at src/main.rs:3:21: 3:22",
        "        return;                          // scope 0 at src/main.rs:4:2: 4:2",
        "    }",
        "}",
        "",
    ]
)


UNIQUE = (
    "copy ((_{0}.0: std::ptr::Unique<{1}>).0: std::ptr::NonNull<{1}>) as *const {1} (Transmute);"
)


def test_a_box_in_a_box_is_moved_out_of_through_a_copy_in_any_block_order():
    # The pointer made from the copy (`_38`) comes in a block before the copy (`_17`).
    body = mir(
        {
            0: [f"_37 = {UNIQUE.format(1, 'Outer')}", "goto -> bb2;"],
            1: [
                f"_38 = {UNIQUE.format(17, 'Inner')}",
                "_7 = move ((*_38).0: std::boxed::Box<Pt>);",
                "_21 = " + UNIQUE.format(20, "Pt"),
                "goto -> bb3;",
            ],
            2: [
                "_17 = copy ((*_37).1: std::boxed::Box<Inner>);",
                "_20 = copy ((*_5).0: std::boxed::Box<Pt>);",
                "goto -> bb1;",
            ],
            3: ["return;"],
        }
    )
    boxes = body._boxes()
    assert boxes[37] == boxes[38] == 1
    assert body._box_paths[38] == "((*B_1).1: std::boxed::Box<Inner>)"
    assert 1 in body._kills("_7 = move ((*_38).0: std::boxed::Box<Pt>);")
    # A copy out of a reference is a Box of its own.
    assert boxes[21] == 20


@pytest.mark.parametrize(
    "text, kept",
    [
        ("/* eat collapses */ #[collapse_debuginfo(yes)]", True),
        ('let g = "src/*.rs";\n#[collapse_debuginfo(yes)]\n/* x */', True),
        ('let u = "http://x"; #[collapse_debuginfo(yes)]', True),
        ("let c = '\"'; #[collapse_debuginfo(yes)] // \"", True),
        ('let r = r#"/*"#; #[collapse_debuginfo(yes)] /* */', True),
        ('let r = br"\\"; #[collapse_debuginfo(yes)] // "', True),
        ('let s = "unterminated /* collapse_debuginfo', True),
        ("/* outer /* inner */ collapse_debuginfo */", False),
        ("/// #[collapse_debuginfo(yes)]\nfn f() {}", False),
        ("fn f<'a>(x: &'a str) {} /* collapse_debuginfo */", False),
    ],
)
def test_comments_are_told_from_strings_where_collapse_is_looked_for(text, kept):
    from peeky.rust import without_comments

    assert ("collapse_debuginfo" in without_comments(text)) is kept


def test_aliases_follow_values_that_can_hold_a_pointer_and_find_drops_through_them():
    text = "\n".join(
        [
            "// MIR for `f` after PreCodegen",
            "",
            "fn f(_1: &RefCell<Vec<i32>>) -> () {",
            "    let mut _0: ();                      // return place in scope 0 at src/main.rs:1:10: 1:10",
            "    let _2: usize;                       // in scope 0 at src/main.rs:2:9: 2:10",
            "    let _3: std::string::String;         // in scope 0 at src/main.rs:2:9: 2:10",
            "    let mut _4: std::cell::RefMut<'_, Vec<i32>>; // in scope 0 at src/main.rs:2:9: 2:10",
            "    let mut _5: &mut std::cell::RefMut<'_, Vec<i32>>; // in scope 0 at src/main.rs:2:9: 2:10",
            "    let _6: &mut Vec<i32>;               // in scope 0 at src/main.rs:2:9: 2:10",
            "",
            "    bb0: {",
            "        _2 = Vec::<i32>::len(copy _1) -> [return: bb1, unwind continue];  // scope 0 at src/main.rs:3:5: 3:9",
            "    }",
            "",
            "    bb1: {",
            "        _3 = format(copy _2) -> [return: bb2, unwind continue];  // scope 0 at src/main.rs:4:5: 4:9",
            "    }",
            "",
            "    bb2: {",
            "        _4 = RefCell::<Vec<i32>>::borrow_mut(copy _1) -> [return: bb3, unwind continue];  // scope 0 at src/main.rs:5:5: 5:9",
            "    }",
            "",
            "    bb3: {",
            "        _5 = &mut _4;                    // scope 0 at src/main.rs:6:5: 6:9",
            "        _6 = <RefMut<'_, Vec<i32>> as DerefMut>::deref_mut(move _5) -> [return: bb4, unwind continue];  // scope 0 at src/main.rs:6:5: 6:9",
            "    }",
            "",
            "    bb4: {",
            "        drop((*_6)) -> [return: bb5, unwind continue];  // scope 0 at src/main.rs:7:5: 7:9",
            "    }",
            "",
            "    bb5: {",
            "        return;                          // scope 0 at src/main.rs:8:2: 8:2",
            "    }",
            "}",
            "",
        ]
    )
    body = parse_body(text)
    assert body.supported
    body.generics = ()  # matched to a function that has no type parameters
    assert body._aliases(1) == {1, 4, 5, 6}  # not the length or the String made from it
    assert body.drops_through(1) and not body.drops_through(2)


# #116: an `#[inline(always)]` function, as rustc dumps its MIR (built before LLVM
# inlines it into every caller, even at opt-level 0).
INLINED = """fn e(_1: &mut Vec<Pt>) -> i64 {
    debug v => _1;                       // in scope 0 at src/main.rs:13:6: 13:7
    let mut _0: i64;                     // return place in scope 0 at src/main.rs:13:26: 13:29
    let _2: &Pt;                         // in scope 0 at src/main.rs:14:9: 14:14
    let _3: &Pt;                         // in scope 0 at src/main.rs:14:19: 14:22
    let mut _4: &std::vec::Vec<Pt>;      // in scope 0 at src/main.rs:14:18: 14:19
    let _6: ();                          // in scope 0 at src/main.rs:16:5: 16:30
    let mut _7: &mut std::vec::Vec<Pt>;  // in scope 0 at src/main.rs:16:5: 16:6
    let mut _8: Pt;                      // in scope 0 at src/main.rs:16:12: 16:29
    let mut _9: i64;                     // in scope 0 at src/main.rs:17:5: 17:6
    let mut _10: i64;                    // in scope 0 at src/main.rs:17:9: 17:16
    let mut _11: (i64, bool);            // in scope 0 at src/main.rs:17:5: 17:16
    scope 1 {
        debug first => _2;               // in scope 1 at src/main.rs:14:9: 14:14
        let _5: i64;                     // in scope 1 at src/main.rs:15:9: 15:10
        scope 2 {
            debug k => _5;               // in scope 2 at src/main.rs:15:9: 15:10
        }
    }
    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:14:9: 14:14
        StorageLive(_3);                 // scope 0 at src/main.rs:14:19: 14:22
        StorageLive(_4);                 // scope 0 at src/main.rs:14:18: 14:19
        _4 = &(*_1);                     // scope 0 at src/main.rs:14:18: 14:19
        _3 = <Vec<Pt> as Index<usize>>::index(move _4, const 0_usize) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:14:19: 14:22
    }
    bb1: {
        StorageDead(_4);                 // scope 0 at src/main.rs:14:21: 14:22
        _2 = &(*_3);                     // scope 0 at src/main.rs:14:17: 14:22
        StorageLive(_5);                 // scope 1 at src/main.rs:15:9: 15:10
        _5 = copy ((*_2).0: i64);        // scope 1 at src/main.rs:15:13: 15:20
        StorageLive(_6);                 // scope 2 at src/main.rs:16:5: 16:30
        StorageLive(_7);                 // scope 2 at src/main.rs:16:5: 16:6
        _7 = &mut (*_1);                 // scope 2 at src/main.rs:16:5: 16:6
        StorageLive(_8);                 // scope 2 at src/main.rs:16:12: 16:29
        _8 = Pt { x: const 3_i64, y: const 4_i64 }; // scope 2 at src/main.rs:16:12: 16:29
        _6 = Vec::<Pt>::push(move _7, move _8) -> [return: bb2, unwind continue]; // scope 2 at src/main.rs:16:5: 16:30
    }
    bb2: {
        StorageDead(_8);                 // scope 2 at src/main.rs:16:29: 16:30
        StorageDead(_7);                 // scope 2 at src/main.rs:16:29: 16:30
        StorageDead(_6);                 // scope 2 at src/main.rs:16:30: 16:31
        StorageLive(_9);                 // scope 2 at src/main.rs:17:5: 17:6
        _9 = copy _5;                    // scope 2 at src/main.rs:17:5: 17:6
        StorageLive(_10);                // scope 2 at src/main.rs:17:9: 17:16
        _10 = churn() -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:17:9: 17:16
    }
    bb3: {
        _11 = AddWithOverflow(copy _9, copy _10); // scope 2 at src/main.rs:17:5: 17:16
        assert(!move (_11.1: bool), "attempt to compute `{} + {}`, which would overflow", move _9, move _10) -> [success: bb4, unwind continue]; // scope 2 at src/main.rs:17:5: 17:16
    }
    bb4: {
        _0 = move (_11.0: i64);          // scope 2 at src/main.rs:17:5: 17:16
        StorageDead(_10);                // scope 2 at src/main.rs:17:15: 17:16
        StorageDead(_9);                 // scope 2 at src/main.rs:17:15: 17:16
        StorageDead(_5);                 // scope 1 at src/main.rs:18:1: 18:2
        StorageDead(_3);                 // scope 0 at src/main.rs:18:1: 18:2
        StorageDead(_2);                 // scope 0 at src/main.rs:18:1: 18:2
        return;                          // scope 0 at src/main.rs:18:2: 18:2
    }
}
"""

INLINED_IR = """define internal void @_ZN5chars4main17h0123456789abcdefE() unnamed_addr #0 !dbg !10 {
start:
  %_3.i = call ptr @index(), !dbg !31
  call void @push(), !dbg !32
  %_10.i = call i64 @churn(), !dbg !33
  ret void, !dbg !24
}

!2 = !DIFile(filename: "src/main.rs", directory: "/w")
!9 = !DINamespace(name: "chars", scope: null)
!10 = distinct !DISubprogram(name: "main", linkageName: "_ZN5chars4main17h0123456789abcdefE", scope: !9, file: !2, line: 27, unit: !1)
!12 = distinct !DISubprogram(name: "e", linkageName: "_ZN5chars1e17h00000000000000eeE", scope: !9, file: !2, line: 13, unit: !1)
!13 = distinct !DILexicalBlock(scope: !12, file: !2, line: 14, column: 5)
!22 = !DILocation(line: 29, column: 20, scope: !10)
!24 = !DILocation(line: 31, column: 2, scope: !10)
!31 = !DILocation(line: 14, column: 19, scope: !12, inlinedAt: !22)
!32 = !DILocation(line: 16, column: 7, scope: !13, inlinedAt: !22)
!33 = !DILocation(line: 17, column: 9, scope: !13, inlinedAt: !22)
"""


def test_a_function_inlined_everywhere_is_recorded_from_its_inlined_code(tmp_path):
    # #116: `#[inline(always)]` leaves no IR function of its own, yet its frames carry
    # its hash; the lines of its inlined copies identify its MIR body.
    path = tmp_path / "app.ll"
    path.write_text(INLINED_IR)
    functions = ir_functions(path, ["src/main.rs"])
    inlined = functions["h00000000000000ee"]
    assert inlined.inline_only and not inlined.blocks
    assert (inlined.name, inlined.parent, inlined.line) == ("e", "chars", 13)
    assert inlined.copies == {14, 16, 17} and inlined.ancestors == ("chars",)
    assert not functions["h0123456789abcdef"].inline_only
    assert not functions["h0123456789abcdef"].copies


def test_an_inlined_frame_s_references_are_judged_at_every_place_of_its_line(tmp_path):
    path = tmp_path / "app.ll"
    path.write_text(INLINED_IR)
    functions = ir_functions(path, ["src/main.rs"])
    body = parse_body(INLINED)
    owned = OwnedLocals([body], functions, SOURCES)
    assert owned.body("h00000000000000ee") is body
    for breakpoint in (False, True):
        # `first` past its last use and `v.push(...)`: the Vec may have reallocated.
        assert owned.reference_valid("h00000000000000ee", "first", 17, MAIN, breakpoint, 5) is False
        assert owned.reference_valid("h00000000000000ee", "first", 15, MAIN, breakpoint, 13)
        # The caller lent `v` for the whole call.
        assert owned.reference_valid("h00000000000000ee", "v", 17, MAIN, breakpoint, 5)
    # Every statement of the line, and after its call: the line's own places only (not
    # before the `return` of the closing brace, past the `StorageDead`s).
    site = owned._site("h00000000000000ee", 17, MAIN)
    assert owned._positions(site, 17, None) == [(2, 4), (2, 6), (3, 0), (3, 1), (4, 0)]
    assert owned._positions(owned._site("h00000000000000ee", 13, MAIN), 13, None) == [(0, 0)]
    # Not yet assigned on its own line.
    assert owned.reference_valid("h00000000000000ee", "first", 14, MAIN) is False
    # A line with no code of it: nothing places the stop, so its references are gated.
    assert owned._site("h00000000000000ee", 19, MAIN) is None
    assert owned.reference_valid("h00000000000000ee", "first", 19, MAIN) is False
    assert owned.reference_valid("h00000000000000ee", "v", 19, MAIN) is None
    assert owned.proven("h00000000000000ee", "k", 17, MAIN)


def test_an_inlined_function_not_told_apart_gates_its_borrowing_bindings(tmp_path):
    path = tmp_path / "app.ll"
    path.write_text(INLINED_IR)
    functions = ir_functions(path, ["src/main.rs"])
    # Two bodies it may be (a macro's twins), or none with a span on its `fn` line.
    for bodies in (
        [parse_body(INLINED), parse_body(INLINED)],
        [parse_body(INLINED.replace("13:", "12:"))],
    ):
        owned = OwnedLocals(bodies, functions, SOURCES)
        assert owned.body("h00000000000000ee") is None
        assert owned.reference_valid("h00000000000000ee", "first", 15, MAIN) is False
        # A reference parameter never reassigned is the caller's.
        assert owned.reference_valid("h00000000000000ee", "v", 15, MAIN) is None


def test_a_function_also_inlined_takes_no_breakpoint_facts(tmp_path):
    # A frame with its hash may be an inlined copy, whose layout its IR does not show.
    path = tmp_path / "app.ll"
    path.write_text(IR.replace("!23 = !DILocation(line: 99", "!23 = !DILocation(line: 4"))
    function = ir_functions(path, ["src/main.rs"])["h0123456789abcdef"]
    assert function.copies == {4} and not function.inline_only


# #116: an `async fn`'s body after rustc's coroutine transform: `first` is kept in its
# state across the `.await` (variant 3), `stale` is not, and `v` is copied from the
# state into the poll's own frame.
COROUTINE = "\n".join(
    [
        "fn live::{closure#0}(_1: Pin<&mut {async fn body of live()}>, _2: &mut Context<'_>) -> Poll<i64> {",
        "    debug _task_context => _2;           // in scope 0 at src/main.rs:1:35: 6:2",
        "    debug v => ((*_9).0: &std::vec::Vec<Pt>); // in scope 0 at src/main.rs:1:15: 1:16",
        "    let mut _0: std::task::Poll<i64>;    // return place in scope 0 at src/main.rs:1:35: 6:2",
        "    let _3: &std::vec::Vec<Pt>;          // in scope 0 at src/main.rs:1:15: 1:16",
        "    let _4: &Pt;                         // in scope 0 at src/main.rs:2:9: 2:14",
        "    let _5: &Pt;                         // in scope 0 at src/main.rs:2:19: 2:22",
        "    let mut _6: bool;                    // in scope 0 at src/main.rs:4:5: 4:23",
        "    let mut _7: i64;                     // in scope 0 at src/main.rs:5:5: 5:12",
        "    let mut _8: u32;                     // in scope 0 at src/main.rs:1:35: 6:2",
        "    let mut _9: &mut {async fn body of live()}; // in scope 0 at src/main.rs:1:35: 6:2",
        "    let _10: &Pt;                        // in scope 0 at src/main.rs:3:9: 3:14",
        "    scope 1 {",
        "        debug v => _3;                   // in scope 1 at src/main.rs:1:15: 1:16",
        "        scope 2 {",
        "            debug first => (((*_9) as variant#3).0: &Pt); // in scope 2 at src/main.rs:2:9: 2:14",
        "            debug stale => _10;          // in scope 2 at src/main.rs:3:9: 3:14",
        "        }",
        "    }",
        "",
        "    bb0: {",
        "        _9 = copy (_1.0: &mut {async fn body of live()}); // scope 0 at src/main.rs:1:35: 6:2",
        "        _8 = discriminant((*_9));        // scope 0 at src/main.rs:1:35: 6:2",
        "        switchInt(move _8) -> [0: bb1, 1: bb6, 2: bb6, 3: bb5, otherwise: bb6]; // scope 0 at src/main.rs:1:35: 6:2",
        "    }",
        "",
        "    bb1: {",
        "        StorageLive(_3);                 // scope 0 at src/main.rs:1:15: 1:16",
        "        _3 = copy ((*_9).0: &std::vec::Vec<Pt>); // scope 0 at src/main.rs:1:15: 1:16",
        "        _5 = <Vec<Pt> as Index<usize>>::index(copy _3, const 0_usize) -> [return: bb2, unwind continue]; // scope 1 at src/main.rs:2:19: 2:22",
        "    }",
        "",
        "    bb2: {",
        "        (((*_9) as variant#3).0: &Pt) = &(*_5); // scope 1 at src/main.rs:2:17: 2:22",
        "        StorageLive(_10);                // scope 2 at src/main.rs:3:9: 3:14",
        "        _10 = &(*_5);                    // scope 2 at src/main.rs:3:17: 3:22",
        "        goto -> bb8;                     // scope 2 at src/main.rs:4:5: 4:23",
        "    }",
        "",
        "    bb8: {",
        "        _6 = poll() -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:4:5: 4:23",
        "    }",
        "",
        "    bb3: {",
        "        switchInt(move _6) -> [0: bb7, otherwise: bb4]; // scope 2 at src/main.rs:4:5: 4:23",
        "    }",
        "",
        "    bb4: {",
        "        _7 = copy ((*(((*_9) as variant#3).0: &Pt)).0: i64); // scope 2 at src/main.rs:5:5: 5:12",
        "        _0 = Poll::<i64>::Ready(move _7); // scope 0 at src/main.rs:6:2: 6:2",
        "        discriminant((*_9)) = 1;         // scope 0 at src/main.rs:6:2: 6:2",
        "        return;                          // scope 0 at src/main.rs:6:2: 6:2",
        "    }",
        "",
        "    bb5: {",
        "        StorageLive(_3);                 // scope 0 at src/main.rs:1:35: 6:2",
        "        StorageLive(_10);                // scope 0 at src/main.rs:1:35: 6:2",
        "        goto -> bb8;                     // scope 2 at src/main.rs:4:5: 4:23",
        "    }",
        "",
        "    bb6: {",
        "        unreachable;                     // scope 0 at src/main.rs:1:35: 6:2",
        "    }",
        "",
        "    bb7: {",
        "        _0 = Poll::<i64>::Pending;       // scope 2 at src/main.rs:4:18: 4:23",
        "        StorageDead(_3);                 // scope 2 at src/main.rs:4:18: 4:23",
        "        StorageDead(_10);                // scope 2 at src/main.rs:4:18: 4:23",
        "        discriminant((*_9)) = 3;         // scope 2 at src/main.rs:4:18: 4:23",
        "        return;                          // scope 2 at src/main.rs:4:18: 4:23",
        "    }",
        "}",
        "",
    ]
)


def coroutine_function():
    body = parse_body(COROUTINE)
    blocks = {"start": {1}}
    for number, block in body.blocks.items():
        if number:
            blocks[f"bb{number}"] = {line for _, _, line in block.statements}
    return IrFunction(
        "{async_fn#0}", "live", "src/main.rs", blocks, {}, 1, ancestors=("app", "live")
    )


def test_what_a_coroutine_keeps_across_a_suspension_is_a_local_of_its_own():
    body = parse_body(COROUTINE)
    assert body.supported and body.coroutine == 9
    # The state's variant field is renamed a local past the body's own.
    assert body.saved == {11} and body.types[11] == "&Pt"
    assert body.local("first") == 11 and body.resumes == {7: 5}
    assert body.blocks[4].statements[0][0] == "_7 = copy ((*_11).0: i64);"
    # It stays initialized across the suspension; the poll's own locals do not.
    entry = body._dataflow()
    assert entry[4] & (1 << 11) and not entry[4] & (1 << 10) and not entry[4] & (1 << 3)
    assert entry[4] & (1 << 2)
    # Used after the `.await`, it is used ahead of the suspension too.
    assert body.used_ahead(11, (7, 0)) and body.used_ahead(11, (8, 0))
    assert not body.used_before(11, (5, 0))
    # What the caller lent: the parameter's copy, and the poll's `&mut Context`.
    assert body._lent() == {2, 3}


def test_an_async_body_is_matched_and_judges_what_it_keeps_and_what_it_does_not():
    owned = OwnedLocals([parse_body(COROUTINE)], {"h": coroutine_function()}, SOURCES)
    assert owned.body("h") is not None
    assert owned.reference_valid("h", "first", 5, MAIN) is True
    assert owned.reference_valid("h", "first", 3, MAIN) is True
    # After a later poll resumes it, the poll's own copies hold whatever the stack did.
    assert owned.reference_valid("h", "stale", 5, MAIN) is False
    assert owned.reference_valid("h", "v @ main.rs:1", 5, MAIN) is False
    # Before the suspension the parameter's copy is the caller's (the state's is not
    # judged, so the name reads as before).
    assert owned.reference_valid("h", "v @ main.rs:1", 3, MAIN) is None
    assert owned.reference_valid("h", "_task_context", 5, MAIN) is True


def test_a_coroutine_whose_state_is_not_understood_gates_its_references():
    # A suspension the dispatch never resumes: its saved values are not followed.
    text = COROUTINE.replace("3: bb5, otherwise", "otherwise")
    body = parse_body(text)
    assert not body.supported
    owned = OwnedLocals([body], {"h": coroutine_function()}, SOURCES)
    assert owned.body("h") is None
    assert owned.reference_valid("h", "first", 5, MAIN) is False
    # A coroutine whose state is not taken where it always is.
    moved = parse_body(COROUTINE.replace("_9 = copy (_1.0:", "_9 = move (_1.0:"))
    assert not moved.supported and moved.coroutine == -1
    assert OwnedLocals([moved], {"h": coroutine_function()}, SOURCES).body("h") is None
    # A place in the state other than a field of a variant.
    assert not parse_body(
        COROUTINE.replace("_10 = &(*_5);", "_10 = &(((*_9) as variant#3));")
    ).supported


@pytest.mark.parametrize(
    ("name", "ancestors", "path"),
    [
        ("{async_fn#0}", ("app", "serve"), "serve::{closure#0}"),
        ("{async_block#0}", ("app", "serve", "{async_fn#0}"), "serve::{closure#0}::{closure#0}"),
        (
            "{closure#0}",
            ("app", "serve", "{async_fn#0}", "{async_block#0}"),
            "serve::{closure#0}::{closure#0}::{closure#0}",
        ),
        ("{async_closure#1}", ("app", "main", "{closure#1}"), "main::{closure#1}::{closure#1}"),
        (
            "{async_fn#0}",
            ("app", "{impl#1}", "lookup"),
            "<impl at src/main.rs:48:1: 48:8>::lookup::{closure#0}",
        ),
    ],
)
def test_a_coroutine_s_body_is_its_closure_of_the_same_path(name, ancestors, path):
    # #116: debug info names a coroutine by its kind, the MIR as a closure; a closure in
    # an async block is told from the block by the whole path.
    body = mir({0: ["return;"]}, header=f"fn {path}() -> () {{")
    function = IrFunction(
        name, ancestors[-1], "src/main.rs", {"start": {1}}, {}, 1, ancestors=ancestors
    )
    assert _mir_name(name) == body.name and _same_path(body, function)
    nested = mir({0: ["return;"]}, header=f"fn {path}::{{closure#0}}() -> () {{")
    assert not (_mir_name(name) == nested.name and _same_path(nested, function))


def test_a_parameter_s_rows_read_where_its_copy_holds_what_the_state_does():
    # #116: lldb-dap shows an async fn's parameter twice, as the state's field (lent for
    # the future's life) and as the poll's copy; after a resume the copy is unassigned.
    from peeky.rust import _same_lent_rows

    owned = OwnedLocals([parse_body(COROUTINE)], {"h": coroutine_function()}, SOURCES)
    assert owned.reference_valid("h", "v @ main.rs:1", 5, MAIN) is False
    assert owned.lent_copies("h", "v @ main.rs:1", 5, MAIN) == (8, 2)
    assert owned.lent_copies("h", "first", 5, MAIN) is None
    assert owned.lent_copies("h", "stale", 5, MAIN) is None
    rows = [
        {"name": "v @ main.rs:1", "memoryReference": "0x10"},
        {"name": "v @ main.rs:1", "memoryReference": "0x20"},
        {"name": "first", "memoryReference": "0x30"},
    ]
    memory = {"0x10": b"\x01" * 8, "0x20": b"\x01" * 8, "0x30": b"\x02" * 8}

    def read(reference, count):
        return memory[reference][:count]

    assert _same_lent_rows((8, 2), rows[1], rows, read)
    assert not _same_lent_rows(None, rows[1], rows, read)
    # A row missing, a row not among them, other bytes, an unreadable row.
    assert not _same_lent_rows((8, 3), rows[1], rows, read)
    assert not _same_lent_rows((8, 2), dict(rows[1]), rows, read)
    memory["0x20"] = b"\x01" * 7 + b"\x03"
    assert not _same_lent_rows((8, 2), rows[1], rows, read)
    del memory["0x20"]
    assert not _same_lent_rows((8, 2), rows[1], rows, read)
    # A thin reference shows the pointer it holds: lldb-dap 18 reads no memory, and
    # gives no memoryReference.
    shown = [{"name": "v @ main.rs:1", "value": "0x0000fffffffff120"} for _ in range(2)]
    assert _same_lent_rows((8, 2), shown[0], shown, None)
    shown[1]["value"] = "0x0000fffffffff128"
    assert not _same_lent_rows((8, 2), shown[0], shown, None)
    # A wide one (`&[T]`, `&str`) shows no pointer, nor does a summary: then its bytes.
    shown[1]["value"] = "0x0000fffffffff120"
    assert not _same_lent_rows((16, 2), shown[0], shown, None)
    summary = [{"name": "s @ main.rs:1", "value": '0x0000fffffffff120 "ab"'} for _ in range(2)]
    assert not _same_lent_rows((8, 2), summary[0], summary, None)


def test_a_value_a_coroutine_s_state_holds_is_not_a_capture_through_it():
    # #116: `n: i64`, an async fn's parameter in its state, points at nothing that a drop
    # through the state's other fields (`*v = new`) frees.
    text = COROUTINE.replace(
        "    debug v => ((*_9).0: &std::vec::Vec<Pt>); // in scope 0 at src/main.rs:1:15: 1:16",
        "    debug v => ((*_9).0: &std::vec::Vec<Pt>); // in scope 0 at src/main.rs:1:15: 1:16\n"
        "    debug n => ((*_9).1: i64);           // in scope 0 at src/main.rs:1:30: 1:31",
    ).replace(
        "        _7 = copy ((*(((*_9) as variant#3).0: &Pt)).0: i64); // scope 2 at src/main.rs:5:5: 5:12",
        "        drop((*_3)) -> [return: bb9, unwind continue]; // scope 2 at src/main.rs:5:5: 5:12\n"
        "    }\n\n    bb9: {\n"
        "        _7 = copy ((*(((*_9) as variant#3).0: &Pt)).0: i64); // scope 2 at src/main.rs:5:5: 5:12",
    )
    function = coroutine_function()
    function.blocks["bb9"] = {5}
    owned = OwnedLocals([parse_body(text)], {"h": function}, SOURCES)
    assert owned.reference_valid("h", "n", 5, MAIN) is None
    assert owned.reference_valid("h", "v @ main.rs:1", 5, MAIN) is False


@pytest.mark.parametrize(
    "text, spans, complete",
    [
        ("let a = 1; // x\nlet b;", [(11, 15, True)], True),
        ("/* a /* b */ c */ x", [(0, 17, True)], True),
        ("f::<'a>('x', \"y\")", [(8, 11, False), (13, 16, False)], True),
        ('let s = cr"C:\\"; // c', [(8, 15, False), (17, 21, True)], True),
        ('let s = br#"a"b"#;', [(8, 17, False)], True),
        ("/* open", [(0, 7, True)], False),
        ('let s = "open', [(8, 13, False)], False),
    ],
)
def test_rust_comments_and_literals_are_found_by_one_lexer(text, spans, complete):
    from peeky.rust_lexer import code_only, lexemes

    assert lexemes(text) == (spans, complete)
    blanked = code_only(text)
    assert (blanked is None) is not complete
    if blanked is not None:
        assert len(blanked) == len(text) and blanked.count("\n") == text.count("\n")


BOXED = "std::boxed::Box<Pt>"


def replaced(between=(), store=f"(_1.1: {BOXED}) = move _4;", dropped=f"(_1.1: {BOXED})"):
    """`h.b = new` on a local: its drop, then (past ``between``) a store."""
    return mir(
        {
            0: ["_1 = make() -> [return: bb1, unwind continue];"],
            1: [f"drop({dropped}) -> [return: bb2, unwind: bb4];"],
            2: ["StorageDead(_5);", *between, store, "goto -> bb3;"],
            3: ["_6 = const ();", "return;"],
            4: [store, "resume;"],
        },
        bindings=V,
    )


def test_a_field_dropped_and_replaced_in_place_leaves_its_local_whole():
    # Review of #108: the drop of `h.b = new` ended `h`, and the store of the new Box
    # into the same place never made it whole again.
    body = replaced()
    drops, stores = body._replacements()
    assert set(drops.values()) == {1} and stores == {f"(_1.1: {BOXED}) = move _4;": 1}
    assert body.initialized_throughout(1, {3})
    assert not body.initialized_throughout(1, {2})  # between the drop and the store
    # A nested field, and an element (`arr[i] = new`).
    nested = f"((_1.0: Inner).2: {BOXED})"
    assert replaced(store=f"{nested} = move _4;", dropped=nested).initialized_throughout(1, {3})
    assert replaced(store="_1[_7] = move _4;", dropped="_1[_7]").initialized_throughout(1, {3})
    # Past the drop flags an element's store sets (`_20 = const false;`).
    flagged = replaced(("_20 = const false;",), "_1[_7] = move _4;", "_1[_7]")
    assert flagged.initialized_throughout(1, {3})


@pytest.mark.parametrize(
    "between, store, dropped",
    [
        # The store goes to another field, or another element.
        ((), "(_1.0: i64) = const 2_i64;", None),
        ((), "_1[_8] = move _4;", "_1[_7]"),
        # Something runs between them (an index changes; a move out of the local).
        (("_7 = const 1_usize;",), "_1[_7] = move _4;", "_1[_7]"),
        ((f"_9 = move (_1.0: {BOXED});",), None, None),
        # Not the local's own place: through a pointer it holds.
        ((), f"((*_1).1: {BOXED}) = move _4;", f"((*_1).1: {BOXED})"),
    ],
)
def test_a_drop_whose_store_is_not_to_the_same_place_is_never_paired(between, store, dropped):
    body = replaced(
        between,
        store or f"(_1.1: {BOXED}) = move _4;",
        dropped or f"(_1.1: {BOXED})",
    )
    assert body._replacements() == ({}, {})
    assert not body.initialized_throughout(1, {3})


def test_a_replaced_field_leaves_a_partly_moved_local_partly_moved():
    # `let d = h.b; h.v = new;`: the store fills `h.v`, not `h.b`.
    body = mir(
        {
            0: ["_1 = make() -> [return: bb1, unwind continue];"],
            1: [
                f"_2 = move (_1.1: {BOXED});",
                "drop((_1.0: Vec<i64>)) -> [return: bb2, unwind continue];",
            ],
            2: ["(_1.0: Vec<i64>) = move _4;", "goto -> bb3;"],
            3: ["return;"],
        },
        bindings=V,
    )
    assert set(body._replacements()[0].values()) == {1}
    assert not body.initialized_throughout(1, {3})
    # The store moving out of the local itself (`h.b = h.c`) leaves it partly moved.
    body = replaced(store=f"(_1.1: {BOXED}) = move (_1.2: {BOXED});")
    assert set(body._replacements()[1].values()) == {1}
    assert not body.initialized_throughout(1, {3})


GENERIC_IR = """define internal void @_ZN3app4show17h1111111111111111E() !dbg !10 {
start:
  ret void, !dbg !20
}

define internal void @_ZN3app4show17h2222222222222222E() !dbg !11 {
start:
  ret void, !dbg !21
}

define internal void @_ZN3app4main17h3333333333333333E() !dbg !12 {
start:
  ret void, !dbg !22
}

define internal void @_ZN3app5fixed17h4444444444444444E() !dbg !13 {
start:
  ret void, !dbg !23
}

define internal void @_ZN3app3odd17h5555555555555555E() !dbg !14 {
start:
  ret void, !dbg !24
}

!2 = !DIFile(filename: "src/main.rs", directory: "/w")
!9 = !DINamespace(name: "app", scope: null)
!10 = distinct !DISubprogram(name: "show<i8>", linkageName: "_ZN3app4show17h1111111111111111E", scope: !9, file: !2, line: 1, unit: !1, templateParams: !30)
!11 = distinct !DISubprogram(name: "show<char>", linkageName: "_ZN3app4show17h2222222222222222E", scope: !9, file: !2, line: 1, unit: !1, templateParams: !32)
!12 = distinct !DISubprogram(name: "main", linkageName: "_ZN3app4main17h3333333333333333E", scope: !9, file: !2, line: 5, unit: !1, templateParams: !34)
!13 = distinct !DISubprogram(name: "fixed<3>", linkageName: "_ZN3app5fixed17h4444444444444444E", scope: !9, file: !2, line: 7, unit: !1, templateParams: !34)
!14 = distinct !DISubprogram(name: "odd<u8>", linkageName: "_ZN3app3odd17h5555555555555555E", scope: !9, file: !2, line: 9, unit: !1, templateParams: !34)
!20 = !DILocation(line: 2, scope: !10)
!21 = !DILocation(line: 2, scope: !11)
!22 = !DILocation(line: 6, scope: !12)
!23 = !DILocation(line: 8, scope: !13)
!24 = !DILocation(line: 10, scope: !14)
!30 = !{!31}
!31 = !DITemplateTypeParameter(name: "i8", type: !40)
!32 = !{!33}
!33 = !DITemplateTypeParameter(name: "i8", type: !41)
!34 = !{}
"""


def test_a_generic_instantiation_names_its_type_parameters(tmp_path):
    # Review of #106: a generic body's MIR types may name a type parameter, whatever its
    # name (`fn show<i8>(p: Pair<i8>)` called with chars).
    from peeky.rust_mir import mentions

    path = tmp_path / "app.ll"
    path.write_text(GENERIC_IR)
    functions = ir_functions(path, ["src/main.rs"])
    generics = {hashed[-1]: function.generics for hashed, function in functions.items()}
    # rustc lists no const parameter (`fixed<3>`): its argument is a value, no type. A
    # name's type argument with no parameter listed for it is not understood.
    assert generics == {"1": ("i8",), "2": ("i8",), "3": (), "4": (), "5": None}
    assert mentions("Pair<i8>", ("i8",)) and mentions("(i8, i8)", ("i8",))
    assert mentions("std::vec::Vec<Pair<T>>", ("T",))
    assert not mentions("chars::i8::Span", ("i8",)) and not mentions("Thing", ("T",))
    assert not mentions("std::vec::Vec<usize>", ("i8",))


def test_a_generic_bodys_primitive_named_types_may_hold_pointers():
    # `fn hold<u64>(w: u64)`: the `u64` may be a `&mut Vec`, so values made from it alias.
    body = mir(
        {
            0: ["_2 = copy _1;", "_3 = &mut _2;", "return;"],
        },
        header="fn main(_1: u64) -> () {",
    )
    body.types.update({1: "u64", 2: "u64", 3: "&mut u64"})
    body.generics = ()
    assert body._aliases(1) == {1}
    body = mir(
        {
            0: ["_2 = copy _1;", "_3 = &mut _2;", "return;"],
        },
        header="fn main(_1: u64) -> () {",
    )
    body.types.update({1: "u64", 2: "u64", 3: "&mut u64"})
    body.generics = ("u64",)
    assert body._aliases(1) == {1, 2, 3}


# #132: `async fn work(n: u32)` with a local the future does not keep across its `.await`
# (`tmp`) and one it does (`before`): lines 1-6 are the fn line, `let tmp = n + 100;`,
# `let before = n * 2;`, `Yield.await;`, `before + 1` and `}`.
SCALARS = "\n".join(
    [
        "fn work::{closure#0}(_1: Pin<&mut {async fn body of work()}>, _2: &mut Context<'_>) -> Poll<u32> {",
        "    debug _task_context => _2;           // in scope 0 at src/main.rs:1:30: 6:2",
        "    debug n => ((*_9).0: u32);           // in scope 0 at src/main.rs:1:15: 1:16",
        "    let mut _0: std::task::Poll<u32>;    // return place in scope 0 at src/main.rs:1:30: 6:2",
        "    let _3: u32;                         // in scope 0 at src/main.rs:1:15: 1:16",
        "    let _4: u32;                         // in scope 1 at src/main.rs:2:9: 2:12",
        "    let mut _5: bool;                    // in scope 0 at src/main.rs:4:5: 4:15",
        "    let mut _6: u32;                     // in scope 0 at src/main.rs:5:5: 5:11",
        "    let mut _8: u32;                     // in scope 0 at src/main.rs:1:30: 6:2",
        "    let mut _9: &mut {async fn body of work()}; // in scope 0 at src/main.rs:1:30: 6:2",
        "    scope 1 {",
        "        debug n => _3;                   // in scope 1 at src/main.rs:1:15: 1:16",
        "        scope 2 {",
        "            debug tmp => _4;             // in scope 2 at src/main.rs:2:9: 2:12",
        "            scope 3 {",
        "                debug before => (((*_9) as variant#3).0: u32); // in scope 3 at src/main.rs:3:9: 3:15",
        "            }",
        "        }",
        "    }",
        "",
        "    bb0: {",
        "        _9 = copy (_1.0: &mut {async fn body of work()}); // scope 0 at src/main.rs:1:30: 6:2",
        "        _8 = discriminant((*_9));        // scope 0 at src/main.rs:1:30: 6:2",
        "        switchInt(move _8) -> [0: bb1, 1: bb6, 2: bb6, 3: bb5, otherwise: bb6]; // scope 0 at src/main.rs:1:30: 6:2",
        "    }",
        "",
        "    bb1: {",
        "        StorageLive(_3);                 // scope 0 at src/main.rs:1:15: 1:16",
        "        _3 = copy ((*_9).0: u32);        // scope 0 at src/main.rs:1:15: 1:16",
        "        StorageLive(_4);                 // scope 1 at src/main.rs:2:9: 2:12",
        "        _4 = Add(copy _3, const 100_u32); // scope 1 at src/main.rs:2:15: 2:22",
        "        (((*_9) as variant#3).0: u32) = Mul(copy _3, const 2_u32); // scope 2 at src/main.rs:3:18: 3:23",
        "        goto -> bb8;                     // scope 3 at src/main.rs:4:5: 4:15",
        "    }",
        "",
        "    bb8: {",
        "        _5 = poll() -> [return: bb3, unwind continue]; // scope 3 at src/main.rs:4:5: 4:15",
        "    }",
        "",
        "    bb3: {",
        "        switchInt(move _5) -> [0: bb7, otherwise: bb4]; // scope 3 at src/main.rs:4:5: 4:15",
        "    }",
        "",
        "    bb4: {",
        "        _6 = copy (((*_9) as variant#3).0: u32); // scope 3 at src/main.rs:5:5: 5:11",
        "        _0 = Poll::<u32>::Ready(move _6); // scope 0 at src/main.rs:6:2: 6:2",
        "        discriminant((*_9)) = 1;         // scope 0 at src/main.rs:6:2: 6:2",
        "        return;                          // scope 0 at src/main.rs:6:2: 6:2",
        "    }",
        "",
        "    bb5: {",
        "        StorageLive(_3);                 // scope 0 at src/main.rs:1:30: 6:2",
        "        StorageLive(_4);                 // scope 0 at src/main.rs:1:30: 6:2",
        "        goto -> bb8;                     // scope 3 at src/main.rs:4:5: 4:15",
        "    }",
        "",
        "    bb6: {",
        "        unreachable;                     // scope 0 at src/main.rs:1:30: 6:2",
        "    }",
        "",
        "    bb7: {",
        "        _0 = Poll::<u32>::Pending;       // scope 3 at src/main.rs:4:10: 4:15",
        "        StorageDead(_4);                 // scope 3 at src/main.rs:4:10: 4:15",
        "        StorageDead(_3);                 // scope 3 at src/main.rs:4:10: 4:15",
        "        discriminant((*_9)) = 3;         // scope 3 at src/main.rs:4:10: 4:15",
        "        return;                          // scope 3 at src/main.rs:4:10: 4:15",
        "    }",
        "}",
        "",
    ]
)


def scalars(text=SCALARS):
    body = parse_body(text)
    blocks = {"start": {1}}
    for number, block in body.blocks.items():
        if number:
            blocks[f"bb{number}"] = {line for _, _, line in block.statements}
    function = IrFunction(
        "{async_fn#0}", "work", "src/main.rs", blocks, {}, 1, ancestors=("app", "work")
    )
    lines = "async fn work(n: u32) -> u32 {\n    let tmp = n + 100;\n    let before = n * 2;\n"
    lines += "    Yield.await;\n    before + 1\n}\n"
    return body, OwnedLocals([body], {"h": function}, SOURCES, texts={"src/main.rs": lines})


def test_a_resumed_poll_leaves_what_the_future_does_not_keep_unassigned():
    body, owned = scalars()
    assert body.supported and body.saved == {10} and body.resumes == {7: 5}
    # The poll's copy of `n` and `tmp`: assigned in the first poll, never after a resume.
    assert body.unassigned_at(3, (4, 0)) and body.unassigned_at(4, (4, 0))
    assert body.unassigned_at(3, (4, 0), resumed=False) is False
    assert body.unassigned_at(10, (4, 0)) is False  # `before`, in the state
    assert owned.unassigned("h", "n @ main.rs:1", 5, MAIN) == (2, {1: "suspended"})
    assert owned.unassigned("h", "tmp", 5, MAIN) == (1, {0: "suspended"})
    assert owned.unassigned("h", "before", 5, MAIN) is None
    assert owned.unassigned("h", "_task_context", 5, MAIN) is None
    # A caller frame inside the `.await`'s poll: everywhere on the line, so withheld.
    assert owned.unassigned("h", "tmp", 4, MAIN, caller=True) == (1, {0: "suspended"})
    # Before the `.await` only the first poll runs: assigned.
    assert owned.unassigned("h", "n @ main.rs:1", 3, MAIN) is None
    assert owned.unassigned("h", "tmp", 3, MAIN) is None
    # Not yet assigned on its own line (lldb-dap shows it only inside its scope).
    assert owned.unassigned("h", "before", 3, MAIN) is None


def test_a_parameter_the_body_reassigns_is_not_what_the_futures_field_holds():
    # `mut n; n += 1` before the `.await`: the field holds what the caller passed.
    text = SCALARS.replace(
        "        (((*_9) as variant#3).0: u32) = Mul(",
        "        _3 = Add(copy _3, const 1_u32);  // scope 2 at src/main.rs:3:5: 3:11\n"
        "        (((*_9) as variant#3).0: u32) = Mul(",
    )
    _, owned = scalars(text)
    assert owned.unassigned("h", "n @ main.rs:1", 5, MAIN) == (2, {0: "passed", 1: "suspended"})
    # Out of the copy's scope, the field alone.
    assert owned.unassigned("h", "n", 6, MAIN) == (1, {0: "passed"})
    _, plain = scalars()
    assert plain.unassigned("h", "n", 6, MAIN) is None
    # Before the reassignment can run, the field is the parameter; on the fn line a later
    # poll's start (its dispatch, its resume) runs after it.
    assert owned.unassigned("h", "n @ main.rs:1", 2, MAIN) is None
    assert owned.unassigned("h", "n", 1, MAIN) == (1, {0: "passed"})


def test_a_let_without_a_value_is_unassigned_until_assigned_and_joins_take_their_edge():
    body = parse_body(
        "fn main() -> () {\n"
        "    let mut _0: ();  // return place in scope 0 at src/main.rs:1:10: 1:10\n"
        "    scope 1 {\n"
        "        debug late => _1;  // in scope 1 at src/main.rs:2:9: 2:13\n"
        "        debug v => _2;  // in scope 1 at src/main.rs:3:9: 3:10\n"
        "    }\n"
        "    bb0: {\n"
        "        StorageLive(_1);  // scope 0 at src/main.rs:2:9: 2:13\n"
        "        _2 = make() -> [return: bb1, unwind: bb4];  // scope 1 at src/main.rs:3:13: 3:19\n"
        "    }\n"
        "    bb1: {\n"
        "        _1 = const 5_u32;  // scope 1 at src/main.rs:4:5: 4:13\n"
        "        drop(_2) -> [return: bb2, unwind: bb3];  // scope 1 at src/main.rs:5:1: 5:2\n"
        "    }\n"
        "    bb2: {\n"
        "        return;  // scope 0 at src/main.rs:5:2: 5:2\n"
        "    }\n"
        "    bb3 (cleanup): {\n"
        "        drop(_9) -> [return: bb4, unwind terminate(cleanup)];  // scope 1 at src/main.rs:5:1: 5:2\n"
        "    }\n"
        "    bb4 (cleanup): {\n"
        "        resume;  // scope 0 at src/main.rs:1:1: 5:2\n"
        "    }\n"
        "}\n"
    )
    assert body.unassigned_at(1, (0, 1)) and body.unassigned_at(1, (1, 0))
    assert body.unassigned_at(1, (1, 1)) is False
    # bb4 joins the unwind from `make()` (before `late` is assigned) with the cleanup
    # drop on line 5: a stop after that drop comes through its edge only.
    assert body.unassigned_at(1, (4, 0))
    assert body.returned_into(4, 5, "src/main.rs", False, {1, 3}) == [3]
    assert body.unassigned_after(1, 4, [3]) is False
    # A call's own result is assigned on its return edge.
    assert body.unassigned_after(2, 1, [0]) is False
    assert body.returned_into(1, 4, "src/main.rs", False, {1}) is None


# #115: `let k = r.x; println!("k = {}", k);`: `format_args!` declares `args` twice, so
# LLDB stops three times on the line, once per lexical block (k's, then each `args`'s).
PRINTED = r"""
fn kay(_1: &Vec<Pt>) -> i64 {
    debug v => _1;                       // in scope 0 at src/main.rs:16:8: 16:9
    let mut _0: i64;                     // return place in scope 0 at src/main.rs:16:24: 16:27
    let _2: &Pt;                         // in scope 0 at src/main.rs:17:9: 17:10
    let _3: &Pt;                         // in scope 0 at src/main.rs:17:15: 17:18
    let mut _4: &std::vec::Vec<Pt>;      // in scope 0 at src/main.rs:17:14: 17:15
    let _6: ();                          // in scope 0 at /rustc/x/library/std/src/macros.rs:145:23: 147:6
    let _7: ();                          // in scope 0 at /rustc/x/library/std/src/macros.rs:146:9: 146:62
    let mut _8: std::fmt::Arguments<'_>; // in scope 0 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
    let mut _10: &i64;                   // in scope 0 at src/main.rs:19:24: 19:25
    let mut _12: core::fmt::rt::Argument<'_>; // in scope 0 at src/main.rs:19:19: 19:21
    let mut _13: &i64;                   // in scope 0 at src/main.rs:19:24: 19:25
    let mut _14: &[u8; 9];               // in scope 0 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
    let _15: &[u8; 9];                   // in scope 0 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
    let mut _16: &[core::fmt::rt::Argument<'_>; 1]; // in scope 0 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
    let _17: &[core::fmt::rt::Argument<'_>; 1]; // in scope 0 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
    let mut _18: i64;                    // in scope 0 at src/main.rs:20:5: 20:6
    let mut _19: (i64, bool);            // in scope 0 at src/main.rs:20:5: 20:10
    let mut _20: &i64;                   // in scope 0 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
    scope 1 {
        debug r => _2;                   // in scope 1 at src/main.rs:17:9: 17:10
        let _5: i64;                     // in scope 1 at src/main.rs:18:9: 18:10
        scope 2 {
            debug k => _5;               // in scope 2 at src/main.rs:18:9: 18:10
            let _9: (&i64,);             // in scope 2 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
            scope 3 {
                debug args => _9;        // in scope 3 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
                let _11: [core::fmt::rt::Argument<'_>; 1]; // in scope 3 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
                scope 4 {
                    debug args => _11;   // in scope 4 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:17:9: 17:10
        StorageLive(_3);                 // scope 0 at src/main.rs:17:15: 17:18
        StorageLive(_4);                 // scope 0 at src/main.rs:17:14: 17:15
        _4 = &(*_1);                     // scope 0 at src/main.rs:17:14: 17:15
        _3 = <Vec<Pt> as Index<usize>>::index(move _4, const 0_usize) -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:17:15: 17:18
    }

    bb1: {
        StorageDead(_4);                 // scope 0 at src/main.rs:17:17: 17:18
        _2 = &(*_3);                     // scope 0 at src/main.rs:17:13: 17:18
        StorageLive(_5);                 // scope 1 at src/main.rs:18:9: 18:10
        _5 = copy ((*_2).0: i64);        // scope 1 at src/main.rs:18:13: 18:16
        StorageLive(_6);                 // scope 2 at /rustc/x/library/std/src/macros.rs:145:23: 147:6
        StorageLive(_7);                 // scope 2 at /rustc/x/library/std/src/macros.rs:146:9: 146:62
        StorageLive(_8);                 // scope 2 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_9);                 // scope 2 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_10);                // scope 2 at src/main.rs:19:24: 19:25
        _10 = &_5;                       // scope 2 at src/main.rs:19:24: 19:25
        _9 = (move _10,);                // scope 2 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
        StorageDead(_10);                // scope 2 at /rustc/x/library/std/src/macros.rs:146:60: 146:61
        StorageLive(_11);                // scope 3 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_12);                // scope 3 at src/main.rs:19:19: 19:21
        StorageLive(_13);                // scope 3 at src/main.rs:19:24: 19:25
        _20 = copy (_9.0: &i64);         // scope 3 at src/main.rs:19:24: 19:25
        _13 = &(*_20);                   // scope 3 at src/main.rs:19:24: 19:25
        _12 = core::fmt::rt::Argument::<'_>::new_display::<i64>(move _13) -> [return: bb2, unwind continue]; // scope 3 at src/main.rs:19:19: 19:21
    }

    bb2: {
        StorageDead(_13);                // scope 3 at src/main.rs:19:20: 19:21
        _11 = [move _12];                // scope 3 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
        StorageDead(_12);                // scope 3 at /rustc/x/library/std/src/macros.rs:146:60: 146:61
        StorageLive(_14);                // scope 4 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_15);                // scope 4 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
        _15 = const b"\x04k = \xc0\x01\n\x00"; // scope 4 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
        _14 = &(*_15);                   // scope 4 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_16);                // scope 4 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
        StorageLive(_17);                // scope 4 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
        _17 = &_11;                      // scope 4 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
        _16 = &(*_17);                   // scope 4 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
        _8 = Arguments::<'_>::new::<9, 1>(move _14, move _16) -> [return: bb3, unwind continue]; // scope 4 at /rustc/x/library/std/src/macros.rs:146:28: 146:61
    }

    bb3: {
        StorageDead(_17);                // scope 4 at /rustc/x/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_16);                // scope 4 at /rustc/x/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_15);                // scope 4 at /rustc/x/library/std/src/macros.rs:146:60: 146:61
        StorageDead(_14);                // scope 4 at /rustc/x/library/std/src/macros.rs:146:60: 146:61
        _7 = std::io::_print(move _8) -> [return: bb4, unwind continue]; // scope 2 at /rustc/x/library/std/src/macros.rs:146:9: 146:62
    }

    bb4: {
        StorageDead(_8);                 // scope 2 at /rustc/x/library/std/src/macros.rs:146:61: 146:62
        StorageDead(_11);                // scope 2 at /rustc/x/library/std/src/macros.rs:146:62: 146:63
        StorageDead(_9);                 // scope 2 at /rustc/x/library/std/src/macros.rs:146:62: 146:63
        StorageDead(_7);                 // scope 2 at /rustc/x/library/std/src/macros.rs:146:62: 146:63
        _6 = const ();                   // scope 2 at /rustc/x/library/std/src/macros.rs:145:23: 147:6
        StorageDead(_6);                 // scope 2 at /rustc/x/library/std/src/macros.rs:147:5: 147:6
        StorageLive(_18);                // scope 2 at src/main.rs:20:5: 20:6
        _18 = copy _5;                   // scope 2 at src/main.rs:20:5: 20:6
        _19 = AddWithOverflow(copy _18, const 1_i64); // scope 2 at src/main.rs:20:5: 20:10
        assert(!move (_19.1: bool), "attempt to compute `{} + {}`, which would overflow", move _18, const 1_i64) -> [success: bb5, unwind continue]; // scope 2 at src/main.rs:20:5: 20:10
    }

    bb5: {
        _0 = move (_19.0: i64);          // scope 2 at src/main.rs:20:5: 20:10
        StorageDead(_18);                // scope 2 at src/main.rs:20:9: 20:10
        StorageDead(_5);                 // scope 1 at src/main.rs:21:1: 21:2
        StorageDead(_3);                 // scope 0 at src/main.rs:21:1: 21:2
        StorageDead(_2);                 // scope 0 at src/main.rs:21:1: 21:2
        return;                          // scope 0 at src/main.rs:21:2: 21:2
    }

}
"""


# The source, with `kay` on its lines.
PRINTED_TEXT = "\n" * 15 + (
    "fn kay(v: &Vec<Pt>) -> i64 {\n"
    "    let r = &v[0];\n"
    "    let k = r.x;\n"
    '    println!("k = {}", k);\n'
    "    k + 1\n"
    "}\n"
)


# `kay`'s IR as the real build has it (trimmed): rustc merges each block a call that
# cannot unwind returns to into its caller's, and `println!`'s calls are collapsed onto
# its column. `args` is spilled for debug info (`args.dbg.spill`) in k's block.
KAY_IR = """define internal i64 @_ZN5chars3kay17h8491af1d40105d89E(ptr align 8 %v) unnamed_addr #0 !dbg !10 {
start:
  %args.dbg.spill = alloca [8 x i8], align 8
  %r.dbg.spill = alloca [8 x i8], align 8
  %v.dbg.spill = alloca [8 x i8], align 8
  %_12 = alloca [16 x i8], align 8
  %args = alloca [16 x i8], align 8
  %k = alloca [8 x i8], align 8
  store ptr %v, ptr %v.dbg.spill, align 8
    #dbg_declare(ptr %v.dbg.spill, !12, !DIExpression(), !30)
    #dbg_declare(ptr %k, !15, !DIExpression(), !31)
    #dbg_declare(ptr %args, !20, !DIExpression(), !32)
; call <alloc::vec::Vec<T,A> as core::ops::index::Index<I>>::index
  %_3 = call align 8 ptr @index(ptr align 8 %v, i64 0, ptr align 8 @alloc_e2) #16, !dbg !33
  store ptr %_3, ptr %r.dbg.spill, align 8, !dbg !34
    #dbg_declare(ptr %r.dbg.spill, !13, !DIExpression(), !35)
  %0 = load i64, ptr %_3, align 8, !dbg !36
  store i64 %0, ptr %k, align 8, !dbg !36
  store ptr %k, ptr %args.dbg.spill, align 8, !dbg !37
    #dbg_declare(ptr %args.dbg.spill, !17, !DIExpression(), !38)
; call core::fmt::rt::Argument::new_display
  call void @new_display(ptr sret([16 x i8]) align 8 %_12, ptr align 8 %k) #16, !dbg !38
  %1 = getelementptr inbounds nuw %"core::fmt::rt::Argument<'_>", ptr %args, i64 0, !dbg !38
  call void @llvm.memcpy.p0.p0.i64(ptr align 8 %1, ptr align 8 %_12, i64 16, i1 false), !dbg !38
; call core::fmt::Arguments::new
  %2 = call { ptr, ptr } @new(ptr @alloc_7b, ptr align 8 %args) #16, !dbg !32
  %_8.0 = extractvalue { ptr, ptr } %2, 0, !dbg !32
  %_8.1 = extractvalue { ptr, ptr } %2, 1, !dbg !32
; call std::io::stdio::_print
  call void @_print(ptr %_8.0, ptr %_8.1), !dbg !37
  %_18 = load i64, ptr %k, align 8, !dbg !39
  %3 = call { i64, i1 } @llvm.sadd.with.overflow.i64(i64 %_18, i64 1), !dbg !39
  %_19.0 = extractvalue { i64, i1 } %3, 0, !dbg !39
  %_19.1 = extractvalue { i64, i1 } %3, 1, !dbg !39
  br i1 %_19.1, label %panic, label %bb5, !dbg !39

bb5:                                              ; preds = %start
  ret i64 %_19.0, !dbg !40

panic:                                            ; preds = %start
  call void @panic_const_add_overflow(ptr align 8 @alloc_86) #20, !dbg !39
  unreachable, !dbg !39
}

!2 = !DIFile(filename: "src/main.rs", directory: "/w")
!3 = !DIFile(filename: "/rustc/x/library/std/src/macros.rs", directory: "")
!9 = !DINamespace(name: "chars", scope: null)
!10 = distinct !DISubprogram(name: "kay", linkageName: "_ZN5chars3kay17h8491af1d40105d89E", scope: !9, file: !2, line: 16, unit: !1, retainedNodes: !11)
!11 = !{!12, !13, !15, !17, !20}
!12 = !DILocalVariable(name: "v", arg: 1, scope: !10, file: !2, line: 16, type: !7)
!13 = !DILocalVariable(name: "r", scope: !14, file: !2, line: 17, type: !7, align: 64)
!14 = distinct !DILexicalBlock(scope: !10, file: !2, line: 17, column: 5)
!15 = !DILocalVariable(name: "k", scope: !16, file: !2, line: 18, type: !7, align: 64)
!16 = distinct !DILexicalBlock(scope: !14, file: !2, line: 18, column: 5)
!17 = !DILocalVariable(name: "args", scope: !18, file: !2, line: 19, type: !7, align: 64)
!18 = !DILexicalBlockFile(scope: !19, file: !2, discriminator: 0)
!19 = distinct !DILexicalBlock(scope: !16, file: !3, line: 146, column: 28)
!20 = !DILocalVariable(name: "args", scope: !21, file: !2, line: 19, type: !7, align: 64)
!21 = !DILexicalBlockFile(scope: !22, file: !2, discriminator: 0)
!22 = distinct !DILexicalBlock(scope: !19, file: !3, line: 146, column: 28)
!30 = !DILocation(line: 16, column: 8, scope: !10)
!31 = !DILocation(line: 18, column: 9, scope: !16)
!32 = !DILocation(line: 19, column: 5, scope: !21)
!33 = !DILocation(line: 17, column: 15, scope: !10)
!34 = !DILocation(line: 17, column: 13, scope: !10)
!35 = !DILocation(line: 17, column: 9, scope: !14)
!36 = !DILocation(line: 18, column: 13, scope: !14)
!37 = !DILocation(line: 19, column: 5, scope: !16)
!38 = !DILocation(line: 19, column: 5, scope: !18)
!39 = !DILocation(line: 20, column: 5, scope: !16)
!40 = !DILocation(line: 21, column: 2, scope: !10)
"""

K = (("k",), ("r",), ("v",))
ARGS = (("args",), *K)
INNER = (("args",), *ARGS)


def printed_function(tmp_path, ir=KAY_IR):
    path = tmp_path / "kay.ll"
    path.write_text(ir)
    return ir_functions(path, ["src/main.rs"])["h8491af1d40105d89"]


def test_the_ir_names_each_lexical_block_and_its_code(tmp_path):
    # #115: each instruction's scope, named as the DWARF names the block holding a stop;
    # a DILexicalBlockFile is its block's.
    function = printed_function(tmp_path)
    assert function.blocks == {"start": {17, 18, 19, 20}, "bb5": {21}, "panic": {20}}
    assert function.events["start"] == (((17, 15), (19, 5), (19, 5), (19, 5)), ((20, 5),))
    assert function.scoped
    assert function.chains == {(("v",),): 1, (("r",), ("v",)): 1, K: 1, ARGS: 1, INNER: 1}
    assert function.line_chains[(19, K)] == function.line_chains[(19, INNER)] == 1
    # Calls (not intrinsics), stores of bytes, a branch on a value; each with the calls
    # before it in its IR block. A store with no location (the prologue's) is none.
    assert function.sure["start"] == (
        (17, (("v",),), 0),
        (17, (("v",),), 1),
        (18, (("r",), ("v",)), 1),
        (19, K, 1),
        (19, ARGS, 1),
        (19, INNER, 2),
        (19, K, 3),
        (20, K, 4),
    )
    assert function.calls == {"start": 4, "bb5": 0, "panic": 1}
    # A store of nothing or of an undefined value, or a branch on a constant, leaves none.
    for line in ("store {} %e, ptr %p", "store i64 undef, ptr %p", "br i1 true, label %a"):
        changed = KAY_IR.replace(
            "  %_18 = load i64, ptr %k, align 8, !dbg !39\n", f"  {line}, !dbg !39\n"
        )
        assert printed_function(tmp_path, changed).sure == function.sure
    # A block with no variable of its own has no DWARF block: its code is its parent's.
    changed = KAY_IR.replace('!17 = !DILocalVariable(name: "args"', "!17 = !DILocalVariable(")
    function = printed_function(tmp_path, changed)
    assert INNER not in function.chains and function.chains[ARGS] == function.chains[K] == 1
    assert (19, K, 1) in function.sure["start"] and (19, ARGS, 2) in function.sure["start"]


def kay_blocks():
    from peeky.rust_symbols import CodeBlock

    # As the real build's DWARF has them (addresses less the image base).
    return (
        CodeBlock(None, ((0xE0C, 0xEA4),), ("v",)),
        CodeBlock(0, ((0xE38, 0xE88), (0xE98, 0xEA4)), ("r",)),
        CodeBlock(1, ((0xE44, 0xE88), (0xE98, 0xEA4)), ("k",)),
        CodeBlock(2, ((0xE50, 0xE6C),), ("args",)),
        CodeBlock(3, ((0xE60, 0xE6C),), ("args",)),
        CodeBlock(3, ((0xE64, 0xE68),), (), inlined=True),
    )


def test_a_stop_address_names_its_lexical_block_and_the_blocks_around_it(tmp_path):
    blocks = kay_blocks()
    assert stop_block(blocks, 0xE44).chain == (("k",), ("r",), ("v",))
    assert stop_block(blocks, 0xE50).chain == (("args",), ("k",), ("r",), ("v",))
    inner = stop_block(blocks, 0xE60)
    assert inner.chain == (("args",), ("args",), ("k",), ("r",), ("v",))
    assert inner.chains[(("k",), ("r",), ("v",))] == 1
    # Outside the function, in an inlined call (another frame's), or no blocks: unknown.
    assert stop_block(blocks, 0xEA4) is None and stop_block(blocks, 0xE64) is None
    assert stop_block((), 0xE44) is None and stop_block(blocks, None) is None
    body = parse_body(PRINTED)
    assert body.scope_chain(4) == inner.chain and body.scope_chain(0) == (("v",),)
    function = printed_function(tmp_path)
    assert body.chain_scopes(stop_block(blocks, 0xE50), function) == {0, 1, 2, 3}
    # Without the IR's blocks, or with fewer of them than the DWARF's: none.
    assert body.chain_scopes(stop_block(blocks, 0xE50)) is None
    function.chains[ARGS] = 2
    assert body.chain_scopes(stop_block(blocks, 0xE50), function) is None


def test_a_macro_s_own_stops_are_told_apart_by_their_lexical_block(tmp_path):
    # #115: all three stops report the macro's column; the block keeps only its places,
    # and at a breakpoint (the line's lowest address in the block) only those up to the
    # IR's first instruction of the line there that surely leaves code.
    body = parse_body(PRINTED)
    function = printed_function(tmp_path)
    blocks = kay_blocks()
    line = {0, 1, 2, 3, 4}
    shared = dict.fromkeys(line, line)

    def places(pc, facts=None, function=function):
        within = None if pc is None else stop_block(blocks, pc)
        return body.stop_positions(line, 19, "src/main.rs", True, facts, shared, within, function)

    every = places(None)
    # A step in k's block: its own statements, the `_print` call after the macro's.
    assert set(places(0xE44)) < set(every) and (3, 4) in places(0xE44)
    assert (1, 15) not in places(0xE44)
    breakpoint = LineFacts(5, function, frozenset(), frozenset({5}), int)
    # Its lowest address in k's block is at or before `args`'s spill, the IR's first
    # store of the line there: before `Argument::new_display`, the next call.
    assert places(0xE44, breakpoint) == [(1, 9), (1, 10)]
    # The MIR's spans do not say which line a macro's code is collapsed onto: where the
    # IR puts that store on another line, the first code of the line in k's block is
    # `_print`'s call, past the macro's other calls.
    moved = KAY_IR.replace(
        "  store ptr %k, ptr %args.dbg.spill, align 8, !dbg !37",
        "  store ptr %k, ptr %args.dbg.spill, align 8, !dbg !41",
    ).replace("!40 = ", "!41 = !DILocation(line: 18, column: 5, scope: !16)\n!40 = ")
    later = places(0xE44, breakpoint, printed_function(tmp_path, moved))
    assert later == [(1, 9), (1, 10), (3, 4)]
    assert not body.used_before(body.local("r"), (3, 4))
    # The IR's call after that store is the MIR's second (`new_display`'s block, bb1). One
    # LLVM inlined leaves no call: then the next may be a MIR call later (bb2), no
    # further. More IR calls than the MIR's tell nothing.
    last = {number: len(body.blocks[number].statements) - 1 for number in (1, 2)}
    assert body._cut(function, 19, K) == (0, 1, last[1])
    inlined = KAY_IR.replace(
        "  call void @new_display(ptr sret([16 x i8]) align 8 %_12, ptr align 8 %k) #16, !dbg !38",
        "  store i64 0, ptr %_12, align 8, !dbg !43",
    ).replace(
        "!40 = ",
        '!42 = distinct !DISubprogram(name: "new_display", file: !3, line: 1, unit: !1)\n'
        "!43 = !DILocation(line: 1, column: 1, scope: !42, inlinedAt: !38)\n!40 = ",
    )
    assert body._cut(printed_function(tmp_path, inlined), 19, K) == (0, 2, last[2])
    extra = KAY_IR.replace(
        "  %_18 = load i64, ptr %k, align 8, !dbg !39\n",
        "  call void @extra(), !dbg !39\n  %_18 = load i64, ptr %k, align 8, !dbg !39\n",
    )
    assert body._cut(printed_function(tmp_path, extra), 19, K) is None
    # In the first `args`'s: up to `Argument::new_display`, the first call.
    assert places(0xE50, breakpoint) == [(1, 15), (1, 16), (1, 17)]
    r = body.local("r")
    for pc in (0xE44, 0xE50):
        assert all(body.used_before(r, at) for at in places(pc, breakpoint))
    # In the second's: past that call, and past `r`'s last use.
    after = places(0xE60, breakpoint)
    assert after and all(at > (1, 17) for at in after)
    assert not any(body.used_ahead(r, at) or body.used_before(r, at) for at in after)
    # A block the debug info has fewer of than the body (one with no code), or not the
    # stop's at all: every place counts.
    fewer = stop_block(blocks, 0xE50)
    fewer.chains.pop((("args",), ("args",), ("k",), ("r",), ("v",)))
    assert (
        body.stop_positions(line, 19, "src/main.rs", True, None, shared, fewer, function) == every
    )
    assert places(0xE38) == every
    # Without the IR's blocks, or where the IR has two blocks with the stop's names on
    # the line: every place counts.
    assert places(0xE44, breakpoint, None) == places(None, breakpoint)
    function.line_chains[(19, K)] = 2
    assert places(0xE44, breakpoint) == places(None, breakpoint)


def test_a_macro_s_own_stops_judge_the_references_of_their_block(tmp_path):
    owned = OwnedLocals(
        [parse_body(PRINTED)],
        {"h": printed_function(tmp_path)},
        SOURCES,
        texts={"src/main.rs": PRINTED_TEXT},
    )
    blocks = kay_blocks()

    def judged(name, breakpoint, pc=None):
        within = None if pc is None else stop_block(blocks, pc)
        return owned.reference_valid("h", name, 19, MAIN, breakpoint, 5, within)

    assert [judged("r", True, pc) for pc in (0xE44, 0xE50, 0xE60)] == [True, True, False]
    # Without the address, as before: gated at every stop.
    assert judged("r", True) is False
    # A step may stop in k's block after `_print`: `r` past its last use and a call.
    assert judged("r", False, 0xE44) is False
    # The macro's own `args` of the stop's block, set there.
    assert judged("args", True, 0xE50) is True
    assert judged("v", True, 0xE60) is True


def test_a_body_no_function_was_matched_to_may_have_any_type_parameter():
    # Review of #106: a macro's twins are never matched, so their type parameters are
    # unknown: a `usize` there may hold a pointer too.
    body = mir(
        {0: ["_2 = copy _1;", "_3 = &mut _2;", "return;"]}, header="fn main(_1: u64) -> () {"
    )
    body.types.update({1: "u64", 2: "u64", 3: "&mut u64"})
    assert body.generics is None and body._aliases(1) == {1, 2, 3}


def test_a_generic_name_rustc_cannot_be_read_from_is_not_understood():
    # Review 3 of #106: with no parameter list read, a name whose arguments cannot be
    # read was taken for a non-generic function's.
    from peeky.rust_mir import _generics

    meta = {"!1": ("list", {"items": []})}
    assert _generics({}, {"name": "f>"}) is None and _generics({}, {"name": "main"}) == ()
    assert _generics({}, {"name": "show<u8>"}) is None
    # A char const is named by a hash (`ch::<'a'>`, rustc 1.96): a value, never a type.
    assert _generics(meta, {"name": "ch<{CONST#27d96aba666aa2f7}>", "templateParams": "!1"}) == ()
    assert _generics(meta, {"name": "ng<-3>", "templateParams": "!1"}) == ()


def test_an_alias_graph_is_built_again_when_the_type_parameters_become_known():
    # Review 3 of #106: built while they were unknown (an `#[inline(always)]` body not yet
    # matched), it kept every link once they were known.
    body = mir(
        {0: ["_2 = copy _1;", "_3 = &mut _2;", "return;"]}, header="fn main(_1: u64) -> () {"
    )
    body.types.update({1: "u64", 2: "u64", 3: "&mut u64"})
    assert body._aliases(1) == {1, 2, 3}
    body.generics = ()
    assert body._aliases(1) == {1}
    body.generics = ("u64",)
    assert body._aliases(1) == {1, 2, 3}


def test_an_unassigned_local_is_judged_where_its_stop_block_and_rows_put_the_pc(tmp_path):
    # #132 with #115: `k` assigned only inside `println!`'s blocks (a synthetic body): a
    # stop told apart by its lexical block, or whose rows lldb-dap lists there (`args`,
    # bound only in those blocks), is judged at that block's places alone.
    def kay(assigned_before):
        text = PRINTED.replace(
            "        _5 = copy ((*_2).0: i64);        // scope 1 at src/main.rs:18:13: 18:16\n", ""
        ).replace(
            assigned_before,
            "        _5 = copy ((*_2).0: i64);        // scope 3 at src/main.rs:18:13: 18:16\n"
            + assigned_before,
        )
        return OwnedLocals(
            [parse_body(text)],
            {"h": printed_function(tmp_path)},
            SOURCES,
            texts={"src/main.rs": PRINTED_TEXT},
        )

    blocks = kay_blocks()
    inner = kay("        StorageDead(_13);                // scope 3 at src/main.rs:19:20: 19:21\n")
    for pc, verdict in ((None, (1, {0: "unassigned"})), (0xE44, (1, {0: "unassigned"}))):
        within = None if pc is None else stop_block(blocks, pc)
        assert inner.unassigned("h", "k", 19, MAIN, True, 5, block=within) == verdict
    assert inner.unassigned("h", "k", 19, MAIN, True, 5, block=stop_block(blocks, 0xE50))
    assert inner.unassigned("h", "k", 19, MAIN, True, 5, block=stop_block(blocks, 0xE60)) is None
    outer = kay("        _20 = copy (_9.0: &i64);         // scope 3 at src/main.rs:19:24: 19:25\n")
    rows = ["v", "r", "k"]
    assert outer.unassigned("h", "k", 19, MAIN, True, 5, shown=rows) == (1, {0: "unassigned"})
    assert outer.unassigned("h", "k", 19, MAIN, True, 5, shown=[*rows, "args"]) is None
    assert outer.unassigned("h", "k", 19, MAIN, False, shown=[*rows, "args", "args"]) is None
