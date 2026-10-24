"""Single-session HTTP debugger worker. Bind locally; tunnel for remote use."""

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import signal
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

from peeky.adapters import PythonAdapter, ViewBudgetExhausted
from peeky.dap import DAP, RequestFailed, ResponseTimeout
from peeky.errors import _RULES, PROTOCOL_VERSION, classify
from peeky.identifiers import changeable, is_located, is_name, member_name, reachable
from peeky.names import display_function
from peeky.text_sets import Memory as TextMemory
from peeky.text_sets import change as change_text
from peeky.text_sets import held_in_place, parse_text, text_kind

VIEW_SECONDS = 10
# C and C++ previews (#136): their own budget per inspection or expansion, so that
# they never take the one value views and string checks use (#128).
PREVIEW_FETCHES = 256
PREVIEW_SECONDS = 3
MAX_HISTORY_BYTES = 4_000_000
MAX_HISTORY_ENTRY_BYTES = 256_000


OPERATIONS = frozenset(
    {
        "launch",
        "wait",
        "inspect",
        "expand",
        "preview",
        "set_variable",
        "breakpoints",
        "continue",
        "step",
        "report",
        "close",
        "probe",
        "shutdown",
        "version",
    }
)
# Stops the pause budget (--max-pause) may auto-resume: Peeky's own breakpoints and the
# user's own steps. Crashes, exceptions and signals stay stopped for inspection.
RESUMABLE_STOPS = frozenset({"breakpoint", "step", "interrupted"})
# Stops where a user's step left the thread, maybe outside the program's sources: its
# end, or (LLDB) the point another thread's stop at the same time caught it (#162).
STEP_STOPS = frozenset({"step", "interrupted"})
STEPS = {"over": "next", "in": "stepIn", "out": "stepOut"}
# The frames a step's start is compared by with the stop it returns to (#155).
ORIGIN_LEVELS = 10
# The deepest stack a session step's note compares (#262): deeper ones get no note.
CALL_FRAMES = 256
# Why a step out's function shows no value (#155).
ABANDONED = (
    "it never returned: its caller went on without its return (an exception, longjmp or "
    "panic ended it), so a value the debugger lists would be another call's"
)
INLINED = (
    "it was inlined into its caller: it has no return of its own, so there is no value to read"
)
# Lost `stopped` events on attached JVMs (#27): see find_lost_stop. java-debug reports a
# breakpoint stop within milliseconds. Polls start LOST_STOP_POLL_S after a resume and
# run at most that often. Each is one `threads` request and one-frame `stackTrace`
# requests: at most LOST_STOP_THREADS threads, the JVM's own threads and rechecks of a
# suspect, all within LOST_STOP_BUDGET_S. Evidence needs LOST_STOP_SAMPLES clean
# sightings in a row spanning LOST_STOP_SPAN_S.
LOST_STOP_POLL_S = 2.0
LOST_STOP_THREADS = 256
LOST_STOP_BUDGET_S = 1.0
LOST_STOP_SAMPLES = 3
LOST_STOP_SPAN_S = 4.0
# Threads every JVM runs with Java frames: suspended only when every thread is (a
# class-load or other suspend-all event), never by a line breakpoint of the session.
JVM_THREADS = frozenset({"Reference Handler", "Finalizer", "Common-Cleaner"})


class SessionFailed(RuntimeError):
    """The session failed for good and was closed; it keeps only its final report."""


class LostStop(SessionFailed):
    """The debugger made a stop it never reported (#27); the session was detached."""


# Debugger placeholders for a local that has no value at this point (optimized out,
# or not live at this line). They are text, not values, and must never be shown or
# changed as if they were.
_UNAVAILABLE = re.compile(
    r"^<(?:error: .*|variable not available|optimized out|unavailable|not available|"
    r"value optimized out)>$",
    re.IGNORECASE | re.DOTALL,
)


def optimization_level(identity):
    """The build's optimization level when it is not a no-optimization debug build."""
    identity = identity or {}
    level = identity.get("optimization")
    if level not in (None, "O0"):
        return f"-{level}"
    rust = (identity.get("profile") or {}).get("CARGO_PROFILE_DEV_OPT_LEVEL")
    if rust not in (None, "0"):
        return f"opt-level={rust}"
    return None


def _same_number(shown, read):
    """Whether two decimal integers as shown (`50`, `-3`) are the same number."""
    pattern = re.compile(r"-?[0-9]{1,40}")
    return (
        isinstance(shown, str)
        and isinstance(read, str)
        and bool(pattern.fullmatch(shown.strip()))
        and bool(pattern.fullmatch(read.strip()))
        and int(shown) == int(read)
    )


def is_unavailable(value):
    """LLDB reports "no value" as text such as `<error: variable not available>` or
    `<error: could not evaluate DW_OP_entry_value: ...>`; none of it is a value."""
    return isinstance(value, str) and bool(_UNAVAILABLE.match(value.strip()))


def _process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class StackPage(list):
    """The frames of one `stackTrace` reply, innermost first, and whether they are the
    whole stack (``complete``): fewer than ``levels`` asked for (DAP: a shorter reply
    ends the stack), and no more in its `totalFrames` (#187 review: a partial page is
    never taken for the whole stack)."""

    def __init__(self, frames, levels, total, failed=False):
        super().__init__(frames)
        self.complete = (
            not failed and len(self) < levels and not (type(total) is int and total > len(self))
        )


# Operations that resume the program or return its values: a remote Python session's
# sources are rechecked before and after each (#249).
SOURCE_CHECKED = frozenset(
    {"wait", "continue", "step", "inspect", "expand", "preview", "set_variable", "report"}
)
# A remote client's request holds the sha256 of every source (#249): up to 4096 of them.
MAX_REQUEST_BYTES = 4 * 1024 * 1024


class DebugWorker:
    def __init__(self, root, adapter=None):
        self.root = Path(root).resolve()
        # Adapter configuration is trusted startup input, never HTTP request data.
        self.adapter = adapter if adapter is not None else PythonAdapter()
        self.runtime = None
        # A remote Python session's sources as accepted at launch (#249): rechecked around
        # every later operation, since the program may import any of them at any time.
        self.accepted_sources = None
        # The sources a remote client compared at launch (#249): their names mark its
        # results' locations (#252).
        self.remote_names = None
        # A stack frame in a file under the root that was not compared (#249): it taints
        # the session until it is closed.
        self.foreign_frame = None
        # A remote Python run's bytecode cache: outside the root, removed at close; a probe
        # child makes it under its parent's scratch (`pycache_base`).
        self.pycache = None
        self.pycache_base = None
        self.capabilities = {}
        self.dap = None
        self.state = "idle"
        self.session = None
        self.stop_id = None
        self.thread_id = None
        self.frames = set()
        self.listed_frames = []
        self.deeper_frames = None
        self.thread_count = None
        self.whole_frames = None
        self.innermost_frame = None
        self.references = set()
        # Scope handles of module globals: listed for reading, never changed.
        self.global_handles = set()
        self.launched_at = None
        # Whether the current launch has asked the debugger to start (or attach to) the
        # program: an error before that is one before launch (#190).
        self.launch_requested = False
        # A file written just before that request, for a probe child's watchdog.
        self.launch_marker = None
        self.reference_frames = {}
        self.reference_adapter = {}
        self.reference_counter = 0
        self.children = {}
        # {handle: taint}: values inside a local whose pointers may reach freed memory
        # (a runtime's aggregate_taint), passed on to everything inside them.
        self.taints = {}
        # {handle: (note, refuses)}: values inside a local shown with a note (#140), which
        # everything read through it carries (#148).
        self.notes = {}
        # (name, why) for roots a rule left unmarked in the last inspection (unsettable_here).
        self.unsettable_reasons = []
        # Verdicts behind [settable] marks on fields and elements, by where the program is
        # stopped (see mark_place): a loop's later hits at the same place reuse them.
        self.marks = {}
        self.inspection_nodes = 0
        self.view_fetches = 0
        self.preview_fetches = 0
        self.preview_deadline = 0.0
        self.previewing = False
        self.view_items = 0
        self.view_bytes = 0
        self.view_deadline = 0.0
        # Seconds the value views spent waiting for the debugger's replies (#156 review).
        self.view_waited = 0.0
        self.view_timed_out = False
        self.audit, self.audit_sizes, self.audit_bytes = [], [], 0
        self.breakpoint_events = []
        # Breakpoint count per source path, and whether the current stop stopped every
        # thread (then other threads' stops in the same event are still pending).
        self.breakpoint_counts = {}
        self.stop_all_threads = False
        self.lock = threading.Lock()
        self.last_used = time.monotonic()
        # Optional per-session pause budget: a stop older than this is auto-resumed so
        # a slow client cannot stall a program with real-time deadlines.
        self.max_pause = None
        self.stopped_at = None
        self.auto_resumed = []
        self.cursor = 0
        self.resumed_at = 0.0
        self.liveness_checked = 0.0
        self.empty_thread_replies = 0
        self.exit_inferred = False
        self.program = None
        self.stop_location = None
        self.stop_function = None
        self.stop_reason = None
        self.points, self.temporary_line = [], None
        # Stops at the session's own breakpoints so far (None once a stop could not be
        # classified: numbers are never guessed); the current stop's number.
        self.hits, self.stop_hit, self.stop_line = 0, None, None
        # Lines the session's own breakpoints were requested at or placed on, their
        # adapter ids, and every one-shot `--to` line the session has used.
        self.own_lines, self.own_ids, self.temporary_lines = set(), set(), set()
        self.stop_outside = self.stop_returns = self.co_served = False
        # Where the last step started (#155): the function it may leave, and the stack
        # its return must come back to, to tell whose value a debugger lists after it;
        # and, while a step out runs, what watches its caller (NativeSession.watch_return).
        self.step_origin = None
        self.return_watch = None
        # The stop a step out began at, where this stop interrupted it and its watch
        # proves the caller's frame is the one the step was returning to (#174).
        self.step_held = None
        self.reset_calls()
        self.forget_places()
        # {line: stops}: the launch's breakpoint lines that are loop headers stopping
        # only before their loop, so many times each time it starts (#161).
        self.loop_lines = {}
        self.stop_place = None
        self.reset_lost_stops()
        # A session that failed on a lost stop (#27): its id, error and final report.
        self.failed = None
        # What closing the last session did to the processes it started (#143).
        self.last_cleanup = None
        # The output closing the last session left, and whether it is whole (#203).
        self.last_final = None
        # The single absolute launch budget, below Client.call's 45 s HTTP timeout.
        # Preparation (supports_deadline adapters: builds, symbols, module identity)
        # and every DAP launch phase receive only the remaining time, so a launch
        # either returns within this bound or is discarded and an abandoned slow
        # launch cannot leave a session. Cleanup after expiry has its own bounded
        # grace (DAP close) outside this budget; adapters do not declare their own.
        self.launch_deadline_s = 40

    def candidate_edit(self, program, edit):
        """(SourceEdit, sha256 of the edited text) for a candidate launch, else (None, None).

        The edit is staged into a fresh build by the same mechanism as `verify`; the
        program's own file is never written.
        """
        if edit is None:
            return None, None
        if not getattr(self.adapter, "supports_edits", False):
            from peeky.probe import CANDIDATE_NEEDS_BUILD

            raise ValueError(CANDIDATE_NEEDS_BUILD)
        if (
            not isinstance(edit, dict)
            or set(edit) != {"old", "new"}
            or not all(isinstance(edit[key], str) for key in ("old", "new"))
        ):
            raise ValueError("edit must be an object with string fields old and new")
        from peeky.build import SourceEdit

        old, new = edit["old"], edit["new"]
        text = program.read_text()
        if not old or old == new or text.count(old) != 1:
            raise ValueError("--old must match the --at file exactly once and --new must change it")
        candidate = text.replace(old, new, 1).encode()
        return (
            SourceEdit(program.relative_to(self.root).as_posix(), old, new),
            hashlib.sha256(candidate).hexdigest(),
        )

    def path(self, name):
        path = (self.root / name).resolve()
        if self.root not in path.parents or not path.is_file():
            raise ValueError("Expected an existing file inside the worker root")
        return path

    def stopped(self, args):
        if self.state != "stopped" or args.get("stop_id") != self.stop_id:
            if any(entry["stop_id"] == args.get("stop_id") for entry in self.auto_resumed):
                raise ValueError(
                    f"Stale stop_id: the program was auto-resumed after its {self.max_pause} s "
                    "pause budget (--max-pause); wait for the next stop or use a probe"
                )
            raise ValueError("Stale stop_id or program is not stopped; inspect the latest stop")

    def optimized(self):
        return optimization_level(getattr(self.runtime, "target_identity", None))

    def placeholders(self):
        # LLDB (C, C++, Rust) writes "no value" as `<error: ...>` text. Elsewhere such
        # text could be a real value (e.g. a Python repr), so it is not reinterpreted.
        return bool(getattr(self.runtime, "reports_unavailable_as_text", False))

    def debuggee_gone(self, runtime):
        """Liveness check for launched JVMs whose adapter drops exit events (#27).

        java-debug can answer `threads` with an empty list for a live, churning JVM
        (a thread collected mid-lookup), so an empty reply is only a hint. When the
        adapter announced the JVM pid, exit is inferred only if that process is gone;
        without a pid, only after a second empty reply at least two seconds later.
        A slow or failed check means "still running": it never closes the session.
        """
        if not getattr(runtime, "liveness_by_threads", False) or self.state != "running":
            return False
        now = time.monotonic()
        if now - self.resumed_at < 2 or now - self.liveness_checked < 2:
            return False
        self.liveness_checked = now
        try:
            threads = self.dap.request("threads", {}, timeout=2).get("threads")
        except (TimeoutError, RuntimeError):
            self.empty_thread_replies = 0
            return False
        if threads:
            self.empty_thread_replies = 0
            return False
        self.empty_thread_replies += 1
        pid = getattr(self.dap, "debuggee_pid", None)
        if pid:
            # The announced pid is the JVM itself: only its absence proves an exit.
            # A zombie or reused pid can only look alive, which fails closed (timeout).
            return not _process_alive(pid)
        return self.empty_thread_replies >= 2

    def reset_lost_stops(self):
        """Forget what lost-stop detection (#27) knows; every session starts clean.

        ``placed``: the session's breakpoints per resolved source path, as the adapter
        placed them (line, id, verified). ``stepping``: threads with a step in flight.
        ``candidate``: the thread the last polls found alone suspended at a breakpoint.
        ``sighted``: the latest such sighting whose stop was never reported, kept so an
        exit after it is not mistaken for a line that never ran (then ``unreported``).
        """
        self.placed, self.stepping, self.candidate, self.sighted = {}, set(), None, None
        self.unreported = None
        self.lost_stop_polled = time.monotonic()
        self.lost_stop_offset = 0

    def mark_resumed(self):
        """The program runs again: restart the liveness and lost-stop clocks."""
        self.resumed_at = self.lost_stop_polled = time.monotonic()
        self.empty_thread_replies = 0
        self.candidate = None

    def note_placed(self, source, requested, reply):
        """Record where the adapter placed one source's breakpoints (DAP `setBreakpoints`
        replaces them all for that source). Unreadable replies place nothing."""
        try:
            key = str(Path(source).resolve())
        except (OSError, ValueError):
            return
        placed = []
        for point, answer in zip(requested, (reply or {}).get("breakpoints") or []):
            if not isinstance(answer, dict):
                continue
            line = answer.get("line", point.get("line"))
            if type(line) is int:
                placed.append(
                    {
                        "id": answer.get("id"),
                        "line": line,
                        "verified": answer.get("verified") is True,
                    }
                )
        self.placed[key] = placed

    def note_breakpoint_update(self, body):
        """Apply a DAP `breakpoint` event (a pending breakpoint verified once its class
        loads, or moved) to the placed breakpoint with the same id."""
        point = body.get("breakpoint") or {}
        if point.get("id") is None:
            return
        for placed in self.placed.values():
            for entry in placed:
                if entry["id"] != point["id"]:
                    continue
                if type(point.get("line")) is int:
                    entry["line"] = point["line"]
                entry["verified"] = body.get("reason") != "removed" and (
                    point.get("verified") is True if "verified" in point else entry["verified"]
                )

    def take_breakpoint_event(self, event):
        """Consume one DAP `breakpoint` event: kept for clients (`breakpoint_events`),
        a moved own breakpoint's new line, and the placed breakpoint's verification."""
        self.cursor = event["cursor"]
        self.breakpoint_events.append(event.get("body", {}))
        self.breakpoint_events = self.breakpoint_events[-100:]
        moved = event.get("body", {}).get("breakpoint") or {}
        if moved.get("id") in self.own_ids and type(moved.get("line")) is int:
            self.own_lines.add(moved["line"])
        self.note_breakpoint_update(event.get("body") or {})

    def drain_breakpoint_events(self):
        """Consume the `breakpoint` events that come before any stop or exit, and only
        those: with no client waiting (the pause guard), a breakpoint that was pending
        at launch is verified only by such an event. A stop or exit is left for a
        `wait`, and so is a failed transport (its client gets DEBUGGER_ERROR); the
        worker lock keeps this and a client's `wait` apart."""
        while True:
            try:
                event = self.dap.wait_event({"stopped", "terminated", "breakpoint"}, self.cursor, 0)
            except (TimeoutError, RuntimeError):
                return
            if event["event"] != "breakpoint":
                return
            self.take_breakpoint_event(event)

    def placed_breakpoint(self, frame):
        """The session's verified breakpoint (resolved source path, line) that a frame is
        exactly at, else None."""
        path, line = (frame.get("source") or {}).get("path"), frame.get("line")
        if not isinstance(path, str) or not path or type(line) is not int:
            return None
        try:
            key = str(Path(path).resolve())
        except (OSError, ValueError):
            return None
        if any(entry["verified"] and entry["line"] == line for entry in self.placed.get(key, ())):
            return key, line
        return None

    def lost_stop_due(self, runtime=None):
        """Whether a lost-stop poll (see find_lost_stop) may run now."""
        runtime = runtime or self.runtime
        return bool(
            getattr(runtime, "detects_lost_stops", False)
            and self.dap
            and self.state == "running"
            and time.monotonic() - self.lost_stop_polled >= LOST_STOP_POLL_S
            and any(entry["verified"] for placed in self.placed.values() for entry in placed)
        )

    def find_lost_stop(self, runtime):
        """Evidence that java-debug made a breakpoint stop and never reported it (#27).

        Only for runtimes that opt in (an attached JVM), while running with nothing
        reported. java-debug lists frames only for threads the debugger suspended. The
        session's breakpoints and steps suspend one thread; but java-debug's own
        class-load requests (`Breakpoint.install`) suspend every thread for a moment,
        and a thread blocked entering `synchronized` then shows the block's first line.
        So a poll is a sighting only when exactly one thread that is not stepping is
        suspended, at a verified session breakpoint's exact source and line, while every
        other thread checked runs: the JVM's own threads (before and after the rest),
        the window of other threads, and the JVM's own threads again after each of two
        re-checks of that thread. Evidence is the same thread and place in
        LOST_STOP_SAMPLES consecutive sightings spanning LOST_STOP_SPAN_S, with no
        report in between.

        Returns the evidence (a dict); "event" when a real stop or exit arrived during
        the check (it wins); else None. A slow, failed or unclean poll is no evidence
        and starts the count again. Nothing here ever serves a stop.
        """
        if not self.lost_stop_due(runtime):
            return None
        started = time.monotonic()
        self.lost_stop_polled = started
        deadline = started + LOST_STOP_BUDGET_S
        previous, self.candidate = self.candidate, None
        suspended, checked, sighting = {}, [0], None

        def top(thread):
            """The thread's top frame if the debugger has it suspended, else None."""
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("lost-stop poll budget spent")
            checked[0] += 1
            reply = self.dap.request(
                "stackTrace",
                {"threadId": thread, "levels": 1},
                timeout=remaining,
                **self.untraced(),
            )
            frames = reply.get("stackFrames") if isinstance(reply, dict) else None
            if not isinstance(frames, list) or not frames:
                return None
            return frames[0] if isinstance(frames[0], dict) else {}

        try:
            listed = self.dap.request("threads", {}, timeout=LOST_STOP_BUDGET_S, **self.untraced())
            listed = listed.get("threads") if isinstance(listed, dict) else None
            names = {}
            for thread in listed if isinstance(listed, list) else []:
                if isinstance(thread, dict) and type(thread.get("id")) is int:
                    name = str(thread.get("name") or "")
                    names[thread["id"]] = name[8:-1] if name.startswith("Thread [") else name
            ids = [thread for thread in names if thread not in self.stepping]
            witnesses = [thread for thread in ids if names[thread] in JVM_THREADS]
            known = [t for t in ids if previous and t == previous["thread"] and t not in witnesses]
            others = [t for t in ids if t not in witnesses and t not in known]
            if others:
                start = self.lost_stop_offset % len(others)
                others = others[start:] + others[:start]
            window = others[:LOST_STOP_THREADS]
            self.lost_stop_offset += len(window)
            # The JVM's own threads before and after: a suspend-all event in between
            # leaves its mark on them or on another thread checked.
            for thread in witnesses + known + window + witnesses:
                if (frame := top(thread)) is not None:
                    suspended[thread] = frame
            if len(suspended) == 1:
                ((thread, frame),) = suspended.items()
                where = None if thread in witnesses else self.placed_breakpoint(frame)
                # Twice more, each followed by the JVM's own threads: a thread a
                # suspend-all event caught for a moment is running again by then.
                for _ in range(2):
                    if where is None:
                        break
                    again = top(thread)
                    if again is None or self.placed_breakpoint(again) != where:
                        where = None
                    elif any(top(witness) is not None for witness in witnesses):
                        where = None
                if where is not None:
                    sighting = {
                        "thread": thread,
                        "name": names[thread],
                        "frame": frame,
                        "at": where,
                    }
        except (TimeoutError, RuntimeError):
            sighting = None
        if sighting:
            same = previous and (previous["thread"], previous["at"]) == (
                sighting["thread"],
                sighting["at"],
            )
            self.candidate = self.sighted = {
                **sighting,
                "since": previous["since"] if same else started,
                "samples": previous["samples"] + 1 if same else 1,
            }
        if annotate := getattr(self.dap, "annotate", None):
            annotate(
                "lost-stop poll",
                checked=checked[0],
                suspended=sorted(suspended)[:8],
                candidate=self.candidate and self.candidate["thread"],
                samples=self.candidate and self.candidate["samples"],
            )
        evidence = self.candidate
        if not evidence or evidence["samples"] < LOST_STOP_SAMPLES:
            return None
        if started - evidence["since"] < LOST_STOP_SPAN_S:
            return None
        try:
            self.dap.wait_event({"stopped", "terminated"}, self.cursor, 0)
            return "event"
        except TimeoutError:
            pass
        return evidence

    def diagnose(self, runtime, evidence):
        """What a sighting of an unreported stop shows, for messages and records."""
        frame = evidence["frame"]
        source = (frame.get("source") or {}).get("path") or ""
        path, line = evidence["at"]
        try:
            shown = Path(path).relative_to(self.root).as_posix()
        except ValueError:
            shown = _display_path(runtime, path)
        return {
            "thread": evidence["thread"],
            "thread_name": evidence["name"],
            "function": str(frame.get("name") or "")[:300] or None,
            "location": f"{Path(source).name}:{frame.get('line')}",
            "breakpoint": f"{shown}:{line}",
            "unreported_s": round(time.monotonic() - evidence["since"], 1),
            "samples": evidence["samples"],
        }

    def fail_lost_stop(self, runtime, evidence):
        """Record the evidence, detach (an attached JVM resumes) and keep the session as
        a failed one: every later request on it gets the same error, `report` the
        state at failure, and `close` ends it. Raises LostStop."""
        diagnosis = self.diagnose(runtime, evidence)
        message = (
            "Lost stop: java-debug never reported a stop it made, so Peeky served nothing "
            f"from it and detached from the program. Thread {diagnosis['thread_name']!r} (id "
            f"{diagnosis['thread']}) has been suspended for {diagnosis['unreported_s']} s in "
            f"{diagnosis['function']} at {diagnosis['location']}, this session's verified "
            f"breakpoint {diagnosis['breakpoint']}, with no other thread suspended in "
            f"{diagnosis['samples']} checks"
        )
        self.record(
            {"time": time.time(), "operation": "lost_stop", "arguments": {}, "result": diagnosis}
        )
        if annotate := getattr(self.dap, "annotate", None):
            annotate("lost stop", **diagnosis)
        report = self.report(runtime)
        report.update(state="detached", lost_stop=diagnosis)
        session = self.session
        if cleanup := self.close():
            report["cleanup"] = cleanup
        self.failed = {"session": session, "message": message, "report": report, "error": LostStop}
        raise LostStop(message)

    def abandon(self, exc, *, unattended=False):
        """Close the session after a debugger failure; returns the error to raise.

        When the adapter itself said why it cannot go on (``DAP.fatal``: java-debug's
        event thread died, #27), the session is kept as a failed one, as for a lost
        stop: its diagnosis goes to the history, later requests repeat the error, and
        `report` returns its final state with the adapter's stderr tail. So is any
        debugger failure met ``unattended`` (by the pause guard): no client was there
        to receive it, so its next request does, never "Missing or stale session_id".
        """
        fatal = getattr(self.dap, "fatal", None)
        if isinstance(exc, SessionFailed) or not self.session:
            self.close()
            return exc
        if fatal:
            message, operation, extra = fatal, "adapter_failed", {"adapter_failure": fatal}
        elif unattended and isinstance(exc, (TimeoutError, RuntimeError, OSError)):
            message, operation, extra = str(exc) or type(exc).__name__, "session_failed", {}
        else:
            self.close()
            return exc
        self.record(
            {
                "time": time.time(),
                "operation": operation,
                "arguments": {},
                "result": {"message": message},
            }
        )
        try:
            report = self.report(self.runtime)
            report.update(state="closed", failure=message, **extra)
        except Exception:
            report = {"session_id": self.session, "state": "closed", "failure": message, **extra}
        session = self.session
        if cleanup := self.close():
            report["cleanup"] = cleanup
        self.failed = {
            "session": session,
            "message": message,
            "report": report,
            "error": SessionFailed,
        }
        return SessionFailed(message)

    def untraced(self):
        """Keep lost-stop poll traffic out of the bounded DAP trace (its diagnostics)."""
        return {"trace": False} if getattr(self.dap, "annotate", None) else {}

    def read_location(self):
        """Best-effort top-frame location for display; never evidence, never fatal.

        Crash or signal stops may fail runtime validation; that belongs to `inspect`,
        so a failed read here only omits the location.
        """
        try:
            levels = 20 if self.stop_reason in STEP_STOPS else 1
            stack = self.dap.request(
                "stackTrace", {"threadId": self.thread_id, "levels": levels}, timeout=2
            )
            frames = stack.get("stackFrames") or [{}]
            frame = frames[0]
            self.stop_outside = self.stop_reason in STEP_STOPS and not _in_program_sources(
                self.runtime, frame, self.root
            )
            self.stop_returns = self.stop_outside and any(
                _in_program_sources(self.runtime, caller, self.root) for caller in frames[1:]
            )
            path = frame.get("source", {}).get("path")
            self.stop_function = str(frame.get("name") or "")[:300] or None
            self.stop_line = frame.get("line")
            self.stop_place = self.place_of(frame)
            return f"{_display_path(self.runtime, path)}:{frame.get('line')}" if path else None
        except Exception:
            return None

    def call_origin(self):
        """Where a step starts (#155): the thread, the function it may leave, and the
        frames it would return to; None if the debugger does not list them."""
        try:
            stack = self.dap.request(
                "stackTrace", {"threadId": self.thread_id, "levels": ORIGIN_LEVELS}, timeout=2
            )
        except Exception:
            # Best effort: without it, no returned value is shown after this step.
            return None
        frames = [
            _frame_identity(f)
            for f in (stack.get("stackFrames") if isinstance(stack, dict) else None) or []
            if isinstance(f, dict)
        ][:ORIGIN_LEVELS]
        if not frames or not frames[0][0]:
            return None
        listed = [f for f in stack.get("stackFrames") or [] if isinstance(f, dict)]
        return {
            "thread": self.thread_id,
            "function": frames[0][0],
            "frames": frames,
            # What a step out's watch reads (frame ids are valid only until it starts).
            "listed": listed[:2],
        }

    def left_function(self, frames):
        """Whether this stop is the step's return to the function it started in: the
        top frame is the frame that called it, at the same instruction where the
        debugger names it (where the call returns), and the frames below are the same
        frames. Not after a step that stayed in the function, or went past its caller
        (LLDB steps out of libc++'s hidden frames too, and then the value it lists is
        no longer that function's)."""
        origin = self.step_origin
        if not origin or self.stop_reason != "step" or origin["thread"] != self.thread_id:
            return False
        before, after = origin["frames"], [_frame_identity(f) for f in frames[:ORIGIN_LEVELS]]
        if len(before) < 2 or not after:
            return False
        if (before[1][0], before[1][1]) != (after[0][0], after[0][1]):
            return False
        if len(before) < ORIGIN_LEVELS and len(after) != len(before) - 1:
            # Both stacks are whole: exactly one frame fewer.
            return False
        return all(new == old for new, old in zip(after[1:], before[2:]))

    def same_call(self, frames):
        """The stop a step out began at, where this breakpoint stop interrupted it in the
        call it was leaving (#174): its watch held (`end_return_watch`), the stopped
        function is the one it was leaving, and the frames under it are as they were when
        the step began. Else None."""
        origin = self.step_origin
        if not self.step_held or not origin or self.stop_reason != "breakpoint":
            return None
        if origin["thread"] != self.thread_id:
            return None
        before = origin["frames"]
        after = [_frame_identity(f) for f in frames[:ORIGIN_LEVELS]]
        if len(before) < 2 or len(after) != len(before) or after[0][0] != before[0][0]:
            return None
        return self.step_held if after[1:] == list(before[1:]) else None

    def returned_value(self, runtime, frames, entry):
        """What the function a step just left returned (#155), as the debugger lists it in
        the frame it returned to (`(Return Value)`, java-debug's `->name()`): {function,
        type, value}, or {function, unavailable, reason} where Peeky cannot vouch for it.
        None if the stop is not such a return, or the debugger never lists one (debugpy:
        only by writing it into the program's globals, which Peeky does not ask for)."""
        if getattr(runtime, "return_entry", None) is None:
            return None
        origin = self.step_origin
        if origin and self.stop_reason == "step" and origin["thread"] == self.thread_id:
            if origin.get("abandoned"):
                return {"function": origin["function"], "unavailable": True, "reason": ABANDONED}
            if origin.get("inlined"):
                return {"function": origin["function"], "unavailable": True, "reason": INLINED}
        if not self.left_function(frames):
            origin = self.step_origin
            if entry is None or not origin or self.stop_reason != "step":
                return None
            if origin["thread"] != self.thread_id:
                return None
            # Listed, but not by a return Peeky can tie to a function: never shown.
            return {
                "function": self.step_origin["function"],
                "unavailable": True,
                "reason": "the step did not stop where this function returned (it went on "
                f"to {frames[0].get('name') if frames else 'another frame'}), so the value the "
                "debugger lists may be another function's",
            }
        function = self.step_origin["function"]
        returned = {"function": function}
        if self.step_origin.get("moved"):
            return {
                **returned,
                "unavailable": True,
                "reason": "its caller's stack pointer is not where the call left it, so this "
                "is not that call's return",
            }
        if why := self.step_origin.get("unwatched"):
            return {
                **returned,
                "unavailable": True,
                "reason": f"Peeky could not tell its return from a later call's: {why}",
            }
        named = getattr(runtime, "return_entry", lambda variable: None)
        if entry is None and (by_command := getattr(runtime, "returned_by_command", None)):
            # lldb-dap before LLVM 21 lists none; LLDB itself still has it.
            entry = by_command(self.dap, self.thread_id)
        if entry is None:
            reason = (
                getattr(
                    runtime,
                    "no_return",
                    "the debugger reported no value (a function returning nothing, or a "
                    "value it cannot read)",
                )
                if self.step_origin.get("kind", "out") == "out"
                else "the debugger reports what a function returned only after `step --out`"
            )
        elif (name := named(entry)) and name != _method_name(function):
            reason = f"the debugger lists the value {name!r} returned, not this function's"
        elif level := self.optimized():
            reason = (
                f"optimized build ({level}): the function may have been inlined, and the "
                "register the debugger reads may not hold what it returned"
            )
        elif (why := getattr(runtime, "return_withheld", lambda variable: None)(entry)) is not None:
            reason = why
        elif not isinstance(entry.get("value"), str) or (
            self.placeholders() and is_unavailable(entry["value"])
        ):
            reason = f"the debugger could not read it ({str(entry.get('value'))[:200]})"
        else:
            kind, value = entry.get("type"), entry["value"]
            if shown := getattr(runtime, "return_shown", None):
                # As the language names and prints it (Rust: u8 65, not 'A').
                kind, value = shown(entry)
            return {
                **returned,
                "value": str(value)[:2000],
                **({"type": kind[:300]} if isinstance(kind, str) and kind else {}),
            }
        return {**returned, "unavailable": True, "reason": reason}

    def end_return_watch(self, runtime, watch, event):
        """A step out's watch on its caller ends with the step's first stop (#155). A stop
        at one of its watched instructions, on its thread, in a frame at or above the
        caller's, means the call never returned: the caller went on without it. That stop
        ends the step (it is no breakpoint of the session's, unless one of those is there
        too). A step's end anywhere the stack pointer is not the caller's is not the
        call's return either.

        A breakpoint on its thread that interrupts the step (not at a watched instruction)
        is in the call the step was leaving when the watch was kept whole, the stopped
        frame's caller is at the stack pointer the call's caller had, and the frames are
        that call's (`inspect` compares them): another call returning to the same place
        in a caller frame there would have had to run the watched call instruction there
        first (#174). `step_held` names the stop the step began at."""
        ids, reason = (event.get("body") or {}).get("hitBreakpointIds"), self.stop_reason
        # lldb-dap names one breakpoint of those at an address: where it is, decides.
        seen = runtime.unwatch_return(
            self.dap,
            watch,
            self.thread_id if self.stop_id and reason in ("step", "breakpoint") else None,
            **({"caller": True} if reason == "breakpoint" else {}),
        )
        origin = self.step_origin
        if seen is None or not origin or origin["thread"] != self.thread_id:
            return
        if reason == "breakpoint" and seen["pc"] in watch["addresses"]:
            if seen["sp"] >= watch["sp"]:
                origin["abandoned"] = True
                if not (isinstance(ids, list) and set(ids) & self.own_ids):
                    self.stop_reason = "step"
                return
        elif reason == "step" and seen["sp"] != watch["sp"]:
            origin["moved"] = True
        if traps := seen.get("dropped"):
            # A recursion through the caller ran its watched instructions that often,
            # and LLDB dropped the watch to keep the step's cost bounded (#162).
            origin["unwatched"] = (
                f"its caller's watched instructions ran over {traps} times in deeper calls "
                "during the step (a recursion through its caller), so the watch was "
                "dropped to keep the step's cost bounded"
            )
        elif (
            reason == "breakpoint"
            and seen["pc"] not in watch["addresses"]
            and seen.get("caller_sp") is not None
            and seen["caller_sp"] == watch["sp"]
            and origin.get("stop_id")
        ):
            self.step_held = origin["stop_id"]

    def stack_depth(self):
        """The number of frames on the stopped thread's stack (every one listed)."""
        reply = self.dap.request(
            "stackTrace", {"threadId": self.thread_id, "startFrame": 0, "levels": 0}, timeout=10
        )
        return len(reply.get("stackFrames", []))

    def reset_calls(self):
        """Forget where the last `session step` started (#262), and what the current
        stop's comparison with it found."""
        self.calls_from = None
        self.forget_calls()

    def forget_calls(self):
        """A new stop (or none): ``stop_names``, the names the innermost frame's
        inspections listed at it; ``call_change``, how its stack relates to the one the
        last session step started from; ``names_before``, after a step that stayed in its
        function at its depth, the names listed at an earlier stop there."""
        self.stop_names = set()
        self.call_change = self.names_before = None

    def call_stack(self):
        """The stopped thread's whole stack, innermost first, as `_frame_identity`s; None
        where it is deeper than CALL_FRAMES, or the debugger does not say the reply is all
        of it (`totalFrames`), or does not answer within 2 s. Bounded, so a deep recursion's
        stack never makes a reply too large to read (best effort: then no note is given)."""
        try:
            reply = self.dap.request(
                "stackTrace",
                {"threadId": self.thread_id, "startFrame": 0, "levels": CALL_FRAMES},
                timeout=2,
            )
        except Exception:
            return None
        frames = reply.get("stackFrames") if isinstance(reply, dict) else None
        if not isinstance(frames, list) or not frames or len(frames) > CALL_FRAMES:
            return None
        # lldb-dap says more frames follow by a total past those listed; debugpy and
        # java-debug give the exact total. Anything else is not known to be whole.
        if reply.get("totalFrames") != len(frames) or not all(isinstance(f, dict) for f in frames):
            return None
        return [_frame_identity(frame) for frame in frames]

    def mark_calls(self):
        """Where a session's `step` starts (its request's `calls`), for the step's own stop
        to be compared with (#262): the whole stack, and the names the innermost frame
        listed here, or at an earlier stop in the same function at the same depth."""
        stack = self.call_stack()
        names, line = self.stop_names, self.stop_line
        if not names and self.names_before:
            names, line = self.names_before["names"], self.names_before["line"]
        self.calls_from = (
            None
            if stack is None
            else {"thread": self.thread_id, "stack": stack, "names": frozenset(names), "line": line}
        )

    def compare_calls(self):
        """At the first stop after a session's step: where it is the step's own end (reason
        `step`, on its thread), how its stack relates to the one the step started from
        (`call_change`), or, where the innermost function and its depth and callers are
        the same, which names were listed earlier there (`names_before`).

        Stacks are compared, not calls: two activations of a function at one depth, under
        callers at the same lines, look the same. So nothing is said after a `continue`,
        or after a step a breakpoint, exception or pause stopped first, where the program
        may have left the call and made another; after a step's own end the stacks say
        only what they show."""
        mark, self.calls_from = self.calls_from, None
        if not (
            self.stop_id
            and mark
            and mark["thread"] == self.thread_id
            and self.stop_reason == "step"
        ):
            return
        after = self.call_stack()
        change = call_change(mark["stack"], after) if after else None
        if change is None:
            return
        if change["kind"] != "same":
            self.call_change = change
        elif mark["names"] and type(mark["line"]) is int:
            # The same function at the same depth, under the same callers: a Java step
            # stayed in its call; LLDB's may have run on to a later call from the same
            # place after an exception or `longjmp` ended it. Either way, a name listed
            # earlier in this function and not here is out of scope at this stop (scopes
            # are the code's, not the call's).
            self.names_before = {"names": mark["names"], "line": mark["line"]}

    def take_returned(self, runtime, variables):
        """(variables without it, the entry) where a scope lists what a function returned
        (`(Return Value)`, java-debug's `->name()`); None if it lists none."""
        named = getattr(runtime, "return_entry", None)
        if named is None:
            return None
        found = [v for v in variables if isinstance(v, dict) and named(v) is not None]
        if not found:
            return None
        return [v for v in variables if not any(v is f for f in found)], found[0]

    def library_stop(self, runtime, frames):
        """A step's stop in a library frame (outside the program's sources) that returned
        from the program's function: where it is and what that function returned, never
        the library's own locals (#155). None if the step returned from nothing."""
        if not self.step_origin or getattr(runtime, "return_entry", None) is None:
            return None
        scopes = self.dap.request("scopes", {"frameId": frames[0]["id"]}).get("scopes", [])
        entry = None
        for scope in runtime.local_scopes(scopes)[:16]:
            variables = self.dap.request(
                "variables", {"variablesReference": scope["variablesReference"]}
            ).get("variables", [])
            if (taken := self.take_returned(runtime, variables)) is not None:
                entry = taken[1]
                break
        returned = self.returned_value(runtime, frames, entry)
        if returned is None:
            return None
        source = (frames[0].get("source") or {}).get("path")
        return {
            "stop_id": self.stop_id,
            **(
                {"location": f"{_display_path(runtime, source)}:{frames[0].get('line')}"}
                if isinstance(source, str) and source
                else {}
            ),
            "function": str(frames[0].get("name") or "")[:300],
            "frame": 0,
            "frames": _public_sources(runtime, frames),
            "scopes": [],
            "returned": returned,
            "outside_program": True,
            "returns_to_program": self.stop_returns,
            "library_frame": (
                "The step stopped outside the program's sources: the library's locals are "
                "not read. "
                + (
                    "`session step --out` returns to your code, or `session continue`"
                    if self.stop_returns
                    else "Your code is no longer on the stack: `session continue`"
                )
            ),
            **self.pause_status(),
            **({"optimized": level} if (level := self.optimized()) else {}),
        }

    def location(self):
        where = {"location": self.stop_location} if self.stop_location else {}
        if self.stop_location and self.stop_function:
            where["function"] = self.stop_function
        if self.stop_id and self.stop_hit:
            where["hit"] = self.stop_hit
        if self.stop_id and self.stop_place:
            # Which of the line's breakpoint locations in this function the stop is at
            # (#157): 1 to `places`, in address order.
            where["place"], where["places"] = self.stop_place
        if self.stop_id:
            # This worker numbers hits (older ones never send `hit`); False once a stop
            # could not be classified.
            where["numbered"] = self.hits is not None
        if self.stop_id and self.stop_outside:
            # A step that left the program (into a library, past main): nothing to read.
            where["outside_program"] = True
            where["returns_to_program"] = self.stop_returns
        if self.stop_id and self.call_change:
            # Another function or depth than where the session's step started (#262).
            where["call_change"] = dict(self.call_change)
        return {**where, "reason": self.stop_reason} if self.stop_id else where

    def pause_status(self):
        if self.state != "stopped" or self.stopped_at is None:
            return {}
        paused = round(time.monotonic() - self.stopped_at, 2)
        status = {"paused_s": paused}
        if self.max_pause and self.stop_reason in RESUMABLE_STOPS:
            status["resume_in_s"] = round(max(0.0, self.max_pause - paused), 2)
        return status

    def guard_pause(self):
        """Auto-resume a stop that outlived the session's pause budget.

        Called periodically by the worker server. Only stops at Peeky's own breakpoints
        and the user's own steps are resumed; crashes, exceptions and signals stay stopped for inspection (and
        resuming a fault would usually re-fault). Any failure releases the program by
        closing the session (detaching an attached JVM) rather than leaving it paused.
        """
        with self.lock:
            if not self.dap:
                return
            try:
                if fatal := getattr(self.dap, "fatal", None):
                    # The adapter said it cannot go on (java-debug's event thread died,
                    # #27): close now, keeping the failed session (abandon), whoever waits.
                    raise RuntimeError(fatal)
                if not self.max_pause:
                    return
                if self.state == "running":
                    pending = self.dap.pending_stop(self.cursor)
                    if pending is None:
                        # No reported stop; one whose report was lost (#27) is found by
                        # the same bounded poll `wait` uses, and then budgeted like any.
                        # The poll needs a verified breakpoint, and a pending one is only
                        # verified by a `breakpoint` event no client may be there to take.
                        if getattr(self.runtime, "detects_lost_stops", False):
                            self.drain_breakpoint_events()
                        if getattr(self.dap, "error", None) or not self.lost_stop_due():
                            # A failed transport is the client's to see (DEBUGGER_ERROR).
                            return
                    elif pending[1] not in RESUMABLE_STOPS:
                        return
                    elif time.monotonic() - pending[0] < self.max_pause:
                        return
                    self._dispatch("wait", {"timeout": 0.1})
                if self.state != "stopped" or self.stopped_at is None:
                    return
                if self.stop_reason not in RESUMABLE_STOPS:
                    return
                paused = time.monotonic() - self.stopped_at
                if paused < self.max_pause:
                    return
                entry = {
                    "stop_id": self.stop_id,
                    "reason": self.stop_reason,
                    "location": self.stop_location,
                    "paused_s": round(paused, 2),
                }
                self._dispatch("continue", {"stop_id": self.stop_id})
                self.auto_resumed.append(entry)
                self.auto_resumed = self.auto_resumed[-100:]
                self.record(
                    {
                        "time": time.time(),
                        "operation": "auto_resume",
                        "arguments": {"stop_id": entry["stop_id"]},
                        "result": {**entry, "max_pause": self.max_pause},
                    }
                )
            except Exception as exc:
                import sys

                failure = self.abandon(exc, unattended=True)
                if isinstance(failure, SessionFailed):
                    # Closed; requests on the session get the same error.
                    print(f"peeky: pause guard: {failure}", file=sys.stderr)
                else:
                    import traceback

                    print("peeky: pause guard failed; closing the session", file=sys.stderr)
                    traceback.print_exc()

    def dispatch(self, operation, args):
        try:
            self.validate(operation, args)
        except Exception as exc:
            if operation == "launch":
                exc.before_launch = True
            raise
        with self.lock:
            failed = self.failed
            if operation != "launch" and failed and args.get("session_id") == failed["session"]:
                # A session that failed for good (#27: a lost stop, a dead adapter event
                # thread) is already closed: it only reports what it knew, closes, or
                # repeats its error. Nothing is served.
                # What ending it did to the processes it started (#143), as on any error.
                cleanup = (failed["report"] or {}).get("cleanup")
                if operation == "close":
                    self.failed = None
                    return {"state": "idle", **({"cleanup": cleanup} if cleanup else {})}
                if operation == "report":
                    return failed["report"]
                error = failed["error"](failed["message"])
                if cleanup:
                    error.cleanup = cleanup
                raise error
            if operation != "launch":
                if not self.dap or args.get("session_id") != self.session:
                    raise ValueError("Missing or stale session_id")
            # Activity counts from both ends of an operation, so a long operation is never
            # mistaken for idleness the moment it returns; rejected requests do not count.
            self.last_used = time.monotonic()
            if operation == "launch":
                self.failed = None
                self.launch_requested = False
                self.last_final = None
            self.last_cleanup = None
            try:
                try:
                    if operation in SOURCE_CHECKED:
                        self.check_sources()
                    result = self._dispatch(operation, args)
                    if operation in SOURCE_CHECKED or operation == "launch":
                        # Values from sources that changed meanwhile are not returned.
                        self.check_sources()
                    if operation != "close":
                        self.check_stack()
                except (TimeoutError, RuntimeError, OSError) as exc:
                    failure = self.abandon(exc)
                    if failure is exc:
                        raise
                    raise failure from exc
            except Exception as exc:
                if self.last_cleanup and not self.dap:
                    # The error says what became of the processes the session started.
                    exc.cleanup = self.last_cleanup
                if operation == "launch" and not self.launch_requested:
                    # Before launch: the build, its checks or the debugger's start failed
                    # before the program was asked to start (#190).
                    exc.before_launch = True
                raise
            finally:
                self.last_used = time.monotonic()
            if operation != "report":
                self.record(
                    {
                        "time": time.time(),
                        "operation": operation,
                        "arguments": args,
                        "result": result,
                    }
                )
            return result

    def record(self, entry):
        """Append to the report history, bounded in entries and bytes.

        An oversized result (a large inspect snapshot) keeps its size and digest; the
        oldest entries go first once the whole history passes MAX_HISTORY_BYTES.
        """
        text = json.dumps(entry["result"], default=str, sort_keys=True)
        if len(text) > MAX_HISTORY_ENTRY_BYTES:
            entry = {
                **entry,
                "result": {
                    "omitted": "result too large for the history",
                    "bytes": len(text),
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                },
            }
            text = json.dumps(entry["result"])
        self.audit.append(entry)
        self.audit_sizes.append(len(text))
        self.audit_bytes += len(text)
        while len(self.audit) > 500 or (
            self.audit_bytes > MAX_HISTORY_BYTES and len(self.audit) > 1
        ):
            self.audit.pop(0)
            self.audit_bytes -= self.audit_sizes.pop(0)

    def pending_stop_elsewhere(self):
        """Whether another thread, stopped separately (java-debug), awaits service.

        All-threads stops (LLDB) are covered by co_stopped/co_served; their other
        pending events go stale once every thread resumes.
        """
        if self.stop_all_threads:
            return False
        events_between = getattr(self.dap, "events_between", None)
        cursor = getattr(self.dap, "event_cursor", None)
        if not (events_between and cursor):
            return False
        return any(
            event["event"] == "stopped" and event.get("body", {}).get("threadId") != self.thread_id
            for event in events_between(self.cursor, cursor(), {"stopped"})
        )

    def record_own_points(self, requested, placed):
        """Remember where the session's own breakpoints are: requested and placed lines,
        and their ids (later `breakpoint` events for those ids add their new lines)."""
        self.own_lines.update(
            point["line"] for point in requested if type(point.get("line")) is int
        )
        for reply in (placed or {}).get("breakpoints") or []:
            if type(reply.get("line")) is int:
                self.own_lines.add(reply["line"])
            if reply.get("id") is not None:
                self.own_ids.add(reply["id"])

    def forget_places(self):
        # {line: [(function, its locations' addresses, ascending)]}: where the debugger
        # placed the session program's breakpoints (#157), by the code holding them.
        self.places = {}
        # What they were asked from (runtime, request, reply): a stop at an address none
        # of them names asks once more (placed before the program loaded, unslid).
        self.places_asked = None
        self.places_renewed = False
        # Some were listed before the program loaded (`m[0x…], unresolved`): their counts
        # hold, but their addresses are the module's, which a stop's pc may match by
        # chance once the program is slid. No stop is labelled from them (review 2 of
        # #160): the first stop asks again.
        self.places_unresolved = False

    def note_places(self, runtime, requested, placed):
        """Ask the runtime where the debugger placed the session program's breakpoints
        (#157): a line can have several locations in one function (a macro's or a
        block's own code, a loop header's), and it stops at each. Best effort: none
        known means no warning and no `place`, never a failure."""
        self.forget_places()
        self.places_asked = (runtime, list(requested), placed)
        hook = getattr(runtime, "breakpoint_locations", None)
        replies = (placed.get("breakpoints") or []) if isinstance(placed, dict) else []
        ids = [
            reply["id"]
            for reply in replies
            if isinstance(reply, dict)
            and reply.get("verified") is True
            and type(reply.get("id")) is int
        ]
        if hook is None or not ids or self.dap is None:
            return
        try:
            found = hook(self.dap, ids)
        except Exception:
            return
        groups = {}
        self.places_unresolved = any(location.get("resolved") is False for location in found or ())
        for location in found or ():
            group = groups.setdefault(
                (location["line"], location["place"]), (location["function"], set())
            )
            group[1].add(location["address"])
        for (line, _), (function, addresses) in groups.items():
            self.places.setdefault(line, []).append((function, tuple(sorted(addresses))))

    def loop_headers(self, runtime):
        """{line: stops} of the session's breakpoint lines that are loop headers whose
        every location runs once each time the loop starts, before it (#161): a C, C++
        `for`/`while` header binds its first code, Rust's `for` its two. Asked of the
        runtime once, at launch, before the program runs (the debugger's disassembly and
        line table, kept for the launch); never again when breakpoints are set again or
        their places asked again (review of #166). Best effort."""
        hook = getattr(runtime, "loop_header_stops", None)
        if hook is None or self.dap is None or not self.places:
            return {}
        try:
            source = runtime.source_path(self.program)
        except Exception:
            return {}
        found, cache = {}, {}
        for line in sorted(self.places)[:8]:
            groups = [addresses for _, addresses in self.places[line]]
            try:
                count = hook(self.dap, source, line, groups, cache)
            except Exception:
                count = None
            if count:
                found[line] = count
        return found

    def loop_warnings(self):
        """The note for each breakpoint line that is a loop header stopping only before
        its loop (#161)."""
        from peeky.loop_headers import loop_header_note

        notes = []
        for line, count in sorted(self.loop_lines.items()):
            if line not in self.places:
                continue
            count, exact = count
            note = loop_header_note(line, count, exact)
            if count > 1:
                note += f" (each hit's stop, 1 to {count}, says which)"
            notes.append(note + ".")
        return notes[:5]

    def place_of(self, frame):
        """(index, count) of the breakpoint location a stop's ``frame`` is at among its
        line's locations in the same code, in address order, where there are several
        (#157); else None."""
        if self.stop_reason != "breakpoint" or frame.get("line") not in self.places:
            return None
        text = frame.get("instructionPointerReference")
        if not isinstance(text, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,16}", text):
            return None
        pc = int(text, 16)
        for _ in range(2):
            if not self.places_unresolved:
                for _, addresses in self.places.get(frame.get("line"), ()):
                    if pc in addresses:
                        if len(addresses) == 1:
                            return None
                        return (addresses.index(pc) + 1, len(addresses))
            if self.places_renewed or self.places_asked is None:
                return None
            # Placed before the program loaded, at addresses it may no longer have (listed
            # as unresolved, or none matches): asked once more, now that it runs.
            self.note_places(*self.places_asked)
            self.places_renewed = True
        return None

    def place_warnings(self):
        """A warning for each line that stops more than once per pass in a function
        (#157): --hits, --set-hit and --set-at's @HIT count stops. Inlined copies of one
        function, which read alike, are said once."""
        labels = self.place_labels(
            function for groups in self.places.values() for function, _ in groups
        )
        notes = []
        for line in sorted(self.places):
            if line in self.loop_lines:
                # Its loop note says more: none of those stops is at a pass.
                continue
            for function, addresses in self.places[line]:
                if len(addresses) > 1:
                    count = len(addresses)
                    notes.append(
                        f"Line {line} stops up to {count} times per pass in "
                        f"{labels[function][:200]} ({count} breakpoint locations): --hits, "
                        "--set-hit and --set-at's @HIT count stops, not passes (as do session "
                        f"hit numbers); each hit's stop (1 to {count}, in address order) says "
                        "which location."
                    )
        return list(dict.fromkeys(notes))[:5]

    def place_labels(self, names):
        """{debugger function name: how a warning names it}: as tables show it, a Rust
        generic's instance with its type arguments (`r1::gener<i32>`, #151) where the
        shortened names would read alike; the whole name where that still does."""
        names = list(dict.fromkeys(names))
        shown = {name: display_function(name) for name in names}
        alike = [name for name in names if list(shown.values()).count(shown[name]) > 1]
        named = getattr(self.runtime, "instance_name", None)
        for name in alike:
            try:
                instance = named({"name": name}) if named else None
            except Exception:
                instance = None
            if instance:
                shown[name] = instance
        return {
            name: label if list(shown.values()).count(label) == 1 or name not in alike else name
            for name, label in shown.items()
        }

    def classify_hit(self):
        """Number this stop if it is at one of the session's own breakpoints.

        The session's breakpoints are its own and its one-shot `--to` lines. A stop at
        an own line (requested or placed) is a hit; one at a `--to` line that is not an
        own line never is, whenever it is served (LLDB serves other threads' stops
        before resuming, java-debug after). A stop whose line could not be read is the
        session's own while no `--to` was ever used. Anything else (a program's own
        `breakpoint()` call, a line Peeky did not place) is never guessed: numbering ends.
        """
        if self.stop_reason != "breakpoint" or self.hits is None:
            return
        line = self.stop_line
        if line in self.own_lines or (line is None and not self.temporary_lines):
            self.hits += 1
            self.stop_hit = self.hits
        elif line is None or line not in self.temporary_lines:
            self.hits = None

    def set_points(self, runtime, points, temporary=None):
        """Replace the session program's breakpoints (deduplicated by line)."""
        unique = list({point["line"]: point for point in points}.values())
        runtime.validate_breakpoints(unique)
        if check_lines := getattr(runtime, "check_breakpoint_lines", None):
            check_lines(self.program, unique)
        placed = self.dap.request(
            "setBreakpoints",
            {"source": {"path": str(runtime.source_path(self.program))}, "breakpoints": unique},
        )
        self.note_placed(runtime.source_path(self.program), unique, placed)
        self.note_places(runtime, unique, placed)
        self.breakpoint_counts[str(self.program)] = len(unique)
        self.temporary_line = temporary
        if temporary is not None:
            self.temporary_lines.add(temporary)
        replies = (placed or {}).get("breakpoints") or []
        self.record_own_points(
            [point for point in unique if point["line"] != temporary],
            {
                "breakpoints": [
                    reply for point, reply in zip(unique, replies) if point["line"] != temporary
                ]
            },
        )
        if temporary is None:
            return
        point = ((placed or {}).get("breakpoints") or [])[
            [entry["line"] for entry in unique].index(temporary) :
        ][:1]
        point = point[0] if point else {}
        # Runtimes that already proved the line from their own line tables (Java) report
        # `verified: false` until the class loads: only a moved line is refused there.
        pending_ok = getattr(runtime, "check_breakpoint_lines", None) is not None
        if point.get("line", temporary) != temporary or (
            point.get("verified") is not True and not pending_ok
        ):
            # The debugger would stop elsewhere (or nowhere): restore the session's own
            # breakpoints and keep this stop, rather than run on.
            self.set_points(runtime, self.points)
            moved = (
                f" (the debugger would use line {point['line']})"
                if point.get("line") not in (None, temporary)
                else ""
            )
            raise ValueError(
                f"Line {temporary} has no code to stop at{moved}; choose a line from "
                "`peeky describe`"
            )

    def breakpoint_notes(self, program, args):
        """Runtime warnings about the requested breakpoint lines (display only)."""
        notes = getattr(self.runtime, "breakpoint_notes", None)
        try:
            return (
                [str(note)[:500] for note in notes(program, args.get("breakpoints", []))][:5]
                if notes
                else []
            )
        except (KeyError, TypeError, ValueError, OSError):
            return []

    def has_memory_location(self, variable):
        """False only when the adapter reports memory locations (lldb-dap 20+) and this
        value has none: it is computed, so writing it changes only the debugger's copy."""
        return not (
            getattr(self.runtime, "requires_memory_location", False)
            and self.capabilities.get("supports_read_memory")
            and not variable.get("memoryReference")
        )

    def forget_stop(self):
        self.stop_outside = self.stop_returns = False
        self.step_origin = self.step_held = None
        self.forget_calls()
        self.stop_hit = self.stop_line = self.stop_place = None
        self.state = "running"
        self.stopped_at = None
        self.stop_location = None
        self.stop_function = None
        self.stop_reason = None
        self.children.clear()
        self.taints.clear()
        self.notes.clear()
        self.stop_id = None
        self.frames.clear()
        self.listed_frames = []
        self.deeper_frames = None
        self.thread_count = None
        self.whole_frames = None
        self.references.clear()
        self.reference_frames.clear()
        self.reference_adapter.clear()

    def co_stopped(self):
        """The next unserved breakpoint stop of another thread in this all-threads stop."""
        events_between = getattr(self.dap, "events_between", None)
        cursor = getattr(self.dap, "event_cursor", None)
        if not (self.stop_all_threads and events_between and cursor):
            return None
        if not any(self.breakpoint_counts.values()):
            return None
        for event in events_between(self.cursor, cursor(), {"stopped", "continued"}):
            body = event.get("body", {})
            if event["event"] == "continued":
                return None
            if body.get("reason") == "breakpoint" and body.get("threadId") != self.thread_id:
                return event
        return None

    def check_sources(self):
        """Raise (closing the session) if a remote Python session's sources changed on
        this host since launch (#249). A file changed and restored between two checks is
        not seen."""
        accepted = self.accepted_sources
        if accepted is None or not self.dap:
            return
        if self.foreign_frame is not None:
            raise RuntimeError(self.foreign_message(self.foreign_frame))
        from peeky.sources import Unhashable, drift, python_sources

        try:
            current = dict(python_sources(self.root))
        except Unhashable as exc:
            raise RuntimeError(
                f"Worker source provenance can no longer be checked ({exc}): the session was "
                "closed, and no values were returned. Start again"
            ) from None
        if changed := drift(accepted, current):
            raise RuntimeError(
                f"Worker source provenance changed during the session: {changed} on the "
                "worker host after launch. The session was closed, and no values were "
                "returned. Sync sources, then start again"
            )

    def check_frames(self, body):
        """Raise (closing the session) for a stack frame in a file under the root that is
        not a compared .py file (#249). Frames outside the root, and in a virtual
        environment inside it (installed packages), are not compared."""
        accepted = self.accepted_sources
        if accepted is None:
            return
        from peeky.sources import in_environment

        for frame in (body or {}).get("stackFrames") or []:
            path = (frame.get("source") or {}).get("path") if isinstance(frame, dict) else None
            if not isinstance(path, str) or not os.path.isabs(path):
                continue
            name = None
            for candidate in (Path(os.path.normpath(path)), Path(path).resolve()):
                try:
                    name = candidate.relative_to(self.root).as_posix()
                    break
                except ValueError:
                    continue
            if name is None or name in accepted or in_environment(self.root, name):
                continue
            self.foreign_frame = name
            raise RuntimeError(self.foreign_message(name))

    def make_pycache(self):
        """A fresh, empty directory for a remote Python run's bytecode (#249): never in the
        target's root, or under ~/Documents. A probe child's is in its parent's scratch;
        otherwise the system's temporary directory, then the worker's own scratch."""
        documents = (Path.home() / "Documents").resolve()
        if self.pycache_base is not None:
            bases = [Path(self.pycache_base)]
        else:
            from peeky.cli import _private_dir

            bases = [None, _private_dir("scratch")]
        for base in bases:
            if base is not None:
                base.mkdir(parents=True, exist_ok=True, mode=0o700)
            made = Path(tempfile.mkdtemp(prefix="peeky-pycache-", dir=base)).resolve()
            if not (made.is_relative_to(self.root) or made.is_relative_to(documents)):
                return made
            shutil.rmtree(made, ignore_errors=True)
        raise RuntimeError(
            "No place for a remote Python run's bytecode cache outside the target's root and "
            "~/Documents: set TMPDIR (or PEEKY_STATE_DIR) to a directory outside them"
        )

    def check_stack(self):
        """At a stop of a remote Python session, read the whole stack: check_frames
        refuses a frame in a file that was not compared (#249)."""
        if self.accepted_sources is None or not self.dap or self.state != "stopped":
            return
        if self.thread_id is None:
            return
        self.dap.request(
            "stackTrace", {"threadId": self.thread_id, "startFrame": 0, "levels": 0}, timeout=10
        )

    @staticmethod
    def foreign_message(name):
        return (
            f"Worker source provenance: a stack frame is in {name}, under the worker's root "
            "but not one of the .py files compared with yours (its code names a file the "
            "worker did not compare, such as one loaded from a .pyc). The session was closed, "
            "and no values were returned"
        )

    @staticmethod
    def built_digest(runtime):
        """A compiled run's inputs_sha256: over the sources its build read."""
        built = getattr(runtime, "source_manifest", None)
        if not isinstance(built, dict) or not built:
            return None
        from peeky.sources import digest

        return digest(built)

    @staticmethod
    def validate(operation, args):
        if not isinstance(args, dict):
            raise ValueError("Expected a JSON object")
        if operation not in {
            "launch",
            "wait",
            "inspect",
            "expand",
            "preview",
            "set_variable",
            "breakpoints",
            "continue",
            "step",
            "report",
            "close",
        }:
            raise ValueError("Unknown operation")
        if operation == "step" and args.get("kind", "over") not in STEPS:
            raise ValueError("step kind must be over, in or out")
        if operation == "continue" and args.get("to_line") is not None:
            line = args["to_line"]
            if type(line) is not int or line < 1:
                raise ValueError("to_line must be a positive line number")
            if args.get("to_exit"):
                raise ValueError("Pass to_line or to_exit, not both")
        if operation == "launch" and args.get("entry") is not None:
            if not isinstance(args["entry"], str):
                raise ValueError("entry must be a string")
        if operation == "launch" and args.get("expect_sha256") is not None:
            expected = args["expect_sha256"]
            if (
                not isinstance(expected, dict)
                or set(expected) - {"program", "entry"}
                or not all(
                    isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
                    for v in expected.values()
                )
            ):
                raise ValueError("expect_sha256 maps program/entry to sha256 hex digests")
        if operation == "launch" and args.get("expect_inputs") is not None:
            from peeky.sources import check_expected

            check_expected(args["expect_inputs"])
        if operation == "launch" and args.get("args") is not None:
            from peeky.probe import _check_args

            if not isinstance(args["args"], list):
                raise ValueError("args must be a list of strings")
            _check_args(tuple(args["args"]))
        if operation == "launch" and args.get("sets") is not None:
            # Each change with the line whose stops make it (#137: a --set-at's own).
            changes = args["sets"]
            if (
                not isinstance(changes, list)
                or len(changes) > 8
                or not all(
                    isinstance(change, dict)
                    and {"line", "name"} <= set(change) <= {"line", "name", "text", "value"}
                    and change.get("text", True) is True
                    and isinstance(change.get("value", ""), str)
                    and len(change.get("value", "")) <= 10000
                    and type(change["line"]) is int
                    and change["line"] > 0
                    and isinstance(change["name"], str)
                    and 0 < len(change["name"]) <= 1000
                    for change in changes
                )
            ):
                raise ValueError(
                    "sets must be a list of at most 8 {line, name} changes (a positive line; "
                    "text: true for a text; value: the value, a string)"
                )
            from peeky.probe import parse_selector

            for change in changes:
                # Each name is a selector, as a probe's --set and --set-at take it.
                try:
                    parse_selector(change["name"])
                except ValueError as exc:
                    raise ValueError(f"sets: {exc}") from None
        if operation == "launch" and args.get("max_pause") is not None:
            value = args["max_pause"]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 300:
                raise ValueError("max_pause must be a number of seconds within (0, 300]")
        if operation == "continue" and type(args.get("to_exit", False)) is not bool:
            raise ValueError("to_exit must be a boolean")
        if operation == "step" and type(args.get("calls", False)) is not bool:
            raise ValueError("calls must be a boolean")
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
        if operation == "inspect" and "frame" in args:
            if type(args["frame"]) is not int or args["frame"] < 0:
                raise ValueError("frame must be a non-negative integer (0 is the innermost)")
            if "frame_id" in args:
                raise ValueError("Pass frame or frame_id, not both")
        if operation == "inspect" and "globals" in args and type(args["globals"]) is not bool:
            if not _name_list(args["globals"]):
                raise ValueError("globals must be true, false or a list of at most 32 names")
        if operation == "inspect" and type(args.get("depth", False)) is not bool:
            raise ValueError("depth must be true or false")
        if operation == "inspect" and "names" in args and not _name_list(args["names"]):
            raise ValueError("names must be a list of at most 32 names")
        if operation == "inspect" and type(args.get("previews", True)) is not bool:
            if not _name_list(args["previews"]):
                raise ValueError("previews must be true, false or a list of at most 32 names")
        if operation == "expand" and type(args.get("previews", False)) is not bool:
            raise ValueError("previews must be true or false")
        if operation == "preview" and (type(args.get("handle")) is not int or args["handle"] < 1):
            raise ValueError("Invalid preview handle")
        if operation == "preview" and type(args.get("pointee", False)) is not bool:
            raise ValueError("pointee must be true or false")
        if operation == "expand":
            for key, default, low, high in (
                ("handle", None, 1, None),
                ("start", 0, 0, 511),
                ("count", 64, 1, 64),
                # One element by its index, of an array of any length (`[N]` as LLDB
                # names it); instead of `start` and `count`.
                ("index", 0, 0, 2**31 - 1),
            ):
                value = args.get(key, default)
                if type(value) is not int or value < low or (high is not None and value > high):
                    raise ValueError(f"Invalid expansion {key}")
        if operation == "set_variable":
            # A root local by its scope's reference, or a member by its parent's handle.
            if ("reference" in args) == ("handle" in args):
                raise ValueError("set_variable takes a reference or a handle")
            if type(args.get("reference", args.get("handle"))) is not int:
                raise ValueError("reference must be an integer")
            if not isinstance(args.get("name"), str) or not isinstance(args.get("value"), str):
                raise ValueError("name and value must be strings")
            # Only what a selector can name (#195): a change never reaches a variable the
            # CLI could not select, such as one whose name holds an invisible character.
            name = args["name"]
            if "reference" in args and not changeable(name):
                raise ValueError(_SET_NAME)
            if "handle" in args and not member_name(name):
                raise ValueError(_SET_MEMBER)

    def _dispatch(self, operation, args):
        runtime = self.runtime or self.adapter
        if operation in {
            "wait",
            "inspect",
            "expand",
            "preview",
            "set_variable",
            "breakpoints",
            "continue",
            "step",
            "report",
        }:
            runtime.assert_current()
        if operation == "launch":
            if self.dap:
                raise ValueError("Close the current session before launching another")
            program = self.path(args["program"])
            self.program = program
            entry = None
            if args.get("args"):
                # Launched programs take per-run arguments; an attached JVM is already running.
                if not getattr(self.adapter, "accepts_arguments", False) and not isinstance(
                    self.adapter, PythonAdapter
                ):
                    raise ValueError(
                        "Program arguments (--arg) need a launched program; this target attaches "
                        "to one that is already running"
                    )
                if validate_arguments := getattr(self.adapter, "validate_arguments", None):
                    validate_arguments(tuple(args["args"]))
            if args.get("entry") is not None:
                # Python only: run one script while breaking in another module it imports.
                if not isinstance(self.adapter, PythonAdapter):
                    raise ValueError("entry (--run) is supported only for Python targets")
                entry = self.path(args["entry"])
                if entry.suffix != ".py":
                    raise ValueError("entry must be a .py file inside the root")
            # A remote client's expected source identity is checked before anything runs,
            # so a mismatched program is never executed or changed.
            for role, path in (("program", program), ("entry", entry)):
                expected = (args.get("expect_sha256") or {}).get(role)
                if expected and path is None:
                    raise ValueError(f"expect_sha256 names an {role} that was not given")
                if expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError(
                        f"Worker source provenance differs for the {role} "
                        f"{path.relative_to(self.root).as_posix()}: refused "
                        "before launching. Sync sources before debugging remotely"
                    )
            # And every other source the target reads (#249): what this worker's trusted
            # target names, or every .py file under the root for Python.
            inputs_checked = inputs_python = None
            self.accepted_sources = self.foreign_frame = self.remote_names = None
            if (expected_inputs := args.get("expect_inputs")) is not None:
                from peeky.sources import (
                    declared,
                    difference,
                    digest,
                    python_manifest_digest,
                    refusal,
                    worker_sources,
                )

                own = worker_sources(self.adapter, self.root)
                if what := difference(expected_inputs, own):
                    raise ValueError(refusal(what))
                if declared(self.adapter) is None:
                    # Python: what runs first must be among the files compared.
                    for role, path in (("program", program), ("entry script", entry)):
                        name = None if path is None else path.relative_to(self.root).as_posix()
                        if name is not None and name not in own:
                            raise ValueError(
                                refusal(
                                    f"the {role} {name} (not among the compared sources: it "
                                    "is in a virtual environment)"
                                )
                            )
                    inputs_python = python_manifest_digest(own)
                inputs_checked = digest(expected_inputs)
                self.remote_names = frozenset(expected_inputs)
            edit, candidate_sha256 = self.candidate_edit(program, args.get("edit"))
            self.adapter.validate_breakpoints(args.get("breakpoints", []))
            self.session = uuid.uuid4().hex
            # Wall-clock start: a source file changed after it may not be the code running.
            self.launched_at = time.time()
            self.audit, self.audit_sizes, self.audit_bytes = [], [], 0
            self.breakpoint_events = []
            self.breakpoint_counts = {str(program): len(args.get("breakpoints", []))}
            # The session's own breakpoints in its program, restored after a one-shot
            # `continue --to` line is reached.
            self.points = list(args.get("breakpoints", []))
            self.temporary_line = None
            self.hits, self.stop_hit, self.stop_line = 0, None, None
            self.own_lines, self.own_ids, self.temporary_lines = set(), set(), set()
            self.forget_places()
            self.loop_lines = {}
            # Nothing about an earlier session's stops carries over on a shared worker.
            self.co_served = self.stop_outside = self.stop_returns = False
            self.step_origin = self.return_watch = self.step_held = None
            self.reset_calls()
            self.stop_all_threads = False
            self.reset_lost_stops()
            self.max_pause = args.get("max_pause")
            self.stopped_at = None
            self.auto_resumed = []
            deadline = time.monotonic() + self.launch_deadline_s

            def left(limit=15):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Launch exceeded its deadline; session discarded")
                return min(limit, remaining)

            try:
                staging = {"edit": edit} if edit else {}
                if getattr(self.adapter, "supports_deadline", False):
                    prepared = self.adapter.prepare(
                        program, self.root, deadline=deadline, **staging
                    )
                else:
                    prepared = self.adapter.prepare(program, self.root, **staging)
                self.runtime = runtime = prepared
                # A candidate's copy is written while it is prepared: it is what runs, so
                # the sources' clock starts once it is written (#182).
                self.launched_at = time.time()
                runtime.assert_current()
                built = getattr(runtime, "source_manifest", None)
                if expected_inputs is not None and isinstance(built, dict):
                    # What the build read, compared again before the debugger's launch
                    # request: a source changed after the check above is refused (#249).
                    from peeky.sources import difference, refusal

                    if what := difference(expected_inputs, built):
                        raise RuntimeError(refusal(what))
                if inputs_python is not None:
                    self.accepted_sources = own
                staged = Path(runtime.source_path(program)) if edit else None
                if edit and hashlib.sha256(staged.read_bytes()).hexdigest() != candidate_sha256:
                    raise RuntimeError(
                        "Provenance: the staged candidate differs from the requested edit; the debugger's launch request was not sent"
                    )
                if args.get("sets"):
                    self.refuse_never_settable(runtime, program, args["sets"])
                left()
                self.dap = DAP(
                    command=runtime.command,
                    env=getattr(runtime, "environment", None),
                    fatal_stderr=getattr(runtime, "fatal_stderr", None),
                    provoked_exit=getattr(runtime, "provoked_exit", None),
                    diagnostics=getattr(runtime, "debugger_diagnostics", None),
                )
                if self.accepted_sources is not None:
                    self.dap.frames_check = self.check_frames
                capabilities = self.dap.request(
                    "initialize", runtime.initialize_args(), timeout=left()
                )
                self.capabilities = runtime.normalize_capabilities(capabilities)
                launch_args = (
                    runtime.launch_args(entry or program, self.root, args["args"])
                    if args.get("args")
                    else runtime.launch_args(entry or program, self.root)
                )
                if self.accepted_sources is not None:
                    # A remote Python run reads no bytecode from the root (a stale .pyc is
                    # not the compared source): its cache is a fresh, empty directory (#249).
                    self.pycache = str(self.make_pycache())
                    launch_args["env"] = {
                        **launch_args.get("env", {}),
                        "PYTHONPYCACHEPREFIX": self.pycache,
                    }
                timeout = left(5)
                # From here the program may run, even if the request then fails.
                if self.launch_marker is not None:
                    Path(self.launch_marker).write_text("requested\n")
                self.launch_requested = True
                launch = self.dap.send(
                    getattr(runtime, "dap_request", "launch"), launch_args, timeout=timeout
                )
                budget = left()
                try:
                    self.dap.wait_event({"initialized"}, failed_request=launch, timeout=budget)
                except TimeoutError as exc:
                    if getattr(runtime, "dap_request", "launch") != "launch":
                        raise
                    # Its launcher ran, but the program never reached the debugger (#143:
                    # pydevd blocked listing a directory above it).
                    raise TimeoutError(
                        "The debuggee did not start: the debugger never initialized it "
                        f"within the launch's {round(budget, 1)} s"
                    ) from exc
                if check_lines := getattr(runtime, "check_breakpoint_lines", None):
                    check_lines(program, args.get("breakpoints", []))
                points = self.dap.request(
                    "setBreakpoints",
                    {
                        "source": {"path": str(runtime.source_path(program))},
                        "breakpoints": args.get("breakpoints", []),
                    },
                    timeout=left(),
                )
                self.record_own_points(args.get("breakpoints", []), points)
                self.note_placed(runtime.source_path(program), args.get("breakpoints", []), points)
                self.note_places(runtime, args.get("breakpoints", []), points)
                # Once, before the program runs (#161): breakpoints set again later keep it.
                self.loop_lines = self.loop_headers(runtime)
                self.dap.request("configurationDone", timeout=left())
                self.dap.response(launch, timeout=left())
                runtime.assert_current()
                left()
                self.state = "running"
                self.cursor = 0
                self.mark_resumed()
                self.exit_inferred = False
                self.liveness_checked = 0.0
                # What the program actually received: per-run arguments replace the
                # configured ones, so records never show arguments it did not get. An
                # attached JVM's arguments are unknown, so none are claimed.
                launched = getattr(self.adapter, "accepts_arguments", False) or isinstance(
                    self.adapter, PythonAdapter
                )
                program_args = list(
                    args["args"]
                    if args.get("args")
                    else getattr(getattr(runtime, "target", None), "arguments", ()) or ()
                )
                identity = getattr(runtime, "target_identity", None)
                if launched and isinstance(identity, dict) and "arguments" in identity:
                    identity = {**identity, "arguments": program_args}
                return {
                    "session_id": self.session,
                    "state": self.state,
                    "breakpoints": _public_sources(runtime, points.get("breakpoints", [])),
                    "source_sha256": hashlib.sha256(program.read_bytes()).hexdigest(),
                    **({"candidate_sha256": candidate_sha256} if edit else {}),
                    **(
                        {"entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest()}
                        if entry
                        else {}
                    ),
                    "supports_set_variable": self.capabilities.get("supports_set_variable", False),
                    # Fields and array elements too (Java), not only root locals.
                    "supports_member_set": self.member_set(),
                    "supports_expand": True,
                    "protocol": PROTOCOL_VERSION,
                    "artifact_manifest_id": runtime.manifest_id,
                    "source_manifest": getattr(runtime, "source_manifest", None),
                    **({"inputs_checked": inputs_checked} if inputs_checked else {}),
                    **(
                        {"inputs_sha256": inputs_sha256}
                        if (inputs_sha256 := inputs_python or self.built_digest(runtime))
                        else {}
                    ),
                    "target_identity": identity,
                    **({"program_args": program_args} if launched else {}),
                    **({"optimized": level} if (level := self.optimized()) else {}),
                    **(
                        {"warnings": notes}
                        if (
                            notes := [
                                *self.breakpoint_notes(program, args),
                                *self.loop_warnings(),
                                *self.place_warnings(),
                                *(getattr(runtime, "warnings", None) or ()),
                            ]
                        )
                        else {}
                    ),
                }
            except Exception:
                self.close()
                raise
        if operation == "wait":
            timeout = max(0.1, min(float(args.get("timeout", 2)), 2))
            updates = {}
            if self.breakpoint_events:
                updates["breakpoint_events"] = list(self.breakpoint_events)
            if self.auto_resumed:
                updates["auto_resumed"] = len(self.auto_resumed)
                updates["last_auto_resumed"] = self.auto_resumed[-1]
            if self.state in ("stopped", "terminated"):
                return {
                    "state": self.state,
                    "stop_id": self.stop_id,
                    **({"unreported_stop": self.unreported} if self.unreported else {}),
                    **self.location(),
                    **self.pause_status(),
                    **updates,
                }
            deadline = time.monotonic() + timeout
            while True:
                try:
                    event = self.dap.wait_event(
                        {"stopped", "terminated", "breakpoint"},
                        self.cursor,
                        max(0, deadline - time.monotonic()),
                    )
                except TimeoutError:
                    if self.debuggee_gone(runtime):
                        try:
                            # A real stop or exit that arrived during the check wins.
                            event = self.dap.wait_event({"stopped", "terminated"}, self.cursor, 0)
                        except TimeoutError:
                            event = {
                                "event": "terminated",
                                "cursor": self.cursor,
                                "body": {"peeky": "exit inferred: no live threads, no exit event"},
                            }
                            self.exit_inferred = True
                        else:
                            self.cursor = event["cursor"]
                            break
                        self.record(
                            {
                                "time": time.time(),
                                "operation": "exit_inferred",
                                "arguments": {},
                                "result": {"reason": "debugger reported no live threads"},
                            }
                        )
                        break
                    evidence = self.find_lost_stop(runtime)
                    if evidence == "event":
                        # A real stop or exit arrived during the check: it wins.
                        continue
                    if evidence is not None:
                        # Never served: a stop java-debug did not report is not trusted.
                        self.fail_lost_stop(runtime, evidence)
                    return {"state": self.state, "timed_out": True, **updates}
                self.cursor = event["cursor"]
                if event["event"] != "breakpoint":
                    break
                self.take_breakpoint_event(event)
                updates["breakpoint_events"] = list(self.breakpoint_events)
                if time.monotonic() >= deadline:
                    return {"state": self.state, "timed_out": True, **updates}
            # Rebuilt classes or edited sources while running invalidate the evidence.
            runtime.assert_current()
            self.state = event["event"]
            self.frames.clear()
            self.listed_frames = []
            self.deeper_frames = None
            self.thread_count = None
            self.whole_frames = None
            self.references.clear()
            self.reference_frames.clear()
            self.reference_adapter.clear()
            self.children.clear()
            self.taints.clear()
            self.notes.clear()
            self.stop_id = uuid.uuid4().hex if self.state == "stopped" else None
            self.stopped_at = event.get("received", time.monotonic()) if self.stop_id else None
            self.thread_id = event.get("body", {}).get("threadId")
            self.stop_all_threads = event.get("body", {}).get("allThreadsStopped") is True
            self.stop_reason = event.get("body", {}).get("reason") if self.stop_id else None
            self.stop_function = self.stop_line = self.stop_hit = self.stop_place = None
            self.forget_calls()
            # This thread's step (if any) has ended; the next poll starts afresh.
            was_stepping = set(self.stepping)
            self.stepping.discard(self.thread_id)
            self.candidate = None
            self.step_held = None
            if self.return_watch is not None:
                watch, self.return_watch = self.return_watch, None
                self.end_return_watch(runtime, watch, event)
            if self.stop_id and (discard := getattr(runtime, "discard_steps", None)):
                # Every step under way at this stop is over (#162): LLDB keeps one a stop
                # interrupted, and would end it at a later `continue` with a step stop no
                # one asked for. A step that ended left nothing to discard.
                for thread in list(was_stepping):
                    left = discard(self.dap, thread)
                    self.stepping.discard(thread)
                    if left and thread == self.thread_id and self.stop_reason == "step":
                        # lldb-dap called it the step's end, but the step was still under
                        # way: another thread's stop came at the same time and caught this
                        # one mid-step (inside `usleep`).
                        self.stop_reason = "interrupted"
            if self.stop_id and self.sighted and self.sighted["thread"] == self.thread_id:
                self.sighted = None  # it was reported after all
            if self.state == "terminated" and self.sighted:
                # The program ended while a stop it made was never reported (#27).
                self.unreported = self.diagnose(runtime, self.sighted)
                self.record(
                    {
                        "time": time.time(),
                        "operation": "unreported_stop",
                        "arguments": {},
                        "result": self.unreported,
                    }
                )
            self.stop_location = self.read_location() if self.stop_id else None
            # Counted like a probe's hits: every stop at the session's breakpoints,
            # including other threads' stops served one by one; never `--to` lines.
            self.classify_hit()
            self.compare_calls()
            if self.stop_id and self.temporary_line is not None:
                # The one-shot `continue --to` line served its purpose (or another stop
                # came first): only the session's own breakpoints remain.
                self.set_points(runtime, self.points)
            return {
                "state": self.state,
                "stop_id": self.stop_id,
                "event": event,
                **({"exit_inferred": True} if self.exit_inferred else {}),
                **({"unreported_stop": self.unreported} if self.unreported else {}),
                **self.location(),
                **self.pause_status(),
                **updates,
            }
        if operation == "inspect":
            self.stopped(args)
            self.children.clear()
            self.taints.clear()
            self.notes.clear()
            self.inspection_nodes = 0
            self.reset_views()
            self.view_timed_out = False
            self.reset_previews()
            # C and C++ previews (#136): of every root, none, or the roots named.
            wanted = args.get("previews", True)
            self.previewing = wanted if type(wanted) is bool else frozenset(wanted)
            validate_children = getattr(runtime, "validate_children", None)
            # A timed-out view read may still be processed by the adapter; until its
            # reply drains, this inspection is read-only.
            drained = getattr(self.dap, "drained", None)
            quiescent = drained(1) if drained else True
            stack = self.dap.request("stackTrace", {"threadId": self.thread_id, "levels": 10})
            frames = stack.get("stackFrames", [])[:10]
            self.frames = {f["id"] for f in frames}
            # As listed, for rules that read what runs below a frame (a constructor), with
            # whether they are the whole stack.
            self.listed_frames = StackPage(frames, 10, stack.get("totalFrames"))
            self.deeper_frames = None
            self.thread_count = None
            self.whole_frames = None
            self.innermost_frame = frames[0].get("id") if frames else None
            if listed := getattr(runtime, "frames_listed", None):
                # Which frame is innermost: a caller's address is where its call returns.
                listed(frames)
            if "frame" in args:
                # An index into `frames` as this reply lists them (0 is the innermost).
                if args["frame"] >= len(frames):
                    raise ValueError(
                        f"Frame {args['frame']} is out of range: this stop lists "
                        f"{len(frames)} frame(s)"
                        + (f", 0 to {len(frames) - 1}" if frames else "")
                        + " (0 is the innermost; at most 10 are listed)"
                    )
                index = args["frame"]
            else:
                selected = args.get("frame_id", frames[0]["id"] if frames else None)
                index = next((i for i, f in enumerate(frames) if f["id"] == selected), None)
            selected = frames[index]["id"] if index is not None else None
            if selected not in self.frames:
                raise ValueError("Unknown frame")
            if [f["id"] for f in frames].count(selected) != 1:
                # Proofs that hold only in the innermost frame are keyed by its id.
                raise ValueError(
                    f"The debugger gave frame {index} the same id as another frame; it "
                    "cannot be read"
                )
            selected_frame = frames[index]
            if self.stop_reason in STEP_STOPS and not _in_program_sources(
                runtime, selected_frame, self.root
            ):
                if index == 0 and (shown := self.library_stop(runtime, frames)) is not None:
                    # What the function the step left returned is still shown (#155).
                    return {**shown, **({"depth": self.stack_depth()} if args.get("depth") else {})}
                # The user's own step left the program (into a library, or out of main):
                # nothing there is read or changed, and the session stays usable.
                raise ValueError(
                    (
                        "Another thread's stop interrupted the step"
                        if self.stop_reason == "interrupted"
                        else "The step stopped"
                    )
                    + f" outside the program's sources (in {selected_frame.get('name')}); "
                    + (
                        "`session step --out` returns to your code, or `session continue`"
                        if self.stop_returns
                        else "your code is no longer on the stack: `session continue`"
                    )
                )
            try:
                runtime.validate_stop(selected_frame)
            except RuntimeError as exc:
                if selected == frames[0]["id"] or _inside_program(runtime, selected_frame):
                    # A mismatch inside the program's own sources is provenance: fail closed.
                    raise
                # A caller frame outside the prepared program (JDK, libc, test runner) is
                # a request the stop cannot serve, not a debugger failure.
                raise ValueError(
                    f"Frame {index} ({selected_frame.get('name')}) is not in the prepared "
                    "program's sources; inspect one of your program's frames"
                ) from exc
            if validate_debugger := getattr(runtime, "validate_debugger", None):
                validate_debugger(self.dap, selected_frame)
            # Adapters may reuse raw scope IDs for different selected frames.
            # A new inspection revokes all earlier client scope handles.
            self.references.clear()
            self.reference_frames.clear()
            self.reference_adapter.clear()
            scopes = self.dap.request("scopes", {"frameId": selected}).get("scopes", [])
            is_global = getattr(runtime, "global_scope", None)
            groups = getattr(runtime, "variable_groups", frozenset())
            # Module globals are listed only on request (`true`), or for names missing
            # from the locals (a list) that the function provably reads from them:
            # listing a value runs its repr in the program.
            wanted = args.get("globals") or None
            names = {*(args.get("names") or ()), *(wanted if isinstance(wanted, list) else ())}
            listed = self.frame_scopes(runtime, selected_frame, scopes)
            available = bool(is_global) and any(is_global(scope) for scope in listed)
            pending, own, proof, given = [], [], None, None
            # Why a rule left roots unmarked (a stop part-way through a statement).
            self.unsettable_reasons, roots = [], []
            for scope in listed:
                module = bool(is_global) and is_global(scope)
                if module and not wanted:
                    continue
                if module and isinstance(wanted, list):
                    proof = self.globals_proof(runtime, selected_frame, own, wanted)
                    if None not in (proof.get("names") or {}).values():
                        continue
                variables = self.dap.request(
                    "variables", {"variablesReference": scope["variablesReference"]}
                ).get("variables", [])
                if validate_children:
                    validate_children(variables)
                if (taken := self.take_returned(runtime, variables)) is not None:
                    # Shown apart, as `returned`, and only if it is the function's (#155).
                    variables, found = taken
                    if index == 0 and given is None:
                        given = found
                if not module:
                    own += [
                        v["name"]
                        for v in variables
                        if isinstance(v, dict)
                        and isinstance(v.get("name"), str)
                        and v["name"] not in groups
                    ]
                self.reference_counter += 1
                handle = self.reference_counter
                shown = variables[: min(50, 512 - self.inspection_nodes)]
                if names:
                    # Names a request asks for are listed even past the bound.
                    room = max(0, min(32, 512 - self.inspection_nodes - len(shown)))
                    shown += [
                        v
                        for v in variables[len(shown) :]
                        if isinstance(v, dict) and v.get("name") in names
                    ][:room]
                exposed = self.expose_variables(
                    shown, selected_frame, self.scope_identity(scope), ()
                )
                if not module:
                    roots += shown
                pending.append((handle, scope, exposed, len(variables)))
            references = [scope["variablesReference"] for _, scope, _, _ in pending]
            writable = quiescent and not self.view_timed_out
            if (self.view_fetches or self.preview_fetches) and writable:
                # Value views issued nested reads that may change adapter scope context.
                # Bind mutation authority only to freshly re-acquired, identical scopes.
                references = self.reacquire_scopes(runtime, selected_frame, pending)
            result = []
            self.global_handles = set()
            for (handle, scope, exposed, total), reference in zip(pending, references):
                entry = {"name": scope["name"], "reference": handle, "variables": exposed}
                if is_global and is_global(scope):
                    # The module's globals are read, never changed.
                    entry["globals"] = True
                    self.global_handles.add(handle)
                elif writable:
                    self.references.add(handle)
                    self.reference_frames[handle] = selected_frame
                    self.reference_adapter[handle] = reference
                if total > len(exposed):
                    # Cut at the bound (50 per scope, 512 per inspection): never silently.
                    entry.update(truncated=True, total=total)
                result.append(entry)
            source = (selected_frame.get("source") or {}).get("path")
            snapshot = {
                "stop_id": self.stop_id,
                # The user's own source file, as `frames` show it.
                **(
                    {"location": f"{_display_path(runtime, source)}:{selected_frame.get('line')}"}
                    if isinstance(source, str) and source
                    else {}
                ),
                "function": str(selected_frame.get("name") or "")[:300],
                # A generic's instance, with its type arguments (Rust: `tf<rc::A>`), where
                # only a symbol hash tells instances apart.
                **(
                    {"instance": instance}
                    if (named := getattr(runtime, "instance_name", None))
                    and (instance := named(selected_frame))
                    else {}
                ),
                # Which frame was read: an index into `frames` (0 is the innermost).
                "frame": index,
                # Which stop at the session's breakpoint this is, as state lines say.
                **({"hit": self.stop_hit} if self.stop_hit else {}),
                # Which of the line's breakpoint locations in its function (#157).
                **(
                    {"place": self.stop_place[0], "places": self.stop_place[1]}
                    if self.stop_place
                    else {}
                ),
                "frames": _public_sources(runtime, frames),
                "scopes": result,
                # The object whose members selectors may name without it (C++, Java).
                **(
                    {"receiver": receiver}
                    if (receiver := getattr(runtime, "implicit_receiver", None))
                    else {}
                ),
                # What the function keeps in static storage, which lldb-dap does not list
                # among a frame's locals (C, C++).
                **(
                    {"statics": statics}
                    if (lister := getattr(runtime, "static_locals", None))
                    and (statics := lister(selected_frame))
                    else {}
                ),
                **self.pause_status(),
                **({"optimized": level} if (level := self.optimized()) else {}),
                # C++ string keys: whether LLDB's summary of a std::string is all of it.
                **(
                    {"whole_string_summaries": True}
                    if getattr(runtime, "whole_string_summaries", False) is True
                    else {}
                ),
            }
            if index == 0 and (returned := self.returned_value(runtime, frames, given)):
                snapshot["returned"] = returned
            if args.get("depth"):
                # How many frames the thread has: a probe tells a recursive call from
                # the same call's next stop by it (#155).
                snapshot["depth"] = self.stack_depth()
                if index == 0 and (began := self.same_call(frames)):
                    # This stop is in the call a step out that began at `began` was
                    # leaving, proven by that step's watch (#174).
                    snapshot["same_call_as"] = began
            if index:
                # Where the program is stopped, which a caller frame's location is not.
                stopped = (frames[0].get("source") or {}).get("path")
                snapshot["innermost"] = {
                    **(
                        {"location": f"{_display_path(runtime, stopped)}:{frames[0].get('line')}"}
                        if isinstance(stopped, str) and stopped
                        else {}
                    ),
                    "function": str(frames[0].get("name") or "")[:300],
                }
            if available:
                # A name missing from the locals may be a module global (`globals`).
                snapshot["globals_available"] = True
            if index == 0:
                # Kept for a session step's stop in this function at this depth (#262).
                self.stop_names.update(own)
                before = self.names_before
                gone = sorted(set(before["names"]) - set(own)) if before else []
                if gone and not isinstance(self.adapter, PythonAdapter):
                    # Listed at an earlier stop in this function, at this depth: this stop
                    # is outside each one's scope (Python has no block scopes; its own reason
                    # says why). Java's steps also prove it is the same call.
                    snapshot["no_longer_visible"] = {
                        "names": gone[:50],
                        "line": before["line"],
                        **(
                            {"same_call": True}
                            if getattr(runtime, "steps_end_in_their_call", False)
                            else {}
                        ),
                    }
            if names:
                # Searched in every listed scope, past the bound too.
                snapshot["names"] = sorted(names)
            if wanted:
                snapshot["globals"] = (
                    "listed"
                    if any("globals" in entry for entry in result)
                    else "not listed: no requested name is one the function reads from them"
                    if proof is not None
                    else "not listed: at module level the locals are the module's globals"
                    if is_global and selected_frame.get("name") == "<module>"
                    else "not listed: this frame has no module globals to list"
                )
            if proof is not None:
                snapshot["globals_proof"] = proof
            if not writable:
                snapshot["mutation_blocked"] = "A value-view read is still pending; inspect again"
            elif (
                index == 0
                # A whole listing (`session inspect`), not a probe's or `get`'s names.
                and not args.get("names")
                and (found := self.unsettable_here(runtime, selected_frame, roots, result))
            ):
                snapshot["unsettable"], everywhere = found
                if everywhere:
                    # No stop of this run allows a change: no advice to stop elsewhere.
                    snapshot["unsettable_everywhere"] = True
            return snapshot
        if operation == "expand":
            return self.expand(args, runtime)
        if operation == "preview":
            return self.preview(args, runtime)
        if operation == "set_variable":
            self.stopped(args)
            if self.capabilities.get("supports_text_set") is False and args["value"].startswith(
                '"'
            ):
                raise ValueError(
                    "This Java adapter bundle predates String changes (text in double quotes); "
                    "rebuild it with scripts/setup_java_adapter.py"
                )
            if "handle" in args:
                return self.set_member(args, runtime)
            self.children.clear()
            self.taints.clear()
            self.notes.clear()
            self.marks.clear()
            if not self.capabilities.get("supports_set_variable", False):
                raise ValueError("Adapter does not support variable mutation")
            ref = args["reference"]
            if ref in self.global_handles:
                raise ValueError(
                    "Module globals cannot be changed: only the stopped function's own locals can"
                )
            if ref not in self.references:
                raise ValueError("Inspect the scope before changing variables")
            if (self.reference_frames.get(ref) or {}).get("id") != self.innermost_frame:
                raise ValueError(_CALLER_SET.format(name=repr(args["name"])))
            adapter_ref = self.reference_adapter[ref]
            # Text in double quotes (#137): C, C++ and Rust strings are written in place.
            text = parse_text(args["value"]) if getattr(runtime, "text_language", None) else None
            if text is None:
                runtime.validate_value(args["value"])
            current = self.dap.request("variables", {"variablesReference": adapter_ref}).get(
                "variables", []
            )
            matches = [v for v in current if v.get("name") == args["name"]]
            for group in current if not matches else ():
                # debugpy files a local whose value is a function or class under a group;
                # a literal never is one, so the changed local is listed directly after.
                reference = group.get("variablesReference")
                if (
                    group.get("name") in getattr(runtime, "value_groups", ())
                    and not group.get("type")
                    and type(reference) is int
                    and reference > 0
                ):
                    matches += [
                        v
                        for v in self.dap.request(
                            "variables", {"variablesReference": reference}
                        ).get("variables", [])
                        if v.get("name") == args["name"]
                    ]
            if len(matches) != 1:
                raise ValueError("Expected exactly one existing local variable")
            variable = matches[0]
            if level := self.optimized():
                raise ValueError(
                    f"Changing locals is refused in an optimized build ({level}): the program "
                    "may keep using other copies of the value. Use an unoptimized build"
                )
            unlocated = getattr(runtime, "unlocated", None)
            if unlocated and (why := unlocated(variable, self.reference_frames.get(ref))):
                raise ValueError(f"{args['name']!r} cannot be changed: {why}")
            if self.placeholders() and is_unavailable(variable.get("value")):
                raise ValueError(
                    f"{args['name']!r} has no readable value here "
                    f"({str(variable.get('value'))[1:-1][:200]}); it cannot be changed"
                )
            if not self.has_memory_location(variable):
                raise ValueError(
                    f"{args['name']!r} has no memory location here (a computed value, such "
                    "as a bitfield binding): a change would not reach the program"
                )
            if text is not None:
                kind, shape = self.text_storage(runtime, variable, self.reference_frames.get(ref))
            elif validate_variable := getattr(runtime, "validate_variable", None):
                validate_variable(variable, args["value"])
            if validate_target := getattr(runtime, "validate_mutation_target", None):
                validate_target(variable, self.reference_frames.get(ref), **self.since_launch())
            unread_note = None
            if read_after := getattr(runtime, "read_after", None):
                # Java (#164): a local the code never reads again is never changed.
                verdict = read_after(variable, self.reference_frames.get(ref), self.stop_reason)
                if verdict is False:
                    raise ValueError(_UNREAD.format(name=repr(args["name"])))
                if verdict is None:
                    unread_note = _UNREAD_UNKNOWN
            if text is not None and (check := getattr(runtime, "validate_text_target", None)):
                check(variable, self.reference_frames.get(ref), self.text_stop())
            if validate_debugger := getattr(runtime, "validate_debugger", None):
                frame = self.reference_frames.get(ref)
                if frame is None:
                    raise ValueError("Inspect the scope before changing variables")
                runtime.validate_stop(frame)
                validate_debugger(self.dap, frame)
            self.refuse_mid_statement(
                runtime, variable, self.reference_frames.get(ref), args["name"]
            )
            if text is not None:
                return self.set_text(runtime, variable, adapter_ref, kind, text, shape)
            before = variable.get("value")
            literal = getattr(runtime, "mutation_literal", None)
            try:
                changed = self.dap.request(
                    "setVariable",
                    {
                        "variablesReference": adapter_ref,
                        "name": args["name"],
                        "value": literal(args["value"], variable=variable)
                        if literal
                        else args["value"],
                    },
                )
            except RequestFailed as exc:
                # A refused write changed nothing (e.g. LLDB's "Cannot change the value of
                # a constant" for a computed value): the session stays usable.
                raise ValueError(
                    f"The debugger refused to change {args['name']!r} ({str(exc)[:200]}); "
                    "it is unchanged. Edit the source and run verify instead"
                ) from None
            runtime.assert_current()
            normalize_value = getattr(runtime, "mutation_value", None)
            after = normalize_value(changed) if normalize_value else changed.get("value")
            if confirm := getattr(runtime, "confirm_mutation", None):
                confirm(args["name"], args["value"], before, after, variable=variable)
            return {
                "name": args["name"],
                "before": before,
                "after": after,
                **({"note": unread_note} if unread_note else {}),
            }
        if operation == "breakpoints":
            runtime.validate_breakpoints(args["breakpoints"])
            self.breakpoint_counts[str(self.path(args["program"]))] = len(args["breakpoints"])
            if check_lines := getattr(runtime, "check_breakpoint_lines", None):
                check_lines(self.path(args["program"]), args["breakpoints"])
            placed = self.dap.request(
                "setBreakpoints",
                {
                    "source": {"path": str(runtime.source_path(self.path(args["program"])))},
                    "breakpoints": args["breakpoints"],
                },
            )
            self.note_placed(
                runtime.source_path(self.path(args["program"])), args["breakpoints"], placed
            )
            if self.path(args["program"]) == self.program:
                self.points, self.temporary_line = list(args["breakpoints"]), None
                self.record_own_points(args["breakpoints"], placed)
                self.note_places(runtime, args["breakpoints"], placed)
                if warnings := [*self.loop_warnings(), *self.place_warnings()]:
                    return {**_public_placed(runtime, placed), "warnings": warnings}
            return _public_placed(runtime, placed)
        if operation == "continue":
            self.stopped(args)
            # A continue's stop is never compared with this one (#262): the program may
            # have left this call and made another that looks the same.
            self.calls_from = None
            pending = None if args.get("to_exit") else self.co_stopped()
            if pending is not None:
                # Another thread stopped at a breakpoint in this same all-threads stop:
                # it is a real hit. Serve it as the next stop before resuming anything,
                # because resuming would step it over the breakpoint unreported.
                self.cursor = pending["cursor"] - 1
                self.forget_stop()
                # Those threads keep their old stops in the debugger until a real resume.
                self.co_served = True
                return {"state": self.state}
            if args.get("to_exit"):
                # Checked and applied under one lock: clear the launch breakpoints only
                # while this exact stop is still current.
                self.dap.request(
                    "setBreakpoints",
                    {"source": {"path": str(runtime.source_path(self.program))}, "breakpoints": []},
                )
                self.note_placed(runtime.source_path(self.program), [], {})
                self.forget_places()
                self.breakpoint_counts[str(self.program)] = 0
                self.points, self.temporary_line = [], None
            elif args.get("to_line") is not None:
                self.set_points(
                    runtime,
                    [*self.points, {"line": args["to_line"]}],
                    temporary=args["to_line"],
                )
            cursor = getattr(self.dap, "event_cursor", None)
            consumed = cursor() if cursor else self.cursor
            resumed = self.dap.request("continue", {"threadId": self.thread_id})
            self.co_served = False
            if cursor and resumed.get("allThreadsContinued", True) is not False:
                # Every thread runs again, so stops reported before this continue (lldb
                # sends one per thread that hit the breakpoint) describe nothing current.
                for update in self.dap.events_between(self.cursor, consumed, {"breakpoint"}):
                    self.breakpoint_events.append(update.get("body", {}))
                self.breakpoint_events = self.breakpoint_events[-100:]
                self.cursor = max(self.cursor, consumed)
            self.mark_resumed()
            self.forget_stop()
            return {"state": self.state}
        if operation == "step":
            self.stopped(args)
            if self.capabilities.get("supports_stepping") is False:
                raise ValueError(
                    "Stepping is not supported by this debugger adapter (a Java adapter bundle "
                    "built before stepping: rebuild it with scripts/setup_java_adapter.py); use "
                    "`session continue --to LINE` to stop at another line"
                )
            if self.co_stopped() is not None or self.co_served or self.pending_stop_elsewhere():
                # LLDB re-reports the other threads' old stops when a step ends; java-debug
                # suspends each thread separately, and its pending stop must be served.
                raise ValueError(
                    "Other threads stopped at this breakpoint too and are still paused there; "
                    "`session continue` serves and resumes them before stepping"
                )
            kind = args.get("kind", "over")
            # A session's own step (`calls`): its stop is compared with this one (#262).
            self.calls_from = None
            if args.get("calls"):
                self.mark_calls()
            cursor = getattr(self.dap, "event_cursor", None)
            consumed = cursor() if cursor else self.cursor
            # Only where the debugger lists returns (not debugpy): one request per step.
            origin = self.call_origin() if getattr(runtime, "return_entry", None) else None
            listed = origin.pop("listed") if origin else []
            if origin:
                origin["kind"] = kind
                # Where it began: a stop that interrupts it may be proven in the same call.
                origin["stop_id"] = self.stop_id
            if before_step := getattr(runtime, "before_step", None):
                # LLDB: a step out leaves one frame, not the library frames above (#155).
                before_step(self.dap)
            watch = None
            if (
                origin
                and kind == "out"
                and (watch_return := getattr(runtime, "watch_return", None))
                # An optimized build's returns are withheld anyway: nothing to watch for.
                and not self.optimized()
            ):
                watch = watch_return(self.dap, self.thread_id, listed)
                if watch.get("inlined") or "unwatched" in watch:
                    origin.update(watch)
                    watch = None
            try:
                self.dap.request(STEPS[kind], {"threadId": self.thread_id})
            except Exception as exc:
                if watch is not None:
                    # No step runs that it could watch.
                    runtime.unwatch_return(self.dap, watch)
                if not isinstance(exc, RequestFailed):
                    raise
                # Refused (e.g. stepping out of the outermost frame): nothing moved.
                raise ValueError(
                    f"The debugger refused to step {kind} here ({exc}); `session continue` instead"
                ) from None
            if self.stop_all_threads:
                # Every thread was stopped together and is resumed now: stops reported
                # before this step describe nothing current. Otherwise (java-debug) each
                # earlier stop is a thread still suspended there, so none is skipped.
                self.cursor = max(self.cursor, consumed)
            # Until this thread reports a stop, it may sit suspended anywhere the step
            # ends, a breakpoint line included: never a lost-stop candidate (#27).
            self.stepping.add(self.thread_id)
            self.mark_resumed()
            self.forget_stop()
            self.step_origin, self.return_watch = origin, watch
            return {"state": self.state}
        if operation == "report":
            return self.report(runtime)
        if operation == "close":
            cleanup = self.close()
            return {"state": self.state, **({"cleanup": cleanup} if cleanup else {})}
        raise ValueError("Unknown operation")

    def report(self, runtime):
        output, truncated = self.dap.snapshot_output()
        diagnostics = getattr(self.dap, "snapshot_diagnostics", None)
        lines, count = diagnostics() if diagnostics else ([], 0)
        return {
            "session_id": self.session,
            "state": self.state,
            "output": output,
            "output_truncated": truncated,
            # The debugger's own lines, left out of the output (#167).
            **({"debugger_diagnostics": lines, "diagnostic_count": count} if count else {}),
            **(
                {"exit_code": code}
                if type(code := getattr(self.dap, "exit_code", None)) is int
                else {}
            ),
            "history": [e for e in self.audit if e["operation"] != "report"],
            "artifact_manifest_id": runtime.manifest_id,
            "breakpoint_events": list(self.breakpoint_events),
            "debugger_events": self.dap.recent_events(),
        }

    @staticmethod
    def scope_identity(scope):
        return (scope.get("name"), scope.get("presentationHint"))

    def since_launch(self):
        """{"loaded_by": when the program started} for a runtime whose rules read its
        sources (Python): one changed since may not be the code running."""
        if getattr(self.runtime, "judges_sources", False):
            return {"loaded_by": self.launched_at}
        return {}

    def globals_proof(self, runtime, frame, own, names):
        """How the frame's function sees each of ``names`` missing from its locals (see
        PythonAdapter.globals_proof), computed only when asked."""
        if not _inside_program(runtime, frame, self.root):
            return {"unproven": "its source is outside the target root"}
        return runtime.globals_proof(
            frame,
            [name for name in own if name.isidentifier()],
            names,
            loaded_by=self.launched_at,
        )

    @staticmethod
    def frame_scopes(runtime, frame, scopes):
        """The scopes an inspection of ``frame`` lists, at most 16."""
        listed = runtime.local_scopes(scopes)
        is_global = getattr(runtime, "global_scope", None)
        if is_global and frame.get("name") == "<module>":
            # Module code runs with its globals as its locals: they are listed once.
            listed = [scope for scope in listed if not is_global(scope)]
        return listed[:16]

    def preview_stack(self):
        """The stop for a preview's rule on standard containers (#181): its frames as
        listed, a way to read more, and how many threads the program has."""
        return {
            "stack": getattr(self, "listed_frames", None),
            "deeper": self.whole_stack,
            "threads": self.threads_at_stop,
        }

    def whole_stack(self, levels):
        """The stop's first ``levels`` frames (a ``StackPage``), fetched once per
        inspection, for the rule on standard containers a library call may be changing
        (#181, #187); none, and not the whole stack, if the debugger refuses."""
        cached = getattr(self, "whole_frames", None)
        if cached is None or cached[0] < levels:
            try:
                reply = self.dap.request(
                    "stackTrace", {"threadId": self.thread_id, "levels": levels}, timeout=10
                )
                frames = StackPage(
                    reply.get("stackFrames", [])[:levels], levels, reply.get("totalFrames")
                )
            except (TimeoutError, RuntimeError, OSError):
                frames = StackPage([], levels, None, failed=True)
            self.whole_frames = cached = (levels, frames)
        return cached[1]

    def threads_at_stop(self):
        """How many threads the program has at this stop, read once per inspection, or
        None where that is not sure (#181 review). The threads every source names: two
        `threads` replies (lldb-dap answers its first with the threads the program had
        at launch), the `thread` events that said a thread started since launch, and
        the runtime's own list where it has one (LLDB's `thread list`); a source that
        does not answer makes it unsure."""
        if getattr(self, "thread_count", None) is None:
            ids, sure = set(), True
            for _ in range(2):
                try:
                    listed = self.dap.request("threads", {}, timeout=2).get("threads")
                except (TimeoutError, RuntimeError, OSError):
                    listed = None
                if not isinstance(listed, list):
                    sure = False
                    continue
                ids |= {each.get("id") for each in listed if isinstance(each, dict)}
            ids |= set(getattr(self.dap, "started_threads", None) or ())
            if listing := getattr(self.runtime, "listed_threads", None):
                found = listing(self.dap)
                if found is None:
                    sure = False
                else:
                    ids |= set(found)
            self.thread_count = len(ids) if sure and ids and None not in ids else -1
        return None if self.thread_count < 0 else self.thread_count

    def deeper_stack(self, levels=64):
        """The stop's first ``levels`` frames, fetched once per inspection for a rule that
        cannot decide from the ten listed; [] if the debugger refuses."""
        if getattr(self, "deeper_frames", None) is None:
            try:
                reply = self.dap.request(
                    "stackTrace", {"threadId": self.thread_id, "levels": levels}
                )
                self.deeper_frames = list(reply.get("stackFrames", [])[:levels])
            except (TimeoutError, RuntimeError, OSError):
                self.deeper_frames = []
        return self.deeper_frames

    @staticmethod
    def frame_identity(frame):
        return (
            frame.get("id"),
            frame.get("name"),
            frame.get("source", {}).get("path"),
            frame.get("line"),
            frame.get("moduleId"),
            frame.get("instructionPointerReference"),
        )

    def reacquire_scopes(self, runtime, frame, pending):
        frames = self.dap.request("stackTrace", {"threadId": self.thread_id, "levels": 10}).get(
            "stackFrames", []
        )[:10]
        matches = [f for f in frames if f.get("id") == frame.get("id")]
        if len(matches) != 1 or self.frame_identity(matches[0]) != self.frame_identity(frame):
            raise ValueError("Frame changed during value views; inspect again")
        runtime.validate_stop(matches[0])
        if validate_debugger := getattr(runtime, "validate_debugger", None):
            validate_debugger(self.dap, matches[0])
        fresh = self.frame_scopes(
            runtime, frame, self.dap.request("scopes", {"frameId": frame["id"]}).get("scopes", [])
        )
        original = [self.scope_identity(scope) for _, scope, _, _ in pending]
        if len(set(original)) != len(original):
            raise ValueError("Duplicate scope identities after value views; inspect again")
        if sorted(map(repr, (self.scope_identity(s) for s in fresh))) != sorted(
            map(repr, original)
        ) or len(fresh) != len(original):
            raise ValueError("Scopes changed during value views; inspect again")
        references = {self.scope_identity(s): s["variablesReference"] for s in fresh}
        return [references[identity] for identity in original]

    def reset_views(self):
        """A fresh value-view budget, for one inspection or expansion."""
        self.view_fetches = self.view_items = self.view_bytes = 0
        self.view_waited = 0.0
        self.view_deadline = time.monotonic() + VIEW_SECONDS

    def view_exhausted(self, why=None):
        """The budget's error, saying which limit ran out (#156 review: Peeky's own checks
        once took the whole time, and the rows blamed an enum's view)."""
        if why is None and self.view_fetches >= 128:
            why = "128 debugger reads"
        elif why is None:
            why = (
                f"its {VIEW_SECONDS} s ran out, {self.view_waited:.1f} s of them waiting for "
                "the debugger's reads, the rest listing values and in Peeky's own checks"
            )
        return ViewBudgetExhausted(f"Value view budget exhausted: {why}")

    def view_request(self, command, arguments, remaining):
        """One debugger request of a value view, its wait counted (view_exhausted)."""
        started = time.monotonic()
        try:
            return self.dap.request(command, arguments, timeout=remaining)
        finally:
            self.view_waited += time.monotonic() - started

    def raw_value(self, frame, path):
        """The debugger's text for the variable path ``path`` in ``frame`` (#167: an
        unordered container's own count, `us._M_h._M_element_count`), asked as a hover:
        lldb-dap then reads the path's variables and members as they are, past any
        formatter, and never evaluates it as an expression, so nothing runs in the
        program. Cached for the stop; counted against the value-view budget."""
        cache = getattr(self, "raw_values", None)
        if cache is None or cache[0] != self.stop_id:
            cache = self.raw_values = (self.stop_id, {})
        key = (frame.get("id"), path)
        if key in cache[1]:
            return cache[1][key]
        remaining = self.view_deadline - time.monotonic()
        if self.view_fetches >= 128 or remaining <= 0:
            raise self.view_exhausted()
        self.view_fetches += 1
        arguments = {"expression": path, "frameId": frame.get("id"), "context": "hover"}
        try:
            reply = self.view_request("evaluate", arguments, remaining)
        except ResponseTimeout as exc:
            self.view_timed_out = True
            raise self.view_exhausted("the debugger did not answer in the time left") from exc
        except RequestFailed as exc:
            raise ValueError(f"The debugger could not read {path}: {exc}") from None
        result = reply.get("result")
        if not isinstance(result, str):
            raise ValueError(f"The debugger gave no value for {path}")
        cache[1][key] = result[:2000]
        return cache[1][key]

    def view_fetch(self, reference):
        """Bounded raw reads used only by a runtime's trusted value-view hook.

        Count, time, item and byte budgets are shared by one inspection or expansion;
        exhaustion makes remaining views opaque instead of extending the request.
        """
        remaining = self.view_deadline - time.monotonic()
        if self.view_fetches >= 128 or remaining <= 0:
            raise self.view_exhausted()
        self.view_fetches += 1
        if type(reference) is not int or reference <= 0:
            raise ValueError("Variable is not expandable")
        try:
            values = self.view_request(
                "variables", {"variablesReference": reference, "start": 0, "count": 65}, remaining
            ).get("variables", [])
        except ResponseTimeout as exc:
            # The read was written completely; only its reply missed the view budget.
            self.view_timed_out = True
            raise self.view_exhausted("the debugger did not answer in the time left") from exc
        if not isinstance(values, list) or len(values) > 64:
            raise ValueError("Value view child collection exceeds limit")
        self.view_items += len(values)
        self.view_bytes += len(json.dumps(values, default=str))
        if self.view_items > 1024 or self.view_bytes > 1024 * 1024:
            raise self.view_exhausted("1,024 values or 1 MiB read")
        return values

    def reset_previews(self):
        self.preview_fetches = 0
        self.preview_deadline = time.monotonic() + PREVIEW_SECONDS

    def preview_fetch(self, reference, count):
        """The first ``count`` children of a raw debugger value, for a runtime's preview
        (#136). Bounded per inspection or expansion (PREVIEW_FETCHES requests within
        PREVIEW_SECONDS), apart from the value-view budget; exhaustion leaves the
        remaining values as the debugger shows them."""
        remaining = self.preview_deadline - time.monotonic()
        if self.preview_fetches >= PREVIEW_FETCHES or remaining <= 0:
            raise ViewBudgetExhausted("Preview budget exhausted")
        self.preview_fetches += 1
        if type(reference) is not int or reference <= 0:
            raise ValueError("Variable is not expandable")
        if type(count) is not int or not 0 < count <= 64:
            raise ValueError("Preview read exceeds limit")
        try:
            values = self.dap.request(
                "variables",
                {"variablesReference": reference, "start": 0, "count": count},
                timeout=remaining,
            ).get("variables", [])
        except ResponseTimeout as exc:
            # As for a view: until its reply drains, nothing is changed.
            self.view_timed_out = True
            raise ViewBudgetExhausted("Preview budget exhausted") from exc
        if not isinstance(values, list) or len(values) > count:
            raise ValueError("Preview child collection exceeds limit")
        return [value for value in values if isinstance(value, dict)]

    def view_read(self, reference, count):
        """Bounded memory reads used only by a runtime's trusted value-view hook; shares
        the inspection's view budgets with view_fetch."""
        remaining = self.view_deadline - time.monotonic()
        if self.view_fetches >= 128 or remaining <= 0:
            raise self.view_exhausted()
        self.view_fetches += 1
        if not self.capabilities.get("supports_read_memory"):
            raise ValueError("The debugger cannot read memory")
        if not isinstance(reference, str) or not re.fullmatch(r"0x[0-9A-Fa-f]{1,16}", reference):
            raise ValueError("Invalid memory reference")
        if type(count) is not int or not 0 < count <= 4096:
            raise ValueError("Memory read exceeds limit")
        if self.view_bytes + count > 1024 * 1024:
            raise self.view_exhausted("1 MiB read")
        try:
            reply = self.view_request(
                "readMemory", {"memoryReference": reference, "count": count}, remaining
            )
        except ResponseTimeout as exc:
            self.view_timed_out = True
            raise self.view_exhausted("the debugger did not answer in the time left") from exc
        except RequestFailed as exc:
            raise ValueError(f"The debugger refused the memory read: {exc}") from None
        try:
            data = base64.b64decode(reply.get("data") or "", validate=True)
        except ValueError:
            raise ValueError("Malformed memory read reply") from None
        if len(data) != count or reply.get("unreadableBytes"):
            raise ValueError("Partial memory read")
        self.view_bytes += count
        return data

    def expose_variables(
        self, variables, frame, scope, ancestry, taint=None, root=None, noted=None, trail=()
    ):
        """Only worker-issued handles leave the process. A child handle permits a write only
        to a member the runtime allows (Java fields and array elements), checked again
        when the write is made. ``taint`` is the parent's (see ``self.taints``). ``root``:
        the debugger's variable for the root local of ``ancestry``, whose fields and
        elements are marked settable where `set` would change them (C, C++, Rust).
        ``noted``: the parent's (note, refuses) (see ``self.notes``): every child shows
        that note, and none is settable where it refuses (#148). ``trail``: the
        debugger's variable for each step of ``ancestry`` (Java: what holds an array)."""
        result = []
        # What holds an array whose elements this listing shows (#261), asked once here.
        listing = {}
        member_rule = (
            self.member_rule(self.runtime, frame, root) if ancestry and root is not None else None
        )
        view_variable = getattr(self.runtime, "view_variable", None)
        aggregate_taint = getattr(self.runtime, "aggregate_taint", None)
        display_value = getattr(self.runtime, "display_value", None)
        string_value = getattr(self.runtime, "string_value", None)
        preview = getattr(self.runtime, "preview_value", None)
        member_value = getattr(self.runtime, "member_value", None)
        changing_container = getattr(self.runtime, "changing_container", None)
        unlisted_container = getattr(self.runtime, "unlisted_container", None)
        identify = getattr(self.runtime, "object_identity", None)
        groups = getattr(self.runtime, "variable_groups", frozenset())
        # Python's module globals are no frame's own locals: none is judged as one.
        is_global = getattr(self.runtime, "global_scope", None)
        module = (
            bool(is_global)
            and isinstance(scope, tuple)
            and len(scope) == 2
            and is_global({"name": scope[0], "presentationHint": scope[1]})
        )
        for variable in variables:
            name, kind = variable.get("name"), variable.get("type")
            if not isinstance(name, str) or (kind is not None and not isinstance(kind, str)):
                raise RuntimeError("Malformed debugger variable identity")
            # Read through a standard container something may be part-way through
            # changing (an element `clear()` has destroyed, another thread's `push_back`,
            # #187): withheld, and so is everything below it, whatever its identity.
            changing = (
                changing_container(
                    frame, [step for step, _ in ancestry] + [name], **self.preview_stack()
                )
                if ancestry and changing_container
                else None
            )
            # An unordered container the debugger counts otherwise than its own count says
            # (GCC's, read as empty, #167): withheld, its entries never listed.
            unlisted = (
                unlisted_container(
                    variable,
                    frame,
                    [*ancestry, (name, kind)],
                    lambda path: self.raw_value(frame, path),
                )
                if unlisted_container
                else None
            )
            changing = changing or unlisted
            if len(name) > 2000 or (kind is not None and len(kind) > 2000):
                if not ancestry:
                    raise RuntimeError("Debugger variable identity exceeds limit")
                # A child (a dict entry keyed by a long string, a long C++ type) is shown
                # without its full identity: never expandable or selectable, and the
                # session survives.
                shown = {
                    "name": name if len(name) <= 2000 else f"<{len(name)}-character name>",
                    "value": str(variable.get("value", ""))[:2000],
                    "variablesReference": 0,
                    **({"type": kind if len(kind) <= 2000 else kind[:200] + "…"} if kind else {}),
                    "identity_truncated": True,
                }
                if changing:
                    shown.update(value=None, unavailable=True, reason=changing[:300])
                    shown.update(withheld=True, settable=False)
                    if isinstance(label := getattr(changing, "label", None), str):
                        shown["withheld_as"] = label[:100]
                elif view_variable:
                    # Values that need a qualified view (Rust enums) are never shown raw.
                    shown["value"] = "<not shown: type name too long to verify>"
                elif self.placeholders() and is_unavailable(shown["value"]):
                    shown.update(value=None, unavailable=True, reason=shown["value"][1:-1][:300])
                elif noted:
                    shown["note"] = noted[0][:500]
                result.append(shown)
                self.inspection_nodes += 1
                continue
            pending = (
                getattr(self.runtime, "not_yet_declared", None)
                if not ancestry and not module
                else None
            )
            reason = (
                pending(
                    variable,
                    frame,
                    innermost=frame.get("id") == self.innermost_frame,
                    stack=getattr(self, "listed_frames", None),
                    deeper=self.deeper_stack,
                    # The scope's other variables: rows a debugger shows under one name.
                    siblings=variables,
                    stop=self.stop_reason,
                    **self.since_launch(),
                )
                if pending
                else None
            )
            # The plain name a selector reads this local by, where the debugger names it
            # with where it is declared (`t @ two.c:20`, #158).
            alias = getattr(self.runtime, "selected_as", None) if not ancestry else None
            plain = alias(variable, frame, variables) if alias else None
            # Where a local the debugger lists by its plain name is declared, where its
            # function declares several of that name (`entry @ main.rs:81`, #206): the
            # selector `describe` lists it by reads it too.
            located = getattr(self.runtime, "declared_at", None) if not ancestry else None
            declared_at, sharing = (located(variable, frame) if located else None) or (None, 0)
            # A value shown though the runtime cannot vouch for it: a note on its row, and
            # on everything read through it (#148), which says through what.
            note = getattr(reason, "note", None)
            carried = None
            if isinstance(note, str):
                refuses = bool(getattr(reason, "refuses", False))
                carried = (f"read through `{name}`: {note}"[:500], refuses)
            elif noted:
                note, carried = noted[0], noted
            if changing and not reason:
                reason, note, carried = changing, None, None
            elif unlisted and note is not None:
                # Its count is wrong, so no note makes its listing true.
                reason, note, carried = unlisted, None, None
            # A structured binding the build holds as a reference to its element (GCC's,
            # of a pair or tuple, #291), which the debugger shows as the element's
            # address: the element is shown instead, or the row withheld.
            bound = (
                getattr(self.runtime, "bound_element", None)
                if not ancestry and not module and (not reason or note is not None)
                else None
            )
            element = (
                bound(variable, frame, self.preview_fetch, note=note)
                if bound and not (self.placeholders() and is_unavailable(variable.get("value")))
                else None
            )
            reference_kind = None
            if isinstance(element, str):
                reason, note, carried = element, None, None
            elif element is not None and isinstance(element.get("variable"), dict):
                # The element itself, as the debugger lists it under the reference.
                reference_kind, variable = kind, element["variable"]
                kind = variable.get("type") if isinstance(variable.get("type"), str) else kind
            if reason and note is None:
                # Indeterminate here: never shown as a value, expanded or offered for set,
                # nor read through by a qualified view (a Rust container, enum or Box).
                output = {
                    "name": name,
                    "value": None,
                    "variablesReference": 0,
                    "unavailable": True,
                    "reason": reason[:300],
                    # Peeky's own reason, not the debugger's.
                    "withheld": True,
                    "settable": False,
                }
                if isinstance(label := getattr(reason, "label", None), str):
                    # Withheld for another reason than a value not set yet.
                    output["withheld_as"] = label[:100]
                if plain:
                    output["selected_as"] = plain
                if declared_at:
                    output["declared_at"] = declared_at
                    if sharing > 1:
                        # Bindings on one line it cannot tell apart (#215 review).
                        output["declared_at_shared"] = sharing
                if kind is not None:
                    output["type"] = kind
                    shown = getattr(self.runtime, "display_type", None)
                    if shown and (rust := shown(variable, frame, top=True)):
                        output.update(type=rust, debugger_type=kind)
                result.append(output)
                self.inspection_nodes += 1
                continue
            view = (
                view_variable(
                    variable,
                    self.view_fetch,
                    self.view_read,
                    frame=frame,
                    top=not ancestry,
                    innermost=frame.get("id") == self.innermost_frame,
                    stop=self.stop_reason,
                    # The scope's other variables: rows a debugger shows under one name.
                    siblings=None if ancestry else variables,
                    **({"taint": taint} if taint else {}),
                    # A field's way from its root local (#156).
                    **({"ancestry": ancestry} if ancestry else {}),
                )
                if view_variable
                else None
            )
            raw_reason, tainted = None, None
            if view is not None and "rust_view" not in view:
                # Left as the debugger shows it, with why (a Rust Vec or String, #156), and
                # how much of what it holds reads (a Vec of references, #205).
                raw_reason, tainted = view.get("raw_reason"), view.get("taint")
                if view.get("carry") and isinstance(raw_reason, str) and not carried:
                    # What a call may be changing says so on each value read through it
                    # (#260), as a value shown with a note does (#148).
                    carried = (f"read through `{name}`: {raw_reason}"[:500], True)
                view = None
            if view is not None:
                # Interpreted values never carry expansion handles to raw storage. Their
                # decoded elements get a worker-owned handle that never reaches the
                # debugger; only a struct or tuple preview's (view_writable) can lead to a
                # change of a field, judged again from the debugger's own variable.
                handle = 0
                if view.get("children"):
                    self.reference_counter += 1
                    handle = self.reference_counter
                    # A preview of the value's own fields (#106) also keeps where the
                    # debugger lists it, which a change to a field walks again (#105).
                    writable = getattr(self.runtime, "view_writable", None)
                    raw = variable.get("variablesReference", 0)
                    source = (
                        (dict(frame), scope, (*ancestry, (name, kind)))
                        if writable and writable(view) and type(raw) is int and raw > 0
                        else None
                    )
                    self.children[handle] = ("view", list(view["children"][:512]), source)
                    if carried:
                        self.notes[handle] = carried
                output = {
                    "name": name,
                    "value": str(view["value"])[:2000],
                    "variablesReference": handle,
                    "view": str(view["rust_view"])[:100],
                }
                if type(view.get("length")) is int:
                    output["length"] = view["length"]
                if view["rust_view"] == "unproven" and isinstance(view.get("why"), str):
                    # Why only the pointer shows, where the runtime says (#205).
                    output["unproven_reason"] = view["why"][:500]
                if view["rust_view"] == "opaque" and isinstance(view.get("why"), str):
                    # Why a Rust enum is not decoded, where it is stored and what to read
                    # instead (#303).
                    output["opaque_reason"] = view["why"][:500]
                if view["rust_view"] == "unproven" and view.get("pointer") in ("raw", "unknown"):
                    # A Rust raw pointer, or one the build does not tell from one (#255).
                    output["unproven_pointer"] = view["pointer"]
                if taint == "stale" and view["rust_view"] != "unproven":
                    # A decoded enum inside a moved-from value: its stack bytes (#108).
                    output["stale"] = True
                if kind is not None:
                    output["type"] = kind
                shown = getattr(self.runtime, "display_type", None)
                if shown and (rust := shown(variable, frame, **self.typed_at(ancestry, name))):
                    # A reference parameter's Rust type (`&mut Window`), as for plain rows.
                    output.update(type=rust, debugger_type=kind)
                if isinstance(note, str):
                    output["note"] = note[:500]
                if not ancestry and (marked := self.view_settable(variable, frame)) is not None:
                    # A Rust String's text (#137), shown as read from memory.
                    output["settable"] = marked and reachable({"name": name, "selected_as": plain})
                if plain:
                    output["selected_as"] = plain
                if declared_at:
                    output["declared_at"] = declared_at
                    if sharing > 1:
                        # Bindings on one line it cannot tell apart (#215 review).
                        output["declared_at_shared"] = sharing
                result.append(output)
                self.inspection_nodes += 1
                continue
            settable = never = maybe = None
            unread = False
            # Only the stopped frame's values change: a caller's are read-only.
            innermost = frame.get("id") == self.innermost_frame
            if not ancestry and (check := getattr(self.runtime, "settable", None)):
                # Root locals `set` can change, so agents need not try and be refused;
                # optimized builds refuse every change.
                mid = getattr(self.runtime, "mid_statement", None)
                settable = (
                    self.capabilities.get("supports_set_variable", False) is True
                    and innermost
                    and not self.optimized()
                    # An element read through a reference (#291) is never changed.
                    and reference_kind is None
                    and self.has_memory_location(variable)
                    and (bool(check(variable, frame)) or self.text_settable(variable, frame))
                    # Marked only where a selector reaches it (#195): `t` for LLDB's
                    # `t @ my file.c:3`, never a row no selector names.
                    and reachable({"name": name, "selected_as": plain})
                )
                # Not a value shown with a note whose declaration still runs here (#140),
                # nor one a statement under way may hold a copy of (#105); why, for
                # inspect's note (sixth evaluation, finding 8).
                if settable and getattr(reason, "refuses", False):
                    settable = False
                    self.unsettable_reasons.append((name, str(reason)))
                elif settable and mid and (why := mid(variable, frame, self.stop_reason)):
                    settable = False
                    self.unsettable_reasons.append((name, why))
                elif (
                    settable
                    and (read_after := getattr(self.runtime, "read_after", None))
                    and read_after(variable, frame, self.stop_reason) is False
                ):
                    settable, unread = False, True
                    self.unsettable_reasons.append((name, _UNREAD_WHY))
            elif ancestry and member_rule is not None:
                # A field or element `set` would change (#105), judged as `set` judges it
                # (sixth evaluation, finding 7); never one reached through a pointer.
                steps = [step for step, _ in ancestry[1:]] + [name]
                settable = member_rule(variable, steps)
            elif ancestry and (member := getattr(self.runtime, "member_settable", None)):
                # A JDK class's own state (#204), never changed: why, on the row too.
                internal = getattr(self.runtime, "internal_member", None)
                jdk = internal(variable, parent=ancestry[-1][1], trail=trail) if internal else None
                settable = (
                    not jdk
                    and innermost
                    and self.member_set()
                    and not self.optimized()
                    and member(variable, parent=ancestry[-1][1], proven=True) is None
                )
                never = getattr(jdk, "row", None)
                if settable and (held := self.array_held(trail, listing)):
                    # An array a JDK class's field holds, whatever path reaches it (#261):
                    # the write would be refused, so the row says why instead; or why
                    # that is not known, where the write may yet be made.
                    settable = False
                    if getattr(held, "unproven", False):
                        maybe = str(held)
                    else:
                        never = held
            if ancestry and settable and noted and noted[1]:
                # Inside a local whose change is refused (#140): so is a change to this.
                settable = False
            # A value no stop of this build can change (#138: a Rust local the IR keeps in
            # a register), and why, so no probe waits for a hit to learn it.
            if settable is False and (rule := getattr(self.runtime, "never_settable_row", None)):
                if not ancestry:
                    never = rule(variable, frame)
                elif root is not None:
                    steps = [step for step, _ in ancestry[1:]] + [name]
                    never = rule(root, frame, member=True, steps=steps)
            if (
                settable is False
                and not never
                and not maybe
                and (why_not := getattr(self.runtime, "unsettable_row", None))
            ):
                # Why a row of a type `set` changes is not marked (#261: Java).
                never = why_not(
                    variable,
                    frame,
                    parent=ancestry[-1][1] if ancestry else None,
                    trail=trail,
                    member=bool(ancestry),
                    innermost=innermost,
                    unread=unread,
                )
            path = (*ancestry, (name, kind))
            # A debugger's grouping node (debugpy's `special variables`) is not a level.
            depth = sum(1 for step in path if not (step[0] in groups and not step[1]))
            handle = 0
            raw = variable.get("variablesReference", 0)
            own = taint
            if not ancestry and aggregate_taint and type(raw) is int and raw > 0:
                # #108: a struct, tuple or array local that may be moved or dropped.
                own = aggregate_taint(variable, frame, innermost=innermost)
            if tainted in ("pointers", "stale") and own != "stale":
                own = tainted
            if type(raw) is int and raw > 0 and depth < 4:
                self.reference_counter += 1
                handle = self.reference_counter
                self.children[handle] = (dict(frame), scope, path)
                if own:
                    self.taints[handle] = own
                if carried:
                    self.notes[handle] = carried
            output = {
                "name": name,
                "value": str(variable.get("value", ""))[:2000],
                "variablesReference": handle,
            }
            if display_value and (shown := display_value(variable)) is not None:
                output["value"] = shown
            if handle and (whole := getattr(self.runtime, "whole_value", None)) and whole(variable):
                # A Java String or boxed number (#204): a value, its class's own fields
                # listed only when selected by name.
                output["whole"] = True
            if string_value and not (self.placeholders() and is_unavailable(output["value"])):
                # A string the debugger shows only up to a NUL byte (#128); never one it
                # has no value for (optimized out).
                reads = self.view_read if self.capabilities.get("supports_read_memory") else None
                if (shown := string_value(variable, self.view_fetch, reads)) is not None:
                    for key in ("value", "unavailable", "reason", "view", "string_length"):
                        if key in shown:
                            output[key] = shown[key]
                    if shown.get("text") is True:
                        # A C++ string is its text: its members are the library's own
                        # (`(anonymous struct)`, `_M_dataplus`), never listed (eval 6).
                        output["text"] = True
                        self.children.pop(handle, None)
                        self.taints.pop(handle, None)
                        self.notes.pop(handle, None)
                        output["variablesReference"] = handle = 0
            if self.placeholders() and is_unavailable(output["value"]):
                # The placeholder text is the debugger's reason, never the value.
                output["unavailable"] = True
                output["reason"] = output["value"][1:-1][:300]
                output["value"] = None
                unlocated = getattr(self.runtime, "unlocated", None) if not ancestry else None
                if unlocated and (why := unlocated(variable, frame)):
                    # Why no stop reads it, where the build says (#167).
                    output.update(reason=why[:300], withheld=True, withheld_as="not readable")
            elif isinstance(note, str):
                output["note"] = note[:500]
            if (
                ancestry
                and member_value
                and not output.get("unavailable")
                and (fixed := member_value(variable, frame, [s for s, _ in ancestry] + [name]))
            ):
                # A field LLDB reads wrongly (a char-typed bitfield, #136): its number,
                # or no value with the reason (a wide character bitfield).
                if fixed.get("unavailable"):
                    # Peeky's own reason, not the debugger's.
                    output.update(value=None, unavailable=True, reason=fixed["reason"][:300])
                    output.update(withheld=True, withheld_as="not shown", settable=False)
                else:
                    output["value"] = fixed["value"]
            wanted = self.previewing
            if (
                preview
                and (
                    wanted is True
                    or (not ancestry and isinstance(wanted, frozenset) and name in wanted)
                )
                and not output.get("unavailable")
                and not output.get("text")
                and type(raw) is int
                and raw > 0
            ):
                # A C or C++ struct or array, shown by its members (#136); its fields
                # stay selectable through the same handle.
                reads = self.view_read if self.capabilities.get("supports_read_memory") else None
                steps = [step for step, _ in ancestry] + [name]
                try:
                    shown = preview(
                        variable,
                        self.preview_fetch,
                        reads,
                        frame=frame,
                        steps=steps,
                        kinds=[step for _, step in ancestry] + [kind],
                        raw=lambda path: self.raw_value(frame, path),
                        **self.preview_stack(),
                    )
                except ValueError:
                    # The budget is spent: the value stays as the debugger shows it.
                    shown = None
                if shown is not None:
                    output["value"] = str(shown)[:2000]
            if (
                element is not None
                and element.get("strip")
                and not output.get("unavailable")
                and (text := getattr(self.runtime, "element_text", None))
            ):
                # The element's own text, never the reference's address (#291).
                shown = text(variable, frame, output["value"])
                if hasattr(shown, "label"):
                    # Withheld (a Withholding): never the address alone.
                    output.update(value=None, unavailable=True, reason=str(shown)[:300])
                    output.update(withheld=True, withheld_as=str(shown.label)[:100])
                    output["variablesReference"] = 0
                    self.children.pop(handle, None)
                    self.taints.pop(handle, None)
                    self.notes.pop(handle, None)
                    handle, raw, settable = 0, 0, False
                else:
                    output["value"] = shown[:2000]
            if kind is not None:
                output["type"] = kind
            resolve = getattr(self.runtime, "typedef_kind", None)
            if resolve and (resolved := resolve(kind)) is not None:
                # A C/C++ type named by a typedef: a pointer indexes as C does, a reference
                # as its referent (#102).
                output["typedef_kind"], pointee = resolved
                if resolved[0] == "pointer":
                    output["pointee_type"] = pointee
            alias = getattr(self.runtime, "aliased_type", None)
            if alias and (aliased := alias(kind)) is not None:
                # The class behind a typedef'd type: a C++ map by another name (#119).
                output["aliased_type"] = aliased[:2000]
            pointed = getattr(self.runtime, "pointee_aliased_type", None)
            if pointed and (aliased := pointed(kind, output.get("pointee_type"))) is not None:
                # Or behind what a pointer points at (`IntSet *`).
                output["pointee_aliased_type"] = aliased[:2000]
            shown = getattr(self.runtime, "display_type", None)
            if shown and (rust := shown(variable, frame, **self.typed_at(ancestry, name))):
                # The language's own name; the debugger's stays alongside.
                output.update(type=rust, debugger_type=kind)
            if reference_kind is not None and output.get("type") != reference_kind:
                # The element's type; the debugger's, of the reference, alongside (#291).
                output["debugger_type"] = reference_kind
            if isinstance(variable.get("evaluateName"), str) and variable["evaluateName"]:
                # Only whether the debugger could name it as an expression; the expression
                # itself never leaves the worker. debugpy names a dict entry only when its
                # key's repr evaluates back to the key (str, int, ... not an impostor).
                output["evaluable"] = True
            for key in ("indexedVariables", "namedVariables"):
                value = variable.get(key)
                if type(value) is int and 0 <= value <= 2**31 - 1:
                    output[key] = value
            hint = variable.get("presentationHint")
            if isinstance(hint, dict):
                safe_hint = {}
                for key in ("kind", "visibility"):
                    if isinstance(hint.get(key), str):
                        safe_hint[key] = hint[key][:100]
                attributes = hint.get("attributes")
                if isinstance(attributes, list):
                    safe_hint["attributes"] = [
                        v[:100] for v in attributes[:16] if isinstance(v, str)
                    ]
                output["presentationHint"] = safe_hint
            if type(raw) is int and raw > 0 and depth >= 4 and not output.get("text"):
                output["depth_limited"] = True
            if identify and (identity := identify(variable)):
                output["identity"] = identity
            if settable is not None:
                output["settable"] = settable
            if plain:
                output["selected_as"] = plain
            if declared_at:
                output["declared_at"] = declared_at
                if sharing > 1:
                    output["declared_at_shared"] = sharing
            if never:
                output["not_settable"] = never
            if maybe:
                # Neither mark can be promised (#261): `set` decides when it is made.
                output["maybe_settable"] = maybe[:500]
            if own == "stale":
                # Not proven initialized: the bytes a move or drop left (#108).
                output["stale"] = True
            if isinstance(raw_reason, str) and not output.get("unavailable"):
                output["raw_reason"] = raw_reason[:500]
            result.append(output)
            self.inspection_nodes += 1
        return result

    def named_children(self, reference, indexed, name, runtime):
        """The children named ``name`` of the value ``reference`` names. An element LLDB
        names `[N]` of an array with more children than one bounded listing holds is
        fetched alone (`start` N, `count` 1), as reads index it, where the debugger names
        elements so (lldb-dap); anything else from the bounded listing."""
        validate_children = getattr(runtime, "validate_children", None)
        index = re.fullmatch(r"\[(0|[1-9][0-9]{0,9})\]", str(name))
        alone = getattr(runtime, "elements_by_index", False) is True
        if alone and index and type(indexed) is int and indexed > 512 and type(reference) is int:
            if int(index[1]) >= indexed:
                return []
            variables = self.dap.request(
                "variables", {"variablesReference": reference, "start": int(index[1]), "count": 1}
            ).get("variables", [])
            if not isinstance(variables, list) or len(variables) > 1:
                raise ValueError("Variable collection exceeds bounded expansion limit")
        else:
            variables = self.bounded_variables(reference, indexed)
        if validate_children:
            validate_children(variables)
        return [v for v in variables if v.get("name") == name]

    def bounded_variables(self, reference, indexed=None):
        if type(reference) is not int or reference <= 0:
            raise ValueError("Variable is not expandable")
        request = {"variablesReference": reference, "start": 0, "count": 513}
        # java-debug rejects ranges past the end of an array, and any range at all for
        # non-array objects (failing the session). Ask for exactly the stated number of
        # indexed children (refusing oversize collections first), or for Java objects
        # no range. The check below bounds how many children are kept; reply bytes are
        # bounded by the DAP frame limit and the Java adapter's String length cap.
        if type(indexed) is int and indexed > 0:
            if indexed > 512:
                raise ValueError("Variable collection exceeds bounded expansion limit")
            request["count"] = indexed
        elif getattr(self.runtime, "ranges_only_for_indexed", False):
            request = {"variablesReference": reference}
        values = self.dap.request("variables", request).get("variables", [])
        if not isinstance(values, list) or len(values) > 512:
            raise ValueError("Variable collection exceeds bounded expansion limit")
        return values

    def expand(self, args, runtime):
        self.stopped(args)
        # Resolving an expansion can change an adapter's implicit scope context.
        # Require fresh inspection before authorizing another mutation.
        self.references.clear()
        self.reference_frames.clear()
        self.reference_adapter.clear()
        record = self.children.get(args["handle"])
        if record is None:
            raise ValueError("Unknown or stale expansion handle; inspect again")
        if self.inspection_nodes >= 512:
            raise ValueError("Inspection node budget exhausted; inspect again")
        if record[0] == "view":
            # Elements a value view already decoded at this stop: nothing to re-read.
            runtime.assert_current()
            elements = record[1]
            start = args.get("index", args.get("start", 0))
            count = (
                1 if "index" in args else min(args.get("count", 64), 512 - self.inspection_nodes)
            )
            settable = self.preview_settable(record, runtime, elements[start : start + count])
            # LLDB's names for the fields of a value read from memory, as a field it lists
            # raw shows beside the language's (seventh evaluation, review of finding 18).
            listed = self.preview_kinds(record, runtime)
            # Fields of a preview whose root no stop can change (#138), and why; a nested
            # preview's keep its root's.
            source = record[2] if len(record) > 2 else None
            never = record[3] if len(record) > 3 else None
            if source and (rule := getattr(runtime, "never_settable_row", None)):
                name, kind = source[2][0]
                never = rule({"name": name, "type": kind}, source[0], member=True)
            noted = self.notes.get(args["handle"])
            page = []
            for element in elements[start : start + count]:
                handle = 0
                if element.get("children"):
                    # A tuple's or struct's decoded fields, like its elements: worker-owned.
                    self.reference_counter += 1
                    handle = self.reference_counter
                    nested = list(element["children"][:512])
                    self.children[handle] = (
                        ("view", nested, None, never) if never else ("view", nested)
                    )
                    if noted:
                        self.notes[handle] = noted
                shown = {key: value for key, value in element.items() if key != "children"}
                # A field the view did not read (#260) is withheld, not read from memory.
                read = {} if shown.get("unavailable") else {"view": "memory"}
                page.append({**shown, "variablesReference": handle, **read})
                kind = listed.get(element.get("name"))
                if isinstance(kind, str) and kind != shown.get("type"):
                    page[-1]["debugger_type"] = kind
                if noted and not shown.get("unavailable"):
                    # Read through a local shown with a note (#148).
                    page[-1]["note"] = noted[0]
                if settable is not None:
                    page[-1]["settable"] = settable(element.get("name")) and not (
                        noted and noted[1]
                    )
                if never and not page[-1].get("settable"):
                    page[-1]["not_settable"] = never
            self.inspection_nodes += len(page)
            return {
                "variables": page,
                "start": start,
                "truncated": start + len(page) < len(elements),
                "remaining_nodes": 512 - self.inspection_nodes,
            }
        trail = []
        frame, scope_identity, ancestry, reference, indexed = self.walk(record, runtime, trail)
        start = args.get("start", 0)
        count = min(args.get("count", 64), 512 - self.inspection_nodes)
        if "index" in args:
            # The one element `[N]`, fetched alone however long the array (#105).
            variables = self.named_children(reference, indexed, f"[{args['index']}]", runtime)
            start, count = 0, 1
        else:
            variables = self.bounded_variables(reference, indexed)
            if validate_children := getattr(runtime, "validate_children", None):
                validate_children(variables)
        # Each expansion is its own bounded request with a fresh value-view budget.
        self.reset_views()
        self.reset_previews()
        # Previews of the page's values only where asked: a listing shows them, a
        # selector walking to a field needs none (its value is previewed alone).
        self.previewing = args.get("previews", False) is True
        result = self.expose_variables(
            variables[start : start + count],
            frame,
            scope_identity,
            ancestry,
            self.taints.get(args["handle"]),
            root=trail[0] if trail else None,
            noted=self.notes.get(args["handle"]),
            trail=trail,
        )
        runtime.assert_current()
        return {
            "variables": result,
            "start": start,
            "truncated": start + len(result) < len(variables),
            "remaining_nodes": 512 - self.inspection_nodes,
        }

    def preview(self, args, runtime):
        """The preview of the value an expansion handle names (#136), as `inspect` would
        show it: {"value": text}, or {} where it has none. A selector that walks to a
        field or element reads it alone, so the listings it walks through need none."""
        self.stopped(args)
        # As for an expansion: the reads can change an adapter's implicit scope context.
        self.references.clear()
        self.reference_frames.clear()
        self.reference_adapter.clear()
        record = self.children.get(args["handle"])
        if record is None:
            raise ValueError("Unknown or stale expansion handle; inspect again")
        hook = getattr(runtime, "preview_value", None)
        if hook is None or record[0] == "view":
            return {}
        trail = []
        frame, _, ancestry, _, _ = self.walk(record, runtime, trail)
        if not trail:
            return {}
        variable = trail[-1]
        steps = [step for step, _ in ancestry]
        self.reset_previews()
        # Its strings' memory reads (#128) count as an expansion's.
        self.reset_views()
        reads = self.view_read if self.capabilities.get("supports_read_memory") else None
        if self.placeholders() and is_unavailable(variable.get("value")):
            return {}
        try:
            # With `pointee`, what a pointer points at (`p[0]`), as a selector asked:
            # lldb-dap lists it as the pointer's own children.
            shown = hook(
                variable,
                self.preview_fetch,
                reads,
                pointee=args.get("pointee") is True,
                frame=frame,
                steps=steps,
                kinds=[kind for _, kind in ancestry],
                raw=lambda path: self.raw_value(frame, path),
                **self.preview_stack(),
            )
        except ValueError:
            shown = None
        runtime.assert_current()
        return {"value": str(shown)[:2000]} if shown is not None else {}

    def unsettable_here(self, runtime, frame, roots, scopes):
        """Why nothing is settable at this stop, where a rule says so (sixth evaluation,
        finding 8): the stop is part-way through a statement (a loop header's later
        pass, back from a call) or at one that makes an inlined call. Each reason names
        the locals it holds for. Given only when nothing is: no root is marked, and no
        field or element of a struct, tuple or array local would be (one whose rule
        lets its members change, with a member `set` takes, or a member holding
        others, is enough to say nothing). None where nothing could be in any case (an
        optimized build, a debugger that cannot change values)."""
        mid = getattr(runtime, "mid_statement", None)
        if (
            mid is None
            or self.capabilities.get("supports_set_variable") is not True
            or self.optimized()
            or any(row.get("settable") for entry in scopes for row in entry["variables"])
        ):
            return None
        reasons = {}
        for name, reason in self.unsettable_reasons:
            reasons.setdefault(reason, []).append(name)
        if self.member_set():
            aggregates = [
                variable
                for variable in roots
                if type(variable.get("variablesReference")) is int
                and variable["variablesReference"] > 0
                and self.has_memory_location(variable)
            ]
            for variable in aggregates[:8]:
                name = str(variable.get("name"))
                if reason := self.root_reason(runtime, frame, variable):
                    reasons.setdefault(reason, []).append(name)
                    continue
                rule = self.member_rule(runtime, frame, variable)
                try:
                    children = self.bounded_variables(variable["variablesReference"])
                except ValueError:
                    # Too many to list: some may be changeable.
                    return None
                for child in children[:64]:
                    nested = child.get("variablesReference")
                    if (type(nested) is int and nested > 0) or (
                        rule is not None and rule(child, [str(child.get("name"))])
                    ):
                        # A member that may be changed, or may hold one: not nothing.
                        return None
            if len(aggregates) > 8:
                return None
        if not reasons:
            return None
        text = "; ".join(
            f"{', '.join(f'`{name}`' for name in names)}: {reason}"
            for reason, names in reasons.items()
        )
        # Whether no other stop would help either (Everywhere reasons alone).
        return text, all(getattr(reason, "everywhere", False) for reason in reasons)

    def mark_place(self):
        """Where the program is stopped, as far as [settable] marks go: each listed
        frame's function and pc, and the stop's reason, which decide every rule a mark
        follows (the statement under way, what the MIR proves, what a declaration below
        the stop leaves). Later stops at the same place (a loop's hits under `--hits
        all`) reuse its marks; this stop alone where the stack does not say all (a
        frame without a pc, or as many frames as are listed, 10, which may be cut). A
        runtime whose rules read only the stopped frame (``marks_by_frame``: Rust's MIR
        proofs) is placed by that frame alone."""
        frames = getattr(self, "listed_frames", None) or []
        if frames and getattr(self.runtime, "marks_by_frame", False) is True:
            frames = frames[:1]
        stack = tuple((f.get("name"), f.get("instructionPointerReference")) for f in frames)
        if frames and len(frames) < 10 and all(isinstance(pc, str) for _, pc in stack):
            return ("stack", stack, self.stop_reason)
        return ("stop", self.stop_id)

    def remember(self, key, compute):
        """A mark's verdict, kept under ``key`` (see mark_place); bounded."""
        if key not in self.marks:
            if len(self.marks) >= 4096:
                self.marks.clear()
            self.marks[key] = compute()
        return self.marks[key]

    def array_held(self, trail, listing):
        """Why the elements of the array a listing shows (``trail``'s last step) are not
        marked [settable], where the runtime asks the debugger what holds it (#261:
        Java, a JDK class's field, under any name), else None; within what is left of
        the pause budget. A verdict is kept for the rest of the stop only where the
        runtime says it holds that long (every thread suspended); while another thread
        runs, only for this listing (``listing``, a dict) and asked again at the next."""
        ask = getattr(self.runtime, "array_holder", None)
        array = trail[-1] if trail else None
        if ask is None or array is None or not str(array.get("type") or "").endswith("]"):
            return None
        key = ("holder", self.stop_id, str(array.get("type")), str(array.get("value")))
        for kept in (self.marks, listing):
            if key in kept:
                return kept[key]
        left = None
        if self.max_pause and self.stopped_at:
            left = (self.max_pause - (time.monotonic() - self.stopped_at)) * 1000
        verdict, settled = ask(self.dap, array, left)
        listing[key] = verdict
        return self.remember(key, lambda: verdict) if settled else verdict

    def root_reason(self, runtime, frame, root):
        """Why no member of the root local ``root`` can change at this stop (a statement
        under way may hold a copy of it: mid_statement), or None; one per place."""
        mid = getattr(runtime, "mid_statement", None)
        if mid is None:
            return None
        key = ("root", self.mark_place(), str(root.get("name")), str(root.get("type")))
        # A copy of the program's own is judged per member (#192 review).
        every = {"members": True} if getattr(runtime, "projected_copies", False) else {}
        return self.remember(key, lambda: mid(root, frame, self.stop_reason, **every))

    def preview_kinds(self, record, runtime):
        """{field: the type the debugger lists it with} for a value shown as a preview
        read from memory whose fields the debugger lists too (its own storage: a
        struct's or tuple's fields), read once per function and value path; {} for what
        a view decodes alone (a container's elements) or where it cannot be read."""
        source = record[2] if len(record) > 2 else None
        if source is None:
            return {}
        key = ("kinds", source[0].get("name"), tuple(map(tuple, source[2])))

        def read():
            try:
                _, _, _, reference, indexed = self.walk(source, runtime, [])
                variables = self.bounded_variables(reference, indexed)
            except ValueError:
                return {}
            kinds = {}
            for variable in variables:
                kinds.setdefault(variable.get("name"), []).append(variable.get("type"))
            return {name: types[0] for name, types in kinds.items() if len(types) == 1}

        return self.remember(key, read)

    def preview_settable(self, record, runtime, elements):
        """For the fields of a value shown as a preview read from memory (#106): a check
        name -> whether `set` would change that field, judged on the debugger's own
        listing of it, walked again from the frame (once per place: see mark_place).
        What a view decodes without such a source (a container's elements and length,
        an enum's payload) is never changed: `set` refuses it, so it says no. None where
        the runtime judges no member (Python, Java)."""
        if not elements or getattr(runtime, "validate_member", None) is None:
            return None
        source = record[2] if len(record) > 2 else None
        if (
            source is None
            or source[0].get("id") != self.innermost_frame
            or not self.member_set()
            or self.optimized()
        ):
            return lambda name: False
        place = ("preview", self.mark_place(), tuple(map(tuple, source[2])))
        names = [str(element.get("name")) for element in elements]
        if all((*place, name) in self.marks for name in names):
            return lambda name: self.marks.get((*place, str(name)), False)
        trail = []
        try:
            frame, _, ancestry, reference, indexed = self.walk(source, runtime, trail)
        except ValueError:
            # Marks are advice: a value no longer found is left unmarked (`set` says why).
            return None
        rule = self.member_rule(runtime, frame, trail[0]) if trail else None
        if rule is None:
            return None
        listed = {}
        for variable in self.bounded_variables(reference, indexed):
            listed.setdefault(variable.get("name"), []).append(variable)
        prefix = [step for step, _ in ancestry[1:]]

        def judged(name):
            found = listed.get(name, [])
            return len(found) == 1 and rule(found[0], [*prefix, name])

        for name in names:
            self.remember((*place, name), lambda name=name: judged(name))
        return lambda name: self.marks.get((*place, str(name)), False)

    def member_rule(self, runtime, frame, root):
        """For the fields and elements of the root local ``root`` of ``frame``: a check
        (variable, steps) -> whether `set` would change that member here, under every
        rule `set_member` applies (C, C++ and Rust: ``validate_member``), which says no
        to all where no member can change here (another frame, an optimized build, a
        debugger that cannot, a root with no value or memory, a stop part-way through a
        statement). The rules' verdicts are kept per place (see mark_place): what holds
        for the root once, each member's once. None where the runtime judges members
        otherwise (Java's member_settable)."""
        validate = getattr(runtime, "validate_member", None)
        if validate is None:
            return None
        if (
            not self.member_set()
            or frame.get("id") != self.innermost_frame
            or self.optimized()
            or (self.placeholders() and is_unavailable(root.get("value")))
            or not self.has_memory_location(root)
            or self.root_reason(runtime, frame, root)
        ):
            return lambda variable, steps: False
        stack = getattr(self, "listed_frames", None)
        place = (self.mark_place(), str(root.get("name")), str(root.get("type")))
        # A copy of the program's own is judged by the part a change makes (#192 review).
        projected = getattr(runtime, "projected_copies", False)
        mid = getattr(runtime, "mid_statement", None) if projected else None

        def judged(variable, steps):
            if mid is not None and mid(root, frame, self.stop_reason, steps=list(steps)):
                return False
            try:
                # The value it holds: in range, so only the member itself is judged.
                validate(
                    frame,
                    root,
                    steps,
                    variable,
                    variable.get("value"),
                    stack=stack,
                    deeper=self.deeper_stack,
                )
            except ValueError:
                return False
            return True

        def check(variable, steps):
            if self.placeholders() and is_unavailable(variable.get("value")):
                return False
            if not self.has_memory_location(variable):
                return False
            key = ("member", *place, tuple(steps), str(variable.get("type")))
            return self.remember(key, lambda: judged(variable, steps))

        return check

    def member_set(self):
        """Whether `set_variable` may change fields and elements here, not only locals."""
        return (
            self.capabilities.get("supports_set_variable") is True
            and self.capabilities.get("supports_member_set") is True
            and any(
                hasattr(self.runtime or self.adapter, hook)
                for hook in ("member_settable", "validate_member")
            )
        )

    def set_member(self, args, runtime):
        """Change a field or element of a value an expansion handle names (Java; and in
        C, C++ and Rust a scalar the runtime's ``validate_member`` proves the frame holds
        itself, #105).

        The handle only locates the parent: it is walked again from its frame and scope,
        the member must be listed exactly once, and the runtime decides what may change.
        The new value is read back from the parent, not taken from the debugger's echo
        (C, C++ and Rust: walked again from the frame, and it must be the value asked).
        """
        record = self.children.get(args["handle"])
        # A change invalidates every handle: the next read or change inspects again.
        self.children.clear()
        self.taints.clear()
        self.notes.clear()
        self.marks.clear()
        self.references.clear()
        self.reference_frames.clear()
        self.reference_adapter.clear()
        check = getattr(runtime, "member_settable", None)
        validate = getattr(runtime, "validate_member", None)
        if check is None and validate is None:
            raise ValueError(
                "Only the stopped function's own locals can be changed for this language "
                "(fields and elements cannot)"
            )
        if not self.member_set():
            raise ValueError(
                "This Java adapter bundle predates field and element changes, so only locals "
                "can be changed; rebuild it with scripts/setup_java_adapter.py"
                if check is not None
                else "Only the stopped function's own locals can be changed for this language "
                "with this debugger, which cannot change fields and elements"
            )
        previewed = record is not None and record[0] == "view"
        if previewed:
            if len(record) < 3 or record[2] is None:
                raise ValueError(
                    f"{args['name']!r} cannot be changed: it is inside a value Peeky decodes "
                    "itself (such as a Rust Vec, String, Box or enum), whose storage Peeky "
                    "does not write"
                )
            # A #106 preview of a struct's or tuple's own fields: changed as the debugger
            # lists it, under every rule a field there has (#105).
            record = record[2]
        if record is None:
            raise ValueError("Unknown or stale expansion handle; inspect again")
        if record[0].get("id") != self.innermost_frame:
            raise ValueError(_CALLER_SET.format(name=repr(args["name"])))
        if level := self.optimized():
            raise ValueError(
                f"Changing values is refused in an optimized build ({level}); use an "
                "unoptimized build"
            )
        name, value = args["name"], args["value"]
        if getattr(runtime, "text_language", None) and value.startswith('"'):
            raise ValueError(_TEXT_MEMBER.format(name=name))
        runtime.validate_value(value)
        trail = []
        frame, _, ancestry, reference, indexed = self.walk(record, runtime, trail)

        def member():
            return self.named_children(reference, indexed, name, runtime)

        matches = member()
        if len(matches) != 1:
            raise ValueError(f"{name!r} is not a field or element the debugger lists there")
        variable = matches[0]
        if validate is not None:
            return self._set_scalar_member(
                record, runtime, frame, ancestry, trail, reference, variable, args, previewed
            )
        parent = ancestry[-1][1] if ancestry else None
        # The whole selector, not the debugger's name alone (`ranking[1]`, not `1`; #261).
        selector = member_selector(ancestry, name)
        internal = getattr(runtime, "internal_member", None)
        if internal and (reason := internal(variable, parent=parent, trail=trail)):
            # A JDK class's own state (#204), or a launcher that cannot tell.
            raise ValueError(f"`{selector}` cannot be changed: {reason}")
        if reason := check(variable, parent=parent):
            raise ValueError(f"`{selector}` cannot be changed: {reason}")
        runtime.validate_variable(variable, value)
        before = variable.get("value")
        literal = getattr(runtime, "mutation_literal", None)
        request = {
            "variablesReference": reference,
            "name": name,
            "value": literal(value, variable=variable) if literal else value,
        }
        if getattr(runtime, "pause_budget_writes", False) and self.max_pause and self.stopped_at:
            # What the launcher may spend searching what refers to an array (#204).
            left = self.max_pause - (time.monotonic() - self.stopped_at)
            request["peekyPauseLeftMs"] = max(0, int(left * 1000))
        try:
            changed = self.dap.request("setVariable", request)
        except RequestFailed as exc:
            raise ValueError(
                f"The debugger refused to change `{selector}` ({str(exc)[:200]}); it is unchanged"
            ) from None
        runtime.assert_current()
        after = changed.get("value")
        reread = member()
        if len(reread) != 1 or reread[0].get("value") != after:
            # The write happened but cannot be confirmed: the run's state is unknown.
            raise RuntimeError(
                f"The change to `{selector}` could not be confirmed: the debugger reported "
                f"{str(after)[:80]}, and the value now reads "
                f"{str(reread[0].get('value'))[:80] if len(reread) == 1 else 'nothing'}"
            )
        if confirm := getattr(runtime, "confirm_mutation", None):
            confirm(selector, value, before, after, variable=variable)
        return {"name": name, "before": before, "after": after}

    def _set_scalar_member(
        self, record, runtime, frame, ancestry, trail, reference, variable, args, previewed=False
    ):
        """set_member for C, C++ and Rust (#105): the runtime proves the change reaches
        the program (``validate_member``), and the value read back, walked again from the
        frame, must be the one asked; anything else is never reported as a change.
        ``previewed``: the parent is shown as a preview read from memory (#106), which
        must show the same value afterwards."""
        name, value = args["name"], args["value"]
        root, steps = trail[0], [step for step, _ in ancestry[1:]] + [name]
        pairs = ((root, ancestry[0][0]), (variable, name))
        for each, shown in pairs:
            if self.placeholders() and is_unavailable(each.get("value")):
                raise ValueError(
                    f"{shown!r} has no readable value here ({str(each.get('value'))[1:-1][:200]}); "
                    f"{name!r} cannot be changed"
                )
        runtime.validate_member(
            frame,
            root,
            steps,
            variable,
            value,
            stack=getattr(self, "listed_frames", None),
            deeper=self.deeper_stack,
        )
        for each, shown in pairs:
            if not self.has_memory_location(each):
                raise ValueError(
                    f"{shown!r} has no memory location here (a computed value): {name!r} "
                    "cannot be changed, as a change would not reach the program"
                )
        selector = ancestry[0][0] + "".join(
            step if step.startswith("[") else f".{step}" for step in steps
        )
        self.refuse_mid_statement(runtime, root, frame, selector, steps)
        before = variable.get("value")
        literal = getattr(runtime, "mutation_literal", None)
        try:
            self.dap.request(
                "setVariable",
                {
                    "variablesReference": reference,
                    "name": name,
                    "value": literal(value, variable=variable) if literal else value,
                },
            )
        except RequestFailed as exc:
            raise ValueError(
                f"The debugger refused to change {name!r} ({str(exc)[:200]}); it is unchanged"
            ) from None
        runtime.assert_current()
        # Read back from a fresh walk (new scopes, new children), not the debugger's echo.
        try:
            _, _, _, reread, indexed = self.walk(record, runtime)
            found = self.named_children(reread, indexed, name, runtime)
        except Exception as exc:
            found = [str(exc)[:200]]
        if (
            len(found) != 1
            or not isinstance(found[0], dict)
            or not isinstance(found[0].get("value"), str)
        ):
            # A write was attempted, and what it did cannot be confirmed: fail closed.
            raise RuntimeError(
                f"The change to {name!r} was sent, but could not be read back"
                + (f" ({found[0]})" if found and isinstance(found[0], str) else "")
                + ": the program's state is unknown, so the session is closed"
            )
        after = found[0]["value"]
        # Exactly the value asked, or an error: never a change reported that is not made.
        runtime.confirm_mutation(name, value, before, after, variable=variable)
        if previewed:
            self._confirm_preview(record, runtime, name, after)
        return {"name": name, "before": before, "after": after}

    def text_stop(self):
        """The stop's reason as a text change judges where in its line the stop is
        (#164): a breakpoint at a later one of its line's locations in the function
        (#157: a loop header's) is not before the line's first code, so every place of
        the line counts."""
        if self.stop_reason == "breakpoint" and self.stop_place and self.stop_place[0] > 1:
            return "breakpoint-later"
        return self.stop_reason

    def text_storage(self, runtime, variable, frame):
        """(kind, shape) of the root string a text change (#137) writes: the runtime's,
        where it reads the build's debug information, else text_sets.text_kind's by the
        debugger's type name; ValueError naming why it is none."""
        hook = getattr(runtime, "text_storage", None)
        if hook is not None:
            return hook(variable, frame)
        return text_kind(variable, runtime.text_language), None

    def text_settable(self, variable, frame):
        """Whether a text change (#137) could be made to this root string here, for its
        `[settable]` mark: a char array always (by element where the debugger cannot
        write memory); a std::string where the debugger writes memory, or where it reads
        it and the text is in the string's own buffer; a Rust String where it reads and
        writes memory."""
        if not getattr(self.runtime, "text_language", None):
            return False
        try:
            kind = self.text_storage(self.runtime, variable, frame)[0]
        except ValueError:
            return False
        check = getattr(self.runtime, "text_settable", None)
        if kind is None or (check is not None and not check(variable, frame, self.text_stop())):
            return False
        reads = self.capabilities.get("supports_read_memory")
        if kind == "array" or (reads and self.capabilities.get("supports_write_memory")):
            return True
        if kind != "std" or not reads:
            return False
        # lldb-dap 20 changes a string in its own short buffer, element by element.
        memory = TextMemory(
            lambda parent, count=None: self.view_fetch(parent),
            None,
            lambda address, count: self.view_read(f"0x{address:x}", count),
        )
        return held_in_place(variable, memory)

    def view_settable(self, variable, frame):
        """The `[settable]` mark of a root string a runtime's view shows (a Rust String,
        #137), as for a root listed as the debugger shows it; None for other values."""
        if not getattr(self.runtime, "text_language", None):
            return None
        try:
            self.text_storage(self.runtime, variable, frame)
        except ValueError:
            return None
        mid = getattr(self.runtime, "mid_statement", None)
        return bool(
            self.capabilities.get("supports_set_variable", False) is True
            and frame.get("id") == self.innermost_frame
            and not self.optimized()
            and self.has_memory_location(variable)
            and self.text_settable(variable, frame)
            and not (mid and mid(variable, frame, self.stop_reason))
        )

    def set_text(self, runtime, variable, reference, kind, text, shape=None):
        """Write ``text`` into the string ``variable``, a root local listed under the
        scope ``reference`` (#137, see text_sets): only within its storage, and read
        back. A refusal (ValueError) changed nothing; anything written that cannot be
        confirmed closes the session."""
        name = variable.get("name")

        def children(parent, count=None):
            bounds = {"start": 0, "count": count} if count else {}
            return self.dap.request("variables", {"variablesReference": parent, **bounds}).get(
                "variables", []
            )

        def relist():
            found = [row for row in children(reference) if row.get("name") == name]
            if len(found) != 1:
                raise RuntimeError(
                    f"{name!r} could not be found again after a change to it: the program's "
                    "state is unknown, so the session is closed"
                )
            return found[0]

        def change(parent, member, value):
            reply = self.dap.request(
                "setVariable", {"variablesReference": parent, "name": member, "value": value}
            )
            return runtime.mutation_value(reply)

        def read(address, count):
            if not 0 < count <= 64 * 1024:
                raise ValueError("Memory read exceeds limit")
            reply = self.dap.request(
                "readMemory", {"memoryReference": f"0x{address:x}", "count": count}
            )
            data = base64.b64decode(reply.get("data") or "", validate=True)
            if len(data) != count or reply.get("unreadableBytes"):
                raise ValueError("Partial memory read")
            return data

        def write(address, data):
            reply = self.dap.request(
                "writeMemory",
                {"memoryReference": f"0x{address:x}", "data": base64.b64encode(data).decode()},
            )
            if reply.get("bytesWritten", len(data)) != len(data):
                raise RuntimeError(f"wrote {reply.get('bytesWritten')} of {len(data)} bytes")

        capabilities = self.capabilities
        memory = TextMemory(
            children,
            change,
            read if capabilities.get("supports_read_memory") else None,
            write if capabilities.get("supports_write_memory") else None,
        )
        changed = change_text(variable, text, memory, relist, kind, shape)
        runtime.assert_current()
        result = {"name": name, "before": changed["before"], "after": changed["after"]}
        if kind == "std":
            # What the program took from it before cannot be seen, nor changed (#164).
            result["note"] = STRING_VIEW_NOTE
        return result

    def _confirm_preview(self, record, runtime, name, after):
        """After a change to a field of a value shown as a preview read from memory (#106),
        read the preview again as `inspect` and `get` would: the field must show what the
        debugger read back. A preview that no longer reads is shown as the debugger lists
        the value, which matched; one that shows anything else closes the session."""
        trail = []
        try:
            frame, _, path, _, _ = self.walk(record, runtime, trail)
            self.reset_views()
            view = runtime.view_variable(
                trail[-1],
                self.view_fetch,
                self.view_read,
                frame=frame,
                top=len(path) == 1,
                innermost=len(path) == 1 and frame.get("id") == self.innermost_frame,
                stop=self.stop_reason,
            )
        except Exception as exc:
            raise RuntimeError(
                f"The change to {name!r} was made, but its value could not be read again "
                f"from memory ({str(exc)[:200]}): the program's state is unknown, so the "
                "session is closed"
            ) from None
        if view is None or not view.get("children"):
            return
        shown = [child for child in view["children"] if child.get("name") == name]
        if len(shown) != 1 or not _same_number(shown[0].get("value"), after):
            raise RuntimeError(
                f"The change to {name!r} reads back as {str(after)[:80]} from the debugger "
                f"but as {str(shown[0].get('value'))[:80] if len(shown) == 1 else 'nothing'} "
                "from memory: the program's state is unknown, so the session is closed"
            )

    @staticmethod
    def typed_at(ancestry, name):
        """display_type's arguments for the value ``name`` under ``ancestry``: a root
        local's, or a field's or element's with its path from the root (finding 18)."""
        if not ancestry:
            return {"top": True}
        return {"top": False, "path": [step for step, _ in ancestry] + [name]}

    def refuse_never_settable(self, runtime, program, changes):
        """Refuse, before the program runs, a probe's change ({line, name}: a `--set` at
        its `--at` line, a `--set-at` at its own, #137) that no stop of its line can make
        in this build (#138): every change in an optimized build, and what the runtime's
        build facts rule out in every function with code on that line
        (``never_settable``: a Rust local the IR keeps in a register, a field reached
        through a pointer). What depends on the stop (a statement under way, a local not
        yet initialized) is judged there."""
        from peeky.errors import Refused

        refusal = None
        members = [
            change["name"]
            for change in changes
            if change.get("text") and re.search(r"[.\[]", change["name"])
        ]
        if members and getattr(runtime, "text_language", None):
            refusal = _TEXT_MEMBER.format(name=members[0]) + _BEFORE_LAUNCH
        elif level := self.optimized():
            note = getattr(runtime, "storage_note", None)
            first = changes[0]
            refusal = (
                f"`{first['name']}` cannot be changed: use an unoptimized build. Changing "
                f"values is refused in an optimized build ({level}), because the program may "
                "keep using other copies of a value"
                + ((note(program, [first["line"]], first["name"]) or "") if note else "")
                + _BEFORE_LAUNCH
            )
        elif check := getattr(runtime, "launch_refusal", None):
            # Java (#183): each change from the class files' declared types, its value too.
            for change in changes:
                if refusal := check(program, change["line"], change["name"], change.get("value")):
                    break
        elif check := getattr(runtime, "never_settable", None):
            lines, texts = {}, {}
            for change in changes:
                # A text (#137) is judged as a string's change, an integer as a number's.
                group = texts if change.get("text") else lines
                group.setdefault(change["line"], []).append(change["name"])
            for line, names in lines.items():
                # Each change judged against its own line's functions only.
                if refusal := check(program, [line], names):
                    break
            for line, names in texts.items() if not refusal else ():
                if refusal := check(program, [line], names, text=True):
                    break
        if refusal:
            # (code, message), or a message: a change no stop allows.
            code, refusal = (
                refusal if isinstance(refusal, tuple) else ("MUTATION_REJECTED", refusal)
            )
            refused = Refused(code, refusal)
            refused.before_launch = True
            raise refused

    def refuse_mid_statement(self, runtime, root, frame, selector, steps=None):
        """Refuse a change to the root local ``root`` (or what ``selector`` reaches in it:
        the debugger's child names ``steps``) where the runtime says a statement under
        way at the stop may hold a copy of its old value (#105): the change would miss
        that statement. The session is kept."""
        check = getattr(runtime, "mid_statement", None)
        part = {"steps": list(steps)} if steps and getattr(runtime, "projected_copies", 0) else {}
        if check and (reason := check(root, frame or {}, self.stop_reason, **part)):
            if advice := getattr(reason, "advice", None):
                # The program's own binding holds the copy (#192): its own way around.
                raise ValueError(f"{selector!r} cannot be changed here: {reason}. {advice}")
            if getattr(reason, "everywhere", False):
                # No stop of this run allows one: no advice to stop elsewhere.
                raise ValueError(f"{selector!r} cannot be changed: {reason}")
            raise ValueError(
                f"{selector!r} cannot be changed here. Change it at a statement's first "
                "instruction: stop on the statement's first line (a probe's `--at`, `session "
                "continue --to LINE`), or `session step` to it. Here, "
                f"{reason}, so the statement may hold a copy of the old value and would not "
                "see the change"
            )

    def walk(self, record, runtime, trail=None):
        """The value an expansion handle names, found again from its frame and scope by
        each step's name and type: (frame, scope identity, path, reference, indexed).
        ``trail``, if given, receives the debugger's variable for each step."""
        original_frame, scope_identity, ancestry = record
        frames = self.dap.request("stackTrace", {"threadId": self.thread_id, "levels": 10}).get(
            "stackFrames", []
        )[:10]
        if listed := getattr(runtime, "frames_listed", None):
            listed(frames)
        matches = [f for f in frames if f.get("id") == original_frame.get("id")]
        if len(matches) != 1 or self.frame_identity(matches[0]) != self.frame_identity(
            original_frame
        ):
            raise ValueError("Expansion frame identity changed; inspect again")
        frame = matches[0]
        runtime.validate_stop(frame)
        if validate_debugger := getattr(runtime, "validate_debugger", None):
            validate_debugger(self.dap, frame)
        scopes = self.frame_scopes(
            runtime, frame, self.dap.request("scopes", {"frameId": frame["id"]}).get("scopes", [])
        )
        matches = [scope for scope in scopes if self.scope_identity(scope) == scope_identity]
        if len(matches) != 1:
            raise ValueError("Expansion scope is missing or ambiguous")
        reference = matches[0].get("variablesReference")
        indexed = matches[0].get("indexedVariables")
        for name, kind in ancestry:
            matches = self.named_children(reference, indexed, name, runtime)
            if len(matches) != 1 or matches[0].get("type") != kind:
                raise ValueError("Expansion ancestry changed or is ambiguous")
            reference = matches[0].get("variablesReference")
            indexed = matches[0].get("indexedVariables")
            if trail is not None:
                trail.append(matches[0])
        return frame, scope_identity, ancestry, reference, indexed

    def close(self):
        """End the session; returns what became of the processes it started, if any."""
        adapter, self.dap = self.dap, None
        runtime, self.runtime = self.runtime, None
        pycache, self.pycache = self.pycache, None
        self.state = "idle"
        self.stopped_at = None
        self.empty_thread_replies = 0
        self.reset_lost_stops()
        self.stop_location = None
        self.stop_function = None
        self.stop_reason = None
        self.session = None
        self.stop_id = None
        self.thread_id = None
        self.capabilities = {}
        self.frames.clear()
        self.listed_frames = []
        self.deeper_frames = None
        self.thread_count = None
        self.whole_frames = None
        self.references.clear()
        self.global_handles.clear()
        self.reference_frames.clear()
        self.reference_adapter.clear()
        self.children.clear()
        self.taints.clear()
        self.notes.clear()
        cleanup = None
        try:
            if adapter:
                if runtime is not None and getattr(runtime, "terminate_debuggee", True) is False:
                    cleanup = adapter.close(terminate=False)
                else:
                    cleanup = adapter.close()
                # What ending the session did to the processes it started (#143).
                self.last_cleanup = cleanup if isinstance(cleanup, str) else None
                # The output as the session's end left it, and whether it is whole (#203).
                final = getattr(adapter, "final", None)
                self.last_final = final if isinstance(final, dict) else None
        finally:
            try:
                if runtime:
                    runtime.close()
            finally:
                # Once the adapter and the debuggee are gone (#249).
                if pycache is not None:
                    shutil.rmtree(pycache, ignore_errors=True)
        return self.last_cleanup if adapter else None


def _frame_identity(frame):
    """(name, instruction, line) a frame is told apart by; the instruction where the
    debugger gives one (LLDB: a caller's is where its call returns)."""
    return (
        str(frame.get("name") or "")[:300],
        frame.get("instructionPointerReference"),
        frame.get("line"),
    )


def call_change(before, after):
    """How a stop's whole stack (``after``) differs from the one a session's step started
    from (``before``), each a list of `_frame_identity`s, innermost first (#262). A caller
    waiting on a call keeps its line and instruction; the innermost frame's line moves, so
    only its name counts. Depths count too, so a recursive call is told from its caller.

    These are stacks, not calls: an activation is not told from another at the same depth
    under callers at the same lines. So the kinds say only what the stacks show, and never
    that a call returned (it may have thrown, yielded or been unwound):

    {"kind": "same"}: the same function at the same depth, under the same callers.
    "left": shallower by `calls` frames, over the earlier innermost frame's callers.
    "deeper": deeper by `calls` frames, over the earlier innermost frame's function.
    "other": the earlier innermost function is no longer at its depth, and this stack is
    not one of those shapes.
    None where nothing is told: the same function at the earlier depth, with other
    callers (another call, or a Python generator resumed from elsewhere)."""
    if not before or not after:
        return None
    change = {"from": before[0][0], "to": after[0][0]}
    depth = len(after) - len(before)
    if depth < 0:
        up = -depth
        if before[up][0] == change["to"] and after[1:] == before[up + 1 :]:
            return {"kind": "left", **change, "calls": up}
        return {"kind": "other", **change}
    if after[depth][0] != change["from"]:
        return {"kind": "other", **change}
    if after[depth + 1 :] != before[1:]:
        return None
    return {"kind": "deeper", **change, "calls": depth} if depth else {"kind": "same"}


def _method_name(function):
    """A function's own name as debuggers label what it returned: `Ret.twice(int)` ->
    `twice`, `before(Task const&, Task const&)` -> `before`."""
    head = str(function or "").split("(", 1)[0].strip()
    return re.split(r"[.:]", head)[-1] if head else ""


def member_selector(ancestry, name):
    """The selector naming the member ``name`` of the value ``ancestry`` walks to, from
    the debugger's names: `ranking[1]`, `this.count`. Java names an array's elements by
    their index alone."""
    text, kind = "", None
    for step, step_kind in (*ancestry, (name, None)):
        step = str(step)
        if not text:
            text = step
        elif step.startswith("["):
            text += step
        elif str(kind or "").endswith("]") and re.fullmatch(r"[0-9]+", step):
            text += f"[{step}]"
        else:
            text += f".{step}"
        kind = step_kind
    return text


_CALLER_SET = (
    "{name} is in a caller's frame, whose values cannot be changed: only the stopped "
    "(innermost) frame's can, which `session set` and probes change"
)
# Java (#164): a local no path from the stop reads again; or where that is not known.
_UNREAD_WHY = (
    "the program does not read it after this stop (a lambda or inner class that captured "
    "it before keeps its own copy; javac compiles a constant local into its uses)"
)
_UNREAD = (
    "{name} cannot be changed at this stop: " + _UNREAD_WHY + ", so a change would never be seen"
)
_UNREAD_UNKNOWN = (
    "Peeky could not tell whether the program reads it after this stop; a lambda or inner "
    "class that captured it before keeps its own copy, which the change does not reach"
)
# A std::string's text changes in place (#164): a view the program took before the
# change keeps the old size.
STRING_VIEW_NOTE = (
    "a std::string_view, c_str() or data() pointer or iterator the program took from the "
    "string before the change still has its old size, and reads a mix of old and new text "
    "(Peeky cannot see those)"
)
# C, C++ and Rust (#137): a text changes a string local, not a field or element.
_TEXT_MEMBER = (
    "`{name}` cannot be changed to a text: copy it into a local with a candidate edit "
    "(`--old`/`--new`), or edit the source. Peeky writes text only into a string local "
    "that the stopped frame holds (a char array, a std::string or a Rust String), not into a "
    "field or element"
)
_BEFORE_LAUNCH = (
    ". Refused before the program runs: the debugger's launch/attach request was not sent"
)


def _name_list(value):
    """A request's list of at most 32 names a selector can write (a Unicode identifier
    with no invisible character, #195), or names LLDB gives a local with where it is
    declared (`s @ main.rs:81`, when several of that name are in scope)."""
    return (
        isinstance(value, list)
        and len(value) <= 32
        and all(
            isinstance(name, str)
            and len(name) <= 400
            and (is_name(name) or is_located(name, _SHADOWED_PLACE))
            for name in value
        )
    )


_SHADOWED_PLACE = re.compile(r" @ [^\s:@\[\]'\"]{1,255}:[0-9]{1,9}")
_NAMED = (
    "a Unicode identifier of at most 128 characters with no invisible character (U+200B, "
    "U+200D, a bidi control)"
)
_SET_NAME = (
    f"set_variable's name must be a local's name a selector can write: {_NAMED}, or one "
    "LLDB gives with where it is declared (`s @ main.rs:81`)"
)
_SET_MEMBER = (
    f"set_variable's name must be a field's name a selector can write ({_NAMED}) or an "
    "element's index as the debugger names it (`[3]`, `3`)"
)


def _public_sources(runtime, items):
    """Breakpoints or frames as answered: copies, with a build copy's source shown as the
    user's file, as `location` is (#252). The debugger's own frames are left as they are,
    for the rules that compare them."""
    shown = []
    for item in items or []:
        source = item.get("source") if isinstance(item, dict) else None
        path = source.get("path") if isinstance(source, dict) else None
        if isinstance(path, str) and path:
            item = {**item, "source": {**source, "path": _display_path(runtime, path)}}
        shown.append(item)
    return shown


def _public_placed(runtime, placed):
    if isinstance(placed, dict) and isinstance(placed.get("breakpoints"), list):
        return {**placed, "breakpoints": _public_sources(runtime, placed["breakpoints"])}
    return placed


def _source_names(value, root, names, parent=None):
    """A remote client's view of a result (#252): each `location`, and each `source`'s
    `path`, that is one of the sources it compared becomes `source:NAME` (with its
    `:LINE`), NAME relative to the root. The client shows its own copy of NAME there;
    any other path stays this host's."""
    if isinstance(value, list):
        return [_source_names(item, root, names, parent) for item in value]
    if not isinstance(value, dict):
        return value
    shown = {}
    for key, item in value.items():
        if isinstance(item, str) and (key == "location" or (key == "path" and parent == "source")):
            path, line = item, ""
            if key == "location":
                head, separator, tail = item.rpartition(":")
                if separator and tail.isdigit():
                    path, line = head, separator + tail
            if (name := _compared_name(path, root, names)) is not None:
                item = f"source:{name}{line}"
        shown[key] = _source_names(item, root, names, key)
    return shown


def _compared_name(path, root, names):
    """The name of the compared source ``path`` is, relative to ``root``; None unless it
    is a regular file under the root, named without `..` and reached through no link, and
    ``names`` has it."""
    if not path or not os.path.isabs(path) or "`" in path:
        return None
    pure = PurePosixPath(path)
    if any(part in (".", "..") for part in pure.parts):
        return None
    try:
        name = pure.relative_to(root).as_posix()
    except ValueError:
        return None
    if name not in names:
        return None
    try:
        real = Path(path).resolve(strict=True)
        # Through no link: the file itself is what the name says, in the root.
        if real.relative_to(Path(root).resolve()).as_posix() != name or not real.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return name


def _display_path(runtime, path):
    """A prepared build's copy of a source, shown as the user's original file.

    Display only: compiled and Java targets run from a private build workspace that
    mirrors the source tree, so `<workspace>/src/main.rs` is `<source>/src/main.rs`.
    """
    prepared = getattr(runtime, "prepared", None)
    workspace = getattr(prepared, "workspace", None)
    source_root = getattr(prepared, "source_root", None)
    if workspace is None or source_root is None:
        return path
    if "`" in path:
        # LLDB's name for a frame without line information (`module`function`).
        return path
    try:
        relative = Path(path).resolve().relative_to(Path(workspace).resolve())
    except (OSError, ValueError):
        return path
    manifest = getattr(prepared, "manifest", None)
    if manifest is not None:
        # Only an input the build copied unchanged is the user's file. A candidate's
        # edited file is the copy that ran: its lines are the candidate's, not the user's.
        built = {item.path: item.sha256 for item in manifest.inputs}
        original = {item.path: item.sha256 for item in manifest.original_inputs}
        name = relative.as_posix()
        if name not in built or built[name] != original.get(name):
            return path
    shown = Path(source_root) / relative
    # Only a file the user has (a source, not a build product) is shown as theirs.
    return str(shown) if shown.is_file() else path


def _in_program_sources(runtime, frame, root):
    """Whether a frame is in one of the program's own source files.

    Compiled targets: a declared source of the prepared build (a frame without line
    information, which LLDB names with a backquote, never is). Python: a file under
    the worker's root.
    """
    path = (frame.get("source") or {}).get("path")
    if not isinstance(path, str) or not path or "`" in path:
        return False
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, ValueError):
        return False
    lines, prepared = getattr(runtime, "lines", None), getattr(runtime, "prepared", None)
    bases = (
        (prepared.workspace, prepared.source_root)
        if prepared is not None
        else (runtime.root,)
        if getattr(runtime, "root", None) is not None
        else ()
    )
    if isinstance(lines, dict) and bases:
        for base in bases:
            try:
                relative = resolved.relative_to(Path(base).resolve()).as_posix()
            except (OSError, ValueError):
                continue
            return relative in lines
        return False
    return _inside_program(runtime, frame, root)


def _inside_program(runtime, frame, *roots):
    """Whether a frame's source lies in the prepared or declared program directories
    (or in any of ``roots``, such as the worker's own root for Python targets)."""
    path = (frame.get("source") or {}).get("path")
    if not isinstance(path, str) or not path:
        return False
    prepared = getattr(runtime, "prepared", None)
    roots = [
        getattr(prepared, "workspace", None),
        getattr(prepared, "source_root", None),
        getattr(runtime, "root", None),
        *roots,
    ]
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return False
    for root in roots:
        if root is None:
            continue
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def make_server(root, token, port=8765, *, adapter=None):
    if not isinstance(token, str) or len(token) < 16:
        raise ValueError("Token must contain at least 16 characters")
    worker = DebugWorker(root, adapter=adapter)
    probes = threading.Semaphore(1)

    def probe(args):
        # Stateless and hard-bounded: a separate process with its own worker, killed at
        # the cap so an HTTP client timeout can never leave work holding the slot.
        from peeky.probe import HTTP_PROBE_SECONDS, Probe, parse_location

        if set(args) - {
            "at",
            "capture",
            "set",
            "hits",
            "timeout",
            "until",
            "set_hit",
            "entry",
            "args",
            "expect_sha256",
            "expect_inputs",
            "edit",
            "set_at",
        }:
            raise ValueError("Unknown probe field")
        from peeky.sources import check_expected

        if args.get("expect_inputs") is not None:
            check_expected(args["expect_inputs"])
        set_at = args.get("set_at", [])
        if not isinstance(set_at, list) or not all(
            isinstance(point, list)
            and len(point) == 4
            and type(point[0]) is int
            and (type(point[1]) is int or point[1] == "all")
            and all(isinstance(part, str) for part in point[2:])
            for point in set_at
        ):
            raise ValueError(
                "Probe set_at must be a list of [line, hit, name, value] (hit: a number, or 'all')"
            )
        edit = args.get("edit")
        # One {old, new} object, or a list of them for several edits.
        edits = edit if isinstance(edit, list) else [edit]
        if edit is not None and (
            not edits
            or len(edits) > 32
            or not all(
                isinstance(item, dict)
                and set(item) == {"old", "new"}
                and all(isinstance(item[key], str) for key in ("old", "new"))
                for item in edits
            )
        ):
            raise ValueError(
                "Probe edit must be an object with string fields old and new, or a list of them"
            )
        program, line = parse_location(args.get("at"))
        changes = args.get("set", {})
        if not isinstance(changes, dict) or not isinstance(args.get("capture", []), list):
            raise ValueError("Probe capture must be a list and set an object")
        request = Probe(
            program,
            line,
            tuple(args.get("capture", [])),
            tuple(changes.items()),
            args.get("hits", 1),
            min(args.get("timeout", HTTP_PROBE_SECONDS), HTTP_PROBE_SECONDS - 10),
            args.get("until", "exit"),
            args.get("set_hit", 1),
            entry=args.get("entry"),
            args=tuple(args.get("args", [])) if isinstance(args.get("args", []), list) else None,
            expect_sha256=tuple(args["expect_sha256"].items())
            if isinstance(args.get("expect_sha256"), dict)
            else (),
            edit=None
            if edit is None
            else (edit["old"], edit["new"])
            if isinstance(edit, dict)
            else tuple((item["old"], item["new"]) for item in edits),
            set_at=tuple(tuple(point) for point in set_at),
            expect_inputs=None
            if args.get("expect_inputs") is None
            else tuple(sorted(args["expect_inputs"].items())),
        )
        if not probes.acquire(blocking=False):
            raise ValueError("Another probe is running; retry later")
        try:
            return _run_probe_process(Path(root).resolve(), request, adapter, HTTP_PROBE_SECONDS)
        finally:
            probes.release()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            # Compared as bytes: a non-ASCII header must be a 401, not a dropped connection.
            supplied = self.headers.get("Authorization", "").encode("latin-1", "replace")
            if not hmac.compare_digest(supplied, ("Bearer " + token).encode()):
                self.reply(
                    401,
                    {
                        "error": {
                            "code": "UNAUTHORIZED",
                            "message": "Missing or wrong bearer token",
                            "hint": "Use the DEBUG_TOKEN the worker was started with.",
                            "retryable": False,
                            "session_closed": False,
                        },
                        "protocol": PROTOCOL_VERSION,
                    },
                )
                return
            try:
                # Nothing before dispatch touches the session: its failures are request
                # errors, never "session closed".
                try:
                    size = int(self.headers.get("Content-Length", 0))
                    if not 0 < size <= MAX_REQUEST_BYTES:
                        raise ValueError(f"Request body must be 1..{MAX_REQUEST_BYTES} bytes")
                    self.connection.settimeout(10)
                    body = self.rfile.read(size)
                    if len(body) != size:
                        raise ValueError("Request body ended early")
                    args = json.loads(body)
                except (OSError, RecursionError, UnicodeDecodeError) as exc:
                    raise ValueError(
                        f"Request body could not be read as JSON ({type(exc).__name__})"
                    ) from exc
                if not isinstance(args, dict):
                    raise ValueError("Expected a JSON object")
                operation = self.path.removeprefix("/")
                if (
                    operation in ("launch", "probe")
                    and args.get("expect_sha256") is not None
                    and args.get("expect_inputs") is None
                ):
                    # A remote client that checks only the program would get values from
                    # other code without a note (#249).
                    raise ValueError(
                        "This client sent no sha256 for the target's other sources "
                        "(expect_inputs), so the worker cannot compare them: install the same "
                        "Peeky version on both hosts"
                    )
                if operation in ("launch", "probe"):
                    declared = args.pop("protocol", None)
                    if args.get("expect_inputs") is not None and declared != PROTOCOL_VERSION:
                        # A client of protocol 2 sends expect_inputs and no protocol; it would
                        # show this worker's `source:` names as paths (#252).
                        spoken = declared if type(declared) is int else 2
                        raise ValueError(
                            f"Client speaks protocol {spoken}, this worker {PROTOCOL_VERSION}: "
                            "install the same Peeky version on both hosts"
                        )
                if operation == "version":
                    from peeky.sources import describe

                    result = {
                        "protocol": PROTOCOL_VERSION,
                        # Which sources a client hashes for expect_inputs (#249).
                        "sources": describe(worker.adapter),
                        # Where its paths start: a remote client shows them as its own (#252).
                        "root": str(worker.root),
                        "operations": sorted(OPERATIONS),
                        "error_codes": sorted(
                            {rule[1] for rule in _RULES}
                            | {
                                "DEBUGGER_ERROR",
                                "INVALID_REQUEST",
                                "UNAUTHORIZED",
                                "INTERNAL_ERROR",
                            }
                        ),
                    }
                elif operation == "shutdown":
                    # Token-authenticated: only the owner of this worker can stop it.
                    with worker.lock:
                        worker.close()
                    threading.Thread(target=server.shutdown, daemon=True).start()
                    result = {"state": "shutdown"}
                else:
                    result = (
                        probe(args) if operation == "probe" else worker.dispatch(operation, args)
                    )
                    names = (
                        args.get("expect_inputs") if operation == "probe" else worker.remote_names
                    )
                    if isinstance(names, (dict, frozenset)) and names:
                        result = _source_names(result, worker.root, frozenset(names))
                self.reply(200, result)
            except (KeyError, TypeError, ValueError) as exc:
                self.reply(
                    400,
                    {"error": classify(exc, session_closed=False), "protocol": PROTOCOL_VERSION},
                )
            except Exception as exc:
                self.reply(500, {"error": classify(exc), "protocol": PROTOCOL_VERSION})

        def reply(self, status, body):
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Peeky-Protocol", str(PROTOCOL_VERSION))
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.worker = worker
    return server


# A probe child's launch budget is the HTTP cap less this, so that a slow launch (a
# build) ends in the child's own answer, before the watchdog at ``cap - 5`` (#190 review).
PROBE_LAUNCH_MARGIN_S = 10


def _run_probe_process(root, request, adapter, cap):
    import dataclasses
    import signal
    import subprocess
    import sys
    import tempfile

    import peeky

    # Isolated interpreter importing this exact Peeky by absolute path, run outside the
    # target so a target-local `peeky` package or relative PYTHONPATH cannot interfere.
    package_parent = str(Path(peeky.__file__).resolve().parents[1])
    bootstrap = (
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        "from peeky.cli import main; sys.exit(main(sys.argv[1:]))"
    )
    # -B: a remote probe never writes bytecode into the worker's root.
    command = [sys.executable, "-I", "-B", "-c", bootstrap, package_parent, "probe", str(root)]
    # The client records evidence; the worker host keeps none (stateless probes).
    command += ["--json", "--quiet", "--no-save"]
    command += ["--at", f"{request.program}:{request.line}", "--hits", str(request.hits)]
    command += ["--timeout", str(request.timeout), "--until", request.until]
    command += ["--set-hit", str(request.set_hit)]
    if request.entry:
        command += ["--run", request.entry]
    for argument in request.args:
        command += [f"--arg={argument}"]
    for role, digest in request.expect_sha256:
        command += [f"--expect-sha256={role}={digest}"]
    if request.capture:
        command += ["--capture", ",".join(request.capture)]
    for name, value in request.set:
        command += ["--set", f"{name}={value}"]
    for line, hit, name, value in request.set_at:
        command += ["--set-at", f"{line}@{hit}", f"{name}={value}"]
    with tempfile.TemporaryDirectory(prefix="peeky-probe-") as scratch:
        if request.edit:
            from peeky.probe import edit_pairs

            # Files, not arguments: edit texts keep their newlines and stay off `ps`.
            for number, pair in enumerate(edit_pairs(request.edit)):
                for flag, text in zip(("--old-file", "--new-file"), pair, strict=True):
                    path = Path(scratch) / f"{flag.strip('-')}-{number}"
                    path.write_bytes(text.encode())
                    path.chmod(0o600)
                    command += [flag, str(path)]
        if request.expect_inputs is not None:
            expected = Path(scratch) / "expect-inputs.json"
            expected.write_text(json.dumps(dict(request.expect_inputs)))
            expected.chmod(0o600)
            command += ["--expect-inputs-file", str(expected)]
        if adapter is not None:
            config = Path(scratch) / "target.json"
            config.write_text(json.dumps(dataclasses.asdict(adapter), default=str))
            config.chmod(0o600)
            command += ["--config", str(config)]
        # A remote Python run's bytecode cache, removed with the scratch whatever ends the
        # child (#249).
        pycache = Path(scratch) / "pycache"
        pycache.mkdir(mode=0o700)
        command += ["--pycache-dir", str(pycache)]
        marker = Path(scratch) / "launch-requested"
        command += ["--launch-budget", str(cap - PROBE_LAUNCH_MARGIN_S)]
        command += ["--launch-marker", str(marker)]
        env = {k: v for k, v in os.environ.items() if k != "DEBUG_TOKEN"}
        process = subprocess.Popen(
            command,
            cwd=scratch,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=cap - 5)
        except subprocess.TimeoutExpired:
            # SIGTERM lets the probe close its debugger and debuggee; force at the cap.
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            stdout, stderr = None, b""
        # Whether the debugger was asked to start the program: an answer made up here
        # says so (#190 review), as the child's own would.
        requested = marker.exists()
    if stdout is None:
        before = "" if requested else " before the debugger's launch/attach request was sent"
        return _probe_failure(
            "timeout", TimeoutError(f"Probe exceeded the {cap} s HTTP cap{before}"), requested
        )
    if len(stdout) > 16 * 1024 * 1024:
        return _probe_failure(
            "failed", ValueError("Probe result exceeds limit of 16 MiB"), requested
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        message = stderr[-2000:].decode(errors="replace") or "Probe process failed"
        return _probe_failure("failed", RuntimeError(message), requested)


def _probe_failure(status, exc, requested=True):
    """A probe child's failure as its result; ``requested``: whether the debugger was
    asked to start the program (if not, it is one before launch)."""
    from peeky.errors import classify

    failure = {"status": status, "error": classify(exc, session_closed=True)}
    if not requested:
        failure["refused_before_launch"] = True
    return failure


def load_java_target(path):
    """Load a fixed trusted target; request bodies never supply this configuration."""
    from peeky.java import JavaTarget

    path = Path(path)
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise ValueError("Java configuration must be an object")
    if set(config) - {"bundle", "sources", "main_class", "java", "javac", "arguments"}:
        raise ValueError("Unknown Java configuration field")
    if not {"bundle", "sources", "main_class"} <= set(config):
        raise ValueError("Java configuration requires bundle, sources and main_class")
    if not isinstance(config["bundle"], str) or not config["bundle"]:
        raise ValueError("Java bundle must be a nonempty path string")
    if not isinstance(config["sources"], list) or not isinstance(config.get("arguments", []), list):
        raise ValueError("Java sources and arguments must be JSON arrays")
    config["bundle"] = (path.resolve().parent / config["bundle"]).resolve()
    config["sources"] = tuple(config["sources"])
    config["arguments"] = tuple(config.get("arguments", []))
    return JavaTarget(**config)


def load_native_target(path):
    """Load trusted native tools and inputs; HTTP clients cannot choose executables."""
    from peeky.native import NativeTarget

    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict):
        raise ValueError("Native configuration must be an object")
    if set(config) - {
        "sources",
        "language",
        "headers",
        "compiler",
        "adapter",
        "dwarfdump",
        "dsymutil",
        "sysroot",
        "arguments",
        "optimization",
    }:
        raise ValueError("Unknown native configuration field")
    if "sources" not in config:
        raise ValueError("Native configuration requires sources")
    for key in ("sources", "headers", "arguments"):
        value = config.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"Native {key} must be a JSON array")
        config[key] = tuple(value)
    return NativeTarget(**config)


def load_rust_target(path):
    """Load trusted Rust tools and inputs; HTTP clients cannot choose executables."""
    from peeky.rust import RustTarget

    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict):
        raise ValueError("Rust configuration must be an object")
    fields = {
        "sources",
        "tests",
        "cargo",
        "rustc",
        "adapter",
        "dwarfdump",
        "linker",
        "dsymutil",
        "sysroot",
        "arguments",
        "rust_version",
        "opt_level",
    }
    if set(config) - fields:
        raise ValueError("Unknown Rust configuration field")
    if not {"sources", "tests", "cargo", "rustc", "adapter", "dwarfdump", "linker"} <= set(config):
        raise ValueError("Rust configuration requires sources, tests and explicit tool paths")
    for key in ("sources", "tests", "arguments"):
        value = config.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"Rust {key} must be a JSON array")
        config[key] = tuple(value)
    return RustTarget(**config)


def load_java_attach_target(path):
    """Trusted attach configuration: loopback port, declared sources and class roots."""
    from peeky.java_attach import JavaAttachTarget

    path = Path(path)
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise ValueError("Java attach configuration must be an object")
    config.pop("language", None)
    allowed = {"bundle", "port", "sources", "class_roots", "java", "host", "timeout_ms"}
    if set(config) - allowed or not {"bundle", "port", "sources", "class_roots"} <= set(config):
        raise ValueError("Java attach configuration needs bundle, port, sources and class_roots")
    for key in ("sources", "class_roots"):
        if not isinstance(config[key], list):
            raise ValueError(f"Java attach {key} must be a JSON array")
        config[key] = tuple(config[key])
    config["bundle"] = (path.resolve().parent / config["bundle"]).resolve()
    return JavaAttachTarget(**config)


def main():
    from peeky.processes import keep_children_waitable

    keep_children_waitable()  # #170: an inherited ignored SIGCHLD reaps adapters
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=300,
        help="Close a session (and its debuggee) after this many idle seconds (default 300)",
    )
    parser.add_argument(
        "--exit-when-idle",
        action="store_true",
        help="Also exit once idle with no session (workers started by `peeky session start`)",
    )
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument(
        "--java-config", type=Path, help="Trusted fixed Java target JSON configuration"
    )
    targets.add_argument(
        "--native-config", type=Path, help="Trusted fixed C/C++ target JSON configuration"
    )
    targets.add_argument(
        "--rust-config", type=Path, help="Trusted fixed Cargo binary JSON configuration"
    )
    targets.add_argument(
        "--java-attach-config", type=Path, help="Trusted loopback JVM attach configuration"
    )
    args = parser.parse_args()
    token = os.environ.get("DEBUG_TOKEN")
    if not token or len(token) < 16:
        parser.error("Set DEBUG_TOKEN to a random secret of at least 16 characters")
    adapter = None
    if args.java_config or args.native_config or args.rust_config or args.java_attach_config:
        try:
            if args.java_attach_config:
                adapter = load_java_attach_target(args.java_attach_config)
            elif args.java_config:
                adapter = load_java_target(args.java_config)
            elif args.native_config:
                adapter = load_native_target(args.native_config)
            else:
                adapter = load_rust_target(args.rust_config)
        except (OSError, ValueError, TypeError) as exc:
            parser.error(str(exc))
    server = make_server(args.root, token, args.port, adapter=adapter)

    if not 1 <= args.idle_timeout <= 86400:
        parser.error("--idle-timeout must be within [1, 86400] seconds")

    def expire():
        while True:
            time.sleep(min(5, args.idle_timeout / 2))
            with server.worker.lock:
                idle = time.monotonic() - server.worker.last_used
                if server.worker.dap and idle > args.idle_timeout:
                    server.worker.close()
                    # The close counts as activity; a private worker exits right away.
                    if args.exit_when_idle:
                        idle = args.idle_timeout + 1
                if args.exit_when_idle and not server.worker.dap and idle > args.idle_timeout:
                    threading.Thread(target=server.shutdown, daemon=True).start()
                    return

    def guard():
        while True:
            time.sleep(0.25)
            server.worker.guard_pause()

    def terminate(signum, frame):
        # Unwind serve_forever so `finally` closes the debugger and the debuggee; the
        # adapter runs in its own process session, so a group kill would orphan it.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    threading.Thread(target=expire, daemon=True).start()
    threading.Thread(target=guard, daemon=True).start()
    print(
        f"Debugger worker: http://127.0.0.1:{server.server_port}; root={Path(args.root).resolve()}",
        flush=True,
    )
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.worker.close()
        server.server_close()


if __name__ == "__main__":
    main()
