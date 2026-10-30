"""Session states on a C build: a step into or out of a function says so (a recursive
call too), a name whose block a step left says why it is gone, a stack too deep to read
whole gets no note, and `session start` warns when it stopped at a breakpoint the debugger
moved. A step out shows what the function returned, and a step that the session's breakpoint
stopped first says so."""

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
        # LLDB's steps can run on to a later call (after a `longjmp`): only the scope is
        # claimed, not the call.
        assert row["error"].startswith(
            f"not found: 'square' was visible at line {line_of('SQUARE')}, at an earlier stop "
            "in this function, and this stop is outside its scope (read it at a line in its "
            "scope); visible: "
        ), row
    finally:
        main(["session", "close"])
    assert main(["session", "start", str(program), "--at", str(line_of("CALL"))]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "step", "--into"]) == 0
        out = capsys.readouterr().out
        assert f"calls.c:{line_of('WALK')} in walk" in out
        assert "note: now in `walk`, one call deeper, called from `main`" in out
        assert "call_change" not in state_of(capsys, "session", "step")
        # A recursive call is one call deeper, in the same function.
        state = state_of(capsys, "session", "step", "--into")
        assert state["location"].endswith(f":{line_of('WALK')}"), state
        assert state["call_change"] == {
            "kind": "deeper",
            "from": "walk",
            "to": "walk",
            "calls": 1,
        }, state
        assert main(["session", "step", "--out"]) == 0
        out = capsys.readouterr().out
        assert f"calls.c:{line_of('RECURSE')} in walk" in out
        assert "note: left `walk`; now in `walk`" in out
        state = state_of(capsys, "session", "step", "--out")
        assert state["call_change"] == {
            "kind": "left",
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


DEEP = """#include <stdio.h>

static int deep(int n) {
    if (n == 0) {
        return 0; /* BOTTOM */
    }
    return 1 + deep(n - 1); /* UP */
}

int main(void) {
    printf("DEEP:%d\\n", deep(20000));
    return 0;
}
"""


@pytest.mark.timeout(180)
def test_a_stack_too_deep_to_read_whole_gets_no_note_and_steps_on(program, capsys):
    # A session step reads at most 256 frames: 20,000 frames read whole (`levels: 0`) pass
    # the debugger connection's 4 MiB message limit, which closes the session
    # (`DEBUGGER_ERROR`). A deeper stack gets no note.
    path = program.with_name("deep.c")
    path.write_text(DEEP)

    def line(marker):
        return next(i for i, text in enumerate(DEEP.splitlines(), 1) if f"/* {marker} */" in text)

    assert main(["session", "start", str(path), "--at", str(line("BOTTOM"))]) == 0
    capsys.readouterr()
    try:
        for args in ((), ("--out",), ("--into",)):
            state = state_of(capsys, "session", "step", *args)
            assert state["state"] == "stopped" and "call_change" not in state, (args, state)
        state = state_of(capsys, "session", "step", "--out")
        assert state["location"].endswith(f":{line('UP')}") and "call_change" not in state
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report"]) == 0
        assert "DEEP:20000" in capsys.readouterr().out
    finally:
        main(["session", "close"])


@pytest.mark.timeout(120)
def test_session_start_never_warns_of_a_moved_breakpoint_it_never_stopped_at(program, capsys):
    # The program ends without stopping, so the start says nothing of "values are from
    # line N".
    path = program.with_name("never.c")
    path.write_text(
        "static int never(int x) {\n"
        "    int value = x + 1;\n"
        "\n"
        "    return value;\n"
        "}\n"
        "\n"
        "int main(int argc, char **argv) {\n"
        "    (void)argv;\n"
        "    return argc > 5 ? never(argc) : 0;\n"
        "}\n"
    )
    assert main(["session", "start", str(path), "--at", "3", "--json"]) == 0
    try:
        state = json.loads(capsys.readouterr().out)
        assert state["state"] == "terminated" and "warnings" not in state, state
    finally:
        main(["session", "close"])


STEPS = """#include <stdio.h>
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
def steps(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "s.c"
    path.write_text(STEPS)
    return path


def step_line(marker):
    return next(i for i, line in enumerate(STEPS.splitlines(), 1) if marker in line)


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr().out


@pytest.mark.timeout(240)
def test_step_out_shows_what_the_function_returned(steps, capsys):
    # `session step --out` shows the value, with no `session get return`.
    assert run(capsys, "session", "start", steps, "--at", step_line("TWICE"), "--name", "r")[0] == 0
    try:
        code, out = run(capsys, "session", "step", "--out", "--name", "r")
        assert code == 0 and "(return): int = 42" in out, out
    finally:
        run(capsys, "session", "close", "--name", "r")
    # JSON has it too.
    assert run(capsys, "session", "start", steps, "--at", step_line("TWICE"), "--name", "j")[0] == 0
    try:
        code, out = run(capsys, "session", "step", "--out", "--name", "j", "--json")
        state = json.loads(out)
        assert state.get("returned", {}).get("value") == "42", state
    finally:
        run(capsys, "session", "close", "--name", "j")


@pytest.mark.timeout(240)
def test_a_step_the_sessions_breakpoint_stops_says_so(steps, capsys):
    # A step over a recursive call stops at the session's breakpoint in the nested call
    # (hit 2), and says so.
    assert run(capsys, "session", "start", steps, "--at", step_line("DEPTH"), "--name", "d")[0] == 0
    try:
        code, out = run(capsys, "session", "step", "--name", "d")
        assert code == 0 and f":{step_line('RECURSE')}" in out, out
        code, out = run(capsys, "session", "step", "--name", "d")
        assert f":{step_line('DEPTH')} in depth, hit 2" in out, out
        assert "note: the session's breakpoint (hit 2) stopped the step before it ended" in out
    finally:
        run(capsys, "session", "close", "--name", "d")
