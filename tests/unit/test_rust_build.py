"""Peeky's build of a Rust workspace (rust_build): its rustc wrapper, the checks of rustc's
recorded commands, and its refusals' codes."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from peeky import rust_build as rb
from peeky.errors import ERROR_CODES, classify

FAKE_RUSTC = """import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "bootstrap": os.environ.get("RUSTC_BOOTSTRAP"),
                          "makeflags": os.environ.get("CARGO_MAKEFLAGS")}) + "\\n")
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
        env = {
            "PATH": os.environ["PATH"],
            "FAKE_LOG": str(log),
            "PEEKY_ANALYSED": json.dumps({"util": [str(source.resolve())]}),
            "PEEKY_UNITS": str(units),
            "CARGO_MAKEFLAGS": "-j --jobserver-fds=6,7",
            "CARGO_PKG_NAME": "util",
            **({"FAKE_STABLE_REFUSES": "1"} if refuse else {}),
        }
        argv = [str(wrapper), str(rustc), "--crate-name", crate, *args]
        done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
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
    # Expansion: a second rustc that writes only the expanded text, without Cargo's jobserver.
    assert expand["args"][-3:] == ["-Zunpretty=expanded", "-o", str(unit / "expanded.rs")]
    assert not any(arg.startswith("--emit") or arg == "--out-dir" for arg in expand["args"])
    assert expand["bootstrap"] == "util" and expand["makeflags"] is None
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
    assert runs == [
        {
            "args": ["--crate-name", crate, *args],
            "bootstrap": None,
            "makeflags": runs[0]["makeflags"],
        }
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
    later = classify(
        ValueError(
            "Peeky doesn't read values in a Rust workspace's build yet: it will once a call into "
            "a dependency counts as code it can't read. The build is current; nothing ran"
        )
    )
    assert later["code"] == "UNSUPPORTED" and later["hint"].startswith("Nothing ran.")


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


def test_a_packages_files_are_listed_as_cargo_lists_them(tmp_path):
    write(
        tmp_path,
        {
            "build.rs": "fn main() {}\n",
            "src/lib.rs": "",
            "data/schema.txt": "a\n",
            ".git/HEAD": "x",
            "target/debug/out": "x",
            "nested/Cargo.toml": "[package]\n",
            "nested/src/lib.rs": "",
            "vendor/smallnum/src/lib.rs": "",
        },
    )
    found = rb._tree(tmp_path, {str(tmp_path / "vendor")}, budget=[100])
    assert sorted(found) == ["build.rs", "data/schema.txt", "src/lib.rs"]
    with pytest.raises(ValueError) as caught:
        rb._tree(tmp_path, budget=[2])
    assert str(caught.value).startswith(
        f"Peeky records at most {rb.MAX_RECORDED} files a build script or procedural macro may "
        f"read, and {tmp_path} holds more"
    )
    assert classify(caught.value)["code"] == "UNSUPPORTED"


# A build that started after the test wrote its files: none changed while Cargo ran.
LATER = 2**62


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
    found = rb._build_inputs(fake_shape(tmp_path), root, messages, env, {}, LATER, None)
    assert found["files"] == {
        str(root / "util/gone.txt"): None,
        str(root / "util/schema.txt"): hashlib.sha256(b"one\n").hexdigest(),
    }
    assert set(found["trees"]) == {str(root / "util/proto"), str(root / "app")}
    assert sorted(found["trees"][str(root / "app")]) == [
        "assets/logo.txt",
        "build.rs",
        "src/main.rs",
    ]
    assert found["env"] == {
        "PATH": "/usr/bin:/bin",
        "PROTOC": None,
        "ZSTD_SYS_USE_PKG_CONFIG": None,
    }
    # A local procedural macro: every file of its package and of the analysed ones.
    macro = {
        "reason": "compiler-artifact",
        "manifest_path": str(root / "derive/Cargo.toml"),
        "target": {"name": "derive", "kind": ["proc-macro"]},
    }
    shape = fake_shape(tmp_path, local=("util", "app", "derive"))
    found = rb._build_inputs(shape, root, [macro], env, {}, LATER, None)
    assert set(found["trees"]) == {str(root / "derive"), str(root / "util")}


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
