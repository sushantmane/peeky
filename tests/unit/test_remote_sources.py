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
    assert sources.difference({"x.c": a}, {"x.c": None}) == "x.c (missing on the worker host)"
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


def test_hidden_cache_and_environment_directories_are_not_sources(app):
    for directory in (".git", "__pycache__", "venv", "env", "site-packages", "node_modules"):
        (app / directory).mkdir()
        (app / directory / "mod.py").write_text("z = 3\n")
    (app / "pkg").mkdir()
    (app / "pkg" / "mod.py").write_text("z = 3\n")
    assert {name for name, _ in sources.python_sources(app)} == {
        "helper.py",
        "main.py",
        "pkg/mod.py",
    }


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
        "page.h (missing on the worker host)"
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
    with pytest.raises(ValueError, match="at most 4096"):
        sources.check_expected({f"m{n}.py": "0" * 64 for n in range(4097)})
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
