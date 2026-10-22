"""Session stops compared with where their step started (tenth evaluation): a step's stop
in another function or at another depth says so, in words the stacks prove; a step that
left a block says so when a name it declared is read; and `session start` warns when it
stopped at a breakpoint the debugger moved."""

import pytest

from peeky.cli import _call_text, _moved_start, _state_text
from peeky.worker import CALL_FRAMES, DebugWorker, call_change


def stack(*frames):
    """`_frame_identity`s, innermost first, from "name@line" or (name, pc, line)."""
    return [
        frame if isinstance(frame, tuple) else (frame.rsplit("@", 1)[0], None, int(frame[-2:]))
        for frame in frames
    ]


def test_the_same_function_at_the_same_depth_with_the_same_callers_is_the_same_stack():
    before = stack("count@79", "main@96")
    assert call_change(before, stack("count@76", "main@96")) == {"kind": "same"}


def test_a_shallower_stack_over_the_earlier_callers_left_the_function():
    before = stack("rename@54", "run@90", "main@98", "<module>@07")
    after = stack("run@90", "main@98", "<module>@07")
    assert call_change(before, after) == {"kind": "left", "from": "rename", "to": "run", "calls": 1}
    deeper = stack("index@33", "rename@52", "run@90", "main@98")
    assert call_change(deeper, stack("run@91", "main@98"))["calls"] == 2
    # The callers below moved on: the stacks say only that `rename` is no longer there.
    assert call_change(before, stack("run@91", "main@99", "<module>@07")) == {
        "kind": "other",
        "from": "rename",
        "to": "run",
    }


def test_a_deeper_stack_over_the_earlier_function_is_deeper_recursion_too():
    before = stack("walk@08", "run@13", "<module>@18")
    after = stack("walk@06", "walk@08", "run@13", "<module>@18")
    assert call_change(before, after) == {
        "kind": "deeper",
        "from": "walk",
        "to": "walk",
        "calls": 1,
    }
    # A name alone would call this the same function: its depth says otherwise.
    assert call_change(after, before)["kind"] == "left"
    two = stack("index@33", "rename@52", "run@90", "<module>@18")
    assert call_change(stack("run@90", "<module>@18"), two)["calls"] == 2


def test_another_function_at_the_earlier_depth_is_other():
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
    assert call_change(before, back)["kind"] == "left"


def test_state_lines_say_only_what_the_stacks_show():
    # Review of #274: "returned from" was said of a generator that yielded and a C++
    # function an exception unwound.
    left = {"kind": "left", "from": "boom()", "to": "main", "calls": 1}
    assert _call_text(left) == "left `boom()`; now in `main`"
    assert _call_text({**left, "calls": 3}) == "left `boom()`; now in `main`, 3 calls up"
    deeper = {"kind": "deeper", "from": "walk", "to": "walk", "calls": 1}
    assert _call_text(deeper) == "now in `walk`, one call deeper, called from `walk`"
    assert (
        _call_text({**deeper, "to": "h", "calls": 2}) == "now in `h`, 2 calls deeper, below `walk`"
    )
    other = {"kind": "other", "from": "add", "to": "shift"}
    assert _call_text(other) == "left `add`; now in `shift`"
    for kind in ("same", "returned", "entered", "later"):
        assert _call_text({**left, "kind": kind}) is None
    # `session get`'s place note.
    assert _call_text(left, since=True) == "after a step from `boom()`"
    assert _call_text({**left, "calls": 2}, since=True) == "after a step from `boom()`, 2 calls up"
    assert _call_text(deeper, since=True) == "after a step from `walk`, one call deeper"
    assert (
        _call_text({**deeper, "calls": 4}, since=True) == "after a step from `walk`, 4 calls deeper"
    )
    assert _call_text(other, since=True) == "after a step from `add`"
    # Rust frames lose their hash, as the state line shows them.
    rust = {"kind": "deeper", "from": "kitty::main::h0123456789abcdef", "calls": 1}
    rust["to"] = "kitty::settle::h0123456789abcdef"
    assert _call_text(rust) == "now in `kitty::settle`, one call deeper, called from `kitty::main`"
    state = {
        "state": "stopped",
        "location": "/nonexistent-peeky-root/outline.py:90",
        "function": "run",
        "call_change": {**left, "from": "rename", "to": "run"},
        "note": "a later note",
    }
    assert _state_text(state).splitlines() == [
        "state: stopped at /nonexistent-peeky-root/outline.py:90 in run",
        "note: left `rename`; now in `run`",
        "note: a later note",
    ]
    assert "note" not in _state_text({**state, "call_change": {"kind": "same"}, "note": None})


MOVED = (
    "The debugger moved the breakpoint from line 55 to line 54 (line 55 has no code of its "
    "own); values are from line 54. Choose a line from `peeky describe`."
)


def test_a_moved_start_breakpoint_warns_only_after_a_stop_there():
    launch = {"breakpoints": [{"id": 3, "verified": True, "line": 54}]}
    stop = {
        "state": "stopped",
        "reason": "breakpoint",
        "location": "/nonexistent-peeky-root/app/outline.py:54",
    }
    assert _moved_start(launch, stop, 55, "app/outline.py") == MOVED
    assert _moved_start(launch, {**stop, "location": "app/outline.py:54"}, 55, "app/outline.py")
    assert _moved_start(launch, stop, 54, "app/outline.py") is None
    # Review of #274: "values are from line 54" of a run that never stopped there.
    for state in (
        {"state": "terminated"},
        {"state": "running"},
        {**stop, "reason": "exception"},
        {**stop, "location": "/nonexistent-peeky-root/app/outline.py:60"},
        {**stop, "location": "/nonexistent-peeky-root/app/other.py:54"},
    ):
        assert _moved_start(launch, state, 55, "app/outline.py") is None, state
    # A later `breakpoint` event moves it (a class loaded after the launch).
    pending = {"breakpoints": [{"id": 3, "verified": False, "line": 12}]}
    moved = {
        **stop,
        "location": "/nonexistent-peeky-root/app/outline.py:13",
        "breakpoint_events": [{"reason": "changed", "breakpoint": {"id": 3, "line": 13}}],
    }
    assert "from line 12 to line 13" in _moved_start(pending, moved, 12, "app/outline.py")
    assert _moved_start({"breakpoints": []}, stop, 55, "app/outline.py") is None


class Recorder:
    def __init__(self, frames, total="all"):
        self.frames, self.total, self.requests = frames, total, []

    def request(self, command, arguments=None, timeout=None):
        self.requests.append((command, arguments))
        reply = {"stackFrames": self.frames[: arguments["levels"]]}
        if self.total == "all":
            reply["totalFrames"] = len(self.frames)
        elif self.total is not None:
            reply["totalFrames"] = self.total
        return reply


def stopped(worker, reason="step", thread=1):
    worker.forget_calls()
    worker.stop_id, worker.thread_id, worker.stop_reason = "s", thread, reason


def test_only_a_session_steps_own_stop_is_compared(tmp_path):
    worker = DebugWorker(tmp_path)
    worker.dap = Recorder([{"name": "f", "line": 3}, {"name": "main", "line": 9}])
    stopped(worker, "breakpoint")
    # A probe's stops (no `calls` step) cost no request.
    worker.compare_calls()
    assert worker.dap.requests == [] and worker.call_change is None
    worker.stop_names, worker.stop_line = {"x", "y"}, 3
    worker.mark_calls()
    assert worker.dap.requests == [
        ("stackTrace", {"threadId": 1, "startFrame": 0, "levels": CALL_FRAMES}),
    ]
    stopped(worker)
    worker.dap.frames = [{"name": "f", "line": 4}, {"name": "main", "line": 9}]
    worker.compare_calls()
    # The same function at the same depth: the names listed at line 3 are kept.
    assert worker.call_change is None
    assert worker.names_before == {"names": frozenset({"x", "y"}), "line": 3}
    # The mark is used once.
    stopped(worker)
    worker.compare_calls()
    assert worker.names_before is None
    # Review of #274: a stop a breakpoint, exception or pause made first, or another
    # thread's, may be in another activation that looks the same: nothing is said.
    for reason, thread in (("breakpoint", 1), ("exception", 1), ("pause", 1), ("step", 2)):
        stopped(worker)
        worker.dap.frames = [{"name": "f", "line": 3}, {"name": "main", "line": 9}]
        worker.mark_calls()
        stopped(worker, reason, thread)
        worker.dap.frames = [
            {"name": "g", "line": 1},
            {"name": "f", "line": 3},
            {"name": "main", "line": 9},
        ]
        worker.compare_calls()
        assert worker.call_change is None and worker.names_before is None, reason
    stopped(worker)
    worker.dap.frames = [{"name": "f", "line": 3}, {"name": "main", "line": 9}]
    worker.mark_calls()
    stopped(worker)
    worker.dap.frames = [
        {"name": "g", "line": 1},
        {"name": "f", "line": 3},
        {"name": "main", "line": 9},
    ]
    worker.compare_calls()
    assert worker.call_change == {"kind": "deeper", "from": "f", "to": "g", "calls": 1}


@pytest.mark.parametrize(
    "frames, total",
    [
        (CALL_FRAMES + 40, "all"),  # deeper than the bound: the reply is cut
        (CALL_FRAMES, CALL_FRAMES + 20),  # lldb-dap: more frames follow
        (3, None),  # no total: not known to be whole
        (3, 7),
    ],
)
def test_a_stack_not_known_to_be_whole_gives_no_note(tmp_path, frames, total):
    # Review of #274: `levels: 0` on a deep recursion could pass the 4 MiB frame limit.
    worker = DebugWorker(tmp_path)
    worker.dap = Recorder([{"name": "deep", "line": 4}] * frames, total)
    stopped(worker)
    worker.mark_calls()
    assert worker.calls_from is None
    assert worker.dap.requests[0][1]["levels"] == CALL_FRAMES
    worker.dap.frames = [{"name": "deep", "line": 3}, *worker.dap.frames]
    stopped(worker)
    worker.compare_calls()
    assert worker.call_change is None


def test_only_a_step_takes_calls():
    assert DebugWorker.validate("step", {"calls": True}) is None
    with pytest.raises(ValueError, match="calls must be a boolean"):
        DebugWorker.validate("step", {"calls": "yes"})
