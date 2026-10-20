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
    with pytest.raises(HTTPError) as caught:
        client.call("inspect", **context)
    # The error says what became of the JVM the adapter launched and announced (#143).
    assert json.load(caught.value)["error"]["cleanup"] == f"terminated pid {pid}"
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
    # Records state what the program received: --arg replaces the configured arguments.
    assert launch["program_args"] == ["one", "two words"]
    assert launch["target_identity"]["arguments"] == ["one", "two words"]
    stop = wait(client, session, "stopped")
    client.call("continue", **session, stop_id=stop["stop_id"])
    wait(client, session, "terminated")
    assert "ARGS=2:[one, two words]" in client.call("report", **session)["output"]
    client.call("close", **session)
    launch = client.call("launch", program="demo/BranchBug.java", breakpoints=[{"line": first}])
    session = {"session_id": launch["session_id"]}
    assert launch["program_args"] == ["configured"]
    assert launch["target_identity"]["arguments"] == ["configured"]
    stop = wait(client, session, "stopped")
    client.call("continue", **session, stop_id=stop["stop_id"])
    wait(client, session, "terminated")
    assert "ARGS=1:[configured]" in client.call("report", **session)["output"]
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


def test_java_sessions_step_and_run_to_lines(
    java_worker, java_bundle, tmp_path, capsys, monkeypatch
):
    from peeky.cli import main

    _, _, first, second = java_worker
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
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

    def step(*kind):
        assert main(["session", "step", *kind, "--json"]) == 0
        state = json.loads(capsys.readouterr().out)
        assert state["state"] == "stopped" and state["reason"] == "step", state
        return state

    start = ["session", "start", str(tmp_path), "--config", str(config)]
    # Line 4 is in main, before the nested class Inner is loaded.
    assert main([*start, "--at", "demo/BranchBug.java:4"]) == 0
    capsys.readouterr()
    # Over a JDK call (the launcher filters java.*): the next line of main.
    assert step()["location"].endswith("BranchBug.java:5")
    # Into a call of the program's own nested class, then back out to main.
    assert step("--into")["location"].endswith("BranchBug.java:9")
    assert step()["location"].endswith("BranchBug.java:10")
    assert main(["session", "get", "timeoutMs"]) == 0
    assert "timeoutMs: int = 0" in capsys.readouterr().out
    # Back in main once run returns: nothing is left of line 5, so javac's return on 6.
    assert step("--out")["location"].endswith("BranchBug.java:6")
    # java-debug verifies a line in a class that is not loaded yet only once it loads:
    # pending, not refused (the line is already checked against the class metadata).
    assert main(["session", "close"]) == 0
    assert main([*start, "--at", "demo/BranchBug.java:4"]) == 0
    capsys.readouterr()
    for line in (first, second):
        assert main(["session", "continue", "--to", str(line), "--json"]) == 0
        state = json.loads(capsys.readouterr().out)
        assert state["state"] == "stopped"
        assert state["location"].endswith(f"BranchBug.java:{line}")
    assert main(["session", "continue", "--to", "1", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_BREAKPOINT"
    assert main(["session", "close"]) == 0


def test_java_adapters_built_before_stepping_are_refused_cleanly(tmp_path):
    from types import SimpleNamespace

    from peeky.java import JavaSession
    from peeky.worker import DebugWorker

    caps = JavaSession.normalize_capabilities(None, {"supportsSetVariable": True})
    assert caps["supports_stepping"] is False
    assert JavaSession.normalize_capabilities(None, {"peekySteps": True})["supports_stepping"]
    worker = DebugWorker(tmp_path)
    worker.dap, worker.session, worker.thread_id = SimpleNamespace(), "s1", 1
    worker.runtime = SimpleNamespace(assert_current=lambda: None)
    worker.state, worker.stop_id, worker.capabilities = "stopped", "stop1", caps
    with pytest.raises(ValueError, match="rebuild it with scripts/setup_java_adapter.py"):
        worker.dispatch("step", {"session_id": "s1", "stop_id": "stop1", "kind": "over"})
    assert worker.state == "stopped"


THREADS_JAVA = """package demo;
import java.util.concurrent.CountDownLatch;
public class Threads {
  static final CountDownLatch READY = new CountDownLatch(2);
  public static void main(String[] args) throws Exception {
    Thread a = new Thread(() -> work(1));
    Thread b = new Thread(() -> work(2));
    a.start(); b.start(); a.join(); b.join();
    System.out.println("DONE");
  }
  static void work(int id) {
    READY.countDown();
    try { READY.await(); } catch (InterruptedException e) { throw new RuntimeException(e); }
    int x = id * 10; // HIT
    System.out.println("W" + id + "=" + (x + id));
  }
}
"""

CALLBACKS_JAVA = """package demo;
import java.util.List;
public class Callbacks {
  public static void main(String[] args) {
    List<Integer> xs = List.of(4, 2);
    xs.forEach(x -> sink(x)); // EACH
    System.out.println("END");
  }
  static void sink(int value) {
    int doubled = value * 2;
    System.out.println("S" + doubled);
  }
}
"""


def java_cli(tmp_path, java_bundle, monkeypatch, name, source):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / f"demo/{name}.java"
    path.parent.mkdir(exist_ok=True)
    path.write_text(source)
    config = tmp_path / f"{name}.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(java_bundle),
                "sources": [f"demo/{name}.java"],
                "main_class": f"demo.{name}",
            }
        )
    )
    lines = source.splitlines()
    marker = next(i for i, text in enumerate(lines, 1) if "// HIT" in text or "// EACH" in text)
    return ["session", "start", str(tmp_path), "--config", str(config)], marker


def test_java_steps_wait_for_threads_stopped_separately(java_bundle, tmp_path, capsys, monkeypatch):
    from peeky.cli import main

    start, hit = java_cli(tmp_path, java_bundle, monkeypatch, "Threads", THREADS_JAVA)
    assert main([*start, "--at", f"demo/Threads.java:{hit}"]) == 0
    capsys.readouterr()
    time.sleep(1.5)  # the second thread reaches the breakpoint and is suspended there
    # java-debug suspends each thread separately: the other thread's stop is served
    # before any step, never skipped (which would leave it suspended for good).
    assert main(["session", "step", "--json"]) == 2
    assert "Other threads" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert main(["session", "continue", "--json"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["state"] == "stopped" and state["location"].endswith(f"Threads.java:{hit}")
    assert main(["session", "step", "--json"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["reason"] == "step" and state["location"].endswith(f"Threads.java:{hit + 1}")
    assert main(["session", "continue", "--to-exit"]) == 0
    assert main(["session", "report"]) == 0
    output = capsys.readouterr().out
    assert "W1=11" in output and "W2=22" in output and "DONE" in output
    assert main(["session", "close"]) == 0


def test_java_step_into_reaches_lambda_bodies(java_bundle, tmp_path, capsys, monkeypatch):
    from peeky.cli import main

    start, each = java_cli(tmp_path, java_bundle, monkeypatch, "Callbacks", CALLBACKS_JAVA)
    assert main([*start, "--at", f"demo/Callbacks.java:{each}"]) == 0
    capsys.readouterr()
    locations = []
    for _ in range(3):
        assert main(["session", "step", "--into", "--json"]) == 0
        state = json.loads(capsys.readouterr().out)
        assert not state.get("outside_program"), state
        locations.append(state["location"].rsplit(":", 1)[1])
    # The lambda body (synthetic lambda$main$0, line 6), then sink's first two lines.
    assert locations == [str(each), "10", "11"]
    assert main(["session", "get", "doubled"]) == 0 and "= 8" in capsys.readouterr().out
    assert main(["session", "continue", "--to-exit"]) == 0
    assert main(["session", "close"]) == 0
