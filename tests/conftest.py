"""Puts tests/ on the import path, for the helpers the suites share (native_compilers);
gives C++ cases whose expectations depend on the compiler a fixture over clang and GCC;
marks every C++ case `cpp`, so a lane can run only them (`-m cpp`); and gives the tests a
private temporary directory where the system's is shared."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from native_compilers import cxx_for  # noqa: E402


def _shared(path):
    path = Path(path).resolve()
    return any(
        (info := directory.stat()).st_uid not in (0, os.getuid()) or info.st_mode & 0o022
        for directory in (*path.parents, path)
    )


# Peeky builds only where no other user can write (peeky.build.private_base), and tests
# keep their state under tmp_path. Where the temporary directory is shared (Linux's
# /tmp), tests use a private one of their own, and so do the processes they start.
if _shared(tempfile.gettempdir()):
    _private = Path.home() / ".cache" / "peeky-tests"
    _private.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _shared(_private):
        os.environ["TMPDIR"] = tempfile.tempdir = str(_private)


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
