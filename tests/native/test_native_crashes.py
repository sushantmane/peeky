"""A C or C++ program that crashes during a probe or a session: a fault in libc
(`strlen(NULL)`), a failed `assert`, an uncaught C++ exception and `std::terminate` stop
in libc, the C++ runtime or `abort`, outside the program's sources. Each is the program's
crash (`PROGRAM_CRASHED`), with its output so far and the nearest frame of its own code,
not a debugger failure that drops the output; a fault in the program's own code is too.
A signal the program handles, and a C++ throw or catch filter's stop, are no crash: the
probe lets the program go on. The crash's frame is checked against the
build's module, as `inspect` checks one."""

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from peeky.cli import main
from peeky.client import Client
from peeky.errors import PeekyError
from peeky.native import NativeTarget
from peeky.worker import make_server

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


HANDLED = """#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static volatile sig_atomic_t got;

static void on_signal(int number) { got = number; }

int main(int argc, char **argv) {
    int n = argc;
    signal(SIGINT, on_signal);
    signal(SIGPIPE, on_signal);
    signal(SIGABRT, on_signal);
    printf("start %d\\n", n); /* START */
    fflush(stdout);
    if (argv[1][0] == 'i')
        raise(SIGINT);
    if (argv[1][0] == 'p') {
        int ends[2];
        if (pipe(ends) == 0 && close(ends[0]) == 0)
            n = (int)write(ends[1], "x", 1);
    }
    if (argv[1][0] == 'a')
        raise(SIGABRT);
    for (int k = 0; argv[1][0] == 'l' && k < atoi(argv[2]); k++)
        raise(SIGINT); /* LOOP */
    for (int k = 0; argv[1][0] == 'm' && k < atoi(argv[2]); k++) {
        if (k % 2)
            raise(SIGINT); /* ODD */
        else
            raise(SIGINT); /* EVEN */
    }
    printf("done %d\\n", n); /* DONE */
    return 0;
}
"""


SIGNALLED = """#include <signal.h>
#include <stdio.h>

static volatile sig_atomic_t got;

static void on_signal(int number) { got = number; }

int main(void) {
    int step = 1, total = 0;
    signal(SIGINT, on_signal);
    for (int k = 0; k < 4; k++) {
        total += step; /* ADD */
        raise(SIGINT);
    }
    printf("total %d\\n", total); /* DONE */
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
def test_a_signal_the_program_handles_is_no_crash_and_the_probe_goes_on(cc, capsys):
    Path("handled.c").write_text(HANDLED)
    start = line_of(HANDLED, "START")
    for argument, name in (("i", "SIGINT"), ("p", "SIGPIPE"), ("a", "SIGABRT")):
        code, result = probe(capsys, "handled.c", start, argument)
        assert code == 0 and result["status"] == "completed", (name, result)
        assert "crash" not in result and "error" not in result, (name, result)
        assert [row["value"] for row in result["rows"]] == ["2"], result["rows"]
        assert "start 2\n" in result["output"] and "done " in result["output"], result
        # Where the debugger stopped on it (LLDB on Linux stops on SIGPIPE, macOS's not).
        stopped = [w for w in result.get("warnings", ()) if "let the program go on" in w]
        assert all(f"stopped on signal {name}, which Peeky" in w for w in stopped), stopped
        if name != "SIGPIPE":
            assert len(stopped) == 1, result


@pytest.mark.timeout(300)
def test_a_loop_of_handled_signals_is_bounded_with_an_honest_message(cc, capsys):
    Path("handled.c").write_text(HANDLED)
    done, loop = line_of(HANDLED, "DONE"), line_of(HANDLED, "LOOP")
    # 25 raises from one line: the program makes progress; the probe goes on to its line.
    code, result = probe(capsys, "handled.c", done, "l", "25")
    assert code == 0 and result["status"] == "completed", result
    assert [row["value"] for row in result["rows"]] == ["3"], result["rows"]
    assert "error" not in result and "done 3\n" in result["output"], result
    assert any(
        w.startswith("The probe let the program go on after it stopped on signal SIGINT")
        and "(25 times)" in w
        for w in result["warnings"]
    ), result["warnings"]
    # Past the bound, the probe ends, and says what it can't tell, not that the debugger
    # failed.
    code, result = probe(capsys, "handled.c", done, "l", "150")
    assert code == 1 and result["status"] == "failed" and result["hits"] == 0, result
    error = result["error"]
    assert error["code"] == "BUDGET_EXHAUSTED", error
    # The 100th stop is not let go on, so nothing says "letting it go on each time".
    assert error["message"] == (
        "The probe ended at the program's 100th stop in a row on signal SIGINT, outside the "
        f"program's sources, in a call from main at handled.c:{loop}. It let the program go "
        "on after each of the first 99. The program may cause that stop again and again, or "
        "the debugger may stop there without letting it past: Peeky can't tell which"
    ), error
    assert error["hint"].startswith("Probe a line the program reaches before these stops")
    assert result["output"].startswith("start 3\n"), result
    assert any(
        w.startswith("The probe let the program go on after it stopped on signal SIGINT (99 times)")
        for w in result["warnings"]
    ), result["warnings"]
    # Raises alternating between two lines reach one place in libc, but are not the same
    # stop: none comes 100 times in a row, and the probe goes on to its line.
    code, result = probe(capsys, "handled.c", done, "m", "150")
    assert code == 0 and result["status"] == "completed", result
    assert "error" not in result and "done 3\n" in result["output"], result
    assert any(
        w.startswith(
            "The probe let the program go on after it stopped on signal SIGINT (150 times)"
        )
        for w in result["warnings"]
    ), result["warnings"]


@pytest.mark.timeout(300)
def test_a_handled_signal_between_a_set_at_lines_stops_leaves_its_count(cc, capsys):
    # The signal's stop after each pass started line ADD's count again: `ADD@2` was never
    # made ("line 12 stopped 0 time(s)", though it stopped 4 times), and the total was 4.
    Path("signalled.c").write_text(SIGNALLED)
    add = line_of(SIGNALLED, "ADD")
    code = main(
        ["probe", "signalled.c", "--at", str(line_of(SIGNALLED, "DONE")), "--capture", "total"]
        + ["--set-at", f"{add}@2", "step=100", "--json", "--quiet"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["status"] == "completed", result
    assert [row["value"] for row in result["rows"]] == ["301"], result["rows"]
    [change] = result["interventions"]
    assert (change["at"], change["before"], change["after"]) == (f"{add}@2", "1", "100"), change
    assert "unapplied" not in result, result
    assert any("stopped on signal SIGINT" in w for w in result["warnings"]), result["warnings"]


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
        assert main(["session", "get", "argv[0]", "--frame", str(nearest), "--json"]) == 0
        (row, *_) = json.loads(capsys.readouterr().out)
        assert row["frame"] == nearest and row["value"].endswith('"'), row
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


@pytest.fixture
def served(cxx, tmp_path):
    """A worker in this process, for a C++ session on abort.cpp: its DAP and its runtime are
    at hand (a C++ throw filter, which no Peeky command sets; a module that isn't the
    build's)."""
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    root = tmp_path / "root"
    root.mkdir()
    (root / "abort.cpp").write_text(ABORT)
    target = NativeTarget(
        sources=("abort.cpp",),
        language="cpp",
        compiler=os.environ["PEEKY_NATIVE_CXX"],
        adapter=os.environ["PEEKY_NATIVE_DAP"],
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT") if cxx == "clang" else None,
    )
    token = "native-crash-tests-token-123456-0123456789abcdef"
    server = make_server(root, token, 0, adapter=target)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, Client(f"http://127.0.0.1:{server.server_port}", token)
    server.worker.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def wait_stop(client, session):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = client.call("wait", **session, timeout=1)
        if state["state"] != "running":
            return state
    pytest.fail("no stop")


@pytest.mark.timeout(300)
def test_cpp_a_throw_or_catch_filters_stop_is_no_crash(served):
    server, client = served
    before = line_of(ABORT, "BEFORE")
    started = client.call("launch", program="abort.cpp", breakpoints=[{"line": before}], args=[])
    session = {"session_id": started["session_id"]}
    state = wait_stop(client, session)
    assert state["reason"] == "breakpoint", state
    # lldb-dap 18 and 20 report a filter's stop as `exception`, as they do a signal's.
    server.worker.dap.request("setExceptionBreakpoints", {"filters": ["cpp_throw", "cpp_catch"]})
    client.call("continue", **session, stop_id=state["stop_id"])
    state = wait_stop(client, session)
    assert state["state"] == "stopped" and state["reason"] == "exception", state
    assert "crash" not in state, state
    with pytest.raises(PeekyError) as refused:
        client.call("inspect", **session, stop_id=state["stop_id"])
    error = refused.value.payload
    assert error["code"] == "INVALID_REQUEST" and not error["session_closed"], error
    assert "where the program stopped (" in error["message"], error
    assert "crash" not in error["message"], error
    # The session goes on; the program catches what it throws.
    server.worker.dap.request("setExceptionBreakpoints", {"filters": []})
    client.call("continue", **session, stop_id=state["stop_id"])
    state = wait_stop(client, session)
    assert state["reason"] == "breakpoint", state


@pytest.mark.timeout(300)
def test_cpp_a_crash_frame_whose_module_is_not_the_builds_fails_closed(served):
    server, client = served
    throw = line_of(ABORT, "THROW")
    started = client.call("launch", program="abort.cpp", breakpoints=[{"line": throw}], args=["x"])
    session = {"session_id": started["session_id"]}
    state = wait_stop(client, session)
    assert state["reason"] == "breakpoint", state
    # The program's frame at the crash then lies in a module that isn't the prepared one.
    server.worker.runtime.module_identity = "0" * 32
    client.call("continue", **session, stop_id=state["stop_id"])
    with pytest.raises(PeekyError) as failed:
        wait_stop(client, session)
    # Not a `crash` naming main at abort.cpp: the crash's frame is checked against the build.
    error = failed.value.payload
    assert error["code"] == "DEBUGGER_ERROR" and error["session_closed"], error
    assert "module" in error["message"], error
