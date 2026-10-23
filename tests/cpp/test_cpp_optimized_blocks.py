"""#173: in an optimized build, a range-for's structured binding withholds the outer
local of its name where LLDB lists only that local, as it does at -O0 (the C cases are
in tests/native/test_native_optimized_blocks.py). #283: on a one-line range-for whose
range reads the outer local, that local's note says so, at -O0 too. #286: so does any
one-line statement whose header names it before the line declares the name again."""

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
    tmp_path, cxx, optimization, marker
):
    found = rows(tmp_path, target(optimization), CPP_SOURCE, marker, "t")
    shown = [row for row in found if "value" in row]
    # Before: a stop of the header listed only the outer `t` (argc, 1) as plain `t`.
    assert not shown, shown
    if cxx == "gcc":
        # LLDB lists both of GCC's `t`s here, so plain `t` names neither, and names both
        # (#167); the outer one, by its line, is withheld as clang's is.
        line = next(i for i, row in enumerate(CPP_SOURCE.splitlines(), 1) if marker in row)
        for row in found:
            assert row["error"].startswith("not found; visible: "), row
            assert f"t @ blocks.cpp:{line}, t @ blocks.cpp:9" in row["error"], row
        found = rows(tmp_path, target(optimization), CPP_SOURCE, marker, "t @ blocks.cpp:9")
        assert found and not [row for row in found if "value" in row], found
    withheld = [row for row in found if "structured binding `t`" in row.get("error", "")]
    assert withheld, found


# #283: range-`for` headers that name the outer `t` before their binding exists.
RANGE_SOURCE = """#include <cstdio>
#include <utility>
#include <vector>
struct P { template <int N> static long t() { return N + 5; } };
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; }
__attribute__((noinline)) std::vector<std::pair<long, long>> make(long v) {
    return {{v + 1000, 1}, {v + 1001, 2}};
}
int main(int argc, char **argv) {
    long t = argc; // OUTER
    P p, *q = &p;
    for (const auto &[t, u] : make(t)) { use(t * u); } // ONE_LINE
    for (const auto &[t, u] : // FOR_LINE
         make(t)) { use(t - u); } // RANGE_LINE
    for (const auto &[t, u] : make(sizeof(t))) { use(t + u); } // SIZEOF
    for (const auto &[t, u] : make(p.template t<0>() + q->template t<1>() + P::template t<2>())) { use(t | u); } // MEMBER
    use(t);
    return 0;
}
"""
HEDGED = (
    "may not be what the code reads: the code here may name the structured binding `t` "
    "(line {}), but the loop's header names `t` before that binding exists, and Peeky "
    "cannot tell whether running the header reads this local"
)
PLAIN = "not what the code reads: the code here names the structured binding `t` (line {})"


def line_of(marker):
    return next(i for i, row in enumerate(RANGE_SOURCE.splitlines(), 1) if marker in row)


@pytest.mark.timeout(180)
@pytest.mark.parametrize("optimization", ["O0", "O1"])
@pytest.mark.parametrize("marker", ["ONE_LINE", "FOR_LINE", "RANGE_LINE", "SIZEOF", "MEMBER"])
def test_a_range_for_header_naming_the_outer_local_hedges_its_note(
    tmp_path, cxx, optimization, marker
):
    # #283: on `for (const auto &[t, u] : make(t)) { use(t * u); }`, the outer `t` said
    # "not what the code reads: the code here names the structured binding `t`", though
    # the header runs before the binding exists, where `t` is the outer local. Whether
    # running it reads `t` (`make(t)`, or `sizeof(t)`) Peeky cannot tell: withheld, hedged.
    # GCC's `t`s are both listed here (see above): its outer one is selected by its line.
    selector = f"t @ blocks.cpp:{line_of('OUTER')}" if cxx == "gcc" else "t"
    found = rows(tmp_path, target(optimization), RANGE_SOURCE, marker, selector)
    # The binding's own rows (a reference) are never said to be the outer local: on the
    # MEMBER line, `P::template t<2>()` read as a declaration of `t` made them so.
    bound = [row for row in found if "&" in (row.get("type") or "")]
    assert not [row for row in bound if "outer local" in row.get("error", "")], bound
    outer = [row for row in found if "structured binding `t`" in row.get("error", "")]
    assert outer and not [row for row in found if "value" in row and row not in bound], found
    split = marker in ("FOR_LINE", "RANGE_LINE")
    binding = line_of("FOR_LINE") if split else line_of(marker)
    hedged, plain = HEDGED.format(binding), PLAIN.format(binding)
    for row in outer:
        error = row["error"]
        assert "reads this local first" not in error, row
        if marker == "MEMBER":
            # `p.template t<0>()` names a member: the line names `t` only as the binding.
            assert error.startswith(plain), row
        elif split or optimization != "O0" or row["hit"] == 1:
            # A header over two lines, an optimized build, or the line's first stop.
            assert error.startswith(hedged), row
        else:
            # Unoptimized, past the line's first instruction: the header has run.
            assert error.startswith(plain), row
    if optimization == "O0" and not split and marker != "MEMBER":
        assert any(row["hit"] == 1 and row["error"].startswith(hedged) for row in outer), found


# #286: other statements' headers that name the outer `t` before the line declares a `t`:
# C++17 init-statements, a classic `for` whose body binds `t`, and a range whose lambda's
# block declares a `static` `t` (that lambda's own, run by another function).
HEADER_SOURCE = """#include <cstdio>
#include <utility>
#include <vector>
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; }
__attribute__((noinline)) std::vector<std::pair<long, long>> make(long v) {
    return {{v + 1000, 1}, {v + 1001, 2}};
}
template <class F> __attribute__((noinline)) std::vector<std::pair<long, long>> make2(long v, F f) {
    return {{v + f(), 1}, {v + 2000, 2}};
}
int main(int argc, char **argv) {
    long t = argc + 2; // OUTER
    if (long k = t; k) { static long t = 5; use(t + k); } // IF_INIT
    switch (long k = t; k) { default: { static long t = 6; use(t + k); } } // SWITCH_INIT
    for (long i = 0; i < t; i++) { auto [t, u] = make(i)[0]; use(t + u); } // FOR_BINDING
    for (const auto &[a, b] : make2(t, [] { long r; { static long t = 7; r = t; } return r; })) { use(a + b); } // LAMBDA
    for (const auto &[t, u] : make2(t, [] { long r; { static long t = 8; r = t; } return r; })) { // LAMBDA_HEADER
        use(t * u);
    }
    use(t);
    return 0;
}
"""
STATEMENTS = {
    # marker: (keyword, what the line declares, whether every stop is hedged)
    "IF_INIT": ("if", "the block's `static` `t` (line {}), ", False),
    "SWITCH_INIT": ("switch", "the block's `static` `t` (line {}), ", False),
    "FOR_BINDING": ("for", "the structured binding `t` (line {})", True),
    "LAMBDA": ("for", "the block's `static` `t` (line {}), ", True),
    "LAMBDA_HEADER": ("for", "the block's `static` `t` (line {}), ", False),
}


def header_line(marker):
    return next(
        i for i, row in enumerate(HEADER_SOURCE.splitlines(), 1) if row.endswith(f"// {marker}")
    )


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    "marker, optimization",
    [
        (marker, level)
        for marker in sorted(STATEMENTS)
        for level in ("O0", "O1")
        # Optimized, #283 hedges this line for its binding (above).
        if (marker, level) != ("LAMBDA_HEADER", "O1")
    ],
)
def test_a_statement_header_naming_the_outer_local_hedges_its_note(
    tmp_path, cxx, marker, optimization
):
    # #286: on `if (long k = t; k) { static long t = 5; use(t + k); }` the outer `t` said
    # "not what the code reads: the code here names the block's `static`", though the
    # header runs first and names the outer `t` there. Whether running it reads `t`, Peeky
    # cannot tell: withheld, hedged. A classic `for`'s condition runs on each pass; a
    # lambda's block in the range is another function's, never this frame's code.
    adapter = target(optimization)
    line, outer = header_line(marker), header_line("OUTER")
    selectors = ("t", f"t @ blocks.cpp:{outer}")
    (tmp_path / "blocks.cpp").write_text(HEADER_SOURCE)
    result = run_probe(tmp_path, Probe("blocks.cpp", line, selectors, hits="all"), adapter=adapter)
    assert result["status"] == "completed", result.get("error")
    found = [row for row in result["rows"] if row["selector"] in selectors]
    # The outer `t` holds 3 here; each `static` 5 to 8, and each binding 1000 or more.
    assert not [row for row in found if row.get("value") == "3"], found
    # The outer local's rows: selected by its line where LLDB lists several `t`s, else
    # plain `t` (a binding's own row is a reference).
    rows = [
        row
        for row in found
        if "this row is the outer local" in row.get("error", "")
        and "&" not in (row.get("type") or "")
    ]
    assert any(row["hit"] == 1 for row in rows), found
    keyword, what, always = STATEMENTS[marker]
    what = what.format(line)
    listed = ("which the debugger does not list", f"listed separately as `t @ blocks.cpp:{line}`")
    shown = listed if what.endswith(", ") else ("",)
    hedged = {
        f"may not be what the code reads: the code here may name {what}{how}, but the "
        f"`{keyword}` statement's header names `t` before that declaration, and Peeky cannot "
        "tell whether running the header reads this local: this row is the outer local of "
        "that name"
        for how in shown
    }
    for row in rows:
        if always or optimization != "O0" or row["hit"] == 1:
            # A part that runs on each pass, an optimized build, or the line's first stop.
            assert row["error"] in hedged, row
        elif marker == "LAMBDA_HEADER":
            # Past the first instruction, the loop's binding exists (#283).
            assert row["error"].startswith(
                f"not what the code reads: the code here names the structured binding `t` "
                f"(line {line}): this row is the outer local"
            ), row
        else:
            # Unoptimized, past the line's first instruction: a header run once has run.
            assert row["error"].startswith(f"not what the code reads: the code here names {what}")
            assert "header names" not in row["error"], row
