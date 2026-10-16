"""`peeky`: a standalone debugger tool that agents, harnesses and humans call.

Peeky never calls a model. A model (Copilot, Claude, Codex), a script or a person
runs these commands and decides what to do next, as with heappy. There is no mode
switch: `probe` answers one question in one stateless call and saves its evidence,
`sql` and `queries` ask follow-up questions about saved evidence (read-only SQL),
`session` subcommands explore step by step, `verify` checks a candidate fix on a fresh
build, `describe` prints a manifest of what can be asked. Targets are detected from the path:
`Cargo.toml` -> Rust, `.py` -> Python, `.c`/`.cpp` -> C/C++; `--config` overrides
detection with a trusted target configuration (required for Java).
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

from peeky.client import Client
from peeky.errors import PROTOCOL_VERSION
from peeky.probe import Probe, _capture, _table, parse_selector, run_probe

RUST_VERSION = "1.96.0"


MAX_SESSION_SECONDS = 300


def _private_dir(name):
    # Absolute (not symlink-resolved) so detached children with another cwd agree.
    base = os.environ.get("PEEKY_STATE_DIR") or str(Path.home() / ".peeky")
    path = Path(os.path.abspath(base)) / name
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.getuid():
        raise ValueError(f"State directory must be a real directory owned by you: {path}")
    if info.st_mode & 0o077:
        path.chmod(0o700)
    return path


def _state_dir():
    return _private_dir("sessions")


def _private_write(path, text):
    """Atomically create or replace a 0600 file (session credentials, configs)."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)
    os.replace(temporary, path)


def _confined(root, program):
    """A program path relative to and inside the target root."""
    if program is None:
        return None
    path = (root / program).resolve()
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        raise ValueError("--at and --run must name a file inside the target") from None


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
        text = str(at)
        if re.fullmatch(r"[1-9][0-9]{0,6}", text):
            line = int(text)
        elif ":" in text:
            location, _, number = text.rpartition(":")
            if not location or not re.fullmatch(r"[1-9][0-9]{0,6}", number):
                raise ValueError("--at must be <line>, <path> or <path>:<line>")
            program, line = location, int(number)
        else:
            program = text
    root, program, line, language, data = _resolve(path, program, line, config)
    return root, _confined(root, program), line, language, data


def _resolve(path, program, line, config):
    if config is not None:
        data = json.loads(Path(config).read_text())
        if not isinstance(data, dict):
            raise ValueError("Target configuration must be a JSON object")
        language = data.pop("language", None) or (
            "rust"
            if "cargo" in data
            else "java-attach"
            if "port" in data
            else "java"
            if "main_class" in data
            else "c"
        )
        if language == "java-attach":
            data["bundle"] = str((Path(config).resolve().parent / data["bundle"]).resolve())
        if language in ("c", "cpp"):
            data["language"] = language
        root = path if path.is_dir() else path.parent
        return root, program, line, language, data
    if path.is_file() and path.suffix == ".py":
        return path.parent, program or path.name, line, "python", None
    if path.is_dir() and program and program.endswith(".py"):
        return path, program, line, "python", None
    rust_intent = (
        (path.is_dir() and (path / "Cargo.toml").is_file())
        or path.name == "Cargo.toml"
        or path.suffix == ".rs"
        or (path.is_dir() and program and program.endswith(".rs"))
    )
    if rust_intent and (crate := _cargo_root(path if path.is_dir() else path.parent)):
        manifest = tomllib.loads((crate / "Cargo.toml").read_text())
        binary = manifest.get("bin", [{}])[0].get("path", "src/main.rs")
        tests = tuple(item["path"] for item in manifest.get("test", []) if "path" in item)
        sources = tuple(
            sorted(p.relative_to(crate).as_posix() for p in (crate / "src").rglob("*.rs"))
        )
        if len(sources) > 64:
            raise ValueError("Rust targets support at most 64 source files; use --config")
        if path.is_file() and path.suffix == ".rs":
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
    if path.is_dir() and (scripts := sorted(p.name for p in path.glob("*.py"))[:20]):
        raise ValueError(
            "Python directory target: pass --at <module.py>[:<line>] (and --run <script.py> "
            f"when the module is imported). Python files here: {', '.join(scripts)}"
        )
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
    if language == "java-attach":
        from peeky.java_attach import JavaAttachTarget

        return JavaAttachTarget(**{**data, "bundle": Path(data["bundle"])})
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


MAX_SCOPES = 300


def _python_scopes(path):
    """Names each Python function (and the module) can see, for choosing selectors.

    Static: lists parameters and assigned locals, not values or attributes. Methods are
    qualified by class (`IPv6Address.is_private`); listed in source order, first
    MAX_SCOPES only.
    """
    import ast
    import symtable

    text = path.read_text()
    tree = ast.parse(text, str(path))
    table = symtable.symtable(text, str(path), "exec")
    spans = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            spans.setdefault((node.lineno, getattr(node, "name", "lambda")), node.end_lineno)
    found, stack = [], [(table, "")]
    while stack and len(found) < 20000:
        item, prefix = stack.pop()
        kind = item.get_type()
        name = item.get_name()
        qualified = f"{prefix}{name}"
        # Only functions and classes name a level; PEP 695 type-parameter scopes and the
        # module pass their prefix through unchanged.
        child_prefix = f"{qualified}." if kind in ("function", "class") else prefix
        stack.extend((child, child_prefix) for child in item.get_children())
        end = spans.get((item.get_lineno(), name))
        if kind == "module":
            found.append((0, "<module>", None, item))
        elif kind == "function" and end is not None:
            # Generator expressions and annotation scopes have no source span: skipped.
            found.append((item.get_lineno(), qualified, [item.get_lineno(), end], item))
    found.sort(key=lambda entry: entry[0])
    scopes = []
    for _, qualified, lines, item in found[:MAX_SCOPES]:
        names = sorted(
            s.get_name()
            for s in item.get_symbols()
            if s.is_parameter()
            or s.is_assigned()
            or (item.get_type() == "module" and s.is_imported())
        )
        scopes.append({"scope": qualified, "lines": lines, "names": names[:100]})
    if len(found) > MAX_SCOPES:
        scopes.append(
            {"scope": "...", "lines": None, "names": [], "omitted": len(found) - MAX_SCOPES}
        )
    return scopes


def _entry(root, run, language):
    if run is None:
        return None
    if language != "python":
        raise ValueError("--run is supported only for Python targets")
    return _confined(root, run)


def _default_program(program, data, command):
    if program is not None:
        return program
    sources = (data or {}).get("sources") or ()
    if command == "describe" and sources:
        # Breakpoint lines cover every declared source; any one prepares the target.
        return sources[0]
    listed = f" (one of: {', '.join(sources[:10])})" if sources else ""
    raise ValueError(f"{command} needs --at <source>{listed} for this target")


def describe(args):
    root, program, _, language, data = resolve(args.target, args.at, args.config)
    program = _confined(root, _default_program(program, data, "describe"))
    run = " [--run <script.py>] [--arg A ...]" if language == "python" else ""
    manifest = {
        "peeky": "describe",
        "language": language,
        "root": str(root),
        "program": program,
        "commands": {
            "probe": f"peeky probe {args.target} --at {program}:<line>{run} --capture <a,b.c> "
            "[--set name=value] [--set-hit N] [--hits N] [--json]",
            "session": f"peeky session start {args.target} --at {program}:<line>{run}; then "
            "peeky session inspect | get <a.b> | set <name>=<value> | continue [--to-exit] | "
            "report | close",
            "verify": f"peeky verify {args.target} --at {program} --check <checker.py> "
            "--old <text> --new <text> (or --old-file/--new-file)",
            "background": "add --background to probe or verify, then `peeky job wait <id>` "
            "(exit 3 while running)",
            "evidence": 'every probe is saved with a run_id: peeky sql "SELECT hit, selector, '
            'value FROM last_run"; peeky queries [name] (canned, no SQL); peeky sql --schema',
        },
        "selectors": "dotted paths and indexes, e.g. request.page, args[0]; never expressions",
        "interventions": "root locals only; evidence about one run, never a verified fix",
    }
    if language == "python":
        manifest["breakpoint_lines"] = {program: _python_lines(root / program)}
        manifest["scopes"] = _python_scopes(root / program)
        manifest["mutable_types"] = (
            "Python literals: numbers, strings, bytes, True/False/None and lists/tuples/dicts/sets of them"
        )
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
            "java-attach": "numeric/boolean root locals (attached JVM; no edit verification)",
        }[language]
        from peeky.probe import optimization_level

        if level := optimization_level(manifest.get("target_identity")):
            manifest["optimized"] = level
            manifest["mutable_types"] = f"none: changes are refused in optimized builds ({level})"
            # Enum views are opaque in optimized builds; none is qualified.
            manifest.pop("value_views", None)
    budget = _max_pause(None, language)
    manifest["timing"] = {
        "probe_pause": "milliseconds per hit; safe for programs with timeouts",
        "session_max_pause_s": budget,
        "advice": (
            "This target keeps real clocks running (test timeouts, heartbeats): prefer "
            f"probes; session stops auto-resume after {budget} s (--max-pause)."
            if budget
            else "Session stops wait for you; pass --max-pause S if the program has "
            "timeouts. Use --background for anything slower than your tool timeout."
        ),
    }
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
    for scope in manifest.get("scopes", ()):
        if scope.get("omitted"):
            lines.append(f"... {scope['omitted']} more scopes not listed (first 300 shown)")
            continue
        where = f" (lines {scope['lines'][0]}-{scope['lines'][1]})" if scope["lines"] else ""
        lines.append(f"names in {scope['scope']}{where}: {', '.join(scope['names']) or '-'}")
    from peeky.probe import optimization_level

    if level := optimization_level(manifest.get("target_identity")):
        lines.append(
            f"warning: optimized build ({level}); locals may be unavailable, stale or wrong. "
            "Prefer an unoptimized build for debugging."
        )
    lines.append(f"mutable: {manifest['mutable_types']}")
    lines.extend(f"{name}: {usage}" for name, usage in manifest["commands"].items())
    lines.append(f"timing: {manifest['timing']['advice']}")
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
    if args.label is not None and len(args.label) > 200:
        raise ValueError("--label is at most 200 characters")
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
        # An attached JVM keeps running after the probe, so waiting for its exit only
        # delays the answer; launched programs run to exit for their output.
        args.until if args.until != "auto" else ("hits" if language == "java-attach" else "exit"),
        args.set_hit,
        entry=_entry(root, args.run, language),
        args=tuple(args.arg),
        expect_sha256=tuple(tuple(item.split("=", 1)) for item in args.expect_sha256),
    )
    progress = (
        None
        if args.quiet
        else lambda message: print(f"peeky: {message}", file=sys.stderr, flush=True)
    )
    if args.worker is not None:
        result = _probe_remote(args, root, request)
    else:
        result = run_probe(root, request, adapter=adapter_for(language, data), progress=progress)
    if not args.no_save:
        _keep_evidence(args, result, root, language)
    _emit(result, args.json, _probe_text)
    return 0 if result["status"] == "completed" else 1


def _evidence_store(args):
    if getattr(args, "store", None):
        return Path(os.path.abspath(os.path.expanduser(args.store)))
    return _private_dir("evidence") / "evidence.db"


def _keep_evidence(args, result, root, language):
    """Record a probe result for later SQL; a failure to save never changes the result."""
    from peeky import evidence

    try:
        path = _evidence_store(args)
        run_id = evidence.record(
            path, result, language=language, target=root, label=args.label, worker=args.worker
        )
    except Exception as exc:
        result.setdefault("warnings", []).append(f"Evidence was not saved: {exc}")
        return
    result["evidence"] = {
        "run_id": run_id,
        "store": str(path),
        "next": f"peeky queries run --param run={run_id}",
    }


def _probe_text(result):
    text = _table(result)
    if saved := result.get("evidence"):
        text += (
            f"\nevidence: saved as run {saved['run_id']}; query it with `{saved['next']}` or "
            "`peeky sql` (see `peeky sql --schema`)"
        )
    return text


def sql(args):
    from peeky import evidence

    path = _evidence_store(args)
    if args.schema:
        if args.query:
            raise ValueError("Pass either SQL or --schema, not both")
        _emit(evidence.schema(path), args.json, evidence.schema_text)
        return 0
    if not args.query:
        raise ValueError(
            'peeky sql needs one SELECT, e.g. peeky sql "SELECT * FROM last_run"; '
            "`peeky sql --schema` lists the tables"
        )
    result = evidence.query(path, args.query, evidence.parse_params(args.param), limit=args.limit)
    _emit({**result, "store": str(path)}, args.json, evidence.table)
    return 0


def queries(args):
    from peeky import evidence

    path = _evidence_store(args)
    if args.name is None:
        listed = evidence.schema(path)["queries"]
        _emit(
            listed,
            args.json,
            lambda listed: "\n".join(
                f"{name}: {info['description']}"
                + "".join(f"\n  --param {p}=...  {d}" for p, d in info["params"].items())
                for name, info in listed.items()
            ),
        )
        return 0
    result = evidence.run_query(
        path, args.name, evidence.parse_params(args.param), limit=args.limit
    )
    _emit({**result, "query": args.name, "store": str(path)}, args.json, evidence.table)
    return 0


class _Remote:
    """Adapts the HTTP client to the dispatch interface used by probe helpers."""

    def __init__(self, client):
        self.client = client

    def dispatch(self, operation, args):
        return self.client.call(operation, **args)


def _record(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise ValueError("Session names use 1-32 letters, digits, '-' or '_'")
    return _state_dir() / f"{name}.json"


def _load(name):
    path = _record(name)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"No session named {name!r}; start one with `peeky session start`")
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError(f"Session record {path} must be private (0600) and owned by you")
    record = json.loads(path.read_text())
    if (
        not isinstance(record, dict)
        or record.get("name") != name
        or not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", str(record.get("url")))
        or not isinstance(record.get("token"), str)
        or len(record["token"]) < 16
        or not re.fullmatch(r"[0-9a-f]{32}", str(record.get("session_id")))
    ):
        raise ValueError(f"Session record {path} is malformed; remove it")
    if record.get("remote"):
        return record, Client(record["url"], record["token"])
    return record, _LocalClient(record["url"], record["token"], record.get("pid"))


class _LocalClient(Client):
    """A session's own local worker: if its process is gone, the session is lost."""

    def __init__(self, url, token, pid):
        super().__init__(url, token)
        self.pid = pid

    def call(self, operation, **arguments):
        from peeky.client import TransportLost, WorkerUnreachable

        try:
            return super().call(operation, **arguments)
        except (WorkerUnreachable, TransportLost):
            if type(self.pid) is int and not _pid_alive(self.pid):
                raise RuntimeError(
                    f"The local worker (pid {self.pid}) has exited; the session is lost. "
                    "Start a new session"
                ) from None
            raise


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        # A dead worker that is still our own child is a zombie until reaped.
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return reaped == 0


def _save(record):
    _private_write(_record(record["name"]), json.dumps(record, indent=2))


def _seconds(value):
    if not isinstance(value, (int, float)) or not 0 < value <= MAX_SESSION_SECONDS:
        raise ValueError(f"--timeout must be within (0, {MAX_SESSION_SECONDS}] seconds")
    return value


def _wait(record, client, timeout):
    deadline = time.monotonic() + _seconds(timeout)
    state = {"state": "running"}
    while state["state"] == "running" and time.monotonic() < deadline:
        state = client.call(
            "wait", session_id=record["session_id"], timeout=min(2, deadline - time.monotonic())
        )
    record["state"], record["stop_id"] = state["state"], state.get("stop_id")
    _save(record)
    state.pop("event", None)
    return state


_OPTIMIZED_NOTE = (
    "warning: optimized build ({}): shown values can be stale or wrong and locals cannot be "
    "changed; prefer an unoptimized build"
)


def _state_text(state):
    where = f" at {state['location']}" if state.get("location") else ""
    hint = ""
    if state["state"] == "running":
        hint = " (still running; run `peeky session wait`)"
    elif state["state"] == "terminated":
        hint = " (program exited; `peeky session report` shows output)"
    reason = state.get("reason")
    if reason and reason != "breakpoint":
        where += f" [reason: {reason}; not auto-resumed, inspect it or `session report`]"
    text = f"state: {state['state']}{where}{_pause_text(state)}{hint}"
    if state.get("optimized"):
        text += f"\n{_OPTIMIZED_NOTE.format(state['optimized'])}"
    if state.get("exit_inferred"):
        text += "\nnote: the debugger never reported the exit; Peeky inferred it (no threads left)"
    if state.get("auto_resumed"):
        last = state.get("last_auto_resumed") or {}
        text += (
            f"\nnote: {state['auto_resumed']} breakpoint stop(s) were auto-resumed after the "
            f"pause budget (latest at {last.get('location')} after {last.get('paused_s')} s)"
        )
    return text


def _stop(record):
    if record.get("state") != "stopped" or not record.get("stop_id"):
        raise ValueError("The program is not stopped; run `peeky session wait`")
    return {"session_id": record["session_id"], "stop_id": record["stop_id"]}


def _variables_text(snapshot):
    frame = snapshot["frames"][0] if snapshot["frames"] else {}
    lines = [
        f"stopped at {frame.get('source', {}).get('path')}:{frame.get('line')}"
        + _pause_text(snapshot)
    ]
    for scope in snapshot["scopes"]:
        lines.append(f"[{scope['name']}]")
        for variable in scope["variables"]:
            more = " (expand with `get`)" if variable.get("variablesReference") else ""
            if variable.get("unavailable"):
                more = f" (no readable value: {variable.get('reason')})"
            view = f"  [{variable['view']}]" if variable.get("view") else ""
            lines.append(
                f"  {variable['name']}: {variable.get('type', '')} = "
                f"{'<no value>' if variable.get('unavailable') else variable['value']}{view}{more}"
            )
    if snapshot.get("optimized"):
        lines.append(_OPTIMIZED_NOTE.format(snapshot["optimized"]))
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
        _emit(state, args.json, _state_text)
    elif args.action == "inspect":
        snapshot = client.call("inspect", **_stop(record))
        _emit(snapshot, args.json, _variables_text)
    elif args.action == "get":
        rows = _capture(_Remote(client), _stop(record), tuple(args.selectors), 0, "observed")
        if level := record.get("optimized"):
            rows = [{**row, "optimized": level} for row in rows]
        _emit(
            rows,
            args.json,
            lambda rows: "\n".join(
                [
                    f"{r['selector']}: {r.get('type', '')} = "
                    + str(r.get("value", "<" + r.get("error", "") + ">"))
                    for r in rows
                ]
                + ([_OPTIMIZED_NOTE.format(record["optimized"])] if record.get("optimized") else [])
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
        stop = _stop(record)
        # --to-exit clears the breakpoints in the same worker operation, after it
        # confirms this stop is still current (it may have been auto-resumed).
        client.call("continue", **stop, **({"to_exit": True} if args.to_exit else {}))
        state = _wait(record, client, args.timeout)
        _emit(state, args.json, _state_text)
    elif args.action == "report":
        report = client.call("report", session_id=record["session_id"])
        _emit(
            report,
            args.json,
            lambda r: f"state: {r['state']}\noutput:\n{r['output']}operations: {len(r['history'])}",
        )
    elif args.action == "close" and args.forget:
        # The worker is gone for good: drop the local record without contacting it.
        for suffix in (".json", ".target.json"):
            (_state_dir() / f"{args.name}{suffix}").unlink(missing_ok=True)
        print(f"forgot {args.name} (the worker was not contacted)")
    elif args.action == "close":
        from peeky.client import TransportLost, WorkerUnreachable

        keep = False
        try:
            client.call("close", session_id=record["session_id"])
        except (WorkerUnreachable, TransportLost):
            # The remote worker may still hold the session: keep the record so the close
            # can be retried once the tunnel is back.
            keep = bool(record.get("remote"))
            raise
        finally:
            if keep:
                pass
            elif not record.get("remote"):
                try:
                    # Authenticated by this record's token, so only its own worker stops.
                    # A remote worker is shared infrastructure: it keeps running.
                    client.call("shutdown")
                except Exception:
                    pass
            if not keep:
                for suffix in (".json", ".target.json"):
                    (_state_dir() / f"{args.name}{suffix}").unlink(missing_ok=True)
                log = _state_dir() / f"{args.name}.log"
                if log.is_file() and not log.is_symlink():
                    # Kept for post-mortem diagnosis until the next session of this name.
                    os.replace(log, _state_dir() / f"{args.name}.last.log")
        print(f"closed {args.name}")
    return 0


ATTACH_MAX_PAUSE = 10


def _max_pause(value, language):
    # Attached JVMs (e.g. Gradle tests with TestNG/JUnit timeouts) keep real clocks
    # running while paused, so they get a pause budget unless explicitly disabled.
    if value is None:
        return ATTACH_MAX_PAUSE if language == "java-attach" else None
    if value == 0:
        return None
    if not 0 < value <= 300:
        raise ValueError("--max-pause must be within (0, 300] seconds, or 0 for none")
    return value


def _pause_text(state):
    if "paused_s" not in state:
        return ""
    text = f"paused {state['paused_s']} s"
    if "resume_in_s" in state:
        text += f", auto-resume in {state['resume_in_s']} s"
    return f" ({text})"


WORKER_TOKEN_ENV = "PEEKY_WORKER_TOKEN"


def _remote(url):
    """A client for a worker reached over loopback (e.g. an SSH -L tunnel)."""
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", url or ""):
        raise ValueError(
            "--worker must be a loopback URL such as http://127.0.0.1:8765; reach remote "
            "workers through an SSH tunnel (ssh -N -L 8765:127.0.0.1:8765 user@host)"
        )
    token = os.environ.get(WORKER_TOKEN_ENV, "")
    if len(token) < 16:
        raise ValueError(
            f"Set {WORKER_TOKEN_ENV} to the worker's DEBUG_TOKEN; tokens are never accepted "
            "as arguments"
        )
    client = Client(url, token)
    version = client.call("version")
    if version.get("protocol") != PROTOCOL_VERSION:
        raise ValueError(
            f"Worker speaks protocol {version.get('protocol')}, this client {PROTOCOL_VERSION}"
        )
    return client, token


def _source_matches(root, relative, remote_sha256, what):
    local = hashlib.sha256((root / relative).read_bytes()).hexdigest()
    if local != remote_sha256:
        raise RuntimeError(
            f"Worker source provenance differs for {what} {relative}: local sha256 {local[:12]}, "
            f"worker {str(remote_sha256)[:12]}. Sync sources before debugging remotely"
        )


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expected(root, program, entry):
    expected = {"program": _sha256(root / program)}
    if entry:
        expected["entry"] = _sha256(root / entry)
    return expected


def _probe_remote(args, root, request):
    client, _ = _remote(args.worker)
    expected = _expected(root, request.program, request.entry)
    result = client.probe(
        f"{request.program}:{request.line}",
        request.capture,
        dict(request.set),
        hits=request.hits,
        timeout=min(request.timeout, 30),
        until=request.until,
        set_hit=request.set_hit,
        entry=request.entry,
        args=request.args,
        # The worker refuses before launching when its sources differ.
        expect_sha256=expected,
    )
    result["worker"] = args.worker
    # Evidence is recorded here, on the client; anything the worker reports about its own
    # store would point at runs that do not exist locally.
    result.pop("evidence", None)
    result["warnings"] = [
        warning
        for warning in result.get("warnings") or []
        if not warning.startswith("Evidence was not saved")
    ]
    ran = bool(result.get("hits") or result.get("rows") or result.get("output"))
    try:
        # Defense in depth: also compare what the worker reports having run.
        if ran or result.get("source_sha256") is not None:
            _source_matches(root, request.program, result.get("source_sha256"), "program")
        if request.entry and (ran or result.get("entry_sha256") is not None):
            _source_matches(root, request.entry, result.get("entry_sha256"), "entry script")
    except RuntimeError as exc:
        from peeky.errors import classify

        # Values and output from different source text cannot be attributed locally.
        result.update(
            status="failed",
            error=classify(exc, session_closed=True),
            rows=[],
            by_hit=[],
            interventions=[],
            output="",
            results_discarded=True,
        )
    return result


def _start_remote(args, root, program, line, language):
    client, token = _remote(args.worker)
    launch = client.call(
        "launch",
        program=program,
        breakpoints=[{"line": line}],
        max_pause=_max_pause(args.max_pause, language),
        **({"entry": entry} if (entry := _entry(root, args.run, language)) else {}),
        **({"args": list(args.arg)} if args.arg else {}),
        expect_sha256=_expected(root, program, _entry(root, args.run, language)),
    )
    try:
        _source_matches(root, program, launch.get("source_sha256"), "program")
        if entry:
            _source_matches(root, entry, launch.get("entry_sha256"), "entry script")
    except Exception:
        client.call("close", session_id=launch["session_id"])
        raise
    record = {"name": args.name, "url": args.worker, "token": token, "remote": True}
    record.update(root=str(root), program=program, language=language)
    record.update(session_id=launch["session_id"], state="running", stop_id=None)
    if launch.get("optimized"):
        record["optimized"] = launch["optimized"]
    _save(record)
    state = _wait(record, client, args.timeout)
    if record.get("optimized"):
        state["optimized"] = record["optimized"]
    _emit(
        {"session": args.name, "language": language, "worker": args.worker, **state},
        args.json,
        lambda s: f"session {s['session']} ({s['language']}, remote): " + _state_text(s),
    )
    return 0


def _start(args):
    root, program, line, language, data = resolve(args.target, args.at, args.config)
    if line is None:
        raise ValueError("session start requires --at <path>:<line>")
    if _record(args.name).exists():
        raise ValueError(f"Session {args.name!r} exists; close it or pass --name")
    if args.worker is not None:
        return _start_remote(args, root, program, line, language)
    token = secrets.token_urlsafe(32)
    command = [sys.executable, "-m", "peeky.worker", "--root", str(root), "--port", "0"]
    if data is not None:
        config = _state_dir() / f"{args.name}.target.json"
        _private_write(config, json.dumps(data, default=list))
        command += [f"--{'native' if language in ('c', 'cpp') else language}-config", str(config)]
    log_path = _state_dir() / f"{args.name}.log"
    log = os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w")
    process = subprocess.Popen(
        command,
        env={**os.environ, "DEBUG_TOKEN": token},
        stdout=subprocess.PIPE,
        stderr=log,
        text=True,
        start_new_session=True,
    )
    ready, _, _ = select.select([process.stdout], [], [], 30)
    banner = process.stdout.readline() if ready else ""
    match = re.search(r"(http://127\.0\.0\.1:[0-9]+)", banner)
    if not match:
        process.kill()
        raise RuntimeError(f"Worker did not start; see {log.name}")
    client = _LocalClient(match[1], token, process.pid)
    record = {"name": args.name, "url": match[1], "token": token, "pid": process.pid}
    record.update(root=str(root), program=program, language=language)
    try:
        launch = client.call(
            "launch",
            program=program,
            breakpoints=[{"line": line}],
            max_pause=_max_pause(args.max_pause, language),
            **({"entry": entry} if (entry := _entry(root, args.run, language)) else {}),
            **({"args": list(args.arg)} if args.arg else {}),
        )
    except Exception as exc:
        os.killpg(process.pid, signal.SIGTERM)
        from peeky.client import TransportLost, WorkerUnreachable

        if isinstance(exc, (WorkerUnreachable, TransportLost)):
            # Worded so it is classified as a closed session, not a retryable transport error.
            raise RuntimeError(
                "The local worker failed while launching and was stopped; no session exists. "
                "Start again"
            ) from exc
        raise
    record.update(session_id=launch["session_id"], state="running", stop_id=None)
    if launch.get("optimized"):
        record["optimized"] = launch["optimized"]
    _save(record)
    state = _wait(record, client, args.timeout)
    if record.get("optimized"):
        state["optimized"] = record["optimized"]
    _emit(
        {"session": args.name, "language": language, **state},
        args.json,
        lambda s: f"session {s['session']} ({s['language']}): " + _state_text(s),
    )
    return 0


def _submitted_edits(args):
    if args.command != "verify" or (args.old_file is None and args.new_file is None):
        return None
    return {
        "old": _edit_text(args.old, args.old_file, "old"),
        "new": _edit_text(args.new, args.new_file, "new"),
    }


def _edit_text(text, path, name):
    """Exact edit text from --<name> or --<name>-file (read verbatim, newlines kept)."""
    if (text is None) == (path is None):
        raise ValueError(f"Pass exactly one of --{name} or --{name}-file")
    if path is None:
        return text
    data = Path(path).read_bytes()
    if len(data) > 1024 * 1024:
        raise ValueError(f"--{name}-file exceeds 1 MiB")
    return data.decode()


def _check_edit(text, old, new):
    if not old or old == new:
        raise ValueError("--old must match exactly once and --new must change it")
    count = text.count(old)
    if count == 1:
        return
    if count:
        lines, start = [], 0
        while (index := text.find(old, start)) >= 0 and len(lines) < 10:
            lines.append(str(text.count("\n", 0, index) + 1))
            start = index + len(old)
        raise ValueError(
            f"--old must match exactly once and --new must change it; it matched {count} "
            f"times (lines {', '.join(lines)}); include more surrounding text"
        )
    advice = "copy the exact text, including indentation"
    if "\\n" in old and "\n" not in old:
        advice = "it contains a literal backslash-n; pass real newlines (bash: $'a\\nb')"
    raise ValueError(
        f"--old must match exactly once and --new must change it; it matched 0 times: {advice}"
    )


def _diff(before, after, name):
    import difflib

    lines = difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True), name, name
    )
    text = "".join(lines)
    return text if len(text) <= 8000 else text[:8000] + "\n... (diff truncated)\n"


def _verify_candidate(args, root, program, language, data, source, check, destination):
    if language == "python":
        from peeky.agent import digest
        from peeky.agent import verify as run_checker

        original = digest(source)
        text = source.read_text()
        if text.count(args.old) != 1 or args.old == args.new:
            raise ValueError("--old must match exactly once and --new must change it")
        candidate = destination / "candidate" / program
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(text.replace(args.old, args.new, 1))
        candidate_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
        result = {**run_checker(check, candidate, root), "candidate": str(candidate)}
        result["candidate_sha256"] = candidate_sha256
        if digest(source) != original:
            raise RuntimeError("Original source changed during verification")
        return result
    from peeky.compiled_verification import CompiledVerification

    class Verification(CompiledVerification):
        pass

    Verification.language = language
    return Verification(adapter_for(language, data), root).candidate(
        source, check, args.old, args.new, destination
    )


def verify(args):
    root, program, _, language, data = resolve(args.target, args.at, args.config)
    if language == "java-attach":
        raise ValueError("Attached JVMs cannot verify edits; verify with a fresh build target")
    program = _default_program(program, data, "verify")
    source, check = (root / program).resolve(strict=True), Path(args.check).resolve(strict=True)
    original = source.read_text()
    if edits := getattr(args, "_edits", None):
        args.old, args.new = edits["old"], edits["new"]
    else:
        args.old, args.new = (
            _edit_text(args.old, args.old_file, "old"),
            _edit_text(args.new, args.new_file, "new"),
        )
    _check_edit(original, args.old, args.new)
    destination = Path(tempfile.mkdtemp(prefix="peeky-verify-"))
    try:
        result = _verify_candidate(args, root, program, language, data, source, check, destination)
        # The reported diff must describe exactly the candidate that was verified.
        expected = original.replace(args.old, args.new, 1).encode()
        if hashlib.sha256(expected).hexdigest() != result.get("candidate_sha256"):
            raise RuntimeError(
                "Original source changed during verification; the verified candidate does "
                "not match the reported edit"
            )
    finally:
        if not args.keep:
            shutil.rmtree(destination, ignore_errors=True)
    result.setdefault("stage", "verification")
    result["diff"] = _diff(original, original.replace(args.old, args.new, 1), program)
    if args.keep:
        result["kept"] = str(destination)
    else:
        for key in ("candidate", "evidence_file"):
            result.pop(key, None)

    def text(result):
        lines = [f"passed: {result['passed']}  stage: {result['stage']}"]
        if tests := result.get("tests"):
            lines.append(f"compiled tests: {tests['passed']} {tests.get('summary')}")
        if not result["passed"]:
            reason = result.get("diagnostic") or (result.get("checker") or {}).get("error")
            if result.get("timed_out"):
                reason = "checker timed out"
            if reason:
                lines.append(f"reason: {reason}")
        lines.append(f"checker exit: {result.get('exit_code')}")
        output = [(result.get(key) or "")[-2000:] for key in ("stdout", "stderr")]
        if any(output):
            lines.extend(("output:", *output))
        if args.keep:
            lines.append(f"candidate: {result.get('candidate')}")
        lines.append("diff:\n" + result["diff"])
        return "\n".join(line for line in lines if line)

    _emit(result, args.json, text)
    return 0 if result["passed"] else 1


JOB_RETENTION_S = 24 * 3600
MAX_JOB_WAIT_S = 300


def _job_dir(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{16}", job_id):
        raise ValueError(f"No job named {job_id!r}; job ids are 16 hex digits")
    path = _private_dir("jobs") / job_id
    if not path.is_dir() or path.is_symlink():
        raise ValueError(f"No job named {job_id!r}")
    return path


def _background(argv, command, edits=None):
    """Run one probe or verify detached, so a caller's tool timeout cannot kill it."""
    import peeky

    _prune_jobs()
    job_id = secrets.token_hex(8)
    path = _private_dir("jobs") / job_id
    path.mkdir(mode=0o700)
    spec = {"id": job_id, "command": command, "argv": argv, "cwd": os.getcwd()}
    if edits:
        # Edit texts are fixed at submission; later changes to --old-file/--new-file
        # cannot change what this job verifies.
        spec["edits"] = edits
    _private_write(path / "job.json", json.dumps({**spec, "started": time.time()}))
    bootstrap = (
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        "from peeky.cli import run_job; sys.exit(run_job(sys.argv[1]))"
    )
    boot = os.open(path / "boot.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        subprocess.Popen(
            [sys.executable, "-I", "-c", bootstrap]
            + [str(Path(peeky.__file__).resolve().parents[1]), str(path)],
            cwd=path,
            stdin=subprocess.DEVNULL,
            stdout=boot,
            stderr=boot,
            start_new_session=True,
        )
    finally:
        os.close(boot)
    return {"job": job_id, "status": "running", "next": f"peeky job wait {job_id}"}


def run_job(path):
    """Job child: run the stored command, capture its output, then record completion."""
    import fcntl
    import traceback

    def private(name):
        return os.fdopen(os.open(path / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w")

    path = Path(path)
    spec = json.loads((path / "job.json").read_text())
    lock = private("lock")
    fcntl.flock(lock, fcntl.LOCK_EX)
    # Written only while the lock is held: a free lock plus this marker means the job died.
    private("started").close()
    os.chdir(spec["cwd"])
    code = 2
    with private("out") as out, private("log") as log:
        sys.stdout, sys.stderr = out, log
        try:
            code = main(spec["argv"], _job=True, _edits=spec.get("edits"))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        except BaseException:
            traceback.print_exc()
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    _private_write(path / "status.json", json.dumps({"exit_code": code, "finished": time.time()}))
    return 0


def _job_state(path):
    import fcntl

    spec = json.loads((path / "job.json").read_text())
    state = {"job": spec["id"], "command": spec["command"]}
    if (path / "status.json").is_file():
        status = json.loads((path / "status.json").read_text())
        return {
            **state,
            "status": "finished",
            **status,
            "seconds": round(status["finished"] - spec["started"], 1),
        }
    state.update(status="running", elapsed_s=round(time.time() - spec["started"], 1))
    try:
        with open(path / "lock") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return state
    except FileNotFoundError:
        pass
    if (path / "status.json").is_file():
        # Finished between the two checks.
        return _job_state(path)
    if not (path / "started").is_file() and time.time() - spec["started"] < 30:
        # Not yet holding its lock (slow interpreter start); not lost.
        return state
    # Neither running (lock free or never taken) nor finished: the job process died.
    boot = (path / "boot.log").read_text()[-2000:] if (path / "boot.log").is_file() else ""
    return {**state, "status": "lost", "exit_code": 2, "error": boot or "Job process exited"}


def _prune_jobs():
    """Job states from the last day; older finished or lost jobs are deleted."""
    jobs = []
    for path in sorted(_private_dir("jobs").iterdir(), key=lambda p: p.stat().st_mtime):
        if not re.fullmatch(r"[0-9a-f]{16}", path.name) or path.is_symlink():
            continue
        try:
            state = _job_state(path)
        except (OSError, ValueError, KeyError):
            continue
        if state["status"] != "running" and time.time() - path.stat().st_mtime > JOB_RETENTION_S:
            shutil.rmtree(path, ignore_errors=True)
            continue
        jobs.append(state)
    return jobs


def job(args):
    if args.action == "list":
        jobs = _prune_jobs()
        _emit(
            jobs,
            args.json,
            lambda jobs: (
                "\n".join(f"{j['job']}  {j['command']}  {j['status']}" for j in jobs) or "no jobs"
            ),
        )
        return 0
    path = _job_dir(args.id)
    if not 0 <= args.timeout <= MAX_JOB_WAIT_S:
        raise ValueError(f"--timeout must be within [0, {MAX_JOB_WAIT_S}] seconds")
    deadline = time.monotonic() + args.timeout
    while (state := _job_state(path))["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.2)
    if state["status"] == "running":
        log = (path / "log").read_text().splitlines()[-5:] if (path / "log").is_file() else []
        state.update(progress=log, next=f"peeky job wait {state['job']}")
        _emit(
            state,
            args.json,
            lambda s: (
                f"job {s['job']}: still running ({s['elapsed_s']} s)\n"
                + "".join(f"  {line}\n" for line in s["progress"])
                + f"run `{s['next']}` again"
            ),
        )
        return 3
    if state["status"] == "lost":
        raise RuntimeError(f"Job {state['job']} was lost: {state['error']}")
    # Replay exactly what the foreground command would have printed on each stream.
    sys.stdout.write((path / "out").read_text())
    if (path / "log").is_file():
        sys.stderr.write((path / "log").read_text())
    return state["exit_code"]


def main(argv=None, _job=False, _edits=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="peeky", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def background(sub):
        sub.add_argument(
            "--background",
            action="store_true",
            help="Return a job id at once; collect the result with `peeky job wait <id>`",
        )

    def common(sub, target=True):
        if target:
            sub.add_argument("target", help="Program file, Cargo crate or project directory")
            sub.add_argument("--config", help="Trusted target JSON (required for Java)")
        sub.add_argument("--json", action="store_true", help="Machine-readable output")

    def evidence_store(sub):
        sub.add_argument(
            "--store", help="Evidence database (default: $PEEKY_STATE_DIR/evidence/evidence.db)"
        )

    def queried(sub):
        sub.add_argument(
            "--param", action="append", default=[], help="name=value for :name (repeatable)"
        )
        sub.add_argument("--limit", type=int, default=200, help="Most rows to return (max 10000)")
        evidence_store(sub)

    sub = commands.add_parser("describe", help="Manifest: language, breakpoint lines, views")
    common(sub)
    sub.add_argument("--at", help="Program path (for multi-file targets)")
    sub = commands.add_parser("probe", help="One stateless question: stop, capture, set, report")
    common(sub)
    sub.add_argument("--at", required=True, help="<line> or <path>:<line>")
    sub.add_argument("--capture", default="", help="Comma-separated selectors, e.g. a,b.c,d[0]")
    sub.add_argument("--set", action="append", default=[], help="name=value (repeatable)")
    sub.add_argument("--hits", type=int, default=1)
    sub.add_argument(
        "--set-hit", type=int, default=1, help="Which hit gets the --set interventions (default 1)"
    )
    sub.add_argument(
        "--run", help="Python: script to run when --at is in a module it imports (e.g. main.py)"
    )
    sub.add_argument(
        "--worker",
        help="Probe on a running worker at this loopback URL (e.g. through ssh -L); token "
        "from $PEEKY_WORKER_TOKEN; local and worker sources must match",
    )
    sub.add_argument("--expect-sha256", action="append", default=[], help=argparse.SUPPRESS)
    sub.add_argument(
        "--arg", action="append", default=[], help="Python: program argument (repeatable)"
    )
    sub.add_argument("--timeout", type=float, default=60)
    sub.add_argument(
        "--until",
        choices=("auto", "exit", "hits"),
        default="auto",
        help="exit: run to completion for output; hits: end right after the last hit "
        "(auto: hits for attached JVMs, else exit)",
    )
    sub.add_argument("--quiet", action="store_true", help="No progress messages on stderr")
    sub.add_argument("--label", help="Tag this run in the evidence store (e.g. input=empty)")
    sub.add_argument(
        "--no-save", action="store_true", help="Do not record this run in the evidence store"
    )
    evidence_store(sub)
    background(sub)
    sub = commands.add_parser("sql", help="Read-only SQL over saved probe evidence")
    common(sub, target=False)
    sub.add_argument("query", nargs="?", help='One SELECT, e.g. "SELECT * FROM last_run"')
    sub.add_argument(
        "--schema", action="store_true", help="Tables, columns, canned queries and examples"
    )
    queried(sub)
    sub = commands.add_parser("queries", help="Canned evidence queries: no SQL needed")
    common(sub, target=False)
    sub.add_argument("name", nargs="?", help="Query to run; omit to list them")
    queried(sub)
    sub = commands.add_parser("session", help="Step-by-step debugging across commands")
    actions = sub.add_subparsers(dest="action", required=True)
    start = actions.add_parser("start", help="Build, launch and run to --at")
    common(start)
    start.add_argument("--at", required=True)
    start.add_argument("--run", help="Python: script to run when --at is in an imported module")
    start.add_argument(
        "--worker",
        help="Use a running worker at this loopback URL (e.g. through ssh -L) instead of "
        "starting one; token from $PEEKY_WORKER_TOKEN",
    )
    start.add_argument("--arg", action="append", default=[], help="Python: program argument")
    start.add_argument(
        "--max-pause",
        type=float,
        help=f"Auto-resume a stop after this many seconds (0: never; default "
        f"{ATTACH_MAX_PAUSE} for attached JVMs, never otherwise)",
    )
    for name in ("list", "wait", "inspect", "get", "set", "continue", "report", "close"):
        action = start if name == "start" else actions.add_parser(name)
        if name != "list":
            common(action, target=False)
    for action in actions.choices.values():
        action.add_argument("--name", default="default", help="Session name")
        action.add_argument("--timeout", type=float, default=30)
    actions.choices["get"].add_argument("selectors", nargs="+")
    actions.choices["set"].add_argument("assignment", help="name=value")
    actions.choices["close"].add_argument(
        "--forget",
        action="store_true",
        help="Drop the local record without contacting the worker (worker gone for good)",
    )
    actions.choices["continue"].add_argument(
        "--to-exit", action="store_true", help="Clear breakpoints and run the program to exit"
    )
    sub = commands.add_parser("verify", help="Fresh build + independent checker for one edit")
    common(sub)
    sub.add_argument("--at", help="Program path (for multi-file targets)")
    sub.add_argument("--check", required=True, help="Trusted checker script")
    sub.add_argument("--old", help="Exact source text to replace (once)")
    sub.add_argument("--new", help="Replacement text")
    sub.add_argument("--old-file", help="Read --old verbatim from a file (keeps newlines)")
    sub.add_argument("--new-file", help="Read --new verbatim from a file (keeps newlines)")
    sub.add_argument(
        "--keep", action="store_true", help="Keep the scratch candidate (default: removed)"
    )
    background(sub)
    sub = commands.add_parser("job", help="Results of --background probes and verifies")
    actions = sub.add_subparsers(dest="action", required=True)
    wait = actions.add_parser("wait", help="Wait up to --timeout; exit 3 while still running")
    wait.add_argument("id")
    wait.add_argument("--timeout", type=float, default=20)
    listing = actions.add_parser("list", help="Jobs from the last 24 hours")
    for action in (wait, listing):
        common(action, target=False)
    args = parser.parse_args(argv)
    args._edits = _edits
    handlers = {
        "describe": describe,
        "probe": probe,
        "sql": sql,
        "queries": queries,
        "session": session,
        "verify": verify,
        "job": job,
    }

    def terminate(signum, frame):
        # Unwind so `finally` blocks close debuggers and owned debuggees.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        if getattr(args, "background", False) and not _job:
            _emit(
                _background(argv, args.command, _submitted_edits(args)),
                args.json,
                lambda j: f"job {j['job']} started; collect it with `{j['next']}`",
            )
            return 0
        return handlers[args.command](args)
    except Exception as exc:
        # Every failure, including unexpected ones, is a structured error, never a traceback.
        from peeky.errors import classify

        payload = classify(exc, session_closed=False if isinstance(exc, ValueError) else None)
        if getattr(args, "json", False):
            print(json.dumps({"error": payload}, indent=2))
        print(f"peeky: [{payload.get('code')}] {payload.get('message')}", file=sys.stderr)
        print(f"  hint: {payload.get('hint', '')}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
