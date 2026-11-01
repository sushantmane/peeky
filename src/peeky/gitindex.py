"""A git index's (`.git/index`) entries, to tell git's refresh of its cached file status
from a change.

git rewrites its index to refresh the file status it caches (`git status`, `git describe
--dirty`), every entry as it was. A build that reads the index (git-version's
`git_version!()` `include_bytes!` it, and runs `git describe --dirty` while the crate
compiles) still compares it by its bytes, since a crate may use them, and a rebuild would
differ. These say why it changed: each entry's path, mode, object ID and stage, and the
flags that change what git reports (assume-unchanged, skip-worktree, intent-to-add), with
the resolve-undo and sparse-directory extensions. The cached file status, and the caches
git rebuilds from the entries or the working tree (the cache tree, the untracked cache,
the file system monitor's, the entry offsets), are left out. An index Peeky can't read
so (another version, a split index, an extension it doesn't know, a checksum that doesn't
match) has none."""

import hashlib
import struct
from pathlib import Path

# Bigger than this, an index is compared by its bytes.
MAX_INDEX_BYTES = 256 * 1024 * 1024
# Extensions git rebuilds from the entries or the working tree: left out.
_CACHES = frozenset({b"TREE", b"UNTR", b"FSMN", b"EOIE", b"IEOT"})
# Extensions that are part of what the index says: kept.
_KEPT = frozenset({b"REUC", b"sdir"})
# The flags kept: assume-unchanged and the stage; skip-worktree and intent-to-add.
_FLAGS = 0x8000 | 0x3000
_EXTENDED = 0x4000 | 0x2000


def is_index(path):
    """Whether ``path`` names a repository's own index (`.git/index`)."""
    path = Path(path)
    return path.name == "index" and path.parent.name == ".git"


def digest(path):
    """``path``'s entries' digest (`git-index:<SHA-256>`), or None where it isn't an index
    Peeky reads so."""
    try:
        with open(path, "rb") as file:
            data = file.read(MAX_INDEX_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_INDEX_BYTES:
        return None
    return entries_digest(data)


def entries_digest(data):
    """``digest`` of an index's bytes ``data``."""
    if len(data) < 12 or data[:4] != b"DIRC":
        return None
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3, 4):
        return None
    for size, algorithm in ((20, hashlib.sha1), (32, hashlib.sha256)):
        if len(data) >= 12 + size and algorithm(data[:-size]).digest() == data[-size:]:
            body = data[:-size]
            break
    else:
        return None
    out = hashlib.sha256(b"git-index\0" + bytes([size]))
    at, previous = 12, b""
    for _ in range(count):
        start = at
        if at + 42 + size > len(body):
            return None
        mode = struct.unpack(">I", body[at + 24 : at + 28])[0]
        oid = body[at + 40 : at + 40 + size]
        flags = struct.unpack(">H", body[at + 40 + size : at + 42 + size])[0]
        at += 42 + size
        extended = 0
        if flags & 0x4000:
            if version < 3 or at + 2 > len(body):
                return None
            extended = struct.unpack(">H", body[at : at + 2])[0]
            at += 2
        if version == 4:
            found = _varint(body, at)
            if found is None:
                return None
            strip, at = found
            end = body.find(b"\0", at)
            if end < 0 or strip > len(previous):
                return None
            name = previous[: len(previous) - strip] + body[at:end]
            at = end + 1
        else:
            end = body.find(b"\0", at)
            if end < 0:
                return None
            name = body[at:end]
            # Padded with 1 to 8 NULs to a multiple of 8 bytes.
            at = start + ((end - start + 8) & ~7)
            if at > len(body) or body[end:at].strip(b"\0"):
                return None
        if (flags & 0x0FFF) != min(len(name), 0x0FFF):
            return None
        previous = name
        out.update(struct.pack(">I", len(name)) + name)
        out.update(struct.pack(">IHH", mode, flags & _FLAGS, extended & _EXTENDED) + oid)
    while at < len(body):
        if at + 8 > len(body):
            return None
        signature = body[at : at + 4]
        length = struct.unpack(">I", body[at + 4 : at + 8])[0]
        if at + 8 + length > len(body):
            return None
        if signature in _KEPT:
            out.update(signature + body[at + 4 : at + 8 + length])
        elif signature not in _CACHES:
            # A split index (`link`), or one Peeky doesn't know: its bytes.
            return None
        at += 8 + length
    return "git-index:" + out.hexdigest()


def _varint(data, at):
    """(the number git's index version 4 writes at ``at``, where it ends), or None."""
    if at >= len(data):
        return None
    byte = data[at]
    value = byte & 0x7F
    at += 1
    while byte & 0x80:
        if at >= len(data):
            return None
        byte = data[at]
        value = ((value + 1) << 7) | (byte & 0x7F)
        at += 1
    return value, at
