"""Which element of a captured array a C pointer holds the address of (#302).

Eleventh evaluation (tides): `turn const int * 0x…7040` showed nothing that tied it to
`level`, and `queries diff` sent the agent to address() and a hard-coded `sizeof(int)`
for a pointer that held element 6's address in one run and element 4's in the other.
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
    if (*p >= *top) top = p;
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
    assert rows["top"]["points_into"] == {"selector": "level", "index": 2, "length": 6}, rows
    # One past the end: C lets a pointer hold it, never read it.
    assert rows["end"]["points_into"] == {"selector": "level", "index": 6, "length": 6}, rows
    assert rows["first"]["points_into"] == {"selector": "level", "index": 0, "length": 6}, rows
    # A `char *` into an `int` array points at no whole element of it.
    assert "points_into" not in rows["bytes"] and rows["bytes"]["element_size"] == 1, rows
    assert "points_into" not in rows["level"], rows
    assert [rows[name]["element_size"] for name in ("top", "end", "first", "level")] == [4] * 4

    # Where the array is a parameter, `level` is a pointer: its length is not known.
    inner = probed(capsys, program, "PEAK", "top,level")
    rows = {row["selector"]: row for row in inner["rows"]}
    assert not any("points_into" in row for row in rows.values()), rows
    assert rows["top"]["element_size"] == rows["level"]["element_size"] == 4, rows

    at = f"levels.c:{line_of('MAIN')}"
    assert main(["probe", str(program), "--at", at, "--capture", "top,end,level", *ARGS]) == 0
    table = capsys.readouterr().out
    assert "  [points at level[2]]" in table, table
    assert "  [level + 6: one past its end]" in table, table

    # The store keeps it, for SQL.
    sql = "SELECT selector, element_size, points_into, element_index FROM last_run"
    assert main(["sql", sql, "--json"]) == 0
    stored = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    assert stored["top"] == {
        "selector": "top",
        "element_size": 4,
        "points_into": "level",
        "element_index": 2,
    }
    assert stored["level"]["points_into"] is None and stored["level"]["element_size"] == 4


@pytest.mark.timeout(240)
def test_diff_compares_the_elements_a_pointer_holds(program, capsys):
    # The first 5 against the last: `top` holds element 2's address, then element 1's.
    for extra in ([], EDIT):
        probed(capsys, program, "MAIN", "top,first,level", *extra)
    assert main(["queries", "diff", "--param", "a=1", "--param", "b=2", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    notes = {row["selector"]: row["note"] for row in rows}
    assert notes["top"] == "another element of `level`: index 2 in run a, 1 in run b", rows
    # A real difference: right after the run-level rows, before any address-only row.
    assert [row["selector"] for row in rows][:3] == ["(output)", "(edit)", "top"], rows
    # `first` holds element 0's address in both: where the stack moved, addresses only.
    assert notes.get("first", "addresses only: the same element of `level` in both runs") == (
        "addresses only: the same element of `level` in both runs"
    ), rows
    assert "level" not in notes, rows


@pytest.mark.timeout(240)
def test_session_get_says_which_element_a_pointer_holds(program, capsys):
    at = f"levels.c:{line_of('MAIN')}"
    assert main(["session", "start", str(program), "--at", at, *ARGS]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "get", "top", "level"]) == 0
        out = capsys.readouterr().out
        assert "top: const int * = 0x" in out and "  [points at level[2]]" in out, out
        # Read alone, nothing at the same stop proves which array it points into.
        assert main(["session", "get", "top"]) == 0
        assert "points at" not in capsys.readouterr().out
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
    assert rows["best"]["points_into"] == {"selector": "roster", "index": 2, "length": 3}, rows
    assert rows["last"]["points_into"] == {"selector": "names", "index": 2, "length": 3}, rows
    assert "points_into" not in rows["first"], rows
