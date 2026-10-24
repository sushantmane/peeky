"""#173: in an optimized build, a block's own declaration of a name (an enumerator, a
`typedef`, an `extern`) withholds the outer local's row as it does at -O0. The outer
local there lives in a register or as a constant, not in the frame's memory, and the
scan (#144) skipped it: plain `n` showed the outer local's value where the code names
the block's. The structured binding case is in tests/cpp/test_cpp_optimized_blocks.py.
#286: a one-line statement whose header names the outer local before its block declares
the name again hedges that local's note, at -O0 too, with clang and GCC; also at a stop
back inside that header, past the line's first instruction. #293: so does a line that
names the outer local outside the scope of its block's declaration, before the block or
after it ends."""

import json
import os
import shutil

import pytest
from native_compilers import is_gcc

from peeky.cli import main
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


# #286: one-line statements whose header names the outer `t` before their block's `static`
# `t` exists. A `while`'s condition, and a `for`'s condition, run on each pass.
HEADER_SOURCE = """#include <stdio.h>
__attribute__((noinline)) long use(long v) { printf("U=%ld\\n", v); return v; }
int main(int argc, char **argv) {
    long t = argc + 2; /* OUTER */
    if (t) { static long t = 5; use(t); } /* IF */
    for (long i = 0; i < t; i++) { static long t = 6; use(t + i); } /* FOR */
    for (long i = t; i < 4; i++) { static long t = 7; use(t + i); } /* FOR_INIT */
    while (t-- > 1) { static long t = 8; use(t); } /* WHILE */
    switch (t) { default: { static long t = 9; use(t); } } /* SWITCH */
    use(t);
    return 0;
}
"""
HEADERS = {"IF": "if", "FOR": "for", "FOR_INIT": "for", "WHILE": "while", "SWITCH": "switch"}


@pytest.fixture(params=["clang", "gcc"])
def cc(request, monkeypatch):
    """The C compiler's family, with PEEKY_NATIVE_CC set to it: the lane's own where it is
    of the family (the clang case skips where the lane builds C with GCC), else a `gcc` on
    the path that is GCC."""
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    compiler = os.environ.get("PEEKY_NATIVE_CC", "clang")
    if (request.param == "gcc") != is_gcc(compiler):
        if request.param == "clang":
            pytest.skip("this lane builds C with GCC; the clang lanes run the clang case")
        compiler = shutil.which("gcc")
        if compiler is None or not is_gcc(compiler):
            pytest.skip("needs GCC")
    monkeypatch.setenv("PEEKY_NATIVE_CC", compiler)
    return request.param


def header_line(marker):
    return next(
        i for i, row in enumerate(HEADER_SOURCE.splitlines(), 1) if row.endswith(f"/* {marker} */")
    )


@pytest.mark.timeout(180)
@pytest.mark.parametrize("optimization", ["O0", "O1"])
@pytest.mark.parametrize("marker", sorted(HEADERS))
def test_a_statement_header_naming_the_outer_local_hedges_its_note(
    tmp_path, cc, optimization, marker
):
    # #286: on `if (t) { static long t = 5; use(t); }` the outer `t` said "not what the
    # code reads: the code here names the block's `static`", though the line's first
    # instruction runs the header, where `t` is the outer local. Whether running it reads
    # `t`, Peeky cannot tell: withheld, hedged. The outer local is selected by its line
    # where the debugger lists several `t`s; plain `t` is whichever it lists.
    adapter = target(optimization)
    (tmp_path / "blocks.c").write_text(HEADER_SOURCE)
    line, outer = header_line(marker), header_line("OUTER")
    selectors = ("t", f"t @ blocks.c:{outer}")
    result = run_probe(tmp_path, Probe("blocks.c", line, selectors, hits="all"), adapter=adapter)
    assert result["status"] == "completed", result.get("error")
    found = [row for row in result["rows"] if row["selector"] in selectors]
    # The outer `t` holds 0 to 3 here; each block's `static` 5 or more.
    assert not [row for row in found if row.get("value") in ("0", "1", "2", "3")], found
    rows = [row for row in found if "this row is the outer local" in row.get("error", "")]
    assert any(row["hit"] == 1 for row in rows), found
    static = f"the block's `static` `t` (line {line}), "
    listed = ("which the debugger does not list", f"listed separately as `t @ blocks.c:{line}`")
    hedged = {
        f"may not be what the code reads: the code here may name {static}{how}, but the "
        f"`{HEADERS[marker]}` statement's header names `t` before that declaration, and Peeky "
        "cannot tell whether running the header reads this local: this row is the outer local "
        "of that name"
        for how in listed
    }
    plain = {
        f"not what the code reads: the code here names {static}{how}: this row is the outer "
        "local of that name"
        for how in listed
    }
    for row in rows:
        if marker in ("FOR", "WHILE") or optimization != "O0" or row["hit"] == 1:
            # A part that runs on each pass, an optimized build, or the line's first stop.
            assert row["error"] in hedged, row
        else:
            # Unoptimized, past the line's first instruction: a header run once has run.
            assert row["error"] in plain, row


# Review of PR #287: a stop back from a call in a one-line header's condition is past the
# line's first instruction, but the rest of the header (`t > 2`) has not run yet.
STEP_SOURCE = """#include <stdio.h>
__attribute__((noinline)) long check(long v) { return v + 1; } /* CHECK */
__attribute__((noinline)) long use(long v) { printf("U=%ld\\n", v); return v; } /* USE */
int main(int argc, char **argv) {
    long t = argc + 2; /* OUTER */
    if (check(t) && t > 2) { static long t = 5; use(t); use(t); } /* IF */
    return 0;
}
"""


@pytest.mark.timeout(240)
def test_a_stop_back_inside_a_header_hedges_its_note(tmp_path, cc, monkeypatch, capsys):
    # Back from `check(t)` (`step --out`), the stop is part-way through the `if`'s
    # header, before `t > 2` reads the outer `t`: it said "not what the code reads",
    # since only the line's first instruction was hedged. Back from the body's first
    # `use(t)`, the line table's column is past the header's `)`: the plain note stays.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "step.c"
    path.write_text(STEP_SOURCE)
    lines = {
        marker: next(
            i for i, row in enumerate(STEP_SOURCE.splitlines(), 1) if f"/* {marker} */" in row
        )
        for marker in ("CHECK", "USE", "OUTER", "IF")
    }
    selectors = ("t", f"t @ step.c:{lines['OUTER']}")
    static = f"the block's `static` `t` (line {lines['IF']}), which the debugger does not list"
    expected = {
        "CHECK": (
            f"may not be what the code reads: the code here may name {static}, but the `if` "
            "statement's header names `t` before that declaration, and Peeky cannot tell "
            "whether running the header reads this local: this row is the outer local of that "
            "name"
        ),
        "USE": (
            f"not what the code reads: the code here names {static}: this row is the outer "
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
        assert {row.get("line") for row in rows} == {lines["IF"]}, rows
        assert not [row for row in rows if "value" in row], rows
        outer = [row for row in rows if "this row is the outer local" in row.get("error", "")]
        assert outer and all(row["error"] == error for row in outer), (marker, rows)


# #293: lines that name the outer `t` outside the scope of their block's `static` `t`:
# a `do` loop's condition and a call after the block ends, a call before it opens. Review
# of PR #319: a comma expression after the block, also from a macro, names it too.
SAME_LINE_SOURCE = """#include <stdio.h>
__attribute__((noinline)) long use(long v) { printf("U=%ld\\n", v); return v; }
#define PAIR use(0), t
__attribute__((noinline)) long last(long t) {
    { static long t = 8; use(t); } return use(0), t; /* COMMA */
}
__attribute__((noinline)) long paired(long t) {
    { static long t = 9; use(t); } return PAIR; /* MACRO */
}
int main(int argc, char **argv) {
    long t = argc + 2; /* OUTER */
    do { static long t = 5; use(t); } while (t-- > 2); /* DO */
    use(t); { static long t = 6; use(t); } /* BEFORE */
    { static long t = 7; use(t); } use(t); /* AFTER */
    use(last(t) + paired(t));
    return 0;
}
"""
SAME_LINE = {
    "DO": "after that declaration's scope ends",
    "BEFORE": "before that declaration",
    "AFTER": "after that declaration's scope ends",
    "COMMA": "after that declaration's scope ends",
    "MACRO": "after that declaration's scope ends",
}


@pytest.mark.timeout(180)
@pytest.mark.parametrize("optimization", ["O0", "O1"])
@pytest.mark.parametrize("marker", sorted(SAME_LINE))
def test_a_line_naming_the_outer_local_outside_the_blocks_scope_hedges_its_note(
    tmp_path, cc, optimization, marker
):
    # #293: on `do { static long t = 5; use(t); } while (t-- > 2);` the outer `t` said
    # "not what the code reads: the code here names the block's `static`", though the
    # `while` condition runs after that `static` leaves scope and reads the outer `t`;
    # and so on `use(t); { static long t = 6; use(t); }`, whose first call reads it before
    # the block. Withheld still, the note now says where the line names `t` outside. In
    # `last` and `paired`, the parameter `t` (1) is the outer one.
    lines = SAME_LINE_SOURCE.splitlines()
    line = next(i for i, row in enumerate(lines, 1) if row.endswith(f"/* {marker} */"))
    outer = next(i for i, row in enumerate(lines, 1) if row.endswith("/* OUTER */"))
    if marker in ("COMMA", "MACRO"):
        # The function's parameter, on the line above: at -O1 LLDB lists it as `t @
        # blocks.c:N` beside the block's `static` kept as a constant (review round 2).
        outer = line - 1
    (tmp_path / "blocks.c").write_text(SAME_LINE_SOURCE)
    selectors = ("t", f"t @ blocks.c:{outer}")
    result = run_probe(
        tmp_path, Probe("blocks.c", line, selectors, hits="all"), adapter=target(optimization)
    )
    assert result["status"] == "completed", result.get("error")
    found = [row for row in result["rows"] if row["selector"] in selectors]
    # The outer `t` holds 1 to 3 here; each block's `static` 5 or more.
    assert not [row for row in found if row.get("value") in ("1", "2", "3")], found
    rows = [row for row in found if "this row is the outer local" in row.get("error", "")]
    assert any(row["hit"] == 1 for row in rows), found
    static = f"the block's `static` `t` (line {line}), "
    listed = ("which the debugger does not list", f"listed separately as `t @ blocks.c:{line}`")
    hedged = {
        f"may not be what the code reads: the code here may name {static}{how}, but the line "
        f"also names `t` {SAME_LINE[marker]}, and Peeky cannot tell whether that code reads "
        "this local: this row is the outer local of that name"
        for how in listed
    }
    for row in rows:
        assert row["error"] in hedged, row
