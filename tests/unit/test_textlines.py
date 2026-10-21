"""#179: a U+2028 (or another Unicode line break) in a file name, or in source text, is
part of its line: every tool Peeky reads (dwarfdump, LLDB, cargo, rustc's MIR, the
preprocessor) and every compiler ends a line at `\\n` alone. And the names those tools
print escaped (dwarfdump and clang in octal, LLVM IR in hex) read back whole."""

import json
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "peeky"
BREAKS = ["\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"]


def octal(name):
    """``name`` as dwarfdump and clang print it: an unprintable byte as `\\ooo`."""
    return "".join(
        char
        if char.isascii() and char.isprintable()
        else "".join(f"\\{b:03o}" for b in char.encode())
        for char in name
    )


@pytest.mark.parametrize("text", ["", "a", "a\n", "a\n\n", "a\r\nb", "\n", "a\nb\r\n"])
def test_lines_end_where_splitlines_ends_them_at_a_newline(text):
    from peeky.textlines import split_lines

    assert split_lines(text) == text.splitlines()
    assert split_lines(text, keepends=True) == text.splitlines(keepends=True)


@pytest.mark.parametrize(
    "text, lines", [("x\r", ["x\r"]), ("a\rb\nc\r", ["a\rb", "c\r"]), ("a\r\n", ["a"])]
)
def test_a_lone_carriage_return_is_part_of_its_line(text, lines):
    # #188 review: only a `\r` ending a chunk that `\n` ends goes with it.
    from peeky.textlines import split_lines

    assert split_lines(text) == lines


@pytest.mark.parametrize("separator", BREAKS)
def test_a_unicode_line_break_is_part_of_its_line(separator):
    from peeky.textlines import split_lines

    text = f'name: "main{separator}x.c"\nnext\n'
    assert split_lines(text) == [f'name: "main{separator}x.c"', "next"]


def test_no_text_peeky_reads_is_split_with_str_splitlines():
    found = [
        f"{path.name}:{number}"
        for path in sorted(SRC.glob("*.py"))
        if path.name != "textlines.py"
        for number, line in enumerate(path.read_text().split("\n"), 1)
        if ".splitlines(" in line
    ]
    assert found == []


@pytest.mark.parametrize("separator", ["\u2028", "\x0c"])
def test_a_line_table_names_a_source_with_any_character(tmp_path, separator):
    # dwarfdump prints the name in octal (`main\342\200\250x.c`), which JSON did not
    # read: no source with a non-ASCII name could be debugged.
    from peeky.native import parse_lines

    name = f"main{separator}x.c"
    (tmp_path / name).write_text("int main() {\nreturn 0;\n}\n")
    dump = f'''debug_line[0x00000000]
version: 4
file_names[ 1]:
name: "{octal(name)}"
dir_index: 0
Address Line Column File
0x0000000000401000 2 1 1 0 0 is_stmt
0x0000000000401010 2 1 1 0 0 end_sequence
'''
    assert parse_lines(dump, tmp_path, (name,)) == {name: [2]}


def test_a_debug_info_entry_keeps_an_attribute_with_a_line_separator():
    # dwarfdump prints `DW_AT_decl_file` as it is: a U+2028 split the attribute's line.
    from peeky.native import _parse_dies, _text

    unit = octal("main\u2028x.c")
    text = (
        "0x0000000b: DW_TAG_compile_unit\n"
        f'              DW_AT_name\t("{unit}")\n'
        "\n"
        "0x00000020:   DW_TAG_subprogram\n"
        '                DW_AT_name\t("main")\n'
        '                DW_AT_decl_file\t("/w/main\u2028x.c")\n'
        "                DW_AT_decl_line\t(3)\n"
    )
    dies = _parse_dies(text)
    assert _text(dies[0x0B]["attrs"]["DW_AT_name"]) == "main\u2028x.c"
    attrs = dies[0x20]["attrs"]
    assert _text(attrs["DW_AT_decl_file"]) == "/w/main\u2028x.c" and attrs["DW_AT_decl_line"] == "3"


def test_a_rust_unit_name_reads_back_from_octal():
    from peeky.rust_symbols import _string

    printed = octal("src/x\u2028y.rs")
    assert _string(f'"{printed}")') == "src/x\u2028y.rs"
    with pytest.raises(ValueError):
        _string('"tab\\011")')


def test_a_preprocessor_marker_names_a_source_with_any_character():
    # clang writes a marker's name in octal; `\\(.)` stripped the backslashes only.
    from peeky.c_source import Preprocessed

    seen = []
    printed = octal("dir/a\u2028b.c")
    text = f'# 1 "{printed}"\nint x;\n# 1 "dir/plain\\\\name.c"\nint y;\n'
    Preprocessed([line + b"\n" for line in text.encode().split(b"\n")[:-1]], seen.append)
    assert seen == ["dir/a\u2028b.c", "dir/plain\\name.c"]


def test_a_mir_dump_with_a_line_separator_in_a_span_reads_as_without_it():
    import importlib.util

    from peeky.rust_mir import parse_body

    spec = importlib.util.spec_from_file_location(
        "mir", Path(__file__).with_name("test_rust_mir.py")
    )
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    plain = parse_body(fixtures.FROZEN)
    odd = parse_body(fixtures.FROZEN.replace("src/main.rs", "src/x\u2028y.rs"))
    assert odd.file == "src/x\u2028y.rs" and plain.file == "src/main.rs"
    assert odd.lines == plain.lines and odd.blocks.keys() == plain.blocks.keys()


def test_llvm_ir_strings_read_back_from_hex():
    from peeky.rust_ir import ir_string

    assert ir_string(r"src/x\E2\80\A8y.rs") == "src/x\u2028y.rs"
    assert ir_string(r"gr\C3\B6\C3\9Fe") == "größe"
    assert ir_string(r"a\5Cb\22c") == 'a\\b"c'


def test_a_compile_error_in_a_file_with_a_line_separator_is_one():
    # Cargo writes its JSON messages and progress with the name as it is.
    from peeky.rust import compiler_diagnostic

    name = "src/x\u2028y.rs"
    messages = [
        {
            "reason": "compiler-message",
            "message": {
                "level": "error",
                "message": "expected expression",
                "spans": [{"file_name": name, "is_primary": True}],
                "rendered": f"error: expected expression\n --> {name}:2:5\n",
            },
        },
        {"reason": "build-finished", "success": False},
    ]
    result = {
        "stdout": "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages),
        "stderr": "   Compiling odd v0.1.0 (/tmp/w\u2028x)\n"
        'error: could not compile `odd` (bin "odd") due to 1 previous error\n',
        "exit_code": 101,
    }
    assert compiler_diagnostic(result, (name,))


def test_an_lldb_line_table_row_keeps_a_line_separator_in_its_file():
    from peeky.loop_headers import line_table

    info = "[0x0000000100000410-0x0000000100000430): /w/u\u2028v.c:3:9\n"
    assert line_table(info) == [(0x100000410, 0x100000430, 3, 9)]


def test_an_inline_assembly_refusal_names_its_file_with_any_character():
    # #179 (with #180's refusal): clang's `-E` markers write the name in octal.
    from peeky.assembler import c_refusal

    printed = octal("dir/a\u2028b.c")
    output = f'# 1 "{printed}"\n__asm__(".incbin \\"x\\"");\n'.encode()
    assert "the asm at dir/a\u2028b.c:1 uses `.incbin`" in str(c_refusal(output, False))


@pytest.mark.parametrize(
    "text, rust, lines",
    [
        ("a\rb\nc\n", True, ["a\rb", "c"]),
        ("a\rb\nc\n", False, ["a", "b", "c"]),
        ("a\r\nb\r\n", True, ["a", "b"]),
        ("a\r\nb\r\n", False, ["a", "b"]),
        ("a\fb\vc\n", False, ["a\fb\vc"]),
    ],
)
def test_a_source_has_the_lines_its_compiler_counts(text, rust, lines):
    # #188 review: rustc ends a line at `\n` alone; clang (and javac, Python) also at a
    # lone `\r`; neither at a form feed or vertical tab.
    from peeky.textlines import source_lines

    assert source_lines(text, rust) == lines


def test_a_line_table_is_checked_against_each_compilers_count(tmp_path):
    # `Path.read_text` turned a Rust source's lone `\r` into a line break rustc does not
    # count: line 2 of a one-line Rust file passed as real.
    from peeky.native import parse_lines

    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.rs").write_bytes(b"fn main() {}\r// one line to rustc\n")
    (tmp_path / "main.c").write_bytes(b"int main(void)\r{ return 0; }\n")

    def dump(name, line):
        return f'''debug_line[0x00000000]
version: 4
file_names[ 1]:
name: "{name}"
dir_index: 0
Address Line Column File
0x0000000000401000 {line} 1 1 0 0 is_stmt
0x0000000000401010 {line} 1 1 0 0 end_sequence
'''

    with pytest.raises(ValueError, match="exceeds declared source length"):
        parse_lines(dump("src/main.rs", 2), tmp_path, ("src/main.rs",))
    assert parse_lines(dump("src/main.rs", 1), tmp_path, ("src/main.rs",)) == {"src/main.rs": [1]}
    # clang counts the lone `\r`: its second line is real.
    assert parse_lines(dump("main.c", 2), tmp_path, ("main.c",)) == {"main.c": [2]}


@pytest.mark.parametrize(
    "printed, name",
    [
        ("src/\\\\x41.c", "src/\\x41.c"),
        ("src/\\x41.c", None),
        ("src/\\400.c", None),
        ("src/\\q.c", None),
        ("src/\\377.c", None),
        ('src/a"b.c', None),
        ('src/a\\"b\\t.c', 'src/a"b\t.c'),
        ("main\\342\\200\\250x.c", "main\u2028x.c"),
        ("größe.c", "größe.c"),
    ],
)
def test_octal_names_are_read_only_as_llvm_writes_them(printed, name):
    # #188 review: `codecs.escape_decode` read Python's escapes too (`\x41` as `A`),
    # kept unknown ones and cut `\400` short.
    from peeky.native import _text
    from peeky.rust_symbols import _string
    from peeky.textlines import octal_string

    assert octal_string(printed) == name
    assert octal_string(printed.encode()) == name
    assert _text(f'"{printed}"') == name
    if name is None:
        with pytest.raises(ValueError):
            _string(f'"{printed}")')
    elif not any(ord(char) < 32 for char in name):
        assert _string(f'"{printed}")') == name


def test_a_marker_name_that_is_not_llvms_names_no_file():
    from peeky.c_source import Preprocessed

    seen = []
    text = b'# 1 "src/\\x41.c"\nint x;\n# 1 "src/\\\\x41.c"\nint y;\n'
    Preprocessed([line + b"\n" for line in text.split(b"\n")[:-1]], seen.append)
    assert seen == ["<unread>", "src/\\x41.c"]


@pytest.mark.parametrize(
    "printed, name",
    [
        (r"src/\E2\80\A8y.rs", "src/\u2028y.rs"),
        (r"a\5Cb\22c", 'a\\b"c'),
        (r"src/\FF.rs", None),
        (r"src/\q.rs", None),
        (r"src/\5.rs", None),
        ('a"b', None),
        ("gr\u00f6", None),
        ("x\udcff", None),
    ],
)
def test_ir_names_are_read_only_as_llvm_writes_them(printed, name):
    # #188 review: `\FF` read as U+FFFD could name a declared `src/\ufffd.rs`, and a
    # backslash LLVM never writes alone (`\q`) was kept.
    from peeky.rust_ir import ir_string

    assert ir_string(printed) == name


def test_an_ir_name_llvm_does_not_write_proves_nothing(tmp_path):
    from peeky.rust_ir import ir_locals

    ir = tmp_path / "demo.ll"
    ir.write_bytes(
        b"define internal i64 @_ZN4demo3sum17h3333333333333333E() !dbg !40 {\n"
        b"start:\n"
        b"  #dbg_declare(ptr %kept, !50, !DIExpression(), !60)\n"
        b"  #dbg_declare(ptr %other, !51, !DIExpression(), !60)\n"
        b"  ret i64 0\n}\n"
        b'!40 = distinct !DISubprogram(name: "sum", linkageName: "_ZN4demo3sum17h3333333333333333E", '
        b"scope: !1, file: !2, line: 10, type: !3, scopeLine: 11, unit: !9)\n"
        b'!50 = !DILocalVariable(name: "kept", scope: !40, file: !2, line: 12, type: !5)\n'
        b'!51 = !DILocalVariable(name: "k\\FFpt", scope: !40, file: !2, line: 13, type: !5)\n'
        b"!60 = !DILocation(line: 10, column: 1, scope: !40)\n"
    )
    memory, _ = ir_locals(ir)
    assert memory == {("h3333333333333333", "kept"): True}
