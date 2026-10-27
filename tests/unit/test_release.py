"""What a release holds and says: each command's version, from one source; the sdist's
examples; tool variables that name a program on PATH."""

import fnmatch
import json
import re
import socket
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


def test_a_relative_path_entry_is_not_called_a_link(toolchain, monkeypatch, capsys):
    # Review of the PATH lookup: `which` gives `bin/cc` for a relative PATH entry; that
    # is not a link, and the note gives the absolute path.
    (toolchain / "bin" / "cc").write_text("#!/bin/sh\n")
    (toolchain / "bin" / "cc").chmod(0o755)
    monkeypatch.setenv("PATH", "bin:/usr/bin:/bin")
    monkeypatch.setenv("PEEKY_NATIVE_CC", "cc")
    assert cli.native_tools("c")["compiler"] == str(toolchain / "bin" / "cc")
    assert capsys.readouterr().err == (
        f"peeky: PEEKY_NATIVE_CC=cc: using {toolchain / 'bin' / 'cc'}, the first cc on PATH\n"
    )


@pytest.mark.parametrize("link", ["symbolic", "hard"])
def test_rustups_cargo_and_rustc_proxies_name_its_pinned_toolchain(
    tmp_path, monkeypatch, capsys, link
):
    # Review of the PATH lookup: `cargo` and `rustc` on PATH are rustup's proxies, links
    # to rustup itself. Taken as found, both were rustup, and the build was refused as not
    # "the same real toolchain sysroot binaries".
    from peeky.rust import _toolchain

    here = tmp_path.resolve()
    proxies, toolchain = here / "cargo-bin", here / "toolchains" / "1.96.0" / "bin"
    proxies.mkdir()
    toolchain.mkdir(parents=True)
    (toolchain.parent / "lib" / "rustlib").mkdir(parents=True)
    (proxies / "rustup").write_text(
        f'#!/bin/sh\n[ "$1 $2 $3" = "which --toolchain 1.96.0" ] || exit 1\necho "{toolchain}/$4"\n'
    )
    tool = here / "tool"
    for program in (proxies / "rustup", toolchain / "cargo", toolchain / "rustc", tool):
        program.write_text(program.read_text() if program.exists() else "#!/bin/sh\n")
        program.chmod(0o755)
    for name in ("cargo", "rustc"):
        if link == "symbolic":
            (proxies / name).symlink_to(proxies / "rustup")
        else:
            (proxies / name).hardlink_to(proxies / "rustup")
    monkeypatch.setenv("PATH", f"{proxies}:/usr/bin:/bin")
    monkeypatch.setenv("PEEKY_RUST_CARGO", "cargo")
    monkeypatch.setenv("PEEKY_RUST_RUSTC", "rustc")
    for name in ("DAP", "DWARFDUMP", "LINKER", "DSYMUTIL"):
        monkeypatch.setenv(f"PEEKY_RUST_{name}", str(tool))
    monkeypatch.setattr(cli, "_ON_PATH_SAID", set(), raising=False)
    tools = cli.rust_tools()
    assert (tools["cargo"], tools["rustc"]) == (str(toolchain / "cargo"), str(toolchain / "rustc"))
    assert _toolchain(tools["cargo"], tools["rustc"]) == (tools["cargo"], tools["rustc"])
    assert capsys.readouterr().err == "".join(
        f"peeky: PEEKY_RUST_{name.upper()}={name}: using {toolchain / name}, rustup's 1.96.0 "
        f"{name}: the first {name} on PATH, {proxies / name}, is rustup's proxy\n"
        for name in ("cargo", "rustc")
    )


@pytest.mark.parametrize(
    "command",
    [
        ["probe", "main.c", "--at", "2", "--capture", "x", "--no-save"],
        ["session", "start", "main.c", "--at", "2", "--name", "remote-tools"],
    ],
)
def test_a_run_on_a_worker_neither_looks_for_nor_names_this_hosts_tools(
    tmp_path, monkeypatch, capsys, command
):
    # Review of the PATH lookup: the worker's configuration names its tools. A remote
    # probe or session looked for the client's, said `using …` for a compiler it would
    # never run, and failed where the client had none.
    here = tmp_path.resolve()
    (here / "bin").mkdir()
    (here / "bin" / "cc351").write_text("#!/bin/sh\n")
    (here / "bin" / "cc351").chmod(0o755)
    (here / "main.c").write_text("int main(void) {\n  int x = 1;\n  return x;\n}\n")
    monkeypatch.chdir(here)
    for name in ("CC", "CXX", "DAP", "DWARFDUMP", "DSYMUTIL", "SYSROOT"):
        monkeypatch.delenv(f"PEEKY_NATIVE_{name}", raising=False)
    monkeypatch.setenv("PEEKY_NATIVE_CC", "cc351")  # on PATH; no adapter or dwarfdump is
    monkeypatch.setenv("PATH", str(here / "bin"))
    monkeypatch.setenv("PEEKY_STATE_DIR", str(here / "state"))
    monkeypatch.setenv(cli.WORKER_TOKEN_ENV, "a-worker-token-of-some-length")
    monkeypatch.setattr(cli, "_ON_PATH_SAID", set(), raising=False)
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{unused.getsockname()[1]}"
    code = cli.main([*command, "--worker", url, "--json"])
    out, err = capsys.readouterr()
    assert code != 0
    assert json.loads(out)["error"]["code"] == "UNREACHABLE", out
    assert "PEEKY_NATIVE" not in err and "Cannot find" not in out


@pytest.mark.parametrize(
    "variable, name, link",
    [
        ("CARGO", "rustfmt", "symbolic"),
        ("CARGO", "rustfmt", "hard"),
        ("RUSTC", "cargo", "symbolic"),
        ("RUSTC", "cargo", "hard"),
        ("RUSTC", "rustup", None),
    ],
)
def test_only_cargo_and_rustc_proxies_stand_for_the_pinned_toolchain(
    tmp_path, monkeypatch, capsys, link, variable, name
):
    # Review round 2: `PEEKY_RUST_CARGO=rustfmt`, another of rustup's proxies, became the
    # pinned cargo. Only `cargo` and `rustc` are taken for the tools they name; any other
    # stays as found, rustup itself, which the toolchain check refuses.
    from peeky.rust import _toolchain

    here = tmp_path.resolve()
    (here / "bin").mkdir()
    rustup = here / "bin" / "rustup"
    rustup.write_text('#!/bin/sh\necho "should not be asked" >&2\nexit 1\n')
    rustup.chmod(0o755)
    if link:
        if link == "symbolic":
            (here / "bin" / name).symlink_to(rustup)
        else:
            (here / "bin" / name).hardlink_to(rustup)
    tool = here / "tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{here / 'bin'}:/usr/bin:/bin")
    for other in ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER", "DSYMUTIL"):
        monkeypatch.setenv(f"PEEKY_RUST_{other}", str(tool))
    monkeypatch.setenv(f"PEEKY_RUST_{variable}", name)
    monkeypatch.setattr(cli, "_ON_PATH_SAID", set(), raising=False)
    tools = cli.rust_tools()
    assert Path(tools[variable.lower()]).samefile(rustup)  # rustup, by any of its names
    err = capsys.readouterr().err
    assert "proxy" not in err and "1.96.0" not in err, err
    with pytest.raises(ValueError, match="must be the same real toolchain sysroot binaries"):
        _toolchain(tools["cargo"], tools["rustc"])


def test_a_run_on_a_worker_names_files_that_only_look_like_programs(tmp_path, monkeypatch, capsys):
    # Review round 2: with no compiler run here, a main in a comment or an inactive #if
    # branch was said to define main ("Several C/C++ programs define main here").
    here = tmp_path.resolve()
    (here / "app").mkdir()
    (here / "app" / "main.c").write_text("int main(void) {\n  int x = 1;\n  return x;\n}\n")
    (here / "app" / "old.c").write_text("#if 0\nint main(void) { return 1; }\n#endif\n")
    (here / "app" / "notes.c").write_text("/*\nint main(void) { return 2; }\n*/\n")
    monkeypatch.setenv("PATH", str(here))
    monkeypatch.setenv("PEEKY_STATE_DIR", str(here / "state"))
    monkeypatch.setenv(cli.WORKER_TOKEN_ENV, "a-worker-token-of-some-length")
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{unused.getsockname()[1]}"
    argv = ["probe", str(here / "app"), "--capture", "x", "--worker", url, "--json", "--no-save"]
    assert cli.main([*argv, "--at", "2"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert (error["code"], error["message"]) == (
        "INVALID_REQUEST",
        "Name the program with --at FILE:LINE, e.g. --at main.c:<line>: this directory holds "
        "main.c, notes.c, old.c, and a run on a worker runs no compiler on this host to tell "
        "which defines main",
    )
    # Named, it goes to the worker (here, none answers).
    cli.main([*argv, "--at", "main.c:2"])
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "UNREACHABLE"


@pytest.mark.parametrize(
    "files, program",
    [
        # Review round 3: one inactive textual `main`, and a main a macro makes: the text
        # match picked old.c, which defines no main.
        ({"old.c": "#if 0\nint main(void) { return 1; }\n#endif\n", "gen.c": "MAKE_MAIN"}, None),
        ({"gen.c": "MAKE_MAIN", "util.c": "int helper(void) { return 0; }\n"}, None),
        # A sole file is the program, whatever its text.
        ({"gen.c": "MAKE_MAIN"}, "gen.c"),
    ],
)
def test_a_run_on_a_worker_infers_no_program_among_several_files(
    tmp_path, monkeypatch, files, program
):
    macro = "#define MAKE_MAIN int main(void) { return 0; }\n"
    for name, text in files.items():
        (tmp_path / name).write_text(macro + text + "\n" if text == "MAKE_MAIN" else text)
    monkeypatch.setattr(cli, "native_tools", lambda language: pytest.fail("looked for tools"))
    if program:
        assert cli.resolve(tmp_path, "2", tools=False)[1] == program
        return
    with pytest.raises(ValueError) as refused:
        cli.resolve(tmp_path, "2", tools=False)
    listed = ", ".join(sorted(files))
    assert str(refused.value) == (
        f"Name the program with --at FILE:LINE, e.g. --at {sorted(files)[0]}:<line>: this "
        f"directory holds {listed}, and a run on a worker runs no compiler on this host to "
        "tell which defines main"
    )
    assert cli.resolve(tmp_path, "gen.c:2", tools=False)[1] == "gen.c"
