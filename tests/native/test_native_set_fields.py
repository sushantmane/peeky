"""#105: `--set` on scalar fields and elements of C and C++ locals the stopped frame
holds itself, read back and seen by the program; everything else refused."""

import json
import os

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
    refused(capsys, "ps.start=8", "`ps` is a pointer", "session step --out")
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
def test_cpp_fields_change_only_in_objects_the_frame_holds(directory, capsys):
    if not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_CXX to enable C++ conformance")
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
