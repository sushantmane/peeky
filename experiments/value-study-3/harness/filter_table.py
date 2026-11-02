#!/usr/bin/env python3
"""FILTER.md's tables: each examined candidate's outcome at each step of the no-LLM filter.

    harness/filter_table.py results/reach/<sha, 12>

A candidate was examined when it has a bug directory. Step 1 (control image and grading) and
step 2 (Peeky image and grading) failed when its dropped.txt names that step. Step 3 is the reach
record in the given directory: "reached" when a gold point stopped and read a value (round 2's
group), as harness/screen.py decides it. Notes come from candidates/filter-notes.tsv.
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
    notes = {}
    with open(ROUND3 / "candidates" / "filter-notes.tsv") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            notes[row["instance"]] = row["note"]
    tiers = {}
    with open(ROUND3 / "candidates" / "eligible.tsv") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            tiers[row["instance"]] = "1" if float(row["public_rate"]) <= 0.125 else "2"
    counts = {}
    rows = []
    for language in LANGUAGES:
        order = (ROUND3 / "candidates" / f"{language}.txt").read_text().split()
        order += (ROUND3 / "candidates" / f"{language}-reserve.txt").read_text().split()
        c = counts[language] = {"examined": 0, "step 1": 0, "step 2": 0, "reached": 0}
        for bug in order:
            directory = ROUND3 / "bugs" / bug
            if not directory.is_dir():
                continue
            c["examined"] += 1
            dropped = directory / "dropped.txt"
            text = dropped.read_text() if dropped.exists() else ""
            match = re.search(r"(?:step|stage:?)\s*(\d)", text, re.I)
            stage = int(match.group(1)) if match else None
            step1 = stage != 1
            step2 = step1 and stage != 2
            record_path = reach_dir / f"{bug}.json"
            record = json.loads(record_path.read_text()) if record_path.exists() else None
            reached = step2 and groups.get(bug) == "reached" and not dropped.exists()
            c["step 1"] += step1
            c["step 2"] += step2
            c["reached"] += reached
            rows.append(
                "| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                    bug,
                    NAMES[language],
                    tiers.get(bug, "?"),
                    "ok" if step1 else "fail",
                    ("ok" if step2 else "fail") if step1 else "—",
                    gold_summary(record) if (record and step2) else "—",
                    "**survives**" if reached else "dropped",
                    notes.get(bug, ""),
                )
            )
    print(
        "| Language | Examined | Step 1: control image and grading | Step 2: Peeky image and grading | Step 3: reached |"
    )
    print("| --- | --- | --- | --- | --- |")
    total = {k: 0 for k in ("examined", "step 1", "step 2", "reached")}
    for language in LANGUAGES:
        c = counts[language]
        for k in total:
            total[k] += c[k]
        print("| {} | {} | {} | {} | {} |".format(NAMES[language], *c.values()))
    print("| **All** | **{}** | **{}** | **{}** | **{}** |".format(*total.values()))
    print()
    print("| Candidate | Language | Tier | Step 1 | Step 2 | Gold points | Result | Note |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    print("\n".join(rows))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
