import pytest

from peeky.probe import Probe, parse_location, parse_selector


@pytest.mark.parametrize(
    "text, segments",
    [
        ("start", ["start"]),
        ("request.page", ["request", "page"]),
        ("args[0]", ["args", "[0]"]),
        ("a.b[12].c", ["a", "b", "[12]", "c"]),
        # String keys are kept in their canonical repr form, as debugpy names entries.
        ("order['id']", ["order", "['id']"]),
        ('order["id"]', ["order", "['id']"]),
        ('d["it\'s"]', ["d", '["it\'s"]']),
        ("d['a.b]c'][0].x", ["d", "['a.b]c']", "[0]", "x"]),
        ("d['']", ["d", "['']"]),
    ],
)
def test_selectors_are_plain_dotted_paths(text, segments):
    assert parse_selector(text) == segments


@pytest.mark.parametrize(
    "text",
    [
        "",
        "a..b",
        "[0]",
        "a[]",
        "a[x]",
        "a.b()",
        "$variants$",
        "a-b",
        "a;b",
        "x" * 600,
        "a." * 17,
        "a" + "[0]" * 16,
        "['id']",
        "a['x'",
        "a[b'x']",
        "a[f'{x}']",
        "a[r'x']",
        'a["""x"""]',
        "a['\\d']",
        "a['x' + 'y']",
        "a['x']()",
        'a["\n"]',
        "a['" + "x" * 257 + "']",
    ],
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


class Tree:
    """A scripted worker whose handles expand to fixed children."""

    def __init__(self, tree):
        self.tree = tree

    def dispatch(self, operation, args):
        assert operation == "expand"
        return {"variables": self.tree[args["handle"]], "truncated": False}


def snapshot(*variables):
    return {"scopes": [{"name": "Locals", "variables": list(variables)}]}


ORDER = {"name": "order", "type": "dict", "value": "{...}", "variablesReference": 1}
ORDER_CHILDREN = [
    {"name": "special variables", "value": "", "variablesReference": 9},
    {"name": "'id'", "type": "int", "value": "18", "variablesReference": 0},
    {"name": '"it\'s"', "type": "int", "value": "5", "variablesReference": 0},
    {"name": "3", "type": "str", "value": "'three'", "variablesReference": 0},
    {"name": "len()", "type": "int", "value": "3", "variablesReference": 0},
]


@pytest.mark.parametrize(
    "selector, value",
    [
        ("order['id']", "18"),
        ('order["id"]', "18"),
        ('order["it\'s"]', "5"),
        ("order[3]", "'three'"),
    ],
)
def test_mapping_entries_are_read_by_key(selector, value):
    from peeky.probe import _resolve

    found = _resolve(Tree({1: ORDER_CHILDREN}), {}, snapshot(ORDER), parse_selector(selector))
    assert found["value"] == value


@pytest.mark.parametrize(
    "selector, message",
    [
        # An attribute path on a mapping names the key syntax that works.
        ("order.id", "select it with ['id'], not .id"),
        ("order['nope']", "not found in 'order' (dict); children: 'id', \"it's\", 3, len()"),
        ("order[7]", "not found in 'order' (dict)"),
    ],
)
def test_missing_entries_list_what_is_there(selector, message):
    from peeky.probe import _resolve

    with pytest.raises(ValueError) as caught:
        _resolve(Tree({1: ORDER_CHILDREN}), {}, snapshot(ORDER), parse_selector(selector))
    assert message in str(caught.value)
    assert "special variables" not in str(caught.value)


def test_a_key_shown_by_two_entries_is_ambiguous():
    from peeky.probe import _resolve

    twins = [*ORDER_CHILDREN, {"name": "'id'", "type": "Fake", "value": "0"}]
    with pytest.raises(ValueError, match="ambiguous"):
        _resolve(Tree({1: twins}), {}, snapshot(ORDER), parse_selector("order['id']"))


def test_keys_missing_from_a_partial_mapping_are_not_called_absent():
    from peeky.probe import _resolve

    partial = [ORDER_CHILDREN[1], {"name": "Too large to show contents. Max items to show: 300"}]
    with pytest.raises(ValueError, match="only part of a large mapping"):
        _resolve(Tree({1: partial}), {}, snapshot(ORDER), parse_selector("order['zzz']"))


def test_rust_container_indexes_explain_what_is_unsupported():
    from peeky.probe import _resolve

    vec = {
        "name": "ordered",
        "type": "alloc::vec::Vec<int, alloc::alloc::Global>",
        "value": "",
        "variablesReference": 1,
    }
    fields = [
        {"name": "buf", "type": "RawVec", "value": "", "variablesReference": 2},
        {"name": "len", "type": "unsigned long", "value": "3", "variablesReference": 0},
    ]
    with pytest.raises(ValueError) as caught:
        _resolve(Tree({1: fields}), {}, snapshot(vec), parse_selector("ordered[0]"))
    message = str(caught.value)
    assert "has no indexed elements" in message and "children: buf, len" in message
    assert "Rust Vec, slice and String contents are not readable yet" in message
    assert "capture ordered.len" in message


def test_depth_limited_values_say_so():
    from peeky.probe import _resolve

    deep = {"name": "ptr", "value": "Unique<u8>", "variablesReference": 0, "depth_limited": True}
    root = {"name": "v", "value": "", "variablesReference": 1}
    with pytest.raises(ValueError, match="inspection depth limit"):
        _resolve(Tree({1: [deep]}), {}, snapshot(root), parse_selector("v.ptr.pointer"))


@pytest.mark.parametrize(
    "name, selector",
    [
        ("'id'", "order['id']"),
        ('"it\'s"', 'order["it\'s"]'),
        ("'\\\\d'", "order['\\\\d']"),
        ("3", "order[3]"),
        ("[4]", "order[4]"),
        ("field", "order.field"),
        ("len()", None),
        ("(1, 2)", None),
        ("'\\d'", None),
        ("b'x'", None),
    ],
)
def test_children_are_offered_with_the_selector_that_reads_them(name, selector):
    from peeky.probe import _child_selector

    assert _child_selector("order", name) == selector
    if selector:
        assert parse_selector(selector)


def test_children_skip_debugger_groups_and_mark_expandable_entries():
    from peeky.probe import _capture

    class Worker(Tree):
        def dispatch(self, operation, args):
            if operation == "inspect":
                return {**snapshot(ORDER), "frames": [{"line": 3, "name": "f"}]}
            return super().dispatch(operation, args)

    nested = [
        *ORDER_CHILDREN,
        {"name": "'sub'", "type": "dict", "value": "{}", "variablesReference": 4},
    ]
    [row] = _capture(Worker({1: nested}), {}, ("order",), 0, "observed", children=True)
    assert [child.get("selector") for child in row["children"]] == [
        "order['id']",
        'order["it\'s"]',
        "order[3]",
        None,
        "order['sub']",
    ]
    assert row["children"][-1]["expandable"] is True
    [row] = _capture(Worker({1: nested}), {}, ("order",), 0, "observed")
    assert "children" not in row
