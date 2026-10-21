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
        "caveats",
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


LOOP = """#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
  int total = 0, i = 1;
  do {
    int k = total % 7;
    const char *name = argv[i];
    int x = i + 1, X = 100 + i;
    total += k + x + X + atoi(name);
    i++;
  } while (i < argc);
  printf("TOTAL:%d\\n", total);
  return 0;
}
"""


@pytest.mark.timeout(240)
def test_wide_and_join_keep_notes_case_and_addresses_apart(bins, capsys):
    # Review of #139 (PR #145, round 1).
    (bins / "bins.c").write_text(LOOP)

    def probe(line, capture, *args):
        argv = ["probe", str(bins), "--at", f"bins.c:{line}", "--capture", capture]
        code = main([*argv, "--hits", "all", "--quiet", "--", *args])
        assert code == 0, capsys.readouterr().out
        capsys.readouterr()

    # Line 6 is a do body's first line: `k` shows a value Peeky cannot vouch for (the last
    # pass's, or leftovers), which the probe marks [note]. wide and join showed it plain.
    probe(6, "k,i", "5", "6")
    probe(6, "k,i", "6", "5")
    wide = queried(capsys, "wide", "--param", "run=1")
    assert [json.loads(row["caveats"]) for row in wide["rows"]] == [{"k": "note"}] * 2
    assert all(row["k"] is not None for row in wide["rows"])
    stored = queried(capsys, "run", "--param", "run=1")["rows"]
    assert all("loop" in row["note"] for row in stored if row["selector"] == "k")
    joined = queried(capsys, "join", "--param", "a=1", "--param", "b=2", "--param", "key=k")
    assert joined["rows"]
    for row in joined["rows"]:
        for side in ("a", "b"):
            if row[f"hit_{side}"] is not None:
                assert json.loads(row[f"caveats_{side}"]) == {"k": "note"}, row

    # `x` and `X` are one name to SQLite: join's `a:X` held x's values.
    probe(9, "name,x,X", "5", "6")
    probe(9, "name,x,X", "6", "5")
    wide = queried(capsys, "wide", "--param", "run=3")
    assert wide["columns"][3:6] == ["name", "x", "X (2)"]
    assert [(row["x"], row["X (2)"]) for row in wide["rows"]] == [("2", "101"), ("3", "102")]
    assert main(["sql", f'SELECT "x", "X (2)" FROM ({wide["sql"]})', "--json"]) == 0
    refined = json.loads(capsys.readouterr().out)["rows"]
    assert refined == [{"x": "2", "X (2)": "101"}, {"x": "3", "X (2)": "102"}]

    # A C string's address differs between runs: said, and maskable.
    params = ("--param", "a=3", "--param", "b=4", "--param", "key=name")
    exact = queried(capsys, "join", *params)
    assert {row["matched"] for row in exact["rows"]} == {"only in run a", "only in run b"}
    assert "--param masked=1" in exact["notes"][0]
    masked = queried(capsys, "join", *params, "--param", "masked=1")
    shown = [
        (row["name"], row["hit_a"], row["hit_b"], row["a:x"], row["a:X (2)"], row["b:x"])
        for row in masked["rows"]
    ]
    assert shown == [('0x… "5"', 1, 2, "2", "101", "3"), ('0x… "6"', 2, 1, "3", "102", "2")]
    assert all(row["matched"] == "both" and row["b:X (2)"] for row in masked["rows"])


LEFTOVERS = """#include <stdio.h>
#include <stdlib.h>
static long run(int n) {
  long total = 0;
  do {
    int k = n * 2; long v = k + 1; // FIRST
    total += v;
    n--;
  } while (n > 0);
  return total;
}
static long warm(long seed) {
  volatile long pad[16];
  for (int j = 0; j < 16; j++) pad[j] = seed + j;
  return pad[0];
}
int main(int argc, char **argv) {
  long seed = atol(argv[1]);
  char buf[32] = {0};
  snprintf(buf, sizeof buf, "0x%016lx", (unsigned long)seed * 0x100000001UL);
  const char *name = buf;
  warm(seed);
  long total = run(3); // TEXT
  printf("TOTAL:%ld %d\\n", total, name[0] == '0');
  return 0;
}
"""


@pytest.mark.timeout(240)
def test_diff_hits_and_values_never_count_leftovers_or_quoted_hex(bins, capsys):
    # Review 2 of #139 (PR #145). Line FIRST is a do body's first line: at hit 1, `k` and
    # `v` hold leftovers (here `warm`'s, so they differ with the seed), which the probe
    # marks [note]. diff listed them as the first divergences, note NULL, though the
    # runs computed and printed the same; hits and values counted them.
    (bins / "bins.c").write_text(LEFTOVERS)
    lines = {
        marker: next(i for i, text in enumerate(LEFTOVERS.splitlines(), 1) if marker in text)
        for marker in ("FIRST", "TEXT")
    }

    def probe(marker, capture, seed, *extra):
        argv = ["probe", str(bins), "--at", f"bins.c:{lines[marker]}", "--capture", capture]
        assert main([*argv, "--hits", "all", *extra, "--quiet", "--", seed]) == 0
        capsys.readouterr()

    probe("FIRST", "k,v,n", "1000")
    probe("FIRST", "k,v,n", "4000")
    diff = queried(capsys, "diff")["rows"]
    assert [row["selector"] for row in diff if row["selector"].startswith("(")] == ["(args)"]
    values = [row for row in diff if not row["selector"].startswith("(")]
    assert values and {(row["hit"], row["selector"]) for row in values} <= {
        (1, "k"),
        (1, "v"),
    }, diff
    for row in values:
        assert row["note"].startswith("qualified (run a: note, run b: note)"), row
    hits = queried(capsys, "hits", "--param", "run=1", "--param", "selector=v")["rows"]
    assert [(row["caveat"], row["changed"]) for row in hits] == [("note", None)] * 3
    seen = queried(capsys, "values", "--param", "selector=v")["rows"]
    assert all(row["qualified"] == row["seen"] for row in seen), seen

    # `buf` holds hex text, no address; `name` points at it. Masking pairs neither.
    probe("TEXT", "buf,name", "1000")
    probe("TEXT", "buf,name", "4000")
    params = ("--param", "a=3", "--param", "b=4")
    buf = queried(capsys, "join", *params, "--param", "key=buf")
    assert "notes" not in buf
    for key in ("buf", "name"):
        masked = queried(capsys, "join", *params, "--param", f"key={key}", "--param", "masked=1")
        matched = [row["matched"] for row in masked["rows"]]
        assert matched == ["only in run a", "only in run b"], masked
    diff = queried(capsys, "diff", *params)["rows"]
    assert {row["selector"]: row["note"] for row in diff if row["hit"] == 1} == {
        "buf": None,
        "name": None,
    }

    # Review 3: a noted value's row said only `missing in run b` (fewer hits) or
    # `intervention only in run a` (a --set), and came first.
    probe("FIRST", "k,v,n", "1000", "--hits", "2")
    probe("FIRST", "k,v,n", "1000", "--set", "n=1", "--set-hit", "2")
    for a, b, said in ((1, 5, "missing in run b"), (6, 1, "intervention only in run a")):
        diff = queried(capsys, "diff", "--param", f"a={a}", "--param", f"b={b}")["rows"]
        values = [row for row in diff if not row["selector"].startswith("(")]
        noted = [row for row in values if row["selector"] in ("k", "v")]
        # A plain value's row first (`n`: a hit one run did not reach).
        assert values[0]["selector"] == "n" and "qualified" not in values[0]["note"], diff
        assert any(row["note"].startswith(said + "; qualified (run a: note") for row in noted)
        assert all("qualified (" in (row["note"] or "") for row in noted), diff
