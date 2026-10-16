"""Where does a probe's time go? Peeky CPU vs child processes vs waiting.

Usage: python scripts/profile_probe.py <target> --at <line> [peeky probe options] [--runs N]

Runs `peeky probe` in this process and reports, per run:
- wall_ms: elapsed time;
- peeky_cpu_ms: CPU used by Peeky itself (all threads of this process);
- children_cpu_ms: CPU of compilers, debug adapters and the debuggee;
- idle_ms: time nothing here was computing (waiting on adapters, programs, I/O);
- ops_ms: duration of each worker operation, in order.

Only peeky_cpu_ms can shrink by rewriting Peeky in another language.
"""

import contextlib
import io
import json
import os
import resource
import sys
import tempfile
import time
from collections import defaultdict


def main(argv):
    runs = 1
    if "--runs" in argv:
        index = argv.index("--runs")
        runs = int(argv[index + 1])
        del argv[index : index + 2]
    started = time.perf_counter()
    import peeky.cli as cli
    from peeky import worker

    import_ms = round((time.perf_counter() - started) * 1000)
    original = worker.DebugWorker.dispatch
    ops = defaultdict(list)

    def timed(self, operation, args):
        begin = time.perf_counter()
        try:
            return original(self, operation, args)
        finally:
            ops[operation].append(round((time.perf_counter() - begin) * 1000, 1))

    worker.DebugWorker.dispatch = timed
    os.environ.setdefault("PEEKY_STATE_DIR", tempfile.mkdtemp(prefix="peeky-profile-"))
    for _ in range(runs):
        ops.clear()
        own = resource.getrusage(resource.RUSAGE_SELF)
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        begin = time.perf_counter()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["probe", *argv, "--json", "--quiet", "--no-save"])
        wall = (time.perf_counter() - begin) * 1000
        own_after = resource.getrusage(resource.RUSAGE_SELF)
        children_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        peeky = 1000 * (own_after.ru_utime - own.ru_utime + own_after.ru_stime - own.ru_stime)
        child = 1000 * (
            children_after.ru_utime
            - children.ru_utime
            + children_after.ru_stime
            - children.ru_stime
        )
        result = json.loads(output.getvalue())
        print(
            json.dumps(
                {
                    "status": result.get("status"),
                    "exit": code,
                    "import_ms": import_ms,
                    "wall_ms": round(wall),
                    "peeky_cpu_ms": round(peeky),
                    "children_cpu_ms": round(child),
                    "idle_ms": round(max(0, wall - peeky - child)),
                    "ops_ms": dict(ops),
                }
            )
        )


if __name__ == "__main__":
    main(sys.argv[1:])
