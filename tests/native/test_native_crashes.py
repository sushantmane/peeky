"""A C or C++ program that crashes during a probe or a session (#342): a fault in libc
(`strlen(NULL)`), a failed `assert`, an uncaught C++ exception and `std::terminate` stop
in libc, the C++ runtime or `abort`, outside the program's sources. Each is the program's
crash (`PROGRAM_CRASHED`), with its output so far and the nearest frame of its own code,
not a debugger failure that drops the output; a fault in the program's own code is too."""

import json
import os
import shutil
import subprocess

import pytest

from peeky.cli import main

CRASH = """#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv) {
    int n = argc;
    const char *text = argc > 9 ? "ok" : NULL;
    int *none = argc > 9 ? &n : NULL;
    printf("start %d\\n", n); /* START */
    fflush(stdout);
    if (argv[1][0] == 's')
        n = (int)strlen(text); /* STRLEN */
    if (argv[1][0] == 'a')
        assert(n == 0); /* ASSERT */
    if (argv[1][0] == 'f')
        n = *none; /* FAULT */
    printf("done %d\\n", n); /* DONE */
    return 0;
}
"""

ABORT = """#include <cstdio>
#include <exception>
#include <stdexcept>
#include <string>

int main(int argc, char **argv) {
    int n = 0;
    for (int i = 1; i <= 3; i++) {
        n = i * 2;
        std::printf("before %d\\n", i); /* BEFORE */
        std::fflush(stdout);
        try {
            std::string t;
            t.reserve(t.max_size() + 1);
        } catch (const std::length_error &) {
            n += 100; /* CAUGHT */
        }
        if (argc > 1 && argv[1][0] == 'x' && i == 2) {
            std::string s;
            s.reserve(s.max_size() + 1); /* THROW */
        }
        if (argc > 1 && argv[1][0] == 't' && i == 2)
            std::terminate(); /* TERMINATE */
    }
    std::printf("done %d\\n", n); /* DONE */
    return 0;
}
"""


def line_of(source, marker):
    return next(i for i, text in enumerate(source.splitlines(), 1) if f"/* {marker} */" in text)


def _gcc():
    gcc = shutil.which("gcc")
    if not gcc:
        return None
    version = subprocess.run([gcc, "--version"], capture_output=True, text=True)
    return gcc if "Free Software Foundation" in version.stdout else None


@pytest.fixture(params=["clang", "gcc"])
def cc(request, tmp_path, monkeypatch):
    """The C compiler's family, with PEEKY_NATIVE_CC set to it; the GCC case skips where
    no gcc is GCC (macOS)."""
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    if request.param == "gcc":
        if (gcc := _gcc()) is None:
            pytest.skip("needs GCC")
        monkeypatch.setenv("PEEKY_NATIVE_CC", gcc)
        monkeypatch.delenv("PEEKY_NATIVE_SYSROOT", raising=False)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    return request.param


@pytest.fixture
def cpp_dir(cxx, tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "abort.cpp").write_text(ABORT)
    return tmp_path


def probe(capsys, program, line, *args, capture="n"):
    code = main(
        ["probe", program, "--at", str(line), "--capture", capture, "--json", "--quiet", "--"]
        + list(args)
    )
    result = json.loads(capsys.readouterr().out)
    if isinstance(result.get("output"), str):
        # A terminal's line ends, where the debugger gives the program one (lldb-dap).
        result["output"] = result["output"].replace("\r\n", "\n")
    return code, result


def assert_crashed(result, location, outside=True):
    """The program's crash, not the debugger's failure: its output kept, the nearest frame
    of its code named (on main: DEBUGGER_ERROR with no output, or INVALID_REQUEST)."""
    assert result["status"] == "failed", result
    assert result["error"]["code"] == "PROGRAM_CRASHED", result["error"]
    crash = result["crash"]
    assert crash["outside_program"] is outside, crash
    assert crash["function"] == "main", crash
    assert crash["location"].endswith(f"/{location}"), crash
    assert (crash["frame"] > 0) is outside, crash
    assert crash["description"], crash
    assert f" main at {location}" in result["error"]["message"], result["error"]
    assert result["error"]["message"].endswith("Peeky ended it there, so it has no exit status")
    return crash


@pytest.mark.timeout(300)
def test_a_fault_in_libc_an_assert_and_a_fault_in_the_program_are_its_crashes(cc, capsys):
    from pathlib import Path

    Path("crash.c").write_text(CRASH)
    start = line_of(CRASH, "START")
    # strlen(NULL) faults inside libc, in a call from main.
    code, result = probe(capsys, "crash.c", start, "s", "x")
    assert code == 1, result
    assert [row["value"] for row in result["rows"]] == ["3"], result["rows"]
    assert_crashed(result, f"crash.c:{line_of(CRASH, 'STRLEN')}")
    assert result["error"]["message"].startswith("The program crashed after hit 1: ")
    assert "outside the program's sources, in a call from main" in result["error"]["message"]
    assert result["output"].startswith("start 3\n"), result
    assert "uncaught_exception" not in result["crash"]
    # A failed assert aborts in libc; its message is in the output.
    code, result = probe(capsys, "crash.c", start, "a")
    assert code == 1, result
    assert_crashed(result, f"crash.c:{line_of(CRASH, 'ASSERT')}")
    assert "start 2\n" in result["output"] and "n == 0" in result["output"], result
    # A fault in the program's own code: the stop's own frame (on main: INVALID_REQUEST,
    # "Stopped at …crash.c:16 (exception), not the requested crash.c:9").
    code, result = probe(capsys, "crash.c", start, "f")
    assert code == 1, result
    crash = assert_crashed(result, f"crash.c:{line_of(CRASH, 'FAULT')}", outside=False)
    assert crash["frame"] == 0, crash
    # Before the probe's line: no hit, and said so.
    code, result = probe(capsys, "crash.c", line_of(CRASH, "DONE"), "s", "x")
    assert code == 1 and result["hits"] == 0 and result["rows"] == [], result
    assert_crashed(result, f"crash.c:{line_of(CRASH, 'STRLEN')}")
    assert result["error"]["message"].startswith(
        f"The program crashed before it stopped at crash.c:{line_of(CRASH, 'DONE')}: "
    ), result["error"]
    # And as text: the crash, then the output so far.
    assert main(["probe", "crash.c", "--at", str(start), "--capture", "n", "--", "a"]) == 1
    text = capsys.readouterr().out
    assert "\nerror [PROGRAM_CRASHED]: The program crashed after hit 1: " in text, text
    assert "\nhint: The program failed, not Peeky: its output may say why." in text, text
    assert "\noutput so far (the program was stopped at its crash):\n  start 2\n" in text, text


@pytest.mark.timeout(300)
def test_cpp_an_uncaught_exception_is_the_programs_crash_with_its_output(cpp_dir, capsys):
    before = line_of(ABORT, "BEFORE")
    code, result = probe(capsys, "abort.cpp", before, "x")
    # On main: hit 1, then DEBUGGER_ERROR "Native stopped source does not match prepared
    # symbols", and no output.
    assert code == 1, result
    assert [row["value"] for row in result["rows"]] == ["2"], result["rows"]
    crash = assert_crashed(result, f"abort.cpp:{line_of(ABORT, 'THROW')}")
    assert crash["uncaught_exception"] is True, crash
    assert "after a C++ exception that nothing caught" in result["error"]["message"]
    assert result["output"].startswith("before 1\nbefore 2\n"), result
    # The C++ runtime names the exception on stderr: libc++abi and libstdc++ alike.
    assert "std::" in result["output"] and "before 3" not in result["output"], result
    # A line after the throw is never reached: no hit.
    code, result = probe(capsys, "abort.cpp", line_of(ABORT, "DONE"), "x")
    assert code == 1 and result["hits"] == 0, result
    assert_crashed(result, f"abort.cpp:{line_of(ABORT, 'THROW')}")
    # std::terminate called directly: an abort, with no exception.
    code, result = probe(capsys, "abort.cpp", before, "t")
    assert code == 1, result
    crash = assert_crashed(result, f"abort.cpp:{line_of(ABORT, 'TERMINATE')}")
    assert "uncaught_exception" not in crash, crash
    # An exception the program catches never stops it.
    code, result = probe(capsys, "abort.cpp", line_of(ABORT, "CAUGHT"), "-", capture="n")
    assert code == 0 and result["status"] == "completed", result
    assert "crash" not in result and "done 106" in result["output"], result


@pytest.mark.timeout(300)
def test_cpp_a_session_at_the_crash_reads_only_the_programs_frames(cpp_dir, capsys):
    before, throw = line_of(ABORT, "BEFORE"), line_of(ABORT, "THROW")
    assert main(["session", "start", "abort.cpp", "--at", str(before), "--", "x"]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "continue"]) == 0
        capsys.readouterr()
        assert main(["session", "continue", "--json"]) == 0
        state = json.loads(capsys.readouterr().out)
        assert state["state"] == "stopped" and state["reason"] == "exception", state
        crash = state["crash"]
        assert crash["outside_program"] is True and crash["uncaught_exception"] is True, crash
        assert crash["function"] == "main" and crash["location"].endswith(f"abort.cpp:{throw}")
        nearest = crash["frame"]
        # Frame 0 is in libc or the C++ runtime: refused, and the session is kept (on main:
        # DEBUGGER_ERROR, and the session closed).
        assert main(["session", "inspect", "--json"]) == 2
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "INVALID_REQUEST" and not error["session_closed"], error
        assert error["message"].startswith("Nothing in frame 0 ("), error
        assert "outside the program's sources, where the program crashed" in error["message"]
        assert error["message"].endswith(
            f"Frame {nearest}, main at abort.cpp:{throw}, is the nearest of your code: "
            f"`session inspect --frame {nearest}`"
        ), error
        # The program's own frame is read, often past the 10 frames listed by default.
        assert main(["session", "get", "n", "--frame", str(nearest), "--json"]) == 0
        (row,) = json.loads(capsys.readouterr().out)
        assert (row["frame"], row["function"], row["value"]) == (nearest, "main", "104"), row
        assert row["settable"] is False, row
        # Its value views and expansions find that frame again among as many frames.
        assert main(["session", "get", "argv[1]", "--frame", str(nearest), "--json"]) == 0
        (row, *_) = json.loads(capsys.readouterr().out)
        assert row["frame"] == nearest and row["value"].endswith('"x"'), row
        assert main(["session", "report"]) == 0
        report = capsys.readouterr().out.replace("\r\n", "\n")
        assert "before 1\nbefore 2\n" in report, report
        # As text: the crash, and the frame to read.
        assert main(["session", "wait"]) == 0
        out = capsys.readouterr().out
        assert "[the program crashed: " in out, out
        assert f"in a call from main at abort.cpp:{throw}" in out, out
        assert f"`session inspect --frame {nearest}` reads that frame of your code]" in out, out
    finally:
        main(["session", "close"])
