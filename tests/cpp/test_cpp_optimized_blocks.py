"""#173: in an optimized build, a range-for's structured binding withholds the outer
local of its name where LLDB lists only that local, as it does at -O0 (the C cases are
in tests/native/test_native_optimized_blocks.py)."""

import os

import pytest

from peeky.native import NativeTarget
from peeky.probe import Probe, run_probe

CPP_SOURCE = """#include <cstdio>
#include <utility>
#include <vector>
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; }
__attribute__((noinline)) std::vector<std::pair<long, long>> make(long v) {
    return {{v + 1000, 1}, {v + 1001, 2}};
}
int main(int argc, char **argv) {
    long t = argc;
    std::vector<std::pair<long, long>> v = make(t);
    for (auto [t, u] : v) { // HEADER
        use(t + u);
    }
    for (const auto &[t, u] : make(t)) { use(t * u); } // ONE_LINE
    use(t);
    return 0;
}
"""


def target(optimization):
    dap = os.environ.get("PEEKY_NATIVE_DAP")
    if not dap:
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    return NativeTarget(
        sources=("blocks.cpp",),
        language="cpp",
        compiler=os.environ.get("PEEKY_NATIVE_CXX", "clang++"),
        adapter=dap,
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
        optimization=optimization,
    )


def rows(tmp_path, adapter, text, marker, selector):
    name = adapter.sources[0]
    (tmp_path / name).write_text(text)
    line = next(i for i, row in enumerate(text.splitlines(), 1) if marker in row)
    result = run_probe(tmp_path, Probe(name, line, (selector,), hits="all"), adapter=adapter)
    assert result["status"] == "completed", result.get("error")
    return [row for row in result["rows"] if row["selector"] == selector]


@pytest.mark.timeout(180)
@pytest.mark.parametrize("optimization", ["O1", "O2"])
@pytest.mark.parametrize("marker", ["HEADER", "ONE_LINE"])
def test_a_range_for_binding_withholds_the_outer_local_where_lldb_lists_only_it(
    tmp_path, optimization, marker
):
    found = rows(tmp_path, target(optimization), CPP_SOURCE, marker, "t")
    shown = [row for row in found if "value" in row]
    # Before: a stop of the header listed only the outer `t` (argc, 1) as plain `t`.
    assert not shown, shown
    withheld = [row for row in found if "structured binding `t`" in row.get("error", "")]
    assert withheld, found
