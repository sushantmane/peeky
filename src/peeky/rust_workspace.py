"""Rust workspaces with dependencies (ADR 0008, phase 2): the trusted configuration that
names one, and the workspace's shape as its manifests, its lockfile and its vendored
sources give it.

Peeky builds such a workspace itself, in a separate step, offline and from its lockfile,
with every registry dependency from `cargo vendor`'s copy. It refuses here, before
anything runs: a git dependency, a registry other than crates.io, `[patch]` and
`[replace]`, Cargo configuration it doesn't account for, a vendored copy whose checksum
isn't Cargo.lock's, and an analysed package outside editions 2021 and 2024. Trusted
configuration; not a sandbox: Cargo runs the project's build scripts and procedural
macros, as it runs the program.
"""

import hashlib
import json
import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path

from peeky.build import _inside, _relative

# The fields of a workspace's configuration (the tools are RustTarget's).
CONFIG_FIELDS = frozenset(
    {
        "language",
        "workspace",
        "analyse",
        "run",
        "features",
        "vendor",
        "arguments",
        "cargo",
        "rustc",
        "adapter",
        "dwarfdump",
        "linker",
        "dsymutil",
        "sysroot",
        "rust_version",
    }
)
# Any of these makes a Rust configuration a workspace's (a crate's names "sources").
WORKSPACE_FIELDS = frozenset({"workspace", "analyse", "run"})
MAX_ANALYSED = 16
MAX_FEATURES = 64
MAX_PACKAGES = 4096
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_LOCK_BYTES = 16 * 1024 * 1024
MAX_CHECKSUMS_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024 * 1024
EDITIONS = ("2021", "2024")
# crates.io as Cargo.lock names it; the only registry Peeky builds from.
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
VENDORED_SOURCE = "peeky-vendored"

_PACKAGE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")
_FEATURE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_+.-]{0,127}(?:/[A-Za-z0-9_][A-Za-z0-9_+.-]{0,127})?")
_CHECKSUM = re.compile(r"[0-9a-f]{64}")
_RUN_SHAPE = (
    '"run" names what runs: {"bin": "<name>"}, {"package": "<name>", "test": "<name>"} for an '
    'integration test, or {"package": "<name>", "lib": true} for the unit tests of its library'
)


@dataclass(frozen=True)
class Run:
    """What runs: a binary ("bin"), an integration test ("test"), or a library's unit
    tests ("lib", whose ``name`` is None). ``package`` None: the one with that target."""

    kind: str
    name: str | None
    package: str | None


@dataclass(frozen=True)
class WorkspaceConfig:
    """A workspace's configuration, less its tools. Paths are relative: ``workspace`` to
    the target directory (the root), ``vendor`` to the workspace."""

    workspace: str
    analyse: tuple[str, ...]
    run: Run
    features: tuple[str, ...] = ()
    vendor: str | None = None


def is_workspace_config(data):
    """Whether a Rust configuration names a workspace (else it names a crate's sources)."""
    return isinstance(data, dict) and bool(WORKSPACE_FIELDS & set(data))


def _package_name(value, what):
    if not isinstance(value, str) or not _PACKAGE.fullmatch(value):
        raise ValueError(f"{what} must be a Cargo package name, such as tokio or grep-printer")
    return value


def _run(value):
    if not isinstance(value, dict) or not value:
        raise ValueError(_RUN_SHAPE)
    kinds = set(value) - {"package"}
    if len(kinds) != 1 or kinds - {"bin", "test", "lib"}:
        raise ValueError(_RUN_SHAPE)
    kind = kinds.pop()
    package = value.get("package")
    if package is not None:
        _package_name(package, '"run"\'s package')
    if kind == "lib":
        if value["lib"] is not True or package is None:
            raise ValueError(_RUN_SHAPE)
        return Run("lib", None, package)
    name = value[kind]
    if not isinstance(name, str) or not _PACKAGE.fullmatch(name):
        raise ValueError(f'"run"\'s {kind} must be a Cargo target name. {_RUN_SHAPE}')
    return Run(kind, name, package)


def _directory(value, what, default=None):
    if value is None:
        return default
    try:
        return _relative(value, directory=True)
    except ValueError:
        raise ValueError(
            f"{what} must be a relative path to a directory, without `..`: {value!r}"
        ) from None


def workspace_config(data):
    """(WorkspaceConfig, the other fields: the tools and arguments) of a trusted Rust
    configuration that names a workspace."""
    if not isinstance(data, dict):
        raise ValueError("Rust configuration must be a JSON object")
    if fields := {"sources", "tests", "opt_level"} & set(data):
        raise ValueError(
            "A Rust configuration names either a crate's sources and tests, or a workspace "
            f"(analyse, run): this one has both ({', '.join(sorted(fields))})"
        )
    if unknown := set(data) - CONFIG_FIELDS:
        raise ValueError(
            f"Unknown Rust workspace configuration field: {', '.join(sorted(unknown))}"
        )
    if data.get("language", "rust") != "rust":
        raise ValueError('A Rust workspace\'s configuration has "language": "rust"')
    analyse = data.get("analyse")
    if (
        not isinstance(analyse, (list, tuple))
        or not analyse
        or len(analyse) > MAX_ANALYSED
        or len(set(analyse)) != len(analyse)
    ):
        raise ValueError(
            f'"analyse" lists the packages Peeky reads values in (1 to {MAX_ANALYSED}, each '
            'once), such as ["grep-printer"]'
        )
    for name in analyse:
        _package_name(name, 'Each of "analyse"')
    if "run" not in data:
        raise ValueError(_RUN_SHAPE)
    features = data.get("features", [])
    if not isinstance(features, (list, tuple)) or len(features) > MAX_FEATURES:
        raise ValueError(f'"features" is a list of at most {MAX_FEATURES} Cargo features')
    for feature in features:
        if not isinstance(feature, str) or not _FEATURE.fullmatch(feature):
            raise ValueError(f"Not a Cargo feature name: {feature!r}")
    vendor = _directory(data.get("vendor"), '"vendor"')
    if vendor == ".":
        raise ValueError('"vendor" names the directory `cargo vendor` wrote, such as "vendor"')
    config = WorkspaceConfig(
        _directory(data.get("workspace"), '"workspace"', "."),
        tuple(analyse),
        _run(data["run"]),
        tuple(features),
        vendor,
    )
    rest = {key: value for key, value in data.items() if key not in WORKSPACE_FIELDS}
    rest.pop("features", None), rest.pop("vendor", None), rest.pop("language", None)
    return config, rest


# Cargo configuration in the workspace's own `.cargo/config.toml` that Peeky accounts for.
# Peeky's environment overrides `build.rustflags` and every target's `rustflags`
# (CARGO_ENCODED_RUSTFLAGS), `build.target-dir` (CARGO_TARGET_DIR), `build.incremental`
# (CARGO_INCREMENTAL), and the profile's opt-level and debuginfo; the analysed packages'
# recorded rustc commands are checked whatever a profile says. The rest changes nothing
# Peeky builds offline, from its own source replacement, for this host.
_CONFIG_ANY = frozenset(
    {
        "alias",
        "cache",
        "cargo-new",
        "credential-alias",
        "doc",
        "future-incompat-report",
        "http",
        "install",
        "net",
        "profile",
        "registries",
        "registry",
        "term",
    }
)
_CONFIG_BUILD = frozenset({"jobs", "rustflags", "rustdocflags", "incremental", "target-dir"})
# A target table's keys for this host (its triple, or a `cfg(...)`) that Peeky's flags
# override, or that only `cargo run` and `cargo test` use (`runner`; Peeky runs the
# executable itself). Others, `linker` and a build script's overrides, change the build.
_CONFIG_HOST_TARGET = frozenset({"rustflags", "rustdocflags", "runner"})


def check_cargo_config(path, host, *, follow=False):
    """The SHA-256 of a `.cargo/config.toml` (or legacy `.cargo/config`) Cargo reads, the
    workspace's own or one above it, refused where it sets what Peeky doesn't account for
    (_CONFIG_ANY, _CONFIG_BUILD, _CONFIG_HOST_TARGET); ``host``: this toolchain's host
    triple, whose target table applies. ``follow``: a link is read as Cargo reads it (a
    file above the root, such as a dotfile manager's `~/.cargo/config.toml`)."""
    data, digest = _read_toml(path, MAX_MANIFEST_BYTES, follow=follow, digest=True)
    refused = []
    for key, value in data.items():
        if key in _CONFIG_ANY:
            continue
        if key == "build" and isinstance(value, dict):
            refused += [f"build.{name}" for name in value if name not in _CONFIG_BUILD]
        elif key == "target" and isinstance(value, dict):
            for triple, table in value.items():
                if triple != host and not triple.startswith("cfg("):
                    continue
                if not isinstance(table, dict):
                    refused.append(f"target.{triple}")
                    continue
                refused += [
                    f"target.{triple}.{name}" for name in table if name not in _CONFIG_HOST_TARGET
                ]
        else:
            refused.append(key)
    if refused:
        raise ValueError(
            f"Peeky doesn't build with {', '.join(f'`{each}`' for each in refused[:5])} in "
            f"{path}: it changes how Cargo builds, and Peeky would not account for it. Take it "
            "out of the file, or build without it"
        )
    return digest


def _read_toml(path, limit, *, follow=False, digest=False):
    """A TOML file's table (and the SHA-256 of the bytes parsed, with ``digest``)."""
    path = Path(path)
    if (path.is_symlink() and not follow) or not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"{path} must be a regular file of at most {limit // 1024 // 1024} MiB")
    data = path.read_bytes()
    if len(data) > limit:
        raise ValueError(f"{path} must be a regular file of at most {limit // 1024 // 1024} MiB")
    try:
        table = tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path} is not valid TOML: {exc}") from None
    return (table, hashlib.sha256(data).hexdigest()) if digest else table


@dataclass(frozen=True)
class Locked:
    """A package Cargo.lock names: ``source`` None for a path package; ``checksum``, a
    registry package's."""

    name: str
    version: str
    source: str | None
    checksum: str | None


def read_lock(path):
    """Every package of a Cargo.lock (version 3 or 4). Refused: a git dependency, another
    registry than crates.io, a registry package without a checksum, `[[patch.unused]]`."""
    data = _read_toml(path, MAX_LOCK_BYTES)
    version = data.get("version")
    if type(version) is not int or version not in (3, 4):
        raise ValueError(
            f"{path} is not a lockfile Peeky reads (version 3 or 4): run `cargo update "
            "--workspace` with Cargo 1.53 or later to rewrite it"
        )
    if set(data) - {"version", "package", "patch", "metadata"} or data.get("metadata"):
        raise ValueError(f"{path} has tables Peeky doesn't read")
    if data.get("patch"):
        raise ValueError(
            f"Peeky doesn't build a workspace with [patch]: {path} lists unused patches. "
            "Remove the [patch] section and run `cargo update --workspace`"
        )
    packages = data.get("package", [])
    if not isinstance(packages, list) or len(packages) > MAX_PACKAGES:
        raise ValueError(f"{path} must list at most {MAX_PACKAGES} packages")
    found = []
    for entry in packages:
        if not isinstance(entry, dict):
            raise ValueError(f"{path} has a malformed [[package]]")
        name, version = entry.get("name"), entry.get("version")
        source, checksum = entry.get("source"), entry.get("checksum")
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError(f"{path} has a [[package]] without a name or version")
        what = f"`{name} {version}`"
        if isinstance(source, str) and source.startswith("git+"):
            raise ValueError(
                f"Peeky doesn't build git dependencies, vendored or not: Cargo.lock's {what} "
                f"comes from {source.split('#', 1)[0]}, and Cargo.lock records no checksum for "
                "a git source, so nothing ties a vendored copy to the commit it names. Depend "
                "on a crates.io release of it, or a copy inside the workspace"
            )
        if source is not None and source != CRATES_IO:
            raise ValueError(
                f"Peeky builds crates.io dependencies only: Cargo.lock's {what} comes from "
                f"{source}. Depend on its crates.io release, or a copy inside the workspace"
            )
        if source is None and checksum is not None:
            raise ValueError(f"{path}: the path package {what} has a checksum")
        if source is not None and (
            not isinstance(checksum, str) or not _CHECKSUM.fullmatch(checksum)
        ):
            raise ValueError(
                f"Cargo.lock's {what} has no checksum to check its vendored copy against. "
                "Run `cargo update --workspace` to record it"
            )
        found.append(Locked(name, version, source, checksum))
    return tuple(found)


def vendor_command(workspace, vendor="vendor"):
    """The command that vendors a workspace's dependencies, as a refusal gives it."""
    return f"cd {shlex.quote(str(workspace))} && cargo vendor --locked {shlex.quote(vendor)}"


def vendored(workspace, vendor, locked):
    """{(name, version): the `.cargo-checksum.json` of its vendored copy, relative to
    ``workspace``} for each registry package of ``locked``, whose copy in ``vendor`` (from
    `cargo vendor`) must have Cargo.lock's checksum. Cargo then checks each file of the
    copy against that file as it builds."""
    registry = {(each.name, each.version): each.checksum for each in locked if each.source}
    if not registry:
        return {}
    first = ", ".join(f"`{name} {version}`" for name, version in sorted(registry)[:3])
    more = f" and {len(registry) - 3} more" if len(registry) > 3 else ""
    if vendor is None:
        raise ValueError(
            f"Peeky builds registry dependencies only from `cargo vendor`'s copy, and the "
            f"configuration names none ({first}{more} come from crates.io). Run "
            f'`{vendor_command(workspace)}`, then add "vendor": "vendor" to the configuration'
        )
    try:
        directory = _inside(Path(workspace), vendor, directory=True)
    except ValueError:
        raise ValueError(f'"vendor" must be a directory inside the workspace: {vendor}') from None
    if not directory.is_dir():
        raise ValueError(
            f"Peeky builds registry dependencies only from `cargo vendor`'s copy, and "
            f"{directory} doesn't exist ({first}{more} come from crates.io). Run "
            f"`{vendor_command(workspace, vendor)}`"
        )
    copies = {}
    entries = sorted(directory.iterdir())
    if len(entries) > MAX_PACKAGES:
        raise ValueError(f"{directory} holds more than {MAX_PACKAGES} entries")
    for entry in entries:
        manifest, sums = entry / "Cargo.toml", entry / ".cargo-checksum.json"
        if entry.is_symlink() or not entry.is_dir() or not manifest.is_file():
            # Cargo reads only directories with a manifest: `cargo vendor` writes no other.
            continue
        package = _read_toml(manifest, MAX_MANIFEST_BYTES).get("package")
        name = package.get("name") if isinstance(package, dict) else None
        version = package.get("version") if isinstance(package, dict) else None
        if (name, version) not in registry:
            continue
        relative = f"{vendor}/{entry.name}/.cargo-checksum.json"
        if sums.is_symlink() or not sums.is_file() or sums.stat().st_size > MAX_CHECKSUMS_BYTES:
            raise ValueError(
                f"Provenance: vendored `{name} {version}` ({vendor}/{entry.name}) has no "
                f"checksum file. Run `{vendor_command(workspace, vendor)}` again"
            )
        try:
            checksum = json.loads(sums.read_text(encoding="utf-8")).get("package")
        except (UnicodeError, ValueError, AttributeError, RecursionError):
            checksum = None
        if checksum != registry[(name, version)]:
            raise ValueError(
                f"Provenance: vendored `{name} {version}` ({vendor}/{entry.name}) is not the "
                f"one Cargo.lock names: its checksum is {str(checksum)[:16]}…, Cargo.lock's "
                f"{registry[(name, version)][:16]}…. Run `{vendor_command(workspace, vendor)}` "
                "again"
            )
        if (name, version) in copies:
            raise ValueError(
                f"{directory} holds `{name} {version}` twice. Run "
                f"`{vendor_command(workspace, vendor)}` again"
            )
        copies[(name, version)] = relative
    if missing := sorted(set(registry) - set(copies)):
        listed = ", ".join(f"`{name} {version}`" for name, version in missing[:3])
        raise ValueError(
            f"Peeky builds registry dependencies only from `cargo vendor`'s copy, and "
            f"{directory} has none of {listed}"
            + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")
            + f", which Cargo.lock names. Run `{vendor_command(workspace, vendor)}` again"
        )
    return copies


def vendor_arguments(workspace, vendor):
    """Cargo's `--config` arguments that take crates.io packages from the vendored copy."""
    directory = (Path(workspace) / vendor).resolve()
    return (
        "--config",
        f'source.crates-io.replace-with="{VENDORED_SOURCE}"',
        "--config",
        f"source.{VENDORED_SOURCE}.directory={json.dumps(str(directory))}",
    )


@dataclass(frozen=True)
class Preflight:
    """What `preflight` read: the workspace (resolved), Cargo.lock's packages, the vendored
    copies' checksum files, the workspace's own configuration files (relative to the root),
    and those above it: ((absolute path, SHA-256), ...)."""

    workspace: Path
    locked: tuple[Locked, ...]
    vendored: dict
    cargo_config: tuple[str, ...]
    configs_above: tuple[tuple[str, str], ...] = ()


def configs_above(workspace, host):
    """((path, SHA-256), ...) of the Cargo configuration above the workspace, which Cargo
    reads too (`~/.cargo/config.toml` above a workspace in the home directory), each
    accepted under check_cargo_config's rules. A manifest above it is refused: Cargo could
    take the workspace for a member of another. Peeky's own CARGO_HOME holds no
    configuration (cargo_environment), so Cargo reads no other."""
    found = []
    for parent in Path(workspace).parents:
        manifest = parent / "Cargo.toml"
        if manifest.exists() or manifest.is_symlink():
            raise ValueError(
                f"Peeky builds a Rust workspace only where Cargo reads no manifest above it, and "
                f"{manifest} is above {workspace}. Move it, or the workspace"
            )
        for name in ("config", "config.toml"):
            path = parent / ".cargo" / name
            if path.exists() or path.is_symlink():
                found.append((str(path), check_cargo_config(path, host, follow=True)))
    return tuple(found)


def check_configs_above(configs):
    """Refuse a build whose Cargo configuration above the workspace changed since it was
    read (``configs``: configs_above's), by path."""
    for path, digest in configs:
        try:
            now = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            now = None
        if now != digest:
            raise ValueError(
                f"Provenance: Cargo's configuration {path} changed since the build read it. "
                "Build again"
            )


def preflight(root, config, host):
    """Check what Cargo will read before it runs (Preflight): the configuration above the
    workspace (configs_above) and its own (check_cargo_config), its manifest (no `[patch]`
    or `[replace]`), its lockfile (read_lock) and the vendored copies (vendored)."""
    root = Path(root).resolve(strict=True)
    try:
        workspace = _inside(root, config.workspace, directory=True)
    except ValueError:
        raise ValueError(
            f'"workspace" must be a directory inside {root}: {config.workspace}'
        ) from None
    if not (workspace / "Cargo.toml").is_file():
        raise ValueError(f"{workspace} has no Cargo.toml: name the workspace's directory")
    workspace = workspace.resolve(strict=True)
    above = configs_above(workspace, host)
    relative = workspace.relative_to(root)
    cargo = workspace / ".cargo"
    if cargo.is_symlink():
        raise ValueError(f"Cargo's configuration directory can't be a link: {cargo}")
    files = []
    for name in ("config", "config.toml"):
        path = cargo / name
        if path.exists() or path.is_symlink():
            check_cargo_config(path, host)
            files.append((relative / ".cargo" / name).as_posix())
    manifest = _read_toml(workspace / "Cargo.toml", MAX_MANIFEST_BYTES)
    if sections := [key for key in ("patch", "replace") if key in manifest]:
        raise ValueError(
            f"Peeky doesn't build a workspace with [{sections[0]}] (in "
            f"{workspace / 'Cargo.toml'}): Cargo would build other sources than Cargo.lock "
            "names. Remove it, or make the patched crate a path dependency inside the workspace"
        )
    lock = workspace / "Cargo.lock"
    if not lock.exists():
        raise ValueError(
            f"{workspace} has no Cargo.lock, and Peeky builds only from one: run "
            f"`cd {shlex.quote(str(workspace))} && cargo generate-lockfile` once"
        )
    locked = read_lock(lock)
    copies = vendored(workspace, config.vendor, locked)
    return Preflight(workspace, locked, copies, tuple(files), above)


@dataclass(frozen=True)
class Target:
    """A Cargo target: its name, kinds (bin, lib, test, proc-macro, custom-build, …),
    source file relative to the root, and edition."""

    name: str
    kinds: tuple[str, ...]
    source: str
    edition: str


@dataclass(frozen=True)
class Package:
    """A path package of the workspace: ``directory`` relative to the root."""

    name: str
    version: str
    directory: str
    edition: str
    targets: tuple[Target, ...]

    @property
    def manifest(self):
        return f"{self.directory}/Cargo.toml" if self.directory != "." else "Cargo.toml"


@dataclass(frozen=True)
class WorkspaceShape:
    """A workspace as Peeky builds it: its path packages (``local``), those it analyses,
    what runs (``run``: its package and target), the files Cargo reads that a build's
    provenance covers (``inputs``, relative to the root), the vendored copies' checksum
    files ({(name, version): path relative to the root}), and the Cargo configuration
    above the workspace ((absolute path, SHA-256), ...: check_configs_above)."""

    root: Path
    workspace: Path
    local: tuple[Package, ...]
    analysed: tuple[Package, ...]
    run: tuple[Package, Target]
    inputs: tuple[str, ...]
    vendored: dict
    features: tuple[str, ...] = ()
    configs_above: tuple[tuple[str, str], ...] = ()

    def cargo_arguments(self):
        """Cargo's subcommand and target selection for this run (after `cargo`)."""
        package, target = self.run
        if "bin" in target.kinds:
            selection = ("build", "-p", package.name, "--bin", target.name)
        elif "test" in target.kinds:
            selection = ("test", "--no-run", "-p", package.name, "--test", target.name)
        else:
            selection = ("test", "--no-run", "-p", package.name, "--lib")
        if self.features:
            selection += ("--features", ",".join(self.features))
        return selection


def _target(entry, root, package):
    kinds = entry.get("kind") if isinstance(entry, dict) else None
    if (
        not isinstance(entry, dict)
        or not isinstance(entry.get("name"), str)
        or not isinstance(entry.get("src_path"), str)
        or not isinstance(kinds, list)
        or not all(isinstance(kind, str) for kind in kinds)
    ):
        raise ValueError("Cargo's metadata has a target Peeky can't read")
    path = Path(entry["src_path"]).resolve()
    try:
        source = path.relative_to(root).as_posix()
    except ValueError:
        raise ValueError(
            f"Peeky builds only files inside {root}: target `{entry['name']}` of `{package}` "
            f"is {path}. Move it into the workspace"
        ) from None
    return Target(entry["name"], tuple(kinds), source, str(entry.get("edition")))


def read_workspace(root, config, preflight, metadata):
    """The WorkspaceShape of ``config``'s workspace from Cargo's ``metadata`` (`cargo
    metadata --format-version 1`, as parsed JSON, with dependencies). Every path package
    must be inside ``root``; each analysed one a path package of edition 2021 or 2024,
    and no procedural macro; the run's target one of a path package's."""
    root = Path(root).resolve(strict=True)
    workspace = preflight.workspace
    if not isinstance(metadata, dict) or not isinstance(metadata.get("packages"), list):
        raise ValueError("Cargo's metadata is not what Peeky reads")
    if Path(str(metadata.get("workspace_root"))).resolve() != workspace:
        raise ValueError(
            f"Cargo reads {metadata.get('workspace_root')} as the workspace, not {workspace}"
        )
    local, registry = [], set()
    for entry in metadata["packages"]:
        if not isinstance(entry, dict):
            raise ValueError("Cargo's metadata is not what Peeky reads")
        name, version = entry.get("name"), entry.get("version")
        if entry.get("source") is not None:
            registry.add((name, version))
            continue
        manifest = Path(str(entry.get("manifest_path"))).resolve()
        try:
            directory = manifest.parent.relative_to(root).as_posix()
        except ValueError:
            raise ValueError(
                f"Peeky doesn't build a path dependency outside {root}: `{name}` is at "
                f"{manifest.parent}, and Peeky compares only files inside the root. Move it "
                "into the workspace"
            ) from None
        targets = tuple(_target(target, root, name) for target in entry.get("targets") or ())
        local.append(Package(name, version, directory, str(entry.get("edition")), targets))
    names = [package.name for package in local]
    if len(set(names)) != len(names):
        twice = sorted({name for name in names if names.count(name) > 1})
        raise ValueError(f"Peeky can't tell apart two path packages named `{twice[0]}`")
    locked = {(each.name, each.version) for each in preflight.locked if each.source}
    if unlocked := registry - locked:
        name, version = sorted(unlocked)[0]
        raise ValueError(f"Cargo's metadata names `{name} {version}`, which Cargo.lock doesn't")
    by_name = {package.name: package for package in local}
    analysed = []
    for name in config.analyse:
        package = by_name.get(name)
        if package is None:
            where = (
                "a registry dependency"
                if any(each == name for each, _ in registry)
                else "not a package of the workspace"
            )
            raise ValueError(
                f"Peeky analyses only the workspace's own packages: `{name}` is {where}. "
                f"Its packages: {', '.join(sorted(by_name)[:20])}"
            )
        editions = {package.edition, *(target.edition for target in package.targets)}
        if outside := sorted(editions - set(EDITIONS)):
            raise ValueError(
                f"Peeky analyses only editions 2021 and 2024: package `{name}` is edition "
                f"{outside[0]}. Analyse another package, or move this one to edition 2021 "
                "(`cargo fix --edition`)"
            )
        if any("proc-macro" in target.kinds for target in package.targets):
            raise ValueError(
                f"Peeky analyses only code the program runs: `{name}` is a procedural macro, "
                "which runs in the compiler. Analyse the packages that use it"
            )
        analysed.append(package)
    run = _run_target(config.run, local)
    prefix = workspace.relative_to(root)
    inputs = {package.manifest for package in local} | set(preflight.cargo_config)
    inputs |= {(prefix / "Cargo.toml").as_posix(), (prefix / "Cargo.lock").as_posix()}
    inputs = sorted(inputs)
    copies = {key: (prefix / path).as_posix() for key, path in preflight.vendored.items()}
    return WorkspaceShape(
        root,
        workspace,
        tuple(local),
        tuple(analysed),
        run,
        tuple(inputs),
        copies,
        config.features,
        preflight.configs_above,
    )


def _run_target(run, local):
    """(package, target) for ``run`` among the path packages ``local``."""
    wanted = {"bin": "bin", "test": "test"}.get(run.kind)
    found = []
    for package in local:
        if run.package is not None and package.name != run.package:
            continue
        for target in package.targets:
            if wanted is not None and wanted in target.kinds and target.name == run.name:
                found.append((package, target))
            elif wanted is None and {"lib", "rlib", "dylib"} & set(target.kinds):
                found.append((package, target))
    if run.package is not None and not any(package.name == run.package for package in local):
        raise ValueError(
            f'"run" names package `{run.package}`, which is not one of the workspace\'s own'
        )
    if len(found) == 1:
        return found[0]
    what = "a library" if run.kind == "lib" else f"a {run.kind} target `{run.name}`"
    if not found:
        raise ValueError(
            f"No package of the workspace{f' named `{run.package}`' if run.package else ''} "
            f"has {what}: {_RUN_SHAPE}"
        )
    packages = ", ".join(sorted(package.name for package, _ in found))
    raise ValueError(
        f'Several packages have {what} ({packages}): add "package" to "run", such as '
        f'{{"package": "{found[0][0].name}", "{run.kind}": "{run.name}"}}'
    )


def cargo_environment(rustc, cargo_home):
    """The environment Peeky runs Cargo in for a workspace: offline, with ``cargo_home``
    (Peeky's own, without configuration) as CARGO_HOME, and no rustc wrapper from any
    configuration."""
    home = Path(cargo_home)
    for name in ("config", "config.toml", "credentials", "credentials.toml"):
        if (home / name).exists() or (home / name).is_symlink():
            raise ValueError(f"Peeky's own CARGO_HOME holds configuration: {home / name}")
    return {
        "PATH": f"{Path(rustc).parent}:/usr/bin:/bin",
        "CARGO_HOME": str(home),
        "RUSTC": str(rustc),
        "RUSTC_WRAPPER": "",
        "RUSTC_WORKSPACE_WRAPPER": "",
        "CARGO_NET_OFFLINE": "true",
        "CARGO_TERM_COLOR": "never",
        "LC_ALL": "C",
    }


def _cargo(argv, workspace, env, timeout):
    """Run a Cargo command that only reads (its stdout); BuildCommandError on failure."""
    from peeky.agent import execute
    from peeky.build import BuildCommandError

    result = execute(
        list(argv),
        workspace,
        timeout,
        env=env,
        output_limit=MAX_METADATA_BYTES,
        retain=MAX_METADATA_BYTES,
    )
    if result["exit_code"] != 0 or result.get("timed_out") or result.get("output_limit_exceeded"):
        raise BuildCommandError(tuple(argv), result)
    return result["stdout"]


def host_triple(rustc, env, timeout=30):
    """The host triple of ``rustc`` (`rustc -vV`)."""
    output = _cargo((str(rustc), "-vV"), Path(env["CARGO_HOME"]), env, timeout)
    found = re.search(r"^host: (\S+)$", output, re.M)
    if not found:
        raise ValueError(f"{rustc} -vV names no host")
    return found[1]


def read(root, config, cargo, rustc, cargo_home, timeout=60):
    """The WorkspaceShape of ``config``'s workspace under ``root``: preflight, then
    `cargo metadata` (offline, locked, from the vendored copies), then read_workspace."""
    env = cargo_environment(rustc, cargo_home)
    checked = preflight(root, config, host_triple(rustc, env))
    argv = [str(cargo), "metadata", "--format-version", "1", "--offline", "--locked"]
    if checked.vendored:
        argv += vendor_arguments(checked.workspace, config.vendor)
    output = _cargo(argv, checked.workspace, env, timeout)
    try:
        metadata = json.loads(output)
    except (ValueError, RecursionError):
        raise ValueError("Cargo's metadata is not JSON Peeky reads") from None
    return read_workspace(root, config, checked, metadata)
