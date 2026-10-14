"""JSON HTTP SDK and CLI, usable by any agent with shell or HTTP tools."""

import argparse
import json
import os
from urllib.request import Request, urlopen


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
        with urlopen(request, timeout=45) as response:
            return json.load(response)


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
