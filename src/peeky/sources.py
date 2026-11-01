"""The sources a worker's target reads, as {path: sha256}: what a remote client and the
worker compare before anything launches.

The worker's trusted target names them: Java's `sources`; C and C++ `sources` and
`headers`; Rust's Cargo.toml, Cargo.lock, `sources` and `tests`; a Rust workspace's build's
manifests, lockfile and configuration, every workspace file it compiled, and what its build
scripts read. A binary the project built (native_binary.BinaryTarget) names the files its
judged units read, under the root, as its debug information records them. A Python program may
import any `.py` file under the root, so for Python they are all of those, except in virtual
environments (a directory holding `pyvenv.cfg`: installed packages). Both sides must hold the
same set, file for file.

A Rust workspace's are compared by their structure too (``structured``): each file's kind, the
links on its way and what they say, and each directory a build script watches entry by entry,
so a link where the other side has a file, or an entry on one side only, differs. A link that
leads out of the root is refused, and nothing through it is read (``OutsideRoot``).
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
    workspace's are its current build's under ``root`` (``compared_names``); a binary
    target's are those its units name under ``root`` (BinaryTarget.remote_inputs), or its
    refusal."""
    if (names_for := getattr(adapter, "compared_names", None)) is not None:
        return tuple(dict.fromkeys(str(name) for name in names_for(root)))
    if (listed := getattr(adapter, "remote_inputs", None)) is not None:
        return tuple(listed(root))
    names = getattr(adapter, "verification_inputs", None)
    if names is None:
        names = getattr(adapter, "sources", None)  # an attached JVM's declared sources
    return None if names is None else tuple(dict.fromkeys(str(name) for name in names))


def describe(adapter, root=None):
    """The worker's `version` field that tells a client which sources to hash: for a Rust
    workspace, the directories compared entry by entry too (``trees``: {directory:
    "exact"}), each source by its structure (``structured``). A binary target's names only
    its binary, by its name under ``root``: listing its files reads the binary's debug
    information, which only the `inputs` operation does (listed)."""
    if getattr(adapter, "remote_inputs", None) is not None:
        from peeky.native_binary import binary_name

        try:
            shown = binary_name(root, adapter.binary)
        except ValueError:
            shown = adapter.binary
        return {"kind": "binary", "binary": shown}
    names = declared(adapter, root)
    if names is None:
        return {"kind": "python"}
    found = {"kind": "declared", "names": list(names)}
    if (trees_for := getattr(adapter, "compared_trees", None)) is not None:
        found.update(structured=True, trees=dict(trees_for(root)))
    return found


# The most entries Peeky lists of the directories a Rust workspace's build scripts watch.
MAX_TREE_ENTRIES = 200_000
# How a directory is compared entry by entry: as Cargo scans a `rerun-if-changed` one.
_HOW = ("exact",)


class OutsideRoot(ValueError):
    """A compared path that a link leads out of the root: refused, and nothing through the
    link read."""

    def __init__(self, name, link, target, side):
        self.link, self.target = link, target
        super().__init__(
            f"Peeky compares only files inside the root: on {side}, {name} leads out of it "
            f"through {link}, a link to {target}. Nothing there was read. Point the link inside "
            "the root, or make it a file or directory there"
        )


class LinkLoop(ValueError):
    """A compared path whose links loop, or are more than ``_MAX_HOPS`` in a row: refused,
    and nothing through them read."""

    def __init__(self, name, link, side, loop):
        self.loop = loop
        self.what = what = (
            f"leads into a link loop at {link}"
            if loop
            else f"passes through more than {_MAX_HOPS} links, the last {link}"
        )
        super().__init__(
            f"Peeky compares only files it can reach: on {side}, {name} {what}. Nothing through "
            "it was read. "
            + ("Break the loop" if loop else "Shorten the chain of links")
            + ", or make it a file or directory there"
        )


# The most links Peeky follows on one path, as the kernel does (ELOOP).
_MAX_HOPS = 40


def _inside(root, name, side):
    """Follow ``name`` under the resolved ``root`` one component, and one link, at a time,
    as the kernel does: a link's text from where the link is, its `..` from where that is.
    Every step is held inside ``root``: OutsideRoot names the link that would leave it, out
    and back in too. A loop, or more than ``_MAX_HOPS`` links, is LinkLoop. Only links'
    texts are read (``readlink``), never a file. (the links followed, in order, each [its
    path under ``root``, its text]; the path reached, which no link is on.)"""
    current, chain, states = root, [], set()
    # Each part to follow, with the link whose text gave it (None: ``name``'s own).
    pending = [(part, None) for part in PurePosixPath(name).parts]
    while pending:
        part, via = pending.pop(0)
        if part in ("", "."):
            continue
        candidate = current.parent if part == ".." else current / part
        if not candidate.is_relative_to(root):
            raise OutsideRoot(name, via[0] if via else name, via[1] if via else part, side)
        if part == ".." or not os.path.islink(candidate):
            current = candidate
            continue
        link, text = candidate.relative_to(root).as_posix(), os.readlink(candidate)
        state = (link, tuple(pending))
        if state in states:
            raise LinkLoop(name, link, side, loop=True)
        if len(chain) >= _MAX_HOPS:
            raise LinkLoop(name, link, side, loop=False)
        states.add(state)
        chain.append([link, text])
        parts = PurePosixPath(text).parts
        if PurePosixPath(text).is_absolute():
            # Only a text that names the root itself, as written, stays inside it.
            if parts[: len(root.parts)] != root.parts:
                raise OutsideRoot(name, link, text, side)
            current, parts = root, parts[len(root.parts) :]
        else:
            current = candidate.parent
        pending = [(each, (link, text)) for each in parts] + pending
    return chain, current


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


def _identity(root, name, content=None, side="the worker host"):
    """``name``'s identity: its content's SHA-256 for a regular file reached through no
    link (as every target's sources are compared); else the SHA-256 of every link followed
    on its way, and what each says (``_inside``), with its file's SHA-256, or "dir" for a
    directory (one a package's walk names: an empty one). None where it is missing, or
    neither. ``content``: the content's
    SHA-256 as a build recorded it, taken instead of the disk's. One a link leads out of
    ``root`` (resolved), or whose links loop, is refused, unread."""
    chain, real = _inside(Path(root), name, side)
    try:
        info = os.lstat(real)
    except OSError:
        return None
    if stat.S_ISDIR(info.st_mode):
        # A directory a package's walk names (an empty one), or a link to one: by its links.
        what = "dir"
    elif stat.S_ISREG(info.st_mode):
        try:
            what = _hash(real) if content is None else content
        except OSError:
            return None
    else:
        return None
    if not chain and what != "dir":
        return what
    entry = {"links": chain, "sha256": what}
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()


def _tree(root, top, side="the worker host"):
    """The SHA-256 of what the directory ``top`` holds, as Cargo scans a `rerun-if-changed`
    directory: the links on its way, and every entry under it, hidden and `target` ones too,
    through links to directories, by its path and kind; a link by every link followed from
    it and what each says (``_inside``), and its file's SHA-256 (files' contents are compared
    by name). Never a `.git` entry. ``root`` is resolved; a link on the way, or under it,
    that leads out of it, or loops, is refused, unread."""
    chain, _ = _inside(root, top, side)
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
            if kind == "link":
                links, reached = _inside(root, (PurePosixPath(top) / relative).as_posix(), side)
                try:
                    what = "dir" if reached.is_dir() else _hash(reached)
                except OSError:
                    what = None
                says = [links, what]
            elif kind != "missing":
                says = None
            entries.append([relative, kind, says])
            if len(entries) > MAX_TREE_ENTRIES:
                raise ValueError(
                    f"{top} holds more than {MAX_TREE_ENTRIES} entries, more than Peeky compares "
                    "with a remote client's: run Peeky on the worker's host"
                )
            if kind == "dir" or (kind == "link" and path.is_dir()):
                pending.append((path, relative + "/"))
    found = {"links": chain, "root": (_kind(start) or ("missing",))[0], "entries": sorted(entries)}
    return hashlib.sha256(json.dumps(found, sort_keys=True).encode()).hexdigest()


def structured(root, names, trees=None, contents=None, side="the worker host"):
    """{name: ``_identity``} of a Rust workspace's sources, and {directory + "/": ``_tree``}
    of each it compares entry by entry (``trees``: {directory: "exact"}). ``contents``:
    {name: SHA-256} of the files' contents as a build recorded them, taken instead of the
    disk's: what the build read, in the shape the disk has now. ``side`` names the host in
    a refusal: a path a link leads out of the root (OutsideRoot)."""
    root, contents = Path(os.path.realpath(root)), contents or {}
    found = {name: _identity(root, name, contents.get(name), side) for name in names}
    for top in trees or {}:
        found[top.rstrip("/") + "/"] = _tree(root, top, side)
    return found


def listed(adapter, root):
    """The worker's `inputs` answer: a binary target's files, as `version` gives other
    targets' (``{"kind": "declared", "names": [...], "binary": ...}``), listed once per
    binary (BinaryTarget.remote_inputs). A binary the worker refuses is refused, saying so:
    a client's request is never what is wrong."""
    if getattr(adapter, "remote_inputs", None) is None:
        return describe(adapter, root)
    from peeky.errors import Refused, classify
    from peeky.native_binary import binary_name

    try:
        shown = binary_name(root, adapter.binary)
        names = declared(adapter, root)
    except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
        error = classify(exc, session_closed=False)
        if error["code"] == "BUSY":
            raise
        hint = error["hint"]
        if error["code"] == "INVALID_REQUEST":
            hint = (
                "Nothing ran. Fix it on the worker's host: its own configuration names the "
                "binary and its tools."
            )
        raise Refused(
            error["code"], f"The worker refuses its binary: {error['message']}", hint
        ) from None
    return {"kind": "declared", "names": list(names), "binary": shown}


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
        trees = {}
        if (trees_for := getattr(adapter, "compared_trees", None)) is not None:
            trees = trees_for(root)
        if len(names) + len(trees) > MAX_SOURCES:
            raise ValueError(too_many(len(names) + len(trees)))
        # A Rust workspace's build may have read what no client can send a hash of.
        if (refusal := getattr(adapter, "remote_refusal", None)) and (why := refusal(root)):
            raise ValueError(why)
        if trees_for is not None:
            return structured(root, names, trees)
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
    """{name: sha256} of the local copies of the sources the worker says it reads (for a
    binary target, as its `inputs` answer lists them)."""
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
    trees = info.get("trees") or {}
    if not (isinstance(trees, dict) and all(how in _HOW for how in trees.values())):
        raise ValueError("The worker's directories to compare are not a map of paths")
    if len(names) + len(trees) > MAX_SOURCES:
        raise ValueError(too_many(len(names) + len(trees)))
    for name in [*names, *trees]:
        parts = PurePosixPath(name).parts
        if not name or PurePosixPath(name).is_absolute() or ".." in parts or "\x00" in name:
            raise ValueError(f"The worker named a source outside the target's directory: {name}")
    if info.get("structured"):
        manifest = structured(root, names, trees, side="your checkout")
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
