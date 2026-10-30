"""C++ session states on real builds. Where an exception ends a call, the stacks never prove
a return: a step out that an exception cut short says only which function it left, and a
step the session's breakpoint stopped first, in a later call of the same function from the
same place, says nothing of calls or scopes. `session get` names the stop a selector did not
resolve at, `continue --to` says when the session's breakpoint came first, a `void`
function's step out returns no value, and a step onto the breakpoint's line is not a hit."""

import json
import os

import pytest

from peeky.cli import main

SOURCE = """#include <cstdio>
#include <stdexcept>

static int g(int n) {
    if (n == 0) throw std::runtime_error("boom"); /* THROW */
    return n;
}

static int f(int n) {
    int r = n * 10; /* START */
    {
        int doubled = r * 2;
        r = doubled + g(n); /* CALLS_G */
    }
    return r;
}

int main() {
    int total = 0;
    for (int i = 0; i < 3; i++) {
        try {
            total += f(i); /* CALLS_F */
        } catch (const std::exception &e) {
            total += 1;
        }
    }
    std::printf("TOTAL:%d\\n", total);
    return 0;
}
"""


@pytest.fixture
def program(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "throws.cpp"
    path.write_text(SOURCE)
    return path


def line_of(marker):
    return next(i for i, text in enumerate(SOURCE.splitlines(), 1) if f"/* {marker} */" in text)


def state_of(capsys, *argv):
    assert main([*argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.timeout(180)
def test_a_step_out_an_exception_cut_short_never_says_returned(program, cxx, capsys):
    # `step --out` of `g(0)`, which throws, stops in `f(1)`'s call of `g` from the same
    # place (the return watch), and the state doesn't say "returned from".
    assert main(["session", "start", str(program), "--at", str(line_of("THROW"))]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "step", "--out"]) == 0
        out = capsys.readouterr().out
        lines = out.splitlines()
        # GCC's names for static functions, from their declarations, read as
        # clang's (`f(int)`, not `::f(int)`).
        assert lines[0].startswith("state: stopped at ") and " in f(int) " in lines[0], out
        assert lines[1] == "note: left `g(int)`; now in `f(int)`", out
        assert "it never returned" in out and "returned from" not in out, out
    finally:
        main(["session", "close"])


@pytest.mark.timeout(180)
def test_a_step_an_exception_ended_says_nothing_of_calls_or_scopes(program, capsys):
    # `g(0)` throws through `f(0)`: LLDB's step never ends there, and the session's
    # breakpoint stops `f(1)`, called from the same place, before its block. The stacks
    # look alike, but nothing proves they are the same call: no note, and no claim about
    # `doubled`.
    assert main(["session", "start", str(program), "--at", str(line_of("START"))]) == 0
    capsys.readouterr()
    try:
        for _ in range(3):
            state = state_of(capsys, "session", "step")
            if state["location"].endswith(f":{line_of('CALLS_G')}"):
                break
        assert state["location"].endswith(f":{line_of('CALLS_G')}"), state
        assert main(["session", "get", "doubled"]) == 0
        capsys.readouterr()
        state = state_of(capsys, "session", "step")
        assert state["location"].endswith(f":{line_of('START')}") and state["hit"] == 2, state
        assert state["reason"] == "breakpoint" and "call_change" not in state, state
        assert main(["session", "get", "doubled", "--json"]) == 1
        (row,) = json.loads(capsys.readouterr().out)
        assert row["error"].startswith("not found; visible: "), row
    finally:
        main(["session", "close"])


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
    # `session get a.name` at the wrong stop names the stop's function (`main`), not only
    # `<cannot expand 'a' (leaf; value 4)>`.
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
            # Hits 2 to 4 of line 30 first, then the comparator.
            code, out = run(capsys, "session", "continue", "--to", "17")
        code, out = run(capsys, "session", "get", "a.name", "b.nope")
        assert code == 1 and "cannot expand 'a'" not in out, out
        # GCC's name for a static function, from its declaration, shown without
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
    # `session continue --to 17` stops first at the session's own breakpoint (line 30,
    # hit 2), and notes that line 17 was not reached.
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
        # it left, whose stack can look like another activation's.
        assert code == 0 and "sched.cpp:17" in out and "note:" not in out, out
        # No call reaches line 18 on this input (no two tasks tie on both keys).
        code, out = run(capsys, "session", "continue", "--to", "18", "--json")
        state = json.loads(out)
        assert code == 3 and state["state"] == "terminated", state
        assert state["note"] == "the program exited without reaching line 18", state
    finally:
        run(capsys, "session", "close")


STATS = """#include <cstdio>

struct Stats {
    int sum = 0;
    void reset() {
        sum = 0; /* RESET */
    }
    int add(int q) {
        sum += q;
        return sum;
    }
};

int main() {
    Stats stats;
    int total = 0;
    for (int i = 0; i < 3; i++) {
        stats.reset(); /* LOOP */
        total += stats.add(i);
    }
    std::printf("TOTAL:%d\\n", total);
    return 0;
}
"""


@pytest.fixture
def stats(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    case = tmp_path / "case"
    case.mkdir()
    (case / "stats.cpp").write_text(STATS)
    monkeypatch.chdir(case)
    return case


def stats_line(marker):
    return next(i for i, text in enumerate(STATS.splitlines(), 1) if f"/* {marker} */" in text)


def text_of(capsys, *argv, code=0):
    assert main(list(argv)) == code
    return capsys.readouterr().out


@pytest.mark.timeout(180)
def test_a_void_step_out_paths_and_the_report(stats, cxx, capsys, monkeypatch):
    loop, reset = stats_line("LOOP"), stats_line("RESET")
    out = text_of(capsys, "session", "start", "stats.cpp", "--at", str(loop))
    assert f"state: stopped at stats.cpp:{loop} in main, hit 1 " in out, out
    try:
        # From another directory, paths are named as the start named them.
        monkeypatch.chdir(stats.parent)
        out = text_of(capsys, "session", "step", "--into")
        assert out.startswith(f"state: stopped at stats.cpp:{reset} in "), out
        out = text_of(capsys, "session", "step", "--out")
        # Where the call returns: on its line (clang), or the next (GCC's return address).
        where = out.split()[3]
        assert where in (f"stats.cpp:{loop}", f"stats.cpp:{loop + 1}"), out
        assert out.startswith(f"state: stopped at {where} in main "), out
        assert "(return): void (no value: the function returns nothing)" in out, out
        state = json.loads(text_of(capsys, "session", "inspect", "--json"))
        assert state["returned"]["type"] == "void" and state["returned"]["unavailable"], state
        out = text_of(capsys, "session", "report")
        assert out.startswith(f"state: stopped at {where} in main\n"), out
        assert "\nso far: 1 hit (stops at the session's breakpoint), 2 steps\n" in out, out
    finally:
        main(["session", "close"])


@pytest.mark.timeout(180)
def test_a_step_onto_the_breakpoint_line_leaves_its_pass_to_the_next_hit(stats, capsys):
    # A step's stop is never numbered. Xcode's LLDB reports it as the step's, and the next
    # `continue` stops there again first, as hit 2 of the same pass (i == 1); an LLDB that
    # reports it as the breakpoint's numbers it hit 2 then.
    loop = stats_line("LOOP")
    text_of(capsys, "session", "start", "stats.cpp", "--at", str(loop))
    try:
        for _ in range(6):
            state = json.loads(text_of(capsys, "session", "step", "--json"))
            if state["location"].endswith(f":{loop}"):
                break
        assert state["location"].endswith(f":{loop}"), state
        if state["reason"] == "step":
            assert "hit" not in state, state
            assert state["note"].startswith("not a hit: LLDB reports this stop as the step's"), (
                state
            )
            state = json.loads(text_of(capsys, "session", "continue", "--json"))
        assert state["location"].endswith(f":{loop}") and state["hit"] == 2, state
        (row,) = json.loads(text_of(capsys, "session", "get", "i", "--json"))
        assert row["value"] == "1", row
    finally:
        main(["session", "close"])
