"""Inferring a dropped JVM exit must never end a live, thread-churning JVM."""

import os
from pathlib import Path

import pytest

from peeky.probe import Probe, run_probe

CHURN = """package demo;
import java.util.concurrent.locks.LockSupport;
public class Churn {
  public static void main(String[] args) throws Exception {
    for (int i = 0; i < 500; i++) {
      Thread t = new Thread(() -> LockSupport.park(), "parked-" + i);
      t.setDaemon(true);
      t.start();
    }
    long end = System.nanoTime() + 6_000_000_000L;
    byte[][] junk = new byte[64][];
    int n = 0;
    while (System.nanoTime() < end) {
      Thread t = new Thread(() -> {}, "short-" + n);
      t.start();
      junk[n++ % 64] = new byte[64 * 1024];
    }
    System.out.println("CHURNED " + n); // OBSERVE
  }
}
"""


@pytest.mark.timeout(120)
def test_a_live_churning_jvm_is_never_reported_as_exited(tmp_path):
    # Extreme thread churn: java-debug may fall behind (the probe can time out) and
    # its first `threads` replies can be empty while the JVM is alive. The only
    # wrong outcome is a reported or inferred exit; a single empty reply once ended
    # this live JVM within about 6 s.
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a prepared Java adapter bundle")
    from peeky.java import JavaTarget

    (tmp_path / "demo").mkdir()
    (tmp_path / "demo/Churn.java").write_text(CHURN)
    line = next(i for i, text in enumerate(CHURN.splitlines(), 1) if "OBSERVE" in text)
    target = JavaTarget(bundle=Path(bundle), sources=("demo/Churn.java",), main_class="demo.Churn")
    result = run_probe(tmp_path, Probe("demo/Churn.java", line, ("n",), timeout=15), adapter=target)
    # Under this load java-debug may also break its protocol stream (seen on Linux CI);
    # failing closed with DEBUGGER_ERROR is acceptable. A no-stop or exit is not.
    if result["status"] == "failed":
        assert result["error"]["code"] in ("DEBUGGER_ERROR", "TIMEOUT"), result["error"]
    else:
        assert result["status"] in ("completed", "timeout"), result.get("error")
    assert not any("inferred" in warning for warning in result["warnings"])
    if result["status"] == "completed":
        assert result["hits"] == 1 and "CHURNED" in result["output"]
