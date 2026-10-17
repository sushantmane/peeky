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
# debugpy marks entries whose key's repr evaluates back to the key (the worker passes
# that on as `evaluable`, never the expression itself).
ORDER_CHILDREN = [
    {"name": "special variables", "value": "", "variablesReference": 9},
    {"name": "'id'", "type": "int", "value": "18", "variablesReference": 0, "evaluable": True},
    {"name": '"it\'s"', "type": "int", "value": "5", "variablesReference": 0, "evaluable": True},
    {"name": "3", "type": "str", "value": "'three'", "variablesReference": 0, "evaluable": True},
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


class PagedTree(Tree):
    """Children served 64 per page, as the worker does."""

    def dispatch(self, operation, args):
        children = self.tree[args["handle"]]
        start = args.get("start", 0)
        return {"variables": children[start : start + 64], "truncated": start + 64 < len(children)}


def test_a_key_shown_by_an_impostor_object_is_refused():
    from peeky.probe import _resolve

    filler = [
        {"name": f"'k{i}'", "type": "int", "value": str(i), "evaluable": True} for i in range(100)
    ]
    unique = [*ORDER_CHILDREN, *filler]
    found = _resolve(PagedTree({1: unique}), {}, snapshot(ORDER), parse_selector("order['k99']"))
    assert found["value"] == "99"
    # An object whose __repr__ returns "'a'" is listed first under the plain name; the
    # real "a" follows as "'a' (id: N)". Neither may be read as order['a'].
    impostor = [{"name": "'a'", "type": "str", "value": "'weird'"}, *filler]
    impostor.append({"name": "'a' (id: 4336031232)", "value": "'real'", "evaluable": True})
    with pytest.raises(ValueError, match="ambiguous"):
        _resolve(PagedTree({1: impostor}), {}, snapshot(ORDER), parse_selector("order['a']"))
    # The real key first: it is the plain-named, evaluable entry, read at once without
    # scanning the rest of the mapping.
    real_first = [{"name": "'a'", "value": "'real'", "evaluable": True}, *filler]
    found = _resolve(PagedTree({1: real_first}), {}, snapshot(ORDER), parse_selector("order['a']"))
    assert found["value"] == "'real'"


@pytest.mark.parametrize(
    "listing",
    [
        # debugpy 1.8 lists 500 entries, then this marker (pydevd_resolver.TOO_LARGE_ATTR).
        [ORDER_CHILDREN[1], {"name": "Unable to handle:", "value": "Maximum number of items"}],
        # Fewer entries than the mapping's own len(), whatever the marker.
        [ORDER_CHILDREN[1], {"name": "len()", "type": "int", "value": "505"}],
    ],
)
def test_keys_missing_from_a_partial_mapping_are_not_called_absent(listing):
    from peeky.probe import _resolve

    with pytest.raises(ValueError, match="only part of a large mapping"):
        _resolve(Tree({1: listing}), {}, snapshot(ORDER), parse_selector("order['zzz']"))


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
    assert "Peeky reads Rust container contents only for Vec<i32>" in message
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
        {
            "name": "'sub'",
            "type": "dict",
            "value": "{}",
            "variablesReference": 4,
            "evaluable": True,
        },
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


@pytest.mark.parametrize(
    "text, selectors",
    [
        ("a,b", ("a", "b")),
        (" a , ,b ", ("a", "b")),
        ("a,d['x,y'],c", ("a", "d['x,y']", "c")),
        ('d["a,\\"b"],e', ('d["a,\\"b"]', "e")),
        ("x['it\\'s,']", ("x['it\\'s,']",)),
    ],
)
def test_capture_lists_split_on_commas_outside_quoted_keys(text, selectors):
    from peeky.probe import split_selectors

    assert split_selectors(text) == selectors
    assert all(parse_selector(selector) for selector in selectors)


@pytest.mark.parametrize(
    "text, ok",
    [
        ("d['\\x41']", True),
        ("d['\\N{BULLET}']", True),
        ("d['\\u00e9\\t\\\\']", True),
        ("d['\\377']", True),
        ("d['\\d']", False),
        ("d['\\777']", False),
        ("d['\\8']", False),
        ("d['\\N{NO SUCH CHARACTER NAME}']", False),
    ],
)
def test_key_escapes_are_validated_without_touching_warning_filters(text, ok):
    import warnings

    before = list(warnings.filters)
    if ok:
        assert parse_selector(text)
    else:
        with pytest.raises(ValueError):
            parse_selector(text)
    assert warnings.filters == before


def test_unreadable_children_keep_their_reason():
    from peeky.cli import _get_lines
    from peeky.probe import _children

    children = [
        {
            "name": "t",
            "type": "int",
            "value": None,
            "unavailable": True,
            "reason": "error: variable not available",
        }
    ]
    entry = _children(Tree({1: children}), {}, "s", {"variablesReference": 1})["children"][0]
    assert entry["unavailable"] and entry["reason"] == "error: variable not available"
    [_, line] = _get_lines(
        {
            "selector": "s",
            "type": "S",
            "value": "{...}",
            **_children(Tree({1: children}), {}, "s", {"variablesReference": 1}),
        }
    )
    assert "<no readable value: error: variable not available>" in line


def test_child_listings_are_retried_on_a_fresh_inspection():
    from peeky.probe import _capture

    class Starved(Tree):
        expansions = 0

        def dispatch(self, operation, args):
            if operation == "inspect":
                return {**snapshot(ORDER), "frames": [{"line": 3, "name": "f"}]}
            Starved.expansions += 1
            if Starved.expansions == 1:
                raise ValueError("Inspection node budget exhausted; inspect again")
            return super().dispatch(operation, args)

    [row] = _capture(Starved({1: ORDER_CHILDREN}), {}, ("order",), 0, "observed", children=True)
    assert "children_error" not in row and row["children"][0]["selector"] == "order['id']"


def test_impostor_entries_get_no_selector_and_are_never_read():
    from peeky.probe import _children, _resolve

    # {K(): 'weird', 'a': 'real-a'} and {N(): 'weird-int', 1: 'real-int'}, where the
    # impostors' __repr__ return "'a'" and "1": debugpy marks only the real keys evaluable.
    entries = [
        {"name": "'a'", "type": "str", "value": "'weird'"},
        {"name": "'a' (id: 4335162880)", "type": "str", "value": "'real-a'", "evaluable": True},
        {"name": "1", "type": "str", "value": "'weird-int'"},
        {"name": "1 (id: 4335162999)", "type": "str", "value": "'real-int'", "evaluable": True},
        {"name": "'b'", "type": "int", "value": "2", "evaluable": True},
    ]
    listed = _children(Tree({1: entries}), {}, "imp", {"variablesReference": 1})["children"]
    assert [entry.get("selector") for entry in listed] == [None, None, None, None, "imp['b']"]
    for selector in ("imp['a']", "imp[1]"):
        with pytest.raises(ValueError, match="ambiguous"):
            _resolve(
                Tree({1: entries}), {}, snapshot({**ORDER, "name": "imp"}), parse_selector(selector)
            )
    # Debuggers that mark nothing evaluable (LLDB, java-debug) still read indexes.
    plain = [
        {"name": "[0]", "type": "int", "value": "7"},
        {"name": "[1]", "type": "int", "value": "8"},
    ]
    found = _resolve(Tree({1: plain}), {}, snapshot({**ORDER, "name": "a"}), parse_selector("a[1]"))
    assert found["value"] == "8"


def test_entries_debugpy_groups_by_value_type_are_explained():
    from peeky.probe import _partial, _resolve

    # {"it's": len, 'plain': len, 2: len}: debugpy files the function values in a group.
    entries = [
        {"name": "function variables", "value": "", "variablesReference": 7},
        {
            "name": "'plain'",
            "type": "builtin_function_or_method",
            "value": "len",
            "evaluable": True,
        },
        {"name": "len()", "type": "int", "value": "3"},
    ]
    assert not _partial(entries)
    for selector in ('fn_vals["it\'s"]', "fn_vals[2]"):
        with pytest.raises(ValueError, match="'function variables', which selectors cannot reach"):
            _resolve(
                Tree({1: entries}),
                {},
                snapshot({**ORDER, "name": "fn_vals"}),
                parse_selector(selector),
            )


def test_hits_take_a_count_or_all():
    import argparse

    from peeky.probe import MAX_HITS, parse_hits

    assert parse_hits("all") == "all" and parse_hits("7") == 7
    with pytest.raises(argparse.ArgumentTypeError):
        parse_hits("ALL")
    assert Probe("main.py", 3, hits="all").limit == MAX_HITS
    assert Probe("main.py", 3, hits="all", set_hit=MAX_HITS).set_hit == MAX_HITS
    for hits in ("All", "10", 0, True, None):
        with pytest.raises(ValueError):
            Probe("main.py", 3, hits=hits)


def test_progress_for_repeated_hits_is_rate_limited(monkeypatch):
    from peeky import probe

    clock = [100.0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])
    # The first and the last requested hit always print.
    assert probe._report_hit(1, 30, last_said=100.0) and probe._report_hit(30, 30, 100.0)
    assert not probe._report_hit(5, 30, last_said=99.5)
    clock[0] = 102.0
    assert probe._report_hit(6, 30, last_said=99.5)


def test_hits_all_fits_the_row_budget_and_needs_the_program_to_exit():
    from peeky.probe import MAX_HITS, MAX_ROWS

    assert Probe("main.py", 3, ("a",) * 1, hits="all").limit == MAX_HITS
    many = tuple(f"s{i}" for i in range(21))
    assert Probe("main.py", 3, many, hits="all").limit == MAX_ROWS // 21
    with_set = Probe("main.py", 3, many, (("s0", "1"),), hits="all")
    assert with_set.limit == (MAX_ROWS - 21) // 21
    with pytest.raises(ValueError, match="until the program exits"):
        Probe("main.py", 3, ("a",), hits="all", until="hits")
    # Repeated selectors add nothing and would multiply comparison rows.
    assert Probe("main.py", 3, ("a", "b", "a")).capture == ("a", "b")
