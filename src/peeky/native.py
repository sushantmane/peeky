"""Fixed C and C++/LLDB targets with fresh builds and bounded native symbol evidence.

Supported formats are single-architecture Mach-O (UUID) and ELF64 little-endian
(GNU build ID). File/module identity is trusted-code integrity, not protection
against hostile in-memory patching. SDK/system headers are not hermetic inputs.
"""

import bisect
import codecs
import contextlib
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from peeky.adapters import PythonAdapter, locals_only
from peeky.build import (
    ArtifactSpec,
    BuildCommandError,
    BuildRecipe,
    Command,
    _digest,
    assert_current,
    default_sdk,
    dependency_paths,
    prepare,
    real_tool,
)
from peeky.c_source import Statements, macro_hazards
from peeky.cxx_strings import libstdcxx_string, string_class, text_like, tidy
from peeky.digest import sha256_file
from peeky.names import unicode_escape

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


def _metadata(tool, arguments, workspace, env, *, max_bytes=MAX_METADATA, timeout=15):
    """Bound metadata capture; larger engineering probes do not change parser limits."""
    if type(max_bytes) is not int or not 0 < max_bytes <= 16 * 1024 * 1024:
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
        return output.read_text(encoding="utf-8")


def _line_rows(text, workspace, declared, limit=MAX_METADATA, replaced=None):
    """Rows of the LLVM DWARF4 line-table dump for declared sources: (address, name, line).

    Unrecognized rows fail closed. Rows outside the declared sources (SDK and system
    headers) and end-of-sequence rows are skipped. Of the rows a sequence gives one
    address (GCC -O2), LLDB keeps the last: each earlier one of another line or file
    runs no code. It is skipped if it is not a statement; a statement row is kept,
    since a breakpoint can be set on its line, and added to ``replaced`` when given.
    """
    if len(text.encode()) > limit:
        raise ValueError("Native line metadata exceeds limit")
    workspace = Path(workspace).resolve()
    inputs = {str((workspace / name).resolve()): name for name in declared}
    counts = {name: len((workspace / name).read_text().splitlines()) for name in declared}
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

    for raw in text.splitlines():
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
            directories[key] = json.loads(match[2])
        elif match := re.fullmatch(r"file_names\[\s*(\d+)\]:", line):
            current = int(match[1])
            if current in files:
                raise ValueError("Duplicate line file identity")
            files[current] = {}
        elif match := re.fullmatch(r"name:\s*(\".*\")", line):
            if current is None:
                raise ValueError("Missing line file identity")
            files[current]["name"] = json.loads(match[1])
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


def parse_lines(text, workspace, declared, *, limit=MAX_METADATA):
    """Parse the LLVM DWARF4 line-table dump; unrecognized rows fail closed."""
    result = {name: set() for name in declared}
    for _, name, number in _line_rows(text, workspace, declared, limit):
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
    for raw in text.splitlines():
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
    """A dwarfdump string attribute; non-ASCII bytes are printed as octal escapes."""
    if value is None or len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return None
    try:
        return codecs.escape_decode(value[1:-1].encode())[0].decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


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


def _locals(dies, offset):
    """{name: {types}} for the parameters and variables of one function body, including
    nested blocks but not the bodies of calls inlined into it. A type is the name
    dwarfdump gives the DIE's DW_AT_type (`long long`, `Stats *`)."""
    names, pending = {}, list(dies[offset]["children"])
    while pending and len(names) < 512:
        child = pending.pop()
        die = dies[child]
        if die["tag"] in ("DW_TAG_formal_parameter", "DW_TAG_variable"):
            chain = _chain(dies, child)
            name = _text(_first(dies, chain, "DW_AT_name"))
            # `$`-prefixed names are debugger-synthesized storage, not program locals.
            if name and not name.startswith("$"):
                kind = _type_name(dies, _first(dies, chain, "DW_AT_type"))
                names.setdefault(name, set()).update({kind} if kind else set())
        elif die["tag"] == "DW_TAG_lexical_block":
            pending.extend(die["children"])
    return names


_MAX_SPANS = 8


def _block_spans(dies, entry, root, addresses, numbers, source_of):
    """({name: {(first, last)}}, {names visible in the whole function}), or None when
    the declarations were too many to read all: the lines of the blocks declaring each
    local, for the locals declared only inside nested blocks (a `for` loop's `i`), so
    visible only there (#107). A block's lines are those of the function's own rows
    within its code (a local macro's definition lines, where its expansions run, are not
    counted), and the call lines of what is inlined into it (LLDB stops there, at the
    callee's entry, in this frame); one that also runs another file's code (an
    `#include` inside it) gives no span. ``source_of`` names the declared source of a
    DW_AT_call_file."""
    rows = sorted(
        (address, source, line) for (source, line), found in addresses.items() for address in found
    )
    keys = [row[0] for row in rows]
    lines, spans, everywhere = {}, {}, set()
    lowest, highest = min(numbers), max(numbers)

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
            lines[block] = (min(covered), max(covered)) if covered and not other else None
        return lines[block]

    def name_of(child):
        die = dies[child]
        if die["tag"] not in ("DW_TAG_formal_parameter", "DW_TAG_variable"):
            return None
        name = _text(_first(dies, _chain(dies, child), "DW_AT_name"))
        return None if not name or name.startswith("$") else name

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
                spans.setdefault(name, set()).add(found)
    if root not in entry["bodies"]:
        everywhere.update(name for child in dies[root]["children"] if (name := name_of(child)))
    return spans, everywhere


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


def _function_entries(info, line_text, workspace, declared, replaced=None):
    """(dies, {root: entry}, {root: {line: span}}, files, {root: {(source, line): span}})
    for functions in declared sources; see function_scopes. ``replaced``: see
    _line_rows. A span is the lowest and
    highest address of a function's rows on a line. The first mapping keeps rows of the
    function's own file (describe); the last keeps every row the function runs, in any
    declared file (clang runs a header struct's member initializers in its caller)."""
    import bisect

    workspace = Path(workspace).resolve()
    inputs = {str((workspace / name).resolve()): name for name in declared}
    rows = sorted(_line_rows(line_text, workspace, declared, replaced=replaced))
    addresses = [row[0] for row in rows]
    dies = _dies(info)
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
        source = _source_of(_first(dies, chain, "DW_AT_decl_file"), inputs, workspace, sources)
        if source is None:
            # SDK and std code: never listed, and never claims the caller's rows.
            continue
        root = chain[-1]
        entry = functions.setdefault(root, {"bodies": [], "chain": chain, "source": source})
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


def _own_lines(dies, entry, numbers, macro_lines=frozenset()):
    """A function's line numbers from its declaration on, or None if it has none. Lines
    of a local `macro_rules!` definition (``macro_lines``), where its expansions run,
    are not its own unless it is declared there."""
    if not numbers:
        return None
    first_line = _first(dies, entry["chain"], "DW_AT_decl_line")
    first = int(first_line) if first_line and first_line.isdigit() else None
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


def function_scopes(info, line_text, workspace, declared):
    """Each function in a declared source: qualified name, source, line span, locals.

    Display only (`describe`). A line-table row belongs to the innermost function or
    inlined call whose address range covers it, so an inlined callee's lines count for
    the callee, not its caller. Copies of one function (C1/C2 constructors, inlined and
    out-of-line copies) are merged through the DIE that declares them.
    """
    replaced = set()
    dies, functions, lines, (inputs, sources), _, addresses = _function_entries(
        info, line_text, workspace, declared, replaced
    )
    workspace = Path(workspace).resolve()

    def source_of(attribute):
        return _source_of(attribute, inputs, workspace, sources)

    found, macros = {}, {}
    for root, entry in functions.items():
        source = entry["source"]
        name = _qualified(dies, entry["bodies"][0])
        if source not in macros:
            macros[source] = (
                _macro_rules_lines((Path(workspace) / source).read_text(errors="replace"))
                if source.endswith(".rs")
                else set()
            )
        numbers = _own_lines(dies, entry, lines.get(root), macros[source] or frozenset())
        if name is None or not numbers:
            continue
        names = {}
        for body in (*entry["bodies"], root):
            for local, kinds in _locals(dies, body).items():
                names.setdefault(local, set()).update(kinds)
        scope = {
            "scope": name,
            "source": source,
            "lines": [min(numbers), max(numbers)],
            "names": sorted(names),
        }
        # Instantiations of one generic function (Rust's names drop the generic
        # arguments) are one scope; their types join (`&[i32] or &[char]`), and so do
        # their blocks' lines (code a constant condition removes differs between them).
        merged = found.setdefault(json.dumps(scope, sort_keys=True), (scope, {}, {}, set()))
        for local, kinds in names.items():
            merged[1].setdefault(local, set()).update(kinds)
        # Rows a later row at their address replaces run no code.
        ran = {
            key: [address for address in found if (address, *key) not in replaced]
            for key, found in addresses.get(root, {}).items()
        }
        declared_at = _first(dies, entry["chain"], "DW_AT_decl_line")
        # A function a macro declares runs its arguments' code on the invoking line,
        # past other functions: its blocks' lines are not one span.
        in_macro = (
            declared_at and declared_at.isdigit() and int(declared_at) in (macros[source] or ())
        )
        blocks = (
            _block_spans(dies, entry, root, ran, numbers, source_of)
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
    for scope, kinds, spans, everywhere in found.values():
        # Two blocks may declare one name with different types (`int or long`).
        types = {local: " or ".join(sorted(k)) for local, k in sorted(kinds.items()) if k}
        spans = {
            local: _tidy_spans(found_spans)
            for local, found_spans in sorted(spans.items())
            if local not in everywhere and None not in everywhere
        }
        scopes.append(
            {**scope, **({"types": types} if types else {}), **({"spans": spans} if spans else {})}
        )
    scopes.sort(key=lambda s: (s["source"], s["lines"][0], s["scope"]))
    if len(scopes) > MAX_SCOPES:
        scopes = [*scopes[:MAX_SCOPES], {"omitted": len(scopes) - MAX_SCOPES}]
    return scopes


# A local in the frame: DW_OP_fbreg (frame base) or DW_OP_bregN (a register) offsets.
_AUTOMATIC = re.compile(r"DW_OP_(?:fbreg|breg[0-9]+)\b")


def _declared(dies, offset, files, workspace):
    """(name, (source, line, automatic), scope) for each parameter and variable of one
    function body, including nested blocks but not calls inlined into it; ``scope`` is
    the DIE of the block (or body) declaring it. Parameters, statics and anything
    without a frame location are not automatic. A label is listed as (None, None,
    None): `goto` can return above a declaration with the local still live."""
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
        if die["tag"] == "DW_TAG_formal_parameter":
            found.append((name, (None, None, False), die["parent"]))
            continue
        inputs, cache = files
        source = _source_of(_first(dies, chain, "DW_AT_decl_file"), inputs, workspace, cache)
        line = _first(dies, chain, "DW_AT_decl_line")
        location = die["attrs"].get("DW_AT_location") or ""
        automatic = bool(_AUTOMATIC.search(location)) and "DW_OP_addr" not in location
        where = (source, int(line) if line and line.isdigit() else None, automatic)
        found.append((name, where, die["parent"]))
    return found


def local_declarations(info, line_text, workspace, declared):
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
    ``types``: fields.local_types, the type and storage of each local (#105)."""
    from peeky.fields import local_types

    dies, functions, _, files, runs, addresses = _function_entries(
        info, line_text, workspace, declared
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
        for body in (*entry["bodies"], root):
            for name, where, scope in _declared(dies, body, files, workspace):
                if name is None:
                    jumps = True
                    continue
                names.setdefault(name, set()).add(where)
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
        constructor, enclosing, defined, initializer = _class_role(dies, entry["chain"])
        first_line = _first(dies, entry["chain"], "DW_AT_decl_line")
        first = int(first_line) if first_line and first_line.isdigit() else None
        own = [
            n for (where, n), _ in rows.items() if where == entry["source"] and n >= (first or 0)
        ]
        table.append(
            {
                "source": entry["source"],
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
                "types": local_types(dies, entry["bodies"]),
            }
        )
    return table


_CLASSES = ("DW_TAG_class_type", "DW_TAG_structure_type", "DW_TAG_union_type")


def _local_classes(dies):
    """{function root: ((line, closure), ...)}: the classes, structs and unions defined
    inside each function (not just declared), by the line the debug information gives,
    and whether it is a lambda's closure (no plain name, an `operator()`): its body is
    a function of its own, not text the function runs."""
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
        # Clang leaves a closure unnamed; GCC names it `<lambda(...)>`.
        named = _text(die["attrs"].get("DW_AT_name")) or ""
        closure = not re.fullmatch(r"[A-Za-z_]\w*", named) and any(
            _text(dies[child]["attrs"].get("DW_AT_name")) == "operator()"
            for child in die["children"]
        )
        found.setdefault(owner, []).append(
            (int(line) if line and line.isdigit() else None, closure)
        )
    return {owner: tuple(sorted(set(places), key=str)) for owner, places in found.items()}


def _class_role(dies, chain):
    """(constructor, enclosing, defined, initializer) for a function's DIE chain:
    whether it is a constructor of its class (`C::C`, its name the class's without
    template arguments; of an unnamed class, an artificial member, or one named as the
    class, with no return type: `$_0::$_0()`); for a member of a class defined inside
    a function (a lambda's `operator()`, a local class's method), that function's root
    DIE and the class's line, else None; and whether it is a lambda's `operator()`
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
            # Clang names an unnamed struct's constructor `(unnamed struct at f.cpp:55:5)`.
            artificial = any("DW_AT_artificial" in dies[at]["attrs"] for at in chain)
            constructor = (artificial or name == owner) and not (name or "").startswith("~")
        closure = unnamed and name == "operator()"
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


_SHADOWED = re.compile(r"(.+) @ [^:]+:([0-9]+)")


def _is_object(variable):
    """Whether a DAP variable is a class, struct, union or array: it has children and is
    no pointer or reference."""
    kind = re.sub(r"(?:\s*\b(?:const|volatile)\b)+$", "", str(variable.get("type") or "").strip())
    reference = variable.get("variablesReference")
    return type(reference) is int and reference > 0 and not kind.endswith(("*", "&"))


# A C++ constructor's frame name: `Widget::Widget(long)`, `ns::Box<int>::Box(int)`.
# An unnamed class is `$_0` (Clang), `(anonymous struct)`, `(unnamed struct at f:5:3)`
# (LLDB) or `{unnamed type#1}` (GCC).
_CONSTRUCTOR_NAME = re.compile(
    r"(?:^|::|\s)([A-Za-z_]\w*|\$_\d+|\((?:anonymous|unnamed) (?:struct|class|union)[^()]*\)"
    r"|\{unnamed type#\d+\})(?:<[^()]*>)?::\1(?:<[^()]*>)?\("
)
# A source whose statements are scanned (c_source) to place a caller's declaration.
MAX_STATEMENT_SOURCE = 8_000_000


def declared_below(table, name, source, line, through=None):
    """The line a C/C++ local shown at ``source:line`` is declared on, when control
    cannot have reached that declaration on this pass, else None.

    Only when every local of that name in every function owning code of the line is
    an automatic variable declared in this source below it, and all of that
    function's code of this line comes before any code of the declaration's line:
    its value is then indeterminate here (C11 6.2.4). Line numbers alone do not say
    that: clang runs a struct's default member initializer in its caller on the
    struct's line, and GCC runs a `cleanup` at its variable's line when the block
    ends. Never in a function with a label (`goto` can return above a declaration
    with the local live). lldb-dap names a shadowed local `x @ file:line`, and that
    line picks among them.

    A declaration line with no code of its own (an aggregate stored element by element
    on its elements' lines) counts by the code of its statement's lines, to where
    ``through(source, line, name)`` says it ends; with that unknown, it counts as below.
    """
    shown = _SHADOWED.fullmatch(name)
    here = (source, line)
    # Every function with code on the line, whatever file it is defined in: the one
    # stopped in is among them.
    owners = [function for function in table if here in function["rows"]]
    if any(function["jumps"] for function in owners):
        return None
    candidates = [
        (function, where)
        for function in owners
        for where in function["locals"].get(shown[1] if shown else name, ())
    ]
    if shown:
        candidates = [pair for pair in candidates if pair[1][1] == int(shown[2])]
    if not candidates:
        return None
    for function, (declared_in, at, automatic) in candidates:
        if not automatic or declared_in != source or type(at) is not int or at <= line:
            return None
        rows = function["rows"]
        if (source, at) in rows:
            first = rows[source, at][0]
        elif through is None:
            return None
        else:
            end = through(source, at, shown[1] if shown else name)
            starts = [
                rows[source, number][0]
                for number in range(at, (end or at) + 1)
                if end is not None and (source, number) in rows
            ]
            first = min(starts) if starts else None
        if first is not None and not rows[here][1] < first:
            return None
    return min(where[1] for _, where in candidates)


def declared_unfinished(table, name, source, line, address, ends=None):
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
    it before the call."""
    shown = _SHADOWED.fullmatch(name)
    base = shown[1] if shown else name
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
            if shown and at != int(shown[2]):
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
    shown = _SHADOWED.fullmatch(name)
    base = shown[1] if shown else name
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
            if not automatic or type(at) is not int or (shown and at != int(shown[2])):
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
    call (``settled(source, line, name)`` True: `for (int i = 0; ...)`), which is live
    mid-line on later passes of a one-line loop."""
    base, found = _in_scope(table, name, source, line, address)
    places = [at for declared_in, at, _ in found if declared_in == source and at == line]
    if address is None or not places or settled(source, line, base) is True:
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


def _line_start(function, source, line, address):
    """Whether ``address`` (unslid) is where control enters ``source:line`` in the
    function (a declarations entry): the line's lowest address, as a line breakpoint
    and a step stop there; or, on its first line, after the prologue (only that line's
    code runs before it)."""
    span = function.get("rows", {}).get((source, line))
    if span is None:
        return False
    if address == span[0]:
        return True
    order = function.get("order", ())
    if not any(row == (address, source, line) for row in order):
        return False
    earlier = [row for row in order if row[0] < address]
    return bool(earlier) and all(row[1:] == (source, line) for row in earlier)


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
    """The stop line, when the C/C++ local shown there is declared on it and the stop is
    where control enters that line in the block declaring it (``address``, unslid; see
    local_declarations ``starts``): the declaration has not run on this pass, so the
    local holds leftovers. Else None.

    Only there: a one-line loop, or a return from a call (`step --out`), stops
    mid-line, where the local is live. Only when every local of that name in the
    function is an automatic variable declared on this line, and never in a function
    with a label. The caller also rules out an address a loop branches back to.
    """
    shown = _SHADOWED.fullmatch(name)
    here = (source, line)
    owners = [
        function
        for function in table
        if here in function["rows"]
        and function["rows"][here][0] <= address <= function["rows"][here][1]
    ]
    if len(owners) != 1 or owners[0]["jumps"]:
        return None
    base = shown[1] if shown else name
    places = owners[0]["locals"].get(base, ())
    if shown:
        places = {where for where in places if where[1] == int(shown[2])}
    if (
        places
        and all(
            automatic and declared_in == source and at == line
            for declared_in, at, automatic in places
        )
        and address in owners[0].get("starts", {}).get((base, source, line), ())
    ):
        return line
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
        compiler, adapter, dwarfdump = map(_tool, (self.compiler, self.adapter, self.dwarfdump))
        # The compiler path is resolved (clang++ is a symlink to clang), and clang picks
        # its language from the name it runs as; keep C++ mode so libc++ is linked.
        driver = ("--driver-mode=g++",) if self.language == "cpp" else ()
        flags = [*driver, *self.compiler_options]
        # Without an explicit sysroot, use the SDK the /usr/bin xcrun stub would have
        # supplied; it is part of the recorded build commands either way.
        sysroot = self.sysroot if self.sysroot is not None else default_sdk()
        if sysroot is not None:
            sdk = Path(sysroot).resolve(strict=True)
            if not sdk.is_dir():
                raise ValueError("Native sysroot must be a directory")
            flags.extend(("-isysroot", str(sdk)))
        # Keep objects until dsymutil finishes; single-step clang removes its
        # temporary objects and a subsequent dsymutil can erase valid symbols.
        objects = tuple(f"peeky-object-{index}.o" for index in range(len(self.sources)))
        # -MD lists every file each compile read: undeclared or outside files must not
        # escape provenance (`#include "/elsewhere/x.h"`, SDK and system headers).
        commands = [
            Command((compiler, *flags, "-MD", "-MF", obj + ".d", "-c", source, "-o", obj))
            for source, obj in zip(self.sources, objects)
        ]
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
            typedefs, fields = {}, None
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
                    declarations = (
                        None
                        if has_nonlocal_jumps(binary)
                        or mentions_nonlocal_jumps(
                            prepared.workspace / name for name in self.sources + self.headers
                        )
                        # A header outside the workspace can hide the builtin in a macro;
                        # the SDK's own setjmp.h names `setjmp`, so only the builtin counts.
                        or mentions_nonlocal_jumps(external, (b"__builtin_setjmp",))
                        else local_declarations(
                            info, text, prepared.workspace, self.sources + self.headers
                        )
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
            except (RuntimeError, ValueError, OSError):
                # Display aid only: without it, values show as the debugger reads them
                # (and no field or element can be changed).
                declarations, fields = None, None
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
            )
            session.assert_current()
            return session
        except BaseException:
            prepared.cleanup()
            raise


MAX_EXTERNAL_INPUTS = 4096


def external_inputs(depfiles, workspace, declared):
    """{path: sha256} of files the compiler read outside the workspace.

    Files inside the workspace must be declared inputs (they are already hashed);
    files outside it (SDK and system headers, absolute includes) are hashed here and
    rechecked before every operation like the declared ones.
    """
    root = Path(workspace).resolve()
    found = {}
    for depfile in depfiles:
        for dependency in dependency_paths(depfile.read_text(), root):
            path = dependency.resolve(strict=True)
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
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


def _writable_integer(variable):
    attributes = variable.get("presentationHint", {}).get("attributes", [])
    return (
        variable.get("type") in INTEGER_RANGES
        and variable.get("variablesReference", 0) == 0
        and "readOnly" not in attributes
        and bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(variable.get("name", ""))))
    )


class NativeSession:
    # lldb-dap reports optimized-out/not-live locals as `<error: ...>` text.
    reports_unavailable_as_text = True

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
    ):
        self.target, self.prepared = target, prepared
        # local_declarations of the build; None (unknown) never withholds a value.
        self.declarations = declarations
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
            "program": str(self.binary),
            "cwd": str(self.prepared.workspace),
            # Per-run arguments (--arg) replace the configured ones.
            "args": list(self.target.arguments if args is None else args),
            "disableASLR": False,
            "shellExpandArguments": False,
            "console": "internalConsole",
            "enableAutoVariableSummaries": False,
        }

    def function_scopes(self):
        """Each function in the declared sources, its line span and locals (for describe)."""
        dwarfdump = _tool(self.target.dwarfdump)
        env = dict(self.prepared.manifest.effective_environment)
        workspace = self.prepared.workspace

        def dump(option, limit):
            return _metadata(
                dwarfdump, [option, str(self.symbols)], workspace, env, max_bytes=limit
            )

        info = dump("--debug-info", MAX_DEBUG_INFO)
        return function_scopes(
            info, dump("--debug-line", MAX_METADATA), workspace, tuple(self.lines)
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
        `error: summary string parsing error` is never a value. None leaves the value."""
        shown = None
        language = getattr(getattr(self, "target", None), "language", None)
        if not self.whole_string_summaries and language == "cpp":
            found = self._string_class(variable.get("type"))
            if found is not None or text_like(variable):
                shown = libstdcxx_string(variable, fetch, read, found)
        if shown and ("value" in shown or shown.get("unavailable")):
            # Peeky's own text (read from memory, or noted): spelled and bounded already.
            return shown
        text = variable.get("value")
        tidied = tidy(text, language in ("c", "cpp")) if isinstance(text, str) else None
        if tidied is None:
            return shown
        return {**(shown or {}), **tidied}

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

    def not_yet_declared(self, variable, frame, innermost=True, stack=None, deeper=None):
        """Why a root local of ``frame`` has no value yet (declared below the stop), or
        None. Its storage then holds whatever an earlier call or iteration left.
        ``stack``: the frames listed with it (innermost first), when known; ``deeper()``:
        more of them, when a rule needs what runs below the listed ones."""
        name, path = variable.get("name"), ((frame or {}).get("source") or {}).get("path")
        line = (frame or {}).get("line")
        if name == "this" and (building := self._under_construction(frame, stack, deeper)):
            return building
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
        if not innermost and (
            unfinished := declared_unfinished(
                self.declarations,
                name,
                here,
                line,
                self._unslid(frame),
                ends=lambda at, local: self._statement_end(here, at, local),
            )
        ):
            at, why, detail = unfinished
            if why == "file":
                return (
                    f"declared on line {at} of "
                    + (detail or "a file outside the program's sources")
                    + f", while this caller frame is at {stopped}:{raw_line}, in "
                    "another file, inside a call it makes: an initializer can `#include` "
                    "its rows, or be included itself, so Peeky cannot place the frame "
                    "within or after the declaration, and it may not be initialized yet: "
                    "its storage may hold leftovers; read it after the call returns"
                )
            if why == "statement":
                return (
                    (
                        f"declared on line {at}, and its declaration (lines {at}-{detail}) "
                        "is still running in this caller frame"
                        if detail is not None
                        else f"declared on line {at}, and Peeky cannot tell from the source "
                        "where its declaration ends (or the compiler may read that text "
                        "differently: a directive, a macro, a line splice or a trigraph), "
                        "so this caller frame may be inside it"
                    )
                    + " (inside a call it makes): it may not be initialized yet, and its "
                    "storage may hold leftovers; read it after the call returns"
                )
            if why == "no code":
                return (
                    f"declared on line {at}, which has no code of its own (an aggregate is "
                    "stored element by element on its elements' lines), so this caller "
                    "frame cannot tell whether it is initialized yet: its storage may hold "
                    "leftovers; read it in the frame that is running once it is stored"
                )
            # A caller is inside a call: `long r = f(x);` stores `r` only after it.
            return (
                f"declared on line {at}, whose code this caller frame has not finished (it "
                "is inside a call, and the declaration may be stored after the call "
                "returns), so it may not be initialized yet: its storage may hold "
                "leftovers; read it after the call returns"
            )
        address = self._unslid(frame)
        below = declared_below(
            self.declarations, name, here, line, through=self._through if innermost else None
        )
        if below is not None:
            return (
                f"declared at line {below}, below this stop, so not initialized yet: its "
                f"storage holds leftovers; stop after line {below} to read it"
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
                    f"declared on line {at} of {origin}, and Peeky cannot tell from the "
                    f"source whether its declaration has run by {here}:{line} ("
                    + (
                        f"the `#include` on line {where} brings it in, and its initializer "
                        "can run on, or an `#include` or a label after it skip it"
                        if where is not None
                        else f"no one `#include` in {here} brings it in"
                    )
                    + "), so it may not be initialized yet: its storage may hold leftovers"
                )
            return (
                (
                    f"declared on line {at}, and its declaration (lines {at}-{end}) is still "
                    f"running at this stop: it is not fully initialized yet, and its storage "
                    f"may hold leftovers; stop after line {end} to read it"
                )
                if end is not None
                else f"declared on line {at}, and Peeky cannot tell from the source where its "
                "declaration ends (or the compiler may read that text differently: a "
                "directive, a macro, a line splice or a trigraph), so this stop may be "
                "inside it: it may not be initialized yet, and its storage may hold leftovers"
            )
        if building := self._foreign_line(variable, frame, stopped, raw_line):
            return building
        if skipped := declared_skipped(
            self.declarations, name, here, line, address, self._jumped_past
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
                else f"declared on line {at}, and Peeky cannot tell from the source whether "
                "a jump (a `switch` or a `goto`) reaches this line past the declaration, so "
                "it may not be initialized here: its storage may hold leftovers"
            )
        if address is None or self.target.optimization != "O0":
            return None
        # A loop that branches back to this address can bring control here with the
        # local live (x86-64 puts `while ((x = next()) > 0)`'s test first): only an
        # address the disassembly shows no loop returns to is withheld.
        if (
            declared_on_line(self.declarations, name, here, line, address) is not None
            and getattr(self, "loop_targets", {}).get(address) is False
        ):
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

    def _statement_end(self, source, line, name):
        """Where the declaration of ``name`` on ``line`` of the prepared ``source`` ends
        (c_source.Statements), or None, also when a source or header cannot be read."""
        statements = self._statements(source)
        return None if statements is None else statements.end(line, name)

    def _through(self, source, line, name):
        """Statements.declared_through for the prepared ``source``, or None."""
        statements = self._statements(source)
        return None if statements is None else statements.declared_through(line, name)

    def _settled(self, source, line, name):
        """Whether a statement's header sets ``name`` on ``line`` once, by no call (True,
        False), or None if unknown (Statements.declaration)."""
        statements = self._statements(source)
        found = None if statements is None else statements.declaration(line, name)
        return None if found is None else found[3]

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
        shown = _SHADOWED.fullmatch(name or "")
        places = (function or {}).get("locals", {}).get(shown[1] if shown else name, ())
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
            f"this frame runs {source}:{line}, outside its function's own lines (a class's "
            "default member initializer, run where an object is built, or code an "
            "`#include` brings in), so this object may be under construction or skipped: it "
            "may not be initialized yet, and its storage may hold leftovers; read it once "
            "the frame is back in its own lines"
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
        address = self._unslid(frame)
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

    def _unslid(self, frame):
        """The frame's instruction address in the executable's own numbering, or None."""
        pointer = (frame or {}).get("instructionPointerReference")
        bias = getattr(self, "load_bias", None)
        if bias is None or not isinstance(pointer, str):
            return None
        if not re.fullmatch(r"0x[0-9a-fA-F]{1,16}", pointer):
            return None
        return int(pointer, 16) - bias

    def validate_mutation_target(self, variable, frame):
        if reason := self.not_yet_declared(variable, frame):
            raise ValueError(
                f"{variable.get('name')!r} is {reason}; its declaration would overwrite a "
                "change made here"
            )

    def mid_statement(self, variable, frame, stop=None):
        """Why a change to the root local ``variable`` of the stopped ``frame`` may not
        reach a statement under way at the stop, or None (#105). Part-way through a
        statement, what it has already read may be held in a register or a temporary
        (`long t = s.a + f();`, back from `f` with `step --out`). So the stop must be on
        one of its function's own lines, at that line's first instruction, and the line
        must start a statement (Statements.begins), not continue one (`+ f();` under
        `long t = s.a`); anything Peeky cannot tell counts as part-way. ``stop``: the
        stop's reason (unused here)."""
        place, function, address = self._place(frame), self._function_at(frame), None
        if function is not None:
            address = self._unslid(frame)
        if place is None or function is None or address is None:
            return "Peeky cannot place the stop among its function's statements"
        source, line = place
        if self._own_line(function, source, line) is not True:
            return (
                f"the stop is on {source}:{line}, outside its function's own lines (a "
                "class's member initializer, or code an `#include` brings in), part-way "
                "through the statement that runs it"
            )
        if not _line_start(function, source, line, address):
            return (
                f"the stop is part-way through line {line} (back from a call on it, as after "
                "`step --out`)"
            )
        statements = self._statements(source)
        begins = statements.begins(line) if statements is not None else None
        if begins is False:
            return f"line {line} continues a statement that an earlier line starts"
        if begins is None:
            return f"Peeky cannot tell from the source whether line {line} starts a statement"
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
            signed, bits, names = settable_member(graph, kind, name, steps)
            if not framed:
                raise Unsettable(
                    f"`{name}` is not kept in this frame's own memory (a static, or a value "
                    "the debug information places in registers)"
                )
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

    def _root_type(self, frame, root):
        """(type offset, whether the frame keeps it in its own memory, type graph) of
        the root local of a field or element change; raises fields.Unsettable unless it
        is one local of one type."""
        from peeky.fields import Unsettable

        function = self._function_at(frame) if self.fields is not None else None
        if function is None or function.get("types") is None:
            raise Unsettable(
                "Peeky cannot match this frame to the build's debug information (it reads "
                "it when the session starts; a program using setjmp has none)"
            )
        return (*self._declared_type(function["types"], root.get("name")), self.fields)

    def _root_holds(self, frame, root, stack=None, deeper=None):
        """Raise fields.Unsettable unless the root local holds its value at the stop:
        not declared below it, in an initializer still running, or skipped by a jump
        (#120, #121)."""
        from peeky.fields import Unsettable

        if reason := self.not_yet_declared(root, frame, stack=stack, deeper=deeper):
            raise Unsettable(
                f"`{root.get('name')}` is {reason}; its declaration would overwrite a change "
                "made here"
            )

    @staticmethod
    def _declared_type(types, name):
        """(type offset, whether every declaration is an automatic or a parameter in the
        frame's memory) of the local ``name`` (`s`, or `s @ main.rs:81` as LLDB names one
        of several) in a function's ``types`` (fields.local_types)."""
        from peeky.fields import Unsettable

        shown = re.fullmatch(r"(.+) @ ([^:]+):([0-9]+)", str(name))
        declared = types.get(shown[1] if shown else name)
        if declared and shown:
            # LLDB names a local by where it is declared when several share its name.
            declared = {each for each in declared if each[2] == (shown[2], int(shown[3]))}
        if not declared:
            raise Unsettable(f"the debug information declares no local `{name}` in this function")
        kinds = {kind for kind, _, _ in declared}
        if None in kinds:
            raise Unsettable(f"the debug information gives the local `{name}` no type")
        if len(kinds) != 1:
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

    def normalize_capabilities(self, raw):
        return {
            "supports_set_variable": raw.get("supportsSetVariable") is True,
            # lldb-dap changes a field or element as it changes a local (#105).
            "supports_member_set": raw.get("supportsSetVariable") is True,
            # Used only inside the worker, by qualified value views (Rust containers).
            "supports_read_memory": raw.get("supportsReadMemoryRequest") is True,
            "supports_conditional_breakpoints": False,
            "supports_hit_conditional_breakpoints": False,
            "supports_logpoints": False,
            "supports_evaluate": False,
            "supports_attach": False,
        }

    def validate_breakpoints(self, points):
        self.target.validate_breakpoints(points)

    def validate_value(self, value):
        if not isinstance(value, str) or not re.fullmatch(r"[+-]?(?:0|[1-9][0-9]{0,19})", value):
            raise ValueError("Native mutations require bounded decimal integer literals")

    # A value lldb-dap gives no memoryReference has no address at all, so a write could
    # only change LLDB's copy. This catches nothing in practice (lldb-dap 20 and 21 give
    # even bitfield bindings one, set to their value): confirm_mutation's read-back is
    # what guarantees every reported change, on every adapter.
    requires_memory_location = True

    def settable(self, variable, frame=None):
        """Whether a root local is a writable integer of a type Peeky can change."""
        return _writable_integer(variable)

    def mutation_literal(self, value):
        """The canonical decimal sent to LLDB (`+5` and unsigned `-0` are not its syntax)."""
        return str(int(value))

    def confirm_mutation(self, name, value, before, after):
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
            # Display aid only (a local declared on the stop line): unknown never withholds.
            self.load_bias = load_bias(self.binary, module)
        except (OSError, ValueError, struct.error):
            self.load_bias = None
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
