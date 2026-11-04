"""`--test` for Java (java_test, ADR 0009): the agent goes to the test JVM alone, which Peeky
finds by the exact options it added, and the attach is proven to have reached it."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from peeky import java_test as jt
from peeky.errors import Refused


def test_maven_gets_the_agent_for_the_jvms_surefire_forks(tmp_path):
    launch = jt.prepare(["mvn", "test", "-o", "-pl", "core", "-Dtest=FooTest#bar"], tmp_path)
    assert launch.tool == "maven"
    debug = f"{jt.AGENT} -Dpeeky.launch={launch.nonce}"
    assert launch.command == [
        "mvn",
        "test",
        "-o",
        "-pl",
        "core",
        "-Dtest=FooTest#bar",
        f"-Dmaven.surefire.debug={debug}",
        f"-Dmaven.failsafe.debug={debug}",
    ]
    assert len(launch.nonce) == 32 and launch.mark == f"-Dpeeky.launch={launch.nonce}"
    # A new nonce each run.
    assert jt.prepare(["./mvnw", "test"], tmp_path).nonce != launch.nonce


@pytest.mark.parametrize(
    "words, text",
    [
        (["mvn", "test", "-Dmaven.surefire.debug"], "drop it"),
        (["mvn", "test", "-Dmaven.failsafe.debug=x"], "drop it"),
        (["mvn", "test", "-DforkCount=0"], "Maven's own JVM"),
        (["./gradlew", "test", "--debug-jvm"], "drop it"),
        (["java", "-agentlib:jdwp=transport=dt_socket", "-cp", "c", "Main"], "already has"),
        (["javac", "-J-agentlib:jdwp=server=y", "A.java"], "already has"),
        (["sbt", "test"], "can't add Peeky's debug agent"),
    ],
)
def test_commands_peeky_cant_add_its_agent_to_alone_are_refused(tmp_path, words, text):
    with pytest.raises(Refused) as caught:
        jt.prepare(words, tmp_path)
    assert text in str(caught.value)
    assert not list(tmp_path.iterdir()), "nothing written, nothing run"
    if caught.value.code == "UNSUPPORTED":
        assert caught.value.hint.startswith("Nothing ran.")


def test_gradle_gets_an_init_script_for_its_test_tasks(tmp_path):
    launch = jt.prepare(["./gradlew", "--offline", "test", "--tests", "pkg.FooTest"], tmp_path)
    init = Path(launch.command[2])
    assert launch.command[:2] == ["./gradlew", "-I"] and init.parent == tmp_path
    assert launch.command[3:] == ["--offline", "test", "--tests", "pkg.FooTest"]
    text = init.read_text()
    assert f"task.jvmArgs('{jt.AGENT}', '-Dpeeky.launch={launch.nonce}')" in text
    # The JVM arguments alone: a task's maxParallelForks and forkEvery are its own.
    assert "it instanceof Test" in text and "maxParallelForks" not in text
    assert "forkEvery" not in text
    assert init.stat().st_mode & 0o777 == 0o600
    jt._remove(launch)
    assert not init.exists()


def test_java_and_javac_get_it_as_their_first_options(tmp_path):
    launch = jt.prepare(["java", "-cp", "classes", "demo.Main", "x"], tmp_path)
    assert launch.command == ["java", jt.AGENT, launch.mark, "-cp", "classes", "demo.Main", "x"]
    launch = jt.prepare(["javac", "-cp", "lib", "A.java"], tmp_path)
    assert launch.command == ["javac", f"-J{jt.AGENT}", f"-J{launch.mark}", "-cp", "lib", "A.java"]


def test_only_a_jvm_given_both_words_as_jvm_options_is_the_test_jvm():
    mark = "-Dpeeky.launch=" + "ab" * 16
    fork = ["/jdk/bin/java", "-javaagent:jacoco.jar", jt.AGENT, mark, "-jar", "booter.jar"]
    assert jt.is_test_jvm(fork, mark)
    assert jt.is_test_jvm(["java", "-cp", "a:b", jt.AGENT, mark, "Main"], mark)
    # Maven's own JVM holds both inside one argument of its program.
    maven = ["/jdk/bin/java", "-classpath", "boot.jar", "Launcher", "test"]
    assert not jt.is_test_jvm([*maven, f"-Dmaven.surefire.debug={jt.AGENT} {mark}"], mark)
    # As the main class's arguments, they are the program's, not the JVM's.
    assert not jt.is_test_jvm(["java", "Main", jt.AGENT, mark], mark)
    # A shell that passes them on; another run's mark; the agent alone.
    assert not jt.is_test_jvm(["/bin/sh", "-c", f"java {jt.AGENT} {mark} Main"], mark)
    assert not jt.is_test_jvm(["java", jt.AGENT, "-Dpeeky.launch=" + "cd" * 16, "Main"], mark)
    assert not jt.is_test_jvm(["java", jt.AGENT, "Main"], mark)
    assert jt.is_test_jvm(["/jdk/bin/javac", f"-J{jt.AGENT}", f"-J{mark}", "A.java"], mark)
    assert not jt.is_test_jvm(["javac", jt.AGENT, mark, "A.java"], mark)


def test_this_processs_command_line_and_sockets_are_read_from_the_system():
    # The same readings find a test JVM: its argv, its one listening port, and the two ends
    # of a connection to that port.
    argv = jt.processes()[os.getpid()]
    assert "python" in Path(argv[0]).name or any("pytest" in word for word in argv), argv
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert port in jt.listening(os.getpid())
        with socket.create_connection(("127.0.0.1", port)) as client:
            accepted, _ = server.accept()
            with accepted:
                ends = {(local, remote) for pid, local, remote in jt.connections(port)}
                assert (port, client.getsockname()[1]) in ends
                assert (client.getsockname()[1], port) in ends
                # Both ends are in this process: what the attach's proof asks of the JVM and
                # of the debugger under Peeky.
                jt.check_connected(os.getpid(), port)
    assert os.getpid() in jt.descendants(os.getppid())


def test_the_attach_is_refused_unless_its_connection_ends_at_peekys_debugger(monkeypatch):
    jvm, ours, other = 4000, 5000, 6000
    monkeypatch.setattr(jt, "descendants", lambda pid: {pid, ours})
    monkeypatch.setattr(jt, "connections", lambda port: [(jvm, port, 41000), (ours, 41000, port)])
    jt.check_connected(jvm, 7000)
    # Another process connected to the JVM: nothing is read.
    monkeypatch.setattr(jt, "connections", lambda port: [(jvm, port, 42000), (other, 42000, port)])
    with pytest.raises(Refused) as caught:
        jt.check_connected(jvm, 7000)
    assert caught.value.code == "UNSUPPORTED"
    assert "can't prove that its debugger attached" in str(caught.value)
    assert "Nothing was read" in caught.value.hint
    # The JVM holds no connection at all.
    monkeypatch.setattr(jt, "connections", lambda port: [(ours, 41000, port)])
    with pytest.raises(Refused):
        jt.check_connected(jvm, 7000)


def test_tcp6_addresses_read_as_the_kernel_writes_them():
    # A JVM's dual-stack socket shows its IPv4 peer as ::ffff:127.0.0.1, word by word.
    word = int.from_bytes(bytes([127, 0, 0, 1]), "big").to_bytes(4, sys.byteorder).hex()
    mapped = "0" * 16 + int.from_bytes(b"\0\0\xff\xff", "big").to_bytes(4, sys.byteorder).hex()
    assert jt._address(f"{word.upper()}:9A13") == ("127.0.0.1", 0x9A13)
    assert jt._address(f"{mapped}{word}:0016") == ("127.0.0.1", 22)


def _source(root, path, package="pkg"):
    path = root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"package {package};\nclass Foo {{}}\n")
    return path


def test_class_roots_are_the_build_tools_outputs_in_the_files_module(tmp_path, monkeypatch):
    root = tmp_path
    (root / "core").mkdir()
    (root / "pom.xml").write_text("")
    (root / "core/pom.xml").write_text("")
    _source(root, "core/src/main/java/pkg/Foo.java")
    (root / "core/target/classes/pkg").mkdir(parents=True)
    (root / "core/target/test-classes/other").mkdir(parents=True)
    maven = jt.prepare(["mvn", "test"], tmp_path)
    assert jt.class_roots(root, "core/src/main/java/pkg/Foo.java", maven) == (
        "core/target/classes",
    )
    (root / "app/build/classes/java/main/pkg").mkdir(parents=True)
    (root / "app/build/classes/kotlin/test/pkg").mkdir(parents=True)
    (root / "app/build.gradle.kts").write_text("")
    _source(root, "app/src/main/java/pkg/Foo.java")
    gradle = jt.prepare(["gradle", "test"], tmp_path)
    assert jt.class_roots(root, "app/src/main/java/pkg/Foo.java", gradle) == (
        "app/build/classes/java/main",
        "app/build/classes/kotlin/test",
    )
    # `java` and `javac`: the classpath's directories inside the root.
    monkeypatch.chdir(root)
    (root / "lib/pkg").mkdir(parents=True)
    java = jt.prepare(["java", "-cp", f"lib{os.pathsep}/elsewhere{os.pathsep}x.jar", "M"], tmp_path)
    assert jt.class_roots(root, "app/src/main/java/pkg/Foo.java", java) == ("lib",)
    with pytest.raises(Refused) as caught:
        jt.class_roots(
            root, "core/src/main/java/pkg/Foo.java", jt.prepare(["javac", "A.java"], tmp_path)
        )
    assert "found no compiled classes of core/src/main/java/pkg/Foo.java" in str(caught.value)


def test_a_finished_build_says_how_it_ended_with_its_last_lines(tmp_path):
    launch = jt.prepare(["java", "-version"], tmp_path)
    launch.log.write_text("".join(f"line {n}\n" for n in range(100)))
    launch.process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    launch.process.wait()
    found = jt.finish(launch, 0)
    assert found["exit_code"] == 3 and "stopped" not in found
    assert found["output_tail"].splitlines()[-1] == "line 99"
    assert len(found["output_tail"].splitlines()) == jt.TAIL_LINES
    assert found["command"] == "java -version" and found["kind"] == "java"
    # Still running at the deadline: stopped, with no exit code claimed.
    launch.process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    found = jt.finish(launch, 0)
    assert found["exit_code"] is None and found["stopped"] == "the probe's time ran out"
    assert launch.process.poll() is not None


def test_a_java_test_is_a_probes_only(tmp_path, monkeypatch, capsys):
    from peeky.cli import main

    monkeypatch.chdir(tmp_path)
    _source(tmp_path, "src/pkg/Foo.java")
    for argv in (
        ["session", "start", ".", "--at", "src/pkg/Foo.java:2"],
        ["describe", ".", "--at", "src/pkg/Foo.java"],
    ):
        code = main([*argv, "--json", "--test", "--", "mvn", "test"])
        error = json.loads(capsys.readouterr().out)["error"]
        assert code == 2 and "a Java test for a probe only" in error["message"], argv
    # A probe needs Peeky's adapter before anything runs.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    code = main(
        ["probe", ".", "--at", "src/pkg/Foo.java:2", "--json", "--test", "--", "mvn", "test"]
    )
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 2 and "peeky setup-java" in error["message"]
    assert not (tmp_path / "state/tests").exists()


def test_with_no_jvm_attached_a_waiting_one_is_never_called_a_second(tmp_path, monkeypatch):
    # find_jvm ran out of time, or refused the JVM: one that waits now is no second JVM.
    launch = jt.prepare(["mvn", "test"], tmp_path)
    launch.process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    fake = 2**22 + 7
    monkeypatch.setattr(jt, "processes", lambda: {fake: ["java", jt.AGENT, launch.mark, "M"]})
    monkeypatch.setattr(jt, "listening", lambda pid: [40000] if pid == fake else [])
    monkeypatch.setattr(jt, "_kill", lambda pid: None)
    found = jt.finish(launch, float("inf"))
    assert found["stopped"] == "Peeky attached to no test JVM, and one waited for a debugger"
    assert launch.notes == [] and "jvm" not in found
    assert launch.process.poll() is not None


def _running(launch):
    launch.process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    return launch


def test_a_build_running_test_jvms_at_once_is_refused_after_it_started(tmp_path, monkeypatch):
    # Gradle's maxParallelForks (kept as the build has it) or Maven's forkCount: Peeky
    # attaches to one JVM, and the line may run in another. Nothing was read.
    launch = _running(jt.prepare(["./gradlew", "test"], tmp_path))
    first, second = 2**22 + 7, 2**22 + 9
    jvm = ["java", jt.AGENT, launch.mark, "Worker"]
    monkeypatch.setattr(jt, "processes", lambda: {first: jvm, second: jvm})
    monkeypatch.setattr(jt, "listening", lambda pid: [40000] if pid == first else [])
    try:
        with pytest.raises(Refused) as caught:
            jt.find_jvm(launch, time.monotonic() + 30)
        assert "gradle started 2 test JVMs at once" in str(caught.value)
        assert caught.value.hint == (
            "Nothing was read; Peeky stopped the test. Select one test class (--tests NAME), or "
            "give the test task maxParallelForks = 1."
        )
        assert launch.jvm is None
        # One alone, for the settling time: that one.
        monkeypatch.setattr(jt, "processes", lambda: {first: jvm})
        monkeypatch.setattr(jt, "SETTLE_S", 0.3)
        assert jt.find_jvm(launch, time.monotonic() + 30) == (first, 40000)
    finally:
        launch.process.kill()
        launch.process.wait()


def test_refusals_after_the_test_started_say_nothing_was_read_not_nothing_ran(
    tmp_path, monkeypatch
):
    launch = _running(jt.prepare(["mvn", "test"], tmp_path))
    pid = 2**22 + 7
    monkeypatch.setattr(jt, "processes", lambda: {pid: ["java", jt.AGENT, launch.mark, "M"]})
    monkeypatch.setattr(jt, "listening", lambda _: [40000, 40001])
    try:
        with pytest.raises(Refused) as caught:
            jt.find_jvm(launch, time.monotonic() + 30)
        assert "listens on 2 loopback ports" in str(caught.value)
        assert caught.value.hint.startswith("Nothing was read; Peeky stopped the test.")
        assert "Nothing ran" not in caught.value.hint
    finally:
        launch.process.kill()
        launch.process.wait()
    _source(tmp_path, "src/pkg/Foo.java")
    with pytest.raises(Refused) as caught:
        jt.class_roots(tmp_path, "src/pkg/Foo.java", launch)
    assert caught.value.hint == (
        "Nothing was read; Peeky stopped the test. Attach with a config naming class_roots "
        "(docs/lang/java.md)."
    )


def test_timeout_covers_the_build_its_probe_and_its_end(tmp_path, monkeypatch, capsys):
    # The probe ends FINISH_MIN_S before --timeout's deadline, and the build is stopped at it:
    # no time past it for the build to end in.
    from peeky import cli, java_attach

    monkeypatch.chdir(tmp_path)
    _source(tmp_path, "src/pkg/Foo.java")
    seen = {}
    monkeypatch.setattr(
        cli,
        "_java_test_target",
        lambda args: (tmp_path, "src/pkg/Foo.java", 2, "java-attach", None),
    )

    def find(launch, deadline, progress=None):
        seen["find"] = deadline
        return (4242, 40000)

    def probe(root, request, **_):
        seen["probe"] = request.timeout
        return cli._unrun(request, time.monotonic(), "no-stop", {"code": "NO_STOP", "message": ""})

    def finish(launch, deadline, progress=None):
        seen["finish"] = deadline
        return {"command": "mvn test", "kind": "java", "output_tail": "", "log": "x"}

    monkeypatch.setattr(jt, "start", lambda launch, cwd: None)
    monkeypatch.setattr(jt, "find_jvm", find)
    monkeypatch.setattr(jt, "class_roots", lambda *_: ("target/classes",))
    monkeypatch.setattr(java_attach, "JavaAttachTarget", lambda **_: None)
    monkeypatch.setattr(cli, "run_probe", probe)
    monkeypatch.setattr(jt, "finish", finish)
    monkeypatch.setattr("peeky.java_setup.default_bundle", lambda: None)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    argv = ["probe", ".", "--at", "src/pkg/Foo.java:2", "--json", "--test", "--", "mvn", "test"]
    before = time.monotonic()
    cli.main([*argv[:4], "--timeout", "100", *argv[4:]])
    after = time.monotonic()
    capsys.readouterr()
    assert before + 100 <= seen["finish"] <= after + 100
    assert seen["find"] == pytest.approx(seen["finish"] - jt.FINISH_MIN_S - jt.PROBE_MIN_S)
    assert 100 - jt.FINISH_MIN_S - (after - before) <= seen["probe"] <= 100 - jt.FINISH_MIN_S
    # Too short for the probe and the build's end: refused before anything runs.
    seen.clear()
    code = cli.main([*argv[:4], "--timeout", "30", *argv[4:]])
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 2 and "give more than 35 s" in error["message"] and seen == {}


def test_a_build_still_running_at_the_deadline_is_stopped_then(tmp_path):
    launch = _running(jt.prepare(["java", "-version"], tmp_path))
    deadline = time.monotonic() + 1.0
    found = jt.finish(launch, deadline)
    # Stopped at the deadline, give or take the polling and the signal's reaping.
    assert found["stopped"] == "the probe's time ran out"
    assert time.monotonic() - deadline < 3
    assert launch.process.poll() is not None
