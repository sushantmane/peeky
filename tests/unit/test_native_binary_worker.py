"""A worker on a binary the project built (ADR 0008, phase 1). It takes the binary only from
its own configuration, tells a remote client which files to compare (those its judged units
name, bounded), and compares them once its preparation has judged the units. A frame in a
shared library is refused naming the library, and the session is kept."""

import hashlib
import json
import os
import shutil
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from peeky import cli, native_binary, sources
from peeky.adapters import PythonAdapter
from peeky.client import Client
from peeky.errors import PROTOCOL_VERSION, PeekyError, Refused, classify
from peeky.native_binary import BinaryRefused, BinarySession, BinaryTarget
from peeky.probe import _caller_rows
from peeky.worker import DebugWorker, load_native_target, make_server

TOKEN = "binary-worker-token-0123456789abcdef-0123456789"


def test_a_worker_takes_a_binary_from_its_own_configuration(tmp_path):
    config = tmp_path / "native.json"
    config.write_text(
        json.dumps(
            {
                "binary": "out/prog",
                "language": None,
                "compiler": "/usr/bin/clang-18",
                "arguments": ["a", ""],
                "returns_twice": ["nlr_push"],
            }
        )
    )
    target = load_native_target(config)
    assert isinstance(target, BinaryTarget) and target.binary == "out/prog"
    assert target.arguments == ("a", "") and target.returns_twice == ("nlr_push",)
    for extra in ("sources", "compile_commands", "sysroot"):
        config.write_text(json.dumps({"binary": "prog", extra: ["main.c"]}))
        with pytest.raises(
            ValueError, match=f"A binary target's configuration takes .*; not {extra}"
        ):
            load_native_target(config)
    config.write_text(json.dumps({"binary": "prog", "arguments": "a"}))
    with pytest.raises(ValueError, match="arguments must be a JSON array"):
        load_native_target(config)


@pytest.fixture
def served(tmp_path):
    root = tmp_path / "host"
    (root / "src").mkdir(parents=True)
    servers = []

    def start(target):
        server = make_server(root, TOKEN, 0, adapter=target)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return Client(f"http://127.0.0.1:{server.server_port}", TOKEN)

    yield root, start
    for server, thread in servers:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_a_binary_workers_version_names_its_binary_and_inputs_lists_its_files(served, monkeypatch):
    root, start = served
    listed = []

    def remote_inputs(self, at):
        listed.append(at)
        return ("src/a.c", "src/a.h")

    monkeypatch.setattr(BinaryTarget, "remote_inputs", remote_inputs)
    client = start(BinaryTarget(binary="./out/../out/prog"))
    version = client.call("version")
    # `version` stays cheap: it reads none of the binary's debug information.
    assert version["protocol"] == PROTOCOL_VERSION and "inputs" in version["operations"]
    assert version["sources"] == {"kind": "binary", "binary": "out/prog"} and listed == []
    assert client.call("inputs") == {
        "kind": "declared",
        "names": ["src/a.c", "src/a.h"],
        "binary": "out/prog",
    }
    assert [str(at) for at in listed] == [str(root)]

    def refused(self, at):
        raise BinaryRefused("out/prog has no debug information: rebuild with -g -gdwarf-5")

    monkeypatch.setattr(BinaryTarget, "remote_inputs", refused)
    with pytest.raises(PeekyError) as found:
        client.call("inputs")
    error = found.value.payload
    assert error["code"] == "UNSUPPORTED" and error["message"] == (
        "The worker refuses its binary: out/prog has no debug information: rebuild with -g "
        "-gdwarf-5"
    )
    # The worker's own hint says what to do about its reason.
    assert error["hint"] == classify(BinaryRefused("x has no debug information"))["hint"]

    def missing(self, at):
        raise Refused("INVALID_REQUEST", f"No binary out/prog in {at}")

    monkeypatch.setattr(BinaryTarget, "remote_inputs", missing)
    with pytest.raises(PeekyError) as found:
        client.call("inputs")
    # A request of the client's is never what is wrong.
    assert found.value.payload["hint"] == (
        "Nothing ran. Fix it on the worker's host: its own configuration names the binary and "
        "its tools."
    )


@pytest.fixture
def listing(tmp_path, monkeypatch):
    """A binary in a root, and a stand-in for its units' judging: ``files`` is what the
    units read name; ``calls``, each judging's binary."""
    root = (tmp_path / "project").resolve()
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.c").write_text("int a;\n" * 4)
    (root / "src" / "b.c").write_text("int b;\n")
    (root / "prog").write_bytes(b"binary")
    state = SimpleNamespace(root=root, calls=[], delay=0.0)
    state.files = [str(root / "src" / "a.c"), str(root / "src" / "b.c"), "/usr/include/stdio.h"]

    def judge(self, at, binary, shown, *args):
        state.calls.append(shown)
        time.sleep(state.delay)
        # Never asked to hash: the listing checks its bounds first, and hashes nothing.
        assert args[-1] is False, args
        read = SimpleNamespace(files=tuple((path, "0" * 32) for path in state.files))
        return SimpleNamespace(read=[read])

    def never(*args):
        raise AssertionError("a listing hashed a file")

    monkeypatch.setattr(native_binary, "_executable", lambda binary, shown: None)
    monkeypatch.setattr(native_binary, "_tool", lambda name: name)
    monkeypatch.setattr(native_binary.sys, "platform", "linux")
    monkeypatch.setattr(native_binary, "check_files", never)
    monkeypatch.setattr(native_binary, "_hashes", never)
    monkeypatch.setattr(BinaryTarget, "_judge", judge)
    native_binary._REMOTE_INPUTS.clear()
    yield state
    native_binary._REMOTE_INPUTS.clear()


def test_the_files_a_client_compares_are_listed_once_per_binary(listing):
    target = BinaryTarget(binary="prog")
    # Only the files under the root: the worker checks the others against their MD5s.
    assert target.remote_inputs(listing.root) == ("src/a.c", "src/b.c")
    assert target.remote_inputs(listing.root) == ("src/a.c", "src/b.c")
    assert listing.calls == ["prog"]
    (listing.root / "prog").write_bytes(b"rebuilt binary")
    assert target.remote_inputs(listing.root) == ("src/a.c", "src/b.c")
    assert listing.calls == ["prog"] * 2


def test_concurrent_requests_wait_for_one_listing_or_are_busy(listing, monkeypatch):
    listing.delay = 0.5
    target, answers = BinaryTarget(binary="prog"), []
    threads = [
        threading.Thread(target=lambda: answers.append(target.remote_inputs(listing.root)))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    # One listing ran; the others waited for it, and answered from it.
    assert listing.calls == ["prog"] and answers == [("src/a.c", "src/b.c")] * 8
    (listing.root / "prog").write_bytes(b"rebuilt binary")
    monkeypatch.setattr(native_binary, "REMOTE_INPUTS_SECONDS", 0.05)
    native_binary._LISTING.acquire()
    try:
        with pytest.raises(Refused) as found:
            target.remote_inputs(listing.root)
    finally:
        native_binary._LISTING.release()
    error = classify(found.value)
    assert error["code"] == "BUSY" and error["retryable"] is True, error
    assert error["message"] == (
        "The worker is still listing prog's files for another client: nothing ran"
    )
    assert listing.calls == ["prog"]


def test_too_many_files_are_refused_before_any_is_read(listing, monkeypatch):
    target = BinaryTarget(binary="prog")
    monkeypatch.setattr(sources, "MAX_PYTHON_INPUTS", 1)
    with pytest.raises(Refused) as found:
        target.remote_inputs(listing.root)
    assert found.value.code == "UNSUPPORTED" and str(found.value) == (
        "prog's units name 2 files under the target directory. A worker compares at most 1 "
        "files, 16 MiB in all, with a remote client's copies: run Peeky on the binary's own host"
    )
    assert classify(found.value)["hint"] == (
        "Nothing ran. Run Peeky on the binary's own host, where no file is compared with a "
        "client's."
    )
    monkeypatch.setattr(sources, "MAX_PYTHON_INPUTS", 4096)
    monkeypatch.setattr(sources, "MAX_PYTHON_BYTES", 30)
    (listing.root / "prog").write_bytes(b"rebuilt binary")
    with pytest.raises(Refused) as found:
        target.remote_inputs(listing.root)
    assert str(found.value).startswith(
        "prog's units name 2 files under the target directory (0.0 MiB or more). A worker "
        "compares at most 4,096 files, 0 MiB in all"
    ), str(found.value)
    # Nothing was cached: each listing refused.
    assert native_binary._REMOTE_INPUTS == {}


def test_a_client_runs_only_on_the_binary_the_worker_names():
    client = SimpleNamespace(sources={"kind": "declared", "names": [], "binary": "out/prog"})
    cli._worker_binary(client, "out/prog")
    with pytest.raises(Refused) as found:
        cli._worker_binary(client, "prog")
    assert found.value.code == "INVALID_REQUEST" and str(found.value) == (
        "The worker debugs the binary out/prog, which its configuration names, not prog: pass "
        "--binary out/prog"
    )
    with pytest.raises(Refused, match="^The worker debugs the binary out/prog, which its .*: add"):
        cli._worker_binary(client, None)
    client.sources = {"kind": "declared", "names": ["main.c"]}
    cli._worker_binary(client, None)
    with pytest.raises(Refused, match="^The worker debugs no binary target, so not prog: its"):
        cli._worker_binary(client, "prog")


def test_a_remote_session_keeps_the_sources_compared_at_its_start(tmp_path, monkeypatch):
    asked = []

    def call(self, operation, **arguments):
        asked.append((operation, arguments))
        return {"protocol": PROTOCOL_VERSION, "root": "/srv/app", "sources": {"kind": "python"}}

    monkeypatch.setattr(cli.Client, "call", call)
    named = {"kind": "declared", "names": ["src/a.c"], "binary": "prog"}
    record = {
        "protocol": PROTOCOL_VERSION,
        "inputs_checked": "0" * 64,
        "url": "http://127.0.0.1:9",
        "token": TOKEN,
        "root": str(tmp_path),
        "sources": named,
    }
    client = cli._remote_session("s", record)
    # What the worker named at the start, whatever its binary's units name now.
    assert client.sources == named and client.worker_root == "/srv/app"
    assert asked == [("version", {})]


class Binary:
    """A binary target whose preparation judges its units: the worker compares the files
    they name with a client's once it has (BinaryTarget.remote_inputs is for `version`)."""

    accepts_arguments = True
    supports_deadline = True
    supports_edits = False
    target_identity = {"language": "c", "binary": "prog"}

    def __init__(self, manifest):
        self.manifest, self.prepared = manifest, []

    def remote_inputs(self, root):
        raise AssertionError("listed again at launch")

    def validate_breakpoints(self, points):
        pass

    def prepare(self, program, root, *, deadline=None):
        self.prepared.append(program)
        return SimpleNamespace(
            source_manifest=dict(self.manifest),
            assert_current=lambda: None,
            close=lambda: None,
            cleanup=lambda: None,
        )


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_a_binary_targets_files_are_compared_once_its_units_are_judged(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text("int main(void) { return 0; }\n")
    (tmp_path / "src" / "a.h").write_text("#define A 1\n")
    program = _sha("int main(void) { return 0; }\n")
    target = Binary({"src/a.c": program, "src/a.h": _sha("#define A 1\n")})
    worker = DebugWorker(tmp_path, adapter=target)
    with pytest.raises(RuntimeError) as found:
        worker.dispatch(
            "launch",
            {
                "program": "src/a.c",
                "breakpoints": [{"line": 1}],
                "expect_sha256": {"program": program},
                "expect_inputs": {"src/a.c": program, "src/a.h": _sha("#define A 2\n")},
            },
        )
    assert [str(each) for each in target.prepared] == [str(tmp_path / "src" / "a.c")]
    error = classify(found.value, session_closed=True)
    assert error["code"] == "PROVENANCE_CHANGED", error
    assert "src/a.h (the copy on the worker host differs from your checkout)" in error["message"]
    assert worker.dap is None and not worker.launch_requested


def _elf(path, header, executable):
    """An ELF64 file of two load segments: its header at ``header``, and code at
    ``executable`` (256 bytes)."""
    head = bytearray(64)
    head[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<Q", head, 32, 64)
    struct.pack_into("<HH", head, 54, 56, 2)
    table = struct.pack("<IIQQQQQQ", 1, 4, 0, header, 0, 256, 256, 4096)
    table += struct.pack("<IIQQQQQQ", 1, 5, 256, executable, 0, 256, 256, 4096)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((head + table).ljust(512, b"\0"))
    return path


def _library_session(root, binary):
    session = BinarySession.__new__(BinarySession)
    session.module_identity, session.binary = "AABB", binary
    shown = SimpleNamespace(path="prog")
    session.prepared = SimpleNamespace(source_root=root, manifest=SimpleNamespace(binary=shown))
    return session


def test_a_frame_in_a_shared_library_is_named_by_its_module_on_macos(tmp_path):
    root = tmp_path.resolve()
    binary = root / "prog"
    binary.write_bytes(b"")
    session = _library_session(root, binary)
    modules = [
        {"id": "AA-BB", "path": str(binary)},
        {"id": "CCDD", "name": "libfoo.dylib", "path": str(root / "build" / "libfoo.dylib")},
        {"id": "EEFF", "path": "/usr/lib/system/libsystem_c.dylib"},
        # The binary by its path, whatever its id says.
        {"id": "0000", "path": str(binary)},
    ]
    dap = SimpleNamespace(request=lambda operation, args: {"modules": modules})
    assert session.library_frame(dap, {"moduleId": "CCDD"}) == (
        "this frame runs build/libfoo.dylib; Peeky reads only prog's own units"
    )
    assert session.library_frame(dap, {"moduleId": "EEFF"}) == (
        "this frame runs /usr/lib/system/libsystem_c.dylib; Peeky reads only prog's own units"
    )
    for own in ("AA-BB", "0000", "unknown", None):
        assert session.library_frame(dap, {"moduleId": own}) is None, own


def test_a_frame_in_a_shared_library_is_found_by_its_loaded_segments_on_linux(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(native_binary.sys, "platform", "linux")
    root = (tmp_path / "project").resolve()
    binary = _elf(root / "prog", 0x400000, 0x401000)
    library = _elf(root / "build" / "libfoo.so", 0, 0x1000)
    outside = _elf(tmp_path / "outside" / "libbar.so", 0, 0x1000)
    session = _library_session(root, binary)
    modules = [
        {"id": "AABB", "path": str(binary), "addressRange": str(0x400000)},
        {"id": "1234", "path": str(library), "addressRange": str(0x7F0000000000)},
        {"id": "5678", "path": str(outside), "addressRange": "0x7F1000000000"},
        {"id": "[vdso]", "path": "[vdso]", "addressRange": str(0x7FF000000000)},
    ]
    dap = SimpleNamespace(request=lambda operation, args: {"modules": modules})

    def at(address):
        return session.library_frame(dap, {"instructionPointerReference": f"0x{address:x}"})

    assert (
        at(0x7F0000001010) == "this frame runs build/libfoo.so; Peeky reads only prog's own units"
    )
    assert at(0x7F1000001010) == (f"this frame runs {outside}; Peeky reads only prog's own units")
    # The binary's code, a library's header (no code), and memory no module holds.
    for address in (0x401010, 0x7F0000000010, 0x7F2000000000):
        assert at(address) is None, hex(address)
    assert session.library_frame(dap, {}) is None


class Runtime(PythonAdapter):
    """A runtime whose frame 1 runs a shared library (BinarySession.library_frame)."""

    def library_frame(self, dap, frame):
        if frame["id"] == 12:
            return "this frame runs build/libfoo.so; Peeky reads only prog's own units"
        return None

    def library_path(self, dap, frame):
        return "/usr/lib/libfoo.so" if frame["id"] == 13 else None


class Backend:
    def __init__(self, root):
        self.frames = [
            {"id": 11, "name": "callback", "line": 4, "source": {"path": str(root / "a.py")}},
            {"id": 12, "name": "apply", "line": 3, "source": {"path": str(root / "lib.py")}},
        ]

    def request(self, operation, args):
        if operation == "stackTrace":
            return {"stackFrames": [dict(frame) for frame in self.frames]}
        if operation == "scopes":
            return {"scopes": []}
        raise AssertionError(operation)

    def close(self):
        pass


def test_a_shared_librarys_frame_is_unsupported_and_the_session_kept(tmp_path):
    worker = DebugWorker(tmp_path)
    worker.dap, worker.runtime = Backend(tmp_path), Runtime()
    worker.session, worker.stop_id, worker.state = "session", "stop", "stopped"
    worker.thread_id, worker.capabilities, worker.launched_at = 1, {}, 1000.0
    stop = {"session_id": "session", "stop_id": "stop"}
    with pytest.raises(Refused) as found:
        worker.dispatch("inspect", {**stop, "frame": 1})
    assert found.value.code == "UNSUPPORTED"
    assert str(found.value) == (
        "Nothing in frame 1 (apply) is read: this frame runs build/libfoo.so; Peeky reads only "
        "prog's own units"
    )
    error = classify(found.value, session_closed=False)
    assert error["code"] == "UNSUPPORTED" and error["session_closed"] is False
    assert error["hint"] == (
        "Nothing of that frame is read, and the session is kept. Read a frame of the binary's "
        "own code (`session inspect` lists the stack), or step out to one."
    )
    assert worker.state == "stopped" and worker.dap is not None
    assert worker.dispatch("inspect", stop)["frames"][0]["name"] == "callback"


def test_a_probes_row_of_a_shared_librarys_frame_says_unsupported():
    why = "Nothing in frame 1 (apply) is read: this frame runs libfoo.so; Peeky reads only prog's"

    class Worker:
        def dispatch(self, operation, args):
            raise Refused("UNSUPPORTED", why)

    rows = _caller_rows(Worker(), {}, [("1:doubled", "doubled")], 1, "observed", 1, {"line": 5}, {})
    assert rows == [
        {
            "hit": 1,
            "phase": "observed",
            "selector": "1:doubled",
            "frame": 1,
            "stop_line": 5,
            "error": why,
            "code": "UNSUPPORTED",
        }
    ]


def test_a_session_on_a_binary_starts_its_worker_with_the_binarys_config(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.c").write_text("int main(void) { return 0; }\n")
    (root / "prog").write_bytes(b"")
    started = []

    def start_local(args, at, program, line, language, data, max_pause):
        started.append((program, line, language, data))
        raise RuntimeError("stopped here")

    monkeypatch.setattr(cli, "_start_local", start_local)
    monkeypatch.setattr(cli, "native_tools", lambda language: {"compiler": "cc", "adapter": "d"})
    config = tmp_path / "binary.json"
    config.write_text(json.dumps({"binary": "prog", "compiler": "cc"}))
    for given in (["--config", str(config)], ["--binary", "./prog"]):
        argv = ["session", "start", str(root), *given, "--at", "src/a.c:1", "--json"]
        assert cli.main(argv) != 0
        assert "stopped here" in capsys.readouterr().out
    assert started == [
        ("src/a.c", 1, "c", {"binary": "prog", "compiler": "cc", "language": None}),
        ("src/a.c", 1, "c", {"binary": "prog", "language": None, "compiler": "cc", "adapter": "d"}),
    ]


def test_a_step_into_a_shared_library_names_it(tmp_path):
    worker = DebugWorker(tmp_path)
    worker.dap, worker.runtime = Backend(tmp_path), Runtime()
    # The step went into apply, in a library built outside the root.
    worker.dap.frames = [
        {"id": 13, "name": "apply", "line": 3, "source": {"path": "/opt/foo/foo.c"}},
        *worker.dap.frames,
    ]
    worker.session, worker.stop_id, worker.state = "session", "stop", "stopped"
    worker.thread_id, worker.capabilities, worker.launched_at = 1, {}, 1000.0
    worker.stop_reason, worker.stop_returns = "step", True
    with pytest.raises(ValueError) as found:
        worker.dispatch("inspect", {"session_id": "session", "stop_id": "stop"})
    assert str(found.value) == (
        "The step stopped outside the program's sources (in apply, in the shared library "
        "/usr/lib/libfoo.so); `session step --out` returns to your code, or `session continue`"
    )
    assert worker.state == "stopped" and worker.dap is not None


def test_a_listing_on_macos_hashes_no_object_and_no_dsym_and_keeps_its_deadline(
    tmp_path, monkeypatch
):
    # On macOS, judging reads the debug map's objects and makes a dSYM: a listing checks the
    # objects by their stat identities, and hashes nothing, within its deadline.
    root = (tmp_path / "project").resolve()
    root.mkdir()
    objects = [root / "main.o", root / "util.o"]
    for each in objects:
        each.write_bytes(b"object")
        os.utime(each, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    binary = root / "prog"
    binary.write_bytes(b"binary")
    os.utime(binary, ns=(1_700_000_100_000_000_000, 1_700_000_100_000_000_000))
    entries = [(str(each), 1_700_000_000) for each in objects]
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    hashed = []

    class Reached(Exception):
        pass

    def dsymutil(argv, cwd, env, timeout, what):
        made = Path(argv[3]) / "Contents/Resources/DWARF" / binary.name
        made.parent.mkdir(parents=True)
        made.write_bytes(b"dwarf")
        return ""

    def hashing(path):
        hashed.append(str(path))
        return "0" * 64

    def metadata(*args, **kwargs):
        raise Reached

    monkeypatch.setattr(native_binary.sys, "platform", "darwin")
    monkeypatch.setattr(native_binary, "_version_line", lambda compiler: "Apple clang 21")
    monkeypatch.setattr(native_binary, "debug_map", lambda path: list(entries))
    monkeypatch.setattr(native_binary, "_run", dsymutil)
    monkeypatch.setattr(native_binary, "sha256_file", hashing)
    monkeypatch.setattr(native_binary, "_metadata", metadata)
    target = BinaryTarget(binary="prog")
    tools = ["clang", "dwarfdump", "dsymutil"]

    def judge(hashes, deadline, at):
        shutil.rmtree(at, ignore_errors=True)
        at.mkdir()
        target._judge(root, binary, "prog", "clang", "dwarfdump", tools, {}, at, deadline, hashes)

    with pytest.raises(Reached):
        judge(False, time.monotonic() + 40, scratch)
    assert hashed == []
    # Preparing a run hashes them: the objects twice (before and after dsymutil), the dSYM.
    with pytest.raises(Reached):
        judge(True, time.monotonic() + 40, scratch)
    assert sorted(set(hashed)) == sorted(
        [*map(str, objects), str(scratch / "prog.dSYM" / "Contents/Resources/DWARF/prog")]
    )
    # Past its deadline, the debug map's check stops.
    with pytest.raises(TimeoutError, match="ran past Peeky's deadline"):
        judge(False, time.monotonic() - 1, scratch)


def test_an_archive_read_past_the_deadline_is_a_timeout_not_a_stale_binary(tmp_path, monkeypatch):
    # TimeoutError is an OSError: it must not read as an archive gone or unreadable, which
    # would refuse the binary as stale.
    archive = tmp_path / "libfoo.a"
    archive.write_bytes(b"!<arch>\n")
    os.utime(archive, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    binary = tmp_path / "prog"
    binary.write_bytes(b"binary")
    os.utime(binary, ns=(1_700_000_100_000_000_000, 1_700_000_100_000_000_000))

    def late(path, deadline=None):
        raise TimeoutError(f"Reading {path}'s members ran past Peeky's deadline")

    monkeypatch.setattr(native_binary, "_members", late)
    entries = [(f"{archive}(foo.o)", 1_700_000_000)]
    for hashes in (False, True):
        with pytest.raises(TimeoutError, match="ran past Peeky's deadline"):
            native_binary.check_debug_map(entries, tmp_path, binary, hashes, time.monotonic() + 40)
