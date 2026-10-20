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


class RequestFailed(RuntimeError):
    """The debugger answered and refused the request; the session itself is intact."""


class ResponseTimeout(TimeoutError):
    """A complete request received no response in time; reads may treat this as budget."""


MAX_HEADER_BYTES = 8192
MAX_CONTROL_EVENTS = 2000
LIFECYCLE_EVENTS = frozenset(
    {"initialized", "stopped", "continued", "terminated", "exited", "breakpoint", "process"}
)
# Chatty events left out of the trace (still counted): java-debug sends a `telemetry`
# event after every stackTrace.
_UNTRACED_EVENTS = frozenset({"output", "thread", "telemetry"})


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
        if b":" not in line:
            # e.g. the adapter's own JVM printing a warning to its protocol stream.
            raise ValueError(
                f"Debugger adapter wrote a non-DAP line to its protocol stream: {line[:120]!r}"
            )
        key, value = line.decode("ascii").split(":", 1)
        key = key.lower()
        if key in headers:
            raise ValueError("Duplicate DAP header")
        headers[key] = value.strip()
    if "content-length" not in headers:
        raise ValueError(f"DAP frame has no Content-Length header: {sorted(headers)[:5]}")
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
        # Bounded message summaries (no arguments or values) for diagnosing lost events.
        self.trace = collections.deque(maxlen=200)
        # Requests kept out of the trace (Peeky's own bounded polls), until their reply.
        self.untraced = set()
        self.debuggee_pid = None
        # The program's exit status from the `exited` event, when the debugger sends one.
        self.exit_code = None
        # The DAP `process` event's pid (debugpy, lldb-dap): cleanup backstop on close.
        self.process_pid = None
        # Set only when a write failed; a read-side failure still allows `disconnect`.
        self.write_error = None
        self.started = time.monotonic()
        self.output = collections.deque(maxlen=500)
        self.output_truncated = False
        self.sequence = 0
        self.event_sequence = 0
        self.error = None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _note(self, direction, body):
        trace = getattr(self, "trace", None)
        if trace is None:
            return
        kind = body.get("type")
        untraced = getattr(self, "untraced", None)
        if kind == "response" and untraced and body.get("request_seq") in untraced:
            untraced.discard(body.get("request_seq"))
            return
        entry = {
            "t": round(time.monotonic() - getattr(self, "started", 0.0), 3),
            "dir": direction,
            "type": kind,
            "name": body.get("command") or body.get("event"),
        }
        if kind == "response":
            entry.update(request_seq=body.get("request_seq"), success=body.get("success"))
        elif kind == "request":
            entry["seq"] = body.get("seq")
        elif kind == "event" and body.get("event") not in _UNTRACED_EVENTS:
            detail = body.get("body") or {}
            entry.update(reason=detail.get("reason"), thread=detail.get("threadId"))
        if kind != "event" or body.get("event") not in _UNTRACED_EVENTS:
            trace.append(entry)

    def annotate(self, name, **fields):
        """Add one of Peeky's own diagnostics (not a DAP message) to the trace.

        Consecutive notes of one name merge (`times` counts them), so a long run of
        periodic notes cannot evict the DAP messages before it.
        """
        with self.condition:
            trace = getattr(self, "trace", None)
            if trace is None:
                return
            entry = {
                "t": round(time.monotonic() - getattr(self, "started", 0.0), 3),
                "dir": "peeky",
                "type": "note",
                "name": name,
                **fields,
            }
            last = trace[-1] if trace else {}
            if last.get("dir") == "peeky" and last.get("name") == name:
                trace[-1] = {**entry, "times": last.get("times", 1) + 1}
            else:
                trace.append(entry)

    def _accept(self, body):
        with self.condition:
            self._note("in", body)
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
                    if body["event"] == "exited":
                        code = (body.get("body") or {}).get("exitCode")
                        if type(code) is int and -(2**31) <= code < 2**32:
                            self.exit_code = code
                    if body["event"] == "process" and self.process_pid is None:
                        pid = (body.get("body") or {}).get("systemProcessId")
                        if type(pid) is int and pid > 1:
                            self.process_pid = pid
                    self.event_sequence += 1
                    body["cursor"] = self.event_sequence
                    body["received"] = time.monotonic()
                    self.events.append(body)
                else:
                    if body["event"] == "processid":
                        # java-debug announces the launched JVM's pid (liveness, #27).
                        pid = (body.get("body") or {}).get("processId")
                        if type(pid) is int and pid > 1:
                            self.debuggee_pid = pid
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

    def send(self, command, arguments=None, timeout=5, *, trace=True):
        """Write one complete frame within a deadline, or poison the transport.

        Never hold the response/event condition while waiting for pipe capacity.
        A partial write is an uncertain request: callers must close the session.
        """
        deadline = time.monotonic() + timeout
        if not self.write_lock.acquire(timeout=max(0, timeout)):
            exc = TimeoutError("Debugger write lock timed out; outcome may be uncertain")
            self.write_error = str(exc)
            self._fail(exc)
            raise exc
        try:
            with self.condition:
                # After a read-side failure only `disconnect` may still go out: it is
                # how the adapter is told to terminate the debuggee it launched.
                if self.write_error or (self.error and command != "disconnect"):
                    raise RuntimeError(self.error or self.write_error)
                self.sequence += 1
                seq = self.sequence
                if trace:
                    self._note("out", {"type": "request", "command": command, "seq": seq})
                else:
                    if len(self.untraced) >= 1024:
                        self.untraced.clear()
                    self.untraced.add(seq)
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
            self.write_error = str(exc)
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
            # DAP puts the human-readable reason in body.error.format when it has one.
            error = (value.get("body") or {}).get("error") or {}
            reason = error.get("format") if isinstance(error, dict) else None
            raise RequestFailed(
                str(reason)[:300]
                if isinstance(reason, str) and reason
                else value.get("message", "Debugger request failed")
            )
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

    def request(self, command, arguments=None, *, timeout=15, trace=True):
        """One absolute deadline covers both the write and the response."""
        deadline = time.monotonic() + timeout
        seq = self.send(command, arguments, timeout=max(0.001, min(5, timeout)), trace=trace)
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
            return {
                "recent": recent,
                "ignored": dict(self.ignored_events),
                "trace": list(getattr(self, "trace", ()))[-60:],
            }

    def pending_stop(self, after):
        """(receive time, reason) of the first unconsumed stop after ``after``, else None."""
        with self.condition:
            for event in self.events:
                if event["cursor"] > after and event["event"] in ("stopped", "terminated"):
                    if event["event"] != "stopped":
                        return None
                    return event["received"], event.get("body", {}).get("reason")
            return None

    def event_cursor(self):
        with self.condition:
            return self.event_sequence

    def events_between(self, after, upto, names):
        with self.condition:
            return [e for e in self.events if after < e["cursor"] <= upto and e["event"] in names]

    def _leftover_debuggee(self):
        """("group"|"pid", id) of a launched debuggee that left the adapter's group.

        debugpy starts the program in its own process group, so killing the adapter's
        group does not reach it. Only a process whose ancestry runs through the
        adapter's group is ours: a reused pid can never match.
        """
        pid = self.process_pid
        if not pid:
            return None
        try:
            os.kill(pid, 0)
            group = os.getpgid(pid)
        except OSError:
            return None
        if group == self.process.pid:
            return None
        try:
            listing = subprocess.run(
                ["ps", "-A", "-o", "pid=,ppid=,pgid="],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        table = {}
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) == 3 and all(part.isdigit() for part in parts):
                table[int(parts[0])] = (int(parts[1]), int(parts[2]))
        node = pid
        for _ in range(16):
            if node not in table:
                return None
            parent, node_group = table[node]
            if node != pid and node_group == self.process.pid:
                return ("group", group) if group == pid else ("pid", pid)
            node = parent
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
            leftover = self._leftover_debuggee() if terminate else None
            # Always reap the owned group, including children surviving adapter exit.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if leftover is not None:
                try:
                    if leftover[0] == "group":
                        os.killpg(leftover[1], signal.SIGKILL)
                    else:
                        os.kill(leftover[1], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.process.wait(timeout=3)
            self.reader.join(timeout=3)
            self.process.stdin.close()
            self.process.stdout.close()
