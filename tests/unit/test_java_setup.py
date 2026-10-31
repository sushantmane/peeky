"""`peeky setup-java` builds the Java adapter from the installed package, and Java targets
whose config names no bundle find the one it builds. A wheel has no scripts/ or adapters/
directory: this needs no JDK, network or checkout.

Peeky's own bundle is only yours (every directory on the way, the pointer and the lock),
each build is an immutable version switched to atomically under a lock, versions go only
with `--prune`, and a bundle runs only while its recipe (patch text, launcher, lock, notices,
flags) is this Peeky's; setup also rebuilds for another javac."""

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

    def run(command, env=None):
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
    assert recipe == java_setup.recipe({"javac_version": offline.versions[JAVAC]})
    assert "java" not in recipe and recipe["javac_version"] == offline.versions[JAVAC]
    assert manifest["java_version"] == offline.versions[JAVA]
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


def test_versions_stay_until_prune_removes_all_but_the_current_one(state, offline, monkeypatch):
    versions = []
    for schema in (1, 2, 3):
        monkeypatch.setattr(java_setup, "RECIPE_SCHEMA", schema)
        result = java_setup.install()
        versions.append(Path(result["bundle"]).name)
        # Setup removes no version: sessions may still use any of them.
        assert result["older"] == sorted(versions[:-1])
    container = state / "java-adapter"
    assert java_setup._versions(container) == sorted(versions)
    assert java_setup.installed().name == versions[2]
    (container / ".peeky-java-dead").mkdir()
    pruned = java_setup.prune()
    assert pruned == {
        "bundle": str(container.resolve() / versions[2]),
        "pruned": sorted(versions[:2]),
        "failed": {},
    }
    assert sorted(p.name for p in container.iterdir()) == sorted([".lock", "current", versions[2]])
    assert java_setup.prune()["pruned"] == []


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
    assert peeky.java._bundle(old, JAVA)
    # What setups that died left: a version built but never switched to, a staging
    # directory and a pointer draft. The next setup removes the staging and the draft,
    # under the lock; versions go only with prune.
    (orphan,) = [
        p for p in container.iterdir() if java_setup.VERSION.fullmatch(p.name) and p != old
    ]
    (container / ".peeky-java-dead").mkdir()
    (container / ".current.dead").write_text("x")
    newest = Path(java_setup.install(force=True)["bundle"])
    assert sorted(p.name for p in container.iterdir()) == sorted(
        [".lock", "current", old.name, orphan.name, newest.name]
    )
    java_setup.prune()
    assert java_setup._versions(container) == [newest.name]


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
    modes = ((version.name, 0o644), (version.name, 0o400), (version.name, 0o700))
    for text, chmod in (*modes, ("../../elsewhere", 0o600), ("", 0o600)):
        pointer.chmod(0o600)
        pointer.write_text(text)
        pointer.chmod(chmod)
        refused("must be a file of yours, mode 0600, naming a version", java_setup.installed)
    # One this user can't open (root opens any).
    pointer.chmod(0)
    refused("must be a file of yours, mode 0600, naming a version", java_setup.installed)
    pointer.chmod(0o600)
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


@pytest.mark.parametrize("home_mode", [0o755, 0o700])
def test_a_state_directory_of_root_or_home_is_refused_never_tightened(
    tmp_path, offline, monkeypatch, home_mode
):
    # The state directory is checked whenever it exists, before java-adapter is looked for.
    monkeypatch.setenv("PEEKY_STATE_DIR", "/")
    before = mode("/")
    for call in (java_setup.installed, lambda: bundle_directory(Path("/java-adapter"))):
        refused("/", call)
    refused("/", java_setup.install)
    refused("/", java_setup.prune)
    assert mode("/") == before and not os.path.lexists("/java-adapter")
    # The home directory, even only yours (0700): it is never taken for Peeky's own.
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(home_mode)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PEEKY_STATE_DIR", str(home))
    for call in (java_setup.install, java_setup.installed, java_setup.prune):
        message = refused(str(home), call)
        assert "is / or your home directory, which Peeky never takes for its own" in message
    with pytest.raises(ValueError, match="can't be / or your home directory, whatever its mode"):
        cli._state_base()
    assert mode(home) == home_mode and list(home.iterdir()) == []


@pytest.mark.skipif(os.getuid() == 0, reason="root reads any directory")
def test_a_state_directory_that_cant_be_read_is_refused(tmp_path, offline, monkeypatch):
    locked = tmp_path / "locked"
    (locked / "state").mkdir(parents=True)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(locked / "state"))
    locked.chmod(0)
    try:
        message = refused(str(locked / "state"), java_setup.installed)
        assert "can't be read (Permission denied)" in message
    finally:
        locked.chmod(0o700)


def test_a_lock_that_cant_be_opened_is_refused(state, offline):
    java_setup.install()
    lock = state / "java-adapter/.lock"
    lock.unlink()
    lock.mkdir()
    message = refused(str(lock), java_setup.install)
    assert "is a link, or can't be opened" in message


def test_untrusted_directories_are_refused_before_a_target_builds(state, offline, tmp_path):
    java_setup.install()
    source = program(tmp_path / "app")
    (state / "java-adapter").chmod(0o750)
    refused(
        str(state / "java-adapter"), lambda: own_target(state).prepare(source, tmp_path / "app")
    )
    assert not [c for c in offline.commands if "-g" in c]


def test_a_session_on_a_pruned_version_fails_its_next_step(state, offline, monkeypatch, tmp_path):
    # JavaSession.assert_current, which runs before every operation, as a session that
    # started on the first version has it.
    from peeky.java import JavaSession

    first = bundle_directory(Path(java_setup.install()["bundle"]).parent)
    command, bundle_hash = peeky.java._bundle(first, JAVA)
    mapping = tmp_path / "map.json"
    mapping.write_text("{}")
    session = SimpleNamespace(
        prepared=None,
        bundle=first,
        java=JAVA,
        adapter_java=JAVA,
        bundle_hash=bundle_hash,
        command=(*command, str(mapping)),
        mapping_path=mapping,
        mapping_hash=peeky.java._hash(mapping),
    )
    monkeypatch.setattr(peeky.java, "assert_current", lambda prepared: None)
    java_setup.install(force=True)
    JavaSession.assert_current(session)
    assert java_setup.prune()["pruned"] == [first.name]
    with pytest.raises(RuntimeError, match="Java runtime provenance mismatch") as gone:
        JavaSession.assert_current(session)
    assert classify(gone.value)["code"] == "PROVENANCE_CHANGED"


# --- the exact recipe: equal or rebuild; the worker checks the same -----------------------


def test_the_fingerprint_changes_with_every_input(tmp_path, offline, monkeypatch):
    tools = java_setup.toolchain(JAVA, JAVAC)
    base = java_setup.fingerprint(java_setup.recipe(tools))
    changed = []
    for key in java_setup.JDK_KEYS:
        changed.append(java_setup.fingerprint(java_setup.recipe({**tools, key: tools[key] + "x"})))
    # java itself, and javac's path, are not part of it: the worker never asks them.
    other = {**tools, "java": "/elsewhere/java", "java_version": "x", "javac": "/elsewhere/javac"}
    assert java_setup.fingerprint(java_setup.recipe(other)) == base

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
    assert base not in changed and len(set(changed)) == len(changed) == 8


def program(root):
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "Main.java").write_text("package demo;\nclass Main {}\n")
    return root / "demo" / "Main.java"


def own_target(state):
    """A target that uses Peeky's own bundle, run by the fake JDK."""
    return peeky.java.JavaTarget(
        state / "java-adapter", ("demo/Main.java",), "demo.Main", java=JAVA, javac=JAVAC
    )


def test_a_changed_patch_text_is_refused_by_the_worker_and_rebuilt_by_setup(
    state, offline, monkeypatch, tmp_path
):
    first = Path(java_setup.install()["bundle"])
    source = program(tmp_path / "app")
    # The patch's text changed, while the compiled class still has the byte probe's marker.
    original, patched = java_setup.EVENT_HUB_ORIGINAL, java_setup.EVENT_HUB_PATCHED
    with monkeypatch.context() as changed:
        changed.setitem(
            java_setup.PATCHES,
            "eventhub-log-guard",
            (java_setup.EVENT_HUB, original, patched.replace("RuntimeException", "Exception")),
        )
        for check in (
            lambda: peeky.java._bundle(first, JAVA),
            lambda: own_target(state).prepare(source, tmp_path / "app"),
        ):
            with pytest.raises(ValueError, match="is not what this Peeky's setup builds") as other:
                check()
            assert "(it was built by another version of Peeky)" in str(other.value)
            assert classify(other.value)["code"] == "UNSUPPORTED"
        messages = []
        second = java_setup.install(progress=messages.append)
        assert second["built"] and "(it was built by another version of Peeky)" in messages[0]
        assert peeky.java._bundle(Path(second["bundle"]), JAVA)


def test_the_worker_asks_no_jdk_but_setup_rebuilds_for_another_javac(state, offline):
    first = Path(java_setup.install()["bundle"])
    offline.versions[JAVA] = 'openjdk version "21.0.4"'
    offline.versions[JAVAC] = "javac 21.0.4"
    offline.commands.clear()
    # The worker checks only the bundle's own content: no JDK is asked, and it runs.
    assert peeky.java._verify_bundle(first, JAVA)[0][-1] == "PeekyJavaAdapter"
    assert peeky.java.bundle_directory(state / "java-adapter") == first
    assert offline.commands == []
    # Setup asks javac afresh, and builds again for another javac.
    messages = []
    second = java_setup.install(progress=messages.append)
    assert second["built"] and Path(second["bundle"]) != first
    assert "(it was built with another javac)" in messages[0]
    assert sum(c == [JAVAC, "-version"] for c in offline.commands) == 1
    again = java_setup.install()
    assert not again["built"] and again["bundle"] == second["bundle"]


def test_a_bundle_without_an_exact_recipe_is_refused(tmp_path, offline):
    bundle = tmp_path / "bundle"
    manifest = java_setup.setup(bundle)
    path = bundle / "adapter.json"

    def other_flags(edited):
        edited["recipe"]["javac_flags"] = ["--release", "21"]

    def other_flags_and_fingerprint(edited):
        other_flags(edited)
        edited["fingerprint"] = java_setup.fingerprint(edited["recipe"])

    for change, why in (
        (lambda m: m.pop("recipe"), "it records no build recipe: an earlier Peeky built it"),
        (lambda m: m.pop("fingerprint"), "its recipe doesn't match its fingerprint"),
        (other_flags, "its recipe doesn't match its fingerprint"),
        (other_flags_and_fingerprint, "it was built by another version of Peeky"),
    ):
        edited = json.loads(json.dumps(manifest))
        change(edited)
        path.write_text(json.dumps(edited))
        with pytest.raises(ValueError, match="is not what this Peeky's setup builds") as other:
            peeky.java._bundle(bundle.resolve(), JAVA)
        assert f"({why})" in str(other.value)


@pytest.mark.parametrize("text", ["[]", '"bundle"', "7", "not json", "null"])
def test_an_adapter_json_that_is_not_an_object_is_stale_not_an_internal_error(state, offline, text):
    version = Path(java_setup.install()["bundle"])
    (version / "adapter.json").write_text(text)
    with pytest.raises(ValueError, match="is not what this Peeky's setup builds") as stale:
        _verify_bundle(version, JAVA)
    assert classify(stale.value)["code"] == "UNSUPPORTED"
    wanted = java_setup.recipe(java_setup.toolchain(JAVA, JAVAC))
    assert java_setup.current(version, wanted, JAVA).startswith(
        "it does not verify: its adapter.json"
    )
    assert java_setup.install()["built"]


def test_an_unpatched_bundle_is_called_stale_by_setup(state, offline):
    version = Path(java_setup.install()["bundle"])
    hub = version / EVENT_HUB_CLASS
    hub.write_bytes(b"\xca\xfe Logger fine")
    manifest = json.loads((version / "adapter.json").read_text())
    manifest["files"][EVENT_HUB_CLASS] = java_setup.digest(hub)
    (version / "adapter.json").write_text(json.dumps(manifest))
    wanted = java_setup.recipe(java_setup.toolchain(JAVA, JAVAC))
    assert java_setup.current(version, wanted, JAVA) == "it predates Peeky's java-debug fixes"


# --- the java that runs the adapter: its bundle's, whatever PATH holds ----------------------

# The project's JDK, first on PATH: not the one the bundle was built with (JAVA).
OLD_JAVA, OLD_JAVAC = "/opt/jdk11/bin/java", "/opt/jdk11/bin/javac"


@pytest.fixture
def jdk11_on_path(state, offline, monkeypatch):
    """`java` and `javac` on PATH are JDK 11's; setup built the bundle with JAVA (JDK 17)."""
    offline.versions[OLD_JAVA] = 'openjdk version "11.0.2" 2019-01-15'
    on_path = {"java": OLD_JAVA, "javac": OLD_JAVAC}
    gone = set()

    def tool(value):
        if value in gone:
            raise ValueError(f"Missing Java toolchain executable: {value}")
        return on_path.get(value, value)

    monkeypatch.setattr(peeky.java, "_tool", tool)
    java_setup.install()  # With JAVA: setup's own `which` finds /opt/jdk.
    monkeypatch.setattr(peeky.java.shutil, "which", lambda name: on_path.get(name))
    offline.commands.clear()
    return SimpleNamespace(gone=gone, versions=offline.versions, commands=offline.commands)


def test_a_target_without_java_runs_the_adapter_on_its_bundles_java(
    state, jdk11_on_path, monkeypatch, tmp_path
):
    # The program builds and runs on the java on PATH (JDK 11); the adapter on the bundle's.
    import peeky.java_metadata

    source = program(tmp_path / "app")
    workspace = tmp_path / "workspace"
    (workspace / "demo").mkdir(parents=True)
    (workspace / "demo" / "Main.java").write_text(source.read_text())
    built = []

    def prepare(recipe, root, **kwargs):
        built.append(recipe)
        manifest = SimpleNamespace(manifest_id="build", effective_environment=())
        return SimpleNamespace(
            workspace=workspace, source_root=root, manifest=manifest, cleanup=lambda: None
        )

    entry = {"path": str(workspace / "demo" / "Main.java"), "lines": [2]}
    monkeypatch.setattr(peeky.java, "prepare", prepare)
    monkeypatch.setattr(peeky.java, "assert_current", lambda prepared: None)
    monkeypatch.setattr(
        peeky.java_metadata, "generate_source_map", lambda *a: {"classes": {"demo.Main": entry}}
    )
    target = peeky.java.JavaTarget(state / "java-adapter", ("demo/Main.java",), "demo.Main")
    session = target.prepare(source, tmp_path / "app")
    assert session.command[0] == JAVA and session.adapter_java == JAVA
    assert session.java == OLD_JAVA
    assert session.launch_args(source, tmp_path / "app")["javaExec"] == OLD_JAVA
    assert session.verification_command[0] == OLD_JAVA
    # javac is PATH's too, and the build's record holds both JDKs' versions.
    (recipe,) = built
    assert recipe.build_commands[0].argv[0] == OLD_JAVAC
    assert [c.argv for c in recipe.toolchain_probes] == [
        (OLD_JAVA, "-version"),
        (OLD_JAVAC, "-version"),
        (JAVA, "-version"),
    ]
    # Peeky picked the bundle's java itself, so it asked that java its version first.
    assert jdk11_on_path.commands == [[JAVA, "-version"]]
    session.assert_current()


def test_an_attached_jvm_without_java_uses_the_bundles_java_too(state, jdk11_on_path):
    from peeky.java_attach import JavaAttachTarget

    bundle = bundle_directory(state / "java-adapter")
    target = JavaAttachTarget(state / "java-adapter", 5005, ("demo/Main.java",), ("classes",))
    assert target.java is None
    assert peeky.java.adapter_java(bundle, target.java, launched=False) == JAVA
    # Where the java on PATH is the bundle's, nothing more is asked, as before.
    jdk11_on_path.commands.clear()
    assert peeky.java.adapter_java(bundle, None, JAVA) == JAVA
    assert jdk11_on_path.commands == []


@pytest.mark.parametrize("launched", [True, False])
def test_a_config_naming_another_java_is_refused_saying_what_to_do(
    state, jdk11_on_path, tmp_path, launched
):
    from peeky.java_attach import JavaAttachTarget

    source = program(tmp_path / "app")
    target = (
        peeky.java.JavaTarget(state / "java-adapter", ("demo/Main.java",), "demo.Main", OLD_JAVA)
        if launched
        else JavaAttachTarget(
            state / "java-adapter", 5005, ("demo/Main.java",), ("demo",), java=OLD_JAVA
        )
    )
    with pytest.raises(ValueError) as refused:
        target.prepare(source, tmp_path / "app")
    message, bundle = str(refused.value), bundle_directory(state / "java-adapter")
    assert message.startswith(
        "The Java adapter runs only on the java that its bundle was built with: "
        f'{JAVA} (openjdk version "17.0.1"), for the bundle at {bundle}. '
        f"The config's java is {OLD_JAVA}, another one. Drop java from the config: then the "
        f"adapter runs on {JAVA}"
    ), message
    assert (", and the program on the java on PATH." in message) is launched
    assert message.endswith(
        f"Or set the config's java to {JAVA}. Or rebuild the bundle with the JDK 17 or later "
        "you want: put its bin directory first on PATH, then run `peeky setup-java`"
    )
    error = classify(refused.value)
    assert error["code"] == "UNSUPPORTED" and "java" in error["hint"]
    assert len(error["hint"]) <= 150
    # The config chose it: no JDK is asked, and nothing was built.
    assert jdk11_on_path.commands == []


def test_a_bundles_java_that_is_gone_or_changed_is_refused(state, jdk11_on_path, tmp_path):
    source = program(tmp_path / "app")
    target = peeky.java.JavaTarget(state / "java-adapter", ("demo/Main.java",), "demo.Main")
    rule = (
        "The Java adapter runs only on the java that its bundle was built with: "
        f'{JAVA} (openjdk version "17.0.1")'
    )
    rebuild = "put its bin directory first on PATH, then run `peeky setup-java`"
    # A JDK switched behind the same path (an alternatives link, say).
    jdk11_on_path.versions[JAVA] = 'openjdk version "11.0.2" 2019-01-15'
    with pytest.raises(ValueError) as changed:
        target.prepare(source, tmp_path / "app")
    assert str(changed.value).startswith(rule) and str(changed.value).endswith(rebuild)
    assert (
        "Peeky runs it in place of the java on PATH only while it is the JDK that setup "
        'recorded, and its `-version` now prints `openjdk version "11.0.2" 2019-01-15`, not '
        '`openjdk version "17.0.1"`. Rebuild the bundle'
    ) in str(changed.value)
    assert classify(changed.value)["code"] == "UNSUPPORTED"
    # What a JVM says of options it picked up from the environment doesn't count.
    jdk11_on_path.versions[JAVA] = 'Picked up JAVA_TOOL_OPTIONS: -Xss2m\nopenjdk version "17.0.1"'
    bundle = bundle_directory(state / "java-adapter")
    assert peeky.java.adapter_java(bundle, None, OLD_JAVA) == JAVA
    jdk11_on_path.gone.add(JAVA)
    for named in (None, OLD_JAVA):
        with pytest.raises(ValueError) as gone:
            peeky.java.adapter_java(bundle, named, OLD_JAVA)
        assert str(gone.value) == (
            f"{rule}, for the bundle at {bundle}. That java is gone. Rebuild the bundle with "
            f"the JDK 17 or later you want: {rebuild}"
        )


def test_a_java_on_path_outside_a_bin_directory_is_refused(
    state, jdk11_on_path, monkeypatch, tmp_path
):
    # java-debug launches javaExec only from a directory named bin; else the adapter's JDK.
    shim = "/home/me/.jenv/shims/java"
    jdk11_on_path.versions[shim] = jdk11_on_path.versions[JAVA]
    monkeypatch.setattr(peeky.java, "_tool", lambda value: shim if value == "java" else value)
    source = program(tmp_path / "app")
    target = peeky.java.JavaTarget(state / "java-adapter", ("demo/Main.java",), "demo.Main")
    with pytest.raises(ValueError) as refused:
        target.prepare(source, tmp_path / "app")
    assert str(refused.value) == (
        "Peeky launches a Java program on another java than the adapter's only from a JDK's "
        f"bin directory, where java-debug runs it. The java on PATH, {shim}, is not in one (a "
        f"wrapper or a shim?), so the program would run on the adapter's JDK, {JAVA}, instead. "
        "Put a JDK's bin directory first on PATH, or set the config's java to "
        f"{JAVA} to run the program on that JDK too"
    )
    error = classify(refused.value)
    assert error["code"] == "UNSUPPORTED" and len(error["hint"]) <= 150

    # Named, the bundle's java runs both: the build starts.
    class Built(Exception):
        pass

    def build(*args, **kwargs):
        raise Built

    monkeypatch.setattr(peeky.java, "prepare", build)
    named = peeky.java.JavaTarget(
        state / "java-adapter", ("demo/Main.java",), "demo.Main", java=JAVA
    )
    with pytest.raises(Built):
        named.prepare(source, tmp_path / "app")


def test_a_bundle_a_config_names_is_rebuilt_with_output(tmp_path, jdk11_on_path):
    bundle = tmp_path / "adapter"
    java_setup.setup(bundle, tools=java_setup.toolchain(JAVA, JAVAC))
    with pytest.raises(ValueError) as refused:
        peeky.java.adapter_java(bundle.resolve(), OLD_JAVA)
    assert str(refused.value).endswith(
        "then run `peeky setup-java --output <a new directory>`, and point the config's "
        "bundle at it"
    )


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


def test_setup_java_prune_removes_all_but_the_current_version(state, offline, capsys):
    status, out, err = setup_java(capsys, "--prune")
    assert (status, out.strip()) == (0, "Nothing to remove. No version is in use.")
    java_setup.install()
    status, out, err = setup_java(capsys, "--force", "--quiet")
    assert "1 older version(s) kept, for sessions still using them" in out
    assert "`peeky setup-java --prune` removes them." in out
    current = java_setup.installed()
    status, out, err = setup_java(capsys, "--prune", "--json")
    result = json.loads(out)
    assert status == 0 and result["bundle"] == str(current) and len(result["pruned"]) == 1
    assert result["warnings"] == [cli.PRUNED_NOTE]
    assert "fails its next step (PROVENANCE_CHANGED)" in cli.PRUNED_NOTE
    status, out, err = setup_java(capsys, "--prune")
    assert out.strip() == f"Nothing to remove. Kept {current}."
    for extra in ("--force", "--output=x"):
        status, out, err = setup_java(capsys, "--prune", extra)
        assert status == 2 and "--prune takes neither --output nor --force" in err


def test_prune_reports_versions_it_could_not_remove_and_fails(state, offline, capsys, monkeypatch):
    container = state / "java-adapter"
    versions = []
    for schema in (1, 2, 3, 4):
        monkeypatch.setattr(java_setup, "RECIPE_SCHEMA", schema)
        versions.append(Path(java_setup.install()["bundle"]).name)
    stuck, kept_anyway, gone = versions[0], versions[1], versions[2]
    (container / ".peeky-java-dead").mkdir()
    rmtree = java_setup.shutil.rmtree

    def removing(path, *args, **kwargs):
        name = Path(path).name
        if name == stuck:
            raise PermissionError(13, "Permission denied", str(Path(path) / "classes"))
        if name in (kept_anyway, ".peeky-java-dead"):
            return None  # Says nothing, removes nothing.
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(java_setup.shutil, "rmtree", removing)
    status, out, err = setup_java(capsys, "--prune", "--json")
    result = json.loads(out)
    assert status == 2
    assert result["pruned"] == [gone]
    assert result["failed"] == {
        stuck: f"Permission denied: {container.resolve() / stuck / 'classes'}",
        kept_anyway: "it is still there",
    }
    assert result["warnings"] == [cli.PRUNED_NOTE]
    assert java_setup._versions(container) == sorted([stuck, kept_anyway, versions[3]])
    # Staging is best-effort, and never reported.
    assert (container / ".peeky-java-dead").exists() and ".peeky-java-dead" not in out
    status, out, err = setup_java(capsys, "--prune")
    assert status == 2
    assert f"Could not remove {stuck} (Permission denied: " in out
    assert f"Could not remove {kept_anyway} (it is still there); what is left" in out
    assert "Removed" not in out and cli.PRUNED_NOTE in out


@pytest.mark.skipif(os.getuid() == 0, reason="root removes any file")
def test_prune_fails_on_a_version_it_has_no_permission_to_remove(state, offline, capsys):
    container = state / "java-adapter"
    old = Path(java_setup.install()["bundle"])
    java_setup.install(force=True)
    (old / "lib").chmod(0o500)
    try:
        status, out, err = setup_java(capsys, "--prune")
        assert status == 2 and f"Could not remove {old.name} (Permission denied: " in out
        assert old.name in java_setup._versions(container)
    finally:
        (old / "lib").chmod(0o700)
    status, out, err = setup_java(capsys, "--prune")
    assert status == 0 and f"Removed 1 older version(s): {old.name}." in out


def test_prune_removes_nothing_while_the_pointer_is_refused(state, offline, capsys):
    java_setup.install()
    java_setup.install(force=True)
    container = state / "java-adapter"
    (container / "current").chmod(0o644)
    status, out, err = setup_java(capsys, "--prune")
    assert status == 2 and "[UNSUPPORTED] Peeky's own Java adapter can't be used" in err
    assert len(java_setup._versions(container)) == 2


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
