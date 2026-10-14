"""Real C++ class/reference/array observations through the authenticated worker."""

import time
from urllib.error import HTTPError

import pytest


def wait(client, session, state):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = client.call("wait", **session, timeout=1)
        if result["state"] == state:
            return result
        assert result["state"] == "running", result
    pytest.fail(f"C++ target did not reach {state}")


def test_cpp_class_reference_and_array_expansion(cpp_investigation):
    client, _, _, _, line, _ = cpp_investigation
    started = client.call("launch", program="pagination_bug.cpp", breakpoints=[{"line": line}])
    session = {"session_id": started["session_id"]}
    stop = wait(client, session, "stopped")
    context = dict(session, stop_id=stop["stop_id"])
    snapshot = client.call("inspect", **context)
    variables = {v["name"]: v for s in snapshot["scopes"] for v in s["variables"]}
    assert variables["start"]["value"] == "3"
    assert "Pagination" in variables["pagination"]["type"]
    assert "Pagination" in variables["request"]["type"]
    assert "array" in variables["items"]["type"]

    def expand(variable, start=0, count=16):
        return client.call(
            "expand", **context, handle=variable["variablesReference"], start=start, count=count
        )["variables"]

    for name in ("pagination", "request"):
        members = {v["name"]: v for v in expand(variables[name])}
        assert {key: members[key]["value"] for key in ("page", "page_size", "total")} == {
            "page": "1",
            "page_size": "3",
            "total": "8",
        }
    array = expand(variables["items"])
    # Raw storage uses a platform-specific member; synthetic views may expose elements directly.
    if not array[0]["name"].startswith("["):
        assert len(array) == 1 and array[0]["variablesReference"] > 0, array
        array = expand(array[0], count=3)
    assert [v["value"] for v in array[:3]] == ["10", "20", "30"]
    stale = variables["pagination"]["variablesReference"]
    client.call("inspect", **context)
    with pytest.raises(HTTPError) as error:
        client.call("expand", **context, handle=stale, start=0, count=3)
    assert error.value.code == 400
