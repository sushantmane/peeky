"""Two JDKs: the adapter runs on the `java` its bundle was built with, whatever `java` is
first on PATH, and the program builds and runs on the one on PATH (the project's JDK). Where
they differ, each must be a JDK's own bin/java, known by its release file; a wrapper, a shim
or macOS's /usr/bin/java is refused, naming the JDK to use. A config's `java` must be the
bundle's; another is refused, saying what to do. A run shows both (`java`)."""

import json
import os
import re
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
RULE = "The Java adapter runs only on the java that its bundle was built with: "


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    configured = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not configured:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return Path(configured).resolve()


def recorded(bundle):
    """The `java` the bundle was built with, as it names it, and its real path."""
    java = json.loads((bundle / "adapter.json").read_text())["command"][0]
    return java, os.path.realpath(java)


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


def probe(capsys, root, config, *, json_out=True):
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
            *(["--json"] if json_out else []),
            "--no-save",
            "--quiet",
        ]
    )
    out = capsys.readouterr().out
    return code, json.loads(out) if json_out else out


def first_on_path(monkeypatch, directory):
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")


def javac_of(java):
    """The javac beside ``java``: the program's classes must be ones its JVM runs."""
    return str(Path(java).parent / "javac")


def java_home(java):
    """The java.home that ``java`` says it runs."""
    printed = subprocess.run(
        [java, "-XshowSettings:properties", "-version"], capture_output=True, text=True, timeout=60
    ).stderr
    return re.search(r"^\s*java\.home = (\S.*?)\s*$", printed, re.MULTILINE)[1]


def wrapper(tmp_path, java, folder):
    """A `java` that is not a JDK's own: a script that runs ``java``. Its `javac` is the
    one beside ``java``."""
    directory = tmp_path / folder
    directory.mkdir(parents=True)
    script = directory / "java"
    script.write_text(f'#!/bin/sh\nexec "{java}" -Dpeeky.via=path "$@"\n')
    script.chmod(0o755)
    (directory / "javac").symlink_to(javac_of(java))
    return directory, script


def refused_as_a_launcher(result, named):
    """Where the bundle's own java is not a JDK's own (macOS's /usr/bin/java), Peeky never
    picks it in place of another java on PATH."""
    error = result["error"]
    assert error["code"] == "UNSUPPORTED", error
    assert error["message"].startswith(f"{RULE}{named}")
    assert "whose files setup pinned. It is not one: " in error["message"]


@pytest.mark.timeout(120)
@pytest.mark.parametrize("folder", ["wrapper/bin", "shims"])
def test_a_wrapper_on_path_is_refused_naming_the_jdk_it_runs(
    tmp_path, bundle, monkeypatch, capsys, folder
):
    named, java = recorded(bundle)
    directory, script = wrapper(tmp_path, java, folder)
    first_on_path(monkeypatch, directory)
    root, config = shop(tmp_path, bundle)
    code, result = probe(capsys, root, config)
    assert code == 2 and result["refused_before_launch"] is True
    if jdk_release(java) is None:
        return refused_as_a_launcher(result, named)
    error = result["error"]
    assert error["code"] == "UNSUPPORTED", error
    home = java_home(java)
    assert f"The java on PATH, {script}, is not one: " in error["message"]
    assert f"; it says it runs {home}. Put {home}/bin first on PATH" in error["message"]


@pytest.mark.timeout(120)
def test_a_config_naming_another_java_is_refused_before_anything_runs(
    tmp_path, bundle, monkeypatch, capsys
):
    named, java = recorded(bundle)
    _, script = wrapper(tmp_path, java, "wrapper/bin")
    root, config = shop(tmp_path, bundle, java=str(script))
    code, result = probe(capsys, root, config)
    assert code == 2 and result["refused_before_launch"] is True
    error = result["error"]
    assert error["code"] == "UNSUPPORTED"
    assert error["message"].startswith(f"{RULE}{named} (")
    assert f"The config's java is {script}, another one" in error["message"]
    assert f"Or set the config's java to {named}." in error["message"]
    # The bundle's own java, named, runs both; the run says so.
    root, config = shop(tmp_path / "named", bundle, java=named, javac=javac_of(java))
    code, result = probe(capsys, root, config)
    assert code == 0, result.get("error")
    assert result["java"]["program"]["path"] == result["java"]["adapter"]["path"] == java
    code, text = probe(capsys, root, config, json_out=False)
    assert code == 0 and f"\njava: {java} (" in text
    assert "), the program's and the adapter's\n" in text
    # A session says so too.
    start = [
        "session",
        "start",
        str(root),
        "--config",
        str(config),
        "--at",
        f"shop/Main.java:{LINE}",
    ]
    try:
        assert main([*start, "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["java"] == result["java"]
    finally:
        main(["session", "close"])
        capsys.readouterr()


def _other_jdks(java):
    """Other JDKs' own bin/java (with javac beside it): each a real path."""
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
        real = os.path.realpath(Path(home) / "bin" / "java")
        if real in seen or not jdk_release(real) or not Path(javac_of(real)).is_file():
            continue
        seen.add(real)
        yield real


def native(path):
    from peeky.java import NATIVE_MAGIC

    return path.is_file() and path.read_bytes()[:4] in NATIVE_MAGIC


def jdk_release(java):
    """(home, JAVA_VERSION) where ``java`` is a JDK 9 or later's own bin/java."""
    path = Path(java)
    home = path.parent.parent
    release = home / "release"
    libjvm = [home / "lib" / "server" / name for name in ("libjvm.so", "libjvm.dylib")]
    if (
        path.name != "java"
        or path.parent.name != "bin"
        or not native(path)
        or not release.is_file()
        or not (home / "lib" / "modules").is_file()
        or not any(native(lib) for lib in libjvm)
    ):
        return None
    found = re.search(r'^JAVA_VERSION="([0-9][^"]*)"', release.read_text(), re.MULTILINE)
    return (str(home), found[1]) if found and not found[1].startswith("1.") else None


def _feature(version):
    found = re.match(r"(\d+)(?:\.(\d+))?", version)
    return int(found[2]) if found[1] == "1" else int(found[1])


@pytest.mark.timeout(300)
def test_a_project_on_another_jdk_runs_on_it(tmp_path, bundle, monkeypatch, capsys):
    """The case that was refused: the project's JDK (say 11) first on PATH, the bundle built
    with another. Each other JDK found runs the program, but one before 19 where the adapter
    is on 19 or later, refused before the launch. Where the bundle's java is not a JDK's own
    (macOS's /usr/bin/java), Peeky never picks it in place of PATH's. Needs another JDK:
    `/usr/libexec/java_home -V` on macOS, /usr/lib/jvm on Linux (the value study's image has
    11, 17 and 21)."""
    named, java = recorded(bundle)
    found = list(_other_jdks(java))[:3]
    if not found:
        pytest.skip("needs a JDK besides the java the bundle was built with")
    adapter = jdk_release(java)
    path = os.environ["PATH"]
    for index, other in enumerate(found):
        monkeypatch.setenv("PATH", f"{Path(other).parent}{os.pathsep}{path}")
        root, config = shop(tmp_path / str(index), bundle)
        code, result = probe(capsys, root, config)
        if adapter is None:
            assert code == 2, result
            refused_as_a_launcher(result, named)
            continue
        version = jdk_release(other)[1]
        if _feature(adapter[1]) >= 19 > _feature(version):
            assert code == 2 and result["error"]["code"] == "UNSUPPORTED", result.get("error")
            assert result["error"]["message"].startswith(
                "A Java adapter on JDK 19 or later launches a program only on JDK 19 or later"
            )
            assert f"is JDK {_feature(version)}. Rebuild" in result["error"]["message"]
            continue
        assert code == 0, (other, result.get("error"))
        values = {row["selector"]: row.get("value") for row in result["rows"]}
        assert values["runtime"] == f'"{version}"', (values, other)
        assert values["via"] == '"null"'
        assert result["java"] == {
            "program": {"path": other, "version": version, "version_from": "release"},
            "adapter": {"path": java, "version": adapter[1], "version_from": "release"},
        }
