"""Worker enforcement of native scope ownership, typed writes and module guards."""

from copy import deepcopy

import pytest

from peeky.adapters import PythonAdapter
from peeky.worker import DebugWorker


class Backend:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.variable = {"name": "value", "type": "int", "value": "1", "variablesReference": 0}
        self.duplicates = False
        self.frames = [
            {"id": 1, "line": 2, "source": {"path": "/prepared/main.c"}, "moduleId": "built"},
            {"id": 2, "line": 3, "source": {"path": "/prepared/main.c"}, "moduleId": "built"},
        ]

    def request(self, operation, args):
        self.calls.append((operation, deepcopy(args)))
        if operation == "stackTrace":
            return {"stackFrames": self.frames}
        if operation == "scopes":
            # LLDB reuses this raw locals reference across selected frames.
            return {
                "scopes": [
                    {"name": "Locals", "presentationHint": "locals", "variablesReference": 7},
                    {"name": "Globals", "presentationHint": "globals", "variablesReference": 9},
                ]
            }
        if operation == "variables":
            assert args["variablesReference"] == 7
            return {"variables": [deepcopy(self.variable)] * (2 if self.duplicates else 1)}
        if operation == "setVariable":
            assert args["variablesReference"] == 7
            self.variable["value"] = args["value"]
            return {"value": args["value"]}
        if operation == "continue":
            return {}
        raise AssertionError(operation)

    def close(self):
        self.closed = True


class Runtime:
    def __init__(self):
        self.validated = []
        self.module_valid = True
        self.closed = False

    def assert_current(self):
        pass

    def validate_stop(self, frame):
        assert frame["source"]["path"] == "/prepared/main.c"

    def validate_debugger(self, dap, frame):
        self.validated.append(frame["id"])
        if not self.module_valid:
            raise RuntimeError("Loaded module identity mismatch")

    def local_scopes(self, scopes):
        return PythonAdapter().local_scopes(scopes)

    def validate_value(self, value):
        assert value == "2"

    def validate_variable(self, variable, value):
        if variable["type"] != "int" or variable["variablesReference"] != 0:
            raise ValueError("Not an eligible observed scalar")

    def close(self):
        self.closed = True


def stopped(tmp_path):
    worker = DebugWorker(tmp_path)
    worker.dap = Backend()
    worker.runtime = Runtime()
    worker.session, worker.stop_id, worker.state = "session", "stop", "stopped"
    worker.thread_id = 1
    worker.capabilities = {"supports_set_variable": True}
    return worker


def inspect(worker, frame_id=1):
    return worker.dispatch(
        "inspect", {"session_id": "session", "stop_id": "stop", "frame_id": frame_id}
    )


def mutate(worker, reference, name="value"):
    return worker.dispatch(
        "set_variable",
        {
            "session_id": "session",
            "stop_id": "stop",
            "reference": reference,
            "name": name,
            "value": "2",
        },
    )


def test_native_mutation_rechecks_module_and_translates_owned_scope(tmp_path):
    worker = stopped(tmp_path)
    snapshot = inspect(worker)
    assert len(snapshot["scopes"]) == 1
    handle = snapshot["scopes"][0]["reference"]
    result = mutate(worker, handle)
    assert result["before"] == "1" and result["after"] == "2"
    assert worker.runtime.validated == [1, 1]
    assert [args for op, args in worker.dap.calls if op == "setVariable"] == [
        {"variablesReference": 7, "name": "value", "value": "2"}
    ]


def test_module_mismatch_rejected_before_inspection_exposes_values(tmp_path):
    worker = stopped(tmp_path)
    runtime, backend = worker.runtime, worker.dap
    runtime.module_valid = False
    with pytest.raises(RuntimeError, match="module"):
        inspect(worker)
    assert not any(op in {"variables", "scopes"} for op, _ in backend.calls)
    assert backend.closed and runtime.closed
    assert worker.session is None


def test_module_changed_after_inspection_blocks_write_and_closes(tmp_path):
    worker = stopped(tmp_path)
    handle = inspect(worker)["scopes"][0]["reference"]
    runtime, backend = worker.runtime, worker.dap
    runtime.module_valid = False
    with pytest.raises(RuntimeError, match="module"):
        mutate(worker, handle)
    assert not any(op == "setVariable" for op, _ in backend.calls)
    assert backend.closed and runtime.closed
    assert worker.session is None


@pytest.mark.parametrize("kind", ["int *", "int[3]", "struct Value"])
def test_fresh_variable_type_blocks_pointer_array_aggregate_write(tmp_path, kind):
    worker = stopped(tmp_path)
    handle = inspect(worker)["scopes"][0]["reference"]
    worker.dap.variable.update(type=kind, variablesReference=11)
    with pytest.raises(ValueError, match="scalar"):
        mutate(worker, handle)
    assert not any(op == "setVariable" for op, _ in worker.dap.calls)


@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_or_ambiguous_name_rejected_before_write(tmp_path, duplicate):
    worker = stopped(tmp_path)
    handle = inspect(worker)["scopes"][0]["reference"]
    worker.dap.duplicates = duplicate
    with pytest.raises(ValueError, match="exactly one"):
        mutate(worker, handle, "value" if duplicate else "not_observed")
    assert not any(op == "setVariable" for op, _ in worker.dap.calls)


@pytest.mark.parametrize("frame_id", [1, 2])
def test_reinspect_revokes_previous_scope_even_if_adapter_reuses_reference(tmp_path, frame_id):
    worker = stopped(tmp_path)
    old = inspect(worker)["scopes"][0]["reference"]
    new = inspect(worker, frame_id)["scopes"][0]["reference"]
    assert old != new
    with pytest.raises(ValueError, match="scope"):
        mutate(worker, old)
    assert not any(op == "setVariable" for op, _ in worker.dap.calls)
    mutate(worker, new)
    assert worker.runtime.validated[-1] == frame_id


def test_uninspected_scope_rejected_without_fetching_variables(tmp_path):
    worker = stopped(tmp_path)
    inspect(worker)
    before = len(worker.dap.calls)
    with pytest.raises(ValueError, match="scope"):
        mutate(worker, 987654)
    assert len(worker.dap.calls) == before


def test_continue_revokes_native_scope_and_frame_binding(tmp_path):
    worker = stopped(tmp_path)
    handle = inspect(worker)["scopes"][0]["reference"]
    worker.dispatch("continue", {"session_id": "session", "stop_id": "stop"})
    assert not worker.references and not worker.reference_frames
    with pytest.raises(ValueError, match="stop"):
        mutate(worker, handle)
