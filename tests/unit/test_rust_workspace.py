"""A Rust workspace's configuration and shape (rust_workspace): what Peeky refuses before
Cargo runs, and what it reads from Cargo's metadata."""

import hashlib
import json
from pathlib import Path

import pytest

from peeky import rust_workspace as rw
from peeky.errors import classify

HOST = "aarch64-unknown-linux-gnu"
CHECKSUM = "ab" * 32


def config(**overrides):
    data = {"analyse": ["util"], "run": {"bin": "app"}, **overrides}
    return rw.workspace_config(data)[0]


def write(root, files):
    for name, text in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def vendored_crate(vendor, name, version, checksum=CHECKSUM, directory=None):
    """A copy of a crates.io package as `cargo vendor` writes it."""
    root = Path(vendor) / (directory or name)
    write(root, {"Cargo.toml": f'[package]\nname = "{name}"\nversion = "{version}"\n'})
    write(root, {"src/lib.rs": "pub fn one() -> u32 { 1 }\n"})
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }
    (root / ".cargo-checksum.json").write_text(json.dumps({"files": files, "package": checksum}))
    return root


LOCK = f"""version = 4

[[package]]
name = "app"
version = "0.1.0"
dependencies = ["smallnum", "util"]

[[package]]
name = "smallnum"
version = "1.0.0"
source = "{rw.CRATES_IO}"
checksum = "{CHECKSUM}"

[[package]]
name = "util"
version = "0.1.0"
"""


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    write(
        root,
        {
            "Cargo.toml": '[workspace]\nmembers = ["app", "util"]\nresolver = "2"\n',
            "Cargo.lock": LOCK,
            "app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n',
            "app/src/main.rs": "fn main() {}\n",
            "util/Cargo.toml": '[package]\nname = "util"\nversion = "0.1.0"\nedition = "2021"\n',
            "util/src/lib.rs": "pub fn double(x: u32) -> u32 { x * 2 }\n",
        },
    )
    vendored_crate(root / "vendor", "smallnum", "1.0.0")
    return root


def metadata(root, editions=None, extra=()):
    """`cargo metadata`'s JSON for the fixture workspace (its fields Peeky reads)."""
    editions = editions or {}

    def package(name, kinds, source):
        directory = root / name
        return {
            "name": name,
            "version": "0.1.0",
            "source": None,
            "manifest_path": str(directory / "Cargo.toml"),
            "edition": editions.get(name, "2021"),
            "targets": [
                {
                    "name": name,
                    "kind": kinds,
                    "src_path": str(directory / source),
                    "edition": editions.get(name, "2021"),
                }
            ],
        }

    return {
        "workspace_root": str(root),
        "packages": [
            package("app", ["bin"], "src/main.rs"),
            package("util", ["lib"], "src/lib.rs"),
            {
                "name": "smallnum",
                "version": "1.0.0",
                "source": rw.CRATES_IO,
                "manifest_path": str(root / "vendor/smallnum/Cargo.toml"),
                "edition": "2015",
                "targets": [],
            },
            *extra,
        ],
    }


def shape(root, **overrides):
    chosen = config(vendor="vendor", **overrides)
    return rw.read_workspace(root, chosen, rw.preflight(root, chosen, HOST), metadata(root))


def code(exc):
    return classify(exc.value)["code"]


# The configuration.


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        ({"bin": "rg"}, rw.Run("bin", "rg", None)),
        ({"bin": "coreutils", "package": "coreutils"}, rw.Run("bin", "coreutils", "coreutils")),
        ({"package": "tokio", "test": "sync_broadcast"}, rw.Run("test", "sync_broadcast", "tokio")),
        ({"package": "axum", "lib": True}, rw.Run("lib", None, "axum")),
    ],
)
def test_a_workspace_config_names_what_runs(run, expected):
    chosen, rest = rw.workspace_config(
        {
            "language": "rust",
            "workspace": ".",
            "analyse": ["grep-printer", "ripgrep"],
            "run": run,
            "features": ["full", "serde/std"],
            "vendor": "vendor",
            "cargo": "/c/cargo",
            "arguments": ["-U"],
        }
    )
    assert chosen == rw.WorkspaceConfig(
        ".", ("grep-printer", "ripgrep"), expected, ("full", "serde/std"), "vendor"
    )
    # The tools and the program's arguments stay for the target to check.
    assert rest == {"cargo": "/c/cargo", "arguments": ["-U"]}


@pytest.mark.parametrize(
    ("data", "said"),
    [
        ({"sources": ["src/main.rs"]}, "either a crate's sources and tests, or a workspace"),
        ({"opt_level": "2"}, "either a crate's sources"),
        ({"build": "make"}, "Unknown Rust workspace configuration field: build"),
        ({"analyse": []}, '"analyse" lists the packages'),
        ({"analyse": ["a", "a"]}, '"analyse" lists the packages'),
        ({"analyse": "util"}, '"analyse" lists the packages'),
        ({"analyse": ["../util"]}, "must be a Cargo package name"),
        ({"run": {"lib": True}}, '"run" names what runs'),
        ({"run": {"package": "a", "lib": "yes"}}, '"run" names what runs'),
        ({"run": {"bin": "a", "test": "b"}}, '"run" names what runs'),
        ({"run": {"example": "a"}}, '"run" names what runs'),
        ({"run": {"bin": "a b"}}, "must be a Cargo target name"),
        ({"features": ["full", "--all-features"]}, "Not a Cargo feature name"),
        ({"features": "full"}, '"features" is a list'),
        ({"vendor": "."}, '"vendor" names the directory'),
        ({"vendor": "../vendor"}, '"vendor" must be a relative path'),
        ({"workspace": "/elsewhere"}, '"workspace" must be a relative path'),
        ({"language": "c"}, '"language": "rust"'),
    ],
)
def test_a_workspace_config_is_refused_where_it_is_not_one(data, said):
    with pytest.raises(ValueError, match=None) as caught:
        rw.workspace_config({"analyse": ["util"], "run": {"bin": "app"}, **data})
    assert said in str(caught.value), caught.value
    assert code(caught) == ("UNSUPPORTED" if "Unknown" in said else "INVALID_REQUEST")


def test_a_workspace_config_is_told_from_a_crates():
    assert rw.is_workspace_config({"analyse": ["a"], "run": {"bin": "a"}})
    assert rw.is_workspace_config({"workspace": "."})
    assert not rw.is_workspace_config({"sources": ["src/main.rs"], "tests": []})
    assert not rw.is_workspace_config(None)


# Cargo's configuration in the workspace, and above it.


@pytest.mark.parametrize(
    "text",
    [
        # ripgrep's, coreutils' and tokio's, at the study's commits.
        '[target.x86_64-pc-windows-msvc]\nrustflags = ["-C", "target-feature=+crt-static"]\n',
        '[target.x86_64-unknown-redox]\nlinker = "x86_64-unknown-redox-gcc"\n\n'
        "[target.'cfg(clippy)']\nrustflags = [\"-Wclippy::use_self\"]\n",
        '# [build]\n# rustflags = ["--cfg", "tokio_unstable"]\n',
        f'[build]\nrustflags = ["--cfg", "x"]\njobs = 4\n[target.{HOST}]\nrunner = "qemu"\n',
        '[alias]\nb = "build"\n[net]\noffline = true\n[profile.dev]\nopt-level = 1\n',
    ],
)
def test_cargo_configuration_peeky_accounts_for_is_accepted(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    rw.check_cargo_config(path, HOST)


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ('[env]\nRUSTC_BOOTSTRAP = "1"\n', "`env`"),
        ('[source.crates-io]\nreplace-with = "mirror"\n', "`source`"),
        ('[patch.crates-io]\nfoo = { path = "foo" }\n', "`patch`"),
        ('paths = ["../foo"]\n', "`paths`"),
        ('[build]\ntarget = "wasm32-unknown-unknown"\n', "`build.target`"),
        ('[build]\nrustc-wrapper = "sccache"\n', "`build.rustc-wrapper`"),
        (f'[target.{HOST}]\nlinker = "gcc"\n', f"`target.{HOST}.linker`"),
        ("[target.'cfg(unix)']\nlinker = \"gcc\"\n", "`target.cfg(unix).linker`"),
        (f'[target.{HOST}.z]\nrustc-link-lib = ["z"]\n', f"`target.{HOST}.z`"),
        ("[unstable]\nbuild-std = ['std']\n", "`unstable`"),
    ],
)
def test_cargo_configuration_that_changes_the_build_is_refused(tmp_path, text, named):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(ValueError) as caught:
        rw.check_cargo_config(path, HOST)
    assert f"Peeky doesn't build with {named} in {path}" in str(caught.value)
    assert code(caught) == "UNSUPPORTED"


def ours(configs, root):
    """The configuration files above the workspace under the test's directory (the
    machine's own, above it, are none of the test's)."""
    return [(path, digest) for path, digest in configs if Path(path).is_relative_to(root)]


def test_cargo_configuration_above_the_workspace_is_accepted_under_the_same_rules(workspace):
    chosen = config(vendor="vendor")
    text = '[net]\noffline = true\n[build]\nrustflags = ["-C", "x"]\n'
    write(workspace.parent, {".cargo/config.toml": text})
    found = rw.read_workspace(
        workspace, chosen, rw.preflight(workspace, chosen, HOST), metadata(workspace)
    )
    path = str(workspace.parent / ".cargo/config.toml")
    assert ours(found.configs_above, workspace.parent) == [
        (path, hashlib.sha256(text.encode()).hexdigest())
    ]
    # Not one of the inputs compared with a client's: it is outside the root.
    assert path not in found.inputs
    rw.check_configs_above(found.configs_above)
    # A change since the build read it is refused by name.
    write(workspace.parent, {".cargo/config.toml": text + "[term]\nverbose = true\n"})
    with pytest.raises(ValueError) as caught:
        rw.check_configs_above(found.configs_above)
    assert str(caught.value).startswith(f"Provenance: Cargo's configuration {path} changed")
    assert code(caught) == "PROVENANCE_CHANGED"
    (workspace.parent / ".cargo/config.toml").unlink()
    with pytest.raises(ValueError, match="changed since the build"):
        rw.check_configs_above(found.configs_above)


def test_cargo_configuration_above_the_workspace_off_the_rules_is_refused_by_file_and_key(
    workspace,
):
    chosen = config(vendor="vendor")
    write(workspace.parent, {".cargo/config": '[env]\nRUSTC_BOOTSTRAP = "1"\n'})
    with pytest.raises(ValueError) as caught:
        rw.preflight(workspace, chosen, HOST)
    path = workspace.parent / ".cargo/config"
    assert f"Peeky doesn't build with `env` in {path}:" in str(caught.value)
    assert code(caught) == "UNSUPPORTED"


def test_a_linked_configuration_above_the_workspace_is_read_as_cargo_reads_it(tmp_path, workspace):
    # As a dotfile manager links `~/.cargo/config.toml`.
    text = "[net]\nretry = 3\n"
    write(tmp_path, {"dotfiles/cargo.toml": text})
    (workspace.parent / ".cargo").mkdir()
    (workspace.parent / ".cargo/config.toml").symlink_to(tmp_path / "dotfiles/cargo.toml")
    checked = rw.preflight(workspace, config(vendor="vendor"), HOST)
    assert ours(checked.configs_above, workspace.parent) == [
        (str(workspace.parent / ".cargo/config.toml"), hashlib.sha256(text.encode()).hexdigest())
    ]


def test_a_manifest_above_the_workspace_is_refused(workspace):
    write(workspace.parent, {"Cargo.toml": "[workspace]\n"})
    with pytest.raises(ValueError, match="no manifest above it, and .*Cargo.toml is above"):
        rw.preflight(workspace, config(vendor="vendor"), HOST)


def test_the_workspaces_own_cargo_configuration_is_an_input(workspace):
    write(workspace, {".cargo/config.toml": "[net]\noffline = true\n"})
    assert shape(workspace).inputs == (
        ".cargo/config.toml",
        "Cargo.lock",
        "Cargo.toml",
        "app/Cargo.toml",
        "util/Cargo.toml",
    )
    write(workspace, {".cargo/config": "[env]\nA = '1'\n"})
    with pytest.raises(ValueError, match="`env` in .*/.cargo/config:"):
        shape(workspace)


# The manifest, the lockfile and the vendored copies.


@pytest.mark.parametrize("section", ['[patch.crates-io]\nsmallnum = { path = "x" }', "[replace]"])
def test_a_workspace_with_patch_or_replace_is_refused(workspace, section):
    manifest = workspace / "Cargo.toml"
    manifest.write_text(manifest.read_text() + section + "\n")
    with pytest.raises(
        ValueError, match=r"doesn't build a workspace with \[(patch|replace)\]"
    ) as e:
        shape(workspace)
    assert code(e) == "UNSUPPORTED"


def test_a_git_dependency_is_refused_by_name(workspace):
    lock = workspace / "Cargo.lock"
    lock.write_text(
        LOCK + '\n[[package]]\nname = "gitty"\nversion = "0.2.0"\n'
        'source = "git+https://example.com/gitty?rev=abc#0123456789abcdef"\n'
    )
    with pytest.raises(ValueError) as caught:
        shape(workspace)
    message = str(caught.value)
    assert message.startswith(
        "Peeky doesn't build git dependencies, vendored or not: Cargo.lock's `gitty 0.2.0`"
    )
    assert "https://example.com/gitty?rev=abc," in message and "#0123" not in message
    assert "Cargo.lock records no checksum for a git source" in message
    assert code(caught) == "UNSUPPORTED"


def test_another_registry_and_a_missing_checksum_are_refused(workspace):
    lock = workspace / "Cargo.lock"
    lock.write_text(LOCK.replace(rw.CRATES_IO, "sparse+https://mirror.example/index/"))
    with pytest.raises(ValueError, match="crates.io dependencies only") as caught:
        shape(workspace)
    assert code(caught) == "UNSUPPORTED"
    lock.write_text(LOCK.replace(f'checksum = "{CHECKSUM}"\n', ""))
    with pytest.raises(ValueError, match="has no checksum to check its vendored copy against"):
        shape(workspace)
    lock.write_text(LOCK.replace("version = 4", "version = 2", 1))
    with pytest.raises(ValueError, match="not a lockfile Peeky reads"):
        shape(workspace)
    lock.write_text(LOCK + '\n[[patch.unused]]\nname = "x"\nversion = "1.0.0"\n')
    with pytest.raises(ValueError, match="lists unused patches"):
        shape(workspace)
    lock.unlink()
    with pytest.raises(ValueError, match="cargo generate-lockfile` once"):
        shape(workspace)


def test_registry_dependencies_need_cargo_vendors_copy_and_the_refusal_says_how(workspace):
    command = f"`cd {workspace} && cargo vendor --locked vendor`"
    chosen = config()
    with pytest.raises(ValueError) as caught:
        rw.preflight(workspace, chosen, HOST)
    message = str(caught.value)
    assert message.startswith("Peeky builds registry dependencies only from `cargo vendor`'s")
    assert "(`smallnum 1.0.0` come from crates.io)" in message
    assert command in message and 'add "vendor": "vendor" to the configuration' in message
    assert code(caught) == "INVALID_REQUEST"
    with pytest.raises(ValueError, match="other/vendor doesn't exist") as caught:
        rw.preflight(workspace, config(vendor="other/vendor"), HOST)
    assert "cargo vendor --locked other/vendor`" in str(caught.value)
    # A copy of another version is not the one Cargo.lock names.
    (workspace / "vendor/smallnum/Cargo.toml").write_text(
        '[package]\nname = "smallnum"\nversion = "1.0.1"\n'
    )
    with pytest.raises(ValueError, match="has none of `smallnum 1.0.0`, which Cargo.lock names"):
        rw.preflight(workspace, config(vendor="vendor"), HOST)


def test_a_vendored_copy_must_have_cargo_locks_checksum(workspace):
    vendored_crate(workspace / "vendor", "smallnum", "1.0.0", checksum="cd" * 32)
    with pytest.raises(ValueError) as caught:
        shape(workspace)
    message = str(caught.value)
    assert message.startswith("Provenance: vendored `smallnum 1.0.0` (vendor/smallnum) is not")
    assert "its checksum is cdcdcdcdcdcdcdcd…, Cargo.lock's abababababababab…" in message
    assert code(caught) == "PROVENANCE_CHANGED"
    assert "cargo vendor" in classify(caught.value)["hint"]
    (workspace / "vendor/smallnum/.cargo-checksum.json").unlink()
    with pytest.raises(ValueError, match="has no checksum file"):
        shape(workspace)


def test_vendored_copies_are_found_by_their_manifest_and_others_ignored(workspace):
    (workspace / "vendor/smallnum").rename(workspace / "vendor/smallnum-1.0.0")
    vendored_crate(workspace / "vendor", "unused", "3.0.0", checksum="ef" * 32)
    found = shape(workspace)
    assert found.vendored == {("smallnum", "1.0.0"): "vendor/smallnum-1.0.0/.cargo-checksum.json"}
    vendored_crate(workspace / "vendor", "smallnum", "1.0.0", directory="again")
    with pytest.raises(ValueError, match="holds `smallnum 1.0.0` twice"):
        shape(workspace)


def test_a_workspace_without_registry_dependencies_needs_no_vendor(workspace):
    (workspace / "Cargo.lock").write_text(LOCK.split('[[package]]\nname = "smallnum"')[0])
    chosen = config()
    assert rw.preflight(workspace, chosen, HOST).vendored == {}


# Cargo's metadata.


def test_the_shape_names_the_packages_the_run_and_the_inputs(workspace):
    found = shape(workspace, features=["fast"])
    assert [package.name for package in found.local] == ["app", "util"]
    assert [package.directory for package in found.analysed] == ["util"]
    package, target = found.run
    assert (package.name, target.name, target.source) == ("app", "app", "app/src/main.rs")
    assert found.cargo_arguments() == ("build", "-p", "app", "--bin", "app", "--features", "fast")
    assert found.vendored == {("smallnum", "1.0.0"): "vendor/smallnum/.cargo-checksum.json"}
    assert found.inputs == ("Cargo.lock", "Cargo.toml", "app/Cargo.toml", "util/Cargo.toml")


def test_a_workspace_inside_the_root_has_its_paths_from_the_root(tmp_path, workspace):
    chosen = config(vendor="vendor", workspace="ws")
    found = rw.read_workspace(
        tmp_path, chosen, rw.preflight(tmp_path, chosen, HOST), metadata(workspace)
    )
    assert found.inputs == (
        "ws/Cargo.lock",
        "ws/Cargo.toml",
        "ws/app/Cargo.toml",
        "ws/util/Cargo.toml",
    )
    assert found.vendored == {("smallnum", "1.0.0"): "ws/vendor/smallnum/.cargo-checksum.json"}
    assert found.run[1].source == "ws/app/src/main.rs"


@pytest.mark.parametrize(
    ("run", "arguments"),
    [
        ({"package": "util", "lib": True}, ("test", "--no-run", "-p", "util", "--lib")),
        ({"test": "flow"}, ("test", "--no-run", "-p", "util", "--test", "flow")),
    ],
)
def test_tests_run_as_cargo_builds_them(workspace, run, arguments):
    chosen = config(vendor="vendor", run=run)
    data = metadata(workspace)
    data["packages"][1]["targets"].append(
        {
            "name": "flow",
            "kind": ["test"],
            "src_path": str(workspace / "util/tests/flow.rs"),
            "edition": "2021",
        }
    )
    found = rw.read_workspace(workspace, chosen, rw.preflight(workspace, chosen, HOST), data)
    assert found.cargo_arguments() == arguments


def test_an_analysed_package_outside_editions_2021_and_2024_is_refused(workspace):
    chosen = config(vendor="vendor")
    checked = rw.preflight(workspace, chosen, HOST)
    with pytest.raises(ValueError) as caught:
        rw.read_workspace(workspace, chosen, checked, metadata(workspace, {"util": "2018"}))
    assert str(caught.value).startswith(
        "Peeky analyses only editions 2021 and 2024: package `util` is edition 2018"
    )
    assert code(caught) == "UNSUPPORTED"
    # A package not analysed may be of any edition: its code counts as a dependency's.
    rw.read_workspace(workspace, chosen, checked, metadata(workspace, {"app": "2015"}))
    # A target's own edition counts too.
    data = metadata(workspace)
    data["packages"][1]["targets"][0]["edition"] = "2018"
    with pytest.raises(ValueError, match="package `util` is edition 2018"):
        rw.read_workspace(workspace, chosen, checked, data)


def test_only_the_workspaces_own_code_is_analysed(workspace):
    checked = rw.preflight(workspace, config(vendor="vendor"), HOST)
    chosen = config(vendor="vendor", analyse=["smallnum"])
    with pytest.raises(ValueError, match="`smallnum` is a registry dependency") as caught:
        rw.read_workspace(workspace, chosen, checked, metadata(workspace))
    assert code(caught) == "INVALID_REQUEST"
    chosen = config(vendor="vendor", analyse=["nowhere"])
    with pytest.raises(ValueError, match="`nowhere` is not a package of the workspace"):
        rw.read_workspace(workspace, chosen, checked, metadata(workspace))
    data = metadata(workspace)
    data["packages"][1]["targets"][0]["kind"] = ["proc-macro"]
    with pytest.raises(ValueError, match="`util` is a procedural macro") as caught:
        rw.read_workspace(workspace, config(vendor="vendor"), checked, data)
    assert code(caught) == "UNSUPPORTED"


def test_a_path_package_outside_the_root_is_refused(tmp_path, workspace):
    checked = rw.preflight(workspace, config(vendor="vendor"), HOST)
    outside = {
        "name": "far",
        "version": "0.1.0",
        "source": None,
        "manifest_path": str(tmp_path / "far/Cargo.toml"),
        "edition": "2021",
        "targets": [],
    }
    with pytest.raises(ValueError, match=f"`far` is at {tmp_path / 'far'}") as caught:
        rw.read_workspace(
            workspace, config(vendor="vendor"), checked, metadata(workspace, extra=[outside])
        )
    assert code(caught) == "UNSUPPORTED"


@pytest.mark.parametrize(
    ("run", "said"),
    [
        ({"bin": "nothing"}, "No package of the workspace has a bin target `nothing`"),
        ({"package": "util", "bin": "app"}, "named `util` has a bin target `app`"),
        ({"package": "ghost", "lib": True}, "names package `ghost`, which is not one"),
    ],
)
def test_what_runs_must_be_a_target_of_the_workspace(workspace, run, said):
    chosen = config(vendor="vendor", run=run)
    with pytest.raises(ValueError, match=said):
        rw.read_workspace(
            workspace, chosen, rw.preflight(workspace, chosen, HOST), metadata(workspace)
        )


def test_a_target_two_packages_have_needs_the_package_named(workspace):
    data = metadata(workspace)
    data["packages"][1]["targets"].append(
        {
            "name": "app",
            "kind": ["bin"],
            "src_path": str(workspace / "util/src/main.rs"),
            "edition": "2021",
        }
    )
    chosen = config(vendor="vendor")
    with pytest.raises(ValueError, match=r"Several packages have a bin target `app` \(app, util\)"):
        rw.read_workspace(workspace, chosen, rw.preflight(workspace, chosen, HOST), data)


def test_a_vendor_command_quotes_the_path():
    assert rw.vendor_command("/w/a b") == "cd '/w/a b' && cargo vendor --locked vendor"
    assert rw.vendor_arguments("/w", "vendor") == (
        "--config",
        'source.crates-io.replace-with="peeky-vendored"',
        "--config",
        f"source.peeky-vendored.directory={json.dumps(str(Path('/w/vendor').resolve()))}",
    )
