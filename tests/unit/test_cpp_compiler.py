"""C++ targets build with clang or GCC, told apart by what the compiler's
`--version` says: only clang gets `--driver-mode=g++`, a clang option. Any other compiler
is refused before anything is built, since Peeky's C++ rules are checked against clang's
and GCC's debug information only."""

import json
import os
import stat

import pytest

from peeky.errors import classify
from peeky.native import CPP_COMPILER_REFUSAL, NativeTarget, compiler_family

GCC = "g++ (Ubuntu 13.2.0-23ubuntu4) 13.2.0\nCopyright (C) 2023 Free Software Foundation, Inc."
CLANG = "Ubuntu clang version 18.1.3 (1ubuntu1)\nTarget: aarch64-unknown-linux-gnu"
APPLE = "Apple clang version 17.0.0 (clang-1700.3.19.1)\nTarget: arm64-apple-darwin25.0.0"


def fake_compiler(tmp_path, name, says):
    """An executable that prints ``says`` for `--version` and counts how often it ran."""
    path = tmp_path / name
    count = tmp_path / f"{name}.runs"
    path.write_text(f"#!/bin/sh\necho run >> '{count}'\ncat <<'EOF'\n{says}\nEOF\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path, count


def runs(count):
    return len(count.read_text().splitlines()) if count.exists() else 0


@pytest.mark.parametrize(
    "name, says, family",
    [
        # Named alike, told apart by what they say: `g++` is often clang (macOS), and a
        # `clang++` may be anything.
        ("g++", GCC, "gcc"),
        ("clang++", GCC, "gcc"),
        ("g++-apple", APPLE, "clang"),
        ("c++", CLANG, "clang"),
        ("icpx", "Intel(R) oneAPI DPC++/C++ Compiler 2024.0.0", "unknown"),
    ],
)
def test_the_compiler_is_told_by_its_version_once(tmp_path, name, says, family):
    path, count = fake_compiler(tmp_path, name, says)
    assert compiler_family(str(path)) == family
    assert compiler_family(str(path)) == family and runs(count) == 1
    # A changed compiler file is asked again.
    os.utime(path, ns=(1, 1))
    assert compiler_family(str(path)) == family and runs(count) == 2


def target(compiler, language="cpp"):
    source = "a.cpp" if language == "cpp" else "a.c"
    return NativeTarget(sources=(source,), compiler=str(compiler), language=language)


def test_a_cpp_target_built_with_neither_clang_nor_gcc_is_refused_before_anything_runs(tmp_path):
    other, _ = fake_compiler(tmp_path, "icpx", "Intel(R) oneAPI DPC++/C++ Compiler 2024.0.0")
    (tmp_path / "a.cpp").write_text("int main() { return 0; }\n")
    # Refused before the build, with the adapter and dwarfdump never needed.
    with pytest.raises(ValueError) as refused:
        target(other).prepare(tmp_path / "a.cpp", tmp_path)
    assert str(refused.value).startswith(CPP_COMPILER_REFUSAL)
    assert "is neither clang nor GCC" in str(refused.value)
    assert refused.value.before_launch is True
    error = classify(refused.value)
    assert error["code"] == "UNSUPPORTED" and "PEEKY_NATIVE_CXX" in error["hint"]
    assert "g++" in error["hint"] and len(error["hint"]) <= 150
    assert not (tmp_path / "program").exists()


def test_only_clang_gets_its_cpp_driver_and_gcc_builds_cpp_and_c(tmp_path):
    clang, _ = fake_compiler(tmp_path, "clang", CLANG)
    driver, flags, _, _ = target(clang)._compile_flags()
    assert driver == ("--driver-mode=g++",) and flags[0] == "--driver-mode=g++"
    # GCC builds C++, without clang's option, which g++ does not know: before, it
    # was refused.
    gxx, count = fake_compiler(tmp_path, "g++", GCC)
    driver, flags, _, _ = target(gxx)._compile_flags()
    assert driver == () and "--driver-mode=g++" not in flags and runs(count) == 1
    # C targets build with GCC as they did, and never ask the compiler what it is.
    gcc, count = fake_compiler(tmp_path, "gcc", GCC)
    driver, flags, _, _ = target(gcc, "c")._compile_flags()
    assert driver == () and "--driver-mode=g++" not in flags and runs(count) == 0


def test_a_probe_with_an_unknown_compiler_for_cpp_keeps_no_run(tmp_path, monkeypatch, capsys):
    from peeky.cli import main

    other, _ = fake_compiler(tmp_path, "c++", "Intel(R) oneAPI DPC++/C++ Compiler 2024.0.0")
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("lldb-dap", "llvm-dwarfdump", "dsymutil"):
        fake_compiler(tools, name, "LLVM version 18")
    monkeypatch.setenv("PEEKY_NATIVE_CXX", str(other))
    monkeypatch.setenv("PEEKY_NATIVE_DAP", str(tools / "lldb-dap"))
    monkeypatch.setenv("PEEKY_NATIVE_DWARFDUMP", str(tools / "llvm-dwarfdump"))
    monkeypatch.setenv("PEEKY_NATIVE_DSYMUTIL", str(tools / "dsymutil"))
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "a.cpp").write_text("int main() {\n    int t = 1;\n    return t;\n}\n")
    code = main(["probe", str(tmp_path / "a.cpp"), "--at", "2", "--capture", "t", "--json"])
    shown = json.loads(capsys.readouterr().out)
    assert code == 2 and shown["error"]["code"] == "UNSUPPORTED"
    assert shown["error"]["message"].startswith(CPP_COMPILER_REFUSAL)
    assert "evidence" not in shown and shown.get("refused_before_launch") is True
    assert not (tmp_path / "state" / "evidence" / "evidence.db").exists()


def test_a_compiler_that_never_answers_is_ended_with_its_children(tmp_path, monkeypatch):
    # The timeout killed only the wrapper; its own child (`sleep 60`)
    # kept running. `--version` runs in a group of its own, ended whole.
    import time

    from peeky import native

    monkeypatch.setattr(native, "VERSION_TIMEOUT", 0.5)
    child = tmp_path / "child.pid"
    wrapper = tmp_path / "slow++"
    wrapper.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > '{child}'\nsleep 60\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    began = time.monotonic()
    assert compiler_family(str(wrapper)) == "timeout"
    assert time.monotonic() - began < 10
    from peeky import processes

    pid = int(child.read_text())
    deadline = time.monotonic() + 5
    # Gone, or a zombie its new parent has yet to reap (a container's init may not).
    while processes.alive((processes.table() or {}).get(pid)):
        if time.monotonic() > deadline:
            os.kill(pid, 9)
            raise AssertionError("the wrapper's child outlived the timeout")
        time.sleep(0.05)
    # Not kept: a compiler slow once (a loaded host) is asked again next time.
    assert not any(key[0] == str(wrapper) for key in native._FAMILIES)
    with pytest.raises(ValueError) as refused:
        target(wrapper)._compile_flags()
    assert "timed out: no answer to --version within 0.5 s" in str(refused.value)
