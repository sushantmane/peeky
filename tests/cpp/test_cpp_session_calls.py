"""C++ session states where an exception ends a call (tenth agent evaluation, and the
review of #274). The stacks never prove a return: a step out that an exception cut short
says only which function it left, and a step the session's breakpoint stopped first, in
a later call of the same function from the same place, says nothing of calls or scopes."""

import json
import os

import pytest

from peeky.cli import main

SOURCE = """#include <cstdio>
#include <stdexcept>

static int g(int n) {
    if (n == 0) throw std::runtime_error("boom"); /* THROW */
    return n;
}

static int f(int n) {
    int r = n * 10; /* START */
    {
        int doubled = r * 2;
        r = doubled + g(n); /* CALLS_G */
    }
    return r;
}

int main() {
    int total = 0;
    for (int i = 0; i < 3; i++) {
        try {
            total += f(i); /* CALLS_F */
        } catch (const std::exception &e) {
            total += 1;
        }
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
    path = tmp_path / "throws.cpp"
    path.write_text(SOURCE)
    return path


def line_of(marker):
    return next(i for i, text in enumerate(SOURCE.splitlines(), 1) if f"/* {marker} */" in text)


def state_of(capsys, *argv):
    assert main([*argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.timeout(180)
def test_a_step_out_an_exception_cut_short_never_says_returned(program, cxx, capsys):
    # Review of #274: `step --out` of `g(0)`, which throws, stopped in `f(1)`'s call of
    # `g` from the same place (the return watch), and the state said "returned from".
    assert main(["session", "start", str(program), "--at", str(line_of("THROW"))]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "step", "--out"]) == 0
        out = capsys.readouterr().out
        lines = out.splitlines()
        # GCC's names for static functions, from their declarations (#167).
        scope = "::" if cxx == "gcc" else ""
        assert lines[0].startswith("state: stopped at ") and f" in {scope}f(int) " in lines[0], out
        assert lines[1] == f"note: left `{scope}g(int)`; now in `{scope}f(int)`", out
        assert "it never returned" in out and "returned from" not in out, out
    finally:
        main(["session", "close"])


@pytest.mark.timeout(180)
def test_a_step_an_exception_ended_says_nothing_of_calls_or_scopes(program, capsys):
    # `g(0)` throws through `f(0)`: LLDB's step never ends there, and the session's
    # breakpoint stops `f(1)`, called from the same place, before its block. The stacks
    # look alike, but nothing proves they are the same call: no note, and no claim about
    # `doubled`.
    assert main(["session", "start", str(program), "--at", str(line_of("START"))]) == 0
    capsys.readouterr()
    try:
        for _ in range(3):
            state = state_of(capsys, "session", "step")
            if state["location"].endswith(f":{line_of('CALLS_G')}"):
                break
        assert state["location"].endswith(f":{line_of('CALLS_G')}"), state
        assert main(["session", "get", "doubled"]) == 0
        capsys.readouterr()
        state = state_of(capsys, "session", "step")
        assert state["location"].endswith(f":{line_of('START')}") and state["hit"] == 2, state
        assert state["reason"] == "breakpoint" and "call_change" not in state, state
        assert main(["session", "get", "doubled", "--json"]) == 1
        (row,) = json.loads(capsys.readouterr().out)
        assert row["error"].startswith("not found; visible: "), row
    finally:
        main(["session", "close"])
