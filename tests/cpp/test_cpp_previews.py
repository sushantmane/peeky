"""C++ struct, class and array previews (#136), on real builds: bases, a vtable,
anonymous members, unions, references and standard-library members."""

import contextlib
import json
import os
import sys

import pytest

from peeky.cli import main

SHAPES = """#include <cstdio>
#include <map>
#include <string>
#include <vector>
struct Pt { int x; int y; };
struct Line { Pt a; Pt b; const char *label; Pt *next; int (*fn)(int); };
struct Bits { unsigned flag : 1; unsigned mode : 3; int rest; };
union Num { int i; float f; };
struct Tagged { int kind; union { int i; double d; }; struct { int u; int v; }; };
struct Holder { Num num; Pt at; };
struct Base { virtual ~Base() {} int b = 1; };
struct Derived : Base { int d = 2; };
namespace geo { struct Box { Pt lo; Pt hi; }; }
struct Mixed { std::string s; std::vector<int> v; double ratio; bool ok; char c; };
enum Color { RED, GREEN };
struct WithEnum { Color color; float f; };
static int twice(int x) { return 2 * x; }
static int area(const geo::Box &box, const Num &num) {
    return (box.hi.x - box.lo.x) * (box.hi.y - box.lo.y) + num.i; // IN_AREA
}
int main() {
    Pt p{1, 2};
    Pt q{3, 4};
    Line line{{1, 2}, {3, 4}, "edge", &q, twice};
    Bits bits{1, 5, 7};
    Num num; num.i = 1065353216;
    Tagged tagged; tagged.kind = 1; tagged.i = 9; tagged.u = 5; tagged.v = 6;
    Holder holder{num, {7, 8}};
    Derived derived;
    geo::Box box{{0, 0}, {2, 3}};
    Mixed mixed{std::string("hi"), {1, 2}, 0.5, true, 'z'};
    WithEnum we{GREEN, 1.5f};
    Pt pts[3] = {{1, 1}, {2, 2}, {3, 3}};
    std::vector<Pt> vp{{1, 2}, {3, 4}};
    std::map<int, Pt> mp{{1, {5, 6}}};
    const Pt &rp = p;
    Pt *pp = &p;
    int n = area(box, num) + p.x + line.a.x + (int)bits.mode + pts[2].x + rp.x + pp->y; // HERE
    Pt later{8, 9};
    std::printf("%d %d %zu %zu %d\\n", n, later.x, vp.size(), mp.size(), tagged.kind + holder.at.x);
    return 0;
}
"""
EXPECTED = {
    "p": "{x = 1, y = 2}",
    "bits": "{flag = 1, mode = 5, rest = 7}",
    # An anonymous union is not read as one of its members; an anonymous struct's
    # members show in braces, with no name.
    "tagged": "{kind = 1, <union>, {u = 5, v = 6}}",
    "holder": "{num = <union>, at = {x = 7, y = 8}}",
    # A base class shows as LLDB lists it, named by its class; the vtable pointer is not
    # a member of the program's.
    "derived": "{Base = {b = 1}, d = 2}",
    "box": "{lo = {x = 0, y = 0}, hi = {x = 2, y = 3}}",
    "we": "{color = GREEN, f = 1.5}",
    "pts": "[{x = 1, y = 1}, {x = 2, y = 2}, {x = 3, y = 3}]",
    "pts[1]": "{x = 2, y = 2}",
    "pp[0]": "{x = 1, y = 2}",
    "mp[1]": "{x = 5, y = 6}",
}


@pytest.fixture
def cpp(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "shapes.cpp"
    source.write_text(SHAPES)
    return source


def line_of(text, marker):
    return next(i for i, line in enumerate(text.splitlines(), 1) if marker in line)


@contextlib.contextmanager
def session(source, line, capsys):
    assert main(["session", "start", str(source), "--at", str(line)]) == 0
    capsys.readouterr()

    def get(*selectors):
        main(["session", "get", *selectors, "--json"])
        return {row["selector"]: row for row in json.loads(capsys.readouterr().out)}

    try:
        yield get
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(120)
def test_classes_previews_follow_lldb_and_never_guess(cpp, capsys):
    with session(cpp, line_of(SHAPES, "// HERE"), capsys) as get:
        rows = get(*EXPECTED, "num", "line", "mixed", "rp", "pp", "vp", "mp", "later")
        for selector, value in EXPECTED.items():
            assert rows[selector].get("value") == value, (selector, rows[selector])
        # A union is not previewed: lldb-dap names it as it names a struct.
        assert " @ 0x" in rows["num"]["value"], rows["num"]
        # Pointers inside show their addresses only, never what they point at, nor a
        # C string or a function's name the debugger reads through them.
        line = rows["line"]["value"]
        assert line.startswith("{a = {x = 1, y = 2}, b = {x = 3, y = 4}, label = 0x"), line
        assert '"edge"' not in line and "twice" not in line and line.count("0x") == 3, line
        # A reference shows its address, then what it refers to; a pointer only its address.
        assert rows["rp"]["value"].endswith(" {x = 1, y = 2}"), rows["rp"]
        assert rows["rp"]["value"].startswith("0x") and rows["pp"]["value"].startswith("0x")
        assert " {" not in rows["pp"]["value"], rows["pp"]
        # Standard-library values keep their own views (#119, #128): a vector's or map's
        # summary, a string's checked text.
        assert rows["vp"]["value"] == "size=2" and rows["mp"]["value"] == "size=1"
        mixed = rows["mixed"]["value"]
        tail = ", v = size=2, ratio = 0.5, ok = true, c = 'z'}"
        assert mixed in ('{s = "hi"' + tail, "{s = <no value>" + tail), mixed
        if sys.platform == "darwin" or os.environ.get("PEEKY_EXPECT_READ_MEMORY"):
            assert mixed.startswith('{s = "hi"'), mixed
        # Declared below the stop: no value, so no preview.
        assert rows["later"].get("unavailable") and "value" not in rows["later"]
        # Fields stay selectable and marked as before (#141).
        fields = {c["selector"]: c for c in rows["pts[1]"]["children"]}
        assert fields["pts[1].x"]["value"] == "2" and fields["pts[1].x"].get("settable") is True
    with session(cpp, line_of(SHAPES, "// IN_AREA"), capsys) as get:
        rows = get("box", "num")
        # A reference to a class is previewed after its address; one to a union is not.
        assert rows["box"]["value"].endswith(" {lo = {x = 0, y = 0}, hi = {x = 2, y = 3}}")
        assert rows["num"]["value"].startswith("0x") and " {" not in rows["num"]["value"]


OPTIMIZED = """#include <cstdio>
struct Bin { int lo; int hi; int count; };
__attribute__((noinline)) void fill(Bin *b, int n) {
    for (int i = 0; i < n; i++) { b[i].lo = i; b[i].hi = i + 1; b[i].count = 0; }
}
__attribute__((noinline)) int total(const Bin *b, int n) {
    int t = 0;
    for (int i = 0; i < n; i++) t += b[i].hi;
    return t;
}
int main() {
    Bin bins[3];
    fill(bins, 3);
    int t = total(bins, 3); // HERE
    std::printf("%d\\n", t);
    return 0;
}
"""


@pytest.mark.timeout(120)
def test_an_optimized_cpp_build_previews_its_memory(cpp, tmp_path, capsys):
    (tmp_path / "opt.cpp").write_text(OPTIMIZED)
    config = tmp_path / "native.json"
    config.write_text(
        json.dumps(
            {
                "language": "cpp",
                "sources": ["opt.cpp"],
                "compiler": os.environ["PEEKY_NATIVE_CXX"],
                "adapter": os.environ["PEEKY_NATIVE_DAP"],
                "dwarfdump": os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
                "dsymutil": os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
                "sysroot": os.environ.get("PEEKY_NATIVE_SYSROOT"),
                "optimization": "O2",
            }
        )
    )
    line = line_of(OPTIMIZED, "// HERE")
    command = ["probe", str(tmp_path), "--config", str(config), "--at", f"opt.cpp:{line}"]
    assert main([*command, "--capture", "bins", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    (row,) = result["rows"]
    assert any("optimized build" in warning for warning in result["warnings"]), result
    expected = "[{lo = 0, hi = 1, count = 0}, {lo = 1, hi = 2, count = 0}, "
    assert row.get("value") == expected + "{lo = 2, hi = 3, count = 0}]", row
