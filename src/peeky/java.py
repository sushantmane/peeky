"""Trusted standalone Java targets built freshly before debugger launch.

The source map comes from compiler metadata. Filesystem integrity checks bind
launch inputs and stop locations; they are not proof against malicious native
code or concurrent replacement of an already loaded JVM image.
"""

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from peeky.adapters import PythonAdapter
from peeky.build import ArtifactSpec, BuildRecipe, Command, assert_current, prepare

MAX_BUNDLE_FILES = 4096
MAX_BUNDLE_FILE_BYTES = 256 * 1024 * 1024
SCALAR = re.compile(r"(?:true|false|[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\Z")
CLASS_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*\Z")


def _hash(path):
    if not path.is_file() or path.stat().st_size > MAX_BUNDLE_FILE_BYTES:
        raise ValueError("Expected a bounded regular bundle file")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def _tool(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Invalid Java toolchain executable")
    found = shutil.which(value)
    if not found:
        raise ValueError(f"Missing Java toolchain executable: {value}")
    return str(Path(found).resolve(strict=True))


def _member(root, name):
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or PurePosixPath(name).is_absolute()
        or any(part in ("", ".", "..") for part in name.split("/"))
    ):
        raise ValueError("Invalid bundle member path")
    path = root
    for part in name.split("/"):
        path = path / part
        if path.is_symlink():
            raise ValueError("Bundle symlinks are unsupported")
    return path


def _bundle(bundle, java):
    manifest_path = bundle / "adapter.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 1024 * 1024:
        raise ValueError("Invalid adapter manifest")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema") != 1:
        raise ValueError("Unsupported adapter manifest schema")
    command = manifest.get("command")
    expected_classpath = f"{bundle}/classes{os.pathsep}{bundle}/lib/*"
    if (
        not isinstance(command, list)
        or len(command) != 4
        or not all(isinstance(arg, str) for arg in command)
        or _tool(command[0]) != java
        or command[1:] != ["-cp", expected_classpath, "PeekyJavaAdapter"]
    ):
        raise ValueError("Adapter command does not match the trusted Java bundle layout")
    files = manifest.get("files")
    if not isinstance(files, dict) or not 0 < len(files) <= MAX_BUNDLE_FILES:
        raise ValueError("Invalid adapter file inventory")
    actual = set()
    for path in bundle.rglob("*"):
        if path.is_symlink():
            raise ValueError("Bundle symlinks are unsupported")
        if path.is_file() and path != manifest_path:
            actual.add(path.relative_to(bundle).as_posix())
            if len(actual) > MAX_BUNDLE_FILES:
                raise ValueError("Adapter file inventory exceeds limit")
    if actual != set(files) or "classes/PeekyJavaAdapter.class" not in files:
        raise ValueError("Adapter file inventory mismatch")
    for name, expected in files.items():
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Invalid bundle checksum")
        if _hash(_member(bundle, name)) != expected:
            raise ValueError("Adapter bundle checksum mismatch")
    jars = sorted(name for name in files if name.startswith("lib/") and name.endswith(".jar"))
    if not jars:
        raise ValueError("Adapter bundle has no dependencies")
    # Avoid runtime wildcard discovery: execute only the verified inventory.
    classpath = os.pathsep.join([str(bundle / "classes"), *(str(bundle / name) for name in jars)])
    return (java, "-cp", classpath, "PeekyJavaAdapter"), _hash(manifest_path)


@dataclass(frozen=True)
class JavaTarget:
    bundle: Path
    sources: tuple[str, ...]
    main_class: str
    java: str = "java"
    javac: str = "javac"
    arguments: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.main_class, str) or not CLASS_NAME.fullmatch(self.main_class):
            raise ValueError("Invalid main class")
        if not isinstance(self.sources, tuple) or not self.sources:
            raise ValueError("Java sources must be a nonempty tuple")
        for source in self.sources:
            _member(Path("."), source)
            if not source.endswith(".java") or source.startswith(("-", "@")):
                raise ValueError("Expected relative .java source files")
        if (
            not isinstance(self.arguments, tuple)
            or len(self.arguments) > 100
            or any(
                not isinstance(arg, str)
                or any(c in arg for c in ("\x00", '"', "\r", "\n"))
                or len(arg) > 8192
                for arg in self.arguments
            )
        ):
            raise ValueError("Invalid Java application arguments")

    @property
    def verification_inputs(self):
        return self.sources

    @property
    def verification_sources(self):
        return self.sources

    @property
    def target_identity(self):
        return {
            "language": "java",
            "main_class": self.main_class,
            "arguments": list(self.arguments),
            "sources": sorted(self.sources),
            "compiler_options": ["-g", "-proc:none"],
        }

    def validate_breakpoints(self, points):
        PythonAdapter().validate_breakpoints(points)
        if any(set(point) != {"line"} for point in points):
            raise ValueError("Java supports unconditional line breakpoints only")

    supports_deadline = True

    def prepare(self, program, root, *, edit=None, deadline=None):
        import time

        from peeky.java_metadata import generate_source_map

        root = Path(root).resolve(strict=True)
        program = Path(program).resolve(strict=True)
        relative = program.relative_to(root).as_posix()
        if relative not in self.sources:
            raise ValueError("Program is not a declared Java source")
        bundle = Path(self.bundle).resolve(strict=True)
        java, javac = _tool(self.java), _tool(self.javac)
        command, bundle_hash = _bundle(bundle, java)
        recipe = BuildRecipe(
            language="java",
            inputs=self.sources,
            build_commands=(Command((javac, "-g", "-proc:none", "-d", "classes", *self.sources)),),
            artifacts=(ArtifactSpec("classes", "directory", "jvm-classes"),),
            toolchain_probes=(Command((java, "-version")), Command((javac, "-version"))),
        )
        prepared = prepare(recipe, root, edit=edit, deadline=deadline)
        try:
            source_paths = tuple(prepared.workspace / name for name in self.sources)
            mapping = generate_source_map(
                prepared.workspace / "classes", source_paths, prepared.workspace
            )
            if self.main_class not in mapping["classes"]:
                raise ValueError("Main class is absent from compiled source metadata")
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Java preparation exceeded its overall deadline")
            mapping_path = prepared.workspace / "peeky-source-map.json"
            mapping_path.write_text(json.dumps(mapping, sort_keys=True))
            session = JavaSession(
                self, prepared, java, bundle, command, bundle_hash, mapping, mapping_path
            )
            session.assert_current()
            return session
        except BaseException:
            prepared.cleanup()
            raise


class JavaSession:
    def __init__(self, target, prepared, java, bundle, command, bundle_hash, mapping, mapping_path):
        self.target = target
        self.prepared = prepared
        self.java = java
        self.bundle = bundle
        self._command = (*command, str(mapping_path))
        self.bundle_hash = bundle_hash
        self._mapping_data = mapping_path.read_bytes()
        self.mapping_path = mapping_path
        self.mapping_hash = _hash(mapping_path)
        self.manifest_id = hashlib.sha256(
            json.dumps(
                {
                    "build": prepared.manifest.manifest_id,
                    "adapter": bundle_hash,
                    "source_map": self.mapping_hash,
                    "main_class": target.main_class,
                    "arguments": target.arguments,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    @property
    def target_identity(self):
        return self.target.target_identity

    @property
    def source_manifest(self):
        return {item.path: item.sha256 for item in self.prepared.manifest.original_inputs}

    @property
    def mapping(self):
        return json.loads(self._mapping_data)

    @property
    def lines(self):
        """Breakpoint lines per declared source, from the prepared class line tables."""
        workspace = Path(self.prepared.workspace).resolve()
        result = {}
        for entry in self.mapping["classes"].values():
            name = Path(entry["path"]).resolve().relative_to(workspace).as_posix()
            result.setdefault(name, set()).update(entry["lines"])
        return {name: sorted(lines) for name, lines in result.items()}

    @property
    def environment(self):
        return dict(self.prepared.manifest.effective_environment)

    @property
    def verification_command(self):
        return (self.java, "-cp", str(self.prepared.workspace / "classes"), self.target.main_class)

    @property
    def command(self):
        return self._command

    def initialize_args(self):
        return {**PythonAdapter().initialize_args(), "adapterID": "java"}

    def launch_args(self, program, root):
        self.assert_current()
        self.source_path(program)
        return {
            "mainClass": self.target.main_class,
            "classPaths": [str(self.prepared.workspace / "classes")],
            "cwd": str(self.prepared.workspace),
            "javaExec": self.java,
            # JDI's launcher tokenizes double quotes; it does not use a shell.
            # Embedded quotes/newlines are rejected at configuration validation.
            "args": " ".join('"' + arg + '"' for arg in self.target.arguments),
            "console": "internalConsole",
        }

    def source_path(self, original):
        relative = (
            Path(original).resolve(strict=True).relative_to(self.prepared.source_root).as_posix()
        )
        if relative not in self.target.sources:
            raise ValueError("Source is not part of the prepared Java target")
        return self.prepared.workspace / relative

    def local_scopes(self, scopes):
        return [scope for scope in scopes if not scope.get("expensive")]

    def normalize_capabilities(self, raw):
        return {
            "supports_set_variable": raw.get("supportsSetVariable") is True,
            "supports_conditional_breakpoints": False,
            "supports_hit_conditional_breakpoints": False,
            "supports_logpoints": False,
            "supports_evaluate": False,
            "supports_attach": False,
        }

    def validate_breakpoints(self, points):
        self.target.validate_breakpoints(points)

    def validate_value(self, value):
        if not isinstance(value, str) or not SCALAR.fullmatch(value):
            raise ValueError("Java mutation requires a numeric or boolean scalar literal")

    def assert_current(self):
        assert_current(self.prepared)
        try:
            command, bundle_hash = _bundle(self.bundle, self.java)
            if (
                bundle_hash != self.bundle_hash
                or (*command, str(self.mapping_path)) != self.command
                or _hash(self.mapping_path) != self.mapping_hash
            ):
                raise ValueError("Java adapter/source map identity changed")
        except (OSError, ValueError) as exc:
            raise RuntimeError("Java runtime provenance mismatch") from exc

    def validate_stop(self, frame):
        try:
            path = Path(frame["source"]["path"]).resolve(strict=True)
            line = frame["line"]
            if type(line) is not int or line < 1:
                raise ValueError("Invalid stopped frame line")
            matched = any(
                Path(entry["path"]) == path and line in entry["lines"]
                for entry in self.mapping["classes"].values()
            )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise RuntimeError("Java stopped frame has no verified source location") from exc
        if not matched:
            raise RuntimeError("Java stopped frame does not match prepared source metadata")

    def close(self):
        self.prepared.cleanup()
