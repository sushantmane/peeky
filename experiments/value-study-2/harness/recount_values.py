#!/usr/bin/env python3
"""Counts again the program values Peeky printed in each attempt, `peeky_values_printed` in
its result.json, from the attempt's raw events, with round 1's metrics.py:

    harness/recount_values.py --runs <raw runs dir> [--check]

<raw runs dir> holds each attempt's events.jsonl.gz, as run_one.py left them (outside the
repository). The count is written into results/runs/<bug>/<arm>-<rep>/result.json, and nothing
else there changes. Prints each attempt whose count changed. With --check, writes nothing, and
exits 1 if a count would change. Python 3.9+, standard library only."""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUND2 = HERE.parent
sys.path.insert(0, str(ROUND2.parent / "value-study" / "harness"))
import metrics  # noqa: E402


def recount(raw_dir, peeky_sha):
    return sum(
        metrics.peeky_values(entry["output"], entry["command"], peeky_sha)
        for entry in metrics.shell_transcript(raw_dir)
        if metrics.peeky_subcommands(entry["command"])
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", required=True, help="the raw runs directory")
    parser.add_argument("--results", default=str(ROUND2 / "results" / "runs"))
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 on a change")
    args = parser.parse_args()
    paths = sorted(Path(args.results).glob("*/*/result.json"))
    changed = []
    for path in paths:
        raw = Path(args.runs) / path.parent.parent.name / path.parent.name
        if not any((raw / name).is_file() for name in ("events.jsonl", "events.jsonl.gz")):
            sys.exit("no raw events for {}".format(raw))
        result = json.loads(path.read_text())
        count = recount(raw, result.get("peeky_sha"))
        if count != result.get("peeky_values_printed"):
            changed.append((path, result, count))
    # Each image writes its own PROJECT.md; every other doc must be read at the image's commit.
    missing = {name for (_, name), lines in metrics.DOCS.items() if lines is None}
    if missing - {"PROJECT.md"}:
        sys.exit("docs not found at the attempts' Peeky commit: {}".format(sorted(missing)))
    for path, result, count in changed:
        print(
            "{}/{}: {} -> {}".format(
                path.parent.parent.name, path.parent.name, result["peeky_values_printed"], count
            )
        )
        if not args.check:
            result["peeky_values_printed"] = count
            path.write_text(json.dumps(result, indent=1) + "\n")
    print("{} of {} attempts changed".format(len(changed), len(paths)))
    if args.check and changed:
        sys.exit(1)


if __name__ == "__main__":
    main()
