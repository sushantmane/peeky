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
from peeky.probe import _capture, _pointed, _table, pointed_element, pointed_text
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


def session(offset):
    """A NativeSession whose `_typed` reads ``offset`` in GRAPH, for any value."""
    return SimpleNamespace(_typed=lambda frame, steps: None if offset is None else typed(offset))


def extent(offset, value="", kind="", reference=None):
    variable = {"value": value, "type": kind}
    if reference:
        variable["memoryReference"] = reference
    return NativeSession.value_extent(session(offset), variable, {"id": 1}, ["v"])


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
    assert rows[0]["points_into"] == {"selector": "level", "index": 6, "length": 96}
    assert rows[0]["element_size"] == rows[1]["element_size"] == 4
    assert "points_into" not in rows[1] and "element_size" not in rows[2]


def test_a_pointer_base_a_qualified_value_or_itself_proves_nothing():
    # tides' `find_waters`: `level` is a `const int *` parameter, whose length is unknown.
    rows = [row("turn", "0x0000000000001018", "const int *"), row("level", "0x1000", "const int *")]
    _pointed(rows, {0: INT, 1: dict(INT)})
    assert not any("points_into" in each for each in rows)
    assert [each["element_size"] for each in rows] == [4, 4]
    for qualified in ({"note": "leftovers"}, {"view": "stale"}, {"stale": True}):
        rows = [
            row("p", "0x0000000000001018", "int *", **qualified),
            row("level", "[1]", "int[96]"),
        ]
        _pointed(rows, {0: INT, 1: LEVEL})
        assert "points_into" not in rows[0], qualified
        rows = [
            row("p", "0x0000000000001018", "int *"),
            row("level", "[1]", "int[96]", **qualified),
        ]
        _pointed(rows, {0: INT, 1: LEVEL})
        assert "points_into" not in rows[0], qualified
    unread = [row("p", "0x0000000000001018", "int *"), {"selector": "level", "unavailable": True}]
    _pointed(unread, {0: INT, 1: LEVEL})
    assert "points_into" not in unread[0]


def test_an_element_inside_one_array_comes_before_one_past_another():
    # `a` ends where `b` starts: the pointer is b + 0, and a + 4 (one past a's end).
    a = {"address": 0x1000, "length": 4, "element": 1, "element_size": 4}
    b = {"address": 0x1010, "length": 4, "element": 1, "element_size": 4}
    rows = [
        row("p", "0x0000000000001010", "int *"),
        row("a", "[1]", "int[4]"),
        row("b", "[2]", "int[4]"),
    ]
    _pointed(rows, {0: INT, 1: a, 2: b})
    assert rows[0]["points_into"] == {"selector": "b", "index": 0, "length": 4}
    rows = rows[:2]
    _pointed(rows, {0: INT, 1: a})
    assert rows[0]["points_into"] == {"selector": "a", "index": 4, "length": 4}


def test_tables_and_session_get_show_the_element():
    assert pointed_text({"points_into": {"selector": "level", "index": 6, "length": 96}}) == (
        "points at level[6]"
    )
    assert pointed_text({"points_into": {"selector": "level", "index": 96, "length": 96}}) == (
        "level + 96: one past its end"
    )
    assert pointed_text({"points_into": {"selector": "x", "index": 0}}) == "points at x"
    assert (
        pointed_text({"points_into": {"selector": "x", "index": 1}}) == "&x + 1: one past its end"
    )
    assert pointed_text({}) == ""
    shown = row(
        "turn",
        "0x0000000000001018",
        "const int *",
        element_size=4,
        points_into={"selector": "level", "index": 6, "length": 96},
    )
    table = _table(
        {"rows": [shown], "interventions": [], "status": "completed", "hits": 1, "seconds": 1}
    )
    assert "0x0000000000001018  [points at level[6]]" in table
    assert _get_lines(shown)[0] == "turn: const int * = 0x0000000000001018  [points at level[6]]"


class Frames:
    """A worker whose stop has `p` (a pointer into its caller's array) in frame 0 and
    `level` and `q` in frame 1, each with what its address can tell."""

    def __init__(self):
        self.requests = []

    def dispatch(self, operation, args):
        assert operation == "inspect"
        self.requests.append(args)
        frame = args.get("frame", 0)
        names = {
            0: [{"name": "p", "value": "0x0000000000001018", "type": "int *", "extent": INT}],
            1: [
                {"name": "level", "value": "[1, 2]", "type": "int[96]", "extent": LEVEL},
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
    assert "points_into" not in pointer and "points_into" not in base
    assert pointer["element_size"] == base["element_size"] == 4
    # The roots read say what their addresses can tell, as they are previewed.
    assert worker.requests[0]["extents"] == ["p"]
    rows = _capture(worker, stop, ("q", "level"), 1, "observed", frame=1)
    assert rows[0]["points_into"] == {"selector": "level", "index": 2, "length": 96}


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
    return {"element_size": 4, "points_into": {"selector": "level", "index": index, "length": 96}}


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
        (1, "turn", "another element of `level`: index 6 in run a, 4 in run b"),
        (1, "count", None),
        (2, "turn", "another element of `level`: index 17 in run a, 16 in run b"),
        (2, "count", None),
        # Proven nothing of: it may still point at another element.
        (1, "p", POINTS),
        (1, "left", qualified),
        (2, "left", qualified),
        # The same element, where only the stack moved: addresses only, ranked last.
        (
            1,
            "first",
            "addresses only, at 2 hits (one row per selector: its first hit): the same "
            "element of `level` in both runs",
        ),
    ]
    # The array itself, whose elements agree, is no difference at all.
    assert not any(r["selector"] == "level" for r in rows)
    compared = evidence.run_query(store, "compare")["rows"]
    assert {(r["hit"], r["selector"], r["note"]) for r in compared} >= {
        (1, "turn", "another element of `level`: index 6 in run a, 4 in run b"),
        (2, "first", "addresses only: the same element of `level` in both runs"),
    }
    sql = "SELECT run_id, hit, element_size, points_into, element_index FROM evidence WHERE "
    found = evidence.query(store, sql + "selector = 'turn' ORDER BY run_id, hit")["rows"]
    assert [(r["element_size"], r["points_into"], r["element_index"]) for r in found] == [
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
    assert found["note"] == "another element of `level`: index 6 in run a, 4 in run b"
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


def test_a_pointer_to_another_element_never_agrees_in_the_alignment_note(store):
    # `word` reads "a", "b", "c" in both runs, at other elements of `text`: the note said
    # it agreed between the runs at each hit, under rows that said another element.
    def at(index):
        return {"element_size": 1, "points_into": {"selector": "text", "index": index}}

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
    assert [r["note"] for r in found["rows"] if r["selector"] == "word"] == [
        f"another element of `text`: index {8 + 4 * hit} in run a, {20 + 4 * hit} in run b"
        for hit in (0, 1, 2)
    ]
    notes = " ".join(found.get("notes") or [])
    assert "`quiz`" in notes and "`word` agrees" not in notes, notes


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


def test_a_schema_16_store_gains_the_columns_and_keeps_its_runs(store):
    evidence.record(store, captured([(1, "turn", "const int *", "0x1018")]), language="c")
    connection = sqlite3.connect(store)
    connection.executescript(
        "DROP VIEW last_run; DROP VIEW evidence;"
        + "".join(
            f"ALTER TABLE captures DROP COLUMN {column};"
            for column in ("element_size", "points_into", "element_index")
        )
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
    rows = [(1, "turn", "const int *", "0x1018", into(6))]
    evidence.record(store, captured(rows), language="c")
    sql = "SELECT run_id, selector, element_size, points_into, element_index FROM evidence"
    assert evidence.query(store, sql + " ORDER BY run_id")["rows"] == [
        {
            "run_id": 1,
            "selector": "turn",
            "element_size": None,
            "points_into": None,
            "element_index": None,
        },
        {
            "run_id": 2,
            "selector": "turn",
            "element_size": 4,
            "points_into": "level",
            "element_index": 6,
        },
    ]
    connection = sqlite3.connect(store)
    assert connection.execute("PRAGMA user_version").fetchone() == (17,)
    for column in ("element_size", "points_into", "element_index"):
        assert column in evidence.TABLES["captures"][1], column
