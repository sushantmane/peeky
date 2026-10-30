"""Strict input contract for a dependency-free Cargo crate.

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
    # None for a crate without an integration test: verification is the checker alone.
    test: str | None
    test_entrypoint: str | None
    sources: tuple[str, ...]
    tests: tuple[str, ...]

    @property
    def inputs(self):
        return ("Cargo.toml", "Cargo.lock", *self.sources, *self.tests)


# Package fields that only describe the crate; none changes what Cargo builds.
_METADATA = frozenset(
    {
        "authors",
        "categories",
        "description",
        "documentation",
        "homepage",
        "keywords",
        "license",
        "license-file",
        "publish",
        "readme",
        "repository",
        "rust-version",
    }
)
_SWITCHES = ("build", "autobins", "autotests", "autoexamples", "autobenches", "autolib")
# Dependency tables (`cargo new` writes an empty [dependencies]); Peeky builds offline, so
# they must stay empty.
_EMPTY_TABLES = frozenset({"dependencies", "dev-dependencies", "build-dependencies"})
_SHAPE = (
    "Peeky builds one dependency-free binary crate, with at most one integration test "
    "(see docs/lang/rust-cookbook.md#set-up-the-crate)"
)


def _toml(root, name):
    path = _inside(root, name)
    if name == "Cargo.lock" and not path.exists():
        raise ValueError(
            "Cargo.lock is missing: run `cargo generate-lockfile` in the crate once (Peeky "
            "builds offline, from the lockfile)"
        )
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError(f"{name} must be a regular file of at most 1 MiB")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("Invalid Cargo manifest/lockfile TOML") from exc


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError("Expected a plain Cargo package/target name")
    return value


def _sources(root, names, *, empty=False):
    if not isinstance(names, tuple) or (not names and not empty) or len(names) > 64:
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
        raise ValueError(f"Exactly one explicit Cargo {kind} target is required. {_SHAPE}")
    target = entries[0]
    if not isinstance(target, dict) or set(target) != {"name", "path"}:
        raise ValueError(f"Cargo {kind} target supports only name and path")
    name = _name(target["name"])
    if not isinstance(target["path"], str) or target["path"] not in declared:
        raise ValueError(f"Cargo {kind} target path must be explicitly declared")
    return name, target["path"]


def _exists(root, name):
    path = root / name
    return path.exists() or path.is_symlink()


def _package(manifest):
    """The [package] table, checked: what Cargo would build from it is only the binary
    and the tests Peeky declares."""
    extra = set(manifest) - {"package", "bin", "test", *_EMPTY_TABLES}
    if extra:
        raise ValueError(
            f"Unsupported Cargo manifest tables ({', '.join(sorted(extra))}); "
            "dependencies/workspaces are excluded. " + _SHAPE
        )
    for table in sorted(_EMPTY_TABLES & set(manifest)):
        if manifest[table] != {}:
            raise ValueError(
                f"Unsupported Cargo manifest: [{table}] must be empty (Peeky builds offline, "
                "without dependencies)"
            )
    package = manifest.get("package")
    if not isinstance(package, dict) or not {"name", "version", "edition"} <= set(package):
        raise ValueError("Cargo [package] needs name, version and edition")
    if fields := set(package) - {"name", "version", "edition", *_SWITCHES, *_METADATA}:
        raise ValueError(f"Unsupported Cargo package fields ({', '.join(sorted(fields))})")
    if any(package[key] is not False for key in _SWITCHES if key in package):
        raise ValueError(
            "Cargo build scripts and automatic target discovery must be disabled where set "
            "(build = false, autobins = false, ...); leave them out to use Cargo's defaults"
        )
    return package


def cargo_layout(root):
    """(binary source, test sources) Cargo builds for this crate: its [[bin]] or
    src/main.rs, and its [[test]] or, by Cargo's discovery, tests/*.rs."""
    root = Path(root).resolve(strict=True)
    manifest = _toml(root, "Cargo.toml")
    package = _package(manifest)
    bins = manifest.get("bin")
    binary = (
        bins[0].get("path")
        if isinstance(bins, list) and bins and isinstance(bins[0], dict)
        else "src/main.rs"
    )
    tests = [
        entry["path"]
        for entry in manifest.get("test", [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    ]
    if "autotests" not in package and (root / "tests").is_dir():
        for path in sorted((root / "tests").iterdir()):
            relative = f"tests/{path.name}"
            if path.name.startswith("."):
                # Cargo's discovery skips hidden entries (`._a.rs`, Emacs's `.#a.rs`).
                continue
            if path.is_dir() and _exists(path, "main.rs"):
                raise ValueError(
                    f"Integration tests in a directory ({relative}/main.rs) are not supported. "
                    + _SHAPE
                )
            if path.suffix == ".rs" and relative not in tests:
                tests.append(relative)
    return binary, tuple(tests)


def validate_cargo_inputs(root, *, sources, tests):
    """Accept one binary, at most one integration test and a dependency-free lockfile.

    Explicit targets with discovery switched off are accepted; so is a plain
    `cargo new` crate, when nothing Cargo would discover besides those targets exists.
    """
    root = Path(root).resolve(strict=True)
    _sources(root, sources)
    _sources(root, tests, empty=True)
    if set(sources) & set(tests):
        raise ValueError("Rust application and immutable test inputs must be distinct")
    manifest = _toml(root, "Cargo.toml")
    package = _package(manifest)
    # A switch left out means Cargo's default: what it would find must not exist.
    for switch, name, what in (
        ("build", "build.rs", "Cargo build scripts (build.rs) are not supported"),
        ("autolib", "src/lib.rs", "A library target (src/lib.rs) is not supported"),
        ("autobins", "src/bin", "More binaries (src/bin) are not supported"),
    ):
        if switch not in package and _exists(root, name):
            raise ValueError(f"{what}. {_SHAPE}")
    name = _name(package["name"])
    version = package["version"]
    if not isinstance(version, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version
    ):
        raise ValueError("Expected a plain Cargo package version")
    if len(version) > 62 or any(int(part) > 2**64 - 1 for part in version.split(".")):
        raise ValueError("Cargo package version components exceed u64 bounds")
    if package["edition"] not in ("2021", "2024"):
        raise ValueError("Rust support is qualified for editions 2021 and 2024")
    if "bin" in manifest:
        binary, entrypoint = _target(manifest, "bin", sources)
        if (
            "autobins" not in package
            and _exists(root, "src/main.rs")
            and entrypoint != "src/main.rs"
            and binary != name
        ):
            # Cargo would also build src/main.rs, as a second binary named the package.
            raise ValueError(
                f"src/main.rs would be a second binary ({name}) beside [[bin]] {binary}: "
                f"set autobins = false, or point [[bin]] at src/main.rs. {_SHAPE}"
            )
    elif "autobins" in package:
        raise ValueError(f"Declare the binary in a [[bin]] table (autobins = false). {_SHAPE}")
    else:
        binary, entrypoint = name, "src/main.rs"
        if entrypoint not in sources:
            raise ValueError("Cargo bin target path must be explicitly declared")
    layout = cargo_layout(root)[1]
    if set(layout) != set(tests):
        raise ValueError("Cargo test targets must be explicitly declared")
    test = test_entrypoint = None
    if "test" in manifest:
        test, test_entrypoint = _target(manifest, "test", tests)
    if len(tests) > 1:
        raise ValueError(
            f"At most one Cargo integration test is supported ({', '.join(tests)}). {_SHAPE}"
        )
    if tests and test is None:
        test_entrypoint = tests[0]
        test = Path(test_entrypoint).stem
    if test is not None and binary.replace("-", "_") == test.replace("-", "_"):
        # Both executables would be deps/<crate>-<hash>; Peeky could not tell them apart.
        raise ValueError(
            f'The Cargo [[test]] target must not share the [[bin]] name "{binary}": '
            f'rename it in Cargo.toml, for example name = "{test.replace("-", "_")}_test"'
        )
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
