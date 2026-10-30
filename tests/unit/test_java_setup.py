"""`peeky setup-java` builds the Java adapter from the installed package, and Java targets
whose config names no bundle find the one it builds. A wheel has no scripts/ or adapters/
directory: this needs no JDK, network or checkout."""

import io
import json
import os
import subprocess
import sys
import tarfile
import tomllib
from fnmatch import fnmatch
from pathlib import Path

import pytest

from peeky import cli, java_setup

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


@pytest.fixture
def offline(monkeypatch):
    """Setup with its downloads and tools faked: javac "compiles" a class per source."""
    calls = []

    def acquire(item):
        if "fail" in calls:
            raise ValueError(f"download checksum/size mismatch: {item['url']}")
        return source_archive() if item["url"].startswith("https://codeload") else b"PK jar"

    def run(command):
        calls.append(command)
        if command[-1] == "-version":
            return "17.0.0"
        classes = Path(command[command.index("-d") + 1])
        (classes / Path(command[-1].lstrip("@")).stem).with_suffix(".class").write_bytes(b"\xca")
        return ""

    monkeypatch.setattr(java_setup, "acquire", acquire)
    monkeypatch.setattr(java_setup, "run", run)
    monkeypatch.setattr(java_setup.shutil, "which", lambda name: f"/opt/jdk/bin/{name}")
    return calls


def test_setup_builds_a_bundle_with_its_provenance_and_notices(tmp_path, offline):
    output = tmp_path / "out" / "adapter"
    manifest = java_setup.setup(output)
    assert json.loads((output / "adapter.json").read_text()) == manifest
    assert manifest["launcher_sha256"] == java_setup.digest(java_setup.LAUNCHER)
    assert manifest["dependency_lock_sha256"] == java_setup.digest(java_setup.LOCK)
    assert set(manifest["patches"]) == {"eventhub-log-guard"}
    assert (
        manifest["command"][2] == f"{output.resolve()}/classes{os.pathsep}{output.resolve()}/lib/*"
    )
    for name in ("LICENSE.txt", "ThirdPartyNotices.txt", "PEEKY-NOTICES.md"):
        assert f"notices/{name}" in manifest["files"]
    # The launcher compiled is the packaged one.
    assert offline[-1][-1] == str(java_setup.LAUNCHER)
    assert sorted(p.name for p in output.parent.iterdir()) == ["adapter"]


def test_replacing_a_bundle_swaps_it_only_once_the_new_one_is_built(tmp_path, offline):
    output = tmp_path / "adapter"
    output.mkdir()
    (output / "old.txt").write_text("old")
    offline.append("fail")
    with pytest.raises(ValueError, match="Java adapter setup failed, and nothing was installed"):
        java_setup.setup(output, replace=True, private=True)
    assert [p.name for p in output.iterdir()] == ["old.txt"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["adapter"]
    offline.remove("fail")
    java_setup.setup(output, replace=True, private=True)
    assert (output / "adapter.json").is_file() and not (output / "old.txt").exists()
    assert output.stat().st_mode & 0o777 == 0o700
    assert sorted(p.name for p in tmp_path.iterdir()) == ["adapter"]


def test_setup_replaces_only_a_real_directory_of_yours(tmp_path, offline):
    (tmp_path / "real").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")
    with pytest.raises(ValueError, match="not a directory you own"):
        java_setup.setup(link, replace=True)
    with pytest.raises(ValueError, match="already exists; choose a fresh directory"):
        java_setup.setup(tmp_path / "real")
    assert offline == []


def setup_java(capsys, *args):
    status = cli.main(["setup-java", *args])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_setup_java_builds_peekys_own_bundle_owner_only(state, offline, capsys):
    status, out, err = setup_java(capsys, "--json")
    assert status == 0, err
    result = json.loads(out)
    bundle = state / "java-adapter"
    assert (result["bundle"], result["built"], result["default"]) == (
        str(bundle.resolve()),
        True,
        True,
    )
    assert result["adapter"] == json.loads((bundle / "adapter.json").read_text())
    assert bundle.stat().st_mode & 0o777 == 0o700 and state.stat().st_mode & 0o077 == 0
    assert f"Building the Java adapter in {bundle}" in err


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


def test_setup_java_keeps_an_up_to_date_bundle_and_replaces_a_stale_one(
    state, offline, capsys, monkeypatch
):
    reasons = iter([None, "it was built by another version of Peeky"])
    monkeypatch.setattr(java_setup, "current", lambda bundle: next(reasons))
    assert setup_java(capsys, "--quiet")[0] == 0
    built = len(offline)
    status, out, err = setup_java(capsys)
    assert status == 0 and "is up to date: nothing to do" in out and len(offline) == built
    status, out, err = setup_java(capsys)
    assert status == 0 and "Built the Java adapter" in out and len(offline) > built
    assert "Replacing the Java adapter in " in err
    assert "java-adapter (it was built by another version of Peeky): downloading" in err
    built = len(offline)
    assert setup_java(capsys, "--force")[0] == 0 and len(offline) > built


def test_an_up_to_date_bundle_verifies_and_matches_this_peeky(state, offline, monkeypatch):
    import hashlib

    import peeky.java

    bundle = state / "java-adapter"
    java_setup.setup(bundle)
    # The fake build's EventHub class: the patched one calls Logger.isLoggable.
    event_hub = bundle / EVENT_HUB_CLASS
    event_hub.parent.mkdir(parents=True, exist_ok=True)
    event_hub.write_bytes(b"\xca\xfe isLoggable")
    manifest = json.loads((bundle / "adapter.json").read_text())
    manifest["files"][EVENT_HUB_CLASS] = hashlib.sha256(event_hub.read_bytes()).hexdigest()
    (bundle / "adapter.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(peeky.java, "_tool", lambda name: "/opt/jdk/bin/java")
    assert java_setup.current(bundle) is None
    manifest["launcher_sha256"] = "0" * 64
    (bundle / "adapter.json").write_text(json.dumps(manifest))
    assert java_setup.current(bundle) == "it was built by another version of Peeky"
    event_hub.write_bytes(b"\xca\xfe")
    assert java_setup.current(bundle) == "it does not verify: Adapter bundle checksum mismatch"
    # A bundle built before the event thread's patch, as an older Peeky built it.
    manifest["files"][EVENT_HUB_CLASS] = hashlib.sha256(b"\xca\xfe").hexdigest()
    (bundle / "adapter.json").write_text(json.dumps(manifest))
    assert java_setup.current(bundle) == "it predates Peeky's java-debug fixes"
    assert "No such file" in java_setup.current(state / "missing")


def test_setup_java_without_a_jdk_installs_nothing(state, monkeypatch, capsys):
    monkeypatch.setattr(java_setup.shutil, "which", lambda name: None)
    status, out, err = setup_java(capsys, "--quiet")
    assert status == 2
    assert "[UNSUPPORTED] JDK required: java and javac (JDK 17 or later) must be on PATH" in err
    assert "Install JDK 17 or later" in err
    assert sorted(p.name for p in state.iterdir()) == []


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
