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
from peeky.dap import RequestFailed
from peeky.digest import sha256_file, tree_fingerprint
from peeky.identifiers import is_name
from peeky.java_metadata import descriptor_name, loop_header_stops
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


def _settable_kinds(session):
    """The non-integer types `--set` changes: a char and a String only where the
    launcher allows them (#183, #137)."""
    kinds = ("boolean", "float", "double")
    if getattr(session, "char_set", False):
        kinds += ("char",)
    if getattr(session, "text_set", False):
        kinds += _JAVA_STRINGS
    return kinds


# A JDK class's fields hold state other code relies on (a String's cached hash must match
# its text in every HashMap holding it, an ArrayList's size its elements), so Peeky never
# changes one (#204). A JDK class is one the JDK's own class loaders loaded (as the
# launcher reports it), or, judged from the class files, any class not the program's.
_JDK_WHY = (
    "Peeky never changes a JDK class's fields (a String's cached hash, an ArrayList's size): "
    "other code relies on them, so a change could corrupt the program's state behind its back"
)
# The JDK fields whose array holds the program's own values as its elements (#204): the
# backing arrays of ArrayList, Vector and ArrayDeque, and Arrays.asList's (the caller's own
# array); as the launcher's USER_ELEMENTS.
_USER_ELEMENTS = frozenset(
    {
        "java.util.ArrayList.elementData",
        "java.util.Vector.elementData",
        "java.util.ArrayDeque.elements",
        "java.util.Arrays$ArrayList.a",
        "java.util.Arrays$ArrayItr.a",
    }
)
# Values shown whole, their fields listed only when selected by name (#204): a String
# (java-debug shows its text) and the boxed primitives (`Integer@57 {value=1001}`).
_BOXED = ("Integer", "Long", "Short", "Byte", "Character", "Boolean", "Float", "Double")


def _may_be(declared, name, holder):
    """Whether the program's class ``name`` may be a ``holder`` (#204): ``holder`` is
    among the supertypes its class files list, or, for a ``holder`` not of the program,
    one of them is a JDK type whose own supertypes Peeky does not read
    (java.lang.Object's excepted)."""
    seen, stack, unread = set(), [name], False
    while stack:
        each = stack.pop()
        if each is None or each in seen:
            continue
        seen.add(each)
        if each == holder:
            return True
        if each not in declared:
            unread = unread or each != "java/lang/Object"
            continue
        stack.append(declared[each].get("super"))
        stack.extend(declared[each].get("interfaces", ()))
    return unread and holder not in declared


def _may_declare(declared, holder, field):
    """Whether a class of the program that may be a ``holder`` declares, or inherits
    from a class of the program, a field named ``field`` (#204)."""
    for name in declared:
        if not _may_be(declared, name, holder):
            continue
        current = name
        for _ in range(64):
            if current not in declared:
                break
            if field in declared[current].get("fields", {}):
                return True
            current = declared[current].get("super")
    return False


def _jdk_field(variable):
    """(declaring class, field, True) for a field the JDK declares, as the launcher
    names it; None for the program's; what is not known, as text."""
    owner, jdk = variable.get("peekyOwner"), variable.get("peekyJdkOwner")
    if not isinstance(owner, str) or not owner or type(jdk) is not bool:
        return f"`{variable.get('name')}`"
    return (owner, variable.get("name"), True) if jdk else None


def _jdk_state(trail, index):
    """Whether the value at ``trail[index]`` (the debugger's variables from a root local)
    is a JDK class's state (#204): (declaring class, field, whether that field holds it
    directly) of the JDK field holding it, or an array holding it, whatever that array's
    type (an Object[] is no proof of whose its elements are); None where it is the
    program's: a root local's, a field of the program's, or a JDK list's element (its
    backing array, _USER_ELEMENTS) where the list itself is the program's. What is not
    known, as text."""
    direct = True
    while index > 0:
        holder = trail[index - 1]
        if not str(holder.get("type") or "").endswith("]"):
            # A field of an object: whose the field is.
            found = _jdk_field(trail[index])
            return (*found[:2], direct) if isinstance(found, tuple) else found
        # An element of the array ``holder``: whose that array is.
        direct = False
        if index > 1 and not str(trail[index - 2].get("type") or "").endswith("]"):
            found = _jdk_field(holder)
            if isinstance(found, tuple) and f"{found[0]}.{found[1]}" in _USER_ELEMENTS:
                # A JDK list's element: the program's, where the list is.
                index -= 2
                continue
        index -= 1
    return None


# The short form a row shows (#261) of why `set` refuses a field (member_settable).
_MEMBER_ROWS = {"it is final": "final", "it is static (shared by every instance)": "static"}


# Why no element is promised [settable] while other threads run (#261 review).
_THREADS_RUNNING = "other threads are running; set checks again"


class Unproven(str):
    """Why an element's mark can be promised neither way (#261): the launcher could not
    tell in time whether a JDK class holds its array, or cannot tell before a change (an
    older bundle). `set` checks again when the change is made."""

    unproven = True


class JdkState(str):
    """Why a field or element is a JDK class's own state, which no change reaches (#204);
    ``row``: the short form a row's `[not settable: ...]` shows, for a value of a type
    `set` changes elsewhere (None for an object or array)."""

    def __new__(cls, reason, row):
        made = super().__new__(cls, reason)
        made.row = row
        return made


# Why a local the code never reads after a line's start cannot be changed there (#164),
# judged before the program runs too (#183).
_UNREAD_REASON = (
    "is never read after a stop at this line's start (a lambda or inner class that "
    "captured it before keeps its own copy; javac compiles a constant local into its uses), "
    "so a change would never be seen"
)
# A local variable table's descriptors of what `--set` can change (#183).
_DESCRIPTORS = {
    "B": "byte",
    "S": "short",
    "I": "int",
    "J": "long",
    "C": "char",
    "Z": "boolean",
    "F": "float",
    "D": "double",
    "Ljava/lang/String;": "String",
}


def _descriptor_kind(descriptor):
    """The Java type `--set` judges a local declared with ``descriptor`` as, or None."""
    return _DESCRIPTORS.get(str(descriptor))


def _fit_reason(name, kind, value):
    """Why ``value`` does not fit a local ``name`` of the Java type ``kind``, or None."""
    try:
        if parse_text(value) is None and not (
            SCALAR.fullmatch(value) or java_char(value, code_points=False) is not None
        ):
            raise ValueError(
                "Java mutation requires a literal: a number, true or false, a char in single "
                "quotes ('+'), or a text in double quotes for a String"
            )
        _value_fits(name, kind, value)
    except ValueError as exc:
        return str(exc)
    return None


def _value_fits(name, kind, value):
    """Raise ValueError where ``value`` does not fit the Java type ``kind`` of ``name``
    exactly: java-debug narrows integers, keeps one character of a char and parses any
    non-"true" boolean as false, then reports success."""
    text = parse_text(value)
    if kind == "char":
        if java_char(value) is None:
            raise ValueError(
                f"Java mutation requires a char for char {name}: a char literal in single "
                "quotes ('+', '\\n', '\\u0041') or a code point from 0 to 65535"
            )
        return
    if kind in _JAVA_STRINGS:
        if text is None:
            raise ValueError(
                f"Java mutation requires text in double quotes for String {name} "
                f"(--set '{name}=\"apple\"'): the local then refers to a new String; "
                "other references keep the old one"
            )
        return
    if text is not None or java_char(value, code_points=False) is not None:
        raise ValueError(
            f"Java mutation requires a literal of {name}'s type ({kind}); text in double "
            "quotes changes a String, a char in single quotes a char"
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


# A Java char literal's escapes (JLS 3.10.7), and the octal ones (`'\0'` to `'\377'`).
_CHAR_ESCAPES = {
    "b": "\b",
    "s": " ",
    "t": "\t",
    "n": "\n",
    "f": "\f",
    "r": "\r",
    '"': '"',
    "'": "'",
    "\\": "\\",
}
_CHAR_LITERAL = re.compile(
    r"'(?:([^'\\\n\r])|\\([bstnfr\"'\\])|\\u+([0-9a-fA-F]{4})|\\([0-3]?[0-7]{1,2}))'\Z"
)


def java_char(value, code_points=True):
    """The char a value names for a char variable (#183): a char literal as Java writes
    one (`'+'`, `'\n'`, `'\''`, `'\u0041'`, `'\101'`), or, with ``code_points``, a
    decimal code point from 0 to 65535 (as `char op = 43;` is `'+'`); None if neither.
    One UTF-16 unit: a character outside the BMP is two chars in Java."""
    found = _CHAR_LITERAL.fullmatch(str(value))
    if found:
        plain, escape, unicode, octal = found.groups()
        if plain is not None:
            return plain if len(plain.encode("utf-16-le")) == 2 else None
        if escape is not None:
            return _CHAR_ESCAPES[escape]
        if unicode is not None:
            return chr(int(unicode, 16))
        number = int(octal, 8)
        return chr(number) if number < 256 else None
    if code_points and re.fullmatch(r"0|[1-9][0-9]{0,4}", str(value)) and int(value) < 65536:
        return chr(int(value))
    return None


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
    "{missing}): rebuild it with `python scripts/setup_java_adapter.py --output <a new "
    "directory>`, then point the target's bundle (or PEEKY_JAVA_ADAPTER) at it. Without "
    "eventhub-log-guard, java-debug can stop reporting stops and exits"
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
    # Every class but the JDK's is the program's, read from its class files (#204).
    knows_every_class = True
    # Set from the launcher's capabilities: whether it names fields' declaring classes.
    field_owners = False
    # And whether it says what holds an array, before its elements are marked (#261).
    array_holders = False
    # And whether it changes fields and elements at all.
    field_set = False
    # A field or element change tells the launcher what is left of the pause budget, for
    # its search of what refers to an array (#204).
    pause_budget_writes = True
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
            # And a char a quoted char (#183).
            self.char_set = raw.get("peekyCharSet") is True
            # A launcher that names each listed field's declaring class (#204).
            self.field_owners = raw.get("peekyJdkOwners") is True
            # And says whether a JDK class's field holds an array (#261).
            self.array_holders = raw.get("peekyArrayHolders") is True
            self.field_set = raw.get("peekyFieldSet") is True
        return {
            "supports_set_variable": raw.get("supportsSetVariable") is True,
            # Launcher bundles built before stepping lack the marker: refuse cleanly.
            "supports_stepping": raw.get("peekySteps") is True,
            # Bundles whose launcher refuses a field name a hiding static field shares.
            "supports_member_set": raw.get("peekyFieldSet") is True,
            # Bundles whose launcher changes a String to a text (#137).
            "supports_text_set": raw.get("peekyTextSet") is True,
            # Bundles whose launcher changes a char to a quoted char (#183).
            "supports_char_set": raw.get("peekyCharSet") is True,
            "supports_conditional_breakpoints": False,
            "supports_hit_conditional_breakpoints": False,
            "supports_logpoints": False,
            "supports_evaluate": False,
            "supports_attach": False,
        }

    def validate_breakpoints(self, points):
        self.target.validate_breakpoints(points)

    def validate_value(self, value):
        """Only the shape of a literal of some Java type; the variable's own type decides
        the rest (validate_variable), so a refusal names the real rule (#183)."""
        if isinstance(value, str) and (
            parse_text(value) is not None
            or SCALAR.fullmatch(value)
            or java_char(value, code_points=False) is not None
        ):
            return
        raise ValueError(
            "Java mutation requires a literal: a number, true or false, a char in single "
            "quotes ('+'), or a text in double quotes for a String"
        )

    def mutation_literal(self, value, variable=None):
        """What java-debug is sent: a text (#137) between plain quotes, which it takes
        off and keeps everything between as the new String (it unescapes nothing); for a
        char (#183), the char itself between single quotes, which it takes as that char
        (a code point or an escape is turned into it here)."""
        if variable is not None and variable.get("type") == "char":
            return f"'{java_char(value)}'"
        text = parse_text(value)
        return value if text is None else f'"{text}"'

    def confirm_mutation(self, name, value, before, after, variable=None):
        """A String (#137) must read back as the text asked, as java-debug shows one
        (between quotes, nothing escaped), and a char (#183) as the char itself: else
        the change is not reported."""
        if variable is not None and variable.get("type") == "char":
            if after != java_char(value):
                raise RuntimeError(
                    f"java-debug stored {str(after)[:80]!r} in {name!r}, not the char asked: "
                    "the change did not reach the program as stated, so the session is closed"
                )
            return
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
                f"{name!r} cannot be changed: change what the code computes from it with a "
                "candidate edit (`--old`/`--new`). The program never reads it, because javac "
                "compiles a constant local (`final`, with a constant initializer) into the "
                "code that uses it, so a change would never be seen"
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
            return f"it is {kind}; {self._settable_text(kind)}"
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

    def whole_value(self, variable):
        """Whether a value is shown whole, its fields listed only when selected by name
        (#204): a String, which java-debug shows as its text (only a java.lang.String's
        value is quoted), and a boxed primitive (`Integer@57`), whose `.value` stays
        readable."""
        kind, value = str(variable.get("type")), str(variable.get("value"))
        if kind in _JAVA_STRINGS:
            return value.startswith('"')
        short = kind.removeprefix("java.lang.")
        return short in _BOXED and bool(re.fullmatch(rf"{short}@[0-9]+", value))

    def internal_member(self, variable, parent=None, trail=()):
        """Why `set` never changes this field or element (#204), else None: a field a JDK
        class declares (a String's `hash`, an ArrayList's `size`, inherited or not), as
        the launcher names its declaring class and whether it is the JDK's (its module
        or class loader), or an element of an array that is the JDK's state (_jdk_state:
        a String's bytes). ``parent``: the type of what holds it; ``trail``: the
        debugger's variable for each step from the root local to it. Also why it cannot
        be told (a launcher that names no declaring class). Judged by the way a selector
        reaches it: the launcher refuses an array a JDK field holds under any other name
        when the change is made."""
        element = str(parent or "").endswith("]")
        if element:
            trail = list(trail or ())
            if not trail:
                return "Peeky cannot tell what holds its array"
            found = _jdk_state(trail, len(trail) - 1)
        else:
            found = _jdk_field(variable)
        if found is None:
            return None
        if isinstance(found, str):
            if not getattr(self, "field_owners", False):
                return (
                    "this adapter bundle predates telling the JDK's own fields from the "
                    "program's, so no field can be changed; rebuild it with "
                    "scripts/setup_java_adapter.py"
                )
            return "Peeky cannot tell which class declares " + (
                f"{found}, which holds its array" if element else "it"
            )
        owner, field, direct = found
        scalar = variable.get("type") in (*_JAVA_INTEGER_BITS, "char", "boolean", "float")
        scalar = scalar or variable.get("type") in ("double", *_JAVA_STRINGS)
        if element:
            where = "an array that" if direct else "an array inside what"
            return JdkState(
                f"it is in {where} `{field}` holds, a field the JDK class {owner} "
                f"declares: {_JDK_WHY}",
                f"in an array {owner}'s `{field}` holds (the JDK's own state)" if scalar else None,
            )
        return JdkState(
            f"it is declared by {owner}, a JDK class: {_JDK_WHY}",
            f"declared by {owner} (the JDK's own state)" if scalar else None,
        )

    def array_holder(self, dap, array, pause_left_ms=None):
        """(why no element of ``array`` is marked [settable], in short, or None; whether
        that holds for the rest of the stop) (#261). ``array``: the debugger's variable
        for an array whose elements are listed. From the launcher's search of what
        refers to it, which a write makes again (with a longer wait and every thread
        suspended), so a mark promises no more than a write does:

        - a JDK class's field holds it, or the search cannot tell for a reason a write
          meets too (too many referrers): the reason;
        - Unproven where a write may yet be made: a search that ran out of time, a
          launcher that cannot search before a change, or another thread that is running
          (a line breakpoint stops its own thread only), which could change what holds
          the array before the write;
        - None: no JDK class holds it, and no other thread runs.

        Only a verdict made with every thread suspended holds for the rest of the stop.
        ``pause_left_ms``: what is left of the session's pause budget."""
        if not self.array_holders:
            return (
                Unproven(
                    "this adapter bundle can't tell before a change whether a JDK class holds "
                    "its array; rebuild it with scripts/setup_java_adapter.py"
                ),
                True,
            )
        request = {"variablesReference": array.get("variablesReference")}
        if pause_left_ms is not None:
            request["peekyPauseLeftMs"] = max(0, int(pause_left_ms))
        try:
            found = dap.request("peekyArrayHolder", request)
        except RequestFailed as exc:
            return (
                Unproven(
                    f"Peeky can't tell whether a JDK class holds its array ({str(exc)[:160]}); "
                    "`set` checks when the change is made"
                ),
                False,
            )
        settled = found.get("allSuspended") is True
        holder, unknown = found.get("holder"), found.get("unknown")
        if isinstance(holder, str) and holder:
            return f"its array is {holder[:200]} (the JDK's own state)", settled
        if isinstance(unknown, str) and unknown:
            if found.get("timedOut") is True:
                return Unproven(f"{unknown[:200]}; `set` searches again, for up to 2 s"), settled
            # More referrers than are read, or a JVM that cannot list them: a write is
            # refused for the same reason.
            return unknown[:200], settled
        if not settled:
            return Unproven(_THREADS_RUNNING), False
        return None, True

    def unsettable_row(
        self,
        variable,
        frame=None,
        parent=None,
        trail=(),
        member=False,
        innermost=True,
        unread=False,
    ):
        """Why a row of a type `set` changes is not marked [settable], in short (#261), or
        None: for an object or array its type says it, and a name no selector reads is
        never marked. ``member``: a field or element (``parent``: the type of what holds
        it; ``trail``: as for internal_member); ``unread``: a local the program does not
        read after this stop."""
        name, kind = str(variable.get("name")), variable.get("type")
        if kind not in (*_JAVA_INTEGER_BITS, *_settable_kinds(self)):
            return None
        if member:
            why = self.member_settable(variable, parent=parent, proven=True)
            if why == "no selector names it":
                return None
            if why is not None:
                return _MEMBER_ROWS.get(why) or (
                    f"declared {found[1].removeprefix('java.lang.')}, not String"
                    if (found := re.match(r"it is declared (\S+) and holds a String", why))
                    else why
                )
            internal = self.internal_member(variable, parent=parent, trail=trail)
            if internal:
                return getattr(internal, "row", None) or str(internal)[:200]
            if not self.field_set:
                return (
                    "this adapter bundle predates field and element changes; rebuild it with "
                    "scripts/setup_java_adapter.py"
                )
        else:
            if not is_name(name):
                return None
            declared = self.declared_local(frame, name) if frame else None
            if declared and not all(read for _, read in declared):
                return "a constant javac compiles into its uses"
            if kind in _JAVA_STRINGS and not (
                declared and all(each == _STRING_DESCRIPTOR for each, _ in declared)
            ):
                shown = {descriptor_name(each) for each, _ in declared or ()}
                return (
                    f"declared {', '.join(sorted(shown))}, not String"
                    if shown
                    else "Peeky cannot tell whether it is declared a String"
                )
        if not innermost:
            return "in a caller's frame"
        if unread:
            return "not read again after this stop"
        return None

    def validate_variable(self, variable, value):
        """The value must fit the local's declared type exactly.

        java-debug narrows integers, keeps one character of a char and parses any
        non-"true" boolean as false, then reports success; refuse those instead.
        """
        self.validate_value(value)
        name, kind = variable.get("name"), variable.get("type")
        if kind not in (*_JAVA_INTEGER_BITS, *_settable_kinds(self)):
            # The type first (#183): `op='+'` on a char named the literal, not the rule.
            raise ValueError(f"{name} is {kind}; {self._settable_text(kind)}")
        _value_fits(name, kind, value)

    def launch_refusal(self, program, line, name, value=None):
        """Why a probe's `--set` of the local ``name`` (to ``value``, when given) can
        change nothing at any stop of ``line`` of ``program``, judged from the class
        files before the program runs (#183), or None: in every method that stops on the
        line (the first holding it in its class, as java-debug binds it), it is no local
        of the method there, a type no change reaches, a value its
        declared type cannot hold, or a local the code never reads after the line's start
        (where a line breakpoint stops). A field (`this.x`) and an element are judged at
        the stop. (code, message) for a name no method there declares."""
        from peeky.java_metadata import reads_after

        if re.search(r"[.\[]", str(name)):
            return self._member_refusal(program, line, str(name))
        if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", str(name)):
            return None
        try:
            source = self._line_key(program)
        except (OSError, ValueError):
            return None
        # java-debug binds a line to the first method holding it in its class, in
        # class-file order (a lambda's on its method's line, overloads on one line): only
        # that method's stops can make the change (#192 review).
        methods = self._line_methods(source, line)
        if not methods:
            return None
        reasons, visible = [], set()
        for method in methods:
            starts = method["line_pcs"][line]
            declared = [
                (descriptor, read, slot)
                for (local, descriptor, lines, read), (_, slot, _, _) in zip(
                    method["locals"], method["slots"]
                )
                if line in lines and (visible.add(local) or True) and local == name
            ]
            reason = None
            for descriptor, read, slot in declared:
                kind = _descriptor_kind(descriptor)
                if kind is None:
                    why = (
                        f"`{name}` is declared {descriptor_name(descriptor)}; only byte, short, "
                        "int, long, char, boolean, float and double locals can be changed, and "
                        "a String to a text"
                    )
                elif value is not None and (why := _fit_reason(name, kind, value)):
                    pass
                elif not read or reads_after(method, slot, starts) is False:
                    why = f"`{name}` " + _UNREAD_REASON
                else:
                    reason = None
                    break
                reason = why
            if not declared:
                reason = "absent"
            if reason is None:
                return None
            reasons.append(reason)
        where = f"`{name}` cannot be changed at {source}:{line}, at any stop of this build: "
        tail = ". Refused before the program runs: nothing ran"
        if all(reason == "absent" for reason in reasons):
            listed = ", ".join(sorted(visible)) or "none"
            return (
                "NOT_FOUND",
                where + f"no method with code on line {line} has a local `{name}` there "
                f"(locals there: {listed}"
                + (f"; a field is `this.{name}`" if "this" in visible else "")
                + ")"
                + tail,
            )
        found = [reason for reason in reasons if reason != "absent"]
        return where + "; ".join(dict.fromkeys(found)) + tail

    def _line_methods(self, source, line):
        """The methods that stop on ``line`` of ``source``: in each class, the first in
        class-file order holding it, as java-debug binds a line (#192 review)."""
        methods = []
        for declarations in self._declared().values():
            if declarations.get("source") != source:
                continue
            holding = [
                method
                for method in declarations.get("methods", ())
                if line in method.get("line_pcs", {})
            ]
            methods += holding[:1]
        return methods

    def _member_refusal(self, program, line, name):
        """Why a change of the field or element ``name`` (`a.text.hash`, `list.size`,
        `a.text.value[0]`) can never be made at ``line`` (#204), judged from the class
        files before the program runs, or None (the stop judges it): a field of a value
        declared a JDK type (any class not in the program's class files), or of a class
        of the program whose superclass is one, that no class of the program which may be
        that value declares (so any such field is the JDK's own), or an element of an
        array such a field holds. Only where every class but the JDK's is the program's
        (not an attached JVM, whose libraries Peeky does not read)."""
        if not getattr(self, "knows_every_class", True):
            return None
        match = re.fullmatch(r"([^\W\d][\w$]*)((?:\.[^\W\d][\w$]*|\[-?[0-9]+\])+)", name)
        if not match:
            return None
        try:
            source = self._line_key(program)
        except (OSError, ValueError):
            return None
        declared = self._declared()
        descriptors = {
            descriptor
            for method in self._line_methods(source, line)
            for local, descriptor, lines, _ in method["locals"]
            if local == match[1] and line in lines
        }
        if len(descriptors) != 1:
            return None
        current, path, internal = descriptors.pop(), match[1], None
        for field, index in re.findall(r"\.([^\W\d][\w$]*)|\[(-?[0-9]+)\]", match[2]):
            element = not field
            if element:
                # An element: of an array whose type is known, or directly of one a JDK
                # field holds (whose rows may be the program's: the stop judges them).
                if current and current.startswith("["):
                    current = current[1:]
                elif not (internal and current is None and not path.endswith("]")):
                    return None
                path += f"[{index}]"
                continue
            holder = current[1:-1] if current and current.startswith("L") else None
            if holder is None:
                return None
            top = holder
            for _ in range(64):
                if top not in declared or field in declared[top].get("fields", {}):
                    break
                top = declared[top].get("super")
            if top in declared and field in declared[top].get("fields", {}):
                current, internal = declared[top]["fields"][field], None
            elif (
                top is None
                or top in declared
                or top == "java/lang/Object"
                or _may_declare(declared, holder, field)
            ):
                # No such field, or one a class of the program that may be the value
                # declares: the stop judges.
                return None
            else:
                shown, above = holder.replace("/", "."), top.replace("/", ".")
                internal = (
                    f"`{path}` is declared {shown}, a JDK type"
                    if holder == top
                    else f"`{path}` is declared {shown}, which extends the JDK class {above}"
                ) + (
                    f", and no class of the program that may be one declares a field "
                    f"`{field}`, so any such field is declared by a JDK class"
                )
                current = None
            path += f".{field}"
        if internal is None:
            return None
        if element:
            internal += f"; `{name}` is in an array that field holds"
        return (
            f"`{name}` cannot be changed at {source}:{line}, at any stop of this build: "
            f"{internal}. {_JDK_WHY}. Refused before the program runs: nothing ran"
        )

    def _settable_text(self, kind=None):
        """What a Java change can reach with this launcher; for a ``kind`` an older one
        lacks (a char, #183; a String, #137), that a current bundle changes it."""
        kinds = ["byte", "short", "int", "long", "boolean", "float", "double"]
        if getattr(self, "char_set", False):
            kinds.insert(4, "char")
        if getattr(self, "text_set", False):
            kinds.append("String (to a text)")
        text = f"only {', '.join(kinds[:-1])} and {kinds[-1]} values can be changed"
        if kind not in kinds and kind in ("char", *_JAVA_STRINGS):
            text += (
                f"; a {kind} too with an adapter bundle built by this version of "
                "scripts/setup_java_adapter.py"
            )
        return text

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
