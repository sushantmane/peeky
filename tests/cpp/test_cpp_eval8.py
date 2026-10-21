"""The eighth agent evaluation's C++ `describe` findings, on real builds: types named as
code writes them (L12), and no compiler-generated locals (L13)."""

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
    # L12: describe showed `std::__1::vector<std::__1::basic_string<char, ...>>`, while
    # probe tables show `std::vector<std::string>`. L13: it listed the range `for`'s
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
