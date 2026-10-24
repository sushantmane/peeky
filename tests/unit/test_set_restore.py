"""`--set-at LINE@HIT name=@before` (#305): write back the value a variable held just before
the probe's latest change to it, made at the same hit of its own line, in the same call.
Eleventh evaluation, tides: testing "skip `turn = p` on a stand" took six `--set-at`s, with
the values to restore (1, 1, -1) copied by hand from an earlier probe, because `@all` can't
restore values that differ."""

import json
from types import SimpleNamespace

import pytest

import peeky.probe as probe_module
from peeky import evidence
from peeky.adapters import PythonAdapter
from peeky.errors import Refused, classify
from peeky.java import JavaSession
from peeky.native import NativeSession, PrologueWindow, _coroutine, _function_starts
from peeky.probe import ALL, BEFORE, Probe, _launch_set, parse_set_at, probe_exit_code
from peeky.worker import DebugWorker, _written

FIRST, GUARD, FLAT = 2, 5, 8


class Calls:
    """Stops of main.c, each {line, function, depth, thread, call, callers}: a call is a
    frame instance, whose `x` keeps its own value. Only lines with a breakpoint stop; a
    stop's `call` names the frame its `x` lives in. ``firsts``: the worker's first lines
    for each restore line; ``exact``: whether a write's `before` is kept to restore."""

    def __init__(self, stops, firsts=None, exact=True, values=None, moved=None):
        self.order = [dict(stop) for stop in stops]
        self.firsts = firsts if firsts is not None else {FLAT: [FIRST]}
        self.exact, self.moved = exact, moved or {}
        self.values = dict(values or {})
        self.placed, self.calls, self.writes, self.stop, self.launch = [], [], [], None, None

    def _keep(self, points):
        self.placed.append([point["line"] for point in points])
        asked = {point["line"] for point in points}
        self.order = [stop for stop in self.order if stop["line"] in asked]

    def _replies(self, points):
        return [
            {"id": n, "verified": True, "line": self.moved.get(point["line"], point["line"])}
            for n, point in enumerate(points, 1)
        ]

    def dispatch(self, operation, args):
        self.calls.append(operation)
        if operation == "launch":
            self.launch = args
            points = list(args["breakpoints"])
            entries = None
            if "entry_lines" in args:
                entries = [[line, self.firsts.get(line, [])] for line in args["entry_lines"]]
                asked = {point["line"] for point in points}
                extra = sorted({n for _, ns in entries for n in ns} - asked)
                points += [{"line": n} for n in extra]
            self._keep(points)
            return {
                "session_id": "s",
                "breakpoints": self._replies(points),
                "artifact_manifest_id": None,
                "target_identity": None,
                "source_sha256": "x",
                **({"entry_lines": entries} if entries is not None else {}),
            }
        if operation == "breakpoints":
            self._keep(args["breakpoints"])
            return {"breakpoints": self._replies(args["breakpoints"])}
        if operation == "wait":
            if not self.order:
                return {"state": "terminated"}
            self.stop = self.order.pop(0)
            return {
                "state": "stopped",
                "stop_id": "t",
                "event": {"body": {"reason": "breakpoint", "threadId": self.stop.get("thread", 1)}},
                "location": f"main.c:{self.stop['line']}",
            }
        if operation == "inspect":
            stop = self.stop
            callers = stop.get("callers", [("main", "0x10", 20)])
            return {
                "frames": [
                    {
                        "name": stop.get("function", "f"),
                        "source": {"path": "/w/main.c"},
                        "line": stop["line"],
                    },
                    *(
                        {"name": name, "instructionPointerReference": at, "line": number}
                        for name, at, number in callers
                    ),
                ],
                "depth": stop.get("depth", 2),
                "scopes": [
                    {
                        "name": "Locals",
                        "reference": 1,
                        "variables": [
                            {"name": "x", "value": self._value(), "variablesReference": 0}
                        ],
                    }
                ],
            }
        if operation == "set_variable":
            before = self._value()
            self.values[self._frame()] = args["value"]
            self.writes.append((self.stop["line"], args["value"]))
            result = {"name": args["name"], "before": before, "after": args["value"]}
            if args.get("restorable"):
                result["restore"] = (
                    {"value": before}
                    if self.exact
                    else {"refused": "it is a double, shown rounded"}
                )
            return result
        if operation == "report":
            return {"output": "", "output_truncated": False}
        return {}

    def _frame(self):
        return (
            self.stop.get("thread", 1),
            self.stop.get("function", "f"),
            self.stop.get("call", 1),
        )

    def _value(self):
        return self.values.get(self._frame(), "1")

    def close(self):
        self.calls.append("close")


def run(monkeypatch, worker, request):
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    return probe_module.run_probe("/w", request)


def flip(guard_hit=ALL, flat_hit=ALL):
    """The guard flip: `trend` (here `x`) to 0 at the guard, written back at the flat line."""
    return Probe(
        "main.c", GUARD, ("x",), hits="all",
        set_at=((GUARD, guard_hit, "x", "0"), (FLAT, flat_hit, "x", BEFORE)),
    )  # fmt: skip


def passes(*values):
    """One call of `f` (line FIRST once), then a pass per value: the guard, then the flat
    line, with `x` holding that value at the guard."""
    stops = [{"line": FIRST}]
    for value in values:
        stops += [{"line": GUARD, "set": value}, {"line": FLAT}]
    return stops


class Passes(Calls):
    """Calls whose `x` is set, at each guard stop, to the value its pass gives (the
    program's own `trend = now`)."""

    def dispatch(self, operation, args):
        if operation == "wait":
            reply = super().dispatch(operation, args)
            if self.stop is not None and "set" in self.stop and reply.get("state") == "stopped":
                self.values[self._frame()] = self.stop["set"]
            return reply
        return super().dispatch(operation, args)


def refusal(result):
    assert result["status"] == "failed", result
    assert result["error"]["code"] == "MUTATION_REJECTED", result["error"]
    return result["error"]["message"]


# Parsing and the check before the run.


def test_before_parses_as_a_value_and_needs_another_change_to_the_same_name():
    assert parse_set_at("35@all", "trend=@before") == (None, 35, ALL, "trend", BEFORE)
    assert parse_set_at("35@2", "s.lo = @before") == (None, 35, 2, "s.lo", BEFORE)
    request = Probe("a.c", 32, set_at=((32, ALL, "trend", "0"), (35, ALL, "trend", BEFORE)))
    assert request.set_at[1] == (35, ALL, "trend", BEFORE)
    # A --set's change counts too, at its --set-hit of the --at line.
    Probe("a.c", 32, set=(("trend", "0"),), set_at=((35, 1, "trend", BEFORE),))
    with pytest.raises(ValueError, match=r"no other --set or --set-at changes `trend`"):
        Probe("a.c", 32, set_at=((35, ALL, "trend", BEFORE),))
    # Another name's change is not this one's; nor is another restore.
    with pytest.raises(ValueError, match=r"no other --set or --set-at changes `turn`"):
        Probe("a.c", 32, set_at=((32, 1, "trend", "0"), (35, 1, "turn", BEFORE)))
    with pytest.raises(ValueError, match="changes `x`"):
        Probe("a.c", 32, set_at=((32, 1, "x", BEFORE), (35, 1, "x", BEFORE)))
    with pytest.raises(ValueError, match=r"`@before` is for --set-at"):
        Probe("a.c", 32, set=(("trend", BEFORE),), set_at=((35, 1, "trend", "0"),))
    # One restore is one of the eight changes a probe may ask for.
    assert len(flip().set_at) == 2


def test_a_restore_is_judged_by_its_name_alone_before_the_run():
    # Its value is known only at its stop: no value, and no text, for the check.
    assert _launch_set(35, "trend", BEFORE) == {"line": 35, "name": "trend"}
    assert _launch_set(35, "s", '"x"') == {"line": 35, "name": "s", "text": True, "value": '"x"'}


# The run: what is written back, where, and what is refused.


def test_the_guard_flip_writes_back_each_hit_s_own_value(monkeypatch):
    worker = Passes(passes("1", "1", "-1"))
    result = run(monkeypatch, worker, flip())
    assert result["status"] == "completed" and probe_exit_code(result) == 0, result
    # The worker watches the first line of the restore line's function from the start.
    assert worker.launch["entry_lines"] == [FLAT]
    assert worker.placed[0] == [GUARD, FLAT, FIRST]
    assert worker.writes == [
        (GUARD, "0"),
        (FLAT, "1"),
        (GUARD, "0"),
        (FLAT, "1"),
        (GUARD, "0"),
        (FLAT, "-1"),
    ]
    restores = [change for change in result["interventions"] if change.get("restores")]
    assert [(c["at"], c["restores"], c["before"], c["after"]) for c in restores] == [
        (f"{FLAT}@1", f"{GUARD}@1", "0", "1"),
        (f"{FLAT}@2", f"{GUARD}@2", "0", "1"),
        (f"{FLAT}@3", f"{GUARD}@3", "0", "-1"),
    ]
    text = probe_module._table(result)
    assert f"set x: 0 -> -1  (restored, --set-at line {FLAT}@all, hit 3, in f)" in text, text
    assert f"set x: -1 -> 0  (--set-at line {GUARD}@all, hit 3, in f)" in text, text
    # Each restore is one of the run's outcomes, as any change is.
    assert [o["value"] for o in result["set_outcomes"]] == ["0", BEFORE]
    assert result["set_outcomes"][1]["made"] == 3 and result["set_outcomes"][1]["complete"]


def test_many_restores_are_counted_as_restores(monkeypatch):
    worker = Passes(passes(*["1", "-1"] * 4))
    result = run(monkeypatch, worker, flip())
    text = probe_module._table(result)
    assert f"set x: 5 more restores by --set-at line {FLAT}@all, at hits 4 to 8" in text, text
    assert f"set x: 5 more changes by --set-at line {GUARD}@all, at hits 4 to 8" in text, text
    # The value written back may be the one already there: said so, as for any write.
    worker = Passes(passes(*["0"] * 6))
    text = probe_module._table(run(monkeypatch, worker, flip()))
    assert (
        f"set x: 3 more restores by --set-at line {FLAT}@all, at hits 4 to 6, each with the "
        "same text, not proven unchanged"
    ) in text, text


def test_no_earlier_change_is_refused(monkeypatch):
    stops = [{"line": FIRST}, {"line": FLAT}, {"line": GUARD}]
    result = run(monkeypatch, Calls(stops), flip())
    message = refusal(result)
    assert (
        "x=@before was refused at hit 1 of line 8 (in f): `x` has no earlier change to undo"
        in message
    )
    assert "(--set-at LINE@1 x=VALUE)" in message
    assert classify(Refused("MUTATION_REJECTED", message))["hint"].startswith(
        "Give the value itself"
    )


def test_a_change_at_another_hit_is_not_written_back(monkeypatch):
    # The guard's change at hit 2 only; the flat line's first stop has no change of its hit.
    worker = Passes(passes("1", "1"))
    result = run(monkeypatch, worker, flip(guard_hit=2, flat_hit=ALL))
    assert "no earlier change to undo" in refusal(result)
    worker = Passes([{"line": FIRST}, {"line": GUARD, "set": "1"}, {"line": GUARD}, {"line": FLAT}])
    message = refusal(run(monkeypatch, worker, flip()))
    assert "`x` was last changed at hit 2 of line 5, not at hit 1" in message, message
    assert "Choose lines that stop once each per pass" in message


def test_a_value_not_kept_exactly_is_not_written_back(monkeypatch):
    worker = Passes(passes("1"), exact=False)
    message = refusal(run(monkeypatch, worker, flip()))
    assert (
        "the value `x` had before the change at line 5@1 cannot be written back exactly" in message
    )
    assert "it is a double, shown rounded" in message
    assert worker.writes == [(GUARD, "0")]


@pytest.mark.parametrize(
    "where, said",
    [
        ({"thread": 2}, "this stop is on another thread"),
        ({"function": "g"}, "this stop is in g"),
        ({"depth": 3}, "this stop is 1 call deeper (a recursive call)"),
        ({"depth": 1}, "this stop is 1 call higher"),
        ({"callers": [("main", "0x20", 21)]}, "this stop is under other callers"),
    ],
)
def test_a_restore_in_another_frame_is_refused(monkeypatch, where, said):
    stops = [{"line": FIRST}, {"line": GUARD}, {"line": FLAT, **where}]
    if "thread" in where or "function" in where or where.get("depth") == 3:
        # That frame's own call began too, where Peeky saw it.
        stops.insert(2, {"line": FIRST, **where})
    message = refusal(run(monkeypatch, Calls(stops), flip()))
    assert f"`x` was changed at line 5@1 in f, and {said}: a value is written back only" in message


def test_a_new_call_since_the_change_is_refused(monkeypatch):
    # The guard changes `x` in call 1, which returns; call 2, from the same place, reaches
    # the flat line: the same function, depth and callers, but another call.
    stops = [
        {"line": FIRST},
        {"line": GUARD},
        {"line": FIRST, "call": 2},
        {"line": FLAT, "call": 2},
    ]
    worker = Calls(stops)
    message = refusal(run(monkeypatch, worker, flip()))
    assert "and the first line of f ran again since, at the same stack depth or above" in message
    assert worker.writes == [(GUARD, "0")]
    # A deeper call (recursion) between the change and the restore is no new call of this one.
    stops = [
        {"line": FIRST},
        {"line": GUARD},
        {"line": FIRST, "depth": 3, "call": 2},
        {"line": FLAT},
    ]
    result = run(monkeypatch, Calls(stops), flip())
    assert result["status"] == "completed", result


def test_a_call_not_seen_to_begin_is_refused(monkeypatch):
    # No stop at the first line before the change: Peeky cannot tell that call from another.
    stops = [{"line": GUARD}, {"line": FLAT}]
    message = refusal(run(monkeypatch, Calls(stops), flip()))
    assert "Peeky did not see the call of f that changed `x` at line 5@1 begin" in message
    assert "(line 2)" in message
    # A first line the worker did not find watches nothing.
    message = refusal(run(monkeypatch, Calls(passes("1"), firsts={}), flip()))
    assert "(line none)" in message


def test_a_watched_line_the_debugger_moved_ends_the_watch(monkeypatch):
    worker = Calls([{"line": GUARD}, {"line": FLAT}], moved={FIRST: FIRST + 1})
    message = refusal(run(monkeypatch, worker, flip()))
    assert (
        "Peeky stopped watching for new calls (the debugger moved the breakpoint on line 2"
        in message
    )


def test_the_watch_ends_with_the_last_restore(monkeypatch):
    request = Probe(
        "main.c", GUARD, ("x",), hits="all", set_at=((GUARD, 1, "x", "0"), (FLAT, 1, "x", BEFORE))
    )
    worker = Passes(passes("1", "1"))
    result = run(monkeypatch, worker, request)
    assert result["status"] == "completed", result
    # The first line's breakpoint goes once the one restore is made.
    assert worker.placed == [[GUARD, FLAT, FIRST], [GUARD]]


def test_a_probe_without_a_restore_asks_the_worker_for_nothing_new(monkeypatch):
    worker = Calls([{"line": GUARD}, {"line": FLAT}])
    request = Probe("main.c", GUARD, ("x",), set_at=((FLAT, 1, "x", "0"),))
    result = run(monkeypatch, worker, request)
    assert result["status"] == "completed" and "entry_lines" not in worker.launch
    assert "restores" not in result["interventions"][0]


def test_a_worker_that_cannot_watch_is_refused(monkeypatch):
    class Old(Calls):
        def dispatch(self, operation, args):
            reply = super().dispatch(operation, args)
            if operation == "launch":
                reply.pop("entry_lines", None)
            return reply

    result = run(monkeypatch, Old(passes("1")), flip())
    assert "does not watch for new calls" in result["error"]["message"]


# What each language writes back, and where its calls begin.


def test_python_writes_back_a_number_true_false_or_none_whose_text_is_all_of_it():
    literal = PythonAdapter.restore_literal
    for kind, text in (("int", "-3"), ("int", "12345678901234567890"), ("float", "0.1"),
                       ("float", "-0.0"), ("bool", "True"), ("NoneType", "None")):  # fmt: skip
        assert literal({"type": kind}, text) == text
    for kind, text, said in (
        ("str", "'abc'", "it holds a str"),
        ("list", "[1]", "it holds a list"),
        ("float", "nan", "not the whole float"),
        ("float", "inf", "not the whole float"),
        ("float", "0.10", "not the whole float"),
        ("int", "12...89", "not the whole int"),
        ("bool", "1", "not the whole bool"),
    ):
        with pytest.raises(ValueError, match=said):
            literal({"type": kind}, text)


def test_java_writes_back_an_integer_boolean_or_char_but_never_a_float():
    literal = JavaSession.restore_literal
    assert literal({"type": "int"}, "-7") == "-7"
    assert literal({"type": "long"}, "9000000000") == "9000000000"
    assert literal({"type": "boolean"}, "true") == "true"
    # A char is shown bare: written back as its code point.
    assert literal({"type": "char"}, "+") == "43"
    for kind, text, said in (
        ("double", "0.1", "which java-debug may show rounded"),
        ("float", "1.5", "which java-debug may show rounded"),
        ("String", '"a"', "not a primitive"),
        ("Integer", "3", "not a primitive"),
        ("char", "ab", "not one char"),
        ("int", "0x10", "not a int"),
    ):
        with pytest.raises(ValueError, match=said):
            literal({"type": kind}, text)


def test_native_writes_back_only_a_proven_integer():
    session = SimpleNamespace(exact_text=lambda variable, **where: variable.get("exact", False))
    literal = NativeSession.restore_literal
    assert literal(session, {"exact": True}, "-4096") == "-4096"
    with pytest.raises(ValueError, match="cannot prove its text is all of its value"):
        literal(session, {"exact": False}, "1.5")
    with pytest.raises(ValueError, match="not a decimal integer"):
        literal(session, {"exact": True}, "'a'")


def test_python_watches_the_first_statement_of_the_function_or_module(tmp_path):
    path = tmp_path / "trail.py"
    path.write_text(
        '"""A module."""\n'  # 1
        "import sys\n"  # 2
        "def find(xs):\n"  # 3
        '    """Doc."""\n'  # 4
        "    global SEEN\n"  # 5
        "    trend = 0\n"  # 6
        "    for x in xs:\n"  # 7
        "        trend = x  # 8\n"
        "    return trend\n"  # 9
        "def gen(xs):\n"  # 10
        "    total = 0\n"  # 11
        "    for x in xs:\n"  # 12
        "        yield x  # 13\n"
        "class C:\n"  # 14
        "    n = 1  # 15\n"
        "    def m(self):\n"  # 16
        "        return [y\n"  # 17
        "                for y in (1,)]  # 18\n"
        "print(find([1]))\n"  # 19
    )
    assert PythonAdapter.entry_lines(path, [8, 9, 19]) == {8: [6], 9: [6], 19: [1]}
    for line, said in (
        (13, "line 13 is in `gen`, a generator or coroutine"),
        (15, "line 15 is in the body of class `C`"),
        (18, "line 18 is in a listcomp"),
    ):
        with pytest.raises(ValueError, match=said):
            PythonAdapter.entry_lines(path, [line])
    path.write_text("async def a():\n    await b()\n")
    with pytest.raises(ValueError, match="a generator or coroutine"):
        PythonAdapter.entry_lines(path, [2])


def test_native_watches_the_row_each_body_s_prologue_ends_at():
    order = ((0x100, "t.c", 23), (0x110, "t.c", 25), (0x120, "t.c", 32), (0x200, "t.c", 40))
    function = {"name": "find", "ranges": ((0x100, 0x180),), "order": order}
    window = PrologueWindow(0x100, 0x110, True, frozenset(), False, frozenset({("t.c", 23)}))
    assert _function_starts([function], [window], "t.c", 32) == [25]
    # An empty prologue: the body starts at the entry.
    assert _function_starts([function], [], "t.c", 32) == [23]
    with pytest.raises(ValueError, match="begins in other.h"):
        _function_starts([{**function, "order": ((0x110, "other.h", 3),)}], [window], "t.c", 32)
    with pytest.raises(ValueError, match="cannot place the first line of `find`"):
        _function_starts([{**function, "order": ((0x120, "t.c", 32),)}], [window], "t.c", 32)
    for name in ("app::work::{async_fn#0}", "app::main::{async_block#1}", "g::{coroutine#0}"):
        assert _coroutine({"name": name})
        with pytest.raises(ValueError, match="a coroutine or async body"):
            _function_starts([{**function, "name": name}], [window], "t.c", 32)
    assert not _coroutine({"name": "app::main::{closure#0}"})


def test_java_watches_the_line_each_method_s_code_starts_at(tmp_path):
    source = tmp_path / "Trail.java"
    source.write_text("class Trail {}\n")
    methods = [
        {"name": "findTurns", "line_pcs": {5: (0,), 12: (20,), 15: (40,)}},
        {"name": "lambda$main$0", "line_pcs": {30: (0,)}},
    ]
    session = SimpleNamespace(
        source_path=lambda program: program,
        _declared=lambda: {"demo/Trail": {"methods": methods}},
        mapping={"classes": {"demo.Trail": {"path": str(source), "shared_lines": {}}}},
    )
    assert JavaSession.entry_lines(session, source, [12, 15]) == {12: [5], 15: [5]}
    session.mapping["classes"]["demo.Trail"]["shared_lines"] = {
        "5": {"stops": "main", "never": ["findTurns"]}
    }
    with pytest.raises(ValueError, match="where the debugger stops only in main"):
        JavaSession.entry_lines(session, source, [12])
    session._declared = dict
    with pytest.raises(ValueError, match="could not read the class files"):
        JavaSession.entry_lines(session, source, [12])


# The worker's part.


def test_a_change_keeps_what_a_restore_may_write_back_only_when_asked():
    runtime = SimpleNamespace(
        restore_literal=lambda variable, before, **where: (
            before if before != "0.1" else (_ for _ in ()).throw(ValueError("it is a double"))
        )
    )
    assert "restore" not in _written(runtime, {}, {}, "1", "0")
    assert _written(runtime, {}, {}, "1", "0", restorable=True)["restore"] == {"value": "1"}
    assert _written(runtime, {}, {}, "0.1", "0", restorable=True)["restore"] == {
        "refused": "it is a double"
    }
    plain = _written(SimpleNamespace(), {}, {}, "1", "0", restorable=True)
    assert plain["restore"] == {"refused": "Peeky restores no value of this language's"}


def test_a_restore_line_whose_calls_cannot_be_watched_is_refused_before_the_run(tmp_path):
    worker = DebugWorker(tmp_path)

    def gone(program, lines):
        raise ValueError("line 13 is in `gen`, a generator or coroutine")

    with pytest.raises(Refused) as caught:
        worker.watch_entries(SimpleNamespace(entry_lines=gone), tmp_path / "a.py", [13])
    assert caught.value.before_launch
    assert str(caught.value).startswith("`@before` cannot be used here: line 13 is in `gen`")
    assert "Refused before the program runs" in str(caught.value)
    found = {13: [3, 3, 1]}
    runtime = SimpleNamespace(entry_lines=lambda program, lines: found)
    assert worker.watch_entries(runtime, tmp_path / "a.py", [13, 20]) == [[13, [1, 3]], [20, []]]
    with pytest.raises(Refused, match="cannot watch this language"):
        worker.watch_entries(SimpleNamespace(), tmp_path / "a.py", [13])


def test_the_worker_checks_the_new_arguments(tmp_path):
    worker = DebugWorker(tmp_path)
    for lines in ("35", [0], [1.5], list(range(1, 10))):
        with pytest.raises(ValueError, match="entry_lines must be a list"):
            worker.validate("launch", {"program": "a.c", "entry_lines": lines})
    with pytest.raises(ValueError, match="restorable must be true or false"):
        worker.validate(
            "set_variable",
            {
                "session_id": "s",
                "stop_id": "t",
                "reference": 1,
                "name": "x",
                "value": "1",
                "restorable": 1,
            },
        )


# The evidence.


def test_restores_are_saved_and_queried_as_changes(monkeypatch, tmp_path):
    store = tmp_path / "evidence" / "evidence.db"
    evidence.record(store, run(monkeypatch, Passes(passes("1", "-1")), flip()))
    rows = evidence.query(
        store, "SELECT line, hit, name, before, after FROM interventions ORDER BY rowid", {}
    )["rows"]
    assert [(r["line"], r["hit"], r["name"], r["before"], r["after"]) for r in rows] == [
        (GUARD, 1, "x", "1", "0"), (FLAT, 1, "x", "0", "1"),
        (GUARD, 2, "x", "-1", "0"), (FLAT, 2, "x", "0", "-1"),
    ]  # fmt: skip
    evidence.record(store, run(monkeypatch, Passes(passes("1")), flip()))
    sets = {
        row["run_id"]: json.loads(row["sets"])
        for row in evidence.run_query(store, "outputs", {"a": 1, "b": 2})["rows"]
    }
    assert sets[1] == [
        ["x", "0", f"requested at {GUARD}@all, made at 2 stops, to the program's end"],
        ["x", BEFORE, f"requested at {FLAT}@all, made at 2 stops, to the program's end"],
    ]
