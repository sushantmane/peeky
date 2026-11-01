"""Peeky's build of a Rust workspace (rust_build): its rustc wrapper, the checks of rustc's
recorded commands, and its refusals' codes."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from peeky import rust_build as rb
from peeky.errors import ERROR_CODES, classify

FAKE_RUSTC = """import json, os, sys
args = sys.argv[1:]
jobserver = int(os.environ.get("FAKE_JOBSERVER_FD", "-1"))
try:
    os.fstat(jobserver)
    open_ = True
except OSError:
    open_ = False
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "bootstrap": os.environ.get("RUSTC_BOOTSTRAP"),
                          "makeflags": os.environ.get("CARGO_MAKEFLAGS"),
                          "jobserver": open_, "env": dict(os.environ)}) + "\\n")
if "-o" in args:
    with open(args[args.index("-o") + 1], "w") as out:
        out.write("#![feature(prelude_import)]\\npub fn expanded() {}\\n")
if "--emit=metadata" in args and os.environ.get("FAKE_STABLE_REFUSES"):
    sys.stderr.write('{"artifact":"/x/libutil.rmeta","emit":"metadata"}\\n')
    sys.stderr.write("error[E0554]: `#![feature]` may not be used on the stable release channel\\n")
    sys.exit(1)
"""


@pytest.fixture
def wrapped(tmp_path):
    """Run Peeky's wrapper as Cargo does: `wrapper rustc args…`, rustc being a fake that
    logs each run."""
    wrapper, _ = rb._write_wrapper(tmp_path)
    rustc = tmp_path / "rustc"
    rustc.write_text(f"#!{sys.executable}\n{FAKE_RUSTC}")
    rustc.chmod(0o755)
    source = tmp_path / "ws/util/src/lib.rs"
    source.parent.mkdir(parents=True)
    source.write_text("pub fn double(x: u32) -> u32 { x * 2 }\n")
    units = tmp_path / "units"
    units.mkdir()
    log = tmp_path / "log"

    def run(*args, crate="util", cwd=tmp_path / "ws", refuse=False, failed=False):
        # Cargo's jobserver: a pipe whose descriptors every rustc it runs inherits.
        read_end, write_end = os.pipe()
        env = {
            "PATH": os.environ["PATH"],
            "FAKE_LOG": str(log),
            "FAKE_JOBSERVER_FD": str(read_end),
            "PEEKY_ANALYSED": json.dumps({"util": [str(source.resolve())]}),
            "PEEKY_UNITS": str(units),
            "CARGO_MAKEFLAGS": f"-j --jobserver-fds={read_end},{write_end}",
            "CARGO_PKG_NAME": "util",
            **({"FAKE_STABLE_REFUSES": "1"} if refuse else {}),
        }
        argv = [str(wrapper), str(rustc), "--crate-name", crate, *args]
        try:
            done = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                pass_fds=(read_end, write_end),
            )
        finally:
            os.close(read_end)
            os.close(write_end)
        assert (done.returncode != 0) is failed, done.stderr
        runs = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        log.unlink(missing_ok=True)
        return (runs, done) if failed else runs

    return run, units


def test_the_wrapper_records_expands_and_dumps_an_analysed_compile(wrapped):
    run, units = wrapped
    runs = run(
        "--edition=2021",
        "util/src/lib.rs",
        "--emit=dep-info,metadata,link",
        "-C",
        "extra-filename=-0123456789abcdef",
        "--out-dir",
        "/target/debug/deps",
    )
    unit = units / "util-0123456789abcdef"
    command = json.loads((unit / "command.json").read_text())
    assert (
        command["source"].endswith("util/src/lib.rs") and command["env"]["CARGO_PKG_NAME"] == "util"
    )
    assert json.loads((unit / "expand.json").read_text()) == {"exit_code": 0}
    assert (unit / "expanded.rs").read_text().startswith("#![feature(prelude_import)]")
    gate, expand, compile_ = runs
    # First, Cargo's own command in Cargo's environment (no RUSTC_BOOTSTRAP), writing only
    # metadata, and that into the unit's directory.
    assert gate["args"] == [
        "--crate-name",
        "util",
        "--edition=2021",
        "util/src/lib.rs",
        "-C",
        "extra-filename=-0123456789abcdef",
        "--emit=metadata",
        "--out-dir",
        str(unit / "stable"),
    ]
    assert gate["bootstrap"] is None and not (unit / "stable").exists()
    # Expansion: a second rustc that writes only the expanded text.
    assert expand["args"][-3:] == ["-Zunpretty=expanded", "-o", str(unit / "expanded.rs")]
    assert not any(arg.startswith("--emit") or arg == "--out-dir" for arg in expand["args"])
    # Each in Cargo's environment, with its jobserver; the expansion and the compile in the
    # same one, RUSTC_BOOTSTRAP too.
    assert all(each["jobserver"] and each["makeflags"] for each in runs)
    assert expand["env"] == compile_["env"] and expand["bootstrap"] == "util"
    assert {**gate["env"], "RUSTC_BOOTSTRAP": "util"} == compile_["env"]
    # The compile: MIR dumped, LLVM IR emitted. The program is built from the MIR the
    # proofs read, so its compile has those nightly flags, allowed in its own environment
    # for this crate alone.
    assert "--emit=dep-info,metadata,link,llvm-ir" in compile_["args"]
    assert "-Zmir-opt-level=0" in compile_["args"]
    assert f"-Zdump-mir-dir={unit / 'mir'}" in compile_["args"]
    assert compile_["bootstrap"] == "util"


def test_what_stable_rustc_refuses_fails_the_compile_in_rustcs_words(wrapped):
    run, units = wrapped
    (runs, done) = run(
        "--edition=2021",
        "util/src/lib.rs",
        "--emit=dep-info,metadata,link",
        "--out-dir",
        "/target/debug/deps",
        refuse=True,
        failed=True,
    )
    # Only Cargo's own command ran, without RUSTC_BOOTSTRAP; its error is Cargo's to show,
    # and its notice of the metadata it would have written isn't.
    assert [each["bootstrap"] for each in runs] == [None]
    assert done.returncode == 1
    assert "error[E0554]: `#![feature]` may not be used on the stable release channel" in (
        done.stderr
    )
    assert '"artifact"' not in done.stderr


@pytest.mark.parametrize(
    ("crate", "source"),
    [
        # A build script's probe of rustc (autocfg), with the analysed package's name set.
        ("autocfg_0123_0", "-"),
        # The crate's name, from another file.
        ("util", "util/src/other.rs"),
        ("build_script_build", "util/build.rs"),
    ],
)
def test_the_wrapper_passes_any_other_compile_through_unchanged(wrapped, crate, source):
    run, units = wrapped
    args = ("--edition=2021", source, "--emit=link", "--crate-type", "lib")
    runs = run(*args, crate=crate)
    assert [(each["args"], each["bootstrap"]) for each in runs] == [
        (["--crate-name", crate, *args], None)
    ]
    assert list(units.iterdir()) == []


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (["-C", "opt-level=3"], "`-C opt-level=3`"),
        (["-Copt-level=1"], "`-C opt-level=1`"),
        (["--codegen", "opt-level=2"], "`-C opt-level=2`"),
        (["-O"], "`-C opt-level=2`"),
        (["-C", "debuginfo=2", "-C", "debuginfo=1"], "`-C debuginfo=1`"),
        (["-C", "strip=symbols"], "`-C strip=symbols`"),
        (["-C", "lto=fat"], "`-C lto=fat`"),
    ],
)
def test_an_analysed_compile_is_checked_from_rustcs_recorded_command(args, said):
    with pytest.raises(ValueError) as caught:
        rb.check_codegen("util", "util", ["rustc", "-C", "debuginfo=2", *args])
    assert said in str(caught.value)
    assert str(caught.value).startswith("Peeky analyses only code built at opt-level 0")
    assert classify(caught.value)["code"] == "UNSUPPORTED"
    assert rb.check_codegen("util", "util", ["rustc", "-C", "debuginfo=2", "-C", "lto=off"]) == "0"


def test_a_compiles_files_lead_to_rustcs_dependency_file(tmp_path):
    deps = tmp_path / "debug/deps"
    deps.mkdir(parents=True)
    for name in ("libutil-0123.rlib", "util-0123.d", "app-4567", "app-4567.d"):
        (deps / name).write_text("")
    (tmp_path / "debug/app").write_text("")
    (tmp_path / "debug/app.d").write_text("")
    assert rb._dependency_file([str(deps / "libutil-0123.rlib")]) == (deps / "util-0123.d", "rustc")
    assert rb._dependency_file([str(deps / "app-4567")]) == (deps / "app-4567.d", "rustc")
    # A binary Cargo copies up names only its copy: Cargo's own file, in make's form.
    assert rb._dependency_file([str(tmp_path / "debug/app")]) == (
        tmp_path / "debug/app.d",
        "make",
    )
    assert rb._dependency_file([str(tmp_path / "debug/none")]) == (None, None)
    # A build script: Cargo names its copy, beside rustc's output and dependency file.
    script = tmp_path / "debug/build/util-89ab"
    script.mkdir(parents=True)
    for name in ("build-script-build", "build_script_build-89ab", "build_script_build-89ab.d"):
        (script / name).write_text("")
    assert rb._dependency_file([str(script / "build-script-build")]) == (
        script / "build_script_build-89ab.d",
        "rustc",
    )
    assert rb._stem("libutil-0123.rlib") == "util-0123"
    assert rb._stem("app-4567.dSYM") == "app-4567"


def test_a_watched_directory_cargo_lists_is_recorded_by_its_entries_not_hashed(tmp_path):
    # Cargo's own dependency file lists a build script's `rerun-if-changed` paths: a
    # directory too, which isn't a file to hash (a build watching one failed, "Expected a
    # regular file").
    root, target = tmp_path / "ws", tmp_path / "target"
    (root / "util/proto").mkdir(parents=True)
    (root / "util/src").mkdir()
    (root / "util/src/lib.rs").write_text("")
    target.mkdir()
    depfile = tmp_path / "app.d"
    depfile.write_text(f"{target}/debug/app: {root}/util/src/lib.rs {root}/util/proto\n")
    read = rb._read_dependencies(depfile, root, root, target, "app", "make")
    assert read["sources"] == ["util/src/lib.rs"]
    assert read["directories"] == [root / "util/proto"]
    # Recorded entry by entry where a local build script watches it, or a vendored copy's
    # checksums cover it; else refused, naming it.
    shape = SimpleNamespace(watch={"vendor_directory": str(root / "vendor")})
    manifest = {"root": str(root), "build_inputs": {"dirs": {str(root / "util/proto"): {}}}}
    rb._watched_directories([root / "util/proto"], manifest, shape, "vendor")
    rb._watched_directories([root / "vendor/x/data"], manifest, shape, "vendor")
    with pytest.raises(ValueError) as refused:
        rb._watched_directories([root / "util/other"], manifest, shape, None)
    assert str(refused.value) == (
        "Cargo says the build read the directory util/other, which no local build script's "
        "`rerun-if-changed` names: Peeky can't tell what of it was read. Name it in the build "
        "script that reads it"
    )


def test_the_build_refusals_have_their_codes():
    assert "BUILD_REQUIRED" in ERROR_CODES
    required = classify(
        rb.BuildRequired(
            "No current Peeky build of this workspace: none has been made for this "
            "configuration. Run `peeky build . --config rust.json` first, or add --build"
        )
    )
    assert required["code"] == "BUILD_REQUIRED" and required["retryable"] is True
    assert "--build" in required["hint"]
    stale = classify(
        RuntimeError(
            "Provenance: util/src/lib.rs changed since `peeky build` ran, so its build may not be "
            "of these files. Run `peeky build . --config rust.json` again, or add --build"
        )
    )
    assert stale["code"] == "PROVENANCE_CHANGED" and "peeky build" in stale["hint"]
    edited = classify(
        rb.BuildRequired(
            "No current Peeky build of this workspace: util/src/lib.rs changed since `peeky build` "
            "ran. Run `peeky build . --config rust.json` again, or add --build"
        )
    )
    assert edited["code"] == "BUILD_REQUIRED" and edited["retryable"] is True
    busy = classify(
        RuntimeError("A `peeky build` of this workspace is running: wait for it to end, then retry")
    )
    assert busy["code"] == "BUSY" and busy["retryable"] is True


def test_the_build_command_is_given_as_typed():
    assert rb.build_command(".", "rust.json") == "peeky build . --config rust.json"
    assert rb.build_command("my ws", "a b.json") == "peeky build 'my ws' --config 'a b.json'"


def test_only_one_build_runs_at_once_and_none_while_probes_read(tmp_path):
    with rb._locked(tmp_path, exclusive=True):
        with pytest.raises(RuntimeError, match="A `peeky build` of this workspace is running"):
            with rb._locked(tmp_path, exclusive=False):
                pass
    with rb._locked(tmp_path, exclusive=False), rb._locked(tmp_path, exclusive=False):
        with pytest.raises(RuntimeError, match="or a probe is reading it"):
            with rb._locked(tmp_path, exclusive=True):
                pass


def test_the_build_directory_is_private_and_one_per_root_and_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))

    class Target:
        def __init__(self, analyse):
            self.analyse = analyse

        def identity(self, root):
            return {"root": str(root), "analyse": self.analyse}

    first = rb.build_directory(Target(["util"]), tmp_path)
    assert first == rb.build_directory(Target(["util"]), tmp_path)
    assert first != rb.build_directory(Target(["app"]), tmp_path)
    assert (
        first.parent == tmp_path / "state/rust-builds" and oct(first.stat().st_mode)[-3:] == "700"
    )
    assert Path(first).parent.stat().st_mode & 0o077 == 0


# What build scripts and procedural macros may read.


def write(root, files):
    for name, text in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_a_package_is_walked_whole_by_lstat_and_links_are_never_followed(tmp_path):
    write(
        tmp_path,
        {
            "build.rs": "fn main() {}\n",
            "src/lib.rs": "",
            ".hidden/x": "x\n",
            "target/manual": "m\n",
            "fixture/Cargo.toml": "[package]\n",
            "fixture/src/lib.rs": "",
            "member/Cargo.toml": "[package]\n",
            "member/src/lib.rs": "",
            "vendor/smallnum/src/lib.rs": "",
            ".git/HEAD": "x",
            ".DS_Store": "junk",
            "src/Thumbs.db": "junk",
            "src/desktop.ini": "junk",
            "shared/data.txt": "d\n",
        },
    )
    (tmp_path / "to-file").symlink_to("shared/data.txt")
    (tmp_path / "to-dir").symlink_to("shared")
    (tmp_path / "dangling").symlink_to("gone")
    (tmp_path / "out").symlink_to("/etc")
    # An empty directory a build script may look for (`Path::exists`).
    (tmp_path / "feature-enabled").mkdir()
    skip = {str(tmp_path / "member"), str(tmp_path / "vendor")}
    found = rb._tree(tmp_path, skip, budget=[100])
    digest = hashlib.sha256
    assert found == {
        # Hidden and `target` directories too, and a crate that isn't a workspace package.
        ".hidden": ["dir"],
        ".hidden/x": digest(b"x\n").hexdigest(),
        "build.rs": digest(b"fn main() {}\n").hexdigest(),
        "dangling": ["link", "gone"],
        "feature-enabled": ["dir"],
        "fixture": ["dir"],
        "fixture/Cargo.toml": digest(b"[package]\n").hexdigest(),
        "fixture/src": ["dir"],
        "fixture/src/lib.rs": digest(b"").hexdigest(),
        # A link by its text: never followed, into a directory or out of the root.
        "out": ["link", "/etc"],
        "shared": ["dir"],
        "shared/data.txt": digest(b"d\n").hexdigest(),
        "src": ["dir"],
        "src/lib.rs": digest(b"").hexdigest(),
        "target": ["dir"],
        "target/manual": digest(b"m\n").hexdigest(),
        "to-dir": ["link", "shared"],
        "to-file": ["link", "shared/data.txt"],
    }
    # Where every entry is what a build may read, a link out of the root is refused.
    with pytest.raises(ValueError) as caught:
        rb._reachable(tmp_path / "out", tmp_path)
    assert str(caught.value) == (
        f"Peeky builds only files inside {tmp_path}: out leads out of it through out, a link to "
        "/etc, and a build script or procedural macro may read every file of its package. "
        "Point the link inside the workspace, or make it a file there"
    )
    assert classify(caught.value)["code"] == "UNSUPPORTED"
    rb._reachable(tmp_path / "to-dir", tmp_path)
    # Past the budget, refused: never filtered. Directories spend it, empty ones too, as
    # each is read: one of more entries than the budget is refused before they are held.
    with pytest.raises(ValueError) as caught:
        rb._tree(tmp_path, skip, budget=[2])
    assert str(caught.value).startswith(
        f"Peeky records at most {rb.MAX_RECORDED} files a build script or procedural macro may "
        f"read, and {tmp_path} holds more"
    )
    many = tmp_path / "many"
    for index in range(50):
        (many / f"empty-{index}").mkdir(parents=True)
    with pytest.raises(ValueError, match="Peeky records at most"):
        rb._tree(many, budget=[40])
    held = []
    real = os.scandir

    class Counting:
        def __init__(self, path):
            self.listing = real(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.listing.close()

        def __iter__(self):
            for entry in self.listing:
                held.append(entry.name)
                yield entry

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rb.os, "scandir", Counting)
        with pytest.raises(ValueError, match="Peeky records at most"):
            rb._tree(many, budget=[10])
    assert len(held) == 11


# A build that started after the test wrote its files: none changed while Cargo ran.
LATER = 2**62


def values(found):
    """{variable: value} of build inputs' variables, all of one package."""
    return {record["name"]: record["value"] for record in found["env"]}


def build_script_run(tmp_path, package, output, *, registry=False):
    """Cargo's message for a build script's run, and the output it kept."""
    out_dir = tmp_path / "target/debug/build" / f"{package}-0123" / "out"
    out_dir.mkdir(parents=True)
    (out_dir.parent / "output").write_text(output)
    package_id = (
        f"registry+https://github.com/rust-lang/crates.io-index#{package}@1.0.0"
        if registry
        else f"path+file://{tmp_path / 'ws' / package}#0.1.0"
    )
    return {"reason": "build-script-executed", "package_id": package_id, "out_dir": str(out_dir)}


def fake_shape(tmp_path, local=("util", "app"), analysed=("util",)):
    from types import SimpleNamespace

    packages = {name: SimpleNamespace(name=name, directory=name) for name in local}
    return SimpleNamespace(
        workspace=tmp_path / "ws",
        local=tuple(packages.values()),
        analysed=tuple(packages[name] for name in analysed),
        watch={"vendor_directory": None},
        host="aarch64-unknown-linux-gnu",
        build_profiles=(
            (f"path+file://{tmp_path / 'ws' / 'util'}#0.1.0", "0", "true", ("default", "my-feat")),
        ),
        cfgs=('target_has_atomic="16"', 'target_has_atomic="8"', "unix", 'target_os="linux"'),
        vendored={("zstd-sys", "1.0.0"): "vendor/zstd-sys"},
        root=tmp_path / "ws",
    )


def test_build_scripts_and_procedural_macros_inputs_are_recorded(tmp_path):
    root = tmp_path / "ws"
    write(
        root,
        {
            "util/build.rs": "",
            "util/src/lib.rs": "",
            "util/schema.txt": "one\n",
            "util/proto/a.proto": "a\n",
            "app/build.rs": "",
            "app/src/main.rs": "",
            "app/assets/logo.txt": "logo\n",
            "derive/src/lib.rs": "",
        },
    )
    messages = [
        build_script_run(
            tmp_path,
            "util",
            "cargo:rerun-if-changed=schema.txt\ncargo::rerun-if-changed=proto\n"
            "cargo:rerun-if-changed=gone.txt\ncargo:rerun-if-env-changed=PROTOC\n"
            "cargo:rustc-cfg=x\n",
        ),
        # No rerun-if-changed: Cargo's default, every file of the package.
        build_script_run(tmp_path, "app", "cargo:rerun-if-env-changed=PATH\n"),
        build_script_run(
            tmp_path,
            "zstd-sys",
            "cargo:rerun-if-env-changed=ZSTD_SYS_USE_PKG_CONFIG\n"
            "cargo:rerun-if-changed=/elsewhere/zstd.h\n",
            registry=True,
        ),
    ]
    env = {"PATH": "/usr/bin:/bin", "PEEKY_UNITS": "/x"}
    before = rb._snapshot(fake_shape(tmp_path), None)
    found = rb._build_inputs(fake_shape(tmp_path), root, messages, env, before, LATER, None)
    assert found["files"] == {
        str(root / "util/gone.txt"): ["missing"],
        str(root / "util/schema.txt"): [
            "file",
            str((root / "util/schema.txt").resolve()),
            hashlib.sha256(b"one\n").hexdigest(),
        ],
    }
    assert found["dirs"] == {
        str(root / "util/proto"): {
            "root": ["dir"],
            "entries": {"a.proto": hashlib.sha256(b"a\n").hexdigest()},
        }
    }
    assert set(found["trees"]) == {str(root / "app")}
    assert sorted(found["trees"][str(root / "app")]) == [
        "assets",
        "assets/logo.txt",
        "build.rs",
        "src",
        "src/main.rs",
    ]
    assert values(found) == {
        "PATH": "/usr/bin:/bin",
        "PROTOC": None,
        "ZSTD_SYS_USE_PKG_CONFIG": None,
    }
    app, util = (f"path+file://{root / name}#0.1.0" for name in ("app", "util"))
    zstd = "registry+https://github.com/rust-lang/crates.io-index#zstd-sys@1.0.0"
    assert [(each["package"], each["name"], each["from"]) for each in found["env"]] == [
        (app, "PATH", "launch"),
        (util, "PROTOC", "launch"),
        (zstd, "ZSTD_SYS_USE_PKG_CONFIG", "launch"),
    ]
    # A local procedural macro: every file of its package and of the analysed ones.
    macro = {
        "reason": "compiler-artifact",
        "manifest_path": str(root / "derive/Cargo.toml"),
        "target": {"name": "derive", "kind": ["proc-macro"]},
    }
    shape = fake_shape(tmp_path, local=("util", "app", "derive"))
    found = rb._build_inputs(shape, root, [macro], env, {}, LATER, None)
    assert set(found["trees"]) == {str(root / "derive"), str(root / "util")}


def test_a_package_compared_whole_is_listed_as_cargo_package_lists_it(tmp_path):
    root = tmp_path / "ws"
    write(
        root,
        {
            "app/build.rs": "",
            "app/src/main.rs": "",
            "app/.tracked": "t\n",
            "app/ignored.log": "i\n",
            "app/.DS_Store": "junk\n",
            "app/Thumbs.db": "junk\n",
            "shared/data.txt": "d\n",
            "util/src/lib.rs": "",
            "derive/src/lib.rs": "",
            "derive/.DS_Store": "junk\n",
        },
    )
    (tmp_path / "outside.txt").write_text("o\n")
    (root / "app/escape.txt").symlink_to(tmp_path / "outside.txt")
    (root / "app/dangling.txt").symlink_to("gone.txt")
    (root / "app/assets").symlink_to("../shared")
    (root / "app/feature-enabled").mkdir()
    # Names Cargo writes as they are: a space begins or ends one.
    (root / "app/ leading.txt").write_text("l\n")
    (root / "app/trailing.txt ").write_text("t\n")
    messages = [build_script_run(tmp_path, "app", "cargo:rerun-if-env-changed=CC\n")]
    asked = []
    listed = [
        " leading.txt",
        "trailing.txt ",
        "",
        ".tracked",
        "Cargo.lock",
        "Cargo.toml.orig",
        ".cargo_vcs_info.json",
        "assets/data.txt",
        "build.rs",
        "dangling.txt",
        "deleted.txt",
        "escape.txt",
        "src/main.rs",
    ]

    def lister(package):
        # `cargo package --list`, with git: tracked and untracked files git doesn't ignore,
        # hidden ones too, through a linked directory, and the files Cargo writes into a
        # package.
        asked.append(package)
        return listed, None

    shape = fake_shape(tmp_path)
    found = rb._build_inputs(shape, root, messages, {"CC": None}, {}, LATER, None, 1, lister)
    assert asked == ["app@0.1.0"]
    record = found["lists"][str(root / "app")]
    assert record["cargo"] is True and record["note"] is None
    digest = hashlib.sha256
    # Each looked at by lstat first. Cargo's own files that aren't there are left out; a
    # listed file that isn't there, a link to nothing or out of the root, is refused by type.
    assert record["files"] == {
        " leading.txt": digest(b"l\n").hexdigest(),
        "trailing.txt ": digest(b"t\n").hexdigest(),
        ".tracked": digest(b"t\n").hexdigest(),
        "assets/data.txt": digest(b"d\n").hexdigest(),
        "build.rs": digest(b"").hexdigest(),
        "dangling.txt": {"refused": "dangling"},
        "deleted.txt": {"refused": "missing"},
        "escape.txt": {"refused": "outside"},
        "src/main.rs": digest(b"").hexdigest(),
    }
    # The walk, for the build's freshness, has what Cargo ignores too, and links by text.
    walked = found["trees"][str(root / "app")]
    assert "ignored.log" in walked and walked["escape.txt"] == [
        "link",
        str(tmp_path / "outside.txt"),
    ]
    manifest, files = {"root": str(root), "build_inputs": found, "watch": {}}, dict(record["files"])
    for name, what in (
        ("dangling.txt", "a link to nothing"),
        ("deleted.txt", "missing, though Cargo lists it"),
        ("escape.txt", "a link that leads out of the root"),
    ):
        found["lists"][str(root / "app")]["files"] = {name: files[name]}
        assert rb._watched_inputs(manifest)[1] == (
            "A remote client can't compare what this build read with its own files: "
            f"app/{name} is {what} on the worker's host, among the files of a package a build "
            "may read whole. Run Peeky on the worker's host, or "
            + rb._UNLISTABLE[files[name]["refused"]][1]
        )
    # A member's Cargo.lock that is there is its own.
    (root / "app/Cargo.lock").write_text("lock\n")
    found = rb._build_inputs(shape, root, messages, {"CC": None}, {}, LATER, None, 1, lister)
    assert "Cargo.lock" in found["lists"][str(root / "app")]["files"]
    (root / "app/Cargo.lock").unlink()
    # Cargo can't list it: every entry Peeky walks, and the record says why. A link out of
    # the root is then refused, as a build may read it.
    with pytest.raises(ValueError, match="app/escape.txt leads out of it through app/escape.txt"):
        rb._build_inputs(
            shape, root, messages, {"CC": None}, {}, LATER, None, 1, lambda p: (None, "it failed")
        )
    (root / "app/escape.txt").unlink()
    found = rb._build_inputs(
        shape, root, messages, {"CC": None}, {}, LATER, None, 1, lambda p: (None, "it failed")
    )
    record = found["lists"][str(root / "app")]
    assert record["cargo"] is False
    assert record["note"] == (
        "it failed: Peeky compares every file of app with a remote client's instead, but "
        ".DS_Store, Thumbs.db and desktop.ini"
    )
    # An empty directory is named; one with entries is compared by them.
    assert sorted(record["files"]) == [
        " leading.txt",
        ".tracked",
        "assets",
        "build.rs",
        "dangling.txt",
        "feature-enabled",
        "ignored.log",
        "src/main.rs",
        "trailing.txt ",
    ]
    assert record["files"]["assets"] == record["files"]["feature-enabled"] == "dir"
    assert record["files"]["dangling.txt"] == {"refused": "dangling"}
    # The build's summary says so, once.
    summary = rb.summary(
        {
            "workspace": str(root),
            "analysed": ["util"],
            "run": {"package": "app", "target": "app", "kinds": ["bin"], "executable": "app"},
            "build_id": "b",
            "seconds": 1.0,
            "dependencies": 0,
            "sources": {},
            "units": [],
            "build_inputs": {"lists": {"a": record, "b": dict(record), "c": {"note": None}}},
        },
        tmp_path,
    )
    assert summary["notes"] == [record["note"]]
    from peeky.cli import _build_text

    assert _build_text(summary).splitlines()[-1] == f"note: {record['note']}"
    # A procedural macro's package, and the analysed ones: every entry Peeky walks.
    macro = {
        "reason": "compiler-artifact",
        "manifest_path": str(root / "derive/Cargo.toml"),
        "target": {"name": "derive", "kind": ["proc-macro"]},
    }
    shape = fake_shape(tmp_path, local=("util", "app", "derive"))
    found = rb._build_inputs(shape, root, [macro], {}, {}, LATER, None, 1, lister)
    assert sorted(found["lists"][str(root / "derive")]["files"]) == ["src/lib.rs"]
    assert asked == ["app@0.1.0", "app@0.1.0"]


def test_many_empty_directories_are_listed_in_one_pass(tmp_path):
    # 32,000 empty directories in a package Cargo can't list: each is named (a build script
    # may look for it), each by one membership test, not a scan of the walk per directory
    # (~5e8 prefix checks, minutes, with no stop checked).
    import time

    root = tmp_path / "ws"
    write(root, {"app/build.rs": "", "app/src/main.rs": "", "app/a/b/c.txt": "c\n"})
    for index in range(32000):
        os.mkdir(root / "app" / f"empty-{index}")
    (root / "app/a/empty").mkdir()
    messages = [build_script_run(tmp_path, "app", "cargo:rerun-if-env-changed=CC\n")]
    started = time.monotonic()
    found = rb._build_inputs(
        fake_shape(tmp_path),
        root,
        messages,
        {"CC": None},
        {},
        LATER,
        None,
        1,
        lambda package: (None, "it failed"),
    )
    assert time.monotonic() - started < 20
    files = found["lists"][str(root / "app")]["files"]
    assert sum(1 for name in files if name.startswith("empty-")) == 32000
    # A directory that holds an entry is compared by it; an empty one by its name.
    assert "a" not in files and "a/b" not in files and "src" not in files
    assert files["a/empty"] == "dir" and "a/b/c.txt" in files


def test_a_stopped_builds_own_work_ends_at_its_next_entry(tmp_path, monkeypatch):
    import threading

    from peeky.agent import Stopped, stopping

    for index in range(200):
        (tmp_path / "pkg" / f"d{index}").mkdir(parents=True)
    (tmp_path / "pkg/file.txt").write_text("f\n")
    stop = threading.Event()
    stop.set()
    # A walk, a hash, a listed file: each checks the stop first.
    with stopping(stop):
        for work in (
            lambda: rb._tree(tmp_path / "pkg", budget=[1000]),
            lambda: rb._hash(tmp_path / "pkg/file.txt"),
            lambda: rb._compared_file(tmp_path / "pkg", "file.txt", tmp_path, {}),
        ):
            with pytest.raises(Stopped) as caught:
                work()
            assert caught.value.started is False
    # Outside a stopped build, nothing is checked.
    assert len(rb._tree(tmp_path / "pkg", budget=[1000])) == 201
    # A build stopped in its own work ends as stopped, its next process never started.
    monkeypatch.setattr(
        rb, "_build", lambda *args, **kwargs: rb._tree(tmp_path / "pkg", budget=[1000])
    )
    with pytest.raises(rb.BuildStopped) as caught:
        rb.build(None, tmp_path, stop=stop)
    assert "its next process never started" in str(caught.value)


# Every directory any code opens to list (the interpreter's audit events: os.scandir,
# os.listdir), whatever reads it, while ``_OPENED`` is a list.
_OPENED = None


def _audit(event, args):
    if _OPENED is not None and event in ("os.scandir", "os.listdir"):
        _OPENED.append(str(args[0]))


sys.addaudithook(_audit)


def opened(work):
    """The directories ``work()`` opens to list, and what it raised (or None)."""
    global _OPENED
    _OPENED, raised = [], None
    try:
        work()
    except Exception as exc:  # noqa: BLE001 - the caller checks it
        raised = exc
    finally:
        found, _OPENED = _OPENED, None
    return found, raised


def test_a_large_artifact_tree_is_read_lazily_and_stops_promptly(tmp_path, monkeypatch):
    import threading

    from peeky.stopping import Stopped, stopping

    run = tmp_path / "runs/0123"
    for unit in range(20):
        (run / f"units/u{unit}").mkdir(parents=True)
        for index in range(500):
            (run / f"units/u{unit}/f{index}.mir").write_text("")
    stop = threading.Event()
    stop.set()
    # A set stop ends it at the first entry read, never once the whole tree is listed.
    with stopping(stop):
        listed, raised = opened(lambda: rb._artifacts(tmp_path, run, None))
    assert isinstance(raised, Stopped) and len(listed) <= 1, (raised, len(listed))
    # Past the bound, refused as read: the 22 directories aren't all listed first.
    monkeypatch.setattr(rb, "MAX_RECORDED", 100)
    listed, raised = opened(lambda: rb._artifacts(tmp_path, run, None))
    assert isinstance(raised, ValueError) and len(listed) <= 3, (raised, len(listed))
    assert str(raised).startswith("The build wrote more than")
    # Unstopped and within the bound, every file, sorted.
    monkeypatch.setattr(rb, "MAX_RECORDED", 20000)
    found = rb._artifacts(tmp_path, run, None)
    assert len(found) == 10000 and list(found) == sorted(found)
    # The snapshot before Cargo runs, and the MIR left behind, too.
    shape = SimpleNamespace(workspace=run, local=(), root=run)
    with stopping(stop):
        listed, raised = opened(lambda: rb._snapshot(shape, None))
        assert isinstance(raised, Stopped) and listed == [], (raised, listed)
        listed, raised = opened(lambda: list(rb._walked(run / "units", [10**6], "too many")))
        assert isinstance(raised, Stopped) and len(listed) <= 1, (raised, len(listed))


def a_build_stopped_in_cargo(tmp_path, monkeypatch, files):
    """(``build``'s stop, the build's directory, the time ``build`` took): a build whose
    Cargo leaves ``files`` files in the run's units and is then stopped, every step before
    Cargo stood in for."""
    import threading
    import time

    from peeky import build as peeky_build
    from peeky.stopping import Stopped

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "ws"
    root.mkdir()
    target = SimpleNamespace(
        identity=lambda root: {"test": "stopped"},
        rustc="rustc",
        cargo="cargo",
        rustflags=lambda: (),
        config=SimpleNamespace(vendor=None, analyse=["util"]),
    )
    shape = SimpleNamespace(workspace=root, analysed=(), vendored={}, rustflags=())
    monkeypatch.setattr(peeky_build, "require_private", lambda path: None)
    monkeypatch.setattr(rb, "_tools", lambda target, env: ([], {}))
    monkeypatch.setattr(rb.rust_workspace, "read", lambda *args, **kwargs: shape)
    monkeypatch.setattr(rb, "_write_wrapper", lambda directory: (directory / "wrapper", ""))
    monkeypatch.setattr(rb, "_environment", lambda *args: {})
    monkeypatch.setattr(rb, "_snapshot", lambda shape, vendor: {})
    stop = threading.Event()

    def cargo(argv, cwd, env, deadline, what):
        units = next((rb.build_directory(target, root) / "runs").iterdir()) / "units"
        for index in range(files):
            (units / f"f{index}.mir").write_text("")
        stop.set()
        raise Stopped(["cargo"], started=True)

    monkeypatch.setattr(rb, "_run", cargo)
    started = time.monotonic()
    with pytest.raises(rb.BuildStopped):
        rb.build(target, root, stop=stop)
    return stop, rb.build_directory(target, root), time.monotonic() - started, target, root


def test_a_stopped_build_leaves_its_run_to_the_trash_and_ends_at_once(tmp_path, monkeypatch):
    stop, directory, _, target, root = a_build_stopped_in_cargo(tmp_path, monkeypatch, 20000)
    # Not removed while the stopped build ends: one rename into the trash.
    assert list((directory / "runs").iterdir()) == []
    (left,) = (directory / "trash").iterdir()
    assert len(list((left / "units").iterdir())) == 20000
    # A later build empties the trash, a bounded part at a time, its stop checked at each.
    with rb_stopping(stop):
        with pytest.raises(Exception) as caught:
            rb._empty_trash(directory, 10**6)
    assert type(caught.value).__name__ == "Stopped"
    assert len(list((left / "units").iterdir())) == 20000
    rb._empty_trash(directory, 1000)
    assert 18000 < len(list((left / "units").iterdir())) < 20000
    while (left / "units").exists():
        rb._empty_trash(directory, 5000)
    assert list((directory / "trash").iterdir()) == []


def entries_under(path):
    """How many entries ``path`` holds, at any depth."""
    return sum(len(folders) + len(files) for _, folders, files in os.walk(path))


@pytest.mark.parametrize(
    ("shape", "budget"),
    [
        # More children than the budget, each empty: unchanged forever, once.
        ({f"run/c{index}/": None for index in range(3)}, 2),
        # Wide: 40 runs of 3 files each.
        ({f"r{run}/f{index}": "" for run in range(40) for index in range(3)}, 7),
        # Deeper than the budget: a chain of 30 directories, a file at its end.
        ({"/".join(f"d{level}" for level in range(30)) + "/end": ""}, 2),
        # Both: wide directories inside a deep one.
        ({f"a/b/c/w{index}/f": "" for index in range(25)}, 1),
    ],
    ids=["children-past-the-budget", "wide", "deep", "deep-and-wide"],
)
def test_the_trash_empties_over_calls_each_making_progress(tmp_path, shape, budget):
    trash = tmp_path / "trash"
    for name, text in shape.items():
        path = trash / name
        if name.endswith("/"):
            path.mkdir(parents=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    calls, before = 0, entries_under(trash)
    while (now := entries_under(trash)) > 0:
        rb._empty_trash(tmp_path, budget)
        calls += 1
        after = entries_under(trash)
        # Each call removes something, and at most its budget.
        assert now - budget <= after < now, (calls, now, after)
        assert calls <= before, "the trash isn't emptying"
    assert trash.is_dir() and list(trash.iterdir()) == []


def test_a_deep_trash_empties_with_few_file_descriptors(tmp_path):
    # 100 directories in a chain, with 32 file descriptors: one directory is open at a
    # time, so each call of 10 removes 10.
    levels = [f"d{index}" for index in range(100)]
    (tmp_path / "trash" / Path(*levels)).mkdir(parents=True)
    script = """
import resource, sys
from pathlib import Path
from peeky import rust_build
directory = Path(sys.argv[1])
_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (32, hard))
levels = [f"d{index}" for index in range(100)]
def depth():
    path, found = directory / "trash", 0
    for level in levels:
        path = path / level
        if not path.is_dir():
            break
        found += 1
    return found
for _ in range(10):
    rust_build._empty_trash(directory, 10)
    print(depth())
"""
    done = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(Path(rb.__file__).parents[1])},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert [int(line) for line in done.stdout.split()] == list(range(90, -1, -10))
    assert list((tmp_path / "trash").iterdir()) == []


def test_a_removal_that_fails_is_passed_over_and_spends_no_budget(tmp_path, monkeypatch):
    trash = tmp_path / "trash"
    for name in ("a/stuck", "a/f1", "a/f2", "b/f3"):
        (trash / name).parent.mkdir(parents=True, exist_ok=True)
        (trash / name).write_text("")
    real = os.unlink
    tried = []

    def unlink(path, *args, **kwargs):
        tried.append(str(path))
        if str(path).endswith("stuck"):
            raise PermissionError(1, "Operation not permitted")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(rb.os, "unlink", unlink)
    # `stuck` fails once, and is passed over for the rest of the call (its directory `a`
    # too, which can't be removed), though the trash is read again after `b` is gone.
    rb._empty_trash(tmp_path, 100)
    assert sorted(path.name for path in trash.rglob("*")) == ["a", "stuck"]
    assert sum(path.endswith("stuck") for path in tried) == 1
    # A failure spends none of the budget: five removals (f4, f5, c, f6, d) take five.
    for name in ("c/f4", "c/f5", "d/f6"):
        (trash / name).parent.mkdir(parents=True, exist_ok=True)
        (trash / name).write_text("")
    tried.clear()
    rb._empty_trash(tmp_path, 5)
    assert sorted(path.name for path in trash.rglob("*")) == ["a", "stuck"]


def rb_stopping(stop):
    from peeky.stopping import stopping

    return stopping(stop)


def test_debug_symbols_are_copied_lazily_and_bounded(tmp_path, monkeypatch):
    import threading

    from peeky.stopping import Stopped

    monkeypatch.setattr(rb.sys, "platform", "darwin")
    executable = tmp_path / "target/debug/app"
    symbols = executable.with_name("app.dSYM")
    (symbols / "Contents/Resources/DWARF").mkdir(parents=True)
    executable.write_text("binary\n")
    (symbols / "Contents/Info.plist").write_text("plist\n")
    (symbols / "Contents/Resources/DWARF/app").write_text("dwarf\n")

    def copied(name):
        run = tmp_path / "runs" / name
        run.mkdir(parents=True)
        return run, lambda: rb._copy_executable(executable, run)

    run, copy = copied("whole")
    assert copy() == "runs/whole/bin/app"
    assert (run / "bin/app.dSYM/Contents/Resources/DWARF/app").read_text() == "dwarf\n"
    # A stop ends it at the bundle's first entry, not once it is all copied.
    stop = threading.Event()
    stop.set()
    run, copy = copied("stopped")
    with rb_stopping(stop):
        with pytest.raises(Stopped):
            copy()
    assert list((run / "bin/app.dSYM").iterdir()) == []
    # More entries than a bundle holds: refused as read; a link: refused.
    monkeypatch.setattr(rb, "MAX_SYMBOL_ENTRIES", 3)
    run, copy = copied("bounded")
    with pytest.raises(ValueError, match="holds more than 3 entries"):
        copy()
    monkeypatch.setattr(rb, "MAX_SYMBOL_ENTRIES", 4096)
    (symbols / "Contents/Resources/link").symlink_to("DWARF/app")
    run, copy = copied("linked")
    with pytest.raises(ValueError, match="Peeky's build holds a link"):
        copy()


def test_a_record_of_an_earlier_schema_is_build_required_before_it_is_read(tmp_path, monkeypatch):
    from tests.unit.test_rust_workspace_worker import toolchain

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "ws"
    write(root, {"Cargo.toml": "[workspace]\n", "Cargo.lock": ""})
    target = rb.RustWorkspaceTarget.from_config(
        {"analyse": ["util"], "run": {"bin": "app"}, **toolchain(tmp_path)}, "peeky build"
    )
    directory = rb.build_directory(target, root)
    # Schema 4's record: a package's `lists` hold None for a link out of the root, its
    # `trees` no directories, and there is no `walk_skip`. Read as today's, each would fail.
    assert rb.SCHEMA > 4
    record = {
        "schema": 4,
        "identity": target.identity(root),
        "root": str(root),
        "nonce": "0" * 16,
        "tools": [],
        "sources": {},
        "inputs": {},
        "watch": {"workspace": str(root), "vendored": {}, "files": {}},
        "build_inputs": {
            "files": {},
            "dirs": {},
            "trees": {str(root / "app"): {"build.rs": "x"}},
            "lists": {str(root / "app"): {"files": {"escape.txt": None}, "cargo": True}},
            "env": [],
        },
    }
    (directory / "manifest.json").write_text(json.dumps(record))
    with pytest.raises(rb.BuildRequired) as caught:
        rb.current_build(target, root)
    assert str(caught.value).startswith(
        "No current Peeky build of this workspace: the one recorded is for another "
        "configuration or Peeky."
    )
    assert classify(caught.value)["code"] == "BUILD_REQUIRED"
    # A worker names only the manifest and lockfile, and refuses nothing: the probe is then
    # BUILD_REQUIRED.
    assert rb.recorded_comparison(target, root) is None
    assert target.compared_names(root) == ("Cargo.toml", "Cargo.lock")
    assert target.remote_refusal(root) is None and target.compared_trees(root) == {}


def test_a_listed_file_the_walk_leaves_out_keeps_the_build_current_until_it_changes(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    root = tmp_path / "ws"
    write(root, {"app/build.rs": "", "app/.hidden/h.txt": "h\n", "app/ignored.log": "i\n"})
    messages = [build_script_run(tmp_path, "app", "cargo:rerun-if-env-changed=CC\n")]
    found = rb._build_inputs(
        fake_shape(tmp_path),
        root,
        messages,
        {"CC": None},
        {},
        LATER,
        None,
        1,
        lambda package: ([".hidden/h.txt", "build.rs"], None),
    )
    manifest = {"build_inputs": found, "watch": {}, "rustflags": []}
    target = SimpleNamespace(build_command="peeky build . --config rust.json")
    monkeypatch.setattr(rb, "_environment", lambda *args: {})
    rb._check_build_inputs(target, root, tmp_path, manifest)
    (root / "app/.hidden/h.txt").write_text("changed\n")
    with pytest.raises(rb.BuildRequired) as caught:
        rb._check_build_inputs(target, root, tmp_path, manifest)
    assert str(caught.value).startswith(
        "No current Peeky build of this workspace: app/.hidden/h.txt changed since"
    )


def test_a_build_input_changed_while_cargo_ran_is_refused(tmp_path):
    root = tmp_path / "ws"
    write(root, {"util/build.rs": "", "util/schema.txt": "one\n"})
    messages = [build_script_run(tmp_path, "util", "cargo:rerun-if-changed=schema.txt\n")]
    # Not in the snapshot taken before Cargo ran, and written since it started.
    with pytest.raises(RuntimeError) as caught:
        rb._build_inputs(fake_shape(tmp_path, local=("util",)), root, messages, {}, {}, 0, None)
    assert str(caught.value).startswith("Provenance: util/schema.txt changed while Peeky built")
    assert classify(caught.value)["code"] == "PROVENANCE_CHANGED"


def test_a_changed_build_input_is_build_required_naming_it(tmp_path, monkeypatch):
    from types import SimpleNamespace

    root = tmp_path / "ws"
    write(root, {"util/build.rs": "", "util/schema.txt": "one\n", "app/src/main.rs": ""})
    messages = [
        build_script_run(tmp_path, "util", "cargo:rerun-if-changed=schema.txt\n"),
        build_script_run(tmp_path, "app", "cargo:rerun-if-env-changed=CC\n"),
    ]
    found = rb._build_inputs(fake_shape(tmp_path), root, messages, {"CC": None}, {}, LATER, None)
    manifest = {"build_inputs": found, "watch": {}, "rustflags": []}
    target = SimpleNamespace(build_command="peeky build . --config rust.json")
    fresh = {}
    monkeypatch.setattr(rb, "_environment", lambda *args: fresh)
    rb._check_build_inputs(target, root, tmp_path, manifest)

    def refused():
        with pytest.raises(rb.BuildRequired) as caught:
            rb._check_build_inputs(target, root, tmp_path, manifest)
        assert classify(caught.value)["code"] == "BUILD_REQUIRED"
        return str(caught.value)

    (root / "app/assets").mkdir()
    (root / "app/assets/new.txt").write_text("new\n")
    assert refused() == (
        "No current Peeky build of this workspace: app/assets/new.txt changed since `peeky build` "
        "ran. Run `peeky build . --config rust.json` again, or add --build"
    )
    (root / "app/assets/new.txt").unlink()
    # An empty directory left behind is a change too: a build script may look for it.
    assert refused().startswith("No current Peeky build of this workspace: app/assets changed")
    (root / "app/assets").rmdir()
    (root / "util/schema.txt").write_text("two\n")
    assert refused().startswith(
        "No current Peeky build of this workspace: util/schema.txt, which a build script reads, "
        "changed since `peeky build` ran."
    )
    (root / "util/schema.txt").write_text("one\n")
    fresh["CC"] = "clang"
    assert "the build environment's CC, which a build script reads, changed" in refused()


# A probe's or session's build stays while it runs.


def test_a_leased_build_is_kept_until_its_lease_is_released(tmp_path):
    runs = tmp_path / "runs"
    for index, nonce in enumerate(("a", "b", "c", "d")):
        (runs / nonce).mkdir(parents=True)
        os.utime(runs / nonce, ns=(index * 10**9, index * 10**9))
    with rb._locked(tmp_path, exclusive=False):
        lease = rb.Lease({"nonce": "a"}, rb._lease(tmp_path, "a"))
    # Builds C and D complete: the two newest are kept, and A, held.
    rb._prune(tmp_path, "d")
    assert sorted(path.name for path in runs.iterdir()) == ["a", "c", "d"]
    lease.close()
    rb._prune(tmp_path, "d")
    assert sorted(path.name for path in runs.iterdir()) == ["c", "d"]
    assert not (tmp_path / "leases/a").exists()


def test_an_analysed_compile_that_reads_rustc_bootstrap_is_refused(tmp_path):
    depfile = tmp_path / "util-0123.d"
    depfile.write_text("util-0123.d: src/lib.rs\n\nsrc/lib.rs:\n\n# env-dep:HOME=/x\n")
    rb._check_bootstrap_read("util", "util", depfile)
    depfile.write_text(depfile.read_text() + "# env-dep:RUSTC_BOOTSTRAP=util\n")
    with pytest.raises(ValueError) as caught:
        rb._check_bootstrap_read("util", "util", depfile)
    assert str(caught.value).startswith(
        "Peeky analyses only code built as stable Rust builds it: `util`'s util reads "
        "RUSTC_BOOTSTRAP"
    )
    assert classify(caught.value)["code"] == "UNSUPPORTED"


def test_oversized_build_script_output_is_refused_not_cut(tmp_path):
    # Past 16 MiB, a `rerun-if-changed` line would not be read.
    root = tmp_path / "ws"
    write(root, {"util/build.rs": "", "util/schema.txt": "one\n"})
    output = "x" * (rb.MAX_BUILD_OUTPUT + 1) + "\ncargo::rerun-if-changed=schema.txt\n"
    messages = [build_script_run(tmp_path, "util", output)]
    with pytest.raises(ValueError) as caught:
        rb._build_inputs(fake_shape(tmp_path), root, messages, {}, {}, LATER, None)
    assert str(caught.value).startswith(
        "Peeky reads at most 16 MiB of a build script's output, and `util`'s is more"
    )
    assert classify(caught.value)["code"] == "UNSUPPORTED"


def test_a_watched_directory_is_recorded_entry_by_entry(tmp_path, monkeypatch):
    from types import SimpleNamespace

    root = tmp_path / "ws"
    write(
        root,
        {
            "util/build.rs": "",
            "util/data/.schema": "one\n",
            "util/data/target/x.txt": "x\n",
            "util/data/nested/Cargo.toml": "[package]\n",
        },
    )
    (root / "util/data/link").symlink_to("nested/Cargo.toml")
    messages = [build_script_run(tmp_path, "util", "cargo::rerun-if-changed=data\n")]
    found = rb._build_inputs(fake_shape(tmp_path), root, messages, {}, {}, LATER, None)
    assert found["dirs"][str(root / "util/data")]["entries"] == {
        ".schema": hashlib.sha256(b"one\n").hexdigest(),
        "link": ["link", "nested/Cargo.toml", hashlib.sha256(b"[package]\n").hexdigest()],
        "nested": "dir",
        "nested/Cargo.toml": hashlib.sha256(b"[package]\n").hexdigest(),
        "target": "dir",
        "target/x.txt": hashlib.sha256(b"x\n").hexdigest(),
    }
    manifest = {"build_inputs": found, "watch": {}, "rustflags": []}
    target = SimpleNamespace(build_command="peeky build . --config rust.json")
    monkeypatch.setattr(rb, "_environment", lambda *args: {})
    rb._check_build_inputs(target, root, tmp_path, manifest)
    for change in (
        lambda: (root / "util/data/.schema").write_text("two\n"),
        lambda: (root / "util/data/target/new").mkdir(),
    ):
        change()
        with pytest.raises(rb.BuildRequired, match=r"util/data/.*, which a build script reads"):
            rb._check_build_inputs(target, root, tmp_path, manifest)
    # A link out of the root is refused.
    (root / "util/data/out").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="Peeky builds only files inside") as caught:
        rb._build_inputs(fake_shape(tmp_path), root, messages, {}, {}, LATER, None)
    assert classify(caught.value)["code"] == "UNSUPPORTED"


def test_a_watched_directorys_own_identity_is_recorded(tmp_path, monkeypatch):
    from types import SimpleNamespace

    root = tmp_path / "ws"
    write(root, {"util/build.rs": ""})
    for name in ("util/empty", "util/a", "util/b"):
        (root / name).mkdir(parents=True)
    (root / "util/linked").symlink_to("a")
    output = "cargo::rerun-if-changed=empty\ncargo::rerun-if-changed=linked\n"
    messages = [build_script_run(tmp_path, "util", output)]
    found = rb._build_inputs(fake_shape(tmp_path), root, messages, {}, {}, LATER, None)
    assert found["dirs"] == {
        str(root / "util/empty"): {"root": ["dir"], "entries": {}},
        str(root / "util/linked"): {"root": ["link", "a"], "entries": {}},
    }
    manifest = {"build_inputs": found, "watch": {}, "rustflags": []}
    target = SimpleNamespace(build_command="peeky build . --config rust.json")
    monkeypatch.setattr(rb, "_environment", lambda *args: {})
    rb._check_build_inputs(target, root, tmp_path, manifest)
    # An empty watched directory removed, and a watched link retargeted between empty
    # directories: each is a change.
    (root / "util/empty").rmdir()
    with pytest.raises(rb.BuildRequired, match=r"util/empty, which a build script reads"):
        rb._check_build_inputs(target, root, tmp_path, manifest)
    (root / "util/empty").mkdir()
    rb._check_build_inputs(target, root, tmp_path, manifest)
    (root / "util/linked").unlink()
    (root / "util/linked").symlink_to("b")
    with pytest.raises(rb.BuildRequired, match=r"util/linked, which a build script reads"):
        rb._check_build_inputs(target, root, tmp_path, manifest)


def test_a_watched_link_retargeted_while_cargo_ran_is_refused(tmp_path):
    import time

    root = tmp_path / "ws"
    write(root, {"util/build.rs": ""})
    for name in ("util/a", "util/b"):
        (root / name).mkdir(parents=True)
    (root / "util/linked").symlink_to("a")
    shape = fake_shape(tmp_path, local=("util",))
    before = rb._snapshot(shape, None)
    assert "l:" + str(root / "util/linked") in before
    started = time.time_ns()
    # Retargeted after the build started: the record would be of the final state.
    (root / "util/linked").unlink()
    (root / "util/linked").symlink_to("b")
    messages = [build_script_run(tmp_path, "util", "cargo::rerun-if-changed=linked\n")]
    with pytest.raises(RuntimeError) as caught:
        rb._build_inputs(shape, root, messages, {}, before, started, None)
    assert str(caught.value).startswith("Provenance: util/linked changed while Peeky built")
    assert classify(caught.value)["code"] == "PROVENANCE_CHANGED"


def test_explicit_files_count_against_the_limit(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    write(root, {"util/build.rs": ""})
    monkeypatch.setattr(rb, "MAX_RECORDED", 3)
    lines = "".join(f"cargo::rerun-if-changed=f{n}\n" for n in range(5))
    messages = [build_script_run(tmp_path, "util", lines)]
    before = rb._snapshot(fake_shape(tmp_path), None)
    with pytest.raises(ValueError, match="Peeky records at most 3 files"):
        rb._build_inputs(fake_shape(tmp_path), root, messages, {}, before, LATER, None)


def test_a_build_scripts_variables_are_recorded_as_cargo_gives_them(tmp_path, monkeypatch):
    from types import SimpleNamespace

    root = tmp_path / "ws"
    write(root, {"util/build.rs": ""})
    names = "NUM_JOBS TARGET HOST PROFILE OPT_LEVEL DEBUG RUSTC PATH CC"
    output = "".join(f"cargo::rerun-if-env-changed={name}\n" for name in names.split())
    output += "cargo::rerun-if-changed=build.rs\n"
    messages = [build_script_run(tmp_path, "util", output)]
    env = {"PATH": "/usr/bin:/bin", "RUSTC": "/toolchain/bin/rustc"}
    found = rb._build_inputs(fake_shape(tmp_path), root, messages, env, {}, LATER, None, 6)
    assert values(found) == {
        "CC": None,
        "DEBUG": "true",
        "HOST": "aarch64-unknown-linux-gnu",
        "NUM_JOBS": "6",
        "OPT_LEVEL": "0",
        "PATH": "/usr/bin:/bin",
        "PROFILE": "debug",
        "RUSTC": "/toolchain/bin/rustc",
        "TARGET": "aarch64-unknown-linux-gnu",
    }
    manifest = {"build_inputs": found, "watch": {}, "rustflags": []}
    target = SimpleNamespace(build_command="peeky build . --config rust.json")
    monkeypatch.setattr(rb, "_environment", lambda *args: env)
    monkeypatch.setattr(rb, "jobs", lambda: 6)
    rb._check_build_inputs(target, root, tmp_path, manifest)
    # Another machine's jobs: a later build's NUM_JOBS differs.
    monkeypatch.setattr(rb, "jobs", lambda: 8)
    with pytest.raises(rb.BuildRequired, match="the build environment's NUM_JOBS"):
        rb._check_build_inputs(target, root, tmp_path, manifest)
    # One Cargo sets that Peeky can't compute is refused, naming it.
    for name in (
        "OUT_DIR",
        "DEP_Z_INCLUDE",
        "CARGO_MAKEFLAGS",
        "CARGO_PKG_README",
        "CARGO_CFG_DEBUG_ASSERTIONS",
        "CARGO",
    ):
        messages = [
            build_script_run(tmp_path / name, "util", f"cargo::rerun-if-env-changed={name}\n")
        ]
        with pytest.raises(ValueError) as caught:
            rb._build_inputs(fake_shape(tmp_path), root, messages, env, {}, LATER, None, 6)
        assert str(caught.value).startswith(
            f"Peeky can't tell the value Cargo gives `util`'s build script of {name}"
        )
        assert classify(caught.value)["code"] == "UNSUPPORTED"


def test_a_mention_of_rustc_bootstrap_is_refused_naming_the_crate_and_file(tmp_path):
    write(
        tmp_path,
        {
            "ws/util/src/lib.rs": "pub fn f() {}\n",
            "ws/util/src/env.rs": "// RUSTC_BOOTSTRAP\n",
            "target/debug/build/derive-0123/out/generated.rs": 'std::env::var("RUSTC_BOOTSTRAP")\n',
            "target/debug/build/derive-0123/out/data.txt": "RUSTC_BOOTSTRAP\n",
        },
    )
    root, target_dir = tmp_path / "ws", tmp_path / "target"
    generated = "debug/build/derive-0123/out/generated.rs"
    clean = {"sources": ["util/src/lib.rs"], "generated": ["debug/build/derive-0123/out/data.txt"]}
    rb._check_bootstrap_mentions(clean, root, target_dir, "`util`")
    for read, said in (
        ({"sources": ["util/src/lib.rs", "util/src/env.rs"], "generated": []}, "util/src/env.rs"),
        # A source a build script generated, which the compile `include!`s.
        ({"sources": ["util/src/lib.rs"], "generated": [generated]}, f"{generated} (generated)"),
    ):
        with pytest.raises(ValueError) as caught:
            rb._check_bootstrap_mentions(read, root, target_dir, "the procedural macro `derive`")
        assert str(caught.value).startswith(
            "Peeky analyses only code built as stable Rust builds it: the procedural macro "
            f"`derive`'s {said} mentions RUSTC_BOOTSTRAP"
        )
        assert classify(caught.value)["code"] == "UNSUPPORTED"


def test_cargos_own_variables_for_a_build_script_are_computed(tmp_path):
    root = tmp_path / "ws"
    write(
        root,
        {
            "Cargo.toml": '[workspace]\nmembers = ["util"]\n[workspace.package]\n'
            'license = "MIT"\nauthors = ["A <a@x>", "B"]\n',
            "util/Cargo.toml": '[package]\nname = "util"\nversion = "0.1.0"\nlinks = "z"\n'
            'description = "d"\nlicense.workspace = true\nauthors.workspace = true\n',
            "util/build.rs": "",
        },
    )
    names = (
        "CARGO_MANIFEST_DIR CARGO_MANIFEST_PATH CARGO_MANIFEST_LINKS CARGO_PKG_NAME "
        "CARGO_PKG_VERSION CARGO_PKG_VERSION_MAJOR CARGO_PKG_VERSION_PRE CARGO_PKG_AUTHORS "
        "CARGO_PKG_DESCRIPTION CARGO_PKG_HOMEPAGE CARGO_PKG_LICENSE CARGO_FEATURE_MY_FEAT "
        "CARGO_FEATURE_OTHER CARGO_CFG_FEATURE CARGO_CFG_TARGET_HAS_ATOMIC CARGO_CFG_UNIX "
        "CARGO_CFG_WINDOWS CARGO_ENCODED_RUSTFLAGS CARGO_HOME CARGO_PRIMARY_PACKAGE"
    ).split()
    output = "".join(f"cargo::rerun-if-env-changed={name}\n" for name in names)
    output += "cargo::rerun-if-changed=build.rs\n"
    messages = [build_script_run(tmp_path, "util", output)]
    env = {"CARGO_ENCODED_RUSTFLAGS": "-C\x1fdwarf-version=4", "CARGO_HOME": "/home"}
    found = rb._build_inputs(fake_shape(tmp_path), root, messages, env, {}, LATER, None, 6)
    assert values(found) == {
        "CARGO_CFG_FEATURE": "default,my-feat",
        "CARGO_CFG_TARGET_HAS_ATOMIC": "16,8",
        "CARGO_CFG_UNIX": "",
        "CARGO_CFG_WINDOWS": None,
        "CARGO_ENCODED_RUSTFLAGS": "-C\x1fdwarf-version=4",
        "CARGO_FEATURE_MY_FEAT": "1",
        "CARGO_FEATURE_OTHER": None,
        "CARGO_HOME": "/home",
        "CARGO_MANIFEST_DIR": str(root / "util"),
        "CARGO_MANIFEST_LINKS": "z",
        "CARGO_MANIFEST_PATH": str(root / "util/Cargo.toml"),
        "CARGO_PKG_AUTHORS": "A <a@x>:B",
        "CARGO_PKG_DESCRIPTION": "d",
        "CARGO_PKG_HOMEPAGE": "",
        "CARGO_PKG_LICENSE": "MIT",
        "CARGO_PKG_NAME": "util",
        "CARGO_PKG_VERSION": "0.1.0",
        "CARGO_PKG_VERSION_MAJOR": "0",
        "CARGO_PKG_VERSION_PRE": "",
        "CARGO_PRIMARY_PACKAGE": None,
    }


def test_cargos_own_variables_are_not_rechecked_against_peekys_environment(tmp_path, monkeypatch):
    from types import SimpleNamespace

    # Two build scripts read CARGO_PKG_NAME, each its own: both are kept, and neither is
    # compared at a probe with Peeky's environment, which has none.
    root = tmp_path / "ws"
    write(
        root,
        {
            "util/Cargo.toml": '[package]\nname = "util"\nversion = "0.1.0"\n',
            "util/build.rs": "",
            "app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "app/build.rs": "",
        },
    )
    output = "cargo::rerun-if-env-changed=CARGO_PKG_NAME\ncargo::rerun-if-env-changed=CC\n"
    output += "cargo::rerun-if-changed=build.rs\n"
    messages = [
        build_script_run(tmp_path, "util", output),
        build_script_run(tmp_path, "app", output),
    ]
    found = rb._build_inputs(fake_shape(tmp_path), root, messages, {}, {}, LATER, None, 6)
    assert [(each["name"], each["value"], each["from"]) for each in found["env"]] == [
        ("CARGO_PKG_NAME", "app", "cargo"),
        ("CC", None, "launch"),
        ("CARGO_PKG_NAME", "util", "cargo"),
        ("CC", None, "launch"),
    ]
    manifest = {"build_inputs": found, "watch": {}, "rustflags": []}
    target = SimpleNamespace(build_command="peeky build . --config rust.json")
    fresh = {}
    monkeypatch.setattr(rb, "_environment", lambda *args: fresh)
    rb._check_build_inputs(target, root, tmp_path, manifest)
    fresh["CC"] = "clang"
    with pytest.raises(rb.BuildRequired, match="the build environment's CC"):
        rb._check_build_inputs(target, root, tmp_path, manifest)


def test_an_unknown_cargo_variable_is_refused_and_a_known_inherited_one_kept(tmp_path):
    root = tmp_path / "ws"
    write(root, {"util/build.rs": ""})
    env = {"CARGO_HOME": "/peeky/cargo-home", "CARGO_TARGET_DIR": "/peeky/target"}
    output = (
        "cargo::rerun-if-env-changed=CARGO_HOME\ncargo::rerun-if-env-changed=CARGO_TARGET_DIR\n"
    )
    output += "cargo::rerun-if-changed=build.rs\n"
    messages = [build_script_run(tmp_path, "util", output)]
    found = rb._build_inputs(fake_shape(tmp_path), root, messages, env, {}, LATER, None, 6)
    assert [(each["name"], each["value"], each["from"]) for each in found["env"]] == [
        ("CARGO_HOME", "/peeky/cargo-home", "launch"),
        ("CARGO_TARGET_DIR", "/peeky/target", "launch"),
    ]
    for name in ("CARGO_TRIM_PATHS_SCOPE", "CARGO_SOMETHING_NEW", "__CARGO_FIX_PLZ"):
        messages = [
            build_script_run(tmp_path / name, "util", f"cargo::rerun-if-env-changed={name}\n")
        ]
        with pytest.raises(
            ValueError,
            match=f"Peeky can't tell the value Cargo gives `util`'s build script of {name}",
        ):
            rb._build_inputs(fake_shape(tmp_path), root, messages, env, {}, LATER, None, 6)


@pytest.mark.parametrize("each", ["../schema.txt", ".config/schema.txt"])
def test_an_input_deleted_while_cargo_ran_is_refused(tmp_path, each):
    import time

    # A workspace-level file outside the package, and one in a hidden directory: the
    # snapshot before the build has them, so their absence after it is a change.
    root = tmp_path / "ws"
    write(root, {"util/build.rs": "", "util/src/lib.rs": "", "schema.txt": "1\n"})
    write(root / "util", {".config/schema.txt": "1\n"})
    shape = fake_shape(tmp_path, local=("util",))
    before = rb._snapshot(shape, None)
    started = time.time_ns()
    (root / "util" / each).resolve().unlink()
    messages = [build_script_run(tmp_path, "util", f"cargo::rerun-if-changed={each}\n")]
    with pytest.raises(RuntimeError) as caught:
        rb._build_inputs(shape, root, messages, {}, before, started, None)
    shown = Path(os.path.normpath(Path("util") / each)).as_posix()
    assert str(caught.value).startswith(f"Provenance: {shown} changed while Peeky built")
    assert classify(caught.value)["code"] == "PROVENANCE_CHANGED"


def test_a_missing_input_is_kept_only_where_its_absence_before_was_recorded(tmp_path):
    root = tmp_path / "ws"
    write(root, {"util/build.rs": "", "util/src/lib.rs": ""})
    shape = fake_shape(tmp_path, local=("util",))
    before = rb._snapshot(shape, None)
    # Never there, inside the workspace: recorded as missing.
    messages = [build_script_run(tmp_path, "util", "cargo::rerun-if-changed=gone.txt\n")]
    found = rb._build_inputs(shape, root, messages, {}, before, LATER, None)
    assert found["files"] == {str((root / "util").resolve() / "gone.txt"): ["missing"]}
    # Outside the workspace, Peeky recorded nothing before: it can't tell.
    (Path(messages[0]["out_dir"]).parent / "output").write_text(
        "cargo::rerun-if-changed=../../outside.txt\n"
    )
    with pytest.raises(ValueError) as caught:
        rb._build_inputs(shape, root, messages, {}, before, LATER, None)
    assert str(caught.value).startswith(
        f"Peeky can't tell whether {(tmp_path / 'outside.txt').resolve()} was there while "
        "`util`'s build script ran"
    )
    assert classify(caught.value)["code"] == "UNSUPPORTED"
    # Nor where the snapshot was cut short, which the refusal says.
    (Path(messages[0]["out_dir"]).parent / "output").write_text(
        "cargo::rerun-if-changed=gone.txt\n"
    )
    cut = {**before, ":limit": [200_000, str(root)]}
    with pytest.raises(ValueError) as caught:
        rb._build_inputs(shape, root, messages, {}, cut, LATER, None)
    assert str(caught.value).endswith(
        "missing now, and Peeky's record of the workspace before the build stopped at its "
        "limit, 200,000 entries. Keep fewer than 200,000 entries (files and directories) under "
        "the workspace (move data the build doesn't use out of it), or take the line out of the "
        "build script"
    )
    assert classify(caught.value)["code"] == "UNSUPPORTED"
    assert "fewer entries under the workspace" in classify(caught.value)["hint"]


def test_the_snapshot_proves_no_absence_where_it_couldnt_read(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    write(root, {"util/build.rs": "", "util/src/lib.rs": "", "util/locked/x.txt": ""})
    write(root, {"util/flaky/y.txt": ""})
    shape = fake_shape(tmp_path, local=("util",))
    locked = (root / "util/locked").resolve()
    flaky = (root / "util/flaky/y.txt").resolve()
    real_lstat, real_scandir = os.lstat, os.scandir

    # A directory it can't list (os.walk's error), and an entry it can't read.
    def scandir(path="."):
        if os.path.normpath(path) == str(locked):
            raise PermissionError(13, "Permission denied", str(locked))
        return real_scandir(path)

    def lstat(path, *args, **kwargs):
        if os.path.normpath(path) == str(flaky):
            raise PermissionError(13, "Permission denied", str(flaky))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "lstat", lstat)
    before = rb._snapshot(shape, None)
    monkeypatch.setattr(os, "scandir", real_scandir)
    monkeypatch.setattr(os, "lstat", real_lstat)
    assert before[":limit"] is None
    assert {Path(path) for path, _ in before[":uncovered"]} == {locked, flaky}
    # Nothing under them is proven absent; elsewhere it is.
    why = rb._absent_before(locked / "gone.txt", before)
    assert why[0] == "unread" and why[1].startswith(f"Peeky couldn't read {locked} before")
    assert rb._absent_before(flaky, before)[0] == "unread"
    assert rb._absent_before(root.resolve() / "util/src/gone.rs", before) is None
    assert rb._absent_before(root.resolve() / "util/nothing/deeper/gone.rs", before) is None


def test_the_snapshot_keeps_one_status_per_entry_and_follows_links(tmp_path):
    root = tmp_path / "ws"
    write(root, {"util/src/lib.rs": "", "util/.git/HEAD": "ref\n", "target/debug/x": ""})
    (root / "util/linked.rs").symlink_to("src/lib.rs")
    before = rb._snapshot(fake_shape(tmp_path, local=("util",)), None)
    real = root.resolve()
    keys = {key for key in before if not key.startswith(":")}
    assert {key[:2] for key in keys} == {"l:", "f:", "d:"}
    assert "l:" + str(real / "util/.git/HEAD") in before
    assert "f:" + str(real / "util/linked.rs") in before
    assert not any("target/debug" in key for key in keys)
    # One status per entry, a link's target besides: files count once against the limit.
    assert all(os.path.islink(key[2:]) for key in keys if key.startswith("f:"))


@pytest.mark.parametrize("made", ["gone.txt", "fresh/gone.txt"])
def test_an_input_made_and_removed_while_cargo_ran_is_refused(tmp_path, made):
    import time

    # Missing before and after: the build script may have made it, read it and removed it.
    # The directory whose listing proved it absent changed meanwhile.
    root = tmp_path / "ws"
    write(root, {"util/build.rs": "", "util/src/lib.rs": ""})
    shape = fake_shape(tmp_path, local=("util",))
    before = rb._snapshot(shape, None)
    time.sleep(0.01)
    started = time.time_ns()
    made_path = root / "util" / made
    write(root / "util", {made: "transient\n"})
    made_path.unlink()
    if made_path.parent != root / "util":
        made_path.parent.rmdir()
    messages = [build_script_run(tmp_path, "util", f"cargo::rerun-if-changed={made}\n")]
    with pytest.raises(RuntimeError) as caught:
        rb._build_inputs(shape, root, messages, {}, before, started, None)
    assert str(caught.value).startswith(
        f"Provenance: util changed while Peeky built the workspace, and `util`'s build script "
        f"says it reads util/{made}, missing before and after"
    )
    assert classify(caught.value)["code"] == "PROVENANCE_CHANGED"


def test_a_links_target_that_cant_be_read_is_recorded_so(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    write(root, {"util/src/lib.rs": ""})
    (root / "util/dangling").symlink_to("nowhere")
    (root / "util/guarded").symlink_to("src/lib.rs")
    guarded = root.resolve() / "util/guarded"
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if os.path.normpath(path) == str(guarded):
            raise PermissionError(13, "Permission denied", str(guarded))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    before = rb._snapshot(fake_shape(tmp_path, local=("util",)), None)
    monkeypatch.setattr(os, "stat", real_stat)
    assert before["f:" + str(root.resolve() / "util/dangling")] is None
    assert [Path(path) for path, _ in before[":uncovered"]] == [guarded]
    assert rb._absent_before(guarded, before)[0] == "unread"


def test_the_snapshot_counts_each_entry_once(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    write(root, {"util/src/lib.rs": "", "util/src/a/b.rs": "", "util/c.txt": ""})
    shape = fake_shape(tmp_path, local=("util",))
    entries = len([key for key in rb._snapshot(shape, None) if key.startswith("l:")])
    # ws, ws/util, util/src, src/a: four directories; three files.
    assert entries == 7
    monkeypatch.setattr(rb, "_MAX_SNAPSHOT", entries)
    assert rb._snapshot(shape, None)[":limit"] is None
    monkeypatch.setattr(rb, "_MAX_SNAPSHOT", entries - 1)
    assert rb._snapshot(shape, None)[":limit"][0] == entries - 1


def test_an_input_through_a_link_to_nothing_is_refused(tmp_path):
    import time

    write(tmp_path / "ws", {"util/build.rs": "", "util/src/lib.rs": "", "util/data/a.txt": ""})
    root = (tmp_path / "ws").resolve()
    util = root / "util"
    (util / "link").symlink_to("target.txt")
    (util / "dlink").symlink_to("nowhere")
    (util / "data/stale").symlink_to("../gone.txt")
    shape = fake_shape(tmp_path, local=("util",))
    before = rb._snapshot(shape, None)
    started = time.time_ns()
    # While Cargo ran, what the link names was made, read and removed.
    (util / "target.txt").write_text("read\n")
    (util / "target.txt").unlink()

    seen = []

    def refused(line):
        message = build_script_run(tmp_path, f"util{len(seen)}", line + "\n")
        seen.append(line)
        message["package_id"] = f"path+file://{util}#0.1.0"
        with pytest.raises(ValueError) as caught:
            rb._build_inputs(shape, root, [message], {}, before, started, None)
        assert classify(caught.value)["code"] == "UNSUPPORTED"
        return str(caught.value)

    assert refused("cargo::rerun-if-changed=link").startswith(
        "`util`'s build script watches util/link, a link to util/target.txt that doesn't exist; "
        "Peeky can't tell what it read through it"
    )
    assert refused("cargo::rerun-if-changed=dlink/x.txt").startswith(
        "`util`'s build script watches util/dlink/x.txt, by way of util/dlink, a link to "
        "util/nowhere that doesn't exist"
    )
    assert refused("cargo::rerun-if-changed=data").startswith(
        "`util`'s build script watches util/data, which holds util/data/stale, a link to "
        "util/gone.txt that doesn't exist"
    )
    # A link to nothing before the build, to a file after it.
    (util / "target.txt").write_text("now\n")
    assert refused("cargo::rerun-if-changed=link").startswith(
        "`util`'s build script watches util/link, a link to util/target.txt that didn't exist "
        "before the build"
    )
