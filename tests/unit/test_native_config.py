"""Trusted native configuration rejects accidental coercion and command hooks."""

import json

import pytest

from peeky.worker import load_native_target


@pytest.mark.parametrize(
    "config",
    [
        [],
        {},
        {"sources": "main.c"},
        {"sources": ["main.c"], "arguments": "--flag"},
        {"sources": ["main.c"], "headers": "main.h"},
        {"sources": ["main.c"], "initCommands": ["script print('unexpected')"]},
        {"sources": ["../main.c"]},
        {"sources": ["@response.c"]},
    ],
)
def test_native_configuration_rejects_nonarrays_and_untrusted_fields(tmp_path, config):
    path = tmp_path / "target.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_native_target(path)


def test_native_configuration_preserves_argument_boundaries(tmp_path):
    path = tmp_path / "target.json"
    path.write_text(json.dumps({"sources": ["main.c"], "arguments": ["", "a b", "*.c"]}))
    target = load_native_target(path)
    assert target.sources == ("main.c",)
    assert target.headers == ()
    assert target.arguments == ("", "a b", "*.c")
