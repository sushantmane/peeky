"""CUDA phase 0 qualification (issue #61, ADR 0007): run on a Linux host with an NVIDIA GPU.

It records the toolchain, then drives cuda-gdb through the Debug Adapter Protocol on a
known kernel bug (examples/cuda/scale_bug.cu) and checks what Peeky needs:
- cuda-gdb has Python (GDB's DAP is written in Python);
- a kernel breakpoint stops;
- CUDA focus moves to a chosen block and thread, and DAP frames follow it;
- DAP locals there match the CLI's;
- a block that does not exist leaves the focus unchanged;
- the program continues to its output.

It also runs Compute Sanitizer's memcheck with XML output on an out-of-bounds kernel
(examples/cuda/oob_bug.cu). Every raw debugger reply is kept (bounded) in the JSON report;
checks only say pass or fail. Nothing here certifies GPU support: the report is evidence
for ADR 0007's open questions.

Usage (from a Peeky checkout, with Peeky installed):
    python scripts/cuda/qualify.py --out cuda-qualification.json
Options: --cuda-gdb, --nvcc, --compute-sanitizer (default: found on PATH; a given path
must be executable),
--timeout seconds per debugger step (default 120; the first CUDA launch can be slow).
Exit status: 0 when every check passed, 1 when one failed, 2 when a tool is missing.
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "cuda"
MAX_TEXT = 4000


def bounded(value, limit=MAX_TEXT):
    """A JSON-safe copy of ``value`` with long strings cut, so reports stay small."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"... ({len(value)} chars)"
    if isinstance(value, dict):
        return {str(k): bounded(v, limit) for k, v in list(value.items())[:200]}
    if isinstance(value, (list, tuple)):
        return [bounded(v, limit) for v in list(value)[:200]]
    return value


def run(command, cwd=None, timeout=300):
    """Run one command; never raises for a failing command, only records it."""
    started = time.monotonic()
    try:
        done = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
        result = {"exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}
    except FileNotFoundError as exc:
        result = {"exit_code": None, "error": f"not found: {exc.filename}"}
    except subprocess.TimeoutExpired:
        result = {"exit_code": None, "error": f"timed out after {timeout} s"}
    return bounded({"command": command, **result, "seconds": round(time.monotonic() - started, 2)})


def toolchain(tools):
    """Versions of everything the report depends on; a missing tool is recorded, not fatal."""
    report = {"platform": platform.platform(), "python": sys.version.split()[0]}
    report["nvidia-smi"] = run(
        ["nvidia-smi", "--query-gpu=name,driver_version,compute_cap", "--format=csv,noheader"]
    )
    for name in ("nvcc", "cuda-gdb", "compute-sanitizer"):
        report[name] = run([tools[name], "--version"])
    report["cuda-gdb-python"] = run(
        [tools["cuda-gdb"], "-q", "-batch", "-ex", "python print(gdb.VERSION)"]
    )
    for name in ("nsys", "ncu"):
        report[name] = run([name, "--version"]) if shutil.which(name) else {"missing": True}
    return report


def marker_line(source, marker):
    for number, text in enumerate(source.read_text().splitlines(), 1):
        if marker in text:
            return number
    raise ValueError(f"{source.name} has no {marker} line")


INITIALIZE = {
    "clientID": "peeky",
    "adapterID": "gdb",
    "linesStartAt1": True,
    "columnsStartAt1": True,
    "pathFormat": "path",
}
FOCUS = re.compile(r"block \(2,0,0\).*thread \(255,0,0\)")
LAUNCH_CHECKS = ("dap_initialize", "breakpoint_accepted", "stopped_at_breakpoint")
# Every DAP check starts False, so a debugger that hangs or dies fails the report.
DAP_CHECKS = (
    "debugger_started",
    *LAUNCH_CHECKS,
    "kernel_frame_named",
    "focus_moved_to_block_2_thread_255",
    "dap_frame_follows_focus",
    "dap_locals_match_cli",
    "missing_block_left_focus_unchanged",
    "continued_to_exit",
    "program_output_seen",
)
MAX_EXTRA_STOPS = 20


class Steps:
    """Each DAP exchange, recorded in order with its outcome."""

    def __init__(self, dap, timeout):
        self.dap, self.timeout, self.log = dap, timeout, []

    def request(self, command, arguments=None):
        from peeky.dap import RequestFailed

        entry = {"request": command, "arguments": bounded(arguments)}
        self.log.append(entry)
        try:
            body = self.dap.request(command, arguments, timeout=self.timeout)
        except RequestFailed as exc:
            entry["refused"] = str(exc)
            return None
        entry["body"] = bounded(body)
        return body

    def response(self, seq, command):
        """The reply to a request sent earlier with ``dap.send``."""
        from peeky.dap import RequestFailed

        entry = {"response_to": command}
        self.log.append(entry)
        try:
            entry["body"] = bounded(self.dap.response(seq, timeout=self.timeout))
        except RequestFailed as exc:
            entry["refused"] = str(exc)
            return False
        return True

    def repl(self, command):
        """A CLI command; no frameId, which would reselect a frame from the old focus."""
        body = self.request("evaluate", {"expression": command, "context": "repl"})
        return None if body is None else str(body.get("result", ""))

    def evaluate(self, expression, frame_id):
        body = self.request(
            "evaluate", {"expression": expression, "context": "watch", "frameId": frame_id}
        )
        return None if body is None else str(body.get("result", "")).strip()

    def event(self, names, after):
        entry = {"wait_for": sorted(names)}
        self.log.append(entry)
        event = self.dap.wait_event(set(names), after=after, timeout=self.timeout)
        entry["event"] = bounded(event)
        return event


def launch_to_stop(steps, program, cwd, source, line):
    """Start ``program`` under a GDB-based adapter and wait for its first stop.

    GDB's DAP starts the program at different requests: 14.x at configurationDone,
    15.x at launch, and 16.x at configurationDone, which it refuses before launch.
    Breakpoints right after ``initialized``, then launch, then configurationDone works on
    all three (ADR 0007). 15.x may refuse configurationDone because the program is
    already running; that refusal is recorded and harmless. A refused launch ends here.
    """
    checks = dict.fromkeys(LAUNCH_CHECKS, False)
    checks["dap_initialize"] = steps.request("initialize", INITIALIZE) is not None
    if not checks["dap_initialize"]:
        return checks, None
    steps.event({"initialized"}, after=0)
    placed = steps.request(
        "setBreakpoints", {"source": {"path": str(source)}, "breakpoints": [{"line": line}]}
    )
    checks["breakpoint_accepted"] = bool(placed and placed.get("breakpoints"))
    launch = steps.dap.send("launch", {"program": str(program), "cwd": str(cwd)})
    steps.request("configurationDone")
    if not steps.response(launch, "launch"):
        return checks, None
    stopped = steps.event({"stopped", "terminated", "exited"}, after=0)
    checks["stopped_at_breakpoint"] = stopped["event"] == "stopped"
    return checks, stopped


def top_frame(steps, thread_id):
    stack = steps.request("stackTrace", {"threadId": thread_id, "levels": 5}) or {}
    frames = stack.get("stackFrames") or []
    return frames[0] if frames else {}


def frame_locals(steps, frame):
    """{name: value} from the frame's scopes except registers."""
    if "id" not in frame:
        return {}
    scopes = (steps.request("scopes", {"frameId": frame["id"]}) or {}).get("scopes") or []
    values = {}
    for scope in scopes:
        reference = scope.get("variablesReference")
        if scope.get("presentationHint") == "registers":
            continue
        if reference:
            listed = steps.request("variables", {"variablesReference": reference}) or {}
            for variable in listed.get("variables") or []:
                values.setdefault(variable.get("name"), variable.get("value"))
    return values


def printed(text):
    """The value from GDB's ``$N = value`` reply, or None."""
    match = re.match(r"\$\d+ = (.*)", (text or "").strip())
    return match.group(1).strip() if match else None


def debug_scale(tools, workdir, timeout):
    """Kernel breakpoint; focus on block 2 thread 255 (index 767); block 3 refused; exit.

    The index check uses builtins in a fresh DAP frame, which are right wherever that
    thread's warp is stopped; the local ``i`` is compared with the CLI's own view.
    """
    from peeky.dap import DAP

    source = workdir / "scale_bug.cu"
    line = marker_line(source, "// BREAK")
    steps = Steps(None, timeout)
    checks = dict.fromkeys(DAP_CHECKS, False)
    dap = None
    try:
        steps.dap = dap = DAP(command=[tools["cuda-gdb"], "-q", "-i=dap"])
        checks["debugger_started"] = True
        started, stopped = launch_to_stop(steps, workdir / "scale", workdir, source, line)
        checks.update(started)
        if not stopped or stopped["event"] != "stopped":
            return checks, steps.log, dap.snapshot_output()[0]
        thread = stopped.get("body", {}).get("threadId")
        checks["kernel_frame_named"] = "scale" in str(top_frame(steps, thread).get("name", ""))
        steps.repl("info cuda kernels")
        steps.repl("cuda block thread")
        moved = steps.repl("cuda block (2,0,0) thread (255,0,0)")
        focus = steps.repl("cuda block thread")
        checks["focus_moved_to_block_2_thread_255"] = moved is not None and bool(
            FOCUS.search(focus or "")
        )
        frame = top_frame(steps, thread)
        index = steps.evaluate("blockIdx.x * blockDim.x + threadIdx.x", frame.get("id"))
        checks["dap_frame_follows_focus"] = index == "767"
        values = frame_locals(steps, frame)
        cli = printed(steps.repl("print i"))
        checks["dap_locals_match_cli"] = cli is not None and str(values.get("i")).strip() == cli
        steps.repl("cuda block (3,0,0) thread (0,0,0)")
        checks["missing_block_left_focus_unchanged"] = bool(
            FOCUS.search(steps.repl("cuda block thread") or "")
        )
        steps.request("setBreakpoints", {"source": {"path": str(source)}, "breakpoints": []})
        checks["continued_to_exit"] = continue_to_exit(steps, thread)
        output = settled_output(dap, "SUM:") if checks["continued_to_exit"] else ""
        output = output or dap.snapshot_output()[0]
        checks["program_output_seen"] = "SUM:1536" in output
        return checks, steps.log, output
    except Exception as exc:
        steps.log.append({"error": f"{type(exc).__name__}: {exc}"})
        return checks, steps.log, dap.snapshot_output()[0] if dap else ""
    finally:
        if dap:
            dap.close()


def settled_output(dap, marker, seconds=5):
    """The program's output once ``marker`` is in it: GDB forwards stdout on its own
    thread, so the last lines can arrive after the exit event."""
    deadline = time.monotonic() + seconds
    while marker not in (output := dap.snapshot_output()[0]) and time.monotonic() < deadline:
        time.sleep(0.05)
    return output


def continue_to_exit(steps, thread):
    """Continue until the program ends; warps that hit the cleared breakpoint may still stop."""
    for _ in range(MAX_EXTRA_STOPS + 1):
        cursor = steps.dap.event_cursor()
        if steps.request("continue", {"threadId": thread}) is None:
            return False
        event = steps.event({"stopped", "terminated", "exited"}, after=cursor)
        if event["event"] != "stopped":
            return True
        thread = event.get("body", {}).get("threadId", thread)
    return False


def sanitize_oob(tools, workdir):
    """memcheck with XML output on the out-of-bounds kernel: it must report an error."""
    xml = workdir / "memcheck.xml"
    result = run(
        [
            tools["compute-sanitizer"],
            "--tool",
            "memcheck",
            "--xml",
            "yes",
            "--save",
            str(xml),
            "--error-exitcode",
            "86",
            "--print-limit",
            "20",
            str(workdir / "oob"),
        ],
        cwd=workdir,
    )
    summary = sanitizer_summary(xml.read_text(errors="replace")) if xml.exists() else None
    checks = {
        "memcheck_flagged_errors": result.get("exit_code") == 86,
        "memcheck_xml_written": summary is not None,
    }
    return checks, {"run": result, "xml": summary}


def sanitizer_summary(text):
    """The XML's shape without assuming its schema: root, element counts, a first record."""
    import xml.etree.ElementTree as ElementTree

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        return {"parse_error": str(exc), "head": text[:MAX_TEXT]}
    counts = {}
    for element in root.iter():
        counts[element.tag] = counts.get(element.tag, 0) + 1
    first = next(iter(root), None)
    return {
        "root": root.tag,
        "element_counts": dict(sorted(counts.items())[:100]),
        "first_child": bounded(ElementTree.tostring(first, encoding="unicode"))
        if first is not None
        else None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default="cuda-qualification.json")
    parser.add_argument("--timeout", type=float, default=120)
    for tool in ("cuda-gdb", "nvcc", "compute-sanitizer"):
        parser.add_argument(f"--{tool}", dest=tool.replace("-", "_"))
    args = parser.parse_args(argv)
    tools = {}
    for tool in ("cuda-gdb", "nvcc", "compute-sanitizer"):
        found = shutil.which(getattr(args, tool.replace("-", "_")) or tool)
        # Absolute: builds run in a scratch directory, where a relative path means nothing.
        tools[tool] = os.path.abspath(found) if found else None
    missing = sorted(name for name, path in tools.items() if not path)
    report = {"schema": 1, "issue": 61, "tools": tools, "missing_tools": missing}
    if missing:
        report["toolchain"] = {"platform": platform.platform()}
        Path(args.out).write_text(json.dumps(report, indent=2))
        print(f"missing tools: {', '.join(missing)}; see {args.out}", file=sys.stderr)
        return 2
    report["toolchain"] = toolchain(tools)
    with tempfile.TemporaryDirectory(prefix="peeky-cuda-") as scratch:
        workdir = Path(scratch)
        for name in ("scale_bug.cu", "oob_bug.cu"):
            shutil.copyfile(EXAMPLES / name, workdir / name)
        builds = {
            "scale": run([tools["nvcc"], "-g", "-G", "-o", "scale", "scale_bug.cu"], cwd=workdir),
            "oob": run([tools["nvcc"], "-lineinfo", "-o", "oob", "oob_bug.cu"], cwd=workdir),
        }
        report["builds"] = builds
        python = report["toolchain"]["cuda-gdb-python"]
        checks = {"cuda_gdb_python": python.get("exit_code") == 0 and bool(python.get("stdout"))}
        checks.update({f"build_{name}": build["exit_code"] == 0 for name, build in builds.items()})
        if checks["build_scale"] and checks["build_oob"]:
            baseline = run([str(workdir / "scale")], cwd=workdir)
            report["baseline"] = baseline
            checks["baseline_shows_the_bug"] = "SUM:1536" in str(baseline.get("stdout"))
            debug_checks, transcript, output = debug_scale(tools, workdir, args.timeout)
            checks.update(debug_checks)
            report["dap_transcript"] = transcript
            report["debugged_output"] = bounded(output)
            sanitize_checks, sanitize = sanitize_oob(tools, workdir)
            checks.update(sanitize_checks)
            report["sanitizer"] = sanitize
    report["checks"] = checks
    report["passed"] = bool(checks) and all(checks.values())
    Path(args.out).write_text(json.dumps(report, indent=2))
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    print(f"report: {args.out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
