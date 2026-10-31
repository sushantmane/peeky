"""`peeky build` of a Rust workspace with a path dependency and a vendored registry dependency,
and what a probe or session does with the build (rust_build)."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from peeky import rust_workspace as rw
from peeky.cli import main

CHECKSUM = "ab" * 32
UTIL = """pub fn double(x: u32) -> u32 {
    let doubled = x * 2;
    doubled
}

#[cfg(test)]
mod tests {
    #[test]
    fn doubles() {
        assert_eq!(super::double(2), 4);
    }
}
"""
FILES = {
    "Cargo.toml": '[workspace]\nmembers = ["app", "util"]\nresolver = "2"\n',
    "app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n\n'
    '[dependencies]\nutil = { path = "../util" }\nsmallnum = "1"\n',
    "app/src/main.rs": 'fn main() {\n    let x = util::double(smallnum::one());\n    println!("{x}");\n}\n',
    "util/Cargo.toml": '[package]\nname = "util"\nversion = "0.1.0"\nedition = "2024"\n',
    "util/src/lib.rs": UTIL,
    "util/tests/flow.rs": "#[test]\nfn flows() {\n    assert_eq!(util::double(3), 6);\n}\n",
    "vendor/smallnum/Cargo.toml": '[package]\nname = "smallnum"\nversion = "1.0.0"\n'
    'edition = "2021"\n',
    "vendor/smallnum/src/lib.rs": "pub fn one() -> u32 {\n    1\n}\n",
}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    names = ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER")
    if not all(os.environ.get("PEEKY_RUST_" + name) for name in names):
        pytest.skip("set PEEKY_RUST_* tool paths to enable Rust conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "ws"
    for name, text in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    copy = root / "vendor/smallnum"
    files = {
        path.relative_to(copy).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in copy.rglob("*")
        if path.is_file()
    }
    (copy / ".cargo-checksum.json").write_text(json.dumps({"files": files, "package": CHECKSUM}))
    home = tmp_path / "cargo-home"
    home.mkdir()
    subprocess.run(
        [os.environ["PEEKY_RUST_CARGO"], "generate-lockfile", "--offline"]
        + list(rw.vendor_arguments(root, "vendor")),
        cwd=root,
        env=rw.cargo_environment(os.environ["PEEKY_RUST_RUSTC"], home),
        check=True,
        capture_output=True,
    )
    return root


def configure(root, **fields):
    config = root / "rust.json"
    config.write_text(
        json.dumps({"analyse": ["util"], "run": {"bin": "app"}, "vendor": "vendor", **fields})
    )
    return config


def run(capsys, *argv):
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def probe(capsys, root, config, at="util/src/lib.rs:3", *more):
    return run(
        capsys,
        "probe",
        str(root),
        "--config",
        str(config),
        "--at",
        at,
        "--capture",
        "x",
        "--quiet",
        *more,
    )


def manifest_of(summary):
    executable = Path(summary["executable"])
    directory = executable.parents[3]
    return directory, json.loads((directory / "manifest.json").read_text())


NOT_YET = "Peeky doesn't read values in a Rust workspace's build yet"


@pytest.mark.timeout(300)
def test_a_probe_without_a_build_is_refused_naming_the_build_command(workspace, capsys):
    config = configure(workspace)
    code, result = probe(capsys, workspace, config)
    assert code == 2, result
    error = result["error"]
    assert error["code"] == "BUILD_REQUIRED" and error["retryable"] is True
    assert error["message"] == (
        "No current Peeky build of this workspace: none has been made for this configuration. "
        f"Run `peeky build {workspace} --config {config}` first, or add --build"
    )
    assert result["refused_before_launch"] is True
    # A session too, before any worker starts.
    code, result = run(
        capsys,
        "session",
        "start",
        str(workspace),
        "--config",
        str(config),
        "--at",
        "util/src/lib.rs:3",
    )
    assert code == 2 and result["error"]["code"] == "BUILD_REQUIRED"


@pytest.mark.timeout(300)
def test_peeky_build_builds_the_analysed_packages_and_records_them(workspace, capsys):
    config = configure(workspace)
    code, summary = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 0, summary
    assert summary["status"] == "built" and summary["analysed"] == ["util"]
    assert summary["run"] == {"package": "app", "target": "app", "kinds": ["bin"]}
    assert summary["units"] == [
        {"package": "util", "crate": "util", "test": False, "expanded": True}
    ]
    # The program Peeky built runs, as the program.
    shown = subprocess.run([summary["executable"]], capture_output=True, text=True, check=True)
    assert shown.stdout == "2\n"
    directory, manifest = manifest_of(summary)
    unit = manifest["units"][0]
    assert unit["opt_level"] == "0" and unit["sources"] == ["util/src/lib.rs"]
    assert {"app/src/main.rs", "util/src/lib.rs"} <= set(manifest["sources"])
    assert "vendor/smallnum/src/lib.rs" in manifest["watch"]["vendored"]
    assert manifest["watch"]["files"][str(workspace / "Cargo.lock")][0] == "file"
    assert manifest["rustflags"] == []
    assert set(manifest["inputs"]) == {
        "Cargo.lock",
        "Cargo.toml",
        "app/Cargo.toml",
        "util/Cargo.toml",
    }
    files = directory / unit["unit"]
    assert (files / "expanded.rs").read_text().startswith("#![feature(prelude_import)]")
    # The expansion had Cargo's jobserver, as the compile did: rustc warned of none.
    assert "jobserver" not in (files / "expand.log").read_text()
    assert list((files / "mir").glob("util.*.PreCodegen.after.mir"))
    assert not list((files / "mir").glob("*.before.mir"))
    assert (files / "ir.ll").stat().st_size > 0
    # The analysed package is compiled again by every build; its dependencies are reused.
    code, again = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 0 and again["build_id"] == summary["build_id"]
    assert again["executable"] != summary["executable"]
    code, third = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    # The two newest builds' files are kept, for a session on the one before.
    assert sorted(path.name for path in (directory / "runs").iterdir()) == sorted(
        Path(each["executable"]).parents[1].name for each in (again, third)
    )


@pytest.mark.timeout(300)
def test_a_change_since_the_build_is_refused_and_build_makes_it_current(
    workspace, capsys, tmp_path
):
    config = configure(workspace)
    command = f"`peeky build {workspace} --config {config}`"
    assert run(capsys, "build", str(workspace), "--config", str(config), "--quiet")[0] == 0
    code, result = probe(capsys, workspace, config)
    assert code == 2 and result["error"]["message"].startswith(NOT_YET)
    # An edit since the build, found before launch, is the normal edit cycle.
    source = workspace / "util/src/lib.rs"
    source.write_text(UTIL.replace("x * 2", "x * 3"))
    code, result = probe(capsys, workspace, config)
    assert result["error"]["code"] == "BUILD_REQUIRED" and result["error"]["retryable"] is True
    assert result["error"]["message"] == (
        "No current Peeky build of this workspace: util/src/lib.rs changed since `peeky build` "
        f"ran. Run {command} again, or add --build"
    )
    # A manifest too.
    manifest = workspace / "util/Cargo.toml"
    manifest.write_text(manifest.read_text() + "# edited\n")
    code, result = probe(capsys, workspace, config, "util/src/lib.rs:3", "--build")
    assert result["error"]["message"].startswith(NOT_YET), result
    manifest.write_text(manifest.read_text() + "# again\n")
    code, result = probe(capsys, workspace, config)
    assert result["error"]["code"] == "BUILD_REQUIRED"
    assert "util/Cargo.toml changed since `peeky build` ran" in result["error"]["message"]
    # --build builds first, then the probe goes on with the build.
    code, result = probe(capsys, workspace, config, "util/src/lib.rs:3", "--build")
    assert result["error"]["message"].startswith(NOT_YET), result
    # A vendored file, and Cargo's configuration above the workspace, count too.
    vendored = workspace / "vendor/smallnum/src/lib.rs"
    assert run(capsys, "build", str(workspace), "--config", str(config), "--quiet")[0] == 0
    # What identifies the build is a provenance change: a vendored file, the lockfile,
    # Cargo's configuration.
    vendored.write_text(vendored.read_text() + "\n")
    code, result = probe(capsys, workspace, config)
    assert result["error"]["code"] == "PROVENANCE_CHANGED"
    assert result["error"]["message"].startswith(
        "Provenance: vendored vendor/smallnum/src/lib.rs changed since `peeky build` ran"
    )
    vendored.write_text(FILES["vendor/smallnum/src/lib.rs"])
    code, result = probe(capsys, workspace, config)
    assert result["error"]["message"].startswith(NOT_YET), result
    above = tmp_path / ".cargo/config.toml"
    above.parent.mkdir()
    above.write_text("[net]\nretry = 2\n")
    code, result = probe(capsys, workspace, config)
    assert result["error"]["code"] == "PROVENANCE_CHANGED"
    assert f"Cargo's configuration {above} appeared since" in result["error"]["message"]
    # A manifest above the workspace, which must be absent, is one too.
    above.unlink()
    (tmp_path / "Cargo.toml").write_text("[workspace]\n")
    code, result = probe(capsys, workspace, config)
    assert result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert f"{tmp_path / 'Cargo.toml'} appeared since" in result["error"]["message"]


@pytest.mark.timeout(300)
def test_a_probe_stops_only_in_the_analysed_packages_files(workspace, capsys):
    config = configure(workspace)
    assert run(capsys, "build", str(workspace), "--config", str(config), "--quiet")[0] == 0
    code, result = probe(capsys, workspace, config, "app/src/main.rs:2")
    assert result["error"]["code"] == "INVALID_REQUEST"
    assert "Peeky stops only in the analysed packages' files (util)" in result["error"]["message"]


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    ("run_", "test"),
    [({"package": "util", "lib": True}, True), ({"package": "util", "test": "flow"}, False)],
)
def test_tests_are_built_as_cargo_test_builds_them(workspace, capsys, run_, test):
    config = configure(workspace, run=run_)
    code, summary = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 0, summary
    assert {(unit["crate"], unit["test"]) for unit in summary["units"]} >= {("util", test)}
    shown = subprocess.run([summary["executable"]], capture_output=True, text=True)
    assert shown.returncode == 0 and "test result: ok. 1 passed" in shown.stdout, shown.stdout


@pytest.mark.timeout(300)
def test_a_compile_error_is_a_build_failure_in_rustcs_words(workspace, capsys):
    config = configure(workspace)
    (workspace / "util/src/lib.rs").write_text(UTIL.replace("x * 2", "x * undefined_name"))
    code, result = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 2 and result["error"]["code"] == "BUILD_FAILED"
    assert "undefined_name" in result["error"]["message"]


@pytest.mark.timeout(300)
def test_an_analysed_package_built_optimized_is_refused(workspace, capsys):
    config = configure(workspace)
    manifest = workspace / "Cargo.toml"
    manifest.write_text(manifest.read_text() + "\n[profile.dev.package.util]\nopt-level = 3\n")
    code, result = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 2 and result["error"]["code"] == "UNSUPPORTED"
    assert "`util`'s util has opt-level 3" in result["error"]["message"]


@pytest.mark.timeout(300)
def test_what_a_workspace_doesnt_do_yet_is_refused(workspace, capsys, tmp_path):
    config = configure(workspace)
    check = tmp_path / "check.py"
    check.write_text("print('ok')\n")
    code, result = run(
        capsys,
        "verify",
        str(workspace),
        "--config",
        str(config),
        "--at",
        "util/src/lib.rs",
        "--check",
        str(check),
        "--old",
        "x * 2",
        "--new",
        "x * 3",
    )
    assert result["error"]["code"] == "UNSUPPORTED"
    assert (
        "doesn't verify or probe candidate edits in a Rust workspace" in result["error"]["message"]
    )
    code, result = run(capsys, "build", str(workspace))
    assert result["error"]["code"] == "INVALID_REQUEST"
    assert "peeky build needs --config" in result["error"]["message"]
    script = tmp_path / "script.py"
    script.write_text("x = 1\nprint(x)\n")
    code, result = run(capsys, "probe", str(script), "--at", "2", "--capture", "x", "--build")
    assert result["error"]["code"] == "INVALID_REQUEST"
    assert "--build is for a Rust workspace's configuration" in result["error"]["message"]


@pytest.mark.timeout(300)
def test_a_worker_loads_a_workspace_and_refuses_a_remote_clients_probe_of_it(workspace):
    from peeky.cli import rust_tools
    from peeky.errors import classify
    from peeky.rust_build import RustWorkspaceTarget
    from peeky.worker import DebugWorker, load_rust_target

    config = workspace / "worker.json"
    data = {"analyse": ["util"], "run": {"bin": "app"}, "vendor": "vendor", **rust_tools()}
    config.write_text(json.dumps(data))
    target = load_rust_target(config, workspace)
    assert isinstance(target, RustWorkspaceTarget)
    # `peeky build` of the worker's root and its configuration, as a refusal names it.
    assert target.build_command == f"peeky build {workspace} --config {config.resolve()}"
    typed = "peeky build . --config rust.json"
    assert load_rust_target(config, workspace, typed).build_command == typed
    program = hashlib.sha256((workspace / "util/src/lib.rs").read_bytes()).hexdigest()
    worker = DebugWorker(workspace, adapter=target)
    with pytest.raises(ValueError) as caught:
        worker.dispatch(
            "launch",
            {
                "program": "util/src/lib.rs",
                "breakpoints": [{"line": 3}],
                "expect_sha256": {"program": program},
                "expect_inputs": {"util/src/lib.rs": program},
            },
        )
    assert str(caught.value).startswith(
        "A worker doesn't probe a Rust workspace for a remote client yet"
    )
    assert classify(caught.value)["code"] == "UNSUPPORTED"


@pytest.mark.timeout(300)
def test_the_build_keeps_the_projects_cfg_rustflags_and_records_them(workspace, capsys):
    library = workspace / "util/src/lib.rs"
    library.write_text(UTIL + "\n#[cfg(special)]\npub fn special() -> u32 {\n    7\n}\n")
    main = workspace / "app/src/main.rs"
    main.write_text(main.read_text().replace("util::double(", "util::special() + util::double("))
    settings = workspace / ".cargo/config.toml"
    settings.parent.mkdir()
    settings.write_text(
        '[build]\nrustflags = ["--cfg", "special", "--check-cfg", "cfg(special)"]\n'
    )
    config = configure(workspace)
    code, summary = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 0, summary
    shown = subprocess.run([summary["executable"]], capture_output=True, text=True, check=True)
    assert shown.stdout == "9\n"
    _, manifest = manifest_of(summary)
    assert manifest["rustflags"] == ["--cfg", "special", "--check-cfg", "cfg(special)"]
    assert ".cargo/config.toml" in manifest["inputs"]


@pytest.mark.timeout(300)
def test_what_stable_rust_refuses_fails_peeky_build_as_a_stable_build_fails(workspace, capsys):
    config = configure(workspace)
    (workspace / "util/src/lib.rs").write_text("#![feature(never_type)]\n" + UTIL)
    plain = subprocess.run(
        [os.environ["PEEKY_RUST_CARGO"], "build", "--offline", "--locked"]
        + list(rw.vendor_arguments(workspace, "vendor")),
        cwd=workspace,
        env={
            **rw.cargo_environment(os.environ["PEEKY_RUST_RUSTC"], workspace.parent / "cargo-home"),
            "CARGO_TARGET_DIR": str(workspace.parent / "plain"),
        },
        capture_output=True,
        text=True,
    )
    assert plain.returncode != 0 and "E0554" in plain.stderr, plain.stderr
    code, result = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 2 and result["error"]["code"] == "BUILD_FAILED"
    assert "E0554" in result["error"]["message"]
    assert "may not be used on the stable release channel" in result["error"]["message"]


BUILD_SCRIPT = """fn main() {
    println!("cargo::rerun-if-changed=schema.txt");
    let schema = std::fs::read_to_string("schema.txt").unwrap();
    println!("cargo::rustc-env=SCHEMA={}", schema.trim());
}
"""


@pytest.mark.timeout(300)
def test_what_a_build_script_reads_is_an_input_of_the_build(workspace, capsys):
    (workspace / "util/build.rs").write_text(BUILD_SCRIPT)
    (workspace / "util/schema.txt").write_text("one\n")
    (workspace / "util/src/lib.rs").write_text(
        UTIL + '\npub fn schema() -> &\'static str {\n    env!("SCHEMA")\n}\n'
    )
    config = configure(workspace)
    command = f"`peeky build {workspace} --config {config}`"
    code, summary = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 0, summary
    _, manifest = manifest_of(summary)
    assert str(workspace / "util/schema.txt") in manifest["build_inputs"]["files"]
    assert probe(capsys, workspace, config)[1]["error"]["message"].startswith(NOT_YET)
    (workspace / "util/schema.txt").write_text("two\n")
    code, result = probe(capsys, workspace, config)
    assert result["error"]["code"] == "BUILD_REQUIRED"
    message = result["error"]["message"]
    assert message.startswith("No current Peeky build of this workspace: util/schema.txt")
    assert message.endswith(f"since `peeky build` ran. Run {command} again, or add --build")
    code, result = probe(capsys, workspace, config, "util/src/lib.rs:3", "--build")
    assert result["error"]["message"].startswith(NOT_YET), result
    # With no rerun-if-changed, Cargo reruns a build script for any file of its package: a
    # new one too.
    (workspace / "util/build.rs").write_text(
        BUILD_SCRIPT.replace('println!("cargo::rerun-if-changed=schema.txt");\n    ', "")
    )
    assert run(capsys, "build", str(workspace), "--config", str(config), "--quiet")[0] == 0
    (workspace / "util/data").mkdir()
    (workspace / "util/data/new.txt").write_text("new\n")
    code, result = probe(capsys, workspace, config)
    assert result["error"]["code"] == "BUILD_REQUIRED"
    assert result["error"]["message"].startswith(
        "No current Peeky build of this workspace: util/data/new.txt changed since"
    )


@pytest.mark.timeout(300)
def test_a_build_a_probe_holds_stays_until_it_is_released(workspace, capsys):
    from peeky import rust_build as rb
    from peeky.cli import rust_tools

    config = configure(workspace)
    target = rb.RustWorkspaceTarget.from_config(
        {**json.loads(config.read_text()), **rust_tools()}, "peeky build"
    )
    code, first = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 0, first
    held = Path(first["executable"]).parents[1]
    lease = rb.lease_build(target, workspace)
    assert lease.manifest["nonce"] == held.name
    for _ in range(2):
        assert run(capsys, "build", str(workspace), "--config", str(config), "--quiet")[0] == 0
    assert held.is_dir() and len(list(held.parent.iterdir())) == 3
    lease.close()
    assert run(capsys, "build", str(workspace), "--config", str(config), "--quiet")[0] == 0
    assert not held.exists() and len(list(held.parent.iterdir())) == 2


@pytest.mark.timeout(300)
def test_a_sessions_worker_names_the_build_command_as_typed(
    workspace, capsys, tmp_path, monkeypatch
):
    import shutil

    from peeky import cli

    spaced = tmp_path / "my ws"
    shutil.move(workspace, spaced)
    config = tmp_path / "my config.json"
    config.write_text(json.dumps({"analyse": ["util"], "run": {"bin": "app"}, "vendor": "vendor"}))
    assert run(capsys, "build", str(spaced), "--config", str(config), "--quiet")[0] == 0
    # A source changes after `session start` checked the build, before its worker does.
    monkeypatch.setattr(cli, "_workspace_ready", lambda *args: None)
    (spaced / "util/src/lib.rs").write_text(UTIL.replace("x * 2", "x * 3"))
    code, result = run(
        capsys,
        "session",
        "start",
        str(spaced),
        "--config",
        str(config),
        "--at",
        "util/src/lib.rs:3",
        "--name",
        "spaced",
    )
    assert code == 2 and result["error"]["code"] == "BUILD_REQUIRED", result
    assert result["error"]["message"] == (
        "No current Peeky build of this workspace: util/src/lib.rs changed since `peeky build` "
        f"ran. Run `peeky build '{spaced}' --config '{config}'` again, or add --build"
    )


@pytest.mark.timeout(300)
def test_rustc_bootstrap_is_the_analysed_compiles_alone(workspace, capsys):
    # A build script runs in Cargo's environment: it never sees the variable.
    (workspace / "util/build.rs").write_text(
        "fn main() {\n"
        '    println!("cargo::rerun-if-changed=build.rs");\n'
        '    let seen = std::env::var("RUSTC_BOOTSTRAP").unwrap_or_default();\n'
        '    println!("cargo::rustc-env=SEEN=[{seen}]");\n'
        "}\n"
    )
    (workspace / "util/src/lib.rs").write_text(
        UTIL + '\npub fn seen() -> &\'static str {\n    env!("SEEN")\n}\n'
    )
    main = workspace / "app/src/main.rs"
    main.write_text(
        main.read_text().replace('println!("{x}");', 'println!("{x} {}", util::seen());')
    )
    config = configure(workspace)
    code, summary = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 0, summary
    shown = subprocess.run([summary["executable"]], capture_output=True, text=True, check=True)
    assert shown.stdout == "2 []\n"
    # The analysed crate's own compile has it: a mention of it there is refused.
    (workspace / "util/src/lib.rs").write_text(
        UTIL + '\npub fn seen() -> Option<&\'static str> {\n    option_env!("RUSTC_BOOTSTRAP")\n}\n'
    )
    main.write_text(main.read_text().replace("util::seen()", 'util::seen().unwrap_or("")'))
    code, result = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 2 and result["error"]["code"] == "UNSUPPORTED", result
    assert result["error"]["message"].startswith(
        "Peeky analyses only code built as stable Rust builds it: `util`'s util/src/lib.rs "
        "mentions RUSTC_BOOTSTRAP"
    )


@pytest.mark.timeout(300)
def test_a_procedural_macro_that_mentions_rustc_bootstrap_is_refused(workspace, capsys):
    # It runs inside the analysed crate's compile, which has RUSTC_BOOTSTRAP.
    (workspace / "derive/src").mkdir(parents=True)
    (workspace / "derive/Cargo.toml").write_text(
        '[package]\nname = "derive"\nversion = "0.1.0"\nedition = "2021"\n\n'
        "[lib]\nproc-macro = true\n"
    )
    (workspace / "derive/src/lib.rs").write_text(
        "use proc_macro::TokenStream;\n\n#[proc_macro]\npub fn seen(_: TokenStream) -> "
        'TokenStream {\n    let seen = std::env::var("RUSTC_BOOTSTRAP").unwrap_or_default();\n'
        '    format!("{seen:?}").parse().unwrap()\n}\n'
    )
    manifest = workspace / "Cargo.toml"
    manifest.write_text(manifest.read_text().replace('"util"]', '"util", "derive"]'))
    util = workspace / "util/Cargo.toml"
    util.write_text(util.read_text() + '\n[dependencies]\nderive = { path = "../derive" }\n')
    (workspace / "util/src/lib.rs").write_text(
        UTIL + "\npub fn seen() -> &'static str {\n    derive::seen!()\n}\n"
    )
    subprocess.run(
        [os.environ["PEEKY_RUST_CARGO"], "generate-lockfile", "--offline"]
        + list(rw.vendor_arguments(workspace, "vendor")),
        cwd=workspace,
        env=rw.cargo_environment(os.environ["PEEKY_RUST_RUSTC"], workspace.parent / "cargo-home"),
        check=True,
        capture_output=True,
    )
    config = configure(workspace)
    code, result = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 2 and result["error"]["code"] == "UNSUPPORTED", result
    assert result["error"]["message"].startswith(
        "Peeky analyses only code built as stable Rust builds it: the procedural macro "
        "`derive`'s derive/src/lib.rs mentions RUSTC_BOOTSTRAP"
    )


@pytest.mark.timeout(300)
def test_a_build_scripts_jobs_are_the_builds_own(workspace, capsys):
    from peeky import rust_build as rb

    (workspace / "util/build.rs").write_text(
        "fn main() {\n"
        '    println!("cargo::rerun-if-env-changed=NUM_JOBS");\n'
        '    println!("cargo::rustc-env=JOBS={}", std::env::var("NUM_JOBS").unwrap());\n'
        "}\n"
    )
    (workspace / "util/src/lib.rs").write_text(
        UTIL + '\npub fn jobs() -> &\'static str {\n    env!("JOBS")\n}\n'
    )
    main = workspace / "app/src/main.rs"
    main.write_text(
        main.read_text().replace('println!("{x}");', 'println!("{x} {}", util::jobs());')
    )
    config = configure(workspace)
    code, summary = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 0, summary
    shown = subprocess.run([summary["executable"]], capture_output=True, text=True, check=True)
    # What the build script was given, and what Peeky recorded, are the build's -j.
    assert shown.stdout == f"2 {rb.jobs()}\n"
    _, manifest = manifest_of(summary)
    assert manifest["build_inputs"]["env"] == {"NUM_JOBS": str(rb.jobs())}
