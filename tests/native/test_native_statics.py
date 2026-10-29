"""C locals in static storage (#364): a `static` local, and a `const` aggregate GCC builds
at compile time in read-only data. lldb-dap lists none of them among a frame's locals, so
each was `not found`. Peeky reads the one the code at the stop names, from the debug
information's declaration, and never changes one."""

import json
import os
import re
import shutil

import pytest
from native_compilers import is_gcc

from peeky.cli import main

STATICS = """#include <stdio.h>

struct span { long start, end; };

__attribute__((noinline)) long tick(long x) {
    long seen = x;  // ABOVE
    static long calls = 0;
    static const struct span cs = {8, 9};
    static struct span st = {1, 2};
    const struct span ls = {4, 5};
    calls += x;
    {
        static long calls = 100;
        calls++;
        printf("%ld\\n", calls);  // INNER
    }
    {
        static long count = 7;
        seen += count;
    }
    printf("%ld %ld %ld %ld %ld\\n", calls, cs.end, st.end, ls.end, seen);  // OUTER
    return calls;
}

int main(void) {
    tick(1);
    tick(2);
    return 0;
}
"""


@pytest.fixture(params=["clang", "gcc"])
def directory(request, tmp_path, monkeypatch):
    """A directory to build in, with PEEKY_NATIVE_CC set to a compiler of the family: GCC
    places a `const` local aggregate in read-only data, clang in the frame. The GCC case
    skips where no gcc is GCC (macOS), and the clang case where the lane builds C with GCC."""
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    default = os.environ.get("PEEKY_NATIVE_CC", "clang")
    if request.param == "clang" and is_gcc(default):
        pytest.skip("this lane builds C with GCC; the clang lanes run the clang case")
    if request.param == "gcc" and not is_gcc(default):
        gcc = shutil.which("gcc")
        if not gcc or not is_gcc(gcc):
            pytest.skip("needs GCC")
        monkeypatch.setenv("PEEKY_NATIVE_CC", gcc)
        monkeypatch.delenv("PEEKY_NATIVE_SYSROOT", raising=False)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def line_of(source, marker):
    return next(n for n, text in enumerate(source.splitlines(), 1) if marker in text)


def rows_at(capsys, path, line, selectors, hit=1):
    command = ["probe", str(path), "--at", str(line), "--capture", ",".join(selectors)]
    code = main([*command, "--hits", str(hit), "--json", "--quiet", "--no-save"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result
    return {row["selector"]: row for row in result["rows"] if row["hit"] == hit}, result


def refused(capsys, assignment, phrase):
    assert main(["session", "set", assignment, "--json"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "MUTATION_REJECTED" and phrase in error["message"], error


@pytest.mark.timeout(300)
def test_a_static_local_is_read_where_the_code_names_it(directory, capsys):
    # #364: `calls`, `cs` and `st` were `not found: tick declares 'calls' in static
    # storage ... Peeky cannot read it here`. In the inner block, `calls` is its own.
    path = directory / "statics.c"
    path.write_text(STATICS)
    inner, outer = line_of(STATICS, "// INNER"), line_of(STATICS, "// OUTER")
    rows, _ = rows_at(capsys, path, inner, ["calls", "cs", "cs.end", "st.end", "ls", "x"], hit=2)
    assert rows["calls"]["value"] == "102", rows
    assert rows["cs"]["value"] == "{start = 8, end = 9}" and rows["cs.end"]["value"] == "9"
    assert rows["st.end"]["value"] == "2" and rows["ls"]["value"] == "{start = 4, end = 5}"
    # Static storage is no frame's own: never settable, and the row says why.
    for selector in ("calls", "cs", "cs.end", "st.end"):
        assert rows[selector].get("settable") is not True, rows[selector]
        assert rows[selector]["not_settable"] == "static", rows[selector]
    assert rows["x"]["settable"] is True
    rows, result = rows_at(capsys, path, outer, ["calls", "count", "seen"], hit=2)
    assert rows["calls"]["value"] == "3" and rows["seen"]["value"] == "9", rows
    # Its block has ended: the code here can't name it.
    count = line_of(STATICS, "static long count")
    assert rows["count"]["error"].startswith(
        f"not found: tick declares 'count' in static storage (a `static` local, or a `const` "
        f"one the compiler placed there), at line {count}, in a block this stop is outside "
        f"of, so the code here doesn't name it; read it in that block, below line {count}"
    ), rows["count"]
    assert result["output"].splitlines()[-2:] == ["102", "3 9 2 5 9"], result["output"]
    # Above its declaration, the code doesn't name it yet.
    rows, _ = rows_at(capsys, path, line_of(STATICS, "// ABOVE"), ["calls"])
    declared = line_of(STATICS, "static long calls = 0")
    assert (
        f"at line {declared}, below this stop, so the code here doesn't name it yet; read "
        f"it below line {declared}" in rows["calls"]["error"]
    ), rows["calls"]


@pytest.mark.timeout(240)
def test_describe_marks_a_static_local_static(directory, capsys):
    # Eval 13 (#385): `describe` listed `now (struct vitals)`, a `static` local, as if it
    # were the frame's own; its rows say `[not settable: static]`.
    path = directory / "statics.c"
    path.write_text(STATICS)
    assert main(["describe", str(path), "--json"]) == 0
    scopes = json.loads(capsys.readouterr().out)["scopes"]
    tick = next(scope for scope in scopes if scope["scope"] == "tick")
    statics = set(tick.get("statics", ()))
    assert {"calls", "count", "cs", "st"} <= statics and not {"seen", "x"} & statics, tick
    # GCC builds `ls` at compile time, in read-only data, which its rows say too.
    assert ("ls" in statics) is is_gcc(os.environ["PEEKY_NATIVE_CC"]), tick
    assert main(["describe", str(path)]) == 0
    text = capsys.readouterr().out
    assert re.search(r"names in tick \(statics\.c:[0-9-]+\): calls \(long( int)?, static\)", text)
    assert re.search(r", seen \(long( int)?\)", text), text


@pytest.mark.timeout(300)
def test_a_static_local_is_never_changed(directory, capsys):
    path = directory / "statics.c"
    path.write_text(STATICS)
    inner = line_of(STATICS, "// INNER")
    # Refused before the program runs: no stop can change it.
    command = ["probe", str(path), "--at", str(inner), "--set", "calls=5", "--json", "--quiet"]
    assert main([*command, "--no-save"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert "`calls` is a `static` local, kept in the program's static storage" in error["message"]
    assert main(["session", "start", str(path), "--at", str(inner)]) == 0
    capsys.readouterr()
    try:
        refused(capsys, "calls=5", "'calls' cannot be changed: tick keeps it in the program's")
        refused(capsys, "st.end=5", "`st` is a `static` local, kept in the program's static")
        refused(capsys, "cs.end=5", "`cs` is const")
        refused(capsys, "ls.end=5", "`ls` is const")
        assert main(["session", "get", "calls", "st", "--json"]) == 0
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
        assert rows["calls"]["value"] == "101" and rows["st"]["value"] == "{start = 1, end = 2}"
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        # Every change was refused: the program ran as written.
        lines = json.loads(capsys.readouterr().out)["output"].splitlines()
        assert lines == ["101", "1 9 2 5 8", "102", "3 9 2 5 9"], lines
    finally:
        main(["session", "close"])
        capsys.readouterr()


# Review of PR #370: a function inlined twice and called once out of line, optimized: each
# copy has its own code for the block declaring `hits`.
INLINED = """#include <stdio.h>

__attribute__((noinline)) long use(long v) { return v + 1; }

static inline long bump(long v) {
    long r = v;
    if (v > 0) {
        static long hits = 0;
        hits += v;
        r += use(hits);  // HERE
    }
    return r;
}

long (*volatile keep)(long) = bump;

int main(int argc, char **argv) {
    long a = bump(argc);
    long b = keep(argc + 1);
    long c = bump(argc + 2);
    printf("%ld %ld %ld\\n", a, b, c);
    return 0;
}
"""


@pytest.mark.timeout(300)
@pytest.mark.parametrize("optimization", ["O1", "O2"])
def test_a_static_of_an_inlined_function_is_never_refused_for_another_copy(
    directory, capsys, optimization
):
    # In each copy's code the declaration the code names is `hits`, whichever copy's block
    # the stop is in: read where the debugger lists it (clang's out-of-line copy), with
    # the optimized build's note; else the note says the debugger doesn't list it there.
    (directory / "inlined.c").write_text(INLINED)
    config = directory / "inlined.json"
    config.write_text(
        json.dumps(
            {
                "language": "c",
                "sources": ["inlined.c"],
                "optimization": optimization,
                "compiler": os.environ.get("PEEKY_NATIVE_CC", "clang"),
                "adapter": os.environ["PEEKY_NATIVE_DAP"],
                "dwarfdump": os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
                "dsymutil": os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
                "sysroot": os.environ.get("PEEKY_NATIVE_SYSROOT"),
            }
        )
    )
    at = f"inlined.c:{line_of(INLINED, '// HERE')}"
    command = ["probe", str(directory), "--config", str(config), "--at", at]
    code = main([*command, "--capture", "hits", "--hits", "all", "--json", "--quiet"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["rows"], result
    declared = line_of(INLINED, "static long hits")
    for row in result["rows"]:
        if "value" in row:
            assert row["value"] in ("0", "1", "3", "6"), row
            assert row["note"].startswith(f"the build is optimized ({optimization})"), row
        else:
            # Apple's LLDB names an optimized frame `bump [opt]`.
            assert re.match(
                r"not found: bump(?: \[opt\])? declares 'hits' in static storage \(a `static` "
                r"local, or a `const` one the compiler placed there\), at line "
                rf"{declared}, but the debugger doesn't list it for this frame",
                row["error"],
            ), row
