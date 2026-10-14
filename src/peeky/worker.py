"""Single-session HTTP debugger worker. Bind locally; tunnel for remote use."""

import argparse
import hashlib
import hmac
import json
import math
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from peeky.adapters import PythonAdapter
from peeky.dap import DAP


class DebugWorker:
    def __init__(self, root, adapter=None):
        self.root = Path(root).resolve()
        # Adapter configuration is trusted startup input, never HTTP request data.
        self.adapter = adapter if adapter is not None else PythonAdapter()
        self.capabilities = {}
        self.dap = None
        self.state = "idle"
        self.session = None
        self.stop_id = None
        self.thread_id = None
        self.frames = set()
        self.references = set()
        self.audit = []
        self.lock = threading.Lock()
        self.last_used = time.monotonic()

    def path(self, name):
        path = (self.root / name).resolve()
        if self.root not in path.parents or not path.is_file():
            raise ValueError("Expected an existing file inside the worker root")
        return path

    def stopped(self, args):
        if self.state != "stopped" or args.get("stop_id") != self.stop_id:
            raise ValueError("Stale stop_id or program is not stopped; inspect the latest stop")

    def dispatch(self, operation, args):
        self.validate(operation, args)
        with self.lock:
            self.last_used = time.monotonic()
            if operation != "launch":
                if not self.dap or args.get("session_id") != self.session:
                    raise ValueError("Missing or stale session_id")
            try:
                result = self._dispatch(operation, args)
            except (TimeoutError, RuntimeError, OSError):
                self.close()
                raise
            if operation != "report":
                self.audit.append(
                    {
                        "time": time.time(),
                        "operation": operation,
                        "arguments": args,
                        "result": result,
                    }
                )
            self.audit = self.audit[-500:]
            return result

    @staticmethod
    def validate(operation, args):
        if not isinstance(args, dict):
            raise ValueError("Expected a JSON object")
        if operation not in {
            "launch",
            "wait",
            "inspect",
            "set_variable",
            "breakpoints",
            "continue",
            "report",
            "close",
        }:
            raise ValueError("Unknown operation")
        if operation in {"launch", "breakpoints"}:
            if not isinstance(args.get("program"), str):
                raise ValueError("program must be a string")
            points = args.get("breakpoints", [])
            if not isinstance(points, list) or len(points) > 100:
                raise ValueError("breakpoints must be a list of at most 100 entries")
            for point in points:
                if (
                    not isinstance(point, dict)
                    or type(point.get("line")) is not int
                    or point["line"] < 1
                ):
                    raise ValueError("breakpoint line must be a positive integer")
                if set(point) - {"line", "condition", "hitCondition", "logMessage"}:
                    raise ValueError("Unsupported breakpoint field")
                if any(not isinstance(v, str) for k, v in point.items() if k != "line"):
                    raise ValueError("Breakpoint expressions must be strings")
        if operation == "wait":
            value = args.get("timeout", 2)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("timeout must be a positive finite number")
        if operation == "inspect" and "frame_id" in args and type(args["frame_id"]) is not int:
            raise ValueError("frame_id must be an integer")
        if operation == "set_variable":
            if type(args.get("reference")) is not int:
                raise ValueError("reference must be an integer")
            if not isinstance(args.get("name"), str) or not isinstance(args.get("value"), str):
                raise ValueError("name and value must be strings")

    def _dispatch(self, operation, args):
        if operation == "launch":
            if self.dap:
                raise ValueError("Close the current session before launching another")
            program = self.path(args["program"])
            self.adapter.validate_breakpoints(args.get("breakpoints", []))
            self.session = uuid.uuid4().hex
            self.audit = []
            self.dap = DAP(command=self.adapter.command)
            try:
                capabilities = self.dap.request(
                    "initialize",
                    self.adapter.initialize_args(),
                )
                self.capabilities = self.adapter.normalize_capabilities(capabilities)
                launch = self.dap.send(
                    "launch",
                    self.adapter.launch_args(program, self.root),
                )
                self.dap.wait_event({"initialized"})
                points = self.dap.request(
                    "setBreakpoints",
                    {
                        "source": {"path": str(program)},
                        "breakpoints": args.get("breakpoints", []),
                    },
                )
                self.dap.request("configurationDone")
                self.dap.response(launch)
                self.state = "running"
                self.cursor = 0
                return {
                    "session_id": self.session,
                    "state": self.state,
                    "breakpoints": points.get("breakpoints", []),
                    "source_sha256": hashlib.sha256(program.read_bytes()).hexdigest(),
                    "supports_set_variable": self.capabilities.get("supports_set_variable", False),
                }
            except Exception:
                self.close()
                raise
        if operation == "wait":
            timeout = max(0.1, min(float(args.get("timeout", 2)), 2))
            if self.state in ("stopped", "terminated"):
                return {"state": self.state, "stop_id": self.stop_id}
            try:
                event = self.dap.wait_event({"stopped", "terminated"}, self.cursor, timeout)
            except TimeoutError:
                return {"state": self.state, "timed_out": True}
            self.cursor = event["cursor"]
            self.state = event["event"]
            self.frames.clear()
            self.references.clear()
            self.stop_id = uuid.uuid4().hex if self.state == "stopped" else None
            self.thread_id = event.get("body", {}).get("threadId")
            return {"state": self.state, "stop_id": self.stop_id, "event": event}
        if operation == "inspect":
            self.stopped(args)
            stack = self.dap.request("stackTrace", {"threadId": self.thread_id, "levels": 10})
            frames = stack.get("stackFrames", [])
            self.frames = {f["id"] for f in frames}
            selected = args.get("frame_id", frames[0]["id"] if frames else None)
            if selected not in self.frames:
                raise ValueError("Unknown frame")
            scopes = self.dap.request("scopes", {"frameId": selected}).get("scopes", [])
            result = []
            for scope in self.adapter.local_scopes(scopes):
                ref = scope["variablesReference"]
                self.references.add(ref)
                variables = self.dap.request("variables", {"variablesReference": ref}).get(
                    "variables", []
                )
                result.append(
                    {
                        "name": scope["name"],
                        "reference": ref,
                        "variables": [
                            dict(v, value=v.get("value", "")[:2000]) for v in variables[:50]
                        ],
                    }
                )
            return {"stop_id": self.stop_id, "frames": frames, "scopes": result}
        if operation == "set_variable":
            self.stopped(args)
            if not self.capabilities.get("supports_set_variable", False):
                raise ValueError("Adapter does not support variable mutation")
            ref = args["reference"]
            if ref not in self.references:
                raise ValueError("Inspect the scope before changing variables")
            current = self.dap.request("variables", {"variablesReference": ref}).get(
                "variables", []
            )
            before = next((v["value"] for v in current if v["name"] == args["name"]), None)
            changed = self.dap.request(
                "setVariable",
                {
                    "variablesReference": ref,
                    "name": args["name"],
                    "value": args["value"],
                },
            )
            return {"name": args["name"], "before": before, "after": changed.get("value")}
        if operation == "breakpoints":
            self.adapter.validate_breakpoints(args["breakpoints"])
            return self.dap.request(
                "setBreakpoints",
                {
                    "source": {"path": str(self.path(args["program"]))},
                    "breakpoints": args["breakpoints"],
                },
            )
        if operation == "continue":
            self.stopped(args)
            self.dap.request("continue", {"threadId": self.thread_id})
            self.state = "running"
            self.stop_id = None
            self.frames.clear()
            self.references.clear()
            return {"state": self.state}
        if operation == "report":
            output, truncated = self.dap.snapshot_output()
            return {
                "session_id": self.session,
                "state": self.state,
                "output": output,
                "output_truncated": truncated,
                "history": [e for e in self.audit if e["operation"] != "report"],
            }
        if operation == "close":
            self.close()
            return {"state": self.state}
        raise ValueError("Unknown operation")

    def close(self):
        adapter, self.dap = self.dap, None
        self.state = "idle"
        self.session = None
        self.stop_id = None
        self.thread_id = None
        self.capabilities = {}
        self.frames.clear()
        self.references.clear()
        if adapter:
            adapter.close()


def make_server(root, token, port=8765):
    if not isinstance(token, str) or len(token) < 16:
        raise ValueError("Token must contain at least 16 characters")
    worker = DebugWorker(root)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.reply(401, {"error": "Unauthorized"})
                return
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 65536:
                    raise ValueError("Request body must be 1..65536 bytes")
                self.connection.settimeout(10)
                args = json.loads(self.rfile.read(size))
                if not isinstance(args, dict):
                    raise ValueError("Expected a JSON object")
                result = worker.dispatch(self.path.removeprefix("/"), args)
                self.reply(200, result)
            except (KeyError, TypeError, ValueError) as exc:
                self.reply(400, {"error": str(exc)})
            except Exception as exc:
                self.reply(500, {"error": str(exc)})

        def reply(self, status, body):
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.worker = worker
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    token = os.environ.get("DEBUG_TOKEN")
    if not token or len(token) < 16:
        parser.error("Set DEBUG_TOKEN to a random secret of at least 16 characters")
    server = make_server(args.root, token, args.port)

    def expire():
        while True:
            time.sleep(5)
            with server.worker.lock:
                if server.worker.dap and time.monotonic() - server.worker.last_used > 300:
                    server.worker.close()

    threading.Thread(target=expire, daemon=True).start()
    print(
        f"Debugger worker: http://127.0.0.1:{server.server_port}; root={Path(args.root).resolve()}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.worker.close()
        server.server_close()


if __name__ == "__main__":
    main()
