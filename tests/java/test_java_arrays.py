"""Java arrays through real java-debug: indexed selectors must never fail the session."""

import os
from pathlib import Path

import pytest

from peeky.probe import Probe, run_probe

SOURCE = """package demo;
public class Arr {
  public static void main(String[] args) {
    int[] values = {2, 4, 6};
    int[] big = new int[600];
    System.out.println(values.length + big.length); // OBSERVE
  }
}
"""


@pytest.mark.timeout(120)
def test_java_array_elements_from_either_end_and_oversize_arrays_fail_per_selector(tmp_path):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a prepared Java adapter bundle")
    from peeky.java import JavaTarget

    (tmp_path / "demo").mkdir()
    (tmp_path / "demo/Arr.java").write_text(SOURCE)
    line = next(i for i, text in enumerate(SOURCE.splitlines(), 1) if "OBSERVE" in text)
    target = JavaTarget(bundle=Path(bundle), sources=("demo/Arr.java",), main_class="demo.Arr")
    capture = ("values[0]", "values[2]", "values[-1]", "values[-3]", "values[-4]", "big[0]")
    result = run_probe(tmp_path, Probe("demo/Arr.java", line, capture), adapter=target)
    assert result["status"] == "completed", result.get("error")
    rows = {row["selector"]: row for row in result["rows"]}
    assert [rows[s]["value"] for s in capture[:4]] == ["2", "6", "6", "2"]
    assert "out of range for 3 elements" in rows["values[-4]"]["error"]
    assert "exceeds bounded expansion limit" in rows["big[0]"]["error"]
    assert "603" in result["output"]


OBJECT = """package demo;
public class Obj {
  static class Point { int x = 3; int[] ys = {7, 8}; Point next; }
  public static void main(String[] args) {
    Point p = new Point();
    p.next = new Point();
    System.out.println(p.x); // OBSERVE
  }
}
"""


@pytest.mark.timeout(120)
def test_java_object_fields_expand_without_failing_the_session(tmp_path):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a prepared Java adapter bundle")
    from peeky.java import JavaTarget

    (tmp_path / "demo").mkdir()
    (tmp_path / "demo/Obj.java").write_text(OBJECT)
    line = next(i for i, text in enumerate(OBJECT.splitlines(), 1) if "OBSERVE" in text)
    target = JavaTarget(bundle=Path(bundle), sources=("demo/Obj.java",), main_class="demo.Obj")
    capture = ("p.x", "p.ys[1]", "p.ys[-1]", "p.next.x", "p.nope")
    result = run_probe(tmp_path, Probe("demo/Obj.java", line, capture), adapter=target)
    assert result["status"] == "completed", result.get("error")
    rows = {row["selector"]: row for row in result["rows"]}
    assert [rows[s]["value"] for s in capture[:4]] == ["3", "8", "8", "3"]
    assert "not found" in rows["p.nope"]["error"]


HUGE = """package demo;
public class Huge {
  static class Request { String body = "x".repeat(5_000_000); int status = 200; }
  public static void main(String[] args) {
    Request req = new Request();
    String text = req.body;
    System.out.println(req.status + text.length()); // OBSERVE
  }
}
"""


@pytest.mark.timeout(120)
def test_multi_megabyte_java_strings_are_bounded_not_fatal(tmp_path):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a prepared Java adapter bundle")
    from peeky.java import JavaTarget

    (tmp_path / "demo").mkdir()
    (tmp_path / "demo/Huge.java").write_text(HUGE)
    line = next(i for i, text in enumerate(HUGE.splitlines(), 1) if "OBSERVE" in text)
    target = JavaTarget(bundle=Path(bundle), sources=("demo/Huge.java",), main_class="demo.Huge")
    result = run_probe(
        tmp_path, Probe("demo/Huge.java", line, ("req.status", "text")), adapter=target
    )
    assert result["status"] == "completed", result.get("error")
    rows = {row["selector"]: row for row in result["rows"]}
    assert rows["req.status"]["value"] == "200"
    assert len(rows["text"]["value"]) <= 2000
    assert "5000200" in result["output"]
