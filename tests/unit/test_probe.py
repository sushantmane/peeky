import re

import pytest

import peeky.probe as probe_module
from peeky.errors import classify
from peeky.probe import MAX_SETS, Probe, _table, parse_location, parse_selector


@pytest.mark.parametrize(
    "text, segments",
    [
        ("start", ["start"]),
        ("request.page", ["request", "page"]),
        ("args[0]", ["args", "[0]"]),
        ("a.b[12].c", ["a", "b", "[12]", "c"]),
        # String keys are kept in their canonical repr form, as debugpy names entries.
        ("order['id']", ["order", "['id']"]),
        ('order["id"]', ["order", "['id']"]),
        ('d["it\'s"]', ["d", '["it\'s"]']),
        ("d['a.b]c'][0].x", ["d", "['a.b]c']", "[0]", "x"]),
        ("d['']", ["d", "['']"]),
    ],
)
def test_selectors_are_plain_dotted_paths(text, segments):
    assert parse_selector(text) == segments


@pytest.mark.parametrize(
    "text",
    [
        "",
        "a..b",
        "[0]",
        "a[]",
        "a[x]",
        "a.b()",
        "$variants$",
        "a-b",
        "a;b",
        "x" * 600,
        "a." * 17,
        "a" + "[0]" * 16,
        "['id']",
        "a['x'",
        "a[b'x']",
        "a[f'{x}']",
        "a[r'x']",
        'a["""x"""]',
        "a['\\d']",
        "a['x' + 'y']",
        "a['x']()",
        'a["\n"]',
        "a['" + "x" * 257 + "']",
    ],
)
def test_selectors_reject_expressions_and_synthetic_names(text):
    with pytest.raises(ValueError):
        parse_selector(text)


def test_locations_require_relative_path_and_positive_line():
    assert parse_location("src/main.rs:36") == ("src/main.rs", 36)
    for text in ("main.rs", "main.rs:0", ":3", "main.rs:x", None):
        with pytest.raises(ValueError):
            parse_location(text)


@pytest.mark.parametrize(
    "change",
    [
        {"line": 0},
        {"capture": ["a"]},
        {"capture": ("a()",)},
        {"capture": ("a",) * 33},
        {"set": (("a['b']", "1"),)},
        {"set": (("a()", "1"),)},
        {"set": (("a", 1),)},
        {"set": (("a", "1"),) * 9},
        {"hits": 0},
        {"hits": 101},
        {"timeout": 0},
        {"timeout": 301},
        {"timeout": True},
    ],
)
def test_probe_requests_are_bounded_and_typed(change):
    with pytest.raises(ValueError):
        Probe(**{"program": "main.py", "line": 3, **change})


class FakeWorker:
    """Scripted worker: one stop with a given reason/location, then termination."""

    def __init__(
        self, reason="breakpoint", path="/w/main.py", line=8, update=None, crash=None, seen=None
    ):
        self.reason, self.path, self.line, self.update = reason, path, line, update
        self.crash, self.seen = crash, seen
        self.calls = []
        self.stopped = False

    def dispatch(self, operation, args):
        self.calls.append(operation)
        if operation == "launch":
            return {
                "session_id": "s",
                "breakpoints": [{"id": 1, "verified": True, "line": 8}],
                "artifact_manifest_id": None,
                "target_identity": None,
                "source_sha256": "x",
            }
        if operation == "wait":
            if self.stopped:
                return {"state": "terminated"}
            self.stopped = True
            result = {
                "state": "stopped",
                "stop_id": "t",
                "event": {"body": {"reason": self.reason}},
            }
            if self.update:
                result["breakpoint_events"] = [{"breakpoint": {"id": 1, "line": self.update}}]
            if self.crash is not None:
                result["crash"] = self.crash
            if self.seen is not None:
                result["signal_stop"] = self.seen
            return result
        if operation == "inspect":
            frame = {"source": {"path": self.path}, "line": self.line}
            return {
                "frames": [frame],
                "scopes": [
                    {
                        "name": "Locals",
                        "reference": 1,
                        "variables": [{"name": "x", "value": "1", "variablesReference": 0}],
                    }
                ],
            }
        if operation == "report":
            return {"output": "", "output_truncated": False}
        if operation == "continue":
            return {"state": "running", "resumed": True}
        return {}

    def close(self):
        self.calls.append("close")


@pytest.mark.parametrize(
    "fake, ok",
    [
        ({}, True),
        ({"update": 9, "line": 9}, True),
        ({"reason": "exception"}, False),
        ({"reason": "signal"}, False),
        ({"reason": "exception", "seen": {"kind": "cpp_throw", "description": "C++ Throw"}}, False),
        (
            {"reason": "exception", "seen": {"kind": "signal", "description": "signal SIGINT"}},
            False,
        ),
        ({"reason": "exception", "crash": {}}, False),
        ({"reason": "signal", "crash": {"description": "signal SIGSEGV"}}, False),
        ({"line": 12}, False),
        ({"path": "/w/other.py"}, False),
        ({"path": "/w/notmain.py"}, False),
    ],
)
def test_only_the_requested_breakpoint_counts_as_a_hit(monkeypatch, fake, ok):
    import peeky.probe as probe_module

    worker = FakeWorker(**fake)
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("main.py", 8, ("x",), (("x", "2"),)))
    if ok:
        assert result["status"] == "completed" and result["hits"] == 1
    elif "crash" in fake:
        # The program crashed: nothing of its frames is read, and nothing changed.
        assert result["status"] == "failed" and result["hits"] == 0
        assert result["error"]["code"] == "PROGRAM_CRASHED", result["error"]
        what = fake["crash"].get("description", "the debugger did not say what")
        assert result["error"]["message"] == (
            f"The program crashed before it stopped at main.py:8: {what}. Peeky ended it "
            "there, so it has no exit status"
        )
        assert {"inspect", "set_variable", "continue"}.isdisjoint(worker.calls)
        assert result["rows"] == []
    elif fake.get("reason") in ("exception", "signal"):
        # No proven crash: a signal the program may handle, or a C++
        # throw filter's stop. Nothing is read, and the program goes on to its end.
        assert result["status"] == "no-stop" and result["hits"] == 0, result
        assert "crash" not in result and result["error"]["code"] == "NO_STOP"
        seen = fake.get("seen") or {}
        what = seen.get("description", f"reason: {fake['reason']}")
        # A C++ throw or catch filter's stop is no signal: nothing about delivering one.
        assert result["warnings"][0] == (
            f"The probe let the program go on after it stopped on {what}, which Peeky "
            "doesn't take for a crash."
            + (
                ""
                if seen.get("kind") == "cpp_throw"
                else " The debugger may not deliver a signal to the program, so its run after "
                "that stop can differ from one without Peeky."
            )
        ), result["warnings"]
        assert {"inspect", "set_variable"}.isdisjoint(worker.calls) and result["rows"] == []
        assert "continue" in worker.calls
    else:
        assert result["status"] == "failed" and result["hits"] == 0
        assert "not the requested" in result["error"]["message"]
        assert "set_variable" not in worker.calls and result["rows"] == []
    assert worker.calls[-1] == "close"


def test_a_failed_probe_keeps_the_output_so_far_and_never_says_all(monkeypatch):
    import peeky.probe as probe_module

    class Refusing(FakeWorker):
        def dispatch(self, operation, args):
            if operation == "set_variable":
                raise ValueError("'x' is kept in a register")
            if operation == "report":
                self.calls.append(operation)
                return {"output": "before\n", "output_truncated": False, "state": "stopped"}
            return super().dispatch(operation, args)

    worker = Refusing()
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("main.py", 8, ("x",), (("x", "2"),), hits="all"))
    assert result["status"] == "failed" and result["hits"] == 1
    assert (result["output"], result["output_complete"]) == ("before\n", False)
    text = _table(result)
    assert "hits: 1 before it failed" in text and "(all)" not in text
    assert "output so far (the program was stopped when the probe failed):\n  before" in text
    assert worker.calls[-1] == "close"


class Ending(FakeWorker):
    """A session whose report reads ``output`` (``state``), and whose close leaves
    ``final``: what the DAP read once the session ended."""

    def __init__(self, state, final, output="at the hit\n", **kwargs):
        super().__init__(**kwargs)
        self.state, self.last_final, self.output = state, None, output
        self.final = final

    def dispatch(self, operation, args):
        if operation == "report":
            self.calls.append(operation)
            return {"state": self.state, "output": self.output, "output_truncated": False}
        return super().dispatch(operation, args)

    def close(self):
        super().close()
        self.last_final = self.final


WHOLE = {"ended": True, "drained": True, "output_truncated": False, "exit_code": 0}
WHOLE["output"] = "at the hit\nafter it\n"
CUT = {**WHOLE, "output_truncated": True}
# The debugger reported the end, but the adapter kept its stream open: output after
# `terminated` may still have been on its way, and so may a late `exited`.
OPEN = {**WHOLE, "drained": False, "exit_code": None}
# Text after Peeky's disconnect (lldb-dap 18's crash on Linux): kept apart, not as output.
NOISY = {**OPEN, "after_disconnect": "free(): invalid pointer\n"}
# Its stream ended cleanly, but the adapter then failed.
CRASHED = {**OPEN, "stream_drained": True, "adapter_exit": 2}
KILLED = {**OPEN, "stream_drained": True, "adapter_exit": -6}
# A status 1 from debugpy without the signature of the exit Peeky provokes: a crash.
UNEXPLAINED = {**OPEN, "stream_drained": True, "adapter_exit": 1, "exit_provoked": False}
# With it: Peeky's own doing.
PROVOKED = {**WHOLE, "stream_drained": True, "adapter_exit": 1, "exit_provoked": True}
RAN_ON = "output so far (the program ran on after the last hit, so this can include text "


@pytest.mark.parametrize(
    "final, complete, label",
    [
        (None, False, RAN_ON + "written after it):\n  at the hit"),
        ({**WHOLE, "ended": False}, False, RAN_ON + "written after it):\n  at the hit"),
        (WHOLE, True, "output (all of it: the program ran on after the last hit, to its exit):"),
        # Both flags: never "all of it".
        (
            CUT,
            True,
            "output through the program's exit (it ran on after the last hit; truncated: some "
            "of it was dropped):",
        ),
        (
            OPEN,
            False,
            "output so far (the program ran on after the last hit and ended, but its output may "
            "still have been arriving):\n  at the hit\n  after it",
        ),
    ],
)
def test_until_hits_says_the_output_can_follow_the_last_hit(monkeypatch, final, complete, label):
    # Resumed after its last hit, the program runs on while its output is read; it
    # was "the output so far (the program was still running)", even once it had exited.
    import peeky.probe as probe_module

    worker = Ending("running", final)
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("main.py", 8, ("x",), until="hits"))
    assert result["status"] == "completed" and result["ended"] == "hits"
    assert result["output_after_last_hit"] is True and result["output_complete"] is complete
    ended = bool(final and final["ended"])
    assert result["output"] == (final["output"] if ended else "at the hit\n")
    assert result["output_truncated"] is (final is CUT)
    assert result.get("exit_code") == (final["exit_code"] if ended else None)
    assert result.get("program_ended", False) is (final is OPEN)
    text = _table(result)
    assert "still running" not in text and label in text
    assert ("all of it" in text) is (final is WHOLE)


@pytest.mark.parametrize(
    "state, final, complete, label",
    [
        ("terminated", WHOLE, True, "output:\n  at the hit\n  after it"),
        ("terminated", CUT, True, "output (truncated: some of it was dropped):\n  at the hit"),
        (
            "terminated",
            OPEN,
            False,
            "output so far (the program ended, but its output may still have been arriving):",
        ),
        # Peeky inferred the end: java-debug never reported it.
        (
            "terminated",
            None,
            False,
            "output so far (the program ended, but its output may still have been arriving):",
        ),
        (
            "terminated",
            NOISY,
            False,
            "arriving):\n  at the hit\n  after it\nthe debugger sent more text once Peeky "
            "began closing the session, its own or the program's still on its way "
            "(output_after_disconnect in --json)",
        ),
    ],
)
def test_output_is_whole_only_past_the_adapters_end(monkeypatch, state, final, complete, label):
    import peeky.probe as probe_module

    worker = Ending(state, final)
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("main.py", 8, ("x",)))
    assert (result["ended"], result["output_complete"]) == ("exit", complete)
    assert result.get("program_ended", False) is not complete
    assert "output_after_last_hit" not in result and label in _table(result)
    assert result.get("output_after_disconnect") == (final or {}).get("after_disconnect")
    assert "free()" not in result["output"]


@pytest.mark.parametrize("after", [True, False])
def test_truncated_output_is_never_called_all_of_it(after):
    # Complete (the program's end was read) but truncated is not all of it.

    result = {"status": "completed", "hits": 1, "rows": [], "interventions": [], "seconds": 1}
    result.update(output="tail\n", output_complete=True, output_truncated=True)
    if after:
        result["output_after_last_hit"] = True
    text = _table(result)
    assert "all of it" not in text and "truncated: some of it was dropped" in text
    assert ("output through the program's exit (it ran on after the last hit;" in text) is after


@pytest.mark.parametrize(
    "final, how",
    [
        (CRASHED, "exited with status 2"),
        (KILLED, "was ended by signal 6"),
        (UNEXPLAINED, "exited with status 1"),
    ],
)
def test_an_adapter_that_failed_as_it_exited_leaves_the_output_partial(monkeypatch, final, how):
    import peeky.probe as probe_module

    worker = Ending("terminated", final)
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("main.py", 8, ("x",)))
    assert (result["output_complete"], result["program_ended"]) == (False, True)
    assert result["adapter_exit"] == final["adapter_exit"]
    warning = f"The debugger's adapter {how} after the program ended, so output it had not sent"
    assert any(w.startswith(warning) for w in result["warnings"]), result["warnings"]
    text = _table(result)
    assert "the program ended, but its output may still have been arriving" in text
    assert "warning: " + warning in text


def test_the_exit_peeky_provokes_is_said_in_json_and_never_warned(monkeypatch):
    import peeky.probe as probe_module

    worker = Ending("terminated", PROVOKED)
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("main.py", 8, ("x",)))
    assert (result["output_complete"], result["exit_provoked"]) == (True, True)
    assert not result["warnings"] and "adapter_exit" not in result
    assert "program_ended" not in result
    text = _table(result)
    assert "output:\n  at the hit\n  after it" in text and "warning" not in text


def test_only_the_last_twenty_lines_of_output_are_shown_and_it_says_so():

    output = "".join(f"line {n}\n" for n in range(1, 26))
    result = {"status": "completed", "hits": 1, "rows": [], "interventions": [], "seconds": 1}
    result["output"] = output
    text = _table({**result, "output_complete": True})
    assert "output:\n  (5 earlier lines: see --json)\n  line 6\n" in text and "line 5\n" not in text


def test_a_closed_session_keeps_what_its_end_left(tmp_path):
    from peeky.worker import DebugWorker

    class Closing:
        final = {**WHOLE}

        def close(self, terminate=True):
            return "nothing Peeky could identify was still running"

    worker = DebugWorker(tmp_path)
    worker.dap = Closing()
    worker.close()
    assert worker.last_final == WHOLE
    worker.close()  # nothing left to close: what the end left stays
    assert worker.last_final == WHOLE


@pytest.mark.parametrize("selector", ["items[-1]", "a.b[-12]", "x[0][-1]"])
def test_negative_index_selectors_parse(selector):
    from peeky.probe import parse_selector

    assert parse_selector(selector)[-1].startswith("[")


@pytest.mark.parametrize("selector", ["items[--1]", "items[-]", "items[1-]", "[-1]"])
def test_malformed_negative_selectors_are_rejected(selector):
    from peeky.probe import parse_selector

    with pytest.raises(ValueError):
        parse_selector(selector)


@pytest.mark.parametrize(
    "variable, children, length",
    [
        ({"type": "list", "value": "[1, 2]"}, [{"name": "len()", "value": "2"}], 2),
        ({"type": "tuple", "value": "(1,)"}, [{"name": "len()", "value": "1"}], 1),
        ({"type": "dict", "value": "{0: 1}"}, [{"name": "len()", "value": "1"}], None),
        ({"type": "MyList", "value": "[1]"}, [{"name": "len()", "value": "1"}], None),
        ({"type": "list", "value": "[1]"}, [], None),
        ({"value": "", "type": "int[4]"}, [], 4),
        ({"value": "", "type": "unsigned char[16]"}, [], 16),
        ({"value": "", "type": "int[4][3]"}, [], None),
        ({"value": "int[5]@123", "type": "int[]"}, [], 5),
        # LLDB summaries and DAP counts can equal a capped child count: never trusted.
        ({"value": "size=24", "type": "std::forward_list<int>"}, [], None),
        ({"value": "size=3", "type": "std::vector<int>", "indexedVariables": 3}, [], None),
        ({"value": "{...}", "indexedVariables": 6}, [], None),
        ({"value": "[1, 2]", "type": "str"}, [], None),
    ],
)
def test_declared_lengths_must_be_independent_of_the_listing(variable, children, length):
    from peeky.probe import _declared_length

    assert _declared_length(variable, children) == length


class Pages:
    def __init__(self, names, value):
        self.names, self.value = names, value

    def dispatch(self, operation, args):
        children = [{"name": name, "value": name} for name in self.names]
        return {"variables": children, "truncated": False}


@pytest.mark.parametrize(
    "names, value, outcome",
    [
        (["[0]", "[1]", "[2]"], "int[3]", "[2]"),
        (["[0]", "[1]"], "int[3]", "only 2 of 3"),
        (["[0]", "[1]"], "size=2", "independent length"),
        (["[0]", "[2]"], "int[2]", "not an indexed sequence"),
    ],
)
def test_negative_indexes_never_guess_the_end(names, value, outcome):
    from peeky.probe import _from_end

    variable = {"variablesReference": 1, "type": value}
    if outcome.startswith("["):
        assert _from_end(Pages(names, value), {}, variable, 1, "[-1]")["name"] == outcome
    else:
        with pytest.raises(ValueError, match=outcome):
            _from_end(Pages(names, value), {}, variable, 1, "[-1]")


class Tree:
    """A scripted worker whose handles expand to fixed children."""

    def __init__(self, tree):
        self.tree = tree

    def dispatch(self, operation, args):
        assert operation == "expand"
        return {"variables": self.tree[args["handle"]], "truncated": False}


def snapshot(*variables):
    return {"scopes": [{"name": "Locals", "variables": list(variables)}]}


ORDER = {"name": "order", "type": "dict", "value": "{...}", "variablesReference": 1}
# debugpy marks entries whose key's repr evaluates back to the key (the worker passes
# that on as `evaluable`, never the expression itself).
ORDER_CHILDREN = [
    {"name": "special variables", "value": "", "variablesReference": 9},
    {"name": "'id'", "type": "int", "value": "18", "variablesReference": 0, "evaluable": True},
    {"name": '"it\'s"', "type": "int", "value": "5", "variablesReference": 0, "evaluable": True},
    {"name": "3", "type": "str", "value": "'three'", "variablesReference": 0, "evaluable": True},
    {"name": "len()", "type": "int", "value": "3", "variablesReference": 0},
]


@pytest.mark.parametrize(
    "selector, value",
    [
        ("order['id']", "18"),
        ('order["id"]', "18"),
        ('order["it\'s"]', "5"),
        ("order[3]", "'three'"),
    ],
)
def test_mapping_entries_are_read_by_key(selector, value):
    from peeky.probe import _resolve

    found = _resolve(Tree({1: ORDER_CHILDREN}), {}, snapshot(ORDER), parse_selector(selector))
    assert found["value"] == value


@pytest.mark.parametrize(
    "selector, message",
    [
        # An attribute path on a mapping names the key syntax that works.
        ("order.id", "select it with ['id'], not .id"),
        ("order['nope']", "not found in 'order' (dict); children: 'id', \"it's\", 3, len()"),
        ("order[7]", "not found in 'order' (dict)"),
    ],
)
def test_missing_entries_list_what_is_there(selector, message):
    from peeky.probe import _resolve

    with pytest.raises(ValueError) as caught:
        _resolve(Tree({1: ORDER_CHILDREN}), {}, snapshot(ORDER), parse_selector(selector))
    assert message in str(caught.value)
    assert "special variables" not in str(caught.value)


class PagedTree(Tree):
    """Children served 64 per page, as the worker does."""

    def dispatch(self, operation, args):
        children = self.tree[args["handle"]]
        start = args.get("start", 0)
        return {"variables": children[start : start + 64], "truncated": start + 64 < len(children)}


def test_a_key_shown_by_an_impostor_object_is_refused():
    from peeky.probe import _resolve

    filler = [
        {"name": f"'k{i}'", "type": "int", "value": str(i), "evaluable": True} for i in range(100)
    ]
    unique = [*ORDER_CHILDREN, *filler]
    found = _resolve(PagedTree({1: unique}), {}, snapshot(ORDER), parse_selector("order['k99']"))
    assert found["value"] == "99"
    # An object whose __repr__ returns "'a'" is listed first under the plain name; the
    # real "a" follows as "'a' (id: N)". Neither may be read as order['a'].
    impostor = [{"name": "'a'", "type": "str", "value": "'weird'"}, *filler]
    impostor.append({"name": "'a' (id: 4336031232)", "value": "'real'", "evaluable": True})
    with pytest.raises(ValueError, match="ambiguous"):
        _resolve(PagedTree({1: impostor}), {}, snapshot(ORDER), parse_selector("order['a']"))
    # The real key first: it is the plain-named, evaluable entry, read at once without
    # scanning the rest of the mapping.
    real_first = [{"name": "'a'", "value": "'real'", "evaluable": True}, *filler]
    found = _resolve(PagedTree({1: real_first}), {}, snapshot(ORDER), parse_selector("order['a']"))
    assert found["value"] == "'real'"


@pytest.mark.parametrize(
    "listing",
    [
        # debugpy 1.8 lists 500 entries, then this marker (pydevd_resolver.TOO_LARGE_ATTR).
        [ORDER_CHILDREN[1], {"name": "Unable to handle:", "value": "Maximum number of items"}],
        # Fewer entries than the mapping's own len(), whatever the marker.
        [ORDER_CHILDREN[1], {"name": "len()", "type": "int", "value": "505"}],
    ],
)
def test_keys_missing_from_a_partial_mapping_are_not_called_absent(listing):
    from peeky.probe import _resolve

    with pytest.raises(ValueError, match="only part of a large mapping"):
        _resolve(Tree({1: listing}), {}, snapshot(ORDER), parse_selector("order['zzz']"))


def test_each_rust_note_names_every_gate_on_what_peeky_reads():
    # A shorter note must still name each condition Peeky
    # reads under (MIR-proven, at the stop line, innermost frame, unoptimized, live, no
    # unsafe code, a raw pointer never), so no row reads as vouched for where it is not.
    from peeky import probe

    gates = {
        "_OWNED_RUST": ("the MIR proves it initialized at the stop line", "innermost frame"),
        "_FIELD_RUST": (
            "a reference parameter the IR proves live",
            "the MIR proves initialized at the stop line",
            "only in the innermost frame",
        ),
        "_COMPOSITE_RUST": (
            "integer, bool, float, char and String fields",
            "the MIR proves it initialized at this stop, in the innermost frame",
        ),
        # A slice or `&Vec` is never told to be read as one.
        "_LENT_COMPOSITE_RUST": (
            "a parameter the build's IR proves live",
            "only in the innermost frame",
            "integer, bool, float, char and String fields",
        ),
        "_UNPROVEN_RUST": (
            "never follows a raw pointer",
            "a value the MIR proves live",
            "in the innermost frame of an unoptimized build",
            "no unsafe code",
            "can reach freed memory",
        ),
        "_STALE_RUST": (
            "The MIR does not prove it initialized here",
            "Peeky has no proof here: an optimized build, a caller frame, or unmatched MIR",
            "moved-from copy",
        ),
        "_UNREAD_RUST": (
            "structs your crate defines (their integer, bool, float, char and String",
        ),
    }
    for name, needed in gates.items():
        note = getattr(probe, name)
        for gate in needed:
            assert gate in note, (name, gate)


def test_a_raw_pointers_note_says_only_that_peeky_never_follows_one():
    # A raw pointer local got a reference's note ("This Rust reference shows
    # only its pointer: the build's MIR does not show that what it points at still
    # stands…"), and one in a live struct the generic one ("Here what it points at may be
    # moved or dropped, or the MIR cannot say"): both false for it.
    from peeky.probe import (
        _UNPROVEN_RUST,
        _held_unproven_note,
        _resolve,
        _rust_note_kind,
        _unproven_note,
    )

    raw = {
        "name": "p",
        "type": "long *",
        "value": "0x10",
        "view": "unproven",
        "variablesReference": 0,
        "unproven_pointer": "raw",
    }
    maybe = {**raw, "unproven_pointer": "unknown"}
    # A raw pointer's own words come first, whatever reason a reference's gate gave.
    note = _unproven_note({**raw, "unproven_reason": "the build's MIR does not show it"})
    assert note == (
        "This Rust raw pointer shows only its address: Peeky never follows a raw pointer, "
        "because nothing proves that what it points at still stands."
    )
    assert _unproven_note(maybe) == (
        "This Rust pointer shows only its address: Peeky never follows a raw pointer, and "
        "the build's debug information does not say whether this is one."
    )
    held = _held_unproven_note([raw, raw])
    assert held == (
        "Its Rust raw pointers show only their address: Peeky never follows a raw pointer, "
        "because nothing proves that what one points at still stands."
    )
    held_maybe = _held_unproven_note([raw, maybe])
    assert held_maybe.startswith("Its Rust pointers show only their address: Peeky never")
    for text in (note, _unproven_note(maybe), held, held_maybe):
        assert "reference" not in text and "Box" not in text, text
        for claim in ("moved", "dropped", "MIR", "freed", "may be", "Here"):
            assert claim not in text, (claim, text)
        # A limit of Peeky's, said once among a probe's warnings, not a doubt.
        assert _rust_note_kind(text), text
    # Beside another pointer shown only as its address, every way that can be.
    generic = _UNPROVEN_RUST[0].upper() + _UNPROVEN_RUST[1:] + "."
    assert _held_unproven_note([raw, {"view": "unproven"}]) == generic
    # Selecting through one is refused in the same words.
    with pytest.raises(ValueError) as caught:
        _resolve(Tree({}), {}, snapshot(raw), parse_selector("p.x"))
    assert str(caught.value) == (
        "cannot expand 'p': this Rust raw pointer shows only its address: Peeky never "
        "follows a raw pointer, because nothing proves that what it points at still stands"
    )


def test_a_withheld_rust_pointers_note_matches_what_its_row_shows():
    # A `NonNull` local's row was `<not shown: …>`, but its note said it "shows only its
    # pointer"; so did a tuple's, an `Option`'s or a `&str`'s past its proof. A NonNull
    # now shows its address, as a raw pointer does; what shows no address says so.
    from peeky.probe import _resolve, _rust_note_kind, _unproven_note

    unshown = "<not shown: may point at freed memory>"
    non_null = {
        "name": "n",
        "type": "core::ptr::non_null::NonNull<app::Pt>",
        "value": "0x10",
        "view": "unproven",
        "variablesReference": 0,
        "unproven_pointer": "raw",
    }
    shown = _unproven_note(non_null)
    assert shown == (
        "This Rust NonNull shows only its address: a NonNull is a raw pointer that is never "
        "null, and Peeky never follows a raw pointer, because nothing proves that what it "
        "points at still stands."
    )
    wide = "this one is a wide pointer, its address with a length or vtable (to a slice"
    hidden = _unproven_note({**non_null, "value": unshown, "unproven_reason": wide})
    assert hidden.startswith(
        "This Rust NonNull is not shown: Peeky shows a NonNull, a raw pointer that is never "
        f"null, only as one address, and {wide}. To see its address, capture a thin pointer"
    ), hidden
    tuple_ = {
        "name": "t",
        "type": "(i64, &app::Pt)",
        "value": "<not shown: its type can hold a pointer>",
        "view": "unproven",
    }
    held = _unproven_note(tuple_)
    assert held == (
        "This Rust value is not shown: its type can hold a pointer, and Peeky can't prove that "
        "what it would point at still stands. Read a plain function's own local where it is "
        "live, or capture what it holds where it is created. Peeky never follows a raw "
        "pointer, and follows another only to a value the MIR proves live, in the innermost "
        "frame of an unoptimized build, in a crate with no unsafe code (rust-cookbook 7.2)."
    ), held
    # Said of its type, not its value, which is not read: an `Option<&T>` may be `None`.
    option = {**tuple_, "name": "o", "type": "core::option::Option<&app::Pt>"}
    assert _unproven_note(option) == held
    for claim in ("a pointer in it", "what it points at", "moved or dropped", "freed memory"):
        assert claim not in held, claim
    why = "the build's MIR does not show that what it points at still stands here"
    text = {"name": "s", "type": "&str", "value": unshown, "view": "unproven"}
    lent = _unproven_note({**text, "unproven_reason": why})
    assert lent == f"This Rust reference is not shown: {why}.", lent
    for note in (shown, hidden, held, lent):
        assert _rust_note_kind(note), note
    assert "only its address" not in hidden and "only its pointer" not in held + lent
    # A row that does show an address keeps its words.
    assert _unproven_note({**text, "value": "0x10", "unproven_reason": why}).startswith(
        "This Rust reference shows only its pointer: "
    )
    # Selecting through the NonNull is refused in its row's words: `n` is the address.
    with pytest.raises(ValueError) as caught:
        _resolve(Tree({}), {}, snapshot(non_null), parse_selector("n.pointer"))
    assert str(caught.value) == "cannot expand 'n': " + shown[0].lower() + shown[1:-1]


def test_rust_container_indexes_explain_what_is_unsupported():
    from peeky.probe import _resolve

    vec = {
        "name": "ordered",
        "type": "alloc::vec::Vec<int, alloc::alloc::Global>",
        "value": "",
        "variablesReference": 1,
    }
    fields = [
        {"name": "buf", "type": "RawVec", "value": "", "variablesReference": 2},
        {"name": "len", "type": "unsigned long", "value": "3", "variablesReference": 0},
    ]
    with pytest.raises(ValueError) as caught:
        _resolve(Tree({1: fields}), {}, snapshot(vec), parse_selector("ordered[0]"))
    message = str(caught.value)
    assert "has no indexed elements" in message and "children: buf, len" in message
    assert "Peeky reads Rust contents only for reference parameters" in message
    assert "MIR proves initialized" in message and "This one stays raw" in message
    assert "at the stop line (innermost frame)" in message and "readMemory" in message
    assert "structs your crate defines" in message, message
    assert "capture ordered.len" in message
    # Text read from memory has only its length as children: no element hint.
    text = {
        "name": "label",
        "type": "&str",
        "value": '"ab"',
        "view": "memory",
        "length": 2,
        "variablesReference": 1,
    }
    length = [{"name": "length", "type": "usize", "value": "2", "variablesReference": 0}]
    with pytest.raises(ValueError) as caught:
        _resolve(Tree({1: length}), {}, snapshot(text), parse_selector("label[0]"))
    message = str(caught.value)
    assert "shown whole, not by element" in message and "reference parameters" not in message
    # So is an owned String's, and an element's String field's, never "beyond the
    # debugger's visible window" of as many elements as it has bytes.
    sizes = [{"name": "len", "type": "usize", "value": "2", "variablesReference": 0}]
    for kind in ("alloc::string::String", "String"):
        owned = {**text, "name": "name", "type": kind, "length": 2}
        with pytest.raises(ValueError) as caught:
            _resolve(Tree({1: sizes}), {}, snapshot(owned), parse_selector("name[0]"))
        message = str(caught.value)
        assert "shown whole, not by element" in message and "window" not in message, message


def test_a_container_a_call_may_be_changing_offers_no_field_as_its_length():
    # A caller's `&mut Vec<T>` (`friends.retain_mut(decide)` under way)
    # was told "Capture friends.len for the length", a length the call may be changing.
    from peeky.probe import _raw_rust, _resolve

    reason = (
        "`friends` is a `&mut` parameter of a caller frame: Peeky doesn't check whether it "
        "was lent to the call under way, which may be changing what it points at, so its "
        "fields aren't trusted here; Peeky reads it in the innermost frame"
    )
    friends = {
        "name": "friends",
        "type": "alloc::vec::Vec<kitty::Friend, alloc::alloc::Global> *",
        "value": "0x1",
        "variablesReference": 1,
        "raw_reason": reason,
        "stale": True,
    }
    fields = [
        {"name": "buf", "type": "RawVec", "value": "", "variablesReference": 2},
        {"name": "len", "type": "unsigned long", "value": "3", "variablesReference": 0},
    ]
    note = _raw_rust(friends, "friends")
    assert reason in note and "Capture" not in note, note
    with pytest.raises(ValueError) as caught:
        _resolve(Tree({1: fields}), {}, snapshot(friends), parse_selector("friends[0]"))
    assert reason in str(caught.value) and "friends.len" not in str(caught.value)
    # A `Vec<String>` (elements never read) a call may be changing says the same.
    strings = {**friends, "type": "alloc::vec::Vec<alloc::string::String, alloc::alloc::Global> *"}
    with pytest.raises(ValueError) as caught:
        _resolve(Tree({1: fields}), {}, snapshot(strings), parse_selector("friends[0]"))
    assert reason in str(caught.value) and "friends.len" not in str(caught.value)
    # Any other reason still names the length.
    other = {**friends, "raw_reason": "the debugger gives no memory address for it"}
    assert "Capture friends.len for the length" in _raw_rust(other, "friends")


def test_a_slice_of_structs_left_raw_is_never_told_to_be_read_as_a_slice():
    # `friends: &mut [Friend]`, a live parameter, was told "read it as a &Vec or
    # &[T] parameter", which it was; an owned Vec may still be read where it is passed.
    from peeky.probe import _composite_note, _resolve

    fields = [
        {"name": "data_ptr", "type": "kitty::Friend *", "value": "0x1", "variablesReference": 2},
        {"name": "length", "type": "unsigned long", "value": "4", "variablesReference": 0},
    ]
    for kind in ("&mut [kitty::Friend]", "alloc::vec::Vec<kitty::Friend, alloc::alloc::Global> *"):
        friends = {"name": "friends", "type": kind, "value": "", "variablesReference": 1}
        with pytest.raises(ValueError) as caught:
            _resolve(Tree({1: fields}), {}, snapshot(friends), parse_selector("friends[0]"))
        for text in (str(caught.value), _composite_note(friends, "friends")):
            assert "as a &Vec or &[T] parameter" not in text and "passed as" not in text, text
            assert "a parameter the build's IR proves live" in text, text
            assert "innermost frame" in text, text
    owned = {
        "name": "friends",
        "type": "alloc::vec::Vec<kitty::Friend, alloc::alloc::Global>",
        "value": "",
        "variablesReference": 1,
    }
    assert "passed as &Vec or &[T]" in _composite_note(owned, "friends")
    # The worker's reason, where it gives one, comes first (an element type not read).
    raw = {**owned, "type": "&[kitty::Shape]", "raw_reason": "Peeky does not read the elements"}
    with pytest.raises(ValueError) as caught:
        _resolve(Tree({1: fields}), {}, snapshot(raw), parse_selector("friends[0]"))
    assert "Peeky does not read the elements" in str(caught.value)


def test_a_raw_rust_field_s_index_says_why_and_names_its_whole_selector():
    # `self.values[0]` was "not found ... Capture values.len",
    # with the owned local's reason for a field reached through `&mut self`.
    from peeky.probe import _raw_rust, _resolve

    reason = "`self` is a `&mut` parameter of a caller frame"
    values = {
        "name": "values",
        "type": "alloc::vec::Vec<long, alloc::alloc::Global>",
        "value": "alloc::vec::Vec<long, alloc::alloc::Global> @ 0x10",
        "variablesReference": 2,
        "raw_reason": reason,
    }
    fields = [
        {"name": "buf", "type": "RawVec", "value": "", "variablesReference": 3},
        {"name": "len", "type": "unsigned long", "value": "3", "variablesReference": 0},
    ]
    root = {"name": "self", "type": "Window *", "value": "0x10", "variablesReference": 1}
    tree = Tree({1: [values], 2: fields})
    with pytest.raises(ValueError) as caught:
        _resolve(tree, {}, snapshot(root), parse_selector("self.values[0]"))
    message = str(caught.value)
    assert reason in message and "Capture self.values.len for the length" in message, message
    assert "MIR proves initialized" not in message, message
    note = _raw_rust(values, "self.values")
    assert note.startswith("This Rust Vec shows LLDB's summary (its type and address)"), note
    assert reason in note and "Capture self.values.len" in note, note
    text = {**values, "type": "alloc::string::String", "name": "name"}
    assert "Capture self.name.vec.len" in _raw_rust(text, "self.name")


def test_depth_limited_values_say_so():
    from peeky.probe import _resolve

    deep = {"name": "ptr", "value": "Unique<u8>", "variablesReference": 0, "depth_limited": True}
    root = {"name": "v", "value": "", "variablesReference": 1}
    with pytest.raises(ValueError, match="inspection depth limit"):
        _resolve(Tree({1: [deep]}), {}, snapshot(root), parse_selector("v.ptr.pointer"))


@pytest.mark.parametrize(
    "value, message",
    [
        ("null", r"^'current' is null: it has no members to select$"),
        ("None", r"^'current' is None: it has no members to select$"),
        ("10", r"^cannot expand 'current' \(leaf; value 10\)$"),
    ],
)
def test_a_leaf_or_null_selector_says_what_it_is(value, message):
    from peeky.probe import _resolve

    leaf = {"name": "current", "value": value, "variablesReference": 0}
    with pytest.raises(ValueError, match=message):
        _resolve(Tree({}), {}, snapshot(leaf), parse_selector("current.end"))


def test_a_view_that_cannot_expand_names_the_view_not_the_value():
    from peeky.probe import _resolve

    view = {"name": "o", "value": "None", "variablesReference": 0, "view": "active-variant"}
    with pytest.raises(ValueError, match=r"^cannot expand 'o' \(active-variant\)$"):
        _resolve(Tree({}), {}, snapshot(view), parse_selector("o.x"))


@pytest.mark.parametrize(
    "name, selector",
    [
        ("'id'", "order['id']"),
        ('"it\'s"', 'order["it\'s"]'),
        ("'\\\\d'", "order['\\\\d']"),
        ("3", "order[3]"),
        ("[4]", "order[4]"),
        ("field", "order.field"),
        ("len()", None),
        ("(1, 2)", None),
        ("'\\d'", None),
        ("b'x'", None),
    ],
)
def test_children_are_offered_with_the_selector_that_reads_them(name, selector):
    from peeky.probe import _child_selector

    assert _child_selector("order", name) == selector
    if selector:
        assert parse_selector(selector)


def test_children_skip_debugger_groups_and_mark_expandable_entries():
    from peeky.probe import _capture

    class Worker(Tree):
        def dispatch(self, operation, args):
            if operation == "inspect":
                return {**snapshot(ORDER), "frames": [{"line": 3, "name": "f"}]}
            return super().dispatch(operation, args)

    nested = [
        *ORDER_CHILDREN,
        {
            "name": "'sub'",
            "type": "dict",
            "value": "{}",
            "variablesReference": 4,
            "evaluable": True,
        },
    ]
    [row] = _capture(Worker({1: nested}), {}, ("order",), 0, "observed", children=True)
    assert [child.get("selector") for child in row["children"]] == [
        "order['id']",
        'order["it\'s"]',
        "order[3]",
        None,
        "order['sub']",
    ]
    assert row["children"][-1]["expandable"] is True
    [row] = _capture(Worker({1: nested}), {}, ("order",), 0, "observed")
    assert "children" not in row


@pytest.mark.parametrize(
    "text, selectors",
    [
        ("a,b", ("a", "b")),
        (" a , ,b ", ("a", "b")),
        ("a,d['x,y'],c", ("a", "d['x,y']", "c")),
        ('d["a,\\"b"],e', ('d["a,\\"b"]', "e")),
        ("x['it\\'s,']", ("x['it\\'s,']",)),
    ],
)
def test_capture_lists_split_on_commas_outside_quoted_keys(text, selectors):
    from peeky.probe import split_selectors

    assert split_selectors(text) == selectors
    assert all(parse_selector(selector) for selector in selectors)


@pytest.mark.parametrize(
    "text, ok",
    [
        ("d['\\x41']", True),
        ("d['\\N{BULLET}']", True),
        ("d['\\u00e9\\t\\\\']", True),
        ("d['\\377']", True),
        ("d['\\d']", False),
        ("d['\\777']", False),
        ("d['\\8']", False),
        ("d['\\N{NO SUCH CHARACTER NAME}']", False),
    ],
)
def test_key_escapes_are_validated_without_touching_warning_filters(text, ok):
    import warnings

    before = list(warnings.filters)
    if ok:
        assert parse_selector(text)
    else:
        with pytest.raises(ValueError):
            parse_selector(text)
    assert warnings.filters == before


def test_unreadable_children_keep_their_reason():
    from peeky.cli import _get_lines
    from peeky.probe import _children

    children = [
        {
            "name": "t",
            "type": "int",
            "value": None,
            "unavailable": True,
            "reason": "error: variable not available",
        }
    ]
    entry = _children(Tree({1: children}), {}, "s", {"variablesReference": 1})["children"][0]
    assert entry["unavailable"] and entry["reason"] == "error: variable not available"
    [_, line] = _get_lines(
        {
            "selector": "s",
            "type": "S",
            "value": "{...}",
            **_children(Tree({1: children}), {}, "s", {"variablesReference": 1}),
        }
    )
    assert "<no readable value: error: variable not available>" in line


def test_session_get_text_shows_why_for_a_missing_name_and_how_to_expand_a_child():
    from peeky.cli import _get_lines

    assert _get_lines({"selector": "x", "error": "not found; visible: a, b"}) == [
        "x: <not found; visible: a, b>"
    ]
    lines = _get_lines(
        {
            "selector": "o",
            "type": "Obj",
            "value": "Obj@1",
            "children": [
                {
                    "name": "inner",
                    "selector": "o.inner",
                    "type": "In",
                    "value": "In@2",
                    "expandable": True,
                },
                {"name": "n", "selector": "o.n", "type": "int", "value": "3"},
                {
                    "name": "[0]",
                    "selector": "o[0]",
                    "type": "int",
                    "value": "4",
                    "expandable": True,
                },
            ],
        }
    )
    assert lines == [
        "o: Obj = Obj@1",
        "  o.inner: In = In@2 (children: peeky session get o.inner)",
        "  o.n: int = 3",
        "  o[0]: int = 4 (children: peeky session get 'o[0]')",
    ]


def test_child_listings_are_retried_on_a_fresh_inspection():
    from peeky.probe import _capture

    class Starved(Tree):
        expansions = 0

        def dispatch(self, operation, args):
            if operation == "inspect":
                return {**snapshot(ORDER), "frames": [{"line": 3, "name": "f"}]}
            Starved.expansions += 1
            if Starved.expansions == 1:
                raise ValueError("Inspection node budget exhausted; inspect again")
            return super().dispatch(operation, args)

    [row] = _capture(Starved({1: ORDER_CHILDREN}), {}, ("order",), 0, "observed", children=True)
    assert "children_error" not in row and row["children"][0]["selector"] == "order['id']"


def test_impostor_entries_get_no_selector_and_are_never_read():
    from peeky.probe import _children, _resolve

    # {K(): 'weird', 'a': 'real-a'} and {N(): 'weird-int', 1: 'real-int'}, where the
    # impostors' __repr__ return "'a'" and "1": debugpy marks only the real keys evaluable.
    entries = [
        {"name": "'a'", "type": "str", "value": "'weird'"},
        {"name": "'a' (id: 4335162880)", "type": "str", "value": "'real-a'", "evaluable": True},
        {"name": "1", "type": "str", "value": "'weird-int'"},
        {"name": "1 (id: 4335162999)", "type": "str", "value": "'real-int'", "evaluable": True},
        {"name": "'b'", "type": "int", "value": "2", "evaluable": True},
    ]
    listed = _children(Tree({1: entries}), {}, "imp", {"variablesReference": 1})["children"]
    assert [entry.get("selector") for entry in listed] == [None, None, None, None, "imp['b']"]
    for selector in ("imp['a']", "imp[1]"):
        with pytest.raises(ValueError, match="ambiguous"):
            _resolve(
                Tree({1: entries}), {}, snapshot({**ORDER, "name": "imp"}), parse_selector(selector)
            )
    # Debuggers that mark nothing evaluable (LLDB, java-debug) still read indexes.
    plain = [
        {"name": "[0]", "type": "int", "value": "7"},
        {"name": "[1]", "type": "int", "value": "8"},
    ]
    found = _resolve(Tree({1: plain}), {}, snapshot({**ORDER, "name": "a"}), parse_selector("a[1]"))
    assert found["value"] == "8"


def test_entries_debugpy_groups_by_value_type_are_explained():
    from peeky.probe import _partial, _resolve

    # {"it's": len, 'plain': len, 2: len}: debugpy files the function values in a group.
    entries = [
        {"name": "function variables", "value": "", "variablesReference": 7},
        {
            "name": "'plain'",
            "type": "builtin_function_or_method",
            "value": "len",
            "evaluable": True,
        },
        {"name": "len()", "type": "int", "value": "3"},
    ]
    assert not _partial(entries)
    for selector in ('fn_vals["it\'s"]', "fn_vals[2]"):
        with pytest.raises(ValueError, match="'function variables', which selectors cannot reach"):
            _resolve(
                Tree({1: entries}),
                {},
                snapshot({**ORDER, "name": "fn_vals"}),
                parse_selector(selector),
            )


def test_hits_take_a_count_or_all():
    import argparse

    from peeky.probe import MAX_HITS, parse_hits

    assert parse_hits("all") == "all" and parse_hits("7") == 7
    with pytest.raises(argparse.ArgumentTypeError):
        parse_hits("ALL")
    assert Probe("main.py", 3, hits="all").limit == MAX_HITS
    assert Probe("main.py", 3, hits="all", set_hit=MAX_HITS).set_hit == MAX_HITS
    for hits in ("All", "10", 0, True, None):
        with pytest.raises(ValueError):
            Probe("main.py", 3, hits=hits)


def test_progress_for_repeated_hits_is_rate_limited(monkeypatch):
    from peeky import probe

    clock = [100.0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])
    # The first and the last requested hit always print.
    assert probe._report_hit(1, 30, last_said=100.0) and probe._report_hit(30, 30, 100.0)
    assert not probe._report_hit(5, 30, last_said=99.5)
    clock[0] = 102.0
    assert probe._report_hit(6, 30, last_said=99.5)


def test_hits_all_fits_the_row_budget_and_needs_the_program_to_exit():
    from peeky.probe import MAX_HITS, MAX_ROWS

    assert Probe("main.py", 3, ("a",) * 1, hits="all").limit == MAX_HITS
    many = tuple(f"s{i}" for i in range(21))
    assert Probe("main.py", 3, many, hits="all").limit == MAX_ROWS // 21
    with_set = Probe("main.py", 3, many, (("s0", "1"),), hits="all")
    assert with_set.limit == (MAX_ROWS - 21) // 21
    with pytest.raises(ValueError, match="until the program exits"):
        Probe("main.py", 3, ("a",), hits="all", until="hits")
    # Repeated selectors add nothing and would multiply comparison rows.
    assert Probe("main.py", 3, ("a", "b", "a")).capture == ("a", "b")


def test_a_bare_member_name_points_to_its_this_selector_never_reads_it():
    from peeky.probe import _resolve

    this = {"name": "this", "type": "Ring *", "value": "0x1", "variablesReference": 1}
    tail = {"name": "tail", "type": "int", "value": "1", "variablesReference": 0}
    members = [
        {"name": "head_", "type": "int", "value": "2", "variablesReference": 0},
        {"name": "tail", "type": "int", "value": "9", "variablesReference": 0},
    ]
    tree, snap = Tree({1: members}), {**snapshot(this, tail), "receiver": "this"}
    # The debugger's list of locals can be incomplete, so a member is never read in
    # place of a missing local: the error names the selector instead.
    for selector in ("head_", "head_.x"):
        with pytest.raises(ValueError) as caught:
            _resolve(tree, {}, snap, parse_selector(selector))
        assert str(caught.value) == (
            "not found; visible: tail, this; this has a member 'head_': select it as "
            "this.head_ if the code means the member"
        )
    assert _resolve(tree, {}, snap, parse_selector("this.head_"))["value"] == "2"
    assert _resolve(tree, {}, snap, parse_selector("tail"))["value"] == "1"
    with pytest.raises(ValueError, match=r"^not found; visible: tail, this$"):
        _resolve(tree, {}, snap, parse_selector("size_"))
    # Without a receiver (C, Python, Rust), `this` is only a local's name.
    with pytest.raises(ValueError, match=r"^not found; visible: tail, this$"):
        _resolve(tree, {}, snapshot(this, tail), parse_selector("head_"))


def test_nameless_debugger_slots_are_not_listed_as_visible():
    from peeky.probe import _visible

    shown = _visible(snapshot({"name": "<null>"}, {"name": "score"}))
    assert shown == "score"


@pytest.mark.parametrize(
    "variable, settable",
    [
        ({"name": "count", "type": "int"}, True),
        ({"name": "ratio", "type": "double"}, True),
        ({"name": "->compute()", "type": "int"}, False),  # java-debug's return value
        ({"name": "\u23af\u25bacompute()", "type": "int"}, False),
        ({"name": "count$", "type": "int"}, False),  # no selector can name it
        ({"name": "items", "type": "int[]"}, False),
        ({"name": "letter", "type": "char"}, False),
    ],
)
def test_java_settable_locals_are_scalars_a_selector_can_name(variable, settable):
    from peeky.java import JavaSession

    assert JavaSession.settable(None, variable) is settable


def _java_fetch(children):
    def fetch(handle, count):
        return children[handle][:count]

    return fetch


@pytest.mark.parametrize(
    "variable, children, shown",
    [
        (
            {"value": "P@1", "type": "P", "variablesReference": 1},
            {1: [{"name": "x", "value": "3"}, {"name": "s", "value": '"hi"'}]},
            'P@1 {x=3, s="hi"}',
        ),
        (
            {"value": "P@1", "type": "P", "variablesReference": 1},
            {1: [{"name": f"f{n}", "value": str(n)} for n in range(10)]},
            "P@1 {f0=0, f1=1, f2=2, f3=3, f4=4, f5=5, f6=6, f7=7, …}",
        ),
        (
            {"value": "int[3]@2", "type": "int[]", "variablesReference": 2},
            {2: [{"name": str(n), "value": str(n * 2)} for n in range(3)]},
            "int[3]@2 [0, 2, 4]",
        ),
        (
            {"value": "ArrayList@3", "type": "ArrayList", "variablesReference": 3},
            {
                3: [
                    {"name": "elementData", "value": "Object[10]@4", "variablesReference": 4},
                    {"name": "modCount", "value": "2", "type": "int"},
                    {"name": "size", "value": "2", "type": "int"},
                ],
                4: [{"name": str(n), "value": f"P@{n}"} for n in range(2)]
                + [{"name": str(n), "value": "null"} for n in range(2, 10)],
            },
            "ArrayList@3 size=2 [P@0, P@1]",
        ),
        ({"value": '"text"', "type": "String", "variablesReference": 5}, {}, None),
        ({"value": "null", "type": "null", "variablesReference": 0}, {}, None),
    ],
)
def test_java_values_preview_fields_lists_and_arrays(variable, children, shown):
    from peeky.java import java_preview

    assert java_preview(variable, _java_fetch(children)) == shown


def test_a_java_preview_that_cannot_show_every_element_says_nothing():
    from peeky.java import java_preview

    array = {"value": "int[3]@2", "type": "int[]", "variablesReference": 2}
    with pytest.raises(ValueError, match="not all visible"):
        java_preview(array, _java_fetch({2: [{"name": "0", "value": "1"}]}))


def test_java_preview_strings_are_literals_so_ids_mask_correctly():
    # java-debug does not escape quotes inside a String, and a cut string
    # lost its closing quote, so masking paired the wrong quotes.
    from peeky.evidence import _no_addresses
    from peeky.java import _brief

    said = _brief({"value": '"he said "x@5" ok"', "type": "String"})
    assert said == '"he said \\"x@5\\" ok"'
    masked = _no_addresses(f"Q$S@55 {{b=Q$B@62, q={said}}}", "java")
    assert masked == f"Q$S@… {{b=Q$B@…, q={said}}}"
    long = _brief({"value": '"' + "x" * 60 + '"', "type": "String"})
    assert long.endswith('…"') and len(long) == 40
    assert _no_addresses(f"Q$R@54 {{a={long}, b=Q$B@59}}", "java").endswith("b=Q$B@…}")
    # Cut before escaping, so an escape is never split.
    assert _brief({"value": '"' + "\\" * 60 + '"', "type": "String"}) == '"' + "\\\\" * 37 + '…"'
    assert _brief({"value": '"a\nb"', "type": "String"}) == '"a\\nb"'


def test_java_preview_chars_are_literals_never_strings():
    # java-debug writes a char bare, so `"` read as an empty String.
    from peeky.evidence import _no_addresses
    from peeky.java import _brief

    chars = [_brief({"value": value, "type": "char"}) for value in ('"', "'", "\\", "\n", "a")]
    assert chars == ["'\"'", "'\\''", "'\\\\'", "'\\n'", "'a'"]
    assert _brief({"value": '""', "type": "String"}) == '""'
    value = f'P@3 {{c={chars[0]}, d={chars[1]}, s="it\'s @5", n=N@6}}'
    assert _no_addresses(value, "java") == value.replace("P@3", "P@…").replace("N@6", "N@…")


@pytest.mark.parametrize("name", ["OrderBookSnapshotValidator$PriceTier", "Q" * 60])
def test_a_cut_java_field_keeps_its_object_number_whole(name):
    # `Long$Name@998` fit in 40 characters and `Long$Name@1003` was cut to
    # `Long$Name@10…`, so the same field differed across runs by more than its number.
    from peeky.evidence import _no_addresses
    from peeky.java import java_preview

    holder = {"value": "Holder@143", "variablesReference": 5, "type": "Holder"}

    def preview(number):
        field = {
            "name": "level",
            "value": f"{name}@{number}",
            "type": name,
            "variablesReference": 7,
        }
        return java_preview(holder, lambda handle, count: [field])

    a, b = preview(998), preview(1003)
    assert a.endswith("@998}") and b.endswith("@1003}")
    assert _no_addresses(a, "java") == _no_addresses(b, "java")
    assert _no_addresses(a, "java").endswith("@…}")


def test_a_field_set_where_only_locals_change_fails_before_the_program_runs(monkeypatch):
    # Only Java changes fields and elements; elsewhere say so at launch, not at the hit.
    import peeky.probe as probe_module

    worker = FakeWorker()
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("main.py", 8, ("x",), (("p.x", "2"),)))
    assert result["status"] == "failed" and result["error"]["code"] == "MUTATION_REJECTED"
    assert "can be changed only in Java" in result["error"]["message"]
    assert "wait" not in worker.calls and "set_variable" not in worker.calls


@pytest.mark.parametrize(
    "selector, value, message",
    [
        ("xs[-1]", "Holder@4", "a negative index can be changed only in a Java array"),
        ("xs[3]", "int[3]@4", "[3] is out of range: the array has 3 elements"),
        ("xs[-4]", "int[3]@4", "[-4] is out of range: the array has 3 elements"),
    ],
)
def test_indexes_that_name_no_array_element_are_refused_before_any_write(selector, value, message):
    from peeky.probe import apply_set

    class Holder(FakeWorker):
        def dispatch(self, operation, args):
            self.calls.append(operation)
            if operation == "inspect":
                variable = {"name": "xs", "value": value, "type": "int[]", "variablesReference": 5}
                return {"frames": [{}], "scopes": [{"reference": 1, "variables": [variable]}]}
            raise AssertionError(operation)

    worker = Holder()
    with pytest.raises(ValueError, match=re.escape(message)):
        apply_set(worker, {}, selector, "1")
    assert worker.calls == ["inspect"]


def test_an_array_element_from_the_end_is_written_by_its_index():
    from peeky.probe import apply_set

    class Holder(FakeWorker):
        def dispatch(self, operation, args):
            self.calls.append((operation, args))
            if operation == "inspect":
                variable = {
                    "name": "xs",
                    "value": "int[3]@4",
                    "type": "int[]",
                    "variablesReference": 5,
                }
                return {"frames": [{}], "scopes": [{"reference": 1, "variables": [variable]}]}
            return {"name": args["name"], "before": "3", "after": "9"}

    worker = Holder()
    changed = apply_set(worker, {"stop_id": "t"}, "xs[-1]", "9")
    assert changed == {"name": "xs[-1]", "before": "3", "after": "9"}
    assert worker.calls[-1] == (
        "set_variable",
        {"stop_id": "t", "handle": 5, "name": "2", "value": "9"},
    )


@pytest.mark.parametrize("names", ["deadline, retries", "breakpoint, provenance"])
def test_a_missing_intervention_parent_is_not_found_whatever_the_names(names):
    # The listed names matched the TIMEOUT and INVALID_BREAKPOINT rules.
    from peeky.errors import classify
    from peeky.probe import apply_set

    class Holder(FakeWorker):
        def dispatch(self, operation, args):
            if operation == "inspect":
                variables = [{"name": name, "value": "1"} for name in names.split(", ")]
                return {"frames": [{}], "scopes": [{"reference": 1, "variables": variables}]}
            raise AssertionError(operation)

    with pytest.raises(ValueError) as caught:
        apply_set(Holder(), {}, "request.limits", "3")
    assert classify(caught.value)["code"] == "NOT_FOUND"
    assert names in str(caught.value)


@pytest.mark.parametrize(
    "selector, code",
    [
        ("this.deadline.x", "MUTATION_REJECTED"),
        ("this.breakpoints[5]", "INVALID_REQUEST"),
        ("this.breakpoints[-9]", "INVALID_REQUEST"),
        ("this.provenance[0]", "NOT_FOUND"),
        ("this.deadline_list[0]", "MUTATION_REJECTED"),
    ],
)
def test_member_refusals_are_classified_by_rule_never_by_the_selectors_words(selector, code):
    # `deadline` made a refusal TIMEOUT, `breakpoint` INVALID_BREAKPOINT.
    from peeky.errors import classify
    from peeky.probe import apply_set

    class Holder(FakeWorker):
        def dispatch(self, operation, args):
            if operation == "inspect":
                this = {"name": "this", "value": "Req@3", "type": "Req", "variablesReference": 5}
                return {"frames": [{}], "scopes": [{"reference": 1, "variables": [this]}]}
            if operation == "expand":
                children = [
                    {"name": "deadline", "value": "5", "type": "long", "variablesReference": 0},
                    {"name": "breakpoints", "value": "int[3]@4", "type": "int[]"},
                    {"name": "provenance", "value": "Holder@6", "type": "Holder"},
                    {"name": "deadline_list", "value": "ArrayList@7", "type": "ArrayList"},
                ]
                children[1]["variablesReference"] = 6
                children[2]["variablesReference"] = 7
                children[3]["variablesReference"] = 8
                return {"variables": children, "truncated": False}
            if operation == "set_variable":
                # As the worker answers a name its parent does not list.
                raise ValueError(f"{args['name']!r} is not a field or element the debugger lists")
            raise AssertionError(operation)

    with pytest.raises(ValueError) as caught:
        apply_set(Holder(), {}, selector, "1")
    assert classify(caught.value)["code"] == code, str(caught.value)


def test_a_bundle_without_member_sets_is_a_mutation_refusal():
    from peeky.errors import classify

    message = (
        "This Java adapter bundle predates field and element changes, so only locals can be "
        "changed; rebuild it with scripts/setup_java_adapter.py"
    )
    assert classify(ValueError(message))["code"] == "MUTATION_REJECTED"


@pytest.mark.parametrize(
    "error, code",
    [
        ("Variable collection exceeds bounded expansion limit", "BUDGET_EXHAUSTED"),
        ("Expansion ancestry changed or is ambiguous", "STALE_HANDLE"),
        ("Adapter does not support variable mutation", "INVALID_REQUEST"),
    ],
)
def test_the_workers_own_errors_keep_their_codes_in_an_intervention(error, code):
    # A budget or stale-handle failure while reading the parent became
    # NOT_FOUND, and INVALID_REQUEST took the evidence store's hint.
    from peeky.errors import classify
    from peeky.probe import apply_set

    class Holder(FakeWorker):
        def dispatch(self, operation, args):
            if operation == "inspect":
                items = {"name": "items", "value": "Item[600]@3", "variablesReference": 5}
                return {"frames": [{}], "scopes": [{"reference": 1, "variables": [items]}]}
            raise ValueError(error)

    with pytest.raises(ValueError) as caught:
        apply_set(Holder(), {}, "items[0].v", "1")
    payload = classify(caught.value)
    assert payload["code"] == code and error in payload["message"]
    assert "store" not in payload["hint"]


def test_a_remote_row_error_keeps_its_code():
    from peeky.cli import _Remote
    from peeky.errors import PeekyError, Refused

    class Client:
        def call(self, operation, **args):
            payload = {"code": "STALE_HANDLE", "message": "Unknown or stale expansion handle"}
            raise PeekyError("u", 400, {}, payload)

    with pytest.raises(Refused) as caught:
        _Remote(Client()).dispatch("expand", {})
    assert caught.value.code == "STALE_HANDLE"


def test_a_c_arrow_selector_says_to_use_a_dot():
    # `stats->total_us` was refused without saying what to write instead.
    with pytest.raises(ValueError, match=r"follow a pointer with \. \(not ->\): stats\.total_us"):
        parse_selector("stats->total_us")


def test_a_java_lists_children_are_its_elements_with_their_selectors():
    # `session get workers` listed elementData, modCount and size.
    from peeky.probe import _children

    fields = [
        {
            "name": "elementData",
            "type": "Object[]",
            "value": "Object[10]@5",
            "variablesReference": 6,
        },
        {"name": "modCount", "type": "int", "value": "3", "variablesReference": 0},
        {"name": "size", "type": "int", "value": "3", "variablesReference": 0},
    ]
    slots = [
        {"name": str(i), "type": "Worker", "value": f"Worker@{9 + i}", "variablesReference": 9 + i}
        for i in range(10)
    ]

    class Lists:
        def dispatch(self, operation, args):
            assert operation == "expand"
            page = fields if args["handle"] == 4 else slots[: args.get("count", 64)]
            return {"variables": page, "truncated": False}

    workers = {
        "name": "workers",
        "type": "ArrayList",
        "value": "ArrayList@4",
        "variablesReference": 4,
    }
    shown = _children(Lists(), {}, "workers", workers)
    assert [(c["name"], c["selector"], c["value"]) for c in shown["children"]] == [
        ("[0]", "workers[0]", "Worker@9"),
        ("[1]", "workers[1]", "Worker@10"),
        ("[2]", "workers[2]", "Worker@11"),
    ]
    assert all(c["expandable"] for c in shown["children"]) and "children_truncated" not in shown


def test_a_java_list_whose_backing_array_is_too_big_lists_its_fields():
    # A list with over 512 slots lost its children (and its size).
    from peeky.probe import _children

    fields = [
        {
            "name": "elementData",
            "type": "Object[]",
            "value": "Object[1000]@5",
            "variablesReference": 6,
        },
        {"name": "modCount", "type": "int", "value": "2", "variablesReference": 0},
        {"name": "size", "type": "int", "value": "2", "variablesReference": 0},
    ]

    class Big:
        def dispatch(self, operation, args):
            if args["handle"] == 4:
                return {"variables": fields, "truncated": False}
            raise ValueError("Variable collection exceeds bounded expansion limit")

    big = {"name": "big", "type": "ArrayList", "value": "ArrayList@4", "variablesReference": 4}
    shown = _children(Big(), {}, "big", big)
    assert [c["selector"] for c in shown["children"]] == [
        "big.elementData",
        "big.modCount",
        "big.size",
    ]


def test_a_probe_that_times_out_says_what_the_debugger_last_reported(monkeypatch):
    # A stop the debugger never reported looked like a slow program; the events
    # and the DAP trace show it.
    import peeky.probe as probe_module

    class Silent(FakeWorker):
        def dispatch(self, operation, args):
            self.calls.append(operation)
            if operation == "wait":
                return {"state": "running"}
            if operation == "report":
                events = {"recent": [{"event": "continued"}], "ignored": {}, "trace": ["->"]}
                return {"output": "", "output_truncated": False, "debugger_events": events}
            return super().dispatch(operation, args)

    worker = Silent()
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("main.py", 8, ("x",), timeout=0.3))
    assert result["status"] == "timeout" and result["error"]["code"] == "TIMEOUT"
    assert result["debugger_events"]["recent"] == [{"event": "continued"}]


@pytest.mark.parametrize(
    "kind, composite, unread",
    [
        ("alloc::vec::Vec<(char, usize), alloc::alloc::Global>", True, False),
        ("alloc::vec::Vec<(char, usize), alloc::alloc::Global> *", True, False),
        ("alloc::vec::Vec<(i8), alloc::alloc::Global>", True, False),
        ("&[demo::Interval]", True, False),
        ("alloc::vec::Vec<demo::Pair<unsigned short>, alloc::alloc::Global>", True, False),
        # A tuple with a String, or with any field read (the rest not read).
        ("&[(i32, alloc::string::String)]", True, False),
        ("&[((i32, i32), i32)]", True, False),
        # Never read: a tuple with no field read, nested containers, enums, std structs.
        ("&[(alloc::vec::Vec<i32, alloc::alloc::Global>, &str)]", False, True),
        ("&[(fn(i32) -> i32, &str)]", False, True),
        (
            "alloc::vec::Vec<alloc::vec::Vec<int, alloc::alloc::Global>, alloc::alloc::Global>",
            False,
            True,
        ),
        ("alloc::vec::Vec<core::option::Option<u8>, alloc::alloc::Global>", False, True),
        (
            "alloc::vec::Vec<core::ops::range::Range<unsigned long>, alloc::alloc::Global>",
            False,
            True,
        ),
        # LLDB's spelling of a scalar Vec is not a composite (`Vec<int>` is Vec<i32>).
        ("alloc::vec::Vec<int, alloc::alloc::Global>", False, False),
        ("alloc::vec::Vec<char32_t, alloc::alloc::Global> *", False, False),
    ],
)
def test_rust_containers_of_tuples_and_structs_are_told_apart(kind, composite, unread):
    from peeky.probe import _rust_composite, _unread_rust

    assert (_rust_composite(kind), _unread_rust(kind)) == (composite, unread)


def test_a_numbered_field_selector_says_how_rust_names_tuple_fields():
    with pytest.raises(ValueError, match=r"\.__0, \.__1 in Rust.*: runs\[0\]\.__0$"):
        parse_selector("runs[0].0")


def test_set_at_locations_parse_with_a_hit_and_are_bounded():
    from peeky.probe import MAX_SET_AT_HIT, parse_set_at

    assert parse_set_at("stock.cpp:22@3", "colon=6") == ("stock.cpp", 22, 3, "colon", "6")
    assert parse_set_at("23", "w.start = 0") == (None, 23, 1, "w.start", "0")
    assert parse_set_at("src/main.rs:5@12", "x=a=b") == ("src/main.rs", 5, 12, "x", "a=b")
    for where, assignment in (("22@0", "x=1"), ("x@3", "x=1"), ("22@", "x=1"), ("22", "x")):
        with pytest.raises(ValueError, match="--set-at"):
            parse_set_at(where, assignment)
    ok = Probe("a.c", 3, set_at=((4, 2, "x", "1"),))
    assert ok.set_at == ((4, 2, "x", "1"),)
    for set_at in (
        ((4, 0, "x", "1"),),
        ((4, MAX_SET_AT_HIT + 1, "x", "1"),),
        ((0, 1, "x", "1"),),
        ((4, 1, "x", 1),),
        ((4, 1, "x()", "1"),),
        (("4", 1, "x", "1"),),
        ((4, 1, "x", "1"),) * 9,
    ):
        with pytest.raises(ValueError):
            Probe("a.c", 3, set_at=set_at)
    # --set and --set-at share the bound on interventions.
    with pytest.raises(ValueError, match="--set and --set-at together"):
        Probe("a.c", 3, set=(("x", "1"),) * 4, set_at=((4, 1, "x", "1"),) * 5)
    with pytest.raises(Exception, match="map and dict entries cannot be changed"):
        Probe("a.c", 3, set_at=((4, 1, "m['k']", "1"),))
    # One set of after-set rows per captured stop with changes, in the row budget.
    many = tuple(f"a{i}" for i in range(20))
    assert Probe("a.c", 3, many, hits="all", set_at=((3, 2, "a", "1"),)).limit == 99
    assert Probe("a.c", 3, many, hits="all", set_at=((4, 2, "a", "1"),)).limit == 100


class Lines(FakeWorker):
    """Stops at lines 8 and 9 of main.py in turn, `stops` times each, then ends."""

    def __init__(self, stops=3, moved=None, names=None):
        super().__init__()
        self.order = [line for _ in range(stops) for line in (8, 9)]
        self.moved, self.placed, self.previews = moved, [], []
        # {line: the function of each of its stops}, else `main`.
        self.names, self.name = {line: list(n) for line, n in (names or {}).items()}, "main"
        # {frame name: the generic instance the worker names for it}.
        self.instances = {}

    def dispatch(self, operation, args):
        if operation == "launch":
            self.calls.append(operation)
            self.placed.append([point["line"] for point in args["breakpoints"]])
            # Only the lines with breakpoints stop.
            asked = {point["line"] for point in args["breakpoints"]}
            self.order = [line for line in self.order if line in asked]
            replies = [
                {"id": index, "verified": True, "line": self.moved.get(p["line"], p["line"])}
                if self.moved
                else {"id": index, "verified": True, "line": p["line"]}
                for index, p in enumerate(args["breakpoints"], 1)
            ]
            return {
                "session_id": "s",
                "breakpoints": replies,
                "artifact_manifest_id": None,
                "target_identity": None,
                "source_sha256": "x",
            }
        if operation == "breakpoints":
            self.calls.append(operation)
            self.placed.append([point["line"] for point in args["breakpoints"]])
            lines = {point["line"] for point in args["breakpoints"]}
            self.order = [line for line in self.order if line in lines]
            return {"breakpoints": [{"line": p["line"]} for p in args["breakpoints"]]}
        if operation == "wait":
            self.calls.append(operation)
            if not self.order:
                return {"state": "terminated"}
            self.line = self.order.pop(0)
            self.name = (self.names.get(self.line) or ["main"]).pop(0)
            return {
                "state": "stopped",
                "stop_id": "t",
                "event": {"body": {"reason": "breakpoint"}},
                "location": f"main.py:{self.line}",
            }
        if operation == "inspect":
            self.previews.append((self.line, args.get("previews")))
            listed = super().dispatch(operation, args)
            listed["frames"][0]["name"] = self.name
            if self.name in self.instances:
                listed["instance"] = self.instances[self.name]
            return listed
        if operation == "set_variable":
            self.calls.append(operation)
            return {"name": args["name"], "before": "1", "after": args["value"]}
        return super().dispatch(operation, args)


def test_set_at_applies_each_change_at_its_own_stop_and_reports_the_rest(monkeypatch):
    import peeky.probe as probe_module

    # Line 9's second stop is in a lambda on that line: it counts.
    worker = Lines(names={9: ["main", "<lambda>", "main"]})
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    request = Probe(
        "main.py", 8, ("x",), set_at=((9, 2, "x", "5"), (8, 3, "x", "6"), (9, 4, "x", "7"))
    )
    result = probe_module.run_probe("/w", request)
    assert result["status"] == "completed" and result["hits"] == 1
    # Line 8 is captured once; its 3rd stop (kept for its --set-at) is not.
    assert {(row["hit"], row["phase"]) for row in result["rows"]} == {(1, "observed")}
    assert [
        (change["after"], change["line"], change["hit"], change["at"], change["capture_hit"])
        for change in result["interventions"]
    ] == [("5", 9, 2, "9@2", 1), ("6", 8, 3, "8@3", 1)]
    assert [change["function"] for change in result["interventions"]] == ["<lambda>", "main"]
    assert (
        "--set-at line 9 stopped in 2 functions: main (stops 1, 3); <lambda> (stop 2). @HIT "
        "counts every stop of the line, in any function; each change says which function's "
        "variable it changed." in result["warnings"]
    )
    # Line 8 stopped in one function only: no such warning for it.
    assert not any(w.startswith("--set-at line 8 stopped") for w in result["warnings"])
    text = probe_module._table(result)
    assert "set x: 1 -> 5  (--set-at line 9@2, in <lambda>)" in text
    assert "status: completed (1 requested change not made)  hits: 1" in text
    # A breakpoint is kept only while a later stop needs it.
    assert worker.placed == [[8, 9], [9]]
    assert result["unapplied"] == [
        {"option": "--set-at", "at": "9@4", "name": "x", "value": "7", "stops": 3}
    ]
    assert any("9@4 x=7 was not applied: line 9 stopped 3 time(s)" in w for w in result["warnings"])
    # The other five stops only counted or changed.
    assert len(result["pauses_ms"]) == 1 and len(result["set_pauses_ms"]) == 5
    # Previews only where values are captured: line 8's first stop.
    assert [previews for line, previews in worker.previews if previews] == [["x"]]
    assert worker.previews[0] == (8, ["x"])
    assert not any("Interventions are applied once" in w for w in result["warnings"])


def test_a_set_at_line_the_debugger_moves_is_refused(monkeypatch):
    import peeky.probe as probe_module

    worker = Lines(moved={9: 10})
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("main.py", 8, ("x",), set_at=((9, 1, "x", "5"),)))
    assert result["status"] == "failed" and result["error"]["code"] == "INVALID_REQUEST"
    assert (
        "line 9 has no code of its own (the debugger moved its breakpoint to line 10)"
        in (result["error"]["message"])
    )
    assert "set_variable" not in worker.calls and "wait" not in worker.calls


def test_the_http_probe_passes_set_at_and_refuses_malformed_ones(tmp_path, monkeypatch):
    import subprocess
    import threading

    from peeky import worker as worker_module
    from peeky.client import Client

    seen = {}

    class Process:
        pid = 0

        def __init__(self, command, **kwargs):
            seen["command"] = command

        def communicate(self, timeout=None):
            return b'{"status": "completed"}', b""

    monkeypatch.setattr(subprocess, "Popen", Process)
    request = Probe("x.c", 3, set_at=((4, 2, "w.start", "9"), (5, 1, "n", "a b")))
    assert worker_module._run_probe_process(tmp_path, request, None, 60) == {"status": "completed"}
    command = seen["command"]
    at = [index for index, part in enumerate(command) if part == "--set-at"]
    assert [command[i + 1 : i + 3] for i in at] == [["4@2", "w.start=9"], ["5@1", "n=a b"]]

    server = worker_module.make_server(tmp_path, "unit-test-secret-token-1234", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", "unit-test-secret-token-1234")
        for set_at in ([[4, 2, "x"]], [["4", 2, "x", "1"]], [[4, 2, "x", 1]], {"x": 1}):
            with pytest.raises(Exception, match="set_at"):
                client.call("probe", at="x.c:1", set_at=set_at)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_a_set_whose_hit_never_comes_is_unapplied_like_a_set_at(monkeypatch):
    # A requested change that never happened is reported the same way for
    # --set as for --set-at (`unapplied`; the CLI exits with 4).
    import peeky.probe as probe_module

    worker = FakeWorker()
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    request = Probe("main.py", 8, ("x",), (("x", "2"),), hits=2, set_hit=2)
    result = probe_module.run_probe("/w", request)
    assert result["status"] == "completed" and result["interventions"] == []
    assert result["unapplied"] == [
        {"option": "--set", "at": "8@2", "name": "x", "value": "2", "stops": 1}
    ]
    assert (
        "--set x=2 was not applied: the program stopped 1 time(s) at line 8, before hit 2."
        in (result["warnings"])
    )


def test_outputs_and_diff_say_a_requested_change_whose_hit_never_came_was_not_made(
    monkeypatch, tmp_path
):
    # runs.sets holds requests; `outputs` and `diff` said where each
    # change "applied", a --set or --set-at whose hit never came too.
    import json

    import peeky.probe as probe_module
    from peeky import evidence

    store = tmp_path / "evidence" / "evidence.db"
    workers = [Lines(names={9: ["main", "<lambda>", "main"]}), Lines(stops=1), Lines(stops=1)]
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: workers.pop(0))
    for request in (
        Probe("main.py", 8, ("x",), set_at=((9, 2, "x", "5"), (9, 4, "x", "7"))),
        Probe("main.py", 8, ("x",), (("x", "2"),), hits=2, set_hit=2),
        Probe("main.py", 8, ("x",), (("x", "2"),), hits=1, set_hit=1),
    ):
        result = probe_module.run_probe("/w", request)
        evidence.record(store, result)
    rows = evidence.run_query(store, "outputs", {"a": 1, "b": 2})["rows"]
    assert [json.loads(row["sets"]) for row in rows] == [
        [
            ["x", "5", "requested at 9@2"],
            ["x", "7", "requested at 9@4, not made: that hit never came"],
        ],
        [["x", "2", "requested at 8@2, not made: that hit never came"]],
    ]
    # The same request, made in one run and not in the other, differs in `diff`.
    (row,) = [
        r
        for r in evidence.run_query(store, "diff", {"a": 2, "b": 3})["rows"]
        if r["selector"] == "(sets)"
    ]
    assert json.loads(row["value_a"]) == [
        ["x", "2", "requested at 8@2, not made: that hit never came"]
    ]
    assert json.loads(row["value_b"]) == [["x", "2", "requested at 8@1"]]


def test_a_set_no_stop_allows_is_refused_before_launch_and_never_saved(
    monkeypatch, tmp_path, capsys
):
    # The worker refuses a `--set` its build rules out before the program runs; the
    # probe fails with that reason, nothing ran, and no run is kept as evidence.
    import json

    import peeky.cli as cli_module
    import peeky.probe as probe_module
    from peeky.errors import Refused

    class Refusing(FakeWorker):
        def dispatch(self, operation, args):
            if operation == "launch":
                self.calls.append(("launch", args.get("sets")))
                refused = Refused("MUTATION_REJECTED", "`x` is kept in a register. Refused")
                refused.before_launch = True
                raise refused
            return super().dispatch(operation, args)

    worker = Refusing()
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("main.py", 8, ("x",), (("x", "2"),)))
    assert worker.calls[0] == ("launch", [{"line": 8, "name": "x", "value": "2"}])
    assert worker.calls[-1] == "close"
    assert result["status"] == "failed" and result["refused_before_launch"] is True
    assert result["error"]["code"] == "MUTATION_REJECTED" and result["hits"] == 0
    assert "black_box" in result["error"]["hint"]
    # Without one, the launch names no sets.
    plain = FakeWorker()
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: plain)
    probe_module.run_probe("/w", Probe("main.py", 8, ("x",)))
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "main.py").write_text("x = 1\n")
    monkeypatch.setattr(cli_module, "run_probe", lambda *args, **kwargs: dict(result))
    command = ["probe", str(tmp_path), "--at", "main.py:1", "--set", "x=2", "--json"]
    # A refusal exits 2, as any request Peeky turns down.
    assert cli_module.main(command) == 2
    shown = json.loads(capsys.readouterr().out)
    assert "evidence" not in shown and shown["refused_before_launch"] is True
    assert not (tmp_path / "state" / "evidence" / "evidence.db").exists()
    # Its text has no empty table, status or timing: only why, and what to do.
    assert cli_module.main(command[:-1]) == 2
    text = capsys.readouterr().out
    assert text.splitlines()[0].startswith("error [MUTATION_REJECTED]: `x` is kept"), text
    assert "hint: " in text and "hit  phase" not in text and "status:" not in text, text


def test_rows_say_why_no_stop_can_change_them():
    # `[not settable: register]` beside the value, never with `[settable]`.

    rows = [
        {"hit": 1, "phase": "observed", "line": 3, "selector": "width", "type": "u64"}
        | {"value": "10", "settable": False, "not_settable": "register"},
        {"hit": 1, "phase": "observed", "line": 3, "selector": "total", "type": "u64"}
        | {"value": "0", "settable": True},
    ]
    text = _table(
        {"rows": rows, "status": "completed", "hits": 1, "seconds": 0, "interventions": []}
    )
    assert "10  [not settable: register]" in text and "0  [settable]" in text


def test_rows_say_when_neither_mark_can_be_promised():
    # A Java element whose array's holder the launcher could not tell in time.
    from peeky.cli import _get_lines

    why = "Peeky could not tell within 500 ms; `set` searches again, for up to 2 s"
    row = {"hit": 1, "phase": "observed", "line": 3, "selector": "xs[0]", "type": "int"}
    row |= {"value": "4", "settable": False, "maybe_settable": why}
    text = _table(
        {"rows": [row], "status": "completed", "hits": 1, "seconds": 0} | {"interventions": []}
    )
    assert f"4  [maybe settable: {why}]" in text and "[settable]" not in text
    child = {"name": "0", "selector": "xs[0]", "type": "int", "value": "4"}
    child |= {"settable": False, "maybe_settable": why}
    lines = _get_lines(
        {"selector": "xs", "type": "int[]", "value": "int[1]@5 [4]"} | {"children": [child]}
    )
    assert lines[-1] == f"  xs[0]: int = 4  [maybe settable: {why}]"


def test_the_launch_names_each_change_with_its_own_line(monkeypatch):
    # The worker judges a --set at the --at line and each --set-at at its
    # own line before the program runs.
    import peeky.probe as probe_module

    launched = {}

    class Recording(Lines):
        def dispatch(self, operation, args):
            if operation == "launch":
                launched.update(args)
            return super().dispatch(operation, args)

    worker = Recording()
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    request = Probe("main.py", 8, ("x",), (("x", "2"),), set_at=((9, 1, "w.start", "5"),))
    probe_module.run_probe("/w", request)
    # With each value, which a Java local's declared type judges before the run.
    assert launched["sets"] == [
        {"line": 8, "name": "x", "value": "2"},
        {"line": 9, "name": "w.start", "value": "5"},
    ]
    assert launched["breakpoints"] == [{"line": 8}, {"line": 9}]


def test_a_set_at_made_beside_an_unmade_set_names_only_the_set(monkeypatch):
    # "No intervention was applied" was said while a --set-at was made.
    import peeky.probe as probe_module

    worker = Lines()
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    request = Probe(
        "main.py", 8, ("x",), (("x", "5"),), hits="all", set_hit=9, set_at=((9, 2, "x", "7"),)
    )
    result = probe_module.run_probe("/w", request)
    assert [change["at"] for change in result["interventions"]] == ["9@2"]
    assert not any("No intervention" in warning for warning in result["warnings"])
    assert (
        "--set x=5 was not applied: the program stopped 3 time(s) at line 8, before hit 9."
        in (result["warnings"])
    )
    assert result["unapplied"] == [
        {"option": "--set", "at": "8@9", "name": "x", "value": "5", "stops": 3}
    ]
    assert probe_module.probe_exit_code(result) == 4


def test_generic_instances_and_a_shared_line_are_told_apart(monkeypatch):
    # A Rust generic's instances differ only in their symbol's hash, which
    # the warning's names dropped (`tf::<A>`, `tf::<B>`: no warning).
    import peeky.probe as probe_module

    a, b = "rc::tf::h0000000000000001", "rc::tf::h0000000000000002"
    worker = Lines(names={8: [a, b, a], 9: [a, b, a]})
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    request = Probe("main.py", 8, ("x",), hits="all", set_at=((9, 2, "x", "5"),))
    result = probe_module.run_probe("/w", request)
    assert (
        "Line 8 stopped in 2 functions: rc::tf::h0000000000000001 (hits 1, 3); "
        "rc::tf::h0000000000000002 (hit 2). --hits, --set-hit and --set-at's @HIT count every "
        "stop; the function column says which is which." in result["warnings"]
    )
    assert any(
        w.startswith(f"--set-at line 9 stopped in 2 functions: {a} (stop 1); {b} (stop 2).")
        for w in result["warnings"]
    ), result["warnings"]
    # With the instances the worker names (their type arguments), and --at a --set-at line
    # too: one warning for the line.
    worker = Lines(names={8: [a, b, a], 9: [a, b, a]})
    worker.instances = {a: "rc::tf<rc::A>", b: "rc::tf<rc::B>"}
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    request = Probe("main.py", 8, ("x",), hits="all", set_at=((8, 2, "x", "5"),))
    result = probe_module.run_probe("/w", request)
    stopped = [w for w in result["warnings"] if "stopped in 2 functions" in w]
    assert stopped == [
        "Line 8 stopped in 2 functions: rc::tf<rc::A> (stops 1, 3); rc::tf<rc::B> (stop 2). "
        "--hits, --set-hit and --set-at's @HIT count every stop of the line, in any "
        "function; the function column and each change say which is which."
    ], result["warnings"]
    (change,) = result["interventions"]
    assert change["instance"] == "rc::tf<rc::B>"
    assert "set x: 1 -> 5  (--set-at line 8@2, in rc::tf<rc::B>)" in probe_module._table(result)


def test_peeky_probe_exits_as_peeky_probe_does(monkeypatch, capsys):
    # The `peeky-probe` script exited 0 where `peeky probe` exits 4.
    import peeky.probe as probe_module

    for result, code in (
        ({"status": "completed"}, 0),
        ({"status": "completed", "unapplied": [{"at": "9@3"}]}, 4),
        ({"status": "failed", "unapplied": [{"at": "9@3"}]}, 1),
    ):
        shown = {"rows": [], "interventions": [], "hits": 0, "seconds": 0, **result}
        monkeypatch.setattr(probe_module, "run_probe", lambda *args, **kwargs: shown)
        assert probe_module.main(["--at", "main.py:8", "--json"]) == code
        capsys.readouterr()


def test_a_refusal_names_the_functions_sharing_a_reason_once():
    from peeky.fields import FUNCTION, by_function

    reasons = [
        ("fc", f"in {FUNCTION}, `a.pp` is a pointer"),
        ("fd", f"in {FUNCTION}, `a.pp` is a pointer"),
        ("fe", f"{FUNCTION} has no local `a`"),
        ("fg", f"in {FUNCTION}, `a.pp` is a pointer"),
    ]
    assert by_function(reasons) == "in fc, fd and fg, `a.pp` is a pointer; fe has no local `a`"
    reasons.append(("fh", f"{FUNCTION} has no local `a`"))
    assert by_function(reasons).endswith("; fe and fh have no local `a`")


def test_instances_that_read_alike_are_told_apart_by_their_hash(monkeypatch):
    # Two instances whose names were cut at the same 300 characters got
    # the same label on both changes; the cut was not marked.
    import peeky.probe as probe_module
    from peeky.rust_mir import _instance

    long = "tf<" + "core::iter::Map<" * 30 + "i64" + ">" * 31
    assert len(_instance(long)) == 300 and _instance(long).endswith("…")
    assert _instance("tf<u8>") == "tf<u8>" and _instance("main") == ""
    a, b = "lg::tf::h0000000000000001", "lg::tf::h0000000000000002"
    worker = Lines(names={9: [a, b, a]})
    cut = "lg::" + _instance(long)
    worker.instances = {a: cut, b: cut}
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    request = Probe("main.py", 8, ("x",), set_at=((9, 1, "x", "5"), (9, 2, "x", "6")))
    result = probe_module.run_probe("/w", request)
    labels = [change["instance"] for change in result["interventions"]]
    assert labels == [f"{cut} (h0000000000000001)", f"{cut} (h0000000000000002)"], labels
    text = probe_module._table(result)
    assert f"in {cut} (h0000000000000001))" in text and f"in {cut} (h0000000000000002))" in text
    assert any(f"{cut} (h0000000000000002) (stop 2)" in w for w in result["warnings"])


def test_python_code_the_debugger_names_alike_is_said_not_merged(tmp_path, monkeypatch):
    # Two lambdas on one line are both `<lambda>`, and debugpy's frames
    # give no column, so their stops were listed as one function's.
    import peeky.probe as probe_module

    assert probe_module._python_alike(
        "def main():\n    f = lambda v: v + 1; g = lambda v: v * 2\n", [2]
    ) == {2: {"<lambda>": 2}}
    assert probe_module._python_alike("x = (lambda: 1)()\n", [1]) == {}
    lines = ["x = 0"] * 7 + ["x = 1", "f = lambda v: v + 1; g = lambda v: v * 2"]
    (tmp_path / "main.py").write_text("\n".join(lines) + "\n")
    worker = Lines(names={9: ["main", "<lambda>", "<lambda>"]})
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    request = Probe("main.py", 8, ("x",), set_at=((9, 3, "x", "5"),))
    result = probe_module.run_probe(tmp_path, request)
    assert (
        "--set-at line 9 stopped in 2 functions: main (stop 1); <lambda> (stops 2, 3; 2 on "
        "the line, which the debugger names alike, so their stops are counted together). @HIT"
        in " ".join(result["warnings"])
    ), result["warnings"]
    (change,) = result["interventions"]
    assert change["alike"] == 2
    text = probe_module._table(result)
    assert "(--set-at line 9@3, in <lambda>, one of 2 on the line the debugger names alike)" in (
        text
    )


def test_a_warning_says_how_many_functions_it_does_not_list():
    # "stopped in 6 functions" listed 4.
    from peeky.probe import _stops_by_function

    functions = {f"f{i}": [i] for i in range(10)}
    shown = _stops_by_function(functions, {name: name for name in functions}, "stop")
    assert shown.startswith("f0 (stop 0); f1 (stop 1)") and shown.endswith(
        "f7 (stop 7); and 2 more"
    )


def test_a_selector_by_line_that_names_several_bindings_is_refused():
    # `x @ part.rs:1` where two included files named `part.rs` (or two
    # `let x` on one line) both declare an `x` there: the one lldb-dap lists by its
    # plain name is not selected by it.
    from peeky.probe import Refused, _scope_lookup

    row = {"name": "x", "value": "1", "declared_at": "x @ part.rs:1", "declared_at_shared": 2}
    scopes = [{"name": "Locals", "variables": [row]}]
    with pytest.raises(Refused, match="2 bindings on this line: not selectable by line"):
        _scope_lookup(None, {}, {}, scopes, "x @ part.rs:1")
    alone = {**row, "declared_at_shared": 1}
    found, _ = _scope_lookup(
        None, {}, {}, [{"name": "Locals", "variables": [alone]}], "x @ part.rs:1"
    )
    assert found is alone
    assert _scope_lookup(None, {}, {}, scopes, "x")[0] is row


def test_an_opaque_rust_enum_row_says_why_where_it_is_and_what_to_read():
    # Rota's `last: Option<Engineer>` read `<unqualified Rust enum layout>`, with no
    # address to compare `l` of `if let Some(l) = &last` with, and nothing to read instead.
    from peeky.probe import _capture

    why = (
        "Peeky decodes `Option<i32>`, and an `Option` of a tuple or struct your crate defines "
        "whose layout the debug information proves; it does not decode this `Option<u32>`. It "
        "is stored at 0x000000016fbfab10; read what `if let Some(x) = &count` binds instead"
    )
    count = {
        "name": "count",
        "type": "core::option::Option<unsigned int>",
        "value": "<unqualified Rust enum layout>",
        "variablesReference": 0,
        "view": "opaque",
        "opaque_reason": why,
    }

    class Worker(Tree):
        def dispatch(self, operation, args):
            if operation == "inspect":
                return {**snapshot(count), "frames": [{"line": 3, "name": "f"}]}
            return super().dispatch(operation, args)

    [row] = _capture(Worker({}), {}, ("count",), 0, "observed")
    assert row["value"] == "<unqualified Rust enum layout>" and row["view"] == "opaque", row
    assert row["note"] == why[0].upper() + why[1:] + ".", row
    # Without a reason (an older worker), no note.
    count.pop("opaque_reason")
    [row] = _capture(Worker({}), {}, ("count",), 0, "observed")
    assert "note" not in row, row


def test_a_signal_stop_counts_as_let_go_on_only_where_the_program_resumed(monkeypatch):
    # `continue` serves another thread's breakpoint stop from the same
    # all-threads stop first, and resumes nothing; the warning said the probe let the
    # program go on after the signal.
    import peeky.probe as probe_module

    class Queued(FakeWorker):
        def __init__(self):
            super().__init__(
                reason="exception", seen={"kind": "signal", "description": "signal SIGINT"}
            )
            self.waits = 0

        def dispatch(self, operation, args):
            if operation == "continue":
                self.calls.append(operation)
                return {"state": "stopped", "resumed": False}
            return super().dispatch(operation, args)

    worker = Queued()
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("main.py", 8, ("x",)))
    assert "continue" in worker.calls and "inspect" not in worker.calls
    assert not any("let the program go on" in w for w in result.get("warnings", ())), result


def test_an_inferred_exit_says_what_it_costs_and_what_to_do(monkeypatch):
    # An inferred exit's warning says what it costs and what to do.
    import peeky.probe as probe_module
    from peeky.probe import Probe

    class Ended:
        waited = False

        def dispatch(self, operation, args):
            if operation == "launch":
                return {
                    "session_id": "s",
                    "breakpoints": [{"id": 1, "verified": True, "line": 8}],
                    "artifact_manifest_id": None,
                    "target_identity": None,
                    "source_sha256": "x",
                }
            if operation == "wait":
                if self.waited:
                    return {"state": "terminated", "exit_inferred": True}
                self.waited = True
                return {
                    "state": "stopped",
                    "stop_id": "t",
                    "event": {"body": {"reason": "breakpoint"}},
                }
            if operation == "inspect":
                return {
                    "frames": [{"source": {"path": "/w/main.py"}, "line": 8}],
                    "scopes": [
                        {
                            "name": "Locals",
                            "reference": 1,
                            "variables": [{"name": "x", "value": "1", "variablesReference": 0}],
                        }
                    ],
                }
            if operation == "report":
                return {"output": "", "output_truncated": False}
            return {}

        def close(self):
            pass

    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: Ended())
    result = probe_module.run_probe("/w", Probe("main.py", 8, ("x",)))
    assert result["status"] == "completed", result
    (warning,) = [w for w in result["warnings"] if "never reported" in w]
    assert "exit code is unknown" in warning and "nothing to do" in warning, warning
    assert "as java-debug sometimes doesn't" in warning and "#27" not in warning, warning


def test_a_set_hit_past_the_captured_hits_says_what_to_do():
    # `--set gap=20 --set-hit 4` gave `Probe set_hit
    # must be within 1..1`, an internal name, with no way out.
    with pytest.raises(ValueError) as caught:
        Probe("sessions.py", 34, ("gap",), set=(("gap", "20"),), set_hit=4)
    assert str(caught.value) == (
        "--set-hit 4 is past the last hit this probe captures (1, --hits 1): raise --hits "
        "to 4 or more, or use --hits all; or --set-at 34@4 NAME=VALUE changes it at that "
        "hit without capturing there"
    )
    with pytest.raises(ValueError, match=r"^--set-hit must be within 1\.\.1$"):
        Probe("sessions.py", 34, ("gap",), set=(("gap", "20"),), set_hit=0)


@pytest.mark.parametrize(
    ("selector", "advice"),
    [
        ("self.items[at]", "capture `at` first, then use its value, as self.items[2]"),
        ("xs[i + 1]", "capture what it is computed from first, then use its value, as xs[2]"),
        ("d[k]", "capture `k` first, then use its value, as d[2]"),
    ],
)
def test_an_index_named_by_a_variable_says_to_read_it_first(selector, advice):
    # `self.items[at]` was refused, saying only that selectors are paths.
    with pytest.raises(ValueError) as refused:
        parse_selector(selector)
    message = str(refused.value)
    assert "an index or key is a literal (a number, or text in quotes)" in message, message
    assert message.endswith(advice), message


def test_a_too_long_number_gets_no_such_advice():
    with pytest.raises(ValueError) as refused:
        parse_selector("xs[1234567]")
    assert "capture" not in str(refused.value)


class _Stops:
    """Stops once at each of ``lines`` (a --set-at line's stops), never at the --at line,
    then ends with ``exit_code``. `trend` holds "1" until a change."""

    def __init__(self, lines, exit_code=0, proven=True):
        self.lines, self.exit_code, self.value, self.line = list(lines), exit_code, "1", None
        # Whether the worker proves a write of the same text a no-op (worker._written).
        self.proven = proven

    def dispatch(self, operation, args):
        if operation == "launch":
            return {
                "session_id": "s",
                "breakpoints": [
                    {"id": index, "verified": True, "line": point["line"]}
                    for index, point in enumerate(args["breakpoints"], 1)
                ],
                "artifact_manifest_id": None,
                "target_identity": {"language": "c"},
                "source_sha256": "x",
            }
        if operation == "wait":
            if not self.lines:
                return {"state": "terminated"}
            self.line = self.lines.pop(0)
            return {
                "state": "stopped",
                "stop_id": f"t{len(self.lines)}",
                "event": {"body": {"reason": "breakpoint"}},
                "location": f"tides.c:{self.line}",
            }
        if operation == "inspect":
            frame = {"name": "find_waters", "source": {"path": "/w/tides.c"}, "line": self.line}
            return {
                "frames": [frame],
                "scopes": [
                    {
                        "name": "Locals",
                        "reference": 1,
                        "variables": [
                            {"name": "trend", "value": self.value, "variablesReference": 0}
                        ],
                    }
                ],
            }
        if operation == "set_variable":
            before, self.value = self.value, args["value"]
            same = self.proven and before == args["value"]
            return {
                "name": args["name"],
                "before": before,
                "after": args["value"],
                "type": "int",
                **({"unchanged": True} if same else {}),
            }
        if operation == "report":
            return {"output": "", "output_truncated": False, "exit_code": self.exit_code}
        return {}

    def close(self):
        pass


RAN = "The run made 2 changes by --set-at (at line 9): compare with a run without them"


@pytest.mark.parametrize(
    "exit_code, message, hint",
    [
        (
            0,
            f"The program finished without stopping at tides.c:8. {RAN}",
            "Probe the line without the changes to compare; if it never stops there either, "
            "pick a line the program executes (`peeky describe`).",
        ),
        (
            3,
            f"The program failed (exit code 3) without stopping at tides.c:8; its output says "
            f"why. {RAN}",
            "Read its output, and check its arguments and input; probe without the changes to "
            "compare.",
        ),
        (
            None,
            f"The program ended without stopping at tides.c:8 (exit code unknown). {RAN}",
            "If the output shows an error, the program failed before the line; else probe "
            "without the changes to compare, or pick a line it executes.",
        ),
    ],
)
def test_no_stop_after_changes_counts_them_and_claims_no_cause(
    monkeypatch, exit_code, message, hint
):
    # Tides, `--at 33` with six change-and-restore --set-at: every change was made,
    # and NO_STOP said to pick a line the program executes. Nothing proves
    # a change came before the line's chance to run, so the message claims no cause.
    worker = _Stops([9, 9], exit_code)
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    request = Probe("tides.c", 8, ("trend",), set_at=((9, 1, "trend", "0"), (9, 2, "trend", "1")))
    result = probe_module.run_probe("/w", request)
    assert result["status"] == "no-stop" and len(result["interventions"]) == 2, result
    assert result["error"]["code"] == "NO_STOP"
    assert result["error"]["message"] == message
    assert result["error"]["hint"] == hint and len(hint) <= 150
    # Without changes, the line is blamed as before.
    worker = _Stops([], 0)
    result = probe_module.run_probe("/w", Probe("tides.c", 8, ("trend",)))
    assert result["error"]["hint"] == "Pick a line the program executes; see `peeky describe`."


def _no_stop(monkeypatch, lines, set_at, proven=True):
    worker = _Stops(lines, proven=proven)
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("tides.c", 8, ("trend",), set_at=set_at))
    assert result["status"] == "no-stop", result
    return result


def test_no_stop_after_writes_proven_to_change_nothing_blames_the_line(monkeypatch):
    # `trend: 1 -> 1` counted as a change ("after 1 change by --set-at:
    # they may have kept the line from running"). A write proven to change nothing is not
    # one: where every write is such, the line is blamed, as with none.
    result = _no_stop(monkeypatch, [9, 9], ((9, 1, "trend", "1"), (9, 2, "trend", "1")))
    assert [(c["before"], c["after"]) for c in result["interventions"]] == [("1", "1")] * 2
    assert result["error"]["message"] == (
        "The program finished without stopping at tides.c:8; check `peeky describe` for "
        "executable lines"
    )
    assert result["error"]["hint"] == "Pick a line the program executes; see `peeky describe`."
    # One proven no-op and one change: the change alone is counted.
    result = _no_stop(monkeypatch, [9, 9], ((9, 1, "trend", "1"), (9, 2, "trend", "5")))
    assert result["error"]["message"] == (
        "The program finished without stopping at tides.c:8. The run made 1 change by "
        "--set-at (at line 9): compare with a run without them"
    )


def test_no_stop_after_writes_of_the_same_text_not_proven_no_ops_names_them(monkeypatch):
    # The same text is not always a no-op (a Java String written alike
    # is a new object; a Python `1000` may be another). Such writes are named, never taken
    # for no-ops, and never blamed on the line.
    result = _no_stop(
        monkeypatch, [9, 9], ((9, 1, "trend", "1"), (9, 2, "trend", "1")), proven=False
    )
    assert result["error"]["message"] == (
        "The program finished without stopping at tides.c:8. The run made 2 writes by "
        "--set-at with the same text, not proven unchanged (at line 9): "
        "compare with a run without them"
    )
    # A change, a write not proven a no-op, and one proven: the first two are named.
    worker = _Stops([9, 20, 31])

    def dispatch(operation, args, plain=worker.dispatch):
        reply = plain(operation, args)
        if operation == "set_variable" and worker.line == 20:
            reply.pop("unchanged", None)
        return reply

    worker.dispatch = dispatch
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    request = Probe(
        "tides.c",
        8,
        ("trend",),
        set_at=((9, 1, "trend", "0"), (20, 1, "trend", "0"), (31, 1, "trend", "0")),
    )
    result = probe_module.run_probe("/w", request)
    assert [c.get("unchanged") for c in result["interventions"]] == [None, None, True]
    assert result["error"]["message"] == (
        "The program finished without stopping at tides.c:8. The run made 1 change by "
        "--set-at (at line 9), and 1 write by --set-at with the same text, not proven "
        "unchanged (at line 20): compare with a run without them"
    )


def test_no_stop_after_a_change_past_the_line_claims_no_cause(monkeypatch):
    # A --set-at on a line after the --at line got the same causal
    # wording. Where they were is said; that they kept the line from running is not.
    result = _no_stop(monkeypatch, [20], ((20, 1, "trend", "0"),))
    message = result["error"]["message"]
    assert message == (
        "The program finished without stopping at tides.c:8. The run made 1 change by "
        "--set-at (at line 20): compare with a run without them"
    )
    result = _no_stop(
        monkeypatch,
        [9, 20, 31],
        ((9, 1, "trend", "0"), (20, 1, "trend", "2"), (31, 1, "trend", "3")),
    )
    assert (
        "The run made 3 changes by --set-at (at lines 9, 20 and 31)" in result["error"]["message"]
    )
    for text in (message, result["error"]["message"], result["error"]["hint"]):
        assert "kept" not in text and "may have" not in text, text


def test_the_intervention_limit_says_how_many_and_what_to_do():
    # 12 `--set-at`s gave `At most 8 interventions (--set and --set-at
    # together)`, with the generic hint `Fix the request arguments; …`.
    changes = tuple((31, hit, "hh", str(hit)) for hit in range(1, 13))
    with pytest.raises(ValueError) as refused:
        Probe("nightlot.cpp", 40, ("hour",), set_at=changes)
    message = str(refused.value)
    assert f"At most {MAX_SETS} interventions" in message, message
    assert "this one has 12" in message, message
    error = classify(refused.value)
    assert error["code"] == "INVALID_REQUEST", error
    assert "several probes" in error["hint"] and "`--old`/`--new`" in error["hint"], error
    assert len(error["hint"]) <= 150, error["hint"]
    # A probe of 8 is still one probe.
    assert len(Probe("nightlot.cpp", 40, ("hour",), set_at=changes[:MAX_SETS]).set_at) == 8
