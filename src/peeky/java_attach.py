"""Attach to an already running JVM (for example a Gradle test JVM started with
`--debug-jvm`) through the experimental Java adapter.

Peeky did not build this program. Provenance is therefore labeled `attached`:
declared source files, the exact class files that map them and the adapter bundle
are hashed and rechecked before every operation, but Peeky cannot prove the JVM
loaded those bytes. Attached sessions never verify source edits (use a fresh
build for that) and detach without terminating the debuggee. Loopback only;
trusted code, not a sandbox.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from peeky.adapters import PythonAdapter
from peeky.java import JavaSession, _bundle, _hash, _tool
from peeky.java_metadata import MAX_CLASS_BYTES, _class_metadata

MAX_ATTACH_SOURCES = 64
MAX_ATTACH_CLASSES = 2048
MAX_ATTACH_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 20000
_PACKAGE = re.compile(r"^\s*package\s+([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)\s*;", re.MULTILINE)


def _inside(root, name, label):
    path = (root / name).resolve(strict=True)
    if root != path and root not in path.parents:
        raise ValueError(f"Attached {label} must stay inside the source root")
    return path


def attached_source_map(class_roots, sources, deadline=None, identities=None):
    """Map only classes whose package and SourceFile identify a declared source.

    Only each declared source's package directory is read in every class root, so
    large projects stay bounded. Ambiguous or duplicate identities fail closed.
    ``identities`` (a dict) receives the SHA-256 of exactly the bytes that were
    parsed, so a rebuild after parsing cannot become the provenance baseline.
    """
    identities = {} if identities is None else identities
    packages, counts, source_bytes = {}, {}, 0
    for source in sources:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Java attach preparation exceeded its deadline")
        if source.is_symlink() or not source.is_file():
            raise ValueError("Attached Java sources must be regular files")
        size = source.stat().st_size
        source_bytes += size
        if size > MAX_CLASS_BYTES or source_bytes > MAX_ATTACH_BYTES:
            raise ValueError("Attached Java sources exceed their size limit")
        raw = source.read_bytes()
        identities[str(source)] = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8")
        match = _PACKAGE.search(text[:65536])
        package = match[1] if match else ""
        key = (package, source.name)
        if key in packages:
            raise ValueError("Two declared sources share a package and file name")
        packages[key] = source
        counts[source] = len(text.splitlines())
    classes, total = {}, 0
    for class_root in class_roots:
        for package in sorted({package for package, _ in packages}):
            directory = class_root.joinpath(*package.split(".")) if package else class_root
            if not directory.is_dir():
                continue
            entries, scanned = [], 0
            with os.scandir(directory) as listing:
                for entry in listing:
                    scanned += 1
                    if scanned > MAX_PACKAGE_ENTRIES:
                        raise ValueError("Package directory has too many entries to attach")
                    if deadline is not None and scanned % 256 == 0 and time.monotonic() >= deadline:
                        raise TimeoutError("Java attach preparation exceeded its deadline")
                    if entry.name.endswith(".class"):
                        entries.append(Path(entry.path))
            for path in sorted(entries):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("Java attach preparation exceeded its deadline")
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Class files must be regular files")
                size = path.stat().st_size
                total += size
                if size > MAX_CLASS_BYTES or total > MAX_ATTACH_BYTES:
                    raise ValueError("Attached class metadata exceeds its size limit")
                data = path.read_bytes()
                shared = {}
                internal, source_name, lines = _class_metadata(data, shared)
                if path.relative_to(class_root).as_posix() != internal + ".class":
                    raise ValueError("Class file path does not match binary identity")
                source = packages.get((package, source_name))
                if source is None:
                    continue
                binary = internal.replace("/", ".")
                if binary in classes:
                    raise ValueError("Duplicate binary class identity across class roots")
                if lines and lines[-1] > counts[source]:
                    raise ValueError("Class lines exceed the declared source; rebuild or resync")
                classes[binary] = {
                    "path": str(source),
                    "lines": lines,
                    "class_file": str(path),
                    **(
                        {"shared_lines": {str(k): v for k, v in sorted(shared.items())}}
                        if shared
                        else {}
                    ),
                }
                identities[str(path)] = hashlib.sha256(data).hexdigest()
                if len(classes) > MAX_ATTACH_CLASSES:
                    raise ValueError("Too many attached classes")
    if not classes:
        raise ValueError("No compiled classes match the declared sources; build them first")
    return {"classes": dict(sorted(classes.items()))}


@dataclass(frozen=True)
class JavaAttachTarget:
    bundle: Path
    port: int
    sources: tuple[str, ...]
    class_roots: tuple[str, ...]
    java: str = "java"
    host: str = "127.0.0.1"
    timeout_ms: int = 10000

    supports_deadline = True
    language = "java"

    def __post_init__(self):
        if self.host not in ("127.0.0.1", "localhost"):
            raise ValueError("Java attach is loopback-only; tunnel remote JVMs over SSH")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Java attach port must be 1..65535")
        # The worker waits at most 15 s for the adapter to attach.
        if type(self.timeout_ms) is not int or not 1000 <= self.timeout_ms <= 15000:
            raise ValueError("Java attach timeout must be 1000..15000 ms")
        for field, limit in ((self.sources, MAX_ATTACH_SOURCES), (self.class_roots, 32)):
            if not isinstance(field, tuple) or not 0 < len(field) <= limit:
                raise ValueError("Declare bounded nonempty sources and class roots")
        for name in (*self.sources, *self.class_roots):
            if (
                not isinstance(name, str)
                or not name
                or name.startswith("/")
                or ".." in name.split("/")
            ):
                raise ValueError("Attach paths must be relative to the source root")
        if any(not name.endswith(".java") for name in self.sources):
            raise ValueError("Attach sources must be .java files")

    @property
    def target_identity(self):
        return {
            "language": "java",
            "mode": "attach",
            "host": self.host,
            "port": self.port,
            "sources": sorted(self.sources),
            "class_roots": sorted(self.class_roots),
        }

    def validate_breakpoints(self, points):
        PythonAdapter().validate_breakpoints(points)
        if any(set(point) != {"line"} for point in points):
            raise ValueError("Java supports unconditional line breakpoints only")

    def prepare(self, program, root, *, edit=None, deadline=None):
        if edit is not None:
            raise ValueError("Attached JVMs cannot verify edits; verify with a fresh build")
        root = Path(root).resolve(strict=True)
        relative = Path(program).resolve(strict=True).relative_to(root).as_posix()
        if relative not in self.sources:
            raise ValueError("Program is not a declared attached source")
        sources = [_inside(root, name, "source") for name in self.sources]
        class_roots = [_inside(root, name, "class root") for name in self.class_roots]
        java = _tool(self.java)
        bundle = Path(self.bundle).resolve(strict=True)
        command, bundle_hash = _bundle(bundle, java)
        parsed = {}
        mapping = attached_source_map(class_roots, sources, deadline, parsed)
        workspace = Path(tempfile.mkdtemp(prefix="peeky-java-attach-"))
        try:
            path = workspace / "peeky-source-map.json"
            path.write_text(json.dumps(mapping, sort_keys=True))
            return JavaAttachSession(
                self, root, java, bundle, command, bundle_hash, path, workspace, parsed
            )
        except BaseException:
            shutil.rmtree(workspace, ignore_errors=True)
            raise


class JavaAttachSession(JavaSession):
    """Java runtime contract over an attached JVM; reuses JavaSession inspection rules."""

    # Attached JVMs report their own death through the closed JDWP connection; the
    # dropped exit event (#27) was seen only on launch, and a false "terminated"
    # would detach a live JVM.
    liveness_by_threads = False
    # Their `stopped` event can be lost instead (#27): the worker detects a thread left
    # suspended at a verified session breakpoint and fails fast (find_lost_stop).
    detects_lost_stops = True

    dap_request = "attach"
    terminate_debuggee = False
    provenance = "attached"

    def __init__(
        self, target, root, java, bundle, command, bundle_hash, mapping_path, workspace, parsed
    ):
        self.target, self.root, self.java, self.bundle = target, root, java, bundle
        self.workspace = workspace
        self._command = (*command, str(mapping_path))
        self.bundle_hash = bundle_hash
        self._mapping_data = mapping_path.read_bytes()
        self.mapping_path = mapping_path
        self.mapping_hash = _hash(mapping_path)
        # The baseline is the bytes the line tables came from, not a later re-read.
        self._identities = {name: parsed[name] for name in self._identity_files()}
        if self._current_identities() != self._identities:
            raise RuntimeError("Attached sources or classes changed during attach; re-attach")
        self.manifest_id = hashlib.sha256(
            json.dumps(
                {
                    "provenance": "attached",
                    "adapter": bundle_hash,
                    "source_map": self.mapping_hash,
                    "files": self._identities,
                    "target": target.target_identity,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def _identity_files(self):
        # Resolved, as preparation reads them (a declared path may cross an in-root link).
        files = {str((self.root / name).resolve()) for name in self.target.sources}
        files |= {entry["class_file"] for entry in self.mapping["classes"].values()}
        return sorted(files)

    def _current_identities(self):
        return {name: _hash(Path(name)) for name in self._identity_files()}

    @property
    def lines(self):
        result = {}
        for entry in self.mapping["classes"].values():
            name = Path(entry["path"]).relative_to(self.root).as_posix()
            result.setdefault(name, set()).update(entry["lines"])
        return {name: sorted(lines) for name, lines in result.items()}

    @property
    def source_manifest(self):
        return {name: _hash(self.root / name) for name in self.target.sources}

    def function_scopes(self):
        """As for a launched JVM, from the attached classes' own files (`class_file`)."""
        from peeky.java_metadata import java_scopes

        self.assert_current()
        return java_scopes(self.mapping, self.root, self.root)

    def declaration_roots(self):
        """(classes directory, workspace): an attached JVM's classes are under its root."""
        return self.root, self.root

    @property
    def environment(self):
        return None

    @property
    def verification_command(self):
        raise ValueError("Attached JVMs cannot run fresh verification")

    def launch_args(self, program, root):
        self.assert_current()
        self.source_path(program)
        return {
            "hostName": self.target.host,
            "port": self.target.port,
            "timeout": self.target.timeout_ms,
        }

    def source_path(self, original):
        relative = Path(original).resolve(strict=True).relative_to(self.root).as_posix()
        if relative not in self.target.sources:
            raise ValueError("Source is not part of the attached Java target")
        return self.root / relative

    def normalize_capabilities(self, raw):
        return {**super().normalize_capabilities(raw), "supports_attach": True}

    def _line_key(self, program):
        return Path(program).resolve(strict=True).relative_to(self.root).as_posix()

    def assert_current(self):
        try:
            command, bundle_hash = _bundle(self.bundle, self.java)
            unchanged = (
                bundle_hash == self.bundle_hash
                and (*command, str(self.mapping_path)) == self.command
                and _hash(self.mapping_path) == self.mapping_hash
                and self._current_identities() == self._identities
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError("Attached Java provenance mismatch") from exc
        if not unchanged:
            raise RuntimeError("Attached sources, classes or adapter changed; re-attach")

    def close(self):
        shutil.rmtree(self.workspace, ignore_errors=True)
