"""The sources a worker's target reads, as {path: sha256}: what a remote client and the
worker compare before anything launches.

The worker's trusted target names them: Java's `sources`; C and C++ `sources` and
`headers`; Rust's Cargo.toml, Cargo.lock, `sources` and `tests`; a Rust workspace's build's
manifests, lockfile and configuration, every workspace file it compiled, and what its build
scripts read. A Python program may import any `.py` file under the root, so for Python they are
all of those, except in virtual environments (a directory holding `pyvenv.cfg`: installed
packages). Both sides must hold the same set, file for file.

A Rust workspace's are compared by their structure too (``structured``): each file's kind, the
links on its way and what they say, and each directory a build script watches entry by entry,
so a link where the other side has a file, or an entry on one side only, differs.
"""

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

MAX_PYTHON_INPUTS = 4096
MAX_PYTHON_BYTES = 16 * 1024 * 1024
# What crosses the wire: at most this many sources, each name at most this long.
MAX_SOURCES = MAX_PYTHON_INPUTS
MAX_NAME = 1024
TOO_MANY = (
    f"at most {MAX_PYTHON_INPUTS} .py files and {MAX_PYTHON_BYTES // (1024 * 1024)} MiB in all, "
    "each a readable regular file, and no symlinked directory"
)
_CHUNK = 1024 * 1024


class Unhashable(ValueError):
    """The Python source set cannot be hashed completely: the reason says why."""


def _regular(path):
    """os.stat of ``path`` (following a link), refusing FIFOs, devices and directories."""
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"{path} is not a regular file")
    return info


def _hash(path):
    """The sha256 of a regular file, read in chunks."""
    _regular(path)
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def environment(path):
    """True for a virtual environment: installed packages, not the program's sources."""
    return os.path.isfile(os.path.join(path, "pyvenv.cfg"))


def in_environment(root, relative):
    """True where ``relative`` (under ``root``) is inside a virtual environment there."""
    current = Path(root)
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        if environment(current):
            return True
    return False


def python_sources(root):
    """[(path, sha256)] of the `.py` files under ``root``, in walk order.

    Raises Unhashable where there are too many or too many bytes, where one is not a
    readable regular file, or where a directory under the root is a symlink: following it
    could loop or leave the root, and skipping it would leave code unchecked."""
    root = Path(root)
    found, total = [], 0
    for directory, names, files in os.walk(root):
        kept = []
        for name in sorted(names):
            path = os.path.join(directory, name)
            if environment(path):
                continue
            if os.path.islink(path):
                relative = Path(path).relative_to(root).as_posix()
                raise Unhashable(f"{relative} is a symlinked directory")
            kept.append(name)
        names[:] = kept
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            try:
                size = _regular(path).st_size
            except OSError:
                raise Unhashable(f"{relative} is not a readable regular file") from None
            total += size
            if len(found) >= MAX_PYTHON_INPUTS or total > MAX_PYTHON_BYTES:
                raise Unhashable(
                    f"more than {MAX_PYTHON_INPUTS} .py files, or more than "
                    f"{MAX_PYTHON_BYTES // (1024 * 1024)} MiB in all"
                )
            try:
                found.append((relative, _hash(path)))
            except OSError:
                raise Unhashable(f"{relative} is not a readable regular file") from None
    return found


def python_digest(root):
    """A Python run's inputs_sha256: a digest of python_sources, or None."""
    try:
        found = python_sources(root)
    except Unhashable:
        return None
    return python_manifest_digest(dict(found)) if found else None


def python_manifest_digest(manifest):
    """python_digest of a manifest built from python_sources (its walk order kept)."""
    return hashlib.sha256(json.dumps(list(manifest.items())).encode()).hexdigest()


def declared(adapter, root=None):
    """The names the worker's trusted target reads, or None for a Python program. A Rust
    workspace's are its current build's under ``root`` (``compared_names``)."""
    if (names_for := getattr(adapter, "compared_names", None)) is not None:
        return tuple(dict.fromkeys(str(name) for name in names_for(root)))
    names = getattr(adapter, "verification_inputs", None)
    if names is None:
        names = getattr(adapter, "sources", None)  # an attached JVM's declared sources
    return None if names is None else tuple(dict.fromkeys(str(name) for name in names))


def describe(adapter, root=None):
    """The worker's `version` field that tells a client which sources to hash: for a Rust
    workspace, the directories compared entry by entry too (``trees``: {directory: how},
    and ``skip``), each source by its structure (``structured``)."""
    names = declared(adapter, root)
    if names is None:
        return {"kind": "python"}
    found = {"kind": "declared", "names": list(names)}
    if (trees_for := getattr(adapter, "compared_trees", None)) is not None:
        trees, skip = trees_for(root)
        found.update(structured=True, trees=dict(trees), skip=list(skip))
    return found


# The most entries Peeky lists of the directories a Rust workspace's build scripts watch.
MAX_TREE_ENTRIES = 200_000
_HOW = ("exact", "package")


def _kind(path):
    """(``path``'s kind, not followed: "file", "dir", "link" or "other"; a link's text), or
    None where it is missing or can't be read."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        try:
            return "link", os.readlink(path)
        except OSError:
            return None
    if stat.S_ISREG(info.st_mode):
        return "file", None
    return ("dir" if stat.S_ISDIR(info.st_mode) else "other"), None


def _checkout_metadata(name):
    """A `.git` directory's entry, or a `.git` file: the checkout's, never compared."""
    return ".git" in PurePosixPath(name).parts


def _identity(root, name, content=None):
    """``name``'s identity: its content's SHA-256 for a regular file reached through no
    link (as every target's sources are compared); else the SHA-256 of what it is (the links
    on its way, and what each says; a link's text and its target's SHA-256). None where it is
    missing or not a file. ``content``: the content's SHA-256 as a build recorded it, taken
    instead of the disk's."""
    root, parts, links = Path(root), PurePosixPath(name).parts, []
    for depth in range(1, len(parts)):
        found = _kind(root.joinpath(*parts[:depth]))
        if found is not None and found[0] == "link":
            links.append(["/".join(parts[:depth]), found[1]])
    path = root / name
    found = _kind(path)
    if found is None or found[0] in ("dir", "other"):
        return None
    try:
        content = _hash(path) if content is None else content
    except OSError:
        return None
    if found[0] == "file" and not links:
        return content
    entry = {"links": links, "kind": found[0], "says": found[1], "sha256": content}
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()


def _tree(root, top, how, skip):
    """The SHA-256 of what the directory ``top`` holds, as the build scripts' watch reads
    it: its own kind (and a link's text), and each entry under it by its path, kind, and a
    link's text and its file's SHA-256 (files' contents are compared by name). "exact": every
    entry, hidden and
    `target` ones too, through links to directories under the root, as Cargo scans a
    `rerun-if-changed` directory. "package": as Cargo lists a package's files, with no
    hidden or `target` directory, no other package's, and none of ``skip``. Never a `.git`
    entry."""
    root = Path(root).resolve()
    start = root / top
    entries, seen = [], set()
    pending = [(start, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            real = directory.resolve(strict=True)
        except OSError:
            continue
        if real in seen or not real.is_dir():
            continue
        seen.add(real)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            entries.append([prefix or ".", "unreadable", None])
            continue
        for name in names:
            relative, path = prefix + name, directory / name
            if _checkout_metadata(relative):
                continue
            kind, says = _kind(path) or ("missing", None)
            if how == "package" and kind in ("dir", "link") and path.is_dir():
                place = (Path(top) / relative).as_posix()
                if (
                    name.startswith(".")
                    or name == "target"
                    or place in skip
                    or (path / "Cargo.toml").exists()
                ):
                    continue
            if kind == "link" and path.is_file():
                try:
                    says = [says, _hash(path)]
                except OSError:
                    says = [says, None]
            entries.append([relative, kind, says])
            if len(entries) > MAX_TREE_ENTRIES:
                raise ValueError(
                    f"{top} holds more than {MAX_TREE_ENTRIES} entries, more than Peeky compares "
                    "with a remote client's: run Peeky on the worker's host"
                )
            if kind == "dir" or (
                kind == "link"
                and path.is_dir()
                and (how == "exact" and path.resolve().is_relative_to(root))
            ):
                pending.append((path, relative + "/"))
    found = {"root": list(_kind(start) or ("missing", None)), "entries": sorted(entries)}
    return hashlib.sha256(json.dumps(found, sort_keys=True).encode()).hexdigest()


def structured(root, names, trees=None, skip=(), contents=None):
    """{name: ``_identity``} of a Rust workspace's sources, and {directory + "/": ``_tree``}
    of each it compares entry by entry (``trees``: {directory: "exact" or "package"}).
    ``contents``: {name: SHA-256} of the files' contents as a build recorded them, taken
    instead of the disk's: what the build read, in the shape the disk has now."""
    contents = contents or {}
    found = {name: _identity(root, name, contents.get(name)) for name in names}
    for top, how in (trees or {}).items():
        found[top.rstrip("/") + "/"] = _tree(root, top, how, frozenset(skip))
    return found


def files(root, names):
    """{name: sha256, or None where the file is missing, unreadable or not a regular
    file}."""
    manifest = {}
    for name in names:
        try:
            manifest[name] = _hash(Path(root) / name)
        except OSError:
            manifest[name] = None
    return manifest


def digest(manifest):
    """As a compiled run's inputs_sha256: over the sorted (path, sha256) pairs."""
    return hashlib.sha256(json.dumps(sorted(manifest.items())).encode()).hexdigest()


def difference(theirs, ours, them="your checkout", us="the worker host", *, structured=False):
    """The first source, by name, that differs or is on one side only, and why; or None.
    Files first, then the directories compared entry by entry (named with a trailing `/`).
    ``structured``: both sides are ``structured``'s, so a file differs by its links too.

    ``ours`` may hold None for a file that is missing or unreadable on our side."""
    for name in sorted(set(theirs) | set(ours), key=lambda each: (each.endswith("/"), each)):
        if name.endswith("/") and name in theirs and name in ours and ours[name] != theirs[name]:
            return (
                f"{name} (its entries on {us} differ from {them}'s: an entry on one side only, "
                "one of another kind, or a link that points elsewhere)"
            )
        if name not in ours:
            return f"{name} (only in {them})"
        if name not in theirs:
            return f"{name} (only on {us})" if ours[name] else f"{name} (missing on {us})"
        if ours[name] is None:
            return f"{name} (missing on {us}, or not a readable regular file)"
        if ours[name] != theirs[name]:
            if structured:
                return (
                    f"{name} (the copy on {us} differs from {them}, in its contents or in where "
                    "a link is or points)"
                )
            return f"{name} (the copy on {us} differs from {them})"
    return None


def refusal(what):
    """The message a source mismatch is refused with: PROVENANCE_CHANGED."""
    return (
        f"Worker source provenance differs for {what}: refused before launching. Sync "
        "sources before debugging remotely"
    )


def check_expected(expected):
    """Validate a client's expect_inputs: {relative path: sha256}, never empty: an empty
    manifest would compare nothing."""
    if (
        not isinstance(expected, dict)
        or not expected
        or len(expected) > MAX_SOURCES
        or not all(
            isinstance(name, str)
            and 0 < len(name) <= MAX_NAME
            and "\x00" not in name
            and isinstance(value, str)
            and re.fullmatch(r"[0-9a-f]{64}", value)
            for name, value in expected.items()
        )
    ):
        raise ValueError(
            f"expect_inputs maps 1 to {MAX_SOURCES} source paths to sha256 hex digests"
        )


def too_many(count):
    """The refusal of a remote run whose target reads ``count`` files, more than
    ``MAX_SOURCES``: they can't all be compared with a client's."""
    return (
        f"The worker's target reads {count} files, more than the {MAX_SOURCES} Peeky compares "
        "with a remote client's: refused before launching. Run Peeky on the worker's host, or "
        "give the worker a smaller target (a Rust workspace: fewer packages in analyse)"
    )


def worker_sources(adapter, root):
    """{name: sha256 or None} of what this worker's target reads under ``root``; a Rust
    workspace's by their structure (``structured``)."""
    names = declared(adapter, root)
    if names is not None:
        trees, skip = ({}, ())
        if (trees_for := getattr(adapter, "compared_trees", None)) is not None:
            trees, skip = trees_for(root)
        if len(names) + len(trees) > MAX_SOURCES:
            raise ValueError(too_many(len(names) + len(trees)))
        # A Rust workspace's build may have read what no client can send a hash of.
        if (refusal := getattr(adapter, "remote_refusal", None)) and (why := refusal(root)):
            raise ValueError(why)
        if trees_for is not None:
            return structured(root, names, trees, skip)
        return files(root, names)
    try:
        return dict(python_sources(root))
    except Unhashable as exc:
        raise ValueError(
            f"The worker cannot hash every .py file under its root: {exc} ({TOO_MANY}). So it "
            "cannot compare them with yours: refused before launching. Start it with another "
            "--root"
        ) from None


def client_sources(root, info):
    """{name: sha256} of the local copies of the sources the worker says it reads."""
    if not isinstance(info, dict) or info.get("kind") not in ("python", "declared"):
        raise ValueError(
            "The worker does not say which sources it reads, so they cannot be compared: "
            "install the same Peeky version on both hosts"
        )
    if info["kind"] == "python":
        try:
            return dict(python_sources(root))
        except Unhashable as exc:
            raise ValueError(
                f"Peeky cannot hash every .py file under {root}: {exc} ({TOO_MANY}). So the "
                "worker cannot compare them with its own: probe another directory"
            ) from None
    names = info.get("names")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("The worker's source list is not a list of paths")
    trees, skip = info.get("trees") or {}, info.get("skip") or []
    if not (
        isinstance(trees, dict)
        and all(how in _HOW for how in trees.values())
        and isinstance(skip, list)
        and all(isinstance(each, str) for each in skip)
    ):
        raise ValueError("The worker's directories to compare are not a map of paths")
    if len(names) + len(trees) > MAX_SOURCES:
        raise ValueError(too_many(len(names) + len(trees)))
    for name in [*names, *trees, *skip]:
        parts = PurePosixPath(name).parts
        if not name or PurePosixPath(name).is_absolute() or ".." in parts or "\x00" in name:
            raise ValueError(f"The worker named a source outside the target's directory: {name}")
    if info.get("structured"):
        manifest = structured(root, names, trees, skip)
    else:
        manifest = files(root, names)
    if missing := sorted(name for name, sha in manifest.items() if sha is None):
        raise RuntimeError(refusal(f"{missing[0]} (missing in your checkout)"))
    return manifest


def drift(accepted, current):
    """How the worker's sources changed since they were accepted at launch, or None."""
    for name in sorted(set(accepted) | set(current)):
        if name not in current:
            return f"{name} was removed"
        if name not in accepted:
            return f"{name} appeared"
        if accepted[name] != current[name]:
            return f"{name} changed"
    return None
