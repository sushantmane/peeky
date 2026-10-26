"""Adversarial worker tests for read-only expansion ownership and fresh evidence."""

from copy import deepcopy

import pytest

from peeky.adapters import PythonAdapter
from peeky.worker import DebugWorker


def variable(name, kind="int", ref=0):
    return {"name": name, "type": kind, "value": "1", "variablesReference": ref}


class Backend:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.roots = [variable("request", "Request", 100), variable("value")]
        self.children = {
            100: [variable("page"), variable("nested", "Nested", 200)],
            200: [variable("leaf")],
        }
        self.frames = [
            {"id": i, "line": 2, "source": {"path": "/prepared/main.cpp"}, "moduleId": "built"}
            for i in (1, 2)
        ]
        self.fail_children = False

    def request(self, operation, args):
        self.calls.append((operation, deepcopy(args)))
        if operation == "stackTrace":
            return {"stackFrames": deepcopy(self.frames)}
        if operation == "scopes":
            return {
                "scopes": [
                    {"name": "Locals", "presentationHint": "locals", "variablesReference": 7}
                ]
            }
        if operation == "variables":
            ref = args["variablesReference"]
            if ref != 7 and self.fail_children:
                raise TimeoutError("uncertain expansion")
            # Deliberately ignore DAP paging: worker must enforce output bounds.
            return {"variables": deepcopy(self.roots if ref == 7 else self.children[ref])}
        if operation == "setVariable":
            return {"value": args["value"]}
        if operation == "continue":
            return {}
        raise AssertionError(operation)

    def close(self):
        self.closed = True


class Runtime:
    def __init__(self):
        self.valid = True
        self.checked = []

    def assert_current(self):
        if not self.valid:
            raise RuntimeError("provenance changed")

    def validate_stop(self, frame):
        if frame.get("source", {}).get("path") != "/prepared/main.cpp":
            raise RuntimeError("source changed")

    def validate_debugger(self, dap, frame):
        self.checked.append(frame["id"])
        if frame.get("moduleId") != "built":
            raise RuntimeError("module changed")

    def local_scopes(self, scopes):
        return PythonAdapter().local_scopes(scopes)

    def validate_value(self, value):
        pass

    def validate_variable(self, value, replacement):
        assert value["type"] == "int" and not value["variablesReference"]

    def close(self):
        pass


@pytest.fixture
def worker(tmp_path):
    instance = DebugWorker(tmp_path)
    instance.dap, instance.runtime = Backend(), Runtime()
    instance.session, instance.stop_id, instance.state = "session", "stop", "stopped"
    instance.thread_id = 1
    instance.capabilities = {"supports_set_variable": True}
    return instance


def call(worker, operation, **kwargs):
    return worker.dispatch(operation, {"session_id": "session", "stop_id": "stop", **kwargs})


def inspected(worker, frame=1):
    result = call(worker, "inspect", frame_id=frame)
    scope = result["scopes"][0]
    return scope["reference"], scope["variables"][0]["variablesReference"]


def test_expand_uses_distinct_opaque_handle_and_fresh_scope(worker):
    scope, child = inspected(worker)
    assert child > 0 and child not in (scope, 100)
    worker.dap.calls.clear()
    result = call(worker, "expand", handle=child)
    assert [v["name"] for v in result["variables"]] == ["page", "nested"]
    assert result["variables"][0]["variablesReference"] == 0
    assert result["variables"][1]["variablesReference"] not in (0, scope, child, 200)
    operations = [op for op, _ in worker.dap.calls]
    assert operations.index("scopes") < operations.index("variables")
    assert worker.runtime.checked[-1] == 1


def test_mutable_scope_and_child_handles_are_not_interchangeable(worker):
    scope, child = inspected(worker)
    with pytest.raises(ValueError):
        call(worker, "expand", handle=scope)
    with pytest.raises(ValueError):
        call(worker, "set_variable", reference=child, name="page", value="2")
    assert not any(op == "setVariable" for op, _ in worker.dap.calls)


@pytest.mark.parametrize("frame", [1, 2])
def test_reinspection_revokes_old_child_despite_adapter_ref_reuse(worker, frame):
    _, old = inspected(worker)
    _, current = inspected(worker, frame)
    assert current != old
    with pytest.raises(ValueError):
        call(worker, "expand", handle=old)
    assert call(worker, "expand", handle=current)["variables"]


@pytest.mark.parametrize("change", ["name", "type", "duplicate", "missing"])
def test_fresh_ancestry_rejects_disappeared_or_ambiguous_parent(worker, change):
    _, child = inspected(worker)
    if change == "name":
        worker.dap.roots[0]["name"] = "different"
    elif change == "type":
        worker.dap.roots[0]["type"] = "Other"
    elif change == "duplicate":
        worker.dap.roots.append(deepcopy(worker.dap.roots[0]))
    else:
        worker.dap.roots.pop(0)
    with pytest.raises(ValueError):
        call(worker, "expand", handle=child)
    assert not any(
        op == "variables" and args["variablesReference"] == 100 for op, args in worker.dap.calls
    )


def test_ancestry_follows_fresh_adapter_reference_not_saved_raw_number(worker):
    _, child = inspected(worker)
    worker.dap.roots[0]["variablesReference"] = 300
    worker.dap.children[300] = [variable("fresh")]
    assert [v["name"] for v in call(worker, "expand", handle=child)["variables"]] == ["fresh"]


def test_nested_parent_identity_is_rechecked(worker):
    _, child = inspected(worker)
    nested = call(worker, "expand", handle=child)["variables"][1]["variablesReference"]
    worker.dap.children[100][1]["type"] = "ChangedNested"
    with pytest.raises(ValueError):
        call(worker, "expand", handle=nested)


@pytest.mark.parametrize("action", ["mutation", "continue", "close"])
def test_lifecycle_revokes_children(worker, action):
    scope, child = inspected(worker)
    if action == "mutation":
        call(worker, "set_variable", reference=scope, name="value", value="2")
    else:
        call(worker, action)
    with pytest.raises(ValueError):
        call(worker, "expand", handle=child)


@pytest.mark.parametrize(
    "args",
    [
        {"count": 65},
        {"count": 0},
        {"count": True},
        {"start": -1},
        {"start": True},
        {"handle": True},
    ],
)
def test_expansion_input_limits_are_strict(worker, args):
    _, child = inspected(worker)
    with pytest.raises(ValueError):
        call(worker, "expand", **{"handle": child, **args})


def test_adapter_ignoring_paging_cannot_expand_unbounded_output(worker):
    _, child = inspected(worker)
    worker.dap.children[100] = [variable(f"item{i}") for i in range(512)]
    assert len(call(worker, "expand", handle=child, count=8)["variables"]) == 8


def test_provenance_failure_prevents_child_reads_and_closes(worker):
    _, child = inspected(worker)
    backend = worker.dap
    worker.runtime.valid = False
    with pytest.raises(RuntimeError):
        call(worker, "expand", handle=child)
    assert backend.closed
    assert not any(
        op == "variables" and args["variablesReference"] == 100 for op, args in backend.calls
    )


def test_adapter_error_during_expansion_fails_closed(worker):
    _, child = inspected(worker)
    backend = worker.dap
    backend.fail_children = True
    with pytest.raises(TimeoutError):
        call(worker, "expand", handle=child)
    assert backend.closed and worker.session is None


def test_oversized_child_collection_is_rejected(worker):
    _, child = inspected(worker)
    worker.dap.children[100] = [variable(f"item{i}") for i in range(513)]
    with pytest.raises(ValueError):
        call(worker, "expand", handle=child)


def test_invalid_reinspection_also_revokes_child_handles(worker):
    _, child = inspected(worker)
    with pytest.raises(ValueError):
        call(worker, "inspect", frame_id=999)
    with pytest.raises(ValueError):
        call(worker, "expand", handle=child)


def test_mutation_rejection_revokes_child_handles(worker):
    _, child = inspected(worker)
    with pytest.raises(ValueError):
        call(worker, "set_variable", reference=999, name="value", value="2")
    with pytest.raises(ValueError):
        call(worker, "expand", handle=child)


def test_expansion_depth_limit_stops_issuing_handles(worker):
    _, child = inspected(worker)
    for ref in range(100, 105):
        worker.dap.children[ref] = [variable(f"node{ref}", "Node", ref + 1)]
    depths = 0
    while child and depths < 10:
        child = call(worker, "expand", handle=child)["variables"][0]["variablesReference"]
        depths += 1
    assert child == 0 and depths <= 4


def test_total_node_budget_cannot_be_reset_by_reexpanding_same_handle(worker):
    _, child = inspected(worker)
    worker.dap.children[100] = [variable(f"item{i}") for i in range(64)]
    returned = 0
    for _ in range(10):
        try:
            result = call(worker, "expand", handle=child)
        except ValueError:
            break
        returned += len(result["variables"])
    else:
        pytest.fail("Repeated expansion bypassed the per-inspection budget")
    assert returned <= 510  # Two variables were already returned by inspect.


@pytest.mark.parametrize(
    "field,value", [("line", 3), ("moduleId", "other"), ("instructionPointerReference", "0x1234")]
)
def test_expansion_requires_same_fresh_frame_identity(worker, field, value):
    _, child = inspected(worker)
    worker.dap.frames[0][field] = value
    with pytest.raises(ValueError, match="frame identity"):
        call(worker, "expand", handle=child)


@pytest.mark.parametrize("shape", ["missing", "duplicate"])
def test_expansion_requires_unique_fresh_scope(worker, shape):
    _, child = inspected(worker)
    request = worker.dap.request

    def changed(operation, args):
        result = request(operation, args)
        if operation == "scopes":
            result["scopes"] = [] if shape == "missing" else result["scopes"] * 2
        return result

    worker.dap.request = changed
    with pytest.raises(ValueError, match="scope"):
        call(worker, "expand", handle=child)


def test_paging_is_applied_locally_without_reusing_adapter_offsets(worker):
    _, child = inspected(worker)
    worker.dap.children[100] = [variable(f"item{i}") for i in range(10)]
    result = call(worker, "expand", handle=child, start=3, count=2)
    assert [v["name"] for v in result["variables"]] == ["item3", "item4"]


def test_expansion_revokes_mutable_root_scope_handles(worker):
    scope, child = inspected(worker)
    call(worker, "expand", handle=child)
    with pytest.raises(ValueError):
        call(worker, "set_variable", reference=scope, name="value", value="2")
    assert not any(op == "setVariable" for op, _ in worker.dap.calls)


def test_over_long_child_identities_keep_value_rules(tmp_path):
    """The placeholder for a child whose identity is too long still never shows a
    debugger 'no value' message, or an unverified value, as its value."""
    from types import SimpleNamespace

    long_type = "std::tuple<" + "int, " * 500 + "int>"
    child = {
        "name": "m",
        "type": long_type,
        "value": "<error: variable not available>",
        "variablesReference": 5,
        "evaluateName": "s.m",
    }
    instance = DebugWorker(tmp_path)
    instance.runtime = SimpleNamespace(reports_unavailable_as_text=True)
    [shown] = instance.expose_variables([child], {}, ("Locals",), (("s", "S"),))
    assert shown["value"] is None and shown["unavailable"] and shown["identity_truncated"]
    assert shown["variablesReference"] == 0 and shown["type"].endswith("…")
    instance.runtime = SimpleNamespace(view_variable=lambda variable, fetch: None)
    [shown] = instance.expose_variables(
        [{**child, "value": "Some(3)"}], {}, ("Locals",), (("s", "S"),)
    )
    assert shown["value"] == "<not shown: type name too long to verify>"
    # Root locals still fail closed.
    with pytest.raises(RuntimeError, match="identity exceeds limit"):
        instance.expose_variables([child], {}, ("Locals",), ())


def test_only_whether_a_child_is_evaluable_leaves_the_worker(tmp_path):
    instance = DebugWorker(tmp_path)
    [shown] = instance.expose_variables(
        [{"name": "'a'", "type": "str", "value": "'x'", "evaluateName": "d['a']"}],
        {},
        ("Locals",),
        (("d", "dict"),),
    )
    assert shown["evaluable"] is True and "d['a']" not in str(shown)
    [shown] = instance.expose_variables(
        [{"name": "'a'", "type": "str", "value": "'x'"}], {}, ("Locals",), (("d", "dict"),)
    )
    assert "evaluable" not in shown


class MemberBackend(Backend):
    """Applies setVariable to the listed child, as a debugger writing the field would."""

    def request(self, operation, args):
        if operation == "setVariable":
            self.calls.append((operation, deepcopy(args)))
            for child in self.children[args["variablesReference"]]:
                if child["name"] == args["name"]:
                    child["value"] = args["value"]
            return {"value": args["value"]}
        return super().request(operation, args)


class MemberRuntime(Runtime):
    def member_settable(self, variable, parent=None, proven=False):
        return "it is final" if variable["name"] == "nested" else None


@pytest.fixture
def member_worker(worker):
    worker.dap, worker.runtime = MemberBackend(), MemberRuntime()
    worker.capabilities = {"supports_set_variable": True, "supports_member_set": True}
    return worker


def test_a_java_bundle_without_the_field_guard_changes_only_locals(member_worker):
    # Older launchers let java-debug write a hiding static field in place of the listed one.
    member_worker.capabilities = {"supports_set_variable": True}
    _, child = inspected(member_worker)
    listed = call(member_worker, "expand", handle=child)["variables"]
    assert [v["settable"] for v in listed] == [False, False]
    _, child = inspected(member_worker)
    with pytest.raises(ValueError, match="predates field and element changes"):
        call(member_worker, "set_variable", handle=child, name="page", value="2")
    assert not any(op == "setVariable" for op, _ in member_worker.dap.calls)


def test_a_field_is_set_through_its_parents_handle_and_read_back(member_worker):
    _, child = inspected(member_worker)
    changed = call(member_worker, "set_variable", handle=child, name="page", value="2")
    assert changed == {"name": "page", "before": "1", "after": "2", "type": "int"}
    writes = [args for op, args in member_worker.dap.calls if op == "setVariable"]
    # The parent's own adapter reference, found again from the frame, never the handle.
    assert writes == [{"variablesReference": 100, "name": "page", "value": "2"}]
    # A change invalidates every handle.
    with pytest.raises(ValueError, match="stale"):
        call(member_worker, "expand", handle=child)


def test_field_rows_say_whether_set_can_change_them(member_worker):
    _, child = inspected(member_worker)
    listed = call(member_worker, "expand", handle=child)["variables"]
    assert {v["name"]: v["settable"] for v in listed} == {"page": True, "nested": False}


def test_members_the_runtime_refuses_are_never_written(member_worker):
    _, child = inspected(member_worker)
    # The whole selector, not the debugger's name alone (#261).
    with pytest.raises(ValueError, match="`request.nested` cannot be changed: it is final"):
        call(member_worker, "set_variable", handle=child, name="nested", value="2")
    _, child = inspected(member_worker)
    with pytest.raises(ValueError, match="'nope' is not a field or element"):
        call(member_worker, "set_variable", handle=child, name="nope", value="2")
    with pytest.raises(ValueError, match="stale expansion handle"):
        call(member_worker, "set_variable", handle=child, name="page", value="2")
    assert not any(op == "setVariable" for op, _ in member_worker.dap.calls)


def test_other_languages_change_only_root_locals(worker):
    _, child = inspected(worker)
    with pytest.raises(ValueError, match="own locals can be changed for this language"):
        call(worker, "set_variable", handle=child, name="page", value="2")
    assert not any(op == "setVariable" for op, _ in worker.dap.calls)


def test_an_unconfirmed_field_write_closes_the_session(worker):
    # The debugger reports success but the field still reads its old value.
    worker.runtime = MemberRuntime()
    worker.capabilities = {"supports_set_variable": True, "supports_member_set": True}
    _, child = inspected(worker)
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        call(worker, "set_variable", handle=child, name="page", value="2")
    assert worker.dap is None


def test_a_java_write_tells_the_launcher_what_is_left_of_the_pause_budget(member_worker):
    # #204 review round 2: the launcher's search for what refers to an array must end
    # within the session's pause budget (an attached JVM's 10 s).
    import time

    member_worker.runtime.pause_budget_writes = True
    _, child = inspected(member_worker)
    member_worker.max_pause = 10
    member_worker.stopped_at = time.monotonic() - 3
    call(member_worker, "set_variable", handle=child, name="page", value="2")
    [write] = [args for op, args in member_worker.dap.calls if op == "setVariable"]
    assert 6000 < write.pop("peekyPauseLeftMs") <= 7000
    assert write == {"variablesReference": 100, "name": "page", "value": "2"}
    # Without a budget, nothing is sent.
    _, child = inspected(member_worker)
    member_worker.max_pause = None
    call(member_worker, "set_variable", handle=child, name="page", value="3")
    writes = [args for op, args in member_worker.dap.calls if op == "setVariable"]
    assert len(writes) == 2 and writes[-1]["value"] == "3"
    assert "peekyPauseLeftMs" not in writes[-1]


class HolderBackend(MemberBackend):
    """A Java array `xs` among the roots, whose write the launcher may refuse."""

    def __init__(self):
        super().__init__()
        xs = {**variable("xs", "int[]", 300), "value": "int[2]@5", "indexedVariables": 2}
        self.roots.append(xs)
        self.children[300] = [variable("0"), variable("1")]
        self.refusal = None

    def request(self, operation, args):
        if operation == "setVariable" and self.refusal:
            from peeky.dap import RequestFailed

            self.calls.append((operation, deepcopy(args)))
            raise RequestFailed(self.refusal)
        return super().request(operation, args)


class HolderRuntime(MemberRuntime):
    """Asks what holds an array before marking its elements (Java's launcher, #261);
    ``settled``: whether every thread was suspended, so the verdict lasts the stop."""

    def __init__(self, held, settled=True):
        super().__init__()
        self.held, self.settled, self.asked = held, settled, []

    def array_holder(self, dap, array, pause_left_ms=None):
        self.asked.append(array["value"])
        return self.held, self.settled


def array_handle(worker):
    rows = call(worker, "inspect", frame_id=1)["scopes"][0]["variables"]
    return next(row["variablesReference"] for row in rows if row["name"] == "xs")


@pytest.mark.parametrize(
    "held", [None, "its array is held by java.util.Fake.a (the JDK's own state)"]
)
def test_an_elements_mark_follows_what_holds_its_array(member_worker, held):
    # #261: an element was marked [settable] by its path alone, then the launcher refused
    # the write (the array was in a list an unmodifiable view wraps).
    member_worker.dap, member_worker.runtime = HolderBackend(), HolderRuntime(held)
    handle = array_handle(member_worker)
    listed = call(member_worker, "expand", handle=handle)["variables"]
    assert [row["settable"] for row in listed] == [held is None] * 2
    assert [row.get("not_settable") for row in listed] == [held] * 2
    # Asked once per array at a stop, however many of its elements are listed or read.
    call(member_worker, "expand", handle=array_handle(member_worker), index=1)
    assert member_worker.runtime.asked == ["int[2]@5"]
    # Never for a field of an object.
    call(member_worker, "expand", handle=inspected(member_worker)[1])
    assert member_worker.runtime.asked == ["int[2]@5"]


def test_a_verdict_made_while_other_threads_run_is_asked_again(member_worker):
    # #261 review: a verdict cached for the stop went stale when a background thread
    # wrapped the array's list in an unmodifiable view: `get` said settable, `set` refused.
    from peeky.java import Unproven

    running = Unproven("other threads, the JVM's own included, are running; set checks again")
    member_worker.dap = HolderBackend()
    member_worker.runtime = HolderRuntime(running, settled=False)
    for _ in range(2):
        listed = call(member_worker, "expand", handle=array_handle(member_worker))["variables"]
        assert [row.get("maybe_settable") for row in listed] == [str(running)] * 2
        assert [row["settable"] for row in listed] == [False, False]
    # Asked at each listing, never kept.
    assert member_worker.runtime.asked == ["int[2]@5"] * 2
    # What another thread did since is seen at the next listing.
    member_worker.runtime.held = "its array is held by java.util.Fake.a (the JDK's own state)"
    listed = call(member_worker, "expand", handle=array_handle(member_worker))["variables"]
    assert [row.get("not_settable") for row in listed] == [member_worker.runtime.held] * 2


def test_an_element_whose_holder_is_not_known_is_maybe_settable(member_worker):
    # #261: where the launcher could not tell in time (or an older one cannot tell before
    # a change), neither mark is promised: the row says why, and `set` decides.
    from peeky.java import Unproven

    held = Unproven("Peeky could not tell within 500 ms; `set` searches again, for up to 2 s")
    member_worker.dap, member_worker.runtime = HolderBackend(), HolderRuntime(held)
    listed = call(member_worker, "expand", handle=array_handle(member_worker))["variables"]
    assert [row["settable"] for row in listed] == [False, False]
    assert [row["maybe_settable"] for row in listed] == [str(held)] * 2
    assert not any("not_settable" in row for row in listed)
    # And the write is still made, as the launcher allows it.
    call(member_worker, "set_variable", handle=array_handle(member_worker), name="1", value="2")
    assert [op for op, _ in member_worker.dap.calls].count("setVariable") == 1


def test_a_refused_member_write_names_the_whole_selector(member_worker):
    # #261: "The debugger refused to change '1'" named the element's index alone.
    member_worker.dap, member_worker.runtime = HolderBackend(), HolderRuntime(None)
    member_worker.dap.refusal = "the array is held by java.util.Fake.a, a JDK class's field"
    with pytest.raises(ValueError, match=r"refused to change `xs\[1\]` \(the array is held"):
        call(member_worker, "set_variable", handle=array_handle(member_worker), name="1", value="2")


def test_a_member_selector_is_written_as_a_read_names_it():
    from peeky.worker import member_selector

    assert member_selector([("ranking", "int[]")], "1") == "ranking[1]"
    assert member_selector([("this", "Runoff"), ("names", "String[]")], "0") == "this.names[0]"
    assert member_selector([("grid", "int[][]"), ("2", "int[]")], "3") == "grid[2][3]"
    assert member_selector([("s", "Pair"), ("in", "Inner")], "lo") == "s.in.lo"
    assert member_selector([("xs", "int [3]")], "[1]") == "xs[1]"


@pytest.mark.parametrize("args", [{}, {"reference": 1, "handle": 1}, {"handle": "1"}])
def test_set_variable_takes_a_reference_or_a_handle(member_worker, args):
    with pytest.raises(ValueError):
        call(member_worker, "set_variable", name="page", value="2", **args)
