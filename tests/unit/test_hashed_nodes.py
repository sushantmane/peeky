"""A libstdc++ unordered container's entries, read from its own nodes (#360): the debug
information must prove where a node holds its value, the chain must end after exactly
the container's own count, and the entries must be of types Peeky reads there."""

import struct

import pytest

from peeky.hashed_nodes import (
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
    # More entries than Peeky reads: not even its chain.
    with pytest.raises(Unread):
        read_entries(types, types[6], MAX_NODES + 1, Memory(first, reader(held)))


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


def test_only_integers_bools_strings_pairs_and_structs_are_read():
    for kind in (13, 14):
        types = graph(value=kind, value_size=8 if kind == 13 else 4)
        held, first = nodes(1, size=8 if kind == 13 else 4)
        if kind == 13:
            with pytest.raises(Unread):
                read_entries(types, types[6], 1, Memory(first, reader(held)))
        else:
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
    assert memory.at(0x1008, 4) == (9).to_bytes(4, "little")
    with pytest.raises(Unread):
        memory.at(0x5000, 8)
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


def test_a_strings_text_is_read_from_its_own_buffer_or_where_it_points():
    types = graph(value=35, value_size=32, extra=TEXTS)
    heap = 0x9000
    second = struct.pack("<QQQ", 0, heap, 20) + b"\0" * 16
    first = struct.pack("<QQQ", 0x2000, 0x1000 + 24, 2) + b"al" + b"\0" * 14
    held = {0x1000: first + b"\0" * 24, 0x2000: second + b"\0" * 24, heap: b"y" * 20 + b"\0" * 12}
    root, fetch = read_entries(types, types[6], 2, Memory(0x1000, reader(held)))
    entries = fetch(root["variablesReference"], 8)
    assert [entry["value"] for entry in entries] == ['"al"', '"' + "y" * 20 + '"']
    assert all(entry["text"] and entry["type"] == STRING for entry in entries)
    # Without `readMemory`, only what is inside the nodes: the second's text is not.
    texts = {}
    path = "s._M_h._M_before_begin._M_nxt"
    texts[path] = "0x0000000000001000"
    for address in (0x1000, 0x2000):
        data = held[address]
        texts[path + "->_M_nxt"] = f"0x{struct.unpack_from('<Q', data)[0]:016x}"
        for k in range(1, 5):
            texts[f"{path}[{k}]._M_nxt"] = f"0x{struct.unpack_from('<Q', data, 8 * k)[0]:016x}"
        path += "->_M_nxt"
    with pytest.raises(Unread):
        read_entries(types, types[6], 2, Words("s._M_h._M_before_begin._M_nxt", texts.get))
    root, fetch = read_entries(
        types,
        types[6],
        1,
        Words(
            "s._M_h._M_before_begin._M_nxt",
            {**texts, "s._M_h._M_before_begin._M_nxt->_M_nxt": "nullptr"}.get,
        ),
    )
    assert fetch(root["variablesReference"], 8)[0]["value"] == '"al"'
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
    # A floating-point number, a wide character, a struct with a base class: never read.
    assert not readable(types, 13) and not readable(types, 45) and not readable(types, 44)
    # As read_entries finds them, value by value.
    data = struct.pack("<QiiiIq", 0, 3, 0, 5, 0, -6)
    paired = graph(value=42, value_size=24, extra=point)
    read_entries(paired, paired[6], 1, Memory(0x1000, reader({0x1000: data + b"\0" * 8})))
    based = graph(value=44, value_size=16, extra=point)
    with pytest.raises(Unread):
        read_entries(based, based[6], 1, Memory(0x1000, reader({0x1000: data})))
