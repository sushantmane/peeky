"""`peeky setup-java` builds the Java adapter from the installed package, and Java targets
whose config names no bundle find the one it builds. A wheel has no scripts/ or adapters/
directory: this needs no JDK, network or checkout.

Peeky's own bundle is only yours (every directory on the way, the pointer and the lock),
each build is an immutable version switched to atomically under a lock, and a bundle is
used or kept only while its exact recipe (patch text, launcher, lock, flags, JDK) is the one
this Peeky builds."""

import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import threading
import time
import tomllib
from fnmatch import fnmatch
from pathlib import Path
from types import SimpleNamespace

import pytest

import peeky.java
from peeky import cli, java_setup
from peeky.errors import classify
from peeky.java import _verify_bundle, bundle_directory

ROOT = Path(__file__).resolve().parents[2]
EVENT_HUB_CLASS = "classes/com/microsoft/java/debug/core/EventHub.class"
UPSTREAM_EVENT_HUB = """\
                for (Event event : set) {
                        try {
                            logger.fine("\\nJDI Event: " + event + "\\n");
                        } catch (VMDisconnectedException e) {
                            // do nothing
                        }
"""


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


def test_the_adapter_sources_ship_in_the_package():
    # The wheel holds only src/peeky: what setup builds from must be package data.
    for path in (java_setup.LAUNCHER, java_setup.LOCK, java_setup.NOTICES):
        assert path.is_file() and path.parent == Path(java_setup.__file__).parent / "java_adapter"
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"][
        "package-data"
    ]["peeky"]
    for path in (java_setup.LAUNCHER, java_setup.LOCK, java_setup.NOTICES):
        assert any(fnmatch(f"java_adapter/{path.name}", pattern) for pattern in patterns)
    assert json.loads(java_setup.LOCK.read_text())["source"]["sha256"]


def test_the_default_bundle_is_in_peekys_state_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "elsewhere"))
    assert java_setup.default_bundle() == tmp_path / "elsewhere" / "java-adapter"
    monkeypatch.delenv("PEEKY_STATE_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert java_setup.default_bundle() == tmp_path / "home" / ".peeky" / "java-adapter"


def java_target(root, **config):
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "Main.java").write_text("package demo;\nclass Main {}\n")
    path = root / "target.json"
    path.write_text(
        json.dumps({"sources": ["demo/Main.java"], "main_class": "demo.Main", **config})
    )
    return path


def test_a_config_without_a_bundle_uses_the_default_one(tmp_path, state):
    config = java_target(tmp_path / "app")
    data = cli.resolve(tmp_path / "app", "demo/Main.java:2", str(config))[4]
    assert data["bundle"] == str(state / "java-adapter")
    assert cli.adapter_for("java", data).bundle == (state / "java-adapter").resolve()
    # A bundle the config names still wins, relative to the config.
    named = java_target(tmp_path / "named", bundle="adapter")
    data = cli.resolve(tmp_path / "named", "demo/Main.java:2", str(named))[4]
    assert data["bundle"] == str((tmp_path / "named" / "adapter").resolve())


def test_the_workers_java_configs_use_the_default_bundle_too(tmp_path, state):
    from peeky.worker import load_java_attach_target, load_java_target

    config = java_target(tmp_path / "app")
    assert load_java_target(config).bundle == state / "java-adapter"
    attach = tmp_path / "attach.json"
    attach.write_text(
        json.dumps({"port": 5005, "sources": ["demo/Main.java"], "class_roots": ["classes"]})
    )
    assert load_java_attach_target(attach).bundle == state / "java-adapter"
    named = java_target(tmp_path / "named", bundle="../adapter")
    assert load_java_target(named).bundle == (tmp_path / "adapter").resolve()
    for bundle in ("", 7):
        with pytest.raises(ValueError, match="Java bundle must be a nonempty path string"):
            load_java_target(java_target(tmp_path / f"bad{bundle}", bundle=bundle))


def test_a_missing_bundle_says_how_to_build_it(tmp_path, state):
    from peeky.errors import classify
    from peeky.java import bundle_directory

    with pytest.raises(ValueError, match="No Java adapter bundle") as missing:
        bundle_directory(state / "java-adapter")
    message = str(missing.value)
    assert "names no bundle: build it once with `peeky setup-java`" in message
    error = classify(missing.value)
    assert error["code"] == "INVALID_REQUEST" and "`peeky setup-java`" in error["hint"]
    named = tmp_path / "work" / "adapter"
    with pytest.raises(ValueError, match="which the config names") as missing:
        bundle_directory(named)
    assert f"`peeky setup-java --output {named}`" in str(missing.value)
    named.mkdir(parents=True)
    assert bundle_directory(named) == named.resolve()


def test_a_java_probe_without_a_built_adapter_names_the_setup_command(tmp_path, state, capsys):
    config = java_target(tmp_path / "app")
    status = cli.main(
        ["probe", str(tmp_path / "app"), "--config", str(config), "--at", "demo/Main.java:2"]
    )
    shown = capsys.readouterr().out
    assert status == 2
    assert f"[INVALID_REQUEST]: No Java adapter bundle at {state / 'java-adapter'}" in shown
    assert "build it once with `peeky setup-java`" in shown


def source_archive(event_hub=UPSTREAM_EVENT_HUB):
    """java-debug's source archive, as far as setup reads it."""
    files = {
        f"java-debug-x/{java_setup.EVENT_HUB}": event_hub,
        "java-debug-x/com.microsoft.java.debug.core/src/main/java/A.java": "class A {}\n",
        "java-debug-x/LICENSE.txt": "EPL-1.0\n",
        "java-debug-x/ThirdPartyNotices.txt": "notices\n",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


JAVA, JAVAC = "/opt/jdk/bin/java", "/opt/jdk/bin/javac"


@pytest.fixture
def offline(monkeypatch):
    """Setup with its downloads and JDK faked: javac "compiles" the patched EventHub and the
    launcher. ``versions`` is what each tool prints for -version; ``builds`` counts builds."""
    jdk = SimpleNamespace(
        versions={JAVA: 'openjdk version "17.0.1"', JAVAC: "javac 17.0.1"},
        builds=0,
        fail=False,
        commands=[],
    )

    def acquire(item, deadline=None):
        if jdk.fail:
            raise ValueError(f"download checksum/size mismatch: {item['url']}")
        return source_archive() if item["url"].startswith("https://codeload") else b"PK jar"

    def run(command):
        jdk.commands.append(command)
        if command[-1] == "-version":
            return jdk.versions[command[0]]
        classes = Path(command[command.index("-d") + 1])
        if command[-1].startswith("@"):
            jdk.builds += 1
            hub = classes / EVENT_HUB_CLASS.removeprefix("classes/")
            hub.parent.mkdir(parents=True, exist_ok=True)
            hub.write_bytes(b"\xca\xfe java/util/logging/Logger isLoggable")
        else:
            (classes / "PeekyJavaAdapter.class").write_bytes(b"\xca\xfe launcher")
        return ""

    monkeypatch.setattr(java_setup, "acquire", acquire)
    monkeypatch.setattr(java_setup, "run", run)
    monkeypatch.setattr(java_setup.shutil, "which", lambda name: f"/opt/jdk/bin/{name}")
    # The fake JDK's paths stand for themselves where the worker resolves `java`.
    monkeypatch.setattr(peeky.java, "_tool", lambda value: value)
    return jdk


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


# --- setup --output: one plain bundle, with its provenance and exact recipe ---------------


def test_setup_builds_a_bundle_with_its_provenance_recipe_and_notices(tmp_path, offline):
    output = tmp_path / "out" / "adapter"
    manifest = java_setup.setup(output)
    assert json.loads((output / "adapter.json").read_text()) == manifest
    recipe = manifest["recipe"]
    assert manifest["fingerprint"] == java_setup.fingerprint(recipe)
    assert recipe == java_setup.recipe(
        {
            "java": JAVA,
            "java_version": offline.versions[JAVA],
            "javac": JAVAC,
            "javac_version": offline.versions[JAVAC],
        }
    )
    assert (
        recipe["launcher_sha256"]
        == manifest["launcher_sha256"]
        == java_setup.digest(java_setup.LAUNCHER)
    )
    assert recipe["dependency_lock_sha256"] == java_setup.digest(java_setup.LOCK)
    assert recipe["javac_flags"] == ["--release", "17"]
    assert (
        recipe["patches"]
        == java_setup.patch_texts()
        == {"eventhub-log-guard": manifest["patches"]["eventhub-log-guard"]["text_sha256"]}
    )
    assert manifest["command"][2] == (
        f"{output.resolve()}/classes{os.pathsep}{output.resolve()}/lib/*"
    )
    for name in ("LICENSE.txt", "ThirdPartyNotices.txt", "PEEKY-NOTICES.md"):
        assert f"notices/{name}" in manifest["files"]
    # The flags recorded are the ones javac ran with, on the packaged launcher.
    compiles = [c for c in offline.commands if "-d" in c]
    assert all(c[1:3] == ["--release", "17"] for c in compiles)
    assert compiles[-1][-1] == str(java_setup.LAUNCHER)
    assert sorted(p.name for p in output.parent.iterdir()) == ["adapter"]
    assert _verify_bundle(output.resolve(), JAVA)[0][-1] == "PeekyJavaAdapter"


def test_setup_needs_a_new_directory_and_removes_its_staging_when_it_fails(tmp_path, offline):
    with pytest.raises(ValueError, match="already exists; choose a fresh directory"):
        java_setup.setup(tmp_path)
    offline.fail = True
    with pytest.raises(ValueError, match="Java adapter setup failed, and nothing was installed"):
        java_setup.setup(tmp_path / "adapter")
    assert list(tmp_path.iterdir()) == []


# --- downloads: bounded in size and in total time -----------------------------------------


class Dripping:
    """A response that never ends: ``size`` bytes per read, after ``pause`` seconds."""

    def __init__(self, size, pause=0.0):
        self.size, self.pause = size, pause

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read1(self, limit):
        time.sleep(self.pause)
        return b"x" * min(self.size, limit)


def test_downloads_have_a_total_deadline_and_a_size_bound(monkeypatch):
    item = {"url": "https://example.invalid/pinned", "sha256": "0" * 64}
    monkeypatch.setattr(java_setup.urllib.request, "urlopen", lambda *a, **k: Dripping(10, 0.02))
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="downloads exceeded their 300 second deadline"):
        java_setup.acquire(item, deadline=time.monotonic() + 0.3)
    assert time.monotonic() - started < 2
    with pytest.raises(TimeoutError, match="deadline"):
        java_setup.acquire(item, deadline=time.monotonic() - 1)
    monkeypatch.setattr(java_setup.urllib.request, "urlopen", lambda *a, **k: Dripping(65536))
    with pytest.raises(ValueError, match=f"is over {java_setup.MAX_DOWNLOAD} bytes"):
        java_setup.acquire(item)


# --- Peeky's own bundle: versions, a pointer, a lock --------------------------------------


def test_install_builds_a_version_only_you_can_use_and_points_at_it(state, offline):
    result = java_setup.install()
    container = state / "java-adapter"
    version = Path(result["bundle"])
    assert result["built"] and version.parent == container.resolve()
    assert java_setup.VERSION.fullmatch(version.name)
    assert version.name.startswith(result["adapter"]["fingerprint"][:20])
    assert (container / "current").read_text() == version.name + "\n"
    assert sorted(p.name for p in container.iterdir()) == sorted([".lock", "current", version.name])
    assert mode(state) == mode(container) == mode(version) == 0o700
    assert mode(container / "current") == mode(container / ".lock") == 0o600
    assert java_setup.installed() == container / version.name
    # A session resolves the pointer once, to the version.
    assert bundle_directory(container) == version
    assert bundle_directory((state / "java-adapter").resolve()) == version


def test_install_keeps_a_version_with_the_same_recipe(state, offline):
    first = java_setup.install()
    builds = offline.builds
    again = java_setup.install()
    assert (again["built"], again["bundle"], offline.builds) == (False, first["bundle"], builds)
    forced = java_setup.install(force=True)
    assert forced["built"] and forced["bundle"] != first["bundle"] and offline.builds > builds


def test_a_new_version_is_switched_to_once_built_and_the_one_before_is_kept(
    state, offline, monkeypatch
):
    versions = []
    for schema in (1, 2, 3):
        monkeypatch.setattr(java_setup, "RECIPE_SCHEMA", schema)
        versions.append(Path(java_setup.install()["bundle"]).name)
    container = state / "java-adapter"
    # The previous version stays for sessions still using it; the one before goes.
    assert sorted(p.name for p in container.iterdir() if java_setup.VERSION.fullmatch(p.name)) == (
        sorted(versions[1:])
    )
    assert java_setup.installed().name == versions[2]


def test_a_session_keeps_the_version_it_started_with(state, offline):
    java_setup.install()
    started = bundle_directory(state / "java-adapter")
    command, manifest_hash = _verify_bundle(started, JAVA)
    newer = Path(java_setup.install(force=True)["bundle"])
    # The session's version is untouched, and verifies as before; new sessions get the new one.
    assert _verify_bundle(started, JAVA) == (command, manifest_hash)
    assert bundle_directory(state / "java-adapter") == newer != started


def test_a_setup_that_dies_before_the_switch_leaves_the_old_version_in_use(
    state, offline, monkeypatch
):
    old = Path(java_setup.install()["bundle"])
    container = state / "java-adapter"

    def crash(container, name):
        raise OSError("killed before the switch")

    with monkeypatch.context() as patched:
        patched.setattr(java_setup, "_point", crash)
        with pytest.raises(ValueError, match="nothing was installed: killed before the switch"):
            java_setup.install(force=True)
    assert java_setup.installed() == container / old.name
    assert _verify_bundle(old, JAVA)
    # What setups that died left: a version built but never switched to, a staging
    # directory and a pointer draft. The next setup removes them, under the lock.
    (orphan,) = [
        p for p in container.iterdir() if java_setup.VERSION.fullmatch(p.name) and p != old
    ]
    (container / ".peeky-java-dead").mkdir()
    (container / ".current.dead").write_text("x")
    newest = Path(java_setup.install(force=True)["bundle"])
    assert sorted(p.name for p in container.iterdir()) == sorted(
        [".lock", "current", old.name, newest.name]
    )
    assert not orphan.exists()


def test_setups_wait_for_each_other(state, offline):
    container = state / "java-adapter"
    java_setup.install()
    messages, results = [], []
    with java_setup._locked(container):
        worker = threading.Thread(
            target=lambda: results.append(java_setup.install(force=True, progress=messages.append))
        )
        worker.start()
        deadline = time.monotonic() + 5
        while not messages and time.monotonic() < deadline:
            time.sleep(0.02)
        assert messages == ["Waiting for another `peeky setup-java` to finish."]
        assert worker.is_alive() and results == []
    worker.join(10)
    assert results and results[0]["built"]


def test_a_setup_gives_up_on_a_lock_held_too_long(state, offline, monkeypatch):
    container = state / "java-adapter"
    java_setup.install()
    monkeypatch.setattr(java_setup, "LOCK_SECONDS", 0.2)
    with java_setup._locked(container):
        with pytest.raises(ValueError, match="another `peeky setup-java` held") as held:
            java_setup.install(force=True)
    assert classify(held.value)["code"] == "UNSUPPORTED"


# --- only yours: the state directory, the bundle's, each version, the pointer, the lock ----


def refused(path_text, call):
    with pytest.raises(ValueError, match="^Peeky's own Java adapter can't be used: ") as caught:
        call()
    assert path_text in str(caught.value), str(caught.value)
    error = classify(caught.value)
    assert error["code"] == "UNSUPPORTED" and "PEEKY_STATE_DIR" in error["hint"]
    return str(caught.value)


def test_a_bundle_in_a_directory_others_can_use_is_refused(state, offline):
    version = Path(java_setup.install()["bundle"])
    container = state / "java-adapter"
    for path in (state, container, version):
        path.chmod(0o755)
        message = refused(str(path), lambda: bundle_directory(container))
        assert "has mode 755, open to other users" in message
        path.chmod(0o700)
    # Setup makes the state directory and the bundle's its own again.
    state.chmod(0o750)
    container.chmod(0o711)
    java_setup.install()
    assert mode(state) == mode(container) == 0o700


def test_links_and_other_users_directories_are_refused(state, offline, tmp_path, monkeypatch):
    version = Path(java_setup.install()["bundle"])
    container = state / "java-adapter"
    # A version that is a link.
    real = tmp_path / "elsewhere"
    version.rename(real)
    version.symlink_to(real)
    refused(str(version), lambda: bundle_directory(container))
    version.unlink()
    real.rename(version)
    # Another user's: the owner check, with this process standing for someone else.
    uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    message = refused(str(state), lambda: bundle_directory(container))
    assert "a link, a file or another user's" in message
    monkeypatch.setattr(os, "getuid", lambda: uid)
    # A state directory that is a link: lookup and setup refuse it.
    moved = tmp_path / "moved-state"
    state.rename(moved)
    state.symlink_to(moved)
    refused(str(state), lambda: bundle_directory(container))
    refused(str(state), java_setup.install)
    with pytest.raises(ValueError, match="state directory must be a directory you own"):
        cli._state_base()


def test_a_bundle_under_a_directory_others_can_write_is_refused(tmp_path, offline, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("PEEKY_STATE_DIR", str(shared / "state"))
    java_setup.install()
    shared.chmod(0o777)
    try:
        message = refused(str(shared), lambda: bundle_directory(shared / "state/java-adapter"))
        assert "is writable by other users, who could replace the adapter" in message
        refused(str(shared), java_setup.install)
    finally:
        shared.chmod(0o700)


def test_the_pointer_must_be_a_file_of_yours_naming_a_version(state, offline, tmp_path):
    version = Path(java_setup.install()["bundle"])
    container = state / "java-adapter"
    pointer = container / "current"
    for text, chmod in ((version.name, 0o644), ("../../elsewhere", 0o600), ("", 0o600)):
        pointer.write_text(text)
        pointer.chmod(chmod)
        refused("must be a file of yours, mode 0600, naming a version", java_setup.installed)
    pointer.unlink()
    pointer.symlink_to(tmp_path / "anywhere")
    refused(str(pointer), java_setup.installed)
    pointer.unlink()
    pointer.write_text("0" * 20 + "-" + "0" * 8 + "\n")
    pointer.chmod(0o600)
    refused("which is gone. Run `peeky setup-java`", lambda: bundle_directory(container))
    # Setup replaces a broken pointer with a new version.
    result = java_setup.install()
    assert result["built"] and java_setup.installed() == container / Path(result["bundle"]).name


def test_a_lock_that_is_a_link_is_refused(state, offline, tmp_path):
    java_setup.install()
    lock = state / "java-adapter/.lock"
    lock.unlink()
    lock.symlink_to(tmp_path / "elsewhere")
    refused(str(lock), java_setup.install)
    assert not (tmp_path / "elsewhere").exists()


def test_the_state_directory_is_made_only_yours(state):
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    assert cli._state_base() == state and mode(state) == 0o700


# --- the exact recipe: equal or rebuild; the worker checks the same -----------------------


def test_the_fingerprint_changes_with_every_input(tmp_path, offline, monkeypatch):
    tools = java_setup.toolchain(JAVA, JAVAC)
    base = java_setup.fingerprint(java_setup.recipe(tools))
    changed = []
    for key in java_setup.TOOL_KEYS:
        changed.append(java_setup.fingerprint(java_setup.recipe({**tools, key: tools[key] + "x"})))

    def edited(name, path):
        copy = tmp_path / path.name
        copy.write_bytes(path.read_bytes() + b"\n")
        monkeypatch.setattr(java_setup, name, copy)
        changed.append(java_setup.fingerprint(java_setup.recipe(tools)))
        monkeypatch.setattr(java_setup, name, path)

    for name in ("LAUNCHER", "LOCK", "NOTICES"):
        edited(name, getattr(java_setup, name))
    for name, value in (
        ("RECIPE_SCHEMA", 2),
        ("JAVAC_FLAGS", ("--release", "21")),
        ("PATCHES", {**java_setup.PATCHES, "eventhub-log-guard": ("a", "b", "c")}),
        ("PATCHES", {**java_setup.PATCHES, "another": ("a", "b", "c")}),
    ):
        with monkeypatch.context() as patched:
            patched.setattr(java_setup, name, value)
            changed.append(java_setup.fingerprint(java_setup.recipe(tools)))
    assert base not in changed and len(set(changed)) == len(changed) == 11


def test_a_changed_patch_text_or_jdk_rebuilds_and_the_worker_refuses_the_old_bundle(
    state, offline, monkeypatch
):
    first = Path(java_setup.install()["bundle"])
    # The patch's text changed, while the compiled class still has the byte probe's marker.
    original, patched = java_setup.EVENT_HUB_ORIGINAL, java_setup.EVENT_HUB_PATCHED
    with monkeypatch.context() as changed:
        changed.setitem(
            java_setup.PATCHES,
            "eventhub-log-guard",
            (java_setup.EVENT_HUB, original, patched.replace("RuntimeException", "Exception")),
        )
        with pytest.raises(ValueError, match="is not what this Peeky's setup builds") as other:
            _verify_bundle(first, JAVA)
        assert "(it was built by another version of Peeky)" in str(other.value)
        assert classify(other.value)["code"] == "UNSUPPORTED"
        wanted = java_setup.recipe(java_setup.toolchain(JAVA, JAVAC))
        assert java_setup.current(first, wanted) == "it was built by another version of Peeky"
    assert _verify_bundle(first, JAVA)
    # The JDK changed: javac's version text differs from the one the bundle records.
    offline.versions[JAVAC] = "javac 17.0.2"
    with pytest.raises(ValueError, match="built with another JDK, or the JDK changed since"):
        _verify_bundle(first, JAVA)
    messages = []
    second = java_setup.install(progress=messages.append)
    assert second["built"] and Path(second["bundle"]) != first
    assert "(it was built with another JDK, or the JDK changed since)" in messages[0]
    assert _verify_bundle(Path(second["bundle"]), JAVA)


def test_a_bundle_without_an_exact_recipe_is_refused(tmp_path, offline):
    bundle = tmp_path / "bundle"
    manifest = java_setup.setup(bundle)
    path = bundle / "adapter.json"

    def other_flags(edited):
        edited["recipe"]["javac_flags"] = ["--release", "21"]

    def other_flags_and_fingerprint(edited):
        other_flags(edited)
        edited["fingerprint"] = java_setup.fingerprint(edited["recipe"])

    def other_java(edited):
        edited["recipe"]["java"] = "/elsewhere/java"
        edited["fingerprint"] = java_setup.fingerprint(edited["recipe"])

    for change, why in (
        (lambda m: m.pop("recipe"), "it records no build recipe: an earlier Peeky built it"),
        (lambda m: m.pop("fingerprint"), "its recipe doesn't match its fingerprint"),
        (other_flags, "its recipe doesn't match its fingerprint"),
        (other_flags_and_fingerprint, "it was built by another version of Peeky"),
        (other_java, "its recipe names another java than its command"),
    ):
        edited = json.loads(json.dumps(manifest))
        change(edited)
        path.write_text(json.dumps(edited))
        with pytest.raises(ValueError, match="is not what this Peeky's setup builds") as other:
            _verify_bundle(bundle.resolve(), JAVA)
        assert f"({why})" in str(other.value)


@pytest.mark.parametrize("text", ["[]", '"bundle"', "7", "not json", "null"])
def test_an_adapter_json_that_is_not_an_object_is_stale_not_an_internal_error(state, offline, text):
    version = Path(java_setup.install()["bundle"])
    (version / "adapter.json").write_text(text)
    with pytest.raises(ValueError, match="is not what this Peeky's setup builds") as stale:
        _verify_bundle(version, JAVA)
    assert classify(stale.value)["code"] == "UNSUPPORTED"
    wanted = java_setup.recipe(java_setup.toolchain(JAVA, JAVAC))
    assert java_setup.current(version, wanted).startswith("it does not verify: its adapter.json")
    assert java_setup.install()["built"]


def test_an_unpatched_bundle_is_called_stale_by_setup(state, offline):
    version = Path(java_setup.install()["bundle"])
    hub = version / EVENT_HUB_CLASS
    hub.write_bytes(b"\xca\xfe Logger fine")
    manifest = json.loads((version / "adapter.json").read_text())
    manifest["files"][EVENT_HUB_CLASS] = java_setup.digest(hub)
    (version / "adapter.json").write_text(json.dumps(manifest))
    wanted = java_setup.recipe(java_setup.toolchain(JAVA, JAVAC))
    assert java_setup.current(version, wanted) == "it predates Peeky's java-debug fixes"


# --- the command ----------------------------------------------------------------------------


def setup_java(capsys, *args):
    status = cli.main(["setup-java", *args])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_setup_java_installs_peekys_own_bundle(state, offline, capsys):
    status, out, err = setup_java(capsys, "--json")
    assert status == 0, err
    result = json.loads(out)
    version = java_setup.installed()
    assert (result["bundle"], result["built"], result["default"]) == (str(version), True, True)
    assert result["adapter"] == json.loads((version / "adapter.json").read_text())
    assert f"Building the Java adapter in {state / 'java-adapter'}" in err
    status, out, err = setup_java(capsys)
    assert status == 0 and f"The Java adapter at {version} is up to date: nothing to do" in out


def test_setup_java_with_output_needs_a_new_directory(tmp_path, state, offline, capsys):
    status, out, err = setup_java(capsys, "--output", str(tmp_path / "mine"), "--quiet")
    assert status == 0 and err == ""
    assert 'Name it as "bundle" in a Java target\'s config' in out
    assert (tmp_path / "mine" / "adapter.json").is_file()
    assert not (state / "java-adapter").exists()
    status, out, err = setup_java(capsys, "--output", str(tmp_path / "mine"))
    assert status == 2 and "[INVALID_REQUEST]" in err and "already exists" in err
    status, out, err = setup_java(capsys, "--output", str(tmp_path / "new"), "--force")
    assert status == 2 and "--force replaces only Peeky's own bundle" in err
    assert not (tmp_path / "new").exists()
    for inside in (state / "java-adapter", state / "java-adapter" / "x"):
        status, out, err = setup_java(capsys, "--output", str(inside))
        assert status == 2 and "is in Peeky's own bundle's directory" in err
    assert not (state / "java-adapter").exists()


def test_setup_java_without_a_jdk_installs_nothing(state, monkeypatch, capsys):
    monkeypatch.setattr(java_setup.shutil, "which", lambda name: None)
    status, out, err = setup_java(capsys, "--quiet")
    assert status == 2
    assert "[UNSUPPORTED] JDK required: java and javac (JDK 17 or later) must be on PATH" in err
    assert "Install JDK 17 or later" in err
    assert java_setup.installed() is None


def test_the_script_runs_this_checkouts_setup_without_peeky_installed(tmp_path):
    # -S: no site-packages, so no installed Peeky; the script finds this checkout's.
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(ROOT / "scripts/setup_java_adapter.py"),
            "--output",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "already exists; choose a fresh directory" in result.stderr
