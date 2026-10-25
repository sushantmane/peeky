"""What a C probe says of its output when the program starts a process that writes after
the program itself has finished its own work (review of PR #335): Peeky calls the output
whole only where the adapter's stream ended cleanly after the program's end, never from
the end alone, and never without what the later process wrote."""

import json
import os

import pytest

from peeky.cli import main
from peeky.probe import _table

PROGRAM = """#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <sys/types.h>
#include <unistd.h>

int main(void) {
    int n = 3;
    fflush(stdout);
    pid_t child = fork();
    if (child == 0) {
        sleep(2);
        printf("LATE %d\\n", n);
        fflush(stdout);
        return 0;
    }
    printf("EARLY %d\\n", n); /* PARENT */
    fflush(stdout);
    return 0;
}
"""


@pytest.fixture
def program(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "late.c"
    path.write_text(PROGRAM)
    return path


@pytest.mark.timeout(240)
def test_output_a_child_writes_after_its_parent_ends_is_never_called_whole_without_it(
    program, capsys
):
    line = next(i for i, text in enumerate(PROGRAM.splitlines(), 1) if "PARENT" in text)
    argv = ["probe", str(program), "--at", str(line), "--capture", "n", "--json", "--no-save"]
    assert main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed", result.get("error")
    output = result["output"].replace("\r\n", "\n")
    text = _table(result)
    assert output.startswith("EARLY 3\n"), output
    if result["output_complete"]:
        # LLDB reported the program's end only once the child had written (macOS, and
        # lldb-dap 20 on Linux), and the adapter then ended its stream with nothing more.
        assert output == "EARLY 3\nLATE 3\n", output
        assert not result.get("output_after_disconnect"), result
        assert "\noutput:\n" in text, text
    else:
        # lldb-dap 18 on Linux prints its own crash once Peeky disconnects: whatever it
        # had read, nothing proves the stream whole, and the text says so.
        assert result.get("program_ended") is True, result
        assert "output so far (the program ended, but its output may still have been " in text
