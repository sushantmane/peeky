"""Which element of a captured array a C or C++ pointer holds the address of (#302).

Eleventh evaluation: tides' `turn const int * 0x…7040` showed nothing that tied it to
`level`, the agent divided by a hard-coded `sizeof(int)` (the store had no element size),
and `queries diff` sent it to address() for `turn` and `level`; grades' `const std::string
&name`, whose text agreed in both runs, was sent there too and ranked as a near-real
difference.
"""

import sqlite3
from types import SimpleNamespace

import pytest

from peeky import evidence
from peeky.cli import _get_lines
from peeky.native import NativeSession, own_address
from peeky.native_previews import Typed
from peeky.probe import (
    OFFSET_CAP,
    _capture,
    _pointed,
    _table,
    pointed_element,
    pointed_text,
    pointer_offset,
)
from peeky.rust import RustSession
from peeky.worker import DebugWorker

GRAPH = {
    1: ("base", "int", "DW_ATE_signed", 4),
    2: ("base", "char", "DW_ATE_signed_char", 1),
    3: ("qualifier", "const", 1),
    4: ("pointer", "", 3, None),  # const int *, as clang leaves its size
    5: ("array", "", 1, (96,), False),  # int[96]
    6: ("typedef", "reading", 1),
    7: ("array", "", 6, (6,), False),  # reading[6]
    8: ("array", "", 1, None, True),  # a variable-length array
    9: ("array", "", 1, (3, 4), False),  # int[3][4]
    10: ("pointer", "", 2, 8),  # char *, as GCC gives its size
    11: (
        "record",
        "Pt",
        "struct",
        8,
        None,
        (("x", 1, 0, False, None, None),),
        (1,),
        False,
        False,
        None,
        (),
        "Pt",
        (),
    ),
    12: ("pointer", "", 11, None),  # Pt *
    13: ("record", "opaque", "struct", None, None, (), (), False, True, None, (), "opaque", ()),
    14: ("pointer", "", 13, None),  # opaque *
    15: ("pointer", "", None, None),  # void *
    16: ("reference", "", 1),  # int &
    17: ("array", "", 1, (0,), False),  # int[0]
    18: ("enum", "Color", 4),
    19: ("array", "", 4, (8,), False),  # const int *[8]
    20: ("pointer", "", 4, None),  # const int **
}


def typed(offset, bits=None):
    return Typed(GRAPH, offset, bits=bits)


def test_byte_sizes_come_from_the_debug_information():
    assert [typed(n).byte_size() for n in (1, 2, 3, 4, 10, 11, 18)] == [4, 1, 4, 8, 8, 8, 4]
    assert typed(5).byte_size() == 384 and typed(9).byte_size() == 48
    # Not known: a variable-length array, an incomplete type, `void *`'s target, a
    # reference, a bitfield, an empty array.
    assert [typed(n).byte_size() for n in (8, 13, 16, 17)] == [None] * 4
    assert typed(15).byte_size() is None and typed(1, bits=3).byte_size() is None


def session(offset, storages=frozenset({"automatic"})):
    """A NativeSession whose `_typed` reads ``offset`` in GRAPH, for any value, each local
    declared with ``storages``."""
    return SimpleNamespace(
        _typed=lambda frame, steps: None if offset is None else typed(offset),
        _storages=lambda frame, root: set(storages),
    )


def extent(offset, value="", kind="", reference=None, steps=("v",), **storages):
    variable = {"value": value, "type": kind}
    if reference:
        variable["memoryReference"] = reference
    return NativeSession.value_extent(session(offset, **storages), variable, {"id": 1}, steps)


def test_an_array_says_where_it_is_its_length_and_its_element():
    found = extent(5, "int[96] @ 0x16d26b038", "int[96]")
    assert found == {"length": 96, "address": 0x16D26B038, "element": 1, "element_size": 4}
    # lldb-dap 20 on gives its address as memoryReference too: both must agree.
    assert extent(5, "int[96] @ 0x16d26b038", "int[96]", "0x16D26B038") == found
    assert extent(5, "[300, 340]", "int[96]", "0x16D26B038") == found
    assert extent(5, "int[96] @ 0x16d26b038", "int[96]", "0x16D26B040") is None
    # A typedef's element is the type under it.
    assert extent(7, "reading[6] @ 0x1000", "reading[6]")["element"] == 1
    # No address at all, a variable-length or 2-D or empty array: nothing is told.
    assert extent(5, "[300, 340]", "int[96]") is None
    for offset in (8, 9, 17):
        assert extent(offset, "int[] @ 0x1000", "int[]") is None


def test_a_pointer_says_what_it_points_at_and_its_size():
    # `const int *`: the type under its qualifier, as an `int` array's element is.
    assert extent(4, "0x000000016d26b040") == {"pointee": 1, "element_size": 4}
    assert extent(20, "0x000000016d26b040") == {"pointee": 4, "element_size": 8}
    # A parameter (the array a caller passed) is the first pointer offsets count from; a
    # name also declared as a local, or a field, is not one.
    assert extent(4, "0x1000", storages={"parameter"})["parameter"] is True
    assert "parameter" not in extent(4, "0x1000", storages={"parameter", "automatic"})
    assert "parameter" not in extent(4, "0x1000", steps=("s", "p"), storages={"parameter"})
    # What an incomplete type's pointer or `void *` points at has no known size.
    assert extent(14, "0x1000") is None and extent(15, "0x1000") is None
    # A reference is no pointer here, and a type the build does not give is unknown.
    assert extent(16, "0x1000") is None and extent(None, "0x1000") is None
    # An object (one element) where its address is known; never a bitfield.
    assert extent(11, "Pt @ 0x1000", "Pt") == {"address": 0x1000, "element": 11, "element_size": 8}
    bitfield = SimpleNamespace(_typed=lambda frame, steps: typed(1, bits=3))
    assert NativeSession.value_extent(bitfield, {"memoryReference": "0x10"}, {}, ["b"]) is None


def test_own_address_needs_one_answer():
    assert own_address({"memoryReference": "0x1000"}) == 0x1000
    assert own_address({"value": "Pt @ 0x1000", "type": "Pt"}) == 0x1000
    # The text of another type, a null address, a malformed reference: none.
    assert own_address({"value": "Pt @ 0x1000", "type": "Other"}) is None
    assert own_address({"memoryReference": "0x0"}) is None
    assert own_address({"memoryReference": "level"}) is None


INT = {"pointee": 1, "element_size": 4}
LEVEL = {"address": 0x1000, "length": 96, "element": 1, "element_size": 4}


@pytest.mark.parametrize(
    "value, pointer, base, found",
    [
        ("0x0000000000001018", INT, LEVEL, 6),
        ("0x0000000000001000", INT, LEVEL, 0),
        ("0x0000000000001018 {x = 1}", INT, LEVEL, 6),
        # One past the end: a pointer may hold it, never read it.
        ("0x0000000000001180", INT, LEVEL, 96),
        # Past that, or before the first element: not this array's.
        ("0x0000000000001184", INT, LEVEL, None),
        ("0x0000000000000ffc", INT, LEVEL, None),
        # Not a whole number of elements from the first.
        ("0x0000000000001002", INT, LEVEL, None),
        # A `char *` into an `int` array points at no element of it.
        ("0x0000000000001018", {"pointee": 2, "element_size": 1}, LEVEL, None),
        # The same type with another size (never in one build) proves nothing.
        ("0x0000000000001018", {"pointee": 1, "element_size": 8}, LEVEL, None),
        # The base is a pointer itself (a parameter): its extent has no address.
        ("0x0000000000001018", INT, {"pointee": 1, "element_size": 4}, None),
        ("0x0000000000000000", INT, LEVEL, None),
        ('"0x0000000000001018"', INT, LEVEL, None),
        (None, INT, LEVEL, None),
        # An object is one element: its own address, or one past it.
        ("0x0000000000001000", INT, {"address": 0x1000, "element": 1, "element_size": 4}, 0),
        ("0x0000000000001004", INT, {"address": 0x1000, "element": 1, "element_size": 4}, 1),
        ("0x0000000000001008", INT, {"address": 0x1000, "element": 1, "element_size": 4}, None),
    ],
)
def test_the_offset_is_proven_or_not_told(value, pointer, base, found):
    assert pointed_element(value, pointer, base) == found


def row(selector, value, kind, **more):
    return {
        "hit": 1,
        "phase": "observed",
        "selector": selector,
        "value": value,
        "type": kind,
        **more,
    }


def test_rows_of_one_stop_say_which_element_and_its_size():
    rows = [
        row("turn", "0x0000000000001018", "const int *"),
        row("level", "[300, 340]", "int[96]"),
        row("count", "2", "int"),
    ]
    _pointed(rows, {0: INT, 1: LEVEL})
    assert rows[0]["at_element_of"] == {"selector": "level", "index": 6, "length": 96}
    assert rows[0]["element_size"] == rows[1]["element_size"] == 4
    assert "at_element_of" not in rows[1] and "element_size" not in rows[2]


def test_a_pointer_base_a_qualified_value_or_itself_proves_nothing():
    # tides' `find_waters`: `level` is a `const int *` parameter, whose length is unknown.
    rows = [row("turn", "0x0000000000001018", "const int *"), row("level", "0x1000", "const int *")]
    _pointed(rows, {0: INT, 1: dict(INT)})
    assert not any("at_element_of" in each for each in rows)
    assert [each["element_size"] for each in rows] == [4, 4]
    # Only its offset by address, which names no element.
    assert rows[1]["offset_from"] == {"selector": "turn", "offset": -6}
    for qualified in ({"note": "leftovers"}, {"view": "stale"}, {"stale": True}):
        rows = [
            row("p", "0x0000000000001018", "int *", **qualified),
            row("level", "[1]", "int[96]"),
        ]
        _pointed(rows, {0: INT, 1: LEVEL})
        assert "at_element_of" not in rows[0], qualified
        rows = [
            row("p", "0x0000000000001018", "int *"),
            row("level", "[1]", "int[96]", **qualified),
        ]
        _pointed(rows, {0: INT, 1: LEVEL})
        assert "at_element_of" not in rows[0], qualified
    unread = [row("p", "0x0000000000001018", "int *"), {"selector": "level", "unavailable": True}]
    _pointed(unread, {0: INT, 1: LEVEL})
    assert "at_element_of" not in unread[0]


@pytest.mark.parametrize(
    "second",
    [
        # `a` ends where `b` starts: the address one past a's end is b[0]'s.
        {"address": 0x1010, "length": 4, "element": 1, "element_size": 4},
        # A union's two arrays share one address: u.a[0]'s is u.b[0]'s.
        {"address": 0x1000, "length": 8, "element": 1, "element_size": 4},
    ],
)
def test_an_address_two_captured_bases_share_names_neither(second):
    # Review of PR #312: the first in capture order was named, as if the address told
    # which object the pointer designates.
    a = {"address": 0x1000, "length": 4, "element": 1, "element_size": 4}
    at = 0x1010 if second["address"] == 0x1010 else 0x1000
    rows = [row("p", f"0x{at:016x}", "int *"), row("a", "[1]", "int[4]"), row("b", "[2]", "int[4]")]
    _pointed(rows, {0: INT, 1: a, 2: second})
    assert "at_element_of" not in rows[0], rows
    # One of them alone says it, as an address.
    rows = rows[:2]
    _pointed(rows, {0: INT, 1: a})
    index = 4 if at == 0x1010 else 0
    assert rows[0]["at_element_of"] == {"selector": "a", "index": index, "length": 4}
    assert pointed_text(rows[0]) == (
        "at the address one past a's end" if index == 4 else "at a[0]'s address"
    )


PARAMETER = {**INT, "parameter": True}


@pytest.mark.parametrize(
    "value, pointer, base_value, base, found",
    [
        ("0x0000000000001018", INT, "0x0000000000001000", INT, 6),
        # Before the base: negative, and nothing is read there.
        ("0x0000000000000ff8", INT, "0x0000000000001000", INT, -2),
        ("0x0000000000001000", INT, "0x0000000000001000 {x = 1}", INT, 0),
        (
            '0x0000000000001003 "lo"',
            {"pointee": 2, "element_size": 1},
            '0x1000 "hello"',
            {"pointee": 2, "element_size": 1},
            3,
        ),
        # Another type, or another size, is not counted.
        ("0x0000000000001018", INT, "0x0000000000001000", {"pointee": 2, "element_size": 1}, None),
        ("0x0000000000001018", INT, "0x0000000000001000", {"pointee": 1, "element_size": 8}, None),
        # Not a whole number of elements apart.
        ("0x0000000000001002", INT, "0x0000000000001000", INT, None),
        # Further apart than the cap (the stack against the heap): unrelated.
        (f"0x{0x1000 + 4 * OFFSET_CAP:016x}", INT, "0x0000000000001000", INT, OFFSET_CAP),
        (f"0x{0x1000 + 4 * OFFSET_CAP + 4:016x}", INT, "0x0000000000001000", INT, None),
        (
            f"0x{0x1000 + 4 * OFFSET_CAP:016x}",
            INT,
            f"0x{0x1000 + 8 * OFFSET_CAP + 4:016x}",
            INT,
            None,
        ),
        # Null, or no address read.
        ("0x0000000000000000", INT, "0x0000000000001000", INT, None),
        ("0x0000000000001018", INT, "0x0000000000000000", INT, None),
        ("0x0000000000001018", INT, "<not found>", INT, None),
        ("0x0000000000001018", INT, None, INT, None),
    ],
)
def test_an_offset_by_address_is_counted_or_not_told(value, pointer, base_value, base, found):
    assert pointer_offset(value, pointer, base_value, base) == found


def test_pointers_into_an_unproven_array_say_their_offset_from_a_parameter():
    # tides' line 40: `turn` and `p` walk `level`, a parameter (`const int *level`).
    rows = [
        row("turn", "0x0000000000001018", "const int *"),
        row("p", "0x000000000000101c", "const int *"),
        row("level", "0x0000000000001000", "const int *"),
        row("trend", "1", "int"),
    ]
    _pointed(rows, {0: INT, 1: INT, 2: PARAMETER})
    assert rows[0]["offset_from"] == {"selector": "level", "offset": 6}
    assert rows[1]["offset_from"] == {"selector": "level", "offset": 7}
    assert not any("offset_from" in each or "at_element_of" in each for each in rows[2:])
    # Shown as arithmetic, never an element.
    assert pointed_text(rows[0]) == "level + 6 by address; same array not proven"
    assert pointed_text({"offset_from": {"selector": "level", "offset": -2}}) == (
        "level - 2 by address; same array not proven"
    )
    # With no parameter among them, the first captured is the base.
    rows = [row("p", "0x000000000000101c", "const int *"), row("turn", "0x1018", "const int *")]
    _pointed(rows, {0: INT, 1: INT})
    assert "offset_from" not in rows[0]
    assert rows[1]["offset_from"] == {"selector": "p", "offset": -1}


def test_an_offset_needs_a_captured_base_of_its_type_and_a_plain_value():
    # The base not captured: one pointer alone, or beside values of other types.
    rows = [row("turn", "0x0000000000001018", "const int *"), row("bytes", "0x1000", "char *")]
    _pointed(rows, {0: INT, 1: {"pointee": 2, "element_size": 1}})
    assert not any("offset_from" in each for each in rows)
    # A base with a note, and one over the cap, say nothing.
    rows = [row("level", "0x1000", "const int *", note="leftovers"), row("p", "0x1018", "int *")]
    _pointed(rows, {0: PARAMETER, 1: INT})
    assert "offset_from" not in rows[1]
    far = f"0x{0x1000 + 4 * OFFSET_CAP + 4:016x}"
    rows = [row("level", "0x0000000000001000", "const int *"), row("heap", far, "int *")]
    _pointed(rows, {0: PARAMETER, 1: INT})
    assert "offset_from" not in rows[1]
    # A proven element says that, never an offset too.
    rows = [
        row("level", "0x0000000000001000", "const int *"),
        row("p", "0x0000000000002008", "int *"),
        row("data", "[1, 2]", "int[4]"),
    ]
    _pointed(rows, {0: PARAMETER, 1: INT, 2: {**LEVEL, "address": 0x2000, "length": 4}})
    assert rows[1]["at_element_of"] == {"selector": "data", "index": 2, "length": 4}
    assert "offset_from" not in rows[1]


def test_tables_and_session_get_show_the_element():
    assert pointed_text({"at_element_of": {"selector": "level", "index": 6, "length": 96}}) == (
        "at level[6]'s address"
    )
    assert pointed_text({"at_element_of": {"selector": "level", "index": 96, "length": 96}}) == (
        "at the address one past level's end"
    )
    assert pointed_text({"at_element_of": {"selector": "x", "index": 0}}) == "at x's address"
    assert (
        pointed_text({"at_element_of": {"selector": "x", "index": 1}})
        == "at the address one past x's end"
    )
    assert pointed_text({}) == ""
    shown = row(
        "turn",
        "0x0000000000001018",
        "const int *",
        element_size=4,
        at_element_of={"selector": "level", "index": 6, "length": 96},
    )
    table = _table(
        {"rows": [shown], "interventions": [], "status": "completed", "hits": 1, "seconds": 1}
    )
    assert "0x0000000000001018  [at level[6]'s address]" in table
    assert _get_lines(shown)[0] == "turn: const int * = 0x0000000000001018  [at level[6]'s address]"


class Frames:
    """A worker whose stop has `p` (a pointer into its caller's array) in frame 0 and
    `level` and `q` in frame 1, each with what its address can tell."""

    def __init__(self):
        self.requests, self.pointers = [], False

    def dispatch(self, operation, args):
        assert operation == "inspect"
        self.requests.append(args)
        frame = args.get("frame", 0)
        # With `pointers`, frame 1's `level` is a `const int *` parameter instead.
        level = (
            {"value": "0x0000000000001008", "type": "const int *", "extent": PARAMETER}
            if self.pointers
            else {"value": "[1, 2]", "type": "int[96]", "extent": LEVEL}
        )
        names = {
            0: [{"name": "p", "value": "0x0000000000001018", "type": "int *", "extent": INT}],
            1: [
                {"name": "level", **level},
                {"name": "q", "value": "0x0000000000001008", "type": "int *", "extent": INT},
            ],
        }[frame]
        variables = [{**variable, "variablesReference": 0} for variable in names]
        return {
            "frame": frame,
            "frames": [{"line": 4, "name": "peak"}, {"line": 9, "name": "main"}],
            "scopes": [{"name": "Locals", "variables": variables}],
        }


def test_only_values_of_one_frame_at_one_stop_are_compared():
    worker = Frames()
    stop = {"session_id": "s", "stop_id": "t"}
    (pointer,) = _capture(worker, stop, ("p",), 1, "observed")
    (base,) = _capture(worker, stop, ("level",), 1, "observed", frame=1)
    assert "at_element_of" not in pointer and "at_element_of" not in base
    assert pointer["element_size"] == base["element_size"] == 4
    # The roots read say what their addresses can tell, as they are previewed.
    assert worker.requests[0]["extents"] == ["p"]
    rows = _capture(worker, stop, ("q", "level"), 1, "observed", frame=1)
    assert rows[0]["at_element_of"] == {"selector": "level", "index": 2, "length": 96}
    # Pointers of two frames are never counted from each other either.
    worker.pointers = True
    (pointer,) = _capture(worker, stop, ("p",), 1, "observed")
    (other,) = _capture(worker, stop, ("q",), 1, "observed", frame=1)
    assert "offset_from" not in pointer and "offset_from" not in other
    rows = _capture(worker, stop, ("level", "q"), 1, "observed", frame=1)
    assert rows[1]["offset_from"] == {"selector": "level", "offset": 0}


def test_the_worker_tells_extents_only_for_the_roots_asked_and_c_or_cpp():
    told = []

    def hook(variable, frame, steps):
        told.append(steps)
        return {"pointee": 1, "element_size": 4}

    worker = SimpleNamespace(runtime=SimpleNamespace(value_extent=hook), extents=frozenset({"a"}))

    def asked(ancestry, name, kind):
        return DebugWorker.value_extent(worker, {}, {}, ancestry, name, kind)

    assert asked((), "a", "int *") == {"pointee": 1, "element_size": 4}
    assert asked((), "b", "int *") is None
    # Read through a root asked for: only a pointer or an array says.
    assert asked((("a", "S"),), "next", "S *const") is not None
    assert asked((("a", "S"),), "rows", "int[4]") is not None
    assert asked((("a", "S"),), "n", "int") is None
    assert told == [["a"], ["a", "next"], ["a", "rows"]]
    worker.extents = False
    assert asked((), "a", "int *") is None
    # Rust: LLDB shows references and Boxes as pointers; nothing is told there.
    assert RustSession.value_extent is None
    worker.extents, worker.runtime = True, SimpleNamespace(value_extent=None)
    assert asked((), "a", "int *") is None


def captured(rows, language="c", output="TIDES:H 03:00\n"):
    """A probe result of observed rows (hit, selector, type, value, extra keys)."""
    return {
        "probe": {
            "program": "tides.c",
            "line": 40,
            "capture": list(dict.fromkeys(each[1] for each in rows)),
            "set": [],
            "hits": "all",
            "args": ["x"],
        },
        "status": "completed",
        "hits": max(each[0] for each in rows),
        "seconds": 1.0,
        "output": output,
        "rows": [
            dict(
                hit=hit,
                phase="observed",
                line=40,
                selector=selector,
                type=kind,
                value=value,
                **(more[0] if more else {}),
            )
            for hit, selector, kind, value, *more in rows
        ],
        "interventions": [],
        "warnings": [],
    }


def into(index):
    return {"element_size": 4, "at_element_of": {"selector": "level", "index": index, "length": 96}}


def apart(offset, base="level"):
    return {"element_size": 4, "offset_from": {"selector": base, "offset": offset}}


POINTS = (
    "addresses differ at 2 hits (one row per selector: its first hit): it may point at "
    "another element too; compare offsets with address() (COOKBOOK 4.8)"
)


@pytest.fixture
def store(tmp_path):
    return tmp_path / "evidence.db"


def test_diff_says_which_element_a_pointer_holds_in_each_run(store):
    def run(base, turns, count, left, output):
        rows = []
        for hit, element in enumerate(turns, 1):
            rows += [
                (hit, "turn", "const int *", f"0x{base + 4 * element:016x}", into(element)),
                (hit, "first", "const int *", f"0x{base:016x}", into(0)),
                (hit, "p", "const int *", f"0x{base + 4 * element + 4:016x}"),
                (hit, "level", "int[96]", "[300, 340, 370]", {"element_size": 4}),
                (hit, "count", "int", str(count + hit)),
                (hit, "left", "int", left, {"note": "leftovers"}),
            ]
        return captured(rows, output=output)

    evidence.record(store, run(0x16B42B028, [6, 17], 0, "7", "H 03:00\n"), language="c")
    evidence.record(store, run(0x16F527038, [4, 16], 1, "9", "H 02:00\n"), language="c")
    rows = evidence.run_query(store, "diff")["rows"]
    qualified = (
        "qualified (run a: note, run b: note): not evidence by itself; queries run shows why"
    )
    assert [(r["hit"], r["selector"], r["note"]) for r in rows] == [
        (None, "(output)", "output differs at line 1, column 4"),
        # Another element is a real difference, in hit and capture order.
        (1, "turn", "the address of `level[6]` in run a, of `level[4]` in run b"),
        (1, "count", None),
        (2, "turn", "the address of `level[17]` in run a, of `level[16]` in run b"),
        (2, "count", None),
        # Proven nothing of: it may still point at another element.
        (1, "p", POINTS),
        (1, "left", qualified),
        (2, "left", qualified),
        # The same element, where only the stack moved: addresses only, ranked last.
        (
            1,
            "first",
            "addresses only, at 2 hits (one row per selector: its first hit): the address of "
            "the same element of `level` in both runs",
        ),
    ]
    # The array itself, whose elements agree, is no difference at all.
    assert not any(r["selector"] == "level" for r in rows)
    compared = evidence.run_query(store, "compare")["rows"]
    assert {(r["hit"], r["selector"], r["note"]) for r in compared} >= {
        (1, "turn", "the address of `level[6]` in run a, of `level[4]` in run b"),
        (2, "first", "addresses only: the address of the same element of `level` in both runs"),
    }
    sql = "SELECT run_id, hit, element_size, at_element_of, element_index FROM evidence WHERE "
    found = evidence.query(store, sql + "selector = 'turn' ORDER BY run_id, hit")["rows"]
    assert [(r["element_size"], r["at_element_of"], r["element_index"]) for r in found] == [
        (4, "level", 6),
        (4, "level", 17),
        (4, "level", 4),
        (4, "level", 16),
    ]


def test_another_element_at_the_same_address_is_still_a_difference(store):
    # The array moved and the pointer did not: the same text, another element.
    for index in (6, 4):
        rows = [
            (1, "turn", "const int *", "0x0000000000001018", into(index)),
            (1, "level", "int[96]", "[1, 2]"),
        ]
        evidence.record(store, captured(rows), language="c")
    [found] = evidence.run_query(store, "diff")["rows"]
    assert found["note"] == "the address of `level[6]` in run a, of `level[4]` in run b"
    assert found["differs"] == 1


def test_a_pointer_proven_in_one_run_only_may_point_elsewhere(store):
    for address, extra in (("0x000000016b42b040", into(6)), ("0x000000016f527048", {})):
        rows = [(1, "turn", "const int *", address, extra), (1, "level", "int[96]", "[1, 2]")]
        evidence.record(store, captured(rows), language="c")
    [found] = evidence.run_query(store, "diff")["rows"]
    assert found["note"] == (
        "addresses differ: it may point at another element too; compare offsets with "
        "address() (COOKBOOK 4.8)"
    )


@pytest.mark.parametrize("key", ["at_element_of", "offset_from"])
def test_the_alignment_note_counts_proven_elements_but_not_offsets(store, key):
    # `word` reads "a", "b", "c" in both runs, at other elements' addresses of `text`: the
    # note said it agreed between the runs at each hit, under rows that said another
    # element. An offset by address alone may be layout, so it agrees or not as before.
    def at(index):
        number = "index" if key == "at_element_of" else "offset"
        return {"element_size": 1, key: {"selector": "text", number: index}}

    for base, starts, quiz in ((0x16B42B028, 4, (87, 88, 90)), (0x16F527038, 16, (84, 87, 88))):
        rows = []
        for hit in (1, 2, 3):
            index = starts + 4 * hit
            text = f'0x{base + index:016x} "{"abc"[hit - 1]}"'
            rows += [
                (hit, "word", "const char *", text, at(index)),
                (hit, "quiz", "int", str(quiz[hit - 1])),
            ]
        evidence.record(store, captured(rows, output=f"{base}\n"), language="c")
    found = evidence.run_query(store, "diff")
    said = [r["note"] for r in found["rows"] if r["selector"] == "word"]
    notes = " ".join(found.get("notes") or [])
    if key == "at_element_of":
        assert said == [
            f"the address of `text[{8 + 4 * hit}]` in run a, of `text[{20 + 4 * hit}]` in run b"
            for hit in (0, 1, 2)
        ]
        assert "`quiz`" in notes and "`word` agrees" not in notes, notes
    else:
        assert said == [
            f"addresses differ: by address, `text + {8 + 4 * hit}` in run a and "
            f"`text + {20 + 4 * hit}` in run b; nothing proves they point into one array, so "
            "it may or may not be another element"
            for hit in (0, 1, 2)
        ]
        assert "`word` agrees" in notes, notes


def test_diff_shows_offsets_by_address_but_ranks_them_with_addresses(store):
    # tides' line 40: `level` is a parameter, so no element is proven, but `turn` is 6
    # ints past it in one run and 4 in the other. Review of PR #312: unrelated pointers of
    # one type (`src`, `dst`: two heap blocks within the cap) differ by the allocator's
    # layout, so another offset ranks with the addresses that differ, showing both.
    def run(base, turns, firsts, gap, output):
        rows = []
        for hit, (element, first) in enumerate(zip(turns, firsts), 1):
            rows += [
                (hit, "turn", "const int *", f"0x{base + 4 * element:016x}", apart(element)),
                (hit, "first", "const int *", f"0x{base + 4 * first:016x}", apart(first)),
                (hit, "level", "const int *", f"0x{base:016x}", {"element_size": 4}),
                (hit, "dst", "const int *", f"0x{base + 4 * gap:016x}", apart(gap)),
                (hit, "trend", "int", str(hit)),
            ]
        return captured(rows, output=output)

    evidence.record(store, run(0x16B42B028, [6, 17], [-2, 0], 1026, "H 03:00\n"), language="c")
    evidence.record(store, run(0x16F527038, [4, 16], [-2, 0], 2050, "H 02:00\n"), language="c")
    rows = evidence.run_query(store, "diff")["rows"]
    hedge = "; nothing proves they point into one array, so it may or may not be another element"
    by = "addresses differ: by address, "
    assert [(r["hit"], r["selector"], r["note"]) for r in rows] == [
        (None, "(output)", "output differs at line 1, column 4"),
        # No real difference: each offset ranks with the addresses that differ.
        (1, "turn", f"{by}`level + 6` in run a and `level + 4` in run b{hedge}"),
        (1, "first", f"{by}`level - 2` in both runs{hedge}"),
        (1, "level", POINTS),
        (
            1,
            "dst",
            "addresses differ at 2 hits (one row per selector: its first hit): by address, "
            f"`level + 1026` in run a and `level + 2050` in run b{hedge}",
        ),
        (2, "turn", f"{by}`level + 17` in run a and `level + 16` in run b{hedge}"),
        (2, "first", f"{by}`level + 0` in both runs{hedge}"),
    ]
    found = evidence.query(
        store,
        "SELECT offset_from, element_offset, at_element_of FROM evidence WHERE selector = "
        "'first' AND hit = 1 ORDER BY run_id",
    )["rows"]
    assert found == [{"offset_from": "level", "element_offset": -2, "at_element_of": None}] * 2
    # The same address in both runs is no difference, whatever the offsets.
    evidence.record(store, run(0x16B42B028, [6, 17], [-2, 0], 1026, "x\n"), language="c")
    evidence.record(store, run(0x16B42B028, [6, 17], [-2, 0], 1026, "x\n"), language="c")
    assert evidence.run_query(store, "diff", {"a": 3, "b": 4})["rows"] == []


def test_offsets_from_other_bases_or_one_run_only_are_not_compared(store):
    for address, extra in (
        ("0x000000016b42b040", apart(-2)),
        ("0x000000016f527048", apart(-2, "p")),
    ):
        rows = [(1, "turn", "const int *", address, extra)]
        evidence.record(store, captured(rows), language="c")
    for address, extra in (("0x000000016b42b040", apart(3)), ("0x000000016f527048", {})):
        rows = [(1, "turn", "const int *", address, extra)]
        evidence.record(store, captured(rows), language="c")
    for a, b in ((1, 2), (3, 4)):
        [found] = evidence.run_query(store, "diff", {"a": a, "b": b})["rows"]
        assert found["note"] == (
            "addresses differ: it may point at another element too; compare offsets with "
            "address() (COOKBOOK 4.8)"
        ), found


READS_ALIKE = (
    "what it points at reads the same in both runs, though it may be another element that "
    "reads the same"
)


@pytest.mark.parametrize("language", ["c", "cpp"])
def test_a_reference_whose_text_agrees_ranks_with_addresses_only(store, language):
    # grades: `name` reads "Maya" in both runs. It may still be another element named
    # Maya, but no offset is needed to see that its text agrees.
    def run(base, quiz):
        rows = []
        for hit in (1, 2):
            rows += [
                (hit, "name", "const std::string &", f'0x{base + 24 * hit:016x} "Maya"'),
                (hit, "s", "const Student &", f"0x{base + 64 * hit:016x} {{exam = 79}}"),
                # Another address inside: what that points at is not told.
                (hit, "n", "const Node &", f"0x{base + 8 * hit:016x} {{next = 0x{base:x}}}"),
                (hit, "top", "const int *", f"0x{base + 4 * hit:016x}"),
                (hit, "quiz", "int", str(quiz + hit)),
                (hit, "left", "int", str(quiz), {"note": "leftovers"}),
            ]
        return captured(rows)

    evidence.record(store, run(0x10492D560, 87), language=language)
    evidence.record(store, run(0x102BA5560, 84), language=language)
    rows = evidence.run_query(store, "diff")["rows"]
    alike = f"addresses only, at 2 hits (one row per selector: its first hit): {READS_ALIKE}"
    assert [(r["selector"], r["note"]) for r in rows if r["hit"] == 1] == [
        ("quiz", None),
        ("n", POINTS),
        ("top", POINTS),
        (
            "left",
            "qualified (run a: note, run b: note): not evidence by itself; queries run shows why",
        ),
        ("name", alike),
        ("s", alike),
    ]


def test_a_rust_pointers_text_is_left_as_it_was(store):
    for address in ("0x000000016b42b040", "0x000000016f527048"):
        rows = [(1, "w", "&str *", f'{address} "abc"')]
        evidence.record(store, captured(rows), language="rust")
    [found] = evidence.run_query(store, "diff")["rows"]
    assert found["note"].startswith("addresses differ: it may point at another element too")


COLUMNS = ("element_size", "at_element_of", "element_index", "offset_from", "element_offset")


def test_a_schema_16_store_gains_the_columns_and_keeps_its_runs(store):
    evidence.record(store, captured([(1, "turn", "const int *", "0x1018")]), language="c")
    connection = sqlite3.connect(store)
    connection.executescript(
        "DROP VIEW last_run; DROP VIEW evidence;"
        + "".join(f"ALTER TABLE captures DROP COLUMN {column};" for column in COLUMNS)
        + """
        CREATE VIEW evidence AS SELECT c.run_id, r.started_at, r.label, r.program,
            c.line, r.status, c.hit, c.stop, c.phase, c.function, c.instance, c.selector,
            c.type, c.value, c.num, c.unavailable, c.error, c.view, c.note, c.identity,
            c.scope, r.args, r.sets, r.artifact_manifest_id, r.source_sha256,
            r.edit IS NOT NULL AS candidate, r.language
            FROM captures c JOIN runs r ON r.run_id = c.run_id;
        CREATE VIEW last_run AS SELECT * FROM evidence
            WHERE run_id = (SELECT MAX(run_id) FROM runs);
        PRAGMA user_version = 16;
        """
    )
    connection.close()
    rows = [
        (1, "turn", "const int *", "0x1018", into(6)),
        (1, "p", "const int *", "0x1018", apart(-2)),
    ]
    evidence.record(store, captured(rows), language="c")
    sql = f"SELECT run_id, selector, {', '.join(COLUMNS)} FROM evidence"
    assert evidence.query(store, sql + " ORDER BY run_id, selector DESC")["rows"] == [
        {"run_id": 1, "selector": "turn", **dict.fromkeys(COLUMNS)},
        {
            "run_id": 2,
            "selector": "turn",
            **dict(zip(COLUMNS, (4, "level", 6, None, None))),
        },
        {
            "run_id": 2,
            "selector": "p",
            **dict(zip(COLUMNS, (4, None, None, "level", -2))),
        },
    ]
    connection = sqlite3.connect(store)
    assert connection.execute("PRAGMA user_version").fetchone() == (17,)
    for column in COLUMNS:
        assert column in evidence.TABLES["captures"][1], column


def test_the_diff_needs_no_materialized_cte_and_stays_within_the_column_limit(store):
    # Review of PR #312: `pair AS MATERIALIZED` needs SQLite 3.35, while Python 3.11 may link
    # an older one. Without it, the tiers' GROUP BY read every term behind `note`, and
    # newer SQLite counts aggregate terms against the column limit Peeky sets (100). The
    # tiers now name `kind` where `note` is `kind`: the rows are the same, far within it.
    for name, (sql, _, _, _) in evidence.QUERIES.items():
        assert "MATERIALIZED" not in str(sql).upper(), name
    assert "MATERIALIZED" not in evidence._DIFF_RANKS.upper()

    def run(base, shift, output):
        rows = []
        for hit in (1, 2, 3):
            element = hit + shift
            rows += [
                (hit, "turn", "const int *", f"0x{base + 4 * element:016x}", into(element)),
                (hit, "p", "const int *", f"0x{base + 4 * element:016x}", apart(element)),
                (hit, "q", "const int *", f"0x{base + 8:016x}"),
                (hit, "name", "const std::string &", f'0x{base + 24 * hit:016x} "Maya"'),
                (hit, "left", "int", str(shift), {"note": "leftovers"}),
                (hit, "count", "int", str(hit * (shift + 1))),
            ]
        if shift:
            rows.append((4, "count", "int", "9"))
        recorded = captured(rows, output=output)
        recorded["rows"].append(
            dict(hit=1, phase="after-set", line=40, selector="count", value=str(7 + shift))
        )
        return recorded

    evidence.record(store, run(0x16B42B028, 0, "a\n"), language="c")
    evidence.record(store, run(0x16F527038, 1, "b\n"), language="cpp")
    expected = evidence.run_query(store, "diff", limit=1000)["rows"]
    notes = [str(r["note"]) for r in expected]
    # Every tier is there: the proven element's address, offsets by address, the reference
    # whose text agrees, a qualified value, a missing hit, a pointer proven nothing of.
    for start in (
        "the address of `level[1]` in run a, of `level[2]` in run b",
        "addresses differ: by address, `level + 1` in run a and `level + 2` in run b",
        "addresses only, at 3 hits (one row per selector: its first hit): what it points at",
        "qualified (run a: note, run b: note)",
        "missing in run a",
        "addresses differ at 3 hits (one row per selector: its first hit): it may point",
    ):
        assert any(note.startswith(start) for note in notes), (start, notes)
    for sql in (evidence._DIFF, evidence._DIFF_RANKS, evidence.QUERIES["compare"][1]):
        connection, _ = evidence._reader(store)
        try:
            connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 50)
            shown = connection.execute(sql, {"a": 1, "b": 2}).fetchall()
        finally:
            connection.close()
        assert shown, sql[:60]
    # The same rows as the CTE read once, where this SQLite has MATERIALIZED (3.35 on).
    if sqlite3.sqlite_version_info >= (3, 35):
        connection, _ = evidence._reader(store)
        try:
            connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 1000)
            materialized = evidence._DIFF.replace("pair AS (", "pair AS MATERIALIZED (", 1)
            assert materialized != evidence._DIFF
            plain = connection.execute(evidence._DIFF, {"a": 1, "b": 2}).fetchall()
            assert connection.execute(materialized, {"a": 1, "b": 2}).fetchall() == plain
        finally:
            connection.close()
        assert [r["note"] for r in expected] == [row[-1] for row in plain]
