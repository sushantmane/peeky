"""Remote workers on a separate host, through a real `ssh -L` tunnel (#8, #248).

The second host is a Linux container (scripts/remote_e2e/Dockerfile): its own
kernel network namespace, filesystem, OS and Python, reached only through sshd
with a throwaway key. The worker binds to the container's loopback; the client
reaches it through the tunnel exactly as docs/REMOTE.md describes. The Java tests
use the image's `java` target, which adds a JDK and the Java adapter bundle: a
launched JVM, and a JVM already waiting for a debugger.

Runs when PEEKY_E2E_DOCKER=1 and Docker is available (OrbStack, Docker Desktop,
GitHub's ubuntu runners). Set PEEKY_E2E_RECORD=<file.json> to keep a transcript;
the Java tests write theirs beside it, as <file>-java.json.
"""

import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from peeky.cli import main

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "peeky-remote-e2e"
WORKER_PORT = 8765

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("PEEKY_E2E_DOCKER") != "1" or not shutil.which("docker"),
        reason="set PEEKY_E2E_DOCKER=1 with Docker available for the separate-host test",
    ),
    pytest.mark.timeout(300),
]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_port(port, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"nothing listens on 127.0.0.1:{port}")


class Host:
    """The container host, the remote worker and the client side tunnel."""

    def __init__(self, workdir, target="host", worker_args=("--root", "/home/peeky/app")):
        self.workdir = workdir
        self.target = target
        self.image = IMAGE if target == "host" else f"{IMAGE}-{target}"
        self.worker_args = list(worker_args)
        self.key = workdir / "id_ed25519"
        self.token = secrets.token_urlsafe(32)
        self.records = []
        self.tunnel = None
        self.local_port = free_port()
        self.url = f"http://127.0.0.1:{self.local_port}"

    def ssh_options(self):
        return [
            "-F",
            "/dev/null",  # never the user's ssh configuration
            "-i",
            str(self.key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={self.workdir / 'known_hosts'}",
            "-o",
            "LogLevel=ERROR",
            "-p",
            str(self.ssh_port),
        ]

    def ssh(self, command, **kwargs):
        return subprocess.run(
            ["ssh", *self.ssh_options(), "peeky@127.0.0.1", command],
            capture_output=True,
            text=True,
            timeout=60,
            **kwargs,
        )

    def start(self):
        subprocess.run(
            [
                "docker",
                "build",
                "-q",
                "-f",
                "scripts/remote_e2e/Dockerfile",
                "--target",
                self.target,
                "-t",
                self.image,
                ".",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=240,
        )
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)], check=True
        )
        self.container = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "-e",
                f"AUTHORIZED_KEY={self.key.with_suffix('.pub').read_text().strip()}",
                "-p",
                "127.0.0.1::22",
                self.image,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        mapped = subprocess.run(
            ["docker", "port", self.container, "22/tcp"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()[0]
        self.ssh_port = int(mapped.rsplit(":", 1)[1])
        deadline = time.monotonic() + 60
        while self.ssh("true").returncode != 0:
            if time.monotonic() > deadline:
                raise TimeoutError("sshd in the container did not accept the key")
            time.sleep(0.3)

    def serve(self):
        self.start_worker()
        self.open_tunnel()

    def start_worker(self):
        # The token travels on stdin, so it never appears in a remote command line.
        self.worker = subprocess.Popen(
            [
                "ssh",
                *self.ssh_options(),
                "peeky@127.0.0.1",
                "read -r DEBUG_TOKEN && export DEBUG_TOKEN && echo $$ > /tmp/worker.pid && "
                f"exec python3 -m peeky.worker --port {WORKER_PORT} --idle-timeout 120 "
                + " ".join(self.worker_args),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.worker.stdin.write(self.token + "\n")
        self.worker.stdin.flush()
        banner = self.worker.stdout.readline()
        assert f"127.0.0.1:{WORKER_PORT}" in banner, banner

    def restart_worker(self, *worker_args):
        assert self.ssh("kill $(cat /tmp/worker.pid)").returncode == 0
        self.worker.wait(timeout=20)
        self.worker_args = list(worker_args)
        self.start_worker()

    def open_tunnel(self):
        self.tunnel = subprocess.Popen(
            [
                "ssh",
                *self.ssh_options(),
                "-N",
                "-o",
                "ExitOnForwardFailure=yes",
                "-L",
                f"{self.local_port}:127.0.0.1:{WORKER_PORT}",
                "peeky@127.0.0.1",
            ]
        )
        wait_for_port(self.local_port)

    def close_tunnel(self):
        self.tunnel.terminate()
        self.tunnel.wait(timeout=10)

    def stop(self):
        for process in (self.tunnel, getattr(self, "worker", None)):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        if getattr(self, "container", None):
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)

    def describe(self):
        remote = self.ssh(
            'uname -srm; python3 -V; python3 -c \'import debugpy; print("debugpy", '
            "debugpy.__version__)'; ssh -V 2>&1 | cut -d, -f1; "
            "java -version 2>&1 | sed -n 2p || true"
        ).stdout.split("\n")
        import debugpy

        java = {"java": remote[4]} if self.target == "java" else {}
        return {
            "client": {
                "os": platform.platform(),
                "python": sys.version.split()[0],
                "debugpy": debugpy.__version__,
                "ssh": subprocess.run(["ssh", "-V"], capture_output=True, text=True)
                .stderr.split(",")[0]
                .strip(),
            },
            "worker_host": {
                "os": remote[0],
                "python": remote[1],
                "debugpy": remote[2],
                "sshd": remote[3],
                **java,
            },
            "transport": f"ssh -L {self.local_port}:127.0.0.1:{WORKER_PORT} into a Linux "
            "container (separate network namespace and filesystem), key-only sshd",
        }


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    yield from _serving(tmp_path_factory, Host)


def _serving(tmp_path_factory, make, before_worker=None, record_suffix=""):
    workdir = tmp_path_factory.mktemp("remote-e2e")
    os.chmod(workdir, 0o700)
    host = make(workdir)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PEEKY_STATE_DIR", str(workdir / "state"))
        patch.setenv("PEEKY_WORKER_TOKEN", host.token)
        try:
            host.start()
            if before_worker is not None:
                before_worker(host)
            host.serve()
            host.environment = host.describe()
            yield host
        finally:
            host.stop()
            if record := os.environ.get("PEEKY_E2E_RECORD"):
                record = Path(record)
                if record_suffix:
                    record = record.with_name(f"{record.stem}{record_suffix}{record.suffix}")
                record.write_text(
                    json.dumps(
                        {"environment": getattr(host, "environment", None), "steps": host.records},
                        indent=2,
                    )
                    + "\n"
                )


@pytest.fixture
def program(tmp_path):
    """The client's checkout: the same source as the worker host's copy."""
    shutil.copyfile(ROOT / "examples/pagination_bug.py", tmp_path / "pagination_bug.py")
    return tmp_path / "pagination_bug.py"


def run(host, capsys, step, *argv):
    started = time.monotonic()
    code = main([str(arg) for arg in argv])
    out = capsys.readouterr()
    seconds = round(time.monotonic() - started, 3)
    assert host.token not in out.out and host.token not in out.err
    try:
        result = json.loads(out.out)
    except ValueError:
        result = out.out.strip()
    shown = [Path(a).name if os.path.isabs(str(a)) else str(a) for a in argv]
    host.records.append(
        {"step": step, "command": "peeky " + " ".join(shown), "exit": code, "seconds": seconds}
    )
    return code, result


def test_a_probe_runs_on_the_other_host(host, capsys, program):
    code, result = run(
        host,
        capsys,
        "probe with an intervention",
        "probe",
        program,
        "--at",
        "8",
        "--capture",
        "start,page",
        "--set",
        "start=0",
        "--worker",
        host.url,
        "--json",
        "--no-save",
    )
    assert code == 0, result
    values = {(row["phase"], row["selector"]): row["value"] for row in result["rows"]}
    assert values[("observed", "start")] == "3" and values[("after-set", "start")] == "0"
    assert "PAGE: [10, 20, 30]" in result["output"] and result["worker"] == host.url
    host.records[-1]["result"] = {
        "status": result["status"],
        "observed start": values[("observed", "start")],
        "after set": values[("after-set", "start")],
        "output": result["output"].strip(),
        "probe seconds (worker side)": result["seconds"],
    }


def test_a_session_survives_a_dropped_tunnel(host, capsys, program):
    at = ["--worker", host.url]
    code, result = run(
        host, capsys, "session start", "session", "start", program, "--at", "8", *at, "--json"
    )
    assert code == 0 and result["state"] == "stopped", result
    code, result = run(host, capsys, "session get", "session", "get", "start", "--json")
    assert code == 0 and result[0]["value"] == "3"
    host.close_tunnel()
    code, result = run(
        host, capsys, "session get with the tunnel down", "session", "get", "start", "--json"
    )
    error = result["error"]
    assert code == 2 and error["code"] == "UNREACHABLE" and error["retryable"] is True
    host.records[-1]["result"] = {k: error[k] for k in ("code", "retryable", "session_closed")}
    host.open_tunnel()
    code, result = run(
        host, capsys, "session get after reopening the tunnel", "session", "get", "start", "--json"
    )
    assert code == 0 and result[0]["value"] == "3"  # the worker kept the stopped session
    code, result = run(host, capsys, "session set", "session", "set", "start=0", "--json")
    assert code == 0 and (result["before"], result["after"]) == ("3", "0")
    code, result = run(
        host, capsys, "session continue", "session", "continue", "--to-exit", "--json"
    )
    assert code == 0 and result["state"] == "terminated", result
    code, result = run(host, capsys, "session report", "session", "report", "--json")
    assert code == 0 and "PAGE: [10, 20, 30]" in result["output"]
    host.records[-1]["result"] = {"output": result["output"].strip()}
    code, result = run(host, capsys, "session close", "session", "close", "--json")
    # The program ran to its end, so cleanup finds nothing left (#143).
    assert code == 0 and result.get("closed") == "default", result
    assert result.get("cleanup", "nothing").startswith("nothing"), result


def test_a_different_source_on_the_host_is_refused_before_it_runs(host, capsys, program):
    changed = host.ssh(
        "cd app && cp pagination_bug.py .original && "
        "{ echo \"open('/home/peeky/RAN', 'w').close()\"; cat .original; } > pagination_bug.py"
    )
    assert changed.returncode == 0, changed.stderr
    try:
        code, result = run(
            host,
            capsys,
            "probe with a different source on the host",
            "probe",
            program,
            "--at",
            "8",
            "--capture",
            "start",
            "--set",
            "start=0",
            "--worker",
            host.url,
            "--json",
            "--no-save",
        )
        assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
        assert result["refused_before_launch"] is True
        assert result["rows"] == [] and not result.get("output")
        assert host.ssh("test -e /home/peeky/RAN").returncode == 1  # never executed
        host.records[-1]["result"] = {
            "code": result["error"]["code"],
            "message": result["error"]["message"],
            "program ran on the host": False,
        }
    finally:
        host.ssh("cd app && mv .original pagination_bug.py")


def test_a_worker_gone_behind_a_live_tunnel_is_transport_lost(host, capsys, program):
    assert host.ssh("kill $(cat /tmp/worker.pid)").returncode == 0
    host.worker.wait(timeout=20)
    code, result = run(
        host,
        capsys,
        "probe with the worker stopped behind a live tunnel",
        "probe",
        program,
        "--at",
        "8",
        "--worker",
        host.url,
        "--json",
        "--no-save",
    )
    error = result["error"]
    assert code == 2 and error["code"] in ("TRANSPORT_LOST", "UNREACHABLE"), error
    host.records[-1]["result"] = {k: error[k] for k in ("code", "retryable", "session_closed")}


# Java on the other host (#248): the worker's own trusted configuration names the
# adapter bundle and the sources; the client's names only which kind of Java target.
JAVA_SOURCE = "demo/PaginationBug.java"
JAVA_LINE = 13  # `int end = …`, after `start` is set
JAVA_LAUNCH = {
    "bundle": "/opt/peeky-java",
    "sources": [JAVA_SOURCE],
    "main_class": "demo.PaginationBug",
}
JAVA_ATTACH = {
    "bundle": "/opt/peeky-java",
    "port": 5005,
    "sources": [JAVA_SOURCE],
    "class_roots": ["classes"],
}


def _java_configs(host):
    for name, config in (("launch.json", JAVA_LAUNCH), ("attach.json", JAVA_ATTACH)):
        written = host.ssh(f"cat > java/{name}", input=json.dumps(config))
        assert written.returncode == 0, written.stderr


@pytest.fixture(scope="module")
def java_host(tmp_path_factory):
    yield from _serving(
        tmp_path_factory,
        lambda workdir: Host(
            workdir,
            "java",
            ("--root", "/home/peeky/java", "--java-config", "/home/peeky/java/launch.json"),
        ),
        before_worker=_java_configs,
        record_suffix="-java",
    )


@pytest.fixture
def java_checkout(tmp_path):
    """The client's checkout: the same Java source, and the same configurations."""
    (tmp_path / "demo").mkdir()
    shutil.copyfile(ROOT / "examples/java" / JAVA_SOURCE, tmp_path / JAVA_SOURCE)
    for name, config in (("launch.json", JAVA_LAUNCH), ("attach.json", JAVA_ATTACH)):
        (tmp_path / name).write_text(json.dumps(config))
    return tmp_path


def java_probe(host, capsys, step, checkout, config, *more):
    return run(
        host,
        capsys,
        step,
        "probe",
        checkout,
        "--config",
        checkout / config,
        "--at",
        f"{JAVA_SOURCE}:{JAVA_LINE}",
        "--capture",
        "start,pageSize",
        "--set",
        "start=0",
        "--worker",
        host.url,
        "--json",
        "--no-save",
        *more,
    )


def test_a_java_probe_runs_on_the_other_host(java_host, capsys, java_checkout):
    code, result = java_probe(
        java_host, capsys, "Java probe with a change", java_checkout, "launch.json"
    )
    assert code == 0, result
    values = {(row["phase"], row["selector"]): row["value"] for row in result["rows"]}
    assert values[("observed", "start")] == "3" and values[("after-set", "start")] == "0"
    assert "PAGE:[10, 20, 30]" in result["output"] and result["worker"] == java_host.url
    assert result["target_identity"]["language"] == "java"
    local = hashlib.sha256((java_checkout / JAVA_SOURCE).read_bytes()).hexdigest()
    assert result["source_sha256"] == local
    java_host.records[-1]["result"] = {
        "status": result["status"],
        "observed start": values[("observed", "start")],
        "after set": values[("after-set", "start")],
        "output": result["output"].strip(),
        "probe seconds (worker side)": result["seconds"],
    }


def test_a_java_session_runs_on_the_other_host(java_host, capsys, java_checkout):
    at = ["--config", java_checkout / "launch.json", "--at", f"{JAVA_SOURCE}:{JAVA_LINE}"]
    code, result = run(
        java_host,
        capsys,
        "Java session start",
        "session",
        "start",
        java_checkout,
        *at,
        "--worker",
        java_host.url,
        "--json",
    )
    assert code == 0 and result["state"] == "stopped", result
    code, result = run(java_host, capsys, "Java session get", "session", "get", "start", "--json")
    assert code == 0 and result[0]["value"] == "3", result
    code, result = run(java_host, capsys, "Java session set", "session", "set", "start=0", "--json")
    assert code == 0 and (result["before"], result["after"]) == ("3", "0"), result
    code, result = run(
        java_host, capsys, "Java session continue", "session", "continue", "--to-exit", "--json"
    )
    assert code == 0 and result["state"] == "terminated", result
    code, result = run(java_host, capsys, "Java session report", "session", "report", "--json")
    assert code == 0 and "PAGE:[10, 20, 30]" in result["output"], result
    java_host.records[-1]["result"] = {"output": result["output"].strip()}
    code, result = run(java_host, capsys, "Java session close", "session", "close", "--json")
    assert code == 0 and result.get("closed") == "default", result


def _changed_java_source(host):
    """The host's source writes a marker when it runs: proof it never ran."""
    changed = host.ssh(
        f"cd java && cp {JAVA_SOURCE} .original && sed -i "
        "'s|int page = |new java.io.File(\"/home/peeky/RAN\").createNewFile(); int page = |' "
        f"{JAVA_SOURCE} && sed -i 's|main(String\\[\\] args) {{|main(String[] args) throws "
        f"Exception {{|' {JAVA_SOURCE} && ! cmp -s .original {JAVA_SOURCE}"
    )
    assert changed.returncode == 0, changed.stderr


def _refused(host, result, code, **proof):
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert result["refused_before_launch"] is True
    assert result["rows"] == [] and not result.get("output")
    host.records[-1]["result"] = {
        "code": result["error"]["code"],
        "message": result["error"]["message"],
        **proof,
    }


def test_a_different_java_source_on_the_host_is_refused_before_it_runs(
    java_host, capsys, java_checkout
):
    _changed_java_source(java_host)
    try:
        code, result = java_probe(
            java_host,
            capsys,
            "Java probe with a different source on the host",
            java_checkout,
            "launch.json",
        )
        assert java_host.ssh("test -e /home/peeky/RAN").returncode == 1  # never executed
        _refused(java_host, result, code, **{"program ran on the host": False})
    finally:
        java_host.ssh(f"cd java && mv .original {JAVA_SOURCE}")


@pytest.fixture
def waiting_jvm(java_host):
    """A JVM on the host, suspended until a debugger attaches (Gradle's --debug-jvm)."""
    started = java_host.ssh(
        "cd java && rm -rf classes && javac -g -d classes demo/PaginationBug.java && "
        "{ nohup java -agentlib:jdwp=transport=dt_socket,server=y,suspend=y,"
        "address=127.0.0.1:5005 -cp classes demo.PaginationBug > jvm.log 2>&1 "
        "< /dev/null & echo $! > jvm.pid; }"
    )
    assert started.returncode == 0, started.stderr
    deadline = time.monotonic() + 30
    while "Listening for transport" not in java_host.ssh("cat java/jvm.log").stdout:
        assert time.monotonic() < deadline, java_host.ssh("cat java/jvm.log").stdout
        time.sleep(0.2)
    java_host.restart_worker(
        "--root", "/home/peeky/java", "--java-attach-config", "/home/peeky/java/attach.json"
    )
    try:
        yield java_host
    finally:
        java_host.ssh("kill $(cat java/jvm.pid) 2>/dev/null; rm -f java/jvm.pid")


def test_an_attached_java_vm_on_the_other_host(waiting_jvm, capsys, java_checkout):
    host = waiting_jvm
    _changed_java_source(host)
    try:
        code, result = java_probe(
            host,
            capsys,
            "Java attach probe with a different source on the host",
            java_checkout,
            "attach.json",
        )
        # Not attached: the JVM still waits for a debugger, and has printed nothing.
        assert host.ssh("kill -0 $(cat java/jvm.pid)").returncode == 0
        assert "PAGE:" not in host.ssh("cat java/jvm.log").stdout
        _refused(host, result, code, **{"the JVM": "still waiting, not attached"})
    finally:
        host.ssh(f"cd java && mv .original {JAVA_SOURCE}")
    # The refusal left the JVM waiting; the same probe with matching sources attaches.
    code, result = java_probe(
        host, capsys, "Java attach probe with a change", java_checkout, "attach.json"
    )
    assert code == 0, result
    values = {(row["phase"], row["selector"]): row["value"] for row in result["rows"]}
    assert values[("observed", "start")] == "3" and values[("after-set", "start")] == "0"
    assert result["target_identity"]["mode"] == "attach"
    deadline = time.monotonic() + 30
    while host.ssh("kill -0 $(cat java/jvm.pid)").returncode == 0:
        assert time.monotonic() < deadline, "the attached JVM did not finish"
        time.sleep(0.2)
    log = host.ssh("cat java/jvm.log").stdout
    assert "PAGE:[10, 20, 30]" in log, log  # detached, not killed: it ran to its end
    host.records[-1]["result"] = {
        "status": result["status"],
        "observed start": values[("observed", "start")],
        "after set": values[("after-set", "start")],
        "JVM output on the host": next(line for line in log.splitlines() if "PAGE:" in line),
    }
