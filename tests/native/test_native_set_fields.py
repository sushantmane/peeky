"""#105: `--set` on scalar fields and elements of C and C++ locals the stopped frame
holds itself, read back and seen by the program; everything else refused."""

import json
import os
import sys

import pytest

from peeky.cli import _load, _Remote, _stop, main
from peeky.native import NativeTarget
from peeky.probe import Probe, run_probe

C_SOURCE = """#include <stdio.h>

typedef int myint;
struct inner { int lo; long hi; };
struct outer { int start; myint end; struct inner in; int arr[3]; };
struct bits { unsigned a : 3; int whole; };
struct consts { const int fixed; int loose; };
union num { int i; float f; };
struct __attribute__((packed)) pk { char c; int i; };
struct flags { char ch; _Bool on; double ratio; short tiny; };

__attribute__((noinline)) static long span(struct outer o) {
    long width = o.end - o.start;  // SPAN
    return width + o.in.hi;
}

int main(void) {
    struct outer s = { 1, 2, { 3, 4 }, { 5, 6, 7 } };
    struct outer *ps = &s;
    int grid[2][2] = { { 1, 2 }, { 3, 4 } };
    struct bits b = { 1, 2 };
    struct consts c = { 10, 11 };
    union num u = { 5 };
    struct pk k = { 'a', 9 };
    struct flags f = { 'x', 1, 2.5, 7 };
    printf("BEFORE %d\\n", s.end);  // STOP
    struct outer later = { 0 };
    long w = span(s);
    printf("S %d %d %ld %d %d\\n", s.end, s.in.lo, s.in.hi, s.arr[2], grid[1][0]);
    printf("R %d %d %d %d %d %d %u %d %ld\\n", b.whole, c.loose, u.i, k.i, f.tiny, ps->start,
           b.a, later.end, w);
    return 0;
}
"""

CPP_SOURCE = """#include <cstdio>
#include <utility>
#include <vector>

namespace geo {
struct Span { long start; long end; };
}
struct Base { int b; };
struct Derived : Base { int d; };
struct Holder { geo::Span &ref; int n; };
struct Counter {
    long count = 0;
    long bump(long by) {
        count += by;  // BUMP
        return count;
    }
};
using Alias = geo::Span;

int main() {
    geo::Span sp{1, 2};
    Alias al{3, 4};
    Derived dv;
    dv.b = 5;
    dv.d = 6;
    Holder h{sp, 7};
    const geo::Span cs{8, 9};
    std::pair<int, int> pr{10, 11};
    std::vector<int> v{1, 2};
    geo::Span &rs = sp;
    Counter c;
    std::printf("BEFORE\\n");  // STOP
    c.bump(1);
    std::printf("V %ld %ld %d %d %ld %d %d %ld %ld\\n", sp.end, al.end, dv.d, h.n, cs.end,
                pr.first, v[0], rs.start, c.count);
    return 0;
}
"""


@pytest.fixture
def directory(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def line_of(source, marker):
    return next(n for n, text in enumerate(source.splitlines(), 1) if marker in text)


def change(capsys, assignment):
    """(exit code, the JSON `session set` gives)."""
    code = main(["session", "set", assignment, "--json"])
    return code, json.loads(capsys.readouterr().out)


def refused(capsys, assignment, *words):
    code, result = change(capsys, assignment)
    error = result.get("error", {})
    assert code == 2 and error.get("code") == "MUTATION_REJECTED", (assignment, result)
    for word in words:
        assert word in error["message"], (assignment, error["message"])


def caller_refuses(root, field):
    """A field of frame 1's ``root``, changed through the worker's own handle."""
    record, client = _load("default")
    remote, stop = _Remote(client), _stop(record)
    snapshot = remote.dispatch("inspect", {**stop, "frame": 1, "names": [root]})
    row = next(v for s in snapshot["scopes"] for v in s["variables"] if v["name"] == root)
    assert row["variablesReference"]
    with pytest.raises(Exception, match="in a caller's frame, whose values cannot be changed"):
        remote.dispatch(
            "set_variable",
            {**stop, "handle": row["variablesReference"], "name": field, "value": "77"},
        )


def output(capsys):
    assert main(["session", "continue", "--to-exit"]) == 0
    capsys.readouterr()
    assert main(["session", "report", "--json"]) == 0
    text = json.loads(capsys.readouterr().out)["output"]
    assert main(["session", "close"]) == 0
    capsys.readouterr()
    return text


@pytest.mark.timeout(240)
def test_c_fields_and_elements_change_and_the_program_sees_them(directory, capsys):
    path = directory / "fields.c"
    path.write_text(C_SOURCE)
    assert main(["session", "start", str(path), "--at", str(line_of(C_SOURCE, "STOP"))]) == 0
    capsys.readouterr()
    for assignment, before, after in (
        ("s.end=20", "2", "20"),  # a typedef'd int
        ("s.in.lo=-3", "3", "-3"),  # a nested struct's field
        ("s.in.hi=9000000000", "4", "9000000000"),
        ("s.arr[2]=70", "7", "70"),  # an array element in a struct
        ("grid[1][0]=30", "3", "30"),  # a two-dimensional array's element
        ("b.whole=40", "2", "40"),  # beside a bitfield
        ("c.loose=50", "11", "50"),  # beside a const field
        ("f.tiny=-7", "7", "-7"),  # a short
    ):
        code, result = change(capsys, assignment)
        assert code == 0, (assignment, result)
        assert (result["before"], result["after"]) == (before, after), (assignment, result)
    # `ps` is `main`'s own local, which no call passed (tenth evaluation, prep 9).
    refused(capsys, "ps.start=8", "`ps` is a pointer", "not a parameter, so no call passed it")
    refused(capsys, "b.a=3", "`b.a` is a bitfield")
    refused(capsys, "c.fixed=3", "`c.fixed` is const")
    refused(capsys, "u.i=3", "`u` is a union")
    refused(capsys, "k.i=3", "`k` is a packed struct (pk)")
    refused(capsys, "f.ch=3", "is a character (char)")
    refused(capsys, "f.on=0", "is a boolean (_Bool)")
    refused(capsys, "f.ratio=3", "is a floating-point value (double)")
    refused(capsys, "s.end=2147483648", "outside the range of `s.end` (myint: -2147483648 to")
    refused(capsys, "f.tiny=40000", "outside the range of `f.tiny` (short: -32768 to 32767)")
    # Declared below the stop: its storage holds leftovers its initializer overwrites.
    refused(capsys, "later.end=5", "'later' has no value here (declared at line")
    # A by-value parameter is the callee's own copy.
    assert main(["session", "continue", "--to", str(line_of(C_SOURCE, "SPAN"))]) == 0
    capsys.readouterr()
    code, result = change(capsys, "o.in.hi=100")
    assert code == 0 and result["after"] == "100", result
    # A caller's local: a call it is making may hold a copy of the old value.
    caller_refuses("s", "end")
    text = output(capsys)
    assert "S 20 -3 9000000000 70 30" in text, text
    # Refused changes left the program as it was; the parameter's change reached span.
    assert "R 40 50 5 9 -7 1 1 0 119" in text, text


@pytest.mark.timeout(240)
def test_cpp_fields_change_only_in_objects_the_frame_holds(directory, cxx, capsys):
    path = directory / "fields.cpp"
    path.write_text(CPP_SOURCE)
    assert main(["session", "start", str(path), "--at", str(line_of(CPP_SOURCE, "STOP"))]) == 0
    capsys.readouterr()
    for assignment, after in (
        ("sp.end=20", "20"),  # a namespace's struct
        ("al.end=21", "21"),  # through a `using` alias
        ("dv.d=22", "22"),  # a derived class's own field
        ("h.n=24", "24"),  # beside a reference member
        ("c.count=5", "5"),  # a class with a default member initializer
    ):
        code, result = change(capsys, assignment)
        assert code == 0 and result["after"] == after, (assignment, result)
    refused(capsys, "h.ref.end=3", "`h.ref` is a reference")
    refused(capsys, "rs.end=3", "`rs` is a reference")
    # GCC places a `const` aggregate it can build at compile time in read-only data,
    # which lldb-dap lists among no frame's locals (#167): read from there (#364).
    refused(capsys, "cs.end=3", "`cs` is const")
    refused(capsys, "pr.first=3", "standard-library type")
    refused(capsys, "v[0]=3", "standard-library type")
    assert main(["session", "continue", "--to", str(line_of(CPP_SOURCE, "BUMP"))]) == 0
    capsys.readouterr()
    # A member function's object is reached through `this`, a pointer.
    refused(capsys, "this.count=9", "`this` is a pointer")
    caller_refuses("sp", "end")
    text = output(capsys)
    assert "V 20 21 22 24 9 10 1 1 6" in text, text


def native_target(language, optimization):
    variable = "PEEKY_NATIVE_CXX" if language == "cpp" else "PEEKY_NATIVE_CC"
    compiler = os.environ.get(variable)
    return NativeTarget(
        sources=(f"fields.{'cpp' if language == 'cpp' else 'c'}",),
        language=language,
        compiler=compiler or ("clang++" if language == "cpp" else "clang"),
        adapter=os.environ["PEEKY_NATIVE_DAP"],
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
        optimization=optimization,
    )


@pytest.mark.timeout(240)
def test_a_probe_changes_a_field_and_an_optimized_build_refuses(directory):
    line = line_of(C_SOURCE, "STOP")
    (directory / "fields.c").write_text(C_SOURCE)
    sets = (("s.arr[1]", "60"), ("s.end", "21"))
    result = run_probe(
        directory, Probe("fields.c", line, ("s.end",), sets), adapter=native_target("c", "O0")
    )
    assert result["status"] == "completed", result.get("error")
    assert [(i["name"], i["after"]) for i in result["interventions"]] == [
        ("s.arr[1]", "60"),
        ("s.end", "21"),
    ]
    assert "S 21 3 4 7 3" in result["output"], result["output"]
    optimized = run_probe(
        directory, Probe("fields.c", line, ("s",), sets[:1]), adapter=native_target("c", "O1")
    )
    error = optimized.get("error") or {}
    assert error.get("code") == "MUTATION_REJECTED", optimized
    assert "optimized build (-O1)" in error["message"], error


MID_SOURCE = """#include <stdio.h>

struct tri { int a, b, c; };

__attribute__((noinline)) static int f(void) {
    return 1000;
}

static void temp_out(void) {
    struct tri s = { 1, 2, 3 };
    int x = 1;
    s.c += 1;
    int t = x + s.a + f();  // TEMP_OUT
    printf("tempout %d %d %d\\n", t, s.a, x);
}

static void temp_across(void) {
    struct tri s = { 1, 2, 3 };
    s.c += 1;
    int t = s.a
        + f();  // TEMP_SECOND
    printf("temp %d %d\\n", t, s.a);
}

int main(void) {
    temp_out();
    temp_across();
    return 0;
}
"""


def settable(capsys, name):
    assert main(["session", "inspect", "--json"]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    rows = [v for s in snapshot["scopes"] for v in s["variables"] if v["name"] == name]
    return rows[0].get("settable")


@pytest.mark.timeout(240)
@pytest.mark.parametrize("language", ["c", "cpp"])
def test_a_change_part_way_through_a_statement_is_refused(directory, capsys, language):
    # Review of #105: back from `f` (`step --out`), `t = x + s.a + f()` already holds
    # `s.a`, and a change reported `1 -> 50` while the program printed `1001`.
    path = directory / ("mid.c" if language == "c" else "mid.cpp")
    path.write_text(MID_SOURCE)
    at = line_of(MID_SOURCE, "TEMP_OUT")
    assert main(["session", "start", str(path), "--at", str(at)]) == 0
    capsys.readouterr()
    # At the statement's start nothing is read yet: the change reaches it.
    assert settable(capsys, "x") is True
    code, result = change(capsys, "s.a=40")
    assert code == 0 and result["after"] == "40", result
    assert main(["session", "step", "--into"]) == 0
    assert main(["session", "step", "--out"]) == 0
    capsys.readouterr()
    assert settable(capsys, "x") is False
    refused(
        capsys,
        "s.a=50",
        "cannot be changed here",
        f"first instruction of the statement on line {at}",
    )
    refused(capsys, "x=7", "cannot be changed here", "would not see the change")
    # A line that continues a statement an earlier line started.
    second = line_of(MID_SOURCE, "TEMP_SECOND")
    assert main(["session", "continue", "--to", str(second)]) == 0
    capsys.readouterr()
    refused(capsys, "s.a=60", f"line {second} continues a statement")
    text = output(capsys)
    # Only the change made where the statement started reached the program.
    assert "tempout 1041 40 1" in text and "temp 1001 1" in text, text


@pytest.mark.timeout(240)
def test_cpp_a_namespace_named_core_or_alloc_is_the_programs_own(directory, capsys):
    # Review of #105: `core::Config cfg; --set cfg.level=60` was refused as std's.
    source = """#include <cstdio>

namespace core {
struct Config { int level; int depth; };
}
namespace alloc {
struct Pool { long size; };
}

int main() {
    core::Config cfg{1, 2};
    alloc::Pool pool{3};
    std::printf("cfg %d %ld\\n", cfg.level, pool.size);  // STOP
    return 0;
}
"""
    path = directory / "ns.cpp"
    path.write_text(source)
    assert main(["session", "start", str(path), "--at", str(line_of(source, "STOP"))]) == 0
    capsys.readouterr()
    for assignment in ("cfg.level=60", "pool.size=70"):
        code, result = change(capsys, assignment)
        assert code == 0, (assignment, result)
    assert "cfg 60 70" in output(capsys)


SHADOWED = """#include <stdio.h>

struct pair { long x, y; };

int main(int argc, char **argv) {
    struct pair p = { 1, 2 };
    int big[1000];
    for (int i = 0; i < 1000; i++) big[i] = i;
    int n = argc + 2;
    int vla[n];
    for (int i = 0; i < n; i++) vla[i] = i;
    {
        struct pair p = { 3, 4 };
        p.x += 0;  // INNER
        printf("inner %ld %d\\n", p.x, vla[1]);
    }
    printf("outer %ld %d\\n", p.x, big[700]);
    return 0;
}
"""


def start_shadowed(directory, capsys):
    path = directory / "s.c"
    path.write_text(SHADOWED)
    assert main(["session", "start", str(path), "--at", str(line_of(SHADOWED, "INNER"))]) == 0
    capsys.readouterr()


@pytest.mark.timeout(240)
def test_c_a_shadowed_root_is_selected_by_where_it_is_declared(directory, capsys):
    # Review of #105: lldb-dap names both `p @ s.c:13` and `p @ s.c:6`, which no
    # selector could name, and plain `p` is not listed.
    start_shadowed(directory, capsys)
    inner, outer = line_of(SHADOWED, "{ 3, 4 }"), line_of(SHADOWED, "{ 1, 2 }")
    for assignment, after in ((f"p @ s.c:{inner}.x=30", "30"), (f"p@s.c:{outer}.x=20", "20")):
        code, result = change(capsys, assignment)
        assert code == 0 and result["after"] == after, (assignment, result)
    text = output(capsys)
    assert "inner 30 1" in text and "outer 20 700" in text, text


@pytest.mark.timeout(240)
def test_c_an_element_of_a_large_array_local_is_changed(directory, capsys):
    # Review of #105: `big[700]` of a 1,000-element local exhausted the expansion
    # budget; the element is now fetched alone.
    start_shadowed(directory, capsys)
    code, result = change(capsys, "big[700]=7000")
    assert code == 0 and result["after"] == "7000", result
    assert "outer 1 7000" in output(capsys)


@pytest.mark.timeout(240)
def test_c_a_variable_length_array_is_refused_as_one(directory, capsys):
    # Review of #105: it said the VLA was "not kept in this frame's own memory".
    start_shadowed(directory, capsys)
    refused(capsys, "vla[1]=5", "`vla` is a variable-length array")
    assert "inner 3 1" in output(capsys)


STARTS = """#include <stdio.h>

struct P { long x; long y; };
struct W { long v; };

__attribute__((noinline)) long g(void) { return 1000; }
__attribute__((noinline)) long sink(long a, long b) { return a + b; }
__attribute__((noinline)) long one(struct P p) { return p.x + sink(g(), 1); }  // ONE
#define TWICE(v) t = v.x; t += g()

int main(void) {
    struct P p = { 1, 2 };
    long c = 1, t;
    t =  // WRAPPED
        p.x + g();
    printf("A %ld\\n", t);
    t = p.x + ({
        long q = 0;
        if (c) {
            q = g();  // EXPRESSION
        }
        q;
    });
    printf("B %ld\\n", t);
    t = p.x + (long)(struct W){
        g() }.v;  // LITERAL
    printf("C %ld\\n", t);
    t = p.x
#if 0
        ;
#endif
        + g();  // DIRECTIVE
    printf("D %ld\\n", t);
    t = one(p);
    printf("E %ld\\n", t);
    TWICE(p);
    printf("F %ld\\n", t);  // AFTER_MACRO
    return 0;
}
"""


def start_at(directory, capsys, name, source, marker):
    path = directory / name
    path.write_text(source)
    assert main(["session", "start", str(path), "--at", str(line_of(source, marker))]) == 0
    capsys.readouterr()


def continue_to(capsys, source, marker):
    assert main(["session", "continue", "--to", str(line_of(source, marker))]) == 0
    capsys.readouterr()


@pytest.mark.timeout(240)
def test_c_a_change_is_refused_unless_at_its_statements_first_instruction(directory, capsys):
    # Review of #105 (round 2): each stop looked like a statement's start while the
    # statement was under way, and `set p.x=50` reported `1 -> 50` while the program
    # printed `1001`:
    # - `t =` / `p.x + g();`: clang emits the second line's code first, so `t =` has
    #   only the final store;
    # - `if (c) {` inside a statement expression `({ ... })`;
    # - `(long)(struct W){` (a compound literal after a cast);
    # - `t = p.x` / `#if 0` / `;` / `#endif` / `+ g();`.
    start_at(directory, capsys, "starts.c", STARTS, "WRAPPED")
    wrapped = line_of(STARTS, "WRAPPED")
    refused(capsys, "p.x=50", f"not at the first instruction of the statement on lines {wrapped}-")
    for marker in ("EXPRESSION", "LITERAL"):
        continue_to(capsys, STARTS, marker)
        refused(capsys, "p.x=50", f"line {line_of(STARTS, marker)} continues a statement")
    continue_to(capsys, STARTS, "DIRECTIVE")
    refused(capsys, "p.x=50", "cannot tell from the source whether line")
    text = output(capsys)
    assert all(f"{tag} 1001" in text for tag in "ABCD"), text


@pytest.mark.timeout(240)
def test_c_a_one_line_function_allows_a_change_only_after_its_prologue(directory, capsys):
    # Review of #105 (round 2): back from `g` (`step --out`) in `long one(struct P p) {
    # return p.x + sink(g(), 1); }`, `set p.x=70` reported `1 -> 70`, and the program
    # printed `E 1002`: every row of a function's first line counted as its start.
    start_at(directory, capsys, "starts.c", STARTS, "ONE")
    code, result = change(capsys, "p.x=60")
    assert code == 0 and result["after"] == "60", result
    assert main(["session", "step", "--into"]) == 0
    assert main(["session", "step", "--out"]) == 0
    capsys.readouterr()
    refused(capsys, "p.x=70", "not at the first instruction of the statement on line")
    assert "E 1061" in output(capsys)


@pytest.mark.timeout(240)
def test_c_a_multi_statement_macro_leaves_the_lines_after_it_settable(directory, capsys):
    # Review of #105 (round 2): one `TWICE(p);` (a macro holding a `;`) refused every
    # change in the rest of its block; only a macro that may hide a bracket does now.
    start_at(directory, capsys, "starts.c", STARTS, "AFTER_MACRO")
    code, result = change(capsys, "t=5")
    assert code == 0 and result["after"] == "5", result
    assert "F 5" in output(capsys)


SHADOWS = """#include <stdio.h>

struct P { long x; long y; };
struct E { long a; int b; };

int main(void) {
    long n = 3;  // OUTER
    struct E es[700];
    long g2[600][3];
    for (int i = 0; i < 700; i++) { es[i].a = i; es[i].b = -i; }
    for (int i = 0; i < 600; i++) for (int j = 0; j < 3; j++) g2[i][j] = i * 10 + j;
    {
        long n = 4;  // INNER
        { struct P p = { 7, 8 }; { struct P p = { 9, 10 };
            n += p.x;  // STOP
        } }
    }
    printf("%ld %ld %ld\\n", n, es[699].a, g2[599][2]);
    return 0;
}
"""


@pytest.mark.timeout(240)
def test_c_shadowed_root_integers_and_elements_past_512_at_any_step(directory, capsys):
    # Review of #105 (round 2): `n@s.c:13=99` was refused ("only ... long; has type
    # 'long'"), `es[699].a` and `g2[599][2]` exhausted the expansion budget, and `get`
    # read the first of two locals LLDB names alike (`p @ s.c:14`) without a word.
    start_at(directory, capsys, "s.c", SHADOWS, "STOP")
    inner, outer = line_of(SHADOWS, "INNER"), line_of(SHADOWS, "OUTER")
    for assignment, after in (
        (f"n@s.c:{inner}=90", "90"),
        (f"n @ s.c:{outer}=30", "30"),
        ("es[699].a=7", "7"),
        ("g2[599][2]=8", "8"),
    ):
        code, result = change(capsys, assignment)
        assert code == 0 and result["after"] == after, (assignment, result)
    same = line_of(SHADOWS, "{ struct P p = { 7, 8 };")
    assert main(["session", "get", f"p @ s.c:{same}", "--json"]) == 1
    row = json.loads(capsys.readouterr().out)[0]
    assert "is ambiguous: the debugger lists 2 locals of that name" in row["error"], row
    assert "30 7 8" in output(capsys)


CPP_STARTS = """#include <cstdio>
#include <memory>

struct P { long x; long y; };
struct W { long v; };

__attribute__((noinline)) long g() { return 1000; }
__attribute__((noinline)) long sink(long a, long b) { return a + b; }

struct Counter {
    long n = 0;
    void add(long v) {
        long k = v;
        n += k;
    }  // CLOSE
};

int main() {
    P p{1, 2};
    long c = 1, t = 0;
    t = p.x + std::unique_ptr<long[]>(new long[1]{
        g() })[0];  // NEW
    std::printf("A %ld\\n", t);
    t = p.x + decltype(W{}){
        g() }.v;  // DECLTYPE
    std::printf("B %ld\\n", t);
    t = p.x + ({
        long q = 0;
        for (int i = 0; i < 1; i++) {
            q += g();  // FOR
        }
        q;
    });
    std::printf("C %ld\\n", t);
    if (c) [[likely]] {
        t = 5;  // LIKELY
    }
    Counter counter;
    counter.add(2);
    auto f = [](P q) { return q.x + sink(g(), 1); };  // LAMBDA
    t = f(p);
    std::printf("D %ld %ld\\n", t, counter.n);
    return 0;
}
"""


@pytest.mark.timeout(240)
def test_cpp_braces_that_are_no_blocks_and_a_lambdas_return_address_are_refused(directory, capsys):
    # Review of #105 (round 2): `new long[1]{`, `decltype(W{}){` and a `for` body in
    # `({` passed as blocks of statements, and back from `g` in a one-line lambda
    # `set q.x=70` reported `1 -> 70` while the program printed `L 1002`.
    start_at(directory, capsys, "starts.cpp", CPP_STARTS, "NEW")
    refused(capsys, "p.x=50", f"line {line_of(CPP_STARTS, 'NEW')} continues a statement")
    for marker in ("DECLTYPE", "FOR"):
        continue_to(capsys, CPP_STARTS, marker)
        refused(capsys, "p.x=50", f"line {line_of(CPP_STARTS, marker)} continues a statement")
    # After `if (c) [[likely]] {`, and at an in-class method's closing `}`, a change is
    # allowed: each is where a statement starts.
    continue_to(capsys, CPP_STARTS, "LIKELY")
    code, result = change(capsys, "c=3")
    assert code == 0 and result["after"] == "3", result
    continue_to(capsys, CPP_STARTS, "CLOSE")
    code, result = change(capsys, "v=4")
    assert code == 0 and result["after"] == "4", result
    continue_to(capsys, CPP_STARTS, "LAMBDA")
    code, result = change(capsys, "q.x=60")
    assert code == 0 and result["after"] == "60", result
    assert main(["session", "step", "--into"]) == 0
    assert main(["session", "step", "--out"]) == 0
    capsys.readouterr()
    refused(capsys, "q.x=70", "not at the first instruction of the statement on line")
    text = output(capsys)
    assert "A 1001" in text and "B 1001" in text and "C 1001" in text, text
    assert "D 1061 2" in text, text


SHAPES = """#include <stdio.h>

struct P { long x; long y; };

__attribute__((noinline)) long g(void) { return 1000; }
static inline __attribute__((always_inline)) long inl(struct P *q) { return q->x + g(); }
static inline __attribute__((always_inline)) long getx(struct P *q) { return q->x; }
#define REPEAT(i, n) for (int i = 0; i < (n); i++)
__attribute__((noinline)) long first(struct P p) { long t =  // FIRST
    p.x + g();  // FIRST_2
    return t; }
__attribute__((noinline)) long ret(struct P p) { return  // RETURN
    p.x + g(); }  // RETURN_2
long old(a, b)
    long a; long b;
{
    long r = a + b;  // OLD
    return r;
}

int main(void) {
    struct P p = { 1, 2 };
    long t;
    t = inl(&p) + p.y;  // INLINED
    printf("A %ld\\n", t);
    t = getx(&p) + g();  // GETX
    printf("B %ld\\n", t);
    t = p.x + ({
        long q = 0;
        REPEAT(k, 1) {
            q += g();  // LOOP_BODY
        }
        q;
    });
    printf("F %ld\\n", t);
    t = first(p);
    printf("E %ld\\n", t);
    t = ret(p);
    printf("R %ld\\n", t);
    t = old(p.x, 2);
    printf("K %ld\\n", t);
    t = p.x + 1;  // PLAIN
    printf("P %ld\\n", t);
    return 0;
}
"""


@pytest.mark.timeout(240)
def test_c_a_statement_calling_an_always_inline_function_is_refused(directory, capsys):
    # Review of #105 (round 3): an `always_inline` callee's code carries its own lines
    # and runs before the statement's lowest row: at `t = inl(&p) + p.y;`, `set p.x=50`
    # reported `1 -> 50` and the program printed `A 1003`; `t = getx(&p) + g();`,
    # `B 1050`. Root integers were marked [settable] there too.
    start_at(directory, capsys, "shapes.c", SHAPES, "INLINED")
    refused(capsys, "p.x=50", "makes a call the compiler inlined (`always_inline`, `flatten`)")
    assert main(["session", "inspect", "--json"]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    rows = {v["name"]: v for s in snapshot["scopes"] for v in s["variables"]}
    assert not rows["t"].get("settable"), rows["t"]
    continue_to(capsys, SHAPES, "GETX")
    refused(capsys, "p.x=60", "makes a call the compiler inlined")
    continue_to(capsys, SHAPES, "PLAIN")
    code, result = change(capsys, "p.x=5")
    assert code == 0 and result["after"] == "5", result
    text = output(capsys)
    assert "A 1003" in text and "B 1001" in text and "P 6" in text, text


@pytest.mark.timeout(240)
def test_c_a_macro_loop_inside_a_statement_expression_is_refused(directory, capsys):
    # Review of #105 (round 3): `REPEAT(k, 1) {` (a `for` header) inside `t = p.x + ({`
    # read as a function's body, and `set p.x=50` printed `F 1001`.
    start_at(directory, capsys, "shapes.c", SHAPES, "LOOP_BODY")
    refused(capsys, "p.x=50", "continues a statement")
    assert "F 1001" in output(capsys)


@pytest.mark.timeout(240)
def test_c_a_functions_first_line_needs_its_statements_first_instruction(directory, capsys):
    # Review of #105 (round 3): on `long first(struct P p) { long t =` / `p.x + g();`,
    # a step from the second line lands on the first line's store, which counted as
    # the first row after the prologue: `set p.x=50` printed `E 1001`. So did
    # `{ return` with its expression on the next line.
    for marker, tag in (("FIRST_2", "E"), ("RETURN_2", "R")):
        start_at(directory, capsys, "shapes.c", SHAPES, marker)
        assert main(["session", "step"]) == 0
        capsys.readouterr()
        top = line_of(SHAPES, marker) - 1
        refused(capsys, "p.x=50", f"first instruction of the statement on lines {top}-{top + 1}")
        assert f"{tag} 1001" in output(capsys)


@pytest.mark.timeout(240)
def test_c_a_k_and_r_definition_is_refused_as_one(directory, capsys):
    # Review of #105 (round 3): a K&R body was refused as "line N continues a statement".
    start_at(directory, capsys, "shapes.c", SHAPES, "OLD")
    refused(capsys, "a=9", "K&R-style definition")
    assert "K 3" in output(capsys)


TRY_BLOCKS = """#include <cstdio>
#include <stdexcept>

struct P { long x; long y; };

__attribute__((noinline)) long g() { return 1000; }
__attribute__((noinline)) long boom(long v) { if (v > 100) throw std::runtime_error("big"); return v; }

long tryfun(P p) try {
    long t = 0;  // FIRST
    t = p.x + g();  // BODY
    return t;
} catch (const std::exception &) {
    long u = p.y;  // HANDLER
    return u + 1;
}

long caught(P p) try {
    return boom(p.x);
} catch (const std::runtime_error &) {
    long u = p.y;  // CAUGHT
    return u + 2;
}

int main() {
    P p{1, 2};
    std::printf("T %ld\\n", tryfun(p));
    P q{500, 7};
    std::printf("C %ld\\n", caught(q));
    return 0;
}
"""


@pytest.mark.timeout(240)
def test_cpp_a_function_try_block_and_its_handler_are_function_bodies(directory, capsys):
    # Review of #105 (round 4): in `long tryfun(P p) try {`, `t = p.x + g();` was refused
    # as continuing a statement an earlier line starts; so was a handler's statement.
    start_at(directory, capsys, "ft.cpp", TRY_BLOCKS, "BODY")
    code, result = change(capsys, "p.x=50")
    assert code == 0 and result["after"] == "50", result
    assert main(["session", "step", "--into"]) == 0
    assert main(["session", "step", "--out"]) == 0
    capsys.readouterr()
    refused(capsys, "p.x=60", "not at the first instruction of the statement on line")
    continue_to(capsys, TRY_BLOCKS, "CAUGHT")
    code, result = change(capsys, "p.y=9")
    assert code == 0 and result["after"] == "9", result
    text = output(capsys)
    assert "T 1050" in text and "C 11" in text, text


@pytest.mark.timeout(240)
def test_c_fields_and_elements_are_marked_where_set_changes_them(directory, capsys):
    # Sixth evaluation, finding 7: an observation probe could not tell whether
    # `bins[3].hi` could be changed, and a field read through a pointer looked the same.
    path = directory / "fields.c"
    path.write_text(C_SOURCE)
    at = str(line_of(C_SOURCE, "STOP"))
    marked = {
        **dict.fromkeys(("s.end", "s.in.lo", "s.in.hi", "s.arr[2]", "grid[1][0]"), True),
        **dict.fromkeys(("b.whole", "c.loose", "f.tiny"), True),
        # As `set` refuses them: through a pointer, a bitfield, const, a union's, packed,
        # not an integer, an aggregate itself.
        **dict.fromkeys(("ps.start", "b.a", "c.fixed", "u.i", "k.i", "f.ch", "f.ratio"), False),
        **dict.fromkeys(("s.in", "s.arr"), False),
    }
    code = main(["probe", str(path), "--at", at, "--capture", ",".join(marked), "--json"])
    rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    assert code == 0 and {name: rows[name].get("settable") for name in marked} == marked, rows
    assert main(["probe", str(path), "--at", at, "--capture", "s.end,ps.end"]) == 0
    table = capsys.readouterr().out
    assert any("s.end" in row and row.endswith("2  [settable]") for row in table.splitlines())
    assert any("ps.end" in row and row.endswith("  2") for row in table.splitlines()), table
    assert main(["session", "start", str(path), "--at", at]) == 0
    capsys.readouterr()
    assert main(["session", "get", "s", "ps"]) == 0
    text = capsys.readouterr().out
    assert "  s.end: myint = 2  [settable]" in text and "  ps.end: myint = 2\n" in text, text
    # A caller's: never marked.
    assert main(["session", "continue", "--to", str(line_of(C_SOURCE, "SPAN"))]) == 0
    capsys.readouterr()
    assert main(["session", "get", "s.end", "--frame", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["settable"] is False
    assert main(["session", "get", "o.in.hi", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["settable"] is True
    assert main(["session", "close"]) == 0


LOOP = """#include <stdio.h>

struct bin { long lo; long hi; int count; };

int main(void) {
    struct bin bins[4] = { { 0, 2, 0 }, { 2, 4, 0 }, { 4, 6, 0 }, { 6, 7, 0 } };
    int values[3] = { 1, 5, 6 };
    for (int i = 0; i < 3; i++) {  // HEADER
        bins[i].count += values[i];  // BODY
    }
    printf("COUNT %d %d\\n", bins[0].count, bins[2].count);
    return 0;
}
"""


@pytest.mark.timeout(240)
def test_c_inspect_says_why_nothing_is_settable_on_a_loops_later_pass(directory, capsys):
    # Sixth evaluation, finding 8: at a loop header's later pass `session inspect`
    # marked nothing [settable] and gave no reason.
    path = directory / "loop.c"
    path.write_text(LOOP)
    assert main(["session", "start", str(path), "--at", str(line_of(LOOP, "BODY"))]) == 0
    capsys.readouterr()
    assert main(["session", "inspect", "--json"]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert "unsettable" not in snapshot, snapshot
    header = line_of(LOOP, "HEADER")
    for _ in range(3):
        # From the body, a step reaches the header's increment: a later pass.
        assert main(["session", "step", "--json"]) == 0
        if json.loads(capsys.readouterr().out)["location"].endswith(f"loop.c:{header}"):
            break
    assert main(["session", "inspect"]) == 0
    text = capsys.readouterr().out
    # Each reason names the locals it holds for (review 1).
    assert (
        "note: nothing here is [settable]: `i`, `bins`, `values`: the stop is not at the first "
        f"instruction of the statement on line {header}" in text
    ), text
    assert "on a loop's later pass" in text
    assert main(["session", "get", "bins[1]"]) == 0
    assert "[settable]" not in capsys.readouterr().out
    assert main(["session", "close"]) == 0


@pytest.mark.timeout(240)
def test_cpp_fields_are_marked_only_in_objects_the_frame_holds(directory, cxx, capsys):
    # Sixth evaluation, finding 7, in C++: through a reference member, a reference, a
    # const object or a standard-library type, never marked.
    path = directory / "fields.cpp"
    path.write_text(CPP_SOURCE)
    marked = {
        **dict.fromkeys(("sp.end", "al.end", "dv.d", "h.n", "c.count"), True),
        **dict.fromkeys(("h.ref.end", "rs.end", "cs.end", "pr.first"), False),
    }
    at = str(line_of(CPP_SOURCE, "STOP"))
    code = main(["probe", str(path), "--at", at, "--capture", ",".join(marked), "--json"])
    rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    if cxx == "gcc":
        # GCC's `cs` is in read-only data, which lldb-dap does not list (#167): read from
        # there, and never changed (#364).
        assert rows["cs.end"]["not_settable"] == "static", rows
    assert code == 0 and {name: rows[name].get("settable") for name in marked} == marked, rows


NOTED = """#include <stdio.h>

struct pt { long a; long b; };

__attribute__((noinline)) static long twice(long v) { return v * 2; }

static long labelled(int n) {
    long total = 0;
    int tries = 0;
again:
    total += twice(tries);  // LABEL
    struct pt q = { twice(total), 1 };
    if (++tries < n) goto again;
    return total + q.a;
}

int main(void) {
    long total = 0;
    int x = 0;
    do {
        struct pt y = { 1, 2 };  // DO_STRUCT
        y.a += twice(x);
        total += y.a + y.b;
        x++;
    } while (x < 3);
    printf("%ld %ld\\n", total, labelled(2));
    return 0;
}
"""


@pytest.mark.timeout(240)
def test_c_marks_agree_with_the_refusals_of_locals_with_a_note_or_withheld(directory, capsys):
    # Merge of #140 into the sixth evaluation's fixes: a local shown with a note that its
    # declaration runs after the stop (a `do` body's first line), and one withheld as
    # maybe not initialized (a label lets control return above it), refuse `set`, their
    # fields too; their rows and their fields' rows must never say [settable].
    start_at(directory, capsys, "noted.c", NOTED, "DO_STRUCT")
    try:
        # A later pass, as at the first: the loop returns to the line's first instruction.
        assert main(["session", "continue"]) == 0
        capsys.readouterr()
        assert main(["session", "get", "y", "y.a", "--json"]) == 0
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
        assert "may be the last pass's value" in rows["y"].get("note", ""), rows["y"]
        assert rows["y"]["settable"] is False and rows["y.a"]["settable"] is False, rows
        assert all(child.get("settable") is False for child in rows["y"]["children"]), rows
        refused(capsys, "y.a=5", "runs after this stop")
        assert main(["session", "inspect", "--json"]) == 0
        snapshot = json.loads(capsys.readouterr().out)
        marked = {v["name"]: v.get("settable") for s in snapshot["scopes"] for v in s["variables"]}
        assert marked["y"] is False and marked["x"] is True, marked
        code, result = change(capsys, "x=0")
        assert code == 0, result
    finally:
        main(["session", "close"])
    start_at(directory, capsys, "noted.c", NOTED, "LABEL")
    try:
        # A row with no value: `get` exits 1.
        assert main(["session", "get", "q", "--json"]) == 1
        [row] = json.loads(capsys.readouterr().out)
        assert "may not be initialized" in row.get("error", ""), row
        assert row.get("settable") is not True and not row.get("children"), row
        refused(capsys, "q.a=5", "may not be initialized")
    finally:
        main(["session", "close"])


JUMPING = """#include <setjmp.h>
#include <stdio.h>

static jmp_buf env;
struct pt { long a; long b; };

static long run(int n) {
    struct pt p = { 1, 2 };
    long total = 0;
    total += p.a;  // TOP
    if (setjmp(env) != 0) {
        total += 100;
    }
    if (--n > 0) longjmp(env, 1);
    return total + p.b;
}

int main(void) {
    printf("%ld\\n", run(2));
    return 0;
}
"""


@pytest.mark.timeout(240)
def test_c_a_program_using_setjmp_judges_changes_as_any_other(directory, capsys):
    # Review 2 of the sixth evaluation's PR refused every change in a program using
    # setjmp, which had no declarations read (#141). Since PR #144 such a program is
    # read as any other: above the setjmp call (and everywhere in a function making
    # none), values change where a statement starts; below it, only what is declared at
    # or below the stop is refused (see test_native_innermost).
    start_at(directory, capsys, "jump.c", JUMPING, "TOP")
    assert main(["session", "inspect"]) == 0
    text = capsys.readouterr().out
    notes = [line for line in text.splitlines() if line.lstrip().startswith("note:")]
    assert not notes and "total: long = 0  [settable]" in text, text
    for assignment, after in (("total=5", "5"), ("n=3", "3"), ("p.a=7", "7")):
        code, result = change(capsys, assignment)
        assert code == 0 and result["after"] == after, result
    # 5 + 7, then 100 on each of the two longjmps back, and p.b.
    assert "214" in output(capsys)


BLOCKS = """#include <stdio.h>
struct big { long a, b, c; };
__attribute__((noinline)) static long take(struct big big, int k) {
    long t = big.a + k;  // TAKE
    return t;
}
int main(void) {
    __block int count = 1;
    void (^bump)(void) = ^{ count += 1; };
    bump();
    int n = count;  // HERE
    struct big b = { 1, 2, 3 };
    printf("C %d %d %ld\\n", n, count, take(b, 1));
    return 0;
}
"""


@pytest.mark.timeout(180)
def test_a_c_local_reached_through_a_pointer_is_never_changed(directory, capsys):
    # #186's review, after round 1: clang describes a C `__block` variable (blocks are on
    # by default only on macOS) at a location read through its forwarding pointer, which
    # `--set count=9` wrote through. A by-value parameter passed through a hidden pointer
    # (a struct over 16 bytes on arm64) is the call's own copy, and still changes.
    if sys.platform != "darwin":
        pytest.skip("C blocks (`__block`) are enabled by default only on macOS")
    path = directory / "blocks.c"
    path.write_text(BLOCKS)
    probe = ["probe", str(path), "--json"]
    at = ["--at", str(line_of(BLOCKS, "HERE"))]
    assert main([*probe, *at, "--capture", "count"]) == 0
    (row,) = json.loads(capsys.readouterr().out)["rows"]
    assert row["value"] == "2" and row.get("settable") is not True, row
    code = main([*probe, *at, "--capture", "count", "--set", "count=9"])
    result = json.loads(capsys.readouterr().out)
    assert code != 0 and "is reached through a pointer or reference" in result["error"]["message"]
    at = ["--at", str(line_of(BLOCKS, "TAKE"))]
    assert main([*probe, *at, "--capture", "big.a", "--set", "big.a=100"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "C 2 2 101" in result["output"], result
