"""JSON HTTP SDK and CLI, usable by any agent with shell or HTTP tools."""

import argparse
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import socket
import threading
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from peeky.errors import PROTOCOL_VERSION, PeekyError


class WorkerUnreachable(OSError):
    """No connection reached the worker, so the request was not applied."""


class TransportLost(OSError):
    """The connection broke mid-request: the request's outcome is unknown."""


# Worker URLs are loopback (or an SSH tunnel's local end): never route them, or the
# bearer token, through http_proxy/HTTP_PROXY.
_OPENER = build_opener(ProxyHandler({}))
# Each request and answer names its sender's protocol.
PROTOCOL_HEADER = "X-Peeky-Protocol"
# The only worker URLs a client sends the token to, in plain HTTP: this host's loopback,
# where an SSH tunnel's local end is.
LOOPBACK_URL = re.compile(r"http://127\.0\.0\.1:[0-9]{1,5}")
# A token's least length: `hello` lets anyone who can connect test guesses of it offline.
MIN_TOKEN_CHARS = 32
# What a worker's `hello` proof is a MAC of, before the client's nonce: no other message
# of Peeky's starts so.
HELLO_LABEL = b"peeky worker hello v1\x00"


def hello_proof(token, nonce):
    """A worker's answer to a client's `hello` nonce (bytes): HMAC-SHA256, keyed by the
    token, of HELLO_LABEL and the nonce, in hex. Only a holder of the token can make it."""
    return hmac.new(token.encode(), HELLO_LABEL + nonce, hashlib.sha256).hexdigest()


def _stated(error):
    """The protocol a worker's error answer states (its header, else its body's), or
    "unknown"."""
    stated = error.headers.get(PROTOCOL_HEADER) if error.headers is not None else None
    if stated is None:
        try:
            stated = json.loads(error.read()).get("protocol")
        except (AttributeError, OSError, ValueError):
            stated = None
    stated = str(stated).strip()
    return int(stated) if re.fullmatch(r"[0-9]{1,6}", stated) else "unknown"


def _spoken(headers, operation, url):
    """Refuse a worker's answer where the worker says it speaks another protocol (its
    `X-Peeky-Protocol` header, on every answer), however the session started: a local
    session's worker of an older Peeky would ignore what this client sends, such as
    `expand`'s `key`, and answer something else. Not `version`'s or
    `hello`'s answer, whose caller compares the protocol it states and says what to do."""
    spoken = headers.get(PROTOCOL_HEADER) if headers is not None else None
    if (
        operation in ("version", "hello")
        or spoken is None
        or spoken.strip() == str(PROTOCOL_VERSION)
    ):
        return
    raise ValueError(
        f"Worker speaks protocol {spoken.strip()[:20]}, this client {PROTOCOL_VERSION}: its "
        f"answer to {operation!r} at {url} is not read. Run the same Peeky version as the "
        "worker; for a session, close it and start it again"
    )


class Client:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        if not LOOPBACK_URL.fullmatch(self.url):
            raise ValueError(
                f"A worker's URL must be loopback (http://127.0.0.1:PORT), not {self.url[:80]}: "
                "the token goes in plain HTTP. Reach a remote worker through an SSH tunnel"
            )
        self.token = token
        # The protocol the worker's `hello` states, once it proved it holds the token: no
        # request, and not the token, is sent to a worker before that (``_hello``), nor to
        # a worker of another protocol, which would run a step or a change
        # as it reads the request, and ignore what it doesn't know (`expand`'s `key`).
        # Asked and set under ``lock``; forgotten when a connection is lost, since another
        # process may answer at the same address then.
        self.protocol = None
        self.lock = threading.Lock()

    def call(self, operation, **arguments):
        try:
            self._preflight(operation)
            return self._request(operation, arguments)
        except (WorkerUnreachable, TransportLost):
            with self.lock:
                self.protocol = None
            raise

    def _preflight(self, operation):
        """Say `hello` once, before anything else is sent; refuse ``operation`` where the
        worker speaks another protocol, or doesn't say. Decided on the protocol held
        under the lock, which one `hello` at a time sets."""
        with self.lock:
            if self.protocol is None:
                self.protocol = self._hello()
            spoken = self.protocol
        if spoken != PROTOCOL_VERSION:
            raise ValueError(self._refusal(operation, spoken))

    def _hello(self):
        """The worker's protocol, once it has proven it holds this client's token: it
        answers a fresh random nonce with hello_proof, checked before the token is sent.
        A process that took the worker's port after it stopped never gets the token. A
        worker before protocol 11 has no `hello`: it says its protocol as it refuses."""
        nonce = secrets.token_bytes(32)
        try:
            answer = self._request("hello", {"nonce": nonce.hex()}, authorized=False)
        except PeekyError as error:
            if not getattr(error, "from_worker", False) or error.error_code == "BUSY":
                # Not as a worker answers (a tunnel's or a proxy's page), or a worker past
                # its connections, which ran nothing: said as it is.
                raise
            if (spoken := _stated(error)) != PROTOCOL_VERSION:
                return spoken
            answer = None
        found = answer.get("protocol") if isinstance(answer, dict) else None
        if type(found) is int and found != PROTOCOL_VERSION:
            return found
        proof = answer.get("proof") if isinstance(answer, dict) else None
        expected = hello_proof(self.token, nonce).encode()
        if found != PROTOCOL_VERSION or not isinstance(proof, str):
            proof = ""
        if not hmac.compare_digest(proof.encode("utf-8", "replace"), expected):
            raise ValueError(
                f"The process at {self.url} did not prove it holds the worker's token, so "
                "neither the token nor the request was sent: it is another program on that "
                "port, or a worker started with another token"
            )
        return PROTOCOL_VERSION

    def _refusal(self, operation, spoken):
        return (
            f"Worker speaks protocol {spoken}, this client {PROTOCOL_VERSION}: install the "
            f"same Peeky version on both hosts. Only a `hello`, without the token, was sent "
            f"to the worker at {self.url}, not {operation!r}"
        )

    def _request(self, operation, arguments, *, authorized=True):
        """One request, as it is: its answer, or the worker's error. ``authorized``: with
        the token, which only a worker that proved it holds it gets (``_hello``)."""
        headers = {
            "Content-Type": "application/json",
            # A worker of protocol 10 or later refuses any request but `version` that
            # names another protocol, or none, before it runs it.
            PROTOCOL_HEADER: str(PROTOCOL_VERSION),
        }
        if authorized:
            headers["Authorization"] = "Bearer " + self.token
        request = Request(
            self.url + "/" + operation, data=json.dumps(arguments).encode(), headers=headers
        )
        try:
            with _OPENER.open(request, timeout=45) as response:
                _spoken(response.headers, operation, self.url)
                try:
                    return json.load(response)
                except ValueError:
                    # Sent and answered, but not as the worker answers: whatever the
                    # worker did is unknown.
                    raise TransportLost(
                        f"Connection to the worker at {self.url} broke during {operation!r} "
                        "(its answer is not JSON); its outcome is unknown"
                    ) from None
        except HTTPError as exc:
            _spoken(exc.headers, operation, self.url)
            return self._http_error(exc)
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, (ConnectionRefusedError, socket.gaierror, FileNotFoundError)):
                raise WorkerUnreachable(
                    f"Worker at {self.url} is unreachable ({reason}); the request was not sent. "
                    "Check the worker process or SSH tunnel, then retry"
                ) from None
            raise TransportLost(
                f"Connection to the worker at {self.url} broke during {operation!r} ({reason}); "
                "its outcome is unknown"
            ) from None
        except (http.client.HTTPException, ConnectionError, TimeoutError, socket.timeout) as exc:
            raise TransportLost(
                f"Connection to the worker at {self.url} broke during {operation!r} "
                f"({type(exc).__name__}); its outcome is unknown"
            ) from None

    def _http_error(self, exc):
        raw = exc.read() or b"{}"
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = PeekyError(exc.url, exc.code, exc.headers, body["error"], raw)
            # The worker's own refusal, as it answers one; any other body may be a
            # tunnel's or a proxy's, after which the request's outcome is unknown.
            error.from_worker = True
            raise error from None
        raise PeekyError(
            exc.url,
            exc.code,
            exc.headers,
            {
                "code": "INVALID_REQUEST",
                "message": raw.decode(errors="replace")[:2000],
                "hint": "The worker returned an unexpected error body; check its log.",
                "retryable": False,
                "session_closed": False,
            },
            raw,
        ) from None

    def probe(
        self,
        at,
        capture=(),
        set=None,
        hits=1,
        timeout=40,
        until="exit",
        set_hit=1,
        entry=None,
        args=(),
        expect_sha256=None,
        edit=None,
        set_at=(),
        expect_inputs=None,
    ):
        """One stateless request: stop at `path:line`, capture, optionally set, report.

        ``set_at`` holds (line, hit, name, value) interventions, each at its own stop of
        the same file; a hit of "all" makes it at every stop of its line.

        ``edit=(old, new)`` probes a candidate fix: the worker builds the program with
        ``old`` replaced once by ``new``; ``at`` is in the original's line numbering. A
        tuple of such pairs makes several edits, each quoting the original file.

        ``expect_inputs`` maps every source the worker's target reads to its sha256: the
        worker refuses before launching when any differs, or is on one side only.
        """
        from peeky.probe import edit_pairs

        options = {"entry": entry} if entry else {}
        if edit:
            pairs = [{"old": old, "new": new} for old, new in edit_pairs(edit)]
            options["edit"] = pairs[0] if len(pairs) == 1 else pairs
        if args:
            options["args"] = list(args)
        if expect_sha256:
            options["expect_sha256"] = dict(expect_sha256)
        if expect_inputs is not None:
            options["expect_inputs"] = dict(expect_inputs)
            # The worker refuses a client of another protocol: they read paths differently.
            options["protocol"] = PROTOCOL_VERSION
        if set_at:
            options["set_at"] = [list(point) for point in set_at]
        return self.call(
            "probe",
            at=at,
            capture=list(capture),
            set=dict(set or {}),
            hits=hits,
            timeout=timeout,
            until=until,
            set_hit=set_hit,
            **options,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation")
    parser.add_argument("arguments", nargs="?", default="{}")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    print(
        json.dumps(
            Client(args.url, os.environ["DEBUG_TOKEN"]).call(
                args.operation, **json.loads(args.arguments)
            ),
            indent=2,
        )
    )
