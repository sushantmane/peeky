#!/usr/bin/env python3
"""Runs a command while holding one of a few machine-wide slots, so that the agents building
round 3's candidates never run more than SLOTS Docker builds or test containers at once.

    harness/heavy.py <command> [args...]

Slots are lock files in $STUDY_LOCKS (default ~/.cache/peeky-study-3/locks), held with flock;
a slot frees when the command ends, or its process dies. SLOTS defaults to 2. Each build or
container also limits itself to 4 CPUs and 6 GB."""

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    locks = Path(os.environ.get("STUDY_LOCKS", Path.home() / ".cache" / "peeky-study-3" / "locks"))
    locks.mkdir(parents=True, exist_ok=True)
    slots = int(os.environ.get("SLOTS", "2"))
    waited = 0
    while True:
        for n in range(slots):
            handle = open(locks / f"slot-{n}", "w")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            if waited:
                print(f"[heavy] slot {n} after {waited} s", file=sys.stderr, flush=True)
            sys.exit(subprocess.call(sys.argv[1:]))
        time.sleep(10)
        waited += 10


if __name__ == "__main__":
    main()
