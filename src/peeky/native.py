"""Fixed C and C++/LLDB targets with fresh builds and bounded native symbol evidence.

Supported formats are single-architecture Mach-O (UUID) and ELF64 little-endian
(GNU build ID). File/module identity is trusted-code integrity, not protection
against hostile in-memory patching. SDK/system headers are not hermetic inputs.
"""

import bisect
import contextlib
import hashlib
import json
import os
import platform
import posixpath
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from peeky.adapters import PythonAdapter, locals_only
from peeky.assembler import ASSEMBLER_REFUSAL, c_refusal
from peeky.build import (
    ArtifactSpec,
    BuildCommandError,
    BuildRecipe,
    Command,
    UnreadDependencies,
    _digest,
    assert_current,
    default_sdk,
    dependency_paths,
    prepare,
    real_tool,
)
from peeky.c_source import Preprocessed, Statements, macro_hazards, returning_twice
from peeky.cxx_strings import (
    SUMMARY_UNITS,
    libstdcxx_string,
    shows_text,
    string_class,
    summary_units,
    text_like,
    tidy,
)
from peeky.digest import sha256_file
from peeky.identifiers import changeable, looks_located, split_located
from peeky.names import standard_type, unicode_escape
from peeky.text_sets import text_kind
from peeky.textlines import octal_string, read_source, source_lines, split_lines

MAX_METADATA = 2 * 1024 * 1024
# Type-rich C++ units (iostream) carry megabytes of DIEs; read only by `describe`.
MAX_DEBUG_INFO = 16 * 1024 * 1024
FLAGS = ("-std=c11", "-g", "-gdwarf-4", "-O0", "-fno-omit-frame-pointer")
CPP_FLAGS = ("-std=c++17", *FLAGS[1:])
OPTIMIZATIONS = ("O0", "Og", "O1", "O2")


def _tool(name):
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError("Invalid native tool executable")
    path = shutil.which(name)
    if not path:
        raise ValueError(f"Missing native tool: {name}")
    return str(real_tool(path))


# What each compiler's `--version` says it is (#167): {(path, mtime, size): family}.
_FAMILIES = {}
# How long a compiler may take to say what it is; past it, its whole group is ended.
VERSION_TIMEOUT = 10
_FAMILIES_LOCK = threading.Lock()
CPP_COMPILER_REFUSAL = (
    "C++ targets need clang++ (Peeky's C++ rules are checked against clang's debug "
    "info); set PEEKY_NATIVE_CXX to clang++"
)


def compiler_family(path):
    """ "clang", "gcc" or "unknown": what ``<path> --version`` says the compiler is,
    asked once per compiler file (its path, modification time and size), never guessed
    from its name (`c++` and `g++` are often clang, `clang++` a wrapper). "timeout" if
    it did not answer within VERSION_TIMEOUT (not kept: asked again next time); it runs
    in a process group of its own, which is ended then, a wrapper's children too."""
    try:
        stat = os.stat(path)
    except OSError:
        return "unknown"
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _FAMILIES_LOCK:
        if key in _FAMILIES:
            return _FAMILIES[key]
    try:
        process = subprocess.Popen(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env={**os.environ, "LC_ALL": "C"},
            start_new_session=True,
        )
    except OSError:
        process, said = None, ""
    if process is not None:
        try:
            out, err = process.communicate(timeout=VERSION_TIMEOUT)
            said = f"{out}\n{err}"
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            # Reaped, never read to the end: a process that left the group could hold
            # the pipes open.
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
            return "timeout"
    if re.search(r"\bclang version\b", said):
        family = "clang"
    elif re.search(
        r"Free Software Foundation|\((?:GCC|[^)]*\bGCC\b[^)]*)\)|^g\+\+|^gcc", said, re.M
    ):
        family = "gcc"
    else:
        family = "unknown"
    with _FAMILIES_LOCK:
        _FAMILIES[key] = family
    return family


def _path_name(name, suffix):
    if (
        not isinstance(name, str)
        or not name.endswith(suffix)
        or name.startswith(("/", "-", "@"))
        or "\\" in name
        or any(part in ("", ".", "..") for part in name.split("/"))
        or any(ord(char) < 32 for char in name)
    ):
        raise ValueError("Expected a plain relative native input path")


def _metadata(tool, arguments, workspace, env, *, max_bytes=MAX_METADATA, timeout=15, read=None):
    """Bound metadata capture; larger engineering probes do not change parser limits.
    ``read(path)`` reads a larger output (up to 64 MiB) as it streams, instead of the
    whole text being returned."""
    limit = (64 if read else 16) * 1024 * 1024
    if type(max_bytes) is not int or not 0 < max_bytes <= limit:
        raise ValueError("Invalid native metadata capture limit")
    if type(timeout) not in (int, float) or not 0 < timeout <= 15:
        raise ValueError("Invalid native metadata time limit")
    with tempfile.TemporaryDirectory(prefix="peeky-symbols-", dir=workspace) as scratch:
        output = Path(scratch) / "dump.txt"
        errors = Path(scratch) / "errors.txt"
        with errors.open("wb") as stderr:
            process = subprocess.Popen(
                [tool, *arguments, "-o", str(output)],
                cwd=workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stderr,
                stderr=stderr,
                start_new_session=True,
            )
            deadline = time.monotonic() + timeout
            exceeded = False
            try:
                while process.poll() is None:
                    if (
                        time.monotonic() >= deadline
                        or errors.stat().st_size > 16000
                        or (output.exists() and output.stat().st_size > max_bytes)
                    ):
                        exceeded = True
                        break
                    time.sleep(0.01)
            finally:
                from peeky.agent import kill_process_group

                kill_process_group(process)
                process.wait(timeout=3)
        if exceeded or process.returncode or not output.is_file():
            size = output.stat().st_size if output.is_file() else 0
            with errors.open("rb") as diagnostics:
                detail = diagnostics.read(2000).decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Native symbol command failed or exceeded its budget: exit={process.returncode}, "
                f"metadata_bytes={size}, capture_limit={max_bytes}, diagnostics={detail}"
            )
        if output.stat().st_size > max_bytes or errors.stat().st_size > 16000:
            raise ValueError("Native symbol metadata exceeds limit")
        # As the tool wrote it: a raw name (`DW_AT_decl_file`) may hold a lone `\r`.
        return read(output) if read else output.read_bytes().decode("utf-8")


def _line_rows(
    text,
    workspace,
    declared,
    limit=MAX_METADATA,
    replaced=None,
    epilogues=None,
    rust=False,
    prologues=None,
):
    """Rows of the LLVM DWARF4 line-table dump for declared sources: (address, name, line).

    Unrecognized rows fail closed. Rows outside the declared sources (SDK and system
    headers) and end-of-sequence rows are skipped. Of the rows a sequence gives one
    address (GCC -O2), LLDB keeps the last: each earlier one of another line or file
    runs no code. It is skipped if it is not a statement; a statement row is kept,
    since a breakpoint can be set on its line, and added to ``replaced`` when given.
    ``epilogues``, when given, gets the address of each row that begins an epilogue;
    ``prologues``, that of each row marked `prologue_end`, in any file (#279).
    """
    if len(text.encode()) > limit:
        raise ValueError("Native line metadata exceeds limit")
    workspace = Path(workspace).resolve()
    inputs = {str((workspace / name).resolve()): name for name in declared}
    # As the compiler counts lines (``rust``: rustc, at `\n` alone; else clang, at a lone
    # `\r` too), told by the caller: a Rust module may have any name (#188 review).
    counts = {name: len(source_lines(read_source(workspace / name), rust)) for name in declared}
    directories, files, current = {0: str(workspace)}, {}, None
    # {file id: declared name or None} for this table: one resolve per file, not per row.
    resolved = {}
    table_count = 0
    version = None
    rows = []
    # The rows of one address in a sequence: (address, name, line, statement, ending).
    group = []

    def flush():
        if not group:
            return
        *earlier, last = group
        for address, name, number, statement, ending in earlier:
            if name is None or ending:
                continue
            if not last[4] and (name, number) == last[1:3]:
                rows.append((address, name, number))
            elif statement:
                rows.append((address, name, number))
                if replaced is not None:
                    replaced.add((address, name, number))
        if last[1] is not None and not last[4]:
            rows.append(last[:3])
        group.clear()

    for raw in split_lines(text):
        line = raw.strip()
        if line.startswith("debug_line["):
            table_count += 1
            if table_count > 4096:
                raise ValueError("Too many native line tables")
            flush()
            directories, files, current = {0: str(workspace)}, {}, None
            resolved = {}
            version = None
        elif match := re.fullmatch(r"version:\s+(\d+)", line):
            version = int(match[1])
            if version != 4:
                raise ValueError("Only explicitly built DWARF4 line tables are supported")
        elif match := re.fullmatch(r"include_directories\[\s*(\d+)\]\s*=\s*(\".*\")", line):
            key = int(match[1])
            if key in directories and key != 0:
                raise ValueError("Duplicate line directory")
            directories[key] = _line_string(match[2])
        elif match := re.fullmatch(r"file_names\[\s*(\d+)\]:", line):
            current = int(match[1])
            if current in files:
                raise ValueError("Duplicate line file identity")
            files[current] = {}
        elif match := re.fullmatch(r"name:\s*(\".*\")", line):
            if current is None:
                raise ValueError("Missing line file identity")
            files[current]["name"] = _line_string(match[1])
        elif match := re.fullmatch(r"dir_index:\s*(\d+)", line):
            if current is None:
                raise ValueError("Missing line file identity")
            files[current]["directory"] = int(match[1])
        elif line.startswith("0x"):
            fields = line.split()
            if version != 4 or len(fields) < 4 or not re.fullmatch(r"0x[0-9a-fA-F]+", fields[0]):
                raise ValueError("Malformed native line row")
            try:
                number, file_id = int(fields[1]), int(fields[3])
                if file_id not in resolved:
                    entry = files[file_id]
                    directory = directories[entry["directory"]]
                    path = (workspace / directory / entry["name"]).resolve()
                    # SDK/system header instructions are not selectable user source.
                    resolved[file_id] = inputs.get(str(path))
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError("Unresolved native line source") from exc
            address, name = int(fields[0], 16), resolved[file_id]
            ending = "end_sequence" in fields or number == 0
            if epilogues is not None and "epilogue_begin" in fields and not ending:
                epilogues.add(address)
            if prologues is not None and "prologue_end" in fields and "end_sequence" not in fields:
                prologues.add(address)
            if name is not None and not ending and not 0 < number <= counts[name]:
                raise ValueError("Native line exceeds declared source length")
            if group and group[0][0] != address:
                flush()
            group.append((address, name, number, "is_stmt" in fields, ending))
            if "end_sequence" in fields:
                # The next sequence may start at this address: its rows are its own.
                flush()
    flush()
    if not table_count:
        raise ValueError("Missing executable native debug symbol line information")
    return rows


def _line_string(quoted):
    """A line table's quoted name (`"main\\342\\200\\250x.c"`): dwarfdump escapes a
    backslash, quote, tab and newline, and writes every other unprintable byte (any
    non-ASCII one) in octal, which JSON does not read (#179)."""
    if (name := _text(quoted)) is None:
        raise ValueError("Unsupported DWARF line table name")
    return name


def parse_lines(text, workspace, declared, *, limit=MAX_METADATA, rust=False):
    """Parse the LLVM DWARF4 line-table dump; unrecognized rows fail closed."""
    result = {name: set() for name in declared}
    for _, name, number in _line_rows(text, workspace, declared, limit, rust=rust):
        result[name].add(number)
    if not any(result.values()):
        raise ValueError("Missing executable native debug symbol line information")
    return {name: sorted(lines) for name, lines in result.items()}


MAX_SCOPES = 300
MAX_DIES = 200_000
_DIE = re.compile(r"(0x[0-9a-fA-F]+):( +)(DW_TAG_\w+|NULL)")
_ATTRIBUTE = re.compile(r" +(DW_AT_\w+)\s+\((.*)\)$")
_RANGES_START = re.compile(r" +DW_AT_ranges\s+\((0x[0-9a-fA-F]+)\s*$")
_RANGE = re.compile(r"\[(0x[0-9a-fA-F]+), (0x[0-9a-fA-F]+)\)")
_REFERENCE = re.compile(r"(0x[0-9a-fA-F]+)(?: \"(.*)\")?")
# Containers that qualify a function's name (`ns::Type::method`).
_QUALIFIERS = {
    "DW_TAG_namespace",
    "DW_TAG_structure_type",
    "DW_TAG_class_type",
    "DW_TAG_union_type",
}
_CODE = ("DW_TAG_subprogram", "DW_TAG_inlined_subroutine")
# Addresses linkers write for discarded code (COMDAT copies, dead functions).
_TOMBSTONE = 0xFFFF_FFFF_0000_0000


# (text, its DIEs) while `_reusing_dies` holds: launch reads one dump twice.
_PARSED = threading.local()


@contextlib.contextmanager
def _reusing_dies():
    _PARSED.last = (None, None)
    try:
        yield
    finally:
        _PARSED.last = None


def _dies(text):
    """A bounded, lenient parse of `dwarfdump --debug-info`, for display only.

    Returns {offset: die} with each die {tag, attrs, parent, children, depth, ranges};
    nesting is read from the indentation (one level per two spaces after the offset).
    """
    last = getattr(_PARSED, "last", None)
    if last is not None and last[0] is text:
        return last[1]
    dies = _parse_dies(text)
    if last is not None:
        _PARSED.last = (text, dies)
    return dies


def _parse_dies(text):
    dies, stack, ranges = {}, [], None
    for raw in split_lines(text):
        if ranges is not None:
            ranges.extend((int(lo, 16), int(hi, 16)) for lo, hi in _RANGE.findall(raw))
            if raw.rstrip().endswith("))") or not _RANGE.search(raw):
                ranges = None
            continue
        if match := _DIE.match(raw):
            if len(dies) >= MAX_DIES:
                raise ValueError("Too many debug-info entries to list function scopes")
            depth = (len(match[2]) - 1) // 2
            del stack[depth:]
            if match[3] == "NULL":
                continue
            offset = int(match[1], 16)
            parent = stack[-1] if stack else None
            dies[offset] = {
                "tag": match[3],
                "attrs": {},
                "parent": parent,
                "children": [],
                "depth": depth,
                "ranges": [],
            }
            if parent is not None:
                dies[parent]["children"].append(offset)
            stack.append(offset)
        elif stack and _RANGES_START.match(raw):
            # A multi-line range list: `[low, high)` rows until the closing `))`.
            ranges = dies[stack[-1]]["ranges"]
        elif stack and (match := _ATTRIBUTE.match(raw)):
            dies[stack[-1]]["attrs"].setdefault(match[1], match[2])
            if match[1] == "DW_AT_ranges":
                found = _RANGE.findall(match[2])
                dies[stack[-1]]["ranges"].extend((int(lo, 16), int(hi, 16)) for lo, hi in found)
    return dies


def _text(value):
    """A dwarfdump string attribute (non-ASCII bytes printed as octal escapes), or None
    where it is none dwarfdump writes (textlines.octal_string)."""
    if value is None or len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return None
    return octal_string(value[1:-1])


def _chain(dies, offset):
    """This DIE, then the DIEs it completes (abstract origin, specification)."""
    chain, seen = [offset], {offset}
    while len(chain) < 8:
        attrs = dies[chain[-1]]["attrs"]
        reference = attrs.get("DW_AT_abstract_origin") or attrs.get("DW_AT_specification")
        match = _REFERENCE.fullmatch(reference or "")
        target = int(match[1], 16) if match else None
        if target not in dies or target in seen:
            break
        chain.append(target)
        seen.add(target)
    return chain


def _first(dies, chain, attribute):
    for offset in chain:
        if attribute in dies[offset]["attrs"]:
            return dies[offset]["attrs"][attribute]
    return None


_RUST_LEGACY = re.compile(r"_ZN[0-9].*17h[0-9a-f]{16}E")
_RUST_ESCAPES = {
    "$LT$": "<",
    "$GT$": ">",
    "$RF$": "&",
    "$BP$": "*",
    "$C$": ",",
    "$SP$": "@",
    "$LP$": "(",
    "$RP$": ")",
}


def _rust_name(linkage):
    """A legacy-mangled Rust symbol, readable and without its hash
    (`crate::Type::method`, `<crate::T as Trait>::fmt`, `crate::main::{{closure}}`);
    None for anything else."""
    if not linkage or not _RUST_LEGACY.fullmatch(linkage):
        return None
    rest, parts = linkage[3:-1], []
    while rest and rest[0].isdigit():
        digits = re.match(r"[0-9]+", rest)[0]
        size = int(digits)
        part, rest = rest[len(digits) : len(digits) + size], rest[len(digits) + size :]
        if len(part) != size:
            return None
        if part.startswith("_$"):
            part = part[1:]
        part = part.replace("..", "::")
        for escape, character in _RUST_ESCAPES.items():
            part = part.replace(escape, character)
        part = re.sub(r"\$u([0-9a-f]{1,6})\$", unicode_escape, part)
        parts.append(part)
    if rest or len(parts) < 2 or not re.fullmatch(r"h[0-9a-f]{16}", parts[-1]):
        return None
    return "::".join(parts[:-1])


def _local_scope(linkage):
    """The enclosing function of a C++ local entity (`_ZZ11apply_twiceiE…` names
    `apply_twice`; `_ZZN3geo3Box4areaEvE…` names `geo::Box::area`), or None."""
    if not linkage or not linkage.startswith("_ZZ"):
        return None
    rest, names = linkage[3:], []
    nested = rest.startswith("N")
    if nested:
        rest = rest[1:].lstrip("KVr")
    while rest[:1].isdigit():
        digits = re.match(r"[0-9]+", rest)[0]
        size = int(digits)
        names.append(rest[len(digits) : len(digits) + size])
        rest = rest[len(digits) + size :]
        if not nested:
            break
    if not names or (nested and not rest.startswith("E")):
        return None
    return "::".join(names)


def typedef_kinds(info):
    """{type name: ("pointer", the name of what it points at, or "") | ("reference", the
    class it refers to, or "") | ("other", the class it names, or "") | ("unknown", "")}:
    what each typedef (or C++ alias) names, through qualifiers and other typedefs.
    lldb-dap shows a variable of one by the typedef's name alone, so `RowPtr rp` cannot
    be told from `RowRef rr` by its type (#102), nor `Index m` (`std::map<int, int>`)
    from a vector (#119). A name for two kinds (a struct and a pointer typedef, or
    typedefs in two units) is "unknown"; for two classes, see ``_merged_class``.
    Plain names are taken only for typedefs at file or function scope; a namespace's or
    class's are known by their qualified names, and every typedef and record also by
    the name lldb-dap shows (``_displayed``), so two entities shown alike are "unknown". A class's member typedef is also kept
    under `::Class::member` (the class's own name, without template arguments), and
    each namespace's name under `namespace::Name`, for ``typedef_kind``'s fallback."""
    dies, found = _dies(info), {}
    named = _typedef_named(dies)

    def note(name, kind):
        if not name:
            return
        known = found.get(name, kind)
        if known == kind:
            found[name] = kind
        elif known[0] == kind[0] and kind[0] in ("other", "reference"):
            found[name] = (kind[0], _merged_class(known[1], kind[1]))
        else:
            found[name] = ("unknown", "")

    def class_named(target):
        """The name lldb-dap shows for the class a DW_AT_type reference names, through
        qualifiers and typedefs, or ""."""
        for _ in range(16):
            reference = _REFERENCE.fullmatch(target or "")
            inner = dies.get(int(reference[1], 16)) if reference else None
            if inner is None:
                return ""
            if inner["tag"] in _RECORDS and inner["tag"] != "DW_TAG_enumeration_type":
                return _displayed(dies, int(reference[1], 16), named) or ""
            if inner["tag"] not in ("DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type"):
                return ""
            target = inner["attrs"].get("DW_AT_type")
        return ""

    def note_member(record_offset, name, kind):
        # A class's member typedef by its class's own name (`::vector::reference`):
        # lldb-dap and the debug information spell the class's scope and template
        # arguments differently (`std::vector<std::vector<int> >` against
        # `std::__1::vector<..., std::__1::allocator<...> >`).
        record = dies[record_offset]
        own = _text(record["attrs"].get("DW_AT_name"))
        for owner in [_without_templates(own)] if own else named.get(record_offset, []):
            key, (sort, target) = f"::{owner}::{name}", kind
            if not (owner and name):
                continue
            known = found.get(key)
            if sort in ("other", "reference") and (known is None or known[0] == sort):
                # The class every instance's typedef names (`Holder<T>::S`, a std::set<int>),
                # or its template's: a set or map behind one is still one (#119).
                found[key] = (sort, target if known is None else _merged_class(known[1], target))
            else:
                found[key] = (sort, "") if known in (None, (sort, "")) else ("unknown", "")

    for offset, die in dies.items():
        if die["tag"] == "DW_TAG_namespace" and (name := _text(die["attrs"].get("DW_AT_name"))):
            found[f"namespace::{name}"] = ("other", "")
            continue
        if die["tag"] in _RECORDS or die["tag"] == "DW_TAG_enumeration_type":
            note(_text(die["attrs"].get("DW_AT_name")), ("other", ""))
            note(_qualified(dies, offset), ("other", ""))
            note(_displayed(dies, offset, named), ("other", ""))
            continue
        if die["tag"] != "DW_TAG_typedef":
            continue
        target, kind = die["attrs"].get("DW_AT_type"), ("other", "")
        for _ in range(16):
            reference = _REFERENCE.fullmatch(target or "")
            inner = dies.get(int(reference[1], 16)) if reference else None
            if inner is None:
                kind = ("unknown", "")
                break
            if inner["tag"] in (
                "DW_TAG_typedef",
                "DW_TAG_const_type",
                "DW_TAG_volatile_type",
                "DW_TAG_restrict_type",
            ):
                target = inner["attrs"].get("DW_AT_type")
                continue
            if inner["tag"] in ("DW_TAG_reference_type", "DW_TAG_rvalue_reference_type"):
                kind = ("reference", class_named(inner["attrs"].get("DW_AT_type")))
            elif inner["tag"] in _RECORDS:
                kind = ("other", class_named(target))
            elif inner["tag"] == "DW_TAG_pointer_type":
                pointee = _REFERENCE.fullmatch(inner["attrs"].get("DW_AT_type") or "")
                aimed = dies.get(int(pointee[1], 16)) if pointee else None
                if aimed is not None and aimed["tag"] == "DW_TAG_subroutine_type":
                    kind = ("other", "")  # a function pointer has no elements
                else:
                    kind = ("pointer", _type_name(dies, inner["attrs"].get("DW_AT_type")) or "")
            break
        parent = dies.get(die["parent"], {})
        name = _text(die["attrs"].get("DW_AT_name"))
        # An unnamed namespace's names show unqualified, as the file's own do.
        if parent.get("tag") in (
            None,
            "DW_TAG_compile_unit",
            "DW_TAG_subprogram",
            "DW_TAG_lexical_block",
        ) or (parent.get("tag") == "DW_TAG_namespace" and "DW_AT_name" not in parent["attrs"]):
            note(name, kind)
        if parent.get("tag") in _RECORDS:
            note_member(die["parent"], name, kind)
        qualified = _qualified(dies, offset)
        if qualified and qualified != name:
            note(qualified, kind)
        note(_displayed(dies, offset, named), kind)
    return found


# A C++ map or set template, its arguments left out (`std::__1::map`, `std::set`).
_ASSOCIATIVE_CLASS = re.compile(
    r"(?:::)?std::(?:__\w+::|pmr::)*(?:unordered_|flat_)?(?:multi)?(?:map|set)"
)


def _merged_class(first, second):
    """The class two typedefs of one name give (``typedef_kinds``): theirs where they
    agree; else, where either is a C++ map or set, the template both instantiate
    (`std::map`), or "?" (it may be a map or set, which one is not known); else ""."""
    if first == second:
        return first
    if "?" in (first, second):
        return "?"
    templates = {_without_templates(name) for name in (first, second)}
    if not any(_ASSOCIATIVE_CLASS.fullmatch(name) for name in templates if name):
        return ""
    return templates.pop() if len(templates) == 1 else "?"


def _without_templates(name):
    """``name`` with every balanced `<...>` removed (`X<int *>::P` is `X::P`)."""
    out, depth = [], 0
    for char in name.replace("->", ""):
        if char == "<":
            depth += 1
        elif char == ">" and depth:
            depth -= 1
        elif not depth:
            out.append(char)
    return "".join(out)


def _qualified(dies, offset, depth=0):
    chain = _chain(dies, offset)
    linkage = _text(_first(dies, chain, "DW_AT_linkage_name"))
    if rust := _rust_name(linkage):
        return rust
    origin = chain[-1]
    name = _text(dies[origin]["attrs"].get("DW_AT_name"))
    if name is None:
        return None
    parts, parent = [name], dies[origin]["parent"]
    while parent is not None and len(parts) < 32:
        die = dies[parent]
        if die["tag"] in _QUALIFIERS and (outer := _text(die["attrs"].get("DW_AT_name"))):
            parts.append(outer)
        elif die["tag"] == "DW_TAG_subprogram" and depth < 8:
            # A closure or lambda inside a function: `main::operator()`. A function that
            # only exists inlined leaves an empty placeholder here; its name is then in
            # the lambda's own local-entity symbol.
            if outer := _qualified(dies, parent, depth + 1) or _local_scope(linkage):
                parts.append(outer)
            break
        parent = die["parent"]
    return "::".join(reversed(parts))


def _displayed(dies, offset, named=None):
    """A type's name as lldb-dap shows it: scoped by its namespaces and classes, but not
    by inline namespaces (`DW_AT_export_symbols`: `v1`, libc++'s `__1`), unnamed ones,
    or the function declaring it (`Node::Link` for `main`'s local `Node`). An unnamed
    class a typedef names (``named``: {offset: name}) shows by that name
    (`typedef struct {...} Anon;`)."""
    named = {offset: names[0] for offset, names in (named or {}).items()}
    name = _text(dies[offset]["attrs"].get("DW_AT_name")) or named.get(offset)
    if name is None:
        return None
    parts, parent = [name], dies[offset]["parent"]
    while parent is not None and len(parts) < 32:
        die = dies[parent]
        if die["tag"] == "DW_TAG_subprogram":
            break
        if (
            die["tag"] in _QUALIFIERS
            and (outer := _text(die["attrs"].get("DW_AT_name")) or named.get(parent))
            and "DW_AT_export_symbols" not in die["attrs"]
        ):
            parts.append(outer)
        parent = die["parent"]
    return "::".join(reversed(parts))


def _typedef_named(dies):
    """{offset: [names]} for each unnamed class or union typedefs in its own scope name
    (`typedef struct { ... } A, B;`), in declaration order: lldb-dap shows it, and its
    members, by the first (`A::P` for `B`'s member too)."""
    named = {}
    for die in dies.values():
        if die["tag"] != "DW_TAG_typedef":
            continue
        reference = _REFERENCE.fullmatch(die["attrs"].get("DW_AT_type") or "")
        target = int(reference[1], 16) if reference else None
        record = dies.get(target)
        if (
            record is not None
            and record["tag"] in _RECORDS
            and "DW_AT_name" not in record["attrs"]
            and record["parent"] == die["parent"]
            and (name := _text(die["attrs"].get("DW_AT_name")))
        ):
            named.setdefault(target, []).append(name)
    return named


# Types that wrap another (`T *`, `const T`, `T[3]`): an unnamed struct, union, class
# or enum inside one is still unnamed.
_TYPE_WRAPPERS = {
    "DW_TAG_pointer_type",
    "DW_TAG_const_type",
    "DW_TAG_volatile_type",
    "DW_TAG_restrict_type",
    "DW_TAG_atomic_type",
    "DW_TAG_reference_type",
    "DW_TAG_rvalue_reference_type",
    "DW_TAG_array_type",
}
_RECORDS = {
    "DW_TAG_structure_type",
    "DW_TAG_union_type",
    "DW_TAG_class_type",
    "DW_TAG_enumeration_type",
}


def _type_name(dies, value, depth=0):
    """The name dwarfdump gives a DW_AT_type reference (`long long`, `Stats *`), or None
    for one no program could write: dwarfdump names an unnamed struct, union, class (a
    lambda) or enum by its tag word (`structure `, `const class  &`, `structure [3]`).
    It prints a restrict pointer as `restrict ` alone, so that is named from its
    pointee (`const float *restrict`)."""
    reference = _REFERENCE.fullmatch(value or "")
    name = _text(f'"{reference[2]}"') if reference and reference[2] else None
    if name is None or depth > 16:
        return None
    offset = int(reference[1], 16)
    if offset not in dies:
        return None if re.search(r"\brestrict\b", name) else name
    if not _balanced(name):
        # dwarfdump cuts some names short (`int (*[1]` for an array of function pointers).
        return None
    if dies[offset]["tag"] == "DW_TAG_restrict_type":
        inner = dies[offset]["attrs"].get("DW_AT_type")
        pointee = _type_name(dies, inner, depth + 1)
        target = _REFERENCE.fullmatch(inner or "")
        if pointee and pointee.endswith("*"):
            return f"{pointee}restrict"
        if pointee and target and dies.get(int(target[1], 16), {}).get("tag") == "DW_TAG_typedef":
            return f"{pointee} restrict"
        # A pointer to an array or function (`double (*restrict m)[n]`): its restrict
        # goes inside the parentheses, which this name cannot say.
        return None
    for _ in range(16):
        die = dies.get(offset)
        if die is None or die["tag"] not in _TYPE_WRAPPERS:
            break
        inner = _REFERENCE.fullmatch(die["attrs"].get("DW_AT_type") or "")
        if inner is None:
            break
        if die["tag"] == "DW_TAG_restrict_type":
            # A qualifier on a restrict pointer: dwarfdump has dropped its pointee.
            return None
        offset = int(inner[1], 16)
    # An unnamed record, or a type declared inside one (`structure ::In *`).
    for _ in range(16):
        die = dies.get(offset)
        if die is None or die["tag"] in ("DW_TAG_compile_unit", "DW_TAG_namespace"):
            break
        if die["tag"] in _RECORDS and "DW_AT_name" not in die["attrs"]:
            return None
        offset = die["parent"]
    return name


def _balanced(name):
    depth = []
    # Rust's `fn(i32) -> i32` returns with an arrow, not a closing bracket.
    for char in name.replace("->", ""):
        if char in "([<":
            depth.append(")]>"["([<".index(char)])
        elif char in ")]>" and (not depth or depth.pop() != char):
            return False
    return not depth


def _locals(dies, offset, hidden=None, label=None):
    """{name: {types}} for the parameters and variables of one function body, including
    nested blocks but not the bodies of calls inlined into it. A type is the name
    dwarfdump gives the DIE's DW_AT_type (`long long`, `Stats *`). ``hidden(offset)``:
    a binding left out (a macro's own, see function_scopes). ``label(offset, name)``:
    the name to list it by (see function_scopes)."""
    names, pending = {}, list(dies[offset]["children"])
    while pending and len(names) < 512:
        child = pending.pop()
        die = dies[child]
        if die["tag"] in ("DW_TAG_formal_parameter", "DW_TAG_variable"):
            if hidden and hidden(child):
                continue
            chain = _chain(dies, child)
            name = _text(_first(dies, chain, "DW_AT_name"))
            # A compiler's own variable (a range `for`'s `__range1`, `__begin1`, a GCC
            # lambda's captures) goes, and GCC's `__closure` (a lambda's object pointer):
            # lldb-dap lists none of them. `this` stays: clang's artificial parameter, and
            # the variable GCC gives a lambda that captures it, which lldb-dap lists.
            if any("DW_AT_artificial" in dies[at]["attrs"] for at in chain) and (
                (die["tag"] == "DW_TAG_variable" and name != "this") or name == "__closure"
            ):
                continue
            # `$`-prefixed names are debugger-synthesized storage, not program locals.
            if name and not name.startswith("$"):
                name = label(child, name) if label else name
                reference = _first(dies, chain, "DW_AT_type")
                kind = _c_tagged(dies, reference, _type_name(dies, reference))
                names.setdefault(name, set()).update({kind} if kind else set())
        elif die["tag"] == "DW_TAG_lexical_block":
            pending.extend(die["children"])
    return names


# Operators whose operand never runs: a call there is no caller.
_UNEVALUATED = frozenset(("sizeof", "_Alignof", "alignof", "typeof", "__typeof__", "__typeof"))


def _arguments(text, opening):
    """(the text inside the parentheses that open at ``opening``, the offset after
    their close), or None where they do not close in ``text``."""
    depth = 0
    for position in range(opening, min(len(text), opening + 65536)):
        char = text[position]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : position], position + 1
    return None


_NONBLANK_CODE = re.compile(r"\S")


def _argument_spans(text, opening):
    """The (start, end) offsets in ``text`` of each argument of the parentheses that
    open at ``opening``, at their top level; None where they do not close."""
    spans, depth, start = [], 0, opening + 1
    for position in range(opening, min(len(text), opening + 65536)):
        char = text[position]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                spans.append((start, position))
                return spans
        elif char == "," and depth == 1:
            spans.append((start, position))
            start = position + 1
    return None


# What may come before the one call in its statement for a stop there to be the last
# word on what the call reads (``_untouched``): `return`, `if (`, `NAME =` (or `op=`), or
# a declaration's `TYPE NAME =`.
_CONTROL = r"(?:if|else|while|for|do|switch|case|default|return|goto|sizeof|break|continue)"
_CALL_LEAD = re.compile(
    r"\s*(?:return\s+|(?P<condition>if)\s*\(\s*|"
    rf"(?:(?!{_CONTROL}\b)[A-Za-z_]\w*(?:\s+|\s*\*[\s*]*))*"
    r"(?P<name>[A-Za-z_]\w*)\s*(?:[-+*/%|^&]|<<|>>)?=\s*)?"
)


# Inline assembly, which may write any memory or register it reaches, operands or not
# (`__asm__("" : "=r"(p->width))`, a store through a register holding a pointer).
_ASSEMBLY = re.compile(r"(?<![\w$])(?:asm|__asm|__asm__)(?![\w$])")
# Builtins may take an address no `&` shows: of a value (`__builtin_addressof(x)`), or of
# a frame (`__builtin_frame_address`, `__builtin_dwarf_cfa`), from which any local of it,
# or of its callers, is reached (review of #211). Every builtin counts but those known to
# reach no memory.
_ADDRESSES = re.compile(
    r"(?<![\w$])__builtin_(?!(?:expect|expect_with_probability|unreachable|assume|trap|"
    r"constant_p|types_compatible_p|choose_expr|offsetof|clzs?|clzl{0,2}|ctzs?|ctzl{0,2}|"
    r"popcountl{0,2}|parityl{0,2}|ffsl{0,2}|bswap(?:16|32|64)|"
    r"(?:add|sub|mul)_overflow|object_size|dynamic_object_size|__\w+_chk)(?![\w$]))[\w$]+"
)
# Operators whose operand is not evaluated: `sizeof x.buf` takes no address.
_UNEVALUATED_BEFORE = re.compile(
    r"(?<![\w$])(?:sizeof|_Alignof|alignof|typeof|__typeof__|__typeof)\s*\(?\s*$"
)


def _under(graph, offset):
    """The type ``offset`` names under its typedefs and qualifiers (fields.type_graph),
    or None past 64 of them."""
    for _ in range(64):
        node = graph.get(offset)
        if node is None or node[0] not in ("typedef", "qualifier"):
            return offset
        offset = node[2]
    return None


def _split_arguments(text):
    """The comma-separated parts of an argument or parameter list, at its top level."""
    parts, depth, start = [], 0, 0
    for position, char in enumerate(text):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:position])
            start = position + 1
    parts.append(text[start:])
    return [part.strip() for part in parts] if any(p.strip() for p in parts) else []


_C_TAGS = {
    "DW_TAG_structure_type": "struct",
    "DW_TAG_union_type": "union",
    "DW_TAG_enumeration_type": "enum",
}


def _c_tagged(dies, value, name):
    """``name``, `_type_name`'s for the DW_AT_type reference ``value``, with C's tag
    word where the type is a C struct, union or enum named by its tag, not by a
    typedef: `struct ring *` for dwarfdump's `ring *`, as C code names it (seventh
    evaluation, finding 17). A typedef's name, a C++ type and a name in which the tag
    is not found exactly once stay as they are."""
    reference = _REFERENCE.fullmatch(value or "")
    if name is None or reference is None:
        return name
    offset = int(reference[1], 16)
    for _ in range(16):
        die = dies.get(offset)
        if die is None or die["tag"] not in _TYPE_WRAPPERS:
            break
        inner = _REFERENCE.fullmatch(die["attrs"].get("DW_AT_type") or "")
        if inner is None:
            return name
        offset = int(inner[1], 16)
    die = dies.get(offset)
    tag = _text(die["attrs"].get("DW_AT_name")) if die and die["tag"] in _C_TAGS else None
    if not tag:
        return name
    unit = die
    for _ in range(64):
        if unit is None or unit["tag"] == "DW_TAG_compile_unit":
            break
        unit = dies.get(unit["parent"])
    language = (unit or {}).get("attrs", {}).get("DW_AT_language") or ""
    if not re.fullmatch(r"DW_LANG_C[0-9]*", language.strip()):
        return name
    found = [m.start() for m in re.finditer(rf"(?<![\w:]){re.escape(tag)}(?![\w:])", name)]
    if len(found) != 1:
        return name
    return f"{name[: found[0]]}{_C_TAGS[die['tag']]} {name[found[0] :]}"


def _bindings(dies, offset):
    """The parameter and variable DIEs of one function body, nested blocks included, the
    bodies of calls inlined into it not (as _locals walks them; at most 512)."""
    found, pending = [], list(dies[offset]["children"])
    while pending and len(found) < 512:
        child = pending.pop()
        tag = dies[child]["tag"]
        if tag in ("DW_TAG_formal_parameter", "DW_TAG_variable"):
            found.append(child)
        elif tag == "DW_TAG_lexical_block":
            pending.extend(dies[child]["children"])
    return found


_MAX_SPANS = 8


def _block_spans(dies, entry, root, addresses, numbers, source_of, hidden=None, label=None):
    """({name: {(first, last)}}, {names visible in the whole function}), or None when
    the declarations were too many to read all: the lines of the blocks declaring each
    local, for the locals declared only inside nested blocks (a `for` loop's `i`), so
    visible only there (#107). A block's lines are those of the function's own rows
    within its code (a local macro's definition lines, where its expansions run, are not
    counted), and the call lines of what is inlined into it (LLDB stops there, at the
    callee's entry, in this frame); one that also runs another file's code (an
    `#include` inside it) gives no span. They are one span unless a line between two of
    them has code only outside the block: C++ puts a block's cleanup, which runs when an
    exception leaves it, on the function's closing line, so a `while` body's locals are
    visible on its lines and on that one (`17-26, 35`), not on the lines between (the
    eval-6 review). ``source_of`` names the declared source of a DW_AT_call_file.
    ``label(offset, name)``: the name to list a binding by (see function_scopes)."""
    rows = sorted(
        (address, source, line) for (source, line), found in addresses.items() for address in found
    )
    keys = [row[0] for row in rows]
    lines, spans, everywhere = {}, {}, set()
    lowest, highest = min(numbers), max(numbers)
    # The function's own lines that run code: where a stop is made at all.
    running = sorted({line for _, source, line in rows if source == entry["source"]} & numbers)

    def span(block):
        if block not in lines:
            covered, other = [], False
            for low, high in _code_ranges(dies[block]):
                for _, source, line in rows[
                    bisect.bisect_left(keys, low) : bisect.bisect_left(keys, high)
                ]:
                    if source != entry["source"]:
                        other = True
                    elif line in numbers:
                        covered.append(line)
                    # The file's other lines are a local macro's definition, expanded here.
            pending = list(dies[block]["children"])
            while pending:
                die = dies[pending.pop()]
                if die["tag"] == "DW_TAG_lexical_block":
                    pending.extend(die["children"])
                elif die["tag"] == "DW_TAG_inlined_subroutine":
                    called = die["attrs"].get("DW_AT_call_line", "")
                    # Its rows are the callee's, so the line may be none of the function's.
                    if (
                        called.isdigit()
                        and lowest <= int(called) <= highest
                        and source_of(die["attrs"].get("DW_AT_call_file")) == entry["source"]
                    ):
                        covered.append(int(called))
            lines[block] = _runs(covered, running) if covered and not other else None
        return lines[block]

    def name_of(child):
        die = dies[child]
        if die["tag"] not in ("DW_TAG_formal_parameter", "DW_TAG_variable"):
            return None
        if hidden and hidden(child):
            return None
        name = _text(_first(dies, _chain(dies, child), "DW_AT_name"))
        if not name or name.startswith("$"):
            return None
        return label(child, name) if label else name

    # The concrete copies carry the code; an abstract or declaring DIE only says which
    # names are the function's own.
    for body in dict.fromkeys(entry["bodies"]):
        pending = [(child, None) for child in dies[body]["children"]]
        while pending:
            if len(spans) + len(everywhere) >= 512:
                return None
            child, block = pending.pop()
            if dies[child]["tag"] == "DW_TAG_lexical_block":
                pending.extend((grandchild, child) for grandchild in dies[child]["children"])
                continue
            if (name := name_of(child)) is None:
                continue
            found = span(block) if block is not None else None
            if found is None:
                # Declared at the function's level, or in a block of unknown lines.
                everywhere.add(name)
            else:
                spans.setdefault(name, set()).update(found)
    if root not in entry["bodies"]:
        everywhere.update(name for child in dies[root]["children"] if (name := name_of(child)))
    return spans, everywhere


def _runs(covered, running):
    """The block's lines ``covered`` as spans ((first, last), ...): split only where a
    line between two of them runs code (``running``, sorted) none of which is the
    block's, so no line the block's code runs on is left out. A later span starts right
    after the last such line: a line with no code before the block's next one (`let s =
    twice!(i);`, whose code is the macro's) stops there, as LLDB moves a breakpoint to
    the next line with code (review of the eval-6 fixes)."""
    ordered = sorted(set(covered))
    found = [[ordered[0], ordered[0]]]
    for line in ordered[1:]:
        at = bisect.bisect_right(running, found[-1][1])
        if at < len(running) and running[at] < line:
            outside = running[bisect.bisect_left(running, line) - 1]
            found.append([outside + 1, line])
        else:
            found[-1][1] = line
    return tuple((first, last) for first, last in found)


def _tidy_spans(found):
    """Sorted spans, overlapping ones joined (shadowed bindings); past ``_MAX_SPANS``,
    one span over them all."""
    joined = []
    for first, last in sorted(found):
        if joined and first <= joined[-1][1]:
            joined[-1][1] = max(joined[-1][1], last)
        else:
            joined.append([first, last])
    if len(joined) > _MAX_SPANS:
        return [[joined[0][0], max(last for _, last in joined)]]
    return joined


def _code_ranges(die):
    attrs = die["attrs"]
    try:
        low = int(attrs["DW_AT_low_pc"], 16)
        high = int(attrs["DW_AT_high_pc"], 16)
    except (KeyError, ValueError):
        spans = die["ranges"]
    else:
        # DWARF4 high_pc may be an offset from low_pc rather than an address.
        spans = [(low, high if high > low else low + high)]
    return [(lo, hi) for lo, hi in spans if 0 < lo < hi and lo < _TOMBSTONE]


# A parameter the caller passed on the stack, at or above the frame base where that is the
# call's frame address (GCC's): the caller's memory, stored before the call.
_PASSED_ON_STACK = re.compile(r"\(?\s*DW_OP_fbreg \+?[0-9]+(?:, DW_OP_deref)?\)?\s*")


def _prologue_kept(dies, offset):
    """The names of the parameters and locals of the function at ``offset`` (its nested
    blocks too) that its prologue leaves alone, each declaration of them: a `static`
    (a fixed or thread-local address, no register or frame in it), one with no one-line
    location (a location list, an optimized build's own account of each address, which
    dwarfdump writes over several lines; or none at all), or a parameter passed on the
    stack (past the eighth on arm64, the sixth on x86-64)."""
    frame_base = dies[offset]["attrs"].get("DW_AT_frame_base") or ""
    cfa = frame_base.strip("() ") == "DW_OP_call_frame_cfa"
    names, pending = {}, list(dies[offset]["children"])
    while pending:
        child = pending.pop()
        die = dies[child]
        if die["tag"] == "DW_TAG_lexical_block":
            pending.extend(die["children"])
            continue
        if die["tag"] not in ("DW_TAG_formal_parameter", "DW_TAG_variable"):
            continue
        name = _text(_first(dies, _chain(dies, child), "DW_AT_name"))
        if not name:
            continue
        location = die["attrs"].get("DW_AT_location")
        kept = (
            location is None
            or bool(_STATIC.search(location) and not re.search(r"DW_OP_(?:fbreg|b?reg)", location))
            or (
                cfa
                and die["tag"] == "DW_TAG_formal_parameter"
                and bool(_PASSED_ON_STACK.fullmatch(location))
            )
        )
        names[name] = names.get(name, True) and kept
    return frozenset(name for name, kept in names.items() if kept)


class PrologueWindow(NamedTuple):
    """Where a function's prologue runs (_prologue_windows)."""

    entry: int
    end: int
    marked: bool
    kept: frozenset
    single: bool
    lines: frozenset


def _prologue_windows(dies, rows, marks):
    """Where each function's prologue runs, before it has stored its parameters (#279):
    a PrologueWindow per function with code on the program's lines (``rows``: their
    (address, source, line), sorted), whether Peeky places it or not (GCC's lambdas name
    no file), sorted by ``entry``, the start of its first range (its low_pc). ``end`` is
    its first row there marked `prologue_end` (``marks``): clang and rustc mark the first
    instruction past the parameters' stores. A build that marks none (GCC's) starts the
    body at the function's second row (at its entry where the prologue is empty), as GDB
    reads it; with no row at its entry, at its first. ``marked``: whether a mark gave it.
    ``kept``: _prologue_kept. ``single``: whether all its code there is on one line (a
    one-line function's). ``lines``: the (source, line) of its rows in the window, where
    the debugger shows a stop there. Only windows that hold code: an empty prologue has
    none."""
    addresses = [row[0] for row in rows]
    marks = sorted(marks or ())
    windows = {}
    for offset, die in dies.items():
        if die["tag"] != "DW_TAG_subprogram":
            continue
        spans = _code_ranges(die)
        if not spans:
            continue
        # Its entry starts its first range (an optimized GCC build's `.cold` part may lie
        # below it); its rows there are what runs first.
        entry, high = spans[0]
        first, last = bisect.bisect_left(addresses, entry), bisect.bisect_left(addresses, high)
        if first == last:
            # No row of the program's sources: not its code.
            continue
        mark = bisect.bisect_left(marks, entry)
        marked = mark < len(marks) and marks[mark] < high
        if marked:
            end = marks[mark]
        elif addresses[first] != entry:
            end = addresses[first]
        else:
            end = addresses[first + 1] if first + 1 < last else high
        if end <= entry:
            continue
        kept = _prologue_kept(dies, offset)
        single = len({row[1:] for row in rows[first:last]}) == 1
        if entry in windows:
            # Two functions at one address (folded copies): the wider window, and only
            # what both keep.
            other = windows[entry]
            end, marked, kept = max(end, other.end), marked and other.marked, kept & other.kept
            single = single and other.single
        inside = rows[first : bisect.bisect_left(addresses, end)]
        lines = frozenset(row[1:] for row in inside)
        windows[entry] = PrologueWindow(entry, end, marked, kept, single, lines)
    return sorted(windows.values(), key=lambda window: window.entry)


def _function_entries(
    info,
    line_text,
    workspace,
    declared,
    replaced=None,
    epilogues=None,
    rust=False,
    prologues=None,
):
    """(dies, {root: entry}, {root: {line: span}}, files, {root: {(source, line): span}})
    for functions in declared sources; see function_scopes. ``replaced``: see
    _line_rows. A span is the lowest and
    highest address of a function's rows on a line. The first mapping keeps rows of the
    function's own file (describe); the last keeps every row the function runs, in any
    declared file (clang runs a header struct's member initializers in its caller).
    ``prologues``, when given, gets _prologue_windows."""
    import bisect

    workspace = Path(workspace).resolve()
    inputs = {str((workspace / name).resolve()): name for name in declared}
    marks = set() if prologues is not None else None
    rows = sorted(
        _line_rows(
            line_text,
            workspace,
            declared,
            replaced=replaced,
            epilogues=epilogues,
            rust=rust,
            prologues=marks,
        )
    )
    addresses = [row[0] for row in rows]
    dies = _dies(info)
    if prologues is not None:
        prologues.extend(_prologue_windows(dies, rows, marks))
    owner = [None] * len(rows)
    runner = [None] * len(rows)
    functions, sources = {}, {}
    for offset, die in dies.items():
        if die["tag"] not in _CODE:
            continue
        spans = _code_ranges(die)
        if not spans:
            continue
        chain = _chain(dies, offset)
        attribute, line, made = _declared_at(dies, chain)
        source = _source_of(attribute, inputs, workspace, sources)
        if source is None and attribute is None and made:
            # A compiler-made member whose class names no source either: its first row's.
            at = bisect.bisect_left(addresses, spans[0][0])
            if at < len(rows) and rows[at][0] < spans[0][1]:
                _, source, line = rows[at]
        if source is None:
            # SDK and std code: never listed, and never claims the caller's rows.
            continue
        root = chain[-1]
        entry = functions.setdefault(
            root, {"bodies": [], "chain": chain, "source": source, "line": line}
        )
        entry["bodies"].append(offset)
        for low, high in spans:
            first = bisect.bisect_left(addresses, low)
            for index in range(first, bisect.bisect_left(addresses, high)):
                # A row belongs to the innermost covering function from its own file:
                # an inlined callee from the same file takes its lines, while std code
                # inlined around the caller's own rows does not.
                if rows[index][1] == source and (
                    owner[index] is None or die["depth"] > owner[index][0]
                ):
                    owner[index] = (die["depth"], root)
                if runner[index] is None or die["depth"] > runner[index][0]:
                    runner[index] = (die["depth"], root)
    lines, runs, starts = {}, {}, {}
    for (address, name, number), claimed, ran in zip(rows, owner, runner):
        for found, key, table in ((claimed, number, lines), (ran, (name, number), runs)):
            if found is not None:
                span = table.setdefault(found[1], {}).setdefault(key, [address, address])
                span[0], span[1] = min(span[0], address), max(span[1], address)
        if ran is not None:
            starts.setdefault(ran[1], {}).setdefault((name, number), []).append(address)
    return dies, functions, lines, (inputs, sources), runs, starts


def _source_of(attribute, inputs, workspace, cache):
    """The declared source a DW_AT_decl_file names, or None; resolved once per name
    (an STL-heavy unit names the same few headers thousands of times)."""
    if attribute not in cache:
        decl = _text(attribute)
        cache[attribute] = inputs.get(str((workspace / decl).resolve())) if decl else None
    return cache[attribute]


def _declared_at(dies, chain):
    """(DW_AT_decl_file, its line or None, whether the compiler made it as a member of a
    class) for a function's DIE chain. GCC gives a lambda's `operator()` and a class's
    implicit members (an unnamed class's `<constructor>`) `DW_AT_artificial` and no file
    or line (#167): theirs are their class's, the closure's for a lambda, as clang gives
    them. A made member whose class has none is placed by its rows (_function_entries)."""
    attribute, line = _first(dies, chain, "DW_AT_decl_file"), None
    made = False
    if attribute is None and any("DW_AT_artificial" in dies[at]["attrs"] for at in chain):
        for at in chain:
            parent = dies.get(dies[at]["parent"])
            if parent is not None and parent["tag"] in _CLASSES:
                made = True
                attribute = parent["attrs"].get("DW_AT_decl_file")
                line = parent["attrs"].get("DW_AT_decl_line") if attribute else None
                break
    else:
        line = _first(dies, chain, "DW_AT_decl_line")
    return attribute, int(line) if line and line.isdigit() else None, made


def _own_lines(dies, entry, numbers, macro_lines=frozenset()):
    """A function's line numbers from its declaration on, or None if it has none. Lines
    of a local `macro_rules!` definition (``macro_lines``), where its expansions run,
    are not its own unless it is declared there."""
    if not numbers:
        return None
    first = entry.get("line")
    if first is not None:
        numbers = {number for number in numbers if number >= first} or numbers
    if first not in macro_lines:
        # A macro defined inside the function (between its other lines) keeps them.
        outside = {number for number in numbers if number not in macro_lines}
        if outside:
            low, high = min(outside), max(outside)
            numbers = {number for number in numbers if number in outside or low < number < high}
    return numbers


_MACRO_RULES = re.compile(r"\bmacro_rules!\s*[A-Za-z_][A-Za-z0-9_]*\s*([{(\[])")
_CLOSING = {"{": "}", "(": ")", "[": "]"}


def _macro_rules_lines(text):
    """The line numbers of each `macro_rules!` definition in a Rust source (rustc keeps
    the definition's lines for code it expands), or None when one cannot be told: an
    unclosed literal, comment or bracket."""
    if "macro_rules" not in text:
        return set()
    from peeky.rust_lexer import code_only

    code = code_only(text)
    if code is None:
        return None
    lines = set()
    for found in _MACRO_RULES.finditer(code):
        opening, depth = found[1], 0
        for index in range(found.start(1), len(code)):
            if code[index] == opening:
                depth += 1
            elif code[index] == _CLOSING[opening]:
                depth -= 1
                if not depth:
                    break
        else:
            return None
        first = code.count("\n", 0, found.start()) + 1
        lines.update(range(first, code.count("\n", 0, index) + 2))
    return lines


def function_scopes(
    info, line_text, workspace, declared, hidden=None, never=None, settable=None, rust=False
):
    """Each function in a declared source: qualified name, source, line span, locals.

    Display only (`describe`). A line-table row belongs to the innermost function or
    inlined call whose address range covers it, so an inlined callee's lines count for
    the callee, not its caller. Copies of one function (C1/C2 constructors, inlined and
    out-of-line copies) are merged through the DIE that declares them.

    ``hidden(linkages, declared)``: for one function, given its copies' linkage names
    and the (source, name, line) of every parameter and variable its debug information
    declares (source None for another file), a test (source, name, line) of which are
    left out as a macro's own bindings (Rust: `println!`'s `args`), or None.

    ``never(linkages, names)``: of a copy's locals (``names``: {name: types}), those no
    stop can change, and why (#138: {name: `register`}, a Rust local kept in a
    register); a scope lists the ones never settable in every copy (``never_settable``).

    ``settable``: (the standard library's namespaces, ``in_memory(linkages, name)`` or
    None): the struct and array locals with an integer field or element `set` may
    change, judged from the type as `set` judges it (fields.settable_parts), in the
    frame's own memory, and where ``in_memory`` says so (Rust: the IR keeps it there):
    {name: [the paths it may change: `board.entries`, `bins[*].hi`]} in every copy
    (``settable_parts``, seventh evaluation, finding 16; #183).

    ``rust``: the program is Rust; its sources' lines are counted as rustc counts them
    (at `\n` alone), and `macro_rules!` bodies are found in them.
    """
    replaced = set()
    dies, functions, lines, (inputs, sources), _, addresses = _function_entries(
        info, line_text, workspace, declared, replaced, rust=rust
    )
    workspace = Path(workspace).resolve()
    graph, typed = None, {}
    if settable is not None:
        from peeky.fields import local_types, type_graph

        typed = {
            root: local_types(dies, entry["bodies"], through=True)
            for root, entry in functions.items()
        }
        kinds = {
            kind
            for types in typed.values()
            for each in (types or {}).values()
            for kind, _, _ in each
        }
        graph = type_graph(dies, kinds, settable[0])

    def source_of(attribute):
        return _source_of(attribute, inputs, workspace, sources)

    def position(offset):
        chain = _chain(dies, offset)
        line = _first(dies, chain, "DW_AT_decl_line")
        return (
            source_of(_first(dies, chain, "DW_AT_decl_file")),
            _text(_first(dies, chain, "DW_AT_name")),
            int(line) if line and line.isdigit() else None,
        )

    def skipped(entry, root):
        """The test of which of this function's bindings are left out (see hidden)."""
        if hidden is None:
            return None
        copies = (*entry["bodies"], root)
        linkages = {
            _text(_first(dies, _chain(dies, body), "DW_AT_linkage_name")) for body in copies
        }
        declared_here = {position(offset) for body in copies for offset in _bindings(dies, body)}
        test = hidden(linkages, declared_here)
        return None if test is None else (lambda offset: test(*position(offset)))

    def linkages_of(entry, root):
        return {
            _text(_first(dies, _chain(dies, body), "DW_AT_linkage_name"))
            for body in (*entry["bodies"], root)
        }

    found, macros = {}, {}
    for root, entry in functions.items():
        source = entry["source"]
        name = _qualified(dies, entry["bodies"][0])
        if source not in macros:
            macros[source] = (
                _macro_rules_lines(read_source(Path(workspace) / source, "replace"))
                if rust
                else set()
            )
        numbers = _own_lines(dies, entry, lines.get(root), macros[source] or frozenset())
        if name is None or not numbers:
            continue
        names, skip = {}, skipped(entry, root)
        for body in (*entry["bodies"], root):
            for local, kinds in _locals(dies, body, skip).items():
                names.setdefault(local, set()).update(kinds)
        label, ambiguous = (
            _shadow_labels(dies, (*entry["bodies"], root), skip, position) if rust else (None, {})
        )
        shown = {}
        for body in (*entry["bodies"], root):
            for local, kinds in _locals(dies, body, skip, label).items():
                shown.setdefault(local, set()).update(kinds)
        scope = {
            "scope": name,
            "source": source,
            "lines": [min(numbers), max(numbers)],
            "names": sorted(shown),
            # A selector names none of several bindings on one line (#215 review).
            **({"ambiguous": dict(sorted(ambiguous.items()))} if ambiguous else {}),
        }
        # Instantiations of one generic function (Rust's names drop the generic
        # arguments) are one scope; their types join (`&[i32] or &[char]`), and so do
        # their blocks' lines (code a constant condition removes differs between them).
        merged = found.setdefault(
            json.dumps(scope, sort_keys=True), (scope, {}, {}, set(), [None], [None])
        )
        for local, kinds in shown.items():
            merged[1].setdefault(local, set()).update(kinds)
        if graph is not None:
            parts = _settable_parts(
                graph, typed.get(root), names, settable[1], linkages_of(entry, root)
            )
            held = merged[5][0]
            merged[5][0] = (
                parts if held is None else {n: p for n, p in held.items() if parts.get(n) == p}
            )
        if never is not None:
            # Never settable in the scope only if in every copy (a generic's instances).
            # By the names the scope lists (`x @ main.rs:6` for one of several, #206);
            # never one no selector names.
            unchanged = dict(
                never(
                    linkages_of(entry, root),
                    {n: k for n, k in shown.items() if n not in ambiguous},
                )
                or {}
            )
            held = merged[4][0]
            merged[4][0] = (
                unchanged
                if held is None
                else {name: why for name, why in held.items() if name in unchanged}
            )
        # Rows a later row at their address replaces run no code.
        ran = {
            key: [address for address in found if (address, *key) not in replaced]
            for key, found in addresses.get(root, {}).items()
        }
        declared_at = entry.get("line")
        # A function a macro declares runs its arguments' code on the invoking line,
        # past other functions: its blocks' lines are not one span.
        in_macro = declared_at in (macros[source] or ())
        blocks = (
            _block_spans(dies, entry, root, ran, numbers, source_of, skip, label)
            if macros[source] is not None and not in_macro
            else None
        )
        if blocks is None:
            merged[3].add(None)
            continue
        for local, spans in blocks[0].items():
            merged[2].setdefault(local, set()).update(spans)
        merged[3].update(blocks[1])
    scopes = []
    for scope, kinds, spans, everywhere, unchanged, parts in found.values():
        # Two blocks may declare one name with different types (`int or long`).
        types = {
            local: " or ".join(sorted({standard_type(kind) for kind in k}))
            for local, k in sorted(kinds.items())
            if k
        }
        spans = {
            local: _tidy_spans(found_spans)
            for local, found_spans in sorted(spans.items())
            if local not in everywhere and None not in everywhere
        }
        scopes.append(
            {
                **scope,
                **({"types": types} if types else {}),
                **({"spans": spans} if spans else {}),
                **({"never_settable": dict(sorted(unchanged[0].items()))} if unchanged[0] else {}),
                **(
                    {"settable_parts": {n: list(p) for n, p in sorted(parts[0].items())}}
                    if parts[0]
                    else {}
                ),
            }
        )
    scopes.sort(key=lambda s: (s["source"], s["lines"][0], s["scope"]))
    if len(scopes) > MAX_SCOPES:
        scopes = [*scopes[:MAX_SCOPES], {"omitted": len(scopes) - MAX_SCOPES}]
    return scopes


def _shadow_labels(dies, bodies, hidden, position):
    """The test ``label(offset, name)`` that names a Rust binding as lldb-dap does where
    another of its name is in scope too (`points @ main.rs:81`), for each name the
    function declares more than once (#206): `describe` lists each by it. A binding
    declared outside the declared sources keeps its plain name. With it, {label: how
    many bindings} for each label several bindings share (two `let x` on one line, or
    files of one name in two directories): a selector cannot tell them apart."""
    places, pending = (
        {},
        [child for body in dict.fromkeys(bodies) for child in dies[body]["children"]],
    )
    while pending and len(places) < 512:
        child = pending.pop()
        die = dies[child]
        if die["tag"] == "DW_TAG_lexical_block":
            pending.extend(die["children"])
            continue
        if die["tag"] not in ("DW_TAG_formal_parameter", "DW_TAG_variable"):
            continue
        if hidden and hidden(child):
            continue
        source, name, line = position(child)
        if name and not name.startswith("$"):
            # One binding, however many DIEs complete it (an abstract origin's copies).
            places.setdefault(name, {})[_chain(dies, child)[-1]] = (source, line)

    def label(offset, name):
        if len(places.get(name, ())) < 2:
            return name
        source, _, line = position(offset)
        return f"{name} @ {Path(source).name}:{line}" if source and line else name

    shared = {}
    for name, bindings in places.items():
        if len(bindings) > 1:
            for source, line in bindings.values():
                if source and line:
                    key = f"{name} @ {Path(source).name}:{line}"
                    shared[key] = shared.get(key, 0) + 1
    return label, {key: count for key, count in shared.items() if count > 1}


def _settable_parts(graph, types, names, in_memory, linkages):
    """{name: the paths `set` may change in it} of one function copy's shown locals
    (``names``) that are one struct or array local, of one type, in the frame's own
    memory (an automatic or a parameter there), with an integer field or element `set`
    may change (see function_scopes, fields.settable_parts)."""
    from peeky.fields import settable_parts

    found = {}
    for name, declared in (types or {}).items():
        kinds = {kind for kind, _, _ in declared}
        storages = {storage for _, storage, _ in declared}
        if name not in names or len(kinds) != 1 or None in kinds:
            continue
        if storages - {"automatic", "parameter"}:
            continue
        if (label := settable_parts(graph, next(iter(kinds)), name)) is None:
            continue
        if in_memory is None or in_memory(linkages, name):
            found[name] = label
    return found


# A local in the frame: DW_OP_fbreg (frame base) or DW_OP_bregN (a register) offsets.
_AUTOMATIC = re.compile(r"DW_OP_(?:fbreg|breg[0-9]+)\b")


# A static or thread-local: listed among globals, never a frame's locals.
_STATIC = re.compile(r"DW_OP_addr\b|DW_OP_(?:GNU_push_tls_address|form_tls_address)\b")


def _declared(dies, offset, files, workspace, parameters=None, unstored=None):
    """(name, (source, line, automatic), scope) for each parameter and variable of one
    function body, including nested blocks but not calls inlined into it; ``scope`` is
    the DIE of the block (or body) declaring it. Parameters, statics and anything
    without a frame location are not automatic. A label is listed as (None, None,
    None): `goto` can return above a declaration with the local still live.
    ``parameters``, when given, gets each parameter's name -> {(source, line)} (#158:
    LLDB names one shadowed by a local `t @ param.c:3`). ``unstored``, when given, gets
    (name, source, line) of each variable that is neither automatic nor a static: one
    an optimized build keeps in registers, as a constant, or nowhere, which lldb-dap
    still lists among the frame's locals (#173); a block's `static` it keeps as a
    constant (`DW_AT_const_value`, no address) is one too, listed as a local."""
    found, pending = [], list(dies[offset]["children"])
    while pending and len(found) < 4096:
        child = pending.pop()
        die = dies[child]
        if die["tag"] == "DW_TAG_lexical_block":
            pending.extend(die["children"])
            continue
        if die["tag"] == "DW_TAG_label":
            found.append((None, None, None))
            continue
        if die["tag"] not in ("DW_TAG_formal_parameter", "DW_TAG_variable"):
            continue
        chain = _chain(dies, child)
        name = _text(_first(dies, chain, "DW_AT_name"))
        if not name or name.startswith("$"):
            continue
        inputs, cache = files
        if die["tag"] == "DW_TAG_formal_parameter":
            found.append((name, (None, None, False), die["parent"]))
            if parameters is not None:
                where = _source_of(_first(dies, chain, "DW_AT_decl_file"), inputs, workspace, cache)
                number = _first(dies, chain, "DW_AT_decl_line")
                if where is not None and number and number.isdigit():
                    parameters.setdefault(name, set()).add((where, int(number)))
            continue
        source = _source_of(_first(dies, chain, "DW_AT_decl_file"), inputs, workspace, cache)
        line = _first(dies, chain, "DW_AT_decl_line")
        location = die["attrs"].get("DW_AT_location") or ""
        automatic = bool(_AUTOMATIC.search(location)) and "DW_OP_addr" not in location
        where = (source, int(line) if line and line.isdigit() else None, automatic)
        found.append((name, where, die["parent"]))
        if (
            unstored is not None
            and not automatic
            and not _STATIC.search(location)
            and "DW_AT_external" not in die["attrs"]
        ):
            unstored.add((name, *where[:2]))
    return found


def local_declarations(info, line_text, workspace, declared, prologues=None):
    """Each function in a declared source: its source; ``rows``, the lowest and highest
    address of its code on each (source, line) it runs, in any declared file and even
    above its declaration (member initializers run where their class is built); for each local
    name, where every local of that name is declared, as (source, line, automatic);
    and whether it has a label, where `goto` can return above a declaration.

    ``starts``: for each automatic local, (name, source, line) -> the addresses where
    control enters its declaration line's code in the block declaring it: the line's
    first row in that block, and on a function's first line also the row after the
    prologue, where its breakpoint is. ``ranges``: the function's code. ``scopes``: for
    each automatic local, (name, source, line) -> the code ranges of the block declaring
    it. ``root``: the function's DIE; ``constructor``, ``enclosing`` and ``defined``:
    see _class_role. ``span``: its own lines in its source, or None. ``order``: every row
    it runs, (address, source, line) by address. ``classes``: _local_classes.
    ``types``: fields.local_types, the type and storage of each local (#105);
    ``referred``: fields.referred_locals, those the frame reaches through a pointer.
    ``prologues``, when given, gets _prologue_windows: every function's, placed or not."""
    from peeky.fields import local_types, referred_locals

    epilogues = set()
    dies, functions, _, files, runs, addresses = _function_entries(
        info, line_text, workspace, declared, epilogues=epilogues, prologues=prologues
    )
    workspace = Path(workspace).resolve()
    classes = _local_classes(dies)
    table = []
    for root, entry in functions.items():
        rows = runs.get(root)
        if not rows:
            continue
        ranges = sorted(span for body in entry["bodies"] for span in _code_ranges(dies[body]))
        entries = {min(span)[0] for body in entry["bodies"] if (span := _code_ranges(dies[body]))}
        names, jumps, starts, scopes = {}, False, {}, {}
        # Every row this function runs, by address: what runs just before a line's start.
        ordered = sorted(
            (address, source, line)
            for (source, line), found in addresses.get(root, {}).items()
            for address in found
        )
        visible, parameters, unstored = {}, {}, set()
        for body in (*entry["bodies"], root):
            for name, where, scope in _declared(dies, body, files, workspace, parameters, unstored):
                if name is None:
                    jumps = True
                    continue
                names.setdefault(name, set()).add(where)
                # Where the debugger lists it: the code of the block declaring it.
                visible.setdefault((name, *where), set()).update(
                    _code_ranges(dies[scope]) if scope in dies else ()
                )
                source, line, automatic = where
                if not automatic or scope not in dies:
                    continue
                block = _code_ranges(dies[scope])
                scopes.setdefault((name, source, line), set()).update(block)
                found = sorted(
                    address
                    for address in addresses.get(root, {}).get((source, line), ())
                    if any(low <= address < high for low, high in block)
                )
                if not found:
                    continue
                index = bisect.bisect_left(ordered, (found[0],)) - 1
                before = ordered[index] if index >= 0 else None
                if before is not None and (before[1] != source or before[2] > line):
                    # Code of a later line runs straight into this one (a header over
                    # several lines: `for (` then `i = 10; i < 13; i++)`), so control can
                    # reach the first row with the local already assigned.
                    continue
                first = {found[0]}
                if found[0] in entries and (later := [a for a in found if a > found[0]]):
                    first.add(later[0])
                starts.setdefault((name, source, line), set()).update(first)
        # Where the function's code after its return starts: its last epilogue's row ends
        # there, and what follows (clang's exception landing pads and cleanups at -O0)
        # runs only when an exception unwinds through it. None without an epilogue row.
        ended = [
            address for address in epilogues if any(low <= address < high for low, high in ranges)
        ]
        returned = None
        if ended:
            later = bisect.bisect_right(ordered, (max(ended), "\U0010ffff", 1 << 62))
            returned = ordered[later][0] if later < len(ordered) else None
        constructor, enclosing, defined, initializer = _class_role(dies, entry["chain"])
        inlined = _inlined_calls(dies, (*entry["bodies"], root), files, workspace)
        first = entry.get("line")
        own = [
            n for (where, n), _ in rows.items() if where == entry["source"] and n >= (first or 0)
        ]
        table.append(
            {
                "source": entry["source"],
                # Its qualified name, for messages (#138).
                "name": _qualified(dies, entry["bodies"][0]),
                "rows": {number: tuple(span) for number, span in rows.items()},
                "locals": names,
                "jumps": jumps,
                "starts": starts,
                "ranges": tuple(ranges),
                "scopes": scopes,
                "root": root,
                "constructor": constructor,
                "enclosing": enclosing,
                "defined": defined,
                "initializer": initializer,
                # Its own lines: from its declaration to its last row there (its `}`).
                "span": (first, max(own)) if first is not None and own else None,
                # Every row it runs, by address: (address, source, line).
                "order": tuple(ordered),
                "classes": classes.get(root, ()),
                "types": local_types(dies, entry["bodies"], through=True),
                # Its locals the frame reaches through a pointer (#181 review).
                "referred": referred_locals(dies, entry["bodies"]),
                "inlined": inlined,
                # (name, source, line, automatic) -> the code ranges of the block declaring
                # it (the function's for a parameter): where lldb-dap lists that local.
                "visible": {key: tuple(sorted(ranges)) for key, ranges in visible.items()},
                # Each parameter's name -> {(source, line)} (#158).
                "parameters": parameters,
                # (name, source, line) of the locals an optimized build keeps outside
                # the frame's memory, which lldb-dap lists all the same (#173).
                "unstored": frozenset(unstored),
                # Rows at or after this address run only as an exception unwinds (#158).
                "returned": returned,
            }
        )
    return table


def _inlined_calls(dies, bodies, files, workspace):
    """(source or None, line or None) of each call inlined into these function bodies
    (`DW_TAG_inlined_subroutine`: a callee the compiler inlined even at opt-level 0, as
    `always_inline` and `flatten` make it), from
    where it is called: its code carries the callee's lines, not the call's (#105)."""
    inputs, cache = files
    found, pending = (
        set(),
        [child for body in dict.fromkeys(bodies) for child in dies[body]["children"]],
    )
    while pending and len(found) < 4096:
        die = dies[pending.pop()]
        if die["tag"] == "DW_TAG_lexical_block":
            pending.extend(die["children"])
        elif die["tag"] == "DW_TAG_inlined_subroutine":
            called = die["attrs"].get("DW_AT_call_line", "")
            source = _source_of(die["attrs"].get("DW_AT_call_file"), inputs, workspace, cache)
            found.add((source, int(called) if called.isdigit() else None))
    return tuple(sorted(found, key=str))


_CLASSES = ("DW_TAG_class_type", "DW_TAG_structure_type", "DW_TAG_union_type")
# A lambda's call operator: `operator()`, a generic lambda's `operator()<int>` (clang and
# GCC name an instance of a member template with its arguments).
_CALL_OPERATOR = re.compile(r"operator\(\)(?:<.*>)?")


def _calls_closure(dies, chain):
    """Whether a function's DIE chain is a lambda's call operator: by its name, or by its
    object pointer, which GCC names `__closure` (clang names it `this`)."""
    if _CALL_OPERATOR.fullmatch(_text(_first(dies, chain, "DW_AT_name")) or ""):
        return True
    reference = _REFERENCE.fullmatch(_first(dies, chain, "DW_AT_object_pointer") or "")
    pointer = dies.get(int(reference[1], 16)) if reference else None
    return pointer is not None and _text(pointer["attrs"].get("DW_AT_name")) == "__closure"


def _local_classes(dies):
    """{function root: ((line, closure), ...)}: the classes, structs and unions defined
    inside each function (not just declared), by the line the debug information gives,
    and whether it is a lambda's closure (no plain name; a call operator, _calls_closure,
    or GCC's constructor or destructor of a closure, `<lambda>`, `~<lambda>`, where it
    folded every call away): its body is a function of its own, not text the function
    runs."""
    found = {}
    for offset, die in dies.items():
        if die["tag"] not in _CLASSES or "DW_AT_declaration" in die["attrs"]:
            continue
        outer, owner = die, None
        for _ in range(32):
            above = outer["parent"]
            outer = dies.get(above)
            if outer is None:
                break
            if outer["tag"] in _CODE:
                owner = _chain(dies, above)[-1]
                break
        if owner is None:
            continue
        line = die["attrs"].get("DW_AT_decl_line")
        # Clang leaves a closure unnamed; GCC may name it `<lambda(...)>`.
        named = _text(die["attrs"].get("DW_AT_name")) or ""
        closure = not re.fullmatch(r"[A-Za-z_]\w*", named) and any(
            dies[child]["tag"] == "DW_TAG_subprogram"
            and (
                _calls_closure(dies, [child])
                or _text(dies[child]["attrs"].get("DW_AT_name")) in ("<lambda>", "~<lambda>")
            )
            for child in die["children"]
        )
        found.setdefault(owner, []).append(
            (int(line) if line and line.isdigit() else None, closure)
        )
    return {owner: tuple(sorted(set(places), key=str)) for owner, places in found.items()}


def _class_role(dies, chain):
    """(constructor, enclosing, defined, initializer) for a function's DIE chain:
    whether it is a constructor of its class (`C::C`, its name the class's without
    template arguments; of an unnamed class, one named as the class, or an artificial
    member named so no other member can be, with no return type: clang's `$_0::$_0()`,
    GCC's `<constructor>`); for a member of a class defined inside a function (a
    lambda's `operator()`, a local class's method), that function's root DIE and the
    class's line, else None; and whether it is a lambda's `operator()` (_calls_closure)
    whose closure a class's scope holds (a default member initializer's)."""
    for offset in chain:
        parent = dies.get(dies[offset]["parent"])
        if parent is None or parent["tag"] not in _CLASSES:
            continue
        name = _text(_first(dies, chain, "DW_AT_name"))
        owner = _text(parent["attrs"].get("DW_AT_name"))
        constructor = bool(name and owner and name == re.sub(r"<.*>$", "", owner))
        unnamed = not owner or not re.fullmatch(r"[A-Za-z_]\w*(?:<.*>)?", owner)
        if unnamed and not constructor and _first(dies, chain, "DW_AT_type") is None:
            # Clang names an unnamed struct's constructor `(unnamed struct at f.cpp:55:5)`,
            # GCC `<constructor>` (a closure's `<lambda>`). GCC makes a closure's
            # `operator()` and `_FUN` artificial too, and they may return nothing.
            artificial = any("DW_AT_artificial" in dies[at]["attrs"] for at in chain)
            special = not name or name.startswith(("(", "<"))
            constructor = ((artificial and special) or name == owner) and not (
                name or ""
            ).startswith("~")
        closure = unnamed and _calls_closure(dies, chain)
        initializer = closure and dies.get(parent["parent"], {}).get("tag") in _CLASSES
        enclosing, outer = None, parent
        for _ in range(16):
            above = outer["parent"]
            outer = dies.get(above)
            if outer is None:
                break
            if outer["tag"] in _CODE:
                enclosing = _chain(dies, above)[-1]
                break
            if outer["tag"] not in _CLASSES:
                break
        line = parent["attrs"].get("DW_AT_decl_line")
        defined = int(line) if line and line.isdigit() else None
        return constructor, enclosing, defined, initializer
    return False, None, None, False


# Non-local jumps: after one returns, code above a declaration can run again with the
# local live (C11 7.13.2.1). Searched in the executable (imports, wherever the call is
# hidden) and in the sources (`__builtin_setjmp` leaves no symbol).
_NONLOCAL_JUMPS = (b"setjmp", b"getcontext", b"swapcontext")


def mentions_nonlocal_jumps(paths, names=_NONLOCAL_JUMPS):
    """Whether any of these source files names a non-local jump (each read once)."""
    for path in paths:
        text = Path(path).read_bytes()
        if any(name in text for name in names):
            return True
    return False


def has_nonlocal_jumps(binary):
    """Whether the executable names setjmp/sigsetjmp or a ucontext switch anywhere
    (its imports), wherever the call is hidden (a TRY macro in a library header)."""
    tail = b""
    with Path(binary).open("rb") as stream:
        while chunk := stream.read(1 << 20):
            window = tail + chunk
            if any(name in window for name in _NONLOCAL_JUMPS):
                return True
            tail = chunk[-16:]
    return False


# The functions a signal handler returns through (macOS, glibc's x86-64 and older ones,
# the arm64 and 32-bit Linux vDSO's; GDB's name for the frame): the frame above one was
# interrupted at an instruction, not suspended in a call.
_TRAMPOLINES = frozenset(
    (
        "_sigtramp",
        "sigtramp",
        "__restore_rt",
        "__restore",
        "__kernel_rt_sigreturn",
        "__kernel_sigreturn",
        "<signal handler called>",
    )
)


def _trampoline(name):
    """Whether a frame's name (`_sigtramp`, `libsystem_platform.dylib`_sigtramp`,
    `__restore_rt()`) is a signal trampoline's."""
    if not isinstance(name, str):
        return False
    name = name.strip()
    if name in _TRAMPOLINES:
        return True
    bare = name.rpartition("`")[2].split("(", 1)[0].strip()
    return bare in _TRAMPOLINES


# A function of the C++ standard library, by its name before its parameters (after a
# function template's return type: `void std::_Destroy<Noisy>(Noisy*)`).
_LIBRARY_FUNCTION = re.compile(r"(?:^|[\s*&])(?:::)?(?:std|__gnu_cxx|__gnu_debug)::")


# The frames read to rule out a standard-library call below a container reached through
# a pointer or reference (#181, #187): a deeper stack is not judged.
STACK_READ = 1024


def _whole(frames, asked):
    """Whether a `stackTrace` reply of ``asked`` frames holds the whole stack: its own
    ``complete`` where the worker kept it (fewer frames than asked for, and none more
    in its `totalFrames`, #187 review), else fewer frames than asked for."""
    complete = getattr(frames, "complete", None)
    if isinstance(complete, bool):
        return complete
    return frames is not None and len(frames) < asked


def _short_frame(name):
    """A frame's function for a reason, at most 80 characters: its name without its
    parameters, return type, ABI tags or template arguments
    (`std::__1::vector<…>::clear`)."""
    if not isinstance(name, str) or not name.strip():
        return "a frame with no name"
    bare = re.sub(r"\[abi:[^\]]*\]", "", name.rpartition("`")[2])
    for _ in range(32):
        folded = re.sub(r"<[^<>]*>", "\x00", bare)
        if folded == bare:
            break
        bare = folded
    bare = (bare.split("(", 1)[0].split() or [name])[-1].replace("\x00", "<…>")
    return bare if len(bare) <= 80 else bare[:79] + "…"


def _not_framed(name, storages):
    """Why a local outside the frame's own memory is not changed: one the frame reaches
    through a pointer or reference ("through", fields.local_types), or a static or a
    value in registers."""
    if "through" in storages:
        return (
            f"`{name}` is reached through a pointer or reference (a C++ structured binding "
            "of a reference, a named return value or a C `__block` variable). Change the "
            "value where it is owned: Peeky changes only values the frame holds itself"
        )
    return (
        f"`{name}` is not kept in this frame's own memory (a static, or a value the debug "
        "information places in registers)"
    )


def _library_frame(frame):
    """Whether a frame runs a function of the C++ standard library (`std::__1::vector<…>
    ::clear()`, libstdc++'s `std::_Rb_tree_insert_and_rebalance(…)`, a module's
    `libc++.1.dylib`std::…`), which may be part-way through changing a container (#181);
    a frame with no name counts as one."""
    name = (frame or {}).get("name")
    if not isinstance(name, str) or not name.strip():
        return True
    return bool(_LIBRARY_FUNCTION.search(name.rpartition("`")[2].split("(", 1)[0]))


# A narrow character array's type, as LLDB names it (`char[8192]`, `const unsigned char[4]`).
CHAR_ARRAY = re.compile(
    r"(?:(?:const|volatile)\s+)*(?:(?:signed|unsigned)\s+)?char(?:\s+(?:const|volatile))*"
    r"\s*\[[0-9]+\]"
)


def _is_object(variable):
    """Whether a DAP variable is a class, struct, union or array: it has children and is
    no pointer or reference."""
    kind = re.sub(r"(?:\s*\b(?:const|volatile)\b)+$", "", str(variable.get("type") or "").strip())
    reference = variable.get("variablesReference")
    return type(reference) is int and reference > 0 and not kind.endswith(("*", "&"))


# A C++ constructor's frame name: `Widget::Widget(long)`, `ns::Box<int>::Box(int)`.
# An unnamed class is `$_0` (Clang), `(anonymous struct)`, `(unnamed struct at f:5:3)`
# (LLDB) or `{unnamed type#1}` (GCC). GCC names an unnamed class's constructor
# `<constructor>` and a closure's `<lambda>`, which LLDB shows as they are.
_CONSTRUCTOR_NAME = re.compile(
    r"(?:^|::|\s)([A-Za-z_]\w*|\$_\d+|\((?:anonymous|unnamed) (?:struct|class|union)[^()]*\)"
    r"|\{unnamed type#\d+\})(?:<[^()]*>)?::\1(?:<[^()]*>)?\("
    r"|(?:^|::|\s)<(?:constructor|lambda)>(?:\(|$)"
)
# A C++ destructor's or assignment operator's frame name (`Noisy::~Noisy()`,
# `ns::Box<int>::~Box()`, `E::operator=(E const&)`, GCC's `~<constructor>`), as
# _CONSTRUCTOR_NAME's.
_LIFETIME_NAME = re.compile(
    r"(?:(?:^|::|\s)([A-Za-z_]\w*|\$_\d+|\((?:anonymous|unnamed) (?:struct|class|union)[^()]*\)"
    r"|\{unnamed type#\d+\})(?:<[^()]*>)?::~\1(?:<[^()]*>)?|::operator=)\("
    r"|(?:^|::|\s)~<(?:constructor|lambda)>(?:\(|$)"
)
# A source whose statements are scanned (c_source) to place a caller's declaration.
MAX_STATEMENT_SOURCE = 8_000_000


def shown_declaration(table, name, pc, unstored=False):
    """The declaration lldb-dap lists under ``name`` (`x`, or `x @ file:line` when it is
    shadowed) at ``pc`` (unslid): ``(function, (source, line, automatic))``, an
    automatic local or parameter of the function running ``pc`` (the narrowest holding
    it) whose block's code holds ``pc``, the innermost such block where several do.
    Another function's (a lambda's parameter), a block out of scope there (an earlier
    loop's) or a static is not it.

    None if that function declares none of that name (nothing of it to judge); False if
    the one shown cannot be singled out: ``pc`` unknown or in no function of the table,
    a declaration's block unknown, none of them in scope, or two in one block. With
    ``unstored``, also a local an optimized build keeps in registers or as a constant
    (``function["unstored"]``), which lldb-dap lists too (#173)."""
    if pc is None:
        return False
    holding = [
        (high - low, index)
        for index, function in enumerate(table)
        for low, high in function.get("ranges", ())
        if low <= pc < high
    ]
    if not holding:
        return False
    function = table[min(holding)[1]]
    shown = split_located(name)
    base = shown[0] if shown else name
    # lldb-dap lists a frame's automatic locals and parameters as its locals; a static
    # (clang may declare it in the function's scope, whatever block it is in) is listed
    # among globals.
    places = {
        where
        for where in function["locals"].get(base, ())
        if where[2]
        or where[0] is None
        and where[1] is None
        or unstored
        and (base, *where[:2]) in function.get("unstored", ())
    }
    if shown:
        places = {where for where in places if where[1] == shown[2]}
    if not places:
        return None
    visible = function.get("visible")
    if visible is None:
        return False
    found = []
    for where in places:
        ranges = visible.get((base, *where))
        if ranges is None:
            return False
        if spans := [high - low for low, high in ranges if low <= pc < high]:
            found.append((min(spans), where))
    found.sort(key=lambda pair: pair[0])
    if not found or (len(found) > 1 and found[0][0] == found[1][0]):
        return False
    return function, found[0][1]


def declared_below(table, name, source, line, through=None, address=None, pc=None):
    """``(line, why)`` for a C/C++ local shown at ``source:line`` that is declared
    below it, on ``line``; else None.

    The declaration judged is the one the debugger shows (``shown_declaration`` at the
    frame's ``pc``, unslid): an automatic variable declared in this source below the
    line. Above its declaration, in the block that declares it, a local's value is
    indeterminate (C11 6.2.4): the block was entered anew (a loop's body each pass), and
    its storage holds leftovers. Another declaration of the name elsewhere (an earlier
    loop's, a static in another block, a lambda's parameter) changes nothing; where the
    one shown cannot be singled out, any of the name declared below withholds it.

    ``why`` is None when control cannot have reached the declaration on this pass:
    ``address`` (the stop's, unslid, when the line is the frame's own) comes before any
    code of the declaration's line, or all of the function's code of this line does.
    Line numbers alone do not say that. Where Peeky cannot tell, ``why`` says what
    stands in the way, and the local is withheld all the same (sixth agent evaluation):
    "after", the line has code placed after the declaration's and the stop is not
    before it (a temporary's destructor in an exception handler, which clang attributes
    to the line of `std::string name = f(entry.substr(0, colon));`; GCC's `cleanup` at
    its variable's line when the block ends; clang's default member initializer of a
    struct, run in its caller on the struct's line); "label", a label lets `goto`
    return above the declaration with the local live; "no code", the declaration's line
    has no code of its own and its statement's end is not known (a caller frame's);
    "unknown", the declaration shown cannot be singled out. A stop past the declaration
    there is in code after it, where the local may be live, destroyed or not reached:
    never shown.

    A declaration line with no code of its own (an aggregate stored element by element
    on its elements' lines) counts by the code of its statement's lines, to where
    ``through(source, line, name)`` says it ends.
    """
    shown = split_located(name)
    base = shown[0] if shown else name
    here = (source, line)
    found = shown_declaration(table, name, pc)
    if found is None:
        return None
    if found is False:
        # Any declaration of the name, in any function with code on the line, declared
        # below it may be the one shown: withheld.
        below = [
            at
            for function in table
            if here in function["rows"]
            for declared_in, at, automatic in function["locals"].get(base, ())
            if automatic
            and declared_in == source
            and type(at) is int
            and at > line
            and (not shown or at == shown[2])
        ]
        return (min(below), "unknown") if below else None
    function, (declared_in, at, automatic) = found
    if not automatic or declared_in != source or type(at) is not int or at <= line:
        return None
    # A backward `goto` can return above the declaration with the local live.
    why = "label" if function["jumps"] else None
    rows = function["rows"]
    if (source, at) in rows:
        first = rows[source, at][0]
    elif through is None:
        why, first = why or "no code", None
    else:
        end = through(source, at, base)
        starts = [
            rows[source, number][0]
            for number in range(at, (end or at) + 1)
            if end is not None and (source, number) in rows
        ]
        first = min(starts) if starts else None
        if end is None:
            why = why or "no code"
    if first is not None and not (here in rows and rows[here][1] < first):
        # Some code of the stop's line comes after the declaration's first code: only a
        # stop before that code is proven to be above it.
        inside = address is not None and any(
            low <= address < high for low, high in function.get("ranges", ())
        )
        if not (inside and address < first):
            why = why or "after"
    return at, why


def declared_unfinished(table, name, source, line, address, ends=None, chosen=None):
    """For a caller frame at ``source:line``, inside a call that returns to ``address``
    (unslid; None if unknown): ``(line, why, detail)`` for a C/C++ local of that name
    whose declaration may not have run yet, else None. ``why`` is `file` (``detail``: the
    file declaring it, None outside the program's sources), `no code`, `statement` (the
    frame's line is within the declaration statement, lines ``line``-``detail``;
    ``detail`` None when ``ends(line, name)`` cannot find it) or `unfinished`.

    Declared in another file than the frame's line: an initializer can `#include` its
    rows (the frame is then in the included file), or be in an included file itself, so
    no line of either file places the frame within or after the declaration.

    Declared at or above the frame's line: the frame's own line always counts, and so
    does a line the function still has code of at or after ``address`` (`long r =` /
    `f(x);` stores `r` on the declaration's line after the call returns, and so does a
    designated initializer over several lines). Declared anywhere on a line with no code
    of its own: an array of structs, a nested struct or a 2-D array is stored element by
    element on its elements' lines, so nothing places it. With the address or its
    function unknown, every declaration at or above the line counts. And so does one whose
    statement, as the source writes it (``ends``), still runs on the frame's line:
    `struct trio t = { seed,` / `{ twice(3), ... } };` has code of its first line, all of
    it before the call.

    ``chosen``: (source, line) of the declaration the name means there, where Peeky
    can tell (#158: `_named_where`); only it is judged then, not every one of the name
    in the function (another block's `long t;` with no code withheld this one)."""
    shown = split_located(name)
    base = shown[0] if shown else name
    owners = [function for function in table if (source, line) in function["rows"]]
    running = [
        function
        for function in owners
        if address is not None
        and any(low <= address < high for low, high in function.get("ranges", ()))
    ]
    if running:
        owners = running
    else:
        # The function returned to is not identified: every declaration counts.
        address = None
        owners = owners or [function for function in table if base in function["locals"]]
    for function in owners:
        for declared_in, at, automatic in function["locals"].get(base, ()):
            if not automatic or type(at) is not int:
                continue
            if shown and at != shown[2]:
                continue
            if chosen is not None and (declared_in, at) != chosen:
                continue
            if declared_in != source:
                return at, "file", declared_in
            span = function["rows"].get((source, at))
            if span is None:
                return at, "no code", None
            if at <= line and (at == line or address is None or span[1] >= address):
                return at, "unfinished", None
            if at <= line and ends is not None:
                end = ends(at, base)
                if end is None or line <= end:
                    return at, "statement", end
    return None


def _in_scope(table, name, source, line, address):
    """(declared_in, line, function) of each automatic C/C++ local of that name a frame at
    ``source:line`` may be reading: those of the function running the address (unslid),
    else of every function with code on the line, else of every function with such a
    local; with the address known, only those whose block holds it."""
    shown = split_located(name)
    base = shown[0] if shown else name
    owners = [function for function in table if (source, line) in function["rows"]]
    running = [
        function
        for function in owners
        if address is not None
        and any(low <= address < high for low, high in function.get("ranges", ()))
    ]
    owners = running or owners or [f for f in table if base in f["locals"]]
    found = []
    for function in owners:
        for declared_in, at, automatic in function["locals"].get(base, ()):
            if not automatic or type(at) is not int or (shown and at != shown[2]):
                continue
            block = function.get("scopes", {}).get((base, declared_in, at))
            if running and block and not any(low <= address < high for low, high in block):
                continue
            found.append((declared_in, at, function))
    return base, found


def declared_running(
    table, name, source, line, address, through, included=None, site=None, skipped=None
):
    """For the frame stopped in, at ``source:line``: ``(at, end, declared_in, where)``
    for a C/C++ local of that name whose declaration may not have run there, else None.
    That is one declared above the stop whose statement reaches the stop's line
    (``at < line <= end``), or whose end the source does not give (``end`` None).

    For one declared in another file, ``where`` is the line of the `#include` in
    ``source`` that brings that file in (``site(source, declared_in)``, None if none is
    found), which stands for its declaration: withheld at or above that line, or when it
    is not found, and below it unless its statement is whole in its file
    (``through``), no `#include` follows it in its block there (``included``,
    Statements.included_after) and no label between lets a jump past it
    (``skipped(source, where, line)``, Statements.jumped_past_include).
    ``through(declared_in, at, name)``: the line by which the declaration has run
    (Statements.declared_through), or None."""
    base, found = _in_scope(table, name, source, line, address)
    for declared_in, at, function in found:
        if declared_in != source:
            where = site(source, declared_in) if site is not None and declared_in else None
            if (
                where is None
                or where >= line
                or through(declared_in, at, base) is None
                or (included is not None and included(declared_in, at, base) is not False)
                or (
                    skipped is not None
                    and skipped(source, where, line, function.get("span")) is not False
                )
            ):
                return at, None, declared_in, where
            continue
        if at >= line:
            continue
        end = through(source, at, base)
        if end is None or line <= end:
            return at, end, declared_in, None
    return None


def declared_mid_line(table, name, source, line, address, settled):
    """The stop line, when the C/C++ local is declared on it, the stop is past the
    line's first instruction (``address``, unslid) and the function still has code of
    the line at or after it: back from a call in `long r = f(3) + f(4);` (`step --out`),
    `r` is stored only by that code. Else None. Not a variable a header sets once by no
    call in a function no jump can enter it in (``settled(source, line, name, span)``
    True, ``span`` the function's lines: `for (int i = 0; ...)`), which is live
    mid-line on later passes of a one-line loop."""
    base, found = _in_scope(table, name, source, line, address)
    places = [
        function for declared_in, at, function in found if declared_in == source and at == line
    ]
    if (
        address is None
        or not places
        or all(settled(source, line, base, function.get("span")) is True for function in places)
    ):
        return None
    for function in table:
        span = function["rows"].get((source, line))
        ranges = function.get("ranges", ())
        if span is None or not any(low <= address < high for low, high in ranges):
            continue
        # The line's last row runs to the next row of any line (a later known row start
        # only widens it), else to the end of the function's code.
        later = [a for low, high in function["rows"].values() for a in (low, high) if a > span[1]]
        end = min(later or [high for low, high in ranges if low <= span[1] < high] or [span[1] + 1])
        if span[0] < address < end:
            return line
    return None


def declared_skipped(table, name, source, line, address, skipped):
    """``(at, label)`` for a C/C++ local of that name declared above a frame at
    ``source:line`` when a jump may have skipped its declaration: a label between the
    two that a `switch` or `goto` reaches (``label``: its line), or unknown (None);
    else None. ``skipped(source, at, name, line, span)``: Statements.jumped_past, with
    the function's own lines."""
    base, found = _in_scope(table, name, source, line, address)
    for declared_in, at, function in found:
        if declared_in != source or at >= line:
            continue
        label = skipped(source, at, base, line, function.get("span"))
        if label is not False:
            return at, label
    return None


def declared_on_line(table, name, source, line, address):
    """``(line, why)``, when the C/C++ local shown at the stop line is declared on it
    and the stop is where control enters that line in the block declaring it
    (``address``, unslid; see local_declarations ``starts``): the declaration has not
    run on this pass, so the local holds leftovers. Else None.

    Only there: a one-line loop, or a return from a call (`step --out`), stops
    mid-line, where the local is live. The declaration judged is the one the debugger
    shows (``shown_declaration``): an automatic variable declared on this line. ``why``
    is None; "label" in a function with a label: a `goto` may have returned above the
    line with the local live, or this is its first pass, which Peeky cannot tell apart,
    so it is withheld all the same (sixth agent evaluation); "unknown" where the
    declaration shown cannot be singled out and one of the name starts here. The caller
    also rules out an address a loop branches back to.
    """
    if address is None:
        return None
    shown = split_located(name)
    base = shown[0] if shown else name
    found = shown_declaration(table, name, address)
    if found is None:
        return None
    if found is False:
        starting = any(
            address in function.get("starts", {}).get((base, source, line), ())
            and any(
                automatic and declared_in == source and at == line
                for declared_in, at, automatic in function["locals"].get(base, ())
            )
            for function in table
        )
        return (line, "unknown") if starting else None
    function, (declared_in, at, automatic) = found
    if (
        automatic
        and declared_in == source
        and at == line
        and address in function.get("starts", {}).get((base, source, line), ())
    ):
        return line, "label" if function["jumps"] else None
    return None


# Jumps (not calls or returns), by the mnemonic LLDB prints.
_JUMPS = {
    "arm64": lambda m: m in _ARM64_JUMPS or m.startswith(("b.", "bc.")),
    "x86_64": lambda m: m.startswith(("j", "loop")),
}
_ARM64_JUMPS = {"b", "br", "braa", "brab", "braaz", "brabz", "cbz", "cbnz", "tbz", "tbnz"}


def backward_targets(instructions, low, high, architecture):
    """The addresses in [low, high) that a jump there goes back to (a loop's start), from
    a DAP disassembly of that range, whose jumps name absolute targets (`b 0x1000…`,
    `jmp 0x4011…`). An empty set when no jump goes back; None when unknown: another
    architecture, or a jump whose target is not named in the range (`br x8`, a jump
    table)."""
    is_jump = _JUMPS.get(architecture)
    if is_jump is None:
        return None
    targets = set()
    for instruction in instructions:
        try:
            at = int(instruction["address"], 16)
        except (KeyError, TypeError, ValueError):
            return None
        if not low <= at < high:
            continue
        # LLDB's trailing comment (`; <+24> at a.c:6`) names no operand.
        text = str(instruction.get("instruction", "")).split(";")[0].split()
        if not text or not is_jump(text[0].lower()):
            continue
        named = [
            target
            for token in re.findall(r"0x[0-9a-fA-F]+", " ".join(text[1:]))
            if low <= (target := int(token, 16)) < high
        ]
        if not named:
            return None
        targets.update(target for target in named if target <= at)
    return targets


def architecture(binary):
    """`arm64` or `x86_64` from a 64-bit Mach-O or ELF header; None for anything else."""
    with Path(binary).open("rb") as stream:
        header = stream.read(20)
    if len(header) >= 8 and struct.unpack_from("<I", header)[0] == 0xFEEDFACF:
        return {0x0100000C: "arm64", 0x01000007: "x86_64"}.get(
            struct.unpack_from("<I", header, 4)[0]
        )
    if len(header) == 20 and header[:6] == b"\x7fELF\x02\x01":
        return {183: "arm64", 62: "x86_64"}.get(struct.unpack_from("<H", header, 18)[0])
    return None


def macho_text_address(binary):
    """Where a 64-bit Mach-O executable's header loads before any slide: its __TEXT
    segment's address (the segment that maps file offset 0)."""
    with Path(binary).open("rb") as stream:
        header = stream.read(32)
        if len(header) != 32 or struct.unpack_from("<I", header)[0] != 0xFEEDFACF:
            raise ValueError("Expected a 64-bit little-endian Mach-O executable")
        count, size = struct.unpack_from("<II", header, 16)
        if not 0 < count <= 4096 or not 0 < size <= 1 << 20:
            raise ValueError("Invalid Mach-O load commands")
        commands = stream.read(size)
    if len(commands) != size:
        raise ValueError("Truncated Mach-O load commands")
    cursor, found = 0, []
    for _ in range(count):
        if cursor + 8 > size:
            raise ValueError("Truncated Mach-O load command")
        kind, length = struct.unpack_from("<II", commands, cursor)
        if length < 8 or cursor + length > size:
            raise ValueError("Invalid Mach-O load command size")
        if kind == 0x19 and length >= 72:  # LC_SEGMENT_64
            name = commands[cursor + 8 : cursor + 24].rstrip(b"\0")
            address, _, offset = struct.unpack_from("<QQQ", commands, cursor + 24)
            if name == b"__TEXT" and offset == 0:
                found.append(address)
        cursor += length
    if len(found) != 1:
        raise ValueError("Missing or ambiguous Mach-O __TEXT segment")
    return found[0]


def load_bias(binary, module):
    """The executable's load slide, from the debugger's module address (where its header
    loaded: hex from Apple's and LLVM 20's lldb-dap, decimal from LLVM 18's)."""
    text = module.get("addressRange")
    if isinstance(text, str) and re.fullmatch(r"0x[0-9a-fA-F]{1,16}", text):
        loaded = int(text, 16)
    elif isinstance(text, str) and re.fullmatch(r"[0-9]{1,20}", text):
        loaded = int(text)
    else:
        raise ValueError("Missing or invalid native module load address")
    header = (
        macho_text_address(binary) if sys.platform == "darwin" else elf_executable_ranges(binary)[0]
    )
    if not header <= loaded < 1 << 64:
        raise ValueError("Invalid native load bias")
    return loaded - header


def elf_build_id(binary):
    """Read bounded ELF64 program-note records; no dependency on shell tools."""
    with Path(binary).open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:6] != b"\x7fELF\x02\x01":
            raise ValueError("Only ELF64 little-endian native executables are supported")
        offset = struct.unpack_from("<Q", header, 32)[0]
        entry_size, count = struct.unpack_from("<HH", header, 54)
        size = os.fstat(stream.fileno()).st_size
        if entry_size != 56 or not 0 < count <= 1024 or offset + count * entry_size > size:
            raise ValueError("Invalid ELF program header table")
        identities = set()
        for index in range(count):
            stream.seek(offset + index * entry_size)
            record = stream.read(entry_size)
            kind, _, start, _, _, length = struct.unpack_from("<IIQQQQ", record)
            if kind != 4:
                continue
            if length > 1024 * 1024 or start + length > size:
                raise ValueError("ELF note exceeds limit")
            stream.seek(start)
            notes = stream.read(length)
            cursor = 0
            while cursor < len(notes):
                if len(notes) - cursor < 12:
                    raise ValueError("Truncated ELF note")
                names, values, tag = struct.unpack_from("<III", notes, cursor)
                cursor += 12
                end_name = cursor + ((names + 3) & ~3)
                end_value = end_name + ((values + 3) & ~3)
                if end_value > len(notes):
                    raise ValueError("Truncated ELF note payload")
                if notes[cursor : cursor + names] == b"GNU\0" and tag == 3:
                    if values not in (16, 20, 32):
                        raise ValueError("Unsupported ELF build ID size")
                    identities.add(notes[end_name : end_name + values].hex().upper())
                cursor = end_value
        if len(identities) != 1:
            raise ValueError("Missing or ambiguous ELF GNU build ID")
        return identities.pop()


def _remaining(deadline, limit=15):
    if deadline is None:
        return limit
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Native preparation exceeded its overall deadline")
    return min(limit, remaining)


def module_identity(dwarfdump, binary, symbols, workspace, env, *, deadline=None):
    """Mach-O binary/dSYM UUID equality or the ELF GNU build ID."""
    if sys.platform != "darwin":
        return elf_build_id(binary)
    identities = []
    for artifact in (binary, symbols):
        output = _metadata(
            dwarfdump, ["--uuid", str(artifact)], workspace, env, timeout=_remaining(deadline)
        )
        found = re.findall(r"UUID:\s+([0-9A-Fa-f-]+)\s+\(", output)
        if len(found) != 1 or len(found[0].replace("-", "")) != 32:
            raise ValueError("Expected one native UUID")
        identities.append(found[0].replace("-", "").upper())
    if identities[0] != identities[1]:
        raise ValueError("Native binary and symbols UUID mismatch")
    return identities[0]


def elf_executable_ranges(binary):
    """Bound ELF64 load metadata used to bind an LLDB instruction pointer."""
    limit = 1 << 64
    with Path(binary).open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:6] != b"\x7fELF\x02\x01":
            raise ValueError("Expected ELF64 little-endian load metadata")
        offset = struct.unpack_from("<Q", header, 32)[0]
        entry_size, count = struct.unpack_from("<HH", header, 54)
        size = os.fstat(stream.fileno()).st_size
        if entry_size != 56 or not 0 < count <= 1024 or offset + count * entry_size > size:
            raise ValueError("Invalid ELF load header table")
        headers, ranges = [], []
        for index in range(count):
            stream.seek(offset + index * entry_size)
            kind, flags, start, address, _, files, memory, _ = struct.unpack(
                "<IIQQQQQQ", stream.read(entry_size)
            )
            if kind != 1:
                continue
            if files > memory or start + files > size or address + memory >= limit:
                raise ValueError("Invalid ELF load segment bounds")
            if start == 0 and files >= 64:
                headers.append(address)
            if flags & 1 and files:
                ranges.append((address, address + files))
        if len(headers) != 1 or not ranges:
            raise ValueError("Missing or ambiguous ELF executable load segments")
        return headers[0], tuple(ranges)


def _elf_frame_matches(binary, module, frame):
    pc_text, base_text = frame.get("instructionPointerReference"), module.get("addressRange")
    if (
        not isinstance(pc_text, str)
        or not re.fullmatch(r"0x[0-9a-fA-F]{1,16}", pc_text)
        or not isinstance(base_text, str)
        or not re.fullmatch(r"[0-9]{1,20}", base_text)
    ):
        raise ValueError("Missing or invalid native instruction/load address")
    pc, loaded_header = int(pc_text, 16), int(base_text)
    header, ranges = elf_executable_ranges(binary)
    bias = loaded_header - header
    if bias < 0 or loaded_header >= 1 << 64:
        raise ValueError("Invalid ELF load bias")
    relocated = [(start + bias, end + bias) for start, end in ranges]
    if any(end >= 1 << 64 for _, end in relocated):
        raise ValueError("Overflowing ELF relocated segment")
    return any(start <= pc < end for start, end in relocated)


# LLDB gives up on a reply from its debug server (lldb-server on Linux, debugserver on
# macOS) after 5 s and reports the read as failed (`invalid load address`): on a loaded
# machine a value it could read went missing (#193). Waiting up to 30 s leaves the
# bound to Peeky's own requests (15 s, `DAP.request`), which fail as TIMEOUT.
LLDB_INIT_COMMANDS = ("settings set plugin.process.gdb-remote.packet-timeout 30",)


@dataclass(frozen=True)
class NativeTarget:
    sources: tuple[str, ...]
    headers: tuple[str, ...] = ()
    compiler: str | None = None
    adapter: str = "lldb-dap"
    dwarfdump: str = "llvm-dwarfdump"
    dsymutil: str | None = None
    sysroot: str | None = None
    arguments: tuple[str, ...] = ()
    language: str = "c"
    # Opt-in: optimized code can report locals as unavailable or show stale values.
    optimization: str = "O0"

    def __post_init__(self):
        if self.language not in ("c", "cpp"):
            raise ValueError("Native language must be c or cpp")
        if self.optimization not in OPTIMIZATIONS:
            raise ValueError(f"Native optimization must be one of {', '.join(OPTIMIZATIONS)}")
        if self.compiler is None:
            object.__setattr__(self, "compiler", "clang++" if self.language == "cpp" else "clang")
        if (
            not isinstance(self.sources, tuple)
            or not self.sources
            or not isinstance(self.headers, tuple)
        ):
            raise ValueError("Native inputs must be immutable source/header tuples")
        for name in self.sources:
            _path_name(name, (".cpp", ".cc", ".cxx") if self.language == "cpp" else ".c")
        for name in self.headers:
            _path_name(name, (".h", ".hpp", ".hh", ".hxx") if self.language == "cpp" else ".h")
        self.validate_arguments(self.arguments)

    accepts_arguments = True

    @staticmethod
    def validate_arguments(arguments):
        if (
            not isinstance(arguments, tuple)
            or len(arguments) > 100
            or any(
                not isinstance(arg, str) or "\x00" in arg or len(arg) > 8192 for arg in arguments
            )
        ):
            raise ValueError("Invalid native application arguments")

    @property
    def compiler_options(self):
        flags = CPP_FLAGS if self.language == "cpp" else FLAGS
        return tuple(f"-{self.optimization}" if flag == "-O0" else flag for flag in flags)

    @property
    def verification_inputs(self):
        return self.sources + self.headers

    @property
    def verification_sources(self):
        return self.sources

    @property
    def target_identity(self):
        return {
            "language": self.language,
            "sources": sorted(self.sources),
            "headers": sorted(self.headers),
            "arguments": list(self.arguments),
            "compiler_options": list(self.compiler_options),
            "optimization": self.optimization,
        }

    def validate_breakpoints(self, points):
        PythonAdapter().validate_breakpoints(points)
        if any(set(point) != {"line"} for point in points):
            raise ValueError("Native targets support unconditional line breakpoints only")

    supports_deadline = True
    # Candidate probes stage an edit into a fresh build (the worker's launch `edit`).
    supports_edits = True

    def _compile_flags(self):
        """(driver, every flag a source is compiled with, the sysroot, its resolved SDK).
        A C++ target's compiler must be clang (#167): refused before anything runs."""
        driver = ()
        if self.language == "cpp":
            compiler = _tool(self.compiler)
            if (family := compiler_family(compiler)) != "clang":
                said = {
                    "gcc": "is GCC",
                    "timeout": f"timed out: no answer to --version within {VERSION_TIMEOUT} s",
                }.get(family, "is not clang, by its --version")
                refused = ValueError(f"{CPP_COMPILER_REFUSAL} ({compiler} {said})")
                # Nothing was built or run: no run is kept as evidence.
                refused.before_launch = True
                raise refused
            # The compiler path is resolved (clang++ is a symlink to clang), and clang
            # picks its language from the name it runs as; keep C++ mode so libc++ is
            # linked. A clang-only option.
            driver = ("--driver-mode=g++",)
        flags = [*driver, *self.compiler_options]
        # Without an explicit sysroot, use the SDK the /usr/bin xcrun stub would have
        # supplied; it is part of the recorded build commands either way.
        sysroot = self.sysroot if self.sysroot is not None else default_sdk()
        sdk = None
        if sysroot is not None:
            sdk = Path(sysroot).resolve(strict=True)
            if not sdk.is_dir():
                raise ValueError("Native sysroot must be a directory")
            flags.extend(("-isysroot", str(sdk)))
        return driver, flags, sysroot, sdk

    def preprocess_argv(self, source):
        """The build's own compile of ``source``, preprocessing only (`-E`, keeping its
        line markers): the text the compiler parsed (#144)."""
        _, flags, _, _ = self._compile_flags()
        return _tool(self.compiler), [*flags, "-E", source]

    def prepare(self, program, root, *, edit=None, deadline=None):
        root = Path(root).resolve(strict=True)
        relative = Path(program).resolve(strict=True).relative_to(root).as_posix()
        if relative not in self.sources:
            raise ValueError(f"Program is not a declared {self.language} source")
        if edit is not None and edit.path not in self.sources:
            raise ValueError(
                "Native candidate edits require a declared C source"
                if self.language == "c"
                else "Native candidate edits require a declared C++ source"
            )
        # The compiler is judged first: a C++ target refuses GCC before anything runs.
        driver, flags, sysroot, sdk = self._compile_flags()
        compiler, adapter, dwarfdump = map(_tool, (self.compiler, self.adapter, self.dwarfdump))
        # Keep objects until dsymutil finishes; single-step clang removes its
        # temporary objects and a subsequent dsymutil can erase valid symbols.
        objects = tuple(f"peeky-object-{index}.o" for index in range(len(self.sources)))
        # Each unit preprocessed first (as `preprocess_argv`), so that inline assembly
        # that can read a file, which no dependency file lists, is refused before
        # anything is compiled (#180; `before_command`).
        units = tuple(f"peeky-unit-{index}.i" for index in range(len(self.sources)))
        commands = [
            Command((compiler, *flags, "-E", source, "-o", unit))
            for source, unit in zip(self.sources, units)
        ]
        # -MD lists every file each compile read: undeclared or outside files must not
        # escape provenance (`#include "/elsewhere/x.h"`, SDK and system headers).
        compiles = [
            Command((compiler, *flags, "-MD", "-MF", obj + ".d", "-c", source, "-o", obj))
            for source, obj in zip(self.sources, objects)
        ]
        commands += compiles
        link_flags = (*driver, *(("-isysroot", str(sdk)) if sysroot is not None else ()))
        if sys.platform == "linux":
            link_flags = (*link_flags, "-Wl,--build-id=sha1")
        commands.append(Command((compiler, *link_flags, *objects, "-o", "program")))
        compile_commands = {command.argv for command in commands}
        artifacts = [
            ArtifactSpec("program"),
            *(ArtifactSpec(obj, role="object") for obj in objects),
            *(ArtifactSpec(obj + ".d", role="dependencies") for obj in objects),
        ]
        probes = [
            Command((compiler, "--version")),
            Command((adapter, "--help")),
            Command((dwarfdump, "--version")),
        ]
        if sys.platform == "darwin":
            dsymutil = _tool(self.dsymutil or "dsymutil")
            commands.append(Command((dsymutil, "program", "-o", "program.dSYM")))
            artifacts.append(ArtifactSpec("program.dSYM", "directory", "debug-symbols"))
            probes.append(Command((dsymutil, "--version")))
        elif sys.platform != "linux":
            raise ValueError("Native target supports Linux and macOS hosts only")

        def before_command(workspace, results, command):
            if command == compiles[0]:
                for source, unit in zip(self.sources, units):
                    path = workspace / unit
                    if path.is_symlink() or not path.is_file():
                        raise RuntimeError(f"The compiler wrote no preprocessed text of {source}")
                    if path.stat().st_size > MAX_UNIT:
                        refused = ValueError(
                            f"{ASSEMBLER_REFUSAL}: Peeky cannot rule it out in {source}, whose "
                            f"preprocessed text is over {MAX_UNIT >> 20} MiB"
                        )
                        refused.before_launch = True
                        raise refused
                    refused = c_refusal(path.read_bytes(), self.language == "cpp")
                    path.unlink()
                    if refused is not None:
                        raise refused

        try:
            prepared = prepare(
                BuildRecipe(
                    self.language,
                    self.sources + self.headers,
                    tuple(commands),
                    tuple(artifacts),
                    tuple(probes),
                ),
                root,
                edit=edit,
                deadline=deadline,
                before_command=before_command,
            )
        except BuildCommandError as exc:
            if exc.command not in compile_commands:
                raise RuntimeError("Native symbol preparation command failed") from exc
            raise
        try:
            binary = prepared.workspace / "program"
            symbols = (
                prepared.workspace / "program.dSYM/Contents/Resources/DWARF/program"
                if sys.platform == "darwin"
                else binary
            )
            env = dict(prepared.manifest.effective_environment)
            text = _metadata(
                dwarfdump,
                ["--debug-line", str(symbols)],
                prepared.workspace,
                env,
                timeout=_remaining(deadline),
            )
            lines = parse_lines(text, prepared.workspace, self.sources + self.headers)
            identity = module_identity(
                dwarfdump, binary, symbols, prepared.workspace, env, deadline=deadline
            )
            external = external_inputs(
                [prepared.workspace / (obj + ".d") for obj in objects],
                prepared.workspace,
                self.sources + self.headers,
            )
            typedefs, fields, unjudged, returns_twice, prologues = {}, None, None, None, None
            try:
                info = _metadata(
                    dwarfdump,
                    ["--debug-info", str(symbols)],
                    prepared.workspace,
                    env,
                    max_bytes=MAX_DEBUG_INFO,
                    timeout=_remaining(deadline),
                )
                with _reusing_dies():
                    typedefs = typedef_kinds(info)
                    nonlocal_jumps = (
                        has_nonlocal_jumps(binary)
                        or mentions_nonlocal_jumps(
                            prepared.workspace / name for name in self.sources + self.headers
                        )
                        # A header outside the workspace can hide the builtin in a macro;
                        # the SDK's own setjmp.h names `setjmp`, so only the builtin counts.
                        or mentions_nonlocal_jumps(external, (b"__builtin_setjmp",))
                    )
                    prologues = []
                    declarations = local_declarations(
                        info,
                        text,
                        prepared.workspace,
                        self.sources + self.headers,
                        prologues=prologues,
                    )
                    if nonlocal_jumps:
                        # Which functions may call setjmp is read from their source:
                        # its family's names, or a macro naming one, including those
                        # of headers outside the program (a TRY macro).
                        returns_twice = returning_twice(
                            Path(path).read_bytes().decode("utf-8", errors="replace")
                            for path in external
                        )
                    if declarations is not None:
                        from peeky.fields import CPP_LIBRARIES, type_graph

                        # The types field and element changes walk (#105).
                        fields = type_graph(
                            _dies(info),
                            {
                                kind
                                for function in declarations
                                for kinds in (function["types"] or {}).values()
                                for kind, _, _ in kinds
                            },
                            CPP_LIBRARIES,
                        )
                        # And, one level down, what their pointers and references reach:
                        # previews of `p[0]` and of a reference (#136). Changes never
                        # walk through either, so these types change nothing.
                        aimed = {
                            node[2]
                            for node in fields.values()
                            if node[0] in ("pointer", "reference") and node[2] is not None
                        } - set(fields)
                        if aimed:
                            for offset, node in type_graph(
                                _dies(info), aimed, CPP_LIBRARIES
                            ).items():
                                fields.setdefault(offset, node)
            except (RuntimeError, ValueError, OSError) as exc:
                # Without it, values show as the debugger reads them, each local with a
                # note that its initialization is not judged (and no field or element
                # can be changed).
                declarations, fields, prologues = None, None, None
                unjudged = ("unreadable", str(exc)[:120] or type(exc).__name__, None)
            session = NativeSession(
                self,
                prepared,
                adapter,
                lines,
                identity,
                binary,
                symbols,
                external,
                declarations=declarations,
                typedefs=typedefs,
                fields=fields,
                unjudged=unjudged,
                returns_twice=returns_twice,
                prologues=prologues,
            )
            session.assert_current()
            return session
        except BaseException:
            prepared.cleanup()
            raise


MAX_EXTERNAL_INPUTS = 4096
# The most preprocessed text of one unit Peeky reads for inline assembly (#180).
MAX_UNIT = 64 * 1024 * 1024


def external_inputs(depfiles, workspace, declared):
    """{path: sha256} of files the compiler read outside the workspace.

    Files inside the workspace must be declared inputs (they are already hashed);
    files outside it (SDK and system headers, absolute includes) are hashed here and
    rechecked before every operation like the declared ones.
    """
    root = Path(workspace).resolve()
    found, listings = {}, {}
    for depfile in depfiles:
        for dependency in dependency_paths(depfile.read_text(), root, "make"):
            try:
                path = dependency.resolve(strict=True)
            except OSError as exc:
                raise UnreadDependencies(
                    f"it lists {dependency}, which Peeky cannot find ({exc.strerror})"
                ) from None
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                if twin := _backslash_twin(dependency, listings):
                    raise UnreadDependencies(
                        f"clang prints a backslash in a file name as `/`, so {dependency} "
                        f"may be {twin}"
                    ) from None
                if str(path) not in found:
                    if len(found) >= MAX_EXTERNAL_INPUTS:
                        raise ValueError("The build read too many files outside the workspace")
                    found[str(path)] = _digest(path)[0]
                continue
            if relative not in declared:
                raise ValueError(
                    f"The build read {relative}, which is not a declared source or header"
                )
    return dict(sorted(found.items()))


def _backslash_twin(path, listings):
    """A file whose name has a backslash that clang would print as ``path`` (it prints a
    name's `\\` as `/`, so `/x/a\\b.h` reads as `/x/a/b.h`): an entry of a directory on
    ``path``'s way named for the next step and a backslash (`a\\...` beside `a`), or one
    that, its backslashes read as `/`, is ``path`` or a directory on its way (`\\x.h`
    beside `x.h`; #178 review), or None. ``listings`` caches each directory's backslashed
    names; one that cannot be listed raises UnreadDependencies."""
    parts = path.parts
    for depth in range(1, len(parts)):
        directory = Path(*parts[:depth])
        if directory not in listings:
            try:
                listings[directory] = [name for name in os.listdir(directory) if "\\" in name]
            except OSError as exc:
                raise UnreadDependencies(
                    f"Peeky cannot list {directory} ({exc.strerror}) to tell which file {path} is"
                ) from None
        for name in listings[directory]:
            printed = Path(str(directory), name.replace("\\", "/").lstrip("/"))
            if name.split("\\")[0] == parts[depth] or printed == path or printed in path.parents:
                return directory / name
    return None


def _integer_ranges():
    """LLDB's names for integer types on LP64 targets (macOS, Linux; C, C++ and Rust),
    with the standard <stdint.h>/<stddef.h> typedefs, and each one's range. Character
    and boolean types are excluded: their values are not plain integers."""
    ranges = {}
    for names, bits, signed in (
        (("short", "short int", "int16_t"), 16, True),
        (("unsigned short", "unsigned short int", "uint16_t"), 16, False),
        (("int", "int32_t"), 32, True),
        (("unsigned int", "unsigned", "uint32_t"), 32, False),
        (
            ("long", "long int", "long long", "long long int", "int64_t", "ssize_t")
            + ("ptrdiff_t", "intptr_t"),
            64,
            True,
        ),
        (
            ("unsigned long", "unsigned long int", "unsigned long long")
            + ("unsigned long long int", "uint64_t", "size_t", "uintptr_t"),
            64,
            False,
        ),
    ):
        span = (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if signed else (0, 2**bits - 1)
        for name in names:
            ranges[name] = span
            if name.endswith("_t"):
                ranges[f"std::{name}"] = span
    return ranges


INTEGER_RANGES = _integer_ranges()
# What a function may return that Peeky shows (#155): LLDB reads it from the return
# register, exactly. Structs, classes, references and arrays are withheld.
RETURN_SCALARS = frozenset(INTEGER_RANGES) | {
    "bool",
    "_Bool",
    "char",
    "signed char",
    "unsigned char",
    "wchar_t",
    "char8_t",
    "char16_t",
    "char32_t",
    "float",
    "double",
}
# A step out's watch on its caller (#162): after this many of the caller's watched
# instructions ran on the thread during the step (deeper calls of a recursion through
# the caller each run them), LLDB drops the watch itself and the return is not followed.
WATCH_TRAPS = 128
# One instruction as LLDB disassembles it: address, mnemonic, operands (#155).
_INSTRUCTION = re.compile(
    r"^\s*(?:->)?\s*(0x[0-9a-fA-F]+)\s*<\+\d+>:\s*([^\s;]*)\s*([^;\n]*)", re.M
)
_PREFIXES = {"notrack", "bnd", "lock", "rep", "repz", "repnz", "ds", "cs"}
_ARM_CALLS = {"bl", "blr", "blraa", "blraaz", "blrab", "blrabz"}
_ARM_INDIRECT = {"br", "braa", "braaz", "brab", "brabz"}
_ARM_BRANCHES = {"b", "cbz", "cbnz", "tbz", "tbnz"}


def _address(value):
    """An address the debugger gives as hex text, else None."""
    if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{1,16}", value.strip()):
        return int(value, 16)
    return None


def _stack_pointer(dap, frame_id):
    """A frame's stack pointer, as LLDB recovers it for that frame (a caller's is where
    the call left it); None if not given. (A command in the debug console reads the
    selected frame's registers, whatever frame is asked for.)"""
    reply = dap.request(
        "evaluate", {"expression": "$sp", "context": "watch", "frameId": frame_id}, timeout=2
    )
    text = str(reply.get("result") or "").strip()
    # lldb-dap 18 gives it in decimal, later ones in hex.
    return int(text) if re.fullmatch(r"[0-9]{1,20}", text) else _address(text)


def _instruction_kind(mnemonic, operands):
    """call, branch (with its target), indirect (a computed jump), or None."""
    name = mnemonic.lower()
    if name in _PREFIXES:
        name, _, operands = operands.strip().partition(" ")
        name = name.lower()
    if name in _ARM_CALLS or name.startswith("call"):
        return "call", None
    if name in _ARM_INDIRECT:
        return "indirect", None
    if name.startswith("j") or name in _ARM_BRANCHES or name.startswith(("b.", "bc.")):
        targets = re.findall(r"0x[0-9a-fA-F]+", operands)
        if operands.strip().startswith("*") or not targets:
            return "indirect", None
        return "branch", int(targets[-1], 16)
    return None, None


def _instructions(disassembly):
    """LLDB's disassembly of a function as [(address, kind, branch target)]."""
    return [
        (int(address, 16), *_instruction_kind(mnemonic, operands))
        for address, mnemonic, operands in _INSTRUCTION.findall(disassembly or "")
    ]


def _return_paths(listed, ret):
    """The instructions a frame can reach address `ret` by other than the return of the
    call just before it (#155): that call, every branch to `ret`, every computed jump;
    the call first. None if no call instruction comes just before `ret` (then `ret` is
    not where a call returns). `listed` is the function's `_instructions`."""
    addresses = [address for address, _, _ in listed]
    if ret not in addresses or addresses.index(ret) == 0:
        return None
    call, kind, _ = listed[addresses.index(ret) - 1]
    if kind != "call":
        return None
    return [call] + [
        address
        for address, kind, target in listed
        if (kind == "branch" and target == ret) or kind == "indirect"
    ]


def _writable_integer(variable):
    attributes = variable.get("presentationHint", {}).get("attributes", [])
    return (
        variable.get("type") in INTEGER_RANGES
        and variable.get("variablesReference", 0) == 0
        and "readOnly" not in attributes
        # A name a change can give: also one LLDB gives with where it is declared, when
        # several of that name are in scope (`n @ main.c:15`); the worker marks it
        # [settable] only where a selector reaches it (identifiers.reachable).
        and changeable(str(variable.get("name", "")))
    )


# One location of LLDB's `breakpoint list` (#157): `  1.2: where = movavg`movavg::main::h…
# + 1008 at main.rs:29:18, address = 0x0000000100000ee4, resolved, hit count = 0`. Before
# the program loads, its address is the module's: `address = m[0x0000000100000558],
# unresolved`.
_BREAKPOINT_LOCATION = re.compile(
    r"^ *([0-9]{1,9})\.[0-9]{1,9}: where = (.*), address = ([^\s,\[\]]+\[)?"
    r"(0x[0-9a-fA-F]{1,16})\]?(,.*)?[ \t]*$",
    re.MULTILINE,
)
# Its `where`: module`function, its offset there, then for inlined code the function
# inlined at it and its offset in that copy (`uses_inl + 72 [inlined] fmt_inl + 12`), and
# file:line.
_LOCATION_WHERE = re.compile(
    r"(?:[^`]*`)?(.+?)(?: \+ ([0-9]{1,12}))?(?: \[inlined\] (.+?)(?: \+ ([0-9]{1,12}))?)?"
    r" at [^:]+:([0-9]{1,9})(?::[0-9]{1,9})?"
)
MAX_LOCATIONS = 256
# Where `loop_headers` reads LLDB's disassembly: its branches are known (#161).
_LOOP_MACHINES = frozenset(("arm64", "aarch64", "x86_64", "amd64"))
# A line a loop header note is looked for on: one naming a loop (#161, review of #166:
# the analysis costs a function's disassembly); and in how many places at most.
_LOOP_WORD = re.compile(r"(?i:for|each|while|loop|repeat|iter|\bdo\b)|\b[A-Z][A-Z0-9_]*\s*\(")
MAX_LOOP_PLACES = 8


def breakpoint_locations(text, ids):
    """Where LLDB placed the breakpoints ``ids`` (#157), from its `breakpoint list` text:
    [{"id", "line", "address", "function", "place", "resolved"}], each location with the
    function that holds it. ``place`` names the code it is in: the function's start (its
    address less its offset), or, for code inlined there, that inlined copy's start (less
    its offset in the copy): two copies are two calls, each stopping at its own locations.
    ``resolved`` is False for one listed before the program loaded (`m[0x…], unresolved`),
    whose address is the module's. Unreadable lines are left out; None past
    MAX_LOCATIONS, where a count would fall short."""
    found = []
    for match in _BREAKPOINT_LOCATION.finditer(text):
        if int(match[1]) not in ids:
            continue
        where = _LOCATION_WHERE.fullmatch(match[2].strip())
        if where is None:
            continue
        address = int(match[4], 16)
        offset = int(where[2]) if where[2] else 0
        inner = int(where[4]) if where[4] else 0
        if offset > address or inner > address:
            continue
        found.append(
            {
                "id": int(match[1]),
                "line": int(where[5]),
                "address": address,
                "function": where[3] or where[1],
                "place": (
                    f"inlined at 0x{address - inner:x}" if where[3] else f"0x{address - offset:x}"
                ),
                # Listed before the program loaded: the module's own address, which the
                # program may not have once it runs (macOS slides it).
                "resolved": not match[3] and "unresolved" not in (match[5] or ""),
            }
        )
        if len(found) > MAX_LOCATIONS:
            return None
    return found


class Withholding(str):
    """A reason ``not_yet_declared`` withholds a row for: ``label`` heads the row's error
    in place of `no value yet`, for a reason that is not about its value being set yet
    (None keeps `no value yet`), and ``refusal`` is why a change to it is refused."""

    def __new__(cls, reason, label, refusal):
        made = super().__new__(cls, reason)
        made.label, made.refusal = label, refusal
        return made


class Advised(str):
    """A ``mid_statement`` reason with its own ``advice``: where to change the value
    instead, as no other stop on the statement helps (a stop in a prologue, #279)."""

    def __new__(cls, reason, advice):
        made = super().__new__(cls, reason)
        made.advice = advice
        return made


def _prologue_texts(window):
    """(where the stop is, why it stops there, where to read, where to change) for a stop
    in a function's prologue (#279), by its _prologue_windows window. A build that marks
    the prologue's end has only a stop no breakpoint makes there (a fault, a signal)."""
    marked, single = window.marked, window.single
    if marked:
        return (
            "the stop is in its function's prologue, before it has stored the parameters",
            "",
            "Read it at a later stop in the function",
            "Change it at a later stop in the function",
        )
    where = "the stop is at its function's start, before the prologue stores the parameters"
    if single:
        return (
            where,
            " (the build marks no prologue end, as GCC's never do, so LLDB stops a one-line "
            "function here)",
            "Read the arguments at the call, or put the body on a line of its own",
            "Change the argument at the call, or put the body on a line of its own and stop there",
        )
    return (
        where,
        " (the build marks no prologue end, as GCC's never do, and LLDB did not find it)",
        "Read the arguments at the call, or stop on a later line of the function",
        "Change the argument at the call, or change it on a later line of the function",
    )


class Everywhere(str):
    """A reason no stop of this run allows a change (mid_statement): no other stop, line
    or step helps, so no advice to stop elsewhere goes with it."""

    everywhere = True


class ValueNote(str):
    """What ``not_yet_declared`` gives for a local it shows, though it cannot vouch for
    the value: the row carries ``note``. As a string, why a change there is refused,
    where ``refuses`` (a declaration still to run on the stop line overwrites it)."""

    def __new__(cls, reason, note, refuses):
        made = super().__new__(cls, reason)
        made.note, made.refuses = note, refuses
        return made


# Initialization is not judged where the build's debug information could not be read:
# the rows that would be judged say so.
_UNJUDGED = {
    "unreadable": (
        "Peeky does not judge whether this local is initialized here: it could not read "
        "the build's debug information ({}), so it may hold leftovers"
    ),
}


class NativeSession:
    # lldb-dap reports optimized-out/not-live locals as `<error: ...>` text.
    reports_unavailable_as_text = True
    # Whose rule numbers the program's lines: rustc's (`\n` alone) or clang's (a lone
    # `\r` too); the session's language says, never a file's name (#188 review).
    rust_lines = False
    # lldb-dap names an array's elements `[N]` and lists any range of them on request,
    # so one element of a long array can be fetched alone (#105).
    elements_by_index = True

    @property
    def implicit_receiver(self):
        """C++ selectors name members of `this` without it, as C++ code does; in C a
        local may itself be called `this`."""
        return "this" if getattr(self.target, "language", None) == "cpp" else None

    def __init__(
        self,
        target,
        prepared,
        adapter,
        lines,
        identity,
        binary,
        symbols,
        external=None,
        declarations=None,
        typedefs=None,
        fields=None,
        unjudged=None,
        returns_twice=None,
        prologues=None,
    ):
        self.target, self.prepared = target, prepared
        # local_declarations of the build; None (unknown) never withholds a value, and
        # ``unjudged`` says why: (_UNJUDGED's key, its detail, the build's
        # local_declarations if read, to tell which rows are automatic locals).
        self.declarations = declarations
        # _prologue_windows of the build: where each function has not stored its
        # parameters yet (#279); None (unknown) where the declarations are.
        self.prologues = None if declarations is None or prologues is None else tuple(prologues)
        self.unjudged = unjudged
        # In a program that may call setjmp: the macros of headers outside the program
        # that may expand to it (see _returns_twice); None in one that cannot.
        self.returns_twice = returns_twice
        # fields.type_graph of the locals' types; None (unknown) changes no field.
        self.fields = fields
        # typedef_kinds of the build: {} (unknown) resolves no typedef.
        self.typedefs = dict(typedefs or {})
        self.external_inputs = dict(external or {})
        self.binary, self.symbols, self.adapter = binary, symbols, adapter
        self._lines_data, self.module_identity = json.dumps(lines, sort_keys=True), identity
        self.home = prepared.workspace / ".peeky-lldb-home"
        self.home.mkdir()
        self.manifest_id = hashlib.sha256(
            json.dumps(
                {
                    "build": prepared.manifest.manifest_id,
                    "external_inputs": self.external_inputs,
                    "lines": lines,
                    "module": identity,
                    "target": target.target_identity,
                    "runtime_environment": self.environment,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    @property
    def lines(self):
        return json.loads(self._lines_data)

    @property
    def verification_command(self):
        return (str(self.binary),)

    @property
    def command(self):
        return (self.adapter,)

    @property
    def environment(self):
        env = dict(self.prepared.manifest.effective_environment)
        env["HOME"] = str(self.home)
        return env

    @property
    def source_manifest(self):
        return {item.path: item.sha256 for item in self.prepared.manifest.original_inputs}

    @property
    def target_identity(self):
        return self.target.target_identity

    def initialize_args(self):
        return {
            **PythonAdapter().initialize_args(),
            "adapterID": "lldb",
            "$__lldb_sourceInitFile": False,
            "sourceInitFile": False,
        }

    def launch_args(self, program, root, args=None):
        self.assert_current()
        self.source_path(program)
        return {
            "initCommands": list(LLDB_INIT_COMMANDS),
            "program": str(self.binary),
            "cwd": str(self.prepared.workspace),
            # Per-run arguments (--arg) replace the configured ones.
            "args": list(self.target.arguments if args is None else args),
            "disableASLR": False,
            "shellExpandArguments": False,
            "console": "internalConsole",
            "enableAutoVariableSummaries": False,
        }

    def breakpoint_locations(self, dap, ids):
        """Where LLDB placed the breakpoints ``ids`` (see ``breakpoint_locations``): a
        line can have several locations in one function (a macro's or a block's own
        code, a loop header's), and it stops at each (#157). Asked with lldb-dap's
        command escape, as a read-only `breakpoint list`; None where it cannot say."""
        ids = sorted({number for number in ids if type(number) is int and number > 0})[:64]
        if not ids:
            return None
        reply = dap.request(
            "evaluate",
            {"expression": "`breakpoint list " + " ".join(map(str, ids)), "context": "repl"},
            timeout=5,
        )
        text = reply.get("result") if isinstance(reply, dict) else None
        if not isinstance(text, str) or len(text) > MAX_METADATA:
            return None
        return breakpoint_locations(text, set(ids))

    def loop_header_stops(self, dap, source, line, groups, cache):
        """(stops, exact): how many times ``line`` of ``source`` stops each time its loop
        starts, before its first pass (at most, unless ``exact``), where it is that
        loop's header alone in each function holding it (#161: `for (int i = 0; ...)`
        binds `i = 0`, never the condition); else None.
        ``groups``: its breakpoint locations, by the code holding them. From LLDB's
        disassembly of each function and the file's line table (`source info`), both
        read-only and kept in ``cache`` for the launch; see ``loop_headers``. Only a line
        that may hold a loop is analysed (a loop keyword, a name with one in it such as
        `list_for_each`, or a macro's capitals: `REPEAT(3) {`), in at most
        MAX_LOOP_PLACES functions or inlined copies."""
        from peeky.loop_headers import disassembly, line_table, stops_before_loop

        path = str(source)
        if not groups or len(groups) > MAX_LOOP_PLACES:
            return None
        if platform.machine().lower() not in _LOOP_MACHINES:
            return None
        if any(character in path for character in '"\\\n`'):
            return None
        if ("text", path) not in cache:
            try:
                cache[("text", path)] = source_lines(read_source(path, "replace"), self.rust_lines)
            except OSError:
                cache[("text", path)] = []
        lines = cache[("text", path)]
        if not 0 < line <= len(lines) or not _LOOP_WORD.search(lines[line - 1]):
            return None

        def command(text):
            reply = dap.request(
                "evaluate", {"expression": "`" + text, "context": "repl"}, timeout=5
            )
            result = reply.get("result") if isinstance(reply, dict) else None
            return result if isinstance(result, str) and len(result) <= MAX_METADATA else None

        if ("table", path) not in cache:
            # The whole file's, once: which line (and column) each range of code is.
            found = command(f'source info -f "{path}" -l 1')
            cache[("table", path)] = line_table(found) if found is not None else []
        table = cache[("table", path)]
        functions = cache.setdefault("functions", [])
        counts = set()
        for addresses in groups:
            listed = next(
                (
                    instructions
                    for instructions in functions
                    if instructions[0][0] <= min(addresses) <= instructions[-1][0]
                ),
                None,
            )
            if listed is None:
                listing = command(f"disassemble -a 0x{min(addresses):x}")
                listed = disassembly(listing) if listing is not None else None
                if listed is None:
                    return None
                functions.append(listed)
            if any(not listed[0][0] <= address <= listed[-1][0] for address in addresses):
                return None
            counts.add(stops_before_loop(listed, table, line, list(addresses)))
        if None in counts or len({count for count, _ in counts}) != 1:
            return None
        return next(iter(counts))[0], all(exact for _, exact in counts)

    def function_scopes(self):
        """Each function in the declared sources, its line span and locals (for describe)."""
        dwarfdump = _tool(self.target.dwarfdump)
        env = dict(self.prepared.manifest.effective_environment)
        workspace = self.prepared.workspace

        def dump(option, limit):
            return _metadata(
                dwarfdump, [option, str(self.symbols)], workspace, env, max_bytes=limit
            )

        from peeky.fields import CPP_LIBRARIES

        info = dump("--debug-info", MAX_DEBUG_INFO)
        return function_scopes(
            info,
            dump("--debug-line", MAX_METADATA),
            workspace,
            tuple(self.lines),
            settable=(CPP_LIBRARIES, None),
        )

    def typedef_kind(self, kind):
        """(kind, pointee) for a type lldb-dap names by a typedef (`const RowPtr`): see
        typedef_kinds; None for a name the build's debug information does not give."""
        if not isinstance(kind, str):
            return None
        bare = re.sub(r"^(?:(?:const|volatile)\s+)+|(?:\s+(?:const|volatile))+$", "", kind.strip())
        if (found := self.typedefs.get(bare)) is not None:
            return found
        # A class's member typedef (`std::vector<...>::reference`) by its class's own
        # name and its own: never a name whose qualifier is a namespace (lldb-dap leaves
        # out inline namespaces, `lib::Rows` for `lib::v1::Rows`), nor a class's name.
        parts = _without_templates(bare).split("::")
        if len(parts) < 2 or not all(parts[-2:]) or f"namespace::{parts[-2]}" in self.typedefs:
            return None
        return self.typedefs.get(f"::{parts[-2]}::{parts[-1]}")

    @property
    def whole_string_summaries(self):
        """Whether LLDB's summary of this build's std::string values shows every byte.
        LLDB reads a libc++ string's size and prints a NUL as `\\0`; its libstdc++ summary
        is the `_M_dataplus._M_p` C string, which ends at the first NUL (#119), so the
        worker shows those itself (``string_value``). Told by the standard library's
        namespaces in the debug information: libc++'s `std::__1`, libstdc++'s
        `std::__cxx11`."""
        typedefs = getattr(self, "typedefs", {})
        return "namespace::__1" in typedefs and "namespace::__cxx11" not in typedefs

    def string_value(self, variable, fetch, read):
        """How to show a C or C++ value's text (see ``cxx_strings``). A libstdc++
        `std::basic_string` (#128), by its type's name, the class a typedef of it names
        (`Bytes`, `const Name &`, `Bytes *`), or, under a name Peeky cannot resolve, its
        members: its bytes read from memory where the debugger's summary ends at a NUL
        or fails, else that summary with a note. libc++'s LLDB shows whole. Any string
        literal is then spelled as C reads it, within the worker's bound, and LLDB's
        `error: summary string parsing error` is never a value. A string shown as its
        text (or as why it has none) is marked "text": the worker lists none of its
        members, which are the library's (the sixth evaluation). None leaves the value."""
        shown = None
        language = getattr(getattr(self, "target", None), "language", None)
        if language == "cpp":
            found = self._string_class(variable.get("type"))
            if not self.whole_string_summaries:
                if found is not None or text_like(variable):
                    shown = libstdcxx_string(variable, fetch, read, found)
                    if shown is not None:
                        shown = {**shown, "text": True}
            elif found is not None and shows_text(variable.get("value"), found):
                shown = {"text": True}
        if shown and ("value" in shown or shown.get("unavailable")):
            # Peeky's own text (read from memory, or noted): spelled and bounded already.
            return shown
        text = variable.get("value")
        ended = False
        if (
            language in ("c", "cpp")
            and isinstance(text, str)
            and CHAR_ARRAY.fullmatch(str(variable.get("type") or "").strip())
            and (units := summary_units(text, 1, "")) is not None
            and units[1]
            and units[0] < SUMMARY_UNITS
        ):
            # LLDB ends a `char[N]` longer than its summary limit with `...` even where
            # the text ended at a NUL within it, which fewer bytes than the limit prove
            # (ninth evaluation: `"    The quick brown fox"...` in a `char[8192]`).
            text, ended = text[:-3], True
        tidied = tidy(text, language in ("c", "cpp")) if isinstance(text, str) else None
        if tidied is None:
            return {**(shown or {}), "value": text} if ended else shown
        return {**(shown or {}), **tidied}

    def preview_value(
        self,
        variable,
        fetch,
        read,
        pointee=False,
        frame=None,
        steps=None,
        stack=None,
        deeper=None,
        threads=None,
    ):
        """A C or C++ struct's, class's, fixed-size array's or standard container's
        preview (#136, #181, ``native_previews``), or None to keep the debugger's text.
        ``frame`` and ``steps`` (the root local's name, then each child's the debugger
        gives) locate the value, whose type the build's debug information gives; without
        it there is no preview, nor in an optimized build (a struct with an address may
        hold values the optimizer has not stored yet). ``fetch(reference, count)`` lists
        a value's first children within the worker's preview budget; ``read`` reads
        memory (or is None), for the strings among its members. With ``pointee``, the
        preview of what the pointer ``variable`` points at, which a selector asked for
        (`p[0]`): lldb-dap lists it as the pointer's children. ``stack``: the stop's
        frames as listed (innermost first), ``deeper(levels)`` more of them,
        ``threads()`` how many threads the program has: a container shows its elements
        only where no standard-library call may be part-way through changing it
        (``containers_settled``)."""
        from peeky.native_previews import preview

        if getattr(getattr(self, "target", None), "language", None) not in ("c", "cpp"):
            return None
        walked = self._typed(frame, steps, walked=True)
        if walked is None:
            return None
        typed, indirect, contained, _ = walked
        known = []

        def settled():
            # Asked only for a container (it reads the stop's threads and frames).
            if not known:
                known.append(
                    self.containers_settled(frame, indirect or pointee, stack, deeper, threads)
                )
            return known[0]

        if contained and not settled():
            # An element of a standard container a library call may be changing (an
            # element `clear()` has destroyed already): not previewed (#181).
            return None
        if getattr(self.target, "optimization", "O0") != "O0" and not (
            not pointee
            and typed.kind() == "array"
            and not typed.narrow()
            and not re.fullmatch(r".+ @ 0x[0-9a-fA-F]+", str(variable.get("value") or ""))
        ):
            # An optimized build's struct may hold values the optimizer has not stored
            # yet: none is previewed. Only an array LLDB shows as text it ends at a NUL
            # (a wide character array) is shown element by element, as it reads them.
            return None
        if pointee:
            address = re.match(r"0x[0-9a-fA-F]+(?=\s|$)", str(variable.get("value") or ""))
            if typed.kind() != "pointer" or not address or int(address[0], 16) == 0:
                return None
            typed = typed.target()
            if typed is None or typed.kind() in ("pointer", "reference"):
                return None
            summary = str(variable.get("value") or "")[address.end() :].strip()
            if typed.container() is not None and summary:
                # The debugger's summary of a standard container follows the address
                # (`0x… size=3`), and it lists the container's elements (#181).
                variable = {**variable, "value": summary}
            else:
                variable = {**variable, "value": f"{variable.get('type')} @ {address[0]}"}

        def text(child):
            shown = self.string_value(child, lambda reference: fetch(reference, 64), read)
            if shown and shown.get("unavailable"):
                return None
            return shown["value"] if shown and "value" in shown else child.get("value")

        return preview(variable, fetch, typed, text, containers=settled)

    def containers_settled(self, frame, indirect, stack, deeper=None, threads=None):
        """Whether a standard container a value of ``frame`` holds is whole (#181): the
        program runs one thread (a stop pauses any other wherever it is, perhaps inside
        a `push_back` on a container this one shares), and nothing may be part-way
        through changing it (``library_call``: a standard-library call, or a
        constructor, destructor or assignment operator). ``threads()``: how many threads
        the program has, or None where that is not known for sure."""
        if threads is None or threads() != 1:
            return False
        return self.library_call(frame, indirect, stack, deeper) is None

    def library_call(self, frame, indirect, stack, deeper=None):
        """A call that may be part-way through changing a container a value of
        ``frame`` holds (#181, #187), or None: ("between", its frame's name) for a
        standard-library call running between the stop and ``frame`` (a `push_back` or
        `clear` there, with an element's constructor or destructor of the program's on
        top); ("lifetime", name) for a constructor, destructor or assignment operator
        below ``frame`` (#181 review); ("stack", name) or ("lifetime-stack", name) for
        one anywhere on the stack where the container is reached through a pointer or
        reference (``indirect``) or is no automatic of the frame (it may be what a
        caller's `clear` changes); ("unknown", None) where the frames cannot be told.
        ``stack``: the frames as listed (innermost first, ten asked for);
        ``deeper(levels)``: the first ``levels`` of them, where those listed may not be
        all (``STACK_READ`` are read). Each may say whether it is the whole stack
        (``_whole``)."""
        listed = _whole(stack, 10)
        frames = list(stack or [])
        ids = [each.get("id") for each in frames]
        if frame is None or ids.count(frame.get("id")) != 1:
            return ("unknown", None)
        index = ids.index(frame.get("id"))
        for each in frames[: index + 1]:
            if _library_frame(each):
                return ("between", each.get("name"))
        for each in frames[:index]:
            if self._lifetime(each):
                # The program's own `slot->~E(); ::new (slot) E(3);` ends and begins an
                # element's lifetime in place (#181 review).
                return ("lifetime", each.get("name"))
        if not indirect:
            return None
        if not listed:
            more = deeper(STACK_READ) if deeper else []
            if not more or not _whole(more, STACK_READ):
                # More frames than are read (a reply of as many as asked for, or fewer
                # than its `totalFrames`): one of them may be the library's.
                return ("unknown", None)
            frames = list(more)
        for each in frames:
            if _library_frame(each):
                return ("stack", each.get("name"))
        for each in frames:
            if self._lifetime(each):
                return ("lifetime-stack", each.get("name"))
        return None

    def changing_container(self, frame, steps, stack=None, deeper=None, threads=None):
        """Why a value read through a standard container's element (`o.items[1]`,
        `o.items[1].name`, a map's entry) is withheld, as a ``Withholding``, or None
        (#187): something may be part-way through changing that container: another
        thread (a stop pauses it wherever it is, perhaps inside a `push_back` on it),
        unless ``threads()`` proves the program runs one; or a library call, or a
        constructor, destructor or assignment operator (``library_call``), as `clear()`
        is while an element's destructor runs, the vector still counting the elements it
        has destroyed. ``steps``: the root local's name, then each child's the debugger
        gives."""
        if getattr(getattr(self, "target", None), "language", None) not in ("c", "cpp"):
            return None
        walked = self._typed(frame, steps, walked=True) if steps and len(steps) > 1 else None
        if walked is None or walked[3] is None:
            return None
        held, container = walked[3]
        container = container if len(container) <= 60 else container[:59] + "…"
        count = threads() if threads is not None else None
        if count != 1:
            how = (
                "Peeky cannot tell how many threads the program runs"
                if count is None
                else f"the program runs {count} threads"
            )
            return Withholding(
                f"{how}: a stop pauses every other one wherever it is, perhaps part-way "
                f"through changing `{container}`, which this is read through (a `push_back` "
                "moving its elements); read it where the program runs one thread",
                "not shown",
                "it is read through a standard container another thread may be changing",
            )
        found = self.library_call(frame, held, stack, deeper)
        if found is None:
            return None
        where, name = found
        if where == "between":
            reason = (
                f"a standard-library call below this frame ({_short_frame(name)}) may be "
                f"part-way through changing `{container}`, which this is read through (an "
                "element `clear()` destroyed reads freed memory); read it after that call "
                "returns"
            )
        elif where == "lifetime":
            reason = (
                f"a constructor, destructor or assignment operator below this frame "
                f"({_short_frame(name)}) may be part-way through building or ending an "
                f"element of `{container}`, which this is read through; read it after that "
                "call returns"
            )
        elif where == "lifetime-stack":
            reason = (
                f"`{container}` is read through a pointer or reference while a constructor, "
                f"destructor or assignment operator runs on this thread ({_short_frame(name)}),"
                " which may be part-way through building or ending one of its elements; read "
                "it where none runs"
            )
        elif where == "stack":
            reason = (
                f"`{container}` is read through a pointer or reference while a "
                f"standard-library call runs on this thread ({_short_frame(name)}), which "
                "may be part-way through changing it; read it where no library call runs"
            )
        else:
            reason = (
                "Peeky cannot read every frame of this stop to rule out a standard-library "
                f"call part-way through changing `{container}`, which this is read through"
            )
        return Withholding(
            reason,
            "not shown",
            "it is read through a standard container a library call may be changing",
        )

    def _lifetime(self, frame):
        """Whether a frame runs a C++ constructor, destructor or assignment operator,
        which may be part-way through building, ending or replacing an element of a
        container (#181 review): by its function's debug information or its name."""
        return self._constructs(frame) or bool(_LIFETIME_NAME.search(str(frame.get("name") or "")))

    def member_value(self, variable, frame, steps):
        """How to show a field LLDB reads wrongly, or None to keep its text (#136): a
        char-typed bitfield LLDB reads as an unsigned character (`'\\x05'` for
        `signed char sc:3` holding -3) as {"value": the number its bits hold,
        sign-extended where signed}; a wide character bitfield, which LLDB reads at other
        bits than the program's, or a char bitfield whose text is no character, as
        {"unavailable": True, "reason": ...}."""
        from peeky.native_previews import WIDE_BITFIELD, bitfield_text

        if getattr(getattr(self, "target", None), "language", None) not in ("c", "cpp"):
            return None
        typed = self._typed(frame, steps)
        shown = bitfield_text(variable.get("value"), typed) if typed is not None else None
        if shown is None:
            return None
        if shown == "<bitfield>":
            reason = (
                WIDE_BITFIELD
                if typed.wide_character()
                else "a char-typed bitfield whose text from the debugger is no character"
            )
            return {"unavailable": True, "reason": reason}
        return {"value": shown}

    def _typed(self, frame, steps, walked=False):
        """``native_previews.Typed`` of the value ``steps`` name in ``frame`` (its root
        local, then the debugger's child names), from the build's debug information, or
        None where it cannot be told (no debug information read, a name declared twice
        with two types, a child the type does not have). With ``walked``, (Typed,
        whether the path goes through a pointer or reference, or starts at a local that
        is no automatic or parameter of the frame or one it reaches through a pointer
        (``fields.referred_locals``), whether it goes through a standard container's
        element, and for the last such container (whether it is reached through a
        pointer or reference, its path: `o.items`), or None)."""
        from peeky.fields import Unsettable
        from peeky.native_previews import Typed

        if getattr(self, "fields", None) is None or not steps or frame is None:
            return None
        function = self._function_at(frame)
        if function is None or function.get("types") is None:
            return None
        try:
            shown = self._shown_where(steps[0], self._block_pc(frame))
            offset, framed = self._declared_type(function["types"], steps[0], shown)
        except Unsettable:
            return None
        typed = Typed(self.fields, offset)
        root = str(steps[0])
        plain = split_located(root)
        referred = function.get("referred")
        # A local the frame reaches through a pointer (a structured binding of a
        # reference, `auto& [items, n] = *owner`, which the debug information types as
        # the vector itself), or one whose locations were not all read, counts as one
        # reached through a pointer (#181 review); so does a row LLDB places where
        # Peeky cannot read (#199 review).
        reached = (
            referred is None
            or (plain is None and looks_located(root))
            or (plain[0] if plain else root) in referred
        )
        indirect, contained, held, path = not framed or reached, False, None, root
        for step in steps[1:]:
            holder = typed
            if typed.kind() in ("pointer", "reference"):
                indirect = True
                if not str(step).startswith("*"):
                    # The debugger lists what it refers to as its own children (#102).
                    holder = typed.target()
            if holder is not None and holder.container() is not None:
                # The container's own selector: what a pointer points at is `p[0]`.
                place = path + "[0]" if typed.kind() == "pointer" else path
                contained, held = True, (indirect, place)
            path += str(step) if str(step).startswith("[") else f".{step}"
            typed = typed.child(step)
            if typed is None:
                return None
        if walked:
            reached = indirect or typed.kind() in ("pointer", "reference")
            return typed, reached, contained, held
        return typed

    def _string_class(self, kind):
        """``cxx_strings.string_class`` of a type, also through a typedef the build's
        debug information resolves (a reference's or pointer's too), or None."""
        if not isinstance(kind, str) or (found := string_class(kind)) is not None:
            return found
        bare = re.sub(r"(?:\s*&{1,2}|\s+(?:const|volatile))+$", "", kind.strip())
        indirect = "&" if bare != kind.strip() else ""
        resolved = self.typedef_kind(bare) or ("", "")
        if resolved[0] == "reference":
            indirect = "&"
        if (aliased := self.aliased_type(kind)) and (found := string_class(aliased)):
            return found[0], found[1], indirect or found[2]
        pointee = resolved[1] if resolved[0] == "pointer" else None
        for target in (self.pointee_aliased_type(kind, pointee), pointee):
            if target and (found := string_class(target)) and not found[2]:
                return found[0], found[1], "*"
        return None

    def aliased_type(self, kind):
        """The class a typedef'd value's type names (`Index`, `const Index &` for
        `using Index = std::map<int, int>`), as lldb-dap would show it, or None: a map or
        set by another name still reads by key (#119)."""
        if not isinstance(kind, str):
            return None
        bare = re.sub(r"(?:\s*&{1,2}|\s+(?:const|volatile))+$", "", kind.strip())
        found = self.typedef_kind(bare)
        if found is not None and found[0] in ("other", "reference") and found[1]:
            return found[1]
        return None

    def pointee_aliased_type(self, kind, pointee=None):
        """``aliased_type`` of what a pointer points at (`IntSet *`), or of a typedef'd
        pointer's ``pointee`` as the debug information names it; None for a value that is
        no pointer."""
        if pointee is None and isinstance(kind, str):
            found = re.fullmatch(r"(.+?)\s*\*\s*(?:const\s*)?", kind.strip())
            pointee = found[1] if found else None
        return self.aliased_type(pointee) if pointee else None

    def not_yet_declared(
        self, variable, frame, innermost=True, stack=None, deeper=None, siblings=None, stop=None
    ):
        """Why a root local of ``frame`` has no value yet (declared below the stop), or
        None. Its storage then holds whatever an earlier call or iteration left.
        ``stack``: the frames listed with it (innermost first), when known; ``deeper()``:
        more of them, when a rule needs what runs below the listed ones. ``siblings``
        (the frame's rows) and ``stop`` (the stop's reason) are for Rust's rules."""
        name, path = variable.get("name"), ((frame or {}).get("source") or {}).get("path")
        line = (frame or {}).get("line")
        if (unstored := self._prologue_unstored(variable, frame)) is not None:
            return unstored
        if name == "this" and (building := self._under_construction(frame, stack, deeper)):
            return building
        if looks_located(name) and split_located(name) is None:
            # LLDB names it with where it is declared, in a form Peeky cannot read: which
            # declaration it is cannot be told, so nothing judges it (#195 review).
            return (
                f"LLDB names it {str(name)[:200]!r}, with where it is declared, in a form "
                "Peeky cannot read, so which declaration it is cannot be told: it may not "
                "be initialized yet, and its storage may hold leftovers"
            )
        if not self.declarations and self.__dict__.get("unjudged") and isinstance(name, str):
            return self._unjudged(name, frame)
        if (unplaced := self._unplaced(variable, frame)) is not None:
            return unplaced
        if not self.declarations or not isinstance(name, str) or not isinstance(path, str):
            return None
        try:
            source = Path(path).resolve().relative_to(self.prepared.workspace.resolve())
        except (OSError, ValueError):
            return None
        if type(line) is not int:
            return None
        here = stopped = source.as_posix()
        raw_line = line
        function = self._function_at(frame)
        # A line of another file, or outside the function's own lines (a class's member
        # initializer, a local class's too), runs as part of one of its own: the rules
        # run from there. A caller frame on another file's line keeps it: everything
        # declared elsewhere is withheld there.
        if innermost or (function is not None and function.get("source") == here):
            place = self._own_place(frame, here, line)
            if place is None:
                if self._parameter(name, frame):
                    return None
                return (
                    f"this frame runs {here}:{line}, outside its function's own lines, and "
                    "Peeky cannot place that among them, so this local may not be "
                    "initialized yet: its storage may hold leftovers; read it once the "
                    "frame is back in its own lines"
                )
            here, line = place
        # At the first instruction of the line's statements, none has run (#169). In an
        # optimized build, None: where the stop is in the line cannot be told (#173).
        entering = (
            (
                lambda: (
                    self.mid_statement(variable, frame) is None
                    if getattr(self.target, "optimization", "O0") == "O0"
                    else None
                )
            )
            if innermost and (here, line) == (stopped, raw_line)
            else None
        )
        if shadowed := self._block_storage(
            name, frame, here, line, (stopped, raw_line), entering=entering, siblings=siblings
        ):
            return shadowed
        if not innermost and (
            unfinished := declared_unfinished(
                self.declarations,
                name,
                here,
                line,
                self._unslid(frame),
                ends=lambda at, local: self._statement_end(here, at, local),
                chosen=self._chosen(name, frame),
            )
        ):
            at, why, detail = unfinished
            if why == "file":
                return (
                    f"read it after the call returns. It is declared on line {at} of "
                    + (detail or "a file outside the program's sources")
                    + f", and this caller frame is in another file ({stopped}:{raw_line}), "
                    "which Peeky cannot place before or after the declaration: it may not be "
                    "initialized, and its storage may hold leftovers"
                )
            if why == "statement":
                return (
                    f"read it after the call returns. It is declared on line {at}, and "
                    + (
                        f"its declaration (lines {at}-{detail}) is still running in this "
                        "caller frame"
                        if detail is not None
                        else "Peeky cannot tell from the source where its declaration ends, so "
                        "this caller frame may be inside it"
                    )
                    + ": it may not be initialized yet, and its storage may hold leftovers"
                )
            if why == "no code":
                return (
                    "read it in the frame that is running, once it is stored. It is declared "
                    f"on line {at}, which has no code of its own (an aggregate is stored "
                    "element by element), so it may not be initialized yet: its storage may "
                    "hold leftovers"
                )
            # A caller is inside a call: `long r = f(x);` stores `r` only after it.
            shown = split_located(name)
            local = shown[0] if shown else name
            if at < line and (word := self._header(here, at, local)) in ("for", "while"):
                # Its header's code also runs after the body, so the frame's place proves
                # nothing; nor can the text (a `goto` into the body, a macro's call). Kept
                # under the worker's 300-character cap on a reason.
                return (
                    "read it in the loop's body: a probe there, or `step --out` to this frame. "
                    f"It is declared in this caller frame's `{word}` header, on line {at}, and "
                    "Peeky cannot prove the loop has run its initialization: its storage may "
                    "hold leftovers"
                )
            return (
                f"read it after the call returns. It is declared on line {at}, whose code this "
                "caller frame has not finished, so it may not be initialized yet: its storage "
                "may hold leftovers"
            )
        address = self._unslid(frame)
        below = declared_below(
            self.declarations,
            name,
            here,
            line,
            through=self._through if innermost else None,
            # The stop's own place, where the line is its own; a caller frame's return
            # address, or a line remapped from another, says nothing of this line.
            address=address if innermost and (here, line) == (stopped, raw_line) else None,
            # Which declaration of the name the debugger shows there.
            pc=self._block_pc(frame),
        )
        if below is not None and (jump := self._setjmp_above(frame, line)) is not None:
            return self._setjmp_note(below[0], jump, "declared at line {}, below this stop")
        if below is not None:
            at, why = below
            if why is None:
                return (
                    f"declared at line {at}, below this stop, so not initialized yet: its "
                    f"storage holds leftovers; stop after line {at} to read it"
                )
            reason = {
                "label": "a label in the function lets a `goto` return above it, so this "
                "may be a later pass",
                "no code": f"line {at} has no code of its own, and Peeky cannot tell where "
                "its declaration ends, to place this frame before it",
                "after": f"code of line {line} also runs after its declaration's (as a "
                "temporary's cleanup does), and Peeky cannot place this stop before it",
                "unknown": "the function declares more than one of that name, and Peeky "
                "cannot tell which one the debugger shows here",
            }[why]
            return (
                f"stop after line {at} to read it. It is declared at line {at}, below this "
                f"stop's line, and Peeky cannot prove the stop comes before it: {reason}. So it "
                "may not be initialized, and its storage may hold leftovers"
            )
        if innermost and (
            running := declared_running(
                self.declarations,
                name,
                here,
                line,
                address,
                self._through,
                self._included,
                self._include_site,
                self._jumped_past_include,
            )
        ):
            at, end, declared_in, where = running
            if declared_in != here:
                origin = declared_in or "a file outside the program's sources"
                if where is not None and where >= line:
                    return (
                        f"declared on line {at} of {origin}, which the `#include` on line "
                        f"{where} brings in, below this stop, so not initialized yet: its "
                        f"storage holds leftovers; stop after line {where} to read it"
                    )
                return (
                    f"declared on line {at} of {origin}, and Peeky cannot tell whether it has "
                    f"run by {here}:{line} ("
                    + (
                        f"code after the `#include` on line {where} can run on or skip it"
                        if where is not None
                        else f"no one `#include` in {here} brings it in"
                    )
                    + "): it may not be initialized yet, and its storage may hold leftovers"
                )
            return (
                (
                    f"declared on line {at}, and its declaration (lines {at}-{end}) is still "
                    f"running at this stop: it is not fully initialized yet, and its storage "
                    f"may hold leftovers; stop after line {end} to read it"
                )
                if end is not None
                else f"declared on line {at}, and Peeky cannot tell from the source where its "
                "declaration ends (a directive, a macro or a line splice can hide it), so this "
                "stop may be inside it: it may not be initialized yet, and its storage may "
                "hold leftovers"
            )
        if building := self._foreign_line(variable, frame, stopped, raw_line):
            return building
        if skipped := declared_skipped(
            self.declarations, name, here, line, self._block_pc(frame), self._jumped_past
        ):
            at, label = skipped
            return (
                (
                    f"declared on line {at}, but a jump (a `switch` to its `case`, or a "
                    f"`goto`) can reach the label on line {label} without running the "
                    "declaration, so it may not be initialized here: its storage may hold "
                    "leftovers"
                )
                if label is not None
                # Neutral: no `switch` or `goto` may be there at all (ninth evaluation:
                # a function whose only jump was a `continue`).
                else f"declared on line {at}, and Peeky cannot tell from the source whether "
                "this line can be reached without running the declaration, so it may not be "
                "initialized here: its storage may hold leftovers"
            )
        if address is None or self.target.optimization != "O0":
            return None
        # A loop that branches back to this address can bring control here with the
        # local live (x86-64 puts `while ((x = next()) > 0)`'s test first): only an
        # address the disassembly shows no loop returns to is withheld.
        on_line = declared_on_line(self.declarations, name, here, line, address)
        if on_line is not None and getattr(self, "loop_targets", {}).get(address) is not False:
            # A loop returns to this first instruction (a `do` body's first line), or
            # Peeky cannot tell: on a later pass the local holds the last pass's value,
            # on the first its leftovers. Shown, with a note on the row.
            return ValueNote(
                f"declared on line {line}, which runs after this stop on every pass (a loop "
                "can return to its first instruction)",
                "may be the last pass's value, or leftovers on the first pass: "
                + (
                    "this line is reached again by the loop"
                    if getattr(self, "loop_targets", {}).get(address)
                    else "Peeky cannot tell whether a loop returns to this line"
                )
                + ", before its declaration runs; read it on the next line",
                refuses=True,
            )
        if on_line is not None and (jump := self._setjmp_above(frame, line)) is not None:
            return self._setjmp_note(
                line, jump, "declared on line {}, at whose first instruction this stop is"
            )
        if on_line is not None:
            if on_line[1] == "label":
                return (
                    f"stop after line {line} to read it. This stop is at its declaration's "
                    "first instruction, and a `goto` can return here with it live: Peeky "
                    "cannot tell that from a first pass, so it may not be initialized, and its "
                    "storage may hold leftovers"
                )
            if on_line[1] == "unknown":
                return (
                    f"declared on line {line}, at whose first instruction this stop is, or "
                    "another local of that name is shown, which Peeky cannot tell apart: it "
                    "may not be initialized, and its storage may hold leftovers; stop after "
                    f"line {line} to read it"
                )
            return (
                f"declared on line {line}, which has not run yet at this stop (its first "
                "instruction), so not initialized: its storage holds leftovers; step once, "
                f"or stop after line {line}, to read it"
            )
        # In C++ an object's initialization can construct it (a call) without a `(`.
        constructs = getattr(self.target, "language", None) == "cpp" and _is_object(variable)
        if innermost and declared_mid_line(
            self.declarations,
            name,
            here,
            line,
            address,
            lambda *at: not constructs and self._settled(*at),
        ):
            return (
                f"declared on line {line}, and this stop is part-way through that line, "
                "whose code still to run may store it, so it may not be initialized yet: its "
                f"storage may hold leftovers; stop after line {line} to read it"
            )
        return None

    def _prologue(self, frame):
        """The _prologue_windows window ``frame`` runs in, or None: there its function has
        not stored its parameters yet (#279)."""
        windows, pc = getattr(self, "prologues", None), self._block_pc(frame)
        if not windows or pc is None:
            return None
        index = bisect.bisect_right(windows, pc, key=lambda window: window.entry) - 1
        if index >= 0 and windows[index].entry <= pc < windows[index].end:
            return windows[index]
        return None

    def _prologue_unstored(self, variable, frame):
        """Why the root local ``variable`` of ``frame`` holds leftovers in its function's
        prologue (#279), or None. GCC marks no prologue end, so LLDB stops a function
        whose code is all on its first line at its first instruction, before the
        parameters are stored; a step into it stops there too. Every parameter and local
        is withheld there, but those the prologue leaves alone (``kept``). Where the
        frame's address is unknown, _prologue_unplaced says why it may be there."""
        name = variable.get("name")
        if not isinstance(name, str):
            return None
        base = (split_located(name) or (name,))[0]
        if (window := self._prologue(frame)) is None:
            return self._prologue_unplaced(base, name, frame)
        if base in window.kept:
            return None
        where, why, read, change = _prologue_texts(window)
        return Withholding(
            f"{where}: its storage holds leftovers{why}. {read}",
            None,
            f"{name!r} cannot be changed here: {where}, and the function's code may "
            f"overwrite a change. {change}",
        )

    def _prologue_unplaced(self, base, name, frame):
        """Why a root local (``base``: its plain name) of ``frame``, whose address Peeky
        cannot read, may hold leftovers, or None (review of #279). A frame on a line where
        a prologue runs may be stopped before it stored the parameters: at a GCC
        function's start, which marks no prologue end, or in any prologue on a fault or a
        signal. So its locals are withheld there, but what that prologue leaves alone."""
        windows = getattr(self, "prologues", None)
        if not windows or self._block_pc(frame) is not None:
            return None
        place = self._place(frame)
        found = [window for window in windows if place in window.lines] if place else []
        if not found or all(base in window.kept for window in found):
            return None
        pointer = (frame or {}).get("instructionPointerReference")
        if getattr(self, "load_bias_unknown", False):
            why = "the debugger did not report where the program is loaded"
        elif not isinstance(pointer, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,16}", pointer):
            why = "the debugger gave no instruction address for this frame"
        else:
            why = "Peeky could not read where the program is loaded"
        where = (
            "Peeky cannot tell whether the stop is before or after its function's prologue "
            f"stores the parameters: {why}, so it cannot place the stop's instruction"
        )
        if all(window.single for window in found):
            read = "Read the arguments at the call, or put the body on a line of its own"
            change = "put the body on a line of its own and change it there"
        else:
            read = "Read the arguments at the call, or stop past the function's first line"
            change = "change it past the function's first line"
        return Withholding(
            f"{where}. {read}",
            None,
            f"{name!r} cannot be changed here: {where}. Change the argument at the call, or "
            f"{change}",
        )

    def _statement_end(self, source, line, name):
        """Where the declaration of ``name`` on ``line`` of the prepared ``source`` ends
        (c_source.Statements), or None, also when a source or header cannot be read."""
        statements = self._statements(source)
        return None if statements is None else statements.end(line, name)

    def _through(self, source, line, name):
        """Statements.declared_through for the prepared ``source``, or None."""
        statements = self._statements(source)
        return None if statements is None else statements.declared_through(line, name)

    def _header(self, source, line, name):
        """The keyword of the statement header that declares ``name`` on ``line`` (`for`,
        `while`, …), or None (Statements.header)."""
        statements = self._statements(source)
        return None if statements is None else statements.header(line, name)

    def _settled(self, source, line, name, span=None):
        """Whether a statement's header sets ``name`` on ``line`` once, by no call, where
        no jump can enter the statement past it (True, False; the function's lines
        ``span``), or None if unknown (Statements.declaration, jumped_into): `goto
        inside;` to a label in a one-line loop's body skips `int i = 0`."""
        statements = self._statements(source)
        found = None if statements is None else statements.declaration(line, name)
        if found is None or found[3] is not True:
            return None if found is None else found[3]
        return statements.jumped_into(line, name, tuple(span) if span else None) is False

    def _jumped_past_include(self, source, line, stop, span):
        """Statements.jumped_past_include for the prepared ``source``; None if unreadable."""
        statements = self._statements(source)
        return None if statements is None else statements.jumped_past_include(line, stop, span)

    def _include_site(self, source, target, depth=8):
        """The line of the one `#include` in the prepared ``source`` that brings in
        ``target`` (directly, or through the program's other headers), else None: none
        does, more than one does, or one Peeky cannot read (a macro) may."""
        cache = self.__dict__.setdefault("include_sites", {})
        key = (str(source), str(target), depth)
        if key in cache:
            return cache[key]
        cache[key] = None
        statements = self._statements(source)
        if statements is None:
            return None
        sites = set()
        for number, name in statements.includes():
            if name is None:
                # `#include MACRO`: it may bring in anything.
                return None
            try:
                found = (
                    (self.prepared.workspace / source)
                    .parent.joinpath(name)
                    .resolve()
                    .relative_to(self.prepared.workspace.resolve())
                    .as_posix()
                )
            except (OSError, ValueError):
                continue
            if found == str(target) or (
                depth > 0 and self._include_site(found, target, depth - 1) is not None
            ):
                sites.add(number)
        cache[key] = sites.pop() if len(sites) == 1 else None
        return cache[key]

    def _own_place(self, frame, source, line):
        """Where in its function's own lines a frame stopped at ``source:line`` runs: that
        line if it is one of them; for another file's line that an `#include` in them
        brings in, that `#include`'s line; else (a class's member initializer, run
        where an object is built) the last of its own lines whose code comes before the
        stop's. None if that cannot be told."""
        function = self._function_at(frame)
        if function is None:
            return source, line
        own = function.get("source")
        mine = self._own_line(function, source, line)
        if mine:
            return source, line
        if mine is None:
            return None
        if source != own:
            site = self._include_site(own, source)
            if site is not None and self._own_line(function, own, site):
                return own, site
        address = self._unslid(frame)
        order = function.get("order", ())
        if address is None:
            return None
        index = bisect.bisect_left(order, (address,)) - 1
        while index >= 0:
            _, where, number = order[index]
            if self._own_line(function, where, number):
                return own, number
            index -= 1
        return None

    def _own_line(self, function, source, line):
        """Whether ``source:line`` is one of the function's own lines: within its span in
        its file and outside every class it defines (whose member initializers run where
        an object is built). None if that cannot be told: no span, or a local class whose
        extent the source does not give."""
        if source != function.get("source"):
            return False
        span = function.get("span")
        if span is None:
            return None
        if not span[0] <= line <= span[1]:
            return False
        extents = self._class_extents(function)
        if extents is None:
            return None
        return not any(first <= line <= last for first, last in extents)

    def _class_extents(self, function):
        """The (first, last) lines of each class the function defines (not a lambda's
        closure), from its line in the debug information and the braces after it
        (Statements.class_extent); None if one cannot be found."""
        cache = self.__dict__.setdefault("class_extents", {})
        key = function.get("root")
        if key not in cache:
            places = [line for line, closure in function.get("classes", ()) if not closure]
            statements = self._statements(function.get("source")) if places else None
            extents = [] if not places else None
            if statements is not None:
                found = [
                    statements.class_extent(line) if type(line) is int else None for line in places
                ]
                extents = None if None in found else found
            cache[key] = extents
        return cache[key]

    def _parameter(self, name, frame):
        """Whether ``name`` is a parameter (or static) of the frame's function only."""
        function = self._function_at(frame)
        shown = split_located(name or "")
        places = (function or {}).get("locals", {}).get(shown[0] if shown else name, ())
        return bool(places) and not any(automatic for _, _, automatic in places)

    def _included(self, source, line, name):
        """Statements.included_after for the prepared ``source``; None if unreadable."""
        statements = self._statements(source)
        return None if statements is None else statements.included_after(line, name)

    def _foreign_line(self, variable, frame, source, line):
        """Why an object local of ``frame`` may be under construction, or None: the frame
        runs a line outside its function's own (in another file, or above or below it
        in its own: a class's default member initializer, run where the object is
        built), and the local is a class, struct, union or array. Pointers and
        references (and scalars) keep their values."""
        if str(variable.get("name") or "").startswith("(") or not _is_object(variable):
            return None
        function = self._function_at(frame)
        if function is None or self._own_line(function, source, line) is not False:
            # Its own line, or unknown: then `_own_place` withholds all but parameters.
            return None
        return (
            "read it once the frame is back in its own lines. This frame runs "
            f"{source}:{line}, outside its function's own lines (a default member initializer, "
            "or code an `#include` brings in): this object may be under construction or "
            "skipped, and its storage may hold leftovers"
        )

    def _jumped_past(self, source, line, name, stop, span=None):
        """Statements.jumped_past for the prepared ``source``; None if unreadable."""
        statements = self._statements(source)
        return None if statements is None else statements.jumped_past(line, name, stop, span)

    def _statements(self, source):
        """The prepared ``source``'s c_source.Statements, or None when it, or one of the
        program's sources or headers (for their macros), cannot be read."""
        cache = self.__dict__.setdefault("statements", {})
        key = str(source)
        if key not in cache:
            raw_strings = getattr(self.target, "language", None) == "cpp"
            if "macro_hazards" not in self.__dict__:
                # The program's own sources and headers (not the SDK's or the system's).
                inputs = [self._source_text(each) for each in self.target.verification_inputs]
                self.macro_hazards = None if None in inputs else macro_hazards(inputs, raw_strings)
            text = self._source_text(source)
            hazards = self.macro_hazards
            cache[key] = (
                None
                if text is None or hazards is None
                else Statements(text, raw_strings, hazards=hazards)
            )
            if cache[key] is not None:
                cache[key].included = lambda line, at=key: self._included_text(at, line)
        return cache[key]

    def _included_text(self, source, line):
        """The Statements of the program file the `#include` on ``line`` of ``source``
        brings in, or None (another program's, a system header, or unreadable)."""
        statements = self._statements(source)
        name = dict(statements.includes()).get(line) if statements is not None else None
        if name is None:
            return None
        try:
            found = (
                (self.prepared.workspace / source)
                .parent.joinpath(name)
                .resolve()
                .relative_to(self.prepared.workspace.resolve())
                .as_posix()
            )
        except (OSError, ValueError):
            return None
        return self._statements(found) if found != str(source) else None

    def _under_construction(self, frame, stack, deeper=None):
        """Why `this` in ``frame`` (C++) may point to an object still being built, or
        None: the frame runs its constructor, a constructor runs below the frame
        (``stack``, innermost first; ``deeper()`` lists up to 64 frames when the ten
        listed do not reach the bottom, and beyond that it is unknown), or the frame runs
        a lambda defined where what it captures may be under construction."""
        if getattr(self.target, "language", None) != "cpp" or not isinstance(frame, dict):
            return None
        frames = [listed for listed in stack or () if isinstance(listed, dict)]
        index = next(
            (i for i, listed in enumerate(frames) if listed.get("id") == frame.get("id")),
            None,
        )
        unread = (
            "its members may not be initialized yet and may hold leftovers; read them once "
            "it returns"
        )
        if self._constructs(frame):
            return f"the object this constructor ({frame.get('name')}) is building: {unread}"
        if (self._function_at(frame) or {}).get("initializer"):
            # A class with no constructor runs its member initializers where it is built.
            return (
                "this lambda is defined in a class's default member initializer, so the "
                f"object it captures may be the one being built: {unread}"
            )
        callers = frames[index + 1 :] if index is not None else []
        # The worker lists ten frames: past them, what runs below is not yet known.
        complete = index is not None and len(frames) < 10
        for attempt in range(2):
            for caller in callers:
                if self._constructs(caller):
                    return (
                        f"a constructor ({caller.get('name')}) called this frame, directly "
                        f"or not, so `this` may point to the object it is building: {unread}"
                    )
            if complete or attempt or deeper is None:
                break
            more = [listed for listed in deeper() or () if isinstance(listed, dict)]
            at = next(
                (i for i, listed in enumerate(more) if listed.get("id") == frame.get("id")),
                None,
            )
            if at is not None:
                callers, complete = more[at + 1 :], len(more) < 64
        if not complete:
            return (
                "Peeky cannot list every frame below this one, so a constructor may be "
                f"running there and `this` may point to the object it is building: {unread}"
            )
        entry = self._function_at(frame)
        if entry is None or entry.get("enclosing") is None:
            return None
        if (outer := self._lambda_scope(entry, callers, complete)) is not None:
            return (
                f"this lambda is defined {outer}, which may still be running, so what it "
                "captures may be under construction: its members may not be initialized "
                "yet and may hold leftovers; read them once that has run"
            )
        return None

    def _lambda_scope(self, entry, callers, complete):
        """Where a lambda (or a local class's method), as ``entry``, is defined that may
        still be running, as text, or None: in a constructor, or within a local's
        declaration statement in the function around it (and around that, for a lambda
        in a lambda), unless the frames below show it has finished."""
        by_root = {function.get("root"): function for function in self.declarations}
        for _ in range(4):
            outer, defined = by_root.get(entry.get("enclosing")), entry.get("defined")
            if outer is None or defined is None:
                return "in a function Peeky cannot place"
            running = [caller for caller in callers if self._function_at(caller) is outer]
            if outer.get("constructor"):
                if running or not complete:
                    return f"in a constructor (line {defined})"
            else:
                for name, places in outer["locals"].items():
                    for declared_in, at, automatic in places:
                        if not automatic or type(at) is not int or at > defined:
                            continue
                        end = self._through(declared_in, at, name) if declared_in else None
                        if end is not None and defined > end:
                            continue
                        if not complete and not running:
                            return f"in the declaration of `{name}` (line {at})"
                        for caller in running:
                            place = self._place(caller)
                            if place is None or (
                                place[0] == declared_in and at <= place[1] <= (end or place[1])
                            ):
                                return f"in the declaration of `{name}` (line {at})"
            if outer.get("enclosing") is None:
                return None
            entry = outer
        return None

    def _constructs(self, frame):
        """Whether a frame runs a C++ constructor: by its function's debug information,
        or by its name (`Widget::Widget(long)`)."""
        entry = self._function_at(frame)
        if entry is not None and entry.get("constructor"):
            return True
        return bool(_CONSTRUCTOR_NAME.search(str(frame.get("name") or "")))

    def _function_at(self, frame):
        """The declarations entry of the function a frame runs (the narrowest whose code
        holds its address), or None."""
        address = self._block_pc(frame)
        if address is None or not self.declarations:
            return None
        cache = self.__dict__.setdefault("functions_at", {})
        if address not in cache:
            holding = [
                (high - low, index)
                for index, function in enumerate(self.declarations)
                for low, high in function.get("ranges", ())
                if low <= address < high
            ]
            cache[address] = self.declarations[min(holding)[1]] if holding else None
        return cache[address]

    def _unplaced(self, variable, frame):
        """Why a root local of a C++ frame whose function Peeky cannot place is withheld
        (a Withholding), or None. Every rule above judges a local by its function's
        declarations; a frame that runs a line of the program's own sources in a function
        the build's table does not hold (``_function_at`` finds none) has none to judge
        it by, so none of its locals or parameters is shown. Where the frame's address is
        unknown, that is a frame on a line no function of the table runs. Frames outside
        the program's sources (the standard library's) and C builds are not judged so."""
        language = getattr(getattr(self, "target", None), "language", None)
        if not getattr(self, "declarations", None) or language != "cpp":
            return None
        name = variable.get("name")
        # `(Return Value)` is the function a step left's, not this frame's.
        if not isinstance(name, str) or name.startswith("(") or not self._in_program(frame):
            return None
        if self._function_at(frame) is not None:
            return None
        if self._block_pc(frame) is None:
            place = self._place(frame)
            if place is None or self._line_entries(*place):
                return None
        return Withholding(
            "Peeky cannot place this function's declarations in the program's sources (its "
            "debug information ties it to none), so it cannot tell whether this is set yet: "
            "it may hold leftovers. Read what the function returns (`step --out`), or stop "
            "in a function `peeky describe` lists",
            "not shown",
            f"{name!r} cannot be changed here: Peeky cannot place this function's declarations "
            "in the program's sources, so it cannot tell whether the function's code "
            "overwrites a change. Change it in a function `peeky describe` lists",
        )

    def _line_entries(self, source, line):
        """The declarations entries a stop on ``line`` of ``source`` can be in: for each
        address of the line's code, the narrowest function holding it, as _function_at
        finds a frame's. Empty when the build's declarations were not read."""
        return [entry for entry, _ in self._line_places(source, line)]

    def _line_places(self, source, line):
        """(declarations entry, the addresses of the line's code it holds) for each
        function a stop on ``line`` of ``source`` can be in (see _line_entries)."""
        found, table = {}, self.declarations or ()
        addresses = {
            address
            for function in table
            for address, where, number in function.get("order", ())
            if where == source and number == line
        }
        for address in sorted(addresses):
            holding = [
                (high - low, index)
                for index, function in enumerate(table)
                for low, high in function.get("ranges", ())
                if low <= address < high
            ]
            if holding:
                found.setdefault(min(holding)[1], []).append(address)
        return [(table[index], held) for index, held in found.items()]

    def selected_as(self, variable, frame, siblings=()):
        """The plain name a selector reads this root local by (#158), where LLDB names it
        with where it is declared (`t @ two.c:20`) because another local of its name is
        in scope there too (an outer block's `t @ two.c:18`): the name, for the one the
        code at the stop names (``_named_where``); else None. None too where the debugger
        lists that plain name itself, or another of the name that is no automatic local
        or parameter of the function (a block's `thread_local`), or Peeky cannot tell
        which one the code names: then `t` is refused, and the rows' names select."""
        shown_name = str(variable.get("name"))
        shown = split_located(shown_name)
        if shown is None or not self.declarations:
            return None
        base, names = shown[0], [str(sibling.get("name")) for sibling in siblings]
        function = self._function_at(frame)
        if base in names or function is None:
            return None
        known = {
            (Path(str(where)).name, at)
            for where, at, framed in function["locals"].get(base, ())
            if framed and where is not None
        }
        parameters = {
            (Path(str(where)).name, at)
            for where, at in function.get("parameters", {}).get(base, ())
        }
        for name in names:
            other = split_located(name)
            if other and other[0] == base and other[1:] not in known | parameters:
                return None
        where = self._named_where(base, self._block_pc(frame))
        if not where:
            return None
        if where[0] is None:
            # A parameter: where it is declared, if the build says so once.
            if len(parameters) != 1:
                return None
            place = next(iter(parameters))
        else:
            place = (Path(str(where[0])).name, where[1])
        # Only an alias a change can also give (a selector's bounds): never one that
        # reads a local whose change the worker would refuse.
        return base if place == shown[1:] and changeable(shown_name) else None

    def _named_where(self, name, pc):
        """(source, line, automatic) of the declaration of the local ``name`` the code at
        ``pc`` (unslid) names, as C and C++ scope it (#158), or None where Peeky cannot
        tell or none is in scope. ``name @ file:line`` names its own (``_shown_where``).

        Of the declarations of the name whose block holds the pc (the function's
        automatic locals and parameters: the ones the debugger lists there), the one
        alone, else the innermost whose scope, read from the source as the compiler
        preprocessed it (``_scope_holds``), holds the pc's line: none declared below
        the line (`t` names an outer one until its declaration), and none a statement's
        header declares past that statement (clang gives a `while`'s or a `switch`'s
        no block of its own, so its block holds the code after the statement). None
        if a source cannot be read so, two are as inner, or a declaration is in
        another file than the line."""
        if pc is None or not self.declarations:
            return None
        if split_located(name):
            return self._shown_where(name, pc)
        holding = [
            (high - low, index)
            for index, function in enumerate(self.declarations)
            for low, high in function.get("ranges", ())
            if low <= pc < high
        ]
        if not holding:
            return None
        function = self.declarations[min(holding)[1]]
        visible = function.get("visible")
        if visible is None:
            return None
        found = []
        for where in function["locals"].get(name, ()):
            if not (where[2] or where[0] is None and where[1] is None):
                continue
            ranges = visible.get((name, *where))
            if ranges is None:
                return None
            if spans := [high - low for low, high in ranges if low <= pc < high]:
                found.append((min(spans), where))
        if len(found) <= 1:
            return found[0][1] if found else None
        order = function.get("order", ())
        index = bisect.bisect_right([row[0] for row in order], pc) - 1
        if index < 0:
            return None
        _, source, line = order[index]
        kept = []
        for span, where in found:
            if where[0] is None:
                # A parameter: in scope in the whole function.
                kept.append((span, where))
                continue
            if where[0] != source:
                return None
            holds = self._scope_holds(source, where[1], name, line)
            if holds is None:
                return None
            if holds:
                kept.append((span, where))
        kept.sort(key=lambda pair: pair[0])
        if not kept or (len(kept) > 1 and kept[0][0] == kept[1][0]):
            return None
        return kept[0][1]

    def _scope_holds(self, source, declared, name, line):
        """Whether the declaration of ``name`` on ``source:declared`` is in scope on
        ``source:line`` (Statements.scope_lines), read from each translation unit
        holding the source as the compiler preprocessed it, so that a macro's statement
        reads as its expansion (`WHILE_DECL(long t = f())` is a `while`'s header); None
        where any of them cannot tell, or they disagree."""
        verdicts = set()
        for unit in self._units(source):
            read = self._preprocessed(unit)
            if isinstance(read, str):
                return None
            preprocessed, statements = read
            if source not in preprocessed.files:
                continue
            at, here = preprocessed.line(source, declared), preprocessed.line(source, line)
            scope = statements.scope_lines(at, name) if at and here else None
            if scope is None:
                return None
            verdicts.add(scope[0] <= here <= scope[1])
        return verdicts.pop() if len(verdicts) == 1 else None

    def _chosen(self, name, frame):
        """(source, line) of the automatic local ``name`` means in ``frame``
        (``_named_where``), or None where Peeky cannot tell or it is a parameter."""
        where = self._named_where(name, self._block_pc(frame))
        return (where[0], where[1]) if where and where[0] is not None else None

    def _shown_where(self, name, pc):
        """(source, line, automatic) of the declaration of the local ``name`` the
        debugger shows at ``pc`` (unslid; shown_declaration: the one whose block holds
        it, the innermost), or None where that cannot be singled out or none is."""
        if pc is None or not self.declarations:
            return None
        found = shown_declaration(self.declarations, name, pc)
        return found[1] if found else None

    def _relative(self, program):
        try:
            return Path(program).resolve().relative_to(self.prepared.source_root).as_posix()
        except (OSError, ValueError):
            return None

    def never_settable(self, program, lines, names, text=False):
        """Why a probe's `--set` of one of ``names`` can change nothing at any stop of
        ``lines`` of ``program`` in this build (#138), or None: a field or element whose
        path the debug information rules out (#105: through a pointer, in a union, a
        bitfield, a root not in the frame's own memory) in every function a stop there
        can be in, or a root local the frame reaches through a pointer or reference
        there (a structured binding of a reference). Any other root local is judged at
        the stop (its type as the debugger names it, its declaration's progress)."""
        from peeky.fields import Unsettable, debugger_steps, settable_member
        from peeky.probe import parse_selector

        source = self._relative(program)
        if source is None or self.fields is None or not self.declarations:
            return None
        stops = set(self.lines.get(source, ()))
        for name in names:
            try:
                root, *steps = parse_selector(name)
            except ValueError:
                continue
            for line in lines:
                # Each function a stop there can be in, and in it the declaration of the
                # root the code names where the line's breakpoint stops: its lowest address
                # that runs before the function returns (not an exception's landing pad,
                # where no stop is made). With none, none is singled out (#158). LLDB also
                # stops at the line's code in each other block (#157), but there the stop
                # is part-way through the line, where no change is made (#105), so only
                # the first stop can make one: said where another stop names another (#169).
                places, elsewhere = [], set()
                for entry, addresses in self._line_places(source, line):
                    returned = entry.get("returned")
                    normal = sorted(a for a in addresses if returned is None or a < returned)
                    first = self._named_where(root, normal[0]) if normal else None
                    places.append((entry, first))
                    if any(self._named_where(root, a) != first for a in normal[1:]):
                        elsewhere.add(entry.get("name") or "this function")
                if line not in stops or not places:
                    continue
                failures = []
                for entry, shown in places:
                    if not steps:
                        # A root is judged at the stop (its type as the debugger names it,
                        # its declaration's progress), but one the frame reaches through a
                        # pointer or reference never changes.
                        storages = self._declared_storages(entry.get("types") or {}, root, shown)
                        if "through" not in storages:
                            break
                        failures.append(
                            (entry.get("name") or "this function", _not_framed(root, storages))
                        )
                        continue
                    try:
                        if entry.get("types") is None:
                            raise Unsettable(
                                "Peeky cannot match it to the build's debug information"
                            )
                        kind, framed = self._declared_type(entry["types"], root, shown)
                        storages = self._declared_storages(entry["types"], root, shown)
                        # As the stop's walk reads the selector (`sp[-1]` is `sp[1]`).
                        named = debugger_steps(self.fields, kind, root, steps)
                        if named is None:
                            break
                        settable_member(
                            self.fields,
                            kind,
                            root,
                            named,
                            owner=lambda e=entry, n=named, w=shown: self._caller_advice(
                                e, root, n, w
                            ),
                        )
                        if not framed:
                            raise Unsettable(_not_framed(root, storages))
                    except Unsettable as exc:
                        failures.append((entry.get("name") or "this function", str(exc)))
                        continue
                    break
                else:
                    from peeky.fields import FUNCTION, by_function

                    reasons = by_function(
                        (function, f"in {FUNCTION}, {why}") for function, why in failures
                    )
                    later = (
                        f". To change the `{root}` of another block (the line's later stops in "
                        f"{', '.join(sorted(elsewhere))}), stop at a line that starts in that "
                        "block: those stops are part-way through the line"
                        if elsewhere
                        else ""
                    )
                    return (
                        f"`{name}` cannot be changed at {source}:{line}, at any stop of this "
                        f"build: {reasons}{later}. Refused before the program runs: nothing ran"
                    )
        return None

    def storage_note(self, program, lines, name):
        """For an optimized build's refusal (#138): that the debug information gives the
        local ``name`` no stack slot at ``lines`` (a register or a location list), in
        every function with code there; else ""."""
        from peeky.probe import parse_selector

        source = self._relative(program)
        try:
            root = parse_selector(name)[0]
        except ValueError:
            return ""
        shown = split_located(root)
        plain = shown[0] if shown else root
        entries = [
            entry
            for line in lines
            if source is not None
            for entry in self._line_entries(source, line)
        ]
        storages = [
            storage
            for entry in entries
            for _, storage, _ in (entry.get("types") or {}).get(plain, ())
        ]
        if storages and all(storage in ("register", "other") for storage in storages):
            return (
                f". Here the debug information gives `{plain}` no stack slot (a register "
                "or a location list), so a change could not reach every copy"
            )
        return ""

    def _place(self, frame):
        """(source relative to the workspace, line) of a frame, or None."""
        path = ((frame or {}).get("source") or {}).get("path")
        line = (frame or {}).get("line")
        if not isinstance(path, str) or type(line) is not int:
            return None
        try:
            source = Path(path).resolve().relative_to(self.prepared.workspace.resolve())
        except (OSError, ValueError):
            return None
        return source.as_posix(), line

    def _source_text(self, source):
        """A prepared source's text, or None if it is unreadable or too large."""
        try:
            path = self.prepared.workspace / source
            if path.stat().st_size > MAX_STATEMENT_SOURCE:
                return None
            return path.read_bytes().decode("utf-8", "replace")
        except OSError:
            return None

    def frames_listed(self, frames):
        """The worker lists a stop's frames (innermost first): which one is innermost,
        and which ones a signal interrupted (each just above a signal trampoline: a
        frame of that name outside the program's sources, not a function of the
        program's named so)."""
        self.innermost_id = frames[0].get("id") if frames else None
        self.interrupted_ids = {
            frames[index + 1].get("id")
            for index, frame in enumerate(frames[:-1])
            if _trampoline(frame.get("name")) and not self._in_program(frame)
        }

    def _in_program(self, frame):
        """Whether ``frame`` runs a line of the program's own sources or headers."""
        path = ((frame or {}).get("source") or {}).get("path")
        if not isinstance(path, str) or not path:
            return False
        try:
            relative = Path(path).resolve().relative_to(self.prepared.workspace.resolve())
        except (OSError, ValueError):
            return False
        inputs = {str(each) for each in getattr(self.target, "verification_inputs", ())}
        return relative.as_posix() in inputs

    def _block_pc(self, frame):
        """Where ``frame`` runs, as blocks and lines are matched against it: its address
        (unslid), less one in a frame above the innermost, whose address is where its
        call returns to, maybe past the call's block and line (#158), as LLDB reads it.
        Not in one a signal interrupted (above a signal trampoline): its address is the
        instruction it stopped at, as LLDB reads it too. None if unknown."""
        address = self._unslid(frame)
        innermost = getattr(self, "innermost_id", None)
        identity = (frame or {}).get("id")
        if (
            address is not None
            and innermost is not None
            and identity != innermost
            and identity not in getattr(self, "interrupted_ids", ())
        ):
            return address - 1
        return address

    def _unslid(self, frame):
        """The frame's instruction address in the executable's own numbering, or None."""
        pointer = (frame or {}).get("instructionPointerReference")
        bias = getattr(self, "load_bias", None)
        if bias is None or not isinstance(pointer, str):
            return None
        if not re.fullmatch(r"0x[0-9a-fA-F]{1,16}", pointer):
            return None
        return int(pointer, 16) - bias

    def _block_storage(self, name, frame, source, line, stop=None, entering=None, siblings=None):
        """Why the root local ``name`` of ``frame`` is not what the code at ``source:line``
        reads, or None: a block around the line declares ``name`` after the local, so
        that the code names that instead: a `static` or an `extern` (lldb-dap does not
        list them among the frame's locals: clang declares a block's static in the
        function's scope, and a block's `extern` not at all; but an optimized build's
        `static` kept as a constant is listed, `t @ file:line`, and a row of it is what
        the code names, #173), a `typedef`, an
        enumerator, a function, a C++ `using` declaration or class, and the row shows
        the outer local under that name. Read from the translation unit as the
        compiler preprocessed it, so what a macro, a conditional or an `#include` makes
        of the source is what is read; withheld where Peeky cannot read it. ``stop``:
        the (file, line) the frame runs, where ``source:line`` is an `#include` line
        that brings it in (the preprocessed text has its lines there instead).
        ``entering()``: whether the stop is the first instruction of the statements that
        start on the line (None: it is not), where a range-`for`'s binding there is not
        declared yet. ``siblings``: the frame's rows, when known, to say whether the
        debugger lists what the code names."""
        # An optimized build's local in registers is listed all the same (#173).
        found = shown_declaration(self.declarations, name, self._block_pc(frame), unstored=True)
        if found is None:
            return None
        function = self._function_at(frame)
        base = (split_located(name) or (name,))[0]
        declared = found[1] if found else (None, None, False)
        verdicts, failure = set(), None
        for unit in self._units(source):
            read = self._preprocessed(unit)
            if isinstance(read, str):
                failure = read
                break
            preprocessed, statements = read
            if source not in preprocessed.files:
                continue
            at = preprocessed.line(source, line)
            if at is None and stop is not None and stop[0] in preprocessed.files:
                at = preprocessed.line(*stop)
            after = None
            if declared[1] is not None:
                after = preprocessed.line(declared[0] or source, declared[1])
            if not at or after == 0 or (declared[1] is not None and after is None):
                # The stop's line, or the local's, is not singled out in it.
                verdicts.add(None)
                continue
            # Neither in the frame's memory nor a parameter: possibly a block's `static`
            # an optimized build keeps as a constant, listed as a local (#173).
            own = after if declared[1] is not None and not declared[2] else None
            # A frame whose function is wholly on the line (a lambda there): its body.
            body = bool(
                function is not None
                and function.get("source") == source
                and tuple(function.get("span") or ()) == (line, line)
            )
            storage = statements.block_storage(at, base, after, True, body, own=own)
            if storage and storage[0] == "binding" and entering is not None:
                if (entered := entering()) is None:
                    # An optimized build: whether the stop starts the line, where its
                    # range is read with the outer local, cannot be told (#173).
                    before = statements.block_storage(at, base, after, True, body, True, own)
                    if before != storage:
                        storage = ("binding-unsure", *storage[1:])
                elif entered:
                    storage = statements.block_storage(at, base, after, True, body, True, own)
            if storage:
                origin = preprocessed.origins[storage[1] - 1]
                storage = None if origin is None else (storage[0], *origin)
            verdicts.add(storage)
        if failure is None and verdicts == {False}:
            return None
        if failure is not None or len(verdicts) != 1 or None in verdicts:
            why = (
                f"Peeky could not preprocess the source to read it ({failure})"
                if failure is not None
                else "Peeky cannot read that from the source as the compiler preprocessed it"
            )
            return Withholding(
                f"a block around line {line} may declare `{base}` (a `static`, an `extern`, "
                f"an enumerator, a function or a type), which the code would name instead of "
                f"this local: {why}",
                "may not be what the code reads",
                f"{name!r} cannot be changed here: at line {line} the code may name a "
                f"`{base}` a block around it declares, not this local ({why}), so a change "
                "to it may not reach that code",
            )
        kind, declared_in, at = next(iter(verdicts))
        place = f"line {at}" if declared_in == source else f"{Path(declared_in).name}:{at}"
        listed = f"`{base} @ {Path(declared_in).name}:{at}`"
        if siblings is not None:
            # What the frame's rows show: the declaration the code names, listed apart.
            shown = any(
                isinstance(row, dict) and row.get("name") == listed[1:-1] for row in siblings
            )
        else:
            # lldb-dap lists a `static` kept as a constant among the frame's locals.
            shown = (base, declared_in, at) in (function or {}).get("unstored", ())
        unlisted = f"listed separately as {listed}" if shown else "which the debugger does not list"
        what = {
            "using": f"the `{base}` the block's `using` declaration ({place}) brings in, "
            "which the debugger does not list",
            "enumerator": f"the block's enumerator `{base}` ({place}), a constant",
            "function": f"the function `{base}` the block declares ({place})",
            "typedef": f"the type `{base}` the block's `typedef` names ({place})",
            "class": f"the type `{base}` the block declares ({place})",
            "binding": f"the structured binding `{base}` ({place})",
            "binding-unsure": f"the structured binding `{base}` ({place}), unless the stop "
            "starts the line, where the loop's range is read with this local (in an "
            "optimized build Peeky cannot tell which)",
            "thread_local": f"the block's `thread_local` `{base}` ({place}), listed "
            f"separately as {listed}",
            "_Thread_local": f"the block's `_Thread_local` `{base}` ({place}), listed "
            f"separately as {listed}",
            "__thread": f"the block's `__thread` `{base}` ({place}), listed separately as {listed}",
        }.get(kind, f"the block's `{kind}` `{base}` ({place}), {unlisted}")
        names = "may name" if kind == "binding-unsure" else "names"
        return Withholding(
            f"the code here {names} {what}: this row is the outer local of that name",
            "may not be what the code reads"
            if kind == "binding-unsure"
            else "not what the code reads",
            f"{name!r} cannot be changed here: at line {line} the code {names} {what}; the "
            "local this row shows is the outer one, so a change to it would not reach that "
            "code",
        )

    def _units(self, source):
        """The program's translation units that may hold ``source``'s lines: itself, and
        those whose dependencies (`-MD`) may name it (all where they cannot be read)."""
        cache = self.__dict__.setdefault("units_holding", {})
        if source not in cache:
            cache[source] = self._read_units(source)
        return cache[source]

    def _read_units(self, source):
        units = []
        for index, unit in enumerate(self.target.sources):
            unit = str(unit)
            if unit != source:
                try:
                    depends = (self.prepared.workspace / f"peeky-object-{index}.o.d").read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    depends = None
                if depends is not None and Path(source).name not in depends:
                    continue
            units.append(unit)
        return units

    def _preprocessed(self, unit):
        """(c_source.Preprocessed, its Statements) of the translation unit ``unit`` as
        the build's compiler preprocesses it (once a session), or why it could not."""
        cache = self.__dict__.setdefault("preprocessed", {})
        if unit not in cache:
            workspace = self.prepared.workspace.resolve()
            own = {posixpath.normpath(str(each)) for each in self.target.verification_inputs}

            def program(path):
                if path.startswith("<"):
                    return None
                if os.path.isabs(path):
                    try:
                        path = Path(path).resolve().relative_to(workspace).as_posix()
                    except (OSError, ValueError):
                        return None
                path = posixpath.normpath(path)
                return path if path in own else None

            def read(output):
                with output.open("rb") as lines:
                    return Preprocessed(lines, program)

            try:
                tool, arguments = self.target.preprocess_argv(unit)
                preprocessed = _metadata(
                    tool,
                    arguments,
                    workspace,
                    dict(self.prepared.manifest.effective_environment),
                    max_bytes=64 * 1024 * 1024,
                    read=read,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                detail = " ".join(str(exc).split())[:200]
                cache[unit] = f"preprocessing {unit} failed: {detail}"
            else:
                raw_strings = getattr(self.target, "language", None) == "cpp"
                statements = Statements(preprocessed.text, raw_strings, trigraphs=False)
                cache[unit] = (preprocessed, statements)
        return cache[unit]

    def _setjmp_above(self, frame, line):
        """The line of a call that may return twice (`setjmp`) above ``line`` in the
        function ``frame`` runs, 0 if Peeky cannot tell where its calls are, else None
        (the program has none, or the function makes none above the line). One on the
        line itself does not count: a `longjmp` returns past the call, never to the
        line's first instruction, where a stop on the line is."""
        if self.__dict__.get("returns_twice") is None:
            return None
        function = self._function_at(frame)
        if function is None:
            return 0
        cache = self.__dict__.setdefault("returning_lines", {})
        key = function.get("root")
        if key not in cache:
            span, statements = function.get("span"), self._statements(function["source"])
            if span is None or statements is None:
                cache[key] = None
            else:
                lines, unknown = statements.returns_twice(span[0], span[1], self.returns_twice)
                cache[key] = None if unknown else lines
        lines = cache[key]
        if lines is None:
            return 0
        above = [number for number in lines if number < line]
        return min(above) if above else None

    def _setjmp_note(self, at, jump, declared):
        """A row shown with a note where its declaration is at or below the stop and a
        call that may return twice (line ``jump``; 0 unknown) lies above: on the first
        pass it holds leftovers, after a `longjmp` back there its value from before."""
        where = f"line {jump}" if jump else "a line Peeky cannot tell"
        reason = declared.format(at)
        return ValueNote(
            f"{reason}, and this function calls setjmp ({where}) above the stop, so a "
            "`longjmp` can bring control here past its declaration",
            f"may be leftovers, or its value from before a `longjmp`: {reason}, and this "
            f"function calls setjmp ({where}) above the stop, so Peeky cannot tell whether "
            "its declaration has run on this pass; read it after its declaration",
            refuses=True,
        )

    def _unjudged(self, name, frame):
        """The note for a root local whose initialization Peeky does not judge (setjmp,
        unread debug information): an automatic local of the frame's function where the
        build's declarations tell (a parameter or static never needs one), any where
        they do not."""
        kind, detail, table = self.unjudged
        if table:
            address = self._block_pc(frame)
            holding = [
                (high - low, index)
                for index, function in enumerate(table)
                for low, high in function.get("ranges", ())
                if address is not None and low <= address < high
            ]
            if holding:
                function = table[min(holding)[1]]
                shown = split_located(name)
                places = function["locals"].get(shown[0] if shown else name, ())
                if places and not any(automatic for _, _, automatic in places):
                    return None
        text = _UNJUDGED[kind].format(detail)
        return ValueNote(text, text, refuses=False)

    def _unread_statements(self):
        """Why Peeky reads no function's statements for this run, so that no stop allows a
        change (an ``Everywhere`` reason), or None: the build's debug information could
        not be read. (A program using setjmp is read as any other: below a setjmp call,
        `not_yet_declared` notes, and refuses changes to, what may be re-entered.)"""
        kind, detail, _ = getattr(self, "unjudged", None) or (None, None, None)
        if kind == "unreadable":
            return Everywhere(
                f"Peeky could not read the build's debug information ({detail}), so no "
                "value can be changed in this program"
            )
        return None

    def text_storage(self, variable, frame):
        """(kind, shape) of the storage a text (#137) is written in, for text_sets: the
        build's debug information decides, under any typedef (`using S = std::string`,
        `typedef char Tag[8]`): a `char[N]` array (shape {"count": N}) or a std::string
        of char; and refuses one const or volatile (#164: a `const std::string`, a typedef
        of one, an array of `const char`), as it refuses what is neither. ValueError
        naming why; also where the debug information cannot be read."""
        from peeky.fields import Unsettable, _strip
        from peeky.text_sets import qualifier_reason

        name = variable.get("name")
        try:
            kind, refusal = text_kind(variable, "c"), None
        except ValueError as exc:
            kind, refusal = None, exc

        def refused(reason):
            return ValueError(f"`{name}` cannot be changed to a text: {reason}")

        try:
            offset, _, graph = self._root_type(frame, variable)
            _, node, _ = _strip(graph, offset, name)
            declared = None
            if node[0] == "array" and node[3] is not None and len(node[3]) == 1:
                # Its elements' own qualifiers count too (`const char[8]`).
                _, element, _ = _strip(graph, node[2], name)
                if element[0] == "base" and element[1] == "char" and element[3] == 1:
                    declared = ("array", {"count": node[3][0]})
            elif (
                node[0] == "record" and node[9] and re.fullmatch(r"basic_string<char\b.*", node[1])
            ):
                declared = ("std", None)
        except Unsettable as exc:
            found = re.search(r"is (const|volatile|atomic)\b", str(exc))
            if found:
                raise refused(qualifier_reason(found[1])) from None
            if refusal is not None:
                raise refusal from None
            raise refused(str(exc)) from None
        if declared is None:
            if refusal is not None:
                raise refusal
            raise refused(
                f"the build's debug information does not show it as a {variable.get('type')}"
            )
        if kind is not None and declared[0] != kind:
            raise refused(
                f"the debugger shows it as a {variable.get('type')}, which its declared type "
                "does not match"
            )
        if kind == "array" and f"[{declared[1]['count']}]" not in str(variable.get("type")):
            raise refused("the debugger and the debug information give it different sizes")
        return declared

    def validate_mutation_target(self, variable, frame):
        reason = self.not_yet_declared(variable, frame)
        if refusal := getattr(reason, "refusal", None):
            raise ValueError(refusal)
        if reason and getattr(reason, "refuses", True):
            raise ValueError(
                f"{variable.get('name')!r} is {reason}; its declaration would overwrite a "
                "change made here"
            )

    def mid_statement(self, variable, frame, stop=None):
        """Why a change to the root local ``variable`` of the stopped ``frame`` may not
        reach a statement under way at the stop, or None (#105). Part-way through a
        statement, what it has already read may be held in a register or a temporary
        (`long t = s.a + f();`, back from `f` with `step --out`), so a change is allowed
        only where the stop is the statement's first instruction:

        - on one of its function's own lines;
        - on the function's first line (a one-line function's or lambda's), only at
          the first row after the prologue, where a breakpoint there stops;
        - elsewhere, the line must start a statement (Statements.begins), and the stop
          must be at the lowest address of every row, in this function, on the lines
          of the statements that start on it (Statements.statement_span): clang emits
          `t =` / `p.x + g();` with the second line's code first.

        On the first line too, the statements start after the body's `{` and the stop
        must be at the lowest of their rows past the function's entry (`long one(struct P
        p) { long t =` / `p.x + g();` runs line 2 first). And no call in them may be
        inlined (a call the compiler inlined, `always_inline`, `flatten`): its code
        carries the callee's lines and runs before
        the statement's own lowest row (`t = inl(&p) + p.y;`). Anything else, or anything
        Peeky cannot tell, is refused. ``stop``: the stop's reason (unused: the address
        says it). A local the frame reaches through a pointer or reference (a structured
        binding of a reference) is refused at every stop: a change would write through
        it. A stop in the function's prologue (a GCC build's one-line function, #279) is
        refused as such: the statements have not started, but the parameters are not
        stored yet."""
        if "through" in (storages := self._storages(frame, variable)):
            return Everywhere(_not_framed(variable.get("name"), storages))
        if (window := self._prologue(frame)) is not None:
            where, _, _, change = _prologue_texts(window)
            return Advised(f"{where}, and the function's code may overwrite a change", change)
        if not getattr(self, "declarations", None) and (unread := self._unread_statements()):
            # No stop of this run is placed: no other stop would allow a change.
            return unread
        place, function, address = self._place(frame), self._function_at(frame), None
        if function is not None:
            address = self._unslid(frame)
        if place is None or function is None or address is None:
            return "Peeky cannot place the stop among its function's statements"
        return self._statement_start(function, *place, address)

    def _statement_start(self, function, source, line, address):
        """mid_statement's verdict for a stop at ``address`` (the executable's own
        numbering) on ``source:line`` in ``function`` (a declarations entry)."""
        if self._own_line(function, source, line) is not True:
            return (
                f"the stop is on {source}:{line}, outside its function's own lines (a "
                "class's member initializer, or code an `#include` brings in), part-way "
                "through the statement that runs it"
            )
        span, statements = function.get("span"), self._statements(source)
        if statements is None:
            return f"Peeky cannot tell from the source whether line {line} starts a statement"
        first = span is not None and line == span[0]
        if first:
            lines = statements.opening_statements(line)
            if lines is None:
                return (
                    f"the stop is on line {line}, its function's first line, and Peeky "
                    "cannot tell from the source which statement starts there"
                )
        else:
            begins = statements.begins(line)
            if begins is False and statements.old_style(line):
                return (
                    f"line {line} is in a K&R-style definition (parameters declared between "
                    "its `)` and its body), which Peeky does not read"
                )
            if begins is False:
                return f"line {line} continues a statement that an earlier line starts"
            if begins is None:
                return f"Peeky cannot tell from the source whether line {line} starts a statement"
            lines = statements.statement_span(line)
            if lines is None:
                return f"Peeky cannot tell from the source where the statement on line {line} ends"
        where = f"line {line}" if lines[0] == lines[1] else f"lines {lines[0]}-{lines[1]}"
        if any(
            called != source or at is None or lines[0] <= at <= lines[1]
            for called, at in function.get("inlined", ())
        ):
            return (
                f"the statement on {where} makes a call the compiler inlined (`always_inline`, "
                "`flatten`), whose code carries the called function's lines and may run before "
                "the statement's own, so Peeky cannot tell where the statement starts"
            )
        entry = min((low for low, _ in function.get("ranges", ())), default=None)
        addresses = [
            at
            for at, file, number in function.get("order", ())
            if file == source and lines[0] <= number <= lines[1] and not (first and at == entry)
        ]
        if entry is None or not addresses or address != min(addresses):
            where = f"line {line}" if lines[0] == lines[1] else f"lines {lines[0]}-{lines[1]}"
            return (
                f"the stop is not at the first instruction of the statement on {where}: it "
                "is part-way through it (after a call returns, on a loop's later pass, or "
                "where the compiler moved code)"
            )
        return None

    def validate_member(self, frame, root, steps, variable, value, stack=None, deeper=None):
        """Refuse (ValueError) unless `--set` can change ``variable``, the field or
        element ``steps`` (the debugger's child names) reach from the root local
        ``root`` of the stopped ``frame``, to the decimal ``value`` (#105). The local
        must be kept in the frame's own memory (an automatic or a parameter there) and
        hold its value at the stop; the path, read from the debug information, may only
        select fields of plain records and elements of fixed-size arrays; the value
        must be a 16- to 64-bit integer, and ``value`` in its range."""
        from peeky.fields import Unsettable, integer_range, settable_member

        name = str(root.get("name"))
        selector = name + "".join(step if step.startswith("[") else f".{step}" for step in steps)
        try:
            kind, framed, graph = self._root_type(frame, root)
            # What the path reaches first (a pointer, a union...), then the local's state.
            signed, bits, names = settable_member(
                graph,
                kind,
                name,
                steps,
                owner=lambda: self._pointer_advice(frame, name, steps),
            )
            if not framed:
                raise Unsettable(_not_framed(name, self._storages(frame, root)))
            self._root_holds(frame, root, stack=stack, deeper=deeper)
            low, high = integer_range(signed, bits)
            shown, debugger = variable.get("value"), variable.get("type")
            hint = variable.get("presentationHint")
            attributes = hint.get("attributes") if isinstance(hint, dict) else None
            if variable.get("variablesReference", 0) != 0 or "readOnly" in (attributes or ()):
                raise Unsettable(f"the debugger does not show `{selector}` as a writable integer")
            # The debugger's view must be the member the debug information describes.
            agrees = (
                INTEGER_RANGES.get(debugger) == (low, high)
                if debugger in INTEGER_RANGES
                else any(debugger == each or str(debugger).endswith(f"::{each}") for each in names)
            )
            if not agrees:
                raise Unsettable(
                    f"the debugger shows `{selector}` as {debugger!r}, which does not match "
                    f"its declared type ({names[0]})"
                )
            if not (
                isinstance(shown, str)
                and re.fullmatch(r"-?(?:0|[1-9][0-9]{0,19})", shown)
                and low <= int(shown) <= high
            ):
                raise Unsettable(
                    f"the debugger shows `{selector}` as {str(shown)[:40]!r}, not as a number, "
                    "so a change could not be read back"
                )
        except Unsettable as exc:
            raise ValueError(f"`{selector}` cannot be changed: {exc}") from None
        if not low <= int(value) <= high:
            raise ValueError(
                f"Native integer mutation is outside the range of `{selector}` ({names[0]}: "
                f"{low} to {high})"
            )

    def _pointer_advice(self, frame, root, steps):
        """Where to change what the pointer local ``root`` of ``frame`` reaches, at a
        stop (validate_member): ``_caller_advice``, for the declaration the debugger
        shows there."""
        where = self._shown_where(root, self._unslid(frame))
        return self._caller_advice(self._function_at(frame), root, steps, where)

    def _pointer_kind(self, function, root, where):
        """What the pointer root local ``root`` (`p`, or `p @ file:line`) of
        ``function`` (a declarations entry) is, by its declaration ``where`` that
        applies at the stop (``_named_where``, ``_shown_where``): "parameter",
        "local" (an automatic of the frame), or None where Peeky can't tell (no single
        declaration, a static, a register, a declaration the debug information doesn't
        match). Same-named declarations are never pooled: an inner local shadowing a
        parameter is a local."""
        if function is None:
            return None
        shown = split_located(str(root))
        base = shown[0] if shown else str(root)
        types = (function.get("types") or {}).get(base) or ()
        if shown:
            # `p @ file:line` names its declaration (LLDB's name where another `p` is in
            # scope too): a parameter's, or an automatic's, there.
            parameters = (function.get("parameters") or {}).get(base) or ()
            if any((Path(str(at)).name, number) == shown[1:] for at, number in parameters):
                return None if self._redirected(function, base) else "parameter"
            where = (shown[1], shown[2], True)
        if not where:
            return None
        if where[0] is None and where[1] is None:
            # The function's parameter (shown_declaration's place for one; C++'s `this`
            # has no line, but is one in the frame).
            if base in (function.get("parameters") or {}) or any(
                storage == "parameter" for _, storage, _ in types
            ):
                # Only a parameter that still holds what the call passed.
                return None if self._redirected(function, base) else "parameter"
            return None
        if len(where) < 3 or not where[2]:
            return None
        place = (Path(str(where[0])).name, where[1])
        declared = [each for each in types if each[2] == place]
        if declared and all(storage == "automatic" for _, storage, _ in declared):
            return "local"
        return None

    def _redirected(self, function, name):
        """Whether the pointer parameter ``name`` of the C or C++ function ``function``
        (a declarations entry) may hold another value than its call passed, at some stop
        in it (review of #276: `p = &global; p->x++;`). Read from its body as each
        translation unit holding it preprocesses it (``_uses``): an assignment (`p = …`,
        `p += …`), a step (`++`, `--`) or its address (`&p`), and inline assembly or an
        address builtin, which may write it unseen. In C++ also any mention but through
        it (`p->x`, `p[i]`, `*p`): a reference to it (`S *&r = p;`, a `S *&` parameter)
        can write it. True where Peeky can't read the body, or the function declares
        another local of that name."""
        source = function.get("source")
        if not source:
            return True
        cplusplus = getattr(self.target, "language", None) != "c"
        defined = False
        for unit in self._units(source):
            read = self._preprocessed(unit)
            if isinstance(read, str):
                return True
            uses = self._uses({"unit": read, "caller": function, "argument_span": (0, 0)}, name)
            if uses is None:
                # Not defined in this unit, or not readable: it is judged by the others.
                continue
            defined = True
            if any(use in ("changes", "assembly") or use.startswith("builtin:") for use in uses):
                return True
            if cplusplus and self._bare_mention(read, function, name):
                return True
        return not defined

    def _bare_mention(self, unit, function, name):
        """Whether the C++ function ``function``'s body in ``unit`` names ``name`` other
        than through it (`name->x`, `name[i]`, `*name`), or can't be read."""
        defined = self._definition(unit, function)
        if defined is None:
            return True
        inner = defined[1]
        for found in re.finditer(rf"(?<![\w$]){re.escape(name)}(?![\w$])", inner):
            before, after = inner[: found.start()].rstrip(), inner[found.end() :]
            if before.endswith((".", "->", "::")):
                continue  # a member's name, or a qualified one
            if not (re.match(r"\s*(?:->|\[)", after) or before.endswith("*")):
                return True
        return False

    def _caller_advice(self, function, root, steps, where=None):
        """Where to change what the pointer root local ``root`` of ``function`` (a
        declarations entry) reaches, by what its declaration that applies at the stop
        (``where``) is (``_pointer_kind``; tenth evaluation): a proven parameter gets
        ``_chain_advice`` in C, else the call advice; a local or an unknown gets
        fields.pointer_advice's, which claims no call."""
        from peeky.fields import pointer_advice

        kind = self._pointer_kind(function, root, where)
        if kind != "parameter":
            return pointer_advice(root, kind)
        shown = split_located(str(root))
        chained = None
        if getattr(self.target, "language", None) == "c":
            chained = self._chain_advice(function, shown[0] if shown else str(root), steps)
        return chained or pointer_advice(root, "parameter")

    def _chain_advice(self, function, root, steps):
        """Where to change a field (``steps``, the debugger's child names) reached
        through the pointer parameter ``root`` of the C function ``function`` (a
        declarations entry), when the program's sources call it once (seventh
        evaluation, finding 13): `push has one caller: m.c:15 in main, which passes `&r`,
        where `r.tail` is settable: ...`. Where that caller passes its own pointer
        parameter on as is, the calls are followed up to the frame that owns the struct
        (#202, ``_call_chain``): `fits has one caller: wrap.c:40 in wrap, which passes it
        on (`lay`, its own pointer parameter); wrap has one caller: wrap.c:75 in main,
        which passes `&lay`, where ...`. Settable is said, with how to change it, only
        where a probe's stop on the first line of the owner's call statement can make the
        change and the call sees it (``_settable_at_call``), and every function on the
        way only reads through the pointer (``_uses``): a `--set-at` there, or in another
        file (a probe stops in its `--at` file) a probe `--at` that line. Else the call is
        named alone. Where the calls cannot be followed to a local the frame holds (a
        function on the way with several callers, a pointer computed or kept elsewhere, a
        static), no frame is named for the change: the message says so and points to
        `describe`'s `mutable:` line. None, for the general advice, where the function
        itself has several or unknown callers, or declares other locals of the name."""
        declared = (function.get("types") or {}).get(root) or ()
        if not declared or {storage for _, storage, _ in declared} != {"parameter"}:
            return None
        try:
            chain, stopped = self._call_chain(function, root)
            if not chain:
                return None
            site = chain[-1]
            caller, source, line, argument = (
                site["caller"],
                site["source"],
                site["line"],
                site["argument"],
            )
            first, start = self._call_statement(site)
        except (OSError, ValueError, RuntimeError):
            return None
        on_the_way = "".join(
            f"{link['called'].get('name')} has one caller: {Path(link['source']).name}:"
            f"{link['line']} in {link['caller'].get('name')}, which passes it on "
            f"(`{link['argument']}`, its own pointer parameter); "
            for link in chain[:-1]
        )
        named, at = site["called"].get("name"), f"{Path(source).name}:{line}"
        where = at + (
            f" (its statement starts on line {first})" if first not in (None, line) else ""
        )
        heading = f"{on_the_way}{named} has one caller: {where} in {caller.get('name')}"
        elsewhere = (
            "; change the value in the frame that owns the struct, before the calls that "
            "lead here: `describe`'s `mutable:` line lists the fields each function can set"
        )
        if stopped is not None:
            return (
                f"{heading}, which passes it on (`{argument}`, its own pointer parameter, "
                f"where a change is refused too), and {stopped}{elsewhere}"
            )
        passed = re.fullmatch(r"&\s*([A-Za-z_]\w*)", argument or "")
        owned = self._owned(caller, passed[1]) if passed else "pointer"
        if owned == "pointer":
            passes = (
                f", which passes on `{argument}`, and Peeky cannot tell what that points to"
                if argument
                else f", and Peeky cannot tell which of the call's arguments is "
                f"`{site['parameter']}`"
            )
            return heading + passes + elsewhere
        if owned is not None:
            return (
                f"{heading}, which passes `{argument}`, {owned}, which Peeky does not change "
                "(it changes only locals a frame holds): change what sets it with a candidate "
                "edit (`--old`/`--new`)"
            )
        # A probe stops in its `--at` file, one of the program's sources (not a header).
        stoppable = source == function.get("source") or source in map(str, self.target.sources)
        writers = [link["caller"].get("name") for link in chain[:-1] if not self._only_reads(link)]
        if (
            passed
            and steps
            and stoppable
            and not writers
            and first is not None
            and start is not None
            and self._untouched(site, start, passed[1])
            and self._settable_at_call(caller, source, first, passed[1], steps)
        ):
            selector = passed[1] + "".join(
                step if step.startswith("[") else f".{step}" for step in steps
            )
            how = (
                f"a probe's `--set-at {first}@N {selector}=<value>`, N counting line {first}'s "
                f"own stops (find N with `--at {first} --capture {selector} --hits all`)"
                if source == function.get("source")
                # A probe stops in its `--at` file only: `--set-at` names a line of it.
                else f"`peeky probe <target> --at {source}:{first} --set "
                f"{selector}=<value> --set-hit N`, N counting that line's own stops (find N "
                f"with `--capture {selector} --hits all` there)"
            )
            return f"{heading}, which passes `{argument}`, where `{selector}` is settable: {how}"
        besides = f", and so may {' or '.join(writers)}, on the way" if writers else ""
        why = (
            f"Peeky cannot tell that a change at line {first or line} reaches it (a statement "
            f"there may change the value, or make the call, first{besides})"
            if stoppable
            else f"a probe stops only in the program's source files, not in {Path(source).name}"
        )
        return (
            f"{heading}, which passes `{argument}`; change the value where it is owned, before "
            f"that call: {why}"
        )

    def _call_chain(self, function, root):
        """The one calls that lead to the C function ``function`` (a declarations entry)
        through its pointer parameter ``root`` (#202), each ``_single_call``'s site with
        the function it calls ("called") and that function's parameter ("parameter"):
        its one call, then, while that call's caller passes it on as is (its own pointer
        parameter, to the same type, never pointed elsewhere there, and no inline assembly
        there: ``_uses``), the
        caller's one call, up to 16. Returns (those sites, why the last site's caller,
        which passes its own pointer parameter on, is followed no further, or None where
        the last call passes something else); no sites where ``function`` itself has
        several or unknown callers."""
        graph, chain, seen = self.fields, [], set()
        declared = (function.get("types") or {}).get(root) or ()
        pointer = graph.get(_under(graph, next(iter(declared))[0])) if graph and declared else None
        pointee = pointer[2] if pointer and pointer[0] == "pointer" else None
        while True:
            seen.add((function.get("name"), function.get("source"), str(function.get("span"))))
            site = self._single_call(function, root)
            if site is None:
                why = f"Peeky cannot single out one call of {function.get('name')}"
                return chain, why if chain else None
            site.update(called=function, parameter=root)
            chain.append(site)
            caller, argument = site["caller"], site["argument"]
            declared = (caller.get("types") or {}).get(argument or "") or ()
            if len(declared) != 1 or next(iter(declared))[1] != "parameter" or graph is None:
                return chain, None
            pointer = graph.get(_under(graph, next(iter(declared))[0]))
            if not pointer or pointer[0] != "pointer":
                return chain, None
            if not self._same_type(pointer[2], pointee):
                return chain, f"Peeky cannot tell that `{argument}` there points to the same type"
            uses = self._uses(site, argument)
            if uses is None or uses.count("passed") != 1:
                return chain, f"Peeky cannot read how {caller.get('name')} uses `{argument}`"
            if "assembly" in uses:
                return chain, (
                    f"{caller.get('name')} has inline assembly, which may change `{argument}` "
                    "or what it points to"
                )
            builtin = next((use[8:] for use in uses if use.startswith("builtin:")), None)
            if builtin is not None:
                return chain, (
                    f"{caller.get('name')} uses `{builtin}`, which may change `{argument}` or "
                    "what it points to"
                )
            if "changes" in uses:
                return chain, f"{caller.get('name')} may point `{argument}` elsewhere first"
            key = (caller.get("name"), caller.get("source"), str(caller.get("span")))
            if key in seen or len(chain) >= 16:
                return chain, "Peeky follows those calls no further"
            function, root = caller, argument

    def _same_type(self, one, other):
        """Whether the type offsets ``one`` and ``other`` name one type, under typedefs
        and qualifiers: the same entry, or records of one name, kind and size (each
        translation unit has its own entries)."""
        graph = self.fields
        if graph is None or one is None or other is None:
            return False
        one, other = _under(graph, one), _under(graph, other)
        if one is None or other is None:
            return False
        if one == other:
            return True
        first, second = graph.get(one), graph.get(other)
        return bool(
            first
            and second
            and first[0] == second[0] == "record"
            and first[1]
            and first[1:4] == second[1:4]
        )

    def _owned(self, caller, name):
        """For a call passing `&name` in ``caller``: None where ``name`` may be a local the
        caller's frame holds; "pointer" where it is a pointer (what it points to is not
        the caller's); else why Peeky cannot change it there (a static, a global)."""
        from peeky.fields import Unsettable

        types = caller.get("types")
        if types is None:
            return None
        declared = types.get(name)
        if not declared:
            return f"not a local of {caller.get('name')}"
        if {storage for _, storage, _ in declared} == {"static"}:
            return f"a static local of {caller.get('name')}"
        try:
            kind, _ = self._declared_type(types, name)
        except Unsettable:
            return None
        node = self.fields.get(_under(self.fields, kind)) if self.fields is not None else None
        return "pointer" if node and node[0] in ("pointer", "reference") else None

    def _only_reads(self, site):
        """Whether the caller of the one call ``site``, which passes its pointer parameter
        on (``_call_chain``), uses it only for that call and to read integer fields
        through it (``_uses``), so nothing it does changes what the call sees."""
        uses = self._uses(site, site["argument"])
        return uses is not None and uses.count("passed") == 1 and set(uses) <= {"passed", "reads"}

    def _uses(self, site, name):
        """What the caller of the one call ``site`` does with its pointer parameter
        ``name`` at each mention in its body, as the compiler preprocessed it: "passed"
        (the call's own argument), "changes" (`name = q`, `name++`, `&name`: it may point
        elsewhere), "reads" (`name->field` or `name->items[i]`, an integer by the build's
        types, only read) or "other" (`*name`, `name[0]`, `name->field = 1`, another
        call's argument...); and first "assembly" where the body has inline assembly, and
        "builtin:NAME" where it takes an address with a builtin (``_ADDRESSES``), which may
        change either unseen (review of #211). None where Peeky cannot find the body."""
        from peeky.fields import Unsettable, debugger_steps, settable_member

        graph = self.fields
        defined = self._definition(site["unit"], site["caller"])
        declared = (site["caller"].get("types") or {}).get(name) or ()
        if defined is None or graph is None or len(declared) != 1:
            return None
        _, inner, offset = defined
        pointer = graph.get(_under(graph, next(iter(declared))[0]))
        kind = _under(graph, pointer[2]) if pointer and pointer[0] == "pointer" else None
        own, uses = range(*site["argument_span"]), []
        if _ASSEMBLY.search(inner):
            uses.append("assembly")
        if builtin := _ADDRESSES.search(inner):
            uses.append(f"builtin:{builtin[0]}")
        assigned = r"[\s)]*(?:(?:[-+*/%|^&]|<<|>>)?=(?!=)|\+\+|--)"
        for found in re.finditer(rf"(?<![\w$]){re.escape(name)}(?![\w$])", inner):
            before, after = inner[: found.start()].rstrip(), inner[found.end() :]
            if before.endswith((".", "->")):
                continue  # a member's name
            if offset + found.start() in own:
                uses.append("passed")
                continue
            applied = re.search(r"(?:(?<!&)&|\+\+|--)\s*(?:\(\s*)*$", before)
            through = before.endswith("*") or re.match(r"\s*(?:->|\[|\.)", after)
            if not through and (applied or re.match(assigned, after)):
                uses.append("changes")
                continue
            use = re.match(r"\s*->\s*([A-Za-z_]\w*)", after)
            if use is None or applied or before.endswith("*") or kind is None:
                uses.append("other")
                continue
            end, steps = use.end(), [use[1]]
            while (bracket := re.match(r"\s*\[", after[end:])) and len(steps) < 8:
                closed = _arguments(after, end + bracket.end() - 1)
                if closed is None:
                    break
                end, steps = closed[1], [*steps, "[0]"]
            if re.match(rf"\s*(?:->|\.|\(|\[)|{assigned}", after[end:]):
                uses.append("other")
                continue
            try:
                named = debugger_steps(graph, kind, name, steps)
                if named is None:
                    raise Unsettable("no such member")
                settable_member(graph, kind, name, named)
            except Unsettable:
                uses.append("other")
                continue
            uses.append("reads")
        return uses

    @staticmethod
    def _definition(unit, entry):
        """(its parameters, the text of its body, the offset where that text starts) of
        the C function ``entry`` (a declarations entry) defined in ``unit`` (a
        translation unit as ``_preprocessed`` reads it), or None where its definition is
        not there on its first line (a prototype's line, or a K&R definition's, is not)."""
        preprocessed, statements = unit
        text, name, span = statements.blanked, entry.get("name"), entry.get("span")
        at = preprocessed.line(entry.get("source"), span[0]) if span and name else 0
        if not at:
            return None
        end = statements.starts[at] if at < len(statements.starts) else len(text)
        heading = re.compile(rf"(?<![\w$]){re.escape(name)}\s*\(").search(
            text, statements.starts[at - 1], end
        )
        listed = _arguments(text, heading.end() - 1) if heading else None
        brace = text.find("{", listed[1]) if listed else -1
        body = _arguments(text, brace) if brace >= 0 else None
        if body is None or ";" in text[listed[1] : brace]:
            return None
        return _split_arguments(listed[0]), body[0], brace + 1

    def _call_statement(self, site):
        """For the one call ``site`` (``_single_call``'s, in the translation unit as the
        compiler preprocessed it, so a macro's code is where it is used): the first line
        of the statement it is in (``Statements.begins``: `int total = 1 +` / `push(&r,
        1);` starts on the line above), as a line of the call's own file, or None; and the
        offset in the unit's text where that statement starts, where the call is in the
        first statement on that line (`r.tail = 1; total += push(&r, 1);` has another
        before it), else None."""
        preprocessed, statements = site["unit"]
        code, starts = statements.blanked, statements.starts
        call = site["call"]
        line = bisect.bisect_right(starts, call)
        first = None
        for number in range(line, max(0, line - 64), -1):
            begins = statements.begins(number)
            if begins is None:
                return None, None
            if begins:
                span = statements.statement_span(number)
                first = number if span is not None and span[1] >= line else None
                break
        origin = preprocessed.origins[first - 1] if first else None
        if origin is None or origin[0] != site["source"]:
            return None, None
        start = _NONBLANK_CODE.search(code, starts[first - 1]).start()
        if re.search(r"[;{}]|<%|%>", code[start:call]) or site["index"] is None:
            return origin[1], None
        return origin[1], start

    def _untouched(self, site, start, root):
        """Whether nothing that runs before the call reads its arguments may change the
        local ``root`` (passed as `&root`): its statement, from ``start``
        (``_call_statement``'s) as the compiler preprocessed it, is one of `push(ARGS);`,
        `NAME = push(ARGS);` (or `NAME op=`, or a declaration's `TYPE NAME =`), `return
        push(ARGS);` and `if (push(ARGS) ...)`, NAME a plain name other than ``root``;
        ARGS are `&root` and reads (``_reads``), and so is the rest of an `if`'s
        condition; and the caller's body, as preprocessed (a header included inside it
        too), takes no address of ``root`` or its fields elsewhere but to lend it
        (``_lent``), nor names an array in it without an index (``_decays``), so no
        pointer reaches it, and has no inline assembly or address builtin (which may take
        it unseen: ``_ADDRESSES``). Anything else may change it
        first (`r = q, push(&r, 1)`, `push(&r, RESET(r))`, `push(&r, ops[0](r.buf))`
        writing through the array that starts `r`, a pointer `a = &r` set earlier)."""
        from peeky.fields import Unsettable

        _, statements = site["unit"]
        code, caller, opening = statements.blanked, site["caller"], site["opening"]
        closed, spans = _arguments(code, opening), _argument_spans(code, opening)
        if closed is None or spans is None or site["index"] is None or self.fields is None:
            return False
        lead = _CALL_LEAD.fullmatch(code[start : site["call"]])
        if lead is None or lead["name"] == root:
            return False
        if lead["condition"] is not None:
            header = _arguments(code, start + code[start : site["call"]].index("("))
            rest = code[closed[1] : header[1] - 1] if header is not None else None
        else:
            rest = "" if re.match(r"\s*;", code[closed[1] :]) else None
        try:
            kind, _ = self._declared_type(caller.get("types") or {}, root)
        except Unsettable:
            return False
        parts = [code[slice(*span)] for i, span in enumerate(spans) if i != site["index"]]
        if (
            rest is None
            or not re.fullmatch(rf"\s*&\s*{re.escape(root)}\s*", code[slice(*spans[site["index"]])])
            or not all(self._reads(part, root, kind) for part in (*parts, rest))
        ):
            return False
        own = range(site["argument_span"][0], site["argument_span"][1])
        # Its whole body as the compiler preprocessed it, whatever each line's origin: a
        # header included inside it is its code too (review of #211).
        defined = self._definition(site["unit"], caller)
        if defined is None:
            return False
        _, body, begin = defined
        if _ASSEMBLY.search(body) or _ADDRESSES.search(body):
            return False  # it may take the root's address unseen
        address = rf"(?<!&)&(?!&)\s*(?:\(\s*)*{re.escape(root)}(?![\w$])"
        for found in re.finditer(address, body):
            at = begin + found.start()
            if at not in own and not self._lent(site, root, at):
                return False
        # An array in it, named without `&`, is a pointer into it: `keep(r.buf)`.
        for found in re.finditer(rf"(?<![\w$]){re.escape(root)}(?![\w$])", body):
            before = body[: found.start()].rstrip()
            if before.endswith((".", "->")) or begin + found.start() in own:
                continue  # a member's name, or the call's own `&root`
            if not _UNEVALUATED_BEFORE.search(before) and self._decays(kind, body, found.end()):
                return False
        return True

    def _decays(self, kind, text, position):
        """Whether the selector that continues at ``position`` of ``text`` (`.buf`,
        `.rows[i]`, nothing) from a local of type ``kind`` names an array, which a use
        turns into a pointer into the local, by the build's types; True where Peeky cannot
        tell. Through a pointer (`.next[0]`) it is no longer the local's."""
        graph = self.fields
        offset, element, pending, at = _under(graph, kind), None, 0, position
        for _ in range(64):
            node = graph.get(offset) if offset is not None else None
            step = re.match(r"\s*(?:\.\s*([A-Za-z_]\w*)|\[)", text[at:])
            if step is None:
                return pending > 0 or node is None or node[0] == "array"
            if step[1] is not None:
                members = (
                    [m for m in node[5] if m[0] == step[1]] if node and node[0] == "record" else []
                )
                if pending or len(members) != 1:
                    return True
                offset, at = _under(graph, members[0][1]), at + step.end()
                continue
            closed = _arguments(text, at + step.end() - 1)
            if closed is None:
                return True
            if not pending:
                if node is None or node[0] != "array":
                    return node is None
                element, pending = node[2], len(node[3]) if node[3] else 1
            pending, at = pending - 1, closed[1]
            if not pending:
                offset = _under(graph, element)
        return True

    def _reads(self, text, root, kind):
        """Whether the expression ``text`` only reads: no assignment, `++` or `--`, no `&`
        (but `&&`, and the address of a plain name other than ``root``, `&out`, which
        runs nothing), no call of any kind (a `(` after a name, `)`, `]` or member: `f(x)`,
        `(*fp)(x)`, `ops[0](x)`, `v.fn(x)`; `sizeof(x)` and `(int)(x)` too), no block,
        and ``root`` (of type ``kind``) only in reads of its integer fields (`r.count`,
        not `r.buf`, which passes a pointer into it)."""
        from peeky.fields import Unsettable, debugger_steps, settable_member

        if (
            re.search(r"(?<![=!<>])(?:[-+*/%|^]|<<|>>|&(?!&))?=(?!=)|\+\+|--", text)
            or re.search(r"[;{}]|<%|%>", text)
            or re.search(r"[\w$)\]]\s*\(", text)
        ):
            return False
        for found in re.finditer(r"(?<!&)&(?!&)", text):
            named = re.match(r"\s*([A-Za-z_]\w*)(?![\w$])(?!\s*(?:[.\[(]|->))", text[found.end() :])
            if named is None or named[1] == root:
                return False
        for found in re.finditer(rf"(?<![\w$]){re.escape(root)}(?![\w$])", text):
            if text[: found.start()].rstrip().endswith((".", "->")):
                continue  # a member's name
            member = re.match(r"\s*\.\s*([A-Za-z_]\w*)(?!\s*(?:[\[.(]|->))", text[found.end() :])
            if member is None:
                return False
            steps = [member[1]]
            try:
                settable_member(
                    self.fields, kind, root, debugger_steps(self.fields, kind, root, steps)
                )
            except Unsettable:
                return False
        return True

    def _lent(self, site, root, position):
        """Whether the `&root` at ``position`` of the caller's unit (``site``'s) is a whole
        argument of a call by name of one of the program's functions, defined in that
        unit, whose parameter there points to ``root``'s type and is used only as
        `p->field` or `p->array[i]` of a scalar (by the build's types), never with `&`,
        in a body with no inline assembly (`pop(&r, &v)`, pop reading `r->count` and
        `r->buf[r->head]`): such a call leaves no pointer to ``root`` behind."""
        from peeky.fields import Unsettable, debugger_steps, settable_member

        preprocessed, statements = site["unit"]
        text, caller, graph = statements.blanked, site["caller"], self.fields
        if graph is None:
            return False
        depth, opening = 0, None
        for at in range(position - 1, max(-1, position - 65536), -1):
            if text[at] in ")]}":
                depth += 1
            elif text[at] in "([{":
                if depth == 0:
                    opening = at if text[at] == "(" else None
                    break
                depth -= 1
            elif text[at] == ";" and depth == 0:
                break
        named = re.search(r"([A-Za-z_]\w*)\s*$", text[:opening]) if opening else None
        if named is None or text[: named.start()].rstrip().endswith((".", "->")):
            return False
        name, spans = named[1], _argument_spans(text, opening) or []
        index = [i for i, (low, high) in enumerate(spans) if low <= position < high]
        if (
            len(index) != 1
            or name in (caller.get("types") or {})  # a local function pointer
            or not re.fullmatch(rf"&\s*{re.escape(root)}", text[slice(*spans[index[0]])].strip())
        ):
            return False
        callees = [e for e in self.declarations or () if e.get("name") == name and e.get("span")]
        defined = self._definition(site["unit"], callees[0]) if len(callees) == 1 else None
        if defined is None or len(defined[0]) != len(spans):
            return False
        parameters, inner, _ = defined
        if _ASSEMBLY.search(inner) or _ADDRESSES.search(inner):
            return False
        parameter = re.search(r"([A-Za-z_]\w*)\s*$", parameters[index[0]])
        declared = (callees[0].get("types") or {}).get(parameter[1] if parameter else None) or ()
        if {storage for _, storage, _ in declared} != {"parameter"} or len(declared) != 1:
            return False
        try:
            kind, _ = self._declared_type(caller.get("types") or {}, root)
        except Unsettable:
            return False
        pointer = graph.get(_under(graph, next(iter(declared))[0]))
        if (
            not pointer
            or pointer[0] != "pointer"
            or _under(graph, pointer[2]) != _under(graph, kind)
        ):
            return False
        for found in re.finditer(rf"(?<![\w$]){re.escape(parameter[1])}(?![\w$])", inner):
            before = inner[: found.start()].rstrip()
            if before.endswith((".", "->")):
                continue  # a member's name
            use = re.match(r"\s*->\s*([A-Za-z_]\w*)\s*(\[)?", inner[found.end() :])
            if use is None or re.search(r"(?<!&)&\s*(?:\(\s*)*$", before):
                return False
            steps = [use[1], *(["[0]"] if use[2] else [])]
            try:
                settable_member(graph, kind, root, debugger_steps(graph, kind, root, steps))
            except Unsettable:
                return False
        return True

    def _single_call(self, function, parameter):
        """{caller (its declarations entry), source, line, argument (the one passed for
        ``parameter``), index (that parameter's), unit (the translation unit read as
        `_preprocessed` gives it), call, opening and argument_span (offsets in its
        text)} of the one call of the C function ``function`` in the program's
        sources, as the compiler preprocessed them (a macro's calls are where it is
        used), each unit's its own (a header's code is compiled once in each unit that
        includes it, and macros may make the copies differ); None unless that is certain,
        and where ``function`` or the caller is defined in more than one unit. Every
        mention of the function's name must be its definition, a prototype at file
        scope, or a call by name inside a function the build's debug information places
        there: anything else (its address taken, a member's function pointer of that
        name, a declaration inside a function, a file Peeky cannot preprocess) leaves the
        callers unknown."""
        from peeky.c_source import _EXPRESSIONS

        name, span, source = function.get("name"), function.get("span"), function.get("source")
        if not re.fullmatch(r"[A-Za-z_]\w*", str(name)) or not span or not source:
            return None
        definition, calls, reads = None, {}, []
        for unit in self.target.sources:
            read = self._preprocessed(str(unit))
            if isinstance(read, str):
                return None
            reads.append(read)
            preprocessed, statements = read
            text, starts = statements.blanked, statements.starts
            counts = {}
            for found in re.finditer(rf"(?<![\w$]){re.escape(name)}(?![\w$])", text):
                index = bisect.bisect_right(starts, found.start()) - 1
                origin = (
                    preprocessed.origins[index] if 0 <= index < len(preprocessed.origins) else None
                )
                if origin is None:
                    return None
                counts[origin] = counts.get(origin, 0) + 1
                # Each unit's: a header's code is compiled once in each unit that
                # includes it, and macros may make those copies differ (review of #211).
                key = (len(reads), origin, counts[origin])
                before = text[: found.start()].rstrip()
                opening = len(text) - len(text[found.end() :].lstrip())
                if before.endswith((".", "->")) or not text.startswith("(", opening):
                    return None
                arguments = _arguments(text, opening)
                if arguments is None:
                    return None
                word = re.search(r"[A-Za-z_]\w*$", before)
                if word is not None and word[0] in _UNEVALUATED:
                    return None
                typed = before.endswith("*") or (word is not None and word[0] not in _EXPRESSIONS)
                if origin == (source, span[0]):
                    # Defined in one unit only, the one whose code the stop runs.
                    if definition is not None:
                        return None
                    definition = arguments
                    continue
                inside = self._enclosing(*origin)
                if inside is None:
                    # At file scope only a prototype: `static int push(struct ring *, int);`
                    if not typed or not text[arguments[1] :].lstrip().startswith(";"):
                        return None
                    continue
                if typed:
                    # A declaration inside a function (or `a * push(x)`, which reads alike).
                    return None
                calls[key] = (inside, origin, arguments, read, found.start(), opening)
        if definition is None or len(calls) != 1:
            return None
        caller, (where, line), arguments, read, call, opening = next(iter(calls.values()))
        # The caller's code must be the call's unit's alone: its first line in no other.
        first = (caller.get("source"), (caller.get("span") or (None,))[0])
        if None in first or any(
            other is not read and other[0].line(*first) is not None for other in reads
        ):
            return None
        parameters = _split_arguments(definition[0])
        values = _split_arguments(arguments[0])
        spans = _argument_spans(read[1].blanked, opening) or []
        named = [
            i for i, text in enumerate(parameters) if re.search(rf"(?<!\w){parameter}(?!\w)", text)
        ]
        site = {
            "caller": caller,
            "source": where,
            "line": line,
            "argument": None,
            "index": None,
            "unit": read,
            "call": call,
            "opening": opening,
            "argument_span": (0, 0),
        }
        if len(named) != 1 or len(values) != len(parameters) or len(spans) != len(values):
            return site
        site.update(
            argument=" ".join(values[named[0]].split()),
            index=named[0],
            argument_span=spans[named[0]],
        )
        return site

    def _enclosing(self, source, line):
        """The declarations entry of the narrowest function whose own lines (its span)
        hold ``source:line``, or None."""
        holding = [
            (entry["span"][1] - entry["span"][0], index)
            for index, entry in enumerate(self.declarations or ())
            if entry.get("source") == source
            and entry.get("span")
            and entry["span"][0] <= line <= entry["span"][1]
        ]
        return self.declarations[min(holding)[1]] if holding else None

    def _settable_at_call(self, caller, source, line, name, steps):
        """Whether `set` would change ``name`` with ``steps`` (the field the callee
        reaches through the pointer) at a breakpoint on ``source:line`` in ``caller``:
        the type walk and storage `set` checks, ``name`` declared on an earlier line by a
        statement that ends before it, and the stop at the first instruction of the
        statement there (mid_statement's rule, at the line's breakpoint address), not a
        loop's header (a later pass stops past it). False where any of that fails or
        cannot be told: a function with labels (a `goto` can return above a
        declaration), or a program that may call setjmp."""
        from peeky.fields import Unsettable, debugger_steps, settable_member

        if self.fields is None or caller.get("jumps") or self.returns_twice is not None:
            return False
        declared = (caller.get("types") or {}).get(name) or ()
        try:
            kind, framed = self._declared_type(caller.get("types") or {}, name)
            named = debugger_steps(self.fields, kind, name, list(steps))
            if not framed or named is None:
                return False
            settable_member(self.fields, kind, name, named)
        except Unsettable:
            return False
        places = {where for _, _, where in declared}
        storages = {storage for _, storage, _ in declared}
        if len(places) != 1 or None in places:
            return False
        (file, at), statements = next(iter(places)), self._statements(source)
        if statements is None or file != Path(source).name or at >= line:
            return False
        if storages != {"parameter"}:
            end = statements.declared_through(at, name)
            if end is None or end >= line:
                return False
        if not 1 <= line <= len(statements.starts):
            return False
        # A loop's header runs again on each pass, after its first instruction.
        opening = re.match(
            r"[\s}]*([A-Za-z_]\w*)", statements.blanked[statements.starts[line - 1] :]
        )
        if opening is not None and opening[1] in ("for", "while", "do"):
            return False
        rows = [
            address
            for address, where, number in caller.get("order", ())
            if (where, number) == (source, line)
        ]
        return bool(rows) and self._statement_start(caller, source, line, min(rows)) is None

    def _root_type(self, frame, root):
        """(type offset, whether the frame keeps it in its own memory, type graph) of
        the root local of a field or element change; raises fields.Unsettable unless it
        is one local of one type."""
        from peeky.fields import Unsettable

        function = self._function_at(frame) if self.fields is not None else None
        if function is None or function.get("types") is None:
            raise Unsettable(
                self._unread_statements()
                or "Peeky cannot match this frame to the build's debug information (it reads "
                "it when the session starts)"
            )
        name = root.get("name")
        shown = self._shown_where(name, self._unslid(frame))
        return (*self._declared_type(function["types"], name, shown), self.fields)

    def _root_holds(self, frame, root, stack=None, deeper=None):
        """Raise fields.Unsettable unless the root local holds its value at the stop:
        not declared below it, in an initializer still running, or skipped by a jump
        (#120, #121)."""
        from peeky.fields import Unsettable

        reason = self.not_yet_declared(root, frame, stack=stack, deeper=deeper)
        if refusal := getattr(reason, "refusal", None):
            raise Unsettable(refusal)
        if reason and getattr(reason, "refuses", True):
            raise Unsettable(
                f"`{root.get('name')}` is {reason}; its declaration would overwrite a change "
                "made here"
            )

    @staticmethod
    def _declared_storages(types, name, where=None):
        """The storages of the declarations of ``name`` that ``_declared_type`` reads
        (fields.local_types); empty where it declares none."""
        shown = split_located(str(name))
        declared = types.get(shown[0] if shown else name) or set()
        if declared and shown:
            declared = {each for each in declared if each[2] == shown[1:]}
        if declared and where is not None and len(declared) > 1:
            place = (Path(str(where[0])).name, where[1])
            declared = {each for each in declared if each[2] == place} or declared
        return {storage for _, storage, _ in declared}

    def _storages(self, frame, root):
        """``_declared_storages`` of the root local ``root`` of ``frame``."""
        function = self._function_at(frame) if self.fields is not None else None
        if function is None or not function.get("types"):
            return set()
        name = root.get("name")
        shown = self._shown_where(name, self._unslid(frame))
        return self._declared_storages(function["types"], name, shown)

    @staticmethod
    def _declared_type(types, name, where=None):
        """(type offset, whether every declaration is an automatic or a parameter in the
        frame's memory) of the local ``name`` (`s`, or `s @ main.rs:81` as LLDB names one
        of several) in a function's ``types`` (fields.local_types). ``where``: the
        (source, line, ...) of the declaration of it the debugger shows at the stop
        (shown_declaration), which alone counts where the function declares several
        (#158: a loop's `Task t`, and a later block's `const Task &t`)."""
        from peeky.fields import Unsettable

        shown = split_located(str(name))
        declared = types.get(shown[0] if shown else name)
        if declared and shown:
            # LLDB names a local by where it is declared when several share its name.
            declared = {each for each in declared if each[2] == shown[1:]}
        place = None
        if declared and where is not None and len(declared) > 1:
            place = (Path(str(where[0])).name, where[1])
            declared = {each for each in declared if each[2] == place} or declared
        if not declared:
            raise Unsettable(f"the debug information declares no local `{name}` in this function")
        kinds = {kind for kind, _, _ in declared}
        if None in kinds:
            raise Unsettable(f"the debug information gives the local `{name}` no type")
        if len(kinds) != 1:
            if place is not None and {each[2] for each in declared} == {place}:
                # The debug information tells those declarations apart by their line only.
                raise Unsettable(
                    f"this function declares several locals named `{name}` of different "
                    f"types on line {place[1]}, which Peeky cannot tell apart"
                )
            raise Unsettable(
                f"this function declares several locals named `{name}` of different types, "
                "and Peeky cannot tell which one the debugger shows"
            )
        framed = not {storage for _, storage, _ in declared} - {"automatic", "parameter"}
        return next(iter(declared))[0], framed

    def source_path(self, original):
        relative = (
            Path(original).resolve(strict=True).relative_to(self.prepared.source_root).as_posix()
        )
        if relative not in self.lines:
            raise ValueError("Source is not a prepared native input")
        return self.prepared.workspace / relative

    def local_scopes(self, scopes):
        return locals_only(scopes)

    @staticmethod
    def return_entry(variable):
        """ "" where LLDB lists what the function a step left returned, else None (#155)."""
        return "" if variable.get("name") == "(Return Value)" else None

    def return_withheld(self, variable):
        """Why a returned value LLDB lists is not shown, or None: only a number, a
        character, a boolean or a pointer, which LLDB reads from the return register.
        A struct may come back in registers or through memory LLDB cannot place, and a
        reference's object may already be gone (#155)."""
        kind = re.sub(
            r"^(?:(?:const|volatile)\s+)+|(?:\s+(?:const|volatile))+$",
            "",
            str(variable.get("type") or "").strip(),
        )
        if kind in RETURN_SCALARS or (kind.endswith("*") and "&" not in kind and "(" not in kind):
            return None
        if (self.typedef_kind(kind) or ("",))[0] == "pointer":
            return None
        return (
            f"{kind or 'its type'} is not a number, character, boolean or pointer: the "
            "debugger may read a struct, class or reference from the wrong place, so "
            "Peeky shows only scalar returns"
        )

    @staticmethod
    def listed_threads(dap):
        """The thread IDs LLDB itself lists at this stop (`thread list`: `thread #2: tid =
        0x3a7f3b5, …`), which lldb-dap's `threads` reply may not yet show (#181 review),
        or None where it does not say."""
        try:
            text = dap.request(
                "evaluate", {"expression": "`thread list", "context": "repl"}, timeout=2
            ).get("result", "")
        except Exception:
            return None
        if not isinstance(text, str) or len(text) > MAX_METADATA:
            return None
        found = re.findall(r"^\*?\s*thread #[0-9]+: tid = (0x[0-9a-fA-F]+|[0-9]+)\b", text, re.M)
        return frozenset(int(tid, 0) for tid in found) or None

    @staticmethod
    def returned_by_command(dap, thread_id):
        """What LLDB itself recorded the step out returned (`thread info`: "Return value:
        (bool) $0 = false"), for lldb-dap before LLVM 21, which lists no `(Return
        Value)`; only for the stopped thread, and only a one-line value. None if none."""
        try:
            text = dap.request(
                "evaluate", {"expression": "`thread info", "context": "repl"}, timeout=2
            ).get("result", "")
        except Exception:
            return None
        thread = re.search(r"\btid = (0x[0-9a-fA-F]+|[0-9]+)", text or "")
        if not thread or int(thread[1], 0) != thread_id:
            return None
        found = re.search(r"^Return value: \(([^\n]+?)\) \$[0-9]+ = ([^\n]*)$", text, re.M)
        return {"name": "(Return Value)", "type": found[1], "value": found[2]} if found else None

    def before_step(self, dap):
        """Once per session, make LLDB's step out stop where the function returns, one
        frame up (#155). By default it goes on past libc++'s internal frames (its "libc++
        frame recognizer" hides them) and past code without debug information (libc's
        qsort), and then the value it lists is not what the function returned, and a
        probe could not tell one call's return from the next call's stop."""
        if getattr(self, "_steps_set", False):
            return
        self._steps_set = True

        def command(text):
            return dap.request(
                "evaluate", {"expression": f"`{text}", "context": "repl"}, timeout=2
            ).get("result", "")

        try:
            command(
                "settings set target.process.thread.step-out-avoids-code-without-debug-info false"
            )
            listed = command("frame recognizer list")
            if found := re.search(r"^(\d+): libc\+\+ frame recognizer", listed or "", re.M):
                command(f"frame recognizer disable {found[1]}")
        except Exception:
            # Without it, a step out that goes further withholds the value (left_function).
            pass

    def watch_return(self, dap, thread_id, frames):
        """Before a step out of frames[0] (#155): watch its caller, so a call that never
        returns is not credited with a later call's value. LLDB's step out waits for the
        return address in the caller's frame; when the call ends by an exception, a
        `longjmp` or a panic that its caller survives, that wait stays armed, and the
        next call from the same place (the same frame, the same stack pointer) ends it,
        with that call's value. So the step watches every way back to the return
        address that is not this call's return: the call itself, a branch to it, and
        any computed jump in the caller, each only on this thread and only in a frame at
        or above the caller's (its stack pointer at least the caller's, which no frame
        below the call can have while the call runs). One of them stopping means the
        call is gone. When the step ends, the stack pointer must be the caller's again
        (`unwatch_return`).

        Each watched instruction also has a counter (#162): a deeper call of a recursion
        through the caller runs it too, and each run costs LLDB a stop of its own (the
        condition holds false). After WATCH_TRAPS of them, the counter's command
        disables the whole watch inside LLDB (no stop reaches Peeky), and the return is
        then withheld as not followed. Returns {thread, sp, ids, counters, addresses},
        or {inlined} / {unwatched: why} when no return can be vouched for."""
        caller, callee = (frames[1], frames[0]) if len(frames) > 1 else (None, None)
        ret = _address(caller.get("instructionPointerReference")) if caller else None
        if ret is None:
            return {"unwatched": "the debugger did not give the address its call returns to"}

        def command(text):
            reply = dap.request(
                "evaluate", {"expression": f"`{text}", "context": "repl"}, timeout=2
            )
            return str(reply.get("result") or "")

        ids = []

        def made(text):
            found = re.search(r"^Breakpoint (\d+):", reply := command(text), re.M)
            if not found:
                raise RuntimeError(reply.strip()[:200] or "no breakpoint made")
            ids.append(int(found[1]))
            return ids[-1]

        try:
            listed = self.caller_instructions(command, ret)
            if isinstance(listed, str):
                return {"unwatched": listed}
            paths = _return_paths(listed, ret)
            if paths is None:
                if _address(callee.get("instructionPointerReference")) == ret:
                    # Its caller is the same machine frame at the same instruction.
                    return {"inlined": True}
                return {"unwatched": "no call instruction comes before where it returns"}
            sp = _stack_pointer(dap, caller.get("id"))
            if sp is None:
                return {"unwatched": "the debugger did not give its caller's stack pointer"}
            thread = int(thread_id)
            for address in paths:
                made(f"breakpoint set -a {address:#x} -c '$sp >= {sp:#x}' -t {thread}")
            counters = [
                made(f"breakpoint set -a {address:#x} -t {thread} -i {WATCH_TRAPS} -G true")
                for address in paths
            ]
            every = " ".join(map(str, ids))
            reply = command(
                f"breakpoint command add -o 'breakpoint disable {every}' "
                + " ".join(map(str, counters))
            )
            if "error" in reply.lower():
                raise RuntimeError(reply.strip()[:200])
        except Exception as exc:
            self.unwatch_return(dap, {"ids": ids})
            return {"unwatched": f"its caller could not be watched ({str(exc)[:200]})"}
        return {
            "thread": thread_id,
            "sp": sp,
            "ids": ids,
            "counters": counters,
            "addresses": paths,
        }

    def caller_instructions(self, command, ret):
        """The `_instructions` of the function holding address `ret`, read once per
        function (a function's call sites share them), or why they could not be read.
        LLDB refuses a very large function unless forced."""
        cache = self.__dict__.setdefault("_functions", {"read": [], "failed": {}})
        for start, end, listed in cache["read"]:
            if start <= ret <= end:
                return listed
        if ret in cache["failed"]:
            return cache["failed"][ret]
        text = command(f"disassemble --force -a {ret:#x}")
        listed = _instructions(text)
        if any(address == ret for address, _, _ in listed):
            cache["read"].append((listed[0][0], listed[-1][0], listed))
            return listed
        said = next((line.strip() for line in split_lines(text) if "error" in line.lower()), "")
        failure = (
            f"LLDB did not disassemble its caller ({said[:200]})"
            if said
            else "LLDB's disassembly of its caller does not list where the call returns"
        )
        cache["failed"][ret] = failure
        return failure

    def unwatch_return(self, dap, watch, thread_id=None, caller=False):
        """End a step's watch (`watch_return`); with the thread that stopped (the watched
        one), where it stopped: {pc, sp} of its innermost frame (with ``caller``, its
        caller's stack pointer too, `caller_sp`), and whether the watch was dropped on
        the way (its counters ran out), else None."""
        watched = thread_id is not None and thread_id == watch.get("thread")
        dropped = False
        if watched and (counters := watch.get("counters")):
            try:
                listed = dap.request(
                    "evaluate",
                    {
                        "expression": "`breakpoint list " + " ".join(map(str, counters)),
                        "context": "repl",
                    },
                    timeout=2,
                ).get("result")
                # Each counter's one location: `3.1: where = …, hit count = 101`.
                counts = dict(
                    re.findall(r"^\s*(\d+)\.\d+:[^\n]*?hit count = (\d+)", str(listed or ""), re.M)
                )
                # Unreadable counts are not taken for a watch kept.
                dropped = any(
                    int(counts.get(str(n), WATCH_TRAPS + 1)) > WATCH_TRAPS for n in counters
                )
            except Exception:
                dropped = True
        if ids := watch.get("ids"):
            try:
                dap.request(
                    "evaluate",
                    {
                        "expression": "`breakpoint delete -f " + " ".join(map(str, ids)),
                        "context": "repl",
                    },
                    timeout=2,
                )
            except Exception:
                # The process is gone (the program ended), and its breakpoints with it.
                pass
        if not watched:
            return None
        try:
            stack = dap.request(
                "stackTrace", {"threadId": thread_id, "levels": 2 if caller else 1}, timeout=2
            )
            listed = stack.get("stackFrames") or [{}]
            frame = listed[0]
            sp = _stack_pointer(dap, frame["id"])
        except Exception:
            return None
        caller_sp = None
        if caller and len(listed) > 1:
            try:
                caller_sp = _stack_pointer(dap, listed[1]["id"])
            except Exception:
                # Best effort: without it, the stop is not proven in the call (#174).
                caller_sp = None
        pc = _address(frame.get("instructionPointerReference"))
        if None in (pc, sp):
            return None
        return {
            "pc": pc,
            "sp": sp,
            **({"caller_sp": caller_sp} if caller_sp is not None else {}),
            **({"dropped": WATCH_TRAPS} if dropped else {}),
        }

    @staticmethod
    def discard_steps(dap, thread_id):
        """Drop what is left of a step a stop interrupted on a thread (a breakpoint hit
        during `step --out`): LLDB keeps the step and would end it at some later
        `continue`, as a step stop no one asked for. After a step that ended, nothing is
        left. Returns whether something was. Best effort."""

        def command(text):
            return str(
                dap.request(
                    "evaluate", {"expression": f"`{text}", "context": "repl"}, timeout=2
                ).get("result")
                or ""
            )

        discarded = False
        try:
            threads = command("thread list")
            index = {
                int(tid, 0): number
                for number, tid in re.findall(
                    r"thread #(\d+): tid = (0x[0-9a-fA-F]+|[0-9]+)", threads
                )
            }
            selected = re.search(r"^\*\s*thread #(\d+)", threads, re.M)
            if int(thread_id) not in index:
                return False
            ours = index[int(thread_id)]
            if selected is None or selected[1] != ours:
                command(f"thread select {ours}")
            # Index 0 is the thread's base plan; 1 and above are what steps left.
            # Nothing there: "error: Could not find User thread plan with index 1."
            discarded = "error" not in command("thread plan discard 1").lower()
            if selected is not None and selected[1] != ours:
                command(f"thread select {selected[1]}")
        except Exception:
            pass
        return discarded

    def normalize_capabilities(self, raw):
        return {
            "supports_set_variable": raw.get("supportsSetVariable") is True,
            # lldb-dap changes a field or element as it changes a local (#105).
            "supports_member_set": raw.get("supportsSetVariable") is True,
            # Used only inside the worker, by qualified value views (Rust containers).
            "supports_read_memory": raw.get("supportsReadMemoryRequest") is True,
            # Used only by text changes (#137: a string's heap buffer, see text_sets).
            "supports_write_memory": raw.get("supportsWriteMemoryRequest") is True,
            "supports_conditional_breakpoints": False,
            "supports_hit_conditional_breakpoints": False,
            "supports_logpoints": False,
            "supports_evaluate": False,
            "supports_attach": False,
        }

    def validate_breakpoints(self, points):
        self.target.validate_breakpoints(points)

    # A `--set` value in double quotes is text, written into a string (text_sets, #137).
    text_language = "c"

    def validate_value(self, value):
        if not isinstance(value, str) or not re.fullmatch(r"[+-]?(?:0|[1-9][0-9]{0,19})", value):
            raise ValueError(
                "Native mutations require bounded decimal integer literals, or text in double "
                "quotes for a string (--set 'name=\"apple\"')"
            )

    # A value lldb-dap gives no memoryReference has no address at all, so a write could
    # only change LLDB's copy. This catches nothing in practice (lldb-dap 20 and 21 give
    # even bitfield bindings one, set to their value): confirm_mutation's read-back is
    # what guarantees every reported change, on every adapter.
    requires_memory_location = True

    def never_settable_row(self, variable, frame, member=False, steps=None):
        """Why no stop changes a field or element (``member``) of the root local
        ``variable`` (#138: `set` refuses a member of a root outside the frame's memory,
        as the check before the run does): `register` where the debug information gives
        it no stack slot in the frame's function (a register or a location list), `static`
        where it is a static, `reference` where the frame reaches it through a pointer or
        reference (a structured binding of a reference); else None. A root itself is left
        to the stop (the debugger writes a register), as is an optimized build, where
        every change is refused."""
        target = getattr(self, "target", None)
        if not member or self.fields is None or getattr(target, "optimization", "O0") != "O0":
            return None
        function = self._function_at(frame)
        if function is None or not function.get("types"):
            return None
        shown = split_located(str(variable.get("name")))
        declared = function["types"].get(shown[0] if shown else variable.get("name"), ())
        if shown:
            declared = [each for each in declared if each[2] == shown[1:]]
        storages = {storage for _, storage, _ in declared}
        if "through" in storages and not any(
            self._is_array(kind) for kind, storage, _ in declared if storage == "through"
        ):
            # Reached through a pointer or reference: a structured binding of one (GCC's
            # variable-length array is too, whose elements `set` refuses as such).
            return "reference"
        if storages and storages <= {"register", "other"}:
            return "register"
        return "static" if storages == {"static"} else None

    def _is_array(self, kind):
        """Whether the type at offset ``kind`` is an array, under typedefs and qualifiers."""
        from peeky.native_previews import Typed

        node = Typed(self.fields or {}, kind).node()
        return node is not None and node[0] == "array"

    def settable(self, variable, frame=None):
        """Whether a root local is a writable integer of a type Peeky can change."""
        return _writable_integer(variable)

    def mutation_literal(self, value, variable=None):
        """The canonical decimal sent to LLDB (`+5` and unsigned `-0` are not its syntax)."""
        return str(int(value))

    def confirm_mutation(self, name, value, before, after, variable=None):
        """The value read back must be exactly the one requested. Nothing written: the
        change is refused and the session kept. Anything else (a lossy conversion, a
        redefined type) changed the program in a way nobody asked for: fail closed."""
        wanted = str(int(value))
        if after == wanted:
            return
        if after == before:
            raise ValueError(
                f"The debugger did not change {name!r} (still {before}): its value is "
                "computed rather than stored, so a change cannot reach the program; edit "
                "the source and run verify"
            )
        raise RuntimeError(
            f"The debugger stored {after} in {name!r}, not {wanted}: the change did not "
            "reach the program as stated, so the session is closed"
        )

    def validate_variable(self, variable, value):
        self.validate_value(value)
        kind = variable.get("type")
        try:
            text_kind(variable, getattr(self, "text_language", "c"))
        except ValueError:
            pass
        else:
            raise ValueError(
                f"Native mutation requires a text in double quotes for {variable.get('name')!r}, "
                f"a {kind} (--set '{variable.get('name')}=\"apple\"')"
            )
        if not _writable_integer(variable):
            raise ValueError(
                f"Only writable integer locals of these types support native mutation: "
                f"short, int, long, long long (signed or unsigned), int16_t to uint64_t, "
                f"size_t, ssize_t, ptrdiff_t, intptr_t, uintptr_t; "
                f"{variable.get('name')!r} has debugger type {kind!r}"
            )
        low, high = INTEGER_RANGES[kind]
        if not low <= int(value) <= high:
            raise ValueError(
                f"Native integer mutation is outside the range of {kind} ({low} to {high})"
            )

    def mutation_value(self, response):
        # LLVM18 used result; current DAP adapters use value.
        value = response.get("value", response.get("result"))
        if not isinstance(value, str):
            raise RuntimeError("Native mutation response has no observed value")
        return value

    def assert_current(self):
        assert_current(self.prepared)
        for path, expected in getattr(self, "external_inputs", {}).items():
            try:
                # One stat per unchanged file (peeky.digest); any change rehashes.
                current = sha256_file(path)
            except (OSError, ValueError):
                current = None
            if current != expected:
                raise RuntimeError(
                    f"Prepared build provenance mismatch: {path}, which the compiler read "
                    "from outside the workspace, changed; start a new session"
                )

    def validate_stop(self, frame):
        try:
            path = Path(frame["source"]["path"]).resolve(strict=True)
            relative = path.relative_to(self.prepared.workspace).as_posix()
            line = frame["line"]
            valid = type(line) is int and line in self.lines.get(relative, [])
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Native stopped source does not match prepared symbols") from exc
        if not valid:
            raise RuntimeError("Native stopped line does not match prepared symbols")

    def validate_debugger(self, dap, frame):
        modules = dap.request("modules", {"startModule": 0, "moduleCount": 256}).get("modules", [])
        if "moduleId" in frame:
            matches = [module for module in modules if module.get("id") == frame["moduleId"]]
        elif sys.platform == "linux":
            # LLVM18 omits frame.moduleId. Bind its PC to the hashed executable's
            # loaded PF_X segments, never infer module identity from source alone.
            matches = [
                module
                for module in modules
                if isinstance(module.get("id"), str)
                and module["id"].replace("-", "").upper() == self.module_identity
            ]
        else:
            matches = []
        if len(matches) != 1:
            raise RuntimeError("Native stopped module is absent or ambiguous")
        module = matches[0]
        try:
            valid = (
                Path(module["path"]).resolve(strict=True) == self.binary
                and isinstance(module["id"], str)
                and module["id"].replace("-", "").upper() == self.module_identity
                and module.get("symbolStatus") == "Symbols loaded."
            )
            valid = valid and Path(module["symbolFilePath"]).resolve(strict=True) == self.symbols
            if "moduleId" not in frame:
                valid = valid and _elf_frame_matches(self.binary, module, frame)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Native module identity is not verified") from exc
        if not valid:
            raise RuntimeError("Native module identity or symbols do not match prepared build")
        try:
            # Most rules that read it are display aids (a local declared on the stop line):
            # unknown never withholds there. A GCC prologue's does (review of #279).
            self.load_bias, self.load_bias_unknown = load_bias(self.binary, module), False
        except (OSError, ValueError, struct.error):
            self.load_bias, self.load_bias_unknown = None, True
        # scripts/probe_rust.py validates with a plain namespace, which records nothing.
        if check := getattr(self, "_check_loop_target", None):
            check(dap, frame)

    def _check_loop_target(self, dap, frame):
        """Record whether the stop's address, when it starts a declaration line, is one a
        loop jumps back to (True), not (False), or unknown (None). Each function is
        disassembled at most once per session."""
        cache = self.__dict__.setdefault("loop_targets", {})
        jumps = self.__dict__.setdefault("backward_jumps", {})
        pointer, bias = frame.get("instructionPointerReference"), self.load_bias
        declarations = getattr(self, "declarations", None)
        if (
            bias is None
            or not declarations
            or not isinstance(pointer, str)
            or not re.fullmatch(r"0x[0-9a-fA-F]{1,16}", pointer)
        ):
            return
        address = int(pointer, 16) - bias
        if address in cache:
            return
        function = next(
            (
                function
                for function in declarations
                if any(address in starts for starts in function.get("starts", {}).values())
            ),
            None,
        )
        if function is None:
            return
        span = next(
            ((low, high) for low, high in function["ranges"] if low <= address < high), None
        )
        if span is not None and span not in jumps:
            jumps[span] = self._backward_jumps(dap, span, bias)
        targets = jumps.get(span)
        cache[address] = None if targets is None else address in targets

    def _backward_jumps(self, dap, span, bias):
        """The unslid addresses in ``span`` that a jump there goes back to, or None."""
        low, high = span
        try:
            kind = architecture(self.binary)
        except (OSError, struct.error):
            return None
        # Enough instructions to pass the function's end: arm64's are 4 bytes each.
        count = (high - low) // 4 + 8 if kind == "arm64" else high - low + 16
        if kind is None or count > 8192:
            return None
        try:
            reply = dap.request(
                "disassemble",
                {
                    "memoryReference": f"0x{low + bias:X}",
                    "instructionOffset": 0,
                    "instructionCount": count,
                    "resolveSymbols": False,
                },
                timeout=3,
            )
            instructions = reply.get("instructions") or []
            if not any(int(item["address"], 16) >= high + bias for item in instructions):
                return None
            targets = backward_targets(instructions, low + bias, high + bias, kind)
        except (RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
            return None
        return None if targets is None else {target - bias for target in targets}

    def close(self):
        self.prepared.cleanup()
