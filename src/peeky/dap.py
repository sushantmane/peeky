"""Bounded asynchronous DAP transport for an owned Python debugger process."""

import collections
import json
import os
import signal
import subprocess
import sys
import threading
import time

MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_HEADER_BYTES = 8192
MAX_CONTROL_EVENTS = 2000


def read_message(stream):
    """Read one bounded frame; malformed adapters fail the session closed."""
    headers = {}
    total = 0
    while True:
        line = stream.readline(MAX_HEADER_BYTES + 1)
        total += len(line)
        if not line:
            raise EOFError("Debugger adapter closed")
        if total > MAX_HEADER_BYTES:
            raise ValueError("DAP headers exceed limit")
        if line == b"\r\n":
            break
        key, value = line.decode("ascii").split(":", 1)
        key = key.lower()
        if key in headers:
            raise ValueError("Duplicate DAP header")
        headers[key] = value.strip()
    length = int(headers["content-length"])
    if not 0 < length <= MAX_FRAME_BYTES:
        raise ValueError("DAP frame exceeds limit")
    payload = stream.read(length)
    if len(payload) != length:
        raise EOFError("Incomplete DAP frame")
    body = json.loads(payload)
    if not isinstance(body, dict):
        raise ValueError("Expected DAP object")
    return body


class DAP:
    def __init__(self):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "debugpy.adapter"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={k: v for k, v in os.environ.items() if k != "DEBUG_TOKEN"},
            start_new_session=os.name == "posix",
        )
        self.condition = threading.Condition()
        self.responses = {}
        self.events = collections.deque(maxlen=MAX_CONTROL_EVENTS)
        self.output = collections.deque(maxlen=500)
        self.output_truncated = False
        self.sequence = 0
        self.event_sequence = 0
        self.error = None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _accept(self, body):
        with self.condition:
            if body["type"] == "response":
                if len(self.responses) >= 256:
                    raise RuntimeError("Too many pending DAP responses")
                self.responses[body["request_seq"]] = body
            elif body["type"] == "event":
                if body["event"] == "output":
                    data = body.get("body", {})
                    if data.get("category") in ("stdout", "stderr"):
                        output = data.get("output", "")
                        self.output_truncated |= (
                            len(self.output) == self.output.maxlen or len(output) > 8192
                        )
                        self.output.append(output[:8192])
                else:
                    self.event_sequence += 1
                    body["cursor"] = self.event_sequence
                    self.events.append(body)
            self.condition.notify_all()

    def _read(self):
        try:
            while True:
                self._accept(read_message(self.process.stdout))
        except Exception as exc:
            with self.condition:
                self.error = str(exc)
                self.condition.notify_all()

    def send(self, command, arguments=None):
        with self.condition:
            if self.error:
                raise RuntimeError(self.error)
            self.sequence += 1
            seq = self.sequence
            data = json.dumps(
                dict(seq=seq, type="request", command=command, arguments=arguments or {})
            ).encode()
            self.process.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
            self.process.stdin.flush()
            return seq

    def response(self, seq, timeout=15):
        deadline = time.monotonic() + timeout
        with self.condition:
            while seq not in self.responses:
                if self.error:
                    raise RuntimeError(self.error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Debugger response timed out; outcome may be uncertain")
                self.condition.wait(remaining)
            value = self.responses.pop(seq)
        if not value.get("success"):
            raise RuntimeError(value.get("message", "Debugger request failed"))
        return value.get("body", {})

    def request(self, command, arguments=None):
        return self.response(self.send(command, arguments))

    def wait_event(self, names, after=0, timeout=15):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                if self.events and after < self.events[0]["cursor"] - 1:
                    raise RuntimeError("DAP control event history overflow; session must restart")
                for event in self.events:
                    if event["cursor"] > after and event["event"] in names:
                        return event
                if self.error:
                    raise RuntimeError(self.error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("No matching debugger event before deadline")
                self.condition.wait(remaining)

    def snapshot_output(self):
        with self.condition:
            return "".join(self.output), self.output_truncated

    def close(self):
        if self.process.poll() is None:
            try:
                self.response(self.send("disconnect", {"terminateDebuggee": True}), timeout=1)
            except Exception:
                pass
        # Always reap the owned group, including children surviving adapter exit.
        if os.name == "posix":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=3)
        self.reader.join(timeout=3)
        self.process.stdin.close()
        self.process.stdout.close()
