"""CUDA qualification's DAP sequence against a real GDB (cuda-gdb is GDB-based).

Needs GDB 14 or newer with Python: set PEEKY_GDB (CI's Linux image uses GDB 15.1).
GDB 14.2 and 16.3 were checked by hand with the same sequence (ADR 0007).
"""

import importlib.util
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from peeky.dap import DAP

ROOT = Path(__file__).resolve().parents[2]
GDB = os.environ.get("PEEKY_GDB")
pytestmark = pytest.mark.skipif(not GDB, reason="set PEEKY_GDB to a GDB 14+ with Python")

spec = importlib.util.spec_from_file_location("qualify", ROOT / "scripts/cuda/qualify.py")
qualify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualify)

LOOP = """#include <stdio.h>
int main(void) {
  int total = 0;
  for (int i = 0; i < 3; i++) {
    total += i; /* BREAK */
  }
  printf("TOTAL:%d\\n", total);
  return 0;
}
"""


def test_launch_sequence_stops_reads_and_finishes(tmp_path):
    source = tmp_path / "loop.c"
    source.write_text(LOOP)
    compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    subprocess.run([compiler, "-g", "-O0", "-o", str(tmp_path / "loop"), str(source)], check=True)
    dap = DAP(command=[GDB, "-q", "-i=dap"])
    steps = qualify.Steps(dap, 30)
    try:
        checks, stopped = qualify.launch_to_stop(steps, tmp_path / "loop", tmp_path, source, 5)
        assert checks == dict.fromkeys(
            ("dap_initialize", "breakpoint_accepted", "stopped_at_breakpoint"), True
        ), steps.log
        thread = stopped["body"]["threadId"]
        frame = qualify.top_frame(steps, thread)
        assert frame["line"] == 5
        assert qualify.frame_locals(steps, frame) == {"i": "0", "total": "0"}
        assert qualify.printed(steps.repl("print total + 100")) == "100"
        assert steps.repl("no-such-command") is None
        assert "no-such-command" in steps.log[-1]["refused"]
        assert steps.evaluate("i + 7", frame["id"]) == "7"
        steps.request("setBreakpoints", {"source": {"path": str(source)}, "breakpoints": []})
        cursor = dap.event_cursor()
        steps.request("continue", {"threadId": thread})
        assert steps.event({"terminated", "exited"}, after=cursor)["event"] in (
            "terminated",
            "exited",
        )
        deadline = time.monotonic() + 5
        while "TOTAL:3" not in dap.snapshot_output()[0] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "TOTAL:3" in dap.snapshot_output()[0]
    finally:
        dap.close()
