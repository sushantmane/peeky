"""`--test`: the project's own test command names the target (test_command, ADR 0009)."""

import json
import os
import stat
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


def test_a_pytest_console_script_runs_on_its_first_lines_python(project):
    test = tc.plan(["pytest", "-q", "t.py"], project)
    assert (test.module, test.arguments) == ("pytest", ("-q", "t.py"))
    assert test.python.endswith("/bin/python3")


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
    assert test.notes == ()
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
    assert code == 2 and error["code"] == "UNSUPPORTED" and "Java" in error["message"]
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


def test_a_rust_test_builds_when_needed_and_again_after_gits_index_refresh(monkeypatch, capsys):
    # bat's first build is refused when git-version's `git describe` refreshes .git/index,
    # its entries the same: `peeky build` again builds it, so `--test` does that once.
    from argparse import Namespace

    from peeky import cli
    from peeky.rust_build import _SAME_ENTRIES, BuildRequired, RustWorkspaceTarget

    calls = []

    def current(self, root):
        raise BuildRequired("No current Peeky build of this workspace: none has been made")

    def build(self, root, **_):
        calls.append(root)
        if len(calls) == 1:
            raise RuntimeError(
                f"Provenance: .git/index changed while Peeky built the workspace{_SAME_ENTRIES}"
                ". Build again"
            )
        return {"notes": []}

    monkeypatch.setattr(RustWorkspaceTarget, "current", current)
    monkeypatch.setattr(RustWorkspaceTarget, "build", build)
    target = object.__new__(RustWorkspaceTarget)
    cli._build_first(Namespace(build_needed=True, build=False, quiet=False), ".", target)
    assert len(calls) == 2
    assert "peeky: building again: .git/index changed" in capsys.readouterr().err
    # Without --test (`--build`), the refusal stands: the build is the user's to repeat.
    calls.clear()
    with pytest.raises(RuntimeError, match="Build again"):
        cli._build_first(Namespace(build=True, quiet=True), ".", target)
    assert len(calls) == 1
    # Another provenance refusal is never built again.
    monkeypatch.setattr(
        RustWorkspaceTarget,
        "build",
        lambda self, root, **_: (_ for _ in ()).throw(
            RuntimeError("Provenance: Cargo.lock changed")
        ),
    )
    with pytest.raises(RuntimeError, match="Cargo.lock"):
        cli._build_first(Namespace(build_needed=True, build=False, quiet=True), ".", target)


def test_assignments_that_wouldnt_reach_a_native_or_rust_run_are_refused(workspace):
    # Peeky runs Cargo and a binary in an environment of its own: an assignment that changes
    # code or the run isn't taken silently. Lints change no code (the study's RUSTFLAGS).
    at = "util/src/lib.rs"
    test = tc.plan(["RUSTFLAGS=-Awarnings", "cargo", "test", "--lib"], workspace, at)
    assert test.notes[-1].startswith("Peeky builds with its own flags, without the command's")
    test = tc.plan(
        ["RUSTFLAGS=-D warnings --cap-lints warn", "cargo", "test", "--lib"], workspace, at
    )
    assert "sets only lints" in test.notes[-1]
    error = refused(["RUSTFLAGS=--cfg tokio_unstable", "cargo", "test", "--lib"], workspace, at)
    assert "wouldn't reach them: drop it" in str(error) and "build.rustflags" in str(error)
    assert "RUST_LOG=…" in str(refused(["RUST_LOG=debug", "cargo", "test", "--lib"], workspace, at))
    (workspace / "prog").write_bytes(b"\x7fELF")
    error = refused(["MALLOC_CHECK_=3", "./prog"], workspace)
    assert "runs the binary in an environment of its own" in str(error)
    # PATH, HOME and the locale are the run's own: taken.
    assert tc.plan(["LANG=C", "./prog"], workspace).environment == (("LANG", "C"),)


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
    assert test.notes == ()
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
    assert test.config["run"] == {"lib": True, "package": "util"} and test.notes == ()


def test_pytest_says_where_python_dash_m_pytest_imports_otherwise(project):
    # `python -m pytest` puts the root first on sys.path; the pytest script doesn't.
    (project / "pkg").mkdir()
    (project / "pkg/__init__.py").write_text("")
    test = tc.plan(["pytest", "tests"], project, "pkg/mod.py")
    assert "imports `pkg` from the root" in test.notes[0]
    assert tc.plan(["pytest", "tests"], project, "src/pkg/mod.py").notes == ()
    assert tc.plan(["python3", "-m", "pytest"], project, "pkg/mod.py").notes == ()


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
