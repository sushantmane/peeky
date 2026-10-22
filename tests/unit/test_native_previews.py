"""C and C++ previews (#136): written from the debugger's children, bounded, and never
guessing (unions, pointers, values with no value, standard-library types)."""

from types import SimpleNamespace

import pytest

from peeky.adapters import ViewBudgetExhausted
from peeky.cxx_strings import shorten
from peeky.native import STACK_READ
from peeky.native_previews import ITEMS, LENGTH, Typed, bitfield_text, preview


def node(name, value, kind="int", reference=0):
    return {"name": name, "value": value, "type": kind, "variablesReference": reference}


class Debugger:
    """`variables` replies by reference, with the requests made."""

    def __init__(self, children, budget=None):
        self.children, self.requests, self.budget = children, [], budget

    def __call__(self, reference, count):
        if self.budget is not None and len(self.requests) >= self.budget:
            raise ViewBudgetExhausted("Preview budget exhausted")
        self.requests.append((reference, count))
        return self.children[reference][:count]


def text(child):
    return None if child["value"] == "<no text>" else child["value"]


def record(name, members, kind="struct", bases=(), declaration=False, library=None):
    """A fields.type_graph record: members as (name, type offset[, bit size])."""
    rows = tuple((m[0], m[1], 0, len(m) > 2, None, m[2] if len(m) > 2 else None) for m in members)
    return ("record", name, kind, 8, None, rows, (), False, declaration, library, tuple(bases))


GRAPH = {
    1: ("base", "int", "DW_ATE_signed", 4),
    2: ("base", "char", "DW_ATE_signed_char", 1),
    3: ("base", "signed char", "DW_ATE_signed_char", 1),
    4: ("base", "unsigned char", "DW_ATE_unsigned_char", 1),
    5: ("base", "wchar_t", "DW_ATE_signed", 4),
    10: record("Pt", [("x", 1), ("y", 1)]),
    11: ("pointer", "", 10),
    12: ("reference", "", 10),
    13: record("Num", [("i", 1), ("f", 1)], kind="union"),
    14: ("array", "", 10, (1000,), False),
    15: ("array", "", 1, (2, 3), False),
    16: ("array", "", 1, None, True),
    17: ("typedef", "Row", 18),
    18: ("array", "", 1, (3,), False),
    19: ("array", "", 2, (8,), False),
    20: ("array", "", 5, (4,), False),
    21: record("string", [], library="std"),
    22: record("opaque", [], declaration=True),
    23: ("pointer", "", 11),
    24: record(None, [("u", 1), ("v", 1)]),
    25: record(None, [("i", 1)], kind="union"),
    26: record("Base", [("b", 1)]),
    27: record("Derived", [("d", 1)], bases=(26,)),
    28: record("Bits", [("sc", 3, 3), ("uc", 4, 3), ("pc", 2, 4), ("x", 1)]),
    30: record(
        "S",
        [
            ("a", 10),
            ("num", 13),
            (None, 25),
            (None, 24),
            ("next", 11),
            ("label", 11),
            ("gone", 1),
            ("s", 21),
            ("name", 19),
            ("w", 20),
            ("vla", 16),
        ],
    ),
    31: record("O", [("s", 30)]),
    # Review 2: wide character bitfields; a member named as a base class.
    32: ("base", "char16_t", "DW_ATE_UTF", 2),
    33: ("typedef", "wchar_t", 1),
    34: record("EB", [("w", 32, 9), ("wc", 33, 9), ("i8", 3, 3)]),
    35: record("B2", [("A2", 1)], bases=(36,)),
    36: record("A2", [("v", 1)]),
}


def typed(offset):
    return Typed(GRAPH, offset)


PT = [node("x", "1"), node("y", "2")]


def test_a_struct_and_its_nested_structs():
    fetch = Debugger(
        {1: [node("s", "S @ 0x10", "S", 2)], 2: [node("a", "Pt @ 0x10", "Pt", 3)], 3: PT}
    )
    # Deeper than two levels: elided.
    assert preview(node("o", "O @ 0x10", "O", 1), fetch, typed(31), text) == "{s = {a = {…}}}"
    assert fetch.requests == [(1, 64), (2, 64)]
    fetch = Debugger({1: PT})
    assert preview(node("p", "Pt @ 0x10", "Pt", 1), fetch, typed(10), text) == "{x = 1, y = 2}"


def test_arrays_take_their_length_from_the_debug_information():
    elements = [node(f"[{i}]", "Pt @ 0x10", "Pt", 100 + i) for i in range(1000)]
    fetch = Debugger({1: elements, **{100 + i: PT for i in range(8)}})
    # The type's name (`Hold<int[1]>[5]`, or `int[]`) never gives the length.
    shown = preview(
        node("a", "Weird<int[1]>[1000] @ 0x10", "Weird<int[1]>[1000]", 1), fetch, typed(14), text
    )
    assert shown.startswith("[{x = 1, y = 2}, {x = 1, y = 2},") and shown.endswith(", …]"), shown
    assert fetch.requests[0] == (1, ITEMS)
    rows = [node("[0]", "int[3] @ 0x10", "int[3]", 2), node("[1]", "int[3] @ 0x1c", "int[3]", 3)]
    grid = Debugger(
        {
            1: rows,
            2: [node(f"[{i}]", str(i)) for i in range(3)],
            3: [node(f"[{i}]", str(i + 3)) for i in range(3)],
        }
    )
    assert (
        preview(node("g", "int[2][3] @ 0x10", "int[2][3]", 1), grid, typed(15), text)
        == "[[0, 1, 2], [3, 4, 5]]"
    )
    # A typedef'd array is an array (`Row`).
    row = Debugger({1: [node(f"[{i}]", str(i)) for i in range(3)]})
    assert preview(node("r", "Row @ 0x10", "Row", 1), row, typed(17), text) == "[0, 1, 2]"


def test_nothing_is_guessed():
    members = [
        node("a", "Pt @ 0x10", "Pt", 2),
        node("num", "Num @ 0x10", "Num", 3),
        node("", "S::(anonymous union) @ 0x14", "S::(anonymous union)", 4),
        node("<null>", "S::(anonymous struct) @ 0x18", "S::(anonymous struct)", 5),
        node("", "0"),  # an unnamed bitfield LLDB adds
        node("next", "0x1000", "Pt *", 6),
        node("label", '0x2000 "edge"', "Pt *", 7),
        node("gone", "<variable not available>"),
    ]
    fetch = Debugger({1: members, 2: PT, 5: [node("u", "5"), node("v", "6")]})
    shown = preview(node("s", "S @ 0x10", "S", 1), fetch, typed(30), text)
    assert shown == (
        "{a = {x = 1, y = 2}, num = <union>, <union>, {u = 5, v = 6}, next = 0x1000, "
        "label = 0x2000, gone = <no value>}"
    )
    # Unions, pointers, VLAs and incomplete types are not previewed.
    assert preview(node("u", "Num @ 0x10", "Num", 1), fetch, typed(13), text) is None
    assert preview(node("p", "0x1000", "Pt *", 1), fetch, typed(11), text) is None
    assert preview(node("v", "int[] @ 0x10", "int[]", 1), fetch, typed(16), text) is None
    assert preview(node("o", "opaque @ 0x10", "opaque", 1), fetch, typed(22), text) is None
    # Children the type does not have: not this type's value.
    other = Debugger({1: [node("z", "1")]})
    assert preview(node("p", "Pt @ 0x10", "Pt", 1), other, typed(10), text) is None
    nothing = Debugger({1: []})
    assert preview(node("p", "Pt @ 0x10", "Pt", 1), nothing, typed(10), text) is None
    # Elements other than the type holds.
    fewer = Debugger({1: [node("[0]", "1")]})
    assert preview(node("r", "Row @ 0x10", "Row", 1), fewer, typed(17), text) is None


def test_members_of_their_own_kind():
    members = [
        node("s", '"hi"', "std::string"),
        node("name", '"abc"', "char[8]", 8),
        node("w", 'L"a"', "wchar_t[4]", 9),
        node("vla", "int[] @ 0x10", "int[]", 10),
    ]
    wide = [
        node("[0]", "97 L'a'"),
        node("[1]", "0 L'\\0'"),
        node("[2]", "98 L'b'"),
        node("[3]", "0 L'\\0'"),
    ]
    fetch = Debugger({1: members, 9: wide})
    shown = preview(node("s", "S @ 0x10", "S", 1), fetch, typed(30), text)
    # A wide array element by element: LLDB's text ends at its first NUL.
    assert shown == "{s = \"hi\", name = \"abc\", w = [L'a', L'\\0', L'b', L'\\0'], vla = […]}", (
        shown
    )
    # At the root too, whatever its text.
    assert (
        preview(node("w", 'L"a"', "wchar_t[4]", 9), fetch, typed(20), text)
        == "[L'a', L'\\0', L'b', L'\\0']"
    )
    # A char array is LLDB's literal.
    assert preview(node("n", '"abc"', "char[8]", 8), fetch, typed(19), text) is None
    # A base class, by its class's name.
    derived = Debugger(
        {1: [node("Base", "Base @ 0x10", "Base", 2), node("d", "2")], 2: [node("b", "1")]}
    )
    assert (
        preview(node("x", "Derived @ 0x10", "Derived", 1), derived, typed(27), text)
        == "{Base = {b = 1}, d = 2}"
    )


@pytest.mark.parametrize(
    "value, offset, bits, shown",
    [
        ("'\\x05'", 3, 3, "-3"),
        ("'\\x06'", 4, 3, "6"),
        ("'\\x0e'", 2, 4, "-2"),
        ("'\\x03'", 3, 3, "3"),
        ("'a'", 4, 8, "97"),
        ("'\\0'", 3, 3, "0"),
        ("'\\t'", 4, 4, "9"),
        ("'\\177'", 3, 7, "-1"),
        ("odd", 3, 3, "<bitfield>"),
        # Not a bitfield, or not char-typed: LLDB's text stays.
        ("'\\x05'", 3, None, None),
        ("5", 1, 3, None),
    ],
)
def test_a_char_bitfield_shows_the_number_its_bits_hold(value, offset, bits, shown):
    assert bitfield_text(value, Typed(GRAPH, offset, bits=bits)) == shown


def test_a_reference_shows_its_address_then_what_it_refers_to():
    fetch = Debugger({1: PT})
    assert preview(node("r", "0x0000000000001000", "Pt &", 1), fetch, typed(12), text) == (
        "0x0000000000001000 {x = 1, y = 2}"
    )


def test_a_selectors_steps_follow_the_type():
    assert typed(30).child("a").kind() == "record"
    assert typed(30).child("vla").count() is None
    assert typed(30).child("nope") is None and typed(30).child("") is None
    assert (
        typed(27).child("Base").kind() == "record"
        and typed(27).child("ns::Base").kind() == "record"
    )
    assert typed(15).child("[1]").count() == 3 and typed(15).child("[2]") is None
    assert typed(15).child("[1]").child("[2]").scalar() == ("int", "DW_ATE_signed", 4)
    # Through a pointer, as the debugger lists what it points at; `*pp` is a pointer.
    assert typed(11).child("x").scalar()[0] == "int"
    assert typed(23).child("*pp").kind() == "pointer"
    assert typed(28).child("sc").bits == 3


def test_a_preview_is_bounded():
    many = [node(f"m{i}", "1") for i in range(12)]
    wide = (
        "record",
        "W",
        "struct",
        8,
        None,
        tuple((f"m{i}", 1, 0, False, None, None) for i in range(12)),
        (),
        False,
        False,
        None,
        (),
    )
    graph = {**GRAPH, 40: wide}
    assert preview(node("w", "W @ 0x10", "W", 1), Debugger({1: many}), Typed(graph, 40), text) == (
        "{" + ", ".join(f"m{i} = 1" for i in range(ITEMS)) + ", …}"
    )
    long = [node(f"m{i}", "x" * 39) for i in range(8)]
    shown = preview(node("w", "W @ 0x10", "W", 1), Debugger({1: long}), Typed(graph, 40), text)
    assert len(shown) <= LENGTH and shown.endswith(", …}"), shown


def test_a_spent_budget_leaves_the_debuggers_text():
    fetch = Debugger({1: PT}, budget=0)
    assert preview(node("p", "Pt @ 0x10", "Pt", 1), fetch, typed(10), text) is None


def test_values_with_their_own_text_are_left_as_they_are():
    fetch = Debugger({1: PT})
    for value in ("size=2", "pair", "<error: variable not available>"):
        assert preview(node("v", value, "Pt", 1), fetch, typed(10), text) is None
    assert preview(node("v", "Pt @ 0x10", "Pt", 0), fetch, typed(10), text) is None
    assert preview(node("v", "Pt @ 0x10", "Pt", 1), fetch, None, text) is None
    assert fetch.requests == []


@pytest.mark.parametrize(
    "text, room, shown",
    [
        ('"abcdefghijklmnopqrstuvwxyz"', 12, '"abcdefg"...'),
        # Peeky's own spelling: `\000` and `\377` are one character each, never split.
        ('"n\\00064abcdefghij"', 12, '"n\\00064"...'),
        ('"\\377\\377\\377\\377\\377\\377"', 12, '"\\377"...'),
        # A note is kept whole.
        ('"aaaaaaaaaaaaaaaaaaaa" (shown up to a NUL at byte 20 of 22)', 30, None),
        ("SOME_LONG_ENUMERATOR_NAME", 10, "SOME_LONG…"),
        ("short", 10, "short"),
    ],
)
def test_a_members_text_is_cut_between_characters(text, room, shown):
    result = shorten(text, room)
    if shown is None:
        assert result.endswith('"... (shown up to a NUL at byte 20 of 22)'), result
    else:
        assert result == shown


def session(language="cpp", optimization="O0", fields=None):
    from peeky.native import NativeSession

    native = NativeSession.__new__(NativeSession)
    native.target = SimpleNamespace(language=language, optimization=optimization)
    native.fields = GRAPH if fields is None else fields
    native.declarations = [
        {
            "types": {
                "p": {(10, "automatic", None)},
                "pp": {(11, "automatic", None)},
                "ppp": {(23, "automatic", None)},
                "b": {(28, "automatic", None)},
                "w": {(20, "automatic", None)},
            },
            "referred": frozenset(),
        }
    ]
    native._function_at = lambda frame: native.declarations[0]
    native.typedefs = {}
    return native


def test_only_unoptimized_c_and_cpp_builds_read_by_their_debug_information_get_previews():
    variable = node("p", "Pt @ 0x10", "Pt", 1)
    fetch = Debugger({1: PT})
    frame = {"id": 1}
    assert (
        session().preview_value(variable, fetch, None, frame=frame, steps=["p"]) == "{x = 1, y = 2}"
    )
    assert (
        session("c").preview_value(variable, fetch, None, frame=frame, steps=["p"])
        == "{x = 1, y = 2}"
    )
    assert session("rust").preview_value(variable, fetch, None, frame=frame, steps=["p"]) is None
    # An optimized build's struct may hold values not stored yet; only a wide character
    # array, whose text LLDB ends at a NUL, is shown element by element there.
    optimized = session(optimization="O2")
    assert optimized.preview_value(variable, fetch, None, frame=frame, steps=["p"]) is None
    codes = (("97", "a"), ("0", "\\0"), ("98", "b"), ("0", "\\0"))
    wide = Debugger({1: [node(f"[{i}]", f"{c} L'{t}'") for i, (c, t) in enumerate(codes)]})
    array = node("w", 'L"a"', "wchar_t[4]", 1)
    shown = optimized.preview_value(array, wide, None, frame=frame, steps=["w"])
    assert shown == "[L'a', L'\\0', L'b', L'\\0']", shown
    # No debug information read (a program using setjmp), or no frame: none.
    native = session()
    native.fields = None
    assert native.preview_value(variable, fetch, None, frame=frame, steps=["p"]) is None
    assert session().preview_value(variable, fetch, None) is None
    assert (
        session().preview_value(
            node("q", "Pt @ 0x10", "Pt", 1), fetch, None, frame=frame, steps=["q"]
        )
        is None
    )


def test_what_a_pointer_points_at_when_a_selector_asks():
    fetch = Debugger({1: PT})
    frame = {"id": 1}
    pointer = node("pp", "0x0000000000001000", "Pt *", 1)
    assert (
        session().preview_value(pointer, fetch, None, pointee=True, frame=frame, steps=["pp"])
        == "{x = 1, y = 2}"
    )
    # A pointer to a pointer points at a pointer: never a struct's preview.
    twice = node("ppp", "0x0000000000002000", "Pt **", 1)
    assert (
        session().preview_value(twice, fetch, None, pointee=True, frame=frame, steps=["ppp"])
        is None
    )
    null = node("pp", "0x0000000000000000", "Pt *", 1)
    assert (
        session().preview_value(null, fetch, None, pointee=True, frame=frame, steps=["pp"]) is None
    )


def test_a_char_bitfield_member_row_shows_its_number():
    frame = {"id": 1}
    native = session()
    shown = native.member_value(node("sc", "'\\x05'", "signed char"), frame, ["b", "sc"])
    assert shown == {"value": "-3"}
    assert native.member_value(node("x", "9"), frame, ["b", "x"]) is None
    assert session("rust").member_value(node("sc", "'\\x05'"), frame, ["b", "sc"]) is None
    # A wide character bitfield, which LLDB reads at other bits: no value, and why.
    native.declarations[0]["types"]["e"] = {(34, "automatic", None)}
    for name in ("w", "wc"):
        shown = native.member_value(node(name, "u'\u030d'"), frame, ["e", name])
        assert shown["unavailable"] and "wide character" in shown["reason"], shown


def test_wide_character_bitfields_and_clashing_names_are_not_previewed():
    # Review 2 of #136: `char16_t w : 9` read `u'̍'` for u'a'; in C `wchar_t` is a typedef
    # of `int`, so its name tells it. A number LLDB gives a char bitfield is masked too.
    assert Typed(GRAPH, 32, bits=9).wide_character() and Typed(GRAPH, 33, bits=9).wide_character()
    assert not Typed(GRAPH, 1, bits=9).wide_character()
    assert bitfield_text("u'\u030d'", Typed(GRAPH, 32, bits=9)) == "<bitfield>"
    assert bitfield_text("5", Typed(GRAPH, 3, bits=3)) == "-3"
    assert bitfield_text("-3", Typed(GRAPH, 3, bits=3)) == "-3"
    members = [node("w", "u'\u030d'"), node("wc", "L'b'"), node("i8", "'\\xfd'")]
    fetch = Debugger({1: members})
    assert preview(node("e", "EB @ 0x10", "EB", 1), fetch, typed(34), text) == (
        "{w = <bitfield>, wc = <bitfield>, i8 = -3}"
    )
    # `struct B2 : A2 { int A2; }`: lldb-dap names the base and the member alike.
    assert typed(35).child("A2") is None
    clash = Debugger(
        {1: [node("A2", "A2 @ 0x10", "A2", 2), node("A2", "60")], 2: [node("v", "50")]}
    )
    assert preview(node("b", "B2 @ 0x10", "B2", 1), clash, typed(35), text) is None
    # A struct where the type says a number: not this member.
    odd = Debugger({1: [node("x", "Q @ 0x10", "Q", 2), node("y", "2")]})
    assert preview(node("p", "Pt @ 0x10", "Pt", 1), odd, typed(10), text) is None


def test_a_selected_field_is_previewed_alone_or_left_as_read():
    # A selector walks to `bins[3]` without previewing what it walks through; the value
    # it reaches is previewed by itself. A worker that predates previews keeps the text.
    from peeky.errors import PeekyError
    from peeky.probe import _previewed

    class Worker:
        def __init__(self, reply):
            self.reply, self.calls = reply, []

        def dispatch(self, operation, args):
            self.calls.append((operation, args))
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

    stop = {"session_id": "s", "stop_id": "t"}
    found = node("[3]", "bin @ 0x10", "bin", 5)
    worker = Worker({"value": "{lo = 6, hi = 7, count = 0}"})
    assert _previewed(worker, stop, found)["value"] == "{lo = 6, hi = 7, count = 0}"
    assert worker.calls == [("preview", {**stop, "handle": 5})]
    # What a pointer points at, as `p[0]` asked for it (a value `_dereferenced` made).
    worker = Worker({"value": "{x = 1}"})
    assert _previewed(worker, stop, {**found, "name": "*p", "dereferenced": True})["value"] == (
        "{x = 1}"
    )
    assert worker.calls[0][1]["pointee"] is True
    # The debugger's own `*pp` child (a pointer, of a pointer to a pointer) is no pointee.
    worker = Worker({})
    _previewed(worker, stop, {**found, "name": "*pp"})
    assert "pointee" not in worker.calls[0][1]
    for reply in ({}, ValueError("Unknown operation"), ValueError("Preview budget exhausted")):
        assert _previewed(Worker(reply), stop, found) == found
    error = PeekyError("http://w", 400, {}, {"code": "INVALID_REQUEST", "message": "Unknown"})
    assert _previewed(Worker(error), stop, found) == found
    # Nothing is asked for a value with no value, or no children.
    worker = Worker({"value": "x"})
    assert _previewed(worker, stop, {**found, "unavailable": True}) == {
        **found,
        "unavailable": True,
    }
    assert _previewed(worker, stop, node("x", "1")) == node("x", "1") and worker.calls == []


def container(name, *parameters, members=()):
    """A standard container's fields.type_graph record: its template arguments as
    ("type", offset) or ("value", constant)."""
    rows = tuple((m[0], m[1], 0, False, None, None) for m in members)
    shown = tuple(
        ("type", f"_T{i}", p[1], None) if p[0] == "type" else ("value", f"_N{i}", None, p[1])
        for i, p in enumerate(parameters)
    )
    return ("record", name, "class", 24, None, rows, (), False, False, "std", (), name, shown)


CONTAINERS = {
    **GRAPH,
    50: container("std::__1::vector<int, std::__1::allocator<int> >", ("type", 1), ("type", 99)),
    51: container("std::vector<Pt, std::allocator<Pt> >", ("type", 10)),
    52: container("std::map<int, Pt>", ("type", 1), ("type", 10)),
    53: container("std::__1::set<int>", ("type", 1)),
    54: container(
        "std::__1::array<int, 3UL>", ("type", 1), ("value", 3), members=[("__elems_", 18)]
    ),
    # libc++'s `std::array<int, 0>` holds bytes, not ints; a `_Size` the array does not have.
    55: container(
        "std::__1::array<int, 0UL>", ("type", 1), ("value", 0), members=[("__elems_", 19)]
    ),
    56: container("std::array<int, 4UL>", ("type", 1), ("value", 4), members=[("_M_elems", 18)]),
    57: ("reference", "", 50),
    58: ("typedef", "Row", 50),
    59: record("Bag", [("v", 50), ("n", 1)]),
    60: container("std::multimap<int, int>", ("type", 1), ("type", 1)),
    61: container("std::vector<std::vector<int> >", ("type", 50)),
    62: ("pointer", "", 50),
    63: ("reference", "", 51),
    64: ("pointer", "", 51),
}


def holding(offset):
    return Typed(CONTAINERS, offset)


def elements(*values, at=100):
    return [node(f"[{i}]", value, "int", 0) for i, value in enumerate(values)]


def test_a_vector_shows_its_first_elements_as_its_element_type_says():
    # #181: `have` showed `size=3`, and only a session read `have[2] = ""`.
    fetch = Debugger({1: elements("1", "2", "3")})
    shown = preview(node("v", "size=3", "std::vector<int>", 1), fetch, holding(50), text)
    assert shown == "size=3 [1, 2, 3]" and fetch.requests == [(1, 3)]
    # By a typedef's name too; empty; and past ITEMS elements, `…`.
    assert preview(node("r", "size=0", "Row", 1), Debugger({1: []}), holding(58), text) == (
        "size=0 []"
    )
    many = Debugger({1: elements(*[str(i) for i in range(20)])})
    shown = preview(node("v", "size=20", "std::vector<int>", 1), many, holding(50), text)
    assert shown == "size=20 [" + ", ".join(str(i) for i in range(ITEMS)) + ", …]"
    assert many.requests == [(1, ITEMS)]
    structs = Debugger({1: [node("[0]", "Pt @ 0x10", "Pt", 2)], 2: PT})
    shown = preview(node("v", "size=1", "std::vector<Pt>", 1), structs, holding(51), text)
    assert shown == "size=1 [{x = 1, y = 2}]"
    nested = Debugger({1: [node("[0]", "size=2", "std::vector<int>", 2)], 2: elements("7", "8")})
    shown = preview(node("vv", "size=1", "", 1), nested, holding(61), text)
    assert shown == "size=1 [size=2 [7, 8]]"


def test_a_container_the_debugger_lists_otherwise_keeps_its_summary():
    # Fewer elements than its size says, the class's own members, or no `size=N`.
    short = Debugger({1: elements("1", "2")})
    assert preview(node("v", "size=3", "", 1), short, holding(50), text) is None
    members = Debugger({1: [node("__begin_", "0x10", "int *", 0)]})
    assert preview(node("v", "size=1", "", 1), members, holding(50), text) is None
    for value in ("size=1 (capacity 4)", "std::vector<int> @ 0x10", "size=-1"):
        assert preview(node("v", value, "", 1), Debugger({1: []}), holding(50), text) is None
    # A struct where the type says a number: not this container's elements.
    wrong = Debugger({1: [node("[0]", "Pt @ 0x10", "Pt", 2)]})
    assert preview(node("v", "size=1", "", 1), wrong, holding(50), text) is None
    # A multimap is not previewed: no key reads one entry of it.
    entries = Debugger({1: [node("[0]", "pair @ 0x10", "", 2)], 2: [node("first", "1")]})
    assert preview(node("m", "size=1", "", 1), entries, holding(60), text) is None


def test_maps_show_keys_and_values_and_sets_their_elements():
    fetch = Debugger(
        {
            1: [node("[0]", "pair @ 0x10", "", 2), node("[1]", "pair @ 0x20", "", 3)],
            2: [node("first", "1"), node("second", "Pt @ 0x14", "Pt", 4)],
            3: [node("first", "5"), node("second", "Pt @ 0x24", "Pt", 4)],
            4: PT,
        }
    )
    shown = preview(node("m", "size=2", "std::map<int, Pt>", 1), fetch, holding(52), text)
    assert shown == "size=2 {1: {x = 1, y = 2}, 5: {x = 1, y = 2}}"
    odd = Debugger({1: [node("[0]", "pair @ 0x10", "", 2)], 2: [node("__cc_", "", "", 3)]})
    assert preview(node("m", "size=1", "", 1), odd, holding(52), text) is None
    fetch = Debugger({1: elements("1", "3")})
    assert preview(node("s", "size=2", "", 1), fetch, holding(53), text) == "size=2 {1, 3}"


def test_a_std_array_is_read_by_its_c_array_where_the_debugger_lists_that():
    fetch = Debugger({1: [node("__elems_", "int[3] @ 0x10", "int[3]", 2)], 2: elements("4", "5")})
    fetch.children[2].append(node("[2]", "6"))
    shown = preview(node("a", "std::array<int, 3> @ 0x10", "", 1), fetch, holding(54), text)
    assert shown == "size=3 [4, 5, 6]"
    for offset in (55, 56):
        assert holding(offset).array_member() is None
        assert preview(node("a", "x @ 0x10", "", 1), fetch, holding(offset), text) is None


def test_a_containers_children_are_typed_as_its_arguments_say():
    assert holding(51).child("[7]").kind() == "record"
    entry = holding(52).child("[0]")
    assert entry.kind() == "entry" and entry.child("first").scalar()[0] == "int"
    assert entry.child("second").kind() == "record" and entry.child("third") is None
    assert holding(54).child("__elems_").count() == 3 and holding(54).child("_M_elems") is None
    assert holding(60).child("[0]") is None and holding(50).child("__begin_") is None
    assert holding(62).child("[1]").scalar()[0] == "int"


def test_inside_a_struct_and_through_a_reference():
    fetch = Debugger(
        {1: [node("v", "size=2", "std::vector<int>", 2), node("n", "3")], 2: elements("1", "2")}
    )
    bag = node("b", "Bag @ 0x10", "Bag", 1)
    assert preview(bag, fetch, holding(59), text) == "{v = size=2 [1, 2], n = 3}"
    # Where a library call may be changing it, the summary alone.
    assert preview(bag, fetch, holding(59), text, containers=False) == "{v = size=2, n = 3}"
    root = node("v", "size=2", "std::vector<int>", 2)
    assert preview(root, fetch, holding(50), text, containers=False) is None
    reference = node("r", "0x0000000000001000 size=2", "std::vector<int> &", 2)
    assert preview(reference, fetch, holding(57), text) == "0x0000000000001000 size=2 [1, 2]"


def stack(*names):
    return [{"id": i + 1, "name": name} for i, name in enumerate(names)]


def one():
    return 1


@pytest.mark.parametrize(
    "name, library",
    [
        ("main", False),
        ("Noisy::~Noisy()", False),
        ("void std::_Destroy<Noisy>(Noisy*)", True),
        ("std::__1::vector<Noisy, std::__1::allocator<Noisy>>::clear[abi:ne180100]()", True),
        ("libstdc++.so.6`std::_Rb_tree_insert_and_rebalance(bool, ...)", True),
        ("__gnu_cxx::new_allocator<int>::deallocate(int*, unsigned long)", True),
        ("sum(std::vector<int> const&)", False),
        ("void apply<std::vector<int>>(std::vector<int>&)", False),
        ("", True),
    ],
)
def test_a_standard_library_frame_is_told_by_its_name(name, library):
    from peeky.native import _library_frame

    assert _library_frame({"name": name}) is library


def test_a_container_is_read_only_where_no_library_call_may_be_changing_it():
    native = session()
    frames = stack("Noisy::~Noisy()", "void std::_Destroy<Noisy>(Noisy*)", "main", "start")
    caller, inner = frames[2], frames[0]
    # Below a library call (`clear()` destroying elements): not in the caller's frame.
    assert native.containers_settled(caller, False, frames, threads=one) is False
    # The destructor's own values are its own; through a pointer they may be the vector.
    assert native.containers_settled(inner, False, frames, threads=one) is True
    assert native.containers_settled(inner, True, frames, threads=one) is False
    plain = stack("count(Owner const&)", "main", "start")
    assert native.containers_settled(plain[0], True, plain, threads=one) is True
    # No frames, or more than can be read: not read.
    assert native.containers_settled(inner, False, None, threads=one) is False
    full = stack(*["f"] * 10)
    # More frames than are read: not read (#187: up to STACK_READ, not 64).
    many = stack(*["f"] * STACK_READ)
    asked = []
    assert (
        native.containers_settled(full[0], True, full, lambda n: asked.append(n) or many, one)
        is False
    )
    assert asked == [STACK_READ]
    assert native.containers_settled(full[0], True, full, lambda n: stack(*["f"] * 64), one) is True
    deep = stack(*["f"] * 11, "std::thread::_State_impl<…>::_M_run()")
    assert native.containers_settled(full[0], True, full, lambda n: deep, one) is False
    # Another thread may be inside a `push_back` on a container this one shares.
    assert native.containers_settled(plain[0], False, plain, threads=lambda: 2) is False
    assert native.containers_settled(plain[0], False, plain, threads=lambda: None) is False
    assert native.containers_settled(plain[0], False, plain) is False


def test_an_element_of_a_container_a_library_call_may_be_changing_is_not_previewed():
    native = session(fields=CONTAINERS)
    native.declarations[0]["types"]["vp"] = {(51, "automatic", None)}
    native.declarations[0]["types"]["v"] = {(50, "automatic", None)}
    fetch = Debugger({1: PT, 2: elements("1")})
    element = node("[0]", "Pt @ 0x10", "Pt", 1)
    frames = stack("Noisy::~Noisy()", "void std::_Destroy<Noisy>(Noisy*)", "main")
    shown = native.preview_value(element, fetch, None, frame=frames[2], steps=["vp", "[0]"])
    assert shown is None
    shown = native.preview_value(
        element, fetch, None, frame=frames[2], steps=["vp", "[0]"], stack=frames, threads=one
    )
    assert shown is None
    # Through a reference or pointer to the vector, whose elements it lists as its own.
    # (Review of #181's own: its elements were not taken for a container's.)
    native.declarations[0]["types"]["rv"] = {(63, "automatic", None)}
    native.declarations[0]["types"]["pv"] = {(64, "automatic", None)}
    below = stack("main", "void std::_Destroy<Noisy>(Noisy*)", "caller")
    alone = stack("main", "start")
    for root in ("rv", "pv"):
        assert native._typed(below[0], [root, "[0]"], walked=True)[2] is True
        shown = native.preview_value(
            element, fetch, None, frame=below[0], steps=[root, "[0]"], stack=below, threads=one
        )
        assert shown is None
        shown = native.preview_value(
            element, fetch, None, frame=alone[0], steps=[root, "[0]"], stack=alone, threads=one
        )
        assert shown == "{x = 1, y = 2}"
    settled = stack("main", "start")
    shown = native.preview_value(
        element, fetch, None, frame=settled[0], steps=["vp", "[0]"], stack=settled, threads=one
    )
    assert shown == "{x = 1, y = 2}"
    vector = node("v", "size=1", "std::vector<int>", 2)
    shown = native.preview_value(
        vector, fetch, None, frame=settled[0], steps=["v"], stack=settled, threads=one
    )
    assert shown == "size=1 [1]"
    # An optimized build: the summary alone.
    optimized = session(optimization="O2", fields=CONTAINERS)
    optimized.declarations = native.declarations
    shown = optimized.preview_value(
        vector, fetch, None, frame=settled[0], steps=["v"], stack=settled, threads=one
    )
    assert shown is None
    # Another thread, which may be changing it: the summary alone.
    shown = native.preview_value(
        vector, fetch, None, frame=settled[0], steps=["v"], stack=settled, threads=lambda: 3
    )
    assert shown is None


def test_a_lifetime_under_way_below_a_container_withholds_it():
    # Review of #181: `slot->~E(); ::new (slot) E(3);` in `main` leaves `v` holding an
    # element whose lifetime ended, with no library frame on the stack.
    native = session()
    frames = stack("E::~E()", "E::~E()", "main", "start")
    assert native.containers_settled(frames[2], False, frames, threads=one) is False
    for name in ("Widget::Widget(int)", "ns::Box<int>::~Box()", "E::operator=(E const&)"):
        below = stack(name, "main")
        assert native.containers_settled(below[1], False, below, threads=one) is False
    # The frame's own constructor or destructor holds its containers whole, unless they
    # are reached through a pointer or reference (an owner's vector holding `*this`).
    own = stack("Owner::~Owner()", "main")
    assert native.containers_settled(own[0], False, own, threads=one) is True
    assert native.containers_settled(own[0], True, own, threads=one) is False
    calls = stack("Widget::reset()", "E::operator==(E const&)", "main")
    assert native.containers_settled(calls[2], False, calls, threads=one) is True


def test_a_local_reached_through_a_pointer_is_read_as_one():
    # Review of #181: a structured binding of a reference (`DW_OP_deref`) is typed as
    # the vector itself; its container must meet the whole-stack rule.
    native = session(fields=CONTAINERS)
    native.declarations[0]["types"]["items"] = {(50, "automatic", None)}
    native.declarations[0]["types"]["own"] = {(50, "automatic", None)}
    native.declarations[0]["referred"] = frozenset({"items"})
    frame = {"id": 1}
    assert native._typed(frame, ["items"], walked=True)[1] is True
    assert native._typed(frame, ["own"], walked=True)[1] is False
    # Where the locations were not read, every local counts as one.
    del native.declarations[0]["referred"]
    assert native._typed(frame, ["own"], walked=True)[1] is True
    vector = node("items", "size=1", "std::vector<int>", 2)
    fetch = Debugger({2: elements("1")})
    native.declarations[0]["referred"] = frozenset({"items"})
    below = stack("main", "start", "std::thread::_State_impl<…>::_M_run()")
    shown = native.preview_value(
        vector, fetch, None, frame=below[0], steps=["items"], stack=below, threads=one
    )
    assert shown is None
    shown = native.preview_value(
        node("own", "size=1", "std::vector<int>", 2),
        fetch,
        None,
        frame=below[0],
        steps=["own"],
        stack=below,
        threads=one,
    )
    assert shown == "size=1 [1]"


def test_why_a_row_read_through_a_changing_container_is_withheld():
    # #187: the rows below a container a library call may be changing (#181's rule,
    # without its one-thread part): the reason names the call and the container.
    from peeky.native import _short_frame

    native = session(fields=CONTAINERS)
    native.declarations[0]["types"]["vp"] = {(51, "automatic", None)}
    native.declarations[0]["types"]["rv"] = {(63, "automatic", None)}
    native.declarations[0]["types"]["pv"] = {(64, "automatic", None)}
    native.declarations[0]["types"]["b"] = {(59, "automatic", None)}
    clear = "std::__1::vector<Noisy, std::__1::allocator<Noisy>>::clear[abi:ne180100]()"
    below = stack("Noisy::~Noisy()", clear, "main", "start")
    reason = native.changing_container(below[2], ["vp", "[1]", "x"], below)
    assert reason.label == "not shown", reason
    assert "below this frame (std::__1::vector<…>::clear)" in reason and "`vp`" in reason
    assert native.changing_container(below[2], ["vp", "[1]"], below) is not None
    # Not read through a container, or a root: never.
    assert native.changing_container(below[2], ["b", "n"], below) is None
    assert native.changing_container(below[2], ["vp"], below) is None
    # Its own frame's container, with the library call its caller's: read.
    assert native.changing_container(below[0], ["vp", "[1]"], below) is None
    # Through a reference or pointer, a library call anywhere on the stack counts.
    for root, shown in (("rv", "`rv`"), ("pv", "`pv[0]`")):
        reason = native.changing_container(below[0], [root, "[1]"], below)
        assert "pointer or reference" in reason and shown in reason, reason
    alone = stack("count(Owner const&)", "main", "start")
    assert native.changing_container(alone[0], ["rv", "[1]"], alone) is None
    # A lifetime under way (#181 review): below the frame, or anywhere through a
    # reference (the destructor's own frame too).
    ended = stack("E::~E()", "main", "start")
    reason = native.changing_container(ended[1], ["vp", "[0]", "x"], ended)
    assert "constructor, destructor or assignment operator below this frame (E::~E)" in reason
    assert native.changing_container(ended[0], ["vp", "[0]"], ended) is None
    reason = native.changing_container(ended[0], ["rv", "[0]"], ended)
    assert "while a constructor, destructor or assignment operator runs" in reason, reason
    # Frames that cannot be told: withheld, and said so.
    assert "cannot read every frame" in native.changing_container(below[2], ["vp", "[1]"], [])
    assert (
        session("rust", fields=CONTAINERS).changing_container(below[2], ["vp", "[1]"], below)
        is None
    )
    assert _short_frame("void std::_Destroy<Noisy*, Noisy>(Noisy*, Noisy*)") == "std::_Destroy<…>"
    assert _short_frame(None) == "a frame with no name"
