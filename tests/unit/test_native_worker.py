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


@pytest.mark.parametrize("frame_id", [1, 2])
def test_module_mismatch_on_any_program_frame_closes_the_session(tmp_path, frame_id):
    worker = stopped(tmp_path)
    worker.runtime.module_valid = False
    with pytest.raises(RuntimeError, match="module"):
        inspect(worker, frame_id)
    assert worker.session is None


def test_only_caller_frames_outside_the_program_are_request_errors(tmp_path):
    def validate_stop(frame):
        if frame["id"] != 1:
            raise RuntimeError("Native stopped line does not match prepared symbols")

    worker = stopped(tmp_path)
    worker.runtime.root = "/prepared"
    worker.runtime.validate_stop = validate_stop
    # Frame 2 is in the program's own sources: a mismatch there is provenance.
    with pytest.raises(RuntimeError, match="does not match"):
        inspect(worker, 2)
    assert worker.session is None
    worker = stopped(tmp_path)
    worker.runtime.root = "/prepared"
    worker.runtime.validate_stop = validate_stop
    worker.dap.frames[1]["source"] = {"path": "/usr/lib/system/libdyld.dylib"}
    with pytest.raises(ValueError, match="not in the prepared program's sources"):
        inspect(worker, 2)
    assert worker.session == "session"
    assert inspect(worker, 1)["scopes"]


def test_inspections_name_the_receiver_only_for_cpp_and_java():
    from types import SimpleNamespace

    from peeky.java import JavaSession
    from peeky.native import NativeSession

    def receiver(language):
        target = SimpleNamespace(language=language) if language else SimpleNamespace()
        return NativeSession.implicit_receiver.fget(SimpleNamespace(target=target))

    assert (receiver("cpp"), receiver("c"), receiver(None)) == ("this", None, None)
    assert JavaSession.implicit_receiver == "this"


def test_the_receiver_reaches_the_inspection(tmp_path):
    worker = stopped(tmp_path)
    assert "receiver" not in inspect(worker)
    worker.runtime.implicit_receiver = "this"
    assert inspect(worker)["receiver"] == "this"


def test_native_integer_locals_take_values_within_their_own_type():
    from types import SimpleNamespace

    from peeky.native import INTEGER_RANGES, NativeSession

    session = SimpleNamespace(
        validate_value=lambda value: NativeSession.validate_value(None, value)
    )

    def change(kind, value):
        NativeSession.validate_variable(session, {"name": "n", "type": kind}, value)

    change("size_t", "18446744073709551615")
    change("std::int64_t", "-9223372036854775808")
    change("short", "-32768")
    with pytest.raises(ValueError, match=r"outside the range of unsigned short \(0 to 65535\)"):
        change("unsigned short", "65536")
    for kind in ("char", "unsigned char", "bool", "const int", "float", "int *"):
        assert kind not in INTEGER_RANGES
        with pytest.raises(ValueError, match="Only writable integer locals of these types"):
            change(kind, "1")
    with pytest.raises(ValueError, match="bounded decimal integer literals"):
        change("long", "123456789012345678901")


def test_inspections_mark_settable_root_locals_only(tmp_path):
    worker = stopped(tmp_path)
    worker.runtime.settable = lambda variable, frame: variable["name"] == "value"
    snapshot = inspect(worker)
    marks = {v["name"]: v.get("settable") for s in snapshot["scopes"] for v in s["variables"]}
    assert marks and marks.get("value") is True
    assert all(mark is False for name, mark in marks.items() if name != "value")
    # Optimized builds refuse every change: nothing is marked.
    worker.optimized = lambda: "-O2"
    snapshot = inspect(worker)
    assert all(v.get("settable") is False for s in snapshot["scopes"] for v in s["variables"])


def test_native_writes_are_read_back_and_never_reported_otherwise():
    from types import SimpleNamespace

    from peeky.native import NativeSession

    session = SimpleNamespace()
    confirm = NativeSession.confirm_mutation
    confirm(session, "n", "+5", "2", "5")  # canonical match
    with pytest.raises(ValueError, match="did not change 'hi' \\(still 2\\)"):
        confirm(session, "hi", "17", "2", "2")
    with pytest.raises(RuntimeError, match="stored 16777216 in 'f', not 16777217"):
        confirm(session, "f", "16777217", "1", "16777216")
    assert NativeSession.mutation_literal(session, "+5") == "5"
    assert NativeSession.mutation_literal(session, "-0") == "0"


def test_values_without_a_memory_location_are_never_settable(tmp_path):
    worker = stopped(tmp_path)
    worker.runtime.requires_memory_location = True
    worker.capabilities["supports_read_memory"] = True
    assert worker.has_memory_location({"memoryReference": "0x10"})
    assert not worker.has_memory_location({"name": "hi"})
    worker.capabilities["supports_read_memory"] = False  # LLVM 18: nothing to go on
    assert worker.has_memory_location({"name": "hi"})
