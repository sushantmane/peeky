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
    # A line run on each pass of a loop: only the call's last hit gets the return.
    loop = returns(probe(capsys, c_program, "LOOP"))
    assert loop[3]["value"] == "6" and loop[3]["line"] == line_of("int s = sum_to(3)")
    assert all("value" not in loop[hit] and "ran again" in loop[hit]["error"] for hit in (1, 2))
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
