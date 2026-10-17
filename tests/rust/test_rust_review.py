"""Rust interventions must reach the program; every compiled file must be declared."""

import json
import os
import shutil
from pathlib import Path

import pytest

from peeky.build import dependency_paths
from peeky.cli import main
from peeky.rust_ir import ir_locals, memory_locals

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def crate(tmp_path, monkeypatch):
    names = ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER")
    if not all(os.environ.get("PEEKY_RUST_" + name) for name in names):
        pytest.skip("set PEEKY_RUST_* tool paths to enable Rust conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "crate"
    shutil.copytree(ROOT / "examples/rust", root, ignore=shutil.ignore_patterns("target"))
    return root


def edit_main(root, after, lines):
    path = root / "src/main.rs"
    text = path.read_text().splitlines()
    index = next(i for i, line in enumerate(text) if after in line)
    text[index + 1 : index + 1] = lines
    path.write_text("\n".join(text) + "\n")
    return index + 2  # first inserted line, 1-based


def probe(capsys, *argv):
    code = main(["probe", *map(str, argv), "--json", "--quiet", "--no-save"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(240)
def test_locals_the_program_keeps_in_registers_cannot_be_changed(crate, capsys):
    first = edit_main(
        crate,
        "let mut start: i32",
        ["    let marker: i32 = 7;", '    println!("MARK:{}", marker + 1000);'],
    )
    at = first + 1  # the println: marker is live and assigned
    code, result = probe(capsys, crate, "--at", at, "--capture", "marker", "--set", "marker=100")
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED"
    assert "only shows a copy" in result["error"]["message"]
    code, result = probe(capsys, crate, "--at", at, "--capture", "marker,start", "--set", "start=0")
    assert code == 0, result.get("error")
    assert "MARK:1007" in result["output"] and "PAGE:[10, 20, 30]" in result["output"]


@pytest.mark.timeout(240)
def test_sources_from_outside_the_crate_fail_the_build(crate, tmp_path, capsys):
    helper = tmp_path / "helper.rs"
    helper.write_text("pub fn one() -> i32 { 1 }\n")
    path = crate / "src/main.rs"
    path.write_text(f'#[path = "{helper}"]\nmod helper;\n' + path.read_text())
    code, result = probe(capsys, crate, "--at", "37", "--capture", "start")
    assert code == 1 and "not a declared source" in result["error"]["message"]
    assert str(helper) in result["error"]["message"]


IR = """
define internal void @_ZN4demo4main17h0123456789abcdefE() !dbg !10 {
start:
  %kept = alloca [4 x i8], align 4
  %copy.dbg.spill = alloca [4 x i8], align 4
  #dbg_declare(ptr %kept, !20, !DIExpression(), !30)
  #dbg_declare(ptr %copy.dbg.spill, !21, !DIExpression(DW_OP_LLVM_fragment, 0, 32), !30)
  call void @llvm.dbg.declare(metadata ptr %older, metadata !22, metadata !DIExpression()), !dbg !30
  #dbg_declare(ptr %twice.dbg.spill2, !24, !DIExpression(), !30)
  #dbg_declare(ptr %twice, !25, !DIExpression(), !30)
  #dbg_value(i32 1, !26, !DIExpression(), !30)
  #dbg_declare(ptr %inlined.dbg.spill.i, !27, !DIExpression(), !31)
  #dbg_declare(ptr %slot.i, !28, !DIExpression(), !31)
  ret void
}
!10 = distinct !DISubprogram(name: "main", linkageName: "_ZN4demo4main17h0123456789abcdefE", scope: !1, file: !2, line: 1)
!11 = distinct !DILexicalBlock(scope: !10, file: !2, line: 2, column: 5)
!12 = distinct !DILexicalBlockFile(scope: !11, file: !2, discriminator: 0)
!13 = distinct !DISubprogram(name: "helper", scope: !1, file: !2, line: 9)
!14 = distinct !DISubprogram(name: "inner", linkageName: "_ZN4demo5inner17hfedcba9876543210E", scope: !1, file: !2, line: 20)
!20 = !DILocalVariable(name: "kept", scope: !12, file: !2, line: 3, type: !5, align: 32)
!21 = !DILocalVariable(name: "copy", scope: !11, file: !2, line: 4, type: !5, align: 32)
!22 = !DILocalVariable(name: "older", scope: !10, file: !2, line: 5, type: !5)
!23 = !DILocalVariable(name: "unnamed", scope: !13, file: !2, line: 9, type: !5)
!24 = !DILocalVariable(name: "twice", scope: !10, file: !2, line: 6, type: !5)
!25 = !DILocalVariable(name: "twice", scope: !11, file: !2, line: 7, type: !5)
!26 = !DILocalVariable(name: "ssa", scope: !10, file: !2, line: 8, type: !5)
!27 = !DILocalVariable(name: "inlined", scope: !14, file: !2, line: 21, type: !5)
!28 = !DILocalVariable(name: "slot", scope: !14, file: !2, line: 22, type: !5)
!30 = !DILocation(line: 3, column: 9, scope: !12)
!31 = !DILocation(line: 21, column: 9, scope: !14, inlinedAt: !30)
"""


def test_memory_locals_follow_the_declared_storage(tmp_path):
    path = tmp_path / "demo.ll"
    path.write_text(IR)
    found = memory_locals(path)
    function, inner = "h0123456789abcdef", "hfedcba9876543210"
    assert found[(function, "kept")] is True
    assert found[(function, "older")] is True
    assert found[(function, "copy")] is False
    # One shadowing declaration is only a copy: the name is not provably in memory.
    assert found[(function, "twice")] is False
    assert (function, "ssa") not in found  # dbg_value only: no memory at all
    assert not any(name == "unnamed" for _, name in found)  # no hash to match frames
    # #[inline(always)] callees: renamed copies and any inlined declaration.
    assert found[(inner, "inlined")] is False and found[(inner, "slot")] is False


PARAMETERS_IR = """
define internal i64 @_ZN4demo3sum17h1111111111111111E() !dbg !40 {
start:
  #dbg_declare(ptr %values.dbg.spill, !50, !DIExpression(), !60)
  #dbg_declare(ptr %list.dbg.spill, !51, !DIExpression(), !60)
  #dbg_declare(ptr %moved, !52, !DIExpression(), !60)
  #dbg_declare(ptr %local.dbg.spill, !53, !DIExpression(), !60)
  #dbg_declare(ptr %shadow.dbg.spill, !54, !DIExpression(), !60)
  #dbg_declare(ptr %shadow, !55, !DIExpression(), !60)
  #dbg_declare(ptr %copied.dbg.spill.i, !56, !DIExpression(), !61)
  #dbg_declare(ptr %untyped.dbg.spill, !57, !DIExpression(), !60)
  #dbg_declare(ptr %other.dbg.spill, !58, !DIExpression(), !60)
  ret i64 0
}
!40 = distinct !DISubprogram(name: "sum", linkageName: "_ZN4demo3sum17h1111111111111111E", scope: !1, file: !2, line: 10, type: !3, scopeLine: 11, unit: !9)
!45 = !DICompositeType(tag: DW_TAG_structure_type, name: "&[i32]", file: !2, size: 128, align: 64, elements: !4)
!46 = !DIDerivedType(tag: DW_TAG_pointer_type, name: "&alloc::vec::Vec<i32, alloc::alloc::Global>", baseType: !47, size: 64, align: 64, dwarfAddressSpace: 0)
!50 = !DILocalVariable(name: "values", arg: 1, scope: !40, file: !2, line: 12, type: !45)
!51 = !DILocalVariable(name: "list", arg: 2, scope: !40, file: !2, line: 10, type: !46)
!52 = !DILocalVariable(name: "moved", arg: 3, scope: !40, file: !2, line: 10, type: !45)
!53 = !DILocalVariable(name: "local", scope: !40, file: !2, line: 13, type: !45, align: 64)
!54 = !DILocalVariable(name: "shadow", arg: 4, scope: !40, file: !2, line: 10, type: !45)
!55 = !DILocalVariable(name: "shadow", scope: !40, file: !2, line: 14, type: !45)
!56 = !DILocalVariable(name: "copied", arg: 1, scope: !40, file: !2, line: 10, type: !45)
!57 = !DILocalVariable(name: "untyped", arg: 5, scope: !40, file: !2, line: 10)
!58 = !DILocalVariable(name: "renamed", arg: 6, scope: !40, file: !2, line: 10, type: !45)
!60 = !DILocation(line: 10, column: 1, scope: !40)
!61 = !DILocation(line: 10, column: 1, scope: !40, inlinedAt: !60)
"""


def test_reference_parameters_are_proven_only_from_their_entry_spill(tmp_path):
    path = tmp_path / "demo.ll"
    path.write_text(PARAMETERS_IR)
    _, parameters = ir_locals(path)
    function = "h1111111111111111"
    # Readable past the last of the function, body-scope and parameter lines.
    assert parameters == {
        (function, "values"): {"type": "&[i32]", "line": 12},
        (function, "list"): {"type": "&alloc::vec::Vec<i32, alloc::alloc::Global>", "line": 11},
        # Borrowed or reassigned: its own slot, still stored on entry.
        (function, "moved"): {"type": "&[i32]", "line": 11},
    }
    # Not proven: a local, a parameter that a local shadows, an inlined copy, an
    # unknown type, and a slot named for another variable.


def test_dependency_files_list_every_prerequisite(tmp_path):
    text = (
        f"{tmp_path}/target/x.d: src/main.rs src/a\\ b.rs \\\n  /abs/helper.rs\n\n"
        "src/main.rs:\n# env-dep:CARGO_MANIFEST_DIR=/ws/My: Project\n"
        "obj.o: main.c /sdk/stdio.h \\\n pagination.h\n"
    )
    assert [str(p) for p in dependency_paths(text, tmp_path)] == [
        f"{tmp_path}/src/main.rs",
        f"{tmp_path}/src/a b.rs",
        "/abs/helper.rs",
        f"{tmp_path}/main.c",
        "/sdk/stdio.h",
        f"{tmp_path}/pagination.h",
    ]


@pytest.mark.timeout(240)
def test_locals_of_always_inlined_functions_cannot_be_changed(crate, capsys):
    path = crate / "src/main.rs"
    path.write_text(
        path.read_text()
        + "\n#[inline(always)]\nfn inner(a: i32) -> i32 {\n    let marker: i32 = a + 7;\n"
        '    println!("MARK:{}", marker + 1000);\n    marker\n}\n'
    )
    edit_main(crate, "let mut start: i32", ['    println!("R:{}", inner(request.page));'])
    at = next(
        i for i, line in enumerate(path.read_text().splitlines(), 1) if 'println!("MARK' in line
    )
    code, result = probe(capsys, crate, "--at", at, "--capture", "marker", "--set", "marker=100")
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED", result
    assert not result["interventions"]


@pytest.mark.timeout(240)
def test_rust_programs_take_per_run_arguments(crate, capsys):
    code, result = probe(
        capsys, crate, "--at", "36", "--capture", "request.page,start", "--arg", "2"
    )
    assert code == 0, result.get("error")
    assert [row["value"] for row in result["rows"]] == ["2", "6"]
    assert "PAGE:[70, 80]" in result["output"]
    assert result["program_args"] == result["target_identity"]["arguments"] == ["2"]


SHOWN = """
fn shown(values: &[i32], label: &str, list: &Vec<i32>, text: &mut String, big: &Vec<i32>) -> usize {
    let ordered: Vec<i32> = values.to_vec();
    let view: &[i32] = &ordered;
    let n = view.len();
    drop(ordered);
    let other: Vec<i32> = vec![-1, -2, -3];
    let s = String::from("first");
    let s = s + "-second-and-longer";
    text.push('!');
    let titled = format!("{label}:{n}");
    n + other.len() + s.len() + titled.len() + list.len() + big.len() // SHOWN
}
"""


@pytest.mark.timeout(240)
def test_rust_reference_parameters_are_read_from_memory_and_locals_never_are(crate, capsys):
    path = crate / "src/main.rs"
    path.write_text(path.read_text() + SHOWN)
    edit_main(
        crate,
        'println!("PAGE:{values:?}");',
        [
            '    let mut text = String::from("größe");',
            "    let big: Vec<i32> = (0..300).map(|i| 1_000_000 + i).collect();",
            '    println!("SHOWN:{}", shown(&values, "größe", &values, &mut text, &big));',
        ],
    )
    at = next(i for i, line in enumerate(path.read_text().splitlines(), 1) if "// SHOWN" in line)
    selectors = (
        "values,values[0],values[-1],values.length,label,label.length,label[0],list,list.len,"
        "list.capacity,list[2],text,big,big[200],big.len,ordered,view,other,s,titled"
    )
    code, result = probe(capsys, crate, "--at", at, "--capture", selectors)
    assert code == 0, result.get("error")
    rows = {row["selector"]: row for row in result["rows"]}
    # Locals are never read from memory: `ordered` was dropped, `view` outlived it,
    # `s` was moved into its shadow. Their stale pointers can reach reused memory.
    # `label` is a parameter that format! borrows: still read.
    for local in ("ordered", "view", "other", "s", "titled"):
        assert rows[local].get("view") != "memory", rows[local]
    if rows["values"].get("view") == "memory":
        # lldb-dap with readMemory (LLVM 20+): read at DWARF-validated offsets.
        value = {selector: row.get("value") for selector, row in rows.items()}
        assert value["values"] == value["list"] == "[40, 50, 60]"
        assert (value["values[0]"], value["values[-1]"], value["values.length"]) == (
            "40",
            "60",
            "3",
        )
        assert value["label"] == '"größe"' and value["label.length"] == str(len("größe".encode()))
        assert "shown whole" in rows["label[0]"]["error"]
        assert (value["list.len"], value["list[2]"]) == ("3", "60")
        assert int(value["list.capacity"]) >= 3
        assert value["text"] == '"größe!"'
        assert len(value["big"]) <= 1000 and value["big"].endswith(" of 300)")
        assert (value["big[200]"], value["big.len"]) == ("1000200", "300")
    else:
        # LLVM 18 and 19 lldb-dap have no readMemory: the raw value, and an explicit reason.
        assert "only for reference parameters" in rows["values[0]"]["error"]
        assert rows["list.len"]["value"] == "3"
    assert "SHOWN:" in result["output"] and "PAGE:[40, 50, 60]" in result["output"]
