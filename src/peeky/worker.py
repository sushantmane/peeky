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

from peeky.adapters import PythonAdapter, ViewBudgetExhausted
from peeky.dap import DAP, ResponseTimeout
from peeky.errors import _RULES, PROTOCOL_VERSION, classify

VIEW_SECONDS = 10


OPERATIONS = frozenset(
    {
        "launch",
        "wait",
        "inspect",
        "expand",
        "set_variable",
        "breakpoints",
        "continue",
        "report",
        "close",
        "probe",
        "shutdown",
        "version",
    }
)


class DebugWorker:
    def __init__(self, root, adapter=None):
        self.root = Path(root).resolve()
        # Adapter configuration is trusted startup input, never HTTP request data.
        self.adapter = adapter if adapter is not None else PythonAdapter()
        self.runtime = None
        self.capabilities = {}
        self.dap = None
        self.state = "idle"
        self.session = None
        self.stop_id = None
        self.thread_id = None
        self.frames = set()
        self.references = set()
        self.reference_frames = {}
        self.reference_adapter = {}
        self.reference_counter = 0
        self.children = {}
        self.inspection_nodes = 0
        self.view_fetches = 0
        self.view_items = 0
        self.view_bytes = 0
        self.view_deadline = 0.0
        self.view_timed_out = False
        self.audit = []
        self.breakpoint_events = []
        self.lock = threading.Lock()
        self.last_used = time.monotonic()
        # Optional per-session pause budget: a stop older than this is auto-resumed so
        # a slow client cannot stall a program with real-time deadlines.
        self.max_pause = None
        self.stopped_at = None
        self.auto_resumed = []
        self.cursor = 0
        self.stop_location = None
        # Below Client.call's 45 s HTTP timeout: a launch either returns within this
        # bound or is discarded, so an abandoned slow launch cannot leave a session.
        self.launch_deadline_s = 40

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

    def read_location(self):
        """Best-effort top-frame location for display; never evidence, never fatal.

        Crash or signal stops may fail runtime validation; that belongs to `inspect`,
        so a failed read here only omits the location.
        """
        try:
            stack = self.dap.request(
                "stackTrace", {"threadId": self.thread_id, "levels": 1}, timeout=2
            )
            frame = (stack.get("stackFrames") or [{}])[0]
            path = frame.get("source", {}).get("path")
            return f"{path}:{frame.get('line')}" if path else None
        except Exception:
            return None

    def location(self):
        return {"location": self.stop_location} if self.stop_location else {}

    def pause_status(self):
        if self.state != "stopped" or self.stopped_at is None:
            return {}
        paused = round(time.monotonic() - self.stopped_at, 2)
        status = {"paused_s": paused}
        if self.max_pause:
            status["resume_in_s"] = round(max(0.0, self.max_pause - paused), 2)
        return status

    def guard_pause(self):
        """Auto-resume a stop that outlived the session's pause budget.

        Called periodically by the worker server. Any failure releases the program by
        closing the session (detaching an attached JVM) rather than leaving it paused.
        """
        with self.lock:
            if not self.dap or not self.max_pause:
                return
            try:
                if self.state == "running":
                    received = self.dap.pending_stop(self.cursor)
                    if received is None or time.monotonic() - received < self.max_pause:
                        return
                    self._dispatch("wait", {"timeout": 0.1})
                if self.state != "stopped" or self.stopped_at is None:
                    return
                paused = time.monotonic() - self.stopped_at
                if paused < self.max_pause:
                    return
                stop_id = self.stop_id
                self._dispatch("continue", {"stop_id": stop_id})
                self.auto_resumed.append({"stop_id": stop_id, "paused_s": round(paused, 2)})
                self.auto_resumed = self.auto_resumed[-100:]
                self.audit.append(
                    {
                        "time": time.time(),
                        "operation": "auto_resume",
                        "arguments": {"stop_id": stop_id},
                        "result": {"paused_s": round(paused, 2), "max_pause": self.max_pause},
                    }
                )
                self.audit = self.audit[-500:]
            except Exception:
                import sys
                import traceback

                print("peeky: pause guard failed; closing the session", file=sys.stderr)
                traceback.print_exc()
                self.close()

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
            "expand",
            "set_variable",
            "breakpoints",
            "continue",
            "report",
            "close",
        }:
            raise ValueError("Unknown operation")
        if operation == "launch" and args.get("max_pause") is not None:
            value = args["max_pause"]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 300:
                raise ValueError("max_pause must be a number of seconds within (0, 300]")
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
        if operation == "expand":
            for key, default, low, high in (
                ("handle", None, 1, None),
                ("start", 0, 0, 511),
                ("count", 64, 1, 64),
            ):
                value = args.get(key, default)
                if type(value) is not int or value < low or (high is not None and value > high):
                    raise ValueError(f"Invalid expansion {key}")
        if operation == "set_variable":
            if type(args.get("reference")) is not int:
                raise ValueError("reference must be an integer")
            if not isinstance(args.get("name"), str) or not isinstance(args.get("value"), str):
                raise ValueError("name and value must be strings")

    def _dispatch(self, operation, args):
        runtime = self.runtime or self.adapter
        if operation in {
            "wait",
            "inspect",
            "expand",
            "set_variable",
            "breakpoints",
            "continue",
            "report",
        }:
            runtime.assert_current()
        if operation == "launch":
            if self.dap:
                raise ValueError("Close the current session before launching another")
            program = self.path(args["program"])
            self.adapter.validate_breakpoints(args.get("breakpoints", []))
            self.session = uuid.uuid4().hex
            self.audit = []
            self.breakpoint_events = []
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
                if getattr(self.adapter, "supports_deadline", False):
                    prepared = self.adapter.prepare(program, self.root, deadline=deadline)
                else:
                    prepared = self.adapter.prepare(program, self.root)
                self.runtime = runtime = prepared
                runtime.assert_current()
                left()
                self.dap = DAP(command=runtime.command, env=getattr(runtime, "environment", None))
                capabilities = self.dap.request(
                    "initialize", runtime.initialize_args(), timeout=left()
                )
                self.capabilities = runtime.normalize_capabilities(capabilities)
                launch = self.dap.send(
                    getattr(runtime, "dap_request", "launch"),
                    runtime.launch_args(program, self.root),
                    timeout=left(5),
                )
                self.dap.wait_event({"initialized"}, failed_request=launch, timeout=left())
                points = self.dap.request(
                    "setBreakpoints",
                    {
                        "source": {"path": str(runtime.source_path(program))},
                        "breakpoints": args.get("breakpoints", []),
                    },
                    timeout=left(),
                )
                self.dap.request("configurationDone", timeout=left())
                self.dap.response(launch, timeout=left())
                runtime.assert_current()
                left()
                self.state = "running"
                self.cursor = 0
                return {
                    "session_id": self.session,
                    "state": self.state,
                    "breakpoints": points.get("breakpoints", []),
                    "source_sha256": hashlib.sha256(program.read_bytes()).hexdigest(),
                    "supports_set_variable": self.capabilities.get("supports_set_variable", False),
                    "supports_expand": True,
                    "protocol": PROTOCOL_VERSION,
                    "artifact_manifest_id": runtime.manifest_id,
                    "source_manifest": getattr(runtime, "source_manifest", None),
                    "target_identity": getattr(runtime, "target_identity", None),
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
            if self.state in ("stopped", "terminated"):
                return {
                    "state": self.state,
                    "stop_id": self.stop_id,
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
                    return {"state": self.state, "timed_out": True, **updates}
                self.cursor = event["cursor"]
                if event["event"] != "breakpoint":
                    break
                self.breakpoint_events.append(event.get("body", {}))
                self.breakpoint_events = self.breakpoint_events[-100:]
                updates["breakpoint_events"] = list(self.breakpoint_events)
                if time.monotonic() >= deadline:
                    return {"state": self.state, "timed_out": True, **updates}
            # Rebuilt classes or edited sources while running invalidate the evidence.
            runtime.assert_current()
            self.state = event["event"]
            self.frames.clear()
            self.references.clear()
            self.reference_frames.clear()
            self.reference_adapter.clear()
            self.children.clear()
            self.stop_id = uuid.uuid4().hex if self.state == "stopped" else None
            self.stopped_at = event.get("received", time.monotonic()) if self.stop_id else None
            self.thread_id = event.get("body", {}).get("threadId")
            self.stop_location = self.read_location() if self.stop_id else None
            return {
                "state": self.state,
                "stop_id": self.stop_id,
                "event": event,
                **self.location(),
                **self.pause_status(),
                **updates,
            }
        if operation == "inspect":
            self.stopped(args)
            self.children.clear()
            self.inspection_nodes = 0
            self.view_fetches = self.view_items = self.view_bytes = 0
            self.view_deadline = time.monotonic() + VIEW_SECONDS
            self.view_timed_out = False
            validate_children = getattr(runtime, "validate_children", None)
            # A timed-out view read may still be processed by the adapter; until its
            # reply drains, this inspection is read-only.
            drained = getattr(self.dap, "drained", None)
            quiescent = drained(1) if drained else True
            stack = self.dap.request("stackTrace", {"threadId": self.thread_id, "levels": 10})
            frames = stack.get("stackFrames", [])[:10]
            self.frames = {f["id"] for f in frames}
            selected = args.get("frame_id", frames[0]["id"] if frames else None)
            if selected not in self.frames:
                raise ValueError("Unknown frame")
            selected_frame = next(frame for frame in frames if frame["id"] == selected)
            runtime.validate_stop(selected_frame)
            if validate_debugger := getattr(runtime, "validate_debugger", None):
                validate_debugger(self.dap, selected_frame)
            # Adapters may reuse raw scope IDs for different selected frames.
            # A new inspection revokes all earlier client scope handles.
            self.references.clear()
            self.reference_frames.clear()
            self.reference_adapter.clear()
            scopes = self.dap.request("scopes", {"frameId": selected}).get("scopes", [])
            pending = []
            for scope in runtime.local_scopes(scopes)[:16]:
                variables = self.dap.request(
                    "variables", {"variablesReference": scope["variablesReference"]}
                ).get("variables", [])
                if validate_children:
                    validate_children(variables)
                self.reference_counter += 1
                handle = self.reference_counter
                exposed = self.expose_variables(
                    variables[: min(50, 512 - self.inspection_nodes)],
                    selected_frame,
                    self.scope_identity(scope),
                    (),
                )
                pending.append((handle, scope, exposed))
            references = [scope["variablesReference"] for _, scope, _ in pending]
            writable = quiescent and not self.view_timed_out
            if self.view_fetches and writable:
                # Value views issued nested reads that may change adapter scope context.
                # Bind mutation authority only to freshly re-acquired, identical scopes.
                references = self.reacquire_scopes(runtime, selected_frame, pending)
            result = []
            for (handle, scope, exposed), reference in zip(pending, references):
                if writable:
                    self.references.add(handle)
                    self.reference_frames[handle] = selected_frame
                    self.reference_adapter[handle] = reference
                result.append({"name": scope["name"], "reference": handle, "variables": exposed})
            snapshot = {
                "stop_id": self.stop_id,
                "frames": frames,
                "scopes": result,
                **self.pause_status(),
            }
            if not writable:
                snapshot["mutation_blocked"] = "A value-view read is still pending; inspect again"
            return snapshot
        if operation == "expand":
            return self.expand(args, runtime)
        if operation == "set_variable":
            self.stopped(args)
            self.children.clear()
            if not self.capabilities.get("supports_set_variable", False):
                raise ValueError("Adapter does not support variable mutation")
            ref = args["reference"]
            if ref not in self.references:
                raise ValueError("Inspect the scope before changing variables")
            adapter_ref = self.reference_adapter[ref]
            runtime.validate_value(args["value"])
            current = self.dap.request("variables", {"variablesReference": adapter_ref}).get(
                "variables", []
            )
            matches = [v for v in current if v.get("name") == args["name"]]
            if len(matches) != 1:
                raise ValueError("Expected exactly one existing local variable")
            variable = matches[0]
            if validate_variable := getattr(runtime, "validate_variable", None):
                validate_variable(variable, args["value"])
            if validate_debugger := getattr(runtime, "validate_debugger", None):
                frame = self.reference_frames.get(ref)
                if frame is None:
                    raise ValueError("Inspect the scope before changing variables")
                runtime.validate_stop(frame)
                validate_debugger(self.dap, frame)
            before = variable.get("value")
            changed = self.dap.request(
                "setVariable",
                {
                    "variablesReference": adapter_ref,
                    "name": args["name"],
                    "value": args["value"],
                },
            )
            runtime.assert_current()
            normalize_value = getattr(runtime, "mutation_value", None)
            after = normalize_value(changed) if normalize_value else changed.get("value")
            return {"name": args["name"], "before": before, "after": after}
        if operation == "breakpoints":
            runtime.validate_breakpoints(args["breakpoints"])
            return self.dap.request(
                "setBreakpoints",
                {
                    "source": {"path": str(runtime.source_path(self.path(args["program"])))},
                    "breakpoints": args["breakpoints"],
                },
            )
        if operation == "continue":
            self.stopped(args)
            self.dap.request("continue", {"threadId": self.thread_id})
            self.state = "running"
            self.stopped_at = None
            self.stop_location = None
            self.children.clear()
            self.stop_id = None
            self.frames.clear()
            self.references.clear()
            self.reference_frames.clear()
            self.reference_adapter.clear()
            return {"state": self.state}
        if operation == "report":
            output, truncated = self.dap.snapshot_output()
            return {
                "session_id": self.session,
                "state": self.state,
                "output": output,
                "output_truncated": truncated,
                "history": [e for e in self.audit if e["operation"] != "report"],
                "artifact_manifest_id": runtime.manifest_id,
                "breakpoint_events": list(self.breakpoint_events),
                "debugger_events": self.dap.recent_events(),
            }
        if operation == "close":
            self.close()
            return {"state": self.state}
        raise ValueError("Unknown operation")

    @staticmethod
    def scope_identity(scope):
        return (scope.get("name"), scope.get("presentationHint"))

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
        fresh = runtime.local_scopes(
            self.dap.request("scopes", {"frameId": frame["id"]}).get("scopes", [])
        )[:16]
        original = [self.scope_identity(scope) for _, scope, _ in pending]
        if len(set(original)) != len(original):
            raise ValueError("Duplicate scope identities after value views; inspect again")
        if sorted(map(repr, (self.scope_identity(s) for s in fresh))) != sorted(
            map(repr, original)
        ) or len(fresh) != len(original):
            raise ValueError("Scopes changed during value views; inspect again")
        references = {self.scope_identity(s): s["variablesReference"] for s in fresh}
        return [references[identity] for identity in original]

    def view_fetch(self, reference):
        """Bounded raw reads used only by a runtime's trusted value-view hook.

        Count, time, item and byte budgets are shared by one inspection or expansion;
        exhaustion makes remaining views opaque instead of extending the request.
        """
        remaining = self.view_deadline - time.monotonic()
        if self.view_fetches >= 128 or remaining <= 0:
            raise ViewBudgetExhausted("Value view budget exhausted")
        self.view_fetches += 1
        if type(reference) is not int or reference <= 0:
            raise ValueError("Variable is not expandable")
        try:
            values = self.dap.request(
                "variables",
                {"variablesReference": reference, "start": 0, "count": 65},
                timeout=remaining,
            ).get("variables", [])
        except ResponseTimeout as exc:
            # The read was written completely; only its reply missed the view budget.
            self.view_timed_out = True
            raise ViewBudgetExhausted("Value view budget exhausted") from exc
        if not isinstance(values, list) or len(values) > 64:
            raise ValueError("Value view child collection exceeds limit")
        self.view_items += len(values)
        self.view_bytes += len(json.dumps(values, default=str))
        if self.view_items > 1024 or self.view_bytes > 1024 * 1024:
            raise ViewBudgetExhausted("Value view budget exhausted")
        return values

    def expose_variables(self, variables, frame, scope, ancestry):
        """Only worker-issued handles leave the process; child handles never permit writes."""
        result = []
        view_variable = getattr(self.runtime, "view_variable", None)
        for variable in variables:
            name, kind = variable.get("name"), variable.get("type")
            if not isinstance(name, str) or (kind is not None and not isinstance(kind, str)):
                raise RuntimeError("Malformed debugger variable identity")
            if len(name) > 2000 or (kind is not None and len(kind) > 2000):
                raise RuntimeError("Debugger variable identity exceeds limit")
            view = view_variable(variable, self.view_fetch) if view_variable else None
            if view is not None:
                # Interpreted values never carry expansion handles to raw storage.
                output = {
                    "name": name,
                    "value": str(view["value"])[:2000],
                    "variablesReference": 0,
                    "view": str(view["rust_view"])[:100],
                }
                if kind is not None:
                    output["type"] = kind
                result.append(output)
                self.inspection_nodes += 1
                continue
            path = (*ancestry, (name, kind))
            handle = 0
            raw = variable.get("variablesReference", 0)
            if type(raw) is int and raw > 0 and len(path) < 4:
                self.reference_counter += 1
                handle = self.reference_counter
                self.children[handle] = (dict(frame), scope, path)
            output = {
                "name": name,
                "value": str(variable.get("value", ""))[:2000],
                "variablesReference": handle,
            }
            if kind is not None:
                output["type"] = kind
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
            if type(raw) is int and raw > 0 and len(path) >= 4:
                output["depth_limited"] = True
            result.append(output)
            self.inspection_nodes += 1
        return result

    def bounded_variables(self, reference):
        if type(reference) is not int or reference <= 0:
            raise ValueError("Variable is not expandable")
        values = self.dap.request(
            "variables", {"variablesReference": reference, "start": 0, "count": 513}
        ).get("variables", [])
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
        original_frame, scope_identity, ancestry = record
        frames = self.dap.request("stackTrace", {"threadId": self.thread_id, "levels": 10}).get(
            "stackFrames", []
        )[:10]
        matches = [f for f in frames if f.get("id") == original_frame.get("id")]
        if len(matches) != 1 or self.frame_identity(matches[0]) != self.frame_identity(
            original_frame
        ):
            raise ValueError("Expansion frame identity changed; inspect again")
        frame = matches[0]
        runtime.validate_stop(frame)
        if validate_debugger := getattr(runtime, "validate_debugger", None):
            validate_debugger(self.dap, frame)
        scopes = runtime.local_scopes(
            self.dap.request("scopes", {"frameId": frame["id"]}).get("scopes", [])
        )
        matches = [scope for scope in scopes if self.scope_identity(scope) == scope_identity]
        if len(matches) != 1:
            raise ValueError("Expansion scope is missing or ambiguous")
        reference = matches[0].get("variablesReference")
        validate_children = getattr(runtime, "validate_children", None)
        for name, kind in ancestry:
            variables = self.bounded_variables(reference)
            if validate_children:
                validate_children(variables)
            matches = [v for v in variables if v.get("name") == name]
            if len(matches) != 1 or matches[0].get("type") != kind:
                raise ValueError("Expansion ancestry changed or is ambiguous")
            reference = matches[0].get("variablesReference")
        variables = self.bounded_variables(reference)
        if validate_children:
            validate_children(variables)
        # Each expansion is its own bounded request with a fresh value-view budget.
        self.view_fetches = self.view_items = self.view_bytes = 0
        self.view_deadline = time.monotonic() + VIEW_SECONDS
        start = args.get("start", 0)
        count = min(args.get("count", 64), 512 - self.inspection_nodes)
        result = self.expose_variables(
            variables[start : start + count], frame, scope_identity, ancestry
        )
        runtime.assert_current()
        return {
            "variables": result,
            "start": start,
            "truncated": start + len(result) < len(variables),
            "remaining_nodes": 512 - self.inspection_nodes,
        }

    def close(self):
        adapter, self.dap = self.dap, None
        runtime, self.runtime = self.runtime, None
        self.state = "idle"
        self.stopped_at = None
        self.session = None
        self.stop_id = None
        self.thread_id = None
        self.capabilities = {}
        self.frames.clear()
        self.references.clear()
        self.reference_frames.clear()
        self.reference_adapter.clear()
        self.children.clear()
        try:
            if adapter:
                if runtime is not None and getattr(runtime, "terminate_debuggee", True) is False:
                    adapter.close(terminate=False)
                else:
                    adapter.close()
        finally:
            if runtime:
                runtime.close()


def make_server(root, token, port=8765, *, adapter=None):
    if not isinstance(token, str) or len(token) < 16:
        raise ValueError("Token must contain at least 16 characters")
    worker = DebugWorker(root, adapter=adapter)
    probes = threading.Semaphore(1)

    def probe(args):
        # Stateless and hard-bounded: a separate process with its own worker, killed at
        # the cap so an HTTP client timeout can never leave work holding the slot.
        from peeky.probe import HTTP_PROBE_SECONDS, Probe, parse_location

        if set(args) - {"at", "capture", "set", "hits", "timeout", "until"}:
            raise ValueError("Unknown probe field")
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
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
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
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 65536:
                    raise ValueError("Request body must be 1..65536 bytes")
                self.connection.settimeout(10)
                args = json.loads(self.rfile.read(size))
                if not isinstance(args, dict):
                    raise ValueError("Expected a JSON object")
                operation = self.path.removeprefix("/")
                if operation == "version":
                    result = {
                        "protocol": PROTOCOL_VERSION,
                        "operations": sorted(OPERATIONS),
                        "error_codes": sorted(
                            {rule[1] for rule in _RULES}
                            | {"DEBUGGER_ERROR", "INVALID_REQUEST", "UNAUTHORIZED"}
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
    command = [sys.executable, "-I", "-c", bootstrap, package_parent, "probe", str(root)]
    command += ["--json", "--quiet"]
    command += ["--at", f"{request.program}:{request.line}", "--hits", str(request.hits)]
    command += ["--timeout", str(request.timeout), "--until", request.until]
    if request.capture:
        command += ["--capture", ",".join(request.capture)]
    for name, value in request.set:
        command += ["--set", f"{name}={value}"]
    with tempfile.TemporaryDirectory(prefix="peeky-probe-") as scratch:
        if adapter is not None:
            config = Path(scratch) / "target.json"
            config.write_text(json.dumps(dataclasses.asdict(adapter), default=str))
            config.chmod(0o600)
            command += ["--config", str(config)]
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
            return _probe_failure("timeout", TimeoutError(f"Probe exceeded the {cap} s HTTP cap"))
    if len(stdout) > 16 * 1024 * 1024:
        return _probe_failure("failed", ValueError("Probe result exceeds limit of 16 MiB"))
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        message = stderr[-2000:].decode(errors="replace") or "Probe process failed"
        return _probe_failure("failed", RuntimeError(message))


def _probe_failure(status, exc):
    from peeky.errors import classify

    return {"status": status, "error": classify(exc, session_closed=True)}


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
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--port", type=int, default=8765)
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

    def expire():
        while True:
            time.sleep(5)
            with server.worker.lock:
                if server.worker.dap and time.monotonic() - server.worker.last_used > 300:
                    server.worker.close()

    def guard():
        while True:
            time.sleep(0.25)
            server.worker.guard_pause()

    threading.Thread(target=expire, daemon=True).start()
    threading.Thread(target=guard, daemon=True).start()
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
