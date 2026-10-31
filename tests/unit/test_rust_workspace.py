"""A Rust workspace's configuration and shape (rust_workspace): what Peeky refuses before
Cargo runs, and what it reads from Cargo's metadata and unit graph."""

import hashlib
import json
from pathlib import Path

import pytest

from peeky import rust_workspace as rw
from peeky.errors import classify

HOST = "aarch64-unknown-linux-gnu"
CFGS = (
    "debug_assertions",
    'panic="unwind"',
    'target_arch="aarch64"',
    'target_family="unix"',
    'target_os="linux"',
    "unix",
)
CHECKSUM = "ab" * 32


def config(**overrides):
    data = {"analyse": ["util"], "run": {"bin": "app"}, **overrides}
    return rw.workspace_config(data)[0]


def write(root, files):
    for name, text in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def checksums(root, checksum=CHECKSUM, files=None):
    """Write a copy's `.cargo-checksum.json` as `cargo vendor` does: every file's SHA-256."""
    if files is None:
        files = {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in Path(root).rglob("*")
            if path.is_file() and path.name != ".cargo-checksum.json"
        }
    (Path(root) / ".cargo-checksum.json").write_text(
        json.dumps({"files": files, "package": checksum})
    )


def vendored_crate(vendor, name, version, checksum=CHECKSUM, directory=None):
    """A copy of a crates.io package as `cargo vendor` writes it."""
    root = Path(vendor) / (directory or name)
    write(root, {"Cargo.toml": f'[package]\nname = "{name}"\nversion = "{version}"\n'})
    write(root, {"src/lib.rs": "pub fn one() -> u32 { 1 }\n"})
    checksums(root, checksum)
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


def ident(root, name):
    return f"path+file://{root / name}#0.1.0"


SMALLNUM = f"{rw.CRATES_IO}#smallnum@1.0.0"


def metadata(root, editions=None, extra=()):
    """`cargo metadata`'s JSON for the fixture workspace (its fields Peeky reads)."""
    editions = editions or {}

    def package(name, kinds, source):
        directory = root / name
        return {
            "id": ident(root, name),
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

    packages = [
        package("app", ["bin"], "src/main.rs"),
        package("util", ["lib"], "src/lib.rs"),
        {
            "id": SMALLNUM,
            "name": "smallnum",
            "version": "1.0.0",
            "source": rw.CRATES_IO,
            "manifest_path": str(root / "vendor/smallnum/Cargo.toml"),
            "edition": "2015",
            "targets": [],
        },
        *extra,
    ]
    return {
        "workspace_root": str(root),
        "workspace_members": [ident(root, "app"), ident(root, "util")],
        "packages": packages,
    }


PROFILE = {
    "name": "dev",
    "opt_level": "0",
    "lto": "false",
    "debuginfo": 2,
    "panic": "unwind",
    "strip": {"resolved": "None"},
}


def unit(pkg_id, name, kinds, src_path, mode="build", edition="2021", **profile):
    return {
        "pkg_id": pkg_id,
        "target": {"name": name, "kind": kinds, "src_path": str(src_path), "edition": edition},
        "mode": mode,
        "profile": {**PROFILE, **profile},
        "features": [],
        "dependencies": [],
    }


def graph(root, *, util=None, extra=()):
    """Cargo's unit graph for `cargo build -p app --bin app` (``util``: its unit's
    changes; ``extra``: more units)."""
    units = [
        unit(ident(root, "app"), "app", ["bin"], root / "app/src/main.rs"),
        unit(ident(root, "util"), "util", ["lib"], root / "util/src/lib.rs", **(util or {})),
        unit(SMALLNUM, "smallnum", ["lib"], root / "vendor/smallnum/src/lib.rs", edition="2015"),
        *extra,
    ]
    return {"version": 1, "units": units, "roots": [0]}


def checked(root, chosen=None):
    return rw.preflight(root, chosen or config(vendor="vendor"), HOST, CFGS)


def shape(root, data=None, units=None, **overrides):
    chosen = config(vendor="vendor", **overrides)
    return rw.read_workspace(
        root, chosen, checked(root, chosen), data or metadata(root), units or graph(root)
    )


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
        # A table for another host changes nothing here.
        "[target.'cfg(windows)']\nlinker = \"link.exe\"\n",
        '[target.\'cfg(target_os = "macos")\']\nlinker = "clang"\n',
        '[target.\'cfg(all(unix, not(target_arch = "aarch64")))\']\nlinker = "cc"\n',
    ],
)
def test_cargo_configuration_peeky_accounts_for_is_accepted(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    assert rw.check_cargo_config(path, HOST, CFGS) == hashlib.sha256(text.encode()).hexdigest()


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
        (
            '[target.\'cfg(any(windows, target_os = "linux"))\']\nlinker = "gcc"\n',
            '`target.cfg(any(windows, target_os = "linux")).linker`',
        ),
        (f'[target.{HOST}.z]\nrustc-link-lib = ["z"]\n', f"`target.{HOST}.z`"),
        ("[unstable]\nbuild-std = ['std']\n", "`unstable`"),
    ],
)
def test_cargo_configuration_that_changes_the_build_is_refused(tmp_path, text, named):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(ValueError) as caught:
        rw.check_cargo_config(path, HOST, CFGS)
    assert f"Peeky doesn't build with {named} in {path}" in str(caught.value)
    assert code(caught) == "UNSUPPORTED"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("cfg(unix)", True),
        ("cfg(windows)", False),
        ('cfg(target_os = "linux")', True),
        ('cfg(target_os="macos")', False),
        ('cfg(all(unix, target_arch = "aarch64"))', True),
        ("cfg(any(windows, debug_assertions))", True),
        ("cfg(not(unix))", False),
        ("cfg(all())", True),
        ("cfg(any())", False),
        ("cfg(clippy)", False),
    ],
)
def test_a_target_tables_cfg_is_evaluated_for_this_host(key, expected):
    assert rw.cfg_applies(key, CFGS) is expected


@pytest.mark.parametrize("key", ["cfg(unix", "cfg(not(unix, windows))", "cfg(x = y)", "cfg(=)"])
def test_a_cfg_peeky_cant_evaluate_is_refused_saying_so(tmp_path, key):
    path = tmp_path / "config.toml"
    path.write_text(f"[target.'{key}']\nlinker = \"cc\"\n")
    with pytest.raises(ValueError) as caught:
        rw.check_cargo_config(path, HOST, CFGS)
    assert f"Peeky can't tell whether `target.'{key}'` in {path} applies to this host" in str(
        caught.value
    )
    assert code(caught) == "UNSUPPORTED"


def ours(configs, root):
    """The configuration files above the workspace under the test's directory (the
    machine's own, above it, are none of the test's)."""
    return [(path, digest) for path, digest in configs if Path(path).is_relative_to(root)]


def test_cargo_configuration_above_the_workspace_is_accepted_under_the_same_rules(workspace):
    text = '[net]\noffline = true\n[build]\nrustflags = ["-C", "x"]\n'
    write(workspace.parent, {".cargo/config.toml": text})
    found = shape(workspace)
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
    write(workspace.parent, {".cargo/config": '[env]\nRUSTC_BOOTSTRAP = "1"\n'})
    with pytest.raises(ValueError) as caught:
        checked(workspace)
    path = workspace.parent / ".cargo/config"
    assert f"Peeky doesn't build with `env` in {path}:" in str(caught.value)
    assert code(caught) == "UNSUPPORTED"


def test_only_the_file_cargo_reads_counts_where_both_names_exist(workspace):
    # Cargo reads the legacy `config` where `config.toml` exists beside it.
    write(workspace.parent, {".cargo/config": "[net]\nretry = 1\n"})
    write(workspace.parent, {".cargo/config.toml": '[env]\nRUSTC_BOOTSTRAP = "1"\n'})
    above = ours(checked(workspace).configs_above, workspace.parent)
    assert above == [
        (
            str(workspace.parent / ".cargo/config"),
            hashlib.sha256(b"[net]\nretry = 1\n").hexdigest(),
        )
    ]
    write(workspace, {".cargo/config": "[net]\nretry = 1\n"})
    write(workspace, {".cargo/config.toml": '[env]\nA = "1"\n'})
    assert ".cargo/config" in shape(workspace).inputs
    assert ".cargo/config.toml" not in shape(workspace).inputs


def test_a_linked_configuration_above_the_workspace_is_read_as_cargo_reads_it(tmp_path, workspace):
    # As a dotfile manager links `~/.cargo/config.toml`.
    text = "[net]\nretry = 3\n"
    write(tmp_path, {"dotfiles/cargo.toml": text})
    (workspace.parent / ".cargo").mkdir()
    (workspace.parent / ".cargo/config.toml").symlink_to(tmp_path / "dotfiles/cargo.toml")
    assert ours(checked(workspace).configs_above, workspace.parent) == [
        (str(workspace.parent / ".cargo/config.toml"), hashlib.sha256(text.encode()).hexdigest())
    ]


def test_a_manifest_above_the_workspace_is_refused(workspace):
    write(workspace.parent, {"Cargo.toml": "[workspace]\n"})
    with pytest.raises(ValueError, match="no manifest above it, and .*Cargo.toml is above") as e:
        checked(workspace)
    assert code(e) == "UNSUPPORTED"


def test_the_workspaces_own_cargo_configuration_is_an_input(workspace):
    write(workspace, {".cargo/config.toml": "[net]\noffline = true\n"})
    assert shape(workspace).inputs == (
        ".cargo/config.toml",
        "Cargo.lock",
        "Cargo.toml",
        "app/Cargo.toml",
        "util/Cargo.toml",
    )
    (workspace / ".cargo/config.toml").write_text("[env]\nA = '1'\n")
    with pytest.raises(ValueError, match="`env` in .*/.cargo/config.toml:"):
        shape(workspace)


# What Cargo reads is checked again after it ran.


def recheck(found, root):
    rw.recheck(
        found.digests,
        found.workspace,
        HOST,
        CFGS,
        found.configs_above,
        found.vendor_directory,
        "vendor",
    )


@pytest.mark.parametrize(
    "change",
    ["util/Cargo.toml", "Cargo.lock", "vendor/smallnum/src/lib.rs", "far/Cargo.toml"],
)
def test_a_file_cargo_read_that_changes_before_peeky_rechecks_is_refused(workspace, change):
    write(workspace, {"far/Cargo.toml": '[package]\nname = "far"\nversion = "0.1.0"\n'})
    found = shape(workspace)
    recheck(found, workspace)
    path = workspace / change
    path.write_text(path.read_text() + "\n# changed\n")
    with pytest.raises(ValueError) as caught:
        recheck(found, workspace)
    assert str(caught.value).startswith(f"Provenance: {path} changed while Peeky read")
    assert code(caught) == "PROVENANCE_CHANGED"


def test_configuration_that_appears_before_peeky_rechecks_is_refused(workspace):
    found = shape(workspace)
    write(workspace.parent, {".cargo/config.toml": "[net]\nretry = 2\n"})
    with pytest.raises(ValueError, match="configuration above the workspace changed") as caught:
        recheck(found, workspace)
    assert code(caught) == "PROVENANCE_CHANGED"
    (workspace.parent / ".cargo/config.toml").unlink()
    write(workspace, {".cargo/config.toml": "[net]\nretry = 2\n"})
    with pytest.raises(ValueError, match="config.toml appeared while Peeky read"):
        recheck(found, workspace)


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


@pytest.mark.parametrize(
    ("lock", "said", "expected"),
    [
        (
            LOCK.replace(rw.CRATES_IO, "sparse+https://mirror.example/index/"),
            "crates.io dependencies only",
            "UNSUPPORTED",
        ),
        (
            LOCK.replace(f'checksum = "{CHECKSUM}"\n', ""),
            "has no checksum to check its vendored copy against",
            "INVALID_REQUEST",
        ),
        (
            LOCK.replace("version = 4", "version = 2", 1),
            "Peeky reads only Cargo.lock versions 3 and 4, and .* is version 2",
            "UNSUPPORTED",
        ),
        (
            LOCK + '\n[[patch.unused]]\nname = "x"\nversion = "1.0.0"\n',
            "lists unused patches",
            "UNSUPPORTED",
        ),
    ],
)
def test_a_lockfile_peeky_doesnt_build_from_is_refused(workspace, lock, said, expected):
    (workspace / "Cargo.lock").write_text(lock)
    with pytest.raises(ValueError, match=said) as caught:
        shape(workspace)
    assert code(caught) == expected


def test_a_missing_lockfile_is_refused_with_the_command(workspace):
    (workspace / "Cargo.lock").unlink()
    with pytest.raises(ValueError, match="cargo generate-lockfile` once") as caught:
        shape(workspace)
    assert code(caught) == "INVALID_REQUEST"


def test_registry_dependencies_need_cargo_vendors_copy_and_the_refusal_says_how(workspace):
    command = f"`cd {workspace} && cargo vendor --locked vendor`"
    with pytest.raises(ValueError) as caught:
        checked(workspace, config())
    message = str(caught.value)
    assert message.startswith("Peeky builds registry dependencies only from `cargo vendor`'s")
    assert "(`smallnum 1.0.0` come from crates.io)" in message
    assert command in message and 'add "vendor": "vendor" to the configuration' in message
    assert code(caught) == "INVALID_REQUEST"
    with pytest.raises(ValueError, match="other/vendor doesn't exist") as caught:
        checked(workspace, config(vendor="other/vendor"))
    assert "cargo vendor --locked other/vendor`" in str(caught.value)
    assert code(caught) == "INVALID_REQUEST"
    # A copy of another version is not the one Cargo.lock names.
    (workspace / "vendor/smallnum/Cargo.toml").write_text(
        '[package]\nname = "smallnum"\nversion = "1.0.1"\n'
    )
    with pytest.raises(
        ValueError, match="has none of `smallnum 1.0.0`, which Cargo.lock names"
    ) as e:
        checked(workspace)
    assert code(e) == "INVALID_REQUEST"


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
    with pytest.raises(ValueError, match="has no .cargo-checksum.json") as caught:
        shape(workspace)
    assert code(caught) == "INVALID_REQUEST"


def test_an_edited_vendored_file_and_a_checksum_file_that_doesnt_list_every_file_are_refused(
    workspace,
):
    copy = workspace / "vendor/smallnum"
    (copy / "src/lib.rs").write_text("pub fn one() -> u32 { 2 }\n")
    with pytest.raises(ValueError) as caught:
        shape(workspace)
    message = str(caught.value)
    assert message.startswith(
        "Provenance: vendored `smallnum 1.0.0` (vendor/smallnum) is not what `cargo vendor` "
        "wrote: src/lib.rs's SHA-256 is"
    )
    assert code(caught) == "PROVENANCE_CHANGED"
    # Cargo accepts a checksum file that lists nothing, or not every file; Peeky doesn't.
    # (One edited to match an edited file passes: nothing but the .crate, which Peeky
    # doesn't fetch, could tell.)
    (copy / "src/lib.rs").write_text("pub fn one() -> u32 { 1 }\n")
    listed = json.loads((copy / ".cargo-checksum.json").read_text())
    checksums(copy, files={})
    with pytest.raises(ValueError, match="lists no files to check it against") as caught:
        shape(workspace)
    assert code(caught) == "INVALID_REQUEST"
    checksums(copy)
    write(copy, {"src/extra.rs": "pub fn two() {}\n"})
    with pytest.raises(ValueError, match="doesn't list src/extra.rs") as caught:
        shape(workspace)
    assert code(caught) == "INVALID_REQUEST"
    (copy / "src/extra.rs").unlink()
    checksums(copy, files={**listed["files"], "src/gone.rs": "00" * 32})
    with pytest.raises(ValueError, match="lacks src/gone.rs, which its .cargo-checksum.json lists"):
        shape(workspace)


def test_a_link_in_the_vendored_copies_is_refused(tmp_path, workspace):
    copy = workspace / "vendor/smallnum"
    write(tmp_path, {"elsewhere.rs": "pub fn evil() {}\n"})
    (copy / "src/linked.rs").symlink_to(tmp_path / "elsewhere.rs")
    with pytest.raises(ValueError, match=r"holds a link \(src/linked.rs\)") as caught:
        shape(workspace)
    assert code(caught) == "INVALID_REQUEST"
    (copy / "src/linked.rs").unlink()
    (workspace / "vendor/other").symlink_to(copy)
    with pytest.raises(ValueError, match="vendor/other is a link"):
        shape(workspace)


def test_vendored_copies_are_found_by_their_manifest_and_others_ignored(workspace):
    (workspace / "vendor/smallnum").rename(workspace / "vendor/smallnum-1.0.0")
    vendored_crate(workspace / "vendor", "unused", "3.0.0", checksum="ef" * 32)
    found = shape(workspace)
    assert found.vendored == {("smallnum", "1.0.0"): "vendor/smallnum-1.0.0"}
    digests = dict(found.digests)
    assert str(workspace / "vendor/smallnum-1.0.0/src/lib.rs") in digests
    assert str(workspace / "vendor/unused/src/lib.rs") not in digests
    vendored_crate(workspace / "vendor", "smallnum", "1.0.0", directory="again")
    with pytest.raises(ValueError, match="holds `smallnum 1.0.0` twice"):
        shape(workspace)


def test_a_workspace_without_registry_dependencies_needs_no_vendor(workspace):
    (workspace / "Cargo.lock").write_text(LOCK.split('[[package]]\nname = "smallnum"')[0])
    assert checked(workspace, config()).vendored == {}


# Cargo's metadata and unit graph.


def test_the_shape_names_the_packages_the_run_and_the_inputs(workspace):
    found = shape(workspace, features=["fast"])
    assert [package.name for package in found.local] == ["app", "util"]
    assert [package.directory for package in found.analysed] == ["util"]
    package, target = found.run
    assert (package.name, target.name, target.source) == ("app", "app", "app/src/main.rs")
    assert found.cargo_arguments() == ("build", "-p", "app", "--bin", "app", "--features", "fast")
    assert found.vendored == {("smallnum", "1.0.0"): "vendor/smallnum"}
    assert found.inputs == ("Cargo.lock", "Cargo.toml", "app/Cargo.toml", "util/Cargo.toml")


def test_a_workspace_inside_the_root_has_its_paths_from_the_root(tmp_path, workspace):
    chosen = config(vendor="vendor", workspace="ws")
    found = rw.read_workspace(
        tmp_path, chosen, checked(tmp_path, chosen), metadata(workspace), graph(workspace)
    )
    assert found.inputs == (
        "ws/Cargo.lock",
        "ws/Cargo.toml",
        "ws/app/Cargo.toml",
        "ws/util/Cargo.toml",
    )
    assert found.vendored == {("smallnum", "1.0.0"): "ws/vendor/smallnum"}
    assert found.run[1].source == "ws/app/src/main.rs"


@pytest.mark.parametrize(
    ("run", "arguments"),
    [
        ({"package": "util", "lib": True}, ("test", "--no-run", "-p", "util", "--lib")),
        ({"package": "util", "test": "flow"}, ("test", "--no-run", "-p", "util", "--test", "flow")),
    ],
)
def test_tests_run_as_cargo_builds_them(workspace, run, arguments):
    chosen = config(vendor="vendor", run=run)
    data = metadata(workspace)
    flow = {
        "name": "flow",
        "kind": ["test"],
        "src_path": str(workspace / "util/tests/flow.rs"),
        "edition": "2021",
    }
    data["packages"][1]["targets"].append(flow)
    kinds = flow["kind"] if "test" in run else ["lib"]
    name = flow["name"] if "test" in run else "util"
    source = flow["src_path"] if "test" in run else workspace / "util/src/lib.rs"
    units = graph(workspace)
    units["units"].append(unit(ident(workspace, "util"), name, kinds, source, mode="test"))
    units["roots"] = [len(units["units"]) - 1]
    found = rw.read_workspace(workspace, chosen, checked(workspace, chosen), data, units)
    assert found.cargo_arguments() == arguments


def test_an_analysed_package_outside_editions_2021_and_2024_is_refused(workspace):
    with pytest.raises(ValueError) as caught:
        shape(workspace, data=metadata(workspace, {"util": "2018"}))
    assert str(caught.value).startswith(
        "Peeky analyses only editions 2021 and 2024: package `util` is edition 2018"
    )
    assert code(caught) == "UNSUPPORTED"
    # A package not analysed may be of any edition: its code counts as a dependency's.
    shape(workspace, data=metadata(workspace, {"app": "2015"}))
    # A compiled target's own edition counts too.
    with pytest.raises(ValueError, match="package `util` is edition 2018"):
        shape(workspace, units=graph(workspace, util={"edition": "2018"}))


def test_only_the_workspaces_own_code_is_analysed(workspace):
    with pytest.raises(ValueError, match="`smallnum` is a registry dependency") as caught:
        shape(workspace, analyse=["smallnum"])
    assert code(caught) == "INVALID_REQUEST"
    with pytest.raises(ValueError, match="`nowhere` is not a package of the workspace") as caught:
        shape(workspace, analyse=["nowhere"])
    assert code(caught) == "INVALID_REQUEST"
    data = metadata(workspace)
    data["packages"][1]["targets"][0]["kind"] = ["proc-macro"]
    with pytest.raises(ValueError, match="`util` is a procedural macro") as caught:
        shape(workspace, data=data)
    assert code(caught) == "UNSUPPORTED"


def test_an_analysed_package_the_run_doesnt_compile_is_refused(workspace):
    units = graph(workspace)
    del units["units"][1]
    with pytest.raises(ValueError) as caught:
        shape(workspace, units=units, features=["slim"])
    assert str(caught.value).startswith(
        "Peeky analyses only packages the run compiles: `cargo build -p app --bin app "
        "--features slim` doesn't compile `util`"
    )
    assert code(caught) == "INVALID_REQUEST"


def test_an_analysed_path_dependency_that_isnt_a_member_is_refused(workspace):
    data = metadata(workspace)
    data["workspace_members"].remove(ident(workspace, "util"))
    with pytest.raises(ValueError, match="`util` is a path dependency that isn't one") as caught:
        shape(workspace, data=data)
    assert code(caught) == "UNSUPPORTED"


@pytest.mark.parametrize(
    ("changes", "said"),
    [
        ({"opt_level": "3"}, "opt-level 3"),
        ({"debuginfo": 1}, "debug 1"),
        ({"lto": "fat"}, "lto fat"),
        ({"strip": {"resolved": {"Named": "symbols"}}}, "strip {'Named': 'symbols'}"),
        ({"panic": "abort"}, "panic abort"),
    ],
)
def test_an_analysed_package_built_otherwise_than_peeky_needs_is_refused(workspace, changes, said):
    with pytest.raises(ValueError) as caught:
        shape(workspace, units=graph(workspace, util=changes))
    message = str(caught.value)
    assert message.startswith("Peeky analyses only code built at opt-level 0")
    assert f"`util`'s util has {said}, from a [profile] setting for it" in message
    assert "[profile.dev.package.util]" in message
    assert code(caught) == "UNSUPPORTED"
    # A package not analysed may be built otherwise.
    shape(workspace, analyse=["app"], units=graph(workspace, util=changes))


def test_a_compiled_path_package_outside_the_root_is_refused(tmp_path, workspace):
    far = {
        "id": f"path+file://{tmp_path / 'far'}#0.1.0",
        "name": "far",
        "version": "0.1.0",
        "source": None,
        "manifest_path": str(tmp_path / "far/Cargo.toml"),
        "edition": "2021",
        "targets": [],
    }
    data = metadata(workspace, extra=[far])
    # Not compiled by this run (an optional dependency it doesn't enable): not refused.
    shape(workspace, data=data)
    extra = [unit(far["id"], "far", ["lib"], tmp_path / "far/src/lib.rs")]
    with pytest.raises(ValueError) as caught:
        shape(workspace, data=data, units=graph(workspace, extra=extra))
    assert f"`far` is at {tmp_path / 'far'} (the build compiles it)" in str(caught.value)
    assert code(caught) == "UNSUPPORTED"
    # A member outside the root is refused too: Cargo reads its manifest.
    data["workspace_members"].append(far["id"])
    with pytest.raises(ValueError, match=r"\(it is a member\)"):
        shape(workspace, data=data)


def test_what_cargo_builds_must_be_what_run_names(workspace):
    units = graph(workspace)
    units["roots"] = [1]
    with pytest.raises(ValueError, match="Cargo would build `util`'s util for this run") as caught:
        shape(workspace, units=units)
    assert code(caught) == "INVALID_REQUEST"


@pytest.mark.parametrize(
    ("run", "said"),
    [
        ({"bin": "nothing"}, "No package of the workspace has a bin target `nothing`"),
        ({"package": "util", "bin": "app"}, "named `util` has a bin target `app`"),
        ({"package": "ghost", "lib": True}, "names package `ghost`, which is not one"),
        ({"package": "app", "lib": True}, "named `app` has a library"),
    ],
)
def test_what_runs_must_be_a_target_of_a_member(workspace, run, said):
    with pytest.raises(ValueError, match=said) as caught:
        rw._run_package(config(run=run).run, metadata(workspace))
    assert code(caught) == "INVALID_REQUEST"


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
    with pytest.raises(ValueError, match=r"Several packages have a bin target `app` \(app, util\)"):
        rw._run_package(config().run, data)
    assert rw._run_package(config(run={"package": "util", "bin": "app"}).run, data) == "util"


# Peeky's build environment.


def test_the_build_environment_sets_the_profile_settings_peeky_relies_on(tmp_path):
    env = rw.build_environment("/t/bin/rustc", tmp_path, ("-C", "linker=/usr/bin/cc"))
    assert env["CARGO_ENCODED_RUSTFLAGS"] == "-C\x1flinker=/usr/bin/cc"
    for profile in ("DEV", "TEST"):
        assert env[f"CARGO_PROFILE_{profile}_OPT_LEVEL"] == "0"
        assert env[f"CARGO_PROFILE_{profile}_DEBUG"] == "2"
        assert env[f"CARGO_PROFILE_{profile}_LTO"] == "false"
        assert env[f"CARGO_PROFILE_{profile}_STRIP"] == "none"
    assert env["CARGO_INCREMENTAL"] == "0" and env["RUSTC_WRAPPER"] == ""
    # No configuration's rustflags apply, even with none of Peeky's own.
    assert rw.build_environment("/t/bin/rustc", tmp_path)["CARGO_ENCODED_RUSTFLAGS"] == ""


def test_a_vendor_command_quotes_the_path():
    assert rw.vendor_command("/w/a b") == "cd '/w/a b' && cargo vendor --locked vendor"
    assert rw.vendor_arguments("/w", "vendor") == (
        "--config",
        'source.crates-io.replace-with="peeky-vendored"',
        "--config",
        f"source.peeky-vendored.directory={json.dumps(str(Path('/w/vendor').resolve()))}",
    )


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (lambda g: g.update(version=2), "wrote version 2"),
        (lambda g: g.pop("version"), "wrote version None"),
        (lambda g: g.pop("roots"), "one without `units` or `roots`"),
        (lambda g: g.update(roots=[9]), "roots that name no unit"),
        (lambda g: g["units"][1].pop("mode"), "a unit without `mode` as Peeky reads it"),
        (lambda g: g["units"][1]["target"].pop("src_path"), "without `target.src_path`"),
        (lambda g: g["units"][1]["profile"].update(opt_level=0), "without `profile.opt_level`"),
        (lambda g: g["units"][1]["profile"].pop("strip"), "a unit without `profile.strip`"),
        (lambda g: g["units"][1]["target"].update(kind=[1]), "without `target.kind`"),
    ],
)
def test_a_unit_graph_peeky_doesnt_read_is_refused_naming_cargo(workspace, change, said):
    units = graph(workspace)
    change(units)
    chosen = config(vendor="vendor")
    with pytest.raises(ValueError) as caught:
        rw.read_workspace(
            workspace, chosen, checked(workspace), metadata(workspace), units, "cargo 1.99.0 (x)"
        )
    message = str(caught.value)
    assert message.startswith(
        "Peeky reads only the unit graph Cargo 1.96 writes (version 1), and cargo 1.99.0 (x) wrote "
    )
    assert said in message and "so Peeky doesn't read it" in message
    assert code(caught) == "UNSUPPORTED"
