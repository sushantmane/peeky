"""Measures a binary a project built against ADR 0008's phase 1: each unit judged or not,
and why, and the sizes Peeky's symbol bounds limit. Run by rebuild.sh in a study image;
the ADR's "Verified for phase 1" table comes from it.

Usage: measure.py <llvm-dwarfdump> <binary> <root> <compiler>

A measurement, not Peeky's judging: it reads every unit's debug information at once.
Each unit is reported by its offset, as several may share a name (`<artificial>`)."""

import collections
import json
import posixpath
import subprocess
import sys
import time
from pathlib import Path

import peeky.native as native
from peeky.native_units import (
    LineFiles,
    NotRecorded,
    md5_of,
    optimization,
    read_units,
    recorded_command,
)


def _lines(dwarfdump, arguments, total):
    with subprocess.Popen(
        [dwarfdump, *arguments], stdout=subprocess.PIPE, text=True, errors="replace"
    ) as process:
        for line in process.stdout:
            total[0] += len(line.encode())
            yield line.rstrip("\n")
    if process.returncode:
        raise RuntimeError(f"{dwarfdump} {' '.join(arguments)} failed: {process.returncode}")


def _why(unit, version, tables, root):
    """Why ``unit`` isn't judged, in a few words each; empty where it is. Its optimization
    is read from its recorded words alone (``optimization``)."""
    why = []
    main = posixpath.join(unit.comp_dir or "", unit.name or "")
    if not posixpath.isabs(main):
        why.append("its main file has no absolute path")
    elif not Path(main).resolve().is_relative_to(root):
        why.append("its main file is outside the root")
    if unit.dwo_name:
        why.append("split DWARF")
    if unit.version < 5:
        why.append(f"DWARF {unit.version}")
    try:
        level, lto = optimization(recorded_command(unit, version))
        if level != "0":
            why.append(f"-O{level}")
        if lto:
            why.append("-flto")
    except NotRecorded as error:
        why.append(str(error))
    table = tables.get(unit.stmt_list)
    if table is None:
        return [*why, "no line table"]
    if not table.checksummed():
        named = {entry.name for entry in table.files.values() if not entry.checksum}
        stdin = "<stdin>" in named
        return [*why, "the line table records no MD5s" + (", and names <stdin>" if stdin else "")]
    if differs := table.differs(unit):
        return [*why, differs]
    for index, entry in table.files.items():
        path = table.path(index, unit.comp_dir)
        if path is None:
            why.append(f"{entry.name} has no absolute path")
            continue
        try:
            if md5_of(path) != entry.checksum:
                why.append(f"{path} changed")
        except OSError:
            why.append(f"{path} is missing")
    return why


def main(dwarfdump, binary, root, compiler):
    root = Path(root).resolve()
    said = subprocess.run([compiler, "--version"], capture_output=True, text=True, check=True)
    version = said.stdout.splitlines()[0]
    report = {"binary": binary, "compiler": version}
    index = subprocess.run(
        [dwarfdump, "--debug-info", "--recurse-depth=0", binary],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    units = read_units(index)
    started, total, files = time.monotonic(), [0], LineFiles()
    files.feed(_lines(dwarfdump, ["--debug-line", binary], total))
    tables = files.close()
    report["line_tables"] = {"bytes": total[0], "seconds": round(time.monotonic() - started, 2)}
    # The bounds are measured, not applied.
    native.MAX_DIES, native.MAX_KEPT = 10**9, 1 << 40
    started, total = time.monotonic(), [0]
    dies = native._parse_dies(_lines(dwarfdump, ["--debug-info", binary], total))
    report["debug_information"] = {
        "bytes": total[0],
        "seconds": round(time.monotonic() - started, 2),
        "entries": len(dies),
        "kept": dies.kept,
    }
    report["bounds"] = {
        "line_tables": native.MAX_LINE_TEXT,
        "debug_information": native.MAX_DEBUG_INFO,
        "entries": 400_000,
        "kept": 300 << 20,
    }
    units = [unit for unit in units if unit.kind == "Compile"]
    report["units"] = {
        f"0x{unit.offset:08x} {unit.name}": "; ".join(_why(unit, version, tables, root)) or "judged"
        for unit in units
    }
    names = collections.Counter(unit.name for unit in units)
    report["count"] = {
        "units": len(units),
        "names": len(names),
        "shared": {name: count for name, count in names.items() if count > 1},
    }
    report["summary"] = dict(collections.Counter(report["units"].values()).most_common())
    json.dump(report, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main(*sys.argv[1:5])
