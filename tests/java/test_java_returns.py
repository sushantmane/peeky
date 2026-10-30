"""What Java methods return: java-debug lists it after a step out (`⎯►name()`,
JDI's exact value); Peeky shows it labelled with the method, and probes capture it once
per call, called by the program or by the JDK (a comparator)."""

import os
import time
from pathlib import Path

import pytest

from peeky.java import JavaTarget
from peeky.probe import RETURN, Probe, run_probe
from peeky.worker import DebugWorker

SOURCE = """package demo;
import java.util.*;
public class Ret implements Comparator<Integer> {
  static int twice(int x) {
    int y = 2 * x; // TWICE
    return y;
  }
  public int compare(Integer a, Integer b) {
    int r = Integer.compare(a, b); // COMPARE
    return r;
  }
  public static void main(String[] args) {
    List<Integer> items = new ArrayList<>(List.of(3, 1, 2));
    items.sort(new Ret());
    int n = twice(4); // CALL
    System.out.println(items + " " + n);
  }
}
"""


@pytest.fixture
def java(tmp_path):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a prepared Java adapter bundle")
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo/Ret.java").write_text(SOURCE)
    return JavaTarget(bundle=Path(bundle), sources=("demo/Ret.java",), main_class="demo.Ret")


def line_of(marker):
    return next(i for i, text in enumerate(SOURCE.splitlines(), 1) if marker in text)


@pytest.mark.timeout(120)
def test_java_returns_once_per_call(java, tmp_path):
    probe = Probe("demo/Ret.java", line_of("COMPARE"), ("a", "b", "return"), hits="all")
    result = run_probe(tmp_path, probe, adapter=java)
    assert result["status"] == "completed", result.get("error")
    hits = {}
    for row in result["rows"]:
        hits.setdefault(row["hit"], {})[row["selector"]] = row
    assert len(hits) >= 2
    for rows in hits.values():
        a, b = (int(rows[name]["value"].split("value=")[1].rstrip("}")) for name in "ab")
        # Integer.compare: -1, 0 or 1; returned into the JDK's sort (through the bridge).
        assert (rows[RETURN]["value"], rows[RETURN]["type"]) == (str((a > b) - (a < b)), "int")
        assert rows[RETURN]["function"] == "Ret.compare(Integer,Integer)"
    result = run_probe(tmp_path, Probe("demo/Ret.java", line_of("TWICE"), (RETURN,)), adapter=java)
    row = result["rows"][0]
    assert (row["value"], row["line"]) == ("8", line_of("CALL")), row


@pytest.mark.timeout(120)
def test_a_session_step_out_shows_the_methods_return_apart_from_the_locals(java, tmp_path):
    worker = DebugWorker(tmp_path, adapter=java)
    try:
        started = worker.dispatch(
            "launch", {"program": "demo/Ret.java", "breakpoints": [{"line": line_of("TWICE")}]}
        )
        session = {"session_id": started["session_id"]}

        def wait():
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                state = worker.dispatch("wait", {**session, "timeout": 1})
                if state["state"] != "running":
                    return state
            raise AssertionError("no stop")

        stop = wait()
        worker.dispatch("step", {**session, "stop_id": stop["stop_id"], "kind": "out"})
        stop = wait()
        snapshot = worker.dispatch("inspect", {**session, "stop_id": stop["stop_id"]})
        assert snapshot["returned"] == {"function": "Ret.twice(int)", "type": "int", "value": "8"}
        names = [v["name"] for scope in snapshot["scopes"] for v in scope["variables"]]
        # java-debug's `⎯►twice()` entry is not listed as a local.
        assert "items" in names and not any("twice" in name for name in names)
    finally:
        worker.close()


# A method that threw was withheld as "a function returning nothing".
THROWS = """package demo;
public class Throws {
  static int f(int n) {
    int r = n * 7; // F
    if (n == 1) throw new RuntimeException("boom");
    return r;
  }
  public static void main(String[] args) {
    int s = 0;
    for (int i = 0; i < 3; i++) {
      try { s += f(i == 0 ? 1 : 2); } catch (RuntimeException e) { s -= 1; }
    }
    System.out.println("S:" + s);
  }
}
"""


@pytest.mark.timeout(120)
def test_a_method_that_threw_is_withheld_without_claiming_it_returns_nothing(java, tmp_path):
    (tmp_path / "demo/Throws.java").write_text(THROWS)
    target = JavaTarget(bundle=java.bundle, sources=("demo/Throws.java",), main_class="demo.Throws")
    line = next(i for i, text in enumerate(THROWS.splitlines(), 1) if "// F" in text)
    probe = Probe("demo/Throws.java", line, ("n", RETURN), hits="all")
    result = run_probe(tmp_path, probe, adapter=target)
    assert result["status"] == "completed", result.get("error")
    hits = {}
    for row in result["rows"]:
        hits.setdefault(row["hit"], {})[row["selector"]] = row
    threw = hits[1][RETURN]
    assert "value" not in threw and "or it ended by an exception" in threw["error"], threw
    assert "a function returning nothing" not in threw["error"]
    assert [hits[hit][RETURN]["value"] for hit in (2, 3)] == ["14", "14"]


LOOPS = """package demo;
public class Loops {
  static int sum(int n) {
    int t = 0;
    for (int i = 1; i <= n; i++) {
      t += i; // SUM
    }
    return t;
  }
  public static void main(String[] args) {
    System.out.println("S:" + sum(3));
  }
}
"""


@pytest.mark.timeout(120)
def test_a_line_that_stops_again_before_the_return_claims_no_cause(java, tmp_path):
    # The line's next stop, at the same depth, before the call returned. Java has
    # no watch on the caller to prove it is the same call: withheld, with a reason that
    # claims no loop's next pass, but says Peeky could not tell, and names the
    # possibilities.
    (tmp_path / "demo/Loops.java").write_text(LOOPS)
    target = JavaTarget(bundle=java.bundle, sources=("demo/Loops.java",), main_class="demo.Loops")
    line = next(i for i, text in enumerate(LOOPS.splitlines(), 1) if "// SUM" in text)
    probe = Probe("demo/Loops.java", line, ("i", RETURN), hits="all")
    result = run_probe(tmp_path, probe, adapter=target)
    assert result["status"] == "completed", result.get("error")
    hits = {}
    for row in result["rows"]:
        hits.setdefault(row["hit"], {})[row["selector"]] = row
    assert [hits[hit]["i"]["value"] for hit in (1, 2, 3)] == ["1", "2", "3"]
    for hit in (1, 2):
        error = hits[hit][RETURN]["error"]
        assert "value" not in hits[hit][RETURN] and error.startswith(
            "withheld: probe a line that runs once per call, such as the function's first. "
            "The line stopped again at the same depth before this call returned, and Peeky "
            "could not tell whether in this call (a loop's next pass) or in a later call after "
            "this one ended without returning"
        ), error
    assert hits[3][RETURN]["value"] == "6"
