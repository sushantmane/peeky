"""lldb-dap 18's notes on a GCC C++ build are whole once per build, then short; a build id
names the sources and the compiler that make a build."""

import json
import os
import re
import shutil

import pytest

from peeky.cli import main

SOURCE = """#include <cstdio>
#include <map>
#include <string>

int main() {
    auto order = [](int a, int b) { return a > b; };
    std::map<int, long, decltype(order)> by_hour(order);
    std::string name = "night";
    for (int i = 0; i < 12; i++) by_hour[i] = i * 10L;
    std::printf("%zu %s\\n", by_hour.size(), name.c_str()); /* READ */
    return 0;
}
"""


@pytest.fixture
def program(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    case = tmp_path / "case"
    case.mkdir()
    (case / "hours.cpp").write_text(SOURCE)
    monkeypatch.chdir(case)
    return case


def line_of(marker):
    return next(i for i, text in enumerate(SOURCE.splitlines(), 1) if f"/* {marker} */" in text)


def probe(capsys, *flags):
    code = main(["probe", "hours.cpp", "--at", str(line_of("READ")), "--capture", *flags])
    out = capsys.readouterr().out
    assert code == 0, out
    return out


SHORT = (
    "of LLDB's about the build's debug information was moved out of the output again "
    "(debugger_diagnostics in --json); a program line just like one would be too"
)
EDIT = ("--old", "i * 10L", "--new", "i * 11L")


def build_of(capsys, *flags):
    return json.loads(probe(capsys, "name", "--json", *flags))["build_id"]


@pytest.mark.timeout(240)
def test_lldb_dap_18s_notes_are_whole_once_per_build(program, capsys):
    first = probe(capsys, "name")
    if "with the shape of LLDB's errors" not in first:
        pytest.skip("this debugger prints no error about the build's debug information")
    assert "_M_local_buf" in first, first
    again = probe(capsys, "name")
    assert "_M_local_buf" not in again and SHORT in again, again
    if "its own or the program's still on its way" in first:
        assert "the debugger again sent more text as Peeky closed the session" in again, again
    shown = json.loads(probe(capsys, "name", "--json"))
    assert any("_M_local_buf" in warning for warning in shown["warnings"]), shown["warnings"]
    # A candidate, and the same target with its source changed, are
    # other builds, whose first probe says it whole again.
    out = probe(capsys, "name", *EDIT)
    assert "_M_local_buf" in out and SHORT not in out, out
    assert SHORT in probe(capsys, "name", *EDIT)
    (program / "hours.cpp").write_text(SOURCE + "// changed\n")
    out = probe(capsys, "name")
    assert "_M_local_buf" in out and SHORT not in out, out


@pytest.mark.timeout(240)
def test_a_build_id_names_the_sources_and_the_compiler(program, cxx, capsys, monkeypatch):
    # What the text's once per build keys on: the same for the same sources and tools,
    # another for a candidate, a changed source or another compiler.
    first = build_of(capsys)
    assert re.fullmatch(r"[0-9a-f]{64}", first) and build_of(capsys) == first
    assert build_of(capsys, *EDIT) not in (first, None)
    from native_compilers import cxx_for

    other, _ = cxx_for("clang" if cxx == "gcc" else "gcc")
    if other is None:
        other = shutil.which("clang++-18") if cxx == "gcc" else None
    if other:
        own = os.environ["PEEKY_NATIVE_CXX"]
        monkeypatch.setenv("PEEKY_NATIVE_CXX", other)
        assert build_of(capsys) not in (first, None)
        monkeypatch.setenv("PEEKY_NATIVE_CXX", own)
    (program / "hours.cpp").write_text(SOURCE + "// changed\n")
    assert build_of(capsys) not in (first, None)
