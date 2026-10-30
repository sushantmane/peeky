"""In C++ built with clang, a local declared on the stop line holds leftovers at the line's
first instruction. At -O0 clang puts an exception landing pad, which ends in a jump, just
before that line's code, on a closing `}`'s line. Taken for a later line's code running into
the line, as a `for` header over two lines is, it would leave the local shown, `[settable]`,
with no note. The stop's disassembly tells them apart. Each case is built with clang and with
g++, which puts its landing pads after the function's code; a local is withheld at its line's
first instruction with both."""

import bisect
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
# Grades: unwithheld, hits 2 and 3 would show the previous student's quiz.
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
    code, result = probe(capsys, program, "--at", str(quiz), "--capture", "quiz", "--hits", "all")
    assert code == 0, result.get("error")
    rows = result["rows"]
    assert [row["hit"] for row in rows] == [1, 2, 3]
    assert all(withheld(row, quiz) for row in rows), rows
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


UNREAD = (
    "may be leftovers: this stop may be where line 16 starts, before its declaration runs, and "
    "Peeky could not read the code just before it to tell; read it on the next line"
)
PATHS = (
    "may be leftovers: this stop may be where line 16 starts, before its declaration runs. "
    "Control reaches it by more than one path, or by one Peeky cannot follow (a call that may "
    "not return, a computed jump, an exception); read it on the next line"
)


class Everywhere:
    """Landing pads at every address."""

    def __contains__(self, address):
        return True


@pytest.mark.timeout(180)
@pytest.mark.parametrize("unknown", ["unread", "paths", "unwinder"])
def test_where_peeky_cannot_tell_how_control_came_the_row_says_so(
    build, cxx, capsys, monkeypatch, unknown
):
    # Clang's landing pad lies just before line 16. Where Peeky cannot read the
    # disassembly ("unread": an architecture it does not know), where control may come
    # both from before the declaration and past it ("paths"), or where the unwinder may
    # enter the line ("unwinder"), the value is shown
    # with a note, and a change is refused; never shown plainly. GCC's line 16 has no
    # other line's code just before it.
    if unknown == "unread":
        monkeypatch.setattr("peeky.native.architecture", lambda binary: None)
    elif unknown == "paths":
        monkeypatch.setattr("peeky.native.runs_into", lambda *_: None, raising=False)
    else:
        monkeypatch.setattr("peeky.native.landing_pads", lambda _: Everywhere(), raising=False)
    program = build("q.cpp", REPRO)
    code, result = probe(capsys, program, "--at", "16", "--capture", "q")
    assert code == 0, result.get("error")
    [row] = result["rows"]
    if cxx == "gcc":
        # Its line's start: withheld, or with no disassembly, a loop may return there.
        assert withheld(row, 16) or (row.get("note") and row["settable"] is False), row
        return
    assert row.get("note") == (UNREAD if unknown == "unread" else PATHS), row
    assert "value" in row and row["settable"] is False, row
    code, result = probe(capsys, program, "--at", "16", "--capture", "s.v", "--set", "q=5")
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED", result
    message = result["error"]["message"]
    assert "declared on line 16, which may not have run yet at this stop" in message


@pytest.mark.timeout(180)
def test_the_binarys_landing_pads_are_read_where_the_compiler_put_them(
    build, cxx, capsys, monkeypatch
):
    # The unwinder enters a function at its landing pads, which
    # no disassembly shows. Peeky reads them from the binary's exception tables (Mach-O's
    # `__unwind_info` and `__gcc_except_tab`, ELF's `.eh_frame` and `.gcc_except_table`).
    # Clang and GCC both put `main`'s in code of its closing line, 20; none is line 16's.
    from peeky.native import NativeSession, landing_pads

    seen = []
    check = NativeSession._check_loop_target

    def spy(self, dap, frame):
        if not seen:
            seen.append((self.declarations, landing_pads(self.binary)))
        return check(self, dap, frame)

    monkeypatch.setattr(NativeSession, "_check_loop_target", spy)
    program = build("q.cpp", REPRO)
    code, result = probe(capsys, program, "--at", "16", "--capture", "q")
    assert code == 0 and withheld(result["rows"][0], 16), result
    declarations, pads = seen[0]
    [function] = [each for each in declarations if each["name"] == "main"]
    own = sorted(pad for pad in pads if any(low <= pad < high for low, high in function["ranges"]))
    assert own, sorted(pads)
    rows = function["order"]
    for pad in own:
        row = rows[bisect.bisect_right(rows, pad, key=lambda row: row[0]) - 1]
        assert row[1:] == ("q.cpp", 20), (hex(pad), row)
    assert not any(found & pads for found in function["preceded"].values())
