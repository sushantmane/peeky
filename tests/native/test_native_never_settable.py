"""#138: C and C++ probes refuse a `--set` no stop of the build can apply before the
program runs: every change in an optimized build, a field through a pointer at -O0. A
-O0 parameter (spilled to the frame) is still changed."""

import os

import pytest

from peeky.native import NativeTarget
from peeky.probe import Probe, run_probe

SOURCE = """#include <stdio.h>

struct span { int start; int end; };

__attribute__((noinline)) static int widen(int n, struct span *ps) {
    int total = n * 2;  /* WIDEN */
    return total + ps->end;
}

__attribute__((noinline)) static int arrs(void) {
    struct span sp[2] = { { 1, 2 }, { 3, 4 } };
    int grid[2][2] = { { 1, 2 }, { 3, 4 } };
    int r = sp[1].end + grid[1][0];  /* ARR */
    return r;
}

int main(int argc, char **argv) {
    struct span s = { 1, 2 };
    int x = argc * 3;
    int y = widen(x, &s);  /* CALL */
    printf("Y %d %d\\n", y, x);
    printf("ARRS %d\\n", arrs());
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
        sources=(f"reg.{'cpp' if language == 'cpp' else 'c'}",),
        language=language,
        compiler=compiler or ("clang++" if language == "cpp" else "clang"),
        adapter=dap,
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
        optimization=optimization,
    )


def line_of(marker):
    return next(i for i, text in enumerate(SOURCE.splitlines(), 1) if marker in text)


def refused_before_launch(result):
    error = result.get("error") or {}
    assert result["status"] == "failed" and error.get("code") == "MUTATION_REJECTED", result
    assert result["hits"] == 0 and not result.get("output") and not result["rows"], result
    assert result.get("refused_before_launch") is True, result
    assert "Refused before the program runs" in error["message"], error
    return error["message"]


@pytest.mark.timeout(240)
@pytest.mark.parametrize("language", ["c", "cpp"])
def test_sets_no_stop_allows_are_refused_before_the_run(tmp_path, language):
    name = f"reg.{'cpp' if language == 'cpp' else 'c'}"
    (tmp_path / name).write_text(SOURCE)
    widen, call = line_of("WIDEN"), line_of("CALL")
    # -O1 keeps `x` in registers (a location list): every change is refused there.
    optimized = run_probe(
        tmp_path, Probe(name, call, ("x",), (("x", "5"),)), adapter=target(language, "O1")
    )
    message = refused_before_launch(optimized)
    assert "refused in an optimized build (-O1)" in message, message
    assert "gives `x` no stack slot (a register or a location list)" in message, message
    # -O0: a field through a pointer can never change; the parameter can.
    debug = target(language, "O0")
    through = run_probe(tmp_path, Probe(name, widen, ("n",), (("ps.start", "5"),)), adapter=debug)
    assert "`ps` is a pointer" in refused_before_launch(through)
    changed = run_probe(tmp_path, Probe(name, widen, ("n",), (("n", "5"),)), adapter=debug)
    assert changed["status"] == "completed", changed.get("error")
    assert [i["after"] for i in changed["interventions"]] == ["5"]
    assert "Y 12 3" in changed["output"], changed["output"]


@pytest.mark.timeout(240)
@pytest.mark.parametrize("language", ["c", "cpp"])
def test_an_index_is_judged_as_the_stop_reads_it(tmp_path, language):
    # Review of #138: `sp[-1].end` and `sp[01].end` change `sp[1].end` at the stop (the
    # walk counts `-1` from the array's end and reads `01` as 1), but were refused before
    # the run ("`sp` is an array: select an element as `sp[0]`").
    name = f"reg.{'cpp' if language == 'cpp' else 'c'}"
    (tmp_path / name).write_text(SOURCE)
    debug, at = target(language, "O0"), line_of("ARR")
    for selector in ("sp[-1].end", "sp[01].end"):
        result = run_probe(tmp_path, Probe(name, at, ("r",), ((selector, "9"),)), adapter=debug)
        assert result["status"] == "completed", (selector, result.get("error"))
        assert "ARRS 12" in result["output"], (selector, result["output"])
    # A field the element's type lacks is still refused before the run.
    result = run_probe(tmp_path, Probe(name, at, ("r",), (("sp[-1].mid", "9"),)), adapter=debug)
    assert "lists no field `mid`" in refused_before_launch(result)


INDEXES = """#include <stdio.h>

struct inner { int arr[3]; };
struct grid { int g[2][4]; };

__attribute__((noinline)) static int run(void) {
    int arr[4] = { 1, 2, 3, 4 };
    short sh[3] = { 5, 6, 7 };
    struct inner np = { { 8, 9, 10 } };
    struct grid gr = { { { 1, 2, 3, 4 }, { 5, 6, 7, 8 } } };
    int r = arr[3] + sh[2] + np.arr[1] + np.arr[2] + gr.g[1][0];  /* IDX */
    return r;
}

int main(void) {
    printf("RUN %d\\n", run());
    return 0;
}
"""


@pytest.mark.timeout(240)
@pytest.mark.parametrize("language", ["c", "cpp"])
def test_a_final_index_is_set_as_it_is_read_and_marked(tmp_path, language):
    # Review of #138: `arr[03]`, `sh[-1]`, `np.arr[-1]`, `gr.g[1][-4]` read and were
    # marked [settable], then `set` refused them ("a negative index can be changed only
    # in a Java array", "'[03]' is not a field or element the debugger lists there").
    name = f"reg.{'cpp' if language == 'cpp' else 'c'}"
    (tmp_path / name).write_text(INDEXES)
    at = next(i for i, text in enumerate(INDEXES.splitlines(), 1) if "IDX" in text)
    debug = target(language, "O0")
    selectors = ("arr[03]", "sh[-1]", "np.arr[01]", "np.arr[-1]", "gr.g[1][-4]")
    marked = run_probe(tmp_path, Probe(name, at, selectors), adapter=debug)
    assert [row.get("settable") for row in marked["rows"]] == [True] * 5, marked.get("error")
    # 4 + 7 + 9 + 10 + 5 = 35, each one changed to 9.
    for selector, total in zip(selectors, (40, 37, 35, 34, 39)):
        result = run_probe(tmp_path, Probe(name, at, (), ((selector, "9"),)), adapter=debug)
        assert result["status"] == "completed", (selector, result.get("error"))
        assert f"RUN {total}" in result["output"], (selector, result["output"])


@pytest.mark.timeout(240)
@pytest.mark.parametrize("language", ["c", "cpp"])
def test_a_previews_fields_are_marked_as_set_treats_them(tmp_path, language):
    # #138 with #146: a struct's preview keeps its fields' marks, as `set` judges them:
    # the frame's own struct's field is [settable] and changed; one reached through a
    # pointer is not, and is refused before the run.
    name = f"reg.{'cpp' if language == 'cpp' else 'c'}"
    (tmp_path / name).write_text(SOURCE)
    debug, widen, call = target(language, "O0"), line_of("WIDEN"), line_of("CALL")
    marked = run_probe(tmp_path, Probe(name, widen, ("ps[0]", "ps[0].end")), adapter=debug)
    rows = {row["selector"]: row for row in marked["rows"]}
    assert rows["ps[0].end"]["value"] == "2" and rows["ps[0].end"]["settable"] is False, rows
    result = run_probe(tmp_path, Probe(name, widen, (), (("ps[0].end", "9"),)), adapter=debug)
    assert "`ps` is a pointer" in refused_before_launch(result)
    marked = run_probe(tmp_path, Probe(name, call, ("s", "s.end")), adapter=debug)
    rows = {row["selector"]: row for row in marked["rows"]}
    assert rows["s"]["value"] == "{start = 1, end = 2}", rows
    assert rows["s.end"]["settable"] is True and "not_settable" not in rows["s.end"], rows
    changed = run_probe(tmp_path, Probe(name, call, (), (("s.end", "9"),)), adapter=debug)
    assert changed["status"] == "completed", changed.get("error")
    assert "Y 15 3" in changed["output"], changed["output"]


CONSTS = """#include <stdio.h>

int main(int argc, char **argv) {
    const long tracks = 3 + argc;
    int played = 0;
    for (int a = 0; a < 3; a++) {
        played += a;  /* LOOP */
    }
    printf("P %d %ld\\n", played, tracks);
    return 0;
}
"""


@pytest.mark.timeout(240)
@pytest.mark.parametrize("language", ["c", "cpp"])
def test_a_root_of_no_writable_integer_type_is_refused_before_the_run(tmp_path, language):
    # Twelfth evaluation's preparation: `--set-at LINE@2 tracks=2` of a `const long` ran to
    # its stop, then was refused there, though the debug information gives its type.
    name = f"reg.{'cpp' if language == 'cpp' else 'c'}"
    (tmp_path / name).write_text(CONSTS)
    at = next(i for i, text in enumerate(CONSTS.splitlines(), 1) if "LOOP" in text)
    debug = target(language, "O0")
    probe = Probe(name, at, ("played",), set_at=((at, 2, "tracks", "2"),))
    message = refused_before_launch(run_probe(tmp_path, probe, adapter=debug))
    assert "only writable integer locals of these types support native mutation" in message
    assert "`tracks` has type `const long" in message, message
    # A writable integer is still changed at its stop.
    probe = Probe(name, at, ("played",), set_at=((at, 2, "played", "10"),))
    changed = run_probe(tmp_path, probe, adapter=debug)
    assert changed["status"] == "completed", changed.get("error")
    assert "P 13 4" in changed["output"], changed["output"]
