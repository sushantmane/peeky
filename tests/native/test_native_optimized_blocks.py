"""#173: in an optimized build, a block's own declaration of a name (an enumerator, a
`typedef`, an `extern`) withholds the outer local's row as it does at -O0. The outer
local there lives in a register or as a constant, not in the frame's memory, and the
scan (#144) skipped it: plain `n` showed the outer local's value where the code names
the block's. The structured binding case is in tests/cpp/test_cpp_optimized_blocks.py."""

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


def target(optimization):
    dap = os.environ.get("PEEKY_NATIVE_DAP")
    if not dap:
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    return NativeTarget(
        sources=("blocks.c",),
        language="c",
        compiler=os.environ.get("PEEKY_NATIVE_CC", "clang"),
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
    found = rows(tmp_path, target(optimization), C_SOURCE, marker, "n")
    assert found, "no stop"
    for row in found:
        # Before: `n` showed the outer local's value (argc) as what line 8 reads.
        assert "value" not in row and reason in row["error"], row


STATIC_SOURCE = """#include <stdio.h>
__attribute__((noinline)) long id(long v) { return v; }
int main(int argc, char **argv) {
    long t = id(argc) * 3;
    printf("A=%ld\\n", t);
    {
        static long t = 50;
        printf("B=%ld\\n", t + 0 * argc); /* STATIC */
    }
    printf("C=%ld\\n", t);
    return 0;
}
"""


@pytest.mark.timeout(180)
@pytest.mark.parametrize("optimization", ["O1", "O2"])
def test_a_blocks_static_kept_as_a_constant_is_shown_where_the_code_names_it(
    tmp_path, optimization
):
    # Review of PR #175: clang keeps a `static` never changed as a constant, which
    # lldb-dap lists as a local, `t @ blocks.c:7`. Before: that row was withheld as the
    # outer local, and the outer one's said the static was not listed.
    adapter = target(optimization)
    (tmp_path / "blocks.c").write_text(STATIC_SOURCE)
    line = next(i for i, row in enumerate(STATIC_SOURCE.splitlines(), 1) if "STATIC" in row)
    selectors = ("t @ blocks.c:7", "t @ blocks.c:4")
    result = run_probe(tmp_path, Probe("blocks.c", line, selectors), adapter=adapter)
    assert result["status"] == "completed", result.get("error")
    rows = {row["selector"]: row for row in result["rows"] if row.get("phase") == "observed"}
    assert rows["t @ blocks.c:7"].get("value") == "50", rows
    outer = rows["t @ blocks.c:4"]
    assert "value" not in outer, outer
    assert (
        "the block's `static` `t` (line 7), listed separately as `t @ blocks.c:7`"
        in (outer["error"])
    )
