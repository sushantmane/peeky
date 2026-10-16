"""Java worker lifecycle through real HTTP and real JDK17 debuggee processes."""

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from urllib.error import HTTPError

import pytest

from peeky.client import Client
from peeky.worker import make_server


@pytest.fixture(scope="module")
def java_bundle():
    value = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not value:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a prepared Java adapter bundle")
    return Path(value)


@pytest.fixture
def java_worker(tmp_path, java_bundle):
    from peeky.java import JavaTarget

    source = tmp_path / "demo/BranchBug.java"
    source.parent.mkdir()
    source.write_text("""package demo;
public class BranchBug {
  public static void main(String[] args) throws Exception {
    System.out.println("ARGS=" + args.length + ":" + java.util.Arrays.toString(args));
    Inner.run(args.length > 0 && args[0].equals("hang"));
  }
  static class Inner {
    static void run(boolean hang) throws Exception {
      int timeoutMs = 0;
      int[] values = {2, 4, 6};
      long pid = ProcessHandle.current().pid();
      boolean tokenPresent = System.getenv("DEBUG_TOKEN") != null;
      String result = timeoutMs > 0 ? "success" : "fallback"; // FIRST
      System.out.println(result); // SECOND
      if (hang) Thread.sleep(60000);
    }
  }
}
""")
    lines = source.read_text().splitlines()
    first = next(i for i, line in enumerate(lines, 1) if "// FIRST" in line)
    second = next(i for i, line in enumerate(lines, 1) if "// SECOND" in line)
    servers = []

    def start(*, hang=False, arguments=()):
        target = JavaTarget(
            bundle=java_bundle,
            sources=("demo/BranchBug.java",),
            main_class="demo.BranchBug",
            arguments=("hang",) if hang else arguments,
        )
        server = make_server(tmp_path, "java-test-token-1234567890", port=0, adapter=target)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = Client(f"http://127.0.0.1:{server.server_port}", "java-test-token-1234567890")
        servers.append((server, thread))
        return client, server.worker

    yield start, source, first, second
    for server, thread in servers:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def wait(client, session, expected):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = client.call("wait", **session, timeout=1)
        if result["state"] == expected:
            return result
        assert result["state"] == "running", result
    pytest.fail(f"Java target did not reach {expected}")


def values(snapshot):
    return {v["name"]: v for scope in snapshot["scopes"] for v in scope["variables"]}


def scope_for(snapshot, name):
    return next(
        s["reference"] for s in snapshot["scopes"] if any(v["name"] == name for v in s["variables"])
    )


def assert_http_error(code, client, operation, **args):
    with pytest.raises(HTTPError) as caught:
        client.call(operation, **args)
    assert caught.value.code == code
    return json.load(caught.value)["error"]["message"]


def assert_process_gone(pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"owned Java process {pid} survived teardown")


def launch_stopped(client, first, second=None):
    points = [{"line": first}]
    if second:
        points.append({"line": second})
    try:
        launch = client.call("launch", program="demo/BranchBug.java", breakpoints=points)
    except HTTPError as error:
        pytest.fail(f"Java launch failed: {error.read().decode()}")
    session = {"session_id": launch["session_id"]}
    stop = wait(client, session, "stopped")
    events = client.call("report", **session)["breakpoint_events"]
    assert any(
        e["breakpoint"].get("verified") and e["breakpoint"].get("line") == first for e in events
    )
    context = dict(session, stop_id=stop["stop_id"])
    snapshot = client.call("inspect", **context)
    assert snapshot["frames"][0]["line"] == first
    return launch, session, context, snapshot


def test_java_http_intervention_second_stop_and_fresh_build(java_worker, monkeypatch):
    monkeypatch.setenv("DEBUG_TOKEN", "must-not-reach-jvm")
    start, source, first, second = java_worker
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    client, worker = start()
    launch, session, old, snapshot = launch_stopped(client, first, second)
    observed = values(snapshot)
    assert observed["timeoutMs"]["value"] == "0"
    assert observed["tokenPresent"]["value"] == "false"
    assert observed["values"]["type"] == "int[]"
    assert observed["values"]["variablesReference"] > 0
    assert observed["values"]["indexedVariables"] == 3
    pid = int(observed["pid"]["value"])
    mutation = {"reference": scope_for(snapshot, "timeoutMs"), "name": "timeoutMs", "value": "5000"}
    changed = client.call("set_variable", **old, **mutation)
    assert changed["before"] == "0" and changed["after"] == "5000"
    client.call("continue", **old)
    second_stop = wait(client, session, "stopped")
    assert second_stop["stop_id"] != old["stop_id"]
    assert "Stale stop" in assert_http_error(400, client, "set_variable", **old, **mutation)
    current = dict(session, stop_id=second_stop["stop_id"])
    assert "Inspect the scope" in assert_http_error(
        400, client, "set_variable", **current, **mutation
    )
    after = client.call("inspect", **current)
    assert after["frames"][0]["line"] == second
    assert values(after)["timeoutMs"]["value"] == "5000"
    client.call("continue", **current)
    wait(client, session, "terminated")
    report = client.call("report", **session)
    assert "success" in report["output"]
    client.call("close", **session)
    assert_process_gone(pid)
    assert worker.session is None
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    # A second HTTP launch must compile a fresh session, without carrying the mutation.
    _, fresh, current, snapshot = launch_stopped(client, first)
    assert fresh != session
    assert values(snapshot)["timeoutMs"]["value"] == "0"
    client.call("continue", **current)
    wait(client, fresh, "terminated")
    assert "fallback" in client.call("report", **fresh)["output"]
    client.call("close", **fresh)


@pytest.mark.parametrize("tamper", ["original-source", "prepared-source", "class"])
def test_java_tampering_invalidates_before_mutation(java_worker, tamper):
    start, source, first, _ = java_worker
    client, worker = start(hang=True)
    _, session, context, snapshot = launch_stopped(client, first)
    pid = int(values(snapshot)["pid"]["value"])
    prepared = worker.runtime.prepared
    if tamper == "original-source":
        target = source
    elif tamper == "prepared-source":
        target = prepared.workspace / "demo/BranchBug.java"
    else:
        target = prepared.workspace / "classes/demo/BranchBug$Inner.class"
    target.write_bytes(target.read_bytes() + b"\n")
    error = assert_http_error(
        500,
        client,
        "set_variable",
        **context,
        reference=scope_for(snapshot, "timeoutMs"),
        name="timeoutMs",
        value="5000",
    )
    assert error
    assert worker.session is None
    assert not prepared.workspace.exists()
    assert_process_gone(pid)
    assert_http_error(400, client, "report", **session)


def test_java_adapter_death_reaps_target(java_worker):
    start, _, first, _ = java_worker
    client, worker = start(hang=True)
    _, session, context, snapshot = launch_stopped(client, first)
    pid = int(values(snapshot)["pid"]["value"])
    prepared = worker.runtime.prepared
    adapter = worker.dap.process
    adapter.kill()
    adapter.wait(timeout=3)
    assert_http_error(500, client, "inspect", **context)
    assert worker.session is None
    assert not prepared.workspace.exists()
    assert_process_gone(pid)
    assert_http_error(400, client, "continue", **context)


def test_java_argument_roundtrip_and_paused_close(java_worker):
    start, _, first, _ = java_worker
    arguments = ("hello world", r"c:\path", "it's", "")
    client, worker = start(arguments=arguments)
    _, session, _, snapshot = launch_stopped(client, first)
    pid = int(values(snapshot)["pid"]["value"])
    output = client.call("report", **session)["output"]
    assert "ARGS=4:[hello world, c:\\path, it's, ]" in output
    prepared = worker.runtime.prepared
    client.call("close", **session)
    assert_process_gone(pid)
    assert not prepared.workspace.exists()


@pytest.mark.parametrize("argument", ['a"b', "a\nb", "a\rb"])
def test_java_rejects_unrepresentable_arguments(java_worker, argument):
    start, _, _, _ = java_worker
    with pytest.raises(ValueError, match="arguments"):
        start(arguments=(argument,))


def test_cli_describe_lists_fresh_build_java_lines(java_worker, java_bundle, tmp_path, capsys):
    from peeky.cli import main

    _, source, first, second = java_worker
    config = tmp_path / "java.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(java_bundle),
                "sources": ["demo/BranchBug.java"],
                "main_class": "demo.BranchBug",
            }
        )
    )
    assert main(["describe", str(tmp_path), "--config", str(config), "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    lines = manifest["breakpoint_lines"]["demo/BranchBug.java"]
    assert manifest["language"] == "java" and {first, second} <= set(lines)


def test_java_mutations_must_fit_the_local_type(java_worker):
    start, _, first, _ = java_worker
    client, _ = start()
    _, session, context, snapshot = launch_stopped(client, first)
    names = values(snapshot)
    assert names["timeoutMs"]["type"] == "int" and names["tokenPresent"]["type"] == "boolean"
    for name, value, message in (
        ("timeoutMs", "3000000000", "outside the int range"),
        ("timeoutMs", "4294967296", "outside the int range"),
        ("timeoutMs", "1.5", "integer literal for int"),
        ("tokenPresent", "1", "true or false"),
        ("pid", "99999999999999999999", "outside the long range"),
        ("values", "1", "is int[]"),
    ):
        error = assert_http_error(
            400,
            client,
            "set_variable",
            **context,
            reference=scope_for(snapshot, name),
            name=name,
            value=value,
        )
        assert message in error, (name, value, error)
    # Refusals happen before the adapter is asked: the stop and the values are intact.
    snapshot = client.call("inspect", **context)
    assert values(snapshot)["timeoutMs"]["value"] == "0"
    changed = client.call(
        "set_variable",
        **context,
        reference=scope_for(snapshot, "timeoutMs"),
        name="timeoutMs",
        value="-2147483648",
    )
    assert changed["after"] == "-2147483648"
    client.call("close", **session)


def test_java_lines_without_code_are_request_errors(java_worker):
    start, source, first, second = java_worker
    client, _ = start()
    blank = 1  # "package demo;"
    with pytest.raises(HTTPError) as caught:
        client.call("launch", program="demo/BranchBug.java", breakpoints=[{"line": blank}])
    error = json.load(caught.value)["error"]
    assert error["code"] == "INVALID_BREAKPOINT" and "no code to stop at" in error["message"]
    _, session, context, _ = launch_stopped(client, first)
    message = assert_http_error(
        400,
        client,
        "breakpoints",
        session_id=session["session_id"],
        program="demo/BranchBug.java",
        breakpoints=[{"line": blank}],
    )
    assert "executable lines nearby" in message
    # The session and its stop survive the refused request.
    assert client.call("inspect", **context)["frames"][0]["line"] == first
    client.call("close", **session)


def test_launched_java_takes_per_run_arguments(java_worker):
    start, _, first, _ = java_worker
    client, _ = start(arguments=("configured",))
    launch = client.call(
        "launch",
        program="demo/BranchBug.java",
        breakpoints=[{"line": first}],
        args=["one", "two words"],
    )
    session = {"session_id": launch["session_id"]}
    stop = wait(client, session, "stopped")
    client.call("continue", **session, stop_id=stop["stop_id"])
    wait(client, session, "terminated")
    assert "ARGS=2:[one, two words]" in client.call("report", **session)["output"]
    client.call("close", **session)
    message = assert_http_error(
        400,
        client,
        "launch",
        program="demo/BranchBug.java",
        breakpoints=[{"line": first}],
        args=['quote"d'],
    )
    assert "Invalid Java application arguments" in message
