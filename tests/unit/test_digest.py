"""Cached file digests: reuse only while a file is provably unchanged."""

import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from peeky import digest


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    digest.clear()
    monkeypatch.delenv("PEEKY_NO_DIGEST_CACHE", raising=False)
    yield
    digest.clear()


@pytest.fixture
def hashes(monkeypatch):
    """Counts real hash computations."""
    calls = []

    def counting(*args):
        calls.append(1)
        return REAL_SHA256(*args)

    monkeypatch.setattr(digest.hashlib, "sha256", counting)
    return calls


REAL_SHA256 = hashlib.sha256


def expected(path):
    return REAL_SHA256(path.read_bytes()).hexdigest()


def test_settled_unchanged_files_are_hashed_once(tmp_path, monkeypatch, hashes):
    monkeypatch.setattr(digest, "SETTLE_SECONDS", -1.0)
    path = tmp_path / "tool"
    path.write_bytes(b"x" * 3_000_000)
    for _ in range(5):
        assert digest.sha256_file(path) == expected(path)
    assert len(hashes) == 1


@pytest.mark.parametrize("change", ["rewrite", "replace", "append", "touch"])
def test_any_change_is_rehashed(tmp_path, monkeypatch, hashes, change):
    monkeypatch.setattr(digest, "SETTLE_SECONDS", -1.0)
    path = tmp_path / "tool"
    path.write_bytes(b"a" * 4096)
    first = digest.sha256_file(path)
    if change == "rewrite":
        # Same size, different bytes: mtime/ctime change.
        path.write_bytes(b"b" * 4096)
    elif change == "replace":
        other = tmp_path / "new"
        other.write_bytes(b"c" * 4096)
        os.replace(other, path)
    elif change == "append":
        with path.open("ab") as stream:
            stream.write(b"!")
    else:
        os.utime(path)
    second = digest.sha256_file(path)
    assert second == expected(path) and len(hashes) == 2
    assert (second != first) is (change != "touch")


def test_recent_files_are_always_rehashed(tmp_path, hashes):
    path = tmp_path / "artifact"
    path.write_bytes(b"fresh")
    for _ in range(3):
        assert digest.sha256_file(path) == expected(path)
    assert len(hashes) == 3


def test_a_file_changing_while_hashed_is_not_cached(tmp_path, monkeypatch, hashes):
    monkeypatch.setattr(digest, "SETTLE_SECONDS", -1.0)
    path = tmp_path / "tool"
    path.write_bytes(b"a" * 4096)
    real = os.fstat
    seen = []

    def drifting(descriptor):
        info = real(descriptor)
        seen.append(1)
        if len(seen) == 2:
            fields = ("st_mode", "st_dev", "st_ino", "st_size", "st_ctime_ns")
            return SimpleNamespace(
                **{name: getattr(info, name) for name in fields},
                st_mtime_ns=info.st_mtime_ns + 1,
            )
        return info

    monkeypatch.setattr(digest.os, "fstat", drifting)
    digest.sha256_file(path)
    monkeypatch.setattr(digest.os, "fstat", real)
    digest.sha256_file(path)
    assert len(hashes) == 2


def test_the_cache_can_be_disabled(tmp_path, monkeypatch, hashes):
    monkeypatch.setattr(digest, "SETTLE_SECONDS", -1.0)
    monkeypatch.setenv("PEEKY_NO_DIGEST_CACHE", "1")
    path = tmp_path / "tool"
    path.write_bytes(b"a")
    digest.sha256_file(path)
    digest.sha256_file(path)
    assert len(hashes) == 2


def test_only_regular_files_are_opened(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular file"):
        digest.sha256_file(fifo)  # would block forever if opened
    with pytest.raises(ValueError, match="regular file"):
        digest.sha256_file(tmp_path)
    with pytest.raises(FileNotFoundError):
        digest.sha256_file(tmp_path / "missing")


def test_symlinks_are_followed_and_a_retargeted_link_is_rehashed(tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "SETTLE_SECONDS", -1.0)
    one, two, link = tmp_path / "one", tmp_path / "two", tmp_path / "link"
    one.write_bytes(b"1")
    two.write_bytes(b"2")
    link.symlink_to(one)
    assert digest.sha256_file(link) == expected(one)
    link.unlink()
    link.symlink_to(two)
    assert digest.sha256_file(link) == expected(two)


@pytest.mark.skipif(sys.platform != "darwin", reason="xcrun stubs are macOS-only")
def test_macos_xcrun_stubs_resolve_to_the_tools_they_run():
    import shutil

    from peeky.build import real_tool

    if not shutil.which("xcrun") or not os.path.exists("/usr/bin/dwarfdump"):
        pytest.skip("no Command Line Tools")
    stub = os.stat("/usr/bin/dwarfdump")
    real = real_tool("/usr/bin/dwarfdump")
    assert real.parent != Path("/usr/bin") and os.stat(real).st_ino != stub.st_ino
    assert real_tool(real) == real
    assert real_tool("/bin/ls") == Path("/bin/ls").resolve()


def test_verified_trees_are_reused_only_while_unchanged(tmp_path, monkeypatch):
    from peeky import java

    monkeypatch.setattr(digest, "SETTLE_SECONDS", -1.0)
    monkeypatch.setattr(java, "_BUNDLES", {})
    bundle = tmp_path / "bundle"
    (bundle / "lib").mkdir(parents=True)
    (bundle / "adapter.json").write_text("{}")
    (bundle / "lib" / "a.jar").write_bytes(b"jar")
    verified = []
    monkeypatch.setattr(java, "_verify_bundle", lambda b, j: verified.append(1) or ("cmd", "h"))

    def check():
        java._bundle(bundle, "/java")
        return len(verified)

    assert check() == 1 and check() == 1
    (bundle / "lib" / "a.jar").write_bytes(b"JAR")
    assert check() == 2 and check() == 2
    (bundle / "lib" / "b.jar").write_bytes(b"new")
    assert check() == 3
    (bundle / "lib" / "b.jar").unlink()
    assert check() == 4
    assert java._bundle(bundle, "/other-java") and len(verified) == 5
    (bundle / "lib" / "link.jar").symlink_to(bundle / "lib" / "a.jar")
    assert check() == 6 and check() == 7  # symlinks always verify (and fail) in full
    (bundle / "lib" / "link.jar").unlink()
    monkeypatch.setattr(digest, "SETTLE_SECONDS", 3600.0)
    assert check() == 8 and check() == 9  # recently changed trees are never reused
    monkeypatch.setattr(digest, "SETTLE_SECONDS", -1.0)
    monkeypatch.setenv("PEEKY_NO_DIGEST_CACHE", "1")
    assert check() == 10 and check() == 11
