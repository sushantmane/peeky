"""A worker proves it holds the token before a client sends it (security review before going
public; protocol 11).

A client sent its token with every request, its first one too. On a shared host, once the
worker stopped, another user's process could take its port, and get the token from the
client's next request: a session's later command, or a remote probe through the tunnel.
A client now says `hello` first, with a fresh nonce and no token. Only a worker holding
the token can answer with the proof, which the client checks before it sends anything else.
"""

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest

from peeky.cli import main
from peeky.client import Client
from peeky.errors import PROTOCOL_VERSION, classify
from peeky.worker import make_server

TOKEN = "hello-test-token-1234567890-0123456789abcdef"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", TOKEN)
    root = tmp_path / "app"
    root.mkdir()
    (root / "main.py").write_text("x = 1\nprint(x)\n")
    return root


class Impostor:
    """A process on the worker's port, without the token: it records every request, and
    answers each as ``answer(path, headers)`` says (status, headers, body)."""

    def __init__(self, answer):
        self.requests = []
        impostor = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                impostor.requests.append((self.path, dict(self.headers), body))
                status, headers, answer_body = answer(self.path, self.headers)
                encoded = json.dumps(answer_body).encode()
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def saw_token(self):
        return any(
            "Authorization" in headers or TOKEN.encode() in body or TOKEN in path
            for path, headers, body in self.requests
        )

    def refusal(self, operation, **arguments):
        """What a client's ``operation`` raised here; first, that the token never came."""
        try:
            Client(self.url, TOKEN).call(operation, **arguments)
        except Exception as error:  # noqa: BLE001
            refused = error
        else:
            refused = None
        assert not self.saw_token(), f"the token reached the impostor: {self.requests}"
        assert isinstance(refused, ValueError), refused
        return refused


@pytest.fixture
def worker(app):
    server = make_server(app, TOKEN, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.worker.close()
    server.shutdown()
    server.server_close()


def hello(url, body, headers=None):
    request = Request(
        url + "/hello",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except Exception as error:  # urllib's HTTPError: the worker's answer
        return error.code, json.load(error)


def test_the_worker_answers_hello_with_its_proof_and_nothing_else(worker):
    from peeky.client import hello_proof

    url = f"http://127.0.0.1:{worker.server_port}"
    nonce = secrets.token_bytes(32)
    # No token: the proof is all it says. Not its root, its sources or its version.
    status, answer = hello(url, {"nonce": nonce.hex()})
    assert status == 200 and answer == {
        "protocol": PROTOCOL_VERSION,
        "proof": hello_proof(TOKEN, nonce),
    }
    assert hello_proof("another-token-1234567890-0123456789abcdef", nonce) != answer["proof"]
    for body in ({}, {"nonce": "zz"}, {"nonce": "ab" * 16}, {"nonce": nonce.hex(), "x": 1}):
        status, answer = hello(url, body)
        assert status == 400 and answer["error"]["code"] == "INVALID_REQUEST"
        assert set(answer) == {"error", "protocol"}
    assert worker.worker.dap is None


def test_an_impostor_without_the_token_never_receives_it(app, capsys):
    # Another user's process took the worker's port after the worker stopped. It knows
    # the protocol, and says it, but can't make the proof.
    impostor = Impostor(
        lambda path, headers: (
            200,
            {"X-Peeky-Protocol": str(PROTOCOL_VERSION)},
            {"protocol": PROTOCOL_VERSION, "proof": secrets.token_hex(32)},
        )
    )
    try:
        refused = impostor.refusal("version")
        assert "did not prove it holds the worker's token" in str(refused)
        assert classify(refused)["code"] == "UNAUTHORIZED"
        argv = ["probe", str(app / "main.py"), "--at", "1", "--worker", impostor.url, "--json"]
        assert main(argv) == 2
        result = json.loads(capsys.readouterr().out)
        assert result["error"]["code"] == "UNAUTHORIZED", result
        assert result["refused_before_launch"] is True
        # Only hellos came, each with a fresh nonce, and none carried the token.
        assert [path for path, _, _ in impostor.requests] == ["/hello", "/hello"]
        nonces = [json.loads(body)["nonce"] for _, _, body in impostor.requests]
        assert len(set(nonces)) == 2 and not impostor.saw_token()
    finally:
        impostor.close()


def test_an_impostor_that_asks_for_the_token_never_receives_it(app):
    # As a worker of protocol 10 does: 401, until the token comes.
    impostor = Impostor(
        lambda path, headers: (
            401,
            {"X-Peeky-Protocol": str(PROTOCOL_VERSION)},
            {"error": {"code": "UNAUTHORIZED", "message": "token?"}, "protocol": PROTOCOL_VERSION},
        )
    )
    try:
        refused = impostor.refusal("launch", program="main.py")
        assert "did not prove it holds the worker's token" in str(refused)
        assert [path for path, _, _ in impostor.requests] == ["/hello"]
        assert not impostor.saw_token()
    finally:
        impostor.close()


def test_a_worker_with_another_token_is_refused_before_any_request(app, capsys):
    server = make_server(app, "another-token-1234567890-0123456789abcdef", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(ValueError, match="did not prove it holds the worker's token"):
            Client(url, TOKEN).call("launch", program="main.py", breakpoints=[{"line": 1}])
        argv = ["probe", str(app / "main.py"), "--at", "1", "--worker", url, "--json"]
        assert main(argv) == 2
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "UNAUTHORIZED"
        assert server.worker.dap is None  # nothing was launched
    finally:
        server.worker.close()
        server.shutdown()
        server.server_close()


def test_a_protocol_10_worker_is_refused_before_any_request(app, capsys):
    # A protocol 10 worker has no `hello`: without the token it answers 401, and says its
    # protocol in its header.
    impostor = Impostor(
        lambda path, headers: (
            401,
            {"X-Peeky-Protocol": "10"},
            {"error": {"code": "UNAUTHORIZED", "message": "Missing or wrong bearer token"}},
        )
    )
    try:
        message = str(impostor.refusal("version"))
        assert message.startswith(
            f"Worker speaks protocol 10, this client {PROTOCOL_VERSION}: install the same Peeky "
            "version on both hosts"
        )
        argv = ["probe", str(app / "main.py"), "--at", "1", "--worker", impostor.url, "--json"]
        assert main(argv) == 2
        result = json.loads(capsys.readouterr().out)
        assert (
            f"Worker speaks protocol 10, this client {PROTOCOL_VERSION}"
            in (result["error"]["message"])
        )
        assert [path for path, _, _ in impostor.requests] == ["/hello", "/hello"]
        assert not impostor.saw_token()
    finally:
        impostor.close()


def post(port, path, headers, body=b"{}"):
    """A request with exactly ``headers`` (Host too, or none)."""
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
    for name, value in {**headers, "Content-Length": str(len(body))}.items():
        connection.putheader(name, value)
    connection.endheaders(body)
    response = connection.getresponse()
    return response.status, json.loads(response.read())


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "attacker.example:8765"},
        {"Host": "localhost:8765"},
        {},
        {"Host": "127.0.0.1", "Origin": "http://attacker.example"},
        {"Host": "127.0.0.1", "Origin": "null"},
    ],
    ids=["rebound-host", "localhost", "no-host", "origin", "null-origin"],
)
def test_a_browsers_request_is_refused_before_hello_or_the_token(worker, headers):
    # Re-review: a page whose name a DNS rebinding pointed at 127.0.0.1 could read `hello`
    # (to test token guesses offline) and, with a guessed token, call every operation.
    # A browser always sends its page's Host, and Origin with a POST.
    port = worker.server_port
    nonce = json.dumps({"nonce": secrets.token_hex(32)}).encode()
    authorized = {"Authorization": f"Bearer {TOKEN}", "X-Peeky-Protocol": str(PROTOCOL_VERSION)}
    for path, extra, body in (("/hello", {}, nonce), ("/version", authorized, b"{}")):
        status, answer = post(port, path, {**headers, **extra}, body)
        assert status == 403 and answer["error"]["code"] == "INVALID_REQUEST", answer
        assert "not a browser's" in answer["error"]["message"]
        assert set(answer) == {"error", "protocol"}
    # 127.0.0.1 at any port is a client's: an SSH tunnel's local port may differ.
    for host in ("127.0.0.1", "127.0.0.1:18765", f"127.0.0.1:{port}"):
        status, answer = post(port, "/hello", {"Host": host}, nonce)
        assert status == 200 and set(answer) == {"protocol", "proof"}, (host, answer)
    assert worker.worker.dap is None


def test_a_token_is_at_least_32_characters(tmp_path, monkeypatch, capsys):
    # `hello` lets anyone who can connect test guesses of the token offline.
    import sys

    from peeky import worker as worker_module
    from peeky.cli import _remote

    short = "s" * 31
    with pytest.raises(ValueError, match="at least 32 characters"):
        make_server(tmp_path, short, 0)
    make_server(tmp_path, "s" * 32, 0).server_close()
    monkeypatch.setenv("DEBUG_TOKEN", short)
    monkeypatch.setattr(sys, "argv", ["peeky-worker", "--port", "0"])
    monkeypatch.setattr(worker_module, "make_server", lambda *a, **k: pytest.fail("started"))
    with pytest.raises(SystemExit) as stopped:
        worker_module.main()
    assert stopped.value.code == 2 and "at least 32 characters" in capsys.readouterr().err
    monkeypatch.setenv("PEEKY_WORKER_TOKEN", short)
    with pytest.raises(ValueError, match="at least 32 characters"):
        _remote("http://127.0.0.1:9")
