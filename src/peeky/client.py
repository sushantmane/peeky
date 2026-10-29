"""JSON HTTP SDK and CLI, usable by any agent with shell or HTTP tools."""

import argparse
import http.client
import json
import os
import socket
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


def _spoken(headers, operation, url):
    """Refuse a worker's answer where the worker says it speaks another protocol (its
    `X-Peeky-Protocol` header, on every answer), however the session started: a local
    session's worker of an older Peeky would ignore what this client sends, such as
    `expand`'s `key`, and answer something else (#374 review). Not `version`'s answer,
    whose caller compares the protocol it states and says what to do."""
    spoken = headers.get("X-Peeky-Protocol") if headers is not None else None
    if operation == "version" or spoken is None or spoken.strip() == str(PROTOCOL_VERSION):
        return
    raise ValueError(
        f"Worker speaks protocol {spoken.strip()[:20]}, this client {PROTOCOL_VERSION}: its "
        f"answer to {operation!r} at {url} is not read. Run the same Peeky version as the "
        "worker; for a session, close it and start it again"
    )


class Client:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token
        # The protocol the worker's `version` states, once asked (or vouched for by the
        # caller): nothing but `version` is sent to a worker before it is this client's
        # (#374 review). A worker of another protocol would run a step or a change as it
        # reads the request, and ignore what it doesn't know (`expand`'s `key`).
        self.protocol = None

    def call(self, operation, **arguments):
        if operation != "version":
            self._preflight(operation)
        answer = self._request(operation, arguments)
        if (
            operation == "version"
            and isinstance(answer, dict)
            and type(answer.get("protocol")) is int
        ):
            self.protocol = answer["protocol"]
        return answer

    def _preflight(self, operation):
        """Ask the worker's `version` once, before anything else is sent; refuse
        ``operation`` where the worker speaks another protocol, or doesn't say."""
        if self.protocol is None:
            answer = self._request("version", {})
            found = answer.get("protocol") if isinstance(answer, dict) else None
            self.protocol = found if type(found) is int else "unknown"
        if self.protocol != PROTOCOL_VERSION:
            raise ValueError(self._refusal(operation))

    def _refusal(self, operation):
        return (
            f"Worker speaks protocol {self.protocol}, this client {PROTOCOL_VERSION}: nothing "
            f"but its version was sent to the worker at {self.url}, not {operation!r}. Run the "
            "same Peeky version as the worker"
        )

    def _request(self, operation, arguments):
        """One request, as it is: its answer, or the worker's error."""
        request = Request(
            self.url + "/" + operation,
            data=json.dumps(arguments).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            with _OPENER.open(request, timeout=45) as response:
                _spoken(response.headers, operation, self.url)
                try:
                    return json.load(response)
                except ValueError:
                    # Sent and answered, but not as the worker answers: whatever the
                    # worker did is unknown (#190 review).
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
        the same file (#137); a hit of "all" makes it at every stop of its line (#265).

        ``edit=(old, new)`` probes a candidate fix: the worker builds the program with
        ``old`` replaced once by ``new``; ``at`` is in the original's line numbering. A
        tuple of such pairs makes several edits, each quoting the original file.

        ``expect_inputs`` maps every source the worker's target reads to its sha256: the
        worker refuses before launching when any differs, or is on one side only (#249).
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
