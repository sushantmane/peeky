"""A C or C++ pointer's notes never contradict each other, and a value read through a
pointer says which other captured pointer holds its address.

playlist: `end … [now + 0 by address; same array not proven; at the
address one past queue's end]` said "same array not proven" beside a fact that both
pointers are one past `queue`'s end, in words that read like code; and `now.title
"Coda"`, read through `now` while it held `end`'s address, one past the last track the
program counts, showed nothing to tie it to `end`.
"""

import itertools
import re

import pytest

from peeky.cli import _get_lines
from peeky.probe import _pointed, _table, pointed_text

TRACK = {"pointee": 11, "element_size": 32}
QUEUE = {"address": 0x1000, "length": 32, "element": 11, "element_size": 32}


def at(address):
    return f"0x{address:016x}"


def row(selector, value, kind="Track *", **more):
    return {
        "hit": 1,
        "phase": "observed",
        "selector": selector,
        "value": value,
        "type": kind,
        **more,
    }


OFFSETS = {
    "none": None,
    "zero": {"selector": "now", "offset": 0},
    "after": {"selector": "now", "offset": 6},
    "before": {"selector": "now", "offset": -2},
}
ELEMENTS = {
    "none": None,
    "element": {"selector": "queue", "index": 3, "length": 32},
    "one past": {"selector": "queue", "index": 32, "length": 32},
    "object": {"selector": "x", "index": 0},
    "one past object": {"selector": "x", "index": 1},
    "other array's start": {"selector": "b", "index": 0, "length": 4},
}
ENDS = {
    "none": None,
    "queue": {"selector": "queue", "length": 32},
    "queue in frame 1": {"selector": "queue", "length": 32, "frame": 1, "function": "main"},
    "queue, also b[0]": {"selector": "queue", "length": 32, "also": ["b[0]"]},
}
THROUGH = {
    "none": None,
    "one": {"selector": "now", "same_address_as": ["end"]},
    "two": {"selector": "now", "same_address_as": ["end", "last"]},
    "three": {"selector": "now", "same_address_as": ["end", "last", "tail"]},
}


def facts(offset, element, end, through):
    found = {
        "offset_from": OFFSETS[offset],
        "at_element_of": ELEMENTS[element],
        "past_end": ENDS[end],
        "read_through": THROUGH[through],
    }
    return {key: value for key, value in found.items() if value is not None}


# Every combination the note builder is given, with what it says. `_pointed` gives a row
# an offset only where no element is named, and an element of the array it is one past
# only at that array's end (both kept here, to show the element wins).
@pytest.mark.parametrize(
    "offset, element, end, through, said",
    [
        ("none", "none", "none", "none", ""),
        ("zero", "none", "none", "none", "same address as `now`"),
        ("after", "none", "none", "none", "now + 6 by address; same array not proven"),
        ("before", "none", "none", "none", "now - 2 by address; same array not proven"),
        # playlist's row: both pointers are one past `queue`'s end.
        (
            "zero",
            "none",
            "queue",
            "none",
            "same address as `now`; at the address one past queue's end",
        ),
        (
            "zero",
            "none",
            "queue in frame 1",
            "none",
            "same address as `now`; at the address one past queue's end in frame 1",
        ),
        (
            "after",
            "none",
            "queue",
            "none",
            "now + 6 by address; same array not proven; at the address one past queue's end",
        ),
        (
            "none",
            "none",
            "queue, also b[0]",
            "none",
            "at the address one past queue's end, also b[0]'s",
        ),
        ("none", "element", "none", "none", "at queue[3]'s address"),
        ("zero", "element", "none", "none", "at queue[3]'s address"),
        ("none", "one past", "none", "none", "at the address one past queue's end"),
        ("none", "one past", "queue", "none", "at the address one past queue's end"),
        ("none", "object", "none", "none", "at x's address"),
        ("none", "one past object", "none", "none", "at the address one past x's end"),
        (
            "none",
            "other array's start",
            "queue",
            "none",
            "at b[0]'s address; at the address one past queue's end",
        ),
        (
            "none",
            "none",
            "none",
            "one",
            "read through `now`, which holds the same address as `end`",
        ),
        (
            "none",
            "none",
            "none",
            "two",
            "read through `now`, which holds the same address as `end` and `last`",
        ),
        (
            "none",
            "none",
            "none",
            "three",
            "read through `now`, which holds the same address as `end`, `last` and `tail`",
        ),
        # A pointer read through another: its own address first, then where it was read.
        (
            "zero",
            "none",
            "none",
            "one",
            "same address as `now`; read through `now`, which holds the same address as `end`",
        ),
    ],
)
def test_each_combination_says_what_it_should(offset, element, end, through, said):
    assert pointed_text(facts(offset, element, end, through)) == said


def test_no_combination_contradicts_itself_or_reads_like_code():
    for combination in itertools.product(OFFSETS, ELEMENTS, ENDS, THROUGH):
        text = pointed_text(facts(*combination))
        offset = OFFSETS[combination[0]]
        named = ELEMENTS[combination[1]] is not None
        # An equal address is never shown as arithmetic, nor doubted as one array.
        assert not re.search(r"[+-] 0 by address", text), (combination, text)
        if "same array not proven" in text:
            assert offset is not None and offset["offset"] != 0 and not named, (combination, text)
        # Where its own address is another's, it never doubts that both are in one array.
        own = text.split("read through")[0]
        assert not ("same address as" in own and "same array not proven" in own), text
        # Each fact once.
        for clause in text.split("; "):
            assert text.split("; ").count(clause) == 1, (combination, text)
        assert text.count("one past queue's end") <= 1, (combination, text)
        assert text.count("read through") <= 1, (combination, text)


def table(rows):
    return _table(
        {
            "rows": list(rows.values()),
            "interventions": [],
            "status": "completed",
            "hits": 1,
            "seconds": 1,
        }
    )


def stop(*rows_and_extents):
    rows = [each for each, _ in rows_and_extents]
    extents = {index: extent for index, (_, extent) in enumerate(rows_and_extents) if extent}
    _pointed(rows, extents)
    return {each["selector"]: each for each in rows}


def test_two_pointers_one_past_the_same_array_say_one_address():
    # playlist's 32 tracks, then `<`: `now` and `end` both one past `queue`'s end,
    # `queue` not captured (the worker's `past_end` says where).
    past = {"selector": "queue", "length": 32}
    rows = stop(
        (row("now", at(0x1400), past_end=past), TRACK),
        (row("end", at(0x1400), past_end=past), TRACK),
    )
    assert rows["end"]["offset_from"] == {"selector": "now", "offset": 0}
    assert pointed_text(rows["now"]) == "at the address one past queue's end"
    assert pointed_text(rows["end"]) == (
        "same address as `now`; at the address one past queue's end"
    )
    shown = table(rows)
    assert "[same address as `now`; at the address one past queue's end]" in shown, shown
    assert "same array not proven" not in shown and "now + 0" not in shown, shown
    # A pointer elsewhere still says how far it is, and that no array is proven.
    rows = stop((row("now", at(0x1000)), TRACK), (row("end", at(0x1060)), TRACK))
    assert pointed_text(rows["end"]) == "now + 3 by address; same array not proven"


def test_a_value_read_through_a_pointer_says_which_others_hold_its_address():
    # playlist's hit 4: after `-4`, `end` is at queue[3], and `<` from the first
    # track set `now` there too. `now.title` is "Coda", a track the program no longer counts.
    rows = stop(
        (row("now", at(0x1060)), TRACK),
        (row("end", at(0x1060)), TRACK),
        (row("queue", "[…]", "Track[32]"), QUEUE),
        (row("now.title", '"Coda"', "std::string"), None),
        (row("end.plays", "0", "int"), None),
        (row("now[0]", '{title = "Coda", plays = 0}', "Track"), None),
        (row("now[0].plays", "0", "int"), None),
        # Through a second pointer, or another element: elsewhere, so nothing is said.
        (row("now.next.title", '"Aria"', "std::string"), None),
        (row("now[1]", '{title = "", plays = 0}', "Track"), None),
        (row("now.title.size", "4", "size_t"), None),
        ({"hit": 1, "phase": "observed", "selector": "end.title", "error": "not read"}, None),
    )
    assert rows["now"]["at_element_of"] == rows["end"]["at_element_of"]
    assert rows["now.title"]["read_through"] == {"selector": "now", "same_address_as": ["end"]}
    assert rows["end.plays"]["read_through"] == {"selector": "end", "same_address_as": ["now"]}
    assert rows["now[0]"]["read_through"]["selector"] == "now"
    assert rows["now[0].plays"]["read_through"]["selector"] == "now"
    for elsewhere in ("now.next.title", "now[1]", "now.title.size", "now", "end", "end.title"):
        assert "read_through" not in rows[elsewhere], elsewhere
    assert pointed_text(rows["now.title"]) == (
        "read through `now`, which holds the same address as `end`"
    )
    shown = table(rows)
    assert '"Coda"  [read through `now`, which holds the same address as `end`]' in shown, shown
    assert _get_lines(rows["now.title"])[0] == (
        'now.title: std::string = "Coda"  '
        "[read through `now`, which holds the same address as `end`]"
    )


def test_only_another_pointer_of_its_type_at_a_nonzero_address_counts():
    alone = stop((row("now", at(0x1060)), TRACK), (row("now.title", '"Coda"'), None))
    assert "read_through" not in alone["now.title"]
    elsewhere = stop(
        (row("now", at(0x1060)), TRACK),
        (row("end", at(0x1080)), TRACK),
        (row("now.title", '"Coda"'), None),
    )
    assert "read_through" not in elsewhere["now.title"]
    # A `char *` at the same address, or a pointer with a note, proves nothing of it.
    other_type = stop(
        (row("now", at(0x1060)), TRACK),
        (row("bytes", at(0x1060), "char *"), {"pointee": 2, "element_size": 1}),
        (row("now.title", '"Coda"'), None),
    )
    assert "read_through" not in other_type["now.title"]
    noted = stop(
        (row("now", at(0x1060)), TRACK),
        (row("end", at(0x1060), note="leftovers"), TRACK),
        (row("now.title", '"Coda"'), None),
    )
    assert "read_through" not in noted["now.title"]
    # Two null pointers read nothing.
    null = stop(
        (row("now", at(0)), TRACK),
        (row("end", at(0)), TRACK),
        (row("now.title", '"Coda"'), None),
    )
    assert "read_through" not in null["now.title"]
    # A self-referencing node: its own `next` is no other pointer to name on its row.
    node = stop(
        (row("node", at(0x1060)), TRACK),
        (row("node.next", at(0x1060)), TRACK),
    )
    assert "read_through" not in node["node.next"]


ITEM_POINTER = {"pointee": 99, "element_size": 8, "indirect": True}


def test_the_nearest_captured_pointer_is_the_one_read_through():
    # `slots[0].uses` said nothing, as only `slots` (an array) was
    # looked for; and `pp[0].uses` was tied to `pp`, whose alias `qq` holds another
    # address than the `Item` read.
    slots = {"address": 0x2000, "length": 3, "element": 99, "element_size": 8}
    rows = stop(
        (row("slots", "[…]", "Item *[3]"), slots),
        (row("slots[0]", at(0x1060)), TRACK),
        (row("end", at(0x1060)), TRACK),
        (row("slots[0].uses", "0", "int"), None),
        (row("slots[1].uses", "0", "int"), None),
    )
    assert rows["slots[0].uses"]["read_through"] == {
        "selector": "slots[0]",
        "same_address_as": ["end"],
    }
    assert "read_through" not in rows["slots[1].uses"]
    pp = (row("pp", at(0x2000), "Item **"), ITEM_POINTER)
    qq = (row("qq", at(0x2000), "Item **"), ITEM_POINTER)
    rows = stop(
        pp,
        qq,
        (row("pp[0]", at(0x1060)), TRACK),
        (row("end", at(0x1060)), TRACK),
        (row("pp[0].uses", "0", "int"), None),
    )
    assert rows["pp[0].uses"]["read_through"] == {"selector": "pp[0]", "same_address_as": ["end"]}
    # `pp[0]` not captured: the `Item` is read through it, at no captured pointer's address.
    rows = stop(pp, qq, (row("end", at(0x1060)), TRACK), (row("pp[0].uses", "0", "int"), None))
    assert "read_through" not in rows["pp[0].uses"]
    # `pp[0]` itself is at `pp`'s address, which `qq` holds; a member of `*pp`, there.
    rows = stop(pp, qq, (row("pp[0]", at(0x1060)), None), (row("pp.uses", "0", "int"), None))
    assert rows["pp[0]"]["read_through"] == {"selector": "pp", "same_address_as": ["qq"]}
    assert "read_through" not in rows["pp.uses"]


def test_a_value_read_through_a_captured_second_pointer_says_so():
    # `node.next.qty` reads through `node.next`: said where it is captured and another
    # captured pointer holds its address; never `node`'s.
    rows = stop(
        (row("node", at(0x1000)), TRACK),
        (row("node.next", at(0x1060)), TRACK),
        (row("other", at(0x1060)), TRACK),
        (row("head", at(0x1000)), TRACK),
        (row("node.next.qty", "3", "int"), None),
    )
    assert rows["node.next.qty"]["read_through"] == {
        "selector": "node.next",
        "same_address_as": ["other"],
    }
    rows = stop(
        (row("node", at(0x1000)), TRACK),
        (row("head", at(0x1000)), TRACK),
        (row("node.next.qty", "3", "int"), None),
    )
    assert "read_through" not in rows["node.next.qty"]


def test_session_get_says_it_of_the_children_it_lists():
    # `session get now end` listed `now.title` under `now` with nothing.
    def listed(name, address):
        return row(
            name,
            at(address),
            children=[
                {
                    "name": "title",
                    "selector": f"{name}.title",
                    "value": '"Coda"',
                    "type": "std::string",
                },
                {"name": "plays", "selector": f"{name}.plays", "value": "0", "type": "int"},
                # A pointer member at the same address is no candidate: only what was given.
                {
                    "name": "self",
                    "selector": f"{name}.self",
                    "value": at(address),
                    "type": "Track *",
                },
                # A child not read keeps a placeholder value.
                {
                    "name": "lost",
                    "selector": f"{name}.lost",
                    "value": "<error: cannot read>",
                    "type": "int",
                    "unavailable": True,
                    "reason": "cannot read",
                },
                {"name": "held", "selector": f"{name}.held", "value": "", "withheld": True},
            ],
        )

    rows = stop((listed("now", 0x1060), TRACK), (listed("end", 0x1060), TRACK))
    children = {child["selector"]: child for each in rows.values() for child in each["children"]}
    for unread in ("now.lost", "now.held", "end.lost"):
        assert "read_through" not in children[unread], children[unread]
    assert "  now.lost: int = <no readable value: cannot read>" in _get_lines(rows["now"])
    assert children["now.title"]["read_through"] == {"selector": "now", "same_address_as": ["end"]}
    assert children["end.plays"]["read_through"] == {"selector": "end", "same_address_as": ["now"]}
    assert children["now.self"]["read_through"] == {"selector": "now", "same_address_as": ["end"]}
    lines = _get_lines(rows["now"])
    assert (
        '  now.title: std::string = "Coda"  '
        "[read through `now`, which holds the same address as `end`]"
    ) in lines, lines
    assert "  now.plays: int = 0  [read through `now`, which holds the same address as `end`]" in (
        lines
    )
    # Apart, nothing.
    rows = stop((listed("now", 0x1060), TRACK), (listed("end", 0x1080), TRACK))
    assert not any("read_through" in child for each in rows.values() for child in each["children"])
