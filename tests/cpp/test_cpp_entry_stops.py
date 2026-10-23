"""A one-line lambda's parameter at a stop on its line (#279): the value passed, or
withheld where the stop is before the lambda's prologue stores it. A GCC build stops
there (`v` read 0, then 7, for 7 and 8); clang marks the prologue's end, so every value
reads. C++ builds with clang only, so with GCC this test skips: its GCC expectations wait
for #167 (C++ with GCC), and pass with that survey's test-only hatch."""

import json
import os
import subprocess

import pytest

from peeky.cli import main

LAMBDA = """#include <cstdio>
int main() {
    int base = 1;
    auto twice = [](int v) { return v * 2; }; // LAMBDA
    int r = base + twice(7) + twice(8);
    std::printf("r=%d\\n", r);
    return 0;
}
"""
NOTE = (
    "no value yet: the stop is at its function's start, before the prologue stores the "
    "parameters: its storage holds leftovers (the build marks no prologue end, as GCC's never "
    "do, so LLDB stops a one-line function here). Read the arguments at the call, or put the "
    "body on a line of its own"
)


def _is_gcc(compiler):
    try:
        version = subprocess.run([compiler, "--version"], capture_output=True, text=True)
    except OSError:
        return False
    return "Free Software Foundation" in version.stdout


@pytest.mark.timeout(300)
def test_a_one_line_lambdas_parameter_is_the_one_passed_or_withheld(tmp_path, monkeypatch, capsys):
    compiler = os.environ.get("PEEKY_NATIVE_CXX")
    if not os.environ.get("PEEKY_NATIVE_DAP") or not compiler:
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "lambda.cpp").write_text(LAMBDA)
    line = next(i for i, text in enumerate(LAMBDA.splitlines(), 1) if "// LAMBDA" in text)
    command = ["probe", str(tmp_path / "lambda.cpp"), "--at", str(line), "--capture", "v"]
    code = main([*command, "--hits", "all", "--json", "--quiet"])
    result = json.loads(capsys.readouterr().out)
    if code != 0 and (result.get("error") or {}).get("code") == "UNSUPPORTED":
        # Waiting for #167: C++ with GCC is refused until its rules are checked against
        # GCC's debug information.
        pytest.skip(f"waits for #167 (C++ with GCC): {result['error']['message'][:120]}")
    assert code == 0 and "r=31" in result["output"], result
    rows = result["rows"]
    assert [row["hit"] for row in rows] == [1, 2], rows
    for row, passed in zip(rows, (7, 8)):
        if row.get("unavailable"):
            assert row["error"] == NOTE, row
        else:
            assert row["value"] == str(passed), row
    withheld = [row["hit"] for row in rows if row.get("unavailable")]
    assert withheld == ([1, 2] if _is_gcc(compiler) else []), rows
