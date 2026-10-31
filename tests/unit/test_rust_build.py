"""Peeky's build of a Rust workspace (rust_build): its rustc wrapper, the checks of rustc's
recorded commands, and its refusals' codes."""

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

    def run(*args, crate="util", cwd=tmp_path / "ws"):
        env = {
            "PATH": os.environ["PATH"],
            "FAKE_LOG": str(log),
            "PEEKY_ANALYSED": json.dumps({"util": [str(source.resolve())]}),
            "PEEKY_UNITS": str(units),
            "CARGO_MAKEFLAGS": "-j --jobserver-fds=6,7",
            "CARGO_PKG_NAME": "util",
        }
        argv = [str(wrapper), str(rustc), "--crate-name", crate, *args]
        subprocess.run(argv, cwd=cwd, env=env, check=True)
        runs = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        log.unlink(missing_ok=True)
        return runs

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
    expand, compile_ = runs
    # Expansion: a second rustc that writes only the expanded text, without Cargo's jobserver.
    assert expand["args"][-3:] == ["-Zunpretty=expanded", "-o", str(unit / "expanded.rs")]
    assert not any(arg.startswith("--emit") or arg == "--out-dir" for arg in expand["args"])
    assert expand["bootstrap"] == "util" and expand["makeflags"] is None
    # The compile: MIR dumped, LLVM IR emitted, nightly flags for this crate alone.
    assert "--emit=dep-info,metadata,link,llvm-ir" in compile_["args"]
    assert "-Zmir-opt-level=0" in compile_["args"]
    assert f"-Zdump-mir-dir={unit / 'mir'}" in compile_["args"]
    assert compile_["bootstrap"] == "util"


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
