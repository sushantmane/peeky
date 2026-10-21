"""What a function returned (#155): shown only where Peeky can tie it to the function a
step left, and captured by probes once per call (`--capture return`)."""

import pytest

from peeky.errors import classify
from peeky.java import JavaSession
from peeky.native import NativeSession, _return_paths
from peeky.probe import RETURN, Probe, run_probe
from peeky.rust import RustSession
from peeky.worker import DebugWorker, _method_name


class Native:
    return_entry = staticmethod(NativeSession.return_entry)
    return_withheld = NativeSession.return_withheld
    reports_unavailable_as_text = True
    target_identity = {"language": "cpp"}

    def typedef_kind(self, kind):
        return ("pointer", "Task") if kind == "TaskPtr" else None


class Rust(Native):
    return_withheld = RustSession.return_withheld
    return_shown = staticmethod(RustSession.return_shown)


def frame(name, pc=None, line=1):
    return {"name": name, "instructionPointerReference": pc, "line": line}


ORIGIN = [frame("before", "0x10", 16), frame("__sort3", "0x20", 115), frame("main", "0x30", 9)]
RETURNED = [frame("__sort3", "0x20", 115), frame("main", "0x30", 9)]
VALUE = {"name": "(Return Value)", "type": "bool", "value": "false"}


def stopped(tmp_path, runtime, origin=ORIGIN, reason="step"):
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.thread_id, worker.stop_reason = runtime, 7, reason
    worker.step_origin = {
        "thread": 7,
        "function": origin[0]["name"],
        "frames": [(f["name"], f["instructionPointerReference"], f["line"]) for f in origin],
    }
    return worker


def test_a_value_is_shown_only_for_a_return_one_frame_up(tmp_path):
    worker = stopped(tmp_path, Native())
    assert worker.returned_value(worker.runtime, RETURNED, VALUE) == {
        "function": "before",
        "type": "bool",
        "value": "false",
    }
    # LLDB went on past the caller (libc++'s hidden frames, before #155): Apple's LLDB
    # then listed `true` for a comparator that returned false. Never shown.
    past = worker.returned_value(worker.runtime, [frame("std::sort", "0x40", 946)], VALUE)
    assert past["unavailable"] and "did not stop where this function returned" in past["reason"]
    assert "value" not in past
    # The same function's frame, at another instruction (a step that stayed in it).
    elsewhere = [frame("__sort3", "0x24", 116), frame("main", "0x30", 9)]
    assert worker.returned_value(worker.runtime, elsewhere, None) is None
    # A different caller below: not the same stack.
    assert not worker.left_function([frame("__sort3", "0x20", 115), frame("other", "0x30", 9)])
    # Only a step's stop, on the thread that stepped.
    worker.stop_reason = "breakpoint"
    assert worker.returned_value(worker.runtime, RETURNED, VALUE) is None
    worker.stop_reason, worker.thread_id = "step", 8
    assert worker.returned_value(worker.runtime, RETURNED, VALUE) is None


@pytest.mark.parametrize(
    "variable, shown",
    [
        ({"type": "int", "value": "7"}, True),
        ({"type": "unsigned long", "value": "7"}, True),
        ({"type": "const char *", "value": '0x1 "a"'}, True),
        ({"type": "TaskPtr", "value": "0x1"}, True),
        ({"type": "double", "value": "3.5"}, True),
        ({"type": "Task", "value": "{prio = 1}"}, False),
        ({"type": "std::string", "value": '"a"'}, False),
        ({"type": "const Task &", "value": "0x1"}, False),
        ({"type": "int", "value": "<error: no value>"}, False),
    ],
)
def test_only_scalar_native_returns_are_shown(tmp_path, variable, shown):
    worker = stopped(tmp_path, Native())
    returned = worker.returned_value(
        worker.runtime, RETURNED, {"name": "(Return Value)", **variable}
    )
    assert ("value" in returned) is shown, returned
    assert returned.get("unavailable") is (None if shown else True)


def test_nothing_returned_and_optimized_builds_are_withheld(tmp_path):
    runtime = Native()
    worker = stopped(tmp_path, runtime)
    # A void function, or a value LLDB cannot read: never "nothing" claimed as a value.
    assert "reported no value" in worker.returned_value(runtime, RETURNED, None)["reason"]
    runtime.target_identity = {"language": "cpp", "optimization": "-O2"}
    worker.runtime = runtime
    optimized = worker.returned_value(runtime, RETURNED, VALUE)
    assert optimized["unavailable"] and "optimized build" in optimized["reason"]


def test_rust_returns_read_as_rust_prints_them(tmp_path):
    worker = stopped(tmp_path, Rust())
    byte = {"name": "(Return Value)", "type": "unsigned char", "value": "'A'"}
    assert worker.returned_value(worker.runtime, RETURNED, byte)["value"] == "65"
    assert worker.returned_value(worker.runtime, RETURNED, byte)["type"] == "u8"
    integer = {"name": "(Return Value)", "type": "int", "value": "-3"}
    assert worker.returned_value(worker.runtime, RETURNED, integer)["type"] == "i32"
    option = {"name": "(Return Value)", "type": "core::option::Option<i32>", "value": "Some"}
    assert worker.returned_value(worker.runtime, RETURNED, option)["unavailable"]


def test_java_debug_names_the_method_it_lists_a_return_for(tmp_path):
    assert JavaSession.return_entry({"name": "\u23af\u25batwice()"}) == "twice"
    assert JavaSession.return_entry({"name": "->cmp()"}) == "cmp"
    assert JavaSession.return_entry({"name": "items"}) is None
    assert _method_name("Ret.twice(int)") == "twice"
    assert _method_name("before(Task const&, Task const&)") == "before"

    class Java:
        return_entry = staticmethod(JavaSession.return_entry)

    origin = [frame("Ret.twice(int)", line=5), frame("Ret.main(String[])", line=14)]
    worker = stopped(tmp_path, Java(), origin)
    after = [frame("Ret.main(String[])", line=14)]
    entry = {"name": "\u23af\u25batwice()", "type": "int", "value": "8"}
    assert worker.returned_value(Java(), after, entry) == {
        "function": "Ret.twice(int)",
        "type": "int",
        "value": "8",
    }
    other = worker.returned_value(Java(), after, {**entry, "name": "->thrice()"})
    assert other["unavailable"] and "'thrice' returned" in other["reason"]
    # JDI gives no value for a method that threw (or returns nothing): neither is claimed.
    Java.no_return = JavaSession.no_return
    none = worker.returned_value(Java(), after, None)
    assert none["unavailable"] and "or it ended by an exception" in none["reason"]
    assert "a function returning nothing" not in none["reason"]


class Watched(Native):
    """LLDB's step out, watched (NativeSession.watch_return): where the thread stopped."""

    def __init__(self, seen):
        self.seen, self.unwatched = seen, []

    def unwatch_return(self, dap, watch, thread_id=None):
        self.unwatched.append(watch["ids"])
        return self.seen if thread_id == watch["thread"] else None


WATCH = {"thread": 7, "sp": 0x1000, "ids": [41], "addresses": [0x18, 0x24]}


def watched(tmp_path, seen, reason, ids=None):
    worker = stopped(tmp_path, Watched(seen), reason=reason)
    worker.stop_id, worker.own_ids = "s", {1}
    body = {"reason": reason, **({"hitBreakpointIds": ids} if ids is not None else {})}
    worker.end_return_watch(worker.runtime, dict(WATCH), {"body": body})
    return worker


def test_a_call_its_caller_went_on_without_is_never_credited_with_a_later_value(tmp_path):
    # f(0) threw; its caller caught it and called again from the same place, which
    # LLDB's step out would take for f(0)'s return (#155 round 1): the watch on that
    # call stops first, in the caller's frame, and nothing is credited.
    worker = watched(tmp_path, {"pc": 0x18, "sp": 0x1000}, "breakpoint", [41])
    assert worker.stop_reason == "step" and worker.runtime.unwatched == [[41]]
    left = worker.returned_value(worker.runtime, [frame("__sort3", "0x18", 115)], VALUE)
    assert left["unavailable"] and "never returned" in left["reason"]
    assert "value" not in left
    # A frame older than the caller's (the caller was unwound too) makes the same call.
    assert watched(tmp_path, {"pc": 0x24, "sp": 0x2000}, "breakpoint").stop_reason == "step"
    # Also the session's own breakpoint there: that stop is a hit, and still no value.
    both = watched(tmp_path, {"pc": 0x18, "sp": 0x1000}, "breakpoint", [1])
    assert both.stop_reason == "breakpoint" and both.step_origin["abandoned"]
    # A deeper frame (a recursive call below the call being stepped out of) is not it.
    deeper = watched(tmp_path, {"pc": 0x18, "sp": 0x0F00}, "breakpoint", [41])
    assert deeper.stop_reason == "breakpoint" and not deeper.step_origin.get("abandoned")


def test_a_return_is_shown_only_where_the_callers_stack_pointer_is_back(tmp_path):
    returned = watched(tmp_path, {"pc": 0x20, "sp": 0x1000}, "step")
    assert returned.returned_value(returned.runtime, RETURNED, VALUE)["value"] == "false"
    # The same return address in the same function, at another frame position: a
    # later call's return, never this one's.
    moved = watched(tmp_path, {"pc": 0x20, "sp": 0x1100}, "step")
    other = moved.returned_value(moved.runtime, RETURNED, VALUE)
    assert other["unavailable"] and "stack pointer" in other["reason"]


def test_inlined_and_unwatched_calls_say_why_no_value_is_shown(tmp_path):
    worker = stopped(tmp_path, Native())
    worker.step_origin["inlined"] = True
    inlined = worker.returned_value(worker.runtime, [frame("caller", "0x31", 9)], None)
    assert "inlined into its caller" in inlined["reason"]
    assert "--capture return" not in inlined["reason"]
    worker = stopped(tmp_path, Native())
    worker.step_origin["unwatched"] = "no call instruction comes before where it returns"
    unwatched = worker.returned_value(worker.runtime, RETURNED, VALUE)
    assert unwatched["unavailable"] and "no call instruction" in unwatched["reason"]


ARM64 = """p`main:
    0x100000490 <+0>:   sub    sp, sp, #0x40
    0x100000494 <+4>:   cbz    w8, 0x1000004a8           ; <+24> at p.cpp:16:25
    0x100000498 <+8>:   blr    x8
    0x10000049c <+12>:  str    w0, [sp, #0x8]
    0x1000004a0 <+16>:  br     x9
    0x1000004a4 <+20>:  b      0x10000049c               ; <+12> at p.cpp:16
    0x1000004a8 <+24>:  bl     0x100000400               ; f at p.cpp:3
->  0x1000004ac <+28>:  tbz    w8, #0x0, 0x10000049c
    0x1000004b0 <+32>:  ret
"""
X86 = """a.out`main:
    0x401130 <+0>:  pushq  %rbp
    0x401131 <+1>:  movq   %rsp, %rbp
    0x401134 <+4>:  callq  0x401110                  ; f at a.c:3
    0x401139 <+9>:  jne    0x401134                  ; <+4> at a.c:9
    0x40113b <+11>: notrack jmpq *%rax
    0x40113e <+14>: jmp    0x401139
    0x401140 <+16>: retq
"""


def test_every_way_back_to_where_a_call_returns_is_watched():
    # The call, each branch to its return address, and each computed jump.
    assert _return_paths(ARM64, 0x10000049C) == [0x100000498, 0x1000004A0, 0x1000004A4, 0x1000004AC]
    assert _return_paths(ARM64, 0x1000004AC) == [0x1000004A8, 0x1000004A0]
    assert _return_paths(X86, 0x401139) == [0x401134, 0x40113B, 0x40113E]
    # No call just before it (an inlined frame's address), or not an instruction there.
    assert _return_paths(X86, 0x40113E) is None
    assert _return_paths(X86, 0x40113A) is None
    assert _return_paths("", 0x401139) is None


def test_a_python_probe_of_returns_is_refused_before_running(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\nf()\n")
    result = run_probe(tmp_path, Probe("a.py", 2, ("return",)))
    error = result["error"]
    # debugpy would write it into the program's globals (__pydevd_ret_val_dict).
    assert (error["code"], result["status"]) == ("UNSUPPORTED", "failed")
    assert "__pydevd_ret_val_dict" in error["message"] and not result.get("rows")
    # Refused before anything ran: no empty run is kept as evidence.
    assert result["refused_before_launch"] is True
    assert Probe("a.c", 2, ("return", "x")).capture == (RETURN, "x")
    with pytest.raises(ValueError, match="Invalid selector"):
        Probe("a.c", 2, ("return x",))


def below(depth):
    """The frames a call at `depth` returns into: recursive `fact` calls over `main`."""
    return [frame("fact", "0xf6", 6)] * (depth - 2) + [frame("main", "0x99", 9)]


def test_a_refused_python_probe_of_returns_keeps_no_run(monkeypatch, tmp_path, capsys):
    # Round 1 of #162: the refusal saved an empty run in the evidence.
    import json

    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "a.py").write_text("def f():\n    return 1\nf()\n")
    command = ["probe", str(tmp_path), "--at", "a.py:2", "--capture", "return", "--json"]
    assert main(command) == 1
    shown = json.loads(capsys.readouterr().out)
    assert shown["error"]["code"] == "UNSUPPORTED" and "evidence" not in shown
    assert not (tmp_path / "state" / "evidence" / "evidence.db").exists()


class Scripted:
    """A worker whose program stops as scripted: ("hit", depth), ("step", depth,
    returned), ("set", depth) (a --set-at line's stop) or ("end",); a stop may end with
    {"thread": n} (else thread 1). Records how each stop was resumed; `refuse` names
    steps the worker refuses because other threads stopped too."""

    def __init__(self, script, optimized=None, refuse=()):
        self.script, self.optimized, self.resumed = list(script), optimized, []
        self.adapter = object()
        self.current = None
        self.refuse, self.steps = set(refuse), 0

    def dispatch(self, operation, args):
        if operation == "launch":
            return {
                "session_id": "s",
                "breakpoints": [{"verified": True, "line": 4}],
                "artifact_manifest_id": "m",
                "target_identity": {"language": "c"},
                "source_sha256": "0" * 64,
            }
        if operation == "wait":
            self.current = self.script.pop(0)
            if self.current[0] == "end":
                return {"state": "terminated", "stop_id": None}
            kind, last = self.current[0], self.current[-1]
            thread = last["thread"] if isinstance(last, dict) and "thread" in last else 1
            body = {"reason": "step" if kind == "step" else "breakpoint", "threadId": thread}
            return {"state": "stopped", "stop_id": "x", "event": {"body": body}}
        if operation == "inspect":
            depth = self.current[1]
            if self.current[0] in ("hit", "set"):
                line = 4 if self.current[0] == "hit" else 5
                return {
                    "frames": [{**frame("fact", "0xf4", line), "source": {"path": "a.c"}}]
                    + below(depth - 1),
                    "scopes": [],
                    "depth": depth,
                    **({"optimized": self.optimized} if self.optimized else {}),
                }
            return {
                "frames": [{**f, "source": {"path": "a.c"}} for f in below(depth)],
                "scopes": [],
                "depth": depth,
                "location": "a.c:9",
                "returned": self.current[2],
            }
        if operation == "step":
            self.steps += 1
            if self.steps in self.refuse:
                raise ValueError(
                    "Other threads stopped at this breakpoint too and are still paused there"
                )
        if operation in ("step", "continue"):
            self.resumed.append(operation)
        if operation == "report":
            return {"state": "terminated", "output": "", "output_truncated": False}
        return {}

    def close(self):
        return None


def probe_with(monkeypatch, tmp_path, script, optimized=None, refuse=(), **options):
    worker = Scripted(script, optimized, refuse)
    monkeypatch.setattr("peeky.probe.DebugWorker", lambda root, adapter=None: worker)
    result = run_probe(tmp_path, Probe("a.c", 4, ("return",), hits="all", **options))
    returns = {row["hit"]: row for row in result["rows"] if row["selector"] == RETURN}
    return result, returns, worker.resumed


def value(text):
    return {"function": "fact", "type": "int", "value": text}


def test_recursive_calls_each_get_their_own_return(monkeypatch, tmp_path):
    script = [("hit", 3), ("hit", 4), ("step", 3, value("1")), ("step", 2, value("2")), ("end",)]
    result, returns, resumed = probe_with(monkeypatch, tmp_path, script)
    assert result["status"] == "completed" and result["hits"] == 2
    # The inner call returns first, into the outer call's frame: step out again.
    assert (returns[2]["value"], returns[1]["value"]) == ("1", "2")
    assert returns[1]["line"] == 9 and returns[1]["function"] == "fact"
    assert resumed == ["step", "step", "step", "continue"]


def test_a_line_reached_again_before_the_return_is_never_attributed(monkeypatch, tmp_path):
    # A loop's next pass, or a call that ended by an exception and the next call: the
    # stack cannot tell them apart, so the earlier hit gets no value.
    script = [("hit", 3), ("hit", 3), ("step", 2, value("7")), ("end",)]
    _, returns, _ = probe_with(monkeypatch, tmp_path, script)
    assert "value" not in returns[1] and "ran again at the same depth" in returns[1]["error"]
    assert returns[2]["value"] == "7"


def test_returns_that_never_came_are_withheld(monkeypatch, tmp_path):
    # The inner call's step out ended two frames up (both frames gone without a return).
    script = [("hit", 3), ("hit", 4), ("step", 2, value("5")), ("end",)]
    _, returns, _ = probe_with(monkeypatch, tmp_path, script)
    assert "ended 2 frames up" in returns[2]["error"] and "value" not in returns[2]
    assert "did not return normally" in returns[1]["error"]
    # The program ended inside the call.
    _, returns, _ = probe_with(monkeypatch, tmp_path, [("hit", 3), ("end",)])
    assert "ended before it returned" in returns[1]["error"]


def test_another_threads_stop_withholds_and_the_probe_goes_on(monkeypatch, tmp_path):
    # Thread 2 reached the line while thread 1's call was being stepped out of.
    script = [("hit", 3), ("hit", 3, {"thread": 2}), ("step", 2, value("7"), {"thread": 2})]
    result, returns, _ = probe_with(monkeypatch, tmp_path, [*script, ("end",)])
    assert result["status"] == "completed" and returns[2]["value"] == "7"
    assert "another thread" in returns[1]["error"] and "value" not in returns[1]
    # Thread 1's step then ended after all: nothing pending is on it, nothing is read.
    script = [("hit", 3), ("hit", 3, {"thread": 2}), ("step", 2, value("9")), ("end",)]
    result, returns, resumed = probe_with(monkeypatch, tmp_path, script)
    assert result["status"] == "completed" and "value" not in returns[2]
    assert "another thread" in returns[2]["error"] and resumed[-1] == "continue"


def test_a_step_refused_for_other_threads_withholds_only_that_return(monkeypatch, tmp_path):
    # Both threads stopped at the line together: the first call is not stepped out of
    # (the worker serves the other stop first); its row is withheld, the probe goes on.
    script = [("hit", 3), ("hit", 3, {"thread": 2}), ("step", 2, value("7"), {"thread": 2})]
    result, returns, resumed = probe_with(monkeypatch, tmp_path, [*script, ("end",)], refuse={1})
    assert result["status"] == "completed" and "error" not in result
    assert "another thread" in returns[1]["error"] and returns[2]["value"] == "7"
    assert "session continue" not in str(result)
    assert resumed[:2] == ["continue", "step"]


def test_a_return_into_another_frame_is_never_credited(monkeypatch, tmp_path):
    # Back above the pending call's frame at a breakpoint: that call is gone.
    script = [("hit", 4), ("hit", 3), ("step", 2, value("5")), ("end",)]
    _, returns, _ = probe_with(monkeypatch, tmp_path, script)
    assert "frame was gone" in returns[1]["error"] and returns[2]["value"] == "5"


def test_optimized_builds_capture_no_returns(monkeypatch, tmp_path):
    _, returns, resumed = probe_with(monkeypatch, tmp_path, [("hit", 3), ("end",)], "-O2")
    assert "optimized build (-O2)" in returns[1]["error"] and resumed == ["continue"]


def test_a_return_row_is_stored_and_queried_with_the_hits(tmp_path):
    from peeky import evidence

    store = tmp_path / "evidence.db"
    rows = [
        {"hit": 1, "phase": "observed", "line": 16, "selector": "a.prio", "value": "2"},
        {
            "hit": 1,
            "phase": "returned",
            "line": 115,
            "function": "before(Task const&, Task const&)",
            "selector": RETURN,
            "type": "bool",
            "value": "false",
        },
    ]
    result = {
        "probe": {"program": "sched.cpp", "line": 16, "capture": ["a.prio", RETURN]},
        "status": "completed",
        "hits": 1,
        "rows": rows,
        "output": "",
        "target_identity": {"language": "cpp"},
    }
    evidence.record(store, result, language="cpp")
    found = evidence.run_query(store, "hits", {"selector": RETURN})["rows"]
    assert [(row["hit"], row["line"], row["value"]) for row in found] == [(1, 115, "false")]
    wide = evidence.run_query(store, "wide", {})["rows"]
    assert [row["phase"] for row in wide] == ["observed", "returned"]
    with pytest.raises(ValueError) as caught:
        evidence.run_query(store, "wide", {"phase": "later"})
    assert classify(caught.value)["message"].endswith("not 'later'")


def test_older_lldb_dap_returns_come_from_lldbs_own_record():
    # lldb-dap before LLVM 21 lists no `(Return Value)`; `thread info` still says it.
    class Dap:
        def __init__(self, text):
            self.text = text

        def request(self, command, arguments, timeout=None):
            assert arguments == {"expression": "`thread info", "context": "repl"}
            return {"result": self.text}

    linux = (
        "thread #1: tid = 4242, 0x0000aaaaaaaa07a4 ret`main at ret.c:36:12, name = 'ret', "
        "stop reason = step out\nReturn value: (double) $0 = 3.5\n"
    )
    found = NativeSession.returned_by_command(Dap(linux), 4242)
    assert found == {"name": "(Return Value)", "type": "double", "value": "3.5"}
    mac = "thread #1: tid = 0x2f8b4b8, 0x1 a`b, stop reason = step out\nReturn value: (bool) $3 = false"
    assert NativeSession.returned_by_command(Dap(mac), 0x2F8B4B8)["value"] == "false"
    # Another thread's record, or none: nothing.
    assert NativeSession.returned_by_command(Dap(linux), 4243) is None
    assert (
        NativeSession.returned_by_command(
            Dap("thread #1: tid = 4242, stop reason = step out"), 4242
        )
        is None
    )
