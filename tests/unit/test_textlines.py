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


@pytest.mark.parametrize("text", ["", "a", "a\n", "a\n\n", "a\r\nb", "\n", "a\nb\r\n", "x\r"])
def test_lines_end_where_splitlines_ends_them_at_a_newline(text):
    from peeky.textlines import split_lines

    assert split_lines(text) == text.splitlines()
    assert split_lines(text, keepends=True) == text.splitlines(keepends=True)


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
