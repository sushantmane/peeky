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


def lose_stops(monkeypatch, lose):
    """Drop the `stopped` events ``lose(body)`` picks before Peeky's DAP client sees
    them. Returns the list of dropped event bodies."""
    from peeky.dap import DAP

    lost, real = [], DAP._accept

    def accept(dap, body):
        if body.get("type") == "event" and body.get("event") == "stopped":
            if lose(body.get("body") or {}):
                lost.append(body.get("body") or {})
                return
        real(dap, body)

    monkeypatch.setattr(DAP, "_accept", accept)
    return lost


def wait_for(worker, session, seconds=20):
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        state = worker.dispatch("wait", {**session, "timeout": 2})
        if state["state"] != "running":
            return state
    raise AssertionError(f"no stop within {seconds} s")


@pytest.mark.timeout(120)
def test_an_attached_probe_fails_fast_when_a_stop_is_never_reported(attached, monkeypatch):
    root, target, process, line = attached
    lost = lose_stops(monkeypatch, lambda body: True)
    probe = Probe(SOURCE, line, ("start", "pageSize"), (("start", "0"),))
    result = run_probe(root, probe, adapter=target)
    assert result["status"] == "failed" and len(lost) == 1
    error = result["error"]
    assert error["code"] == "DEBUGGER_ERROR" and error["session_closed"] is True
    assert error["message"].startswith("Lost stop: java-debug never reported a stop it made")
    for evidence in (
        "'main'",
        "PaginationBug.main",
        f"PaginationBug.java:{line}",
        f"{SOURCE}:{line}",
    ):
        assert evidence in error["message"]
    assert "close" in error["hint"]
    # Nothing was read or changed, and in seconds rather than the 60 s timeout.
    assert result["hits"] == 0 and result["rows"] == [] and result["interventions"] == []
    assert result["seconds"] < 9
    trace = result["debugger_events"]["trace"]
    assert any(entry["name"] == "lost stop" for entry in trace)
    # Peeky detached: the JVM ran on, unchanged.
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "PAGE:[40, 50, 60]" in output


@pytest.mark.timeout(120)
def test_a_session_whose_stop_is_never_reported_fails_and_serves_nothing_more(
    attached_loop, monkeypatch
):
    from peeky.errors import classify
    from peeky.worker import DebugWorker, LostStop

    root, target, process, line = attached_loop
    # Hit 1 is reported and served; hit 2's report is lost.
    reports = []
    lost = lose_stops(monkeypatch, lambda body: reports.append(body) or len(reports) == 2)
    worker = DebugWorker(root, adapter=target)
    try:
        started = worker.dispatch(
            "launch", {"program": LOOP_SOURCE, "breakpoints": [{"line": line}]}
        )
        session = {"session_id": started["session_id"]}
        state = wait_for(worker, session)
        assert state["hit"] == 1
        worker.dispatch("continue", {**session, "stop_id": state["stop_id"]})
        began = time.monotonic()
        with pytest.raises(LostStop) as failure:
            wait_for(worker, session)
        assert time.monotonic() - began < 9 and len(lost) == 1
        error = classify(failure.value)
        assert error["code"] == "DEBUGGER_ERROR"
        assert f"Loop.twice(int) at Loop.java:{line}" in error["message"]
        assert f"{LOOP_SOURCE}:{line}" in error["message"]
        # The session is detached, serves nothing, and repeats its error until closed.
        for operation, extra in (("wait", {"timeout": 2}), ("inspect", {"stop_id": "x"})):
            with pytest.raises(LostStop) as again:
                worker.dispatch(operation, {**session, **extra})
            assert str(again.value) == str(failure.value)
        report = worker.dispatch("report", session)
        assert report["state"] == "detached" and report["lost_stop"]["breakpoint"] == (
            f"{LOOP_SOURCE}:{line}"
        )
        assert [entry["operation"] for entry in report["history"]].count("lost_stop") == 1
        assert any(e["name"] == "lost stop" for e in report["debugger_events"]["trace"])
        assert worker.hits == 1
        assert worker.dispatch("close", session) == {"state": "idle"}
    finally:
        worker.close()
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "TOTAL:12" in output


@pytest.mark.timeout(120)
def test_a_step_whose_report_is_lost_on_a_breakpoint_line_is_never_a_lost_stop(
    attached_loop, monkeypatch
):
    from peeky.worker import DebugWorker

    root, target, process, line = attached_loop
    # The step's own report is lost. It ends on a breakpoint line, verified and set by
    # this session, so the thread looks exactly like a lost breakpoint stop.
    lost = lose_stops(monkeypatch, lambda body: body.get("reason") == "step")
    worker = DebugWorker(root, adapter=target)
    try:
        points = [{"line": line}, {"line": line + 1}]
        started = worker.dispatch("launch", {"program": LOOP_SOURCE, "breakpoints": points})
        session = {"session_id": started["session_id"]}
        state = wait_for(worker, session)
        assert state["hit"] == 1
        thread = worker.thread_id
        worker.dispatch("step", {**session, "stop_id": state["stop_id"]})
        deadline = time.monotonic() + 9  # four polls: more than an error needs
        while time.monotonic() < deadline:
            assert worker.dispatch("wait", {**session, "timeout": 2})["state"] == "running"
        top = worker.dap.request("stackTrace", {"threadId": thread, "levels": 1})["stackFrames"]
        assert top[0]["line"] == line + 1 and [body["reason"] for body in lost] == ["step"]
        assert "lost_stop" not in [entry["operation"] for entry in worker.audit]
    finally:
        worker.close()
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "TOTAL:12" in output


STORM_SOURCE = "src/demo/Storm.java"
STORM = (
    """package demo;

import java.net.URL;
import java.net.URLClassLoader;

public class Storm {
    static final Object LOCK = new Object();

    public static class Gen {
"""
    + "".join(f"        static int m{i}(int a) {{ return a + {i}; }}\n" for i in range(800))
    + """    }

    static void contend() {
        int local = 42;
        synchronized (LOCK) {
            System.out.println("waiter got the lock " + local); // BP
        }
    }

    static void hold() {
        synchronized (LOCK) {
            try {
                Thread.sleep(Long.MAX_VALUE);
            } catch (InterruptedException e) {
                return;
            }
        }
    }

    public static void main(String[] args) throws Exception {
        Thread holder = new Thread(Storm::hold, "holder");
        holder.setDaemon(true);
        holder.start();
        Thread.sleep(300);
        Thread waiter = new Thread(Storm::contend, "waiter");
        waiter.setDaemon(true);
        waiter.start();
        URL url = Storm.class.getProtectionDomain().getCodeSource().getLocation();
        long end = System.nanoTime() + 10_000_000_000L;
        int loads = 0;
        while (System.nanoTime() < end) {
            try (URLClassLoader loader = new URLClassLoader(new URL[] {url}, null)) {
                Class.forName("demo.Storm$Gen", true, loader);
            }
            loads++;
        }
        System.out.println("LOADS:" + loads + " waiter:" + waiter.getState());
    }
}
"""
)


@pytest.mark.timeout(120)
def test_a_breakpoint_that_never_ran_is_never_a_lost_stop_under_a_class_load_storm(
    tmp_path, monkeypatch
):
    # The review's H1. java-debug's class-load requests for a breakpoint's class and
    # its nested classes suspend every thread while it looks for the line; a thread
    # blocked entering `synchronized` then shows the line inside the block. Loading a
    # nested class with 800 methods over and over keeps that happening.
    from peeky.dap import DAP

    target, process, line = attach_to(tmp_path, STORM_SOURCE, STORM, "demo.Storm", "// BP")
    real, seen = DAP.request, []

    def request(dap, command, arguments=None, **options):
        reply = real(dap, command, arguments, **options)
        if command == "stackTrace" and options.get("trace") is False:
            frames = reply.get("stackFrames") or []
            if frames and frames[0].get("line") == line:
                seen.append(time.monotonic())
        return reply

    monkeypatch.setattr(DAP, "request", request)
    try:
        result = run_probe(
            tmp_path, Probe(STORM_SOURCE, line, ("local",), timeout=40), adapter=target
        )
        assert (result["status"], result["hits"], result["rows"]) == ("no-stop", 0, [])
        assert result["error"]["code"] == "NO_STOP"
        # The hazard was real: polls found the waiter suspended at the breakpoint line.
        assert len(seen) >= 3, seen
        output = process.stdout.read()
        assert process.wait(timeout=30) == 0 and "waiter:BLOCKED" in output
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
