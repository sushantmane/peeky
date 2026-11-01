"""gitindex: a git index compared by its entries, not by the file status git caches in it."""

import hashlib
import os
import shutil
import subprocess
import time

import pytest

from peeky import gitindex

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("a\n")
    (root / "sub/b.txt").write_text("b\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


def index(repo):
    path = repo / ".git/index"
    return gitindex.digest(path), hashlib.sha256(path.read_bytes()).hexdigest()


def touched(path, ago=[1000]):
    """``path`` with a modification time the index doesn't cache (earlier, so not racily
    clean), its contents as they were."""
    ago[0] += 100
    earlier = time.time() - ago[0]
    os.utime(path, (earlier, earlier))


def test_a_refreshed_index_is_the_same_index(repo):
    # `git describe --dirty`, as git-version's macro runs it while a crate compiles,
    # rewrites the index with a file's cached status refreshed: its bytes change, not
    # what git reports.
    entries, data = index(repo)
    assert entries.startswith("git-index:")
    touched(repo / "a.txt")
    git(repo, "describe", "--always", "--dirty")
    after = index(repo)
    assert after[1] != data and after[0] == entries
    # Each version git writes, and the caches it rebuilds, are the same index too.
    for version in ("4", "3", "2"):
        git(repo, "update-index", "--index-version", version)
        assert index(repo)[0] == entries, version
    git(repo, "update-index", "--untracked-cache")
    git(repo, "status")
    assert index(repo)[0] == entries
    git(repo, "update-index", "--no-untracked-cache")


@pytest.mark.parametrize(
    "change",
    [
        ("add", "a.txt"),
        ("update-index", "--skip-worktree", "a.txt"),
        ("update-index", "--assume-unchanged", "sub/b.txt"),
        ("add", "-N", "new.txt"),
        ("rm", "--cached", "-q", "sub/b.txt"),
        ("update-index", "--chmod=+x", "a.txt"),
    ],
)
def test_what_changes_what_git_reports_changes_the_index(repo, change):
    entries = index(repo)[0]
    (repo / "a.txt").write_text("changed\n")
    (repo / "new.txt").write_text("new\n")
    git(repo, *change)
    assert index(repo)[0] not in (None, entries)


def test_an_index_peeky_cant_read_by_its_entries_is_none(repo, tmp_path):
    path = repo / ".git/index"
    # A split index keeps its entries in another file.
    git(repo, "update-index", "--split-index")
    assert gitindex.digest(path) is None
    git(repo, "update-index", "--no-split-index")
    data = path.read_bytes()
    assert gitindex.entries_digest(data) is not None
    # A checksum that doesn't match, a version or extension Peeky doesn't know, too short.
    assert gitindex.entries_digest(data[:-1] + bytes([data[-1] ^ 1])) is None
    other = data[:4] + (5).to_bytes(4, "big") + data[8:-20]
    assert gitindex.entries_digest(other + hashlib.sha1(other).digest()) is None
    unknown = data[:-20] + b"abcd" + (0).to_bytes(4, "big")
    assert gitindex.entries_digest(unknown + hashlib.sha1(unknown).digest()) is None
    assert gitindex.entries_digest(data[:30]) is None
    assert gitindex.digest(tmp_path / "missing") is None
    assert gitindex.is_index(path) and not gitindex.is_index(repo / "index")


def test_a_sha256_repository_index_is_read(tmp_path):
    root = tmp_path / "repo256"
    root.mkdir()
    try:
        git(root, "init", "-q", "--object-format=sha256")
    except subprocess.CalledProcessError:
        pytest.skip("this git has no SHA-256 repositories")
    (root / "a.txt").write_text("a\n")
    git(root, "add", "a.txt")
    entries = gitindex.digest(root / ".git/index")
    assert entries is not None
    touched(root / "a.txt")
    git(root, "update-index", "--refresh")
    assert gitindex.digest(root / ".git/index") == entries
