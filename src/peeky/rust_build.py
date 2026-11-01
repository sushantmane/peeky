"""Peeky's own build of a Rust workspace (ADR 0008, phase 2), and the manifest a probe
checks before it uses the build.

`peeky build` runs Cargo in the workspace itself: offline, from its lockfile, with every
crates.io dependency from the vendored copies (rust_workspace). Cargo's target directory,
its CARGO_HOME and what the wrapper writes go in a private directory of Peeky's state
directory, one per root and configuration. Dependencies compiled once are reused. The
analysed packages are compiled every time: Peeky first removes their artifacts (`cargo
clean -p`). The wrapper then dumps their MIR, expands their source (`-Zunpretty=expanded`),
emits their LLVM IR, and records their rustc commands, so their opt-level is checked
whatever a profile says.

The manifest records the tools, the files Cargo read (manifests, the lockfile, Cargo's
configuration in and above the workspace), every local package's sources from rustc's
dependency files, the vendored copies' checksum files, and the build's outputs. A probe
uses the build only while all of it holds, and never builds (`--build` asks for one
first). Trusted code: Cargo runs the project's build scripts and procedural macros, as
the program runs. Not a sandbox.
"""

import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from peeky import rust_workspace
from peeky.build import BuildCommandError, UnreadDependencies, dependency_paths, real_tool
from peeky.digest import sha256_file
from peeky.stopping import Stopped, check_stop
from peeky.stopping import listed as _listed
from peeky.stopping import walked as _walked
from peeky.textlines import split_lines

# The longest a build may take, a cold one included (`peeky build --timeout`).
BUILD_TIMEOUT_S = 900
# The most files a build's manifest records (sources, generated files and outputs).
MAX_RECORDED = 65536
# What Peeky keeps of Cargo's output: its JSON messages name every artifact.
CARGO_RETAINED = 64 * 1024 * 1024
CARGO_OUTPUT_LIMIT = 256 * 1024 * 1024
# Builds kept, the newest first: a session on the one before the last keeps its files.
KEEP_RUNS = 2
# A file changed this close to the build's start may have changed while it ran.
_SETTLE_NS = 1_000_000_000
# The most files Peeky notes the state of before a build, to tell a change during it.
_MAX_SNAPSHOT = 200_000
# 2: the build's crates (`crates`), its local procedural macros (`macros`), and analysed
# MIR with full paths. 3: each analysed unit's generated files (`generated`).
SCHEMA = 5

# Cargo runs this for the workspace's members (RUSTC_WORKSPACE_WRAPPER). For an analysed
# target's own compile, which it knows by crate name and source file (a build script's
# probes of rustc run it too), it records the command, then:
# 1. runs Cargo's command as Cargo gave it, in Cargo's environment, writing only metadata
#    into the unit's directory: what stable rustc refuses (a `#![feature]`), the build
#    fails on, in rustc's words, as a stable build would;
# 2. expands the crate in another rustc process (`-Zunpretty` compiles nothing; its
#    messages go to a file, not to Cargo);
# 3. compiles it with MIR dumped and LLVM IR emitted. The proofs read that MIR, so the
#    program must be built from it: no MIR optimizations, legacy mangling. Those flags
#    are nightly ones, which RUSTC_BOOTSTRAP set to the crate's name allows for that
#    crate alone, in that process's environment (the wrapper's own is never changed).
# Every other compile runs unchanged. Python, not a shell: dash drops variables like
# CARGO_BIN_EXE_<bin-with-dashes>.
WRAPPER = r"""import json
import os
import shutil
import stat
import subprocess
import sys

args = sys.argv[1:]
crate = None
for previous, arg in zip(args, args[1:]):
    if previous == "--crate-name":
        crate = arg
wanted = json.loads(os.environ.get("PEEKY_ANALYSED") or "{}").get(crate or "", [])
cwd = os.getcwd()
source = None
for arg in args[1:]:
    if not arg.startswith("-") and os.path.realpath(os.path.join(cwd, arg)) in wanted:
        source = os.path.realpath(os.path.join(cwd, arg))
if source is not None:
    options = [b for a, b in zip(args, args[1:]) if a == "-C"]
    options += [a[2:] for a in args if a.startswith("-C") and len(a) > 2]
    extra = ""
    for option in options:
        if option.startswith("extra-filename="):
            extra = option.split("=", 1)[1]
    unit = os.path.join(os.environ["PEEKY_UNITS"], crate + extra)
    os.makedirs(unit)
    names = ("CARGO_PKG_NAME", "CARGO_PKG_VERSION", "CARGO_MANIFEST_DIR", "CARGO_CRATE_NAME",
             "CARGO_PRIMARY_PACKAGE", "OUT_DIR")
    with open(os.path.join(unit, "command.json"), "w") as out:
        json.dump({"args": args, "cwd": cwd, "source": source,
                   "env": {name: os.environ[name] for name in names if name in os.environ}}, out)
    expand, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in ("--emit", "--out-dir", "-o"):
            skip = True
        elif not arg.startswith(("--emit=", "--out-dir=")):
            expand.append(arg)
    # Each process has Cargo's environment, its jobserver's descriptors passed on
    # (close_fds=False); the expansion and the compile, RUSTC_BOOTSTRAP too.
    nightly = {**os.environ, "RUSTC_BOOTSTRAP": crate}
    stable = os.path.join(unit, "stable")
    gate = subprocess.run(
        [*expand, "--emit=metadata", "--out-dir", stable],
        env=dict(os.environ), stdin=subprocess.DEVNULL, capture_output=True, close_fds=False,
    )
    if gate.returncode != 0:
        sys.stdout.buffer.write(gate.stdout)
        sys.stdout.buffer.flush()
        # Not its notice of the metadata it wrote: Cargo would take it for its own.
        lines = gate.stderr.split(b"\n")
        sys.stderr.buffer.write(b"\n".join(
            line for line in lines if not line.startswith(b'{"artifact":')))
        sys.stderr.buffer.flush()
        sys.exit(gate.returncode)
    shutil.rmtree(stable, ignore_errors=True)
    with open(os.path.join(unit, "expand.log"), "wb") as log:
        done = subprocess.run(
            [*expand, "-Zunpretty=expanded", "-o", os.path.join(unit, "expanded.rs")],
            env=nightly, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=False,
        )
    with open(os.path.join(unit, "expand.json"), "w") as out:
        json.dump({"exit_code": done.returncode}, out)
    for index, arg in enumerate(args):
        if arg.startswith("--emit=") and "llvm-ir" not in arg[len("--emit="):].split(","):
            args[index] = arg + ",llvm-ir"
    args += [
        "-Zmir-opt-level=0",
        "-Zdump-mir=all & PreCodegen",
        "-Zdump-mir-dir=" + os.path.join(unit, "mir"),
        "-Zmir-include-spans=yes",
        "-Zunstable-options",
        "-Csymbol-mangling-version=legacy",
        # Every path in full (`smallnum::one`, `std::vec::Vec`): a dependency's call is
        # told by its crate. It changes no code rustc generates.
        "-Ztrim-diagnostic-paths=false",
    ]
    os.execve(args[0], args, nightly)
os.execv(args[0], args)
"""
WRAPPER_SHA256 = hashlib.sha256(WRAPPER.encode()).hexdigest()


class BuildRequired(RuntimeError):
    """No build of this workspace and configuration (errors: BUILD_REQUIRED)."""


@dataclass(frozen=True)
class RustWorkspaceTarget:
    """A Rust workspace with dependencies (rust_workspace.WorkspaceConfig), built by
    `peeky build` and probed in its analysed packages. ``build_command``: the command that
    builds it, as a refusal names it."""

    config: rust_workspace.WorkspaceConfig
    cargo: str
    rustc: str
    adapter: str
    dwarfdump: str
    linker: str
    dsymutil: str | None = None
    sysroot: str | None = None
    arguments: tuple[str, ...] = ()
    rust_version: str = "1.96.0"
    build_command: str = "peeky build <target> --config <config>"

    language = "rust"
    accepts_arguments = True
    supports_deadline = True
    # Candidate edits in a workspace come later (ADR 0008, phase 3).
    supports_edits = False
    verification_inputs = ()

    def __post_init__(self):
        from peeky.rust import RustTarget, _absolute_tool, _toolchain

        RustTarget.validate_arguments(self.arguments)
        if not isinstance(self.rust_version, str) or not re.fullmatch(
            r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", self.rust_version
        ):
            raise ValueError("Invalid pinned Rust version")
        _toolchain(self.cargo, self.rustc)
        for label in ("adapter", "dwarfdump", "linker"):
            _absolute_tool(getattr(self, label), label)
        if sys.platform == "darwin":
            if self.dsymutil is None or Path(self.dsymutil).name != "dsymutil":
                raise ValueError("macOS Rust targets require an explicit tool named dsymutil")
            _absolute_tool(self.dsymutil, "dsymutil")
        elif sys.platform != "linux":
            raise ValueError("Rust target supports Linux and macOS hosts only")

    @classmethod
    def from_config(cls, data, build_command=None):
        """The target a trusted workspace configuration names (tools included)."""
        config, rest = rust_workspace.workspace_config(data)
        rest = {key: tuple(v) if isinstance(v, list) else v for key, v in rest.items()}
        if build_command is not None:
            rest["build_command"] = build_command
        return cls(config, **rest)

    @staticmethod
    def validate_arguments(arguments):
        from peeky.rust import RustTarget

        RustTarget.validate_arguments(arguments)

    @staticmethod
    def validate_breakpoints(points):
        from peeky.adapters import PythonAdapter

        PythonAdapter().validate_breakpoints(points)
        if any(set(point) != {"line"} for point in points):
            raise ValueError("Rust targets support unconditional line breakpoints only")

    @property
    def target_identity(self):
        return {
            "language": "rust",
            "workspace": self.config.workspace,
            "analyse": list(self.config.analyse),
            "run": asdict(self.config.run),
            "features": list(self.config.features),
            "arguments": list(self.arguments),
            "rust_version": self.rust_version,
            "wrapper_sha256": WRAPPER_SHA256,
        }

    def identity(self, root):
        """What a build depends on besides the files it records: the root, the
        configuration (less the program's arguments) and the tools' paths."""
        tools = {
            name: getattr(self, name)
            for name in ("cargo", "rustc", "linker", "dsymutil", "sysroot", "adapter", "dwarfdump")
        }
        identity = {
            "schema": SCHEMA,
            "root": str(Path(root).resolve()),
            "config": asdict(self.config),
            "tools": tools,
            "rust_version": self.rust_version,
            "wrapper_sha256": WRAPPER_SHA256,
        }
        # As the manifest's JSON keeps it (lists, not tuples).
        return json.loads(json.dumps(identity))

    def rustflags(self):
        from peeky.rust import _absolute_tool

        flags = ["-C", "dwarf-version=4", "-C", f"linker={_absolute_tool(self.linker, 'linker')}"]
        if sys.platform == "linux":
            flags += ["-C", "link-arg=-Wl,--build-id=sha1"]
        return flags

    def build(self, root, *, deadline=None, progress=None, stop=None):
        """Build the workspace (`peeky build`): a summary of the build. ``stop``: see
        ``build``."""
        return build(self, root, deadline=deadline, progress=progress, stop=stop)

    def current(self, root):
        """The current build's manifest (current_build)."""
        return current_build(self, root)

    def compared_names(self, root):
        """The files under ``root`` a remote client's are compared with
        (`sources.declared`): the recorded build's (``compared_sources``). Where none is
        recorded, the workspace's manifest and lockfile: a probe is refused for that once
        they compare (BUILD_REQUIRED)."""
        found = recorded_comparison(self, root or ".")
        if found is not None:
            return found[0]
        workspace = Path(self.config.workspace)
        return tuple(
            (workspace / name).as_posix().removeprefix("./")
            for name in ("Cargo.toml", "Cargo.lock")
        )

    def remote_refusal(self, root):
        """Why a remote client can't compare the recorded build with its own files
        (``remote_refusal``), or None."""
        found = recorded_comparison(self, root or ".")
        return None if found is None else found[1]

    def compared_trees(self, root):
        """{directory: "exact"} of the recorded build (``compared_trees``), compared with a
        client's entry by entry; {} where none is recorded."""
        found = recorded_comparison(self, root or ".")
        return {} if found is None else found[2]

    def trusted_config(self):
        """The trusted configuration this target is (``from_config`` reads it back as the
        same target): what a worker's probe process is given."""
        config, run = self.config, self.config.run
        data = {
            "language": "rust",
            "workspace": config.workspace,
            "analyse": list(config.analyse),
            "run": {run.kind: True if run.kind == "lib" else run.name}
            | ({} if run.package is None else {"package": run.package}),
            "features": list(config.features),
            "arguments": list(self.arguments),
            "rust_version": self.rust_version,
        }
        if config.vendor is not None:
            data["vendor"] = config.vendor
        for name in ("cargo", "rustc", "adapter", "dwarfdump", "linker", "dsymutil", "sysroot"):
            if (value := getattr(self, name)) is not None:
                data[name] = value
        return data

    def prepare(self, program, root, *, edit=None, deadline=None):
        if edit is not None:
            raise ValueError(
                "Peeky doesn't verify or probe candidate edits in a Rust workspace yet; "
                "change the source and run `peeky build` again"
            )
        from peeky.rust_workspace_session import prepare_session

        return prepare_session(self, program, root, deadline=deadline)


# {a build's manifest's path: (its status when read, ``compared_sources``' names, and
# ``remote_refusal``'s)}: a worker asks at every `version` and launch.
_RECORDED = {}


def recorded_comparison(target, root):
    """(the names of ``compared_sources``, ``remote_refusal``, ``compared_trees``) of
    ``target``'s build under ``root`` as recorded, unchecked (a probe checks the build:
    ``current_build``); None where none is recorded, or it is another configuration's or
    Peeky's."""
    path = build_directory(target, root, create=False) / "manifest.json"
    try:
        status = path.stat()
    except OSError:
        return None
    key = (status.st_ino, status.st_mtime_ns, status.st_size)
    if (cached := _RECORDED.get(path)) is not None and cached[0] == key:
        return cached[1]
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != SCHEMA
        or manifest.get("identity") != target.identity(root)
    ):
        return None
    found = (
        tuple(compared_sources(manifest)),
        remote_refusal(manifest),
        compared_trees(manifest),
    )
    _RECORDED[path] = (key, found)
    return found


def compared_sources(manifest):
    """{name under the root: SHA-256 the build recorded} of what a remote client's files are
    compared with: what Cargo read that identifies the build (every manifest, the lockfile,
    the workspace's own Cargo configuration: ``inputs``), every file of the workspace the
    build compiled (each local package's compiles, the program's own included, from rustc's
    dependency files: ``sources``), each vendored copy's `.cargo-checksum.json`, which
    lists its files' hashes (not every vendored file), and what local build scripts and
    procedural macros may read (``_watched_inputs``); none of the checkout's metadata
    (``_checkout_metadata``)."""
    found = {**manifest["inputs"], **manifest["sources"]}
    state = manifest["watch"]
    prefix = Path(state["workspace"]).resolve().relative_to(Path(manifest["root"]).resolve())
    for name, digest in state["vendored"].items():
        if PurePosixPath(name).name == ".cargo-checksum.json":
            found[(prefix / name).as_posix()] = digest
    found.update(_watched_inputs(manifest)[0])
    # Cargo's dependency file of the binary lists its build scripts' inputs too.
    return {name: found[name] for name in sorted(found) if not _checkout_metadata(name)}


def _checkout_metadata(path):
    """Whether ``path`` is under a `.git` directory, or is a `.git` file: the checkout's
    metadata, not the project's source, which differs between two checkouts of the same
    sources (a build script watching `.git/HEAD` for its version). Never compared with a
    remote client's."""
    return ".git" in PurePosixPath(Path(path).as_posix()).parts


def compared_trees(manifest):
    """{directory under the root: "exact"} of what a client's checkout is compared with
    entry by entry (`sources.structured`): each directory a local build script's
    `rerun-if-changed` names, as Cargo scans it. A package compared whole is compared by
    the files its record lists (``_package_files``), not entry by entry: a client's file
    Cargo leaves out, such as a git-ignored one, is no difference. None under `.git`, or
    outside the root (``remote_refusal`` refuses that)."""
    root = Path(manifest["root"]).resolve()
    trees = {}
    for top in (manifest.get("build_inputs") or {}).get("dirs") or {}:
        try:
            name = Path(top).relative_to(root).as_posix()
        except ValueError:
            continue
        if not _checkout_metadata(name):
            trees[name] = "exact"
    return dict(sorted(trees.items()))


def remote_refusal(manifest):
    """Why a remote client can't compare the build's files with its own, or None: a local
    build script's input that is outside the root, or that a client can't send a hash of
    (missing, unreadable, not a file) (``_watched_inputs``)."""
    return _watched_inputs(manifest)[1]


_UNCOMPARED = (
    "A remote client can't compare what this build read with its own files: a build script "
    "says it reads {what}. Run Peeky on the worker's host, or {remedy}"
)


def _watched_inputs(manifest):
    """({name under the root: SHA-256} of what local build scripts and procedural macros may
    read, as the build recorded it (``_build_inputs``): each `rerun-if-changed` file, each
    file under a `rerun-if-changed` directory, followed through links, and each file of a
    package compared whole, as its record lists them (``_package_files``); the refusal of the
    first a client can't compare, or None). Anything under a `.git` directory is left out: it is the checkout's metadata, not
    the project's source, and differs between two checkouts of the same sources (a build
    script watching `.git/HEAD` for its version). A directory is compared by its files."""
    root = Path(manifest["root"]).resolve()
    inputs = manifest.get("build_inputs") or {}
    found, refusals = {}, []

    def add(path, digest, why=None):
        path = Path(path)
        if _checkout_metadata(path):
            return
        try:
            name = path.relative_to(root).as_posix()
        except ValueError:
            refusals.append(
                _UNCOMPARED.format(
                    what=f"{path}, outside the worker's root {root}, which no client has a copy of",
                    remedy="move it into the workspace",
                )
            )
            return
        if digest is None:
            refusals.append(
                _UNCOMPARED.format(
                    what=f"{name}, {why} on the worker's host, and a client sends only the "
                    "hashes of files it has",
                    remedy="make it a file, or take the line out of the build script",
                )
            )
            return
        found[name] = digest

    for path, identity in (inputs.get("files") or {}).items():
        if identity and identity[0] in ("file", "link") and len(identity) == 3:
            add(path, identity[2])
        else:
            add(path, None, "missing" if identity == ["missing"] else "not a file")
    for top, recorded in (inputs.get("dirs") or {}).items():
        entries = recorded.get("entries")
        if not isinstance(entries, dict):
            add(top, None, f"a directory that is {entries}")
            continue
        for relative, entry in entries.items():
            path = Path(top) / relative
            if entry == "dir" or (isinstance(entry, list) and entry[2:3] == ["dir"]):
                continue
            if isinstance(entry, str) and entry != "other":
                add(path, entry)
            elif isinstance(entry, list) and isinstance(entry[2], str):
                add(path, entry[2])
            else:
                add(path, None, "not a file")
    for top, listed in (inputs.get("lists") or {}).items():
        for relative, digest in listed["files"].items():
            if isinstance(digest, str):
                add(Path(top) / relative, digest)
            elif not _checkout_metadata(relative):
                what, remedy = _UNLISTABLE[digest["refused"]]
                refusals.append(
                    "A remote client can't compare what this build read with its own files: "
                    f"{_shown(Path(top) / relative, root)} is {what} on the worker's host, "
                    "among the files of a package a build may read whole. Run Peeky on the "
                    f"worker's host, or {remedy}"
                )
    return found, (sorted(refusals)[0] if refusals else None)


def build_directory(target, root, *, create=True):
    """Peeky's private directory for this root's and configuration's builds."""
    from peeky.cli import _private_dir

    key = hashlib.sha256(
        json.dumps(target.identity(root), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    base = _private_dir("rust-builds") if create else _state_path("rust-builds")
    directory = base / key
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.lstat().st_uid != os.getuid():
            raise ValueError(f"Peeky's build directory must be a directory you own: {directory}")
    return directory


def _state_path(name):
    from peeky import state_home

    return state_home() / name


@contextlib.contextmanager
def _locked(directory, exclusive):
    """Hold the build directory's lock: a build alone, or any number of readers."""
    path = Path(directory) / "lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "A `peeky build` of this workspace is running"
                + (", or a probe is reading it" if exclusive else "")
                + ": wait for it to end, then retry"
            ) from None
        yield
    finally:
        os.close(descriptor)


def _write_wrapper(directory):
    """The wrapper at a fixed path (Cargo keeps a member's build while the wrapper's path
    stays), with its interpreter: this Python, isolated (-I)."""
    folder = Path(directory) / "wrapper"
    folder.mkdir(mode=0o700, exist_ok=True)
    interpreter, wrapper = folder / "python", folder / "rustc-peeky"
    target = Path(sys.executable).resolve()
    if interpreter.is_symlink() and Path(os.readlink(interpreter)) != target:
        interpreter.unlink()
    if not interpreter.is_symlink():
        interpreter.symlink_to(target)
    text = f"#!{interpreter} -I\n{WRAPPER}"
    if not wrapper.is_file() or wrapper.is_symlink() or wrapper.read_text() != text:
        wrapper.unlink(missing_ok=True)
        wrapper.write_text(text)
    wrapper.chmod(0o700)
    return wrapper, text


def _run(argv, cwd, env, deadline, what):
    """Run a Cargo command within the build's deadline; BuildCommandError on failure."""
    from peeky.agent import execute

    remaining = BUILD_TIMEOUT_S if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"The build exceeded its deadline before {what}")
    result = execute(
        list(argv),
        cwd,
        remaining,
        env=env,
        output_limit=CARGO_OUTPUT_LIMIT,
        retain=CARGO_RETAINED,
    )
    if result.get("timed_out"):
        raise TimeoutError(f"The build exceeded its deadline during {what}") from (
            BuildCommandError(tuple(argv), result)
        )
    if result["exit_code"] != 0:
        raise BuildCommandError(tuple(argv), result)
    if len(result["stdout"].encode()) >= CARGO_RETAINED:
        raise ValueError(f"Cargo wrote more output than Peeky keeps during {what}")
    return result


def _environment(target, home, directory, wrapper, analysed, units, project=()):
    """rust_workspace.build_environment (the target directory, profile and rustflags,
    ``project``'s `--cfg` ones included, that Cargo's unit graph was read with), plus the
    wrapper's and the macOS tools'."""
    env = rust_workspace.build_environment(
        target.rustc, home, Path(directory) / "target", (*target.rustflags(), *project)
    )
    tools = [str(Path(target.rustc).parent)]
    if target.dsymutil:
        # rustc runs `dsymutil` from PATH for packed macOS symbols.
        tools.insert(0, str(Path(real_tool(target.dsymutil)).parent))
    env["PATH"] = ":".join((*dict.fromkeys(tools), "/usr/bin", "/bin"))
    if target.sysroot is not None:
        env["SDKROOT"] = str(Path(target.sysroot).resolve(strict=True))
    env.update(
        {
            "RUSTC_WORKSPACE_WRAPPER": str(wrapper),
            "PEEKY_ANALYSED": json.dumps(analysed, sort_keys=True),
            "PEEKY_UNITS": str(units),
        }
    )
    return env


def _hash(path, store=None):
    """``path``'s SHA-256; first, a stopped build ends here (agent.check_stop): every file
    the build hashes is a step it checks its stop at."""
    check_stop()
    return sha256_file(path, store=store)


def _digest(store):
    """A SHA-256 function that keeps digests in ``store`` (digest.sha256_file's rules), and
    ends a stopped build first (``_hash``)."""
    return lambda path: _hash(path, store=store)


def _snapshot(shape, vendor):
    """What is there before Cargo runs, to tell what changed while it ran from what changed
    before: {"l:" path: (mtime, ctime, size), not followed} of every entry, directories
    and links too, and {"f:" path: the same, followed} of each link; {"d:" directory:
    True} of each directory listed whole. Everything under the workspace, hidden
    directories (`.git`) included, and under each local package outside it; not a
    `target` directory at their tops. What its absence of an entry proves
    (_absent_before) depends on ":tops", ":skipped", ":uncovered" (each path Peeky
    couldn't list or read, and why) and ":limit" (where it stopped, at _MAX_SNAPSHOT
    entries)."""
    tops = [shape.workspace.resolve()]
    for package in shape.local:
        top = (shape.root / package.directory).resolve()
        if not any(top.is_relative_to(each) for each in tops):
            tops.append(top)
    skipped = [str(top / "target") for top in tops]
    uncovered = []
    found = {":tops": [str(top) for top in tops], ":skipped": skipped, ":uncovered": uncovered}
    found[":limit"] = None
    count, seen = 0, set()

    def failed(error):
        uncovered.append([str(error.filename or "?"), error.strerror or str(error)])

    def record(path):
        """Record ``path`` (lstat, and a link's target); False once the limit is reached."""
        nonlocal count
        check_stop()
        if "l:" + path in found:
            # A directory, recorded as its parent's entry.
            return True
        if count >= _MAX_SNAPSHOT:
            found[":limit"] = [count, os.path.dirname(path)]
            return False
        try:
            info = os.lstat(path)
        except OSError as error:
            failed(error)
            return True
        found["l:" + path] = (info.st_mtime_ns, info.st_ctime_ns, info.st_size)
        count += 1
        if stat.S_ISLNK(info.st_mode):
            try:
                info = os.stat(path)
                found["f:" + path] = (info.st_mtime_ns, info.st_ctime_ns, info.st_size)
            except FileNotFoundError:
                # A dangling link, as such.
                found["f:" + path] = None
            except OSError as error:
                failed(error)
        return True

    for top in tops:
        pending = [os.path.normpath(top)]
        while pending:
            base = pending.pop()
            if base in seen:
                continue
            seen.add(base)
            if not record(base):
                return found
            # Read lazily, each entry recorded (and the limit checked) as scandir yields it.
            try:
                with os.scandir(base) as listing:
                    for entry in listing:
                        path = os.path.normpath(entry.path)
                        if not record(path):
                            return found
                        if entry.is_dir(follow_symlinks=False) and path not in skipped:
                            pending.append(path)
            except OSError as error:
                failed(error)
                continue
            # Listed whole: an entry it doesn't name wasn't there.
            found["d:" + base] = True
    return found


# What to do where Peeky can't tell whether a missing input was there (_absent_before).
_ABSENCE_REMEDY = {
    "limit": f"Keep fewer than {_MAX_SNAPSHOT:,} entries (files and directories) under the "
    "workspace (move data the build doesn't use out of it), or take the line out of the "
    "build script",
    "outside": "Create it, or take the line out of the build script",
    "unread": "Make that path readable, or take the line out of the build script",
    "there": "Take the line out of the build script, or name the path without a link",
}


def _absent_before(path, before):
    """None where ``before`` (_snapshot's) proves ``path`` wasn't there before Cargo ran,
    else why it doesn't: it stopped at its limit; the path is outside what it records
    (its tops, less what it skipped); a directory on the way couldn't be listed or read;
    or the path, or the first of its directories that wasn't listed, was there."""
    path = Path(os.path.normpath(path))
    if before.get(":limit"):
        count, _ = before[":limit"]
        return (
            "limit",
            f"Peeky's record of the workspace before the build stopped at its limit, {count:,} "
            "entries",
        )
    if not any(path.is_relative_to(top) for top in before.get(":tops", ())) or any(
        path.is_relative_to(each) for each in before.get(":skipped", ())
    ):
        return (
            "outside",
            "it is outside what Peeky records before a build (the workspace, less a `target` "
            "directory at its top)",
        )
    if (found := _uncovered_on(path, before)) is not None:
        failing, why = found
        return ("unread", f"Peeky couldn't read {failing} before the build ({why})")
    # The deepest directory on the way that was listed whole: the next step down from it
    # wasn't among its entries, so nothing under it was there.
    if (ancestor := _listed_ancestor(path, before)) is not None:
        step = ancestor / path.relative_to(ancestor).parts[0]
        if "l:" + str(step) in before:
            return ("there", f"{step} was there before the build")
        return None
    return ("outside", "it is outside what Peeky records before a build")


def _uncovered_on(path, before):
    """(a path _snapshot couldn't read, why) that is ``path``, on its way, or under it; or
    None. Looked up by ``path`` and its directories in an index of the uncovered paths and
    every directory on their way, made once per snapshot: never a scan per path."""
    index = before.get(":uncovered-index")
    if index is None:
        index = {}
        for failing, why in before.get(":uncovered", ()):
            for each in (Path(failing), *Path(failing).parents):
                index.setdefault(str(each), (failing, why))
        before[":uncovered-index"] = index
        before[":uncovered-paths"] = {
            str(Path(failing)) for failing, _ in before.get(":uncovered", ())
        }
    if (found := index.get(str(path))) is not None:
        # ``path`` is uncovered, or on the way to one: under it.
        return found
    paths = before[":uncovered-paths"]
    for each in path.parents:
        if str(each) in paths:
            return index[str(each)]
    return None


def _dangling_on_the_way(path, before):
    """(the link, what it names, "doesn't exist" or "didn't exist before the build") where
    ``path``, as written, is or passes through a link to nothing, now or in _snapshot's
    record from before Cargo ran; else None. What a build script read through one, Peeky
    can't tell: what it names may have been there while the script ran."""
    path = Path(path)
    for prefix in (*reversed(path.parents), path):
        if not os.path.islink(prefix):
            continue
        target = Path(os.path.normpath(Path(prefix).parent / os.readlink(prefix)))
        if not os.path.exists(prefix):
            return prefix, target, "doesn't exist"
        key = "f:" + os.path.normpath(prefix)
        if key in before and before[key] is None:
            return prefix, target, "didn't exist before the build"
    return None


def _listed_ancestor(path, before):
    """The deepest directory on ``path``'s way that _snapshot listed whole, or None."""
    return next(
        (ancestor for ancestor in Path(path).parents if before.get("d:" + str(ancestor))), None
    )


def _changed_on_the_way(path, before):
    """Where ``path`` was proven absent before Cargo ran (_absent_before) and is absent
    after it: the directory on its way that changed meanwhile, or None. That is the deepest
    one listed whole, whose entries' changes change its own status, or one under it, which
    wasn't there: a file made, read and removed while Cargo ran passes through them."""
    path = Path(os.path.normpath(path))
    ancestor = _listed_ancestor(path, before)
    for directory in (ancestor, *(each for each in path.parents if ancestor in each.parents)):
        try:
            info = os.lstat(directory)
            now = (info.st_mtime_ns, info.st_ctime_ns, info.st_size)
        except OSError:
            now = None
        if tuple(before.get("l:" + str(directory)) or ()) != tuple(now or ()):
            return directory
    return None


def _messages(stdout):
    found = []
    for line in split_lines(stdout):
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(message, dict):
            found.append(message)
    return found


def _stem(filename):
    """A compile's output name as rustc gives it, `<crate><extra-filename>`, from one of its
    files (`libutil-1a2b….rlib`, `app-1a2b…`)."""
    name = Path(filename).name
    for suffix in (".rlib", ".rmeta", ".so", ".dylib", ".a", ".dll", ".dSYM"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            if suffix != ".dSYM" and name.startswith("lib"):
                name = name[3:]
            break
    return name


def _codegen(args):
    """rustc's codegen options of a recorded command (`-C k=v`, `-Ck=v`, `--codegen k=v`,
    `-O`, `-g`), the last of each winning."""
    found, index = {}, 0
    while index < len(args):
        arg, option = args[index], None
        if arg in ("-C", "--codegen") and index + 1 < len(args):
            option, index = args[index + 1], index + 1
        elif arg.startswith("-C") and len(arg) > 2:
            option = arg[2:]
        elif arg.startswith("--codegen="):
            option = arg[len("--codegen=") :]
        elif arg == "-O":
            option = "opt-level=2"
        elif arg == "-g":
            option = "debuginfo=2"
        index += 1
        if option is not None:
            key, _, value = option.partition("=")
            found[key] = value
    return found


def check_codegen(package, crate, args):
    """Refuse an analysed compile that Peeky's proofs don't hold for: optimized, without
    full debug information, stripped, or with link-time optimization."""
    options = _codegen(args)
    level = options.get("opt-level", "0")
    problems = []
    if level != "0":
        problems.append(f"`-C opt-level={level}`")
    if options.get("debuginfo", "0") not in ("2", "full"):
        problems.append(f"`-C debuginfo={options.get('debuginfo', '0')}`")
    if options.get("strip", "none") != "none":
        problems.append(f"`-C strip={options['strip']}`")
    if options.get("lto", "off") not in ("off", "no", "n", "false"):
        problems.append(f"`-C lto={options['lto']}`")
    if problems:
        raise ValueError(
            f"Peeky analyses only code built at opt-level 0 with full debug information: "
            f"`{package}`'s compile of {crate} had {', '.join(problems)}, from a profile in "
            "Cargo.toml or Cargo's configuration. Remove that setting for this package"
        )
    return level


def _dependency_file(filenames):
    """(the dependency file of a compile with these output files, its form): rustc's own,
    beside an output in `deps` or a build script's directory; else the one Cargo writes
    beside a binary it copies up (`target/debug/app.d`, make's form, which also lists the
    local packages it uses)."""
    paths = [Path(filename) for filename in filenames]
    for path in paths:
        candidate = path.with_name(_stem(path.name) + ".d")
        if (path.parent.name == "deps" or path.parent.parent.name == "build") and (
            candidate.is_file()
        ):
            return candidate, "rustc"
        if path.parent.parent.name == "build" and path.name == "build-script-build":
            # A build script, which Cargo names `build-script-build` beside rustc's own
            # `build_script_build-<hash>` and its dependency file.
            found = [
                Path(entry.path)
                for entry in _listed(
                    path.parent, [4096], f"Cargo's {path.parent} holds more than 4096 entries"
                )
                if fnmatch.fnmatchcase(entry.name, "build_script_build-*.d")
            ]
            if len(found) == 1:
                return found[0], "rustc"
    for path in paths:
        candidate = path.with_name(_stem(path.name) + ".d")
        if candidate.is_file():
            return candidate, "make"
    return None, None


def _changed_during(path, before, started_ns):
    """Whether the file ``path`` (followed) changed while Cargo ran: its status differs
    from _snapshot's, or, with none there, it changed after the build started."""
    info = os.stat(path)
    now = (info.st_mtime_ns, info.st_ctime_ns, info.st_size)
    key = ("f:" if os.path.islink(path) else "l:") + os.path.normpath(path)
    if key in before:
        return tuple(before[key] or ()) != now
    return max(info.st_mtime_ns, info.st_ctime_ns) >= started_ns - _SETTLE_NS


class BuildStopped(RuntimeError):
    """A build its ``stop`` ended (agent.Stopped): the process group it ran was killed and
    its leader reaped, or its next process never started."""

    def __init__(self, started):
        super().__init__(
            "The worker is shutting down, so it stopped its build: "
            + (
                "it killed Cargo and the processes Cargo ran. "
                if started
                else "its next process never started. "
            )
            + "Start the worker again, then build"
        )


def build(target, root, *, deadline=None, progress=None, stop=None):
    """`peeky build`: build ``target``'s workspace under ``root`` and write its manifest.
    ``stop`` (a threading.Event), once set, kills each process the build runs (Cargo, its
    build scripts and rustc) and starts none: a worker's shutdown. Every process the build
    runs is an ``execute`` in this thread (``_run``, ``_tools``, ``rust_workspace.read``),
    which takes it from ``stopping``. Only such an end is BuildStopped: any other error,
    a failed kill among them (with its ``process_group``), is raised as it is."""
    from peeky.stopping import Stopped, stopping

    with stopping(stop):
        try:
            return _build(target, root, deadline=deadline, progress=progress)
        except Stopped as exc:
            raise BuildStopped(exc.started) from None


def _build(target, root, *, deadline=None, progress=None):
    """``build``'s work."""
    say = progress or (lambda message: None)
    root = Path(root).resolve(strict=True)
    deadline = time.monotonic() + BUILD_TIMEOUT_S if deadline is None else deadline
    started, started_ns = time.monotonic(), time.time_ns()
    directory = build_directory(target, root)
    from peeky.build import _digest_store, require_private

    require_private(directory)
    with _locked(directory, exclusive=True):
        # What earlier builds left to remove, a bounded part of it each time.
        _empty_trash(directory, MAX_RECORDED)
        home = directory / "cargo-home"
        home.mkdir(mode=0o700, exist_ok=True)
        env = rust_workspace.cargo_environment(target.rustc, home)
        tools, probes = _tools(target, env)
        store = _digest_store()
        digest = _digest(store)
        shape = rust_workspace.read(
            root,
            target.config,
            target.cargo,
            target.rustc,
            home,
            directory / "target",
            min(60, _left(deadline)),
            rustflags=target.rustflags(),
            digest=digest,
        )
        # Cargo reads configuration in every directory above the workspace: none another
        # user could add between Peeky's check and Cargo's read.
        require_private(shape.workspace)
        vendor = target.config.vendor if shape.vendored else None
        wrapper, wrapper_text = _write_wrapper(directory)
        nonce = secrets.token_hex(8)
        run = directory / "runs" / nonce
        units = run / "units"
        units.mkdir(parents=True, mode=0o700)
        try:
            analysed = {}
            for package in shape.analysed:
                for each in package.targets:
                    if {"custom-build", "proc-macro"} & set(each.kinds):
                        continue
                    crate = each.name.replace("-", "_")
                    source = str((root / each.source).resolve())
                    analysed.setdefault(crate, []).append(source)
            env = _environment(target, home, directory, wrapper, analysed, units, shape.rustflags)
            before = _snapshot(shape, vendor)
            extra = (
                rust_workspace.vendor_arguments(shape.workspace, target.config.vendor)
                if vendor
                else ()
            )
            selected = [arg for package in shape.analysed for arg in ("-p", package.name)]
            say(f"building {shape.workspace} (analysed: {', '.join(target.config.analyse)})")
            _run(
                [target.cargo, "clean", "--offline", "--locked", *selected, *extra],
                shape.workspace,
                env,
                deadline,
                "`cargo clean`",
            )
            # A fixed number of jobs: a build script's NUM_JOBS (_script_value).
            jobs_ = jobs()
            result = _run(
                [
                    target.cargo,
                    *shape.cargo_arguments(),
                    "-j",
                    str(jobs_),
                    "--offline",
                    "--locked",
                    "--message-format=json-diagnostic-short",
                    *extra,
                ],
                shape.workspace,
                env,
                deadline,
                "the build",
            )
            if wrapper.read_text() != wrapper_text:
                raise RuntimeError("Provenance: Peeky's rustc wrapper changed during the build")
            _assert_tools(tools)
            messages = _messages(result["stdout"])
            manifest = _record(
                target, root, shape, directory, run, messages, before, started_ns, store
            )

            def lister(package):
                # `cargo package --list`: the files Cargo's default watch of a build script
                # covers (`include`, `exclude`, and with git, what it ignores).
                try:
                    listed = _run(
                        [target.cargo, "package", "--list", "--allow-dirty", "--offline"]
                        + ["--locked", "-p", package, *extra],
                        shape.workspace,
                        env,
                        deadline,
                        "`cargo package --list`",
                    )
                except BuildCommandError as exc:
                    lines = split_lines(exc.result.get("stderr") or "")
                    error = next((line for line in lines if line.startswith("error")), "")
                    why = f"`cargo package --list -p {package}` failed" + (
                        f" ({error.strip()})" if error else ""
                    )
                    return None, why
                # One name a line, each as it is, spaces too.
                return listed["stdout"].split("\n"), None

            manifest["build_inputs"] = _build_inputs(
                shape, root, messages, env, before, started_ns, vendor, jobs_, lister, directory
            )
            _watched_directories(manifest.pop("directories_read"), manifest, shape, vendor)
            # What Cargo read, as Peeky hashed it before Cargo ran: unchanged after the build.
            rust_workspace.changed(shape.watch, digest, "while Peeky built the workspace")
        except BaseException:
            # Never removed here: a stopped build ends at once, however much its run holds.
            # A later build removes it, a bounded part at a time (``_empty_trash``).
            _discard(directory, run)
            raise
        files = shape.watch["files"]
        manifest.update(
            schema=SCHEMA,
            identity=target.identity(root),
            nonce=nonce,
            tools=tools,
            probes=probes,
            inputs={name: files[str(root / name)][-1] for name in shape.inputs},
            watch=shape.watch,
            # The project's `--cfg` and `--check-cfg` rustflags the build kept.
            rustflags=list(shape.rustflags),
            configs_above=[list(each) for each in shape.configs_above],
            wrapper=str(wrapper),
            seconds=round(time.monotonic() - started, 2),
            built_at=time.time(),
        )
        manifest["build_id"] = _build_id(manifest)
        _write_manifest(directory, manifest)
        _prune(directory, nonce)
        say(f"built in {manifest['seconds']} s")
        return summary(manifest, directory)


def _left(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("The build exceeded its deadline")
    return remaining


def _tools(target, env):
    """[{path, sha256}] of the tools a build runs, and their version probes, which must
    name the pinned version."""
    from peeky.agent import execute

    paths = [target.cargo, target.rustc, target.linker]
    paths += [target.dsymutil] if target.dsymutil else []
    tools = [
        {"path": str(real_tool(path)), "sha256": sha256_file(real_tool(path))}
        for path in dict.fromkeys(p for p in paths if p)
    ]
    probes = {}
    for name, argv in (("rustc", (target.rustc, "-vV")), ("cargo", (target.cargo, "--version"))):
        done = execute(list(argv), env["CARGO_HOME"], 30, env=env)
        probes[name] = done["stdout"]
        if done["exit_code"] != 0 or not done["stdout"].startswith(
            f"{name} {target.rust_version} "
        ):
            raise ValueError(
                f"Rust toolchain does not match the pinned version {target.rust_version}: "
                f"{argv[0]} says {split_lines(done['stdout'])[:1]}"
            )
    return tools, probes


def _assert_tools(tools):
    for tool in tools:
        try:
            unchanged = sha256_file(tool["path"]) == tool["sha256"]
        except OSError:
            unchanged = False
        if not unchanged:
            raise RuntimeError(f"Provenance: the tool {tool['path']} changed during the build")


def _record(target, root, shape, directory, run, messages, before, started_ns, store):
    """The manifest's record of what the build compiled: the run's executable, the
    analysed units, every local package's sources, the generated files they read."""
    target_dir = (directory / "target").resolve()
    by_manifest = {str((root / package.manifest).resolve()): package for package in shape.local}
    analysed_names = {package.name for package in shape.analysed}
    run_package, run_target = shape.run
    executable = None
    sources, generated, units, macros = {}, {}, [], set()
    seen_units, directories = set(), set()
    # The analysed compiles the wrapper recorded, by crate, source file and whether a test.
    compiled = {}
    units_read = _listed(run / "units", [MAX_RECORDED], _TOO_MANY_UNITS)
    for unit in (Path(entry.path) for entry in units_read):
        command = json.loads((unit / "command.json").read_text())
        args = command.get("args") or []
        crate = next((b for a, b in zip(args, args[1:]) if a == "--crate-name"), None)
        compiled.setdefault((crate, command.get("source"), "--test" in args), []).append(unit)
    for message in messages:
        if message.get("reason") != "compiler-artifact":
            continue
        package = by_manifest.get(str(Path(str(message.get("manifest_path"))).resolve()))
        macro = "proc-macro" in ((message.get("target") or {}).get("kind") or ())
        if package is None:
            if macro:
                # A vendored procedural macro runs in an analysed crate's compile too.
                name = str((message.get("target") or {}).get("name"))
                filenames = [str(each) for each in message.get("filenames") or ()]
                depfile, producer = _dependency_file(filenames)
                if depfile is None:
                    raise ValueError(f"Cargo wrote no dependency file for the macro `{name}`")
                read = _read_dependencies(
                    depfile, shape.workspace, root, target_dir, name, producer
                )
                _check_bootstrap_mentions(read, root, target_dir, f"the procedural macro `{name}`")
            continue
        artifact = message.get("target") or {}
        profile = message.get("profile") or {}
        kinds = tuple(artifact.get("kind") or ())
        filenames = [str(name) for name in message.get("filenames") or ()]
        if (
            package.name == run_package.name
            and artifact.get("name") == run_target.name
            and kinds == run_target.kinds
            and message.get("executable")
        ):
            executable = message["executable"]
        depfile, producer = _dependency_file(filenames)
        if depfile is None:
            raise ValueError(
                f"Cargo wrote no dependency file for `{package.name}`'s {artifact.get('name')}"
            )
        read = _read_dependencies(
            depfile, shape.workspace, root, target_dir, package.name, producer
        )
        _record_read(read, root, target_dir, before, started_ns, sources, generated)
        directories.update(read["directories"])
        if macro:
            _check_bootstrap_mentions(
                read, root, target_dir, f"the procedural macro `{artifact.get('name')}`"
            )
            macros.add(package.name)
        if package.name not in analysed_names or {"custom-build", "proc-macro"} & set(kinds):
            continue
        crate = str(artifact.get("name", "")).replace("-", "_")
        key = (crate, str(Path(str(artifact.get("src_path"))).resolve()), bool(profile.get("test")))
        found = compiled.get(key, [])
        if len(found) > 1:
            stems = {_stem(Path(name).name) for name in filenames}
            found = [unit for unit in found if unit.name in stems]
        if message.get("fresh") or len(found) != 1:
            raise ValueError(
                f"Peeky's wrapper didn't compile `{package.name}`'s {artifact.get('name')}: "
                "Cargo wraps only the workspace's members. Add the package to the "
                "workspace's members, or analyse another"
            )
        unit = found[0]
        seen_units.add(unit.name)
        entry = _unit(package, artifact, unit, run)
        depfile = Path(entry.pop("dependency_file"))
        read = _read_dependencies(depfile, shape.workspace, root, target_dir, package.name)
        _check_bootstrap_mentions(read, root, target_dir, f"`{package.name}`")
        _check_bootstrap_read(package.name, crate, depfile)
        _record_read(read, root, target_dir, before, started_ns, sources, generated)
        entry["sources"] = sorted(read["sources"])
        # What it read that the build made (a build script's `OUT_DIR` file it
        # `include!`s), under the target directory: its code too.
        entry["generated"] = sorted(read["generated"])
        units.append(entry)
    if len(sources) + len(generated) > MAX_RECORDED:
        raise ValueError(f"The build read more than {MAX_RECORDED} files Peeky would record")
    if executable is None:
        raise ValueError(
            f"Cargo named no executable for {run_target.name} (`{run_package.name}`) in its "
            "messages"
        )
    if not units:
        raise ValueError(
            f"What runs ({run_target.name}) uses none of the analysed packages "
            f"({', '.join(sorted(analysed_names))}): analyse a package it uses"
        )
    if strays := {entry.name for entry in units_read} - seen_units:
        raise ValueError(
            f"Peeky's wrapper compiled {', '.join(sorted(strays))}, which Cargo's messages "
            "don't name"
        )
    copied = _copy_executable(Path(executable), run)
    # Only the bodies after rustc's last MIR pass are read; a body's before and after, and
    # each unit's few other files, within twice what is recorded.
    for entry in _walked(run / "units", [2 * MAX_RECORDED + 4096], _TOO_MANY_FILES):
        if entry.name.endswith(".PreCodegen.before.mir") and entry.is_file(follow_symlinks=False):
            os.unlink(entry.path)
    artifacts = _artifacts(directory, run, store)
    return {
        "root": str(root),
        "workspace": str(shape.workspace),
        "analysed": [package.name for package in shape.analysed],
        "local": [
            {"name": package.name, "directory": package.directory} for package in shape.local
        ],
        "run": {
            "package": run_package.name,
            "target": run_target.name,
            "kinds": list(run_target.kinds),
            "source": run_target.source,
            "executable": copied,
            "cargo_arguments": list(shape.cargo_arguments()),
        },
        "dependencies": len(shape.vendored),
        "crates": list(shape.crates),
        # The local packages that are procedural macros: what they write into a crate is
        # the project's code.
        "macros": sorted(macros),
        "units": units,
        "sources": dict(sorted(sources.items())),
        "generated": dict(sorted(generated.items())),
        # Checked against what build scripts watch (``_watched_directories``), not kept.
        "directories_read": sorted(directories),
        "artifacts": artifacts,
    }


_RERUN = re.compile(r"cargo::?(rerun-if-changed|rerun-if-env-changed)=(.*)")
# The wrapper's own variables, which name one build's files: no build script's input.
_PER_BUILD = ("PEEKY_UNITS", "PEEKY_ANALYSED")
# The most of a build script's output Peeky reads.
MAX_BUILD_OUTPUT = 16 * 1024 * 1024
# Variables Cargo sets or may set for a build script, besides those _script_value
# computes: refused. Any `CARGO*` but those it computes and _CARGO_INHERITED is one.
_CARGO_SETS = re.compile(
    r"(?:_*CARGO.*|DEP_.*|OUT_DIR|RUSTDOC|RUSTC_WRAPPER|RUSTC_WORKSPACE_WRAPPER|RUSTC_LINKER|"
    r"LD_LIBRARY_PATH|DYLD_LIBRARY_PATH|DYLD_FALLBACK_LIBRARY_PATH)"
)
# Cargo's own environment's variables it passes on to a build script unchanged, as
# rustc 1.96's Cargo was seen to: those Peeky sets (its home, target directory, profiles);
# and two it sets for a crate's compile, never for a build script.
_CARGO_INHERITED = re.compile(
    r"CARGO_(?:HOME|TARGET_DIR|INCREMENTAL|NET_OFFLINE|TERM_COLOR|PROFILE_[A-Z0-9_]+|"
    r"PRIMARY_PACKAGE|CRATE_NAME)"
)
# Cargo's `CARGO_PKG_*` of a manifest's `[package]` fields (as written, else empty).
_PACKAGE_FIELDS = {
    "AUTHORS": "authors",
    "DESCRIPTION": "description",
    "HOMEPAGE": "homepage",
    "REPOSITORY": "repository",
    "LICENSE": "license",
    "LICENSE_FILE": "license-file",
    "RUST_VERSION": "rust-version",
}
_SEMVER = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")


def _envify(name):
    return name.upper().replace("-", "_")


def jobs():
    """The jobs Peeky's build runs (`cargo build -j`): Cargo's `NUM_JOBS` for a build
    script."""
    try:
        return len(os.sched_getaffinity(0)) or 1
    except AttributeError:
        return os.cpu_count() or 1


def _spend(budget, where):
    """Spend one of ``budget``'s entries (a list of one count) on what ``where`` holds;
    past it, refused. A stopped build ends here first (agent.check_stop): every entry a
    walk records is a step it checks its stop at."""
    check_stop()
    _spend_one(budget, where)


def _spend_one(budget, where):
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError(_too_many(where))


def _reachable(path, root):
    """Refuse ``path`` (under the resolved ``root``) where a link on it leads out of
    ``root``, or its links loop: a build script or procedural macro may read every file of
    its package, and Peeky builds only files inside the root."""
    from peeky.sources import LinkLoop, OutsideRoot, _inside

    root = Path(os.path.realpath(root))
    try:
        _inside(root, Path(path).relative_to(root).as_posix(), "the worker host")
    except OutsideRoot as exc:
        raise ValueError(
            f"Peeky builds only files inside {root}: {_shown(path, root)} leads out of it "
            f"through {exc.link}, a link to {exc.target}, and a build script or procedural "
            "macro may read every file of its package. Point the link inside the workspace, or "
            "make it a file there"
        ) from None
    except LinkLoop as exc:
        raise ValueError(
            f"Peeky builds only files it can reach: {_shown(path, root)} {exc.what}, and a "
            "build script or procedural macro may read every file of its package. Fix the "
            "link, or make it a file there"
        ) from None


def _tree(top, skip=(), *, budget):
    """{path relative to ``top``: what is there} of every entry of the package directory
    ``top``, by lstat, never through a link: a file's SHA-256; ["dir"] for a directory, an
    empty one too; ["link", its text] for a link; ["other"] for what is neither. Hidden
    directories and `target` ones too. Each entry spends ``budget`` as it is read, before
    any is sorted or walked into. Left out:
    `.git` (the checkout's metadata), OS junk (``OS_JUNK``), and ``skip``: each other
    workspace package's directory (compared as its own), the vendored copies (compared by
    their checksums) and Peeky's own build directory. ``budget``: a list holding the entries
    left to record, spent here; past it, refused. Where every entry is what a build may read,
    a link whose target leaves ``root``, or whose links loop, is refused (``_reachable``)."""
    top, found = Path(top), {}
    pending = [top]
    while pending:
        directory = pending.pop()
        entries = []
        try:
            with os.scandir(directory) as listing:
                for entry in listing:
                    if entry.name == ".git" or entry.name in OS_JUNK or entry.path in skip:
                        continue
                    # Spent as read: a directory of more entries than the budget is refused
                    # before they are all held.
                    _spend(budget, top)
                    entries.append(entry)
        except OSError as exc:
            raise ValueError(
                f"Peeky builds only files it can reach: it can't read {directory} "
                f"({exc.strerror or exc}), and a build script or procedural macro may read every "
                "file of its package. Make it readable"
            ) from None
        for entry in sorted(entries, key=lambda each: each.name):
            path = Path(entry.path)
            relative = path.relative_to(top).as_posix()
            if entry.is_dir(follow_symlinks=False):
                found[relative] = ["dir"]
                pending.append(path)
                continue
            if entry.is_symlink():
                found[relative] = ["link", os.readlink(path)]
            elif entry.is_file(follow_symlinks=False):
                found[relative] = _hash(path)
            else:
                found[relative] = ["other"]
    return dict(sorted(found.items()))


def _root_identity(path):
    """A watched directory's own identity, not followed: ["dir"], ["link", what it
    names], ["missing"], ["unreadable"] or ["other"]."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return ["missing"]
    except OSError:
        return ["unreadable"]
    if stat.S_ISLNK(info.st_mode):
        return ["link", os.readlink(path)]
    return ["dir"] if stat.S_ISDIR(info.st_mode) else ["other"]


def _watched_directory(top, root, budget):
    """A `rerun-if-changed` directory as the manifest keeps it: {"root": its own identity
    (_root_identity), "entries": _exact_tree's, or "missing" or "unreadable" where it
    can't be walked}."""
    try:
        entries = _exact_tree(top, root, budget=budget)
    except FileNotFoundError:
        entries = "missing"
    except PermissionError:
        entries = "unreadable"
    return {"root": _root_identity(top), "entries": entries}


def _entry_changed_during(path, before, started_ns):
    """Whether ``path`` itself, not followed (a directory, a link, a file), changed while
    Cargo ran: its lstat differs from the snapshot's (one gone, too), or, with none there,
    it changed after the build started."""
    try:
        info = os.lstat(path)
        now = (info.st_mtime_ns, info.st_ctime_ns, info.st_size)
    except FileNotFoundError:
        now = None
    key = "l:" + str(path)
    if key in before:
        return tuple(before[key] or ()) != tuple(now or ())
    return now is not None and max(now[0], now[1]) >= started_ns - _SETTLE_NS


def _exact_tree(top, root, *, budget):
    """{path relative to ``top``: what is there} of everything under the directory
    ``top``, as Cargo scans a `rerun-if-changed` directory: hidden, `target` and other
    packages' directories too. A file's SHA-256; "dir" for a directory; for a link,
    ["link", what it names, its file's SHA-256, "dir" or None], a directory it names
    walked too. A link that leaves ``root`` is refused."""
    root, found, seen = Path(root).resolve(), {}, set()
    pending = [(Path(top), "")]
    while pending:
        directory, prefix = pending.pop()
        real = directory.resolve()
        if real in seen:
            continue
        seen.add(real)
        for entry in _listed(directory, budget, _too_many(top)):
            relative, path = prefix + entry.name, Path(entry.path)
            if entry.is_symlink():
                target = path.resolve()
                if not target.is_relative_to(root):
                    raise ValueError(
                        f"Peeky builds only files inside {root}: {path}, which a build script "
                        f"says it reads, is a link to {target}. Make it a file, or move what "
                        "it names into the workspace"
                    )
                what = "dir" if target.is_dir() else _hash(target) if target.is_file() else None
                found[relative] = ["link", os.readlink(path), what]
                if what == "dir":
                    pending.append((path, relative + "/"))
            elif entry.is_dir(follow_symlinks=False):
                found[relative] = "dir"
                pending.append((path, relative + "/"))
            elif entry.is_file(follow_symlinks=False):
                found[relative] = _hash(path)
            else:
                found[relative] = "other"
    return found


def _manifest_field(shape, directory, field):
    """A `[package]` field of the manifest in ``directory`` as Cargo gives it to a build
    script (a list's items joined by `:`; "" where absent; one inherited from the
    workspace's `[workspace.package]`), or Ellipsis where Peeky can't tell."""
    package = rust_workspace._read_toml(
        Path(directory) / "Cargo.toml", rust_workspace.MAX_MANIFEST_BYTES
    ).get("package")
    value = package.get(field, "") if isinstance(package, dict) else ...
    if isinstance(value, dict) and value == {"workspace": True}:
        root = rust_workspace._read_toml(
            shape.workspace / "Cargo.toml", rust_workspace.MAX_MANIFEST_BYTES
        )
        section = (root.get("workspace") or {}).get("package")
        value = section.get(field, ...) if isinstance(section, dict) else ...
    if isinstance(value, list) and all(isinstance(each, str) for each in value):
        return ":".join(value)
    return value if isinstance(value, str) else ...


def _cfg_value(key, cfgs):
    """`CARGO_CFG_<key>` as Cargo gives it from the build's `rustc --print cfg`: "" for a
    name, a key's values joined by `,`, None where none."""
    found = None
    for line in cfgs:
        name, _, value = line.partition("=")
        if _envify(name) != key:
            continue
        if not value:
            return ""
        found = [*(found or []), value.strip('"')]
    return None if found is None else ",".join(found)


def _script_value(name, package_id, shape, env, jobs_):
    """(the value Cargo gives a build script of ``package_id`` for the variable ``name``,
    how Peeky knows it: "cargo", "jobs" or "launch")
    in Peeky's build (``env``: what Peeky runs Cargo with; ``jobs_``: its `-j`): Cargo's
    own for its host, profile and jobs; the profile's for `OPT_LEVEL` and `DEBUG`, and
    the features for `CARGO_FEATURE_*` and `CARGO_CFG_FEATURE`, from the unit graph; the
    build's `rustc --print cfg` for `CARGO_CFG_*`; the package's manifest and place for
    `CARGO_PKG_*` and `CARGO_MANIFEST_*`; Peeky's own rustflags; else what Peeky gives
    Cargo, which Cargo passes on. Refused for one Cargo sets otherwise, whose value Peeky
    doesn't compute (`OUT_DIR`, `DEP_*`, `CARGO_MAKEFLAGS`, `CARGO_PKG_README`, which a
    file's presence decides, `CARGO_CFG_DEBUG_ASSERTIONS`)."""
    source, directory, package, version = rust_workspace.package_id(package_id)
    # "cargo": Cargo's, of the tools, the configuration and the manifests, which the
    # build's other records hold; "jobs": the build's `-j`; "launch": Peeky's own
    # environment's, which Cargo passes on.
    how = "cargo"
    profiles = [each for each in shape.build_profiles if each[0] == package_id]
    profile = profiles[0] if len(profiles) == 1 else None
    if source is None:
        place = Path(directory)
    elif (copy := shape.vendored.get((package, version))) is not None:
        place = shape.root / copy
    else:
        place = None
    found = ...
    if name in ("TARGET", "HOST"):
        found = shape.host
    elif name == "PROFILE":
        found = "debug"
    elif name == "NUM_JOBS":
        found, how = str(jobs_), "jobs"
    elif name in ("OPT_LEVEL", "DEBUG") and profile is not None:
        found = profile[1] if name == "OPT_LEVEL" else profile[2]
    elif name.startswith("CARGO_FEATURE_"):
        if profile is not None:
            features = {_envify(each) for each in profile[3]}
            found = "1" if name.removeprefix("CARGO_FEATURE_") in features else None
    elif name == "CARGO_CFG_FEATURE":
        if profile is not None:
            found = ",".join(profile[3])
    elif name.startswith("CARGO_CFG_") and name != "CARGO_CFG_DEBUG_ASSERTIONS":
        found = _cfg_value(name.removeprefix("CARGO_CFG_"), shape.cfgs)
    elif name == "CARGO_ENCODED_RUSTFLAGS":
        found = env.get(name)
    elif place is not None and name == "CARGO_MANIFEST_DIR":
        found = str(place)
    elif place is not None and name == "CARGO_MANIFEST_PATH":
        found = str(place / "Cargo.toml")
    elif name == "CARGO_PKG_NAME":
        found = package
    elif name.startswith("CARGO_PKG_VERSION"):
        parts = _SEMVER.fullmatch(version)
        if parts:
            found = {
                "CARGO_PKG_VERSION": version,
                "CARGO_PKG_VERSION_MAJOR": parts[1],
                "CARGO_PKG_VERSION_MINOR": parts[2],
                "CARGO_PKG_VERSION_PATCH": parts[3],
                "CARGO_PKG_VERSION_PRE": parts[4] or "",
            }.get(name, ...)
    elif place is not None and name.removeprefix("CARGO_PKG_") in _PACKAGE_FIELDS:
        found = _manifest_field(shape, place, _PACKAGE_FIELDS[name.removeprefix("CARGO_PKG_")])
    elif place is not None and name == "CARGO_MANIFEST_LINKS":
        found = _manifest_field(shape, place, "links")
        found = None if found == "" else found
    elif name == "RUSTC" or _CARGO_INHERITED.fullmatch(name) or not _CARGO_SETS.fullmatch(name):
        found, how = env.get(name), "launch"
    if found is ...:
        raise ValueError(
            f"Peeky can't tell the value Cargo gives `{package}`'s build script of {name}, "
            "which it says it reads (`rerun-if-env-changed`), so not whether a later build "
            "would differ"
        )
    return found, how


# Why a package's file can't be compared with a remote client's (`lists`' {"refused": key}):
# (what it is on the worker's host, what to do).
_UNLISTABLE = {
    "outside": ("a link that leads out of the root", "point the link inside the root"),
    "loop": ("a link in a loop", "break the loop"),
    "hops": ("behind more than 40 links", "shorten the chain of links"),
    "dangling": ("a link to nothing", "point the link at a file, or remove it"),
    "other": ("not a file", "make it a file, or remove it"),
    "missing": ("missing, though Cargo lists it", "restore it, or stop git tracking it"),
}
# What `cargo package --list` names that it writes into a package, not the package's own.
_SYNTHESIZED = frozenset({"Cargo.toml.orig", ".cargo_vcs_info.json"})


def _compared_file(directory, name, root, walked):
    """What a client's ``name`` (a package's, in ``directory``) is compared with: its file's
    SHA-256 (``walked``'s where the walk took it), "dir" for a directory (an empty one the
    walk names) or a link to one, or {"refused": a key of ``_UNLISTABLE``}. Looked at by lstat first; a link is followed only
    hop by hop inside ``root`` (sources._inside), never out of it."""
    from peeky.sources import LinkLoop, OutsideRoot, _inside

    check_stop()
    root = Path(os.path.realpath(root))
    path = Path(directory) / name
    if not os.path.lexists(path):
        return {"refused": "missing"}
    try:
        chain, reached = _inside(root, path.relative_to(root).as_posix(), "the worker host")
    except OutsideRoot:
        return {"refused": "outside"}
    except LinkLoop as exc:
        return {"refused": "loop" if exc.loop else "hops"}
    try:
        target = os.lstat(reached)
    except FileNotFoundError:
        return {"refused": "dangling" if chain else "missing"}
    if stat.S_ISREG(target.st_mode):
        if not chain and isinstance(walked.get(name), str):
            return walked[name]
        return _hash(reached)
    if stat.S_ISDIR(target.st_mode):
        return "dir"
    return {"refused": "other"}


def _package_files(directory, names, walked, root, workspace):
    """{path relative to ``directory``: ``_compared_file``} of a package's files as Cargo
    lists them (``names``, `cargo package --list`'s lines), none under `.git`. One absent
    is left out only where Cargo writes it into a package: `Cargo.toml.orig`,
    `.cargo_vcs_info.json`, and `Cargo.lock` but in the ``workspace``'s own directory.
    ``walked``: the hashes the package's walk already took."""
    found = {}
    for name in names:
        # Exactly as Cargo writes it: a name may begin or end with a space.
        if name == "":
            continue
        if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
            raise ValueError(f"`cargo package --list` named {name}, a path Peeky doesn't read")
        if _checkout_metadata(name):
            continue
        synthesized = name in _SYNTHESIZED or (
            name == "Cargo.lock" and Path(directory).resolve() != Path(workspace).resolve()
        )
        if synthesized and not os.path.lexists(Path(directory) / name):
            continue
        found[name] = _compared_file(directory, name, root, walked)
    return found


# The refusals of a build that wrote more than Peeky records.
_TOO_MANY_FILES = f"The build wrote more than {MAX_RECORDED} files Peeky records"
_TOO_MANY_UNITS = f"Peeky's wrapper recorded more than {MAX_RECORDED} compiles"


def _too_many(where):
    """``_spend``'s refusal, for what ``where`` holds."""
    return (
        f"Peeky records at most {MAX_RECORDED} files a build script or procedural macro may "
        f"read, and {where} holds more. Build fewer packages' files, or give the build script "
        "`cargo::rerun-if-changed` lines"
    )


# Files an operating system's file manager leaves in any directory, which no build reads:
# never compared where Peeky lists a package's files itself.
OS_JUNK = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


def _build_inputs(
    shape, root, messages, env, before, started_ns, vendor, jobs_=1, lister=None, private=None
):
    """What build scripts and procedural macros may read, beyond what rustc's dependency
    files list, as the manifest keeps it: {"files": {path: identity}, "dirs": {directory:
    _exact_tree}, "trees": {directory: _tree}, "env": {variable: value or None}}. That
    is each local build script's `rerun-if-changed` paths (a directory, everything under
    it), or with none, Cargo's default, every file of its package; every build
    script's `rerun-if-env-changed` variables (dependencies' too), as Cargo gives them
    (_script_value); and with a local procedural macro, every file of its package and of
    each analysed one (a macro reading files elsewhere isn't followed). Refused where one
    changed while Cargo ran, or where Peeky can't record it.

    A package compared whole is walked (``trees``: every file, for the build's freshness), and
    its files a client's are compared with are listed (``lists``, ``_package_files``): as
    Cargo lists them (``lister(package)``: its `cargo package --list`) for a build script's
    package, every walked entry for a procedural macro's and the analysed ones, and where
    Cargo can't list it (``_tree``, which ``walk_skip`` records what it leaves out of).
    ``private``: Peeky's build directory, never walked."""
    by_directory = {str((root / package.directory).resolve()): package for package in shape.local}
    skip = {*by_directory, *([shape.watch["vendor_directory"]] if vendor else [])}
    skip |= {str(Path(private).resolve())} if private is not None else set()
    files, dirs, trees, variables, budget = {}, {}, {}, {}, [MAX_RECORDED]
    lists = {}

    def unchanged(path):
        # The entry itself (a directory, a link, a file), and a file's contents; never
        # through a link.
        check_stop()
        if _entry_changed_during(path, before, started_ns) or (
            Path(path).is_file()
            and not Path(path).is_symlink()
            and _changed_during(path, before, started_ns)
        ):
            raise RuntimeError(
                f"Provenance: {_shown(path, root)} changed while Peeky built the "
                "workspace, so the build may not be of it. Build again"
            )

    def watch_tree(directory, package=None):
        """Walk ``directory`` (a package's), and list its files a client's are compared
        with: as Cargo lists them, given ``package`` (`name@version`); else every walked
        file but OS junk."""
        key = str(directory)
        if key not in trees:
            trees[key] = _tree(directory, skip - {key}, budget=budget)
            for name in trees[key]:
                unchanged(directory / name)
        entry = lists.setdefault(key, {"files": {}, "cargo": False, "note": None})
        names, why = (None, None)
        if package is not None:
            names, why = lister(package) if lister else (None, "Peeky didn't ask Cargo")
        if names is None:
            if package is not None:
                # Said in the build's summary (``summary``'s `notes`).
                entry["note"] = (
                    f"{why}: Peeky compares every file of {_shown(directory, root)} with a remote "
                    "client's instead, but .DS_Store, Thumbs.db and desktop.ini"
                )
            # Each directory that holds an entry, in one pass: compared by its entries.
            holding = {name.rpartition("/")[0] for name in trees[key]}
            for name, what in trees[key].items():
                if what == ["dir"] and name in holding:
                    continue
                if isinstance(what, list) and what[0] == "link":
                    _reachable(directory / name, root)
                entry["files"][name] = _compared_file(directory, name, root, trees[key])
            return
        entry["cargo"] = True
        listed = _package_files(directory, names, trees[key], root, shape.workspace)
        for name, digest in listed.items():
            if isinstance(digest, str) and digest != "dir" and name not in trees[key]:
                _spend(budget, directory)
                unchanged(directory / name)
            entry["files"][name] = digest

    for message in messages:
        if message.get("reason") != "build-script-executed":
            continue
        found = rust_workspace.package_id(message.get("package_id"))
        out_dir = message.get("out_dir")
        if found is None or not isinstance(out_dir, str):
            raise ValueError("Cargo named a build script's run in a form Peeky doesn't read")
        source, directory, name, _ = found
        output = Path(out_dir).parent / "output"
        try:
            if output.stat().st_size > MAX_BUILD_OUTPUT:
                raise ValueError(
                    f"Peeky reads at most {MAX_BUILD_OUTPUT // 1024 // 1024} MiB of a build "
                    f"script's output, and `{name}`'s is more: Peeky can't tell all it says "
                    "it reads. Make it print less"
                )
            text = output.read_bytes().decode("utf-8", "replace")
        except OSError:
            raise ValueError(f"Cargo kept no output of `{name}`'s build script") from None
        changed = []
        for line in split_lines(text):
            if (rerun := _RERUN.fullmatch(line.strip())) is None:
                continue
            if rerun[1] == "rerun-if-env-changed":
                value, how = _script_value(rerun[2], message["package_id"], shape, env, jobs_)
                variables[message["package_id"], rerun[2]] = {"value": value, "from": how}
            elif source is None:
                changed.append(rerun[2])
        if source is not None:
            continue
        package_directory = Path(directory).resolve()
        if not changed:
            # Cargo's default: any file of the package, as Cargo lists it.
            watch_tree(package_directory, f"{name}@{found[3]}")
        for each in changed:
            # Its directory as found; its last segment not followed (a link is recorded).
            joined = package_directory / each
            if joined.name in ("", ".", ".."):
                path = Path(os.path.realpath(joined))
            else:
                path = Path(os.path.realpath(joined.parent)) / joined.name
            if (dangling := _dangling_on_the_way(joined, before)) is not None:
                link, target, when = dangling
                raise ValueError(
                    f"`{name}`'s build script watches {_shown(joined, root)}"
                    + ("" if link == joined else f", by way of {_shown(link, root)}")
                    + f", a link to {_shown(target, root)} that {when}; Peeky can't tell what it "
                    "read through it. Point the link at what exists, or take the line out of "
                    "the build script"
                )
            if path.is_dir():
                if str(path) not in dirs:
                    if path.is_symlink() and not path.resolve().is_relative_to(root):
                        raise ValueError(
                            f"Peeky builds only files inside {root}: {path}, which a build "
                            f"script says it reads, is a link to {path.resolve()}. Make it a "
                            "directory, or move what it names into the workspace"
                        )
                    dirs[str(path)] = _watched_directory(path, root, budget)
                    unchanged(path)
                    entries = dirs[str(path)]["entries"]
                    for entry in entries if isinstance(entries, dict) else ():
                        unchanged(path / entry)
                        if (dangling := _dangling_on_the_way(path / entry, before)) is not None:
                            link, target, when = dangling
                            raise ValueError(
                                f"`{name}`'s build script watches {_shown(path, root)}, which "
                                f"holds {_shown(link, root)}, a link to {_shown(target, root)} "
                                f"that {when}; Peeky can't tell what it read through it. Point "
                                "the link at what exists, or take the line out of the build "
                                "script"
                            )
            elif str(path) not in files:
                _spend(budget, path)
                unchanged(path)
                files[str(path)] = rust_workspace.identity(path)
                missing = files[str(path)] == ["missing"]
                unproven = missing and _absent_before(path, before)
                if missing and not unproven and (moved := _changed_on_the_way(path, before)):
                    # It may have been made, read and removed while Cargo ran.
                    raise RuntimeError(
                        f"Provenance: {_shown(moved, root)} changed while Peeky built the "
                        f"workspace, and `{name}`'s build script says it reads "
                        f"{_shown(path, root)}, missing before and after: the script may "
                        "have read it meanwhile. Build again"
                    )
                if unproven:
                    # Not there now, and nothing says it wasn't there while the build
                    # script ran (deleted since, where Peeky recorded nothing before).
                    kind, why = unproven
                    raise ValueError(
                        f"Peeky can't tell whether {_shown(path, root)} was there while "
                        f"`{name}`'s build script ran: the script says it reads it, it is "
                        f"missing now, and {_shown(why, root)}. {_ABSENCE_REMEDY[kind]}"
                    )
    macros = {
        str(Path(str(message.get("manifest_path"))).resolve().parent)
        for message in messages
        if message.get("reason") == "compiler-artifact"
        and "proc-macro" in ((message.get("target") or {}).get("kind") or ())
    }
    if macros := sorted(each for each in macros if each in by_directory):
        for directory in macros:
            watch_tree(Path(directory))
        for package in shape.analysed:
            watch_tree((root / package.directory).resolve())
    return {
        "files": dict(sorted(files.items())),
        "dirs": dict(sorted(dirs.items())),
        "trees": dict(sorted(trees.items())),
        # What each package's walk leaves out (``_tree``'s ``skip``), as `verify` walks again.
        "walk_skip": sorted(skip),
        "lists": {
            key: {**entry, "files": dict(sorted(entry["files"].items()))}
            for key, entry in sorted(lists.items())
        },
        "env": [
            {"package": package_id, "name": variable, **record}
            for (package_id, variable), record in sorted(variables.items())
        ],
    }


def _record_read(read, root, target_dir, before, started_ns, sources, generated):
    """Hash what a compile read: sources (refused where one changed while Cargo ran) and
    files a build script generated."""
    for path in read["sources"]:
        if path not in sources:
            if _changed_during(root / path, before, started_ns):
                raise RuntimeError(
                    f"Provenance: {path} changed while Peeky built the workspace, so the build "
                    "may not be of it. Build again"
                )
            sources[path] = _hash(root / path)
    for path in read["generated"]:
        if path not in generated:
            generated[path] = _hash(target_dir / path)


def _watched_directories(directories, manifest, shape, vendor):
    """Refuse a directory Cargo says the build read (its own dependency file lists build
    scripts' `rerun-if-changed` paths) whose entries Peeky didn't record: one no local build
    script watches (``_build_inputs``), outside the vendored copies, which their checksums
    cover."""
    root = Path(manifest["root"])
    watched = {Path(each).resolve() for each in manifest["build_inputs"]["dirs"]}
    copies = Path(shape.watch["vendor_directory"]).resolve() if vendor else None
    for each in directories:
        if each in watched or (copies is not None and each.is_relative_to(copies)):
            continue
        raise ValueError(
            f"Cargo says the build read the directory {_shown(each, root)}, which no local "
            "build script's `rerun-if-changed` names: Peeky can't tell what of it was read. "
            "Name it in the build script that reads it"
        )


def _read_dependencies(depfile, workspace, root, target_dir, package, producer="rustc"):
    try:
        paths = dependency_paths(depfile.read_text(errors="surrogateescape"), workspace, producer)
    except UnreadDependencies as exc:
        raise ValueError(f"{exc} ({depfile.name}, of `{package}`)") from None
    found = {"sources": [], "generated": [], "directories": []}
    for path in paths:
        resolved = path.resolve()
        if resolved.is_relative_to(target_dir):
            found["generated"].append(resolved.relative_to(target_dir).as_posix())
        elif producer == "make" and resolved.is_relative_to(root) and resolved.is_dir():
            # A build script's `rerun-if-changed` directory, which Cargo lists too: what it
            # holds is recorded entry by entry (``_build_inputs``), not hashed as a file.
            found["directories"].append(resolved)
        elif resolved.is_relative_to(root):
            found["sources"].append(resolved.relative_to(root).as_posix())
        elif producer == "make":
            # Cargo's own dependency file lists its build scripts' `rerun-if-changed` paths too.
            raise ValueError(
                f"Peeky builds only files inside {root}: `{package}`'s build read "
                f"{os.path.normpath(path)}, which no remote client could compare either. Move it "
                "into the workspace, or drop what names it: an `include!`, a `#[path]`, or a "
                "build script's `rerun-if-changed`"
            )
        else:
            raise ValueError(
                f"Peeky builds only files inside {root}: `{package}`'s build read {path}. Move it "
                "into the workspace, or drop the `include!` or `#[path]` that names it"
            )
    return found


def _unit(package, artifact, unit, run):
    """An analysed compile, as its manifest entry: the checked flags and the wrapper's
    files (MIR, expanded text, LLVM IR), relative to the build's run, and its own
    dependency file (``dependency_file``, for the caller)."""
    command = json.loads((unit / "command.json").read_text())
    args = command.get("args") or []
    crate = artifact.get("name", "").replace("-", "_")
    level = check_codegen(package.name, crate, args)
    out_dir = next(
        (b for a, b in zip(args, args[1:]) if a == "--out-dir"),
        next((a.split("=", 1)[1] for a in args if a.startswith("--out-dir=")), None),
    )
    ir = Path(out_dir or "") / f"{unit.name}.ll"
    if not out_dir or not ir.is_file():
        raise ValueError(f"rustc wrote no LLVM IR for `{package.name}`'s {crate}")
    shutil.copy2(ir, unit / "ir.ll")
    expanded = json.loads((unit / "expand.json").read_text()).get("exit_code") == 0
    return {
        "package": package.name,
        "crate": crate,
        "target": artifact.get("name"),
        "kinds": list(artifact.get("kind") or ()),
        "test": "--test" in args,
        "source": command.get("source"),
        "unit": unit.relative_to(run.parent.parent).as_posix(),
        "opt_level": level,
        "expanded": expanded,
        "dependency_file": str(Path(out_dir) / f"{unit.name}.d"),
    }


def _check_bootstrap_mentions(read, root, target_dir, whose):
    """Refuse where a Rust source a compile read (``read``: _read_dependencies's, its
    sources relative to ``root`` and the files a build script generated relative to
    ``target_dir``) mentions RUSTC_BOOTSTRAP: Peeky sets it for an analysed crate's own
    compile, which the crate's code and the procedural macros running there (``whose``)
    could then tell from a stable one. Otherwise they are taken not to change on it, as
    dependencies are taken as sound."""
    files = [(path, Path(root) / path) for path in read["sources"]]
    files += [(f"{path} (generated)", Path(target_dir) / path) for path in read["generated"]]
    for path, where in files:
        check_stop()
        if where.name.endswith(".rs") and b"RUSTC_BOOTSTRAP" in where.read_bytes():
            raise ValueError(
                f"Peeky analyses only code built as stable Rust builds it: {whose}'s {path} "
                "mentions RUSTC_BOOTSTRAP, which Peeky's build sets for an analysed crate's "
                "own compile, where procedural macros run too. Take the mention out, or "
                "analyse another package"
            )


def _check_bootstrap_read(package, crate, depfile):
    """Refuse an analysed compile that read RUSTC_BOOTSTRAP, as rustc's dependency file
    records (`# env-dep:`: `env!`, `option_env!`, a macro's tracked read): Peeky sets it
    for that compile alone, which would then build otherwise than stable Rust does."""
    for line in split_lines(depfile.read_text(errors="surrogateescape")):
        if line == "# env-dep:RUSTC_BOOTSTRAP" or line.startswith("# env-dep:RUSTC_BOOTSTRAP="):
            raise ValueError(
                f"Peeky analyses only code built as stable Rust builds it: `{package}`'s "
                f"{crate} reads RUSTC_BOOTSTRAP (`env!`, `option_env!` or a macro), which "
                "Peeky's build sets for that crate's compile alone. Read it elsewhere, or "
                "analyse another package"
            )


def _copy_executable(executable, run):
    """The run's executable (and its macOS symbols), copied out of Cargo's target
    directory: a later build replaces Cargo's, never this one."""
    folder = run / "bin"
    folder.mkdir(mode=0o700)
    copy = folder / executable.name
    shutil.copy2(executable, copy)
    symbols = executable.with_name(executable.name + ".dSYM")
    if sys.platform == "darwin":
        if not symbols.is_dir():
            raise ValueError(f"Cargo wrote no debug symbols for {executable.name}")
        _copy_symbols(symbols, folder / symbols.name)
    return copy.relative_to(run.parent.parent).as_posix()


# The most entries Peeky copies of a macOS debug symbols bundle (a handful in dsymutil's).
MAX_SYMBOL_ENTRIES = 4096


def _copy_symbols(symbols, destination):
    """Copy the macOS debug symbols bundle ``symbols`` to ``destination``, read lazily
    (``_walked``): at most ``MAX_SYMBOL_ENTRIES`` entries, the build's stop checked at each.
    A link, or what is neither a file nor a directory, is refused."""
    destination.mkdir(mode=0o700)
    refusal = f"Cargo's {symbols.name} holds more than {MAX_SYMBOL_ENTRIES} entries"
    for entry in _walked(symbols, [MAX_SYMBOL_ENTRIES], refusal):
        path = Path(entry.path)
        target = destination / path.relative_to(symbols)
        if entry.is_symlink():
            raise ValueError(f"Peeky's build holds a link: {path}")
        if entry.is_dir(follow_symlinks=False):
            target.mkdir(mode=0o700)
        elif entry.is_file(follow_symlinks=False):
            shutil.copy2(path, target)
        else:
            raise ValueError(f"Cargo's {symbols.name} holds {path}, which is not a file")


def _artifacts(directory, run, store):
    """{path relative to ``directory``: SHA-256} of every file of the build's ``run``, read
    lazily (``_walked``): at most MAX_RECORDED entries, the build's stop checked at each."""
    found = {}
    for entry in _walked(run, [MAX_RECORDED], _TOO_MANY_FILES):
        path = Path(entry.path)
        if entry.is_symlink():
            raise ValueError(f"Peeky's build holds a link: {path}")
        if entry.is_file(follow_symlinks=False):
            found[path.relative_to(directory).as_posix()] = _hash(path, store=store)
    return dict(sorted(found.items()))


def _build_id(manifest):
    kept = {
        key: value
        for key, value in manifest.items()
        if key not in ("nonce", "seconds", "built_at", "artifacts", "units", "run")
    }
    return hashlib.sha256(
        json.dumps(kept, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _write_manifest(directory, manifest):
    path = Path(directory) / "manifest.json"
    temporary = path.with_name(f".manifest-{secrets.token_hex(4)}.json")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as out:
        json.dump(manifest, out, sort_keys=True)
    os.replace(temporary, path)


class Lease:
    """A probe's or session's hold on one build (``manifest``): _prune keeps that build's
    files while it is held. Release it with close()."""

    def __init__(self, manifest, descriptor):
        self.manifest = manifest
        self._descriptor = descriptor

    def close(self):
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _lease(directory, nonce):
    """A shared lock on ``nonce``'s lease file, taken while the build directory's own lock
    is held (so no build prunes it meanwhile): its descriptor."""
    folder = Path(directory) / "leases"
    folder.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(folder / nonce, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _held(lease):
    """Whether a probe or session holds ``lease`` (a lease file); one nobody holds goes."""
    try:
        descriptor = os.open(lease, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        lease.unlink()
        return False
    finally:
        os.close(descriptor)


def _prune(directory, nonce):
    """Keep the newest KEEP_RUNS builds' files, and any a probe or session holds (Lease).
    Called with the build directory's lock held alone."""
    runs, leases = Path(directory) / "runs", Path(directory) / "leases"
    # Peeky's own directories, read lazily within a bound: its runs and leases. The build
    # is recorded by now, so it is never stopped here.
    listing = _listed(
        runs, [MAX_RECORDED], f"{runs} holds more than {MAX_RECORDED} runs", stoppable=False
    )
    kept = sorted(
        (Path(entry.path) for entry in listing if entry.is_dir(follow_symlinks=False)),
        key=lambda path: (path.name == nonce, path.stat().st_mtime_ns),
        reverse=True,
    )
    for path in kept[KEEP_RUNS:]:
        if not _held(leases / path.name):
            _discard(directory, path)
    if leases.is_dir():
        refusal = f"{leases} holds more than {MAX_RECORDED} leases"
        listing = _listed(leases, [MAX_RECORDED], refusal, stoppable=False)
        for lease in (Path(entry.path) for entry in listing):
            if not (runs / lease.name).is_dir():
                _held(lease)
    try:
        _empty_trash(directory, MAX_RECORDED)
    except Stopped:
        # The build is recorded: a stop ends only the cleaning, which a later build resumes.
        pass


def _discard(directory, path):
    """Move ``path`` (a run, in the build ``directory``) into its trash: one rename, nothing
    under it read. A later build empties the trash within a bound (``_empty_trash``). Where
    the rename fails, ``path`` stays where it is, for a later ``_prune``."""
    trash = Path(directory) / "trash"
    try:
        trash.mkdir(mode=0o700, exist_ok=True)
        os.rename(path, trash / f"{Path(path).name}-{secrets.token_hex(4)}")
    except OSError:
        pass


def _empty_trash(directory, budget):
    """Remove what the build ``directory``'s trash holds, ``budget`` removals this time (a
    later build goes on). Depth first, holding only paths: a directory is read until its
    first subdirectory, its files removed as they are read, and its listing closed before
    that subdirectory is entered, so one directory is open at a time, however deep the
    trash. Once a subdirectory is gone, its parent is read again, without what was removed.
    A directory whose listing ends is removed. Only a removal that succeeds spends the
    budget. One that fails, or a directory that can't be read, is recorded and passed over
    for the rest of this call. The build's stop is checked at each entry (``check_stop``)."""
    trash = Path(directory) / "trash"
    if not trash.is_dir():
        return
    left, stack, failed = budget, [trash], set()
    while stack and left > 0:
        folder, deeper = stack[-1], None
        try:
            with os.scandir(folder) as listing:
                for entry in listing:
                    check_stop()
                    if entry.path in failed:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        deeper = Path(entry.path)
                        break
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        failed.add(entry.path)
                        continue
                    left -= 1
                    if left <= 0:
                        return
        except OSError:
            # Unreadable: passed over for the rest of this call.
            failed.add(str(folder))
            stack.pop()
            if not stack or folder == trash:
                return
            continue
        if deeper is not None:
            stack.append(deeper)
            continue
        stack.pop()
        if folder == trash:
            return
        try:
            os.rmdir(folder)
        except OSError:
            failed.add(str(folder))
            continue
        left -= 1


def summary(manifest, directory):
    """What `peeky build` says of a build."""
    return {
        "peeky": "build",
        "status": "built",
        "workspace": manifest["workspace"],
        "analysed": manifest["analysed"],
        "run": {
            "package": manifest["run"]["package"],
            "target": manifest["run"]["target"],
            "kinds": manifest["run"]["kinds"],
        },
        "executable": str(Path(directory) / manifest["run"]["executable"]),
        "build_id": manifest["build_id"],
        "seconds": manifest["seconds"],
        "dependencies": manifest["dependencies"],
        "sources": len(manifest["sources"]),
        "units": [
            {key: unit[key] for key in ("package", "crate", "test", "expanded")}
            for unit in manifest["units"]
        ],
        # Why a package isn't compared with a remote client's as Cargo lists it.
        "notes": list(
            dict.fromkeys(
                listed["note"]
                for listed in ((manifest.get("build_inputs") or {}).get("lists") or {}).values()
                if listed.get("note")
            )
        ),
    }


def _required(target, why):
    return BuildRequired(
        f"No current Peeky build of this workspace: {why}. Run `{target.build_command}` "
        "first, or add --build"
    )


def _edited(target, what):
    """A source or manifest edited since the build: the normal edit cycle, BUILD_REQUIRED."""
    return BuildRequired(
        f"No current Peeky build of this workspace: {what} since `peeky build` ran. Run "
        f"`{target.build_command}` again, or add --build"
    )


def _stale(target, what):
    """What identifies the build changed since it ran: Cargo's configuration, the lockfile,
    a vendored copy, a tool, or the build's own output (PROVENANCE_CHANGED)."""
    return RuntimeError(
        f"Provenance: {what} since `peeky build` ran, so its build may not be of these files. "
        f"Run `{target.build_command}` again, or add --build"
    )


def current_build(target, root):
    """The manifest of ``target``'s build under ``root`` while it holds. BuildRequired
    where there is none, or where a source or manifest changed since it ran (the normal
    edit cycle); a provenance error where what identifies it changed (Cargo's
    configuration, the lockfile, a vendored copy, a tool, the build's own output). Each
    names the file."""
    return _current(target, root, hold=False)[0]


def lease_build(target, root):
    """current_build's, held (Lease) until the probe or session using it ends: no later
    build removes its files meanwhile."""
    manifest, descriptor = _current(target, root, hold=True)
    return Lease(manifest, descriptor)


def _current(target, root, *, hold):
    """(current_build's manifest, with ``hold`` a lease's descriptor, else None)."""
    root = Path(root).resolve(strict=True)
    directory = build_directory(target, root, create=False)
    path = directory / "manifest.json"
    if not path.is_file():
        raise _required(target, "none has been made for this configuration")
    with _locked(directory, exclusive=False):
        try:
            manifest = json.loads(path.read_text())
        except (OSError, ValueError):
            raise _required(target, "its record can't be read") from None
        if manifest.get("schema") != SCHEMA or manifest.get("identity") != target.identity(root):
            raise _required(target, "the one recorded is for another configuration or Peeky")
        verify(target, root, directory, manifest)
        descriptor = _lease(directory, manifest["nonce"]) if hold else None
    manifest["directory"] = str(directory)
    return manifest, descriptor


def verify(target, root, directory, manifest):
    """Refuse a build whose record (``manifest``, in ``directory``) no longer holds:
    BuildRequired where a source, a manifest or a build script's input changed since it
    ran (the normal edit cycle); a provenance error where what identifies it changed
    (Cargo's configuration, the lockfile, a vendored copy, a tool, the build's own output).
    Each names the file."""
    for tool in manifest["tools"]:
        try:
            same = sha256_file(tool["path"]) == tool["sha256"]
        except OSError:
            same = False
        if not same:
            raise _stale(target, f"the tool {tool['path']} changed")
    from peeky.build import _digest_store

    store = _digest_store()
    digest = _digest(store)
    # Edits first (sources, manifests, a new member: BUILD_REQUIRED); then what
    # identifies the build (PROVENANCE_CHANGED).
    for name, recorded in manifest["sources"].items():
        try:
            same = sha256_file(root / name) == recorded
        except OSError:
            same = False
        if not same:
            raise _edited(target, f"{name} changed")
    state = manifest["watch"]
    for path, recorded in state["files"].items():
        if (
            Path(path).name == "Cargo.toml"
            and path not in state.get("absent", ())
            and rust_workspace.identity(path, digest) != recorded
        ):
            raise _edited(target, f"{_shown(path, root)} changed")
    members = rust_workspace.members_now(state)
    if members != state["candidates"]:
        added = sorted(set(members) ^ set(state["candidates"]))
        raise _edited(target, f"the workspace's members changed ({_shown(added[0], root)})")
    _check_build_inputs(target, root, directory, manifest)
    if (what := rust_workspace.recheck(state, digest)) is not None:
        raise _stale(target, _shown(what, root))
    target_dir = directory / "target"
    for name, digest in manifest["generated"].items():
        try:
            same = sha256_file(target_dir / name) == digest
        except OSError:
            same = False
        if not same:
            raise _stale(target, f"the build's generated file {name} changed")
    for name, digest in manifest["artifacts"].items():
        try:
            same = sha256_file(directory / name, store=store) == digest
        except OSError:
            same = False
        if not same:
            raise _stale(target, f"the build's output {name} changed")


def _check_build_inputs(target, root, directory, manifest):
    """BuildRequired where what a build script or procedural macro may read changed since
    the build (_build_inputs): a file, a directory's contents, a variable as Cargo
    would give it now."""
    inputs = manifest["build_inputs"]
    for path, recorded in inputs["files"].items():
        if rust_workspace.identity(path) != recorded:
            raise _edited(target, f"{_shown(path, root)}, which a build script reads, changed")
    for top, recorded in inputs["dirs"].items():
        try:
            now = _watched_directory(Path(top), root, [MAX_RECORDED])
        except (OSError, ValueError):
            now = None
        if now != recorded:
            path = Path(top)
            if now is not None and now["root"] == recorded["root"]:
                entries, old = now["entries"], recorded["entries"]
                if isinstance(entries, dict) and isinstance(old, dict):
                    keys = set(entries) ^ set(old) or {k for k in entries if entries[k] != old[k]}
                    path = path / sorted(keys)[0]
            raise _edited(target, f"{_shown(path, root)}, which a build script reads, changed")
    skip = set(inputs["walk_skip"])
    for top, recorded in inputs["trees"].items():
        try:
            now = _tree(Path(top), skip - {top}, budget=[MAX_RECORDED])
        except (OSError, ValueError):
            now = {}
        if now != recorded:
            # A file it holds, rather than a directory new or gone with it.
            name = min(
                (key for key in {*now, *recorded} if now.get(key) != recorded.get(key)),
                key=lambda key: (["dir"] in (now.get(key), recorded.get(key)), key),
            )
            raise _edited(target, f"{_shown(Path(top) / name, root)} changed")
    # What a client's are compared with, looked at again as the build did: one under a
    # linked directory, or a link's target, isn't in the walk.
    for top, listed in (inputs.get("lists") or {}).items():
        for name, recorded in listed["files"].items():
            try:
                same = _compared_file(Path(top), name, root, {}) == recorded
            except (OSError, ValueError):
                same = False
            if not same:
                raise _edited(target, f"{_shown(Path(top) / name, root)} changed")
    fresh = _environment(
        target,
        directory / "cargo-home",
        directory,
        directory / "wrapper" / "rustc-peeky",
        {},
        "",
        manifest["rustflags"],
    )
    for record in inputs["env"]:
        name, value = record["name"], record["value"]
        if record["from"] == "jobs":
            now = str(jobs())
        elif record["from"] == "launch" and name not in _PER_BUILD:
            now = fresh.get(name)
        else:
            # Cargo's own, of the tools, the configuration and the manifests: the build's
            # other records hold what decides it.
            continue
        if now != value:
            raise _edited(
                target, f"the build environment's {name}, which a build script reads, changed"
            )


def _shown(text, root):
    """A refusal's text with paths under the root relative to it."""
    return str(text).replace(f"{Path(root)}/", "")


def build_command(target_argument, config_argument):
    """`peeky build` as the user would type it again, from the same directory."""
    return f"peeky build {shlex.quote(str(target_argument))} --config {shlex.quote(str(config_argument))}"
