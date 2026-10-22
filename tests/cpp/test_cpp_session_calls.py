"""C++ session states after a step whose call an exception ended (tenth agent evaluation):
LLDB's step runs on to a later call of the same function from the same place, so a name
the earlier call listed is said to be out of scope at this line, never in this same call."""

import json
import os

import pytest

from peeky.cli import main

SOURCE = """#include <cstdio>
#include <stdexcept>

static int g(int n) {
    if (n == 0) throw std::runtime_error("boom");
    return n;
}

static int f(int n) {
    int r = n * 10; /* START */
    {
        int doubled = r * 2;
        r = doubled + g(n); /* THROWS */
    }
    return r;
}

int main() {
    int total = 0;
    for (int i = 0; i < 3; i++) {
        try {
            total += f(i);
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


@pytest.mark.timeout(180)
def test_a_step_an_exception_ended_never_claims_the_same_call(program, capsys):
    assert main(["session", "start", str(program), "--at", str(line_of("START"))]) == 0
    capsys.readouterr()
    try:
        for _ in range(3):
            assert main(["session", "step", "--json"]) == 0
            state = json.loads(capsys.readouterr().out)
            if state["location"].endswith(f":{line_of('THROWS')}"):
                break
        assert state["location"].endswith(f":{line_of('THROWS')}"), state
        assert main(["session", "get", "doubled"]) == 0
        capsys.readouterr()
        # `g(0)` throws through `f(0)`: the step never ends there, and the session's
        # breakpoint stops `f(1)`, called from the same place, before its block.
        assert main(["session", "step", "--json"]) == 0
        state = json.loads(capsys.readouterr().out)
        assert state["location"].endswith(f":{line_of('START')}") and state["hit"] == 2, state
        assert main(["session", "get", "doubled", "--json"]) == 1
        (row,) = json.loads(capsys.readouterr().out)
        assert row["error"].startswith(
            f"not found: 'doubled' was visible at line {line_of('THROWS')}, at an earlier stop "
            "in this function, and this stop is outside its scope (read it at a line in its "
            "scope); visible: "
        ), row
        assert "same call" not in row["error"], row
    finally:
        main(["session", "close"])
