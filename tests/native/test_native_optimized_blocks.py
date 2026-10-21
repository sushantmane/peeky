"""#173: in an optimized build, a block's own declaration of a name (an enumerator, a
`typedef`, an `extern`, a structured binding) withholds the outer local's row as it
does at -O0. The outer local there lives in a register or as a constant, not in the
frame's memory, and the scan (#144) skipped it: plain `n` showed the outer local's
value where the code names the block's."""

import os

import pytest

from peeky.native import NativeTarget
from peeky.probe import Probe, run_probe

C_SOURCE = """#include <stdio.h>
__attribute__((noinline)) long use(long v) { printf("U=%ld\\n", v); return v; }
int main(int argc, char **argv) {
    long n = argc;
    use(n);
    {
        enum { n = 9 };
        use(n); /* ENUMERATOR */
    }
    {
        typedef int n;
        n k = 4;
        use(k); /* TYPEDEF */
    }
    {
        extern long n;
        use(n); /* EXTERN */
    }
    use(n);
    return 0;
}
long n = 7;
"""

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


def target(language, optimization):
    dap = os.environ.get("PEEKY_NATIVE_DAP")
    if not dap:
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    variable = "PEEKY_NATIVE_CXX" if language == "cpp" else "PEEKY_NATIVE_CC"
    compiler = os.environ.get(variable)
    if language == "cpp" and not compiler:
        pytest.skip("set PEEKY_NATIVE_CXX to enable C++ conformance")
    return NativeTarget(
        sources=(f"blocks.{language}",),
        language=language,
        compiler=compiler or "clang",
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
@pytest.mark.parametrize(
    "marker, reason",
    [
        ("ENUMERATOR", "the code here names the block's enumerator `n` (line 7)"),
        ("TYPEDEF", "the code here names the type `n` the block's `typedef` names (line 11)"),
        ("EXTERN", "the code here names the block's `extern` `n` (line 16)"),
    ],
)
def test_a_blocks_own_declaration_withholds_the_outer_local(tmp_path, optimization, marker, reason):
    found = rows(tmp_path, target("c", optimization), C_SOURCE, marker, "n")
    assert found, "no stop"
    for row in found:
        # Before: `n` showed the outer local's value (argc) as what line 8 reads.
        assert "value" not in row and reason in row["error"], row


@pytest.mark.timeout(180)
@pytest.mark.parametrize("optimization", ["O1", "O2"])
@pytest.mark.parametrize("marker", ["HEADER", "ONE_LINE"])
def test_a_range_for_binding_withholds_the_outer_local_where_lldb_lists_only_it(
    tmp_path, optimization, marker
):
    found = rows(tmp_path, target("cpp", optimization), CPP_SOURCE, marker, "t")
    shown = [row for row in found if "value" in row]
    # Before: a stop of the header listed only the outer `t` (argc, 1) as plain `t`.
    assert not shown, shown
    withheld = [row for row in found if "structured binding `t`" in row.get("error", "")]
    assert withheld, found
