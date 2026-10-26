"""What a release holds and says: each command's version, from one source; the sdist's
examples; tool variables that name a program on PATH."""

import fnmatch
import re
import sys
import tomllib
from pathlib import Path

import pytest

import peeky
from peeky import agent, cli, probe, worker
from peeky.errors import classify

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("main", [cli.main, worker.main, agent.main, probe.main])
def test_every_command_prints_the_one_version(main, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["command", "--version"])
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 0
    assert capsys.readouterr().out == f"peeky {peeky.__version__}\n"


def test_the_package_metadata_reads_the_same_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" not in project["project"]
    assert project["project"]["dynamic"] == ["version"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "peeky.__version__"}
    assert re.fullmatch(r"\d+\.\d+\.\d+", peeky.__version__)


def _shipped(manifest, files):
    """The files MANIFEST.in puts in the sdist, by setuptools' rules for the commands it
    uses here: `include` (a root file), `recursive-include` (by file name) and `prune`."""
    kept = set()
    for line in manifest.splitlines():
        command, *words = line.split() or ["#"]
        if command == "include":
            kept |= {name for name in files if name in words}
        elif command == "recursive-include":
            directory, patterns = words[0].rstrip("/") + "/", words[1:]
            kept |= {
                name
                for name in files
                if name.startswith(directory)
                and any(fnmatch.fnmatchcase(name.rpartition("/")[2], p) for p in patterns)
            }
        elif command == "prune":
            kept = {name for name in kept if not name.startswith(words[0].rstrip("/") + "/")}
        else:
            assert command.startswith("#"), f"teach _shipped the MANIFEST.in command {command}"
    return kept


def _sources(directory):
    """Every file under `directory` but build outputs: a crate's target, Python's
    bytecode, and files with no suffix (built programs)."""
    return {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / directory).rglob("*")
        if path.is_file()
        and not {"target", "__pycache__"} & set(path.relative_to(ROOT).parts)
        and not path.name.startswith(".")
        and path.suffix not in ("", ".pyc")
    }


def test_the_sdist_holds_every_example_and_the_skill():
    # The tests build and debug the C, C++, Rust and CUDA examples: an unpacked sdist
    # must hold them, and the agent skill the README points at.
    files = _sources("examples") | _sources("skills")
    assert {"examples/c/pagination_bug.c", "skills/peeky-debug/SKILL.md"} <= files
    missing = files - _shipped((ROOT / "MANIFEST.in").read_text(), files)
    assert not missing, f"add these to MANIFEST.in: {sorted(missing)}"


def test_the_sdist_leaves_out_a_crates_build_output():
    built = {"examples/rust/target/debug/build/x/out/gen.rs", "examples/rust/target/a.json"}
    assert not _shipped((ROOT / "MANIFEST.in").read_text(), built)


@pytest.fixture
def toolchain(tmp_path, monkeypatch):
    """A PATH whose first `gcc` is a link to another file, and a `gcc` in the working
    directory that a shell would never run."""
    here = tmp_path.resolve()
    (here / "bin").mkdir()
    real = here / "gcc-13"
    tool = here / "tool"
    for program in (real, tool):
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
    (here / "bin" / "gcc").symlink_to(real)
    (here / "gcc").write_text("not a compiler\n")
    monkeypatch.chdir(here)
    monkeypatch.setenv("PATH", f"{here / 'bin'}:/usr/bin:/bin")
    for name in ("DAP", "DWARFDUMP", "DSYMUTIL"):
        monkeypatch.setenv(f"PEEKY_NATIVE_{name}", str(tool))
    monkeypatch.setenv("PEEKY_NATIVE_SYSROOT", str(here))
    monkeypatch.setattr(cli, "_ON_PATH_SAID", set(), raising=False)
    return here


def test_a_bare_compiler_name_is_found_on_path_and_named(toolchain, monkeypatch, capsys):
    monkeypatch.setenv("PEEKY_NATIVE_CC", "gcc")
    assert cli.native_tools("c")["compiler"] == str(toolchain / "gcc-13")
    assert cli.native_tools("c")["compiler"] == str(toolchain / "gcc-13")
    assert capsys.readouterr().err == (
        f"peeky: PEEKY_NATIVE_CC=gcc: using {toolchain / 'bin' / 'gcc'} (a link to "
        f"{toolchain / 'gcc-13'}), the first gcc on PATH\n"
    )
    # A value with a slash is a path, as before.
    monkeypatch.setenv("PEEKY_NATIVE_CC", "./gcc")
    assert cli.native_tools("c")["compiler"] == str(toolchain / "gcc")


def test_a_bare_name_not_on_path_says_what_to_do(toolchain, monkeypatch):
    monkeypatch.setenv("PEEKY_NATIVE_CXX", "g++-99")
    with pytest.raises(FileNotFoundError) as refused:
        cli.native_tools("cpp")
    error = classify(refused.value)
    assert (error["code"], error["message"]) == (
        "INVALID_REQUEST",
        "PEEKY_NATIVE_CXX=g++-99: no program named g++-99 on PATH; install it, or set "
        "PEEKY_NATIVE_CXX to its full path",
    )
