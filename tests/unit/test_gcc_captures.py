"""A GCC lambda's captures, read through `__closure` (#361): which member of the lambda's
object holds each, how the worker lists them in a scope of their own, and that none is
ever changed. Real builds: tests/cpp/test_cpp_gcc_captures.py."""

import re

import pytest

from peeky.dap import RequestFailed, ResponseTimeout
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


HIDDEN = "the lambda's own `k`, declared on line 30, hides it here"


class _Lambda:
    """A GCC lambda's runtime, with no rule but the captures' own."""

    reports_unavailable_as_text = True

    def __init__(self, unread=None):
        self.unread = unread

    def capture_members(self, frame):
        return [("grade", "__grade"), ("bonus", "__bonus"), ("twice", "__twice"), ("k", "__k")]

    def capture_plan(self, frame):
        if self.unread:
            return [(name, "unread", None, self.unread) for name, _ in self.capture_members(frame)]
        return [
            ("grade", "read", "__grade", None),
            ("bonus", "read", "__bonus", None),
            ("twice", "read", "__twice", None),
            # A local of the lambda's of its name is in scope here: the code names it.
            ("k", "hidden", None, HIDDEN),
        ]

    def capture_referent(self, frame, name):
        return None

    def assert_current(self):
        pass

    def validate_member(self, *args, **kwargs):
        raise AssertionError("a capture's member is never judged for a change")

    def _function_at(self, frame):
        # No build's declarations: no route to the lambda's own copy is told.
        return None

    _capture_route = NativeSession._capture_route

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
    scope, unread = worker.read_captures(worker.runtime, frame, worker.runtime.capture_plan(frame))
    assert scope["name"] == "Captures" and scope["reference"] in worker.capture_handles
    rows = {row["name"]: row for row in scope["variables"]}
    # A member the debugger lists twice is left out, saying why; so is the hidden `k`.
    assert sorted(rows) == ["bonus", "grade"]
    assert unread == {
        "twice": "the debugger doesn't list the member of the lambda's object that holds it, once",
        "k": HIDDEN,
    }
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
    # A stop before the lambda stores `__closure`, or an optimized build: not even asked,
    # and each says why.
    why = "the stop is at the lambda's start, before its prologue stores `__closure`"
    worker = _worker(tmp_path, _Lambda(unread=why))
    plan = worker.runtime.capture_plan(frame)
    assert worker.read_captures(worker.runtime, frame, plan) == (
        None,
        {"grade": why, "bonus": why, "twice": why, "k": why},
    )
    assert worker.dap.asked == []
    worker = _worker(tmp_path, _Lambda(), hover="<error: invalid value object>")
    scope, unread = worker.read_captures(worker.runtime, frame, worker.runtime.capture_plan(frame))
    assert scope is None
    assert unread["bonus"] == "the debugger gave no address for `__closure` at this stop"
    assert unread["k"] == HIDDEN


def test_a_not_found_capture_says_why_the_worker_did_not_read_it():
    from peeky.probe import _root

    snapshot = {
        "scopes": [{"name": "Locals", "variables": [{"name": "k"}]}],
        "closure_captures": ["bonus", "grade"],
        "closure_unread": {"bonus": "Peeky reads no capture in an optimized build, …"},
    }
    with pytest.raises(ValueError) as raised:
        _root(None, {}, snapshot, "bonus")
    assert str(raised.value) == (
        "not found: 'bonus' is a capture of this lambda, which GCC passes through "
        "`__closure`, and Peeky can't read it here: Peeky reads no capture in an optimized "
        "build, …; read it where the lambda is created, or print it from the program; "
        "visible: k"
    )
    with pytest.raises(ValueError) as raised:
        _root(None, {}, snapshot, "grade")
    assert "and Peeky didn't read it here; read it where" in str(raised.value)


def test_this_of_a_by_copy_lambda_is_its_objects_copy():
    # GCC 13 gives `[*this]`'s `this` no location; the copy is the member `__this`, of the
    # class `this` points at. `[this]`'s `this` lldb-dap lists itself.
    entry = {"chain": [0x16], "bodies": [0x16]}
    dies = closure_dies()
    dies[0x0A]["children"].append(0x23)
    dies[0x16]["children"].append(0x24)
    dies[0x23] = die(
        "DW_TAG_member",
        0x0A,
        DW_AT_name='"__this"',
        DW_AT_type='0x00000080 "Acc"',
        DW_AT_data_member_location="0x28",
    )
    dies[0x24] = die(
        "DW_TAG_variable",
        0x16,
        DW_AT_name='"this"',
        DW_AT_type='0x00000090 "const Acc *const"',
        DW_AT_artificial="true",
    )
    dies[0x80] = die("DW_TAG_structure_type", 0x01, DW_AT_name='"Acc"')
    dies[0x90] = die("DW_TAG_const_type", 0x01, DW_AT_type='0x00000091 "const Acc *"')
    dies[0x91] = die("DW_TAG_pointer_type", 0x01, DW_AT_type='0x00000092 "const Acc"')
    dies[0x92] = die("DW_TAG_const_type", 0x01, DW_AT_type='0x00000080 "Acc"')
    assert _gcc_capture_members(dies, entry)[-1] == ("this", "__this", 0x80)
    # `[this]`: GCC reads it through `__closure`, and lldb-dap lists it.
    dies[0x24]["attrs"]["DW_AT_location"] = "DW_OP_fbreg -24, DW_OP_deref, DW_OP_plus_uconst 0x28"
    assert "this" not in [name for name, _, _ in _gcc_capture_members(dies, entry)]
    # A `__this` of another class is no copy of what `this` points at.
    del dies[0x24]["attrs"]["DW_AT_location"]
    dies[0x23]["attrs"]["DW_AT_type"] = '0x00000060 "Pt"'
    assert "this" not in [name for name, _, _ in _gcc_capture_members(dies, entry)]


class _Statements:
    """`long x = 9;` on line 30, in a block open from line 28 to its `}` on line 33."""

    def scope_lines(self, line, name):
        return (30, 33) if (line, name) == (30, "x") else None

    def declared_through(self, line, name):
        return 30 if (line, name) == (30, "x") else None


def test_which_x_a_stop_names_where_the_lambda_captures_and_declares_it(monkeypatch):
    # #361 review: `[x]{ use(x); { use(x); long x = 9; use(x); } use(x); }`, each on its
    # own line: the capture above the block, in it above the declaration, and past it;
    # the local past its declaration, in its block.
    entry = {
        "root": 0x16,
        "locals": {"x": {("lam.cpp", 30, True), (None, None, True)}},
        "visible": {("x", "lam.cpp", 30, True): ((0x100, 0x200),)},
    }
    session = NativeSession.__new__(NativeSession)
    monkeypatch.setattr(NativeSession, "_function_at", lambda self, frame: entry)
    monkeypatch.setattr(NativeSession, "_block_pc", lambda self, frame: frame["pc"])
    monkeypatch.setattr(NativeSession, "_place", lambda self, frame: ("lam.cpp", frame["line"]))
    statements = _Statements()
    monkeypatch.setattr(NativeSession, "_statements", lambda self, source: statements)

    def scope(line, pc):
        return session._capture_scope({"line": line, "pc": pc}, "x")

    assert scope(26, 0x50) == ("capture", None)
    assert scope(29, 0x150) == ("capture", None)
    assert scope(31, 0x160) == ("local", 30)
    assert scope(35, 0x250) == ("capture", None)
    # Review round 2: where the stop's code isn't its block's, the capture, whatever the
    # text reads (a macro's `if (int x = 9) …` ends where its line doesn't say).
    assert scope(31, 0x250) == ("capture", None)
    # On the declaration's own line, or past its block where the stop's code is the
    # block's, Peeky can't tell.
    for line, pc in ((30, 0x158), (35, 0x180)):
        assert scope(line, pc)[0] == "unknown"
        assert scope(line, pc)[1].startswith(
            "this lambda also declares a local `x` on line 30, and Peeky can't tell whether "
            "the code at this stop names that local or the capture"
        )
    # Where the source can't be read, a stop above the declaration, or outside its block.
    monkeypatch.setattr(NativeSession, "_statements", lambda self, source: None)
    session.__dict__.pop("capture_scopes", None)
    assert scope(29, 0x150) == ("capture", None)
    assert scope(36, 0x250) == ("capture", None)
    assert scope(31, 0x160)[0] == "unknown"
    # Where the block's code isn't known, the text alone.
    monkeypatch.setattr(NativeSession, "_statements", lambda self, source: statements)
    entry["visible"] = {}
    session.__dict__.pop("capture_scopes", None)
    assert scope(31, 0x250) == ("local", 30)
    assert scope(35, 0x250) == ("capture", None)
    # A capture the lambda doesn't declare again is the capture everywhere.
    assert session._capture_scope({"line": 31, "pc": 0x160}, "y") == ("capture", None)


def test_where_a_lambda_is_defined_is_found_however_deep_it_is_nested(monkeypatch):
    # #361 review: only 4 functions around a lambda were read, and past them it counted
    # as finished: a fifth lambda inside `T t{1, …}` read `t.b` uninitialized.
    from peeky.native import LAMBDA_NESTING

    def chain(depth):
        entries = [
            {"root": n, "enclosing": n + 1, "defined": 10, "locals": {}, "constructor": False}
            for n in range(depth)
        ]
        entries.append(
            {
                "root": depth,
                "enclosing": None,
                "locals": {"t": {("f.cpp", 5, True)}},
                "constructor": False,
            }
        )
        return entries

    session = NativeSession.__new__(NativeSession)
    monkeypatch.setattr(NativeSession, "_through", lambda self, source, line, name: 20)
    for depth in (1, 5, 40):
        session.declarations = chain(depth)
        found = session._lambda_scope(session.declarations[0], [], False)
        assert found == "in the declaration of `t` (line 5)", depth
    # Past the bound, never taken for finished.
    session.declarations = chain(LAMBDA_NESTING + 5)
    found = session._lambda_scope(session.declarations[0], [], False)
    assert found == f"more than {LAMBDA_NESTING} functions deep"


class _Failing(_Dap):
    """lldb-dap answering the hover of `__closure`, then failing its listing a way."""

    def __init__(self, listing):
        super().__init__("0x0000fffff8f0")
        self.listing = listing

    def request(self, command, arguments=None, timeout=15, **_):
        if command == "variables":
            self.asked.append((command, arguments))
            if isinstance(self.listing, Exception):
                raise self.listing
            return self.listing
        return super().request(command, arguments, timeout)


@pytest.mark.parametrize(
    ("listing", "why"),
    [
        (
            ResponseTimeout("variables"),
            "the debugger didn't list what `__closure` points at in time",
        ),
        (
            RequestFailed("no children"),
            "the debugger couldn't list what `__closure` points at (no children)",
        ),
        (
            {"variables": [{"name": f"__m{n}", "value": "1", "type": "int"} for n in range(513)]},
            "what `__closure` points at has more than 512 members, past the bound Peeky lists",
        ),
        (
            {"variables": "x"},
            "the debugger's listing of what `__closure` points at is malformed",
        ),
        (
            {"variables": [{"name": "__bonus"}, "x"]},
            "the debugger's listing of what `__closure` points at is malformed",
        ),
    ],
)
def test_each_capture_not_read_says_which_read_failed(tmp_path, listing, why):
    # Review round 2 of #361: a listing that timed out, failed, ran past 512 members or
    # was malformed said the debugger gave no address for `__closure`.
    frame = {"id": 3, "name": "operator()"}
    worker = _worker(tmp_path, _Lambda())
    worker.dap = _Failing(listing)
    scope, unread = worker.read_captures(worker.runtime, frame, worker.runtime.capture_plan(frame))
    assert scope is None
    assert {name: unread[name] for name in ("grade", "bonus", "twice")} == {
        "grade": why,
        "bonus": why,
        "twice": why,
    }
    assert unread["k"] == HIDDEN
    assert worker.view_timed_out is isinstance(listing, ResponseTimeout)
    # An expansion that reads it again says so too.
    with pytest.raises(ValueError, match=f"^Peeky can't read `grade` again: {re.escape(why)}"):
        worker.walk_capture(worker.runtime, frame, (("grade", "std::map<int, int> &"),))


def test_a_hover_of_closure_that_fails_or_times_out_says_so(tmp_path):
    frame = {"id": 3, "name": "operator()"}

    class Hover(_Dap):
        def __init__(self, failure):
            super().__init__("")
            self.failure = failure

        def request(self, command, arguments=None, timeout=15, **_):
            raise self.failure

    for failure, why in (
        (ResponseTimeout("evaluate"), "the debugger didn't answer a hover of `__closure` in time"),
        (RequestFailed("no location"), "the debugger couldn't read `__closure` here (no location)"),
    ):
        worker = _worker(tmp_path, _Lambda())
        worker.dap = Hover(failure)
        _, unread = worker.read_captures(worker.runtime, frame, worker.runtime.capture_plan(frame))
        assert unread["bonus"] == why


def _kind(offset, name):
    return f'0x{offset:08x} "{name}"'


def test_describe_tells_how_each_capture_is_held_by_the_lambdas_object():
    # Review of #393: `describe` gave a capture the body's variable's type (`const Pt`,
    # `[*this]`'s `const Acc *const`), and left `[this]` as an ordinary local.
    from peeky.native import _gcc_capture_kinds

    entry = {"chain": [0x16], "bodies": [0x16]}
    found = _gcc_capture_kinds(closure_dies({0x14: {"DW_AT_type": _kind(0x50, "long")}}), entry)
    # The member's type (`int`, not the variable's `const int`), and how it is held. A
    # capture whose member isn't told says how, from its variable, with no type.
    assert {name: shown["how"] for name, shown in found.items()} == {
        "bonus": "by value",
        "grade": "by reference",
        "n": "by value",
        "odd": "by value",
        "pt": "by value",
    }
    assert "type" not in found["n"] and "this" not in found, found
    # `[this]`, or `this` captured implicitly: GCC's `this` reads a pointer member.
    held = closure_dies(
        {
            0x1D: {
                "DW_AT_location": "DW_OP_fbreg -24, DW_OP_deref, DW_OP_plus_uconst 0x28",
                "DW_AT_type": _kind(0x72, "Acc *const"),
            }
        }
    )
    held[0x0A]["children"].append(0x21)
    held[0x21] = die(
        "DW_TAG_member",
        0x0A,
        DW_AT_name='"__this"',
        DW_AT_type=_kind(0x71, "Acc *"),
        DW_AT_data_member_location="0x28",
    )
    held[0x71] = die("DW_TAG_pointer_type", 0x01, DW_AT_type=_kind(0x80, "Acc"))
    held[0x72] = die("DW_TAG_const_type", 0x01, DW_AT_type=_kind(0x71, "Acc *"))
    held[0x80] = die("DW_TAG_structure_type", 0x01, DW_AT_name='"Acc"')
    assert _gcc_capture_kinds(held, entry)["this"] == {"type": "Acc *", "how": "by reference"}
    # `[*this]`: no location, and its member is the object's copy.
    copied = closure_dies({0x1D: {"DW_AT_type": _kind(0x73, "const Acc *const")}})
    del copied[0x1D]["attrs"]["DW_AT_location"]
    copied[0x0A]["children"].append(0x21)
    copied[0x21] = {
        **held[0x21],
        "attrs": {**held[0x21]["attrs"], "DW_AT_type": _kind(0x80, "Acc")},
    }
    copied[0x80] = held[0x80]
    copied[0x73] = die("DW_TAG_const_type", 0x01, DW_AT_type=_kind(0x74, "const Acc *"))
    copied[0x74] = die("DW_TAG_pointer_type", 0x01, DW_AT_type=_kind(0x75, "const Acc"))
    copied[0x75] = die("DW_TAG_const_type", 0x01, DW_AT_type=_kind(0x80, "Acc"))
    assert _gcc_capture_kinds(copied, entry)["this"] == {"type": "Acc", "how": "by value"}
    # clang's lambda: no `__closure`, no captures of this kind.
    assert _gcc_capture_kinds(closure_dies({0x17: {"DW_AT_name": '"this"'}}), entry) == {}


def test_describe_lists_this_once_as_the_lambdas_capture_of_the_object():
    from peeky import cli

    def text(captures, names=("d", "this")):
        return cli._describe_text(
            {
                "language": "cpp",
                "root": "/w",
                "breakpoint_lines": {},
                "scopes": [
                    {
                        "scope": "Acc::run::operator()",
                        "lines": [6, 8],
                        "names": list(names),
                        "types": {"d": "long int", "this": "const Acc *const"},
                        "captures": captures,
                    }
                ],
                "mutable_types": "integer root locals",
                "commands": {},
                "timing": {"advice": "fast"},
            }
        )

    copied = text(
        {"this": {"type": "Acc", "how": "by value"}, "k": {"type": "long", "how": "by value"}}
    )
    assert (
        "names in Acc::run::operator() (lines 6-8): d (long int), k (long, capture by value), "
        "this (Acc, capture of the object by value)\n" in copied
    ), copied
    held = text({"this": {"type": "Acc *", "how": "by reference"}})
    assert "this (Acc *, capture of the object by reference)" in held and held.count("this (") == 1
