import pytest

from peeky.probe import Probe, parse_location, parse_selector


@pytest.mark.parametrize(
    "text, segments",
    [
        ("start", ["start"]),
        ("request.page", ["request", "page"]),
        ("args[0]", ["args", "[0]"]),
        ("a.b[12].c", ["a", "b", "[12]", "c"]),
    ],
)
def test_selectors_are_plain_dotted_paths(text, segments):
    assert parse_selector(text) == segments


@pytest.mark.parametrize(
    "text",
    ["", "a..b", "[0]", "a[]", "a[x]", "a.b()", "$variants$", "a-b", "a;b", "x" * 600, "a." * 17],
)
def test_selectors_reject_expressions_and_synthetic_names(text):
    with pytest.raises(ValueError):
        parse_selector(text)


def test_locations_require_relative_path_and_positive_line():
    assert parse_location("src/main.rs:36") == ("src/main.rs", 36)
    for text in ("main.rs", "main.rs:0", ":3", "main.rs:x", None):
        with pytest.raises(ValueError):
            parse_location(text)


@pytest.mark.parametrize(
    "change",
    [
        {"line": 0},
        {"capture": ["a"]},
        {"capture": ("a()",)},
        {"capture": ("a",) * 33},
        {"set": (("a.b", "1"),)},
        {"set": (("a", 1),)},
        {"set": (("a", "1"),) * 9},
        {"hits": 0},
        {"hits": 101},
        {"timeout": 0},
        {"timeout": 301},
        {"timeout": True},
    ],
)
def test_probe_requests_are_bounded_and_typed(change):
    with pytest.raises(ValueError):
        Probe(**{"program": "main.py", "line": 3, **change})


class FakeWorker:
    """Scripted worker: one stop with a given reason/location, then termination."""

    def __init__(self, reason="breakpoint", path="/w/main.py", line=8, update=None):
        self.reason, self.path, self.line, self.update = reason, path, line, update
        self.calls = []
        self.stopped = False

    def dispatch(self, operation, args):
        self.calls.append(operation)
        if operation == "launch":
            return {
                "session_id": "s",
                "breakpoints": [{"id": 1, "verified": True, "line": 8}],
                "artifact_manifest_id": None,
                "target_identity": None,
                "source_sha256": "x",
            }
        if operation == "wait":
            if self.stopped:
                return {"state": "terminated"}
            self.stopped = True
            result = {
                "state": "stopped",
                "stop_id": "t",
                "event": {"body": {"reason": self.reason}},
            }
            if self.update:
                result["breakpoint_events"] = [{"breakpoint": {"id": 1, "line": self.update}}]
            return result
        if operation == "inspect":
            frame = {"source": {"path": self.path}, "line": self.line}
            return {
                "frames": [frame],
                "scopes": [
                    {
                        "name": "Locals",
                        "reference": 1,
                        "variables": [{"name": "x", "value": "1", "variablesReference": 0}],
                    }
                ],
            }
        if operation == "report":
            return {"output": "", "output_truncated": False}
        return {}

    def close(self):
        self.calls.append("close")


@pytest.mark.parametrize(
    "fake, ok",
    [
        ({}, True),
        ({"update": 9, "line": 9}, True),
        ({"reason": "exception"}, False),
        ({"reason": "signal"}, False),
        ({"line": 12}, False),
        ({"path": "/w/other.py"}, False),
        ({"path": "/w/notmain.py"}, False),
    ],
)
def test_only_the_requested_breakpoint_counts_as_a_hit(monkeypatch, fake, ok):
    import peeky.probe as probe_module

    worker = FakeWorker(**fake)
    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: worker)
    result = probe_module.run_probe("/w", Probe("main.py", 8, ("x",), (("x", "2"),)))
    if ok:
        assert result["status"] == "completed" and result["hits"] == 1
    else:
        assert result["status"] == "failed" and result["hits"] == 0
        assert "not the requested" in result["error"]["message"]
        assert "set_variable" not in worker.calls and result["rows"] == []
    assert worker.calls[-1] == "close"


@pytest.mark.parametrize("selector", ["items[-1]", "a.b[-12]", "x[0][-1]"])
def test_negative_index_selectors_parse(selector):
    from peeky.probe import parse_selector

    assert parse_selector(selector)[-1].startswith("[")


@pytest.mark.parametrize("selector", ["items[--1]", "items[-]", "items[1-]", "[-1]"])
def test_malformed_negative_selectors_are_rejected(selector):
    from peeky.probe import parse_selector

    with pytest.raises(ValueError):
        parse_selector(selector)


@pytest.mark.parametrize(
    "variable, children, length",
    [
        ({"type": "list", "value": "[1, 2]"}, [{"name": "len()", "value": "2"}], 2),
        ({"type": "tuple", "value": "(1,)"}, [{"name": "len()", "value": "1"}], 1),
        ({"type": "dict", "value": "{0: 1}"}, [{"name": "len()", "value": "1"}], None),
        ({"type": "MyList", "value": "[1]"}, [{"name": "len()", "value": "1"}], None),
        ({"type": "list", "value": "[1]"}, [], None),
        ({"value": "", "type": "int[4]"}, [], 4),
        ({"value": "", "type": "unsigned char[16]"}, [], 16),
        ({"value": "", "type": "int[4][3]"}, [], None),
        ({"value": "int[5]@123", "type": "int[]"}, [], 5),
        # LLDB summaries and DAP counts can equal a capped child count: never trusted.
        ({"value": "size=24", "type": "std::forward_list<int>"}, [], None),
        ({"value": "size=3", "type": "std::vector<int>", "indexedVariables": 3}, [], None),
        ({"value": "{...}", "indexedVariables": 6}, [], None),
        ({"value": "[1, 2]", "type": "str"}, [], None),
    ],
)
def test_declared_lengths_must_be_independent_of_the_listing(variable, children, length):
    from peeky.probe import _declared_length

    assert _declared_length(variable, children) == length


class Pages:
    def __init__(self, names, value):
        self.names, self.value = names, value

    def dispatch(self, operation, args):
        children = [{"name": name, "value": name} for name in self.names]
        return {"variables": children, "truncated": False}


@pytest.mark.parametrize(
    "names, value, outcome",
    [
        (["[0]", "[1]", "[2]"], "int[3]", "[2]"),
        (["[0]", "[1]"], "int[3]", "only 2 of 3"),
        (["[0]", "[1]"], "size=2", "independent length"),
        (["[0]", "[2]"], "int[2]", "not an indexed sequence"),
    ],
)
def test_negative_indexes_never_guess_the_end(names, value, outcome):
    from peeky.probe import _from_end

    variable = {"variablesReference": 1, "type": value}
    if outcome.startswith("["):
        assert _from_end(Pages(names, value), {}, variable, 1, "[-1]")["name"] == outcome
    else:
        with pytest.raises(ValueError, match=outcome):
            _from_end(Pages(names, value), {}, variable, 1, "[-1]")
