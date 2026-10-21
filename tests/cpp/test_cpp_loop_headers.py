"""C++ loop headers (#161), on a real build: a counted `for` stops only before its loop
and says so; a range `for` binds its element at each pass (a location in the body's
block, #157), so it stops at every pass and gets no such note."""

import json
import os

import pytest

from peeky.cli import main

LOOPS = """#include <cstdio>
#include <vector>
int main() {
    std::vector<int> v{4, 5, 6};
    long total = 0;
    for (int i = 0; i < 3; i++) { // FOR
        total += i;
    }
    for (int x : v) { // RANGE_FOR
        total += x;
    }
    std::printf("total=%ld\\n", total);
    return 0;
}
"""


@pytest.fixture
def loops(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "loops.cpp"
    source.write_text(LOOPS)
    return source


def probe(source, capsys, marker):
    line = next(i for i, text in enumerate(LOOPS.splitlines(), 1) if f"// {marker}" in text)
    command = ["probe", str(source), "--at", str(line), "--capture", "total", "--hits", "all"]
    assert main([*command, "--json", "--quiet", "--no-save"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed" and "total=18" in result["output"], result
    return line, result


@pytest.mark.timeout(180)
def test_a_counted_for_says_it_stops_before_its_loop_and_a_range_for_does_not(loops, capsys):
    line, result = probe(loops, capsys, "FOR")
    assert result["hits"] == 1
    assert f"Line {line} is a loop header: it stops once each time its loop starts" in "".join(
        result["warnings"]
    ), result["warnings"]
    line, result = probe(loops, capsys, "RANGE_FOR")
    assert result["hits"] == 4
    assert not [w for w in result["warnings"] if "loop header" in w], result["warnings"]
    assert [row["stop"] for row in result["rows"]] == [1, 2, 2, 2]
