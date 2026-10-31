"""Measures a binary a project built against ADR 0008's phase 1: each unit judged or not,
and why, and the sizes Peeky's symbol bounds limit. Run by rebuild.sh in a study image;
the ADR's "Verified for phase 1" table comes from it.

Usage: measure.py <llvm-dwarfdump> <binary> <root> <compiler>

A measurement, not Peeky's judging: it takes a unit's optimization from the last `-O`
word of its recorded command, and reads every unit's debug information at once."""

import collections
import json
import subprocess
import sys
import time
from pathlib import Path

import peeky.native as native
from peeky.native_units import LineFiles, NotRecorded, md5_of, read_units, recorded_command


def _lines(dwarfdump, arguments, total):
    with subprocess.Popen(
        [dwarfdump, *arguments], stdout=subprocess.PIPE, text=True, errors="replace"
    ) as process:
        for line in process.stdout:
            total[0] += len(line.encode())
            yield line.rstrip("\n")


def _optimization(words):
    level, lto, value = "0", False, False
    separate = {"-o", "-I", "-D", "-U", "-include", "-isystem", "-MF", "-MT", "-MQ", "-x"}
    for word in words[1:]:
        if value:
            value = False
        elif word in separate or word in ("-Xclang", "-mllvm"):
            value = True
        elif word.startswith("-O"):
            level = word[2:] or "1"
        elif word == "-flto" or word.startswith("-flto="):
            lto = True
        elif word == "-fno-lto":
            lto = False
    return level, lto


def _why(unit, version, tables, root):
    """Why ``unit`` isn't judged, in a few words each; empty where it is."""
    why = []
    main = Path(unit.comp_dir or "/", unit.name or "")
    if not main.resolve().is_relative_to(root):
        why.append("its main file is outside the root")
    if unit.dwo_name:
        why.append("split DWARF")
    if unit.version < 5:
        why.append(f"DWARF {unit.version}")
    try:
        level, lto = _optimization(recorded_command(unit, version))
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
        return [*why, "no MD5s" + (" (names <stdin>)" if "<stdin>" in named else "")]
    for index, entry in table.files.items():
        path = table.path(index, unit.comp_dir)
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
    found = {unit.name: _why(unit, version, tables, root) for unit in units}
    report["units"] = {name: "; ".join(why) or "judged" for name, why in found.items()}
    report["summary"] = dict(collections.Counter(report["units"].values()).most_common())
    json.dump(report, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main(*sys.argv[1:5])
