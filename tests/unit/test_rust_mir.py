"""The owned-local proof (rust_mir): MIR parsing, initialization dataflow and IR blocks."""

import subprocess
import sys

import pytest

from peeky.rust_mir import IrFunction, OwnedLocals, _segments, ir_functions, parse_body


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
    owned = OwnedLocals([body], functions)
    assert owned.body("h0123456789abcdef") is body
    assert owned.proven("h0123456789abcdef", "v", 4) and not owned.proven(
        "h0123456789abcdef", "v", 3
    )
    assert not owned.proven("h0123456789abcdef", "v @ main.rs:x", 4)
    assert not owned.proven("hffffffffffffffff", "v", 4)
    assert OwnedLocals([body, body], functions).body("h0123456789abcdef") is None


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
