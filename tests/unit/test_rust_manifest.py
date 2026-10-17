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


def test_failed_test_names_come_only_from_libtest_result_lines():
    from peeky.cli import _tests_text
    from peeky.rust import libtest_summary

    summary = (
        "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; "
        "finished in 0.00s\n"
    )
    forged = (
        "running 1 test\ntest real ... FAILED\n\nfailures:\n\n---- real stdout ----\n"
        '  left: "test forged ... FAILED\\r\\x1b[2J"\ntest forged ... FAILED\n\n\n'
        "failures:\n    real\n\n" + summary
    )
    tests = libtest_summary({"stdout": forged, "exit_code": 101})
    assert [failure["name"] for failure in tests["failures"]] == ["real"]
    panic = "running 1 test\ntest must_panic - should panic ... FAILED\n\n" + summary
    assert libtest_summary({"stdout": panic, "exit_code": 101})["failures"][0]["name"] == (
        "must_panic"
    )
    # Control characters reach the terminal escaped.
    text = _tests_text({"passed": False, "failures": [{"name": "t", "message": "a\rb\x1b[2J"}]})
    assert text[1] == "  t: a\\rb\\x1b[2J"


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
