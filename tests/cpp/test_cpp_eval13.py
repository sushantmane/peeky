"""The thirteenth agent evaluation's Lows in a C++ build (#385): a map ordered by a lambda
shows the compiler's name for it, not the typedef LLDB names it by; its preview says how
to read the entries it leaves out; and lldb-dap 18's notes on a GCC build are whole once
per build, then short."""

import json
import os
import re

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


@pytest.mark.timeout(240)
def test_a_lambda_comparator_and_a_cut_preview(program, cxx, capsys):
    shown = json.loads(probe(capsys, "by_hour,by_hour[3]", "--json"))
    row = next(row for row in shown["rows"] if row["selector"] == "by_hour")
    # The compiler's own name for the lambda: clang's `(lambda at hours.cpp:6:18)`, GCC's
    # `main()::<lambda(int, int)>`; never LLDB's typedef (`type`, `key_compare`).
    want = (
        r"\(lambda at hours\.cpp:6:\d+\)" if cxx == "clang" else r"main\(\)::<lambda\(int, int\)>"
    )
    assert re.fullmatch(rf"std::map<int, long, {want}>", row["type"]), row
    # LLDB's own name stays alongside.
    assert row.get("debugger_type") and "lambda" not in row["debugger_type"], row
    assert row["value"].startswith("size=12 {11: 110, 10: 100,") and row["value"].endswith("…}"), (
        row
    )
    keyed = next(row for row in shown["rows"] if row["selector"] == "by_hour[3]")
    assert keyed.get("value") == "30", keyed
    text = probe(capsys, "by_hour")
    assert (
        "preview: `by_hour` holds 12 entries, more than its preview shows; read one with "
        "`by_hour[K]` (key K) or `by_hour[#N]` (the Nth), or list them in a session: "
        "`peeky session get by_hour`"
    ) in text, text
    # The probe's text and the store keep the same rows.
    assert re.search(rf"by_hour  +std::map<int, long, {want}>", text), text


@pytest.mark.timeout(240)
def test_lldb_dap_18s_notes_are_whole_once_per_build(program, capsys):
    first = probe(capsys, "name")
    if "with the shape of LLDB's errors" not in first:
        pytest.skip("this debugger prints no error about the build's debug information")
    assert "_M_local_buf" in first, first
    again = probe(capsys, "name")
    assert "_M_local_buf" not in again, again
    assert (
        "of LLDB's about the build's debug information was moved out of the output again "
        "(debugger_diagnostics in --json); a program line just like one would be too"
    ) in again, again
    if "its own or the program's still on its way" in first:
        assert "the debugger again sent more text as Peeky closed the session" in again, again
    shown = json.loads(probe(capsys, "name", "--json"))
    assert any("_M_local_buf" in warning for warning in shown["warnings"]), shown["warnings"]
