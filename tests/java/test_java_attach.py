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


@pytest.fixture
def attached(tmp_path):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to enable Java attach conformance")
    javac, java = shutil.which("javac"), shutil.which("java")
    (tmp_path / "src/demo").mkdir(parents=True)
    shutil.copyfile(ROOT / "examples/java/demo/PaginationBug.java", tmp_path / SOURCE)
    subprocess.run(
        [javac, "-g", "-d", str(tmp_path / "classes"), str(tmp_path / SOURCE)], check=True
    )
    port = free_port()
    agent = f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=y,address=127.0.0.1:{port}"
    process = subprocess.Popen(
        [java, agent, "-cp", str(tmp_path / "classes"), "demo.PaginationBug"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert "Listening for transport dt_socket" in process.stdout.readline()
    from peeky.java_attach import JavaAttachTarget

    target = JavaAttachTarget(
        bundle=Path(bundle), port=port, sources=(SOURCE,), class_roots=("classes",)
    )
    line = next(
        i
        for i, text in enumerate((tmp_path / SOURCE).read_text().splitlines(), 1)
        if "// OBSERVE" in text
    )
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
    assert result["status"] == "completed", result.get("error")
    rows = {(r["phase"], r["selector"]): r["value"] for r in result["rows"]}
    assert rows["observed", "start"] == "3" and rows["observed", "pageSize"] == "3"
    assert rows["after-set", "start"] == "0"
    assert result["target_identity"]["mode"] == "attach"
    output = process.stdout.read()
    assert process.wait(timeout=30) == 0 and "PAGE:[10, 20, 30]" in output


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
