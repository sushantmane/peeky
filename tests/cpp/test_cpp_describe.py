"""C++ `describe` on real builds: types named as code writes them, no compiler-generated
locals, and a block's locals not listed on lines outside the block."""

import json
import os

import pytest

from peeky.cli import main

ROUTES = """#include <string>
#include <vector>
int main() {
    std::vector<std::string> parts{"users", "42"};
    int total = 0;
    for (const std::string &part : parts) {
        total += (int)part.size();
    }
    return total == 7 ? 0 : 1;
}
"""


@pytest.mark.timeout(240)
def test_describe_names_cpp_types_as_code_does_without_compiler_locals(
    tmp_path, capsys, monkeypatch
):
    # `std::vector<std::string>`, as probe tables show it, not
    # `std::__1::vector<std::__1::basic_string<char, ...>>`; and not the range `for`'s
    # `__range1`, `__begin1` and `__end1`, which no code names.
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "routes.cpp"
    source.write_text(ROUTES)
    code = main(["describe", str(source), "--json"])
    out = capsys.readouterr().out
    assert code == 0, out
    (main_scope,) = [s for s in json.loads(out)["scopes"] if s["scope"] == "main"]
    assert main_scope["names"] == ["part", "parts", "total"], main_scope
    assert main_scope["types"]["parts"] == "std::vector<std::string>", main_scope
    # llvm-dwarfdump 18 names a typedef without its scope (`const string &`).
    assert main_scope["types"]["part"] in ("const std::string &", "const string &"), main_scope


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


@pytest.mark.timeout(120)
def test_a_blocks_locals_are_not_listed_on_lines_outside_it(cpp, capsys):
    # Not `name (lines 22-35)` for the `if` block's local and `colon (lines 17-35)` for the
    # `while` body's: C++ puts a block's cleanup code (run when an exception leaves it) on
    # the function's closing line, beyond the block. The lines between, where code outside
    # the block runs, are not the block's; its own lines, and that closing line, still are.
    source = cpp("stock.cpp", STOCK)
    assert main(["describe", str(source), "--json"]) == 0
    scopes = json.loads(capsys.readouterr().out)["scopes"]
    spans = next(scope for scope in scopes if scope["scope"] == "main")["spans"]

    def lines(name):
        return {line for first, last in spans[name] for line in range(first, last + 1)}

    for name in ("name", "qty"):
        assert {22, 23, 24} <= lines(name) and not lines(name) & set(range(26, 35)), spans
    for name in ("comma", "entry", "colon"):
        assert set(range(17, 25)) <= lines(name), spans
        assert not lines(name) & set(range(28, 35)), spans
    assert main(["describe", str(source)]) == 0
    text = capsys.readouterr().out
    assert "lines 22-35" not in text and "lines 17-35" not in text, text
    assert ", lines 22-2" in text, text
