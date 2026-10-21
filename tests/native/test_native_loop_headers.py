"""A C loop header that stops only before its loop says so (#161), on real builds: a
`for`/`while` header's one breakpoint location is its first code (`i = 0`, a jump to
the condition), which runs once before the loop, never at the condition the loop
returns to. `--at` on it with `--hits all` gave one row where an agent expected one per
pass, with no word. Decided from the code (LLDB's disassembly), with clang and GCC."""

import json
import os
import shutil
import subprocess

import pytest

from peeky.cli import main

LOOPS = """#include <stdio.h>
int main(void) {
    int total = 0, n = 0;
    for (int i = 0; i < 3; i++) { // FOR
        total += i; // BODY
    }
    int k = 0;
    while (k < 3) { // WHILE
        k++;
    }
    for (;;) { // FOREVER
        if (++n > 3) break;
    }
    do { // DO_OPEN
        total++;
    } while (total < 12); // DO_CLOSE
    for (int a = 0; a < 2; a++) { // OUTER
        for (int b = 0; b < 2; b++) { // INNER
            total += b;
        }
    }
    for (int i = 0; i < 3; i++) { int sq = i * i; total += sq; } // ONE_LINE
    printf("total=%d k=%d n=%d\\n", total, k, n);
    return 0;
}
"""
OUTPUT = "total=19 k=3 n=4"


def _gcc():
    gcc = shutil.which("gcc")
    version = subprocess.run([gcc, "--version"], capture_output=True, text=True) if gcc else None
    return gcc if version is not None and "Free Software Foundation" in version.stdout else None


@pytest.fixture(params=["clang", "gcc"])
def loops(request, tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    compiler = os.environ.get("PEEKY_NATIVE_CC", "clang") if request.param == "clang" else _gcc()
    if compiler is None:
        pytest.skip("needs GCC")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "loops.c").write_text(LOOPS)
    config = tmp_path / "loops.json"
    config.write_text(
        json.dumps(
            {
                "language": "c",
                "sources": ["loops.c"],
                "compiler": compiler,
                "adapter": os.environ["PEEKY_NATIVE_DAP"],
                "dwarfdump": os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
                "dsymutil": os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
                "sysroot": os.environ.get("PEEKY_NATIVE_SYSROOT")
                if request.param == "clang"
                else None,
            }
        )
    )
    return request.param, tmp_path, config


def line_of(marker):
    return next(i for i, line in enumerate(LOOPS.splitlines(), 1) if f"// {marker}" in line)


def probe(root, config, capsys, marker):
    line = line_of(marker)
    command = ["probe", str(root), "--config", str(config), "--at", f"loops.c:{line}"]
    assert main([*command, "--capture", "total", "--hits", "all", "--json", "--quiet"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed" and OUTPUT in result["output"], result
    notes = [w for w in result["warnings"] if "is a loop header" in w]
    return result["hits"], notes, result["rows"][0]["line"] if result["rows"] else None


@pytest.mark.timeout(300)
def test_a_loop_header_that_stops_only_before_its_loop_says_so(loops, capsys):
    compiler, root, config = loops
    seen = {}
    for marker in ("FOR", "WHILE", "FOREVER", "DO_OPEN", "OUTER", "INNER"):
        seen[marker] = probe(root, config, capsys, marker)
    for marker in ("BODY", "DO_CLOSE", "ONE_LINE"):
        seen[marker] = probe(root, config, capsys, marker)
    print(compiler, seen)
    for marker in ("FOR", "WHILE", "OUTER", "INNER"):
        hits, notes, line = seen[marker]
        assert line == line_of(marker), (marker, seen[marker])
        assert notes == [
            f"Line {line} is a loop header: it stops once each time its loop starts, before "
            "the first pass, never at the passes; put --at (or --set-at) on the first line of "
            "the loop's body for one stop per pass."
        ], (marker, seen[marker])
        # Once per start of its loop: the inner one starts at each outer pass.
        assert hits == (2 if marker == "INNER" else 1), (marker, seen[marker])
    # A header with no code of its own may move to the next line (GCC); where it stops
    # on its own line, it is a loop entry.
    for marker in ("FOREVER", "DO_OPEN"):
        hits, notes, line = seen[marker]
        if line == line_of(marker):
            assert hits == 1 and len(notes) == 1, (marker, seen[marker])
        else:
            assert not notes, (marker, seen[marker])
    if compiler == "clang":
        assert all(seen[marker][1] for marker in ("FOREVER", "DO_OPEN")), seen
    # What stops at each pass gets no note: a body, a `do`'s `while`, a one-line loop's
    # body block.
    assert seen["BODY"][:2] == (3, []), seen["BODY"]
    assert seen["DO_CLOSE"][0] >= 8 and not seen["DO_CLOSE"][1], seen["DO_CLOSE"]
    assert seen["ONE_LINE"][0] == 4 and not seen["ONE_LINE"][1], seen["ONE_LINE"]


@pytest.mark.timeout(180)
def test_session_start_on_a_loop_header_says_it_stops_before_the_loop(loops, capsys):
    compiler, root, config = loops
    if compiler != "clang":
        pytest.skip("the probe test covers GCC")
    line = line_of("WHILE")
    command = ["session", "start", str(root), "--config", str(config), "--at", f"loops.c:{line}"]
    assert main(command) == 0
    try:
        out = capsys.readouterr().out
        assert f"warning: Line {line} is a loop header: it stops once each time its loop" in out
        # It stops before the loop: the next breakpoint stop is past it, at the exit.
        main(["session", "continue"])
        assert "program exited" in capsys.readouterr().out
    finally:
        main(["session", "close"])
        capsys.readouterr()
