"""Bounded asynchronous DAP transport for an owned debugger process."""

import collections
import json
import os
import selectors
import signal
import subprocess
import threading
import time

from peeky.adapters import PythonAdapter

MAX_FRAME_BYTES = 4 * 1024 * 1024


class ResponseTimeout(TimeoutError):
    """A complete request received no response in time; reads may treat this as budget."""


MAX_HEADER_BYTES = 8192
MAX_CONTROL_EVENTS = 2000
LIFECYCLE_EVENTS = frozenset(
    {"initialized", "stopped", "continued", "terminated", "exited", "breakpoint", "process"}
)


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
    def __init__(self, command=None, *, env=None):
        # Trusted worker configuration only. Never forward agent-provided argv.
        command = PythonAdapter().command if command is None else command
        if (
            not isinstance(command, (tuple, list))
            or not command
            or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command)
        ):
            raise ValueError("Adapter command must be a nonempty argument vector")
        command = tuple(command)
        if os.name != "posix":
            raise RuntimeError("Peeky DAP pipe deadlines currently require a POSIX host")
        self.write_lock = threading.Lock()
        self.close_lock = threading.Lock()
        self.closed = False
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={
                k: v for k, v in (os.environ if env is None else env).items() if k != "DEBUG_TOKEN"
            },
            start_new_session=os.name == "posix",
        )
        os.set_blocking(self.process.stdin.fileno(), False)
        self.condition = threading.Condition()
        self.responses = {}
        self.abandoned = set()
        self.events = collections.deque(maxlen=MAX_CONTROL_EVENTS)
        self.ignored_events = {}
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
                if body["request_seq"] in self.abandoned:
                    # A reply to a request whose caller already timed out: discard it.
                    self.abandoned.discard(body["request_seq"])
                    self.condition.notify_all()
                    return
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
                elif body["event"] in LIFECYCLE_EVENTS:
                    self.event_sequence += 1
                    body["cursor"] = self.event_sequence
                    body["received"] = time.monotonic()
                    self.events.append(body)
                else:
                    # Thread/module churn (thousands per second in large JVMs) must not
                    # evict lifecycle events; keep only bounded counts.
                    name = body["event"] if len(self.ignored_events) < 64 else "<other>"
                    self.ignored_events[name] = self.ignored_events.get(name, 0) + 1
            self.condition.notify_all()

    def _read(self):
        try:
            while True:
                self._accept(read_message(self.process.stdout))
        except Exception as exc:
            self._fail(exc)

    def _fail(self, exc):
        with self.condition:
            # Preserve the first cause when cleanup subsequently produces EOF.
            self.error = self.error or str(exc)
            self.condition.notify_all()

    def send(self, command, arguments=None, timeout=5):
        """Write one complete frame within a deadline, or poison the transport.

        Never hold the response/event condition while waiting for pipe capacity.
        A partial write is an uncertain request: callers must close the session.
        """
        deadline = time.monotonic() + timeout
        if not self.write_lock.acquire(timeout=max(0, timeout)):
            exc = TimeoutError("Debugger write lock timed out; outcome may be uncertain")
            self._fail(exc)
            raise exc
        try:
            with self.condition:
                if self.error:
                    raise RuntimeError(self.error)
                self.sequence += 1
                seq = self.sequence
            data = json.dumps(
                dict(seq=seq, type="request", command=command, arguments=arguments or {})
            ).encode()
            if len(data) > MAX_FRAME_BYTES:
                raise ValueError("Outgoing DAP frame exceeds limit")
            frame = memoryview(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
            fd = self.process.stdin.fileno()
            with selectors.DefaultSelector() as selector:
                selector.register(fd, selectors.EVENT_WRITE)
                while frame:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError(
                            "Debugger pipe write timed out; outcome may be uncertain"
                        )
                    try:
                        count = os.write(fd, frame[:65536])
                    except BlockingIOError:
                        continue
                    if count == 0:
                        raise BrokenPipeError("Debugger pipe closed during write")
                    frame = frame[count:]
            return seq
        except (TimeoutError, OSError) as exc:
            self._fail(exc)
            raise
        finally:
            self.write_lock.release()

    def response(self, seq, timeout=15):
        """Wait for one response; ResponseTimeout means the write completed but no reply came."""
        deadline = time.monotonic() + timeout
        with self.condition:
            while seq not in self.responses:
                if self.error:
                    raise RuntimeError(self.error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.abandoned.add(seq)
                    raise ResponseTimeout("Debugger response timed out; outcome may be uncertain")
                self.condition.wait(remaining)
            value = self.responses.pop(seq)
        if not value.get("success"):
            raise RuntimeError(value.get("message", "Debugger request failed"))
        return value.get("body", {})

    def drained(self, timeout=1):
        """True once every timed-out request has received (and discarded) its reply."""
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.abandoned and not self.error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
            return not self.abandoned and not self.error

    def request(self, command, arguments=None, *, timeout=15):
        """One absolute deadline covers both the write and the response."""
        deadline = time.monotonic() + timeout
        seq = self.send(command, arguments, timeout=max(0.001, min(5, timeout)))
        return self.response(seq, timeout=max(0, deadline - time.monotonic()))

    def wait_event(self, names, after=0, timeout=15, *, failed_request=None):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                response = self.responses.get(failed_request)
                if response is not None and not response.get("success"):
                    raise RuntimeError(response.get("message", "Debugger launch failed"))
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

    def recent_events(self, limit=20):
        """Diagnostics: the latest lifecycle events and counts of ignored ones."""
        with self.condition:
            recent = [
                {
                    "cursor": event["cursor"],
                    "event": event["event"],
                    "reason": event.get("body", {}).get("reason"),
                    "thread": event.get("body", {}).get("threadId"),
                }
                for event in list(self.events)[-limit:]
            ]
            return {"recent": recent, "ignored": dict(self.ignored_events)}

    def pending_stop(self, after):
        """(receive time, reason) of the first unconsumed stop after ``after``, else None."""
        with self.condition:
            for event in self.events:
                if event["cursor"] > after and event["event"] in ("stopped", "terminated"):
                    if event["event"] != "stopped":
                        return None
                    return event["received"], event.get("body", {}).get("reason")
            return None

    def snapshot_output(self):
        with self.condition:
            return "".join(self.output), self.output_truncated

    def close(self, terminate=True):
        """Disconnect and reap the adapter; attached debuggees are detached, not killed."""
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
            if self.process.poll() is None:
                try:
                    seq = self.send("disconnect", {"terminateDebuggee": terminate}, timeout=0.25)
                    self.response(seq, timeout=1)
                except Exception:
                    pass
            # Always reap the owned group, including children surviving adapter exit.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=3)
            self.reader.join(timeout=3)
            self.process.stdin.close()
            self.process.stdout.close()
