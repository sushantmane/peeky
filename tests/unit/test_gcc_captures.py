"""A GCC lambda's captures, read through `__closure` (#361): which member of the lambda's
object holds each, how the worker lists them in a scope of their own, and that none is
ever changed. Real builds: tests/cpp/test_cpp_gcc_captures.py."""

import pytest

from peeky.native import NativeSession, _gcc_capture_members
from peeky.probe import unproven_text
from peeky.worker import _CLOSURE_HOVER, CAPTURES_SCOPE, DebugWorker


def die(tag, parent=None, children=(), **attrs):
    return {"tag": tag, "parent": parent, "attrs": attrs, "children": list(children)}


def closure_dies(changes=None):
    """GCC 13's debug information for `[&grade, bonus, pt]` (by reference, by value, a
    struct by value), as llvm-dwarfdump writes it, with ``changes`` to some DIEs' attrs."""

    def kind(offset, name):
        return f'0x{offset:08x} "{name}"'

    def member(name, offset, at):
        return die(
            "DW_TAG_member",
            0x0A,
            DW_AT_name=f'"{name}"',
            DW_AT_type=kind(offset, name),
            DW_AT_data_member_location=f"0x{at:02x}",
        )

    def capture(name, offset, location):
        return die(
            "DW_TAG_variable",
            0x16,
            DW_AT_name=f'"{name}"',
            DW_AT_type=kind(offset, name),
            DW_AT_artificial="true",
            DW_AT_location=location,
        )

    through = "DW_OP_fbreg -24, DW_OP_deref"
    dies = {
        0x01: die("DW_TAG_compile_unit"),
        # The lambda's object: its members at their offsets, and its call operator.
        0x0A: die("DW_TAG_structure_type", 0x01, (0x11, 0x12, 0x13, 0x14, 0x15, 0x16)),
        0x11: member("__grade", 0x40, 0x00),
        0x12: member("__bonus", 0x50, 0x08),
        0x13: member("__pt", 0x60, 0x10),
        0x14: member("__n", 0x50, 0x20),
        0x15: member("__m", 0x50, 0x20),
        0x16: die(
            "DW_TAG_subprogram",
            0x0A,
            (0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F),
            DW_AT_name='"operator()"',
            DW_AT_object_pointer="0x00000017",
        ),
        0x17: die(
            "DW_TAG_formal_parameter",
            0x16,
            DW_AT_name='"__closure"',
            DW_AT_type=kind(0x30, "const structure  *const"),
            DW_AT_artificial="true",
            DW_AT_location="DW_OP_fbreg -24",
        ),
        0x18: die(
            "DW_TAG_formal_parameter", 0x16, DW_AT_name='"a"', DW_AT_location="DW_OP_fbreg -32"
        ),
        # Each capture: an artificial variable at its member's offset in `*__closure`, of
        # its type, `const` where the lambda is not `mutable`.
        0x19: capture("bonus", 0x52, f"{through}, DW_OP_plus_uconst 0x8"),
        0x1A: capture("grade", 0x40, through),
        0x1B: capture("pt", 0x62, f"{through}, DW_OP_plus_uconst 0x10"),
        # Two members at one offset: which one holds it is not told.
        0x1C: capture("n", 0x50, f"{through}, DW_OP_plus_uconst 0x20"),
        # lldb-dap lists GCC's `this`.
        0x1D: capture("this", 0x70, through),
        # A local of the lambda's own is no capture.
        0x1E: die("DW_TAG_variable", 0x16, DW_AT_name='"ga"', DW_AT_location="DW_OP_fbreg -12"),
        # Not read through `__closure`.
        0x1F: capture("odd", 0x50, "DW_OP_fbreg -40, DW_OP_deref"),
        0x30: die("DW_TAG_const_type", 0x01, DW_AT_type=kind(0x31, "const structure  *")),
        0x31: die("DW_TAG_pointer_type", 0x01, DW_AT_type=kind(0x32, "const structure ")),
        0x32: die("DW_TAG_const_type", 0x01, DW_AT_type=kind(0x0A, "structure ")),
        0x40: die("DW_TAG_reference_type", 0x01, DW_AT_type=kind(0x41, "map")),
        0x41: die("DW_TAG_structure_type", 0x01, DW_AT_name='"map"'),
        0x50: die("DW_TAG_base_type", 0x01, DW_AT_name='"int"'),
        0x52: die("DW_TAG_const_type", 0x01, DW_AT_type=kind(0x50, "int")),
        0x60: die("DW_TAG_structure_type", 0x01, DW_AT_name='"Pt"'),
        0x62: die("DW_TAG_const_type", 0x01, DW_AT_type=kind(0x60, "Pt")),
        0x70: die("DW_TAG_pointer_type", 0x01),
    }
    for offset, attrs in (changes or {}).items():
        dies[offset]["attrs"].update(attrs)
    return dies


def test_each_capture_is_the_member_at_its_offset_in_what_closure_points_at():
    entry = {"chain": [0x16], "bodies": [0x16]}
    # In the object's order, with the variable's own type (`const` where not `mutable`).
    assert _gcc_capture_members(closure_dies(), entry) == (
        ("grade", "__grade", 0x40),
        ("bonus", "__bonus", 0x52),
        ("pt", "__pt", 0x62),
    )

    def names(dies):
        return [name for name, _, _ in _gcc_capture_members(dies, entry)]

    # A variable of another type than its member's (not just made `const`) is left out.
    assert names(closure_dies({0x19: {"DW_AT_type": '0x00000040 "map &"'}})) == ["grade", "pt"]
    # So is one at an offset no member is at.
    moved = "DW_OP_fbreg -24, DW_OP_deref, DW_OP_plus_uconst 0x18"
    assert names(closure_dies({0x1B: {"DW_AT_location": moved}})) == ["grade", "bonus"]
    # And one declared twice: which one the code names is not told.
    dies = closure_dies()
    dies[0x16]["children"].append(0x20)
    dies[0x20] = {**dies[0x19], "attrs": {**dies[0x19]["attrs"]}}
    assert names(dies) == ["grade", "pt"]
    # `__closure` kept elsewhere than one frame offset (a register, a location list): no
    # capture is read.
    assert names(closure_dies({0x17: {"DW_AT_location": "DW_OP_reg0 X0"}})) == []
    # clang names a lambda's object pointer `this`, and declares no such variables.
    assert names(closure_dies({0x17: {"DW_AT_name": '"this"'}})) == []


def test_a_hover_of_closure_gives_its_address_in_either_lldb_dap():
    held = _CLOSURE_HOVER.fullmatch(
        "(const (unnamed struct) *const) __closure = 0x0000fffffffff8f0"
    )
    assert held[1] == "0x0000fffffffff8f0"
    assert _CLOSURE_HOVER.fullmatch("0x0000fffffffff8f0")[1] == "0x0000fffffffff8f0"
    assert _CLOSURE_HOVER.fullmatch("(const int) bonus = 2") is None
    assert _CLOSURE_HOVER.fullmatch("<error: no value>") is None


def test_a_capture_by_reference_refers_to_what_it_reads():
    why = {"kind": "other", "statics": True}
    note = unproven_text("count", why, reference=True)
    assert note.startswith(
        "read through `count`: Peeky can't show that `count` refers to a live object, so "
        "this value may be left over: its address is in no local"
    )
    assert "points at" in unproven_text("p", why)


class _Dap:
    """lldb-dap's replies at a stop in a GCC lambda: a hover of `__closure`, and the
    members of what it points at."""

    def __init__(self, hover):
        self.hover, self.asked = hover, []

    def request(self, command, arguments=None, timeout=15, **_):
        self.asked.append((command, arguments))
        if command == "evaluate":
            assert arguments["expression"] == "__closure"
            assert arguments["context"] == "hover"
            return {
                "result": self.hover,
                "type": "const (unnamed struct) *const",
                "variablesReference": 9,
            }
        assert command == "variables" and arguments["variablesReference"] == 9
        return {
            "variables": [
                {
                    "name": "__grade",
                    "value": "0x0000fffffffffa88 size=2",
                    "type": "std::map<int, int> &",
                    "variablesReference": 10,
                },
                {"name": "__bonus", "value": "2", "type": "int", "variablesReference": 0},
                {"name": "__twice", "value": "1", "type": "int", "variablesReference": 0},
                {"name": "__twice", "value": "2", "type": "int", "variablesReference": 0},
                {"name": "__k", "value": "5", "type": "int", "variablesReference": 0},
            ]
        }


class _Lambda:
    """A GCC lambda's runtime, with no rule but the captures' own."""

    reports_unavailable_as_text = True

    def __init__(self, unread=None):
        self.unread = unread

    def capture_members(self, frame):
        return [("grade", "__grade"), ("bonus", "__bonus"), ("twice", "__twice"), ("k", "__k")]

    def captures_unread(self, frame):
        return self.unread

    def capture_referent(self, frame, name):
        return None

    def assert_current(self):
        pass

    def validate_member(self, *args, **kwargs):
        raise AssertionError("a capture's member is never judged for a change")

    capture_refusal = NativeSession.capture_refusal
    never_settable_row = NativeSession.never_settable_row


def _worker(tmp_path, runtime, hover="(const (unnamed struct) *const) __closure = 0x0000fffff8f0"):
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.dap, worker.stop_id = runtime, _Dap(hover), "s1"
    worker.capabilities = {"supports_set_variable": True, "supports_member_set": True}
    worker.innermost_frame = 3
    return worker


def test_the_worker_lists_the_captures_it_reads_through_closure_and_changes_none(tmp_path):
    worker = _worker(tmp_path, _Lambda())
    frame = {"id": 3, "name": "operator()"}
    # `k` is a local the frame lists (an inner block's): the code names that one here.
    scope = worker.read_captures(worker.runtime, frame, ["a", "k @ lam.cpp:30"])
    assert scope["name"] == "Captures" and scope["reference"] in worker.capture_handles
    rows = {row["name"]: row for row in scope["variables"]}
    # A member the debugger lists twice is left out; so is the shadowed `k`.
    assert sorted(rows) == ["bonus", "grade"]
    assert rows["bonus"]["value"] == "2" and rows["bonus"]["settable"] is False
    assert rows["bonus"]["not_settable"] == "capture"
    assert rows["grade"]["value"] == "0x0000fffffffffa88 size=2"
    assert all("peekyCapture" not in row for row in rows.values())
    # Its children are found again through `__closure`, by the member and its type.
    record = worker.children[rows["grade"]["variablesReference"]]
    assert record[1] == CAPTURES_SCOPE
    trail = []
    found = worker.walk_capture(worker.runtime, frame, record[2], trail)
    assert found[3] == 10 and trail[0]["name"] == "grade" and trail[0]["peekyCapture"] is True
    with pytest.raises(ValueError, match="ancestry changed"):
        worker.walk_capture(worker.runtime, frame, (("grade", "std::map<long, int> &"),))
    # No change is made: the refusal says why, before the debugger is asked.
    asked, worker.state = len(worker.dap.asked), "stopped"
    change = {"stop_id": "s1", "reference": scope["reference"], "name": "bonus", "value": "5"}
    with pytest.raises(ValueError) as refused:
        worker._dispatch("set_variable", change)
    assert str(refused.value).startswith(
        "'bonus' cannot be changed: `bonus` is a capture of this lambda, so it is reached "
        "through a pointer or reference: GCC keeps it in the lambda's object"
    )
    # Nor a member of one, through its handle.
    worker.children[11] = (frame, CAPTURES_SCOPE, (("pt", "Pt"),))
    with pytest.raises(ValueError, match=r"^`pt.x` cannot be changed: `pt` is a capture"):
        worker._dispatch("set_variable", {"stop_id": "s1", "handle": 11, "name": "x", "value": "5"})
    assert len(worker.dap.asked) == asked


def test_no_capture_is_read_where_the_runtime_says_why_or_the_debugger_gives_no_address(tmp_path):
    frame = {"id": 3, "name": "operator()"}
    # A stop before the lambda stores `__closure`, or an optimized build: not even asked.
    worker = _worker(tmp_path, _Lambda(unread="the lambda has not stored `__closure` at this stop"))
    assert worker.read_captures(worker.runtime, frame, []) is None
    assert worker.dap.asked == []
    worker = _worker(tmp_path, _Lambda(), hover="<error: invalid value object>")
    assert worker.read_captures(worker.runtime, frame, []) is None


def test_a_capture_the_lambda_also_declares_with_another_type_is_not_read(monkeypatch):
    # The rules that type a value by its root's name (previews, a container a library
    # call may be changing) couldn't tell which declaration `bonus` is.
    entry = {
        "capture_members": (("grade", "__grade", 0x40), ("bonus", "__bonus", 0x52)),
        "types": {
            "grade": frozenset({(0x40, "through", None)}),
            "bonus": frozenset({(0x52, "through", None), (0x50, "automatic", ("l.cpp", 9))}),
        },
    }
    monkeypatch.setattr(NativeSession, "_function_at", lambda self, frame: entry)
    assert NativeSession.__new__(NativeSession).capture_members({}) == [("grade", "__grade")]
