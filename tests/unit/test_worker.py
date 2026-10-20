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
    worker.walk = lambda record, runtime: (record[0], record[1], record[2], 7, None)
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
    worker.walk = lambda record, runtime: (record[0], record[1], record[2], 8, None)
    [length] = worker.expand({"stop_id": "stop1", "handle": v["variablesReference"]}, runtime)[
        "variables"
    ]
    assert length["value"] == "3" and length["stale"] and ("len", False, "stale") in seen
    worker.walk = lambda record, runtime: (record[0], record[1], record[2], 7, None)
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
