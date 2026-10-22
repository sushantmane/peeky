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
from dataclasses import asdict
from pathlib import Path

from peeky.adapters import COPY_NOTE, PYDEVD_GROUPS
from peeky.client import Client
from peeky.errors import PROTOCOL_VERSION
from peeky.identifiers import is_name, look_alike, reachable
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
    parse_set_at,
    probe_exit_code,
    run_probe,
    split_selectors,
)
from peeky.textlines import split_lines

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
    program = _confined(root, program)
    if language == "python" and program:
        # A Python candidate runs a copy whose files keep their stored names, and the
        # debugger reports those: `APP.py` typed for `app.py` must not stop elsewhere.
        program = _stored_name(root, program)
    return root, program, line, language, data


def _stored_name(root, program):
    """``program`` (relative to ``root``) as the file system stores it, where it was
    typed in another case or Unicode form on a volume that ignores the difference."""
    import unicodedata

    def key(name):
        return unicodedata.normalize("NFC", name).casefold()

    current, parts = Path(root), []
    for part in Path(program).parts:
        try:
            names = [name for name in os.listdir(current) if key(name) == key(part)]
        except OSError:
            return program
        if (
            part not in names
            and len(names) == 1
            and os.path.samefile(current / names[0], current / part)
        ):
            part = names[0]
        parts.append(part)
        current = current / part
    return Path(*parts).as_posix()


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
        from peeky.rust_manifest import cargo_layout

        # The targets Cargo builds: a [[bin]] or src/main.rs, a [[test]] or tests/*.rs.
        binary, tests = cargo_layout(crate)
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
            "[--set name=value] [--set-hit N] [--set-at LINE@HIT name=value] [--hits N] [--json]",
            "session": f"peeky session start {target} --at {program}:<line>{run} [--hit N]; then "
            "peeky session inspect | get <a.b> | set <name>=<value> | step [--into|--out] | "
            "continue [--count N|--to LINE|--to-exit] | "
            "report | close",
            "verify": f"peeky verify {target}{at} --check <checker.py> "
            "--old <text> --new <text> (or --old-file/--new-file; repeat the pair for several "
            "edits, each quoting the original file); --baseline instead checks the unedited "
            "program, which the checker should fail",
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
            "evidence": 'every probe not refused before launch is saved with a run_id: peeky sql "SELECT hit, selector, '
            'value FROM last_run"; peeky queries [name] (canned, no SQL); peeky sql --schema',
        },
        "selectors": "dotted paths, indexes and string keys, e.g. request.page, args[0], "
        "order['id']; never expressions",
        "interventions": "root locals (the stopped function's own variables); in Java, Rust, "
        "C and C++ also scalar fields and array elements (current.end, this.count, xs[0]; in "
        "Rust, C and C++ only of a local the frame holds itself, never through a pointer); "
        "evidence about one run, never a verified fix",
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
                    name: layout["kind"]
                    for name, layout in sorted(layouts.items())
                    if layout["kind"] != "unread"
                }
        finally:
            runtime.close()
        manifest["mutable_types"] = {
            "rust": "integer root locals (i16 to i64, u16 to u64, isize, usize) the program "
            "keeps in memory, and String root locals to a text in double quotes within their "
            "capacity (--set 'name=\"apple\"'); `inspect` marks them [settable]. Also such "
            "integer fields and elements (s.end, t.__0, xs[2]) of a struct, tuple or array "
            "local the MIR proves initialized, never through a reference, Box or enum; rows "
            "mark them [settable] too",
            "c": "integer root locals (16 to 64 bits, incl. size_t/int64_t), and char array "
            "locals to a text in double quotes that fits (--set 'name=\"apple\"'); `inspect` "
            "marks them [settable]. Also such integer fields and elements (s.end, s.in.lo, "
            "xs[2]) of a struct or array local, never through a pointer; rows mark them "
            "[settable] too",
            "cpp": "integer root locals (16 to 64 bits, incl. size_t/int64_t), and char array "
            "and std::string locals to a text in double quotes that fits (--set "
            "'name=\"apple\"'); `inspect` marks them [settable]. Also such integer fields and "
            "elements (s.end, xs[2]) of a struct, class or array local, never through a "
            "pointer or reference, nor in std types; rows mark them [settable] too",
            "java": "root locals, fields (not final or static) and array elements (current.end, "
            "xs[0]) of type byte, short, int, long, char, boolean, float or double, and "
            "String ones; a char takes a char in single quotes ('+') or a code point (43), a "
            "String a text in double quotes (--set 'name=\"apple\"'); never a boxed value "
            "(Integer), another object or an ArrayList's elements, nor a field a JDK class "
            "declares (a String's hash, an ArrayList's size) or an element of an array one "
            "holds. Field, char and String changes need an adapter bundle built by this "
            "version of scripts/setup_java_adapter.py. Rows mark what can change [settable]",
            "java-attach": "root locals, fields and array elements of type byte, short, int, "
            "long, char, boolean, float or double, and String ones, as for launched Java, "
            "never a JDK class's fields (attached JVM; no edit verification)",
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
        from peeky.rust_symbols import (
            CONTAINER_KINDS,
            QUALIFIED_CONTAINERS,
            REFERENCE_PARAMETERS,
            composite_container,
        )

        enums = [name for name, kind in views.items() if kind == "option"]
        containers = [
            declared
            for declared, (_, name, _) in REFERENCE_PARAMETERS.items()
            if views.get(name) in CONTAINER_KINDS
        ]
        composites = [
            name
            for name, kind in views.items()
            if kind != "unqualified" and composite_container(name)
        ]
        opaque = [
            name
            for name, kind in views.items()
            if kind == "unqualified"
            and name not in QUALIFIED_CONTAINERS
            and not composite_container(name)
        ]
        from peeky.rust import rust_spelling

        shown = ", ".join(dict.fromkeys(map(rust_spelling, enums)))
        lines.append(f"qualified enum views: {shown or 'none'}")
        if containers:
            lines.append(
                "read from memory (debuggers with readMemory): reference parameters, owned "
                "Vec<T>/String locals where the build's MIR proves them initialized, and such "
                "fields of either (self.values): "
                + ", ".join(dict.fromkeys(map(rust_spelling, containers)))
            )
        if composites:
            lines.append(
                "tuple and struct elements read the same way: "
                + ", ".join(composites[:12])
                + (f", and {len(composites) - 12} more" if len(composites) > 12 else "")
            )
        lines.append(f"other enum types (shown opaque, not decoded): {len(opaque)}")
    if reason := manifest.get("scopes_unavailable"):
        lines.append(f"function names: unavailable ({reason})")
    spell = str
    if manifest["language"] == "rust":
        # As the program spells them (#183): `Vec<u32>`, `String`.
        from peeky.rust import rust_spelling as spell
    for scope in manifest.get("scopes", ()):
        if scope.get("omitted"):
            lines.append(f"... {scope['omitted']} more scopes not listed (first 300 shown)")
            continue
        span = f"{scope['lines'][0]}-{scope['lines'][1]}" if scope["lines"] else ""
        if scope.get("source"):
            where = f" ({scope['source']}:{span})"
        else:
            where = f" (lines {span})" if span else ""
        types, spans = scope.get("types", {}), scope.get("spans", {})
        shown = []
        for name in scope["names"]:
            # A local declared only in nested blocks is visible on their lines alone.
            parts = [spell(types[name])] if name in types else []
            if name in spans:
                listed = [f"{a}-{b}" if b > a else str(a) for a, b in spans[name]]
                single = len(listed) == 1 and "-" not in listed[0]
                parts.append(("line " if single else "lines ") + ", ".join(listed))
            shown.append(f"{name} ({', '.join(parts)})" if parts else name)
        lines.append(f"names in {scope['scope']}{where}: {', '.join(shown) or '-'}")
    from peeky.probe import optimization_level

    if level := optimization_level(manifest.get("target_identity")):
        lines.append(
            f"warning: optimized build ({level}); locals may be unavailable, stale or wrong. "
            "Prefer an unoptimized build for debugging."
        )
    mutable = manifest["mutable_types"]
    if "root locals" in mutable and "fields" not in mutable:
        mutable += " (root locals: the stopped function's own variables, not fields)"
    # The fields and elements of struct and array locals `set` may change, per function
    # (seventh evaluation, finding 16; #183: the paths, `board.entries`, not `board`).
    with_parts = [
        f"{scope['scope']}: {', '.join(_shown_paths(paths) for paths in scope['settable_parts'].values())}"
        for scope in manifest.get("scopes", ())
        if scope.get("settable_parts")
    ]
    if with_parts and not manifest.get("optimized"):
        mutable += (
            ". Such fields and elements (`[*]`: any element): "
            + "; ".join(with_parts[:12])
            + (f"; and {len(with_parts) - 12} more functions" if len(with_parts) > 12 else "")
        )
    # Locals no stop can change, per function and why (#138); `probe --set` refuses them
    # before the program runs.
    whys = {
        "register": "kept in a register (a candidate edit with `std::hint::black_box(&mut x)` "
        "keeps one in memory, COOKBOOK 6.6)",
        "another binding": "another binding of the name, on the line given, is kept in a "
        "register (a candidate edit black-boxing that `let` keeps every one in memory, "
        "COOKBOOK 6.6)",
        "inlined": "a local of a function inlined where it is called (`#[inline(always)]`: no "
        "edit to it helps; change the value in the caller)",
        "async state": "held in an async body's state across an `.await` (change what it is "
        "computed from, or copy it into a local after the `.await`)",
    }
    for why, label in whys.items():
        never = []
        for scope in manifest.get("scopes", ()):
            names = []
            for name, given in (scope.get("never_settable") or {}).items():
                if given == why:
                    names.append(name)
                elif why == "another binding" and (
                    blocking := re.fullmatch(
                        r"another binding in a register (\(line [^)]+\))", str(given)
                    )
                ):
                    # `another binding in a register (line 4)`: `x (line 4)`.
                    names.append(f"{name} {blocking[1]}")
            if names:
                never.append(f"{scope['scope']}: {', '.join(names)}")
        if never:
            mutable += (
                f". Never settable, {label}: "
                + "; ".join(never[:12])
                + (f"; and {len(never) - 12} more functions" if len(never) > 12 else "")
            )
    if manifest["language"] == "rust" and not manifest.get("optimized"):
        # #183: where nothing can change, the way around it (an edit that keeps a local in
        # memory, or changes what the program computes).
        settable = ("i16", "i32", "i64", "u16", "u32", "u64", "isize", "usize")
        settable += ("alloc::string::String",)
        nothing = [
            scope["scope"]
            for scope in manifest.get("scopes", ())
            if scope.get("names")
            and not scope.get("settable_parts")
            and not any(
                (scope.get("types") or {}).get(name) in settable
                and name not in (scope.get("never_settable") or {})
                for name in scope["names"]
            )
        ]
        mutable += (
            ". A Vec's, String's or Box's contents (`v[0]`, `self.values[1]`) and what a "
            "reference points at are never settable"
            + (
                "; nor is anything in "
                + ", ".join(nothing[:12])
                + (f" and {len(nothing) - 12} more functions" if len(nothing) > 12 else "")
                if nothing
                else ""
            )
            + ": change those with a candidate edit, the program's own code (`--old`/`--new`; "
            "`std::hint::black_box(&mut x)` after a `let` keeps a local in memory: COOKBOOK 6.6)"
        )
    if manifest["language"] in ("c", "cpp"):
        # Two blocks declaring one name with different types (#158): a change follows
        # the one in scope at the stop.
        twice = [
            f"{scope['scope']}: {', '.join(names)}"
            for scope in manifest.get("scopes", ())
            if (
                names := [
                    name for name, kind in (scope.get("types") or {}).items() if " or " in kind
                ]
            )
        ]
        if twice:
            mutable += (
                ". Locals of one name with different types ("
                + "; ".join(twice[:12])
                + (f"; and {len(twice) - 12} more functions" if len(twice) > 12 else "")
                + "): a plain name means the declaration the code names at the stop, and a "
                "change follows it; where Peeky cannot tell which, it refuses, and the "
                "debugger lists them as `name @ file:line`, which select"
            )
    lines.append(f"mutable: {mutable}")
    lines.extend(f"{name}: {usage}" for name, usage in manifest["commands"].items())
    lines.append(f"timing: {manifest['timing']['advice']}")
    return "\n".join(lines)


def _shown_paths(paths, limit=8):
    """A local's settable paths for `describe`'s line: the first ``limit``, then how
    many more (#192 review: never cut silently)."""
    paths = list(paths)
    marked = paths and str(paths[-1]).startswith("… and ")
    more = paths.pop() if marked else None
    shown = paths[:limit]
    rest = len(paths) - len(shown)
    if more is not None:
        counted = re.fullmatch(r"… and ([0-9]+) more", more)
        if not counted:
            # The walk stopped before it found them all.
            return ", ".join(shown) + ", … and more"
        rest += int(counted[1])
    return ", ".join(shown) + (f", … and {rest} more" if rest else "")


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
    from peeky.client import WorkerUnreachable
    from peeky.errors import PeekyError

    try:
        root, request, edit, original, language, adapter = _probe_request(args)
        if args.worker is not None:
            client, expected = _remote_probe(args, root, request)
    except Exception as exc:
        # Refused before the probe request was sent (here, or in the worker's version
        # handshake; #190): the debugger's launch or attach request was not sent.
        exc.before_launch = True
        raise
    if args.worker is not None:
        try:
            result = _probe_remote(args, root, request, client, expected)
        except (WorkerUnreachable, PeekyError) as exc:
            # The request never reached the worker, or the worker refused it as it
            # answers a refusal. Anything else once it is sent (a broken connection, an
            # answer that is not the worker's) leaves its outcome unknown.
            if isinstance(exc, WorkerUnreachable) or getattr(exc, "from_worker", False):
                exc.before_launch = True
            raise
    else:
        result = run_probe(
            root,
            request,
            adapter=adapter,
            progress=_progress(args),
            launch_budget=args.launch_budget,
            launch_marker=args.launch_marker,
        )
    return _probe_result(args, root, result, edit, original, request.program, language)


def _probe_request(args):
    """(root, the Probe, edit, original text, language, the local adapter or None) for
    `peeky probe`'s arguments."""
    if args.label is not None and len(args.label) > 200:
        raise ValueError("--label is at most 200 characters")
    root, program, line, language, data = resolve(args.target, args.at, args.config)
    if line is None:
        raise ValueError("probe requires --at <path>:<line>")
    edit, original = _probe_edit(args, root, program, line, language)
    set_at = _set_at(args, root, program)
    _member_sets([*args.set, *(f"{name}={value}" for _, _, name, value in set_at)], language)
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
        set_at=set_at,
    )
    adapter = adapter_for(language, data) if args.worker is None else None
    return root, request, edit, original, language, adapter


def _progress(args):
    if args.quiet:
        return None
    return lambda message: print(f"peeky: {message}", file=sys.stderr, flush=True)


def _probe_result(args, root, result, edit, original, program, language):
    if edit:
        _bind_candidate(result, original, edit, program)
    if misplaced := _misplaced_options(args):
        if (result.get("error") or {}).get("code") == "NO_STOP":
            result["error"]["hint"] = misplaced + "."
        else:
            result.setdefault("warnings", []).append(misplaced)
    if language == "python" and "inputs_sha256" not in result and result.get("source_sha256"):
        # A Python program reads the root's modules as they are on disk (no build).
        if (inputs := _python_inputs(root)) is not None:
            result["inputs_sha256"] = inputs
    if not args.no_save and not result.get("refused_before_launch"):
        # A probe refused before it ran (#138) leaves no run to compare.
        _keep_evidence(args, result, root, language)
    _emit(result, args.json, lambda shown: _probe_text(shown, quiet=args.quiet))
    # 2 for a probe refused before it ran (finding 14), 4 for a change not made (#151).
    return probe_exit_code(result)


MAX_PYTHON_INPUTS = 4096
MAX_PYTHON_BYTES = 16 * 1024 * 1024


def _python_inputs(root):
    """A digest of the `.py` files under ``root`` (outside hidden, cache and environment
    directories), the sources a Python program may import, or None where there are too
    many or one cannot be read."""
    found, total = [], 0
    skipped = {"__pycache__", "node_modules", "site-packages", "venv", "env"}
    for directory, names, files in os.walk(root):
        names[:] = sorted(n for n in names if not n.startswith(".") and n not in skipped)
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = Path(directory) / name
            try:
                data = path.read_bytes()
            except OSError:
                return None
            total += len(data)
            if len(found) >= MAX_PYTHON_INPUTS or total > MAX_PYTHON_BYTES:
                return None
            found.append((path.relative_to(root).as_posix(), hashlib.sha256(data).hexdigest()))
    return hashlib.sha256(json.dumps(found).encode()).hexdigest() if found else None


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


def _set_at(args, root, program):
    """`--set-at [<path>:]<line>[@<hit>] name=value` as Probe.set_at: in the --at file,
    where the probe's breakpoints are (#137)."""
    points = []
    for where, assignment in getattr(args, "set_at", None) or ():
        path, line, hit, name, value = parse_set_at(where, assignment)
        if path is not None and _confined(root, path) != program:
            raise ValueError(
                f"--set-at {where}: a probe stops in one source file, the --at one "
                f"({program}); give a line of it (--set-at {line}@{hit} {assignment})"
            )
        points.append((line, hit, name, value))
    return tuple(points)


def _member_sets(sets, language):
    """Refuse a field or element --set before anything is built where no language rule
    allows one (Python)."""
    from peeky.errors import Refused

    names = [text.split("=", 1)[0].strip() for text in sets or ()]
    fields = [name for name in names if name and len(parse_selector(name)) > 1]
    if fields and language not in ("java", "java-attach", "c", "cpp", "rust"):
        raise Refused(
            "MUTATION_REJECTED",
            f"--set {fields[0]} changes a field or element, which only Java, Rust, C and C++ "
            f"targets allow; in {language} only the stopped function's own locals change. To "
            "test a value there, add a local with a candidate edit and set it in the same "
            "probe (COOKBOOK 6.6: `--old/--new` with `--set`), or edit the source and run "
            "verify",
        )


def _probe_edit(args, root, program, line, language):
    """The edit (one (old, new) pair, or a tuple of them) and the original text for
    `probe --old/--new`, else (None, None)."""
    if not getattr(args, "_edits", None) and all(
        not value for value in (args.old, args.new, args.old_file, args.new_file)
    ):
        return None, None
    if language == "java-attach":
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
    if result.get("candidate"):
        # A plain `queries diff` compares with the run before, whatever it was: name the
        # run that is this probe without the edit (seventh evaluation, finding 12).
        try:
            # The unedited source as the run read it (the candidate's own is the edit's).
            before = evidence.baseline(path, run_id, result.get("source_sha256"))
            # The run to compare with makes no change: a candidate's --set (perhaps of a
            # variable only the edit declares) is left out too (eighth evaluation, L21).
            probe = result.get("probe") or {}
            without = (
                " and without its --set/--set-at" if probe.get("set") or probe.get("set_at") else ""
            )
            apart = evidence.stops_apart(path, before, run_id) if before is not None else None
        except Exception:
            return
        result["evidence"]["compare"] = (
            {"run": before, "command": f"peeky queries diff --param a={before} --param b={run_id}"}
            if before is not None and apart is None
            # Its hits are at other breakpoint locations of the line (#157): the edit
            # changed how often the line stops per pass, or it predates stop labels.
            else {
                "run": None,
                "hint": f"run {before} is this probe without the edit, but at hit "
                f"{apart['hit']} it was at {evidence.stop_text(apart['stop_a'])} of the line "
                f"and this run at {evidence.stop_text(apart['stop_b'])}, so hits do not pair "
                "by number: pair them by a value, `peeky queries join --param "
                f"a={before} --param b={run_id} --param key=<selector>`",
            }
            if before is not None
            else {
                "run": None,
                "hint": "no saved run is this probe without the edit (the same --at, "
                "--capture, program arguments and build, of the sources as they are now, with "
                f"no --set or --set-at): run it without --old/--new{without}, then `peeky "
                "queries diff`"
                if result.get("inputs_sha256")
                else "Peeky cannot tell which saved run read the same sources as this one: "
                f"run this probe without --old/--new{without}, then `peeky queries diff`",
            }
        )


def _first_time(name):
    """True the first time ``name`` is asked for in this state directory (then never
    again): a note worth reading once, not on every command."""
    try:
        marker = _private_dir("notes") / name
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except (OSError, ValueError):
        # No state to remember it in: said every time rather than never.
        return True
    os.close(descriptor)
    return True


def _probe_text(result, quiet=False):
    from peeky.probe import _short_note

    # The note on short frame names and instance labels, once per state directory and
    # never with --quiet (seventh evaluation, finding 8): `peeky sql --help`, `--schema`
    # and query tables say it too.
    noted = not result.get("refused_before_launch") and _short_note(result) is not None
    short_names = not quiet and noted and _first_time("short-names")
    # A Python candidate's note on its copied root (eighth evaluation, L6): once per
    # root and state directory, never with --quiet; `--json` always has it.
    warnings = [
        warning
        for warning in result.get("warnings", ())
        if not warning.startswith(COPY_NOTE)
        or (
            not quiet
            and _first_time("python-copy-" + hashlib.sha256(warning.encode()).hexdigest()[:16])
        )
    ]
    if warnings != list(result.get("warnings", ())):
        result = {**result, "warnings": warnings}
    text = _table(result, short_names=short_names)
    if result.get("refused_before_launch"):
        return text
    if candidate := result.get("candidate"):
        ran = result.get("hits") or result.get("rows") or result.get("output")
        python = str((result.get("probe") or {}).get("program") or "").endswith(".py")
        source = "a copy of your tree" if python else "a fresh build"
        text += (
            f"\ncandidate: values are from {source} with this edit"
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
        if compare := saved.get("compare"):
            text += (
                f"\ncompare: {compare['command']} (run {compare['run']}: this probe without "
                "the edit)"
                if compare.get("run") is not None
                else f"\ncompare: {compare['hint']}"
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
                from peeky.errors import Refused

                # Still a row's error, and still its code (an intervention reports it).
                raise Refused(exc.error_code, exc.payload.get("message", str(exc))) from exc
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
    # Where it stopped, for messages about this stop (`session get`).
    record["location"], record["function"] = state.get("location"), state.get("function")
    _save(record)
    state.pop("event", None)
    return state


_OPTIMIZED_NOTE = (
    "warning: optimized build ({}): shown values can be stale or wrong and locals cannot be "
    "changed; prefer an unoptimized build"
)


def _named(text, name):
    """``text`` with `--name NAME` added to each session command it names (`peeky session
    wait`, `session continue --to 17`), for a session other than `default`: pasted, a
    command acts on the session it came from (eighth evaluation, L1)."""
    if not name or name == "default":
        return text
    return re.sub(
        r"`((?:peeky )?session [a-z]+(?: [^`]*)?)`",
        lambda m: f"`{m[1]}`" if "--name" in m[1] else f"`{m[1]} --name {shlex.quote(name)}`",
        text,
    )


def _state_text(state, name="default"):
    where = f" at {state['location']}" if state.get("location") else ""
    if where and state.get("function"):
        from peeky.names import display_function

        where += f" in {display_function(state['function'])}"
    if where and type(state.get("hit")) is int:
        where += f", hit {state['hit']}"
    if where and type(state.get("place")) is int and type(state.get("places")) is int:
        # One of the line's several breakpoint locations in this function (#157).
        where += f", stop {state['place']} of {state['places']}"
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
    if reason == "interrupted":
        where += (
            " [the step was interrupted here: another thread stopped at the same time; "
            "`session continue` serves its stop]"
        )
    elif reason and reason not in ("breakpoint", "step"):
        where += f" [reason: {reason}; not auto-resumed, inspect it or `session report`]"
    text = f"state: {state['state']}{where}{_pause_text(state)}{hint}"
    if state.get("optimized"):
        text += f"\n{_OPTIMIZED_NOTE.format(state['optimized'])}"
    if state.get("exit_inferred"):
        text += "\nnote: the debugger never reported the exit; Peeky inferred it (no threads left)"
    if unreported := state.get("unreported_stop"):
        text += (
            f"\nnote: thread {unreported.get('thread_name')!r} was seen paused at "
            f"{unreported.get('breakpoint')}, but the debugger never reported that stop "
            "(java-debug, issue #27); `peeky session report --json` has its debugger_events"
        )
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
    return _named(text, name)


def _stop(record):
    if record.get("state") != "stopped" or not record.get("stop_id"):
        raise ValueError(
            _named("The program is not stopped; run `peeky session wait`", record.get("name"))
        )
    return {"session_id": record["session_id"], "stop_id": record["stop_id"]}


def _frame_text(snapshot):
    """`frame 1: tags (app.py:12)`: the caller frame a read came from."""
    from peeky.names import display_function

    index = snapshot.get("frame", 0)
    frame = snapshot["frames"][index] if len(snapshot["frames"]) > index else {}
    where = snapshot.get("location") or f"{frame.get('source', {}).get('path')}:{frame.get('line')}"
    return f"frame {index}: {display_function(snapshot.get('function'))} ({where})"


def _variables_text(snapshot, name="default"):
    from peeky.names import display_function

    index = snapshot.get("frame", 0)
    frame = snapshot["frames"][0] if snapshot["frames"] else {}
    # Where the program stopped; a caller frame's own location is on the next line.
    stopped = snapshot.get("innermost") or {"function": frame.get("name")} if index else snapshot
    where = stopped.get("location") or f"{frame.get('source', {}).get('path')}:{frame.get('line')}"
    if function := stopped.get("function"):
        where += f" in {display_function(function)}"
    if type(snapshot.get("hit")) is int:
        where += f", hit {snapshot['hit']}"
    if type(snapshot.get("place")) is int and type(snapshot.get("places")) is int:
        where += f", stop {snapshot['place']} of {snapshot['places']}"
    lines = [f"stopped at {where}" + _pause_text(snapshot)]
    if index:
        lines.append(_frame_text(snapshot))
    # Selectors read the frame that was inspected: say so in the commands shown.
    reads = (f" --name {name}" if name != "default" else "") + (
        f" --frame {index}" if index else ""
    )
    for scope in snapshot["scopes"]:
        lines.append(
            f"[{scope['name']}]" + (" (module globals, read-only)" if scope.get("globals") else "")
        )
        alike = look_alike([variable["name"] for variable in scope["variables"]])
        for variable in scope["variables"]:
            more = (
                f" (children: peeky session get {variable['name']}{reads})"
                if variable.get("variablesReference") and is_name(variable["name"])
                else ""
            )
            if more and variable.get("whole"):
                # A Java String or boxed number (#204): a value, not its class's fields.
                more = (
                    ""
                    if str(variable.get("value")).startswith('"')
                    else more.replace("(children:", "(value:", 1)
                )
            if variable.get("unavailable"):
                more = f" (no readable value: {variable.get('reason')})"
            elif variable["name"] in PYDEVD_GROUPS and not variable.get("type"):
                lines.append(f"  ({variable['name']}: debugpy's group; select its members by name)")
                continue
            elif not reachable(variable):
                # LLDB's `(Return Value)` after `step --out`, debugpy's `special variables`.
                more = " [not selectable]"
            if variable["name"] in alike:
                # Two rows that look the same: which spelling each is (#195).
                more += (
                    f" [written {ascii(variable['name'])}: another name here differs from it "
                    "only by Unicode normalization]"
                )
            view = f"  [{variable['view']}]" if variable.get("view") else ""
            if variable.get("stale"):
                # May be moved or dropped: its fields are what it left behind (#108).
                view += "  [stale]"
            if variable.get("settable"):
                view += "  [settable]"
            elif variable.get("not_settable"):
                # No stop of this build can change it (#138).
                view += f"  [not settable: {variable['not_settable']}]"
            if variable.get("identity"):
                view += f"  [id {variable['identity']}]"
            lines.append(
                f"  {variable['name']}: {variable.get('type', '')} = "
                f"{'<no value>' if variable.get('unavailable') else variable['value']}{view}{more}"
            )
            if variable.get("note") and not variable.get("unavailable"):
                # A value shown though Peeky cannot vouch for it (a line a loop returns
                # to; initialization not judged).
                lines.append(f"    note: {variable['note']}")
        if scope.get("truncated"):
            lines.append(
                f"  ... and {scope['total'] - len(scope['variables'])} more, not listed (at "
                "most 50 per scope): `session get NAME` reads one by name"
            )
    if returned := snapshot.get("returned"):
        # What the function the step left returned (#155), apart from the locals.
        lines.append("[Returned]")
        lines.append(f"  {_returned_text(returned)}  (session get return)")
    if snapshot.get("library_frame"):
        lines.append(f"note: {snapshot['library_frame']}")
    if str(snapshot.get("globals", "listed")) != "listed":
        lines.append(f"note: module globals {str(snapshot['globals'])[:300]}")
    if snapshot.get("optimized"):
        lines.append(_OPTIMIZED_NOTE.format(snapshot["optimized"]))
    if snapshot.get("mutation_blocked"):
        lines.append(f"note: {snapshot['mutation_blocked']}")
    if snapshot.get("unsettable"):
        lines.append(
            f"note: nothing here is [settable]: {snapshot['unsettable']}"
            + (
                ""
                if snapshot.get("unsettable_everywhere")
                else ". Values change at a statement's first instruction: `session continue "
                "--to LINE`, or a `session step` that lands on a statement's first line"
            )
        )
    return "\n".join(lines)


def _returned_text(returned):
    """`before(Task const&, Task const&) returned bool = false`, or why it is withheld."""
    from peeky.names import display_function

    who = display_function(returned.get("function")) or "the function"
    if returned.get("unavailable"):
        return f"{who} returned: <withheld: {returned.get('reason')}>"
    return f"{who} returned: {returned.get('type') or ''} = {returned.get('value')}"


def _require_stepping(record, client):
    """Remote workers built before `step` silently ignore `continue --to`: refuse."""
    if not record.get("remote"):
        return
    if "step" not in client.call("version").get("operations", ()):
        raise ValueError(
            "This remote worker predates `session step` and `continue --to`; update Peeky "
            "on the worker host"
        )


def _stop_place(record, row):
    """`main at sched.cpp:30`: the function and place of the session's stop, the file
    relative to the target's root where it is under it."""
    from peeky.names import display_function

    path, _, line = str(record.get("location") or "").rpartition(":")
    if not path or not line.isdigit():
        path, line = None, row.get("line")
    elif os.path.isabs(path):
        try:
            path = Path(path).relative_to(Path(record.get("root") or "/")).as_posix()
        except ValueError:
            path = Path(path).name
    where = f"{path}:{line}" if path else f"line {line}" if line is not None else "an unknown line"
    function = record.get("function") or row.get("function")
    return f"{display_function(function)} at {where}" if function else where


def _read_frame(row):
    """`frame 1: tags (app.py:12)`: which caller frame `get --frame` read."""
    from peeky.names import display_function

    return (
        f"frame {row.get('frame')}: {display_function(row.get('function'))} ({row.get('location')})"
    )


def _get_lines(row, session="default"):
    """`session get` text: the value, then one level of children with their selectors."""
    if "value" not in row and not row.get("unavailable"):
        # Nothing was read: there is no type or value to show, only why.
        lines = [f"{row['selector']}: <{row.get('error', '')}>"]
    else:
        value = row.get("value", "<" + row.get("error", "") + ">")
        lines = [
            f"{row['selector']}: {row.get('type') or ''} = {value}"
            + (f"  [id {row['identity']}]" if row.get("identity") else "")
            + ("  [module global]" if row.get("scope") == "globals" else "")
            + ("  [stale]" if row.get("stale") else "")
            + ("  [settable]" if row.get("settable") else "")
            + (f"  [not settable: {row['not_settable']}]" if row.get("not_settable") else "")
        ]
    for child in row.get("children", []):
        name = child.get("selector") or f"{child['name']} [not selectable]"
        more = (
            f" (children: peeky session get {shlex.quote(child['selector'])}"
            + (f" --name {shlex.quote(session)}" if session != "default" else "")
            + (f" --frame {row['frame']})" if row.get("frame") else ")")
            if child.get("expandable") and child.get("selector")
            else ""
        )
        value = (
            f"<no readable value: {child.get('reason')}>"
            if child.get("unavailable")
            else child.get("value")
        )
        shown = f"  [id {child['identity']}]" if child.get("identity") else ""
        if child.get("view") == "unproven":
            shown += "  [unproven]"
        if child.get("stale"):
            shown += "  [stale]"
        if child.get("settable"):
            shown += "  [settable]"
        elif child.get("not_settable"):
            shown += f"  [not settable: {child['not_settable']}]"
        if child.get("note") and not child.get("unavailable"):
            # Read through a local shown with a note, given below (#148).
            shown += "  [note]"
        lines.append(f"  {name}: {child.get('type') or ''} = {value}{shown}{more}")
    if row.get("children_truncated"):
        lines.append("  ... more children; select one by name or index")
    if row.get("children_grouped"):
        lines.append(
            f"  (debugpy groups more members under {', '.join(row['children_grouped'])}: "
            f"select one by name, as {row['selector']}.__class__)"
        )
    if row.get("children_error"):
        lines.append(f"  <children: {row['children_error']}>")
    if row.get("note"):
        lines.append(f"  note: {row['note']}")
    raw = {}
    for child in row.get("children", []):
        if isinstance(child.get("raw_reason"), str):
            raw.setdefault(child["raw_reason"], []).append(child.get("selector") or child["name"])
    for reason, names in raw.items():
        # Why a Rust Vec or String field shows LLDB's summary, not its contents (#156).
        verb = "shows" if len(names) == 1 else "show"
        lines.append(f"  note: {', '.join(names)} {verb} LLDB's summary, not contents: {reason}")
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
        _emit(state, args.json, lambda shown: _state_text(shown, args.name))
    elif args.action == "inspect":
        if args.globals and record.get("language") not in (None, "python"):
            raise ValueError("--globals lists a Python module's globals; this is not Python")
        snapshot = client.call(
            "inspect",
            **_stop(record),
            **({"frame": args.frame} if args.frame else {}),
            # Listing a global runs its repr in the program, so only on request.
            **({"globals": True} if args.globals else {}),
        )
        if snapshot.get("frame", 0) != args.frame:
            # A worker that predates `frame` ignores it and shows the innermost frame.
            raise ValueError(
                "This worker cannot read a caller's frame (it predates --frame); update "
                "Peeky on the worker host"
            )
        if args.globals and "globals" not in snapshot:
            raise ValueError(
                "This worker cannot list module globals (it predates --globals); update "
                "Peeky on the worker host"
            )
        if snapshot.get("optimized"):
            # As the text says: a run-level warning, not a note on each row.
            snapshot["warning"] = _OPTIMIZED_NOTE.format(snapshot["optimized"]).removeprefix(
                "warning: "
            )
        _emit(snapshot, args.json, lambda shown: _variables_text(shown, args.name))
    elif args.action == "get":
        from peeky.probe import returns

        rows = _capture(
            _Remote(client),
            _stop(record),
            tuple(returns(selector) for selector in args.selectors),
            0,
            "observed",
            children=True,
            # Launched Java values show their fields (an attached JVM's do not).
            preview=record.get("language") == "java",
            # C and C++ only: LLDB shows Rust references and Boxes as pointers too.
            pointers=record.get("language") in ("c", "cpp"),
            frame=args.frame,
        )
        # A session read is not a probe hit: no hit number to explain.
        rows = [{key: v for key, v in row.items() if key != "hit"} for row in rows]
        if level := record.get("optimized"):
            rows = [{**row, "optimized": level} for row in rows]
        unresolved = [row for row in rows if "value" not in row and not row.get("unavailable")]
        place = None
        if unresolved and not args.frame:
            # A name read at the wrong stop (a caller's, a callee's): say which stop this
            # is (seventh evaluation, finding 15). `--frame N` names its frame already.
            place = _stop_place(record, unresolved[0])
            for row in unresolved:
                row.setdefault("location", record.get("location"))
        _emit(
            rows,
            args.json,
            lambda rows: "\n".join(
                [_read_frame(rows[0])] * bool(args.frame and rows)
                + [line for row in rows for line in _get_lines(row, args.name)]
                + ([f"note: this stop is in {place}"] if place else [])
                + ([_OPTIMIZED_NOTE.format(record["optimized"])] if record.get("optimized") else [])
            ),
        )
        if any("error" in row for row in rows):
            # Every row was still printed; the exit status says one selector failed.
            return 1
    elif args.action == "set":
        from peeky.probe import apply_set

        (name, value), stop = _sets([args.assignment])[0], _stop(record)
        if (last := parse_selector(name)[-1]).startswith("[") and last[1:2] in "'\"#":
            from peeky.errors import Refused

            raise Refused(
                "MUTATION_REJECTED",
                f"Intervention target {name!r}: map and dict entries cannot be changed; "
                "set changes a local, or a field or array element (Java, Rust, C, C++)",
            )
        changed = apply_set(_Remote(client), stop, name, value)
        _emit(
            changed,
            args.json,
            lambda c: (
                f"set {c['name']}: {c['before']} -> {c['after']}"
                + (f"\nnote: {c['note']}" if c.get("note") else "")
            ),
        )
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
        if args.to is not None:
            complete = _reached_line(state, args.to)
        finished = 0
        while args.to_exit and state["state"] == "stopped" and state.get("reason") == "step":
            # LLDB completes a step that a breakpoint interrupted before running on.
            finished += 1
            if finished > 3:
                break
            client.call("continue", **_stop(record), to_exit=True)
            state = _wait(record, client, args.timeout)
        _emit(state, args.json, lambda shown: _state_text(shown, args.name))
        if not complete:
            return 3
    elif args.action == "step":
        kind = "in" if args.into else "out" if args.out else "over"
        _require_stepping(record, client)
        client.call("step", **_stop(record), kind=kind)
        state = _wait(record, client, args.timeout)
        if _counted(state) and state.get("reason") == "breakpoint":
            # The code the step ran reached the session's breakpoint first (a recursive call,
            # or the rest of the function on `--out`): the step is over (eighth evaluation, L3).
            state["note"] = (
                f"the session's breakpoint (hit {state['hit']}) stopped the step before it "
                "ended, and the step is over"
            )
        returned = None
        if kind == "out" and state["state"] == "stopped" and record.get("language") != "python":
            # What the function returned, as `session get return` reads it, shown with the
            # stop (eighth evaluation, L2); none where the step did not end at a return.
            from peeky.probe import RETURN

            try:
                (row,) = _capture(
                    _Remote(client), _stop(record), (RETURN,), 0, "observed", children=False
                )
            except (ValueError, KeyError, TypeError):
                row = {}
            if "value" in row or row.get("unavailable"):
                returned = {key: v for key, v in row.items() if key != "hit"}
                state["returned"] = returned
        _emit(
            state,
            args.json,
            lambda shown: "\n".join(
                [
                    _state_text(shown, args.name),
                    *(_get_lines(returned, args.name) if returned else []),
                ]
            ),
        )
    elif args.action == "report":
        report = client.call("report", session_id=record["session_id"])
        _emit(
            report,
            args.json,
            # The worker's request history (`history`, several per command) is JSON only:
            # a bare count of it read as the commands run (sixth evaluation, finding 14).
            lambda r: f"state: {r['state']}\noutput:\n{r['output'].rstrip(chr(10)) or '(none)'}",
        )
    elif args.action == "close":
        from peeky.client import TransportLost, WorkerUnreachable

        keep, closed = False, {}
        try:
            closed = client.call("close", session_id=record["session_id"])
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
        # What became of the program the session started (#143).
        cleanup = closed.get("cleanup") if isinstance(closed, dict) else None
        _emit(
            {"closed": args.name, **({"cleanup": cleanup} if cleanup else {})},
            args.json,
            lambda r: f"closed {r['closed']}" + (f"\ncleanup: {r['cleanup']}" if cleanup else ""),
        )
    return 0


ATTACH_MAX_PAUSE = 10
PROBE_STATUSES = frozenset({"completed", "no-stop", "failed", "timeout"})


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
    protocol = version.get("protocol") if isinstance(version, dict) else None
    if protocol != PROTOCOL_VERSION:
        raise ValueError(f"Worker speaks protocol {protocol}, this client {PROTOCOL_VERSION}")
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


def _remote_probe(args, root, request):
    """(client, the sources' expected hashes) for a remote probe, checked before it is
    sent."""
    client, _ = _remote(args.worker)
    return client, _expected(root, request.program, request.entry)


def _probe_remote(args, root, request, client, expected):
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
        set_at=request.set_at,
    )
    # A probe answer has one of these statuses, or is a probe child's bare error (#190
    # review: any JSON object was taken for one).
    error = result.get("error") if isinstance(result, dict) else None
    bare_error = isinstance(error, dict) and isinstance(error.get("code"), str)
    if not isinstance(result, dict) or (
        result.get("status") not in PROBE_STATUSES
        and not (result.get("status") is None and bare_error)
    ):
        from peeky.client import TransportLost

        raise TransportLost(
            "Connection to the worker broke during 'probe' (its answer is not a probe "
            "result); its outcome is unknown"
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
        (
            "probe",
            {
                **asdict(request),
                "set": [list(change) for change in request.set],
                "set_at": [list(point) for point in request.set_at],
            },
        ),
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
        lambda s: (
            f"session {s['session']} ({s['language']}, remote): " + _state_text(s, s["session"])
        ),
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
        lambda s: f"session {s['session']} ({s['language']}): " + _state_text(s, s["session"]),
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


def _frame_index(text):
    # The worker refuses an index past the stack it lists, naming how many frames it has.
    if not re.fullmatch(r"[0-9]{1,4}", text):
        raise argparse.ArgumentTypeError("expected a frame index: 0 (innermost), 1, 2, ...")
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


def _reached_line(state, line):
    """Whether `continue --to LINE` stopped at LINE; if not, a note says where it stopped
    first, as `--count` and `--hit` do (seventh evaluation, finding 5): the `--to` line
    is removed at the next stop, whatever it is, so the session's own breakpoint can
    come first."""
    if state["state"] == "stopped":
        at = str(state.get("location") or "").rpartition(":")[2]
        if state.get("reason") == "breakpoint" and at == str(line):
            return True
        if _counted(state):
            state["note"] = (
                f"stopped at the session's breakpoint (line {at}, hit {state['hit']}) before "
                f"reaching line {line}; `session continue --to {line}` again"
            )
        else:
            state["note"] = f"stopped for another reason before line {line} (shown above)"
    elif state["state"] == "terminated":
        state["note"] = f"the program exited without reaching line {line}"
    else:
        state["note"] = (
            f"the program is still running (line {line} not reached yet); `peeky session wait`"
        )
    return False


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
    if args.command == "verify" and not texts["old"] and not texts["new"]:
        raise ValueError(
            "verify needs an edit: --old and --new (or --old-file/--new-file), or --baseline "
            "to check the unedited program"
        )
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
    """Refuse an edit whose --old is not in the original exactly once, or whose --new
    changes nothing, saying which condition failed."""
    if not old:
        raise ValueError(f"--old is empty{which}: it must quote text of the file exactly once")
    count = text.count(old)
    same = (
        f"--new equals --old{which}, so the edit changes nothing (to run the unedited "
        "program, leave out --old/--new; to check it, `verify --baseline`)"
    )
    if count == 1 and old == new:
        raise ValueError(same)
    if count == 1:
        return
    also = "; also, --new equals --old" if old == new else ""
    if count:
        lines, start = [], 0
        while (index := text.find(old, start)) >= 0 and len(lines) < 10:
            lines.append(str(text.count("\n", 0, index) + 1))
            start = index + len(old)
        raise ValueError(
            f"--old matched {count} times{which} (lines {', '.join(lines)}); it must match "
            f"exactly once: include more surrounding text{also}"
        )
    advice = "copy the exact text, including indentation"
    if "\\n" in old and "\n" not in old:
        advice = "it contains a literal backslash-n; pass real newlines (bash: $'a\\nb')"
    raise ValueError(f"--old matched 0 times{which}; it must match exactly once: {advice}{also}")


def _diff(before, after, name):
    import difflib

    lines = difflib.unified_diff(
        split_lines(before, keepends=True), split_lines(after, keepends=True), name, name
    )
    text = "".join(lines)
    return text if len(text) <= 8000 else text[:8000] + "\n... (diff truncated)\n"


def _verify_candidate(args, root, program, language, data, source, check, destination, deadline):
    if language == "python":
        from peeky.agent import digest
        from peeky.agent import verify as run_checker

        original = digest(source)
        text = source.read_text()
        if args.old is not None and (text.count(args.old) != 1 or args.old == args.new):
            raise ValueError("--old must match exactly once and --new must change it")
        candidate = destination / "candidate" / program
        candidate.parent.mkdir(parents=True, exist_ok=True)
        # --baseline: the unedited program, checked as a candidate is.
        candidate.write_text(text if args.old is None else text.replace(args.old, args.new, 1))
        candidate_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
        result = {
            **run_checker(check, candidate, root, deadline=deadline),
            "candidate": str(candidate),
        }
        result["candidate_sha256"] = candidate_sha256
        if digest(source) != original:
            raise RuntimeError("Original source changed during verification")
        return result
    from peeky.compiled_verification import CompiledVerification

    class Verification(CompiledVerification):
        pass

    Verification.language = language
    return Verification(adapter_for(language, data), root).candidate(
        source, check, args.old, args.new, destination, deadline=deadline
    )


MAX_VERIFY_S = 600


def verify(args):
    started = time.monotonic()
    if args.timeout is not None and not 0 < args.timeout <= MAX_VERIFY_S:
        raise ValueError(f"--timeout must be within (0, {MAX_VERIFY_S}] seconds")
    deadline = None if args.timeout is None else started + args.timeout
    root, program, _, language, data = resolve(args.target, args.at, args.config)
    if language == "java-attach":
        raise ValueError("Attached JVMs cannot verify edits; verify with a fresh build target")
    program = _default_program(program, data, "verify")
    source, check = (root / program).resolve(strict=True), Path(args.check).resolve(strict=True)
    original = source.read_text()
    if args.baseline:
        if any(getattr(args, name) for name in ("old", "new", "old_file", "new_file")):
            raise ValueError(
                "--baseline checks the unedited program: leave out --old/--new (and their "
                "-file forms)"
            )
        args.old = args.new = None
    else:
        # Several edits are verified as the one edit with their combined effect.
        args.old, args.new = combine_edits(original, _checked_edit(original, _edit_pairs(args)))
    edited = original if args.baseline else original.replace(args.old, args.new, 1)
    destination = Path(tempfile.mkdtemp(prefix="peeky-verify-"))
    try:
        result = _verify_candidate(
            args, root, program, language, data, source, check, destination, deadline
        )
        # The reported diff must describe exactly the candidate that was verified.
        expected = edited.encode()
        if hashlib.sha256(expected).hexdigest() != result.get("candidate_sha256"):
            raise RuntimeError(
                "Original source changed during verification; the verified candidate does "
                "not match the reported edit"
            )
    finally:
        if not args.keep:
            shutil.rmtree(destination, ignore_errors=True)
    result.setdefault("stage", "verification")
    result["seconds"] = round(time.monotonic() - started, 3)
    result["diff"] = _diff(original, edited, program)
    if args.baseline:
        # The checker's verdict on the program as it is: it should fail on the bug.
        result["baseline"] = True
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
    if not result["passed"] and (reason := _verify_reason(result)):
        result["reason"] = reason

    _emit(result, args.json, lambda shown: _verify_text(shown, args.keep))
    return 0 if result["passed"] else 1


def _verify_text(result, keep=False):
    lines = [
        f"passed: {result['passed']}  stage: {result['stage']}"
        + (f"  seconds: {result['seconds']}" if "seconds" in result else "")
    ]
    if tests := result.get("tests"):
        lines.extend(_tests_text(tests))
    elif result.get("language") == "rust" and result.get("stage") == "verification":
        lines.append(
            "compiled tests: none (the crate has no integration test; the checker decides)"
        )
    reason = not result["passed"] and (result.get("reason") or _verify_reason(result))
    if reason:
        lines.append(f"reason: {reason}")
    lines.append(f"checker exit: {result.get('exit_code')}")
    output = [(result.get(key) or "")[-2000:] for key in ("stdout", "stderr")]
    shown = "\n".join(text.strip() for text in output if text.strip())
    if reason and shown and shown == re.sub(r" \(SystemExit: [^)]*\)$", "", reason):
        # The checker printed only the line `reason` shows (eighth evaluation, L5).
        lines.append("output: the reason above, its only line")
    elif any(output):
        lines.extend(("output:", *output))
    if keep:
        lines.append(
            f"candidate: {result.get('candidate')} (the edited file, and for compiled "
            "programs the build evidence beside it; build artifacts are not kept)"
        )
    if result.get("baseline"):
        lines.append(
            "baseline: the unedited program, no edit"
            + (
                " (the checker rejects it, as it should reject the bug)"
                if _rejected(result)
                else ""
            )
        )
    else:
        lines.append("diff:\n" + result["diff"])
    return "\n".join(line for line in lines if line)


# What the checker's output streams keep (agent.execute's `retain`): a stream this long
# may end part-way through a line.
_RETAINED_BYTES = 16000


# A line that says what failed, at a line's start: `FAIL`/`FAILED` (not a unittest
# summary, `FAILED (failures=1)`), unittest's `ERROR:`, pytest's `E   ...`, an exception
# (`ValueError: ...`, a traceback's last line), an `assert`.
_FAILURE = re.compile(
    r"^(?:FAIL(?:ED)?\b(?! \((?:failures|errors)=)|ERROR: |E\s|"
    r"[A-Za-z_][\w.]*(?:Error|Exception)(?::|$)|assert\b)"
)
_EXCEPTION = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception)(?::|$)")


def _failure_line(stream):
    """The first line of ``stream`` that says what failed (``_FAILURE``), or None: with
    up to two indented lines after it (`FAIL first case; expected 3; got 2`); for a
    unittest test's header (`FAIL: test_wrap (...)`), with the exception it reports;
    pytest's `E` marker left out."""
    lines = [line.rstrip() for line in split_lines(stream) if line.strip()]
    at = next((i for i, line in enumerate(lines) if _FAILURE.match(line)), None)
    if at is None:
        return None
    first = re.sub(r"^E\s+", "", lines[at])
    if re.match(r"(?:FAIL|ERROR): ", first):
        raised = next((line for line in lines[at + 1 :] if _EXCEPTION.match(line)), None)
        return f"{first}: {raised}" if raised else first
    following = []
    for line in lines[at + 1 : at + 3]:
        if not line[:1].isspace():
            break
        following.append(line.strip())
    return "; ".join([first.strip(), *following])


def _verify_reason(result):
    """Why a verify did not pass, in one line: the build's diagnostic, a timeout, or the
    checker's error; for a checker that exited with a status alone (`sys.exit(1)`,
    unittest's `sys.exit(True)`), the first line of its stdout that says what failed
    (``_FAILURE``), else stdout's last line; with no stdout, the same of stderr
    (seventh evaluation, finding 11). The status stays in `checker exit` (and
    `checker.error`), and in the reason where it is not that exit code."""
    checker = str((result.get("checker") or {}).get("error") or "")
    reason = result.get("diagnostic") or checker
    # A build's own timeout is in its diagnostic (`The build failed: clang timed out`).
    if result.get("timed_out") and result.get("stage") != "build":
        return "checker timed out"
    status = re.fullmatch(r"SystemExit: (-?[0-9]+|True)", checker)
    if result.get("diagnostic") or status is None or status[1] == "0":
        return reason or None
    streams = [result.get(key) or "" for key in ("stdout", "stderr")]
    if any(len(stream.encode()) >= _RETAINED_BYTES for stream in streams):
        # Cut short: its last line may be part of one.
        return reason
    chosen = None
    for stream in streams:
        lines = [line.strip() for line in split_lines(stream) if line.strip()]
        if lines:
            chosen = _failure_line(stream) or lines[-1]
            break
    if chosen is None:
        return reason
    chosen = chosen[:500]
    code = "1" if status[1] == "True" else status[1]
    return chosen if code == str(result.get("exit_code")) else f"{chosen} ({checker})"


def _rejected(result):
    """Whether the checker ran to its own verdict and rejected the program: an assertion
    failed, or it exited with a nonzero status. Not a crash of the checker (any other
    exception), a timeout, nor a build that failed before it ran."""
    error = str((result.get("checker") or {}).get("error") or "")
    return (
        result.get("stage") == "verification"
        and not result.get("timed_out")
        and result.get("exit_code") not in (0, None)
        and (
            error.startswith("AssertionError")
            or re.fullmatch(r"SystemExit: (?!0$|None$)\S.*", error) is not None
        )
    )


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
        message = "\n    ".join(split_lines(_printable(failure["message"])))
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
    # The job always records JSON, so `job wait` can show it either way (--json or text).
    spec = {
        "id": job_id,
        "command": command,
        # Right after the subcommand: never after a bare `--` (verify ... -- target).
        "argv": argv if "--json" in argv else [argv[0], "--json", *argv[1:]],
        "json": "--json" in argv,
        "keep": "--keep" in argv,
        "cwd": os.getcwd(),
    }
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
                "\n".join(
                    f"{j['job']}  {j['command']}  {j['status']}"
                    + (f"  {j['seconds']} s" if "seconds" in j else "")
                    for j in jobs
                )
                or "no jobs"
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
        log = split_lines((path / "log").read_text())[-5:] if (path / "log").is_file() else []
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
    # What the foreground command would have printed on each stream: its JSON with
    # --json (here or at submission), else its text.
    spec, out = json.loads((path / "job.json").read_text()), (path / "out").read_text()
    if args.json or spec.get("json", True):
        sys.stdout.write(out)
    else:
        sys.stdout.write(_job_text(spec, out))
    if (path / "log").is_file():
        sys.stderr.write((path / "log").read_text())
    return state["exit_code"]


def _job_text(spec, out):
    """A finished job's text, rendered from the JSON it recorded."""
    try:
        result = json.loads(out)
    except ValueError:
        return out
    if not isinstance(result, dict) or set(result) == {"error"}:
        # An error printed only to stderr (the log), as in the foreground.
        return ""
    text = (
        _verify_text(result, spec.get("keep", False))
        if spec["command"] == "verify"
        else _probe_text(result, quiet="--quiet" in spec.get("argv", ()))
    )
    return text + "\n"


def main(argv=None, _job=False, _edits=None):
    from peeky.processes import keep_children_waitable

    keep_children_waitable()  # #170: an inherited ignored SIGCHLD reaps adapters
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
    # Accepted everywhere: describe prints no progress, so it changes nothing here.
    sub.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    sub = commands.add_parser("probe", help="One stateless question: stop, capture, set, report")
    probe_parser = sub
    common(sub)
    sub.add_argument("--at", required=True, help="<line> or <path>:<line>")
    sub.add_argument(
        "--capture",
        default="",
        help="Comma-separated selectors, e.g. a,b.c,d[0]; `return`: what the function holding "
        "the line returns, each call (C, C++, Rust, Java)",
    )
    sub.add_argument(
        "--set",
        action="append",
        default=[],
        help="name=value: a local; in Java, Rust, C and C++ also a scalar field or array "
        "element (current.end=10, xs[0]=1). A value in double quotes is a text, as JSON "
        "writes a string ('name=\"apple\"'): a char array, std::string, Rust String or Java "
        "String local takes it within its storage. Repeatable",
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
        "--set-at",
        nargs=2,
        action="append",
        default=[],
        metavar=("[PATH:]LINE[@HIT]", "NAME=VALUE"),
        help="Change a value at its own stop: at hit HIT (default 1) of LINE, counting that "
        "line's own stops, in the --at file, whether or not values are captured there. "
        "Repeatable, at several lines and hits (shorten at 22@3, restore at 23@3); each is "
        "applied once. HIT counts every stop of LINE, in any function (a lambda or closure "
        "on the line, a recursive call); each change says which function's variable it "
        "changed. One the program never reaches is reported, and the probe exits with 4",
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
    # A worker's probe child (#190 review): a launch budget below the worker's watchdog,
    # and a file written just before the debugger is asked to start the program.
    sub.add_argument("--launch-budget", type=float, help=argparse.SUPPRESS)
    sub.add_argument("--launch-marker", type=Path, help=argparse.SUPPRESS)
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
        help="exit: run to completion for output; hits: end right after the last hit (the "
        "program, resumed, runs on until Peeky ends or detaches it, so its output can follow "
        "that hit; auto: hits for attached JVMs, else exit)",
    )
    sub.add_argument(
        "--quiet", action="store_true", help="No progress messages on stderr, nor one-time notes"
    )
    sub.add_argument("--label", help="Tag this run in the evidence store (e.g. input=empty)")
    sub.add_argument(
        "--no-save", action="store_true", help="Do not record this run in the evidence store"
    )
    candidate = (
        "Probe a candidate fix (any target but an attached JVM): a fresh build, or for "
        "Python an edited copy, never "
    )
    sub.add_argument(
        "--old",
        action="append",
        help=candidate + "touching your files. Exact text to replace once in the --at file; "
        "repeat --old/--new for several edits",
    )
    edit_options(sub)
    evidence_store(sub)
    background(sub)
    sub = commands.add_parser(
        "sql",
        help="Read-only SQL over saved probe evidence",
        description="Read-only SQL over saved probe evidence. Tables show a C++ or Rust "
        "frame's short name (trim_left); the store keeps the debugger's whole name, which "
        "SQL compares: match it with short_function(function) = 'trim_left', and a closure "
        "or generic instance as a probe's table shows it with instance = "
        "'cz::main::{closure#0}'.",
    )
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
        (
            "get",
            "Read selectors (a, a.b, a[0], a['key']; a C++ map's m[K] is key K's value, "
            "m[#N] its Nth entry); lists one level of children",
        ),
        (
            "set",
            "Change one root local (in Java, Rust, C and C++ also a field or array element) "
            "to a literal (name=value) at the current stop",
        ),
        ("continue", "Resume to the next breakpoint stop, --to LINE, or --to-exit"),
        ("step", "Run to the next line (--into a call, --out of the function)"),
        ("report", "State and program output (--json also lists the worker's requests)"),
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
    actions.choices["get"].add_argument(
        "selectors",
        nargs="+",
        help="Selectors; `return`: what the function a step just left returned",
    )
    actions.choices["inspect"].add_argument(
        "--globals",
        action="store_true",
        help="Python: also list the module's globals. Listing them runs every global's repr "
        "in the program, as listing locals runs theirs; `get` lists them too (all of them) "
        "for a name missing from the locals, a misspelled one included",
    )
    for action in ("inspect", "get"):
        actions.choices[action].add_argument(
            "--frame",
            type=_frame_index,
            default=0,
            metavar="N",
            help="Read stack frame N: 0 is where the program stopped (default), 1 its caller, "
            "... as `session inspect --json` lists `frames` (at most 10)",
        )
    actions.choices["set"].add_argument(
        "assignment",
        help="name=value: a root local, or a scalar field or array element (Java, Rust, C, "
        "C++: current.end=10, xs[0]=1, t.__0=3); a text in double quotes for a string "
        "('name=\"apple\"')",
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
        help="Also stop at LINE of the session's file, once (removed at the next stop: "
        "another stop first, such as the session's breakpoint, says so and exits 3)",
    )
    stepping = actions.choices["step"].add_mutually_exclusive_group()
    stepping.add_argument("--into", action="store_true", help="Step into the call on this line")
    stepping.add_argument("--out", action="store_true", help="Run until this function returns")
    sub = commands.add_parser(
        "verify",
        help="Fresh build + independent checker for one edit (--baseline: the unedited program)",
        usage="%(prog)s target --check CHECK (--old OLD --new NEW [--old OLD --new NEW ...] | "
        "--baseline)\n                    [--at AT] [--config CONFIG] [--json] [--keep] "
        "[--details] [--timeout TIMEOUT]\n                    [--quiet] [--background]",
        description="Build the program fresh with the edit and run the trusted checker on it. "
        "--old and --new (or --old-file/--new-file) are required, once per edit, unless "
        "--baseline, which checks the unedited program instead: the checker should fail there "
        "before a fix's `passed: True` means anything.",
    )
    common(sub)
    sub.add_argument("--at", help="Program path (for multi-file targets)")
    sub.add_argument("--check", required=True, help="Trusted checker script")
    sub.add_argument(
        "--old",
        action="append",
        help="Required unless --baseline: exact source text to replace (once); repeat "
        "--old/--new for several edits, each quoting the original file",
    )
    edit_options(sub)
    sub.add_argument(
        "--baseline",
        action="store_true",
        help="Instead of an edit, build and check the unedited program (no --old/--new): "
        "`passed: False` shows the checker catches the bug",
    )
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
    sub.add_argument(
        "--timeout",
        type=float,
        help=f"Fail with TIMEOUT after this many seconds, build included (at most {MAX_VERIFY_S})",
    )
    sub.add_argument("--quiet", action="store_true", help="No progress messages (verify has none)")
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
            shown = {"error": payload}
            if getattr(args, "command", None) == "probe" and getattr(exc, "before_launch", False):
                # As a probe the worker refused (#190): its launch request was not sent.
                shown = {"status": "failed", **shown, "refused_before_launch": True}
            print(json.dumps(shown, indent=2))
        print(f"peeky: [{payload.get('code')}] {payload.get('message')}", file=sys.stderr)
        print(f"  hint: {payload.get('hint', '')}", file=sys.stderr)
        if payload.get("cleanup"):
            print(f"  cleanup: {payload['cleanup']}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
