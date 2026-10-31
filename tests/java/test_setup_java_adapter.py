"""Bootstrap guards require no JDK or network: peeky.java_setup, which `peeky setup-java`
and scripts/setup_java_adapter.py run."""

import io
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def bootstrap():
    from peeky import java_setup

    return java_setup


def test_setup_rejects_existing_and_dangling_output(bootstrap, tmp_path):
    with pytest.raises(ValueError, match="already exists"):
        bootstrap.setup(tmp_path)
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="already exists"):
        bootstrap.setup(link)


def test_bootstrap_strips_worker_token(bootstrap, monkeypatch):
    monkeypatch.setenv("DEBUG_TOKEN", "test-worker-secret-0123456789abcdef")
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", "test-client-secret-0123456789abcdef")
    script = "import os; print(sorted({'DEBUG_TOKEN', 'PEEKY_WORKER_TOKEN'} & set(os.environ)))"
    assert bootstrap.run([sys.executable, "-c", script]) == "[]"


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
    # java-debug's event thread died building a log line nobody reads.
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


def fake_tools(java):
    from peeky.java_setup import java_identity

    return {
        "java": java,
        "java_version": "17",
        "javac": java,
        "javac_version": "17",
        **java_identity(java),
    }


def fake_bundle(root, event_hub, patches=None):
    """A bundle that passes every other check: its command, inventory, checksums and recipe
    (with fake_tools for the JDK, which java_setup.toolchain must then return)."""
    import hashlib
    import json
    import os

    from peeky.java import _tool
    from peeky.java_setup import fingerprint, recipe

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
        "recipe": recipe(fake_tools(java)),
        "fingerprint": fingerprint(recipe(fake_tools(java))),
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
    assert "eventhub-log-guard" in str(refused.value) and "`peeky setup-java`" in str(refused.value)
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
def test_the_compiled_patch_is_what_counts(tmp_path, patches, monkeypatch):
    # The `patches` record is documentation; the recipe holds each patch's text.
    from peeky import java_setup
    from peeky.java import _verify_bundle

    java = fake_bundle(tmp_path, PATCHED_EVENT_HUB, patches)
    monkeypatch.setattr(java_setup, "toolchain", lambda java, javac: fake_tools(java))
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


MAIN = """package demo;
public class Main {
    public static void main(String[] args) {
        int x = 1;
        System.out.println(x);
    }
}
"""


@pytest.fixture
def own_bundle(tmp_path, monkeypatch):
    """Install copies of the real bundle as versions of Peeky's own, as setup does (each
    command names its version's path), pointing at the last; ``edit`` changes its recipe,
    with a fingerprint to match."""
    import os
    import shutil

    from peeky import java_setup

    configured = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not configured:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a prepared Java adapter bundle")
    state = tmp_path / "state"
    container = state / "java-adapter"
    container.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(state))
    (tmp_path / "app" / "demo").mkdir(parents=True)
    (tmp_path / "app" / "demo" / "Main.java").write_text(MAIN)

    def add(name, edit=None):
        version = container / name
        shutil.copytree(configured, version)
        version.chmod(0o700)
        manifest = json.loads((version / "adapter.json").read_text())
        resolved = version.resolve()
        manifest["command"][2] = f"{resolved}/classes{os.pathsep}{resolved}/lib/*"
        if edit is not None:
            edit(manifest["recipe"])
            manifest["fingerprint"] = java_setup.fingerprint(manifest["recipe"])
        (version / "adapter.json").write_text(json.dumps(manifest))
        java_setup._point(container, name)
        return resolved

    return add


def prepare_own(tmp_path):
    from peeky.java import JavaTarget
    from peeky.java_setup import default_bundle

    target = JavaTarget(default_bundle(), ("demo/Main.java",), "demo.Main")
    return target.prepare(tmp_path / "app" / "demo" / "Main.java", tmp_path / "app")


def test_a_session_keeps_its_version_until_prune_removes_it(own_bundle, tmp_path):
    from peeky import java_setup
    from peeky.errors import classify
    from peeky.java import bundle_directory

    first = own_bundle("a" * 20 + "-" + "1" * 8)
    session = prepare_own(tmp_path)
    try:
        assert session.bundle == first
        second = own_bundle("b" * 20 + "-" + "2" * 8)
        # A new session gets the new version; this one keeps its own, unchanged.
        assert bundle_directory(java_setup.default_bundle()) == second
        session.assert_current()
        assert java_setup.prune()["pruned"] == [first.name]
        with pytest.raises(RuntimeError, match="provenance mismatch") as gone:
            session.assert_current()
        assert classify(gone.value)["code"] == "PROVENANCE_CHANGED"
    finally:
        session.prepared.cleanup()


def test_the_worker_runs_a_bundle_whatever_javac_it_records_but_not_another_launcher(
    own_bundle, tmp_path
):
    # The JDK isn't asked at run time: the JVM refuses classes it can't run (--release 17).
    own_bundle("c" * 20 + "-" + "3" * 8, lambda recipe: recipe.update(javac_version="javac 99"))
    session = prepare_own(tmp_path)
    session.prepared.cleanup()
    own_bundle("d" * 20 + "-" + "4" * 8, lambda recipe: recipe.update(launcher_sha256="0" * 64))
    with pytest.raises(ValueError, match="is not what this Peeky's setup builds") as other:
        prepare_own(tmp_path)
    assert "(it was built by another version of Peeky)" in str(other.value)
