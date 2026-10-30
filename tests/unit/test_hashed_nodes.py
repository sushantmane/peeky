"""A libstdc++ unordered container's entries, read from its own nodes (#360): the debug
information must prove where a node holds its value, the chain must end after exactly
the container's own count, and the entries must be of types Peeky reads there."""

import struct

import pytest

from peeky.hashed_nodes import (
    MAX_CHAIN,
    MAX_NODES,
    Memory,
    Unread,
    Words,
    layout,
    read_entries,
    readable,
    view_children,
    word,
)
from peeky.native_previews import Typed, preview


def record(qualified, size, members=(), bases=(), parameters=(), library=True, kind="struct"):
    """A fields.type_graph record."""
    return (
        "record",
        qualified.rsplit("::", 1)[-1],
        kind,
        size,
        None,
        tuple((name, kind, at, False, None, None) for name, kind, at in members),
        tuple(kind for _, kind, _ in members) + tuple(base for base, _ in bases),
        False,
        False,
        library,
        tuple(base for base, _ in bases),
        qualified,
        tuple(parameters),
        tuple(at for _, at in bases),
    )


def graph(value=1, cache=0, node_cache=0, value_size=4, at=8, extra=None):
    """The types of a `std::unordered_set` of ``value`` (an offset in the graph) whose
    hashtable caches hash codes where ``cache``, with one node type, `_Hash_node<value,
    node_cache>`, holding its value ``at`` bytes in."""
    types = {
        1: ("base", "int", "DW_ATE_signed", 4),
        2: ("pointer", "", 3, 8),
        3: record("std::__detail::_Hash_node_base", 8, [("_M_nxt", 2, 0)]),
        4: record(
            "std::__detail::_Hashtable_traits<false, true, true>",
            1,
            parameters=[("value", "_Cache_hash_code", 9, cache)],
        ),
        5: record(
            "std::_Hashtable<int, int>",
            56,
            [("_M_before_begin", 3, 16), ("_M_element_count", 12, 24)],
            parameters=[("type", "_Key", value, None), ("type", "_Value", value, None)]
            + [("type", "_Traits", 4, None)],
        ),
        6: record(
            "std::unordered_set<int>", 56, [("_M_h", 5, 0)], [], [("type", "_Value", value, None)]
        ),
        7: record(
            "__gnu_cxx::__aligned_buffer<int>", value_size, [], [], [("type", "_Tp", value, None)]
        ),
        8: record(
            "std::__detail::_Hash_node_value_base<int>",
            value_size,
            [("_M_storage", 7, 0)],
            parameters=[("type", "_Value", value, None)],
        ),
        9: ("base", "bool", "DW_ATE_boolean", 1),
        10: record("std::__detail::_Hash_node_value<int, false>", value_size, bases=[(8, 0)]),
        11: record(
            "std::__detail::_Hash_node<int, false>",
            at + value_size + (8 if node_cache else 0),
            bases=[(3, 0), (10, at)],
            parameters=[
                ("type", "_Value", value, None),
                ("value", "_Cache_hash_code", 9, node_cache),
            ],
        ),
        12: ("base", "unsigned long", "DW_ATE_unsigned", 8),
        13: ("base", "double", "DW_ATE_float", 8),
        14: ("qualifier", "const", 1),
    }
    types.update(extra or {})
    return types


def nodes(*values, at=8, size=4):
    """{address: a node's bytes} of a chain holding ``values`` (ints), and its first."""
    held, first = {}, 0
    for index, number in reversed(list(enumerate(values))):
        address = 0x1000 + 0x40 * index
        data = struct.pack("<Q", first) + b"\0" * (at - 8) + number.to_bytes(size, "little")
        held[address], first = data + b"\0" * (0x40 - len(data)), address
    return held, first


def reader(held):
    def read(reference, count):
        address = int(reference, 16)
        for start, data in held.items():
            if start <= address < start + len(data):
                found = data[address - start : address - start + count]
                if len(found) == count:
                    return found
        raise ValueError("Partial memory read")

    return read


def test_entries_are_read_where_the_node_type_places_them():
    types = graph()
    held, first = nodes(9, 4)
    root, fetch = read_entries(types, types[6], 2, Memory(first, reader(held)))
    assert root["value"] == "size=2" and root["view"] == "memory"
    entries = fetch(root["variablesReference"], 8)
    assert [(entry["name"], entry["type"], entry["value"]) for entry in entries] == [
        ("[0]", "int", "9"),
        ("[1]", "int", "4"),
    ]
    assert preview(root, fetch, Typed(types, 6), lambda child: child["value"]) == "size=2 {9, 4}"
    assert [child["value"] for child in view_children(types, root, fetch)] == ["9", "4"]
    # Its own count, not the debugger's: an empty one has no chain.
    root, fetch = read_entries(types, types[6], 0, Memory(0, reader({})))
    assert root["value"] == "size=0" and fetch(root["variablesReference"], 8) == []


def test_a_chain_longer_or_shorter_than_the_count_is_not_read():
    types = graph()
    held, first = nodes(9, 4, 7)
    for count in (2, 4):
        with pytest.raises(Unread):
            read_entries(types, types[6], count, Memory(first, reader(held)))
    # A chain that never ends is longer than any count.
    held[0x1080] = struct.pack("<Q", 0x1000) + held[0x1080][8:]
    with pytest.raises(Unread):
        read_entries(types, types[6], 3, Memory(first, reader(held)))
    # More entries than Peeky walks: not even its chain.
    with pytest.raises(Unread):
        read_entries(types, types[6], MAX_CHAIN + 1, Memory(first, reader(held)))


@pytest.mark.parametrize(
    "change",
    [
        # No node type in the debug information.
        lambda types: types.pop(11),
        # A node type that caches hash codes, for a hashtable that does not.
        lambda types: types.update(
            {
                11: types[11][:12]
                + ((types[11][12][0], ("value", "_Cache_hash_code", 9, 1)),)
                + types[11][13:]
            }
        ),
        # The value's storage holds another type.
        lambda types: types.update(
            {7: record("__gnu_cxx::__aligned_buffer<long>", 4, [], [], [("type", "_Tp", 12, None)])}
        ),
        # Storage smaller than the value.
        lambda types: types.update(
            {7: record("__gnu_cxx::__aligned_buffer<int>", 2, [], [], [("type", "_Tp", 1, None)])}
        ),
        # The link is not at the node's start.
        lambda types: types.update({11: types[11][:13] + ((8, 0),)}),
        # The link is no 8-byte pointer.
        lambda types: types.update({2: ("pointer", "", 3, 4)}),
    ],
)
def test_a_node_the_debug_information_does_not_place_is_not_read(change):
    types = graph()
    change(types)
    held, first = nodes(9)
    with pytest.raises(Unread):
        read_entries(types, types[6], 1, Memory(first, reader(held)))


def test_two_node_types_that_disagree_are_not_read():
    # Two units, one type alike in both, placed differently.
    types = graph()
    types[21] = record(
        "std::__detail::_Hash_node<int, false>",
        24,
        bases=[(3, 0), (10, 16)],
        parameters=[("type", "_Value", 1, None), ("value", "_Cache_hash_code", 9, 0)],
    )
    with pytest.raises(Unread):
        layout(types, types[6])
    del types[21]
    assert layout(types, types[6]) == (1, 8, 4, 4)


def test_a_const_value_is_read_with_its_qualifier():
    types = graph(value=14)
    held, first = nodes(1)
    root, fetch = read_entries(types, types[6], 1, Memory(first, reader(held)))
    assert fetch(root["variablesReference"], 1)[0]["type"] == "const int"


def test_words_are_read_through_variable_paths_inside_the_nodes_only():
    held, first = nodes(9, 4)
    texts = {
        "us._M_h._M_before_begin._M_nxt": f"(std::__detail::_Hash_node_base *) _M_nxt = 0x{first:016x}"
    }
    path = "us._M_h._M_before_begin._M_nxt"
    for address in sorted(held):
        data = held[address]
        texts[path + "->_M_nxt"] = f"0x{struct.unpack_from('<Q', data)[0]:016x}"
        texts[f"{path}[1]._M_nxt"] = f"0x{struct.unpack_from('<Q', data, 8)[0]:016x}"
        path += "->_M_nxt"
    texts[path] = "nullptr"
    asked = []

    def evaluate(text):
        asked.append(text)
        return texts[text]

    types = graph()
    memory = Words("us._M_h._M_before_begin._M_nxt", evaluate)
    root, fetch = read_entries(types, types[6], 2, memory)
    assert [entry["value"] for entry in fetch(root["variablesReference"], 8)] == ["9", "4"]
    assert asked == [
        "us._M_h._M_before_begin._M_nxt",
        "us._M_h._M_before_begin._M_nxt->_M_nxt",
        "us._M_h._M_before_begin._M_nxt[1]._M_nxt",
        "us._M_h._M_before_begin._M_nxt->_M_nxt->_M_nxt",
        "us._M_h._M_before_begin._M_nxt->_M_nxt[1]._M_nxt",
    ]
    assert word("nullptr") == 0 and word("0x0000aaaaaaab12f0") == 0xAAAAAAAB12F0
    with pytest.raises(Unread):
        word("<error: invalid>")


STRING = "std::__cxx11::basic_string<char, std::char_traits<char>, std::allocator<char> >"
TEXTS = {
    30: ("base", "char", "DW_ATE_signed_char", 1),
    31: ("pointer", "", 30, 8),
    32: record(STRING + "::_Alloc_hider", 8, [("_M_p", 31, 0)]),
    33: ("array", "", 30, (16,), False),
    34: record(
        STRING + "::union",
        16,
        [("_M_local_buf", 33, 0), ("_M_allocated_capacity", 12, 0)],
        kind="union",
    ),
    35: record(STRING, 32, [("_M_dataplus", 32, 0), ("_M_string_length", 12, 8), (None, 34, 16)]),
}


HEAP_TEXT = (
    "Peeky reads a string in an unordered container's node only where its text is in the "
    "node: this one's is outside it, in heap memory Peeky can't show is still allocated; "
    "print it from the program"
)


def test_a_strings_text_is_read_from_its_own_buffer_only():
    # #374 review: a string's text outside its node is in heap memory nothing proves still
    # allocated. One destroyed in place (`std::destroy_at(&m[1])`), before a new one is
    # built there, keeps a pointer to its freed text, which read as if it were its own.
    types = graph(value=35, value_size=32, extra=TEXTS)
    heap = 0x9000
    second = struct.pack("<QQQ", 0, heap, 20) + b"\0" * 16
    first = struct.pack("<QQQ", 0x2000, 0x1000 + 24, 2) + b"al" + b"\0" * 14
    held = {0x1000: first + b"\0" * 24, 0x2000: second + b"\0" * 24, heap: b"y" * 20 + b"\0" * 12}
    asked = []

    def read(reference, count):
        asked.append(int(reference, 16))
        return reader(held)(reference, count)

    texts = {}
    path = "s._M_h._M_before_begin._M_nxt"
    texts[path] = "0x0000000000001000"
    for address in (0x1000, 0x2000):
        data = held[address]
        texts[path + "->_M_nxt"] = f"0x{struct.unpack_from('<Q', data)[0]:016x}"
        for k in range(1, 5):
            texts[f"{path}[{k}]._M_nxt"] = f"0x{struct.unpack_from('<Q', data, 8 * k)[0]:016x}"
        path += "->_M_nxt"
    # With `readMemory` or without it: the first's text, in its own buffer, reads; the
    # second's alone is withheld, and nothing is read where its pointer points.
    for memory in (Memory(0x1000, read), Words("s._M_h._M_before_begin._M_nxt", texts.get)):
        root, fetch = read_entries(types, types[6], 2, memory)
        one, two = fetch(root["variablesReference"], 8)
        assert (one["value"], one["text"], one["type"]) == ('"al"', True, STRING), one
        assert two["value"] is None and two["withheld"] and two["reason"] == HEAP_TEXT, two
        assert preview(root, fetch, Typed(types, 6), text) == 'size=2 {"al", <not read>}'
        assert [entry.get("withheld") for entry in view_children(types, root, fetch)] == [
            None,
            True,
        ]
    assert not any(heap <= address < heap + 0x1000 for address in asked), asked
    # A length its own buffer cannot hold is not read.
    held[0x1000] = struct.pack("<QQQ", 0, 0x1000 + 24, 16) + held[0x1000][24:]
    with pytest.raises(Unread):
        read_entries(types, types[6], 1, Memory(0x1000, reader(held)))


def test_a_pair_and_a_struct_are_listed_as_the_debugger_lists_them():
    point = {
        40: record("Pt", 16, [("x", 1, 0), ("y", 43, 8)], library=False),
        41: record("std::__pair_base<int const, Pt>", 1),
        42: record(
            "std::pair<int const, Pt>", 24, [("first", 14, 0), ("second", 40, 8)], bases=[(41, 0)]
        ),
        43: ("base", "long int", "DW_ATE_signed", 8),
    }
    types = graph(value=42, value_size=24, extra=point)
    data = struct.pack("<QiiiIq", 0, 3, 0, 5, 0, -6)
    root, fetch = read_entries(
        types, types[6], 1, Memory(0x1000, reader({0x1000: data + b"\0" * 8}))
    )
    (pair,) = fetch(root["variablesReference"], 8)
    assert pair["value"] == "std::pair<int const, Pt> @ 0x1008"
    first, second = fetch(pair["variablesReference"], 8)
    assert (first["name"], first["type"], first["value"]) == ("first", "const int", "3")
    assert second["value"] == "Pt @ 0x1010"
    (listed,) = view_children(types, root, fetch)
    assert listed["value"] == "std::pair<int const, Pt> @ 0x1008"
    assert [child["value"] for child in listed["children"]] == ["3", "{x = 5, y = -6}"]
    assert [m["value"] for m in listed["children"][1]["children"]] == ["5", "-6"]
    # A struct with a base class, or a bitfield, is not read.
    types[40] = record("Pt", 16, [("x", 1, 0), ("y", 43, 8)], bases=[(41, 0)], library=False)
    with pytest.raises(Unread):
        read_entries(types, types[6], 1, Memory(0x1000, reader({0x1000: data + b"\0" * 8})))


def test_the_worker_shows_a_container_read_from_its_nodes_as_a_value_view(tmp_path):
    from types import SimpleNamespace

    from peeky.native import UNLISTED, Withholding
    from peeky.worker import DebugWorker

    counted, read = [], []

    def unlisted_container(variable, frame, steps, evaluate):
        # The count alone: no node is read here.
        counted.append(steps[-1][0])
        return Withholding("it counts 0 entries", UNLISTED, "cannot be changed")

    def listed_container(withheld, variable, frame, steps, nodes, **stop):
        read.append(steps[-1][0])
        assert nodes is not None and sorted(stop) == ["deeper", "stack", "threads"]
        entries = [{"name": "[0]", "type": "int", "value": "4"}]
        view = {"value": "size=1 {4}", "rust_view": "memory", "children": entries}
        if variable["name"] in ("p", "q"):
            view.update(value="0x10 size=1", pointee="size=1 {4}")
        return view

    runtime = SimpleNamespace(
        assert_current=lambda: None,
        reports_unavailable_as_text=True,
        unlisted_container=unlisted_container,
        listed_container=listed_container,
        aliased_type=lambda kind: "std::unordered_set<int>" if kind == "Ids" else None,
        not_yet_declared=lambda variable, frame, **_: (
            type("Noted", (str,), {"note": "may hold leftovers"})("x")
            if variable["name"] == "late"
            else None
        ),
        checks_past_ends=True,
        pointee_of=lambda frame, steps: {"pointee": "set", "element_size": 56, "count": 1},
        referent_of=lambda frame, steps: {"pointee": "set", "element_size": 56, "count": 1},
        value_extent=lambda variable, frame, steps: None,
        # Which steps are pointers or references; None where the build can't say.
        indirections=lambda frame, steps: (
            None
            if steps[-1] == "lost"
            else [
                (i, "pointer" if step in ("p", "q", "h") else "reference")
                for i, step in enumerate(steps)
                if step in ("p", "q", "h", "r", "s", "seen")
            ]
        ),
    )
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.state, worker.stop_id = runtime, "stopped", "stop1"
    worker.listed_frames = [{"id": 1, "name": "main"}]
    # `p` is proven to point at a live object; `q` (a heap block, a dead frame) is not.
    worker.pointee_unproven = lambda variable, frame, steps: (
        None if variable["name"] == "p" else {"kind": "heap"}
    )
    # A reference's referent: `r` and the member `seen` refer to 0x40, placed; `s` to a
    # heap block.
    worker.judge_pointee = lambda address, pointer, frame: (
        None if address == 0x40 else {"kind": "heap"}
    )
    worker.reset_views()
    rows = [
        {"name": "us", "type": "Ids", "value": "size=0", "variablesReference": 4},
        {
            "name": "p",
            "type": "std::unordered_set<int> *",
            "value": "0x10 size=0",
            "variablesReference": 5,
        },
        {
            "name": "q",
            "type": "std::unordered_set<int> *",
            "value": "0x20 size=0",
            "variablesReference": 6,
        },
        {
            "name": "late",
            "type": "std::unordered_set<int>",
            "value": "size=0",
            "variablesReference": 7,
        },
    ]
    us, p, q, late = worker.expose_variables(rows, {"id": 1}, ("Locals", None), ())
    assert counted == ["us", "p", "q", "late"] and read == ["us", "p"], (counted, read)
    # Its value and entries as read, said so, with what its type names.
    assert (us["value"], us["view"], us["aliased_type"]) == (
        "size=1 {4}",
        "memory",
        "std::unordered_set<int>",
    )
    assert not us.get("unavailable") and us["variablesReference"] > 0
    stop = {"stop_id": "stop1"}
    listed = worker.expand({**stop, "handle": us["variablesReference"]}, runtime)["variables"]
    assert [(e["name"], e["value"], e["view"]) for e in listed] == [("[0]", "4", "memory")]
    # A pointer: its address and count; what it points at, shown whole.
    assert p["value"] == "0x10 size=1"
    shown = worker.preview({**stop, "handle": p["variablesReference"], "pointee": True}, runtime)
    assert shown == {"value": "size=1 {4}"}
    assert worker.preview({**stop, "handle": us["variablesReference"]}, runtime) == {}
    # Never through a pointer not proven to point at a live object, nor where its value
    # may be leftovers: its note makes no listing true. Neither is read at all.
    assert q["withheld_as"] == UNLISTED and q["value"] is None and q["variablesReference"] == 0
    assert late["withheld_as"] == UNLISTED and late["value"] is None
    # Nor through a pointer on the way that the expansion marks unproven, nor below a
    # value shown with a note.
    read.clear()
    member = {
        "name": "us",
        "type": "std::unordered_set<int>",
        "value": "size=0",
        "variablesReference": 8,
    }
    owner = ("h", "Holder *")
    holder = {"name": "h", "type": "Holder *", "value": "0x30"}
    (through,) = worker.expose_variables(
        [member], {"id": 1}, ("Locals", None), (owner,), trail=[holder], through="unproven"
    )
    (noted,) = worker.expose_variables(
        [member], {"id": 1}, ("Locals", None), (("o", "Holder"),), noted=("leftovers", True)
    )
    (unjudged,) = worker.expose_variables(
        [member], {"id": 1}, ("Locals", None), (owner,), trail=[holder]
    )
    assert read == [] and all(row["withheld_as"] == UNLISTED for row in (through, noted, unjudged))
    # A reference is an indirection too (review 2 of #360): read only where its referent
    # is judged live, never where nothing is judged (a value with no address, a step the
    # build can't place).
    rows = [
        {"name": "r", "type": "Ref", "value": "0x40 size=0", "variablesReference": 9},
        {
            "name": "s",
            "type": "std::unordered_set<int> &",
            "value": "0x50 size=0",
            "variablesReference": 10,
        },
        {
            "name": "lost",
            "type": "std::unordered_set<int>",
            "value": "size=0",
            "variablesReference": 11,
        },
    ]
    r, s_, lost = worker.expose_variables(rows, {"id": 1}, ("Locals", None), ())
    assert read == ["r"] and r["view"] == "memory", (read, r)
    assert s_["withheld_as"] == UNLISTED and lost["withheld_as"] == UNLISTED
    viewer = {"name": "view", "type": "Viewer", "value": "Viewer @ 0x60"}
    seen = {
        "name": "seen",
        "type": "std::unordered_set<int> &",
        "value": "0x40 size=0",
        "variablesReference": 12,
    }
    far = {**seen, "value": "0x50 size=0"}
    (shown,) = worker.expose_variables(
        [seen], {"id": 1}, ("Locals", None), (("view", "Viewer"),), trail=[viewer]
    )
    (hidden,) = worker.expose_variables(
        [far], {"id": 1}, ("Locals", None), (("view", "Viewer"),), trail=[viewer]
    )
    assert shown["view"] == "memory" and hidden["withheld_as"] == UNLISTED, (shown, hidden)
    # Through a reference to a struct that holds one: the reference's referent decides.
    boxed = {"name": "s", "type": "Holder &", "value": "0x50"}
    (inner,) = worker.expose_variables(
        [member], {"id": 1}, ("Locals", None), (("s", "Holder &"),), trail=[boxed]
    )
    assert inner["withheld_as"] == UNLISTED, inner
    # A member whose type's name is too long to verify is shown without a view: withheld,
    # never the debugger's miscount.
    long = "std::unordered_set<" + "x" * 2100 + ">"
    (member,) = worker.expose_variables(
        [{"name": "us", "type": long, "value": "size=0", "variablesReference": 8}],
        {"id": 1},
        ("Locals", None),
        (("h", "Holder"),),
    )
    assert member["withheld_as"] == UNLISTED and member["value"] is None, member


@pytest.mark.parametrize(
    "text, asks",
    [
        ('__attribute__((optimize("O2"))) static int f(int x);', True),
        ('static int f(int) __attribute__ ((noinline, __optimize__ ("-O3")));', True),
        ('__attribute__((section("t"), optimize(2))) int g();', True),
        ('[[gnu::optimize("O2")]] int f();', True),
        ('[[nodiscard, gnu::optimize("O1")]] int f();', True),
        # Review 2 of #360: C++17's `using` prefix, which GCC 13 honours at -O0.
        ('[[using gnu: optimize("O2")]] int f();', True),
        ('[[using gnu: noinline, optimize("O2")]] int f();', True),
        ('[[ using __gnu__ : cold , __optimize__ ( "O1" ) ]] int f();', True),
        ('#pragma GCC optimize ("O2")\nint f();', True),
        ('  # pragma GCC optimize("unroll-loops")', True),
        # A function of that name, called, counts too: the detector is shared with the
        # caller-aggregate rule, and never misses one.
        ("int optimize(int route); int r = optimize(3);", True),
        # Not: libstdc++'s regex constants, a comment or a literal, GCC's other pragmas,
        # and clang's `optnone`.
        ("inline constexpr syntax_option_type optimize = _S_optimize;", False),
        ('/* optimize("O2") */ const char *s = "optimize(1)";', False),
        ('#pragma GCC diagnostic ignored "-Wunused"\n#pragma GCC push_options', False),
        ("__attribute__((optnone, noinline)) int f();", False),
    ],
)
def test_a_source_that_may_optimize_a_function_is_told(text, asks):
    # #360 review: a function GCC optimizes may keep a stored value in a register while
    # the nodes and count are unchanged, so no entry is read from the nodes then.
    from peeky.c_source import optimizes

    assert optimizes(text.encode()) is asks


def test_no_node_is_read_in_a_build_that_may_be_optimized():
    from types import SimpleNamespace

    from peeky.native import NativeSession

    session = NativeSession.__new__(NativeSession)
    session.fields, walked = {}, []
    # Past the gate, the container's type is looked up first.
    session._typed = lambda frame, steps, walked_=None, **_: walked.append(steps)
    cases = (("O1", False), ("O2", False), ("O0", True), ("O0", None), ("O0", False))
    for optimization, optimizing in cases:
        session.target = SimpleNamespace(language="cpp", optimization=optimization)
        session.optimized = optimizing
        listing = session._node_listing(
            {"type": "std::unordered_set<int>"},
            {"id": 1},
            [("us", "std::unordered_set<int>")],
            "us._M_h._M_element_count",
            1,
            (lambda path: "0x10", None),
            None,
            None,
            lambda: 1,
        )
        assert listing is None, (optimization, optimizing)
    # Only the unoptimized build whose source asks for no optimization gets past it.
    assert walked == [["us"]], walked


def test_a_row_says_to_read_a_container_later_only_where_its_entries_read_at_all():
    # Eval 13 prep, finding 3: "read it after that call returns" is said only where Peeky
    # reads entries of that type at all, as read_entries does, by their type alone.
    point = {
        40: record("Pt", 16, [("x", 1, 0), ("y", 43, 8)], library=False),
        41: record("std::__pair_base<int const, Pt>", 1),
        42: record(
            "std::pair<int const, Pt>", 24, [("first", 14, 0), ("second", 40, 8)], bases=[(41, 0)]
        ),
        43: ("base", "long int", "DW_ATE_signed", 8),
        44: record("Based", 16, [("x", 1, 0)], bases=[(40, 0)], library=False),
        45: ("base", "char32_t", "DW_ATE_unsigned", 4),
    }
    types = graph(extra=point)
    assert readable(types, 1) and readable(types, 9) and readable(types, 42), types
    # A `double` and a wide character are read (#374); a struct with a base class, a
    # `long double` and a `char8_t` never are.
    assert readable(types, 13) and readable(types, 45) and not readable(types, 44)
    types.update({46: ("base", "long double", "DW_ATE_float", 16)})
    types.update({47: ("base", "char8_t", "DW_ATE_UTF", 1)})
    assert not readable(types, 46) and not readable(types, 47)
    # As read_entries finds them, value by value.
    data = struct.pack("<QiiiIq", 0, 3, 0, 5, 0, -6)
    paired = graph(value=42, value_size=24, extra=point)
    read_entries(paired, paired[6], 1, Memory(0x1000, reader({0x1000: data + b"\0" * 8})))
    based = graph(value=44, value_size=16, extra=point)
    with pytest.raises(Unread):
        read_entries(based, based[6], 1, Memory(0x1000, reader({0x1000: data})))


# #374: more kinds of entries, read where proven, each as LLDB shows one it reads itself.


@pytest.mark.parametrize(
    "packed, shown",
    [
        # As lldb-dap 18 and 20 show these locals (real builds, Linux and macOS).
        (struct.pack("<f", 0.1), "0.100000001"),
        (struct.pack("<f", 1e10), "1.0E+10"),
        (struct.pack("<f", 1234567.0), "1234567"),
        (struct.pack("<f", 1e-3), "0.00100000005"),
        (struct.pack("<f", float("inf")), "+Inf"),
        (struct.pack("<d", 0.1), "0.10000000000000001"),
        (struct.pack("<d", 100.0), "100"),
        (struct.pack("<d", 1e6), "1000000"),
        (struct.pack("<d", 1e7), "1.0E+7"),
        (struct.pack("<d", 1e-7), "9.9999999999999995E-8"),
        (struct.pack("<d", 0.0001), "0.0001"),
        (struct.pack("<d", 1 / 3), "0.33333333333333331"),
        (struct.pack("<d", -2.5), "-2.5"),
        (struct.pack("<d", -0.0), "-0"),
        (struct.pack("<d", 1e300), "1.0000000000000001E+300"),
        (struct.pack("<d", 5e-324), "4.9406564584124654E-324"),
        (struct.pack("<d", float("-inf")), "-Inf"),
        (struct.pack("<d", float("nan")), "NaN"),
    ],
)
def test_a_float_or_double_is_shown_as_lldb_shows_one(packed, shown):
    from peeky.hashed_nodes import float_text

    assert float_text(packed) == shown


def test_every_float_and_double_shown_reads_back_as_itself():
    import random

    from peeky.hashed_nodes import float_text

    chosen = random.Random(374)
    for _ in range(20000):
        data = chosen.getrandbits(64).to_bytes(8, "little")
        (number,) = struct.unpack("<d", data)
        if number == number:
            assert float(float_text(data).replace("Inf", "inf")) == number, number
        data = chosen.getrandbits(32).to_bytes(4, "little")
        (number,) = struct.unpack("<f", data)
        if number == number:
            back = struct.unpack(
                "<f", struct.pack("<f", float(float_text(data).replace("Inf", "inf")))
            )
            assert back[0] == number, number


def test_a_character_is_shown_with_exact_escapes():
    from peeky.hashed_nodes import character_text

    # A `char` as LLDB shows one.
    shown = [character_text(code) for code in (97, 0, 39, 92, 10, 27, 0xC8, 0x7F, 1)]
    assert shown == [
        "'a'",
        "'\\0'",
        "'''",
        "'\\'",
        "'\\n'",
        "'\\e'",
        "'\\xc8'",
        "'\\x7f'",
        "'\\x01'",
    ]
    # A wide one: its literal; never LLDB's `\U0000fffd`, nor nothing, for an invalid one.
    assert character_text(0xE9, "L") == "L'é'" and character_text(0x1F600, "U") == "U'😀'"
    assert character_text(0xFFFFFFFF, "L") == "L'\\xffffffff'"
    assert character_text(0xD800, "u") == "u'\\xd800'"
    assert character_text(0x110000, "U") == "U'\\x110000'"
    assert character_text(0xA0, "U") == "U'\\xa0'" and character_text(10, "u") == "u'\\n'"


def kinds_graph(value, value_size, extra):
    types = graph(value=value, value_size=value_size, extra=extra)
    return types


def one_node(data, at=8):
    """A chain of one node holding ``data`` ``at`` bytes in, at 0x1000."""
    return {0x1000: struct.pack("<Q", 0) + b"\0" * (at - 8) + data + b"\0" * 64}


def test_numbers_characters_enums_and_pointers_are_read():
    kinds = {
        50: ("base", "double", "DW_ATE_float", 8),
        51: ("base", "char", "DW_ATE_unsigned_char", 1),
        52: ("base", "wchar_t", "DW_ATE_signed", 4),
        53: ("base", "char16_t", "DW_ATE_UTF", 2),
        54: (
            "enum",
            "Color",
            4,
            (("Red", 0), ("Green", 5), ("Blue", 5), ("Teal", 6)),
            False,
            "Color",
        ),
        55: ("enum", "Neg", 4, (("Minus", -1),), True, "ns::Neg"),
        56: ("pointer", "", 1, 8),
        57: ("qualifier", "const", 51),
        58: ("pointer", "", 57, 8),
        59: ("qualifier", "const", 58),
        60: ("pointer", "", 56, 8),
    }
    cases = [
        (50, struct.pack("<d", 0.5), "double", "0.5"),
        (51, b"\xc8", "char", "'\\xc8'"),
        (52, struct.pack("<i", -1), "wchar_t", "L'\\xffffffff'"),
        (53, struct.pack("<H", 0x61), "char16_t", "u'a'"),
        (54, struct.pack("<I", 6), "Color", "Teal"),
        # Two enumerators hold 5: neither is told, the number is.
        (54, struct.pack("<I", 5), "Color", "5"),
        (54, struct.pack("<I", 9), "Color", "9"),
        (55, struct.pack("<i", -1), "ns::Neg", "Minus"),
        (55, struct.pack("<i", -5), "ns::Neg", "-5"),
        (56, struct.pack("<Q", 0xAAAAAAAB12F0), "int *", "0x0000aaaaaaab12f0"),
        (59, struct.pack("<Q", 0), "const char *const", "0x0000000000000000"),
        (60, struct.pack("<Q", 0x10), "int **", "0x0000000000000010"),
    ]
    for kind, data, name, value in cases:
        types = kinds_graph(kind, len(data), kinds)
        assert readable(types, kind), name
        root, fetch = read_entries(types, types[6], 1, Memory(0x1000, reader(one_node(data))))
        (entry,) = fetch(root["variablesReference"], 8)
        assert (entry["type"], entry["value"]) == (name, value), (entry, value)
        assert entry["variablesReference"] == 0, entry
    # An enum whose enumerators weren't read, or whose sign isn't known for a value with
    # its top bit set, a `long double` and a pointer to a member: never read.
    unknown = {54: ("enum", "Color", 4, None, False, "Color")}
    unsigned = {54: ("enum", "Color", 4, (("Red", 0),), None, "Color")}
    for extra, data in ((unknown, struct.pack("<I", 0)), (unsigned, struct.pack("<I", 1 << 31))):
        types = kinds_graph(54, 4, extra)
        with pytest.raises(Unread):
            read_entries(types, types[6], 1, Memory(0x1000, reader(one_node(data))))
    for kind, data in (
        (61, b"\0" * 16),
        (62, b"\0" * 8),
    ):
        types = kinds_graph(
            kind,
            len(data),
            {61: ("base", "long double", "DW_ATE_float", 16), 62: ("pointer", "", None, 8)},
        )
        assert not readable(types, kind)
        with pytest.raises(Unread):
            read_entries(
                types,
                types[6],
                1,
                Memory(0x1000, reader(one_node(data, at=16 if kind == 61 else 8))),
            )


VECTOR = "std::vector<int, std::allocator<int> >"
NESTED = {
    # Each entry a std::pair<int const, T>, T placed as libstdc++ places it.
    70: ("pointer", "", 1, 8),
    71: record(
        "std::_Vector_base<int, std::allocator<int> >::_Vector_impl_data",
        24,
        [("_M_start", 70, 0), ("_M_finish", 70, 8), ("_M_end_of_storage", 70, 16)],
    ),
    72: record("std::allocator<int>", 1),
    73: record(
        "std::_Vector_base<int, std::allocator<int> >::_Vector_impl", 24, bases=[(72, 0), (71, 0)]
    ),
    74: record("std::_Vector_base<int, std::allocator<int> >", 24, [("_M_impl", 73, 0)]),
    75: record(VECTOR, 24, bases=[(74, 0)], parameters=[("type", "_Tp", 1, None)]),
    76: record("std::__pair_base<int const, std::vector<int> >", 1),
    77: record(
        "std::pair<int const, std::vector<int> >",
        32,
        [("first", 14, 0), ("second", 75, 8)],
        bases=[(76, 0)],
    ),
    78: record("std::map<int, int>", 48),
    79: record(
        "std::pair<int const, std::map<int, int> >", 56, [("first", 14, 0), ("second", 78, 8)]
    ),
    80: ("array", "", 1, (2,), False),
    81: record(
        "std::array<int, 2>",
        8,
        [("_M_elems", 80, 0)],
        parameters=[("type", "_Tp", 1, None), ("value", "_Nm", 12, 2)],
    ),
    82: record(
        "std::pair<int const, std::array<int, 2> >", 12, [("first", 14, 0), ("second", 81, 4)]
    ),
}


def mapped(value, value_size, held):
    """``graph``'s types, with a `std::unordered_map<int, held>` (90) of ``value`` entries."""
    types = graph(value=value, value_size=value_size, extra=NESTED)
    parameters = [("type", "_Key", 1, None), ("type", "_Tp", held, None)]
    types[90] = record("std::unordered_map<int, T>", 56, [("_M_h", 5, 0)], [], parameters)
    return types


def node_words(data, words):
    """A ``Words`` over one node at 0x1000 holding ``data`` 8 bytes in, ``words`` long."""
    node = one_node(data)[0x1000]
    texts = {"m": "0x1000", "m->_M_nxt": "nullptr"}
    for k in range(1, words):
        texts[f"m[{k}]._M_nxt"] = f"0x{struct.unpack_from('<Q', node, 8 * k)[0]:x}"
    return Words("m", texts.get)


def text(child):
    return child.get("value")


HEAP_BLOCK = (
    "Peeky doesn't read a `{}` held in an unordered container's node: its elements are in a "
    "heap block, which Peeky can't show is still allocated; print it from the program"
)


def test_a_container_held_in_a_node_is_read_from_it_or_withheld_on_its_own():
    # #374 review: a vector's buffer is a heap block nothing proves still allocated. One
    # destroyed in place (`std::destroy_at(&m[1])`), before a new one is built there,
    # keeps its old pointers, and its freed buffer still read `size=2 [2, 3]`.
    types = mapped(77, 32, 75)
    assert readable(types, 77)
    heap = 0x9000
    data = struct.pack("<iiQQQ", 1, 0, heap, heap + 8, heap + 16)
    held = {**one_node(data), heap: struct.pack("<iiii", 2, 3, 0, 0)}
    asked = []

    def read(reference, count):
        asked.append(int(reference, 16))
        return reader(held)(reference, count)

    for memory in (Memory(0x1000, read), node_words(data, 5)):
        root, fetch = read_entries(types, types[90], 1, memory)
        assert preview(root, fetch, Typed(types, 90), text) == "size=1 {1: <not read>}"
        (entry,) = view_children(types, root, fetch)
        assert [part["value"] for part in entry["children"]] == ["1", None], entry
        second = entry["children"][1]
        assert second["withheld"] and second["withheld_as"] == "not read", second
        assert second["reason"] == HEAP_BLOCK.format("std::vector"), second
    # Nothing was read where its pointers point.
    assert not any(heap <= address < heap + 0x1000 for address in asked), asked
    # A `std::map` in a node: withheld, with why.
    types = mapped(79, 56, 78)
    memory = Memory(0x1000, reader(one_node(b"\1" + b"\0" * 55)))
    root, fetch = read_entries(types, types[90], 1, memory)
    (entry,) = view_children(types, root, fetch)
    assert entry["children"][1]["reason"] == (
        "Peeky doesn't read a `std::map` held in an unordered container's node; print it from "
        "the program"
    )
    assert preview(root, fetch, Typed(types, 90), text) == "size=1 {1: <not read>}"
    # A `std::array` is inside the node, which the walk proves: read, without `readMemory`
    # too.
    types = mapped(82, 12, 81)
    root, fetch = read_entries(types, types[90], 1, node_words(struct.pack("<iii", 1, 4, 5), 3))
    assert preview(root, fetch, Typed(types, 90), text) == "size=1 {1: size=2 [4, 5]}"


def test_a_container_of_more_than_256_entries_is_walked_whole_and_read_when_asked():
    from peeky.hashed_nodes import Paged

    types = graph()
    count = MAX_NODES + 44
    held, first = nodes(*range(count))
    asked = []

    def read(reference, size):
        asked.append(int(reference, 16))
        return reader(held)(reference, size)

    root, fetch = read_entries(types, types[6], count, Memory(first, read))
    assert root["value"] == f"size={count}" and root["length"] == count
    listed = view_children(types, root, fetch)
    assert isinstance(listed, Paged) and len(listed) == count
    # Only the first 256 were read with the listing; each later one when asked for.
    assert listed[:2] == [
        {"name": "[0]", "type": "int", "value": "0"},
        {"name": "[1]", "type": "int", "value": "1"},
    ]
    assert [e["value"] for e in listed[count - 2 :]] == [str(count - 2), str(count - 1)]
    assert listed[-1]["name"] == f"[{count - 1}]"
    # Its count is proven by the whole chain: one node more or fewer, and nothing reads.
    for wrong in (count - 1, count + 1):
        with pytest.raises(Unread):
            read_entries(types, types[6], wrong, Memory(first, reader(held)))
    # A later node that doesn't read, or whose link isn't the one the walk read, is
    # withheld alone, with why.
    last, before = 0x1000 + 0x40 * (count - 1), 0x1000 + 0x40 * (count - 2)
    root, fetch = read_entries(types, types[6], count, Memory(first, reader(held)))
    listed = view_children(types, root, fetch)
    del held[last]
    held[before] = struct.pack("<Q", 0x5000) + held[before][8:]
    assert listed[count - 1]["withheld"] and listed[count - 1]["value"] is None
    assert listed[count - 1]["reason"].startswith("the debugger didn't read this entry's node")
    assert listed[count - 2]["reason"] == (
        "Peeky can't read this entry from its node (its link is not the one the walk read); "
        "print it from the program"
    )


def test_the_worker_pages_and_finds_a_key_among_entries_read_when_asked(tmp_path):
    # #374: past 256 entries, a view's entries are read when a page, a position or a key
    # asks for them, on that expansion's own budget, past the inspection window too.
    from types import SimpleNamespace

    from peeky.hashed_nodes import Paged
    from peeky.native import UNLISTED, Withholding
    from peeky.worker import DebugWorker

    made, budgets = [], []

    def entry(at):
        made.append(at)
        parts = [
            {"name": "first", "type": "const int", "value": str(at * 3)},
            {"name": "second", "type": "int", "value": str(at)},
        ]
        return {
            "name": f"[{at}]",
            "type": "std::pair<int const, int>",
            "value": "p",
            "children": parts,
        }

    def page(start, stop):
        budgets.append(worker.view_deadline)
        return [entry(at) for at in range(start, stop)]

    def listed_container(withheld, variable, frame, steps, nodes, **stop):
        children = Paged(700, [entry(at) for at in range(256)], page)
        return {"value": "size=700 {…}", "rust_view": "memory", "children": children}

    runtime = SimpleNamespace(
        assert_current=lambda: None,
        reports_unavailable_as_text=True,
        unlisted_container=lambda variable, frame, steps, evaluate: Withholding(
            "it counts 0 entries", UNLISTED, "cannot be changed"
        ),
        listed_container=listed_container,
        indirections=lambda frame, steps: [],
    )
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.state, worker.stop_id = runtime, "stopped", "stop1"
    worker.listed_frames = [{"id": 1, "name": "main"}]
    worker.reset_views()
    row = {"name": "m", "type": "std::unordered_map<int, int>", "value": "size=0"}
    (m,) = worker.expose_variables(
        [{**row, "variablesReference": 4}], {"id": 1}, ("Locals", None), ()
    )
    assert m["view"] == "memory" and max(made) == 255, (m, max(made))
    stop, handle = {"stop_id": "stop1"}, m["variablesReference"]
    first = worker.expand({**stop, "handle": handle}, runtime)
    assert len(first["variables"]) == 64 and first["truncated"] and max(made) == 255
    # One entry by its position, past the inspection window's 512: read alone.
    (one,) = worker.expand({**stop, "handle": handle, "index": 650}, runtime)["variables"]
    assert one["name"] == "[650]" and one["view"] == "memory" and 650 in made
    assert 649 not in made and budgets, made
    # A key, wherever its entry is: every key compared, each as a selector compares one.
    found = worker.expand({**stop, "handle": handle, "key": "1797"}, runtime)
    assert [entry["name"] for entry in found["variables"]] == ["[599]"] and found["key"] == "1797"
    assert found["compared"] == 700 and found["keys"][:3] == ["0", "3", "6"]
    missing = worker.expand({**stop, "handle": handle, "key": "1"}, runtime)
    assert missing["variables"] == [] and missing["compared"] == 700
    with pytest.raises(ValueError, match="cannot compare"):
        worker.expand({**stop, "handle": handle, "key": '"a"'}, runtime)
    # A key only for entries a view read; never with a page's bounds.
    with pytest.raises(ValueError, match="key, or start"):
        worker.dispatch("expand", {**stop, "handle": handle, "key": "3", "start": 0})


def test_a_listing_says_how_many_more_entries_a_container_holds():
    # #374: a container read from its nodes counts its entries by its whole chain; the
    # listing's last line says how many it doesn't list, and how to read one.
    from peeky import cli

    child = {"type": "int", "value": "2"}
    row = {
        "selector": "big",
        "type": "std::unordered_map<int, int>",
        "value": "size=700 {699: 1398, …}",
        "children": [{**child, "selector": f"big[{699 - at}]"} for at in range(64)],
        "children_truncated": True,
    }
    assert cli._get_lines(row)[-1] == (
        "  ... 56 more listed, big[691] to big[636], then 636 more (700 entries in all): "
        "`--json` has only the first 64; read any entry by key, as `peeky session get "
        "'big[691]'`"
    )
    sets = {**row, "children": [{**child, "selector": f"s[#{at}]"} for at in range(64)]}
    assert (
        cli._get_lines(sets)[-1]
        == "  ... 636 more entries (700 in all); select one by name or index"
    )


def test_a_long_walk_through_variable_paths_is_never_begun_and_a_failed_one_forgets():
    # #374 review: without `readMemory`, node N's path holds N steps, so a walk of 16,384
    # nodes would send about 1.2 GB of paths, each cached for the stop.
    from types import SimpleNamespace

    from peeky.hashed_nodes import MAX_WORDS
    from peeky.native import NativeSession

    types = graph()
    asked = []

    class Paths:
        forgot = 0

        def __call__(self, path):
            asked.append(path)
            return "nullptr" if path.endswith("->_M_nxt") else "0x0000000000001000"

        def forget(self):
            Paths.forgot += 1

    with pytest.raises(Unread):
        read_entries(types, types[6], MAX_WORDS + 1, Words("us", Paths()))
    assert asked == ["us"]
    session = NativeSession.__new__(NativeSession)
    session.fields = types
    session.target = SimpleNamespace(language="cpp", optimization="O0")
    session.optimized = False
    session._typed = lambda frame, steps, walked=False: (Typed(types, 6), False, None, None)
    session.containers_settled = lambda *args: True
    for count, words in ((MAX_WORDS + 1, None), (2, None), (MAX_WORDS + 1, lambda *a: b"")):
        asked.clear()
        listing = session._node_listing(
            {"type": "std::unordered_set<int>"},
            {"id": 1},
            [("us", "std::unordered_set<int>")],
            "us._M_h._M_element_count",
            count,
            (Paths(), words),
            None,
            None,
            lambda: 1,
        )
        assert listing is None, (count, listing)
        if count > MAX_WORDS and words is None:
            # Refused before any path is read.
            assert asked == [], asked
    # The walk of 2 found a chain of 1, and dropped the paths it read; with `readMemory`, a
    # longer count is walked by address.
    assert Paths.forgot == 2, Paths.forgot


def test_a_failed_walks_paths_are_not_cached(tmp_path):
    from peeky.worker import DebugWorker, _NodePaths

    worker = DebugWorker(tmp_path)
    worker.stop_id = "stop1"
    worker.reset_views()
    worker.view_request = lambda command, arguments, remaining: {"result": "0x10"}
    paths = _NodePaths(worker, {"id": 1})
    assert paths("us._M_h._M_before_begin._M_nxt") == "0x10"
    assert paths("us._M_h._M_before_begin._M_nxt->_M_nxt") == "0x10"
    assert len(worker.raw_values[1]) == 2
    paths.forget()
    assert worker.raw_values[1] == {} and paths.asked == []


def test_a_worker_of_another_protocol_is_never_sent_anything_but_its_version(tmp_path):
    # #374 review: a worker of protocol 9 ignores `expand`'s `key` and answers its first
    # entries, and runs `continue` as it reads it. The `Client` SDK (and `python -m
    # peeky.client`) asks a worker's `version` before its first other request, once, and
    # sends nothing else to a worker of another protocol.
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from peeky.client import Client

    asked = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            asked.append(self.path)
            failing = self.path == "/fail"
            answer = json.dumps(
                {"error": {"code": "INVALID_REQUEST", "message": "no"}}
                if failing
                else {"protocol": 9, "variables": [{"name": "[0]", "value": "p"}]}
            ).encode()
            self.send_response(400 if failing else 200)
            self.send_header("X-Peeky-Protocol", "9")
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", "t" * 32)
        for operation in ("continue", "expand", "fail"):
            with pytest.raises(ValueError, match="^Worker speaks protocol 9, this client 10"):
                client.call(operation, handle=1, key="5")
        assert asked == ["/version"], asked
        # Its version is read: its caller compares the protocol it states.
        assert client.call("version")["protocol"] == 9
        # An answer whose header names another protocol, from a worker whose version said
        # this one's, is refused too.
        client.protocol = 10
        with pytest.raises(ValueError, match="its answer to 'continue' .* is not read"):
            client.call("continue")
    finally:
        server.shutdown()
        server.server_close()


def test_a_key_is_read_only_from_an_answer_for_that_key():
    # #374 review: the client took the first entry of a worker that ignored `key` for the
    # entry of that key.
    from peeky.probe import _value_for_key

    def entry(at, key):
        return {
            "name": f"[{at}]",
            "type": "std::pair<int const, int>",
            "value": "p",
            "variablesReference": 10 + at,
            "view": "memory",
            "_key": key,
        }

    class Worker:
        def __init__(self, answer):
            self.answer = answer

        def dispatch(self, operation, args):
            handle = args["handle"]
            if handle == 1 and "key" in args:
                return self.answer
            if handle == 1:
                return {"variables": [entry(0, 1)], "start": 0, "truncated": True}
            key = {10: 1, 11: 5}[handle]
            parts = [
                {"name": "first", "type": "const int", "value": str(key), "view": "memory"},
                {"name": "second", "type": "int", "value": str(key * 2), "view": "memory"},
            ]
            return {"variables": parts, "start": 0, "truncated": False}

    variable = {
        "name": "m",
        "type": "std::unordered_map<int, int>",
        "value": "size=2 {1: 2, 5: 10}",
        "variablesReference": 1,
        "view": "memory",
    }
    how = "select an entry by position with m[#N]"
    first_page = lambda handle: {"variables": [entry(0, 1)], "truncated": True}  # noqa: E731
    # Protocol 9's answer: its first page, with no key.
    old = Worker({"variables": [entry(0, 1)], "start": 0, "truncated": True})
    with pytest.raises(ValueError, match="doesn't say it compared every key with 5"):
        _value_for_key(old, {}, variable, "m", "[5]", first_page, how)
    # An answer for the key whose entry has another key.
    wrong = Worker({"variables": [entry(0, 1)], "key": "5", "compared": 2, "keys": ["1", "5"]})
    with pytest.raises(ValueError, match="has the key 1, not 5"):
        _value_for_key(wrong, {}, variable, "m", "[5]", first_page, how)
    right = Worker({"variables": [entry(1, 5)], "key": "5", "compared": 2, "keys": ["1", "5"]})
    assert _value_for_key(right, {}, variable, "m", "[5]", first_page, how)["value"] == "10"


@pytest.mark.parametrize("recorded", [None, 9])
def test_a_local_session_never_sends_a_worker_of_another_protocol_anything_it_runs(
    tmp_path, monkeypatch, capsys, recorded
):
    # #374 review 2: the protocol check read only the answer, after a local worker of
    # protocol 9 had run `continue`, `step`, `set_variable` or `close`. A local session
    # keeps its worker's protocol; a record that names another, or none, is checked with
    # `version` first, and nothing else is sent.
    import json
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from peeky.cli import _record, main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    asked = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            asked.append(self.path)
            answer = json.dumps({"protocol": 9, "state": "stopped", "stop_id": "s"}).encode()
            self.send_response(200)
            self.send_header("X-Peeky-Protocol", "9")
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        commands = (
            ["continue"],
            ["step"],
            ["set", "x=1"],
            ["get", "x"],
            ["inspect"],
            ["close"],
        )
        for command in commands:
            path = _record("old")
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "name": "old",
                "url": f"http://127.0.0.1:{server.server_port}",
                "token": "t" * 32,
                "pid": os.getpid(),
                "session_id": "0" * 32,
                "stop_id": "s",
                "state": "stopped",
                **({"protocol": recorded} if recorded else {}),
            }
            path.write_text(json.dumps(record))
            os.chmod(path, 0o600)
            assert main(["session", *command, "--name", "old", "--json"]) == 2, command
            message = json.loads(capsys.readouterr().out)["error"]["message"]
            assert message.startswith("Worker speaks protocol 9, this session 10: another "), (
                message
            )
            assert "peeky session close --forget --name old" in message, message
    finally:
        server.shutdown()
        server.server_close()
    # Its `version` alone was asked, which changes nothing.
    assert asked == ["/version"] * len(commands), asked
