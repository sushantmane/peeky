"""A real GCC builds a C++ target (#167): without clang's `--driver-mode=g++`, which g++
does not know, and a probe and `describe` read it. Runs where a `g++` that is GCC exists
(the Linux lanes); macOS's `g++` is clang."""

import json

import pytest
from native_compilers import cxx_for

from peeky.cli import main

SOURCE = """#include <cstdio>
#include <string>
int main() {
    std::string name = "named";
    int t = 1;
    std::printf("%s %d\\n", name.c_str(), t); // HERE
    return 0;
}
"""


@pytest.fixture
def gcc():
    found, why = cxx_for("gcc")
    if found is None:
        pytest.skip(why)
    return found


@pytest.mark.timeout(120)
def test_gcc_builds_cpp_and_peeky_reads_it(gcc, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PEEKY_NATIVE_CXX", gcc)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "a.cpp"
    path.write_text(SOURCE)
    line = next(i for i, text in enumerate(SOURCE.splitlines(), 1) if "// HERE" in text)
    command = ["probe", str(path), "--at", str(line), "--capture", "t,name"]
    code = main([*command, "--json", "--quiet"])
    shown = json.loads(capsys.readouterr().out)
    # Before: UNSUPPORTED, "C++ targets need clang++ ... (.../g++ is GCC)", and before that,
    # BUILD_FAILED on "unrecognized command-line option '--driver-mode=g++'".
    assert code == 0, shown
    assert "driver-mode" not in json.dumps(shown)
    rows = {row["selector"]: row for row in shown["rows"]}
    assert rows["t"]["value"] == "1", rows
    assert rows["name"]["value"] == '"named"', rows
    assert "named 1" in shown["output"], shown["output"]
    code = main(["describe", str(path), "--json"])
    described = json.loads(capsys.readouterr().out)
    assert code == 0, described
