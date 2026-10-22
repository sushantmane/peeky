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
import re
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
                # LLDB turns ASLR off for the program it launches (personality(2)), which
                # Docker's default seccomp profile refuses; the CI mirror runs the same way.
                *(
                    ["--cap-add=SYS_PTRACE", "--security-opt", "seccomp=unconfined"]
                    if self.target == "native"
                    else []
                ),
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
        if self.target == "native":
            # The versions this run used: apt installs LLVM 18's current package.
            tools = self.ssh(
                "clang-18 --version | head -1; lldb-18 --version | head -1; "
                "llvm-dwarfdump-18 --version | head -1; "
                "dpkg-query -W -f '${Version}' lldb-18; echo; "
                # ssh sessions don't see Docker's ENV, which names the toolchain's home.
                "export RUSTUP_HOME=/opt/rust/rustup; /opt/rust/cargo/bin/rustc --version; "
                "/opt/rust/cargo/bin/cargo --version; /opt/rust/cargo/bin/rustup --version"
            ).stdout.split("\n")
            java = dict(
                zip(
                    ("clang", "lldb", "llvm-dwarfdump", "lldb package", "rustc", "cargo", "rustup"),
                    (line.strip() for line in tools),
                )
            )
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
    """The client's checkout: the same sources as the worker host's copy (#249: every .py
    file under the root, not only the program)."""
    for name in ("pagination_bug.py", "verify_pagination.py"):
        shutil.copyfile(ROOT / "examples" / name, tmp_path / name)
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
# The attached JVM prints its page, then blocks until a `release` file appears: a probe
# that waited for the JVM's exit, or killed it, cannot pass.
ATTACHED_SOURCE = "demo/AttachedPagination.java"
ATTACHED_TEXT = """package demo;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;

public class AttachedPagination {
    public static void main(String[] args) throws Exception {
        int page = 1;
        int pageSize = 3;
        int[] items = new int[8];
        for (int i = 0; i < items.length; i++) items[i] = (i + 1) * 10;
        int start = page * pageSize;
        int end = Math.min(start + pageSize, items.length); // OBSERVE
        int[] result = Arrays.copyOfRange(items, Math.min(start, items.length), end);
        System.out.println("PAGE:" + Arrays.toString(result));
        while (!Files.exists(Path.of("release"))) Thread.sleep(50);
        System.out.println("DONE");
    }
}
"""
ATTACHED_LINE = next(
    number for number, text in enumerate(ATTACHED_TEXT.splitlines(), 1) if "// OBSERVE" in text
)
JAVA_ATTACH = {
    "bundle": "/opt/peeky-java",
    "port": 5005,
    "sources": [ATTACHED_SOURCE],
    "class_roots": ["classes"],
}


def _java_configs(host):
    for name, config in (("launch.json", JAVA_LAUNCH), ("attach.json", JAVA_ATTACH)):
        written = host.ssh(f"cat > java/{name}", input=json.dumps(config))
        assert written.returncode == 0, written.stderr
    written = host.ssh(f"cat > java/{ATTACHED_SOURCE}", input=ATTACHED_TEXT)
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
    (tmp_path / ATTACHED_SOURCE).write_text(ATTACHED_TEXT)
    for name, config in (("launch.json", JAVA_LAUNCH), ("attach.json", JAVA_ATTACH)):
        (tmp_path / name).write_text(json.dumps(config))
    return tmp_path


def java_probe(host, capsys, step, checkout, config, *more, at=f"{JAVA_SOURCE}:{JAVA_LINE}"):
    return run(
        host,
        capsys,
        step,
        "probe",
        checkout,
        "--config",
        checkout / config,
        "--at",
        at,
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


def _changed_java_source(host, source=JAVA_SOURCE):
    """The host's source writes a marker when it runs: proof it never ran."""
    changed = host.ssh(
        f"cd java && cp {source} .original && sed -i "
        "'s|int page = |new java.io.File(\"/home/peeky/RAN\").createNewFile(); int page = |' "
        f"{source} && sed -i 's|main(String\\[\\] args) {{|main(String[] args) throws "
        f"Exception {{|' {source} && ! cmp -s .original {source}"
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


def test_a_second_java_source_that_differs_on_the_host_is_refused_by_name(
    java_host, capsys, java_checkout
):
    # #249: only the breakpoint's file was compared, so the host compiled another
    # Helper.java and the probe completed without a note.
    helper = "package demo;\npublic class Helper { static int size() { return 3; } }\n"
    config = dict(JAVA_LAUNCH, sources=[JAVA_SOURCE, "demo/Helper.java"])
    (java_checkout / "demo/Helper.java").write_text(helper)
    (java_checkout / "two.json").write_text(json.dumps(config))
    assert java_host.ssh("cat > java/two.json", input=json.dumps(config)).returncode == 0
    changed = helper.replace("return 3", "return 4")
    assert java_host.ssh("cat > java/demo/Helper.java", input=changed).returncode == 0
    java_host.restart_worker(
        "--root", "/home/peeky/java", "--java-config", "/home/peeky/java/two.json"
    )
    try:
        code, result = java_probe(
            java_host,
            capsys,
            "Java probe with a second source that differs on the host",
            java_checkout,
            "two.json",
        )
        assert (
            "for demo/Helper.java (the copy on the worker host differs from your checkout)"
            in (result["error"]["message"])
        )
        _refused(java_host, result, code, **{"named": "demo/Helper.java"})
        assert java_host.ssh("cat > java/demo/Helper.java", input=helper).returncode == 0
        code, result = java_probe(
            java_host, capsys, "Java probe with both sources the same", java_checkout, "two.json"
        )
        assert code == 0 and "PAGE:[10, 20, 30]" in result["output"], result
    finally:
        java_host.ssh("rm -f java/demo/Helper.java java/two.json")
        java_host.restart_worker(
            "--root", "/home/peeky/java", "--java-config", "/home/peeky/java/launch.json"
        )


@pytest.fixture
def waiting_jvm(java_host):
    """A JVM on the host, suspended until a debugger attaches (Gradle's --debug-jvm).

    jvm.pid is the JVM's own pid; its exit status lands in jvm.exit."""
    started = java_host.ssh(
        "cd java && rm -rf classes release jvm.exit jvm.pid && "
        f"javac -g -d classes {ATTACHED_SOURCE} && "
        "{ nohup sh -c 'java -agentlib:jdwp=transport=dt_socket,server=y,suspend=y,"
        "address=127.0.0.1:5005 -cp classes demo.AttachedPagination & echo $! > jvm.pid; "
        "wait $!; echo $? > jvm.exit' > jvm.log 2>&1 < /dev/null & }"
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
        java_host.ssh("cd java && touch release; kill $(cat jvm.pid) 2>/dev/null; rm -f jvm.pid")


def _jvm_alive(host):
    return host.ssh("kill -0 $(cat java/jvm.pid)").returncode == 0


def _wait_for(condition, what, seconds=30):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.2)


def test_an_attached_java_vm_on_the_other_host(waiting_jvm, capsys, java_checkout):
    host = waiting_jvm
    at = f"{ATTACHED_SOURCE}:{ATTACHED_LINE}"
    _changed_java_source(host, ATTACHED_SOURCE)
    try:
        code, result = java_probe(
            host,
            capsys,
            "Java attach probe with a different source on the host",
            java_checkout,
            "attach.json",
            at=at,
        )
        # Not attached: the JVM still waits for a debugger, and has printed nothing.
        assert _jvm_alive(host)
        assert "PAGE:" not in host.ssh("cat java/jvm.log").stdout
        _refused(host, result, code, **{"the JVM": "still waiting, not attached"})
    finally:
        host.ssh(f"cd java && mv .original {ATTACHED_SOURCE}")
    # The refusal left the JVM waiting; the same probe with matching sources attaches.
    code, result = java_probe(
        host, capsys, "Java attach probe with a change", java_checkout, "attach.json", at=at
    )
    assert code == 0, result
    # The probe ended after its hit: it neither waited for the JVM's exit nor ended it.
    assert result["ended"] == "hits", result
    assert _jvm_alive(host)
    values = {(row["phase"], row["selector"]): row["value"] for row in result["rows"]}
    assert values[("observed", "start")] == "3" and values[("after-set", "start")] == "0"
    assert result["target_identity"]["mode"] == "attach"
    # Detached, the JVM runs on with the change, and blocks on the sentinel.
    _wait_for(lambda: "PAGE:" in host.ssh("cat java/jvm.log").stdout, "no PAGE line")
    assert _jvm_alive(host) and host.ssh("test -e java/jvm.exit").returncode == 1
    log = host.ssh("cat java/jvm.log").stdout
    assert "PAGE:[10, 20, 30]" in log and "DONE" not in log, log
    assert host.ssh("touch java/release").returncode == 0
    _wait_for(lambda: host.ssh("test -e java/jvm.exit").returncode == 0, "the JVM did not end")
    status = host.ssh("cat java/jvm.exit").stdout.strip()
    log = host.ssh("cat java/jvm.log").stdout
    assert status == "0" and "DONE" in log, (status, log)
    host.records[-1]["result"] = {
        "status": result["status"],
        "ended": result["ended"],
        "observed start": values[("observed", "start")],
        "after set": values[("after-set", "start")],
        "JVM output on the host": next(line for line in log.splitlines() if "PAGE:" in line),
        "the JVM after the probe": "alive, blocked on its sentinel file",
        "after the sentinel": f"exit status {status}, printed DONE",
    }


# C, C++ and Rust on the other host (#252): the image's `native` target holds LLVM 18 and
# Rust 1.96.0. Each language has its own root and trusted config there; the worker is
# restarted with it. The client's checkout holds the same files.
LLVM = {"adapter": "/usr/bin/lldb-dap-18", "dwarfdump": "/usr/bin/llvm-dwarfdump-18"}
NATIVE = {
    "c": {
        "files": {
            "main.c": (
                '#include <stdio.h>\n#include "page.h"\n\nint main(void) {\n'
                "  int page = 1;\n  int start = page * PAGE_SIZE;\n"
                '  printf("START:%d\\n", start);\n  return 0;\n}\n'
            ),
            "page.h": "#define PAGE_SIZE 3\n",
        },
        "config": {
            "sources": ["main.c"],
            "headers": ["page.h"],
            "compiler": "/usr/bin/clang-18",
            **LLVM,
        },
        "flag": "--native-config",
        "at": "main.c:7",
        "source": "main.c",
        "other": ("page.h", "#define PAGE_SIZE 4\n"),
    },
    "cpp": {
        "files": {
            "main.cpp": (
                '#include <iostream>\n#include "page.hpp"\n\nint main() {\n'
                "  int page = 1;\n  int start = page * kPageSize;\n"
                '  std::cout << "START:" << start << "\\n";\n  return 0;\n}\n'
            ),
            "page.hpp": "constexpr int kPageSize = 3;\n",
        },
        "config": {
            "language": "cpp",
            "sources": ["main.cpp"],
            "headers": ["page.hpp"],
            "compiler": "/usr/bin/clang++-18",
            **LLVM,
        },
        "flag": "--native-config",
        "at": "main.cpp:7",
        "source": "main.cpp",
        "other": ("page.hpp", "constexpr int kPageSize = 4;\n"),
    },
    "rust": {
        "files": {
            "Cargo.toml": (
                '[package]\nname = "remote-pages"\nversion = "0.1.0"\nedition = "2021"\n'
                "build = false\nautolib = false\nautobins = false\nautotests = false\n"
                'autoexamples = false\nautobenches = false\n\n[[bin]]\nname = "remote-pages"\n'
                'path = "src/main.rs"\n'
            ),
            "Cargo.lock": (
                "# This file is automatically @generated by Cargo.\n"
                "# It is not intended for manual editing.\nversion = 4\n\n[[package]]\n"
                'name = "remote-pages"\nversion = "0.1.0"\n'
            ),
            "src/main.rs": (
                "mod page;\n\nfn main() {\n    let mut start = page::start(1, 3);\n"
                '    let shown = start;\n    println!("START:{shown}");\n    start += 0;\n'
                "    let _ = start;\n}\n"
            ),
            "src/page.rs": "pub fn start(page: i32, size: i32) -> i32 {\n    page * size\n}\n",
        },
        "config": {
            "sources": ["src/main.rs", "src/page.rs"],
            "tests": [],
            "linker": "/usr/bin/clang-18",
            **LLVM,
        },
        "flag": "--rust-config",
        "at": "src/main.rs:5",
        "source": "src/main.rs",
        "other": (
            "src/page.rs",
            "pub fn start(page: i32, size: i32) -> i32 {\n    page + size\n}\n",
        ),
    },
}


def _native_files(host):
    tools = json.loads(host.ssh("cat /opt/rust/tools.json").stdout)
    NATIVE["rust"]["config"].update(cargo=tools["cargo"], rustc=tools["rustc"])
    for language, spec in NATIVE.items():
        for name, text in {**spec["files"], "target.json": json.dumps(spec["config"])}.items():
            written = host.ssh(
                f"mkdir -p $(dirname {language}/{name}) && cat > {language}/{name}", input=text
            )
            assert written.returncode == 0, written.stderr


def _native_worker(language):
    spec = NATIVE[language]
    return (
        "--root",
        f"/home/peeky/{language}",
        spec["flag"],
        f"/home/peeky/{language}/target.json",
    )


@pytest.fixture(scope="module")
def native_host(tmp_path_factory):
    yield from _serving(
        tmp_path_factory,
        lambda workdir: Host(workdir, "native", _native_worker("c")),
        before_worker=_native_files,
        record_suffix="-native",
    )


@pytest.fixture(params=list(NATIVE))
def native(request, native_host, tmp_path):
    """(language, the client's checkout) with the worker serving that language."""
    language = request.param
    if native_host.worker_args != list(_native_worker(language)):
        native_host.restart_worker(*_native_worker(language))
    for name, text in NATIVE[language]["files"].items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    return language, tmp_path


def _host_paths(value, where="result", found=None):
    """(where, text) for every string in ``value`` that names a path on the worker host
    without saying so (`worker:`)."""
    found = [] if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            _host_paths(item, f"{where}.{key}", found)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _host_paths(item, f"{where}[{index}]", found)
    elif (
        isinstance(value, str)
        and not value.startswith("worker:")
        and re.search(r"/home/peeky/|/tmp/peeky-", value)
    ):
        # Unmarked: a path that exists only on the worker host, shown as if it were yours.
        found.append((where, value))
    return found


def _mine(path, checkout):
    """``path`` relative to the client's checkout; None if it is not in it."""
    try:
        return Path(path).resolve().relative_to(checkout.resolve()).as_posix()
    except ValueError:
        return None


def test_a_native_probe_runs_on_the_other_host(native_host, capsys, native):
    language, checkout = native
    spec = NATIVE[language]
    code, result = run(
        native_host,
        capsys,
        f"{language}: probe with a change",
        "probe",
        checkout,
        "--at",
        spec["at"],
        "--capture",
        "start",
        "--set",
        "start=0",
        "--worker",
        native_host.url,
        "--json",
        "--no-save",
    )
    assert code == 0, (result.get("error"), result.get("unapplied"))
    values = {(row["phase"], row["selector"]): row["value"] for row in result["rows"]}
    assert values[("observed", "start")] == "3" and values[("after-set", "start")] == "0"
    assert "START:0" in result["output"], result["output"]
    assert result["target_identity"]["language"] == ("rust" if language == "rust" else language)
    # Locations are the client's: no path that exists only on the worker host.
    assert _host_paths(result) == [], _host_paths(result)
    native_host.records[-1]["result"] = {
        "observed start": values[("observed", "start")],
        "after set": values[("after-set", "start")],
        "output": result["output"].strip(),
        "probe seconds (worker side)": result["seconds"],
    }


def test_a_native_session_runs_on_the_other_host(native_host, capsys, native):
    language, checkout = native
    spec = NATIVE[language]
    url = ["--worker", native_host.url]
    steps = [
        ("session start", ["session", "start", checkout, "--at", spec["at"], *url, "--json"]),
        ("session get", ["session", "get", "start", "--json"]),
        ("session inspect", ["session", "inspect", "--json"]),
        ("session continue", ["session", "continue", "--to-exit", "--json"]),
        ("session report", ["session", "report", "--json"]),
        ("session close", ["session", "close", "--json"]),
    ]
    # What each step's record keeps: the stop in the client's terms, values, the output.
    shown = {
        "session start": lambda r: {"state": r["state"], "at": _mine(r["location"], checkout)},
        "session get": lambda r: {"start": r[0]["value"]},
        "session inspect": lambda r: {
            "frame 0": _mine(r["frames"][0]["source"]["path"], checkout),
            # The callers, outside your sources: the worker host's, and marked so.
            "frames 1-2": [(f.get("source") or {}).get("path") for f in r["frames"][1:3]],
        },
        "session continue": lambda r: {"state": r["state"]},
        "session report": lambda r: {"output": r["output"].strip()},
        "session close": lambda r: {"closed": r["closed"]},
    }
    results = {}
    for step, argv in steps:
        code, result = run(native_host, capsys, f"{language}: {step}", *argv)
        assert code == 0, (step, result)
        results[step] = result
        native_host.records[-1]["result"] = shown[step](result)
    assert results["session start"]["state"] == "stopped"
    assert results["session get"][0]["value"] == "3"
    assert results["session continue"]["state"] == "terminated"
    assert "START:3" in results["session report"]["output"]
    # The stop is named in the client's checkout, not where the worker built it.
    assert _mine(results["session start"]["location"], checkout) == spec["at"]
    assert (
        _mine(results["session inspect"]["frames"][0]["source"]["path"], checkout)
        == (spec["source"])
    )
    assert _host_paths(results) == [], sorted(
        {re.sub(r"\[\d+\]", "[]", w) for w, _ in _host_paths(results)}
    )


# Each main source, changed to leave a marker if it is ever built and run.
RAN = "/home/peeky/RAN"
CHANGED = {
    "c": ("int page = 1;", f'int page = 1; fclose(fopen("{RAN}", "w"));'),
    "cpp": ("int page = 1;", f'int page = 1; std::fclose(std::fopen("{RAN}", "w"));'),
    "rust": ("let mut start", f'std::fs::write("{RAN}", "").unwrap(); let mut start'),
}


@pytest.mark.parametrize("changed", ["source", "header or module"])
def test_a_different_native_file_on_the_host_is_refused_by_name(
    native_host, capsys, native, changed
):
    language, checkout = native
    spec = NATIVE[language]
    if changed == "source":
        name = spec["source"]
        text = spec["files"][name].replace(*CHANGED[language])
        if language == "cpp":
            text = "#include <cstdio>\n" + text
    else:
        name, text = spec["other"]
    assert text != spec["files"][name]
    assert native_host.ssh(f"cat > {language}/{name}", input=text).returncode == 0
    try:
        code, result = run(
            native_host,
            capsys,
            f"{language}: probe with a different {name} on the host",
            "probe",
            checkout,
            "--at",
            spec["at"],
            "--capture",
            "start",
            "--worker",
            native_host.url,
            "--json",
            "--no-save",
        )
        # The breakpoint's file is compared first, then every other file the build reads.
        named = (
            f"for the program {name}: refused before launching"
            if changed == "source"
            else f"for {name} (the copy on the worker host differs from your checkout)"
        )
        assert named in result["error"]["message"], result
        assert native_host.ssh(f"test -e {RAN}").returncode == 1  # never built and run
        _refused(native_host, result, code, named=name, **{"program ran on the host": False})
    finally:
        restored = native_host.ssh(f"cat > {language}/{name}", input=spec["files"][name])
        assert restored.returncode == 0, restored.stderr
