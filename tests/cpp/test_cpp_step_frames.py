"""After a step that stopped in the program's own code, a frame outside its sources (the C
runtime's below main) is refused as before any step, by its index. It is never said that
the step left the program, which `session continue` would lose the stop on."""

import json
import os

import pytest

from peeky.cli import main

SOURCE = """#include <cstdio>

static int total;

static void add_reading(int value) {
    int scaled = value * 2; /* STOP */
    total += scaled;
}

int main() {
    int limit = 3;
    for (int i = 0; i < limit; i++) {
        add_reading(i);
    }
    std::printf("TOTAL:%d\\n", total);
    return 0;
}
"""

REFUSED = "is not in the prepared program's sources; inspect one of your program's frames"


@pytest.fixture
def program(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    case = tmp_path / "case"
    case.mkdir()
    (case / "ward.cpp").write_text(SOURCE)
    monkeypatch.chdir(case)
    return case


def line_of(marker):
    return next(i for i, text in enumerate(SOURCE.splitlines(), 1) if f"/* {marker} */" in text)


@pytest.mark.timeout(180)
def test_after_a_step_in_the_sources_a_frame_below_main_is_refused_by_its_index(program, capsys):
    stop = line_of("STOP")
    assert main(["session", "start", "ward.cpp", "--at", str(stop)]) == 0
    try:
        capsys.readouterr()
        assert main(["session", "inspect", "--frame", "2"]) == 2
        before = capsys.readouterr().err
        assert before.startswith("peeky: [INVALID_REQUEST] Frame 2 (") and REFUSED in before
        assert main(["session", "step", "--json"]) == 0
        state = json.loads(capsys.readouterr().out)
        assert state["location"].endswith(f"/ward.cpp:{stop + 1}") and state["reason"] == "step", (
            state
        )
        assert "outside_program" not in state, state
        for command in (("inspect",), ("get", "limit")):
            assert main(["session", *command, "--frame", "2"]) == 2
            err = capsys.readouterr().err
            # The same refusal as before the step, never "no longer on the stack".
            assert err.split("\n")[0] == before.split("\n")[0], err
            assert "step stopped outside" not in err, err
        # The stop is kept: the step's own frame and main's are read.
        snapshot = json.loads(_out(capsys, "session", "inspect", "--json"))
        assert "add_reading(" in snapshot["function"], snapshot
        assert snapshot["location"].endswith(f"/ward.cpp:{stop + 1}"), snapshot
        (row,) = json.loads(_out(capsys, "session", "get", "limit", "--frame", "1", "--json"))
        assert row["value"] == "3", row
    finally:
        main(["session", "close"])


def _out(capsys, *argv):
    assert main(list(argv)) == 0
    return capsys.readouterr().out
