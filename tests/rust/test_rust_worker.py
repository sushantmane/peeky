"""Real Cargo + LLDB Rust worker conformance; optional locally, required in the Rust CI lane."""

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError

import pytest

from peeky.client import Client
from peeky.worker import make_server

ROOT = Path(__file__).resolve().parents[2]
INPUTS = ("Cargo.toml", "Cargo.lock", "src/main.rs", "tests/pagination.rs")
TOKEN = "rust-test-secret-token-1234"


@pytest.fixture
def rust_worker(tmp_path):
    names = ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER")
    if not all(os.environ.get("PEEKY_RUST_" + name) for name in names):
        pytest.skip("set PEEKY_RUST_* tool paths to enable Rust worker conformance")
    from peeky.rust import RustTarget

    root = tmp_path / "app"
    for name in INPUTS:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "examples/rust" / name, root / name)
    target = RustTarget(
        sources=("src/main.rs",),
        tests=("tests/pagination.rs",),
        cargo=os.environ["PEEKY_RUST_CARGO"],
        rustc=os.environ["PEEKY_RUST_RUSTC"],
        adapter=os.environ["PEEKY_RUST_DAP"],
        dwarfdump=os.environ["PEEKY_RUST_DWARFDUMP"],
        linker=os.environ["PEEKY_RUST_LINKER"],
        dsymutil=os.environ.get("PEEKY_RUST_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_RUST_SYSROOT"),
    )
    server = make_server(root, TOKEN, 0, adapter=target)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_port}", TOKEN)
    line = next(
        i
        for i, text in enumerate((root / "src/main.rs").read_text().splitlines(), 1)
        if "// OBSERVE" in text
    )
    try:
        yield client, server.worker, root, line
    finally:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def stopped(client, session):
    for _ in range(60):
        state = client.call("wait", **session, timeout=2)
        if state["state"] != "running":
            return state
    raise AssertionError("Rust target did not stop")


@pytest.mark.timeout(120)
def test_rust_http_inspection_active_variants_and_scalar_intervention(rust_worker):
    client, worker, root, line = rust_worker
    launch = client.call("launch", program="src/main.rs", breakpoints=[{"line": line}])
    assert launch["target_identity"]["language"] == "rust"
    assert launch["breakpoints"][0]["verified"] is True
    session = {"session_id": launch["session_id"]}
    runtime = worker.runtime
    workspace, parent = runtime.prepared.workspace, runtime.parent
    assert runtime.lines["src/main.rs"] and line in runtime.lines["src/main.rs"]
    assert runtime.layouts["core::option::Option<i32>"]["kind"] == "option"
    stop = stopped(client, session)
    assert stop["state"] == "stopped"
    inspected = client.call("inspect", **session, stop_id=stop["stop_id"])
    assert inspected["frames"][0]["line"] == line
    scope = inspected["scopes"][0]
    variables = {v["name"]: v for v in scope["variables"]}
    assert variables["some"]["value"] == "Some(3)"
    assert variables["some"]["view"] == "active-variant"
    assert variables["none"]["value"] == "None"
    assert variables["some"]["variablesReference"] == 0 == variables["none"]["variablesReference"]
    assert variables["mode"]["value"] == "First"
    assert variables["start"] == {
        **variables["start"],
        "type": "i32",
        "debugger_type": "int",
        "value": "3",
    }
    request = client.call(
        "expand",
        **session,
        stop_id=stop["stop_id"],
        handle=variables["request"]["variablesReference"],
    )
    assert {v["name"]: v["value"] for v in request["variables"]} == {
        "page": "1",
        "page_size": "3",
        "total": "8",
    }
    with pytest.raises(HTTPError) as rejected:
        client.call(
            "set_variable",
            **session,
            stop_id=stop["stop_id"],
            reference=scope["reference"],
            name="start",
            value="0",
        )
    assert rejected.value.code == 400  # expansion revoked root mutation authority
    scope = client.call("inspect", **session, stop_id=stop["stop_id"])["scopes"][0]
    for name, value in (("some", "0"), ("start", "2147483648"), ("start", "start")):
        with pytest.raises(HTTPError) as rejected:
            client.call(
                "set_variable",
                **session,
                stop_id=stop["stop_id"],
                reference=scope["reference"],
                name=name,
                value=value,
            )
        assert rejected.value.code == 400
        scope = client.call("inspect", **session, stop_id=stop["stop_id"])["scopes"][0]
    changed = client.call(
        "set_variable",
        **session,
        stop_id=stop["stop_id"],
        reference=scope["reference"],
        name="start",
        value="0",
    )
    assert changed == {"name": "start", "before": "3", "after": "0"}
    client.call("continue", **session, stop_id=stop["stop_id"])
    assert stopped(client, session)["state"] == "terminated"
    report = client.call("report", **session)
    assert "PAGE:[10, 20, 30]" in report["output"]
    assert report["artifact_manifest_id"] == launch["artifact_manifest_id"]
    client.call("close", **session)
    assert not workspace.exists() and not parent.exists()
    for name in INPUTS:
        assert (root / name).read_bytes() == (ROOT / "examples/rust" / name).read_bytes()


@pytest.mark.timeout(120)
def test_rust_source_drift_fails_closed_before_inspection(rust_worker):
    client, worker, root, line = rust_worker
    launch = client.call("launch", program="src/main.rs", breakpoints=[{"line": line}])
    session = {"session_id": launch["session_id"]}
    parent = worker.runtime.parent
    stop = stopped(client, session)
    source = root / "src/main.rs"
    source.write_text(source.read_text() + "\n// drift\n")
    with pytest.raises(HTTPError) as failed:
        client.call("inspect", **session, stop_id=stop["stop_id"])
    assert failed.value.code == 500
    assert worker.runtime is None and worker.dap is None and not parent.exists()


@pytest.mark.timeout(120)
def test_rust_compile_failure_and_frozen_test_edit_leave_no_workspace(
    rust_worker, tmp_path, tmp_path_factory, monkeypatch
):
    client, worker, root, line = rust_worker
    from peeky.build import BuildCommandError, SourceEdit

    # A private temp root outside the crate, so concurrent Peeky runs cannot interfere.
    private = tmp_path_factory.mktemp("rust-tmp")
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    edit = SourceEdit("src/main.rs", "let mut start: i32 =", "let mut start: i32 = ;")
    with pytest.raises(BuildCommandError):
        worker.adapter.prepare(root / "src/main.rs", root, edit=edit)
    with pytest.raises(ValueError, match="declared"):
        worker.adapter.prepare(
            root / "src/main.rs", root, edit=SourceEdit("tests/pagination.rs", "1", "2")
        )
    assert list(private.glob("peeky-rust-*")) == []


@pytest.mark.timeout(120)
def test_rust_program_stopped_when_its_debugger_dies_is_ended(rust_worker):
    from peeky import processes

    client, worker, root, line = rust_worker
    launch = client.call("launch", program="src/main.rs", breakpoints=[{"line": line}])
    session = {"session_id": launch["session_id"]}
    stop = stopped(client, session)
    pid = worker.dap.process_pid
    assert pid and processes.alive(processes.table().get(pid))
    worker.dap.process.kill()
    worker.dap.process.wait(timeout=3)
    with pytest.raises(HTTPError) as failed:
        client.call("inspect", **session, stop_id=stop["stop_id"])
    error = json.load(failed.value)["error"]
    # Apple's debugserver outlives lldb-dap and keeps the program stopped: Peeky ends both,
    # as it listed them (#143). Linux's lldb-server ends its tracee itself.
    assert error["code"] == "DEBUGGER_ERROR"
    cleanup = error["cleanup"]
    assert "; the debugger was gone: a process the program started in the last " in cleanup
    assert str(pid) in cleanup or cleanup.startswith("nothing Peeky could identify was still")
    deadline = time.monotonic() + 5
    while processes.alive(processes.table().get(pid)) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not processes.alive(processes.table().get(pid))
