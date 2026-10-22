"""#249: a remote client and its worker compare every source the target reads, not only
the breakpoint's file, before anything launches."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from peeky import sources
from peeky.cli import main
from peeky.client import Client
from peeky.errors import PROTOCOL_VERSION, PeekyError
from peeky.worker import DebugWorker, make_server

TOKEN = "remote-sources-token-1234567890"
MAIN = "import helper\nx = helper.page()\nprint(x)\n"
HELPER = "def page():\n    return 3\n"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", TOKEN)
    root = tmp_path / "app"
    root.mkdir()
    (root / "main.py").write_text(MAIN)
    (root / "helper.py").write_text(HELPER)
    return root


def launch(worker, expected):
    return worker.dispatch(
        "launch",
        {
            "program": "main.py",
            "breakpoints": [{"line": 2}],
            "expect_sha256": {"program": sha(MAIN)},
            "expect_inputs": expected,
        },
    )


def test_the_first_source_that_differs_or_is_on_one_side_only_is_named():
    a, b = "a" * 64, "b" * 64
    assert sources.difference({"x.c": a}, {"x.c": a}) is None
    assert sources.difference({"x.c": a, "y.h": a}, {"x.c": a, "y.h": b}) == (
        "y.h (the copy on the worker host differs from your checkout)"
    )
    assert sources.difference({"x.c": a, "z.h": a}, {"x.c": a}) == "z.h (only in your checkout)"
    assert sources.difference({"x.c": a}, {"x.c": a, "w.h": a}) == "w.h (only on the worker host)"
    assert sources.difference({"x.c": a}, {"x.c": None}) == (
        "x.c (missing on the worker host, or not a readable regular file)"
    )
    # By name: the first of several is named.
    assert sources.difference({"b.h": a, "c.h": a}, {"b.h": b, "a.h": a}).startswith("a.h ")


def test_a_python_worker_compares_every_module_under_its_root(app):
    worker = DebugWorker(app)
    own = dict(sources.python_sources(app))
    assert set(own) == {"main.py", "helper.py"}
    with pytest.raises(ValueError, match=r"provenance differs for helper\.py \(the copy on"):
        launch(worker, {**own, "helper.py": sha("def page():\n    return 4\n")})
    with pytest.raises(ValueError, match=r"for extra\.py \(only in your checkout\)"):
        launch(worker, {**own, "extra.py": sha("x = 1\n")})
    (app / "other.py").write_text("y = 2\n")
    with pytest.raises(ValueError, match=r"for other\.py \(only on the worker host\)"):
        launch(worker, own)
    assert worker.dap is None  # nothing was launched


def test_a_workers_paths_come_back_as_the_clients():
    # #252: a remote stop was named by the worker host's path, which the client lacks.
    from peeky.cli import _client_paths

    answer = {
        "location": "/srv/app/src/main.rs:5",
        "frames": [
            {"name": "main", "source": {"path": "/srv/app/src/main.rs"}},
            {"name": "start", "source": {"path": "/rustc/abc/library/std/src/rt.rs"}},
        ],
        "history": [{"result": {"location": "/srv/app:1", "path": "/srv/app"}}],
        # Only locations and paths: other text, and a root that merely shares a prefix.
        "output": "/srv/app/src/main.rs",
        "path": "/srv/application/main.rs",
    }
    assert _client_paths(answer, "/srv/app", "/home/me/app") == {
        "location": "/home/me/app/src/main.rs:5",
        "frames": [
            {"name": "main", "source": {"path": "/home/me/app/src/main.rs"}},
            {"name": "start", "source": {"path": "/rustc/abc/library/std/src/rt.rs"}},
        ],
        "history": [{"result": {"location": "/home/me/app:1", "path": "/home/me/app"}}],
        "output": "/srv/app/src/main.rs",
        "path": "/srv/application/main.rs",
    }
    assert _client_paths(answer, "/", "/home/me/app") == answer


def test_a_program_that_differs_is_named_by_its_path_under_the_root(app):
    # #252: a crate's src/main.rs was named `main.rs`, which other directories may hold.
    (app / "src").mkdir()
    (app / "src" / "main.py").write_text(MAIN)
    worker = DebugWorker(app)
    with pytest.raises(ValueError, match=r"differs for the program src/main\.py: refused"):
        worker.dispatch(
            "launch",
            {
                "program": "src/main.py",
                "breakpoints": [{"line": 2}],
                "expect_sha256": {"program": sha(MAIN + "# changed\n")},
            },
        )
    assert worker.dap is None


def test_only_virtual_environments_are_left_out(app):
    # #254 review: hidden directories and names like `env` were skipped, so a program
    # in .hidden/ imported a .hidden/helper.py that was never compared; round 2: .git too.
    for directory in (".git", "__pycache__", ".hidden", "env", "site-packages", "node_modules"):
        (app / directory).mkdir()
        (app / directory / "mod.py").write_text("z = 3\n")
    for environment in (".venv", "venv"):
        (app / environment / "lib").mkdir(parents=True)
        (app / environment / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (app / environment / "lib" / "installed.py").write_text("z = 3\n")
    assert {name for name, _ in sources.python_sources(app)} == {
        "helper.py",
        "main.py",
        ".git/mod.py",
        "__pycache__/mod.py",
        ".hidden/mod.py",
        "env/mod.py",
        "node_modules/mod.py",
        "site-packages/mod.py",
    }


def test_a_symlinked_directory_under_the_root_is_refused_not_skipped(app, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "linked.py").write_text("z = 3\n")
    (app / "lib").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(ValueError, match="lib is a symlinked directory"):
        sources.python_sources(app)


def test_only_regular_files_are_hashed_and_sizes_are_checked_before_reading(app):
    import os

    os.mkfifo(app / "pipe.py")
    with pytest.raises(ValueError, match="pipe.py is not a readable regular file"):
        sources.python_sources(app)
    assert sources.files(app, ["pipe.py"]) == {"pipe.py": None}
    (app / "pipe.py").unlink()
    with open(app / "huge.py", "wb") as stream:
        stream.truncate(sources.MAX_PYTHON_BYTES + 1)  # sparse: never read
    with pytest.raises(ValueError, match="more than 16 MiB"):
        sources.python_sources(app)


def test_declared_targets_name_their_sources():
    class Native:
        verification_inputs = ("main.c", "page.h", "main.c")

    class Attached:
        sources = ("A.java",)

    assert sources.describe(Native()) == {"kind": "declared", "names": ["main.c", "page.h"]}
    assert sources.describe(Attached()) == {"kind": "declared", "names": ["A.java"]}


def test_the_worker_names_a_declared_source_missing_on_its_host(tmp_path):
    class Native:
        verification_inputs = ("main.c", "page.h")

    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n")
    assert sources.worker_sources(Native(), tmp_path)["page.h"] is None
    expected = {"main.c": sha("int main(void) { return 0; }\n"), "page.h": sha("#define N 3\n")}
    assert sources.difference(expected, sources.worker_sources(Native(), tmp_path)) == (
        "page.h (missing on the worker host, or not a readable regular file)"
    )


@pytest.fixture
def served(app):
    server = make_server(app, TOKEN, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(f"http://127.0.0.1:{server.server_port}", TOKEN)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_version_says_which_sources_a_client_hashes(served, tmp_path):
    from peeky.java import JavaTarget

    version = served.call("version")
    assert version["protocol"] == PROTOCOL_VERSION == 2
    assert version["sources"] == {"kind": "python"}
    target = JavaTarget(bundle=tmp_path, sources=("demo/A.java", "demo/B.java"), main_class="A")
    server = make_server(tmp_path, TOKEN, 0, adapter=target)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", TOKEN)
        assert client.call("version")["sources"] == {
            "kind": "declared",
            "names": ["demo/A.java", "demo/B.java"],
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("operation", ["launch", "probe"])
def test_a_client_that_checks_only_the_program_is_refused(served, operation):
    arguments = {"program": "main.py", "breakpoints": [{"line": 2}]}
    if operation == "probe":
        arguments = {"at": "main.py:2"}
    with pytest.raises(PeekyError) as refused:
        served.call(operation, **arguments, expect_sha256={"program": sha(MAIN)})
    assert refused.value.error_code == "INVALID_REQUEST"
    assert "no sha256 for the target's other sources" in refused.value.payload["message"]


def test_a_manifest_larger_than_the_old_request_limit_reaches_the_comparison(served):
    # 3000 sources make a body of about 300 KB; the limit was 64 KiB.
    expected = {f"pkg/module_{n:04}.py": "0" * 64 for n in range(3000)}
    with pytest.raises(PeekyError) as refused:
        served.call(
            "launch",
            program="main.py",
            breakpoints=[{"line": 2}],
            expect_sha256={"program": sha(MAIN)},
            expect_inputs=expected,
        )
    assert refused.value.error_code == "PROVENANCE_CHANGED"
    assert "for helper.py (only on the worker host)" in refused.value.payload["message"]


def test_expected_inputs_are_bounded():
    with pytest.raises(ValueError, match="1 to 4096"):
        sources.check_expected({f"m{n}.py": "0" * 64 for n in range(4097)})
    with pytest.raises(ValueError, match="1 to 4096"):
        sources.check_expected({})
    with pytest.raises(ValueError, match="sha256 hex"):
        sources.check_expected({"m.py": "not-a-digest"})
    with pytest.raises(ValueError):
        sources.check_expected(["m.py"])


def test_the_client_hashes_only_names_inside_its_target(tmp_path):
    with pytest.raises(ValueError, match="outside the target's directory"):
        sources.client_sources(tmp_path, {"kind": "declared", "names": ["../secret.c"]})
    with pytest.raises(ValueError, match="outside the target's directory"):
        sources.client_sources(tmp_path, {"kind": "declared", "names": ["/etc/passwd"]})
    with pytest.raises(RuntimeError, match=r"page\.h \(missing in your checkout\)"):
        sources.client_sources(tmp_path, {"kind": "declared", "names": ["page.h"]})
    with pytest.raises(ValueError, match="does not say which sources"):
        sources.client_sources(tmp_path, None)


def old_worker(protocol):
    """A worker that answers its version with ``protocol``, and nothing else."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            answer = json.dumps({"protocol": protocol}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_worker_that_cannot_compare_every_source_is_refused_before_anything_runs(app, capsys):
    server = old_worker(1)
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        code = main(["probe", str(app / "main.py"), "--at", "2", "--worker", url, "--json"])
    finally:
        server.shutdown()
        server.server_close()
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result["refused_before_launch"] is True, result
    assert "protocol 1, this client 2" in result["error"]["message"]
    assert "install the same Peeky version" in result["error"]["message"]


def probe_against(monkeypatch, capsys, app, answer):
    class Worker:
        sources = {"kind": "python"}

        def probe(self, *args, **kwargs):
            return answer(kwargs["expect_inputs"])

    monkeypatch.setattr("peeky.cli._remote", lambda url: (Worker(), TOKEN))
    argv = ["probe", str(app / "main.py"), "--at", "2", "--capture", "x", "--no-save"]
    code = main([*argv, "--worker", "http://127.0.0.1:1", "--json"])
    return code, json.loads(capsys.readouterr().out)


def completed(**more):
    return {
        "status": "completed",
        "hits": 1,
        "rows": [{"hit": 1, "phase": "observed", "selector": "x", "value": "3"}],
        "output": "3\n",
        "source_sha256": sha(MAIN),
        **more,
    }


def test_a_result_the_worker_did_not_confirm_is_discarded(app, capsys, monkeypatch):
    code, result = probe_against(monkeypatch, capsys, app, lambda expected: completed())
    assert code == 1 and result["results_discarded"] is True and result["rows"] == []
    assert result["error"]["code"] == "PROVENANCE_CHANGED"
    assert "did not report comparing every source" in result["error"]["message"]


def test_a_result_read_from_other_sources_is_discarded(app, capsys, monkeypatch):
    code, result = probe_against(
        monkeypatch,
        capsys,
        app,
        lambda expected: completed(inputs_checked=sources.digest(expected), inputs_sha256="f" * 64),
    )
    assert code == 1 and result["results_discarded"] is True and not result["output"]
    assert "the sources the worker read (inputs_sha256 ffffffffffff)" in result["error"]["message"]


def test_a_confirmed_result_is_kept(app, capsys, monkeypatch):
    code, result = probe_against(
        monkeypatch,
        capsys,
        app,
        lambda expected: completed(
            inputs_checked=sources.digest(expected), inputs_sha256=sources.python_digest(app)
        ),
    )
    assert code == 0 and result["rows"][0]["value"] == "3", result
    assert "expect_inputs" not in result["probe"]


# Regressions from the #254 review: each passed on its first head.


def test_a_result_without_the_sources_it_read_is_discarded(app, capsys, monkeypatch):
    # Blocking 1: a missing inputs_sha256 was accepted.
    code, result = probe_against(
        monkeypatch,
        capsys,
        app,
        lambda expected: completed(inputs_checked=sources.digest(expected)),
    )
    assert code == 1 and result["results_discarded"] is True and result["rows"] == []
    assert result["error"]["code"] == "PROVENANCE_CHANGED"
    assert "did not report which sources the run read" in result["error"]["message"]


@pytest.mark.parametrize("operation", ["launch", "probe"])
def test_an_empty_manifest_is_refused(served, operation):
    # Blocking 2: `expect_inputs: {}` passed the guard, and /probe then compared nothing.
    arguments = {"program": "main.py", "breakpoints": [{"line": 2}]}
    if operation == "probe":
        arguments = {"at": "main.py:2", "capture": ["x"]}
    with pytest.raises(PeekyError) as refused:
        served.call(operation, **arguments, expect_sha256={"program": sha(MAIN)}, expect_inputs={})
    assert refused.value.error_code == "INVALID_REQUEST", refused.value.payload
    assert "1 to 4096 source paths" in refused.value.payload["message"]


class Built:
    """A compiled target whose build reads its declared sources when it is prepared."""

    verification_inputs = ("main.c", "page.h")
    prepared = False

    def validate_breakpoints(self, points):
        pass

    def prepare(self, program, root, **staging):
        Built.prepared = True
        return BuiltRuntime(root, self.verification_inputs)


class BuiltRuntime:
    def __init__(self, root, names):
        self.source_manifest = sources.files(root, names)

    def assert_current(self):
        pass

    def source_path(self, program):
        return program

    def close(self):
        pass


def test_a_source_changed_between_the_check_and_the_build_is_refused(tmp_path, monkeypatch):
    # Blocking 3: the worker hashed the live sources, then built later; a change in
    # between reached the debugger. The hook runs right after the check, before prepare.
    from peeky.errors import classify

    main_c, header = "int main(void) { return PAGE; }\n", "#define PAGE 3\n"
    (tmp_path / "main.c").write_text(main_c)
    (tmp_path / "page.h").write_text(header)
    checked = sources.worker_sources

    def then_changed(adapter, root):
        own = checked(adapter, root)
        (tmp_path / "page.h").write_text("#define PAGE 4\n")
        return own

    monkeypatch.setattr(sources, "worker_sources", then_changed)
    worker = DebugWorker(tmp_path, adapter=Built())
    with pytest.raises(Exception) as refused:
        worker.dispatch(
            "launch",
            {
                "program": "main.c",
                "breakpoints": [{"line": 1}],
                "expect_sha256": {"program": sha(main_c)},
                "expect_inputs": {"main.c": sha(main_c), "page.h": sha(header)},
            },
        )
    error = classify(refused.value)
    assert Built.prepared and error["code"] == "PROVENANCE_CHANGED", error
    assert "page.h (the copy on the worker host differs from your checkout)" in error["message"]
    assert worker.dap is None  # the debugger was never started


def write_record(name, **fields):
    import os

    from peeky.cli import _record

    path = _record(name)
    record = {
        "name": name,
        "url": "http://127.0.0.1:9",
        "token": TOKEN,
        "remote": True,
        "session_id": "0" * 32,
        "stop_id": None,
        **fields,
    }
    path.write_text(json.dumps(record))
    os.chmod(path, 0o600)


def test_a_remote_session_from_before_protocol_2_is_refused(app, capsys):
    # Blocking 4: a resumed remote session skipped the handshake.
    write_record("legacy")
    assert main(["session", "get", "x", "--name", "legacy", "--json"]) == 2
    message = json.loads(capsys.readouterr().out)["error"]["message"]
    assert "started without source provenance" in message and "start the session again" in message


def test_a_remote_session_checks_the_workers_protocol_on_every_command(app, capsys):
    server = old_worker(1)
    try:
        write_record(
            "resumed",
            url=f"http://127.0.0.1:{server.server_port}",
            protocol=PROTOCOL_VERSION,
            inputs_checked="0" * 64,
        )
        assert main(["session", "get", "x", "--name", "resumed", "--json"]) == 2
    finally:
        server.shutdown()
        server.server_close()
    message = json.loads(capsys.readouterr().out)["error"]["message"]
    assert "Worker speaks protocol 1, this session 2" in message


def test_the_program_must_be_among_the_compared_sources(app):
    # Blocking 6b: a program in a virtual environment was never compared, but ran.
    (app / "tools").mkdir()
    (app / "tools" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (app / "tools" / "run.py").write_text("x = 1\nprint(x)\n")
    worker = DebugWorker(app)
    try:
        with pytest.raises(ValueError, match=r"the program tools/run.py \(not among the compared"):
            worker.dispatch(
                "launch",
                {
                    "program": "tools/run.py",
                    "breakpoints": [{"line": 2}],
                    "expect_sha256": {"program": sha("x = 1\nprint(x)\n")},
                    "expect_inputs": dict(sources.python_sources(app)),
                },
            )
        assert worker.dap is None
    finally:
        worker.close()


def test_frames_in_uncompared_files_under_the_root_are_refused(app):
    # 6(c) option A: a frame under the root must be in a compared .py file; frames outside
    # the root and in a virtual environment inside it are installed code, not compared.
    (app / ".git").mkdir()
    (app / ".git" / "hook.py").write_text("def run(f):\n    return f()\n")
    (app / ".venv" / "lib").mkdir(parents=True)
    (app / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    worker = DebugWorker(app)
    worker.accepted_sources = dict(sources.python_sources(app))

    def frames(*paths):
        return {"stackFrames": [{"source": {"path": str(path)}} for path in paths]}

    worker.check_frames(frames(app / "main.py", "/usr/lib/python3/json/__init__.py"))
    worker.check_frames(frames(app / ".venv" / "lib" / "pkg.py", app / ".git" / "hook.py"))
    assert worker.foreign_frame is None
    # A module that exists only as bytecode names a .py the manifest doesn't have.
    with pytest.raises(RuntimeError, match=r"a stack frame is in compiled\.py"):
        worker.check_frames(frames(app / "main.py", app / "compiled.py"))
    assert worker.foreign_frame == "compiled.py"


def test_your_checkout_changing_during_a_compiled_probe_discards_it(tmp_path, capsys, monkeypatch):
    # Round 2: only Python results were checked against a fresh hash of your checkout.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "loop"
    root.mkdir()
    (root / "main.c").write_text('#include "page.h"\nint main(void) {\n  return PAGE;\n}\n')
    (root / "page.h").write_text("#define PAGE 3\n")

    class Worker:
        sources = {"kind": "declared", "names": ["main.c", "page.h"]}

        def probe(self, *args, **kwargs):
            expected = kwargs["expect_inputs"]
            (root / "page.h").write_text("#define PAGE 4\n")  # while the probe runs
            return {
                "status": "completed",
                "hits": 1,
                "rows": [{"hit": 1, "phase": "observed", "selector": "x", "value": "3"}],
                "output": "",
                "source_sha256": expected["main.c"],
                "inputs_checked": sources.digest(expected),
                "inputs_sha256": sources.digest(expected),
            }

    monkeypatch.setattr("peeky.cli._remote", lambda url: (Worker(), TOKEN))
    argv = ["probe", str(root), "--at", "main.c:3", "--capture", "x", "--no-save", "--json"]
    code = main([*argv, "--worker", "http://127.0.0.1:1"])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["results_discarded"] is True and result["rows"] == [], result
    assert (
        "a source in your checkout changed after the worker compared it"
        in (result["error"]["message"])
    )


def test_the_bytecode_cache_is_never_in_the_root_and_goes_at_close(app, tmp_path, monkeypatch):
    import tempfile

    worker = DebugWorker(app)
    # The system's temporary directory is inside the target: the worker's scratch is used.
    (app / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(app / "tmp"))
    made = worker.make_pycache()
    assert not made.is_relative_to(app.resolve())
    assert made.is_relative_to((tmp_path / "state" / "scratch").resolve())
    assert list((app / "tmp").iterdir()) == []  # the one made inside was removed
    # A probe child's goes under its parent's scratch.
    worker.pycache_base = tmp_path / "probe-scratch" / "pycache"
    child = worker.make_pycache()
    assert child.parent == worker.pycache_base.resolve()
    worker.pycache = str(child)
    worker.close()
    assert not child.exists() and worker.pycache is None
