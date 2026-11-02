"""Shared pieces of the study harness: paths, bug files, Docker and grading.

Python 3.9+, standard library only (the host's python3 runs it)."""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
STUDY = HARNESS.parent

# A later round reuses this harness with a study config: a JSON file that $PEEKY_STUDY_CONFIG
# names (value-study-2/study.json). Without one, everything is as this round ran it. Paths in a
# config are relative to the config's directory. Its keys:
#   "bugs", "results": directories; "seed": the schedule's shuffle and the bootstrap's seed;
#   "arms": {arm: {"image_tag": tag, "prompts": [files, joined], "peeky": bool}}, in order;
#   "comparisons": [[treatment, base], ...], the first being the primary outcome.
DEFAULT_ARMS = {
    "control": {"image_tag": "control", "prompts": [str(HARNESS / "prompt.md")], "peeky": False},
    "peeky": {
        "image_tag": "peeky",
        "prompts": [str(HARNESS / "prompt.md"), str(HARNESS / "prompt-peeky.md")],
        "peeky": True,
    },
}


def _load_config():
    path = os.environ.get("PEEKY_STUDY_CONFIG")
    if not path:
        return None, {}
    path = Path(path).resolve()
    data = json.loads(path.read_text())
    base = path.parent

    def resolve(value):
        return str((base / value).resolve())

    for key in ("bugs", "results"):
        if key in data:
            data[key] = resolve(data[key])
    for arm in (data.get("arms") or {}).values():
        arm["prompts"] = [resolve(prompt) for prompt in arm.get("prompts", [])]
    return path, data


CONFIG_PATH, CONFIG = _load_config()
BUGS = Path(CONFIG.get("bugs") or STUDY / "bugs")
RESULTS = Path(CONFIG.get("results") or STUDY / "results")
RUNS = RESULTS / "runs"
ARM_SPECS = CONFIG.get("arms") or DEFAULT_ARMS
ARMS = tuple(ARM_SPECS)
SEED = int(CONFIG.get("seed", 441))
COMPARISONS = [tuple(pair) for pair in CONFIG.get("comparisons", [["peeky", "control"]])]
# Whether the analysis adds what round 1's report didn't have (more arms, values printed).
EXTENDED = set(ARMS) != {"control", "peeky"}
START_MARKER = ">>>>> Start Test Output"
END_MARKER = ">>>>> End Test Output"
PASSING = ("PASSED", "XFAIL")


# ---------------------------------------------------------------------------------------------
# A small YAML reader for bug.yaml: a top-level mapping whose values are plain, quoted or block
# (| >) scalars, flow lists ([a, b]) or block lists whose items are scalars or flat mappings.
# ---------------------------------------------------------------------------------------------


def _scalar(text):
    text = text.strip()
    if not text:
        return ""
    if text[0] in "\"'":
        quote = text[0]
        end = text.rfind(quote)
        body = text[1:end] if end > 0 else text[1:]
        if quote == "'":
            return body.replace("''", "'")
        try:
            return json.loads('"' + body + '"')
        except ValueError:
            return body
    text = re.sub(r"\s+#.*$", "", text)
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(part) for part in _split_flow(inner)] if inner else []
    if text.startswith("{") and text.endswith("}"):
        result = {}
        for part in _split_flow(text[1:-1]):
            key, _, value = part.partition(":")
            result[_scalar(key)] = _scalar(value)
        return result
    lowered = text.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d*", text):
        return float(text)
    return text


def _split_flow(text):
    parts, depth, quote, current = [], 0, None, ""
    for char in text:
        if quote:
            current += char
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current)
    return parts


def _block_scalar(indicator, lines):
    if not lines:
        return ""
    indent = (
        min(len(line) - len(line.lstrip(" ")) for line in lines if line.strip())
        if any(line.strip() for line in lines)
        else 0
    )
    body = [line[indent:] if line.strip() else "" for line in lines]
    while body and not body[-1]:
        body.pop()
    if indicator.startswith(">"):
        text, paragraph = [], []
        for line in body:
            if line and not line.startswith(" "):
                paragraph.append(line)
            else:
                if paragraph:
                    text.append(" ".join(paragraph))
                    paragraph = []
                text.append(line)
        if paragraph:
            text.append(" ".join(paragraph))
        result = "\n".join(text)
    else:
        result = "\n".join(body)
    return result if "-" in indicator else result + "\n"


def _block_list(lines):
    items, current = [], None
    item_indent = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if stripped.startswith("- ") or stripped == "-":
            if item_indent is None:
                item_indent = indent
            if indent == item_indent:
                if current is not None:
                    items.append(current)
                current = [stripped[2:] if stripped != "-" else ""]
                continue
        if current is not None:
            current.append(stripped)
    if current is not None:
        items.append(current)
    result = []
    for parts in items:
        first = parts[0]
        mapping = re.match(r"^([A-Za-z_][\w-]*):(\s|$)", first)
        if mapping:
            entry = {}
            for part in parts:
                key, sep, value = part.partition(":")
                if sep and re.fullmatch(r"[A-Za-z_][\w-]*", key.strip()):
                    entry[key.strip()] = _scalar(value)
                    last = key.strip()
                elif entry:
                    entry[last] = "{} {}".format(entry[last], part).strip()
            result.append(entry)
        else:
            value = " ".join(parts)
            result.append(_scalar(value) if len(parts) == 1 else value)
    return result


def load_yaml(path):
    lines = Path(path).read_text().splitlines()
    data, index = {}, 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r"^([A-Za-z_][\w-]*):(?:\s+(.*))?$", line)
        if not match:
            index += 1
            continue
        key, rest = match.group(1), (match.group(2) or "").rstrip()
        block, index = [], index + 1
        while index < len(lines) and (not lines[index].strip() or lines[index][0] in " \t-#"):
            if lines[index].startswith("#"):
                index += 1
                continue
            block.append(lines[index].rstrip())
            index += 1
        if re.fullmatch(r"[|>][-+]?\d*(\s+#.*)?", rest):
            data[key] = _block_scalar(rest.split()[0], block)
        elif not rest:
            if any(item.strip().startswith("-") for item in block):
                data[key] = _block_list(block)
            else:
                text = " ".join(item.strip() for item in block if item.strip())
                data[key] = _scalar(text) if text else None
        elif rest[0] in "\"'" and (len(rest) < 2 or rest[-1] != rest[0]):
            data[key] = _scalar(" ".join([rest] + [item.strip() for item in block]))
        elif block and any(item.strip() for item in block) and rest[0] not in "[{":
            data[key] = _scalar(" ".join([rest] + [item.strip() for item in block if item.strip()]))
        else:
            data[key] = _scalar(rest)
    return data


# ---------------------------------------------------------------------------------------------
# Bugs
# ---------------------------------------------------------------------------------------------


def bug_dir(bug_id, bugs=None):
    return Path(bugs or BUGS) / bug_id


def load_bug(bug_id, bugs=None):
    """The bug's files and fields. `control_image` and `peeky_image` derive from bug.yaml's
    `image` (default peeky-study/<id lowercased>:control)."""
    directory = bug_dir(bug_id, bugs)
    yaml_path = directory / "bug.yaml"
    meta = load_yaml(yaml_path) if yaml_path.is_file() else {}
    grade_path = directory / "grade.json"
    grade = json.loads(grade_path.read_text()) if grade_path.is_file() else {}
    control = str(meta.get("image") or "peeky-study/{}:control".format(bug_id.lower()))
    base = (
        control[: -len(":control")] if control.endswith(":control") else control.rsplit(":", 1)[0]
    )
    return {
        "id": bug_id,
        "dir": directory,
        "meta": meta,
        "grade": grade,
        "control_image": control,
        "peeky_image": base + ":peeky",
        "image_base": base,
    }


def image_for(bug, arm):
    if not CONFIG:
        return bug["control_image"] if arm == "control" else bug["peeky_image"]
    tag = ARM_SPECS[arm]["image_tag"]
    return bug["control_image"] if tag == "control" else "{}:{}".format(bug["image_base"], tag)


def arm_uses_peeky(arm):
    return bool(ARM_SPECS[arm]["peeky"])


def arm_prompts(arm):
    """The prompt files an arm's prompt joins, in order."""
    return [Path(prompt) for prompt in ARM_SPECS[arm]["prompts"]]


def validation_ok(directory):
    """Whether validation.json shows base unsolved (every FAIL_TO_PASS not passing) and both
    gold runs solved."""
    path = Path(directory) / "validation.json"
    if not path.is_file():
        return False
    try:
        runs = json.loads(path.read_text())
    except ValueError:
        return False
    if isinstance(runs, dict):
        runs = runs.get("runs") or list(runs.values())
    by_name = {run.get("name"): run for run in runs if isinstance(run, dict)}
    base, gold1, gold2 = by_name.get("base"), by_name.get("gold-1"), by_name.get("gold-2")
    if not (base and gold1 and gold2):
        return False
    base_ok = base.get("solved") is False and not any(
        status in PASSING for status in (base.get("f2p") or {}).values()
    )
    return bool(base_ok and gold1.get("solved") is True and gold2.get("solved") is True)


def test_patch_files(patch_text):
    """Paths a unified diff touches (old and new sides, without /dev/null)."""
    files = []
    for line in patch_text.splitlines():
        match = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if match:
            for path in match.groups():
                if path not in files:
                    files.append(path)
            continue
        match = re.match(r"^(?:---|\+\+\+) (?:[ab]/)?(\S+)", line)
        if match and match.group(1) != "/dev/null" and match.group(1) not in files:
            files.append(match.group(1))
    return files


# ---------------------------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------------------------


class DockerError(RuntimeError):
    """The Docker daemon or CLI failed (an infrastructure failure, not the agent's)."""


def docker(*args, input=None, timeout=300, check=True, text=True):
    try:
        result = subprocess.run(
            ["docker", *args],
            input=input,
            capture_output=True,
            text=text,
            timeout=timeout,
            stdin=None if input is not None else subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as error:
        raise DockerError("docker {} timed out after {} s".format(args[0], timeout)) from error
    if check and result.returncode != 0:
        stderr = result.stderr if text else result.stderr.decode("utf-8", "replace")
        raise DockerError(
            "docker {} failed ({}): {}".format(
                " ".join(args[:2]), result.returncode, stderr.strip()[-2000:]
            )
        )
    return result


def image_exists(image):
    return (
        subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    )


def image_info(image):
    data = json.loads(docker("image", "inspect", image).stdout)[0]
    labels = (data.get("Config") or {}).get("Labels") or {}
    return {
        "id": data.get("Id"),
        "digests": data.get("RepoDigests") or [],
        "peeky_sha": labels.get("peeky-study.peeky-sha"),
    }


def docker_up():
    try:
        docker("info", "--format", "{{.ServerVersion}}", timeout=30)
        return True
    except DockerError:
        return False


def start_container(image, name, memory="6g", cpus="4", read_only_mounts=()):
    """A container that sleeps; read_only_mounts are host directories mounted read-only at the
    same path."""
    mounts = []
    for path in read_only_mounts:
        mounts += ["-v", "{0}:{0}:ro".format(path)]
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        "none",
        "--cap-add",
        "SYS_PTRACE",
        "--security-opt",
        "seccomp=unconfined",
        "--memory",
        memory,
        "--cpus",
        str(cpus),
        "--label",
        "peeky-study=1",
        *mounts,
        "--entrypoint",
        "",
        image,
        "sleep",
        "infinity",
        timeout=120,
    )
    return name


def remove_container(name):
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)


def exec_in(container, script, timeout=None, input=None, workdir="/testbed"):
    """Runs `bash -c script` in the container; returns (exit code, stdout+stderr, timed_out).
    A timeout kills the docker exec client; kill_leftovers() stops what it started."""
    command = ["docker", "exec", "-i", "-w", workdir, container, "bash", "-c", script]
    try:
        result = subprocess.run(
            command,
            input=input if input is not None else b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        output = error.output or b""
        return None, output.decode("utf-8", "replace"), True
    output = result.stdout.decode("utf-8", "replace")
    if result.returncode in (125, 126, 127) and "Error response from daemon" in output:
        raise DockerError(output.strip()[-2000:])
    return result.returncode, output, False


def kill_leftovers(container):
    """Kills every process in the container but its PID 1; returns how many there were."""
    code, output, _ = exec_in(
        container,
        'n=0; for p in $(ps -e -o pid=); do [ "$p" = 1 ] || [ "$p" = $$ ] || [ ! -d /proc/$p ] || '
        "n=$((n + 1)); done; kill -9 -1 2>/dev/null; echo $n",
        timeout=60,
        workdir="/",
    )
    try:
        return int(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------------------------


def extract_test_output(log):
    start = log.find(START_MARKER)
    if start < 0:
        return None
    start += len(START_MARKER)
    end = log.find(END_MARKER, start)
    return log[start : end if end >= 0 else len(log)]


def compare(grade, statuses, completed=True):
    f2p = {test: statuses.get(test, "MISSING") for test in grade.get("fail_to_pass", [])}
    p2p_failed = [
        test
        for test in grade.get("pass_to_pass", [])
        if statuses.get(test, "MISSING") not in PASSING
    ]
    solved = bool(completed and f2p and all(s in PASSING for s in f2p.values()) and not p2p_failed)
    return {"f2p": f2p, "p2p_failed": p2p_failed, "solved": solved}


def _parsed_statuses(text):
    """parse.py's JSON object: the whole output, else from its first `{` on, else the last
    line (when the parser printed something before it)."""
    text = text.strip()
    candidates = [text]
    if "{" in text:
        candidates.append(text[text.index("{") :])
    if text:
        candidates.append(text.splitlines()[-1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def grade_container(container, bug, log_path=None, apply_gold=False):
    """Grades /testbed in a running container: copies test.patch, eval.sh and parse.py to
    /grade, runs eval.sh with grade.json's timeout, parses the marked output with parse.py
    (in the container) and compares with grade.json."""
    directory, grade = bug["dir"], bug["grade"]
    started = time.monotonic()
    exec_in(container, "rm -rf /grade && mkdir -p /grade", timeout=60, workdir="/")
    for name in ("test.patch", "eval.sh", "parse.py") + (("gold.patch",) if apply_gold else ()):
        docker("cp", str(directory / name), "{}:/grade/{}".format(container, name), timeout=120)
    if apply_gold:
        code, output, _ = exec_in(container, "git apply /grade/gold.patch", timeout=120)
        if code != 0:
            raise RuntimeError("gold.patch does not apply: " + output[-1000:])
    timeout = int(grade.get("timeout_s") or 1200)
    code, log, timed_out = exec_in(container, "bash /grade/eval.sh", timeout=timeout)
    seconds = round(time.monotonic() - started, 1)
    if timed_out:
        kill_leftovers(container)
    if log_path:
        Path(log_path).write_text(log)
    body = extract_test_output(log)
    statuses, parse_error = {}, None
    if body is not None:
        parse_code, parsed, _ = exec_in(
            container, "python3 /grade/parse.py", timeout=300, input=body.encode()
        )
        statuses = _parsed_statuses(parsed) if parse_code == 0 else None
        if statuses is None:
            statuses, parse_error = {}, parsed[-1000:]
    result = compare(grade, statuses, completed=body is not None and not timed_out)
    result.update(
        {
            "seconds": seconds,
            "eval_exit": code,
            "eval_timed_out": timed_out,
            "markers_found": body is not None,
            "tests_parsed": len(statuses),
        }
    )
    if parse_error:
        result["parse_error"] = parse_error
    return result


def which_or_die(program):
    path = shutil.which(program)
    if not path:
        raise RuntimeError("{} not found on PATH".format(program))
    return path


def write_json(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=1, sort_keys=False) + "\n")
    os.replace(str(temporary), str(path))
