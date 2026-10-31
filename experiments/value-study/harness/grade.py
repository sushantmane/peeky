#!/usr/bin/env python3
"""Grades a bug's image as the harness does, in a fresh container with no network.

    harness/grade.py <bug_id> [--gold] [--name NAME] [--arm control|peeky] [--bugs DIR]
                     [--write-validation]

Without --gold, /testbed is graded as the image has it (the base commit); with --gold,
gold.patch is applied first. Prints {"name", "f2p", "p2p_failed", "solved", "seconds"};
--write-validation merges that record into the bug's validation.json under NAME
(default: base, or gold-1 with --gold)."""

import argparse
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import studylib  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bug_id")
    parser.add_argument("--gold", action="store_true")
    parser.add_argument("--name")
    parser.add_argument("--arm", choices=studylib.ARMS, default="control")
    parser.add_argument("--bugs", default=str(studylib.BUGS))
    parser.add_argument("--log", help="write the eval log here")
    parser.add_argument("--write-validation", action="store_true")
    args = parser.parse_args()

    bug = studylib.load_bug(args.bug_id, args.bugs)
    image = studylib.image_for(bug, args.arm)
    name = args.name or ("gold-1" if args.gold else "base")
    container = "study-grade-{}-{}".format(args.bug_id, secrets.token_hex(3))
    try:
        studylib.start_container(image, container)
        graded = studylib.grade_container(container, bug, args.log, apply_gold=args.gold)
    finally:
        studylib.remove_container(container)
    record = {
        "name": name,
        "f2p": graded["f2p"],
        "p2p_failed": graded["p2p_failed"],
        "solved": graded["solved"],
        "seconds": graded["seconds"],
    }
    print(json.dumps(record, indent=1))
    if not graded["markers_found"]:
        print("warning: no test output markers in the eval log", file=sys.stderr)
    if args.write_validation:
        path = bug["dir"] / "validation.json"
        runs = json.loads(path.read_text()) if path.is_file() else []
        runs = [run for run in runs if run.get("name") != name] + [record]
        studylib.write_json(path, runs)


if __name__ == "__main__":
    main()
