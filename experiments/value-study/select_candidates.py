"""The study's candidate order, fixed before any bug is examined (issue #441).

Pools: SWE-bench Verified for Python; SWE-bench Multilingual for C, C++, Java and Rust. Within each
language the repositories are shuffled, each repository's instances are shuffled, and the lists
are interleaved round-robin, so one large repository (django) doesn't fill a language. The seed is
441, the issue's number. Candidates are then examined in this order (see README.md, "Selection").

Usage: python select_candidates.py VERIFIED.parquet MULTILINGUAL.parquet OUTDIR
"""

import random
import sys
from pathlib import Path

import pandas as pd

SEED = 441
LANGUAGES = {
    "c": ["redis/redis", "jqlang/jq", "micropython/micropython", "valkey-io/valkey"],
    "cpp": ["fmtlib/fmt", "nlohmann/json"],
    "java": [
        "projectlombok/lombok",
        "apache/lucene",
        "google/gson",
        "apache/druid",
        "javaparser/javaparser",
        "reactivex/rxjava",
    ],
    "rust": [
        "tokio-rs/tokio",
        "sharkdp/bat",
        "tokio-rs/axum",
        "astral-sh/ruff",
        "nushell/nushell",
        "uutils/coreutils",
        "burntsushi/ripgrep",
    ],
}


def interleave(frame, repos, rng):
    queues = []
    for repo in sorted(repos):
        ids = sorted(frame[frame.repo == repo].instance_id)
        rng.shuffle(ids)
        queues.append(ids)
    rng.shuffle(queues)
    order = []
    while any(queues):
        for queue in queues:
            if queue:
                order.append(queue.pop(0))
    return order


def main(verified, multilingual, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    pools = {"python": (pd.read_parquet(verified), None)}
    ml = pd.read_parquet(multilingual)
    for language, repos in LANGUAGES.items():
        pools[language] = (ml, repos)
    for language in ("python", "c", "cpp", "java", "rust"):
        frame, repos = pools[language]
        order = interleave(frame, repos or sorted(set(frame.repo)), rng)
        (out / f"{language}.txt").write_text("\n".join(order) + "\n")
        print(language, len(order))


if __name__ == "__main__":
    main(*sys.argv[1:4])
