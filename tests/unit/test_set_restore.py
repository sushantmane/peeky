"""`--set-at LINE@HIT name=@before`: write back the value a variable held just before
the probe's latest change to it, made at the same hit of its own line, in the same call.
tides: testing "skip `turn = p` on a stand" took six `--set-at`s, with
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
# What a warning says to do about a change with no matching restore.
ADVICE = " To undo a change, restore it on a line that runs after it in every pass."


class Calls:
    """Stops of main.c, each {line, function, depth, thread, call, callers}: a call is a
    frame instance, whose `x` keeps its own value. Only lines with a breakpoint stop; a
    stop's `call` names the frame its `x` lives in. ``firsts``: the worker's first lines
    for each restore line; ``exact``: whether a write's `before` is kept to restore;
    ``proven``: whether a write of the value already there is proven to change nothing."""

    def __init__(self, stops, firsts=None, exact=True, values=None, moved=None, proven=False):
        self.order = [dict(stop) for stop in stops]
        self.firsts = firsts if firsts is not None else {FLAT: [FIRST]}
        self.exact, self.moved, self.proven = exact, moved or {}, proven
        self.values = dict(values or {})
        self.placed, self.calls, self.writes, self.stop, self.launch = [], [], [], None, None
        self.asked = []

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
                        # `y` is another name for `x`'s storage (an alias).
                        "variables": [
                            {"name": name, "value": self._value(), "variablesReference": 0}
                            for name in ("x", "y")
                        ],
                    }
                ],
            }
        if operation == "set_variable":
            self.asked.append(args)
            # Which storage `x` is here: its declaration, unless a stop names another.
            identity = self.stop.get("storage", "the local `x`")
            if "target" in args and args["target"] != identity:
                raise Refused(
                    "MUTATION_REJECTED",
                    f"`x` cannot be changed back here: it is {identity}, not {args['target']}, "
                    "which the change was made to. The value from before that change is not "
                    "this one's",
                )
            before = self._value()
            self.values[self._frame()] = args["value"]
            self.writes.append((self.stop["line"], args["value"]))
            result = {"name": args["name"], "before": before, "after": args["value"]}
            if self.proven and before == args["value"]:
                result["unchanged"] = True
            if args.get("restorable"):
                result["restore"] = (
                    {"value": before, "target": identity}
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
    assert (
        f"set x: 0 -> -1  (restored, --set-at line {FLAT}@all, stop 3 of line {FLAT}, in f)" in text
    ), text
    assert f"set x: -1 -> 0  (--set-at line {GUARD}@all, stop 3 of line {GUARD}, in f)" in text, (
        text
    )
    # Each restore is one of the run's outcomes, as any change is.
    assert [o["value"] for o in result["set_outcomes"]] == ["0", BEFORE]
    assert result["set_outcomes"][1]["made"] == 3 and result["set_outcomes"][1]["complete"]


def test_many_restores_are_counted_as_restores(monkeypatch):
    worker = Passes(passes(*["1", "-1"] * 4))
    result = run(monkeypatch, worker, flip())
    text = probe_module._table(result)
    assert (
        f"set x: 5 more restores by --set-at line {FLAT}@all, at stops 4 to 8 of line {FLAT}"
        in text
    ), text
    assert (
        f"set x: 5 more changes by --set-at line {GUARD}@all, at stops 4 to 8 of line {GUARD}"
        in text
    ), text
    # The value written back may be the one already there: said so, as for any write.
    worker = Passes(passes(*["0"] * 6))
    text = probe_module._table(run(monkeypatch, worker, flip()))
    assert (
        f"set x: 3 more restores by --set-at line {FLAT}@all, at stops 4 to 6 of line {FLAT}, each with the "
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
    # The restore's line stops before the change's line in
    # each pass; the advice names that, as COOKBOOK 2.8 does.
    assert (
        f"This stop of line {FLAT} came before line {GUARD}'s change, so restore at a line that "
        f"stops once per pass, after line {GUARD} (COOKBOOK 2.8)"
    ) in message, message
    assert classify(Refused("MUTATION_REJECTED", message))["hint"].startswith(
        "Give the value itself"
    )


def test_a_restore_with_no_change_of_its_name_planned_says_to_make_one():
    restores = probe_module._Restores(
        [
            {"name": "x", "value": BEFORE, "line": FLAT, "build_line": FLAT, "hit": ALL},
            {"name": "y", "value": "0", "line": GUARD, "build_line": GUARD, "hit": ALL},
        ]
    )
    with pytest.raises(Refused) as caught:
        restores.value("x", FLAT, 1, {})
    message = str(caught.value)
    assert message.endswith(
        "a change made at the same hit, so make that change at an earlier line "
        "(--set-at LINE@1 x=VALUE)"
    ), message
    assert "COOKBOOK 2.8" not in message


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
    # A first-line stop whose depth Peeky could not count may be a new call too.
    stops = [{"line": FIRST}, {"line": GUARD}, {"line": FIRST, "depth": None}, {"line": FLAT}]
    assert "ran again since" in refusal(run(monkeypatch, Calls(stops), flip()))
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
    # A stop at the first line of a function on another restore's line is not this call's
    # beginning.
    request = Probe(
        "main.c", GUARD, ("x",), hits="all",
        set_at=((GUARD, ALL, "x", "0"), (FLAT, ALL, "x", BEFORE), (9, 1, "x", BEFORE)),
    )  # fmt: skip
    worker = Calls([{"line": 3}, {"line": GUARD}, {"line": FLAT}], firsts={FLAT: [FIRST], 9: [3]})
    message = refusal(run(monkeypatch, worker, request))
    assert "Peeky did not see the call of f that changed `x` at line 5@1 begin" in message
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


def test_a_restore_to_other_storage_is_refused_before_it_writes(monkeypatch):
    # Java `b.n=0`, then `b = new Box(9)`, then `b.n=@before` wrote 1
    # into the new Box. The worker compares the storage the change wrote with this one's.
    stops = [{"line": FIRST}, {"line": GUARD}, {"line": FLAT, "storage": "`n` of Box@31"}]
    worker = Calls(stops)
    message = refusal(run(monkeypatch, worker, flip()))
    assert "x=@before was refused at hit 1 of line 8 (in f): `x` cannot be changed back here:" in (
        message
    )
    assert "it is `n` of Box@31, not the local `x`, which the change was made to" in message
    assert worker.writes == [(GUARD, "0")]
    # The restore named the storage its change wrote; the change asked for it.
    assert [(args.get("restorable"), args.get("target")) for args in worker.asked] == [
        (True, None),
        (True, "the local `x`"),
    ]


def test_a_restore_over_another_value_says_so(monkeypatch):
    # The program wrote `x` (2) after the change (0): the restore writes over it.
    stops = [{"line": FIRST}, {"line": GUARD, "set": "1"}, {"line": FLAT, "set": "2"}]
    result = run(monkeypatch, Passes(stops), flip())
    restore = result["interventions"][1]
    assert (restore["before"], restore["after"], restore["overwrote"]) == ("2", "1", "2")
    text = probe_module._table(result)
    assert (
        "set x: 2 -> 1  (restored over the current 2, --set-at line 8@all, stop 1 of line 8, in f)"
        in (text)
    )
    # The probe's own write through another name for
    # the same storage (`y` here; Java `c.n`, `c` being `b`'s Box) is no write of the
    # program's, so nothing says whose it was.
    request = Probe(
        "main.c", GUARD, ("x",), hits="all",
        set_at=((GUARD, ALL, "x", "0"), (7, ALL, "y", "6"), (FLAT, ALL, "x", BEFORE)),
    )  # fmt: skip
    stops = [{"line": FIRST}, {"line": GUARD, "set": "1"}, {"line": 7}, {"line": FLAT}]
    result = run(monkeypatch, Passes(stops), request)
    restore = result["interventions"][2]
    assert (restore["before"], restore["after"], restore["overwrote"]) == ("6", "1", "6")
    text = probe_module._table(result)
    assert "set x: 6 -> 1  (restored over the current 6, --set-at line 8@all" in text, text
    assert "program" not in text.split("set x: 6 -> 1")[1].splitlines()[0]
    # Where the value is still the one the change wrote, nothing is said.
    result = run(monkeypatch, Passes(passes("1")), flip())
    assert "overwrote" not in result["interventions"][1]


def test_until_hits_waits_for_the_restore_its_last_change_awaits(monkeypatch):
    # `--hits 2 --until hits` ended after the guard's second change,
    # before the restore that undoes it, with the guard left at 0.
    request = Probe(
        "main.c", GUARD, ("x",), hits=2, until="hits",
        set_at=((GUARD, ALL, "x", "0"), (FLAT, ALL, "x", BEFORE)),
    )  # fmt: skip
    worker = Passes(passes("1", "1", "1"))
    result = run(monkeypatch, worker, request)
    assert result["status"] == "completed" and result["ended"] == "hits", result
    assert worker.writes == [(GUARD, "0"), (FLAT, "1"), (GUARD, "0"), (FLAT, "1")]
    assert "unapplied" not in result and probe_exit_code(result) == 0
    # The third pass ran without the probe: it ended after the restore.
    assert [stop["line"] for stop in worker.order] == [GUARD, FLAT]


def test_a_change_never_restored_is_reported_and_exits_4(monkeypatch):
    # The restore line's second stop never comes: the guard's second change stays.
    request = Probe(
        "main.c", GUARD, ("x",), hits=2, until="hits",
        set_at=((GUARD, ALL, "x", "0"), (FLAT, ALL, "x", BEFORE)),
    )  # fmt: skip
    stops = [{"line": FIRST}, {"line": GUARD, "set": "1"}, {"line": FLAT}, {"line": GUARD}]
    result = run(monkeypatch, Passes(stops), request)
    assert result["status"] == "completed" and probe_exit_code(result) == 4, result
    assert result["unapplied"] == [
        {
            "option": "--set-at",
            "at": f"{FLAT}@all",
            "name": "x",
            "value": BEFORE,
            "stops": 1,
            "unrestored": f"{GUARD}@2",
            "unrestored_all": [f"{GUARD}@2"],
            "unrestored_kinds": ["change"],
        }
    ]
    warning = next(w for w in result["warnings"] if "made no restore" in w)
    assert warning == (
        f"--set-at main.c:{FLAT}@all x=@before made no restore for the change at line "
        f"{GUARD}@2. Line {FLAT} stopped 1 time(s) before the probe ended, so its hit 2 never "
        "came." + ADVICE
    )
    outcome = result["set_outcomes"][1]
    assert (outcome["unrestored"], outcome["complete"]) == (f"{GUARD}@2", False)
    assert (
        f"status: completed (1 change had no matching restore (line {GUARD}, hit 2))"
    ) in probe_module._table(result)


def test_a_later_change_owes_no_restore(monkeypatch):
    # `x=0`, its restore, then a later `x=2` all ran,
    # yet the restore was said not to undo the change at line 9@1, with exit 4.
    request = Probe(
        "main.c", GUARD, ("x",), hits="all",
        set_at=((GUARD, ALL, "x", "0"), (FLAT, ALL, "x", BEFORE), (9, ALL, "x", "2")),
    )  # fmt: skip
    stops = [{"line": FIRST}, {"line": GUARD, "set": "1"}, {"line": FLAT}, {"line": 9}]
    result = run(monkeypatch, Passes(stops), request)
    assert result["status"] == "completed" and probe_exit_code(result) == 0, result
    assert "unapplied" not in result, result.get("unapplied")
    assert not [w for w in result["warnings"] if "made no restore" in w]
    assert all(outcome["complete"] for outcome in result["set_outcomes"])


def chained(passes_made):
    """Passes of the guard (`x=0`), a restore at the flat line, and a second restore at
    line 9, which undoes the first."""
    stops = [{"line": FIRST}]
    for _ in range(passes_made):
        stops += [{"line": GUARD, "set": "1"}, {"line": FLAT}, {"line": 9}]
    return stops


# The first line each restore line's function begins at.
FIRSTS = {FLAT: [FIRST], 9: [FIRST]}
CHAINED = Probe(
    "main.c", GUARD, ("x",), hits=2, until="hits",
    set_at=((GUARD, ALL, "x", "0"), (FLAT, ALL, "x", BEFORE), (9, ALL, "x", BEFORE)),
)  # fmt: skip


def test_until_hits_waits_for_a_restore_of_a_restore(monkeypatch):
    # With `--hits 2 --until hits`, the probe ended after
    # line 8 on pass 2, as the latest change was a restore, and line 9's never came.
    worker = Passes(chained(3), firsts=FIRSTS)
    result = run(monkeypatch, worker, CHAINED)
    assert result["status"] == "completed" and result["ended"] == "hits", result
    assert worker.writes == [(GUARD, "0"), (FLAT, "1"), (9, "0")] * 2
    assert [(c["at"], c.get("restores")) for c in result["interventions"]][-2:] == [
        (f"{FLAT}@2", f"{GUARD}@2"),
        ("9@2", f"{FLAT}@2"),
    ]
    assert "unapplied" not in result and probe_exit_code(result) == 0
    # Where line 9's second stop never comes, its restore is left undone, and said so.
    stops = chained(2)[:-1]
    result = run(monkeypatch, Passes(stops, firsts=FIRSTS), CHAINED)
    assert probe_exit_code(result) == 4, result
    assert [(e["at"], e["stops"], e["unrestored"]) for e in result["unapplied"]] == [
        ("9@all", 1, f"{FLAT}@2")
    ]


def test_a_restore_whose_line_never_stopped_is_listed_once(monkeypatch):
    # It was listed as not made, and again as leaving
    # its change in place.
    stops = [{"line": FIRST}, {"line": GUARD, "set": "1"}]
    result = run(monkeypatch, Passes(stops), flip())
    assert result["unapplied"] == [
        {
            "option": "--set-at",
            "at": f"{FLAT}@all",
            "name": "x",
            "value": BEFORE,
            "stops": 0,
            "unrestored": f"{GUARD}@1",
            "unrestored_all": [f"{GUARD}@1"],
            "unrestored_kinds": ["change"],
        }
    ]
    assert probe_exit_code(result) == 4
    text = probe_module._table(result)
    assert (
        "status: completed (1 requested change not made; 1 change had no matching restore "
        f"(line {GUARD}, hit 1))"
    ) in text, text
    outcome = result["set_outcomes"][1]
    assert (outcome["made"], outcome["unrestored"], outcome["complete"]) == (0, f"{GUARD}@1", False)


def flips(count):
    """One call of `f`, then `count` passes of the guard alone: once `x` is 0, the restore's
    line no longer runs."""
    return [{"line": FIRST}] + [{"line": GUARD, "set": "1"}] * count


def test_every_change_a_restore_owed_is_named_not_only_the_latest(monkeypatch):
    # rainfall: the guard was flipped at each of 6 hits, and the
    # restore's line never ran once it was. None of the 6 was restored, yet the run said
    # "1 change left in place" and named only the one at hit 6.
    result = run(monkeypatch, Passes(flips(6)), flip())
    ats = [f"{GUARD}@{hit}" for hit in range(1, 7)]
    assert result["status"] == "completed" and probe_exit_code(result) == 4, result
    text = probe_module._table(result)
    assert (
        "status: completed (1 requested change not made; 6 changes had no matching restore "
        f"(line {GUARD}, hits 1 to 6))"
    ) in text, text
    assert result["unapplied"] == [
        {
            "option": "--set-at",
            "at": f"{FLAT}@all",
            "name": "x",
            "value": BEFORE,
            "stops": 0,
            "unrestored": f"{GUARD}@6",
            "unrestored_all": ats,
            "unrestored_kinds": ["change"] * 6,
        }
    ]
    warning = next(w for w in result["warnings"] if "made no restore" in w)
    assert warning == (
        f"--set-at main.c:{FLAT}@all x=@before made no restore for 6 changes (line {GUARD}, "
        f"hits 1 to 6). Line {FLAT} stopped 0 time(s) before the probe ended, so its hits 1 to "
        "6 never came." + ADVICE
    )
    outcome = result["set_outcomes"][1]
    assert (outcome["unrestored"], outcome["unrestored_all"]) == (f"{GUARD}@6", ats)
    assert outcome["complete"] is False


def test_writes_that_changed_nothing_are_not_called_changes(monkeypatch):
    # The guard changes `x` at hit 1 only. At hits 2 to 6 it writes the
    # 0 already there, which the worker proves changed nothing, yet each was called a
    # change left in place.
    stops = flips(1) + [{"line": GUARD}] * 5
    result = run(monkeypatch, Passes(stops, proven=True), flip())
    assert [change["unchanged"] for change in result["interventions"][1:]] == [True] * 5
    text = probe_module._table(result)
    assert (
        "6 writes had no matching restore: 1 change and 5 with no change "
        f"(line {GUARD}, hits 1 to 6))"
    ) in text, text
    [entry] = result["unapplied"]
    assert entry["unrestored_all"] == [f"{GUARD}@{hit}" for hit in range(1, 7)]
    assert entry["unrestored_kinds"] == ["change"] + ["unchanged"] * 5
    assert (
        f"made no restore for 6 writes (line {GUARD}, hits 1 to 6): 1 change and 5 with no "
        f"change. Line {FLAT} stopped 0 time(s)"
    ) in next(w for w in result["warnings"] if "made no restore" in w)
    # The same text, not proven to change nothing, is said so.
    text = probe_module._table(run(monkeypatch, Passes(stops), flip()))
    assert (
        "6 writes had no matching restore: 1 change and 5 with the same text, not proven "
        f"unchanged (line {GUARD}, hits 1 to 6))"
    ) in text, text
    # Writes of one kind, or one write, say it once.
    stops = [{"line": FIRST}] + [{"line": GUARD, "set": "0"}] * 3
    result = run(monkeypatch, Passes(stops, proven=True), flip())
    text = probe_module._table(result)
    assert (
        f"3 writes had no matching restore, each with no change (line {GUARD}, hits 1 to 3))"
    ) in text, text
    result = run(monkeypatch, Passes(stops[:2], proven=True), flip())
    assert (
        f"1 write had no matching restore, with no change (line {GUARD}, hit 1))"
    ) in probe_module._table(result)
    assert (
        f"made no restore for the write at line {GUARD}@1, with no change. Line {FLAT}"
    ) in next(w for w in result["warnings"] if "made no restore" in w)


def test_writes_to_several_names_at_one_place_are_each_counted(monkeypatch):
    # The status counted writes by LINE@HIT alone, so `x` and `y`, both
    # written at 5@1 and never restored, were "1 change".
    request = Probe(
        "main.c", GUARD, ("x",), hits="all",
        set_at=(
            (GUARD, ALL, "x", "0"), (GUARD, ALL, "y", "2"),
            (FLAT, ALL, "x", BEFORE), (FLAT, ALL, "y", BEFORE),
        ),
    )  # fmt: skip
    result = run(monkeypatch, Passes(flips(1)), request)
    assert [(e["name"], e["unrestored_all"]) for e in result["unapplied"]] == [
        ("x", [f"{GUARD}@1"]),
        ("y", [f"{GUARD}@1"]),
    ]
    text = probe_module._table(result)
    assert (
        "status: completed (2 requested changes not made; 2 changes had no matching restore "
        f"(line {GUARD}, hit 1: x and y))"
    ) in text, text
    # A write two restores of one name owed is counted once.
    request = Probe(
        "main.c", GUARD, ("x",), hits="all",
        set_at=((GUARD, ALL, "x", "0"), (FLAT, ALL, "x", BEFORE), (9, ALL, "x", BEFORE)),
    )  # fmt: skip
    result = run(monkeypatch, Passes(flips(2), firsts=FIRSTS), request)
    assert [e["unrestored_all"] for e in result["unapplied"]] == [[f"{GUARD}@1", f"{GUARD}@2"]] * 2
    assert (
        f"2 changes had no matching restore (line {GUARD}, hits 1 and 2))"
    ) in probe_module._table(result)


def test_changes_restored_or_replaced_at_their_hit_are_not_counted(monkeypatch):
    # Hits 1 and 2 are restored; then the restore's line stops no more. What a restore
    # owes is kept: only hits 3 to 5 have no matching restore.
    result = run(monkeypatch, Passes(passes("1", "1") + flips(3)[1:]), flip())
    [entry] = result["unapplied"]
    assert (entry["stops"], entry["unrestored"], entry["unrestored_all"]) == (
        2,
        f"{GUARD}@5",
        [f"{GUARD}@3", f"{GUARD}@4", f"{GUARD}@5"],
    )
    assert [w for w in result["warnings"] if "made no restore" in w] == [
        f"--set-at main.c:{FLAT}@all x=@before made no restore for 3 changes (line {GUARD}, "
        f"hits 3 to 5). Line {FLAT} stopped 2 time(s) before the probe ended, so its hits 3 to "
        "5 never came." + ADVICE
    ]
    text = probe_module._table(result)
    # Each restore made counts as made: none is a change not made.
    assert (
        f"status: completed (3 changes had no matching restore (line {GUARD}, hits 3 to 5))"
    ) in text, text
    # A restore undoes the latest change at its hit: a change at that hit before it, which
    # the change it undoes overwrote, is not its to undo.
    request = Probe(
        "main.c", GUARD, ("x",), hits="all",
        set_at=((GUARD, ALL, "x", "0"), (6, ALL, "x", "2"), (FLAT, ALL, "x", BEFORE)),
    )  # fmt: skip
    stops = [{"line": FIRST}] + [{"line": GUARD, "set": "1"}, {"line": 6}] * 2
    result = run(monkeypatch, Passes(stops), request)
    assert result["unapplied"][0]["unrestored_all"] == ["6@1", "6@2"], result["unapplied"]
    assert "2 changes had no matching restore (line 6, hits 1 and 2)" in probe_module._table(result)


def test_a_restore_at_one_hit_names_the_change_it_never_restored(monkeypatch):
    # The flat line stops twice, and its hit 3, the restore's, never comes: the guard's
    # change at hit 3 has no matching restore. Those at hits 1 and 2 were never this
    # restore's to undo.
    request = Probe(
        "main.c", GUARD, ("x",), hits="all",
        set_at=((GUARD, ALL, "x", "0"), (FLAT, 3, "x", BEFORE)),
    )  # fmt: skip
    stops = passes("1", "1") + flips(1)[1:]
    result = run(monkeypatch, Passes(stops), request)
    assert probe_exit_code(result) == 4, result
    assert result["unapplied"] == [
        {
            "option": "--set-at",
            "at": f"{FLAT}@3",
            "name": "x",
            "value": BEFORE,
            "stops": 2,
            "unrestored": f"{GUARD}@3",
            "unrestored_all": [f"{GUARD}@3"],
            "unrestored_kinds": ["change"],
        }
    ]
    assert [w for w in result["warnings"] if f"{FLAT}@3" in w] == [
        f"--set-at main.c:{FLAT}@3 x=@before was not applied: line {FLAT} stopped 2 time(s) "
        "before the probe ended, so its hit 3 never came.",
        f"--set-at main.c:{FLAT}@3 x=@before made no restore for the change at line {GUARD}@3. "
        f"Line {FLAT} stopped 2 time(s) before the probe ended, so its hit 3 never came." + ADVICE,
    ]
    text = probe_module._table(result)
    assert (
        "status: completed (1 requested change not made; 1 change had no matching restore "
        f"(line {GUARD}, hit 3))"
    ) in text, text
    outcome = result["set_outcomes"][1]
    assert (outcome["made"], outcome["unrestored"], outcome["complete"]) == (0, f"{GUARD}@3", False)


# A line before the loop, whose stops run ahead of the guard's.
EARLY = 3
DIVERGED = Probe(
    "main.c", GUARD, ("x",), hits=1, until="hits",
    set_at=((EARLY, ALL, "x", "7"), (GUARD, ALL, "x", "0"), (FLAT, ALL, "x", BEFORE)),
)  # fmt: skip


def diverged(*after):
    """One call of `f`, three stops of EARLY (each a change of `x` at hits 1 to 3), then
    ``after``'s stops."""
    return [{"line": FIRST}] + [{"line": EARLY, "set": "2"}] * 3 + list(after)


def test_until_hits_waits_for_an_earlier_hit_still_owed(monkeypatch):
    # Where the lines' hit counts differ, the latest change (the flat
    # line's restore at hit 1) is owed nothing, but EARLY's at hits 2 and 3 are, and the
    # probe waits for the flat line to reach them. What happens then:
    pass_ = [{"line": GUARD, "set": "1"}, {"line": FLAT}]
    # 1. The guard changes `x` at hit 2, then hit 3, each before the flat line's stop: the
    # restore undoes that latest change at its hit, which EARLY's was written over by, and
    # the probe ends after the last owed one (--until hits). EARLY's are not restored, and
    # no longer owed.
    worker = Passes(diverged(*pass_ * 4))
    result = run(monkeypatch, worker, DIVERGED)
    assert result["status"] == "completed" and result["ended"] == "hits", result
    assert worker.writes == [(EARLY, "7")] * 3 + [(GUARD, "0"), (FLAT, "1")] * 3
    assert "unapplied" not in result and probe_exit_code(result) == 0
    assert [stop["line"] for stop in worker.order] == [GUARD, FLAT]
    # 2. The program ends first, after the flat line's hit 2: EARLY's change at hit 3 had
    # no matching restore, and is reported as such (exit 4).
    result = run(monkeypatch, Passes(diverged(*pass_ * 2)), DIVERGED)
    assert result["status"] == "completed" and probe_exit_code(result) == 4, result
    assert [(e["at"], e["stops"], e["unrestored_all"]) for e in result["unapplied"]] == [
        (f"{FLAT}@all", 2, [f"{EARLY}@3"])
    ]
    assert (
        f"status: completed (1 change had no matching restore (line {EARLY}, hit 3))"
    ) in probe_module._table(result)
    # 3. The flat line's hit 2 comes with no change at hit 2 since: the latest is at hit 1,
    # so the restore is refused, as any restore at another hit's change is (exit 1).
    result = run(monkeypatch, Passes(diverged(*pass_, {"line": FLAT})), DIVERGED)
    assert f"`x` was last changed at hit 1 of line {FLAT}, not at hit 2" in refusal(result), result
    assert probe_exit_code(result) == 1


def test_hits_and_changes_are_named_as_people_read_them():
    from peeky.names import changes_named, hits_named

    assert hits_named([4]) == "hit 4"
    assert hits_named([1, 2]) == "hits 1 and 2"
    assert hits_named([7, 1, 2, 3, 5]) == "hits 1 to 3, 5 and 7"
    assert changes_named(["49@1", "49@2", "49@3", "52@2", "49@6"]) == (
        "line 49, hits 1 to 3 and 6; line 52, hit 2"
    )
    # Several selectors: each place names its own.
    assert changes_named(["5@1", "5@2", "5@3", "5@1"], ["x", "x", "x", "y"]) == (
        "line 5, hits 1 to 3: x; line 5, hit 1: y"
    )
    assert changes_named(["5@1", "5@1", "7@2"], ["x", "y", "y"]) == (
        "line 5, hit 1: x and y; line 7, hit 2: y"
    )


def test_watch_stops_are_counted_apart(monkeypatch):
    result = run(monkeypatch, Passes(passes("1", "1")), flip())
    # The first line's one stop; the flat line's two (a --set-at's) stay set pauses.
    assert len(result["watch_pauses_ms"]) == 1 and len(result["set_pauses_ms"]) == 2
    # It said "1 stops".
    assert "watching for new calls: 1 stop, max" in probe_module._table(result)
    more = {**result, "watch_pauses_ms": [1.5, 2.5]}
    assert "watching for new calls: 2 stops, max 2.5 ms" in probe_module._table(more)


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
    kept = ("the local `x`", None)
    assert "restore" not in _written(runtime, {}, {}, "1", "0", target=kept)
    assert _written(runtime, {}, {}, "1", "0", restorable=True, target=kept)["restore"] == {
        "value": "1",
        "target": "the local `x`",
    }
    assert _written(runtime, {}, {}, "0.1", "0", restorable=True, target=kept)["restore"] == {
        "refused": "it is a double"
    }
    plain = _written(SimpleNamespace(), {}, {}, "1", "0", restorable=True, target=kept)
    assert plain["restore"] == {"refused": "Peeky restores no value of this language's"}
    # A value Peeky can write back, but to a storage it can't name, is not kept either.
    unnamed = _written(runtime, {}, {}, "1", "0", restorable=True, target=(None, "no table"))
    assert unnamed["restore"] == {"refused": "Peeky cannot tell which storage it wrote (no table)"}


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


def test_the_evidence_links_each_restore_to_the_change_it_undid(monkeypatch, tmp_path):
    store = tmp_path / "evidence" / "evidence.db"
    evidence.record(store, run(monkeypatch, Passes(passes("1", "-1")), flip()))
    rows = evidence.query(store, "SELECT restores_at FROM interventions ORDER BY rowid", {})
    assert [row["restores_at"] for row in rows["rows"]] == [None, "5@1", None, "5@2"]
    effect = evidence.run_query(store, "set_effect", {"run": 1})["rows"]
    texts = [row["interventions"] for row in effect]
    assert "x: 0 -> 1 (restored from before 5@1)" in texts, texts
    table = evidence.query_table(store, evidence.run_query(store, "set_effect", {"run": 1}), {})
    assert "x: 0 -> -1 (restored from before 5@2)" in table, table
    # diff's and compare's (sets) row name the restore, which `…` would hide.
    sets = json.dumps(
        [
            ["x", "0", "requested at 5@all, made at 2 stops, to the program's end"],
            ["x", BEFORE, "requested at 8@all, made at 2 stops, to the program's end"],
        ]
    )
    assert evidence._sets_text(sets) == (
        "2 requests: x=0 (5@all, made at 2 stops, to the program's end), …; "
        "restore: x=@before (8@all, made at 2 stops, to the program's end)"
    )


def test_the_evidence_keeps_a_write_with_no_matching_restore(monkeypatch, tmp_path):
    store = tmp_path / "evidence" / "evidence.db"
    request = Probe(
        "main.c", GUARD, ("x",), hits=2, until="hits",
        set_at=((GUARD, ALL, "x", "0"), (FLAT, ALL, "x", BEFORE)),
    )  # fmt: skip
    stops = [{"line": FIRST}, {"line": GUARD, "set": "1"}, {"line": FLAT}, {"line": GUARD}]
    evidence.record(store, run(monkeypatch, Passes(stops), request))
    evidence.record(store, run(monkeypatch, Passes(passes("1")), flip()))
    sets = {
        row["run_id"]: json.loads(row["sets"])
        for row in evidence.run_query(store, "outputs", {"a": 1, "b": 2})["rows"]
    }
    assert sets[1][1] == [
        "x",
        BEFORE,
        # The probe waited for the restore, and the program ended first.
        f"requested at {FLAT}@all, made at 1 stop, to the program's end; the write at "
        f"{GUARD}@2 had no matching restore",
    ]
    # An outcome is read only where it fits its request: `unrestored` only on a restore.
    entry = json.dumps(["x", "0", "5@all"])
    outcome = {
        "option": "--set-at", "at": "5@all", "name": "x", "value": "0", "made": 2,
        "ended": "exit", "complete": False, "unrestored": "5@2",
    }  # fmt: skip
    assert evidence._checked_outcome(json.dumps(outcome), entry, 5, None) is None
    restore = {**outcome, "value": BEFORE}
    entry = json.dumps(["x", BEFORE, "5@all"])
    assert evidence._checked_outcome(json.dumps(restore), entry, 5, None) == restore
    assert (
        evidence._checked_outcome(json.dumps({**restore, "complete": True}), entry, 5, None) is None
    )


def test_the_evidence_names_every_write_with_no_matching_restore(monkeypatch, tmp_path):
    store = tmp_path / "evidence" / "evidence.db"
    evidence.record(store, run(monkeypatch, Passes(flips(3)), flip()))
    evidence.record(store, run(monkeypatch, Passes(passes("1")), flip()))
    sets = {
        row["run_id"]: json.loads(row["sets"])
        for row in evidence.run_query(store, "outputs", {"a": 1, "b": 2})["rows"]
    }
    assert sets[1][1] == [
        "x",
        BEFORE,
        # Whether each write changed the value is not kept: they are writes.
        f"requested at {FLAT}@all, not made: its line never stopped; 3 writes had no matching "
        f"restore (line {GUARD}, hits 1 to 3)",
    ]

    def checked(outcome, at="8@all"):
        entry = json.dumps(["x", BEFORE, at])
        return evidence._checked_outcome(json.dumps(outcome), entry, 5, None)

    outcome = {
        "option": "--set-at", "at": "8@all", "name": "x", "value": BEFORE, "made": 0,
        "ended": "exit", "complete": False, "unrestored": "5@3",
        "unrestored_all": ["5@1", "5@2", "5@3"],
    }  # fmt: skip
    assert checked(outcome) == outcome
    # The latest is `unrestored`; one per hit, each a LINE@HIT; never without `unrestored`.
    for wrong in (["5@1", "5@2"], ["6@3", "5@3"], "5@3", [], [3, "5@3"], ["5@0", "5@3"]):
        assert checked({**outcome, "unrestored_all": wrong}) is None, wrong
    assert checked({k: v for k, v in outcome.items() if k != "unrestored"}) is None
    # A restore at one hit, never made, leaves only the change at its own hit.
    one = {**outcome, "at": "8@3", "unrestored_all": ["5@3"]}
    assert checked(one, "8@3") == one
    assert checked({**one, "unrestored": "5@2", "unrestored_all": ["5@2"]}, "8@3") is None
    assert checked({**one, "made": 1, "ended": "made"}, "8@3") is None


def test_set_effect_marks_a_write_proven_to_change_nothing(monkeypatch, tmp_path):
    # rainfall's run 2, hit 5: set_effect showed
    # `in_spell: False -> False`, where the probe's own text says `no change`.
    store = tmp_path / "evidence" / "evidence.db"
    evidence.record(store, run(monkeypatch, Passes(passes("0", "1"), proven=True), flip()))
    rows = evidence.query(store, "SELECT unchanged FROM interventions ORDER BY rowid", {})
    assert [row["unchanged"] for row in rows["rows"]] == [1, 1, 0, 0]
    texts = [
        row["interventions"] for row in evidence.run_query(store, "set_effect", {"run": 1})["rows"]
    ]
    assert texts == [
        "x: 0 -> 0, no change", "x: 0 -> 0, no change (restored from before 5@1)",
        "x: 1 -> 0", "x: 0 -> 1 (restored from before 5@2)",
    ], texts  # fmt: skip
    table = evidence.query_table(store, evidence.run_query(store, "set_effect", {"run": 1}), {})
    assert "x: 0 -> 0, no change (restored from before 5@1)" in table, table
    # The same text not proven unchanged is not marked.
    evidence.record(store, run(monkeypatch, Passes(passes("0")), flip()))
    texts = [
        row["interventions"] for row in evidence.run_query(store, "set_effect", {"run": 2})["rows"]
    ]
    assert texts == ["x: 0 -> 0", "x: 0 -> 0 (restored from before 5@1)"], texts


def test_a_schema_19_store_gains_the_unchanged_column(monkeypatch, tmp_path):
    import sqlite3

    store = tmp_path / "evidence" / "evidence.db"
    evidence.record(store, run(monkeypatch, Passes(passes("0"), proven=True), flip()))
    connection = sqlite3.connect(store)
    connection.executescript(
        "DROP VIEW last_run; DROP VIEW evidence; "
        "ALTER TABLE interventions DROP COLUMN unchanged; PRAGMA user_version = 19;"
    )
    connection.close()
    evidence.record(store, run(monkeypatch, Passes(passes("0"), proven=True), flip()))
    rows = evidence.query(store, "SELECT run_id, unchanged FROM interventions ORDER BY rowid", {})
    assert [(row["run_id"], row["unchanged"]) for row in rows["rows"]] == [
        (1, None), (1, None), (2, 1), (2, 1),
    ]  # fmt: skip
    assert evidence.query(store, "SELECT COUNT(*) AS n FROM last_run", {})["rows"][0]["n"] > 0


def test_a_schema_18_store_gains_the_restore_column(monkeypatch, tmp_path):
    import sqlite3

    store = tmp_path / "evidence" / "evidence.db"
    evidence.record(store, run(monkeypatch, Passes(passes("1")), flip()))
    connection = sqlite3.connect(store)
    connection.executescript(
        "ALTER TABLE interventions DROP COLUMN restores_at; PRAGMA user_version = 18;"
    )
    connection.close()
    evidence.record(store, run(monkeypatch, Passes(passes("1")), flip()))
    rows = evidence.query(store, "SELECT run_id, restores_at FROM interventions ORDER BY rowid", {})
    assert [(row["run_id"], row["restores_at"]) for row in rows["rows"]] == [
        (1, None), (1, None), (2, None), (2, "5@1"),
    ]  # fmt: skip
    connection = sqlite3.connect(store)
    assert connection.execute("PRAGMA user_version").fetchone() == (evidence.SCHEMA_VERSION,)
    connection.close()


def test_each_language_names_the_storage_a_change_writes():
    # Python: one local of each name in a call.
    assert PythonAdapter.storage_identity({"name": "trend"}) == "the local `trend`"
    # Native: the declaration the debugger shows, and the steps inside it.
    native = SimpleNamespace(_declaration_at=lambda frame, root: ("/w/a.c", 12))
    identity = NativeSession.storage_identity
    assert identity(native, {"name": "x"}) == "`x` (`x` declared at a.c:12)"
    assert identity(native, {"name": "lo"}, root={"name": "s"}, steps=("in", "lo")) == (
        "`s.in.lo` (`s` declared at a.c:12)"
    )
    assert identity(native, {"name": "[2]"}, root={"name": "xs"}, steps=("[2]",)) == (
        "`xs[2]` (`xs` declared at a.c:12)"
    )
    # A name LLDB gives with its declaration says it itself.
    assert identity(native, {"name": "x @ a.c:5"}) == "`x` (`x` declared at a.c:5)"
    unplaced = SimpleNamespace(_declaration_at=lambda frame, root: None)
    with pytest.raises(ValueError, match="which declaration of `x` the debugger shows"):
        identity(unplaced, {"name": "x"})
    # Java: a field or element by the object holding it; a local by its declaration.
    java = JavaSession.storage_identity
    method = {
        "name": "find",
        "line_pcs": {12: (20,), 30: (35, 90)},
        "slots": [("i", 3, 10, 30), ("i", 3, 70, 40), ("n", 4, 0, 100)],
    }
    session = SimpleNamespace(_frame_method=lambda frame: (method, frame["line"]))
    assert java(session, {"name": "n"}, parent={"value": "Box@27"}) == "`n` of Box@27"
    assert java(session, {"name": "2"}, parent={"value": "int[5]@33"}) == "`2` of int[5]@33"
    with pytest.raises(ValueError, match="which object holds `n`"):
        java(session, {"name": "n"}, parent={"value": "null"})
    assert java(session, {"name": "i"}, frame={"line": 12}) == (
        "`i` (declared in find: slot 3, bytecode 10 to 40)"
    )
    # Line 30 runs where each of the two `i`s is in scope: not singled out.
    with pytest.raises(ValueError, match="which declaration of `i` the debugger shows on line 30"):
        java(session, {"name": "i"}, frame={"line": 30})


def test_the_worker_writes_a_restore_only_to_the_storage_its_change_wrote(tmp_path):
    worker = DebugWorker(tmp_path)
    runtime = SimpleNamespace(storage_identity=lambda variable, **where: f"`{variable['name']}`")
    assert worker.storage_target(runtime, {}, {"name": "x"}, "x") == (None, None)
    assert worker.storage_target(runtime, {"restorable": True}, {"name": "x"}, "x") == ("`x`", None)
    assert worker.storage_target(runtime, {"target": "`x`"}, {"name": "x"}, "x") == ("`x`", None)
    with pytest.raises(ValueError, match="`y` cannot be changed back here: it is `y`, not `x`"):
        worker.storage_target(runtime, {"target": "`x`"}, {"name": "y"}, "y")

    def unknown(variable, **where):
        raise ValueError("Peeky cannot tell which declaration of `x` the debugger shows here")

    with pytest.raises(ValueError, match="cannot be changed back here: Peeky cannot tell which"):
        worker.storage_target(SimpleNamespace(storage_identity=unknown), {"target": "`x`"}, {}, "x")
    with pytest.raises(ValueError, match="target must be a nonempty string"):
        worker.validate(
            "set_variable",
            {
                "session_id": "s",
                "stop_id": "t",
                "reference": 1,
                "name": "x",
                "value": "1",
                "target": "",
            },
        )
