"""#104: caller frames, Python module globals and Python object identity, with fakes."""

from copy import deepcopy

import pytest

from peeky.adapters import PythonAdapter
from peeky.errors import classify
from peeky.probe import _capture, _resolve, parse_selector
from peeky.python_scopes import shadowed_names
from peeky.worker import DebugWorker

GROUP = {"name": "special variables", "type": "", "value": "", "variablesReference": 90}


class Backend:
    """A debugpy-shaped adapter: three frames, Locals and Globals per frame."""

    def __init__(self, root):
        source = {"path": str(root / "app.py")}
        self.frames = [
            {"id": 11, "name": "normalize", "line": 4, "source": source},
            {"id": 12, "name": "main", "line": 9, "source": source},
            {"id": 13, "name": "<module>", "line": 12, "source": source},
        ]
        self.locals = {
            11: [{"name": "seen", "type": "list", "value": "[]", "variablesReference": 7}],
            12: [
                {"name": "a", "type": "list", "value": "[1]", "variablesReference": 8},
                {"name": "b", "type": "list", "value": "[1]", "variablesReference": 8},
                {"name": "n", "type": "int", "value": "3", "variablesReference": 0},
            ],
            13: [{"name": "LIMIT", "type": "int", "value": "3", "variablesReference": 0}],
        }
        self.globals = [
            GROUP,
            {"name": "LIMIT", "type": "int", "value": "3", "variablesReference": 0},
            {"name": "TABLE", "type": "dict", "value": "{}", "variablesReference": 9},
        ]
        self.calls = []

    def request(self, operation, args):
        self.calls.append((operation, deepcopy(args)))
        if operation == "stackTrace":
            return {"stackFrames": deepcopy(self.frames)}
        if operation == "scopes":
            frame = args["frameId"]
            return {
                "scopes": [
                    {"name": "Locals", "presentationHint": "locals", "variablesReference": frame},
                    {"name": "Globals", "variablesReference": 100 + frame},
                ]
            }
        if operation == "variables":
            reference = args["variablesReference"]
            if reference > 100:
                return {"variables": deepcopy(self.globals)}
            if reference in self.locals:
                return {"variables": deepcopy(self.locals[reference])}
            if reference == 90:
                return {
                    "variables": [
                        {
                            "name": "__name__",
                            "type": "str",
                            "value": "'app'",
                            "variablesReference": 0,
                        }
                    ]
                }
            return {"variables": []}
        if operation == "setVariable":
            return {"value": args["value"]}
        raise AssertionError(operation)

    def close(self):
        pass


class Runtime(PythonAdapter):
    """PythonAdapter's scope, identity and proof hooks; records `innermost` per value."""

    def __init__(self):
        object.__setattr__(self, "innermost", [])

    def view_variable(self, variable, fetch, read=None, *, frame=None, top=False, innermost=False):
        if top:
            self.innermost.append((frame["id"], variable["name"], innermost))
        return None

    @staticmethod
    def globals_proof(frame, listed, loaded_by=None):
        assert loaded_by == 1000.0
        return {"shadowed": ["key", "seen"]} if frame["name"] == "normalize" else {"shadowed": []}


@pytest.fixture
def worker(tmp_path):
    instance = DebugWorker(tmp_path)
    instance.dap, instance.runtime = Backend(tmp_path), Runtime()
    instance.session, instance.stop_id, instance.state = "session", "stop", "stopped"
    instance.thread_id = 1
    instance.capabilities = {"supports_set_variable": True}
    instance.launched_at = 1000.0
    return instance


STOP = {"session_id": "session", "stop_id": "stop"}


def inspect(worker, **args):
    return worker.dispatch("inspect", {**STOP, **args})


def test_a_frame_index_reads_that_frame_and_only_the_innermost_gets_its_proofs(worker):
    snapshot = inspect(worker, frame=1)
    assert snapshot["frame"] == 1 and snapshot["function"] == "main"
    assert snapshot["innermost"]["function"] == "normalize"
    assert snapshot["innermost"]["location"].endswith("app.py:4")
    assert [v["name"] for v in snapshot["scopes"][0]["variables"]] == ["a", "b", "n"]
    assert ("scopes", {"frameId": 12}) in worker.dap.calls
    # A caller's values are never treated as the innermost frame's.
    assert worker.runtime.innermost[:3] == [(12, "a", False), (12, "b", False), (12, "n", False)]
    assert not any(innermost for _, _, innermost in worker.runtime.innermost)
    worker.runtime.innermost.clear()
    snapshot = inspect(worker)
    assert snapshot["frame"] == 0 and "innermost" not in snapshot
    assert worker.runtime.innermost[0] == (11, "seen", True)


def test_an_out_of_range_frame_names_how_many_there_are(worker):
    with pytest.raises(ValueError) as caught:
        inspect(worker, frame=3)
    assert "this stop lists 3 frame(s), 0 to 2" in str(caught.value)
    assert classify(caught.value)["code"] == "INVALID_REQUEST"
    for bad in (-1, "1", 1.0, True):
        with pytest.raises(ValueError, match="non-negative integer"):
            inspect(worker, frame=bad)
    with pytest.raises(ValueError, match="not both"):
        inspect(worker, frame=1, frame_id=12)


def test_a_frame_sharing_the_innermost_frames_id_is_refused(worker):
    worker.dap.frames[1]["id"] = 11
    with pytest.raises(ValueError, match="same id as another frame"):
        inspect(worker, frame=1)


def test_python_globals_are_listed_read_only_and_once_at_module_level(worker):
    snapshot = inspect(worker)
    locals_, globals_ = snapshot["scopes"]
    assert globals_["name"] == "Globals" and globals_["globals"] is True
    assert "globals" not in locals_
    assert snapshot["globals_proof"] == {"shadowed": ["key", "seen"]}
    worker.dap.frames[0]["source"]["path"] = "/elsewhere/app.py"
    snapshot = inspect(worker)
    assert snapshot["globals_proof"] == {"unproven": "its source is outside the target root"}
    globals_ = snapshot["scopes"][1]
    with pytest.raises(ValueError, match="Module globals cannot be changed") as caught:
        worker.dispatch(
            "set_variable",
            {**STOP, "reference": globals_["reference"], "name": "LIMIT", "value": "1"},
        )
    assert classify(caught.value)["code"] == "MUTATION_REJECTED"
    # A module's code runs with its globals as its locals: one scope, no proof needed.
    module = inspect(worker, frame=2)
    assert [scope["name"] for scope in module["scopes"]] == ["Locals"]
    assert "globals_proof" not in module


def test_python_values_carry_an_identity_but_scalars_and_groups_do_not(worker):
    snapshot = inspect(worker, frame=1)
    a, b, n = snapshot["scopes"][0]["variables"]
    assert a["identity"] == b["identity"] == "list@8" and "identity" not in n
    group = snapshot["scopes"][1]["variables"][0]
    assert group["name"] == "special variables" and "identity" not in group
    assert snapshot["scopes"][1]["variables"][2]["identity"] == "dict@9"


@pytest.mark.parametrize(
    "variable",
    [
        {"name": "x", "type": "int", "variablesReference": 0},
        {"name": "special variables", "type": "", "variablesReference": 5},
        {"name": "function variables", "type": "list", "variablesReference": 5},
        {"name": "more", "type": "MoreItems", "variablesReference": 5},
        {"name": "[100:200]", "type": "MoreItemsRange", "variablesReference": 5},
        {"name": "x", "variablesReference": 5},
        {"name": "x", "type": "list", "variablesReference": "5"},
    ],
)
def test_no_identity_without_an_adapter_reference_or_for_debugpys_own_nodes(variable):
    assert PythonAdapter.object_identity(variable) is None


def test_scopes_cut_at_the_bound_say_so(worker):
    worker.dap.locals[11] = [
        {"name": f"v{i:02}", "type": "int", "value": "1", "variablesReference": 0}
        for i in range(60)
    ]
    scope = inspect(worker)["scopes"][0]
    assert len(scope["variables"]) == 50 and scope["truncated"] and scope["total"] == 60


def test_a_missing_name_is_read_from_globals_only_when_proven_unshadowed(worker):
    snapshot = inspect(worker)
    found = _resolve(worker, STOP, snapshot, parse_selector("LIMIT"))
    assert found["value"] == "3" and found["scope"] == "globals"
    # In debugpy's `special variables` group of the Globals listing.
    assert _resolve(worker, STOP, snapshot, parse_selector("__name__"))["value"] == "'app'"
    with pytest.raises(ValueError, match="normalize binds it"):
        _resolve(worker, STOP, snapshot, parse_selector("key"))
    snapshot["globals_proof"] = {"unproven": "its source could not be parsed"}
    with pytest.raises(ValueError) as caught:
        _resolve(worker, STOP, snapshot, parse_selector("LIMIT"))
    assert "may be the module global 'LIMIT'" in str(caught.value)
    assert "could not prove" in str(caught.value) and "could not be parsed" in str(caught.value)
    assert str(caught.value).startswith("not found")
    with pytest.raises(ValueError, match=r"^not found; visible: seen; module globals: LIMIT"):
        _resolve(worker, STOP, snapshot, parse_selector("nothing"))


def test_get_rows_name_the_frame_scope_and_identity(worker):
    rows = _capture(worker, STOP, ("a", "LIMIT"), 0, "observed", frame=1)
    assert rows[0]["frame"] == 1 and rows[0]["function"] == "main" and rows[0]["line"] == 9
    assert rows[0]["identity"] == "list@8" and "scope" not in rows[0]
    assert rows[1]["value"] == "3" and rows[1]["scope"] == "globals"
    assert "identity" not in rows[1]


def test_a_worker_that_ignores_frame_is_refused():
    class Old:
        def dispatch(self, operation, args):
            return {"frames": [{"line": 1}], "scopes": []}

    with pytest.raises(ValueError, match="predates --frame"):
        _capture(Old(), STOP, ("a",), 0, "observed", frame=1)


def scope_of(tmp_path, text, function, line, listed=()):
    path = tmp_path / "app.py"
    path.write_text(text)
    return shadowed_names(path, function, line, listed)


SOURCE = """\
import functools
LIMIT = 1
TOTAL = 2


def shadow(n):
    print(LIMIT)
    LIMIT = n
    return [w for w in range(n)]


@functools.cache
def declared():
    global TOTAL
    TOTAL = TOTAL + 1


def outer():
    TOTAL = 5

    def inner():
        return LIMIT

    return inner()


class Box:
    size = 4

    def grow(self):
        return super().grow()

"""


def test_shadowed_names_follow_the_compilers_scoping(tmp_path):
    names, reason = scope_of(tmp_path, SOURCE, "shadow", 7, ["n"])
    assert reason is None and {"LIMIT", "n", "w"} <= set(names) and "TOTAL" not in names
    names, _ = scope_of(tmp_path, SOURCE, "declared", 15)
    assert "TOTAL" not in names
    # The name is free in neither: still the enclosing function's, never the module's.
    names, _ = scope_of(tmp_path, SOURCE, "inner", 22)
    assert "TOTAL" in names and "LIMIT" not in names
    names, _ = scope_of(tmp_path, SOURCE, "grow", 31, ["self", "__class__"])
    assert "self" in names and "size" not in names


NESTED_TWINS = """\
def twin():
    def twin():
        return 1

    return twin()
"""


@pytest.mark.parametrize(
    "source, function, line, listed, reason",
    [
        (NESTED_TWINS, "twin", 3, (), "2 functions named twin contain line 3"),
        (SOURCE, "shadow", 2, (), "no function named shadow contains line 2"),
        (SOURCE, "Box", 28, (), "class body"),
        (SOURCE, "<lambda>", 7, (), "not a function"),
        (SOURCE, "shadow", 7, ("n", "ghost"), "does not bind (ghost)"),
    ],
)
def test_globals_stay_unread_when_the_function_is_not_identified(
    tmp_path, source, function, line, listed, reason
):
    names, why = scope_of(tmp_path, source, function, line, listed)
    assert names is None and reason in why


def test_a_source_changed_after_the_program_started_proves_nothing(tmp_path):
    import os
    import time

    path = tmp_path / "app.py"
    path.write_text(SOURCE)
    started = time.time()
    assert shadowed_names(path, "shadow", 7, ["n"], loaded_by=started)[1] is None
    os.utime(path, (started + 5, started + 5))
    names, why = shadowed_names(path, "shadow", 7, ["n"], loaded_by=started)
    assert names is None and why == "its source file changed after the program started"


def test_unreadable_or_unparsable_sources_prove_nothing(tmp_path):
    assert shadowed_names(tmp_path / "gone.py", "f", 1)[1].startswith(
        "its source could not be read"
    )
    names, why = scope_of(tmp_path, "def f(:\n", "f", 1)
    assert names is None and "could not be parsed" in why


class Tree:
    """Expansions that page fixed children, as the worker does."""

    def __init__(self, tree):
        self.tree = tree

    def dispatch(self, operation, args):
        children = self.tree[args["handle"]]
        start = args.get("start", 0)
        return {"variables": children[start : start + 64], "truncated": start + 64 < len(children)}


def group(name, handle):
    return {"name": name, "type": "", "value": "", "variablesReference": handle}


def entry(name, value, kind="int", handle=0):
    return {"name": name, "type": kind, "value": value, "variablesReference": handle}


def test_attributes_debugpy_groups_are_read_by_name_but_never_a_mappings_entries():
    function = entry("normalize", "<function normalize>", "function", 1)
    mapping = entry("table", "{...}", "dict", 4)
    tree = Tree(
        {
            1: [group("special variables", 2)],
            2: [entry("__defaults__", "([],)", "tuple", 3), entry("__name__", "'normalize'")],
            3: [entry("0", "[]", "list", 5), entry("len()", "1")],
            # {Impostor(): 1}: a key whose repr is `__doc__` sits in the dict's groups.
            4: [group("special variables", 6), entry("len()", "1")],
            6: [entry("__doc__", "1")],
        }
    )
    snapshot = {"scopes": [{"name": "Locals", "variables": [function, mapping]}]}
    found = _resolve(tree, STOP, snapshot, parse_selector("normalize.__defaults__"))
    assert found["value"] == "([],)"
    found = _resolve(tree, STOP, snapshot, parse_selector("normalize.__defaults__[0]"))
    assert found["value"] == "[]"
    with pytest.raises(ValueError, match="'__doc__' not found in 'table'"):
        _resolve(tree, STOP, snapshot, parse_selector("table.__doc__"))
    with pytest.raises(ValueError, match="'__code__' not found in 'normalize'"):
        _resolve(tree, STOP, snapshot, parse_selector("normalize.__code__"))


def test_inspect_and_get_text_name_the_frame_and_identities():
    from peeky.cli import _get_lines, _read_frame, _variables_text

    snapshot = {
        "frame": 1,
        "location": "app.py:9",
        "function": "main",
        "innermost": {"location": "app.py:4", "function": "normalize"},
        "frames": [{"line": 4}, {"line": 9}],
        "scopes": [
            {
                "name": "Locals",
                "variables": [
                    {"name": "a", "type": "list", "value": "[1]", "identity": "list@8"},
                ],
                "truncated": True,
                "total": 70,
            },
            {"name": "Globals", "globals": True, "variables": []},
        ],
    }
    text = _variables_text(snapshot)
    assert text.startswith("stopped at app.py:4 in normalize\nframe 1: main (app.py:9)\n")
    assert "a: list = [1]  [id list@8]" in text
    assert "and 69 more, not listed or readable" in text
    assert "[Globals] (module globals, read-only)" in text
    row = {"selector": "a", "type": "list", "value": "[1]", "identity": "list@8"}
    assert _get_lines({**row, "scope": "globals"}) == [
        "a: list = [1]  [id list@8]  [module global]"
    ]
    assert _read_frame({"frame": 1, "function": "main", "location": "app.py:9"}) == (
        "frame 1: main (app.py:9)"
    )
