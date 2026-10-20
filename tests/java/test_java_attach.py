"""Attach to a JVM already waiting on JDWP (the shape of `gradlew test --debug-jvm`)."""

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from peeky.cli import main
from peeky.probe import Probe, run_probe

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "src/demo/PaginationBug.java"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def attach_to(tmp_path, source, text, main_class, marker):
    """A JVM suspended on JDWP running ``text`` as ``source``, and its attach target."""
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to enable Java attach conformance")
    javac, java = shutil.which("javac"), shutil.which("java")
    (tmp_path / source).parent.mkdir(parents=True)
    (tmp_path / source).write_text(text)
    subprocess.run(
        [javac, "-g", "-d", str(tmp_path / "classes"), str(tmp_path / source)], check=True
    )
    port = free_port()
    agent = f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=y,address=127.0.0.1:{port}"
    process = subprocess.Popen(
        [java, agent, "-cp", str(tmp_path / "classes"), main_class],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert "Listening for transport dt_socket" in process.stdout.readline()
    from peeky.java_attach import JavaAttachTarget

    target = JavaAttachTarget(
        bundle=Path(bundle), port=port, sources=(source,), class_roots=("classes",)
    )
    line = next(i for i, text in enumerate(text.splitlines(), 1) if marker in text)
    return target, process, line


@pytest.fixture
def attached(tmp_path):
    text = (ROOT / "examples/java/demo/PaginationBug.java").read_text()
    target, process, line = attach_to(tmp_path, SOURCE, text, "demo.PaginationBug", "// OBSERVE")
    try:
        yield tmp_path, target, process, line
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


@pytest.mark.timeout(120)
def test_attached_probe_observes_intervenes_and_detaches_without_killing(attached):
    root, target, process, line = attached
    result = run_probe(
        root, Probe(SOURCE, line, ("start", "pageSize"), (("start", "0"),)), adapter=target
    )
    assert result["status"] == "completed", (result.get("error"), result.get("debugger_events"))
    rows = {(r["phase"], r["selector"]): r["value"] for r in result["rows"]}
    assert rows["observed", "start"] == "3" and rows["observed", "pageSize"] == "3"
    assert rows["after-set", "start"] == "0"
    assert result["target_identity"]["mode"] == "attach"
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "PAGE:[10, 20, 30]" in output


@pytest.mark.timeout(120)
def test_an_attached_jvms_array_element_takes_set(attached):
    # #86: fields and elements change in an attached JVM too.
    root, target, process, line = attached
    probe = Probe(SOURCE, line, ("items[3]",), (("items[3]", "99"),))
    result = run_probe(root, probe, adapter=target)
    assert result["status"] == "completed", (result.get("error"), result.get("debugger_events"))
    assert [(c["name"], c["before"], c["after"]) for c in result["interventions"]] == [
        ("items[3]", "40", "99")
    ]
    assert {r["phase"]: r["settable"] for r in result["rows"]} == {
        "observed": True,
        "after-set": True,
    }
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "PAGE:[99, 50, 60]" in output


@pytest.mark.timeout(120)
def test_attached_session_close_detaches_and_the_jvm_finishes_normally(
    attached, tmp_path, monkeypatch, capsys
):
    import json

    root, target, process, line = attached
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    config = tmp_path / "attach.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(target.bundle),
                "port": target.port,
                "sources": [SOURCE],
                "class_roots": ["classes"],
            }
        )
    )
    assert (
        main(["session", "start", str(root), "--config", str(config), "--at", f"{SOURCE}:{line}"])
        == 0
    )
    assert main(["session", "get", "start", "--json"]) == 0
    capsys.readouterr()
    assert main(["describe", str(root), "--config", str(config), "--at", SOURCE, "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert line in manifest["breakpoint_lines"][SOURCE]
    # #91: the attached classes' own LocalVariableTable names each method's locals.
    main_scope = next(s for s in manifest["scopes"] if s["scope"] == "PaginationBug.main")
    assert {"args", "page", "pageSize", "items", "start", "end"} <= set(main_scope["names"])
    assert main(["session", "close"]) == 0
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "PAGE:[40, 50, 60]" in output
    assert (
        main(
            [
                "verify",
                str(root),
                "--config",
                str(config),
                "--at",
                SOURCE,
                "--check",
                SOURCE,
                "--old",
                "page * pageSize",
                "--new",
                "(page - 1) * pageSize",
            ]
        )
        == 2
    )
    assert "fresh build" in capsys.readouterr().err


def test_attach_configuration_is_loopback_only_and_bounded(tmp_path):
    from peeky.java_attach import JavaAttachTarget

    base = {"bundle": tmp_path, "port": 5005, "sources": ("A.java",), "class_roots": ("c",)}
    for change in (
        {"host": "10.0.0.5"},
        {"port": 0},
        {"sources": ("/abs/A.java",)},
        {"class_roots": ("../c",)},
        {"sources": ("A.kt",)},
        {"timeout_ms": 999999},
    ):
        with pytest.raises(ValueError):
            JavaAttachTarget(**{**base, **change})
    time.sleep(0)


def test_a_class_rebuilt_while_attaching_is_refused(attached, monkeypatch):
    import peeky.java_attach as java_attach

    root, target, _, _ = attached
    real = java_attach._class_metadata

    def parse_then_rebuild(data, *rest):
        parsed = real(data, *rest)
        for path in (root / "classes").rglob("PaginationBug.class"):
            path.write_bytes(path.read_bytes() + b"\0")  # a later rebuild
        return parsed

    import tempfile

    workspaces = set(Path(tempfile.gettempdir()).glob("peeky-java-attach-*"))
    monkeypatch.setattr(java_attach, "_class_metadata", parse_then_rebuild)
    with pytest.raises(RuntimeError, match="changed during attach") as caught:
        target.prepare(root / SOURCE, root)
    from peeky.errors import classify

    assert classify(caught.value)["code"] == "PROVENANCE_CHANGED"
    assert set(Path(tempfile.gettempdir()).glob("peeky-java-attach-*")) == workspaces


def test_attach_timeouts_fit_the_worker_wait(tmp_path):
    from peeky.java_attach import JavaAttachTarget

    base = {"bundle": tmp_path, "port": 5005, "sources": ("A.java",), "class_roots": ("c",)}
    assert JavaAttachTarget(**base, timeout_ms=15000).timeout_ms == 15000
    with pytest.raises(ValueError, match="1000..15000"):
        JavaAttachTarget(**base, timeout_ms=15001)


def test_declared_sources_behind_an_in_root_link_attach(attached):
    from dataclasses import replace

    root, target, _, _ = attached
    vendor = root / "vendor" / "util"
    vendor.mkdir(parents=True)
    (vendor / "Util.java").write_text(
        "package util;\npublic class Util {\n  static int one() { return 1; }\n}\n"
    )
    (root / "lib").symlink_to(root / "vendor")
    subprocess.run(
        [shutil.which("javac"), "-g", "-d", str(root / "classes"), str(vendor / "Util.java")],
        check=True,
    )
    linked = replace(target, sources=(SOURCE, "lib/util/Util.java"))
    session = linked.prepare(root / SOURCE, root)
    try:
        session.assert_current()
    finally:
        session.close()


def test_attached_jvms_refuse_per_run_arguments(attached, tmp_path):
    from peeky.worker import DebugWorker

    root, target, _, line = attached
    worker = DebugWorker(root, adapter=target)
    with pytest.raises(ValueError, match="already running"):
        worker.dispatch(
            "launch", {"program": SOURCE, "breakpoints": [{"line": line}], "args": ["x"]}
        )


@pytest.mark.timeout(120)
def test_attached_sessions_step_within_the_declared_sources(
    attached, tmp_path, monkeypatch, capsys
):
    import json

    root, target, process, line = attached
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    config = tmp_path / "attach.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(target.bundle),
                "port": target.port,
                "sources": [SOURCE],
                "class_roots": ["classes"],
            }
        )
    )
    start = ["session", "start", str(root), "--config", str(config), "--at", f"{SOURCE}:{line}"]
    assert main(start) == 0
    capsys.readouterr()
    assert main(["session", "step", "--json"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["reason"] == "step" and state["location"].endswith(f"{SOURCE}:{line + 1}")
    assert "outside_program" not in state
    assert main(["session", "get", "end"]) == 0 and "end: int = 6" in capsys.readouterr().out
    assert main(["session", "close"]) == 0
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "PAGE:[40, 50, 60]" in output


# #27: java-debug sometimes never sends `stopped` for an attached JVM although the
# thread sits suspended at the breakpoint. The loss is not reproducible on demand, so
# these tests drop the event at Peeky's DAP client; java-debug itself runs normally.
LOOP_SOURCE = "src/demo/Loop.java"
LOOP = """package demo;

public class Loop {
    static int twice(int n) {
        int doubled = n * 2; // HIT
        return doubled; // NEXT
    }

    public static void main(String[] args) {
        int total = 0;
        for (int i = 1; i <= 3; i++) {
            total += twice(i);
        }
        System.out.println("TOTAL:" + total);
    }
}
"""


@pytest.fixture
def attached_loop(tmp_path):
    target, process, line = attach_to(tmp_path, LOOP_SOURCE, LOOP, "demo.Loop", "// HIT")
    try:
        yield tmp_path, target, process, line
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


class LostStops:
    """Drops chosen `stopped` events before Peeky's DAP client sees them; ``deliver``
    hands them over later (a late report)."""

    def __init__(self, monkeypatch, lose):
        from peeky.dap import DAP

        self.lose, self.lost, self.real = lose, [], DAP._accept

        def accept(dap, body):
            if body.get("type") == "event" and body.get("event") == "stopped":
                if self.lose(body.get("body") or {}, len(self.lost)):
                    self.lost.append((dap, body))
                    return
            self.real(dap, body)

        monkeypatch.setattr(DAP, "_accept", accept)

    def deliver(self):
        for dap, body in self.lost:
            self.real(dap, body)
        self.lost.clear()


def wait_for(worker, session, seconds=20):
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        state = worker.dispatch("wait", {**session, "timeout": 2})
        if state["state"] != "running":
            return state, time.monotonic() - started
    raise AssertionError(f"no stop within {seconds} s")


def local(worker, stop, name):
    snapshot = worker.dispatch("inspect", stop)
    values = {v["name"]: v["value"] for scope in snapshot["scopes"] for v in scope["variables"]}
    return values[name]


@pytest.mark.timeout(120)
def test_an_attached_probe_recovers_a_stop_whose_event_was_lost(attached, monkeypatch):
    root, target, process, line = attached
    lost = LostStops(monkeypatch, lambda body, count: True)
    result = run_probe(
        root, Probe(SOURCE, line, ("start", "pageSize"), (("start", "0"),)), adapter=target
    )
    assert result["status"] == "completed", (result.get("error"), result.get("debugger_events"))
    assert len(lost.lost) == 1 and result["hits"] == 1 and result["recovered_stops"] == 1
    rows = {(r["phase"], r["selector"]): r["value"] for r in result["rows"]}
    assert rows["observed", "start"] == "3" and rows["observed", "pageSize"] == "3"
    assert rows["after-set", "start"] == "0"
    assert any("never reported 1 stop" in warning for warning in result["warnings"])
    # Two polls 2 s apart, not the 60 s timeout.
    assert result["seconds"] < 15
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "PAGE:[10, 20, 30]" in output


@pytest.mark.timeout(120)
def test_a_session_steps_and_continues_after_recovered_stops_and_counts_each_hit_once(
    attached_loop, monkeypatch
):
    from peeky.worker import DebugWorker

    root, target, process, line = attached_loop
    # The reports of hits 1 and 2 are lost, and each arrives late.
    lost = LostStops(
        monkeypatch, lambda body, count: body.get("reason") == "breakpoint" and count < 1
    )
    worker = DebugWorker(root, adapter=target)
    try:
        started = worker.dispatch(
            "launch", {"program": LOOP_SOURCE, "breakpoints": [{"line": line}]}
        )
        session = {"session_id": started["session_id"]}
        state, waited = wait_for(worker, session)
        assert (state["reason"], state["hit"], state.get("recovered")) == ("breakpoint", 1, True)
        assert state["location"].endswith(f"{LOOP_SOURCE}:{line}") and waited < 10
        assert state["event"]["body"]["peeky"].startswith("recovered")
        stop = {**session, "stop_id": state["stop_id"]}
        assert local(worker, stop, "n") == "1"
        # The late report arrives while the recovered stop is current: never a new stop.
        lost.deliver()
        worker.dispatch("step", stop)
        state, _ = wait_for(worker, session)
        assert state["reason"] == "step" and "recovered" not in state and "hit" not in state
        assert state["location"].endswith(f"{LOOP_SOURCE}:{line + 1}")
        assert local(worker, {**session, "stop_id": state["stop_id"]}, "doubled") == "2"
        # Lose the next report too, and deliver it only after the thread runs again.
        lost.lose = lambda body, count: body.get("reason") == "breakpoint" and count < 1
        worker.dispatch("continue", {**session, "stop_id": state["stop_id"]})
        state, _ = wait_for(worker, session)
        assert (state["hit"], state.get("recovered")) == (2, True)
        assert local(worker, {**session, "stop_id": state["stop_id"]}, "n") == "2"
        lost.lose = lambda body, count: False
        worker.dispatch("continue", {**session, "stop_id": state["stop_id"]})
        # Hit 3's own report may come before or after hit 2's late one: either way
        # one is consumed, the other serves hit 3, and nothing is counted twice.
        lost.deliver()
        state, _ = wait_for(worker, session)
        assert (state["hit"], "recovered" in state) == (3, False)
        assert local(worker, {**session, "stop_id": state["stop_id"]}, "n") == "3"
        worker.dispatch("continue", {**session, "stop_id": state["stop_id"]})
        state, _ = wait_for(worker, session)
        assert state["state"] == "terminated" and worker.hits == 3
        operations = [entry["operation"] for entry in worker.audit]
        assert operations.count("stop_recovered") == 2
        assert operations.count("late_stop_report") == 2
    finally:
        worker.close()
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "TOTAL:12" in output


@pytest.mark.timeout(120)
def test_a_thread_whose_step_ends_on_a_breakpoint_line_is_never_taken_for_a_lost_stop(
    attached_loop, monkeypatch
):
    from peeky.worker import DebugWorker

    root, target, process, line = attached_loop
    # The step's own report is lost. It ends on a breakpoint line, verified and set by
    # this session, so the thread looks exactly like a lost breakpoint stop.
    lost = LostStops(monkeypatch, lambda body, count: body.get("reason") == "step")
    worker = DebugWorker(root, adapter=target)
    try:
        points = [{"line": line}, {"line": line + 1}]
        started = worker.dispatch("launch", {"program": LOOP_SOURCE, "breakpoints": points})
        session = {"session_id": started["session_id"]}
        state, _ = wait_for(worker, session)
        assert (state["hit"], "recovered" in state) == (1, False)
        thread = worker.thread_id
        worker.dispatch("step", {**session, "stop_id": state["stop_id"]})
        deadline = time.monotonic() + 7  # three polls
        while time.monotonic() < deadline:
            assert worker.dispatch("wait", {**session, "timeout": 2})["state"] == "running"
        top = worker.dap.request("stackTrace", {"threadId": thread, "levels": 1})["stackFrames"]
        assert top[0]["line"] == line + 1 and len(lost.lost) == 1
        assert lost.lost[0][1]["body"]["reason"] == "step"
        assert not any(entry["operation"] == "stop_recovered" for entry in worker.audit)
        assert worker.hits == 1
    finally:
        worker.close()
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "TOTAL:12" in output
