"""Independent edge cases for the dependency-free Cargo preflight contract."""

import pytest

from peeky.rust_manifest import assert_cargo_ancestry_clear, validate_cargo_inputs


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.rs").write_text("fn main() {}")
    (root / "check.rs").write_text("#[test] fn check() {}")
    (root / "Cargo.toml").write_text("""[package]
name = "fixture"
version = "0.1.0"
edition = "2021"
build = false
autobins = false
autotests = false
autoexamples = false
autobenches = false
autolib = false
[[bin]]
name = "fixture"
path = "main.rs"
[[test]]
name = "check"
path = "check.rs"
""")
    (root / "Cargo.lock").write_text(
        'version = 4\n[[package]]\nname = "fixture"\nversion = "0.1.0"\n'
    )
    return root


def validate(root):
    return validate_cargo_inputs(root, sources=("main.rs",), tests=("check.rs",))


@pytest.mark.parametrize("component", [0, 1, 2])
def test_manifest_version_components_must_fit_cargo_semver(project, component):
    pieces = ["0", "1", "0"]
    pieces[component] = str(1 << 64)
    version = ".".join(pieces)
    for name in ("Cargo.toml", "Cargo.lock"):
        path = project / name
        path.write_text(path.read_text().replace('"0.1.0"', f'"{version}"'))
    with pytest.raises(ValueError, match="version"):
        validate(project)


@pytest.mark.parametrize(
    "hook", ["harness = false", 'runner = "external"', 'required-features = ["hidden"]']
)
def test_test_target_cannot_change_harness_or_execution_selection(project, hook):
    manifest = project / "Cargo.toml"
    manifest.write_text(manifest.read_text() + hook + "\n")
    with pytest.raises(ValueError, match="only name and path"):
        validate(project)


def test_nested_source_directory_symlink_is_rejected(project):
    alias = project / "alias"
    alias.symlink_to(project, target_is_directory=True)
    manifest = project / "Cargo.toml"
    manifest.write_text(manifest.read_text().replace('"main.rs"', '"alias/main.rs"'))
    with pytest.raises(ValueError, match="Symlinks"):
        validate_cargo_inputs(project, sources=("alias/main.rs",), tests=("check.rs",))


@pytest.mark.parametrize("name", ["config", "config.toml"])
def test_ancestry_rejects_both_config_names_even_when_other_exists(tmp_path, name):
    root = tmp_path / "nested" / "project"
    root.mkdir(parents=True)
    cargo = tmp_path / ".cargo"
    cargo.mkdir()
    (cargo / name).write_text('[env]\nUNEXPECTED = "yes"\n')
    with pytest.raises(ValueError, match="configuration"):
        assert_cargo_ancestry_clear(root)


def test_dangling_ancestor_workspace_manifest_is_not_ignored(tmp_path):
    root = tmp_path / "nested" / "project"
    root.mkdir(parents=True)
    (tmp_path / "Cargo.toml").symlink_to(tmp_path / "missing.toml")
    with pytest.raises(ValueError, match="Ancestor"):
        assert_cargo_ancestry_clear(root)
