#!/usr/bin/env python3
"""Round 2's descriptive tables, beside the analysis (harness/study.sh analyze.py):

    harness/descriptive.py [--reach results/reach/<sha, 12>]

1. Drift: each bug's solve rate in this round's control arm against round 1's control arm. The
   mean over bugs of the difference, with round 1's bootstrap (resampling bugs).
2. Uptake against reach: per Peeky arm, the share of attempts that ran Peeky and that printed a
   program value, split by whether the reach check stopped at any of the bug's gold points.

Reads round 1's and this round's results/runs, and a reach run's records. Python 3.9+, standard
library only, with round 1's analysis code."""

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUND2 = HERE.parent
ROUND1 = ROUND2.parent / "value-study"
os.environ.setdefault("PEEKY_STUDY_CONFIG", str(ROUND2 / "study.json"))
sys.path.insert(0, str(ROUND1 / "harness"))
import analyze  # noqa: E402
import studylib  # noqa: E402


def valid(runs):
    return [r for r in runs if not r.get("infra_failure") and not r.get("contaminated")]


def rates(runs, arm):
    by_bug = {}
    for run in runs:
        if run["arm"] == arm:
            by_bug.setdefault(run["bug"], []).append(bool(run.get("solved")))
    return {bug: sum(s) / len(s) for bug, s in by_bug.items()}


def drift(round1_runs, round2_runs):
    before, after = rates(round1_runs, "control"), rates(round2_runs, "control")
    bugs = sorted(set(before) & set(after))
    differences = [after[b] - before[b] for b in bugs]
    low, high = analyze.bootstrap(differences)
    print("### Drift: this round's control arm against round 1's\n")
    print("| Bug | Round 1 | Round 2 |")
    print("| --- | --- | --- |")
    for bug in bugs:
        print("| {} | {:.0%} | {:.0%} |".format(bug, before[bug], after[bug]))
    if differences:
        print(
            "\nMean difference over {} bugs: {:+.0f} pp (95% CI {:+.0f} to {:+.0f}).".format(
                len(bugs), statistics.mean(differences) * 100, low * 100, high * 100
            )
        )


def reached_bugs(reach_dir):
    reached = set()
    for path in Path(reach_dir).glob("*.json"):
        record = json.loads(path.read_text())
        if any(p["role"] == "gold" and p["outcome"] == "reached" for p in record["points"]):
            reached.add(record["bug"])
    return reached


def uptake(round2_runs, reached):
    print("\n### Uptake against reach\n")
    print("| Arm | Gold point reached | Attempts | Ran Peeky | Printed a value |")
    print("| --- | --- | --- | --- | --- |")
    for arm in studylib.ARMS:
        if not studylib.arm_uses_peeky(arm):
            continue
        for label, inside in (("yes", True), ("no", False)):
            group = [r for r in round2_runs if r["arm"] == arm and (r["bug"] in reached) == inside]
            ran = sum(bool(r.get("peeky_commands")) for r in group)
            printed = sum((r.get("peeky_values_printed") or 0) > 0 for r in group)
            print("| {} | {} | {} | {} | {} |".format(arm, label, len(group), ran, printed))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--round1", default=str(ROUND1 / "results" / "runs"))
    parser.add_argument("--results", default=str(studylib.RUNS))
    parser.add_argument("--reach", help="a reach run's directory (default: the newest)")
    args = parser.parse_args()
    reach = args.reach
    if not reach:
        runs = sorted((ROUND2 / "results" / "reach").iterdir(), key=lambda p: p.stat().st_mtime)
        reach = str(runs[-1])
    round1_runs = valid(analyze.load_runs(args.round1))
    round2_runs = valid(analyze.load_runs(args.results))
    drift(round1_runs, round2_runs)
    uptake(round2_runs, reached_bugs(reach))


if __name__ == "__main__":
    main()
