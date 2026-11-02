"""Round 3's candidate order, fixed before any candidate is built or screened (value-study-3).

Pools (instances, after removing rounds 1 and 2's 29 bugs):
- Python: SWE-bench Verified.
- C, C++, Java and Rust: SWE-bench Multilingual and Multi-SWE-bench, one record per instance id
  (Multilingual's where both have it).

Public difficulty. For each benchmark (Verified, Multilingual, and Multi-SWE-bench per language),
the frontier is the top quarter of its public submissions by instances resolved, at least 8 of
them. An instance's public rate is the share of its benchmark's frontier that resolved it
(Multilingual's where both have it). A candidate must be:
- hard: public rate at most 0.5;
- solvable: resolved by at least one public submission of its benchmark, any one.

Tiers. Tier 1 is a public rate of at most 0.125 (at most 1 in 8 of the frontier); tier 2 is the
rest, up to 0.5. Rounds 1 and 2 showed this model is far stronger than most public submissions:
round 2's control solved 31 of 39 attempts on the 13 earlier bugs at 0.5 or below, and 7 of 12 on
the 4 at 0.125 or below. Tier 1 comes first.

Runtime tag. A candidate whose problem statement names runtime state, by the word lists below
(values in loops, concurrency and ordering, serialization, numbers, state across calls), is
tagged. Within a tier, tagged candidates come first.

Order. Within a language, tier and tag, repositories are shuffled, each repository's instances
are shuffled, and the lists are interleaved round-robin, so no repository fills a language. Seed
20261102. The first QUOTA[language] candidates in that order are the pool; the rest are kept as
the reserve, in order.

Usage: select_candidates.py <data dir> <public results.json> <out dir>
<data dir> holds verified.parquet, multilingual.parquet and mswe/<lang>/*.jsonl."""

import collections
import json
import random
import re
import sys
from pathlib import Path

import pandas as pd

SEED = 20261102
TIER_1 = 0.125
QUOTA = {"python": 20, "java": 16, "c": 8, "cpp": 8, "rust": 16}
EARLIER = """
astropy__astropy-13398 django__django-11087 mwaskom__seaborn-3069 mwaskom__seaborn-3187
pallets__flask-5014 psf__requests-6028 pydata__xarray-6599 sphinx-doc__sphinx-10614
sphinx-doc__sphinx-7889 apache__druid-15402 apache__lucene-12022 apache__lucene-13704
projectlombok__lombok-3009 projectlombok__lombok-3052 projectlombok__lombok-3326
reactivex__rxjava-7597 jqlang__jq-2650 micropython__micropython-13569 valkey-io__valkey-1842
valkey-io__valkey-790 fmtlib__fmt-3158 fmtlib__fmt-3272 nlohmann__json-4237
burntsushi__ripgrep-2209 sharkdp__bat-3108 tokio-rs__axum-1934 tokio-rs__tokio-4867
tokio-rs__tokio-6752 uutils__coreutils-6690
""".split()
MULTILINGUAL_LANGUAGES = {
    "c": {"redis/redis", "jqlang/jq", "micropython/micropython", "valkey-io/valkey"},
    "cpp": {"fmtlib/fmt", "nlohmann/json"},
    "java": {
        "projectlombok/lombok",
        "apache/lucene",
        "google/gson",
        "apache/druid",
        "javaparser/javaparser",
        "reactivex/rxjava",
    },
    "rust": {
        "tokio-rs/tokio",
        "sharkdp/bat",
        "tokio-rs/axum",
        "astral-sh/ruff",
        "nushell/nushell",
        "uutils/coreutils",
        "burntsushi/ripgrep",
    },
}
MSWE_LANGUAGES = {"c": "c", "cpp": "cpp", "java": "java", "rust": "rust"}
MSWE_BENCH = {"c": "mswe-c", "cpp": "mswe-c++", "java": "mswe-java", "rust": "mswe-rust"}
RUNTIME = {
    "loops and values": r"\b(loop|iterat\w*|off[- ]by[- ]one|index(es|ing)?|wrong (value|result|output)s?|"
    r"incorrect (value|result|output)s?|unexpected (value|result|output)s?)\b",
    "concurrency and ordering": r"\b(thread\w*|concurren\w*|race|deadlock\w*|async\w*|await\w*|parallel\w*|"
    r"mutex\w*|atomic\w*|lock(s|ed|ing)?|order(ing)?|hang\w*)\b",
    "serialization": r"\b(seriali[sz]\w*|deseriali[sz]\w*|marshal\w*|encod\w*|decod\w*|round[- ]?trip\w*)\b",
    "numbers": r"\b(precision|rounding|float\w*|double|overflow\w*|underflow\w*|nan|infinity|decimal\w*|"
    r"epsilon|truncat\w*)\b",
    "state across calls": r"\b(cache[ds]?|caching|stale|state(ful)?|second (time|call)|subsequent|"
    r"repeated(ly)?|reus\w*|mutat\w*|side[- ]effect\w*)\b",
}


def frontier_rates(public):
    """{benchmark: {instance: rate}} over each benchmark's frontier, and {benchmark: set of
    instances any submission resolved}."""
    by_bench = collections.defaultdict(dict)
    for key, value in public.items():
        bench, submission = key.split("|", 1)
        by_bench[bench][submission] = set(value["resolved"])
    rates, solved = {}, {}
    for bench, subs in by_bench.items():
        ranked = sorted(subs, key=lambda s: (-len(subs[s]), s))
        frontier = ranked[: max(8, len(ranked) // 4)]
        counts = collections.Counter(i for s in frontier for i in subs[s])
        rates[bench] = {i: counts[i] / len(frontier) for i in counts}
        rates[bench]["__frontier__"] = len(frontier)
        solved[bench] = set().union(*subs.values())
    return rates, solved


def runtime_tags(text):
    return sorted(name for name, pattern in RUNTIME.items() if re.search(pattern, text or "", re.I))


def records(data):
    """[{instance, language, repo, bench, problem}] from the three datasets."""
    out = []
    verified = pd.read_parquet(data / "verified.parquet")
    for row in verified.itertuples():
        out.append(
            dict(
                instance=row.instance_id,
                language="python",
                repo=row.repo,
                bench="verified",
                problem=row.problem_statement,
            )
        )
    multilingual = pd.read_parquet(data / "multilingual.parquet")
    for row in multilingual.itertuples():
        for language, repos in MULTILINGUAL_LANGUAGES.items():
            if row.repo in repos:
                out.append(
                    dict(
                        instance=row.instance_id,
                        language=language,
                        repo=row.repo,
                        bench="multilingual",
                        problem=row.problem_statement,
                    )
                )
    seen = {r["instance"] for r in out}
    for language, folder in MSWE_LANGUAGES.items():
        for path in sorted((data / "mswe" / folder).glob("*.jsonl")):
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if row["instance_id"] in seen:
                    continue
                seen.add(row["instance_id"])
                issues = " ".join(
                    (i.get("title") or "") + "\n" + (i.get("body") or "")
                    for i in row.get("resolved_issues") or []
                )
                out.append(
                    dict(
                        instance=row["instance_id"],
                        language=language,
                        repo=f"{row['org']}/{row['repo']}",
                        bench=MSWE_BENCH[language],
                        problem=issues or row.get("body") or "",
                    )
                )
    return out


def interleave(rows, rng):
    by_repo = collections.defaultdict(list)
    for row in rows:
        by_repo[row["repo"]].append(row)
    queues = []
    for repo in sorted(by_repo):
        queue = sorted(by_repo[repo], key=lambda r: r["instance"])
        rng.shuffle(queue)
        queues.append(queue)
    rng.shuffle(queues)
    order = []
    while any(queues):
        for queue in queues:
            if queue:
                order.append(queue.pop(0))
    return order


def main(data, public, out):
    data, out = Path(data), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rates, solved = frontier_rates(json.loads(Path(public).read_text()))
    rows, dropped = [], collections.Counter()
    for row in records(data):
        if row["instance"] in EARLIER:
            dropped["rounds 1 and 2"] += 1
            continue
        bench = row["bench"]
        row["public_rate"] = rates[bench].get(row["instance"], 0.0)
        row["frontier"] = rates[bench]["__frontier__"]
        if row["instance"] not in solved[bench]:
            dropped["no public submission resolved it"] += 1
            continue
        if row["public_rate"] > 0.5:
            dropped["public rate above 0.5"] += 1
            continue
        row["tags"] = runtime_tags(row["problem"])
        rows.append(row)
    rng = random.Random(SEED)
    lines = ["instance\tlanguage\trepo\tbenchmark\tpublic_rate\tfrontier\ttags\tpool"]
    summary = {}
    for language in ("python", "java", "c", "cpp", "rust"):
        mine = [r for r in rows if r["language"] == language]
        order = []
        for tier in (1, 2):
            mine_tier = [r for r in mine if (r["public_rate"] <= TIER_1) == (tier == 1)]
            order += interleave([r for r in mine_tier if r["tags"]], rng)
            order += interleave([r for r in mine_tier if not r["tags"]], rng)
        pool = order[: QUOTA[language]]
        (out / f"{language}.txt").write_text("".join(r["instance"] + "\n" for r in pool))
        (out / f"{language}-reserve.txt").write_text(
            "".join(r["instance"] + "\n" for r in order[QUOTA[language] :])
        )
        for n, r in enumerate(order):
            lines.append(
                "\t".join(
                    [
                        r["instance"],
                        language,
                        r["repo"],
                        r["bench"],
                        "{:.2f}".format(r["public_rate"]),
                        str(r["frontier"]),
                        ",".join(r["tags"]),
                        "pool" if n < QUOTA[language] else "reserve",
                    ]
                )
            )
        summary[language] = {
            "eligible": len(mine),
            "tier 1": sum(1 for r in mine if r["public_rate"] <= TIER_1),
            "tagged": sum(1 for r in mine if r["tags"]),
            "pool": len(pool),
        }
    (out / "eligible.tsv").write_text("\n".join(lines) + "\n")
    (out / "summary.json").write_text(
        json.dumps({"dropped": dropped, "languages": summary}, indent=1) + "\n"
    )
    print(json.dumps({"dropped": dropped, "languages": summary}, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:4])
