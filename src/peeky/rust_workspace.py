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

import fnmatch
import hashlib
import json
import os
import re
import shlex
import sys
import tomllib
import urllib.parse
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
# Peeky accounts for. Peeky's environment (build_environment) sets CARGO_ENCODED_RUSTFLAGS
# (its own flags, and the project's `--cfg` and `--check-cfg` ones: project_rustflags),
# CARGO_TARGET_DIR, CARGO_INCREMENTAL, and a profile's opt-level, debug, split-debuginfo,
# strip and lto (CARGO_PROFILE_*); what overrides those for one package is checked in
# Cargo's unit graph (check_profile). The rest changes nothing Peeky builds offline, from
# its own source replacement, for this host.
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
# Peeky's flags override or keep (`rustflags`: only `--cfg` and `--check-cfg`), or that
# only `cargo run` and `cargo test` use (`runner`; Peeky runs the executable itself).
# Others, `linker` and a build script's overrides, change the build.
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
# The split-debuginfo each platform's build has (Peeky's symbol reading needs it).
SPLIT_DEBUGINFO = "packed" if sys.platform == "darwin" else "off"


class _Unclear(ValueError):
    """A `cfg(...)` expression Peeky can't evaluate."""


_CFG_TOKEN = re.compile(r'\s*(?:([A-Za-z_][A-Za-z0-9_]*)|("(?:[^"\\]|\\.)*")|([(),=]))')


def cfg_applies(key, cfgs):
    """Whether a `[target.'cfg(...)']` key holds for a target with ``cfgs`` (`rustc --print
    cfg`'s lines: `unix`, `target_os="linux"`): `all`, `any`, `not`, the literals `true`
    and `false`, names and `name = "value"`. _Unclear where Peeky can't evaluate it."""
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

    def following(token):
        return position < len(tokens) and tokens[position] == token

    def predicate():
        name = take()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise _Unclear(key)
        if name in ("all", "any", "not") and following("("):
            take("(")
            values = []
            while not following(")"):
                values.append(predicate())
                if following(","):
                    take(",")
                elif not following(")"):
                    raise _Unclear(key)
            take(")")
            if name == "not":
                if len(values) != 1:
                    raise _Unclear(key)
                return not values[0]
            return all(values) if name == "all" else any(values)
        if following("="):
            take("=")
            value = take()
            if not value.startswith('"') or name in ("true", "false"):
                raise _Unclear(key)
            return f"{name}={value}" in known
        if name in ("true", "false"):
            # Literals, as Cargo reads them (`cfg(true)`), never a name.
            return name == "true"
        return name in known

    result = predicate()
    if position != len(tokens):
        raise _Unclear(key)
    return result


def _unclear(path, key):
    return ValueError(
        f"Peeky can't tell whether `target.'{key}'` in {path} applies to this host, so it "
        "doesn't build with it. Take it out of the file, or write it with all(), any(), not(), "
        'true, false, names and name = "value"'
    )


def _target_tables(table, host, cfgs, path):
    """[(key, table)] of a configuration's `[target]` tables that apply to this host: its
    triple's, and each `cfg(...)` that holds for ``cfgs``."""
    found = []
    targets = table.get("target")
    if not isinstance(targets, dict):
        return found
    for key, value in targets.items():
        if key.startswith("cfg("):
            try:
                applies = cfg_applies(key, cfgs)
            except _Unclear:
                raise _unclear(path, key) from None
        else:
            applies = key == host
        if applies:
            found.append((key, value))
    return found


def _flags(value):
    """A configuration's `rustflags` as a list (a string is split at spaces), or None."""
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list) and all(isinstance(each, str) for each in value):
        return list(value)
    return None


def _not_cfg(flags):
    """The first of ``flags`` that isn't part of a `--cfg` or `--check-cfg` flag, or None
    where all are."""
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag in ("--cfg", "--check-cfg") and index + 1 < len(flags):
            index += 2
        elif flag.startswith(("--cfg=", "--check-cfg=")):
            index += 1
        else:
            return flag
    return None


def _rustflags_refusal(path, key, flag):
    return ValueError(
        f"Peeky doesn't build with `{key}` in {path}: Peeky builds with its own flags, and "
        f"keeps only a project's `--cfg` and `--check-cfg` ones (this has `{flag}`). Take it "
        "out of the file, or build without it"
    )


def _checked_flags(path, key, value):
    flags = _flags(value)
    if flags is None:
        raise ValueError(f"`{key}` in {path} is not a string or a list of strings")
    if (flag := _not_cfg(flags)) is not None:
        raise _rustflags_refusal(path, key, flag)
    return flags


def check_config_table(path, table, host, cfgs):
    """Refuse a configuration file's table where it sets what Peeky doesn't account for
    (_CONFIG_ANY, _CONFIG_BUILD, _CONFIG_HOST_TARGET). Its `rustflags` are
    project_rustflags's to choose and check, as Cargo chooses them."""
    refused = []
    for key, value in table.items():
        if key in _CONFIG_ANY or key == "target":
            continue
        if key == "build" and isinstance(value, dict):
            refused += [f"build.{name}" for name in value if name not in _CONFIG_BUILD]
        else:
            refused.append(key)
    for key, value in _target_tables(table, host, cfgs, path):
        if not isinstance(value, dict):
            refused.append(f"target.{key}")
            continue
        refused += [f"target.{key}.{name}" for name in value if name not in _CONFIG_HOST_TARGET]
    if refused:
        raise ValueError(
            f"Peeky doesn't build with {', '.join(f'`{each}`' for each in refused[:5])} in "
            f"{path}: it changes how Cargo builds, and Peeky would not account for it. Take it "
            "out of the file, or build without it"
        )


def check_cargo_config(path, host, cfgs, *, follow=False):
    """The SHA-256 of a `.cargo/config.toml` (or legacy `.cargo/config`) Cargo reads, the
    workspace's own or one above it, refused as check_config_table refuses it, and as
    project_rustflags refuses the rustflags it would choose were it the only one. ``host``:
    this toolchain's host triple, and ``cfgs`` its `rustc --print cfg`, which say whether
    a target table applies. ``follow``: a link is read as Cargo reads it (a file above the
    root, such as a dotfile manager's `~/.cargo/config.toml`)."""
    table, digest = _read_toml(path, MAX_MANIFEST_BYTES, follow=follow, digest=True)
    check_config_table(path, table, host, cfgs)
    project_rustflags([(path, table)], host, lambda flags: cfgs)
    return digest


def project_rustflags(tables, host, print_cfg, own=()):
    """(the project's `--cfg` and `--check-cfg` rustflags, the host's cfgs with them): as
    Cargo chooses rustflags from ``tables`` ([(path, table)], every configuration file it
    reads): every applicable target table's (`target.<triple>`, `target.'cfg(…)'`) joined,
    where any has `rustflags`, else `build.rustflags`; only the chosen ones are checked.
    Peeky builds with its own flags (``own``) and these alone. ``print_cfg(flags)``:
    `rustc --print cfg` with those flags. Refused: any other flag; a chosen key set in
    more than one file, which Cargo merges and Peeky doesn't; and target tables whose own
    `--cfg` flags change which of them apply."""

    def once(found, key):
        if len(found) > 1:
            raise ValueError(
                f"Peeky doesn't build with `{key}` set in {found[0]} and {found[1]}: Cargo merges "
                "rustflags across configuration files, and Peeky doesn't. Set them in one file"
            )

    def collect(cfgs):
        targets, build = {}, []
        for path, table in tables:
            for key, value in _target_tables(table, host, cfgs, path):
                if isinstance(value, dict) and "rustflags" in value:
                    targets.setdefault(key, []).append((path, value["rustflags"]))
            section = table.get("build")
            if isinstance(section, dict) and "rustflags" in section:
                build.append((path, section["rustflags"]))
        flags, applied = [], []
        if targets:
            for key, found in targets.items():
                once([path for path, _ in found], f"target.{key}.rustflags")
                path, value = found[0]
                flags += _checked_flags(path, f"target.{key}.rustflags", value)
                applied.append((str(path), key))
        elif build:
            once([path for path, _ in build], "build.rustflags")
            flags = _checked_flags(build[0][0], "build.rustflags", build[0][1])
        return tuple(flags), applied

    cfgs = tuple(print_cfg(tuple(own)))
    flags, applied = collect(cfgs)
    if flags:
        cfgs = tuple(print_cfg((*own, *flags)))
        if collect(cfgs) != (flags, applied):
            raise ValueError(
                "Peeky can't tell which of Cargo's [target.'cfg(...)'] tables apply: the "
                f"`--cfg` rustflags of {', '.join(f'`{key}` in {path}' for path, key in applied[:3])} "
                "change the cfgs that choose them. Take those flags out, or build without them"
            )
    return flags, cfgs


def config_file(directory):
    """The configuration file Cargo reads in ``directory``'s `.cargo`, or None: the
    legacy `config` where both it and `config.toml` exist, as Cargo does."""
    cargo = Path(directory) / ".cargo"
    for name in ("config", "config.toml"):
        path = cargo / name
        if path.exists() or path.is_symlink():
            return path
    return None


def config_paths(workspace):
    """The configuration files Cargo reads for a build in ``workspace``: the workspace's
    own, then one in each directory above it (config_file). Peeky's own CARGO_HOME holds
    none (cargo_environment)."""
    return [
        path
        for directory in (Path(workspace), *Path(workspace).parents)
        if (path := config_file(directory)) is not None
    ]


def _read_bytes(path, limit, *, follow=False, seen=None):
    """A regular file's bytes; with ``seen`` (a dict), its identity as those bytes are
    (identity's form: kind and location as found before reading, and their SHA-256) goes
    in it, so what Peeky checked is what it later compares."""
    path = Path(path)
    link = path.is_symlink()
    if (link and not follow) or not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"{path} must be a regular file of at most {limit // 1024 // 1024} MiB")
    resolved = str(path.resolve(strict=True))
    data = path.read_bytes()
    if len(data) > limit:
        raise ValueError(f"{path} must be a regular file of at most {limit // 1024 // 1024} MiB")
    if seen is not None:
        seen[str(path)] = ["link" if link else "file", resolved, hashlib.sha256(data).hexdigest()]
    return data


def _read_toml(path, limit, *, follow=False, digest=False, seen=None):
    """A TOML file's table (and the SHA-256 of the bytes parsed, with ``digest``; their
    identity in ``seen``, as _read_bytes)."""
    data = _read_bytes(path, limit, follow=follow, seen=seen)
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


def read_lock(path, seen=None):
    """Every package of a Cargo.lock (version 3 or 4). Refused: a git dependency, another
    registry than crates.io, a registry package without a checksum, `[[patch.unused]]`.
    ``seen``: as _read_bytes."""
    data = _read_toml(path, MAX_LOCK_BYTES, seen=seen)
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
        parsed = {}
        package = _read_toml(manifest, MAX_MANIFEST_BYTES, seen=parsed).get("package")
        name = package.get("name") if isinstance(package, dict) else None
        version = package.get("version") if isinstance(package, dict) else None
        if (name, version) not in registry:
            continue
        label = f"`{name} {version}` ({vendor}/{entry.name})"
        if sums.is_symlink() or not sums.is_file() or sums.stat().st_size > MAX_CHECKSUMS_BYTES:
            raise _vendor_refusal(f"{label} has no .cargo-checksum.json", workspace, vendor)
        try:
            data = _read_bytes(sums, MAX_CHECKSUMS_BYTES, seen=parsed)
            checksums = json.loads(data.decode("utf-8"))
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
        if found.get("Cargo.toml") != parsed[str(manifest)][2]:
            raise ValueError(
                f"Provenance: vendored {label} changed while Peeky checked it. Build again"
            )
        copies[(name, version)] = f"{vendor}/{entry.name}"
        files[f"{vendor}/{entry.name}/.cargo-checksum.json"] = parsed[str(sums)][2]
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


def manifest_above(workspace):
    """Refuse a manifest above the workspace: Cargo could take the workspace for a member
    of another."""
    for parent in Path(workspace).parents:
        manifest = parent / "Cargo.toml"
        if manifest.exists() or manifest.is_symlink():
            raise ValueError(
                f"Peeky builds a Rust workspace only where Cargo reads no manifest above it, and "
                f"{manifest} is above {workspace}. Move it, or the workspace"
            )


def check_configs_above(configs):
    """Refuse a build whose Cargo configuration above the workspace changed since it was
    read (``configs``: Preflight.configs_above), by path."""
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


def identity(path, digest=None):
    """A file as Cargo would read it: ["file" or "link", where it resolves to, its
    SHA-256], ["missing"], or ["other", where it resolves to]."""
    digest = digest or sha256_file
    path = Path(path)
    try:
        info = path.lstat()
    except OSError:
        return ["missing"]
    kind = "link" if path.is_symlink() else "file"
    try:
        resolved = str(path.resolve(strict=True))
        if not Path(resolved).is_file():
            return ["other", resolved]
        return [kind, resolved, digest(path)]
    except OSError:
        return ["other", str(info.st_mode)]


_GLOB = re.compile(r"[*?\[]")


def _excluded(workspace, manifest, members, exclude):
    """Whether Cargo leaves ``manifest`` out of the workspace: it is under an `exclude`
    path, and under no `members` entry taken as a path (as Cargo's `is_excluded`, which
    compares path components and expands no glob)."""

    parts = Path(os.path.normpath(manifest)).parts

    def under(entry):
        return parts[: len(Path(workspace, entry).parts)] == Path(workspace, entry).parts

    return any(under(each) for each in exclude) and not any(under(each) for each in members)


def member_candidates(workspace, patterns, exclude=()):
    """The manifests the root manifest's `[workspace] members` patterns can name (each
    match's `Cargo.toml`, wherever it is, hidden and `target` directories included), less
    those its `exclude` leaves out, as a sorted list of paths: what Cargo could discover,
    rescanned after it ran."""
    found = set()
    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        current = [Path(workspace)]
        for part in pattern.replace("\\", "/").split("/"):
            if part in ("", "."):
                continue
            if part == "**":
                deeper = []
                for base in current:
                    for directory, folders, _ in os.walk(base):
                        deeper.append(Path(directory))
                        if len(deeper) > MAX_PACKAGES * 16:
                            raise ValueError(
                                f"Workspace member pattern `{pattern}` matches more directories "
                                "than Peeky checks"
                            )
                current = deeper
            elif _GLOB.search(part):
                current = [
                    base / name
                    for base in current
                    if base.is_dir()
                    for name in sorted(os.listdir(base))
                    if fnmatch.fnmatchcase(name, part)
                ]
            else:
                current = [base / part for base in current]
            if len(current) > MAX_PACKAGES:
                raise ValueError(f"Workspace member pattern `{pattern}` matches too many paths")
        found.update(
            str(path / "Cargo.toml")
            for path in current
            if ((path / "Cargo.toml").exists() or (path / "Cargo.toml").is_symlink())
            and not _excluded(workspace, path / "Cargo.toml", patterns, exclude)
        )
    return sorted(found)


def _strings(section, key):
    value = section.get(key, []) if isinstance(section, dict) else []
    return [each for each in value if isinstance(each, str)] if isinstance(value, list) else []


def members_now(state):
    """The manifests the workspace's member patterns name now (member_candidates), for
    ``state`` (watch's)."""
    return member_candidates(state["workspace"], state["patterns"], state.get("exclude", ()))


def watch(workspace, configs, manifest, vendor, copies, files, seen, digest=None):
    """What Peeky checks again before and once Cargo has run (recheck): the configuration
    files Cargo reads (``configs``, the paths Peeky read), the lockfile, the root
    manifest (``manifest``, as parsed) and every manifest its member patterns can name,
    each by its identity (kind, resolved location, SHA-256), and the vendored copies
    ({path relative to the workspace: SHA-256}, rechecked file by file). What Peeky
    checked has the identity of the bytes it parsed (``seen``, _read_bytes's); only the
    member manifests Cargo reads first are read here."""
    workspace = Path(workspace)
    section = manifest.get("workspace") if isinstance(manifest.get("workspace"), dict) else {}
    patterns, exclude = _strings(section, "members"), _strings(section, "exclude")
    candidates = member_candidates(workspace, patterns, exclude)
    paths = [str(path) for path in configs]
    tracked = [*paths, str(workspace / "Cargo.lock"), str(workspace / "Cargo.toml"), *candidates]
    return {
        "workspace": str(workspace),
        "configs": paths,
        "patterns": patterns,
        "exclude": exclude,
        "candidates": candidates,
        "files": {
            path: seen[path] if path in seen else identity(path, digest)
            for path in dict.fromkeys(tracked)
        },
        "vendor": vendor if copies else None,
        "vendor_directory": str(workspace / vendor) if copies else None,
        "vendored": dict(files),
    }


def watched(state, paths, digest=None):
    """``state`` (watch's) with ``paths`` (manifests Cargo's metadata or unit graph named)
    watched too."""
    files = dict(state["files"])
    for path in paths:
        files.setdefault(str(path), identity(path, digest))
    return {**state, "files": files}


def recheck(state, digest=None):
    """What changed since ``state`` (watch's) was taken, as a refusal names it, or None:
    a configuration file Cargo reads (one more, one fewer, or other bytes, compared before
    any is read), a watched file (other bytes, kind or location), a manifest a member
    pattern now names, or a vendored copy (rechecked file by file)."""
    workspace = Path(state["workspace"])
    configs = [str(path) for path in config_paths(workspace)]
    if configs != state["configs"]:
        added = sorted(set(configs) - set(state["configs"]))
        gone = sorted(set(state["configs"]) - set(configs))
        return f"Cargo's configuration {', '.join(added or gone)} {'appeared' if added else 'went'}"
    for path, recorded in state["files"].items():
        if identity(path, digest) != recorded:
            return f"{path} changed"
    candidates = members_now(state)
    if candidates != state["candidates"]:
        added = sorted(set(candidates) - set(state["candidates"]))
        return f"the workspace's members changed ({', '.join(added) or 'one went'})"
    if state["vendor"] is not None:
        if str((workspace / state["vendor"]).resolve()) != state["vendor_directory"]:
            return f"the vendored copies' directory {state['vendor']} changed"
        for path, recorded in state["vendored"].items():
            if identity(workspace / path, digest) != [
                "file",
                str((workspace / path).resolve()),
                recorded,
            ]:
                return f"vendored {path} changed"
        try:
            _, files = vendored(
                workspace, state["vendor"], read_lock(workspace / "Cargo.lock"), digest
            )
        except ValueError as exc:
            return f"the vendored copies changed ({str(exc).removeprefix('Provenance: ')})"
        if files != state["vendored"]:
            return f"the vendored copies changed ({sorted(set(files) ^ set(state['vendored']))[0]})"
    return None


@dataclass(frozen=True)
class Preflight:
    """What `preflight` read: the workspace (resolved), Cargo.lock's packages, the vendored
    copies ({(name, version): directory relative to the workspace}), the workspace's own
    configuration file (relative to the root) and those above it ((path, SHA-256), ...),
    the project's `--cfg` and `--check-cfg` rustflags Peeky keeps, the host's triple and
    cfgs with them, and what Peeky checks again after Cargo ran (``watch``)."""

    workspace: Path
    locked: tuple[Locked, ...]
    vendored: dict
    cargo_config: tuple[str, ...]
    configs_above: tuple[tuple[str, str], ...] = ()
    rustflags: tuple[str, ...] = ()
    cfgs: tuple[str, ...] = ()
    host: str = ""
    watch: dict | None = None


def preflight(root, config, host, print_cfg, own=(), digest=None):
    """Check what Cargo will read before it runs (Preflight): the configuration in and above
    the workspace (project_rustflags, check_config_table; a manifest above it refused), its
    manifest (no `[patch]` or `[replace]`), its lockfile (read_lock) and the vendored
    copies (vendored); and take the watch that ``recheck`` compares after Cargo ran.
    ``print_cfg(flags)``: `rustc --print cfg` with ``flags`` (``own``: Peeky's, then the
    project's `--cfg` ones)."""
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
    manifest_above(workspace)
    if (workspace / ".cargo").is_symlink():
        raise ValueError(f"Cargo's configuration directory can't be a link: {workspace / '.cargo'}")
    tables, seen, configs = [], {}, config_paths(workspace)
    for path in configs:
        own_file = path.parent.parent == workspace
        table, sha = _read_toml(
            path, MAX_MANIFEST_BYTES, follow=not own_file, digest=True, seen=seen
        )
        tables.append((path, table, sha, own_file))
    rustflags, cfgs = project_rustflags(
        [(path, table) for path, table, _, _ in tables], host, print_cfg, own
    )
    for path, table, _, _ in tables:
        check_config_table(path, table, host, cfgs)
    relative = workspace.relative_to(root)
    files = [
        (relative / ".cargo" / path.name).as_posix() for path, _, _, own_file in tables if own_file
    ]
    above = tuple((str(path), sha) for path, _, sha, own_file in tables if not own_file)
    manifest = _read_toml(workspace / "Cargo.toml", MAX_MANIFEST_BYTES, seen=seen)
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
    locked = read_lock(lock, seen)
    copies, vendored_files = vendored(workspace, config.vendor, locked, digest)
    state = watch(workspace, configs, manifest, config.vendor, copies, vendored_files, seen, digest)
    return Preflight(
        workspace,
        locked,
        copies,
        tuple(files),
        above,
        tuple(rustflags),
        tuple(cfgs),
        host,
        state,
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
    alone); ``edition`` empty for a package that isn't (Cargo's unit graph names its
    targets' editions)."""

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
    root}), the configuration above the workspace ((path, SHA-256), ...), the project's
    `--cfg` and `--check-cfg` rustflags Peeky keeps, and the ``watch`` that ``recheck``
    compares again after Cargo runs."""

    root: Path
    workspace: Path
    local: tuple[Package, ...]
    analysed: tuple[Package, ...]
    run: tuple[Package, Target]
    inputs: tuple[str, ...]
    vendored: dict
    features: tuple[str, ...] = ()
    configs_above: tuple[tuple[str, str], ...] = ()
    rustflags: tuple[str, ...] = ()
    host: str = ""
    cfgs: tuple[str, ...] = ()
    watch: dict | None = None

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


def check_profile(package, unit, split=SPLIT_DEBUGINFO):
    """Refuse an analysed package's compile, as Cargo's unit graph gives its profile, that
    Peeky's proofs don't hold for: optimized, without full debug information, with
    link-time optimization, stripped, with another split-debuginfo than ``split`` (what
    Peeky's symbol reading needs), or aborting on a panic (its proofs are qualified with
    unwinding). A setting for one package (`[profile.dev.package.x]`) overrides Peeky's
    environment, so it is checked here; rustc's recorded command is checked again as the
    build runs."""
    profile = unit["profile"]
    strip = profile.get("strip")
    stripped = strip.get("resolved", strip.get("deferred")) if isinstance(strip, dict) else strip
    problems = []
    if profile["opt_level"] != "0":
        problems.append(f"opt-level {profile['opt_level']}")
    if profile["debuginfo"] not in (2, "2", "full"):
        problems.append(f"debug {profile['debuginfo']}")
    if profile["lto"] not in ("false", "off"):
        problems.append(f"lto {profile['lto']}")
    if stripped not in ("None", None):
        problems.append(f"strip {stripped}")
    if profile.get("split_debuginfo") != split:
        problems.append(f"split-debuginfo {profile.get('split_debuginfo')}")
    if profile["panic"] != "unwind":
        problems.append(f"panic {profile['panic']}")
    if problems:
        raise ValueError(
            f"Peeky analyses only code built at opt-level 0, with full debug information "
            f"(split-debuginfo {split}), no link-time optimization or stripping, and unwinding "
            f"panics: `{package}`'s {unit['target']['name']} has {', '.join(problems)}, from a "
            f"[profile] setting for it (such as [profile.dev.package.{package}] in Cargo.toml). "
            "Remove that setting"
        )


def _not_read(what, cargo_version, kind="unit graph"):
    return ValueError(
        f"Cargo's {kind} is not one Peeky reads: {cargo_version or 'this Cargo'} wrote {what}. "
        f"Peeky reads it as Cargo 1.96 writes it{' (version 1)' if kind == 'unit graph' else ''}, "
        "and doesn't guess at another; build with Rust 1.96"
    )


def _typed(value, kind):
    """``value`` is of ``kind`` (a type or tuple of types), never a bool for an int."""
    if isinstance(value, bool) and bool not in (kind if isinstance(kind, tuple) else (kind,)):
        return False
    return isinstance(value, kind)


def _fields(value, fields, where, refuse):
    if not isinstance(value, dict):
        raise refuse(f"{where or 'an entry'} that isn't an object")
    for name, kind in fields.items():
        if not _typed(value.get(name), kind):
            raise refuse(f"an entry without `{where}{name}` as Peeky reads it")


_PACKAGE_ID = re.compile(
    r"(?P<source>path\+file://|registry\+)(?P<location>[^#\s]+)"
    r"#(?:(?P<name>[A-Za-z0-9_-]+)@)?(?P<version>[0-9][0-9A-Za-z.+-]*)"
)


def package_id(text):
    """(None for a path package, else its source; its directory, or None; name; version)
    of a Cargo package ID (`path+file:///w/util#0.1.0`,
    `registry+https://…#smallnum@1.0.0`), or None where it is no ID Peeky reads."""
    found = _PACKAGE_ID.fullmatch(text) if isinstance(text, str) else None
    if not found:
        return None
    if found["source"] == "path+file://":
        directory = urllib.parse.unquote(found["location"])
        if not directory.startswith("/"):
            return None
        return None, directory, found["name"] or Path(directory).name, found["version"]
    if not found["name"]:
        return None
    return "registry+" + found["location"], None, found["name"], found["version"]


_METADATA_PACKAGE = {"id": str, "name": str, "version": str, "manifest_path": str, "edition": str}
_TARGET = {"name": str, "kind": list, "src_path": str, "edition": str}
_UNIT = {"pkg_id": str, "target": dict, "profile": dict, "mode": str, "features": list}
_UNIT_PROFILE = {
    "opt_level": str,
    "debuginfo": (int, str),
    "lto": str,
    "panic": str,
    "split_debuginfo": (str, type(None)),
    "strip": (dict, str),
}


def check_metadata(metadata, cargo_version):
    """Refuse `cargo metadata --no-deps` output without every field Peeky reads, of its type."""

    def refuse(what):
        return _not_read(what, cargo_version, "metadata")

    _fields(
        metadata, {"workspace_root": str, "workspace_members": list, "packages": list}, "", refuse
    )
    if not all(isinstance(each, str) for each in metadata["workspace_members"]):
        raise refuse("members that aren't package IDs")
    for package in metadata["packages"]:
        _fields(package, {**_METADATA_PACKAGE, "targets": list}, "packages.", refuse)
        for target in package["targets"]:
            _fields(target, _TARGET, "targets.", refuse)
            if not all(isinstance(kind, str) for kind in target["kind"]):
                raise refuse("an entry without `targets.kind` as Peeky reads it")
        dependencies = package.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(each, dict) and isinstance(each.get("path", ""), str)
            for each in dependencies
        ):
            raise refuse("an entry without `packages.dependencies` as Peeky reads it")


def check_graph(graph, cargo_version):
    """Refuse a unit graph that isn't version 1 with every field Peeky reads, of the type it
    reads (an int never a bool), each package ID one Peeky reads, and each root and
    dependency a unit's index: Peeky never guesses at another. ``cargo_version``: `cargo
    --version`, named."""

    def refuse(what):
        return _not_read(what, cargo_version)

    if not isinstance(graph, dict):
        raise refuse("something other than an object")
    if not _typed(graph.get("version"), int) or graph["version"] != 1:
        raise refuse(f"version {graph.get('version')!r}")
    units, roots = graph.get("units"), graph.get("roots")
    if not isinstance(units, list) or not isinstance(roots, list):
        raise refuse("one without `units` or `roots`")
    if not all(_typed(each, int) and 0 <= each < len(units) for each in roots):
        raise refuse("roots that name no unit")
    for unit in units:
        _fields(unit, _UNIT, "", refuse)
        _fields(unit["target"], _TARGET, "target.", refuse)
        _fields(unit["profile"], _UNIT_PROFILE, "profile.", refuse)
        if not all(isinstance(kind, str) for kind in unit["target"]["kind"]):
            raise refuse("a unit without `target.kind` as Peeky reads it")
        if package_id(unit["pkg_id"]) is None:
            raise refuse(f"a package ID Peeky doesn't read ({str(unit['pkg_id'])[:120]})")
        dependencies = unit.get("dependencies")
        if not isinstance(dependencies, list) or not all(
            isinstance(each, dict)
            and _typed(each.get("index"), int)
            and 0 <= each["index"] < len(units)
            for each in dependencies
        ):
            raise refuse("a unit whose dependencies aren't units' indices")


def read_workspace(root, config, preflight, metadata, graph, cargo_version=""):
    """The WorkspaceShape of ``config``'s workspace from Cargo's ``metadata`` (`cargo
    metadata --no-deps --format-version 1`, as parsed JSON: the members) and ``graph``:
    Cargo's unit graph of the exact command the build runs (its packages, features,
    targets and profiles). Every path package that command compiles, and every member,
    must be inside ``root``. Each analysed package must be a member of edition 2021 or
    2024, compiled by that command at opt-level 0 (check_profile), and no procedural
    macro; the run's target is the command's root. ``cargo_version`` names Cargo where
    its output is not what Peeky reads (check_metadata, check_graph)."""
    root = Path(root).resolve(strict=True)
    workspace = preflight.workspace
    check_metadata(metadata, cargo_version)
    check_graph(graph, cargo_version)
    if Path(metadata["workspace_root"]).resolve() != workspace:
        raise ValueError(
            f"Cargo reads {metadata['workspace_root']} as the workspace, not {workspace}"
        )
    members = {
        package["id"]: package
        for package in metadata["packages"]
        if package["id"] in set(metadata["workspace_members"])
    }
    locked = {(each.name, each.version) for each in preflight.locked if each.source}
    units = graph["units"]
    compiled, registry = {}, set()
    for unit in units:
        source, directory, name, version = package_id(unit["pkg_id"])
        if source is not None:
            if (name, version) not in locked:
                raise ValueError(
                    f"Cargo's unit graph names `{name} {version}`, which Cargo.lock doesn't"
                )
            registry.add(name)
            continue
        if not Path(directory).resolve().is_relative_to(root):
            raise ValueError(
                f"Peeky doesn't build a path dependency outside {root}: `{name}` is at "
                f"{directory} (the build compiles it), and Peeky compares only files inside "
                "the root. Move it into the workspace"
            )
        source_path = Path(unit["target"]["src_path"]).resolve()
        if not source_path.is_relative_to(root):
            raise ValueError(
                f"Peeky builds only files inside {root}: target `{unit['target']['name']}` of "
                f"`{name}` is {source_path}. Move it into the workspace"
            )
        compiled.setdefault(unit["pkg_id"], []).append(unit)
    local = {}
    for package_key, package in members.items():
        manifest = Path(package["manifest_path"]).resolve()
        if not manifest.parent.is_relative_to(root):
            raise ValueError(
                f"Peeky doesn't build a path dependency outside {root}: `{package['name']}` is "
                f"at {manifest.parent} (it is a member), and Peeky compares only files inside "
                "the root. Move it into the workspace"
            )
        targets = tuple(
            Target(
                each["name"],
                tuple(each["kind"]),
                Path(each["src_path"]).resolve().relative_to(root).as_posix(),
                each["edition"],
            )
            for each in package["targets"]
            if Path(each["src_path"]).resolve().is_relative_to(root)
        )
        local[package_key] = Package(
            package["name"],
            package["version"],
            manifest.parent.relative_to(root).as_posix(),
            package["edition"],
            targets,
            True,
        )
    for package_key, own in compiled.items():
        if package_key in local:
            continue
        _, directory, name, version = package_id(package_key)
        targets = {
            (unit["target"]["name"], tuple(unit["target"]["kind"])): Target(
                unit["target"]["name"],
                tuple(unit["target"]["kind"]),
                Path(unit["target"]["src_path"]).resolve().relative_to(root).as_posix(),
                unit["target"]["edition"],
            )
            for unit in own
        }
        local[package_key] = Package(
            name,
            version,
            Path(directory).resolve().relative_to(root).as_posix(),
            "",
            tuple(targets.values()),
            False,
        )
    names = [package.name for package in local.values()]
    if len(set(names)) != len(names):
        twice = sorted({name for name in names if names.count(name) > 1})
        raise ValueError(f"Peeky can't tell apart two path packages named `{twice[0]}`")
    by_name = {package.name: (package_key, package) for package_key, package in local.items()}
    run = _run_target(config.run, graph, units, local)
    command = "cargo " + " ".join(_selection(run[0].name, run[1], config.features))
    analysed = []
    for name in config.analyse:
        if name not in by_name:
            where = (
                "a registry dependency" if name in registry else "not a package of the workspace"
            )
            raise ValueError(
                f"Peeky analyses only the workspace's own packages: `{name}` is {where}. "
                f"Its packages: {', '.join(sorted(by_name)[:20])}"
            )
        package_key, package = by_name[name]
        if not package.member:
            raise ValueError(
                f"Peeky analyses only the workspace's members, as Cargo runs Peeky's wrapper "
                f"for those alone: `{name}` is a path dependency that isn't one. Add it to "
                "[workspace] members"
            )
        if any("proc-macro" in target.kinds for target in package.targets):
            raise ValueError(
                f"Peeky analyses only code the program runs: `{name}` is a procedural macro, "
                "which runs in the compiler. Analyse the packages that use it"
            )
        own = [
            unit
            for unit in compiled.get(package_key, ())
            if unit["mode"] in ("build", "test")
            and not {"custom-build", "proc-macro"} & set(unit["target"]["kind"])
        ]
        if not own:
            raise ValueError(
                f"Peeky analyses only packages the run compiles: `{command}` doesn't compile "
                f'`{name}`. Analyse a package it uses, or change "run" or "features"'
            )
        editions = {package.edition, *(unit["target"]["edition"] for unit in own)}
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
        preflight.rustflags,
        preflight.host,
        preflight.cfgs,
        preflight.watch,
    )


def _run_target(run, graph, units, local):
    """(package, target) of the unit graph's root, which must be what ``run`` names."""
    roots = graph["roots"]
    if len(roots) != 1:
        raise ValueError(
            f"Cargo's unit graph has {len(roots)} roots for this run, where Peeky selects one "
            "target"
        )
    unit = units[roots[0]]
    target = unit["target"]
    kinds = tuple(target["kind"])
    wanted = {"bin": "bin", "test": "test"}.get(run.kind)
    package = local.get(unit["pkg_id"])
    if (
        package is None
        or (run.package is not None and package.name != run.package)
        or (wanted is not None and (wanted not in kinds or target["name"] != run.name))
        or (wanted is None and not {"lib", "rlib", "dylib"} & set(kinds))
    ):
        raise ValueError(
            f"Cargo would build `{package_id(unit['pkg_id'])[2]}`'s {target['name']} for this "
            f'run, not what "run" names: {_RUN_SHAPE}'
        )
    found = next(
        (each for each in package.targets if each.name == target["name"] and each.kinds == kinds),
        None,
    )
    if found is None:
        raise ValueError(
            f"Cargo's metadata doesn't name `{package.name}`'s {target['name']}, which its unit "
            "graph builds for this run"
        )
    return package, found


def _run_package(run, metadata):
    """The package Cargo's selection (`-p`) names for ``run``: its own, or the one member
    with that target; refused where none, or several, has it."""
    members = set(metadata.get("workspace_members") or ())
    packages = [
        entry
        for entry in metadata.get("packages") or ()
        if isinstance(entry, dict) and entry.get("id") in members
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
        f'Several packages have {what} ({", ".join(found)}): add "package" to "run", such as '
        f'{{"package": "{found[0]}", "{run.kind}": "{run.name}"}}'
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


def build_environment(rustc, cargo_home, target_dir, rustflags=()):
    """cargo_environment, plus what defines Peeky's build of a workspace: ``rustflags``
    for every compile (CARGO_ENCODED_RUSTFLAGS: Peeky's own, then the project's `--cfg`
    and `--check-cfg` ones, so no other configuration's `rustflags` apply), Peeky's own
    target directory (``target_dir``, whatever `build.target-dir` says), no incremental
    compilation, and the dev and test profiles' settings Peeky relies on (_PROFILE,
    SPLIT_DEBUGINFO), whatever Cargo.toml or Cargo's configuration says of the profile.
    A setting for one package overrides these; check_profile refuses such a one for an
    analysed package."""
    env = cargo_environment(rustc, cargo_home)
    env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(rustflags)
    env["CARGO_TARGET_DIR"] = str(target_dir)
    env["CARGO_INCREMENTAL"] = "0"
    for profile in ("DEV", "TEST"):
        for key, value in {**_PROFILE, "SPLIT_DEBUGINFO": SPLIT_DEBUGINFO}.items():
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


def cargo_version(cargo, env, timeout=30):
    """`cargo --version`'s line."""
    output = _cargo((str(cargo), "--version"), Path(env["CARGO_HOME"]), env, timeout)
    return next((line.strip() for line in split_lines(output) if line.strip()), "")


def host_triple(rustc, env, timeout=30):
    """The host triple of ``rustc`` (`rustc -vV`)."""
    output = _cargo((str(rustc), "-vV"), Path(env["CARGO_HOME"]), env, timeout)
    found = re.search(r"^host: (\S+)$", output, re.M)
    if not found:
        raise ValueError(f"{rustc} -vV names no host")
    return found[1]


def host_cfgs(rustc, env, flags=(), timeout=30):
    """The host target's `cfg` values with ``flags`` (`rustc --print cfg`), which say whether
    a target table of Cargo's configuration applies."""
    output = _cargo((str(rustc), "--print", "cfg", *flags), Path(env["CARGO_HOME"]), env, timeout)
    return tuple(line.strip() for line in split_lines(output) if line.strip())


def changed(state, digest=None, when="while Peeky read the workspace and Cargo ran"):
    """Refuse, as PROVENANCE_CHANGED, what changed since ``state`` (recheck)."""
    if (what := recheck(state, digest)) is not None:
        raise ValueError(f"Provenance: {what} {when}. Build again")


def _json(output, what, cargo_version):
    try:
        return json.loads(output)
    except (ValueError, RecursionError):
        raise _not_read("output that isn't JSON", cargo_version, what) from None


def read(root, config, cargo, rustc, cargo_home, target_dir, timeout=60, rustflags=(), digest=None):
    """The WorkspaceShape of ``config``'s workspace under ``root``, read in the build's own
    environment (build_environment: Peeky's ``rustflags``, then the project's `--cfg` ones;
    Peeky's ``target_dir``): preflight; `cargo metadata --no-deps` (the members, which
    needs no dependency resolved); Cargo's unit graph of the exact command the build runs
    (`--unit-graph`, an unstable flag that RUSTC_BOOTSTRAP allows for that one command,
    which compiles nothing); read_workspace. What preflight checked is checked again just
    before Cargo starts. Then every manifest Cargo's metadata and unit graph name joins
    the watch, both are taken again and must be identical, and everything is checked
    again (recheck)."""
    digest = digest or sha256_file
    own = build_environment(rustc, cargo_home, target_dir, rustflags)
    host, version = host_triple(rustc, own), cargo_version(cargo, own)
    checked = preflight(
        root, config, host, lambda flags: host_cfgs(rustc, own, flags), rustflags, digest
    )
    env = build_environment(rustc, cargo_home, target_dir, (*rustflags, *checked.rustflags))
    extra = vendor_arguments(checked.workspace, config.vendor) if checked.vendored else ()
    common = ("--offline", "--locked", *extra)
    members = [str(cargo), "metadata", "--format-version", "1", "--no-deps", *common]
    changed(checked.watch, digest, "while Peeky checked the workspace")
    first = _cargo(members, checked.workspace, env, timeout)
    metadata = _json(first, "metadata", version)
    check_metadata(metadata, version)
    package = _run_package(config.run, metadata)
    chosen = WorkspaceConfig(
        config.workspace,
        config.analyse,
        Run(config.run.kind, config.run.name, package),
        config.features,
        config.vendor,
    )
    selection = run_selection(chosen.run, config.features)
    units = [str(cargo), *selection, *common, "--unit-graph", "-Z", "unstable-options"]
    output = _cargo(units, checked.workspace, {**env, "RUSTC_BOOTSTRAP": "1"}, timeout)
    shape = read_workspace(
        root, chosen, checked, metadata, _json(output, "unit graph", version), version
    )
    named = [shape.root / each.manifest for each in shape.local]
    for package in metadata["packages"]:
        named.append(Path(package["manifest_path"]))
        named += [
            Path(each["path"], "Cargo.toml")
            for each in package.get("dependencies", ())
            if "path" in each
        ]
    state = watched(checked.watch, named, digest)
    for argv, before, what, bootstrap in (
        (members, first, "metadata of the workspace's members", {}),
        (units, output, "unit graph of the build", {"RUSTC_BOOTSTRAP": "1"}),
    ):
        if _cargo(argv, checked.workspace, {**env, **bootstrap}, timeout) != before:
            raise ValueError(
                f"Provenance: Cargo's {what} changed while Peeky read the workspace and Cargo "
                "ran. Build again"
            )
    changed(state, digest)
    return replace(shape, watch=state)
