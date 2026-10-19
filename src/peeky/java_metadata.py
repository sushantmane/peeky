"""Bounded Java class debug-metadata reader, not a bytecode verifier.

Reads class identity, SourceFile and Code/LineNumberTable directly (JVMS chapter
4). Unknown attributes are length-bounded and skipped, as the class format permits.
SourceDebugExtension remapping, preview versions and versions newer than Java 21
are unsupported. The Java target supports JDK 17-21. Every class needs SourceFile;
concrete code needs executable line information. Interfaces/annotations without
concrete methods map to an empty line list. Shared
source lines between classes remain explicit; breakpoint selection must reject
ambiguous owners. No nested/anonymous name is guessed from a '$' suffix.
"""

import re
from pathlib import Path

MAX_CLASS_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 16384
MAX_CLASSES = 4096
MAX_SOURCES = 4096
# A name a Peeky selector can write (the probe's selector grammar).
_SELECTABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


class _Reader:
    def __init__(self, data):
        self.data = memoryview(data)
        self.offset = 0

    def take(self, size):
        if size < 0 or self.offset + size > len(self.data):
            raise ValueError("Truncated Java class metadata")
        start = self.offset
        self.offset += size
        return self.data[start : self.offset]

    def number(self, size):
        return int.from_bytes(self.take(size), "big")

    def u2(self):
        return self.number(2)

    def end(self):
        if self.offset != len(self.data):
            raise ValueError("Unexpected trailing bytes in Java metadata attribute")


def _modified_utf8(raw):
    # Class-file CONSTANT_Utf8 uses encoded UTF-16 code units, including C0 80
    # for NUL and surrogate pairs rather than four-byte UTF-8 sequences.
    units = []
    index = 0
    raw = bytes(raw)
    while index < len(raw):
        first = raw[index]
        index += 1
        if 1 <= first <= 0x7F:
            unit = first
        elif 0xC0 <= first <= 0xDF:
            if index >= len(raw) or raw[index] & 0xC0 != 0x80:
                raise ValueError("Invalid modified UTF-8 constant")
            unit = ((first & 0x1F) << 6) | (raw[index] & 0x3F)
            index += 1
            if unit < 0x80 and unit != 0:
                raise ValueError("Overlong modified UTF-8 constant")
        elif 0xE0 <= first <= 0xEF:
            if index + 1 >= len(raw) or any(b & 0xC0 != 0x80 for b in raw[index : index + 2]):
                raise ValueError("Invalid modified UTF-8 constant")
            unit = ((first & 15) << 12) | ((raw[index] & 63) << 6) | (raw[index + 1] & 63)
            index += 2
            if unit < 0x800:
                raise ValueError("Overlong modified UTF-8 constant")
        else:
            raise ValueError("Unsupported or invalid modified UTF-8 constant")
        units.append(unit.to_bytes(2, "big"))
    return b"".join(units).decode("utf-16-be", errors="surrogatepass")


def _class_metadata(data, shared=None, scopes=None):
    """(binary internal name, SourceFile, sorted executable lines). With ``scopes`` (a
    list), also appends (method, descriptor, first line, last line, local names) for each
    method with code, its names from the LocalVariableTable (`javac -g`). With ``shared`` (a
    dict), also records {line: {"stops": method, "never": [methods]}} for lines that
    several methods of the class share (a lambda body on its enclosing method's line, a
    static initializer's lambda, nested lambdas, a field initializer in each
    constructor). java-debug binds a line breakpoint to the first such method in
    class-file order only, so the others never stop there."""
    if len(data) > MAX_CLASS_BYTES:
        raise ValueError("Class file exceeds metadata size limit")
    reader = _Reader(data)
    if reader.number(4) != 0xCAFEBABE:
        raise ValueError("Invalid Java class magic")
    minor, major = reader.u2(), reader.u2()
    if not 45 <= major <= 65 or (minor > 3 if major == 45 else minor != 0):
        raise ValueError("Unsupported Java class version (require non-preview Java 21 or earlier)")
    pool = [None] * reader.u2()
    if len(pool) < 2:
        raise ValueError("Missing constant pool")
    index = 1
    widths = {
        3: 4,
        4: 4,
        5: 8,
        6: 8,
        8: 2,
        9: 4,
        10: 4,
        11: 4,
        12: 4,
        15: 3,
        16: 2,
        17: 4,
        18: 4,
        19: 2,
        20: 2,
    }
    while index < len(pool):
        tag = reader.number(1)
        if tag == 1:
            pool[index] = (tag, _modified_utf8(reader.take(reader.u2())))
        elif tag == 7:
            pool[index] = (tag, reader.u2())
        elif tag in widths:
            pool[index] = (tag, bytes(reader.take(widths[tag])))
        else:
            raise ValueError(f"Unsupported constant pool tag: {tag}")
        if tag in (5, 6):
            index += 1
            if index >= len(pool):
                raise ValueError("Invalid double-slot constant pool entry")
        index += 1

    def constant(index, tag):
        if not 0 < index < len(pool) or pool[index] is None or pool[index][0] != tag:
            raise ValueError("Invalid constant pool reference")
        return pool[index][1]

    def utf(index):
        return constant(index, 1)

    def attrs(parent):
        for _ in range(parent.u2()):
            name = utf(parent.u2())
            yield name, _Reader(parent.take(parent.number(4)))

    reader.u2()  # access flags
    internal = utf(constant(reader.u2(), 7))
    if (
        not internal
        or any(char in internal for char in ".;[\\")
        or any(part in ("", ".", "..") for part in internal.split("/"))
        or any(ord(c) == 0 or 0xD800 <= ord(c) <= 0xDFFF for c in internal)
    ):
        raise ValueError("Invalid binary class identity")
    super_class = reader.u2()
    if super_class:
        constant(super_class, 7)
    for _ in range(reader.u2()):
        constant(reader.u2(), 7)
    for _ in range(reader.u2()):
        reader.u2()
        utf(reader.u2())
        utf(reader.u2())
        for name, _ in attrs(reader):
            if name in {"Code", "SourceFile", "LineNumberTable", "SourceDebugExtension"}:
                raise ValueError("Debug metadata in unsupported field context")
    lines = set()
    line_methods = {}
    for _ in range(reader.u2()):
        flags = reader.u2()
        method_name = utf(reader.u2())
        descriptor = utf(reader.u2())
        code_count = 0
        for name, body in attrs(reader):
            if name != "Code":
                if name in {"SourceFile", "LineNumberTable", "SourceDebugExtension"}:
                    raise ValueError("Debug metadata in unsupported method context")
                continue
            code_count += 1
            if code_count > 1 or flags & (0x0100 | 0x0400):
                raise ValueError("Duplicate or invalid Code attribute")
            body.u2()
            body.u2()
            code_length = body.number(4)
            if not 0 < code_length < 65536:
                raise ValueError("Invalid method code length")
            body.take(code_length)
            for _ in range(body.u2()):
                start, end, handler, catch = (body.u2() for _ in range(4))
                if not (start < end <= code_length and handler < code_length):
                    raise ValueError("Invalid exception-table location")
                if catch:
                    constant(catch, 7)
            method_lines, names, table = {}, [], False
            for nested_name, nested in attrs(body):
                if nested_name in {"Code", "SourceFile", "SourceDebugExtension"}:
                    raise ValueError("Unsupported nested debug metadata")
                if nested_name == "LocalVariableTable" and scopes is not None:
                    table = True
                    for _ in range(nested.u2()):
                        start, length = nested.u2(), nested.u2()
                        name = utf(nested.u2())
                        utf(nested.u2())
                        slot = nested.u2()
                        if start + length > code_length:
                            raise ValueError("Invalid local variable location")
                        names.append((slot, start, name))
                    nested.end()
                    continue
                if nested_name != "LineNumberTable":
                    continue
                for _ in range(nested.u2()):
                    pc, line = nested.u2(), nested.u2()
                    if pc >= code_length or line < 1:
                        raise ValueError("Invalid source line location")
                    if pc in method_lines and method_lines[pc] != line:
                        raise ValueError("Ambiguous source line for bytecode location")
                    method_lines[pc] = line
                nested.end()
            body.end()
            if not method_lines:
                raise ValueError("Missing executable line debug information; compile with -g")
            lines.update(method_lines.values())
            if scopes is not None:
                # javac -g leaves the table out only where there is no name to list (a
                # static method without parameters or locals); `this` or a parameter
                # without it means the class was compiled without local names.
                if not table and (not flags & 0x0008 or not descriptor.startswith("()")):
                    raise ValueError(
                        "the classes have no local variable names (LocalVariableTable); "
                        "compile with javac -g"
                    )
                # The line at pc 0 starts the method. javac copies every field
                # initializer's line into each constructor, wherever the field is
                # declared, while a constructor ends at its last instruction (the
                # `return` at its closing brace): only lines between the two are its own.
                first = method_lines[min(method_lines)]
                last = method_lines[max(method_lines)] if method_name == "<init>" else None
                found = [
                    line
                    for line in method_lines.values()
                    if line >= first and (last is None or line <= last)
                ]
                # `this` and the parameters first (the lowest slots), then locals; only
                # names a selector can write (not `this$0`).
                ordered = [
                    name
                    for name in dict.fromkeys(name for _, _, name in sorted(names))
                    if _SELECTABLE.fullmatch(name)
                ]
                scopes.append((method_name, descriptor, first, max(found), ordered))
            for line in sorted(set(method_lines.values())):
                # Class-file order, which is the order java-debug considers them in.
                line_methods.setdefault(line, []).append((method_name, descriptor))
        if not flags & (0x0100 | 0x0400) and code_count != 1:
            raise ValueError("Missing Code attribute")
    source = None
    for name, body in attrs(reader):
        if name == "SourceDebugExtension":
            raise ValueError("SourceDebugExtension remapping is unsupported")
        if name in {"Code", "LineNumberTable"}:
            raise ValueError("Debug metadata in unsupported class context")
        if name == "SourceFile":
            if source is not None:
                raise ValueError("Duplicate SourceFile attribute")
            source = utf(body.u2())
            body.end()
    reader.end()
    if shared is not None:
        for line, methods in line_methods.items():
            if len(methods) > 1:
                names = [name for name, _ in methods]
                shown = [
                    f"{name}{descriptor}" if names.count(name) > 1 else name
                    for name, descriptor in methods
                ]
                shared[line] = {"stops": shown[0], "never": shown[1:]}
    if not source or not source.endswith(".java") or any(c in source for c in "/\\\x00"):
        raise ValueError("Missing or unsupported SourceFile debug information")
    return internal, source, sorted(lines)


def java_scopes(mapping, classes_dir, workspace, limit=300):
    """Each method's line span and the names it can see (`this`, parameters, locals),
    as `describe` lists them for other languages: from the prepared class files."""
    workspace = Path(workspace).resolve()
    found = []
    for binary, entry in mapping["classes"].items():
        internal = binary.replace(".", "/")
        path = Path(entry.get("class_file") or Path(classes_dir) / f"{internal}.class")
        methods = []
        _class_metadata(path.read_bytes(), scopes=methods)
        source = Path(entry["path"]).resolve()
        source = (
            source.relative_to(workspace).as_posix()
            if source.is_relative_to(workspace)
            else source.name
        )
        owner = internal.rsplit("/", 1)[-1]
        for method, _, first, last, names in methods:
            found.append(
                {
                    "scope": f"{owner}.{method}",
                    "source": source,
                    "lines": [first, last],
                    "names": names,
                }
            )
    found.sort(key=lambda scope: (scope["source"], scope["lines"][0], scope["scope"]))
    if len(found) > limit:
        found = found[:limit] + [{"omitted": len(found) - limit}]
    return found


def _regular_inside(path, root):
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Source or class path is outside its declared root") from exc
    if any(part in ("..", ".") for part in relative.parts):
        raise ValueError("Source or class path traversal is unsupported")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Source or class symlinks are unsupported")
    if not path.is_file():
        raise ValueError("Source or class must be a regular file")


def generate_source_map(classes_dir: Path, source_paths: list[Path], source_root: Path) -> dict:
    """Map exact binary names to uniquely declared package-relative Java sources.

    Sources may have prefixes (e.g. src/main/java); their suffix must match the
    class package plus SourceFile. Multiple matching roots are rejected. Calls
    must be surrounded by prepared-build identity checks by the owning session.
    """
    classes_dir, source_root = Path(classes_dir).absolute(), Path(source_root).absolute()
    if classes_dir.is_symlink() or source_root.is_symlink():
        raise ValueError("Metadata roots cannot be symlinks")
    if not classes_dir.is_dir() or not source_root.is_dir():
        raise ValueError("Metadata roots must be existing directories")
    if not 0 < len(source_paths) <= MAX_SOURCES:
        raise ValueError("Source map requires a bounded nonempty source list")
    sources = {}
    total = 0
    for declared in source_paths:
        path = Path(declared)
        if not path.is_absolute():
            path = source_root / path
        _regular_inside(path, source_root)
        if path in sources:
            raise ValueError("Duplicate declared source path")
        size = path.stat().st_size
        total += size
        if size > MAX_CLASS_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("Source metadata size limit exceeded")
        try:
            count = len(path.read_text(encoding="utf-8").splitlines())
        except UnicodeError as exc:
            raise ValueError("Java sources must use UTF-8") from exc
        sources[path] = count
    classes = {}
    for entry_index, path in enumerate(classes_dir.rglob("*")):
        if entry_index >= MAX_ENTRIES:
            raise ValueError("Class directory entry count exceeds limit")
        if path.is_symlink():
            raise ValueError("Class directory symlinks are unsupported")
        if path.suffix != ".class":
            continue
        _regular_inside(path, classes_dir)
        total += path.stat().st_size
        if (
            total > MAX_TOTAL_BYTES
            or path.stat().st_size > MAX_CLASS_BYTES
            or len(classes) >= MAX_CLASSES
        ):
            raise ValueError("Class metadata size/count limit exceeded")
        shared = {}
        internal, source_name, lines = _class_metadata(path.read_bytes(), shared)
        binary = internal.replace("/", ".")
        if binary in classes:
            raise ValueError("Duplicate binary class identity")
        if path.relative_to(classes_dir).as_posix() != internal + ".class":
            raise ValueError("Class file path does not match binary identity")
        suffix = (*internal.split("/")[:-1], source_name)
        matches = [
            source
            for source in sources
            if source.relative_to(source_root).parts[-len(suffix) :] == suffix
        ]
        if len(matches) != 1:
            raise ValueError("Missing or ambiguous declared source for class metadata")
        source = matches[0]
        if lines and lines[-1] > sources[source]:
            raise ValueError("Class source line exceeds declared source length")
        classes[binary] = {
            "path": str(source),
            "lines": lines,
            # Lines several methods share; only the first stops (see _class_metadata).
            **({"shared_lines": {str(k): v for k, v in sorted(shared.items())}} if shared else {}),
        }
    if not classes:
        raise ValueError("No executable Java classes found")
    return {"classes": dict(sorted(classes.items()))}
