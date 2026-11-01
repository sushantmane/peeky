"""A worker's limits on what an unauthenticated connection can hold (security review before
going public).

Each read of a request waited at most 10 s, but a client that sent a byte every few
seconds held a worker thread for as long as it liked, and any local process could open
as many such connections as it liked. A request must now arrive whole within its
deadline, and the worker serves at most a fixed number of connections at once.
"""

import socket
import threading
import time

import pytest

from peeky import worker as worker_module
from peeky.client import Client, TransportLost
from peeky.errors import PROTOCOL_VERSION, PeekyError
from peeky.worker import make_server

TOKEN = "limits-test-token-1234567890-0123456789abcdef"


@pytest.fixture
def served(tmp_path):
    def serve():
        server = make_server(tmp_path, TOKEN, 0)
        ran = []
        server.worker.dispatch = lambda operation, args: ran.append(operation) or {}
        server.ran = ran
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    servers = []
    yield serve
    for server in servers:
        server.shutdown()
        server.server_close()
        server.worker.close()


def trickle(connection, data, every=0.2):
    """Send ``data`` a byte at a time, each well within the 10 s read timeout."""
    for byte in data:
        try:
            connection.sendall(bytes([byte]))
        except OSError:
            return
        time.sleep(every)


SLOW = [
    (b"", b"POST /version HTTP/1.0\r\nX-Slow: " + b"a" * 100),
    (
        b"POST /inspect HTTP/1.0\r\nHost: 127.0.0.1\r\nAuthorization: Bearer "
        + TOKEN.encode()
        + f"\r\nX-Peeky-Protocol: {PROTOCOL_VERSION}\r\nContent-Length: 200\r\n\r\n".encode(),
        b"{" + b" " * 198 + b"}",
    ),
]


@pytest.mark.parametrize("head, slow", SLOW, ids=["headers", "body"])
def test_a_request_sent_a_byte_at_a_time_is_closed_at_its_deadline(served, monkeypatch, head, slow):
    monkeypatch.setattr(worker_module, "REQUEST_DEADLINE_S", 1, raising=False)
    server = served()
    with socket.create_connection(("127.0.0.1", server.server_port)) as connection:
        connection.sendall(head)
        sender = threading.Thread(target=trickle, args=(connection, slow), daemon=True)
        started = time.monotonic()
        sender.start()
        connection.settimeout(8)
        try:
            answer = connection.recv(1024)
        except TimeoutError:
            answer = None
        assert answer == b"", "the worker kept reading a request past its deadline"
        assert time.monotonic() - started < 4
    # Nothing of it ran, and the worker still serves others.
    assert server.ran == []
    assert Client(f"http://127.0.0.1:{server.server_port}", TOKEN).call("inspect") == {}
    assert server.ran == ["inspect"]


def taken(slots, within=10):
    """Wait until the worker has accepted connections into all of ``slots`` (read only)."""
    deadline = time.monotonic() + within
    while slots._value:
        assert time.monotonic() < deadline, "the worker never accepted the connections"
        time.sleep(0.02)


def released(slots, count, within=10):
    """Wait until the worker has released all ``count`` of ``slots`` (read only). A connection
    holds its slot until its thread ends, a little after its client has read the answer."""
    deadline = time.monotonic() + within
    while slots._value < count:
        assert time.monotonic() < deadline, "the worker never released its connections"
        time.sleep(0.02)


@pytest.mark.parametrize("lagging", [False, True], ids=["prompt", "lagging"])
def test_a_connection_past_the_cap_is_answered_busy_and_runs_nothing(served, monkeypatch, lagging):
    # Re-review: one closed unread read to a client as a lost connection, whose request's
    # outcome is unknown, so a session's change couldn't be retried safely.
    monkeypatch.setattr(worker_module, "MAX_CONNECTIONS", 2, raising=False)
    monkeypatch.setattr(worker_module, "SPARE_CONNECTIONS", 1, raising=False)
    if lagging:
        # As under load: a connection's thread ends, and frees its slot, well after its
        # client read the answer. The connections held below must not race it (#457).
        shut = worker_module._Server.shutdown_request

        def late(self, request):
            shut(self, request)
            time.sleep(0.3)

        monkeypatch.setattr(worker_module._Server, "shutdown_request", late)
    server = served()
    address = ("127.0.0.1", server.server_port)
    client = Client(f"http://127.0.0.1:{server.server_port}", TOKEN)
    assert client.call("inspect") == {}  # its hello is done
    # The hello's and inspect's connections free their slots once their threads end.
    released(server.slots, 2)
    held = [socket.create_connection(address) for _ in range(2)]
    try:
        taken(server.slots)
        assert server.spares._value == 1, "a held connection was taken past the cap"
        with pytest.raises(PeekyError) as busy:
            client.call("continue", session_id="s", stop_id="t")
        assert busy.value.code == 503 and busy.value.error_code == "BUSY", busy.value.payload
        assert busy.value.retryable is True and busy.value.session_closed is False
        assert "was not run" in busy.value.payload["message"]
        assert server.ran == ["inspect"]
        # Past the spare connections too: closed unread. The BUSY answer's connection frees
        # its spare first.
        released(server.spares, 1)
        with socket.create_connection(address) as spare:
            spare.sendall(b"POST /hello HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n")
            taken(server.spares)
            with socket.create_connection(address) as extra:
                extra.settimeout(5)
                try:
                    answer = extra.recv(1024)
                except TimeoutError:
                    answer = None
                assert answer == b"", "the worker took a connection past its spares"
    finally:
        for connection in held:
            connection.close()
    # Once those are closed, it serves again.
    deadline = time.monotonic() + 5
    while True:
        try:
            assert client.call("inspect") == {}
            break
        except (PeekyError, TransportLost):
            assert time.monotonic() < deadline
            time.sleep(0.1)
    assert server.ran == ["inspect", "inspect"]


def test_the_limits():
    assert worker_module.REQUEST_READ_TIMEOUT_S == 10
    assert worker_module.REQUEST_DEADLINE_S == 30
    assert worker_module.MAX_CONNECTIONS == 32
    assert (worker_module.SPARE_CONNECTIONS, worker_module.BUSY_DEADLINE_S) == (16, 2)
