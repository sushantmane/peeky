"""Peeky builds with clang at DWARF 5, as projects build (ADR 0008), and reads DWARF 4 as it
did: `PEEKY_NATIVE_DWARF=4` keeps clang at 4. The same probes, built either way, read the
same values and withhold the same ones: statics (DWARF 5's address indexes on Linux),
nested blocks and C++'s code in several sections (range lists), a variable-length array
and an optimized build's locals (location lists)."""

import os
import re

import pytest
from native_compilers import default_cc, default_cxx, is_gcc

from peeky.native import NativeTarget
from peeky.probe import Probe, run_probe

C_SOURCE = """#include <stdio.h>
#include <string.h>

struct span { long start, end; };
long total_seen = 40;

__attribute__((noinline)) long tick(long x, int n) {
    static long calls = 0;
    static struct span st = {1, 2};
    long seen = x;
    char name[8];
    long widths[n];
    strcpy(name, "peeky");
    for (int i = 0; i < n; i++) widths[i] = i * x;
    calls += x;
    {
        long inner = seen + calls;
        total_seen += inner;
        printf("%ld %ld %ld %s %ld\\n", inner, st.end, widths[n - 1], name, total_seen); // STOP
    }
    return calls;
}

int main(void) {
    tick(3, 4);
    return 0;
}
"""
CPP_SOURCE = """#include <cstdio>
#include <string>
#include <vector>

template <typename T> struct Box {
    T held;
    T doubled() const {
        T twice = held + held;
        std::printf("%d\\n", (int)twice); // STOP
        return twice;
    }
};

inline int shared(int x) { return x + 1; }

int main() {
    std::vector<int> v{1, 2, 3};
    std::string s = "peeky";
    int base = shared(4);
    auto add = [&base, v](int x) { return base + x + (int)v.size(); };
    Box<int> box{add(1)};
    return box.doubled() - (int)s.size() == 0;
}
"""
OPTIMIZED = """#include <stdio.h>

__attribute__((noinline)) static int mix(int a, int b) {
    int t = a * 3 + b;
    printf("t %d\\n", t); // STOP
    int r = t ^ (b << 2);
    printf("mix %d\\n", r);
    return r;
}

int main(int argc, char **argv) {
    int total = 0;
    for (int i = 0; i < 3; i++) total += mix(i + argc, i * 2);
    printf("TOTAL %d\\n", total);
    return 0;
}
"""


def _target(language, name, optimization="O0"):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    compiler = default_cxx() if language == "cpp" else default_cc()
    if not compiler or is_gcc(compiler):
        pytest.skip("needs clang: GCC builds at DWARF 4 only")
    return NativeTarget(
        sources=(name,),
        language=language,
        compiler=compiler,
        adapter=os.environ["PEEKY_NATIVE_DAP"],
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
        optimization=optimization,
    )


def _read(tmp_path, monkeypatch, language, source, selectors, optimization="O0"):
    """Each selector's row, read from a build at DWARF 5 and at DWARF 4: what was read
    or withheld, and why."""
    name = "main.cpp" if language == "cpp" else "main.c"
    (tmp_path / name).write_text(source)
    line = next(n for n, text in enumerate(source.splitlines(), 1) if "// STOP" in text)
    found = {}
    for version in ("5", "4"):
        monkeypatch.setenv("PEEKY_NATIVE_DWARF", version)
        monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / f"state-{version}"))
        target = _target(language, name, optimization)
        result = run_probe(tmp_path, Probe(name, line, selectors), adapter=target)
        assert result["status"] == "completed", result.get("error")
        assert f"-gdwarf-{version}" in result["target_identity"]["compiler_options"]
        # Addresses differ between any two runs.
        found[version] = {
            row["selector"]: {
                key: re.sub(r"0x[0-9a-f]+", "0x…", str(row.get(key)))
                for key in ("value", "type", "error", "unavailable", "notes")
            }
            for row in result["rows"]
        }
    return found


@pytest.mark.timeout(300)
def test_c_reads_alike_at_dwarf_4_and_5(tmp_path, monkeypatch):
    selectors = ("calls", "st", "seen", "name", "widths", "inner", "x", "n")
    found = _read(tmp_path, monkeypatch, "c", C_SOURCE, selectors)
    assert found["5"] == found["4"]
    rows = found["5"]
    assert rows["calls"]["value"] == "3" and rows["inner"]["value"] == "6", rows
    assert rows["seen"]["value"] == "3" and rows["st"]["value"] == "{start = 1, end = 2}", rows
    assert rows["name"]["value"] == '"peeky" (then 3 NUL bytes)', rows


@pytest.mark.timeout(300)
def test_cpp_reads_alike_at_dwarf_4_and_5(tmp_path, monkeypatch):
    found = _read(tmp_path, monkeypatch, "cpp", CPP_SOURCE, ("twice", "held", "this"))
    assert found["5"] == found["4"]
    assert found["5"]["twice"]["value"] == "18", found


@pytest.mark.timeout(300)
def test_an_optimized_build_withholds_alike_at_dwarf_4_and_5(tmp_path, monkeypatch):
    found = _read(tmp_path, monkeypatch, "c", OPTIMIZED, ("a", "b", "t", "r"), "O2")
    assert found["5"] == found["4"]
    assert found["5"]["r"]["unavailable"] == "True", found


@pytest.mark.parametrize(
    "flags, said",
    [
        (("-gdwarf-5", "-gsplit-dwarf"), "split DWARF.*rebuild without -gsplit-dwarf"),
        (("-gdwarf-4", "-gsplit-dwarf"), "split DWARF.*rebuild without -gsplit-dwarf"),
        (("-gdwarf-5", "-fdebug-types-section"), "type units.*-fdebug-types-section"),
    ],
)
def test_units_held_elsewhere_are_refused_not_read_in_part(tmp_path, flags, said):
    # A split build's binary holds a skeleton of each unit, whose locals and types are in
    # its .dwo file; type units hold what units name by signature. Neither is read as the
    # part the binary holds.
    import subprocess
    import sys

    from peeky.native import ForeignUnits, read_debug_info

    if sys.platform != "linux":
        pytest.skip("split DWARF and type units are ELF's")
    compiler = default_cxx()
    if not compiler or is_gcc(compiler) or not os.environ.get("PEEKY_NATIVE_DWARFDUMP"):
        pytest.skip("needs clang and PEEKY_NATIVE_DWARFDUMP")
    (tmp_path / "main.cpp").write_text(CPP_SOURCE)
    subprocess.run(
        # clang picks C++ by the name it runs as: Peeky's pick of it may be `clang`.
        [compiler, "--driver-mode=g++", "-g", "-O0", *flags, "main.cpp", "-o", "program"],
        cwd=tmp_path,
        check=True,
    )
    binary = str(tmp_path / "program")
    with pytest.raises(ForeignUnits, match=said):
        read_debug_info(
            os.environ["PEEKY_NATIVE_DWARFDUMP"],
            [["--debug-info", binary], ["--debug-addr", binary]],
            tmp_path,
            dict(os.environ),
            timeout=15,
        )
