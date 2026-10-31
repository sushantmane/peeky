#!/usr/bin/env python3
"""One attempt of the study: a fresh container, the agent, then grading in that container.

    harness/run_one.py <bug_id> <control|peeky> <rep> [--results DIR] [--bugs DIR]
                       [--budget-minutes 40] [--budget-credits 250] [--fake-agent gold|noop]

Writes <results>/<bug>/<arm>-<rep>/: prompt.txt, command.json, bash_env.sh, events.jsonl.gz
(the CLI's JSONL output), stderr.txt, usage.json, transcript.md, session-events.jsonl.gz and
copilot-logs/ (from the run's COPILOT_HOME), agent.patch, eval.log and result.json. A run
directory that already exists is moved aside to <arm>-<rep>.prev<N>.

The agent runs on the host in an empty directory, with a COPILOT_HOME and a TMPDIR of its own;
all three live under --agent-root, at a random name, so nothing in the paths the CLI shows the
model names the arm. The container mounts the TMPDIR read-only at the same path: the CLI saves
a command's output that is too large to show there, and tells the agent to read it. BASH_ENV
hands each shell command to `docker exec` in the container. The child's environment is built
from scratch: the host basics, the GitHub token, and nothing of the parent Copilot session.
--fake-agent replaces the CLI (gold: apply gold.patch; noop: nothing) to test the pipeline
without model calls."""

import argparse
import datetime
import gzip
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics  # noqa: E402
import studylib  # noqa: E402

LANGUAGE_NAMES = {"python": "Python", "java": "Java", "c": "C", "cpp": "C++", "c++": "C++",
                  "rust": "Rust"}
DEFAULT_AGENT_ROOT = Path.home() / ".cache" / "value-study-agent"
AUTH_VARIABLES = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
PASSED_VARIABLES = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
                    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "GH_HOST", "COPILOT_GH_HOST",
                    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")


class Interrupted(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def build_prompt(bug, arm):
    meta = bug["meta"]
    template = (studylib.HARNESS / "prompt.md").read_text()
    if arm == "peeky":
        template = template.rstrip("\n") + "\n\n" + (studylib.HARNESS / "prompt-peeky.md").read_text()
    language = str(meta.get("language") or "")
    problem = (bug["dir"] / "problem.md").read_text().strip("\n")
    values = [("{repo}", str(meta.get("repo") or bug["id"])),
              ("{language}", LANGUAGE_NAMES.get(language.lower(), language)),
              ("{test_hint}", str(meta.get("test_hint") or "").strip())]
    for key, value in values:
        template = template.replace(key, value)
    return template.replace("{problem}", problem)  # last: the report may hold braces


def child_environment(home, bash_env, copilot, docker):
    """The agent's environment, built from scratch: no COPILOT_* or app variables of the parent
    session reach it, only the host basics and the token the CLI authenticates with."""
    env = {name: os.environ[name] for name in PASSED_VARIABLES if os.environ.get(name)}
    for name in AUTH_VARIABLES:
        if os.environ.get(name):
            env[name] = os.environ[name]
            break
    directories = []
    for program in (copilot, docker):
        directory = str(Path(program).parent)
        if directory not in directories:
            directories.append(directory)
    env["PATH"] = ":".join(directories + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    env.update({"SHELL": "/bin/bash", "BASH_ENV": str(bash_env), "COPILOT_HOME": str(home),
                "COPILOT_AUTO_UPDATE": "false", "NO_COLOR": "1"})
    return env


def copilot_version(copilot, env):
    try:
        output = subprocess.run([copilot, "--version"], capture_output=True, text=True, env=env,
                                timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+\.\d+\.\d+\S*?)\.?\s*$", output.strip().splitlines()[0] if output.strip() else "")
    return match.group(1) if match else output.strip()[:80] or None


def kill_group(process, grace=20):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)  # what is left of the group: shells, docker exec
    except OSError:
        pass
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def make_agent_dir(root):
    """A fresh directory for the agent's run: work/ (its working directory), home/ (its
    COPILOT_HOME) and tmp/ (its TMPDIR, where the CLI saves a command's output too large to
    show; the container mounts it read-only at the same path, so the agent can read it)."""
    agent_dir = Path(root).expanduser().resolve() / secrets.token_hex(6)
    for name in ("work", "home", "tmp"):
        (agent_dir / name).mkdir(parents=True)
    return agent_dir


def run_agent(args, bug, arm, container, run_dir, result, agent_dir):
    copilot = studylib.which_or_die("copilot")
    docker = studylib.which_or_die("docker")
    work, home = agent_dir / "work", agent_dir / "home"
    bash_env = agent_dir / "bash_env.sh"
    bash_env.write_text(
        'if [ -z "$STUDY_INSIDE" ]; then\n'
        '  exec {} exec -i -e STUDY_INSIDE=1 -w /testbed {} bash -c "$BASH_EXECUTION_STRING"\n'
        "fi\n".format(shlex.quote(docker), shlex.quote(container)))
    shutil.copy(str(bash_env), str(run_dir / "bash_env.sh"))
    prompt = build_prompt(bug, arm)
    (run_dir / "prompt.txt").write_text(prompt)
    result["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
    env = child_environment(home, bash_env, copilot, docker)
    env["TMPDIR"] = str(agent_dir / "tmp")
    result["auth_variable"] = next((n for n in AUTH_VARIABLES if n in env), None)
    result["copilot_version"] = copilot_version(copilot, env)
    command = [copilot, "-p", prompt, "--available-tools", *metrics.ALLOWED_TOOLS,
               "--allow-all-tools", "--allow-all-paths", "--bash-env", "on",
               "--no-custom-instructions", "--disable-builtin-mcps", "--no-ask-user",
               "--no-auto-update", "--output-format", "json",
               "--usage-output-file", str(run_dir / "usage.json"),
               "--max-ai-credits", str(args.budget_credits),
               "--share", str(run_dir / "transcript.md")]
    (run_dir / "command.json").write_text(json.dumps(
        [part if part != prompt else "<prompt.txt>" for part in command], indent=1) + "\n")
    limit = args.budget_minutes * 60
    result["started"] = now()
    started = time.monotonic()
    process = None
    try:
        with (run_dir / "events.jsonl").open("wb") as stdout, \
                (run_dir / "stderr.txt").open("wb") as stderr:
            process = subprocess.Popen(command, cwd=str(work), env=env, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                result["exit_code"] = process.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                result["timed_out"] = True
            kill_group(process)
            if result.get("exit_code") is None:
                result["exit_code"] = process.returncode
    finally:
        if process is not None and process.poll() is None:
            kill_group(process, grace=5)
        result["wall_seconds"] = round(time.monotonic() - started, 1)
        result["ended"] = now()
        sessions = sorted((home / "session-state").glob("*/events.jsonl"))
        if sessions:
            shutil.copy(str(sessions[-1]), str(run_dir / "session-events.jsonl"))
        if (home / "logs").is_dir():
            shutil.copytree(str(home / "logs"), str(run_dir / "copilot-logs"),
                            ignore=shutil.ignore_patterns("*.lock"))


def run_fake_agent(mode, bug, container, result):
    result["started"] = now()
    started = time.monotonic()
    if mode == "gold":
        studylib.docker("cp", str(bug["dir"] / "gold.patch"), container + ":/tmp/study-gold.patch")
        code, output, _ = studylib.exec_in(
            container, "git apply /tmp/study-gold.patch && rm /tmp/study-gold.patch", timeout=120)
        if code != 0:
            raise RuntimeError("gold.patch does not apply: " + output[-500:])
    result["exit_code"] = 0
    result["wall_seconds"] = round(time.monotonic() - started, 1)
    result["ended"] = now()


def save_agent_patch(container, bug, run_dir):
    """`git diff <base>` of /testbed, new text files (up to 256 KB each) included, the files
    the hidden test patch touches excluded. The index is a copy, so /testbed is unchanged."""
    base = str(bug["meta"].get("base_commit") or "HEAD")
    excluded = studylib.test_patch_files((bug["dir"] / "test.patch").read_text())
    pathspec = " ".join(shlex.quote(":(exclude){}".format(path)) for path in excluded)
    script = (
        'cd /testbed && idx=$(mktemp) && cp "$(git rev-parse --git-dir)/index" "$idx" && '
        'export GIT_INDEX_FILE="$idx" && n=0 && '
        "git ls-files --others --exclude-standard -z | "
        "while IFS= read -r -d '' f; do "
        '  if [ $n -lt 500 ] && [ "$(stat -c %s "$f")" -le 262144 ] && grep -Iq . "$f"; then '
        '    git add -N -- "$f"; n=$((n + 1)); else echo "untracked, not in the patch: $f" >&2; fi; '
        "done; "
        "git -c core.quotepath=off diff --binary {} -- . {}; status=$?; rm -f \"$idx\"; exit $status"
    ).format(shlex.quote(base), pathspec)
    command = ["docker", "exec", "-w", "/testbed", container, "bash", "-c", script]
    completed = subprocess.run(command, capture_output=True, timeout=300, stdin=subprocess.DEVNULL)
    (run_dir / "agent.patch").write_bytes(completed.stdout)
    notes = completed.stderr.decode("utf-8", "replace")
    if completed.returncode != 0:
        if "Error response from daemon" in notes:
            raise studylib.DockerError(notes[-1000:])
    files = re.findall(r"^diff --git a/(\S+) ", completed.stdout.decode("utf-8", "replace"), re.M)
    return {"patch_files": files, "patch_bytes": len(completed.stdout),
            "patch_notes": notes.strip().splitlines()[-20:]}


def compress(path):
    """Replaces a file with its gzipped copy (<path>.gz); metrics.read_jsonl reads either."""
    if not path.is_file():
        return
    with path.open("rb") as source, gzip.open(str(path) + ".gz", "wb") as target:
        shutil.copyfileobj(source, target)
    path.unlink()


def classify_infra(result):
    """Whether the attempt failed for reasons outside the agent: Docker, the harness, or the
    CLI ending with an error (auth, rate limit, service) before any model call or not at a
    budget limit."""
    if result.get("infra_failure"):
        return True
    if result.get("fake_agent"):
        return False
    if result.get("timed_out"):
        return False
    if not result.get("model_calls"):
        result["errors"].append("no model call was made")
        return True
    near_budget = (result.get("ai_credits") or 0) >= 0.9 * float(result.get("budget_credits") or 0)
    if result.get("exit_code") not in (0, None) and not (result.get("credit_limit_reached") or near_budget):
        result["errors"].append("the CLI exited with code {}".format(result.get("exit_code")))
        return True
    if result.get("infra_error_text") and not result.get("assistant_messages"):
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bug_id")
    parser.add_argument("arm", choices=studylib.ARMS)
    parser.add_argument("rep", type=int)
    parser.add_argument("--results", default=str(studylib.RUNS))
    parser.add_argument("--bugs", default=str(studylib.BUGS))
    parser.add_argument("--budget-minutes", type=float, default=40)
    parser.add_argument("--budget-credits", type=float, default=250)
    parser.add_argument("--fake-agent", choices=("gold", "noop"))
    parser.add_argument("--agent-root", default=str(DEFAULT_AGENT_ROOT),
                        help="where the agent's working directory and COPILOT_HOME live during "
                             "the run (default: %(default)s)")
    parser.add_argument("--image", help="override the arm's image")
    parser.add_argument("--keep-container", action="store_true", help="debugging only")
    args = parser.parse_args()
    if args.budget_credits < 30:
        parser.error("the CLI takes --max-ai-credits of 30 or more")

    bug = studylib.load_bug(args.bug_id, args.bugs)
    run_dir = Path(args.results) / args.bug_id / "{}-{}".format(args.arm, args.rep)
    if run_dir.exists():
        n = 1
        while run_dir.with_name("{}.prev{}".format(run_dir.name, n)).exists():
            n += 1
        run_dir.rename(run_dir.with_name("{}.prev{}".format(run_dir.name, n)))
    run_dir.mkdir(parents=True)

    image = args.image or studylib.image_for(bug, args.arm)
    container = "study-{}-{}-{}-{}".format(args.bug_id, args.arm, args.rep, secrets.token_hex(3))
    result = {
        "bug": args.bug_id, "arm": args.arm, "rep": args.rep,
        "language": bug["meta"].get("language"), "category": bug["meta"].get("category"),
        "image": image, "image_digest": None, "peeky_sha": None, "container": container,
        "fake_agent": args.fake_agent, "budget_minutes": args.budget_minutes,
        "budget_credits": args.budget_credits, "copilot_version": None,
        "started": None, "ended": None, "wall_seconds": None, "timed_out": False,
        "exit_code": None, "solved": None, "f2p": None, "p2p_failed": None,
        "grading_seconds": None, "errors": [], "infra_failure": False,
    }

    def interrupt(signum, frame):
        raise Interrupted("signal {}".format(signum))

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    started = time.monotonic()
    interrupted = False
    agent_dir = None
    try:
        info = studylib.image_info(image)
        result["image_digest"] = info["id"]
        result["peeky_sha"] = info["peeky_sha"] if args.arm == "peeky" else None
        if args.arm == "peeky" and not info["peeky_sha"]:
            raise RuntimeError("{} has no peeky-study.peeky-sha label".format(image))
        if args.arm == "control" and info["peeky_sha"]:
            raise RuntimeError("{} is a Peeky image".format(image))
        if args.fake_agent:
            studylib.start_container(image, container)
            run_fake_agent(args.fake_agent, bug, container, result)
        else:
            agent_dir = make_agent_dir(args.agent_root)
            studylib.start_container(image, container, read_only_mounts=[agent_dir / "tmp"])
            run_agent(args, bug, args.arm, container, run_dir, result, agent_dir)
        result["leftover_processes"] = studylib.kill_leftovers(container)
        result.update(save_agent_patch(container, bug, run_dir))
        graded = studylib.grade_container(container, bug, run_dir / "eval.log")
        result.update({"solved": graded["solved"], "f2p": graded["f2p"],
                       "p2p_failed": graded["p2p_failed"], "grading_seconds": graded["seconds"],
                       "grading": {k: v for k, v in graded.items()
                                   if k not in ("solved", "f2p", "p2p_failed", "seconds")}})
    except Interrupted as error:
        interrupted = True
        result["errors"].append("interrupted: {}".format(error))
        result["infra_failure"] = True
    except studylib.DockerError as error:
        result["errors"].append("docker: {}".format(error))
        result["infra_failure"] = True
    except Exception as error:  # a harness failure is never the agent's
        result["errors"].append("harness: {!r}\n{}".format(error, traceback.format_exc()[-3000:]))
        result["infra_failure"] = True
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if not args.keep_container:
            studylib.remove_container(container)
        if agent_dir is not None:
            shutil.rmtree(str(agent_dir), ignore_errors=True)
        collected = metrics.collect(run_dir, bug["meta"].get("language"),
                                    bug["meta"].get("test_hint"), args.arm)
        if collected.get("copilot_version") is None:
            collected["copilot_version"] = result.get("copilot_version")
        result.update(collected)
        result["infra_failure"] = classify_infra(result)
        result["total_seconds"] = round(time.monotonic() - started, 1)
        name = "result.interrupted.json" if interrupted else "result.json"
        studylib.write_json(run_dir / name, result)
        for log in ("events.jsonl", "session-events.jsonl"):
            compress(run_dir / log)
    summary = {k: result.get(k) for k in ("bug", "arm", "rep", "solved", "wall_seconds",
                                          "timed_out", "ai_credits", "tool_calls",
                                          "infra_failure", "contaminated")}
    print(json.dumps(summary))
    if interrupted:
        sys.exit(130)
    sys.exit(3 if result["infra_failure"] else 0)


if __name__ == "__main__":
    main()
