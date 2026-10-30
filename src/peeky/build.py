"""Trusted build recipes and integrity manifests; no language support registry.

Fresh workspaces exclude stale outputs. Commands remain trusted developer code,
not a sandbox or hermetic build: declared environment, files and executable bytes
are identified; shared libraries, external services and undeclared dependencies
are not. Call assert_current before launch, mutation and verification. Adapters
must additionally establish loaded-artifact identity where available.
"""

import contextlib
import functools
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from peeky.digest import sha256_file
from peeky.processes import CREDENTIAL_ENV, without_credentials

# Inherited flags (JAVA_TOOL_OPTIONS, CFLAGS, etc.) require explicit declaration.
INHERITED_ENV = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL")
MAX_FILES = 4096
MAX_FILE_BYTES = 256 * 1024 * 1024
# The most build commands of one parallel group that run at once (PEEKY_BUILD_JOBS).
MAX_JOBS = 8


class BuildCommandError(RuntimeError):
    """A completed command failure/budget limit, distinct from provenance errors."""

    def __init__(self, command, result):
        self.command = tuple(command)
        self.result = dict(result)
        # The output stays in ``result``: it can be up to BUILD_RETAINED per stream.
        outcome = (
            "timed out"
            if self.result.get("timed_out")
            else "output limit exceeded"
            if self.result.get("output_limit_exceeded")
            else f"exit code {self.result.get('exit_code')}"
        )
        super().__init__(f"Build command failed: {self.command[0]}: {outcome}")


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_s: float = 30


@dataclass(frozen=True)
class ArtifactSpec:
    path: str
    kind: str = "file"  # file or directory
    role: str = "executable"  # provenance metadata, not a capability claim


@dataclass(frozen=True)
class SourceEdit:
    path: str
    old: str
    new: str


@dataclass(frozen=True)
class BuildRecipe:
    language: str
    inputs: tuple[str, ...]
    build_commands: tuple[Command, ...] = ()
    artifacts: tuple[ArtifactSpec, ...] = ()
    toolchain_probes: tuple[Command, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class FileIdentity:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ToolIdentity:
    path: str
    sha256: str


@dataclass(frozen=True)
class ProbeResult:
    argv: tuple[str, ...]
    stdout: str
    stderr: str


@dataclass(frozen=True)
class BuildManifest:
    schema_version: int
    recipe_sha256: str
    original_inputs: tuple[FileIdentity, ...]
    inputs: tuple[FileIdentity, ...]
    artifacts: tuple[FileIdentity, ...]
    tools: tuple[ToolIdentity, ...]
    probes: tuple[ProbeResult, ...]
    effective_environment: tuple[tuple[str, str], ...]

    @property
    def manifest_id(self):
        return _json_digest(asdict(self))

    @property
    def build_id(self):
        """The manifest's digest without its artifacts, whose bytes name the fresh
        workspace (the debug information's paths), so every build differs there: the same
        for two builds of one recipe, the same staged sources (an edit included), tools,
        toolchain probes and environment, and different where any differs."""
        return _json_digest({k: v for k, v in asdict(self).items() if k != "artifacts"})


@dataclass(frozen=True)
class PreparedRun:
    workspace: Path
    source_root: Path
    recipe: BuildRecipe
    manifest: BuildManifest

    def cleanup(self):
        """Remove only this fresh owned workspace; the source root is untouched."""
        if self.workspace.exists():
            shutil.rmtree(self.workspace)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cleanup()


def _json_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _relative(name, *, directory=False):
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("Expected a nonempty POSIX relative path")
    if directory and name == ".":
        return name
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("Path must be relative without traversal or empty components")
    return name


def _inside(root, name, *, directory=False):
    _relative(name, directory=directory)
    path = root / name
    for part in path.relative_to(root).parts:
        root = root / part
        if root.is_symlink():
            raise ValueError("Symlinks are not allowed in declared build paths")
    return path


_XCRUN_STUBS = frozenset(
    {"cc", "c++", "clang", "clang++", "dsymutil", "dwarfdump", "llvm-dwarfdump", "lldb-dap", "ld"}
)


_XCRUN = "/usr/bin/xcrun"


@functools.lru_cache(maxsize=64)
def _xcrun_find(name):
    try:
        found = subprocess.run(
            [_XCRUN, "--find", name], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return found.stdout.strip() if found.returncode == 0 else ""


@functools.lru_cache(maxsize=1)
def default_sdk():
    """The macOS SDK the xcrun stubs would pass; real compilers need it explicitly."""
    if sys.platform != "darwin" or not os.path.exists(_XCRUN):
        return None
    try:
        found = subprocess.run(
            [_XCRUN, "--show-sdk-path"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = found.stdout.strip()
    return path if found.returncode == 0 and path and Path(path).is_dir() else None


def real_tool(path):
    """The executable a tool path really runs.

    On macOS, /usr/bin/clang, dsymutil, dwarfdump and friends are one small xcrun
    stub that runs the selected developer tool. Hashing the stub would not notice an
    `xcode-select` switch or a Command Line Tools update, so provenance uses the real
    binary that `xcrun --find` names.
    """
    resolved = Path(path).resolve(strict=True)
    if (
        sys.platform != "darwin"
        or resolved.parent != Path("/usr/bin")
        or resolved.name not in _XCRUN_STUBS
        # Without xcrun there is no stub: the tool is a real binary.
        or not os.path.exists(_XCRUN)
    ):
        return resolved
    found = _xcrun_find(resolved.name)
    real = Path(found) if found else None
    if real is None or real.resolve(strict=True).parent == Path("/usr/bin"):
        raise ValueError(
            f"{resolved} is the macOS xcrun stub and `xcrun --find {resolved.name}` did not name "
            "the real tool; pass the real tool's path"
        )
    return real.resolve(strict=True)


def _digest(path, store=None):
    size = path.stat().st_size
    if not path.is_file() or size > MAX_FILE_BYTES:
        raise ValueError("Expected a regular file within the identity size limit")
    return sha256_file(path, store=store), size


def _files(root, names):
    result = []
    for name in sorted(set(names)):
        path = _inside(root, name)
        digest, size = _digest(path)
        result.append(FileIdentity(name, digest, size))
    return tuple(result)


def _artifacts(root, specs):
    names = []
    for spec in specs:
        path = _inside(root, spec.path)
        if spec.kind == "file":
            if not path.is_file():
                raise ValueError(f"Missing required artifact: {spec.path}")
            names.append(spec.path)
        else:
            if not path.is_dir():
                raise ValueError(f"Missing artifact directory: {spec.path}")
            members = []
            for item in path.rglob("*"):
                if item.is_symlink():
                    raise ValueError("Artifact directories cannot contain symlinks")
                if item.is_file():
                    members.append(item.relative_to(root).as_posix())
                    if len(names) + len(members) > MAX_FILES:
                        raise ValueError("Artifact file count exceeds limit")
            if not members:
                raise ValueError("Required artifact directory is empty")
            names.extend(members)
        if len(names) > MAX_FILES:
            raise ValueError("Artifact file count exceeds limit")
    return _files(root, names)


def _validate(recipe):
    if not isinstance(recipe.language, str) or not recipe.language:
        raise ValueError("A language label is required")
    for field in (
        recipe.inputs,
        recipe.build_commands,
        recipe.artifacts,
        recipe.toolchain_probes,
        recipe.environment,
    ):
        if not isinstance(field, tuple):
            raise ValueError("Recipe collections must be immutable tuples")
    if (
        not recipe.inputs
        or len(recipe.inputs) > MAX_FILES
        or len(set(recipe.inputs)) != len(recipe.inputs)
    ):
        raise ValueError("Declare a nonempty bounded set of unique input files")
    for name in recipe.inputs:
        _relative(name)
    if recipe.build_commands and not recipe.artifacts:
        raise ValueError("Build commands must declare required artifacts")
    if recipe.artifacts and not recipe.build_commands:
        raise ValueError("Produced artifacts require an explicit fresh build command")
    for spec in recipe.artifacts:
        _relative(spec.path)
        if any(name == spec.path or name.startswith(spec.path + "/") for name in recipe.inputs):
            raise ValueError("Build outputs must not be copied inputs")
        if spec.kind not in ("file", "directory") or not spec.role:
            raise ValueError("Invalid artifact kind or role")
    for command in (*recipe.toolchain_probes, *recipe.build_commands):
        if not isinstance(command.argv, tuple) or not command.argv:
            raise ValueError("Command argv must be a nonempty tuple")
        if any(not isinstance(arg, str) or "\x00" in arg for arg in command.argv):
            raise ValueError("Command arguments must be strings without NUL")
        _relative(command.cwd, directory=True)
        if (
            type(command.timeout_s) not in (int, float)
            or not math.isfinite(command.timeout_s)
            or command.timeout_s <= 0
        ):
            raise ValueError("Command timeout must be positive and finite")
    keys = []
    for entry in recipe.environment:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ValueError("Environment entries must be immutable key/value pairs")
        key, value = entry
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\x00" in key
            or not isinstance(value, str)
            or "\x00" in value
        ):
            raise ValueError("Invalid declared environment")
        if key in CREDENTIAL_ENV:
            raise ValueError("Worker credentials cannot be passed to builds")
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate declared environment key")


def _executable(command, workspace, env):
    """(the command's working directory, the executable it runs, resolved)."""
    cwd = _inside(workspace, command.cwd, directory=True)
    if not cwd.is_dir():
        raise ValueError("Build cwd must be an existing directory inside its workspace")
    executable = command.argv[0]
    if "/" in executable:
        executable = Path(executable)
        if not executable.is_absolute():
            name = str(executable)
            if name.startswith("./"):
                name = name[2:]
            executable = _inside(cwd, name)
    else:
        executable = shutil.which(executable, path=env.get("PATH"))
        if not executable:
            raise ValueError(f"Missing toolchain executable: {command.argv[0]}")
    return cwd, Path(executable).resolve(strict=True)


def _run(command, workspace, env, timeout=None, *, stop=None, gate=None, store=None):
    """``stop`` and ``gate``: see agent.execute. ``store``: where the executable's digest
    is kept for later processes (digest.sha256_file)."""
    # agent is the CLI's module (it loads the worker): never imported at module scope.
    from peeky.agent import BUILD_RETAINED, execute

    cwd, executable = _executable(command, workspace, env)
    digest, _ = _digest(executable, store)
    timeout = command.timeout_s if timeout is None else timeout
    result = execute(
        [str(executable), *command.argv[1:]],
        cwd,
        timeout,
        env=env,
        retain=BUILD_RETAINED,
        stop=stop,
        gate=gate,
    )
    try:
        unchanged = _digest(executable)[0] == digest
    except (OSError, ValueError) as exc:
        raise RuntimeError("Toolchain executable changed during build") from exc
    if not unchanged:
        raise RuntimeError("Toolchain executable changed during build")
    if result["exit_code"] != 0:
        raise BuildCommandError(command.argv, result)
    return ToolIdentity(str(executable), digest), result


def _budget(command, deadline):
    """Per-command timeout bounded by an optional overall monotonic deadline."""
    if deadline is None:
        return command.timeout_s, False
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Build preparation exceeded its overall deadline")
    return min(command.timeout_s, remaining), remaining < command.timeout_s


def jobs():
    """How many commands of a parallel group run at once: PEEKY_BUILD_JOBS, or the CPUs
    this process may use, at most MAX_JOBS."""
    configured = os.environ.get("PEEKY_BUILD_JOBS")
    if configured:
        if not configured.isdigit() or not 1 <= int(configured) <= 64:
            raise ValueError("PEEKY_BUILD_JOBS must be a whole number from 1 to 64")
        return int(configured)
    if hasattr(os, "sched_getaffinity"):
        count = len(os.sched_getaffinity(0))
    else:
        count = os.cpu_count() or 1
    return max(1, min(count, MAX_JOBS))


def _groups(commands, parallel):
    """{the index of a parallel group's first command: its size}. Each group must be a
    run of consecutive build commands, and no two may share one."""
    starts, taken = {}, set()
    for group in parallel:
        group = tuple(group)
        at = next(
            (
                at
                for at in range(len(commands) - len(group) + 1)
                if group and commands[at : at + len(group)] == group
            ),
            None,
        )
        if at is None or taken & set(range(at, at + len(group))):
            raise ValueError(
                "Each parallel group must be its own run of consecutive build commands"
            )
        taken.update(range(at, at + len(group)))
        starts[at] = len(group)
    return starts


class _Cancelled(Exception):
    """A command of a parallel group that did not start: one before it failed."""


def _run_group(group, workspace, env, deadline, workers, store=None):
    """Runs ``group``'s commands at the same time, ``workers`` at once: [(tool, result)]
    in the group's order. Workers take commands in that order, but their processes may
    start in any order. When commands fail, the earliest one's error is raised: the
    first a build in order reaches, as the commands are independent. Once one fails,
    the commands after it are stopped, and none after it starts: a process is started
    only under the lock that records a failure, after checking that none before it
    failed."""
    outcomes = [None] * len(group)
    stops = [threading.Event() for _ in group]
    lock = threading.Lock()
    # The next command to claim, and the earliest that failed (len(group): none).
    state = {"next": 0, "failed": len(group)}

    @contextlib.contextmanager
    def gate(index):
        with lock:
            if index >= state["failed"]:
                raise _Cancelled
            yield

    def work():
        while True:
            with lock:
                index = state["next"]
                if index >= state["failed"]:
                    return
                state["next"] += 1
            try:
                timeout, limited = _budget(group[index], deadline)
                try:
                    outcomes[index] = _run(
                        group[index],
                        workspace,
                        env,
                        timeout,
                        stop=stops[index],
                        gate=functools.partial(gate, index),
                        store=store,
                    )
                except BuildCommandError as exc:
                    if limited and exc.result.get("timed_out"):
                        raise TimeoutError(
                            "Build preparation exceeded its overall deadline"
                        ) from exc
                    raise
            except BaseException as exc:
                outcomes[index] = exc
                with lock:
                    if index < state["failed"]:
                        state["failed"] = index
                        for later in stops[index + 1 :]:
                            later.set()

    threads = [threading.Thread(target=work, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join()
    finally:
        if any(thread.is_alive() for thread in threads):
            # Interrupted: start nothing more, stop what runs, and wait for it, so that
            # nothing writes to the workspace while it is removed.
            with lock:
                state["failed"] = -1
            for stop in stops:
                stop.set()
            for thread in threads:
                thread.join()
    if state["failed"] < len(group):
        raise outcomes[state["failed"]]
    return outcomes


def _digest_store():
    """Where the digests of toolchain executables are kept for later processes
    (``keep_tool_digests``), or None: when PEEKY_NO_DIGEST_CACHE=1, or where the state
    directory is unusable."""
    if os.environ.get("PEEKY_NO_DIGEST_CACHE") == "1":
        return None
    from peeky.cli import _private_dir

    try:
        _private_dir("cache")
        return _private_dir("cache/digests")
    except (OSError, ValueError):
        return None


def _assert_tools(tools, stage):
    """Every previously recorded tool still has its probed bytes (drivers delegate to them)."""
    try:
        unchanged = all(_digest(Path(tool.path))[0] == tool.sha256 for tool in tools)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Toolchain changed during {stage}") from exc
    if not unchanged:
        raise RuntimeError(f"Toolchain changed during {stage}")


def prepare(
    recipe,
    source_root,
    *,
    parent=None,
    edit=None,
    deadline=None,
    before_command=None,
    validate_workspace=None,
    parallel=(),
    keep_tool_digests=False,
):
    """Copy declared inputs to a fresh workspace, build, and retain a manifest.

    An optional single SourceEdit preserves the original relative filename.
    Callers own cleanup (or use the returned PreparedRun as a context manager).
    Build failures clean their temporary workspace automatically. ``deadline`` is
    an overall time.monotonic() bound; exhausting it is a fatal TimeoutError, never
    retryable compiler feedback. Immediately before **every** probe and build
    command, the trusted ``validate_workspace(workspace, env)`` callback checks the
    staged workspace and environment, ``before_command(workspace, probes, command)``
    additionally validates probe results before each build command, and every tool
    recorded so far plus the original and staged inputs are rechecked. Any failure
    there is fatal, never compiler feedback.

    ``parallel``: groups of consecutive build commands that are independent of each
    other: each reads the inputs and writes only its own outputs. A group's commands run
    at the same time (``jobs()`` at once), in no set order, after the checks before each
    of them; the checks before the next command cover what any of them did. When several
    fail, the earliest one's error is raised, and none starts after a failure is recorded
    (``_run_group``). What a command writes may still depend on when it runs
    (`__TIME__`), as in any two builds. PEEKY_BUILD_JOBS=1 runs them one after another.
    ``keep_tool_digests``: the digest of each executable a probe or command runs is
    kept in Peeky's state directory, so that a new process reuses it under
    digest.sha256_file's rules. Toolchain probes always run.
    """
    _validate(recipe)
    groups = _groups(recipe.build_commands, parallel)
    source_root = Path(source_root).resolve(strict=True)
    original = _files(source_root, recipe.inputs)
    if edit is not None:
        _relative(edit.path)
        if edit.path not in recipe.inputs or not edit.old or edit.old == edit.new:
            raise ValueError("Edit must change exactly one declared input substring")
    workspace = Path(tempfile.mkdtemp(prefix="peeky-build-", dir=parent)).resolve()
    try:
        for name in recipe.inputs:
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_inside(source_root, name), target)
        if _files(workspace, recipe.inputs) != original:
            raise RuntimeError("Source changed while preparing workspace")
        if edit is not None:
            path = workspace / edit.path
            source = path.read_text()
            if source.count(edit.old) != 1:
                raise ValueError("Edit must match exactly once")
            path.write_text(source.replace(edit.old, edit.new, 1))
        inputs = _files(workspace, recipe.inputs)
        tools, probes = [], []
        env = {key: os.environ[key] for key in INHERITED_ENV if key in os.environ}
        env.update(dict(recipe.environment))
        env = without_credentials(env)

        def check(command=None):
            # Runs before each command: an earlier command or callback must not have
            # changed what the next one reads or delegates to.
            if validate_workspace is not None:
                validate_workspace(workspace, dict(env))
            if command is not None and before_command is not None:
                before_command(workspace, tuple(probes), command)
            _assert_tools(tools, "preparation")
            try:
                original_unchanged = _files(source_root, recipe.inputs) == original
                staged_unchanged = _files(workspace, recipe.inputs) == inputs
            except (OSError, ValueError) as exc:
                raise RuntimeError("Source provenance changed during preparation") from exc
            if not original_unchanged:
                raise RuntimeError("Source provenance changed during preparation")
            if not staged_unchanged:
                raise RuntimeError("Staged inputs changed during preparation (provenance mismatch)")

        store = _digest_store() if keep_tool_digests else None
        for command in recipe.toolchain_probes:
            check()
            timeout, limited = _budget(command, deadline)
            try:
                tool, result = _run(command, workspace, env, timeout, store=store)
            except BuildCommandError as exc:
                if limited and exc.result.get("timed_out"):
                    raise TimeoutError("Build preparation exceeded its overall deadline") from exc
                raise RuntimeError(f"Toolchain probe failed: {command.argv[0]}") from exc
            if tool not in tools:
                tools.append(tool)
            probes.append(ProbeResult(command.argv, result["stdout"], result["stderr"]))
        commands, index = recipe.build_commands, 0
        while index < len(commands):
            group = commands[index : index + groups.get(index, 1)]
            index += len(group)
            if len(group) > 1 and (workers := min(len(group), jobs())) > 1:
                for command in group:
                    check(command)
                ran = _run_group(group, workspace, env, deadline, workers, store)
                ran = [tool for tool, _ in ran]
            else:
                ran = []
                for command in group:
                    check(command)
                    timeout, limited = _budget(command, deadline)
                    try:
                        tool, _ = _run(command, workspace, env, timeout, store=store)
                    except BuildCommandError as exc:
                        if limited and exc.result.get("timed_out"):
                            raise TimeoutError(
                                "Build preparation exceeded its overall deadline"
                            ) from exc
                        raise
                    # Each command's checks cover the tools of those before it.
                    if tool not in tools:
                        tools.append(tool)
            # One entry per executable: every command rechecks the whole list.
            for tool in ran:
                if tool not in tools:
                    tools.append(tool)
        # The last command may have delegated to helpers (e.g. dsymutil via PATH);
        # recheck before its outputs are recorded against the declared tools.
        check()
        manifest = BuildManifest(
            1,
            _json_digest(asdict(recipe)),
            original,
            inputs,
            _artifacts(workspace, recipe.artifacts),
            tuple(sorted(set(tools), key=lambda t: t.path)),
            tuple(probes),
            tuple(sorted(env.items())),
        )
        prepared = PreparedRun(workspace, source_root, recipe, manifest)
        assert_current(prepared)
        return prepared
    except BuildCommandError:
        # A failed compiler is retryable only while both its staged inputs and
        # the original sources still match the identities captured before it ran.
        try:
            try:
                unchanged = (
                    _files(source_root, recipe.inputs) == original
                    and _files(workspace, recipe.inputs) == inputs
                )
            except (OSError, ValueError) as exc:
                raise RuntimeError("Source provenance changed during failed build") from exc
            if not unchanged:
                raise RuntimeError("Source provenance changed during failed build")
            # Drivers delegate to earlier probed tools; their drift is never compiler feedback.
            _assert_tools(tools, "failed build")
            raise
        finally:
            shutil.rmtree(workspace)
    except BaseException:
        shutil.rmtree(workspace)
        raise


def assert_current(prepared):
    """Reject missing/stale sources, outputs, recipe or tool executable bytes.

    Filesystem checks do not establish which image a process loaded and do not
    prevent concurrent malicious changes. This is trusted-code integrity checking.
    """
    manifest = prepared.manifest
    try:
        matches = (
            _json_digest(asdict(prepared.recipe)) == manifest.recipe_sha256
            and _files(prepared.source_root, prepared.recipe.inputs) == manifest.original_inputs
            and _files(prepared.workspace, prepared.recipe.inputs) == manifest.inputs
            and _artifacts(prepared.workspace, prepared.recipe.artifacts) == manifest.artifacts
            and all(_digest(Path(tool.path))[0] == tool.sha256 for tool in manifest.tools)
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError("Prepared build provenance mismatch") from exc
    if not matches:
        raise RuntimeError("Prepared build provenance mismatch")


class UnreadDependencies(ValueError):
    """A compiler's dependency file that Peeky cannot read unambiguously: its
    provenance check fails rather than skip or misread an entry."""

    def __init__(self, why):
        super().__init__(f"Peeky cannot read the compiler's dependency file unambiguously: {why}")


def dependency_paths(text, base, producer):
    """Every file a compile read, from its dependency file: ``producer`` "rustc" (rustc's
    `--emit=dep-info`) or "make" (`clang -MD`, `gcc -MD`), each read by its own escaping.
    Lines end at `\n` alone (a U+2028 is part of a name). Anything that could be read two
    ways raises UnreadDependencies."""
    names = _rustc_dependencies(text) if producer == "rustc" else _make_dependencies(text, base)
    return [path if (path := Path(name)).is_absolute() else Path(base) / path for name in names]


def _rustc_dependencies(text):
    r"""rustc writes each output's rule (`out: a.rs b\ c.rs`, a blank line after each),
    then one `name:` line per file, then, after a blank line, `# env-dep:` notes. It
    escapes only a space (`\ `), so a backslash before another character is itself.
    Each file's own line gives its name; every rule must list exactly those, so a name
    that breaks a line (a newline in it) or starts with `#` cannot pass for others."""
    lines = text.split("\n")
    if lines[-1:] == [""]:
        lines.pop()
    end = len(lines)
    while end and lines[end - 1].startswith("#"):
        end -= 1
    if end < len(lines):
        if not end or lines[end - 1] != "":
            raise UnreadDependencies("its notes do not follow a blank line")
        end -= 1
    start = end
    while start and lines[start - 1] != "":
        start -= 1
    files, rules = lines[start:end], lines[:start]
    if not files or any(not line.endswith(":") or line.startswith("#") for line in files):
        raise UnreadDependencies("a file's own line is not `name:`")
    if not rules or len(rules) % 2 or any(rules[1::2]) or not all(rules[::2]):
        raise UnreadDependencies("its rules are not each followed by a blank line")
    listed = ": " + " ".join(line[:-1] for line in files)
    if any(not rule.endswith(listed) or len(rule) == len(listed) for rule in rules[::2]):
        raise UnreadDependencies("a rule does not list exactly the files named below it")
    return [line[:-1].replace("\\ ", " ") for line in files]


# A continued line's end in a make-style rule, and runs of backslashes and dollars.
_CONTINUED = " \\"
_BACKSLASHES = re.compile(r"\\*")
_DOLLARS = re.compile(r"\$*")
# A run of characters that stand for themselves in a rule.
_PLAIN = re.compile(r"[^\\$#\t ]+")


def _make_dependencies(text, base):
    r"""The prerequisites of the one rule in a `-MD` file (`peeky-object-0.o: a.c b\ c.h
    \`, continued on the next line). clang and gcc (10 and later) escape a space as `\ `
    (and a space after N backslashes as 2N+1 of them), `#` as `\#` and `$` as `$$`; gcc
    a tab as `\<tab>`, which clang leaves bare. Neither escapes a backslash ending a name,
    and clang prints a name's backslash as `/`. So these are refused: a name with a
    backslash or a tab, an undoubled `$`, a bare `#`, a line that neither ends the rule
    nor continues it (a name with a newline), and an escaped space where the file the
    other reading names (one ending in a backslash) exists; as are a `$` where the
    file with it undoubled exists too (gcc before 10 does not escape `$`)."""
    lines = text.split("\n")
    if lines[-1:] == [""]:
        lines.pop()
    if not lines:
        raise UnreadDependencies("it lists no rule")
    rule = []
    for index, line in enumerate(lines):
        if index < len(lines) - 1:
            # ` \` ends a continued line; its space is a separator (no backslash before).
            if not line.endswith(_CONTINUED) or line[:-2].endswith("\\"):
                raise UnreadDependencies(
                    "a line neither ends its rule nor continues it (a file name with a newline?)"
                )
            line = line[:-1]
        elif line.endswith("\\"):
            raise UnreadDependencies("its last line is continued")
        rule.append(line)
    rule = "".join(rule)
    target = re.match(r"[^\s\\:#$]+:(?: |$)", rule)
    if target is None:
        raise UnreadDependencies("its rule's target is not a plain name")
    names, name, raw, others = [], [], [], []
    text, at = rule[target.end() :], 0

    def close():
        if name:
            names.append(("".join(name), "".join(raw), list(others)))
        name.clear(), raw.clear(), others.clear()

    while at < len(text):
        if plain := _PLAIN.match(text, at):
            name.append(plain[0]), raw.append(plain[0])
            at = plain.end()
            continue
        char = text[at]
        if char == "\\":
            run = _BACKSLASHES.match(text, at).end() - at
            after = text[at + run : at + run + 1]
            if after == " " and run % 2:
                if run == 1:
                    # Or a name ending in a backslash (gcc), then a separator.
                    others.append("".join(name) + "\\")
                name.append("\\" * (run // 2) + " "), raw.append("\\" * (run // 2) + " ")
                at += run + 1
                continue
            if after == "#":
                name.append("\\" * (run - 1) + "#"), raw.append("\\" * (run - 1) + "#")
                at += run + 1
                continue
            name.append("\\" * (run // 2 if after == " " else run))
            raw.append(name[-1])
            at += run
            if after == " ":
                close()
                at += 1
            continue
        if char == "$":
            run = _DOLLARS.match(text, at).end() - at
            if run % 2:
                raise UnreadDependencies("a `$` is not doubled")
            name.append("$" * (run // 2)), raw.append("$" * run)
            at += run
            continue
        if char in "#\t":
            raise UnreadDependencies(f"a bare {'`#`' if char == '#' else 'tab'} in a file name")
        if char == " ":
            close()
        else:
            name.append(char), raw.append(char)
        at += 1
    close()
    found = []
    for name, raw, others in names:
        if "\\" in name or "\t" in name:
            raise UnreadDependencies(
                f"a backslash or tab in {name!r}, which make's escaping leaves ambiguous"
            )
        if "//" in name or name.endswith("/"):
            # clang prints a name's backslash as `/`: `/w/\\x.h` reads `/w//x.h`, which a
            # path would shorten to another file (`/w/x.h`).
            raise UnreadDependencies(
                f"an empty step in {name!r}, where clang may have printed a backslash"
            )
        for other in (*others, *((raw,) if raw != name else ())):
            path = Path(other)
            if (path if path.is_absolute() else Path(base) / path).exists():
                raise UnreadDependencies(f"{name!r} could also be {other!r}, which exists")
        found.append(name)
    return found
