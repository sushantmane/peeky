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
from pathlib import Path

from peeky import rust_workspace
from peeky.build import BuildCommandError, UnreadDependencies, dependency_paths, real_tool
from peeky.digest import sha256_file
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
SCHEMA = 1

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
    # A remote client's files aren't compared with a workspace's yet: such a launch is
    # refused, and the worker declares no sources (never Python's .py files).
    compares_remote_sources = False
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

    def build(self, root, *, deadline=None, progress=None):
        """Build the workspace (`peeky build`): a summary of the build."""
        return build(self, root, deadline=deadline, progress=progress)

    def current(self, root):
        """The current build's manifest (current_build)."""
        return current_build(self, root)

    def prepare(self, program, root, *, edit=None, deadline=None):
        if edit is not None:
            raise ValueError(
                "Peeky doesn't verify or probe candidate edits in a Rust workspace yet; "
                "change the source and run `peeky build` again"
            )
        root = Path(root).resolve(strict=True)
        # Held while the program runs, so no later build removes its files.
        with lease_build(self, root) as lease:
            manifest = lease.manifest
            relative = Path(program).resolve().relative_to(root).as_posix()
            analysed = [unit for unit in manifest["units"] if relative in unit["sources"]]
            if not analysed:
                packages = ", ".join(self.config.analyse)
                raise ValueError(
                    f"Peeky stops only in the analysed packages' files ({packages}), as their "
                    f"build read them: {relative} is none of those. Probe a line there, or add "
                    'its package to "analyse" and build again'
                )
            raise ValueError(
                "Peeky doesn't read values in a Rust workspace's build yet: it will once a "
                "call into a dependency counts as code it can't read. The build is current; "
                "nothing ran"
            )


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
    return sha256_file(path, store=store)


def _digest(store):
    """A SHA-256 function that keeps digests in ``store`` (digest.sha256_file's rules)."""
    return lambda path: sha256_file(path, store=store)


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

    for top in tops:
        for base, folders, files in os.walk(top, onerror=failed):
            if base in seen:
                folders[:] = []
                continue
            seen.add(base)
            folders[:] = [name for name in folders if str(Path(base, name)) not in skipped]
            for name in (".", *folders, *files):
                if count >= _MAX_SNAPSHOT:
                    found[":limit"] = [count, base]
                    return found
                path = os.path.normpath(os.path.join(base, name))
                try:
                    info = os.lstat(path)
                    found["l:" + path] = (info.st_mtime_ns, info.st_ctime_ns, info.st_size)
                    count += 1
                    if stat.S_ISLNK(info.st_mode):
                        with contextlib.suppress(FileNotFoundError):
                            info = os.stat(path)
                            found["f:" + path] = (info.st_mtime_ns, info.st_ctime_ns, info.st_size)
                except OSError as error:
                    failed(error)
            # Listed whole: an entry it doesn't name wasn't there.
            found["d:" + os.path.normpath(base)] = True
    return found


# What to do where Peeky can't tell whether a missing input was there (_absent_before).
_ABSENCE_REMEDY = {
    "limit": f"Keep fewer than {_MAX_SNAPSHOT:,} files under the workspace (move data the "
    "build doesn't use out of it), or take the line out of the build script",
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
    for failing, why in before.get(":uncovered", ()):
        if path.is_relative_to(failing) or Path(failing).is_relative_to(path):
            return ("unread", f"Peeky couldn't read {failing} before the build ({why})")
    # The deepest directory on the way that was listed whole: the next step down from it
    # wasn't among its entries, so nothing under it was there.
    for ancestor in path.parents:
        if before.get("d:" + str(ancestor)):
            step = ancestor / path.relative_to(ancestor).parts[0]
            if "l:" + str(step) in before:
                return ("there", f"{step} was there before the build")
            return None
    return ("outside", "it is outside what Peeky records before a build")


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
            found = sorted(path.parent.glob("build_script_build-*.d"))
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
        return tuple(before[key]) != now
    return max(info.st_mtime_ns, info.st_ctime_ns) >= started_ns - _SETTLE_NS


def build(target, root, *, deadline=None, progress=None):
    """`peeky build`: build ``target``'s workspace under ``root`` and write its manifest."""
    say = progress or (lambda message: None)
    root = Path(root).resolve(strict=True)
    deadline = time.monotonic() + BUILD_TIMEOUT_S if deadline is None else deadline
    started, started_ns = time.monotonic(), time.time_ns()
    directory = build_directory(target, root)
    from peeky.build import _digest_store, require_private

    require_private(directory)
    with _locked(directory, exclusive=True):
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
            manifest["build_inputs"] = _build_inputs(
                shape, root, messages, env, before, started_ns, vendor, jobs_
            )
            # What Cargo read, as Peeky hashed it before Cargo ran: unchanged after the build.
            rust_workspace.changed(shape.watch, digest, "while Peeky built the workspace")
        except BaseException:
            shutil.rmtree(run, ignore_errors=True)
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
    sources, generated, units = {}, {}, []
    seen_units = set()
    # The analysed compiles the wrapper recorded, by crate, source file and whether a test.
    compiled = {}
    for unit in sorted((run / "units").iterdir()):
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
        if macro:
            _check_bootstrap_mentions(
                read, root, target_dir, f"the procedural macro `{artifact.get('name')}`"
            )
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
    if strays := {path.name for path in (run / "units").iterdir()} - seen_units:
        raise ValueError(
            f"Peeky's wrapper compiled {', '.join(sorted(strays))}, which Cargo's messages "
            "don't name"
        )
    copied = _copy_executable(Path(executable), run)
    for each in (run / "units").rglob("*.PreCodegen.before.mir"):
        # Only the bodies after rustc's last MIR pass are read.
        each.unlink()
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
        "units": units,
        "sources": dict(sorted(sources.items())),
        "generated": dict(sorted(generated.items())),
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
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError(
            f"Peeky records at most {MAX_RECORDED} files a build script or procedural "
            f"macro may read, and {where} holds more. Build fewer packages' files, or "
            "give the build script `cargo::rerun-if-changed` lines"
        )


def _tree(top, skip=(), *, budget):
    """{path relative to ``top``: SHA-256} of every file under ``top``, as Cargo lists a
    package's files: no hidden directory, no `target` directory, no other package's
    directory (one with a Cargo.toml) and none of ``skip``. ``budget``: a list holding the
    files left to record, spent here."""
    top = Path(top)
    found = {}
    for base, folders, files in os.walk(top):
        folders[:] = sorted(
            name
            for name in folders
            if not name.startswith(".")
            and name != "target"
            and str(Path(base, name)) not in skip
            and not (Path(base, name) / "Cargo.toml").exists()
        )
        for name in sorted(files):
            path = Path(base, name)
            if path.is_symlink() or not path.is_file():
                continue
            _spend(budget, top)
            found[path.relative_to(top).as_posix()] = _hash(path)
    return found


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
        for entry in sorted(os.scandir(directory), key=lambda each: each.name):
            relative, path = prefix + entry.name, Path(entry.path)
            _spend(budget, top)
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


def _build_inputs(shape, root, messages, env, before, started_ns, vendor, jobs_=1):
    """What build scripts and procedural macros may read, beyond what rustc's dependency
    files list, as the manifest keeps it: {"files": {path: identity}, "dirs": {directory:
    _exact_tree}, "trees": {directory: _tree}, "env": {variable: value or None}}. That
    is each local build script's `rerun-if-changed` paths (a directory, everything under
    it), or with none, Cargo's default, every file of its package; every build
    script's `rerun-if-env-changed` variables (dependencies' too), as Cargo gives them
    (_script_value); and with a local procedural macro, every file of its package and of
    each analysed one (a macro reading files elsewhere isn't followed). Refused where one
    changed while Cargo ran, or where Peeky can't record it."""
    skip = {shape.watch["vendor_directory"]} if vendor else set()
    files, dirs, trees, variables, budget = {}, {}, {}, {}, [MAX_RECORDED]
    by_directory = {str((root / package.directory).resolve()): package for package in shape.local}

    def unchanged(path):
        # The entry itself (a directory, a link, a file), and a file's contents.
        if _entry_changed_during(path, before, started_ns) or (
            Path(path).is_file() and _changed_during(path, before, started_ns)
        ):
            raise RuntimeError(
                f"Provenance: {_shown(path, root)} changed while Peeky built the "
                "workspace, so the build may not be of it. Build again"
            )

    def watch_tree(directory):
        key = str(directory)
        if key not in trees:
            trees[key] = _tree(directory, skip, budget=budget)
            for name in trees[key]:
                unchanged(directory / name)

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
            watch_tree(package_directory)
        for each in changed:
            # Its directory as found; its last segment not followed (a link is recorded).
            joined = package_directory / each
            if joined.name in ("", ".", ".."):
                path = Path(os.path.realpath(joined))
            else:
                path = Path(os.path.realpath(joined.parent)) / joined.name
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
            elif str(path) not in files:
                _spend(budget, path)
                unchanged(path)
                files[str(path)] = rust_workspace.identity(path)
                unproven = files[str(path)] == ["missing"] and _absent_before(path, before)
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


def _read_dependencies(depfile, workspace, root, target_dir, package, producer="rustc"):
    try:
        paths = dependency_paths(depfile.read_text(errors="surrogateescape"), workspace, producer)
    except UnreadDependencies as exc:
        raise ValueError(f"{exc} ({depfile.name}, of `{package}`)") from None
    found = {"sources": [], "generated": []}
    for path in paths:
        resolved = path.resolve()
        if resolved.is_relative_to(target_dir):
            found["generated"].append(resolved.relative_to(target_dir).as_posix())
        elif resolved.is_relative_to(root):
            found["sources"].append(resolved.relative_to(root).as_posix())
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
        shutil.copytree(symbols, folder / symbols.name, symlinks=True)
    return copy.relative_to(run.parent.parent).as_posix()


def _artifacts(directory, run, store):
    found = {}
    for path in sorted(run.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Peeky's build holds a link: {path}")
        if path.is_file():
            found[path.relative_to(directory).as_posix()] = sha256_file(path, store=store)
            if len(found) > MAX_RECORDED:
                raise ValueError(f"The build wrote more than {MAX_RECORDED} files Peeky records")
    return found


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
    kept = sorted(
        (path for path in runs.iterdir() if path.is_dir()),
        key=lambda path: (path.name == nonce, path.stat().st_mtime_ns),
        reverse=True,
    )
    for path in kept[KEEP_RUNS:]:
        if not _held(leases / path.name):
            shutil.rmtree(path, ignore_errors=True)
    if leases.is_dir():
        for lease in leases.iterdir():
            if not (runs / lease.name).is_dir():
                _held(lease)


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
        descriptor = _lease(directory, manifest["nonce"]) if hold else None
    manifest["directory"] = str(directory)
    return manifest, descriptor


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
    vendor = manifest["watch"].get("vendor_directory")
    for top, recorded in inputs["trees"].items():
        try:
            now = _tree(Path(top), {vendor} if vendor else set(), budget=[MAX_RECORDED])
        except (OSError, ValueError):
            now = {}
        if now != recorded:
            name = sorted(set(now.items()) ^ set(recorded.items()))[0][0]
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
