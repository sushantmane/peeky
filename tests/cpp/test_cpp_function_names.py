"""C++ frame names on real builds: a table shows a function's short name, not its whole
signature, and says how to match the name the evidence store keeps; overloads keep their
parameters, so they stay apart."""

import contextlib
import json
import os

import pytest

from peeky.cli import main

STOCK = """#include <cstdio>
#include <map>
#include <string>

// Parses "name:qty,name:qty,..." and prints the total per name, sorted by name.
static std::string trim_left(const std::string &s) {
    size_t i = 0;
    while (i < s.size() && s[i] == ' ') i++;
    return s.substr(i);
}

int main(int argc, char **argv) {
    std::string input = argc > 1 ? argv[1] : "";
    std::map<std::string, long> stock;
    size_t pos = 0;
    while (pos <= input.size()) {
        size_t comma = input.find(',', pos);
        if (comma == std::string::npos) comma = input.size();
        std::string entry = input.substr(pos, comma - pos);
        size_t colon = entry.find(':');
        if (colon != std::string::npos) {
            std::string name = trim_left(entry.substr(0, colon));
            long qty = std::stol(entry.substr(colon + 1));
            stock[name] += qty;
        }
        pos = comma + 1;
    }
    std::string out;
    for (const auto &item : stock) {
        if (!out.empty()) out += ",";
        out += item.first + "=" + std::to_string(item.second);
    }
    std::printf("STOCK:%s\\n", out.c_str());
    return 0;
}
"""


ARGS = ["--arg", "apple:3, pear:2, apple :4"]


# Two overloads on one line: one probe stops in both.
OVERLOADS = """#include <cstdio>
#include <string>
static int twice(int x) { return 2 * x; } static int twice(const std::string &s) { return 2 * (int)s.size(); }
int main() {
    int n = twice(3) + twice(std::string("ab"));
    std::printf("%d\\n", n);
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


@contextlib.contextmanager
def session(source, line, capsys, *extra):
    assert main(["session", "start", str(source), "--at", str(line), *extra]) == 0
    started = capsys.readouterr().out

    def get(*selectors, text=False):
        main(["session", "get", *selectors, *([] if text else ["--json"])])
        out = capsys.readouterr().out
        return out if text else {row["selector"]: row for row in json.loads(out)}

    try:
        yield get, started
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(120)
def test_a_cpp_frames_signature_does_not_widen_the_table(cpp, cxx, capsys):
    # The function column shows `trim_left`, not `trim_left(std::__1::basic_string<char,
    # std::__1::char_traits<char>, std::__1::allocator<char>> const&)`, which makes the
    # table about 200 columns wide. JSON and the evidence store keep the whole name. GCC
    # gives a static function no linkage name, so LLDB names it from its declaration, from
    # the global scope: `::trim_left`, `::twice(int)`, which tables show as clang's. The
    # hints name what is stored.
    source = cpp("stock.cpp", STOCK)
    assert main(["probe", str(source), "--at", "9", *ARGS, "--capture", "i", "--hits", "2"]) == 0
    text = capsys.readouterr().out
    assert "trim_left " in text and "basic_string" not in text, text
    assert max(len(line) for line in text.splitlines() if line.startswith(("hit", "1 "))) < 80
    # SQL compares the stored name, which `WHERE function = 'trim_left'` does not match;
    # the probe's table says how to match it.
    assert "note: function shows short frame names" in text, text
    assert "short_function(function) = 'trim_left'" in text, text
    assert main(["sql", "SELECT count(*) AS n FROM last_run WHERE function = 'trim_left'"]) == 0
    assert "\n0" in capsys.readouterr().out
    assert main(["sql", "SELECT DISTINCT function FROM last_run", "--json"]) == 0
    (stored,) = [row["function"] for row in json.loads(capsys.readouterr().out)["rows"]]
    if cxx == "gcc":
        assert stored == "::trim_left(const std::string &)", stored
    else:
        assert stored.startswith("trim_left(") and "basic_string" in stored, stored
    assert main(["queries", "run"]) == 0
    text = capsys.readouterr().out
    assert "trim_left " in text and "basic_string" not in text, text
    # Once per run: the probe said it.
    assert "note: function shows short frame names" not in text, text
    query = "SELECT count(*) AS n FROM last_run WHERE short_function(function) = 'trim_left'"
    assert main(["sql", query, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == [{"n": 2}]
    # Two overloads in one table keep their parameters, so they stay apart.
    source = cpp("overloads.cpp", OVERLOADS)
    assert main(["probe", str(source), "--at", "3", "--capture", "x", "--hits", "all"]) == 0
    text = capsys.readouterr().out
    by_text = "twice(const std::string &)" if cxx == "gcc" else "twice(std::string const&)"
    assert "twice(int)" in text and by_text in text, text
    assert "basic_string" not in text, text
    # A probe's table says the note once per state directory; a query's table says it of a
    # run that has not had it. Its example is what short_function gives: both overloads'
    # rows.
    assert "note: function shows short frame names" not in text, text
    assert main(["queries", "run"]) == 0
    text = capsys.readouterr().out
    # GCC's `::twice(int)` shows as `twice(int)`, as clang's does: the note says so.
    assert "short_function(function) = 'twice'" in text, text
    query = "SELECT count(*) AS n FROM last_run WHERE short_function(function) = 'twice'"
    assert main(["sql", query, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == [{"n": 2}]
    # A session names the frame with its parameters, as code writes their types.
    with session(source, 3, capsys, "--hit", "2") as (_, started):
        assert f" in {by_text}, hit 2" in started, started
