"""What is read through a C or C++ pointer is marked `[unproven]` where Peeky can't show the
pointer points at a live object.

Through a pointer into a call that has returned, into a freed heap block, or two past an
array's end, leftover bytes would read as the object. Peeky reads them, but marks them with
why. A pointer into a local or an array a listed frame holds, or into the program's static
storage, reads plain.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

from peeky.cli import main

SOURCE = """#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct cell { long a; long b; };
struct holder { long *p; int n; };
union num { long i; double d; };

static long table[3] = {1, 2, 3};

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

int main(int argc, char **argv) {
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
  char rows[2][8] = {"ab", "cd"};
  char *row = rows[1];
  const char *lit = "literal";
  char *dup = malloc(8);
  strcpy(dup, "heap");
  char *old = word();
  long total = peek(arr, 4);
  union num un;
  un.i = 5;
  double *pd = &un.d;
  union num ua[1];
  ua[0].i = 6;
  double *pua = &ua[0].d;
  const char *after = lit + 8;
  long *tp = table + 1;
  long *tpast = table + 5;
  long *code = (long *)(uintptr_t)&main;
  const char *arg = argc > 1 ? argv[1] : "none";
  long *la = (long *)argv;
  printf("%ld %ld %ld %ld %s %s %s %s %d %ld %p %p %p %p\\n", *px, *pa, total, live[0], pn, row,
         lit, dup, h.n, hc->a, (void *)two, (void *)dang, (void *)old, (void *)freed);
  printf("%ld %p %ld %p %ld %p %p %s %p %p\\n", un.i, (void *)pd, ua[0].i, (void *)pua, *tp,
         (void *)tpast, (void *)code, arg, (void *)la, (void *)after); /* MAIN */
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
IMAGE = (
    "it points into the program's loaded image, but no compatible global, static local or "
    "proven string literal is there; check where it was assigned: if it should point at a "
    "global, read that global by name; to prove a string literal, store it in a named char array"
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
        "px[0],pa[0],pn,pn[0],row,lit,h,two[0],two.a,dang[0],live[0],freed[0],hc.a,hc[0],h.p[0],"
        "dup,old,pd[0],pua[0],tp[0],tpast[0],code[0],arg,la[0],after"
    )
    result = probed(capsys, program, f"pointees.c:{line_of('MAIN')}", selectors, "--", "Maya")
    rows = {row["selector"]: row for row in result["rows"]}
    # A local, an array element, a `char` array's text, a 2-D array's row, a string literal
    # (the program's own image), and a struct whose pointer member shows only its address:
    # no mark.
    assert rows["px[0]"]["value"] == "7" and rows["pa[0]"]["value"] == "2", rows
    assert rows["pn"]["value"].endswith('"maya"') and rows["pn[0]"]["value"] == "'m'", rows
    assert rows["lit"]["value"].endswith('"literal"') and rows["row"]["value"].endswith('"cd"')
    assert rows["h"]["value"].startswith("{p = 0x") and rows["h"]["value"].endswith("n = 2}")
    for selector in ("px[0]", "pa[0]", "pn", "pn[0]", "row", "h"):
        assert "view" not in rows[selector] and "note" not in rows[selector], rows[selector]
    if sys.platform == "darwin":
        # `__TEXT,__cstring` holds only string literals: from the pointer to its NUL.
        assert "view" not in rows["lit"], rows
    else:
        # The linkers merge an ELF's literals into `.rodata`, among other constants: no
        # proof there, nor past the literal.
        unproven(rows["lit"], "lit", IMAGE, text=True)
        unproven(rows["after"], "after", IMAGE, text=True)
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
    # A union's other member, which a declaration doesn't prove holds a value.
    unproven(rows["pd[0]"], "pd", "read `un` by name, and the member the program last stored")
    # An element of an array of unions, likewise.
    assert "it points into the union `ua[0]`" in rows["pua[0]"]["note"], rows
    # A global's element; past its end; into the program's code (not any address in a
    # load segment counts).
    assert rows["tp[0]"]["value"] == "2" and "view" not in rows["tp[0]"], rows
    past = "it holds the address of `table[5]`, past the end of `table`, which has 3 elements"
    unproven(rows["tpast[0]"], "tpast", f"{past}; read `table` by name")
    unproven(rows["code[0]"], "code", IMAGE)
    # `main`'s argument string, read through a `char *`; the vector itself as `long`s.
    assert rows["arg"]["value"].endswith('"Maya"') and "view" not in rows["arg"], rows
    assert rows["la[0]"]["view"] == "unproven", rows

    # The table marks them; the notes say why, once each.
    at = f"pointees.c:{line_of('MAIN')}"
    assert main(["probe", str(program), "--at", at, "--capture", "live[0],px[0]"]) == 0
    table = capsys.readouterr().out
    assert "  11  [unproven]" in table and "[note]" not in table, table
    assert "live[0]: Peeky can't show that `live` points at a live object" in table, table


@pytest.mark.timeout(240)
def test_more_arguments_than_peeky_reads_say_so(program, capsys):
    # 64 arguments (the most a probe passes), so 65 strings with the program's name: Peeky
    # reads 64 of them, so a heap `char *` may be one it didn't; its note says so, and
    # never that a local was left unplaced.
    words = [f"w{i}" for i in range(64)]
    at = f"pointees.c:{line_of('MAIN')}"
    (row,) = probed(capsys, program, at, "dup", "--", *words)["rows"]
    unproven(row, "dup", ARGUMENT_LIMIT, text=True)


ARGUMENT_LIMIT = (
    "Peeky didn't read every one of `main`'s argument and environment strings at this stop: "
    "the per-stop limit was reached; read the string it should point at by name"
)


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

struct Tagged { long tag = 3; };
struct Shape { long id = 5; };
struct Circle : Tagged, Shape { long r = 2; };

int main() {
  Counter onstack;
  Counter *heap = new Counter();
  onstack.add(1);
  heap->add(2);
  std::string s = "local";
  std::string *pl = &s;
  std::string *ps = new std::string("text");
  Circle circle;
  Shape *shape = &circle;
  std::printf("%ld %ld %s %s %ld\\n", onstack.count, heap->count, pl->c_str(), ps->c_str(),
              shape->id); /* MAIN */
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
    at = f"count.cpp:{line_of('MAIN', CPP)}"
    rows = {
        row["selector"]: row
        for row in probed(capsys, target, at, "heap.count,pl,ps,shape.id")["rows"]
    }
    assert rows["heap.count"]["value"] == "2", rows
    unproven(rows["heap.count"], "heap", HEAP)
    # A base class of a local, 8 bytes into it: a live `Shape`.
    assert rows["shape.id"]["value"] == "5" and "view" not in rows["shape.id"], rows
    # A `std::string *`: at `main`'s `s`, its text is unmarked; at a heap string, marked.
    # lldb-dap 18 reads no libstdc++ string's text, and gives no address for `s`: nothing
    # is read, and nothing marked.
    # (A string Peeky read from memory is `view: memory`.)
    assert rows["pl"].get("view") != "unproven" and "note" not in rows["pl"], rows
    if rows["ps"]["value"].endswith('"text"'):
        unproven(rows["ps"], "ps", HEAP, text=True)
    else:
        assert rows["ps"].get("view") != "unproven", rows
