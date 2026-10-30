"""The eleventh agent evaluation's C++ Lows on real builds: a GCC lambda's capture said why
it was not found (prep 8); it is read through `__closure` now (#361)."""

import json
import os

import pytest

from peeky.cli import main

LAMBDA = """#include <algorithm>
#include <cstdio>
#include <map>
#include <string>
#include <vector>

int main() {
    std::map<std::string, int> grade{{"Ana", 3}, {"Bo", 5}};
    std::vector<std::string> names{"Ana", "Bo"};
    int bonus = 2;
    std::sort(names.begin(), names.end(), [&grade, bonus](const std::string &a, const std::string &b) {
        int ga = grade[a] + bonus;  // IN_LAMBDA
        return ga > grade[b];
    });
    std::printf("%s\\n", names[0].c_str());
    return 0;
}
"""


@pytest.fixture
def directory(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


@pytest.mark.timeout(300)
def test_cpp_a_lambdas_capture_is_read(directory, cxx, capsys):
    path = directory / "lambda.cpp"
    path.write_text(LAMBDA)
    line = next(n for n, text in enumerate(LAMBDA.splitlines(), 1) if "IN_LAMBDA" in text)
    code = main(
        ["probe", str(path), "--at", str(line), "--capture", "grade,bonus,a", "--json", "--quiet"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result
    rows = {row["selector"]: row for row in result["rows"] if row["hit"] == 1}
    # The lambda's parameter is listed.
    assert not str(rows["a"].get("error", "")).startswith("not found"), rows
    if cxx == "gcc":
        # Before prep 8: `not found; visible: a, b, ga`, with no reason; before #361, `not
        # found: 'grade' is a capture of this lambda, which GCC passes through `__closure`,
        # and lldb-dap doesn't list it ...`. Read through `__closure` now: `std::sort` calls
        # a copy of the lambda's object, which no listed frame holds, so both are marked.
        assert rows["bonus"]["value"] == "2", rows
        assert rows["grade"]["type"].startswith("std::map<"), rows
        assert rows["grade"]["debugger_type"].endswith("&"), rows
        for name in ("grade", "bonus"):
            assert rows[name]["view"] == "unproven", rows
            assert rows[name]["note"].startswith("read through `__closure`: "), rows
    else:
        # Clang lists a capture as a member of the lambda's `this`: no GCC note.
        assert all("__closure" not in json.dumps(rows[n]) for n in ("grade", "bonus")), rows
