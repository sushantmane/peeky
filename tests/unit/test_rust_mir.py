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
    _root_access,
    _same_path,
    _segments,
    frame_shape,
    ir_functions,
    load_bodies,
    may_invoke_macro,
    parse_body,
    stop_block,
)


def mir(blocks, header="fn main() -> () {", bindings="", files=None, types=None):
    """A `-Zdump-mir` body: blocks are {number: [statement, ..., terminator]}; ``files``:
    {(block, index): the file a statement's span is in}, else src/main.rs; ``types``:
    {local: its type}, declared after `_1`."""
    lines = [
        "// MIR for `main` after PreCodegen",
        "",
        header,
        "    let mut _0: ();                      // return place in scope 0 at src/main.rs:1:10: 1:10",
        "    let _1: std::vec::Vec<i32>;          // in scope 0 at src/main.rs:2:9: 2:10",
        *(
            f"    let mut _{local}: {kind};   // in scope 0 at src/main.rs:3:9: 3:10"
            for local, kind in (types or {}).items()
        ),
        "    scope 1 {",
        *(f"        {line}" for line in bindings.splitlines() if line),
        "    }",
        "",
    ]
    for number, statements in blocks.items():
        lines.append(f"    bb{number}: {{")
        for index, statement in enumerate(statements):
            file = (files or {}).get((number, index), "src/main.rs")
            lines.append(f"        {statement}  // scope 1 at {file}:{10 + index}:5: 10:9")
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


def test_a_reference_judged_unproven_says_what_may_have_run_since_its_last_use(tmp_path):
    # #259: the reason a reference shows only its pointer is the one that held: not yet
    # assigned, a call on lines 16 and 17 since its last use, or a stop no line places.
    path = tmp_path / "app.ll"
    path.write_text(INLINED_IR)
    owned = OwnedLocals([parse_body(INLINED)], ir_functions(path, ["src/main.rs"]), SOURCES)
    for breakpoint in (False, True):
        expected = {
            14: [("unassigned",)],
            17: [("since", frozenset({("call", 16), ("call", 17)}))],
            19: [("elsewhere",)],
        }
        for line, reasons in expected.items():
            why = []
            assert (
                owned.reference_valid(
                    "h00000000000000ee", "first", line, MAIN, breakpoint, 5, why=why
                )
                is False
            )
            assert why == reasons, (line, why)
        why = []
        assert owned.reference_valid(
            "h00000000000000ee", "first", 15, MAIN, breakpoint, 13, why=why
        )
        assert why == []


# A reference made into a raw pointer for a comparison (kitty's `if ptr::eq(f, e.payer)`):
# its last use, then the call, which a stop on the line may be past.
COMPARED = {
    0: [
        "_29 = copy (((_23 as Some).0: (usize, &Friend)).1: &Friend);",
        "_31 = &raw const (*_29);",
        "_32 = copy ((*_13).0: *const Friend);",
        "_30 = {} -> [return: bb1, unwind continue];",
    ],
    1: ["switchInt(move _30) -> [0: bb2, otherwise: bb2];"],
    2: ["return;"],
}


# What the dump declares them (`let mut _31: *const Friend;`).
POINTERS = {29: "&Friend", 30: "bool", 31: "*const Friend", 32: "*const Friend"}


def compared(call, *more, types=POINTERS):
    blocks = {number: list(statements) for number, statements in COMPARED.items()}
    blocks[0][3] = blocks[0][3].format(call)
    blocks[2] = [*more, *blocks[2]]
    return mir(blocks, types=types)


@pytest.mark.parametrize(
    "call",
    [
        "std::ptr::eq::<Friend>(move _31, move _32)",
        "core::ptr::eq::<dyn Fn()>(move _31, move _32)",
        "addr_eq::<Friend, Friend>(move _31, move _32)",
        "std::ptr::addr_eq::<Friend, (i64, i64)>(move _31, move _32)",
    ],
)
def test_the_standard_address_comparisons_free_nothing_a_reference_points_at(call):
    # #259: making `f` a raw pointer is its last use; past the comparison's call, what it
    # points at still stands, since the call only compares two addresses. The raw pointer
    # it was made into is no use of `f`.
    body = compared(call)
    assert body.types[31] == "*const Friend"
    assert not body.used_ahead(29, (1, 0))
    assert body.used_before(29, (0, 3)) and body.used_before(29, (0, 3), compares=True)
    assert not body.used_before(29, (1, 0))
    assert body.gaps_before(29, (1, 0)) == {(0, 3)}
    assert body.used_before(29, (1, 0), compares=True)
    assert body.gaps_before(29, (1, 0), compares=True) == set()


@pytest.mark.parametrize(
    "call",
    [
        # The crate's own function, which may free it (a `&mut` alias, interior
        # mutability), and an `eq` the dump does not name the standard library's.
        "same(move _31, move _32)",
        "eq::<Friend>(move _31, move _32)",
        "ptr::eq::<Friend>(move _31, move _32)",
        "kitty::std::ptr::eq::<Friend>(move _31, move _32)",
        # Not two raw pointers (#272 review: a crate's own `std::ptr::eq` taking a Box,
        # which it drops), or a constant.
        "std::ptr::eq::<Friend>(move _31, move _32, const ())",
        "std::ptr::eq::<Friend>(move _31, copy _1)",
        "std::ptr::eq::<Friend>(move _31)",
        "std::ptr::eq::<Friend>(move _31, const 0_usize as *const Friend)",
    ],
)
def test_another_call_after_a_reference_s_last_use_still_ends_its_proof(call):
    body = compared(call)
    assert not body.used_before(29, (1, 0), compares=True)
    assert body.gaps_before(29, (1, 0), compares=True) == {(0, 3)}


def test_a_comparison_of_values_not_declared_raw_pointers_is_a_call():
    for kind in ("std::boxed::Box<Friend>", "&Friend", "usize"):
        body = compared("std::ptr::eq::<Friend>(move _31, move _32)", types={**POINTERS, 32: kind})
        assert not body.used_before(29, (1, 0), compares=True), kind
    body = compared("std::ptr::eq::<Friend>(move _31, move _32)", types={})
    assert not body.used_before(29, (1, 0), compares=True)


def test_a_comparison_s_result_stored_into_a_borrowed_local_is_a_write():
    body = compared("std::ptr::eq::<Friend>(move _31, move _32)", "_40 = &_30;")
    assert not body.used_before(29, (1, 0), compares=True)


def test_every_gap_since_the_last_use_is_found_on_every_path_back():
    body = mir(
        {
            0: [
                "_3 = copy _2;",
                "_4 = copy (*_3);",
                "switchInt(move _4) -> [0: bb1, otherwise: bb2];",
            ],
            1: ["_5 = grow(move _6) -> [return: bb3, unwind continue];"],
            2: ["drop(_7) -> [return: bb3, unwind continue];"],
            3: ["_8 = shrink(move _9) -> [return: bb4, unwind continue];"],
            4: ["return;"],
        }
    )
    assert body.gaps_before(3, (4, 0)) == {(1, 0), (2, 0), (3, 0)}
    assert len(body.gaps_before(3, (4, 0), every=False)) == 1
    assert len(body.gaps_before(3, (4, 0), limit=2)) == 2
    # A path from the function's start has no use; its own storage's end stops a path.
    assert (None, None) in body.gaps_before(12, (4, 0))
    body.blocks[3].statements.insert(0, ("StorageDead(_3);", "src/main.rs", 10))
    assert body.gaps_before(3, (4, 0)) == {(3, 0), (3, 1)}


def owned_crate(text, defined=()):
    return OwnedLocals([], {}, ("src/main.rs",), texts={"src/main.rs": text}, defined=defined)


@pytest.mark.parametrize(
    "text",
    [
        "use std::ptr;\nfn main() { ptr::eq(a, b); std::ptr::addr_eq(a, b); }\n",
        'use std::ptr;\n// fn addr_eq\nfn main() { let s = "mod std"; ptr::addr_eq(a, b); }\n',
        "fn main() { core :: ptr :: addr_eq(a, b); }\n",
        # A path through `std` in a macro's arguments declares nothing.
        'fn main() { assert!(std::ptr::eq(a, b)); println!("{}", core :: mem::size_of::<u8>()); }\n',
        "extern crate core;\nfn main() { let stdout = 1; m!(stdout); }\n",
    ],
)
def test_the_dump_s_address_comparisons_are_the_standard_library_s(text):
    owned = owned_crate(text, defined={"main", "<impl at src/main.rs:3:1: 3:9>::eq", "x::eq"})
    assert owned._std_compares() is True


@pytest.mark.parametrize(
    "text",
    [
        # A `std` or `core` of the crate's own: the dump names its items `std::…` too.
        "mod std { pub mod ptr {} }\n",
        "use crate::m as core;\n",
        "macro_rules! m { () => { mod std {} } }\n",
        # An `addr_eq` of its own, which the dump would name alone.
        "fn addr_eq(a: *const u8, b: *const u8) -> bool { a == b }\n",
        "use std::ptr::addr_eq;\nfn main() { addr_eq(a, b); }\n",
        # A source that does not lex.
        'fn main() { let s = "open; }\n',
        # A macro may declare `mod std` from its argument (#272 review), and `extern
        # crate` may take the name.
        "macro_rules! make_mod { ($n:ident) => { mod $n {} } }\nmake_mod!(std);\n",
        "make_mod! { core }\n",
        "make_mod![r#std];\n",
        "outer!(inner!(std));\n",
        "extern crate core as std;\n",
        "extern  crate alloc as core;\n",
        # Raw identifiers on either side (#272 review, round 2).
        "extern crate evil as r#std;\n",
        "extern crate evil as r#core;\n",
        "extern crate r#evil as std;\n",
        "extern crate r#evil\nas\tcore;\n",
    ],
)
def test_a_crate_that_may_name_them_itself_trusts_none(text):
    assert owned_crate(text)._std_compares() is False


@pytest.mark.parametrize(
    "name", ["std::ptr::eq", "core::ptr::eq", "std::ptr::addr_eq", "inner::addr_eq", "addr_eq"]
)
def test_a_crate_whose_dump_holds_its_own_comparison_trusts_none(name):
    # #272 review: `make_mod!(std)` declared `mod std { pub mod ptr { fn eq } }`, dumped
    # as `fn std::ptr::eq(…)`, and its calls as `std::ptr::eq::<Friend>(…)`. The
    # standard library's bodies are never in the crate's dump.
    assert owned_crate("fn main() {}\n", defined={"main", name})._std_compares() is False
    assert owned_crate("fn main() {}\n", defined={"main"})._std_compares() is True
    body = mir({0: ["return;"]}, header=f"fn {name}(_1: *const T) -> bool {{")
    assert body.path == name
    owned = OwnedLocals([body], {}, ("src/main.rs",), texts={"src/main.rs": "fn main() {}\n"})
    assert owned._std_compares() is False


def test_every_function_dumped_is_named_parsed_or_not(tmp_path):
    (tmp_path / "kitty.main.3-2-007.PreCodegen.after.mir").write_text(
        "// MIR for `main` after PreCodegen\n\nfn main() -> () {\n}\n"
    )
    (tmp_path / "kitty.std#1-ptr-eq.3-2-007.PreCodegen.after.mir").write_text(
        "fn std::ptr::eq(_1: *const T, _2: Box<T>, _3: ()) unparsed\n"
    )
    defined = set()
    load_bodies(tmp_path, "kitty", defined)
    assert defined == {"main", "std::ptr::eq"}


def test_the_note_names_what_may_have_run_since_a_reference_s_last_use():
    from peeky.rust import _gap_reason

    def since(*gaps):
        return [("since", frozenset(gaps))]

    assert _gap_reason("f", since(("call", 67)), 67) == (
        "the stop may be past a call on this line that runs after `f`'s last use; probe a "
        "line that uses `f` before any call"
    )
    assert _gap_reason("first", since(("call", 16), ("call", 17), ("write", 15)), 17) == (
        "`first` is not used ahead, and calls on lines 16 and 17 may have run since its last "
        "use; probe a line that still uses `first`"
    )
    assert _gap_reason("r", since(("drop", 4), ("call", None)), 9) == (
        "`r` is not used ahead, and a call in a macro's code and a drop on line 4 may have "
        "run since its last use; probe a line that still uses `r`"
    )
    calls = since(*(("call", n) for n in (3, 5, 7, 9, 11, 13)), ("call", None))
    assert "calls on lines 3, 5, 7, 9 and others, and in a macro's code" in _gap_reason(
        "r", calls, 20
    )
    # Only a write or a scope's end: named, as nothing else may have run.
    assert "and a write on line 8 may have run" in _gap_reason("r", since(("write", 8)), 9)
    assert "the end of a variable's scope on line 8" in _gap_reason("r", since(("end", 8)), 9)
    assert _gap_reason("r", [("dropped",)], 9) == (
        "what `r` points at may be dropped here, before a new value is stored there"
    )
    assert _gap_reason("r", [("captured",)], 9) == _gap_reason("r", [("dropped",)], 9)
    assert _gap_reason("r", [("unassigned",)], 9) == "`r` may not be assigned yet here"
    for reasons in ([], [("since", frozenset())], since(("start", None), ("call", 3))):
        assert _gap_reason("r", reasons, 9).startswith("`r` is not used ahead, and the MIR")


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


@pytest.mark.parametrize(
    "text, spans, complete",
    [
        # Review of #156, round 3: rustc skips a shebang line, so its `"` opens no string
        # (which hid the `unsafe` below it).
        ('#!/usr/bin/env -S echo "\nlet a = "x";', [(0, 24, True), (33, 36, False)], True),
        ('\ufeff#!/bin/sh "\nfn f() {}', [(0, 12, True)], True),
        # `#!` and `[` (whitespace between) are an inner attribute.
        ("#![allow(x)]\nfn f() {}", [], True),
        ("#!\n [allow(x)] fn f() {}", [], True),
        ("#!\u200e[allow(x)]", [], True),
        # Not judged: a comment after `#!`, and frontmatter.
        ("#! /* c */ [allow(x)]", [], False),
        ('---\nx = "\n---\nfn f() {} // "', [], False),
        ("#!/bin/sh\n\n---\n---\nfn f() {}", [], False),
    ],
)
def test_a_shebang_is_skipped_as_rustc_skips_it(text, spans, complete):
    from peeky.rust_lexer import lexemes

    assert lexemes(text) == (spans, complete)


@pytest.mark.parametrize(
    "text, why",
    [
        ("fn f() { (1 }", "its brackets do not pair up"),
        ("fn f() { [1] ", "its brackets do not pair up"),
        ("fn f() { 1 } }", "its brackets do not pair up"),
        ("fn f() { let c = '}'; let s = \"{\"; }", None),
    ],
)
def test_code_whose_brackets_do_not_pair_up_is_not_read(text, why):
    # Every source rustc accepts pairs its brackets (its tokens are trees): ones that do
    # not, once comments and literals are blanked, mean Peeky misread a literal.
    from peeky.rust_lexer import code_only, scan

    assert scan(text)[1] == why
    assert (code_only(text) is None) is (why is not None)


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


def test_temporaries_holding_a_copy_of_a_local_are_found_and_judged_live():
    # Review of #105: `let t = s.a + f();` copies `s.a` into `_4` before calling `f`;
    # back from `f`, a change to `s.a` misses `t`.
    bindings = "debug s => _1;   // in scope 1 at src/main.rs:2:9: 2:10\n"
    bindings += "debug t => _3;   // in scope 1 at src/main.rs:3:9: 3:10\n"
    bindings += "debug r => _9;   // in scope 1 at src/main.rs:4:9: 4:10\n"
    body = mir(
        {
            0: [
                "_4 = copy (_1.0: i32);",
                "_5 = f() -> [return: bb1, unwind continue];",
            ],
            1: [
                "_6 = AddWithOverflow(copy _4, copy _5);",
                "_3 = move (_6.0: i32);",
                "_7 = &(_1.0: i32);",  # a pointer to it is no copy
                "_8 = (move _7,);",
                "_10 = copy ((*_9).0: i32);",  # read through a pointer that may alias it
                "_11 = g(move _8) -> [return: bb2, unwind continue];",  # given a pointer
                "_12 = h() -> [return: bb2, unwind continue];",  # given nothing
                "_13 = copy _3;",  # a copy the program's own `t` holds
            ],
            2: ["StorageDead(_4);", "return;"],
        },
        bindings=bindings,
    )
    assert body.copies_of(1) == frozenset({4, 6, 10, 11})
    # Live back from `f` (bb1's start), not at the statement's start (before `_4 = ...`).
    assert body.used_ahead(4, (1, 0)) and not body.used_ahead(4, (0, 0))


def test_format_args_constructors_keep_pointers_and_are_no_copies():
    # Review of #105 (round 2): `println!("{} {}", s.a, f())` gives `format_args!` a
    # pointer to `s.a`; what its constructors return keeps it (a later change is seen),
    # while any other call given that pointer may return a copy.
    bindings = "debug s => _1;   // in scope 1 at src/main.rs:2:9: 2:10\n"
    body = mir(
        {
            0: [
                "_4 = &(_1.0: i32);",
                "_3 = core::fmt::rt::Argument::<'_>::new_display::<i32>(copy _4) "
                "-> [return: bb1, unwind continue];",
            ],
            1: [
                "_6 = [move _3];",
                '_5 = Arguments::<\'_>::new::<7, 2>(const "", move _6) '
                "-> [return: bb2, unwind continue];",
            ],
            2: [
                "_8 = &(_1.0: i32);",
                "_7 = get(copy _8) -> [return: bb3, unwind continue];",
                "_9 = Arguments::<'_>::new(copy _8) -> [return: bb3, unwind continue];",
            ],
            3: ["return;"],
        },
        bindings=bindings,
    )
    body.types.update(
        {3: "core::fmt::rt::Argument<'_>", 5: "std::fmt::Arguments<'_>", 9: "Arguments<'_>"}
    )
    # `_9` is the program's own `Arguments`: its type is no standard library's.
    assert body.copies_of(1) == frozenset({7, 9})


TUPLE_FIRST = """// MIR for `ref_first` after PreCodegen

fn ref_first(_1: i32) -> () {
    debug k => _1;                       // in scope 0 at src/main.rs:7:14: 7:15
    let mut _0: ();                      // return place in scope 0 at src/main.rs:7:21: 7:21
    let mut _2: Tri;                     // in scope 0 at src/main.rs:8:9: 8:14
    let mut _4: &i32;                    // in scope 0 at src/main.rs:9:14: 9:16
    let mut _5: i32;                     // in scope 0 at src/main.rs:10:14: 10:17
    let mut _6: i32;                     // in scope 0 at src/main.rs:11:14: 11:17
    scope 1 {
        debug s => _2;                   // in scope 1 at src/main.rs:8:9: 8:14
        let _3: (&i32, i32, i32);        // in scope 1 at src/main.rs:9:9: 9:10
        scope 2 {
            debug t => _3;               // in scope 2 at src/main.rs:9:9: 9:10
        }
    }

    bb0: {
        _2 = mk() -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:8:17: 8:21
    }

    bb1: {
        _4 = &_1;                        // scope 1 at src/main.rs:9:14: 9:16
        _5 = copy (_2.0: i32);           // scope 1 at src/main.rs:10:14: 10:17
        _6 = copy (_2.1: i32);           // scope 1 at src/main.rs:11:14: 11:17
        _3 = (move _4, move _5, move _6); // scope 1 at src/main.rs:9:13: 11:18
        return;                          // scope 0 at src/main.rs:13:2: 13:2
    }
}
"""
TUPLE_SOURCE = "\n" * 6 + (
    "fn ref_first(k: i32) {\n"
    "    let mut s = mk(); s.c += 1;\n"
    "    let t = (&k, // REF_1\n"
    "             s.a, // REF_2\n"
    "             s.b); // REF_3\n"
    "    println!();\n"
    "}\n"
)


def test_a_breakpoints_place_on_its_line_is_told_by_its_column_or_not_at_all():
    # Review of #105 (round 3): in `let t = (&k,` / `s.a,` / `s.b);`, the line's first
    # place by the IR's order, `_4 = &_1`, emits no code: its first code is the tuple's,
    # after `s.a` is read. Only the stop's column tells them apart; without it, the
    # change was judged before the read (allowed) and the program used the old value.
    function = IrFunction(
        "ref_first", "app", "src/main.rs", {"start": {8}, "bb1": {9, 10, 11, 13}}, {}, 7
    )
    owned = OwnedLocals(
        [parse_body(TUPLE_FIRST)],
        {"h0": function},
        ("src/main.rs",),
        texts={"src/main.rs": TUPLE_SOURCE},
    )

    def judged(column):
        return owned.copied_in_progress(
            "h0", "s", 9, "/w/src/main.rs", breakpoint=True, column=column
        )

    assert judged(13) is True  # the tuple's column: `s.a` is in `_5`
    assert judged(14) is False  # `&k`'s: nothing of `s` read yet
    assert judged(None) is None


# #300 (eleventh evaluation, `rota`): `if let Some(l) = &last {` / `if ptr::eq(e, l) {`,
# as rustc 1.96 builds it at -Zmir-opt-level=0, cut to what line 39 needs. `e` points
# into `team`, `l` into `last`; `week` and `pick` are read by neither.
RAW_EQ = """// MIR for `plan` after PreCodegen

fn plan(_1: &mut [Engineer], _2: u32) -> () {
    debug team => _1;                    // in scope 0 at src/main.rs:28:9: 28:13
    debug weeks => _2;                   // in scope 0 at src/main.rs:28:32: 28:37
    let mut _0: ();                      // return place in scope 0 at src/main.rs:28:40: 28:40
    let mut _6: &std::option::Option<Engineer>; // in scope 0 at src/main.rs:38:30: 38:35
    let mut _7: isize;                   // in scope 0 at src/main.rs:38:20: 38:27
    let mut _9: bool;                    // in scope 0 at src/main.rs:39:20: 39:33
    let mut _10: *const Engineer;        // in scope 0 at src/main.rs:39:28: 39:29
    let mut _11: *const Engineer;        // in scope 0 at src/main.rs:39:31: 39:32
    let mut _14: usize;                  // in scope 0 at src/main.rs:43:16: 43:20
    let mut _15: u32;                    // in scope 0 at src/main.rs:44:16: 44:20
    scope 1 {
        debug last => _3;                // in scope 1 at src/main.rs:31:9: 31:17
        let mut _3: std::option::Option<Engineer>; // in scope 1 at src/main.rs:31:9: 31:17
        debug week => _4;                // in scope 1 at src/main.rs:32:9: 32:13
        let _4: u32;                     // in scope 1 at src/main.rs:32:9: 32:13
        debug pick => _12;               // in scope 1 at src/main.rs:33:13: 33:21
        let mut _12: usize;              // in scope 1 at src/main.rs:33:13: 33:21
        debug e => _13;                  // in scope 1 at src/main.rs:34:17: 34:18
        let _13: &Engineer;              // in scope 1 at src/main.rs:34:17: 34:18
        scope 2 {
            debug l => _8;               // in scope 2 at src/main.rs:38:25: 38:26
            let _8: &Engineer;           // in scope 2 at src/main.rs:38:25: 38:26
        }
    }

    bb0: {
        _3 = Option::<Engineer>::None;   // scope 0 at src/main.rs:31:38: 31:42
        _4 = copy _2;                    // scope 1 at src/main.rs:32:9: 32:13
        _12 = const usize::MAX;          // scope 1 at src/main.rs:33:24: 33:34
        _13 = first(copy _1) -> [return: bb1, unwind continue]; // scope 1 at src/main.rs:34:23: 34:34
    }

    bb1: {
        _6 = &_3;                        // scope 1 at src/main.rs:38:30: 38:35
        _7 = discriminant((*_6));        // scope 1 at src/main.rs:38:30: 38:35
        switchInt(move _7) -> [1: bb2, otherwise: bb4]; // scope 1 at src/main.rs:38:20: 38:27
    }

    bb2: {
        _8 = &(((*_6) as Some).0: Engineer); // scope 2 at src/main.rs:38:25: 38:26
        _10 = &raw const (*_13);         // scope 2 at src/main.rs:39:28: 39:29
        _11 = &raw const (*_8);          // scope 2 at src/main.rs:39:31: 39:32
        _9 = std::ptr::eq::<Engineer>(move _10, move _11) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:39:20: 39:33
    }

    bb3: {
        switchInt(move _9) -> [0: bb4, otherwise: bb5]; // scope 2 at src/main.rs:39:20: 39:33
    }

    bb4: {
        _14 = copy _12;                  // scope 1 at src/main.rs:43:16: 43:20
        _15 = copy _4;                   // scope 1 at src/main.rs:44:16: 44:20
        goto -> bb5;                     // scope 1 at src/main.rs:43:13: 45:14
    }

    bb5: {
        return;                          // scope 0 at src/main.rs:47:2: 47:2
    }
}
"""
# Its source, as far as the proofs read it: line 39 is `if ptr::eq(e, l) {`.
RAW_EQ_SOURCE = "\n".join(
    [
        "//! An on-call rota.",
        "use std::ptr;",
        "",
        "struct Engineer {",
        "    name: String,",
        "    shifts: u32,",
        "}",
        *[""] * 20,
        "fn plan(team: &mut [Engineer], weeks: u32) {",
        "",
        "",
        "    let mut last: Option<Engineer> = None;",
        "    let week = weeks;",
        "    let mut pick = usize::MAX;",
        "    let e = first(team);",
        "",
        "",
        "",
        "    if let Some(l) = &last {",
        "        if ptr::eq(e, l) {",
        "",
        "",
        "",
        "        let _ = pick;",
        "        let _ = week;",
        "        }",
        "    }",
        "}",
        "",
    ]
)
# The crate's own `std::ptr::eq`, which the dump names as the standard library's.
SHADOWED_SOURCE = RAW_EQ_SOURCE.replace(
    "use std::ptr;",
    "mod std { pub mod ptr { pub fn eq<T>(_: *const T, _: *const T) -> bool { true } } }",
)
HASH = "h0123456789abcdef"
RAW_EQ_BLOCKS = {
    "start": {31, 32, 33, 34},
    "bb1": {38},
    "bb2": {38, 39},
    "bb3": {39},
    "bb4": {43, 44},
    "bb5": {47},
}
RAW_EQ_CALL = (
    "        _9 = std::ptr::eq::<Engineer>(move _10, move _11) -> [return: bb3, unwind continue];"
)
ALL_NAMES = ("week", "pick", "last", "e", "l", "team")


def raw_eq_owned(text=RAW_EQ, source=RAW_EQ_SOURCE, **blocks):
    """OwnedLocals over ``text`` (with ``blocks``, the IR's other blocks) and ``source``.
    Its proofs judge every place of a line: LLVM inlines `ptr::eq` at -O0, and a line
    breakpoint on line 39 stops inside it, which the MIR places past the call."""
    function = IrFunction("plan", "rota", "src/main.rs", {**RAW_EQ_BLOCKS, **blocks}, {}, 28)
    texts = {"src/main.rs": source} if source is not None else {}
    return OwnedLocals([parse_body(text)], {HASH: function}, ("src/main.rs",), texts=texts)


def raw_eq_variant(call, *changes):
    """RAW_EQ with line 39's call ``call``, and each (old, new) of ``changes`` made."""
    assert RAW_EQ.count(RAW_EQ_CALL) == 1
    text = RAW_EQ.replace(RAW_EQ_CALL, f"        _9 = {call} -> [return: bb3, unwind continue];")
    for old, new in changes:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


def raw_eq_session(owned, projection=()):
    """A RustSession stopped on line 39 of RAW_EQ's `plan` (the frame's own hash), over
    ``owned``: the reasons `inspect` and a probe's refusal give (for a change to the part
    ``projection`` of a local)."""
    from types import SimpleNamespace

    from peeky.rust import RustSession

    judge = owned.copy_hazards
    owned.copy_hazards = lambda *args, **asked: judge(*args, **{**asked, "breakpoint": False})
    session = object.__new__(RustSession)
    session.owned = owned
    session.prepared = SimpleNamespace(source_root="/w")
    session._optimized = lambda: False
    session._stop_block = lambda hashed, frame: None
    session._projection = lambda *args: projection
    session._frame_scope = lambda frame, variable: None
    session._settable_names = lambda source, line: []
    frame = {"name": f"rota::plan::{HASH}", "line": 39, "column": 20}
    frame["source"] = {"path": "/w/src/main.rs"}
    return lambda name: session.mid_statement({"name": name}, frame, "breakpoint")


def withheld(owned, names=ALL_NAMES):
    return [owned.copied_in_progress(HASH, name, 39, "/w/src/main.rs") for name in names]


def test_the_standard_librarys_ptr_eq_reads_no_pointee_so_it_withholds_no_local():
    # #300: back from `ptr::eq(e, l)`, every local was refused, as if the call read it:
    # `&raw const (*e)` reads through `e`, which Peeky does not follow, so the pointer may
    # point anywhere. The standard library's `ptr::eq` compares the two addresses only.
    owned = raw_eq_owned()
    assert withheld(owned) == [False] * len(ALL_NAMES)
    # Whatever else the crate holds: the call is still the standard library's.
    owned = raw_eq_owned()
    owned.unsafe_code = "the crate has unsafe code (in src/main.rs)"
    assert withheld(owned) == [False] * len(ALL_NAMES)
    # A `std::ptr::eq` of the crate's own, or a source Peeky cannot read, may read them.
    for source in (SHADOWED_SOURCE, None):
        assert withheld(raw_eq_owned(source=source)) == [True] * len(ALL_NAMES)
    # `addr_eq` too, but `<*const T>::eq` and any other call are judged as before.
    owned = raw_eq_owned(
        raw_eq_variant("std::ptr::addr_eq::<Engineer, Engineer>(move _10, move _11)")
    )
    assert withheld(owned) == [False] * len(ALL_NAMES)
    owned = raw_eq_owned(raw_eq_variant("<*const Engineer as PartialEq>::eq(move _10, move _11)"))
    assert withheld(owned) == [True] * len(ALL_NAMES)


def test_a_call_given_a_raw_pointer_may_read_through_it_whatever_the_source_shows():
    # Review of #300: whether the crate has unsafe code was read from its source, but a
    # standard-library macro brings its own (`#[allow_internal_unsafe]`) with no `unsafe`
    # in the source. Here one expands on line 39 to `ptr::read` of a pointer to `week`:
    # `week` stays refused, though the crate shows no unsafe code.
    macro = "/rustc/0123456789abcdef/library/core/src/macros/mod.rs:60"
    text = raw_eq_variant(
        "core::ptr::read::<u32>(move _10)",
        ("        _10 = &raw const (*_13);  ", "        _10 = &raw const _4;      "),
        ("// scope 2 at src/main.rs:39:28: 39:29", f"// scope 2 at {macro}:9: 60:30"),
        (
            "(move _10) -> [return: bb3, unwind continue]; // scope 2 at src/main.rs:39:20: 39:33",
            f"(move _10) -> [return: bb3, unwind continue]; // scope 2 at {macro}:9: 60:40",
        ),
    )
    source = RAW_EQ_SOURCE.replace("        if ptr::eq(e, l) {", "        if peek!(week) == 0 {")
    owned = raw_eq_owned(text, source)
    owned.unsafe_code = None
    assert withheld(owned, ["week", "pick"]) == [True, False]
    why = raw_eq_session(owned)("week")
    assert why == (
        "a statement under way at the stop uses a pointer that may point into it (at "
        "core/src/macros/mod.rs:60), so it may hold a copy of it, which it uses after the stop"
    )
    assert why.row == "a statement under way may hold a copy (core/src/macros/mod.rs:60)"


UNSAFE_CODE = "the crate has unsafe code (in src/main.rs)"


def test_a_refusal_says_a_call_given_a_pointer_into_the_local_may_have_read_it():
    # Review of #300: a call given a pointer into the local, or a reference Peeky does not
    # follow, was said to have "already read it into a temporary". Here `std::ptr::eq` is
    # the crate's own, so it may read what it is given; and the crate has unsafe code,
    # which changes nothing.
    owned = raw_eq_owned(source=SHADOWED_SOURCE)
    owned.unsafe_code = UNSAFE_CODE
    why = raw_eq_session(owned)
    # `last`: `l` points into it, and the call is given a pointer made from `l`.
    assert why("last") == (
        "a statement under way at the stop uses a pointer that may point into it (at line 39), so it "
        "may hold a copy of it, which it uses after the stop"
    )
    assert why("last").row == "a statement under way may hold a copy (line 39)"
    assert why("e") == why("last")
    # `week`: only through `e` and `l`, which Peeky does not follow.
    assert why("week") == (
        "a statement under way at the stop uses `e` and `l` (at line 39), references Peeky "
        "can't trace that may point at it, so it may hold a copy of it, which it uses after "
        "the stop"
    )


def test_a_refusal_says_the_statement_read_it_only_where_every_place_it_can_stop_did():
    # Review of #300: the places a stop on line 39 can be were joined, so the call's copy
    # of `e`, made past the call, was said to be read at the places before it too.
    owned = raw_eq_owned(raw_eq_variant("same(copy _13, copy _8)"))
    why = raw_eq_session(owned)
    assert why("e") == (
        "a statement under way at the stop reads it into a temporary (at line 39), so it may "
        "hold a copy of it, which it uses after the stop"
    )
    # Where the stop can only be past the call, a crate function given a copy of `e` has
    # read it.
    owned = raw_eq_owned(raw_eq_variant("same(copy _13, copy _8)"))
    owned._positions = lambda site, line, facts, block=None: [(3, 0)]
    why = raw_eq_session(owned)
    assert why("e") == (
        "a statement under way at the stop has already read it into a temporary (at line "
        "39), which it uses after the stop"
    )
    assert why("e").row == "a statement under way read it (line 39)"
    assert why("l").startswith("a statement under way at the stop has already read it")
    assert why("week").startswith("a statement under way at the stop uses `e` and `l` (at")


def test_a_refusal_names_each_variable_it_cannot_trace_by_what_it_is():
    # Review of #300: every name was called "a reference", raw pointers too.
    named = (
        "            let _8: &Engineer;           // in scope 2 at src/main.rs:38:25: 38:26\n",
        "            let _8: &Engineer;           // in scope 2 at src/main.rs:38:25: 38:26\n"
        "            debug p => _10;              // in scope 2 at src/main.rs:39:13: 39:14\n"
        "            debug q => _11;              // in scope 2 at src/main.rs:39:16: 39:17\n",
    )

    def why(call):
        owned = raw_eq_owned(raw_eq_variant(call, named))
        owned.unsafe_code = UNSAFE_CODE
        return raw_eq_session(owned)("week")

    assert why("same(copy _10, copy _11)").startswith(
        "a statement under way at the stop uses `p` and `q` (at line 39), raw pointers Peeky "
        "can't trace that may point at it,"
    )
    assert why("same(copy _13, copy _11)").startswith(
        "a statement under way at the stop uses `e` and `q` (at line 39), values holding "
        "pointers Peeky can't trace that may point at it,"
    )
    owned = raw_eq_owned(raw_eq_variant("same(copy _10, copy _8)", named))
    held = owned.copy_hazards(HASH, "week", 39, "/w/src/main.rs")[-1][1]
    assert owned.untraced_reads(HASH, held) == (
        {("src/main.rs", 39)},
        [("l", "reference"), ("p", "raw pointer")],
    )


def test_a_refusal_says_it_read_the_local_only_where_every_place_the_stop_can_be_did():
    # Review of #300: the places a stop can be were joined, so one with a read the MIR
    # shows made the claim for all, though another held only what may have been read.
    from types import SimpleNamespace

    from peeky.rust import _copy_reason

    proofs = SimpleNamespace(
        copy_lines=lambda hashed, held, named_only=False: {("src/main.rs", min(held))},
        copy_names=lambda hashed, held: ["n"],
        # `_5` is computed from the binding `_20`.
        copy_sources=lambda hashed, held, among: frozenset({20}) & among if held else held,
        untraced_reads=lambda hashed, held: ({("src/main.rs", 9)}, [("e", "reference")]),
    )
    a, b = (1, 0), (2, 0)

    def reason(any_, explicit=(), value=None, traced=None, **others):
        found = {"any": any_, "explicit": explicit, "value": value, "traced": traced}
        found |= {"taken": [], "sources": [], "copied": [], "bindings": [], "whole": value}
        found |= others
        return _copy_reason(proofs, "h", found, "/w/src/main.rs")[:2]

    assert reason([(a, {5}), (b, {6})], [(a, {5}), (b, {6})]) == ("read", None)
    # Where a reading is not known, no more than that it may have read it.
    assert reason([(a, {5}), (b, {6})], [(a, {5}), (b, set())]) == (
        "may",
        "may have read it (at line 5)",
    )
    sure_at_a = [(a, {5}), (b, set())]
    assert reason([(a, {5}), (b, {7})], sure_at_a, sure_at_a, sure_at_a) == (
        "may",
        "reads it into a temporary (at line 5) or uses `e` (at line 9), a reference Peeky "
        "can't trace that may point at it",
    )
    assert reason([(a, {5}), (b, set())], sure_at_a, sure_at_a, sure_at_a)[1] == (
        "reads it into a temporary (at line 5)"
    )
    # A read only on some path to the place: what may hold a copy, by value.
    assert reason([(a, {6})], [(a, set())], [(a, {6})], [(a, {6})])[1] == (
        "reads it into a temporary (at line 6)"
    )
    none = [(a, set()), (b, set())]
    assert reason([(a, {7}), (b, {7})], none, none, [(a, {7}), (b, {7})]) == (
        "may",
        "uses a pointer that may point into it (at line 7)",
    )
    # The program's own binding `_20` (`n`): named where every place holds what it, or a
    # temporary computed from it (`_5`, `let z = n + 1;`), surely took.
    bindings = [(a, {20}), (b, {20})]
    for taken in ([(a, {20}), (b, {20})], [(a, {20}), (b, {5})], [(a, {5}), (b, {5})]):
        sure = {"taken": taken, "sources": bindings, "copied": taken, "bindings": bindings}
        assert reason(taken, none, none, none, **sure) == ("named", ["n"]), taken
        # Where Peeky cannot tell which binding gave it, it says that it may have.
        sure["sources"] = []
        assert reason(taken, none, none, none, **sure)[0] == "may", taken
    # Nor read: a temporary computed from the binding's copy.
    copied = [(a, {5}), (b, set())]
    assert reason(copied, none, copied, copied, copied=copied, bindings=bindings) == (
        "may",
        "uses `n` (at line 20), which may hold the program's own copy of it",
    )
    assert reason(copied, none, copied, copied, copied=copied) == (
        "may",
        "uses what may be the program's own copy of it (at line 5)",
    )
    named_at_a = [(a, {20}), (b, set())]
    sure = {"taken": named_at_a, "sources": named_at_a, "copied": named_at_a}
    assert reason([(a, {20}), (b, {7})], none, none, none, **sure) == (
        "may",
        "uses `n` (at line 20), which may hold the program's own copy of it or uses `e` (at "
        "line 9), a reference Peeky can't trace that may point at it",
    )


def line_39(*statements):
    """A change to RAW_EQ (``raw_eq_variant``): ``statements`` on line 39 for its first
    (a block's end and the next's start, `}` first, as they are)."""
    comment = "// scope 2 at src/main.rs:39:28: 39:29"
    shown = "".join(
        f"    {each}\n" if each.startswith("}") else f"        {each}  {comment}\n"
        for each in statements[:-1]
    )
    return ("        _10 = &raw const (*_13);  ", f"{shown}        {statements[-1]}  ")


def test_a_refusal_states_a_read_only_where_every_path_to_the_stop_makes_it():
    # Review round 2 of #300: what surely read the local was taken from what may have
    # (joined over the paths, through an aggregate holding pointers to other locals too,
    # whatever part a change makes), so "has already read it" could be false.
    def why(call, *changes, name="week", projection=(), **blocks):
        owned = raw_eq_owned(raw_eq_variant(call, *changes), **blocks)
        owned._positions = lambda site, line, facts, block=None: [(3, 0)]
        return raw_eq_session(owned, projection)(name)

    sure = "a statement under way at the stop has already read it into a temporary (at line 39)"
    maybe = ", so it may hold a copy of it, which it uses after the stop"
    # `same(week)`: read by value on every path.
    assert why("same(copy _4)").startswith(sure)
    # `let r = &week; same(*r)`: through a pointer made from it.
    assert why("same(move _17)", line_39("_16 = &_4;", "_17 = copy (*_16);")).startswith(sure)
    # `let pair = (&week, &pick); same(*pair.1)` reads only `pick`.
    pair = line_39(
        "_16 = &_4;",
        "_17 = &_12;",
        "_18 = (move _16, move _17);",
        "_19 = copy (*(_18.1: &usize));",
    )
    assert why("same(copy _19)", pair) == (
        "a statement under way at the stop uses a pointer that may point into it (at line 39)"
        + maybe
    )
    # `same(if c { week } else { pick })`: read on one path only.
    branch = line_39(
        "switchInt(copy _2) -> [0: bb6, otherwise: bb7];",
        "}\n\n    bb6: {",
        "_17 = copy _4;",
        "goto -> bb8;",
        "}\n\n    bb7: {",
        "_17 = copy _12;",
        "goto -> bb8;",
        "}\n\n    bb8: {",
        "_10 = &raw const (*_13);",
    )
    assert why("same(copy _17)", branch, bb6={39}, bb7={39}, bb8={39}) == (
        "a statement under way at the stop reads it into a temporary (at line 39)" + maybe
    )
    # `tt: (u32, u32)`, `same(tt.1)`: a change to `tt.0` is not what it read.
    pair_local = (
        "        debug pick => _12;  ",
        "        debug tt => _16;                 // in scope 1 at src/main.rs:33:9: 33:11\n"
        "        debug pick => _12;  ",
    )
    field = line_39("_17 = copy (_16.1: u32);", "_10 = &raw const (*_13);")
    whole = why("same(copy _17)", pair_local, field, name="tt")
    assert whole.startswith(sure), whole
    part = why("same(copy _17)", pair_local, field, name="tt", projection=(("field", 0),))
    assert part == (
        "a statement under way at the stop reads another part of it into a temporary (at "
        "line 39)" + maybe
    )
    # `let week = weeks;` on line 32, then `weeks = week + 1 …`: the program goes on with
    # its own copy, on every path or on one.
    assign = (
        "    bb3: {\n",
        "    bb3: {\n        _2 = move _17;  // scope 2 at src/main.rs:39:20: 39:21\n",
    )
    plus = line_39("_17 = Add(copy _4, const 1_u32);", "_10 = &raw const (*_13);")
    assert why("same(const 0_u32)", plus, assign, name="weeks") == (
        "the program has already copied its value into `week` (at line 32) and goes on with "
        "that copy, so a change to `weeks` may never be seen: Peeky proves no read of the "
        "changed part first (a reference lent to a call, or `{:?}`, is not one)"
    )
    plus_or_zero = line_39(
        "switchInt(copy _12) -> [0: bb6, otherwise: bb7];",
        "}\n\n    bb6: {",
        "_17 = Add(copy _4, const 1_u32);",
        "goto -> bb8;",
        "}\n\n    bb7: {",
        "_17 = const 0_u32;",
        "goto -> bb8;",
        "}\n\n    bb8: {",
        "_10 = &raw const (*_13);",
    )
    found = why(
        "same(const 0_u32)", plus_or_zero, assign, name="weeks", bb6={39}, bb7={39}, bb8={39}
    )
    assert found == (
        "a statement under way at the stop uses `week` (at line 32), which may hold the "
        "program's own copy of it" + maybe
    )
    # `if c { pick = weeks; }` on one path only: `pick` is no copy it surely uses.
    branch_pick = line_39(
        "switchInt(copy _9) -> [0: bb6, otherwise: bb7];",
        "}\n\n    bb6: {",
        "_12 = copy _2;",
        "goto -> bb8;",
        "}\n\n    bb7: {",
        "goto -> bb8;",
        "}\n\n    bb8: {",
        "_17 = Add(copy _4, copy _12);",
        "_10 = &raw const (*_13);",
    )
    blocks = {"bb6": {39}, "bb7": {39}, "bb8": {39}}
    found = why("same(const 0_u32)", branch_pick, assign, name="weeks", **blocks)
    assert found.startswith(
        "the program has already copied its value into `week` (at line 32) and goes on"
    ), found
    # `week = 0` after `week + 1` was computed: still the copy it was computed from, and
    # where `week` took it.
    reset = line_39(
        "_17 = Add(copy _4, const 1_u32);", "_4 = const 0_u32;", "_10 = &raw const (*_13);"
    )
    found = why("same(const 0_u32)", reset, assign, name="weeks")
    assert found.startswith(
        "the program has already copied its value into `week` (at line 32) and goes on"
    ), found
    # On one path only, and `week` reset since: what may be the program's copy.
    branch_reset = line_39(
        "switchInt(copy _12) -> [0: bb6, otherwise: bb7];",
        "}\n\n    bb6: {",
        "_17 = Add(copy _4, const 1_u32);",
        "goto -> bb8;",
        "}\n\n    bb7: {",
        "_17 = const 0_u32;",
        "goto -> bb8;",
        "}\n\n    bb8: {",
        "_4 = const 0_u32;",
        "_10 = &raw const (*_13);",
    )
    found = why("same(const 0_u32)", branch_reset, assign, name="weeks", **blocks)
    assert found == (
        "a statement under way at the stop uses what may be the program's own copy of it (at "
        "line 39)" + maybe
    )
    # `let week = weeks;` on line 32, then `same(week, e.shifts)`: the statement reads the
    # program's own copy, not `weeks`; only `e` may have reached it.
    shifts = line_39("_17 = copy ((*_13).1: u32);", "_10 = &raw const (*_13);")
    assert why("same(copy _4, move _17)", shifts, name="weeks") == (
        "a statement under way at the stop uses `e` (at line 39), a reference Peeky can't "
        "trace that may point at it" + maybe
    )


def test_format_args_constructors_count_only_where_nothing_else_can_pass_for_them():
    # Review of #105 (round 3): the crate's own `mod core { pub mod fmt { ... } }` prints
    # its `core::fmt::Arguments::new(&s.a)` just as the standard library's.
    def formatting(text, path="main"):
        body = mir({0: ["return;"]})
        body.path = path
        return OwnedLocals([body], {}, ("src/main.rs",), texts={"src/main.rs": text})._formatting()

    assert formatting('use std::fmt;\nfn main() { println!("{}", core::mem::size_of::<u8>()); }')
    assert formatting('// mod core {}\nfn main() { let s = "std"; }')
    assert not formatting("mod core { pub mod fmt {} }\nfn main() {}")
    assert not formatting("use crate::inner as std;\nfn main() {}")
    assert not formatting("fn main() {}", path="core::fmt::<impl at src/main.rs:8:5: 8:28>::new")
    assert not formatting('fn main() { let s = "unterminated; }')
    assert not OwnedLocals([], {}, ("src/main.rs",))._formatting()


def test_a_line_breakpoint_stops_before_its_lines_first_code_in_each_block():
    from peeky.rust_mir import _first_places

    class Body:
        blocks = {1: type("B", (), {"scopes": [3, 3, 3]})(), 2: type("B", (), {"scopes": [5]})()}

        @staticmethod
        def lexical(scope):
            return scope

        @staticmethod
        def _address_order(position, function):
            return None if function == "unknown" else (0, *position)

    places = [(1, 1), (1, 2), (2, 0)]
    assert _first_places(Body, "ir", places) == [(1, 1), (2, 0)]
    # A block whose places' order is not known keeps them all.
    assert _first_places(Body, "unknown", places) == [(1, 1), (1, 2), (2, 0)]


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


def test_a_places_assigned_state_is_its_blocks_entry_and_each_locals_last_event():
    # #132's dataflow takes a block at a time (each block's events read once): every
    # place of a body reads as replaying its block statement by statement does.
    import re

    begins = re.compile(r"^(?:StorageLive|Deinit)\(_([0-9]+)\);$")
    assigns = re.compile(r"^_([0-9]+) = ")

    def replayed(body, resumed):
        # The flow statement by statement, to its fixpoint.
        def flows(number):
            return () if not resumed and number in body.resumes else body._flows(number)

        entry, work = {0: sum(1 << n for n in body.arguments)}, [0]
        kept = sum(1 << n for n in body.saved | body.arguments)
        while work:
            number = work.pop()
            block, state = body.blocks[number], entry[number]
            for text, _, _ in block.statements[:-1]:
                if found := begins.match(text):
                    state &= ~(1 << int(found[1]))
                elif found := assigns.match(text):
                    state |= 1 << int(found[1])
            for target in flows(number):
                out = state & kept if number in body.resumes else state
                if target == block.returns:
                    out |= 1 << int(assigns.match(block.terminator)[1])
                merged = out if target not in entry else entry[target] & out
                if entry.get(target) != merged:
                    entry[target] = merged
                    work.append(target)
        return entry

    for body in (parse_body(SCALARS), parse_body(PRINTED), parse_body(COROUTINE)):
        locals_ = range(1 + max(body.types, default=0))
        for resumed in (True, False):
            entry = body._assigned_flow(resumed)
            assert entry == replayed(body, resumed)
            for number, state in entry.items():
                statements = body.blocks[number].statements
                for index in range(len(statements)):
                    for local in locals_:
                        assert body.unassigned_at(local, (number, index), resumed) == (
                            not state >> local & 1
                        ), (number, index, local)
                    text = statements[index][0]
                    if index < len(statements) - 1:
                        if found := begins.match(text):
                            state &= ~(1 << int(found[1]))
                        elif found := assigns.match(text):
                            state |= 1 << int(found[1])


def test_a_stops_places_are_kept_apart_by_its_kind_even_without_a_column(tmp_path):
    # #132's per-stop memo: a line breakpoint's places (its facts, here without a column)
    # are fewer than a step's on the same line, whichever stop is judged first.
    owned = OwnedLocals(
        [parse_body(PRINTED)],
        {"h": printed_function(tmp_path)},
        SOURCES,
        texts={"src/main.rs": PRINTED_TEXT},
    )
    site = owned._site("h", 17, MAIN)
    facts = owned._facts(site[1], 17, None)
    assert facts.column is None
    for _ in range(2):
        assert owned._positions(site, 17, None) == [(0, 3), (0, 4), (1, 0), (1, 1)]
        assert owned._positions(site, 17, facts) == [(0, 3), (0, 4)]


def test_what_is_live_ahead_of_a_place_is_what_used_ahead_finds_for_each_local():
    # #105's copies are judged at once, by a backward dataflow (``live_ahead``): every
    # local at every place of a body reads as ``used_ahead``'s search from there does.
    bodies = [OWNERS, CONTINUED, INLINED, COROUTINE, SCALARS, PRINTED, TUPLE_FIRST]
    for text in bodies:
        body = parse_body(text)
        locals_ = range(2 + max(body.types, default=0))
        for number, block in body.blocks.items():
            for index in range(len(block.statements) + 1):
                live = body.live_ahead((number, index))
                for local in locals_:
                    used = body.used_ahead(local, (number, index))
                    assert bool(live >> local & 1) == used, (number, index, local)
    assert parse_body(OWNERS).live_ahead((99, 0)) == 0


def test_a_locals_copies_are_what_passes_over_every_assignment_find():
    # #105's copies follow only what mentions each temporary found (``copies_of``): they
    # are what passes over every assignment to a temporary, until none changes, find.
    import re

    from peeky.rust_mir import (
        _BORROWING,
        _MENTIONED,
        _NOT_A_CALL,
        _PART_ASSIGNMENT,
        _STRING,
        _WHOLE_ASSIGNMENT,
    )

    def passes(body, local):
        named = {b[0] for bs in body.bindings.values() for b in bs if b[0] is not None}
        found = []
        for block in body.blocks.values():
            for text, _, _ in block.statements:
                text = _STRING.sub('""', text)
                part = _WHOLE_ASSIGNMENT.match(text) or (
                    None if re.match(r"\(+\*_", text) else _PART_ASSIGNMENT.match(text)
                )
                if part and int(part[1]) not in named and int(part[1]) not in (0, local):
                    mentioned = {int(n) for n in _MENTIONED.findall(part[2])}
                    found.append((int(part[1]), part[2], mentioned))
        holds, points, changed = set(), set(), True
        while changed:
            changed = False
            for target, value, mentioned in found:
                through = bool(re.search(r"\(\*[(_]", value))
                pointing = any(
                    n in points
                    or (n in named and n != local and _BORROWING.search(body.types.get(n, "&")))
                    for n in mentioned
                )
                if value.startswith("&"):
                    hold, point = bool(mentioned & holds), local in mentioned or through or pointing
                else:
                    call = "->" in value and not _NOT_A_CALL.match(value)
                    hold = local in mentioned or through or bool(mentioned & holds)
                    hold, point = hold or (call and pointing), pointing
                for flag, into in ((hold, holds), (point, points)):
                    if flag and target not in into:
                        into.add(target)
                        changed = True
        return frozenset(holds)

    for text in (OWNERS, CONTINUED, INLINED, COROUTINE, SCALARS, PRINTED, TUPLE_FIRST):
        body = parse_body(text)
        for local in range(2 + max(body.types, default=0)):
            assert body.copies_of(local, formatting=False) == passes(body, local), local


@pytest.mark.parametrize(
    "kind, free",
    [
        ("usize", True),
        ("std::string::String", True),
        ("(i32, std::string::String)", True),
        ("[u8; 4]", True),
        ("std::vec::Vec<std::option::Option<u64>>", True),
        ("&str", False),
        ("*const u8", False),
        ("std::str::Chars<'_>", False),
        # The MIR prints a trait object's lifetime nowhere (#164 review 2).
        ("std::boxed::Box<dyn std::ops::Fn() -> usize>", False),
        ("std::pin::Pin<std::boxed::Box<dyn std::future::Future<Output = usize>>>", False),
        ("impl std::iter::Iterator<Item = char>", False),
        ("{closure@src/main.rs:3:9: 3:11}", False),
        ("main::Count", False),
        ("T", False),
        (None, False),
    ],
)
def test_only_types_that_provably_hold_no_pointer_are_free(kind, free):
    from peeky.rust_mir import _pointer_free

    assert _pointer_free(kind, ("T",)) is free
    assert _pointer_free("usize", None) is False


@pytest.mark.parametrize(
    "kind, quiet",
    [
        ("&str", True),
        ("std::vec::Vec<&str>", True),
        ("std::option::Option<&str>", True),
        ("std::str::Chars<'_>", True),
        ("std::collections::HashMap<&str, i32>", True),
        # A drop that may read what it borrows (#164 review 3).
        ("Guard<'_>", False),
        ("std::boxed::Box<dyn std::ops::Fn() -> usize>", False),
        ("std::string::Drain<'_>", False),
        ("{closure@src/main.rs:3:9: 3:11}", False),
        ("T", False),
        (None, False),
    ],
)
def test_only_drops_that_cannot_read_a_borrow_are_quiet(kind, quiet):
    from peeky.rust_mir import _quiet_drop

    assert _quiet_drop(kind, ("T",)) is quiet


def test_crate_types_whose_fields_hold_no_pointer_are_free():
    # #164 review 3: `Vec<Span>` with `Span { start: u32, end: u32 }` holds no borrow.
    # Review 4: nor a `Box` of such (rustc describes it as a pointer named for it), a
    # C-like enum, a union of such, or a type that holds itself through a Box.
    from peeky.rust import free_types
    from peeky.rust_mir import _pointer_free

    def die(tag, name=None, kind=None, children=(), parent=None):
        attrs = {}
        if name:
            attrs["DW_AT_name"] = f'"{name}"'
        if kind is not None:
            attrs["DW_AT_type"] = f"0x{kind:08x}"
        return {"tag": tag, "attrs": attrs, "children": list(children), "parent": parent}

    def option(at, of, param):
        # core::option::Option<…>, its type argument on its `Some` variant's struct.
        return {
            at: die("DW_TAG_structure_type", f"Option<{of}>", children=[at + 1], parent=51),
            at + 1: die("DW_TAG_variant_part", children=[at + 2], parent=at),
            at + 2: die("DW_TAG_variant", children=[at + 3], parent=at + 1),
            at + 3: die("DW_TAG_member", "Some", kind=at + 4, parent=at + 2),
            at + 4: die("DW_TAG_structure_type", "Some", children=[at + 5, at + 6], parent=at),
            at + 5: die("DW_TAG_template_type_parameter", "T", kind=param, parent=at + 4),
            at + 6: die("DW_TAG_member", "__0", kind=param, parent=at + 4),
        }

    spans = [10, 20, 30, 40, 80, 90, 100, 110, 120, 130]
    dies = {
        1: die("DW_TAG_namespace", "spans", children=spans),
        2: die("DW_TAG_base_type", "u32"),
        3: die("DW_TAG_pointer_type", "*const u8", kind=2),
        4: die("DW_TAG_pointer_type", "alloc::boxed::Box<u32, alloc::alloc::Global>", kind=2),
        5: die("DW_TAG_pointer_type", "&u32", kind=2),
        6: die(
            "DW_TAG_pointer_type", "alloc::boxed::Box<spans::Rec, alloc::alloc::Global>", kind=90
        ),
        7: die(
            "DW_TAG_pointer_type", "alloc::boxed::Box<spans::Bad, alloc::alloc::Global>", kind=100
        ),
        10: die("DW_TAG_structure_type", "Span", children=[11, 12], parent=1),
        11: die("DW_TAG_member", "start", kind=2, parent=10),
        12: die("DW_TAG_member", "end", kind=2, parent=10),
        20: die("DW_TAG_structure_type", "Pointing", children=[21], parent=1),
        21: die("DW_TAG_member", "at", kind=3, parent=20),
        30: die("DW_TAG_structure_type", "Pair", children=[31], parent=1),
        31: die("DW_TAG_member", "inner", kind=10, parent=30),
        40: die("DW_TAG_structure_type", "Loop", children=[41], parent=1),
        41: die("DW_TAG_member", "again", kind=40, parent=40),
        50: die("DW_TAG_namespace", "core", children=[51]),
        51: die("DW_TAG_namespace", "option", children=[60, 70, 140], parent=50),
        **option(60, "alloc::boxed::Box<spans::Rec, alloc::alloc::Global>", 6),
        **option(70, "alloc::boxed::Box<spans::Bad, alloc::alloc::Global>", 7),
        **option(140, "&u32", 5),
        # struct Boxed { b: Box<u32> }
        80: die("DW_TAG_structure_type", "Boxed", children=[81], parent=1),
        81: die("DW_TAG_member", "b", kind=4, parent=80),
        # struct Rec { v: u32, next: Option<Box<Rec>> }
        90: die("DW_TAG_structure_type", "Rec", children=[91, 92], parent=1),
        91: die("DW_TAG_member", "v", kind=2, parent=90),
        92: die("DW_TAG_member", "next", kind=60, parent=90),
        # struct Bad { at: Option<&u32>, next: Option<Box<Bad>> }
        100: die("DW_TAG_structure_type", "Bad", children=[101, 102], parent=1),
        101: die("DW_TAG_member", "at", kind=140, parent=100),
        102: die("DW_TAG_member", "next", kind=70, parent=100),
        # enum CEnum { A, B }
        110: die("DW_TAG_enumeration_type", "CEnum", kind=2, parent=1),
        # union Bits { n: u32, s: Span }, union Raw { p: *const u8 }
        120: die("DW_TAG_union_type", "Bits", children=[121, 122], parent=1),
        121: die("DW_TAG_member", "n", kind=2, parent=120),
        122: die("DW_TAG_member", "s", kind=10, parent=120),
        130: die("DW_TAG_union_type", "Raw", children=[131], parent=1),
        131: die("DW_TAG_member", "p", kind=3, parent=130),
    }
    found = free_types(dies)
    free = {"Span", "Pair", "Loop", "Boxed", "Rec", "CEnum", "Bits"}
    assert free | {f"spans::{name}" for name in free} <= found, found
    assert not {"Pointing", "Bad", "Raw", "spans::Bad"} & found, found
    assert _pointer_free("std::vec::Vec<Span>", (), found) is True
    assert _pointer_free("std::vec::Vec<Span>", ()) is False
    assert _pointer_free("std::vec::Vec<Pointing>", (), found) is False
    assert _pointer_free("std::option::Option<Rec>", (), found) is True
    # An `Option` whose type argument is not found holds what it may.
    dies[64] = die("DW_TAG_structure_type", "Some", parent=60)
    assert "Rec" not in free_types(dies)


# `for v in args[0].split('/') { total = count(v.len()); total += 1; }` (#156).
FROZEN = """// MIR for `main` after PreCodegen

fn main() -> () {
    let mut _0: ();                      // return place in scope 0 at src/main.rs:5:10: 5:10
    let mut _1: !;                       // in scope 0 at src/main.rs:5:11: 13:2
    let _2: std::vec::Vec<std::string::String>; // in scope 0 at src/main.rs:6:9: 6:13
    let mut _3: std::env::Args;          // in scope 0 at src/main.rs:6:29: 6:45
    let _5: ();                          // in scope 0 at src/main.rs:8:5: 11:6
    let mut _6: std::str::Split<'_, char>; // in scope 0 at src/main.rs:8:14: 8:32
    let mut _7: std::str::Split<'_, char>; // in scope 0 at src/main.rs:8:14: 8:32
    let mut _8: &str;                    // in scope 0 at src/main.rs:8:14: 8:21
    let _9: &str;                        // in scope 0 at src/main.rs:8:14: 8:32
    let mut _10: &std::string::String;   // in scope 0 at src/main.rs:8:14: 8:21
    let _11: &std::string::String;       // in scope 0 at src/main.rs:8:18: 8:21
    let mut _12: &std::vec::Vec<std::string::String>; // in scope 0 at src/main.rs:8:14: 8:18
    let mut _14: ();                     // in scope 0 at src/main.rs:5:1: 13:2
    let _15: ();                         // in scope 0 at src/main.rs:8:14: 8:32
    let mut _16: std::option::Option<&str>; // in scope 0 at src/main.rs:8:14: 8:32
    let mut _17: &mut std::str::Split<'_, char>; // in scope 0 at src/main.rs:8:14: 8:32
    let mut _18: &mut std::str::Split<'_, char>; // in scope 0 at src/main.rs:8:14: 8:32
    let mut _19: isize;                  // in scope 0 at src/main.rs:8:5: 11:6
    let mut _20: !;                      // in scope 0 at src/main.rs:8:5: 11:6
    let mut _22: usize;                  // in scope 0 at src/main.rs:9:17: 9:31
    let mut _23: usize;                  // in scope 0 at src/main.rs:9:23: 9:30
    let mut _24: &str;                   // in scope 0 at src/main.rs:9:23: 9:24
    let mut _25: (usize, bool);          // in scope 0 at src/main.rs:10:9: 10:19
    let _26: !;                          // in scope 0 at src/main.rs:12:5: 12:37
    let mut _27: i32;                    // in scope 0 at src/main.rs:12:24: 12:36
    let mut _28: usize;                  // in scope 0 at src/main.rs:12:24: 12:29
    scope 1 {
        debug args => _2;                // in scope 1 at src/main.rs:6:9: 6:13
        let mut _4: usize;               // in scope 1 at src/main.rs:7:9: 7:18
        scope 2 {
            debug total => _4;           // in scope 2 at src/main.rs:7:9: 7:18
            let mut _13: std::str::Split<'_, char>; // in scope 2 at src/main.rs:8:14: 8:32
            scope 3 {
                debug iter => _13;       // in scope 3 at src/main.rs:8:14: 8:32
                let _21: &str;           // in scope 3 at src/main.rs:8:9: 8:10
                scope 4 {
                    debug v => _21;      // in scope 4 at src/main.rs:8:9: 8:10
                }
            }
        }
    }

    bb0: {
        StorageLive(_2);                 // scope 0 at src/main.rs:6:9: 6:13
        StorageLive(_3);                 // scope 0 at src/main.rs:6:29: 6:45
        _3 = args() -> [return: bb1, unwind continue]; // scope 0 at src/main.rs:6:29: 6:45
                                         // mir::ConstOperand
                                         // + span: src/main.rs:6:29: 6:43
                                         // + const_: Const { ty: fn() -> Args {args}, val: Value(args) }
    }

    bb1: {
        _2 = <Args as Iterator>::collect::<Vec<String>>(move _3) -> [return: bb2, unwind: bb16]; // scope 0 at src/main.rs:6:29: 6:55
                                         // mir::ConstOperand
                                         // + span: src/main.rs:6:46: 6:53
                                         // + const_: Const { ty: fn(Args) -> Vec<String> {<Args as Iterator>::collect::<Vec<String>>}, val: Value(<Args as Iterator>::collect::<Vec<String>>) }
    }

    bb2: {
        StorageDead(_3);                 // scope 0 at src/main.rs:6:54: 6:55
        StorageLive(_4);                 // scope 1 at src/main.rs:7:9: 7:18
        _4 = const 0_usize;              // scope 1 at src/main.rs:7:21: 7:22
        StorageLive(_5);                 // scope 2 at src/main.rs:8:5: 11:6
        StorageLive(_6);                 // scope 2 at src/main.rs:8:14: 8:32
        StorageLive(_7);                 // scope 2 at src/main.rs:8:14: 8:32
        StorageLive(_8);                 // scope 2 at src/main.rs:8:14: 8:21
        StorageLive(_9);                 // scope 2 at src/main.rs:8:14: 8:32
        StorageLive(_10);                // scope 2 at src/main.rs:8:14: 8:21
        StorageLive(_11);                // scope 2 at src/main.rs:8:18: 8:21
        StorageLive(_12);                // scope 2 at src/main.rs:8:14: 8:18
        _12 = &_2;                       // scope 2 at src/main.rs:8:14: 8:18
        _11 = <Vec<String> as Index<usize>>::index(move _12, const 0_usize) -> [return: bb3, unwind: bb15]; // scope 2 at src/main.rs:8:18: 8:21
                                         // mir::ConstOperand
                                         // + span: src/main.rs:8:18: 8:21
                                         // + const_: Const { ty: for<'a> fn(&'a Vec<String>, usize) -> &'a <Vec<String> as Index<usize>>::Output {<Vec<String> as Index<usize>>::index}, val: Value(<Vec<String> as Index<usize>>::index) }
    }

    bb3: {
        _10 = &(*_11);                   // scope 2 at src/main.rs:8:14: 8:21
        _9 = <String as Deref>::deref(move _10) -> [return: bb4, unwind: bb15]; // scope 2 at src/main.rs:8:14: 8:32
                                         // mir::ConstOperand
                                         // + span: src/main.rs:8:14: 8:32
                                         // + const_: Const { ty: for<'a> fn(&'a String) -> &'a <String as Deref>::Target {<String as Deref>::deref}, val: Value(<String as Deref>::deref) }
    }

    bb4: {
        _8 = &(*_9);                     // scope 2 at src/main.rs:8:14: 8:21
        StorageDead(_12);                // scope 2 at src/main.rs:8:20: 8:21
        StorageDead(_10);                // scope 2 at src/main.rs:8:20: 8:21
        _7 = core::str::<impl str>::split::<char>(move _8, const '/') -> [return: bb5, unwind: bb15]; // scope 2 at src/main.rs:8:14: 8:32
                                         // mir::ConstOperand
                                         // + span: src/main.rs:8:22: 8:27
                                         // + const_: Const { ty: for<'a> fn(&'a str, char) -> std::str::Split<'a, char> {core::str::<impl str>::split::<char>}, val: Value(core::str::<impl str>::split::<char>) }
    }

    bb5: {
        StorageDead(_8);                 // scope 2 at src/main.rs:8:31: 8:32
        _6 = <std::str::Split<'_, char> as IntoIterator>::into_iter(move _7) -> [return: bb6, unwind: bb15]; // scope 2 at src/main.rs:8:14: 8:32
                                         // mir::ConstOperand
                                         // + span: src/main.rs:8:14: 8:32
                                         // + const_: Const { ty: fn(std::str::Split<'_, char>) -> <std::str::Split<'_, char> as IntoIterator>::IntoIter {<std::str::Split<'_, char> as IntoIterator>::into_iter}, val: Value(<std::str::Split<'_, char> as IntoIterator>::into_iter) }
    }

    bb6: {
        StorageDead(_7);                 // scope 2 at src/main.rs:8:31: 8:32
        StorageLive(_13);                // scope 2 at src/main.rs:8:14: 8:32
        _13 = move _6;                   // scope 2 at src/main.rs:8:14: 8:32
        goto -> bb7;                     // scope 3 at src/main.rs:8:5: 11:6
    }

    bb7: {
        StorageLive(_15);                // scope 3 at src/main.rs:8:14: 8:32
        StorageLive(_16);                // scope 3 at src/main.rs:8:14: 8:32
        StorageLive(_17);                // scope 3 at src/main.rs:8:14: 8:32
        StorageLive(_18);                // scope 3 at src/main.rs:8:14: 8:32
        _18 = &mut _13;                  // scope 3 at src/main.rs:8:14: 8:32
        _17 = &mut (*_18);               // scope 3 at src/main.rs:8:14: 8:32
        _16 = <std::str::Split<'_, char> as Iterator>::next(move _17) -> [return: bb8, unwind: bb15]; // scope 3 at src/main.rs:8:14: 8:32
                                         // mir::ConstOperand
                                         // + span: src/main.rs:8:14: 8:32
                                         // + const_: Const { ty: for<'a> fn(&'a mut std::str::Split<'_, char>) -> Option<<std::str::Split<'_, char> as Iterator>::Item> {<std::str::Split<'_, char> as Iterator>::next}, val: Value(<std::str::Split<'_, char> as Iterator>::next) }
    }

    bb8: {
        StorageDead(_17);                // scope 3 at src/main.rs:8:31: 8:32
        _19 = discriminant(_16);         // scope 3 at src/main.rs:8:14: 8:32
        switchInt(move _19) -> [0: bb11, 1: bb10, otherwise: bb9]; // scope 3 at src/main.rs:8:14: 8:32
    }

    bb9: {
        unreachable;                     // scope 3 at src/main.rs:8:14: 8:32
    }

    bb10: {
        StorageLive(_21);                // scope 3 at src/main.rs:8:9: 8:10
        _21 = copy ((_16 as Some).0: &str); // scope 3 at src/main.rs:8:9: 8:10
        StorageLive(_22);                // scope 4 at src/main.rs:9:17: 9:31
        StorageLive(_23);                // scope 4 at src/main.rs:9:23: 9:30
        StorageLive(_24);                // scope 4 at src/main.rs:9:23: 9:24
        _24 = &(*_21);                   // scope 4 at src/main.rs:9:23: 9:24
        _23 = core::str::<impl str>::len(move _24) -> [return: bb12, unwind: bb15]; // scope 4 at src/main.rs:9:23: 9:30
                                         // mir::ConstOperand
                                         // + span: src/main.rs:9:25: 9:28
                                         // + const_: Const { ty: for<'a> fn(&'a str) -> usize {core::str::<impl str>::len}, val: Value(core::str::<impl str>::len) }
    }

    bb11: {
        _5 = const ();                   // scope 3 at src/main.rs:8:5: 11:6
        StorageDead(_18);                // scope 3 at src/main.rs:11:5: 11:6
        StorageDead(_16);                // scope 3 at src/main.rs:11:5: 11:6
        StorageDead(_15);                // scope 3 at src/main.rs:11:5: 11:6
        StorageDead(_13);                // scope 2 at src/main.rs:11:5: 11:6
        StorageDead(_11);                // scope 2 at src/main.rs:11:5: 11:6
        StorageDead(_9);                 // scope 2 at src/main.rs:11:5: 11:6
        StorageDead(_6);                 // scope 2 at src/main.rs:11:5: 11:6
        StorageDead(_5);                 // scope 2 at src/main.rs:11:5: 11:6
        StorageLive(_26);                // scope 2 at src/main.rs:12:5: 12:37
        StorageLive(_27);                // scope 2 at src/main.rs:12:24: 12:36
        StorageLive(_28);                // scope 2 at src/main.rs:12:24: 12:29
        _28 = copy _4;                   // scope 2 at src/main.rs:12:24: 12:29
        _27 = move _28 as i32 (IntToInt); // scope 2 at src/main.rs:12:24: 12:36
        StorageDead(_28);                // scope 2 at src/main.rs:12:35: 12:36
        _26 = exit(move _27) -> bb15;    // scope 2 at src/main.rs:12:5: 12:37
                                         // mir::ConstOperand
                                         // + span: src/main.rs:12:5: 12:23
                                         // + const_: Const { ty: fn(i32) -> ! {exit}, val: Value(exit) }
    }

    bb12: {
        StorageDead(_24);                // scope 4 at src/main.rs:9:29: 9:30
        _22 = count(move _23) -> [return: bb13, unwind: bb15]; // scope 4 at src/main.rs:9:17: 9:31
                                         // mir::ConstOperand
                                         // + span: src/main.rs:9:17: 9:22
                                         // + const_: Const { ty: fn(usize) -> usize {count}, val: Value(count) }
    }

    bb13: {
        StorageDead(_23);                // scope 4 at src/main.rs:9:30: 9:31
        _4 = move _22;                   // scope 4 at src/main.rs:9:9: 9:31
        StorageDead(_22);                // scope 4 at src/main.rs:9:30: 9:31
        _25 = AddWithOverflow(copy _4, const 1_usize); // scope 4 at src/main.rs:10:9: 10:19
        assert(!move (_25.1: bool), "attempt to compute `{} + {}`, which would overflow", copy _4, const 1_usize) -> [success: bb14, unwind: bb15]; // scope 4 at src/main.rs:10:9: 10:19
    }

    bb14: {
        _4 = move (_25.0: usize);        // scope 4 at src/main.rs:10:9: 10:19
        _15 = const ();                  // scope 4 at src/main.rs:8:33: 11:6
        StorageDead(_21);                // scope 3 at src/main.rs:11:5: 11:6
        StorageDead(_18);                // scope 3 at src/main.rs:11:5: 11:6
        StorageDead(_16);                // scope 3 at src/main.rs:11:5: 11:6
        StorageDead(_15);                // scope 3 at src/main.rs:11:5: 11:6
        _14 = const ();                  // scope 3 at src/main.rs:8:5: 11:6
        goto -> bb7;                     // scope 3 at src/main.rs:8:5: 11:6
    }

    bb15 (cleanup): {
        drop(_2) -> [return: bb16, unwind terminate(cleanup)]; // scope 0 at src/main.rs:13:1: 13:2
    }

    bb16 (cleanup): {
        resume;                          // scope 0 at src/main.rs:5:1: 13:2
    }
}
"""


def frozen_with(after, *statements):
    """FROZEN with ``statements`` added after the one starting ``after``, each with its
    span comment (parse_body keeps only statements that have one)."""
    line = next(line for line in FROZEN.splitlines() if line.strip().startswith(after))
    comment = line[line.index("//") :]
    added = "".join(f"\n        {statement} {comment}" for statement in statements)
    return FROZEN.replace(line, line + added, 1)


def test_a_borrow_made_only_from_frozen_owners_is_traced_to_them():
    # Seventh evaluation (Low, #156): `v` of `for v in args[0].split('/')` past its last
    # use and a call was unproven, though it points into `args`, which nothing changes:
    # `&_2`, `index`, `deref`, `split`, `into_iter`, and `next` on an iterator only
    # `next` borrows `&mut`.
    from peeky.rust_mir import _FROZEN_TYPE, _frozen_sources, _only_iterated

    body = parse_body(FROZEN)
    assert _frozen_sources(body, 21, set(), True) == {(2, False)}
    assert _only_iterated(body, 13, True)
    # Where `Iterator` may be the crate's own, `next` may return into the iterator.
    assert _frozen_sources(body, 21, set(), False) is None
    # An iterator borrowed otherwise too (`iter.size_hint()`) may hold anything.
    other = frozen_with("_18 = &mut _13;", "_24 = &_13;")
    assert _frozen_sources(parse_body(other), 21, set(), True) is None
    # A call the borrow passes through with a `&mut` operand: not traced.
    other = FROZEN.replace("index(move _12, const 0_usize)", "index(move _17, const 0_usize)")
    assert _frozen_sources(parse_body(other), 21, set(), True) is None
    for frozen in ("std::vec::Vec<std::string::String>", "[i64; 3]", "(u8, char)", "str"):
        assert _FROZEN_TYPE.fullmatch(frozen), frozen
    for thawed in (
        "std::cell::RefCell<std::string::String>",
        "std::vec::Vec<&str>",
        "std::rc::Rc<std::string::String>",
        "Bag",
    ):
        assert not _FROZEN_TYPE.fullmatch(thawed), thawed


@pytest.mark.parametrize(
    "code,std",
    [
        ("fn f() -> impl Iterator<Item = u8> { [1].into_iter() }", True),
        ("fn g<I: Iterator>(i: I) {} struct S; impl Iterator for S { type Item = u8; }", True),
        ("fn h(i: &mut dyn Iterator<Item = u8>) {} use std::iter::Iterator;", True),
        ("trait Iterator { fn next(&mut self) -> Option<&str>; }", False),
        ("use crate::mine::Walk as Iterator;", False),
        ("make!(Iterator);", False),
    ],
)
def test_next_is_the_standard_librarys_only_where_the_crate_names_no_other(code, std):
    owned = OwnedLocals([], {}, ("src/main.rs",), texts={"src/main.rs": code})
    assert owned._std_iterator() is std


def test_a_frozen_owner_is_never_a_type_the_crate_may_name_itself():
    # Review of #156: a function's header prints its arguments' types by short names
    # (`&Vec<String>`), which a crate's own `Vec` (one a macro declares) may also have.
    from peeky.rust_mir import _frozen_owners

    body = parse_body(FROZEN)
    body.generics = ()  # matched to its IR function: not generic
    assert _frozen_owners(body, 21, True) == {(2, False)}
    # The owner's type is printed whole (`std::vec::Vec<std::string::String>`): a crate's
    # own `Vec` is not that.
    assert _frozen_owners(body, 21, True, frozenset({"Vec"})) == {(2, False)}
    assert _frozen_owners(body, 21, True, frozenset({"String"})) == {(2, False)}
    body = parse_body(FROZEN.replace("std::vec::Vec<std::string::String>", "Vec<String>"))
    body.generics = ()
    assert _frozen_owners(body, 21, True) == {(2, False)}
    assert _frozen_owners(body, 21, True, frozenset({"Vec"})) is None
    # A twin a macro generates (its type parameters unknown) is never judged so.
    assert _frozen_owners(parse_body(FROZEN), 21, True) is None


@pytest.mark.parametrize(
    "edge", ["unwind continue", "unwind unreachable", "unwind terminate(abi)", "unwind: bb9"]
)
def test_a_call_is_read_whatever_its_unwind_edge(edge):
    # Review of #156: calls with no cleanup of their own print `unwind continue`, which
    # left a reference argument's borrows untraced.
    from peeky.rust_mir import _CALL

    call = _CALL.fullmatch(f"<String as Deref>::deref(move _8) -> [return: bb2, {edge}]")
    assert call and (call[1], call[2]) == ("<String as Deref>::deref", "move _8")


@pytest.mark.parametrize(
    "code,untrusted",
    [
        ('fn f() { let v: Vec<String> = vec![]; println!("{}", v.len() as usize); }', set()),
        ("use std::string::String;", set()),
        ('fn f(x: &str) { assert_eq!(x, String::from("a")); }', set()),
        ("struct String;", {"String"}),
        ("use crate::m::Vec;", {"Vec"}),
        ("use std::vec::Vec as Box;", {"Box"}),
        ("mk!(String);", {"String"}),
        ("type u8 = i32;", {"u8"}),
        # Review of #156: a macro may declare one (`decl!(Vec<T>);`).
        ("macro_rules! decl { ($n:ident) => { pub struct Vec<T>(T); }; }", {"Vec"}),
        ("macro_rules! sum { ($($x:expr),*) => { 0 $(+ $x as i64)* }; }", {"i64"}),
        ("macro_rules! two { () => { 2usize }; }", set()),
        ('fn f() { let s = "Vec"; } // String', set()),
    ],
)
def test_a_short_type_name_is_trusted_only_where_the_crate_names_no_other(code, untrusted):
    # A function's header prints types by short names (`&Vec<String>`).
    owned = OwnedLocals([], {}, ("src/main.rs",), texts={"src/main.rs": code})
    assert owned._untrusted_names() == untrusted


def test_the_names_a_crate_gives_its_own_items_are_found_in_one_pass():
    # Review of #156, round 3: each macro call's arguments were searched to the next `;`,
    # per name: 8,000 calls in one statement took 22 s (the inspection's whole budget).
    import time

    calls = ", ".join(f"m!({index})" for index in range(8000))
    code = f"macro_rules! m {{ ($x:expr) => {{ $x }} }}\nfn main() {{ let a = [{calls}]; }}"
    owned = OwnedLocals([], {}, ("src/main.rs",), texts={"src/main.rs": code})
    started = time.monotonic()
    assert owned._untrusted_names() == frozenset()
    assert time.monotonic() - started < 1
    passed = code.replace("m!(7999)", "m!(String)")
    owned = OwnedLocals([], {}, ("src/main.rs",), texts={"src/main.rs": passed})
    assert owned._untrusted_names() == {"String"}


def test_a_temporary_set_before_the_local_took_its_value_holds_no_copy_of_it():
    # #183: `for entry in args` stops at the body's first line after `entry` was moved
    # out of `next()`'s result, a temporary a call given a pointer made, which the next
    # pass reads (`discriminant`). That temporary held an earlier value, never this one:
    # a change to `entry` there was refused as "a statement under way has read it".
    bindings = "debug entry => _14;   // in scope 1 at src/main.rs:2:9: 2:10\n"
    body = mir(
        {
            0: [
                "_11 = &mut _6;",
                "_10 = &mut (*_11);",
                "_9 = next(move _10) -> [return: bb1, unwind continue];",
            ],
            1: [
                "_12 = discriminant(_9);",
                "switchInt(move _12) -> [0: bb5, 1: bb2, otherwise: bb5];",
            ],
            2: [
                "StorageLive(_14);",
                "_14 = move ((_9 as Some).0: std::string::String);",
                "_21 = &_14;",
                "_20 = deref(move _21) -> [return: bb3, unwind continue];",
            ],
            3: [
                "_13 = copy (_14.1: usize);",
                "drop(_14) -> [return: bb4, unwind continue];",
            ],
            4: ["StorageDead(_14);", "_15 = copy _13;", "_16 = discriminant(_9);", "goto -> bb0;"],
            5: ["return;"],
        },
        bindings=bindings,
    )
    # Taken as a whole, a call given any pointer may return a copy of it.
    assert 9 in body.copies_of(14)
    # In the order the code runs, `entry`'s assignment leaves no copy of its new value.
    after = (2, 2)
    assert 9 not in body.copies_at(14, after)
    assert body.live_ahead(after) >> 9 & 1
    # What is read from it after that is a copy (`_13`), until its storage ends.
    assert 13 in body.copies_at(14, (3, 1))
    assert 13 not in body.copies_at(14, (4, 1)) and 13 not in body.copies_at(14, (2, 2))


@pytest.mark.parametrize(
    ("text", "projection", "access"),
    [
        # A copy or move of the part, of a place holding it or inside it, reads it.
        ("_5 = copy _1;", ((("field", 1),)), "read"),
        ("_5 = Add(copy (_1.1: i64), const 1_i64);", (("field", 1),), "read"),
        ("_5 = copy ((_1.1: Inner).0: i32);", (("field", 1),), "read"),
        ("_5 = copy (_1.2: [u32; 3])[2 of 3];", (("field", 2), ("index", 2)), "read"),
        ("_3 = consume(move _1) -> [return: bb1, unwind continue];", ((("field", 0),)), "read"),
        # Another field, an element whose index is not known, or another element: not.
        ("_5 = copy (_1.0: i16);", (("field", 1),), None),
        ("_5 = copy (_1.2: [u32; 3])[_7];", (("field", 2), ("index", 2)), None),
        ("_5 = copy (_1.2: [u32; 3])[1 of 3];", (("field", 2), ("index", 2)), None),
        # A borrow may never be read through; a length or a dereference is no read.
        ("_5 = &_1;", (), None),
        ("_5 = &mut (_1.1: i64);", (("field", 1),), None),
        ("_5 = Len(_1);", (), None),
        # Assigning the whole local, or a place covering the part, ends it; another part
        # or reading it first does not.
        ("_1 = copy _2;", (("field", 1),), "end"),
        ("_1 = Add(copy _1, const 1_i32);", (), "read"),
        ("(_1.1: i64) = const 5_i64;", (("field", 1),), "end"),
        ("(_1.0: i16) = copy _6;", (("field", 1),), None),
        ("(_1.0: i16) = copy (_1.1: i64) as i16 (IntToInt);", (("field", 1),), "read"),
        ("StorageDead(_1);", (), "end"),
        ("drop(_1) -> [return: bb2, unwind continue];", (), None),
        # A projection not known: only the whole local is read for sure.
        ("_5 = copy (_1.1: i64);", None, None),
        ("_5 = copy _1;", None, "read"),
        # A type holding brackets, arrows and braces in the place's annotation.
        (
            "_5 = copy ((_1.0: (fn(i32) -> i32, [u8; 2])).1: [u8; 2])[0 of 2];",
            (("field", 0), ("field", 1), ("index", 0)),
            "read",
        ),
    ],
)
def test_what_a_statement_surely_does_to_a_part_of_a_local(text, projection, access):
    # #192 review: only a read that surely covers the changed part shows the program saw
    # the change.
    assert _root_access(text, 1, projection) == access


STD_MACROS = "/t/lib/rustlib/src/rust/library/std/src/macros.rs"
# What OwnedLocals.std_macros holds for this toolchain, and the crate's function paths.
SINKS = (frozenset({STD_MACROS}), frozenset({"main"}))


def formatting(sink="std::io::_print", kind="i16", file=STD_MACROS, generics=()):
    """`println!("{}", x.a)` as rustc 1.96 builds it, then `x = y` (`y` copied `x`)."""
    body = mir(
        {
            0: [
                "_2 = copy _1;",
                "_8 = &(_1.0: i16);",
                "_7 = (move _8,);",
                "_10 = copy (_7.0: &i16);",
                "_9 = &(*_10);",
                f"_6 = core::fmt::rt::Argument::<'_>::new_display::<{kind}>(move _9) -> [return: bb1, unwind continue];",
            ],
            1: [
                "_5 = [move _6];",
                '_12 = const b"\\x02A \\xc0\\x00";',
                "_11 = &(*_12);",
                "_14 = &_5;",
                "_13 = &(*_14);",
                "_4 = Arguments::<'_>::new::<5, 1>(move _11, move _13) -> [return: bb2, unwind continue];",
            ],
            2: [f"_3 = {sink}(move _4) -> [return: bb3, unwind continue];"],
            3: ["_1 = copy _2;", "StorageDead(_1);", "return;"],
        },
        bindings=(
            "debug x => _1;   // in scope 1 at src/main.rs:2:9: 2:10\n"
            "debug y => _2;   // in scope 1 at src/main.rs:3:9: 3:10\n"
        ),
        files={(2, 0): file},
    )
    body.generics = generics
    return body


@pytest.mark.parametrize("sink", ["std::io::_print", "std::fmt::format", "format"])
def test_std_printing_a_borrowed_primitive_reads_it_at_the_print(sink):
    # `println!("{}", x.a)`: `_print` formats `&x.a`, so the program reads `x.a` there; as
    # `format!` does (`format` alone where no other item is so named).
    body = formatting(sink)
    call = f"_3 = {sink}(move _4) -> [return: bb3, unwind continue];"
    assert body._format_reads(*SINKS) == {call: {"&(_1.0: i16)"}}
    assert _root_access(call, 1, (("field", 0),), body._format_reads(*SINKS)) == "read"
    # Not at the borrow: making a formatting argument reads nothing yet.
    borrow = "_8 = &(_1.0: i16);"
    assert _root_access(borrow, 1, (("field", 0),), body._format_reads(*SINKS)) is None
    # For a change to `x.a` the program saw it; for one to `x.b`, `x = y` puts it back.
    assert not body.copy_undoes(2, 1, (0, 1), (("field", 0),), SINKS)
    assert body.copy_undoes(2, 1, (0, 1), (("field", 1),), SINKS)
    # From past the print, nothing reads `x.a` before `x = y`.
    assert body.copy_undoes(2, 1, (3, 0), (("field", 0),), SINKS)


@pytest.mark.parametrize(
    ("sink", "kind", "file", "generics", "crate"),
    [
        # #192 review 3: built and given to a function of the crate's own, never formatted.
        ("ignore", "i16", STD_MACROS, (), {"main", "ignore"}),
        # #192 review 4: a call written in a file of the crate's own that only ends as
        # std's does (`src/library/std/src/macros.rs`), or in another toolchain's.
        ("format", "i16", "src/library/std/src/macros.rs", (), {"main"}),
        ("std::io::_print", "i16", "/w/src/library/std/src/macros.rs", (), {"main"}),
        (
            "std::io::_print",
            "i16",
            "/u/lib/rustlib/src/rust/library/std/src/macros.rs",
            (),
            {"main"},
        ),
        # A function of the crate's own named `format` (std's is then `std::fmt::format`).
        ("format", "i16", STD_MACROS, (), {"main", "format"}),
        # A call in std's file that formats nothing for sure (a panic hook may not).
        ("core::panicking::panic_fmt", "i16", STD_MACROS, (), {"main"}),
        # A type parameter named like a primitive (its `Display` may read nothing), or a
        # body whose type parameters are not known.
        ("std::io::_print", "i16", STD_MACROS, ("i16",), {"main"}),
        ("std::io::_print", "i16", STD_MACROS, None, {"main"}),
        # A type of the crate's own: its `Display` may read any part, or none.
        ("std::io::_print", "Ab", STD_MACROS, (), {"main"}),
    ],
)
def test_a_formatting_argument_nothing_proven_to_format_is_no_read(
    sink, kind, file, generics, crate
):
    body = formatting(sink, kind, file, generics)
    sinks = (SINKS[0], frozenset(crate))
    assert body._format_reads(*sinks) == {}
    assert body.copy_undoes(2, 1, (0, 1), (("field", 0),), sinks)


def test_a_borrow_given_to_a_call_shows_no_read():
    # #192 review: `ignore(&x)` may never read `x`; `x = y` then puts the old value back.
    bindings = (
        "debug x => _1;   // in scope 1 at src/main.rs:2:9: 2:10\n"
        "debug y => _2;   // in scope 1 at src/main.rs:3:9: 3:10\n"
    )
    body = mir(
        {
            0: [
                "_2 = copy _1;",
                "_4 = &_1;",
                "_3 = ignore(move _4) -> [return: bb1, unwind continue];",
            ],
            1: ["_1 = copy _2;", "_5 = copy _1;", "return;"],
        },
        bindings=bindings,
    )
    assert body.copy_undoes(2, 1, (0, 1))
    # Where the program reads `x` first, it sees the change.
    read = mir(
        {
            0: [
                "_2 = copy _1;",
                "_4 = copy _1;",
                "_3 = ignore2(move _4) -> [return: bb1, unwind continue];",
            ],
            1: ["_1 = copy _2;", "return;"],
        },
        bindings=bindings,
    )
    assert not read.copy_undoes(2, 1, (0, 1))


def test_a_block_the_code_cannot_reach_adds_no_copy_to_a_join():
    # #192 review 2: every parsed block seeded the flow, so a block no path reaches
    # (bb2) put its copy into bb3's entry.
    bindings = "debug x => _1;   // in scope 1 at src/main.rs:2:9: 2:10\n"
    body = mir(
        {
            0: ["_1 = const 1_i32;", "goto -> bb1;"],
            1: ["goto -> bb3;"],
            2: ["_7 = copy _1;", "goto -> bb3;"],
            3: ["_8 = copy _7;", "return;"],
        },
        bindings=bindings,
    )
    assert body._reachable() == {0, 1, 3}
    assert 7 not in body.copies_at(1, (3, 0))
    assert 7 not in body.direct_copies_at(1, (3, 0))


def test_std_macro_files_are_exactly_the_toolchains_own(tmp_path):
    # #192 review 4: where std's printing is written, as this toolchain's MIR names it:
    # its sysroot's sources, or `/rustc/<commit-hash>/` where they are not installed.
    from peeky.build import ProbeResult
    from peeky.rust import std_macro_files

    rustc, commit = "/opt/tc/bin/rustc", "ac68faa20c58cbccd01ee7208bf3b6e93a7d7f96"
    probes = (ProbeResult((rustc, "-vV"), f"rustc 1.96.0\ncommit-hash: {commit}\n", ""),)
    assert std_macro_files(rustc, probes, (tmp_path,)) == {
        f"/opt/tc/lib/rustlib/src/rust/library/{name}/src/macros.rs" for name in ("std", "alloc")
    } | {f"/rustc/{commit}/library/{name}/src/macros.rs" for name in ("std", "alloc")}
    # No commit hash read: the sysroot's only. A toolchain inside the crate: none.
    assert len(std_macro_files(rustc, (), (tmp_path,))) == 2
    inside = str(tmp_path / "tc/bin/rustc")
    assert std_macro_files(inside, (), (tmp_path,)) == frozenset()


# #222: `let label: String;`, `label = format!("sum {sum}");` (line 4), `println!` (5).
# `format!`'s last call, spanned in std's file, returns into line 5's block.
DEFERRED = "\n".join(
    [
        "fn main() -> () {",
        "    let mut _0: ();                      // return place in scope 0 at src/main.rs:1:11: 1:11",
        "    let mut _3: std::string::String;     // in scope 0 at /rustc/x/library/alloc/src/macros.rs:113:9: 115:10",
        "    let mut _4: std::string::String;     // in scope 0 at /rustc/x/library/alloc/src/macros.rs:114:13: 114:22",
        "    let _19: &std::string::String;       // in scope 0 at src/main.rs:5:16: 5:21",
        "    scope 1 {",
        "        debug label => _2;               // in scope 1 at src/main.rs:3:9: 3:14",
        "        let _2: std::string::String;     // in scope 1 at src/main.rs:3:9: 3:14",
        "    }",
        "",
        "    bb0: {",
        "        _4 = format(const 1_i32) -> [return: bb1, unwind continue]; // scope 1 at /rustc/x/library/alloc/src/macros.rs:114:13: 114:22",
        "    }",
        "",
        "    bb1: {",
        "        _3 = must_use::<String>(move _4) -> [return: bb2, unwind continue]; // scope 1 at /rustc/x/library/alloc/src/macros.rs:113:9: 115:10",
        "    }",
        "",
        "    bb2: {",
        "        StorageDead(_4);                 // scope 1 at /rustc/x/library/alloc/src/macros.rs:115:9: 115:10",
        "        _2 = move _3;                    // scope 1 at src/main.rs:4:5: 4:10",
        "        _19 = &_2;                       // scope 1 at src/main.rs:5:16: 5:21",
        "        _0 = const ();                   // scope 0 at src/main.rs:6:2: 6:2",
        "        return;                          // scope 0 at src/main.rs:6:2: 6:2",
        "    }",
        "}",
        "",
    ]
)
# bb1's IR: `must_use`, inlined from line 4 (`#[inline(always)]`), then the branch.
DEFERRED_IR = """define internal void @_ZN5repro4main17h4853d4a0533b6443E() unnamed_addr #0 !dbg !10 {
start:
  %_4 = alloca [24 x i8], align 8
  call void @format(ptr sret([24 x i8]) align 8 %_4, i32 1), !dbg !20
  br label %bb1, !dbg !20

bb1:                                              ; preds = %start
  call void @llvm.memcpy.p0.p0.i64(ptr align 8 %_3, ptr align 8 %_4, i64 24, i1 false), !dbg !22
  br label %bb2, !dbg !23

bb2:                                              ; preds = %bb1
  call void @llvm.memcpy.p0.p0.i64(ptr align 8 %label, ptr align 8 %_3, i64 24, i1 false), !dbg !24
  store ptr %label, ptr %args, align 8, !dbg !25
  ret void, !dbg !26
}

!1 = distinct !DICompileUnit(language: DW_LANG_Rust, file: !30, producer: "rustc")
!2 = !DIFile(filename: "src/main.rs", directory: "/w")
!3 = !DIFile(filename: "/rustc/x/library/alloc/src/macros.rs", directory: "")
!4 = !DIFile(filename: "/rustc/x/library/core/src/hint.rs", directory: "")
!9 = !DINamespace(name: "repro", scope: null)
!10 = distinct !DISubprogram(name: "main", linkageName: "_ZN5repro4main17h4853d4a0533b6443E", scope: !9, file: !2, line: 1, unit: !1)
!11 = distinct !DISubprogram(name: "must_use<String>", linkageName: "_ZN4core4hint8must_use17h1111111111111111E", scope: !9, file: !4, line: 613, unit: !1)
!20 = !DILocation(line: 4, column: 13, scope: !10)
!21 = distinct !DILocation(line: 4, column: 13, scope: !10)
!22 = !DILocation(line: 614, column: 5, scope: !11, inlinedAt: !21)
!23 = !DILocation(line: 615, column: 2, scope: !11, inlinedAt: !21)
!24 = !DILocation(line: 4, column: 5, scope: !10)
!25 = !DILocation(line: 5, column: 5, scope: !10)
!26 = !DILocation(line: 6, column: 2, scope: !10)
!30 = !DIFile(filename: "src/main.rs/@/repro.cgu.0", directory: "/w")
"""


def deferred_function(tmp_path, ir=DEFERRED_IR):
    path = tmp_path / "repro.ll"
    path.write_text(ir)
    return ir_functions(path, ["src/main.rs"])["h4853d4a0533b6443"]


def test_a_foreign_call_is_another_lines_only_where_every_instruction_of_it_says_so(tmp_path):
    body = parse_body(DEFERRED)
    every = body.stop_positions({2}, 5, "src/main.rs", True)
    # Taken for line 5's (a macro it may invoke), `must_use`'s return is a place of it.
    assert (2, 0) in every
    # #230 review: where any instruction of the call does not say where it is, it may be
    # line 5's: no location, line 0, an `inlinedAt` chain that does not resolve.
    unlocated = DEFERRED_IR.replace(", i64 24, i1 false), !dbg !22\n", ", i64 24, i1 false)\n")
    zero = DEFERRED_IR.replace("!23 = !DILocation(line: 615", "!23 = !DILocation(line: 0")
    zero = zero.replace("inlinedAt: !21)\n!24", "inlinedAt: !27)\n!24")
    zero += "!27 = distinct !DILocation(line: 0, column: 0, scope: !10)\n"
    lost = DEFERRED_IR.replace(
        "scope: !11, inlinedAt: !21)\n!23", "scope: !11, inlinedAt: !99)\n!23"
    )
    for ir in (unlocated, zero, lost):
        function = deferred_function(tmp_path, ir)
        placed = body.stop_positions({2}, 5, "src/main.rs", True, None, None, None, function)
        assert (2, 0) in placed, ir
        assert function.call_lines(1) is None, ir
    # Every instruction of it on line 4 (inlined from there): not line 5's (#222).
    function = deferred_function(tmp_path)
    placed = body.stop_positions({2}, 5, "src/main.rs", True, None, None, None, function)
    assert (2, 0) not in placed and placed == [(2, 2)]
    assert function.call_lines(1) == {4}
    # A call that is not inlined says where it is itself: line 4 here, so not line 5's.
    called = DEFERRED_IR.replace(
        "  call void @llvm.memcpy.p0.p0.i64(ptr align 8 %_3, ptr align 8 %_4, i64 24, i1 false), !dbg !22\n",
        "  call void @must_use(ptr align 8 %_3, ptr align 8 %_4), !dbg !20\n",
    )
    assert deferred_function(tmp_path, called).call_lines(1) == {4}
    # Its own call with no location, after located code: unknown, though the rest is line 4.
    silent = called.replace("ptr align 8 %_4), !dbg !20\n", "ptr align 8 %_4)\n")
    assert deferred_function(tmp_path, silent).call_lines(1) is None


def test_a_foreign_calls_block_counts_whole_and_an_invoke_leaves_its_store_unknown(tmp_path):
    # #230 review, round 2: which LLVM call is the MIR terminator's is not told. An
    # unlocated call followed by a located helper call (`memcpy`) and branch on line 4 is
    # no proof: the unlocated one could be line 5's.
    body = parse_body(DEFERRED)
    helper = DEFERRED_IR.replace(
        "  call void @llvm.memcpy.p0.p0.i64(ptr align 8 %_3, ptr align 8 %_4, i64 24, i1 false), !dbg !22\n",
        "  call void @must_use(ptr align 8 %_3, ptr align 8 %_4)\n"
        "  call void @memcpy(ptr align 8 %_3, ptr align 8 %_4, i64 24), !dbg !20\n",
    ).replace("  br label %bb2, !dbg !23\n", "  br label %bb2, !dbg !20\n")
    # An `invoke` stores its result in its normal successor, which is line 5's block.
    invoked = DEFERRED_IR.replace(
        "  call void @llvm.memcpy.p0.p0.i64(ptr align 8 %_3, ptr align 8 %_4, i64 24, i1 false), !dbg !22\n"
        "  br label %bb2, !dbg !23\n",
        "  invoke void @must_use(ptr align 8 %_3, ptr align 8 %_4)\n"
        "          to label %bb2 unwind label %cleanup, !dbg !20\n",
    )
    for ir in (helper, invoked):
        function = deferred_function(tmp_path, ir)
        placed = body.stop_positions({2}, 5, "src/main.rs", True, None, None, None, function)
        assert (2, 0) in placed, ir
        assert function.call_lines(1) is None, ir
    # Inlined (#222 as built): every instruction on line 4, no invoke: line 4's.
    assert deferred_function(tmp_path).call_lines(1) == {4}


def test_files_of_one_name_in_two_directories_are_not_one_file(tmp_path):
    # #230 review: `a/mod.rs` and `b/mod.rs` as DIFiles of one name in two directories:
    # code inlined from `b`'s line 4 is no line of `a`'s.
    ir = DEFERRED_IR.replace(
        '!2 = !DIFile(filename: "src/main.rs", directory: "/w")',
        '!2 = !DIFile(filename: "mod.rs", directory: "/w/src/a")',
    )
    ir = ir.replace(
        '!30 = !DIFile(filename: "src/main.rs/@/repro.cgu.0", directory: "/w")',
        '!30 = !DIFile(filename: "src/a/mod.rs/@/repro.cgu.0", directory: "/w")',
    )
    ir += '!5 = !DIFile(filename: "mod.rs", directory: "/w/src/b")\n'
    ir += '!12 = distinct !DISubprogram(name: "g", scope: !9, file: !5, line: 1, unit: !1)\n'
    ir = ir.replace(
        "!21 = distinct !DILocation(line: 4, column: 13, scope: !10)",
        "!21 = distinct !DILocation(line: 4, column: 13, scope: !12)",
    )
    path = tmp_path / "dup.ll"
    path.write_text(ir)
    function = ir_functions(path, ["src/a/mod.rs"])["h4853d4a0533b6443"]
    assert function.file == "src/a/mod.rs"
    # The inlined code's site is in `b`'s file: another file's, so none of `a`'s lines;
    # under the name alone it was taken for `a`'s line 4.
    assert function.sites.get("bb1") is None
    assert function.call_lines(1) == set()
    # Only the build's own `src/a/mod.rs` is the program's source, not another `mod.rs`.
    other = ir.replace('directory: "/w/src/a")', 'directory: "/elsewhere")')
    path.write_text(other)
    assert "h4853d4a0533b6443" not in ir_functions(path, ["src/a/mod.rs"])
