"""Stateless one-request probes over the guarded worker engine (ADR 0004).

A probe is one complete transaction: prepare a fresh build, launch, stop at one
declared line, capture declared selectors, apply optional declared interventions,
run to exit or a bound, report and clean up. No session, stop or handle identity
leaves the call, so retrying is always a whole new run. Every read and write goes
through DebugWorker.dispatch, so session/stop identity, provenance checks, value
views and mutation rules are identical to session mode. Runtime interventions are
evidence about one execution, never a verified fix.
"""

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from peeky.worker import DebugWorker, load_java_target, load_native_target, load_rust_target

MAX_SELECTORS = 32
MAX_SETS = 8
MAX_HITS = 100
MAX_ROWS = 2000
MAX_SECONDS = 300
# HTTP probes must finish inside Client.call's 45 s timeout.
HTTP_PROBE_SECONDS = 40
_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}|\[[0-9]{1,6}\]")


def parse_selector(text):
    """`request.page` -> ['request', 'page']; `args[0]` -> ['args', '[0]']."""
    if not isinstance(text, str) or not text or len(text) > 512:
        raise ValueError("Selectors must be nonempty dotted paths")
    segments = []
    for part in text.split("."):
        pieces = re.findall(r"[^\[\]]+|\[[^\]]*\]", part)
        if not pieces or "".join(pieces) != part:
            raise ValueError(f"Invalid selector: {text}")
        segments.extend(pieces)
    if (
        len(segments) > 16
        or not all(_SEGMENT.fullmatch(segment) for segment in segments)
        or segments[0].startswith("[")
    ):
        raise ValueError(f"Invalid selector: {text}")
    return segments


def parse_location(text):
    """`src/main.rs:36` -> ('src/main.rs', 36)."""
    program, separator, line = (text or "").rpartition(":")
    if not separator or not program or not re.fullmatch(r"[1-9][0-9]{0,6}", line):
        raise ValueError("Location must be <relative-source-path>:<line>")
    return program, int(line)


@dataclass(frozen=True)
class Probe:
    program: str
    line: int
    capture: tuple[str, ...] = ()
    set: tuple[tuple[str, str], ...] = ()
    hits: int = 1
    timeout: float = 60

    def __post_init__(self):
        if not isinstance(self.program, str) or not self.program:
            raise ValueError("Probe program must be a relative source path")
        if type(self.line) is not int or self.line < 1:
            raise ValueError("Probe line must be a positive integer")
        if not isinstance(self.capture, tuple) or len(self.capture) > MAX_SELECTORS:
            raise ValueError(f"At most {MAX_SELECTORS} capture selectors")
        for selector in self.capture:
            parse_selector(selector)
        if not isinstance(self.set, tuple) or len(self.set) > MAX_SETS:
            raise ValueError(f"At most {MAX_SETS} interventions")
        for change in self.set:
            if (
                not isinstance(change, tuple)
                or len(change) != 2
                or not all(isinstance(part, str) for part in change)
                or len(parse_selector(change[0])) != 1
            ):
                raise ValueError("Interventions are (local-name, value) string pairs")
        if type(self.hits) is not int or not 1 <= self.hits <= MAX_HITS:
            raise ValueError(f"Probe hits must be 1..{MAX_HITS}")
        if type(self.timeout) not in (int, float) or not 0 < self.timeout <= MAX_SECONDS:
            raise ValueError(f"Probe timeout must be within (0, {MAX_SECONDS}] seconds")


def _visible(snapshot):
    names = sorted({v["name"] for scope in snapshot["scopes"] for v in scope["variables"]})
    shown = ", ".join(names[:40]) + (f", ... ({len(names)} total)" if len(names) > 40 else "")
    return shown or "(none)"


def _resolve(worker, stop, snapshot, segments):
    matches = [
        variable
        for scope in snapshot["scopes"]
        for variable in scope["variables"]
        if variable["name"] == segments[0]
    ]
    if not matches:
        raise ValueError(f"not found; visible: {_visible(snapshot)}")
    variable = matches[0]
    for segment in segments[1:]:
        # LLDB names elements `[1]`; debugpy names them `1`.
        names = {segment, segment[1:-1]} if segment.startswith("[") else {segment}
        handle = variable.get("variablesReference", 0)
        if not handle:
            raise ValueError(f"cannot expand {variable['name']!r} ({variable.get('view', 'leaf')})")
        found, start, incomplete = None, 0, False
        while found is None:
            page = worker.dispatch("expand", {**stop, "handle": handle, "start": start})
            found = next((v for v in page["variables"] if v["name"] in names), None)
            if found is not None or not page["truncated"]:
                break
            if start + 64 > 511 or page.get("remaining_nodes", 1) <= 0:
                incomplete = True
                break
            start += 64
        if found is None and incomplete:
            raise ValueError(f"{segment!r} is beyond the bounded inspection window (512 children)")
        if found is None:
            raise ValueError(f"{segment!r} not found")
        variable = found
    return variable


def _capture(worker, stop, selectors, hit, phase):
    rows = []
    for selector in selectors:
        row = {"hit": hit, "phase": phase, "selector": selector}
        # A fresh inspection per selector restores node budgets and handle validity.
        snapshot = worker.dispatch("inspect", stop)
        frame = snapshot["frames"][0] if snapshot["frames"] else {}
        row["line"] = frame.get("line")
        try:
            variable = _resolve(worker, stop, snapshot, parse_selector(selector))
        except ValueError as exc:
            row["error"] = str(exc)
        else:
            row.update(value=variable["value"], type=variable.get("type"))
            if "view" in variable:
                row["view"] = variable["view"]
        rows.append(row)
    return rows


def _expected_line(breakpoint, state, requested):
    """The adapter-resolved breakpoint line, including later breakpoint updates."""
    line = breakpoint.get("line", requested)
    for update in state.get("breakpoint_events", []):
        point = update.get("breakpoint", {})
        if point.get("id") == breakpoint.get("id") and type(point.get("line")) is int:
            line = point["line"]
    return line


def run_probe(root, probe, *, adapter=None, progress=None):
    """Run one self-contained probe; the worker is always closed before returning.

    ``progress(message)`` receives human-readable milestones (never parsed).
    """
    say = progress or (lambda message: None)
    worker = DebugWorker(root, adapter=adapter)
    started = time.monotonic()
    deadline = started + probe.timeout
    result = {"probe": {**asdict(probe), "set": [list(change) for change in probe.set]}}
    rows, changes, hits, warnings, relocated = [], [], 0, [], False
    try:
        launch = worker.dispatch(
            "launch", {"program": probe.program, "breakpoints": [{"line": probe.line}]}
        )
        session = {"session_id": launch["session_id"]}
        result.update(
            breakpoint=(launch["breakpoints"] or [{}])[0],
            artifact_manifest_id=launch["artifact_manifest_id"],
            target_identity=launch["target_identity"],
            source_sha256=launch["source_sha256"],
        )
        point = result["breakpoint"]
        verified = point.get("verified") is True
        say(
            f"started; breakpoint {probe.program}:{point.get('line', probe.line)} "
            + ("verified" if verified else "pending until its class or module loads")
        )
        say("waiting for the program to reach it...")
        status, noted = "timeout", time.monotonic()
        while (remaining := deadline - time.monotonic()) > 0:
            state = worker.dispatch("wait", {**session, "timeout": min(2, remaining)})
            if not verified and any(
                update.get("breakpoint", {}).get("verified") is True
                for update in state.get("breakpoint_events", [])
            ):
                verified = True
                say("breakpoint verified")
            if state["state"] == "running":
                if time.monotonic() - noted >= 10:
                    noted = time.monotonic()
                    phase = "for the next stop" if hits < probe.hits else "for the program to exit"
                    say(f"still waiting {phase} ({int(noted - started)} s elapsed)")
                continue
            if state["state"] == "terminated":
                status = "completed" if hits else "no-stop"
                say("program finished")
                break
            stop = {**session, "stop_id": state["stop_id"]}
            # Only the requested breakpoint counts; a crash, signal or other stop fails
            # closed before any capture or intervention.
            expected = _expected_line(result["breakpoint"], state, probe.line)
            snapshot = worker.dispatch("inspect", stop)
            frame = snapshot["frames"][0] if snapshot["frames"] else {}
            reason = state.get("event", {}).get("body", {}).get("reason")
            path = str(frame.get("source", {}).get("path", "")).replace("\\", "/")
            if (
                reason not in (None, "breakpoint")
                or frame.get("line") != expected
                or not (path == probe.program or path.endswith("/" + probe.program))
            ):
                raise ValueError(
                    f"Stopped at {path}:{frame.get('line')} ({reason}), not the requested "
                    f"{probe.program}:{expected}"
                )
            hits += 1
            if expected != probe.line and not relocated:
                relocated = True
                warnings.append(
                    f"The debugger moved the breakpoint from line {probe.line} to line "
                    f"{expected} (line {probe.line} has no code of its own); values are from "
                    f"line {expected}. Choose a line from `peeky describe`."
                )
                say(warnings[-1])
            say(f"hit {hits}/{probe.hits} at {probe.program}:{expected}; capturing")
            rows.extend(_capture(worker, stop, probe.capture, hits, "observed"))
            if hits == 1 and probe.set:
                for name, value in probe.set:
                    snapshot = worker.dispatch("inspect", stop)
                    scope = next(
                        (
                            s
                            for s in snapshot["scopes"]
                            if any(v["name"] == name for v in s["variables"])
                        ),
                        None,
                    )
                    if scope is None:
                        raise ValueError(
                            f"Intervention target {name!r} is not a visible local; "
                            f"visible: {_visible(snapshot)}"
                        )
                    changes.append(
                        worker.dispatch(
                            "set_variable",
                            {**stop, "reference": scope["reference"], "name": name, "value": value},
                        )
                    )
                    say(f"set {name}: {changes[-1].get('before')} -> {changes[-1].get('after')}")
                rows.extend(_capture(worker, stop, probe.capture, hits, "after-set"))
            if len(rows) > MAX_ROWS:
                raise ValueError("Probe row budget exceeded")
            if hits >= probe.hits:
                worker.dispatch(
                    "breakpoints", {**session, "program": probe.program, "breakpoints": []}
                )
                say("all requested hits captured; letting the program finish")
            worker.dispatch("continue", stop)
        report = worker.dispatch("report", session)
        result.update(
            status=status,
            output=report["output"],
            output_truncated=report["output_truncated"],
        )
        if status == "timeout":
            from peeky.errors import classify

            result["error"] = classify(
                TimeoutError(f"Probe exceeded its {probe.timeout} s timeout"), session_closed=True
            )
        if status == "no-stop":
            result["error"] = {
                "code": "NO_STOP",
                "message": f"The program finished without stopping at {probe.program}:"
                f"{probe.line}; check `peeky describe` for executable lines",
                "hint": "Pick a line the program executes; see `peeky describe`.",
                "retryable": False,
                "session_closed": True,
            }
        elif status == "completed" and hits < probe.hits:
            warnings.append(
                f"Only {hits} of {probe.hits} requested hits occurred before the program exited."
            )
    except Exception as exc:
        from peeky.errors import classify

        result.update(status="failed", error=classify(exc, session_closed=True))
    finally:
        worker.close()
    if changes and probe.hits > 1:
        warnings.append(
            "Interventions are applied once, at hit 1; later hits observe without re-applying."
        )
    result.update(
        warnings=warnings,
        hits=hits,
        rows=rows,
        interventions=changes,
        seconds=round(time.monotonic() - started, 3),
        note="Interventions are runtime evidence for this run only, not a verified fix.",
    )
    return result


def _table(result):
    lines = []
    header = ("hit", "phase", "line", "selector", "type", "value")
    rows = [
        (
            str(row["hit"]),
            row["phase"],
            str(row.get("line", "")),
            row["selector"],
            row.get("type") or "",
            row.get("value", "<" + row.get("error", "") + ">")
            + (f"  [{row['view']}]" if row.get("view") else ""),
        )
        for row in result["rows"]
    ]
    widths = [max(len(item[i]) for item in [header, *rows]) for i in range(len(header) - 1)]
    for item in [header, *rows]:
        lines.append(
            "  ".join(text.ljust(width) for text, width in zip(item, widths)) + "  " + item[-1]
        )
    for change in result["interventions"]:
        lines.append(f"set {change['name']}: {change['before']} -> {change['after']}")
    lines.append(
        f"status: {result['status']}  hits: {result['hits']}  seconds: {result['seconds']}"
    )
    for warning in result.get("warnings", ()):
        lines.append(f"warning: {warning}")
    if error := result.get("error"):
        lines.append(f"error [{error['code']}]: {error['message']}")
        lines.append(f"hint: {error['hint']}")
    if result.get("output"):
        lines.append("output:")
        lines.extend("  " + line for line in result["output"].splitlines()[-20:])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="peeky-probe",
        description="One-shot stateless probe: stop at a line, capture values, optionally "
        "change locals, run to completion and report. Example: peeky-probe --root examples "
        "--at pagination_bug.py:7 --capture page,page_size --set page=0",
    )
    parser.add_argument("--root", default=".", help="Source root containing the program")
    parser.add_argument("--at", required=True, help="Probe location <path>:<line>")
    parser.add_argument("--capture", default="", help="Comma-separated selectors, e.g. a,b.c")
    parser.add_argument(
        "--set", action="append", default=[], help="Local intervention name=value (repeatable)"
    )
    parser.add_argument("--hits", type=int, default=1, help="Stops to capture (default 1)")
    parser.add_argument("--timeout", type=float, default=60, help="Overall seconds (default 60)")
    parser.add_argument("--json", action="store_true", help="Print the full JSON result")
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument("--java-config", type=Path)
    targets.add_argument("--native-config", type=Path)
    targets.add_argument("--rust-config", type=Path)
    args = parser.parse_args(argv)
    try:
        program, line = parse_location(args.at)
        changes = []
        for item in args.set:
            name, separator, value = item.partition("=")
            if not separator:
                raise ValueError("--set expects name=value")
            changes.append((name.strip(), value.strip()))
        probe = Probe(
            program,
            line,
            tuple(s.strip() for s in args.capture.split(",") if s.strip()),
            tuple(changes),
            args.hits,
            args.timeout,
        )
        adapter = None
        if args.java_config:
            adapter = load_java_target(args.java_config)
        elif args.native_config:
            adapter = load_native_target(args.native_config)
        elif args.rust_config:
            adapter = load_rust_target(args.rust_config)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    result = run_probe(args.root, probe, adapter=adapter)
    print(json.dumps(result, indent=2) if args.json else _table(result))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
