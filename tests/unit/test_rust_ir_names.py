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


def proofs(tmp_path, body, metadata):
    """ir_locals' reference-parameter proofs for one function `h3333333333333333`."""
    path = tmp_path / "demo.ll"
    path.write_text(
        "define internal i64 @_ZN4demo1f17h3333333333333333E() !dbg !40 {\nstart:\n"
        + body
        + "  ret i64 0\n}\n"
        '!40 = distinct !DISubprogram(name: "f", linkageName: '
        '"_ZN4demo1f17h3333333333333333E", scope: !1, file: !2, line: 10, type: !3, '
        "scopeLine: 11, unit: !9)\n"
        "!41 = distinct !DILexicalBlock(scope: !40, file: !2, line: 12, column: 5)\n"
        '!46 = !DIDerivedType(tag: DW_TAG_pointer_type, name: "&alloc::vec::Vec<i64>", '
        "baseType: !47, size: 64, align: 64, dwarfAddressSpace: 0)\n"
        "!60 = !DILocation(line: 10, column: 1, scope: !40)\n" + metadata
    )
    return {name: proof["line"] for (_, name), proof in ir_locals(path)[1].items()}


def parameter(number, name, arg=1, scope="!40", line=10):
    return (
        f'!{number} = !DILocalVariable(name: "{name}", arg: {arg}, scope: {scope}, '
        f"file: !2, line: {line}, type: !46)\n"
    )


def local(number, name, scope="!41", line=12):
    return (
        f'!{number} = !DILocalVariable(name: "{name}", scope: {scope}, file: !2, '
        f"line: {line}, type: !46, align: 64)\n"
    )


def declare(pointer, variable):
    return f"  #dbg_declare(ptr %{pointer}, {variable}, !DIExpression(), !60)\n"


def test_a_shadow_never_declared_revokes_a_parameters_proof(tmp_path):
    # `let é: &Vec<i64>;` (written e + U+0301; rustc normalises it to NFC): rustc
    # emits its variable, but no declaration, as it is never stored (#194 review).
    body = declare('"\\C3\\A9.dbg.spill"', "!50")
    assert proofs(tmp_path, body, parameter(50, "\\C3\\A9")) == {"é": 11}
    assert proofs(tmp_path, body, parameter(50, "\\C3\\A9") + local(51, "\\C3\\A9")) == {}
    # As for an ASCII name, and a shadow at the function's own scope.
    body = declare("w.dbg.spill", "!50")
    assert proofs(tmp_path, body, parameter(50, "w") + local(51, "w", "!40")) == {}
    # Another name is no shadow.
    assert proofs(tmp_path, body, parameter(50, "w") + local(51, "v")) == {"w": 11}
    # Spelled in NFD (e + U+0301; not what rustc writes), it still counts as one.
    body = declare('"\\C3\\A9.dbg.spill"', "!50")
    assert proofs(tmp_path, body, parameter(50, "\\C3\\A9") + local(51, "e\\CC\\81")) == {}


def test_an_unnamed_parameter_is_no_shadow_but_an_unreadable_name_may_be(tmp_path):
    body = declare("w.dbg.spill", "!50")
    unnamed = "!51 = !DILocalVariable(arg: 2, scope: !40, file: !2, line: 10, type: !46)\n"
    assert proofs(tmp_path, body, parameter(50, "w") + unnamed) == {"w": 11}
    for name in ("\\FF", "\\q", "w\\"):
        assert proofs(tmp_path, body, parameter(50, "w") + local(51, name)) == {}, name
    # A variable whose function cannot be told could be in any.
    lost = '!51 = !DILocalVariable(name: "w", scope: !77, file: !2, line: 12, type: !46)\n'
    assert proofs(tmp_path, body, parameter(50, "w") + lost) == {}
    # Another function's variable so named is its own.
    other = (
        '!70 = distinct !DISubprogram(name: "g", linkageName: "_ZN4demo1g17h4444444444444444E"'
        ", scope: !1, file: !2, line: 30, scopeLine: 31)\n" + local(51, "w", "!70", 31)
    )
    assert proofs(tmp_path, body, parameter(50, "w") + other) == {"w": 11}


def test_llvms_numbered_and_dotted_names_prove_only_their_own_variable(tmp_path):
    # LLVM numbers a second value of a name (`größe` -> `größe1`, `größe2`), so a
    # natural `größe1` and a generated one meet: a slot proves only the variable its
    # name spells, and the variable must be its function's only one so named.
    natural = parameter(50, "gr\\C3\\B6\\C3\\9Fe1") + parameter(51, "gr\\C3\\B6\\C3\\9Fe", 2)
    body = declare('"gr\\C3\\B6\\C3\\9Fe1"', "!50") + declare('"gr\\C3\\B6\\C3\\9Fe2"', "!51")
    assert proofs(tmp_path, body, natural) == {"größe1": 11}
    # The generated `größe1` names `größe`'s slot: neither is proven.
    body = declare('"gr\\C3\\B6\\C3\\9Fe1"', "!51") + declare('"gr\\C3\\B6\\C3\\9Fe11"', "!50")
    assert proofs(tmp_path, body, natural) == {}
    # Dotted: a `.dbg.spill` copy proves; LLVM's renamed copy, or any other suffix, not.
    for pointer, proven in (
        ('"gr\\C3\\B6\\C3\\9Fe.dbg.spill"', {"größe": 11}),
        ('"gr\\C3\\B6\\C3\\9Fe.dbg.spill1"', {}),
        ('"gr\\C3\\B6\\C3\\9Fe.dbg.spill.i"', {}),
        ('"gr\\C3\\B6\\C3\\9Fe.1"', {}),
    ):
        body = declare(pointer, "!51")
        assert proofs(tmp_path, body, parameter(51, "gr\\C3\\B6\\C3\\9Fe", 2)) == proven, pointer


def test_escaped_quotes_and_backslashes_are_read_and_others_refused(tmp_path):
    # Not Rust names, but LLVM's grammar: `\22` is `"`, `\5C` is `\`, on both sides.
    for written in ("a\\22b", "a\\5Cb"):
        body = declare(f'"{written}"', "!50")
        name = 'a"b' if "22" in written else "a\\b"
        assert proofs(tmp_path, body, parameter(50, written)) == {name: 11}
    # A slot LLVM cannot have written proves nothing, and is no slot in memory either.
    for written in ("w\\q", "w\\FF"):
        path = tmp_path / "refused.ll"
        body = declare("w", "!50") + declare(f'"{written}"', "!50")
        assert proofs(tmp_path, body, parameter(50, "w")) == {}, written
        path.write_text(
            "define internal i64 @_ZN4demo1f17h3333333333333333E() !dbg !40 {\nstart:\n"
            + body
            + "  ret i64 0\n}\n"
            '!40 = distinct !DISubprogram(name: "f", linkageName: '
            '"_ZN4demo1f17h3333333333333333E", scope: !1, file: !2, line: 10)\n'
            + local(50, "w", "!40")
            + "!60 = !DILocation(line: 10, column: 1, scope: !40)\n"
        )
        assert ir_locals(path)[0] == {("h3333333333333333", "w"): False}, written


def test_a_slot_named_for_another_variable_proves_nothing(tmp_path):
    # The declaration's pointer and its variable's name must agree, exactly.
    for pointer, name in (
        ("w.dbg.spill", "v"),
        ('"\\C3\\A9.dbg.spill"', "e"),
        ("e.dbg.spill", "\\C3\\A9"),
        ('"gr\\C3\\B6\\C3\\9Fe.dbg.spill"', "gr\\C3\\B6sse"),
    ):
        assert proofs(tmp_path, declare(pointer, "!50"), parameter(50, name)) == {}, pointer


PIECES_IR = r"""
define internal void @_ZN4demo1f17h4444444444444444E(ptr align 8 %_1) !dbg !40 {
start:
  %pair.dbg.spill = alloca [8 x i8], align 4
  %split = alloca [8 x i8], align 4
  %split.dbg.spill = alloca [4 x i8], align 4
  %_1.dbg.spill = alloca [8 x i8], align 8
  %both = alloca [4 x i8], align 4
  %both.dbg.spill = alloca [4 x i8], align 4
  %whole = alloca [8 x i8], align 4
  #dbg_declare(ptr %pair.dbg.spill, !50, !DIExpression(), !60)
  #dbg_declare(ptr %split, !51, !DIExpression(DW_OP_LLVM_fragment, 0, 32), !60)
  #dbg_declare(ptr %split.dbg.spill, !51, !DIExpression(DW_OP_LLVM_fragment, 32, 32), !60)
  #dbg_declare(ptr %_1.dbg.spill, !52, !DIExpression(DW_OP_deref, DW_OP_deref), !60)
  #dbg_declare(ptr %both, !53, !DIExpression(), !60)
  #dbg_declare(ptr %both.dbg.spill, !54, !DIExpression(), !60)
  #dbg_declare(ptr %whole, !55, !DIExpression(DW_OP_LLVM_fragment, 0, 32), !60)
  #dbg_declare(ptr %whole, !55, !DIExpression(DW_OP_LLVM_fragment, 32, 32), !60)
  ret void
}
!40 = distinct !DISubprogram(name: "f", linkageName: "_ZN4demo1f17h4444444444444444E", scope: !1, file: !2, line: 10, type: !3, scopeLine: 11, unit: !9)
!50 = !DILocalVariable(name: "pair", scope: !40, file: !2, line: 12, type: !5, align: 32)
!51 = !DILocalVariable(name: "split", scope: !40, file: !2, line: 13, type: !5, align: 32)
!52 = !DILocalVariable(name: "lap", scope: !40, file: !2, line: 14, type: !5, align: 32)
!53 = !DILocalVariable(name: "both", scope: !40, file: !2, line: 15, type: !5, align: 32)
!54 = !DILocalVariable(name: "both", scope: !40, file: !2, line: 16, type: !5, align: 32)
!55 = !DILocalVariable(name: "whole", scope: !40, file: !2, line: 17, type: !5, align: 32)
!60 = !DILocation(line: 10, column: 1, scope: !40)
"""


def test_each_declaration_says_where_its_piece_is(tmp_path):
    # Review of PR #335: every declaration of a name was a slot or a `.dbg.spill` copy,
    # whatever its expression: a closure's capture, declared through its environment's
    # pointer (`DW_OP_deref, DW_OP_deref`), read as a copy kept in a register.
    path = tmp_path / "demo.ll"
    path.write_text(PIECES_IR)
    declarations = {}
    memory, _ = ir_locals(path, declarations)
    function = "h4444444444444444"
    assert declarations[(function, "pair")] == [(12, "copy")]
    # One piece in its slot, one spilled: in neither as a whole.
    assert declarations[(function, "split")] == [(13, "slot"), (13, "copy")]
    assert declarations[(function, "lap")] == [(14, "unknown")]
    assert declarations[(function, "both")] == [(15, "slot"), (16, "copy")]
    assert declarations[(function, "whole")] == [(17, "slot"), (17, "slot")]
    assert memory[(function, "whole")] is True
    for name in ("pair", "split", "lap", "both"):
        assert memory[(function, name)] is False, name
