"""Real Microsoft Java core conformance; explicitly enabled by prepared bundle path."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from peeky.dap import DAP
from peeky.worker import _process_alive


@pytest.fixture(scope="module")
def bundle():
    configured = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not configured:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    root = Path(configured)
    manifest = json.loads((root / "adapter.json").read_text())
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
    return manifest


def prepare(tmp_path, nested):
    directory = tmp_path / "demo"
    directory.mkdir()
    source = directory / "BranchBug.java"
    if nested:
        text = """package demo;
public class BranchBug {
  public static void main(String[] args) { Inner.run(); }
  static class Inner {
    static void run() {
      int timeoutMs = 0;
      String label = "request";
      if (timeoutMs > 0) System.out.println(label + ":success");
      else System.out.println(label + ":fallback");
    }
  }
}
"""
        line, binary = 8, "demo.BranchBug$Inner"
    else:
        text = """package demo;
public class BranchBug {
  public static void main(String[] args) {
    int timeoutMs = 0;
    String label = "request";
    if (timeoutMs > 0) System.out.println(label + ":success");
    else System.out.println(label + ":fallback");
  }
}
"""
        line, binary = 6, "demo.BranchBug"
    source.write_text(text)
    classes = tmp_path / "classes"
    subprocess.run(["javac", "-g", "-d", str(classes), str(source)], check=True, timeout=15)
    # Assert this fixture's explicit map against compiler debug metadata, not name heuristics.
    debug = subprocess.check_output(
        ["javap", "-classpath", str(classes), "-l", "-p", binary], text=True, timeout=15
    )
    assert 'Compiled from "BranchBug.java"' in debug
    assert f"line {line}:" in debug
    entries = {binary: {"path": str(source), "lines": [line]}}
    if nested:
        entries["demo.BranchBug"] = {"path": str(source), "lines": [3]}
    mapping = tmp_path / "source-map.json"
    mapping.write_text(json.dumps({"classes": entries}))
    return source, classes, mapping, line


def wait_for_exit(dap, timeout=30):
    """The JVM's exit: `terminated`/`exited`, or its announced pid gone.

    java-debug can drop its exit events for a JVM that is gone. The worker infers
    the exit the same way (`debuggee_gone`); this test drives DAP directly, so it must too.
    Without a pid, or with the JVM still alive, it fails at the deadline.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return dap.wait_event({"terminated", "exited"}, timeout=2)["event"]
        except TimeoutError:
            if dap.debuggee_pid and not _process_alive(dap.debuggee_pid):
                return "exit inferred: the JVM is gone"
            if time.monotonic() >= deadline:
                raise


@pytest.mark.parametrize("nested", [False, True], ids=["package", "nested-class"])
def test_java_runtime_intervention(bundle, tmp_path, nested):
    source, classes, mapping, line = prepare(tmp_path, nested)
    protected = [source, *classes.rglob("*.class")]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    baseline = subprocess.check_output(
        ["java", "-cp", str(classes), "demo.BranchBug"], text=True, timeout=10
    )
    assert baseline == "request:fallback\n"
    dap = DAP(command=[*bundle["command"], str(mapping)])
    try:
        caps = dap.request(
            "initialize",
            {
                "adapterID": "java",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
            },
        )
        launcher = Path(os.environ["PEEKY_JAVA_ADAPTER"]) / "classes" / "PeekyJavaAdapter.class"
        assert caps == {
            "supportsConfigurationDoneRequest": True,
            "supportsSetVariable": True,
            "peekySteps": True,
            "peekyFieldSet": True,
            # Where the launcher has it: a String takes a text (older bundles say to rebuild).
            **({"peekyTextSet": True} if b"peekyTextSet" in launcher.read_bytes() else {}),
            # A char takes a quoted char.
            **({"peekyCharSet": True} if b"peekyCharSet" in launcher.read_bytes() else {}),
            # Each listed field's declaring class.
            **({"peekyJdkOwners": True} if b"peekyJdkOwners" in launcher.read_bytes() else {}),
            # What holds an array, before its elements are marked.
            **(
                {"peekyArrayHolders": True} if b"peekyArrayHolders" in launcher.read_bytes() else {}
            ),
        }
        # Steps are allowed with a positive integer thread only; step-in targets, step back
        # and goto are not.
        for arguments in ({}, {"threadId": "1"}, {"threadId": True}, {"threadId": None}):
            with pytest.raises(RuntimeError, match="numeric threadId"):
                dap.request("stepIn", arguments)
        for thread in (1.5, -1, 0, 1e300, 2**63):
            with pytest.raises(RuntimeError, match="positive integer"):
                dap.request("next", {"threadId": thread})
        for command in ("evaluate", "completions", "restart", "stepBack", "stepInTargets", "goto"):
            with pytest.raises(RuntimeError, match="unsupported command"):
                dap.request(command, {})
        if caps.get("peekyArrayHolders"):
            # Peeky's own request takes an array's reference only, and never reaches java-debug.
            with pytest.raises(RuntimeError, match="requires a numeric variablesReference"):
                dap.request("peekyArrayHolder", {})
        for arguments, message in (
            ({}, "requires loopback"),
            ({"hostName": "10.0.0.5", "port": 5005}, "loopback-only"),
            ({"hostName": "127.0.0.1", "port": 0}, "invalid attach port"),
        ):
            with pytest.raises(RuntimeError, match=message):
                dap.request("attach", arguments)
        launch = dap.send("launch", {"mainClass": "demo.BranchBug", "classPaths": [str(classes)]})
        dap.wait_event({"initialized"})
        for field in ("condition", "logMessage", "hitCondition"):
            with pytest.raises(RuntimeError, match="unsupported"):
                dap.request(
                    "setBreakpoints",
                    {
                        "source": {"path": str(source)},
                        "breakpoints": [{"line": line, field: "true"}],
                    },
                )
        with pytest.raises(RuntimeError):
            dap.request(
                "setBreakpoints", {"source": {"path": str(source)}, "breakpoints": [{"line": 1}]}
            )
        dap.request(
            "setBreakpoints", {"source": {"path": str(source)}, "breakpoints": [{"line": line}]}
        )
        dap.request("configurationDone")
        dap.response(launch)
        stop = dap.wait_event({"stopped"})
        verified = dap.wait_event({"breakpoint"})
        assert verified["body"]["breakpoint"]["verified"] is True
        assert verified["body"]["breakpoint"]["line"] == line
        tid = stop["body"]["threadId"]
        frame = dap.request("stackTrace", {"threadId": tid})["stackFrames"][0]
        assert frame["line"] == line
        assert Path(frame["source"]["path"]) == source
        ref = dap.request("scopes", {"frameId": frame["id"]})["scopes"][0]["variablesReference"]
        variables = dap.request("variables", {"variablesReference": ref})["variables"]
        assert next(v for v in variables if v["name"] == "timeoutMs")["value"] == "0"
        assert next(v for v in variables if v["name"] == "label")["value"] == '"request"'
        with pytest.raises(RuntimeError, match="scalar literals"):
            dap.request(
                "setVariable",
                {"variablesReference": ref, "name": "timeoutMs", "value": "System.exit(0)"},
            )
        changed = dap.request(
            "setVariable", {"variablesReference": ref, "name": "timeoutMs", "value": "5000"}
        )
        assert changed["value"] == "5000"
        dap.request("continue", {"threadId": tid})
        wait_for_exit(dap)
        deadline = time.monotonic() + 5
        while "request:success" not in dap.snapshot_output()[0] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "request:success" in dap.snapshot_output()[0]
    finally:
        dap.close()
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    assert dap.process.poll() is not None


def test_an_array_write_waits_no_longer_than_the_pause_budget_allows(bundle, tmp_path):
    # Before writing an element the launcher has the JVM
    # search its heap for what refers to the array, which nothing can interrupt; it waits
    # at most what the session's pause budget leaves, and refuses when that is too little.
    if (
        b"peekyJdkOwners"
        not in (
            Path(os.environ["PEEKY_JAVA_ADAPTER"]) / "classes" / "PeekyJavaAdapter.class"
        ).read_bytes()
    ):
        pytest.skip("a launcher from before #204 does not search")
    directory = tmp_path / "demo"
    directory.mkdir()
    source = directory / "Cells.java"
    source.write_text(
        """package demo;
public class Cells {
  public static void main(String[] args) {
    int[] cells = {1, 2, 3};
    System.out.println("CELLS:" + cells[0]);
  }
}
"""
    )
    classes = tmp_path / "classes"
    subprocess.run(["javac", "-g", "-d", str(classes), str(source)], check=True, timeout=15)
    mapping = tmp_path / "source-map.json"
    entries = {"demo.Cells": {"path": str(source), "lines": [3, 4, 5, 6]}}
    mapping.write_text(json.dumps({"classes": entries}))
    dap = DAP(command=[*bundle["command"], str(mapping)])
    try:
        dap.request(
            "initialize",
            {"adapterID": "java", "pathFormat": "path", "linesStartAt1": True},
        )
        launch = dap.send("launch", {"mainClass": "demo.Cells", "classPaths": [str(classes)]})
        dap.wait_event({"initialized"})
        dap.request(
            "setBreakpoints", {"source": {"path": str(source)}, "breakpoints": [{"line": 5}]}
        )
        dap.request("configurationDone")
        dap.response(launch)
        tid = dap.wait_event({"stopped"})["body"]["threadId"]
        frame = dap.request("stackTrace", {"threadId": tid})["stackFrames"][0]
        scope = dap.request("scopes", {"frameId": frame["id"]})["scopes"][0]["variablesReference"]
        listed = dap.request("variables", {"variablesReference": scope})["variables"]
        cells = next(v for v in listed if v["name"] == "cells")["variablesReference"]
        write = {"variablesReference": cells, "name": "0", "value": "7"}
        with pytest.raises(RuntimeError, match="s of the session's pause budget is left"):
            dap.request("setVariable", {**write, "peekyPauseLeftMs": 900})
        assert dap.request("setVariable", {**write, "peekyPauseLeftMs": 5000})["value"] == "7"
        dap.request("continue", {"threadId": tid})
        wait_for_exit(dap)
        deadline = time.monotonic() + 5
        while "CELLS:7" not in dap.snapshot_output()[0] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "CELLS:7" in dap.snapshot_output()[0]
    finally:
        dap.close()


def test_a_dropped_exit_event_is_inferred_only_when_the_jvm_is_gone():
    class Silent:
        """An adapter that never sends an exit event."""

        def __init__(self, pid):
            self.debuggee_pid = pid

        def wait_event(self, names, timeout):
            raise TimeoutError

    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    assert wait_for_exit(Silent(finished.pid), timeout=0).startswith("exit inferred")
    for alive_or_unknown in (os.getpid(), None):
        with pytest.raises(TimeoutError):
            wait_for_exit(Silent(alive_or_unknown), timeout=0)


RESUME_HARNESS = """
import com.sun.jdi.InternalException;
import com.sun.jdi.VMDisconnectedException;
import com.sun.jdi.VirtualMachine;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.lang.reflect.Proxy;

/** Calls the launcher's resume with a VirtualMachine that fails as ``args[0]`` says. */
public class ResumeHarness {
    public static void main(String[] args) {
        String mode = args[0];
        StringBuilder calls = new StringBuilder();
        VirtualMachine vm = (VirtualMachine) Proxy.newProxyInstance(
            VirtualMachine.class.getClassLoader(), new Class<?>[] {VirtualMachine.class},
            (proxy, method, arguments) -> {
                calls.append(method.getName()).append(' ');
                if (method.getName().equals("resume")) {
                    if (mode.equals("gone")) throw new VMDisconnectedException();
                    if (!mode.equals("runs")) throw new InternalException("Unexpected JDWP Error: 113", 113);
                }
                if (method.getName().equals("dispose") && mode.equals("stuck"))
                    throw new InternalException("Unexpected JDWP Error: 112", 112);
                return null;
            });
        ByteArrayOutputStream err = new ByteArrayOutputStream();
        boolean resumed = PeekyJavaAdapter.resume(vm, new PrintStream(err, true));
        System.out.println(resumed + " " + calls.toString().trim());
        System.out.print(err);
    }
}
"""


@pytest.mark.parametrize(
    "mode, resumed, calls",
    [
        ("runs", True, "resume"),
        # A JVM gone already has nothing left to resume.
        ("gone", True, "resume"),
        # Any other failure: disconnect (which resumes every thread), and say so.
        ("broken", False, "resume dispose"),
        ("stuck", False, "resume dispose"),
    ],
)
def test_a_jvm_the_launcher_cannot_resume_is_disconnected_and_ends_the_session(
    bundle, tmp_path, mode, resumed, calls
):
    # A failure to resume the JVM after a change is no disconnect, which would leave an
    # attached JVM with every thread suspended.
    import re

    from peeky.java import JavaSession

    root = Path(os.environ["PEEKY_JAVA_ADAPTER"])
    if (
        b"could not resume the JVM"
        not in (root / "classes" / "PeekyJavaAdapter.class").read_bytes()
    ):
        pytest.skip("a launcher from before #261 suspends no thread for a change")
    (tmp_path / "ResumeHarness.java").write_text(RESUME_HARNESS)
    path = os.pathsep.join([str(root / "classes"), str(root / "lib" / "*"), str(tmp_path)])
    subprocess.run(
        ["javac", "-cp", path, "-d", str(tmp_path), str(tmp_path / "ResumeHarness.java")],
        check=True,
        timeout=120,
    )
    shown = subprocess.run(
        ["java", "-cp", path, "ResumeHarness", mode],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    first, _, stderr = shown.partition("\n")
    assert first == f"{str(resumed).lower()} {calls}", shown
    if resumed:
        assert stderr == "", stderr
        return
    # The line Peeky's worker ends the session on, with the failure's own trace.
    [(pattern, _)] = [(p, m) for p, m in JavaSession.fatal_stderr.items() if "resume" in p]
    assert re.search(pattern, stderr), stderr
    assert stderr.startswith(
        "Peeky experimental Java adapter: could not resume the JVM after a change "
        "(com.sun.jdi.InternalException: Unexpected JDWP Error: 113); "
    ), stderr
    after = (
        "disconnected from it, which resumes every thread the debugger suspended"
        if mode == "broken"
        else "disconnecting failed too (com.sun.jdi.InternalException: Unexpected JDWP Error: "
        "112); ending the launcher closes the connection"
    )
    assert stderr.splitlines()[0].endswith(after), stderr
    assert "\tat PeekyJavaAdapter.resume(PeekyJavaAdapter.java:" in stderr, stderr
