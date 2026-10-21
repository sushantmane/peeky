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

int main(int argc, char **argv) {
    struct span s = { 1, 2 };
    int x = argc * 3;
    int y = widen(x, &s);  /* CALL */
    printf("Y %d %d\\n", y, x);
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
