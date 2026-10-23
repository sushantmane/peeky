"""C++ functions Peeky cannot place among the program's declarations, whose locals it
withholds, and GCC's lambdas and implicit constructors, which it places by their class
(#167). Each case runs with clang++ and with g++. C++ with GCC is refused until #167 is
done, so the g++ cases run with the test-only hatch (PEEKY_TEST_CPP_GCC=1,
tests/cpp_gcc_survey.py) and skip without it, or where no g++ is GCC (macOS)."""

import json
import os
import shutil
import subprocess

import pytest

from peeky.cli import main


def _is_gcc(compiler):
    try:
        version = subprocess.run([compiler, "--version"], capture_output=True, text=True)
    except OSError:
        return False
    return "Free Software Foundation" in version.stdout


def _gxx():
    for name in ("g++", "g++-13", "g++-14"):
        found = shutil.which(name)
        if found and _is_gcc(found):
            return found
    return None


@pytest.fixture(params=["clang++", "g++"])
def compiler(request, tmp_path, monkeypatch):
    """The family of the C++ compiler this case builds with: what the build does, not the
    parameter's name (a lane may set PEEKY_NATIVE_CXX to g++)."""
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    found = os.environ.get("PEEKY_NATIVE_CXX", "clang++") if request.param == "clang++" else _gxx()
    if found is None:
        pytest.skip("needs a g++ that is GCC")
    monkeypatch.setenv("PEEKY_NATIVE_CXX", found)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return "gcc" if _is_gcc(found) else "clang"


def line_of(source, marker):
    return next(n for n, text in enumerate(source.splitlines(), 1) if marker in text)


def probed(capsys, path, line, capture, *more):
    code = main(
        ["probe", str(path), "--at", str(line), "--capture", capture, *more]
        + ["--json", "--quiet", "--no-save"]
    )
    result = json.loads(capsys.readouterr().out)
    if code != 0 and (result.get("error") or {}).get("code") == "UNSUPPORTED":
        # C++ with GCC is refused until #167 is done (PEEKY_TEST_CPP_GCC=1 lifts it in tests).
        pytest.skip(f"waits for #167 (C++ with GCC): {result['error']['message'][:120]}")
    return code, result


UNPLACED = (
    "not shown: Peeky cannot place this function's declarations in the program's sources (its "
    "debug information ties it to none), so it cannot tell whether this is set yet: it may "
    "hold leftovers. Read what the function returns (`step --out`), or stop in a function "
    "`peeky describe` lists"
)

# A `#line` (as bison and flex write) puts `helper`'s declaration in another file, while its
# body runs on lined.cpp's own lines: Peeky's table has no entry for it.
LINED = """#include <cstdio>
__attribute__((noinline)) static void scribble() {
    volatile long junk[64];
    for (int i = 0; i < 64; i++) junk[i] = 0x5150 + i;
}
#line 1 "generated.y"
__attribute__((noinline)) static long helper(long a)
#line 9 "lined.cpp"
{
    long x = a * 2;  // X
    long y = x + 1;
    return y;
}
int main() {
    long seed = 3;
    scribble();
    std::printf("%ld\\n", helper(seed));  // CALL
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_a_function_peeky_cannot_place_withholds_its_locals(compiler, tmp_path, capsys):
    # Before: `x = 281474976709600` and `y = 20879` (clang; g++ alike), leftovers shown as
    # values with no note, as GCC's lambdas and implicit constructors were (#167).
    path = tmp_path / "lined.cpp"
    path.write_text(LINED)
    code, result = probed(capsys, path, line_of(LINED, "// X"), "a,x,y")
    assert code == 0 and result["status"] == "completed", result.get("error")
    rows = {row["selector"]: row for row in result["rows"]}
    for name in ("a", "x", "y"):
        assert rows[name].get("unavailable") and rows[name]["error"] == UNPLACED, rows[name]
    # A change is refused, with what to do instead.
    code, result = probed(capsys, path, line_of(LINED, "// X"), "x", "--set", "x=5")
    error = result["error"]
    assert code == 1 and error["code"] == "MUTATION_REJECTED", result
    assert "cannot place this function's declarations" in error["message"], error
    assert "`peeky describe` lists" in error["hint"] and len(error["hint"]) <= 150, error
    # `main` is placed: its own rules judge it (the `#line` hides where `seed`'s declaration
    # ends, so it is withheld for that), never this one.
    code, result = probed(capsys, path, line_of(LINED, "// CALL"), "seed")
    [row] = result["rows"]
    assert code == 0 and row["error"].startswith("no value yet: declared on line 15"), row
