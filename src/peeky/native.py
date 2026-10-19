"""Fixed C and C++/LLDB targets with fresh builds and bounded native symbol evidence.

Supported formats are single-architecture Mach-O (UUID) and ELF64 little-endian
(GNU build ID). File/module identity is trusted-code integrity, not protection
against hostile in-memory patching. SDK/system headers are not hermetic inputs.
"""

import codecs
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from peeky.adapters import PythonAdapter
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


def _line_rows(text, workspace, declared, limit=MAX_METADATA):
    """Rows of the LLVM DWARF4 line-table dump for declared sources: (address, name, line).

    Unrecognized rows fail closed. Rows outside the declared sources (SDK and system
    headers) and end-of-sequence rows are skipped.
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
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("debug_line["):
            table_count += 1
            if table_count > 4096:
                raise ValueError("Too many native line tables")
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
            if "end_sequence" in fields or number == 0:
                continue
            name = resolved[file_id]
            if name is not None:
                if not 0 < number <= counts[name]:
                    raise ValueError("Native line exceeds declared source length")
                rows.append((int(fields[0], 16), name, number))
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


def _dies(text):
    """A bounded, lenient parse of `dwarfdump --debug-info`, for display only.

    Returns {offset: die} with each die {tag, attrs, parent, children, depth, ranges};
    nesting is read from the indentation (one level per two spaces after the offset).
    """
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
    for char in name:
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


def _function_entries(info, line_text, workspace, declared):
    """(dies, {root: entry}, {root: {line: span}}, files, {root: {(source, line): span}})
    for functions in declared sources; see function_scopes. A span is the lowest and
    highest address of a function's rows on a line. The first mapping keeps rows of the
    function's own file (describe); the last keeps every row the function runs, in any
    declared file (clang runs a header struct's member initializers in its caller)."""
    import bisect

    workspace = Path(workspace).resolve()
    inputs = {str((workspace / name).resolve()): name for name in declared}
    rows = sorted(_line_rows(line_text, workspace, declared))
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
    lines, runs = {}, {}
    for (address, name, number), claimed, ran in zip(rows, owner, runner):
        for found, key, table in ((claimed, number, lines), (ran, (name, number), runs)):
            if found is not None:
                span = table.setdefault(found[1], {}).setdefault(key, [address, address])
                span[0], span[1] = min(span[0], address), max(span[1], address)
    return dies, functions, lines, (inputs, sources), runs


def _source_of(attribute, inputs, workspace, cache):
    """The declared source a DW_AT_decl_file names, or None; resolved once per name
    (an STL-heavy unit names the same few headers thousands of times)."""
    if attribute not in cache:
        decl = _text(attribute)
        cache[attribute] = inputs.get(str((workspace / decl).resolve())) if decl else None
    return cache[attribute]


def _own_lines(dies, entry, numbers):
    """A function's line numbers from its declaration on, or None if it has none."""
    if not numbers:
        return None
    first_line = _first(dies, entry["chain"], "DW_AT_decl_line")
    if first_line and first_line.isdigit():
        numbers = {number for number in numbers if number >= int(first_line)} or numbers
    return numbers


def function_scopes(info, line_text, workspace, declared):
    """Each function in a declared source: qualified name, source, line span, locals.

    Display only (`describe`). A line-table row belongs to the innermost function or
    inlined call whose address range covers it, so an inlined callee's lines count for
    the callee, not its caller. Copies of one function (C1/C2 constructors, inlined and
    out-of-line copies) are merged through the DIE that declares them.
    """
    dies, functions, lines, _, _ = _function_entries(info, line_text, workspace, declared)
    found = {}
    for root, entry in functions.items():
        source = entry["source"]
        name = _qualified(dies, entry["bodies"][0])
        numbers = _own_lines(dies, entry, lines.get(root))
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
        # arguments) are one scope; their types join (`&[i32] or &[char]`).
        merged = found.setdefault(json.dumps(scope, sort_keys=True), (scope, {}))[1]
        for local, kinds in names.items():
            merged.setdefault(local, set()).update(kinds)
    scopes = []
    for scope, kinds in found.values():
        # Two blocks may declare one name with different types (`int or long`).
        types = {local: " or ".join(sorted(k)) for local, k in sorted(kinds.items()) if k}
        scopes.append({**scope, **({"types": types} if types else {})})
    scopes.sort(key=lambda s: (s["source"], s["lines"][0], s["scope"]))
    if len(scopes) > MAX_SCOPES:
        scopes = [*scopes[:MAX_SCOPES], {"omitted": len(scopes) - MAX_SCOPES}]
    return scopes


# A local in the frame: DW_OP_fbreg (frame base) or DW_OP_bregN (a register) offsets.
_AUTOMATIC = re.compile(r"DW_OP_(?:fbreg|breg[0-9]+)\b")


def _declared(dies, offset, files, workspace):
    """(name, (source, line, automatic)) for each parameter and variable of one function
    body, including nested blocks but not calls inlined into it. Parameters, statics and
    anything without a frame location are not automatic. A label is listed as
    (None, None): `goto` can return above a declaration with the local still live."""
    found, pending = [], list(dies[offset]["children"])
    while pending and len(found) < 4096:
        child = pending.pop()
        die = dies[child]
        if die["tag"] == "DW_TAG_lexical_block":
            pending.extend(die["children"])
            continue
        if die["tag"] == "DW_TAG_label":
            found.append((None, None))
            continue
        if die["tag"] not in ("DW_TAG_formal_parameter", "DW_TAG_variable"):
            continue
        chain = _chain(dies, child)
        name = _text(_first(dies, chain, "DW_AT_name"))
        if not name or name.startswith("$"):
            continue
        if die["tag"] == "DW_TAG_formal_parameter":
            found.append((name, (None, None, False)))
            continue
        inputs, cache = files
        source = _source_of(_first(dies, chain, "DW_AT_decl_file"), inputs, workspace, cache)
        line = _first(dies, chain, "DW_AT_decl_line")
        location = die["attrs"].get("DW_AT_location") or ""
        automatic = bool(_AUTOMATIC.search(location)) and "DW_OP_addr" not in location
        found.append((name, (source, int(line) if line and line.isdigit() else None, automatic)))
    return found


def local_declarations(info, line_text, workspace, declared):
    """Each function in a declared source: its source; ``rows``, the lowest and highest
    address of its code on each (source, line) it runs, in any declared file and even
    above its declaration (member initializers run where their class is built); for each local
    name, where every local of that name is declared, as (source, line, automatic);
    and whether it has a label, where `goto` can return above a declaration."""
    dies, functions, _, files, runs = _function_entries(info, line_text, workspace, declared)
    workspace = Path(workspace).resolve()
    table = []
    for root, entry in functions.items():
        rows = runs.get(root)
        if not rows:
            continue
        names, jumps = {}, False
        for body in (*entry["bodies"], root):
            for name, where in _declared(dies, body, files, workspace):
                if name is None:
                    jumps = True
                else:
                    names.setdefault(name, set()).add(where)
        table.append(
            {
                "source": entry["source"],
                "rows": {number: tuple(span) for number, span in rows.items()},
                "locals": names,
                "jumps": jumps,
            }
        )
    return table


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


def declared_below(table, name, source, line):
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
        if not ((source, at) in rows and rows[here][1] < rows[source, at][0]):
            return None
    return min(where[1] for _, where in candidates)


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
            try:
                info = _metadata(
                    dwarfdump,
                    ["--debug-info", str(symbols)],
                    prepared.workspace,
                    env,
                    max_bytes=MAX_DEBUG_INFO,
                    timeout=_remaining(deadline),
                )
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
            except (RuntimeError, ValueError, OSError):
                # Display aid only: without it, values show as the debugger reads them.
                declarations = None
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
    ):
        self.target, self.prepared = target, prepared
        # local_declarations of the build; None (unknown) never withholds a value.
        self.declarations = declarations
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

    def not_yet_declared(self, variable, frame):
        """Why a root local of ``frame`` has no value yet (declared below the stop), or
        None. Its storage then holds whatever an earlier call or iteration left."""
        name, path = variable.get("name"), ((frame or {}).get("source") or {}).get("path")
        line = (frame or {}).get("line")
        if not self.declarations or not isinstance(name, str) or not isinstance(path, str):
            return None
        try:
            source = Path(path).resolve().relative_to(self.prepared.workspace.resolve())
        except (OSError, ValueError):
            return None
        if type(line) is not int:
            return None
        below = declared_below(self.declarations, name, source.as_posix(), line)
        if below is None:
            return None
        return (
            f"declared at line {below}, below this stop, so not initialized yet: its storage "
            f"holds leftovers; stop after line {below} to read it"
        )

    def validate_mutation_target(self, variable, frame):
        if reason := self.not_yet_declared(variable, frame):
            raise ValueError(
                f"{variable.get('name')!r} is {reason}; its declaration would overwrite a "
                "change made here"
            )

    def source_path(self, original):
        relative = (
            Path(original).resolve(strict=True).relative_to(self.prepared.source_root).as_posix()
        )
        if relative not in self.lines:
            raise ValueError("Source is not a prepared native input")
        return self.prepared.workspace / relative

    def local_scopes(self, scopes):
        return PythonAdapter().local_scopes(scopes)

    def normalize_capabilities(self, raw):
        return {
            "supports_set_variable": raw.get("supportsSetVariable") is True,
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

    def close(self):
        self.prepared.cleanup()
