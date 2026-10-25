"""What is read through a C or C++ pointer is marked `[unproven]` where Peeky can't show the
pointer points at a live object (#339).

Peeky read any non-NULL pointee and showed it as a plain value: a pointer into a call that
had returned, into a freed heap block, or two past an array's end showed leftover bytes as
the object. It still reads them, since an agent may need them, but marks them with why: the
pointer is proven to point at a live object only inside an array or object a listed frame
holds (of its type), in the program's static storage, or in `main`'s arguments.
"""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from peeky import evidence
from peeky.cli import _get_lines, _returned_text, _variables_text
from peeky.native import NativeSession, image_ranges
from peeky.native_previews import Typed
from peeky.probe import (
    BELOW_STACK,
    RED_ZONE,
    _capture,
    _returned_row,
    _table,
    pointee_past,
    pointee_place,
    pointee_proven,
    unproven_text,
)
from peeky.worker import DebugWorker, _selector_path

LONG, CELL, CHAR = (
    "base long DW_ATE_signed 8",
    "struct cell 16 abc",
    "base char DW_ATE_signed_char 1",
)
POINTS_LONG = {"pointee": LONG, "element_size": 8, "count": 1}
ARR = ("arr", {"address": 0x1000, "length": 4, "element": LONG, "element_size": 8}, {})
CELLS = ("cells", {"address": 0x2000, "length": 3, "element": CELL, "element_size": 16}, {})
X = ("x", {"address": 0x3000, "element": LONG, "element_size": 8}, {})
CALLER = {"frame": 1, "function": "main"}


def at(address):
    return f"0x{address:016x}"


@pytest.mark.parametrize(
    "address, pointer, proven",
    [
        # Each element of the array, and a single object: proven.
        (0x1000, POINTS_LONG, True),
        (0x1018, POINTS_LONG, True),
        (0x3000, POINTS_LONG, True),
        # One past the end (#336's), two past, before, between elements: not.
        (0x1020, POINTS_LONG, False),
        (0x1030, POINTS_LONG, False),
        (0xFF8, POINTS_LONG, False),
        (0x1004, POINTS_LONG, False),
        # Another type, or another size, at an element's address: not.
        (0x1008, {**POINTS_LONG, "pointee": "base double DW_ATE_float 8"}, False),
        (0x1008, {**POINTS_LONG, "element_size": 4}, False),
        (0x3000, {"pointee": CELL, "element_size": 16, "count": 1}, False),
        # A pointer to `long[2]` at element 2 of a `long[4]` fits; at element 3 it doesn't.
        (0x1010, {**POINTS_LONG, "count": 2}, True),
        (0x1018, {**POINTS_LONG, "count": 2}, False),
        # A character type reads any byte of an object Peeky placed (C lets it alias).
        (0x1003, {"pointee": CHAR, "element_size": 1, "count": 1, "character": True}, True),
        (0x1020, {"pointee": CHAR, "element_size": 1, "count": 1, "character": True}, False),
        # The program's static storage, and `main`'s arguments.
        (0x8008, POINTS_LONG, True),
        (0x9FF0, POINTS_LONG, True),
        (0xA000, POINTS_LONG, False),
    ],
)
def test_a_pointee_is_proven_live_only_where_peeky_places_it(address, pointer, proven):
    found = pointee_proven(
        address, pointer, [ARR, X], statics=[(0x8000, 0x9000)], regions=[(0x9F00, 0xA000)]
    )
    assert found is proven


def test_outside_an_array_says_how_far():
    assert pointee_past(0x1030, POINTS_LONG, [ARR]) == {
        "kind": "past",
        "selector": "arr",
        "index": 6,
        "length": 4,
    }
    assert pointee_past(0xFF0, POINTS_LONG, [(*ARR[:2], CALLER)]) == {
        "kind": "before",
        "selector": "arr",
        "elements": 2,
        **CALLER,
    }
    # One past the end is #336's (withheld); another type, a misaligned address, a single
    # object, or one too far: none.
    assert pointee_past(0x1020, POINTS_LONG, [ARR]) is None
    assert pointee_past(0x1030, {**POINTS_LONG, "pointee": CHAR}, [ARR]) is None
    assert pointee_past(0x1034, POINTS_LONG, [ARR]) is None
    assert pointee_past(0x3010, POINTS_LONG, [X]) is None
    assert pointee_past(0x1000 + 8 * (4 + (1 << 20) + 1), POINTS_LONG, [ARR]) is None
    # The nearest array by elements.
    near = ("near", {**ARR[1], "address": 0x1040}, {})
    assert pointee_past(0x1068, POINTS_LONG, [ARR, near])["selector"] == "near"


@pytest.mark.parametrize(
    "address, sp, settled, kind",
    [
        # Below the stack pointer: where no live call keeps its locals.
        (0x7F0 - RED_ZONE, 0x800, True, "below"),
        (0x800 - BELOW_STACK, 0x800 + 0, True, "below"),
        # Far below it is other memory (the heap, on Linux).
        (0x800 - BELOW_STACK - 8, 0x800, True, "unplaced"),
        # Within the red zone, the innermost call may keep locals Peeky didn't place.
        (0x7F8, 0x800, False, "unplaced"),
        (0x7F8, 0x800, True, "below"),
        (0x700, 0x800, False, "below"),
        # Between the stack pointer and the last local Peeky placed: the listed frames.
        (0x1028, 0x800, True, "stack"),
        (0x1028, None, True, "stack"),
        # Past every local Peeky placed: the heap, or a frame it doesn't list.
        (0x3008, 0x800, True, "unplaced"),
        (0x900, None, True, "unplaced"),
    ],
)
def test_where_an_unproven_pointer_points(address, sp, settled, kind):
    assert pointee_place(address, sp, [ARR, X], settled) == kind


def test_the_note_says_the_rule_why_and_what_to_do():
    heap = unproven_text("p", {"kind": "unplaced", "statics": True})
    assert heap == (
        "read through `p`: Peeky can't show that `p` points at a live object, so this value may "
        "be left over: its address is in no local, member or element that Peeky could place in "
        "the frames it lists, nor in the program's static memory; if it is a heap block, Peeky "
        "can't tell whether that block is still allocated, so check where the program frees it"
    )
    # Where Peeky didn't know where the program's static memory is, it doesn't say.
    assert "static" not in unproven_text("p", {"kind": "unplaced", "statics": False})
    assert unproven_text("s.next", {"kind": "below"}).endswith(
        "it points below the stack pointer, where no live call keeps its locals, as a pointer "
        "into a call that has returned does; read the value before that call returns"
    )
    assert unproven_text("q", {"kind": "stack"}).endswith(
        "it points into the stack of the frames Peeky lists, but at no local, member or "
        "element of its type that Peeky could place there; read the local it should point at "
        "by name"
    )
    past = {"kind": "past", "selector": "cells", "index": 4, "length": 3}
    assert unproven_text("two", past).endswith(
        "it holds the address of `cells[4]`, past the end of `cells`, which has 3 elements; "
        "read `cells` by name"
    )
    before = {"kind": "before", "selector": "arr", "elements": 1, **CALLER}
    assert unproven_text("p", before).endswith(
        "it holds an address 1 element before the start of `arr` in main (frame 1); read "
        "`arr` in main (`session get --frame 1`)"
    )
    # A pointer's own text (a `char *`'s), and a returned one's.
    assert unproven_text("dup", {"kind": "unplaced"}, text=True).startswith(
        "Peeky can't show that `dup` points at a live object, so its text may be left over: "
    )
    assert unproven_text(None, {"kind": "below"}, text=True).startswith(
        "Peeky can't show that the pointer it returned points at a live object, so its text "
    )


def test_a_note_names_the_pointer_by_its_selector():
    assert _selector_path(["p"]) == "p"
    assert _selector_path(["s", "next", "next"]) == "s.next.next"
    assert _selector_path(["nodes", "[2]", "next"]) == "nodes[2].next"
    assert _selector_path(["pp", "*pp"]) == "pp[0]"
    assert _selector_path(["this", "head"]) == "this.head"


GRAPH = {
    1: ("base", "long", "DW_ATE_signed", 8),
    2: ("pointer", "", 1, None),  # long *
    3: ("array", "", 1, (2,), False),  # long[2]
    4: ("pointer", "", 3, None),  # long (*)[2]
    5: ("pointer", "", None, None),  # void *
    6: ("base", "char", "DW_ATE_signed_char", 1),
    7: ("pointer", "", 6, None),  # char *
    8: ("pointer", "", 7, None),  # char **
    9: ("typedef", "LongPtr", 2),
    10: (
        "record",
        "text",
        "struct",
        32,
        None,
        (("hider", 11, 0, False, None, None), ("length", 1, 8, False, None, None)),
        (11, 1),
        False,
        False,
        "std",
        (),
        "std::text",
        (),
    ),
    11: ("record", "hider", "struct", 8, None, (), (), False, False, None, (), "hider", ()),
}


def session(offset, storages=("parameter",)):
    native = NativeSession.__new__(NativeSession)
    native.target = SimpleNamespace(language="c")
    native._typed = lambda frame, steps, walked=False: Typed(GRAPH, offset)
    native._storages = lambda frame, root: set(storages)
    return native


def test_what_a_pointer_points_at_comes_from_the_debug_information():
    assert session(2).pointee_of({}, ["p"]) == {"pointee": LONG, "element_size": 8, "count": 1}
    assert session(9).pointee_of({}, ["p"])["pointee"] == LONG
    # A pointer to an array points at all of it.
    assert session(4).pointee_of({}, ["pa"]) == {"pointee": LONG, "element_size": 8, "count": 2}
    # `void *`, or what is no pointer: nothing to judge.
    assert session(5).pointee_of({}, ["v"]) is None
    assert session(1).pointee_of({}, ["n"]) is None


def test_a_struct_with_a_summary_is_where_its_members_are():
    # lldb-dap 18 shows `&s = "text"` for a `std::string`, with no address: its members
    # have theirs (`T @ 0x…`), less their offsets.
    hider = {"name": "hider", "type": "hider", "value": "hider @ 0x1200"}
    length = {"name": "length", "type": "long", "value": "5"}
    assert session(10).address_from_members({}, "s", [hider, length]) == 0x1200
    # A member elsewhere than its offset says, or none placed: no address.
    moved = {"name": "length", "type": "long", "value": "long @ 0x1300"}
    assert session(10).address_from_members({}, "s", [hider, moved]) is None
    assert session(10).address_from_members({}, "s", [length]) is None
    assert session(1).address_from_members({}, "n", [hider]) is None


def test_a_2d_arrays_rows_and_their_members_are_placed():
    graph = {
        1: ("base", "char", "DW_ATE_signed_char", 1),
        2: ("array", "", 1, (4, 16), False),  # char[4][16]
        3: record("pt", [("x", 4, 0)], 8),
        4: ("base", "long", "DW_ATE_signed", 8),
        5: ("array", "", 3, (2, 3), False),  # pt[2][3]
    }
    native = NativeSession.__new__(NativeSession)
    native._typed = lambda frame, steps, walked=False: (
        (Typed(graph, 2 if steps == ["names"] else 5), False) if walked else None
    )
    (names,) = native.held_extents(
        {"value": "char[4][16] @ 0x1000", "type": "char[4][16]"}, {}, "names"
    )
    assert names == (
        "names",
        {"address": 0x1000, "length": 64, "element": CHAR, "element_size": 1, "dims": [4, 16]},
    )
    # `char *row = names[1]` is in it; past its end is #336's; no index names an element.
    pointer = {"pointee": CHAR, "element_size": 1, "count": 1}
    assert pointee_proven(0x1010, pointer, [(*names, {})]) is True
    assert pointee_past(0x1042, pointer, [(*names, {})]) is None
    found = dict(native.held_extents({"value": "pt[2][3] @ 0x2000", "type": "pt[2][3]"}, {}, "pts"))
    assert found["pts[1][2].x"]["address"] == 0x2000 + 5 * 8


def test_a_pointer_is_told_alike_in_every_source_file():
    # `int **pp` in one file at `int *q` in another; a struct holding a callback, and a
    # C++ class's vtable pointer (a pointer to a function), likewise.
    graph = {
        1: ("base", "int", "DW_ATE_signed", 4),
        2: ("pointer", "", 1, 8),
        3: ("base", "int", "DW_ATE_signed", 4),
        4: ("qualifier", "const", 3),
        5: ("pointer", "", 4, 8),
        6: ("other", "DW_TAG_subroutine_type"),
        7: ("pointer", "", 6, 8),
        8: record("handler", [("on", 7, 0)], 8),
        9: ("other", "DW_TAG_subroutine_type"),
        10: ("pointer", "", 9, 8),
        11: record("handler", [("on", 10, 0)], 8),
        12: ("enum", "color", 4),
    }
    assert Typed(graph, 2).identity() == Typed(graph, 5).identity()
    assert str(Typed(graph, 2).identity()).startswith("pointer ")
    assert Typed(graph, 8).identity() == Typed(graph, 11).identity()
    # An enum is still told only within its file: the walk reads no enumerators.
    assert Typed(graph, 12).identity() == 12


def test_a_base_class_is_an_object_where_the_build_places_it():
    # `struct Circle : Shape` with a vtable first: its `Shape` is 8 bytes in. A `Shape *` at
    # a `Circle` local points at a live `Shape`; a virtual base (no offset) is never placed.
    graph = {
        1: ("base", "long", "DW_ATE_signed", 8),
        2: record("Shape", [("id", 1, 0)], 8),
        3: (*record("Circle", [("r", 1, 16)], 24)[:10], (2, 4), "Circle", (), (8, None)),
        4: record("Far", [("f", 1, 0)], 8),
    }
    native = NativeSession.__new__(NativeSession)
    native._typed = lambda frame, steps, walked=False: (
        (Typed(graph, 3), False) if walked else Typed(graph, 3)
    )
    found = dict(native.held_extents({"value": "Circle @ 0x1000", "type": "Circle"}, {}, "c"))
    shape = Typed(graph, 2).identity()
    assert found["c.Shape"] == {"address": 0x1008, "element": shape, "element_size": 8}
    assert found["c.Shape.id"]["address"] == 0x1008 and "c.Far" not in found
    pointer = {"pointee": shape, "element_size": 8, "count": 1}
    bases = [(name, extent, {}) for name, extent in found.items()]
    assert pointee_proven(0x1008, pointer, bases) is True
    assert pointee_proven(0x1000, pointer, bases) is False


def record(name, members, size):
    placed = tuple((m, t, at, False, None, None) for m, t, at in members)
    held = tuple(t for _, t, _ in members)
    return ("record", name, "struct", size, None, placed, held, False, False, None, (), name, ())


def test_mains_argument_vector_is_where_its_strings_are():
    argv = {"name": "argv", "value": at(0x7FF0)}
    assert session(8).argument_vector(argv, {}, "argv") == 0x7FF0
    # Not a `char **` parameter: none.
    assert session(8, ("automatic",)).argument_vector(argv, {}, "argv") is None
    assert session(7).argument_vector(argv, {}, "argv") is None
    assert session(8).argument_vector({**argv, "value": at(0)}, {}, "argv") is None


def test_the_programs_image_is_its_loaded_segments(tmp_path):
    import struct

    # An ELF64 with two loadable segments and a note.
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HH", header, 54, 56, 3)
    table = b"".join(
        struct.pack("<IIQQQQQQ", kind, 5, 0, address, address, size, size, 4096)
        for kind, address, size in ((1, 0x0, 0x1000), (1, 0x2000, 0x800), (4, 0x300, 0x20))
    )
    (tmp_path / "elf").write_bytes(bytes(header) + table)
    assert image_ranges(tmp_path / "elf") == ((0x0, 0x1000), (0x2000, 0x2800))
    # A Mach-O's segments, but `__PAGEZERO` and `__LINKEDIT`.
    commands = b""
    for name, address, size in (
        (b"__PAGEZERO", 0, 0x100000000),
        (b"__TEXT", 0x100000000, 0x4000),
        (b"__DATA", 0x100004000, 0x4000),
        (b"__LINKEDIT", 0x100008000, 0x4000),
    ):
        commands += struct.pack("<II16sQQQQIIII", 0x19, 72, name, address, size, 0, 0, 7, 7, 0, 0)
    macho = struct.pack("<IIIIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 4, len(commands), 0, 0)
    (tmp_path / "macho").write_bytes(macho + commands)
    assert image_ranges(tmp_path / "macho") == (
        (0x100000000, 0x100004000),
        (0x100004000, 0x100008000),
    )
    native = NativeSession.__new__(NativeSession)
    native.binary, native.load_bias = tmp_path / "macho", 0x10000
    assert native.static_ranges()[0] == (0x100010000, 0x100014000)
    native = NativeSession.__new__(NativeSession)
    native.binary, native.load_bias = tmp_path / "macho", None
    assert native.static_ranges() is None


SP = 0x800


class Backend:
    """lldb-dap stopped in `main` (frame id 1), whose stack pointer is ``SP``. `arr` (long[4])
    is at 0x1000, `n` (long, which lldb-dap 18 gives no address) at 0x1100, `text` (a
    `std::string`, which it places only by its members) at 0x1200; `heap` and
    `dup` point at the heap, `lit` at the program's image (0x2000), `dang` below the stack
    pointer, `two` two past `arr`'s end, `gap` (an `int *`) between locals, `pn` at `n`."""

    def __init__(self, source):
        self.calls = []
        self.frames = [
            {"id": 1, "name": "main", "line": 30, "source": {"path": source}},
            {"id": 2, "name": "start", "line": 0, "source": {"path": "/usr/lib/dyld"}},
        ]

        def pointer(name, kind, value, reference):
            return {"name": name, "type": kind, "value": value, "variablesReference": reference}

        self.variables = {
            1: [
                pointer("arr", "long[4]", "long[4] @ 0x1000", 4),
                pointer("pa", "long *", at(0x1008), 5),
                pointer("heap", "cell *", at(0x9000), 6),
                pointer("dang", "long *", at(SP - 16), 7),
                pointer("lit", "const char *", f'{at(0x2000)} "literal"', 9),
                pointer("dup", "char *", f'{at(0x9200)} "heap"', 10),
                pointer("ps", "std::string *", f"{at(0x9300)} <no text: lldb-dap 18>", 14),
                pointer("text", "std::string", '"local"', 15),
                pointer("pt", "std::string *", f'{at(0x1200)} "local"', 16),
                pointer("pn", "long *", at(0x1100), 11),
                pointer("two", "long *", at(0x1030), 12),
                pointer("gap", "int *", at(0x1040), 13),
                pointer("nil", "long *", at(0), 0),
                {"name": "n", "type": "long", "value": "7", "variablesReference": 0},
            ],
            5: [pointer("*pa", "long", "2", 0)],
            6: [pointer("a", "long", "31", 0), pointer("next", "cell *", at(0x9100), 8)],
            7: [pointer("*dang", "long", "1", 0)],
            8: [pointer("a", "long", "41", 0), pointer("next", "cell *", at(0), 0)],
            9: [pointer("*lit", "const char", "'l'", 0)],
            10: [pointer("*dup", "char", "'h'", 0)],
            11: [pointer("*pn", "long", "7", 0)],
            12: [pointer("*two", "long", "99", 0)],
            13: [pointer("*gap", "int", "98", 0)],
            16: [pointer("hider", "hider", "hider @ 0x1200", 17)],
            20: [pointer("hider", "hider", "hider @ 0x1200", 21)],
        }

    def request(self, operation, args, timeout=None):
        self.calls.append((operation, deepcopy(args)))
        if operation == "stackTrace":
            return {"stackFrames": deepcopy(self.frames)}
        if operation == "scopes":
            return {
                "scopes": [
                    {"name": "Locals", "presentationHint": "locals", "variablesReference": 1}
                ]
            }
        if operation == "variables":
            return {"variables": deepcopy(self.variables[args["variablesReference"]])}
        if operation == "evaluate" and args["expression"] == "$sp":
            return {"result": at(SP)}
        if operation == "evaluate" and args["expression"] == "&n":
            assert args["context"] == "hover", "a hover reads a variable, never runs code"
            return {"result": f"(long *) &n = {at(0x1100)}"}
        if operation == "evaluate" and args["expression"] == "&text":
            # lldb-dap 18: a type with a summary shows it in place of the address.
            return {"result": '(std::string *) &text = "local"', "variablesReference": 20}
        raise AssertionError((operation, args))


class Runtime:
    checks_past_ends = True
    pointees = {
        ("pa",): POINTS_LONG,
        ("heap",): {"pointee": CELL, "element_size": 16, "count": 1},
        ("heap", "next"): {"pointee": CELL, "element_size": 16, "count": 1},
        ("dang",): POINTS_LONG,
        ("lit",): {"pointee": CHAR, "element_size": 1, "count": 1},
        ("dup",): {"pointee": CHAR, "element_size": 1, "count": 1},
        ("ps",): {"pointee": "struct string 32 def", "element_size": 32, "count": 1},
        ("pt",): {"pointee": "struct string 32 def", "element_size": 32, "count": 1},
        ("pn",): POINTS_LONG,
        ("two",): POINTS_LONG,
        ("gap",): {"pointee": "base int DW_ATE_signed 4", "element_size": 4, "count": 1},
        ("nil",): POINTS_LONG,
    }

    def __init__(self, root):
        self.prepared = SimpleNamespace(workspace=root, source_root=root)
        self.lines = {"main.c": {}}

    def assert_current(self):
        pass

    def validate_stop(self, frame):
        pass

    def local_scopes(self, scopes):
        return [scope for scope in scopes if scope.get("presentationHint") == "locals"]

    def value_extent(self, variable, frame, steps):
        return None

    def pointee_of(self, frame, steps):
        return dict(self.pointees[tuple(steps)]) if tuple(steps) in self.pointees else None

    def static_ranges(self):
        return [(0x2000, 0x3000)]

    def stack_pointer(self, dap, frame_id):
        from peeky.native import _stack_pointer

        return _stack_pointer(dap, frame_id)

    def held_extents(self, variable, frame, name, address=None):
        if name == "arr":
            return [ARR[:2]]
        if name == "n" and address is not None:
            return [("n", {"address": address, "element": LONG, "element_size": 8})]
        if name == "text" and address is not None:
            string = {"address": address, "element": "struct string 32 def", "element_size": 32}
            return [("text", string)]
        return []

    def address_from_members(self, frame, name, children):
        assert name == "text" and children[0]["value"] == "hider @ 0x1200"
        return 0x1200

    def unplaced_local(self, variable, frame, name):
        return name in ("n", "text")

    def argument_vector(self, variable, frame, name):
        return None

    def settable(self, variable, frame):
        return False


@pytest.fixture
def stopped(tmp_path):
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n")
    instance = DebugWorker(tmp_path)
    instance.dap = Backend(str(tmp_path / "main.c"))
    instance.runtime = Runtime(tmp_path)
    instance.session, instance.stop_id, instance.state = "session", "stop", "stopped"
    instance.thread_id = 1
    instance.capabilities = {"supports_set_variable": True}
    return instance


def call(worker, operation, **kwargs):
    stop = {"session_id": "session", "stop_id": worker.stop_id}
    return worker.dispatch(operation, {**stop, **kwargs})


def asked(worker, expression):
    return sum(
        1
        for operation, args in worker.dap.calls
        if operation == "evaluate" and args["expression"] == expression
    )


def test_the_worker_marks_what_it_reads_through_an_unproven_pointer(stopped):
    shown = {v["name"]: v for v in call(stopped, "inspect", frame_id=1)["scopes"][0]["variables"]}
    # A pointer's own value is its address: no mark, but its text is read through it.
    assert "view" not in shown["heap"] and "view" not in shown["pa"]
    assert shown["dup"]["view"] == "unproven" and shown["dup"]["unproven_read"] is True
    assert shown["dup"]["value"] == f'{at(0x9200)} "heap"'
    assert shown["dup"]["note"].startswith(
        "Peeky can't show that `dup` points at a live object, so its text may be left over: "
        "its address is in no local"
    )
    # A string literal is in the program's image, which lasts the whole run.
    assert "view" not in shown["lit"]
    # Where no text was read through it, only why there is none: nothing to mark.
    assert "view" not in shown["ps"]
    # At `text`, which lldb-dap 18 places only by its members: no mark.
    assert "view" not in shown["pt"] and asked(stopped, "&text") == 1
    # After those reads, the scopes are bound for changes afresh, as after a caller's.
    calls = [operation for operation, _ in stopped.dap.calls]
    assert calls[-2:] == ["stackTrace", "scopes"] and "evaluate" in calls[:-2]

    def expand(name):
        return call(stopped, "expand", handle=shown[name]["variablesReference"], start=0)

    page = expand("heap")
    heap = "read through `heap`: Peeky can't show that `heap` points at a live object"
    assert all(v["view"] == "unproven" and v["unproven_read"] for v in page["variables"])
    assert all(v["note"].startswith(heap) for v in page["variables"])
    assert page["unproven_read"].startswith(heap)
    assert [v["value"] for v in page["variables"]] == ["31", at(0x9100)]
    # What is read through it further down says so too, through the first such pointer.
    deeper = call(stopped, "expand", handle=page["variables"][1]["variablesReference"], start=0)
    assert deeper["variables"][0]["value"] == "41"
    assert deeper["variables"][0]["note"].startswith(heap)

    # Inside a listed array, and at a local the debugger placed only when asked: no mark.
    assert "view" not in expand("pa")["variables"][0]
    assert "unproven_read" not in expand("pa")
    assert "view" not in expand("pn")["variables"][0]
    assert asked(stopped, "&n") == 1
    # Below the stack pointer; two past `arr`'s end; between locals, at none of its type.
    assert "below the stack pointer" in expand("dang")["variables"][0]["note"]
    assert "the address of `arr[6]`, past the end of `arr`" in expand("two")["unproven_read"]
    assert "into the stack of the frames Peeky lists" in expand("gap")["unproven_read"]
    # The stack pointer is asked once per stop, a local's address once.
    assert asked(stopped, "$sp") == 1 and asked(stopped, "&n") == 1
    stopped.stop_id = "next"
    shown = {v["name"]: v for v in call(stopped, "inspect", frame_id=1)["scopes"][0]["variables"]}
    expand("pn")
    assert asked(stopped, "$sp") == 2 and asked(stopped, "&n") == 2


def test_a_read_that_did_not_come_is_asked_again(stopped):
    # The stack pointer's reply doesn't come in time: nothing is kept of that judgement, and
    # the next inspection of the stop asks again (review: a timeout left `dang` unplaced).
    request, slow = stopped.dap.request, [1]

    def answer(operation, args, timeout=None):
        if operation == "evaluate" and args["expression"] == "$sp" and slow:
            slow.pop()
            raise TimeoutError("no answer in time")
        return request(operation, args, timeout)

    stopped.dap.request = answer
    for _ in range(2):
        shown = {
            v["name"]: v for v in call(stopped, "inspect", frame_id=1)["scopes"][0]["variables"]
        }
        page = call(stopped, "expand", handle=shown["dang"]["variablesReference"], start=0)
    assert asked(stopped, "$sp") == 1  # the first one timed out, and was not recorded
    assert "below the stack pointer" in page["unproven_read"]


def test_a_heap_pointers_pointee_asks_nothing_of_locals_it_cant_be(stopped):
    # No `char *`: nothing else asks for `n` first.
    stopped.dap.variables[1] = [v for v in stopped.dap.variables[1] if v["name"] != "dup"]
    shown = {v["name"]: v for v in call(stopped, "inspect", frame_id=1)["scopes"][0]["variables"]}
    call(stopped, "expand", handle=shown["heap"]["variablesReference"], start=0)
    # `n` is a `long`: no `cell` is in it, so it is never placed for `heap`.
    assert asked(stopped, "&n") == 0 and asked(stopped, "$sp") == 1
    # A `long *` may point at it: it is placed then.
    call(stopped, "expand", handle=shown["pn"]["variablesReference"], start=0)
    assert asked(stopped, "&n") == 1


def test_nothing_is_judged_where_the_runtime_does_not_check(stopped):
    stopped.runtime.checks_past_ends = False
    shown = {v["name"]: v for v in call(stopped, "inspect", frame_id=1)["scopes"][0]["variables"]}
    page = call(stopped, "expand", handle=shown["heap"]["variablesReference"], start=0)
    assert "view" not in shown["dup"] and "view" not in page["variables"][0]
    assert not any(operation == "evaluate" for operation, _ in stopped.dap.calls)


def test_the_locals_peeky_places_by_asking_are_bounded(stopped):
    from peeky.worker import PLACED_HOVERS

    stopped.dap.variables[1] += [
        {"name": f"m{i}", "type": "long", "value": "0", "variablesReference": 0}
        for i in range(PLACED_HOVERS + 4)
    ]
    stopped.runtime.unplaced_local = lambda variable, frame, name: name == "n" or name[0] == "m"
    hovered = []
    request = stopped.dap.request

    def answer(operation, args, timeout=None):
        if operation == "evaluate" and args["expression"].startswith("&m"):
            hovered.append(args["expression"])
            return {"result": at(0x5000 + 8 * len(hovered))}
        return request(operation, args, timeout)

    stopped.dap.request = answer
    shown = {v["name"]: v for v in call(stopped, "inspect", frame_id=1)["scopes"][0]["variables"]}
    # `dup`'s text: a `char *` may point into any local, so each is asked for, up to the
    # bound; nothing more at this stop.
    assert len(hovered) + asked(stopped, "&n") == PLACED_HOVERS
    call(stopped, "expand", handle=shown["pn"]["variablesReference"], start=0)
    assert len(hovered) + asked(stopped, "&n") == PLACED_HOVERS


class Stop:
    """A worker whose stop has `heap` (a `cell *`) Peeky can't show points at a live
    object, `dup` (a `char *`) whose text is read through such a pointer, and `pa` inside
    an array."""

    note = unproven_text("heap", {"kind": "unplaced", "statics": True})

    def dispatch(self, operation, args):
        if operation == "expand":
            if args["handle"] == 6:
                child = {"name": "a", "type": "long", "value": "31", "variablesReference": 0}
                child.update(view="unproven", unproven_read=True, note=self.note)
                return {"variables": [child], "truncated": False, "unproven_read": self.note}
            assert args["handle"] == 5
            child = {"name": "*pa", "type": "long", "value": "2", "variablesReference": 0}
            return {"variables": [child], "truncated": False}
        if operation == "preview":
            return {"value": "{a = 31}"}
        assert operation == "inspect"
        text = unproven_text("dup", {"kind": "unplaced", "statics": True}, text=True)
        variables = [
            {"name": "heap", "type": "cell *", "value": at(0x9000), "variablesReference": 6},
            {"name": "pa", "type": "long *", "value": at(0x1008), "variablesReference": 5},
            {
                "name": "dup",
                "type": "char *",
                "value": f'{at(0x9200)} "heap"',
                "variablesReference": 0,
                "view": "unproven",
                "unproven_read": True,
                "note": text,
            },
        ]
        return {
            "frame": 0,
            "frames": [{"line": 23, "name": "main"}],
            "scopes": [{"name": "Locals", "variables": variables}],
        }


def test_rows_read_through_it_are_marked_with_why():
    stop = {"session_id": "s", "stop_id": "t"}
    selectors = ("heap", "heap.a", "heap[0]", "dup", "pa[0]")
    rows = {
        row["selector"]: row
        for row in _capture(Stop(), stop, selectors, 1, "observed", pointers=True)
    }
    assert "view" not in rows["heap"] and "note" not in rows["heap"]
    for selector in ("heap.a", "heap[0]"):
        assert rows[selector]["view"] == "unproven", rows[selector]
        assert rows[selector]["note"] == Stop.note
    # `heap[0]` is the struct it points at, previewed, and marked as its members are.
    assert rows["heap[0]"]["value"] == "{a = 31}"
    assert rows["dup"]["view"] == "unproven" and rows["dup"]["note"].startswith("Peeky can't")
    assert "view" not in rows["pa[0]"] and "note" not in rows["pa[0]"]
    # Rows carry no new key: the mark is the view, why is the note.
    assert "unproven_read" not in rows["heap.a"]
    table = _table(
        {
            "rows": list(rows.values()),
            "interventions": [],
            "status": "completed",
            "hits": 1,
            "seconds": 1,
        }
    )
    line = next(line for line in table.splitlines() if " heap.a " in line)
    assert line.endswith("31  [unproven]"), line


def test_session_get_marks_each_child_and_says_why_once():
    stop = {"session_id": "s", "stop_id": "t"}
    (row,) = _capture(Stop(), stop, ("heap",), 1, "observed", children=True, pointers=True)
    assert row["note"] == Stop.note
    assert _get_lines(row) == [
        f"heap: cell * = {at(0x9000)}",
        "  heap.a: long = 31  [unproven]",
        f"  note: {Stop.note}",
    ]
    (row,) = _capture(Stop(), stop, ("dup",), 1, "observed", children=True, pointers=True)
    assert _get_lines(row)[0] == f'dup: char * = {at(0x9200)} "heap"  [unproven]'


def test_session_get_says_why_a_members_text_is_marked():
    # A struct whose `char *` member's text is read from a heap block: the member is marked,
    # and the row says why (review: it said Rust's note).
    text = unproven_text("r.name", {"kind": "unplaced", "statics": True}, text=True)
    name = {"name": "name", "type": "char *", "value": f'{at(0x9200)} "heap"'}
    name.update(variablesReference=0, view="unproven", unproven_read=True, note=text)
    count = {"name": "count", "type": "int", "value": "2", "variablesReference": 0}

    class Holder:
        def dispatch(self, operation, args):
            if operation == "expand":
                return {"variables": [name, count], "truncated": False}
            if operation == "preview":
                return {}
            variable = {"name": "r", "type": "rec", "value": "{…}", "variablesReference": 3}
            return {
                "frame": 0,
                "frames": [{"line": 9, "name": "main"}],
                "scopes": [{"name": "Locals", "variables": [variable]}],
            }

    stop = {"session_id": "s", "stop_id": "t"}
    (row,) = _capture(Holder(), stop, ("r",), 1, "observed", children=True, pointers=True)
    assert row["note"] == text
    assert _get_lines(row)[1:] == [
        f'  r.name: char * = {at(0x9200)} "heap"  [unproven]',
        "  r.count: int = 2",
        f"  note: {text}",
    ]


def test_session_inspect_marks_a_pointers_text():
    snapshot = Stop().dispatch("inspect", {})
    lines = _variables_text({**snapshot, "frames": [{"name": "main", "line": 23}]}).splitlines()
    at_dup = next(i for i, line in enumerate(lines) if line.startswith("  dup:"))
    assert lines[at_dup].endswith('"heap"  [unproven]')
    assert lines[at_dup + 1].startswith("    note: Peeky can't show that `dup` points at a live")


def test_a_returned_c_string_peeky_cant_place_is_marked():
    calls = []

    def judge(address, pointer, frame):
        calls.append((address, pointer))
        return {"kind": "below"}

    worker = SimpleNamespace(
        runtime=SimpleNamespace(checks_past_ends=True, pointee_of=lambda frame, steps: None),
        judge_pointee=judge,
    )
    note = DebugWorker.returned_unproven(worker, "char *", f'{at(0x7F0)} "stale"', {"id": 1})
    assert note.startswith("Peeky can't show that the pointer it returned points at a live")
    assert calls == [(0x7F0, {"pointee": None, "element_size": 1, "count": 1, "character": True})]
    # No text read, or only why there is none; not a byte-sized character pointer: nothing
    # to mark.
    assert DebugWorker.returned_unproven(worker, "char *", at(0x7F0), {"id": 1}) is None
    assert DebugWorker.returned_unproven(worker, "char *", f"{at(0x7F0)} <no text>", {}) is None
    assert DebugWorker.returned_unproven(worker, "long *", f'{at(0x7F0)} "x"', {"id": 1}) is None
    returned = {
        "function": "word",
        "type": "char *",
        "value": f'{at(0x7F0)} "stale"',
        "view": "unproven",
        "unproven_read": True,
        "note": note,
    }
    snapshot = {"returned": returned, "frames": [{"line": 16}], "location": "w.c:16"}
    row = _returned_row({"hit": 1, "phase": "returned", "selector": "(return)"}, snapshot)
    assert row["view"] == "unproven" and row["note"] == note
    assert _returned_text(returned).endswith('"stale"  [unproven]')


def _result(rows):
    return {
        "status": "completed",
        "hits": max(row[0] for row in rows),
        "probe": {
            "program": "list.c",
            "line": 20,
            "capture": list(dict.fromkeys(row[1] for row in rows)),
        },
        "output": "",
        "rows": [
            dict(hit=hit, phase="observed", line=20, selector=s, type="long", value=v)
            | ({"view": "unproven", "note": unproven_text("p", {"kind": "below"})} if mark else {})
            for hit, s, v, mark in rows
        ],
    }


def test_diff_ranks_an_unproven_difference_after_the_real_ones(tmp_path):
    store = tmp_path / "evidence.db"
    for total, left in (("10", "1"), ("12", "2")):
        rows = [(1, "p[0]", left, True), (1, "total", "3", False), (2, "total", total, False)]
        evidence.record(store, _result(rows), language="c")
    rows = evidence.run_query(store, "diff")["rows"]
    # The real difference first, though at a later hit; the leftover bytes after it,
    # qualified: not evidence by itself.
    assert [(r["hit"], r["selector"], r["note"]) for r in rows] == [
        (2, "total", None),
        (
            1,
            "p[0]",
            "qualified (run a: unproven, run b: unproven): not evidence by itself; queries "
            "run shows why",
        ),
    ]
    # `queries run` gives why, and the store keeps the note.
    caveats = {r["selector"]: r["caveat"] for r in evidence.run_query(store, "hits")["rows"]}
    assert caveats == {"p[0]": "unproven", "total": None}
    run = evidence.run_query(store, "run")["rows"]
    noted = [json.dumps(row) for row in run if "below the stack pointer" in json.dumps(row)]
    assert noted
