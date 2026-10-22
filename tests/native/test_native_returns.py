"""What C functions return (#155), captured by probes once per call and shown by a
session after `step --out`, on real builds; never guessed."""

import json
import os
from math import factorial

import pytest

from peeky.cli import main
from peeky.native import NativeTarget
from peeky.probe import RETURN, Probe, run_probe

SOURCE = """#include <stdio.h>
#include <stdlib.h>

struct pair { int a; int b; };

static int fact(int n) {
    int r = n <= 1 ? 1 : n * fact(n - 1); /* FACT */
    return r;
}

static int sum_to(int n) {
    int total = 0;
    for (int i = 1; i <= n; i++) {
        total += i; /* LOOP */
    }
    return total;
}

static struct pair make(int x) {
    struct pair p = {x, 2 * x}; /* MAKE */
    return p;
}

static void note(int x) {
    printf("note %d\\n", x); /* NOTE */
}

static double half(int x) {
    return x / 2.0; /* HALF */
}

static int cmp(const void *a, const void *b) {
    int x = *(const int *)a, y = *(const int *)b;
    return (x > y) - (x < y); /* CMP */
}

int main(void) {
    int f = fact(4);
    int s = sum_to(3);
    struct pair p = make(5);
    note(f);
    double h = half(7);
    int v[] = {3, 1, 2};
    qsort(v, 3, sizeof v[0], cmp); /* SORT */
    printf("%d %d %d %d %.1f %d%d%d\\n", f, s, p.a, p.b, h, v[0], v[1], v[2]);
    return 0;
}
"""


OPTIMIZED = """#include <stdio.h>

__attribute__((noinline)) static int twice(int x) {
    return 2 * x; /* TWICE */
}

int main(int argc, char **argv) {
    printf("%d\\n", twice(argc + 3));
    return 0;
}
"""


@pytest.fixture
def c_program(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "ret.c").write_text(SOURCE)
    return tmp_path / "ret.c"


def line_of(marker):
    return next(i for i, text in enumerate(SOURCE.splitlines(), 1) if marker in text)


def probe(capsys, source, marker, capture="return"):
    code = main(
        ["probe", str(source), "--at", str(line_of(marker)), "--capture", capture]
        + ["--hits", "all", "--json", "--no-save"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    return result


def returns(result):
    return {row["hit"]: row for row in result["rows"] if row["selector"] == RETURN}


@pytest.mark.timeout(120)
def test_each_call_of_a_recursive_function_gets_its_own_return(c_program, capsys):
    result = probe(capsys, c_program, "FACT", "n,return")
    n = {row["hit"]: int(row["value"]) for row in result["rows"] if row["selector"] == "n"}
    returned = returns(result)
    assert sorted(n.values()) == [1, 2, 3, 4] and set(returned) == set(n)
    for hit, row in returned.items():
        # fact(n) returned n!, into the call above it (or main for the outermost).
        assert (row["value"], row["type"], row["function"]) == (
            str(factorial(n[hit])),
            "int",
            "fact",
        )
        assert row["phase"] == "returned"
        assert row["line"] == (line_of("int f = fact(4)") if n[hit] == 4 else line_of("FACT"))


@pytest.mark.timeout(120)
def test_returns_are_shown_only_where_exact(c_program, capsys):
    # A line run on each pass of a loop: each pass's stop is proven in the same call by
    # the step out's watch (#174), so every hit gets the call's return.
    loop = returns(probe(capsys, c_program, "LOOP"))
    assert loop[3]["value"] == "6" and loop[3]["line"] == line_of("int s = sum_to(3)")
    assert [loop[hit].get("value") for hit in (1, 2)] == ["6", "6"], loop
    # A struct returned in registers, and a function returning nothing: withheld.
    made = returns(probe(capsys, c_program, "MAKE"))[1]
    assert "value" not in made, made
    # LLDB lists the struct (Apple's), or cannot read it at all (LLDB 18/20 on Linux).
    assert "pair is not a number" in made["error"] or "reported no value" in made["error"]
    noted = returns(probe(capsys, c_program, "NOTE"))[1]
    assert "value" not in noted and "reported no value" in noted["error"]
    halved = returns(probe(capsys, c_program, "HALF"))[1]
    assert (halved["value"], halved["type"]) == ("3.5", "double")


@pytest.mark.timeout(120)
def test_a_comparator_called_by_libc_returns_into_qsort_each_call(c_program, capsys):
    # libc has no debug information here: LLDB's step out would go on past qsort to the
    # next comparator call, and no return could be told apart from it.
    result = probe(capsys, c_program, "CMP", "x,y,return")
    values = {}
    for row in result["rows"]:
        values.setdefault(row["hit"], {})[row["selector"]] = row
    assert len(values) == result["hits"] >= 2
    for row in values.values():
        x, y = int(row["x"]["value"]), int(row["y"]["value"])
        assert row[RETURN]["value"] == str((x > y) - (x < y)), row
        # Returned into qsort, in libc: no source of the program's (Linux: none at all).
        assert row[RETURN]["function"] == "cmp"
        assert "ret.c" not in row[RETURN].get("location", ""), row


@pytest.mark.timeout(120)
def test_a_session_step_out_shows_what_the_function_returned(c_program, capsys):
    assert main(["session", "start", str(c_program), "--at", str(line_of("HALF"))]) == 0
    try:
        assert main(["session", "step", "--out"]) == 0
        capsys.readouterr()
        main(["session", "inspect", "--json"])
        snapshot = json.loads(capsys.readouterr().out)
        assert snapshot["returned"] == {"function": "half", "type": "double", "value": "3.5"}
        assert snapshot["frames"][0]["line"] == line_of("double h = half(7)")
        # Shown apart from the locals, never as one.
        names = [v["name"] for s in snapshot["scopes"] for v in s["variables"]]
        assert "(Return Value)" not in names and "f" in names
        main(["session", "inspect"])
        assert "half returned: double = 3.5" in capsys.readouterr().out
        assert main(["session", "get", "return", "--json"]) == 0
        row = json.loads(capsys.readouterr().out)[0]
        assert (row["selector"], row["value"], row["function"]) == (RETURN, "3.5", "half")
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(120)
def test_optimized_builds_capture_no_return(tmp_path):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    (tmp_path / "ret.c").write_text(OPTIMIZED)
    adapter = NativeTarget(
        sources=("ret.c",),
        compiler=os.environ.get("PEEKY_NATIVE_CC", "clang"),
        adapter=os.environ["PEEKY_NATIVE_DAP"],
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
        optimization="O2",
    )
    line = next(i for i, text in enumerate(OPTIMIZED.splitlines(), 1) if "TWICE" in text)
    result = run_probe(tmp_path, Probe("ret.c", line, (RETURN,)), adapter=adapter)
    assert result["status"] == "completed", result.get("error")
    row = returns(result)[1]
    assert "value" not in row and "optimized build (-O2)" in row["error"]


# Round 1 of #162: calls that never return, inlined calls, a --set-at in the call.
ENDINGS = """#include <setjmp.h>
#include <stdio.h>

static jmp_buf env;

static int leap(int n) {
    int r = n * 10; /* LEAP */
    if (n == 1) longjmp(env, 1);
    return r;
}

static int plain(int x) { return x + 1; }

__attribute__((always_inline)) static inline int folded(int x) {
    int y = x * 3; /* FOLDED */
    return y + plain(y);
}

static int add(int n) {
    int r = n * 10; /* ADD */
    r = r + 1; /* SET */
    return r;
}

int main(void) {
    volatile int s = 0;
    volatile int i = 0;
    while (i < 3) {
        if (setjmp(env) == 0) s += leap(i == 0 ? 1 : 2); /* JUMPED */
        i++;
    }
    int t = folded(1) + add(1) + add(2);
    printf("%d %d\\n", s, t);
    return 0;
}
"""

THREADS = """#include <pthread.h>
#include <stdio.h>

static int busy(int n) {
    int r = n * 11; /* BUSY */
    for (volatile int spin = 0; spin < 200000; spin++) {
    }
    return r;
}

static void *work(void *arg) {
    long id = (long)arg, s = 0;
    for (int i = 0; i < 3; i++) s += busy((int)(id * 100 + i));
    return (void *)s;
}

int main(void) {
    pthread_t threads[2];
    for (long k = 0; k < 2; k++) pthread_create(&threads[k], 0, work, (void *)(k + 1));
    long total = 0;
    for (int k = 0; k < 2; k++) {
        void *r;
        pthread_join(threads[k], &r);
        total += (long)r;
    }
    printf("%ld\\n", total);
    return 0;
}
"""


def run(capsys, source, text, marker, capture, *extra):
    line = next(i for i, row in enumerate(text.splitlines(), 1) if marker in row)
    code = main(
        ["probe", str(source), "--at", str(line), "--capture", capture, "--hits", "all"]
        + [*extra, "--json", "--no-save"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    hits = {}
    for row in result["rows"]:
        hits.setdefault(row["hit"], {})[row["selector"]] = row
    return result, hits


@pytest.mark.timeout(120)
def test_a_call_left_by_longjmp_is_never_credited_with_the_next_calls_value(c_program, capsys):
    # leap(1) jumps back to main, which calls leap(2) from the same place in the same
    # frame: LLDB's step out of leap(1) ended at leap(2)'s return, and its 20 was shown
    # as leap(1)'s.
    source = c_program.with_name("endings.c")
    source.write_text(ENDINGS)
    _, hits = run(capsys, source, ENDINGS, "LEAP", "n,return")
    assert [rows["n"]["value"] for rows in hits.values()] == ["1", "2", "2"]
    jumped = hits[1][RETURN]
    assert "value" not in jumped and "never returned" in jumped["error"], jumped
    assert all(hits[hit][RETURN]["value"] == "20" for hit in (2, 3)), hits


@pytest.mark.timeout(120)
def test_an_inlined_function_says_so(c_program, capsys):
    source = c_program.with_name("endings.c")
    source.write_text(ENDINGS)
    _, hits = run(capsys, source, ENDINGS, "FOLDED", "x,return")
    row = hits[1][RETURN]
    assert "value" not in row and "inlined into its caller" in row["error"], row
    assert "--capture return" not in row["error"]


@pytest.mark.timeout(120)
def test_a_return_after_a_set_at_in_the_call_is_that_calls(c_program, capsys):
    # The --set-at's stop comes while the first call's step out runs; its return still
    # comes back to the same frame, and is the call's own.
    source = c_program.with_name("endings.c")
    source.write_text(ENDINGS)
    line = next(i for i, row in enumerate(ENDINGS.splitlines(), 1) if "SET" in row)
    result, hits = run(capsys, source, ENDINGS, "ADD", "n,return", "--set-at", f"{line}@1", "r=500")
    assert [hits[hit][RETURN]["value"] for hit in (1, 2)] == ["501", "21"], hits
    # folded(1) + add(1) + add(2), the first add's r set to 500.
    assert result["output"].split()[1] == str(7 + 501 + 21)


# #174: `if (...) SWAP(...)` has two breakpoint locations (#160), the condition and the
# macro's block, so a call of `order` that swaps stops twice before it returns; `jump(1)`
# leaves by longjmp, and main then calls `jump(2)` from another place, at the same depth.
MULTI = """#include <setjmp.h>
#include <stdio.h>

#define SWAP(a, b) do { int t_ = (a); (a) = (b); (b) = t_; } while (0)

static jmp_buf jb;

static int order(int *a, int *b) {
    if (*a > *b) SWAP(*a, *b); /* ORDER */
    return *a + 10 * *b;
}

static int jump(int n) {
    int r = n * 2; /* JUMP */
    if (n == 1) longjmp(jb, 1);
    return r;
}

int main(void) {
    int x = 3, y = 1, z = 2;
    volatile int j = 0;
    int r1 = order(&x, &y);
    int r2 = order(&x, &z);
    if (setjmp(jb) == 0) j = jump(1);
    j += jump(2);
    printf("%d %d %d\\n", r1, r2, j);
    return 0;
}
"""


@pytest.mark.timeout(120)
def test_a_return_is_followed_across_a_lines_breakpoint_locations(c_program, capsys):
    # order(&x, &y) stops at the condition, then in SWAP's block (stop 2 of 2), before
    # it returns 31. Hit 1's return was withheld as "the line ran again ... (a loop's
    # next pass ...)"; the step out's watch proves stop 2 is in the same call, so both
    # hits get its return, each `(return)` row with its own hit's stop.
    source = c_program.with_name("multi.c")
    source.write_text(MULTI)
    line = next(i for i, row in enumerate(MULTI.splitlines(), 1) if "ORDER" in row)
    command = ["probe", str(source), "--at", str(line), "--capture", "return", "--hits", "all"]
    assert main([*command, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["output"].strip() == "31 21 4"
    shown = [(r["hit"], r.get("value"), r.get("stop"), r.get("stops")) for r in result["rows"]]
    assert shown == [(1, "31", 1, 2), (2, "31", 2, 2), (3, "21", 1, 2)], result["rows"]
    assert [(entry["hit"], entry["stop"]) for entry in result["by_hit"]] == [(1, 1), (2, 2), (3, 1)]
    # The evidence keeps each `(return)` row's stop.
    assert main(["sql", "SELECT hit, stop, value FROM last_run", "--json"]) == 0
    stored = json.loads(capsys.readouterr().out)["rows"]
    assert [(row["hit"], row["stop"], row["value"]) for row in stored] == [
        (1, 1, "31"),
        (2, 2, "31"),
        (3, 1, "21"),
    ]


@pytest.mark.timeout(120)
def test_a_line_that_stops_again_in_another_call_says_so(c_program, capsys):
    # jump(1) left by longjmp, and jump(2) stopped at the line at the same depth before
    # any return: the reason said "the line ran again ... (a loop's next pass ...)".
    # The frames under the stop are not jump(1)'s: another call, said as such.
    source = c_program.with_name("multi.c")
    source.write_text(MULTI)
    _, hits = run(capsys, source, MULTI, "JUMP", "n,return")
    assert [rows["n"]["value"] for rows in hits.values()] == ["1", "2"]
    left = hits[1][RETURN]
    assert "value" not in left and left["error"].startswith(
        "withheld: the line stopped again at the same depth in another call (the frames "
        "under it are not this call's)"
    ), left
    assert "next pass" not in left["error"] and hits[2][RETURN]["value"] == "4"


@pytest.mark.timeout(120)
def test_a_threaded_callee_withholds_returns_it_cannot_follow_and_keeps_every_row(
    c_program, capsys
):
    # Two threads call busy() together: a step out of one call is not made while the
    # other's stop is served, and another thread's stop ends following a return. Those
    # returns are withheld; the probe goes on, with every hit's rows.
    source = c_program.with_name("threads.c")
    source.write_text(THREADS)
    result, hits = run(capsys, source, THREADS, "BUSY", "n,return")
    assert result["status"] == "completed" and len(hits) == 6
    assert sorted(int(rows["n"]["value"]) for rows in hits.values()) == [
        100,
        101,
        102,
        200,
        201,
        202,
    ]
    for rows in hits.values():
        row = rows[RETURN]
        if "value" in row:
            assert row["value"] == str(11 * int(rows["n"]["value"])), rows
        else:
            assert "another thread" in row["error"], row
    assert "session continue" not in json.dumps(result)


# Round 2 of #162: a step out over a deep recursion through its caller, mutual recursion
# in probes, a caller LLDB refuses to disassemble unless forced, and the leftover step.
DEEP = """#include <stdio.h>
#include <stdlib.h>
static int top;
static int c(int n);
static int f(int n) {
    int r = n % 7;
    if (n == top) {
        r += 1; /* TOP */
    }
    if (n > 0) r += c(n - 1);
    return r;
}
static int c(int n) {
    int v = f(n) + 1; /* CALLS */
    return v;
}
int main(int argc, char **argv) {
    top = argc > 1 ? atoi(argv[1]) : 2000;
    printf("%d\\n", c(top));
    return 0;
}
"""

MUTUAL = """#include <stdio.h>
#include <string.h>
static int c(int n);
static int f(int n) {
    int r = n * 10; /* F */
    if (n > 0) r += c(n - 1);
    return r;
}
static int c(int n) {
    char buf[64];
    memset(buf, n, sizeof buf);
    int v = f(n) + buf[3];
    return v;
}
int main(void) {
    int t = c(3);
    printf("%d\\n", t);
    return 0;
}
"""


def line(text, marker):
    return next(i for i, row in enumerate(text.splitlines(), 1) if marker in row)


@pytest.mark.timeout(180)
def test_a_step_out_over_a_deep_recursion_completes_and_withholds_what_it_cannot_follow(
    c_program, capsys
):
    # Each deeper c() runs the watched call: LLDB stopped (and said `continued`) twice
    # per level, and 1,100 levels overflowed the 2,000-entry event history: the session
    # failed and the program was killed. The watch now gives up inside LLDB after
    # WATCH_TRAPS runs, and the return is withheld, never the session.
    source = c_program.with_name("deep.c")
    source.write_text(DEEP)
    start = ["session", "start", str(source), "--at", str(line(DEEP, "TOP")), "--", "1100"]
    assert main(start) == 0
    try:
        assert main(["session", "step", "--out", "--timeout", "60", "--json"]) == 0
        capsys.readouterr()
        main(["session", "inspect", "--json"])
        snapshot = json.loads(capsys.readouterr().out)
        assert snapshot["frames"][0]["line"] == line(DEEP, "CALLS")
        returned = snapshot["returned"]
        assert "value" not in returned and "watch was dropped" in returned["reason"], returned
        assert main(["session", "continue", "--to-exit", "--timeout", "60"]) == 0
    finally:
        main(["session", "close"])
        capsys.readouterr()
    result, hits = run(capsys, source, DEEP, "TOP", "r,return", "--timeout", "120", "--arg", "1100")
    assert result["status"] == "completed" and hits[1]["r"]["value"] == "1"
    assert "watch was dropped" in hits[1][RETURN]["error"]


@pytest.mark.timeout(120)
def test_a_probe_follows_each_call_of_a_mutual_recursion_to_its_return(c_program, capsys):
    # f -> c -> f: the step out of the innermost f returns into c, not into the next
    # pending f; Peeky steps out again until that f returns (each checked against its
    # own hit's frames). Only the innermost got a value before.
    source = c_program.with_name("mutual.c")
    source.write_text(MUTUAL)
    _, hits = run(capsys, source, MUTUAL, "F", "n,return")
    values = {rows["n"]["value"]: rows[RETURN].get("value") for rows in hits.values()}
    # f(n) = 10n + c(n-1), c(n) = f(n) + n.
    assert values == {"0": "0", "1": "10", "2": "31", "3": "63"}, hits


@pytest.mark.timeout(180)
def test_a_caller_too_large_for_lldb_to_disassemble_unforced_is_still_watched(c_program, capsys):
    # LLDB refuses a function this large without --force: every return was withheld,
    # saying no call came before it, and the function was disassembled again each time.
    body = []
    for k in range(120):
        body.append(f"    t += f(k + {k});")
        body += [
            f"    sink = sink * {3 + j} + t + {8 * k + j}; t ^= sink >> {j % 5};" for j in range(8)
        ]
    text = (
        "#include <stdio.h>\nstatic volatile int sink;\n"
        "static int f(int n) {\n    int r = n * 2; /* F */\n    return r + 1;\n}\n"
        "static int big(int k) {\n    int t = 0;\n" + "\n".join(body) + "\n    return t;\n}\n"
        'int main(void) {\n    printf("%d\\n", big(1));\n    return 0;\n}\n'
    )
    source = c_program.with_name("big.c")
    source.write_text(text)
    _, hits = run(capsys, source, text, "/* F */", "n,return", "--hits", "40")
    assert len(hits) == 40
    for rows in hits.values():
        assert rows[RETURN]["value"] == str(2 * int(rows["n"]["value"]) + 1), rows


@pytest.mark.timeout(120)
def test_a_step_out_a_breakpoint_interrupts_leaves_no_step_behind(c_program, capsys):
    # The breakpoint in the next (deeper) f stops the step out of the first; LLDB kept
    # that step and, at a later `continue`, stopped where it returns, as a step stop.
    source = c_program.with_name("mutual.c")
    source.write_text(MUTUAL)
    assert main(["session", "start", str(source), "--at", str(line(MUTUAL, "F"))]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "step", "--out", "--json"]) == 0
        state = json.loads(capsys.readouterr().out)
        assert (state["reason"], state["hit"]) == ("breakpoint", 2), state
        reasons = []
        for _ in range(6):
            main(["session", "continue", "--json"])
            state = json.loads(capsys.readouterr().out)
            if state.get("state") != "stopped":
                break
            reasons.append(state["reason"])
        assert reasons == ["breakpoint", "breakpoint"], reasons
    finally:
        main(["session", "close"])
        capsys.readouterr()
