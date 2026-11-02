"""Reads an attempt's Copilot CLI output (events.jsonl from `--output-format json`, the
session's own events file, usage.json) into the metrics result.json records.

Python 3.9+, standard library only."""

import gzip
import json
import re
import shlex
from collections import Counter, OrderedDict
from pathlib import Path

ALLOWED_TOOLS = ("bash", "read_bash", "write_bash", "stop_bash", "list_bash")
PEEKY_PROGRAMS = ("peeky", "peeky-probe", "peeky-worker", "peeky-agent")
WITHHELD_MARKERS = ("<not shown", "<not read", "[unproven]", "[stale]", "withheld", "not settable")

# Runs of a project's build or tests, by language: the programs (after `python -m`, the
# module), and for cargo its subcommands. The bug's test_hint adds the programs it names.
BUILD_TEST_PROGRAMS = {
    "python": {"pytest", "py.test", "runtests", "runtests.py", "tox", "nox", "unittest"},
    "java": {"mvn", "mvnw", "gradle", "gradlew", "ant"},
    "rust": set(),
    "c": {"make", "cmake", "ctest", "ninja", "meson"},
}
BUILD_TEST_PROGRAMS["cpp"] = BUILD_TEST_PROGRAMS["c++"] = BUILD_TEST_PROGRAMS["c"]
CARGO_BUILD_TEST = {"test", "build", "nextest"}
BIN_TEST = re.compile(r"(?:^|[\s;&|(])(?:\./|/testbed/)?bin/test(?:\s|$)")
PYTHON_SCRIPT_SUBCOMMANDS = {
    "manage.py": {"test"},
    "setup.py": {"test", "build", "build_ext", "develop"},
}

# Errors that are the infrastructure's, not the agent's.
INFRA_ERROR = re.compile(
    r"not authenticated|authenticat\w* (failed|required|error)|unauthori[sz]ed|\b401\b|\b403\b|"
    r"rate.?limit|\b429\b|too many requests|quota|ECONNRESET|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|"
    r"socket hang up|service unavailable|internal server error|bad gateway|gateway timeout|"
    r"\b50[0234]\b|network error|fetch failed",
    re.I,
)
CREDIT_LIMIT = re.compile(r"credit", re.I)

COMMAND_SEPARATORS = re.compile(r"&&|\|\||;|\||\n|\$\(|`|\(|\)|\{|\}")
COMMAND_PREFIXES = {
    "sudo",
    "time",
    "env",
    "nohup",
    "exec",
    "then",
    "do",
    "else",
    "if",
    "while",
    "until",
    "!",
    "xargs",
    "command",
    "stdbuf",
    "nice",
}


def read_jsonl(path):
    """The events of a JSONL file, or of its gzipped copy (<path>.gz), and the count of lines
    that weren't JSON."""
    events, bad = [], 0
    path = Path(path)
    if path.is_file():
        handle = path.open(encoding="utf-8", errors="replace")
    elif path.with_name(path.name + ".gz").is_file():
        handle = gzip.open(
            str(path.with_name(path.name + ".gz")), "rt", encoding="utf-8", errors="replace"
        )
    else:
        return events, bad
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                bad += 1
    return events, bad


def _words(segment):
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.S)


def strip_heredocs(command):
    """The command line without here-documents' bodies (file contents, not commands)."""
    return HEREDOC.sub(lambda m: m.group(0).split("\n", 1)[0], command)


def invoked_programs(command):
    """[(program basename, [arguments])] for each simple command in a shell command line, after
    env assignments and wrappers such as `timeout 60` or `env X=1`; `python -m X` is X and
    `python script.py` is script.py. Here-documents' bodies are skipped."""
    found = []
    for segment in COMMAND_SEPARATORS.split(strip_heredocs(command)):
        words = _words(segment.strip())
        while words:
            word = words[0]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word) or word in COMMAND_PREFIXES:
                words = words[1:]
            elif word == "timeout":
                words = words[1:]
                while words and (
                    words[0].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", words[0])
                ):
                    words = words[1:]
            else:
                break
        if words:
            program = words[0].rsplit("/", 1)[-1]
            arguments = words[1:]
            if re.fullmatch(r"python[\d.]*", program):
                if "-m" in arguments[:-1]:
                    at = arguments.index("-m")
                    program, arguments = arguments[at + 1], arguments[at + 2 :]
                else:
                    scripts = [i for i, a in enumerate(arguments) if a.endswith(".py")]
                    if scripts and all(a.startswith("-") for a in arguments[: scripts[0]]):
                        at = scripts[0]
                        program, arguments = arguments[at].rsplit("/", 1)[-1], arguments[at + 1 :]
            found.append((program, arguments))
    return found


def peeky_subcommands(command):
    """The Peeky subcommands a command line runs (`probe`, `session start`, ...), or []."""
    result = []
    for program, arguments in invoked_programs(command):
        if program not in PEEKY_PROGRAMS:
            continue
        if program != "peeky":
            result.append(program)
            continue
        words = [word for word in arguments if not word.startswith("-")]
        sub = (
            words[0]
            if words
            else (
                "--help"
                if "--help" in arguments or "-h" in arguments
                else "--version"
                if "--version" in arguments
                else "(none)"
            )
        )
        if sub in ("session", "queries") and len(words) > 1:
            sub = "{} {}".format(sub, words[1])
        result.append(sub)
    return result


SUBCOMMAND_TOOLS = {"cargo", "go", "npm", "yarn", "dotnet"}


def _words_of(arguments):
    return [a for a in arguments if not a.startswith("-") and not a.startswith("+")]


def hint_programs(test_hint):
    """[(program regex, subcommand or None)] for what the test hint's `code spans` run (cd and
    the like left out). A `<placeholder>` in a name matches anything; for cargo and the like,
    the subcommand must match too."""
    specs = []
    for span in re.findall(r"`([^`]+)`", test_hint or ""):
        for program, arguments in invoked_programs(span):
            if program in ("cd", "export", "source", ".", "echo", "true", "set", "ls", "cat"):
                continue
            pattern = ".+".join(re.escape(part) for part in re.split(r"<[^>]*>", program))
            words = _words_of(arguments)
            sub = words[0] if program in SUBCOMMAND_TOOLS and words else None
            if (pattern, sub) not in specs:
                specs.append((pattern, sub))
    return specs


def build_test_matcher(language, test_hint):
    """A function telling whether a command line runs the project's build or tests."""
    language = (language or "").lower()
    programs = BUILD_TEST_PROGRAMS.get(language, set())
    hints = [(re.compile(pattern + r"\Z"), sub) for pattern, sub in hint_programs(test_hint)]

    def matches(command):
        for program, arguments in invoked_programs(command):
            words = _words_of(arguments)
            if program in PYTHON_SCRIPT_SUBCOMMANDS:
                if words[:1] and words[0] in PYTHON_SCRIPT_SUBCOMMANDS[program]:
                    return True
            elif program in programs:
                return True
            if program == "cargo" and words[:1] and words[0] in CARGO_BUILD_TEST:
                return True
            for pattern, sub in hints:
                if pattern.match(program) and (sub is None or words[:1] == [sub]):
                    return True
        return bool(language == "python" and BIN_TEST.search(strip_heredocs(command)))

    return matches


def _usage_from_snapshot(snapshot):
    tokens = Counter()
    models = []
    for model, metrics in (snapshot.get("modelMetrics") or {}).items():
        models.append(model)
        usage = metrics.get("usage") or {}
        tokens["input"] += usage.get("inputTokens") or 0
        tokens["output"] += usage.get("outputTokens") or 0
        tokens["cache_read"] += usage.get("cacheReadTokens") or 0
        tokens["cache_write"] += usage.get("cacheWriteTokens") or 0
        tokens["reasoning"] += usage.get("reasoningTokens") or 0
    nano = snapshot.get("totalNanoAiu")
    return {
        "ai_credits": round(nano / 1e9, 4) if nano is not None else None,
        "tokens": dict(tokens) if tokens else None,
        "usage_models": models,
        "premium_requests": snapshot.get("totalPremiumRequestCost"),
        "ai_credits_status": snapshot.get("aiCreditsStatus"),
    }


def usage_metrics(run_dir, events, session_events):
    """Credits and tokens: usage.json, else the session's shutdown snapshot, else the sum of
    its per-call usage records, else the last usage checkpoint (credits only)."""
    path = Path(run_dir) / "usage.json"
    if path.is_file():
        try:
            result = _usage_from_snapshot(json.loads(path.read_text()))
            result["usage_source"] = "usage.json"
            return result
        except ValueError:
            pass
    for event in reversed(session_events):
        if event.get("type") == "session.shutdown":
            snapshot = (event.get("data") or {}).get("accountingSnapshot")
            if snapshot:
                result = _usage_from_snapshot(snapshot)
                result["usage_source"] = "session.shutdown"
                return result
    records = [
        ((e.get("data") or {}).get("usage") or {})
        for e in session_events
        if e.get("type") == "session.usage_record"
    ]
    if records:
        tokens, nano, models = Counter(), 0, []
        for usage in records:
            tokens["input"] += usage.get("inputTokens") or 0
            tokens["output"] += usage.get("outputTokens") or 0
            tokens["cache_read"] += usage.get("cacheReadTokens") or 0
            tokens["cache_write"] += usage.get("cacheWriteTokens") or 0
            tokens["reasoning"] += usage.get("reasoningTokens") or 0
            nano += (usage.get("copilotUsage") or {}).get("totalNanoAiu") or 0
            if usage.get("model") and usage["model"] not in models:
                models.append(usage["model"])
        return {
            "ai_credits": round(nano / 1e9, 4),
            "tokens": dict(tokens),
            "usage_models": models,
            "premium_requests": None,
            "ai_credits_status": None,
            "usage_source": "session.usage_record",
        }
    for event in reversed(events):
        if event.get("type") == "session.usage_checkpoint":
            nano = (event.get("data") or {}).get("totalNanoAiu")
            return {
                "ai_credits": round(nano / 1e9, 4) if nano is not None else None,
                "tokens": None,
                "usage_models": [],
                "premium_requests": None,
                "ai_credits_status": None,
                "usage_source": "session.usage_checkpoint",
            }
    return {
        "ai_credits": None,
        "tokens": None,
        "usage_models": [],
        "premium_requests": None,
        "ai_credits_status": None,
        "usage_source": None,
    }


SHELL_ID = re.compile(r"<shellId: (\S+?)>?\s*(?:completed with exit code (-?\d+))?")


def collect(run_dir, language="", test_hint="", arm=""):
    """Metrics from an attempt's directory (events.jsonl, session-events.jsonl, usage.json)."""
    run_dir = Path(run_dir)
    events, bad_lines = read_jsonl(run_dir / "events.jsonl")
    session_events, _ = read_jsonl(run_dir / "session-events.jsonl")

    starts, completes = OrderedDict(), {}
    messages, reasoning, models = [], [], []
    model_calls, errors, infos, warnings = 0, [], [], []
    mcp_servers, tools_offered, segments = [], [], []
    result_event = None
    for event in events:
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "tool.execution_start":
            starts[data.get("toolCallId")] = data
        elif kind == "tool.execution_complete":
            completes[data.get("toolCallId")] = data
        elif kind == "assistant.message":
            if (data.get("content") or "").strip():
                messages.append(data["content"])
            if data.get("model") and data["model"] not in models:
                models.append(data["model"])
        elif kind == "assistant.reasoning":
            if (data.get("content") or "").strip():
                reasoning.append(data["content"])
        elif kind == "model.call_start":
            model_calls += 1
            if data.get("model") and data["model"] not in models:
                models.append(data["model"])
        elif kind == "session.error":
            errors.append(data.get("message") or json.dumps(data)[:500])
        elif kind == "session.warning":
            warnings.append(data.get("message") or json.dumps(data)[:500])
        elif kind == "session.info":
            infos.append(data.get("message") or "")
        elif kind == "session.mcp_servers_loaded":
            mcp_servers = data.get("servers") or []
        elif kind == "session.usage_checkpoint":
            for state in data.get("promptCacheBreakState") or []:
                for model in (state.get("models") or {}).values():
                    tools_offered = [tool.get("name") for tool in model.get("tools") or []]
                    segments = [s.get("segment") for s in model.get("system_segments") or []]
        elif kind == "result":
            result_event = event
    if not model_calls:
        model_calls = sum(1 for e in session_events if e.get("type") == "session.usage_record")

    copilot_version = None
    for event in session_events:
        if event.get("type") == "session.start":
            copilot_version = (event.get("data") or {}).get("copilotVersion")
            break

    commands = shell_entries(starts, completes)

    is_build_or_test = build_test_matcher(language, test_hint)
    build_test, peeky, subcommands, markers = [], [], Counter(), Counter()
    doc_reads = values_printed = 0
    for entry in commands:
        command = entry["command"]
        if is_build_or_test(command):
            build_test.append(command)
        subs = peeky_subcommands(command)
        if subs:
            peeky.append({"command": command, "subcommands": subs, "exit_code": entry["exit_code"]})
            subcommands.update(subs)
            for marker in WITHHELD_MARKERS:
                markers[marker] += entry["output"].count(marker)
            values_printed += peeky_values(entry["output"])
        if "/opt/peeky/docs" in command:
            doc_reads += 1

    tool_counts = Counter(start.get("toolName") for start in starts.values())
    unexpected_tools = sorted(set(tool_counts) - set(ALLOWED_TOOLS))
    suspicious_segments = [
        s
        for s in segments
        if s and re.search(r"custom|skill|memor|repo_instruction|agents_md|mcp", s, re.I)
    ]
    active_mcp = [s.get("name") for s in mcp_servers if s.get("status") not in ("disabled", None)]
    offered_extra = sorted(set(t for t in tools_offered if t) - set(ALLOWED_TOOLS))

    usage = usage_metrics(run_dir, events, session_events)
    for model in usage.get("usage_models") or []:
        if model not in models:
            models.append(model)
    texts = errors + warnings + [line for line in _read(run_dir / "stderr.txt").splitlines()]
    peeky_mentions = sum(
        len(re.findall(r"\bpeeky\b", entry["command"], re.I)) for entry in commands
    )

    return {
        "copilot_version": copilot_version,
        "models": models,
        "model_calls": model_calls,
        "ai_credits": usage["ai_credits"],
        "ai_credits_status": usage["ai_credits_status"],
        "premium_requests": usage["premium_requests"],
        "tokens": usage["tokens"],
        "usage_source": usage["usage_source"],
        "tool_calls": len(starts),
        "tool_calls_by_name": dict(tool_counts),
        "shell_commands": [
            {"command": e["command"], "exit_code": e["exit_code"], "mode": e["mode"]}
            for e in commands
        ],
        "build_or_test_runs": len(build_test),
        "build_or_test_commands": build_test,
        "peeky_commands": peeky,
        "peeky_subcommands": dict(subcommands),
        "peeky_doc_reads": doc_reads,
        "peeky_mentions_in_commands": peeky_mentions,
        "peeky_withheld_markers": sum(markers.values()),
        "peeky_withheld_by_marker": {k: v for k, v in markers.items() if v},
        "peeky_values_printed": values_printed,
        "contaminated": bool(not uses_peeky(arm) and peeky),
        "final_message": messages[-1] if messages else "",
        "assistant_messages": messages,
        "assistant_reasoning": reasoning,
        "isolation": {
            "tools_offered": tools_offered,
            "unexpected_tools_offered": offered_extra,
            "unexpected_tools_called": unexpected_tools,
            "active_mcp_servers": active_mcp,
            "system_segments": segments,
            "suspicious_segments": suspicious_segments,
            "ok": not (offered_extra or unexpected_tools or active_mcp or suspicious_segments),
        },
        "session_errors": errors,
        "session_warnings": warnings,
        "session_infos": infos,
        "cli_exit_code_reported": (result_event or {}).get("exitCode"),
        "credit_limit_reached": any(
            CREDIT_LIMIT.search(t) and re.search(r"limit|max|exceed", t, re.I)
            for t in errors + warnings + infos + texts
        ),
        "infra_error_text": [t for t in texts if INFRA_ERROR.search(t)][:10],
        "events_unparsable_lines": bad_lines,
    }


# What a Peeky command printed of the program's values: a probe table's rows, a session's
# `name: type = value` lines, and a JSON answer's "value" fields. An approximate count, for
# comparing arms; the judge checks the values themselves.
PROBE_ROW = re.compile(r"^\s*\d+\s+(?:observed|after-set|before-set|set)\s+\d+\s", re.M)
SESSION_VALUE = re.compile(r"^\s*\S+: [^=\n]{1,120} = \S", re.M)
JSON_VALUE = re.compile(r'"value":\s')


def peeky_values(output):
    return (
        len(PROBE_ROW.findall(output))
        + len(SESSION_VALUE.findall(output))
        + len(JSON_VALUE.findall(output))
    )


def uses_peeky(arm):
    """Whether the arm has Peeky (the study config's arms; else every arm but control)."""
    try:
        import studylib

        return studylib.arm_uses_peeky(arm)
    except (ImportError, KeyError):
        return arm != "control"


def shell_entries(starts, completes):
    """[{id, command, mode, exit_code, output}] for each bash call, in order; what read_bash
    (and write_bash, stop_bash) returned for a shell joins its command's output."""
    commands, by_shell = [], {}
    for call_id, start in starts.items():
        name, arguments = start.get("toolName"), start.get("arguments") or {}
        complete = completes.get(call_id) or {}
        content = ((complete.get("result") or {}).get("content")) or ""
        exit_code = (complete.get("shellExecution") or {}).get("exitCode")
        if name == "bash":
            entry = {
                "id": call_id,
                "command": arguments.get("command") or "",
                "mode": arguments.get("mode") or "sync",
                "exit_code": exit_code,
                "output": content,
            }
            match = SHELL_ID.search(content)
            if match:
                by_shell[match.group(1)] = entry
                if exit_code is None and match.group(2) is not None:
                    entry["exit_code"] = int(match.group(2))
            if arguments.get("shellId"):
                by_shell[str(arguments["shellId"])] = entry
            commands.append(entry)
        elif name in ("read_bash", "write_bash", "stop_bash"):
            entry = by_shell.get(str(arguments.get("shellId")))
            if entry is not None:
                entry["output"] += "\n" + content
                match = SHELL_ID.search(content)
                if entry["exit_code"] is None and match and match.group(2) is not None:
                    entry["exit_code"] = int(match.group(2))
    return commands


def shell_transcript(run_dir):
    """The attempt's shell commands with their outputs, from events.jsonl."""
    events, _ = read_jsonl(Path(run_dir) / "events.jsonl")
    starts, completes = OrderedDict(), {}
    for event in events:
        data = event.get("data") or {}
        if event.get("type") == "tool.execution_start":
            starts[data.get("toolCallId")] = data
        elif event.get("type") == "tool.execution_complete":
            completes[data.get("toolCallId")] = data
    return shell_entries(starts, completes)


def _read(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""
