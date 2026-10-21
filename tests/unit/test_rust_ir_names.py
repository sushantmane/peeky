r"""#191: LLVM IR quotes a local whose name is not plain ASCII (`%"gr\C3\B6\C3\9Fe"`, its
bytes in hex) and writes its debug-info name so too: the IR's proofs (kept in memory,
a reference parameter's entry spill) matched only plain names, so `größe` was never
proven, and its reads stayed raw."""

from peeky.rust_ir import ir_locals

QUOTED_IR = r"""
define internal i64 @_ZN4demo3sum17h2222222222222222E(ptr align 8 %"gr\C3\B6\C3\9Fe") !dbg !40 {
start:
  %"z\C3\A4hler" = alloca [8 x i8], align 8
  %"gr\C3\B6\C3\9Fe.dbg.spill" = alloca [8 x i8], align 8
  store ptr %"gr\C3\B6\C3\9Fe", ptr %"gr\C3\B6\C3\9Fe.dbg.spill", align 8
  #dbg_declare(ptr %"gr\C3\B6\C3\9Fe.dbg.spill", !50, !DIExpression(), !60)
  #dbg_declare(ptr %"z\C3\A4hler", !51, !DIExpression(), !60)
  call void @llvm.dbg.declare(metadata ptr %"\C3\B1", metadata !52, metadata !DIExpression()), !dbg !60
  #dbg_declare(ptr %"\C3\A9t\C3\A9.dbg.spill", !53, !DIExpression(), !60)
  ret i64 0
}
!40 = distinct !DISubprogram(name: "sum", linkageName: "_ZN4demo3sum17h2222222222222222E", scope: !1, file: !2, line: 10, type: !3, scopeLine: 11, unit: !9)
!46 = !DIDerivedType(tag: DW_TAG_pointer_type, name: "&alloc::vec::Vec<i64, alloc::alloc::Global>", baseType: !47, size: 64, align: 64, dwarfAddressSpace: 0)
!50 = !DILocalVariable(name: "gr\C3\B6\C3\9Fe", arg: 1, scope: !40, file: !2, line: 10, type: !46)
!51 = !DILocalVariable(name: "z\C3\A4hler", scope: !40, file: !2, line: 12, type: !5, align: 64)
!52 = !DILocalVariable(name: "\C3\B1", scope: !40, file: !2, line: 13, type: !5, align: 32)
!53 = !DILocalVariable(name: "\C3\A9t\C3\A9", scope: !40, file: !2, line: 14, type: !5, align: 64)
!60 = !DILocation(line: 10, column: 1, scope: !40)
"""


def test_quoted_ir_names_are_read_as_the_source_names(tmp_path):
    path = tmp_path / "demo.ll"
    path.write_text(QUOTED_IR)
    declarations = {}
    memory, parameters = ir_locals(path, declarations)
    function = "h2222222222222222"
    # Kept in memory: its own slot, either declaration syntax.
    assert memory[(function, "zähler")] is True and memory[(function, "ñ")] is True
    # A `.dbg.spill` copy: in a register, said so by its line.
    assert memory[(function, "été")] is False
    assert declarations[(function, "été")] == [(14, "copy")]
    # A reference parameter's entry spill proves it, readable past the scope line.
    assert parameters[(function, "größe")] == {
        "type": "&alloc::vec::Vec<i64, alloc::alloc::Global>",
        "line": 11,
    }
    assert not any("\\" in name for _, name in memory)
