"""Nothing is read through a C or C++ pointer one past an array's end (#336).

With `long queue[4]` and `long *end = queue + 4;`, `session get end` listed `end[0]` with the
stack's next bytes as its value, and `--capture 'end[0]'` stored them: the address one past an
array's end is one C lets a pointer hold, but nothing there belongs to the array.
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from peeky.cli import _get_lines, _returned_text
from peeky.native import HELD_EXTENTS, NativeSession
from peeky.native_previews import Typed
from peeky.probe import _capture, _returned_row, _table, past_end, past_end_text, pointed_text
from peeky.worker import DebugWorker

LONG = {"pointee": 1, "element_size": 8}
QUEUE = {"address": 0x1000, "length": 4, "element": 1, "element_size": 8}


def at(address):
    return f"0x{address:016x}"


@pytest.mark.parametrize(
    "value, pointer, found",
    [
        (at(0x1020), LONG, {"selector": "queue", "length": 4}),
        # Inside the array, before it, two past its end, between elements: none.
        (at(0x1018), LONG, None),
        (at(0x1000), LONG, None),
        (at(0xFF8), LONG, None),
        (at(0x1028), LONG, None),
        (at(0x101C), LONG, None),
        # A pointer to another type, or to one of another size, proves nothing.
        (at(0x1020), {"pointee": 2, "element_size": 8}, None),
        (at(0x1020), {"pointee": 1, "element_size": 4}, None),
        (f'{at(0x1020)} "text"', LONG, {"selector": "queue", "length": 4}),
        ("0x0000000000000000", LONG, None),
        ("<error: no value>", LONG, None),
    ],
)
def test_one_past_an_arrays_end_is_told_by_address_and_type(value, pointer, found):
    assert past_end(value, pointer, [("queue", QUEUE, {})]) == found


def test_one_past_a_single_object_is_no_arrays_end():
    # `&x + 1`: one of a type often sits right after another (`long a, b;`), so a pointer at
    # `b` would read as one past `a`; a single object never withholds.
    after = {"address": 0x1000, "element": 1, "element_size": 8}
    assert past_end(at(0x1008), LONG, [("x", after, {})]) is None


def test_what_else_starts_there_is_named_but_not_read():
    after = {"address": 0x1020, "element": 1, "element_size": 8}
    other = {"address": 0x1020, "element": 2, "element_size": 8}
    following = {**QUEUE, "address": 0x1020}
    bases = [
        ("queue", QUEUE, {}),
        ("after", after, {}),
        ("other", other, {}),
        ("b", following, {}),
        ("far", after, {"frame": 1, "function": "main"}),
    ]
    # Another type at that address is not what the pointer would read; a frame other than
    # the array's is left out.
    assert past_end(at(0x1020), LONG, bases) == {
        "selector": "queue",
        "length": 4,
        "also": ["after", "b[0]"],
    }
    # The array's own frame comes first, with its place.
    caller = [("queue", QUEUE, {"frame": 2, "function": "main"}), ("t", after, {})]
    assert past_end(at(0x1020), LONG, caller) == {
        "selector": "queue",
        "length": 4,
        "frame": 2,
        "function": "main",
    }


def test_the_reason_says_why_and_what_to_read():
    past = {"selector": "queue", "length": 4}
    assert past_end_text("end", past) == (
        "`end` holds the address one past the end of `queue`, and nothing there belongs to "
        "`queue`; read `queue` by name"
    )
    caller = {**past, "frame": 1, "function": "main"}
    assert past_end_text("end", caller) == (
        "`end` holds the address one past the end of `queue` in main (frame 1), and nothing "
        "there belongs to `queue`; read `queue` in main (`session get --frame 1`)"
    )
    assert past_end_text("e", {**past, "also": ["after"]}).endswith(
        "; `after` starts there too, but nothing shows `e` points at it: read `after` or "
        "`queue` by name"
    )
    assert past_end_text("e", {**past, "also": ["b[0]", "c"]}).endswith(
        "; `b[0]`, `c` start there too, but nothing shows `e` points at them: read `b[0]`, `c` "
        "or `queue` by name"
    )


def test_rows_say_where_the_address_is():
    past = {"selector": "queue", "length": 4}
    assert pointed_text({"past_end": past}) == "at the address one past queue's end"
    assert pointed_text({"past_end": {**past, "frame": 1, "function": "main"}}) == (
        "at the address one past queue's end in frame 1"
    )
    # Said once where a captured array already says it; else after what that says.
    same = {"selector": "queue", "index": 4, "length": 4}
    assert pointed_text({"past_end": past, "at_element_of": same}) == (
        "at the address one past queue's end"
    )
    other = {"selector": "b", "index": 0, "length": 4}
    assert pointed_text({"past_end": past, "at_element_of": other}) == (
        "at b[0]'s address; at the address one past queue's end"
    )
    # What else starts there, where no captured array says it.
    assert pointed_text({"past_end": {**past, "also": ["b[0]", "after"]}}) == (
        "at the address one past queue's end, also b[0]'s and after's"
    )


class Stop:
    """A worker whose stop has `end` one past `queue`'s end, `mid` inside it, `pend` one past
    an array of structs, and `s` holding `end` as a field."""

    def __init__(self):
        self.requests = []

    def dispatch(self, operation, args):
        self.requests.append(operation)
        if operation == "expand":
            assert args["handle"] == 9, "nothing past an array's end is expanded"
            mid = {"name": "*mid", "type": "long", "value": "2", "variablesReference": 0}
            return {"variables": [mid], "truncated": False}
        if operation == "preview":
            return {}
        assert operation == "inspect"
        past = {"selector": "queue", "length": 4}
        variables = [
            {"name": "end", "type": "long *", "value": at(0x1020), "past_end": past},
            {"name": "mid", "type": "long *", "value": at(0x1008), "variablesReference": 9},
            {"name": "pend", "type": "pt *", "value": at(0x2020), "past_end": past},
            {"name": "s", "type": "S", "value": "{end = 0x1020}", "variablesReference": 0},
        ]
        return {
            "frame": 0,
            "frames": [{"line": 23, "name": "main"}],
            "scopes": [{"name": "Locals", "variables": variables}],
        }


def test_nothing_is_read_through_it():
    worker = Stop()
    stop = {"session_id": "s", "stop_id": "t"}
    selectors = ("end", "end[0]", "pend.x", "pend[0].x", "mid[0]")
    captured = _capture(worker, stop, selectors, 1, "observed", pointers=True)
    rows = {row["selector"]: row for row in captured}
    assert rows["end"]["value"] == at(0x1020)
    assert rows["end"]["past_end"] == {"selector": "queue", "length": 4}
    assert rows["end[0]"]["error"] == (
        "end[0] not read: `end` holds the address one past the end of `queue`, and nothing "
        "there belongs to `queue`; read `queue` by name"
    )
    assert rows["pend.x"]["error"].startswith("pend.x not read: `pend` holds the address")
    assert rows["pend[0].x"]["error"].startswith("pend[0] not read: ")
    assert "value" not in rows["end[0]"] and rows["mid[0]"]["value"] == "2"
    assert worker.requests.count("expand") == 1
    # `session get` lists none of its children, and says why.
    (row,) = _capture(worker, stop, ("end",), 1, "observed", children=True, pointers=True)
    assert "children" not in row and row["children_error"].startswith(
        "none read: `end` holds the address one past the end of `queue`"
    )
    assert _get_lines(row) == [
        f"end: long * = {at(0x1020)}  [at the address one past queue's end]",
        "  <children: " + row["children_error"] + ">",
    ]
    table = _table(
        {"rows": [rows["end"]], "interventions": [], "status": "completed", "hits": 1, "seconds": 1}
    )
    assert f"{at(0x1020)}  [at the address one past queue's end]" in table


def test_a_child_one_past_an_arrays_end_says_so():
    row = {
        "selector": "s",
        "type": "S",
        "value": "{end = 0x1020}",
        "children": [
            {
                "name": "end",
                "selector": "s.end",
                "type": "long *",
                "value": at(0x1020),
                "past_end": {"selector": "queue", "length": 4},
            }
        ],
    }
    assert _get_lines(row)[1] == (
        f"  s.end: long * = {at(0x1020)}  [at the address one past queue's end]"
    )


GRAPH = {
    1: ("base", "long", "DW_ATE_signed", 8),
    2: ("base", "int", "DW_ATE_signed", 4),
    3: ("array", "", 1, (4,), False),  # long[4]
    4: (
        "record",
        "two",
        "struct",
        64,
        None,
        (("a", 3, 0, False, None, None), ("b", 3, 32, False, None, None)),
        (3, 3),
        False,
        False,
        None,
        (),
        "two",
        (),
    ),
    5: (
        "record",
        "ring",
        "struct",
        40,
        None,
        (
            ("buf", 3, 0, False, None, None),
            ("head", 2, 32, False, None, None),
            ("bits", 2, 36, True, None, 3),
            (None, 4, 40, False, None, None),
        ),
        (3, 2, 2),
        False,
        False,
        None,
        (),
        "ring",
        (),
    ),
    6: ("array", "", 5, (2,), False),  # ring[2]
    7: (
        "record",
        "u",
        "union",
        32,
        None,
        (("a", 3, None, False, None, None), ("b", 8, None, False, None, None)),
        (3, 8),
        False,
        False,
        None,
        (),
        "u",
        (),
    ),
    8: ("array", "", 2, (8,), False),  # int[8]
    9: ("pointer", "", 5, None),  # ring *
    10: (
        "record",
        "array<long, 4>",
        "struct",
        32,
        None,
        (("_M_elems", 3, 0, False, None, None),),
        (3,),
        False,
        False,
        "std",
        (),
        "std::array<long, 4>",
        (("type", "_Tp", 1, None), ("value", "_Nm", None, 4)),
    ),
    11: ("array", "", 1, (2, 2), False),  # long[2][2]
}


LONG_TYPE, INT_TYPE = "base long DW_ATE_signed 8", "base int DW_ATE_signed 4"


def record(name, members, size, kind="struct", declared=False):
    """A record node of a type graph: members are (name, type offset, byte offset)."""
    placed = tuple((m, t, at, False, None, None) for m, t, at in members)
    held = tuple(t for _, t, _ in members)
    return ("record", name, kind, size, None, placed, held, False, declared, None, (), name, ())


def test_a_type_is_told_alike_in_every_source_file():
    # Each compile unit has its own `long`: a pointer in one file, an array in another.
    graph = {
        **GRAPH,
        30: ("base", "long", "DW_ATE_signed", 8),
        31: ("qualifier", "const", 30),
        32: ("base", "double", "DW_ATE_float", 8),
        # `struct Cell { long x; }` in two files, and `struct Cell { double y; }` in a third.
        40: record("Cell", [("x", 1, 0)], 8),
        41: record("Cell", [("x", 30, 0)], 8),
        42: record("Cell", [("y", 32, 0)], 8),
        43: record("Cell", [("y", 30, 0)], 8),
        # `struct Node { long v; struct Node *next; }` in two files: a pointer is never
        # followed, so a type that points at itself is told too.
        44: record("Node", [("v", 1, 0), ("next", 45, 8)], 16),
        45: ("pointer", "", 44, None),
        46: record("Node", [("v", 30, 0), ("next", 47, 8)], 16),
        47: ("pointer", "", 46, None),
        48: record("Pair", [("a", 1, 0), ("b", 1, 8)], 16),
        49: record("Pair", [("a", 1, 0), ("b", 1, 16)], 24),
        50: record("Pair", [("a", 1, 0), ("b", 1, 12)], 16),
        51: record("Hold", [("p", 52, 0)], 8),
        52: ("pointer", "", 99, None),  # what it points at was not read
        53: record("Cell", [], 8, declared=True),
        54: ("enum", "Color", 4),
    }
    assert Typed(graph, 31).identity() == Typed(graph, 1).identity() == LONG_TYPE
    assert Typed(graph, 2).identity() != LONG_TYPE
    cell = Typed(graph, 40).identity()
    assert cell.startswith("struct Cell 8 ") and Typed(graph, 41).identity() == cell
    # One name, two layouts: a member's type, or its name, differs.
    assert Typed(graph, 42).identity() != cell and Typed(graph, 43).identity() != cell
    assert Typed(graph, 44).identity() == Typed(graph, 46).identity()
    assert Typed(graph, 48).identity() != Typed(graph, 50).identity()
    assert Typed(graph, 48).identity() != Typed(graph, 49).identity()
    # Unread, incomplete, an enum (whose values the walk does not read), an array: its
    # offset, the same type only within one file.
    assert [Typed(graph, n).identity() for n in (51, 53, 54, 3)] == [51, 53, 54, 3]
    assert Typed(GRAPH, 99).identity() is None


def held(offset, value, name="v", reached=False, kind=None, reference=None, graph=GRAPH):
    """``held_extents`` of a local of the type ``offset`` in ``graph``, shown as ``value``
    (lldb-dap's `T @ 0x…`, whose type is the local's)."""
    session = NativeSession.__new__(NativeSession)

    def typed(frame, steps, walked=False):
        found = Typed(graph, offset)
        return (found, reached, False, None) if walked else found

    session._typed = typed
    variable = {"value": value, "type": kind or value.partition(" @ ")[0]}
    if reference:
        variable["memoryReference"] = reference
    return session.held_extents(variable, {"id": 1}, name)


def test_a_local_holds_its_arrays_and_their_members_where_the_debug_information_puts_them():
    long8 = {"element": LONG_TYPE, "element_size": 8}
    assert held(3, "long[4] @ 0x1000", "queue") == [
        ("queue", {"address": 0x1000, "length": 4, **long8})
    ]
    found = dict(held(4, "two @ 0x1000", "t"))
    assert found["t.a"] == {"address": 0x1000, "length": 4, **long8}
    assert found["t.b"] == {"address": 0x1020, "length": 4, **long8}
    assert found["t"]["element"].startswith("struct two 64 ") and found["t"]["element_size"] == 64
    # A bitfield or an unnamed member is never placed.
    found = dict(held(5, "ring @ 0x2000", "r"))
    assert found["r.head"] == {"address": 0x2020, "element": INT_TYPE, "element_size": 4}
    assert set(found) == {"r", "r.buf", "r.head"}
    # An array of structs: each element's members, not the elements (the array names them).
    found = dict(held(6, "ring[2] @ 0x3000", "rs"))
    assert found["rs"]["length"] == 2 and found["rs[1].buf"]["address"] == 0x3028
    assert "rs[1]" not in found
    # A union's members are at its start.
    found = dict(held(7, "u @ 0x4000", "u"))
    assert found["u.a"]["address"] == found["u.b"]["address"] == 0x4000
    # A std::array is an array of its elements, where the debugger gives its address.
    found = held(10, "size=4", "arr", kind="std::array<long, 4>", reference="0x5000")
    assert found == [("arr", {"address": 0x5000, "length": 4, **long8})]
    assert held(10, "size=4", "arr", kind="std::array<long, 4>") == []
    # No address, a 2-D array, or a local reached through a reference: none.
    assert held(3, "[1, 2, 3, 4]", "queue") == []
    assert held(11, "long[2][2] @ 0x1000", "grid") == []
    assert held(4, "two @ 0x1000", "t", reached=True) == []


def test_cpp_this_holds_its_objects_members():
    found = dict(held(9, "0x0000000000006000", "this", kind="ring *"))
    assert found["this.buf"] == {
        "address": 0x6000,
        "length": 4,
        "element": LONG_TYPE,
        "element_size": 8,
    }
    assert "this" not in found


def test_a_local_holds_at_most_so_many():
    graph = {**GRAPH, 20: ("array", "", 5, (200,), False)}  # ring[200]
    found = held(20, "ring[200] @ 0x1000", "rs", graph=graph)
    # The members of its first 16 elements at most, and never more than HELD_EXTENTS.
    assert found[0][0] == "rs" and found[-1][0] == "rs[15].head"
    assert len(found) == 1 + 16 * 2 <= HELD_EXTENTS


def test_rust_reads_no_past_ends():
    from peeky.rust import RustSession

    rust = RustSession.__new__(RustSession)
    rust.target = SimpleNamespace(language="rust")
    native = NativeSession.__new__(NativeSession)
    native.target = SimpleNamespace(language="cpp")
    assert rust.checks_past_ends is False and native.checks_past_ends is True


class Backend:
    """lldb-dap at a stop in `total(const long *p, const long *end)` (frame id 1), called by
    `main` (frame id 2), which holds `queue`; frame id 3 is outside the program."""

    def __init__(self, source):
        self.calls = []
        self.frames = [
            {"id": 1, "name": "total", "line": 10, "source": {"path": source}},
            {"id": 2, "name": "main", "line": 24, "source": {"path": source}},
            {"id": 3, "name": "start", "line": 0, "source": {"path": "/usr/lib/dyld"}},
        ]
        self.selected = None
        # Frames whose next `variables` reply doesn't come in time.
        self.slow = set()
        self.locals = {
            1: [
                {
                    "name": "end",
                    "type": "const long *",
                    "value": at(0x1020),
                    "variablesReference": 5,
                },
                {"name": "p", "type": "const long *", "value": at(0x1008), "variablesReference": 6},
                {
                    "name": "tag",
                    "type": "char *",
                    "value": f'{at(0x1040)} "zz"',
                    "variablesReference": 7,
                },
                {"name": "s", "type": "long", "value": "3", "variablesReference": 0},
            ],
            2: [
                {
                    "name": "queue",
                    "type": "long[4]",
                    "value": "long[4] @ 0x1000",
                    "variablesReference": 8,
                },
                {
                    "name": "name",
                    "type": "char[8]",
                    "value": "char[8] @ 0x1038",
                    "variablesReference": 9,
                },
            ],
        }

    def request(self, operation, args, timeout=None):
        self.calls.append((operation, deepcopy(args)))
        if operation == "stackTrace":
            return {"stackFrames": deepcopy(self.frames)}
        if operation == "scopes":
            # lldb-dap's Locals scope reads the frame last asked.
            self.selected = args["frameId"]
            return {
                "scopes": [
                    {"name": "Locals", "presentationHint": "locals", "variablesReference": 1}
                ]
            }
        if operation == "variables":
            assert args["variablesReference"] == 1
            if self.selected in self.slow:
                self.slow.discard(self.selected)
                raise TimeoutError("no answer in time")
            return {"variables": deepcopy(self.locals[self.selected])}
        if operation == "setVariable":
            assert self.selected == 1, "a change is made in the frame it was asked for"
            return {"value": args["value"]}
        raise AssertionError(operation)


class Runtime:
    checks_past_ends = True

    def __init__(self, root):
        self.prepared = SimpleNamespace(workspace=root, source_root=root)
        self.lines = {"ends.c": {}}

    def assert_current(self):
        pass

    def validate_stop(self, frame):
        pass

    def local_scopes(self, scopes):
        return [scope for scope in scopes if scope.get("presentationHint") == "locals"]

    def value_extent(self, variable, frame, steps):
        kind = variable.get("type")
        if kind == "const long *":
            return dict(LONG)
        if kind == "char *":
            return {"pointee": 2, "element_size": 1}
        return None

    def held_extents(self, variable, frame, name):
        found = {
            "queue": QUEUE,
            "name": {"address": 0x1038, "length": 8, "element": 2, "element_size": 1},
        }
        return [(name, found[name])] if name in found else []

    def settable(self, variable, frame):
        return variable.get("type") == "long"

    def validate_value(self, value):
        pass

    def validate_variable(self, value, replacement):
        pass


@pytest.fixture
def stopped(tmp_path):
    (tmp_path / "ends.c").write_text("int main(void) { return 0; }\n")
    instance = DebugWorker(tmp_path)
    instance.dap = Backend(str(tmp_path / "ends.c"))
    instance.runtime = Runtime(tmp_path)
    instance.session, instance.stop_id, instance.state = "session", "stop", "stopped"
    instance.thread_id = 1
    instance.capabilities = {"supports_set_variable": True}
    return instance


def call(worker, operation, **kwargs):
    stop = {"session_id": "session", "stop_id": worker.stop_id}
    return worker.dispatch(operation, {**stop, **kwargs})


def callers_read(worker):
    """How often the worker asked for the caller's (`main`'s) scopes."""
    return sum(1 for op, args in worker.dap.calls if op == "scopes" and args["frameId"] == 2)


def ends(result):
    shown = {variable["name"]: variable for variable in result["scopes"][0]["variables"]}
    return shown["end"].get("past_end")


def test_the_worker_reads_nothing_one_past_a_callers_array(stopped):
    result = call(stopped, "inspect", frame_id=1)
    shown = {v["name"]: v for v in result["scopes"][0]["variables"]}
    # One past `main`'s `queue`: its address alone, with where it is, and no handle.
    assert shown["end"]["value"] == at(0x1020) and shown["end"]["variablesReference"] == 0
    assert shown["end"]["past_end"] == {
        "selector": "queue",
        "length": 4,
        "frame": 1,
        "function": "main",
    }
    # A `char *` one past `name`'s end: never the text LLDB read there.
    assert shown["tag"]["value"] == at(0x1040) and shown["tag"]["variablesReference"] == 0
    # Inside the array: as before.
    assert shown["p"]["variablesReference"] and "past_end" not in shown["p"]
    # The caller's locals were read once, then this frame's scopes asked again; frames
    # outside the program are never read.
    asked = [args.get("frameId") for operation, args in stopped.dap.calls if operation == "scopes"]
    assert asked[:3] == [1, 2, 1] and 3 not in asked
    # Its scopes are re-acquired, so a change is still made where it was asked for.
    scope = result["scopes"][0]["reference"]
    assert call(stopped, "set_variable", reference=scope, name="s", value="4")["after"] == "4"


def test_nothing_is_read_where_the_runtime_does_not_check(stopped):
    stopped.runtime.checks_past_ends = False
    result = call(stopped, "inspect", frame_id=1)
    shown = {v["name"]: v for v in result["scopes"][0]["variables"]}
    assert "past_end" not in shown["end"] and shown["end"]["variablesReference"]
    asked = [args.get("frameId") for operation, args in stopped.dap.calls if operation == "scopes"]
    assert asked == [1]


def test_a_container_one_past_an_arrays_end_has_its_count_never_read(stopped):
    # libstdc++'s unordered containers are counted through a pointer, `p->_M_h…`: never
    # one past an array's end.
    counted = []

    def unlisted_container(variable, frame, path, raw):
        counted.append(variable["name"])

    stopped.runtime.unlisted_container = unlisted_container
    call(stopped, "inspect", frame_id=1)
    assert "end" not in counted and "tag" not in counted and "p" in counted


class Returned:
    """A worker whose step out of `fill` returned `value` (of LLDB's type `kind`) to a frame
    whose arrays are ``bases``."""

    checks_past_ends = True

    def __init__(self, bases):
        self.bases = bases

    def returned(self, kind, value):
        worker = SimpleNamespace(runtime=self, stop_bases=lambda frame: self.bases)
        return DebugWorker.returned_past_end(worker, kind, value, {"id": 1})


def test_a_returned_c_string_one_past_a_character_arrays_end_shows_its_address():
    char, wide = "base char DW_ATE_signed_char 1", "base char32_t DW_ATE_UTF 4"
    stop = Returned(
        [
            ("name", {"address": 0x1000, "length": 8, "element": char, "element_size": 1}, {}),
            ("wide", {"address": 0x2000, "length": 2, "element": wide, "element_size": 4}, {}),
            (
                "longs",
                {"address": 0x3000, "length": 1, "element": LONG_TYPE, "element_size": 8},
                {},
            ),
        ]
    )
    found = {"selector": "name", "length": 8}
    assert stop.returned("char *", f'{at(0x1008)} "zz"') == found
    # Any character type of its size: C lets them alias.
    assert stop.returned("const unsigned char *", f'{at(0x1008)} "zz"') == found
    assert stop.returned("char32_t *", f'{at(0x2008)} U"z"')["selector"] == "wide"
    # Inside the array; no text read; not a character pointer; another type's end: none.
    assert stop.returned("char *", f'{at(0x1004)} "zz"') is None
    assert stop.returned("char *", at(0x1008)) is None
    assert stop.returned("long *", f'{at(0x3008)} "zz"') is None
    assert stop.returned("char *", f'{at(0x3008)} "zz"') is None
    assert stop.returned("char", "'a'") is None and stop.returned(None, at(0x1008)) is None

    returned = {"function": "fill", "type": "char *", "value": at(0x1008), "past_end": found}
    snapshot = {"returned": returned, "frames": [{"line": 16}], "location": "ret.c:16"}
    row = _returned_row({"hit": 1, "phase": "returned", "selector": "(return)"}, snapshot)
    assert row["past_end"] == found and row["value"] == at(0x1008)
    assert _returned_text(returned) == (
        f"fill returned: char * = {at(0x1008)}  [at the address one past name's end]"
    )


def test_a_stops_callers_are_read_once_however_often_it_is_inspected(stopped):
    assert ends(call(stopped, "inspect", frame_id=1))["selector"] == "queue"
    assert callers_read(stopped) == 1
    before = len(stopped.dap.calls)
    second = call(stopped, "inspect", frame_id=1)
    assert ends(second)["selector"] == "queue" and callers_read(stopped) == 1
    # Nothing was read but this frame's own scope: no scopes to give back or bind again.
    assert [op for op, _ in stopped.dap.calls[before:]] == ["stackTrace", "scopes", "variables"]
    # Another stop: its callers are read again.
    stopped.stop_id = "next"
    assert ends(call(stopped, "inspect", frame_id=1))["selector"] == "queue"
    assert callers_read(stopped) == 2


def test_a_caller_that_did_not_answer_is_read_again(stopped):
    stopped.dap.slow.add(2)
    first = call(stopped, "inspect", frame_id=1)
    # Its locals never came: nothing is said of them, and nothing may be changed.
    assert ends(first) is None and first["mutation_blocked"]
    assert ends(call(stopped, "inspect", frame_id=1))["selector"] == "queue"
    assert callers_read(stopped) == 2
