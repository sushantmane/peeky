"""Bootstrap guards require no JDK or network."""

import importlib.util
import io
import json
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

    assert set(bootstrap.PATCHES) == set(JAVA_DEBUG_PATCHES)


EVENT_HUB_CLASS = "classes/com/microsoft/java/debug/core/EventHub.class"


def fake_bundle(root, event_hub, patches=None):
    """A bundle that passes every other check: its command, inventory and checksums."""
    import hashlib
    import json
    import os

    from peeky.java import _tool

    files = {
        "classes/PeekyJavaAdapter.class": b"\xca\xfe\xba\xbe launcher",
        EVENT_HUB_CLASS: event_hub,
        "lib/dependency.jar": b"PK jar",
    }
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    java = _tool(sys.executable)
    manifest = {
        "schema": 1,
        "command": [java, "-cp", f"{root}/classes{os.pathsep}{root}/lib/*", "PeekyJavaAdapter"],
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        **({} if patches is None else {"patches": patches}),
    }
    (root / "adapter.json").write_text(json.dumps(manifest))
    return java


OLD_EVENT_HUB = b"\xca\xfe\xba\xbe java/util/logging/Logger fine lambda$start$0"
PATCHED_EVENT_HUB = OLD_EVENT_HUB + b" isLoggable"


@pytest.mark.parametrize(
    "patches", [None, {}, {"eventhub-log-guard": {}}, {"eventhub-log-guard": {"sha256": "0" * 64}}]
)
def test_a_bundle_whose_compiled_event_hub_lacks_the_patch_is_refused_whatever_it_claims(
    tmp_path, patches
):
    from peeky.errors import classify
    from peeky.java import _verify_bundle

    java = fake_bundle(tmp_path, OLD_EVENT_HUB, patches)
    with pytest.raises(ValueError, match="predates Peeky's java-debug fixes") as refused:
        _verify_bundle(tmp_path, java)
    assert "eventhub-log-guard" in str(refused.value) and "setup_java_adapter.py" in str(
        refused.value
    )
    assert classify(refused.value)["code"] == "UNSUPPORTED"


def test_a_bundle_without_the_event_hub_class_is_refused(tmp_path):
    from peeky.java import _verify_bundle

    java = fake_bundle(tmp_path, PATCHED_EVENT_HUB)
    manifest = json.loads((tmp_path / "adapter.json").read_text())
    del manifest["files"][EVENT_HUB_CLASS]
    (tmp_path / EVENT_HUB_CLASS).unlink()
    (tmp_path / "adapter.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="predates"):
        _verify_bundle(tmp_path, java)


@pytest.mark.parametrize("patches", [None, {"eventhub-log-guard": {}}])
def test_the_compiled_patch_is_what_counts(tmp_path, patches):
    from peeky.java import _verify_bundle

    java = fake_bundle(tmp_path, PATCHED_EVENT_HUB, patches)
    command, _ = _verify_bundle(tmp_path, java)
    assert command[-1] == "PeekyJavaAdapter"


def test_real_bundles_are_told_apart_by_their_compiled_event_hub(tmp_path):
    # The patched build's EventHub calls Logger.isLoggable; a bundle built before the
    # patch does not, even with a forged `patches` record.
    import os
    import shutil

    from peeky.java import _tool, _verify_bundle

    patched, old = (
        os.environ.get("PEEKY_JAVA_ADAPTER"),
        os.environ.get("PEEKY_JAVA_ADAPTER_UNPATCHED"),
    )
    if not (patched and old):
        pytest.skip("set PEEKY_JAVA_ADAPTER and PEEKY_JAVA_ADAPTER_UNPATCHED")
    java = _tool("java")
    assert _verify_bundle(Path(patched).resolve(), java)
    forged = tmp_path / "forged"
    shutil.copytree(old, forged)
    manifest = json.loads((forged / "adapter.json").read_text())
    manifest["patches"] = {"eventhub-log-guard": {"file": "EventHub.java", "sha256": "0" * 64}}
    manifest["command"][2] = f"{forged}/classes{os.pathsep}{forged}/lib/*"
    (forged / "adapter.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="missing: eventhub-log-guard"):
        _verify_bundle(forged, java)
