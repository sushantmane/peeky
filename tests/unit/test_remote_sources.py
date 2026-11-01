"""A remote client and its worker compare every source the target reads, not only
the breakpoint's file, before anything launches."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from peeky import sources
from peeky.cli import main
from peeky.client import Client
from peeky.errors import ERROR_CODES, PROTOCOL_VERSION, PeekyError
from peeky.worker import DebugWorker, make_server

TOKEN = "remote-sources-token-1234567890-0123456789abcdef"
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


def test_the_worker_names_only_the_compared_sources_in_a_remote_result(tmp_path):
    # Paths were mapped by prefix, so `..`, links, virtual environments and
    # files never compared were shown as the client's.
    from peeky.worker import _source_names

    root, outside = tmp_path / "app", tmp_path / "outside"
    for name in ("main.py", "a/util.c", "b/util.c", "gen.py", ".venv/lib/pkg.py"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x = 1\n")
    (root / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    outside.mkdir()
    (outside / "x.py").write_text("x = 1\n")
    (root / "link").symlink_to(outside)
    (root / "alias.py").symlink_to(root / "main.py")
    root = root.resolve()
    # gen.py and the virtual environment were not compared; the links were named, but
    # lead elsewhere.
    names = frozenset({"main.py", "a/util.c", "b/util.c", "link/x.py", "alias.py"})
    at = f"{root}/"
    answer = {
        "location": f"{at}b/util.c:7",
        "innermost": {"location": f"{at}main.py"},
        "frames": [
            {"source": {"path": f"{at}a/util.c"}},
            {"source": {"path": f"{at}../outside/x.py"}},
            {"source": {"path": f"{at}a/../main.py"}},
            {"source": {"path": f"{at}.venv/lib/pkg.py"}},
            {"source": {"path": f"{at}gen.py"}},
            {"source": {"path": f"{at}link/x.py"}},
            {"source": {"path": f"{at}alias.py"}},
            {"source": {"path": f"{at}missing.py"}},
            {"source": {"path": f"{at}main.py`main"}},
            {"source": {"path": "main.py"}},
        ],
        "path": f"{at}main.py",
        "output": f"{at}main.py:3",
    }
    shown = _source_names(answer, root, names)
    assert shown["location"] == "source:b/util.c:7"
    assert shown["innermost"]["location"] == "source:main.py"
    paths = [frame["source"]["path"] for frame in shown["frames"]]
    assert paths[0] == "source:a/util.c"
    # Each of the others stays this host's path, as it was.
    assert paths[1:] == [frame["source"]["path"] for frame in answer["frames"][1:]]
    # Only locations and the paths of sources: other text keeps its paths.
    assert shown["path"] == answer["path"] and shown["output"] == answer["output"]


def test_the_client_shows_its_own_copy_only_of_a_compared_source(tmp_path):
    from peeky.cli import _client_paths

    mine = tmp_path / "mine"
    for name in ("main.py", "a/util.c", "b/util.c"):
        (mine / name).parent.mkdir(parents=True, exist_ok=True)
        (mine / name).write_text("x = 1\n")
    (mine / "link.py").symlink_to(mine / "main.py")
    names = frozenset({"main.py", "a/util.c", "b/util.c", "gone.py", "link.py", "../x.py"})
    answer = {
        "location": "source:b/util.c:7",
        "frames": [
            {"source": {"path": "source:a/util.c"}},
            # Hashed here once, but absent now; never hashed; a link; out of the root.
            {"source": {"path": "source:gone.py"}},
            {"source": {"path": "source:other.py"}},
            {"source": {"path": "source:link.py"}},
            {"source": {"path": "source:../x.py"}},
            # Not named as a compared source, or from an older worker.
            {"source": {"path": "/srv/app/.venv/lib/pkg.py"}},
            {"source": {"path": "/srv/app/main.py"}},
            {"source": {"path": "worker:/srv/app/gen.py"}},
            {"source": {"name": "main"}},
        ],
        "innermost": {"location": "/srv/app/main.py:3"},
        "diagnosis": {"location": "main.c:7"},
        "output": "/srv/app/main.py",
    }
    shown = _client_paths(answer, "/srv/app", mine, names)
    assert shown["location"] == f"{mine}/b/util.c:7"
    assert [frame["source"].get("path") for frame in shown["frames"]] == [
        f"{mine}/a/util.c",
        "worker:/srv/app/gone.py",
        "worker:/srv/app/other.py",
        "worker:/srv/app/link.py",
        "worker:/srv/app/../x.py",
        "worker:/srv/app/.venv/lib/pkg.py",
        "worker:/srv/app/main.py",
        "worker:/srv/app/gen.py",
        None,
    ]
    assert shown["innermost"]["location"] == "worker:/srv/app/main.py:3"
    # A relative name is no path of either host's; other text is left alone.
    assert shown["diagnosis"] == answer["diagnosis"] and shown["output"] == answer["output"]
    # Without the names you hashed, nothing is shown as yours.
    unknown = _client_paths(answer, "/srv/app", mine, None)
    assert unknown["location"] == "worker:/srv/app/b/util.c:7"


def test_a_program_that_differs_is_named_by_its_path_under_the_root(app):
    # A crate's src/main.rs was named `main.rs`, which other directories may hold.
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
    # Hidden directories and names like `env` were skipped, so a program
    # in .hidden/ imported a .hidden/helper.py that was never compared; .git too.
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
    assert version["protocol"] == PROTOCOL_VERSION == 16
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


def test_the_version_lists_every_code_a_worker_returns(served):
    # A worker's `launch` fails with BUILD_FAILED, and its `probe` result's error can be
    # NO_STOP or PROGRAM_CRASHED: the list left all three out.
    codes = served.call("version")["error_codes"]
    assert {"BUILD_FAILED", "NO_STOP", "PROGRAM_CRASHED"} <= set(codes)
    assert codes == sorted(ERROR_CODES)


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
            protocol=PROTOCOL_VERSION,
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


@pytest.mark.parametrize("protocol", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13])
def test_a_worker_of_an_older_protocol_is_refused_before_anything_runs(app, capsys, protocol):
    # Protocol 1 compared only the program; protocol 2 named paths as this client can't
    # tell from its own; protocol 3 took a restore's `@before` for a value;
    # protocol 4 read what a C pointer one past an array's end points at; protocol
    # 5 showed what any other C pointer points at with no mark; protocol 6 didn't say
    # which C pointers point at a pointer.
    server = old_worker(protocol)
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        code = main(["probe", str(app / "main.py"), "--at", "2", "--worker", url, "--json"])
    finally:
        server.shutdown()
        server.server_close()
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result["refused_before_launch"] is True, result
    assert f"protocol {protocol}, this client 16" in result["error"]["message"]
    assert "install the same Peeky version" in result["error"]["message"]


def test_a_client_of_an_older_protocol_is_refused_by_this_worker(app, capsys, monkeypatch):
    # A protocol 2 client accepted this worker, and showed its `source:`
    # names as paths. That client refuses at `version`, as a protocol 2 client does:
    monkeypatch.setattr("peeky.cli.PROTOCOL_VERSION", 2)
    server = make_server(app, TOKEN, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        code = main(["probe", str(app / "main.py"), "--at", "2", "--worker", url, "--json"])
        result = json.loads(capsys.readouterr().out)
        assert code == 2 and result["refused_before_launch"] is True, result
        assert (
            "Worker speaks protocol 16, this client 2: install the same Peeky version"
            in (result["error"]["message"])
        )
        # And the worker refuses what a protocol 2 client sends after its handshake: every
        # hash, and no protocol.
        own = dict(sources.python_sources(app))
        requests = {
            "launch": {"program": "main.py", "breakpoints": [{"line": 2}]},
            "probe": {"at": "main.py:2", "capture": ["x"]},
        }
        for (operation, arguments), (declared, spoken) in zip(
            [*requests.items(), *requests.items()], [({}, 2), ({}, 2), (15, 15), (17, 17)]
        ):
            if declared:
                arguments = {**arguments, "protocol": declared}
            with pytest.raises(PeekyError) as refused:
                Client(url, TOKEN).call(
                    operation, **arguments, expect_sha256={"program": sha(MAIN)}, expect_inputs=own
                )
            assert refused.value.error_code == "INVALID_REQUEST", refused.value.payload
            assert (
                f"Client speaks protocol {spoken}, this worker 16: install the same Peeky version"
                in refused.value.payload["message"]
            )
        assert server.worker.dap is None  # nothing was launched
    finally:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_restore_reaches_no_worker_of_another_protocol(app, capsys, monkeypatch):
    # A protocol 3 worker would take `@before` for a value to write, and knows no
    # `entry_lines`, `restorable` or `target`. Each side refuses the other before launch.
    restore = ["--set-at", "2@1", "x=0", "--set-at", "3@1", "x=@before"]
    asked = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            asked.append(self.path)
            answer = json.dumps({"protocol": 3}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args):
            pass

    old = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=old.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{old.server_port}"
        argv = ["probe", str(app / "main.py"), "--at", "2", *restore, "--worker", url, "--json"]
        code = main(argv)
    finally:
        old.shutdown()
        old.server_close()
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result["refused_before_launch"] is True, result
    assert "Worker speaks protocol 3, this client 16" in result["error"]["message"]
    assert asked == ["/hello"]  # the probe was never sent
    # A protocol 3 client, against this worker: refused at `version`, and the probe it
    # would send next (no `protocol` 6) is refused without launching.
    monkeypatch.setattr("peeky.cli.PROTOCOL_VERSION", 3)
    server = make_server(app, TOKEN, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        argv = ["probe", str(app / "main.py"), "--at", "2", *restore, "--worker", url, "--json"]
        code = main(argv)
        result = json.loads(capsys.readouterr().out)
        assert code == 2 and result["refused_before_launch"] is True, result
        assert "Worker speaks protocol 16, this client 3" in result["error"]["message"]
        with pytest.raises(PeekyError) as refused:
            Client(url, TOKEN).call(
                "probe",
                at="main.py:2",
                set_at=[[2, 1, "x", "0"], [3, 1, "x", "@before"]],
                expect_sha256={"program": sha(MAIN)},
                expect_inputs=dict(sources.python_sources(app)),
                protocol=3,
            )
        assert refused.value.error_code == "INVALID_REQUEST", refused.value.payload
        assert "Client speaks protocol 3, this worker 16" in refused.value.payload["message"]
        assert server.worker.dap is None  # nothing was launched
    finally:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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


def test_a_result_without_the_sources_it_read_is_discarded(app, capsys, monkeypatch):
    # A missing inputs_sha256 was accepted.
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
    # `expect_inputs: {}` passed the guard, and /probe then compared nothing.
    arguments = {"program": "main.py", "breakpoints": [{"line": 2}]}
    if operation == "probe":
        arguments = {"at": "main.py:2", "capture": ["x"]}
    with pytest.raises(PeekyError) as refused:
        served.call(
            operation,
            **arguments,
            expect_sha256={"program": sha(MAIN)},
            expect_inputs={},
            protocol=PROTOCOL_VERSION,
        )
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
    # The worker hashed the live sources, then built later; a change in
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
    # A resumed remote session skipped the handshake.
    write_record("legacy")
    assert main(["session", "get", "x", "--name", "legacy", "--json"]) == 2
    message = json.loads(capsys.readouterr().out)["error"]["message"]
    assert "started without source provenance" in message and "start the session again" in message


def test_a_remote_session_from_another_protocol_is_refused(app, capsys):
    write_record("previous", protocol=2, inputs_checked="0" * 64)
    assert main(["session", "get", "x", "--name", "previous", "--json"]) == 2
    message = json.loads(capsys.readouterr().out)["error"]["message"]
    assert "was started with protocol 2, this client 16: start the session again" in message
    assert "peeky session close --forget --name previous" in message


@pytest.mark.parametrize("protocol", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15])
def test_a_remote_session_checks_the_workers_protocol_on_every_command(app, capsys, protocol):
    server = old_worker(protocol)
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
    assert f"Worker speaks protocol {protocol}, this session 16" in message


def test_the_program_must_be_among_the_compared_sources(app):
    # A program in a virtual environment was never compared, but ran. Now it is compared
    # by its own hash (expect_sha256), and refused without one.
    (app / "tools").mkdir()
    (app / "tools" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (app / "tools" / "run.py").write_text("x = 1\nprint(x)\n")
    worker = DebugWorker(app)
    try:
        with pytest.raises(
            ValueError,
            match=r"the program tools/run.py \(not among the compared sources: it is in a "
            r"virtual environment, and your client sent no hash of it\)",
        ):
            worker.dispatch(
                "launch",
                {
                    "program": "tools/run.py",
                    "breakpoints": [{"line": 2}],
                    "expect_inputs": dict(sources.python_sources(app)),
                },
            )
        assert worker.dap is None
    finally:
        worker.close()


def test_an_installed_file_compared_by_its_hash_is_checked_again(app):
    # The installed --at file is in no python_sources walk: hashed on its own at each check.
    (app / ".venv" / "lib").mkdir(parents=True)
    (app / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (app / ".venv" / "lib" / "pkg.py").write_text("x = 1\n")
    worker = DebugWorker(app)
    worker.dap = object()
    worker.installed_sources = {".venv/lib/pkg.py": sha("x = 1\n")}
    worker.accepted_sources = {**dict(sources.python_sources(app)), **worker.installed_sources}
    worker.check_sources()
    frames = {"stackFrames": [{"source": {"path": str(app / ".venv" / "lib" / "pkg.py")}}]}
    worker.check_frames(frames)
    (app / ".venv" / "lib" / "pkg.py").write_text("x = 2\n")
    with pytest.raises(RuntimeError, match=r"\.venv/lib/pkg\.py changed on the worker host"):
        worker.check_sources()
    (app / ".venv" / "lib" / "pkg.py").unlink()
    with pytest.raises(RuntimeError, match=r"\.venv/lib/pkg\.py changed on the worker host"):
        worker.check_sources()


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
    # Only Python results were checked against a fresh hash of your checkout.
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


@pytest.mark.parametrize("protocol", [4, 5, 6, 7, 8, 9])
def test_a_worker_that_reads_through_c_pointers_unmarked_is_refused(
    tmp_path, monkeypatch, capsys, protocol
):
    # A protocol 4 worker reads `end[0]`, `pend.x` and LLDB's text one past an array's
    # end as values; a protocol 5 worker shows what any other C pointer points at
    # (a freed block, a call that returned) with no mark; a protocol 6 worker doesn't
    # say which pointers point at a pointer, so `pp[0].uses` would be tied to `pp`'s alias.
    # This client refuses each, for a probe and a session, before either runs.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", TOKEN)
    (tmp_path / "ends.c").write_text("int main(void) {\n  long q[4] = {0};\n  return 0;\n}\n")
    asked = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            asked.append(self.path)
            hello = {"protocol": protocol, "sources": {"kind": "declared", "names": ["ends.c"]}}
            answer = json.dumps(hello).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args):
            pass

    old = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=old.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{old.server_port}"
    target = str(tmp_path / "ends.c")
    outputs = []
    try:
        for argv in (
            ["probe", target, "--at", "3", "--capture", "q", "--worker", url, "--json"],
            ["session", "start", target, "--at", "3", "--worker", url, "--json"],
        ):
            code = main(argv)
            outputs.append((code, json.loads(capsys.readouterr().out)))
    finally:
        old.shutdown()
        old.server_close()
    for code, result in outputs:
        assert code == 2, result
        assert f"Worker speaks protocol {protocol}, this client 16" in result["error"]["message"]
    assert outputs[0][1]["refused_before_launch"] is True
    assert asked == ["/hello", "/hello"]  # nothing was launched


def fake_worker(protocol, delay=0.0, lost=()):
    """A worker that states ``protocol`` in its `hello`, with the proof that it holds
    TOKEN (after ``delay`` s), and answers every other request; it drops the connection,
    answering nothing, for ``lost``. Its `asked` lists the paths it was sent."""
    import time

    from worker_hello import hello_answer

    asked = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            asked.append(self.path)
            if self.path == "/hello":
                time.sleep(delay)
            if self.path.removeprefix("/") in lost:
                self.close_connection = True
                self.connection.shutdown(2)
                return
            answer = json.dumps({"protocol": protocol, "state": "stopped"}).encode()
            if self.path == "/hello":
                answer = hello_answer(body, TOKEN, protocol)
            self.send_response(200)
            self.send_header("X-Peeky-Protocol", str(protocol))
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        # Room for every concurrent connection a test opens (the default backlog is 5).
        request_queue_size = 64

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.asked = asked
    return server


def test_concurrent_requests_say_hello_once_and_each_decides_on_it():
    # The first ask and the protocol it caches are under the client's lock:
    # eight threads asked the worker's `version` eight times, each deciding on its own. Its
    # `hello` is asked once now.
    server = fake_worker(PROTOCOL_VERSION, delay=0.2)
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", TOKEN)
        answers, errors = [], []

        def inspect():
            try:
                answers.append(client.call("inspect"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=inspect) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert errors == [] and len(answers) == 8, errors
        assert sorted(server.asked) == ["/hello"] + ["/inspect"] * 8, server.asked
    finally:
        server.shutdown()
        server.server_close()


def test_a_lost_connection_forgets_the_workers_protocol():
    # After a lost connection, another worker may answer at that address:
    # the next request says `hello` again, and a worker of another protocol is sent
    # nothing else.
    from peeky.client import TransportLost, WorkerUnreachable

    first = fake_worker(PROTOCOL_VERSION, lost=("step",))
    client = Client(f"http://127.0.0.1:{first.server_port}", TOKEN)
    try:
        assert client.call("inspect")["state"] == "stopped" and client.protocol == PROTOCOL_VERSION
        with pytest.raises(TransportLost):
            client.call("step")
        assert client.protocol is None
        assert client.call("inspect")["state"] == "stopped"
        assert first.asked == ["/hello", "/inspect", "/step", "/hello", "/inspect"]
    finally:
        first.shutdown()
        first.server_close()
    with pytest.raises(WorkerUnreachable):
        client.call("continue")
    assert client.protocol is None
    # A protocol 9 worker now answers there.
    second = fake_worker(9)
    try:
        client.url = f"http://127.0.0.1:{second.server_port}"
        with pytest.raises(ValueError, match="^Worker speaks protocol 9, this client 16"):
            client.call("continue")
        assert second.asked == ["/hello"]
    finally:
        second.shutdown()
        second.server_close()


@pytest.mark.parametrize("header", [None, "9", "eleven"])
def test_this_worker_refuses_a_request_of_another_protocol_before_it_runs_it(app, header):
    # Each request names its client's protocol (`X-Peeky-Protocol`). From
    # protocol 10, a worker refuses one that names another, or none, before dispatch: a
    # client of another protocol never runs anything, whatever it cached.
    import http.client

    server = make_server(app, TOKEN, 0)
    ran = []
    server.worker.dispatch = lambda operation, args: ran.append(operation) or {}
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(operation, header):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        if header is not None:
            headers["X-Peeky-Protocol"] = header
        connection.request("POST", f"/{operation}", body=b"{}", headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    # Only a number is quoted back: no error echoes a header's text.
    shown = {None: "none", "9": "9", "eleven": "unknown"}[header]
    try:
        for operation in ("continue", "step", "set_variable", "close", "probe"):
            status, body = post(operation, header)
            assert status == 400 and body["error"]["code"] == "INVALID_REQUEST", body
            assert body["error"]["message"].startswith(
                f"Client speaks protocol {shown} (its X-Peeky-Protocol header), "
                "this worker 16: install the same Peeky version on both hosts"
            ), body
        # `version` is answered whatever the client speaks; this client's requests run.
        assert post("version", header)[1]["protocol"] == PROTOCOL_VERSION
        assert ran == []
        assert post("inspect", str(PROTOCOL_VERSION))[0] == 200 and ran == ["inspect"]
    finally:
        server.shutdown()
        server.server_close()


def test_every_client_request_names_its_protocol(app):
    # The SDK's, a session's and a remote probe's requests all go through Client._request.
    server = fake_worker(PROTOCOL_VERSION)
    seen = []
    handler = server.RequestHandlerClass
    original = handler.do_POST

    def do_post(self):
        seen.append((self.path, self.headers.get("X-Peeky-Protocol")))
        original(self)

    handler.do_POST = do_post
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", TOKEN)
        client.call("inspect")
        client.probe("main.py:2", capture=["x"])
        assert seen == [
            ("/hello", str(PROTOCOL_VERSION)),
            ("/inspect", str(PROTOCOL_VERSION)),
            ("/probe", str(PROTOCOL_VERSION)),
        ], seen
    finally:
        server.shutdown()
        server.server_close()
