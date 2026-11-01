"""`peeky build` of a Rust workspace with a path dependency and a vendored registry dependency,
and what a probe or session does with the build (rust_build)."""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
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


def read(code, result, printed="2"):
    """The probe ran on the build and read `x` at `doubled` as 1, which `app` gave `double`;
    the program printed what the build's `double` returned."""
    assert code == 0, result
    (row,) = [r for r in result["rows"] if r["selector"] == "x" and r["phase"] == "observed"]
    assert row["value"] == "1", row
    assert printed in result["output"].split(), result["output"]


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
    read(*probe(capsys, workspace, config))
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
    read(*probe(capsys, workspace, config, "util/src/lib.rs:3", "--build"), printed="3")
    manifest.write_text(manifest.read_text() + "# again\n")
    code, result = probe(capsys, workspace, config)
    assert result["error"]["code"] == "BUILD_REQUIRED"
    assert "util/Cargo.toml changed since `peeky build` ran" in result["error"]["message"]
    # --build builds first, then the probe goes on with the build.
    read(*probe(capsys, workspace, config, "util/src/lib.rs:3", "--build"), printed="3")
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
    read(*probe(capsys, workspace, config), printed="3")
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


TOKEN = "rust-workspace-worker-token-0123456789abcdef"


@pytest.fixture
def worker(workspace, tmp_path, monkeypatch):
    """A worker on the workspace (its host's copy), started with the workspace's
    `--rust-config`, and a client's checkout of the same files: (host, local, URL, the
    worker's target)."""
    import shutil
    import threading

    from peeky.cli import WORKER_TOKEN_ENV, rust_tools
    from peeky.worker import load_rust_target, make_server

    local = tmp_path / "local"
    shutil.copytree(workspace, local)
    config = tmp_path / "worker.json"
    data = {"analyse": ["util"], "run": {"bin": "app"}, "vendor": "vendor", **rust_tools()}
    config.write_text(json.dumps(data))
    target = load_rust_target(config, workspace)
    monkeypatch.setenv(WORKER_TOKEN_ENV, TOKEN)
    server = make_server(workspace, TOKEN, 0, adapter=target)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield workspace, local, f"http://127.0.0.1:{server.server_port}", target
    finally:
        server.builds.close()
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def remote_probe(capsys, local, url, *more):
    return run(
        capsys,
        "probe",
        str(local),
        "--at",
        "util/src/lib.rs:3",
        "--capture",
        "x",
        "--quiet",
        "--no-save",
        "--worker",
        url,
        *more,
    )


@pytest.mark.timeout(600)
def test_a_worker_builds_and_probes_a_workspace_for_a_remote_client(worker, capsys):
    from peeky.client import Client

    host, local, url, target = worker
    # `peeky build` of the worker's root and its configuration, as a refusal on its host
    # names it; a remote client is told its own command.
    assert target.build_command.startswith(f"peeky build {host} --config ")
    command = f"peeky build {local} --worker {url}"
    code, result = remote_probe(capsys, local, url)
    assert code == 2 and result["error"]["code"] == "BUILD_REQUIRED", result
    assert result["error"]["message"] == (
        "No current Peeky build of this workspace: none has been made for this configuration. "
        f"Run `{command}` first, or add --build"
    )
    assert result["refused_before_launch"] is True
    # A build names no tool, path or flag: the worker's own configuration only.
    code, result = run(capsys, "build", str(local), "--worker", url, "--quiet", "--config", "x")
    assert code == 2 and "leave out --config" in result["error"]["message"], result
    code, summary = run(capsys, "build", str(local), "--worker", url, "--quiet")
    assert code == 0, summary
    assert summary["workspace"] == f"worker:{host}" and summary["analysed"] == ["util"]
    assert summary["executable"].startswith("worker:")
    # The worker compares the build's manifests, lockfile, every file of the workspace it
    # compiled (the binary's too) and the vendored copy's checksum file with ours.
    version = Client(url, TOKEN).call("version")
    assert version["sources"] == {
        "kind": "declared",
        "names": [
            "Cargo.lock",
            "Cargo.toml",
            "app/Cargo.toml",
            "app/src/main.rs",
            "util/Cargo.toml",
            "util/src/lib.rs",
            "vendor/smallnum/.cargo-checksum.json",
        ],
        # Each by its links too; no directory a build script watches, entry by entry.
        "structured": True,
        "trees": {},
    }
    assert "build" in version["operations"]
    # The value read matches the program's own print.
    read(*remote_probe(capsys, local, url))
    # A manifest or source that differs on the client is refused by name, before anything
    # runs.
    manifest = local / "util/Cargo.toml"
    manifest.write_text(manifest.read_text() + "# a comment\n")
    code, result = remote_probe(capsys, local, url)
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert (
        "util/Cargo.toml (the copy on the worker host differs from your checkout, in its "
        "contents or in where a link is or points)"
    ) in (result["error"]["message"])
    assert result["refused_before_launch"] is True
    manifest.write_text((host / "util/Cargo.toml").read_text())
    # The binary decides what reaches `util`: a run of the worker's isn't of the client's code.
    main_rs = local / "app/src/main.rs"
    main_rs.write_text(main_rs.read_text().replace("util::double(", "util::double(1 + "))
    code, result = remote_probe(capsys, local, url)
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert (
        "app/src/main.rs (the copy on the worker host differs from your checkout, in its "
        "contents or in where a link is or points)"
    ) in (result["error"]["message"])
    assert result["refused_before_launch"] is True
    main_rs.write_text((host / "app/src/main.rs").read_text())
    (local / "util/src/lib.rs").write_text(UTIL.replace("x * 2", "x * 3"))
    code, result = remote_probe(capsys, local, url)
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert "differs for the program util/src/lib.rs" in result["error"]["message"]
    # The same change on the worker's host: the build is no longer current.
    (host / "util/src/lib.rs").write_text(UTIL.replace("x * 2", "x * 3"))
    code, result = remote_probe(capsys, local, url)
    assert code == 2 and result["error"]["code"] == "BUILD_REQUIRED", result
    assert result["error"]["message"] == (
        "No current Peeky build of this workspace: util/src/lib.rs changed since `peeky build` "
        f"ran. Run `{command}` again, or add --build"
    )
    # --build: the worker builds first, then the probe reads the new build.
    read(*remote_probe(capsys, local, url, "--build"), printed="3")


@pytest.mark.timeout(600)
def test_a_remote_client_compares_what_a_build_script_reads_but_the_checkouts_metadata(
    worker, capsys, tmp_path
):
    import time

    from peeky.client import Client

    host, local, url, _ = worker
    script = BUILD_SCRIPT.replace(
        "fn main() {\n",
        'fn main() {\n    println!("cargo::rerun-if-changed=../.git/HEAD");\n',
    )
    for side, head in ((host, "ref: refs/heads/main\n"), (local, "ref: refs/heads/mine\n")):
        (side / "util/build.rs").write_text(script)
        (side / "util/schema.txt").write_text("one\n")
        (side / ".git").mkdir()
        (side / ".git/HEAD").write_text(head)
    code, summary = run(capsys, "build", str(local), "--worker", url, "--quiet")
    assert code == 0, summary
    names = Client(url, TOKEN).call("version")["sources"]["names"]
    assert {"util/build.rs", "util/schema.txt"} <= set(names), names
    # `.git` is the checkout's: two checkouts of the same sources differ there.
    assert not [name for name in names if ".git" in name], names
    read(*remote_probe(capsys, local, url))
    # What the script reads shapes the program: a client's copy that differs is refused.
    (local / "util/schema.txt").write_text("two\n")
    code, result = remote_probe(capsys, local, url)
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert (
        "util/schema.txt (the copy on the worker host differs from your checkout, in its "
        "contents or in where a link is or points)"
    ) in (result["error"]["message"])
    (local / "util/schema.txt").write_text("one\n")
    # An input outside the root: Peeky builds only what it records there, and no client has a
    # copy of it.
    outside = tmp_path / "outside.txt"
    outside.write_text("out\n")
    script = script.replace(
        "fn main() {\n", 'fn main() {\n    println!("cargo::rerun-if-changed=../../outside.txt");\n'
    )
    for side in (host, local):
        (side / "util/build.rs").write_text(script)
    time.sleep(1.2)  # Older than a build's start: not a change while Cargo ran.
    code, result = remote_probe(capsys, local, url, "--build")
    assert code == 2 and result["error"]["code"] == "UNSUPPORTED", result
    assert result["error"]["message"] == (
        f"Peeky builds only files inside {host.resolve()}: `app`'s build read "
        f"{outside.resolve()}, which no remote client could compare either. Move it into the "
        "workspace, or drop what names it: an `include!`, a `#[path]`, or a build script's "
        "`rerun-if-changed`"
    )
    assert result["refused_before_launch"] is True


@pytest.mark.timeout(600)
def test_a_remote_client_compares_a_watched_directorys_entries_and_links(worker, capsys):
    from peeky.client import Client

    host, local, url, _ = worker
    script = BUILD_SCRIPT.replace(
        "fn main() {\n", 'fn main() {\n    println!("cargo::rerun-if-changed=proto");\n'
    )
    for side in (host, local):
        (side / "util/build.rs").write_text(script)
        (side / "util/schema.txt").write_text("one\n")
        (side / "util/data.txt").write_text("a\n")
        (side / "util/proto").mkdir()
        (side / "util/proto/a.txt").symlink_to("../data.txt")
    time.sleep(1.2)  # Older than a build's start: not a change while Cargo ran.
    code, summary = run(capsys, "build", str(local), "--worker", url, "--quiet")
    assert code == 0, summary
    sources = Client(url, TOKEN).call("version")["sources"]
    assert sources["trees"] == {"util/proto": "exact"}, sources
    assert "util/proto/a.txt" in sources["names"], sources
    read(*remote_probe(capsys, local, url))
    # An entry only the client has, in a directory the build script watches: Cargo would
    # rerun the script, which may read it.
    (local / "util/proto/extra.txt").write_text("x\n")
    code, result = remote_probe(capsys, local, url)
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert result["error"]["message"] == (
        "Worker source provenance differs for util/proto/ (its entries on the worker host "
        "differ from your checkout's: an entry on one side only, one of another kind, or a link "
        "that points elsewhere): refused before launching. Sync sources before debugging "
        "remotely"
    )
    assert result["refused_before_launch"] is True
    (local / "util/proto/extra.txt").unlink()
    # The worker's is a link; the client's a file of the same bytes, or a link elsewhere.
    linked = (
        "util/proto/a.txt (the copy on the worker host differs from your checkout, in its "
        "contents or in where a link is or points)"
    )
    (local / "util/proto/a.txt").unlink()
    (local / "util/proto/a.txt").write_text("a\n")
    code, result = remote_probe(capsys, local, url)
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert linked in result["error"]["message"], result
    (local / "util/proto/a.txt").unlink()
    (local / "util/copy.txt").write_text("a\n")
    (local / "util/proto/a.txt").symlink_to("../copy.txt")
    code, result = remote_probe(capsys, local, url)
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert linked in result["error"]["message"], result
    (local / "util/proto/a.txt").unlink()
    (local / "util/proto/a.txt").symlink_to("../data.txt")
    read(*remote_probe(capsys, local, url))


@pytest.mark.timeout(600)
def test_a_package_cargo_watches_whole_is_compared_as_cargo_lists_it(worker, capsys):
    from peeky.client import Client

    host, local, url, _ = worker
    # No `rerun-if-changed`: Cargo reruns the script for any file of its package, as
    # `cargo package --list` lists them, which with git leaves out what git ignores.
    for side in (host, local):
        (side / "util/build.rs").write_text("fn main() {}\n")
        (side / "util/data.txt").write_text("d\n")
        (side / "util/.gitignore").write_text("*.log\n")
    subprocess.run(["git", "init", "-q", str(host)], check=True)
    subprocess.run(["git", "-C", str(host), "add", "-A"], check=True)
    (host / "util/build.log").write_text("the worker's own\n")
    time.sleep(1.2)  # Older than a build's start: not a change while Cargo ran.
    code, summary = run(capsys, "build", str(local), "--worker", url, "--quiet")
    assert code == 0, summary
    sources = Client(url, TOKEN).call("version")["sources"]
    assert sources["trees"] == {}, sources
    names = set(sources["names"])
    assert {"util/build.rs", "util/data.txt", "util/.gitignore"} <= names, names
    assert "util/build.log" not in names, names
    # What a client has that Cargo leaves out is no difference: a git-ignored file, or
    # a .DS_Store.
    (local / "util/client.log").write_text("the client's own\n")
    (local / "util/.DS_Store").write_text("Finder's\n")
    read(*remote_probe(capsys, local, url))
    # A file Cargo lists that the client lacks is named.
    (local / "util/data.txt").unlink()
    code, result = remote_probe(capsys, local, url)
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert "util/data.txt (missing in your checkout)" in result["error"]["message"], result


@pytest.mark.timeout(600)
def test_a_name_cargo_lists_is_compared_as_cargo_writes_it(worker, capsys):
    from peeky.client import Client

    host, local, url, _ = worker
    # Cargo lists ` leading.txt` as it is: Peeky compares that file, not `leading.txt`.
    for side in (host, local):
        (side / "util/build.rs").write_text("fn main() {}\n")
        (side / "util/ leading.txt").write_text("l\n")
    time.sleep(1.2)  # Older than a build's start: not a change while Cargo ran.
    code, summary = run(capsys, "build", str(local), "--worker", url, "--quiet")
    assert code == 0, summary
    assert summary["notes"] == [], summary
    names = Client(url, TOKEN).call("version")["sources"]["names"]
    assert "util/ leading.txt" in names and "util/leading.txt" not in names, names
    read(*remote_probe(capsys, local, url))
    (local / "util/ leading.txt").write_text("the client's\n")
    code, result = remote_probe(capsys, local, url)
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert "util/ leading.txt (the copy on the worker host differs" in result["error"]["message"]


@pytest.mark.timeout(600)
def test_a_link_out_of_the_root_is_never_followed_for_a_remote_client(worker, capsys, tmp_path):
    host, local, url, _ = worker
    code, summary = run(capsys, "build", str(local), "--worker", url, "--quiet")
    assert code == 0, summary
    # The client's app/src, which the build compiled, is a link out of its checkout, to a
    # copy of the same files.
    outside = tmp_path / "elsewhere"
    (local / "app/src").rename(outside)
    (local / "app/src").symlink_to(outside)
    code, result = remote_probe(capsys, local, url)
    assert code == 2 and result["error"]["code"] == "UNSUPPORTED", result
    assert result["error"]["message"] == (
        "Peeky compares only files inside the root: on your checkout, app/src/main.rs leads out "
        f"of it through app/src, a link to {outside.resolve()}. Nothing there was read. Point "
        "the link inside the root, or make it a file or directory there"
    )


# A build script that says where it runs, then outlives any test: what a worker's end must
# stop.
SLEEPER = """fn main() {{
    std::fs::write({pid}, std::process::id().to_string()).unwrap();
    std::thread::sleep(std::time::Duration::from_secs({seconds}));
}}
"""


def sleeper(root, tmp_path, seconds=600):
    pid = tmp_path / "script.pid"
    pid.unlink(missing_ok=True)
    (root / "util/build.rs").write_text(SLEEPER.format(pid=json.dumps(str(pid)), seconds=seconds))
    return pid


def running(pid_file, timeout=180, process=None):
    """The build script's pid, once it runs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process is None or process.poll() is None, process.stdout.read()
        if pid_file.is_file() and pid_file.read_text().strip():
            return int(pid_file.read_text())
        time.sleep(0.2)
    raise AssertionError("the build script never ran")


def gone(pid, timeout=20):
    """Whether the process ``pid`` ended within ``timeout`` (a zombie awaiting its reaper
    counts as ended)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.strip()
        if not state or state.startswith("Z"):
            return True
        time.sleep(0.2)
    return False


def start_worker(root, config, *more):
    """A worker process, as `peeky-worker` runs: (the process, its URL)."""
    import sys

    import peeky

    source = str(Path(peeky.__file__).resolve().parents[1])
    command = [
        sys.executable,
        "-c",
        "import sys; sys.path.insert(0, sys.argv.pop(1)); from peeky.worker import main; main()",
        source,
        "--root",
        str(root),
        "--rust-config",
        str(config),
        "--port",
        "0",
        *more,
    ]
    process = subprocess.Popen(
        command,
        env={**os.environ, "DEBUG_TOKEN": TOKEN},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    line = process.stdout.readline()
    found = re.search(r"http://127\.0\.0\.1:([0-9]+)", line)
    assert found, line
    return process, f"http://127.0.0.1:{found[1]}"


def worker_config(root, tmp_path):
    from peeky.cli import rust_tools

    config = tmp_path / "worker.json"
    data = {"analyse": ["util"], "run": {"bin": "app"}, "vendor": "vendor", **rust_tools()}
    config.write_text(json.dumps(data))
    return config


@pytest.mark.timeout(600)
def test_a_workers_shutdown_stops_its_build(worker, tmp_path):
    from peeky.client import Client

    host, _, url, _ = worker
    pid_file = sleeper(host, tmp_path)
    client = Client(url, TOKEN)
    assert client.call("build", wait=0)["status"] == "building"
    pid = running(pid_file)
    assert client.call("shutdown") == {"state": "shutdown"}
    # The build script Cargo ran is gone with the build: none outlives the worker.
    assert gone(pid)


@pytest.mark.timeout(600)
def test_a_terminated_worker_stops_its_build(workspace, tmp_path):
    from peeky.client import Client

    pid_file = sleeper(workspace, tmp_path)
    process, url = start_worker(workspace, worker_config(workspace, tmp_path))
    try:
        assert Client(url, TOKEN).call("build", wait=0)["status"] == "building"
        pid = running(pid_file)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=60) is not None
        assert gone(pid)
    finally:
        process.kill()
        process.wait()


@pytest.mark.timeout(600)
def test_an_idle_worker_waits_for_its_build_and_leaves_no_process(workspace, tmp_path):
    from peeky.client import Client

    pid_file = sleeper(workspace, tmp_path, seconds=8)
    process, url = start_worker(
        workspace, worker_config(workspace, tmp_path), "--idle-timeout", "1", "--exit-when-idle"
    )
    try:
        assert Client(url, TOKEN).call("build", wait=0)["status"] == "building"
        pid = running(pid_file)
        # Past the idle timeout, unasked: the running build is the worker's activity.
        time.sleep(3)
        assert process.poll() is None
        # Once the build ended, the idle worker exits, and leaves no process behind.
        assert process.wait(timeout=120) is not None
        assert gone(pid)
    finally:
        process.kill()
        process.wait()


@pytest.mark.timeout(600)
def test_a_remote_session_on_a_workspace_reads_the_workers_build(worker, capsys):
    host, local, url, _ = worker
    code, result = run(
        capsys,
        "session",
        "start",
        str(local),
        "--at",
        "util/src/lib.rs:3",
        "--name",
        "remote-ws",
        "--worker",
        url,
    )
    assert code == 2 and result["error"]["code"] == "BUILD_REQUIRED", result
    assert f"Run `peeky build {local} --worker {url}` first" in result["error"]["message"]
    code, result = run(
        capsys,
        "session",
        "start",
        str(local),
        "--at",
        "util/src/lib.rs:3",
        "--name",
        "remote-ws",
        "--worker",
        url,
        "--build",
    )
    assert code == 0, result
    try:
        code, rows = run(capsys, "session", "get", "x", "--name", "remote-ws")
        assert code == 0, rows
        assert [row["value"] for row in rows] == ["1"], rows
    finally:
        run(capsys, "session", "close", "--name", "remote-ws")


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
    read(*probe(capsys, workspace, config))
    (workspace / "util/schema.txt").write_text("two\n")
    code, result = probe(capsys, workspace, config)
    assert result["error"]["code"] == "BUILD_REQUIRED"
    message = result["error"]["message"]
    assert message.startswith("No current Peeky build of this workspace: util/schema.txt")
    assert message.endswith(f"since `peeky build` ran. Run {command} again, or add --build")
    read(*probe(capsys, workspace, config, "util/src/lib.rs:3", "--build"))
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
    assert [
        (each["name"], each["value"], each["from"]) for each in manifest["build_inputs"]["env"]
    ] == [("NUM_JOBS", str(rb.jobs()), "jobs")]


@pytest.mark.timeout(300)
def test_cargos_own_variable_a_build_script_reads_leaves_the_build_current(workspace, capsys):
    # Cargo gives the build script CARGO_PKG_NAME; Peeky's environment has none, and the
    # probe doesn't compare it with that.
    (workspace / "util/build.rs").write_text(
        "fn main() {\n"
        '    println!("cargo::rerun-if-env-changed=CARGO_PKG_NAME");\n'
        '    println!("cargo::rustc-env=NAMED={}", std::env::var("CARGO_PKG_NAME").unwrap());\n'
        "}\n"
    )
    config = configure(workspace)
    code, summary = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 0, summary
    _, manifest = manifest_of(summary)
    assert [
        (each["name"], each["value"], each["from"]) for each in manifest["build_inputs"]["env"]
    ] == [("CARGO_PKG_NAME", "util", "cargo")]
    read(*probe(capsys, workspace, config))


GENERATING_SCRIPT = """fn main() {
    let out = std::env::var("OUT_DIR").unwrap();
    let name = ["RUSTC", "BOOTSTRAP"].join("_");
    let code = format!("fn bootstrap() -> String {{ std::env::var({name:?}).unwrap_or_default() }}\\n");
    std::fs::write(format!("{out}/generated.rs"), code).unwrap();
    println!("cargo::rerun-if-changed=build.rs");
}
"""
GENERATED_MACRO = """use proc_macro::TokenStream;

include!(concat!(env!("OUT_DIR"), "/generated.rs"));

#[proc_macro]
pub fn seen(_: TokenStream) -> TokenStream {
    format!("{:?}", bootstrap()).parse().unwrap()
}
"""


@pytest.mark.timeout(300)
@pytest.mark.parametrize("vendored", [False, True])
def test_a_procedural_macros_generated_source_is_scanned_too(workspace, capsys, vendored):
    # Its own sources never name the variable: the source its build script generates,
    # which it `include!`s, does.
    name = "genmac"
    base = workspace / ("vendor" if vendored else "") / name
    (base / "src").mkdir(parents=True)
    (base / "Cargo.toml").write_text(
        f'[package]\nname = "{name}"\nversion = "1.0.0"\nedition = "2021"\n\n'
        "[lib]\nproc-macro = true\n"
    )
    (base / "build.rs").write_text(GENERATING_SCRIPT)
    (base / "src/lib.rs").write_text(GENERATED_MACRO)
    assert b"RUSTC_BOOTSTRAP" not in (base / "src/lib.rs").read_bytes()
    if vendored:
        files = {
            path.relative_to(base).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in base.rglob("*")
            if path.is_file()
        }
        (base / ".cargo-checksum.json").write_text(
            json.dumps({"files": files, "package": CHECKSUM})
        )
        dependency = f'{name} = "1"'
    else:
        manifest = workspace / "Cargo.toml"
        manifest.write_text(manifest.read_text().replace('"util"]', f'"util", "{name}"]'))
        dependency = f'{name} = {{ path = "../{name}" }}'
    util = workspace / "util/Cargo.toml"
    util.write_text(util.read_text() + f"\n[dependencies]\n{dependency}\n")
    (workspace / "util/src/lib.rs").write_text(
        UTIL + f"\npub fn seen() -> &'static str {{\n    {name}::seen!()\n}}\n"
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
    message = result["error"]["message"]
    assert message.startswith(
        "Peeky analyses only code built as stable Rust builds it: the procedural macro "
        f"`{name}`'s debug/build/{name}-"
    ), message
    assert "/out/generated.rs (generated) mentions RUSTC_BOOTSTRAP" in message


@pytest.mark.timeout(300)
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_git_index_the_build_reads_is_compared_by_its_entries(workspace, capsys):
    # git-version's `git_version!()` (bat's) expands to `include_bytes!` of `.git/index`, so
    # rustc's dependency file lists it, and runs `git describe --dirty` while the crate
    # compiles. git rewrites the index with a file's cached status refreshed: every bat
    # build in a fresh checkout was refused as changed while Cargo ran.
    (workspace / "util/build.rs").write_text(
        "fn main() {\n"
        '    println!("cargo::rerun-if-changed=build.rs");\n'
        '    let described = std::process::Command::new("git")\n'
        '        .args(["describe", "--always", "--dirty"])\n'
        "        .output()\n"
        "        .unwrap();\n"
        "    assert!(described.status.success());\n"
        "}\n"
    )
    (workspace / "util/src/lib.rs").write_text(
        UTIL + '\nconst _: &[u8] = include_bytes!("../../.git/index");\n'
    )
    (workspace / "notes.txt").write_text("notes\n")
    index = workspace / ".git/index"

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
        )

    def stale_status(ago=[1000]):
        # A modification time the index doesn't cache: `git describe --dirty` refreshes it.
        ago[0] += 100
        earlier = time.time() - ago[0]
        os.utime(workspace / "notes.txt", (earlier, earlier))

    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "init")
    stale_status()
    written = index.read_bytes()
    config = configure(workspace)
    code, summary = run(capsys, "build", str(workspace), "--config", str(config), "--quiet")
    assert code == 0, summary
    assert index.read_bytes() != written
    _, manifest = manifest_of(summary)
    assert manifest["sources"][".git/index"].startswith("git-index:")
    read(*probe(capsys, workspace, config))
    # Refreshed again since the build: still current.
    stale_status()
    git("describe", "--always", "--dirty")
    read(*probe(capsys, workspace, config))
    # A staged change changes what git reports: the build is stale.
    (workspace / "notes.txt").write_text("staged\n")
    git("add", "notes.txt")
    code, result = probe(capsys, workspace, config)
    assert code == 2 and result["error"]["code"] == "BUILD_REQUIRED", result
    assert result["error"]["message"].startswith(
        "No current Peeky build of this workspace: .git/index changed since `peeky build` ran"
    )
