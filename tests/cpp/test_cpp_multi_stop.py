"""A C++ range `for` header stops more than once per pass (#157), on a real build: its
range's code runs once, before the loop, and each element's binding at every pass, in
blocks of their own, so LLDB places a location at each."""

import json
import os

import pytest

from peeky.cli import main

RANGE = """#include <cstdio>
#include <vector>
int main() {
    std::vector<int> v{4, 5, 6};
    long total = 0;
    for (int x : v) { // RANGE_FOR
        total += x;
    }
    auto add = [&](int y) { total += y; }; add(1); add(2); // LAMBDA
    std::printf("total=%ld\\n", total);
    return 0;
}
"""


@pytest.fixture
def ranged(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "range.cpp"
    source.write_text(RANGE)
    return source


def line_of(marker):
    return next(i for i, line in enumerate(RANGE.splitlines(), 1) if marker in line)


def probe(source, capsys, line):
    command = ["probe", str(source), "--at", str(line), "--capture", "total", "--hits", "all"]
    assert main([*command, "--json", "--quiet", "--no-save"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed" and result["output"].strip() == "total=18", result
    return result


@pytest.mark.timeout(180)
def test_a_range_for_header_stops_once_before_the_loop_and_at_each_pass(ranged, capsys):
    line = line_of("// RANGE_FOR")
    result = probe(ranged, capsys, line)
    assert result["hits"] == 4
    (warning,) = [w for w in result["warnings"] if "times per pass" in w]
    assert warning.startswith(
        f"Line {line} stops up to 2 times per pass in main (2 breakpoint locations)"
    ), warning
    assert [(row["stop"], row["stops"], row["value"]) for row in result["rows"]] == [
        (1, 2, "0"),
        (2, 2, "0"),
        (2, 2, "4"),
        (2, 2, "9"),
    ]
    # A lambda on a line is another function, not another stop: main has one location
    # there (both calls are in one block) and stops once; the lambda at each call.
    result = probe(ranged, capsys, line_of("// LAMBDA"))
    assert result["hits"] == 3
    assert not [w for w in result["warnings"] if "times per pass" in w]
    assert any("stopped in 2 functions" in w for w in result["warnings"])
    assert not any("stop" in row for row in result["rows"])
