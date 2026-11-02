"""`--test`: the project's own test command names the target (test_command, ADR 0009)."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from peeky import test_command as tc
from peeky.cli import main
from peeky.errors import Refused


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# A Rust tool's `-vV`. ``toolchain`` None: a rustup proxy, which picks its toolchain as
# rustup does, from RUSTUP_TOOLCHAIN, the directory's rust-toolchain.toml, then a directory
# override in $RUSTUP_HOME/settings.toml (as `"PATH" = "TOOLCHAIN"` lines), else 1.96.0.
FAKE_RUST = """#!@PYTHON@
import os, re, sys
toolchain = @TOOLCHAIN@
if toolchain is None:
    toolchain = os.environ.get("RUSTUP_TOOLCHAIN")
    if not toolchain and os.path.exists("rust-toolchain.toml"):
        toolchain = re.search(r'channel = "(.*)"', open("rust-toolchain.toml").read())[1]
    settings = os.path.join(os.environ["RUSTUP_HOME"], "settings.toml")
    for line in open(settings) if not toolchain and os.path.exists(settings) else ():
        where, _, name = line.strip().partition(" = ")
        if os.path.realpath(where.strip('"')) == os.path.realpath(os.getcwd()):
            toolchain = name.strip('"')
release, commit = {
    "1.96.0": ("1.96.0", "30a34c6821b57de0aaec83a901aca39f88f6778c"),
    "nightly": ("1.99.0-nightly", "4a1b2c3d4e5f60718293a4b5c6d7e8f901234567"),
}[toolchain or "1.96.0"]
name = os.path.basename(sys.argv[0])
print(f"{name} {release}\\nrelease: {release}\\ncommit-hash: {commit}\\nhost: x86_64-peeky-test")
""".replace("@PYTHON@", sys.executable)


def fake_rust(toolchain):
    return FAKE_RUST.replace("@TOOLCHAIN@", repr(toolchain))


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project directory, the current one, with a `bin` of fake interpreters first on
    PATH."""
    root = tmp_path / "project"
    root.mkdir()
    bin_ = tmp_path / "bin"
    python = executable(bin_ / "python3", "#!/bin/sh\nexit 0\n")
    executable(bin_ / "pytest", f"#!{python}\nimport pytest\n")
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    # Cargo's variables, configuration and toolchains are the test's own, not the machine's:
    # rustup's proxies on PATH, its pinned 1.96.0 Peeky's, and a nightly.
    (tmp_path / "cargo-home").mkdir()
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "cargo-home"))
    (tmp_path / "rustup").mkdir()
    monkeypatch.setenv("RUSTUP_HOME", str(tmp_path / "rustup"))
    for tool in ("cargo", "rustc"):
        executable(bin_ / tool, fake_rust(None))
        for toolchain in ("1.96.0", "nightly"):
            pinned = tmp_path / "toolchains" / toolchain / "bin" / tool
            executable(pinned, fake_rust(toolchain))
        monkeypatch.setenv(
            f"PEEKY_RUST_{tool.upper()}", str(tmp_path / f"toolchains/1.96.0/bin/{tool}")
        )
    for name in list(os.environ):
        if name in tc._FLAGS or name in tc._RUSTC or name.startswith(tc._CARGO_CHANGES):
            monkeypatch.delenv(name)
    monkeypatch.chdir(root)
    return root


def refused(words, root, program=None, cwd=None):
    with pytest.raises(Refused) as caught:
        tc.plan(words, root, program, cwd)
    return caught.value


def test_environment_assignments_come_off_the_front(project):
    assert tc.split_environment(["A=1", "B=x y", "cmd", "C=2"]) == (
        (("A", "1"), ("B", "x y")),
        ("cmd", "C=2"),
    )
    assert tc.split_environment(["env", "A=1", "cmd"]) == ((("A", "1"),), ("cmd",))
    assert "only with assignments" in str(refused(["env", "-i", "cmd"], project))


def test_a_shell_line_or_an_empty_command_is_refused(project):
    assert refused([], project).code == "INVALID_REQUEST"
    for words in (["bash", "-c", "pytest"], ["pytest", "&&", "make"]):
        error = refused(words, project)
        assert error.code == "UNSUPPORTED" and "not a shell line" in str(error)
        # Its own hint: UNSUPPORTED's usual one is about rebuilding a binary.
        assert error.hint.startswith("Nothing ran. Change the test command")


def test_python_dash_m_pytest_runs_pytest_on_that_python(project):
    test = tc.plan(["python3", "-m", "pytest", "-q", "tests/t.py::test_x"], project)
    assert (test.kind, test.module, test.run) == ("python", "pytest", None)
    assert test.python == str(Path(os.environ["PATH"].split(os.pathsep)[0]) / "python3")
    assert test.arguments == ("-q", "tests/t.py::test_x")


def test_a_pytest_script_is_refused_naming_python_dash_m_pytest(project):
    # `python -m pytest` runs none of the script's own lines, and imports otherwise.
    error = refused(["pytest", "-q", "t.py"], project)
    python = Path(os.environ["PATH"].split(os.pathsep)[0]) / "python3"
    assert error.code == "UNSUPPORTED" and "the script's own lines wouldn't run" in str(error)
    assert f"`--test -- {python} -m pytest -q t.py`, the one" in str(error)
    executable(project / "py.test", '#!/bin/sh\nexport X=1\nexec python3 -m pytest "$@"\n')
    error = refused(["./py.test", "-x"], project)
    assert "not as the `./py.test` script" in str(error)
    assert "`--test -- python -m pytest -x`" in str(error)


def test_a_python_script_runs_with_run(project):
    (project / "tests").mkdir()
    (project / "tests/runtests.py").write_text("print(1)\n")
    test = tc.plan(["python3", "tests/runtests.py", "--parallel", "1"], project)
    assert (test.run, test.module, test.arguments) == (
        "tests/runtests.py",
        None,
        ("--parallel", "1"),
    )
    # An executable script, by its `#!` line.
    executable(project / "tests/run2.py", f"#!{test.python}\nprint(2)\n")
    test = tc.plan(["./tests/run2.py", "a"], project)
    assert (test.run, test.arguments) == ("tests/run2.py", ("a",))


def test_python_forms_peeky_cant_run_are_refused_with_the_next_step(project, tmp_path):
    error = refused(["python3", "-m", "sphinx.cmd.build", "-b", "html"], project)
    assert "runpy.run_module('sphinx.cmd.build'" in str(error)
    assert "--test -- python run.py" in str(error)
    assert "PYTHONWARNINGS=error" in str(
        refused(["python3", "-W", "error", "-m", "pytest"], project)
    )
    assert "not `python -c`" in str(refused(["python3", "-c", "1"], project))
    (tmp_path / "outside.py").write_text("")
    assert "outside the target directory" in str(
        refused(["python3", str(tmp_path / "outside.py")], project)
    )
    # Peeky runs Python programs in the target directory.
    (project / "sub").mkdir()
    error = refused(["python3", "-m", "pytest"], project, cwd=project / "sub")
    assert "run it from the target directory" in str(error)


def test_a_binary_inside_the_root_is_the_binary_target(project, tmp_path):
    (project / "build").mkdir()
    (project / "build/prog").write_bytes(b"\x7fELF" + b"\0" * 60)
    test = tc.plan(["./build/prog", "-n", "x y"], project)
    assert (test.kind, test.binary, test.arguments) == ("binary", "build/prog", ("-n", "x y"))
    executable(project / "runtest", "#!/bin/sh\n./build/prog\n")
    assert "is a script" in str(refused(["./runtest"], project))
    outside = tmp_path / "prog"
    outside.write_bytes(b"\x7fELF")
    assert "outside the target directory" in str(refused([str(outside)], project))
    for tool in ("make", "tox", "ant"):
        assert f"can't run `{tool}`" in str(refused([tool, "test"], project))


WORKSPACE = {
    "Cargo.toml": '[workspace]\nmembers = ["app", "util"]\n',
    "app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
    "app/src/main.rs": "fn main() {}\n",
    "util/Cargo.toml": '[package]\nname = "util"\nversion = "0.1.0"\n\n[features]\n'
    'default = ["a"]\na = []\nfull = []\n',
    "util/src/lib.rs": "pub fn f() {}\n",
    "util/tests/flow.rs": "#[test]\nfn flows() {}\n",
    "util/tests/edge.rs": "#[test]\nfn edges() {}\n",
}


@pytest.fixture
def workspace(project):
    for name, text in WORKSPACE.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (project / "vendor").mkdir()
    return project


def test_cargo_test_names_the_package_to_analyse_and_the_test_to_run(workspace):
    test = tc.plan(["cargo", "test", "-p", "util", "--test", "flow"], workspace, "util/src/lib.rs")
    assert test.kind == "rust" and test.arguments == ()
    assert test.config == {
        "workspace": ".",
        "analyse": ["util"],
        "run": {"test": "flow", "package": "util"},
        "vendor": "vendor",
    }
    # The test's environment is Peeky's, as the note says.
    assert test.notes == (tc._RUST_ENVIRONMENT,)
    # Without -p, Cargo finds the package with that test; the filter and the harness's
    # words are the test's arguments. Peeky says it builds one package of several.
    test = tc.plan(
        ["cargo", "test", "--test", "flow", "flows", "--", "--exact"], workspace, "util/src/lib.rs"
    )
    assert test.config["run"] == {"test": "flow", "package": "util"}
    assert test.arguments == ("flows", "--exact")
    assert "Cargo may unify more features" in test.notes[0]


def test_cargo_test_without_a_target_takes_it_from_the_at_file_or_refuses(workspace):
    test = tc.plan(["cargo", "test"], workspace, "util/tests/edge.rs")
    assert test.config["run"] == {"test": "edge", "package": "util"}
    error = refused(["cargo", "test", "-p", "util"], workspace, "util/src/lib.rs")
    assert "(--lib, --test edge, --test flow)" in str(error)
    test = tc.plan(["cargo", "test", "-p", "util", "--lib"], workspace, "util/src/lib.rs")
    assert test.config["run"] == {"lib": True, "package": "util"}


def test_cargo_run_runs_a_binary_with_its_arguments(workspace):
    test = tc.plan(["cargo", "run", "-p", "app", "--", "a", "b"], workspace, "util/src/lib.rs")
    assert test.config["run"] == {"bin": "app", "package": "app"}
    assert test.config["analyse"] == ["util"] and test.arguments == ("a", "b")
    assert "after `--`" in str(
        refused(["cargo", "run", "-p", "app", "x"], workspace, "util/src/lib.rs")
    )


def test_cargo_features(workspace):
    test = tc.plan(
        ["cargo", "test", "--lib", "--features", "a,full", "-F", "x"], workspace, "util/src/lib.rs"
    )
    assert test.config["features"] == ["a", "full", "x"]
    test = tc.plan(["cargo", "test", "--lib", "--all-features"], workspace, "util/src/lib.rs")
    assert test.config["features"] == ["a", "full"]
    # An optional dependency's own feature too, unless a feature names it `dep:`.
    (workspace / "util/Cargo.toml").write_text(
        WORKSPACE["util/Cargo.toml"]
        + 'quiet = ["dep:hidden"]\n\n[dependencies]\nserde = { version = "1", optional = true }\n'
        'hidden = { version = "1", optional = true }\n'
        "[target.'cfg(unix)'.dependencies]\nlibc = { version = \"0.2\", optional = true }\n"
    )
    test = tc.plan(["cargo", "test", "--lib", "--all-features"], workspace, "util/src/lib.rs")
    assert test.config["features"] == ["a", "full", "libc", "quiet", "serde"]


def test_cargo_forms_peeky_cant_build_are_refused_with_why(workspace):
    at = "util/src/lib.rs"
    for words, text in (
        (["cargo", "test", "--release"], "opt-level 0"),
        (["cargo", "test", "--doc"], "doctests"),
        (["cargo", "test", "--workspace"], "one test target"),
        (["cargo", "test", "--no-default-features", "--lib"], "no field for it"),
        (["cargo", "test", "--bin", "app"], "cargo run --bin NAME"),
        (["cargo", "build"], "not `cargo build`"),
        (["cargo", "+nightly", "test"], "its own Rust toolchain"),
        (["cargo", "test", "-Zbuild-std"], "stable Rust"),
        (["cargo", "test", "--test", "a", "--lib"], "one test target"),
    ):
        assert text in str(refused(words, workspace, at)), words
    # Options that change nothing Peeky builds are taken.
    test = tc.plan(
        ["cargo", "test", "-q", "--offline", "-j", "4", "--lib", "--color=never"], workspace, at
    )
    assert test.config["run"] == {"lib": True, "package": "util"}


def test_a_made_configuration_is_saved_by_its_content(tmp_path):
    first = tc.save_config({"b": 1, "a": [2]}, tmp_path)
    assert first == tc.save_config({"a": [2], "b": 1}, tmp_path)
    assert json.loads(first.read_text()) == {"a": [2], "b": 1}
    assert first != tc.save_config({"a": [3]}, tmp_path)


def test_the_cli_takes_the_words_after_dash_dash_as_the_test_command(project, capsys):
    (project / "calc.py").write_text("x = 1\n")
    code = main(
        [
            "probe",
            ".",
            "--at",
            "calc.py:1",
            "--test",
            "--config",
            "c.json",
            "--json",
            "--",
            "pytest",
        ]
    )
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 2 and error["code"] == "INVALID_REQUEST"
    assert "drop --config" in error["message"]
    code = main(["probe", ".", "--at", "calc.py:1", "--test", "--json", "--", "mvn", "test"])
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 2 and "A Java --test stops in a .java file" in error["message"]
    code = main(
        ["probe", ".", "--at", "calc.py:1", "--test", "--worker", "http://127.0.0.1:1", "--json"]
        + ["--", "pytest"]
    )
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 2 and "a worker runs only what its own configuration names" in error["message"]


def test_without_test_the_words_after_dash_dash_stay_the_programs(project, capsys, monkeypatch):
    seen = {}

    def fake(args):
        seen.update(test=args.test, words=args.test_word, arg=args.arg)
        return 0

    monkeypatch.setattr("peeky.cli.probe", fake)
    main(["probe", ".", "--at", "a.py:1", "--", "x", "--test"])
    assert seen == {"test": False, "words": [], "arg": ["x", "--test"]}
    main(["probe", ".", "--at", "a.py:1", "--test", "--", "pytest", "--", "-k"])
    assert seen == {"test": True, "words": ["pytest", "--", "-k"], "arg": []}


def test_a_rust_test_builds_when_needed_and_a_refused_build_stays_refused(monkeypatch, capsys):
    # bat's first build is refused when git-version's `git describe` refreshes .git/index,
    # its entries the same. `--test` doesn't build again: a second build in the same target
    # directory could reuse what the first one's build scripts made of the changed bytes.
    from argparse import Namespace

    from peeky import cli
    from peeky.rust_build import _SAME_ENTRIES, BuildRequired, RustWorkspaceTarget

    calls = []

    def current(self, root):
        raise BuildRequired("No current Peeky build of this workspace: none has been made")

    def build(self, root, **_):
        calls.append(root)
        raise RuntimeError(
            f"Provenance: .git/index changed while Peeky built the workspace{_SAME_ENTRIES}"
            ". Build again"
        )

    monkeypatch.setattr(RustWorkspaceTarget, "current", current)
    monkeypatch.setattr(RustWorkspaceTarget, "build", build)
    target = object.__new__(RustWorkspaceTarget)
    with pytest.raises(RuntimeError, match="Build again"):
        cli._build_first(Namespace(build_needed=True, build=False, quiet=False), ".", target)
    assert len(calls) == 1
    assert "building again" not in capsys.readouterr().err
    # A current build isn't built again either.
    monkeypatch.setattr(RustWorkspaceTarget, "current", lambda self, root: None)
    calls.clear()
    assert cli._build_first(Namespace(build_needed=True, build=False), ".", target) is None
    assert calls == []


def test_assignments_that_wouldnt_reach_a_native_or_rust_run_are_refused(workspace):
    # Peeky runs Cargo and a binary in an environment of its own: an assignment that changes
    # code or the run isn't taken silently. Lints change no code (the study's RUSTFLAGS).
    at = "util/src/lib.rs"
    test = tc.plan(["RUSTFLAGS=-Awarnings", "cargo", "test", "--lib"], workspace, at)
    assert test.notes[-2].startswith("Peeky builds with its own flags, without the command's")
    assert test.notes[-1] == tc._RUST_ENVIRONMENT
    test = tc.plan(
        ["RUSTFLAGS=-D warnings --cap-lints warn", "cargo", "test", "--lib"], workspace, at
    )
    assert "sets only lints" in test.notes[-2]
    error = refused(["RUSTFLAGS=--cfg tokio_unstable", "cargo", "test", "--lib"], workspace, at)
    assert "the command's RUSTFLAGS" in str(error) and "drop it" in str(error)
    assert "build.rustflags" in str(error)
    for words in (["RUST_LOG=debug"], ["HOME=/x"], ["LANG=C"]):
        error = refused([*words, "cargo", "test", "--lib"], workspace, at)
        assert f"{words[0].split('=')[0]}=… in the test command wouldn't reach them" in str(error)
    (workspace / "prog").write_bytes(b"\x7fELF")
    error = refused(["MALLOC_CHECK_=3", "./prog"], workspace)
    assert "runs the binary in an environment of its own" in str(error)
    # HOME is Peeky's for a binary too.
    assert "HOME=… in the test command wouldn't reach it" in str(
        refused(["HOME=/x", "./prog"], workspace)
    )
    # PATH, TMPDIR and the locale are the run's own: taken, with a note of the rest.
    test = tc.plan(["LANG=C", "./prog"], workspace)
    assert test.environment == (("LANG", "C"),)
    assert test.notes == (tc._BINARY_ENVIRONMENT.format(binary="prog"),)
    assert "HOME is a directory of Peeky's" in test.notes[0]


def test_cargo_variables_of_your_environment_are_noted_or_refused(workspace, monkeypatch):
    # Peeky's Cargo runs in an environment of its own: what would change the build or the
    # run is refused, lints are noted, and where the build goes or Cargo's output isn't.
    at = "util/src/lib.rs"
    run = ["cargo", "run", "-p", "app"]
    monkeypatch.setenv("RUSTFLAGS", "--cfg peeky_repro")
    error = refused(run, workspace, at)
    assert "your environment's RUSTFLAGS ('--cfg peeky_repro')" in str(error)
    assert "unset it" in str(error)
    monkeypatch.setenv("RUSTFLAGS", "-Awarnings")
    test = tc.plan(run, workspace, at)
    assert "without your environment's RUSTFLAGS: it sets only lints" in test.notes[0]
    # With RUSTFLAGS, Cargo leaves out the configuration's rustflags; Peeky's build doesn't.
    (workspace / ".cargo").mkdir()
    (workspace / ".cargo/config.toml").write_text('[build]\nrustflags = ["--cfg", "x"]\n')
    assert "leave out the `rustflags` of" in str(refused(run, workspace, at))
    (workspace / ".cargo/config.toml").unlink()
    monkeypatch.delenv("RUSTFLAGS")
    monkeypatch.setenv("CARGO_ENCODED_RUSTFLAGS", "-Awarnings\x1f--cfg\x1fx")
    assert "CARGO_ENCODED_RUSTFLAGS" in str(refused(run, workspace, at))
    monkeypatch.delenv("CARGO_ENCODED_RUSTFLAGS")
    for name, value in (
        ("CARGO_PROFILE_DEV_OVERFLOW_CHECKS", "false"),
        ("CARGO_BUILD_TARGET", "x86_64-unknown-linux-musl"),
        ("RUSTUP_TOOLCHAIN", "nightly"),
        ("RUSTC_WRAPPER", "sccache"),
    ):
        monkeypatch.setenv(name, value)
        error = refused(run, workspace, at)
        assert f"your environment's {name}={value}" in str(error), name
        monkeypatch.delenv(name)
    error = refused(["CARGO_PROFILE_DEV_DEBUG_ASSERTIONS=false", *run], workspace, at)
    assert "the command's CARGO_PROFILE_DEV_DEBUG_ASSERTIONS" in str(error)
    for name, value in (
        ("CARGO_TARGET_DIR", "/elsewhere"),
        ("CARGO_TERM_COLOR", "never"),
        ("CARGO_NET_OFFLINE", "true"),
        ("CARGO_BUILD_JOBS", "2"),
    ):
        monkeypatch.setenv(name, value)
    assert tc.plan(run, workspace, at).notes == (tc._RUST_ENVIRONMENT,)


def test_a_cargo_runner_is_refused(workspace, monkeypatch):
    # `cargo test` runs the test through a runner; Peeky runs its executable itself.
    at = "util/src/lib.rs"
    run = ["cargo", "test", "-p", "util", "--lib"]
    monkeypatch.setattr(tc, "_host", lambda: ("x86_64-peeky-test", ('target_os="linux"', "unix")))
    monkeypatch.setenv("CARGO_TARGET_X86_64_PEEKY_TEST_RUNNER", "valgrind")
    assert "through CARGO_TARGET_X86_64_PEEKY_TEST_RUNNER (valgrind)" in str(
        refused(run, workspace, at)
    )
    monkeypatch.delenv("CARGO_TARGET_X86_64_PEEKY_TEST_RUNNER")
    (workspace / ".cargo").mkdir()
    config = workspace / ".cargo/config.toml"
    for key in ("x86_64-peeky-test", "'cfg(unix)'"):
        config.write_text(f'[target.{key}]\nrunner = "qemu"\n')
        error = refused(run, workspace, at)
        assert "runner` (qemu)" in str(error) and str(config) in str(error), key
    # Another target's runner, or one whose cfg doesn't hold here, isn't Cargo's for this.
    config.write_text(
        '[target.thumbv7em-none-eabihf]\nrunner = "probe-rs run"\n'
        "[target.'cfg(windows)']\nrunner = \"wine\"\n"
    )
    assert tc.plan(run, workspace, at).config["run"] == {"lib": True, "package": "util"}
    # $CARGO_HOME's configuration counts too: Cargo reads it, and Peeky's build doesn't.
    config.unlink()
    home = Path(os.environ["CARGO_HOME"])
    (home / "config.toml").write_text('[target."cfg(unix)"]\nrunner = "qemu"\n')
    assert "runner` (qemu)" in str(refused(run, workspace, at))
    (home / "config.toml").write_text("[profile.dev]\noverflow-checks = false\n")
    error = refused(run, workspace, at)
    assert "Peeky's build doesn't: its `profile`" in str(error)
    (home / "config.toml").write_text("[net]\noffline = true\n[build]\njobs = 2\n")
    assert tc.plan(run, workspace, at).config["run"] == {"lib": True, "package": "util"}


@pytest.fixture
def ctest(project, tmp_path):
    """A `ctest` on PATH that lists ctest.json's tests (in the directory it runs in), and
    keeps the words it was given."""
    executable(
        tmp_path / "bin/ctest",
        f"#!{sys.executable}\nimport json, sys\n"
        "open('ctest.args', 'w').write(json.dumps(sys.argv[1:]))\n"
        "print(open('ctest.json').read())\n",
    )
    (project / "build").mkdir()
    (project / "build/t").write_bytes(b"\x7fELF")

    def listing(*tests):
        (project / "ctest.json").write_text(json.dumps({"kind": "ctestInfo", "tests": tests}))

    return listing


def one(name="t1", directory=None, **properties):
    return {
        "name": name,
        "command": [str(Path.cwd() / "build/t"), "--case", name],
        "properties": [
            {"name": "WORKING_DIRECTORY", "value": str(directory or Path.cwd())},
            *({"name": k, "value": v} for k, v in properties.items()),
        ],
    }


def test_ctest_runs_its_one_test_as_ctest_would_or_refuses(project, ctest):
    words = ["ctest", "--test-dir", "build", "-R", "^t1$", "-V", "--output-on-failure"]
    ctest(one(LABELS=["unit"], COST=1.5, TIMEOUT=30.0))
    test = tc.plan(words, project)
    assert (test.kind, test.binary, test.arguments) == ("binary", "build/t", ("--case", "t1"))
    assert json.loads((project / "ctest.args").read_text()) == [*words[1:], "--show-only=json-v1"]
    assert "ctest would stop t1 at its TIMEOUT (30.0)" in test.notes[0]
    assert test.notes[-1] == tc._BINARY_ENVIRONMENT.format(binary="build/t")
    # Where ctest would run it otherwise than Peeky runs a binary, nothing runs.
    for listed, text in (
        (one(directory=project / "build"), "ctest runs t1 in"),
        (one(ENVIRONMENT=["A=1"]), 'its ENVIRONMENT ["A=1"]: Peeky runs a binary in an'),
        (one(ENVIRONMENT_MODIFICATION=["B=set:2"]), "its ENVIRONMENT_MODIFICATION"),
        (one(DISABLED=True), "ctest doesn't run t1: it is DISABLED"),
        (one(RESOURCE_GROUPS=["gpus:1"]), "its RESOURCE_GROUPS"),
        (one(REQUIRED_FILES=["missing.txt"]), "REQUIRED_FILES missing.txt doesn't exist"),
    ):
        ctest(listed)
        assert text in str(refused(words, project)), text
    ctest(one("t1"), one("t2"))
    assert "ctest selects 2 tests (t1, t2)" in str(refused(words, project))
    # Options that change how ctest runs a test are refused before it lists any.
    ctest(one())
    for option in (
        ["--repeat", "until-fail:3"],
        ["--schedule-random"],
        ["-j", "4"],
        ["-j4"],
        ["--parallel", "2"],
        ["--timeout", "5"],
        ["--stop-on-failure"],
        ["--preset", "ci"],
        ["-I", "1,1"],
    ):
        error = refused([*words, *option], project)
        assert f"can't take ctest's {option[0]}" in str(error), option


def test_without_p_cargo_acts_on_the_root_package_or_default_members(workspace):
    # A root package: `cargo run` and `cargo test` act on it, not on the --at file's member.
    (workspace / "Cargo.toml").write_text(
        '[package]\nname = "root"\nversion = "0.1.0"\n\n[workspace]\nmembers = ["app", "util"]\n'
    )
    (workspace / "src").mkdir()
    (workspace / "src/main.rs").write_text("fn main() {}\n")
    test = tc.plan(["cargo", "run", "--", "in.txt"], workspace, "util/src/lib.rs")
    assert test.config["run"] == {"bin": "root", "package": "root"}
    assert test.config["analyse"] == ["util"] and test.arguments == ("in.txt",)
    assert test.notes == (tc._RUST_ENVIRONMENT,)
    error = refused(["cargo", "test", "name"], workspace, "util/src/lib.rs")
    assert "without -p runs root's tests" in str(error) and "add -p util" in str(error)
    # The root's package alone: Cargo looks for the target there, and the build says if
    # it has none.
    test = tc.plan(["cargo", "test", "--test", "flow"], workspace, "util/src/lib.rs")
    assert test.config["run"] == {"test": "flow", "package": "root"}
    # `default-members` chooses instead.
    (workspace / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["app", "util"]\ndefault-members = ["util"]\n'
    )
    test = tc.plan(["cargo", "test", "--lib"], workspace, "util/src/lib.rs")
    assert test.config["run"] == {"lib": True, "package": "util"}
    assert test.notes == (tc._RUST_ENVIRONMENT,)


def test_targets_are_discovered_as_cargo_discovers_them(workspace):
    # bat's binary is src/bin/bat/main.rs.
    (workspace / "app/src/bin/tool").mkdir(parents=True)
    (workspace / "app/src/bin/tool/main.rs").write_text("fn main() {}\n")
    (workspace / "util/tests/deep").mkdir()
    (workspace / "util/tests/deep/main.rs").write_text("")
    manifests, selected = tc._packages(workspace)
    assert manifests["app"]["bins"] == ["app", "tool"]
    assert manifests["util"]["tests"] == ["deep", "edge", "flow"]
    assert selected == ["app", "util"]
    test = tc.plan(["cargo", "run", "--bin", "tool"], workspace, "util/src/lib.rs")
    assert test.config["run"] == {"bin": "tool", "package": "app"}
    error = refused(["cargo", "run", "--bin", "nope"], workspace, "util/src/lib.rs")
    assert "no bin target nope in the packages Cargo selects (app, util)" in str(error)


def test_a_toolchain_other_than_peekys_is_refused(workspace, tmp_path):
    # Peeky builds with its pinned Rust 1.96.0; the command's cargo and the rustc it runs are
    # compared with it, as the command would pick them, before anything runs.
    at, run = "util/src/lib.rs", ["test", "-p", "util", "--lib"]
    assert tc.plan(["cargo", *run], workspace, at).kind == "rust"
    # A cargo named by its path.
    nightly = str(tmp_path / "toolchains/nightly/bin/cargo")
    error = refused([nightly, *run], workspace, at)
    assert "The test command's cargo is Rust 1.99.0-nightly (commit 4a1b2c3d4, host " in str(error)
    assert f"x86_64-peeky-test) ({nightly}, run from {workspace})" in str(error)
    assert "Peeky builds with Rust 1.96.0 (commit 30a34c682, host x86_64-peeky-test)" in str(error)
    # The project's rust-toolchain.toml, which rustup's proxies read in the root.
    (workspace / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "nightly"\n')
    assert "command's cargo is Rust 1.99" in str(refused(["cargo", *run], workspace, at))
    # Peeky's own cargo by its path, but the rustc on PATH, which Cargo runs, is nightly's.
    pinned = str(tmp_path / "toolchains/1.96.0/bin/cargo")
    error = refused([pinned, *run], workspace, at)
    assert "The test command's rustc is Rust 1.99.0-nightly" in str(error)
    (workspace / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.96.0"\n')
    assert tc.plan(["cargo", *run], workspace, at).kind == "rust"
    (workspace / "rust-toolchain.toml").unlink()
    # A directory override (`rustup override set nightly` in the root).
    settings = tmp_path / "rustup/settings.toml"
    settings.write_text(f'"{workspace}" = "nightly"\n')
    error = refused(["cargo", *run], workspace, at)
    assert "cargo is Rust 1.99.0-nightly" in str(error) and "rustup override" in str(error)
    settings.unlink()
    # A cargo that can't say which it is.
    executable(tmp_path / "bin/cargo", "#!/bin/sh\nexit 1\n")
    assert "can't read the version of the cargo" in str(refused(["cargo", *run], workspace, at))
