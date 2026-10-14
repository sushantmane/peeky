"""Measure real HTTP/debugger latency with synthetic data; no model or credentials needed."""

import argparse
import importlib.metadata
import json
import math
import platform
import secrets
import statistics
import tempfile
import threading
import time
from pathlib import Path

from peeky.client import Client
from peeky.java import JavaTarget
from peeky.worker import make_server


def benchmark(iterations, java_bundle=None):
    samples = {}
    java_metadata = None

    def timed(label, fn):
        started = time.perf_counter_ns()
        result = fn()
        samples.setdefault(label, []).append((time.perf_counter_ns() - started) / 1e6)
        return result

    with tempfile.TemporaryDirectory(prefix="peeky-benchmark-") as directory:
        token = secrets.token_urlsafe(32)

        if java_bundle:
            bundle_path = Path(java_bundle).resolve()
            java_metadata = json.loads((bundle_path / "adapter.json").read_text())
            Path(directory, "Bench.java").write_text(
                "public class Bench {\n    public static void main(String[] args) {\n        int value = 1;\n        System.out.println(value);\n    }\n}\n"
            )
            adapter = JavaTarget(bundle=bundle_path, sources=("Bench.java",), main_class="Bench")
            server = make_server(directory, token, 0, adapter=adapter)
        else:
            Path(directory, "target.py").write_text("value = 1\nprint(value)\n")
            server = make_server(directory, token, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = Client(f"http://127.0.0.1:{server.server_port}", token)
        try:
            for _ in range(iterations):
                started = time.perf_counter_ns()
                program = "Bench.java" if java_bundle else "target.py"
                breakpoint_line = 4 if java_bundle else 2
                launched = timed(
                    "launch",
                    lambda p=program, bl=breakpoint_line: client.call(
                        "launch", program=p, breakpoints=[{"line": bl}]
                    ),
                )
                session = {"session_id": launched["session_id"]}

                def wait_for(state):
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        result = client.call("wait", **session, timeout=2)
                        if result["state"] == state:
                            return result
                        if result["state"] != "running":
                            raise RuntimeError(f"Expected {state}, got {result['state']}")
                    raise TimeoutError(f"Benchmark did not reach {state}")

                stop = timed("wait_stop", lambda: wait_for("stopped"))
                identity = dict(session, stop_id=stop["stop_id"])
                inspected = timed("inspect", lambda: client.call("inspect", **identity))
                scope = next(
                    s
                    for s in inspected["scopes"]
                    if any(v["name"] == "value" for v in s["variables"])
                )
                changed = timed(
                    "mutate",
                    lambda: client.call(
                        "set_variable",
                        **identity,
                        reference=scope["reference"],
                        name="value",
                        value="2",
                    ),
                )
                if changed["after"] != "2":
                    raise RuntimeError("Mutation was not confirmed")
                timed("continue", lambda: client.call("continue", **identity))
                timed("wait_termination", lambda: wait_for("terminated"))
                report = client.call("report", **session)
                if report["output"].strip() != "2":
                    raise RuntimeError("Runtime did not observe mutation")
                timed("close", lambda: client.call("close", **session))
                samples.setdefault("total", []).append((time.perf_counter_ns() - started) / 1e6)
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.worker.close()
            server.server_close()
    result = {
        "schema_version": 1,
        "language": "java" if java_bundle else "python",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "iterations": iterations,
    }

    if not java_bundle:
        result["environment"]["python"] = platform.python_version()
        result["environment"]["debugpy"] = importlib.metadata.version("debugpy")
        result["notes"] = (
            "Local HTTP, fresh adapter each iteration, no warmup discarded. No model latency."
        )
    else:
        result["environment"].update(
            java=java_metadata["java_version"], adapter_source_commit=java_metadata["source_commit"]
        )
        result["notes"] = (
            "Local HTTP, fresh adapter each iteration with compilation, no warmup discarded. No model latency. Java launch includes compilation."
        )

    result["milliseconds"] = {
        name: {
            "median": round(statistics.median(values), 3),
            "p95": round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 3),
            "samples": [round(value, 3) for value in values],
        }
        for name, values in samples.items()
    }

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--java-bundle", type=str, default=None)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 1000:
        parser.error("iterations must be 1..1000")
    print(json.dumps(benchmark(args.iterations, java_bundle=args.java_bundle), indent=2))
