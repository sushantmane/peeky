"""A worker on a Rust workspace (ADR 0008, phase 2): what it builds for a remote client
(`build`), what it compares with the client's files, and how a refusal names the client's own
build command. Without a real toolchain."""

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from peeky import sources
from peeky.cli import _remote_build_required, resolve
from peeky.rust_build import RustWorkspaceTarget, compared_sources, recorded_names
from peeky.worker import _RemoteBuilds


def toolchain(tmp_path):
    bin_dir = tmp_path / "toolchain/bin"
    bin_dir.mkdir(parents=True)
    (tmp_path / "toolchain/lib/rustlib").mkdir(parents=True)
    tools = {}
    for name in ("cargo", "rustc", "lldb-dap", "llvm-dwarfdump", "cc", "dsymutil"):
        path = bin_dir / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        tools[name] = str(path)
    return {
        "cargo": tools["cargo"],
        "rustc": tools["rustc"],
        "adapter": tools["lldb-dap"],
        "dwarfdump": tools["llvm-dwarfdump"],
        "linker": tools["cc"],
        "dsymutil": tools["dsymutil"],
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"analyse": ["util"], "run": {"bin": "app"}, "vendor": "vendor"},
        {
            "workspace": "rust",
            "analyse": ["axum", "axum-core"],
            "run": {"package": "axum", "lib": True},
            "features": ["json"],
            "arguments": ["--exact", "routing::tests::nest"],
        },
        {"analyse": ["tokio"], "run": {"package": "tests-integration", "test": "rt_common"}},
    ],
)
def test_a_probe_process_reads_the_workers_target_back_as_it_is(tmp_path, fields):
    # The worker hands its probe's process its configuration as a file: read back, it is
    # the same target, and the same build's (its identity).
    target = RustWorkspaceTarget.from_config({**fields, **toolchain(tmp_path)})
    data = json.loads(json.dumps(target.trusted_config()))
    assert data["language"] == "rust"
    again = RustWorkspaceTarget.from_config({k: v for k, v in data.items() if k != "language"})
    assert again == target
    assert again.identity(tmp_path) == target.identity(tmp_path)


class Builds:
    """A Rust workspace's target, as `_RemoteBuilds` uses one: each build waits for its
    release, then gives its summary or fails."""

    def __init__(self, fail=None):
        self.release, self.fail, self.calls = threading.Event(), fail, 0

    def compared_names(self, root):
        return ("Cargo.toml",)

    def build(self, root, *, deadline=None):
        self.calls += 1
        assert deadline is not None and deadline > time.monotonic()
        self.release.wait(10)
        if self.fail:
            raise self.fail
        return {"peeky": "build", "status": "built", "build_id": f"b{self.calls}"}


def test_a_remote_build_answers_within_the_wait_and_is_asked_again_by_its_id(tmp_path):
    builds, target = _RemoteBuilds(), Builds()
    worker = SimpleNamespace(adapter=target, root=tmp_path)
    answer = builds.request(worker, {"wait": 0})
    assert answer["status"] == "building" and len(answer["request"]) == 32
    # Another request joins the running build: no second one starts.
    assert builds.request(worker, {"wait": 0}) == answer
    target.release.set()
    assert builds.request(worker, {"request": answer["request"], "wait": 5}) == {
        "peeky": "build",
        "status": "built",
        "build_id": "b1",
    }
    assert target.calls == 1
    # Once it ended, a request naming none starts the next; an old id names nothing.
    assert builds.request(worker, {"wait": 5})["build_id"] == "b2"
    with pytest.raises(ValueError, match="No such build on this worker"):
        builds.request(worker, {"request": answer["request"]})


def test_a_remote_builds_failure_is_its_answer(tmp_path):
    target = Builds(fail=RuntimeError("Peeky's build of the workspace failed: rustc said so"))
    target.release.set()
    worker = SimpleNamespace(adapter=target, root=tmp_path)
    with pytest.raises(RuntimeError, match="rustc said so"):
        _RemoteBuilds().request(worker, {"wait": 5})


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"wait": 31}, "wait must be a number of seconds within"),
        ({"wait": "5"}, "wait must be a number of seconds within"),
        ({"request": "../x"}, "request names a build by its 32 hex digits"),
        # A request names no tool, path or flag.
        ({"config": "x.json"}, "build takes only `request`"),
        ({"cargo": "/bin/sh"}, "build takes only `request`"),
    ],
)
def test_a_remote_build_request_names_nothing_but_a_build_and_a_wait(tmp_path, args, message):
    worker = SimpleNamespace(adapter=Builds(), root=tmp_path)
    with pytest.raises(ValueError, match=message):
        _RemoteBuilds().request(worker, args)


def test_only_a_workspaces_worker_builds_on_request(tmp_path):
    worker = SimpleNamespace(adapter=None, root=tmp_path)
    with pytest.raises(ValueError, match="isn't a Rust workspace"):
        _RemoteBuilds().request(worker, {})


def test_a_workers_build_required_names_the_clients_own_command():
    error = {
        "code": "BUILD_REQUIRED",
        "message": "No current Peeky build of this workspace: util/src/lib.rs changed since "
        "`peeky build` ran. Run `peeky build /srv/ws --config /srv/rust.json` again, or add "
        "--build",
    }
    args = SimpleNamespace(target="my ws", worker="http://127.0.0.1:8765")
    assert _remote_build_required(error, args)["message"] == (
        "No current Peeky build of this workspace: util/src/lib.rs changed since `peeky build` "
        "ran. Run `peeky build 'my ws' --worker http://127.0.0.1:8765` again, or add --build"
    )
    other = {"code": "PROVENANCE_CHANGED", "message": "Run `x` again"}
    assert _remote_build_required(other, args) is other


def manifest(**more):
    return {
        "inputs": {"Cargo.toml": "a" * 64, "Cargo.lock": "b" * 64, "util/Cargo.toml": "c" * 64},
        "units": [{"sources": ["util/src/lib.rs", "util/src/m.rs"]}],
        "sources": {"util/src/lib.rs": "d" * 64, "util/src/m.rs": "e" * 64, "app/src/main.rs": "f"},
        **more,
    }


def test_a_client_compares_the_builds_manifests_lockfile_and_analysed_sources():
    # Not the binary's own sources (`app`, not analysed): Peeky reads no value there.
    assert compared_sources(manifest()) == {
        "Cargo.lock": "b" * 64,
        "Cargo.toml": "a" * 64,
        "util/Cargo.toml": "c" * 64,
        "util/src/lib.rs": "d" * 64,
        "util/src/m.rs": "e" * 64,
    }


def test_the_recorded_builds_names_are_read_again_only_once_its_record_changed(
    tmp_path, monkeypatch
):
    from peeky import rust_build

    target = RustWorkspaceTarget.from_config(
        {"analyse": ["util"], "run": {"bin": "app"}, **toolchain(tmp_path)}
    )
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "ws"
    root.mkdir()
    # None recorded: the workspace's manifest and lockfile, which a probe is then refused on.
    assert recorded_names(target, root) is None
    assert target.compared_names(root) == ("Cargo.toml", "Cargo.lock")
    directory = rust_build.build_directory(target, root)
    record = manifest(schema=rust_build.SCHEMA, identity=target.identity(root))
    (directory / "manifest.json").write_text(json.dumps(record))
    names = target.compared_names(root)
    assert names == tuple(sorted(compared_sources(record)))
    reads = []
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: reads.append(self) or "{}")
    assert target.compared_names(root) == names and reads == []
    # Another configuration's, or Peeky's, isn't this one's.
    monkeypatch.undo()
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (directory / "manifest.json").write_text(json.dumps({**record, "identity": {}}))
    assert recorded_names(target, root) is None


def test_more_files_than_a_client_may_send_are_refused_on_both_sides(tmp_path):
    names = [f"src/{n}.rs" for n in range(sources.MAX_SOURCES + 1)]
    adapter = SimpleNamespace(compared_names=lambda root: names)
    with pytest.raises(ValueError, match=r"reads 4097 files, more than the 4096 Peeky compares"):
        sources.worker_sources(adapter, tmp_path)
    with pytest.raises(ValueError, match=r"fewer packages in analyse"):
        sources.client_sources(tmp_path, {"kind": "declared", "names": names})


def test_a_workers_workspace_is_the_workers_to_read(tmp_path):
    # A remote probe of a workspace (or a crate with dependencies): the worker's own
    # configuration names what it builds; the client checks no crate shape of its own.
    root = tmp_path / "ws"
    (root / "util/src").mkdir(parents=True)
    (root / "Cargo.toml").write_text('[workspace]\nmembers = ["util"]\n')
    (root / "util/src/lib.rs").write_text("pub fn f() {}\n")
    assert resolve(root, "util/src/lib.rs:1", tools=False)[1:4] == ("util/src/lib.rs", 1, "rust")
    with pytest.raises(ValueError, match="Unsupported Cargo manifest tables"):
        resolve(root, "util/src/lib.rs:1")
