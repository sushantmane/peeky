"""Bounded asynchronous DAP transport for an owned debugger process."""

import collections
import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time

from peeky import processes
from peeky.adapters import PythonAdapter

MAX_FRAME_BYTES = 4 * 1024 * 1024


class RequestFailed(RuntimeError):
    """The debugger answered and refused the request; the session itself is intact."""


class ResponseTimeout(TimeoutError):
    """A complete request received no response in time; reads may treat this as budget."""


# Ending a launched debuggee at close: SIGTERM, then SIGKILL after this many seconds.
TERMINATE_GRACE = 1.0
# How often the session's processes are listed (#143), and at most how often a stop or
# an announcement lists them again.
FAMILY_INTERVAL = 1.0
FAMILY_MIN_GAP = 0.25
MAX_HEADER_BYTES = 8192
MAX_CONTROL_EVENTS = 2000
# The adapter's own stderr: the last STDERR_TAIL bytes are kept for diagnosis, in lines
# of at most STDERR_LINE bytes.
STDERR_TAIL = 16 * 1024
STDERR_LINE = 4096
# Fatal patterns are scanned across reads with this much of the previous bytes, so a
# match split between reads, or deep in an overlong line, is still found.
STDERR_OVERLAP = 1024
LIFECYCLE_EVENTS = frozenset(
    {"initialized", "stopped", "continued", "terminated", "exited", "breakpoint", "process"}
)
# Chatty events left out of the trace (still counted): java-debug sends a `telemetry`
# event after every stackTrace.
_UNTRACED_EVENTS = frozenset({"output", "thread", "telemetry"})
_JDK_FRAMES = ("at java.", "at jdk.", "at sun.", "at com.sun.")


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
    def __init__(self, command=None, *, env=None, fatal_stderr=None):
        """``fatal_stderr`` maps a regex to a message: an adapter stderr line matching it
        means the adapter can no longer work (e.g. a dead event thread), and fails the
        transport with that message (``{excerpt}``: the line and its stack frames)."""
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
            stderr=subprocess.PIPE,
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
        # Every process the session started, listed while their ancestry still shows
        # it, and the debuggees the adapter announced (`process`, `processid`): close()
        # ends them even once their parents are gone, never a reused pid (#143).
        # The adapter leads a session of its own (start_new_session): what is still in
        # it was started from it, even once its parents are gone (#168).
        self.family = processes.Family(
            self.process.pid,
            session=self.process.pid
            if processes.session_id(self.process.pid) == self.process.pid
            else None,
        )
        self.announcing = []
        self.family_wake = threading.Event()
        # What close() did about the processes the session started (#143).
        self.cleanup = None
        # Set only when a write failed; a read-side failure still allows `disconnect`.
        self.write_error = None
        self.started = time.monotonic()
        self.output = collections.deque(maxlen=500)
        self.output_truncated = False
        self.sequence = 0
        self.event_sequence = 0
        self.error = None
        self.fatal_stderr = [
            (re.compile(pattern), text) for pattern, text in (fatal_stderr or {}).items()
        ]
        self.stderr_tail = collections.deque()
        self.stderr_bytes = 0
        self.stderr_truncated = False
        # The adapter's own diagnosis of why it cannot go on (a fatal stderr pattern).
        self.fatal = None
        # close() asks the stderr reader to stop; it sets `stderr_quiet` once it has
        # stopped or reported a fatal line.
        self.stderr_stop = threading.Event()
        self.stderr_quiet = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_reader.start()
        self.family_watch = threading.Thread(target=self._watch_family, daemon=True)
        self.family_watch.start()

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
                            self._announce(pid)
                    if body["event"] == "stopped" and (wake := getattr(self, "family_wake", None)):
                        # What the program has started so far, while its tree shows it.
                        wake.set()
                    self.event_sequence += 1
                    body["cursor"] = self.event_sequence
                    body["received"] = time.monotonic()
                    last = self.events[-1] if self.events else None
                    if body["event"] == "continued" and last and last["event"] == "continued":
                        # LLDB says `continued` after each of its own stops it resumes from
                        # (a step out's breakpoint hit by a deeper call, a condition that
                        # held false): one entry stands for a run of them, from the first
                        # one's cursor, so a deep step out never evicts the stops (#162).
                        body["since"] = last.get("since", last["cursor"])
                        body["repeated"] = last.get("repeated", 1) + 1
                        self.events[-1] = body
                    else:
                        self.events.append(body)
                else:
                    if body["event"] == "processid":
                        # java-debug announces the launched JVM's pid (liveness, #27).
                        pid = (body.get("body") or {}).get("processId")
                        if type(pid) is int and pid > 1:
                            self.debuggee_pid = pid
                            self._announce(pid)
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
            if isinstance(exc, EOFError) and getattr(self, "fatal_stderr", None):
                # An adapter that exits right after saying why on stderr: read that first,
                # so its diagnosis is the transport's error, not "adapter closed".
                self.stderr_quiet.wait(0.5)
            self._fail(exc)

    def _keep_stderr(self, text):
        with self.condition:
            self.stderr_tail.append(text)
            self.stderr_bytes += len(text) + 1
            while self.stderr_bytes > STDERR_TAIL and len(self.stderr_tail) > 1:
                self.stderr_bytes -= len(self.stderr_tail.popleft()) + 1
                self.stderr_truncated = True

    def _keep_lines(self, data):
        """Keep complete lines (cut to STDERR_LINE) in the tail; return the remainder."""
        *lines, rest = data.split(b"\n")
        if len(rest) > STDERR_LINE:
            lines.append(rest)
            rest = b""
        for raw in lines:
            self._keep_stderr(raw[:STDERR_LINE].decode("utf-8", "replace").rstrip("\r"))
        return rest

    def _read_stderr(self):
        """Own and drain the adapter's stderr: keep a bounded tail of lines, and fail the
        transport on a fatal pattern, after the stack frames that follow it (once stderr
        is quiet for 0.2 s, 1 s at most). Patterns are scanned across reads, so neither an overlong line nor a
        split write hides one, and a last line without a newline still counts.

        close() asks this reader to stop rather than closing the descriptor under it:
        a descendant outside the adapter's group may keep the pipe open, and a read
        after the number was reused would take another file's data. The reader closes
        the descriptor itself as it stops.
        """
        stream = self.process.stderr
        pending, overlap, fatal, deadline, cap = b"", b"", None, 0.0, 0.0
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(stream.fileno(), selectors.EVENT_READ)
                while not self.stderr_stop.is_set():
                    if fatal is not None and time.monotonic() >= deadline:
                        self._fail_fatal(*fatal)
                        fatal = None
                    wait = 0.1 if fatal is None else min(0.1, deadline - time.monotonic())
                    if not selector.select(max(0.0, wait)):
                        continue
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        break
                    pending = self._keep_lines(pending + chunk)
                    if fatal is not None:
                        fatal[1].append(chunk.decode("utf-8", "replace"))
                        deadline = min(cap, time.monotonic() + 0.2)
                        if sum(map(len, fatal[1])) > STDERR_TAIL:
                            deadline = 0.0
                        continue
                    window = overlap + chunk
                    overlap = window[-STDERR_OVERLAP:]
                    if not self.fatal_stderr or self.fatal:
                        continue
                    text = window.decode("utf-8", "replace")
                    for pattern, message in self.fatal_stderr:
                        if found := pattern.search(text):
                            fatal = (message, [text[found.start() :]])
                            cap = time.monotonic() + 1.0
                            deadline = time.monotonic() + 0.2
                            break
        except (OSError, ValueError):
            pass
        finally:
            if pending:
                self._keep_stderr(pending[:STDERR_LINE].decode("utf-8", "replace").rstrip("\r"))
            if fatal is not None:
                self._fail_fatal(*fatal, ended=True)
            try:
                stream.close()
            except OSError:
                pass
            self.stderr_quiet.set()

    def _fail_fatal(self, message, pieces, ended=False):
        """Fail with the fatal line (from the pattern on) and its first stack frame
        outside the JDK (the adapter's own code, where it failed); the whole trace
        stays in the stderr tail. A last line still being written is not a frame,
        unless the stream ``ended`` there."""
        lines = [line.strip() for line in "".join(pieces).split("\n")]
        complete = lines if ended or len(lines) == 1 else lines[:-1]
        frames = [line for line in complete[1:41] if line.startswith(("at ", "Caused by"))]
        own = [line for line in frames if not line.startswith(_JDK_FRAMES)]
        excerpt = " ".join([lines[0][:600], *(own or frames)[:1]])[:600]
        with self.condition:
            self.fatal = self.fatal or message.format(excerpt=excerpt)
        self._fail(RuntimeError(self.fatal))
        self.stderr_quiet.set()

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
                first = self.events[0] if self.events else None
                if first and after < first.get("since", first["cursor"]) - 1:
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
                    **({"repeated": event["repeated"]} if "repeated" in event else {}),
                }
                for event in list(self.events)[-limit:]
            ]
            return {
                "recent": recent,
                "ignored": dict(self.ignored_events),
                "trace": list(getattr(self, "trace", ()))[-60:],
                # The adapter's own log (java-debug: java.util.logging), last 16 KiB.
                "stderr": "\n".join(getattr(self, "stderr_tail", ())),
                "stderr_truncated": getattr(self, "stderr_truncated", False),
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

    def _announce(self, pid):
        """Remember an announced debuggee's start, off the reader thread (ps on macOS),
        and list the session's processes while its ancestry still shows them."""

        def remember():
            if (start := processes.start_time(pid)) is not None:
                self.family.announce(pid, start)
            self.family_wake.set()

        thread = threading.Thread(target=remember, daemon=True)
        self.announcing.append(thread)
        thread.start()

    def _list_family(self):
        """List the session's processes: ({pid: entry} still running, the adapter ran),
        or (None, ran) if the process table cannot be read."""
        table = processes.table()
        # Asked after the table was read: still running then, it was running while the
        # table was read, and its children had not been handed to another parent yet
        # (an exiting adapter closes its pipes first, and is a zombie only after). Unreaped,
        # the adapter's pid cannot name another process (a zombie keeps it).
        running = self.process.poll() is None
        if table is None:
            return None, running
        return self.family.record(table, running)[0], running

    def _watch_family(self):
        """List the session's processes every FAMILY_INTERVAL, and soon after a stop or
        an announcement: what a program started is known before its parents die."""
        listed = 0.0
        while not self.closed:
            self.family_wake.wait(FAMILY_INTERVAL)
            if (gap := listed + FAMILY_MIN_GAP - time.monotonic()) > 0:
                time.sleep(gap)
            self.family_wake.clear()
            if self.closed:
                return
            try:
                self._list_family()
            except Exception:
                pass
            listed = time.monotonic()

    def _signal(self, pids, sig, table):
        """Send ``sig`` to each of ``pids`` that is still the process listed, and to the
        process groups they prove the session's. The adapter's group is ended apart."""
        for pid, (_, _, start, *_) in pids.items():
            entry = table.get(pid)
            if processes.alive(entry) and entry[2] == start:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
        for group in self.family.groups(pids, table):
            try:
                os.killpg(group, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def _remaining(self, owned):
        table = processes.table() or {}
        left = {
            pid: entry
            for pid, entry in owned.items()
            if processes.alive(table.get(pid)) and table[pid][2] == entry[2]
        }
        return left, table

    def _kill_group(self, sig=signal.SIGKILL):
        """Signal the adapter's own process group (macOS: EPERM once only zombies)."""
        try:
            os.killpg(self.process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _end_owned(self, owned, ran, before):
        """End what the session started and the adapter left running: SIGTERM, then
        SIGKILL after TERMINATE_GRACE, process groups included (the adapter's too, which
        a launched JVM shares). ``owned`` was listed before `disconnect`, ``ran`` says
        whether the adapter was running then, and ``before`` is when the listing before
        that one was made. Returns what happened."""
        adapter = self.process.pid
        if owned is None:
            self._kill_group()
            return (
                "could not read the process table; only the debugger's own process group was ended"
            )
        # The debuggee and what it started: announced by the adapter, or outside the
        # adapter's group (debugpy's launcher puts the program in a group of its own).
        # The adapter's own helpers end with its group and are not named.
        announced = {*list(self.family.announced), self.process_pid, self.debuggee_pid}
        named = sorted(
            pid for pid, entry in owned.items() if entry[1] != adapter or pid in announced
        )
        left, table = self._remaining({pid: owned[pid] for pid in named})
        self._signal(left, signal.SIGTERM, table)
        self._kill_group(signal.SIGTERM)
        for sig, grace in ((signal.SIGKILL, TERMINATE_GRACE), (None, 1.0)):
            deadline = time.monotonic() + grace
            while left and time.monotonic() < deadline:
                time.sleep(0.05)
                left, table = self._remaining(left)
            if sig is None:
                break
            self._kill_group(sig)
            if not left:
                break
            self._signal(left, sig, table)
        if left:
            listed = ", ".join(map(str, sorted(left)))
            return f"pid {listed} did not exit after SIGKILL (stuck in the kernel?)"
        if named:
            listed = ", ".join(map(str, named))
            done = f"terminated pid{'s' if len(named) > 1 else ''} {listed}"
        else:
            done = "nothing Peeky could identify was still running"
        if ran:
            return done
        # Its tree is gone: a process started after the last listing, in a session of
        # its own, whose parent has exited, cannot be told from anyone else's.
        age = time.monotonic() - (before or self.started)
        return (
            f"{done}; the debugger was gone: a process the program started in the last "
            f"{age:.1f} s, in a session of its own and whose parent had exited, may survive"
        )

    def snapshot_output(self):
        with self.condition:
            return "".join(self.output), self.output_truncated

    def close(self, terminate=True):
        """Disconnect and reap the adapter, and return what cleanup did (``cleanup``).

        A launched debuggee ends with the session, wherever its process group: the
        adapter is asked first (`disconnect`), then Peeky ends what is left (#143). An
        attached program is detached, never killed: Peeky did not start it.
        """
        with self.close_lock:
            if self.closed:
                return self.cleanup
            self.closed = True
            self.family_wake.set()  # the watcher stops
            owned, ran, before = {}, True, self.family.listed
            if terminate:
                for thread in list(self.announcing):
                    # A debuggee announced just before the adapter died is still ours.
                    thread.join(timeout=1)
                try:
                    owned, ran = self._list_family()
                except Exception:
                    owned = None
            if self.process.poll() is None:
                try:
                    seq = self.send("disconnect", {"terminateDebuggee": terminate}, timeout=0.25)
                    self.response(seq, timeout=1)
                except Exception:
                    pass
            if stop := getattr(self, "stderr_stop", None):
                # The stderr reader stops and closes its descriptor itself (_read_stderr).
                stop.set()
            if terminate:
                try:
                    self.cleanup = self._end_owned(owned, ran, before)
                except Exception as exc:
                    self.cleanup = f"cleanup failed ({type(exc).__name__}: {exc})"[:300]
            else:
                self.cleanup = "detached; the attached program keeps running"
            # Always reap the owned group, including children surviving adapter exit.
            self._kill_group()
            self.process.wait(timeout=3)
            self.reader.join(timeout=3)
            if stderr_reader := getattr(self, "stderr_reader", None):
                stderr_reader.join(timeout=1)
            self.process.stdin.close()
            self.process.stdout.close()
            return self.cleanup
