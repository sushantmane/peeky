"""Strict input contract for the first dependency-free Cargo qualification.

This is preflight validation, not a Rust runtime adapter or a hostile-code sandbox.
Build preparation must still copy/hash these inputs and isolate Cargo's environment.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from peeky.build import _inside


@dataclass(frozen=True)
class CargoInputs:
    package: str
    version: str
    binary: str
    entrypoint: str
    test: str
    test_entrypoint: str
    sources: tuple[str, ...]
    tests: tuple[str, ...]

    @property
    def inputs(self):
        return ("Cargo.toml", "Cargo.lock", *self.sources, *self.tests)


def _toml(root, name):
    path = _inside(root, name)
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Cargo manifest/lockfile must be a bounded regular file")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("Invalid Cargo manifest/lockfile TOML") from exc


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError("Expected a plain Cargo package/target name")
    return value


def _sources(root, names):
    if not isinstance(names, tuple) or not names or len(names) > 64:
        raise ValueError("Declare an immutable nonempty bounded tuple of Rust files")
    for name in names:
        if (
            not isinstance(name, str)
            or not name.endswith(".rs")
            or any(ord(char) < 32 for char in name)
            or not _inside(root, name).is_file()
        ):
            raise ValueError("Expected an existing declared Rust source file")
    if len(set(names)) != len(names):
        raise ValueError("Duplicate declared Rust source")


def _target(manifest, kind, declared):
    entries = manifest.get(kind)
    if not isinstance(entries, list) or len(entries) != 1:
        raise ValueError(f"Exactly one explicit Cargo {kind} target is required")
    target = entries[0]
    if not isinstance(target, dict) or set(target) != {"name", "path"}:
        raise ValueError(f"Cargo {kind} target supports only name and path")
    name = _name(target["name"])
    if not isinstance(target["path"], str) or target["path"] not in declared:
        raise ValueError(f"Cargo {kind} target path must be explicitly declared")
    return name, target["path"]


def validate_cargo_inputs(root, *, sources, tests):
    """Accept only one explicit binary/test and a matching dependency-free lockfile."""
    root = Path(root).resolve(strict=True)
    _sources(root, sources)
    _sources(root, tests)
    if set(sources) & set(tests):
        raise ValueError("Rust application and immutable test inputs must be distinct")
    manifest = _toml(root, "Cargo.toml")
    if set(manifest) != {"package", "bin", "test"}:
        raise ValueError("Unsupported Cargo manifest tables; dependencies/workspaces are excluded")
    package = manifest["package"]
    switches = {"build", "autobins", "autotests", "autoexamples", "autobenches", "autolib"}
    if not isinstance(package, dict) or set(package) != {"name", "version", "edition", *switches}:
        raise ValueError("Unsupported Cargo package fields")
    if any(package[key] is not False for key in switches):
        raise ValueError("Cargo build scripts and automatic target discovery must be disabled")
    name = _name(package["name"])
    version = package["version"]
    if not isinstance(version, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version
    ):
        raise ValueError("Expected a plain Cargo package version")
    if len(version) > 62 or any(int(part) > 2**64 - 1 for part in version.split(".")):
        raise ValueError("Cargo package version components exceed u64 bounds")
    if package["edition"] != "2021":
        raise ValueError("First Rust qualification requires explicit edition 2021")
    binary, entrypoint = _target(manifest, "bin", sources)
    test, test_entrypoint = _target(manifest, "test", tests)
    lock = _toml(root, "Cargo.lock")
    if (
        set(lock) != {"version", "package"}
        or type(lock["version"]) is not int
        or lock["version"] != 4
        or lock["package"] != [{"name": name, "version": version}]
    ):
        raise ValueError("Cargo lockfile must identify only the declared dependency-free package")
    return CargoInputs(name, version, binary, entrypoint, test, test_entrypoint, sources, tests)


def assert_cargo_ancestry_clear(workspace):
    """Reject implicit Cargo config/workspaces; call immediately before every Cargo run.

    CARGO_HOME must separately point to a fresh owned empty directory. This check
    is a filesystem boundary check, not atomic isolation against concurrent edits.
    """
    workspace = Path(workspace).resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Cargo workspace must be a directory")
    for parent in (workspace, *workspace.parents):
        cargo = parent / ".cargo"
        if cargo.is_symlink():
            raise ValueError("Cargo configuration directory cannot be a symlink")
        for name in ("config", "config.toml"):
            path = cargo / name
            if path.exists() or path.is_symlink():
                raise ValueError("Ambient Cargo configuration is not supported")
        if parent != workspace:
            manifest = parent / "Cargo.toml"
            if manifest.exists() or manifest.is_symlink():
                raise ValueError("Ancestor Cargo manifests are not supported")
