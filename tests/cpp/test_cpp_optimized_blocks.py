"""In an optimized build, a range-for's structured binding withholds the outer local of its
name where LLDB lists only that local, as it does at -O0 (the C cases are in
tests/native/test_native_optimized_blocks.py). On a one-line range-for whose range reads
the outer local, that local's note says so, at -O0 too; so does any one-line statement
whose header names it before the line declares the name again. An `if constexpr` block's
declaration hides the outer local too. A line naming the outer local outside the scope of
its block's declaration, before it or after it ends, says so."""

import json
import os
import platform

import pytest

from peeky.cli import main
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

X86_64 = platform.machine().lower() in ("x86_64", "amd64")


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
    if (cxx, optimization, marker) == ("gcc", "O2", "ONE_LINE") and X86_64:
        # On x86-64, GCC -O2 splits main's exception path off as `main.cold`, and LLDB
        # places line 14's breakpoint there only (lldb-dap 18 reports it on line 17, 20 on
        # line 14): the program runs line 14 without a stop. The probe says the line may
        # have run, never only to pick a line the program executes.
        (tmp_path / "blocks.cpp").write_text(CPP_SOURCE)
        line = next(i for i, row in enumerate(CPP_SOURCE.splitlines(), 1) if marker in row)
        probe = Probe("blocks.cpp", line, ("t",), hits="all")
        result = run_probe(tmp_path, probe, adapter=target(optimization))
        assert result["status"] == "no-stop" and not result["rows"], result
        message = result["error"]["message"]
        assert f"so the program may have run line {line} without a stop there" in message, message
        return
    found = rows(tmp_path, target(optimization), CPP_SOURCE, marker, "t")
    shown = [row for row in found if "value" in row]
    # A stop of the header doesn't show the outer `t` (argc, 1) as plain `t`.
    assert not shown, shown
    if cxx == "gcc":
        # LLDB lists both of GCC's `t`s here, so plain `t` names neither, and names both;
        # the outer one, by its line, is withheld as clang's is.
        line = next(i for i, row in enumerate(CPP_SOURCE.splitlines(), 1) if marker in row)
        for row in found:
            assert row["error"].startswith("not found; visible: "), row
            assert f"t @ blocks.cpp:{line}, t @ blocks.cpp:9" in row["error"], row
        found = rows(tmp_path, target(optimization), CPP_SOURCE, marker, "t @ blocks.cpp:9")
        assert found and not [row for row in found if "value" in row], found
    withheld = [row for row in found if "structured binding `t`" in row.get("error", "")]
    assert withheld, found


# Range-`for` headers that name the outer `t` before their binding exists.
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
    # On `for (const auto &[t, u] : make(t)) { use(t * u); }` the outer `t` is no plain
    # "not what the code reads: the code here names the structured binding `t`": the
    # header runs before the binding exists, where `t` is the outer local. Whether
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


# Other statements' headers that name the outer `t` before the line declares a `t`:
# C++17 init-statements, and a classic `for` whose body binds `t`. (A lambda's block in a
# range is that lambda's own, run by another function: see
# test_a_lambdas_block_is_scoped_to_the_lambda.)
HEADER_SOURCE = """#include <cstdio>
#include <utility>
#include <vector>
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; }
__attribute__((noinline)) std::vector<std::pair<long, long>> make(long v) {
    return {{v + 1000, 1}, {v + 1001, 2}};
}
int main(int argc, char **argv) {
    long t = argc + 2; // OUTER
    if (long k = t; k) { static long t = 5; use(t + k); } // IF_INIT
    switch (long k = t; k) { case 1: use(k); break; default: { static long t = 6; use(t + k); } } // SWITCH_INIT
    for (long i = 0; i < t; i++) { auto [t, u] = make(i)[0]; use(t + u); } // FOR_BINDING
    use(t);
    return 0;
}
"""
STATEMENTS = {
    # marker: (keyword, what the line declares, whether every stop is hedged)
    "IF_INIT": ("if", "the block's `static` `t` (line {}), ", False),
    "SWITCH_INIT": ("switch", "the block's `static` `t` (line {}), ", False),
    "FOR_BINDING": ("for", "the structured binding `t` (line {})", True),
}


def header_line(marker):
    return next(
        i for i, row in enumerate(HEADER_SOURCE.splitlines(), 1) if row.endswith(f"// {marker}")
    )


@pytest.mark.timeout(180)
@pytest.mark.parametrize("optimization", ["O0", "O1"])
@pytest.mark.parametrize("marker", sorted(STATEMENTS))
def test_a_statement_header_naming_the_outer_local_hedges_its_note(
    tmp_path, cxx, marker, optimization
):
    # On `if (long k = t; k) { static long t = 5; use(t + k); }` the outer `t` is no plain
    # "not what the code reads: the code here names the block's `static`": the header
    # runs first and names the outer `t` there. Whether running it reads `t`, Peeky
    # cannot tell: withheld, hedged. A classic `for`'s condition runs on each pass.
    adapter = target(optimization)
    line, outer = header_line(marker), header_line("OUTER")
    selectors = ("t", f"t @ blocks.cpp:{outer}")
    (tmp_path / "blocks.cpp").write_text(HEADER_SOURCE)
    result = run_probe(tmp_path, Probe("blocks.cpp", line, selectors, hits="all"), adapter=adapter)
    assert result["status"] == "completed", result.get("error")
    found = [row for row in result["rows"] if row["selector"] in selectors]
    # The outer `t` holds 3 here; each `static` 5 or 6, and each binding 1000 or more.
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
        else:
            # Unoptimized, past the line's first instruction: a header run once has run.
            assert row["error"].startswith(f"not what the code reads: the code here names {what}")
            assert "header names" not in row["error"], row


# Back from a call in a one-line range-`for`'s range, the stop is past
# the line's first instruction, but the range (`t` after `check(t)`) has not run yet.
STEP_SOURCE = """#include <cstdio>
#include <utility>
#include <vector>
__attribute__((noinline)) long check(long v) { return v + 1; } // CHECK
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; } // USE
__attribute__((noinline)) std::vector<std::pair<long, long>> make(long a, long b) {
    return {{a + 1000, b}};
}
int main(int argc, char **argv) {
    long t = argc + 2; // OUTER
    for (const auto &[t, u] : make(check(t), t)) { use(t * u); use(t); } // FOR
    return 0;
}
"""


@pytest.mark.timeout(240)
def test_a_stop_back_inside_a_range_for_header_hedges_its_note(tmp_path, cxx, monkeypatch, capsys):
    # Back from `check(t)` (`step --out`), the range has yet to read the outer `t`: hedged,
    # as the line's first instruction is, not a plain "not what the code reads: the code
    # here names the structured binding". Back from the body's first `use`,
    # the line table's column is past the header's `)`: the plain note stays.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "step.cpp"
    path.write_text(STEP_SOURCE)
    lines = {
        marker: next(
            i for i, row in enumerate(STEP_SOURCE.splitlines(), 1) if f"// {marker}" in row
        )
        for marker in ("CHECK", "USE", "OUTER", "FOR")
    }
    selectors = ("t", f"t @ step.cpp:{lines['OUTER']}")
    binding = f"the structured binding `t` (line {lines['FOR']})"
    expected = {
        "CHECK": (
            f"may not be what the code reads: the code here may name {binding}, but the loop's "
            "header names `t` before that binding exists, and Peeky cannot tell whether "
            "running the header reads this local: this row is the outer local of that name"
        ),
        "USE": (
            f"not what the code reads: the code here names {binding}: this row is the outer "
            "local of that name"
        ),
    }
    for marker, error in expected.items():
        assert main(["session", "start", str(path), "--at", str(lines[marker])]) == 0
        capsys.readouterr()
        try:
            assert main(["session", "step", "--out"]) == 0
            capsys.readouterr()
            main(["session", "get", *selectors, "--json"])
            rows = json.loads(capsys.readouterr().out)
        finally:
            main(["session", "close"])
            capsys.readouterr()
        assert {row.get("line") for row in rows} == {lines["FOR"]}, rows
        # The outer local's rows (a binding's own row is a reference).
        outer = [
            row
            for row in rows
            if "this row is the outer local" in row.get("error", "")
            and "&" not in (row.get("type") or "")
        ]
        assert outer and all(row["error"] == error for row in outer), (marker, rows)
        assert not [row for row in outer if "value" in row], rows


# A range type's own `begin` and `end` (not inlined) run at the
# loop's `:` in clang's line table, once its range has run.
RANGE_TYPE_SOURCE = """#include <cstdio>
struct P { long first, second; };
struct Range {
    P items[2];
    __attribute__((noinline)) const P *begin() const { return items; } // BEGIN
    __attribute__((noinline)) const P *end() const { return items + 2; } // END
};
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; }
__attribute__((noinline)) Range make(long a) { return {{{a + 1000, 1}, {a + 1001, 2}}}; }
int main(int argc, char **argv) {
    long t = argc + 2; // OUTER
    for (const auto &[t, u] : make(t)) { use(t * u); use(t); } // FOR
    return 0;
}
"""


@pytest.mark.timeout(240)
@pytest.mark.parametrize("marker", ["BEGIN", "END"])
def test_a_stop_back_from_a_range_types_begin_is_judged_by_its_column(
    tmp_path, cxx, monkeypatch, capsys, marker
):
    # Back from `begin()` or `end()` (`step --out`), the stop is past the line's first
    # instruction, after the range (`make(t)`) has run. clang places that call at the
    # loop's `:`, which counts as past the range: the plain note. GCC places it at the
    # range's last column, inside the range: hedged, as Peeky cannot tell it apart.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "range.cpp"
    path.write_text(RANGE_TYPE_SOURCE)
    lines = {
        name: next(
            i for i, row in enumerate(RANGE_TYPE_SOURCE.splitlines(), 1) if f"// {name}" in row
        )
        for name in (marker, "OUTER", "FOR")
    }
    selectors = ("t", f"t @ range.cpp:{lines['OUTER']}")
    binding = f"the structured binding `t` (line {lines['FOR']})"
    error = (
        f"not what the code reads: the code here names {binding}: this row is the outer local "
        "of that name"
        if cxx == "clang"
        else f"may not be what the code reads: the code here may name {binding}, but the "
        "loop's header names `t` before that binding exists, and Peeky cannot tell whether "
        "running the header reads this local: this row is the outer local of that name"
    )
    assert main(["session", "start", str(path), "--at", str(lines[marker])]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "step", "--out"]) == 0
        capsys.readouterr()
        main(["session", "get", *selectors, "--json"])
        rows = json.loads(capsys.readouterr().out)
    finally:
        main(["session", "close"])
        capsys.readouterr()
    assert {row.get("line") for row in rows} == {lines["FOR"]}, rows
    outer = [
        row
        for row in rows
        if "this row is the outer local" in row.get("error", "")
        and "&" not in (row.get("type") or "")
    ]
    assert outer and all(row["error"] == error for row in outer), rows
    assert not [row for row in outer if "value" in row], rows


# A block in a lambda wholly on a line is that lambda's, run by another function.
LAMBDA_SOURCE = """#include <cstdio>
#include <utility>
#include <vector>
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; }
template <class F> __attribute__((noinline)) std::vector<std::pair<long, long>> make2(long v, F f) {
    return {{v + f(), 1}, {v + 2000, 2}};
}
int main(int argc, char **argv) {
    long t = argc + 2; // OUTER
    auto g = [&](long t) __attribute__((noinline)) { { static long t = 5; use(t++); } return use(t); }; // FRAME
    use(g(t + 10));
    for (const auto &[a, b] : make2(t, [] { long r; { static long t = 7; r = t; } return r; })) { use(a + b); } // RANGE
    for (const auto &[t, u] : make2(t, [] { long r; { static long t = 8; r = t; } return r; })) { // BINDING
        use(t * u);
    }
    long v = [](long t) constexpr { long r = t; { enum { t = 9 }; r += t; } return r + t; }(t + 10); // CONSTEXPR
    long w = []<class T>(T t) constexpr { T r = t; { enum { t = 8 }; r += t; } return r + t; }(t + 10); // GENERIC
    use(t + v + w);
    return 0;
}
"""


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    "marker, optimization",
    [
        (marker, level)
        for marker in ("FRAME", "RANGE", "BINDING")
        for level in ("O0", "O1")
        # Optimized, LLDB lists no binding there, and the outer row is hedged for the
        # loop's binding.
        if (marker, level) != ("BINDING", "O1")
    ],
)
def test_a_lambdas_block_is_scoped_to_the_lambda(tmp_path, cxx, marker, optimization):
    # A block in a lambda wholly on the line counts for the lambda, not the function
    # holding it: `main`'s outer `t` is not withheld for the lambda's `static` (RANGE), nor
    # the loop's own binding called "the outer local" (BINDING). In the lambda's own frame,
    # its block counts: the parameter `t` (13) is withheld where the code names the
    # block's `static` (FRAME).
    line = next(
        i for i, row in enumerate(LAMBDA_SOURCE.splitlines(), 1) if row.endswith(f"// {marker}")
    )
    outer = next(i for i, row in enumerate(LAMBDA_SOURCE.splitlines(), 1) if "OUTER" in row)
    selectors = ("t", f"t @ blocks.cpp:{outer}")
    (tmp_path / "blocks.cpp").write_text(LAMBDA_SOURCE)
    result = run_probe(
        tmp_path, Probe("blocks.cpp", line, selectors, hits="all"), adapter=target(optimization)
    )
    assert result["status"] == "completed", result.get("error")
    found = [row for row in result["rows"] if row["selector"] in selectors]
    # `main`'s frames, and the lambda's (clang names a generic one's by its address).
    mains = [row for row in found if (row.get("function") or "").split(" ")[0] == "main"]
    lambdas = [row for row in found if row not in mains]
    static = f"the block's `static` `t` (line {line})"
    # `main` never names the lambda's `static`, nor is a binding's own row the outer local.
    assert not [row for row in mains if static in row.get("error", "")], mains
    assert not [
        row
        for row in mains
        if "this row is the outer local" in row.get("error", "") and "&" in (row.get("type") or "")
    ], mains
    if marker == "FRAME":
        # The parameter (13) is withheld where its line names the block's `static`; but
        # `return use(t)` reads the parameter after that block ends.
        assert not [row for row in found if row.get("value") == "13"], found
        after = (
            "but the line also names `t` after that declaration's scope ends, and Peeky "
            "cannot tell whether that code reads this local: this row is the outer local"
        )
        assert [
            row
            for row in lambdas
            if row["error"].startswith(
                f"may not be what the code reads: the code here may name {static}"
            )
            and after in row["error"]
        ], lambdas
    elif marker == "RANGE":
        # Only the outer `t` (3) is named in `main` there: shown.
        assert [row for row in mains if row.get("value") == "3"], mains
    else:
        # At the line's first instruction, before the binding exists, the outer `t` (3).
        assert [row for row in mains if row["hit"] == 1 and row.get("value") == "3"], mains


@pytest.mark.timeout(180)
@pytest.mark.parametrize("optimization", ["O0", "O1"])
@pytest.mark.parametrize("marker", ["CONSTEXPR", "GENERIC"])
def test_a_constexpr_or_generic_lambdas_block_is_its_own(tmp_path, cxx, marker, optimization):
    # A lambda declared `constexpr` (and a generic one, `[]<class T>`) is a lambda too: its
    # block's enumerator `t` doesn't count for `main`, whose `t` (3) is not withheld as
    # "not what the code reads". In the lambda's own frame, called
    # immediately with 13, its parameter is withheld for that enumerator.
    line = next(
        i for i, row in enumerate(LAMBDA_SOURCE.splitlines(), 1) if row.endswith(f"// {marker}")
    )
    outer = next(i for i, row in enumerate(LAMBDA_SOURCE.splitlines(), 1) if "OUTER" in row)
    selectors = ("t", f"t @ blocks.cpp:{outer}")
    (tmp_path / "blocks.cpp").write_text(LAMBDA_SOURCE)
    result = run_probe(
        tmp_path, Probe("blocks.cpp", line, selectors, hits="all"), adapter=target(optimization)
    )
    assert result["status"] == "completed", result.get("error")
    found = [row for row in result["rows"] if row["selector"] in selectors]
    # `main`'s frames, and the lambda's (clang names a generic one's by its address).
    mains = [row for row in found if (row.get("function") or "").split(" ")[0] == "main"]
    lambdas = [row for row in found if row not in mains]
    enumerator = f"the block's enumerator `t` (line {line})"
    assert not [row for row in mains if enumerator in row.get("error", "")], mains
    # The lambda's parameter (13) is never shown: withheld for its block's `t` (or, at
    # GCC's prologue, as not stored yet).
    assert not [row for row in lambdas if "value" in row], lambdas
    assert not [row for row in found if row.get("value") == "13"], found
    if optimization == "O0":
        # The caller's `t`, and the lambda's own frame, withheld for its block's `t`; but
        # `T r = t` reads the parameter before that block, and `return r + t` after.
        assert [row for row in mains if row.get("value") == "3"], mains
        both = (
            "but the line also names `t` before that declaration and after its scope ends, "
            "and Peeky cannot tell whether that code reads this local"
        )
        assert [
            row
            for row in lambdas
            if row["error"].startswith(
                f"may not be what the code reads: the code here may name {enumerator}"
            )
            and both in row["error"]
        ], lambdas


# An `if constexpr` block's `static`, on the stop's line.
CONSTEXPR_SOURCE = """#include <cstdio>
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; }
int main(int argc, char **argv) {
    long t = argc + 2; // OUTER
    if constexpr (sizeof(long) > 1) { static long t = 5; use(t); } // CONSTEXPR
    if constexpr (sizeof(t) > 1) { static long t = 6; use(t); } // HEADER
    use(t);
    return 0;
}
"""


@pytest.mark.timeout(180)
@pytest.mark.parametrize("optimization", ["O0", "O1"])
@pytest.mark.parametrize("marker", ["CONSTEXPR", "HEADER"])
def test_an_if_constexpr_blocks_static_withholds_the_outer_local(
    tmp_path, cxx, optimization, marker
):
    # On `if constexpr (sizeof(long) > 1) { static long t = 5; use(t); }` the code names
    # the block's `static`, so the outer `t` (3) is not shown with no note: the block is no
    # function's body. Where the header names `t` too, the note is hedged as other
    # headers' are.
    line = next(
        i for i, row in enumerate(CONSTEXPR_SOURCE.splitlines(), 1) if row.endswith(f"// {marker}")
    )
    outer = next(i for i, row in enumerate(CONSTEXPR_SOURCE.splitlines(), 1) if "OUTER" in row)
    selectors = ("t", f"t @ blocks.cpp:{outer}")
    (tmp_path / "blocks.cpp").write_text(CONSTEXPR_SOURCE)
    result = run_probe(
        tmp_path, Probe("blocks.cpp", line, selectors, hits="all"), adapter=target(optimization)
    )
    assert result["status"] == "completed", result.get("error")
    found = [row for row in result["rows"] if row["selector"] in selectors]
    # The outer `t` holds 3; the blocks' `static`s 5 and 6.
    assert not [row for row in found if row.get("value") == "3"], found
    rows = [row for row in found if "this row is the outer local" in row.get("error", "")]
    assert any(row["hit"] == 1 for row in rows), found
    static = f"the block's `static` `t` (line {line}), "
    listed = ("which the debugger does not list", f"listed separately as `t @ blocks.cpp:{line}`")
    plain = {
        f"not what the code reads: the code here names {static}{how}: this row is the outer "
        "local of that name"
        for how in listed
    }
    hedged = {
        f"may not be what the code reads: the code here may name {static}{how}, but the `if` "
        "statement's header names `t` before that declaration, and Peeky cannot tell whether "
        "running the header reads this local: this row is the outer local of that name"
        for how in listed
    }
    for row in rows:
        if marker == "HEADER" and (optimization != "O0" or row["hit"] == 1):
            assert row["error"] in hedged, row
        else:
            assert row["error"] in plain, row


# Lines that name the outer `t` outside the scope of a block's `static` `t`: `and` and
# `not` before it, after the block; and an `if`'s binding, in scope in its `else` (on the
# line, or below it), and up to the end of a `do` loop it controls, or of a labelled block.
SAME_LINE_SOURCE = """#include <cstdio>
#include <utility>
__attribute__((noinline)) long use(long v) { std::printf("U=%ld\\n", v); return v; }
__attribute__((noinline)) std::pair<long, long> make(long v) { return {v + 40, v}; }
__attribute__((noinline)) long both(long t, bool ok) {
    { static long t = 8; use(t); } return ok and t; // AND
}
__attribute__((noinline)) long negated(long t) {
    { static long t = 9; use(t); } return not t; // NOT
}
int main(int argc, char **argv) {
    long t = argc + 2; // OUTER
    do { static long t = 5; use(t); } while (t-- > 2); // DO
    use(t); { static long t = 6; use(t); } // BEFORE
    { static long t = 7; use(t); } use(t); // AFTER
    use(both(t, argc > 0) + negated(t));
    if (auto [t, u] = make(argc); u > 5) { use(u); } else use(t); // ELSE
    if (auto [t, u] = make(argc); u > 5) { // IF
        use(u);
    } else {
        use(t); // ELSE_BODY
    }
    if (auto [t, u] = make(argc); u > 5) do { use(u); } while (false); use(t); // DO_IF
    if (auto [t, u] = make(argc); u > 5) do { // DO_HEAD
        use(u);
    } while (false); use(t); // DO_TAIL
    if (auto [t, u] = make(argc); u > 5) again: { use(u); } use(t); // LABEL
    if (auto [t, u] = make(argc); u > 5) later: { // LABEL_HEAD
        use(u);
    } use(t); // LABEL_TAIL
    return 0;
}
"""
SAME_LINE = {
    "DO": "after that declaration's scope ends",
    "BEFORE": "before that declaration",
    "AFTER": "after that declaration's scope ends",
    "AND": "after that declaration's scope ends",
    "NOT": "after that declaration's scope ends",
}


@pytest.mark.timeout(180)
@pytest.mark.parametrize("optimization", ["O0", "O1"])
@pytest.mark.parametrize("marker", sorted(SAME_LINE))
def test_a_line_naming_the_outer_local_outside_the_blocks_scope_hedges_its_note(
    tmp_path, cxx, optimization, marker
):
    # On `do { static long t = 5; use(t); } while (t-- > 2);` the `while` condition reads
    # the outer `t` after that `static` leaves scope; so does the first call of
    # `use(t); { static long t = 6; use(t); }`, before the block. Withheld still, the outer
    # `t`'s note says where the line names `t` outside, not only "not what the code reads:
    # the code here names the block's `static`".
    lines = SAME_LINE_SOURCE.splitlines()
    line = next(i for i, row in enumerate(lines, 1) if row.endswith(f"// {marker}"))
    outer = next(i for i, row in enumerate(lines, 1) if row.endswith("// OUTER"))
    if marker in ("AND", "NOT"):
        # The function's parameter, on the line above (`t @ blocks.cpp:N` at -O1).
        outer = line - 1
    (tmp_path / "blocks.cpp").write_text(SAME_LINE_SOURCE)
    selectors = ("t", f"t @ blocks.cpp:{outer}")
    result = run_probe(
        tmp_path, Probe("blocks.cpp", line, selectors, hits="all"), adapter=target(optimization)
    )
    assert result["status"] == "completed", result.get("error")
    found = [row for row in result["rows"] if row["selector"] in selectors]
    # The outer `t` holds 1 to 3 here; each block's `static` 5 or more.
    assert not [row for row in found if row.get("value") in ("1", "2", "3")], found
    rows = [row for row in found if "this row is the outer local" in row.get("error", "")]
    assert any(row["hit"] == 1 for row in rows), found
    static = f"the block's `static` `t` (line {line}), "
    listed = ("which the debugger does not list", f"listed separately as `t @ blocks.cpp:{line}`")
    hedged = {
        f"may not be what the code reads: the code here may name {static}{how}, but the line "
        f"also names `t` {SAME_LINE[marker]}, and Peeky cannot tell whether that code reads "
        "this local: this row is the outer local of that name"
        for how in listed
    }
    for row in rows:
        assert row["error"] in hedged, row


@pytest.mark.timeout(180)
@pytest.mark.parametrize("optimization", ["O0", "O1"])
@pytest.mark.parametrize("marker", ["ELSE", "ELSE_BODY"])
def test_an_if_headers_binding_is_what_its_else_names(tmp_path, cxx, optimization, marker):
    # An `if`'s init-statement binding is in scope in its `else`: on `if (auto [t, u] =
    # make(argc); u > 5) { use(u); } else use(t);` the line doesn't name `t` "after that
    # declaration's scope ends"; and below the line, in a braced `else`, the outer `t` (1)
    # is not shown plainly, as the code there names the binding.
    lines = SAME_LINE_SOURCE.splitlines()
    line = next(i for i, row in enumerate(lines, 1) if row.endswith(f"// {marker}"))
    outer = next(i for i, row in enumerate(lines, 1) if row.endswith("// OUTER"))
    header = (
        line
        if marker == "ELSE"
        else next(i for i, row in enumerate(lines, 1) if row.endswith("// IF"))
    )
    (tmp_path / "blocks.cpp").write_text(SAME_LINE_SOURCE)
    selectors = ("t", f"t @ blocks.cpp:{outer}")
    result = run_probe(
        tmp_path, Probe("blocks.cpp", line, selectors, hits="all"), adapter=target(optimization)
    )
    assert result["status"] == "completed", result.get("error")
    found = [row for row in result["rows"] if row["selector"] in selectors]
    # The outer `t` holds 1 here; the binding 41.
    assert not [row for row in found if row.get("value") == "1"], found
    # Optimized, a stop may be placed on another line with the same code (clang merges
    # the calls `use(t)`): only this line's notes are about it.
    found = [row for row in found if row.get("line") == line]
    assert not [row for row in found if "scope ends" in row.get("error", "")], found
    rows = [row for row in found if "this row is the outer local" in row.get("error", "")]
    # Optimized, the outer `t` may be gone there ("no value here").
    assert rows or optimization != "O0", found
    for row in rows:
        assert row["error"].startswith(
            "not what the code reads: the code here names the structured binding "
            f"`t` (line {header})"
        ), row


@pytest.mark.timeout(180)
@pytest.mark.parametrize("optimization", ["O0", "O1"])
@pytest.mark.parametrize(
    "marker, marked",
    [
        ("DO_IF", "DO_IF"),
        ("DO_TAIL", "DO_HEAD"),
        ("LABEL", "LABEL"),
        ("LABEL_TAIL", "LABEL_HEAD"),
    ],
)
def test_a_statement_an_if_controls_ends_where_it_ends(tmp_path, cxx, optimization, marker, marked):
    # The `if`'s binding is in scope only to the end of the `do` loop it controls: on
    # `if (auto [t, u] = make(argc); u > 5) do { use(u); } while (false); use(t);` the last
    # `use(t)` reads the outer `t`, which no note says the code names as the binding. Also
    # on a loop over several lines, on its last; and for a labelled block (`again: { … }`),
    # no plain statement to the `;` after it.
    lines = SAME_LINE_SOURCE.splitlines()
    line = next(i for i, row in enumerate(lines, 1) if row.endswith(f"// {marker}"))
    outer = next(i for i, row in enumerate(lines, 1) if row.endswith("// OUTER"))
    header = next(i for i, row in enumerate(lines, 1) if row.endswith(f"// {marked}"))
    (tmp_path / "blocks.cpp").write_text(SAME_LINE_SOURCE)
    selectors = ("t", f"t @ blocks.cpp:{outer}")
    result = run_probe(
        tmp_path, Probe("blocks.cpp", line, selectors, hits="all"), adapter=target(optimization)
    )
    if optimization != "O0" and result["status"] == "no-stop":
        # Optimized, clang may merge the line's `use(t)` with another line's: no code of
        # its own to stop at.
        return
    assert result["status"] == "completed", result.get("error")
    found = [row for row in result["rows"] if row["selector"] in selectors]
    assert not [row for row in found if row.get("value") == "1"], found
    found = [row for row in found if row.get("line") == line]
    rows = [row for row in found if "this row is the outer local" in row.get("error", "")]
    # Optimized, the outer `t` may be gone there ("no value here").
    assert rows or optimization != "O0", found
    for row in rows:
        assert row["error"] == (
            "may not be what the code reads: the code here may name the structured binding "
            f"`t` (line {header}), but the line also names `t` after that declaration's "
            "scope ends, and Peeky cannot tell whether that code reads this local: this row "
            "is the outer local of that name"
        ), row
