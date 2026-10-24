"""The eleventh agent evaluation's Lows in a C++ session (#306, grades 4 and 5, seats 2
and 6): a `void` function's step out returns no value, paths are named as `session start`
named them from any directory, `session report` says where the program is and how far the
session came, and a step onto the breakpoint's line is not a hit, though the pass is."""

import json
import os

import pytest

from peeky.cli import main

SOURCE = """#include <cstdio>

struct Stats {
    int sum = 0;
    void reset() {
        sum = 0; /* RESET */
    }
    int add(int q) {
        sum += q;
        return sum;
    }
};

int main() {
    Stats stats;
    int total = 0;
    for (int i = 0; i < 3; i++) {
        stats.reset(); /* LOOP */
        total += stats.add(i);
    }
    std::printf("TOTAL:%d\\n", total);
    return 0;
}
"""


@pytest.fixture
def program(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    case = tmp_path / "case"
    case.mkdir()
    (case / "stats.cpp").write_text(SOURCE)
    monkeypatch.chdir(case)
    return case


def line_of(marker):
    return next(i for i, text in enumerate(SOURCE.splitlines(), 1) if f"/* {marker} */" in text)


def text_of(capsys, *argv, code=0):
    assert main(list(argv)) == code
    return capsys.readouterr().out


@pytest.mark.timeout(180)
def test_a_void_step_out_paths_and_the_report(program, cxx, capsys, monkeypatch):
    loop, reset = line_of("LOOP"), line_of("RESET")
    out = text_of(capsys, "session", "start", "stats.cpp", "--at", str(loop))
    assert f"state: stopped at stats.cpp:{loop} in main, hit 1 " in out, out
    try:
        # From another directory, paths are named as the start named them.
        monkeypatch.chdir(program.parent)
        out = text_of(capsys, "session", "step", "--into")
        assert out.startswith(f"state: stopped at stats.cpp:{reset} in "), out
        out = text_of(capsys, "session", "step", "--out")
        assert out.startswith(f"state: stopped at stats.cpp:{loop} in main "), out
        assert "(return): void (no value: the function returns nothing)" in out, out
        state = json.loads(text_of(capsys, "session", "inspect", "--json"))
        assert state["returned"]["type"] == "void" and state["returned"]["unavailable"], state
        out = text_of(capsys, "session", "report")
        assert out.startswith(f"state: stopped at stats.cpp:{loop} in main\n"), out
        assert "\nso far: 1 hit (stops at the session's breakpoint), 2 steps\n" in out, out
    finally:
        main(["session", "close"])


@pytest.mark.timeout(180)
def test_a_step_onto_the_breakpoint_line_leaves_its_pass_to_the_next_hit(program, capsys):
    # A step's stop is never numbered. Xcode's LLDB reports it as the step's, and the next
    # `continue` stops there again first, as hit 2 of the same pass (i == 1); an LLDB that
    # reports it as the breakpoint's numbers it hit 2 then.
    loop = line_of("LOOP")
    text_of(capsys, "session", "start", "stats.cpp", "--at", str(loop))
    try:
        for _ in range(6):
            state = json.loads(text_of(capsys, "session", "step", "--json"))
            if state["location"].endswith(f":{loop}"):
                break
        assert state["location"].endswith(f":{loop}"), state
        if state["reason"] == "step":
            assert "hit" not in state, state
            assert state["note"].startswith("not a hit: LLDB reports this stop as the step's"), (
                state
            )
            state = json.loads(text_of(capsys, "session", "continue", "--json"))
        assert state["location"].endswith(f":{loop}") and state["hit"] == 2, state
        (row,) = json.loads(text_of(capsys, "session", "get", "i", "--json"))
        assert row["value"] == "1", row
    finally:
        main(["session", "close"])
