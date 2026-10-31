"""A binary the project built (ADR 0008, phase 1), on real builds: each unit is judged by
its own debug information, frames in units Peeky doesn't judge show nothing, a
breakpoint in one is refused, a stale file refuses the binary by name, and GCC, DWARF 4,
a missing recorded command line, a libtool script and a missing build ID are refused
with what to rebuild with. Peeky writes nothing in the project's directory."""

import json
import os
import subprocess
import sys
import time

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
def test_describe_lists_each_unit_judged_or_not_and_why(project, capsys, monkeypatch):
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
    # `--binary` names it relative to the target directory, wherever the command runs.
    elsewhere = project.parent / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert main(["describe", str(project), "--binary", "./prog", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["units"] == manifest["units"]
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
@pytest.mark.parametrize("archived", [False, True], ids=["object", "archive member"])
def test_an_object_rebuilt_since_the_link_refuses_the_binary_on_macos(project, archived):
    # dsymutil reads the debug information of the objects the binary's debug map names, as
    # they are now: one rebuilt without a relink would give the old binary the new
    # source's line tables, MD5s and all. The map records each object's time in seconds,
    # and the linker wrote the binary after reading them: one rebuilt after the link, in
    # the same second, is newer than the binary.
    compiler = _clang()
    if sys.platform != "darwin":
        pytest.skip("a Mach-O debug map; Linux keeps the debug information in the binary")
    _build(project, compiler, {})
    second = int(time.time()) - 100
    at = lambda fraction: second * 10**9 + fraction * 10**8  # noqa: E731
    for name in ("main.o", "util.o", "fast.o"):
        os.utime(project / name, ns=(at(1), at(1)))
    objects = ["main.o", "util.o", "fast.o"]
    if archived:
        subprocess.run(["ar", "rcs", "libutil.a", "util.o"], cwd=project, check=True)
        os.utime(project / "libutil.a", ns=(at(2), at(2)))
        objects[1] = "libutil.a"
    subprocess.run([compiler, *_sysroot(), *objects, "-o", "prog"], cwd=project, check=True)
    os.utime(project / "prog", ns=(at(5), at(5)))
    target = _target(compiler)

    def probe():
        return run_probe(project, Probe("src/util.c", STOP, ("x",)), adapter=target)

    assert probe()["status"] == "completed"
    # A binary touched (or copied) later is still newer than its objects.
    os.utime(project / "prog", None)
    assert probe()["status"] == "completed"
    os.utime(project / "prog", ns=(at(5), at(5)))
    (project / "src" / "util.c").write_text(UTIL.replace("x * 2", "x * 4"))
    subprocess.run(
        [compiler, *FLAGS, "-O0", *_sysroot(), "-c", "src/util.c", "-o", "util.o"],
        cwd=project,
        check=True,
    )
    # Rebuilt in the second the debug map records, after the link.
    os.utime(project / "util.o", ns=(at(9), at(9)))
    named = "util.o"
    if archived:
        subprocess.run(["ar", "rcs", "libutil.a", "util.o"], cwd=project, check=True)
        os.utime(project / "libutil.a", ns=(at(9), at(9)))
        named = "libutil.a"
    result = probe()
    error = result.get("error") or {}
    assert error.get("code") == "PROVENANCE_CHANGED", result
    assert (
        f"the binary is stale: {named} is newer than prog: rebuilt after the link?"
        in (error["message"])
    ), error
    assert result.get("refused_before_launch") is True and not result.get("output")


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "flags, said",
    [
        (("-g", "-gdwarf-4", "-grecord-command-line"), "DWARF 4, which records no checksum"),
        (("-g", "-gdwarf-5"), "records no command line: rebuild with -grecord-command-line"),
        (FLAGS + ("-ffast-math",), "built with -ffast-math, under which Peeky's proofs"),
        (FLAGS + ("-ffp-model=fast",), "built with -ffp-model=fast, which implies -ffast-math's"),
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


# A program whose units include nothing: Apple's clang can't name a header of declarations
# alone in a DWARF 5 line table (-fdebug-macro crashes it), so a unit including one has no
# text Peeky reads there. On Linux, clang includes stdc-predef.h in every unit: there,
# -fdebug-macro names it.
SELF_MAIN = """int scale(int x);
int fast(int x);
int printf(const char *, ...);

int main(int argc, char **argv) {
    int base = argc + 1;
    int scaled = scale(base);
    int quick = fast(scaled);
    printf("%d %d %d\\n", base, scaled, quick);
    return 0;
}
"""
SELF_UTIL = """int scale(int x) {
    int doubled = x * 2;
    return doubled + 1; // STOP
}
"""
SELF_FAST = """int scale(int x);

int fast(int x) {
    int tripled = x * 3;
    return scale(tripled) - 1;
}
"""
TEXT_FLAGS = (*FLAGS, *(("-fdebug-macro",) if sys.platform == "linux" else ()))
SELF_STOP = next(n for n, text in enumerate(SELF_UTIL.splitlines(), 1) if "// STOP" in text)


def _build_text(root, compiler, levels, extra=None, util=SELF_UTIL):
    """As _build, of units that include nothing, with -fdebug-macro on Linux;
    ``extra``: {unit: flags} added to its compile."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    texts = {"main": SELF_MAIN, "util": util, "fast": SELF_FAST}
    for name, text in texts.items():
        (root / "src" / f"{name}.c").write_text(text)
        flags = (*TEXT_FLAGS, levels.get(name, "-O0"), *(extra or {}).get(name, ()))
        subprocess.run(
            [compiler, *flags, *_sysroot(), "-c", f"src/{name}.c", "-o", f"{name}.o"],
            cwd=root,
            check=True,
        )
    objects = [f"{name}.o" for name in texts]
    subprocess.run([compiler, *_sysroot(), *objects, "-o", "prog"], cwd=root, check=True)


@pytest.mark.timeout(300)
def test_a_judged_unit_s_values_are_read_by_its_replayed_text(project):
    compiler = _clang()
    _build_text(project, compiler, {})
    result = run_probe(
        project,
        Probe("src/util.c", SELF_STOP, ("doubled", "x", "1:base", "1:scaled")),
        adapter=_target(compiler, returns_twice=()),
    )
    assert result["status"] == "completed", result.get("error")
    rows = {row["selector"]: row for row in result["rows"]}
    # As the program prints them: base 2, and scale(2) is doubled + 1, 5.
    assert rows["doubled"].get("value") == "4", rows
    assert rows["x"].get("value") == "2" and rows["1:base"].get("value") == "2", rows
    assert rows["1:scaled"]["error"].startswith("no value yet"), rows
    assert result["output"].strip() == "2 5 30"


@pytest.mark.timeout(300)
def test_a_unit_whose_text_cant_be_replayed_withholds_its_text_rules_saying_why(project):
    compiler = _clang()
    _build_text(project, compiler, {}, extra={"util": ("-Wp,-DUNUSED=1",)})
    result = run_probe(
        project, Probe("src/util.c", SELF_STOP, ("doubled",)), adapter=_target(compiler)
    )
    assert result["status"] == "completed", result.get("error")
    (row,) = result["rows"]
    assert "Peeky can't read src/util.c as compiled: its command uses -Wp,-DUNUSED=1, which " in (
        row.get("error") or ""
    ), row


@pytest.mark.timeout(300)
def test_a_header_its_line_table_doesnt_name_leaves_a_unit_s_text_unread(project, capsys):
    # A header of declarations alone, without -fdebug-macro: no code or type of the unit
    # comes from it. `describe` says why each unit's text isn't read.
    compiler = _clang()
    _build(project, compiler, {})
    assert main(["describe", str(project), "--binary", "prog", "--json"]) == 0
    units = {unit["unit"]: unit for unit in json.loads(capsys.readouterr().out)["units"]}
    assert units["src/util.c"]["text"] == (
        "Peeky can't read src/util.c as compiled: its preprocessing reads src/util.h, which its "
        "line table doesn't name (-fdebug-macro names each header, on Linux)"
    ), units
    result = run_probe(project, Probe("src/util.c", STOP, ("doubled",)), adapter=_target(compiler))
    assert result["status"] == "completed", result.get("error")
    (row,) = result["rows"]
    assert "Peeky can't read src/util.c as compiled: its preprocessing reads " in row["error"]


ON_ITS_LINE = next(n for n, text in enumerate(SELF_UTIL.splitlines(), 1) if "doubled =" in text)


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "levels, util, said",
    [
        ({"fast": "-O2"}, SELF_UTIL, "src/fast.c is a unit Peeky doesn't read"),
        (
            {},
            SELF_UTIL.replace("    int doubled", '    __asm__ volatile("");\n    int doubled'),
            "src/util.c's text holds asm",
        ),
    ],
)
def test_any_call_may_return_twice_unless_the_config_names_those_that_do(
    project, levels, util, said
):
    # The program's own assembly can return twice (MicroPython's nlr_push): where a unit
    # isn't read, or its text holds asm, a local declared at the stop says so.
    compiler = _clang()
    _build_text(project, compiler, levels, util=util)
    line = next(n for n, text in enumerate(util.splitlines(), 1) if "doubled =" in text)
    found = {}
    for claim in (None, ()):
        result = run_probe(
            project,
            Probe("src/util.c", line, ("doubled",)),
            adapter=_target(compiler, returns_twice=claim),
        )
        assert result["status"] == "completed", result.get("error")
        (row,) = result["rows"]
        found[claim] = row
    # Shown, as today's setjmp note shows a local: maybe leftovers, or its value from
    # before a `longjmp`; and never changed.
    noted = found[None].get("note") or ""
    assert f"any call above the stop may return twice ({said}" in noted, found
    assert '"returns_twice" can name the functions that do' in noted
    # Named by the config (none, here, but setjmp's), no call returns twice.
    error = found[()].get("error") or ""
    assert "return twice" not in str(found[()]) and error.startswith("no value yet"), found


SYSTEM_MAIN = """#include <stdio.h>
#include <string.h>
#include "util.h"

int main(int argc, char **argv) {
    int base = argc + 1;
    int scaled = scale(base);
    printf("%d %d %zu\\n", base, scaled, strlen("abc")); // PRINT
    return 0;
}
"""
MACRO_HEADER = "#define TWICE(x) ((x) * 2)\nint scale(int x);\n"
MACRO_UTIL = """#include "util.h"

int scale(int x) {
    int doubled = TWICE(x);
    return doubled + 1; // STOP
}
"""


@pytest.mark.timeout(300)
@pytest.mark.parametrize("macros", [True, False], ids=["debug-macro", "no-debug-macro"])
def test_system_headers_and_a_header_of_macros_alone_are_read_on_linux(project, macros):
    # With -fdebug-macro, a unit's line table names every header it reads, with its MD5:
    # stdio.h's, and a header of macros and declarations alone. Without it, no unit
    # naming one is read.
    compiler = _clang()
    if sys.platform != "linux":
        pytest.skip("Apple's clang crashes with -fdebug-macro and DWARF 5")
    (project / "src").mkdir()
    (project / "src" / "main.c").write_text(SYSTEM_MAIN)
    (project / "src" / "util.c").write_text(MACRO_UTIL)
    (project / "src" / "util.h").write_text(MACRO_HEADER)
    flags = (*FLAGS, "-O0", *(("-fdebug-macro",) if macros else ()))
    for name in ("main", "util"):
        subprocess.run(
            [compiler, *flags, "-c", f"src/{name}.c", "-o", f"{name}.o"], cwd=project, check=True
        )
    subprocess.run([compiler, "main.o", "util.o", "-o", "prog"], cwd=project, check=True)
    stop = next(n for n, text in enumerate(MACRO_UTIL.splitlines(), 1) if "// STOP" in text)
    result = run_probe(
        project,
        Probe("src/util.c", stop, ("doubled", "x", "1:base")),
        adapter=_target(compiler, returns_twice=()),
    )
    assert result["status"] == "completed", result.get("error")
    rows = {row["selector"]: row for row in result["rows"]}
    if macros:
        # As the program prints them: base 2, and scaled = doubled + 1 = 5.
        assert result["output"].split()[:2] == ["2", "5"]
        assert [rows[name].get("value") for name in ("doubled", "x", "1:base")] == ["4", "2", "2"]
    else:
        # A row's reason is cut at 300 characters; `describe` gives it whole.
        assert "Peeky can't read src/util.c as compiled: its preprocessing reads src/util.h" in (
            rows["doubled"].get("error") or ""
        ), rows


ASM_CALLER = """int nlr_push(void);

int main(void) {
    int got = nlr_push();
    int later = got + 1; // STOP
    return later;
}
"""


def _assembly(private=False, function="nlr_push"):
    """A function in hand-written assembly, returning 0, for this host; ``private``: one
    the linker keeps out of the program's symbols (macOS's `.private_extern` and `-x`)."""
    import platform

    name = f"_{function}" if sys.platform == "darwin" else function
    body = "xor %eax, %eax" if platform.machine() in ("x86_64", "AMD64") else "mov w0, #0"
    hidden = f"    .private_extern {name}\n" if private else ""
    return f"    .text\n    .globl {name}\n{hidden}{name}:\n    {body}\n    ret\n"


@pytest.mark.timeout(300)
@pytest.mark.parametrize("stripped", [False, True], ids=["named", "symbol stripped"])
def test_code_no_unit_covers_may_return_twice(project, capsys, stripped):
    # Hand-written assembly built without -g has no unit: Peeky can't read it, and it may
    # return twice (MicroPython's nlr_push, a setjmp of the project's own). Found by its
    # symbol, or, where the symbol table lacks one, as code no symbol names.
    compiler = _clang()
    (project / "main.c").write_text(ASM_CALLER)
    macos = sys.platform == "darwin"
    (project / "nlr.s").write_text(_assembly(private=stripped and macos))
    flags = (*TEXT_FLAGS, "-O0", *_sysroot())
    subprocess.run([compiler, *flags, "-c", "main.c", "-o", "main.o"], cwd=project, check=True)
    subprocess.run([compiler, *_sysroot(), "-c", "nlr.s", "-o", "nlr.o"], cwd=project, check=True)
    # macOS's strip drops the debug map too: its linker's -x drops the private symbol alone.
    link = ("-Wl,-x",) if stripped and macos else ()
    subprocess.run(
        [compiler, *_sysroot(), *link, "main.o", "nlr.o", "-o", "prog"], cwd=project, check=True
    )
    said = "nlr_push is code no unit's debug information covers (assembly built without -g?)"
    if stripped:
        if not macos:
            subprocess.run(["objcopy", "--strip-symbol=nlr_push", "prog"], cwd=project, check=True)
        said = "has no symbol, and no unit's debug information covers it"
    assert main(["describe", str(project), "--binary", "prog", "--json"]) == 0
    twice = json.loads(capsys.readouterr().out)["returns_twice"]
    assert twice["any"] is True and said in twice["why"], twice
    line = next(n for n, text in enumerate(ASM_CALLER.splitlines(), 1) if "// STOP" in text)
    notes = {}
    for claim in (None, ("nlr_push",), ()):
        result = run_probe(
            project,
            Probe("main.c", line, ("later",)),
            adapter=_target(compiler, returns_twice=claim),
        )
        assert result["status"] == "completed", result.get("error")
        (row,) = result["rows"]
        notes[claim] = row.get("note") or row.get("error") or ""
    assert "any call above the stop may return twice (" in notes[None] and said in notes[None]
    # The config's claim: nlr_push, called on the line above, returns twice.
    assert (
        'returns twice, or one the config\'s "returns_twice" names (line 4)' in notes[("nlr_push",)]
    ), notes
    assert "return twice" not in notes[()] and notes[()].startswith("no value yet"), notes


VFORK_UTIL = """int vfork(void);
void _exit(int);

int scale(int x) {
    if (x > 100 && vfork() == 0)
        _exit(0);
    int doubled = x * 2;
    return doubled + 1; // STOP
}
"""


@pytest.mark.timeout(300)
def test_a_vfork_above_the_stop_notes_a_local_declared_below_it(project):
    # vfork returns twice, as setjmp does: Peeky's names left it out, so a program
    # calling only it had no note.
    compiler = _clang()
    _build_text(project, compiler, {}, util=VFORK_UTIL)
    line = next(n for n, text in enumerate(VFORK_UTIL.splitlines(), 1) if "doubled =" in text)
    result = run_probe(project, Probe("src/util.c", line, ("doubled",)), adapter=_target(compiler))
    assert result["status"] == "completed", result.get("error")
    (row,) = result["rows"]
    assert "calls setjmp, or another function that returns twice (line 5)" in (
        row.get("note") or ""
    ), row


SPIN = """static inline int spin(int x) {
    __asm__ volatile("" ::: "memory");
    return x;
}
"""
SPIN_UTIL = """#include "spin.h"

int scale(int x) {
    int doubled = spin(x) * 2;
    return doubled + 1; // STOP
}
"""


@pytest.mark.timeout(300)
def test_asm_in_a_header_outside_the_program_may_return_twice(project, capsys):
    # Peeky looked for asm only in the program's own files: a header outside them, its
    # MD5 checked, hid it.
    ext = project.parent / f"{project.name}-ext"
    ext.mkdir()
    (ext / "spin.h").write_text(SPIN)
    compiler = _clang()
    _build_text(project, compiler, {}, extra={"util": ("-I", str(ext))}, util=SPIN_UTIL)
    assert main(["describe", str(project), "--binary", "prog", "--json"]) == 0
    twice = json.loads(capsys.readouterr().out)["returns_twice"]
    assert twice["any"] and twice["why"].startswith("src/util.c's text holds asm ("), twice
    assert twice["why"].endswith("spin.h:2)"), twice


PROBED_UTIL = """#if __has_include("count.h")
#include "count.h"
#else
typedef int count_t;
#endif

int scale(int x) {
    count_t doubled = x * 2;
    return doubled + 1; // STOP
}
"""


@pytest.mark.timeout(300)
@pytest.mark.parametrize("present", [True, False], ids=["found", "not found"])
def test_a_probe_the_build_may_have_answered_otherwise_withholds_the_text(project, capsys, present):
    # `__has_include` may have found a file when the program was built (a header path
    # from its environment, a file since gone) that the replay doesn't.
    (project / "src").mkdir()
    if present:
        (project / "src" / "count.h").write_text("typedef int count_t;\n")
    compiler = _clang()
    _build_text(project, compiler, {}, util=PROBED_UTIL)
    stop = next(n for n, text in enumerate(PROBED_UTIL.splitlines(), 1) if "// STOP" in text)
    result = run_probe(project, Probe("src/util.c", stop, ("doubled",)), adapter=_target(compiler))
    assert result["status"] == "completed", result.get("error")
    (row,) = result["rows"]
    if present:
        assert row.get("value") == "4", row
    else:
        assert (
            "Peeky can't read src/util.c as compiled: its preprocessing asks whether "
            '"count.h" exists'
        ) in (row.get("error") or ""), row
        assert main(["describe", str(project), "--binary", "prog", "--json"]) == 0
        units = {unit["unit"]: unit for unit in json.loads(capsys.readouterr().out)["units"]}
        assert units["src/util.c"]["text"] == (
            "Peeky can't read src/util.c as compiled: its preprocessing asks whether "
            '"count.h" exists (__has_include, in src/util.c:1), and finds none: the build may '
            "have found one (its environment can add header paths, and a file may since be gone)"
        ), units


ATOMIC_UTIL = """int scale(int x) {
    static int calls;
    __atomic_fetch_add(&calls, 1, __ATOMIC_SEQ_CST);
    int doubled = x * 2;
    return doubled + 1; // STOP
}
"""


def _describe_twice(project, capsys):
    assert main(["describe", str(project), "--binary", "prog", "--json"]) == 0
    return json.loads(capsys.readouterr().out)["returns_twice"]


@pytest.mark.timeout(300)
@pytest.mark.parametrize("link", [(), ("-no-pie",)], ids=["default", "no-pie"])
def test_a_normal_build_has_no_code_no_unit_covers(project, capsys, link):
    # The toolchain's startup code (crt1.o, crti.o, crtn.o, crtbegin.o, libgcc's atomics)
    # is proven its own, by name where the name is reserved and by its place elsewhere.
    if link and sys.platform != "linux":
        pytest.skip("-no-pie is a Linux link")
    compiler = _clang()
    _build_text(project, compiler, {}, util=ATOMIC_UTIL)
    objects = ["main.o", "util.o", "fast.o"]
    subprocess.run([compiler, *_sysroot(), *link, *objects, "-o", "prog"], cwd=project, check=True)
    twice = _describe_twice(project, capsys)
    assert twice["any"] is False, twice


# Calls into the toolchain's static archives: glibc's libc_nonshared.a (atexit) and, on
# AArch64, libgcc's long double arithmetic (__extenddftf2, __gttf2).
ARCHIVE_UTIL = """int atexit(void (*)(void));

static void done(void) {}

int scale(int x) {
    long double wide = (double)x;
    atexit(done);
    int doubled = x * 2;
    return doubled + (wide > 1e9L) + 1;
}
"""


@pytest.mark.timeout(300)
@pytest.mark.skipif(sys.platform != "linux", reason="glibc's libc_nonshared.a and libgcc's archive")
def test_code_linked_from_the_toolchain_s_archives_is_the_toolchain_s(project, capsys):
    compiler = _clang()
    _build_text(project, compiler, {}, util=ARCHIVE_UTIL)
    twice = _describe_twice(project, capsys)
    assert twice["any"] is False, twice


@pytest.mark.timeout(300)
@pytest.mark.skipif(sys.platform != "linux", reason="glibc's libc_nonshared.a")
def test_a_program_s_own_function_named_like_an_archive_member_counts(project, capsys):
    # The program's own atexit, without debug information: libc_nonshared.a's isn't
    # linked, and its bytes are other.
    compiler = _clang()
    _build_text(project, compiler, {}, util=ARCHIVE_UTIL)
    (project / "own.c").write_text("int atexit(void (*f)(void)) { return f == 0; }\n")
    subprocess.run([compiler, "-O0", "-c", "own.c", "-o", "own.o"], cwd=project, check=True)
    objects = ["main.o", "util.o", "fast.o", "own.o"]
    subprocess.run([compiler, *objects, "-o", "prog"], cwd=project, check=True)
    twice = _describe_twice(project, capsys)
    assert (
        twice["any"]
        and (
            "atexit is code no unit's debug information covers, and its bytes are none of the "
            "toolchain's (libc_nonshared.a(atexit.oS))"
        )
        in twice["why"]
    ), twice


@pytest.mark.timeout(300)
def test_a_program_s_own_frame_dummy_without_debug_information_counts(project, capsys):
    # frame_dummy isn't reserved: crtbegin.o's is local and called by .init_array; one
    # the program defines is neither.
    compiler = _clang()
    _build_text(project, compiler, {})
    (project / "dummy.s").write_text(_assembly(function="frame_dummy"))
    subprocess.run(
        [compiler, *_sysroot(), "-c", "dummy.s", "-o", "dummy.o"], cwd=project, check=True
    )
    objects = ["main.o", "util.o", "fast.o", "dummy.o"]
    subprocess.run([compiler, *_sysroot(), *objects, "-o", "prog"], cwd=project, check=True)
    twice = _describe_twice(project, capsys)
    assert twice["any"] and twice["why"].startswith(
        "frame_dummy is code no unit's debug information covers"
    ), twice


@pytest.mark.timeout(300)
@pytest.mark.skipif(sys.platform != "linux", reason="Mach-O has no .init")
def test_code_an_object_adds_to_init_counts(project, capsys):
    # .init runs what every object puts there, between crti.o's head and crtn.o's tail.
    import platform

    compiler = _clang()
    _build_text(project, compiler, {})
    x86 = platform.machine() in ("x86_64", "AMD64")
    word = "mov %rax, %rax" if x86 else "mov x9, x9"
    kind = "@progbits" if x86 else "%progbits"
    (project / "hook.s").write_text(f'    .section .init,"ax",{kind}\n    {word}\n')
    subprocess.run([compiler, "-c", "hook.s", "-o", "hook.o"], cwd=project, check=True)
    objects = ["main.o", "util.o", "fast.o", "hook.o"]
    subprocess.run([compiler, *objects, "-o", "prog"], cwd=project, check=True)
    twice = _describe_twice(project, capsys)
    assert (
        twice["any"]
        and "in .init, may be the program's: it isn't crti.o's or crtn.o's" in (twice["why"])
    ), twice


@pytest.mark.timeout(300)
def test_a_program_s_local_frame_dummy_called_at_start_still_counts(project, capsys):
    # A static constructor named frame_dummy is local and called by .init_array, as
    # crtbegin.o's is; only its bytes tell them apart.
    compiler = _clang()
    _build_text(project, compiler, {})
    (project / "dummy.c").write_text(
        "static int ran;\n__attribute__((constructor)) static void frame_dummy(void) { ran = 1; }\n"
    )
    subprocess.run(
        [compiler, *_sysroot(), "-O0", "-c", "dummy.c", "-o", "dummy.o"], cwd=project, check=True
    )
    objects = ["main.o", "util.o", "fast.o", "dummy.o"]
    subprocess.run([compiler, *_sysroot(), *objects, "-o", "prog"], cwd=project, check=True)
    twice = _describe_twice(project, capsys)
    assert (
        twice["any"] and "frame_dummy is code no unit's debug information covers" in (twice["why"])
    ), twice


@pytest.mark.timeout(300)
def test_the_program_s_environment_is_the_same_with_or_without_returns_twice(project):
    # Without the key, Peeky asks the compiler for its startup objects: in its own
    # environment, never the one the program launches with.
    compiler = _clang()
    _build_text(project, compiler, {})
    launched = {}
    for claim in (None, ()):
        session = _target(compiler, returns_twice=claim).prepare(project / "prog", project)
        try:
            launched[claim] = dict(session.prepared.manifest.effective_environment)
        finally:
            session.prepared.cleanup()
    assert launched[None] == launched[()], launched
