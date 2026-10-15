"""`peeky`: a standalone debugger tool that agents, harnesses and humans call.

Peeky never calls a model. A model (Copilot, Claude, Codex), a script or a person
runs these commands and decides what to do next, as with heappy. There is no mode
switch: `probe` answers one question in one stateless call, `session` subcommands
explore step by step, `verify` checks a candidate fix on a fresh build, `describe`
prints a manifest of what can be asked. Targets are detected from the path:
`Cargo.toml` -> Rust, `.py` -> Python, `.c`/`.cpp` -> C/C++; `--config` overrides
detection with a trusted target configuration (required for Java).
"""

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

from peeky.client import Client
from peeky.probe import Probe, _capture, _table, parse_selector, run_probe

RUST_VERSION = "1.96.0"


def _state_dir():
    path = Path(os.environ.get("PEEKY_STATE_DIR", Path.home() / ".peeky")) / "sessions"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _which(*names):
    for name in names:
        if name and (found := shutil.which(name)):
            return str(Path(found).resolve())
    return None


def _xcrun(tool):
    if sys.platform != "darwin" or not shutil.which("xcrun"):
        return None
    result = subprocess.run(
        ["xcrun", "--find", tool], capture_output=True, text=True, timeout=20, check=False
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def _rustup(tool):
    if not shutil.which("rustup"):
        return None
    result = subprocess.run(
        ["rustup", "which", "--toolchain", RUST_VERSION, tool],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def _required(label, value):
    if not value:
        raise ValueError(f"Cannot find {label}; set it with --config or PEEKY_* variables")
    return str(Path(value).resolve(strict=True))


def rust_tools():
    """Discover pinned Rust and LLDB tools; PEEKY_RUST_* variables take precedence."""
    env = os.environ.get
    darwin = sys.platform == "darwin"
    tools = {
        "cargo": env("PEEKY_RUST_CARGO") or _rustup("cargo"),
        "rustc": env("PEEKY_RUST_RUSTC") or _rustup("rustc"),
        "adapter": env("PEEKY_RUST_DAP") or _xcrun("lldb-dap") or _which("lldb-dap", "lldb-dap-18"),
        "dwarfdump": env("PEEKY_RUST_DWARFDUMP")
        or ("/usr/bin/dwarfdump" if darwin else _which("llvm-dwarfdump", "llvm-dwarfdump-18")),
        "linker": env("PEEKY_RUST_LINKER")
        or ("/usr/bin/clang" if darwin else _which("clang", "clang-18")),
    }
    result = {name: _required(f"Rust {name}", value) for name, value in tools.items()}
    if darwin:
        result["dsymutil"] = env("PEEKY_RUST_DSYMUTIL") or "/usr/bin/dsymutil"
    if env("PEEKY_RUST_SYSROOT"):
        result["sysroot"] = env("PEEKY_RUST_SYSROOT")
    return result


def native_tools(language):
    env = os.environ.get
    compiler = env("PEEKY_NATIVE_CXX" if language == "cpp" else "PEEKY_NATIVE_CC")
    name = "clang++" if language == "cpp" else "clang"
    result = {
        "compiler": compiler or _xcrun(name) or _which(name, name + "-18"),
        "adapter": env("PEEKY_NATIVE_DAP")
        or _xcrun("lldb-dap")
        or _which("lldb-dap", "lldb-dap-18"),
        "dwarfdump": env("PEEKY_NATIVE_DWARFDUMP")
        or _xcrun("llvm-dwarfdump")
        or _which("llvm-dwarfdump", "llvm-dwarfdump-18", "dwarfdump"),
    }
    result = {key: _required(f"native {key}", value) for key, value in result.items()}
    if sys.platform == "darwin":
        result["dsymutil"] = env("PEEKY_NATIVE_DSYMUTIL") or _xcrun("dsymutil")
        sdk = subprocess.run(
            ["xcrun", "--show-sdk-path"], capture_output=True, text=True, timeout=20, check=False
        )
        result["sysroot"] = env("PEEKY_NATIVE_SYSROOT") or sdk.stdout.strip() or None
    return result


def _cargo_root(path):
    for candidate in (path, *path.parents):
        if (candidate / "Cargo.toml").is_file():
            return candidate
    return None


def resolve(target, at=None, config=None):
    """Return (root, program, line, language, trusted target config dict or None)."""
    path = Path(target).resolve(strict=True)
    program, line = None, None
    if at:
        location, _, number = str(at).rpartition(":")
        if not location and re.fullmatch(r"[1-9][0-9]{0,6}", str(at)):
            number = str(at)
        elif not re.fullmatch(r"[1-9][0-9]{0,6}", number):
            raise ValueError("--at must be <line> or <path>:<line>")
        program, line = location or None, int(number)
    if config is not None:
        data = json.loads(Path(config).read_text())
        if not isinstance(data, dict):
            raise ValueError("Target configuration must be a JSON object")
        language = data.pop("language", None) or (
            "rust" if "cargo" in data else "java" if "main_class" in data else "c"
        )
        if language in ("c", "cpp"):
            data["language"] = language
        root = path if path.is_dir() else path.parent
        return root, program, line, language, data
    if path.is_file() and path.suffix == ".py":
        return path.parent, program or path.name, line, "python", None
    if crate := _cargo_root(path if path.is_dir() else path.parent):
        manifest = tomllib.loads((crate / "Cargo.toml").read_text())
        binary = manifest.get("bin", [{}])[0].get("path", "src/main.rs")
        tests = tuple(item["path"] for item in manifest.get("test", []) if "path" in item)
        sources = tuple(
            sorted(p.relative_to(crate).as_posix() for p in (crate / "src").rglob("*.rs"))
        )[:64]
        if path.is_file():
            program = program or path.relative_to(crate).as_posix()
        data = {"sources": sources, "tests": tests, **rust_tools()}
        return crate, program or binary, line, "rust", data
    if path.is_file() and path.suffix in (".c", ".cpp", ".cc", ".cxx"):
        language = "c" if path.suffix == ".c" else "cpp"
        suffixes = (".c",) if language == "c" else (".cpp", ".cc", ".cxx")
        headers = (".h",) if language == "c" else (".h", ".hpp", ".hh", ".hxx")
        root = path.parent
        data = {
            "language": language,
            "sources": tuple(sorted(p.name for p in root.iterdir() if p.suffix in suffixes)),
            "headers": tuple(sorted(p.name for p in root.iterdir() if p.suffix in headers)),
            **native_tools(language),
        }
        return root, program or path.name, line, language, data
    if path.is_dir() and program and program.endswith(".py"):
        return path, program, line, "python", None
    raise ValueError(
        "Cannot detect the target; pass a .py/.c/.cpp file, a Cargo crate, or --config"
    )


def adapter_for(language, data):
    if data is None:
        return None
    data = {key: tuple(value) if isinstance(value, list) else value for key, value in data.items()}
    if language == "rust":
        from peeky.rust import RustTarget

        return RustTarget(**data)
    if language == "java":
        from peeky.java import JavaTarget

        return JavaTarget(**{**data, "bundle": Path(data["bundle"]).resolve()})
    from peeky.native import NativeTarget

    return NativeTarget(**data)


def _python_lines(path):
    code = compile(path.read_text(), str(path), "exec")
    lines, stack = set(), [code]
    while stack:
        item = stack.pop()
        lines.update(line for _, _, line in item.co_lines() if line)
        stack.extend(c for c in item.co_consts if hasattr(c, "co_lines"))
    return sorted(lines)


def describe(args):
    root, program, _, language, data = resolve(args.target, args.at, args.config)
    manifest = {
        "peeky": "describe",
        "language": language,
        "root": str(root),
        "program": program,
        "commands": {
            "probe": f"peeky probe {args.target} --at {program}:<line> --capture <a,b.c> "
            "[--set name=value] [--hits N] [--json]",
            "session": f"peeky session start {args.target} --at {program}:<line>; then "
            "peeky session inspect | get <a.b> | set <name>=<value> | continue | report | close",
            "verify": f"peeky verify {args.target} --check <checker.py> --old <text> --new <text>",
        },
        "selectors": "dotted paths and indexes, e.g. request.page, args[0]; never expressions",
        "interventions": "root locals only; evidence about one run, never a verified fix",
    }
    if language == "python":
        manifest["breakpoint_lines"] = {program: _python_lines(root / program)}
        manifest["mutable_types"] = "Python scalars accepted by debugpy setVariable"
    else:
        target = adapter_for(language, data)
        manifest["target_identity"] = target.target_identity
        runtime = target.prepare(root / program, root)
        try:
            manifest["breakpoint_lines"] = runtime.lines
            manifest["artifact_manifest_id"] = runtime.manifest_id
            if layouts := getattr(runtime, "layouts", None):
                manifest["value_views"] = {
                    name: layout["kind"] for name, layout in sorted(layouts.items())
                }
        finally:
            runtime.close()
        manifest["mutable_types"] = {
            "rust": "i32 root locals (debugger type int)",
            "c": "int/unsigned int root locals",
            "cpp": "int/unsigned int root locals",
            "java": "numeric/boolean root locals",
        }[language]
    _emit(manifest, args.json, _describe_text)
    return 0


def _describe_text(manifest):
    lines = [f"language: {manifest['language']}", f"root: {manifest['root']}"]
    for source, numbers in manifest["breakpoint_lines"].items():
        lines.append(f"breakpoint lines in {source}: {_ranges(numbers)}")
    if views := manifest.get("value_views"):
        qualified = [name for name, kind in views.items() if kind != "unqualified"]
        lines.append(f"qualified enum views: {', '.join(qualified) or 'none'}")
        lines.append(f"opaque enum types: {len(views) - len(qualified)}")
    lines.append(f"mutable: {manifest['mutable_types']}")
    lines.extend(f"{name}: {usage}" for name, usage in manifest["commands"].items())
    return "\n".join(lines)


def _ranges(numbers):
    spans, start = [], None
    for index, number in enumerate(numbers):
        if start is None:
            start = number
        if index + 1 == len(numbers) or numbers[index + 1] != number + 1:
            spans.append(str(start) if start == number else f"{start}-{number}")
            start = None
    return ", ".join(spans)


def _emit(value, as_json, text):
    print(json.dumps(value, indent=2, default=str) if as_json else text(value))


def _sets(items):
    changes = []
    for item in items:
        name, separator, value = item.partition("=")
        if not separator:
            raise ValueError("--set expects name=value")
        changes.append((name.strip(), value.strip()))
    return tuple(changes)


def probe(args):
    root, program, line, language, data = resolve(args.target, args.at, args.config)
    if line is None:
        raise ValueError("probe requires --at <path>:<line>")
    request = Probe(
        program,
        line,
        tuple(s.strip() for s in args.capture.split(",") if s.strip()),
        _sets(args.set),
        args.hits,
        args.timeout,
    )
    result = run_probe(root, request, adapter=adapter_for(language, data))
    _emit(result, args.json, _table)
    return 0 if result["status"] == "completed" else 1


class _Remote:
    """Adapts the HTTP client to the dispatch interface used by probe helpers."""

    def __init__(self, client):
        self.client = client

    def dispatch(self, operation, args):
        return self.client.call(operation, **args)


def _record(name):
    return _state_dir() / f"{name}.json"


def _load(name):
    path = _record(name)
    if not path.is_file():
        raise ValueError(f"No session named {name!r}; start one with `peeky session start`")
    record = json.loads(path.read_text())
    return record, Client(record["url"], record["token"])


def _save(record):
    path = _record(record["name"])
    path.write_text(json.dumps(record, indent=2))
    path.chmod(0o600)


def _wait(record, client, timeout):
    deadline = time.monotonic() + timeout
    state = {"state": "running"}
    while state["state"] == "running" and time.monotonic() < deadline:
        state = client.call(
            "wait", session_id=record["session_id"], timeout=min(2, deadline - time.monotonic())
        )
    record["state"], record["stop_id"] = state["state"], state.get("stop_id")
    _save(record)
    return state


def _stop(record):
    if record.get("state") != "stopped" or not record.get("stop_id"):
        raise ValueError("The program is not stopped; run `peeky session wait`")
    return {"session_id": record["session_id"], "stop_id": record["stop_id"]}


def _variables_text(snapshot):
    frame = snapshot["frames"][0] if snapshot["frames"] else {}
    lines = [f"stopped at {frame.get('source', {}).get('path')}:{frame.get('line')}"]
    for scope in snapshot["scopes"]:
        lines.append(f"[{scope['name']}]")
        for variable in scope["variables"]:
            more = " (expand with `get`)" if variable.get("variablesReference") else ""
            view = f"  [{variable['view']}]" if variable.get("view") else ""
            lines.append(
                f"  {variable['name']}: {variable.get('type', '')} = {variable['value']}{view}{more}"
            )
    if snapshot.get("mutation_blocked"):
        lines.append(f"note: {snapshot['mutation_blocked']}")
    return "\n".join(lines)


def session(args):
    if args.action == "start":
        return _start(args)
    if args.action == "list":
        names = sorted(
            path.stem for path in _state_dir().glob("*.json") if not path.stem.endswith(".target")
        )
        print("\n".join(names) if names else "no sessions")
        return 0
    record, client = _load(args.name)
    if args.action == "wait":
        state = _wait(record, client, args.timeout)
        _emit(state, args.json, lambda s: f"state: {s['state']}")
    elif args.action == "inspect":
        snapshot = client.call("inspect", **_stop(record))
        _emit(snapshot, args.json, _variables_text)
    elif args.action == "get":
        rows = _capture(_Remote(client), _stop(record), tuple(args.selectors), 0, "observed")
        _emit(
            rows,
            args.json,
            lambda rows: "\n".join(
                f"{r['selector']}: {r.get('type', '')} = {r.get('value', '<' + r.get('error', '') + '>')}"
                for r in rows
            ),
        )
    elif args.action == "set":
        (name, value), stop = _sets([args.assignment])[0], _stop(record)
        parse_selector(name)
        snapshot = client.call("inspect", **stop)
        scope = next(
            (s for s in snapshot["scopes"] if any(v["name"] == name for v in s["variables"])),
            None,
        )
        if scope is None:
            raise ValueError(f"{name!r} is not a visible local")
        changed = client.call(
            "set_variable", **stop, reference=scope["reference"], name=name, value=value
        )
        _emit(changed, args.json, lambda c: f"set {c['name']}: {c['before']} -> {c['after']}")
    elif args.action == "continue":
        client.call("continue", **_stop(record))
        state = _wait(record, client, args.timeout)
        _emit(state, args.json, lambda s: f"state: {s['state']}")
    elif args.action == "report":
        report = client.call("report", session_id=record["session_id"])
        _emit(
            report,
            args.json,
            lambda r: f"state: {r['state']}\noutput:\n{r['output']}operations: {len(r['history'])}",
        )
    elif args.action == "close":
        try:
            client.call("close", session_id=record["session_id"])
        finally:
            try:
                os.killpg(record["pid"], signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            for suffix in (".json", ".target.json", ".log"):
                (_state_dir() / f"{args.name}{suffix}").unlink(missing_ok=True)
        print(f"closed {args.name}")
    return 0


def _start(args):
    root, program, line, language, data = resolve(args.target, args.at, args.config)
    if line is None:
        raise ValueError("session start requires --at <path>:<line>")
    if _record(args.name).exists():
        raise ValueError(f"Session {args.name!r} exists; close it or pass --name")
    token = secrets.token_urlsafe(32)
    command = [sys.executable, "-m", "peeky.worker", "--root", str(root), "--port", "0"]
    if data is not None:
        config = _state_dir() / f"{args.name}.target.json"
        config.write_text(json.dumps(data, default=list))
        command += [f"--{'native' if language in ('c', 'cpp') else language}-config", str(config)]
    log = (_state_dir() / f"{args.name}.log").open("w")
    process = subprocess.Popen(
        command,
        env={**os.environ, "DEBUG_TOKEN": token},
        stdout=subprocess.PIPE,
        stderr=log,
        text=True,
        start_new_session=True,
    )
    banner = process.stdout.readline()
    match = re.search(r"(http://127\.0\.0\.1:[0-9]+)", banner)
    if not match:
        process.kill()
        raise RuntimeError(f"Worker did not start; see {log.name}")
    client = Client(match[1], token)
    record = {"name": args.name, "url": match[1], "token": token, "pid": process.pid}
    record.update(root=str(root), program=program, language=language)
    try:
        launch = client.call("launch", program=program, breakpoints=[{"line": line}])
    except Exception:
        os.killpg(process.pid, signal.SIGTERM)
        raise
    record.update(session_id=launch["session_id"], state="running", stop_id=None)
    _save(record)
    state = _wait(record, client, args.timeout)
    _emit(
        {"session": args.name, "language": language, "state": state["state"]},
        args.json,
        lambda s: f"session {s['session']} ({s['language']}): {s['state']}",
    )
    return 0


def verify(args):
    root, program, _, language, data = resolve(args.target, args.at, args.config)
    source, check = (root / program).resolve(strict=True), Path(args.check).resolve(strict=True)
    destination = Path(tempfile.mkdtemp(prefix="peeky-verify-"))
    if language == "python":
        from peeky.agent import verify as run_checker

        text = source.read_text()
        if text.count(args.old) != 1 or args.old == args.new:
            raise ValueError("--old must match exactly once and --new must change it")
        candidate = destination / program
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(text.replace(args.old, args.new, 1))
        result = {**run_checker(check, candidate, root), "candidate": str(candidate)}
    else:
        from peeky.compiled_verification import CompiledVerification

        class Verification(CompiledVerification):
            pass

        Verification.language = language
        result = Verification(adapter_for(language, data), root).candidate(
            source, check, args.old, args.new, destination
        )

    def text(result):
        lines = [f"passed: {result['passed']}  stage: {result.get('stage', 'verification')}"]
        if tests := result.get("tests"):
            lines.append(f"compiled tests: {tests['passed']} {tests.get('summary')}")
        lines.append(f"checker exit: {result.get('exit_code')}")
        lines.extend(
            ("output:", (result.get("stdout") or "")[-2000:], (result.get("stderr") or "")[-2000:])
        )
        lines.append(f"candidate: {result.get('candidate')}")
        return "\n".join(line for line in lines if line)

    _emit(result, args.json, text)
    return 0 if result["passed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="peeky", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def common(sub, target=True):
        if target:
            sub.add_argument("target", help="Program file, Cargo crate or project directory")
            sub.add_argument("--config", help="Trusted target JSON (required for Java)")
        sub.add_argument("--json", action="store_true", help="Machine-readable output")

    sub = commands.add_parser("describe", help="Manifest: language, breakpoint lines, views")
    common(sub)
    sub.add_argument("--at", help="Program path (for multi-file targets)")
    sub = commands.add_parser("probe", help="One stateless question: stop, capture, set, report")
    common(sub)
    sub.add_argument("--at", required=True, help="<line> or <path>:<line>")
    sub.add_argument("--capture", default="", help="Comma-separated selectors, e.g. a,b.c,d[0]")
    sub.add_argument("--set", action="append", default=[], help="name=value (repeatable)")
    sub.add_argument("--hits", type=int, default=1)
    sub.add_argument("--timeout", type=float, default=60)
    sub = commands.add_parser("session", help="Step-by-step debugging across commands")
    actions = sub.add_subparsers(dest="action", required=True)
    start = actions.add_parser("start", help="Build, launch and run to --at")
    common(start)
    start.add_argument("--at", required=True)
    for name in ("list", "wait", "inspect", "get", "set", "continue", "report", "close"):
        action = start if name == "start" else actions.add_parser(name)
        if name != "list":
            common(action, target=False)
    for action in actions.choices.values():
        action.add_argument("--name", default="default", help="Session name")
        action.add_argument("--timeout", type=float, default=30)
    actions.choices["get"].add_argument("selectors", nargs="+")
    actions.choices["set"].add_argument("assignment", help="name=value")
    sub = commands.add_parser("verify", help="Fresh build + independent checker for one edit")
    common(sub)
    sub.add_argument("--at", help="Program path (for multi-file targets)")
    sub.add_argument("--check", required=True, help="Trusted checker script")
    sub.add_argument("--old", required=True)
    sub.add_argument("--new", required=True)
    args = parser.parse_args(argv)
    handlers = {"describe": describe, "probe": probe, "session": session, "verify": verify}
    try:
        return handlers[args.command](args)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"peeky: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
