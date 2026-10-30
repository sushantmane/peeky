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
from peeky.errors import PROTOCOL_VERSION
from peeky.worker import make_server

TOKEN = "limits-test-token-1234567890"


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
        b"POST /inspect HTTP/1.0\r\nAuthorization: Bearer "
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


def test_connections_past_the_cap_are_closed_unread(served, monkeypatch):
    monkeypatch.setattr(worker_module, "MAX_CONNECTIONS", 2, raising=False)
    server = served()
    address = ("127.0.0.1", server.server_port)
    held = [socket.create_connection(address) for _ in range(2)]
    try:
        time.sleep(0.3)
        with socket.create_connection(address) as extra:
            extra.settimeout(5)
            started = time.monotonic()
            try:
                answer = extra.recv(1024)
            except TimeoutError:
                answer = None
            assert answer == b"", "the worker took a connection past its cap"
            assert time.monotonic() - started < 2
    finally:
        for connection in held:
            connection.close()
    # Once those are closed, it serves again.
    deadline = time.monotonic() + 5
    while True:
        try:
            assert Client(f"http://127.0.0.1:{server.server_port}", TOKEN).call("inspect") == {}
            break
        except TransportLost:
            assert time.monotonic() < deadline
            time.sleep(0.1)


def test_the_limits():
    assert worker_module.REQUEST_READ_TIMEOUT_S == 10
    assert worker_module.REQUEST_DEADLINE_S == 30
    assert worker_module.MAX_CONNECTIONS == 32
