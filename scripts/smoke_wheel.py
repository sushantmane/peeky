"""Exercise the installed wheel from outside the checkout; no LLM or network service."""

import secrets
import tempfile
import threading
import time
from pathlib import Path

from peeky.client import Client
from peeky.worker import make_server

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    (root / "target.py").write_text("value = 1\nprint(value, flush=True)\n")
    token = secrets.token_urlsafe(32)
    server = make_server(root, token, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_port}", token)
    session = None
    try:
        launched = client.call("launch", program="target.py", breakpoints=[{"line": 2}])
        session = {"session_id": launched["session_id"]}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            stopped = client.call("wait", **session, timeout=1)
            if stopped["state"] == "stopped":
                break
        else:
            raise AssertionError("Installed wheel did not reach breakpoint")
        snapshot = client.call("inspect", **session, stop_id=stopped["stop_id"])
        assert any(
            v["name"] == "value" and v["value"] == "1"
            for scope in snapshot["scopes"]
            for v in scope["variables"]
        )
        print("Installed-wheel real debugger smoke passed")
    finally:
        if session:
            client.call("close", **session)
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join()
