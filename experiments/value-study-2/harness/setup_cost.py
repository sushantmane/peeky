#!/usr/bin/env python3
"""Where `peeky-first` attempts spent their tool calls and AI credits before Peeky first stopped:

    harness/setup_cost.py --runs <raw runs dir> [--write results/setup_cost.json]
    harness/setup_cost.py [--from results/setup_cost.json]

With --runs, reads each `peeky-first` attempt's session events (session-events.jsonl.gz, kept
outside the repository) and classifies every tool call up to the first one that shows a Peeky
stop. --write saves the per-call records, host paths removed, so the tables can be made again
from results/setup_cost.json alone (the default input). Prints the tables in Markdown.

A tool call's category is the first of these that applies:

- `stop`: the call whose output shows Peeky's first stop (`peeky: hit N/`, `state: stopped at`,
  or a JSON answer with hits).
- `peeky_refused`: a Peeky command that printed an error code, a usage error, or ended with no
  stop.
- `peeky_other`: another Peeky command (`describe`, `build`, `sql`, a session's `get`).
- `debuggee`: starting a program for Peeky to attach to (JDWP's agent, Maven's
  `-Dmaven.surefire.debug`, Gradle's `--debug-jvm`, `JAVA_TOOL_OPTIONS`), or finding the
  classpath to start it with.
- `config`: writing a Peeky config by hand (Java attach, Rust workspace, native).
- `docs`: reading Peeky's docs, its `--help` or its source.
- `build_test`: the project's build or tests (the study's own matcher, metrics.py).
- `wait`: read_bash, stop_bash or sleep, waiting on a command started in the background.
- `code`: anything else, mostly reading the project.

Credits: each model call's AI credits (the session's usage records) are split evenly over the
tool calls it asked for. "Credits before the first stop" sums the model calls up to the one that
asked for the stopping call. Python 3.9+, standard library only, with round 1's metrics.py."""

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUND2 = HERE.parent
ROUND1 = ROUND2.parent / "value-study"
sys.path.insert(0, str(ROUND1 / "harness"))
import metrics  # noqa: E402
import studylib  # noqa: E402

ARM = "peeky-first"
# The Peeky commit the study's images were built from (README, "The Peeky image").
STUDY_COMMIT = "fef02c78b57b1ea9095f975a492640520c1b7e69"
CATEGORIES = (
    "docs",
    "config",
    "debuggee",
    "peeky_refused",
    "peeky_other",
    "build_test",
    "wait",
    "code",
    "stop",
)
SETUP = ("docs", "config", "debuggee", "peeky_refused", "peeky_other", "wait")

STOP_TEXT = re.compile(r"(?m)^peeky: hit \d+/|state: stopped at |^\s*\d+\s+observed\s+\d+\s")
STOP_JSON = re.compile(r'(?m)^  "hits": [1-9]')
CODE = re.compile(r'(?:error \[|peeky: \[|"code": ")([A-Z][A-Z_]+)')
MESSAGE = re.compile(r'(?:error \[[A-Z_]+\]: |peeky: \[[A-Z_]+\] |"message": ")([^\n"]{1,200})')
USAGE_ERROR = re.compile(r"(?m)^(?:usage: peeky[^\n]*\n(?:.*\n)*?)?peeky(?: [\w-]+)*: error: (.*)$")
NO_HITS = re.compile(r"program finished after 0 hit\(s\)|exited before any hit")
TOO_LARGE = re.compile(r"Output too large to read at once \(([\d.]+) KB\)")
DOC_FILE = re.compile(r"/opt/peeky/docs/([\w./-]+?\.md)|site-packages/(peeky/[\w./-]+)")
PEEKY_SOURCE = re.compile(r"site-packages/peeky\b|/opt/peeky/(?!docs|study)\S")
DEBUGGEE = re.compile(
    r"agentlib:jdwp|-Xrunjdwp|maven\.surefire\.debug|--debug-jvm|JAVA_TOOL_OPTIONS|"
    r"jdk\.jdwp|suspend=y"
)
CLASSPATH = re.compile(
    r"surefire\.test\.class\.path|dependency:build-classpath|mdep\.outputFile|"
    r"printClasspath|runtimeClasspath|testRuntimeClasspath"
)
JAVA_CONFIG = re.compile(r'"port"\s*:|"class_roots"\s*:|"host"\s*:\s*"127')
RUST_CONFIG = re.compile(r'"analyse"\s*:|"workspace"\s*:\s*[\["]')
NATIVE_CONFIG = re.compile(r'"binary"\s*:|"build_dir"\s*:|"compile_commands"\s*:')
WRITES_FILE = re.compile(
    r"cat\s*>|tee\s|>\s*\S+\.json|write_text|json\.dump|open\([^)]*['\"]w|printf[^|]*>"
)
INLINE_CONFIG = re.compile(r"--(?:native-|rust-)?config\s+(?:<\(|/dev/stdin|-\s)")
PEEKY_PROGRAMS_HELP = ("peeky",)
HOST_PATH = re.compile(r"/(?:Users|home)/[^\s'\"]+")


def scrub(text):
    return HOST_PATH.sub("<host>", text)


def runs_peeky(command):
    return bool(metrics.peeky_subcommands(command)) or bool(
        re.search(r"[\"'\s/]peeky[\"']?\s*[,\]]|\bpeeky (probe|session|describe)", command)
    )


def classify(command, output, language, matcher):
    """(category, details) for one bash command and what it printed."""
    subs = metrics.peeky_subcommands(command)
    details = {}
    codes = sorted(set(CODE.findall(output)))
    usage = USAGE_ERROR.search(output)
    if usage:
        codes.append("USAGE: " + usage.group(1)[:80])
    docs = sorted({(a or b) for a, b in DOC_FILE.findall(command)})
    help_only = (
        bool(subs)
        and all(re.search(r"(^|\s)(--help|-h)(\s|$)", command) for _ in subs)
        and not STOP_TEXT.search(output)
    )
    if docs or help_only or PEEKY_SOURCE.search(metrics.strip_heredocs(command)):
        details["docs"] = docs + (["--help"] if help_only else [])
    if INLINE_CONFIG.search(command):
        details["inline_config"] = True
    written = WRITES_FILE.search(command)
    kind = (
        "java"
        if JAVA_CONFIG.search(command)
        else "rust"
        if RUST_CONFIG.search(command)
        else "native"
        if NATIVE_CONFIG.search(command)
        else None
    )
    if kind and (written or details.get("inline_config")):
        details["config"] = kind
    if DEBUGGEE.search(command):
        details["debuggee"] = "start"
        started = {program for program, _ in metrics.invoked_programs(command)}
        details["jvm"] = next(
            (
                kind
                for kind, names in (
                    ("maven", {"mvn", "mvnw"}),
                    ("gradle", {"gradle", "gradlew"}),
                    ("ant", {"ant"}),
                    ("jshell", {"jshell"}),
                    ("javac", {"javac"}),
                    ("java", {"java"}),
                )
                if started & names
            ),
            "other",
        )
    elif CLASSPATH.search(command):
        details["debuggee"] = "classpath"
    peeky = runs_peeky(command) and not help_only
    if peeky:
        details["peeky"] = subs or ["(in a script)"]
        if codes:
            details["codes"] = codes
            details["messages"] = sorted({scrub(m)[:160] for m in MESSAGE.findall(output)})[:4]
        elif NO_HITS.search(output):
            details["codes"] = ["NO_STOP (finished with 0 hits)"]
    if peeky and (STOP_TEXT.search(output) or STOP_JSON.search(output)):
        return "stop", details
    if peeky and details.get("codes"):
        return "peeky_refused", details
    if details.get("debuggee"):
        return "debuggee", details
    if details.get("config") and not peeky:
        return "config", details
    if peeky:
        return "peeky_other", details
    if "docs" in details:
        return "docs", details
    if matcher(command):
        return "build_test", details
    if re.fullmatch(r"\s*sleep [\d.]+\s*", command):
        return "wait", details
    return "code", details


def output_bytes(content):
    """What the model saw of a call's output: a long output is only previewed (TOO_LARGE)."""
    return len(content.encode())


def stamp(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _priced(usage):
    """{token type: (tokens, credits)} of one model call, from its usage record."""
    priced = {}
    for detail in (usage.get("copilotUsage") or {}).get("tokenDetails") or ():
        tokens = detail.get("tokenCount") or 0
        per_token = (detail.get("costPerBatch") or 0) / (detail.get("batchSize") or 1) / 1e9
        kind = detail.get("tokenType")
        before = priced.get(kind, (0, 0.0))
        priced[kind] = (before[0] + tokens, before[1] + tokens * per_token)
    return priced


def extract(run_dir, bug):
    events, _ = metrics.read_jsonl(Path(run_dir) / "session-events.jsonl")
    result = json.loads((Path(run_dir) / "result.json").read_text())
    language = bug["language"].lower()
    matcher = metrics.build_test_matcher(language, bug.get("test_hint", ""))
    completes = {
        e["data"]["toolCallId"]: e for e in events if e.get("type") == "tool.execution_complete"
    }
    start = next(stamp(e["timestamp"]) for e in events if e.get("type") == "user.message")
    model_calls, calls = [], []
    for event in events:
        if event.get("type") == "session.usage_record":
            usage = event["data"]["usage"]
            model_calls.append(
                {
                    "credits": ((usage.get("copilotUsage") or {}).get("totalNanoAiu") or 0) / 1e9,
                    "priced": _priced(usage),
                }
            )
        elif event.get("type") == "tool.execution_start":
            data = event["data"]
            complete = completes.get(data["toolCallId"]) or {}
            result_data = (complete.get("data") or {}).get("result") or {}
            content = (result_data.get("content") or "") if isinstance(result_data, dict) else ""
            when = stamp(complete.get("timestamp", event["timestamp"]))
            calls.append(
                {
                    "tool": data["toolName"],
                    "arguments": data.get("arguments") or {},
                    "output": content,
                    "model_call": len(model_calls) - 1,
                    "seconds": round((when - start).total_seconds(), 1),
                }
            )
    attribute(model_calls, calls)
    by_shell, records, first_stop = {}, [], None
    for n, call in enumerate(calls, 1):
        arguments = call["arguments"]
        command = arguments.get("command") or ""
        if call["tool"] == "bash":
            shown = command
            match = metrics.SHELL_ID.search(call["output"])
            for shell in (arguments.get("shellId"), match and match.group(1)):
                if shell:
                    by_shell[str(shell)] = command
            category, details = classify(command, call["output"], language, matcher)
        else:
            shell_command = by_shell.get(str(arguments.get("shellId")), "")
            shown = "{} {}".format(call["tool"], arguments.get("shellId", ""))
            category, details = "wait", {}
            if runs_peeky(shell_command):
                inner, inner_details = classify(shell_command, call["output"], language, matcher)
                if inner in ("stop", "peeky_refused"):
                    category, details = inner, dict(inner_details, via=call["tool"])
        if "docs" in details:
            # A long output reached the model as a preview: it read the doc again in parts.
            seen = 0 if TOO_LARGE.search(call["output"]) else output_bytes(call["output"])
            details["doc_bytes"] = doc_share(shown, seen)
        record = {
            "n": n,
            "tool": call["tool"],
            "category": category,
            "credits": round(call["cost"], 3),
            "seconds": call["seconds"],
            "output_bytes": output_bytes(call["output"]),
            "command": scrub(metrics.strip_heredocs(shown))[:240],
            **details,
        }
        records.append(record)
        if category == "stop" and first_stop is None:
            first_stop = {
                "n": n,
                "seconds": call["seconds"],
                "credits": round(
                    sum(m["credits"] for m in model_calls[: call["model_call"] + 1]), 3
                ),
                "model_calls": call["model_call"] + 1,
            }
    total = sum(m["credits"] for m in model_calls)
    return {
        "bug": result["bug"],
        "language": language,
        "rep": result["rep"],
        "solved": bool(result.get("solved")),
        "tool_calls": len(calls),
        "model_calls": len(model_calls),
        "ai_credits": round(total, 3),
        "unattributed_credits": round(total - sum(c["cost"] for c in calls), 3),
        "wall_seconds": result.get("wall_seconds"),
        "first_stop": first_stop,
        "calls": records,
    }


def attribute(model_calls, calls):
    """Each tool call's cost: its share of the output of the model call that asked for it, and
    what its output cost in the context after it. The next model call writes the output to the
    cache (its cache-write tokens, split over the outputs by size), and each later model call
    reads it again. The system prompt, the task and the model's own text stay unattributed."""
    issued = {}
    for call in calls:
        issued.setdefault(call["model_call"], []).append(call)
        call["cost"] = 0.0
    for k, mine in issued.items():
        if k < 0:
            continue
        spent = model_calls[k]["priced"].get("output", (0, 0.0))[1]
        for call in mine:
            call["cost"] += spent / len(mine)
        if k + 1 >= len(model_calls):
            continue
        written, written_cost = model_calls[k + 1]["priced"].get("cache_write", (0, 0.0))
        size = sum(output_bytes(c["output"]) for c in mine) or 1
        later_read_price = sum(
            (m["priced"].get("cache_read", (0, 0.0))[1] / m["priced"]["cache_read"][0])
            for m in model_calls[k + 2 :]
            if m["priced"].get("cache_read", (0, 0.0))[0]
        )
        # The model's own text and the tool calls' arguments are written too; bytes stand in for
        # tokens when splitting what was written between them and the outputs.
        text = sum(len(json.dumps(c["arguments"])) for c in mine)
        outputs_share = size / (size + text)
        for call in mine:
            share = output_bytes(call["output"]) / size * outputs_share
            call["cost"] += written_cost * share + written * share * later_read_price


_DOCS = {}


def doc_text(name):
    """A Peeky doc as the study's images had it (`main` at the study's commit), else None."""
    if name not in _DOCS:
        import subprocess

        found = subprocess.run(
            ["git", "show", "{}:docs/{}".format(STUDY_COMMIT, name)],
            cwd=HERE,
            capture_output=True,
        )
        _DOCS[name] = found.stdout.decode(errors="replace") if found.returncode == 0 else None
    return _DOCS[name]


def _lines_bytes(text, first, last):
    return len("".join(text.splitlines(True)[first - 1 : last]).encode())


def doc_share(command, size):
    """An estimate of the bytes of Peeky's docs a call printed. A `cat` of a doc counts the
    doc's size at the study's commit, a `sed -n 'A,Bp'`, `head` or `tail` of one its lines;
    another command that reads a doc, `--help` or Peeky's source counts the call's output
    divided by the commands it ran. Never more than the call printed."""
    programs = [
        (program, arguments)
        for program, arguments in metrics.invoked_programs(command)
        if program not in ("cd", "pwd", "ls", "command", "which", "true", "echo", "printf", "[")
    ]
    if not programs:
        return size
    total = 0
    for program, arguments in programs:
        names = [m.group(1) for a in arguments for m in [DOC_FILE.search(a)] if m and m.group(1)]
        texts = [doc_text(name) for name in names]
        helped = any(a in ("--help", "-h") for a in arguments) and program in PEEKY_PROGRAMS_HELP
        if names and all(t is not None for t in texts):
            ranges = [a for a in arguments if re.fullmatch(r"'?\d+,\d+p'?", a)]
            counts = [a for a in arguments if re.fullmatch(r"-n?\d+|\d+", a)]
            for text in texts:
                lines = len(text.splitlines())
                if program == "sed" and ranges:
                    first, last = (int(x) for x in ranges[0].strip("'p").split(","))
                    total += _lines_bytes(text, first, min(last, lines))
                elif program in ("head", "tail") and counts:
                    n = int(counts[0].lstrip("-n"))
                    first = 1 if program == "head" else max(1, lines - n + 1)
                    total += _lines_bytes(text, first, min(lines, first + n - 1))
                elif program in ("cat", "nl", "less", "more"):
                    total += len(text.encode())
                else:
                    total += size // len(programs)
        elif names or helped or any("/opt/peeky/" in a for a in arguments):
            total += size // len(programs)
    return min(total, size)


def median(values):
    return statistics.median(values) if values else float("nan")


def before(attempt):
    """The calls before the first stop, the stop included; all of them if it never stopped."""
    stop = attempt["first_stop"]
    return attempt["calls"][: stop["n"]] if stop else attempt["calls"]


def after(attempt):
    stop = attempt["first_stop"]
    return attempt["calls"][stop["n"] :] if stop else []


def cost(calls, categories):
    return sum(c["credits"] for c in calls if c["category"] in categories)


def doc_cost(call):
    """A call's credits, in the share of its output that was Peeky's docs."""
    if not call.get("doc_bytes") or not call["output_bytes"]:
        return 0.0
    return call["credits"] * min(1.0, call["doc_bytes"] / call["output_bytes"])


def setup_cost(calls):
    """Set-up's credits: the calls that are set-up, and the docs' share of the calls that
    read docs among other things."""
    return sum(
        c["credits"] if c["category"] in SETUP and c["category"] != "docs" else doc_cost(c)
        for c in calls
    )


def per_bug(attempts, value):
    """Each bug's median of value(attempt)."""
    bugs = sorted({a["bug"] for a in attempts})
    return {b: median([value(a) for a in attempts if a["bug"] == b]) for b in bugs}


def summarize(attempts, control):
    languages = sorted({a["language"] for a in attempts}) + ["all"]
    groups = {
        language: [a for a in attempts if language in ("all", a["language"])]
        for language in languages
    }
    out = ["### Up to the first stop (`peeky-first`, medians per attempt)\n"]
    out.append(
        "| Language | Attempts | Stopped | Calls | Credits | Seconds | Setup calls | Doc reads "
        "| Doc KB | Config writes | JVMs started by hand | Refused or failed Peeky runs |"
    )
    out.append("| --- " * 12 + "|")
    for language, group in groups.items():
        stopped = [a for a in group if a["first_stop"]]
        out.append(
            "| {} | {} | {} | {:.0f} | {:.1f} | {:.0f} | {:.0f} | {:.0f} | {:.0f} | {:.0f} | {:.0f} "
            "| {:.0f} |".format(
                language,
                len(group),
                len(stopped),
                median([a["first_stop"]["n"] for a in stopped]),
                median([a["first_stop"]["credits"] for a in stopped]),
                median([a["first_stop"]["seconds"] for a in stopped]),
                median([sum(c["category"] in SETUP for c in before(a)) for a in group]),
                median([sum("docs" in c for c in before(a)) for a in group]),
                median([sum(c.get("doc_bytes", 0) for c in before(a)) / 1024 for a in group]),
                median([sum(bool(c.get("config")) for c in before(a)) for a in group]),
                median([sum(c.get("debuggee") == "start" for c in before(a)) for a in group]),
                median([sum(c["category"] == "peeky_refused" for c in before(a)) for a in group]),
            )
        )
    out.append(
        "\nCalls, credits (the session's, cumulative) and seconds run to the call that showed the "
        "first stop, in attempts that stopped. The other columns count the calls before the "
        "first stop, or all calls if there was none. Setup calls are docs, config, debuggee, "
        "wait, and Peeky runs that didn't stop. A doc read is any call that read Peeky's docs, "
        "`--help` or source; Doc KB is the docs' share of what those calls printed. A config "
        "write is any call that wrote one, inline ones included.\n"
    )
    if control:
        out.append("### Where the extra credits went (medians per bug)\n")
        out.append(
            "| Language | Bugs | control | peeky-first | Difference | Setup before the first stop "
            "| of which docs | Peeky after the first stop |"
        )
        out.append("| --- " * 8 + "|")
        for language, group in groups.items():
            bugs = sorted({a["bug"] for a in group})
            base = per_bug([c for c in control if c["bug"] in bugs], lambda c: c["ai_credits"])
            arm = per_bug(group, lambda a: a["ai_credits"])
            setup = per_bug(group, lambda a: setup_cost(before(a)))
            docs = per_bug(group, lambda a: sum(doc_cost(c) for c in before(a)))
            probing = per_bug(
                group, lambda a: cost(after(a), ("stop", "peeky_refused", "peeky_other"))
            )
            out.append(
                "| {} | {} | {:.1f} | {:.1f} | {:+.1f} | {:.1f} | {:.1f} | {:.1f} |".format(
                    language,
                    len(bugs),
                    median(list(base.values())),
                    median(list(arm.values())),
                    median([arm[b] - base[b] for b in bugs]),
                    median(list(setup.values())),
                    median(list(docs.values())),
                    median(list(probing.values())),
                )
            )
        out.append(
            "\nA call's credits: its share of the output of the model call that asked for it, "
            "plus what its output cost in context afterwards (written to the cache once, read "
            "again by each later model call). The system prompt, the task and the model's own "
            "text aren't attributed to a call. Set-up is the set-up calls' credits, and the "
            "docs' share of a call that read docs among other things.\n"
        )
    out.append("### Calls before the first stop, by category (all attempts)\n")
    out.append("| Language | " + " | ".join(CATEGORIES) + " |")
    out.append("| --- " * (len(CATEGORIES) + 1) + "|")
    for language, group in groups.items():
        calls = [c for a in group for c in before(a)]
        counts = Counter(c["category"] for c in calls)
        out.append(
            "| {} | ".format(language)
            + " | ".join("{} ({:.0f} cr)".format(counts[k], cost(calls, (k,))) for k in CATEGORIES)
            + " |"
        )
    out.append("\nEach cell: calls, and their credits as above, summed over attempts.\n")
    out.append("### Refused or failed Peeky runs before the first stop\n")
    out.append("| Language | Code | Runs | Attempts |")
    out.append("| --- | --- | --- | --- |")
    for language, group in groups.items():
        if language == "all":
            continue
        runs, seen = Counter(), Counter()
        for a in group:
            codes = Counter()
            for c in before(a):
                if c["category"] == "peeky_refused":
                    for code in c.get("codes") or ["(no code)"]:
                        codes["USAGE" if code.startswith("USAGE") else code] += 1
            runs.update(codes)
            seen.update(codes.keys())
        for code, n in sorted(runs.items(), key=lambda kv: (-kv[1], kv[0]))[:6]:
            out.append("| {} | {} | {} | {} |".format(language, code, n, seen[code]))
    out.append("\n### Peeky docs read before the first stop\n")
    out.append("| Language | File | Attempts that read it |")
    out.append("| --- | --- | --- |")
    for language, group in groups.items():
        if language == "all":
            continue
        files = Counter()
        for a in group:
            files.update({name for c in before(a) for name in c.get("docs") or ()})
        for name, n in sorted(files.items(), key=lambda kv: (-kv[1], kv[0]))[:6]:
            out.append("| {} | {} | {} of {} |".format(language, name, n, len(group)))
    out.append("\n### Config writes and debuggee starts before the first stop\n")
    out.append(
        "| Language | Java attach | Rust workspace | Native | Of these, inline | JVM started by hand "
        "| Classpath hunts |"
    )
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for language, group in groups.items():
        calls = [c for a in group for c in before(a)]
        out.append(
            "| {} | {} | {} | {} | {} | {} | {} |".format(
                language,
                sum(c.get("config") == "java" for c in calls),
                sum(c.get("config") == "rust" for c in calls),
                sum(c.get("config") == "native" for c in calls),
                sum(bool(c.get("config") and c.get("inline_config")) for c in calls),
                sum(c.get("debuggee") == "start" for c in calls),
                sum(c.get("debuggee") == "classpath" for c in calls),
            )
        )
    out.append("\n### How the Java attempts started the JVM they attached to\n")
    out.append("| Started with | Attempts | Starts |")
    out.append("| --- | --- | --- |")
    starts, seen = Counter(), Counter()
    for a in attempts:
        kinds = [c["jvm"] for c in before(a) if c.get("jvm")]
        starts.update(kinds)
        seen.update(set(kinds))
    for kind, n in sorted(starts.items(), key=lambda kv: (-kv[1], kv[0])):
        out.append("| {} | {} | {} |".format(kind, seen[kind], n))
    return "\n".join(out)


def load_control(runs_dir):
    control = []
    for path in sorted(Path(runs_dir).glob("*/control-*/result.json")):
        result = json.loads(path.read_text())
        control.append({"bug": result["bug"], "ai_credits": result.get("ai_credits") or 0})
    return control


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", help="the raw runs directory, with session-events.jsonl.gz")
    parser.add_argument(
        "--from", dest="source", default=str(ROUND2 / "results" / "setup_cost.json")
    )
    parser.add_argument("--write", help="save the per-call records here")
    args = parser.parse_args()
    if args.runs:
        attempts = []
        for run_dir in sorted(Path(args.runs).glob("*/{}-*".format(ARM))):
            if not (run_dir / "result.json").is_file():
                continue
            meta = studylib.load_bug(run_dir.parent.name)["meta"]
            attempts.append(extract(run_dir, meta))
        if args.write:
            Path(args.write).write_text(json.dumps(attempts, indent=1) + "\n")
    else:
        attempts = json.loads(Path(args.source).read_text())
    control = load_control(ROUND2 / "results" / "runs")
    print(summarize(attempts, control))


if __name__ == "__main__":
    main()
