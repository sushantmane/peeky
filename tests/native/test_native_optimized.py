"""#12/#13: optimized-out locals are reported as unavailable, never as values."""

import os

import pytest

from peeky.native import NativeTarget
from peeky.probe import Probe, run_probe

SOURCE = """#include <stdio.h>

__attribute__((noinline)) static int mix(int a, int b) {
    int t = a * 3 + b; /* BEFORE_T: at -O2, t and r have no location yet */
    printf("t %d\\n", t);
    int r = t ^ (b << 2); /* AFTER_LAST_USE_OF_A: a is dead and its register clobbered */
    printf("mix %d\\n", r);
    return r;
}

int main(int argc, char **argv) {
    int total = 0;
    for (int i = 0; i < 3; i++) {
        total += mix(i + argc, i * 2);
    }
    printf("TOTAL %d\\n", total);
    return 0;
}
"""


def target(language, optimization):
    dap = os.environ.get("PEEKY_NATIVE_DAP")
    if not dap:
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    variable = "PEEKY_NATIVE_CXX" if language == "cpp" else "PEEKY_NATIVE_CC"
    compiler = os.environ.get(variable)
    if language == "cpp" and not compiler:
        pytest.skip("set PEEKY_NATIVE_CXX to enable C++ conformance")
    return NativeTarget(
        sources=(f"opt.{'cpp' if language == 'cpp' else 'c'}",),
        language=language,
        compiler=compiler or ("clang++" if language == "cpp" else "clang"),
        adapter=dap,
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
        optimization=optimization,
    )


@pytest.mark.parametrize("language", ["c", "cpp"])
def test_optimized_out_locals_are_unavailable_and_cannot_be_changed(tmp_path, language):
    adapter = target(language, "O2")
    name = adapter.sources[0]
    (tmp_path / name).write_text(SOURCE)
    line = next(i for i, text in enumerate(SOURCE.splitlines(), 1) if "BEFORE_T" in text)
    result = run_probe(tmp_path, Probe(name, line, ("a", "b", "t", "r")), adapter=adapter)
    assert result["status"] == "completed", result.get("error")
    assert "-O2" in result["target_identity"]["compiler_options"]
    rows = {row["selector"]: row for row in result["rows"]}
    for row in rows.values():
        assert ("value" in row) != bool(row.get("unavailable")), row
        if row.get("unavailable"):
            assert "optimized out" in row["error"] and "value" not in row
    assert rows["t"].get("unavailable") and rows["r"].get("unavailable"), rows
    assert any("no value at this line" in warning for warning in result["warnings"])
    assert result["optimized"] == "-O2"
    assert any("can still be stale or wrong" in warning for warning in result["warnings"])
    refused = run_probe(tmp_path, Probe(name, line, ("t",), (("t", "5"),)), adapter=adapter)
    assert refused["status"] == "failed" and refused["error"]["code"] == "MUTATION_REJECTED"


@pytest.mark.parametrize("language", ["c", "cpp"])
def test_unoptimized_builds_remain_the_default_and_show_values(tmp_path, language):
    adapter = target(language, "O0")
    name = adapter.sources[0]
    (tmp_path / name).write_text(SOURCE)
    line = next(i for i, text in enumerate(SOURCE.splitlines(), 1) if "AFTER_LAST" in text)
    result = run_probe(tmp_path, Probe(name, line, ("a", "t"), (("a", "5"),)), adapter=adapter)
    assert result["status"] == "completed", result.get("error")
    assert all("value" in row for row in result["rows"]) and "optimized" not in result
    assert result["interventions"][0]["after"] == "5"
    with pytest.raises(ValueError, match="optimization"):
        target(language, "O3")
