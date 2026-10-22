"""The sources a worker's target reads, as {path: sha256}: what a remote client and the
worker compare before anything launches (#249).

The worker's trusted target names them: Java's `sources`; C and C++ `sources` and
`headers`; Rust's Cargo.toml, Cargo.lock, `sources` and `tests`. A Python program may
import any `.py` file under the root, so for Python they are all of those, except in
`__pycache__`, `.git` and virtual environments (a directory holding `pyvenv.cfg`: installed
packages). Both sides must hold the same set, file for file.
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
SKIPPED = frozenset({"__pycache__", ".git"})
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
            if name in SKIPPED or environment(path):
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


def declared(adapter):
    """The names the worker's trusted target reads, or None for a Python program."""
    names = getattr(adapter, "verification_inputs", None)
    if names is None:
        names = getattr(adapter, "sources", None)  # an attached JVM's declared sources
    return None if names is None else tuple(dict.fromkeys(str(name) for name in names))


def describe(adapter):
    """The worker's `version` field that tells a client which sources to hash."""
    names = declared(adapter)
    return {"kind": "python"} if names is None else {"kind": "declared", "names": list(names)}


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


def difference(theirs, ours, them="your checkout", us="the worker host"):
    """The first source, by name, that differs or is on one side only, and why; or None.

    ``ours`` may hold None for a file that is missing or unreadable on our side."""
    for name in sorted(set(theirs) | set(ours)):
        if name not in ours:
            return f"{name} (only in {them})"
        if name not in theirs:
            return f"{name} (only on {us})" if ours[name] else f"{name} (missing on {us})"
        if ours[name] is None:
            return f"{name} (missing on {us}, or not a readable regular file)"
        if ours[name] != theirs[name]:
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


def worker_sources(adapter, root):
    """{name: sha256 or None} of what this worker's target reads under ``root``."""
    names = declared(adapter)
    if names is not None:
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
    for name in names:
        parts = PurePosixPath(name).parts
        if not name or PurePosixPath(name).is_absolute() or ".." in parts or "\x00" in name:
            raise ValueError(f"The worker named a source outside the target's directory: {name}")
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
