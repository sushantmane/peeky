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


def _resolve(worker, stop, snapshot, segments):
    matches = [
        variable
        for scope in snapshot["scopes"]
        for variable in scope["variables"]
        if variable["name"] == segments[0]
    ]
    if not matches:
        raise ValueError("not found in the inspected scopes")
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


def run_probe(root, probe, *, adapter=None):
    """Run one self-contained probe; the worker is always closed before returning."""
    worker = DebugWorker(root, adapter=adapter)
    started = time.monotonic()
    deadline = started + probe.timeout
    result = {"probe": {**asdict(probe), "set": [list(change) for change in probe.set]}}
    rows, changes, hits = [], [], 0
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
        status = "timeout"
        while (remaining := deadline - time.monotonic()) > 0:
            state = worker.dispatch("wait", {**session, "timeout": min(2, remaining)})
            if state["state"] == "running":
                continue
            if state["state"] == "terminated":
                status = "completed" if hits else "no-stop"
                break
            hits += 1
            stop = {**session, "stop_id": state["stop_id"]}
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
                        raise ValueError(f"Intervention target {name!r} is not a visible local")
                    changes.append(
                        worker.dispatch(
                            "set_variable",
                            {**stop, "reference": scope["reference"], "name": name, "value": value},
                        )
                    )
                rows.extend(_capture(worker, stop, probe.capture, hits, "after-set"))
            if len(rows) > MAX_ROWS:
                raise ValueError("Probe row budget exceeded")
            if hits >= probe.hits:
                worker.dispatch(
                    "breakpoints", {**session, "program": probe.program, "breakpoints": []}
                )
            worker.dispatch("continue", stop)
        report = worker.dispatch("report", session)
        result.update(
            status=status,
            output=report["output"],
            output_truncated=report["output_truncated"],
        )
        if status == "no-stop":
            result["error"] = (
                f"The program finished without stopping at {probe.program}:{probe.line}; "
                "check `peeky describe` for executable lines"
            )
    except Exception as exc:
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        worker.close()
    result.update(
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
    if result.get("error"):
        lines.append(f"error: {result['error']}")
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
