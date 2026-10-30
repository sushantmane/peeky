"""C++ functions Peeky cannot place among the program's declarations, whose locals it
withholds, and GCC's lambdas and implicit constructors, which it places by their class.
Each case runs with clang++ and with g++: the g++ cases skip where no g++ is GCC
(macOS), and the clang cases where the lane builds C++ with g++."""

import json
import os

import pytest
from native_compilers import cxx_for, is_gcc

from peeky.cli import main


@pytest.fixture(params=["clang++", "g++"])
def compiler(request, tmp_path, monkeypatch):
    """The family of the C++ compiler this case builds with: what the build does, not the
    parameter's name."""
    found, why = cxx_for("clang" if request.param == "clang++" else "gcc")
    if found is None:
        pytest.skip(why)
    monkeypatch.setenv("PEEKY_NATIVE_CXX", found)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return "gcc" if is_gcc(found) else "clang"


def line_of(source, marker):
    return next(n for n, text in enumerate(source.splitlines(), 1) if marker in text)


def probed(capsys, path, line, capture, *more):
    code = main(
        ["probe", str(path), "--at", str(line), "--capture", capture, *more]
        + ["--json", "--quiet", "--no-save"]
    )
    return code, json.loads(capsys.readouterr().out)


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
    # Not `x = 281474976709600` and `y = 20879` (clang; g++ alike), leftovers shown as
    # values with no note.
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


# The same helper, forced inline into `main`. Its frame shares `main`'s code, where Peeky
# finds `main`'s entry, which must not leave its locals unwithheld.
INLINED = """#include <cstdio>
__attribute__((noinline)) static long twice(long v) {
    long w = v * 2;
    return w;  // TWICE
}
#line 1 "generated.y"
static inline __attribute__((always_inline)) long helper(long a)
#line 9 "inlined.cpp"
{
    long x = a * 3;  // X
    long y = twice(x);
    return y;
}
int main() {
    long seed = 3;
    long r = helper(seed);  // CALL
    std::printf("%ld\\n", r);
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_an_inlined_function_peeky_cannot_place_withholds_its_locals(compiler, tmp_path, capsys):
    # Not `x = 281474976709728`, `y = 4294967295` (clang; g++ alike) in the inlined
    # `helper`, as values with no note.
    path = tmp_path / "inlined.cpp"
    path.write_text(INLINED)
    code, result = probed(capsys, path, line_of(INLINED, "// X"), "a,x,y")
    assert code == 0 and result["status"] == "completed", result.get("error")
    for row in result["rows"]:
        assert row.get("unavailable") and row["error"] == UNPLACED, row
    # Below a call it makes, its frame is withheld too. The callee's and `main`'s are judged
    # by their own rules (the `#line` hides where their declarations end, so those
    # withhold `w` and `seed`), never this one.
    line = line_of(INLINED, "// TWICE")
    code = main(["session", "start", str(path), "--at", str(line), "--json"])
    started = json.loads(capsys.readouterr().out)
    assert code == 0, started
    try:
        code, rows = run(capsys, "session", "get", "x", "--frame", "1")
        assert [row.get("error") for row in rows] == [UNPLACED], rows
        for frame, name, function in ((0, "w", "twice(long)"), (2, "seed", "main")):
            code, rows = run(capsys, "session", "get", name, "--frame", str(frame))
            [row] = rows
            assert row["function"].endswith(function), row
            assert row["error"].startswith("no value yet: "), row
            assert "Peeky cannot place" not in row["error"], row
            assert "which function" not in row["error"], row
    finally:
        main(["session", "close"])
        capsys.readouterr()


# At -O1, `wrap`'s inlined code ends with its call to `twice`, so
# `wrap`'s frame returns to the first address past that code, `run`'s; and `run`'s frame
# is at the start of `wrap`'s code (lldb-dap 18 and 20, clang and GCC alike). Each frame's
# code changes at its address: its line and name tell whose it is.
LAST_CALL = """#include <cstdio>
__attribute__((noinline)) long twice(long v) {
    long w = v * 2;
    return w;  // TWICE
}
static inline __attribute__((always_inline)) long wrap(long a) {
    return twice(a + 1);  // WRAP
}
__attribute__((noinline)) long run(long seed) {
    long r = wrap(seed);  // RUN
    return r + seed;
}
int main(int argc, char **) {
    std::printf("%ld\\n", run(argc + 2));
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_frames_whose_code_changes_at_their_address_are_told_apart(compiler, tmp_path, capsys):
    (tmp_path / "last.cpp").write_text(LAST_CALL)
    config = tmp_path / "native.json"
    config.write_text(
        json.dumps(
            {
                "language": "cpp",
                "sources": ["last.cpp"],
                "compiler": os.environ["PEEKY_NATIVE_CXX"],
                "adapter": os.environ["PEEKY_NATIVE_DAP"],
                "dwarfdump": os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
                "dsymutil": os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
                "sysroot": os.environ.get("PEEKY_NATIVE_SYSROOT"),
                "optimization": "O1",
            }
        )
    )
    at = f"last.cpp:{line_of(LAST_CALL, '// TWICE')}"
    code = main(["session", "start", str(tmp_path), "--config", str(config), "--at", at, "--json"])
    started = json.loads(capsys.readouterr().out)
    assert code == 0, started
    try:
        frames = [(1, "a", "wrap", "// WRAP")]
        if compiler == "gcc":
            # Clang's build has no code on `run`'s call line, which Peeky refuses to read
            # a frame on; GCC's has a row there, at `wrap`'s start.
            frames.append((2, "seed", "run", "// RUN"))
        for frame, name, function, line in frames:
            code, rows = run(capsys, "session", "get", name, "--frame", str(frame))
            [row] = rows
            assert row["function"].lstrip(":").startswith(function), row
            assert row["location"].endswith(f"last.cpp:{line_of(LAST_CALL, line)}"), row
            # Read, or withheld by the optimized build's own rules: never as a frame whose
            # function Peeky cannot tell.
            error = row.get("error", "")
            assert row.get("value") == "3" or "optimized" in error, row
            assert "which function" not in error and "cannot place" not in error, row
    finally:
        main(["session", "close"])
        capsys.readouterr()


def run(capsys, *argv):
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


SCRIBBLE = """__attribute__((noinline)) static void scribble() {
    volatile long junk[128];
    for (int i = 0; i < 128; i++) junk[i] = 0x5150 + i;
}
"""

LAMBDA_LOCALS = (
    "#include <cstdio>\n"
    + SCRIBBLE
    + """int main() {
    long base = 5;
    auto f = [&](long a) {
        long x = a * 2;  // X
        long y = x + base;
        return y;
    };
    scribble();
    long r = f(3);
    std::printf("%ld\\n", r);
    return 0;
}
"""
)


@pytest.mark.timeout(300)
def test_a_lambdas_locals_are_judged_by_its_own_declarations(compiler, tmp_path, capsys):
    # GCC gives a lambda's `operator()` no source, so Peeky's rules skipped it: `x` and `y`
    # showed leftovers (`281474976709584`, `187649984432472`) with no note before
    # `long x = a * 2;` ran, where clang withholds them.
    path = tmp_path / "lambda.cpp"
    path.write_text(LAMBDA_LOCALS)
    line = line_of(LAMBDA_LOCALS, "// X")
    code, result = probed(capsys, path, line, "a,x,y")
    assert code == 0 and result["status"] == "completed", result.get("error")
    rows = {row["selector"]: row for row in result["rows"]}
    assert rows["a"]["value"] == "3", rows["a"]
    assert rows["x"]["error"] == (
        f"no value yet: declared on line {line}, which has not run yet at this stop (its first "
        "instruction), so not initialized: its storage holds leftovers; step once, or stop "
        f"after line {line}, to read it"
    ), rows["x"]
    assert rows["y"]["error"] == (
        f"no value yet: declared at line {line + 1}, below this stop, so not initialized yet: "
        f"its storage holds leftovers; stop after line {line + 1} to read it"
    ), rows["y"]
    # `describe` lists the lambda, with its parameter and locals.
    code, described = run(capsys, "describe", str(path))
    scopes = {scope["scope"]: scope for scope in described["scopes"]}
    assert code == 0 and {"a", "x", "y"} <= set(scopes["main::operator()"]["names"]), scopes


UNNAMED_CONSTRUCTOR = (
    """#include <cstdio>
__attribute__((noinline)) long twice(long v) {
    long w = v * 2;
    return w;  // TWICE
}
"""
    + SCRIBBLE
    + """__attribute__((noinline)) static long nested(long seed) {
    struct {
        long s = twice(13);
        long t = twice(14);
    } anon;
    return anon.t + seed;
}
int main() {
    scribble();
    std::printf("%ld\\n", nested(1));
    return 0;
}
"""
)


@pytest.mark.timeout(300)
def test_an_unnamed_structs_implicit_constructor_withholds_this(compiler, tmp_path, capsys):
    # GCC names it `<constructor>`, with no source: frame 1's `this.t` holds `20943`, a
    # leftover of `scribble()`, while `t = twice(14)` is still running.
    path = tmp_path / "unnamed.cpp"
    path.write_text(UNNAMED_CONSTRUCTOR)
    line = line_of(UNNAMED_CONSTRUCTOR, "// TWICE")
    code = main(["session", "start", str(path), "--at", str(line), "--hit", "2", "--json"])
    started = json.loads(capsys.readouterr().out)
    assert code == 0, started
    try:
        code, rows = run(capsys, "session", "get", "this", "this.t", "--frame", "1")
        shown = {row["selector"]: row for row in rows}
        name = shown["this"]["function"]
        assert name == ("<constructor>" if compiler == "gcc" else "nested(long)::$_0::$_0()")
        assert "value" not in shown["this"] and "value" not in shown["this.t"], rows
        assert f"the object this constructor ({name}) is building" in shown["this"]["error"], rows
    finally:
        main(["session", "close"])
        capsys.readouterr()


GENERIC = """#include <cstdio>
int main() {
    auto twice = [](auto x) { return x * 2; };
    int s = twice(3) + (int)twice(4L);
    std::printf("%d\\n", s);  // S
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_a_generic_lambda_is_no_class_its_function_runs(compiler, tmp_path, capsys):
    # Clang and GCC name a generic lambda's call operator `operator()<int>`, a lambda's
    # too: its closure is no class `main` defines, and `s` is not withheld as "outside its
    # function's own lines".
    path = tmp_path / "generic.cpp"
    path.write_text(GENERIC)
    code, result = probed(capsys, path, line_of(GENERIC, "// S"), "s")
    assert code == 0 and [row.get("value") for row in result["rows"]] == ["14"], result


RANGE_LAMBDA = """#include <cstdio>
#include <utility>
#include <vector>
template <class F> std::vector<std::pair<long, long>> make(F f) { return {{f(40), 2}, {f(41) + 1, 3}}; }
__attribute__((noinline)) long id(long v) { return v; }  // ID
int main() {
    long t = 1;
    for (auto [t, u] : make([&](long t) { return id(t); })) {
        std::printf("FA=%ld\\n", t + 0 * u);
    }
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_a_lambdas_own_parameter_is_judged_as_its_own(compiler, tmp_path, capsys):
    # GCC's lambda's frame is judged by its own declarations, not `main`'s: its own
    # parameter `t` (40) is not withheld as "the code here names the structured binding
    # `t`".
    path = tmp_path / "range_lambda.cpp"
    path.write_text(RANGE_LAMBDA)
    line = line_of(RANGE_LAMBDA, "// ID")
    code = main(["session", "start", str(path), "--at", str(line), "--json"])
    started = json.loads(capsys.readouterr().out)
    assert code == 0, started
    try:
        code, rows = run(capsys, "session", "get", "t", "--frame", "1")
        assert code == 0 and [row.get("value") for row in rows] == ["40"], rows
    finally:
        main(["session", "close"])
        capsys.readouterr()
