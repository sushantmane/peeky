"""Two JDKs: the adapter runs on the `java` its bundle was built with, whatever `java` is
first on PATH, and the program builds and runs on the one on PATH (the project's JDK). A
config's `java` must be the bundle's; another is refused, saying what to do."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from peeky.cli import main

PROGRAM = """package shop;

public class Main {
    public static void main(String[] args) {
        String runtime = System.getProperty("java.version");
        String via = String.valueOf(System.getProperty("peeky.via"));
        int total = runtime.length() + via.length();
        System.out.println(total);
    }
}
"""
LINE = 8


@pytest.fixture
def bundle():
    configured = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not configured:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    return Path(configured).resolve()


def recorded(bundle):
    """The `java` the bundle was built with, and its resolved path."""
    java = json.loads((bundle / "adapter.json").read_text())["command"][0]
    return java, str(Path(java).resolve())


def shop(tmp_path, bundle, **config):
    root = tmp_path / "shop"
    (root / "shop").mkdir(parents=True)
    (root / "shop" / "Main.java").write_text(PROGRAM)
    path = root / "c.json"
    path.write_text(
        json.dumps(
            {
                "bundle": str(bundle),
                "sources": ["shop/Main.java"],
                "main_class": "shop.Main",
                **config,
            }
        )
    )
    return root, path


def probe(capsys, root, config):
    code = main(
        [
            "probe",
            str(root),
            "--config",
            str(config),
            "--at",
            f"shop/Main.java:{LINE}",
            "--capture",
            "runtime,via",
            "--json",
            "--no-save",
            "--quiet",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    return code, result


def first_on_path(monkeypatch, directory):
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")


def javac_of(java):
    """The javac beside ``java``: the program's classes must be ones its JVM runs."""
    return str(Path(java).parent / "javac")


def wrapper(tmp_path, java, folder="path-jdk/bin"):
    """A `java` that is not the bundle's: a script that runs ``java``, telling the program
    that it ran through PATH's `java` (the system property peeky.via). Its `javac` is the
    one beside ``java``."""
    directory = tmp_path / folder
    directory.mkdir(parents=True)
    script = directory / "java"
    script.write_text(f'#!/bin/sh\nexec "{java}" -Dpeeky.via=path "$@"\n')
    script.chmod(0o755)
    (directory / "javac").symlink_to(javac_of(java))
    return directory, script


@pytest.mark.timeout(120)
def test_the_program_runs_on_the_java_on_path_and_the_adapter_on_its_bundles(
    tmp_path, bundle, monkeypatch, capsys
):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    _, java = recorded(bundle)
    directory, _ = wrapper(tmp_path, java)
    first_on_path(monkeypatch, directory)
    root, config = shop(tmp_path, bundle)
    code, result = probe(capsys, root, config)
    assert code == 0, result.get("error")
    values = {row["selector"]: row.get("value") for row in result["rows"]}
    assert values["via"] == '"path"', values


@pytest.mark.timeout(120)
def test_a_java_on_path_outside_a_bin_directory_is_refused(tmp_path, bundle, monkeypatch, capsys):
    # java-debug would launch the adapter's own JDK for it: the program would not run there.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    _, java = recorded(bundle)
    directory, script = wrapper(tmp_path, java, folder="shims")
    first_on_path(monkeypatch, directory)
    root, config = shop(tmp_path, bundle)
    code, result = probe(capsys, root, config)
    assert code == 2 and result["refused_before_launch"] is True
    assert result["error"]["code"] == "UNSUPPORTED"
    assert f"The java on PATH, {script}, is not in one" in result["error"]["message"]


@pytest.mark.timeout(120)
def test_a_config_naming_another_java_is_refused_before_anything_runs(
    tmp_path, bundle, monkeypatch, capsys
):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    named, java = recorded(bundle)
    _, script = wrapper(tmp_path, java)
    root, config = shop(tmp_path, bundle, java=str(script))
    code, result = probe(capsys, root, config)
    assert code == 2 and result["refused_before_launch"] is True
    error = result["error"]
    assert error["code"] == "UNSUPPORTED"
    assert error["message"].startswith(
        f"The Java adapter runs only on the java that its bundle was built with: {named} ("
    )
    assert f"The config's java is {script}, another one" in error["message"]
    assert f"Or set the config's java to {named}." in error["message"]
    # The bundle's own java, named, runs both.
    root, config = shop(tmp_path / "named", bundle, java=named, javac=javac_of(java))
    code, result = probe(capsys, root, config)
    assert code == 0, result.get("error")


def _other_jdks(java):
    """Other JDKs' bin directories (with javac), each with its `java -version` text."""
    homes = []
    if sys.platform == "darwin" and os.path.exists("/usr/libexec/java_home"):
        listed = subprocess.run(
            ["/usr/libexec/java_home", "-V"], capture_output=True, text=True, timeout=30
        )
        homes = re.findall(r"\s(/\S.*?/Contents/Home)\s*$", listed.stderr, re.MULTILINE)
    elif os.path.isdir("/usr/lib/jvm"):
        homes = [str(path) for path in sorted(Path("/usr/lib/jvm").iterdir())]
    seen = {java}
    for home in homes:
        bin_dir = Path(home) / "bin"
        if not (bin_dir / "java").is_file() or not (bin_dir / "javac").is_file():
            continue
        resolved = str((bin_dir / "java").resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        printed = subprocess.run(
            [resolved, "-version"], capture_output=True, text=True, timeout=60
        ).stderr
        yield bin_dir, printed


def _feature(printed):
    found = re.search(r' version "(\d+)(?:\.(\d+))?', printed)
    return int(found[2]) if found[1] == "1" else int(found[1])


@pytest.mark.timeout(300)
def test_a_project_on_another_jdk_runs_on_it(tmp_path, bundle, monkeypatch, capsys):
    """The case that was refused: the project's JDK (say 11) first on PATH, the bundle built
    with another. Each other JDK found runs the program, but one before 19 where the adapter
    is on 19 or later, which is refused before the launch. Needs a second JDK:
    `/usr/libexec/java_home -V` on macOS, /usr/lib/jvm on Linux (the value study's image
    has 11, 17 and 21)."""
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    _, java = recorded(bundle)
    adapter_version = json.loads((bundle / "adapter.json").read_text())["java_version"]
    adapter = _feature(adapter_version)
    found = [
        (bin_dir, printed)
        for bin_dir, printed in _other_jdks(java)
        if re.search(r' version "([^"]+)"', printed) and printed.strip() != adapter_version.strip()
    ][:3]
    if not found:
        pytest.skip("needs a second JDK, with javac, besides the one the bundle was built with")
    path = os.environ["PATH"]
    for index, (bin_dir, printed) in enumerate(found):
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{path}")
        assert Path(shutil.which("java")).resolve() == (bin_dir / "java").resolve()
        root, config = shop(tmp_path / str(index), bundle)
        code, result = probe(capsys, root, config)
        if adapter >= 19 > _feature(printed):
            assert code == 2 and result["error"]["code"] == "UNSUPPORTED", result.get("error")
            assert result["error"]["message"].startswith(
                "A Java adapter on JDK 19 or later launches a program only on JDK 19 or later"
            )
            assert f"is JDK {_feature(printed)}. Rebuild" in result["error"]["message"]
            continue
        assert code == 0, (printed, result.get("error"))
        values = {row["selector"]: row.get("value") for row in result["rows"]}
        expected = re.search(r' version "([^"]+)"', printed)[1]
        assert values["runtime"] == f'"{expected}"', (values, printed)
        assert values["via"] == '"null"'
