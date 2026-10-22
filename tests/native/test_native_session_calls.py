"""Session states on a C build: a step into or out of a call says so (a recursive call
too), a name whose block a step left says why it is gone, and `session start` warns
when the debugger moved its breakpoint (tenth agent evaluation)."""

import json
import os

import pytest

from peeky.cli import main

SOURCE = """#include <stdio.h>

static int walk(int depth) {
    if (depth == 0) { /* WALK */
        return 0;
    }
    return 1 + walk(depth - 1); /* RECURSE */
}

int main(void) {
    int total = 0;
    for (int i = 0; i < 2; i++) { /* LOOP */
        int square = i * i;
        total += square; /* SQUARE */
    }

    total += walk(2); /* CALL */
    printf("TOTAL:%d\\n", total);
    return 0;
}
"""


@pytest.fixture
def program(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "calls.c"
    path.write_text(SOURCE)
    return path


def line_of(marker):
    return next(i for i, text in enumerate(SOURCE.splitlines(), 1) if f"/* {marker} */" in text)


def state_of(capsys, *argv):
    assert main([*argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.timeout(180)
def test_steps_say_when_they_leave_a_block_or_change_calls(program, capsys):
    assert main(["session", "start", str(program), "--at", str(line_of("SQUARE"))]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "get", "square"]) == 0
        assert "square: int = 0" in capsys.readouterr().out
        for _ in range(4):
            # Past the closing `}` (where a build may stop), to the loop's header.
            state = state_of(capsys, "session", "step")
            assert "call_change" not in state, state
            if state["location"].endswith(f":{line_of('LOOP')}"):
                break
        # Back on the loop's header: the body's block, and `square`, are gone.
        assert state["location"].endswith(f":{line_of('LOOP')}"), state
        assert main(["session", "get", "square", "--json"]) == 1
        (row,) = json.loads(capsys.readouterr().out)
        assert row["error"].startswith(
            f"not found: 'square' was visible at line {line_of('SQUARE')}, earlier in this "
            "same call, but the block declaring it has ended (read it at a line inside that "
            "block); visible: "
        ), row
    finally:
        main(["session", "close"])
    assert main(["session", "start", str(program), "--at", str(line_of("CALL"))]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "step", "--into"]) == 0
        out = capsys.readouterr().out
        assert f"calls.c:{line_of('WALK')} in walk" in out
        assert "note: entered `walk`, called from `main`" in out
        assert "call_change" not in state_of(capsys, "session", "step")
        # A recursive call is another call of the same function.
        state = state_of(capsys, "session", "step", "--into")
        assert state["location"].endswith(f":{line_of('WALK')}"), state
        assert state["call_change"] == {
            "kind": "entered",
            "from": "walk",
            "to": "walk",
            "calls": 1,
        }, state
        assert main(["session", "step", "--out"]) == 0
        out = capsys.readouterr().out
        assert f"calls.c:{line_of('RECURSE')} in walk" in out
        assert "note: returned from `walk` to `walk`" in out
        state = state_of(capsys, "session", "step", "--out")
        assert state["call_change"] == {
            "kind": "returned",
            "from": "walk",
            "to": "main",
            "calls": 1,
        }, state
    finally:
        main(["session", "close"])


@pytest.mark.timeout(120)
def test_session_start_warns_when_the_debugger_moved_its_breakpoint(program, capsys):
    blank = line_of("CALL") - 1
    assert main(["session", "start", str(program), "--at", str(blank)]) == 0
    try:
        out = capsys.readouterr().out
        assert f"calls.c:{line_of('CALL')} in main, hit 1" in out
        assert (
            f"\nwarning: The debugger moved the breakpoint from line {blank} to line "
            f"{line_of('CALL')} (line {blank} has no code of its own); values are from line "
            f"{line_of('CALL')}. Choose a line from `peeky describe`."
        ) in out, out
    finally:
        main(["session", "close"])
