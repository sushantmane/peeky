"""#382 on a launched JVM: a probe's `1:pass` reads the caller's loop counter while the
callee runs, and `1:readings[1]` an element of the caller's array, in one run. A caller's
values are never settable."""

import json
import os
from pathlib import Path

import pytest

from peeky.cli import main

WARD = """package demo;

public class Ward {
    static int addReading(int[] totals, int n, int reading) {
        totals[n] = reading; // ADD
        return n + 1;
    }

    public static void main(String[] args) {
        int[] readings = {3, 5, 7};
        int[] totals = new int[3];
        int n = 0;
        for (int pass = 0; pass < readings.length; pass++) {
            n = addReading(totals, n, readings[pass] * 2); // CALL
        }
        System.out.println(n + " " + totals[2]);
    }
}
"""


def line_of(marker):
    return next(i for i, line in enumerate(WARD.splitlines(), 1) if line.endswith(marker))


@pytest.fixture
def ward(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "ward"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "Ward.java").write_text(WARD)
    config = root / "ward.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["demo/Ward.java"],
                "main_class": "demo.Ward",
                "arguments": [],
            }
        )
    )
    return root, config


@pytest.mark.timeout(240)
def test_a_probe_reads_the_callers_pass_beside_the_callees_values(ward, capsys):
    root, config = ward
    add, call = line_of("// ADD"), line_of("// CALL")
    argv = ["probe", root, "--config", config, "--at", f"demo/Ward.java:{add}", "--json"]
    argv += ["--hits", "all", "--capture", "reading,1:pass,1:readings[1]"]
    code = main([str(arg) for arg in argv])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["status"] == "completed", result.get("error")
    assert result["output"].strip() == "3 14"
    rows = {(row["hit"], row["selector"]): row for row in result["rows"]}
    assert [rows[hit, "reading"]["value"] for hit in (1, 2, 3)] == ["6", "10", "14"]
    assert [rows[hit, "1:pass"]["value"] for hit in (1, 2, 3)] == ["0", "1", "2"]
    assert rows[1, "1:readings[1]"]["value"] == "5"
    caller = rows[1, "1:pass"]
    assert (caller["frame"], caller["line"], caller["stop_line"]) == (1, call, add)
    assert "main" in caller["function"] and caller.get("settable") is not True
    assert "frame" not in rows[1, "reading"]
