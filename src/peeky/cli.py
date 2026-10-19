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
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import asdict
from pathlib import Path

from peeky.client import Client
from peeky.errors import PROTOCOL_VERSION
from peeky.probe import (
    CANDIDATE_NEEDS_BUILD,
    MAX_EDITS,
    MAX_HITS,
    Probe,
    _capture,
    _table,
    candidate_line,
    combine_edits,
    parse_hits,
    parse_selector,
    run_probe,
    split_selectors,
)

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
        if language in ("java", "java-attach") and isinstance(data.get("bundle"), str):
            # Relative to the configuration file, wherever the command runs from (session
            # workers read a copy of the configuration from the state directory).
            data["bundle"] = str((Path(config).resolve().parent / data["bundle"]).resolve())
        if language in ("c", "cpp"):
            data["language"] = language
        root = path if path.is_dir() else path.parent
        return root, program, line, language, data
    if path.suffix == ".java" or str(program or "").endswith(".java"):
        raise ValueError(_JAVA_NEEDS_CONFIG)
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
    if path.is_dir() and (native := _native_program(path, program)):
        # A directory names its program by --at (`ring.cpp:16`), or holds exactly one.
        path, program = native
    if path.is_file() and path.suffix in (".c", ".cpp", ".cc", ".cxx"):
        language = "c" if path.suffix == ".c" else "cpp"
        suffixes = (".c",) if language == "c" else (".cpp", ".cc", ".cxx")
        headers = (".h",) if language == "c" else (".h", ".hpp", ".hh", ".hxx")
        root = path.parent
        tools = native_tools(language)
        data = {
            "language": language,
            "sources": _native_sources(root, path.name, suffixes, tools, language),
            "headers": tuple(sorted(p.name for p in root.iterdir() if p.suffix in headers)),
            **tools,
        }
        return root, program or path.name, line, language, data
    if (
        path.is_dir()
        and not program
        and (next(path.glob("*.java"), None) or next(path.glob("*/*.java"), None))
    ):
        raise ValueError(_JAVA_NEEDS_CONFIG)
    if path.is_dir() and (scripts := sorted(p.name for p in path.glob("*.py"))[:20]):
        raise ValueError(
            "Python directory target: pass --at <module.py>[:<line>] (and --run <script.py> "
            f"when the module is imported). Python files here: {', '.join(scripts)}"
        )
    raise ValueError(
        "Cannot detect the target; pass a .py/.c/.cpp file, a Cargo crate, or --config"
    )


_NATIVE_SUFFIXES = (".c", ".cpp", ".cc", ".cxx")
_JAVA_NEEDS_CONFIG = (
    "Java targets need --config <file.json> naming the adapter bundle, the sources and the "
    'main class, e.g. {"bundle": "work/java-adapter", "sources": ["shop/Main.java"], '
    '"main_class": "shop.Main"} (COOKBOOK 7.3; attached JVMs: 7.4)'
)


def _native_program(directory, program):
    """(file, program) for a C/C++ program in ``directory``, or None.

    `--at ring.cpp[:16]` names it; with only a line (or nothing), the directory must
    hold exactly one C/C++ file that defines `main`. Several are listed, never guessed.
    """
    if program:
        if Path(program).suffix not in _NATIVE_SUFFIXES:
            return None
        if "/" in program.replace("\\", "/"):
            # A C/C++ program is built with the files of its own directory.
            raise ValueError(
                f"Pass the program's own directory as the target: peeky probe "
                f"{(directory / Path(program).parent).resolve()} --at {Path(program).name}:<line>"
            )
        candidate = (directory / program).resolve()
        if not candidate.is_relative_to(directory) or not candidate.is_file():
            raise ValueError(f"No C/C++ file {program} inside {directory}")
        return candidate, candidate.name
    mains = sorted(
        item.name
        for item in directory.iterdir()
        if item.suffix in _NATIVE_SUFFIXES
        and item.is_file()
        and item.resolve().parent == directory
        and _MAIN.search(item.read_bytes()[:4_000_000])
    )[:20]
    if len(mains) > 1:
        # As for a file target: the preprocessor settles which really define main.
        mains = [
            name
            for name in mains
            if _defines_main(
                directory / name,
                native_tools(language := "c" if name.endswith(".c") else "cpp"),
                language,
            )
        ]
    if len(mains) > 1:
        raise ValueError(
            f"Several C/C++ programs define main here ({', '.join(mains)}); name one with "
            f"--at, e.g. --at {mains[0]}:<line>"
        )
    return ((directory / mains[0]).resolve(), mains[0]) if mains else None


_MAIN = re.compile(rb"^[ \t]*(?:int|void)[ \t\n]+main[ \t]*\(", re.MULTILINE)
_MAIN_DEFINITION = re.compile(
    rb"(?:^|[;{}\s])(?:int|void)\s+main\s*\([^;{}()]*\)\s*\{", re.MULTILINE
)


def _defines_main(path, tools, language):
    """Whether the compiled file defines main: comments and inactive #if branches don't."""
    compiler = (tools or {}).get("compiler")
    if not compiler:
        return True
    sysroot = (tools or {}).get("sysroot")
    command = [compiler, "-E", "-P", "-x", "c" if language == "c" else "c++"]
    command += ["-isysroot", sysroot] if sysroot else []
    try:
        done = subprocess.run(
            [*command, str(path)], cwd=path.parent, capture_output=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return done.returncode != 0 or bool(_MAIN_DEFINITION.search(done.stdout[:16_000_000]))


def _native_sources(root, target, suffixes, tools=None, language="c"):
    """Sources to build for an auto-detected C/C++ file.

    A directory may hold one program split over several files, or several programs.
    Every file defining `main` is a separate program, so only the target's own `main`
    is linked with the files that have none. A quick text match finds candidates; when
    there are several, the compiler's preprocessor settles which really define main.
    """
    sources = sorted(p.name for p in root.iterdir() if p.suffix in suffixes and p.is_file())
    mains = [name for name in sources if _MAIN.search((root / name).read_bytes()[:4_000_000])]
    if len(mains) > 1:
        mains = [name for name in mains if _defines_main(root / name, tools, language)]
    if len(mains) <= 1:
        return tuple(sources)
    if target not in mains:
        raise ValueError(
            f"{root} holds several programs (main is defined in {', '.join(mains[:10])}); pass "
            "the file that defines the program's main as the target, or use --config with "
            "explicit sources"
        )
    return tuple(name for name in sources if name == target or name not in mains)


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


def _parse_error(path, exc):
    return ValueError(f"{path} does not parse as Python: {exc.msg} (line {exc.lineno})")


def _python_lines(path):
    try:
        code = compile(path.read_text(), str(path), "exec")
    except SyntaxError as exc:
        raise _parse_error(path, exc) from exc
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
    try:
        tree = ast.parse(text, str(path))
        table = symtable.symtable(text, str(path), "exec")
    except SyntaxError as exc:
        raise _parse_error(path, exc) from exc
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
    run = (
        " [--run <script.py>] [--arg A ...]"
        if language == "python"
        else ""
        if language == "java-attach"
        else " [--arg A ...]"
    )
    # The commands repeat the --config this describe was given (Java, explicit targets).
    target = f"{args.target} --config {args.config}" if args.config else args.target
    # A file target is the file to edit; a directory needs --at to name it.
    single = (
        Path(args.target).is_file() and Path(args.target).resolve() == (root / program).resolve()
    )
    at = "" if single else f" --at {program}"
    manifest = {
        "peeky": "describe",
        "language": language,
        "root": str(root),
        "program": program,
        "commands": {
            "probe": f"peeky probe {target} --at {program}:<line>{run} --capture <a,b.c> "
            "[--set name=value] [--set-hit N] [--hits N] [--json]",
            "session": f"peeky session start {target} --at {program}:<line>{run} [--hit N]; then "
            "peeky session inspect | get <a.b> | set <name>=<value> | step [--into|--out] | "
            "continue [--count N|--to LINE|--to-exit] | "
            "report | close",
            "verify": f"peeky verify {target}{at} --check <checker.py> "
            "--old <text> --new <text> (or --old-file/--new-file; repeat the pair for several "
            "edits, each quoting the original file)",
            "background": "add --background to probe or verify, then `peeky job wait <id>` "
            "(exit 3 while running)",
            **(
                {}
                if language in ("python", "java-attach")
                else {
                    "candidate": f"peeky probe {target} --at {program}:<line> --capture <a,b> "
                    "--old <text> --new <text> (or --old-file/--new-file): the same probe on a "
                    "fresh build with the edit, your files unchanged; then `peeky queries diff`"
                }
            ),
            "evidence": 'every probe is saved with a run_id: peeky sql "SELECT hit, selector, '
            'value FROM last_run"; peeky queries [name] (canned, no SQL); peeky sql --schema',
        },
        "selectors": "dotted paths, indexes and string keys, e.g. request.page, args[0], "
        "order['id']; never expressions",
        "interventions": "root locals (the stopped function's own variables); in Java also "
        "scalar fields and array elements (current.end, this.count, xs[0]); evidence about one "
        "run, never a verified fix",
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
            if scopes := getattr(runtime, "function_scopes", None):
                # Display only: a dump the parser cannot read omits scopes, never the target.
                try:
                    manifest["scopes"] = scopes()
                except (OSError, ValueError, RuntimeError) as exc:
                    manifest["scopes_unavailable"] = str(exc)[:300]
            if layouts := getattr(runtime, "layouts", None):
                manifest["value_views"] = {
                    name: layout["kind"] for name, layout in sorted(layouts.items())
                }
        finally:
            runtime.close()
        manifest["mutable_types"] = {
            "rust": "integer root locals (i16 to i64, u16 to u64, isize, usize) the program "
            "keeps in memory; `inspect` marks them [settable]",
            "c": "integer root locals (16 to 64 bits, incl. size_t/int64_t); `inspect` marks "
            "them [settable]",
            "cpp": "integer root locals (16 to 64 bits, incl. size_t/int64_t); `inspect` marks "
            "them [settable]",
            "java": "numeric/boolean root locals, and numeric/boolean fields (not final or "
            "static) and array elements (current.end, xs[0]); rows mark them [settable]",
            "java-attach": "numeric/boolean root locals, fields and array elements (attached "
            "JVM; no edit verification)",
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
        lines.append(f"breakpoint lines in {source}: {_ranges(numbers) or 'none'}")
    if views := manifest.get("value_views"):
        from peeky.rust_symbols import CONTAINER_KINDS, QUALIFIED_CONTAINERS, REFERENCE_PARAMETERS

        enums = [name for name, kind in views.items() if kind == "option"]
        containers = [
            declared
            for declared, (_, name, _) in REFERENCE_PARAMETERS.items()
            if views.get(name) in CONTAINER_KINDS
        ]
        opaque = [
            name
            for name, kind in views.items()
            if kind == "unqualified" and name not in QUALIFIED_CONTAINERS
        ]
        lines.append(f"qualified enum views: {', '.join(enums) or 'none'}")
        if containers:
            lines.append(
                "read from memory (debuggers with readMemory): reference parameters, and owned "
                "Vec<T>/String locals where the build's MIR proves them initialized: "
                + ", ".join(containers)
            )
        lines.append(f"other enum types (shown opaque, not decoded): {len(opaque)}")
    if reason := manifest.get("scopes_unavailable"):
        lines.append(f"function names: unavailable ({reason})")
    for scope in manifest.get("scopes", ()):
        if scope.get("omitted"):
            lines.append(f"... {scope['omitted']} more scopes not listed (first 300 shown)")
            continue
        span = f"{scope['lines'][0]}-{scope['lines'][1]}" if scope["lines"] else ""
        if scope.get("source"):
            where = f" ({scope['source']}:{span})"
        else:
            where = f" (lines {span})" if span else ""
        lines.append(f"names in {scope['scope']}{where}: {', '.join(scope['names']) or '-'}")
    from peeky.probe import optimization_level

    if level := optimization_level(manifest.get("target_identity")):
        lines.append(
            f"warning: optimized build ({level}); locals may be unavailable, stale or wrong. "
            "Prefer an unoptimized build for debugging."
        )
    mutable = manifest["mutable_types"]
    if "root locals" in mutable and "fields" not in mutable:
        mutable += " (root locals: the stopped function's own variables, not fields)"
    lines.append(f"mutable: {mutable}")
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
    edit, original = _probe_edit(args, root, program, line, language)
    request = Probe(
        program,
        line,
        split_selectors(args.capture),
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
        edit=edit,
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
    if edit:
        _bind_candidate(result, original, edit, program)
    if misplaced := _misplaced_options(args):
        if (result.get("error") or {}).get("code") == "NO_STOP":
            result["error"]["hint"] = misplaced + "."
        else:
            result.setdefault("warnings", []).append(misplaced)
    if not args.no_save:
        _keep_evidence(args, result, root, language)
    _emit(result, args.json, _probe_text)
    return 0 if result["status"] == "completed" else 1


def _misplaced_options(args):
    """Program arguments after `--` that look like Peeky's own options, as a warning."""
    options = getattr(args, "_options", None) or ()
    found = [arg.split("=", 1)[0] for arg in getattr(args, "arg", None) or ()]
    found = sorted({name for name in found if name in options})
    if not found:
        return None
    return (
        f"Everything after `--` goes to the program, including {', '.join(found[:4])}, which "
        "Peeky also takes: put Peeky's options before `--`"
    )


def _probe_edit(args, root, program, line, language):
    """The edit (one (old, new) pair, or a tuple of them) and the original text for
    `probe --old/--new`, else (None, None)."""
    if not getattr(args, "_edits", None) and all(
        not value for value in (args.old, args.new, args.old_file, args.new_file)
    ):
        return None, None
    if language in ("python", "java-attach"):
        raise ValueError(CANDIDATE_NEEDS_BUILD)
    pairs = _edit_pairs(args)
    original = (root / program).read_text()
    edit = _checked_edit(original, pairs)
    candidate_line(original, edit, line)  # refused before anything is built
    return edit, original


def _bind_candidate(result, original, edit, program):
    """Attach the diff, and keep values only if they came from exactly this candidate."""
    candidate = original.replace(*combine_edits(original, edit), 1)
    expected = hashlib.sha256(candidate.encode()).hexdigest()
    (result.get("probe") or {}).pop("edit", None)
    info = result.setdefault("candidate", {})
    info["diff"] = _diff(original, candidate, program)
    ran = bool(result.get("hits") or result.get("rows") or result.get("output"))
    if info.get("sha256") != expected and (ran or info.get("sha256") is not None):
        from peeky.errors import classify

        # A worker without candidate probes, or a source changed mid-run: these values
        # cannot be attributed to this edit.
        result.update(
            status="failed",
            error=classify(
                RuntimeError(
                    "Provenance: the build that ran is not this candidate (expected sha256 "
                    f"{expected[:12]}, got {str(info.get('sha256'))[:12]}); values discarded"
                ),
                session_closed=True,
            ),
            rows=[],
            by_hit=[],
            interventions=[],
            output="",
            results_discarded=True,
        )


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
    if candidate := result.get("candidate"):
        ran = result.get("hits") or result.get("rows") or result.get("output")
        text += (
            "\ncandidate: values are from a fresh build with this edit"
            if ran
            else "\ncandidate: this edit"
        ) + "; your files are unchanged"
        if candidate.get("line") not in (None, candidate.get("original_line")):
            text += (
                f" (line {candidate['original_line']} is line {candidate['line']} of the candidate)"
            )
        if diff := candidate.get("diff"):
            text += "\n" + diff.rstrip("\n")
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
        from peeky.errors import PeekyError

        try:
            return self.client.call(operation, **args)
        except PeekyError as exc:
            # One selector the worker could not read is that row's error (as in a local
            # probe), not a failure of the whole `get`; session-level errors still raise.
            if exc.error_code in _ROW_ERRORS and not exc.session_closed:
                raise ValueError(exc.payload.get("message", str(exc))) from exc
            raise


_ROW_ERRORS = frozenset({"BUDGET_EXHAUSTED", "NOT_FOUND", "STALE_HANDLE"})


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
    if isinstance(record, dict) and record.get("starting"):
        if _abandoned(path):
            raise ValueError(
                f"No session named {name!r}: its start was interrupted. Start again (the name "
                f"is taken over) or run `peeky session close --forget --name {name}`"
            )
        raise ValueError(
            f"Session {name!r} is still starting (pid {record['starting']}); wait for it, or "
            f"pass another --name"
        )
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
    if where and state.get("function"):
        from peeky.names import display_function

        where += f" in {display_function(state['function'])}"
    if where and type(state.get("hit")) is int:
        where += f", hit {state['hit']}"
    hint = ""
    if state["state"] == "running":
        hint = " (still running; run `peeky session wait`)"
    elif state["state"] == "terminated":
        hint = " (program exited; `peeky session report` shows output)"
    reason = state.get("reason")
    if state.get("outside_program"):
        where += (
            " (outside the program's sources: `session step --out` returns to your code, "
            "or `session continue`)"
            if state.get("returns_to_program")
            else " (outside the program's sources, and your code is no longer on the stack: "
            "`session continue`)"
        )
    if reason and reason not in ("breakpoint", "step"):
        where += f" [reason: {reason}; not auto-resumed, inspect it or `session report`]"
    text = f"state: {state['state']}{where}{_pause_text(state)}{hint}"
    if state.get("optimized"):
        text += f"\n{_OPTIMIZED_NOTE.format(state['optimized'])}"
    if state.get("exit_inferred"):
        text += "\nnote: the debugger never reported the exit; Peeky inferred it (no threads left)"
    if state.get("note"):
        text += f"\nnote: {state['note']}"
    for warning in state.get("warnings", []):
        text += f"\nwarning: {warning}"
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


def _variables_text(snapshot, name="default"):
    frame = snapshot["frames"][0] if snapshot["frames"] else {}
    where = snapshot.get("location") or f"{frame.get('source', {}).get('path')}:{frame.get('line')}"
    if function := snapshot.get("function"):
        from peeky.names import display_function

        where += f" in {display_function(function)}"
    lines = [f"stopped at {where}" + _pause_text(snapshot)]
    for scope in snapshot["scopes"]:
        lines.append(f"[{scope['name']}]")
        for variable in scope["variables"]:
            more = (
                f" (children: peeky session get {variable['name']}"
                + (f" --name {name})" if name != "default" else ")")
                if variable.get("variablesReference")
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable["name"])
                else ""
            )
            if variable.get("unavailable"):
                more = f" (no readable value: {variable.get('reason')})"
            view = f"  [{variable['view']}]" if variable.get("view") else ""
            if variable.get("settable"):
                view += "  [settable]"
            lines.append(
                f"  {variable['name']}: {variable.get('type', '')} = "
                f"{'<no value>' if variable.get('unavailable') else variable['value']}{view}{more}"
            )
    if snapshot.get("optimized"):
        lines.append(_OPTIMIZED_NOTE.format(snapshot["optimized"]))
    if snapshot.get("mutation_blocked"):
        lines.append(f"note: {snapshot['mutation_blocked']}")
    return "\n".join(lines)


def _require_stepping(record, client):
    """Remote workers built before `step` silently ignore `continue --to`: refuse."""
    if not record.get("remote"):
        return
    if "step" not in client.call("version").get("operations", ()):
        raise ValueError(
            "This remote worker predates `session step` and `continue --to`; update Peeky "
            "on the worker host"
        )


def _get_lines(row):
    """`session get` text: the value, then one level of children with their selectors."""
    if "value" not in row and not row.get("unavailable"):
        # Nothing was read: there is no type or value to show, only why.
        lines = [f"{row['selector']}: <{row.get('error', '')}>"]
    else:
        value = row.get("value", "<" + row.get("error", "") + ">")
        lines = [f"{row['selector']}: {row.get('type') or ''} = {value}"]
    for child in row.get("children", []):
        name = child.get("selector") or f"{child['name']} [not selectable]"
        more = (
            f" (children: peeky session get {shlex.quote(child['selector'])})"
            if child.get("expandable") and child.get("selector")
            else ""
        )
        value = (
            f"<no readable value: {child.get('reason')}>"
            if child.get("unavailable")
            else child.get("value")
        )
        lines.append(f"  {name}: {child.get('type') or ''} = {value}{more}")
    if row.get("children_truncated"):
        lines.append("  ... more children; select one by name or index")
    if row.get("children_error"):
        lines.append(f"  <children: {row['children_error']}>")
    if row.get("note"):
        lines.append(f"  note: {row['note']}")
    return lines


def session(args):
    if args.action in ("start", "wait", "continue", "step"):
        # Checked before anything runs: a bad --timeout must not follow a real action.
        _seconds(args.timeout)
    if args.action == "start":
        return _start(args)
    if args.action == "list":
        names = sorted(
            path.stem for path in _state_dir().glob("*.json") if not path.stem.endswith(".target")
        )
        _emit(names, args.json, lambda names: "\n".join(names) if names else "no sessions")
        return 0
    if args.action == "close" and args.forget:
        # The worker is gone for good, or the start never finished: drop the local
        # record without reading it or contacting any worker.
        _record(args.name)
        _release(args.name)
        _emit(
            {"forgot": args.name, "worker_contacted": False},
            args.json,
            lambda r: f"forgot {r['forgot']} (the worker was not contacted)",
        )
        return 0
    record, client = _load(args.name)
    if args.action == "wait":
        state = _wait(record, client, args.timeout)
        _emit(state, args.json, _state_text)
    elif args.action == "inspect":
        snapshot = client.call("inspect", **_stop(record))
        _emit(snapshot, args.json, lambda shown: _variables_text(shown, args.name))
    elif args.action == "get":
        rows = _capture(
            _Remote(client),
            _stop(record),
            tuple(args.selectors),
            0,
            "observed",
            children=True,
            # Launched Java values show their fields (an attached JVM's do not).
            preview=record.get("language") == "java",
        )
        # A session read is not a probe hit: no hit number to explain.
        rows = [{key: v for key, v in row.items() if key != "hit"} for row in rows]
        if level := record.get("optimized"):
            rows = [{**row, "optimized": level} for row in rows]
        _emit(
            rows,
            args.json,
            lambda rows: "\n".join(
                [line for row in rows for line in _get_lines(row)]
                + ([_OPTIMIZED_NOTE.format(record["optimized"])] if record.get("optimized") else [])
            ),
        )
        if any("error" in row for row in rows):
            # Every row was still printed; the exit status says one selector failed.
            return 1
    elif args.action == "set":
        from peeky.probe import apply_set

        (name, value), stop = _sets([args.assignment])[0], _stop(record)
        if (last := parse_selector(name)[-1]).startswith("[") and last[1:2] in "'\"":
            from peeky.errors import Refused

            raise Refused(
                "MUTATION_REJECTED",
                f"Intervention target {name!r}: map and dict entries cannot be changed; "
                "set changes a local, or in Java a field or array element",
            )
        changed = apply_set(_Remote(client), stop, name, value)
        _emit(changed, args.json, lambda c: f"set {c['name']}: {c['before']} -> {c['after']}")
    elif args.action == "continue":
        # Argument errors first, whatever state the program is in.
        if args.to is not None and args.to_exit:
            raise ValueError("Pass --to LINE or --to-exit, not both")
        if args.count != 1 and (args.to is not None or args.to_exit):
            raise ValueError("--count counts breakpoint stops; it cannot go with --to or --to-exit")
        stop = _stop(record)
        if args.to is not None:
            _require_stepping(record, client)
        # --to-exit clears the breakpoints in the same worker operation, after it
        # confirms this stop is still current (it may have been auto-resumed).
        client.call(
            "continue",
            **stop,
            **({"to_exit": True} if args.to_exit else {}),
            **({"to_line": args.to} if args.to is not None else {}),
        )
        deadline = time.monotonic() + args.timeout
        state = _wait(record, client, args.timeout)
        complete = True
        if args.count > 1:
            if _counted(state):
                state, _, complete = _run_on(
                    record, client, state, args.count - 1, deadline, f"stop {args.count}"
                )
            else:
                complete = False
                state["note"] = (
                    f"the program exited before stop {args.count}"
                    if state["state"] == "terminated"
                    else f"stopped for another reason before stop {args.count} (shown above)"
                    if state["state"] == "stopped"
                    else f"the program is still running (stop {args.count}); `peeky session wait`"
                )
        finished = 0
        while args.to_exit and state["state"] == "stopped" and state.get("reason") == "step":
            # LLDB completes a step that a breakpoint interrupted before running on.
            finished += 1
            if finished > 3:
                break
            client.call("continue", **_stop(record), to_exit=True)
            state = _wait(record, client, args.timeout)
        _emit(state, args.json, _state_text)
        if not complete:
            return 3
    elif args.action == "step":
        kind = "in" if args.into else "out" if args.out else "over"
        _require_stepping(record, client)
        client.call("step", **_stop(record), kind=kind)
        state = _wait(record, client, args.timeout)
        _emit(state, args.json, _state_text)
    elif args.action == "report":
        report = client.call("report", session_id=record["session_id"])
        _emit(
            report,
            args.json,
            lambda r: (
                f"state: {r['state']}\noutput:\n{r['output']}operations: {len(r['history'])} "
                "(worker requests in this session, including waits; --json lists them)"
            ),
        )
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
        _emit({"closed": args.name}, args.json, lambda r: f"closed {r['closed']}")
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
        edit=request.edit,
    )
    # A worker whose probe child was killed (its cap) or crashed early answers with
    # only status/error; give every result the same complete shape.
    if result.get("status") is None:
        result["status"] = "failed"
    for key, default in (
        ("rows", []),
        ("by_hit", []),
        ("interventions", []),
        ("warnings", []),
        ("pauses_ms", []),
        ("hits", 0),
        ("seconds", 0),
        ("output", ""),
        ("output_truncated", False),
        ("probe", {**asdict(request), "set": [list(change) for change in request.set]}),
    ):
        result.setdefault(key, default)
    if result["status"] != "completed" and not isinstance(result.get("error"), dict):
        result["error"] = {
            "code": "DEBUGGER_ERROR",
            "message": "The worker's probe failed without an error description",
            "hint": "Check the worker's log; start again.",
            "retryable": False,
            "session_closed": True,
        }
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


def _start_remote(args, root, program, line, language, max_pause):
    client, token = _remote(args.worker)
    launch = client.call(
        "launch",
        program=program,
        breakpoints=[{"line": line}],
        max_pause=max_pause,
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
    state, complete = _until_hit(record, client, args.hit, args.timeout)
    if record.get("optimized"):
        state["optimized"] = record["optimized"]
    if launch.get("warnings"):
        state["warnings"] = launch["warnings"]
    if misplaced := _misplaced_options(args):
        state["warnings"] = [*state.get("warnings", []), misplaced]
    _emit(
        {"session": args.name, "language": language, "worker": args.worker, **state},
        args.json,
        lambda s: f"session {s['session']} ({s['language']}, remote): " + _state_text(s),
    )
    return 0 if complete else 3


def _abandoned(path):
    """A reservation whose `session start` died (SIGKILL) before saving the session."""
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        # Killed between creating and writing the reservation.
        try:
            return time.time() - path.stat().st_mtime > 30
        except OSError:
            return False
    starter = record.get("starting") if isinstance(record, dict) else None
    return type(starter) is int and starter > 0 and not _pid_alive(starter)


def _reserve(name):
    """Claim a session name atomically: two starts with one name cannot both succeed."""
    import fcntl

    path = _record(name)
    with open(_state_dir() / ".reserve.lock", "a") as lock:
        # Serializes takeovers: two starters cannot both remove an abandoned claim.
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists() and not path.is_symlink() and _abandoned(path):
            path.unlink()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise ValueError(f"Session {name!r} exists; close it or pass --name") from None
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"name": name, "starting": os.getpid()}, stream)


def _release(name):
    for suffix in (".json", ".target.json"):
        (_state_dir() / f"{name}{suffix}").unlink(missing_ok=True)


def _stop_worker(process):
    """Stop a worker this command spawned; SIGTERM lets it close its debugger first."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    except ProcessLookupError:
        pass


def _start(args):
    root, program, line, language, data = resolve(args.target, args.at, args.config)
    if line is None:
        raise ValueError("session start requires --at <path>:<line>")
    _seconds(args.timeout)
    max_pause = _max_pause(args.max_pause, language)
    _reserve(args.name)
    try:
        if args.worker is not None:
            return _start_remote(args, root, program, line, language, max_pause)
        state = _start_local(args, root, program, line, language, data, max_pause)
    except BaseException:
        # Failed or interrupted (e.g. SIGTERM from a tool timeout) before the session
        # existed: give the name back. Once saved, the session stays for `session wait`.
        try:
            if json.loads(_record(args.name).read_text()).get("starting"):
                _release(args.name)
        except (OSError, ValueError):
            pass
        raise
    _emit(
        {"session": args.name, "language": language, **state},
        args.json,
        lambda s: f"session {s['session']} ({s['language']}): " + _state_text(s),
    )
    # 3: the session exists but is not at the requested hit (see the note).
    return 3 if state.pop("incomplete", False) else 0


def _start_local(args, root, program, line, language, data, max_pause):
    token = secrets.token_urlsafe(32)
    command = [sys.executable, "-m", "peeky.worker", "--root", str(root), "--port", "0"]
    # A session's own worker ends with the session, including after its idle timeout.
    command.append("--exit-when-idle")
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
    # Until the record is saved, this command owns the worker: any failure or
    # interruption stops it (and its paused debuggee).
    try:
        ready, _, _ = select.select([process.stdout], [], [], 30)
        banner = process.stdout.readline() if ready else ""
        match = re.search(r"(http://127\.0\.0\.1:[0-9]+)", banner)
        if not match:
            raise RuntimeError(f"Worker did not start; see {log_path}")
        client = _LocalClient(match[1], token, process.pid)
        record = {"name": args.name, "url": match[1], "token": token, "pid": process.pid}
        record.update(root=str(root), program=program, language=language)
        launch = client.call(
            "launch",
            program=program,
            breakpoints=[{"line": line}],
            max_pause=max_pause,
            **({"entry": entry} if (entry := _entry(root, args.run, language)) else {}),
            **({"args": list(args.arg)} if args.arg else {}),
        )
        record.update(session_id=launch["session_id"], state="running", stop_id=None)
        if launch.get("optimized"):
            record["optimized"] = launch["optimized"]
        _save(record)
    except BaseException as exc:
        _stop_worker(process)
        from peeky.client import TransportLost, WorkerUnreachable

        if isinstance(exc, (WorkerUnreachable, TransportLost)):
            # Worded so it is classified as a closed session, not a retryable transport error.
            raise RuntimeError(
                "The local worker failed while launching and was stopped; no session exists. "
                "Start again"
            ) from exc
        raise
    state, complete = _until_hit(record, client, args.hit, args.timeout)
    if record.get("optimized"):
        state["optimized"] = record["optimized"]
    if not complete:
        state["incomplete"] = True
    if launch.get("warnings"):
        state["warnings"] = launch["warnings"]
    if misplaced := _misplaced_options(args):
        state["warnings"] = [*state.get("warnings", []), misplaced]
    return state


def _positive_count(text):
    if not re.fullmatch(r"[1-9][0-9]{0,3}", text):
        raise argparse.ArgumentTypeError("expected a whole number from 1 to 9999")
    return int(text)


def _counted(state):
    """Whether a stop is one of the session's numbered breakpoint stops. A worker that
    does not number hits (older Peeky) counts its breakpoint stops by reason."""
    if state["state"] != "stopped":
        return False
    if "numbered" in state:
        return type(state.get("hit")) is int
    return state.get("reason") == "breakpoint"


def _run_on(record, client, state, more, deadline, target):
    """Continue while the stop is a counted breakpoint stop, `more` times at most.
    Anything else (a crash, a finished step, the program's exit) ends it and is shown.
    Returns (state, stops reached, complete)."""
    reached, last = 0, state.get("hit")
    while reached < more:
        if not _counted(state) or time.monotonic() >= deadline:
            break
        client.call("continue", **_stop(record))
        state = _wait(record, client, max(0.1, deadline - time.monotonic()))
        if not _counted(state):
            break
        reached, last = reached + 1, state.get("hit", last)
    if reached == more:
        return state, reached, True
    if state["state"] == "terminated":
        state["note"] = f"the program exited before {target}"
    elif state["state"] == "stopped" and not _counted(state):
        state["note"] = f"stopped for another reason before {target} (shown above)"
    elif state["state"] == "stopped":
        state["note"] = f"reached {reached} of {more} stops before --timeout ({target})"
    else:
        state["note"] = f"the program is still running ({target}); `peeky session wait`"
    if type(last) is int:
        state["note"] += f"; last hit {last}"
    return state, reached, False


def _until_hit(record, client, hit, timeout):
    """Wait for the first stop, then continue to the session's Nth breakpoint stop
    (`session start --hit N`), all within one --timeout. Returns (state, complete)."""
    deadline = time.monotonic() + _seconds(timeout)
    state = _wait(record, client, timeout)
    if not hit or state["state"] != "stopped":
        if hit and state["state"] == "terminated":
            state["note"] = f"the program exited before hit {hit}"
        return state, not hit or state["state"] == "stopped"
    if not state.get("numbered") or type(state.get("hit")) is not int:
        state["note"] = (
            "this stop has no hit number (the worker predates hit numbers: update Peeky on "
            "the worker host)"
            if "numbered" not in state
            else "this stop has no hit number"
        ) + f"; stopped at the first stop instead of hit {hit}"
        return state, False
    state, _, complete = _run_on(record, client, state, hit - state["hit"], deadline, f"hit {hit}")
    return state, complete


def _submitted_edits(args):
    """Edits read from files when a background job is submitted (the files may change)."""
    if args.command not in ("verify", "probe") or not (args.old_file or args.new_file):
        return None
    return {"pairs": [list(pair) for pair in _edit_pairs(args)]}


def _edit_pairs(args):
    """[(old, new)] from repeatable --old/--new (or --old-file/--new-file), in order."""
    if edits := getattr(args, "_edits", None):
        return [tuple(pair) for pair in edits["pairs"]]
    texts = {}
    for name in ("old", "new"):
        given, files = getattr(args, name) or [], getattr(args, f"{name}_file") or []
        if given and files:
            raise ValueError(f"Pass either --{name} or --{name}-file, not both")
        texts[name] = list(given) or [_edit_text(None, path, name) for path in files]
    if not texts["old"] or len(texts["old"]) != len(texts["new"]):
        raise ValueError(
            "Pass --old and --new (or their -file forms) once per edit, in pairs: "
            f"got {len(texts['old'])} --old and {len(texts['new'])} --new"
        )
    if len(texts["old"]) > MAX_EDITS:
        raise ValueError(f"At most {MAX_EDITS} edits")
    return list(zip(texts["old"], texts["new"]))


def _checked_edit(original, pairs):
    """The edit for these pairs (one pair, or a tuple of them), each checked against the
    original: --old matches exactly once, --new changes it, and no two overlap."""
    for number, (old, new) in enumerate(pairs, 1):
        _check_edit(original, old, new, f" (edit {number})" if len(pairs) > 1 else "")
    edit = pairs[0] if len(pairs) == 1 else tuple(pairs)
    combine_edits(original, edit)
    return edit


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


def _check_edit(text, old, new, which=""):
    if not old or old == new:
        raise ValueError(f"--old must match exactly once and --new must change it{which}")
    count = text.count(old)
    if count == 1:
        return
    if count:
        lines, start = [], 0
        while (index := text.find(old, start)) >= 0 and len(lines) < 10:
            lines.append(str(text.count("\n", 0, index) + 1))
            start = index + len(old)
        raise ValueError(
            f"--old must match exactly once and --new must change it{which}; it matched "
            f"{count} times (lines {', '.join(lines)}); include more surrounding text"
        )
    advice = "copy the exact text, including indentation"
    if "\\n" in old and "\n" not in old:
        advice = "it contains a literal backslash-n; pass real newlines (bash: $'a\\nb')"
    raise ValueError(
        f"--old must match exactly once and --new must change it{which}; it matched 0 times: "
        f"{advice}"
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
    # Several edits are verified as the one edit with their combined effect.
    args.old, args.new = combine_edits(original, _checked_edit(original, _edit_pairs(args)))
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
    if not args.details:
        # Provenance the hashes and ids already identify; --details keeps it (with the
        # toolchain probes, it is several KB an agent rarely needs).
        for key in ("descriptor", "build_manifest"):
            result.pop(key, None)

    _emit(result, args.json, lambda shown: _verify_text(shown, args.keep))
    return 0 if result["passed"] else 1


def _verify_text(result, keep=False):
    lines = [f"passed: {result['passed']}  stage: {result['stage']}"]
    if tests := result.get("tests"):
        lines.extend(_tests_text(tests))
    if not result["passed"]:
        reason = result.get("diagnostic") or (result.get("checker") or {}).get("error")
        # A build's own timeout is in its diagnostic (`The build failed: clang timed out`).
        if result.get("timed_out") and result.get("stage") != "build":
            reason = "checker timed out"
        if reason:
            lines.append(f"reason: {reason}")
    lines.append(f"checker exit: {result.get('exit_code')}")
    output = [(result.get(key) or "")[-2000:] for key in ("stdout", "stderr")]
    if any(output):
        lines.extend(("output:", *output))
    if keep:
        lines.append(
            f"candidate: {result.get('candidate')} (the edited file, and for compiled "
            "programs the build evidence beside it; build artifacts are not kept)"
        )
    lines.append("diff:\n" + result["diff"])
    return "\n".join(line for line in lines if line)


def _printable(text):
    """Test output for a terminal: control characters (\\r, ANSI escapes) escaped."""
    return "".join(c if c.isprintable() or c == "\n" else repr(c)[1:-1] for c in text)


def _tests_text(tests):
    """`compiled tests: passed (3 passed)`, or the failed tests and how they failed."""
    summary = tests.get("summary")
    counts = ", ".join(
        f"{summary[key]} {key.replace('_', ' ')}"
        for key in ("passed", "failed", "ignored", "filtered_out")
        if summary and summary.get(key)
    )
    if tests.get("passed"):
        return [f"compiled tests: passed ({counts})"]
    if summary is None:
        counts = "no test summary"
    lines = [f"compiled tests: failed ({counts or '0 tests ran'})"]
    for failure in tests.get("failures", []):
        message = "\n    ".join(_printable(failure["message"]).splitlines())
        name = _printable(failure["name"])
        lines.append(f"  {name}: {message}" if message else f"  {name}")
    if tests.get("timed_out"):
        lines.append("  (the tests timed out)")
    if tests.get("output_limit_exceeded"):
        lines.append("  (the tests wrote more output than Peeky keeps)")
    elif summary and summary.get("status") == "ok" and tests.get("exit_code") not in (0, None):
        lines.append(f"  (the test binary exited with code {tests['exit_code']})")
    return lines


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
    if "--" in argv and (argv[:1] == ["probe"] or argv[:2] == ["session", "start"]):
        # `peeky probe t --at 5 -- a b c` passes a, b and c to the program.
        split = argv.index("--")
        argv = argv[:split] + [f"--arg={value}" for value in argv[split + 1 :]]
    parser = argparse.ArgumentParser(prog="peeky", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def edit_options(sub):
        sub.add_argument("--new", action="append", help="Replacement text for the --old before it")
        sub.add_argument(
            "--old-file",
            action="append",
            help="Read an --old verbatim from a file (keeps newlines)",
        )
        sub.add_argument(
            "--new-file", action="append", help="Read a --new verbatim from a file (keeps newlines)"
        )

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
    probe_parser = sub
    common(sub)
    sub.add_argument("--at", required=True, help="<line> or <path>:<line>")
    sub.add_argument("--capture", default="", help="Comma-separated selectors, e.g. a,b.c,d[0]")
    sub.add_argument(
        "--set",
        action="append",
        default=[],
        help="name=value: a local; in Java also a field or array element (current.end=10, "
        "xs[0]=1). Repeatable",
    )
    sub.add_argument(
        "--hits",
        type=parse_hits,
        default=1,
        help=f"Stops to capture (default 1), or 'all': every stop, up to {MAX_HITS}",
    )
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
        "--arg",
        action="append",
        default=[],
        help="Program argument (repeatable; replaces configured arguments). Or put the "
        "program's arguments after `--`: peeky probe t --at 5 -- a b c",
    )
    sub.add_argument(
        "--timeout",
        type=float,
        default=60,
        help="Seconds for the whole probe, build included (default 60); then it stops the "
        "program and fails with TIMEOUT",
    )
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
    candidate = "Probe a candidate fix (C, C++, Rust, Java): build with this edit, never "
    sub.add_argument(
        "--old",
        action="append",
        help=candidate + "touching your files. Exact text to replace once in the --at file; "
        "repeat --old/--new for several edits",
    )
    edit_options(sub)
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
    start.add_argument(
        "--arg",
        action="append",
        default=[],
        help="Program argument (repeatable), or put them after `--`",
    )
    start.add_argument(
        "--hit",
        type=_positive_count,
        help="Run on to the Nth stop at the breakpoint (state lines show `hit N`)",
    )
    start.add_argument(
        "--max-pause",
        type=float,
        help=f"Auto-resume a stop after this many seconds (0: never; default "
        f"{ATTACH_MAX_PAUSE} for attached JVMs, never otherwise)",
    )
    for name, about in (
        ("list", "Names of this user's sessions"),
        ("wait", "Wait up to --timeout for the program to stop or exit"),
        ("inspect", "Stack and locals at the current stop"),
        ("get", "Read selectors (a, a.b, a[0], a['key']); lists one level of children"),
        (
            "set",
            "Change one root local (in Java also a field or array element) to a literal "
            "(name=value) at the current stop",
        ),
        ("continue", "Resume to the next breakpoint stop, --to LINE, or --to-exit"),
        ("step", "Run to the next line (--into a call, --out of the function)"),
        ("report", "State, program output and the session's operation count"),
        ("close", "End the session and stop its local worker"),
    ):
        common(actions.add_parser(name, help=about), target=False)
    for action in actions.choices.values():
        action.add_argument("--name", default="default", help="Session name")
        action.add_argument(
            "--timeout",
            type=float,
            default=30,
            help="Seconds this command waits (default 30). A program still "
            "running is left running: `session wait` again",
        )
    actions.choices["get"].add_argument("selectors", nargs="+")
    actions.choices["set"].add_argument(
        "assignment", help="name=value (Java also current.end=10, xs[0]=1)"
    )
    actions.choices["close"].add_argument(
        "--forget",
        action="store_true",
        help="Drop the local record without contacting the worker (worker gone for good)",
    )
    actions.choices["continue"].add_argument(
        "--to-exit", action="store_true", help="Clear breakpoints and run the program to exit"
    )
    actions.choices["continue"].add_argument(
        "--count",
        type=_positive_count,
        default=1,
        help="Continue N times: the Nth stop from here (default 1)",
    )
    actions.choices["continue"].add_argument(
        "--to",
        type=int,
        metavar="LINE",
        help="Also stop at LINE of the session's file, once (removed at the next stop)",
    )
    stepping = actions.choices["step"].add_mutually_exclusive_group()
    stepping.add_argument("--into", action="store_true", help="Step into the call on this line")
    stepping.add_argument("--out", action="store_true", help="Run until this function returns")
    sub = commands.add_parser("verify", help="Fresh build + independent checker for one edit")
    common(sub)
    sub.add_argument("--at", help="Program path (for multi-file targets)")
    sub.add_argument("--check", required=True, help="Trusted checker script")
    sub.add_argument(
        "--old",
        action="append",
        help="Exact source text to replace (once); repeat --old/--new for several edits, "
        "each quoting the original file",
    )
    edit_options(sub)
    sub.add_argument(
        "--keep",
        action="store_true",
        help="Keep the edited candidate file and its build evidence (default: removed); "
        "build artifacts are not kept",
    )
    sub.add_argument(
        "--details",
        action="store_true",
        help="JSON: include the checker's full descriptor and build manifest",
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
    if args.command == "probe" or getattr(args, "action", None) == "start":
        chosen = probe_parser if args.command == "probe" else start
        args._options = set(chosen._option_string_actions)
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
