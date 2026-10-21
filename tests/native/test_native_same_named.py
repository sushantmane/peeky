"""#158: a function declaring one name twice with different types (in sibling blocks,
or an inner block shadowing an outer one) is changed through the declaration the
debugger shows at the stop, the one whose block holds it, as reads judge it; refused
only where that cannot be singled out."""

import json
import os

import pytest

from peeky.cli import main

# The seventh evaluation's `sched` (C++): the loop's `Task t`, and a later block's
# `const Task &t` (a range-`for`).
SCHED = """#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

struct Task {
    std::string name;
    int prio;
    int deadline;
};

static bool before(const Task &a, const Task &b) {
    if (a.prio != b.prio) return a.prio > b.prio;
    if (a.deadline != b.deadline) return a.deadline > b.deadline;
    return a.name < b.name;
}

int main(int argc, char **argv) {
    std::string input = argc > 1 ? argv[1] : "";
    std::vector<Task> tasks;
    size_t pos = 0;
    while (pos < input.size()) {
        size_t comma = input.find(',', pos);
        if (comma == std::string::npos) comma = input.size();
        std::string item = input.substr(pos, comma - pos);
        size_t a = item.find(':'), b = item.rfind(':');
        Task t{item.substr(0, a), std::atoi(item.substr(a + 1, b - a - 1).c_str()),
               std::atoi(item.substr(b + 1).c_str())};
        tasks.push_back(t);  // PUSH
        pos = comma + 1;
    }
    std::sort(tasks.begin(), tasks.end(), before);
    std::string out;
    for (const Task &t : tasks) out += (out.empty() ? "" : ",") + t.name;
    std::printf("ORDER:%s\\n", out.c_str());
    return 0;
}
"""

SAME = """#include <stdio.h>

struct A { long x; long y; };
struct B { int x; long z; };

__attribute__((noinline)) long use(long v) { return v; }

int main(void) {
    long total = 0;
    {
        struct A s = { 1, 2 };
        total += use(s.x);  // S1
        printf("S1 %ld\\n", s.x);
    }
    {
        struct B s = { 3, 4 };
        total += use(s.x);  // S2
        printf("S2 %d\\n", s.x);
    }
    struct A t = { 5, 6 };  // DECLARED
    {
        struct B t = { 7, 8 };
        total += use(t.x);  // INNER
        printf("INNER %d\\n", t.x);
    }
    total += use(t.x);  // OUTER
    printf("OUTER %ld\\n", t.x);
    return 0;
}
"""

# Two blocks on one line declaring `s` with different types: the build's debug
# information tells them apart by their line only.
AMBIGUOUS = """#include <stdio.h>

struct A { long x; long y; };
struct B { int x; long z; };

__attribute__((noinline)) long use(long v) { return v + 1; }

int main(void) {
    long total = 0;
    { struct A s = { 1, 2 }; total += use(s.x); } { struct B s = { 3, 4 }; total += use(s.x); }  // SAME
    printf("%ld\\n", total);
    return 0;
}
"""


def cpp(source):
    return source.replace("<stdio.h>", "<cstdio>").replace("(void)", "()")


@pytest.fixture
def directory(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def line_of(source, marker):
    return next(n for n, text in enumerate(source.splitlines(), 1) if marker in text)


def probed(capsys, path, line, assignment, capture, *args):
    code = main(
        ["probe", str(path), "--at", str(line), "--set", assignment, "--capture", capture]
        + ["--json", "--quiet", "--no-save", *(arg for value in args for arg in ("--arg", value))]
    )
    return code, json.loads(capsys.readouterr().out)


def run(capsys, *argv):
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(300)
def test_cpp_the_loops_task_is_changed_and_the_program_sees_it(directory, capsys):
    # Refused before the run with "several locals named `t` of different types": at
    # line 32 only the loop's `Task t` is in scope.
    path = directory / "sched.cpp"
    path.write_text(SCHED)
    line = line_of(SCHED, "// PUSH")
    tasks = "build:2:30,test:2:10,deploy:1:5,lint:3:50"
    code, result = probed(capsys, path, line, "t.deadline=-30", "t.deadline", tasks)
    assert code == 0 and result["status"] == "completed", result.get("error")
    [change] = result["interventions"]
    assert (change["before"], change["after"]) == ("30", "-30"), change
    assert "ORDER:lint,test,build,deploy" in result["output"], result["output"]
    # In a session too, where `t` is settable.
    assert main(["session", "start", str(path), "--at", str(line), "--arg", tasks]) == 0
    capsys.readouterr()
    try:
        code, rows = run(capsys, "session", "get", "t.deadline")
        assert rows[0]["value"] == "30" and rows[0]["settable"] is True, rows
        code, result = run(capsys, "session", "set", "t.deadline=-30")
        assert code == 0 and result["after"] == "-30", result
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(300)
@pytest.mark.parametrize("suffix", ["c", "cpp"])
def test_each_block_changes_its_own_local_of_a_shared_name(directory, capsys, suffix):
    source = SAME if suffix == "c" else cpp(SAME)
    path = directory / f"same.{suffix}"
    path.write_text(source)
    # Before the run: each line's block holds one `s`, so the probe runs.
    code, result = probed(capsys, path, line_of(source, "// S2"), "s.x=41", "s.x")
    assert code == 0 and result["status"] == "completed", result.get("error")
    assert "S2 41" in result["output"], result["output"]
    outer = line_of(source, "// DECLARED")
    assert main(["session", "start", str(path), "--at", str(line_of(source, "// S1"))]) == 0
    capsys.readouterr()
    try:
        # Sibling blocks: each `s` in its own.
        assert run(capsys, "session", "set", "s.x=40")[1]["after"] == "40"
        assert main(["session", "continue", "--to", str(line_of(source, "// S2"))]) == 0
        capsys.readouterr()
        assert run(capsys, "session", "set", "s.x=41")[1]["after"] == "41"
        # Nested: the inner `t` is what the code names, so `t` reads and changes it.
        assert main(["session", "continue", "--to", str(line_of(source, "// INNER"))]) == 0
        capsys.readouterr()
        code, rows = run(capsys, "session", "get", "t", f"t @ same.{suffix}:{outer}.x")
        assert code == 0 and rows[0]["type"] == "B" and rows[1]["value"] == "5", rows
        assert run(capsys, "session", "set", "t.x=42")[1]["after"] == "42"
        code, rows = run(capsys, "session", "get", f"t @ same.{suffix}:{outer}.x")
        assert rows[0]["value"] == "5", rows
        # Past the inner block, the outer one.
        assert main(["session", "continue", "--to", str(line_of(source, "// OUTER"))]) == 0
        capsys.readouterr()
        assert run(capsys, "session", "set", "t.x=43")[1]["after"] == "43"
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        printed = json.loads(capsys.readouterr().out)["output"]
        for text in ("S1 40", "S2 41", "INNER 42", "OUTER 43"):
            assert text in printed, printed
    finally:
        main(["session", "close"])
        capsys.readouterr()
    # `describe` says how a change picks among them.
    assert main(["describe", str(path)]) == 0
    described = capsys.readouterr().out
    assert "Locals of one name with different types (main: s, t)" in described, described
    assert "a plain name means the declaration the code names at the stop" in described


@pytest.mark.timeout(300)
@pytest.mark.parametrize("suffix", ["c", "cpp"])
def test_locals_of_one_name_the_debug_information_cannot_tell_apart_are_refused(
    directory, capsys, suffix
):
    source = AMBIGUOUS if suffix == "c" else cpp(AMBIGUOUS)
    path = directory / f"amb.{suffix}"
    path.write_text(source)
    line = line_of(source, "// SAME")
    code, result = probed(capsys, path, line, "s.x=40", "s")
    error = result["error"]
    assert code == 1 and error["code"] == "MUTATION_REJECTED", result
    assert result.get("refused_before_launch") is True, result
    assert f"several locals named `s` of different types on line {line}" in error["message"]


# Review of PR #159: a plain name chose by the debugger's blocks alone.
CONDITIONS = {
    # clang gives a `while`'s condition variable no block of its own: its block's code
    # runs on after the loop, where the code names the outer `t`.
    "while.cpp": """#include <cstdio>
static int next_val(int &n) { return n-- > 0 ? n + 10 : 0; }
int main() {
    int total = 0;
    {
        int t = 1;
        {
            int n = 2;
            while (long t = next_val(n)) { total += (int)t; }
            total += t;  // AFTER
            std::printf("t=%d total=%d\\n", t, total);
        }
    }
    return 0;
}
""",
    # Nor a `switch`'s (a C++17 init-statement here).
    "switch.cpp": """#include <cstdio>
int main(int argc, char **argv) {
    int total = 0;
    {
        int t = 1;
        {
            switch (short t = (short)(argc + 4); t) {
            case 5:
                total += t;
                break;
            default:
                break;
            }
            total += t;  // AFTER
            std::printf("t=%d total=%d\\n", t, total);
        }
    }
    return total;
}
""",
}


def captured(capsys, path, line, capture, *more):
    code = main(
        ["probe", str(path), "--at", str(line), "--capture", capture, *more]
        + ["--json", "--quiet", "--no-save"]
    )
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(300)
@pytest.mark.parametrize("name", sorted(CONDITIONS))
def test_cpp_a_condition_variable_names_nothing_after_its_statement(directory, capsys, name):
    # `t` read the dead condition variable (`0`, `5`), and `--set t=500` changed it
    # while the program printed the outer `t=1`.
    source = CONDITIONS[name]
    path = directory / name
    path.write_text(source)
    line = line_of(source, "// AFTER")
    code, result = captured(capsys, path, line, "t", "--set", "t=500")
    assert code == 0 and result["status"] == "completed", result.get("error")
    observed = next(row for row in result["rows"] if row["phase"] == "observed")
    assert (observed["type"], observed["value"]) == ("int", "1"), observed
    assert "t=500" in result["output"], result["output"]


CALLERS = """#include <stdio.h>

__attribute__((noinline)) static void callee(long v) {
    printf("callee %ld\\n", v);  // CALLEE
}
__attribute__((noinline)) static void scribble(void) {
    volatile long junk[64];
    for (int i = 0; i < 64; i++) junk[i] = 0x5150 + i;
}
__attribute__((noinline)) static long shadowed(void) {
    int t = 10;
    {
        long t = 20;
        callee(t);  // SHADOWED
    }
    return t;
}
__attribute__((noinline)) static long jumped(int k) {
    long total = 0;
    if (k) goto skip;
    {
        long t = 7;
        total += t;
skip:
        callee(total);  // JUMPED
    }
    return total;
}
int main(int argc, char **argv) {
    scribble();
    long a = shadowed();
    scribble();
    long b = jumped(argc);
    printf("%ld %ld\\n", a, b);
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_c_a_caller_frame_is_judged_where_its_call_is(directory, capsys):
    # A caller frame's address is where its call returns: past the block when the call
    # is its last statement. `t` read the outer `int t = 10`; and a `goto` past
    # `long t = 7;` showed `t`'s leftovers in the caller (on main too).
    path = directory / "callers.c"
    path.write_text(CALLERS)
    assert main(["session", "start", str(path), "--at", str(line_of(CALLERS, "// CALLEE"))]) == 0
    capsys.readouterr()
    try:
        code, rows = run(capsys, "session", "get", "t", "--frame", "1")
        assert rows[0]["line"] == line_of(CALLERS, "// SHADOWED"), rows
        assert (rows[0]["type"], rows[0]["value"]) == ("long", "20"), rows
        assert main(["session", "continue"]) == 0
        capsys.readouterr()
        code, rows = run(capsys, "session", "get", "t", "total", "--frame", "1")
        assert rows[0]["line"] == line_of(CALLERS, "// JUMPED"), rows
        assert "value" not in rows[0] and "can reach the label" in rows[0]["error"], rows
        assert rows[1]["value"] == "0", rows
    finally:
        main(["session", "close"])
        capsys.readouterr()


BEFORE = """#include <stdio.h>
#define P(tag, v) printf("%s=%ld\\n", tag, (long)(v))
static long f(int t) {
    {
        long t = 99;
        P("B", t);  // PARAMETER
    }
    return t;
}
int main(int argc, char **argv) {
    int t = 1;
    for (int t = 10; t < 11; t++) {
        P("B", t);  // LOOP
        long t = 20;
        P("C", t);
    }
    {
        long t = 40;
        if (argc > 0) {
            P("G", t);  // BLOCK
            int t = 50;
            P("H", t);
        }
    }
    return (int)f(argc + 4) - 5 + t - 1;
}
"""


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "marker, value",
    [
        ("// LOOP", "10"),  # before the body's `long t`, the `for`'s
        ("// BLOCK", "40"),  # before the `if` block's `int t`, the block's
        ("// PARAMETER", "99"),  # a block's `long t`, shadowing the parameter
    ],
)
def test_c_a_name_is_the_one_declared_above_the_stop(directory, capsys, marker, value):
    # Before an inner declaration `t` was mapped to it, and withheld as declared below
    # (the program reads the outer one); a shadowed parameter left `t` unmatched.
    path = directory / "before.c"
    path.write_text(BEFORE)
    code, result = captured(capsys, path, line_of(BEFORE, marker), "t", "--set", "t=77")
    assert code == 0 and result["status"] == "completed", result.get("error")
    [observed] = [row for row in result["rows"] if row["phase"] == "observed"]
    assert observed["value"] == value, observed
    tag = "G" if marker == "// BLOCK" else "B"
    assert f"{tag}=77" in result["output"], result["output"]


CLOSING = """#include <cstdio>
#include <string>
struct Task { int id; long deadline; std::string name; };
static int run(int argc) {
    int t = argc;
    {
        Task t{1, 10, "x"};
        std::printf("%d %ld %s\\n", t.id, t.deadline, t.name.c_str());
    }
    return t;
}  // CLOSING
int main(int argc, char **argv) { return run(argc) - 1; }
"""


@pytest.mark.timeout(300)
def test_cpp_a_line_only_an_exception_runs_singles_out_nothing(directory, capsys):
    # The function's closing line has code only in exception landing pads, inside the
    # block's ranges: a breakpoint there is never reached, so it singles out no `t`
    # (the probe ran to NO_STOP).
    path = directory / "closing.cpp"
    path.write_text(CLOSING)
    code, result = probed(capsys, path, line_of(CLOSING, "// CLOSING"), "t.deadline=5", "t")
    assert code == 1 and result.get("refused_before_launch") is True, result
    assert "several locals named `t` of different types" in result["error"]["message"]
