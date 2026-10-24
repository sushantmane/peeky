"""The seventh agent evaluation's C++ findings, on real builds: `continue --to` says when
the session's own breakpoint came first (5), and `session get` names the stop a name
did not resolve at (15)."""

import json
import os

import pytest

from peeky.cli import main

# The evaluation's case, as the agent had it.
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
ARGS = ["--", "build:2:30,test:2:10,deploy:1:5,lint:3:50"]


@pytest.fixture
def sched(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "sched.cpp").write_text(SCHED)
    return tmp_path / "sched.cpp"


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr().out


@pytest.mark.timeout(180)
def test_get_names_the_stop_a_selector_did_not_resolve_at(sched, cxx, capsys):
    # Finding 15: `session get a.name` at the wrong stop printed `<cannot expand 'a'
    # (leaf; value 4)>` without naming the stop's function (`main`).
    code, out = run(capsys, "session", "start", sched, "--at", "30", *ARGS)
    assert code == 0 and "hit 1" in out, out
    try:
        code, out = run(capsys, "session", "get", "a.name", "zz")
        assert code == 1, out
        assert out.splitlines()[-1] == "note: this stop is in main at sched.cpp:30", out
        code, out = run(capsys, "session", "get", "a.name", "--json")
        (row,) = json.loads(out)
        assert row["function"] == "main" and row["location"].endswith("sched.cpp:30"), row
        # A name that resolves needs no note.
        code, out = run(capsys, "session", "get", "pos")
        assert code == 0 and "note:" not in out, out
        for _ in range(4):
            # Hits 2 to 4 of line 30 first (finding 5), then the comparator.
            code, out = run(capsys, "session", "continue", "--to", "17")
        code, out = run(capsys, "session", "get", "a.name", "b.nope")
        assert code == 1 and "cannot expand 'a'" not in out, out
        # GCC's name for a static function, from its declaration (#167), shown without
        # the global scope's `::`.
        before = (
            "before(const Task &, const Task &)"
            if cxx == "gcc"
            else "before(Task const&, Task const&)"
        )
        assert f"note: this stop is in {before} at sched.cpp:17" in out, out
    finally:
        run(capsys, "session", "close")


@pytest.mark.timeout(180)
def test_continue_to_says_when_the_sessions_breakpoint_came_first(sched, capsys):
    # Finding 5: `session continue --to 17` stopped first at the session's own
    # breakpoint (line 30, hit 2), with no note that line 17 was not reached.
    code, out = run(capsys, "session", "start", sched, "--at", "30", *ARGS)
    assert code == 0 and "hit 1" in out, out
    try:
        code, out = run(capsys, "session", "continue", "--to", "17")
        assert code == 3, out
        note = (
            "note: stopped at the session's breakpoint (line 30, hit 2) before reaching "
            "line 17; `session continue --to 17` again"
        )
        assert note in out.splitlines(), out
        code, out = run(capsys, "session", "continue", "--to", "17", "--json")
        state = json.loads(out)
        assert code == 3 and state["hit"] == 3, state
        assert "(line 30, hit 3) before reaching line 17" in state["note"], state
        for _ in range(2):
            code, out = run(capsys, "session", "continue", "--to", "17")
        # Past the parsing loop, line 17 comes first: no note. Nor one on the comparator
        # being a call std::sort made: a continue's stop is never compared with the stop
        # it left, whose stack can look like another activation's (review of #274).
        assert code == 0 and "sched.cpp:17" in out and "note:" not in out, out
        # No call reaches line 18 on this input (no two tasks tie on both keys).
        code, out = run(capsys, "session", "continue", "--to", "18", "--json")
        state = json.loads(out)
        assert code == 3 and state["state"] == "terminated", state
        assert state["note"] == "the program exited without reaching line 18", state
    finally:
        run(capsys, "session", "close")
