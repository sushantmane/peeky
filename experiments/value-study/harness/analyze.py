#!/usr/bin/env python3
"""Reads the attempts' result.json files and bug.yaml files; writes results/summary.json and
prints the report's tables as Markdown.

    harness/analyze.py [--results DIR] [--bugs DIR] [--judge DIR] [--out FILE]

Valid attempts are those that aren't infrastructure failures and aren't contaminated (a
control attempt that ran Peeky). A bug's solve rate in an arm is the share of its valid
attempts solved; an arm's solve rate is the mean over bugs, with a 95% bootstrap interval that
resamples bugs (10,000 resamples, seed 441). The paired difference is the mean over bugs with
valid attempts in both arms of (peeky rate - control rate), with the same bootstrap.

Judge verdicts, where results/judge/<bug>/<arm>-<rep>.json exists with
{"wrong_root_cause": bool, "evidence": str, "peeky_wrong_value": bool|null}, add the share of
judged attempts that stated a wrong root cause, per arm."""

import argparse
import json
import random
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import studylib  # noqa: E402

SEED = studylib.SEED
RESAMPLES = 10000
EXACT = studylib.CONFIG_PATH is not None
RUN_NAME = re.compile(
    r"^({})-(\d+)$".format(
        "|".join(re.escape(a) for a in sorted(studylib.ARMS, key=len, reverse=True))
    )
)


def load_runs(results):
    runs = []
    for bug_dir in sorted(Path(results).iterdir()) if Path(results).is_dir() else []:
        if not bug_dir.is_dir():
            continue
        for run_dir in sorted(bug_dir.iterdir()):
            match = RUN_NAME.match(run_dir.name)
            if not match or not (run_dir / "result.json").is_file():
                continue
            try:
                result = json.loads((run_dir / "result.json").read_text())
            except ValueError:
                continue
            result.setdefault("bug", bug_dir.name)
            result.setdefault("arm", match.group(1))
            result.setdefault("rep", int(match.group(2)))
            runs.append(result)
    return runs


def percentile(values, q):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * q
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def bootstrap(per_bug, statistic=statistics.mean, resamples=RESAMPLES, seed=SEED):
    """95% interval of statistic(values) when the bugs (the values) are resampled."""
    values = list(per_bug)
    if not values:
        return None, None
    rng = random.Random(seed)
    n = len(values)
    estimates = [statistic([values[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples)]
    return percentile(estimates, 0.025), percentile(estimates, 0.975)


def describe(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "median": None, "q1": None, "q3": None, "mean": None, "sd": None}
    return {
        "n": len(values),
        "median": percentile(values, 0.5),
        "q1": percentile(values, 0.25),
        "q3": percentile(values, 0.75),
        "mean": statistics.mean(values),
        "sd": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def rnd(value, digits=2):
    # Round 1 kept its summary's values rounded, and printed some of them from those, rounding
    # twice (13.746 to 13.75 to 13.8). With a study config, values stay exact until they are
    # printed, so each is rounded once; without one, round 1's output stays as it was.
    if value is None or EXACT:
        return value
    return round(value, digits)


def fmt(value, digits=1, percent=False):
    if value is None:
        return "-"
    return (
        "{:.{}f}%".format(value * 100, digits - 1 if digits else 0)
        if percent
        else "{:.{}f}".format(value, digits)
    )


def solve_rates(runs, bugs):
    """{bug: {arm: rate}} over valid attempts."""
    rates = defaultdict(dict)
    grouped = defaultdict(list)
    for run in runs:
        grouped[(run["bug"], run["arm"])].append(bool(run.get("solved")))
    for (bug, arm), solved in grouped.items():
        if bug in bugs:
            rates[bug][arm] = sum(solved) / len(solved)
    return rates


def paired(rates, members, treatment="peeky", base="control"):
    """The mean over bugs of (treatment rate - base rate), with each arm's own mean rate."""
    differences = [
        rates[b][treatment] - rates[b][base]
        for b in members
        if treatment in rates[b] and base in rates[b]
    ]
    low, high = bootstrap(differences)
    base_rates = [rates[b][base] for b in members if base in rates[b]]
    treated = [rates[b][treatment] for b in members if treatment in rates[b]]
    return {
        "bugs": len(differences),
        base + "_rate": rnd(statistics.mean(base_rates), 4) if base_rates else None,
        base + "_ci": [rnd(x, 4) for x in bootstrap(base_rates)],
        treatment + "_rate": rnd(statistics.mean(treated), 4) if treated else None,
        treatment + "_ci": [rnd(x, 4) for x in bootstrap(treated)],
        "difference": rnd(statistics.mean(differences), 4) if differences else None,
        "difference_ci": [rnd(low, 4), rnd(high, 4)],
    }


def paired_cost(runs, bugs, treatment="peeky", base="control"):
    """Per measure, the median over bugs of (the bug's median in treatment - its median in
    base), with a bootstrap interval."""
    cost = {}
    for key in ("wall_seconds", "ai_credits", "tool_calls"):
        differences = []
        for bug in bugs:
            medians = {}
            for arm in (treatment, base):
                values = [
                    r.get(key)
                    for r in runs
                    if r["bug"] == bug and r["arm"] == arm and r.get(key) is not None
                ]
                if values:
                    medians[arm] = percentile(values, 0.5)
            if len(medians) == 2:
                differences.append(medians[treatment] - medians[base])
        low, high = bootstrap(differences, statistic=lambda xs: percentile(xs, 0.5))
        cost[key] = {
            "bugs": len(differences),
            "median_difference": rnd(percentile(differences, 0.5), 2) if differences else None,
            "ci": [rnd(low, 2), rnd(high, 2)],
            treatment + "_higher": sum(d > 0 for d in differences),
        }
    return cost


def comparison(runs, rates, bugs, meta, treatment, base):
    return {
        "treatment": treatment,
        "base": base,
        "paired": paired(rates, bugs, treatment, base),
        "paired_cost": paired_cost(runs, bugs, treatment, base),
        "by_category": {
            c: paired(rates, [b for b in bugs if meta[b]["category"] == c], treatment, base)
            for c in sorted({meta[b]["category"] for b in bugs})
        },
        "by_language": {
            lang: paired(rates, [b for b in bugs if meta[b]["language"] == lang], treatment, base)
            for lang in sorted({meta[b]["language"] for b in bugs})
        },
    }


def peeky_usage(arm_runs):
    subcommands, markers = Counter(), Counter()
    for run in arm_runs:
        subcommands.update(run.get("peeky_subcommands") or {})
        markers.update(run.get("peeky_withheld_by_marker") or {})
    ran = [r for r in arm_runs if r.get("peeky_commands")]
    usage = {
        "peeky_attempts": len(arm_runs),
        "ran_peeky": len(ran),
        "ran_peeky_share": rnd(len(ran) / len(arm_runs), 4) if arm_runs else None,
        "read_docs": sum(bool(r.get("peeky_doc_reads")) for r in arm_runs),
        "commands": sum(len(r.get("peeky_commands") or []) for r in arm_runs),
        "commands_per_attempt": describe([len(r.get("peeky_commands") or []) for r in arm_runs]),
        "subcommands": dict(subcommands.most_common()),
        "withheld_markers": sum(markers.values()),
        "withheld_by_marker": dict(markers.most_common()),
        "solved_when_ran_peeky": [sum(bool(r.get("solved")) for r in ran), len(ran)],
        "solved_when_not": [
            sum(bool(r.get("solved")) for r in arm_runs if not r.get("peeky_commands")),
            sum(1 for r in arm_runs if not r.get("peeky_commands")),
        ],
    }
    if studylib.EXTENDED:
        printed = [r.get("peeky_values_printed") or 0 for r in arm_runs]
        usage["values_printed"] = sum(printed)
        usage["values_printed_per_attempt"] = describe(printed)
        usage["attempts_with_values"] = sum(n > 0 for n in printed)
    return usage


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", default=str(studylib.RUNS))
    parser.add_argument("--bugs", default=str(studylib.BUGS))
    parser.add_argument("--judge", help="default: judge/ beside the results directory")
    parser.add_argument("--out", help="default: summary.json beside the results directory")
    args = parser.parse_args()
    results = Path(args.results)
    judge_dir = Path(args.judge) if args.judge else results.parent / "judge"
    out = Path(args.out) if args.out else results.parent / "summary.json"

    all_runs = load_runs(results)
    meta = {}
    for run in all_runs:
        if run["bug"] not in meta:
            path = Path(args.bugs) / run["bug"] / "bug.yaml"
            data = studylib.load_yaml(path) if path.is_file() else {}
            meta[run["bug"]] = {
                "category": data.get("category") or run.get("category") or "?",
                "language": data.get("language") or run.get("language") or "?",
            }
    excluded, runs = [], []
    for run in all_runs:
        reasons = [
            r
            for r, flag in (
                ("infrastructure failure", run.get("infra_failure")),
                ("contaminated", run.get("contaminated")),
            )
            if flag
        ]
        if reasons:
            excluded.append(
                {
                    "bug": run["bug"],
                    "arm": run["arm"],
                    "rep": run["rep"],
                    "reasons": reasons,
                    "errors": (run.get("errors") or [])[:3],
                }
            )
        else:
            runs.append(run)
    bugs = sorted({run["bug"] for run in runs})
    rates = solve_rates(runs, set(bugs))
    fake = sorted({run.get("fake_agent") for run in runs if run.get("fake_agent")})

    # Per bug and arm.
    per_bug = []
    for bug in bugs:
        for arm in studylib.ARMS:
            group = [r for r in runs if r["bug"] == bug and r["arm"] == arm]
            if not group:
                continue
            per_bug.append(
                {
                    "bug": bug,
                    "arm": arm,
                    **meta[bug],
                    "solved": sum(bool(r.get("solved")) for r in group),
                    "n": len(group),
                    "median_wall_seconds": rnd(
                        percentile([r.get("wall_seconds") or 0 for r in group], 0.5), 1
                    ),
                    "median_ai_credits": rnd(
                        percentile(
                            [r["ai_credits"] for r in group if r.get("ai_credits") is not None], 0.5
                        )
                    ),
                    "median_tool_calls": percentile([r.get("tool_calls") or 0 for r in group], 0.5),
                    "median_build_or_test_runs": percentile(
                        [r.get("build_or_test_runs") or 0 for r in group], 0.5
                    ),
                }
            )

    # Per arm.
    per_arm = {}
    for arm in studylib.ARMS:
        group = [r for r in runs if r["arm"] == arm]
        arm_rates = [rates[b][arm] for b in bugs if arm in rates[b]]
        low, high = bootstrap(arm_rates)
        per_arm[arm] = {
            "attempts": len(group),
            "bugs": len(arm_rates),
            "solved": sum(bool(r.get("solved")) for r in group),
            "solve_rate": rnd(statistics.mean(arm_rates), 4) if arm_rates else None,
            "solve_rate_ci": [rnd(low, 4), rnd(high, 4)],
            "wall_seconds": {
                k: rnd(v, 1) for k, v in describe([r.get("wall_seconds") for r in group]).items()
            },
            "ai_credits": {
                k: rnd(v, 2) for k, v in describe([r.get("ai_credits") for r in group]).items()
            },
            "tool_calls": {
                k: rnd(v, 1) for k, v in describe([r.get("tool_calls") for r in group]).items()
            },
            "build_or_test_runs": {
                k: rnd(v, 1)
                for k, v in describe([r.get("build_or_test_runs") for r in group]).items()
            },
            "timed_out": sum(bool(r.get("timed_out")) for r in group),
        }

    # The first comparison is the primary outcome; round 1 had only peeky - control.
    comparisons = [comparison(runs, rates, bugs, meta, t, b) for t, b in studylib.COMPARISONS]
    primary = comparisons[0]
    peeky_arms = [a for a in studylib.ARMS if studylib.arm_uses_peeky(a)]
    usage_by_arm = {a: peeky_usage([r for r in runs if r["arm"] == a]) for a in peeky_arms}
    usage = usage_by_arm.get("peeky") or (usage_by_arm[peeky_arms[0]] if peeky_arms else {})

    judged = {}
    for run in runs:
        path = judge_dir / run["bug"] / "{}-{}.json".format(run["arm"], run["rep"])
        if path.is_file():
            try:
                judged[(run["bug"], run["arm"], run["rep"])] = json.loads(path.read_text())
            except ValueError:
                pass
    judge = None
    if judged:
        judge = {}
        for arm in studylib.ARMS:
            verdicts = [v for (b, a, r), v in judged.items() if a == arm]
            wrong = sum(bool(v.get("wrong_root_cause")) for v in verdicts)
            judge[arm] = {
                "judged": len(verdicts),
                "wrong_root_cause": wrong,
                "wrong_root_cause_share": rnd(wrong / len(verdicts), 4) if verdicts else None,
            }
        values = [v.get("peeky_wrong_value") for (b, a, r), v in judged.items() if a in peeky_arms]
        judge["peeky_wrong_value"] = {
            "checked": sum(v is not None for v in values),
            "wrong": sum(v is True for v in values),
        }

    summary = {
        "attempts": len(all_runs),
        "valid": len(runs),
        "bugs": len(bugs),
        "fake_agent_runs": fake,
        "seed": SEED,
        "resamples": RESAMPLES,
        "copilot_versions": sorted(
            {r.get("copilot_version") for r in runs if r.get("copilot_version")}
        ),
        "models": sorted({m for r in runs for m in (r.get("models") or [])}),
        "peeky_shas": sorted({r.get("peeky_sha") for r in runs if r.get("peeky_sha")}),
        "per_bug": per_bug,
        "per_arm": per_arm,
        "paired": primary["paired"],
        "paired_cost": primary["paired_cost"],
        "by_category": primary["by_category"],
        "by_language": primary["by_language"],
        "peeky_usage": usage,
        "judge": judge,
        "excluded": excluded,
    }
    if studylib.EXTENDED:
        summary["comparisons"] = comparisons
        summary["peeky_usage_by_arm"] = usage_by_arm
    out.parent.mkdir(parents=True, exist_ok=True)
    studylib.write_json(out, summary)
    print_report(summary)


def print_report(s):
    out = print
    if s["fake_agent_runs"]:
        out(
            "**Note:** these results include fake-agent runs ({}).\n".format(
                ", ".join(s["fake_agent_runs"])
            )
        )
    out(
        "{} attempts, {} valid, {} bugs. Copilot CLI {}; models {}; Peeky {}.\n".format(
            s["attempts"],
            s["valid"],
            s["bugs"],
            ", ".join(s["copilot_versions"]) or "-",
            ", ".join(s["models"]) or "-",
            ", ".join(x[:12] for x in s["peeky_shas"]) or "-",
        )
    )
    out("### Per bug and arm\n")
    out(
        "| Bug | Cat. | Lang. | Arm | Solved | Median wall (s) | Median credits | Median tool calls "
        "| Median build/test runs |"
    )
    out("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in s["per_bug"]:
        out(
            "| {bug} | {category} | {language} | {arm} | {solved}/{n} | {w} | {c} | {t} | {b} |".format(
                w=fmt(row["median_wall_seconds"], 0),
                c=fmt(row["median_ai_credits"], 1),
                t=fmt(row["median_tool_calls"], 0),
                b=fmt(row["median_build_or_test_runs"], 0),
                **row,
            )
        )
    out("\n### Per arm\n")
    out(
        "| Arm | Attempts | Solved | Solve rate (95% CI) | Wall s: median [IQR] | Wall s: mean ± sd | "
        "Credits: median [IQR] | Credits: mean ± sd | Timed out |"
    )
    out("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for arm, a in s["per_arm"].items():
        w, c = a["wall_seconds"], a["ai_credits"]
        out(
            "| {} | {} | {} | {} ({}–{}) | {} [{}–{}] | {} ± {} | {} [{}–{}] | {} ± {} | {} |".format(
                arm,
                a["attempts"],
                a["solved"],
                fmt(a["solve_rate"], 1, True),
                fmt(a["solve_rate_ci"][0], 1, True),
                fmt(a["solve_rate_ci"][1], 1, True),
                fmt(w["median"], 0),
                fmt(w["q1"], 0),
                fmt(w["q3"], 0),
                fmt(w["mean"], 0),
                fmt(w["sd"], 0),
                fmt(c["median"], 1),
                fmt(c["q1"], 1),
                fmt(c["q3"], 1),
                fmt(c["mean"], 1),
                fmt(c["sd"], 1),
                a["timed_out"],
            )
        )
    comparisons = s.get("comparisons") or [
        {
            "treatment": "peeky",
            "base": "control",
            "paired": s["paired"],
            "paired_cost": s["paired_cost"],
            "by_category": s["by_category"],
            "by_language": s["by_language"],
        }
    ]
    for c in comparisons:
        print_comparison(out, c)
    usages = s.get("peeky_usage_by_arm") or {"peeky": s["peeky_usage"]}
    for arm, u in usages.items():
        print_usage(out, arm, u)
    print_judge(out, s)


def arm_label(arm):
    return {"peeky": "Peeky", "control": "control"}.get(arm, arm)


def print_comparison(out, c):
    t, b = c["treatment"], c["base"]
    out("\n### Paired difference in solve rate ({} − {})\n".format(arm_label(t), arm_label(b)))
    out(
        "| Group | Bugs | {} | {} | Difference (95% CI) |".format(
            arm_label(b).capitalize(), arm_label(t)
        )
    )
    out("| --- | --- | --- | --- | --- |")

    def row(label, p):
        out(
            "| {} | {} | {} | {} | {} ({} to {}) |".format(
                label,
                p["bugs"],
                fmt(p[b + "_rate"], 1, True),
                fmt(p[t + "_rate"], 1, True),
                "-" if p["difference"] is None else "{:+.0f} pp".format(p["difference"] * 100),
                "-"
                if p["difference_ci"][0] is None
                else "{:+.0f}".format(p["difference_ci"][0] * 100),
                "-"
                if p["difference_ci"][1] is None
                else "{:+.0f}".format(p["difference_ci"][1] * 100),
            )
        )

    row("All", c["paired"])
    for category, p in c["by_category"].items():
        row("Category " + category, p)
    for language, p in c["by_language"].items():
        row(language, p)
    out(
        "\n### Paired difference in cost per bug ({} − {}, each bug's medians)\n".format(
            arm_label(t), arm_label(b)
        )
    )
    more = "Peeky's arm" if t == "peeky" else "the {} arm".format(t)
    out("| Measure | Bugs | Median difference (95% CI) | Bugs where {} cost more |".format(more))
    out("| --- | --- | --- | --- |")
    for key, label in (
        ("wall_seconds", "Wall time (s)"),
        ("ai_credits", "AI credits"),
        ("tool_calls", "Tool calls"),
    ):
        k = c["paired_cost"][key]
        out(
            "| {} | {} | {:+.1f} ({:+.1f} to {:+.1f}) | {} |".format(
                label,
                k["bugs"],
                k["median_difference"],
                k["ci"][0],
                k["ci"][1],
                k[t + "_higher"],
            )
        )


def print_usage(out, arm, u):
    out("\n### Peeky usage ({} arm)\n".format(arm_label(arm)))
    out(
        "- Attempts that ran Peeky: {}/{} ({}); read its docs: {}.".format(
            u["ran_peeky"], u["peeky_attempts"], fmt(u["ran_peeky_share"], 1, True), u["read_docs"]
        )
    )
    out(
        "- Commands: {} (median per attempt {}). By subcommand: {}.".format(
            u["commands"],
            fmt(u["commands_per_attempt"]["median"], 0),
            ", ".join("`{}` {}".format(k, v) for k, v in u["subcommands"].items()) or "-",
        )
    )
    out(
        "- Withheld-value markers in Peeky's output: {} ({}).".format(
            u["withheld_markers"],
            ", ".join("`{}` {}".format(k, v) for k, v in u["withheld_by_marker"].items()) or "-",
        )
    )
    out(
        "- Solved when Peeky ran: {}/{}; when it didn't: {}/{}.".format(
            *u["solved_when_ran_peeky"], *u["solved_when_not"]
        )
    )
    if "values_printed" in u:
        out(
            "- Program values Peeky printed: {} (median per attempt {}); attempts with any: {}.".format(
                u["values_printed"],
                fmt(u["values_printed_per_attempt"]["median"], 0),
                u["attempts_with_values"],
            )
        )


def print_judge(out, s):
    if s["judge"]:
        out("\n### Wrong root causes (blind judge)\n")
        out("| Arm | Judged | Wrong root cause |")
        out("| --- | --- | --- |")
        for arm in studylib.ARMS:
            j = s["judge"][arm]
            out(
                "| {} | {} | {} ({}) |".format(
                    arm,
                    j["judged"],
                    j["wrong_root_cause"],
                    fmt(j["wrong_root_cause_share"], 1, True),
                )
            )
        pv = s["judge"]["peeky_wrong_value"]
        out(
            "\nPeeky printed a value the program contradicted in {} of {} checked attempts.".format(
                pv["wrong"], pv["checked"]
            )
        )
    out("\n### Excluded attempts\n")
    if not s["excluded"]:
        out("None.")
    for e in s["excluded"]:
        out("- {} {} {}: {}".format(e["bug"], e["arm"], e["rep"], ", ".join(e["reasons"])))


if __name__ == "__main__":
    main()
