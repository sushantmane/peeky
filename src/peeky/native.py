"""Fixed C/LLDB targets with fresh builds and bounded native symbol evidence.

Supported formats are single-architecture Mach-O (UUID) and ELF64 little-endian
(GNU build ID). File/module identity is trusted-code integrity, not protection
against hostile in-memory patching. SDK/system headers are not hermetic inputs.
"""

import hashlib
import json
import os
import re
import shutil
import signal
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
    assert_current,
    prepare,
)

MAX_METADATA = 2 * 1024 * 1024
FLAGS = ("-std=c11", "-g", "-gdwarf-4", "-O0", "-fno-omit-frame-pointer")


def _tool(name):
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError("Invalid native tool executable")
    path = shutil.which(name)
    if not path:
        raise ValueError(f"Missing native tool: {name}")
    return str(Path(path).resolve(strict=True))


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


def _metadata(tool, arguments, workspace, env):
    """Bound command time, diagnostic output and parsed metadata file size."""
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
            deadline = time.monotonic() + 15
            exceeded = False
            try:
                while process.poll() is None:
                    if (
                        time.monotonic() >= deadline
                        or errors.stat().st_size > 16000
                        or (output.exists() and output.stat().st_size > MAX_METADATA)
                    ):
                        exceeded = True
                        break
                    time.sleep(0.01)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
        if exceeded or process.returncode or not output.is_file():
            raise RuntimeError("Native symbol command failed or exceeded its budget")
        if output.stat().st_size > MAX_METADATA or errors.stat().st_size > 16000:
            raise ValueError("Native symbol metadata exceeds limit")
        return output.read_text(encoding="utf-8")


def parse_lines(text, workspace, declared):
    """Parse the LLVM DWARF4 line-table dump; unrecognized rows fail closed."""
    if len(text.encode()) > MAX_METADATA:
        raise ValueError("Native line metadata exceeds limit")
    workspace = Path(workspace).resolve()
    inputs = {str((workspace / name).resolve()): name for name in declared}
    counts = {name: len((workspace / name).read_text().splitlines()) for name in declared}
    result = {name: set() for name in declared}
    directories, files, current = {0: str(workspace)}, {}, None
    table_count = 0
    version = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("debug_line["):
            table_count += 1
            if table_count > 4096:
                raise ValueError("Too many native line tables")
            directories, files, current = {0: str(workspace)}, {}, None
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
                entry = files[file_id]
                directory = directories[entry["directory"]]
                path = (workspace / directory / entry["name"]).resolve()
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError("Unresolved native line source") from exc
            if "end_sequence" in fields or number == 0:
                continue
            # SDK/system header instructions are not selectable user source.
            name = inputs.get(str(path))
            if name is not None:
                if not 0 < number <= counts[name]:
                    raise ValueError("Native line exceeds declared source length")
                result[name].add(number)
    if not table_count or not any(result.values()):
        raise ValueError("Missing executable native debug symbol line information")
    return {name: sorted(lines) for name, lines in result.items()}


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
    compiler: str = "clang"
    adapter: str = "lldb-dap"
    dwarfdump: str = "llvm-dwarfdump"
    dsymutil: str | None = None
    sysroot: str | None = None
    arguments: tuple[str, ...] = ()

    def __post_init__(self):
        if (
            not isinstance(self.sources, tuple)
            or not self.sources
            or not isinstance(self.headers, tuple)
        ):
            raise ValueError("Native inputs must be immutable source/header tuples")
        for name in self.sources:
            _path_name(name, ".c")
        for name in self.headers:
            _path_name(name, ".h")
        if (
            not isinstance(self.arguments, tuple)
            or len(self.arguments) > 100
            or any(
                not isinstance(arg, str) or "\x00" in arg or len(arg) > 8192
                for arg in self.arguments
            )
        ):
            raise ValueError("Invalid native application arguments")

    @property
    def verification_inputs(self):
        return self.sources + self.headers

    @property
    def verification_sources(self):
        return self.sources

    @property
    def target_identity(self):
        return {
            "language": "c",
            "sources": sorted(self.sources),
            "headers": sorted(self.headers),
            "arguments": list(self.arguments),
            "compiler_options": list(FLAGS),
        }

    def validate_breakpoints(self, points):
        PythonAdapter().validate_breakpoints(points)
        if any(set(point) != {"line"} for point in points):
            raise ValueError("Native targets support unconditional line breakpoints only")

    def prepare(self, program, root, *, edit=None):
        root = Path(root).resolve(strict=True)
        relative = Path(program).resolve(strict=True).relative_to(root).as_posix()
        if relative not in self.sources:
            raise ValueError("Program is not a declared C source")
        if edit is not None and edit.path not in self.sources:
            raise ValueError("Native candidate edits require a declared C source")
        compiler, adapter, dwarfdump = map(_tool, (self.compiler, self.adapter, self.dwarfdump))
        flags = list(FLAGS)
        if self.sysroot is not None:
            sdk = Path(self.sysroot).resolve(strict=True)
            if not sdk.is_dir():
                raise ValueError("Native sysroot must be a directory")
            flags.extend(("-isysroot", str(sdk)))
        # Keep objects until dsymutil finishes; single-step clang removes its
        # temporary objects and a subsequent dsymutil can erase valid symbols.
        objects = tuple(f"peeky-object-{index}.o" for index in range(len(self.sources)))
        commands = [
            Command((compiler, *flags, "-c", source, "-o", obj))
            for source, obj in zip(self.sources, objects)
        ]
        link_flags = ("-isysroot", str(sdk)) if self.sysroot is not None else ()
        if sys.platform == "linux":
            link_flags = (*link_flags, "-Wl,--build-id=sha1")
        commands.append(Command((compiler, *link_flags, *objects, "-o", "program")))
        compile_commands = {command.argv for command in commands}
        artifacts = [
            ArtifactSpec("program"),
            *(ArtifactSpec(obj, role="object") for obj in objects),
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
                    "c",
                    self.sources + self.headers,
                    tuple(commands),
                    tuple(artifacts),
                    tuple(probes),
                ),
                root,
                edit=edit,
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
            text = _metadata(dwarfdump, ["--debug-line", str(symbols)], prepared.workspace, env)
            lines = parse_lines(text, prepared.workspace, self.sources + self.headers)
            if sys.platform == "darwin":
                identities = []
                for artifact in (binary, symbols):
                    output = _metadata(
                        dwarfdump, ["--uuid", str(artifact)], prepared.workspace, env
                    )
                    found = re.findall(r"UUID:\s+([0-9A-Fa-f-]+)\s+\(", output)
                    if len(found) != 1 or len(found[0].replace("-", "")) != 32:
                        raise ValueError("Expected one native UUID")
                    identities.append(found[0].replace("-", "").upper())
                if identities[0] != identities[1]:
                    raise ValueError("Native binary and symbols UUID mismatch")
                identity = identities[0]
            else:
                identity = elf_build_id(binary)
            session = NativeSession(self, prepared, adapter, lines, identity, binary, symbols)
            session.assert_current()
            return session
        except BaseException:
            prepared.cleanup()
            raise


class NativeSession:
    def __init__(self, target, prepared, adapter, lines, identity, binary, symbols):
        self.target, self.prepared = target, prepared
        self.binary, self.symbols, self.adapter = binary, symbols, adapter
        self._lines_data, self.module_identity = json.dumps(lines, sort_keys=True), identity
        self.home = prepared.workspace / ".peeky-lldb-home"
        self.home.mkdir()
        self.manifest_id = hashlib.sha256(
            json.dumps(
                {
                    "build": prepared.manifest.manifest_id,
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

    def launch_args(self, program, root):
        self.assert_current()
        self.source_path(program)
        return {
            "program": str(self.binary),
            "cwd": str(self.prepared.workspace),
            "args": list(self.target.arguments),
            "disableASLR": False,
            "shellExpandArguments": False,
            "console": "internalConsole",
            "enableAutoVariableSummaries": False,
        }

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
            "supports_conditional_breakpoints": False,
            "supports_hit_conditional_breakpoints": False,
            "supports_logpoints": False,
            "supports_evaluate": False,
            "supports_attach": False,
        }

    def validate_breakpoints(self, points):
        self.target.validate_breakpoints(points)

    def validate_value(self, value):
        if not isinstance(value, str) or not re.fullmatch(r"[+-]?(?:0|[1-9][0-9]{0,9})", value):
            raise ValueError("Native mutations require bounded decimal integer literals")

    def validate_variable(self, variable, value):
        self.validate_value(value)
        kind = variable.get("type")
        attributes = variable.get("presentationHint", {}).get("attributes", [])
        if (
            kind not in ("int", "unsigned int")
            or variable.get("variablesReference", 0) != 0
            or "readOnly" in attributes
        ):
            raise ValueError("Only writable int or unsigned int locals support native mutation")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable.get("name", "")):
            raise ValueError("Native mutation requires an exact local identifier")
        number = int(value)
        if not (
            -2147483648 <= number <= 2147483647 if kind == "int" else 0 <= number <= 4294967295
        ):
            raise ValueError("Native integer mutation is outside the supported 32-bit range")

    def mutation_value(self, response):
        # LLVM18 used result; current DAP adapters use value.
        value = response.get("value", response.get("result"))
        if not isinstance(value, str):
            raise RuntimeError("Native mutation response has no observed value")
        return value

    def assert_current(self):
        assert_current(self.prepared)

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
