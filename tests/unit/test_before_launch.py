"""Every probe error before the debugger's launch or attach request is sent is a
refusal before launch (#190), whatever its code: no table, exit 2,
`refused_before_launch` in JSON, and no run kept as evidence. Before, only the refusals that said so did that; a file outside the worker
root (`INVALID_REQUEST`), an outdated Java adapter (`UNSUPPORTED`), a build that failed
or a debugger that never started printed an empty table and exited 1. An error once
the debugger was asked to start the program is not one: the program may have run."""

import hashlib
import json
import os
import sys

import pytest

from peeky.adapters import PythonAdapter
from peeky.build import BuildCommandError
from peeky.cli import _probe_text, main
from peeky.java import STALE_BUNDLE, _tool
from peeky.probe import Probe, probe_exit_code, run_probe

HEADER = "hit  phase  line"

# A debugger that answers until it is sent ``stage``, then dies.
ADAPTER = r"""
import json, sys
stage = sys.argv[1]
while True:
    header = sys.stdin.buffer.readline()
    if not header:
        break
    length = int(header.split(b":")[1])
    sys.stdin.buffer.readline()
    request = json.loads(sys.stdin.buffer.read(length))
    if request["command"] == stage:
        sys.exit(3)
    body = json.dumps({"type": "response", "request_seq": request["seq"], "success": True,
                       "command": request["command"], "body": {}}).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()
"""


class Target(PythonAdapter):
    """A Python target whose preparation fails with ``error``, or whose debugger dies
    when sent ``stage``."""

    def __init__(self, script, error=None, stage=None):
        self.script, self.error, self.stage = script, error, stage

    def prepare(self, program, root, edit=None, *, deadline=None):
        if self.error is not None:
            raise self.error
        return self

    @property
    def command(self):
        return (sys.executable, str(self.script), self.stage or "none")


@pytest.fixture
def root(tmp_path):
    (tmp_path / "main.py").write_text("x = 1\nprint(x)\n")
    (tmp_path / "adapter.py").write_text(ADAPTER)
    return tmp_path


def probe(root, adapter, **fields):
    result = run_probe(root, Probe(**{"program": "main.py", "line": 2, **fields}), adapter=adapter)
    return result, probe_exit_code(result), _probe_text(result)


def assert_refused(result, code, text, error):
    assert result["status"] == "failed" and result["error"]["code"] == error, result
    assert result.get("refused_before_launch") is True and code == 2, result
    assert HEADER not in text and f"error [{error}]" in text, text


@pytest.mark.parametrize(
    "error, code",
    [
        (
            BuildCommandError(
                ("/usr/bin/cc", "-c", "main.c"),
                {"exit_code": 1, "stdout": "", "stderr": "main.c:1:9: error: expected ';'"},
            ),
            "BUILD_FAILED",
        ),
        (RuntimeError("Source provenance changed during preparation"), "PROVENANCE_CHANGED"),
        (TimeoutError("Build preparation exceeded its overall deadline"), "TIMEOUT"),
        (
            ValueError(STALE_BUNDLE.format(bundle="/old", missing="eventhub-log-guard")),
            "UNSUPPORTED",
        ),
        (ValueError("Program is not a declared Java source"), "INVALID_REQUEST"),
        (FileNotFoundError(2, "No such file or directory", "/missing/javac"), "INVALID_REQUEST"),
        (OSError(13, "Permission denied", "/opt/lldb-dap"), "DEBUGGER_ERROR"),
    ],
)
def test_an_error_preparing_the_program_is_a_refusal_before_launch(root, error, code):
    result, exit_code, text = probe(root, Target(root / "adapter.py", error=error))
    assert_refused(result, exit_code, text, code)


def test_a_debugger_that_dies_before_the_program_is_asked_to_start_is_one(root):
    result, code, text = probe(root, Target(root / "adapter.py", stage="initialize"))
    assert_refused(result, code, text, "DEBUGGER_ERROR")


def test_a_debugger_that_dies_once_asked_to_start_the_program_is_not_one(root):
    # The program may have run: its failure keeps its table and exits 1.
    result, code, text = probe(root, Target(root / "adapter.py", stage="launch"))
    assert result["status"] == "failed" and "refused_before_launch" not in result, result
    assert code == 1 and HEADER in text


def test_the_worker_refuses_a_file_outside_its_root_before_launch(root):
    (root.parent / "outside.py").write_text("x = 1\nprint(x)\n")
    result, code, text = probe(root, PythonAdapter(), program="../outside.py")
    assert_refused(result, code, text, "INVALID_REQUEST")
    assert "inside the worker root" in result["error"]["message"]


def test_a_source_the_worker_does_not_have_is_refused_before_launch(root):
    expected = (("program", "0" * 64),)
    result, code, text = probe(root, PythonAdapter(), expect_sha256=expected)
    assert_refused(result, code, text, "PROVENANCE_CHANGED")
    assert not result.get("output")


def stale_bundle(tmp_path):
    """A Java adapter bundle that passes every check but the java-debug patch."""
    bundle = tmp_path / "bundle"
    files = {
        "classes/PeekyJavaAdapter.class": b"\xca\xfe\xba\xbe launcher",
        "classes/com/microsoft/java/debug/core/EventHub.class": b"\xca\xfe\xba\xbe old",
        "lib/dependency.jar": b"PK jar",
    }
    for name, data in files.items():
        (bundle / name).parent.mkdir(parents=True, exist_ok=True)
        (bundle / name).write_bytes(data)
    java = _tool(sys.executable)
    classpath = f"{bundle}/classes{os.pathsep}{bundle}/lib/*"
    manifest = {
        "schema": 1,
        "command": [java, "-cp", classpath, "PeekyJavaAdapter"],
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    (bundle / "adapter.json").write_text(json.dumps(manifest))
    return bundle, java


def test_an_outdated_java_adapter_is_refused_before_launch_by_the_cli(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    bundle, java = stale_bundle(tmp_path)
    app = tmp_path / "app"
    app.mkdir()
    (app / "Main.java").write_text(
        "public class Main {\n    public static void main(String[] a) {\n"
        "        int x = 1;\n        System.out.println(x);\n    }\n}\n"
    )
    config = app / "java.json"
    config.write_text(
        json.dumps(
            {
                "language": "java",
                "sources": ["Main.java"],
                "main_class": "Main",
                "bundle": str(bundle),
                "java": java,
                "javac": java,
            }
        )
    )
    command = ["probe", str(app), "--config", str(config), "--at", "Main.java:3", "--capture", "x"]
    assert main(command) == 2
    text = capsys.readouterr().out
    assert HEADER not in text and "error [UNSUPPORTED]" in text and "predates" in text, text
    assert main([*command, "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["refused_before_launch"] is True and result["error"]["code"] == "UNSUPPORTED"
    # Refused before the launch request: no run is kept as evidence.
    assert not (tmp_path / "state" / "evidence" / "evidence.db").exists()


def test_a_cli_refusal_before_the_probe_says_so_in_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "main.py").write_text("x = 1\nprint(x)\n")
    assert main(["probe", str(tmp_path), "--at", "other.py:2", "--capture", "x", "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "INVALID_REQUEST", result
    assert result["refused_before_launch"] is True and result["status"] == "failed"
    # Other commands' errors are not probes: no such field.
    assert main(["describe", str(tmp_path / "missing.py"), "--json"]) == 2
    assert "refused_before_launch" not in json.loads(capsys.readouterr().out)


# A compiler that answers `--version` and never finishes a build.
SLOW_COMPILER = """#!/bin/sh
case "$*" in *--version*) echo "clang version 18.1.3"; exit 0;; esac
exec sleep 20
"""


def slow_build(tmp_path):
    """A C target whose build outlasts any probe child's budget."""
    from peeky.native import NativeTarget

    tools = tmp_path / "tools"
    tools.mkdir()
    for name, text in (("cc", SLOW_COMPILER), ("dap", "#!/bin/sh\necho dap\n")):
        (tools / name).write_text(text)
        (tools / name).chmod(0o755)
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.c").write_text("int main(void) {\n    int x = 1;\n    return x;\n}\n")
    tool = str(tools / "dap")
    target = NativeTarget(
        sources=("a.c",),
        compiler=str(tools / "cc"),
        adapter=tool,
        dwarfdump=tool,
        dsymutil=tool if sys.platform == "darwin" else None,
    )
    return root, target


def test_a_remote_build_slower_than_its_budget_answers_before_the_watchdog(tmp_path):
    # #190 review: the launch had the worker's 40 s, the watchdog fired at 35 s and made
    # up a failure without `refused_before_launch`: an empty table, exit 1, a saved run.
    from peeky.worker import _run_probe_process

    root, target = slow_build(tmp_path)
    # A launch budget of 4 s, a watchdog at 9 s.
    result = _run_probe_process(root, Probe("a.c", 2, ("x",), timeout=30), target, 14)
    assert result["error"]["code"] == "TIMEOUT", result
    assert "HTTP cap" not in result["error"]["message"], result["error"]
    assert result["refused_before_launch"] is True and probe_exit_code(result) == 2


def test_the_watchdog_says_when_the_launch_request_was_not_sent(tmp_path, monkeypatch):
    from peeky import worker

    root, target = slow_build(tmp_path)
    # The child's budget outlasts the watchdog (at 3 s).
    monkeypatch.setattr(worker, "PROBE_LAUNCH_MARGIN_S", 0)
    result = worker._run_probe_process(root, Probe("a.c", 2, ("x",), timeout=30), target, 8)
    assert result["status"] == "timeout" and result["error"]["code"] == "TIMEOUT", result
    message = result["error"]["message"]
    assert "before the debugger's launch/attach request was sent" in message, message
    assert result["refused_before_launch"] is True and probe_exit_code(result) == 2


def test_the_watchdog_after_the_launch_request_is_no_refusal(tmp_path, monkeypatch):
    # The program ran: its failure is an ordinary one.
    from peeky import worker

    (tmp_path / "main.py").write_text("import time\nx = 1\ntime.sleep(60)\n")
    monkeypatch.setattr(worker, "PROBE_LAUNCH_MARGIN_S", 0)
    result = worker._run_probe_process(tmp_path, Probe("main.py", 3, ("x",), timeout=30), None, 10)
    assert result["status"] == "timeout" and "refused_before_launch" not in result, result
    assert probe_exit_code(result) == 1


def answering(status, body):
    """A loopback worker that answers its version, then every other request with
    ``status`` and ``body``."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from peeky.errors import PROTOCOL_VERSION

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            version = self.path == "/version"
            answer = json.dumps({"protocol": PROTOCOL_VERSION}).encode() if version else body
            self.send_response(200 if version else status)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize(
    "status, body, code",
    [
        # Sent and answered, but not as the worker answers: the outcome is unknown.
        (200, b"<html>proxy</html>", "TRANSPORT_LOST"),
        (200, b"[1, 2]", "TRANSPORT_LOST"),
        (200, b'{"protocol": 1}', "TRANSPORT_LOST"),
        (200, b'{"status": "ok", "rows": []}', "TRANSPORT_LOST"),
        (502, b"<html>Bad Gateway</html>", "INVALID_REQUEST"),
        # The worker's own refusal.
        (
            400,
            json.dumps({"error": {"code": "BUSY", "message": "Another probe is running"}}),
            "BUSY",
        ),
    ],
)
def test_only_a_remote_refusal_the_worker_made_is_one_before_launch(
    tmp_path, monkeypatch, capsys, status, body, code
):
    # #190 review: an answer that was not JSON was taken for a refusal before launch.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", "before-launch-token-1234567890")
    (tmp_path / "main.py").write_text("x = 1\nprint(x)\n")
    server = answering(status, body if isinstance(body, bytes) else body.encode())
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        argv = ["probe", str(tmp_path / "main.py"), "--at", "2", "--capture", "x"]
        assert main([*argv, "--worker", url, "--json"]) == 2
    finally:
        server.shutdown()
        server.server_close()
    shown = json.loads(capsys.readouterr().out)
    assert shown["error"]["code"] == code, shown
    assert shown.get("refused_before_launch") is (True if code == "BUSY" else None), shown


def test_a_worker_nobody_answers_at_is_one_before_launch(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", "before-launch-token-1234567890")
    (tmp_path / "main.py").write_text("x = 1\nprint(x)\n")
    argv = ["probe", str(tmp_path / "main.py"), "--at", "2", "--worker", "http://127.0.0.1:1"]
    assert main([*argv, "--json"]) == 2
    shown = json.loads(capsys.readouterr().out)
    assert shown["error"]["code"] == "UNREACHABLE" and shown["refused_before_launch"] is True
