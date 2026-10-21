"""A real GCC for a C++ target (#167): refused before building, with what to set, never
a build failure on clang's `--driver-mode=g++`. Runs where a `g++` that is GCC exists
(the Linux lanes); macOS's `g++` is clang."""

import json
import os
import shutil

import pytest

from peeky.cli import main
from peeky.native import CPP_COMPILER_REFUSAL, compiler_family


@pytest.fixture
def gcc():
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    found = next(
        (path for name in ("g++", "g++-13", "g++-14") if (path := shutil.which(name))), None
    )
    if found is None or compiler_family(os.path.realpath(found)) != "gcc":
        pytest.skip("no g++ that is GCC here")
    return found


@pytest.mark.timeout(60)
def test_gcc_for_cpp_is_refused_before_building(gcc, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PEEKY_NATIVE_CXX", gcc)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "a.cpp").write_text("int main() {\n    int t = 1;\n    return t;\n}\n")
    code = main(["probe", str(tmp_path / "a.cpp"), "--at", "2", "--capture", "t", "--json"])
    shown = json.loads(capsys.readouterr().out)
    error = shown["error"]
    # Before: BUILD_FAILED, "unrecognized command-line option '--driver-mode=g++'".
    assert code == 1 and error["code"] == "UNSUPPORTED", error
    assert error["message"].startswith(CPP_COMPILER_REFUSAL) and "is GCC" in error["message"]
    assert "driver-mode" not in json.dumps(shown)
    assert shown.get("refused_before_launch") is True and "evidence" not in shown
    code = main(["describe", str(tmp_path / "a.cpp"), "--json"])
    described = json.loads(capsys.readouterr().out)
    assert code != 0 and described["error"]["code"] == "UNSUPPORTED"
