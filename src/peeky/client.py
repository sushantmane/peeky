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
                {"code": "INVALID_REQUEST", "message": raw.decode(errors="replace")[:2000]},
                raw,
            ) from None

    def probe(self, at, capture=(), set=None, hits=1, timeout=40):
        """One stateless request: stop at `path:line`, capture, optionally set, report."""
        return self.call(
            "probe", at=at, capture=list(capture), set=dict(set or {}), hits=hits, timeout=timeout
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
