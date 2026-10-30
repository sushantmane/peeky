"""Each unit's preprocessing, then each unit's compile, run at the same time (#398). A
parallel build gives what a build in order gives: the same values, and, where several
sources fail, the first one's error, word for word."""

import os

import pytest

from peeky.build import BuildCommandError

MAIN = """#include <stdio.h>
int one(int);
int two(int);
int main(void) {
  int total = one(2) + two(3);
  printf("%d\\n", total);
  return 0;
}
"""
ONE = "int one(int x) {\n  int doubled = x * 2;\n  return doubled;\n}\n"
TWO = "int two(int x) {\n  int tripled = x * 3;\n  return tripled;\n}\n"
BROKEN = {
    "preprocess": '#include "missing-{name}.h"\nint {name}(int x) {{ return x; }}\n',
    "compile": "int {name}(int x) {{\n  return x +;\n}}\n",
}


def target(tmp_path, language, sources):
    from peeky.native import NativeTarget

    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    suffix = ".cpp" if language == "cpp" else ".c"
    for name, text in sources.items():
        (tmp_path / (name + suffix)).write_text(text)
    compiler = os.environ.get("PEEKY_NATIVE_CXX" if language == "cpp" else "PEEKY_NATIVE_CC")
    return NativeTarget(
        sources=tuple(name + suffix for name in sources),
        compiler=compiler or ("clang++" if language == "cpp" else "clang"),
        adapter=os.environ["PEEKY_NATIVE_DAP"],
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
        language=language,
    )


def matches_one_in_order(tmp_path, monkeypatch, language):
    from peeky.probe import Probe, run_probe

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    program = tmp_path / "program"
    program.mkdir()
    adapter = target(program, language, {"main": MAIN, "one": ONE, "two": TWO})
    suffix = ".cpp" if language == "cpp" else ".c"
    results = []
    for jobs in ("1", "3"):
        monkeypatch.setenv("PEEKY_BUILD_JOBS", jobs)
        result = run_probe(program, Probe("two" + suffix, 3, ("x", "tripled")), adapter=adapter)
        assert result["status"] == "completed", result.get("error")
        rows = [row.get("value") for row in result["rows"]]
        results.append((rows, result["output"].splitlines()))
    assert results[0] == results[1] == (["3", "9"], ["13"])


def test_a_parallel_build_matches_one_in_order(tmp_path, monkeypatch):
    matches_one_in_order(tmp_path, monkeypatch, "c")


def test_cpp_a_parallel_build_matches_one_in_order(tmp_path, monkeypatch, cxx):
    matches_one_in_order(tmp_path, monkeypatch, "cpp")


def first_error(tmp_path, monkeypatch, language, stage):
    import tempfile

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    # Where the build's workspace is made: removed when the build fails.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    program = tmp_path / "program"
    program.mkdir()
    sources = {"main": MAIN, **{name: BROKEN[stage].format(name=name) for name in ("one", "two")}}
    adapter = target(program, language, sources)
    suffix = ".cpp" if language == "cpp" else ".c"
    failures = []
    for jobs in ("1", "3"):
        monkeypatch.setenv("PEEKY_BUILD_JOBS", jobs)
        with pytest.raises(BuildCommandError) as caught:
            adapter.prepare(program / ("main" + suffix), program)
        failures.append((caught.value.command, str(caught.value), caught.value.result["stderr"]))
    assert failures[1] == failures[0]
    command, _, stderr = failures[0]
    assert ("-E" if stage == "preprocess" else "-c") in command
    assert "one" + suffix in stderr and "two" + suffix not in stderr, stderr
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("stage", ["preprocess", "compile"])
def test_a_parallel_builds_error_is_the_first_sources(tmp_path, monkeypatch, stage):
    first_error(tmp_path, monkeypatch, "c", stage)


@pytest.mark.parametrize("stage", ["preprocess", "compile"])
def test_cpp_a_parallel_builds_error_is_the_first_sources(tmp_path, monkeypatch, cxx, stage):
    first_error(tmp_path, monkeypatch, "cpp", stage)
