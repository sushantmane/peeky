#!/usr/bin/env python3
"""Runs the study's attempts: every bug with both images and a passing validation.json, both
arms, reps 1..3, in an order shuffled with random.Random(441), at most --concurrency at once.

    harness/schedule.py [--concurrency 3] [--only bug,...] [--dry-run] [--preflight]
                        [--budget-minutes 40] [--budget-credits 250] [--reps 3]
                        [--results DIR] [--bugs DIR] [--fake-agent gold|noop]

Resumable: an attempt whose result.json exists and isn't an infrastructure failure is done.
An infrastructure failure (Docker, auth, rate limit, a CLI crash) is retried, at most
--max-retries times; an interrupted attempt (result.interrupted.json) is rerun. Each attempt is
harness/run_one.py; their lines go to the progress log (default: progress.log beside the
results directory). --only filters the shuffled list, so the order of what remains is the same.
"""

import argparse
import datetime
import json
import os
import random
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_one  # noqa: E402
import studylib  # noqa: E402

SEED = 441


def log(handle, text):
    line = "{} {}".format(datetime.datetime.now().isoformat(timespec="seconds"), text)
    print(line, flush=True)
    if handle:
        handle.write(line + "\n")
        handle.flush()


def eligible_bugs(bugs_dir):
    """({bug id: bug}, {bug id: why it is left out}) over every directory in bugs_dir."""
    chosen, excluded = {}, {}
    for directory in sorted(Path(bugs_dir).iterdir()) if Path(bugs_dir).is_dir() else []:
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        bug = studylib.load_bug(directory.name, bugs_dir)
        missing = [
            name
            for name in (
                "bug.yaml",
                "problem.md",
                "test.patch",
                "eval.sh",
                "parse.py",
                "grade.json",
            )
            if not (directory / name).is_file()
        ]
        if missing:
            excluded[directory.name] = "missing " + ", ".join(missing)
        elif not studylib.validation_ok(directory):
            excluded[directory.name] = "validation.json missing or not passing"
        elif not studylib.image_exists(bug["control_image"]):
            excluded[directory.name] = "no image " + bug["control_image"]
        elif not studylib.image_exists(bug["peeky_image"]):
            excluded[directory.name] = "no image " + bug["peeky_image"]
        else:
            control = studylib.image_info(bug["control_image"])
            peeky = studylib.image_info(bug["peeky_image"])
            if control["peeky_sha"]:
                excluded[directory.name] = "the control image holds Peeky"
            elif not peeky["peeky_sha"]:
                excluded[directory.name] = "the peeky image has no Peeky label"
            elif peeky_control_id(bug["peeky_image"]) not in (None, control["id"]):
                excluded[directory.name] = (
                    "the peeky image was built from an older control image;"
                    " rerun build_peeky_images.sh"
                )
            else:
                chosen[directory.name] = bug
    return chosen, excluded


def peeky_control_id(image):
    result = studylib.docker(
        "image",
        "inspect",
        "-f",
        '{{ index .Config.Labels "peeky-study.control-id" }}',
        image,
        check=False,
    )
    value = result.stdout.strip()
    return value if value and value != "<no value>" else None


def attempt_state(results, bug, arm, rep, max_retries):
    """(state, infra failures so far): todo, done, retry, interrupted or gave-up."""
    run_dir = Path(results) / bug / "{}-{}".format(arm, rep)
    failures = 0
    for previous in sorted(run_dir.parent.glob("{}.prev*".format(run_dir.name))):
        failures += _infra(previous / "result.json")
    if (run_dir / "result.json").is_file():
        if not _infra(run_dir / "result.json"):
            return "done", failures
        failures += 1
        return ("retry" if failures <= max_retries else "gave-up"), failures
    if (run_dir / "result.interrupted.json").is_file():
        return "interrupted", failures
    return ("todo" if failures <= max_retries else "gave-up"), failures


def _infra(path):
    try:
        return bool(json.loads(Path(path).read_text()).get("infra_failure"))
    except (OSError, ValueError):
        return False


def preflight_call(agent_root):
    """A tiny prompt through the same isolated environment as the attempts."""
    copilot = studylib.which_or_die("copilot")
    docker = studylib.which_or_die("docker")
    directory = Path(agent_root).expanduser() / ("preflight-" + secrets.token_hex(4))
    (directory / "work").mkdir(parents=True)
    (directory / "home").mkdir()
    bash_env = directory / "bash_env.sh"
    bash_env.write_text("")
    env = run_one.child_environment(directory / "home", bash_env, copilot, docker)
    try:
        completed = subprocess.run(
            [
                copilot,
                "-p",
                "Reply with the single word: ready",
                "--available-tools",
                "bash",
                "--allow-all-tools",
                "--no-custom-instructions",
                "--disable-builtin-mcps",
                "--no-ask-user",
                "--no-auto-update",
                "--output-format",
                "json",
                "--max-ai-credits",
                "30",
            ],
            cwd=str(directory / "work"),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
        )
        replied = any(
            '"assistant.message"' in line and "ready" in line.lower()
            for line in completed.stdout.splitlines()
        )
        return completed.returncode == 0 and replied, completed.stderr.strip()[-500:]
    finally:
        shutil.rmtree(str(directory), ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--only", help="comma-separated bug ids")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="check the CLI with a tiny prompt (one model call) before starting",
    )
    parser.add_argument("--budget-minutes", type=float, default=40)
    parser.add_argument("--budget-credits", type=float, default=250)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument(
        "--stop-after-infra",
        type=int,
        default=5,
        help="stop when this many attempts in a row fail for infrastructure",
    )
    parser.add_argument("--results", default=str(studylib.RUNS))
    parser.add_argument("--bugs", default=str(studylib.BUGS))
    parser.add_argument("--progress-log")
    parser.add_argument("--agent-root", default=str(run_one.DEFAULT_AGENT_ROOT))
    parser.add_argument("--fake-agent", choices=("gold", "noop"))
    args = parser.parse_args()

    if not studylib.docker_up():
        sys.exit("Docker is not running")
    bugs, excluded = eligible_bugs(args.bugs)
    attempts = [
        (bug, arm, rep)
        for bug in sorted(bugs)
        for arm in studylib.ARMS
        for rep in range(1, args.reps + 1)
    ]
    random.Random(SEED).shuffle(attempts)
    if args.only:
        wanted = set(args.only.split(","))
        unknown = wanted - set(bugs)
        if unknown:
            print(
                "not eligible: "
                + ", ".join(
                    "{} ({})".format(b, excluded.get(b, "no such bug")) for b in sorted(unknown)
                )
            )
        attempts = [a for a in attempts if a[0] in wanted]

    states = {a: attempt_state(args.results, *a, args.max_retries) for a in attempts}
    queue = [a for a in attempts if states[a][0] in ("todo", "retry", "interrupted")]
    counts = {}
    for state, _ in states.values():
        counts[state] = counts.get(state, 0) + 1
    print("bugs: {} eligible, {} left out".format(len(bugs), len(excluded)))
    for bug, why in sorted(excluded.items()):
        print("  left out: {}: {}".format(bug, why))
    print(
        "attempts: {} ({})".format(
            len(attempts), ", ".join("{} {}".format(n, s) for s, n in sorted(counts.items()))
        )
    )
    if args.dry_run:
        for index, attempt in enumerate(attempts, 1):
            print(
                "{:4d} {:40s} {:7s} {}  {}".format(
                    index, attempt[0], attempt[1], attempt[2], states[attempt][0]
                )
            )
        return

    if not args.fake_agent:
        studylib.which_or_die("copilot")
        if not any(os.environ.get(n) for n in run_one.AUTH_VARIABLES):
            print(
                "warning: no COPILOT_GITHUB_TOKEN, GH_TOKEN or GITHUB_TOKEN; the CLI's stored "
                "login in the run's empty COPILOT_HOME won't exist"
            )
        if args.preflight:
            ok, stderr = preflight_call(args.agent_root)
            if not ok:
                sys.exit("preflight failed: " + stderr)
            print("preflight: the CLI answered")

    progress_path = Path(args.progress_log or Path(args.results).parent / "progress.log")
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    handle = progress_path.open("a")
    log(
        handle,
        "schedule: {} to run, concurrency {}, budgets {} min / {} credits{}".format(
            len(queue),
            args.concurrency,
            args.budget_minutes,
            args.budget_credits,
            ", fake agent " + args.fake_agent if args.fake_agent else "",
        ),
    )

    running = {}
    infra_streak = 0
    stopping = False

    def launch(attempt):
        bug, arm, rep = attempt
        command = [
            sys.executable,
            str(studylib.HARNESS / "run_one.py"),
            bug,
            arm,
            str(rep),
            "--results",
            args.results,
            "--bugs",
            args.bugs,
            "--budget-minutes",
            str(args.budget_minutes),
            "--budget-credits",
            str(args.budget_credits),
            "--agent-root",
            args.agent_root,
        ]
        if args.fake_agent:
            command += ["--fake-agent", args.fake_agent]
        log(handle, "start {} {} {}".format(bug, arm, rep))
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        running[process] = attempt

    try:
        while (queue and not stopping) or running:
            while queue and not stopping and len(running) < args.concurrency:
                launch(queue.pop(0))
            time.sleep(1)
            for process in [p for p in running if p.poll() is not None]:
                attempt = running.pop(process)
                output = process.stdout.read().strip()
                summary = output.splitlines()[-1] if output else ""
                log(
                    handle, "end {} {} {} exit {}: {}".format(*attempt, process.returncode, summary)
                )
                state, failures = attempt_state(args.results, *attempt, args.max_retries)
                if state == "done":
                    infra_streak = 0
                    continue
                infra_streak += 1
                if state in ("retry", "interrupted", "todo"):
                    log(
                        handle,
                        "  infrastructure failure {} of {}; queued again".format(
                            failures, args.max_retries + 1
                        ),
                    )
                    queue.append(attempt)
                else:
                    log(handle, "  infrastructure failure {}; giving up on it".format(failures))
                if infra_streak >= args.stop_after_infra and not stopping:
                    log(
                        handle, "stopping: {} infrastructure failures in a row".format(infra_streak)
                    )
                    stopping = True
    except KeyboardInterrupt:
        log(handle, "interrupted; waiting for the running attempts to clean up")
        for process in running:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 180
        for process in running:
            try:
                process.wait(timeout=max(deadline - time.monotonic(), 1))
            except subprocess.TimeoutExpired:
                process.kill()
        sys.exit(130)
    finally:
        handle.close()
    left = [a for a in attempts if attempt_state(args.results, *a, args.max_retries)[0] != "done"]
    print("done; {} attempts not done".format(len(left)))
    sys.exit(1 if stopping else 0)


if __name__ == "__main__":
    main()
