"""#382, on real builds: a probe's `1:pass` reads main's `pass` while add_reading runs, beside
add_reading's own values, in one run. A caller's local is withheld wherever `session get
--frame 1` withholds it: below a call in its declaration, or a loop's own variable."""

import json
import os

import pytest

from peeky.cli import main

PROGRAM = """#include <stdio.h>
static int twice(int v) {
    int r = v * 2;
    return r; /* TWICE */
}
static int add_reading(int *totals, int n, int reading) {
    totals[n] = reading; /* ADD */
    return n + 1;
}
int main(void) {
    int totals[4];
    int n = 0;
    int pass = 0;
    while (pass < 3) {
        n = add_reading(totals, n, pass * 10); /* CALL */
        pass++;
    }
    long result = twice(n); /* RESULT */
    for (int i = 0; i < 2; i++) twice(i); /* LOOP */
    printf("%d %ld %d\\n", n, result, totals[2]);
    return 0;
}
"""


def line_of(marker):
    return next(i for i, line in enumerate(PROGRAM.splitlines(), 1) if marker in line)


@pytest.fixture
def source(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "ward.c"
    path.write_text(PROGRAM)
    return path


def probe(capsys, *argv):
    code = main(["probe", *map(str, argv), "--hits", "all", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["status"] == "completed", result.get("error")
    assert result["output"].strip() == "3 6 20"
    return {(row["hit"], row["selector"]): row for row in result["rows"]}, result


@pytest.mark.timeout(300)
def test_a_probe_reads_the_callers_pass_beside_the_callees_values(source, capsys):
    add, call = line_of("/* ADD */"), line_of("/* CALL */")
    rows, result = probe(capsys, source, "--at", add, "--capture", "reading,1:pass,1:n,9:x")
    assert [rows[hit, "reading"]["value"] for hit in (1, 2, 3)] == ["0", "10", "20"]
    assert [rows[hit, "1:pass"]["value"] for hit in (1, 2, 3)] == ["0", "1", "2"]
    # The assignment `n = add_reading(...)` hasn't stored the call's result yet.
    assert [rows[hit, "1:n"]["value"] for hit in (1, 2, 3)] == ["0", "1", "2"]
    for hit in (1, 2, 3):
        caller = rows[hit, "1:pass"]
        assert (caller["frame"], caller["function"], caller["line"]) == (1, "main", call)
        assert caller["stop_line"] == add and caller.get("settable") is not True
        assert "note" not in caller and "frame" not in rows[hit, "reading"]
        assert rows[hit, "9:x"]["error"].startswith("Frame 9 is out of range"), rows[hit, "9:x"]
    assert [hit["line"] for hit in result["by_hit"]] == [add] * 3


@pytest.mark.timeout(300)
def test_a_callers_local_is_withheld_where_a_session_withholds_it(source, capsys):
    at, declared, loop = line_of("/* TWICE */"), line_of("/* RESULT */"), line_of("/* LOOP */")
    rows, _ = probe(capsys, source, "--at", at, "--capture", "v,1:result,1:i")
    assert [rows[hit, "v"]["value"] for hit in (1, 2, 3)] == ["3", "0", "1"]
    # Hit 1 is in `long result = twice(n);`: main stores `result` only after the call.
    first = rows[1, "1:result"]
    assert first["unavailable"] is True and first["line"] == declared, first
    assert first["error"].startswith("no value yet: read it after the call returns"), first
    assert "not found" in rows[1, "1:i"]["error"]
    # Hits 2 and 3 are calls from the `for` loop's body: `result` is stored, and the loop's
    # own variable is withheld.
    for hit in (2, 3):
        assert rows[hit, "1:result"]["value"] == "6" and rows[hit, "1:result"]["line"] == loop
        assert rows[hit, "1:i"]["unavailable"] is True
        assert rows[hit, "1:i"]["error"].startswith("no value yet"), rows[hit, "1:i"]
    # The same stop read by a session's `get --frame 1` says the same.
    assert main(["session", "start", str(source), "--at", str(at)]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "get", "--frame", "1", "result", "--json"]) == 1
        (read,) = json.loads(capsys.readouterr().out)
        assert (read["error"], read["unavailable"]) == (first["error"], True)
    finally:
        main(["session", "close"])
