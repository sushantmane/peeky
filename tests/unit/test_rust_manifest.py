from pathlib import Path

import pytest

from peeky.rust_manifest import assert_cargo_ancestry_clear, validate_cargo_inputs

MANIFEST = """[package]
name = "peeky-pagination"
version = "0.1.0"
edition = "2021"
build = false
autobins = false
autotests = false
autoexamples = false
autobenches = false
autolib = false

[[bin]]
name = "peeky-pagination"
path = "src/main.rs"

[[test]]
name = "pagination"
path = "tests/pagination.rs"
"""
LOCK = """version = 4
[[package]]
name = "peeky-pagination"
version = "0.1.0"
"""


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for name, text in (
        ("Cargo.toml", MANIFEST),
        ("Cargo.lock", LOCK),
        ("src/main.rs", "fn main() {}"),
        ("tests/pagination.rs", "#[test] fn test() {}"),
    ):
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
    return root


def validate(project, **overrides):
    return validate_cargo_inputs(
        project, **{"sources": ("src/main.rs",), "tests": ("tests/pagination.rs",), **overrides}
    )


def test_explicit_inputs_preserve_editable_and_immutable_sets(project):
    spec = validate(project)
    assert spec.entrypoint == "src/main.rs"
    assert spec.test_entrypoint == "tests/pagination.rs"
    assert spec.inputs == ("Cargo.toml", "Cargo.lock", "src/main.rs", "tests/pagination.rs")


@pytest.mark.parametrize(
    "table",
    [
        '[dependencies]\nserde="1"',
        '[dev-dependencies]\nhelper={path="../helper"}',
        '[build-dependencies]\ncc="1"',
        "[workspace]\nmembers=[]",
        "[profile.dev]\nopt-level=3",
        '[target.x86_64-unknown-linux-gnu.dependencies]\nhelper="1"',
        '[patch.crates-io]\nhelper={path="../helper"}',
    ],
)
def test_unsupported_cargo_execution_inputs_are_rejected(project, table):
    (project / "Cargo.toml").write_text(MANIFEST + "\n" + table)
    with pytest.raises(ValueError, match="Unsupported Cargo manifest"):
        validate(project)


@pytest.mark.parametrize(
    "field", ["build", "autobins", "autotests", "autoexamples", "autobenches", "autolib"]
)
def test_implicit_targets_and_scripts_disabled(project, field):
    (project / "Cargo.toml").write_text(MANIFEST.replace(f"{field} = false", f"{field} = true"))
    with pytest.raises(ValueError, match="disabled"):
        validate(project)


@pytest.mark.parametrize(
    "replacement",
    [
        'path = "../outside.rs"',
        'path = "/tmp/elsewhere.rs"',
        'path = "tests/pagination.rs"',
    ],
)
def test_target_cannot_select_undeclared_or_immutable_source(project, replacement):
    (project / "Cargo.toml").write_text(MANIFEST.replace('path = "src/main.rs"', replacement))
    with pytest.raises(ValueError, match="explicitly declared"):
        validate(project)


@pytest.mark.parametrize(
    "lock",
    [
        LOCK.replace('version = "0.1.0"', 'version = "0.2.0"'),
        LOCK.replace("version = 4", "version = 3"),
        LOCK + 'dependencies = ["helper"]\n',
        LOCK + 'source = "registry+https://example.invalid"\n',
        LOCK + '\n[[package]]\nname="helper"\nversion="1.0.0"\n',
        "version = true\n",
    ],
)
def test_mismatched_or_dependency_lockfile_rejected(project, lock):
    (project / "Cargo.lock").write_text(lock)
    with pytest.raises(ValueError, match="lockfile"):
        validate(project)


@pytest.mark.parametrize("file", ["Cargo.toml", "Cargo.lock", "src/main.rs", "tests/pagination.rs"])
def test_symlinked_declared_input_rejected(project, file):
    path = project / file
    outside = project.parent / "outside"
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="Symlinks"):
        validate(project)


@pytest.mark.parametrize("file", ["Cargo.toml", "Cargo.lock"])
def test_oversized_or_invalid_toml_rejected(project, file):
    (project / file).write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match=f"{file} must be a regular file of at most 1 MiB"):
        validate(project)
    (project / file).write_bytes(b"\xff")
    with pytest.raises(ValueError, match="Invalid"):
        validate(project)


def test_source_roles_are_unique_and_immutable(project):
    with pytest.raises(ValueError, match="Duplicate"):
        validate(project, sources=("src/main.rs", "src/main.rs"))
    with pytest.raises(ValueError, match="distinct"):
        validate(project, tests=("src/main.rs",))
    with pytest.raises(ValueError, match="immutable"):
        validate(project, sources=["src/main.rs"])


@pytest.mark.parametrize("relative", [".cargo/config", ".cargo/config.toml", "Cargo.toml"])
def test_ancestor_configuration_rejected(tmp_path, relative):
    workspace = tmp_path / "staging" / "owned"
    workspace.mkdir(parents=True)
    assert_cargo_ancestry_clear(workspace)
    config = tmp_path / relative
    config.parent.mkdir(exist_ok=True)
    config.write_text("")
    with pytest.raises(ValueError, match="Cargo"):
        assert_cargo_ancestry_clear(workspace)


def test_dangling_ambient_config_link_rejected(tmp_path):
    workspace = tmp_path / "owned"
    workspace.mkdir()
    config = tmp_path / ".cargo"
    config.mkdir()
    (config / "config.toml").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="configuration"):
        assert_cargo_ancestry_clear(workspace)


def test_project_manifest_allowed_but_own_config_rejected(project):
    assert_cargo_ancestry_clear(project)
    (project / ".cargo").symlink_to(Path("missing-directory"))
    with pytest.raises(ValueError, match="symlink"):
        assert_cargo_ancestry_clear(project)


def test_a_missing_lockfile_says_how_to_create_it(project):
    (project / "Cargo.lock").unlink()
    with pytest.raises(ValueError, match="Cargo.lock is missing: run `cargo generate-lockfile`"):
        validate(project)


LIBTEST_FAILURE = """
running 2 tests
test first ... ok
test second ... FAILED

failures:

---- second stdout ----

thread 'second' panicked at tests/cli.rs:6:5:
assertion `left == right` failed
  left: "TOP:[9, 1]"
 right: "TOP:[9, 7]"
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace


failures:
    second

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""


def test_failed_cargo_tests_are_named_with_how_they_failed():
    from peeky.cli import _tests_text
    from peeky.rust import libtest_summary

    tests = libtest_summary({"stdout": LIBTEST_FAILURE, "exit_code": 101})
    assert tests["passed"] is False
    [failure] = tests["failures"]
    assert failure["name"] == "second"
    assert failure["message"].startswith("thread 'second' panicked at tests/cli.rs:6:5:")
    assert failure["message"].endswith('right: "TOP:[9, 7]"')
    lines = _tests_text(tests)
    assert lines[0] == "compiled tests: failed (1 passed, 1 failed)"
    assert lines[1].startswith("  second: thread 'second' panicked")
    passed = (
        "test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; "
        "finished in 0.00s\n"
    )
    clean = libtest_summary({"stdout": passed, "exit_code": 0})
    assert "failures" not in clean and _tests_text(clean) == ["compiled tests: passed (3 passed)"]


def test_failed_test_names_come_only_from_libtests_final_list():
    from peeky.cli import _tests_text
    from peeky.rust import libtest_summary

    def run(stdout, failed=1):
        summary = (
            f"test result: FAILED. 0 passed; {failed} failed; 0 ignored; 0 measured; "
            "0 filtered out; finished in 0.00s\n"
        )
        return libtest_summary({"stdout": stdout + summary, "exit_code": 101})

    # The program wrote into libtest's stdout among the result lines.
    forged = (
        "running 1 test\ntest real ... \ntest forged ... FAILED\nFAILED\n\nfailures:\n\n"
        "---- real stdout ----\nreal panic\n\nfailures:\n    real\n\n"
    )
    assert [failure["name"] for failure in run(forged)["failures"]] == ["real"]
    # Another test's escaped output cannot supply this test's message.
    two = (
        "running 2 tests\ntest a ... FAILED\ntest b ... FAILED\n\nfailures:\n\n"
        '---- a stdout ----\n  left: "---- b stdout ----\\nfake"\n\n'
        "---- b stdout ----\nreal b panic\n\nfailures:\n    a\n    b\n\n"
    )
    assert [f["message"] for f in run(two, failed=2)["failures"]] == [
        'left: "---- b stdout ----\\nfake"',
        "real b panic",
    ]
    # should_panic: libtest's note has no newline; the footer is not part of it.
    panic = (
        "running 1 test\ntest must_panic - should panic ... FAILED\n\nfailures:\n\n"
        "---- must_panic stdout ----\nnote: test did not panic as expected at t.rs:15:4"
        "\nfailures:\n    must_panic\n\n"
    )
    [failure] = run(panic)["failures"]
    assert failure == {
        "name": "must_panic",
        "message": "note: test did not panic as expected at t.rs:15:4",
    }
    # Control characters reach the terminal escaped, in names and messages.
    text = _tests_text(
        {"passed": False, "failures": [{"name": "t\x1b[2J", "message": "a\rb\x1b[2J"}]}
    )
    assert text[1] == "  t\\x1b[2J: a\\rb\\x1b[2J"


def test_compiled_test_text_says_why_when_nothing_ran_or_output_overflowed():
    from peeky.cli import _tests_text

    empty = {
        "status": "ok",
        "passed": 0,
        "failed": 0,
        "ignored": 0,
        "measured": 0,
        "filtered_out": 0,
    }
    assert _tests_text({"passed": False, "summary": empty}) == [
        "compiled tests: failed (0 tests ran)"
    ]
    overflow = _tests_text({"passed": False, "summary": None, "output_limit_exceeded": True})
    assert overflow == [
        "compiled tests: failed (no test summary)",
        "  (the tests wrote more output than Peeky keeps)",
    ]
    exited = _tests_text({"passed": False, "summary": {**empty, "passed": 2}, "exit_code": 3})
    assert exited[-1] == "  (the test binary exited with code 3)"


@pytest.mark.parametrize("test_name", ["peeky-pagination", "peeky_pagination"])
def test_test_target_sharing_the_binary_crate_name_is_refused_with_the_fix(project, test_name):
    (project / "Cargo.toml").write_text(MANIFEST.replace('"pagination"', f'"{test_name}"'))
    with pytest.raises(ValueError, match=r'must not share the \[\[bin\]\] name.*_test"') as error:
        validate(project)
    assert 'name = "peeky_pagination_test"' in str(error.value)


PLAIN = """[package]
name = "plain"
version = "0.1.0"
edition = "2024"

[dependencies]
"""
PLAIN_LOCK = 'version = 4\n[[package]]\nname = "plain"\nversion = "0.1.0"\n'


@pytest.fixture
def plain(tmp_path):
    root = tmp_path / "plain"
    for name, text in (
        ("Cargo.toml", PLAIN),
        ("Cargo.lock", PLAIN_LOCK),
        ("src/main.rs", "fn main() {}"),
        ("tests/real.rs", "#[test] fn t() {}"),
    ):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


def test_hidden_files_in_tests_are_not_test_targets(plain):
    # Cargo skips `._real.rs` (macOS) and `.#real.rs` (Emacs locks).
    from peeky.rust_manifest import cargo_layout

    (plain / "tests/._real.rs").write_text("")
    (plain / "tests/.#real.rs").symlink_to("nowhere")
    assert cargo_layout(plain) == ("src/main.rs", ("tests/real.rs",))
    spec = validate_cargo_inputs(plain, sources=("src/main.rs",), tests=("tests/real.rs",))
    assert (spec.binary, spec.test, spec.test_entrypoint) == ("plain", "real", "tests/real.rs")


def test_a_bin_elsewhere_beside_src_main_rs_is_refused_up_front(plain):
    # Cargo would infer src/main.rs as a second binary.
    (plain / "src/app.rs").write_text("fn main() {}")
    (plain / "Cargo.toml").write_text(PLAIN + '\n[[bin]]\nname = "foo"\npath = "src/app.rs"\n')
    sources = ("src/app.rs", "src/main.rs")
    with pytest.raises(ValueError, match=r"src/main.rs would be a second binary \(plain\)"):
        validate_cargo_inputs(plain, sources=sources, tests=("tests/real.rs",))
    (plain / "Cargo.toml").write_text(PLAIN + '\n[[bin]]\nname = "plain"\npath = "src/app.rs"\n')
    assert validate_cargo_inputs(plain, sources=sources, tests=("tests/real.rs",)).binary == "plain"
