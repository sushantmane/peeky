"""A binary the project built (ADR 0008, phase 1), on real builds: each unit is judged by
its own debug information, frames in units Peeky doesn't judge show nothing, a
breakpoint in one is refused, a stale file refuses the binary by name, and GCC, DWARF 4,
a missing recorded command line, a libtool script and a missing build ID are refused
with what to rebuild with. Peeky writes nothing in the project's directory."""

import json
import os
import subprocess
import sys

import pytest
from native_compilers import default_cc, default_cxx, gcc, is_gcc

from peeky.cli import main
from peeky.native_binary import BinaryTarget
from peeky.probe import Probe, run_probe

MAIN = """#include <stdio.h>
#include "util.h"

int main(int argc, char **argv) {
    int base = argc + 1;
    int scaled = scale(base);
    int quick = fast(scaled);
    printf("%d %d %d\\n", base, scaled, quick);
    return 0;
}
"""
UTIL = """#include "util.h"

int scale(int x) {
    int doubled = x * 2;
    return doubled + 1; // STOP
}
"""
FAST = """#include "util.h"

int fast(int x) {
    int tripled = x * 3;
    return scale(tripled) - 1;
}
"""
HEADER = "int scale(int x);\nint fast(int x);\n"
FLAGS = ("-g", "-gdwarf-5", "-grecord-command-line")
STOP = next(n for n, text in enumerate(UTIL.splitlines(), 1) if "// STOP" in text)


def _clang():
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    compiler = default_cc()
    if not compiler or is_gcc(compiler):
        pytest.skip("needs clang: a binary target is judged by clang's checksums")
    return compiler


def _sysroot():
    sysroot = os.environ.get("PEEKY_NATIVE_SYSROOT")
    return ("-isysroot", sysroot) if sysroot and sys.platform == "darwin" else ()


def _build(root, compiler, levels, flags=FLAGS, link=()):
    """Builds src/main.c, src/util.c and src/fast.c in ``root``, as a project's Makefile
    would (each from the root, by its relative name), each at its ``levels`` level, into
    `prog`."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    for name, text in (("main.c", MAIN), ("util.c", UTIL), ("fast.c", FAST), ("util.h", HEADER)):
        (root / "src" / name).write_text(text)
    objects = []
    for name in ("main", "util", "fast"):
        level = levels.get(name, "-O0")
        subprocess.run(
            [compiler, *flags, level, *_sysroot(), "-c", f"src/{name}.c", "-o", f"{name}.o"],
            cwd=root,
            check=True,
        )
        objects.append(f"{name}.o")
    subprocess.run([compiler, *_sysroot(), *link, *objects, "-o", "prog"], cwd=root, check=True)


def _target(compiler, **given):
    return BinaryTarget(
        binary=given.pop("binary", "prog"),
        compiler=compiler,
        adapter=os.environ["PEEKY_NATIVE_DAP"],
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        **given,
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    return root


def _listing(root):
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


@pytest.mark.timeout(300)
def test_frames_of_a_unit_peeky_doesnt_judge_show_nothing(project):
    compiler = _clang()
    _build(project, compiler, {"fast": "-O2"})
    before = _listing(project)
    result = run_probe(
        project,
        Probe("src/util.c", STOP, ("doubled", "1:tripled", "1:x"), hits=2),
        adapter=_target(compiler),
    )
    assert result["status"] == "completed", result.get("error")
    rows = {(row["hit"], row["selector"]): row for row in result["rows"]}
    # Hit 2 is the call from fast(), built at -O2: its frame shows nothing, and says why.
    for selector in ("1:tripled", "1:x"):
        row = rows[(2, selector)]
        assert (row.get("error") or "").startswith(
            "not shown: this frame runs src/fast.c, a unit Peeky doesn't judge: built with -O2 "
            "(its recorded flags)"
        ), row
    # The judged frame follows today's rules: without its unit's text, what needs it
    # withholds, saying so.
    assert "Peeky can't read src/util.c as compiled" in (rows[(1, "doubled")].get("error") or "")
    assert result["target_identity"]["optimization"] == "O0"
    assert result["target_identity"]["units"] == {"judged": 2, "optimized": 0, "not judged": 1}
    assert "2 5 30" in result.get("output", "")
    # Peeky ran the binary where it is, and wrote nothing beside it.
    assert _listing(project) == before


@pytest.mark.timeout(300)
def test_optimized_units_are_read_as_an_optimized_build_when_opted_in(project):
    compiler = _clang()
    _build(project, compiler, {"fast": "-O2"})
    result = run_probe(
        project,
        Probe("src/util.c", STOP, ("1:tripled",), hits=2),
        adapter=_target(compiler, optimized=True),
    )
    assert result["status"] == "completed", result.get("error")
    row = next(row for row in result["rows"] if row["hit"] == 2)
    assert not (row.get("error") or "").startswith("not shown"), row
    assert result["target_identity"]["optimization"] == "O2"
    assert any("Optimized build (-O2)" in warning for warning in result.get("warnings", ()))


@pytest.mark.timeout(300)
def test_a_breakpoint_where_no_judged_unit_has_code_is_refused(project):
    compiler = _clang()
    _build(project, compiler, {"fast": "-O2"})
    for program, said in (
        ("src/fast.c", "src/fast.c is a unit Peeky doesn't judge: built with -O2 (its recorded"),
        ("src/util.h", "No unit of prog Peeky judges has code in src/util.h"),
    ):
        result = run_probe(project, Probe(program, 4, ("x",)), adapter=_target(compiler))
        error = result.get("error") or {}
        assert error.get("code") == "UNSUPPORTED" and said in error["message"], result
        assert result.get("refused_before_launch") is True and not result.get("output")


@pytest.mark.timeout(300)
def test_describe_lists_each_unit_judged_or_not_and_why(project, capsys):
    compiler = _clang()
    _build(project, compiler, {"fast": "-O2"})
    config = project.parent / "binary.json"
    config.write_text(
        json.dumps(
            {
                "binary": "prog",
                "compiler": compiler,
                "adapter": os.environ["PEEKY_NATIVE_DAP"],
                "dwarfdump": os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
                "dsymutil": os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
            }
        )
    )
    assert main(["describe", str(project), "--config", str(config), "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    units = {unit["unit"]: unit for unit in manifest["units"]}
    assert units["src/main.c"]["judged"] and units["src/util.c"]["judged"], units
    assert not units["src/fast.c"]["judged"], units
    assert units["src/fast.c"]["why"].startswith("built with -O2 (its recorded flags)")
    assert set(manifest["breakpoint_lines"]) == {"src/main.c", "src/util.c"}
    assert manifest["language"] == "c" and manifest["target_identity"]["binary"] == "prog"
    assert "--at <file>:<line>" in manifest["commands"]["probe"]
    assert "session" not in manifest["commands"] and "verify" not in manifest["commands"]


@pytest.mark.timeout(300)
def test_a_file_changed_since_the_build_refuses_the_binary_by_name(project):
    compiler = _clang()
    _build(project, compiler, {})
    with (project / "src" / "util.c").open("a") as source:
        source.write("// edited, not rebuilt\n")
    result = run_probe(project, Probe("src/main.c", 6, ("base",)), adapter=_target(compiler))
    error = result.get("error") or {}
    assert error.get("code") == "PROVENANCE_CHANGED", result
    assert "the binary is stale: src/util.c changed" in error["message"], error
    assert result.get("refused_before_launch") is True and not result.get("output")


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "flags, said",
    [
        (("-g", "-gdwarf-4", "-grecord-command-line"), "DWARF 4, which records no checksum"),
        (("-g", "-gdwarf-5"), "records no command line: rebuild with -grecord-command-line"),
        (FLAGS + ("-ffast-math",), "built with -ffast-math, under which Peeky's proofs"),
    ],
)
def test_units_peeky_cant_judge_are_refused_with_what_to_rebuild_with(project, flags, said):
    compiler = _clang()
    if sys.platform == "darwin" and "-gdwarf-4" in flags:
        pytest.skip("dsymutil writes DWARF 4 units as DWARF 4 too; Linux checks it")
    _build(project, compiler, {}, flags=flags)
    result = run_probe(project, Probe("src/util.c", STOP, ("x",)), adapter=_target(compiler))
    error = result.get("error") or {}
    assert error.get("code") == "UNSUPPORTED", result
    assert (
        error["message"].startswith("Peeky judges no unit of prog: ") and said in error["message"]
    ), error
    assert result.get("refused_before_launch") is True


@pytest.mark.timeout(300)
def test_a_gcc_binary_is_refused_with_the_flags_to_rebuild_with_clang(project):
    compiler = gcc()
    if compiler is None or not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("needs GCC and native tools")
    _build(project, compiler, {}, flags=("-g", "-gdwarf-5"))
    result = run_probe(
        project, Probe("src/util.c", STOP, ("x",)), adapter=_target(default_cc() or "clang")
    )
    error = result.get("error") or {}
    assert error.get("code") == "UNSUPPORTED", result
    assert (
        "built by GCC, which writes no checksum of its files: rebuild with clang"
        in (error["message"])
    ), error


@pytest.mark.timeout(300)
def test_what_isnt_a_binary_peeky_can_read_is_refused(project):
    compiler = _clang()
    _build(project, compiler, {})
    wrapper = project / "jq"
    wrapper.write_text('#!/bin/sh\nexec ./prog "$@"\n')
    wrapper.chmod(0o755)
    result = run_probe(
        project, Probe("src/util.c", STOP, ("x",)), adapter=_target(compiler, binary="jq")
    )
    error = result.get("error") or {}
    assert (
        error.get("code") == "UNSUPPORTED"
        and "jq is a script, not an executable" in (error["message"])
    ), result
    assert ".libs/jq" in error["message"] and "--disable-shared" in error["message"]
    outside = project.parent / "elsewhere"
    outside.mkdir()
    (outside / "prog").write_bytes((project / "prog").read_bytes())
    result = run_probe(
        project,
        Probe("src/util.c", STOP, ("x",)),
        adapter=_target(compiler, binary="../elsewhere/prog"),
    )
    assert (result.get("error") or {}).get("code") == "INVALID_REQUEST", result


@pytest.mark.timeout(300)
def test_a_binary_without_a_build_id_is_refused(project):
    compiler = _clang()
    if sys.platform != "linux":
        pytest.skip("an ELF build ID; Mach-O's UUID is the linker's default")
    _build(project, compiler, {}, link=("-Wl,--build-id=none",))
    result = run_probe(project, Probe("src/util.c", STOP, ("x",)), adapter=_target(compiler))
    error = result.get("error") or {}
    assert error.get("code") == "UNSUPPORTED" and "has no build ID" in error["message"], result
    assert "-Wl,--build-id" in error["message"]


ICF = """#include <stdio.h>

__attribute__((noinline)) int add_one(int x) {
    int y = x + 1;
    return y;
}

__attribute__((noinline)) int plus_one(int z) {
    int w = z + 1;
    return w; // FOLDED
}

int main(void) {
    printf("%d %d\\n", add_one(1), plus_one(2));
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_a_c_frame_at_code_two_functions_share_shows_nothing(project):
    # Identical code folding is a link option no debug information records: the frame
    # may run either function, whose declarations differ.
    compiler = _clang()
    if sys.platform != "linux":
        pytest.skip("gold's --icf=all")
    (project / "icf.c").write_text(ICF)
    flags = (*FLAGS, "-O0", "-ffunction-sections")
    subprocess.run([compiler, *flags, "-c", "icf.c", "-o", "icf.o"], cwd=project, check=True)
    subprocess.run(
        [compiler, "-fuse-ld=gold", "-Wl,--icf=all", "-Wl,--build-id", "icf.o", "-o", "prog"],
        cwd=project,
        check=True,
    )
    line = next(n for n, text in enumerate(ICF.splitlines(), 1) if "// FOLDED" in text)
    result = run_probe(
        project, Probe("icf.c", line, ("w", "z", "x", "y")), adapter=_target(compiler)
    )
    if result["status"] != "completed":
        pytest.fail(str(result.get("error")))
    rows = {row["selector"]: row for row in result["rows"]}
    # LLDB shows the stop on plus_one's line, with add_one's locals: neither is shown.
    assert rows["w"]["error"].startswith("not found; visible: x, y"), rows
    for name in ("x", "y"):
        assert (rows[name].get("error") or "").startswith("not shown: "), rows[name]
        assert (
            "2 functions share the code this frame runs (identical code folding"
            in (rows[name]["error"])
        ), rows[name]


CPP_INHERITS = """struct Base {
    explicit Base(int v) : value(v) {}
    int value;
};

struct Derived : Base {
    using Base::Base;
};

int read(const Derived &d) {
    int got = d.value;
    return got;
}

int main() {
    Derived d(4);
    return read(d) - 4;
}
"""
CPP_PLAIN = """struct Box {
    int held;
    int twice() const {
        int doubled = held * 2;
        return doubled; // STOP
    }
};

int main() {
    Box box{21};
    return box.twice() - 42;
}
"""


@pytest.mark.cpp
@pytest.mark.timeout(300)
def test_cpp_units_are_judged_by_their_md5s_and_refused_with_what_the_table_shows(project):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    compiler = default_cxx()
    if not compiler or is_gcc(compiler):
        pytest.skip("needs clang++")
    (project / "plain.cpp").write_text(CPP_PLAIN)
    (project / "inherits.cpp").write_text(CPP_INHERITS.replace("int main()", "int other()"))
    for name in ("plain", "inherits"):
        subprocess.run(
            # A .cpp file is C++ to clang whatever name it runs as.
            [compiler, *FLAGS, "-O0", *_sysroot(), "-c", f"{name}.cpp"],
            cwd=project,
            check=True,
        )
    subprocess.run(
        [compiler, "--driver-mode=g++", *_sysroot(), "plain.o", "inherits.o", "-o", "prog"],
        cwd=project,
        check=True,
    )
    target = _target(compiler)
    line = next(n for n, text in enumerate(CPP_PLAIN.splitlines(), 1) if "// STOP" in text)
    result = run_probe(project, Probe("plain.cpp", line, ("held",)), adapter=target)
    assert result["status"] == "completed", result.get("error")
    assert result["target_identity"]["language"] == "cpp"
    refused = run_probe(project, Probe("inherits.cpp", 11, ("got",)), adapter=target)
    error = refused.get("error") or {}
    assert error.get("code") == "UNSUPPORTED", refused
    assert (
        "its line table records no MD5s, and names <stdin>, which clang adds for an "
        "inheriting constructor" in error["message"]
    ), error


@pytest.mark.timeout(120)
def test_a_binary_config_with_sources_or_a_session_is_refused(project, capsys):
    config = project / "binary.json"
    config.write_text(json.dumps({"binary": "prog", "sources": ["src/main.c"]}))
    assert main(["probe", str(project), "--config", str(config), "--at", "src/main.c:5"]) != 0
    said = capsys.readouterr()
    assert "INVALID_REQUEST" in said.out + said.err
    assert "not sources: its sources are the files its debug information names" in (
        said.out + said.err
    )
    config.write_text(json.dumps({"binary": "prog"}))
    assert (
        main(["session", "start", str(project), "--config", str(config), "--at", "src/main.c:5"])
        != 0
    )
    said = capsys.readouterr()
    assert "INVALID_REQUEST" in said.out + said.err and "sessions come with" in said.out + said.err
