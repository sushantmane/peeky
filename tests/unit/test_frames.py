"""#104: caller frames, Python module globals and Python object identity, with fakes."""

from copy import deepcopy

import pytest

from peeky.adapters import PythonAdapter
from peeky.errors import classify
from peeky.probe import _capture, _resolve, parse_selector
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

    def view_variable(
        self,
        variable,
        fetch,
        read=None,
        *,
        frame=None,
        top=False,
        innermost=False,
        stop=None,
        siblings=None,
        ancestry=None,
    ):
        if top:
            self.innermost.append((frame["id"], variable["name"], innermost))
        return None

    def globals_proof(self, frame, listed, names, loaded_by=None):
        assert loaded_by == 1000.0
        self.innermost.append(("proof", tuple(names), None))
        bound = {"key": {"kind": "local", "owner": None}} if frame["name"] == "normalize" else {}
        return {"names": {name: bound.get(name) for name in names}}


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


def test_python_globals_are_listed_and_proven_only_on_request(worker):
    # Review of #104: every inspection listed the globals, running each one's repr in
    # the program, and computed the proof, even to read a local.
    snapshot = inspect(worker)
    assert [scope["name"] for scope in snapshot["scopes"]] == ["Locals"]
    assert snapshot["globals_available"] is True and "globals_proof" not in snapshot
    assert not any(args.get("variablesReference", 0) > 100 for _, args in worker.dap.calls)
    assert not any(entry[0] == "proof" for entry in worker.runtime.innermost)
    # Asked for by name: proven first, listed only for a name the function reads there.
    snapshot = inspect(worker, globals=["key"])
    assert [scope["name"] for scope in snapshot["scopes"]] == ["Locals"]
    assert snapshot["globals_proof"] == {"names": {"key": {"kind": "local", "owner": None}}}
    assert snapshot["globals"].startswith("not listed")
    assert not any(args.get("variablesReference", 0) > 100 for _, args in worker.dap.calls)
    snapshot = inspect(worker, globals=["LIMIT"])
    assert [scope["name"] for scope in snapshot["scopes"]] == ["Locals", "Globals"]
    assert snapshot["globals"] == "listed"
    worker.dap.frames[0]["source"]["path"] = "/elsewhere/app.py"
    snapshot = inspect(worker, globals=["LIMIT"])
    assert snapshot["globals_proof"] == {"unproven": "its source is outside the target root"}
    assert [scope["name"] for scope in snapshot["scopes"]] == ["Locals"]


def test_python_globals_are_listed_read_only_and_once_at_module_level(worker):
    snapshot = inspect(worker, globals=True)
    locals_, globals_ = snapshot["scopes"]
    assert globals_["name"] == "Globals" and globals_["globals"] is True
    assert "globals" not in locals_ and snapshot["globals"] == "listed"
    with pytest.raises(ValueError, match="Module globals cannot be changed") as caught:
        worker.dispatch(
            "set_variable",
            {**STOP, "reference": globals_["reference"], "name": "LIMIT", "value": "1"},
        )
    assert classify(caught.value)["code"] == "MUTATION_REJECTED"
    # A module's code runs with its globals as its locals: one scope, no proof needed.
    module = inspect(worker, frame=2, globals=True)
    assert [scope["name"] for scope in module["scopes"]] == ["Locals"]
    assert "globals_proof" not in module and "globals_available" not in module
    assert module["globals"] == "not listed: at module level the locals are the module's globals"
    for bad in ("yes", ["not a name"], ["x"] * 33, 1):
        with pytest.raises(ValueError, match="globals must be"):
            inspect(worker, globals=bad)


def test_python_values_carry_an_identity_but_scalars_and_groups_do_not(worker):
    # Review of #104: the number alone identifies the object; its class can change.
    snapshot = inspect(worker, frame=1, globals=True)
    a, b, n = snapshot["scopes"][0]["variables"]
    assert a["identity"] == b["identity"] == 8 and "identity" not in n
    group = snapshot["scopes"][1]["variables"][0]
    assert group["name"] == "special variables" and "identity" not in group
    assert snapshot["scopes"][1]["variables"][2]["identity"] == 9


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


def test_scopes_cut_at_the_bound_say_so_and_names_asked_for_are_listed_past_it(worker):
    worker.dap.locals[11] = [
        {"name": f"v{i:02}", "type": "int", "value": "1", "variablesReference": 0}
        for i in range(60)
    ]
    scope = inspect(worker)["scopes"][0]
    assert len(scope["variables"]) == 50 and scope["truncated"] and scope["total"] == 60
    snapshot = inspect(worker, names=["v58", "v01", "missing"])
    shown = [v["name"] for v in snapshot["scopes"][0]["variables"]]
    assert shown[:50] == [f"v{i:02}" for i in range(50)] and shown[50:] == ["v58"]
    assert snapshot["names"] == ["missing", "v01", "v58"]
    found = _resolve(worker, STOP, snapshot, parse_selector("v58"))
    assert found["name"] == "v58"


def test_a_missing_name_is_read_from_globals_only_when_proven_unshadowed(worker):
    snapshot = inspect(worker)
    found = _resolve(worker, STOP, snapshot, parse_selector("LIMIT"))
    assert found["value"] == "3" and found["scope"] == "globals"
    # In debugpy's `special variables` group of the Globals listing.
    assert _resolve(worker, STOP, snapshot, parse_selector("__name__"))["value"] == "'app'"
    with pytest.raises(ValueError, match="'key' is a local of normalize with no value"):
        _resolve(worker, STOP, snapshot, parse_selector("key"))
    snapshot = inspect(worker)
    snapshot["globals_proof"] = {"unproven": "its source could not be parsed"}
    with pytest.raises(ValueError) as caught:
        _resolve(worker, STOP, snapshot, parse_selector("LIMIT"))
    assert "It may be a module global" in str(caught.value)
    assert "could not prove" in str(caught.value) and "could not be parsed" in str(caught.value)
    assert str(caught.value).startswith("not found")
    snapshot = inspect(worker)
    with pytest.raises(ValueError, match=r"^not found: 'nothing' is neither a local nor a module"):
        _resolve(worker, STOP, snapshot, parse_selector("nothing"))


@pytest.mark.parametrize(
    "kind, owner, words",
    [
        ("local", None, "'X' is a local of f with no value at this line"),
        ("parameter", None, "'X' is a parameter of f, deleted before this line"),
        ("comprehension", None, "'X' is the variable of a comprehension in f"),
        ("free", "outer", "'X' is a variable of outer that f uses (a closure variable)"),
        ("enclosing", "outer", "'X' is a local of outer, the function around f, which f"),
        ("type parameter", "generic", "'X' is a type parameter of generic (PEP 695)"),
        ("class cell", None, "'__class__' is the class cell Python gives only a method"),
    ],
)
def test_each_reason_a_name_is_not_a_global_is_worded_for_what_binds_it(kind, owner, words):
    # Review of #104: an enclosing function's local, a type parameter and a method's
    # missing `__class__` were all called "a local not yet assigned".
    class Proven:
        def dispatch(self, operation, args):
            assert operation == "inspect" and args["globals"] == ["X"]
            how = {"kind": kind, "owner": owner}
            return {**snapshot, "globals_proof": {"names": {"X": how}}}

    snapshot = {"function": "f", "globals_available": True, "frames": [], "scopes": []}
    with pytest.raises(ValueError) as caught:
        _resolve(Proven(), STOP, dict(snapshot), parse_selector("X"))
    message = str(caught.value)
    assert message.startswith("not found: ") and words in message
    assert ("UnboundLocalError" in message) == (kind in ("local", "parameter"))


def test_a_caller_frames_values_are_never_changed_or_marked_settable(worker):
    # Review of #104: set_variable on a scope of `inspect --frame 1` changed the caller.
    worker.runtime.settable = lambda variable, frame: True
    caller = inspect(worker, frame=1)
    assert not any(v.get("settable") for v in caller["scopes"][0]["variables"])
    with pytest.raises(ValueError, match="in a caller's frame, whose values cannot") as caught:
        worker.dispatch(
            "set_variable",
            {**STOP, "reference": caller["scopes"][0]["reference"], "name": "n", "value": "4"},
        )
    assert classify(caught.value)["code"] == "MUTATION_REJECTED"
    assert not any(operation == "setVariable" for operation, _ in worker.dap.calls)
    own = inspect(worker)
    assert own["scopes"][0]["variables"][0]["settable"] is True


def test_a_frame_outside_the_program_is_named_by_its_index(worker):
    # Review of #104: the error named lldb-dap's frame id (`Frame 524291 (start)`).
    def validate_stop(frame):
        if frame["name"] != "normalize":
            raise RuntimeError("not a prepared source")

    object.__setattr__(worker.runtime, "validate_stop", validate_stop)
    worker.dap.frames[1]["source"] = {"path": "/usr/lib/dyld"}
    with pytest.raises(ValueError, match=r"^Frame 1 \(main\) is not in the prepared"):
        inspect(worker, frame=1)


@pytest.mark.parametrize("reason", ["step", "interrupted"])
def test_after_a_step_in_the_sources_a_frame_outside_them_is_refused_by_its_index(worker, reason):
    # After a step that stopped in the program's own code, a frame outside its sources was
    # refused as if the step had left them ("your code is no longer on the stack").
    (worker.root / "app.py").write_text("")
    worker.stop_reason, worker.stop_returns = reason, False
    worker.dap.frames[2].update(name="start", source={"path": "/usr/lib/dyld"})
    with pytest.raises(ValueError) as caught:
        inspect(worker, frame=2)
    assert str(caught.value) == (
        "Frame 2 (start) is not in the prepared program's sources: read one of your "
        "program's frames"
    )
    assert classify(caught.value)["code"] == "INVALID_REQUEST"
    assert not any(operation == "scopes" for operation, _ in worker.dap.calls)
    # The step's own stop and its callers in the sources are read as before.
    assert inspect(worker)["function"] == "normalize"
    assert inspect(worker, frame=1)["function"] == "main"


def test_after_a_step_out_of_the_sources_its_refusal_names_where_the_step_stopped(worker):
    (worker.root / "app.py").write_text("")
    worker.stop_reason, worker.stop_returns = "step", True
    worker.dap.frames[0].update(name="printf", source={"path": "/usr/lib/libc.c"})
    worker.dap.frames[2].update(name="start", source={"path": "/usr/lib/dyld"})
    with pytest.raises(ValueError) as caught:
        inspect(worker, frame=2)
    assert str(caught.value) == (
        "The step stopped outside the program's sources (in printf), and frame 2 (start) is "
        "outside them too; `session step --out` returns to your code, or `session continue`"
    )
    assert classify(caught.value)["code"] == "INVALID_REQUEST"
    with pytest.raises(ValueError, match=r"^The step stopped outside the program's sources "):
        inspect(worker)
    worker.stop_returns = False
    with pytest.raises(ValueError) as caught:
        inspect(worker)
    assert str(caught.value) == (
        "The step stopped outside the program's sources (in printf); your code is no longer "
        "on the stack: `session continue`"
    )
    assert not any(operation == "scopes" for operation, _ in worker.dap.calls)
    # Its caller in the sources is read.
    assert inspect(worker, frame=1)["function"] == "main"


def test_a_module_listing_of_only_debugpys_groups_reads_plainly():
    from peeky.probe import _visible

    snapshot = {
        "scopes": [
            {"name": "Locals", "variables": [{"name": "x"}]},
            {"name": "Globals", "globals": True, "variables": [dict(GROUP)]},
        ]
    }
    assert _visible(snapshot) == (
        "x; module globals: the names in debugpy's special variables (select them by name)"
    )
    cut = {"name": "Globals", "globals": True, "truncated": True, "total": 63}
    cut["variables"] = [dict(GROUP), *({"name": f"g{i:02}"} for i in range(49))]
    assert _visible({"scopes": [cut]}) == (
        "(none); module globals: g00, g01, g02, g03, g04, g05, g06, g07, g08, g09, g10, g11, "
        "g12, g13, g14, g15, g16, g17, g18, g19, g20, g21, g22, g23, g24, g25, g26, g27, "
        "g28, g29, g30, g31, g32, g33, g34, g35, g36, g37, g38, g39, ... (49 total) and the "
        "names in debugpy's special variables (select them by name) (Globals lists 50 of "
        "its 63 entries, debugpy's groups included)"
    )


def test_debugpys_groups_are_read_once_per_inspection():
    # Review of #104: every selector expanded a value's groups again.
    class Counting(Tree):
        def __init__(self, tree):
            super().__init__(tree)
            self.expanded = []

        def dispatch(self, operation, args):
            if operation == "inspect":
                return {
                    "frames": [{"line": 1}],
                    "scopes": [
                        {
                            "name": "Locals",
                            "variables": [entry("normalize", "<function>", "function", 1)],
                        }
                    ],
                }
            self.expanded.append(args["handle"])
            return super().dispatch(operation, args)

    worker = Counting(
        {
            1: [group("special variables", 2)],
            2: [entry("__defaults__", "([],)", "tuple"), entry("__name__", "'normalize'")],
        }
    )
    selectors = ("normalize.__defaults__", "normalize.__name__", "normalize.__doc__")
    rows = _capture(worker, STOP, selectors, 0, "observed")
    assert [row.get("value") for row in rows[:2]] == ["([],)", "'normalize'"]
    assert worker.expanded.count(2) == 1


def test_get_rows_name_the_frame_scope_and_identity(worker):
    rows = _capture(worker, STOP, ("a", "LIMIT"), 0, "observed", frame=1)
    assert rows[0]["frame"] == 1 and rows[0]["function"] == "main" and rows[0]["line"] == 9
    assert rows[0]["identity"] == 8 and "scope" not in rows[0]
    assert rows[1]["value"] == "3" and rows[1]["scope"] == "globals"
    assert "identity" not in rows[1]


NAMES = ("LIMIT", "TOTAL", "n", "w", "self", "size", "__class__", "print")


def classify_names(*args, **kwargs):
    from peeky.python_scopes import classify

    return classify(*args, **kwargs)


def scope_of(tmp_path, text, function, line, listed=()):
    path = tmp_path / "app.py"
    path.write_text(text)
    return classify_names(path, function, line, NAMES, listed)


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


def test_names_are_classified_by_the_compilers_scoping(tmp_path):
    names, reason = scope_of(tmp_path, SOURCE, "shadow", 7, ["n"])
    assert reason is None
    assert names["LIMIT"] == ("local", None) and names["n"] == ("parameter", None)
    assert names["w"] == ("comprehension", None) and names["TOTAL"] is None
    assert names["print"] is None and names["__class__"] == ("class cell", None)
    names, _ = scope_of(tmp_path, SOURCE, "declared", 15)
    assert names["TOTAL"] is None
    # The name is free in neither: still the enclosing function's, never the module's.
    names, _ = scope_of(tmp_path, SOURCE, "inner", 22)
    assert names["TOTAL"] == ("enclosing", "outer") and names["LIMIT"] is None
    names, _ = scope_of(tmp_path, SOURCE, "grow", 31, ["self", "__class__"])
    assert names["self"] == ("parameter", None) and names["size"] is None
    assert names["__class__"] == ("class cell used", None)


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
    time.sleep(0.01)
    started = time.time()
    assert classify_names(path, "shadow", 7, NAMES, ["n"], loaded_by=started)[1] is None
    os.utime(path, (started + 5, started + 5))
    names, why = classify_names(path, "shadow", 7, NAMES, ["n"], loaded_by=started)
    assert names is None and why == "its source file changed after the program started"
    # Review of #104: a file copied over it with an older mtime (`cp -p`) is changed too.
    os.utime(path, (started - 3600, started - 3600))
    names, why = classify_names(path, "shadow", 7, NAMES, ["n"], loaded_by=started)
    assert names is None and why == "its source file changed after the program started"


def test_the_index_keeps_no_syntax_tree_and_is_bounded_by_size(tmp_path, monkeypatch):
    # Review of #104: each cached parse of a 3.9 MB module held about 1.4 GB.
    import ast
    import symtable

    from peeky import python_scopes

    monkeypatch.setattr(python_scopes, "_INDEX", python_scopes.OrderedDict())
    monkeypatch.setattr(python_scopes, "_INDEX_BYTES", [0])
    paths = []
    for number in range(6):
        path = tmp_path / f"m{number}.py"
        path.write_text(SOURCE + "".join(f"\ndef f{i}(a):\n    return a\n" for i in range(50)))
        paths.append(path)
    assert classify_names(paths[0], "shadow", 7, NAMES)[1] is None
    index, size = next(iter(python_scopes._INDEX.values()))
    pending = [index]
    while pending:
        item = pending.pop()
        assert not isinstance(item, (ast.AST, symtable.SymbolTable))
        if isinstance(item, dict):
            pending.extend([*item.keys(), *item.values()])
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)
    monkeypatch.setattr(python_scopes, "MAX_INDEX_BYTES", size * 3)
    for path in paths:
        assert classify_names(path, "shadow", 7, NAMES)[1] is None
    assert len(python_scopes._INDEX) == 3 and python_scopes._INDEX_BYTES[0] <= size * 3
    # A cached index is reused without parsing again.
    monkeypatch.setattr(symtable, "symtable", None)
    assert classify_names(paths[-1], "shadow", 7, NAMES)[1] is None


def test_unreadable_or_unparsable_sources_prove_nothing(tmp_path):
    assert classify_names(tmp_path / "gone.py", "f", 1, NAMES)[1].startswith(
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
                    {"name": "a", "type": "list", "value": "[1]", "identity": 8},
                ],
                "truncated": True,
                "total": 70,
            },
            {"name": "Globals", "globals": True, "variables": []},
        ],
    }
    text = _variables_text(snapshot)
    assert text.startswith("stopped at app.py:4 in normalize\nframe 1: main (app.py:9)\n")
    assert "a: list = [1]  [id 8]" in text
    assert "and 69 more, not listed (at most 50 per scope): `session get NAME`" in text
    assert "[Globals] (module globals, read-only)" in text
    row = {"selector": "a", "type": "list", "value": "[1]", "identity": 8}
    assert _get_lines({**row, "scope": "globals"}) == ["a: list = [1]  [id 8]  [module global]"]
    assert _read_frame({"frame": 1, "function": "main", "location": "app.py:9"}) == (
        "frame 1: main (app.py:9)"
    )


COMPREHENSIONS = """\
X = "global-x"
Y = "global-y"


def f(data):
    squares = [X for X in range(3)]
    total = sum(Y for Y in range(3))
    inside = [
        X * 2
        for X in data
    ]
    return X, Y, squares, total, inside


def outer():
    X = "outer"

    def declared():
        global X
        return X

    return declared()
"""


def test_a_name_the_function_reads_from_its_globals_is_global_whatever_its_comprehensions_bind(
    tmp_path,
):
    # Review of #104 (round 2): `Y` of a generator expression, which runs in its own
    # frame, was called "the variable of a comprehension in f" where f reads the global.
    path = tmp_path / "c2.py"
    path.write_text(COMPREHENSIONS)
    names, why = classify_names(path, "f", 12, ("X", "Y", "squares"), ("data", "squares"))
    assert why is None and names == {"X": None, "Y": None, "squares": ("local", None)}
    # Stopped inside an inlined comprehension (3.12+), its variable is listed: a name the
    # source binds there, not a sign the file changed.
    names, why = classify_names(path, "f", 9, ("Y", "data"), ("data", "squares", "X"))
    assert why is None and names == {"Y": None, "data": ("parameter", None)}
    # Declared global in a function nested in one that binds it.
    names, why = classify_names(path, "declared", 20, ("X",))
    assert why is None and names == {"X": None}
