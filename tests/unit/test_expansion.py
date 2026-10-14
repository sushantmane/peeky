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
