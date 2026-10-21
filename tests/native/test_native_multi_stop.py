"""A C line that stops several times per pass (#157), on real builds: LLDB places a line
breakpoint at the first code of each block on the line, so a macro's own block (`do {
int t_ = ...; } while (0)`) or a one-line loop's body is a location of its own.

Found by the seventh agent evaluation (Rust `movavg`): `--hits all` gave 12 hits for 4
values, with no word that the line stops 3 times per pass.
"""

import json
import os

import pytest

from peeky.cli import main

SWAP = """#include <stdio.h>
#define SWAP(a, b) do { int t_ = (a); (a) = (b); (b) = t_; } while (0)
static int twice(int v) { return v * 2; }
int main(void) {
    int v[4] = {1, 3, 2, 4};
    int total = 0, a = 0, b = 0;
    for (int i = 0; i < 3; i++) { // FOR
        if (v[i] > v[i + 1]) SWAP(v[i], v[i + 1]); // MACRO
        a = twice(i); b = a + 1; // TWO
        total += a + b;
    }
    for (int i = 0; i < 3; i++) { int sq = i * i; total += sq; } // ONE_LINE_FOR
    printf("total=%d v=%d%d%d%d\\n", total, v[0], v[1], v[2], v[3]);
    return 0;
}
"""


@pytest.fixture
def swap(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "swap.c"
    source.write_text(SWAP)
    return source


def line_of(marker):
    return next(i for i, line in enumerate(SWAP.splitlines(), 1) if marker in line)


def probe(source, capsys, line, capture="total,i"):
    command = ["probe", str(source), "--at", str(line), "--capture", capture, "--hits", "all"]
    assert main([*command, "--json", "--quiet"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed", result
    return result


def per_pass(result):
    return [w for w in result["warnings"] if "times per pass" in w]


@pytest.mark.timeout(180)
def test_a_line_with_a_blocks_own_code_says_it_stops_more_than_once_per_pass(swap, capsys):
    # The macro's block is a location of its own: a pass that swaps stops twice there.
    line = line_of("// MACRO")
    result = probe(swap, capsys, line)
    assert result["output"].startswith("total=20 v=1234")
    assert result["hits"] == 4
    (warning,) = per_pass(result)
    assert warning.startswith(
        f"Line {line} stops up to 2 times per pass in main (2 breakpoint locations): --hits, "
        "--set-hit and --set-at's @HIT count stops, not passes"
    ), warning
    # i = 1 swaps 3 and 2: its pass stops at the condition, then inside SWAP's block.
    seen = [
        (row["hit"], row["stop"], row["value"]) for row in result["rows"] if row["selector"] == "i"
    ]
    assert seen == [(1, 1, "0"), (2, 1, "1"), (3, 2, "1"), (4, 1, "2")]
    assert {row["stops"] for row in result["rows"]} == {2}
    # The evidence keeps each hit's stop, and queries wide shows it.
    assert main(["sql", "SELECT DISTINCT hit, stop FROM last_run", "--json"]) == 0
    stored = json.loads(capsys.readouterr().out)["rows"]
    assert [(row["hit"], row["stop"]) for row in stored] == [(1, 1), (2, 1), (3, 2), (4, 1)]
    assert main(["queries", "wide", "--json"]) == 0
    wide = json.loads(capsys.readouterr().out)
    assert wide["columns"][:3] == ["hit", "stop", "phase"]
    assert [(row["hit"], row["stop"], row["i"]) for row in wide["rows"]] == [
        (1, 1, "0"),
        (2, 1, "1"),
        (3, 2, "1"),
        (4, 1, "2"),
    ]
    # The table says it too: a stop column, and the warning.
    assert main(["probe", str(swap), "--at", str(line), "--capture", "i", "--hits", "all"]) == 0
    captured = capsys.readouterr()
    table = captured.out.splitlines()
    assert table[0].split()[:3] == ["hit", "stop", "phase"]
    assert [entry.split()[1] for entry in table[1:5]] == ["1/2", "1/2", "2/2", "1/2"]
    assert f"warning: Line {line} stops up to 2 times per pass in main" in captured.out


@pytest.mark.timeout(180)
def test_a_one_line_loop_and_two_statements(swap, capsys):
    # A one-line `for` with a block: its header's code runs once, its body's each pass.
    line = line_of("// ONE_LINE_FOR")
    result = probe(swap, capsys, line, "total")
    assert result["hits"] == 4 and len(per_pass(result)) == 1
    assert [(row["stop"], row["stops"]) for row in result["rows"]] == [
        (1, 2),
        (2, 2),
        (2, 2),
        (2, 2),
    ]
    # Two statements in one block are one location: one stop per pass, no warning.
    result = probe(swap, capsys, line_of("// TWO"))
    assert result["hits"] == 3 and not per_pass(result)
    assert not any("stop" in row or "stops" in row for row in result["rows"])
    # A loop header with one location stops at its first code, before the loop: once.
    result = probe(swap, capsys, line_of("// FOR"), "total")
    assert result["hits"] == 1 and not per_pass(result)
    assert "stop" not in result["rows"][0]


@pytest.mark.timeout(180)
def test_session_state_lines_say_which_stop(swap, capsys):
    line = line_of("// MACRO")
    assert main(["session", "start", str(swap), "--at", str(line), "--json"]) == 0
    state = json.loads(capsys.readouterr().out)
    try:
        assert (state["hit"], state["place"], state["places"]) == (1, 1, 2), state
        assert any("stops up to 2 times per pass" in w for w in state["warnings"])
        for hit, place in ((2, 1), (3, 2)):
            assert main(["session", "continue"]) == 0
            assert f"hit {hit}, stop {place} of 2" in capsys.readouterr().out
        assert main(["session", "inspect", "--json"]) == 0
        snapshot = json.loads(capsys.readouterr().out)
        assert (snapshot["hit"], snapshot["place"], snapshot["places"]) == (3, 2, 2)
        assert main(["session", "inspect"]) == 0
        assert "hit 3, stop 2 of 2" in capsys.readouterr().out
    finally:
        main(["session", "close"])
        capsys.readouterr()
