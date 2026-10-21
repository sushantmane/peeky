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


# Each JVM instruction's length by opcode, where fixed (JVMS 6.5); tableswitch (0xaa),
# lookupswitch (0xab) and wide (0xc4) are measured from their operands.
_LENGTHS = bytearray([1] * 256)
for _op in (0x10, 0x12, 0x15, 0x16, 0x17, 0x18, 0x19, 0x36, 0x37, 0x38, 0x39, 0x3A):
    _LENGTHS[_op] = 2
for _op in (0xA9, 0xBC):
    _LENGTHS[_op] = 2
for _op in (0x11, 0x13, 0x14, 0x84, *range(0x99, 0xA9), *range(0xB2, 0xB9), 0xBB, 0xBD):
    _LENGTHS[_op] = 3
for _op in (0xC0, 0xC1, 0xC6, 0xC7):
    _LENGTHS[_op] = 3
_LENGTHS[0xC5] = 4
for _op in (0xB9, 0xBA, 0xC8, 0xC9):
    _LENGTHS[_op] = 5
# Instructions that read a local's slot: xload, xload_n, iinc, ret.
_LOADS = frozenset({0x15, 0x16, 0x17, 0x18, 0x19, 0x84, 0xA9})


def _reads(code):
    """{(slot, pc)} of every instruction in ``code`` that reads a local variable slot
    (a load, `iinc`, `ret`, their `wide` forms). ValueError where the code does not
    decode."""
    found, pc = set(), 0
    while pc < len(code):
        op = code[pc]
        if op in _LOADS:
            found.add((code[pc + 1], pc))
        elif 0x1A <= op <= 0x2D:
            found.add(((op - 0x1A) % 4, pc))
        if op == 0xC4:
            inner = code[pc + 1]
            if inner in _LOADS:
                found.add((int.from_bytes(code[pc + 2 : pc + 4], "big"), pc))
            size = 6 if inner == 0x84 else 4
        elif op in (0xAA, 0xAB):
            start = (pc + 4) & ~3
            if start + 12 > len(code):
                raise ValueError("Truncated switch instruction")
            if op == 0xAA:
                low = int.from_bytes(code[start + 4 : start + 8], "big", signed=True)
                high = int.from_bytes(code[start + 8 : start + 12], "big", signed=True)
                size = start + 12 + 4 * (high - low + 1) - pc
            else:
                pairs = int.from_bytes(code[start + 4 : start + 8], "big", signed=True)
                size = start + 8 + 8 * pairs - pc
            if size <= 0:
                raise ValueError("Invalid switch instruction")
        else:
            size = _LENGTHS[op]
        pc += size
    if pc != len(code):
        raise ValueError("Code does not decode into whole instructions")
    return found


# Stores into a local's slot: xstore with an index, xstore_n.
_STORES = frozenset({0x36, 0x37, 0x38, 0x39, 0x3A})


def _instructions(code):
    """{pc: (reads slot or None, writes slot or None, successors, falls through)}
    for each instruction of ``code``; None where it does not decode or uses `jsr`/
    `ret` (subroutines, which modern javac never emits)."""
    found, pc = {}, 0

    def offset(at, size):
        return int.from_bytes(code[at : at + size], "big", signed=True)

    try:
        while pc < len(code):
            op = code[pc]
            reads = writes = None
            targets, falls, size = [], True, _LENGTHS[op]
            if op in (0xA8, 0xA9, 0xC9):
                return None
            if op in _LOADS:
                reads = code[pc + 1]
            elif 0x1A <= op <= 0x2D:
                reads = (op - 0x1A) % 4
            elif op in _STORES:
                writes = code[pc + 1]
            elif 0x3B <= op <= 0x4E:
                writes = (op - 0x3B) % 4
            if op == 0xC4:
                inner, index = code[pc + 1], int.from_bytes(code[pc + 2 : pc + 4], "big")
                if inner in (0xA9,):
                    return None
                reads = index if inner in _LOADS else None
                writes = index if inner in _STORES else None
                size = 6 if inner == 0x84 else 4
            elif 0x99 <= op <= 0xA7 or op in (0xC6, 0xC7):
                targets.append(pc + offset(pc + 1, 2))
                falls = op != 0xA7
            elif op == 0xC8:
                targets.append(pc + offset(pc + 1, 4))
                falls = False
            elif op in (0xAA, 0xAB):
                start = (pc + 4) & ~3
                targets.append(pc + offset(start, 4))
                if op == 0xAA:
                    low, high = offset(start + 4, 4), offset(start + 8, 4)
                    targets += [pc + offset(start + 12 + 4 * i, 4) for i in range(high - low + 1)]
                    size = start + 12 + 4 * (high - low + 1) - pc
                else:
                    pairs = offset(start + 4, 4)
                    targets += [pc + offset(start + 12 + 8 * i, 4) for i in range(pairs)]
                    size = start + 8 + 8 * pairs - pc
                falls = False
            elif 0xAC <= op <= 0xB1 or op == 0xBF:
                falls = False
            if size <= 0:
                return None
            found[pc] = (reads, writes, targets, falls, pc + size)
            pc += size
    except IndexError:
        return None
    return found if pc == len(code) else None


def reads_after(method, slot, starts):
    """Whether the method's code may read local ``slot`` on some path from any of
    ``starts`` (the pcs where the stop's line begins: a breakpoint or a step stops
    there) before storing into it again: True, False, or None where the code does not
    decode (#164: a change to a local the code never reads again is never seen, as when
    a lambda captured its value before the stop)."""
    code = _instructions(method["code"])
    if code is None or not starts:
        return None
    pending, seen = list(starts), set()
    while pending:
        pc = pending.pop()
        if pc in seen:
            continue
        if pc not in code:
            return None
        seen.add(pc)
        reads, writes, targets, falls, after = code[pc]
        if reads == slot:
            return True
        # Any instruction a handler covers may throw into it.
        pending += [handler for start, end, handler in method["handlers"] if start <= pc < end]
        if writes == slot:
            continue
        pending += targets
        if falls:
            pending.append(after)
    return False


def _class_metadata(data, shared=None, scopes=None, declarations=None):
    """(binary internal name, SourceFile, sorted executable lines). With ``scopes`` (a
    list), also appends (method, descriptor, first line, last line, local names) for each
    method with code, its names from the LocalVariableTable (`javac -g`). With ``shared`` (a
    dict), also records {line: {"stops": method, "never": [methods]}} for lines that
    several methods of the class share (a lambda body on its enclosing method's line, a
    static initializer's lambda, nested lambdas, a field initializer in each
    constructor). java-debug binds a line breakpoint to the first such method in
    class-file order only, so the others never stop there. With ``declarations`` (a
    dict), also fills in "super" (the superclass's internal name), "fields" ({name:
    descriptor}) and "methods": [{"name", "first", "last", "locals": [(name, descriptor,
    {lines its scope's code is on}, whether the code ever reads it)]}] (#164: what a
    variable is declared, and a constant local javac folds into its uses, which the
    program never reads)."""
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
    if declarations is not None:
        declarations.update(
            super=utf(constant(super_class, 7)) if super_class else None, fields={}, methods=[]
        )
    for _ in range(reader.u2()):
        constant(reader.u2(), 7)
    for _ in range(reader.u2()):
        reader.u2()
        field_name = utf(reader.u2())
        field_type = utf(reader.u2())
        if declarations is not None:
            declarations["fields"][field_name] = field_type
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
            code = bytes(body.take(code_length))
            handlers = []
            for _ in range(body.u2()):
                start, end, handler, catch = (body.u2() for _ in range(4))
                if not (start < end <= code_length and handler < code_length):
                    raise ValueError("Invalid exception-table location")
                if catch:
                    constant(catch, 7)
                handlers.append((start, end, handler))
            method_lines, names, table, declared = {}, [], False, []
            for nested_name, nested in attrs(body):
                if nested_name in {"Code", "SourceFile", "SourceDebugExtension"}:
                    raise ValueError("Unsupported nested debug metadata")
                if nested_name == "LocalVariableTable" and (
                    scopes is not None or declarations is not None
                ):
                    table = True
                    for _ in range(nested.u2()):
                        start, length = nested.u2(), nested.u2()
                        name = utf(nested.u2())
                        descriptor_of = utf(nested.u2())
                        slot = nested.u2()
                        if start + length > code_length:
                            raise ValueError("Invalid local variable location")
                        names.append((slot, start, name))
                        declared.append((name, descriptor_of, slot, start, length))
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
            if declarations is not None:
                reads = _reads(code)
                starts = sorted(method_lines)

                def lines_of(start, length):
                    # The lines of the instructions in its scope: where a stop can see it.
                    found = {method_lines[pc] for pc in starts if start <= pc < start + length}
                    before = [pc for pc in starts if pc <= start]
                    return found | ({method_lines[before[-1]]} if before else set())

                declarations["methods"].append(
                    {
                        "name": method_name,
                        "first": min(method_lines.values()),
                        "last": max(method_lines.values()),
                        # For ``reads_after``: the code, its handlers, where each line's
                        # code starts, and each local's slot.
                        "code": code,
                        "handlers": tuple(handlers),
                        "line_pcs": {
                            line: tuple(pc for pc in starts if method_lines[pc] == line)
                            for line in set(method_lines.values())
                        },
                        "slots": [
                            (name, slot, start, length) for name, _, slot, start, length in declared
                        ],
                        "locals": [
                            (
                                name,
                                descriptor_of,
                                lines_of(start, length),
                                any(
                                    slot == read and start <= pc < start + length
                                    for read, pc in reads
                                ),
                            )
                            for name, descriptor_of, slot, start, length in declared
                        ],
                    }
                )
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
                scopes.append((method_name, descriptor, first, max(found), ordered, flags, found))
            for line in sorted(set(method_lines.values())):
                # Class-file order, which is the order java-debug considers them in.
                line_methods.setdefault(line, []).append((method_name, descriptor))
        if not flags & (0x0100 | 0x0400) and code_count != 1:
            raise ValueError("Missing Code attribute")
    if scopes is not None:
        _own_constructor_lines(scopes)
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


def _own_constructor_lines(scopes):
    """A constructor without a body of its own (implicit, or an anonymous class's), and a
    static initializer, end on their last field initializer: end them before the next
    method declared after them, so they never span methods between fields. Synthetic
    methods (a lambda) do not count."""
    starts = sorted(first for _, _, first, _, _, flags, _ in scopes if not flags & 0x1000)
    for index, (method, descriptor, first, last, names, flags, found) in enumerate(scopes):
        following = [start for start in starts if start > first]
        if method in ("<init>", "<clinit>") and following:
            own = [line for line in found if line < following[0]]
            last = max(own) if own else last
        scopes[index] = (method, descriptor, first, last, names)


def java_declarations(mapping, classes_dir, workspace):
    """{internal class name: ``_class_metadata``'s declarations, plus "source" (its path
    relative to ``workspace``)} for each prepared class (#164)."""
    workspace = Path(workspace).resolve()
    found = {}
    for binary, entry in mapping["classes"].items():
        internal = binary.replace(".", "/")
        path = Path(entry.get("class_file") or Path(classes_dir) / f"{internal}.class")
        declarations = {}
        _class_metadata(path.read_bytes(), declarations=declarations)
        source = Path(entry["path"]).resolve()
        declarations["source"] = (
            source.relative_to(workspace).as_posix()
            if source.is_relative_to(workspace)
            else source.name
        )
        found[internal] = declarations
    return found


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
