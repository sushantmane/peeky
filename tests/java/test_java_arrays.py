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
