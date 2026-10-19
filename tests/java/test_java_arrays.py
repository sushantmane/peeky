"""Java arrays through real java-debug: indexed selectors must never fail the session."""

import os
import re
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


MERGE = 'package demo;\n\nimport java.util.ArrayList;\nimport java.util.List;\n\npublic class Merge {\n    static class Interval {\n        int start;\n        int end;\n        String label;\n        Interval next;\n        Interval(int start, int end) { this.start = start; this.end = end; this.label = "i" + start; }\n    }\n\n    public static void main(String[] args) {\n        List<Interval> merged = new ArrayList<>();\n        merged.add(new Interval(1, 10));\n        merged.add(new Interval(12, 15));\n        Interval current = merged.get(0);\n        Interval none = null;\n        int[] counts = {1, 2, 3};\n        String name = "merge";\n        System.out.println(merged.size() + " " + current.end + " " + counts.length + name + none); // HERE\n    }\n}\n'


@pytest.mark.timeout(120)
def test_java_objects_show_their_fields_and_lists_index_like_arrays(tmp_path):
    # #78 (eval 3, Java): values read `Merge$Interval@172`, so every field needed its own
    # selector, and `merged[0]` failed on an ArrayList's raw fields.
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a prepared Java adapter bundle")
    from peeky.java import JavaTarget

    (tmp_path / "demo").mkdir()
    (tmp_path / "demo/Merge.java").write_text(MERGE)
    line = next(i for i, text in enumerate(MERGE.splitlines(), 1) if "HERE" in text)
    target = JavaTarget(bundle=Path(bundle), sources=("demo/Merge.java",), main_class="demo.Merge")
    capture = ("current", "merged", "counts", "merged[0]", "merged[1].end", "merged[-1]")
    capture += ("merged[2]", "name", "none")
    result = run_probe(tmp_path, Probe("demo/Merge.java", line, capture), adapter=target)
    assert result["status"] == "completed", result.get("error")
    rows = {row["selector"]: row for row in result["rows"]}
    value = {name: row.get("value") for name, row in rows.items()}
    assert value["current"].endswith(' {end=10, label="i1", next=null, start=1}')
    assert (
        value["merged"].startswith("ArrayList@") and " size=2 [Merge$Interval@" in value["merged"]
    )
    assert value["counts"].endswith(" [1, 2, 3]") and value["counts"].startswith("int[3]@")
    assert value["merged[0]"] == value["current"]
    assert value["merged[1].end"] == "15"
    assert value["merged[-1]"].endswith(' {end=15, label="i12", next=null, start=12}')
    assert "out of range: the list has 2 elements" in rows["merged[2]"]["error"]
    assert (value["name"], value["none"]) == ('"merge"', "null")


LISTS = """package demo;
import java.util.ArrayList;
import java.util.List;
public class Lists {
  static class P { int v; P(int v) { this.v = v; } }
  public static void main(String[] args) {
    List<P> many = new ArrayList<>();
    for (int i = 0; i < 200; i++) many.add(new P(i));
    List<P> roomy = new ArrayList<>(1000);
    roomy.add(new P(7));
    int[] counts = new int[500];
    Object[] slots = new Object[40];
    for (int pass = 0; pass < 3; pass++) {
      if (pass == 1) for (int i = 0; i < 20; i++) slots[i] = new P(i);
      slots[30] = many.get(5);
      System.out.println(pass + " " + counts.length); // OBSERVE
    }
  }
}
"""


@pytest.mark.timeout(120)
def test_java_previews_depend_only_on_the_value_and_lists_read_within_bounds(tmp_path):
    # Review of #87: a preview depended on the siblings listed before it; a list whose
    # backing array holds over 512 slots, or a node budget cut short by earlier
    # selectors, failed list indexing.
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a prepared Java adapter bundle")
    from peeky.java import JavaTarget

    (tmp_path / "demo").mkdir()
    (tmp_path / "demo/Lists.java").write_text(LISTS)
    line = next(i for i, text in enumerate(LISTS.splitlines(), 1) if "OBSERVE" in text)
    target = JavaTarget(bundle=Path(bundle), sources=("demo/Lists.java",), main_class="demo.Lists")
    capture = ("slots[30]", "counts[400]", "many[127]", "many[-1].v", "roomy[0]")
    result = run_probe(tmp_path, Probe("demo/Lists.java", line, capture, hits=3), adapter=target)
    assert result["status"] == "completed", result.get("error")
    by = {(row["hit"], row["selector"]): row for row in result["rows"]}
    slot = {by[hit, "slots[30]"]["value"] for hit in (1, 2, 3)}
    assert len(slot) == 1 and slot.pop().endswith(" {v=5}")
    assert (
        by[1, "many[127]"]["value"].endswith(" {v=127}") and by[1, "many[-1].v"]["value"] == "199"
    )
    assert "backing array holds more than 512 slots" in by[1, "roomy[0]"]["error"]


BUDGET = """package demo;
import java.util.ArrayList;
import java.util.List;
public class Bud {
  static class P { int a = 1, b = 2, c = 3, d = 4, e = 5; char q = '"'; String s = "say \\"hi\\" @3"; }
  public static void main(String[] args) {
    int[] arr = new int[506];
    P p = new P();
    List<Integer> xs = new ArrayList<>();
    xs.add(1);
    xs.add(2);
    System.out.println(arr.length + p.a + xs.size()); // OBSERVE
  }
}
"""


@pytest.mark.timeout(120)
def test_a_java_preview_after_selectors_that_used_the_budget_is_complete(tmp_path):
    # Review of #87: an object preview cut short by the node budget showed `{a=1}`.
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a prepared Java adapter bundle")
    from peeky.java import JavaTarget

    (tmp_path / "demo").mkdir()
    (tmp_path / "demo/Bud.java").write_text(BUDGET)
    line = next(i for i, text in enumerate(BUDGET.splitlines(), 1) if "OBSERVE" in text)
    target = JavaTarget(bundle=Path(bundle), sources=("demo/Bud.java",), main_class="demo.Bud")
    capture = ("arr[460]", "p", "xs")
    result = run_probe(tmp_path, Probe("demo/Bud.java", line, capture), adapter=target)
    assert result["status"] == "completed", result.get("error")
    values = [row["value"] for row in result["rows"]]
    # A char and a String are Java literals (java-debug writes both unescaped).
    expected = ' {a=1, b=2, c=3, d=4, e=5, q=\'"\', s="say \\"hi\\" @3"}'
    assert values[1].endswith(expected), values[1]
    # java-debug shows boxed Integers as objects (`Integer@69`).
    assert re.fullmatch(r"ArrayList@[0-9]+ size=2 \[Integer@[0-9]+, Integer@[0-9]+\]", values[2])
