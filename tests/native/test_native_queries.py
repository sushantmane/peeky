"""`queries wide` and `queries join` over probes of a real C program (#139)."""

import json
import os

import pytest

from peeky.cli import main

BINS = """#include <stdio.h>
#include <stdlib.h>
struct bin { int lo; int hi; };
int main(int argc, char **argv) {
  struct bin bins[4] = {{0, 1}, {1, 2}, {2, 3}, {3, 4}};
  int total = 0;
  for (int i = 1; i < argc; i++) {
    int v = atoi(argv[i]);
    total += v * bins[v % 4].hi;
  }
  printf("TOTAL:%d\\n", total);
  return 0;
}
"""


@pytest.fixture
def bins(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "bins"
    root.mkdir()
    (root / "bins.c").write_text(BINS)
    return root


def queried(capsys, *argv):
    code = main(["queries", *argv, "--json"])
    output = capsys.readouterr().out
    assert code == 0, output
    return json.loads(output)


@pytest.mark.timeout(180)
def test_wide_and_join_read_probes_of_a_c_program(bins, capsys):
    capture = ("--capture", "v,total,bins[3].hi", "--hits", "all")
    for args in (("7", "2", "5"), ("2", "7", "7", "8")):
        code = main(["probe", str(bins), "--at", "bins.c:9", *capture, "--quiet", "--", *args])
        assert code == 0, capsys.readouterr().out
    capsys.readouterr()

    wide = queried(capsys, "wide", "--param", "run=1")
    assert wide["columns"] == [
        "hit",
        "phase",
        "line",
        "v",
        "total",
        "bins[3].hi",
        "no_value",
        "views",
    ]
    rows = [(row["hit"], row["v"], row["total"], row["bins[3].hi"]) for row in wide["rows"]]
    assert rows == [(1, "7", "0", "4"), (2, "2", "28", "4"), (3, "5", "34", "4")]
    assert {row["phase"] for row in wide["rows"]} == {"observed"}
    assert {row["line"] for row in wide["rows"]} == {9}
    assert all(row["no_value"] is None for row in wide["rows"])

    joined = queried(capsys, "join", "--param", "a=1", "--param", "b=2", "--param", "key=v")
    assert joined["columns"][:3] == ["v", "hit_a", "hit_b"]
    shown = [
        (row["v"], row["hit_a"], row["hit_b"], row["a:total"], row["b:total"], row["matched"])
        for row in joined["rows"]
    ]
    # Run 2 saw 7 at two hits: both pair with run 1's.
    assert shown == [
        ("7", 1, 2, "0", "6", "both"),
        ("7", 1, 3, "0", "34", "both"),
        ("2", 2, 1, "28", "0", "both"),
        ("5", 3, None, "34", None, "only in run a"),
        ("8", None, 4, None, "62", "only in run b"),
    ]

    # The plain table, and the SQL it ran reused through peeky sql.
    assert main(["queries", "wide", "--param", "run=2"]) == 0
    text = capsys.readouterr().out
    assert "bins[3].hi" in text and "62" in text
    assert main(["sql", joined["sql"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == joined["rows"]
