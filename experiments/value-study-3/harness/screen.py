#!/usr/bin/env python3
"""Round 3's screening: which candidates go to the main run (README, "Screening").

    harness/screen.py survivors <reach dir>   # the filter's survivors on one Peeky commit
    harness/screen.py status [--write]        # each survivor's screening decision so far

`survivors` lists the candidates whose bug directory has no dropped.txt and whose reach record in
<reach dir> is in round 2's "reached" group: a gold point stopped, and `session inspect` read at
least one value there. It writes candidates/survivors.txt.

`status` reads the screening's control attempts (results/screening/runs, run by
harness/screen.sh). For each survivor: attempt 1; if it failed, attempts 2 and 3. A candidate is
kept when attempt 1 failed and attempts 2 and 3 solved at most once between them. An
infrastructure failure doesn't count; the schedule retries it. It prints what is still to run,
and with --write records every decision in results/screening/screening.json, with the main run's
bugs and attempts per arm:

- bugs: every kept candidate, or 30 of them if more are kept, the first 30 of the kept list
  shuffled with seed 20261102;
- attempts per arm, k: 160 / bugs, rounded up, at least 6 and at most 11.

Fewer than 12 kept: the coordinator stops and reports (README, "Screening").
Python 3.9+, standard library only."""

import argparse
import json
import math
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUND3 = HERE.parent
sys.path.insert(0, str(ROUND3.parent / "value-study-2" / "harness"))
import descriptive  # noqa: E402

SEED = 20261102
RUNS = ROUND3 / "results" / "screening" / "runs"
MAX_BUGS, MIN_BUGS, TOTAL, MIN_K, MAX_K = 30, 12, 160, 6, 11


def survivors(reach_dir):
    groups = descriptive.reach_groups(reach_dir)
    keep = sorted(
        bug
        for bug, group in groups.items()
        if group == "reached" and not (ROUND3 / "bugs" / bug / "dropped.txt").exists()
    )
    (ROUND3 / "candidates" / "survivors.txt").write_text("".join(b + "\n" for b in keep))
    print("{} survivors of {} reach records in {}".format(len(keep), len(groups), reach_dir))
    return keep


def attempts(bug):
    """{rep: solved} for the bug's screening attempts that count."""
    out = {}
    for path in sorted((RUNS / bug).glob("control-*/result.json")):
        result = json.loads(path.read_text())
        if result.get("infra_failure"):
            continue
        out[int(result["rep"])] = bool(result["solved"])
    return out


def decide(runs):
    if 1 not in runs:
        return "attempt 1 to run"
    if runs[1]:
        return "dropped: attempt 1 solved"
    if 2 not in runs or 3 not in runs:
        return "attempts 2 and 3 to run"
    solved = runs[2] + runs[3]
    return "kept" if solved <= 1 else "dropped: attempts 2 and 3 both solved"


def status(write):
    bugs = (ROUND3 / "candidates" / "survivors.txt").read_text().split()
    decisions = {bug: decide(attempts(bug)) for bug in bugs}
    for bug in bugs:
        print("{:45} {:40} {}".format(bug, decisions[bug], attempts(bug)))
    kept = sorted(b for b, d in decisions.items() if d == "kept")
    pending = [b for b, d in decisions.items() if d.endswith("to run")]
    wave2 = [b for b, d in decisions.items() if d == "attempts 2 and 3 to run"]
    print(
        "\nkept {}, pending {}; attempts 2 and 3 still to run for: {}".format(
            len(kept), len(pending), ",".join(wave2) or "none"
        )
    )
    main = list(kept)
    random.Random(SEED).shuffle(main)
    main = sorted(main[:MAX_BUGS])
    k = min(MAX_K, max(MIN_K, math.ceil(TOTAL / len(main)))) if main else None
    if not pending:
        if len(main) < MIN_BUGS:
            print("fewer than {} kept: stop and report".format(MIN_BUGS))
        else:
            print("main run: {} bugs, {} attempts per arm".format(len(main), k))
    if write:
        record = {
            "decisions": {b: {"decision": decisions[b], "attempts": attempts(b)} for b in bugs},
            "kept": kept,
            "pending": pending,
            "main_run": {"bugs": main, "attempts_per_arm": k} if not pending else None,
        }
        path = RUNS.parent / "screening.json"
        path.write_text(json.dumps(record, indent=1) + "\n")
        print("wrote", path)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("survivors")
    s.add_argument("reach_dir")
    t = sub.add_parser("status")
    t.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.command == "survivors":
        survivors(args.reach_dir)
    else:
        status(args.write)


if __name__ == "__main__":
    main()
