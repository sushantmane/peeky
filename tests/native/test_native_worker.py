"""Real LLDB C worker conformance; optional locally, required in the native CI lane."""

import hashlib
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError

import pytest

from peeky.client import Client
from peeky.worker import make_server

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def native_config():
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    return {
        "sources": ("branch_bug.c",),
        "compiler": os.environ.get("PEEKY_NATIVE_CC", "clang"),
        "adapter": os.environ["PEEKY_NATIVE_DAP"],
        "dwarfdump": os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        "dsymutil": os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        "sysroot": os.environ.get("PEEKY_NATIVE_SYSROOT"),
    }


@pytest.fixture
def native_worker(tmp_path, native_config):
    from peeky.native import NativeTarget

    source = tmp_path / "branch_bug.c"
    shutil.copyfile(ROOT / "examples/c/branch_bug.c", source)
    lines = source.read_text().splitlines()
    first = next(i for i, line in enumerate(lines, 1) if "/* FIRST */" in line)
    second = next(i for i, line in enumerate(lines, 1) if "/* SECOND */" in line)
    servers = []

    def start(**overrides):
        target = NativeTarget(**{**native_config, **overrides})
        server = make_server(tmp_path, "native-test-secret-token-123", 0, adapter=target)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        client = Client(f"http://127.0.0.1:{server.server_port}", "native-test-secret-token-123")
        return client, server.worker

    yield start, source, first, second
    for server, thread in servers:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def wait(client, session, state):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = client.call("wait", **session, timeout=1)
        if result["state"] == state:
            return result
        assert result["state"] == "running", result
    pytest.fail(f"native target did not reach {state}")


def observed(snapshot):
    return {v["name"]: v for s in snapshot["scopes"] for v in s["variables"]}


def launch(client, first, second=None):
    points = [{"line": first}]
    if second:
        points.append({"line": second})
    try:
        started = client.call("launch", program="branch_bug.c", breakpoints=points)
    except HTTPError as error:
        pytest.fail(f"native launch failed: {error.read().decode()}")
    session = {"session_id": started["session_id"]}
    stop = wait(client, session, "stopped")
    context = dict(session, stop_id=stop["stop_id"])
    snapshot = client.call("inspect", **context)
    assert snapshot["frames"][0]["line"] == first
    events = client.call("report", **session)["breakpoint_events"]
    verified = any(p.get("verified") and p.get("line") == first for p in started["breakpoints"])
    verified |= any(
        e["breakpoint"].get("verified") and e["breakpoint"].get("line") == first for e in events
    )
    assert verified
    return started, session, context, snapshot


def fail_http(code, client, operation, **args):
    with pytest.raises(HTTPError) as caught:
        client.call(operation, **args)
    assert caught.value.code == code
    return json.load(caught.value)["error"]


def gone(pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"native target {pid} survived close")


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_c_http_scalar_intervention_and_stale_stop(native_worker, monkeypatch):
    monkeypatch.setenv("DEBUG_TOKEN", "never-pass-to-native-target")
    start, source, first, second = native_worker
    client, worker = start()
    initial_hash = hash_file(source)
    started, session, old, snapshot = launch(client, first, second)
    assert started["artifact_manifest_id"]
    current = observed(snapshot)
    assert current["value"]["value"] == "0"
    assert current["pointer"]["type"] == "int *"
    assert current["pointer"]["variablesReference"] > 0
    assert current["items"]["type"] == "int[3]"
    assert current["items"]["variablesReference"] > 0
    assert current["token_present"]["value"] == "0"
    assert all(s["name"].lower() != "registers" for s in snapshot["scopes"])
    pid = int(current["pid"]["value"])
    binary = worker.runtime.binary
    binary_hash = hash_file(binary)
    ref = next(
        s["reference"]
        for s in snapshot["scopes"]
        if any(v["name"] == "value" for v in s["variables"])
    )
    old_ref = ref
    snapshot = client.call("inspect", **old)
    ref = next(
        s["reference"]
        for s in snapshot["scopes"]
        if any(v["name"] == "value" for v in s["variables"])
    )
    assert ref != old_ref
    fail_http(400, client, "set_variable", **old, reference=old_ref, name="value", value="99")
    fail_http(400, client, "set_variable", **old, reference=ref, name="pointer", value="0")
    change = client.call("set_variable", **old, reference=ref, name="value", value="5000")
    assert change["before"] == "0" and change["after"] == "5000"
    assert hash_file(binary) == binary_hash and hash_file(source) == initial_hash
    client.call("continue", **old)
    stop = wait(client, session, "stopped")
    assert stop["stop_id"] != old["stop_id"]
    assert "Stale stop" in fail_http(
        400, client, "set_variable", **old, reference=ref, name="value", value="99"
    )
    context = dict(session, stop_id=stop["stop_id"])
    fail_http(400, client, "set_variable", **context, reference=ref, name="value", value="99")
    assert observed(client.call("inspect", **context))["value"]["value"] == "5000"
    client.call("continue", **context)
    wait(client, session, "terminated")
    assert (
        "RESULT=success value=5000 array=4 pointer=5000"
        in client.call("report", **session)["output"]
    )
    client.call("close", **session)
    gone(pid)
    assert hash_file(source) == initial_hash


@pytest.mark.parametrize("tamper", ["source", "copied-source", "binary", "symbols"])
def test_native_integrity_drift_invalidates_before_mutation(native_worker, tamper):
    start, source, first, _ = native_worker
    client, worker = start()
    _, session, context, snapshot = launch(client, first)
    pid = int(observed(snapshot)["pid"]["value"])
    runtime = worker.runtime
    if tamper == "source":
        path = source
    elif tamper == "copied-source":
        path = runtime.prepared.workspace / "branch_bug.c"
    elif tamper == "symbols":
        path = runtime.symbols
    else:
        path = runtime.binary
    path.write_bytes(path.read_bytes() + b"changed")
    ref = snapshot["scopes"][0]["reference"]
    fail_http(500, client, "set_variable", **context, reference=ref, name="value", value="5000")
    assert worker.session is None
    assert not runtime.prepared.workspace.exists()
    gone(pid)
    fail_http(400, client, "report", **session)


def test_native_stopped_close_reaps_target(native_worker):
    start, _, first, _ = native_worker
    client, worker = start()
    _, session, _, snapshot = launch(client, first)
    pid = int(observed(snapshot)["pid"]["value"])
    runtime = worker.runtime
    client.call("close", **session)
    gone(pid)
    assert not runtime.prepared.workspace.exists()


def test_native_missing_symbols_fails_launch(native_worker, native_config, tmp_path):
    start, _, first, _ = native_worker
    compiler = shutil.which(native_config["compiler"])
    assert compiler
    wrapper = tmp_path / "compiler-without-symbols"
    wrapper.write_text(
        f"#!{sys.executable}\nimport os, sys\nos.execv({compiler!r}, [{compiler!r}, *[arg for arg in sys.argv[1:] if not arg.startswith('-g')]])\n"
    )
    wrapper.chmod(0o755)
    client, worker = start(compiler=str(wrapper))
    with pytest.raises(HTTPError) as caught:
        client.call("launch", program="branch_bug.c", breakpoints=[{"line": first}])
    message = json.load(caught.value)["error"]
    assert any(
        word in message.lower()
        for word in ("debug", "symbol", "dwarf", "line table", "line information")
    ), message
    assert worker.session is None and worker.runtime is None


def test_native_skips_hostile_home_init_file(native_worker, native_config, tmp_path, monkeypatch):
    from peeky.dap import DAP

    home = tmp_path / "hostile-home"
    home.mkdir()
    marker = tmp_path / "lldbinit-executed"
    (home / ".lldbinit").write_text(f"script open({str(marker)!r}, 'w').write('loaded')\n")
    monkeypatch.setenv("HOME", str(home))
    # Positive control: verify this LLDB really consults our fixture HOME.
    control = DAP(command=(native_config["adapter"],), env=dict(os.environ))
    try:
        control.request(
            "initialize",
            {
                "adapterID": "lldb",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "pathFormat": "path",
                "sourceInitFile": True,
                "$__lldb_sourceInitFile": True,
            },
        )
        assert marker.read_text() == "loaded"
    finally:
        control.close()
    marker.unlink()
    start, _, first, _ = native_worker
    client, _ = start()
    _, session, _, _ = launch(client, first)
    assert not marker.exists()
    client.call("close", **session)


def test_native_adapter_death_reaps_stopped_target(native_worker):
    start, _, first, _ = native_worker
    client, worker = start()
    _, session, context, snapshot = launch(client, first)
    pid = int(observed(snapshot)["pid"]["value"])
    runtime = worker.runtime
    adapter = worker.dap.process
    adapter.kill()
    adapter.wait(timeout=3)
    fail_http(500, client, "inspect", **context)
    assert worker.session is None
    assert not runtime.prepared.workspace.exists()
    gone(pid)
    fail_http(400, client, "report", **session)


def test_native_arguments_are_literal_and_keep_empty_values(native_worker):
    start, _, first, _ = native_worker
    client, _ = start(arguments=("two words", "", "*", "$HOME", "it's"))
    _, session, context, _ = launch(client, first)
    client.call("continue", **context)
    wait(client, session, "terminated")
    output = client.call("report", **session)["output"]
    for expected in ("ARG:9:two words", "ARG:0:", "ARG:1:*", "ARG:5:$HOME", "ARG:4:it's"):
        assert expected in output
    client.call("close", **session)
