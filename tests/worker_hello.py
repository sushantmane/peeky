"""A fake worker's answer to a client's `hello`, for the tests' own HTTP fakes: the proof a
worker holding ``token`` makes (protocol 11)."""

import json

from peeky.client import hello_proof
from peeky.errors import PROTOCOL_VERSION


def hello_answer(body, token, protocol=PROTOCOL_VERSION):
    """The JSON bytes a worker holding ``token`` answers a `hello` request's ``body`` with."""
    nonce = bytes.fromhex(json.loads(body)["nonce"])
    return json.dumps({"protocol": protocol, "proof": hello_proof(token, nonce)}).encode()
