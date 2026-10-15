"""Real Microsoft Java core conformance; explicitly enabled by prepared bundle path."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from peeky.dap import DAP


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
        assert caps == {"supportsConfigurationDoneRequest": True, "supportsSetVariable": True}
        for command in ("evaluate", "completions", "restart", "stepIn"):
            with pytest.raises(RuntimeError, match="unsupported command"):
                dap.request(command, {})
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
        dap.wait_event({"terminated"})
        assert "request:success" in dap.snapshot_output()[0]
    finally:
        dap.close()
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    assert dap.process.poll() is not None
