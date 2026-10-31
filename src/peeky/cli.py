"""`peeky`: a standalone debugger tool that agents, harnesses and humans call.

Peeky never calls a model. A model (Copilot, Claude, Codex), a script or a person
runs these commands and decides what to do next, as with `git`. There is no mode
switch: `probe` answers one question in one stateless call and saves its evidence,
`sql` and `queries` ask follow-up questions about saved evidence (read-only SQL),
`session` subcommands explore step by step, `verify` checks a candidate fix on a fresh
build, `describe` prints a manifest of what can be asked, `setup-java` builds the Java
adapter once. Targets are detected from the path:
`Cargo.toml` -> Rust, `.py` -> Python, `.c`/`.cpp` -> C/C++; `--config` overrides
detection with a trusted target configuration (required for Java).
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, replace
from pathlib import Path

from peeky import add_version, state_home
from peeky.adapters import COPY_NOTE, PYDEVD_GROUPS
from peeky.client import LOOPBACK_URL, MIN_TOKEN_CHARS, WORKER_TOKEN_ENV, Client, client_token
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
    debugger_diagnostics,
    diagnostics_again,
    keeps_lines,
    parse_hits,
    parse_selector,
    parse_set_at,
    pointed_text,
    probe_exit_code,
    run_probe,
    split_selectors,
)
from peeky.processes import CREDENTIAL_ENV, without_credentials
from peeky.textlines import split_lines

RUST_VERSION = "1.96.0"


MAX_SESSION_SECONDS = 300


def _state_base():
    """Peeky's state directory (peeky.state_home), made if missing: a real directory (not a
    link) that you own, mode 0700."""
    base = state_home()
    # Only yours, whatever the umask (002 where each user has a group of their own): Peeky
    # builds under it (build.private_base), and runs its own Java adapter from it.
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = base.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(
            f"Peeky's state directory must be a directory you own, not a link: {base}. Move "
            "it aside, or set PEEKY_STATE_DIR to a directory of yours"
        )
    resolved = base.resolve()
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise ValueError(
            f"Peeky's state directory can't be / or your home directory, whatever its mode: "
            f"{base}. Set PEEKY_STATE_DIR to a directory of its own"
        )
    if info.st_mode & 0o077:
        base.chmod(0o700)
    return base


def _private_dir(name):
    path = _state_base() / name
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


class OutsideTarget(ValueError):
    """`--at` or `--run` names a file outside the target: ``path``, resolved, ``root``, and
    the target's ``language`` (None where not known)."""

    def __init__(self, root, path, language=None):
        super().__init__(
            f"--at and --run must name a file inside the target, {root}, and {path} is not "
            "inside it. Give a target directory that holds it"
        )
        self.root, self.path, self.language = root, path, language


def _confined(root, program, language=None):
    """A program path relative to and inside the target root."""
    if program is None:
        return None
    path = (root / program).resolve()
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        raise OutsideTarget(root, path, language) from None


def _outside(exc, args):
    """The refusal of a file outside a Python target: where the program's interpreter (the
    one `--python` or the root chooses) proves it installed code, what to do instead. For
    another language, or a remote worker, the refusal as it is."""
    if (
        exc.language != "python"
        or getattr(args, "worker", None) is not None
        or exc.path.suffix != ".py"
    ):
        return exc
    adapter = None
    try:
        adapter = python_adapter(exc.root, getattr(args, "python", None))
        if not adapter.installed(exc.path):
            return exc
    except Exception:
        return exc
    finally:
        if adapter is not None:
            adapter.close()
    return ValueError(
        f"--at and --run must name a file inside the target, {exc.root}, and {exc.path} is "
        "installed code outside it, in a site-packages directory of the program's "
        f"interpreter ({adapter.interpreter}). Name your project's own file, and install the "
        "project editable (`pip install -e .`), or install the package in the root's .venv "
        "and name its file there"
    )


def _which(*names):
    for name in names:
        if name and (found := shutil.which(name)):
            return str(Path(found).resolve())
    return None


def _xcrun(tool):
    if sys.platform != "darwin" or not shutil.which("xcrun"):
        return None
    result = subprocess.run(
        ["xcrun", "--find", tool],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env=without_credentials(os.environ),
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
        env=without_credentials(os.environ),
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


_ON_PATH_SAID = set()
# The tools rustup's proxies stand for, by the variable that names them.
_RUSTUP_PROXIES = {"PEEKY_RUST_CARGO": "cargo", "PEEKY_RUST_RUSTC": "rustc"}


def _env_tool(variable):
    """A PEEKY_* tool variable's program. A bare name, such as `gcc`, is looked up on PATH
    as a shell would, and Peeky says which file it found; any other value is a path."""
    value = os.environ.get(variable)
    if not value or "/" in value:
        return value
    found = shutil.which(value)
    if not found:
        raise FileNotFoundError(
            f"{variable}={value}: no program named {value} on PATH; install it, or set "
            f"{variable} to its full path"
        )
    shown = Path(found).absolute()
    path = str(shown.resolve())
    linked = any(part.is_symlink() for part in (shown, *shown.parents))
    said = f"using {shown} (a link to {path})" if linked else f"using {path}"
    said += f", the first {value} on PATH"
    if _RUSTUP_PROXIES.get(variable) == value and _is_rustup(shown):
        # `cargo` or `rustc` that is rustup's proxy runs whichever toolchain rustup picks;
        # Peeky builds with its pinned one, whose real binaries its checks require. Any
        # other name (`rustfmt`, `rustup`) stays as found, for the checks to refuse.
        tool = _RUSTUP_PROXIES[variable]
        path = _rustup(tool)
        if not path:
            raise FileNotFoundError(
                f"{variable}={value}: {shown} is rustup's proxy, and rustup gave no "
                f"{RUST_VERSION} {tool}; run `rustup toolchain install {RUST_VERSION}`, or set "
                f"{variable} to a toolchain's {tool}"
            )
        said = (
            f"using {path}, rustup's {RUST_VERSION} {tool}: the first {value} on PATH, "
            f"{shown}, is rustup's proxy"
        )
    if (variable, value) not in _ON_PATH_SAID:
        _ON_PATH_SAID.add((variable, value))
        print(f"peeky: {variable}={value}: {said}", file=sys.stderr, flush=True)
    return path


def _is_rustup(path):
    """Whether ``path`` is rustup itself: a proxy is a link to it, or a hard link."""
    if path.resolve().name in ("rustup", "rustup-init"):
        return True
    beside = path.parent / "rustup"
    try:
        return beside.is_file() and os.path.samefile(path, beside)
    except OSError:
        return False


def _required(label, value):
    if not value:
        raise ValueError(f"Cannot find {label}; set it with --config or PEEKY_* variables")
    return str(Path(value).resolve(strict=True))


def rust_tools():
    """Discover pinned Rust and LLDB tools; PEEKY_RUST_* variables take precedence."""
    env = os.environ.get
    darwin = sys.platform == "darwin"
    tools = {
        "cargo": _env_tool("PEEKY_RUST_CARGO") or _rustup("cargo"),
        "rustc": _env_tool("PEEKY_RUST_RUSTC") or _rustup("rustc"),
        "adapter": _env_tool("PEEKY_RUST_DAP")
        or _xcrun("lldb-dap")
        or _which("lldb-dap", "lldb-dap-18"),
        "dwarfdump": _env_tool("PEEKY_RUST_DWARFDUMP")
        or ("/usr/bin/dwarfdump" if darwin else _which("llvm-dwarfdump", "llvm-dwarfdump-18")),
        "linker": _env_tool("PEEKY_RUST_LINKER")
        or ("/usr/bin/clang" if darwin else _which("clang", "clang-18")),
    }
    result = {name: _required(f"Rust {name}", value) for name, value in tools.items()}
    if darwin:
        result["dsymutil"] = _env_tool("PEEKY_RUST_DSYMUTIL") or "/usr/bin/dsymutil"
    if env("PEEKY_RUST_SYSROOT"):
        result["sysroot"] = env("PEEKY_RUST_SYSROOT")
    return result


def native_tools(language):
    env = os.environ.get
    compiler = _env_tool("PEEKY_NATIVE_CXX" if language == "cpp" else "PEEKY_NATIVE_CC")
    name = "clang++" if language == "cpp" else "clang"
    result = {
        "compiler": compiler or _xcrun(name) or _which(name, name + "-18"),
        "adapter": _env_tool("PEEKY_NATIVE_DAP")
        or _xcrun("lldb-dap")
        or _which("lldb-dap", "lldb-dap-18"),
        "dwarfdump": _env_tool("PEEKY_NATIVE_DWARFDUMP")
        or _xcrun("llvm-dwarfdump")
        or _which("llvm-dwarfdump", "llvm-dwarfdump-18", "dwarfdump"),
    }
    result = {key: _required(f"native {key}", value) for key, value in result.items()}
    if sys.platform == "darwin":
        result["dsymutil"] = _env_tool("PEEKY_NATIVE_DSYMUTIL") or _xcrun("dsymutil")
        sdk = subprocess.run(
            ["xcrun", "--show-sdk-path"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=without_credentials(os.environ),
        )
        result["sysroot"] = env("PEEKY_NATIVE_SYSROOT") or sdk.stdout.strip() or None
    return result


def _cargo_root(path):
    for candidate in (path, *path.parents):
        if (candidate / "Cargo.toml").is_file():
            return candidate
    return None


def resolve(target, at=None, config=None, *, tools=True, binary=None):
    """Return (root, program, line, language, trusted target config dict or None).

    ``tools=False`` for a run on a worker: its configuration names the tools, so this
    host's are neither looked for nor named (the config has no tool paths then).
    ``binary``: `--binary`, a binary the project built under ``target`` (ADR 0008)."""
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
    if binary is not None:
        root, language, data = _binary_target(path, binary, config, program, tools)
    else:
        root, program, line, language, data = _resolve(path, program, line, config, tools)
    program = _confined(root, program, language)
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


# The keys a binary target's config may hold (native_binary.BinaryTarget).
_BINARY_KEYS = frozenset(
    {"binary", "language", "compiler", "adapter", "dwarfdump", "dsymutil", "arguments", "optimized"}
)
_CPP_SUFFIXES = (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx")


def _binary_target(path, binary, config, program, tools):
    """(root, language, data) of `--binary`: the binary, relative to the target directory
    and inside it, with this host's tools. Its units say whether it is C or C++ (``language`` None in the
    data); the language returned is the `--at` file's, for what the command prints."""
    from peeky.errors import Refused

    if config is not None:
        raise Refused("INVALID_REQUEST", "Pass --binary or a config naming its binary, not both")
    if not tools:
        raise Refused(
            "INVALID_REQUEST",
            "A worker takes a binary only from its own configuration, which this version of "
            "Peeky doesn't read yet: probe the binary on its own host",
        )
    if not path.is_dir():
        raise Refused(
            "INVALID_REQUEST",
            "With --binary, the target is the directory the binary's sources are under",
        )
    from peeky.native_binary import BinaryTarget

    # Relative to the target directory, as a config's is, wherever the command runs.
    found = BinaryTarget(binary=binary).located(path)
    language = "cpp" if str(program or "").endswith(_CPP_SUFFIXES) else "c"
    found_tools = {key: value for key, value in native_tools(language).items() if key != "sysroot"}
    data = {"binary": found.relative_to(path).as_posix(), "language": None, **found_tools}
    return path, language, data


def _resolve(path, program, line, config, tools=True):
    if config is not None:
        data = json.loads(Path(config).read_text())
        if not isinstance(data, dict):
            raise ValueError("Target configuration must be a JSON object")
        if "binary" in data:
            from peeky.errors import Refused

            if unknown := sorted(set(data) - _BINARY_KEYS):
                raise Refused(
                    "INVALID_REQUEST",
                    "A binary target's config takes "
                    + ", ".join(sorted(_BINARY_KEYS))
                    + f"; not {', '.join(unknown)}"
                    + (
                        ": its sources are the files its debug information names"
                        if "sources" in unknown
                        else ""
                    ),
                )
            given = data.get("language")
            language = given or ("cpp" if str(program or "").endswith(_CPP_SUFFIXES) else "c")
            root = path if path.is_dir() else path.parent
            return root, program, line, language, {**data, "language": given}
        from peeky.rust_workspace import is_workspace_config

        language = data.pop("language", None) or (
            "rust"
            if "cargo" in data or is_workspace_config(data)
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
        elif language in ("java", "java-attach") and "bundle" not in data:
            from peeky.java_setup import default_bundle

            # The one `peeky setup-java` builds.
            data["bundle"] = str(default_bundle())
        if language in ("c", "cpp"):
            data["language"] = language
        if language == "rust" and is_workspace_config(data) and tools:
            # The tools the configuration leaves out are this host's (PEEKY_RUST_*, rustup).
            data = {**rust_tools(), **data}
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
        try:
            binary, tests = cargo_layout(crate)
        except ValueError:
            if tools or not program or path.suffix == ".rs":
                raise
            # A worker's: its own configuration names what it builds (a workspace's
            # `--rust-config`), and it compares the files that build reads with these.
            return crate, program, line, "rust", None
        sources = tuple(
            sorted(p.relative_to(crate).as_posix() for p in (crate / "src").rglob("*.rs"))
        )
        if len(sources) > 64:
            raise ValueError("Rust targets support at most 64 source files; use --config")
        if path.is_file() and path.suffix == ".rs":
            program = program or path.relative_to(crate).as_posix()
        data = {"sources": sources, "tests": tests, **(rust_tools() if tools else {})}
        return crate, program or binary, line, "rust", data
    if path.is_dir() and (native := _native_program(path, program, tools)):
        # A directory names its program by --at (`ring.cpp:16`), or holds exactly one.
        path, program = native
    if path.is_file() and path.suffix in (".c", ".cpp", ".cc", ".cxx"):
        language = "c" if path.suffix == ".c" else "cpp"
        suffixes = (".c",) if language == "c" else (".cpp", ".cc", ".cxx")
        headers = (".h",) if language == "c" else (".h", ".hpp", ".hh", ".hxx")
        root = path.parent
        headers = tuple(sorted(p.name for p in root.iterdir() if p.suffix in headers))
        if not tools:
            # The worker's configuration names the sources it builds, and its tools.
            return root, program or path.name, line, language, {"language": language}
        found = native_tools(language)
        data = {
            "language": language,
            "sources": _native_sources(root, path.name, suffixes, found, language),
            "headers": headers,
            **found,
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
    "Java targets need --config <file.json> naming the sources and the main class, e.g. "
    '{"sources": ["shop/Main.java"], "main_class": "shop.Main"}, and an adapter built once '
    "with `peeky setup-java` (java-cookbook 7.3; attached JVMs: 7.4)"
)


def _native_program(directory, program, tools=True):
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
    files = sorted(
        item.name
        for item in directory.iterdir()
        if item.suffix in _NATIVE_SUFFIXES and item.is_file() and item.resolve().parent == directory
    )
    if not tools:
        # A run on a worker runs no compiler here, and the text alone can't tell a main
        # in an inactive #if branch from one a macro makes: only a sole file is the program.
        if len(files) > 1:
            listed = ", ".join(files[:20]) + (f" and {len(files) - 20} more" if files[20:] else "")
            raise ValueError(
                f"Name the program with --at FILE:LINE, e.g. --at {files[0]}:<line>: this "
                f"directory holds {listed}, and a run on a worker runs no compiler on this "
                "host to tell which defines main"
            )
        return ((directory / files[0]).resolve(), files[0]) if files else None
    mains = [name for name in files if _MAIN.search((directory / name).read_bytes()[:4_000_000])]
    mains = mains[:20]
    if len(mains) > 1:
        # As for a file target: the preprocessor settles which really define main.
        languages = {name: "c" if name.endswith(".c") else "cpp" for name in mains}
        mains = [
            name
            for name in mains
            if _defines_main(directory / name, native_tools(languages[name]), languages[name])
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
            [*command, str(path)],
            cwd=path.parent,
            capture_output=True,
            timeout=20,
            check=False,
            env=without_credentials(os.environ),
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


def adapter_for(language, data, *, build=None):
    """The target ``data`` names. ``build``: the `peeky build` command a Rust workspace's
    refusal names (rust_build.build_command)."""
    if data is None:
        return None
    from peeky.rust_workspace import is_workspace_config

    if language == "rust" and is_workspace_config(data):
        from peeky.rust_build import RustWorkspaceTarget

        return RustWorkspaceTarget.from_config(data, build)
    data = {key: tuple(value) if isinstance(value, list) else value for key, value in data.items()}
    if "binary" in data:
        from peeky.native_binary import BinaryTarget

        return BinaryTarget(**data)
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


def python_adapter(root, option=None):
    """The PythonAdapter that runs a program under ``root`` on its interpreter
    (python_interpreter): ``option`` is `--python`."""
    from peeky.adapters import PythonAdapter
    from peeky.python_interpreter import find

    return PythonAdapter.on(find(root, option))


def _python_option(args, language):
    """Refuse a `--python` that would not be used: for another language, or on a
    worker, which chooses its own."""
    option = getattr(args, "python", None)
    if option is None:
        return
    if language != "python":
        raise ValueError(f"--python is for Python programs; this target is {language}")
    if getattr(args, "worker", None) is not None:
        raise ValueError(
            "--python is for a program run here; a worker runs Python programs on the "
            "interpreter it was started with (peeky-worker --python), or its root's .venv"
        )


def _launched_python(launch):
    """The interpreter a launch ran a Python program on (its target identity, as a
    run's `python`), else None."""
    identity = launch.get("target_identity") if isinstance(launch, dict) else None
    if not isinstance(identity, dict) or identity.get("language") != "python":
        return None
    return {key: value for key, value in identity.items() if key != "language"} or None


def _python_line(identity):
    from peeky.python_interpreter import text

    shown = text(identity)
    return f"python: {shown}" if shown else ""


def _java_line(runtimes):
    from peeky.java import java_text

    shown = java_text(runtimes)
    return f"java: {shown}" if shown else ""


def _entry(root, run, language):
    if run is None:
        return None
    if language != "python":
        raise ValueError("--run is supported only for Python targets")
    return _confined(root, run, language)


def _module(args, language):
    """`--run-module`'s module, checked; None without one."""
    module = getattr(args, "run_module", None)
    if module is None:
        return None
    if language != "python":
        raise ValueError("--run-module is supported only for Python targets")
    if getattr(args, "run", None) is not None:
        raise ValueError(
            "Pass --run or --run-module, not both: --run names a script, --run-module pytest"
        )
    from peeky.adapters import check_module

    return check_module(module)


def _default_program(program, data, command):
    if program is not None:
        return program
    sources = (data or {}).get("sources") or ()
    if command == "describe" and sources:
        # Breakpoint lines cover every declared source; any one prepares the target.
        return sources[0]
    if command == "verify" and len(sources) == 1:
        # The one source is the only file an edit can change (a Java config that declares
        # one source needs no --at).
        return sources[0]
    listed = f" (one of: {', '.join(sources[:10])})" if sources else ""
    raise ValueError(
        f"{command} needs --at <source>{listed} for this target"
        + (
            ": pass `--at <the source containing --old>`, the file the edit changes"
            if command == "verify" and sources
            else ""
        )
    )


def describe(args):
    root, program, _, language, data = resolve(
        args.target, args.at, args.config, binary=args.binary
    )
    if program is None and "binary" in (data or {}):
        # Breakpoint lines cover every unit judged: the binary alone prepares the target.
        program = data["binary"]
    program = _confined(root, _default_program(program, data, "describe"), language)
    _python_option(args, language)
    run = (
        " [--run <script.py> | --run-module pytest] [--arg A ...]"
        if language == "python"
        else ""
        if language == "java-attach"
        else " [--arg A ...]"
    )
    # The commands repeat the --config this describe was given (Java, explicit targets).
    target = f"{args.target} --config {args.config}" if args.config else args.target
    if args.binary is not None:
        target += f" --binary {args.binary}"
    if args.python is not None:
        target += f" --python {args.python}"
    # A file target is the file to edit; a directory needs --at to name it, but where
    # verify picks this program without it: a config's one source, a one-file crate.
    single = (
        Path(args.target).is_file() and Path(args.target).resolve() == (root / program).resolve()
    )
    sources = tuple((data or {}).get("sources") or ())
    only = sources == (program,) and (
        bool(args.config) or (language == "rust" and not (data or {}).get("tests"))
    )
    at = "" if single or only else f" --at {program}"
    manifest = {
        "peeky": "describe",
        "language": language,
        "root": str(root),
        "program": program,
        "commands": {
            "probe": f"peeky probe {target} --at {program}:<line>{run} --capture <a,b.c> "
            "[--set name=value] [--set-hit N] [--set-at LINE@HIT|LINE@all name=value] [--hits N] "
            "[--json]",
            "session": f"peeky session start {target} --at {program}:<line>{run} [--hit N]; then "
            "peeky session inspect | get <a.b> | set <name>=<value> | step [--into|--out] | "
            "continue [--count N|--to LINE|--to-exit] | "
            "report | close",
            # `verify` refuses an attached JVM: edits need a launched program.
            **(
                {}
                if language == "java-attach"
                else {
                    "verify": f"peeky verify {target}{at} --check <checker.py> "
                    "--old <text> --new <text> (or --old-file/--new-file; repeat the pair for "
                    "several edits, each quoting the original file); --baseline instead checks "
                    "the unedited program, which the checker should fail"
                    + (
                        "; --check-command '<test command>' in place of --check runs your tests "
                        "on a fresh copy of the project"
                        if language == "python"
                        else ""
                    )
                }
            ),
            "background": "add --background to probe"
            + ("" if language == "java-attach" else " or verify")
            + ", then `peeky job wait <id>` (exit 3 while running)",
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
    if "binary" in (data or {}):
        # The project's own build: probed in any file of a unit Peeky judges; no edit or
        # session (they need a build Peeky runs, or its worker).
        commands = manifest["commands"]
        commands["probe"] = commands["probe"].replace(f"--at {program}:", "--at <file>:")
        for name in ("session", "verify", "candidate"):
            commands.pop(name, None)
        commands["background"] = "add --background to probe, then `peeky job wait <id>`"
    if language == "python":
        from peeky.python_interpreter import Readings, Unavailable, find

        # As the program's interpreter compiles it: lines and scopes differ by version and
        # optimization level.
        interpreter = find(root, args.python)
        read = Readings(interpreter.executable, interpreter)
        try:
            manifest["breakpoint_lines"] = {program: read("lines", str(root / program))}
            manifest["scopes"] = read("scopes", str(root / program))
        except Unavailable as exc:
            raise RuntimeError(
                f"Peeky could not read {program} in the program's interpreter, "
                f"{interpreter.path} ({exc})"
            ) from None
        finally:
            read.close()
        manifest["python"] = interpreter.identity()
        manifest["mutable_types"] = (
            "Python literals: numbers, strings, bytes, True/False/None and lists/tuples/dicts/sets of them"
        )
    else:
        target = adapter_for(language, data)
        manifest["target_identity"] = target.target_identity
        runtime = target.prepare(root / program, root)
        try:
            # As the build or binary read: a binary's units say its language and level.
            manifest["target_identity"] = getattr(
                runtime, "target_identity", manifest["target_identity"]
            )
            if (units := getattr(runtime, "described_units", None)) is not None:
                language = manifest["language"] = runtime.target.language
                manifest["units"] = units
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
            "version of Peeky (`peeky setup-java`). Rows mark what can change [settable]",
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
    if python := _python_line(manifest.get("python")):
        lines.append(python)
    if (units := manifest.get("units")) is not None:
        judged = sum(1 for unit in units if unit["judged"])
        lines.append(f"units: {judged} of {len(units)} judged")
        for unit in units:
            if not unit["judged"]:
                read = " (read as an optimized build)" if unit.get("read") else ""
                lines.append(f"not judged{read}: {unit['unit']}: {unit['why']}")
    for source, numbers in manifest["breakpoint_lines"].items():
        lines.append(f"breakpoint lines in {source}: {_ranges(numbers) or 'none'}")
    if views := manifest.get("value_views"):
        from peeky.rust_symbols import (
            CONTAINER_KINDS,
            OPTION_COMPOSITE,
            QUALIFIED_CONTAINERS,
            REFERENCE_PARAMETERS,
            composite_container,
        )

        enums = [name for name, kind in views.items() if kind in ("option", OPTION_COMPOSITE)]
        composite_options = OPTION_COMPOSITE in views.values()
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
        lines.append(
            f"qualified enum views: {shown or 'none'}"
            + (
                " (an Option of a tuple or struct reads from memory, as a local the build's "
                "MIR proves initialized)"
                if composite_options
                else ""
            )
        )
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
        # As the program spells them: `Vec<u32>`, `String`.
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
        captures = scope.get("captures") or {}
        typed = scope.get("typed_spans") or {}
        shown = []
        ambiguous = scope.get("ambiguous") or {}
        for name in scope["names"]:
            if name in ambiguous:
                # Bindings no selector tells apart: `let x = 1; let x = "a";`.
                count = ambiguous[name]
                shown.append(f"{name} ({count} bindings on this line: not selectable by line)")
                continue
            if isinstance(typed.get(name), list):
                # One name declared with different types: each with its own lines.
                shown += [
                    f"{name} ({spell(each['type'])}, {_block_span_text(each['spans'])})"
                    for each in typed[name]
                ]
                continue
            # A local declared only in nested blocks is visible on their lines alone.
            parts = [spell(types[name])] if name in types else []
            if name in spans:
                parts.append(_block_span_text(spans[name]))
            if name in scope.get("globals", ()):
                parts.append("module global")
            if name in scope.get("statics", ()):
                # In static storage: read where the code names it, never changed.
                parts.append("static")
            if name == "this" and name in captures:
                # A lambda's `this` is its capture of the object, said of the one name.
                parts = _capture_parts(captures[name], name, spell)
            shown.append(f"{name} ({', '.join(parts)})" if parts else name)
        # A GCC lambda's other captures, which a selector names inside it, and how each
        # is captured; among the names, in order.
        for name, capture in captures.items():
            if name != "this" or name not in scope["names"]:
                shown.append(f"{name} ({', '.join(_capture_parts(capture, name, spell))})")
        if captures:
            shown.sort(key=lambda text: text.split(" ", 1)[0])
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
    rust = manifest["language"] == "rust" and not manifest.get("optimized")
    # The root local types `--set` changes, as the build declares them.
    settable = ("i16", "i32", "i64", "u16", "u32", "u64", "isize", "usize")
    settable += ("alloc::string::String",)

    def roots(scope):
        return [
            name
            for name in scope.get("names", ())
            if (scope.get("types") or {}).get(name) in settable
            and name not in (scope.get("never_settable") or {})
            and name not in (scope.get("ambiguous") or {})
            and not isinstance((scope.get("typed_spans") or {}).get(name), list)
        ]

    # Each function's root locals of those types (`start`, `total`).
    with_roots = [
        f"{scope['scope']}: {_shown_paths(names)}"
        for scope in manifest.get("scopes", ())
        if rust and (names := roots(scope))
    ]
    if with_roots:
        mutable += (
            ". Root locals of those types (a stop's rows and `inspect` mark which can change "
            "there): "
            + "; ".join(with_roots[:12])
            + (f"; and {len(with_roots) - 12} more functions" if len(with_roots) > 12 else "")
        )
    # The fields and elements of struct and array locals `set` may change, per function:
    # the paths (`board.entries`), not `board`.
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
    # Locals no stop can change, per function and why; `probe --set` refuses them before
    # the program runs.
    whys = {
        "register": "kept in a register (a candidate edit with `std::hint::black_box(&mut x)` "
        "keeps one in memory, rust-cookbook 6.6)",
        "another binding": "another binding of the name, on the line given, is kept in a "
        "register (a candidate edit black-boxing that `let` keeps every one in memory, "
        "rust-cookbook 6.6)",
        "inlined": "a local of a function inlined where it is called (`#[inline(always)]`: no "
        "edit to it helps; change the value in the caller)",
        "async state": "held in an async body's state across an `.await` (change what it is "
        "computed from, or copy it into a local after the `.await`)",
        "not proven in memory": "not proven in memory (Peeky reads no slot of its own for "
        "every binding: a closure's capture is reached through the closure; change it where "
        "it is declared, or with a candidate edit)",
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
    if rust:
        # Where nothing can change, the way around it (an edit that keeps a local in
        # memory, or changes what the program computes).
        nothing = [
            scope["scope"]
            for scope in manifest.get("scopes", ())
            if scope.get("names")
            and not scope.get("settable_parts")
            and not any(
                (scope.get("types") or {}).get(name) in settable
                and name not in (scope.get("never_settable") or {})
                and name not in (scope.get("ambiguous") or {})
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
            "`std::hint::black_box(&mut x)` after a `let` keeps a local in memory: rust-cookbook 6.6)"
        )
    if manifest["language"] in ("c", "cpp"):
        # Two blocks declaring one name with different types: a change follows the one
        # in scope at the stop.
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


def _capture_parts(capture, name, spell):
    """`describe`'s words on a GCC lambda's capture: its type, as the lambda's
    object holds it, and how it is captured; `this` is the object, by reference
    (`[this]`) or by value (`[*this]`)."""
    parts = [spell(capture["type"])] if capture.get("type") else []
    whose = "capture of the object" if name == "this" else "capture"
    return [*parts, f"{whose} {capture.get('how')}"]


def _block_span_text(spans):
    """`line 23`, `lines 23, 28`, `lines 51-53, 78-79`: a block local's lines; `no
    breakpoint line` where none is (a Java one-line block)."""
    if not spans:
        return "no breakpoint line"
    listed = [f"{a}-{b}" if b > a else str(a) for a, b in spans]
    single = len(listed) == 1 and "-" not in listed[0]
    return ("line " if single else "lines ") + ", ".join(listed)


def _shown_paths(paths, limit=8):
    """A local's settable paths for `describe`'s line: the first ``limit``, then how
    many more (never cut silently)."""
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


# Notes every result of this command carries: a remote Python session's.
_NOTES = []


def _emit(value, as_json, text):
    if as_json:
        print(json.dumps(_noted(value), indent=2, default=str))
        return
    shown = text(value)
    for note in _NOTES:
        if note not in shown:
            shown = f"{shown}\nwarning: {note}"
    print(shown)


def _noted(value):
    """``value`` with this command's notes in its `warnings` (each row's, for a list)."""
    if not _NOTES:
        return value

    def add(item):
        if not isinstance(item, dict):
            return item
        warnings = list(item.get("warnings") or [])
        return {**item, "warnings": warnings + [n for n in _NOTES if n not in warnings]}

    return [add(item) for item in value] if isinstance(value, list) else add(value)


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
    from peeky.errors import PeekyError, Refused, classify

    try:
        root, request, edit, original, language, adapter = _probe_request(args)
        if args.worker is not None:
            client, request, expected = _remote_probe(args, root, request)
    except Exception as exc:
        # Refused before the probe request was sent (here, or in the worker's version
        # handshake): the debugger's launch or attach request was not sent.
        exc.before_launch = True
        if isinstance(exc, Refused) and exc.code == "MUTATION_REJECTED":
            # A change refused here (Python's field `--set`) reads as the worker's
            # refusals before launch do: `error [MUTATION_REJECTED]: …`.
            result = {
                "status": "failed",
                "error": classify(exc, session_closed=False),
                "refused_before_launch": True,
            }
            _emit(result, args.json, lambda shown: _probe_text(shown, quiet=args.quiet))
            return probe_exit_code(result)
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
            if isinstance(exc, PeekyError):
                exc.payload = _remote_build_required(exc.payload, args)
            raise
        if isinstance(result.get("error"), dict):
            result["error"] = _remote_build_required(result["error"], args)
    else:
        result = run_probe(
            root,
            request,
            adapter=adapter,
            progress=_progress(args),
            launch_budget=args.launch_budget,
            launch_marker=args.launch_marker,
            pycache_base=args.pycache_dir,
        )
    return _probe_result(args, root, result, edit, original, request.program, language)


def _probe_request(args):
    """(root, the Probe, edit, original text, language, the local adapter or None) for
    `peeky probe`'s arguments."""
    if args.label is not None and len(args.label) > 200:
        raise ValueError("--label is at most 200 characters")
    root, program, line, language, data = resolve(
        args.target, args.at, args.config, tools=args.worker is None, binary=args.binary
    )
    if line is None:
        raise ValueError("probe requires --at <path>:<line>")
    _python_option(args, language)
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
        module=_module(args, language),
        args=tuple(args.arg),
        expect_sha256=tuple(tuple(item.split("=", 1)) for item in args.expect_sha256),
        edit=edit,
        set_at=set_at,
        expect_inputs=_expect_inputs(args),
    )
    adapter = None
    if args.worker is None:
        adapter = (
            python_adapter(root, args.python)
            if language == "python"
            else adapter_for(language, data, build=_build_command(args))
        )
    _build_first(args, root, adapter)
    return root, request, edit, original, language, adapter


def _build_command(args):
    """The `peeky build` command for this command's target and --config, as typed."""
    from peeky.rust_build import build_command

    return build_command(args.target, args.config) if getattr(args, "config", None) else None


def _build_first(args, root, adapter):
    """`--build`: build a Rust workspace (`peeky build`) under the build's own deadline,
    before anything else runs. Without it, a probe or session never builds."""
    if not getattr(args, "build", False):
        return None
    from peeky.rust_build import BUILD_TIMEOUT_S, RustWorkspaceTarget

    if getattr(args, "worker", None) is not None:
        # The worker builds from its own configuration, as `peeky build --worker` does.
        return _remote_build(args, time.monotonic() + BUILD_TIMEOUT_S)
    if not isinstance(adapter, RustWorkspaceTarget):
        raise ValueError(
            "--build is for a Rust workspace's configuration (analyse and run); other "
            "targets are built by every probe"
        )
    return adapter.build(
        root, deadline=time.monotonic() + BUILD_TIMEOUT_S, progress=_progress(args)
    )


def _expect_inputs(args):
    path = getattr(args, "expect_inputs_file", None)
    if path is None:
        return None
    expected = json.loads(Path(path).read_text())
    if not isinstance(expected, dict):
        raise ValueError("--expect-inputs-file holds a JSON object of path: sha256")
    return tuple(sorted(expected.items()))


def _progress(args):
    if getattr(args, "quiet", False):
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
        # A probe refused before it ran leaves no run to compare.
        _keep_evidence(args, result, root, language)
    _emit(result, args.json, lambda shown: _probe_text(shown, quiet=args.quiet))
    # 2 for a probe refused before it ran, 4 for a change not made.
    return probe_exit_code(result)


def _python_inputs(root):
    """A digest of the `.py` files under ``root`` (outside hidden, cache and environment
    directories), the sources a Python program may import, or None where there are too
    many or one cannot be read."""
    from peeky.sources import python_digest

    return python_digest(root)


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
    where the probe's breakpoints are."""
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
            f"targets allow: in {language}, only the stopped function's own locals change. Add "
            "a local with a candidate edit and set it in the same probe (COOKBOOK 6.6: "
            "`--old/--new` with `--set`), or edit the source and run verify",
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


CANDIDATE_LOCATIONS = (
    "In the edited file, locations name the candidate's copy, since the line numbers are "
    "the candidate's"
)
# An edit that adds or removes no line.
CANDIDATE_SAME_LINES = "The edit keeps the line numbers: the candidate's lines are the original's"


def _bind_candidate(result, original, edit, program):
    """Attach the diff, and keep values only if they came from exactly this candidate."""
    candidate = original.replace(*combine_edits(original, edit), 1)
    expected = hashlib.sha256(candidate.encode()).hexdigest()
    (result.get("probe") or {}).pop("edit", None)
    info = result.setdefault("candidate", {})
    info["diff"] = _diff(original, candidate, program)
    # Its frames, breakpoint and locations are the copy that ran, whose numbers
    # differ only where the edit adds or removes a line.
    info["note"] = CANDIDATE_SAME_LINES if keeps_lines(original, edit) else CANDIDATE_LOCATIONS
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
        # run that is this probe without the edit.
        try:
            # The unedited source as the run read it (the candidate's own is the edit's).
            before = evidence.baseline(path, run_id, result.get("source_sha256"))
            # The run to compare with makes no change: a candidate's --set (perhaps of a
            # variable only the edit declares) is left out too.
            probe = result.get("probe") or {}
            without = (
                " and without its --set/--set-at" if probe.get("set") or probe.get("set_at") else ""
            )
            apart = evidence.stops_apart(path, before, run_id) if before is not None else None
            # The same probe with other captures: named, with what differs, since `diff`
            # compares the selectors both captured.
            near = (
                evidence.near_baseline(path, run_id, result.get("source_sha256"))
                if before is None and result.get("inputs_sha256")
                else None
            )
            if near is not None and evidence.stops_apart(path, near[0], run_id) is not None:
                near = None
        except Exception:
            return
        result["evidence"]["compare"] = (
            {"run": before, "command": f"peeky queries diff --param a={before} --param b={run_id}"}
            if before is not None and apart is None
            # Its hits are at other breakpoint locations of the line: the edit
            # changed how often the line stops per pass, or it predates stop labels.
            else {
                "run": None,
                "hint": "pair the hits by a value: `peeky queries join --param "
                f"a={before} --param b={run_id} --param key=<selector>`. Run {before} is this "
                f"probe without the edit, but at hit {apart['hit']} it was at "
                f"{evidence.stop_text(apart['stop_a'])} of the line and this run at "
                f"{evidence.stop_text(apart['stop_b'])}, so hits do not pair by number",
            }
            if before is not None
            else {"run": None, "hint": _near_hint(near, run_id, without)}
            if near is not None
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


def _near_hint(near, run_id, without):
    """`compare:`'s line where the saved run that is a candidate's probe without the edit
    captured other selectors (``near``: evidence.near_baseline's): which run, what differs,
    and the `diff` of the selectors both captured."""
    from peeky.evidence import _quoted

    run, theirs, ours = near
    shared = [selector for selector in ours if selector in theirs]
    missing = [selector for selector in ours if selector not in theirs]
    extra = [selector for selector in theirs if selector not in ours]
    differs = [
        *([f"didn't capture {_quoted(missing)}"] if missing else []),
        *([f"captured {_quoted(extra)} too"] if extra else []),
    ] or ["lists the same selectors in another order"]
    return (
        f"run {run} is this probe without the edit, but it {' and '.join(differs)}: "
        f"`peeky queries diff --param a={run} --param b={run_id}` compares the selectors both "
        f"captured ({_quoted(shared)}). To compare them all, run this probe without "
        f"--old/--new{without}, then `peeky queries diff`"
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


def _runs_noted(store, runs):
    """Whether a table showing short frame names of saved ``runs`` (run ids in the
    evidence store at ``store``) adds the note on them: where one of them has not had it
    yet, which marks them all; and for a table of no known runs, every time. Once per
    saved run, not per state directory, which an earlier run of other code could use up
    for good."""
    if not runs:
        return True
    from peeky import evidence

    try:
        known = evidence.query(
            Path(store),
            "SELECT run_id, started_at FROM runs WHERE run_id IN ("
            + ", ".join(str(int(run)) for run in runs)
            + ")",
        )["rows"]
    except Exception:
        return True
    new = not known
    for run in known:
        # A store made again numbers its runs from 1: its start time tells them apart.
        named = f"{os.path.abspath(store)}\0{run['run_id']}\0{run['started_at']}"
        new = _first_time("short-names-" + hashlib.sha256(named.encode()).hexdigest()[:24]) or new
    return new


# More probe rows than this, of several selectors, get a pointer to `queries wide`; so do
# this many selectors at 3 hits or more.
_WIDE_ROWS, _WIDE_SELECTORS = 40, 4


def _probe_text(result, quiet=False):
    from peeky.probe import _short_note

    # The note on short frame names and instance labels, once per state directory and
    # never with --quiet: `peeky sql --help` and `--schema`
    # say it too, and so does a query's table of a run that has not had it (`_runs_noted`).
    noted = not result.get("refused_before_launch") and _short_note(result) is not None
    short_names = not quiet and noted and _first_time("short-names")
    if short_names and (saved := result.get("evidence")):
        # Said with the run it is about: a query's table of that run leaves it out.
        _runs_noted(saved.get("store"), [saved.get("run_id")])
    # A Python candidate's note on its copied root: once per root and state directory,
    # never with --quiet; `--json` always has it.
    warnings = [
        warning
        for warning in result.get("warnings", ())
        if not warning.startswith(COPY_NOTE)
        or (
            not quiet
            and _first_time("python-copy-" + hashlib.sha256(warning.encode()).hexdigest()[:16])
        )
    ]
    # lldb-dap 18's lines about a GCC build's debug information, and the text it sends as
    # it ends, come with every probe of the build: whole the first time for that build
    # in this state directory, then in short. A build is its
    # `build_id`, the digest of its recipe, staged sources (an edit included) and tools;
    # without one (an older worker), they are whole every time. `--json` has them whole.
    build = result.get("build_id")
    build = hashlib.sha256(build.encode()).hexdigest()[:24] if isinstance(build, str) else None
    diagnosed = result.get("debugger_diagnostics") or [""]
    lines = hashlib.sha256(f"{build}\0{diagnosed[0]}".encode()).hexdigest()[:24]
    warnings = [
        short
        if build
        and (short := diagnostics_again(warning))
        and not _first_time(f"diagnostics-{lines}")
        else warning
        for warning in warnings
    ]
    sent_before = (
        build is not None
        and bool(result.get("output_after_disconnect"))
        and not _first_time(f"sent-after-closing-{build}")
    )
    if warnings != list(result.get("warnings", ())):
        result = {**result, "warnings": warnings}
    text = _table(result, short_names=short_names, sent_before=sent_before)
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
        if note := candidate.get("note"):
            text += f"\ncandidate: {note}"
        if diff := candidate.get("diff"):
            text += "\n" + diff.rstrip("\n")
    if saved := result.get("evidence"):
        text += (
            f"\nevidence: saved as run {saved['run_id']}; query it with `{saved['next']}` or "
            "`peeky sql` (see `peeky sql --schema`)"
        )
        selectors = {row.get("selector") for row in result.get("rows", ())}
        hits = {row.get("hit") for row in result.get("rows", ())}
        if len(selectors) > 1 and (
            len(result.get("rows", ())) > _WIDE_ROWS
            or (len(selectors) >= _WIDE_SELECTORS and len(hits) >= 3)
        ):
            # A long table, a row per selector per hit.
            text += (
                f"\nevidence: one line per hit, a column per selector: "
                f"`peeky queries wide --param run={saved['run_id']}`"
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
    if args.schema is not None:
        if args.query or any(re.search(r"\s", name.strip()) for name in args.schema):
            raise ValueError("Pass either SQL or --schema, not both")
        manifest = evidence.schema(path)
        if not args.schema:
            # Text: a short index; --json: the whole manifest.
            _emit(manifest, args.json, evidence.schema_index)
            return 0
        part = evidence.schema_part(manifest, args.schema)
        _emit(
            part, args.json, evidence.schema_text if part is manifest else evidence.schema_part_text
        )
        return 0
    if not args.query:
        raise ValueError(
            'peeky sql needs one SELECT, e.g. peeky sql "SELECT * FROM last_run"; '
            "`peeky sql --schema` lists the tables"
        )
    result = evidence.query(path, args.query, evidence.parse_params(args.param), limit=args.limit)
    _emit(
        {**result, "store": str(path)},
        args.json,
        lambda shown: evidence.table(shown, short_note=lambda runs: _runs_noted(path, runs)),
    )
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
    # The rows, their notes and the text table come from one snapshot of the store.
    result, text = evidence.canned(
        path,
        args.name,
        evidence.parse_params(args.param),
        limit=args.limit,
        table=not args.json,
        short_note=lambda runs: _runs_noted(path, runs),
    )
    _emit({**result, "query": args.name, "store": str(path)}, args.json, lambda _: text)
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
        or len(record["token"]) < MIN_TOKEN_CHARS
        or not re.fullmatch(r"[0-9a-f]{32}", str(record.get("session_id")))
    ):
        raise ValueError(f"Session record {path} is malformed; remove it")
    if record.get("remote"):
        return record, _remote_session(name, record)
    return record, _local_session(name, record)


def _local_session(name, record):
    """A local session's client. Before it sends anything else, or the
    token, its worker must prove it holds the token, and say its protocol
    (``Client._hello``): another process may hold the port once the worker is gone."""
    return _LocalClient(record["url"], record["token"], record.get("pid"), name)


def _remote_session(name, record):
    """A remote session's client, once the worker still speaks the protocol the session
    was started with: one that compares every source, and marks which paths are yours."""
    forget = (
        f"Drop this record with `peeky session close --forget --name {name}`; the worker ends "
        "the old session after its idle timeout"
    )
    if not isinstance(record.get("protocol"), int) or not isinstance(
        record.get("inputs_checked"), str
    ):
        raise ValueError(
            f"Remote session {name!r} was started without source provenance (by a Peeky "
            f"before protocol 2): start the session again. {forget}"
        )
    if record["protocol"] != PROTOCOL_VERSION:
        raise ValueError(
            f"Remote session {name!r} was started with protocol {record['protocol']}, this "
            f"client {PROTOCOL_VERSION}: start the session again. {forget}"
        )
    client = _RemoteSession(record)
    version = Client.call(client, "version")
    protocol = version.get("protocol") if isinstance(version, dict) else None
    if protocol != PROTOCOL_VERSION:
        raise ValueError(
            f"Worker speaks protocol {protocol}, this session {PROTOCOL_VERSION}: start the "
            "session again on a worker of the same Peeky version"
        )
    client.sources = version.get("sources")
    client.worker_root = version.get("root") if isinstance(version.get("root"), str) else None
    return client


class _RemoteClient(Client):
    """A remote worker's client. The worker marks each location that is one of the
    sources you both compared (`source:src/main.rs:5`): it is shown as your copy of that
    file, once you have it among the files you hashed. Any other path is the worker
    host's, and is shown as such: `worker:/srv/app/.venv/lib/pkg.py:12`."""

    def __init__(self, url, token, worker_root=None, client_root=None):
        super().__init__(url, token)
        self.sources = None
        self.worker_root, self.client_root = worker_root, client_root
        # The names of the sources you hashed for this request.
        self.names = None

    def call(self, operation, **arguments):
        result = super().call(operation, **arguments)
        if operation == "version":
            return result
        return _client_paths(result, self.worker_root, self.client_root, self.names)


WORKER_PATH = "worker:"


def _client_paths(value, worker_root, client_root, names, parent=None):
    """``value`` with each `location`, and each `source`'s `path`, as you can read it:
    your copy of a compared source, else marked as the worker host's."""
    if isinstance(value, list):
        return [_client_paths(item, worker_root, client_root, names, parent) for item in value]
    if not isinstance(value, dict):
        return value
    shown = {}
    for key, item in value.items():
        if isinstance(item, str) and (key == "location" or (key == "path" and parent == "source")):
            item = _client_path(item, worker_root, client_root, names, key == "location")
        shown[key] = _client_paths(item, worker_root, client_root, names, key)
    return shown


def _client_path(text, worker_root, client_root, names, located):
    if text.startswith(WORKER_PATH):
        return text
    path, line = text, ""
    if located:
        head, separator, tail = text.rpartition(":")
        if separator and tail.isdigit():
            path, line = head, separator + tail
    if path.startswith("source:"):
        name = path.removeprefix("source:")
        if (mine := _own_copy(name, client_root, names)) is not None:
            return mine + line
        where = f"{worker_root.rstrip('/')}/{name}" if isinstance(worker_root, str) else name
        return f"{WORKER_PATH}{where}{line}"
    # A path the worker did not name as a compared source (or an older worker's).
    return f"{WORKER_PATH}{text}" if os.path.isabs(path) else text


def _own_copy(name, client_root, names):
    """Your copy of the compared source ``name``: None unless you hashed a file of that
    name, and it is a regular file under your root, reached through no link."""
    if client_root is None or names is None or name not in names:
        return None
    if not name or "\\" in name or any(part in ("", ".", "..") for part in name.split("/")):
        return None
    root = Path(client_root)
    try:
        real = (root / name).resolve(strict=True)
        if real.relative_to(root.resolve()).as_posix() != name or not real.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return str(root / name)


class _RemoteSession(_RemoteClient):
    """A remote session's client: your checkout is rehashed before and after every
    request, and must still be what the session compared at its start."""

    def __init__(self, record):
        super().__init__(record["url"], record["token"], client_root=record.get("root"))
        self.record = record

    def _refusal(self, operation, spoken):
        return (
            f"Worker speaks protocol {spoken}, this session {PROTOCOL_VERSION}: start the "
            "session again on a worker of the same Peeky version"
        )

    def call(self, operation, **arguments):
        if operation != "close":
            self.unchanged()
        result = super().call(operation, **arguments)
        if operation != "close":
            self.unchanged()
        return result

    def unchanged(self):
        try:
            self.names = frozenset(
                _checkout_unchanged(
                    Path(self.record["root"]), self.sources, self.record, saved=True
                )
            ) | frozenset(self.record.get("hashed") or {})
        except RuntimeError:
            try:
                Client.call(self, "close", session_id=self.record["session_id"])
            except Exception:
                pass
            _release(self.record["name"])
            raise


def _checkout_unchanged(root, sources, expected, *, saved=False):
    """Your checkout's sources ({name: sha256}), once they are still the ones the worker
    compared: else PROVENANCE_CHANGED. ``expected`` is that manifest, or a saved session
    record."""
    from peeky.sources import client_sources, digest

    accepted = expected["inputs_checked"] if saved else digest(expected)
    try:
        hashed = client_sources(root, sources)
        current = digest(hashed)
    except (ValueError, RuntimeError) as exc:
        current, why = None, f" ({exc})"
    else:
        why = ""
    # The --at file and --run script in a virtual environment, compared by their own hashes
    # (``_hashed``): a session's are rehashed too.
    for name, sha in ((expected.get("hashed") or {}) if saved else {}).items():
        try:
            now = _sha256(root / name)
        except OSError:
            now = None
        if now != sha and current == accepted:
            current, why = None, f" ({name})"
    if current != accepted:
        raise RuntimeError(
            "Worker source provenance: a source in your checkout changed after the worker "
            f"compared it{why}. "
            + (
                "The session was closed, and no values were returned. Start it again"
                if saved
                else "Its results were discarded. Run it again"
            )
        )
    return hashed


def _hashed(root, sources, inputs, expected, program, entry):
    """{name: sha256} of the --at file and the --run script that are in a virtual
    environment: not among a Python target's compared sources (``inputs``), the worker
    compares them by their own hashes (``expected``, as sent)."""
    if (sources or {}).get("kind") != "python":
        return {}
    from peeky.sources import in_environment

    return {
        name: expected[role]
        for role, name in (("program", program), ("entry", entry))
        if name and role in expected and name not in inputs and in_environment(root, name)
    }


def _python_note(hashed):
    """What a remote Python result says was compared, and wasn't."""
    if not hashed:
        return REMOTE_PYTHON_NOTE
    return (
        "Only the .py files under the target's directory are compared with yours, and "
        f"{', '.join(sorted(hashed))}, which is in a virtual environment. Not compared: the "
        "rest of any directory holding a pyvenv.cfg, other installed packages, the standard "
        "library, and compiled modules (.so, .pyc)."
    )


class _LocalClient(Client):
    """A session's own local worker: if its process is gone, the session is lost.
    ``Client`` says `hello` before anything else, and sends nothing more to a worker of
    another protocol. ``name``: the session's, which says what to do then."""

    def __init__(self, url, token, pid, name=None):
        super().__init__(url, token)
        self.pid, self.name = pid, name

    def _refusal(self, operation, spoken):
        if self.name is None:
            return super()._refusal(operation, spoken)
        return (
            f"Worker speaks protocol {spoken}, this session {PROTOCOL_VERSION}: another "
            f"Peeky version started session {self.name!r}, so no request is sent to its worker. "
            f"Drop the record with `peeky session close --forget --name {self.name}` (its "
            "worker ends after its idle timeout), then start the session again"
        )

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
    record["call_change"] = state.get("call_change")
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
    command acts on the session it came from."""
    if not name or name == "default":
        return text
    return re.sub(
        r"`((?:peeky )?session [a-z]+(?: [^`]*)?)`",
        lambda m: f"`{m[1]}`" if "--name" in m[1] else f"`{m[1]} --name {shlex.quote(name)}`",
        text,
    )


def _relative_location(location, base=None):
    """`path:line`, the path relative to ``base`` (a session's: the directory its `session
    start` ran in), else the working directory, where the file is below it, as a probe
    names its program; else as given. A session's commands so name a file as its start
    did, from any directory. Text only: JSON keeps the path whole."""
    if not isinstance(location, str):
        return location
    path, sep, line = location.rpartition(":")
    if not sep or not line.isdigit() or not os.path.isabs(path):
        return location
    try:
        relative = Path(path).resolve().relative_to(Path(base or Path.cwd()).resolve())
    except (OSError, ValueError):
        return location
    return f"{relative}:{line}"


def _call_text(change, since=False):
    """How a session step's stop differs from where the step started, as the stacks show
    it, never that a call returned (it may have thrown, yielded or been unwound):
    "left `rename`; now in `run`", "now in `walk`, one call deeper, called from `walk`".
    ``since``: the same for `session get`'s place note, "after a step from `rename`".
    None for the same function at the same depth, or a kind this client does not know."""
    from peeky.names import display_function

    old, new = (f"`{display_function(change.get(key))}`" for key in ("from", "to"))
    calls = change.get("calls")
    calls = calls if type(calls) is int and calls > 0 else 1
    kind = change.get("kind")
    if kind not in ("left", "deeper", "other"):
        return None
    if since:
        return f"after a step from {old}" + (
            f", {calls} calls up"
            if kind == "left" and calls > 1
            else ", one call deeper"
            if kind == "deeper" and calls == 1
            else f", {calls} calls deeper"
            if kind == "deeper"
            else ""
        )
    if kind == "deeper":
        return f"now in {new}, " + (
            f"one call deeper, called from {old}"
            if calls == 1
            else f"{calls} calls deeper, below {old}"
        )
    return f"left {old}; now in {new}" + (
        f", {calls} calls up" if kind == "left" and calls > 1 else ""
    )


def _state_text(state, name="default", base=None):
    where = f" at {_relative_location(state['location'], base)}" if state.get("location") else ""
    if where and state.get("function"):
        from peeky.names import display_function

        where += f" in {display_function(state['function'])}"
    if where and type(state.get("hit")) is int:
        where += f", hit {state['hit']}"
    if where and type(state.get("place")) is int and type(state.get("places")) is int:
        # One of the line's several breakpoint locations in this function.
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
    elif isinstance(crash := state.get("crash"), dict):
        from peeky.probe import crash_text

        # The program crashed: what the debugger says, and where in its code.
        if isinstance(crash.get("location"), str):
            crash = {**crash, "location": _relative_location(crash["location"], base)}
        nearest = crash.get("frame")
        where += f" [the program crashed: {crash_text(crash)}; " + (
            f"`session inspect --frame {nearest}` reads that frame of your code]"
            if type(nearest) is int and nearest > 0
            else "not auto-resumed: inspect it, or `session report` for its output]"
        )
    elif reason and reason not in ("breakpoint", "step"):
        where += f" [reason: {reason}; not auto-resumed, inspect it or `session report`]"
    text = f"state: {state['state']}{where}{_pause_text(state)}{hint}"
    if isinstance(state.get("call_change"), dict) and (call := _call_text(state["call_change"])):
        text += f"\nnote: {call}"
    if isinstance(state.get("macro_stop"), str):
        text += f"\nnote: {state['macro_stop']}"
    if state.get("optimized"):
        text += f"\n{_OPTIMIZED_NOTE.format(state['optimized'])}"
    if state.get("exit_inferred"):
        text += "\nnote: the debugger never reported the exit; Peeky inferred it (no threads left)"
    if unreported := state.get("unreported_stop"):
        text += (
            f"\nnote: thread {unreported.get('thread_name')!r} was seen paused at "
            f"{unreported.get('breakpoint')}, but the debugger never reported that stop "
            "(java-debug sometimes loses a stop event); `peeky session report --json` has its "
            "debugger_events"
        )
    if state.get("note"):
        text += f"\nnote: {state['note']}"
    for warning in state.get("warnings", []):
        text += f"\nwarning: {warning}"
    if state.get("auto_resumed"):
        last = state.get("last_auto_resumed") or {}
        # A stop resumed before the debugger said where it was has no location.
        where = f"at {last['location']}" if last.get("location") else "at a place not read in time"
        text += (
            f"\nnote: {state['auto_resumed']} breakpoint stop(s) were auto-resumed after the "
            f"pause budget (latest {where}, after {last.get('paused_s')} s)"
        )
    return _named(text, name)


def _step_onto_breakpoint(record, state):
    """The note for a step's stop (over or into, which end at a line's start) on the line
    `session start` put the session's breakpoint on, in its program, or None. Hits,
    `--hit` and `continue --count` count only the stops the debugger reports as the
    breakpoint's: debugpy reports such a stop so (a hit), java-debug and LLDB as the
    step's. java-debug's next `continue` runs on to the line's next pass; Xcode's LLDB stops
    here again first where the step ended at one of the line's breakpoint locations."""
    path, _, line = str(state.get("location") or "").rpartition(":")
    if state.get("state") != "stopped" or not line.isdigit() or int(line) != record.get("line"):
        return None
    try:
        program = Path(str(record.get("root"))) / str(record.get("program"))
        if not path or Path(path).resolve() != program.resolve():
            return None
    except (OSError, ValueError):
        return None
    language = str(record.get("language") or "")
    if language.startswith("java"):
        return (
            "not a hit: java-debug reports this stop as the step's, and `session continue` "
            "runs on to the line's next pass, so this pass gets no hit number and later hits "
            "number lower than a probe's"
        )
    if language in ("c", "cpp", "rust"):
        return (
            "not a hit: LLDB reports this stop as the step's; where it is at one of the line's "
            "breakpoint locations, `session continue` stops here again first, as the next hit"
        )
    return "not a hit: hits count only the stops the debugger reports as the breakpoint's"


def _step_line_note(record, state):
    """What a step's stop on a line of only `continue`, `} else {` or `} else if …` means,
    or None. Steps land where the code jumps (AGENT_GUIDE rule 10): `continue` has not yet left the
    pass; a `} else` line's code is often the jump that ends the branch above it, and an
    `else if`'s holds its test too."""
    path, _, line = str(state.get("location") or "").rpartition(":")
    if state.get("state") != "stopped" or not path or not line.isdigit() or int(line) < 1:
        return None
    try:
        source = Path(path)
        if not source.is_absolute():
            source = Path(str(record.get("root"))) / source
        text = split_lines(source.read_text(errors="replace"))[int(line) - 1]
    except (OSError, ValueError, IndexError):
        return None
    python = record.get("language") == "python"
    code = re.sub(r"\s*#.*$" if python else r"\s*//.*$", "", text).strip()
    if re.fullmatch(r"continue\s*;?", code):
        return (
            f"line {line} is `continue`, not run yet: the next step leaves this pass of the "
            "loop, for its next pass or its end"
        )
    if not python and re.fullmatch(r"\}\s*else\s*\{?", code):
        return (
            f"line {line} is `{code}`: its code is often the jump that ends the branch above "
            "it, past the `else` part; the next step shows where the program goes"
        )
    if not python and re.match(r"\}\s*else\s+if\b", code):
        return (
            f"line {line} starts `}} else if`: its code may be the jump that ends the branch "
            "above it or the `else if` test; the next step shows which"
        )
    return None


def _report_text(report, base=None):
    """`session report` text: the state, where the program is stopped, the session's hits
    and steps so far as its worker counted them (an older worker's report has neither),
    and the program's output. The worker's request history (`history`, several per
    command) is JSON only: a bare count of it would read as the commands run."""
    from peeky.names import display_function

    where = ""
    if report.get("state") == "stopped" and report.get("location"):
        where = f" at {_relative_location(report['location'], base)}"
        if report.get("function"):
            where += f" in {display_function(report['function'])}"
        if type(report.get("hit")) is int:
            where += f", hit {report['hit']}"
        if type(report.get("place")) is int and type(report.get("places")) is int:
            where += f", stop {report['place']} of {report['places']}"
    counts = [
        f"{count} {word}{'s' if count != 1 else ''}{what}"
        for key, word, what in (
            ("hits", "hit", " (stops at the session's breakpoint)"),
            ("steps", "step", ""),
        )
        if type(count := report.get(key)) is int
    ]
    return (
        f"state: {report['state']}{where}"
        + (f"\nso far: {', '.join(counts)}" if counts else "")
        + f"\noutput:\n{report['output'].rstrip(chr(10)) or '(none)'}"
        + (f"\nwarning: {warned}" if (warned := debugger_diagnostics({}, report)) else "")
    )


def _stop(record):
    if record.get("state") != "stopped" or not record.get("stop_id"):
        raise ValueError(
            _named("The program is not stopped; run `peeky session wait`", record.get("name"))
        )
    return {"session_id": record["session_id"], "stop_id": record["stop_id"]}


def _frame_text(snapshot, base=None):
    """`frame 1: tags (app.py:12)`: the caller frame a read came from."""
    from peeky.names import display_function

    index = snapshot.get("frame", 0)
    frame = snapshot["frames"][index] if len(snapshot["frames"]) > index else {}
    where = snapshot.get("location") or f"{frame.get('source', {}).get('path')}:{frame.get('line')}"
    function = display_function(snapshot.get("function"))
    return f"frame {index}: {function} ({_relative_location(where, base)})"


def _inspect_pointed(snapshot):
    """Says on each C or C++ pointer an inspection lists which element's address of an
    array it lists it holds (`at_element_of`), or how far it is from another pointer
    (`offset_from`), as one `session get` of them all does (probe._pointed), from each
    value's `extent`, which is then dropped."""
    from peeky.probe import _pointed

    variables, rows, extents = [], [], {}
    for scope in snapshot.get("scopes") or ():
        for variable in scope.get("variables") or ():
            extent = variable.pop("extent", None)
            if not isinstance(extent, dict) or not reachable(variable):
                continue
            row = {
                key: variable[key]
                for key in ("value", "unavailable", "note", "view", "stale")
                if key in variable
            }
            row["selector"] = variable.get("selected_as") or variable["name"]
            extents[len(rows)] = extent
            variables.append(variable)
            rows.append(row)
    _pointed(rows, extents)
    for variable, row in zip(variables, rows):
        for key in ("at_element_of", "offset_from"):
            if key in row:
                variable[key] = row[key]
    return snapshot


def _variables_text(snapshot, name="default", base=None):
    from peeky.names import display_function

    index = snapshot.get("frame", 0)
    frame = snapshot["frames"][0] if snapshot["frames"] else {}
    # Where the program stopped; a caller frame's own location is on the next line.
    stopped = snapshot.get("innermost") or {"function": frame.get("name")} if index else snapshot
    where = _relative_location(
        stopped.get("location") or f"{frame.get('source', {}).get('path')}:{frame.get('line')}",
        base,
    )
    if function := stopped.get("function"):
        where += f" in {display_function(function)}"
    if type(snapshot.get("hit")) is int:
        where += f", hit {snapshot['hit']}"
    if type(snapshot.get("place")) is int and type(snapshot.get("places")) is int:
        where += f", stop {snapshot['place']} of {snapshot['places']}"
    lines = [f"stopped at {where}" + _pause_text(snapshot)]
    if index:
        lines.append(_frame_text(snapshot, base))
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
                # A Java String or boxed number: a value, not its class's fields.
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
                # Two rows that look the same: which spelling each is.
                more += (
                    f" [written {ascii(variable['name'])}: another name here differs from it "
                    "only by Unicode normalization]"
                )
            view = f"  [{variable['view']}]" if variable.get("view") else ""
            if pointed := pointed_text(variable):
                # Which element's address a C or C++ pointer holds, as `get` says.
                view = f"  [{pointed}]" + view
            if variable.get("stale"):
                # May be moved or dropped: its fields are what it left behind.
                view += "  [stale]"
            if variable.get("settable"):
                view += "  [settable]"
            elif variable.get("not_settable"):
                # No stop of this build can change it, or this one (Java).
                view += f"  [not settable: {variable['not_settable']}]"
            elif variable.get("maybe_settable"):
                view += f"  [maybe settable: {variable['maybe_settable']}]"
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
        # What the function the step left returned, apart from the locals.
        lines.append("[Returned]")
        lines.append(f"  {_returned_text(returned)}  (session get return)")
        if returned.get("view") == "unproven" and isinstance(returned.get("note"), str):
            lines.append(f"    note: {returned['note']}")
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
    if returned.get("unavailable") and returned.get("type") == "void":
        return f"{who} returned: void (no value: {returned.get('reason')})"
    if returned.get("unavailable"):
        return f"{who} returned: <withheld: {returned.get('reason')}>"
    pointed = f"  [{pointed_text(returned)}]" if returned.get("past_end") else ""
    if returned.get("view") == "unproven" and isinstance(returned.get("note"), str):
        # Text read through a pointer Peeky can't show points at a live object.
        pointed += "  [unproven]"
    return f"{who} returned: {returned.get('type') or ''} = {returned.get('value')}{pointed}"


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


def _read_frame(row, base=None):
    """`frame 1: tags (app.py:12)`: which caller frame `get --frame` read."""
    from peeky.names import display_function

    return (
        f"frame {row.get('frame')}: {display_function(row.get('function'))} "
        f"({_relative_location(row.get('location'), base)})"
    )


def _shell_word(text):
    """``text`` as one shell word to paste: as is where plain; in double quotes where it
    holds a single quote but nothing a shell reads inside double quotes (`"`, `$`, a
    backtick, a backslash, `!`); else as shlex quotes it. Double quotes keep
    `history['alice'][0]` readable, where shlex gives `'history['"'"'alice'"'"'][0]'`."""
    quoted = shlex.quote(text)
    if quoted == text or "'" not in text or re.search(r'["$`\\!]', text):
        return quoted
    return f'"{text}"'


def _repeats_shortened(rows):
    """``rows`` for text, each `visible:` list a row above gave already said so."""
    from peeky.probe import repeated_visible

    errors = repeated_visible(rows, lambda row: "above")
    return [{**row, "error": errors[i]} if i in errors else row for i, row in enumerate(rows)]


# `session get` lists an array's first elements in text, as a probe's preview does.
_GET_ELEMENTS = 8


def _element_count(row):
    """An array's length where its type or value states it: C and C++ `Track[100]`, Rust
    `[u32; 100]`, Java `int[100]@55 …`; a C++ container's count its value states
    (`size=700 {…}`, which Peeky checked against the chain of one it read from its
    nodes); else None."""
    kind, value = str(row.get("type") or ""), str(row.get("value") or "")
    found = (
        re.fullmatch(r"[^\[\]]+\[([0-9]+)\]", kind)
        or re.fullmatch(r"\[[^;\[\]]+; ([0-9]+)\]", kind)
        or re.match(r"[\w.$]+\[([0-9]+)\]@[0-9]+ ", value)
        or re.match(r"(?:0x[0-9a-fA-F]+ )?size=([0-9]{1,9})(?: [\[{]|$)", value)
    )
    return int(found[1]) if found else None


def _get_lines(row, session="default"):
    """`session get` text: the value, then one level of children with their selectors."""
    if "value" not in row and not row.get("unavailable"):
        # Nothing was read: there is no type or value to show, only why.
        lines = [f"{row['selector']}: <{row.get('error', '')}>"]
    elif row.get("unavailable") and row.get("type") == "void":
        # A function that returns nothing: no value was withheld (C, C++).
        lines = [f"{row['selector']}: void ({row.get('error', 'no value')})"]
    else:
        value = row.get("value", "<" + row.get("error", "") + ">")
        lines = [
            f"{row['selector']}: {row.get('type') or ''} = {value}"
            + (f"  [{pointed}]" if (pointed := pointed_text(row)) else "")
            + (f"  [id {row['identity']}]" if row.get("identity") else "")
            + ("  [module global]" if row.get("scope") == "globals" else "")
            + ("  [unproven]" if row.get("view") == "unproven" else "")
            + ("  [stale]" if row.get("stale") else "")
            + ("  [settable]" if row.get("settable") else "")
            + (f"  [not settable: {row['not_settable']}]" if row.get("not_settable") else "")
            + (f"  [maybe settable: {row['maybe_settable']}]" if row.get("maybe_settable") else "")
        ]
    children = row.get("children", [])
    # An array's elements past the first few, as a probe's preview shows them; `--json`
    # lists all.
    elements = [
        child
        for child in children
        if re.fullmatch(
            re.escape(str(row.get("selector"))) + r"\[[0-9]+\]", str(child.get("selector"))
        )
    ]
    options = (f" --name {shlex.quote(session)}" if session != "default" else "") + (
        f" --frame {row['frame']}" if row.get("frame") else ""
    )
    hidden = elements[_GET_ELEMENTS:] if len(elements) == len(children) else []
    for child in children[: len(children) - len(hidden)]:
        name = child.get("selector") or f"{child['name']} [not selectable]"
        more = (
            f" (children: peeky session get {_shell_word(child['selector'])}"
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
        if (child.get("past_end") or child.get("read_through")) and (
            pointed := pointed_text(child)
        ):
            # One past an array's end: nothing it points at is read. Or read at
            # the address another pointer `session get` was given holds.
            shown += f"  [{pointed}]"
        if child.get("view") == "unproven":
            shown += "  [unproven]"
        if child.get("stale"):
            shown += "  [stale]"
        if child.get("settable"):
            shown += "  [settable]"
        elif child.get("not_settable"):
            shown += f"  [not settable: {child['not_settable']}]"
        elif child.get("maybe_settable"):
            shown += f"  [maybe settable: {child['maybe_settable']}]"
        if child.get("note") and not child.get("unavailable") and child.get("view") != "unproven":
            # Read through a local shown with a note, given below; `[unproven]` says it
            # for one read through a pointer.
            shown += "  [note]"
        lines.append(f"  {name}: {child.get('type') or ''} = {value}{shown}{more}")
    if hidden and row.get("children_truncated"):
        # The listing is one page of the debugger's (64): JSON has no more, and the count
        # is of that page; the whole length only where the type gives it.
        first, last = hidden[0]["selector"], hidden[-1]["selector"]
        size = _element_count(row)
        # A C++ container's count (`size=700 {…}`): how many more, and of what.
        counted = str(row.get("value") or "").startswith("size=") and size
        keyed = counted and "{" in str(row["value"])
        lines.append(
            f"  ... {len(hidden)} more listed, {first} to {last}, then "
            + (f"{size - len(elements)} more" if counted and size > len(elements) else "more")
            + (f" ({size} {'entries' if keyed else 'elements'} in all)" if size else "")
            + f": `--json` has only the first {len(elements)}; read any "
            + ("entry by key" if keyed else "element by index")
            + f", as `peeky session get {_shell_word(first)}{options}`"
        )
    elif hidden:
        first, last = hidden[0]["selector"], hidden[-1]["selector"]
        lines.append(
            f"  ... {len(hidden)} more elements, {first} to {last}: `peeky session get "
            f"{_shell_word(first)}{options}` reads one; `--json` lists them all"
        )
    elif row.get("children_truncated"):
        size = _element_count(row) if str(row.get("value") or "").startswith("size=") else None
        lines.append(
            f"  ... {size - len(children)} more entries ({size} in all); select one by name or "
            "index"
            if size and size > len(children)
            else "  ... more children; select one by name or index"
        )
    if row.get("children_grouped") and not row.get("children"):
        # Said only where nothing else is listed (on every dataclass it would be noise);
        # `--json` always has `children_grouped`.
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
        # Why a Rust Vec or String field shows LLDB's summary, not its contents.
        verb = "shows" if len(names) == 1 else "show"
        lines.append(f"  note: {', '.join(names)} {verb} LLDB's summary, not contents: {reason}")
    return lines


def session(args):
    _NOTES.clear()
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
    _NOTES[:] = [record["note"]] if record.get("note") else []
    # Paths as `session start` named them (from the directory it ran in).
    base = record.get("cwd") if isinstance(record.get("cwd"), str) else None
    if args.action == "wait":
        state = _wait(record, client, args.timeout)
        _emit(state, args.json, lambda shown: _state_text(shown, args.name, base))
    elif args.action == "inspect":
        if args.globals and record.get("language") not in (None, "python"):
            raise ValueError("--globals lists a Python module's globals; this is not Python")
        snapshot = client.call(
            "inspect",
            **_stop(record),
            **({"frame": args.frame} if args.frame else {}),
            # Listing a global runs its repr in the program, so only on request.
            **({"globals": True} if args.globals else {}),
            # Which element's address a C or C++ pointer holds, as `session get` says.
            **({"extents": True} if record.get("language") in ("c", "cpp") else {}),
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
        _inspect_pointed(snapshot)
        _emit(snapshot, args.json, lambda shown: _variables_text(shown, args.name, base))
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
            # is. `--frame N` names its frame already.
            place = _stop_place(record, unresolved[0])
            change = record.get("call_change")
            call = _call_text(change, since=True) if isinstance(change, dict) else None
            if call:
                # Why the earlier stop's names may be gone: the step changed function.
                place += f", {call}"
            for row in unresolved:
                row.setdefault("location", record.get("location"))
                if call:
                    row.setdefault("call_change", change)
        _emit(
            rows,
            args.json,
            lambda rows: "\n".join(
                [_read_frame(rows[0], base)] * bool(args.frame and rows)
                + [line for row in _repeats_shortened(rows) for line in _get_lines(row, args.name)]
                + ([f"note: this stop is in {place}"] if place else [])
                + ([_OPTIMIZED_NOTE.format(record["optimized"])] if record.get("optimized") else [])
            ),
        )
        if failed := [row["selector"] for row in rows if "error" in row]:
            # Every row was still printed; the exit status says one selector failed, and
            # this line which. The rows first: a pipe buffers stdout, so the line it calls
            # "above" would come before them where both streams share one.
            sys.stdout.flush()
            print(
                f"peeky: no value for {', '.join(failed[:10])}"
                + (f" and {len(failed) - 10} more" if len(failed) > 10 else "")
                + " (each row above says why), so the exit status is 1",
                file=sys.stderr,
            )
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
        _emit(state, args.json, lambda shown: _state_text(shown, args.name, base))
        if not complete:
            return 3
    elif args.action == "step":
        kind = "in" if args.into else "out" if args.out else "over"
        client.call("step", **_stop(record), kind=kind, calls=True)
        state = _wait(record, client, args.timeout)
        if _counted(state) and state.get("reason") == "breakpoint":
            # The code the step ran reached the session's breakpoint first (a recursive call,
            # or the rest of the function on `--out`): the step is over.
            state["note"] = (
                f"the session's breakpoint (hit {state['hit']}) stopped the step before it "
                "ended, and the step is over"
            )
        elif (
            kind != "out"
            and state.get("reason") == "step"
            and (note := _step_onto_breakpoint(record, state) or _step_line_note(record, state))
        ):
            state["note"] = note
        returned = None
        if kind == "out" and state["state"] == "stopped" and record.get("language") != "python":
            # What the function returned, as `session get return` reads it, shown with the
            # stop; none where the step did not end at a return.
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
                    _state_text(shown, args.name, base),
                    *(_get_lines(returned, args.name) if returned else []),
                ]
            ),
        )
    elif args.action == "report":
        report = client.call("report", session_id=record["session_id"])
        _emit(report, args.json, lambda r: _report_text(r, base))
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
        # What became of the program the session started.
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


# The worker's token, by the names `main` took out of the environment (take_credentials).
_CREDENTIALS = {}


REMOTE_PYTHON_NOTE = (
    "Only the .py files under the target's directory are compared with yours. Not compared: "
    "installed packages, any directory holding a pyvenv.cfg, the standard library, and compiled "
    "modules (.so, .pyc)."
)


def _remote(url):
    """A client for a worker reached over loopback (e.g. an SSH -L tunnel)."""
    if not LOOPBACK_URL.fullmatch(url or ""):
        raise ValueError(
            "--worker must be a loopback URL (http://127.0.0.1:PORT); for a remote worker, "
            "ssh -N -o ExitOnForwardFailure=yes -L 8765:127.0.0.1:8765 user@host"
        )
    # Taken out of the environment by `main`; a caller in this process may set them later.
    present = {name: os.environ[name] for name in CREDENTIAL_ENV if name in os.environ}
    token = client_token({**present, **_CREDENTIALS})
    client = _RemoteClient(url, token)
    version = client.call("version")
    protocol = version.get("protocol") if isinstance(version, dict) else None
    if protocol != PROTOCOL_VERSION:
        raise ValueError(
            f"Worker speaks protocol {protocol}, this client {PROTOCOL_VERSION}: install the "
            f"same Peeky version on both hosts (protocol {PROTOCOL_VERSION} compares every "
            "source with yours before launching, and marks which paths in a result are yours)"
        )
    # Which sources the worker's target reads: their local copies are hashed and sent.
    client.sources = version.get("sources")
    client.worker_root = version.get("root") if isinstance(version.get("root"), str) else None
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
    """(client, the request with every source's expected hash, the program's and entry
    script's expected hashes) for a remote probe, checked before it is sent."""
    from peeky.sources import client_sources

    client, _ = _remote(args.worker)
    client.client_root = root
    inputs = client_sources(root, client.sources)
    expected = _expected(root, request.program, request.entry)
    client.hashed = _hashed(root, client.sources, inputs, expected, request.program, request.entry)
    client.names = frozenset(inputs) | frozenset(client.hashed)
    request = replace(request, expect_inputs=tuple(sorted(inputs.items())))
    return client, request, expected


def _inputs_match(root, client, expected, reported):
    """Raise unless the worker confirms it compared every source with ours before
    launching (``inputs_checked``), and read the same sources (``inputs_sha256``)."""
    from peeky.sources import digest

    own = digest(expected)
    if reported.get("inputs_checked") != own:
        raise RuntimeError(
            "Worker source provenance is unconfirmed: the worker did not report comparing "
            "every source with yours before launching. Install the same Peeky version on both "
            "hosts"
        )
    read = reported.get("inputs_sha256")
    if read is None:
        raise RuntimeError(
            "Worker source provenance is unconfirmed: the worker did not report which sources "
            "the run read (inputs_sha256). Install the same Peeky version on both hosts"
        )
    if (getattr(client, "sources", None) or {}).get("kind") == "python":
        own = _python_inputs(root)
    if read != own:
        raise RuntimeError(
            f"Worker source provenance differs: the sources the worker read (inputs_sha256 "
            f"{str(read)[:12]}) are not yours ({str(own)[:12]}). Sync sources before debugging "
            "remotely"
        )


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
        module=request.module,
        args=request.args,
        # The worker refuses before launching when its sources differ.
        expect_sha256=expected,
        expect_inputs=dict(request.expect_inputs),
        edit=request.edit,
        set_at=request.set_at,
    )
    # A probe answer has one of these statuses, or is a probe child's bare error; not
    # just any JSON object.
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
                **{k: v for k, v in asdict(request).items() if k != "expect_inputs"},
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
        if ran or result.get("source_sha256") is not None:
            _inputs_match(root, client, dict(request.expect_inputs), result)
        # Your own checkout must still be what was compared.
        _checkout_unchanged(root, client.sources, dict(request.expect_inputs))
        if (getattr(client, "sources", None) or {}).get("kind") == "python":
            result["warnings"].append(_python_note(getattr(client, "hashed", None)))
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
    from peeky.sources import client_sources, difference

    client, token = _remote(args.worker)
    client.client_root = root
    inputs = client_sources(root, client.sources)
    entry = _entry(root, args.run, language)
    expected = _expected(root, program, entry)
    hashed = _hashed(root, client.sources, inputs, expected, program, entry)
    client.names = frozenset(inputs) | frozenset(hashed)
    from peeky.errors import PeekyError

    try:
        launch = client.call(
            "launch",
            program=program,
            breakpoints=[{"line": line}],
            max_pause=max_pause,
            **({"entry": entry} if entry else {}),
            **({"module": module} if (module := _module(args, language)) else {}),
            **({"args": list(args.arg)} if args.arg else {}),
            expect_sha256=expected,
            expect_inputs=inputs,
            protocol=PROTOCOL_VERSION,
        )
    except PeekyError as exc:
        exc.payload = _remote_build_required(exc.payload, args)
        raise
    try:
        _source_matches(root, program, launch.get("source_sha256"), "program")
        if entry:
            _source_matches(root, entry, launch.get("entry_sha256"), "entry script")
        _inputs_match(root, client, inputs, launch)
        _checkout_unchanged(root, client.sources, inputs)
        # A compiled target's launch names each source its build read: name the one that
        # differs.
        if isinstance(manifest := launch.get("source_manifest"), dict) and (
            what := difference(inputs, manifest)
        ):
            raise RuntimeError(
                f"Worker source provenance differs for {what}: the session was closed. Sync "
                "sources before debugging remotely"
            )
    except Exception:
        client.call("close", session_id=launch["session_id"])
        raise
    record = {"name": args.name, "url": args.worker, "token": token, "remote": True}
    record.update(root=str(root), program=program, language=language, cwd=os.getcwd(), line=line)
    # What a later command checks again before it uses this session.
    record.update(protocol=PROTOCOL_VERSION, inputs_checked=launch["inputs_checked"])
    if hashed:
        record["hashed"] = hashed
    if (client.sources or {}).get("kind") == "python":
        record["note"] = _python_note(hashed)
    record.update(session_id=launch["session_id"], state="running", stop_id=None)
    if launch.get("optimized"):
        record["optimized"] = launch["optimized"]
    if python := _launched_python(launch):
        record["python"] = python
    if isinstance(launch.get("java"), dict):
        record["java"] = launch["java"]
    _save(record)
    state, complete = _until_hit(record, client, args.hit, args.timeout)
    if record.get("optimized"):
        state["optimized"] = record["optimized"]
    if record.get("python"):
        state["python"] = record["python"]
    if record.get("java"):
        state["java"] = record["java"]
    if launch.get("warnings"):
        state["warnings"] = launch["warnings"]
    if moved := _moved_start(launch, state, line, program):
        state["warnings"] = [moved, *state.get("warnings", [])]
    if misplaced := _misplaced_options(args):
        state["warnings"] = [*state.get("warnings", []), misplaced]
    if (client.sources or {}).get("kind") == "python":
        state["warnings"] = [*state.get("warnings", []), _python_note(hashed)]
    _emit(
        {"session": args.name, "language": language, "worker": args.worker, **state},
        args.json,
        lambda s: (
            f"session {s['session']} ({s['language']}, remote): "
            + _state_text(s, s["session"])
            + (f"\n{line}" if (line := _python_line(s.get("python"))) else "")
            + (f"\n{line}" if (line := _java_line(s.get("java"))) else "")
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
    root, program, line, language, data = resolve(
        args.target, args.at, args.config, tools=args.worker is None
    )
    if line is None:
        raise ValueError("session start requires --at <path>:<line>")
    if "binary" in (data or {}):
        from peeky.errors import Refused

        # A session runs in a worker, which reads no binary target's config yet.
        raise Refused(
            "INVALID_REQUEST",
            "A binary target takes `peeky probe` and `peeky describe` in this version of "
            "Peeky; sessions come with its worker support",
        )
    _python_option(args, language)
    _seconds(args.timeout)
    max_pause = _max_pause(args.max_pause, language)
    _workspace_ready(args, root, language, data)
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
        lambda s: (
            f"session {s['session']} ({s['language']}): "
            + _state_text(s, s["session"])
            + (f"\n{line}" if (line := _python_line(s.get("python"))) else "")
            + (f"\n{line}" if (line := _java_line(s.get("java"))) else "")
        ),
    )
    # 3: the session exists but is not at the requested hit (see the note).
    return 3 if state.pop("incomplete", False) else 0


def _workspace_ready(args, root, language, data):
    """A session on a Rust workspace: its build first with --build, then a current one,
    checked here so that a refusal names the build command as typed."""
    from peeky.rust_build import RustWorkspaceTarget

    if args.worker is not None:
        _build_first(args, root, None)
        return
    target = adapter_for(language, data, build=_build_command(args))
    _build_first(args, root, target)
    if isinstance(target, RustWorkspaceTarget):
        target.current(root)


def _moved_start(launch, state, line, program):
    """The probe's warning where `session start` stopped at its breakpoint on another line
    than `--at` asked, where the debugger placed it (one with no code of its own), with
    any later move its `breakpoint` events report; else None. As a probe's, only after a
    real stop there: "values are from line N" says nothing true of a run that never
    stopped, or stopped elsewhere."""
    from peeky.probe import _expected_line, moved_breakpoint

    placed = (launch.get("breakpoints") or [None])[0]
    if not isinstance(placed, dict) or state.get("state") != "stopped":
        return None
    actual = _expected_line(placed, state, line)
    if type(actual) is not int or actual == line or state.get("reason") != "breakpoint":
        return None
    path, _, at = str(state.get("location") or "").rpartition(":")
    path = path.replace("\\", "/")
    if at != str(actual) or not (path == program or path.endswith("/" + program)):
        return None
    return moved_breakpoint(line, actual)


def _start_local(args, root, program, line, language, data, max_pause):
    import peeky

    token = secrets.token_urlsafe(32)
    # Isolated, importing this exact Peeky by absolute path, and run in Peeky's own
    # directory: a `peeky` package in the current directory or on PYTHONPATH never runs.
    bootstrap = (
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        "from peeky.worker import main; sys.exit(main())"
    )
    command = [sys.executable, "-I", *(["-B"] if sys.dont_write_bytecode else [])]
    command += ["-c", bootstrap, str(Path(peeky.__file__).resolve().parents[1])]
    command += ["--root", str(root), "--port", "0"]
    # A session's own worker ends with the session, including after its idle timeout.
    command.append("--exit-when-idle")
    if (python := getattr(args, "python", None)) is not None:
        from peeky.python_interpreter import absolute_option

        # The worker runs in another directory.
        command += ["--python", absolute_option(python)]
    if data is not None:
        config = _state_dir() / f"{args.name}.target.json"
        _private_write(config, json.dumps(data, default=list))
        command += [f"--{'native' if language in ('c', 'cpp') else language}-config", str(config)]
        from peeky.rust_workspace import is_workspace_config

        typed = _build_command(args)
        if language == "rust" and is_workspace_config(data) and typed is not None:
            # The worker reads a private copy of the configuration: its refusals name the
            # command as the user would type it.
            command += ["--rust-build-command", typed]
    log_path = _state_dir() / f"{args.name}.log"
    log = os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w")
    process = subprocess.Popen(
        command,
        cwd=_state_dir(),
        # Its own token, by the name the worker reads; never a remote worker's.
        env={**without_credentials(os.environ), "DEBUG_TOKEN": token},
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
        record.update(
            root=str(root), program=program, language=language, cwd=os.getcwd(), line=line
        )
        # Its worker's protocol, asked before anything runs and kept: each later command
        # checks it before it sends a request.
        version = client.call("version")
        protocol = version.get("protocol") if isinstance(version, dict) else None
        if protocol != PROTOCOL_VERSION:
            raise RuntimeError(
                f"Worker speaks protocol {protocol}, this client {PROTOCOL_VERSION}: the "
                f"worker `{sys.executable} -m peeky.worker` runs another Peeky version; "
                "install one Peeky for both"
            )
        record["protocol"] = protocol
        launch = client.call(
            "launch",
            program=program,
            breakpoints=[{"line": line}],
            max_pause=max_pause,
            **({"entry": entry} if (entry := _entry(root, args.run, language)) else {}),
            **({"module": module} if (module := _module(args, language)) else {}),
            **({"args": list(args.arg)} if args.arg else {}),
        )
        record.update(session_id=launch["session_id"], state="running", stop_id=None)
        if launch.get("optimized"):
            record["optimized"] = launch["optimized"]
        if python := _launched_python(launch):
            record["python"] = python
        if isinstance(launch.get("java"), dict):
            record["java"] = launch["java"]
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
    if record.get("python"):
        state["python"] = record["python"]
    if record.get("java"):
        state["java"] = record["java"]
    if not complete:
        state["incomplete"] = True
    if launch.get("warnings"):
        state["warnings"] = launch["warnings"]
    if moved := _moved_start(launch, state, line, program):
        state["warnings"] = [moved, *state.get("warnings", [])]
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
    """Whether a stop is one of the session's numbered breakpoint stops."""
    return state["state"] == "stopped" and type(state.get("hit")) is int


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
        state["note"] = (
            f"reached {reached} of {more} stop{'s' if more > 1 else ''} before --timeout ({target})"
        )
    else:
        state["note"] = f"the program is still running ({target}); `peeky session wait`"
    if type(last) is int:
        state["note"] += f"; last hit {last}"
    return state, reached, False


def _reached_line(state, line):
    """Whether `continue --to LINE` stopped at LINE; if not, a note says where it stopped
    first, as `--count` and `--hit` do: the `--to` line
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
            f"this stop has no hit number; stopped at the first stop instead of hit {hit}"
        )
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
        from peeky.python_interpreter import find

        # The checker runs the candidate on the program's interpreter, as a probe does.
        interpreter = find(root, args.python)
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
            **run_checker(check, candidate, root, deadline=deadline, python=interpreter.executable),
            "candidate": str(candidate),
        }
        result["candidate_sha256"] = candidate_sha256
        result["python"] = interpreter.identity()
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


CHECK_COMMAND = "--check-command"
# What a shell would read as its own syntax: --check-command runs no shell.
_SHELL_SYNTAX = frozenset("|&;<>()$`\\*?[]{}~!#\n\r")
_SHOWN = {"\n": "a newline", "\r": "a carriage return", "`": "a backquote"}


def _check_command_words(args):
    """The words of --check-command (None for --check): a string split into words as a
    shell splits them, with no shell syntax in it, or the words after `--check-command
    --`, as given."""
    text, words = args.check_command, args.check_argv
    if text is not None and words is not None:
        raise ValueError(
            "Pass --check-command once: as one string, or as words after `--check-command --`"
        )
    if args.check is not None and (text is not None or words is not None):
        raise ValueError("Pass either --check or --check-command, not both")
    if text is None and words is None:
        if args.check is None:
            raise ValueError(
                "verify needs a check: --check <checker.py>, or for Python, --check-command "
                "'<test command>'"
            )
        return None
    if words is None:
        if found := sorted(set(text) & _SHELL_SYNTAX):
            shown = ", ".join(_SHOWN.get(char, f"`{char}`") for char in found)
            raise ValueError(
                f"--check-command takes no shell syntax ({shown}): Peeky runs the command "
                "itself, with no shell. For a pipe or a redirect, run a script. To pass these "
                "characters as text, as in a test id with `[`, put the command's words after "
                "`--check-command --`, last"
            )
        try:
            words = shlex.split(text)
        except ValueError as error:
            raise ValueError(f"--check-command could not be split into words: {error}") from None
    if not words or not words[0]:
        raise ValueError(
            "--check-command needs a command, such as 'python -m pytest tests/test_x.py -q'"
        )
    if "=" in words[0]:
        raise ValueError(
            f"--check-command's first word, {words[0]}, must be the program to run: Peeky runs "
            "no shell, so set a variable in the environment you run peeky in"
        )
    tool = Path(words[0]).name if Path(words[0]).name in ("tox", "nox") else _module_run(words)
    if tool in ("tox", "nox"):
        raise ValueError(
            f"--check-command doesn't run {tool}: tox and nox build their own environments, "
            "outside the copy. Give the test command they run, such as 'python -m pytest "
            "tests/test_x.py -q'"
        )
    return words


# A Python interpreter's name (`python`, `python3.12`), as a command or a script's `#!`.
_PYTHON_NAME = re.compile(r"python[0-9.]*")
# Interpreter options that take no value: `python -I -B -m pytest`.
_PYTHON_FLAGS = frozenset("bBdEhiIOPqsSuvx")


def _python_start(args):
    """How `python ARGS` starts: (flags, how). ``flags``: those of -I, -E, -P, -s and -S it
    takes, which decide its import path. ``how``: ("module", index of the module's word),
    ("command", None), ("stdin", None), ("script", the script's word), or (None, None) for
    an option Peeky doesn't know."""
    flags, at = [], 0
    while at < len(args):
        word = args[at]
        at += 1
        if word == "-" or not word.startswith("-"):
            return flags, ("stdin", None) if word == "-" else ("script", word)
        if word.startswith("--"):
            return flags, (None, None)
        for place, flag in enumerate(word[1:], 1):
            if flag in "IEPsS":
                if f"-{flag}" not in flags:
                    flags.append(f"-{flag}")
                continue
            if flag in _PYTHON_FLAGS:
                continue
            if flag in "WX":
                # Its value is the rest of the word, or the next word.
                if place == len(word) - 1:
                    at += 1
                break
            if flag == "m":
                return flags, ("module", at - 1 if place < len(word) - 1 else at)
            if flag == "c":
                return flags, ("command", None)
            return flags, (None, None)
    return flags, ("stdin", None)


def _module_at(words):
    """(module, index of its word) that `python [options] -m MODULE` runs, or None."""
    if not words or not _PYTHON_NAME.fullmatch(Path(words[0]).name):
        return None
    _, (how, at) = _python_start(words[1:])
    if how != "module" or 1 + at >= len(words):
        return None
    word = words[1 + at]
    if word.startswith("-"):
        module = word[word.index("m") + 1 :]
        return (module, 1 + at) if module else None
    return word, 1 + at


def _module_run(words):
    """The module `python [options] -m MODULE ...` runs, or None: not an interpreter's
    command line, a script or `-c`, or an option Peeky doesn't know."""
    found = _module_at(words)
    return found and found[0]


def _runner(words):
    """The test runner a check command runs, as its words say: `pytest` (`pytest`,
    `py.test`, `python ... -m pytest`), `unittest` (`python ... -m unittest`), or None."""
    if not words:
        return None
    if Path(words[0]).name in ("pytest", "py.test"):
        return "pytest"
    return {
        "pytest": "pytest",
        "pytest.__main__": "pytest",
        "unittest": "unittest",
        "unittest.__main__": "unittest",
    }.get(_module_run(words))


def _command_environment(interpreter, candidate):
    """The environment a check command runs in: Peeky's, without the worker's token, as
    a launched program gets it, with the interpreter's directory first on PATH, its other
    entries inside the root moved to the copy (relative and empty ones are the copy's
    root's, where the command runs), and the interpreter's virtual environment, if it is
    in one, as VIRTUAL_ENV."""
    from peeky.python_interpreter import PROGRAM_ENV

    environment = {**without_credentials(os.environ), **PROGRAM_ENV}
    executable = interpreter.executable or interpreter.path
    entries = []
    for path in (executable, interpreter.path):
        if (folder := os.path.dirname(path)) and folder not in entries:
            entries.append(folder)
    for entry in (environment.get("PATH", os.defpath) or "").split(os.pathsep):
        if not os.path.isabs(entry):
            entry = os.path.normpath(os.path.join(candidate.root, entry))
        elif moved := candidate.copy_path(entry):
            entry = moved
        entries.append(entry)
    environment["PATH"] = os.pathsep.join(entries)
    home = Path(executable).parent.parent
    if (home / "pyvenv.cfg").is_file():
        environment["VIRTUAL_ENV"] = str(home)
    else:
        environment.pop("VIRTUAL_ENV", None)
    # As a shell sets it there.
    environment["PWD"] = str(candidate.root)
    if candidate.pythonpath is not None:
        environment["PYTHONPATH"] = candidate.pythonpath
    return environment


def _check_command_arguments(words, root, where="argument"):
    """Refuse a check command's argument (or a word of ``where``, as `PYTEST_ADDOPTS`)
    that names a path inside ``root`` by its absolute path: the command would read your
    files, not the copy's."""
    for word in words:
        for part in (word, *word.split("=", 1)[1:]):
            if os.path.isabs(part) and any(
                Path(path).is_relative_to(root)
                for path in (os.path.normpath(part), os.path.realpath(part))
            ):
                raise ValueError(
                    f"--check-command's {where} {word} names a path in your project, not in "
                    "the copy the command runs in: give it relative to the root"
                )


def _same_python(path, interpreter):
    """Whether ``path`` is the program's interpreter: the same file, in the same directory
    (a virtual environment's `python3` is its `python`, also through the copy's link to
    its `bin`; another environment's is not)."""
    for chosen in {interpreter.path, interpreter.executable} - {"", None}:
        try:
            if os.path.realpath(os.path.dirname(os.path.abspath(path))) == os.path.realpath(
                os.path.dirname(os.path.abspath(chosen))
            ) and os.path.samefile(path, chosen):
                return True
        except (OSError, ValueError):
            continue
    return False


# pip's first lines for a script whose interpreter's path is too long for `#!`.
_SH_EXEC = re.compile(r"'''exec' \"?(.+?)\"? \"\$0\" \"\$@\"")


def _script_python(path, environment):
    """The Python a script's first line runs it on, and that line's options for it
    (`#!/p/bin/python -I`, `#!/usr/bin/env python3`, or pip's `#!/bin/sh` with
    ``_SH_EXEC`` next), or None: not a Python script."""
    try:
        with open(path, "rb") as stream:
            head = stream.read(4096)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    lines = head.decode(errors="replace").split("\n")
    words = lines[0][2:].split()
    if not words:
        return None
    target, options = words[0], words[1:]
    if Path(target).name == "env":
        named = next((word for word in words[1:] if not word.startswith("-")), None)
        target = named and (shutil.which(named, path=environment.get("PATH")) or named)
        options = words[words.index(named) + 1 :] if named else []
    elif Path(target).name == "sh" and len(lines) > 1:
        found = _SH_EXEC.match(lines[1])
        target, options = found and found[1], []
    if not target or not _PYTHON_NAME.fullmatch(Path(target).name):
        return None
    return target, options


def _command_program(word, candidate, environment, interpreter):
    """What a check command's first word runs, in the copy: `python` and `python3` are the
    program's interpreter; a path with a `/` is that file (a relative one in the copy's
    root); another name is found on the command's PATH. A file inside the root is the
    copy's. Another Python, and a Python script that runs on another, are refused."""
    executable = interpreter.executable or interpreter.path
    if word in ("python", "python3"):
        return executable
    if "/" in word:
        path = os.path.normpath(os.path.join(candidate.root, word))
        if not os.path.isabs(word) and not Path(path).is_relative_to(candidate.root):
            raise ValueError(
                f"--check-command's program {word} is outside the root, which is all the copy "
                "holds: give its absolute path"
            )
    else:
        path = shutil.which(word, path=environment.get("PATH"))
        if path is None:
            raise ValueError(
                f"--check-command's program {word} is not on PATH, with the interpreter's "
                f"directory first ({environment['PATH'].split(os.pathsep)[0]}): install it in "
                "the project's environment, or give its path"
            )
    # Your file runs as the copy's, so what it reads and imports beside it is the copy's.
    path = candidate.copy_path(path) or path
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise ValueError(f"--check-command's program {word} is not an executable file")
    if _PYTHON_NAME.fullmatch(Path(path).name) and not _same_python(path, interpreter):
        raise ValueError(
            f"--check-command's program {word} is {path}, not the program's interpreter "
            f"({executable}): run `python`, or pass --python"
        )
    script = _script_python(path, environment)
    if script is not None and not _same_python(script[0], interpreter):
        raise ValueError(
            f"--check-command's program {word} ({path}) runs on {script[0]}, not the "
            f"program's interpreter ({executable}): install it in the project's environment, "
            "or run `python -m <module>`"
        )
    return path


def _invocation(ran, candidate, environment, interpreter):
    """How the command's own Python starts, for ``command_imports``: (python, flags,
    first entry of its import path). An interpreter's command line: its options, and the
    working directory first for -m, -c and stdin, or the script's directory (its links
    followed). A Python script, such as a console script: the Python and options its
    first line names, and its own directory. Any other program: the program's
    interpreter, with nothing first, as Peeky can't see what Pythons it starts."""
    program = ran[0]
    if _PYTHON_NAME.fullmatch(Path(program).name):
        flags, (how, script) = _python_start(ran[1:])
        if how == "script":
            folder = os.path.dirname(os.path.realpath(os.path.join(candidate.root, script)))
            return program, flags, folder
        return program, flags, "" if how in ("module", "command", "stdin") else None
    if (script := _script_python(program, environment)) is not None:
        flags, _ = _python_start(script[1])
        return script[0], flags, os.path.dirname(os.path.realpath(program))
    return interpreter.executable or interpreter.path, [], None


# Peeky's watch on a check command's Python, as a module of a name of the run's own (no
# module of the project's can stand in for it), with that run's nonce and report folder
# written in, in a directory of Peeky's at the end of the command's PYTHONPATH. Peeky's
# bootstrap installs it before the command runs: an audit hook on process starts, and
# multiprocessing's BaseProcess.start. As the process exits, it writes a report: the
# nonce, its own file, each module's file, and the processes started. It detects accidental
# mistakes, not hostile code, which runs as you.
_WATCH_TEXT = '''"""Peeky's watch on a check command's Python (`peeky verify --check-command`).

PYTEST_DONT_REWRITE: Peeky's bootstrap imports it before pytest could rewrite it.
"""

import atexit
import json
import multiprocessing.process as _process
import os
import shlex
import sys
import threading

NONCE = {nonce!r}
FOLDER = {folder!r}
# The processes started: "unknown" counts all but those of ``_LEAVES``.
STARTED = {{"count": 0, "unknown": 0, "examples": []}}
# A process start's own events (subprocess.Popen's os.posix_spawn, a multiprocessing
# start's os.fork) are that one start: counted once.
_SPAWNING = threading.local()
# Programs that start no others, by their file's real name: whatever they are given, they
# run nothing (no `-exec`, `system()`, compress program or editor). Any other program
# started may start Python, which Peeky can't follow.
_LEAVES = frozenset({{
    "basename", "cat", "chmod", "cksum", "cmp", "comm", "cp", "cut", "date", "df", "diff",
    "dirname", "du", "echo", "false", "grep", "head", "id", "ln", "ls", "md5", "md5sum",
    "mkdir", "mv", "od", "paste", "printenv", "printf", "pwd", "readlink", "realpath", "rm",
    "rmdir", "seq", "sha1sum", "sha256sum", "sleep", "stat", "tail", "tee", "touch", "tr",
    "true", "uname", "uniq", "wc", "whoami", "yes",
}})
# A native executable's first bytes: ELF, Mach-O (both byte orders, 32 and 64 bits), and a
# universal (fat) Mach-O.
_NATIVE = (b"\\x7fELF", b"\\xfe\\xed\\xfa\\xce", b"\\xfe\\xed\\xfa\\xcf", b"\\xce\\xfa\\xed\\xfe",
           b"\\xcf\\xfa\\xed\\xfe", b"\\xca\\xfe\\xba\\xbe", b"\\xbe\\xba\\xfe\\xca")
# Who installed the watch: "bootstrap", before the command ran; "plugin", the pytest
# plugin as it loaded (a pytest-xdist worker, which execnet starts), after what loaded
# before it. The process the bootstrap started writes the report at its exit.
INSTALLED = [None]
MAIN = [None]
# Whether the command ended on an uncaught KeyboardInterrupt, as on Ctrl-C.
INTERRUPTED = [False]


def _words(args):
    if isinstance(args, (str, bytes)):
        text = os.fsdecode(args)
        try:
            return shlex.split(text)
        except ValueError:
            return text.split()
    try:
        return [os.fsdecode(arg) for arg in args]
    except TypeError:
        return []


def _resolve(program, cwd, env):
    """The file the child runs for ``program``: a path is its cwd's; a name is looked
    up on its own PATH (relative entries are its cwd's)."""
    if not program:
        return None
    if os.sep in program:
        found = [os.path.join(cwd, program)]
    else:
        path = os.environ.get("PATH") if env is None else (env.get("PATH") or env.get(b"PATH"))
        path = os.fsdecode(path) if path else os.defpath
        found = [os.path.join(cwd, entry, program) for entry in path.split(os.pathsep)]
    return next((each for each in found if os.path.isfile(each) and os.access(each, os.X_OK)), None)


def _kind(program, cwd, env):
    """("other", None) for a native program of ``_LEAVES``, found as the child finds it;
    else ("unknown", why or None)."""
    path = _resolve(program, cwd, env)
    if path is None:
        return "unknown", "Peeky can't find it"
    real = os.path.realpath(path)
    if os.path.basename(real) not in _LEAVES:
        return "unknown", None
    try:
        with open(real, "rb") as stream:
            native = stream.read(4).startswith(_NATIVE)
    except OSError:
        native = False
    return ("other", None) if native else ("unknown", None)


def _started(words, kind, why):
    STARTED["count"] += 1
    STARTED["unknown"] += kind == "unknown"
    listed = [each for each in STARTED["examples"] if each["kind"] == kind]
    if len(listed) < 5:
        STARTED["examples"].append({{"args": words[:6], "kind": kind, "why": why}})


def _audit(event, args):
    try:
        if getattr(_SPAWNING, "multiprocessing", False):
            return
        cwd, env = os.getcwd(), None
        if event in ("os.fork", "os.forkpty"):
            _started([event], "unknown", "a copy of the process, which Peeky doesn't follow")
            return
        if event == "subprocess.Popen":
            program, words = os.fsdecode(args[0] or ""), _words(args[1])
            cwd = os.path.join(cwd, os.fsdecode(args[2])) if args[2] else cwd
            env = args[3]
            _SPAWNING.executable = program
        elif event in ("os.exec", "os.posix_spawn"):
            program, words, env = os.fsdecode(args[0]), _words(args[1]), args[2]
            if program == getattr(_SPAWNING, "executable", None):
                _SPAWNING.executable = None
                return
        elif event == "os.spawn":
            program, words, env = os.fsdecode(args[1]), _words(args[2]), args[3]
        elif event == "os.system":
            program, words = "/bin/sh", ["/bin/sh", "-c", os.fsdecode(args[0])]
        else:
            return
        if any("exec(eval(sys.stdin.readline()))" in word for word in words):
            return  # execnet starting a pytest-xdist worker, which reports itself
        _started(words or [program], *_kind(program or (words[0] if words else ""), cwd, env))
    except Exception:
        try:
            _started(["?"], "unknown", "Peeky couldn't read how it was started")
        except Exception:
            pass


# multiprocessing (and concurrent.futures.ProcessPoolExecutor, through it) starts its
# processes in BaseProcess.start: each call that returns is one, whatever its start
# method, some of which (spawn, forkserver) raise no audit event.
_starting = _process.BaseProcess.start


def _start(self, *args, **kwargs):
    method = getattr(self, "_start_method", None) or "default"
    _SPAWNING.multiprocessing = True
    try:
        started = _starting(self, *args, **kwargs)
    finally:
        _SPAWNING.multiprocessing = False
    _started(["multiprocessing", str(method)], "unknown", "a multiprocessing process")
    return started


def modules():
    """Each loaded module's file."""
    found = {{}}
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if isinstance(file, str):
            found[name] = os.path.abspath(file)
    return found


def write(kind, report):
    path = os.path.join(FOLDER, str(os.getpid()) + "-" + kind + ".json")
    report = {{"nonce": NONCE, "watch": os.path.abspath(__file__), "kind": kind, **report}}
    with open(path + ".part", "w") as stream:
        json.dump(report, stream)
    os.replace(path + ".part", path)


def _at_exit():
    # Not in a forked copy, which inherits this handler.
    if os.getpid() == MAIN[0]:
        write("process", {{
            "instrumented": INSTALLED[0],
            "interrupted": INTERRUPTED[0],
            "modules": modules(),
            "started": STARTED,
        }})


def install(by):
    if INSTALLED[0] is None:
        INSTALLED[0] = by
        sys.addaudithook(_audit)
        _process.BaseProcess.start = _start
        if by == "bootstrap":
            MAIN[0] = os.getpid()
            atexit.register(_at_exit)
'''

# Peeky's pytest plugin, for a pytest command, beside the watch and named for the run too:
# at each pytest process's session end it reports the test files collected, the conftests
# loaded, the tests run, and pytest-xdist's workers (each worker writes its own report).
_PYTEST_PLUGIN_TEXT = '''"""Peeky's report of what a pytest run loaded (`peeky verify --check-command`).

PYTEST_DONT_REWRITE: Peeky's bootstrap imports it before pytest could rewrite it.
"""

import os

import pytest

watch = __import__({watch!r})
WORKERS = set()
RAN = [0]


def pytest_addoption(parser):
    # As this plugin is registered: where Peeky's bootstrap didn't install the watch.
    watch.install("plugin")


def pytest_runtest_logreport(report):
    if report.when == "call":
        RAN[0] += 1


@pytest.hookimpl(optionalhook=True)
def pytest_testnodeready(node):
    worker = (getattr(node, "workerinput", None) or {{}}).get("workerid")
    worker = worker or getattr(getattr(node, "gateway", None), "id", None)
    if worker:
        WORKERS.add(str(worker))


def pytest_sessionfinish(session):
    worker = getattr(session.config, "workerinput", None)
    tests = set()
    for item in getattr(session, "items", None) or ():
        path = getattr(item, "path", None) or getattr(item, "fspath", None)
        if path is not None:
            tests.add(os.path.abspath(os.fspath(path)))
    conftests = set()
    for plugin in session.config.pluginmanager.get_plugins():
        file = getattr(plugin, "__file__", None)
        if isinstance(file, str) and os.path.basename(file) == "conftest.py":
            conftests.add(os.path.abspath(file))
    watch.write("pytest", {{
        "plugin": os.path.abspath(__file__),
        "role": "worker" if worker is not None else "main",
        "worker": None if worker is None else str(worker.get("workerid")),
        "workers": sorted(WORKERS),
        "tests": sorted(tests),
        "conftests": sorted(conftests),
        "modules": watch.modules(),
        "started": watch.STARTED,
        "ran": RAN[0],
        "instrumented": watch.INSTALLED[0],
    }})
'''

# Peeky's start of a check command's Python: the watch goes in first, then the command
# runs as Python would have run it: `-m MODULE`, `-c CODE` or a script (a console script
# too), with the same import path, `sys.argv` and `__main__`. An uncaught KeyboardInterrupt
# is in the report: a run cut short says nothing of the edit.
_BOOTSTRAP_TEXT = '''"""Peeky's start of a check command (`peeky verify --check-command`)."""

# A program's `__main__` has `__annotations__` as it starts, or not, by Python's version.
ANNOTATED = "__annotations__" in globals()

import builtins
import importlib.machinery
import importlib.util
import marshal
import os
import sys
import zipimport

how, target = sys.argv[1:3]
del sys.argv[1:3]


def _entry(path):
    """Whether Python runs ``path`` from its `__main__` module: a directory or a zip."""
    if os.path.isdir(path):
        return True
    try:
        zipimport.zipimporter(path)
    except (zipimport.ZipImportError, OSError):
        return False
    return True


if how == "script" and _entry(target):
    how = "entry"
if how == "entry":
    # Python puts it first, safe path or not.
    if sys.flags.safe_path:
        sys.path.insert(0, os.path.abspath(target))
    else:
        sys.path[0] = os.path.abspath(target)
elif not sys.flags.safe_path:
    # This script's directory is first: Python's own for the command, instead.
    if how == "module":
        sys.path[0] = os.getcwd()
    elif how == "command":
        sys.path[0] = ""
    else:
        sys.path[0] = os.path.dirname(os.path.realpath(target))
if {folder!r} not in sys.path:
    sys.path.append({folder!r})
watch = __import__({watch!r})
watch.install("bootstrap")
# A `__main__` as Python makes it: none of this script's names.
main = type(sys)("__main__")
if ANNOTATED:
    main.__annotations__ = {{}}
main.__builtins__ = builtins
main.__loader__ = importlib.machinery.BuiltinImporter
sys.modules["__main__"] = main
try:
    if how == "module":
        import runpy

        sys.argv[0] = "-m"
        runpy._run_module_as_main(target)
    elif how == "entry":
        import runpy

        sys.argv[0] = target
        runpy._run_module_as_main("__main__", alter_argv=False)
    elif how == "command":
        sys.argv[0] = "-c"
        exec(compile(target, "<string>", "exec"), vars(main))
    else:
        sys.argv[0] = target
        main.__file__ = os.path.abspath(target)
        main.__cached__ = None
        with open(main.__file__, "rb") as stream:
            source = stream.read()
        if target.endswith(".pyc") or source[:2] == importlib.util.MAGIC_NUMBER[:2]:
            if source[:4] != importlib.util.MAGIC_NUMBER:
                raise RuntimeError("Bad magic number in .pyc file")
            main.__loader__ = importlib.machinery.SourcelessFileLoader("__main__", main.__file__)
            code = marshal.loads(source[16:])
        else:
            main.__loader__ = importlib.machinery.SourceFileLoader("__main__", main.__file__)
            code = compile(source, main.__file__, "exec")
        exec(code, vars(main))
except KeyboardInterrupt:
    watch.INTERRUPTED[0] = True
    raise
'''
# Why a command Peeky doesn't start as Python has no verdict.
NOT_WATCHED = (
    "Peeky can only vouch for a Python command it starts itself; run the test command "
    "directly, as `python -m pytest …`"
)


def _python_split(words):
    """(interpreter options, how, target, the rest) of `python [options] -m MODULE ...`
    ("module"), `-c CODE ...` ("command") or `SCRIPT ...` ("script"), the options word
    for word (`-IBmpytest` gives `-IB`); None for stdin, no program, or an option Peeky
    doesn't know."""
    args, options, at = words[1:], [], 0
    while at < len(args):
        word = args[at]
        at += 1
        if word == "-" or word.startswith("--"):
            return None
        if not word.startswith("-"):
            return options, "script", word, args[at:]
        for place, flag in enumerate(word[1:], 1):
            if flag in _PYTHON_FLAGS:
                continue
            if flag in "WX":
                # Its value is the rest of the word, or the next word.
                options.append(word)
                if place == len(word) - 1:
                    if at >= len(args):
                        return None
                    options.append(args[at])
                    at += 1
                break
            if flag in "mc":
                if word[:place] != "-":
                    options.append(word[:place])
                value = word[place + 1 :]
                if not value:
                    if at >= len(args):
                        return None
                    value, at = args[at], at + 1
                return options, "module" if flag == "m" else "command", value, args[at:]
            return None
        else:
            options.append(word)
    return None


def _python_command(ran, environment):
    """(python, its options, how, target, the rest) of a command Peeky starts as Python,
    through its bootstrap: the interpreter's own command line (`-m`, `-c` or a script), or
    a console script on its `#!` Python and options; None for any other command."""
    if _PYTHON_NAME.fullmatch(Path(ran[0]).name):
        if (split := _python_split(ran)) is None:
            return None
        return ran[0], *split
    if (script := _script_python(ran[0], environment)) is not None:
        return script[0], script[1], "script", ran[0], ran[1:]
    return None


def _watched(command, candidate, environment, pytest):
    """``command`` (``_python_command``'s) run through Peeky's bootstrap, which installs
    the watch before it runs: `python [its options] <bootstrap> module|command|script
    TARGET ARGS`. For pytest (``pytest``), with `-p <plugin>` after its word. The watch,
    the plugin and the bootstrap are written, with the run's nonce and report folder, to a
    directory of Peeky's, at the end of the command's PYTHONPATH. Returns (ran, how to
    read the reports: folder, nonce, watch file, plugin file)."""
    python, options, how, target, rest = command
    folder = Path(tempfile.mkdtemp(prefix="peeky-watch-", dir=candidate.scratch))
    tag, nonce = secrets.token_hex(8), secrets.token_hex(16)
    watch, plugin = f"peeky_watch_{tag}", f"peeky_check_{tag}"
    for each in ("modules", "start", "reports"):
        (folder / each).mkdir()
    modules = str(folder / "modules")
    (folder / "modules" / f"{watch}.py").write_text(
        _WATCH_TEXT.format(nonce=nonce, folder=str(folder / "reports"))
    )
    (folder / "modules" / f"{plugin}.py").write_text(_PYTEST_PLUGIN_TEXT.format(watch=watch))
    # Not on any import path: it is run, never imported.
    bootstrap = folder / "start" / "peeky_start.py"
    bootstrap.write_text(_BOOTSTRAP_TEXT.format(folder=modules, watch=watch))
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, [environment.get("PYTHONPATH"), modules])
    )
    if pytest:
        rest = ["-p", plugin, *rest]
    reading = (
        folder / "reports",
        nonce,
        folder / "modules" / f"{watch}.py",
        folder / "modules" / f"{plugin}.py",
    )
    return [python, *options, str(bootstrap), how, target, *rest], reading


def _example(started, kind):
    """`/usr/bin/find . -exec ...`: one process started of ``kind`` ("unknown": not of a
    program that starts no other, with why if Peeky knows; "other"), as a short example;
    "" if none."""
    examples = [each for each in started.get("examples") or () if each.get("kind") == kind]
    if not examples:
        return ""
    shown = shlex.join(str(word) for word in (examples[0].get("args") or [])[:6])
    why = examples[0].get("why")
    return f"{shown} ({why})" if kind == "unknown" and why else shown


def _command_reports(reading, candidate, names, pytest):
    """What Peeky's watch (and for pytest, its plugin) reported: (reports, test files,
    tests run, problems, notes). A problem leaves the run without a verdict:
    - a report that isn't this run's (``reading``: folder, nonce, watch and plugin files);
    - no report from the command's process as it exited (os._exit, a crash, a kill), or
      one that says it stopped on an uncaught KeyboardInterrupt;
    - for pytest: no report from its main process, or from a pytest-xdist worker the main
      process started; a worker watched only from the plugin's load;
    - a test file, conftest or module loaded from your project instead of the copy, or a
      module of the project's (``names``) from outside the copy;
    - a process started, other than of a program that starts no other (the watch's
      ``_LEAVES``), which Peeky can't follow. Those are a note."""
    folder, nonce, watch, plugin = reading
    copy = Path(os.path.realpath(candidate.root))
    processes, pytests, problems, notes = [], [], [], []
    for path in sorted(folder.glob("*.json")):
        try:
            report = json.loads(path.read_text())
            kind = report.get("kind")
            ours = (
                report.get("nonce") == nonce
                and os.path.realpath(str(report.get("watch"))) == os.path.realpath(watch)
                and (
                    kind == "process"
                    or kind == "pytest"
                    and os.path.realpath(str(report.get("plugin"))) == os.path.realpath(plugin)
                )
            )
            files = [("collected", file) for file in report.get("tests") or ()]
            files += [("loaded", file) for file in report.get("conftests") or ()]
            modules = dict(report["modules"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            ours = False
        if not ours:
            problems.append(f"{path.name} in the command's reports isn't from Peeky's watch")
            continue
        (processes if kind == "process" else pytests).append(report)
        if kind == "process" and report.get("interrupted"):
            problems.append(
                "the command's Python stopped on an uncaught KeyboardInterrupt, as on Ctrl-C, "
                "so its exit status says nothing of the edit"
            )
        if kind == "pytest" and report.get("role") == "worker":
            if report.get("instrumented") != "bootstrap":
                problems.append(
                    f"pytest-xdist's worker {report.get('worker')} started before Peeky could "
                    "watch the processes its tests start (run without -n for a verdict)"
                )
        for each, file in files:
            if candidate.copy_path(file) is not None:
                problems.append(f"pytest {each} {file} from your project, not the copy")
        for name, file in sorted(modules.items()):
            if candidate.copy_path(file) is not None:
                problems.append(f"`{name}` was loaded from {file}, in your project, not the copy")
            elif name.split(".")[0] in names and not Path(os.path.realpath(file)).is_relative_to(
                copy
            ):
                problems.append(f"`{name}` was loaded from {file}, outside the copy")
    if not processes:
        problems.insert(
            0,
            "the command's Python wrote no report as it exited (it ended by os._exit, a crash "
            "or a kill), so Peeky can't tell what it loaded or started",
        )
    if pytest:
        if not any(report.get("role") == "main" for report in pytests):
            problems.insert(
                0,
                "pytest wrote no report from Peeky's plugin (-p no:<its name> turned it off, "
                "or pytest stopped first), so Peeky can't tell which test files it ran",
            )
        expected = {worker for report in pytests for worker in report.get("workers") or ()}
        workers = {
            str(report.get("worker")) for report in pytests if report.get("role") == "worker"
        }
        if missing := sorted(expected - workers):
            problems.insert(0, f"pytest-xdist's worker {', '.join(missing)} wrote no report")
    # The command's process reports what it started; each pytest-xdist worker, its own.
    started = [report.get("started") or {} for report in processes]
    started += [report.get("started") or {} for report in pytests if report.get("role") == "worker"]
    unknown = sum(int(each.get("unknown") or 0) for each in started)
    count = sum(int(each.get("count") or 0) for each in started)
    if unknown:
        example = next((found for each in started if (found := _example(each, "unknown"))), "")
        problems.append(
            f"the command started {example}; Peeky can't check what it ran or loaded"
            if unknown == 1
            else f"the command started {unknown} programs, such as {example}; Peeky can't "
            "check what they ran or loaded"
        )
    if count > unknown:
        example = next((found for each in started if (found := _example(each, "other"))), "")
        others = count - unknown
        notes.append(
            f"The command started {others} process{'es' if others > 1 else ''} of programs "
            f"that start no other ({example})"
        )
    tests = {file for report in pytests for file in report.get("tests") or ()}
    ran = sum(int(report.get("ran") or 0) for report in pytests)
    return len(processes) + len(pytests), len(tests), ran, list(dict.fromkeys(problems)), notes


def _verify_command(args, root, source, words, deadline):
    """`verify --check-command`: run ``words`` in a fresh copy of ``root`` with the edit
    (with --baseline, none), on the program's interpreter's environment, through Peeky's
    bootstrap where it is a Python Peeky starts. Its `verdict` is "unknown" where Peeky
    stopped it, its test runner ran no test, or the run may not be the copy's: what the
    watch reported, an import from outside the copy, or no report at all (a command that
    isn't a Python Peeky starts)."""
    from peeky import agent
    from peeky.adapters import PythonAdapter, PythonCandidate
    from peeky.build import SourceEdit
    from peeky.python_interpreter import find

    root = Path(root).resolve()
    _check_command_arguments(words[1:], root)
    if addopts := os.environ.get("PYTEST_ADDOPTS"):
        try:
            options = shlex.split(addopts)
        except ValueError:
            options = addopts.split()
        _check_command_arguments(options, root, "PYTEST_ADDOPTS word")
    # The command runs on the program's interpreter, as a probe and a checker do.
    interpreter = find(root, args.python)
    original = agent.digest(source)
    relative = source.relative_to(root).as_posix()
    edit = None if args.old is None else SourceEdit(relative, args.old, args.new)
    candidate = PythonCandidate(
        PythonAdapter.on(interpreter), source, root, edit, deadline, command=True
    )
    try:
        environment = _command_environment(interpreter, candidate)
        ran = [_command_program(words[0], candidate, environment, interpreter), *words[1:]]
        pytest = _runner(words) == "pytest"
        # A command that isn't a Python Peeky starts runs with no verdict, whatever its
        # imports would be: they are checked only for one it watches.
        command = _python_command(ran, environment)
        # What pytest loads it reports itself, as pytest's import path differs.
        static = (
            candidate.command_imports(
                *_invocation(ran, candidate, environment, interpreter), environment
            )
            if command is not None and not pytest
            else []
        )
        reasons = [*candidate.unedited_imports, *static] if command is not None else []
        if edit is not None and reasons:
            raise ValueError(
                f"verify --check-command can't test this edit: {reasons[0]}. Otherwise, check "
                "the edit with a --check checker, which is given the edited file"
            )
        ran, reading = (
            (ran, None) if command is None else _watched(command, candidate, environment, pytest)
        )
        candidate_sha256 = hashlib.sha256(candidate.staged.read_bytes()).hexdigest()
        timeout, limited = agent.CHECK_S, False
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("verify ran past its --timeout before the check command")
            timeout, limited = min(timeout, remaining), remaining < timeout
        result = agent.execute(ran, candidate.root, timeout, env=environment, tail=True)
        if limited and result["timed_out"]:
            # The --timeout cut the command short: that says nothing about the candidate.
            raise TimeoutError("verify ran past its --timeout while the check command was running")
        if agent.digest(source) != original:
            raise RuntimeError("Original source changed during verification")
        result.update(
            check_command={
                "argv": list(words),
                "ran": ran,
                "copy_of": str(root),
                "timeout_s": agent.CHECK_S,
            },
            candidate=str(candidate.staged),
            candidate_sha256=candidate_sha256,
            python=interpreter.identity(),
        )
        # Why the run may not be the copy's: then it is no verdict, whatever its status.
        provenance, notes = list(static), []
        if reading is None:
            provenance.append(NOT_WATCHED)
        else:
            count, tests, ran_tests, problems, notes = _command_reports(
                reading,
                candidate,
                candidate.project_names((environment.get("PYTHONPATH") or "").split(os.pathsep)),
                pytest,
            )
            if pytest and not ran_tests and result["exit_code"] == 0 and count:
                # A pass with no test run (`--collect-only`, all skipped) shows nothing.
                problems.append("pytest ran no test, so its pass shows nothing of the edit")
            result["check_report"] = {
                "reports": count,
                **({"test_files": tests} if pytest else {}),
                "problems": problems,
            }
            provenance += problems
        if result["timed_out"] or result["output_limit_exceeded"]:
            # Peeky stopped it at its bound: no verdict, whatever runs it.
            result.update(verdict="unknown", no_verdict=_command_reason(result))
        elif no_tests := _no_tests(result):
            # No test ran: what the run loaded is beside the point.
            result.update(verdict="unknown", no_verdict=no_tests)
        elif provenance:
            result.update(verdict="unknown", no_verdict=provenance[0])
            result["reason"] = f"no verdict: {provenance[0]}"
        elif result["exit_code"] == 0:
            result["verdict"] = "passed"
        else:
            result["verdict"] = "failed"
        result["passed"] = result["verdict"] == "passed"
        if warnings := [*candidate.warnings, *notes]:
            result["warnings"] = warnings
        if args.keep:
            result["kept"] = str(candidate.scratch)
        return result
    finally:
        candidate.close(keep=args.keep)


MAX_VERIFY_S = 600


def build_workspace(args):
    """`peeky build`: build a Rust workspace its configuration names (rust_build)."""
    from peeky.rust_build import BUILD_TIMEOUT_S, RustWorkspaceTarget

    if getattr(args, "worker", None) is not None:
        if args.config:
            raise ValueError(
                "peeky build --worker builds from the worker's own --rust-config: leave out "
                "--config"
            )
        timeout = BUILD_TIMEOUT_S if args.timeout is None else args.timeout
        if not 0 < timeout <= BUILD_TIMEOUT_S:
            raise ValueError(f"--timeout must be within (0, {BUILD_TIMEOUT_S}] seconds")
        result = _remote_build(args, time.monotonic() + timeout)
        _emit(result, args.json, _build_text)
        return 0
    if not args.config:
        raise ValueError(
            "peeky build needs --config <file.json> naming a Rust workspace's packages to "
            'analyse and what runs, e.g. {"analyse": ["util"], "run": {"bin": "app"}, '
            '"vendor": "vendor"} (lang/rust.md)'
        )
    timeout = BUILD_TIMEOUT_S if args.timeout is None else args.timeout
    if not 0 < timeout <= BUILD_TIMEOUT_S:
        raise ValueError(f"--timeout must be within (0, {BUILD_TIMEOUT_S}] seconds")
    root, _, _, language, data = resolve(args.target, None, args.config)
    target = adapter_for(language, data, build=_build_command(args))
    if not isinstance(target, RustWorkspaceTarget):
        raise ValueError(
            "peeky build builds a Rust workspace's configuration (analyse and run); other "
            "targets are built by every probe"
        )
    result = target.build(root, deadline=time.monotonic() + timeout, progress=_progress(args))
    _emit(result, args.json, _build_text)
    return 0


def _remote_build(args, deadline):
    """A worker's build of its Rust workspace (its `build` operation), asked again while
    it runs until ``deadline``: its summary, its paths the worker host's, marked so."""
    from peeky.errors import PeekyError

    client, _ = _remote(args.worker)
    progress, arguments = _progress(args), {}
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(
                "The worker's build is still running past the deadline; it goes on there. Run "
                f"`peeky build {shlex.quote(str(args.target))} --worker {args.worker}` again to "
                "wait for it"
            )
        try:
            answer = client.call("build", **arguments, wait=min(30.0, left))
        except PeekyError as exc:
            exc.payload = _remote_build_required(exc.payload, args)
            raise
        if answer.get("status") != "building":
            for key in ("workspace", "executable"):
                answer[key] = f"worker:{answer[key]}"
            return answer
        if progress is not None:
            progress("the worker is still building")
        arguments = {"request": answer["request"]}


def _remote_build_required(error, args):
    """A worker's BUILD_REQUIRED as its client says it: the build command this client runs
    (`peeky build TARGET --worker URL`), not the one on the worker's host."""
    if not isinstance(error, dict) or error.get("code") != "BUILD_REQUIRED":
        return error
    command = f"peeky build {shlex.quote(str(args.target))} --worker {args.worker}"
    message = re.sub(r"Run `[^`]*`", f"Run `{command}`", str(error.get("message", "")), count=1)
    return {**error, "message": message}


def _build_text(result):
    run = result["run"]
    kind = next((kind for kind in ("bin", "test") if kind in run["kinds"]), "lib")
    what = f"the unit tests of `{run['package']}`" if kind == "lib" else f"{kind} {run['target']}"
    lines = [
        f"built {result['workspace']} in {result['seconds']} s: analysed "
        f"{', '.join(result['analysed'])}; runs {what}",
        f"executable: {result['executable']}",
        f"build: {result['build_id']}",
    ]
    if unread := [unit["crate"] for unit in result["units"] if not unit["expanded"]]:
        lines.append(f"rustc could not expand {', '.join(unread)}: their source scans withhold")
    return "\n".join(lines)


def verify(args):
    started = time.monotonic()
    if args.timeout is not None and not 0 < args.timeout <= MAX_VERIFY_S:
        raise ValueError(f"--timeout must be within (0, {MAX_VERIFY_S}] seconds")
    deadline = None if args.timeout is None else started + args.timeout
    words = _check_command_words(args)
    root, program, _, language, data = resolve(args.target, args.at, args.config)
    if language == "java-attach":
        raise ValueError("Attached JVMs cannot verify edits; verify with a fresh build target")
    from peeky.rust_workspace import is_workspace_config

    if language == "rust" and is_workspace_config(data):
        raise ValueError(
            "Peeky doesn't verify or probe candidate edits in a Rust workspace yet: edit the "
            "source, run `peeky build` again, and run the project's own tests"
        )
    if words is not None and language != "python":
        named = {"c": "C", "cpp": "C++", "rust": "Rust", "java": "Java"}.get(language, language)
        raise ValueError(
            f"--check-command supports Python targets for now, and this target is {named}: "
            "pass a --check checker, which runs on the fresh build"
        )
    _python_option(args, language)
    program = _default_program(program, data, "verify")
    source = (root / program).resolve(strict=True)
    check = None if words is not None else Path(args.check).resolve(strict=True)
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
    from peeky.build import private_base

    destination = None
    try:
        if words is not None:
            result = _verify_command(args, root, source, words, deadline)
        else:
            # Where no other user can write: a candidate that reaches above its folder finds
            # nothing of theirs there.
            destination = Path(tempfile.mkdtemp(prefix="peeky-verify-", dir=private_base()))
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
        if destination is not None and not args.keep:
            shutil.rmtree(destination, ignore_errors=True)
    result.setdefault("stage", "verification")
    result["seconds"] = round(time.monotonic() - started, 3)
    result["diff"] = _diff(original, edited, program)
    if args.baseline:
        # The checker's verdict on the program as it is: it should fail on the bug.
        result["baseline"] = True
    if args.keep:
        result.setdefault("kept", str(destination))
    else:
        for key in ("candidate", "evidence_file"):
            result.pop(key, None)
    if not args.details:
        # Provenance the hashes and ids already identify; --details keeps it (with the
        # toolchain probes, it is several KB an agent rarely needs).
        for key in ("descriptor", "build_manifest"):
            result.pop(key, None)
    if not result["passed"] and "reason" not in result and (reason := _verify_reason(result)):
        result["reason"] = reason
    # A checker's verdict is its pass or fail; a check command's may be "unknown".
    result.setdefault("verdict", "passed" if result["passed"] else "failed")

    _emit(result, args.json, lambda shown: _verify_text(shown, args.keep))
    return 0 if result["passed"] else 1


def _verify_text(result, keep=False):
    lines = [
        # A check command's verdict leads: "unknown" is no failure of the edit.
        (f"verdict: {result.get('verdict')}  " if result.get("check_command") else "")
        + f"passed: {result['passed']}  stage: {result['stage']}"
        + (f"  seconds: {result['seconds']}" if "seconds" in result else "")
    ]
    if python := _python_line(result.get("python")):
        lines.append(python)
    if command := result.get("check_command"):
        lines.append(f"command: {shlex.join(command['argv'])}")
        lines.append(
            f"ran: {shlex.join(command['ran'])} (in a copy of {command['copy_of']}, for at most "
            f"{command['timeout_s']} s)"
        )
    if (report := result.get("check_report")) and report.get("reports"):
        files, problems = report.get("test_files"), len(report["problems"])
        lines.append(
            "check report: "
            + ("" if files is None else f"{files} test file" + "s" * (files != 1) + ", ")
            + (
                f"{problems} problem{'s' * (problems > 1)} (`reason:` names the first)"
                if problems
                else "nothing loaded from your project"
            )
        )
    if tests := result.get("tests"):
        lines.extend(_tests_text(tests))
    elif result.get("language") == "rust" and result.get("stage") == "verification":
        lines.append(
            "compiled tests: none (the crate has no integration test; the checker decides)"
        )
    reason = not result["passed"] and (result.get("reason") or _verify_reason(result))
    if reason:
        lines.append(f"reason: {reason}")
    lines.append(f"{'command' if command else 'checker'} exit: {result.get('exit_code')}")
    output = [(result.get(key) or "")[-2000:] for key in ("stdout", "stderr")]
    shown = "\n".join(text.strip() for text in output if text.strip())
    if reason and shown and shown == re.sub(r" \(SystemExit: [^)]*\)$", "", reason):
        # The checker printed only the line `reason` shows.
        lines.append(
            f"output: the {'command' if command else 'checker'} printed only the `reason:` "
            "line above"
        )
    elif any(output):
        lines.extend(("output:", *output))
    lines.extend(f"warning: {warning}" for warning in result.get("warnings") or ())
    if keep and command:
        lines.append(
            f"candidate: {result.get('candidate')} (the edited file, in the copy the command "
            f"ran in; remove {result.get('kept')} when done)"
        )
    elif keep:
        lines.append(
            f"candidate: {result.get('candidate')} (the edited file, and for compiled "
            "programs the build evidence beside it; build artifacts are not kept)"
        )
    if result.get("baseline") and command:
        lines.append("baseline: the unedited program, no edit" + _baseline_note(result))
    elif result.get("baseline"):
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
    (``_FAILURE``), else stdout's last line; with no stdout, the same of stderr. The
    status stays in `checker exit` (and
    `checker.error`), and in the reason where it is not that exit code. A check command's:
    ``_command_reason``."""
    if result.get("check_command"):
        return _command_reason(result)
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


# pytest's exit statuses other than 0 and 1: none says a test failed.
_PYTEST_EXITS = {
    2: "pytest exited 2: it was interrupted, as by a test file it could not import",
    3: "pytest exited 3: an internal error",
    4: "pytest exited 4: a usage error, such as a test path that doesn't exist",
    5: "pytest exited 5: no tests ran",
}
# pytest's last line: `2 failed, 1 passed in 0.12s`, or `===== 1 failed in 0.1s =====`.
_PYTEST_FAILED = re.compile(
    r"^=*\s*(?:[0-9]+ [a-z]+, )*[1-9][0-9]* failed(?:, [0-9]+ [a-z]+)* in [0-9.]+s\b", re.M
)
# unittest's last line: `FAILED (failures=1, errors=2)`.
_UNITTEST_FAILED = re.compile(r"^FAILED \(([^)]*)\)\s*$", re.M)
# unittest's error for a test module it could not import.
_UNITTEST_IMPORT = re.compile(r"unittest\.loader\._FailedTest|^ImportError: Failed to import", re.M)


def _verdict(result):
    """Whether a check command that exited nonzero failed tests, and if not, why that says
    nothing about the program: (True, None) only where its words name a test runner Peeky
    reads (``_runner``) and its output shows failed tests (pytest: exit 1 with `N failed`;
    unittest: exit 1 with `FAILED (failures=N)`, or errors none of which is a test module
    it could not import). (False, why) for such a runner's other statuses, and (False,
    None) for any other command."""
    command = result.get("check_command") or {}
    code = result.get("exit_code")
    output = "\n".join(result.get(key) or "" for key in ("stdout", "stderr"))
    runner = _runner(command.get("argv") or [])
    if runner == "pytest":
        if code == 1 and _PYTEST_FAILED.search(output):
            return True, None
        if code == 1:
            return False, "pytest exited 1, and its output shows no failed test"
        return False, _PYTEST_EXITS.get(code, f"pytest exited {code}")
    if runner == "unittest":
        found = _UNITTEST_FAILED.findall(output)
        counts = dict(re.findall(r"\b(failures|errors)=([0-9]+)", found[-1])) if found else {}
        failures, errors = (int(counts.get(key, 0)) for key in ("failures", "errors"))
        imported = _UNITTEST_IMPORT.search(output) is not None
        if code == 1 and (failures or (errors and not imported)):
            return True, None
        if code == 1 and imported:
            return False, "unittest exited 1: it could not import a test module"
        if code == 5:
            return False, "unittest exited 5: no tests ran"
        return False, f"unittest exited {code}, and its output shows no failed test"
    return False, None


def _no_tests(result):
    """A test runner's status that says it ran no test, or couldn't (pytest's 3, 4 and 5,
    unittest's 5), as ``_verdict`` words it; else None."""
    runner = _runner((result.get("check_command") or {}).get("argv") or [])
    code = result.get("exit_code")
    if runner == "pytest" and code in (3, 4, 5):
        return _PYTEST_EXITS[code]
    if runner == "unittest" and code == 5:
        return "unittest exited 5: no tests ran"
    return None


def _command_reason(result):
    """Why a check command failed, in one line: a timeout, too much output, or the line
    of its output that says what failed, looked for in both streams: pytest's `FAILED
    tests/t.py::test_x - ...` summary, then unittest's `FAIL: test_x (...)` header with
    its exception, then any line ``_failure_line`` finds (stdout's first); only then the
    last line (stdout's, else stderr's). A test runner's status that says no test failed
    follows (``_verdict``)."""
    command = result.get("check_command") or {}
    if result.get("timed_out"):
        return f"the check command ran past {command.get('timeout_s')} s and was stopped"
    if result.get("output_limit_exceeded"):
        return (
            "the check command printed over 1 MiB of output, both streams together, and was stopped"
        )
    streams = [result.get(key) or "" for key in ("stdout", "stderr")]
    chosen = None
    for stream in streams:
        # pytest's `FAILED tests/t.py::test_x - ...`, not unittest's `FAILED (failures=1)`.
        summary = [
            line.strip()
            for line in split_lines(stream)
            if re.match(r"(?:FAILED|ERROR) [^\s(]", line)
        ]
        if summary:
            more = f" (and {len(summary) - 1} more)" if len(summary) > 1 else ""
            chosen = summary[0] + more
            break
    for stream in streams if chosen is None else ():
        if header := re.search(r"^(?:FAIL|ERROR): \S", stream, re.M):
            chosen = _failure_line(stream[header.start() :])
            break
    for stream in streams if chosen is None else ():
        if chosen := _failure_line(stream):
            break
    for stream in streams if chosen is None else ():
        if lines := [line.strip() for line in split_lines(stream) if line.strip()]:
            chosen = lines[-1]
            break
    why = None if result.get("exit_code") in (0, None) else _verdict(result)[1]
    parts = [part for part in (chosen and chosen[:500], why) if part]
    return "; ".join(parts) or f"the check command exited {result.get('exit_code')}"


def _baseline_note(result):
    """What a check command's result at --baseline says of it: that tests failed on the
    unedited program, as they should on the bug, only where ``_verdict`` reads that."""
    code = result.get("exit_code")
    if result.get("verdict") == "unknown":
        return f" ({result.get('no_verdict')}, so this says nothing about the bug)"
    if result.get("timed_out") or result.get("output_limit_exceeded"):
        return ""
    if result.get("passed"):
        return " (the command passes on it, so it can't tell a fix from the bug)"
    caught, why = _verdict(result)
    if caught:
        return " (its tests fail on it, as they should fail on the bug)"
    if why:
        return f" ({why}, so this says nothing about the bug)"
    return (
        f" (the command exited {code}, but Peeky reads failed tests only from pytest and "
        "unittest: check that its output shows the bug)"
    )


def _rejected(result):
    """Whether the checker ran to its own verdict and rejected the program: an assertion
    failed, or it exited with a nonzero status. Not a crash of the checker (any other
    exception), a timeout, nor a build that failed before it ran. A check command: its
    tests failed (``_verdict``)."""
    if result.get("check_command"):
        return (
            not result.get("timed_out")
            and not result.get("output_limit_exceeded")
            and result.get("exit_code") not in (0, None)
            and _verdict(result)[0]
        )
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
    # The job is this command, run later: a remote one needs the client's token.
    env = without_credentials(os.environ)
    if token := _CREDENTIALS.get(WORKER_TOKEN_ENV):
        env[WORKER_TOKEN_ENV] = token
    try:
        subprocess.Popen(
            [sys.executable, "-I", "-c", bootstrap]
            + [str(Path(peeky.__file__).resolve().parents[1]), str(path)],
            cwd=path,
            env=env,
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
        else _build_text(result)
        if spec["command"] == "build"
        else _probe_text(result, quiet="--quiet" in spec.get("argv", ()))
    )
    return text + "\n"


PRUNED_NOTE = (
    "A session still using a removed version fails its next step (PROVENANCE_CHANGED): start "
    "it again."
)


def setup_java(args):
    """`peeky setup-java`: build the Java adapter bundle (peeky.java_setup). Without
    --output, install Peeky's own: kept while it is up to date, otherwise a new version,
    switched to once it is complete. --prune removes every version but the current one."""
    from peeky import java_setup

    def progress(message):
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    if args.prune:
        if args.output is not None or args.force:
            raise ValueError("--prune takes neither --output nor --force; run it on its own")
        result = {**java_setup.prune(progress), "default": True}
        if result["pruned"] or result["failed"]:
            result["warnings"] = [PRUNED_NOTE]

        def pruned(result):
            kept = f"Kept {result['bundle']}." if result["bundle"] else "No version is in use."
            lines = []
            if result["pruned"]:
                names = ", ".join(result["pruned"])
                lines.append(f"Removed {len(result['pruned'])} older version(s): {names}.")
            for name, why in result["failed"].items():
                lines.append(f"Could not remove {name} ({why}); what is left of it may be partial.")
            if not lines:
                return f"Nothing to remove. {kept}"
            lines.append(kept)
            lines.append(PRUNED_NOTE)
            return "\n".join(lines)

        _emit(result, args.json, pruned)
        # Any version still there is a failure, whatever was removed beside it.
        return 2 if result["failed"] else 0
    if args.output is None:
        result = {**java_setup.install(force=args.force, progress=progress), "default": True}
    elif args.force:
        raise ValueError(
            "--force replaces only Peeky's own bundle (no --output); give --output a new directory"
        )
    else:
        output = Path(os.path.abspath(os.path.expanduser(args.output)))
        own = java_setup.default_bundle().resolve()
        if output.resolve() == own or own in output.resolve().parents:
            raise ValueError(
                f"--output {output} is in Peeky's own bundle's directory, which `peeky "
                "setup-java` manages: run it without --output, or give another directory"
            )
        progress(
            f"Building the Java adapter in {output}: downloading java-debug and its jars "
            "(pinned by SHA-256) and compiling them can take a minute."
        )
        manifest = java_setup.setup(output)
        built = output.absolute().parent.resolve() / output.name
        result = {"bundle": str(built), "built": True, "default": False, "adapter": manifest}

    def text(result):
        adapter = result["adapter"]
        older = (
            f"\n{len(result['older'])} older version(s) kept, for sessions still using them: "
            "`peeky setup-java --prune` removes them."
            if result.get("older")
            else ""
        )
        if not result["built"]:
            return (
                f"The Java adapter at {result['bundle']} is up to date: nothing to do "
                f"(--force rebuilds it).{older}"
            )
        use = (
            'Java configs without "bundle" use it.'
            if result["default"]
            else f'Name it as "bundle" in a Java target\'s config: "bundle": "{result["bundle"]}"'
        )
        return (
            f"Built the Java adapter at {result['bundle']} in {adapter['setup_seconds']:.1f} s: "
            f"java-debug {adapter['source_commit'][:12]} with Peeky's patches "
            f"({', '.join(sorted(adapter['patches']))}).\n{use}{older}"
        )

    _emit(result, args.json, text)
    return 0


# `peeky queries --help`'s epilog, filled in only when help is shown: the evidence module,
# which lists the canned queries, is imported only by the commands that read the store.
_QUERIES_HELP = "(canned queries)"


class _QueriesHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def add_text(self, text):
        if text is _QUERIES_HELP:
            from peeky.evidence import queries_help

            text = queries_help()
        super().add_text(text)


def main(argv=None, _job=False, _edits=None):
    """Both names of the worker's token leave the environment first, so that no process
    this command starts can inherit them; they come back for a caller in this process."""
    from peeky.processes import take_credentials

    taken = take_credentials()
    _CREDENTIALS.update(taken)
    try:
        return _main(argv, _job, _edits)
    finally:
        for name in taken:
            _CREDENTIALS.pop(name, None)
        os.environ.update(taken)


# The help text's first line; not read from the docstring, which `python -OO` drops.
DESCRIPTION = "`peeky`: a standalone debugger tool that agents, harnesses and humans call."


def _main(argv, _job, _edits):
    from peeky.processes import keep_children_waitable

    keep_children_waitable()  # An inherited ignored SIGCHLD reaps adapters
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv and (argv[:1] == ["probe"] or argv[:2] == ["session", "start"]):
        # `peeky probe t --at 5 -- a b c` passes a, b and c to the program.
        split = argv.index("--")
        argv = argv[:split] + [f"--arg={value}" for value in argv[split + 1 :]]
    elif argv[:1] == ["verify"] and "--" in argv and argv[argv.index("--") - 1] == CHECK_COMMAND:
        # `--check-command -- python -m pytest 't.py::test[1]'`: the rest are its words.
        split = argv.index("--")
        argv = argv[: split - 1] + (
            [f"--check-argv={word}" for word in argv[split + 1 :]] or ["--check-argv="]
        )
    parser = argparse.ArgumentParser(prog="peeky", description=DESCRIPTION)
    add_version(parser)
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

    def python_option(sub):
        sub.add_argument(
            "--python",
            metavar="PATH",
            help="Python: the program's interpreter (default: the root's .venv or venv, else "
            "$VIRTUAL_ENV, else Peeky's own); `peeky`: Peeky's own",
        )

    def common(sub, target=True):
        if target:
            sub.add_argument("target", help="Program file, Cargo crate or project directory")
            sub.add_argument("--config", help="Trusted target JSON (required for Java)")
        sub.add_argument("--json", action="store_true", help="Machine-readable output")

    def build_option(sub):
        sub.add_argument(
            "--build",
            action="store_true",
            help="Rust workspace: run `peeky build` first, under the build's own deadline "
            "(900 s); without it, nothing is built",
        )

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

    def binary_option(sub):
        sub.add_argument(
            "--binary",
            metavar="PATH",
            help="C/C++: a binary the project built, relative to the target directory and "
            "inside it, with clang -O0 -g -gdwarf-5 -grecord-command-line (docs/lang/c-cpp.md)",
        )

    sub = commands.add_parser("describe", help="Manifest: language, breakpoint lines, views")
    common(sub)
    sub.add_argument("--at", help="Program path (for multi-file targets)")
    binary_option(sub)
    python_option(sub)
    # Accepted everywhere: describe prints no progress, so it changes nothing here.
    sub.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    sub = commands.add_parser("probe", help="One stateless question: stop, capture, set, report")
    probe_parser = sub
    common(sub)
    sub.add_argument("--at", required=True, help="<line> or <path>:<line>")
    binary_option(sub)
    sub.add_argument(
        "--capture",
        default="",
        help="Comma-separated selectors, e.g. a,b.c,d[0]; 1:i reads i in frame 1, the caller "
        "(2:i its caller), as `session get --frame 1 i` does; `return`: what the function "
        "holding the line returns, each call (C, C++, Rust, Java)",
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
        metavar=("[PATH:]LINE[@HIT|@all]", "NAME=VALUE"),
        help="Change a value at its own stop: at hit HIT (default 1) of LINE, counting that "
        "line's own stops, in the --at file, whether or not values are captured there. "
        "Repeatable, at several lines and hits (shorten at 22@3, restore at 23@3); each is "
        "applied once. LINE@all applies it at every stop of LINE, up to "
        f"{MAX_HITS}: on a line reached only in the case to test, that is the condition. HIT "
        "counts every stop of LINE, in any function (a lambda or closure on the line, a "
        "recursive call); each change says which function's variable it changed. One the "
        "program never reaches is reported, and the probe exits with 4. NAME=@before writes "
        "back NAME's value from just before this probe's change to it at the same hit, in "
        "the same call (32@all trend=0, then 35@all trend=@before)",
    )
    sub.add_argument(
        "--run", help="Python: script to run when --at is in a module it imports (e.g. main.py)"
    )
    sub.add_argument(
        "--run-module",
        metavar="MODULE",
        help="Python: run pytest, as `python -m pytest` does, when --at is in code a test runs "
        "(pytest only); its arguments go after `--` (--run-module pytest -- tests/test_x.py -q)",
    )
    python_option(sub)
    sub.add_argument(
        "--worker",
        help="Probe on a running worker at this loopback URL (e.g. through ssh -L); token "
        "from $PEEKY_WORKER_TOKEN; local and worker sources must match",
    )
    sub.add_argument("--expect-sha256", action="append", default=[], help=argparse.SUPPRESS)
    # A worker's probe child: a JSON {path: sha256} of every source its client holds.
    sub.add_argument("--expect-inputs-file", type=Path, help=argparse.SUPPRESS)
    # A worker's probe child: a launch budget below the worker's watchdog,
    # and a file written just before the debugger is asked to start the program.
    sub.add_argument("--launch-budget", type=float, help=argparse.SUPPRESS)
    sub.add_argument("--launch-marker", type=Path, help=argparse.SUPPRESS)
    # A worker's probe child: where a remote Python run's bytecode cache goes.
    sub.add_argument("--pycache-dir", type=Path, help=argparse.SUPPRESS)
    # A worker's probe child: errors keep their text long (errors.relayed).
    sub.add_argument("--relay-errors", action="store_true", help=argparse.SUPPRESS)
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
    build_option(sub)
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
        "--schema",
        nargs="*",
        metavar="NAME",
        help="A short index of the tables, views, joins, canned queries, functions, examples "
        "and rules; with NAMEs (any of those, or all), those in full",
    )
    queried(sub)
    sub = commands.add_parser(
        "queries",
        help="Canned evidence queries: no SQL needed",
        epilog=_QUERIES_HELP,
        formatter_class=_QueriesHelpFormatter,
    )
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
        "--run-module",
        metavar="MODULE",
        help="Python: run pytest, as `python -m pytest` does (--run-module pytest -- TEST)",
    )
    python_option(start)
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
    build_option(start)
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
        (
            "report",
            "State, where it is stopped, its hits and steps so far, and program output "
            "(--json also lists the worker's requests)",
        ),
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
            "... as `session inspect --json` lists `frames` (10, or up to N; at most 64)",
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
        usage="%(prog)s target (--check CHECK | --check-command COMMAND) (--old OLD --new NEW "
        "[--old OLD --new NEW ...] | --baseline)\n                    [--at AT] [--config "
        "CONFIG] [--json] [--keep] [--details] [--timeout TIMEOUT]\n                    "
        "[--python PATH] [--quiet] [--background] [--check-command -- WORD ...]",
        description="Build the program fresh with the edit and run the trusted checker on it. "
        "--old and --new (or --old-file/--new-file) are required, once per edit, unless "
        "--baseline, which checks the unedited program instead: the checker should fail there "
        "before a fix's `passed: True` means anything. For Python, --check-command runs the "
        "project's own test command instead, in a fresh copy of the project with the edit.",
    )
    common(sub)
    sub.add_argument("--at", help="Program path (for multi-file targets)")
    sub.add_argument("--check", help="Trusted checker script (or --check-command)")
    sub.add_argument(
        CHECK_COMMAND,
        metavar="COMMAND",
        help="Python: instead of a checker, run this test command in a fresh copy of the "
        "project with the edit, as `python -m pytest tests/test_x.py -q`; it passes if it exits "
        "0. No shell: put words with shell characters after `--check-command --`, last",
    )
    sub.add_argument("--check-argv", action="append", help=argparse.SUPPRESS)
    python_option(sub)
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
    sub = commands.add_parser(
        "build",
        help="Build a Rust workspace for probes: its analysed packages, what runs",
        description="Build the Rust workspace a configuration names (analyse, run, vendor), "
        "offline, into Peeky's own directory. Probes use the build while its files are "
        "unchanged, and never build themselves (add --build to one to build first).",
    )
    common(sub)
    sub.add_argument(
        "--timeout",
        type=float,
        help="Fail with TIMEOUT after this many seconds (default and most: 900)",
    )
    sub.add_argument("--quiet", action="store_true", help="No progress messages on stderr")
    sub.add_argument(
        "--worker",
        help="Build on a running worker at this loopback URL, from the worker's own "
        "--rust-config (no --config here); token from $PEEKY_WORKER_TOKEN",
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
    sub = commands.add_parser(
        "setup-java",
        help="Build the Java debugger adapter once (needs JDK 17+ and network access)",
        description="Build the adapter Peeky debugs Java with: java-debug and its jars, "
        "downloaded and checked against pinned SHA-256s, patched, and compiled with Peeky's "
        "launcher. Without --output it goes to $PEEKY_STATE_DIR/java-adapter "
        '(~/.peeky/java-adapter), which Java configs without "bundle" use. One there that is '
        "up to date is kept; otherwise a new version is built and switched to. Older versions "
        "stay, for sessions using them, until --prune.",
    )
    sub.add_argument(
        "--output",
        metavar="DIR",
        help='Build into this new directory instead, and name it as a config\'s "bundle"',
    )
    sub.add_argument(
        "--force", action="store_true", help="Rebuild Peeky's own bundle even if up to date"
    )
    sub.add_argument(
        "--prune",
        action="store_true",
        help="Remove every version of Peeky's own bundle but the current one (a session "
        "still using one fails its next step)",
    )
    sub.add_argument("--quiet", action="store_true", help="No progress message")
    common(sub, target=False)
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
        "build": build_workspace,
        "job": job,
        "setup-java": setup_java,
    }

    def terminate(signum, frame):
        # Unwind so `finally` blocks close debuggers and owned debuggees.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    _NOTES.clear()
    from peeky.errors import relayed

    # A worker's probe child: its errors' text stays long, for the worker to remove its
    # token from, then cut (errors.bound).
    with relayed() if getattr(args, "relay_errors", False) else contextlib.nullcontext():
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

            if isinstance(exc, OutsideTarget):
                exc = _outside(exc, args)

            payload = classify(exc, session_closed=False if isinstance(exc, ValueError) else None)
            if getattr(args, "json", False):
                shown = {"error": payload}
                if getattr(args, "command", None) == "probe" and getattr(
                    exc, "before_launch", False
                ):
                    # As a probe the worker refused: its launch request was not sent.
                    shown = {"status": "failed", **shown, "refused_before_launch": True}
                print(json.dumps(shown, indent=2))
            # Whatever the command printed comes before why it failed.
            sys.stdout.flush()
            print(f"peeky: [{payload.get('code')}] {payload.get('message')}", file=sys.stderr)
            print(f"  hint: {payload.get('hint', '')}", file=sys.stderr)
            if payload.get("cleanup"):
                print(f"  cleanup: {payload['cleanup']}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main())
