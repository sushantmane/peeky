import json

import pytest

from peeky.adapters import PythonAdapter
from peeky.worker import DebugWorker, make_server


class Adapter:
    closed = False
    calls = []

    def request(self, command, args):
        self.calls.append(command)
        if command == "variables":
            return {"variables": [{"name": "value", "value": "1"}]}
        raise TimeoutError("reply lost after operation applied")

    def close(self):
        self.closed = True

    def wait_event(self, names, cursor, timeout):
        assert timeout <= 2
        raise TimeoutError("poll expired")


def stopped_worker(tmp_path):
    worker = DebugWorker(tmp_path)
    worker.dap = Adapter()
    worker.session = "session"
    worker.state = "stopped"
    worker.stop_id = "stop"
    worker.thread_id = 1
    worker.capabilities = {"supports_set_variable": True}
    worker.references = {7}
    worker.reference_adapter = {7: 7}
    return worker


@pytest.mark.parametrize(
    "operation,extra",
    [("continue", {}), ("set_variable", {"reference": 7, "name": "value", "value": "2"})],
)
def test_uncertain_mutation_invalidates_session(tmp_path, operation, extra):
    worker = stopped_worker(tmp_path)
    adapter = worker.dap
    with pytest.raises(TimeoutError):
        worker.dispatch(operation, {"session_id": "session", "stop_id": "stop", **extra})
    assert adapter.closed
    assert worker.dap is None and worker.session is None and worker.stop_id is None
    with pytest.raises(ValueError, match="session"):
        worker.dispatch("continue", {"session_id": "session", "stop_id": "stop"})


def test_wait_poll_preserves_session(tmp_path):
    worker = stopped_worker(tmp_path)
    worker.state = "running"
    worker.cursor = 0
    assert worker.dispatch("wait", {"session_id": "session", "timeout": 20}) == {
        "state": "running",
        "timed_out": True,
    }
    assert worker.session == "session"


def test_report_requires_current_session(tmp_path):
    worker = stopped_worker(tmp_path)
    with pytest.raises(ValueError, match="session"):
        worker.dispatch("report", {})


def test_unsupported_mutation_rejected_before_adapter(tmp_path):
    worker = stopped_worker(tmp_path)
    worker.capabilities = {"supports_set_variable": False}
    worker.dap.calls = []
    with pytest.raises(ValueError, match="does not support"):
        worker.dispatch(
            "set_variable",
            {"session_id": "session", "stop_id": "stop", "reference": 7, "name": "x", "value": "2"},
        )
    assert worker.dap.calls == []


@pytest.mark.parametrize(
    "operation,args",
    [
        ("launch", {"program": "a.py", "breakpoints": [{}]}),
        ("launch", {"program": "a.py", "breakpoints": [{"line": True}]}),
        ("launch", {"program": "a.py", "breakpoints": [{"line": 1, "condition": []}]}),
        ("wait", {"timeout": float("nan")}),
        ("wait", {"timeout": "2"}),
        ("set_variable", {"reference": [], "name": "x", "value": "3"}),
        ("inspect", {"frame_id": []}),
    ],
)
def test_invalid_input_rejected_before_adapter(tmp_path, operation, args):
    with pytest.raises(ValueError):
        DebugWorker(tmp_path).dispatch(operation, args)


def test_server_rejects_empty_token(tmp_path):
    with pytest.raises(ValueError, match="Token"):
        make_server(tmp_path, "")


def test_symlink_cannot_escape_root(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.py")
    outside.write_text("pass")
    try:
        (tmp_path / "escape.py").symlink_to(outside)
        with pytest.raises(ValueError, match="inside"):
            DebugWorker(tmp_path).path("escape.py")
    finally:
        outside.unlink()


def test_failed_adapter_spawn_cleans_prepared_runtime(tmp_path, monkeypatch):
    closed = []

    class Runtime(PythonAdapter):
        def close(self):
            closed.append(True)

    def fail_spawn(*args, **kwargs):
        raise OSError("adapter missing")

    monkeypatch.setattr("peeky.worker.DAP", fail_spawn)
    (tmp_path / "target.py").write_text("pass\n")
    worker = DebugWorker(tmp_path, adapter=Runtime())
    with pytest.raises(OSError, match="adapter missing"):
        worker.dispatch("launch", {"program": "target.py"})
    assert closed == [True]
    assert worker.runtime is None and worker.session is None


def test_provenance_failure_prevents_mutation_and_cleans_session(tmp_path):
    closed = []

    class Runtime(PythonAdapter):
        def assert_current(self):
            raise RuntimeError("Prepared build provenance mismatch")

        def close(self):
            closed.append(True)

    worker = stopped_worker(tmp_path)
    worker.runtime = Runtime()
    backend = worker.dap
    backend.calls = []
    with pytest.raises(RuntimeError, match="provenance"):
        worker.dispatch(
            "set_variable",
            {
                "session_id": "session",
                "stop_id": "stop",
                "reference": 7,
                "name": "value",
                "value": "2",
            },
        )
    assert backend.calls == [] and backend.closed
    assert closed == [True]
    assert worker.runtime is None and worker.session is None


@pytest.mark.parametrize("field,value", [("arguments", "hello"), ("sources", {"Main.java": True})])
def test_java_cli_rejects_non_array_inputs_before_starting_server(
    tmp_path, monkeypatch, capsys, field, value
):
    from peeky.worker import main

    config = {"bundle": "adapter", "sources": ["Main.java"], "main_class": "Main"}
    config[field] = value
    path = tmp_path / "target.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("DEBUG_TOKEN", "test-token-at-least-sixteen")
    monkeypatch.setattr("sys.argv", ["peeky-worker", "--java-config", str(path)])
    monkeypatch.setattr("peeky.worker.make_server", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "JSON arrays" in capsys.readouterr().err


class CrashStop:
    """A stop whose stack cannot be read (e.g. a native signal stop)."""

    closed = False

    def wait_event(self, names, cursor, timeout):
        import time

        body = {"reason": "signal", "threadId": 3}
        return {"event": "stopped", "cursor": 1, "received": time.monotonic(), "body": body}

    def request(self, command, args, timeout=None):
        raise RuntimeError("stack unavailable at crash")

    def close(self, *args, **kwargs):
        self.closed = True


def test_location_read_failure_never_closes_the_session(tmp_path):
    worker = DebugWorker(tmp_path)
    worker.dap = CrashStop()
    worker.session, worker.state, worker.cursor = "session", "running", 0
    state = worker.dispatch("wait", {"session_id": "session", "timeout": 1})
    assert state["state"] == "stopped" and "location" not in state
    assert worker.dap is not None and not worker.dap.closed and worker.session == "session"
    again = worker.dispatch("wait", {"session_id": "session", "timeout": 1})
    assert again["stop_id"] == state["stop_id"]


class QuietDap:
    """An adapter that resumed the program but never reports its exit (java-debug #27)."""

    def __init__(self, threads, pid=None, late_event=None, fail=None):
        self.threads, self.calls, self.closed = threads, [], False
        self.debuggee_pid, self.late_event, self.fail = pid, late_event, fail

    def wait_event(self, names, cursor, timeout):
        if timeout == 0 and self.late_event:
            return self.late_event
        raise TimeoutError("poll expired")

    def request(self, command, args, timeout=None):
        self.calls.append(command)
        if self.fail:
            raise self.fail
        return {"threads": self.threads} if self.threads is not None else {}

    def close(self, *args, **kwargs):
        self.closed = True


class JavaLike:
    liveness_by_threads = True

    def assert_current(self):
        pass

    def close(self):
        pass


def quiet_worker(tmp_path, dap, runtime=None):
    import time

    worker = DebugWorker(tmp_path)
    worker.dap, worker.runtime = dap, runtime or JavaLike()
    worker.session, worker.state, worker.cursor = "session", "running", 0
    worker.resumed_at = time.monotonic() - 5
    return worker


def poll(worker):
    worker.liveness_checked = 0.0  # as if two seconds passed since the last check
    return worker.dispatch("wait", {"session_id": "session", "timeout": 0.1})


@pytest.mark.parametrize("threads", [None, []])
def test_one_empty_threads_reply_is_a_hint_two_are_an_inferred_exit(tmp_path, threads):
    worker = quiet_worker(tmp_path, QuietDap(threads))
    assert poll(worker) == {"state": "running", "timed_out": True}
    state = poll(worker)
    assert state["state"] == "terminated" and state["exit_inferred"] is True
    assert worker.dap.calls == ["threads", "threads"]
    assert any(entry["operation"] == "exit_inferred" for entry in worker.audit)


def test_live_threads_reset_the_count(tmp_path):
    worker = quiet_worker(tmp_path, QuietDap([]))
    poll(worker)
    worker.dap.threads = [{"id": 1}]
    poll(worker)
    worker.dap.threads = []
    assert poll(worker)["state"] == "running"


def test_with_a_known_pid_only_the_jvm_process_being_gone_proves_exit(tmp_path):
    import os
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    worker = quiet_worker(tmp_path, QuietDap([], pid=child.pid))
    assert poll(worker)["exit_inferred"] is True
    alive = quiet_worker(tmp_path, QuietDap([], pid=os.getpid()))
    for _ in range(4):
        assert poll(alive)["state"] == "running"


def test_empty_replies_never_carry_over_into_the_next_session(tmp_path):
    worker = quiet_worker(tmp_path, QuietDap([]))
    assert poll(worker)["state"] == "running"
    assert worker.empty_thread_replies == 1
    worker.close()
    assert worker.empty_thread_replies == 0
    # A new session on the same worker needs two fresh empty replies again.
    worker.dap, worker.runtime = QuietDap([]), JavaLike()
    worker.session, worker.state, worker.resumed_at = "session", "running", 0.0
    assert poll(worker)["state"] == "running"


@pytest.mark.parametrize("failure", [TimeoutError("slow"), RuntimeError("error reply")])
def test_a_slow_or_failed_check_means_still_running_and_never_closes(tmp_path, failure):
    worker = quiet_worker(tmp_path, QuietDap([], fail=failure))
    for _ in range(3):
        assert poll(worker) == {"state": "running", "timed_out": True}
    assert worker.dap is not None and not worker.dap.closed and worker.session == "session"


def test_a_real_event_arriving_during_the_check_wins(tmp_path):
    late = {"event": "stopped", "cursor": 7, "body": {"reason": "breakpoint", "threadId": 1}}
    worker = quiet_worker(tmp_path, QuietDap([], late_event=late))
    poll(worker)
    state = poll(worker)
    assert state["state"] == "stopped" and "exit_inferred" not in state
    assert worker.cursor == 7


def test_exit_is_never_inferred_for_attach_other_adapters_or_right_after_resuming(tmp_path):
    import time

    from peeky.java_attach import JavaAttachSession

    assert JavaAttachSession.liveness_by_threads is False
    for runtime, resumed in ((PythonAdapter(), 5), (JavaLike(), 0)):
        worker = quiet_worker(tmp_path, QuietDap(None), runtime)
        worker.resumed_at = time.monotonic() - resumed
        state = poll(worker)
        assert state["state"] == "running" and worker.dap.calls == []


@pytest.mark.parametrize(
    "value, unavailable",
    [
        ("<error: variable not available>", True),
        ("<variable not available>", True),
        ("<optimized out>", True),
        ("<value optimized out>", True),
        ("<unavailable>", True),
        ("<error: could not evaluate DW_OP_entry_value: no parent function>", True),
        ("0", False),
        ('"<optimized out>"', False),
        ("<not a placeholder>", False),
        ("", False),
    ],
)
def test_debugger_placeholders_are_recognized_and_never_values(value, unavailable):
    from peeky.worker import is_unavailable

    assert is_unavailable(value) is unavailable


class LldbLike(PythonAdapter):
    reports_unavailable_as_text = True


def test_python_values_that_look_like_placeholders_stay_values(tmp_path):
    worker = stopped_worker(tmp_path)
    exposed = worker.expose_variables(
        [{"name": "x", "value": "<error: user repr>", "type": "Thing"}], {"id": 1}, ("L", None), ()
    )
    assert "unavailable" not in exposed[0] and exposed[0]["value"] == "<error: user repr>"


def test_unavailable_locals_are_flagged_and_cannot_be_set(tmp_path):
    worker = stopped_worker(tmp_path)
    worker.runtime = LldbLike()
    exposed = worker.expose_variables(
        [{"name": "a", "value": "<error: variable not available>", "type": "int"}],
        {"id": 1},
        ("Locals", None),
        (),
    )
    assert exposed[0]["unavailable"] is True and exposed[0]["value"] is None
    assert exposed[0]["reason"] == "error: variable not available"

    class Placeholder(Adapter):
        def request(self, command, args):
            return {"variables": [{"name": "value", "value": "<optimized out>"}]}

    worker.dap = Placeholder()
    with pytest.raises(ValueError, match="has no readable value here"):
        worker.dispatch(
            "set_variable",
            {
                "session_id": "session",
                "stop_id": "stop",
                "reference": 7,
                "name": "value",
                "value": "2",
            },
        )
    from peeky.errors import classify

    assert classify(ValueError("'a' has no readable value here (x)"))["code"] == "MUTATION_REJECTED"
    refused = classify(ValueError("Changing locals is refused in an optimized build (-O2): x"))
    assert refused["code"] == "MUTATION_REJECTED"


class OptimizedRuntime(LldbLike):
    @property
    def target_identity(self):
        return {"optimization": "O2"}


def test_optimized_builds_refuse_every_change_and_are_marked(tmp_path):
    worker = stopped_worker(tmp_path)
    worker.runtime = OptimizedRuntime()
    assert worker.optimized() == "-O2"
    with pytest.raises(ValueError, match="refused in an optimized build"):
        worker.dispatch(
            "set_variable",
            {
                "session_id": "session",
                "stop_id": "stop",
                "reference": 7,
                "name": "value",
                "value": "2",
            },
        )
    worker.runtime = LldbLike()
    assert worker.optimized() is None


def test_unreadable_values_in_unoptimized_builds_are_not_called_optimized_out():
    from peeky.probe import _capture

    class Snapshot:
        def __init__(self, optimized):
            self.optimized = optimized

        def dispatch(self, operation, args):
            variable = {
                "name": "p",
                "value": None,
                "unavailable": True,
                "reason": "error: parent is NULL",
            }
            snapshot = {"frames": [{"line": 3}], "scopes": [{"name": "L", "variables": [variable]}]}
            if self.optimized:
                snapshot["optimized"] = "-O2"
            return snapshot

    plain = _capture(Snapshot(False), {}, ("p",), 1, "observed")[0]
    assert "could not read a value" in plain["error"] and "optimized" not in plain["error"]
    assert "parent is NULL" in plain["error"]
    optimized = _capture(Snapshot(True), {}, ("p",), 1, "observed")[0]
    assert "optimized out" in optimized["error"]


def test_activity_is_stamped_when_a_long_operation_ends(tmp_path, monkeypatch):
    import time

    worker = stopped_worker(tmp_path)
    worker.last_used = 0.0

    def slow(operation, args):
        time.sleep(0.3)
        return {"state": "stopped"}

    monkeypatch.setattr(worker, "_dispatch", slow)
    worker.dispatch("wait", {"session_id": "session", "timeout": 1})
    assert time.monotonic() - worker.last_used < 0.1
    before = worker.last_used
    with pytest.raises(ValueError, match="stale session_id"):
        worker.dispatch("wait", {"session_id": "other", "timeout": 1})
    assert worker.last_used == before  # rejected requests never keep a session alive


def test_locations_show_the_users_source_for_a_prepared_build(tmp_path):
    from types import SimpleNamespace

    from peeky.worker import _display_path

    workspace, source = tmp_path / "peeky-build-x", tmp_path / "crate"
    (workspace / "src").mkdir(parents=True)
    (source / "src").mkdir(parents=True)
    (source / "src/main.rs").write_text("fn main() {}\n")
    runtime = SimpleNamespace(prepared=SimpleNamespace(workspace=workspace, source_root=source))
    shown = _display_path(runtime, str(workspace / "src/main.rs"))
    assert shown == str(source / "src/main.rs")
    # Anything outside the workspace (std, libc) and unprepared targets are unchanged.
    assert _display_path(runtime, "/rustc/abc/library/core/src/lib.rs").startswith("/rustc/")
    assert _display_path(SimpleNamespace(), "/w/a.py") == "/w/a.py"
    assert _display_path(None, "/w/a.py") == "/w/a.py"


def test_frames_without_line_information_are_never_shown_as_user_files(tmp_path):
    from types import SimpleNamespace

    from peeky.worker import _display_path, _in_program_sources

    workspace, source = tmp_path / "peeky-build-x", tmp_path / "crate"
    (workspace / "src").mkdir(parents=True)
    (source / "src").mkdir(parents=True)
    (workspace / "src/main.rs").write_text("fn main() {}\n")
    (source / "src/main.rs").write_text("fn main() {}\n")
    (workspace / "target").mkdir()
    (workspace / "target/bin").write_text("")
    prepared = SimpleNamespace(workspace=workspace, source_root=source)
    runtime = SimpleNamespace(prepared=prepared, lines={"src/main.rs": [1]})
    synthetic = f"{workspace}/target/bin`main"
    assert _display_path(runtime, synthetic) == synthetic
    assert _display_path(runtime, str(workspace / "target/bin")) == str(workspace / "target/bin")
    frame = {"source": {"path": str(workspace / "src/main.rs")}}
    assert _in_program_sources(runtime, frame, tmp_path)
    for path in (synthetic, str(workspace / "target/bin"), "/rustc/abc/library/core/src/lib.rs"):
        assert not _in_program_sources(runtime, {"source": {"path": path}}, tmp_path)


def test_a_step_the_debugger_refuses_keeps_the_stop_and_session(tmp_path):
    from types import SimpleNamespace

    from peeky.dap import RequestFailed
    from peeky.worker import DebugWorker

    class Refusing:
        def request(self, command, arguments=None, **_):
            assert command == "stepOut"
            raise RequestFailed("Cannot step out of the outermost frame")

        def close(self):
            raise AssertionError("the session must not be closed")

    worker = DebugWorker(tmp_path)
    worker.dap, worker.session, worker.thread_id = Refusing(), "s1", 1
    worker.runtime = SimpleNamespace(assert_current=lambda: None)
    worker.state, worker.stop_id = "stopped", "stop1"
    with pytest.raises(ValueError, match="refused to step out"):
        worker.dispatch("step", {"session_id": "s1", "stop_id": "stop1", "kind": "out"})
    assert worker.state == "stopped" and worker.stop_id == "stop1" and worker.dap is not None


class MemoryDap:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.requests = reply, error, []

    def request(self, command, arguments=None, **_):
        self.requests.append((command, arguments))
        if self.error:
            raise self.error
        return self.reply


@pytest.mark.parametrize(
    "capabilities, reference, count, dap, message",
    [
        ({}, "0x10", 8, MemoryDap({"data": "AAAAAAAAAAA="}), "cannot read memory"),
        ({"supports_read_memory": True}, "10", 8, MemoryDap(), "Invalid memory reference"),
        ({"supports_read_memory": True}, "0x10", 0, MemoryDap(), "exceeds limit"),
        ({"supports_read_memory": True}, "0x10", 4097, MemoryDap(), "exceeds limit"),
        ({"supports_read_memory": True}, "0x10", 8, MemoryDap({"data": "AAAA"}), "Partial"),
        (
            {"supports_read_memory": True},
            "0x10",
            8,
            MemoryDap({"data": "AAAAAAAAAAA=", "unreadableBytes": 2}),
            "Partial",
        ),
        ({"supports_read_memory": True}, "0x10", 8, MemoryDap({"data": "%%%"}), "Malformed"),
    ],
)
def test_memory_reads_for_value_views_are_bounded_and_exact(
    tmp_path, capabilities, reference, count, dap, message
):
    import time as clock

    from peeky.worker import DebugWorker

    worker = DebugWorker(tmp_path)
    worker.capabilities, worker.dap = capabilities, dap
    worker.view_deadline, worker.view_fetches, worker.view_bytes = clock.monotonic() + 5, 0, 0
    with pytest.raises(ValueError, match=message):
        worker.view_read(reference, count)


def test_a_refused_memory_read_is_a_value_error_not_a_session_failure(tmp_path):
    import time as clock

    from peeky.dap import RequestFailed
    from peeky.worker import DebugWorker

    worker = DebugWorker(tmp_path)
    worker.capabilities = {"supports_read_memory": True}
    worker.dap = MemoryDap(error=RequestFailed("memory read failed"))
    worker.view_deadline, worker.view_fetches, worker.view_bytes = clock.monotonic() + 5, 0, 0
    with pytest.raises(ValueError, match="refused the memory read"):
        worker.view_read("0x10", 8)
    worker.dap = MemoryDap({"data": "AQAAAAAAAAA="})
    assert worker.view_read("0x10", 8) == (1).to_bytes(8, "little")


def test_decoded_elements_get_worker_owned_handles_that_never_authorize_writes(tmp_path):
    from types import SimpleNamespace

    from peeky.worker import DebugWorker

    elements = [{"name": f"[{i}]", "type": "i32", "value": str(v)} for i, v in enumerate((5, 1))]
    runtime = SimpleNamespace(
        assert_current=lambda: None,
        view_variable=lambda variable, fetch, read, **_: {
            "value": "[5, 1]",
            "rust_view": "memory",
            "length": 2,
            "children": elements,
        },
    )
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.state, worker.stop_id = runtime, "stopped", "stop1"
    [shown] = worker.expose_variables(
        [{"name": "ordered", "type": "Vec", "value": "raw", "variablesReference": 7}],
        {"id": 1},
        ("Locals",),
        (),
    )
    assert shown["value"] == "[5, 1]" and shown["length"] == 2 and shown["view"] == "memory"
    handle = shown["variablesReference"]
    assert handle and handle not in worker.references
    page = worker.expand({"stop_id": "stop1", "handle": handle, "start": 1}, runtime)
    assert page["variables"] == [
        {"name": "[1]", "type": "i32", "value": "1", "variablesReference": 0, "view": "memory"}
    ]
    assert page["truncated"] is False


def test_a_tainted_locals_children_are_tainted_and_marked(tmp_path):
    # #108: what the runtime says of a top-level aggregate passes to its children, and to
    # theirs; a pointer inside shows only its address; scalars are marked stale.
    from types import SimpleNamespace

    from peeky.worker import DebugWorker

    seen = []

    def view_variable(variable, fetch, read, *, top=False, taint=None, siblings=None, **_):
        seen.append((variable["name"], top, taint))
        if taint and variable["type"].endswith("*"):
            return {"value": "0x10", "rust_view": "unproven"}
        return None

    levels = {"h": "stale", "c": "pointers", "f": None}
    runtime = SimpleNamespace(
        assert_current=lambda: None,
        view_variable=view_variable,
        aggregate_taint=lambda variable, frame, innermost=False: levels[variable["name"]],
    )
    children = {
        7: [
            {"name": "b", "type": "Pt *", "value": "0x10", "variablesReference": 9},
            {"name": "n", "type": "long", "value": "9", "variablesReference": 0},
            {"name": "v", "type": "Vec", "value": "Vec @ 0x20", "variablesReference": 8},
        ],
        8: [{"name": "len", "type": "unsigned long", "value": "3", "variablesReference": 0}],
    }
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.state, worker.stop_id = runtime, "stopped", "stop1"
    worker.innermost_frame = 1
    worker.walk = lambda record, runtime, trail=None: (record[0], record[1], record[2], 7, None)
    worker.bounded_variables = lambda reference, indexed=None: children[reference]
    top = worker.expose_variables(
        [
            {"name": name, "type": "S", "value": "S @ 0x30", "variablesReference": 7}
            for name in levels
        ],
        {"id": 1},
        ("Locals",),
        (),
    )
    assert [shown.get("stale") for shown in top] == [True, None, None]
    assert ("h", True, None) in seen
    stale = worker.expand({"stop_id": "stop1", "handle": top[0]["variablesReference"]}, runtime)
    b, n, v = stale["variables"]
    assert b == {
        "name": "b",
        "type": "Pt *",
        "value": "0x10",
        "variablesReference": 0,
        "view": "unproven",
    }
    assert n["value"] == "9" and n["stale"] and v["stale"] and v["variablesReference"]
    worker.walk = lambda record, runtime, trail=None: (record[0], record[1], record[2], 8, None)
    [length] = worker.expand({"stop_id": "stop1", "handle": v["variablesReference"]}, runtime)[
        "variables"
    ]
    assert length["value"] == "3" and length["stale"] and ("len", False, "stale") in seen
    worker.walk = lambda record, runtime, trail=None: (record[0], record[1], record[2], 7, None)
    # A caller's proven local: its fields read, its pointers do not.
    b, n, v = worker.expand({"stop_id": "stop1", "handle": top[1]["variablesReference"]}, runtime)[
        "variables"
    ]
    assert b["view"] == "unproven" and "stale" not in n and "stale" not in v
    # A proven one reads whole.
    b, n, v = worker.expand({"stop_id": "stop1", "handle": top[2]["variablesReference"]}, runtime)[
        "variables"
    ]
    assert "view" not in b and b["variablesReference"] and "stale" not in n


def test_what_is_read_through_a_noted_local_carries_its_note(tmp_path):
    # #148: a root shown with a note (#140) passed it to nothing read through it; its
    # fields, their fields and a decoded preview's elements now carry it, said as read
    # through the root, and none is settable where the root's change is refused.
    from types import SimpleNamespace

    from peeky.worker import DebugWorker

    class Noted(str):
        def __new__(cls, note, refuses):
            made = super().__new__(cls, note)
            made.note, made.refuses = note, refuses
            return made

    notes = {"q": Noted("may be leftovers", True), "u": Noted("not judged", False), "k": None}
    preview = [{"name": "x", "type": "long", "value": "1"}]
    runtime = SimpleNamespace(
        assert_current=lambda: None,
        not_yet_declared=lambda variable, frame, **_: notes[variable["name"]],
        member_settable=lambda variable, **_: None,
        view_variable=lambda variable, fetch, read, **_: (
            {"value": "{x = 1}", "rust_view": "memory", "children": preview}
            if variable["type"] == "View"
            else None
        ),
        view_writable=lambda view: False,
    )
    children = {
        7: [
            {"name": "x", "type": "long", "value": "9", "variablesReference": 0},
            {"name": "inner", "type": "pt", "value": "pt @ 0x10", "variablesReference": 8},
            {"name": "w", "type": "View", "value": "raw", "variablesReference": 9},
        ],
        8: [{"name": "y", "type": "long", "value": "3", "variablesReference": 0}],
    }
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.state, worker.stop_id = runtime, "stopped", "stop1"
    worker.capabilities = {"supports_set_variable": True, "supports_member_set": True}
    worker.innermost_frame = 1
    reference = {"value": 7}
    worker.walk = lambda record, runtime, trail=None: (*record, reference["value"], None)
    worker.bounded_variables = lambda handle, indexed=None: children[handle]
    top = worker.expose_variables(
        [
            {"name": name, "type": "pt", "value": "pt @ 0x30", "variablesReference": 7}
            for name in notes
        ],
        {"id": 1},
        ("Locals",),
        (),
    )
    assert [shown.get("note") for shown in top] == ["may be leftovers", "not judged", None]

    def expand(handle, at=7):
        reference["value"] = at
        return worker.expand({"stop_id": "stop1", "handle": handle}, runtime)["variables"]

    x, inner, w = expand(top[0]["variablesReference"])
    through = "read through `q`: may be leftovers"
    assert x["note"] == through and x["settable"] is False
    assert inner["note"] == through and w["note"] == through
    [y] = expand(inner["variablesReference"], at=8)
    assert y["note"] == through and y["settable"] is False
    [element] = expand(w["variablesReference"])
    assert element["note"] == through
    # A note that refuses no change: carried, and settable as before.
    x, inner, w = expand(top[1]["variablesReference"])
    assert x["note"] == "read through `u`: not judged" and x["settable"] is True
    # A local with no note: nothing to carry.
    x, inner, w = expand(top[2]["variablesReference"])
    assert "note" not in x and "note" not in inner and x["settable"] is True
    [y] = expand(inner["variablesReference"], at=8)
    assert "note" not in y


def test_a_string_the_runtime_shows_keeps_its_handle_and_never_overrides_no_value(tmp_path):
    # #128: a libstdc++ string's text is the runtime's (read from memory, or noted); the
    # worker keeps its handle to the raw members, reads memory only where the debugger
    # can, and never gives a value to one LLDB has none for (optimized out).
    from types import SimpleNamespace

    from peeky.worker import DebugWorker

    calls = []

    def string_value(variable, fetch, read):
        calls.append((variable["name"], read))
        return {"value": '"a\\0b"', "view": "memory", "string_length": 3}

    runtime = SimpleNamespace(
        assert_current=lambda: None, reports_unavailable_as_text=True, string_value=string_value
    )
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.state, worker.stop_id = runtime, "stopped", "stop1"
    shown, gone = worker.expose_variables(
        [
            {"name": "s", "type": "std::string", "value": '"a"', "variablesReference": 7},
            {"name": "t", "type": "std::string", "value": "<error: optimized out>"},
        ],
        {"id": 1},
        ("Locals",),
        (),
    )
    assert shown["value"] == '"a\\0b"' and shown["view"] == "memory" and shown["string_length"] == 3
    assert shown["variablesReference"]
    assert gone["unavailable"] and gone["value"] is None and "view" not in gone
    # Without readMemory the runtime gets no reader.
    assert calls == [("s", None)]
    worker.capabilities["supports_read_memory"] = True
    worker.expose_variables(
        [{"name": "s", "type": "std::string", "value": '"a"', "variablesReference": 7}],
        {"id": 1},
        ("Locals",),
        (),
    )
    assert calls[-1][1] == worker.view_read


def test_a_string_shown_as_its_text_lists_no_members(tmp_path):
    # Eval 6, finding 15: `session get name` listed libc++'s `(anonymous struct)` (and
    # libstdc++'s `_M_dataplus`) under a std::string. A value the runtime marks "text"
    # gets no handle, and is never "at the depth limit".
    from types import SimpleNamespace

    from peeky.worker import DebugWorker

    def string_value(variable, fetch, read):
        return {"text": True} if variable["name"] == "s" else None

    runtime = SimpleNamespace(
        assert_current=lambda: None, reports_unavailable_as_text=True, string_value=string_value
    )
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.state, worker.stop_id = runtime, "stopped", "stop1"
    text, other = worker.expose_variables(
        [
            {"name": "s", "type": "std::string", "value": '"a"', "variablesReference": 7},
            {"name": "r", "type": "Rec", "value": "Rec @ 0x1", "variablesReference": 8},
        ],
        {"id": 1},
        ("Locals",),
        (),
    )
    assert text["variablesReference"] == 0 and text["text"] is True and text["value"] == '"a"'
    assert other["variablesReference"] and "text" not in other
    assert list(worker.children) == [other["variablesReference"]]
    (deep,) = worker.expose_variables(
        [{"name": "s", "type": "std::string", "value": '"a"', "variablesReference": 9}],
        {"id": 1},
        ("Locals",),
        (("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")),
    )
    assert deep["variablesReference"] == 0 and "depth_limited" not in deep


class ChildrenDap:
    """`variables` replies by reference, each request recorded."""

    def __init__(self, children):
        self.children, self.requests = children, []

    def request(self, command, arguments=None, **_):
        self.requests.append((command, arguments))
        reference = arguments["variablesReference"]
        return {"variables": self.children[reference][: arguments.get("count", 64)]}


def test_previews_are_made_only_where_asked_and_within_their_budget(tmp_path):
    # #136: a C or C++ struct's preview replaces lldb-dap's `Type @ 0x…`; the handle
    # stays, so its fields are selected as before. Probes ask only for the roots they
    # capture; a listing for the values it lists.
    import time as clock
    from types import SimpleNamespace

    from peeky.worker import PREVIEW_FETCHES, DebugWorker

    located = []

    def preview_value(variable, fetch, read, frame=None, steps=None, **stop):
        # Where the value is: its frame, and the path of names its type is found by.
        located.append((frame, steps))
        listed = fetch(variable["variablesReference"], 64)
        return "{" + ", ".join(f"{c['name']} = {c['value']}" for c in listed) + "}"

    runtime = SimpleNamespace(
        assert_current=lambda: None, reports_unavailable_as_text=True, preview_value=preview_value
    )
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.state, worker.stop_id = runtime, "stopped", "stop1"
    worker.dap = ChildrenDap({7: [{"name": "x", "value": "1"}], 8: [{"name": "y", "value": "2"}]})
    locals_ = [
        {"name": "p", "type": "Pt", "value": "Pt @ 0x10", "variablesReference": 7},
        {"name": "q", "type": "Pt", "value": "Pt @ 0x20", "variablesReference": 8},
        {"name": "g", "type": "Pt", "value": "<variable not available>", "variablesReference": 9},
    ]

    def expose(previewing):
        worker.previewing = previewing
        worker.reset_previews()
        return worker.expose_variables(locals_, {"id": 1}, ("Locals",), ())

    p, q, gone = expose(True)
    assert p["value"] == "{x = 1}" and q["value"] == "{y = 2}" and p["variablesReference"]
    assert located[:2] == [({"id": 1}, ["p"]), ({"id": 1}, ["q"])]
    # No value, no preview.
    assert gone["unavailable"] and gone["value"] is None
    assert [arguments["variablesReference"] for _, arguments in worker.dap.requests] == [7, 8]
    p, q, _ = expose(frozenset({"q"}))
    assert p["value"] == "Pt @ 0x10" and q["value"] == "{y = 2}"
    p, q, _ = expose(False)
    assert (p["value"], q["value"]) == ("Pt @ 0x10", "Pt @ 0x20")
    # A spent budget leaves the debugger's text; it is the preview's own, not the
    # value views' (#128 strings, Rust).
    worker.previewing = True
    worker.preview_fetches, worker.preview_deadline = PREVIEW_FETCHES, clock.monotonic() + 5
    worker.view_fetches = 0
    p, _, _ = worker.expose_variables(locals_, {"id": 1}, ("Locals",), ())
    assert p["value"] == "Pt @ 0x10" and worker.view_fetches == 0
    worker.reset_previews()
    worker.preview_deadline = clock.monotonic() - 1
    with pytest.raises(ValueError, match="Preview budget exhausted"):
        worker.preview_fetch(7, 8)
    worker.reset_previews()
    for count in (0, 65):
        with pytest.raises(ValueError, match="exceeds limit"):
            worker.preview_fetch(7, count)


@pytest.mark.parametrize(
    "operation, args, message",
    [
        ("inspect", {"previews": "p"}, "previews must be"),
        ("inspect", {"previews": ["p"] * 33}, "previews must be"),
        ("expand", {"handle": 1, "previews": ["p"]}, "previews must be true or false"),
        ("preview", {"handle": 0}, "Invalid preview handle"),
        ("preview", {"handle": 1, "pointee": "yes"}, "pointee must be"),
    ],
)
def test_preview_requests_are_checked(tmp_path, operation, args, message):
    from peeky.worker import DebugWorker

    with pytest.raises(ValueError, match=message):
        DebugWorker(tmp_path).validate(operation, {"session_id": "s", "stop_id": "t", **args})


def test_a_noted_reason_shows_the_value_with_its_note_and_only_others_withhold(tmp_path):
    # #140 with #132's check before any view: a reason with a `note` shows the value (a
    # view's too) with the note, and is offered for set unless it `refuses`; a reason
    # without one withholds the row; the runtime sees the frame's rows and the stop.
    from types import SimpleNamespace

    from peeky.native import ValueNote
    from peeky.worker import DebugWorker

    reasons = {
        "kept": ValueNote("declared on line 3", "may be the last pass's value", refuses=False),
        "loop": ValueNote("declared on line 4", "may be the last pass's value", refuses=True),
        "late": "declared on line 9, which has not run yet",
        "seen": ValueNote("in a function calling setjmp", "setjmp may have run", refuses=False),
    }
    asked = []

    def not_yet_declared(variable, frame, **kwargs):
        asked.append((variable["name"], [row["name"] for row in kwargs["siblings"]]))
        return reasons.get(variable["name"])

    def view_variable(variable, fetch, read=None, **kwargs):
        if variable["name"] != "seen":
            return None
        return {"value": '"x"', "rust_view": "memory"}

    runtime = SimpleNamespace(
        assert_current=lambda: None,
        reports_unavailable_as_text=True,
        not_yet_declared=not_yet_declared,
        view_variable=view_variable,
        settable=lambda variable, frame: True,
    )
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.state, worker.stop_id = runtime, "stopped", "stop1"
    worker.capabilities["supports_set_variable"] = True
    worker.innermost_frame, worker.stop_reason = 1, "breakpoint"
    names = ["kept", "loop", "late", "seen", "plain"]
    rows = worker.expose_variables(
        [{"name": name, "type": "int", "value": "7"} for name in names],
        {"id": 1},
        ("Locals",),
        (),
    )
    kept, loop, late, seen, plain = rows
    assert kept["value"] == "7" and kept["note"] == "may be the last pass's value"
    assert kept["settable"] is True and "withheld" not in kept
    assert loop["value"] == "7" and loop["note"] and loop["settable"] is False
    assert late["withheld"] and late["value"] is None and late["reason"].startswith("declared")
    assert late["settable"] is False and late["variablesReference"] == 0
    assert (
        seen["value"] == '"x"'
        and seen["view"] == "memory"
        and seen["note"] == "setjmp may have run"
    )
    assert plain["value"] == "7" and "note" not in plain and plain["settable"] is True
    assert asked == [(name, names) for name in names]


def test_a_set_the_build_rules_out_is_refused_before_launch(tmp_path):
    # #138: an optimized build refuses every change, saying where the local has no stack
    # slot; otherwise the runtime's build facts decide, as a message or (code, message).
    from types import SimpleNamespace

    import pytest

    from peeky.errors import Refused
    from peeky.worker import DebugWorker

    worker = DebugWorker(tmp_path)
    asked = []
    runtime = SimpleNamespace(
        target_identity={"optimization": "O1"},
        storage_note=lambda program, lines, name: asked.append((lines, name)) or "; no slot",
        never_settable=lambda program, lines, names: pytest.fail("not asked when optimized"),
    )
    worker.runtime = runtime
    with pytest.raises(Refused) as refused:
        worker.refuse_never_settable(runtime, tmp_path / "a.c", [{"line": 7, "name": "x"}])
    assert refused.value.code == "MUTATION_REJECTED" and refused.value.before_launch is True
    assert "refused in an optimized build (-O1)" in str(refused.value)
    assert "; no slot. Refused before the program runs" in str(refused.value)
    assert asked == [([7], "x")]
    for verdict, code in (("`x` is kept in a register", "MUTATION_REJECTED"),):
        runtime = SimpleNamespace(
            target_identity={}, never_settable=lambda program, lines, names: verdict
        )
        worker.runtime = runtime
        with pytest.raises(Refused) as refused:
            worker.refuse_never_settable(runtime, tmp_path / "a.c", [{"line": 7, "name": "x"}])
        assert refused.value.code == code and str(refused.value) == verdict
    runtime = SimpleNamespace(
        target_identity={},
        never_settable=lambda program, lines, names: ("NOT_FOUND", "no local `y`"),
    )
    worker.runtime = runtime
    with pytest.raises(Refused) as refused:
        worker.refuse_never_settable(runtime, tmp_path / "a.c", [{"line": 7, "name": "y"}])
    assert refused.value.code == "NOT_FOUND"
    runtime = SimpleNamespace(target_identity={}, never_settable=lambda *args: None)
    worker.runtime = runtime
    worker.refuse_never_settable(runtime, tmp_path / "a.c", [{"line": 7, "name": "z"}])
    for sets in ("x", ["x"], [{"line": 0, "name": "x"}], [{"line": 3, "name": ""}]):
        with pytest.raises(ValueError, match="sets must be a list"):
            DebugWorker.validate("launch", {"program": "a.c", "sets": sets})
    # Each change is judged against its own line's functions only (#137: --set-at).
    judged = []
    runtime = SimpleNamespace(
        target_identity={},
        never_settable=lambda program, lines, names: (
            judged.append((lines, names)) or ("`w` is kept in a register" if lines == [9] else None)
        ),
    )
    worker.runtime = runtime
    changes = [{"line": 7, "name": "x"}, {"line": 9, "name": "w"}, {"line": 7, "name": "y"}]
    with pytest.raises(Refused, match="`w` is kept in a register"):
        worker.refuse_never_settable(runtime, tmp_path / "a.c", changes)
    assert judged == [([7], ["x", "y"]), ([9], ["w"])]


def test_a_row_no_stop_can_change_says_why(tmp_path):
    # #138: a root (or a field whose root) the runtime says no stop changes is marked
    # `not_settable`; a settable row never is.
    from types import SimpleNamespace

    from peeky.worker import DebugWorker

    def never_settable_row(variable, frame, member=False):
        return "register" if variable["name"] in ("width", "pair") else None

    runtime = SimpleNamespace(
        assert_current=lambda: None,
        reports_unavailable_as_text=True,
        settable=lambda variable, frame: variable["name"] == "total",
        never_settable_row=never_settable_row,
    )
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.state, worker.stop_id = runtime, "stopped", "stop1"
    worker.capabilities["supports_set_variable"] = True
    worker.innermost_frame = 1
    rows = worker.expose_variables(
        [
            {"name": name, "type": "unsigned long", "value": "1"}
            for name in ("width", "total", "other")
        ],
        {"id": 1},
        ("Locals",),
        (),
    )
    width, total, other = rows
    assert width["settable"] is False and width["not_settable"] == "register"
    assert total["settable"] is True and "not_settable" not in total
    assert "not_settable" not in other


class ThreadsDap:
    """`threads` replies in turn; evaluate replies LLDB's `thread list`."""

    def __init__(self, replies, listing, started=()):
        self.replies, self.listing, self.started_threads = list(replies), listing, set(started)
        self.asked = 0

    def request(self, command, arguments=None, **_):
        if command == "threads":
            self.asked += 1
            reply = self.replies.pop(0)
            if reply is None:
                raise TimeoutError("no reply")
            return {"threads": [{"id": thread} for thread in reply]}
        raise AssertionError(command)


def test_the_threads_at_a_stop_are_every_source_s(tmp_path):
    # Review of #181: two `threads` replies that both say one thread (stale) proved
    # nothing; LLDB's own `thread list` and the `thread` events that said a thread
    # started count too, and any source that does not answer leaves it unsure.
    from types import SimpleNamespace

    from peeky.worker import DebugWorker

    def worker(replies, listing, started=()):
        made = DebugWorker(tmp_path)
        made.dap = ThreadsDap(replies, listing, started)
        made.runtime = SimpleNamespace(listed_threads=lambda dap: dap.listing)
        made.thread_count = None
        return made

    assert worker([[1], [1]], frozenset({1})).threads_at_stop() == 1
    # Stale replies, but LLDB lists the worker thread.
    assert worker([[1], [1]], frozenset({1, 2})).threads_at_stop() == 2
    # Stale replies and list, but a thread said it started.
    assert worker([[1], [1]], frozenset({1}), started={1, 7}).threads_at_stop() == 2
    assert worker([[1], [1, 2]], frozenset({1})).threads_at_stop() == 2
    # Unsure: a reply missing, LLDB not answering, a thread without an id.
    assert worker([None, [1]], frozenset({1})).threads_at_stop() is None
    assert worker([[1], [1]], None).threads_at_stop() is None
    assert worker([[1], [1]], frozenset({1}), started={None}).threads_at_stop() is None
    # Read once per inspection.
    made = worker([[1], [1]], frozenset({1}))
    assert made.threads_at_stop() == made.threads_at_stop() == 1 and made.dap.asked == 2


def test_lldb_s_thread_list_is_read_for_its_thread_ids():
    from types import SimpleNamespace

    from peeky.native import NativeSession

    text = (
        "Process 8509 stopped\n* thread #1: tid = 0x3a7f386, 0x0000000100728568 program`main "
        "at threads.cpp:9:25, stop reason = breakpoint 1.1\n  thread #2: tid = 0x3a7f3b5, "
        "0x0000000190494c74 libsystem_kernel.dylib`swtch_pri + 8\n"
    )
    dap = SimpleNamespace(request=lambda *a, **k: {"result": text})
    assert NativeSession.listed_threads(dap) == {0x3A7F386, 0x3A7F3B5}
    assert (
        NativeSession.listed_threads(SimpleNamespace(request=lambda *a, **k: {"result": ""}))
        is None
    )
