"""A Rust workspace with a path dependency and a vendored registry dependency, read as Cargo
reads it (rust_workspace.read), and what the build relies on from Cargo and rustc."""

import hashlib
import json
import os
import subprocess

import pytest

from peeky import rust_workspace as rw
from peeky.build import BuildCommandError
from peeky.errors import classify

CHECKSUM = "ab" * 32
FILES = {
    "Cargo.toml": '[workspace]\nmembers = ["app", "util"]\nresolver = "2"\n',
    "app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n\n'
    '[dependencies]\nutil = { path = "../util" }\nsmallnum = "1"\n',
    "app/src/main.rs": 'fn main() {\n    let x = util::double(smallnum::one());\n    println!("{x}");\n}\n',
    "util/Cargo.toml": '[package]\nname = "util"\nversion = "0.1.0"\nedition = "2024"\n',
    "util/src/lib.rs": "pub fn double(x: u32) -> u32 {\n    x * 2\n}\n",
    "vendor/smallnum/Cargo.toml": '[package]\nname = "smallnum"\nversion = "1.0.0"\n'
    'edition = "2021"\n',
    "vendor/smallnum/src/lib.rs": "pub fn one() -> u32 {\n    1\n}\n",
}


def tools():
    cargo, rustc = os.environ.get("PEEKY_RUST_CARGO"), os.environ.get("PEEKY_RUST_RUSTC")
    if not cargo or not rustc:
        pytest.skip("set PEEKY_RUST_* tool paths to enable Rust conformance")
    return cargo, rustc


def checksums(directory, package=CHECKSUM):
    files = {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != ".cargo-checksum.json"
    }
    (directory / ".cargo-checksum.json").write_text(
        json.dumps({"files": files, "package": package})
    )


@pytest.fixture
def workspace(tmp_path):
    cargo, rustc = tools()
    root = tmp_path / "ws"
    for name, text in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    checksums(root / "vendor/smallnum")
    home = tmp_path / "cargo-home"
    home.mkdir()
    env = rw.cargo_environment(rustc, home)
    # As a user would: Cargo writes the lockfile, from the vendored copy.
    subprocess.run(
        [cargo, "generate-lockfile", "--offline", *rw.vendor_arguments(root, "vendor")],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
    )
    return root, home, cargo, rustc


def config(**overrides):
    data = {"analyse": ["util"], "run": {"bin": "app"}, "vendor": "vendor", **overrides}
    return rw.workspace_config(data)[0]


def test_the_workspace_is_read_as_cargo_reads_it(workspace):
    root, home, cargo, rustc = workspace
    shape = rw.read(root, config(), cargo, rustc, home)
    assert [package.name for package in shape.local] == ["app", "util"]
    assert [(package.name, package.edition) for package in shape.analysed] == [("util", "2024")]
    assert [target.source for target in shape.analysed[0].targets] == ["util/src/lib.rs"]
    package, target = shape.run
    assert (package.name, target.name, target.kinds) == ("app", "app", ("bin",))
    assert shape.inputs == ("Cargo.lock", "Cargo.toml", "app/Cargo.toml", "util/Cargo.toml")
    assert shape.vendored == {("smallnum", "1.0.0"): "vendor/smallnum/.cargo-checksum.json"}
    # Cargo builds it from the vendored copy, offline, with Peeky's source replacement, and
    # checks each file of the copy against its checksums as it compiles it.
    env = rw.cargo_environment(rustc, home)
    env["CARGO_TARGET_DIR"] = str(home.parent / "target")
    argv = [cargo, *shape.cargo_arguments(), "--offline", "--locked"]
    argv += rw.vendor_arguments(root, "vendor")
    library = root / "vendor/smallnum/src/lib.rs"
    library.write_text("pub fn one() -> u32 {\n    2\n}\n")
    built = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True)
    assert built.returncode != 0
    assert f"the listed checksum of `{library}` has changed" in built.stderr, built.stderr
    library.write_text(FILES["vendor/smallnum/src/lib.rs"])
    built = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True)
    assert built.returncode == 0, built.stderr


def test_a_lockfile_cargo_would_change_is_a_build_failure_in_cargos_words(workspace):
    root, home, cargo, rustc = workspace
    manifest = root / "util/Cargo.toml"
    manifest.write_text(manifest.read_text() + '\n[dependencies]\nsmallnum = "1"\n')
    with pytest.raises(BuildCommandError) as caught:
        rw.read(root, config(), cargo, rustc, home)
    error = classify(caught.value)
    assert error["code"] == "BUILD_FAILED"
    assert "cannot update the lock file" in error["message"] or "--locked" in error["message"]


def test_cargo_home_must_hold_no_configuration(workspace):
    root, home, cargo, rustc = workspace
    (home / "config.toml").write_text("[net]\noffline = true\n")
    with pytest.raises(ValueError, match="Peeky's own CARGO_HOME holds configuration"):
        rw.read(root, config(), cargo, rustc, home)


def test_rustc_expands_a_crate_for_its_name_alone_on_a_stable_toolchain(workspace):
    """The analysed crates' source scans read them as rustc expands them: `-Zunpretty=
    expanded`, a nightly flag, which RUSTC_BOOTSTRAP set to the crate's name allows for that
    crate's compile alone (docs/adr/0008-native-project-reach.md)."""
    root, home, _, rustc = workspace
    argv = [rustc, "--crate-name", "util", "--edition=2024", "util/src/lib.rs"]
    argv += ["--crate-type", "lib", "-Zunpretty=expanded", "-o", str(home / "util.rs")]
    env = {**rw.cargo_environment(rustc, home), "RUSTC_BOOTSTRAP": "util"}
    done = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    expanded = (home / "util.rs").read_text()
    assert "#![feature(prelude_import)]" in expanded
    assert "pub fn double(x: u32) -> u32 { x * 2 }" in expanded
    env["RUSTC_BOOTSTRAP"] = "other"
    done = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True)
    assert done.returncode != 0 and "only accepted on the nightly compiler" in done.stderr


def test_cargo_configuration_above_the_workspace_is_read_and_recorded(workspace):
    root, home, cargo, rustc = workspace
    # Peeky's environment overrides these flags; Cargo reads the file, and Peeky records it.
    above = root.parent / ".cargo/config.toml"
    above.parent.mkdir()
    above.write_text('[build]\nrustflags = ["--cfg", "peeky_test"]\n[net]\nretry = 2\n')
    shape = rw.read(root, config(), cargo, rustc, home)
    assert (str(above), hashlib.sha256(above.read_bytes()).hexdigest()) in shape.configs_above
    above.write_text("[target.'cfg(unix)']\nlinker = \"/bin/false\"\n")
    with pytest.raises(ValueError, match=r"`target\.cfg\(unix\)\.linker` in .*config\.toml"):
        rw.read(root, config(), cargo, rustc, home)
