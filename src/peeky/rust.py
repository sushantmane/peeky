"""Fixed dependency-free Cargo binaries debugged through LLDB DAP.

Scope: one explicit edition-2021 binary with immutable declared integration-test
files, built offline/locked into a fresh workspace with a controlled environment
and explicit real (non-rustup-proxy) cargo/rustc executables. Symbols come only
from application compile units (see rust_symbols). Root i32 locals are the only
mutable values; qualified Option<i32> values are shown by active variant and other
data-carrying enums are opaque. Cargo test execution and candidate-repair
verification are not implemented here. Trusted code; not a sandbox.
"""

import hashlib
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from peeky.adapters import PythonAdapter
from peeky.build import (
    ArtifactSpec,
    BuildCommandError,
    BuildRecipe,
    Command,
    prepare,
)
from peeky.native import NativeSession, _path_name, module_identity
from peeky.rust_manifest import assert_cargo_ancestry_clear, validate_cargo_inputs
from peeky.rust_symbols import active_variant, application_symbols

DEPS = "target/debug/deps"
RAW_ENUM_FIELDS = ("$variants$", "$discr$")
MAX_VIEW_FETCHES = 128


def _absolute_tool(path, label):
    if not isinstance(path, str) or not path or "\x00" in path or not Path(path).is_absolute():
        raise ValueError(f"Rust {label} must be an absolute executable path")
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"Rust {label} must be an executable file")
    return str(resolved)


def _toolchain(cargo, rustc):
    """Require real sysroot binaries; hashing a rustup proxy does not identify rustc."""
    cargo, rustc = _absolute_tool(cargo, "cargo"), _absolute_tool(rustc, "rustc")
    cargo_path, rustc_path = Path(cargo), Path(rustc)
    if (
        cargo_path.name != "cargo"
        or rustc_path.name != "rustc"
        or cargo_path.parent != rustc_path.parent
        or not (rustc_path.parent.parent / "lib/rustlib").is_dir()
    ):
        raise ValueError("Rust cargo/rustc must be the same real toolchain sysroot binaries")
    return cargo, rustc


def cargo_artifacts(names, crate):
    """Select the hashed application binary and its symbols from fresh deps files."""
    binaries = {
        name
        for name in names
        if re.fullmatch(re.escape(f"{DEPS}/{crate}-") + r"[0-9a-f]{16}", name)
    }
    if len(binaries) != 1:
        raise ValueError("Expected exactly one Cargo application executable")
    (binary,) = binaries
    bundle = binary + ".dSYM/"
    allowed = {binary, binary + ".d"}
    for name in names:
        if name in allowed:
            continue
        if sys.platform == "darwin" and name.startswith(bundle):
            continue
        raise ValueError("Unexpected Cargo build artifact")
    if sys.platform == "darwin":
        symbols = f"{bundle}Contents/Resources/DWARF/{Path(binary).name}"
        if symbols not in names:
            raise ValueError("Missing Cargo packed debug symbols")
        return binary, symbols
    return binary, binary


@dataclass(frozen=True)
class RustTarget:
    sources: tuple[str, ...]
    tests: tuple[str, ...]
    cargo: str
    rustc: str
    adapter: str
    dwarfdump: str
    linker: str
    dsymutil: str | None = None
    sysroot: str | None = None
    arguments: tuple[str, ...] = ()
    rust_version: str = "1.96.0"

    def __post_init__(self):
        for field in (self.sources, self.tests):
            if not isinstance(field, tuple) or not field:
                raise ValueError("Rust sources and tests must be nonempty immutable tuples")
            for name in field:
                _path_name(name, ".rs")
        if (
            not isinstance(self.arguments, tuple)
            or len(self.arguments) > 100
            or any(
                not isinstance(arg, str) or "\x00" in arg or len(arg) > 8192
                for arg in self.arguments
            )
        ):
            raise ValueError("Invalid Rust application arguments")
        if not isinstance(self.rust_version, str) or not re.fullmatch(
            r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", self.rust_version
        ):
            raise ValueError("Invalid pinned Rust version")
        _toolchain(self.cargo, self.rustc)
        for label in ("adapter", "dwarfdump", "linker"):
            _absolute_tool(getattr(self, label), label)
        if self.dsymutil is not None:
            _absolute_tool(self.dsymutil, "dsymutil")

    language = "rust"

    @property
    def verification_inputs(self):
        return ("Cargo.toml", "Cargo.lock", *self.sources, *self.tests)

    @property
    def verification_sources(self):
        return self.sources

    @property
    def rustflags(self):
        flags = ["-C", "dwarf-version=4", "-C", f"linker={_absolute_tool(self.linker, 'linker')}"]
        if sys.platform == "linux":
            flags += ["-C", "link-arg=-Wl,--build-id=sha1"]
        return flags

    @property
    def target_identity(self):
        return {
            "language": "rust",
            "sources": sorted(self.sources),
            "tests": sorted(self.tests),
            "arguments": list(self.arguments),
            "rust_version": self.rust_version,
            "rustflags": self.rustflags,
            "profile": self.profile,
        }

    @property
    def profile(self):
        return {
            "CARGO_INCREMENTAL": "0",
            "CARGO_PROFILE_DEV_OPT_LEVEL": "0",
            "CARGO_PROFILE_DEV_DEBUG": "2",
            "CARGO_PROFILE_DEV_SPLIT_DEBUGINFO": "packed" if sys.platform == "darwin" else "off",
            "CARGO_PROFILE_DEV_STRIP": "none",
        }

    def validate_breakpoints(self, points):
        PythonAdapter().validate_breakpoints(points)
        if any(set(point) != {"line"} for point in points):
            raise ValueError("Rust targets support unconditional line breakpoints only")

    def _environment(self, rustc):
        # rustc runs dsymutil from PATH for packed macOS symbols; prefer the declared one.
        tools = [str(Path(rustc).parent)]
        if sys.platform == "darwin" and self.dsymutil is not None:
            tools.append(str(Path(_absolute_tool(self.dsymutil, "dsymutil")).parent))
        env = {
            "PATH": ":".join((*dict.fromkeys(tools), "/usr/bin", "/bin")),
            "CARGO_HOME": ".peeky-cargo-home",
            "CARGO_TARGET_DIR": "target",
            "RUSTC": rustc,
            "CARGO_ENCODED_RUSTFLAGS": "\x1f".join(self.rustflags),
            "CARGO_TERM_COLOR": "never",
            "LC_ALL": "C",
            **self.profile,
        }
        if self.sysroot is not None:
            sdk = Path(self.sysroot).resolve(strict=True)
            if not sdk.is_dir():
                raise ValueError("Rust sysroot must be a directory")
            env["SDKROOT"] = str(sdk)
        return env

    def prepare(self, program, root, *, edit=None):
        root = Path(root).resolve(strict=True)
        relative = Path(program).resolve(strict=True).relative_to(root).as_posix()
        if relative not in self.sources:
            raise ValueError("Program is not a declared Rust application source")
        if edit is not None and edit.path not in self.sources:
            raise ValueError("Rust candidate edits require a declared application source")
        cargo_inputs = validate_cargo_inputs(root, sources=self.sources, tests=self.tests)
        cargo, rustc = _toolchain(self.cargo, self.rustc)
        adapter, dwarfdump, linker = (
            _absolute_tool(self.adapter, "adapter"),
            _absolute_tool(self.dwarfdump, "dwarfdump"),
            _absolute_tool(self.linker, "linker"),
        )
        build = Command(
            (cargo, "build", "--offline", "--locked", "--bin", cargo_inputs.binary), timeout_s=120
        )
        probes = [
            Command((rustc, "-vV")),
            Command((cargo, "--version")),
            Command((adapter, "--help")),
            Command((dwarfdump, "--version")),
            Command((linker, "--version")),
        ]
        if sys.platform == "darwin":
            probes.append(Command((_absolute_tool(self.dsymutil or "", "dsymutil"), "--version")))
        elif sys.platform != "linux":
            raise ValueError("Rust target supports Linux and macOS hosts only")
        recipe = BuildRecipe(
            "rust",
            cargo_inputs.inputs,
            (build,),
            (ArtifactSpec(DEPS, "directory", "cargo-deps"),),
            tuple(probes),
            tuple(sorted(self._environment(rustc).items())),
        )
        # Cargo also reads ancestor configuration; own a checked parent directory.
        parent = Path(tempfile.mkdtemp(prefix="peeky-rust-")).resolve()
        try:
            assert_cargo_ancestry_clear(parent)
            try:
                prepared = prepare(recipe, root, parent=parent, edit=edit)
            except BuildCommandError as exc:
                if exc.command != build.argv:
                    raise RuntimeError("Rust toolchain command failed") from exc
                raise
        except BaseException:
            shutil.rmtree(parent, ignore_errors=True)
            raise
        try:
            versions = {probe.argv[0]: probe.stdout for probe in prepared.manifest.probes}
            if not versions[rustc].startswith(f"rustc {self.rust_version} ") or not versions[
                cargo
            ].startswith(f"cargo {self.rust_version} "):
                raise ValueError("Rust toolchain does not match the pinned version")
            crate = cargo_inputs.binary.replace("-", "_")
            names = [item.path for item in prepared.manifest.artifacts]
            binary, symbols = (prepared.workspace / name for name in cargo_artifacts(names, crate))
            env = dict(prepared.manifest.effective_environment)
            lines, layouts, units = application_symbols(
                dwarfdump,
                symbols,
                prepared.workspace,
                env,
                entrypoint=cargo_inputs.entrypoint,
                crate=crate,
                declared=self.sources,
            )
            identity = module_identity(dwarfdump, binary, symbols, prepared.workspace, env)
            session = RustSession(
                self, prepared, adapter, lines, identity, binary, symbols, layouts, parent
            )
            session.assert_current()
            return session
        except BaseException:
            prepared.cleanup()
            shutil.rmtree(parent, ignore_errors=True)
            raise


class RustSession(NativeSession):
    def __init__(
        self, target, prepared, adapter, lines, identity, binary, symbols, layouts, parent
    ):
        self._layouts_data = json.dumps(layouts, sort_keys=True)
        self.parent = parent
        super().__init__(target, prepared, adapter, lines, identity, binary, symbols)
        self.manifest_id = hashlib.sha256(
            json.dumps(
                {"native": self.manifest_id, "enum_layouts": layouts}, sort_keys=True
            ).encode()
        ).hexdigest()

    @property
    def layouts(self):
        return json.loads(self._layouts_data)

    def launch_args(self, program, root):
        return {
            **super().launch_args(program, root),
            # Never execute Python scripts embedded beside symbol files.
            "initCommands": ["settings set target.load-script-from-symbol-file false"],
        }

    def validate_variable(self, variable, value):
        if variable.get("type") != "int":
            raise ValueError("Only writable i32 locals support Rust mutation")
        super().validate_variable(variable, value)

    def view_variable(self, variable, fetch):
        """Active-variant view for compiled enums; None leaves ordinary values unchanged."""
        return active_variant(variable, fetch, self.layouts)

    def validate_children(self, variables):
        if any(variable.get("name") in RAW_ENUM_FIELDS for variable in variables):
            raise ValueError("Raw Rust enum storage is not exposed; its layout is not qualified")

    def close(self):
        try:
            super().close()
        finally:
            shutil.rmtree(self.parent, ignore_errors=True)
