"""Nothing is read through a C or C++ pointer one past an array's end (#336).

With `long queue[4]` and `long *end = queue + 4;`, `session get end` listed `end[0]` with the
stack's next bytes as its value. C lets a pointer hold that address, but nothing there belongs
to the array: Peeky shows the pointer's own row and says why it reads nothing through it.
"""

import json
import os
import shutil
import subprocess

import pytest

from peeky.cli import main

SOURCE = """#include <stdio.h>
#include <string.h>

struct pt { long x; long y; };
struct two { long a[4]; long b[4]; };
struct ring { long buf[4]; int head; };

static long total(const long *p, const long *end) {
  long s = 0;
  while (p < end) s += *p++;
  return s; /* TOTAL */
}

static char *fill(char *out) {
  memcpy(out, "abcdefg", 8);
  return out + 8; /* FILL */
}

int main(void) {
  long queue[4] = {1, 2, 3, 4};
  long *end = queue + 4;
  long *last = queue + 3;
  struct pt pts[2] = {{1, 2}, {3, 4}};
  struct pt *pend = pts + 2;
  struct two t = {{1, 2, 3, 4}, {5, 6, 7, 8}};
  long *tend = t.a + 4;
  struct ring r = {{9, 8, 7, 6}, 2};
  long *rend = r.buf + 4;
  char name[8] = "abcdefg";
  char *nend = name + 8;
  char *nmid = name + 2;
  char buf[8];
  char *bend = fill(buf);
  long sum = total(queue, end) + total(t.a, tend);
  printf("%ld %ld %ld %ld %s %ld %s\\n", sum, *last, rend[-1], pend[-1].x, nmid,
         (long)strlen(nend - 1), bend - 8); /* MAIN */
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
    root = tmp_path / "ends"
    root.mkdir()
    (root / "ends.c").write_text(SOURCE)
    return root


def line_of(marker, source=SOURCE):
    return next(i for i, text in enumerate(source.splitlines(), 1) if f"/* {marker} */" in text)


def probed(capsys, target, at, capture, *extra):
    code = main(["probe", str(target), "--at", at, "--capture", capture, "--json", *extra])
    output = capsys.readouterr().out
    assert code == 0, output
    return json.loads(output)["rows"]


def reason(pointer, array, frame=None):
    where = f" in main (frame {frame})" if frame else ""
    return (
        f"not read: `{pointer}` holds the address one past the end of `{array}`{where}, and "
        f"nothing there belongs to `{array}`"
    )


def placed(row):
    """A row's `past_end` without `also`: what else starts at that address depends on how
    the compiler lays out the frame (GCC puts `pts` right after `queue`)."""
    return {key: value for key, value in row["past_end"].items() if key != "also"}


# lldb-dap 20 on gives every value's address; 18 gives none for a `char[N]`, whose text it
# shows instead (as for #302's elements).
ADDRESSED = bool(os.environ.get("PEEKY_EXPECT_READ_MEMORY"))


@pytest.mark.timeout(240)
def test_nothing_is_read_one_past_an_arrays_end(program, capsys):
    selectors = "end,end[0],last[0],pend,pend[0],pend.x,tend,tend[0],rend[0],nend,nend[0],nmid"
    rows = probed(capsys, program, f"ends.c:{line_of('MAIN')}", selectors)
    rows = {row["selector"]: row for row in rows}
    # The pointer's own row: its address, and where it is.
    assert rows["end"]["value"].startswith("0x") and " " not in rows["end"]["value"], rows
    assert placed(rows["end"]) == {"selector": "queue", "length": 4}, rows
    assert rows["end[0]"]["error"].startswith(f"end[0] {reason('end', 'queue')}; "), rows
    assert "value" not in rows["end[0]"], rows
    # Inside the array: read as before.
    assert rows["last[0]"]["value"] == "4", rows
    # An array of structs: neither the element nor a field through it.
    assert placed(rows["pend"]) == {"selector": "pts", "length": 2}, rows
    for selector in ("pend[0]", "pend.x"):
        assert rows[selector]["error"].startswith(f"{selector} {reason('pend', 'pts')}"), rows
    # A struct's arrays: the one that follows starts there, and is named, but not read.
    assert rows["tend[0]"]["error"].endswith(
        "; `t.b[0]` starts there too, but nothing shows `tend` points at it: read `t.b[0]` or "
        "`t.a` by name"
    ), rows
    assert rows["rend[0]"]["error"].startswith(f"rend[0] {reason('rend', 'r.buf')}"), rows
    if ADDRESSED:
        # A `char *`: never the text LLDB read past the array.
        assert rows["nend"]["value"].startswith("0x") and '"' not in rows["nend"]["value"], rows
        assert rows["nend[0]"]["error"].startswith(f"nend[0] {reason('nend', 'name')}"), rows
    assert rows["nmid"]["value"].endswith('"cdefg"'), rows

    at = f"ends.c:{line_of('MAIN')}"
    assert main(["probe", str(program), "--at", at, "--capture", "end,end[0]"]) == 0
    table = capsys.readouterr().out
    assert "  [at the address one past queue's end" in table, table
    assert f"<end[0] {reason('end', 'queue')}; " in table, table


@pytest.mark.timeout(240)
def test_a_callers_array_is_known_in_the_function_it_calls(program, capsys):
    # `total(queue, end)`: in `total`, `end` is a parameter, one past `main`'s `queue`.
    rows = probed(capsys, program, f"ends.c:{line_of('TOTAL')}", "end,end[0]", "--hits", "all")
    first, second = [row for row in rows if row["selector"] == "end[0]"]
    assert first["error"].startswith(f"end[0] {reason('end', 'queue', frame=1)}; "), rows
    assert first["error"].endswith("in main (`session get --frame 1`)"), rows
    assert second["error"].startswith(f"end[0] {reason('end', 't.a', frame=1)}; `t.b[0]`"), rows
    assert [row["past_end"]["frame"] for row in rows if row["selector"] == "end"] == [1, 1]


SUM = """long total(const long *p, const long *end) {
  long s = 0;
  while (p < end) s += *p++;
  return s; /* TOTAL */
}
"""
CALLER = """#include <stdio.h>
long total(const long *p, const long *end);
int main(void) {
  long queue[4] = {1, 2, 3, 4};
  printf("%ld\\n", total(queue, queue + 4));
  return 0;
}
"""


@pytest.mark.timeout(240)
def test_an_array_in_another_source_file_is_known(program, capsys):
    # Each source file's debug information has its own `long`: they are one type.
    (program / "sum.c").write_text(SUM)
    (program / "caller.c").write_text(CALLER)
    config = program / "two.json"
    tools = {
        "compiler": os.environ["PEEKY_NATIVE_CC"],
        "adapter": os.environ["PEEKY_NATIVE_DAP"],
        "dwarfdump": os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        "dsymutil": os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        "sysroot": os.environ.get("PEEKY_NATIVE_SYSROOT"),
    }
    config.write_text(json.dumps({"language": "c", "sources": ["caller.c", "sum.c"], **tools}))
    at = f"sum.c:{line_of('TOTAL', SUM)}"
    rows = probed(capsys, program, at, "end,end[0]", "--config", str(config))
    assert placed(rows[0]) == {"selector": "queue", "length": 4, "frame": 1, "function": "main"}
    assert rows[1]["error"].startswith(f"end[0] {reason('end', 'queue', frame=1)}"), rows


CELLS = """#include <stdio.h>
struct Cell { long x; };
void use_same(void *v);
void use_other(void *v);
int main(void) {
  struct Cell cells[2] = {{1}, {2}};
  use_same(cells + 2);
  use_other(cells + 2);
  return 0;
}
"""
SAME = """#include <stdio.h>
struct Cell { long x; };
void use_same(void *v) {
  struct Cell *p = v;
  printf("%p\\n", (void *)p); /* SAME */
}
"""
OTHER = """#include <stdio.h>
struct Cell { double y; };
void use_other(void *v) {
  struct Cell *p = v;
  printf("%p\\n", (void *)p); /* OTHER */
}
"""


@pytest.mark.timeout(240)
def test_a_struct_of_one_name_matches_across_files_only_with_one_layout(program, capsys):
    # Two files may give `struct Cell` two layouts: the name and size alone prove nothing.
    for name, text in (("cells.c", CELLS), ("same.c", SAME), ("other.c", OTHER)):
        (program / name).write_text(text)
    config = program / "cells.json"
    tools = {
        "compiler": os.environ["PEEKY_NATIVE_CC"],
        "adapter": os.environ["PEEKY_NATIVE_DAP"],
        "dwarfdump": os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        "dsymutil": os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        "sysroot": os.environ.get("PEEKY_NATIVE_SYSROOT"),
    }
    sources = ["cells.c", "same.c", "other.c"]
    config.write_text(json.dumps({"language": "c", "sources": sources, **tools}))
    at = f"same.c:{line_of('SAME', SAME)}"
    same = probed(capsys, program, at, "p,p.x", "--config", str(config))
    assert placed(same[0]) == {"selector": "cells", "length": 2, "frame": 1, "function": "main"}
    assert same[1]["error"].startswith(f"p.x {reason('p', 'cells', frame=1)}"), same
    at = f"other.c:{line_of('OTHER', OTHER)}"
    (other,) = probed(capsys, program, at, "p", "--config", str(config))
    assert "past_end" not in other and "one past" not in str(other), other


@pytest.mark.timeout(240)
def test_a_returned_c_string_one_past_an_arrays_end_shows_its_address(program, capsys):
    # `fill` returns `out + 8`: LLDB's text of it is what lies past `buf`.
    (row,) = probed(capsys, program, f"ends.c:{line_of('FILL')}", "return")
    assert row["type"].replace(" ", "") == "char*", row
    if ADDRESSED:
        assert '"' not in row["value"], row
        assert placed(row) == {"selector": "buf", "length": 8}, row


@pytest.mark.timeout(240)
def test_session_get_lists_nothing_through_it(program, capsys):
    at = f"ends.c:{line_of('MAIN')}"
    assert main(["session", "start", str(program), "--at", at]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "get", "end", "pend", "last"]) == 0
        out = capsys.readouterr().out
        assert "end: long * = 0x" in out and "  [at the address one past queue's end" in out
        assert "  <children: none read: `end` holds the address one past the end of `queue`" in out
        assert "end[0]" not in out and "pend.x" not in out, out
        assert "last[0]: long = 4" in out, out
        assert main(["session", "get", "end", "--json"]) == 0
        (row,) = json.loads(capsys.readouterr().out)
        assert "children" not in row and placed(row) == {"selector": "queue", "length": 4}
        assert main(["session", "inspect", "--json"]) == 0
        listed = json.loads(capsys.readouterr().out)["scopes"][0]["variables"]
        shown = {variable["name"]: variable for variable in listed}
        assert shown["end"]["variablesReference"] == 0 and "past_end" in shown["end"], shown
    finally:
        main(["session", "close"])
        capsys.readouterr()


CPP = """#include <array>
#include <cstdio>
#include <string>

struct Ring {
  long buf[4] = {1, 2, 3, 4};
  long *end_ = buf + 4;
  long sum() const {
    long s = 0;
    for (const long *p = buf; p < end_; p++) s += *p;
    return s; /* SUM */
  }
};

struct Holder { long *end; long n; };

int main() {
  std::array<long, 4> arr = {5, 6, 7, 8};
  long *aend = arr.data() + 4;
  std::string names[2] = {"ann", "bob"};
  std::string *nend = names + 2;
  std::string *nlast = names + 1;
  long queue[3] = {1, 2, 3};
  Holder h{queue + 3, 3};
  Ring ring;
  long total = ring.sum() + aend[-1] + h.end[-1];
  std::printf("%ld %s %s\\n", total, nlast->c_str(), nend[-1].c_str()); /* MAIN */
  return 0;
}
"""


@pytest.mark.timeout(240)
def test_cpp_reads_nothing_one_past_an_arrays_end(tmp_path, monkeypatch, cxx, capsys):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "ends.cpp").write_text(CPP)
    target = tmp_path / "ends.cpp"
    selectors = "aend,aend[0],nend,nend[0],nlast[0],h.end,h.end[0]"
    rows = probed(capsys, target, f"ends.cpp:{line_of('MAIN', CPP)}", selectors)
    rows = {row["selector"]: row for row in rows}
    # An array of std::string: never the string LLDB read past it.
    assert placed(rows["nend"]) == {"selector": "names", "length": 2}, rows
    assert '"' not in rows["nend"]["value"], rows
    assert rows["nend[0]"]["error"].startswith(f"nend[0] {reason('nend', 'names')}"), rows
    # Inside the array: read as before (lldb-dap 18 can't read a libstdc++ string's text).
    assert "one past" not in str(rows["nlast[0]"]), rows
    # A field that holds one.
    assert rows["h.end[0]"]["error"].startswith(f"h.end[0] {reason('h.end', 'queue')}"), rows
    if ADDRESSED:
        # A std::array has an address only where lldb-dap gives every value's.
        assert rows["aend[0]"]["error"].startswith(f"aend[0] {reason('aend', 'arr')}"), rows
    # A member array, through `this`.
    rows = probed(capsys, target, f"ends.cpp:{line_of('SUM', CPP)}", "this.end_,this.end_[0]")
    assert rows[1]["error"].startswith(f"this.end_[0] {reason('this.end_', 'this.buf')}"), rows


TRACKS = """#include <cstdio>
#include <string>

struct Item {
  std::string name;
  int uses = 0;
};

int main() {
  Item items[4];
  items[0].name = "a";
  items[3].name = "left";
  Item *end = items + 4;
  Item *cur = items + 4;
  Item *last = items + 3;
  Item *pick = items + 3;
  Item *slots[2] = {items + 3, items};
  Item **pp = slots;
  Item **qq = slots;
  std::printf("%d %d %d\\n", (int)(end - cur), pick->uses + last->uses, pp[0]->uses + qq[1]->uses); /* MAIN */
  return 0;
}
"""


@pytest.mark.timeout(240)
def test_cpp_pointers_at_one_address_say_so_plainly(tmp_path, monkeypatch, cxx, capsys):
    # Twelfth evaluation, playlist: `end … [now + 0 by address; same array not proven; at
    # the address one past queue's end]` doubted one array beside a fact that both are one
    # past `queue`'s end; and `now.title` read `"Coda"` at `end`'s address, with nothing to
    # tie it to `end`.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "items.cpp").write_text(TRACKS)
    target = tmp_path / "items.cpp"
    at = f"items.cpp:{line_of('MAIN', TRACKS)}"
    rows = probed(capsys, target, at, "cur,end,pick,last,pick.uses,last[0].uses,cur.uses")
    rows = {row["selector"]: row for row in rows}
    assert placed(rows["end"]) == {"selector": "items", "length": 4}, rows
    assert rows["end"]["offset_from"] == {"selector": "cur", "offset": 0}, rows
    assert rows["last"]["offset_from"] == {"selector": "cur", "offset": -1}, rows
    assert rows["pick.uses"]["read_through"] == {
        "selector": "pick",
        "same_address_as": ["last"],
    }, rows
    assert rows["last[0].uses"]["read_through"] == {
        "selector": "last",
        "same_address_as": ["pick"],
    }, rows
    assert rows["cur.uses"]["error"].startswith(f"cur.uses {reason('cur', 'items')}"), rows
    assert main(["probe", str(target), "--at", at, "--capture", "cur,end,pick,last,pick.uses"]) == 0
    table = capsys.readouterr().out
    assert "  [same address as `cur`; at the address one past items's end]" in table, table
    assert "  0  [read through `pick`, which holds the same address as `last`]" in table, table
    assert "same array not proven" in table and "+ 0 by address" not in table, table
    # Review of PR #354: the nearest captured pointer is the one read through. `pp[0].uses`
    # reads through `pp[0]`, never at `pp`'s address, which `qq` holds.
    rows = probed(capsys, target, at, "pp,qq,last,slots[0],slots[0].uses,pp[0].uses")
    rows = {row["selector"]: row for row in rows}
    assert rows["slots[0].uses"]["read_through"] == {
        "selector": "slots[0]",
        "same_address_as": ["last"],
    }, rows
    assert "read_through" not in rows["pp[0].uses"], rows
    rows = probed(capsys, target, at, "pp,qq,pp[0],last,pp[0].uses")
    rows = {row["selector"]: row for row in rows}
    assert rows["pp[0].uses"]["read_through"] == {
        "selector": "pp[0]",
        "same_address_as": ["last"],
    }, rows
    assert rows["pp[0]"]["read_through"] == {"selector": "pp", "same_address_as": ["qq"]}, rows
    # `session get` says it of the children it lists under each pointer.
    assert main(["session", "start", str(target), "--at", at]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "get", "pick", "last"]) == 0
        out = capsys.readouterr().out
        assert (
            "  pick.uses: int = 0  [read through `pick`, which holds the same address as `last`]"
            in out
        ), out
        assert "  last.uses: int = 0  [read through `last`, which holds the same address as " in (
            out
        ), out
    finally:
        main(["session", "close"])
        capsys.readouterr()
