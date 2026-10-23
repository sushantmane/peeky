"""#288 (found while preparing the eleventh evaluation): in C++ built with clang, a local
declared on the stop line showed its leftovers, `[settable]`, with no note, at the line's
first instruction. At -O0 clang puts an exception landing pad, which ends in a jump, just
before that line's code, on a closing `}`'s line. Peeky took it for a later line's code
running into the line, as a `for` header over two lines does. Now the stop's disassembly
tells them apart. Each case is built with clang and with g++, which puts its landing pads
after the function's code; a local is withheld at its line's first instruction with both."""

import json

import pytest

from peeky.cli import main

# The issue's repro: a range-for over strings, and a braced inner loop calling a member
# function, whose landing pad lies just before line 16.
REPRO = """#include <cstdio>
#include <string>
#include <vector>
struct S {
    int v = 0;
    void add(int x) { v += x; }
};

int main() {
    std::vector<std::string> names = {"a", "b", "c"};
    S s;
    for (const std::string& name : names) {
        for (int k = 0; k < 2; k++) {
            s.add(k);
        }
        int q = 0;
        std::printf("%s %d\\n", name.c_str(), q + s.v);
    }
    return 0;
}
"""
# Like the evaluation's grades: on main, hits 2 and 3 showed the previous student's quiz.
GRADES = """#include <cstdio>
#include <string>
#include <unordered_map>
#include <vector>

struct Stats {
    int sum = 0;
    int count = 0;
    void add(int score) { sum += score; count++; }
    void reset() { sum = 0; count = 0; }
    int average() const { return sum / (count > 0 ? count : 1); }
};

int main() {
    std::unordered_map<std::string, std::vector<int>> book = {
        {"Maya", {58, 91}}, {"Leon", {72, 80}}, {"Ines", {90, 64}}};
    std::vector<std::string> names = {"Maya", "Leon", "Ines"};
    Stats stats;
    for (const std::string& name : names) {
        const std::vector<int>& quizzes = book.at(name);
        stats.reset();
        for (int q : quizzes) {
            stats.add(q);
        }
        int quiz = stats.average(); // QUIZ
        std::printf("%s=%d\\n", name.c_str(), quiz); // PRINT
    }
    return 0;
}
"""
# A string the loop's body owns: its landing pad ends on the body's `}` (line 19), not on
# the function's.
INNER = """#include <cstdio>
#include <string>
#include <vector>
struct S {
    int v = 0;
    void add(int x) { v += x; }
};

int main() {
    std::vector<std::string> names = {"a", "b", "c"};
    S s;
    for (const std::string& name : names) {
        std::string t = name + "!";
        for (int k = 0; k < 2; k++) {
            s.add(k);
        }
        int q = 0;
        std::printf("%s %d\\n", t.c_str(), q + s.v);
    }
    return 0;
}
"""


@pytest.fixture
def build(cxx, tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))

    def write(name, text):
        path = tmp_path / name
        path.write_text(text)
        return path

    return write


def probe(capsys, path, *arguments):
    code = main(["probe", str(path), *arguments, "--json", "--quiet"])
    return code, json.loads(capsys.readouterr().out)


def line_of(text, marker):
    return next(i for i, line in enumerate(text.splitlines(), 1) if line.endswith(marker))


def withheld(row, line):
    note = f"declared on line {line}, which has not run yet at this stop (its first instruction)"
    return "value" not in row and note in str(row.get("error")) and row.get("settable") is False


@pytest.mark.timeout(180)
def test_a_local_declared_after_a_landing_pad_is_withheld_at_its_lines_start(build, capsys):
    program = build("q.cpp", REPRO)
    code, result = probe(capsys, program, "--at", "16", "--capture", "q", "--hits", "all")
    assert code == 0, result.get("error")
    rows = result["rows"]
    assert [row["hit"] for row in rows] == [1, 2, 3]
    assert all(withheld(row, 16) for row in rows), rows
    # Once its line has run, it reads.
    code, result = probe(capsys, program, "--at", "17", "--capture", "q", "--hits", "all")
    assert code == 0 and [row["value"] for row in result["rows"]] == ["0", "0", "0"]
    # A change there is refused: the declaration would overwrite it.
    code, result = probe(capsys, program, "--at", "16", "--capture", "s.v", "--set", "q=5")
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED", result
    assert "declared on line 16" in result["error"]["message"]


@pytest.mark.timeout(180)
def test_a_local_never_shows_the_last_passs_value_at_its_lines_start(build, capsys):
    program = build("grades.cpp", GRADES)
    quiz, after = line_of(GRADES, "// QUIZ"), line_of(GRADES, "// PRINT")
    code, result = probe(
        capsys, program, "--at", str(quiz), "--capture", "quiz,name", "--hits", "all"
    )
    assert code == 0, result.get("error")
    rows = {(row["hit"], row["selector"]): row for row in result["rows"]}
    assert all(withheld(rows[hit, "quiz"], quiz) for hit in (1, 2, 3)), rows
    assert [rows[hit, "name"]["value"].split()[-1] for hit in (1, 2, 3)] == [
        '"Maya"',
        '"Leon"',
        '"Ines"',
    ]
    code, result = probe(capsys, program, "--at", str(after), "--capture", "quiz", "--hits", "all")
    assert code == 0 and [row["value"] for row in result["rows"]] == ["74", "76", "77"]
    # A session continued to the line again is at its start too.
    assert main(["session", "start", str(program), "--at", str(quiz)]) == 0
    try:
        assert main(["session", "continue"]) == 0
        capsys.readouterr()
        assert main(["session", "get", "quiz", "--json"]) == 1
        [row] = json.loads(capsys.readouterr().out)
    finally:
        main(["session", "close"])
        capsys.readouterr()
    assert withheld(row, quiz), row


@pytest.mark.timeout(180)
def test_a_landing_pad_on_an_inner_blocks_closing_line_is_no_later_lines_code(build, capsys):
    program = build("inner.cpp", INNER)
    code, result = probe(capsys, program, "--at", "17", "--capture", "q", "--hits", "all")
    assert code == 0, result.get("error")
    assert len(result["rows"]) == 3 and all(withheld(row, 17) for row in result["rows"])


@pytest.mark.timeout(180)
def test_where_the_code_before_the_line_cannot_be_read_the_row_says_so(
    build, cxx, capsys, monkeypatch
):
    # Without the disassembly, clang's landing pad might be a later line's code running
    # into line 16: the value is shown with a note, and not settable. GCC's line 16 has
    # no other line's code just before it.
    monkeypatch.setattr("peeky.native.control_sources", lambda *_: None)
    program = build("q.cpp", REPRO)
    code, result = probe(capsys, program, "--at", "16", "--capture", "q")
    assert code == 0, result.get("error")
    [row] = result["rows"]
    if cxx == "gcc":
        assert withheld(row, 16), row
        return
    assert row["note"] == (
        "may be leftovers: this stop may be where line 16 starts, before its declaration runs, "
        "and Peeky could not read the code just before it to tell; read it on the next line"
    )
    assert "value" in row and row["settable"] is False, row
    code, result = probe(capsys, program, "--at", "16", "--capture", "s.v", "--set", "q=5")
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED", result
    message = result["error"]["message"]
    assert "declared on line 16, which may not have run yet at this stop" in message
