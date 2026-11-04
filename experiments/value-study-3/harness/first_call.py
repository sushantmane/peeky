#!/usr/bin/env python3
"""Round 3's one-call check: does `peeky probe … --test -- <the bug's test command>` stop at a
fix line and read a value, in one call? An extra column beside the reach check (README, "The
no-LLM filter"). It decides nothing: the reach check does.

    harness/first_call.py [bug ...] --reach results/reach/<sha, 12> [--out DIR]
    harness/first_call.py --report DIR

For each bug, in a fresh container from its `peeky-study/<id>:peeky-r3` image, with no network,
4 CPUs and 6 GB, set up as the reach check sets it up (the hidden test patch, the spec's files
and `prepare` commands), each gold point of its reach.json gets one command, with no retry:

    peeky probe . --at FILE:LINE --capture NAMES --timeout T --json --test -- <test command>

- The test command is the gold point's run in the reach check, written in docs/FIRST.md's forms
  (test_command below): `python -m pytest ARGS`, or the project's own script (`python
  tests/runtests.py ARGS`, `python bin/test ARGS`, which the reach check had to copy to a .py
  file); the Maven or Gradle command that `launch` ran, less the switch that made the test JVM
  wait for a debugger; `cargo test -p PKG --test T [--features F] FILTER -- ARGS` or `cargo run
  --bin B -- ARGS`, from the point's config; a C binary with its arguments.
- NAMES: up to 4 names that `session inspect` read at the point in the reach run given (--reach),
  else in the first pass's (3e51ccd7232d); none if neither read one.
- T: the spec's `timeout`, at most 300 s, a probe's longest (a Java --test's build is within
  it; a Rust --test's build is not).

A point's outcome: `read` (it stopped, and a captured value was read with no note), `stopped,
none read`, `no stop`, `refused` (Peeky's error code and message), `timed out` or `error`. A
bug is read in one call when one of its gold points is. Results: <out>/<bug>.json. Python 3.9+,
standard library only."""

import argparse
import json
import secrets
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUND3 = HERE.parent
sys.path.insert(0, str(ROUND3.parent / "value-study-2" / "harness"))
import reach  # noqa: E402
import studylib  # noqa: E402

BUGS = ROUND3 / "bugs"
FIRST_PASS = ROUND3 / "results" / "reach" / "3e51ccd7232d"
MAX_NAMES = 4
MAX_PROBE_S = 300
# The switches that made the test JVM wait for the reach check's attach: --test adds its own.
DEBUG_SWITCHES = ("-Dmaven.surefire.debug", "--debug-jvm")


def test_command(spec, point):
    """The words after `--test --` for a gold point, from its run in the reach check."""
    options = point.get("options", spec.get("options", []))
    program = list(point.get("program", spec.get("program")) or [])
    kind = spec.get("kind")
    if kind == "java":
        words = shlex.split(spec["launch"].split(" & ", 1)[0])
        return [w for w in words if w not in DEBUG_SWITCHES]
    if kind == "python":
        if options[:2] == ["--run-module", "pytest"]:
            return ["python", "-m", "pytest", *program]
        script = options[options.index("--run") + 1]
        # The reach check's copy of bin/test (`--run` takes a .py file), and pytest's own
        # __main__ (`--run-module pytest` in pytest's checkout): each is a correction that a
        # Peeky message named. The project's command is bin/test, and `python -m pytest`.
        if script == "bin/reach_test.py":
            script = "bin/test"
        if script == "src/pytest/__main__.py":
            return ["python", "-m", "pytest", *program]
        return ["python", script, *program]
    if kind in ("c", "cpp"):
        return [options[options.index("--binary") + 1], *program]
    if kind == "rust":
        config = spec["files"][options[options.index("--config") + 1]]
        run = config["run"]
        if "bin" in run:
            return ["cargo", "run", "--bin", run["bin"], "--", *program]
        words = ["cargo", "test", "-p", run["package"]]
        words += ["--lib"] if run.get("lib") else ["--test", run["test"]]
        if config.get("features"):
            words += ["--features", ",".join(config["features"])]
        filters = [w for w in program if not w.startswith("-")]
        harness = [w for w in program if w.startswith("-")]
        return [*words, *filters[:1], "--", *harness, *filters[1:]]
    raise ValueError("no test command for a {} spec".format(kind))


def names_read(record_dir, bug_id, at):
    """The names `session inspect` read at `at` in a reach run's record, shown values first."""
    path = Path(record_dir) / "{}.json".format(bug_id)
    if not path.is_file():
        return []
    for point in json.loads(path.read_text()).get("points", []):
        if point.get("at") == at and point.get("outcome") == "reached":
            sample = (point.get("values") or {}).get("sample") or []
            return [entry["name"] for entry in sample if entry.get("name")][:MAX_NAMES]
    return []


def classify(code, answer, err):
    """(outcome, detail, values) of one probe's answer."""
    if not isinstance(answer, dict):
        return ("timed out" if code is None else "error"), {"stderr": reach.tail(err, 2000)}, None
    error = answer.get("error")
    if error and not answer.get("hits"):
        detail = {k: error.get(k) for k in ("code", "message", "hint")}
        kinds = {"NO_STOP": "no stop", "TIMEOUT": "timed out"}
        return kinds.get(error.get("code"), "refused"), detail, None
    detail = {k: answer.get(k) for k in ("status", "ended", "hits", "exit_code", "seconds")}
    detail["warnings"] = answer.get("warnings") or []
    if error:
        detail["error"] = {k: error.get(k) for k in ("code", "message")}
    if not answer.get("hits"):
        return "no stop", detail, None
    read, withheld, sample = 0, 0, []
    for row in answer.get("rows") or []:
        value = str(row.get("value", ""))
        noted = any(row.get(key) for key in (*reach.NOTE_KEYS, "error")) or "value" not in row
        if noted or reach.WITHHELD_TEXT.search(value):
            withheld += 1
        else:
            read += 1
        if len(sample) < 6:
            entry = {"selector": row.get("selector"), "value": value[:160]}
            for key in (*reach.NOTE_KEYS, "error"):
                if row.get(key):
                    entry[key] = str(row[key])[:300]
            sample.append(entry)
    values = {"read": read, "withheld": withheld, "sample": sample}
    return ("read" if read else "stopped, none read"), detail, values


def check_bug(bug_id, reach_dir, out_dir):
    spec = json.loads((BUGS / bug_id / "reach.json").read_text())
    bug = studylib.load_bug(bug_id, BUGS)
    image = bug["control_image"].rsplit(":", 1)[0] + ":peeky-r3"
    info = studylib.image_info(image)
    container = "first-call-{}-{}".format(bug_id.lower(), secrets.token_hex(3))
    result = {
        "bug": bug_id,
        "language": bug["meta"].get("language"),
        "image": image,
        "image_id": info["id"],
        "peeky_sha": info["peeky_sha"],
        "started": reach.now(),
        "prepare": [],
        "points": [],
    }
    timeout = min(spec.get("timeout", 300), MAX_PROBE_S)
    studylib.start_container(image, container)
    try:
        reach.run(container, "mkdir -p " + reach.WORK, 60)
        reach.write_file(container, "/grade/test.patch", (bug["dir"] / "test.patch").read_text())
        code, out, err, _ = reach.run(container, "git apply /grade/test.patch", 120)
        result["test_patch_applied"] = code == 0
        for path, content in (spec.get("files") or {}).items():
            text = content if isinstance(content, str) else json.dumps(content, indent=1) + "\n"
            reach.write_file(container, path, text)
        for command in spec.get("prepare", []):
            code, out, err, seconds = reach.run(
                container, command, spec.get("prepare_timeout", 1800)
            )
            result["prepare"].append(
                {
                    "command": command,
                    "exit": code,
                    "seconds": seconds,
                    "output": reach.tail(out + err, 1500),
                }
            )
        for point in spec["points"]:
            if point["role"] != "gold":
                continue
            record = {"why": point.get("why")}
            result["points"].append(record)
            if point.get("not_attempted"):
                record.update(outcome="not attempted", detail={"reason": point["not_attempted"]})
                continue
            try:
                at = reach.resolve_at(container, point)
                words = test_command(spec, point)
            except Exception as error:  # the harness's own error
                record.update(outcome="error", detail={"message": str(error)})
                continue
            names = names_read(reach_dir, bug_id, at) or names_read(FIRST_PASS, bug_id, at)
            argv = ["probe", ".", "--at", at]
            if names:
                argv += ["--capture", ",".join(names)]
            argv += ["--timeout", str(timeout), "--json", "--test", "--", *words]
            script = "peeky " + " ".join(shlex.quote(a) for a in argv)
            # A Rust or Java --test builds first: its build isn't under --timeout.
            code, out, err, seconds = reach.run(container, script, timeout + 2400)
            try:
                answer = json.loads(out)
            except ValueError:
                answer = None
                err = (err + "\n[stdout] " + out).strip()
            outcome, detail, values = classify(code, answer, err)
            record.update(
                at=at,
                test_command=shlex.join(words),
                captures=names,
                command=script,
                exit=code,
                seconds=seconds,
                outcome=outcome,
                detail=detail,
            )
            if values is not None:
                record["values"] = values
            if outcome != "read":
                record["stderr_tail"] = reach.tail(err, 1500)
            record["leftover_processes"] = studylib.kill_leftovers(container)
            print("  {} {}: {}".format(bug_id, at, outcome), file=sys.stderr, flush=True)
    except Exception as error:
        result["error"] = repr(error)
    finally:
        studylib.remove_container(container)
    result["ended"] = reach.now()
    out_dir.mkdir(parents=True, exist_ok=True)
    studylib.write_json(out_dir / "{}.json".format(bug_id), result)
    return result


def short(point):
    outcome, detail = point["outcome"], point.get("detail") or {}
    if outcome == "read":
        return "read, {} of {}".format(point["values"]["read"], len(point.get("captures") or []))
    if outcome == "refused":
        return "refused: `{}` {}".format(detail.get("code"), (detail.get("message") or "")[:240])
    if outcome == "no stop" and detail.get("message"):
        return "no stop: " + detail["message"][:240]
    return outcome


def report(directory):
    rows = [json.loads(p.read_text()) for p in sorted(Path(directory).glob("*.json"))]
    print("| Bug | Lang. | Gold point | Test command | One call |")
    print("| --- | --- | --- | --- | --- |")
    read = 0
    for row in rows:
        points = row["points"] or [{"outcome": "error", "detail": {"message": row.get("error")}}]
        read += any(p["outcome"] == "read" for p in points)
        for p in points:
            print(
                "| {} | {} | `{}` | `{}` | {} |".format(
                    row["bug"],
                    row["language"],
                    p.get("at", "-"),
                    (p.get("test_command") or "-").replace("|", "\\|"),
                    short(p).replace("|", "\\|").replace("\n", " "),
                )
            )
    print("\n{} of {} bugs read a value at a gold point in one call.".format(read, len(rows)))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bugs", nargs="*")
    parser.add_argument("--reach", help="the reach run whose read names are captured")
    parser.add_argument("--out", help="default: results/first-call/<the --reach run's name>")
    parser.add_argument("--report", metavar="DIR")
    parser.add_argument("--print", action="store_true", help="print each test command only")
    args = parser.parse_args()
    if args.report:
        report(args.report)
        return
    if args.print:
        for bug_id in args.bugs:
            spec = json.loads((BUGS / bug_id / "reach.json").read_text())
            for point in spec["points"]:
                if point["role"] == "gold" and not point.get("not_attempted"):
                    print("{}: {}".format(bug_id, shlex.join(test_command(spec, point))))
        return
    if not args.reach:
        parser.error("--reach is required")
    out = Path(args.out or ROUND3 / "results" / "first-call" / Path(args.reach).name)
    for bug_id in args.bugs:
        print("{}: {}".format(bug_id, reach.now()), file=sys.stderr, flush=True)
        check_bug(bug_id, Path(args.reach), out)


if __name__ == "__main__":
    main()
