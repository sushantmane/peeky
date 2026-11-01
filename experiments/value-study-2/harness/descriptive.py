#!/usr/bin/env python3
"""Round 2's descriptive tables, beside the analysis (harness/study.sh analyze.py):

    harness/descriptive.py [--reach results/reach/<sha, 12>]

1. Drift: each bug's solve rate in this round's control arm against round 1's control arm. The
   mean over bugs of the difference, with round 1's bootstrap (resampling bugs).
2. Reach groups (README, "Reach groups"), from a reach run's records: "reached" (a gold point
   stopped and `session inspect` read a value there), else "refused" (every gold point refused)
   or "other". The two primary differences within "reached" and within the rest, with the
   analysis's bootstrap.
3. Uptake in each group: per Peeky arm, the share of attempts that ran Peeky and that printed a
   program value.

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


def reach_groups(reach_dir):
    """{bug: "reached" | "refused" | "other"} from a reach run's records."""
    groups = {}
    for path in Path(reach_dir).glob("*.json"):
        record = json.loads(path.read_text())
        gold = [p for p in record["points"] if p["role"] == "gold"]
        if any(
            p["outcome"] == "reached" and (p.get("values") or {}).get("shown", 0) > 0 for p in gold
        ):
            groups[record["bug"]] = "reached"
        elif gold and all(p["outcome"] == "refused" for p in gold):
            groups[record["bug"]] = "refused"
        else:
            groups[record["bug"]] = "other"
    return groups


GROUPS = (
    ("reached", ("reached",)),
    ("not reached", ("refused", "other")),
    ("refused", ("refused",)),
    ("other", ("other",)),
)


def by_group(round2_runs, groups):
    print("\n### The primary differences by reach group\n")
    print("| Comparison | Group | Bugs | Base | Arm | Difference (95% CI) |")
    print("| --- | --- | --- | --- | --- | --- |")
    rates = analyze.solve_rates(round2_runs, set(groups))
    for treatment, base in studylib.COMPARISONS:
        for label, kinds in GROUPS:
            members = sorted(b for b, g in groups.items() if g in kinds)
            p = analyze.paired(rates, members, treatment, base)
            low, high = p["difference_ci"]
            print(
                "| {} − {} | {} | {} | {} | {} | {} |".format(
                    treatment,
                    base,
                    label,
                    p["bugs"],
                    analyze.fmt(p[base + "_rate"], 1, True),
                    analyze.fmt(p[treatment + "_rate"], 1, True),
                    "-"
                    if p["difference"] is None
                    else "{:+.0f} pp ({:+.0f} to {:+.0f})".format(
                        p["difference"] * 100, low * 100, high * 100
                    ),
                )
            )


def uptake(round2_runs, groups):
    print("\n### Uptake by reach group\n")
    print("| Arm | Group | Attempts | Ran Peeky | Printed a value |")
    print("| --- | --- | --- | --- | --- |")
    for arm in studylib.ARMS:
        if not studylib.arm_uses_peeky(arm):
            continue
        for label, kinds in GROUPS:
            group = [r for r in round2_runs if r["arm"] == arm and groups.get(r["bug"]) in kinds]
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
    groups = reach_groups(reach)
    print(
        "Reach groups from {}: {}.\n".format(
            Path(reach).name,
            ", ".join(
                "{} {}".format(sum(g == kind for g in groups.values()), kind)
                for kind in ("reached", "refused", "other")
            ),
        )
    )
    drift(round1_runs, round2_runs)
    by_group(round2_runs, groups)
    uptake(round2_runs, groups)


if __name__ == "__main__":
    main()
