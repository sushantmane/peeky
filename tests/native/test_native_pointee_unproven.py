"""What is read through a C or C++ pointer is marked `[unproven]` where Peeky can't show the
pointer points at a live object (#339).

Peeky read any non-NULL pointee and showed it as a plain value: through a pointer into a call
that had returned, into a freed heap block, or two past an array's end, leftover bytes read
as the object. It still reads them, but marks them with why. A pointer into a local or an
array a listed frame holds, or into the program's static storage, reads as before.
"""

import json
import os
import shutil
import subprocess

import pytest

from peeky.cli import main

SOURCE = """#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct cell { long a; long b; };
struct holder { long *p; int n; };

static long *gone(void) {
  long local[2] = {41, 42};
  long *p = local;
  return p;
}

static char *word(void) {
  char buf[8];
  strcpy(buf, "stale");
  char *w = buf;
  return w; /* WORD */
}

static long peek(const long *v, int n) {
  long s = 0;
  for (int i = 0; i < n; i++) s += v[i];
  return s; /* PEEK */
}

int main(void) {
  long x = 7;
  long *px = &x;
  long arr[4] = {1, 2, 3, 4};
  long *pa = arr + 1;
  struct cell cells[3] = {{1, 2}, {3, 4}, {5, 6}};
  struct cell *two = cells + 4;
  long *dang = gone();
  long *live = malloc(2 * sizeof(long));
  live[0] = 11;
  live[1] = 12;
  long *freed = malloc(2 * sizeof(long));
  freed[0] = 21;
  free(freed);
  struct cell *hc = malloc(sizeof(struct cell));
  hc->a = 31;
  hc->b = 32;
  struct holder h = {live, 2};
  char name[8] = "maya";
  char *pn = name;
  const char *lit = "literal";
  char *dup = malloc(8);
  strcpy(dup, "heap");
  char *old = word();
  long total = peek(arr, 4);
  printf("%ld %ld %ld %ld %s %s %s %d %ld %p %p %p %p\\n", *px, *pa, total, live[0], pn, lit,
         dup, h.n, hc->a, (void *)two, (void *)dang, (void *)old, (void *)freed); /* MAIN */
  free(live);
  free(hc);
  free(dup);
  return 0;
}
"""


def _gcc():
    gcc = shutil.which("gcc")
    if not gcc:
        return None
    version = subprocess.run([gcc, "--version"], capture_output=True, text=True)
    return gcc if "Free Software Foundation" in version.stdout else None


@pytest.fixture(params=["clang", "gcc"])
def program(request, tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    compiler = os.environ.get("PEEKY_NATIVE_CC", "clang") if request.param == "clang" else _gcc()
    if compiler is None:
        pytest.skip("needs GCC")
    monkeypatch.setenv("PEEKY_NATIVE_CC", compiler)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "pointees"
    root.mkdir()
    (root / "pointees.c").write_text(SOURCE)
    return root


def line_of(marker, source=SOURCE):
    return next(i for i, text in enumerate(source.splitlines(), 1) if f"/* {marker} */" in text)


def probed(capsys, target, at, capture, *extra):
    code = main(["probe", str(target), "--at", at, "--capture", capture, "--json", *extra])
    output = capsys.readouterr().out
    assert code == 0, output
    return json.loads(output)


def through(pointer, text=False):
    lead = "" if text else f"read through `{pointer}`: "
    what = "its text" if text else "this value"
    return f"{lead}Peeky can't show that `{pointer}` points at a live object, so {what} may be "


HEAP = (
    "its address is in no local, member or element that Peeky could place in the frames it "
    "lists, nor in the program's static memory; if it is a heap block, Peeky can't tell "
    "whether that block is still allocated, so check where the program frees it"
)
BELOW = (
    "it points below the stack pointer, where no live call keeps its locals, as a pointer "
    "into a call that has returned does; read the value before that call returns"
)


def unproven(row, pointer, why, text=False):
    assert row.get("view") == "unproven", row
    assert row["note"].startswith(through(pointer, text)), row
    assert row["note"].endswith(why), row


@pytest.mark.timeout(240)
def test_what_is_read_through_a_pointer_is_marked_unless_peeky_places_it(program, capsys):
    selectors = (
        "px[0],pa[0],pn,pn[0],lit,h,two[0],two.a,dang[0],live[0],freed[0],hc.a,hc[0],h.p[0],dup,old"
    )
    result = probed(capsys, program, f"pointees.c:{line_of('MAIN')}", selectors)
    rows = {row["selector"]: row for row in result["rows"]}
    # A local, an array element, a `char` array's text, a string literal (the program's own
    # image), and a struct whose pointer member shows only its address: no mark.
    assert rows["px[0]"]["value"] == "7" and rows["pa[0]"]["value"] == "2", rows
    assert rows["pn"]["value"].endswith('"maya"') and rows["pn[0]"]["value"] == "'m'", rows
    assert rows["lit"]["value"].endswith('"literal"'), rows
    assert rows["h"]["value"].startswith("{p = 0x") and rows["h"]["value"].endswith("n = 2}")
    for selector in ("px[0]", "pa[0]", "pn", "pn[0]", "lit", "h"):
        assert "view" not in rows[selector] and "note" not in rows[selector], rows[selector]
    # Two past `cells`' end: the bytes there are read, marked.
    past = "it holds the address of `cells[4]`, past the end of `cells`, which has 3 elements"
    for selector in ("two[0]", "two.a"):
        unproven(rows[selector], "two", f"{past}; read `cells` by name")
    # Into `gone`'s frame, which returned.
    unproven(rows["dang[0]"], "dang", BELOW)
    # A live heap block and a freed one: alike, since nothing tells them apart.
    assert rows["live[0]"]["value"] == "11", rows
    for selector, pointer in (("live[0]", "live"), ("freed[0]", "freed"), ("h.p[0]", "h.p")):
        unproven(rows[selector], pointer, HEAP)
    # A struct read through one, field or whole.
    assert rows["hc.a"]["value"] == "31" and rows["hc[0]"]["value"] == "{a = 31, b = 32}"
    unproven(rows["hc.a"], "hc", HEAP)
    unproven(rows["hc[0]"], "hc", HEAP)
    # A `char *`'s own text, read through it: from the heap, and from `word`'s frame.
    assert rows["dup"]["value"].endswith('"heap"'), rows
    unproven(rows["dup"], "dup", HEAP, text=True)
    unproven(rows["old"], "old", BELOW, text=True)

    # The table marks them; the notes say why, once each.
    at = f"pointees.c:{line_of('MAIN')}"
    assert main(["probe", str(program), "--at", at, "--capture", "live[0],px[0]"]) == 0
    table = capsys.readouterr().out
    assert "  11  [unproven]" in table and "[note]" not in table, table
    assert "live[0]: Peeky can't show that `live` points at a live object" in table, table


@pytest.mark.timeout(240)
def test_a_callers_array_read_through_a_parameter_is_unmarked(program, capsys):
    result = probed(capsys, program, f"pointees.c:{line_of('PEEK')}", "v[0],v")
    rows = {row["selector"]: row for row in result["rows"]}
    assert rows["v[0]"]["value"] == "1" and "view" not in rows["v[0]"], rows


@pytest.mark.timeout(240)
def test_session_get_and_inspect_mark_what_is_read_through_one(program, capsys):
    at = f"pointees.c:{line_of('MAIN')}"
    assert main(["session", "start", str(program), "--at", at]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "get", "hc", "h", "h.p", "px"]) == 0
        out = capsys.readouterr().out
        assert "  hc.a: long = 31  [unproven]\n  hc.b: long = 32  [unproven]\n" in out, out
        assert f"  note: {through('hc')}left over: {HEAP}" in out, out
        # A struct's pointer member is its own address; what it points at is marked.
        assert "  h.p: long * = 0x" in out and "  h.n: int = 2" in out, out
        assert "  h.p[0]: long = 11  [unproven]" in out, out
        assert "  px[0]: long = 7\n" in out, out
        assert main(["session", "inspect"]) == 0
        out = capsys.readouterr().out
        assert '"heap"  [unproven]' in out and f"    note: {through('dup', True)}" in out, out
        assert '"literal"\n' in out or '"literal" ' in out, out
        assert main(["session", "get", "hc", "--json"]) == 0
        (row,) = json.loads(capsys.readouterr().out)
        assert [child.get("view") for child in row["children"]] == ["unproven", "unproven"]
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(240)
def test_a_returned_string_from_a_call_that_returned_is_marked(program, capsys):
    (row,) = probed(capsys, program, f"pointees.c:{line_of('WORD')}", "return")["rows"]
    assert row["type"].replace(" ", "") == "char*", row
    assert row.get("view") == "unproven", row
    assert row["note"].startswith(
        "Peeky can't show that the pointer it returned points at a live object, so its text"
    ), row
    assert row["note"].endswith(BELOW), row


@pytest.mark.timeout(300)
def test_diff_qualifies_a_difference_read_through_one(program, capsys):
    at = f"pointees.c:{line_of('MAIN')}"
    for extra in ((), ("--old", "live[0] = 11;", "--new", "live[0] = 13;")):
        probed(capsys, program, at, "live[0],px[0],total", *extra)
    assert main(["queries", "diff", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    (row,) = [row for row in rows if row["selector"] == "live[0]"]
    assert (row["value_a"], row["value_b"]) == ("11", "13"), rows
    assert row["note"] == (
        "qualified (run a: unproven, run b: unproven): not evidence by itself; queries run "
        "shows why"
    ), rows


CPP = """#include <cstdio>
#include <string>

struct Counter {
  long count = 0;
  void add(long n) {
    count += n; /* ADD */
  }
};

int main() {
  Counter onstack;
  Counter *heap = new Counter();
  onstack.add(1);
  heap->add(2);
  std::string s = "local";
  std::string *pl = &s;
  std::string *ps = new std::string("text");
  std::printf("%ld %ld %s %s\\n", onstack.count, heap->count, pl->c_str(), ps->c_str()); /* MAIN */
  delete ps;
  delete heap;
  return 0;
}
"""


@pytest.mark.timeout(240)
def test_cpp_marks_members_read_through_a_heap_objects_this(tmp_path, monkeypatch, cxx, capsys):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "count.cpp").write_text(CPP)
    target = tmp_path / "count.cpp"
    at = f"count.cpp:{line_of('ADD', CPP)}"
    rows = probed(capsys, target, at, "this.count", "--hits", "2")["rows"]
    # `onstack.add(1)`: `this` is `main`'s local. `heap->add(2)`: a heap block.
    assert [row["value"] for row in rows] == ["0", "0"], rows
    assert "view" not in rows[0], rows
    unproven(rows[1], "this", HEAP)
    rows = probed(capsys, target, f"count.cpp:{line_of('MAIN', CPP)}", "heap.count,pl,ps")
    rows = {row["selector"]: row for row in rows["rows"]}
    assert rows["heap.count"]["value"] == "2", rows
    unproven(rows["heap.count"], "heap", HEAP)
    # A `std::string *`: at `main`'s `s`, its text is unmarked; at a heap string, marked.
    # lldb-dap 18 reads no libstdc++ string's text, and gives no address for `s`: nothing
    # is read, and nothing marked.
    # (A string Peeky read from memory is `view: memory`.)
    assert rows["pl"].get("view") != "unproven" and "note" not in rows["pl"], rows
    if rows["ps"]["value"].endswith('"text"'):
        unproven(rows["ps"], "ps", HEAP, text=True)
    else:
        assert rows["ps"].get("view") != "unproven", rows
