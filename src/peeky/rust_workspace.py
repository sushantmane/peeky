"""Rust workspaces with dependencies (ADR 0008, phase 2): the trusted configuration that
names one, and the workspace's shape as its manifests, its lockfile and its vendored
sources give it.

Peeky builds such a workspace itself, in a separate step, offline and from its lockfile,
with every registry dependency from `cargo vendor`'s copy. It refuses here, before
anything runs: a git dependency, a registry other than crates.io, `[patch]` and
`[replace]`, Cargo configuration it doesn't account for, a vendored copy that isn't what
`cargo vendor` wrote, and an analysed package outside editions 2021 and 2024, or built
otherwise than at opt-level 0. What the build compiles comes from Cargo's unit graph of
the build's own command. What Cargo reads is hashed before it runs and checked again
after. Trusted configuration; not a sandbox: Cargo runs the project's build scripts and
procedural macros, as it runs the program.
"""

import hashlib
import json
import os
import re
import shlex
import sys
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from peeky.build import _inside, _relative
from peeky.digest import sha256_file
from peeky.textlines import split_lines

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
MAX_VENDORED_FILES = 200_000
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


# Cargo's configuration (in the workspace's `.cargo/` and in every directory above it) that
# Peeky accounts for. Peeky's environment (build_environment) overrides `build.rustflags`
# and every target's `rustflags` (CARGO_ENCODED_RUSTFLAGS), `build.target-dir`
# (CARGO_TARGET_DIR), `build.incremental` (CARGO_INCREMENTAL), and a profile's
# opt-level, debug, split-debuginfo, strip and lto (CARGO_PROFILE_*); what overrides
# those for one package is checked in Cargo's unit graph (check_profile). The rest
# changes nothing Peeky builds offline, from its own source replacement, for this host.
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
# A target table's keys for this host (its triple, or a `cfg(...)` that holds for it) that
# Peeky's flags override, or that only `cargo run` and `cargo test` use (`runner`; Peeky
# runs the executable itself). Others, `linker` and a build script's overrides, change
# the build.
_CONFIG_HOST_TARGET = frozenset({"rustflags", "rustdocflags", "runner"})
# The profile settings Peeky's environment sets, for `cargo build` (dev) and `cargo test`
# (test): what its proofs and symbol reading rely on.
_PROFILE = {
    "OPT_LEVEL": "0",
    "DEBUG": "2",
    "STRIP": "none",
    "LTO": "false",
    "INCREMENTAL": "false",
}


class _Unclear(ValueError):
    """A `cfg(...)` expression Peeky can't evaluate."""


_CFG_TOKEN = re.compile(r'\s*(?:([A-Za-z_][A-Za-z0-9_]*)|("(?:[^"\\]|\\.)*")|([(),=]))')


def cfg_applies(key, cfgs):
    """Whether a `[target.'cfg(...)']` key holds for a target with ``cfgs`` (`rustc --print
    cfg`'s lines: `unix`, `target_os="linux"`): `all`, `any`, `not`, names and `name =
    "value"`. _Unclear where Peeky can't evaluate it."""
    if not key.startswith("cfg(") or not key.endswith(")"):
        raise _Unclear(key)
    tokens, at, text = [], 0, key[4:-1]
    while at < len(text):
        found = _CFG_TOKEN.match(text, at)
        if not found or found.end() == at:
            if text[at:].strip():
                raise _Unclear(key)
            break
        tokens.append(found.group(1) or found.group(2) or found.group(3))
        at = found.end()
    known = {line.strip() for line in cfgs}
    position = 0

    def take(expected=None):
        nonlocal position
        if position >= len(tokens) or (expected is not None and tokens[position] != expected):
            raise _Unclear(key)
        position += 1
        return tokens[position - 1]

    def predicate():
        name = take()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise _Unclear(key)
        if name in ("all", "any", "not") and position < len(tokens) and tokens[position] == "(":
            take("(")
            values = []
            while tokens[position : position + 1] != [")"]:
                values.append(predicate())
                if tokens[position : position + 1] == [","]:
                    take(",")
            take(")")
            if name == "not":
                if len(values) != 1:
                    raise _Unclear(key)
                return not values[0]
            return all(values) if name == "all" else any(values)
        if position < len(tokens) and tokens[position] == "=":
            take("=")
            value = take()
            if not value.startswith('"'):
                raise _Unclear(key)
            return f"{name}={value}" in known
        return name in known

    result = predicate()
    if position != len(tokens):
        raise _Unclear(key)
    return result


def check_cargo_config(path, host, cfgs, *, follow=False):
    """The SHA-256 of a `.cargo/config.toml` (or legacy `.cargo/config`) Cargo reads, the
    workspace's own or one above it, refused where it sets what Peeky doesn't account for
    (_CONFIG_ANY, _CONFIG_BUILD, _CONFIG_HOST_TARGET). ``host``: this toolchain's host
    triple, and ``cfgs`` its `rustc --print cfg`, which say whether a target table
    applies. ``follow``: a link is read as Cargo reads it (a file above the root, such as
    a dotfile manager's `~/.cargo/config.toml`)."""
    data, digest = _read_toml(path, MAX_MANIFEST_BYTES, follow=follow, digest=True)
    refused = []
    for key, value in data.items():
        if key in _CONFIG_ANY:
            continue
        if key == "build" and isinstance(value, dict):
            refused += [f"build.{name}" for name in value if name not in _CONFIG_BUILD]
        elif key == "target" and isinstance(value, dict):
            for triple, table in value.items():
                if triple.startswith("cfg("):
                    try:
                        applies = cfg_applies(triple, cfgs)
                    except _Unclear:
                        raise ValueError(
                            f"Peeky can't tell whether `target.'{triple}'` in {path} applies "
                            "to this host, so it doesn't build with it. Take it out of the file, "
                            'or write it with all(), any(), not(), names and name = "value"'
                        ) from None
                elif triple == host:
                    applies = True
                else:
                    continue
                if not applies:
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


def config_file(directory):
    """The configuration file Cargo reads in ``directory``'s `.cargo`, or None: the
    legacy `config` where both it and `config.toml` exist, as Cargo does."""
    cargo = Path(directory) / ".cargo"
    for name in ("config", "config.toml"):
        path = cargo / name
        if path.exists() or path.is_symlink():
            return path
    return None


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
            f"Peeky reads only Cargo.lock versions 3 and 4, and {path} is "
            f"{'version ' + str(version) if version is not None else 'an older one'}. Run "
            "`cargo update --workspace` with this Cargo to rewrite it"
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


def _vendor_refusal(what, workspace, vendor):
    return ValueError(
        f"Peeky builds registry dependencies only from `cargo vendor`'s copy, file by file, "
        f"and {what}. Run `{vendor_command(workspace, vendor)}` again"
    )


def _copy_files(entry, label, workspace, vendor, listed, digest):
    """{path in the copy: SHA-256} of a vendored copy's files, each of which its
    `.cargo-checksum.json` must list (``listed``) with the digest Peeky computes."""
    found = {}
    for base, folders, files in os.walk(entry):
        for name in (*folders, *files):
            path = Path(base, name)
            if path.is_symlink():
                raise _vendor_refusal(
                    f"{label} holds a link ({path.relative_to(entry)})", workspace, vendor
                )
        for name in files:
            path = Path(base, name)
            relative = path.relative_to(entry).as_posix()
            if relative == ".cargo-checksum.json":
                continue
            if not path.is_file():
                raise _vendor_refusal(
                    f"{label} holds {relative}, which is not a regular file", workspace, vendor
                )
            found[relative] = digest(path)
            if len(found) > MAX_VENDORED_FILES:
                raise ValueError(f"The vendored copies hold more than {MAX_VENDORED_FILES} files")
    if not listed:
        raise _vendor_refusal(
            f"{label}'s .cargo-checksum.json lists no files to check it against",
            workspace,
            vendor,
        )
    if unlisted := sorted(set(found) - set(listed)):
        raise _vendor_refusal(
            f"{label}'s .cargo-checksum.json doesn't list {unlisted[0]}"
            + (f" and {len(unlisted) - 1} more" if len(unlisted) > 1 else ""),
            workspace,
            vendor,
        )
    if missing := sorted(set(listed) - set(found)):
        raise _vendor_refusal(
            f"{label} lacks {missing[0]}, which its .cargo-checksum.json lists",
            workspace,
            vendor,
        )
    for relative, value in sorted(found.items()):
        if listed[relative] != value:
            raise ValueError(
                f"Provenance: vendored {label} is not what `cargo vendor` wrote: {relative}'s "
                f"SHA-256 is {value[:16]}…, its .cargo-checksum.json's {str(listed[relative])[:16]}…"
                f". Run `{vendor_command(workspace, vendor)}` again"
            )
    return found


def vendored(workspace, vendor, locked, digest=None):
    """({(name, version): the vendored copy's directory, relative to ``workspace``}, {each
    of its files, relative to ``workspace``: SHA-256}) for each registry package of
    ``locked``. Its copy in ``vendor`` (from `cargo vendor`) must have Cargo.lock's
    checksum, and its `.cargo-checksum.json` must list every file of the copy (none
    missing, none more) with the SHA-256 Peeky computes. So an edit to a copy is refused,
    unless its checksum file is edited to match; nothing authenticates the copy against
    crates.io beyond Cargo.lock's package checksum."""
    digest = digest or sha256_file
    registry = {(each.name, each.version): each.checksum for each in locked if each.source}
    if not registry:
        return {}, {}
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
    copies, files = {}, {}
    entries = sorted(directory.iterdir())
    if len(entries) > MAX_PACKAGES:
        raise ValueError(f"{directory} holds more than {MAX_PACKAGES} entries")
    for entry in entries:
        if entry.is_symlink():
            raise _vendor_refusal(f"{vendor}/{entry.name} is a link", workspace, vendor)
        manifest, sums = entry / "Cargo.toml", entry / ".cargo-checksum.json"
        if not entry.is_dir() or not manifest.is_file():
            # Cargo reads only directories with a manifest: `cargo vendor` writes no other.
            continue
        package = _read_toml(manifest, MAX_MANIFEST_BYTES).get("package")
        name = package.get("name") if isinstance(package, dict) else None
        version = package.get("version") if isinstance(package, dict) else None
        if (name, version) not in registry:
            continue
        label = f"`{name} {version}` ({vendor}/{entry.name})"
        if sums.is_symlink() or not sums.is_file() or sums.stat().st_size > MAX_CHECKSUMS_BYTES:
            raise _vendor_refusal(f"{label} has no .cargo-checksum.json", workspace, vendor)
        try:
            checksums = json.loads(sums.read_text(encoding="utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            checksums = None
        if not isinstance(checksums, dict) or not isinstance(checksums.get("files"), dict):
            raise _vendor_refusal(
                f"{label}'s .cargo-checksum.json isn't one Peeky reads", workspace, vendor
            )
        if checksums.get("package") != registry[(name, version)]:
            raise ValueError(
                f"Provenance: vendored {label} is not the one Cargo.lock names: its "
                f"checksum is {str(checksums.get('package'))[:16]}…, Cargo.lock's "
                f"{registry[(name, version)][:16]}…. Run `{vendor_command(workspace, vendor)}` "
                "again"
            )
        if (name, version) in copies:
            raise _vendor_refusal(f"{directory} holds `{name} {version}` twice", workspace, vendor)
        found = _copy_files(entry, label, workspace, vendor, checksums["files"], digest)
        copies[(name, version)] = f"{vendor}/{entry.name}"
        files[f"{vendor}/{entry.name}/.cargo-checksum.json"] = digest(sums)
        files.update({f"{vendor}/{entry.name}/{path}": value for path, value in found.items()})
    if missing := sorted(set(registry) - set(copies)):
        listed = ", ".join(f"`{name} {version}`" for name, version in missing[:3])
        raise ValueError(
            f"Peeky builds registry dependencies only from `cargo vendor`'s copy, and "
            f"{directory} has none of {listed}"
            + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")
            + f", which Cargo.lock names. Run `{vendor_command(workspace, vendor)}` again"
        )
    return copies, files


def vendor_arguments(workspace, vendor):
    """Cargo's `--config` arguments that take crates.io packages from the vendored copy."""
    directory = (Path(workspace) / vendor).resolve()
    return (
        "--config",
        f'source.crates-io.replace-with="{VENDORED_SOURCE}"',
        "--config",
        f"source.{VENDORED_SOURCE}.directory={json.dumps(str(directory))}",
    )


def configs_above(workspace, host, cfgs):
    """((path, SHA-256), ...) of the Cargo configuration above the workspace, which Cargo
    reads too (`~/.cargo/config.toml` above a workspace in the home directory), each
    accepted under check_cargo_config's rules: in each directory, the one file Cargo
    reads (config_file). A manifest above it is refused: Cargo could take the workspace
    for a member of another. Peeky's own CARGO_HOME holds no configuration
    (cargo_environment), so Cargo reads no other."""
    found = []
    for parent in Path(workspace).parents:
        manifest = parent / "Cargo.toml"
        if manifest.exists() or manifest.is_symlink():
            raise ValueError(
                f"Peeky builds a Rust workspace only where Cargo reads no manifest above it, and "
                f"{manifest} is above {workspace}. Move it, or the workspace"
            )
        if (path := config_file(parent)) is not None:
            found.append((str(path), check_cargo_config(path, host, cfgs, follow=True)))
    return tuple(found)


@dataclass(frozen=True)
class Preflight:
    """What `preflight` read: the workspace (resolved), Cargo.lock's packages, the vendored
    copies ({(name, version): directory relative to the workspace}), the workspace's own
    configuration file (relative to the root) and those above it ((path, SHA-256), ...),
    and the SHA-256 of every file Cargo may read that Peeky checks again after Cargo ran
    (``digests``, by absolute path: configuration at every level, every manifest under the
    root, the lockfile and each vendored file)."""

    workspace: Path
    locked: tuple[Locked, ...]
    vendored: dict
    cargo_config: tuple[str, ...]
    configs_above: tuple[tuple[str, str], ...] = ()
    digests: tuple[tuple[str, str], ...] = ()
    vendor_directory: str | None = None


def _manifests(root, skip):
    """Every `Cargo.toml` under ``root``, but in hidden and `target` directories and
    ``skip`` (the vendored copies, checked file by file)."""
    found = []
    for base, folders, files in os.walk(root):
        folders[:] = sorted(
            name
            for name in folders
            if not name.startswith(".")
            and name != "target"
            and os.path.join(base, name) not in skip
        )
        if "Cargo.toml" in files:
            found.append(Path(base, "Cargo.toml"))
            if len(found) > MAX_PACKAGES:
                raise ValueError(f"{root} holds more than {MAX_PACKAGES} Cargo manifests")
    return found


def preflight(root, config, host, cfgs, digest=None):
    """Check what Cargo will read before it runs (Preflight): the configuration above the
    workspace (configs_above) and its own (check_cargo_config), its manifest (no `[patch]`
    or `[replace]`), its lockfile (read_lock) and the vendored copies (vendored), and
    record the digests ``recheck`` compares after Cargo ran."""
    digest = digest or sha256_file
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
    above = configs_above(workspace, host, cfgs)
    relative = workspace.relative_to(root)
    cargo = workspace / ".cargo"
    if cargo.is_symlink():
        raise ValueError(f"Cargo's configuration directory can't be a link: {cargo}")
    files, digests = [], dict(above)
    if (path := config_file(workspace)) is not None:
        digests[str(path)] = check_cargo_config(path, host, cfgs)
        files.append((relative / ".cargo" / path.name).as_posix())
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
    copies, vendored_files = vendored(workspace, config.vendor, locked, digest)
    vendor_directory = str((workspace / config.vendor).resolve()) if copies else None
    digests[str(lock)] = digest(lock)
    for path in _manifests(root, {vendor_directory} if vendor_directory else set()):
        digests[str(path)] = digest(path)
    digests.update({str(workspace / path): value for path, value in vendored_files.items()})
    return Preflight(
        workspace,
        locked,
        copies,
        tuple(files),
        above,
        tuple(sorted(digests.items())),
        vendor_directory,
    )


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
    """A path package of the workspace: ``directory`` relative to the root; ``member``,
    whether it is one of the workspace's members (Cargo runs Peeky's wrapper for those
    alone)."""

    name: str
    version: str
    directory: str
    edition: str
    targets: tuple[Target, ...]
    member: bool = True

    @property
    def manifest(self):
        return f"{self.directory}/Cargo.toml" if self.directory != "." else "Cargo.toml"


@dataclass(frozen=True)
class WorkspaceShape:
    """A workspace as Peeky builds it: its path packages the build compiles or Cargo
    loads as members (``local``), those it analyses, what runs (``run``: its package and
    target), the files Cargo reads that a build's provenance covers (``inputs``, relative
    to the root), the vendored copies ({(name, version): directory relative to the
    root}), the configuration above the workspace ((path, SHA-256), ...), and
    ``digests``, which ``recheck`` compares again after Cargo runs."""

    root: Path
    workspace: Path
    local: tuple[Package, ...]
    analysed: tuple[Package, ...]
    run: tuple[Package, Target]
    inputs: tuple[str, ...]
    vendored: dict
    features: tuple[str, ...] = ()
    configs_above: tuple[tuple[str, str], ...] = ()
    digests: tuple[tuple[str, str], ...] = ()
    vendor_directory: str | None = None
    host: str = ""
    cfgs: tuple[str, ...] = ()

    def cargo_arguments(self):
        """Cargo's subcommand and target selection for this run (after `cargo`)."""
        package, target = self.run
        return _selection(package.name, target, self.features)


def _selection(package, target, features):
    if "bin" in target.kinds:
        selection = ("build", "-p", package, "--bin", target.name)
    elif "test" in target.kinds:
        selection = ("test", "--no-run", "-p", package, "--test", target.name)
    else:
        selection = ("test", "--no-run", "-p", package, "--lib")
    if features:
        selection += ("--features", ",".join(features))
    return selection


def run_selection(run, features):
    """Cargo's selection for ``run`` before Cargo has named its target's kinds."""
    kinds = {"bin": ("bin",), "test": ("test",), "lib": ("lib",)}[run.kind]
    return _selection(run.package or "", Target(run.name or "", kinds, "", ""), features)


def _target(entry, root):
    """A target of a path package, or None for one outside the root (refused if the build
    compiles it)."""
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
    if not path.is_relative_to(root):
        return None
    return Target(
        entry["name"], tuple(kinds), path.relative_to(root).as_posix(), str(entry.get("edition"))
    )


def check_profile(package, unit):
    """Refuse an analysed package's compile, as Cargo's unit graph gives its profile, that
    Peeky's proofs don't hold for: optimized, without full debug information, with
    link-time optimization, stripped, or aborting on a panic (its proofs are qualified
    with unwinding). A setting for one package (`[profile.dev.package.x]`) overrides
    Peeky's environment, so it is checked here; rustc's recorded command is checked again
    as the build runs."""
    profile = unit.get("profile") or {}
    strip = profile.get("strip")
    stripped = (
        (strip or {}).get("resolved", (strip or {}).get("deferred"))
        if isinstance(strip, dict)
        else strip
    )
    problems = []
    if str(profile.get("opt_level")) != "0":
        problems.append(f"opt-level {profile.get('opt_level')}")
    if profile.get("debuginfo") not in (2, "2", "full"):
        problems.append(f"debug {profile.get('debuginfo')}")
    if str(profile.get("lto")) not in ("false", "off"):
        problems.append(f"lto {profile.get('lto')}")
    if stripped not in ("None", None):
        problems.append(f"strip {stripped}")
    if profile.get("panic", "unwind") != "unwind":
        problems.append(f"panic {profile.get('panic')}")
    if problems:
        raise ValueError(
            f"Peeky analyses only code built at opt-level 0, with full debug information, "
            f"no link-time optimization or stripping, and unwinding panics: `{package}`'s "
            f"{(unit.get('target') or {}).get('name')} has {', '.join(problems)}, from a "
            f"[profile] setting for it (such as [profile.dev.package.{package}] in Cargo.toml). "
            "Remove that setting"
        )


def _unit_package(unit, by_id):
    package = by_id.get(unit.get("pkg_id"))
    if package is None:
        raise ValueError(
            f"Cargo's unit graph names {unit.get('pkg_id')}, which its metadata doesn't"
        )
    return package


def read_workspace(root, config, preflight, metadata, graph):
    """The WorkspaceShape of ``config``'s workspace from Cargo's ``metadata`` (`cargo
    metadata --format-version 1`, as parsed JSON) and ``graph``: Cargo's unit graph of
    the exact command the build runs (its packages, features, targets and profiles). Every
    path package that command compiles, and every workspace member, must be inside
    ``root``. Each analysed package must be a member of edition 2021 or 2024, compiled by
    that command at opt-level 0 (check_profile), and no procedural macro; the run's
    target is the command's root."""
    root = Path(root).resolve(strict=True)
    workspace = preflight.workspace
    if not isinstance(metadata, dict) or not isinstance(metadata.get("packages"), list):
        raise ValueError("Cargo's metadata is not what Peeky reads")
    if not isinstance(graph, dict) or graph.get("version") != 1:
        raise ValueError("Cargo's unit graph is not one Peeky reads (version 1)")
    if Path(str(metadata.get("workspace_root"))).resolve() != workspace:
        raise ValueError(
            f"Cargo reads {metadata.get('workspace_root')} as the workspace, not {workspace}"
        )
    members = set(metadata.get("workspace_members") or ())
    by_id, registry = {}, set()
    for entry in metadata["packages"]:
        if not isinstance(entry, dict):
            raise ValueError("Cargo's metadata is not what Peeky reads")
        by_id[entry.get("id")] = entry
        if entry.get("source") is not None:
            registry.add((entry.get("name"), entry.get("version")))
    locked = {(each.name, each.version) for each in preflight.locked if each.source}
    if unlocked := registry - locked:
        name, version = sorted(unlocked)[0]
        raise ValueError(f"Cargo's metadata names `{name} {version}`, which Cargo.lock doesn't")
    units = graph.get("units")
    if not isinstance(units, list):
        raise ValueError("Cargo's unit graph is not one Peeky reads")
    compiled = {}
    for unit in units:
        entry = _unit_package(unit, by_id)
        if entry.get("source") is None:
            compiled.setdefault(entry["id"], []).append(unit)
    local = {}
    for package_id in sorted(set(compiled) | {each for each in members if each in by_id}):
        entry = by_id[package_id]
        name = entry.get("name")
        manifest = Path(str(entry.get("manifest_path"))).resolve()
        if not manifest.parent.is_relative_to(root):
            why = "the build compiles it" if package_id in compiled else "it is a member"
            raise ValueError(
                f"Peeky doesn't build a path dependency outside {root}: `{name}` is at "
                f"{manifest.parent} ({why}), and Peeky compares only files inside the root. "
                "Move it into the workspace"
            )
        for unit in compiled.get(package_id, ()):
            source = Path(str((unit.get("target") or {}).get("src_path"))).resolve()
            if not source.is_relative_to(root):
                raise ValueError(
                    f"Peeky builds only files inside {root}: target "
                    f"`{(unit.get('target') or {}).get('name')}` of `{name}` is {source}. Move "
                    "it into the workspace"
                )
        targets = tuple(
            target
            for target in (_target(each, root) for each in entry.get("targets") or ())
            if target is not None
        )
        local[package_id] = Package(
            name,
            entry.get("version"),
            manifest.parent.relative_to(root).as_posix(),
            str(entry.get("edition")),
            targets,
            package_id in members,
        )
    names = [package.name for package in local.values()]
    if len(set(names)) != len(names):
        twice = sorted({name for name in names if names.count(name) > 1})
        raise ValueError(f"Peeky can't tell apart two path packages named `{twice[0]}`")
    by_name = {package.name: (package_id, package) for package_id, package in local.items()}
    run = _run_target(config.run, graph, units, by_id, local)
    command = "cargo " + " ".join(_selection(run[0].name, run[1], config.features))
    analysed = []
    for name in config.analyse:
        if name not in by_name:
            where = (
                "a registry dependency"
                if any(each == name for each, _ in registry)
                else "not a package of the workspace"
            )
            raise ValueError(
                f"Peeky analyses only the workspace's own packages: `{name}` is {where}. "
                f"Its packages: {', '.join(sorted(by_name)[:20])}"
            )
        package_id, package = by_name[name]
        if not package.member:
            raise ValueError(
                f"Peeky analyses only the workspace's members, as Cargo runs Peeky's wrapper "
                f"for those alone: `{name}` is a path dependency that isn't one. Add it to "
                "[workspace] members"
            )
        own = [
            unit
            for unit in compiled.get(package_id, ())
            if unit.get("mode") in ("build", "test")
            and not {"custom-build", "proc-macro"}
            & set((unit.get("target") or {}).get("kind") or ())
        ]
        if any("proc-macro" in target.kinds for target in package.targets):
            raise ValueError(
                f"Peeky analyses only code the program runs: `{name}` is a procedural macro, "
                "which runs in the compiler. Analyse the packages that use it"
            )
        if not own:
            raise ValueError(
                f"Peeky analyses only packages the run compiles: `{command}` doesn't compile "
                f'`{name}`. Analyse a package it uses, or change "run" or "features"'
            )
        editions = {
            package.edition,
            *(str((unit.get("target") or {}).get("edition")) for unit in own),
        }
        if outside := sorted(editions - set(EDITIONS)):
            raise ValueError(
                f"Peeky analyses only editions 2021 and 2024: package `{name}` is edition "
                f"{outside[0]}. Analyse another package, or move this one to edition 2021 "
                "(`cargo fix --edition`)"
            )
        for unit in own:
            check_profile(name, unit)
        analysed.append(package)
    prefix = workspace.relative_to(root)
    inputs = {package.manifest for package in local.values()} | set(preflight.cargo_config)
    inputs |= {(prefix / "Cargo.toml").as_posix(), (prefix / "Cargo.lock").as_posix()}
    copies = {key: (prefix / path).as_posix() for key, path in preflight.vendored.items()}
    return WorkspaceShape(
        root,
        workspace,
        tuple(sorted(local.values(), key=lambda package: package.name)),
        tuple(analysed),
        run,
        tuple(sorted(inputs)),
        copies,
        config.features,
        preflight.configs_above,
        preflight.digests,
        preflight.vendor_directory,
    )


def _run_target(run, graph, units, by_id, local):
    """(package, target) of the unit graph's root, which must be what ``run`` names."""
    roots = graph.get("roots")
    if not isinstance(roots, list) or len(roots) != 1 or not isinstance(roots[0], int):
        raise ValueError("Cargo's unit graph has no single root for what runs")
    unit = units[roots[0]]
    entry = _unit_package(unit, by_id)
    target = unit.get("target") or {}
    kinds = tuple(target.get("kind") or ())
    wanted = {"bin": "bin", "test": "test"}.get(run.kind)
    package = local.get(entry.get("id"))
    if (
        package is None
        or (run.package is not None and package.name != run.package)
        or (wanted is not None and (wanted not in kinds or target.get("name") != run.name))
        or (wanted is None and not {"lib", "rlib", "dylib"} & set(kinds))
    ):
        raise ValueError(
            f"Cargo would build `{entry.get('name')}`'s {target.get('name')} for this run, not "
            f'what "run" names: {_RUN_SHAPE}'
        )
    found = next(
        (
            each
            for each in package.targets
            if each.name == target.get("name") and each.kinds == kinds
        ),
        None,
    )
    if found is None:
        raise ValueError(f"Peeky builds only files inside the root: {target.get('src_path')}")
    return package, found


def _run_package(run, metadata):
    """The package Cargo's selection (`-p`) names for ``run``: its own, or the one member
    with that target; refused where none, or several, has it."""
    members = set(metadata.get("workspace_members") or ())
    packages = [
        entry
        for entry in metadata.get("packages") or ()
        if isinstance(entry, dict) and entry.get("source") is None and entry.get("id") in members
    ]
    if run.package is not None and not any(entry.get("name") == run.package for entry in packages):
        raise ValueError(
            f'"run" names package `{run.package}`, which is not one of the workspace\'s own'
        )

    def has(entry):
        for target in entry.get("targets") or ():
            kinds = set(target.get("kind") or ()) if isinstance(target, dict) else set()
            if run.kind == "lib" and {"lib", "rlib", "dylib"} & kinds:
                return True
            if run.kind in kinds and target.get("name") == run.name:
                return True
        return False

    found = sorted(
        {
            entry.get("name")
            for entry in packages
            if (run.package is None or entry.get("name") == run.package) and has(entry)
        }
    )
    if len(found) == 1:
        return found[0]
    what = "a library" if run.kind == "lib" else f"a {run.kind} target `{run.name}`"
    if not found:
        named = f" named `{run.package}`" if run.package else ""
        raise ValueError(f"No package of the workspace{named} has {what}: {_RUN_SHAPE}")
    raise ValueError(
        f"Several packages have a {run.kind} target `{run.name}` ({', '.join(found)}): add "
        f'"package" to "run", such as {{"package": "{found[0]}", "{run.kind}": "{run.name}"}}'
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


def build_environment(rustc, cargo_home, rustflags=()):
    """cargo_environment, plus what defines Peeky's build of a workspace: ``rustflags``
    for every compile (CARGO_ENCODED_RUSTFLAGS, so no configuration's `rustflags` apply),
    no incremental compilation, and the dev and test profiles' settings Peeky relies on
    (_PROFILE), whatever Cargo.toml or Cargo's configuration says of the profile. A
    setting for one package overrides these; check_profile refuses such a one for an
    analysed package."""
    env = cargo_environment(rustc, cargo_home)
    env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(rustflags)
    env["CARGO_INCREMENTAL"] = "0"
    split = "packed" if sys.platform == "darwin" else "off"
    for profile in ("DEV", "TEST"):
        for key, value in {**_PROFILE, "SPLIT_DEBUGINFO": split}.items():
            env[f"CARGO_PROFILE_{profile}_{key}"] = value
    return env


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


def host_cfgs(rustc, env, timeout=30):
    """The host target's `cfg` values (`rustc --print cfg`), which say whether a target
    table of Cargo's configuration applies."""
    output = _cargo((str(rustc), "--print", "cfg"), Path(env["CARGO_HOME"]), env, timeout)
    return tuple(line.strip() for line in split_lines(output) if line.strip())


def recheck(digests, workspace, host, cfgs, above, vendor_directory, vendor, digest=None):
    """Refuse, naming it, a file Cargo may have read that changed since Peeky first read
    it (``digests``: Preflight's), a configuration file that now appears above the
    workspace or in it, or a vendor directory that is now another."""
    digest = digest or sha256_file
    for path, value in digests:
        try:
            now = digest(path)
        except OSError:
            now = None
        if now != value:
            raise ValueError(
                f"Provenance: {path} changed while Peeky read the workspace and Cargo ran. "
                "Build again"
            )
    if configs_above(workspace, host, cfgs) != tuple(above):
        raise ValueError(
            "Provenance: Cargo's configuration above the workspace changed while Peeky read "
            "it and Cargo ran. Build again"
        )
    own = config_file(workspace)
    if own is not None and str(own) not in dict(digests):
        raise ValueError(
            f"Provenance: {own} appeared while Peeky read the workspace and Cargo ran. Build again"
        )
    if vendor_directory is not None and str((workspace / vendor).resolve()) != vendor_directory:
        raise ValueError(
            f"Provenance: the vendored copies' directory {vendor} changed while Peeky read the "
            "workspace. Build again"
        )


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


def read(root, config, cargo, rustc, cargo_home, timeout=60, rustflags=(), digest=None):
    """The WorkspaceShape of ``config``'s workspace under ``root``: preflight; `cargo
    metadata` with every feature; Cargo's unit graph of the exact command the build runs
    (`--unit-graph`, an unstable flag that RUSTC_BOOTSTRAP allows for that one command,
    which compiles nothing), in the build's own environment (build_environment, with
    ``rustflags``); read_workspace; then every file Preflight hashed is checked again
    (recheck)."""
    env = build_environment(rustc, cargo_home, rustflags)
    host, cfgs = host_triple(rustc, env), host_cfgs(rustc, env)
    checked = preflight(root, config, host, cfgs, digest)
    extra = vendor_arguments(checked.workspace, config.vendor) if checked.vendored else ()
    common = ("--offline", "--locked", *extra)
    # Every feature: the metadata describes each package the build may compile; the unit
    # graph says which it does.
    output = _cargo(
        [str(cargo), "metadata", "--format-version", "1", "--all-features", *common],
        checked.workspace,
        env,
        timeout,
    )
    try:
        metadata = json.loads(output)
    except (ValueError, RecursionError):
        raise ValueError("Cargo's metadata is not JSON Peeky reads") from None
    package = _run_package(config.run, metadata)
    selection = run_selection(Run(config.run.kind, config.run.name, package), config.features)
    output = _cargo(
        [str(cargo), *selection, *common, "--unit-graph", "-Z", "unstable-options"],
        checked.workspace,
        {**env, "RUSTC_BOOTSTRAP": "1"},
        timeout,
    )
    try:
        graph = json.loads(output)
    except (ValueError, RecursionError):
        raise ValueError("Cargo's unit graph is not JSON Peeky reads") from None
    chosen = WorkspaceConfig(
        config.workspace,
        config.analyse,
        Run(config.run.kind, config.run.name, package),
        config.features,
        config.vendor,
    )
    shape = read_workspace(root, chosen, checked, metadata, graph)
    recheck(
        checked.digests,
        checked.workspace,
        host,
        cfgs,
        checked.configs_above,
        checked.vendor_directory,
        config.vendor,
        digest,
    )
    return replace(shape, host=host, cfgs=cfgs)
