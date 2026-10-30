"""`probe --set-at` on real C++ builds: a value changed at one line and restored at the
next in one probe, and a line that stops in a lambda as well as in its function."""

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


@pytest.mark.timeout(240)
def test_a_value_shortened_at_one_line_is_restored_at_the_next_in_one_probe(cpp, capsys):
    # `colon` shortened at line 22 for the names with a trailing space and restored at
    # line 23, so `std::stol` still reads the quantity: one probe, where one `--set colon=`
    # crashes at line 23.
    source = cpp("stock.cpp", STOCK)
    name = line_of(STOCK, "std::string name = trim_left")
    qty = line_of(STOCK, "long qty = ")
    sets = []
    for hit, short, whole in ((3, 6, 7), (4, 4, 5)):
        sets += ["--set-at", f"stock.cpp:{name}@{hit}", f"colon={short}"]
        sets += ["--set-at", f"stock.cpp:{qty}@{hit}", f"colon={whole}"]
    argv = ["probe", str(source), "--at", str(name), "--capture", "colon", "--hits", "all"]
    code = main([*argv, *sets, "--json", "--quiet", "--", "apple:3, pear:2, apple :4,pear :1"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert "STOCK:apple=7,pear=3" in result["output"], result["output"]
    assert [
        (change["before"], change["after"], change["line"], change["hit"])
        for change in result["interventions"]
    ] == [("7", "6", name, 3), ("6", "7", qty, 3), ("5", "4", name, 4), ("4", "5", qty, 4)]


LAMBDA = """#include <cstdio>
int main() {
    long total = 0;
    for (long i = 0; i < 3; i++) {
        long v = i * 10;
        auto f = [](long v) { return v + 1; }; total += f(v); // BOTH
    }
    std::printf("T %ld\\n", total); // END
}
"""


@pytest.mark.timeout(240)
def test_a_set_at_line_that_stops_in_a_lambda_says_whose_variable_changed(cpp, cxx, capsys):
    # Line BOTH stops in `main` and in the lambda it declares, and @HIT counts both, so
    # `6@2` changes the lambda's `v`, and says so.
    source = cpp("lam.cpp", LAMBDA)
    both, end = line_of(LAMBDA, "BOTH"), line_of(LAMBDA, "END")
    argv = ["probe", str(source), "--at", str(end), "--capture", "total"]
    code = main([*argv, "--set-at", f"{both}@2", "v=100", "--quiet"])
    shown = capsys.readouterr()
    text = shown.out
    if cxx == "gcc":
        # GCC marks no prologue end, so the lambda's one-line body stops at its first
        # instruction, before its prologue stores `v`: the change is refused, and says why.
        # GCC names the lambda plain `operator()`.
        warned = f"warning: --set-at line {both} stopped in 2 functions: main (stop 1); "
        assert code == 1 and warned + "operator() (stop 2)." in text, text
        assert "MUTATION_REJECTED" in text and "(in operator())" in text, text
        assert "the stop is at its function's start, before the prologue stores the " in text
    else:
        assert code == 0, shown
        expected = f"set v: 0 -> 100  (--set-at line {both}@2, in main::$_0::operator())"
        assert expected in text, text
        warned = f"warning: --set-at line {both} stopped in 2 functions: main (stop 1); main::"
        assert warned in text, text
        assert "T 133" in text
        assert main(["sql", "SELECT name, line, hit, function FROM interventions", "--json"]) == 0
        (stored,) = json.loads(capsys.readouterr().out)["rows"]
        assert stored["line"] == both and stored["hit"] == 2
        assert stored["function"].startswith("main::$_0::operator()"), stored
    # main's own `v` on the second pass is that line's third stop.
    assert main([*argv, "--set-at", f"{both}@3", "v=100", "--json", "--quiet"]) == 0
    result = json.loads(capsys.readouterr().out)
    (change,) = result["interventions"]
    assert change["function"] == "main" and change["before"] == "10", change
    assert "T 123" in result["output"], result["output"]
