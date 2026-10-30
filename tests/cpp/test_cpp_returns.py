"""What C++ functions return, on real builds: `before()`, a comparator `std::sort` calls,
is the proof of the bug in `sched`. `step --out` of it stops in std::sort's own code,
outside the program's sources, and shows what it returned; probes capture each call's
return."""

import json
import os

import pytest

from peeky.cli import main
from peeky.probe import RETURN

# What LLDB names `before`: GCC gives a static function no linkage name, so LLDB names it
# from its declaration, from the global scope.
BEFORE = {
    "clang": "before(Task const&, Task const&)",
    "gcc": "::before(const Task &, const Task &)",
}

# sched.cpp: a later deadline runs first.
SCHED = """#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

// sched "name:prio:deadline,...": runs tasks by priority (higher first); among equal
// priorities, the earlier deadline first; then by name. Prints the run order.
struct Task {
    std::string name;
    int prio;
    int deadline;
};

static bool before(const Task &a, const Task &b) {
    if (a.prio != b.prio) return a.prio > b.prio;
    if (a.deadline != b.deadline) return a.deadline > b.deadline;
    return a.name < b.name;
}

int main(int argc, char **argv) {
    std::string input = argc > 1 ? argv[1] : "";
    std::vector<Task> tasks;
    size_t pos = 0;
    while (pos < input.size()) {
        size_t comma = input.find(',', pos);
        if (comma == std::string::npos) comma = input.size();
        std::string item = input.substr(pos, comma - pos);
        size_t a = item.find(':'), b = item.rfind(':');
        Task t{item.substr(0, a), std::atoi(item.substr(a + 1, b - a - 1).c_str()),
               std::atoi(item.substr(b + 1).c_str())};
        tasks.push_back(t);
        pos = comma + 1;
    }
    std::sort(tasks.begin(), tasks.end(), before);
    std::string out;
    for (const Task &t : tasks) out += (out.empty() ? "" : ",") + t.name;
    std::printf("ORDER:%s\\n", out.c_str());
    return 0;
}
"""
INPUT = "build:2:30,test:2:10,deploy:1:5,lint:3:50"
FIELDS = ("name", "prio", "deadline")

RECORDS = """#include <cstdio>
#include <string>
struct Task { std::string name; int prio; };
static Task make(int prio) {
    return Task{"t", prio}; // MAKE
}
static std::string label(const Task &t) {
    return t.name + "!"; // LABEL
}
static const Task &same(const Task &t) {
    return t; // SAME
}
static long weight(const Task &t) {
    return 10L * t.prio; // WEIGHT
}
int main() {
    Task t = make(3);
    std::string l = label(t);
    long w = weight(same(t));
    std::printf("%s %ld\\n", l.c_str(), w);
    return 0;
}
"""


@pytest.fixture
def cpp(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))

    def write(name, text):
        (tmp_path / name).write_text(text)
        return tmp_path / name

    return write


def line_of(text, marker):
    return next(i for i, line in enumerate(text.splitlines(), 1) if marker in line)


def written(a, b):
    """The comparator as the program has it (the bug: `>` on deadlines); None where it
    compares names this LLDB cannot read (lldb-dap 18 and libstdc++'s std::string)."""
    if a["prio"] != b["prio"]:
        return a["prio"] > b["prio"]
    if a["deadline"] != b["deadline"]:
        return a["deadline"] > b["deadline"]
    return None if None in (a["name"], b["name"]) else a["name"] < b["name"]


def task(rows, side):
    def value(field):
        text = rows[f"{side}.{field}"].get("value")
        if field != "name":
            return int(text)
        # A string's row may carry an address prefix (libc++ summaries): keep the text.
        return text[text.index('"') :].strip('"') if text and '"' in text else None

    return {field: value(field) for field in FIELDS}


@pytest.mark.timeout(180)
def test_every_comparator_call_returns_what_it_computed(cpp, cxx, capsys):
    source = cpp("sched.cpp", SCHED)
    capture = ",".join(f"{side}.{field}" for side in "ab" for field in FIELDS) + ",return"
    code = main(
        ["probe", str(source), "--at", str(line_of(SCHED, "if (a.prio != b.prio)"))]
        + ["--capture", capture, "--hits", "all", "--json", "--no-save", "--", INPUT]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    hits = {}
    for row in result["rows"]:
        hits.setdefault(row["hit"], {})[row["selector"]] = row
    assert len(hits) == result["hits"] >= 4
    for rows in hits.values():
        returned = rows[RETURN]
        expected = written(task(rows, "a"), task(rows, "b"))
        # One row per call: what it returned, into std::sort's code (sort.h or libstdc++).
        assert returned["type"] == "bool" and returned["value"] in ("true", "false"), rows
        assert expected is None or returned["value"] == str(expected).lower(), rows
        assert returned["function"] == BEFORE[cxx] and returned["phase"] == "returned", rows
        assert "sched.cpp" not in returned["location"]
    # The proof of the bug: test (2:10) is not "before" build (2:30), deadline 10 first.
    assert any(
        (task(rows, "a")["prio"], task(rows, "a")["deadline"]) == (2, 10)
        and (task(rows, "b")["prio"], task(rows, "b")["deadline"]) == (2, 30)
        and rows[RETURN]["value"] == "false"
        for rows in hits.values()
    )


@pytest.mark.timeout(180)
def test_step_out_into_the_library_shows_what_before_returned(cpp, cxx, capsys):
    source = cpp("sched.cpp", SCHED)
    line = line_of(SCHED, "if (a.prio != b.prio)")
    assert main(["session", "start", str(source), "--at", str(line), "--", INPUT]) == 0
    try:
        capsys.readouterr()
        main(["session", "get", *(f"{s}.{f}" for s in "ab" for f in FIELDS), "--json"])
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
        # The first call's tasks differ in priority or deadline (no name compared).
        expected = str(written(task(rows, "a"), task(rows, "b"))).lower()
        assert expected in ("true", "false")
        assert main(["session", "step", "--out", "--json"]) == 0
        state = json.loads(capsys.readouterr().out)
        # One frame up, in std::sort's own code: outside the program's sources.
        assert state["outside_program"] is True and state["reason"] == "step", state
        assert main(["session", "inspect", "--json"]) == 0
        snapshot = json.loads(capsys.readouterr().out)
        assert snapshot["returned"] == {
            "function": BEFORE[cxx],
            "type": "bool",
            "value": expected,
        }
        assert (
            snapshot["scopes"] == []
            and "library's locals are not read" in (snapshot["library_frame"])
        )
        main(["session", "inspect"])
        text = capsys.readouterr().out
        # Shown without GCC's leading `::`, which the stored name keeps.
        assert f"{BEFORE[cxx].removeprefix('::')} returned: bool = {expected}" in text, text
        assert main(["session", "get", "return", "a"]) == 1
        text = capsys.readouterr().out
        assert f"(return): bool = {expected}" in text
        assert "a: <The step stopped outside the program's sources" in text
        # A caller outside the sources too (std::sort's own) is named, and so is where
        # the step stopped.
        assert main(["session", "inspect", "--frame", "1"]) == 2
        err = capsys.readouterr().err
        assert "The step stopped outside the program's sources (in " in err, err
        assert "), and frame 1 (" in err and ") is outside them too; " in err, err
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(180)
def test_struct_string_and_reference_returns_are_withheld(cpp, capsys):
    source = cpp("records.cpp", RECORDS)
    found = {}
    for marker in ("MAKE", "LABEL", "SAME", "WEIGHT"):
        code = main(
            ["probe", str(source), "--at", str(line_of(RECORDS, marker))]
            + ["--capture", "return", "--json", "--no-save"]
        )
        result = json.loads(capsys.readouterr().out)
        assert code == 0, result.get("error")
        found[marker] = result["rows"][0]
    for marker in ("MAKE", "LABEL", "SAME"):
        # LLDB may read a class from the wrong registers or memory (or not at all,
        # returned through memory the caller passed): never shown.
        assert "value" not in found[marker], found[marker]
        assert any(
            reason in found[marker]["error"]
            for reason in ("is not a number, character, boolean or pointer", "reported no value")
        ), found[marker]
    assert (found["WEIGHT"]["value"], found["WEIGHT"]["type"]) == ("30", "long")


# f(0) throws and its caller catches it on the same line; the loop's next call, g(1),
# returns to the same place in the same frame. LLDB's step out of f(0) ends there, and
# g(1)'s 1000 is not f(0)'s return.
THROWS = """#include <cstdio>
#include <stdexcept>
static int f(int n) {
    int r = n * 10; // F
    if (n == 0) throw std::runtime_error("boom"); // THROW
    return r;
}
static int g(int n) {
    int r = n * 1000;
    return r;
}
int main() {
    int (*table[])(int) = {f, g, f, g};
    long s = 0;
    for (int i = 0; i < 4; i++) {
        try { s += table[i](i); } catch (...) { s -= 1; } // CALL
    }
    std::printf("%ld\\n", s);
    return 0;
}
"""


@pytest.mark.timeout(180)
def test_a_call_that_threw_is_never_credited_with_a_later_calls_value(cpp, capsys):
    source = cpp("throws.cpp", THROWS)
    code = main(
        ["probe", str(source), "--at", str(line_of(THROWS, "// F")), "--capture", "n,return"]
        + ["--hits", "all", "--json", "--no-save"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    hits = {}
    for row in result["rows"]:
        hits.setdefault(row["hit"], {})[row["selector"]] = row
    assert [rows["n"]["value"] for rows in hits.values()] == ["0", "2"]
    threw = hits[1][RETURN]
    assert "value" not in threw and "never returned" in threw["error"], threw
    assert hits[2][RETURN]["value"] == "20"


@pytest.mark.timeout(180)
def test_a_session_step_out_of_a_throw_shows_no_value(cpp, capsys):
    source = cpp("throws.cpp", THROWS)
    assert main(["session", "start", str(source), "--at", str(line_of(THROWS, "THROW"))]) == 0
    try:
        assert main(["session", "step", "--out"]) == 0
        capsys.readouterr()
        main(["session", "inspect", "--json"])
        snapshot = json.loads(capsys.readouterr().out)
        returned = snapshot["returned"]
        assert returned["unavailable"] and "never returned" in returned["reason"], returned
        assert "value" not in returned
        # Where its caller went on: about to call g(1), from the same line.
        assert snapshot["frames"][0]["line"] == line_of(THROWS, "CALL")
        main(["session", "get", "return", "--json"])
        assert "1000" not in capsys.readouterr().out
    finally:
        main(["session", "close"])
        capsys.readouterr()
