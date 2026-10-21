"""#158: a function declaring one name twice with different types (in sibling blocks,
or an inner block shadowing an outer one) is changed through the declaration the
debugger shows at the stop, the one whose block holds it, as reads judge it; refused
only where that cannot be singled out."""

import json
import os

import pytest

from peeky.cli import main

# The seventh evaluation's `sched` (C++): the loop's `Task t`, and a later block's
# `const Task &t` (a range-`for`).
SCHED = """#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

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
        tasks.push_back(t);  // PUSH
        pos = comma + 1;
    }
    std::sort(tasks.begin(), tasks.end(), before);
    std::string out;
    for (const Task &t : tasks) out += (out.empty() ? "" : ",") + t.name;
    std::printf("ORDER:%s\\n", out.c_str());
    return 0;
}
"""

SAME = """#include <stdio.h>

struct A { long x; long y; };
struct B { int x; long z; };

__attribute__((noinline)) long use(long v) { return v; }

int main(void) {
    long total = 0;
    {
        struct A s = { 1, 2 };
        total += use(s.x);  // S1
        printf("S1 %ld\\n", s.x);
    }
    {
        struct B s = { 3, 4 };
        total += use(s.x);  // S2
        printf("S2 %d\\n", s.x);
    }
    struct A t = { 5, 6 };  // DECLARED
    {
        struct B t = { 7, 8 };
        total += use(t.x);  // INNER
        printf("INNER %d\\n", t.x);
    }
    total += use(t.x);  // OUTER
    printf("OUTER %ld\\n", t.x);
    return 0;
}
"""

# Two blocks on one line declaring `s` with different types: the build's debug
# information tells them apart by their line only.
AMBIGUOUS = """#include <stdio.h>

struct A { long x; long y; };
struct B { int x; long z; };

__attribute__((noinline)) long use(long v) { return v + 1; }

int main(void) {
    long total = 0;
    { struct A s = { 1, 2 }; total += use(s.x); } { struct B s = { 3, 4 }; total += use(s.x); }  // SAME
    printf("%ld\\n", total);
    return 0;
}
"""


def cpp(source):
    return source.replace("<stdio.h>", "<cstdio>").replace("(void)", "()")


@pytest.fixture
def directory(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def line_of(source, marker):
    return next(n for n, text in enumerate(source.splitlines(), 1) if marker in text)


def probed(capsys, path, line, assignment, capture, *args):
    code = main(
        ["probe", str(path), "--at", str(line), "--set", assignment, "--capture", capture]
        + ["--json", "--quiet", "--no-save", *(arg for value in args for arg in ("--arg", value))]
    )
    return code, json.loads(capsys.readouterr().out)


def run(capsys, *argv):
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(300)
def test_cpp_the_loops_task_is_changed_and_the_program_sees_it(directory, capsys):
    # Refused before the run with "several locals named `t` of different types": at
    # line 32 only the loop's `Task t` is in scope.
    path = directory / "sched.cpp"
    path.write_text(SCHED)
    line = line_of(SCHED, "// PUSH")
    tasks = "build:2:30,test:2:10,deploy:1:5,lint:3:50"
    code, result = probed(capsys, path, line, "t.deadline=-30", "t.deadline", tasks)
    assert code == 0 and result["status"] == "completed", result.get("error")
    [change] = result["interventions"]
    assert (change["before"], change["after"]) == ("30", "-30"), change
    assert "ORDER:lint,test,build,deploy" in result["output"], result["output"]
    # In a session too, where `t` is settable.
    assert main(["session", "start", str(path), "--at", str(line), "--arg", tasks]) == 0
    capsys.readouterr()
    try:
        code, rows = run(capsys, "session", "get", "t.deadline")
        assert rows[0]["value"] == "30" and rows[0]["settable"] is True, rows
        code, result = run(capsys, "session", "set", "t.deadline=-30")
        assert code == 0 and result["after"] == "-30", result
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(300)
@pytest.mark.parametrize("suffix", ["c", "cpp"])
def test_each_block_changes_its_own_local_of_a_shared_name(directory, capsys, suffix):
    source = SAME if suffix == "c" else cpp(SAME)
    path = directory / f"same.{suffix}"
    path.write_text(source)
    # Before the run: each line's block holds one `s`, so the probe runs.
    code, result = probed(capsys, path, line_of(source, "// S2"), "s.x=41", "s.x")
    assert code == 0 and result["status"] == "completed", result.get("error")
    assert "S2 41" in result["output"], result["output"]
    outer = line_of(source, "// DECLARED")
    assert main(["session", "start", str(path), "--at", str(line_of(source, "// S1"))]) == 0
    capsys.readouterr()
    try:
        # Sibling blocks: each `s` in its own.
        assert run(capsys, "session", "set", "s.x=40")[1]["after"] == "40"
        assert main(["session", "continue", "--to", str(line_of(source, "// S2"))]) == 0
        capsys.readouterr()
        assert run(capsys, "session", "set", "s.x=41")[1]["after"] == "41"
        # Nested: the inner `t` is what the code names, so `t` reads and changes it.
        assert main(["session", "continue", "--to", str(line_of(source, "// INNER"))]) == 0
        capsys.readouterr()
        code, rows = run(capsys, "session", "get", "t", f"t @ same.{suffix}:{outer}.x")
        assert code == 0 and rows[0]["type"] == "B" and rows[1]["value"] == "5", rows
        assert run(capsys, "session", "set", "t.x=42")[1]["after"] == "42"
        code, rows = run(capsys, "session", "get", f"t @ same.{suffix}:{outer}.x")
        assert rows[0]["value"] == "5", rows
        # Past the inner block, the outer one.
        assert main(["session", "continue", "--to", str(line_of(source, "// OUTER"))]) == 0
        capsys.readouterr()
        assert run(capsys, "session", "set", "t.x=43")[1]["after"] == "43"
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        printed = json.loads(capsys.readouterr().out)["output"]
        for text in ("S1 40", "S2 41", "INNER 42", "OUTER 43"):
            assert text in printed, printed
    finally:
        main(["session", "close"])
        capsys.readouterr()
    # `describe` says how a change picks among them.
    assert main(["describe", str(path)]) == 0
    described = capsys.readouterr().out
    assert "Locals of one name with different types (main: s, t)" in described, described


@pytest.mark.timeout(300)
@pytest.mark.parametrize("suffix", ["c", "cpp"])
def test_locals_of_one_name_the_debug_information_cannot_tell_apart_are_refused(
    directory, capsys, suffix
):
    source = AMBIGUOUS if suffix == "c" else cpp(AMBIGUOUS)
    path = directory / f"amb.{suffix}"
    path.write_text(source)
    line = line_of(source, "// SAME")
    code, result = probed(capsys, path, line, "s.x=40", "s")
    error = result["error"]
    assert code == 1 and error["code"] == "MUTATION_REJECTED", result
    assert result.get("refused_before_launch") is True, result
    assert f"several locals named `s` of different types on line {line}" in error["message"]
