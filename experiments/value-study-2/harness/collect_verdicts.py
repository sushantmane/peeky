#!/usr/bin/env python3
"""Gathers the judges' verdicts into results/judge/<bug>/<arm>-<rep>.json, as round 1 kept them.

    harness/collect_verdicts.py <judge dir> [--out results/judge]

<judge dir> is what round 1's prepare_judging.py wrote (harness/study.sh prepare_judging.py),
with the judges' answers added:

- out/<anon>.json: part 1, blind: {"wrong_root_cause": bool, "evidence": str};
- peeky-out/<bug>__<arm>__<rep>.json: part 2, for attempts that ran Peeky:
  {"peeky_wrong_value": bool|null, "peeky_evidence": str}.

key.json maps each anonymous id to its attempt. An attempt with no part 2 answer gets
"peeky_wrong_value": null. Python 3.9+, standard library only."""

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("judge_dir")
    parser.add_argument("--out", default=str(HERE.parent / "results" / "judge"))
    args = parser.parse_args()
    judge, out = Path(args.judge_dir), Path(args.out)
    key = json.loads((judge / "key.json").read_text())
    missing = []
    for anon, (bug, arm, rep) in sorted(key.items()):
        path = judge / "out" / "{}.json".format(anon)
        if not path.is_file():
            missing.append(anon)
            continue
        verdict = json.loads(path.read_text())
        part2 = judge / "peeky-out" / "{}__{}__{}.json".format(bug, arm, rep)
        extra = json.loads(part2.read_text()) if part2.is_file() else {}
        record = {
            "judge_id": anon,
            "wrong_root_cause": bool(verdict.get("wrong_root_cause")),
            "evidence": verdict.get("evidence", ""),
            "peeky_wrong_value": extra.get("peeky_wrong_value"),
            "peeky_evidence": extra.get("peeky_evidence", ""),
        }
        target = out / bug / "{}-{}.json".format(arm, rep)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record, indent=1) + "\n")
    print("{} verdicts; missing: {}".format(len(key) - len(missing), ", ".join(missing) or "none"))


if __name__ == "__main__":
    main()
