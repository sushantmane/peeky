#!/usr/bin/env python3
"""FILTER.md's tables for the deciding run (README, "The no-LLM filter"): the candidates that
passed steps 1 and 2, each one's gold points on the first pass and on the final commit, which
survive by harness/screen.py's rule, and for each survivor the image check and the one-call
--test check (harness/first_call.py), an extra column that decides nothing.

    harness/deciding_table.py <final reach dir> <first-call dir> <image check tsv>

The first pass is results/reach/3e51ccd7232d. The image check TSV has a row per rebuilt Peeky
image: bug, gold (solved with the upstream fix), noop (solved unchanged), and their seconds.
Python 3.9+, standard library only."""

import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUND3 = HERE.parent
sys.path.insert(0, str(ROUND3.parent / "value-study-2" / "harness"))
import descriptive  # noqa: E402

sys.path.insert(0, str(HERE))
import filter_table  # noqa: E402
import screen  # noqa: E402

FIRST_PASS = ROUND3 / "results" / "reach" / "3e51ccd7232d"
NAMES = filter_table.NAMES


def record(directory, bug):
    path = Path(directory) / f"{bug}.json"
    return json.loads(path.read_text()) if path.exists() else None


def read_in_one_call(row):
    return bool(row) and any(p["outcome"] == "read" for p in row.get("points") or [])


def one_call(row):
    if row is None:
        return "—"
    points = row.get("points") or []
    if not points:
        return "error: " + str(row.get("error"))[:120]
    parts = []
    for point in points:
        outcome, detail = point["outcome"], point.get("detail") or {}
        if outcome == "read":
            outcome = "read {} of {}".format(point["values"]["read"], len(point["captures"]))
        elif outcome == "refused":
            outcome = "refused ({})".format(detail.get("code"))
        parts.append(outcome)
    return "; ".join(parts)


def main(final_dir, first_call_dir, image_tsv):
    languages = {}
    with open(ROUND3 / "candidates" / "eligible.tsv") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            languages[row["instance"]] = row["language"]
    images = {}
    with open(image_tsv) as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            images[row["bug"]] = row
    first_groups = descriptive.reach_groups(FIRST_PASS)
    final_groups = descriptive.reach_groups(final_dir)
    kept, counted, after = screen.survivor_sets(Path(final_dir))
    survivors = set(kept) | set(counted)
    candidates = ROUND3 / "candidates"
    pool = [
        b for lang in screen.LANGUAGES for b in (candidates / f"{lang}.txt").read_text().split()
    ]
    refill = (candidates / "refill.txt").read_text().split()
    # The candidates the deciding run checked, and any it should have: one reached on the first
    # pass that wasn't dropped (zstd-1107 was, by a ruling).
    examined = [
        b
        for b in pool + refill
        if record(final_dir, b) is not None
        or (first_groups.get(b) == "reached" and not (ROUND3 / "bugs" / b / "dropped.txt").exists())
    ]

    counts = {}
    for bug in examined:
        c = counts.setdefault(languages[bug], [0, 0, 0, 0, 0])
        c[0] += 1
        c[1] += first_groups.get(bug) == "reached"
        c[2] += final_groups.get(bug) == "reached"
        c[3] += bug in survivors
        c[4] += bug in survivors and read_in_one_call(record(first_call_dir, bug))
    print(
        "| Language | Rerun | Reached on `3e51ccd7` | Reached on `1422812c` | Survivors | "
        "Read in one `--test` call |"
    )
    print("| --- | --- | --- | --- | --- | --- |")
    total = [0] * 5
    for language in screen.LANGUAGES:
        c = counts.get(language, [0] * 5)
        total = [t + v for t, v in zip(total, c)]
        print("| {} | {} |".format(NAMES[language], " | ".join(str(v) for v in c)))
    print("| **All** | {} |".format(" | ".join("**{}**".format(v) for v in total)))
    print(
        "\n{} survivors: {} of the pool, {} of the refill; {} reached after the refill's end.".format(
            len(survivors), len(kept), len(counted), len(after)
        )
    )
    changed = [b for b in examined if first_groups.get(b) != final_groups.get(b)]
    print("Changed group between the runs: {}.".format(", ".join(changed) or "none"))

    print(
        "\n| Candidate | Language | `3e51ccd7` | `1422812c` | Image check | Result | One `--test` call |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for bug in examined:
        first = record(FIRST_PASS, bug)
        final = record(final_dir, bug)
        image = images.get(bug)
        check = "—"
        if image:
            check = "gold {}, noop {}".format(
                "solved" if image["gold"] == "True" else "not solved",
                "solved" if image["noop"] == "True" else "not solved",
            )
        if bug in survivors:
            result = "**survives**"
        elif final_groups.get(bug) == "reached":
            result = "reached, after the refill's end"
        else:
            result = "dropped"
        print(
            "| {} | {} | {} | {} | {} | {} | {} |".format(
                bug,
                NAMES[languages[bug]],
                filter_table.gold_summary(first),
                filter_table.gold_summary(final) if final else "not rerun",
                check,
                result,
                one_call(record(first_call_dir, bug)) if bug in survivors else "—",
            )
        )


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
