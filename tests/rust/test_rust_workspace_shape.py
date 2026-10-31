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
    shape = rw.read(root, config(), cargo, rustc, home, home.parent / "target")
    assert [package.name for package in shape.local] == ["app", "util"]
    assert [(package.name, package.edition) for package in shape.analysed] == [("util", "2024")]
    assert [target.source for target in shape.analysed[0].targets] == ["util/src/lib.rs"]
    package, target = shape.run
    assert (package.name, target.name, target.kinds) == ("app", "app", ("bin",))
    assert shape.inputs == ("Cargo.lock", "Cargo.toml", "app/Cargo.toml", "util/Cargo.toml")
    assert shape.vendored == {("smallnum", "1.0.0"): "vendor/smallnum"}
    files = shape.watch["files"]
    for name in ("Cargo.lock", "app/Cargo.toml"):
        sha = hashlib.sha256((root / name).read_bytes()).hexdigest()
        assert files[str(root / name)] == ["file", str(root / name), sha]
    sha = hashlib.sha256((root / "vendor/smallnum/src/lib.rs").read_bytes()).hexdigest()
    assert shape.watch["vendored"]["vendor/smallnum/src/lib.rs"] == sha
    # Cargo builds it from the vendored copy, offline, with Peeky's source replacement, and
    # checks each file of the copy against its checksums as it compiles it.
    env = rw.build_environment(rustc, home, home.parent / "target", shape.rustflags)
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
        rw.read(root, config(), cargo, rustc, home, home.parent / "target")
    error = classify(caught.value)
    assert error["code"] == "BUILD_FAILED"
    assert "cannot update the lock file" in error["message"] or "--locked" in error["message"]


def test_cargo_home_must_hold_no_configuration(workspace):
    root, home, cargo, rustc = workspace
    (home / "config.toml").write_text("[net]\noffline = true\n")
    with pytest.raises(ValueError, match="Peeky's own CARGO_HOME holds configuration"):
        rw.read(root, config(), cargo, rustc, home, home.parent / "target")


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
    shape = rw.read(root, config(), cargo, rustc, home, home.parent / "target")
    assert (str(above), hashlib.sha256(above.read_bytes()).hexdigest()) in shape.configs_above
    above.write_text("[target.'cfg(unix)']\nlinker = \"/bin/false\"\n")
    with pytest.raises(ValueError, match=r"`target\.cfg\(unix\)\.linker` in .*config\.toml"):
        rw.read(root, config(), cargo, rustc, home, home.parent / "target")


def regenerate(root, home, cargo, rustc):
    subprocess.run(
        [cargo, "generate-lockfile", "--offline", *rw.vendor_arguments(root, "vendor")],
        cwd=root,
        env=rw.cargo_environment(rustc, home),
        check=True,
        capture_output=True,
    )


def test_a_feature_gated_path_dependency_outside_the_root_is_refused_when_built(workspace):
    root, home, cargo, rustc = workspace
    far = root.parent / "far"
    (far / "src").mkdir(parents=True)
    (far / "Cargo.toml").write_text(
        '[package]\nname = "far"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (far / "src/lib.rs").write_text("pub fn far() {}\n")
    manifest = root / "app/Cargo.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'smallnum = "1"\n',
            'smallnum = "1"\nfar = { path = "../../far", optional = true }\n\n'
            '[features]\nextra = ["dep:far"]\n',
        )
    )
    regenerate(root, home, cargo, rustc)
    # Without the feature, the build doesn't compile it: nothing outside the root is read.
    shape = rw.read(root, config(), cargo, rustc, home, home.parent / "target")
    assert [package.name for package in shape.local] == ["app", "util"]
    with pytest.raises(ValueError) as caught:
        rw.read(root, config(features=["extra"]), cargo, rustc, home, home.parent / "target")
    assert f"`far` is at {far} (the build compiles it)" in str(caught.value)
    assert classify(caught.value)["code"] == "UNSUPPORTED"


def test_profiles_are_peekys_and_a_per_package_override_of_an_analysed_one_is_refused(workspace):
    root, home, cargo, rustc = workspace
    manifest = root / "Cargo.toml"
    base = manifest.read_text()
    # A profile's own settings are overridden by Peeky's environment.
    manifest.write_text(
        base + '\n[profile.dev]\nopt-level = 3\nlto = true\nstrip = "symbols"\n'
        "[profile.test]\nopt-level = 3\n"
    )
    rw.read(root, config(), cargo, rustc, home, home.parent / "target")
    rw.read(
        root,
        config(run={"package": "util", "lib": True}),
        cargo,
        rustc,
        home,
        home.parent / "target",
    )
    # One package's setting overrides it, so it is refused for an analysed package only.
    manifest.write_text(base + "\n[profile.dev.package.util]\nopt-level = 2\n")
    with pytest.raises(ValueError) as caught:
        rw.read(root, config(), cargo, rustc, home, home.parent / "target")
    assert "`util`'s util has opt-level 2" in str(caught.value)
    assert classify(caught.value)["code"] == "UNSUPPORTED"
    rw.read(root, config(analyse=["app"]), cargo, rustc, home, home.parent / "target")


def test_a_target_table_for_another_host_is_ignored_and_one_for_this_host_refused(workspace):
    root, home, cargo, rustc = workspace
    config_file = root / ".cargo/config.toml"
    config_file.parent.mkdir()
    config_file.write_text("[target.'cfg(windows)']\nlinker = \"link.exe\"\n")
    rw.read(root, config(), cargo, rustc, home, home.parent / "target")
    config_file.write_text("[target.'cfg(not(windows))']\nlinker = \"cc\"\n")
    with pytest.raises(ValueError, match=r"`target\.cfg\(not\(windows\)\)\.linker`"):
        rw.read(root, config(), cargo, rustc, home, home.parent / "target")


def test_an_optional_path_dependency_the_run_doesnt_build_is_read_as_the_build_reads_it(
    workspace,
):
    """Cargo's metadata (`--no-deps`) and unit graph are of the build's own selection: an
    optional path dependency outside the root that no configured feature enables is not
    refused, nor watched. Where its manifest is missing, Peeky fails as the build itself
    does, in Cargo's words."""
    root, home, cargo, rustc = workspace
    far = root.parent / "far"
    (far / "src").mkdir(parents=True)
    (far / "Cargo.toml").write_text(
        '[package]\nname = "far"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (far / "src/lib.rs").write_text("pub fn far() {}\n")
    manifest = root / "app/Cargo.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'smallnum = "1"\n',
            'smallnum = "1"\nfar = { path = "../../far", optional = true }\n\n'
            '[features]\nextra = ["dep:far"]\n',
        )
    )
    regenerate(root, home, cargo, rustc)
    target = home.parent / "target"
    shape = rw.read(root, config(), cargo, rustc, home, target)
    assert [package.name for package in shape.local] == ["app", "util"]
    assert str(far / "Cargo.toml") not in shape.watch["files"]
    (far / "Cargo.toml").rename(far / "Cargo.toml.gone")
    env = rw.build_environment(rustc, home, target)
    argv = [cargo, *shape.cargo_arguments(), "--offline", "--locked"]
    built = subprocess.run(
        [*argv, *rw.vendor_arguments(root, "vendor")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert built.returncode != 0 and "far/Cargo.toml" in built.stderr
    with pytest.raises(BuildCommandError) as caught:
        rw.read(root, config(), cargo, rustc, home, target)
    error = classify(caught.value)
    assert error["code"] == "BUILD_FAILED" and "far/Cargo.toml" in error["message"]


def test_the_projects_cfg_rustflags_are_kept_and_others_refused(workspace):
    root, home, cargo, rustc = workspace
    library = root / "util/src/lib.rs"
    library.write_text(
        library.read_text() + "#[cfg(special)]\npub fn special() -> u32 {\n    7\n}\n"
    )
    main = root / "app/src/main.rs"
    main.write_text(main.read_text().replace("util::double(", "util::special() + util::double("))
    settings = root / ".cargo/config.toml"
    settings.parent.mkdir()
    settings.write_text(
        '[build]\nrustflags = ["--cfg", "special", "--check-cfg", "cfg(special)"]\n'
        'target-dir = "elsewhere"\n'
    )
    target = home.parent / "target"
    shape = rw.read(root, config(), cargo, rustc, home, target)
    assert shape.rustflags == ("--cfg", "special", "--check-cfg", "cfg(special)")
    assert "special" in shape.cfgs
    # Built with Peeky's environment, the project's cfg holds, and Peeky's target directory
    # is the build's.
    env = rw.build_environment(rustc, home, target, shape.rustflags)
    argv = [cargo, *shape.cargo_arguments(), "--offline", "--locked"]
    argv += rw.vendor_arguments(root, "vendor")
    built = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    assert subprocess.run([target / "debug/app"], capture_output=True, text=True).stdout == "9\n"
    assert not (root / "elsewhere").exists()
    settings.write_text('[build]\nrustflags = ["-C", "target-cpu=native"]\n')
    with pytest.raises(ValueError) as caught:
        rw.read(root, config(), cargo, rustc, home, target)
    assert f"Peeky doesn't build with `build.rustflags` in {settings}" in str(caught.value)
    assert "keeps only a project's `--cfg` and `--check-cfg` ones" in str(caught.value)
    assert classify(caught.value)["code"] == "UNSUPPORTED"


def test_cfg_literals_and_a_split_debuginfo_override_are_read_as_cargo_reads_them(workspace):
    root, home, cargo, rustc = workspace
    settings = root / ".cargo/config.toml"
    settings.parent.mkdir()
    settings.write_text("[target.'cfg(true)']\nlinker = \"cc\"\n")
    with pytest.raises(ValueError, match=r"`target\.cfg\(true\)\.linker`"):
        rw.read(root, config(), cargo, rustc, home, home.parent / "target")
    settings.write_text("[target.'cfg(false)']\nlinker = \"cc\"\n")
    rw.read(root, config(), cargo, rustc, home, home.parent / "target")
    manifest = root / "Cargo.toml"
    manifest.write_text(
        manifest.read_text() + '\n[profile.dev.package.util]\nsplit-debuginfo = "unpacked"\n'
    )
    if rw.SPLIT_DEBUGINFO == "unpacked":
        pytest.skip("this platform's builds are unpacked")
    with pytest.raises(ValueError, match="`util`'s util has split-debuginfo unpacked"):
        rw.read(root, config(), cargo, rustc, home, home.parent / "target")
