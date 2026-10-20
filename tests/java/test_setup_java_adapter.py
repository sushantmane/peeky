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


UPSTREAM_EVENT_HUB = """\
                for (Event event : set) {
                        try {
                            logger.fine("\\nJDI Event: " + event + "\\n");
                        } catch (VMDisconnectedException e) {
                            // do nothing
                        }
                        DebugEvent dbgEvent = new DebugEvent();
"""


def test_the_event_hub_patch_applies_once_and_is_idempotent(bootstrap, tmp_path):
    # #27: java-debug's event thread died building a log line nobody reads.
    path = tmp_path / bootstrap.EVENT_HUB
    path.parent.mkdir(parents=True)
    path.write_text(UPSTREAM_EVENT_HUB)
    first = bootstrap.apply_patches(tmp_path)
    patched = path.read_text()
    assert "logger.isLoggable(java.util.logging.Level.FINE)" in patched
    assert "catch (RuntimeException e)" in patched and "VMDisconnectedException e" not in patched
    assert bootstrap.apply_patches(tmp_path) == first and path.read_text() == patched
    assert first["eventhub-log-guard"]["file"] == bootstrap.EVENT_HUB


@pytest.mark.parametrize(
    "text", ["", UPSTREAM_EVENT_HUB * 2, UPSTREAM_EVENT_HUB.replace("fine", "info")]
)
def test_the_event_hub_patch_fails_loudly_when_the_source_moved(bootstrap, tmp_path, text):
    path = tmp_path / bootstrap.EVENT_HUB
    path.parent.mkdir(parents=True)
    path.write_text(text)
    with pytest.raises(ValueError, match="eventhub-log-guard does not apply"):
        bootstrap.apply_patches(tmp_path)


def test_the_runtime_requires_every_patch_setup_applies(bootstrap):
    from peeky.java import JAVA_DEBUG_PATCHES

    assert set(bootstrap.PATCHES) == JAVA_DEBUG_PATCHES


@pytest.mark.parametrize("patches", [None, {}, ["eventhub-log-guard"], {"other": {}}])
def test_a_bundle_built_without_the_patches_is_refused(tmp_path, patches):
    import json

    from peeky.errors import classify
    from peeky.java import _verify_bundle

    manifest = {"schema": 1, **({} if patches is None else {"patches": patches})}
    (tmp_path / "adapter.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="predates Peeky's java-debug fixes") as refused:
        _verify_bundle(tmp_path, "/usr/bin/java")
    assert "eventhub-log-guard" in str(refused.value) and "setup_java_adapter.py" in str(
        refused.value
    )
    assert classify(refused.value)["code"] == "UNSUPPORTED"
