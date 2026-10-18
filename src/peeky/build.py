"""Trusted build recipes and integrity manifests; no language support registry.

Fresh workspaces exclude stale outputs. Commands remain trusted developer code,
not a sandbox or hermetic build: declared environment, files and executable bytes
are identified; shared libraries, external services and undeclared dependencies
are not. Call assert_current before launch, mutation and verification. Adapters
must additionally establish loaded-artifact identity where available.
"""

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
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from peeky.digest import sha256_file

# Inherited flags (JAVA_TOOL_OPTIONS, CFLAGS, etc.) require explicit declaration.
INHERITED_ENV = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL")
MAX_FILES = 4096
MAX_FILE_BYTES = 256 * 1024 * 1024


class BuildCommandError(RuntimeError):
    """A completed command failure/budget limit, distinct from provenance errors."""

    def __init__(self, command, result):
        self.command = tuple(command)
        self.result = dict(result)
        super().__init__(f"Build command failed: {self.command[0]}: {self.result}")


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


def _digest(path):
    size = path.stat().st_size
    if not path.is_file() or size > MAX_FILE_BYTES:
        raise ValueError("Expected a regular file within the identity size limit")
    return sha256_file(path), size


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
        if key == "DEBUG_TOKEN":
            raise ValueError("Worker credentials cannot be passed to builds")
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate declared environment key")


def _run(command, workspace, env, timeout=None):
    # Temporary dependency reuse. Move execute to a shared process module when
    # the build boundary is integrated; never import agent at module scope.
    from peeky.agent import BUILD_RETAINED, execute

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
    executable = Path(executable).resolve(strict=True)
    digest, _ = _digest(executable)
    timeout = command.timeout_s if timeout is None else timeout
    result = execute(
        [str(executable), *command.argv[1:]], cwd, timeout, env=env, retain=BUILD_RETAINED
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
    """
    _validate(recipe)
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
        env.pop("DEBUG_TOKEN", None)

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

        for command in recipe.toolchain_probes:
            check()
            timeout, _ = _budget(command, deadline)
            try:
                tool, result = _run(command, workspace, env, timeout)
            except BuildCommandError as exc:
                raise RuntimeError(f"Toolchain probe failed: {command.argv[0]}") from exc
            if tool not in tools:
                tools.append(tool)
            probes.append(ProbeResult(command.argv, result["stdout"], result["stderr"]))
        for command in recipe.build_commands:
            check(command)
            timeout, limited = _budget(command, deadline)
            try:
                tool, _ = _run(command, workspace, env, timeout)
            except BuildCommandError as exc:
                if limited and exc.result.get("timed_out"):
                    raise TimeoutError("Build preparation exceeded its overall deadline") from exc
                raise
            # One entry per executable: every command rechecks the whole list.
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


def dependency_paths(text, base):
    """Prerequisites listed in a Makefile-style dependency file (`clang -MD`, rustc)."""
    joined = re.sub(r"\\\r?\n", " ", text)
    found = []
    for line in joined.splitlines():
        if line.lstrip().startswith("#"):
            # rustc's `# env-dep:NAME=value` notes; a literal `#` in a path is `\#`.
            continue
        match = re.search(r"(?<!\\):(?:\s|$)", line)
        if not match:
            continue
        rest = line[match.end() :]
        for item in re.findall(r"(?:\\.|[^\s\\])+", rest):
            name = re.sub(r"\\(.)", r"\1", item).replace("$$", "$")
            path = Path(name)
            found.append(path if path.is_absolute() else Path(base) / path)
    return found
