"""Bootstrap guards require no JDK or network."""

import importlib.util
import io
import sys
from pathlib import Path

import pytest


@pytest.fixture
def bootstrap():
    path = Path(__file__).resolve().parents[2] / "scripts/setup_java_adapter.py"
    spec = importlib.util.spec_from_file_location("java_setup_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_rejects_existing_and_dangling_output(bootstrap, tmp_path):
    with pytest.raises(ValueError, match="already exists"):
        bootstrap.setup(tmp_path)
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="already exists"):
        bootstrap.setup(link)


def test_bootstrap_strips_worker_token(bootstrap, monkeypatch):
    monkeypatch.setenv("DEBUG_TOKEN", "test-worker-secret")
    result = bootstrap.run([sys.executable, "-c", "import os; print('DEBUG_TOKEN' in os.environ)"])
    assert result == "False"


def test_bootstrap_limits_tool_output(bootstrap):
    with pytest.raises(RuntimeError, match="output exceeds"):
        bootstrap.run([sys.executable, "-c", "print('x' * 100000)"])


def test_bootstrap_rejects_changed_download(bootstrap, monkeypatch):
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"wrong"))
    with pytest.raises(ValueError, match="checksum/size mismatch"):
        bootstrap.acquire({"url": "https://example.invalid/pinned", "sha256": "0" * 64})


def test_setup_accepts_an_output_whose_parent_does_not_exist_yet(bootstrap, tmp_path, monkeypatch):
    # The documented `--output "$PWD/work/java-adapter"` names a gitignored parent.
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="JDK required"):
        bootstrap.setup(tmp_path / "work" / "java-adapter")
    assert (tmp_path / "work").is_dir()
