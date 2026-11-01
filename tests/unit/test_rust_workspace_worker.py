"""A worker on a Rust workspace (ADR 0008, phase 2): what it builds for a remote client
(`build`), what it compares with the client's files, and how a refusal names the client's own
build command. Without a real toolchain."""

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from peeky import sources
from peeky.cli import _remote_build_required, resolve
from peeky.rust_build import (
    BuildStopped,
    RustWorkspaceTarget,
    compared_sources,
    recorded_comparison,
    remote_refusal,
)
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
        # What a stop ends it with; whether it goes on regardless (``stubborn``), and the
        # error it then ends with (``fail_on_stop``).
        self.stopped_with, self.stubborn, self.fail_on_stop = BuildStopped(True), False, None

    def compared_names(self, root):
        return ("Cargo.toml",)

    def build(self, root, *, deadline=None, stop=None):
        self.calls += 1
        assert deadline is not None and deadline > time.monotonic()
        while not self.release.wait(0.01):
            if stop is not None and stop.is_set() and not self.stubborn:
                raise self.stopped_with
        if self.fail_on_stop is not None and stop is not None and stop.is_set():
            raise self.fail_on_stop
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


def manifest(root="/srv/ws", workspace="/srv/ws/rust", **more):
    return {
        "root": root,
        "inputs": {"rust/Cargo.toml": "a", "rust/Cargo.lock": "b", "rust/util/Cargo.toml": "c"},
        "units": [{"sources": ["rust/util/src/lib.rs"]}],
        "sources": {
            "rust/util/src/lib.rs": "d",
            "rust/util/src/m.rs": "e",
            "rust/app/src/main.rs": "f",
            "rust/util/build.rs": "g",
        },
        "generated": {"debug/build/util-1/out/gen.rs": "h"},
        "watch": {
            "workspace": workspace,
            "vendored": {
                "vendor/smallnum/.cargo-checksum.json": "i",
                "vendor/smallnum/src/lib.rs": "j",
                "vendor/smallnum/Cargo.toml": "k",
            },
        },
        **more,
    }


def test_a_client_compares_every_file_of_the_workspace_the_build_compiled():
    # The program run is the build's, which the binary's own code decides too (`app`, not
    # analysed): every local compile's files, its build script's too, are compared. Of a
    # vendored copy, its checksum file only, which lists its files' hashes. Not what the
    # build generated, which no client has.
    assert compared_sources(manifest()) == {
        "rust/Cargo.lock": "b",
        "rust/Cargo.toml": "a",
        "rust/app/src/main.rs": "f",
        "rust/util/Cargo.toml": "c",
        "rust/util/build.rs": "g",
        "rust/util/src/lib.rs": "d",
        "rust/util/src/m.rs": "e",
        "rust/vendor/smallnum/.cargo-checksum.json": "i",
    }


def test_a_client_compares_what_local_build_scripts_read_but_their_checkouts_metadata():
    record = manifest(
        build_inputs={
            "files": {
                "/srv/ws/rust/util/schema.txt": ["file", "/srv/ws/rust/util/schema.txt", "s"],
                "/srv/ws/rust/util/linked.txt": ["link", "/srv/ws/rust/shared/x.txt", "l"],
                # The checkout's metadata: never compared, at the root or above it.
                "/srv/ws/.git/HEAD": ["file", "/srv/ws/.git/HEAD", "h"],
                "/srv/.git/HEAD": ["file", "/srv/.git/HEAD", "h"],
            },
            "dirs": {
                "/srv/ws/rust/util/proto": {
                    "root": ["dir"],
                    "entries": {
                        "a.proto": "p",
                        "sub": "dir",
                        "sub/b.proto": "q",
                        "to-file": ["link", "../shared/c.proto", "c"],
                        "to-dir": ["link", "../shared", "dir"],
                        ".git": "dir",
                        ".git/config": "g",
                    },
                }
            },
            # The package's walk, for the build's freshness; what a client's are compared
            # with is its files as Cargo lists them, never a git-ignored one.
            "trees": {"/srv/ws/rust/gen": {"build.rs": "t", "data/x.json": "u", "x.log": "i"}},
            "lists": {
                "/srv/ws/rust/gen": {
                    "files": {"build.rs": "t", "data/x.json": "u", ".tracked": "v"},
                    "cargo": True,
                    "note": None,
                }
            },
        }
    )
    found = compared_sources(record)
    assert {
        name: digest
        for name, digest in found.items()
        if name.startswith(("rust/util/schema", "rust/util/linked", "rust/util/proto", "rust/gen"))
    } == {
        "rust/util/schema.txt": "s",
        "rust/util/linked.txt": "l",
        "rust/util/proto/a.proto": "p",
        "rust/util/proto/sub/b.proto": "q",
        "rust/util/proto/to-file": "c",
        "rust/gen/build.rs": "t",
        "rust/gen/data/x.json": "u",
        "rust/gen/.tracked": "v",
    }
    assert not any(".git" in name for name in found)
    assert remote_refusal(record) is None


@pytest.mark.parametrize(
    ("inputs", "said"),
    [
        # The client has no copy of a file outside the root.
        (
            {"files": {"/elsewhere/schema.txt": ["file", "/elsewhere/schema.txt", "s"]}},
            "a build script says it reads /elsewhere/schema.txt, outside the worker's root "
            "/srv/ws, which no client has a copy of",
        ),
        # Nor can it send the hash of what isn't a file.
        (
            {"files": {"/srv/ws/rust/util/gone.txt": ["missing"]}},
            "a build script says it reads rust/util/gone.txt, missing on the worker's host",
        ),
        (
            {"files": {"/srv/ws/rust/util/fifo": ["other", "/srv/ws/rust/util/fifo"]}},
            "rust/util/fifo, not a file on the worker's host",
        ),
        (
            {"dirs": {"/srv/ws/rust/util/proto": {"root": ["missing"], "entries": "missing"}}},
            "rust/util/proto, a directory that is missing on the worker's host",
        ),
        # A package's file Cargo lists, through a link out of the root: never read.
        (
            {"lists": {"/srv/ws/rust/gen": {"files": {"data.txt": {"refused": "outside"}}}}},
            "rust/gen/data.txt is a link that leads out of the root on the worker's host",
        ),
        (
            {"lists": {"/srv/ws/rust/gen": {"files": {"data.txt": {"refused": "dangling"}}}}},
            "rust/gen/data.txt is a link to nothing on the worker's host",
        ),
    ],
)
def test_a_build_input_a_client_cant_compare_refuses_a_remote_run(inputs, said):
    from peeky.errors import classify

    why = remote_refusal(manifest(root="/srv/ws", build_inputs=inputs))
    assert why.startswith("A remote client can't compare what this build read with its own"), why
    assert said in why, why
    assert classify(ValueError(why))["code"] == "UNSUPPORTED"


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
    assert recorded_comparison(target, root) is None
    assert target.compared_names(root) == ("Cargo.toml", "Cargo.lock")
    assert target.remote_refusal(root) is None
    directory = rust_build.build_directory(target, root)
    record = manifest(
        str(root), str(root / "rust"), schema=rust_build.SCHEMA, identity=target.identity(root)
    )
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
    assert recorded_comparison(target, root) is None


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


def test_a_remote_builds_refusals_say_what_to_do():
    from peeky.errors import classify

    found = classify(ValueError(sources.too_many(5000)))
    assert found["code"] == "INVALID_REQUEST" and "fewer packages in analyse" in found["hint"]
    found = classify(TimeoutError("The worker's build is still running past the deadline"))
    assert found["code"] == "TIMEOUT" and "goes on on the worker" in found["hint"]


def test_a_workers_close_stops_its_build_and_starts_none(tmp_path):
    from peeky.errors import classify

    builds, target = _RemoteBuilds(), Builds()
    worker = SimpleNamespace(adapter=target, root=tmp_path, last_used=0.0)
    answer = builds.request(worker, {"wait": 0})
    assert answer["status"] == "building" and builds.active()
    # A request is activity, and so is a running build (`active`).
    assert worker.last_used > 0
    asked = worker.last_used
    # The build ended as its stop says: its processes killed.
    assert builds.close(timeout=5) is None
    assert not builds.active()
    # The build's end is activity too.
    assert worker.last_used > asked
    with pytest.raises(BuildStopped) as stopped:
        builds.request(worker, {"request": answer["request"], "wait": 0})
    found = classify(stopped.value)
    assert found["code"] == "UNREACHABLE" and "Start the worker again" in found["hint"], found
    assert found["message"] == (
        "The worker is shutting down, so it stopped its build: it killed Cargo and the processes "
        "Cargo ran. Start the worker again, then build"
    )
    assert str(BuildStopped(False)) == (
        "The worker is shutting down, so it stopped its build: its next process never started. "
        "Start the worker again, then build"
    )
    with pytest.raises(RuntimeError) as refused:
        builds.request(worker, {"wait": 0})
    assert classify(refused.value)["message"] == (
        "The worker is shutting down, so no build starts. Start the worker again, then build"
    )
    assert target.calls == 1
    # Asked again, it answers for the same build.
    assert builds.close(timeout=5) is None


def test_a_build_that_doesnt_end_when_stopped_is_never_reported_ended(tmp_path):
    builds, target = _RemoteBuilds(), Builds()
    target.stubborn = True
    worker = SimpleNamespace(adapter=target, root=tmp_path, last_used=0.0)
    builds.request(worker, {"wait": 0})
    assert builds.close(timeout=0.2) == (
        "its build is still running 0.2 s after the worker stopped it, and so may the processes "
        "it runs"
    )
    assert builds.active()
    # It ends, but not as stopped: a kill that failed, say. Every later answer says so.
    target.fail_on_stop = PermissionError(1, "Operation not permitted")
    target.fail_on_stop.process_group = 4242
    target.release.set()
    for _ in range(2):
        assert builds.close(timeout=5) == (
            "a build ended with PermissionError: [Errno 1] Operation not permitted, so the "
            "processes it ran may still run. End them yourself: kill -KILL -- -4242"
        )
    assert not builds.active()


def test_a_build_whose_kill_failed_before_any_shutdown_is_reported_when_the_worker_ends(
    tmp_path,
):
    # A build that ended on its own (its deadline, say) with a kill that failed: no one
    # stopped it, and its processes may still run.
    builds, target = _RemoteBuilds(), Builds()
    failed = PermissionError(1, "Operation not permitted")
    failed.process_group = 4242
    target.fail = RuntimeError("The build exceeded its deadline")
    target.fail.__context__ = failed
    worker = SimpleNamespace(adapter=target, root=tmp_path, last_used=0.0)
    answer = builds.request(worker, {"wait": 0})
    target.release.set()
    with pytest.raises(RuntimeError, match="deadline"):
        builds.request(worker, {"request": answer["request"], "wait": 5})
    assert not builds.active()
    assert builds.close(timeout=5) == (
        "a build ended with RuntimeError: The build exceeded its deadline, so the processes it "
        "ran may still run. End them yourself: kill -KILL -- -4242"
    )


def test_a_worker_whose_kill_failed_exits_with_status_1_saying_so(tmp_path, monkeypatch, capsys):
    from peeky import worker as worker_module

    failed = PermissionError(1, "Operation not permitted")
    failed.process_group = 4242
    server = SimpleNamespace(
        builds=_RemoteBuilds(),
        worker=SimpleNamespace(close=lambda: None),
        serve_forever=lambda: None,
        server_close=lambda: None,
        server_port=1,
    )
    server.builds.risks.append((failed, 4242))
    monkeypatch.setattr(worker_module, "make_server", lambda *args, **kwargs: server)
    monkeypatch.setattr(
        worker_module.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None)
    )
    monkeypatch.setattr(worker_module.signal, "signal", lambda *args: None)
    monkeypatch.setenv("DEBUG_TOKEN", "t" * 40)
    monkeypatch.setattr("sys.argv", ["peeky-worker", "--root", str(tmp_path)])
    with pytest.raises(SystemExit) as ended:
        worker_module.main()
    assert ended.value.code == 1
    assert capsys.readouterr().err.strip() == (
        "peeky-worker: exiting, but a build ended with PermissionError: [Errno 1] Operation not "
        "permitted, so the processes it ran may still run. End them yourself: kill -KILL -- -4242"
    )


def test_a_worker_whose_build_doesnt_end_refuses_to_shut_down_and_says_why(tmp_path):
    import threading as threads

    from peeky.client import Client
    from peeky.errors import PeekyError
    from peeky.worker import make_server

    token = "rust-workspace-worker-token-0123456789abcdef"
    target = Builds()
    target.stubborn = True
    server = make_server(tmp_path, token, 0, adapter=target)
    threads.Thread(target=server.serve_forever, daemon=True).start()
    client = Client(f"http://127.0.0.1:{server.server_port}", token)
    try:
        assert client.call("build", wait=0)["status"] == "building"
        server.builds.close = lambda timeout=30, close=server.builds.close: close(0.2)
        with pytest.raises(PeekyError) as refused:
            client.call("shutdown")
        error = refused.value.payload
        assert error["code"] == "BUSY" and error["retryable"] is True, error
        assert error["message"] == (
            "The worker didn't shut down: its build is still running 0.2 s after the worker "
            "stopped it, and so may the processes it runs"
        )
        # It still answers, and owns its build.
        assert client.call("version")["protocol"] > 0
        # The build ends, but not as stopped: the worker shuts down, and says so.
        target.fail_on_stop = OSError("its group survived")
        target.release.set()
        assert client.call("shutdown") == {
            "state": "shutdown",
            "warning": "its build, stopped, ended with an error instead (OSError: its group "
            "survived), so its processes may still run",
        }
        # The worker still answers for it at its end.
        assert server.builds.close(timeout=0) == (
            "its build, stopped, ended with an error instead (OSError: its group survived), so "
            "its processes may still run"
        )
    finally:
        target.release.set()
        server.shutdown()
        server.server_close()


def test_a_stopped_context_kills_and_starts_no_process(tmp_path):
    from peeky.agent import Stopped, execute, stopping

    stop = threading.Event()
    marker = tmp_path / "ran"
    with stopping(stop):
        threading.Timer(0.2, stop.set).start()
        started = time.monotonic()
        with pytest.raises(Stopped) as ended:
            execute(["sleep", "30"], tmp_path, 60)
        assert ended.value.started is True and time.monotonic() - started < 10
        # Once set, nothing starts.
        with pytest.raises(Stopped) as ended:
            execute(["touch", str(marker)], tmp_path, 60)
    assert ended.value.started is False and not marker.exists()
    # Outside the context, an execute takes no stop of it; one given its own stop answers
    # as before.
    assert execute(["true"], tmp_path, 60)["exit_code"] == 0
    assert execute(["true"], tmp_path, 60, stop=stop)["stopped"] is True


def cargo_with_a_build_script(tmp_path, monkeypatch):
    """(``build``'s stop, the build script's pid file): ``rust_build._build`` runs a Cargo
    whose build script outlives it, a child in its process group, and the stop is set once
    the script runs."""
    import os

    from peeky import rust_build

    pid_file, stop = tmp_path / "script.pid", threading.Event()
    script = f"sleep 600 & echo $! > {pid_file}; wait"

    def cargo(target, root, *, deadline=None, progress=None):
        rust_build._run(["sh", "-c", script], root, dict(os.environ), deadline, "`cargo build`")

    def stop_once_running():
        while not (pid_file.is_file() and pid_file.read_text().strip()):
            time.sleep(0.05)
        stop.set()

    monkeypatch.setattr(rust_build, "_build", cargo)
    threading.Thread(target=stop_once_running, daemon=True).start()
    return stop, pid_file


def test_a_stopped_workspace_build_leaves_no_process_of_cargos(tmp_path, monkeypatch):
    # Cargo runs in a process group of its own; a build script it runs is in that group.
    # The build's `stop` kills the group, whatever runs in it, and the build ends.
    from peeky import rust_build

    stop, pid_file = cargo_with_a_build_script(tmp_path, monkeypatch)
    started = time.monotonic()
    with pytest.raises(BuildStopped):
        rust_build.build(None, tmp_path, deadline=time.monotonic() + 120, stop=stop)
    assert time.monotonic() - started < 60
    assert gone(int(pid_file.read_text()))


def test_a_kill_that_fails_is_never_called_a_stopped_build(tmp_path, monkeypatch):
    import os
    import signal

    from peeky import agent, rust_build

    def refused(process):
        raise PermissionError(1, "Operation not permitted")

    stop, pid_file = cargo_with_a_build_script(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "kill_process_group", refused)
    group = None
    try:
        # The kill's own error, never BuildStopped, with the group whose processes may run.
        with pytest.raises(PermissionError) as failed:
            rust_build.build(None, tmp_path, deadline=time.monotonic() + 120, stop=stop)
        group = failed.value.process_group
        assert group == os.getpgid(int(pid_file.read_text()))
        # It is so: the build script still runs.
        assert not gone(int(pid_file.read_text()), timeout=0.5)
        # And a worker's close says so.
        builds, target = _RemoteBuilds(), Builds()
        target.stubborn, target.fail_on_stop = True, failed.value
        worker = SimpleNamespace(adapter=target, root=tmp_path, last_used=0.0)
        builds.request(worker, {"wait": 0})
        threading.Timer(0.2, target.release.set).start()
        assert builds.close(timeout=5) == (
            "a build ended with PermissionError: [Errno 1] Operation not permitted, so the "
            f"processes it ran may still run. End them yourself: kill -KILL -- -{group}"
        )
    finally:
        if group is not None:
            os.killpg(group, signal.SIGKILL)
    assert gone(int(pid_file.read_text()))


def gone(pid, timeout=10):
    """Whether the process ``pid`` ended within ``timeout``: a zombie no one reaped (in a
    container whose first process isn't an init) has ended too."""
    import os
    import subprocess

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
        time.sleep(0.05)
    return False


def checkouts(tmp_path, files, links=()):
    """A worker host's and a client's checkout of the same ``files``; ``links``: {name:
    what it says} on both."""
    found = []
    for side in ("host", "client"):
        root = tmp_path / side
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        for name, says in dict(links).items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).symlink_to(says)
        found.append(root)
    return found


def compared(host, client, names, trees=None):
    """How a worker on ``host`` and a client on ``client`` compare a Rust workspace's files:
    the first that differs, as the worker refuses it, or None."""
    adapter = SimpleNamespace(
        compared_names=lambda root: tuple(names),
        compared_trees=lambda root: dict(trees or {}),
        remote_refusal=lambda root: None,
    )
    ours = sources.worker_sources(adapter, host)
    theirs = sources.client_sources(client, sources.describe(adapter, host))
    return sources.difference(theirs, ours, structured=True)


WATCHED = {"util/build.rs": "fn main() {}\n", "util/proto/a.txt": "a\n", "util/data.txt": "a\n"}


def test_an_entry_only_in_the_clients_watched_directory_is_refused(tmp_path):
    # `cargo::rerun-if-changed=proto`: Cargo reruns the script for any entry there, and the
    # script may read each.
    host, client = checkouts(tmp_path, WATCHED)
    names = ("util/build.rs", "util/proto/a.txt")
    assert compared(host, client, names, {"util/proto": "exact"}) is None
    (client / "util/proto/extra.txt").write_text("x\n")
    assert compared(host, client, names, {"util/proto": "exact"}) == (
        "util/proto/ (its entries on the worker host differ from your checkout's: an entry on "
        "one side only, one of another kind, or a link that points elsewhere)"
    )
    # An empty directory too.
    (client / "util/proto/extra.txt").unlink()
    (client / "util/proto/more").mkdir()
    assert compared(host, client, names, {"util/proto": "exact"}).startswith("util/proto/ ")


def test_a_package_compared_whole_is_compared_by_the_files_cargo_lists(tmp_path):
    # A build script with no `rerun-if-changed`: its package's files as Cargo lists them
    # are the names compared. A client's git-ignored file, or a .DS_Store, is none of them.
    host, client = checkouts(tmp_path, WATCHED)
    names = ("util/build.rs", "util/data.txt", "util/proto/a.txt")
    for junk in ("util/.DS_Store", "util/ignored.log", "util/target/x", "util/proto/Thumbs.db"):
        (client / junk).parent.mkdir(parents=True, exist_ok=True)
        (client / junk).write_text("junk\n")
    assert compared(host, client, names) is None
    # A listed file the client lacks is named.
    (client / "util/data.txt").unlink()
    with pytest.raises(RuntimeError, match=r"util/data.txt \(missing in your checkout\)"):
        compared(host, client, names)


@pytest.mark.parametrize(
    "name", ["util/proto/a.txt", "util/src/lib.rs", "Cargo.toml"], ids=["watched", "src", "top"]
)
def test_a_link_where_the_other_side_has_a_file_of_the_same_bytes_is_refused(tmp_path, name):
    files = {**WATCHED, "util/src/lib.rs": "pub fn f() {}\n", "Cargo.toml": "[workspace]\n"}
    host, client = checkouts(tmp_path, files)
    names = ("Cargo.toml", "util/build.rs", "util/proto/a.txt", "util/src/lib.rs")
    trees = {"util/proto": "exact"}
    assert compared(host, client, names, trees) is None
    # The worker's is a link to a file of the same bytes; the client's a file.
    copy = host / (name + ".real")
    copy.write_bytes((host / name).read_bytes())
    (host / name).unlink()
    (host / name).symlink_to(copy.name)
    assert compared(host, client, names, trees) == (
        f"{name} (the copy on the worker host differs from your checkout, in its contents or "
        "in where a link is or points)"
    )


def test_a_directory_linked_on_one_side_only_is_refused(tmp_path):
    files = {"util/src/lib.rs": "pub fn f() {}\n"}
    host, client = checkouts(tmp_path, files)
    names = ("util/src/lib.rs",)
    (host / "util/src").rename(host / "util/real")
    (host / "util/src").symlink_to("real")
    assert compared(host, client, names) == (
        "util/src/lib.rs (the copy on the worker host differs from your checkout, in its "
        "contents or in where a link is or points)"
    )


def test_links_that_point_elsewhere_are_refused_though_the_bytes_are_the_same(tmp_path):
    files = {**WATCHED, "util/other.txt": "a\n"}
    del files["util/proto/a.txt"]
    links = {"util/proto/a.txt": "../data.txt", "util/proto/b.txt": "../other.txt"}
    host, client = checkouts(tmp_path, files, links)
    names = ("util/build.rs", "util/proto/a.txt", "util/proto/b.txt")
    trees = {"util/proto": "exact"}
    assert compared(host, client, names, trees) is None
    (client / "util/proto/a.txt").unlink()
    (client / "util/proto/a.txt").symlink_to("../other.txt")
    assert compared(host, client, names, trees) == (
        "util/proto/a.txt (the copy on the worker host differs from your checkout, in its "
        "contents or in where a link is or points)"
    )


@pytest.mark.parametrize("side", ["host", "client"])
@pytest.mark.parametrize(
    ("link", "says", "name", "trees", "named"),
    [
        # The file itself.
        ("util/src/lib.rs", "{outside}/lib.rs", "util/src/lib.rs", None, "util/src/lib.rs"),
        # A directory on its way.
        ("util/src", "{outside}", "util/src/lib.rs", None, "util/src/lib.rs"),
        # Out of the root and back in: what it reads is inside, but a link outside decides it.
        ("util/src", "{outside}/back", "util/src/lib.rs", None, "util/src/lib.rs"),
        # An entry of a watched directory, which isn't among the names.
        ("util/proto/x.txt", "{outside}/lib.rs", "util/build.rs", "util/proto", "util/proto/x.txt"),
        # A watched directory itself.
        ("util/proto", "{outside}", "util/build.rs", "util/proto", "util/proto"),
    ],
    ids=["file", "directory-on-the-way", "out-and-back", "watched-entry", "watched-directory"],
)
def test_a_link_out_of_the_root_is_refused_and_nothing_through_it_read(
    tmp_path, monkeypatch, side, link, says, name, trees, named
):
    import shutil

    from peeky.errors import classify

    host, client = checkouts(tmp_path, {**WATCHED, "util/src/lib.rs": "pub fn f() {}\n"})
    checkout = host if side == "host" else client
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "lib.rs").write_text("pub fn f() {}\n")
    # Back into the checkout: its own copy of util/src, moved aside.
    shutil.copytree(checkout / "util/src", checkout / "util/real")
    (outside / "back").symlink_to(checkout / "util/real")
    if (checkout / link).is_dir():
        shutil.rmtree(checkout / link)
    else:
        (checkout / link).unlink(missing_ok=True)
    (checkout / link).symlink_to(says.format(outside=outside))
    read = []
    original = sources._hash
    monkeypatch.setattr(sources, "_hash", lambda path: read.append(str(path)) or original(path))
    with pytest.raises(ValueError) as refused:
        compared(host, client, ("util/build.rs", name), {trees: "exact"} if trees else None)
    where = "the worker host" if side == "host" else "your checkout"
    target = says.format(outside=os.path.realpath(outside))
    assert str(refused.value) == (
        f"Peeky compares only files inside the root: on {where}, {named} leads out of "
        f"it through {link}, a link to {target}. Nothing there was read. Point the link inside "
        "the root, or make it a file or directory there"
    )
    assert classify(refused.value)["code"] == "UNSUPPORTED"
    # Nothing outside was read, nor anything through the link.
    through = os.path.realpath(checkout / link.split("/x.txt")[0])
    assert not [path for path in read if path.startswith((str(checkout / link), through))]
    assert not [path for path in read if os.path.realpath(path).startswith(str(outside))]


@pytest.mark.parametrize("watched", [False, True], ids=["file", "watched-entry"])
def test_every_link_on_a_chain_is_compared(tmp_path, watched):
    # util/proto/a -> ../b -> x on the worker, -> y in the client's checkout: a says the
    # same on both sides, x and y hold the same bytes, and b is compared on neither. Only
    # a's chain tells them apart.
    files = {**WATCHED, "util/x.txt": "same\n", "util/y.txt": "same\n"}
    host, client = checkouts(tmp_path, files)
    for side, end in ((host, "x.txt"), (client, "y.txt")):
        (side / "util/b").symlink_to(end)
        (side / "util/proto/a").symlink_to("../b")
    if watched:
        found = compared(host, client, ("util/build.rs",), {"util/proto": "exact"})
        assert found.startswith("util/proto/ (its entries on the worker host differ"), found
    else:
        assert compared(host, client, ("util/build.rs", "util/proto/a")) == (
            "util/proto/a (the copy on the worker host differs from your checkout, in its "
            "contents or in where a link is or points)"
        )


@pytest.mark.parametrize("side", ["host", "client"])
def test_a_link_loop_or_too_many_links_is_its_own_refusal(tmp_path, side):
    from peeky.errors import classify

    host, client = checkouts(tmp_path, WATCHED)
    checkout = host if side == "host" else client
    where = "the worker host" if side == "host" else "your checkout"
    (checkout / "util/proto/a.txt").unlink()
    (checkout / "util/proto/a.txt").symlink_to("b.txt")
    (checkout / "util/proto/b.txt").symlink_to("a.txt")
    names = ("util/build.rs", "util/proto/a.txt")
    with pytest.raises(ValueError) as refused:
        compared(host, client, names)
    assert str(refused.value) == (
        f"Peeky compares only files it can reach: on {where}, util/proto/a.txt leads into a link "
        "loop at util/proto/a.txt. Nothing through it was read. Break the loop, or make it a "
        "file or directory there"
    )
    assert classify(refused.value)["code"] == "UNSUPPORTED"
    # 41 links in a row, no loop.
    (checkout / "util/proto/a.txt").unlink()
    (checkout / "util/proto/b.txt").unlink()
    (checkout / "util/proto/a.txt").symlink_to("l0")
    for index in range(41):
        (checkout / f"util/proto/l{index}").symlink_to(f"l{index + 1}")
    (checkout / "util/proto/l41").write_text("a\n")
    with pytest.raises(ValueError) as refused:
        compared(host, client, names)
    assert str(refused.value) == (
        f"Peeky compares only files it can reach: on {where}, util/proto/a.txt passes through more "
        "than 40 links, the last util/proto/l39. Nothing through it was read. Shorten the chain "
        "of links, or make it a file or directory there"
    )
    assert classify(refused.value)["code"] == "UNSUPPORTED"


@pytest.mark.parametrize("remote", [False, True], ids=["local", "worker"])
def test_a_builds_notes_are_said_beside_a_probes_own_output(tmp_path, monkeypatch, capsys, remote):
    from peeky import cli

    note = (
        "`cargo package --list -p util@0.1.0` failed (error: x): Peeky compares every file of "
        "util with a remote client's instead, but .DS_Store, Thumbs.db and desktop.ini"
    )
    built = {"status": "built", "notes": [note]}
    target = RustWorkspaceTarget.from_config(
        {"analyse": ["util"], "run": {"bin": "app"}, **toolchain(tmp_path)}
    )
    monkeypatch.setattr(RustWorkspaceTarget, "build", lambda self, root, **kwargs: built)
    monkeypatch.setattr(cli, "_remote_build", lambda args, deadline: built)
    args = SimpleNamespace(build=True, worker="http://127.0.0.1:1" if remote else None, quiet=True)
    assert cli._build_first(args, tmp_path, target) == built
    assert capsys.readouterr().err == f"peeky: note: {note}\n"


def test_an_empty_directory_a_package_walk_names_is_compared(tmp_path):
    # A build script may look for `feature-enabled/` (`Path::exists`): where a package's
    # walk names it, the client's must be there, a directory too.
    host, client = checkouts(tmp_path, WATCHED)
    for side in (host, client):
        (side / "util/feature-enabled").mkdir()
    names = ("util/build.rs", "util/feature-enabled")
    assert compared(host, client, names) is None
    (client / "util/feature-enabled").rmdir()
    with pytest.raises(RuntimeError, match=r"util/feature-enabled \(missing in your checkout\)"):
        compared(host, client, names)
    (client / "util/feature-enabled").write_text("a file, not a directory\n")
    assert compared(host, client, names) == (
        "util/feature-enabled (the copy on the worker host differs from your checkout, in its "
        "contents or in where a link is or points)"
    )


def test_a_worker_stops_a_build_busy_in_its_own_work(tmp_path, monkeypatch):
    # A build walking and hashing a package (no process running) ends at its next entry
    # once the worker stops it, so the worker's end isn't held up.
    from peeky import rust_build

    for index in range(500):
        (tmp_path / "pkg" / f"d{index}").mkdir(parents=True)

    def walking(target, root, *, deadline=None, progress=None):
        while True:
            rust_build._tree(tmp_path / "pkg", budget=[rust_build.MAX_RECORDED])

    monkeypatch.setattr(rust_build, "_build", walking)
    target = SimpleNamespace(
        compared_names=lambda root: ("Cargo.toml",),
        build=lambda root, *, deadline=None, stop=None: rust_build.build(
            None, root, deadline=deadline, stop=stop
        ),
    )
    builds = _RemoteBuilds()
    worker = SimpleNamespace(adapter=target, root=tmp_path, last_used=0.0)
    assert builds.request(worker, {"wait": 0.2})["status"] == "building"
    started = time.monotonic()
    assert builds.close(timeout=10) is None
    assert time.monotonic() - started < 5 and not builds.active()
