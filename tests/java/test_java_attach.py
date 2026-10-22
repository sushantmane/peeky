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


def attach_to(tmp_path, source, text, main_class, marker, jvm_args=()):
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
        [java, *jvm_args, agent, "-cp", str(tmp_path / "classes"), main_class],
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
    # The JVM's other threads run on at a line breakpoint: no element is promised
    # (#261 review), though the change is made.
    assert {r["phase"]: (r["settable"], r.get("maybe_settable")) for r in result["rows"]} == {
        "observed": (False, "other threads are running; set checks again"),
        "after-set": (False, "other threads are running; set checks again"),
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


@pytest.mark.timeout(120)
def test_a_failed_attach_detaches_and_never_ends_the_jvm(attached, monkeypatch):
    from peeky.errors import classify
    from peeky.java import JavaSession
    from peeky.worker import DebugWorker

    root, target, process, line = attached

    def expire(self, program, points):
        raise TimeoutError("Launch exceeded its deadline; session discarded")

    # The attach times out once java-debug is attached to the JVM.
    monkeypatch.setattr(JavaSession, "check_breakpoint_lines", expire)
    worker = DebugWorker(root, adapter=target)
    with pytest.raises(TimeoutError) as caught:
        worker.dispatch("launch", {"program": SOURCE, "breakpoints": [{"line": line}]})
    error = classify(caught.value)
    # Peeky did not start this JVM (#143): it detaches, and the JVM runs on to its end.
    assert error["code"] == "TIMEOUT" and "did not start" not in error["message"]
    assert error["cleanup"] == "detached; the attached program keeps running"
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "PAGE:[40, 50, 60]" in output


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
        detached = "detached; the attached program keeps running"
        assert error["cleanup"] == detached
        # The session is detached, serves nothing, and repeats its error until closed.
        for operation, extra in (("wait", {"timeout": 2}), ("inspect", {"stop_id": "x"})):
            with pytest.raises(LostStop) as again:
                worker.dispatch(operation, {**session, **extra})
            assert str(again.value) == str(failure.value)
            assert classify(again.value)["cleanup"] == detached
        report = worker.dispatch("report", session)
        assert report["state"] == "detached" and report["lost_stop"]["breakpoint"] == (
            f"{LOOP_SOURCE}:{line}"
        )
        assert [entry["operation"] for entry in report["history"]].count("lost_stop") == 1
        assert any(e["name"] == "lost stop" for e in report["debugger_events"]["trace"])
        assert worker.hits == 1 and report["cleanup"] == detached
        assert worker.dispatch("close", session) == {"state": "idle", "cleanup": detached}
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


# #27's root cause: java-debug's "Event Hub" thread died on ObjectCollectedException while
# building a log line naming a short-lived thread the JVM had already collected, and no
# JVM event was processed again. Two threads starting short-lived threads in a 32 MB heap
# with constant garbage reproduce it at once with the unpatched adapter (review round 2,
# work/vr27/churn/drive6.py); setup_java_adapter.py now patches it (eventhub-log-guard).
CHURN_SOURCE = "src/demo/Churn.java"
CHURN = """package demo;

public class Churn {
    static class Hit {
        static int warm() { return 1; }

        static int hit(int n) {
            int doubled = n * 2; // BP
            return doubled;
        }
    }

    static volatile Object sink;

    public static void main(String[] args) throws Exception {
        Hit.warm();
        Runnable empty = () -> {};
        for (int c = 0; c < 2; c++) {
            Thread t = new Thread(() -> {
                long stop = System.nanoTime() + 3_000_000_000L;
                while (System.nanoTime() < stop) {
                    Thread s = new Thread(empty);
                    s.start();
                    try {
                        s.join();
                        Thread.sleep(1);
                    } catch (InterruptedException e) {
                        return;
                    }
                }
            }, "churner-" + c);
            t.setDaemon(true);
            t.start();
        }
        Thread garbage = new Thread(() -> { while (true) sink = new byte[64 * 1024]; }, "garbage");
        garbage.setDaemon(true);
        garbage.start();
        Thread.sleep(5000);
        System.out.println("HIT:" + Hit.hit(21));
    }
}
"""


@pytest.fixture
def churning(tmp_path):
    target, process, line = attach_to(
        tmp_path, CHURN_SOURCE, CHURN, "demo.Churn", "// BP", jvm_args=("-Xmx32m",)
    )
    try:
        yield tmp_path, target, process, line
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


@pytest.mark.timeout(120)
def test_short_lived_threads_no_longer_lose_the_stop(churning):
    from peeky.worker import DebugWorker

    root, target, process, line = churning
    worker = DebugWorker(root, adapter=target)
    try:
        started = worker.dispatch(
            "launch", {"program": CHURN_SOURCE, "breakpoints": [{"line": line}]}
        )
        session = {"session_id": started["session_id"]}
        state = wait_for(worker, session)
        assert (state["reason"], state["hit"]) == ("breakpoint", 1)
        assert state["location"].endswith(f"{CHURN_SOURCE}:{line}")
        snapshot = worker.dispatch("inspect", {**session, "stop_id": state["stop_id"]})
        values = {v["name"]: v["value"] for scope in snapshot["scopes"] for v in scope["variables"]}
        assert values["n"] == "21"
        # Reported by java-debug itself, within milliseconds: nothing was detected as lost.
        assert "lost_stop" not in [entry["operation"] for entry in worker.audit]
        stderr = worker.dispatch("report", session)["debugger_events"]["stderr"]
        assert 'Exception in thread "Event Hub"' not in stderr
        worker.dispatch("continue", {**session, "stop_id": state["stop_id"]})
        assert wait_for(worker, session)["state"] == "terminated"
    finally:
        worker.close()
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "HIT:42" in output


@pytest.mark.timeout(120)
def test_an_unpatched_adapters_dead_event_hub_ends_the_session_at_once(churning, monkeypatch):
    # The real failure, kept reproducible: point PEEKY_JAVA_ADAPTER_UNPATCHED at a bundle
    # built before eventhub-log-guard (Peeky refuses such bundles; the test allows it).
    unpatched = os.environ.get("PEEKY_JAVA_ADAPTER_UNPATCHED")
    if not unpatched:
        pytest.skip("set PEEKY_JAVA_ADAPTER_UNPATCHED to a bundle built without the patch")
    from dataclasses import replace

    import peeky.java
    from peeky.errors import classify
    from peeky.worker import DebugWorker

    monkeypatch.setattr(peeky.java, "JAVA_DEBUG_PATCHES", {})
    root, target, process, line = churning
    worker = DebugWorker(root, adapter=replace(target, bundle=Path(unpatched)))
    began = time.monotonic()
    with pytest.raises(RuntimeError) as failure:
        started = worker.dispatch(
            "launch", {"program": CHURN_SOURCE, "breakpoints": [{"line": line}]}
        )
        wait_for(worker, {"session_id": started["session_id"]})
    # Before the breakpoint is even reached, not after a 60 s timeout.
    assert time.monotonic() - began < 5
    error = classify(failure.value)
    assert error["code"] == "DEBUGGER_ERROR"
    assert error["message"].startswith(
        'java-debug\'s event thread died (Exception in thread "Event Hub" '
        "com.sun.jdi.ObjectCollectedException at com.microsoft.java.debug.core.EventHub"
    )
    assert worker.dap is None
    # Detached: the JVM runs on, its breakpoint gone.
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "HIT:42" in output


@pytest.mark.timeout(120)
def test_an_unattended_session_fails_fast_on_a_lost_stop_at_a_breakpoint_pending_at_launch(
    attached_loop, monkeypatch
):
    # Review round 4: attaching to a suspend=y JVM (Gradle's --debug-jvm), the breakpoint
    # is pending until its class loads and is verified by a `breakpoint` event. With no
    # client waiting, only the pause guard runs: it must still see the lost stop.
    from peeky.worker import DebugWorker, LostStop

    root, target, process, line = attached_loop
    lost = lose_stops(monkeypatch, lambda body: True)
    worker = DebugWorker(root, adapter=target)
    try:
        launch = {"program": LOOP_SOURCE, "breakpoints": [{"line": line}], "max_pause": 3}
        started = worker.dispatch("launch", launch)
        assert [point["verified"] for point in started["breakpoints"]] == [False]
        began = time.monotonic()
        while worker.failed is None and time.monotonic() - began < 20:
            worker.guard_pause()
            time.sleep(0.25)
        assert worker.failed and worker.failed["error"] is LostStop and len(lost) == 1
        assert time.monotonic() - began < 10
        with pytest.raises(LostStop):
            worker.dispatch("wait", {"session_id": started["session_id"], "timeout": 1})
    finally:
        worker.close()
    # Detached: the JVM ran on.
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "TOTAL:12" in output


@pytest.mark.timeout(120)
def test_a_waiting_client_receives_every_stop_exactly_once_beside_the_pause_guard(
    attached_loop,
):
    # The guard drains `breakpoint` events and polls between a client's waits; the
    # worker lock serializes them. Its own internal `wait` may take a stop first, but
    # the client's next `wait` still reports it, once, with the same stop_id and hit.
    import threading

    from peeky.worker import DebugWorker

    root, target, process, line = attached_loop
    worker = DebugWorker(root, adapter=target)
    stop = threading.Event()

    def guard():
        while not stop.is_set():
            worker.guard_pause()
            time.sleep(0.01)

    try:
        launch = {"program": LOOP_SOURCE, "breakpoints": [{"line": line}], "max_pause": 30}
        session = {"session_id": worker.dispatch("launch", launch)["session_id"]}
        ticking = threading.Thread(target=guard, daemon=True)
        ticking.start()
        hits, stops, verified = [], [], False
        while True:
            state = wait_for(worker, session)
            verified |= any(
                update.get("breakpoint", {}).get("verified") is True
                for update in state.get("breakpoint_events", [])
            )
            if state["state"] == "terminated":
                break
            hits.append((state["reason"], state["hit"]))
            stops.append(state["stop_id"])
            worker.dispatch("continue", {**session, "stop_id": state["stop_id"]})
        assert hits == [("breakpoint", 1), ("breakpoint", 2), ("breakpoint", 3)] and verified
        assert len(set(stops)) == 3
        assert worker.failed is None and not worker.auto_resumed
    finally:
        stop.set()
        worker.close()
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "TOTAL:12" in output


@pytest.mark.timeout(120)
def test_the_pause_guard_alone_ends_a_session_whose_event_thread_died(churning, monkeypatch):
    # Review round 4: nothing noticed the dead event thread with no client waiting.
    unpatched = os.environ.get("PEEKY_JAVA_ADAPTER_UNPATCHED")
    if not unpatched:
        pytest.skip("set PEEKY_JAVA_ADAPTER_UNPATCHED to a bundle built without the patch")
    from dataclasses import replace

    import peeky.java
    from peeky.worker import DebugWorker, SessionFailed

    monkeypatch.setattr(peeky.java, "JAVA_DEBUG_PATCHES", {})
    root, target, process, line = churning
    worker = DebugWorker(root, adapter=replace(target, bundle=Path(unpatched)))
    launch = {"program": CHURN_SOURCE, "breakpoints": [{"line": line}], "max_pause": 3}
    session = {"session_id": worker.dispatch("launch", launch)["session_id"]}
    began = time.monotonic()
    while worker.failed is None and time.monotonic() - began < 20:
        worker.guard_pause()
        time.sleep(0.25)
    assert worker.failed and worker.failed["error"] is SessionFailed
    assert time.monotonic() - began < 5 and worker.dap is None
    with pytest.raises(SessionFailed, match="event thread died"):
        worker.dispatch("wait", {**session, "timeout": 1})
    assert worker.dispatch("report", session)["history"][-1]["operation"] == "adapter_failed"
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "HIT:42" in output
