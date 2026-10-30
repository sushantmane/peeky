"""Which compiler a native test builds with, told by what its `--version` says, never by its
name: macOS's `g++` is Apple clang, and a lane may set PEEKY_NATIVE_CXX to g++."""

import os
import shutil
import subprocess


def is_gcc(compiler):
    """Whether ``compiler`` is GCC."""
    try:
        version = subprocess.run([compiler, "--version"], capture_output=True, text=True)
    except OSError:
        return False
    return "Free Software Foundation" in version.stdout


def gxx():
    """A g++ on the path that is GCC, or None."""
    for name in ("g++", "g++-13", "g++-14"):
        if (found := shutil.which(name)) and is_gcc(found):
            return found
    return None


def gcc():
    """A gcc on the path that is GCC, or None."""
    for name in ("gcc", "gcc-13", "gcc-14"):
        if (found := shutil.which(name)) and is_gcc(found):
            return found
    return None


def default_cxx():
    """The C++ compiler a case builds with unless it says otherwise: PEEKY_NATIVE_CXX, else
    the one Peeky picks (`clang++` or `clang++-18` on the path, xcrun's on macOS), or None."""
    if found := os.environ.get("PEEKY_NATIVE_CXX"):
        return found
    from peeky.cli import native_tools

    try:
        return native_tools("cpp")["compiler"]
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def cxx_is_gcc():
    """Whether the C++ compiler cases build with by default is GCC."""
    compiler = default_cxx()
    return bool(compiler) and is_gcc(compiler)


def default_cc():
    """The C compiler a case builds with: PEEKY_NATIVE_CC, else the one Peeky picks
    (`clang` or `clang-18` on the path, xcrun's on macOS), or None."""
    if found := os.environ.get("PEEKY_NATIVE_CC"):
        return found
    from peeky.cli import native_tools

    try:
        return native_tools("c")["compiler"]
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def cc_is_gcc():
    """Whether the C compiler cases build with is GCC."""
    compiler = default_cc()
    return bool(compiler) and is_gcc(compiler)


def cxx_for(family):
    """The C++ compiler a case parametrized over ``family`` ("clang" or "gcc") builds with,
    or (None, why) to skip it. A lane builds each family's cases with its default C++
    compiler (``default_cxx``) where that is of the family. Elsewhere the clang cases skip
    (the clang lanes run them), and the GCC cases use a g++ on the path."""
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        return None, "set PEEKY_NATIVE_DAP and native tool paths to enable native conformance"
    compiler = default_cxx()
    if compiler and (family == "gcc") == is_gcc(compiler):
        return compiler, None
    if family == "clang":
        return None, "this lane builds C++ with GCC; the clang lanes run the clang case"
    found = gxx()
    return found, None if found else "needs a g++ that is GCC"
