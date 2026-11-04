"""`--test` with a `java` command (ADR 0009): Peeky starts the test's JVM with its agent,
finds it among the user's JVMs by the options it added, proves the attach reached it,
probes, and lets the JVM run to its end. A JVM another run left waiting is never touched."""

import json
import os
import shutil
import subprocess
import sys
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


# Stand-ins for Maven and Gradle, which the lane has neither of (nor their plugins offline):
# each, as the real tool does, runs as a process of its own and forks the test JVMs, FORKS of
# them at once, with the JVM options Peeky gave it: surefire's `-Dmaven.surefire.debug`, or
# the `jvmArgs` of Peeky's Gradle init script. So Peeky's launcher runs end to end: its words,
# the build tool's own process that holds them in an argument, the marked test JVMs, the
# attach's proof, and its refusal of several at once.
FAKE_TOOL = """#!{python}
import os, re, shlex, subprocess, sys
args = sys.argv[1:]
if os.path.basename(sys.argv[0]) == "mvn":
    debug = next(a for a in args if a.startswith("-Dmaven.surefire.debug=")).partition("=")[2]
    options, classes = shlex.split(debug), "target/classes"
else:
    init = open(args[args.index("-I") + 1]).read()
    options = re.findall(r"'((?:[^'\\\\]|\\\\.)*)'", init.split("task.jvmArgs(")[1].split(")")[0])
    classes = "build/classes/java/main"
java = os.path.join(os.environ["JAVA_HOME_FOR_TEST"], "bin", "java")
forks = [
    subprocess.Popen([java, *options, "-cp", classes, "demo.PaginationBug"])
    for _ in range(int(os.environ.get("FORKS", "1")))
]
sys.exit(max(fork.wait() for fork in forks))
"""


@pytest.fixture
def build_tools(project, tmp_path, monkeypatch):
    """`mvn` and `./gradlew` stand-ins, with the project laid out as each builds it."""
    root, line = project
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name in ("mvn", "gradlew"):
        path = bin_ / name if name == "mvn" else root / name
        path.write_text(FAKE_TOOL.replace("{python}", sys.executable))
        path.chmod(0o755)
    (root / "pom.xml").write_text("<project/>\n")
    (root / "build.gradle").write_text("\n")
    for classes in ("target/classes", "build/classes/java/main"):
        shutil.copytree(root / "classes", root / classes)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    java = Path(os.path.realpath(shutil.which("java"))).parent.parent
    monkeypatch.setenv("JAVA_HOME_FOR_TEST", str(java))
    return root, line


def _marked():
    """Test JVMs with any run's Peeky mark still running."""
    from peeky import java_test

    return [
        pid
        for pid, argv in java_test.processes().items()
        if any(w.startswith(java_test.MARK) for w in java_test.jvm_options(argv))
        and Path(argv[0]).name == "java"
    ]


@pytest.mark.timeout(300)
@pytest.mark.parametrize("command", [["mvn", "test", "-Dtest=X"], ["./gradlew", "test"]])
def test_a_build_tools_forked_test_jvm_is_found_attached_and_read(build_tools, capsys, command):
    root, line = build_tools
    code, result = probe(capsys, f"{SOURCE}:{line}", *command)
    assert code == 0, result
    assert {r["selector"]: r["value"] for r in result["rows"]} == {"start": "3", "pageSize": "3"}
    test = result["test"]
    assert test["tool"] == ("maven" if command[0] == "mvn" else "gradle")
    assert test["exit_code"] == 0 and "PAGE:[40, 50, 60]" in test["output_tail"]
    assert not _marked()


@pytest.mark.timeout(300)
@pytest.mark.parametrize("command", [["mvn", "test"], ["./gradlew", "test"]])
def test_a_build_running_two_test_jvms_at_once_is_refused_and_stopped(
    build_tools, capsys, monkeypatch, command
):
    # Gradle's maxParallelForks, Maven's forkCount: Peeky attaches to one JVM, and the line
    # may run in the other. It reads nothing, says so, and stops the build and both JVMs.
    root, line = build_tools
    monkeypatch.setenv("FORKS", "2")
    code, result = probe(capsys, f"{SOURCE}:{line}", *command)
    error = result["error"]
    assert code == 2 and error["code"] == "UNSUPPORTED", result
    assert "started 2 test JVMs at once" in error["message"]
    assert error["hint"].startswith("Nothing was read; Peeky stopped the test.")
    assert result["rows"] == [] and result["test"]["stopped"]
    assert not _marked()
