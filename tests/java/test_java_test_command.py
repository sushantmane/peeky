"""`--test` with a `java` command (ADR 0009): Peeky starts the test's JVM with its agent,
finds it among the user's JVMs by the options it added, proves the attach reached it,
probes, and lets the JVM run to its end. A JVM another run left waiting is never touched."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from peeky.cli import main

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "src/demo/PaginationBug.java"


def install_bundle(state):
    """Peeky's own bundle in ``state``, where `peeky setup-java` puts it: `--test` has no
    config to name another."""
    source = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not source:
        pytest.skip("set PEEKY_JAVA_ADAPTER to enable Java conformance")
    container = state / "java-adapter"
    version = container / ("0" * 20 + "-" + "0" * 8)
    shutil.copytree(source, version)
    manifest = version / "adapter.json"
    manifest.write_text(manifest.read_text().replace(str(Path(source).resolve()), str(version)))
    for directory in (state, container, version):
        directory.chmod(0o700)
    pointer = container / "current"
    pointer.write_text(version.name + "\n")
    pointer.chmod(0o600)


@pytest.fixture
def project(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    install_bundle(state)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(state))
    root = tmp_path / "project"
    (root / "src/demo").mkdir(parents=True)
    shutil.copy(ROOT / "examples/java/demo/PaginationBug.java", root / SOURCE)
    subprocess.run([shutil.which("javac"), "-g", "-d", "classes", SOURCE], cwd=root, check=True)
    monkeypatch.chdir(root)
    text = (root / SOURCE).read_text().splitlines()
    line = next(n for n, row in enumerate(text, 1) if "// OBSERVE" in row)
    return root, line


def probe(capsys, at, *words, more=()):
    code = main(
        ["probe", ".", "--at", at, "--capture", "start,pageSize", "--json", *more, "--test", "--"]
        + list(words)
    )
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(300)
def test_a_java_command_runs_under_peeky_and_ends_on_its_own(project, capsys):
    root, line = project
    code, result = probe(capsys, f"{SOURCE}:{line}", "java", "-cp", "classes", "demo.PaginationBug")
    assert code == 0, result
    rows = {r["selector"]: r["value"] for r in result["rows"]}
    assert rows == {"start": "3", "pageSize": "3"}
    assert result["target_identity"]["mode"] == "attach"
    test = result["test"]
    assert test["command"] == "java -cp classes demo.PaginationBug" and test["tool"] == "java"
    assert test["exit_code"] == 0 and "stopped" not in test
    assert test["jvm"]["port"] > 0 and "PAGE:[40, 50, 60]" in test["output_tail"]
    assert Path(test["log"]).parent == Path(os.environ["PEEKY_STATE_DIR"]) / "tests"


@pytest.mark.timeout(300)
def test_a_jvm_left_waiting_on_its_own_port_is_never_attached_to(project, capsys):
    # The round 2 agents left JVMs suspended on port 5005. Peeky's JVM has a port of its own.
    root, line = project
    decoy = subprocess.Popen(
        [
            shutil.which("java"),
            "-agentlib:jdwp=transport=dt_socket,server=y,suspend=y,address=127.0.0.1:0",
            "-cp",
            "classes",
            "demo.PaginationBug",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert "Listening for transport dt_socket" in decoy.stdout.readline()
        code, result = probe(
            capsys, f"{SOURCE}:{line}", "java", "-cp", "classes", "demo.PaginationBug"
        )
        assert code == 0, result
        assert result["test"]["jvm"]["pid"] != decoy.pid
        # Still suspended, waiting for its own debugger.
        assert decoy.poll() is None
    finally:
        decoy.kill()
        decoy.wait(timeout=30)


@pytest.mark.timeout(300)
def test_a_test_that_fails_before_the_line_is_no_stop_and_says_how_it_ended(project, capsys):
    # `x` isn't a page number: the JVM throws on line 7 and never reaches the line.
    root, line = project
    code, result = probe(
        capsys, f"{SOURCE}:{line}", "java", "-cp", "classes", "demo.PaginationBug", "x"
    )
    assert code == 1 and result["error"]["code"] == "NO_STOP", result
    assert result["rows"] == []
    assert result["test"]["exit_code"] == 1
    assert "NumberFormatException" in result["test"]["output_tail"]
