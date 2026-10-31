"""Prepare blind judge inputs: one anonymised file per attempt, and the key to map verdicts back.

Usage: python prepare_judging.py RESULTS_RUNS OUTDIR
Writes OUTDIR/inputs/<anon>.md (part 1, blind), OUTDIR/inputs-peeky/<bug>__<rep>.md (part 2, for
Peeky-arm attempts that ran Peeky), OUTDIR/key.json ({anon: [bug, arm, rep]}) and
OUTDIR/batches/<n>.txt (anon ids, shuffled with seed 441).
"""

import json
import random
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(runs, out, batches=6):
    runs, out = Path(runs), Path(out)
    (out / "inputs").mkdir(parents=True, exist_ok=True)
    (out / "inputs-peeky").mkdir(parents=True, exist_ok=True)
    (out / "batches").mkdir(parents=True, exist_ok=True)
    attempts = sorted(p for p in runs.glob("*/*") if (p / "result.json").exists())
    rng = random.Random(441)
    rng.shuffle(attempts)
    key = {}
    for n, run in enumerate(attempts, 1):
        result = json.loads((run / "result.json").read_text())
        anon = f"a{n:03d}"
        key[anon] = [result["bug"], result["arm"], result["rep"]]
        text = subprocess.run(
            [sys.executable, str(HERE / "judge_input.py"), str(run)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        (out / "inputs" / f"{anon}.md").write_text(text)
        if result["arm"] == "peeky" and result.get("peeky_commands"):
            text = subprocess.run(
                [sys.executable, str(HERE / "judge_input.py"), str(run), "--peeky"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            (out / "inputs-peeky" / f"{result['bug']}__{result['rep']}.md").write_text(text)
    (out / "key.json").write_text(json.dumps(key, indent=1))
    ids = list(key)
    for b in range(batches):
        (out / "batches" / f"{b + 1}.txt").write_text("\n".join(ids[b::batches]) + "\n")
    print(len(key), "attempts;", len(list((out / "inputs-peeky").iterdir())), "with Peeky")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
