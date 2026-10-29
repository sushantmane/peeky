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
    # Refused before the program runs: exit 2, as any refusal (#165, finding 14).
    assert code == 2 and error["code"] == "MUTATION_REJECTED", result
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
def test_cpp_a_line_only_an_exception_runs_singles_out_nothing(directory, cxx, capsys):
    # The function's closing line has code only in exception landing pads, inside the
    # block's ranges: a breakpoint there is never reached, so it singles out no `t`
    # (the probe ran to NO_STOP).
    path = directory / "closing.cpp"
    path.write_text(CLOSING)
    code, result = probed(capsys, path, line_of(CLOSING, "// CLOSING"), "t.deadline=5", "t")
    assert code == 2 and result.get("refused_before_launch") is True, result
    # GCC's code for the line lies outside the block (#167): there `t` is the `int`.
    said = "`t` has no fields" if cxx == "gcc" else "several locals named `t` of different types"
    assert said in result["error"]["message"], result["error"]


# Review of PR #159 (round 2): a header a macro writes was read as a plain statement,
# its condition variable scoped to the block's end.
MACRO_HEADERS = {
    "while.cpp": """#include <cstdio>
#define P(tag, v) std::printf("%s=%ld\\n", tag, (long)(v))
static int next_val(int &n) { return n-- > 0 ? n + 10 : 0; }
#define WHILE_DECL(decl) while (decl)
int main(int argc, char **argv) {
    int t = 1;
    {
        int n = 2;
        WHILE_DECL(long t = next_val(n)) { P("A", t); }
        n += 1;
        P("B", t);  // AFTER
    }
    return 0;
}
""",
    # The outer `t` a parameter, which no jump skips.
    "switch.cpp": """#include <cstdio>
#define P(tag, v) std::printf("%s=%ld\\n", tag, (long)(v))
#define SWITCH_ON(decl) switch (decl)
__attribute__((noinline)) static long run(int t) {
    {
        SWITCH_ON(short t = (short)(t + 4)) { default: P("A", t); }
        P("B", t);  // AFTER
    }
    return t;
}
int main(int argc, char **argv) { return (int)run(argc + 4) - 5; }
""",
}


@pytest.mark.timeout(300)
@pytest.mark.parametrize("name", sorted(MACRO_HEADERS))
def test_cpp_a_macros_header_scopes_its_variable_to_its_statement(directory, capsys, name):
    # `t` read the dead `long t` (`0`) and `--set t=500` changed it, while the program
    # printed `B=1`: the scope is read from the preprocessed source now.
    source = MACRO_HEADERS[name]
    path = directory / name
    path.write_text(source)
    line = line_of(source, "// AFTER")
    code, result = captured(capsys, path, line, "t")
    assert code == 0 and result["status"] == "completed", result.get("error")
    [observed] = result["rows"]
    assert (observed["type"], observed["value"]) == ("int", "1" if name == "while.cpp" else "5")
    if name == "while.cpp":
        # (The `switch` line is refused for a change: the raw source does not show where
        # the macro's statement ends, so line 7 may continue it.)
        code, result = captured(capsys, path, line, "t", "--set", "t=500")
        assert code == 0 and "B=500" in result["output"], result.get("error")


SIGNALED = """#define _DEFAULT_SOURCE
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>
static volatile sig_atomic_t ticks;
static void on_alarm(int sig) {
    ticks = ticks + 1;  // HANDLER
}
int main(void) {
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_alarm;
    sigaction(SIGALRM, &sa, NULL);
    int t[10] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9};
    struct itimerval it = {{0, 0}, {0, 1000}};
    {
        int t[3] = {7, 7, 7};
        setitimer(ITIMER_REAL, &it, NULL);
    }
    while (!ticks) t[0]++;
    printf("outer %d %d\\n", t[0], t[9]);
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_c_a_frame_a_signal_interrupted_is_judged_where_it_stopped(directory, capsys):
    # Above the signal trampoline, a frame's address is the instruction it was
    # interrupted at, not a return address: less one, it fell in the block before the
    # loop, and `t` was judged as that block's `int t[3]`.
    path = directory / "signaled.c"
    path.write_text(SIGNALED)
    assert main(["session", "start", str(path), "--at", str(line_of(SIGNALED, "// HANDLER"))]) == 0
    capsys.readouterr()
    try:
        code, rows = run(capsys, "session", "get", "t[9]", "--frame", "2")
        assert rows[0]["value"] == "9", rows
    finally:
        main(["session", "close"])
        capsys.readouterr()


# Review of PR #159 (round 3): a statement on the declaration's line after it that only
# names the local (a header's `while (t > 20)`, a block's `{ t += 0; }`) was read as its
# declaration, scoping it to that line: the outer `t` was chosen, and changed.
SAME_LINE = """#include <stdio.h>
int main(void) {
    int t = 1;
    {
        long t = 11; LATER
        t += 1;
        printf("B=%ld\\n", (long)t);  // B
    }
    return t - 1;
}
"""
LATER = {
    "while": "while (t > 20) t--;",
    "if": "if (t) t += 0;",
    "switch": "switch (t) { default: break; }",
    "for": "for (; t > 20;) t--;",
    "do": "do t += 0; while (t > 20);",
    "block": "{ t += 0; }",
}


@pytest.mark.timeout(300)
@pytest.mark.parametrize("suffix", ["c", "cpp"])
@pytest.mark.parametrize("later", sorted(LATER))
def test_a_statement_naming_the_local_after_its_declaration_keeps_its_scope(
    directory, capsys, later, suffix
):
    source = SAME_LINE.replace("LATER", LATER[later])
    source = source if suffix == "c" else cpp(source)
    path = directory / f"later.{suffix}"
    path.write_text(source)
    code, result = captured(capsys, path, line_of(source, "// B"), "t", "--set", "t=9")
    assert code == 0 and result["status"] == "completed", result.get("error")
    observed = next(row for row in result["rows"] if row["phase"] == "observed")
    assert (observed["type"], observed["value"]) == ("long", "12"), observed
    assert "B=9" in result["output"], result["output"]


# A macro writing the declaration and a header naming it: the inner `t` (its end Peeky
# cannot read in the raw source, so it is withheld), never the outer one.
MACRO_LINE = """#include <cstdio>
static int next_val(int &n) { return n-- > 0 ? n + 10 : 0; }
#define HEAD(d) d; if (t)
int main() {
    int t = 1;
    {
        int n = 2;
        HEAD(long t = next_val(n)) { std::printf("in=%ld\\n", t); }
        n += 1;
        std::printf("B=%d\\n", (int)t);  // B
    }
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_cpp_a_macros_declaration_and_header_never_choose_the_outer_local(directory, capsys):
    path = directory / "macro_line.cpp"
    path.write_text(MACRO_LINE)
    code, result = captured(capsys, path, line_of(MACRO_LINE, "// B"), "t")
    assert code == 0 and result["status"] == "completed", result.get("error")
    [observed] = result["rows"]
    assert observed["type"] == "long", observed


# Review of PR #159 (round 3): a function of the program named like a signal trampoline
# (`sigtramp`, `__restore`) is no trampoline: its caller is judged as any caller.
USER_TRAMPOLINES = """#include <stdio.h>
__attribute__((noinline)) void sigtramp(long v) {
    printf("sigtramp %ld\\n", v);  // SIGTRAMP
}
__attribute__((noinline)) void __restore(long v) {
    printf("restore %ld\\n", v);  // RESTORE
}
int main(void) {
    int t = 10;
    {
        long t = 20;
        sigtramp(t);
    }
    {
        long t = 30;
        __restore(t);
    }
    printf("%d\\n", t);
    return 0;
}
"""


@pytest.mark.timeout(300)
@pytest.mark.parametrize("marker, value", [("// SIGTRAMP", "20"), ("// RESTORE", "30")])
def test_c_a_programs_function_named_like_a_trampoline_is_a_call(directory, capsys, marker, value):
    path = directory / "usertramp.c"
    path.write_text(USER_TRAMPOLINES)
    line = line_of(USER_TRAMPOLINES, marker)
    assert main(["session", "start", str(path), "--at", str(line)]) == 0
    capsys.readouterr()
    try:
        code, rows = run(capsys, "session", "get", "t", "--frame", "1")
        assert (rows[0]["type"], rows[0]["value"]) == ("long", value), rows
    finally:
        main(["session", "close"])
        capsys.readouterr()


# Review of PR #159 (round 4): a declaration that is a control statement's whole body,
# without braces, is in scope to its `;`; clang gives a `while`'s, a `do`'s or a
# `switch`'s no block, so the block around it ran on, and its dead `t` was chosen.
BODIES = """#include <cstdio>
struct P { long x; long y; };
int main(int argc, char **argv) {
    long t = 1;
    int n = argc;
    {
        BODY
        std::printf("A=%ld\\n", t);  // AFTER
    }
    return 0;
}
"""
BODY = {
    "while": "while (n++ < 2) long t = 8;",
    "do": "do long t = 9; while (0);",
    "switch": "switch (argc) case 1: long t = 4;",
    "if": "if (argc > 0) long t = 5;",
    "else": "if (argc < 0) {} else long t = 6;",
    "for": "for (int i = 0; i < 1; i++) long t = 7;",
}


@pytest.mark.timeout(300)
@pytest.mark.parametrize("body", sorted(BODY))
def test_cpp_a_bodys_declaration_without_braces_ends_with_its_statement(directory, capsys, body):
    source = BODIES.replace("BODY", BODY[body])
    path = directory / "bodies.cpp"
    path.write_text(source)
    line = line_of(source, "// AFTER")
    code, result = captured(capsys, path, line, "t")
    assert code == 0 and result["status"] == "completed", result.get("error")
    [observed] = result["rows"]
    if body == "switch":
        # The outer `t`, withheld: a `switch` without braces has its label after it, a
        # jump Peeky does not rule out.
        assert "value" not in observed and "declared on line 4" in observed["error"]
        return
    assert observed["value"] == "1", observed
    code, result = captured(capsys, path, line, "t", "--set", "t=4242")
    assert code == 0 and "A=4242" in result["output"], result.get("error")


STRUCT_BODY = """#include <cstdio>
struct P { long x; long y; };
int main(int argc, char **argv) {
    P t{1, 2};
    int n = argc;
    {
        while (n++ < 2) long t = 8;
        std::printf("DA=%ld\\n", t.x);  // DA
    }
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_cpp_a_field_of_the_outer_struct_after_a_bodys_declaration_is_set(directory, capsys):
    # Refused before the run: "`t` has no fields" (the `while`'s `long t` was chosen).
    path = directory / "struct_body.cpp"
    path.write_text(STRUCT_BODY)
    code, result = probed(capsys, path, line_of(STRUCT_BODY, "// DA"), "t.x=50", "t.x")
    assert code == 0 and result["status"] == "completed", result.get("error")
    assert "DA=50" in result["output"], result["output"]


# Review of PR #159 (round 4): a structured binding hides an outer local of its name;
# clang 18 on Linux describes none in a range-`for` over pairs, so LLDB listed only the
# outer `t`, which read 1 (and changed) where the code reads the binding.
BINDINGS = """#include <cstdio>
#include <utility>
int main() {
    long t = 1;
    std::pair<long, long> arr[2] = {{5, 6}, {7, 8}};
    for (auto [t, u] : arr) {
        std::printf("AA=%ld\\n", t + 0 * u);  // RANGE
    }
    struct S { long a, b; } s{30, 40};
    {
        auto [t, b] = s;
        std::printf("AF=%ld\\n", t + 0 * b);  // BLOCK
    }
    std::printf("AE=%ld\\n", t);  // AFTER
    return 0;
}
"""


@pytest.mark.timeout(300)
@pytest.mark.parametrize("marker, value", [("// RANGE", "5"), ("// BLOCK", "30")])
def test_cpp_a_structured_binding_is_never_read_as_the_outer_local(
    directory, capsys, marker, value
):
    path = directory / "bindings.cpp"
    path.write_text(BINDINGS)
    code, result = captured(capsys, path, line_of(BINDINGS, marker), "t")
    assert code == 0 and result["status"] == "completed", result.get("error")
    [observed] = result["rows"]
    # The binding where the debugger describes it (macOS), else the outer `t` withheld.
    if "value" in observed:
        assert observed["value"] == value or observed["value"].startswith("0x"), observed
    else:
        assert "structured binding `t`" in observed["error"], observed
    code, result = captured(capsys, path, line_of(BINDINGS, "// AFTER"), "t")
    assert result["rows"][0]["value"] == "1", result["rows"]


# Review of PR #159 (round 4): in a caller frame every declaration of the name was
# judged, so another block's `long t;` with no code of its own withheld the outer `t`.
CALLER_NAMESAKE = """#include <stdio.h>
__attribute__((noinline)) long probe_me(int tag, long x) {
    return x + 0 * tag;  // PROBE
}
int main(int argc, char **argv) {
    long t = 1;
    switch (argc) {
    case 0: ;
        long t;
        t = 7;
        probe_me(8, t);
        break;
    }
    probe_me(14, t);  // OUTER
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_c_a_caller_frame_judges_only_the_declaration_its_name_means(directory, capsys):
    path = directory / "namesake.c"
    path.write_text(CALLER_NAMESAKE)
    line = line_of(CALLER_NAMESAKE, "// PROBE")
    assert main(["session", "start", str(path), "--at", str(line)]) == 0
    capsys.readouterr()
    try:
        code, rows = run(capsys, "session", "get", "t", "--frame", "1")
        assert rows[0]["line"] == line_of(CALLER_NAMESAKE, "// OUTER"), rows
        assert rows[0]["value"] == "1", rows
    finally:
        main(["session", "close"])
        capsys.readouterr()


# Review of PR #159 (round 5): a lambda's structured binding, or one in any braces
# inside a statement, named nothing in the enclosing block, and hid its `t` after it.
LAMBDAS = """#include <cstdio>
#include <utility>
int main(int argc, char **argv) {
    long t = 1;
    auto lam = [&]() {
        auto [t, u] = std::pair<long, long>{50, 51};
        return t + u;
    };
    std::printf("MA=%ld\\n", t + 0 * lam());  // MA
    auto lam2 = [&]() {
        struct t { long v; };
        return t{9}.v;
    };
    std::printf("MD=%ld\\n", t + 0 * lam2());  // MD
    return 0;
}
"""


@pytest.mark.timeout(300)
@pytest.mark.parametrize("marker", ["// MA", "// MD"])
def test_cpp_what_a_lambda_declares_never_hides_the_functions_local(directory, capsys, marker):
    path = directory / "lambdas.cpp"
    path.write_text(LAMBDAS)
    code, result = captured(capsys, path, line_of(LAMBDAS, marker), "t", "--set", "t=77")
    assert code == 0 and result["status"] == "completed", result.get("error")
    observed = next(row for row in result["rows"] if row["phase"] == "observed")
    assert observed["value"] == "1", observed
    tag = marker[3:]
    assert f"{tag}=77" in result["output"], result["output"]


# Review of PR #159 (round 5): a structured binding shadowing a parameter, with a use
# on its line, was not read as a declaration, so `t` was not found.
PARAMETER_BINDING = """#include <cstdio>
#include <utility>
struct S2 { long a, b; };
__attribute__((noinline)) long pf(long t) {
    { auto [t, u] = std::pair<long, long>{60, 61}; std::printf("EB=%ld\\n", t + 0 * u); }  // EB
    { auto [t, u] = S2{62, 63}; std::printf("EK=%ld\\n", t + 0 * u); }  // EK
    return t;
}
int main() { return (int)pf(7) - 7; }
"""


@pytest.mark.timeout(300)
@pytest.mark.parametrize("marker", ["// EB", "// EK"])
def test_cpp_a_binding_shadowing_a_parameter_is_the_name(directory, capsys, marker):
    path = directory / "parameter_binding.cpp"
    path.write_text(PARAMETER_BINDING)
    line = line_of(PARAMETER_BINDING, marker)
    code, result = captured(capsys, path, line, "t")
    assert code == 0 and result["status"] == "completed", result.get("error")
    [observed] = result["rows"]
    # The binding, not run yet at its line's first instruction (where the debugger does
    # not describe it, the parameter's row, withheld for it): never `t` not found.
    assert "value" not in observed, observed
    error = observed["error"]
    assert f"declared on line {line}" in error or "structured binding `t`" in error, error


# #169: a range-`for` header's structured binding names nothing in a lambda in its
# range, nor in the range's expression, which runs before the binding is declared.
RANGE_LAMBDA = """#include <cstdio>
#include <utility>
#include <vector>
template <class F> std::vector<std::pair<long, long>> make(F f) { return {{f(40), 2}, {f(41) + 1, 3}}; }
__attribute__((noinline)) long id(long v) { return v; }  // ID
int main() {
    long t = 1;
    for (auto [t, u] : make([&](long t) { return id(t); })) {  // HEADER
        std::printf("FA=%ld\\n", t + 0 * u);
    }
    for (auto [t, u] : make([&](long v) { long t = v; return id(t); })) {
        std::printf("FD=%ld\\n", t + 0 * u);  // FD
    }
    std::printf("FB=%ld\\n", t);
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_cpp_a_range_for_binding_names_nothing_in_a_lambda_in_its_range(directory, capsys):
    path = directory / "range_lambda.cpp"
    path.write_text(RANGE_LAMBDA)
    assert main(["session", "start", str(path), "--at", str(line_of(RANGE_LAMBDA, "// ID"))]) == 0
    capsys.readouterr()
    try:
        # The lambda's own `t` (its parameter), withheld as "the structured binding".
        code, rows = run(capsys, "session", "get", "t", "--frame", "1")
        assert code == 0 and rows[0].get("value") == "40", rows
    finally:
        main(["session", "close"])
        capsys.readouterr()
    # In the loop's body the code names the binding `t`, not the outer local or the
    # lambda's: where the debugger lists the binding (macOS), it reads the element (40, as
    # the program prints FD=40), or is withheld with a note naming the structured binding;
    # never the outer `t` (1).
    code, result = captured(capsys, path, line_of(RANGE_LAMBDA, "// FD"), "t")
    assert code == 0 and result["status"] == "completed", result.get("error")
    [observed] = result["rows"]
    if observed["type"] != "long":
        assert observed.get("value"), observed
    elif "error" in observed:
        assert "structured binding `t`" in observed["error"], observed
    else:
        assert observed.get("value") == "40", observed


@pytest.mark.timeout(300)
def test_cpp_a_range_for_header_reads_the_outer_local_before_its_range_runs(directory, cxx, capsys):
    path = directory / "range_header.cpp"
    path.write_text(RANGE_LAMBDA)
    line = line_of(RANGE_LAMBDA, "// HEADER")
    code, result = captured(capsys, path, line, "t", "--set", "t=7", "--hits", "1")
    if cxx == "gcc":
        # LLDB lists GCC's binding `t` at the header's first instruction (#167), so `t`
        # names it: withheld as not declared yet, and the change refused, naming the
        # outer local's own row.
        outer = f"settable here: t @ range_header.cpp:{line - 1}"
        assert code == 1 and outer in result["error"]["message"], result["error"]
        observed = next(row for row in result["rows"] if row["phase"] == "observed")
        assert f"declared on line {line}, which has not run yet" in observed["error"], observed
        return
    assert code == 0 and result["status"] == "completed", result.get("error")
    observed = next(row for row in result["rows"] if row["phase"] == "observed")
    assert (observed["type"], observed["value"]) == ("long", "1"), observed
    assert "FB=7" in result["output"], result["output"]


# #169: a multi-stop line (#157) is judged before the run at its first stop, the only
# one where a change is made; the refusal says so where a later stop names another `t`.
MULTI_STOP = """#include <stdio.h>
struct Q { long x; long z; };
int main(void) {
    long t = 1;
    for (int i = 0; i < 2; i++) { struct Q t = {10 + i, 0}; { long k = t.x; printf("HB=%ld\\n", k); } }  // MULTI
    printf("HA=%ld\\n", t);
    return 0;
}
"""


@pytest.mark.timeout(300)
@pytest.mark.parametrize("suffix", ["c", "cpp"])
def test_a_multi_stop_lines_refusal_says_its_later_stops_make_no_change(directory, capsys, suffix):
    source = MULTI_STOP if suffix == "c" else cpp(MULTI_STOP)
    path = directory / f"multi.{suffix}"
    path.write_text(source)
    code, result = probed(capsys, path, line_of(source, "// MULTI"), "t.x=50", "t")
    error = result["error"]
    assert code == 2 and error["code"] == "MUTATION_REJECTED", result
    assert result.get("refused_before_launch") is True, result
    assert "`t` has no fields" in error["message"], error
    assert "change the `t` of another block (the line's later stops in main)" in error["message"]
    assert "part-way through the line" in error["message"], error
