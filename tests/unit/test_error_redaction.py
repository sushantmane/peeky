"""Where a worker's token can and can't appear in its replies (release readiness review).

A worker's 400 and 500 replies carry the text of the exception that ended the request
(``classify``). No process that could print the token is given it, so a reply can hold
the token only where the client put it in a request. The worker never adds it to a reply,
and no reply or log echoes a request's headers. As a second line of defence, the worker
removes its token from the error text it builds, before its own cuts: a build's output, a
probe child's errors and stderr, and the message. Text quoted from the debugger or a
request is passed as sent, and may be cut first, so a part of a token there can remain.
Request fields, a program's output and values are sent as they are.
"""

import json
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from peeky import worker as worker_module
from peeky.client import Client
from peeky.errors import PROTOCOL_VERSION, PeekyError
from peeky.worker import make_server

TOKEN = "redaction-test-token-1234567890-0123456789abcdef"


@pytest.fixture
def served(tmp_path):
    servers = []

    def serve(dispatch):
        server = make_server(tmp_path, TOKEN, 0)
        server.worker.dispatch = dispatch
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server, Client(f"http://127.0.0.1:{server.server_port}", TOKEN)

    yield serve
    for server in servers:
        server.shutdown()
        server.server_close()
        server.worker.close()


def refused(client, operation="inspect", **arguments):
    with pytest.raises(PeekyError) as caught:
        client.call(operation, **arguments)
    return caught.value, caught.value.read().decode()


@pytest.mark.parametrize(
    "exc, status",
    [
        (RuntimeError(f"the adapter quoted {TOKEN} back"), 500),
        (TimeoutError(f"no answer to evaluate {TOKEN!r}"), 500),
        (LookupError(f"an unexpected failure with {TOKEN}"), 500),
        (ValueError(f"no variable named {TOKEN}"), 400),
        (KeyError(TOKEN), 400),
    ],
)
def test_an_error_whose_text_holds_the_token_is_sent_without_it(served, exc, status):
    def dispatch(operation, args):
        raise exc

    _, client = served(dispatch)
    error, raw = refused(client)
    assert error.code == status
    assert TOKEN not in raw and TOKEN not in json.dumps(error.payload)
    assert "[token removed]" in error.payload["message"]


def test_the_token_is_removed_before_a_long_message_is_cut(served):
    # Cut first, the message would keep the token's start.
    def dispatch(operation, args):
        raise RuntimeError("x" * 1990 + TOKEN)

    _, client = served(dispatch)
    error, raw = refused(client)
    assert error.code == 500 and len(error.payload["message"]) <= 2000
    assert TOKEN[:10] not in raw


def test_a_failed_probes_error_is_sent_without_the_token(served, monkeypatch):
    def run(root, request, adapter, cap, **kwargs):
        assert kwargs == {"secret": TOKEN}
        return {
            "status": "failed",
            "error": {"code": "DEBUGGER_ERROR", "message": f"stderr: {TOKEN}", "hint": TOKEN},
            "captured": {"name": {"value": "unchanged"}},
        }

    monkeypatch.setattr(worker_module, "_run_probe_process", run)
    _, client = served(lambda operation, args: {})
    result = client.call("probe", at="main.py:1")
    assert TOKEN not in json.dumps(result)
    assert result["error"]["message"] == "stderr: [token removed]"
    assert result["captured"] == {"name": {"value": "unchanged"}}


def test_a_result_is_sent_as_it_is(served):
    # Only an error's text is changed: a value never is, even one equal to the token.
    _, client = served(lambda operation, args: {"variables": {"secret": {"value": TOKEN}}})
    assert client.call("inspect") == {"variables": {"secret": {"value": TOKEN}}}


def test_no_reply_echoes_a_requests_headers(served):
    # Before the token is checked, and after: a wrong token, Host or protocol header.
    server, _ = served(lambda operation, args: {})
    guess = "guessed-token-abcdefghijklmnopqrstuvwxyz-0123456789"
    port = server.server_port
    requests = [
        f"POST /inspect HTTP/1.0\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {guess}\r\n"
        f"X-Peeky-Protocol: {PROTOCOL_VERSION}\r\nContent-Length: 2\r\n\r\n{{}}",
        f"POST /inspect HTTP/1.0\r\nHost: evil.example\r\nAuthorization: Bearer {guess}\r\n"
        "Content-Length: 2\r\n\r\n{}",
        f"POST /hello HTTP/1.0\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {guess}\r\n"
        "Content-Length: 2\r\n\r\n{}",
        f"POST /inspect HTTP/1.0\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {TOKEN}\r\n"
        "X-Peeky-Protocol: 1\r\nContent-Length: 2\r\n\r\n{}",
        f"POST /inspect HTTP/1.0\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {TOKEN}\r\n"
        f"X-Peeky-Protocol: {TOKEN}\r\nContent-Length: 2\r\n\r\n{{}}",
        f"POST /inspect HTTP/1.0\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {TOKEN}\r\n"
        f"X-Peeky-Protocol: {PROTOCOL_VERSION}\r\nContent-Length: {guess}\r\n\r\n{{}}",
    ]
    for request in requests:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.sendall(request.encode())
            answer = b""
            while chunk := connection.recv(65536):
                answer += chunk
        assert answer.startswith(b"HTTP/1.0 4"), answer[:200]
        assert guess.encode() not in answer and TOKEN[:16].encode() not in answer, answer


def test_redact_removes_each_quoted_form():
    from peeky.errors import REDACTED_TOKEN, redact

    secret = 'quote\'d "and" back\\slash-0123456789abcdef'
    text = f"{secret} {secret!r} {json.dumps(secret)}"
    assert secret not in redact(text, secret)
    assert redact(text, secret).count(REDACTED_TOKEN) == 3
    assert redact({"a": [secret, 1, None]}, secret) == {"a": [REDACTED_TOKEN, 1, None]}
    assert redact(text, None) == text


# A token whose plain, Python-quoted and JSON-quoted forms all differ.
ODD = 'odd\\token "quoted"-0123456789abcdefghijklmnop'
FORMS = {"plain": ODD, "repr": repr(ODD)[1:-1], "json": json.dumps(ODD)[1:-1]}


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("stream", ["stderr", "stdout"])
def test_a_build_failures_output_loses_the_token_before_it_is_cut(form, stream):
    # The compiler's words are cut at 1,500 characters; cut first, the token's start stays.
    from peeky.build import BuildCommandError
    from peeky.errors import classify

    text = FORMS[form]
    assert len({*FORMS.values()}) == 3
    exc = BuildCommandError(["/usr/bin/cc"], {"exit_code": 1, stream: "x" * 1490 + text})
    error = classify(exc, secret=ODD)
    assert error["code"] == "BUILD_FAILED" and "output truncated" in error["message"]
    assert text[:10] not in error["message"], error["message"][-80:]
    assert "x" * 1490 + "[token rem" in error["message"]


@pytest.mark.parametrize("form", FORMS)
def test_a_probe_childs_stderr_loses_the_token_before_its_tail_is_cut(tmp_path, monkeypatch, form):
    # The worker keeps the last 2,000 characters; cut first, the token's end stays. The
    # child itself never gets the token: not in its command, environment or files.
    text = FORMS[form]
    started = []

    class Child:
        pid = 0

        def __init__(self, command, env=None, cwd=None, **kwargs):
            files = [path.read_bytes() for path in Path(cwd).rglob("*") if path.is_file()]
            started.append((command, env, files))

        def communicate(self, timeout=None):
            return b"not JSON", (text + "y" * 1980).encode()

    monkeypatch.setattr(subprocess, "Popen", Child)
    server = make_server(tmp_path, ODD, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", ODD)
        result = client.call("probe", at="main.py:1", edit={"old": "x", "new": "y"})
    finally:
        server.shutdown()
        server.server_close()
        server.worker.close()
    message = result["error"]["message"]
    assert result["status"] == "failed" and text[-10:] not in message, message[:80]
    assert message == "[token removed]" + "y" * 1980
    ((command, env, files),) = started
    assert files and ODD not in " ".join(command) and ODD not in json.dumps(env)
    assert not any(ODD.encode() in data or ODD[:10].encode() in data for data in files)


@pytest.mark.parametrize("form", FORMS)
def test_a_probe_childs_own_error_loses_the_token_before_it_is_cut(tmp_path, monkeypatch, form):
    # The child classifies its failure itself, without the token. It leaves the text long
    # (--relay-errors), and the worker removes the token before it cuts it: cut in the
    # child, the token's start would stay.
    import contextlib
    import io

    from peeky import cli
    from peeky.build import BuildCommandError
    from peeky.probe import Probe

    text = FORMS[form]
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "app"
    root.mkdir()
    (root / "main.py").write_text("x = 1\n")

    def run_probe(*args, **kwargs):
        raise BuildCommandError(("cc",), {"exit_code": 1, "stderr": "x" * 1490 + text})

    monkeypatch.setattr(cli, "run_probe", run_probe)
    started, child = [], None

    class Child:
        # The child's own code, in this process: `peeky probe` with the worker's arguments.
        pid = 0

        def __init__(self, command, env=None, **kwargs):
            nonlocal child
            child = self
            started.append((command, env))
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                cli.main(command[command.index("probe") :])
            self.stdout = out.getvalue().encode()

        def communicate(self, timeout=None):
            return self.stdout, b""

    monkeypatch.setattr(subprocess, "Popen", Child)
    result = worker_module._run_probe_process(root, Probe("main.py", 1), None, 60, secret=ODD)
    message = result["error"]["message"]
    assert result["error"]["code"] == "BUILD_FAILED", result
    assert message.endswith("\n... (output truncated)") and text[:10] not in message, message[-80:]
    assert "x" * 1490 + "[token rem" in message
    ((command, env),) = started
    assert ODD not in " ".join(command) and ODD not in json.dumps(env)
    # The child sent the whole output, the token in it uncut.
    assert text in json.loads(child.stdout)["error"]["message"]


@pytest.mark.parametrize("form", FORMS)
def test_bound_removes_the_token_then_cuts_as_classify_does(form):
    from peeky.errors import RELAYED_CHARS, bound, classify, relayed

    text = FORMS[form]
    exc = RuntimeError("m" * 1990 + text)
    exc.cleanup = "c" * 290 + text
    with relayed():
        long = classify(exc)
    assert len(long["message"]) == 1990 + len(text) < RELAYED_CHARS
    sent = bound(long, ODD)
    assert sent == classify(exc, secret=ODD)
    assert sent["message"] == ("m" * 1990 + "[token removed]")[:2000]
    assert sent["cleanup"] == ("c" * 290 + "[token removed]")[:300]
    # Outside it, classify cuts as before.
    assert len(classify(RuntimeError("m" * 3000))["message"]) == 2000


def test_a_token_over_512_characters_is_refused_at_start(tmp_path, monkeypatch, capsys):
    # A probe child's errors keep RELAYED_CHARS of text: far beyond any token's quoted form.
    import sys

    from peeky.client import client_token

    long = "t" * 513
    with pytest.raises(ValueError, match="32 to 512 characters"):
        make_server(tmp_path, long, 0)
    make_server(tmp_path, "t" * 512, 0).server_close()
    with pytest.raises(ValueError, match="32 to 512 characters"):
        client_token({"PEEKY_WORKER_TOKEN": long})
    monkeypatch.setenv("DEBUG_TOKEN", long)
    monkeypatch.setattr(sys, "argv", ["peeky-worker", "--port", "0"])
    monkeypatch.setattr(worker_module, "make_server", lambda *a, **k: pytest.fail("started"))
    with pytest.raises(SystemExit) as stopped:
        worker_module.main()
    assert stopped.value.code == 2 and "32 to 512 characters" in capsys.readouterr().err
