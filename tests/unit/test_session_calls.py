"""Session stops compared with where their step or continue started (tenth evaluation):
another call is named on the state line, a step that left a block says so when a name
it declared is read, and `session start` warns when its breakpoint was moved."""

from peeky.cli import _call_text, _moved_start, _state_text
from peeky.worker import DebugWorker, call_change


def stack(*frames):
    """`_frame_identity`s, innermost first, from "name@line" or (name, pc, line)."""
    return [
        frame if isinstance(frame, tuple) else (frame.rsplit("@", 1)[0], None, int(frame[-2:]))
        for frame in frames
    ]


def test_the_same_function_at_the_same_depth_with_the_same_callers_is_the_same_call():
    before = stack("count@79", "main@96")
    assert call_change(before, stack("count@76", "main@96")) == {"kind": "same"}


def test_a_shorter_stack_whose_frames_were_the_callers_is_a_return():
    before = stack("rename@54", "run@90", "main@98", "<module>@07")
    after = stack("run@90", "main@98", "<module>@07")
    assert call_change(before, after) == {
        "kind": "returned",
        "from": "rename",
        "to": "run",
        "calls": 1,
    }
    deeper = stack("index@33", "rename@52", "run@90", "main@98")
    assert call_change(deeper, stack("run@91", "main@98"))["calls"] == 2
    # The callers below moved on: the call returned, and this stop is in another one.
    assert call_change(before, stack("run@91", "main@99", "<module>@07")) == {
        "kind": "other",
        "from": "rename",
        "to": "run",
    }


def test_a_deeper_stack_over_the_earlier_frame_is_a_call_it_made_recursion_too():
    before = stack("walk@08", "run@13", "<module>@18")
    after = stack("walk@06", "walk@08", "run@13", "<module>@18")
    assert call_change(before, after) == {
        "kind": "entered",
        "from": "walk",
        "to": "walk",
        "calls": 1,
    }
    # A name alone would call this the same function: its depth says otherwise.
    assert call_change(after, before)["kind"] == "returned"
    two = stack("index@33", "rename@52", "run@90", "<module>@18")
    assert call_change(stack("run@90", "<module>@18"), two)["calls"] == 2


def test_another_function_at_the_earlier_depth_is_another_call():
    before = stack("rename@02", "run@12", "<module>@18")
    after = stack("walk@06", "run@13", "<module>@18")
    assert call_change(before, after) == {"kind": "other", "from": "rename", "to": "walk"}
    assert call_change(before, stack("index@33", "add@38", "run@82", "<module>@18")) == {
        "kind": "other",
        "from": "rename",
        "to": "index",
    }


def test_the_same_function_at_the_earlier_depth_with_other_callers_is_not_told():
    # Another call, or a Python generator resumed from another line: nothing is said.
    before = stack("numbers@03", "main@10", "<module>@20")
    assert call_change(before, stack("numbers@04", "main@12", "<module>@20")) is None
    assert call_change(before, stack("inner@07", "numbers@04", "main@12", "<module>@20")) is None
    assert call_change([], before) is None and call_change(before, None) is None


def test_lldb_callers_are_told_apart_by_their_return_address():
    before = [("settle", "0x10", 35), ("feed", "0x200", 57), ("main", "0x300", 80)]
    same = [("settle", "0x18", 36), ("feed", "0x200", 57), ("main", "0x300", 80)]
    assert call_change(before, same) == {"kind": "same"}
    other = [("settle", "0x10", 35), ("feed", "0x200", 57), ("main", "0x308", 80)]
    assert call_change(before, other) is None
    back = [("feed", "0x200", 57), ("main", "0x300", 80)]
    assert call_change(before, back)["kind"] == "returned"


def test_state_lines_name_the_call_a_stop_is_in():
    returned = {"kind": "returned", "from": "rename", "to": "run", "calls": 1}
    assert _call_text(returned) == "returned from `rename` to `run`"
    assert _call_text({**returned, "calls": 3}) == "returned from `rename` to `run`, 3 calls up"
    entered = {"kind": "entered", "from": "run", "to": "walk", "calls": 1}
    assert _call_text(entered) == "entered `walk`, called from `run`"
    assert _call_text({**entered, "calls": 2}) == "entered `walk`, 2 calls below `run`"
    other = {"kind": "other", "from": "add", "to": "shift"}
    assert _call_text(other) == "returned from `add`, then entered `shift`"
    assert _call_text({"kind": "same"}) is None and _call_text({"kind": "later"}) is None
    # Rust frames lose their hash, as the state line shows them.
    rust = {"kind": "entered", "from": "kitty::main::h0123456789abcdef", "calls": 1}
    rust["to"] = "kitty::settle::h0123456789abcdef"
    assert _call_text(rust) == "entered `kitty::settle`, called from `kitty::main`"
    state = {
        "state": "stopped",
        "location": "/w/outline.py:90",
        "function": "run",
        "call_change": returned,
        "note": "a later note",
    }
    assert _state_text(state).splitlines() == [
        "state: stopped at /w/outline.py:90 in run",
        "note: returned from `rename` to `run`",
        "note: a later note",
    ]
    assert "note" not in _state_text({**state, "call_change": {"kind": "same"}, "note": None})


def test_a_moved_start_breakpoint_warns_as_a_probe_does():
    launch = {"breakpoints": [{"id": 3, "verified": True, "line": 54}]}
    assert _moved_start(launch, {}, 55) == (
        "The debugger moved the breakpoint from line 55 to line 54 (line 55 has no code of "
        "its own); values are from line 54. Choose a line from `peeky describe`."
    )
    assert _moved_start(launch, {}, 54) is None
    # A later `breakpoint` event moves it (a class loaded after the launch).
    pending = {"breakpoints": [{"id": 3, "verified": False, "line": 12}]}
    moved = {"breakpoint_events": [{"reason": "changed", "breakpoint": {"id": 3, "line": 13}}]}
    assert "from line 12 to line 13" in _moved_start(pending, moved, 12)
    assert _moved_start({"breakpoints": []}, {}, 12) is None
    assert _moved_start({"breakpoints": [{"id": 1, "verified": False}]}, {}, 12) is None


class Recorder:
    def __init__(self, frames):
        self.frames, self.requests = frames, []

    def request(self, command, arguments=None, timeout=None):
        self.requests.append((command, arguments))
        return {"stackFrames": self.frames}


def test_only_a_session_step_or_continue_reads_whole_stacks(tmp_path):
    worker = DebugWorker(tmp_path)
    worker.dap = Recorder([{"name": "f", "line": 3}, {"name": "main", "line": 9}])
    worker.stop_id, worker.thread_id, worker.stop_reason = "s1", 1, "breakpoint"
    # A probe's stops (no `calls` on its continue) cost no request.
    worker.compare_calls()
    assert worker.dap.requests == [] and worker.call_change is None
    worker.stop_names, worker.stop_line = {"x", "y"}, 3
    worker.mark_calls("step")
    assert worker.dap.requests == [
        ("stackTrace", {"threadId": 1, "startFrame": 0, "levels": 0}),
    ]
    worker.forget_calls()
    worker.stop_id, worker.stop_reason = "s2", "step"
    worker.dap.frames = [{"name": "f", "line": 4}, {"name": "main", "line": 9}]
    worker.compare_calls()
    # The same call: the names listed at line 3 are kept for `inspect` to compare.
    assert worker.call_change is None
    assert worker.names_before == {"names": frozenset({"x", "y"}), "line": 3}
    # A step whose stop is in another thread's frames is not compared.
    worker.forget_calls()
    worker.thread_id = 2
    worker.compare_calls()
    assert worker.names_before is None and worker.call_change is None
    # After a continue, the same frames may be another call: no names are kept.
    worker.thread_id, worker.stop_names = 1, {"x"}
    worker.mark_calls("continue")
    worker.forget_calls()
    worker.compare_calls()
    assert worker.names_before is None and worker.call_change is None
