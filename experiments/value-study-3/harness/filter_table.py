#!/usr/bin/env python3
"""FILTER.md's tables: each examined candidate's outcome at each step of the no-LLM filter.

    harness/filter_table.py results/reach/<sha, 12>

A candidate was examined when it has a bug directory. Step 1 (control image and grading) and
step 2 (Peeky image and grading) failed when its dropped.txt names that step. Step 3 is the reach
record in the given directory: "reached" when a gold point stopped and read a value (round 2's
group). Which reached candidates survive, the pool's and the refill's up to 50, is
harness/screen.py's rule. Notes come from candidates/filter-notes.tsv.
Python 3.9+, standard library only."""

import csv
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUND3 = HERE.parent
sys.path.insert(0, str(ROUND3.parent / "value-study-2" / "harness"))
import descriptive  # noqa: E402

sys.path.insert(0, str(HERE))
import screen  # noqa: E402

LANGUAGES = ("python", "java", "c", "cpp", "rust")
NAMES = {"python": "Python", "java": "Java", "c": "C", "cpp": "C++", "rust": "Rust"}


def gold_summary(record):
    parts = []
    for point in record["points"]:
        if point["role"] != "gold":
            continue
        shown = (point.get("values") or {}).get("shown")
        outcome = point["outcome"]
        if outcome == "reached":
            outcome += ", {} values".format(shown or 0)
        elif outcome == "refused":
            outcome += " ({})".format((point.get("detail") or {}).get("code", "?"))
        parts.append(outcome)
    return "; ".join(parts) or "no gold point"


def main(reach_dir):
    reach_dir = Path(reach_dir)
    groups = descriptive.reach_groups(reach_dir)
    kept, counted, after = screen.survivor_sets(reach_dir)
    notes = {}
    with open(ROUND3 / "candidates" / "filter-notes.tsv") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            notes[row["instance"]] = row["note"]
    meta = {}
    with open(ROUND3 / "candidates" / "eligible.tsv") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            meta[row["instance"]] = row
    candidates = ROUND3 / "candidates"
    pool = [
        (lang, b) for lang in LANGUAGES for b in (candidates / f"{lang}.txt").read_text().split()
    ]
    refill = [(meta[b]["language"], b) for b in (candidates / "refill.txt").read_text().split()]

    def outcome(bug):
        directory = ROUND3 / "bugs" / bug
        if not directory.is_dir():
            return None
        dropped = directory / "dropped.txt"
        text = dropped.read_text() if dropped.exists() else ""
        match = re.search(r"(?:step|stage:?)\s*(\d)", text, re.I)
        stage = int(match.group(1)) if match else None
        step1 = stage != 1
        step2 = step1 and stage != 2
        record_path = reach_dir / f"{bug}.json"
        record = json.loads(record_path.read_text()) if record_path.exists() else None
        reached = step2 and groups.get(bug) == "reached" and not dropped.exists()
        return step1, step2, reached, record

    def rows(entries, numbered):
        out, counts = [], {}
        for n, (language, bug) in enumerate(entries, 1):
            got = outcome(bug)
            c = counts.setdefault(language, [0, 0, 0, 0, 0])
            if got is None:
                continue
            step1, step2, reached, record = got
            c[0] += 1
            c[1] += step1
            c[2] += step2
            c[3] += reached
            c[4] += bug in kept or bug in counted
            if bug in kept or bug in counted:
                result = "**survives**"
            elif reached:
                result = "reached, after the refill's end"
            else:
                result = "dropped"
            tier = "1" if float(meta[bug]["public_rate"]) <= 0.125 else "2"
            out.append(
                "| {}{} | {} | {} | {} | {} | {} | {} | {} |".format(
                    "{} | ".format(n) if numbered else "",
                    bug,
                    NAMES[language],
                    tier,
                    "ok" if step1 else "fail",
                    ("skipped" if step2 and record is None else "ok" if step2 else "fail")
                    if step1
                    else "—",
                    gold_summary(record) if (record and step2) else "—",
                    result,
                    notes.get(bug, ""),
                )
            )
        return out, counts

    pool_rows, pool_counts = rows(pool, False)
    refill_rows, refill_counts = rows(refill, True)
    print(
        "| Language | Pool: examined | Step 1 | Step 2 | Reached | Refill: examined | Step 1 | Step 2 | Reached | Counted | Survivors |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    total = [0] * 10
    for language in LANGUAGES:
        a = pool_counts.get(language, [0] * 5)[:4]
        b = refill_counts.get(language, [0] * 5)
        values = a + b + [a[3] + b[4]]
        total = [t + v for t, v in zip(total, values)]
        print("| {} | {} |".format(NAMES[language], " | ".join(str(v) for v in values)))
    print("| **All** | {} |".format(" | ".join("**{}**".format(v) for v in total)))
    print()
    print("### The pool\n")
    print("| Candidate | Language | Tier | Step 1 | Step 2 | Gold points | Result | Note |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    print("\n".join(pool_rows))
    print()
    print("### The refill, in its order\n")
    print("| # | Candidate | Language | Tier | Step 1 | Step 2 | Gold points | Result | Note |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    print("\n".join(refill_rows))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
