"""Puts tests/ on the import path, for the helpers the suites share (native_compilers);
gives C++ cases whose expectations depend on the compiler a fixture over clang and GCC; and
marks every C++ case `cpp`, so a lane can run only them (`-m cpp`)."""

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from native_compilers import cxx_for  # noqa: E402


def pytest_collection_modifyitems(items):
    # All of tests/cpp, and the cases of tests/native whose name says C++ (`test_cpp_…`,
    # `[cpp]`, `[tri.cpp]`); tests/native marks the others by hand.
    for item in items:
        try:
            suite = Path(item.path).resolve().relative_to(HERE).parts[0]
        except ValueError:
            continue
        if suite == "cpp" or (suite == "native" and "cpp" in item.name.lower()):
            item.add_marker(pytest.mark.cpp)


@pytest.fixture(params=["clang", "gcc"])
def cxx(request, monkeypatch):
    """The C++ compiler's family, "clang" or "gcc", with PEEKY_NATIVE_CXX set to it: for a
    case whose expectations depend on how the compiler lays out code or names functions.
    Each case skips where its compiler is missing (no g++ that is GCC on macOS), and the
    clang case where the lane builds C++ with g++."""
    compiler, why = cxx_for(request.param)
    if compiler is None:
        pytest.skip(why)
    monkeypatch.setenv("PEEKY_NATIVE_CXX", compiler)
    return request.param
