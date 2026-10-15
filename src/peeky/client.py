"""JSON HTTP SDK and CLI, usable by any agent with shell or HTTP tools."""

import argparse
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from peeky.errors import PeekyError


class Client:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token

    def call(self, operation, **arguments):
        request = Request(
            self.url + "/" + operation,
            data=json.dumps(arguments).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=45) as response:
                return json.load(response)
        except HTTPError as exc:
            raw = exc.read() or b"{}"
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                raise PeekyError(exc.url, exc.code, exc.headers, body["error"], raw) from None
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
    ):
        """One stateless request: stop at `path:line`, capture, optionally set, report."""
        options = {"entry": entry} if entry else {}
        if args:
            options["args"] = list(args)
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
