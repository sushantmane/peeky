"""C and C++ previews (#136): written from the debugger's children, bounded, and never
guessing (unions, pointers, values with no value, standard-library types)."""

from types import SimpleNamespace

import pytest

from peeky.adapters import ViewBudgetExhausted
from peeky.cxx_strings import shorten
from peeky.native_previews import ITEMS, LENGTH, preview


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


KINDS = {"Num": "union", "Pt *": "pointer", "std::string": "library", "Pt &": "reference"}


def kind(name):
    return KINDS.get(name)


def text(child):
    return None if child["value"] == "<no text>" else child["value"]


PT = [node("x", "1"), node("y", "2")]


def test_a_struct_and_its_nested_structs():
    fetch = Debugger({1: [node("a", "Pt @ 0x10", "Pt", 2), node("tag", "7")], 2: PT})
    assert (
        preview(node("s", "S @ 0x10", "S", 1), fetch, kind, text) == "{a = {x = 1, y = 2}, tag = 7}"
    )
    # Deeper than two levels: elided.
    deep = Debugger(
        {1: [node("s", "S @ 0x10", "S", 2)], 2: [node("a", "Pt @ 0x10", "Pt", 3)], 3: PT}
    )
    assert preview(node("o", "O @ 0x10", "O", 1), deep, kind, text) == "{s = {a = {…}}}"
    assert deep.requests == [(1, 64), (2, 64)]


def test_arrays_read_only_the_elements_shown():
    elements = [node(f"[{i}]", str(i)) for i in range(1000)]
    fetch = Debugger({1: elements})
    shown = preview(node("a", "int[1000] @ 0x10", "int[1000]", 1), fetch, kind, text)
    assert shown == "[" + ", ".join(str(i) for i in range(ITEMS)) + ", …]"
    assert fetch.requests == [(1, ITEMS)]
    rows = [node("[0]", "int[3] @ 0x10", "int[3]", 2), node("[1]", "int[3] @ 0x1c", "int[3]", 3)]
    grid = Debugger({1: rows, 2: [node(f"[{i}]", str(i)) for i in range(3)], 3: []})
    # Elements the type has but the debugger did not list are elided, never left out.
    assert preview(node("g", "int[2][3] @ 0x10", "int[2][3]", 1), grid, kind, text) == (
        "[[0, 1, 2], […]]"
    )
    # A flexible array member has no length the debugger knows.
    flexible = Debugger({1: [node("n", "2"), node("data", "int[] @ 0x10", "int[]", 2)]})
    assert preview(node("f", "F @ 0x10", "F", 1), flexible, kind, text) == "{n = 2, data = […]}"


def test_nothing_is_guessed():
    members = [
        node("num", "Num @ 0x10", "Num", 2),
        node("", "S::(anonymous union) @ 0x14", "S::(anonymous union)", 3),
        node("<null>", "S::(anonymous struct) @ 0x18", "S::(anonymous struct)", 4),
        node("next", "0x1000", "Pt *", 5),
        node("label", '0x2000 "edge"', "const char *", 6),
        node("fn", "0x3000 (program`twice(int) at a.cpp:3)", "Callback", 0),
        node("gone", "<variable not available>"),
        node("s", '"hi"', "std::string", 0),
        node("v", "std::vector<int> @ 0x20", "std::vector<int>", 7),
        node("_vptr$Base", "0x0000000000004000", "int (**)()", 8),
    ]
    fetch = Debugger({1: members, 4: PT})
    shown = preview(node("s", "S @ 0x10", "S", 1), fetch, kind, text)
    assert shown == (
        "{num = <union>, <union>, {x = 1, y = 2}, next = 0x1000, label = 0x2000, fn = 0x3000, "
        'gone = <no value>, s = "hi", …}'
    )
    # Only the anonymous struct was opened: unions, pointers and std types never are.
    assert fetch.requests == [(1, 64), (4, 64)]
    assert preview(node("u", "Num @ 0x10", "Num", 1), fetch, kind, text) is None
    assert preview(node("p", "0x0000000000001000", "Pt *", 1), fetch, kind, text) is None
    # What a pointer points at, a scalar the debugger lists as `*p`: no struct.
    star = Debugger({1: [node("*p", "5")]})
    assert preview(node("p", "Pt @ 0x10", "Pt", 1), star, kind, text) is None


def test_a_reference_shows_its_address_then_what_it_refers_to():
    fetch = Debugger({1: PT})
    assert preview(node("r", "0x0000000000001000", "Pt &", 1), fetch, kind, text) == (
        "0x0000000000001000 {x = 1, y = 2}"
    )


def test_a_preview_is_bounded():
    many = [node(f"m{i}", "1") for i in range(12)]
    assert preview(node("w", "W @ 0x10", "W", 1), Debugger({1: many}), kind, text) == (
        "{" + ", ".join(f"m{i} = 1" for i in range(ITEMS)) + ", …}"
    )
    long = [node(f"name{i}", "x" * 39, "Kind", 0) for i in range(8)]
    shown = preview(node("w", "W @ 0x10", "W", 1), Debugger({1: long}), kind, text)
    assert len(shown) <= LENGTH and shown.endswith(", …}"), shown
    texts = [node("t", '"' + "z" * 100 + '"', "char[101]", 2)]
    shown = preview(node("w", "W @ 0x10", "W", 1), Debugger({1: texts}), kind, text)
    assert shown == '{t = "' + "z" * 35 + '"...}'


def test_a_spent_budget_leaves_the_debuggers_text():
    fetch = Debugger({1: PT}, budget=0)
    assert preview(node("p", "Pt @ 0x10", "Pt", 1), fetch, kind, text) is None
    nested = Debugger({1: [node("a", "Pt @ 0x10", "Pt", 2)], 2: PT}, budget=1)
    assert preview(node("s", "S @ 0x10", "S", 1), nested, kind, text) is None


def test_values_with_their_own_text_are_left_as_they_are():
    fetch = Debugger({1: PT})
    for value in ('"hello"', "size=2", "pair", "<error: variable not available>"):
        assert preview(node("v", value, "S", 1), fetch, kind, text) is None
    assert preview(node("v", "S @ 0x10", "S", 0), fetch, kind, text) is None
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


UNIONS = """
0x0000000b: DW_TAG_compile_unit
              DW_AT_name\t("a.c")
0x00000010:   DW_TAG_union_type
                DW_AT_name\t("num")
0x00000018:   DW_TAG_union_type
                DW_AT_byte_size\t(0x04)
0x00000020:   DW_TAG_typedef
                DW_AT_type\t(0x00000018 "union word_t")
                DW_AT_name\t("word_t")
0x00000028:   DW_TAG_typedef
                DW_AT_type\t(0x00000030 "const num")
                DW_AT_name\t("cnum")
0x00000030:   DW_TAG_const_type
                DW_AT_type\t(0x00000010 "num")
0x00000038:   DW_TAG_structure_type
                DW_AT_name\t("pt")
0x00000040:   DW_TAG_namespace
                DW_AT_name\t("geo")
0x00000048:     DW_TAG_union_type
                  DW_AT_name\t("Cell")
0x00000050:     NULL
0x00000058:   NULL
"""


def session(unions=frozenset(), typedefs=None, language="cpp"):
    from peeky.native import NativeSession

    native = NativeSession.__new__(NativeSession)
    native.target = SimpleNamespace(language=language)
    native.unions = unions
    native.typedefs = typedefs or {}
    return native


def test_unions_are_known_by_every_name_lldb_shows():
    from peeky.native import union_names

    assert union_names(UNIONS) == {"num", "word_t", "cnum", "Cell", "geo::Cell"}


def test_a_types_kind_for_a_preview():
    native = session(
        frozenset({"Num", "word_t"}),
        {
            "RowPtr": ("pointer", "Row"),
            "RowRef": ("reference", "Row"),
            "NumRef": ("reference", "Num"),
            "Twice": ("unknown", ""),
            "Names": ("other", "std::vector<int>"),
        },
    )
    for written, expected in [
        ("Pt", None),
        ("const Pt", None),
        ("Pt[3]", None),
        ("Pt *", "pointer"),
        ("const Pt *const", "pointer"),
        ("int (*)(int)", "pointer"),
        ("RowPtr", "pointer"),
        ("const Pt &", "reference"),
        ("Pt &&", "reference"),
        ("RowRef", "reference"),
        # A reference is read only to what a preview would show.
        ("const Num &", "pointer"),
        ("NumRef", "pointer"),
        ("std::string &", "pointer"),
        ("Num", "union"),
        ("const word_t", "union"),
        ("union u", "union"),
        ("S::(anonymous union)", "union"),
        ("tagged::(unnamed union)", "union"),
        ("std::vector<int>", "library"),
        ("Names", "library"),
        ("Twice", "unknown"),
    ]:
        assert native.preview_kind(written) == expected, written


def test_only_c_and_cpp_with_the_debug_information_read_get_previews():
    variable = node("p", "Pt @ 0x10", "Pt", 1)
    fetch = Debugger({1: PT})
    assert session().preview_value(variable, fetch, None) == "{x = 1, y = 2}"
    assert session(language="c").preview_value(variable, fetch, None) == "{x = 1, y = 2}"
    assert session(language="rust").preview_value(variable, fetch, None) is None
    assert session(unions=None).preview_value(variable, fetch, None) is None


def test_what_a_pointer_points_at_when_a_selector_asks():
    native = session(typedefs={"PtPtr": ("pointer", "Pt")})
    fetch = Debugger({1: PT})
    pointer = node("p", "0x0000000000001000", "Pt *", 1)
    assert native.preview_value(pointer, fetch, None, pointee=True) == "{x = 1, y = 2}"
    typed = node("p", "0x0000000000001000", "PtPtr", 1)
    assert native.preview_value(typed, fetch, None, pointee=True) == "{x = 1, y = 2}"
    for value, kind_name in (
        ("0x0000000000000000", "Pt *"),
        ("0x1000", "Pt"),
        ("Pt @ 0x1000", "Pt *"),
    ):
        assert (
            native.preview_value(node("p", value, kind_name, 1), fetch, None, pointee=True) is None
        )


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
    # What a pointer points at, as `p[0]` asked for it.
    worker = Worker({"value": "{x = 1}"})
    assert _previewed(worker, stop, {**found, "name": "*p"})["value"] == "{x = 1}"
    assert worker.calls[0][1]["pointee"] is True
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
