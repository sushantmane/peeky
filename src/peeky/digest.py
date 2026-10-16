"""SHA-256 of file contents, reused while a file is provably unchanged.

Provenance checks rerun on every debugger operation, and they cover toolchain
binaries (clang is 125 MB). Rehashing them each time dominated compiled-target
latency. A digest is reused only while the file keeps the same device, inode, size,
modification time and status-change time, which any write, replacement or rename
changes. This is the change detection git, make and Cargo rely on, and it matches
Peeky's threat model: trusted code, accidental changes (see THREAT_MODEL.md). Files
whose timestamps are recent are always rehashed, so filesystems with coarse
timestamps cannot hide a second write in the same tick, and a file that changes
while it is being hashed is never cached. Set PEEKY_NO_DIGEST_CACHE=1 to hash every
time (for example on filesystems whose timestamps are unreliable).
"""

import hashlib
import os
import stat
import threading
import time

SETTLE_SECONDS = 2.0
MAX_ENTRIES = 4096
_CHUNK = 1024 * 1024
_cache = {}
_lock = threading.Lock()


def _key(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _settled(info):
    newest = max(info.st_mtime_ns, info.st_ctime_ns) / 1e9
    return time.time() - newest > SETTLE_SECONDS


def sha256_file(path):
    """Hex SHA-256 of the regular file at ``path`` (symlinks followed, as open does)."""
    name = os.path.abspath(os.fspath(path))
    current = os.stat(name)
    if not stat.S_ISREG(current.st_mode):
        # Never open FIFOs or devices: opening one can block or never end.
        raise ValueError(f"Expected a regular file: {name}")
    caching = os.environ.get("PEEKY_NO_DIGEST_CACHE") != "1"
    if caching:
        with _lock:
            cached = _cache.get(name)
        if cached is not None and cached[0] == _key(current):
            return cached[1]
    with open(name, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Expected a regular file: {name}")
        digest = hashlib.sha256()
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    result = digest.hexdigest()
    key = _key(before)
    if caching and key == _key(after) and key == _key(os.stat(name)) and _settled(before):
        with _lock:
            if len(_cache) >= MAX_ENTRIES:
                _cache.clear()
            _cache[name] = (key, result)
    return result


def clear():
    with _lock:
        _cache.clear()


def tree_fingerprint(root, limit):
    """Stat identity of every entry under ``root`` (symlinks not followed), or None.

    None means "verify fully": caching is off, the tree has more than ``limit``
    entries, holds a symlink or something other than files and directories, or
    changed recently. Equal fingerprints mean no entry was added, removed, replaced
    or written since, under the same rules as ``sha256_file``.
    """
    if os.environ.get("PEEKY_NO_DIGEST_CACHE") == "1":
        return None
    entries, pending = [], [os.fspath(root)]
    while pending:
        with os.scandir(pending.pop()) as listing:
            for entry in listing:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(entry.path)
                elif not stat.S_ISREG(info.st_mode) or not _settled(info):
                    return None
                entries.append((entry.path, info.st_mode, *_key(info)))
                if len(entries) > limit:
                    return None
    return tuple(sorted(entries))
