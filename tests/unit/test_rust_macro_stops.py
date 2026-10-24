"""#326: a `session step` from the line before `let y = mclone!(x) + 1;`, with
`macro_rules! mclone { ($v:expr) => {{ $v.clone() }}; }`, stops at the inlined `clone`'s first
instruction. LLDB shows that stop on the line the call is made from: the macro's definition,
which has no row of its own in the line table. `session inspect` then failed with
"Native stopped line does not match prepared symbols", and the session was closed. Now that
line is the build's (the DWARF names it as an inlined call's site), a stop there reads no
value of the frame and says why, and the session goes on. A breakpoint on such a line is
refused before it is placed."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from peeky.errors import UnplacedStop, classify
from peeky.rust import RustSession
from peeky.rust_mir import macro_definitions, macro_lines
from peeky.rust_symbols import CodeBlock
from peeky.worker import DebugWorker

HASH = "h0123456789abcdef"
SOURCE = """macro_rules! mclone { ($v:expr) => {{ $v.clone() }}; }

#[inline(always)]
fn bump(v: &mut u32) {
    *v += 1;
}

fn f(mut x: u32) -> u32 {
    std::hint::black_box(&mut x);
    let y = mclone!(x) + 1;
    bump(&mut x);
    y + x
}
"""
# Line 9's own row at 0x1a0, line 10's at 0x1b8, line 12's at 0x1d0; line 4's (`bump`'s
# body, inlined) at 0x1c0. Lines 1 and 11 have none: only inlined calls are made there.
ROWS = [
    (0x1A0, "src/main.rs", 9),
    (0x1B8, "src/main.rs", 10),
    (0x1C0, "src/main.rs", 5),
    (0x1D0, "src/main.rs", 12),
]


def site(tmp_path, low, high, line, column, name, file=None):
    """An inlined call's DWARF block in `f`, made from ``line``."""
    called = (file or str(Path(tmp_path) / "src/main.rs"), line, column, name)
    return CodeBlock(0, ((low, high),), (), True, called)


def blocks_of(tmp_path):
    return (
        CodeBlock(None, ((0x100, 0x200),), ("x", "y")),
        # `$v.clone()` of `mclone!(x)`: its site is the macro's definition, line 1.
        site(tmp_path, 0x1B0, 0x1B8, 1, 39, "clone"),
        # `bump(&mut x);`: line 11's only code.
        site(tmp_path, 0x1C0, 0x1C8, 11, 5, "bump"),
    )


def session_at(tmp_path, line, pc, blocks=None, text=SOURCE, rows=ROWS):
    """A RustSession stopped at ``pc`` (unslid) on ``line`` of `f`, whose source reads
    ``text``."""
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src/main.rs").write_text(text or "")
    session = object.__new__(RustSession)
    session.code_blocks = {HASH: blocks_of(tmp_path) if blocks is None else blocks}
    session.prepared = SimpleNamespace(workspace=tmp_path, source_root=tmp_path)
    session.owned = SimpleNamespace(texts={"src/main.rs": text} if text else {})
    session.load_bias = 0x4000
    session._rows = list(rows)
    session._lines_data = json.dumps({"src/main.rs": sorted({n for _, _, n in rows})})
    if not text:
        session._source_text = lambda source: None
    frame = {
        "name": f"macros::f::{HASH}",
        "line": line,
        "column": 39 if line == 1 else 5,
        "source": {"path": str(tmp_path / "src/main.rs")},
        "instructionPointerReference": hex(0x4000 + pc),
    }
    return session, frame


def test_a_macros_definitions_are_read_with_their_names():
    text = (
        "macro_rules! a { () => { 1 } } macro_rules! r#b { () => { 2 } }\n"
        "// macro_rules! commented { }\n"
        'const S: &str = "macro_rules! quoted {}";\n'
        "macro m($x:expr) {\n"
        "    $x\n"
        "}\n"
        "macro_rules! c (\n"
        "    () => { 3 }\n"
        ");\n"
    )
    assert macro_definitions(text) == (("a", 1, 1), ("b", 1, 1), ("m", 4, 6), ("c", 7, 9))
    assert sorted(macro_lines(text)) == [1, 4, 5, 6, 7, 8, 9]
    assert macro_definitions('fn f() { "x') is None
    assert macro_definitions(None) is None


def test_a_stop_at_an_inlined_calls_site_is_the_builds(tmp_path):
    # The macro's definition: the DWARF names line 1 as `clone`'s site in `f`.
    session, frame = session_at(tmp_path, 1, 0x1B0)
    session.validate_stop(frame)
    # A line whose only code is an inlined call: `bump`'s site.
    session, frame = session_at(tmp_path, 11, 0x1C0)
    session.validate_stop(frame)
    # A macro's definition line, though the function's blocks are not known.
    session, frame = session_at(tmp_path, 1, 0x1B0, blocks=())
    session.validate_stop(frame)
    # Anything else with no row of its own is not the build's.
    for line, blocks in ((11, ()), (7, None), (13, None)):
        session, frame = session_at(tmp_path, line, 0x1C0, blocks=blocks)
        with pytest.raises(RuntimeError, match="Native stopped line does not match"):
            session.validate_stop(frame)
    # Nor is a source outside the build.
    session, frame = session_at(tmp_path, 1, 0x1B0)
    frame["source"]["path"] = str(tmp_path.parent / "elsewhere.rs")
    with pytest.raises(RuntimeError, match="does not match"):
        session.validate_stop(frame)


def test_the_stops_pc_must_be_in_the_inlined_calls_code(tmp_path, monkeypatch):
    # #326 review: line 11 is `bump`'s site, but a pc at line 12's row (0x1d0), outside
    # `bump`'s code (0x1c0..0x1c8), is not a stop there; nor is the macro's line 1 at a pc
    # outside `clone`'s code, though the source defines a macro there.
    for line, pc in ((11, 0x1D0), (11, 0x1BC), (11, 0x1C8), (1, 0x1D0), (1, 0x1B8)):
        session, frame = session_at(tmp_path, line, pc)
        with pytest.raises(RuntimeError, match="Native stopped line does not match"):
            session.validate_stop(frame)
    # A caller's frame: its return address may end the call's code (pc - 1 is in it).
    session, frame = session_at(tmp_path, 11, 0x1C8)
    session.innermost_id, frame["id"] = 1, 2
    session.validate_stop(frame)
    frame["id"] = 1
    with pytest.raises(RuntimeError, match="does not match"):
        session.validate_stop(frame)
    # Before the load bias is read, the pc isn't known: `validate_debugger` decides, once it
    # reads the bias with the module, before anything of the frame is read.
    from peeky.native import NativeSession

    monkeypatch.setattr(
        NativeSession,
        "validate_debugger",
        lambda self, dap, frame: setattr(self, "load_bias", 0x4000),
    )
    for pc, valid in ((0x1C0, True), (0x1D0, False)):
        session, frame = session_at(tmp_path, 11, pc)
        session.load_bias = None
        session.validate_stop(frame)
        if valid:
            session.validate_debugger(None, frame)
        else:
            with pytest.raises(RuntimeError, match="does not match"):
                session.validate_debugger(None, frame)
    # A bias that can't be read leaves the pc unknown: refused.
    monkeypatch.setattr(NativeSession, "validate_debugger", lambda self, dap, frame: None)
    session, frame = session_at(tmp_path, 11, 0x1C0)
    session.load_bias = None
    with pytest.raises(RuntimeError, match="does not match"):
        session.validate_debugger(None, frame)
    # A line with rows of its own is not checked again.
    session, frame = session_at(tmp_path, 10, 0x1D0)
    session.validate_debugger(None, frame)


def test_a_stop_in_a_macros_definition_reads_no_value_and_says_why(tmp_path):
    session, frame = session_at(tmp_path, 1, 0x1B0)
    assert session.macro_stop(frame) == ("mclone",)
    note = session.macro_stop_note(frame)
    assert note == (
        "Peeky reads no value of frame 0 (macros::f) here: the debugger shows the stop on "
        "line 1 of src/main.rs, inside the definition of macro `mclone!`. That is the site of "
        "a call the macro makes, which the build inlined, and the debug information doesn't "
        "say which invocation of the macro it is, so Peeky can't tell where in macros::f the "
        "stop is. `session step` goes on to your code's next line, or `session continue`"
    )
    found = classify(ValueError(note))
    assert found["code"] == "INVALID_REQUEST" and not found["session_closed"], found
    assert found["hint"] == (
        "Nothing in that frame is read or changed here: `session step` goes on to your code's "
        "next line, and `--frame N` reads a caller."
    )
    assert len(found["hint"]) <= 150
    # A caller frame there says which frame.
    assert "of frame 2 (macros::f)" in session.macro_stop_note(frame, 2)
    # A source not read as rustc reads it: a line with no row of its own may be a macro's.
    session, frame = session_at(tmp_path, 1, 0x1B0, text='fn f() { "x')
    assert session.macro_stop(frame) == ()
    assert "Peeky can't read the source as rustc does" in session.macro_stop_note(frame)
    # A line of the function's own, and a macro's line with rows of its own in the function
    # (a macro body's statement), are judged by their line as before.
    for line, pc in ((11, 0x1C0), (10, 0x1B8), (9, 0x1A0)):
        session, frame = session_at(tmp_path, line, pc)
        assert session.macro_stop(frame) is None and session.macro_stop_note(frame) is None
    session, frame = session_at(tmp_path, 1, 0x1B0, rows=[*ROWS, (0x1A8, "src/main.rs", 1)])
    assert session.macro_stop(frame) is None
    # Without the function's blocks, a row of the line anywhere counts.
    session, frame = session_at(
        tmp_path, 1, 0x1B0, blocks=(), rows=[*ROWS, (0x1A8, "src/main.rs", 1)]
    )
    assert session.macro_stop(frame) is None


def test_a_line_whose_only_code_is_inlined_starts_at_that_code(tmp_path):
    # A step onto `bump(&mut x);` stops at the inlined call's first instruction: nothing of
    # the line has run. Inside it, the stop is part-way through the line.
    session, frame = session_at(tmp_path, 11, 0x1C0)
    assert session._line_start(frame)
    session, frame = session_at(tmp_path, 11, 0x1C4)
    assert not session._line_start(frame)
    # A macro's definition, or a source not read, is never a line's start.
    session, frame = session_at(tmp_path, 1, 0x1B0)
    assert not session._line_start(frame)
    session, frame = session_at(tmp_path, 11, 0x1C0, text='fn f() { "x')
    assert not session._line_start(frame)
    # Nor where the line table was not read: the line may have rows of its own.
    session, frame = session_at(tmp_path, 11, 0x1C0, rows=[])
    session._lines_data = json.dumps({"src/main.rs": [9, 10, 12]})
    assert not session._line_start(frame)


def test_a_breakpoint_on_such_a_line_is_refused_before_it_is_placed(tmp_path):
    session, _ = session_at(tmp_path, 1, 0x1B0)
    program = tmp_path / "src/main.rs"
    found = session.unplaceable_line(program, 1)
    assert found == (
        "Line 1 has no code to stop at of its own: it is in the definition of macro "
        "`mclone!`. The build inlined the call of `clone` made there into the code that "
        "invokes the macro, and the debug information doesn't say which invocation a stop "
        "there is in. Put the breakpoint on the line that invokes the macro, or choose a line "
        "from `peeky describe`"
    )
    assert classify(ValueError(found))["code"] == "INVALID_BREAKPOINT"
    assert session.unplaceable_line(program, 11) == (
        "Line 11 has no code to stop at of its own: the build inlined the call of `bump` made "
        "there, and the debugger reports that code on the called function's own lines. Stop on "
        "an earlier line and `session step` onto line 11, or choose a line from `peeky "
        "describe`"
    )
    # A line with rows of its own, or one no inlined call is made from (the debugger moves
    # its breakpoint, and says so), is placed as before.
    assert session.unplaceable_line(program, 10) is None
    assert session.unplaceable_line(program, 7) is None
    assert session.unplaceable_line(tmp_path / "other.rs", 1) is None
    # A source not read: the line may be in a macro's definition.
    session, _ = session_at(tmp_path, 1, 0x1B0, text='fn f() { "x')
    assert "may be in a macro's definition" in session.unplaceable_line(program, 11)


class Backend:
    """lldb-dap stopped where ``frames`` say."""

    def __init__(self, frames):
        self.frames, self.calls = frames, []

    def request(self, operation, args, **_):
        self.calls.append(operation)
        if operation == "stackTrace":
            return {"stackFrames": self.frames}
        if operation == "scopes":
            return {"scopes": [{"name": "Locals", "variablesReference": 7}]}
        if operation == "variables":
            return {"variables": [{"name": "x", "value": "5", "type": "u32"}]}
        if operation == "setBreakpoints":
            return {"breakpoints": [{"verified": True, "line": 615}]}
        raise AssertionError(operation)

    def close(self):
        pass


class Runtime:
    def __init__(self, session):
        self.session, self.validated = session, []

    def assert_current(self):
        pass

    def validate_stop(self, frame):
        self.session.validate_stop(frame)

    def validate_debugger(self, dap, frame):
        self.validated.append(frame["id"])

    def local_scopes(self, scopes):
        return scopes

    def validate_breakpoints(self, points):
        pass

    def macro_stop_note(self, frame, index=0):
        return self.session.macro_stop_note(frame, index)

    def unplaceable_line(self, program, line):
        return self.session.unplaceable_line(program, line)

    def source_path(self, program):
        return program

    def close(self):
        pass


def test_the_worker_keeps_the_session_at_such_a_stop(tmp_path):
    session, frame = session_at(tmp_path, 1, 0x1B0)
    caller = {**frame, "id": 2, "name": f"macros::main::{HASH}", "line": 12}
    worker = DebugWorker(tmp_path)
    worker.dap = Backend([{**frame, "id": 1}, caller])
    worker.runtime = Runtime(session)
    worker.session, worker.stop_id, worker.state = "session", "stop", "stopped"
    worker.stop_reason, worker.thread_id = "step", 1
    worker.capabilities = {}
    # The state says why nothing is read.
    worker.stop_location = worker.read_location()
    assert worker.location()["macro_stop"] == session.macro_stop_note(frame)
    with pytest.raises(UnplacedStop, match="^Peeky reads no value of frame 0 ") as refused:
        worker.dispatch("inspect", {"session_id": "session", "stop_id": "stop"})
    assert worker.session == "session" and "variables" not in worker.dap.calls
    # A probe tells its breakpoint's stop by the frame it carries.
    assert (refused.value.frame["line"], refused.value.frame["name"]) == (1, frame["name"])
    # The caller's frame is on a line of its own, and reads.
    found = worker.dispatch("inspect", {"session_id": "session", "stop_id": "stop", "frame": 1})
    assert found["scopes"][0]["variables"][0]["name"] == "x"
    # A one-shot `continue --to` the macro's definition is refused before it is placed, and
    # nothing changes.
    worker.program, worker.points = tmp_path / "src/main.rs", [{"line": 9}]
    with pytest.raises(ValueError, match="definition of macro `mclone!`"):
        worker.set_points(worker.runtime, [*worker.points, {"line": 1}], temporary=1)
    assert "setBreakpoints" not in worker.dap.calls
    # A stop anywhere else has no such note.
    worker.forget_stop()
    assert worker.stop_macro is None


NOTE = (
    "Peeky reads no value of frame 0 (demo::inlined) here: the debugger shows the stop on line 3 "
    "of src/main.rs, inside the definition of macro `pick!`. …"
)


class MacroStops:
    """A probe's worker stopping at line 3 of src/main.rs once per entry of ``order``: in
    `demo::own`, where the line has code of its own, or in `demo::inlined`, where the
    debugger shows the stop inside the macro's definition, whose frame `inspect` refuses
    (#326 review). ``at`` moves an inline-only stop to another line."""

    adapter = None

    def __init__(self, order, at=3):
        self.order, self.at, self.calls, self.current = list(order), at, [], None

    def frame(self):
        line = 3 if self.current == "own" else self.at
        return {
            "name": f"demo::{self.current}::{HASH}",
            "line": line,
            "source": {"path": "/w/src/main.rs"},
        }

    def dispatch(self, operation, args):
        self.calls.append(operation)
        if operation == "launch":
            return {
                "session_id": "s",
                "breakpoints": [
                    {"id": index, "verified": True, "line": point["line"]}
                    for index, point in enumerate(args["breakpoints"], 1)
                ],
                "artifact_manifest_id": None,
                "target_identity": None,
                "source_sha256": "x",
            }
        if operation == "breakpoints":
            return {"breakpoints": [{"line": point["line"]} for point in args["breakpoints"]]}
        if operation == "wait":
            if not self.order:
                return {"state": "terminated"}
            self.current = self.order.pop(0)
            frame = self.frame()
            return {
                "state": "stopped",
                "stop_id": f"t{len(self.calls)}",
                "event": {"body": {"reason": "breakpoint", "threadId": 1}},
                "location": f"src/main.rs:{frame['line']}",
                **({"macro_stop": NOTE} if self.current == "inlined" else {}),
            }
        if operation == "inspect":
            if self.current == "inlined":
                raise UnplacedStop(NOTE, self.frame())
            return {
                "frames": [self.frame()],
                "scopes": [
                    {
                        "name": "Locals",
                        "reference": 1,
                        "variables": [{"name": "x", "value": "5", "variablesReference": 0}],
                    }
                ],
            }
        if operation == "set_variable":
            return {"name": args["name"], "before": "5", "after": args["value"]}
        if operation == "report":
            return {"output": "OWN 6\nINLINED 6\n", "output_truncated": False}
        return {}

    def close(self):
        self.calls.append("close")


def probed(monkeypatch, worker, request):
    import peeky.probe as probe_module

    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    return probe_module.run_probe("/w", request)


def test_a_probe_withholds_a_stop_inside_a_macros_definition_and_goes_on(monkeypatch):
    # #326 review: a macro's line placed in one function, where it has code of its own, and
    # inline-only in another, where the debugger shows the stop inside the macro's
    # definition. Its `inspect` refusal failed the whole probe and closed its session.
    from peeky.probe import Probe, _table

    worker = MacroStops(["own", "inlined", "own"])
    result = probed(monkeypatch, worker, Probe("src/main.rs", 3, ("x",), hits="all"))
    assert result["status"] == "completed" and "error" not in result, result.get("error")
    assert [(row["hit"], row.get("value")) for row in result["rows"]] == [
        (1, "5"),
        (2, None),
        (3, "5"),
    ]
    withheld = result["rows"][1]
    assert withheld["error"] == f"withheld: {NOTE}" and withheld["unavailable"] is True
    assert withheld["settable"] is False and withheld["line"] == 3
    assert withheld["function"] == f"demo::inlined::{HASH}"
    assert f"withheld: {NOTE[:40]}" in _table(result)
    # Nothing is changed there: a change due at that stop is refused, as any refusal is.
    worker = MacroStops(["own", "inlined"])
    request = Probe("src/main.rs", 3, ("x",), hits=2, set=(("x", "100"),), set_hit=2)
    result = probed(monkeypatch, worker, request)
    assert result["status"] == "failed" and "set_variable" not in worker.calls
    assert result["error"]["code"] == "MUTATION_REJECTED", result["error"]
    assert result["error"]["message"] == f"'x' cannot be changed at this stop: {NOTE}"
    # `--capture return`: no step out of a stop Peeky can't place.
    worker = MacroStops(["inlined"])
    result = probed(monkeypatch, worker, Probe("src/main.rs", 3, ("x", "(return)")))
    assert result["status"] == "completed", result.get("error")
    assert [(row["phase"], row["error"]) for row in result["rows"]] == [
        ("observed", f"withheld: {NOTE}"),
        ("returned", f"withheld: {NOTE}"),
    ]
    # A stop whose place isn't the probe's breakpoint's is no hit of it: refused, as before.
    worker = MacroStops(["inlined"], at=1)
    result = probed(monkeypatch, worker, Probe("src/main.rs", 3, ("x",)))
    assert result["status"] == "failed" and result.get("rows") in (None, [])
    assert (
        "Stopped at /w/src/main.rs:1 (breakpoint), not the requested" in result["error"]["message"]
    )
