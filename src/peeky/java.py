"""Trusted standalone Java targets built freshly before debugger launch.

The source map comes from compiler metadata. Filesystem integrity checks bind
launch inputs and stop locations; they are not proof against malicious native
code or concurrent replacement of an already loaded JVM image.
"""

import functools
import hashlib
import json
import math
import os
import re
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from peeky.adapters import PythonAdapter
from peeky.build import ArtifactSpec, BuildRecipe, Command, assert_current, prepare
from peeky.digest import sha256_file, tree_fingerprint

MAX_BUNDLE_FILES = 4096
MAX_BUNDLE_FILE_BYTES = 256 * 1024 * 1024
_JAVA_INTEGER_BITS = {"byte": 8, "short": 16, "int": 32, "long": 64}
# java-debug's values for an object (`Merge$Interval@143`) and an array (`int[3]@144`).
_JAVA_OBJECT = re.compile(r"[A-Za-z_$][\w$.]*@[0-9]+")
_JAVA_ARRAY = re.compile(r"[A-Za-z_$][\w$.]*(?:\[\])*\[([0-9]{1,10})\](?:\[\])*@[0-9]+")
_JAVA_LIST = re.compile(r"(?:java\.util\.)?ArrayList(?:<.*>)?")
PREVIEW_ITEMS = 8


def java_list(variable, fields):
    """(backing array, size) of a java.util.ArrayList shown by its own fields, else None:
    its elements are `elementData[0]` to `elementData[size - 1]`, read without calling
    `toArray` or `get` in the program."""
    if not _JAVA_LIST.fullmatch(str(variable.get("type"))):
        return None
    named = {field.get("name"): field for field in fields}
    if set(named) != {"elementData", "modCount", "size"} or len(fields) != 3:
        return None
    size, backing = named["size"], named["elementData"]
    if size.get("type") != "int" or not re.fullmatch(r"0|[1-9][0-9]{0,9}", str(size.get("value"))):
        return None
    reference = backing.get("variablesReference")
    if type(reference) is not int or reference <= 0:
        return None
    return backing, int(size["value"])


def _brief(item):
    """A field's value, short. java-debug writes a String's text between quotes without
    escaping it; here it is a Java literal (`"say \\"hi\\""`), cut inside its quotes,
    so a preview's strings never run into the fields after them."""
    text = str(item.get("value"))
    if text.startswith('"'):
        inner = text[1:-1] if len(text) > 1 and text.endswith('"') else text[1:]
        inner = inner.replace("\\", "\\\\").replace('"', '\\"')
        return '"' + (inner if len(inner) <= 38 else inner[:37] + "…") + '"'
    return text if len(text) <= 40 else text[:39] + "…"


def _elements(children, length):
    """`[a, b, …]`: the first elements of a length-``length`` sequence, in index order."""
    found = {}
    for child in children:
        # java-debug names array elements `0`, `1`, ...
        match = re.fullmatch(r"\[?([0-9]+)\]?", str(child.get("name")))
        if match and int(match[1]) < length:
            found[int(match[1])] = child
    shown = [_brief(found[index]) for index in range(min(length, PREVIEW_ITEMS)) if index in found]
    if len(shown) != min(length, PREVIEW_ITEMS):
        raise ValueError("Java elements are not all visible")
    return f"[{', '.join(shown)}{', …' if length > PREVIEW_ITEMS else ''}]"


def java_preview(variable, fetch):
    """A captured value with its first fields inline (`Merge$Interval@143 {end=10,
    start=1}`), an ArrayList's elements (`ArrayList@142 size=2 [...]`) or an array's
    (`int[3]@144 [1, 2, 3]`), from the debugger's field reads only: no code runs in the
    program. ``fetch(handle, count)`` lists a value's first children. None leaves the
    value as java-debug shows it."""
    value, handle = variable.get("value"), variable.get("variablesReference")
    if not isinstance(value, str) or type(handle) is not int or handle <= 0:
        return None
    if array := _JAVA_ARRAY.fullmatch(value):
        length = int(array[1])
        elements = fetch(handle, min(length, PREVIEW_ITEMS)) if length else []
        return f"{value} {_elements(elements, length)}"
    if not _JAVA_OBJECT.fullmatch(value):
        return None
    fields = fetch(handle, 64)
    if listed := java_list(variable, fields):
        backing, size = listed
        elements = fetch(backing["variablesReference"], min(size, PREVIEW_ITEMS)) if size else []
        return f"{value} size={size} {_elements(elements, size)}"
    if not fields or not all(isinstance(field.get("name"), str) for field in fields):
        return None
    shown = ", ".join(f"{field['name']}={_brief(field)}" for field in fields[:PREVIEW_ITEMS])
    return f"{value} {{{shown}{', …' if len(fields) > PREVIEW_ITEMS else ''}}}"


SCALAR = re.compile(r"(?:true|false|[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\Z")
CLASS_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*\Z")


def _hash(path):
    if not path.is_file() or path.stat().st_size > MAX_BUNDLE_FILE_BYTES:
        raise ValueError("Expected a bounded regular bundle file")
    return sha256_file(path)


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


_BUNDLES = {}


def _bundle(bundle, java):
    """Verified adapter command and manifest hash; rechecked before every operation.

    A full verification walks and hashes every bundle file. While a stat fingerprint
    of the whole tree is unchanged, the earlier verified result is reused (see
    peeky.digest); any added, removed, replaced or rewritten entry verifies again.
    """
    key = (str(bundle), java)
    fingerprint = tree_fingerprint(bundle, MAX_BUNDLE_FILES * 4)
    cached = _BUNDLES.get(key)
    if fingerprint is not None and cached is not None and cached[0] == fingerprint:
        return cached[1]
    result = _verify_bundle(bundle, java)
    if fingerprint is not None and tree_fingerprint(bundle, MAX_BUNDLE_FILES * 4) == fingerprint:
        _BUNDLES[key] = (fingerprint, result)
    return result


def _verify_bundle(bundle, java):
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
        self.validate_arguments(self.arguments)

    accepts_arguments = True

    @staticmethod
    def validate_arguments(arguments):
        # JDI's launcher tokenizes double quotes; it does not use a shell.
        if (
            not isinstance(arguments, tuple)
            or len(arguments) > 100
            or any(
                not isinstance(arg, str)
                or any(c in arg for c in ("\x00", '"', "\r", "\n"))
                or len(arg) > 8192
                for arg in arguments
            )
        ):
            raise ValueError(
                "Invalid Java application arguments (no quotes, newlines or NUL; at most 100)"
            )

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
    # Candidate probes stage an edit into a fresh build (the worker's launch `edit`).
    supports_edits = True

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


def _method_shown(name):
    """A class-file method name as people read it."""
    if name.startswith("<clinit>"):
        return "the static initializer"
    if name.startswith("<init>"):
        return f"the constructor{name[6:]}" if name != "<init>" else "the constructor"
    return name


class JavaSession:
    # java-debug accepts variables start/count only for arrays.
    ranges_only_for_indexed = True
    # Selectors name fields of `this` without it, as Java code does.
    implicit_receiver = "this"
    # java-debug occasionally drops VM-death events (#27); infer exit from no threads.
    liveness_by_threads = True

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

    @functools.cached_property
    def mapping(self):
        # Parsed once: the bytes are fixed at prepare and rechecked by hash.
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

    def breakpoint_notes(self, program, breakpoints):
        """Warnings for breakpoint lines that several methods share: java-debug stops in
        the first one only (class-file order), never in the others."""
        path = Path(self.source_path(program)).resolve()
        notes = []
        for point in breakpoints:
            line = point.get("line")
            for entry in self.mapping["classes"].values():
                if Path(entry["path"]).resolve() != path:
                    continue
                if not (shared := entry.get("shared_lines", {}).get(str(line))):
                    continue
                never = ", ".join(_method_shown(name) for name in shared["never"][:3])
                notes.append(
                    f"Line {line} runs in several methods; the debugger stops only in "
                    f"{_method_shown(shared['stops'])} there, never in {never}. To stop in "
                    "one of those, put the breakpoint on a line of its own, or `session step "
                    "--into` from this line (a method reference has no line of its own)"
                )
        return notes

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

    def launch_args(self, program, root, args=None):
        self.assert_current()
        self.source_path(program)
        arguments = self.target.arguments if args is None else tuple(args)
        return {
            "mainClass": self.target.main_class,
            "classPaths": [str(self.prepared.workspace / "classes")],
            "cwd": str(self.prepared.workspace),
            "javaExec": self.java,
            # JDI's launcher tokenizes double quotes; it does not use a shell.
            # Embedded quotes/newlines are rejected at configuration validation.
            "args": " ".join('"' + arg + '"' for arg in arguments),
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
            # Launcher bundles built before stepping lack the marker: refuse cleanly.
            "supports_stepping": raw.get("peekySteps") is True,
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

    def settable(self, variable, frame=None):
        """Whether `--set` can change this root local: a scalar of a type it accepts, named
        as a selector can name it (not java-debug's `->compute()` return value entry)."""
        return bool(
            variable.get("type") in (*_JAVA_INTEGER_BITS, "boolean", "float", "double")
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(variable.get("name")))
        )

    def validate_variable(self, variable, value):
        """The value must fit the local's declared type exactly.

        java-debug narrows integers, keeps one character of a char and parses any
        non-"true" boolean as false, then reports success; refuse those instead.
        """
        self.validate_value(value)
        name, kind = variable.get("name"), variable.get("type")
        if kind in _JAVA_INTEGER_BITS:
            # java-debug parses with Long.decode: a leading 0 means octal.
            if not re.fullmatch(r"[+-]?(?:0|[1-9][0-9]{0,19})", value):
                raise ValueError(f"Java mutation requires an integer literal for {kind} {name}")
            bits = _JAVA_INTEGER_BITS[kind]
            if not -(2 ** (bits - 1)) <= int(value) < 2 ** (bits - 1):
                raise ValueError(f"Java mutation is outside the {kind} range for {name}")
        elif kind == "boolean":
            if value not in ("true", "false"):
                raise ValueError(f"Java mutation requires true or false for boolean {name}")
        elif kind in ("float", "double"):
            if value in ("true", "false"):
                raise ValueError(f"Java mutation requires a number for {kind} {name}")
            number = float(value)
            try:
                stored = (
                    struct.unpack(">f", struct.pack(">f", number))[0] if kind == "float" else number
                )
            except OverflowError:
                stored = math.inf
            mantissa = value.lower().split("e")[0]
            if not math.isfinite(stored) or (stored == 0 and re.search(r"[1-9]", mantissa)):
                raise ValueError(f"Java mutation is outside the {kind} range for {name}")
        else:
            raise ValueError(
                "Only byte, short, int, long, boolean, float and double locals support Java "
                f"mutation; {name} is {kind}"
            )

    def _line_key(self, program):
        return Path(program).resolve(strict=True).relative_to(self.prepared.source_root).as_posix()

    def check_breakpoint_lines(self, program, points):
        """Refuse lines without code before the adapter does (it fails the whole launch)."""
        key = self._line_key(program)
        lines = self.lines.get(key, [])
        for point in points:
            if point.get("line") not in lines:
                nearby = sorted(lines, key=lambda line: (abs(line - point.get("line", 0)), line))
                raise ValueError(
                    f"Line {point.get('line')} of {key} has no code to stop at; executable lines "
                    f"nearby: {', '.join(map(str, sorted(nearby[:6]))) or 'none'} (see `peeky "
                    "describe`)"
                )

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
