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
from peeky.identifiers import is_name
from peeky.java_metadata import loop_header_stops
from peeky.loop_headers import loop_header_note
from peeky.text_sets import parse_text

MAX_BUNDLE_FILES = 4096
MAX_BUNDLE_FILE_BYTES = 256 * 1024 * 1024
_JAVA_INTEGER_BITS = {"byte": 8, "short": 16, "int": 32, "long": 64}
# java-debug's values for an object (`Merge$Interval@143`) and an array (`int[3]@144`).
_JAVA_OBJECT = re.compile(r"[A-Za-z_$][\w$.]*@[0-9]+")
JAVA_ARRAY = re.compile(r"[A-Za-z_$][\w$.]*(?:\[\])*\[([0-9]{1,10})\](?:\[\])*@[0-9]+")
JAVA_LIST = re.compile(r"(?:java\.util\.)?ArrayList(?:<.*>)?")
_NUMBERED = re.compile(r"(.*?)(@[0-9]+)", re.S)
PREVIEW_ITEMS = 8


def java_list(variable, fields):
    """(backing array, size) of a java.util.ArrayList shown by its own fields, else None:
    its elements are `elementData[0]` to `elementData[size - 1]`, read without calling
    `toArray` or `get` in the program."""
    if not JAVA_LIST.fullmatch(str(variable.get("type"))):
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


_ESCAPES = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


def _escaped(text, quote):
    return "".join(
        "\\" + quote
        if char == quote
        else _ESCAPES.get(char)
        or (
            f"\\u{ord(char):04x}"
            if ord(char) < 0x20 or 0x7F <= ord(char) < 0xA0 or 0xD800 <= ord(char) <= 0xDFFF
            else char
        )
        for char in text
    )


def _brief(item):
    """A field's value, short. java-debug writes a String's text between quotes, and a
    char bare, without escaping either; here both are Java literals (`"say \\"hi\\""`,
    `'"'`), a String cut inside its quotes, so a preview's quoted text never runs into
    the fields after it."""
    text = str(item.get("value"))
    if item.get("type") == "char" and len(text) == 1:
        return "'" + _escaped(text, "'") + "'"
    if text.startswith('"'):
        inner = text[1:-1] if len(text) > 1 and text.endswith('"') else text[1:]
        return '"' + _escaped(inner if len(inner) <= 38 else inner[:37] + "…", '"') + '"'
    if numbered := _NUMBERED.fullmatch(text):
        # Never cut an object's number: whether a value is cut must not depend on it,
        # or the same object would differ across runs by more than its number.
        prefix, number = numbered.groups()
        return (prefix if len(prefix) <= 39 else prefix[:38] + "…") + number
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
    if array := JAVA_ARRAY.fullmatch(value):
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


# A String (#137): java-debug makes a new one of a text, and the variable refers to it.
_STRING_DESCRIPTOR = "Ljava/lang/String;"
_JAVA_STRINGS = ("String", "java.lang.String")
_JAVA_SETTABLE = ("boolean", "float", "double", *_JAVA_STRINGS)


def _settable_kinds(session):
    """The non-integer types `--set` changes: a String only where the launcher allows it."""
    return _JAVA_SETTABLE if getattr(session, "text_set", False) else _JAVA_SETTABLE[:3]


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
# Patches scripts/setup_java_adapter.py applies to java-debug (also listed, for people, in
# the bundle's adapter.json), each proven by the compiled class it changes: the class and
# bytes only the patched code has. eventhub-log-guard (#27): without it java-debug's
# event thread can die on a finished thread's name, and the adapter reports no stop,
# step or exit ever again; the patched EventHub calls Logger.isLoggable, the old one not.
JAVA_DEBUG_PATCHES = {
    "eventhub-log-guard": ("classes/com/microsoft/java/debug/core/EventHub.class", b"isLoggable")
}
STALE_BUNDLE = (
    "The Java adapter bundle at {bundle} predates Peeky's java-debug fixes (missing: "
    "{missing}; without eventhub-log-guard java-debug can stop reporting every stop and "
    "exit, issue #27). Rebuild it: python scripts/setup_java_adapter.py --output <a new "
    "directory>, then point the target's bundle (or PEEKY_JAVA_ADAPTER) at it"
)


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
    # The verified class files themselves, not the manifest's word for it.
    missing = [
        name
        for name, (member, marker) in sorted(JAVA_DEBUG_PATCHES.items())
        if member not in files or marker not in _member(bundle, member).read_bytes()
    ]
    if missing:
        raise ValueError(STALE_BUNDLE.format(bundle=bundle, missing=", ".join(missing)))
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
    # java-debug's "Event Hub" thread turns every JDI event into DAP stops, steps and
    # exits. If it dies (an unpatched bundle, #27), or an exception escapes one of its
    # handlers, nothing is ever reported again: end the session at once.
    fatal_stderr = {
        r'Exception in thread "Event Hub"': (
            "java-debug's event thread died ({excerpt}); it reports no more stops, steps or "
            "exits, so the session cannot continue: close it and retry"
        )
    }

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

    def function_scopes(self):
        """Each method's line span and the names it can see, for `describe`."""
        from peeky.java_metadata import java_scopes

        self.assert_current()
        workspace = Path(self.prepared.workspace)
        return java_scopes(self.mapping, workspace / "classes", workspace)

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
            # A loop header bound before its loop stops once per loop, not per pass (#161).
            prepared = getattr(getattr(self, "prepared", None), "workspace", None)
            try:
                classes = Path(prepared) / "classes" if prepared else None
                count = loop_header_stops(self.mapping, classes, path, line)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                count = None
            if count:
                notes.append(loop_header_note(line, *count))
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

    @staticmethod
    def return_entry(variable):
        """The method's name where java-debug lists what the method a step left returned
        (`⎯►name()` on macOS and Windows, `->name()` elsewhere), else None (#155). JDI
        reports the value itself, exactly, whatever its type."""
        found = re.fullmatch(r"(?:\u23af\u25ba|->)([^\s()]+)\(\)", str(variable.get("name") or ""))
        return found[1] if found else None

    # JDI gives no value for a method that returns nothing or ends by an exception, and
    # java-debug does not say which.
    no_return = (
        "java-debug listed no returned value: the method returns nothing, or it ended by "
        "an exception"
    )

    def local_scopes(self, scopes):
        return [scope for scope in scopes if not scope.get("expensive")]

    def normalize_capabilities(self, raw):
        if self is not None:
            # A String takes a text only through a launcher that allows it (#137).
            self.text_set = raw.get("peekyTextSet") is True
        return {
            "supports_set_variable": raw.get("supportsSetVariable") is True,
            # Launcher bundles built before stepping lack the marker: refuse cleanly.
            "supports_stepping": raw.get("peekySteps") is True,
            # Bundles whose launcher refuses a field name a hiding static field shares.
            "supports_member_set": raw.get("peekyFieldSet") is True,
            # Bundles whose launcher changes a String to a text (#137).
            "supports_text_set": raw.get("peekyTextSet") is True,
            "supports_conditional_breakpoints": False,
            "supports_hit_conditional_breakpoints": False,
            "supports_logpoints": False,
            "supports_evaluate": False,
            "supports_attach": False,
        }

    def validate_breakpoints(self, points):
        self.target.validate_breakpoints(points)

    def validate_value(self, value):
        if isinstance(value, str) and parse_text(value) is not None:
            return
        if not isinstance(value, str) or not SCALAR.fullmatch(value):
            raise ValueError(
                "Java mutation requires a numeric or boolean scalar literal, or text in "
                "double quotes for a String"
            )

    def mutation_literal(self, value):
        """What java-debug is sent: a text (#137) between plain quotes, which it takes
        off and keeps everything between as the new String (it unescapes nothing)."""
        text = parse_text(value)
        return value if text is None else f'"{text}"'

    def confirm_mutation(self, name, value, before, after):
        """A String (#137) must read back as the text asked, as java-debug shows one
        (between quotes, nothing escaped): else the change is not reported."""
        text = parse_text(value)
        if text is not None and after != f'"{text}"':
            raise RuntimeError(
                f"java-debug stored {str(after)[:80]} in {name!r}, not the text asked: the "
                "change did not reach the program as stated, so the session is closed"
            )

    def settable(self, variable, frame=None):
        """Whether `--set` can change this root local: a scalar of a type it accepts, named
        as a selector can name it (not java-debug's `->compute()` return value entry),
        that the program reads (not a constant javac folds into its uses, #164); a
        String only where the class file declares it one (java-debug names the type of
        the value it holds: `Object o = "x"` shows `String`)."""
        name, kind = str(variable.get("name")), variable.get("type")
        if not (kind in (*_JAVA_INTEGER_BITS, *_settable_kinds(self)) and is_name(name)):
            return False
        declared = self.declared_local(frame, name) if frame else None
        if declared and not all(read for _, read in declared):
            return False
        if kind in _JAVA_STRINGS:
            return bool(declared) and all(each == _STRING_DESCRIPTOR for each, _ in declared)
        return True

    def validate_mutation_target(self, variable, frame):
        """Refuse a local the program never reads (#164): javac folds a constant local
        (`final` with a constant initializer: `final int k = 5;`, `final String s =
        "x";`) into the code that uses it, so a change to it would never be seen."""
        name = str(variable.get("name"))
        declared = self.declared_local(frame, name) if frame else None
        if declared and not all(read for _, read in declared):
            raise ValueError(
                f"{name!r} cannot be changed: the program never reads it (javac compiles a "
                "constant local, one declared `final` with a constant initializer, into the "
                "code that uses it), so a change would never be seen. Change what the code "
                "computes from it with a candidate edit (`--old`/`--new`)"
            )

    def _declared(self):
        """The prepared classes' declarations (java_metadata.java_declarations), once;
        {} where they cannot be read (nothing is then known of declared types)."""
        found = getattr(self, "_declarations", None)
        if found is None:
            from peeky.java_metadata import java_declarations

            try:
                classes, workspace = self.declaration_roots()
                found = java_declarations(self.mapping, classes, workspace)
            except (AttributeError, OSError, ValueError, KeyError):
                found = {}
            self._declarations = found
        return found

    def declaration_roots(self):
        """(classes directory, workspace) of the prepared classes, as `describe` reads
        them."""
        workspace = Path(self.prepared.workspace)
        return workspace / "classes", workspace

    def declared_local(self, frame, name):
        """[(descriptor, whether the code reads it)] for each declaration of the local
        ``name`` the stopped frame's method has in scope on its line, from the class
        files (#164); None where the method is not found."""
        found = self._frame_method(frame)
        if found is None:
            return None
        method, line = found
        return [
            (descriptor, read)
            for local, descriptor, lines, read in method["locals"]
            if local == name and line in lines
        ]

    def read_after(self, variable, frame, stop):
        """Whether the program may read the root local ``variable`` after this stop
        (#164): False where no path from where the stop's line begins reads its slot
        before storing into it again, so a change would never be seen (a lambda or
        inner class captured its value before; a constant javac compiled into its
        uses); True where one may; None where that cannot be told (another kind of
        stop, a method or code not understood)."""
        from peeky.java_metadata import reads_after

        found = self._frame_method(frame) if stop in ("breakpoint", "step") else None
        if found is None:
            return None
        method, line = found
        name = str(variable.get("name"))
        verdicts = [
            reads_after(method, slot, method["line_pcs"].get(line, ()))
            for (local, _, lines, _), (_, slot, _, _) in zip(method["locals"], method["slots"])
            if local == name and line in lines
        ]
        if not verdicts or None in verdicts:
            return None
        return any(verdicts)

    def _frame_method(self, frame):
        """(the class files' record of the stopped frame's method, its line), or None
        where it is not found (#164)."""
        try:
            path = Path(frame["source"]["path"]).resolve()
            workspace = Path(self.declaration_roots()[1]).resolve()
            source = (
                path.relative_to(workspace).as_posix()
                if path.is_relative_to(workspace)
                else path.relative_to(Path(self.prepared.source_root).resolve()).as_posix()
            )
            line = frame["line"]
            method = str(frame["name"]).split("(", 1)[0].rsplit(".", 1)[-1]
        except (AttributeError, KeyError, TypeError, ValueError, OSError):
            return None
        methods = [
            each
            for declarations in self._declared().values()
            if declarations.get("source") == source
            for each in declarations.get("methods", ())
            if each["name"] == method and each["first"] <= line <= each["last"]
        ]
        if len(methods) != 1:
            return None
        return methods[0], line

    def declared_member(self, parent, name):
        """The descriptor a field or element is declared with (#164): an array's
        component type, from its own type as java-debug names it (`String[]`); a field's
        from the class files of the object's class and its superclasses. None where that
        is not known."""
        parent = str(parent or "")
        if parent.endswith("[]"):
            element = parent[:-2]
            return _STRING_DESCRIPTOR if element in _JAVA_STRINGS else f"L{element};"
        declared = self._declared()
        found = [
            internal
            for internal in declared
            if internal == parent.replace(".", "/") or internal.rsplit("/", 1)[-1] == parent
        ]
        current = found[0] if len(found) == 1 else None
        for _ in range(64):
            if current is None or current not in declared:
                return None
            fields = declared[current].get("fields", {})
            if name in fields:
                return fields[name]
            current = declared[current].get("super")
        return None

    def member_settable(self, variable, parent=None, proven=False):
        """None if `--set` can change this field or array element, else why not: a scalar
        of a type it accepts, an instance field (not final or static) or an element. A
        String only where it is declared one (the field's or the array's type, #164;
        ``parent``: the type of what holds it, as java-debug names it); ``proven``, for
        its `[settable]` mark: only where that is known."""
        name, kind = str(variable.get("name")), variable.get("type")
        hint = variable.get("presentationHint")
        attributes = hint.get("attributes") if isinstance(hint, dict) else None
        attributes = attributes if isinstance(attributes, list) else []
        if not (is_name(name) or re.fullmatch(r"0|[1-9][0-9]{0,9}", name)):
            return "no selector names it"
        if "readOnly" in attributes:
            return "it is final"
        if "static" in attributes:
            return "it is static (shared by every instance)"
        if kind not in (*_JAVA_INTEGER_BITS, *_settable_kinds(self)):
            strings = getattr(self, "text_set", False)
            return (
                f"it is {kind}; only byte, short, int, long, boolean, float"
                + (", double and String" if strings else " and double")
                + " values can be changed"
            )
        if kind in _JAVA_STRINGS:
            declared = self.declared_member(parent, name)
            if declared is not None and declared != _STRING_DESCRIPTOR:
                shown = declared[1:-1].replace("/", ".") if declared.startswith("L") else declared
                return (
                    f"it is declared {shown} and holds a String; a text changes only a "
                    "variable declared String"
                )
            if declared is None and proven:
                return "Peeky cannot tell whether it is declared a String"
        return None

    def validate_variable(self, variable, value):
        """The value must fit the local's declared type exactly.

        java-debug narrows integers, keeps one character of a char and parses any
        non-"true" boolean as false, then reports success; refuse those instead.
        """
        self.validate_value(value)
        name, kind = variable.get("name"), variable.get("type")
        text = parse_text(value)
        if kind in _JAVA_STRINGS:
            if text is None:
                raise ValueError(
                    f"Java mutation requires text in double quotes for String {name} "
                    f"(--set '{name}=\"apple\"'): the local then refers to a new String; "
                    "other references keep the old one"
                )
            return
        if text is not None:
            raise ValueError(
                f"Java mutation requires a literal of {name}'s type ({kind}); text in double "
                "quotes changes a String"
            )
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
                "Only byte, short, int, long, boolean, float, double and String locals support "
                f"Java mutation; {name} is {kind}"
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
