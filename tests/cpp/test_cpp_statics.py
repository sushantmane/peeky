"""C++ locals in static storage, built by clang and by g++: a `static` local, and a
`const` aggregate GCC builds at compile time in read-only data (`const Span cs{8, 9};`).
lldb-dap lists none of them among a frame's locals. Peeky reads
the one the code at the stop names, once C++ has run its initializer (its guard variable
says so), and never changes one."""

import json

import pytest

from peeky.cli import main

STATICS = """#include <cstdio>

struct Span { long start, end; };
struct Widget {
    long n;
    explicit Widget(long v) : n(v) {}
};

__attribute__((noinline)) long make(long v) { return v * 3; }

struct Acc {
    int calls = 7;
    int run() {
        static int calls = 100;
        calls += 1;
        return calls;  // RUN
    }
};

__attribute__((noinline)) long tick(long x) {
    static long calls = 0;
    static Widget w(
        make(x));  // INIT
    const Span cs{8, 9};
    calls += x;
    std::printf("%ld %ld %ld\\n", calls, w.n, cs.end);  // HERE
    return calls;
}

int main() {
    Acc acc;
    tick(1);
    tick(2);
    return acc.run() > 0 ? 0 : 1;
}
"""


def line_of(source, marker):
    return next(n for n, text in enumerate(source.splitlines(), 1) if marker in text)


def rows_at(capsys, path, line, selectors, hit=1):
    command = ["probe", str(path), "--at", str(line), "--capture", ",".join(selectors)]
    code = main([*command, "--hits", str(hit), "--json", "--quiet", "--no-save"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result
    return {row["selector"]: row for row in result["rows"] if row["hit"] == hit}


@pytest.fixture
def directory(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


@pytest.mark.timeout(300)
def test_a_static_local_shows_once_its_initializer_has_run(directory, cxx, capsys):
    # `w` and `calls` are read, not `not found`.
    path = directory / "statics.cpp"
    path.write_text(STATICS)
    declared = line_of(STATICS, "static Widget w(")
    # On its first pass the initializer is still running: its guard says it hasn't run.
    rows = rows_at(capsys, path, line_of(STATICS, "// INIT"), ["w", "w.n", "calls"])
    assert rows["w"]["error"] == (
        "no value yet: C++ runs its initializer the first time control passes its "
        f"declaration, on line {declared}, and its guard variable says that hasn't run yet; "
        "read it after that"
    ), rows["w"]
    assert "'w' has no value here" in rows["w.n"]["error"], rows["w.n"]
    assert rows["calls"]["value"] == "0", rows
    rows = rows_at(capsys, path, line_of(STATICS, "// HERE"), ["w", "w.n", "calls", "cs"], hit=2)
    assert rows["w"]["value"] == "{n = 3}" and rows["w.n"]["value"] == "3", rows
    assert rows["calls"]["value"] == "3" and rows["w.n"]["not_settable"] == "static", rows
    # GCC builds `cs` at compile time, in read-only data; clang, in the frame.
    assert rows["cs"]["value"] == "{start = 8, end = 9}", rows
    assert rows["cs"].get("not_settable") == ("static" if cxx == "gcc" else None), rows["cs"]
    # In a method, the static hides the member of its name: `calls` is the static.
    rows = rows_at(capsys, path, line_of(STATICS, "// RUN"), ["calls", "this.calls"])
    assert rows["calls"]["value"] == "101" and rows["this.calls"]["value"] == "7", rows


@pytest.mark.timeout(300)
def test_a_static_local_is_never_changed_in_cpp(directory, cxx, capsys):
    path = directory / "statics.cpp"
    path.write_text(STATICS)
    assert main(["session", "start", str(path), "--at", str(line_of(STATICS, "// HERE"))]) == 0
    capsys.readouterr()
    try:
        for assignment, phrase in (
            ("calls=5", "'calls' cannot be changed: tick(long) keeps it in the program's"),
            ("w.n=5", "`w` is a `static` local, kept in the program's static storage"),
            ("cs.end=5", "`cs` is const"),
        ):
            assert main(["session", "set", assignment, "--json"]) == 2
            error = json.loads(capsys.readouterr().out)["error"]
            assert error["code"] == "MUTATION_REJECTED" and phrase in error["message"], error
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(240)
def test_describe_marks_a_static_local_static(directory, cxx, capsys):
    # `describe` marks a `static` local as static, not as the frame's own.
    path = directory / "statics.cpp"
    path.write_text(STATICS)
    assert main(["describe", str(path), "--json"]) == 0
    scopes = json.loads(capsys.readouterr().out)["scopes"]
    tick = next(scope for scope in scopes if scope["scope"].startswith("tick"))
    statics = set(tick.get("statics", ()))
    assert {"calls", "w"} <= statics and "x" not in statics, tick
    # GCC builds `cs` at compile time, in read-only data; clang, in the frame.
    assert ("cs" in statics) is (cxx == "gcc"), tick
    run = next(scope for scope in scopes if scope["scope"].endswith("run"))
    assert run.get("statics") == ["calls"], run


# Two instances of a template, each with its own `calls`, which share
# their declaration's file and line; LLDB names g++'s by its symbol,
# `long tick<int>(int)::calls`.
TEMPLATES = """#include <cstdio>

template <typename T>
__attribute__((noinline)) long tick(T x) {
    static long calls = 0;
    calls += static_cast<long>(x);
    std::printf("%ld\\n", calls);  // HERE
    return calls;
}

int main() {
    tick<int>(1);
    tick<long>(10);
    tick<int>(2);
    tick<long>(20);
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_each_template_instance_reads_its_own_static(directory, cxx, capsys):
    path = directory / "templates.cpp"
    path.write_text(TEMPLATES)
    command = ["probe", str(path), "--at", str(line_of(TEMPLATES, "// HERE"))]
    code = main([*command, "--capture", "calls,x", "--hits", "4", "--json", "--quiet"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result
    rows = {(row["hit"], row["selector"]): row for row in result["rows"]}
    assert [rows[(hit, "x")]["value"] for hit in (1, 2, 3, 4)] == ["1", "10", "2", "20"], rows
    assert [rows[(hit, "calls")].get("value") for hit in (1, 2, 3, 4)] == [
        "1",
        "10",
        "3",
        "30",
    ], rows
