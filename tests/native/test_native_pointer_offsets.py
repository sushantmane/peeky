"""Which element of a captured array a C pointer holds the address of.

Shown bare, `turn const int * 0x…7040` says nothing that ties it to `level`, and comparing
runs where it holds element 6's address in one and element 4's in the other needs address()
and a hard-coded `sizeof(int)`.
"""

import json
import os

import pytest

from peeky.cli import main

SOURCE = """#include <stdio.h>
#include <stdlib.h>
typedef int reading;

static int peak(const int *level, int n) {
  const int *top = level;
  for (const int *p = level; p < level + n; p++) {
    if (*p >= *top) { /* WALK */
      top = p;
    }
  }
  return (int)(top - level); /* PEAK */
}

int main(int argc, char **argv) {
  reading level[6] = {0};
  int n = 0;
  for (int i = 1; i < argc && n < 6; i++) level[n++] = atoi(argv[i]);
  const int *end = level + 6;
  const int *top = level + peak(level, n);
  const char *bytes = (const char *)top;
  const int *first = level;
  printf("TOP:%d AT:%d\\n", *top, (int)(top - first)); /* MAIN */
  return 0;
}
"""
ARGS = ["--", "1", "5", "5", "2"]
EDIT = ["--old", "*p >= *top", "--new", "*p > *top"]


@pytest.fixture
def program(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "levels"
    root.mkdir()
    (root / "levels.c").write_text(SOURCE)
    return root


def line_of(marker):
    return next(i for i, text in enumerate(SOURCE.splitlines(), 1) if f"/* {marker} */" in text)


def probed(capsys, program, marker, capture, *extra):
    at = f"levels.c:{line_of(marker)}"
    code = main(["probe", str(program), "--at", at, "--capture", capture, "--json", *extra, *ARGS])
    output = capsys.readouterr().out
    assert code == 0, output
    return json.loads(output)


@pytest.mark.timeout(240)
def test_a_pointer_says_which_element_of_a_captured_array_it_holds(program, capsys):
    result = probed(capsys, program, "MAIN", "top,end,bytes,first,level")
    rows = {row["selector"]: row for row in result["rows"]}
    assert rows["top"]["at_element_of"] == {"selector": "level", "index": 2, "length": 6}, rows
    # One past the end: C lets a pointer hold it, never read it.
    assert rows["end"]["at_element_of"] == {"selector": "level", "index": 6, "length": 6}, rows
    assert rows["first"]["at_element_of"] == {"selector": "level", "index": 0, "length": 6}, rows
    # A `char *` into an `int` array points at no whole element of it.
    assert "at_element_of" not in rows["bytes"] and rows["bytes"]["element_size"] == 1, rows
    assert "at_element_of" not in rows["level"], rows
    assert [rows[name]["element_size"] for name in ("top", "end", "first", "level")] == [4] * 4

    # Where the array is a parameter, `level` is a pointer: its length is not known, so
    # no element is named, only how far `top` is from it by address.
    inner = probed(capsys, program, "PEAK", "top,level")
    rows = {row["selector"]: row for row in inner["rows"]}
    assert not any("at_element_of" in row for row in rows.values()), rows
    assert rows["top"]["element_size"] == rows["level"]["element_size"] == 4, rows
    assert rows["top"]["offset_from"] == {"selector": "level", "offset": 2}, rows
    assert "offset_from" not in rows["level"], rows

    at = f"levels.c:{line_of('MAIN')}"
    assert main(["probe", str(program), "--at", at, "--capture", "top,end,level", *ARGS]) == 0
    table = capsys.readouterr().out
    assert "  [at level[2]'s address]" in table, table
    assert "  [at the address one past level's end]" in table, table

    # The store keeps it, for SQL.
    sql = "SELECT selector, element_size, at_element_of, element_index FROM last_run"
    assert main(["sql", sql, "--json"]) == 0
    stored = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    assert stored["top"] == {
        "selector": "top",
        "element_size": 4,
        "at_element_of": "level",
        "element_index": 2,
    }
    assert stored["level"]["at_element_of"] is None and stored["level"]["element_size"] == 4


@pytest.mark.timeout(240)
def test_pointers_walking_a_parameter_say_how_far_they_are_from_it(program, capsys):
    # tides' line 40: `find_waters(const int *level, ...)` walks `p` and `turn` along the
    # readings. `level` is the parameter, so each is counted from it, at every pass.
    result = probed(capsys, program, "WALK", "top,p,level,n", "--hits", "all")
    offsets = [
        (row["selector"], row["offset_from"]["offset"])
        for row in result["rows"]
        if "offset_from" in row
    ]
    assert all(
        row["offset_from"]["selector"] == "level" for row in result["rows"] if "offset_from" in row
    )
    assert offsets == [
        ("top", 0),
        ("p", 0),
        ("top", 0),
        ("p", 1),
        ("top", 1),
        ("p", 2),
        ("top", 2),
        ("p", 3),
    ]
    at = f"levels.c:{line_of('WALK')}"
    command = ["probe", str(program), "--at", at, "--capture", "top,p,level", "--hits", "1"]
    assert main([*command, *ARGS]) == 0
    table = capsys.readouterr().out
    # An equal address is said plainly, never as `level + 0` doubted as one array.
    assert "  [same address as `level`]" in table, table
    assert "+ 0 by address" not in table and "'s address]" not in table, table
    sql = "SELECT selector, offset_from, element_offset, at_element_of FROM last_run"
    assert main(["sql", sql, "--json"]) == 0
    stored = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    assert stored["p"] == {
        "selector": "p",
        "offset_from": "level",
        "element_offset": 0,
        "at_element_of": None,
    }

    # The edit takes the first of equal readings: `top` is at another offset from `level`,
    # shown, but ranked with the addresses that differ, as nothing proves one array.
    for extra in ([], EDIT):
        probed(capsys, program, "PEAK", "top,level", *extra)
    assert main(["queries", "diff", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    notes = {row["selector"]: row["note"] for row in rows}
    assert notes["top"] == (
        "addresses differ: by address, `level + 2` in run a and `level + 1` in run b; nothing "
        "proves they point into one array, so it may or may not be another element"
    ), rows


@pytest.mark.timeout(240)
def test_a_value_read_through_a_pointer_names_the_others_at_its_address(program, capsys):
    # A value read through `now`, which holds `end`'s address, names `end` too. Here `p`,
    # `top` and `level` are equal at the first pass, and apart at the second.
    result = probed(capsys, program, "WALK", "top,p,level,p[0],top[0]", "--hits", "2")
    rows = [row for row in result["rows"] if row["selector"] in ("p[0]", "top[0]")]
    assert [row.get("read_through") for row in rows] == [
        {"selector": "p", "same_address_as": ["top", "level"]},
        {"selector": "top", "same_address_as": ["p", "level"]},
        None,
        {"selector": "top", "same_address_as": ["level"]},
    ], result["rows"]
    assert [row["value"] for row in rows] == ["1", "1", "5", "1"], rows
    at = f"levels.c:{line_of('WALK')}"
    command = ["probe", str(program), "--at", at, "--capture", "top,p,level,p[0]", "--hits", "1"]
    assert main([*command, *ARGS]) == 0
    table = capsys.readouterr().out
    assert "  1  [read through `p`, which holds the same address as `top` and `level`]" in table, (
        table
    )


@pytest.mark.timeout(240)
def test_diff_compares_the_elements_a_pointer_holds(program, capsys):
    # The first 5 against the last: `top` holds element 2's address, then element 1's.
    for extra in ([], EDIT):
        probed(capsys, program, "MAIN", "top,first,level", *extra)
    assert main(["queries", "diff", "--param", "a=1", "--param", "b=2", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    notes = {row["selector"]: row["note"] for row in rows}
    assert notes["top"] == (
        "the address of `level[2]` in run a, of `level[1]` in run b; by address alone: the "
        "pointer or the array may have moved"
    ), rows
    # First among the addresses that differ, ranked after any real difference (none here).
    assert [row["selector"] for row in rows][:3] == ["(output)", "(edit)", "top"], rows
    # `first` holds element 0's address in both: where the stack moved, addresses only.
    same = "addresses only: the address of the same element of `level` in both runs"
    assert notes.get("first", same) == (same), rows
    assert "level" not in notes, rows


@pytest.mark.timeout(240)
def test_session_get_says_which_element_a_pointer_holds(program, capsys):
    at = f"levels.c:{line_of('MAIN')}"
    assert main(["session", "start", str(program), "--at", at, *ARGS]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "get", "top", "level"]) == 0
        out = capsys.readouterr().out
        assert "top: const int * = 0x" in out and "  [at level[2]'s address]" in out, out
        # Read alone, nothing at the same stop proves which array it points into.
        assert main(["session", "get", "top"]) == 0
        assert "'s address" not in capsys.readouterr().out
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(240)
def test_session_inspect_says_which_element_a_pointer_holds(program, capsys):
    # `session inspect` names the element, as `session get end level` does.
    at = f"levels.c:{line_of('MAIN')}"
    assert main(["session", "start", str(program), "--at", at, *ARGS]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "inspect"]) == 0
        out = capsys.readouterr().out
        assert "  top: const int * = 0x" in out and "  [at level[2]'s address]" in out, out
        assert "  [at the address one past level's end]" in out, out
        assert main(["session", "inspect", "--json"]) == 0
        listed = {
            variable["name"]: variable
            for scope in json.loads(capsys.readouterr().out)["scopes"]
            for variable in scope["variables"]
        }
        assert listed["first"]["at_element_of"] == {"selector": "level", "index": 0, "length": 6}
        assert not any("extent" in variable for variable in listed.values()), listed
    finally:
        main(["session", "close"])
        capsys.readouterr()


ROSTER = """#include <cstdio>
#include <string>
struct Student {
  std::string name;
  int grade;
};
int main() {
  Student roster[3] = {{"Maya", 82}, {"Leon", 85}, {"Ines", 89}};
  std::string names[3] = {"a", "b", "c"};
  const Student *best = roster;
  for (const Student &s : roster) {
    if (s.grade > best->grade) best = &s;
  }
  std::string *last = names + 2;
  const Student &first = roster[0];
  std::printf("BEST:%s %s %d\\n", best->name.c_str(), last->c_str(), first.grade); /* BEST */
  return 0;
}
"""


@pytest.mark.timeout(240)
def test_cpp_raw_pointers_say_which_element_they_hold(program, cxx, capsys):
    # The same in C++, for a struct's array and a std::string's; a reference never says.
    (program / "roster.cpp").write_text(ROSTER)
    at = "roster.cpp:" + str(next(i for i, t in enumerate(ROSTER.splitlines(), 1) if "BEST" in t))
    code = main(
        [
            "probe",
            str(program / "roster.cpp"),
            "--at",
            at,
            "--capture",
            "best,last,first,roster,names",
            "--json",
        ]
    )
    output = capsys.readouterr().out
    assert code == 0, output
    rows = {row["selector"]: row for row in json.loads(output)["rows"]}
    assert rows["best"]["at_element_of"] == {"selector": "roster", "index": 2, "length": 3}, rows
    assert rows["last"]["at_element_of"] == {"selector": "names", "index": 2, "length": 3}, rows
    assert "at_element_of" not in rows["first"], rows


SHARED = """#include <stdio.h>
union both {
  int a[4];
  int b[4];
};
int main(void) {
  union both u = {{1, 2, 3, 4}};
  int *p = &u.a[2];
  printf("%d\\n", *p); /* SHARED */
  return 0;
}
"""


@pytest.mark.timeout(240)
def test_an_address_two_captured_arrays_share_names_neither(program, capsys):
    # `u.a[2]` and `u.b[2]` have one address, which cannot tell which the pointer
    # designates, so neither is named.
    (program / "shared.c").write_text(SHARED)
    at = "shared.c:" + str(next(i for i, t in enumerate(SHARED.splitlines(), 1) if "SHARED" in t))

    def rows(capture):
        code = main(
            ["probe", str(program / "shared.c"), "--at", at, "--capture", capture, "--json"]
        )
        output = capsys.readouterr().out
        assert code == 0, output
        return {row["selector"]: row for row in json.loads(output)["rows"]}

    both = rows("p,u.a,u.b")
    assert "at_element_of" not in both["p"], both
    one = rows("p,u.b")
    assert one["p"]["at_element_of"] == {"selector": "u.b", "index": 2, "length": 4}, one
