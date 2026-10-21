"""The eighth agent evaluation's session findings, on real C builds: `session step --out`
shows what the function returned (L2), a step that the session's breakpoint stops first
says so (L3), and hints name a session other than `default` (L1). C++ `describe`: tests/cpp."""

import json
import os

import pytest

from peeky.cli import main

PROGRAM = """#include <stdio.h>
static int twice(int n) {
    int r = n * 2; /* TWICE */
    return r;
}
static int depth(int n) {
    int r = 0; /* DEPTH */
    if (n > 0) r = depth(n - 1) + 1; /* RECURSE */
    return r;
}
int main(void) {
    printf("%d %d\\n", twice(21), depth(2));
    return 0;
}
"""


@pytest.fixture
def program(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "s.c"
    path.write_text(PROGRAM)
    return path


def line_of(marker):
    return next(i for i, line in enumerate(PROGRAM.splitlines(), 1) if marker in line)


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr().out


@pytest.mark.timeout(240)
def test_step_out_shows_what_the_function_returned(program, capsys):
    # L2: `session step --out` needed a `session get return` to show the value.
    assert run(capsys, "session", "start", program, "--at", line_of("TWICE"), "--name", "r")[0] == 0
    try:
        code, out = run(capsys, "session", "step", "--out", "--name", "r")
        assert code == 0 and "(return): int = 42" in out, out
    finally:
        run(capsys, "session", "close", "--name", "r")
    # JSON has it too.
    assert run(capsys, "session", "start", program, "--at", line_of("TWICE"), "--name", "j")[0] == 0
    try:
        code, out = run(capsys, "session", "step", "--out", "--name", "j", "--json")
        state = json.loads(out)
        assert state.get("returned", {}).get("value") == "42", state
    finally:
        run(capsys, "session", "close", "--name", "j")


@pytest.mark.timeout(240)
def test_a_step_the_sessions_breakpoint_stops_says_so(program, capsys):
    # L3: a step over a recursive call stopped at the session's breakpoint in the nested
    # call (hit 2), with no word of it; L1: the hints name the session.
    assert run(capsys, "session", "start", program, "--at", line_of("DEPTH"), "--name", "d")[0] == 0
    try:
        code, out = run(capsys, "session", "step", "--name", "d")
        assert code == 0 and f":{line_of('RECURSE')}" in out, out
        code, out = run(capsys, "session", "step", "--name", "d")
        assert f":{line_of('DEPTH')} in depth, hit 2" in out, out
        assert "note: the session's breakpoint (hit 2) stopped the step before it ended" in out
    finally:
        run(capsys, "session", "close", "--name", "d")
