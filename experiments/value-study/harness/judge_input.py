#!/usr/bin/env python3
"""The text a judge reads for one attempt (see harness/judge_prompt.md).

    harness/judge_input.py <run_dir> [--bugs DIR]           # part 1, blind
    harness/judge_input.py <run_dir> --peeky [--bugs DIR]   # part 2, Peeky arm, not blind

Part 1: the bug report, the reference root cause and fix, and the agent's messages in order,
with every debugger hidden alike: Peeky, pdb, gdb, lldb, jdb and the rest become "the
debugger", their commands "[debugger command]" and what they printed "[debugger output]", in
both arms, so that the text says as little as possible about which arm it is.

Part 2: every shell command with its output (each cut to --max-output characters), Peeky's
marked, for checking the values Peeky printed against what the program did."""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics  # noqa: E402
import studylib  # noqa: E402

DEBUGGERS = r"(?:peeky(?:-probe|-worker|-agent)?|pdb|ipdb|gdb|lldb(?:-dap)?(?:-\d+)?|jdb|debugpy|rr)"
DEBUGGER_WORD = re.compile(r"(?<![\w/.-])" + DEBUGGERS + r"(?![\w-])", re.I)
# Lines of Peeky's output, quoted in a message.
PEEKY_OUTPUT_LINE = re.compile(
    r"^\s*(hit\s+phase\b|status: (completed|stopped|timed|failed)|evidence: saved|peeky: |"
    r"warning: Line \d+ stops|python: .*\(Peeky|output( so far)?.*:$|compare: |set \w+: |"
    r"error \[[A-Z_]+\]|hint: |names in \w+|breakpoint lines|mutable:)", re.I)
PEEKY_MARKERS = re.compile(
    r"\[(settable|stale|unproven|not settable[^\]]*)\]|<not (shown|read)[^>]*>|\bwithheld\b", re.I)
PEEKY_DOCS = re.compile(r"/opt/peeky/\S*|\b(AGENT_GUIDE|COOKBOOK|ERRORS)\.md\b|\b(c-cpp|java|python|rust)"
                        r"(-cookbook)?\.md\b|\bcookbook\b", re.I)


def is_debugger_command(text):
    return bool(DEBUGGER_WORD.search(text)) and any(
        program in metrics.PEEKY_PROGRAMS or re.fullmatch(DEBUGGERS, program, re.I)
        for program, _ in metrics.invoked_programs(text))


def neutralise(message):
    """A message with every debugger, its commands and its output hidden alike."""
    def block(match):
        body = match.group(0)
        lines = body.splitlines()[1:-1]
        if any(is_debugger_command(line) or PEEKY_OUTPUT_LINE.search(line) or
               PEEKY_MARKERS.search(line) or DEBUGGER_WORD.search(line) for line in lines):
            return "[debugger output]"
        return body

    text = re.sub(r"```[^\n]*\n.*?```", block, message, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: "[debugger command]" if is_debugger_command(m.group(1))
                  else m.group(0), text)
    kept = []
    for line in text.splitlines():
        if PEEKY_OUTPUT_LINE.search(line):
            if not kept or kept[-1] != "[debugger output]":
                kept.append("[debugger output]")
            continue
        kept.append(line)
    text = "\n".join(kept)
    text = PEEKY_DOCS.sub("[debugger docs]", text)
    text = PEEKY_MARKERS.sub("", text)
    text = DEBUGGER_WORD.sub("the debugger", text)
    text = re.sub(r"\bprobed\b", "inspected", text, flags=re.I)
    text = re.sub(r"\bprobing\b", "inspecting", text, flags=re.I)
    text = re.sub(r"\bprobes\b", "debugger runs", text, flags=re.I)
    text = re.sub(r"\bprobe\b", "debugger run", text, flags=re.I)
    text = re.sub(r"\b(the|a|an) the debugger", "the debugger", text, flags=re.I)
    return text


def part_one(run_dir, bug, result):
    meta = bug["meta"]
    gold = (bug["dir"] / "gold.patch").read_text() if (bug["dir"] / "gold.patch").is_file() else ""
    problem = (bug["dir"] / "problem.md").read_text() if (bug["dir"] / "problem.md").is_file() else ""
    out = ["# Bug: {}".format(meta.get("repo") or bug["id"]), "", "## Bug report", "",
           _cut(problem, 6000), "", "## Reference root cause", "",
           str(meta.get("root_cause") or "").strip(), "", "## Reference fix (upstream)", "",
           "```diff", _cut(gold, 8000), "```", "", "## The agent's messages, in order", ""]
    messages = result.get("assistant_messages") or []
    if not messages:
        out.append("(none)")
    for index, message in enumerate(messages, 1):
        out += ["### Message {}".format(index), "", neutralise(message).strip(), ""]
    return "\n".join(out)


def part_two(run_dir, bug, result, max_output):
    out = ["# Bug: {} (Peeky arm, attempt {})".format(bug["meta"].get("repo") or bug["id"],
                                                       run_dir.name), "",
           "Each shell command the agent ran, in order, with its output. Peeky's commands are "
           "marked **PEEKY**.", ""]
    for index, entry in enumerate(metrics.shell_transcript(run_dir), 1):
        mark = " **PEEKY**" if metrics.peeky_subcommands(entry["command"]) else ""
        out += ["## Command {}{} (exit {})".format(index, mark, entry["exit_code"]), "",
                "```", entry["command"], "```", "", "```", _cut(entry["output"], max_output), "```", ""]
    out += ["## The agent's final message", "", result.get("final_message") or "(none)"]
    return "\n".join(out)


def _cut(text, limit):
    text = text.rstrip("\n")
    if len(text) <= limit:
        return text
    half = limit // 2
    return "{}\n[... {} characters cut ...]\n{}".format(text[:half], len(text) - limit, text[-half:])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir")
    parser.add_argument("--bugs", default=str(studylib.BUGS))
    parser.add_argument("--peeky", action="store_true", help="part 2: Peeky's values (not blind)")
    parser.add_argument("--max-output", type=int, default=4000)
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    result = json.loads((run_dir / "result.json").read_text())
    bug = studylib.load_bug(result["bug"], args.bugs)
    if args.peeky:
        if result.get("arm") != "peeky":
            sys.exit("part 2 is for Peeky-arm attempts")
        print(part_two(run_dir, bug, result, args.max_output))
    else:
        print(part_one(run_dir, bug, result))


if __name__ == "__main__":
    main()
