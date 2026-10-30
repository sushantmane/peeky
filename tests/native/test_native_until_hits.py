"""On a real C build: resumed after its last hit, a `--until hits` probe's program runs on
until the probe ends it. Its output read meanwhile is not "so far (the program was still
running)" with `output_complete: false` when it has exited with code 0 and all of its
output was read."""

import json
import os

import pytest

from peeky.cli import main
from peeky.probe import _table

PROGRAM = """#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
int main(int argc, char **argv) {
    printf("BEFORE\\n");
    fflush(stdout);
    int total = 0;
    for (int i = 0; i < 3; i++) {
        total += i; /* HIT */
    }
    printf("AFTER %d\\n", total);
    fflush(stdout);
    sleep(atoi(argv[1]));
    return 0;
}
"""


@pytest.fixture
def program(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "ran_on.c"
    path.write_text(PROGRAM)
    return path


@pytest.mark.timeout(240)
@pytest.mark.parametrize("sleep", ["30", "0"])
def test_until_hits_says_the_output_can_follow_the_last_hit(program, capsys, sleep):
    line = next(i for i, text in enumerate(PROGRAM.splitlines(), 1) if "HIT" in text)
    argv = ["probe", program, "--at", line, "--capture", "i", "--hits", 2, "--until", "hits"]
    assert main([str(arg) for arg in [*argv, "--arg", sleep, "--json", "--no-save"]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed" and result["ended"] == "hits"
    output = result["output"].replace("\r\n", "\n")
    text = _table(result)
    assert "still running" not in text and output.startswith("BEFORE\n")
    assert result.get("output_after_last_hit") is True
    if result["output_complete"]:
        # It exited before Peeky disconnected, and the adapter then ended its stream: all
        # of its output, and its exit.
        assert sleep == "0" and result["exit_code"] == 0 and output == "BEFORE\nAFTER 3\n"
        assert not result["output_truncated"] and "program_ended" not in result
        assert "output (all of it: the program ran on after the last hit, to its exit):" in text
    elif result.get("program_ended"):
        # It ended, but the adapter kept its stream open: more output may have been coming.
        assert sleep == "0" and "after the last hit and ended, but its output may still" in text
    else:
        # Peeky ended it: no exit of its own to report.
        assert "exit_code" not in result
        assert (
            "output so far (the program ran on after the last hit, so this can include text "
            "written after it):\n  BEFORE"
        ) in text
