#!/usr/bin/env python3
"""Round 2's reach check: can Peeky stop where each bug lives? No agent and no model.

    harness/reach.py [bug_id ...] [--tag peeky-r2] [--out DIR] [--specs FILE]
    harness/reach.py --report DIR          # REACH.md's tables from a run's results

For each bug, in a fresh container from its round-2 Peeky image (`peeky-study/<id>:peeky-r2`),
with no network, 4 CPUs and 6 GB, one container at a time:

1. The hidden test patch is applied, as grading applies it, so the failing test exists.
2. The spec's `prepare` commands run: a rebuild of Peeky's build where the test patch changed its
   sources, a Rust workspace's config and `peeky build`, and the like. Each is recorded.
3. Each point of the spec (reach_specs.json) is tried with a session, as an agent would:
   `peeky session start <target> <options> --at FILE:LINE -- <program>`, then `session
   inspect` at the stop, then `session close`. Java attaches to a test JVM that `launch` started
   and that waits for a debugger. A server is driven to the line by `trigger` commands.
4. Each point's outcome is one of:
   - `reached`: the program stopped at the breakpoint. `inspect` gives the values read, and
     those withheld with a note.
   - `refused`: Peeky refused before or at launch; the error's code and message are kept.
   - `no stop`: the program ran and ended, or stopped elsewhere, without stopping there.
   - `timed out` or `error`: the probe or the harness failed; the message is kept.
   - `not attempted`: the spec says why (e.g. the assertion is in a Tcl test).

Points are fixed in the spec before any run: see ../README.md, "Reach check". A point may name
its line by number, or by `match` (the first line of the file holding that text, after
`after` if given), so a line that a regenerated file moved is still found.

Results: <out>/<bug>.json, with every command and the tail of its output. Python 3.9+,
standard library only; round 1's studylib supplies the Docker helpers."""

import argparse
import datetime
import json
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUND2 = HERE.parent
ROUND1 = ROUND2.parent / "value-study"
sys.path.insert(0, str(ROUND1 / "harness"))
import studylib  # noqa: E402

SPECS = HERE / "reach_specs.json"
WORK = "/root/reach"
SESSION = "reach"
TAIL = 4000
GROUPINGS = ("special variables", "function variables", "class variables", "protected variables")
# A refusal whose message says this is tried once more.
AGAIN = re.compile(r"\b(build|run) again\b", re.I)
# The longest a session command waits for a stop (`--timeout`).
MAX_WAIT = 300
NOTE_KEYS = ("note", "notes", "unavailable", "stale", "withheld", "reason")
WITHHELD_TEXT = re.compile(
    r"no value yet|<unverified|budget-exhausted|<unqualified|\[stale\]|unproven|not read|"
    r"unavailable"
)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def tail(text, limit=TAIL):
    return text if len(text) <= limit else "…" + text[-limit:]


def run(container, script, timeout):
    """(exit code or None on timeout, stdout, stderr, seconds) of `bash -c script` in /testbed."""
    started = time.monotonic()
    command = ["docker", "exec", "-i", "-w", "/testbed", container, "bash", "-c", script]
    try:
        done = subprocess.run(command, input=b"", capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        out = (error.stdout or b"").decode("utf-8", "replace")
        err = (error.stderr or b"").decode("utf-8", "replace")
        return None, out, err, round(time.monotonic() - started, 1)
    out = done.stdout.decode("utf-8", "replace")
    err = done.stderr.decode("utf-8", "replace")
    if done.returncode in (125, 126, 127) and "Error response from daemon" in err:
        raise studylib.DockerError(err.strip()[-2000:])
    return done.returncode, out, err, round(time.monotonic() - started, 1)


def peeky(container, argv, timeout):
    """`peeky ARGV --json` in /testbed: (exit code, its JSON or None, stderr, seconds)."""
    if "--" in argv:
        cut = argv.index("--")
        argv = [*argv[:cut], "--json", *argv[cut:]]
    else:
        argv = [*argv, "--json"]
    script = "peeky {}".format(" ".join(shlex.quote(a) for a in argv))
    code, out, err, seconds = run(container, script, timeout)
    try:
        answer = json.loads(out)
    except ValueError:
        answer = None
        err = (err + "\n[stdout] " + out).strip()
    return code, answer, err, seconds, script


def write_file(container, path, text):
    command = [
        "docker",
        "exec",
        "-i",
        container,
        "bash",
        "-c",
        "mkdir -p {} && cat > {}".format(shlex.quote(str(Path(path).parent)), shlex.quote(path)),
    ]
    subprocess.run(command, input=text.encode(), check=True, capture_output=True, timeout=60)


def resolve_at(container, point):
    """FILE:LINE for a point given by line, or by `match` (and `after`)."""
    if "line" in point:
        return "{}:{}".format(point["file"], point["line"])
    after = point.get("after")
    script = (
        "python3 - {file} <<'PY'\n"
        "import sys\n"
        "lines = open(sys.argv[1], encoding='utf-8', errors='replace').read().split('\\n')\n"
        "match, after = {match!r}, {after!r}\n"
        "start = 0\n"
        "if after is not None:\n"
        "    start = next(i for i, l in enumerate(lines) if after in l) + 1\n"
        "print(next(i + 1 for i in range(start, len(lines)) if match in lines[i]))\n"
        "PY"
    ).format(file=shlex.quote(point["file"]), match=point["match"], after=after)
    code, out, err, _ = run(container, script, 60)
    if code != 0 or not out.strip().isdigit():
        raise ValueError("no line of {} holds {!r}: {}".format(point["file"], point["match"], err))
    return "{}:{}".format(point["file"], out.strip())


def values(inspected):
    """What `session inspect` showed in the stopped frame: how many values it read, how many it
    withheld with a note, the first few of each. debugpy's groupings aren't values."""
    shown, withheld, sample, notes = 0, 0, [], []
    for scope in (inspected or {}).get("scopes") or []:
        for var in scope.get("variables") or []:
            if var.get("name") in GROUPINGS:
                continue
            value = str(var.get("value", ""))
            entry = {"name": var.get("name"), "value": value[:160]}
            for key in NOTE_KEYS:
                if var.get(key):
                    entry[key] = str(var.get(key))[:300]
            if len(entry) > 2 or WITHHELD_TEXT.search(value):
                withheld += 1
                if len(notes) < 6:
                    notes.append(entry)
            else:
                shown += 1
                if len(sample) < 6:
                    sample.append(entry)
    return {"shown": shown, "withheld": withheld, "sample": sample, "withheld_sample": notes}


def classify(code, answer, err):
    """(outcome, detail) of a session start or wait."""
    if isinstance(answer, dict) and answer.get("error"):
        error = answer["error"]
        kind = error.get("code") or "?"
        detail = {
            "code": kind,
            "message": error.get("message"),
            "hint": error.get("hint"),
            "retryable": error.get("retryable"),
        }
        if kind == "NO_STOP":
            return "no stop", detail
        if kind in ("TIMEOUT",):
            return "timed out", detail
        return "refused", detail
    if isinstance(answer, dict) and answer.get("state"):
        state, reason = answer.get("state"), answer.get("reason")
        detail = {
            "state": state,
            "reason": reason,
            "location": answer.get("location"),
            "function": answer.get("function"),
        }
        for key in ("note", "notes", "warnings", "output", "exit_code"):
            if answer.get(key):
                detail[key] = answer[key] if key != "output" else tail(str(answer[key]), 1000)
        if state == "stopped" and reason in ("breakpoint", "breakpoint hit"):
            return "reached", detail
        if state == "stopped":
            return "no stop", detail
        if state == "running":
            return "running", detail
        return "no stop", detail
    return "error", {"exit": code, "stderr": tail(err, 2000)}


def kill_group(container, pid_file):
    run(
        container,
        "if [ -s {0} ]; then kill -TERM -- -$(cat {0}) 2>/dev/null; sleep 2; "
        "kill -KILL -- -$(cat {0}) 2>/dev/null; rm -f {0}; fi; true".format(pid_file),
        60,
    )


def launch(container, spec, record):
    """Starts `spec.launch` in its own process group and waits for `ready` in its log."""
    log, pid_file = WORK + "/launch.log", WORK + "/launch.pid"
    script = "mkdir -p {w} && rm -f {log} && (setsid bash -c {cmd} > {log} 2>&1 & echo $! > {pid})".format(
        w=WORK, log=log, pid=pid_file, cmd=shlex.quote(spec["launch"])
    )
    run(container, script, 60)
    deadline = time.monotonic() + spec.get("launch_timeout", 600)
    started = time.monotonic()
    while time.monotonic() < deadline:
        code, out, _, _ = run(container, "cat {} 2>/dev/null | tail -c 20000".format(log), 60)
        if spec["ready"] in out:
            record["launch"] = {"ready_seconds": round(time.monotonic() - started, 1)}
            return True
        alive, _, _, _ = run(container, "kill -0 $(cat {}) 2>/dev/null".format(pid_file), 30)
        if alive != 0:
            break
        time.sleep(3)
    _, out, _, _ = run(container, "tail -c 3000 {}".format(log), 60)
    record["launch"] = {"failed": True, "log_tail": out}
    return False


def try_point(container, spec, point, applied):
    """A point's outcome. A refusal Peeky marks retryable, or whose message says to build or
    run again (a build that changed while Peeky read it), is tried once more. So is one whose
    message names a remedy
    the spec lists (`remedies`: a change that leaves the program as it is, such as a lockfile's
    version line), once that remedy ran; a remedy runs once per container. Every answer is
    kept, the earlier ones under `first_try`."""
    record = attempt_point(container, spec, point)
    detail = record.get("detail") or {}
    if record["outcome"] != "refused":
        return record
    remedy = None
    for each in spec.get("remedies", []):
        if each["when"] in str(detail.get("message")) and each["run"] not in applied:
            remedy = each
    again = AGAIN.search(str(detail.get("message"))) and not record.get("retried")
    if not (detail.get("retryable") or again or remedy):
        return record
    first = {k: record.get(k) for k in ("outcome", "detail", "commands")}
    if remedy:
        code, out, err, seconds = run(container, remedy["run"], 300)
        applied.add(remedy["run"])
        first["remedy"] = {"command": remedy["run"], "exit": code, "output": tail(out + err, 1000)}
    retry = (
        try_point(container, spec, point, applied)
        if remedy
        else attempt_point(container, spec, point)
    )
    retry["first_try"] = first
    retry["retried"] = True
    return retry


def attempt_point(container, spec, point):
    record = {"role": point["role"], "why": point.get("why")}
    if point.get("not_attempted"):
        record.update(outcome="not attempted", detail={"reason": point["not_attempted"]})
        return record
    try:
        at = resolve_at(container, point)
    except Exception as error:  # a point the spec names wrongly is the harness's error
        record.update(outcome="error", detail={"message": str(error)})
        return record
    record["at"] = at
    timeout = spec.get("timeout", 300)
    pid_file = WORK + "/launch.pid"
    try:
        if spec.get("launch") and not launch(container, spec, record):
            record.update(outcome="error", detail={"message": "the launch never got ready"})
            return record
        options = point.get("options", spec.get("options", []))
        argv = ["session", "start", spec.get("target", "."), *options]
        wait = min(spec.get("wait", timeout), MAX_WAIT)
        argv += ["--at", at, "--name", SESSION, "--timeout", str(wait)]
        program = point.get("program", spec.get("program"))
        if program:
            argv += ["--", *program]
        code, answer, err, seconds, command = peeky(container, argv, timeout + 120)
        record["commands"] = [{"command": command, "exit": code, "seconds": seconds}]
        outcome, detail = classify(code, answer, err)
        if outcome == "running" and spec.get("trigger"):
            t_code, t_out, t_err, t_seconds = run(container, spec["trigger"], 300)
            record["trigger"] = {
                "exit": t_code,
                "seconds": t_seconds,
                "output": tail(t_out + t_err, 1500),
            }
        deadline = time.monotonic() + timeout
        while outcome == "running" and time.monotonic() < deadline:
            wait = max(1, min(MAX_WAIT, int(deadline - time.monotonic())))
            code, answer, err, seconds, command = peeky(
                container,
                ["session", "wait", "--name", SESSION, "--timeout", str(wait)],
                wait + 120,
            )
            record["commands"].append({"command": command, "exit": code, "seconds": seconds})
            outcome, detail = classify(code, answer, err)
        if outcome == "running":
            outcome = "timed out"
        if outcome == "error" and code is None:
            outcome = "timed out"
        record.update(outcome=outcome, detail=detail)
        if outcome in ("no stop", "timed out"):
            # What the program did instead: its exit and the end of its output.
            code, answer, err, seconds, command = peeky(
                container, ["session", "report", "--name", SESSION], 120
            )
            if isinstance(answer, dict) and not answer.get("error"):
                record["program"] = {
                    "state": answer.get("state"),
                    "exit_code": answer.get("exit_code"),
                    "output_tail": tail(str(answer.get("output") or ""), 2500),
                }
        if outcome == "reached":
            requested = int(at.rsplit(":", 1)[1])
            location = str(detail.get("location") or "")
            stopped = location.rsplit(":", 1)[-1]
            record["moved"] = bool(stopped.isdigit() and int(stopped) != requested)
            code, answer, err, seconds, command = peeky(
                container, ["session", "inspect", "--name", SESSION], 180
            )
            record["commands"].append({"command": command, "exit": code, "seconds": seconds})
            if isinstance(answer, dict) and not answer.get("error"):
                record["values"] = values(answer)
            else:
                record["values"] = {"error": (answer or {}).get("error") or tail(err, 1500)}
    finally:
        peeky(container, ["session", "close", "--name", SESSION], 120)
        if spec.get("cleanup"):
            run(container, spec["cleanup"], 120)
        if spec.get("launch"):
            kill_group(container, pid_file)
        # Nothing a point started outlives it: a JVM still on the debug port would take the
        # next point's attach.
        record["leftover_processes"] = studylib.kill_leftovers(container)
    return record


def check_bug(bug_id, spec, tag, out_dir, bugs=ROUND1 / "bugs"):
    bug = studylib.load_bug(bug_id, bugs)
    image = bug["control_image"].rsplit(":", 1)[0] + ":" + tag
    info = studylib.image_info(image)
    container = "reach-{}-{}".format(bug_id, secrets.token_hex(3))
    result = {
        "bug": bug_id,
        "language": bug["meta"].get("language"),
        "category": bug["meta"].get("category"),
        "image": image,
        "image_id": info["id"],
        "peeky_sha": info["peeky_sha"],
        "started": now(),
        "needs": spec.get("needs", []),
        "notes": spec.get("notes"),
        "prepare": [],
        "points": [],
    }
    studylib.start_container(image, container)
    try:
        run(container, "mkdir -p " + WORK, 60)
        test_patch = (bug["dir"] / "test.patch").read_text()
        write_file(container, "/grade/test.patch", test_patch)
        code, out, err, _ = run(container, "git apply /grade/test.patch", 120)
        result["test_patch_applied"] = code == 0
        if code != 0:
            result["test_patch_error"] = tail(out + err, 1500)
        for path, content in (spec.get("files") or {}).items():
            text = content if isinstance(content, str) else json.dumps(content, indent=1) + "\n"
            write_file(container, path, text)
        for command in spec.get("prepare", []):
            code, out, err, seconds = run(container, command, spec.get("prepare_timeout", 1800))
            result["prepare"].append(
                {
                    "command": command,
                    "exit": code,
                    "seconds": seconds,
                    "output": tail(out + err, 3000),
                }
            )
        applied = set()
        for point in spec["points"]:
            result["points"].append(try_point(container, spec, point, applied))
            print(
                "  {} {} {}: {}".format(
                    bug_id,
                    point["role"],
                    result["points"][-1].get("at", "-"),
                    result["points"][-1]["outcome"],
                ),
                file=sys.stderr,
                flush=True,
            )
    except Exception as error:
        result["error"] = repr(error)
    finally:
        studylib.remove_container(container)
    result["ended"] = now()
    out_dir.mkdir(parents=True, exist_ok=True)
    studylib.write_json(out_dir / "{}.json".format(bug_id), result)
    return result


# ---------------------------------------------------------------------------------------------
# REACH.md's tables
# ---------------------------------------------------------------------------------------------


def short(record):
    text = short_outcome(record)
    first = record.get("first_try")
    while first:
        why = "after `{}`".format(first["remedy"]["command"]) if first.get("remedy") else "retried"
        text += " ({}; first: `{}`)".format(why, (first.get("detail") or {}).get("code"))
        first = first.get("first_try")
    return text


def short_outcome(record):
    outcome = record["outcome"]
    detail = record.get("detail") or {}
    if outcome == "reached":
        v = record.get("values") or {}
        text = "reached"
        if record.get("moved"):
            text += " (moved to {})".format(detail.get("location", "?").rsplit("/", 1)[-1])
        if "shown" in v:
            text += "; {} values, {} withheld".format(v["shown"], v["withheld"])
        elif v.get("error"):
            error = v["error"] if isinstance(v["error"], dict) else {"message": v["error"]}
            text += "; inspect failed: `{}` {}".format(error.get("code"), error.get("message"))
        return text
    if outcome == "refused":
        return "refused: `{}` {}".format(detail.get("code"), (detail.get("message") or "")[:300])
    if outcome == "not attempted":
        return "not attempted: " + detail.get("reason", "")
    if outcome == "no stop":
        what = detail.get("message") or "the program {}".format(detail.get("state"))
        program = record.get("program") or {}
        if program.get("exit_code") is not None:
            what += ", exit code {}".format(program["exit_code"])
        return "no stop: " + str(what)[:300]
    return "{}: {}".format(outcome, str(detail.get("message") or detail)[:300])


def report(directory):
    rows = []
    for path in sorted(Path(directory).glob("*.json")):
        rows.append(json.loads(path.read_text()))
    lines = ["| Bug | Lang. | Point | Where | Outcome |", "| --- | --- | --- | --- | --- |"]
    counts = {}
    for row in rows:
        for point in row["points"]:
            key = (point["role"], point["outcome"])
            counts[key] = counts.get(key, 0) + 1
            lines.append(
                "| {} | {} | {} | `{}` | {} |".format(
                    row["bug"],
                    row["language"],
                    point["role"],
                    point.get("at", "-"),
                    short(point).replace("|", "\\|").replace("\n", " "),
                )
            )
    print("\n".join(lines))
    print()
    print("| Point | Outcome | Count |")
    print("| --- | --- | --- |")
    for (role, outcome), n in sorted(counts.items()):
        print("| {} | {} | {} |".format(role, outcome, n))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bugs", nargs="*")
    parser.add_argument("--specs", default=str(SPECS))
    parser.add_argument(
        "--bugs-dir",
        default=str(ROUND1 / "bugs"),
        help="the bugs' directories; a bug's reach.json there is its spec when --specs has none",
    )
    parser.add_argument("--tag", default="peeky-r2")
    parser.add_argument(
        "--out", help="default: results/reach/<peeky sha, 12>, beside the bugs' directory"
    )
    parser.add_argument("--report", metavar="DIR")
    args = parser.parse_args()
    if args.report:
        report(args.report)
        return
    bugs_dir = Path(args.bugs_dir)
    specs = json.loads(Path(args.specs).read_text()) if Path(args.specs).is_file() else {}
    specs.pop("_comment", None)
    # A later round keeps each bug's spec beside it (value-study-3/bugs/<id>/reach.json).
    for path in sorted(bugs_dir.glob("*/reach.json")):
        specs.setdefault(path.parent.name, json.loads(path.read_text()))
    ids = args.bugs or sorted(specs)
    for bug_id in ids:
        spec = specs[bug_id]
        out = args.out
        if not out:
            bug = studylib.load_bug(bug_id, bugs_dir)
            image = bug["control_image"].rsplit(":", 1)[0] + ":" + args.tag
            sha = studylib.image_info(image)["peeky_sha"] or "unknown"
            results = ROUND2 if bugs_dir == ROUND1 / "bugs" else bugs_dir.parent
            out = results / "results" / "reach" / sha[:12]
        print("{}: {}".format(bug_id, now()), file=sys.stderr, flush=True)
        check_bug(bug_id, spec, args.tag, Path(out), bugs_dir)


if __name__ == "__main__":
    main()
