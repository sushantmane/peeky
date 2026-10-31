"""What clang and GCC record of a unit, on real builds (ADR 0008's checks, phase 1).

clang's `-grecord-command-line` keeps each `-D`, `-I`, `-include`, `-isystem` and `-U` as
it was given, and its DWARF 5 line table an MD5 of each file it names. A header holding
only macros is named only with `-fdebug-macro`. A `#line` naming a file, or a
`#pragma GCC system_header`, leaves the whole table without MD5s. GCC records neither
the preprocessor's arguments nor any MD5."""

import os
import subprocess
import sys

import pytest
from native_compilers import default_cc, default_cxx, gcc, is_gcc

from peeky.native_units import (
    LineFiles,
    NotRecorded,
    compiler_of,
    md5_of,
    read_units,
    recorded_command,
)

ARGUMENTS = [
    "-Iinc",
    "-Iinc sp",
    "-isystem",
    "sys",
    "-include",
    "pre.h",
    "-DSP=a b",
    '-DQ="x y"',
    "-DBS=a\\b",
    "-DTAB=a\tb",
    "-UZZ",
    "-std=gnu11",
]
# As clang's driver writes them back: a joined value as its own word.
RECORDED = [
    "-I",
    "inc",
    "-I",
    "inc sp",
    "-isystem",
    "sys",
    "-include",
    "pre.h",
    "-D",
    "SP=a b",
    "-D",
    'Q="x y"',
    "-D",
    "BS=a\\b",
    "-D",
    "TAB=a\tb",
    "-U",
    "ZZ",
    "-std=gnu11",
]


def _tools():
    dwarfdump = os.environ.get("PEEKY_NATIVE_DWARFDUMP")
    if not dwarfdump or not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    return dwarfdump


def _clang():
    compiler = default_cc()
    if not compiler or is_gcc(compiler):
        pytest.skip("needs clang as the C compiler (the clang lanes and macOS run it)")
    return compiler


def _program(root):
    for name in ("inc", "inc sp", "sys"):
        (root / name).mkdir()
    (root / "pre.h").write_text("#define PRE 1\n")
    (root / "inc" / "a.h").write_text(
        "#define A_H 2\nstatic int twice(int x) { return x * A_H; }\n"
    )
    (root / "inc sp" / "b.h").write_text("#define B_H 3\n")
    (root / "sys" / "s.h").write_text("#define S_H 4\n")
    (root / "main.c").write_text(
        '#include "a.h"\n#include "b.h"\n#include <s.h>\n'
        "int main(void) { return twice(PRE + B_H + S_H) - 16; }\n"
    )


def _build(compiler, root, flags, source="main.c", env=None):
    """The symbols of ``source`` built with ``flags`` (and ``env`` added to the
    environment), linked as a program: on macOS the dSYM dsymutil makes of it, as Peeky
    reads a program's symbols there."""
    sysroot = os.environ.get("PEEKY_NATIVE_SYSROOT")
    driver = [compiler, *(("-isysroot", sysroot) if sysroot and sys.platform == "darwin" else ())]
    subprocess.run(
        [*driver, "-O0", "-g", "-gdwarf-5", *flags, "-c", source, "-o", "main.o"],
        cwd=root,
        check=True,
        env={**os.environ, **(env or {})},
    )
    subprocess.run([*driver, "main.o", "-o", "program"], cwd=root, check=True)
    if sys.platform != "darwin":
        return root / "program"
    dsymutil = os.environ.get("PEEKY_NATIVE_DSYMUTIL", "dsymutil")
    subprocess.run([dsymutil, "program", "-o", "program.dSYM"], cwd=root, check=True)
    return root / "program.dSYM"


def _read(dwarfdump, symbols):
    def dump(*arguments):
        return subprocess.run(
            [dwarfdump, *arguments, str(symbols)], capture_output=True, text=True, check=True
        ).stdout

    (unit,) = read_units(dump("--debug-info", "--recurse-depth=0"))
    files = LineFiles()
    files.feed(dump("--debug-line"))
    return unit, files.close()[unit.stmt_list]


def _version(compiler):
    said = subprocess.run([compiler, "--version"], capture_output=True, text=True, check=True)
    return said.stdout.splitlines()[0]


def _named(unit, table):
    return {
        os.path.realpath(table.path(index, unit.comp_dir)): entry.checksum
        for index, entry in table.files.items()
    }


def test_clang_records_each_preprocessor_argument_and_each_file_s_md5(tmp_path):
    dwarfdump, compiler = _tools(), _clang()
    _program(tmp_path)
    unit, table = _read(
        dwarfdump, _build(compiler, tmp_path, ["-grecord-command-line", *ARGUMENTS])
    )
    assert compiler_of(unit.producer) == "clang" and unit.version == 5
    words = recorded_command(unit, _version(compiler))
    start = words.index("-I")
    assert words[start : start + len(RECORDED)] == RECORDED
    assert words[words.index("-c") + 1] == "main.c"
    assert table.checksummed() and table.describes(unit)
    named = _named(unit, table)
    root = tmp_path.resolve()
    assert named[str(root / "main.c")] == md5_of(root / "main.c")
    assert named[str(root / "inc" / "a.h")] == md5_of(root / "inc" / "a.h")
    for path, md5 in named.items():
        assert md5 == md5_of(path), path
    # A header with only macros has no code, so its unit's table names it only with
    # -fdebug-macro.
    for header in ("pre.h", "inc sp/b.h", "sys/s.h"):
        assert str(root / header) not in named


def test_without_the_flag_clang_records_no_command_line(tmp_path):
    dwarfdump, compiler = _tools(), _clang()
    _program(tmp_path)
    unit, table = _read(dwarfdump, _build(compiler, tmp_path, ARGUMENTS))
    assert table.checksummed()
    with pytest.raises(NotRecorded, match="rebuild with -grecord-command-line"):
        recorded_command(unit, _version(compiler))


def _pairs(words):
    return list(zip(words, words[1:]))


def test_clang_records_what_response_files_configurations_and_overrides_add(tmp_path):
    dwarfdump, compiler = _tools(), _clang()
    _program(tmp_path)
    (tmp_path / "args.rsp").write_text("-DRSP=1 -Iinc\n")
    (tmp_path / "my.cfg").write_text("-DCFG=1\n")
    flags = ["-grecord-command-line", "@args.rsp", "--config=./my.cfg", "-Iinc sp", "-Isys"]
    flags += ["-include", "pre.h"]
    unit, _ = _read(
        dwarfdump,
        _build(compiler, tmp_path, flags, env={"CCC_OVERRIDE_OPTIONS": "+-DOVR=1"}),
    )
    words = recorded_command(unit, _version(compiler))
    for pair in (("-D", "RSP=1"), ("-I", "inc"), ("-D", "CFG=1"), ("-D", "OVR=1")):
        assert pair in _pairs(words), pair
    assert not any(word.startswith(("@", "--config")) for word in words)


def test_clang_records_no_include_path_from_the_environment(tmp_path):
    dwarfdump, compiler = _tools(), _clang()
    _program(tmp_path)
    # <s.h> is found only through them: the build works, and records neither.
    found = str(tmp_path / "sys")
    unit, _ = _read(
        dwarfdump,
        _build(
            compiler,
            tmp_path,
            ["-grecord-command-line", "-Iinc", "-Iinc sp", "-include", "pre.h"],
            env={"CPATH": found, "C_INCLUDE_PATH": found},
        ),
    )
    words = recorded_command(unit, _version(compiler))
    assert not any(found in word for word in words)


def test_clang_records_preprocessor_options_passed_on_as_written(tmp_path):
    dwarfdump, compiler = _tools(), _clang()
    _program(tmp_path)
    passed = ["-Wp,-DWP=1", "-Xpreprocessor", "-DXP=1", "-Xclang", "-DXC=1"]
    unit, _ = _read(
        dwarfdump, _build(compiler, tmp_path, ["-grecord-command-line", *ARGUMENTS, *passed])
    )
    words = recorded_command(unit, _version(compiler))
    assert "-Wp,-DWP=1" in words
    assert ("-Xpreprocessor", "-DXP=1") in _pairs(words) and ("-Xclang", "-DXC=1") in _pairs(words)


def test_a_relative_compilation_directory_names_no_path(tmp_path):
    # `-fdebug-compilation-dir=rel`: the unit's directory says nowhere on this machine.
    dwarfdump, compiler = _tools(), _clang()
    _program(tmp_path)
    unit, table = _read(
        dwarfdump, _build(compiler, tmp_path, [*ARGUMENTS, "-fdebug-compilation-dir=rel"])
    )
    assert unit.comp_dir == "rel" and table.directories[0] == "rel"
    assert table.checksummed() and not table.describes(unit)
    assert {table.path(index, unit.comp_dir) for index in table.files} == {None}


@pytest.mark.skipif(
    sys.platform == "darwin", reason="Apple clang 21 crashes with -fdebug-macro and -gdwarf-5"
)
def test_debug_macro_names_every_header_with_its_md5(tmp_path):
    dwarfdump, compiler = _tools(), _clang()
    _program(tmp_path)
    unit, table = _read(dwarfdump, _build(compiler, tmp_path, ["-fdebug-macro", *ARGUMENTS]))
    assert table.checksummed()
    named, root = _named(unit, table), tmp_path.resolve()
    for header in ("main.c", "pre.h", "inc/a.h", "inc sp/b.h", "sys/s.h"):
        assert named[str(root / header)] == md5_of(root / header)


@pytest.mark.parametrize(
    "source",
    [
        'int twice(int x) { return x * 2; }\n#line 1 "gram.y"\n'
        "int main(void) { return twice(2) - 4; }\n",
        '#include "sh.h"\nint main(void) { return twice(2) - 4; }\n',
    ],
    ids=["line", "system-header"],
)
def test_a_presumed_file_leaves_the_whole_table_without_md5s(tmp_path, source):
    dwarfdump, compiler = _tools(), _clang()
    (tmp_path / "gram.y").write_text("main: ;\n")
    (tmp_path / "sh.h").write_text(
        "#pragma GCC system_header\nstatic int twice(int x) { return x * 2; }\n"
    )
    (tmp_path / "main.c").write_text(source)
    unit, table = _read(dwarfdump, _build(compiler, tmp_path, []))
    assert unit.version == table.version == 5 and len(table.files) >= 2
    assert not table.checksummed()
    assert all(entry.checksum is None for entry in table.files.values())


def test_a_cpp_inheriting_constructor_names_a_file_without_an_md5(tmp_path):
    # So no C++ unit with one is judged; nor is one built with libstdc++, whose headers all
    # have `#pragma GCC system_header`.
    dwarfdump, compiler = _tools(), default_cxx()
    if not compiler or is_gcc(compiler):
        pytest.skip("needs clang as the C++ compiler (the clang lanes and macOS run it)")
    (tmp_path / "main.c").write_text(
        "struct B { B(const char*) {} };\nstruct D : B { using B::B; };\n"
        'int main() { D d("x"); return 0; }\n'
    )
    unit, table = _read(dwarfdump, _build(compiler, tmp_path, ["-x", "c++"]))
    assert [entry.name for entry in table.files.values()] == ["main.c", "<stdin>"]
    assert not table.checksummed() and table.files[1].checksum is None


@pytest.mark.parametrize("assembler", [[], ["-gno-as-loc-support"]], ids=["as", "no-as"])
def test_gcc_records_no_md5_and_no_preprocessor_arguments(tmp_path, assembler):
    # With or without the assembler's line support.
    dwarfdump, compiler = _tools(), gcc()
    if compiler is None:
        pytest.skip("needs GCC")
    _program(tmp_path)
    unit, table = _read(
        dwarfdump,
        _build(compiler, tmp_path, ["-grecord-gcc-switches", *assembler, *ARGUMENTS]),
    )
    assert compiler_of(unit.producer) == "gcc" and table.version == 5
    assert table.files and all(entry.checksum is None for entry in table.files.values())
    assert "-I" not in unit.producer and "SP=" not in unit.producer
    with pytest.raises(NotRecorded, match="GCC, which records no -D, -I or -include"):
        recorded_command(unit, _version(compiler))
