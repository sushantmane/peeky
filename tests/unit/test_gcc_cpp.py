"""C++ built by GCC (#167): what LLDB misreads in its debug information is withheld or
said, never shown as the program's. Unordered containers LLDB counts as empty, a
structured binding of a bitfield with no location, and LLDB's own errors about the
debug information, which lldb-dap 18 sends among the program's output."""

import collections
import re
import threading
from types import SimpleNamespace

import pytest

from peeky.dap import DAP
from peeky.errors import classify
from peeky.native import (
    PROVIDER_RULES,
    UNLISTED,
    NativeSession,
    count_path,
    formatter_count,
    hashed_value,
    lldb_major,
    local_buffer,
    provider_type,
    raw_count,
    undecoded_reason,
    unlisted_reason,
    unordered_proof,
)
from peeky.native_previews import Typed
from peeky.probe import _resolve, debugger_diagnostics, parse_selector

HASHTABLE = "(std::_Hashtable<int, int, ...>::size_type) _M_element_count = {}"


def native(typedefs=None, optimization="O0", function=None, fields=None):
    session = NativeSession.__new__(NativeSession)
    session.target = SimpleNamespace(language="cpp", optimization=optimization)
    session.typedefs = dict(typedefs or {})
    session.fields = fields
    session._function_at = lambda frame: function
    return session


class Counts:
    """lldb-dap's `evaluate` of a variable path: {path: its text}, the paths asked."""

    def __init__(self, counts):
        self.counts, self.asked = counts, []

    def __call__(self, path):
        self.asked.append(path)
        if path not in self.counts:
            raise ValueError(f"The debugger could not read {path}: invalid value object")
        return self.counts[path]


def variable(name, kind, value, reference=7):
    return {"name": name, "type": kind, "value": value, "variablesReference": reference}


@pytest.mark.parametrize(
    "steps, path",
    [
        ([("us", "std::unordered_set<int>")], "us._M_h._M_element_count"),
        ([("h", "Holder"), ("us", "std::unordered_set<int>")], "h.us._M_h._M_element_count"),
        ([("vs", "std::vector<S>"), ("[1]", "S")], "vs[1]._M_h._M_element_count"),
        (
            [("mus", "std::map<int, S>"), ("[0]", "pair"), ("second", "S")],
            "mus[0].second._M_h._M_element_count",
        ),
        # A pointer to one: the debugger lists the container's entries as its own.
        ([("p", "std::unordered_set<int> *")], "p->_M_h._M_element_count"),
        ([("ref", "const std::unordered_map<int, int> &")], "ref._M_h._M_element_count"),
        ([("this", "Holder *"), ("us", "S")], "this->us._M_h._M_element_count"),
        # An element of what a pointer points at is `[0][N]`: `pv[1]` is the next vector.
        ([("pv", "std::vector<S> *"), ("[1]", "S")], "pv[0][1]._M_h._M_element_count"),
        # Names no path holds: located, a pointee row, anonymous, a base class.
        ([("t @ a.cpp:3", "S")], None),
        ([("pp", "S **"), ("*pp", "S *")], None),
        ([("h", "Holder"), ("", "union"), ("us", "S")], None),
        ([("d", "Derived"), ("Base", "Base"), ("us", "S")], None),
        ([], None),
    ],
)
def test_the_path_a_containers_own_count_is_read_by(steps, path):
    assert count_path(steps) == path


def test_counts_as_the_debugger_writes_them():
    assert formatter_count("size=3") == 3 and formatter_count("0x0000fffffffff938 size=0") == 0
    assert formatter_count("size=3 {1, 2, 3}") is None and formatter_count("0x0000") is None
    assert raw_count("1") == 1 and raw_count(HASHTABLE.format(12)) == 12
    assert raw_count("<error>") is None and raw_count("-1") is None and raw_count(None) is None


def test_an_unordered_container_the_debugger_miscounts_is_withheld():
    # #167: GCC's `std::unordered_set<int> us{4}` read `size=0 {}`, `um[1]` "would
    # insert the key", `us[#0]` "us has 0 entries".
    session = native()
    counts = Counts({"us._M_h._M_element_count": HASHTABLE.format(1)})
    shown = variable("us", "std::unordered_set<int>", "size=0")
    why = session.unlisted_container(shown, {"id": 1}, [("us", shown["type"])], counts)
    assert why.label == UNLISTED == "not listed"
    assert str(why) == (
        "the debugger cannot list this build's unordered containers: it counts 0 entries, "
        "but the container's own count (`_M_element_count`) is 1; print its entries from "
        "the program"
    )
    assert counts.asked == ["us._M_h._M_element_count"]
    # Where the counts agree on none, nothing is listed: it stands.
    agreeing = Counts({"us._M_h._M_element_count": "0"})
    assert session.unlisted_container(shown, {"id": 1}, [("us", shown["type"])], agreeing) is None
    # Where they agree on some, the entries are listed only where they are proven to
    # decode as they are (see the provider's rules below).
    listed = variable("us", "std::unordered_set<int>", "size=1")
    one = Counts({"us._M_h._M_element_count": "1"})
    session._unproven_entries = lambda *_: None
    assert session.unlisted_container(listed, {"id": 1}, [("us", listed["type"])], one) is None
    session._unproven_entries = lambda *_: "type"
    why = session.unlisted_container(listed, {"id": 1}, [("us", listed["type"])], one)
    assert str(why) == undecoded_reason("type") and why.label == UNLISTED


@pytest.mark.parametrize(
    "kind, value, path",
    [
        ("std::unordered_map<int, int>", "size=0", "um._M_h._M_element_count"),
        ("std::unordered_multiset<int>", "size=0", "um._M_h._M_element_count"),
        ("std::unordered_multimap<int, int>", "size=0", "um._M_h._M_element_count"),
        ("const std::unordered_map<int, int> &", "0x0000ff00 size=0", "um._M_h._M_element_count"),
        ("std::unordered_set<int> *", "0x0000ff00 size=0", "um->_M_h._M_element_count"),
        ("std::pmr::unordered_set<int>", "size=0", "um._M_h._M_element_count"),
        # By a typedef's name, which also hides the element type from LLDB's formatter.
        ("Ids", "size=0", "um._M_h._M_element_count"),
        ("const Ids &", "0x0000ff00 size=0", "um._M_h._M_element_count"),
        ("IdsPtr", "0x0000ff00 size=0", "um->_M_h._M_element_count"),
    ],
)
def test_each_kind_of_unordered_container_is_checked(kind, value, path):
    session = native(
        typedefs={
            "Ids": ("other", "std::unordered_set<long, std::hash<long> >"),
            "IdsPtr": ("pointer", "std::unordered_set<long>"),
        }
    )
    counts = Counts({path: "2"})
    shown = variable("um", kind, value)
    why = session.unlisted_container(shown, {"id": 1}, [("um", kind)], counts)
    assert counts.asked == [path] and "is 2;" in why
    assert why.startswith("it points at an unordered container") == ("->" in path)


def test_a_count_peeky_cannot_read_withholds_and_nothing_else_is_asked():
    session = native()
    kind = "std::unordered_set<int>"
    for steps in ([("us", kind)], [("t @ a.cpp:3", kind)]):
        why = session.unlisted_container(
            variable(steps[0][0], kind, "size=0"), {"id": 1}, steps, Counts({})
        )
        assert why.label == UNLISTED and len(why) <= 300
        assert "could not read this one's own count (`_M_element_count`)" in why
    # An optimized build where the function declares the name twice: a path may read
    # the other one's count.
    function = {"types": {"us": {(1, "automatic", None), (2, "automatic", None)}}}
    twice = native(optimization="O2", function=function)
    counts = Counts({"us._M_h._M_element_count": "0"})
    why = twice.unlisted_container(
        variable("us", kind, "size=0"), {"id": 1}, [("us", kind)], counts
    )
    assert why is not None and counts.asked == []
    # Not asked: other types, a libc++ build, a null pointer, a value with no value.
    quiet = Counts({})
    others = [
        (native(), variable("m", "std::map<int, int>", "size=0")),
        (native(), variable("v", "std::vector<int>", "size=0")),
        (native(typedefs={"namespace::__1": ("other", "")}), variable("us", kind, "size=0")),
        (native(), variable("p", kind + " *", "0x0000000000000000", 0)),
        (native(), variable("us", kind, "<error: no location>")),
    ]
    rust = native()
    rust.target = SimpleNamespace(language="rust", optimization="O0")
    others.append((rust, variable("us", kind, "size=0")))
    for session, shown in others:
        assert session.unlisted_container(shown, {"id": 1}, [("x", shown["type"])], quiet) is None
    assert quiet.asked == []


def test_every_reason_fits_a_row_and_says_what_to_do():
    for shown, raw, pointer in ((0, 1, False), (0, 99999999, True), (None, None, True)):
        reason = unlisted_reason(shown, raw, pointer)
        assert len(reason) <= 300, reason
        assert reason.endswith(("print it from the program", "print its entries from the program"))


class Lldb:
    def __init__(self, tree):
        self.tree = tree

    def dispatch(self, operation, args):
        children = self.tree[args["handle"]]
        return {"variables": children, "truncated": False, "remaining_nodes": 500}


@pytest.mark.parametrize("selector", ["um[1]", "um[#0]", "um['a']", "p[0]", "p[0][1]"])
def test_no_entry_of_an_unlisted_container_is_read(selector):
    reason = unlisted_reason(0, 3)
    withheld = {
        "value": None,
        "variablesReference": 0,
        "unavailable": True,
        "reason": reason,
        "withheld": True,
        "withheld_as": UNLISTED,
    }
    scopes = {
        "scopes": [
            {
                "name": "Locals",
                "variables": [
                    {**withheld, "name": "um", "type": "std::unordered_map<int, int>"},
                    {**withheld, "name": "p", "type": "std::unordered_map<int, int> *"},
                ],
            }
        ]
    }
    with pytest.raises(ValueError) as caught:
        _resolve(Lldb({}), {}, scopes, parse_selector(selector), True)
    root, segment = selector[: selector.index("[")], selector[selector.index("[") :]
    segment = segment[: segment.index("]") + 1]
    assert str(caught.value) == f"{segment} not read: {root} is not listed: {reason}"
    # Never "um is empty; in C++, um[1] would insert the key".
    assert "insert" not in str(caught.value) and "0 entries:" not in str(caught.value)


def string_graph(buffer=(16,), offsets=(0, 8, 16), element="char", size=32, union=16, capacity=3):
    """fields.type_graph of libstdc++'s `std::string` as GCC describes it."""
    record = ("record", "basic_string<char>", "class", size, None)
    members = (
        ("_M_dataplus", 2, offsets[0], False, None, None),
        ("_M_string_length", 3, offsets[1], False, None, None),
        (None, 4, offsets[2], False, None, None),
    )
    inner = (
        ("_M_local_buf", 5, None, False, None, None),
        ("_M_allocated_capacity", capacity, None, False, None, None),
    )
    hider = (("_M_p", 7, 0, False, None, None),)
    return {
        1: (*record, members, (2, 3, 4), False, False, "std"),
        2: ("record", "_Alloc_hider", "struct", 8, None, hider, (7,), False, False, "std"),
        3: ("base", "long unsigned int", "DW_ATE_unsigned", 8),
        4: ("record", "", "union", union, None, inner, (5, 3), False, False, "std"),
        5: ("array", "", 6, buffer, False),
        6: ("base", element, "DW_ATE_signed_char", 1),
        7: ("pointer", "", 6),
        8: ("base", "unsigned int", "DW_ATE_unsigned", 4),
    }


def test_a_libstdcxx_strings_own_buffer_is_sized_by_the_debug_information():
    # LLDB reads GCC's `_M_local_buf` (`char[16]`, upper bound 15) as `char[0]`.
    graph = string_graph()
    assert local_buffer(graph, graph[1]) == 16
    for other in (
        string_graph(offsets=(0, 8, 24)),
        string_graph(offsets=(8, 0, 16)),
        string_graph(buffer=(4, 4)),
        string_graph(buffer=None),
        string_graph(element="wchar_t"),
        # Review of #281: sizes that do not add up are never trusted. A 64-byte buffer in
        # a 16-byte union of a 32-byte string would let a 40-byte text past the object.
        string_graph(buffer=(64,)),
        string_graph(buffer=(17,)),
        string_graph(union=24),
        string_graph(union=None),
        string_graph(size=None),
        string_graph(capacity=8),
        string_graph(capacity=99),
    ):
        assert local_buffer(other, other[1]) is None
    # A shorter buffer that fits is its own length.
    assert local_buffer(string_graph(buffer=(8,)), string_graph()[1]) == 8


def record(name, members=(), parameters=(), size=8, kind="class", holds=None):
    """A fields.type_graph record: members as (name, type offset, location)."""
    rows = tuple((m[0], m[1], m[2], False, None, None) for m in members)
    shown = tuple(("type", p[0], p[1], None) for p in parameters)
    held = tuple(m[1] for m in members) if holds is None else holds
    return ("record", name, kind, size, None, rows, held, False, False, "std", (), name, shown)


def hashed(name, value, allocator, parameters, holder=30):
    """A libstdc++ unordered container holding ``value``: its record, and its hashtable's."""
    table = record(
        f"std::_Hashtable<{name}>",
        [("_M_buckets", 21, 0), ("_M_before_begin", 20, 16), ("_M_element_count", 22, 24)],
        [("_Key", value), ("_Value", value), ("_Alloc", allocator)],
        size=56,
    )
    return {holder: table, holder + 1: record(name, [("_M_h", holder, 0)], parameters, 56)}


SET = "std::unordered_set<int, std::hash<int>, std::equal_to<int>, std::allocator<int> >"
WEIRD = "std::unordered_set<int, std::hash<int>, std::equal_to<int>, WeirdAlloc<double, int> >"
ASSETS = (
    "std::unordered_map<int, Asset, std::hash<int>, std::equal_to<int>, "
    "std::allocator<std::pair<const int, Asset> > >"
)
GRAPH = {
    1: ("base", "int", "DW_ATE_signed", 4),
    2: ("base", "double", "DW_ATE_float", 8),
    3: ("base", "long double", "DW_ATE_float", 16),
    10: record("std::allocator<int>", parameters=[("_Tp", 1)], size=1),
    # GCC's: no template arguments.
    11: record("std::allocator<int>", size=1),
    12: record("WeirdAlloc<double, int>", parameters=[("T", 2), ("V", 1)], size=1),
    13: record("std::allocator<long double>", parameters=[("_Tp", 3)], size=1),
    14: record("std::allocator<std::pair<const int, Asset> >", parameters=[("_Tp", 60)], size=1),
    20: record("std::__detail::_Hash_node_base", [("_M_nxt", 21, 0)]),
    21: ("pointer", "", None),
    22: ("base", "long unsigned int", "DW_ATE_unsigned", 8),
    50: record("std::hash<int>", parameters=[("_Tp", 1)], size=1),
    51: record("std::equal_to<int>", parameters=[("_Tp", 1)], size=1),
    60: record("std::pair<const int, Asset>", [("first", 61, 0), ("second", 70, 4)]),
    61: ("qualifier", "const", 1),
    70: record("Asset", [("v", 1, 0)], size=4),
    **hashed(SET, 1, 10, [("_Value", 1), ("_Hash", 50), ("_Pred", 51), ("_Alloc", 10)], 30),
    **hashed(SET, 1, 11, [("_Value", 1), ("_Hash", 50), ("_Pred", 51), ("_Alloc", 11)], 32),
    **hashed(WEIRD, 1, 12, [("_Value", 1), ("_Hash", 50), ("_Pred", 51), ("_Alloc", 12)], 34),
    **hashed(
        "std::unordered_set<long double>",
        3,
        13,
        [("_Value", 3), ("_Hash", 50), ("_Pred", 51), ("_Alloc", 13)],
        36,
    ),
    **hashed(
        ASSETS,
        60,
        14,
        [("_Key", 1), ("_Tp", 70), ("_Hash", 50), ("_Pred", 51), ("_Alloc", 14)],
        38,
    ),
}


@pytest.mark.parametrize(
    "container, rules, names, proven",
    [
        # A standard set: its allocator's first argument is the value type, by either rule.
        (31, ("fixed", "last"), [SET], None),
        # GCC's `std::allocator<int>` has no template arguments: nothing proves it.
        (33, ("fixed",), [SET], "unknown"),
        (33, ("last",), [SET], "unknown"),
        # Review of #281: `WeirdAlloc<double, int>` decodes an `int` as a `double`.
        (35, ("last",), [WEIRD], "type"),
        (35, ("fixed",), [WEIRD], "type"),
        # A `long double` (16-byte aligned) is past the 8 bytes LLDB skips in each node.
        (37, ("last",), ["std::unordered_set<long double>"], "place"),
        # A map whose name holds `set`: LLDB 18 takes it for a set (the 4th argument is
        # `std::equal_to<int>`); LLDB 20 takes the last.
        (39, ("fixed",), ["std::unordered_map<int, Asset>", ASSETS], "type"),
        (39, ("last",), ["std::unordered_map<int, Asset>", ASSETS], None),
        (39, ("fixed", "last"), [ASSETS], "type"),
        # Names that disagree on `set` (a typedef's, `Ids`): LLDB 18's choice is not known.
        (31, ("fixed",), ["Ids", SET], "unknown"),
        (31, ("last",), ["Ids", SET], None),
    ],
)
def test_entries_are_listed_only_where_lldbs_provider_decodes_them_as_they_are(
    container, rules, names, proven
):
    assert unordered_proof(GRAPH, GRAPH[container], rules, names) == proven


def test_the_provider_rules_and_the_hashtables_value():
    assert provider_type(GRAPH, GRAPH[31], "fixed", [SET]) == 1
    assert provider_type(GRAPH, GRAPH[35], "last", [WEIRD]) == 2
    assert provider_type(GRAPH, GRAPH[39], "last", [ASSETS]) == 60
    assert hashed_value(GRAPH, GRAPH[39]) == (60, 8) and hashed_value(GRAPH, GRAPH[37]) == (3, 8)
    # Without a hashtable, a `_Value`, or `_Hash_node_base`'s size: none.
    assert hashed_value(GRAPH, record("x", [("_M_h", 70, 0)])) is None
    assert hashed_value(GRAPH, GRAPH[70]) is None
    assert PROVIDER_RULES[18] == ("fixed",) and PROVIDER_RULES[20] == ("last",)
    for reason in ("type", "place", "unknown"):
        assert len(undecoded_reason(reason, pointer=True)) <= 300
        assert undecoded_reason(reason).endswith("print its entries from the program")


LONG = "std::unordered_set<long double>"


@pytest.mark.parametrize(
    "base, pointer, container, proven",
    [
        # Review round 2 of #281: an 8-byte `_M_nxt` in a 16-byte `_Hash_node_base`, with a
        # 16-aligned value: the value sits 16 bytes in, where LLDB reads 8 in. Proven by
        # `_Hash_node_base`'s size alone, it was listed.
        (
            record("_Hash_node_base", [("_M_nxt", 21, 0)], size=16),
            ("pointer", "", None, 8),
            37,
            None,
        ),
        (record("_Hash_node_base", [("_M_nxt", 21, 0)], size=16), ("pointer", "", None), 31, None),
        # `_M_nxt` elsewhere, of another type, or missing: not proven.
        (
            record("_Hash_node_base", [("_M_nxt", 21, 8)], size=16),
            ("pointer", "", None, 8),
            31,
            None,
        ),
        (record("_Hash_node_base", [("_M_nxt", 22, 0)]), ("pointer", "", None, 8), 31, None),
        (record("_Hash_node_base", [("_M_next", 21, 0)]), ("pointer", "", None, 8), 31, None),
        # A pointer's own size, where the debug information gives one: a 4-byte pointer
        # skips 4 bytes, past an `int`, short of a `double`'s 8-byte alignment.
        (record("_Hash_node_base", [("_M_nxt", 21, 0)], size=4), ("pointer", "", None, 4), 31, 4),
        (
            record("_Hash_node_base", [("_M_nxt", 21, 0)], size=8),
            ("pointer", "", None, 4),
            31,
            None,
        ),
        (record("_Hash_node_base", [("_M_nxt", 21, 0)]), ("pointer", "", None, 8), 31, 8),
    ],
)
def test_an_entry_is_where_lldb_reads_only_past_one_pointer(base, pointer, container, proven):
    graph = {**GRAPH, 20: base, 21: pointer}
    held = hashed_value(graph, graph[container])
    assert (held[1] if held else None) == proven
    if proven is None:
        assert unordered_proof(graph, graph[container], ("last",), [LONG, SET]) == "unknown"


def test_a_value_aligned_past_the_pointer_lldb_skips_is_in_the_wrong_place():
    four = {
        **GRAPH,
        20: record("_Hash_node_base", [("_M_nxt", 21, 0)], size=4),
        21: ("pointer", "", None, 4),
    }
    assert unordered_proof(four, four[31], ("last",), [SET]) is None
    doubles = {
        **four,
        **hashed("std::unordered_set<double>", 2, 15, [("_Value", 2), ("_Alloc", 15)], 40),
        15: record("std::allocator<double>", parameters=[("_Tp", 2)], size=1),
    }
    assert unordered_proof(doubles, doubles[41], ("last",), ["x"]) == "place"
    assert unordered_proof(GRAPH, GRAPH[37], ("last",), [LONG]) == "place"


class Versions:
    def __init__(self, reply):
        self.reply, self.asked = reply, []

    def request(self, command, arguments, timeout=None):
        self.asked.append((command, arguments))
        if isinstance(self.reply, Exception):
            raise self.reply
        return {"result": self.reply}


def test_lldbs_version_picks_the_rule_and_unknown_needs_both():
    assert lldb_major(Versions("(lldb) version\nlldb version 18.1.3\n")) == 18
    asked = Versions("(lldb) version\nlldb version 20.1.2\n")
    assert lldb_major(asked) == 20
    assert asked.asked == [("evaluate", {"expression": "`version", "context": "repl"})]
    assert lldb_major(Versions("lldb-1700.0.9.502\nApple Swift version 6.1")) is None
    assert lldb_major(Versions(TimeoutError("no answer"))) is None
    # The row's container record, by its way from its root local; each version's rule.
    function = {"types": {"m": {(39, "automatic", None)}}, "referred": frozenset()}
    session = native(function=function, fields=GRAPH)
    session._shown_where = lambda name, pc: None
    session._block_pc = lambda frame: None
    shown = variable("m", "std::unordered_map<int, Asset>", "size=1")
    steps = [("m", shown["type"])]
    for major, proven in ((18, "type"), (19, "type"), (20, None), (21, None), (None, "type")):
        session.lldb_major = major
        assert session._unproven_entries(shown, {"id": 1}, steps) == proven, major
    # Through a pointer to it: what it points at.
    function["types"]["p"] = {(80, "automatic", None)}
    session.fields = {**GRAPH, 80: ("pointer", "", 39)}
    session.lldb_major = 20
    assert session._unproven_entries(shown, {"id": 1}, [("p", "Assets *")]) is None
    # A row Peeky cannot place in the debug information: not proven.
    assert session._unproven_entries(shown, {"id": 1}, [("q", "Assets")]) == "unknown"
    assert Typed(GRAPH, 39).node()[11] == ASSETS


def test_a_bitfields_binding_with_no_location_says_why_no_stop_reads_it():
    # GCC: `auto [lo, hi] = bits` is `hi`, no DW_AT_location, type `__unknown__`.
    fields = {7: ("base", "__unknown__", "DW_ATE_unsigned", 1), 8: ("base", "int", "x", 4)}
    function = {"types": {"hi": {(7, "other", ("b.cpp", 12))}, "n": {(8, "other", None)}}}
    session = native(function=function, fields=fields)
    shown = {"name": "hi", "type": "unsigned char", "value": "<error: no location, value may>"}
    why = session.unlocated(shown, {"id": 1})
    assert why == (
        "the build gives it no location and no type of its own (`__unknown__`), as GCC does "
        "for a structured binding of a bitfield, so the debugger can neither read nor change "
        "it; print it from the program"
    )
    error = classify(ValueError(f"'hi' cannot be changed: {why}"))
    assert error["code"] == "MUTATION_REJECTED" and "stop after" not in error["hint"]
    assert error["hint"].startswith("Print it from the program") and len(error["hint"]) <= 150
    # Any other value the debugger has none for keeps its reason.
    assert session.unlocated({**shown, "value": "7"}, {"id": 1}) is None
    assert session.unlocated({**shown, "name": "n"}, {"id": 1}) is None
    assert native(fields=fields).unlocated(shown, {"id": 1}) is None


def transport(diagnostics=NativeSession.debugger_diagnostics):
    dap = DAP.__new__(DAP)
    dap.condition = threading.Condition()
    dap.events = collections.deque(maxlen=64)
    dap.ignored_events = {}
    dap.output = collections.deque(maxlen=500)
    dap.output_truncated = False
    dap.output_count = 0
    dap.event_sequence = 0
    dap.diagnostics = __import__("re").compile(diagnostics) if diagnostics else None
    dap.debugger_diagnostics, dap.diagnostic_count = [], 0
    return dap


DWARF = (
    "error: program 0x000000fc: DW_TAG_member '_M_local_buf' refers to type "
    "0x0000000000004eb1 which extends beyond the bounds of 0x000000f3\n"
)


def output(dap, category, text):
    dap._accept(
        {"type": "event", "event": "output", "body": {"category": category, "output": text}}
    )


def test_lldbs_errors_about_the_debug_information_are_no_program_output():
    # lldb-dap 18 sends LLDB's own stderr as `stderr`; the program's streams come through
    # its terminal as `stdout`.
    dap = transport()
    output(dap, "stderr", DWARF)
    output(dap, "stdout", "NM:[named]\r\n")
    assert dap.snapshot_output() == ("NM:[named]\r\n", False)
    assert dap.snapshot_diagnostics() == ([DWARF.rstrip("\n")], 1)
    # The program printing the same line comes through its terminal: it stays output.
    output(dap, "stdout", DWARF.replace("\n", "\r\n"))
    # Anything else on the debugger's stderr, or a line among others, stays too.
    output(dap, "stderr", "free(): invalid pointer\n")
    output(dap, "stderr", DWARF + "something else\n")
    output(dap, "stderr", DWARF.rstrip("\n"))
    output(dap, "stderr", "warning: program 0x000000fc: DW_TAG_member 'x'\n")
    kept, _ = dap.snapshot_output()
    assert kept.count("DW_TAG_member") == 4 and dap.diagnostic_count == 1
    # Another adapter's (no pattern): its stderr is the program's.
    plain = transport(None)
    output(plain, "stderr", DWARF)
    assert plain.snapshot_output() == (DWARF, False) and plain.snapshot_diagnostics()[1] == 0
    # Bounded: the first 20 lines kept, all counted.
    many = transport()
    for _ in range(30):
        output(many, "stderr", DWARF + DWARF)
    lines, count = many.snapshot_diagnostics()
    assert len(lines) == 20 and count == 60


def test_a_probe_warns_of_the_debuggers_lines_it_left_out():
    result = {}
    final = {"debugger_diagnostics": [DWARF.rstrip("\n")], "diagnostic_count": 3}
    warning = debugger_diagnostics(result, None, final)
    # Said neutrally: a program line of exactly this shape on `stderr` would be moved too.
    assert warning == (
        "3 `stderr` lines with the shape of LLDB's errors about the build's debug information "
        "were moved here from the program's output (debugger_diagnostics in --json); a "
        "program line that looks exactly like one would be moved too: " + DWARF.rstrip("\n")
    )
    one = debugger_diagnostics({}, {**final, "diagnostic_count": 1})
    assert (
        one.startswith("A `stderr` line with the shape of LLDB's errors") and " was moved " in one
    )
    assert result == {"debugger_diagnostics": [DWARF.rstrip("\n")]}
    assert debugger_diagnostics({}, None, {"diagnostic_count": 0}) is None


class Evaluating:
    """lldb-dap's `evaluate`: {path: result}; the requests made."""

    def __init__(self, results):
        self.results, self.requests = results, []

    def request(self, command, arguments, timeout=None):
        from peeky.dap import RequestFailed

        self.requests.append((command, arguments))
        if arguments["expression"] not in self.results:
            raise RequestFailed("invalid value object")
        return {"result": self.results[arguments["expression"]]}


def test_a_withheld_container_row_has_no_value_handle_or_entries(tmp_path):
    from peeky.worker import DebugWorker

    runtime = SimpleNamespace(
        assert_current=lambda: None,
        reports_unavailable_as_text=True,
        unlisted_container=native().unlisted_container,
    )
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.state, worker.stop_id = runtime, "stopped", "stop1"
    worker.listed_frames = [{"id": 1, "name": "main"}]
    worker.dap = Evaluating({"us._M_h._M_element_count": "1", "s._M_h._M_element_count": "0"})
    worker.reset_views()
    rows = [
        variable("us", "std::unordered_set<int>", "size=0", 4),
        variable("s", "std::unordered_set<int>", "size=0", 5),
        variable("v", "std::vector<int>", "size=0", 6),
    ]
    us, s, v = worker.expose_variables(rows, {"id": 1}, ("Locals", None), ())
    assert us["unavailable"] and us["value"] is None and us["variablesReference"] == 0
    assert us["withheld"] and us["withheld_as"] == "not listed" and us["settable"] is False
    assert "it counts 0 entries, but the container's own count" in us["reason"]
    assert s["value"] == "size=0" and s["variablesReference"] and not s.get("unavailable")
    assert v["value"] == "size=0" and not v.get("unavailable")
    # A hover: lldb-dap reads the path, never runs it as an expression in the program.
    assert worker.dap.requests == [
        ("evaluate", {"expression": path, "frameId": 1, "context": "hover"})
        for path in ("us._M_h._M_element_count", "s._M_h._M_element_count")
    ]
    # Read once per stop, and again at the next.
    worker.expose_variables(rows[:1], {"id": 1}, ("Locals", None), ())
    assert len(worker.dap.requests) == 2
    worker.stop_id = "stop2"
    worker.expose_variables(rows[:1], {"id": 1}, ("Locals", None), ())
    assert len(worker.dap.requests) == 3
    # A member reached through a struct, by its path; one the debugger cannot read: withheld.
    worker.dap = Evaluating({})
    (member,) = worker.expose_variables(
        [variable("us", "std::unordered_set<int>", "size=2", 8)],
        {"id": 1},
        ("Locals", None),
        (("h", "Holder"),),
    )
    assert worker.dap.requests[0][1]["expression"] == "h.us._M_h._M_element_count"
    assert (
        member["withheld_as"] == "not listed"
        and "could not read this one's own" in member["reason"]
    )


# #291: GCC's `auto [t, u] = pair;` names each element by a variable holding a reference
# to `std::tuple_element<I, E>::type`; its row was the element's address.
ELEMENT = "std::tuple_element<0, std::pair<long, long> >::type"
POINT = "std::tuple_element<0, std::pair<Pt, int> >::type"
BOUND = """0x0000000b: DW_TAG_compile_unit
              DW_AT_name\t("t.cpp")

0x00000010:   DW_TAG_base_type
                DW_AT_name\t("long int")
                DW_AT_encoding\t(DW_ATE_signed)
                DW_AT_byte_size\t(0x08)

0x00000014:   DW_TAG_structure_type
                DW_AT_name\t("Pt")
                DW_AT_byte_size\t(0x10)

0x00000018:     DW_TAG_member
                  DW_AT_name\t("x")
                  DW_AT_type\t(0x00000010 "long int")
                  DW_AT_data_member_location\t(0x00)

0x00000020:   DW_TAG_namespace
                DW_AT_name\t("std")

0x00000024:     DW_TAG_structure_type
                  DW_AT_name\t("tuple_element<0, std::pair<long int, long int> >")

0x00000028:       DW_TAG_typedef
                    DW_AT_name\t("type")
                    DW_AT_type\t(0x00000010 "long int")

0x0000002c:     DW_TAG_structure_type
                  DW_AT_name\t("tuple_element<0, std::pair<Pt, int> >")

0x00000030:       DW_TAG_typedef
                    DW_AT_name\t("type")
                    DW_AT_type\t(0x00000014 "Pt")

0x00000040:   DW_TAG_rvalue_reference_type
                DW_AT_type\t(0x00000028 "type")

0x00000044:   DW_TAG_reference_type
                DW_AT_type\t(0x00000030 "type")

0x00000048:   DW_TAG_reference_type
                DW_AT_type\t(0x00000010 "long int")

0x0000004c:   DW_TAG_reference_type
                DW_AT_type\t(0x00000028 "type")

0x00000050:   DW_TAG_subprogram
                DW_AT_name\t("main")

0x00000054:     DW_TAG_variable
                  DW_AT_name\t("held")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(3)
                  DW_AT_type\t(0x00000014 "Pt")
                  DW_AT_location\t(DW_OP_fbreg -84)

0x00000058:     DW_TAG_variable
                  DW_AT_name\t("p")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(4)
                  DW_AT_type\t(0x00000014 "Pt")
                  DW_AT_location\t(DW_OP_fbreg -88)

0x0000005c:     DW_TAG_variable
                  DW_AT_name\t("x")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(5)
                  DW_AT_type\t(0x00000010 "long int")
                  DW_AT_location\t(DW_OP_fbreg -92)

0x00000060:     DW_TAG_variable
                  DW_AT_name\t("pt")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(6)
                  DW_AT_type\t(0x00000044 "type &")
                  DW_AT_location\t(DW_OP_fbreg -96)

0x00000064:     DW_TAG_variable
                  DW_AT_name\t("lr")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(7)
                  DW_AT_type\t(0x00000048 "long int &")
                  DW_AT_location\t(DW_OP_fbreg -100)

0x00000068:     DW_TAG_variable
                  DW_AT_name\t("t")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(9)
                  DW_AT_type\t(0x00000040 "type &&")
                  DW_AT_location\t(DW_OP_fbreg -104)

0x0000006c:     DW_TAG_variable
                  DW_AT_name\t("u")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(9)
                  DW_AT_type\t(0x00000040 "type &&")
                  DW_AT_location\t(DW_OP_fbreg -108)

0x00000070:     DW_TAG_variable
                  DW_AT_name\t("w")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(10)
                  DW_AT_type\t(0x00000040 "type &&")
                  DW_AT_location\t(DW_OP_fbreg -112)

0x00000074:     DW_TAG_variable
                  DW_AT_name\t("t")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(14)
                  DW_AT_type\t(0x00000048 "long int &")
                  DW_AT_location\t(DW_OP_fbreg -116)

0x00000078:     DW_TAG_variable
                  DW_AT_name\t("d")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(16)
                  DW_AT_type\t(0x0000004c "type &")
                  DW_AT_location\t(DW_OP_fbreg -120)

0x0000007c:     DW_TAG_variable
                  DW_AT_name\t("a")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(17)
                  DW_AT_type\t(0x0000004c "type &")
                  DW_AT_location\t(DW_OP_fbreg -124)

0x00000080:     DW_TAG_variable
                  DW_AT_name\t("r")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(18)
                  DW_AT_type\t(0x0000004c "type &")
                  DW_AT_location\t(DW_OP_fbreg -128)

0x00000084:     DW_TAG_variable
                  DW_AT_name\t("f")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(19)
                  DW_AT_type\t(0x0000004c "type &")
                  DW_AT_location\t(DW_OP_fbreg -132)
"""


# Its source, `src/t.cpp`: by value (`t`, `pt`), by reference to a local (`a`), to a
# member of one (`f`), and to an element of a temporary the line destroys (`d`); and a
# reference the code declares with the element's type (`r`), and one named as a binding.
BOUND_SOURCE = """#include <utility>
int main(int argc, char **) {
    Pt held{1, 2};
    Pt p{3, 4};
    long x = 5;
    auto [pt, n] = both;
    long &lr = x;
    for (long i = 0; i < 2; i++) {
        auto [t, u] = make(i)[0];
        auto [w, z] = make(i)[1];
        use(t + u + w);
    }
    {
        long &t = x;
    }
    auto &[d, e] = make(0)[0];
    auto &[a, b] = p;
    std::tuple_element<0, std::pair<long, long>>::type &r = p.first;
    auto &[f, g] = held.x;
    return 0;
}
"""


def bound(where=None, shadowed=False, readable=True):
    """A NativeSession over BOUND's `main` (with ``shadowed``, also its plain `long &t` on
    line 14), the debugger showing the declaration at ``where``; its source, unless not
    ``readable``."""
    from peeky.c_source import Statements
    from peeky.fields import CPP_LIBRARIES, local_types, type_graph
    from peeky.native import _parse_dies

    dies = _parse_dies(BOUND)
    types = local_types(dies, [0x50], through=True)
    if not shadowed:
        types["t"] = frozenset(each for each in types["t"] if each[2] == ("t.cpp", 9))
    graph = type_graph(dies, {0x10, 0x14, 0x40, 0x44, 0x48, 0x4C}, CPP_LIBRARIES)
    for offset, node in type_graph(dies, {0x28, 0x30}, CPP_LIBRARIES).items():
        graph.setdefault(offset, node)
    session = native(function={"types": types, "source": "src/t.cpp"}, fields=graph)
    statements = Statements(BOUND_SOURCE, True)
    session._statements = lambda path: statements if readable and path == "src/t.cpp" else None
    session._shown_where = lambda name, pc: where
    session._unslid = lambda frame: 0x1000
    session._place = lambda frame: ("src/t.cpp", frame["line"]) if "line" in frame else None
    return session


def test_a_pair_bindings_row_is_its_element_never_its_address():
    from peeky.native import Withholding

    session, frame = bound(), {"id": 1}
    t = variable("t", f"{ELEMENT} &&", "0x0000fffffffffb70", 4)
    pt = variable("pt", f"{POINT} &", "0x0000fffffffffae8", 5)
    lr = variable("lr", "long &", "0x0000fffffffffb20", 6)
    # Typed as the binding is declared; a reference of the program's own keeps LLDB's.
    assert session.display_type(t, frame) == ELEMENT
    assert session.display_type(pt, frame) == POINT
    assert session.display_type(lr, frame) is None
    assert session.display_type(t, frame, top=False, path=["t"]) is None
    asked = []

    def fetch(reference, count):
        asked.append((reference, count))
        return [{"name": "&t", "type": ELEMENT, "value": "10", "variablesReference": 0}]

    # A number: the debugger's one child under the reference, named and listed as the row.
    assert session.bound_element(t, frame, fetch) == {
        "variable": {"name": "t", "type": ELEMENT, "value": "10", "variablesReference": 0}
    }
    assert asked == [(4, 2)]
    assert session.bound_element(lr, frame, fetch) is None
    # A struct: its members are the reference's own children, its text follows the address.
    assert session.bound_element(pt, frame, fetch) == {"strip": True}
    assert session.element_text(pt, frame, "0x0000fffffffffae8 {x = 3, y = 4}") == "{x = 3, y = 4}"
    assert session.element_text(pt, frame, "0x0000fffffffffae8") == f"{POINT} @ 0xfffffffffae8"
    odd = session.element_text(pt, frame, "<invalid>")
    assert isinstance(odd, Withholding) and odd.label == "not shown", odd
    # Anything else the debugger lists, or a read that fails, withholds it: never the address.
    unread = (
        "`t` is a structured binding the build holds as a reference to its element, which "
        "the debugger shows as an address, and the debugger listed no element through it{}; "
        "print it from the program"
    )
    for listed in ([], [{"name": "&t", "type": "long", "value": "10"}] * 2):
        found = session.bound_element(t, frame, lambda reference, count, listed=listed: listed)
        assert found == unread.format("") and found.label == "not shown", found

    def exhausted(reference, count):
        raise ValueError("Preview budget exhausted")

    found = session.bound_element(t, frame, exhausted)
    assert found == unread.format(" (Preview budget exhausted)"), found
    # A row shown with a note: its reference may not be set, so nothing is read through it.
    noted = session.bound_element(t, frame, fetch, note="may be the last pass's value")
    assert isinstance(noted, Withholding) and noted.endswith(": may be the last pass's value")
    # Two references named `t`, the debugger's not told apart: withheld; told: read.
    shadowed = bound(shadowed=True).bound_element(t, frame, fetch)
    assert isinstance(shadowed, Withholding) and "cannot tell which" in shadowed, shadowed
    assert bound(("t.cpp", 9, True), shadowed=True).bound_element(t, frame, fetch)["variable"]
    assert bound(("t.cpp", 14, True), shadowed=True).bound_element(t, frame, fetch) is None
    # Never in C, nor without the build's debug information.
    session.target.language = "c"
    assert session.bound_element(t, frame, fetch) is None and not session.display_type(t, frame)
    assert native(function=None).bound_element(t, frame, fetch) is None


def test_a_pair_binding_a_loop_returns_to_is_withheld_not_noted():
    from peeky.native import ValueNote

    session = bound()
    reason = (
        "declared on line 14, which runs after this stop on every pass (a loop can return "
        "to its first instruction)"
    )
    session._not_yet_declared = lambda *args: ValueNote(reason, "may be the last pass's", True)
    t = variable("t", f"{ELEMENT} &&", "0x0000fffffffffb70")
    found = session.not_yet_declared(t, {"id": 1})
    assert not isinstance(found, ValueNote) and found == (
        f"{reason}: the build reads this structured binding through a reference its "
        "declaration sets, which may point anywhere until then; read it after its declaration"
    )
    assert len(found) <= 300
    # Any other local keeps its note; a reason without one stays as it is.
    assert isinstance(
        session.not_yet_declared(variable("lr", "long &", "0x1"), {"id": 1}), ValueNote
    )
    session._not_yet_declared = lambda *args: "declared at line 14, below this stop"
    assert session.not_yet_declared(t, {"id": 1}) == "declared at line 14, below this stop"


class Listing:
    """lldb-dap's `variables`: {reference: children}; the requests made."""

    def __init__(self, children):
        self.children, self.requests = children, []

    def request(self, command, arguments, timeout=None):
        self.requests.append((command, arguments))
        return {"variables": self.children.get(arguments["variablesReference"], [])}


def test_the_worker_shows_a_pair_bindings_element_or_withholds_it(tmp_path):
    from peeky.native import ValueNote
    from peeky.worker import DebugWorker

    session = bound()
    pending = {
        "u": "declared on line 14, which has not run yet at this stop",
        "w": ValueNote("declared at line 14", "may be leftovers", True),
    }
    runtime = SimpleNamespace(
        assert_current=lambda: None,
        reports_unavailable_as_text=True,
        not_yet_declared=lambda variable, frame, **kwargs: pending.get(variable["name"]),
        bound_element=session.bound_element,
        element_text=session.element_text,
        display_type=session.display_type,
    )
    worker = DebugWorker(tmp_path)
    worker.runtime, worker.state, worker.stop_id = runtime, "stopped", "stop1"
    worker.listed_frames = [{"id": 1, "name": "main"}]
    worker.dap = Listing(
        {4: [{"name": "&t", "type": ELEMENT, "value": "10", "variablesReference": 0}]}
    )
    worker.reset_views()
    worker.reset_previews()
    rows = [
        variable("t", f"{ELEMENT} &&", "0x0000fffffffffb70", 4),
        variable("pt", f"{POINT} &", "0x0000fffffffffae8 {x = 3, y = 4}", 5),
        variable("lr", "long &", "0x0000fffffffffb20", 6),
        variable("u", f"{ELEMENT} &&", "0x0000fffff7ce0ab0", 7),
        variable("w", f"{ELEMENT} &&", "0x0000fffff7ce0ab0", 8),
    ]
    t, pt, lr, u, w = worker.expose_variables(rows, {"id": 1}, ("Locals", None), ())
    assert t["value"] == "10" and t["variablesReference"] == 0, t
    assert t["type"] == ELEMENT and t["debugger_type"] == f"{ELEMENT} &&", t
    assert pt["value"] == "{x = 3, y = 4}" and pt["variablesReference"] > 0, pt
    assert pt["type"] == POINT and pt["debugger_type"] == f"{POINT} &", pt
    assert lr["value"] == "0x0000fffffffffb20" and "debugger_type" not in lr, lr
    # Not set yet: withheld as any local, typed as the binding.
    assert u["value"] is None and u["withheld"] and "withheld_as" not in u, u
    assert u["type"] == ELEMENT and u["debugger_type"] == f"{ELEMENT} &&", u
    # Shown with a note anywhere else: here its reference may not be set, so withheld.
    assert w["value"] is None and w["withheld_as"] == "not shown", w
    assert w["reason"].endswith(": may be leftovers") and "note" not in w, w
    assert not any("0x" in str(row.get("value")) for row in (t, pt, u, w))
    # Only the number's reference was listed, once.
    assert [arguments["variablesReference"] for _, arguments in worker.dap.requests] == [4]
    # A struct's text that does not follow its address is no element Peeky can tell.
    (odd,) = worker.expose_variables(
        [variable("pt", f"{POINT} &", "{x = 3, y = 4}", 9)], {"id": 1}, ("Locals", None), ()
    )
    assert odd["value"] is None and odd["withheld_as"] == "not shown", odd
    assert odd["variablesReference"] == 0 and odd["settable"] is False, odd


def test_a_newer_clangs_binding_is_told_by_its_line():
    # #291: Apple clang 21 describes `t` of `auto [t, u] = pair;` as a plain `long &`,
    # not artificial: its row was the element's address after the declaration ran.
    from peeky.c_source import Statements

    source = "int main() {\n    Pt held{1, 2};\n    auto &[t, u] = held;\n    long &w = x;\n}\n"
    session = bound()
    session._function_at = lambda frame: {
        "types": {
            "held": frozenset({(0x14, "automatic", ("t.cpp", 2))}),
            "t": frozenset({(0x48, "automatic", ("t.cpp", 3))}),
            "w": frozenset({(0x48, "automatic", ("t.cpp", 4))}),
        },
        "source": "src/t.cpp",
    }
    session._statements = lambda path: Statements(source, True) if path == "src/t.cpp" else None
    t, w = variable("t", "long &", "0x16af672d0", 4), variable("w", "long &", "0x16af672d8", 5)

    def fetch(reference, count):
        return [{"name": "&t", "type": "long", "value": "1001", "variablesReference": 0}]

    assert session.display_type(t, {"id": 1}) == "long"
    assert session.bound_element(t, {"id": 1}, fetch)["variable"]["value"] == "1001"
    # A reference the code declares keeps LLDB's text: its address.
    assert session.display_type(w, {"id": 1}) is None
    assert session.bound_element(w, {"id": 1}, fetch) is None
    # Another file's declaration, or a source Peeky cannot read, is not told.
    session._statements = lambda path: None
    assert session.bound_element(t, {"id": 1}, fetch) is None


def test_a_binding_by_reference_is_read_only_through_an_object_of_this_frame():
    # Review of #296: `auto &[d, e] = make(0)[0];` refers into a temporary the line
    # destroyed; a later `make(7000)` reused its memory, and `d` showed `8000` with no note.
    from peeky.native import Withholding

    session, frame = bound(), {"id": 1}

    def fetch(reference, count):
        return [{"name": "&x", "type": ELEMENT, "value": "3", "variablesReference": 0}]

    row = {"type": f"{ELEMENT} &", "value": "0x0000aaaaaaad32b0", "variablesReference": 4}
    dangling = session.bound_element({**row, "name": "d"}, frame, fetch)
    assert isinstance(dangling, Withholding) and dangling.label == "not shown", dangling
    assert dangling == (
        "the binding refers to an object Peeky can't prove is still alive: `d` binds "
        "`make(0)[0]` by reference, and Peeky proves only a local or its non-union member. Read "
        "it where the code names that object, or print it from the program"
    )
    assert len(dangling) <= 300
    # A local object in scope, or a member of one: read through.
    for name in ("a", "f"):
        assert session.bound_element({**row, "name": name}, frame, fetch)["variable"]["value"]
    # The code's own reference with the element's type is no binding: LLDB's text stays.
    assert session.bound_element({**row, "name": "r"}, frame, fetch) is None
    assert session.display_type({**row, "name": "r"}, frame) is None
    # A line whose binding Peeky cannot read through: nor is anything read through it.
    unknown = session._bound_alive(frame, "q", ("t.cpp", 16), None)
    assert isinstance(unknown, Withholding) and "can't read what `q`'s declaration" in unknown
    # GCC's type, but a line Peeky cannot read: neither read nor shown as an address.
    unread = bound(readable=False).bound_element({**row, "name": "t"}, frame, fetch)
    assert isinstance(unread, Withholding) and "cannot read its declaration's line" in unread
    assert bound(readable=False).bound_element({**row, "name": "lr"}, frame, fetch) is None


def test_an_optimized_bindings_own_line_withholds_it():
    # Review of #296: in an optimized build, where in its declaration's lines a stop is
    # cannot be told, so neither can whether this pass has set the reference.
    session = bound()
    session._not_yet_declared = lambda *args: None
    t = variable("t", f"{ELEMENT} &&", "0x0000fffffffffb70")
    assert session.not_yet_declared(t, {"id": 1, "line": 9}) is None
    session.target.optimization = "O1"
    found = session.not_yet_declared(t, {"id": 1, "line": 9})
    assert found == (
        "declared on line 9, and in an optimized build Peeky cannot tell whether this stop is "
        "past that declaration on this pass: the build reads this structured binding through "
        "a reference its declaration sets, which may point anywhere until then; read it below "
        "its declaration"
    )
    assert len(found) <= 300
    # Below its declaration, or any other local: as before.
    assert session.not_yet_declared(t, {"id": 1, "line": 11}) is None
    lr = variable("lr", "long &", "0x0000fffffffffb20")
    assert session.not_yet_declared(lr, {"id": 1, "line": 7}) is None


UNIONS = """0x0000000b: DW_TAG_compile_unit
              DW_AT_name\t("t.cpp")

0x00000010:   DW_TAG_base_type
                DW_AT_name\t("long int")
                DW_AT_encoding\t(DW_ATE_signed)
                DW_AT_byte_size\t(0x08)

0x00000014:   DW_TAG_structure_type
                DW_AT_name\t("Pt")
                DW_AT_byte_size\t(0x08)

0x00000018:     DW_TAG_member
                  DW_AT_name\t("x")
                  DW_AT_type\t(0x00000010 "long int")
                  DW_AT_data_member_location\t(0x00)

0x00000020:   DW_TAG_union_type
                DW_AT_name\t("U")
                DW_AT_byte_size\t(0x08)

0x00000024:     DW_TAG_member
                  DW_AT_name\t("p")
                  DW_AT_type\t(0x00000014 "Pt")

0x00000030:   DW_TAG_structure_type
                DW_AT_name\t("Holder")
                DW_AT_byte_size\t(0x10)

0x00000034:     DW_TAG_member
                  DW_AT_name\t("u")
                  DW_AT_type\t(0x00000020 "U")
                  DW_AT_data_member_location\t(0x00)

0x00000038:     DW_TAG_member
                  DW_AT_name\t("inner")
                  DW_AT_type\t(0x00000014 "Pt")
                  DW_AT_data_member_location\t(0x08)

0x00000040:   DW_TAG_namespace
                DW_AT_name\t("std")

0x00000044:     DW_TAG_union_type
                  DW_AT_name\t("_Storage")
                  DW_AT_byte_size\t(0x08)

0x00000048:       DW_TAG_member
                    DW_AT_name\t("v")
                    DW_AT_type\t(0x00000014 "Pt")
"""


def test_a_member_chain_through_a_union_is_never_proven():
    # Round 2 of the #296 review: `auto &[a, b] = u.p;`, then `u.p.~pair(); new (&u.q)
    # std::pair<long, long>{7, 8};` showed `a = 7, b = 8`: which member of a union is alive
    # changes as the program runs, a standard library's union too.
    from peeky.fields import CPP_LIBRARIES, type_graph
    from peeky.native import _member_object, _parse_dies

    graph = type_graph(_parse_dies(UNIONS), {0x14, 0x20, 0x30, 0x44}, CPP_LIBRARIES)
    holder, union, library = (Typed(graph, offset) for offset in (0x30, 0x20, 0x44))
    assert _member_object(holder, "inner").kind() == "record"
    assert _member_object(holder, "u").kind() == "union"
    assert _member_object(_member_object(holder, "u"), "p") is None
    assert _member_object(union, "p") is None and _member_object(library, "v") is None
    assert _member_object(holder, "missing") is None and _member_object(None, "p") is None


# #297: bindings by reference into a local container, or of a call's result, which GCC
# holds as references to `std::tuple_element<I, E>::type` (as BOUND's).
HELD = """0x0000000b: DW_TAG_compile_unit
              DW_AT_name\t("t.cpp")

0x00000010:   DW_TAG_base_type
                DW_AT_name\t("long int")
                DW_AT_encoding\t(DW_ATE_signed)
                DW_AT_byte_size\t(0x08)

0x00000020:   DW_TAG_namespace
                DW_AT_name\t("std")

0x00000024:     DW_TAG_structure_type
                  DW_AT_name\t("tuple_element<0, std::pair<long int, long int> >")

0x00000028:       DW_TAG_typedef
                    DW_AT_name\t("type")
                    DW_AT_type\t(0x00000010 "long int")

0x00000030:     DW_TAG_structure_type
                  DW_AT_name\t("pair<long int, long int>")
                  DW_AT_byte_size\t(0x10)

0x00000034:       DW_TAG_member
                    DW_AT_name\t("first")
                    DW_AT_type\t(0x00000010 "long int")
                    DW_AT_data_member_location\t(0x00)

0x00000038:       DW_TAG_member
                    DW_AT_name\t("second")
                    DW_AT_type\t(0x00000010 "long int")
                    DW_AT_data_member_location\t(0x08)

0x0000003a:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_T1")
                    DW_AT_type\t(0x00000010 "long int")

0x0000003c:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_T2")
                    DW_AT_type\t(0x00000010 "long int")

0x00000040:     DW_TAG_class_type
                  DW_AT_name\t("vector<std::pair<long int, long int>, std::allocator<std::pair<long int, long int> > >")
                  DW_AT_byte_size\t(0x18)

0x00000044:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_Tp")
                    DW_AT_type\t(0x00000030 "std::pair<long int, long int>")

0x00000048:       DW_TAG_member
                    DW_AT_name\t("_M_impl")
                    DW_AT_type\t(0x00000010 "long int")
                    DW_AT_data_member_location\t(0x00)

0x0000004a:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_Alloc")
                    DW_AT_type\t(0x00000084 "std::allocator<std::pair<long int, long int> >")

0x00000050:     DW_TAG_class_type
                  DW_AT_name\t("map<long int, long int, std::less<long int>, std::allocator<std::pair<long int const, long int> > >")
                  DW_AT_byte_size\t(0x30)

0x00000054:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_Key")
                    DW_AT_type\t(0x00000010 "long int")

0x00000058:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_Tp")
                    DW_AT_type\t(0x00000010 "long int")

0x0000005a:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_Compare")
                    DW_AT_type\t(0x00000088 "std::less<long int>")

0x0000005c:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_Alloc")
                    DW_AT_type\t(0x0000008c "std::allocator<std::pair<long int const, long int> >")

0x00000060:     DW_TAG_class_type
                  DW_AT_name\t("deque<std::pair<long int, long int>, std::allocator<std::pair<long int, long int> > >")
                  DW_AT_byte_size\t(0x50)

0x00000064:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_Tp")
                    DW_AT_type\t(0x00000030 "std::pair<long int, long int>")

0x00000070:     DW_TAG_namespace
                  DW_AT_name\t("__1")

0x00000074:       DW_TAG_class_type
                    DW_AT_name\t("vector<std::pair<long int, long int>, std::allocator<std::pair<long int, long int> > >")
                    DW_AT_byte_size\t(0x18)

0x00000078:         DW_TAG_template_type_parameter
                      DW_AT_name\t("_Tp")
                      DW_AT_type\t(0x00000030 "std::pair<long int, long int>")

0x0000007c:         DW_TAG_member
                      DW_AT_name\t("__begin_")
                      DW_AT_type\t(0x00000010 "long int")
                      DW_AT_data_member_location\t(0x00)

0x00000080:         DW_TAG_member
                      DW_AT_name\t("__end_")
                      DW_AT_type\t(0x00000010 "long int")
                      DW_AT_data_member_location\t(0x08)

0x00000082:         DW_TAG_template_type_parameter
                      DW_AT_name\t("_Allocator")
                      DW_AT_type\t(0x00000084 "std::allocator<std::pair<long int, long int> >")

0x00000084:     DW_TAG_class_type
                  DW_AT_name\t("allocator<std::pair<long int, long int> >")
                  DW_AT_byte_size\t(0x01)

0x00000088:     DW_TAG_structure_type
                  DW_AT_name\t("less<long int>")
                  DW_AT_byte_size\t(0x01)

0x0000008c:     DW_TAG_class_type
                  DW_AT_name\t("allocator<std::pair<long int const, long int> >")
                  DW_AT_byte_size\t(0x01)

0x00000090:   DW_TAG_reference_type
                DW_AT_type\t(0x00000028 "type")

0x00000094:   DW_TAG_rvalue_reference_type
                DW_AT_type\t(0x00000028 "type")

0x00000098:   DW_TAG_reference_type
                DW_AT_type\t(0x00000030 "std::pair<long int, long int>")

0x00000099:   DW_TAG_class_type
                DW_AT_name\t("Spy<std::pair<long int, long int> >")
                DW_AT_byte_size\t(0x01)

0x0000009a:   DW_TAG_structure_type
                DW_AT_name\t("E")
                DW_AT_byte_size\t(0x10)

0x0000009b:   DW_TAG_structure_type
                DW_AT_name\t("ByValue")
                DW_AT_byte_size\t(0x01)

0x0000009d:   DW_TAG_pointer_type
                DW_AT_type\t(0x00000010 "long int")

0x000000a0:   DW_TAG_subprogram
                DW_AT_name\t("one")
                DW_AT_type\t(0x00000030 "std::pair<long int, long int>")

0x000000a4:   DW_TAG_subprogram
                DW_AT_name\t("pick")
                DW_AT_type\t(0x00000098 "std::pair<long int, long int> &")

0x000000b0:   DW_TAG_subprogram
                DW_AT_name\t("main")

0x000000b4:     DW_TAG_variable
                  DW_AT_name\t("m")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(3)
                  DW_AT_type\t(0x00000050 "std::map<long int, long int>")
                  DW_AT_location\t(DW_OP_fbreg -48)

0x000000b8:     DW_TAG_variable
                  DW_AT_name\t("v")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(4)
                  DW_AT_type\t(0x00000040 "std::vector<std::pair<long int, long int> >")
                  DW_AT_location\t(DW_OP_fbreg -72)

0x000000bc:     DW_TAG_variable
                  DW_AT_name\t("k")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(5)
                  DW_AT_type\t(0x00000090 "type &")
                  DW_AT_location\t(DW_OP_fbreg -80)

0x000000c0:     DW_TAG_variable
                  DW_AT_name\t("x")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(5)
                  DW_AT_type\t(0x00000090 "type &")
                  DW_AT_location\t(DW_OP_fbreg -88)

0x000000c4:     DW_TAG_variable
                  DW_AT_name\t("r")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(8)
                  DW_AT_type\t(0x00000090 "type &")
                  DW_AT_location\t(DW_OP_fbreg -96)

0x000000c8:     DW_TAG_variable
                  DW_AT_name\t("p")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(9)
                  DW_AT_type\t(0x00000094 "type &&")
                  DW_AT_location\t(DW_OP_fbreg -104)

0x000000cc:     DW_TAG_variable
                  DW_AT_name\t("a")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(10)
                  DW_AT_type\t(0x00000094 "type &&")
                  DW_AT_location\t(DW_OP_fbreg -112)

0x000000d0:     DW_TAG_variable
                  DW_AT_name\t("w")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(11)
                  DW_AT_type\t(0x00000040 "std::vector<std::pair<long int, long int> >")
                  DW_AT_location\t(DW_OP_fbreg -136)

0x000000d4:     DW_TAG_variable
                  DW_AT_name\t("c")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(12)
                  DW_AT_type\t(0x00000090 "type &")
                  DW_AT_location\t(DW_OP_fbreg -144)

0x000000d8:     DW_TAG_variable
                  DW_AT_name\t("dq")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(14)
                  DW_AT_type\t(0x00000060 "std::deque<std::pair<long int, long int> >")
                  DW_AT_location\t(DW_OP_fbreg -224)

0x000000dc:     DW_TAG_variable
                  DW_AT_name\t("e")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(15)
                  DW_AT_type\t(0x00000090 "type &")
                  DW_AT_location\t(DW_OP_fbreg -232)

0x000000e0:     DW_TAG_variable
                  DW_AT_name\t("lv")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(16)
                  DW_AT_type\t(0x00000074 "std::__1::vector<std::pair<long int, long int> >")
                  DW_AT_location\t(DW_OP_fbreg -256)

0x000000e4:     DW_TAG_variable
                  DW_AT_name\t("g")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(17)
                  DW_AT_type\t(0x00000090 "type &")
                  DW_AT_location\t(DW_OP_fbreg -264)

0x000000e8:     DW_TAG_variable
                  DW_AT_name\t("s")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(8)
                  DW_AT_type\t(0x00000090 "type &")
                  DW_AT_location\t(DW_OP_fbreg -272)

0x000000ec:     DW_TAG_variable
                  DW_AT_name\t("q")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(9)
                  DW_AT_type\t(0x00000094 "type &&")
                  DW_AT_location\t(DW_OP_fbreg -280)

0x000000f0:     DW_TAG_variable
                  DW_AT_name\t("b")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(10)
                  DW_AT_type\t(0x00000094 "type &&")
                  DW_AT_location\t(DW_OP_fbreg -288)

0x000000f4:     DW_TAG_variable
                  DW_AT_name\t("d")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(12)
                  DW_AT_type\t(0x00000090 "type &")
                  DW_AT_location\t(DW_OP_fbreg -296)

0x000000f8:     DW_TAG_variable
                  DW_AT_name\t("h")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(17)
                  DW_AT_type\t(0x00000090 "type &")
                  DW_AT_location\t(DW_OP_fbreg -304)
"""
HELD_SOURCE = """#include <map>
int main(int argc, char **) {
    std::map<long, long> m{{21, 22}, {23, 24}};
    std::vector<P> v{{11, 12}, {13, 14}};
    for (const auto &[k, x] : m) {
        use(k + x); use((long)m.size());
    }
    auto &[r, s] = v[0];
    auto &&[p, q] = one(argc);
    auto &&[a, b] = pick(*held);
    std::vector<P> w{{1, 2}};
    auto &[c, d] = w[0];
    w.push_back({3, 4});
    std::deque<P> dq{{5, 6}};
    for (auto &[e, f] : dq) use(e);
    std::vector<P> lv{{7, 8}};
    auto &[g, h] = lv[0];
    return 0;
}
"""
ENTRY = "std::pair<const long, long>"


def held(optimization="O0", preprocessed=None, dwarf=HELD):
    """A NativeSession over HELD's `main`, its source HELD_SOURCE; ``preprocessed``: the
    Statements of its translation unit as the compiler preprocessed it (else the same)."""
    from peeky.c_source import Statements
    from peeky.fields import CPP_LIBRARIES, local_types, type_graph
    from peeky.native import _parse_dies, call_arguments

    dies = _parse_dies(dwarf)
    types = local_types(dies, [0xB0], through=True)
    kinds = {kind for declared in types.values() for kind, _, _ in declared}
    function = {"types": types, "source": "src/t.cpp", "span": (2, 19)}
    graph = type_graph(dies, kinds, CPP_LIBRARIES)
    # As the build's: one level down through references too.
    aimed = {node[2] for node in graph.values() if node[0] == "reference"}
    for offset, node in type_graph(dies, aimed, CPP_LIBRARIES).items():
        graph.setdefault(offset, node)
    session = native(optimization=optimization, function=function, fields=graph)
    statements = Statements(HELD_SOURCE, True)
    session._statements = lambda path: statements if path == "src/t.cpp" else None
    session._shown_where = lambda name, pc: None
    session._unslid = lambda frame: 0x1000
    session._units = lambda source: ["src/t.cpp"]
    unit = SimpleNamespace(
        files={"src/t.cpp"},
        line=lambda source, number: number,
        origins=[("src/t.cpp", number) for number in range(1, HELD_SOURCE.count("\n") + 2)],
    )
    session._preprocessed = lambda name: (unit, preprocessed or statements)
    session.call_arguments = call_arguments(dies)
    # It replaces no `operator new`, and its launch loads no library first.
    session.allocation_replaced = False
    session.prepared = SimpleNamespace(manifest=SimpleNamespace(effective_environment=()))
    session.home = "home"
    session.system_preload = False
    return session


def unproven(base, what, why):
    # Eval 13: never "bind by value", which breaks a loop that writes through the binding.
    element = re.fullmatch(r"an element of `(.+)`", what)
    instead = (
        f"Read `{element[1]}` itself, or print the value from the program"
        if element
        else "Read it where the code names that object, or print it from the program"
    )
    return (
        f"the binding refers to an object Peeky can't prove is still alive: `{base}` binds "
        f"{what} by reference, {why}. {instead}"
    )


def test_a_binding_into_a_local_container_is_read_where_it_holds_that_element():
    # #297: `for (const auto &[k, x] : m)` over a local map and `auto &[r, s] = v[0];` were
    # `not shown`, though nothing changes either container while the binding refers to it.
    session, frame = held(), {"id": 1}
    entries = [
        {"name": f"[{i}]", "type": ENTRY, "value": f"{ENTRY} @ {at:#x}", "variablesReference": 30}
        for i, at in enumerate((0xAAAAAAAD32D0, 0xAAAAAAAD3310))
    ]
    asked = []

    def fetch(reference, count, start=0):
        asked.append((reference, count, start))
        if reference == 9:
            return entries[start : start + count]
        return [{"name": "&k", "type": ELEMENT, "value": "21", "variablesReference": 0}]

    m = {"name": "m", "type": "std::map<long, long>", "value": "size=2", "variablesReference": 9}
    k = variable("k", f"{ELEMENT} &", "0x0000aaaaaaad32d0", 4)
    x = variable("x", f"{ELEMENT} &", "0x0000aaaaaaad3318", 5)
    # In one of the map's entries, as the debugger lists them: read through.
    for row in (k, x):
        assert session.bound_element(row, frame, fetch, siblings=[m])["variable"]["value"] == "21"
    assert asked[:2] == [(9, 2, 0), (4, 2, 0)], asked
    element = ("an element of `m`", "k")

    def withheld(row, why, **kwargs):
        found = session.bound_element(row, frame, fetch, **kwargs)
        assert found == unproven(element[1], element[0], why) and found.label == "not shown"
        assert len(found) <= 300, found

    # In none of them (an entry erased, whose memory is reused): withheld.
    gone = variable("k", f"{ELEMENT} &", "0x0000aaaaaaad3400", 4)
    withheld(gone, "but it points into none of `m`'s entries now", siblings=[m])
    # An entry's last byte is no room for the element's eight.
    withheld(
        variable("k", f"{ELEMENT} &", "0x0000aaaaaaad32dc", 4),
        "but it points into none of `m`'s entries now",
        siblings=[m],
    )
    unread = "and Peeky could not read `m`'s elements to check where it points"
    # The entries not all listed, or listed with no place, or no one `m` among the locals.
    withheld(gone, unread, siblings=[{**m, "value": "size=3"}])
    entries[1]["value"] = "{first = 23, second = 24}"
    withheld(gone, unread, siblings=[m])
    withheld(gone, unread, siblings=[])
    withheld(gone, unread, siblings=[m, {**m, "name": "m @ t.cpp:30"}])
    withheld(gone, unread, siblings=[{**m, "value": "size=2 {...}"}])
    many = "and `m` has more than 256 entries, more than Peeky checks"
    withheld(gone, many, siblings=[{**m, "value": "size=257"}])
    # Its pages, past the first 64 entries.
    entries[:] = [
        {"name": f"[{i}]", "type": ENTRY, "value": f"{ENTRY} @ {0x1000 + 32 * i:#x}"}
        for i in range(100)
    ]
    del asked[:]
    far = variable("k", f"{ELEMENT} &", f"{0x1000 + 32 * 99:#x}", 4)
    assert session.bound_element(far, frame, fetch, siblings=[{**m, "value": "size=100"}])
    assert asked == [(9, 64, 0), (9, 36, 64), (4, 2, 0)], asked

    # A vector's element: between where its elements start and end, as its own members say.
    def ends(start, finish, prefix="v._M_impl._M_"):
        return Counts({f"{prefix}start": start, f"{prefix}finish": finish})

    v = {"name": "v", "type": "std::vector<P>", "value": "size=2", "variablesReference": 8}
    raw = ends("(pointer) _M_start = 0x0000aaaaaaad3740", "0x0000aaaaaaad3760")
    r = variable("r", f"{ELEMENT} &", "0x0000aaaaaaad3758", 6)
    assert session.bound_element(r, frame, fetch, siblings=[v], raw=raw)["variable"]
    element = ("an element of `v`", "r")
    past = variable("r", f"{ELEMENT} &", "0x0000aaaaaaad3760", 6)
    withheld(past, "but it points outside `v`'s elements now", siblings=[v], raw=raw)
    unread = "and Peeky could not read `v`'s elements to check where it points"
    withheld(r, unread, siblings=[v], raw=ends("0x0000aaaaaaad3740", "<invalid>"))
    withheld(r, unread, siblings=[v], raw=Counts({}))
    withheld(r, unread, siblings=[v])
    # libc++'s own members.
    lv = {**v, "name": "lv"}
    g = variable("g", f"{ELEMENT} &", "0x0000000100649350", 6)
    raw = ends("0x0000000100649350", "0x0000000100649360", "lv.__")
    raw.counts = {"lv.__begin_": raw.counts["lv.__start"], "lv.__end_": raw.counts["lv.__finish"]}
    assert session.bound_element(g, frame, fetch, siblings=[lv], raw=raw)["variable"]
    # The code may add or remove one while it is in scope: never read, whatever its place.
    element = ("an element of `w`", "c")
    w = {**v, "name": "w"}
    c = variable("c", f"{ELEMENT} &", "0x0000aaaaaaad3740", 6)
    withheld(
        c,
        "and while it is in scope the code may change `w` or keep a pointer or reference into it",
        siblings=[w],
        raw=ends("0x0000aaaaaaad3740", "0x0000aaaaaaad3760", "w._M_impl._M_"),
    )
    # Neither a vector nor a map nor a set: a deque's element.
    element = ("an element of `dq`", "e")
    withheld(
        variable("e", f"{ELEMENT} &", "0x0000aaaaaaad3740", 6),
        "and Peeky proves an element alive only in a local vector, or in a local map or set "
        "a range `for` walks",
    )
    # Optimized: the debugger's view of either may be stale.
    element = ("an element of `m`", "k")
    found = held("O1").bound_element(k, frame, fetch, siblings=[m])
    assert found == unproven(
        "k", "an element of `m`", "and in an optimized build Peeky can't check it"
    )


def test_a_binding_of_a_calls_result_is_withheld():
    # #297 read `auto &&[p, q] = one(argc);` in its frame's stack. Review of #368, round 3:
    # with the named return value optimization the function called builds its result where
    # the caller keeps it, and may keep a pointer into it (`saved = &r; return r;`).
    from peeky.c_source import Statements

    frame = {"id": 1}

    def fetch(reference, count, start=0):
        return [{"name": "&p", "type": ELEMENT, "value": "31", "variablesReference": 0}]

    why = "a call's result, which the function called may keep a pointer into"
    p = variable("p", f"{ELEMENT} &&", "0x0000fffffffffa58", 4)
    a = variable("a", f"{ELEMENT} &&", "0x0000fffffffffa58", 4)
    for row, what, session in (
        (p, "`one(argc)`", held()),
        (a, "`pick(*held)`", held()),
        (p, "`one(argc)`", held("O2")),
        (p, "`one(argc)`", held(preprocessed=Statements(HELD_SOURCE, True))),
    ):
        found = session.bound_element(row, frame, fetch)
        assert found == unproven(row["name"], what, why) and found.label == "not shown"


def test_a_binding_is_read_only_where_nothing_may_end_its_elements_life():
    # Review of #368: `P *p = v.data(); auto &[r, s] = v[0]; std::destroy_at(p);` and a
    # stop before `new (p) P{13, 14}` read dead bytes; the container's own uses were
    # checked, not the pointers and references they give, nor the binding's names.
    from peeky.c_source import Statements

    frame = {"id": 1}

    def fetch(reference, count, start=0):
        return [{"name": "&r", "type": ELEMENT, "value": "11", "variablesReference": 0}]

    v = {"name": "v", "type": "std::vector<P>", "value": "size=2", "variablesReference": 8}
    raw = Counts(
        {"v._M_impl._M_start": "0x0000aaaaaaad3740", "v._M_impl._M_finish": "0x0000aaaaaaad3760"}
    )
    r = variable("r", f"{ELEMENT} &", "0x0000aaaaaaad3740", 6)
    assert held().bound_element(r, frame, fetch, siblings=[v], raw=raw)["variable"]
    changed = (
        "and while it is in scope the code may change `v` or keep a pointer or reference into it"
    )
    for edit in (
        # A pointer into it from before the binding, through which code may end the
        # element's life; its name's address, a call that may take it by reference.
        ("    }\n    auto &[r", "    } P *alias = v.data();\n    auto &[r"),
        ("    return 0;", "    std::destroy_at(&r); return 0;"),
        ("    return 0;", "    keep(s); return 0;"),
    ):
        expanded = Statements(HELD_SOURCE.replace(*edit), True)
        found = held(preprocessed=expanded).bound_element(r, frame, fetch, siblings=[v], raw=raw)
        assert found == unproven("r", "an element of `v`", changed), edit
    # A name of a struct, whose uses Peeky can't follow: withheld, saying so.
    session = held()
    types = dict(session._function_at(frame)["types"])
    types["s"] = frozenset({(0x98, "through", ("t.cpp", 8))})
    session._function_at = lambda frame: {"types": types, "source": "src/t.cpp", "span": (2, 19)}
    found = session.bound_element(r, frame, fetch, siblings=[v], raw=raw)
    assert found == unproven(
        "r",
        "an element of `v`",
        "and Peeky follows only names of numbers or characters through the code, never a pointer",
    )


def test_a_containers_elements_are_proven_only_where_the_library_alone_makes_them():
    # Review of #368, round 2: a constructor of the program's element type, or its own
    # allocator or order, sees where each element is (`E() { saved = &x; }`), and may keep
    # it to end that element's life where the binding's scope shows nothing.
    frame = {"id": 1}

    def fetch(reference, count, start=0):
        if reference == 9:
            return [{"name": "[0]", "type": ENTRY, "value": f"{ENTRY} @ 0xaaaaaaad32d0"}]
        return [{"name": "&k", "type": ELEMENT, "value": "21", "variablesReference": 0}]

    m = {"name": "m", "type": "std::map<long, long>", "value": "size=1", "variablesReference": 9}
    k = variable("k", f"{ELEMENT} &", "0x0000aaaaaaad32d0", 4)
    v = {"name": "v", "type": "std::vector<P>", "value": "size=2", "variablesReference": 8}
    raw = Counts(
        {"v._M_impl._M_start": "0x0000aaaaaaad3740", "v._M_impl._M_finish": "0x0000aaaaaaad3760"}
    )
    r = variable("r", f"{ELEMENT} &", "0x0000aaaaaaad3740", 6)
    # The library's own: read.
    assert held().bound_element(k, frame, fetch, siblings=[m])["variable"]
    assert held().bound_element(r, frame, fetch, siblings=[v], raw=raw)["variable"]
    policy = (
        "and Peeky proves only standard vectors, maps and sets of numbers, with the default "
        "allocator and order"
    )
    allocator = '(0x00000084 "std::allocator<std::pair<long int, long int> >")'
    pair = '(0x00000030 "std::pair<long int, long int>")'
    less = '(0x00000088 "std::less<long int>")'
    mapped = 'DW_AT_name\t("_Tp")\n                    DW_AT_type\t(0x00000010 "long int")'
    for edit, row, siblings in (
        # A vector with an allocator of the program's, of elements of its own type.
        ((allocator, '(0x00000099 "Spy<std::pair<long int, long int> >")'), r, [v]),
        ((pair, '(0x0000009a "E")'), r, [v]),
        # A map with an order of the program's, or a pointer for its value.
        ((less, '(0x0000009b "ByValue")'), k, [m]),
        ((mapped, mapped.replace('0x00000010 "long int"', '0x0000009d "long int *"')), k, [m]),
    ):
        found = held(dwarf=HELD.replace(*edit, 1)).bound_element(
            row, frame, fetch, siblings=siblings, raw=raw
        )
        container = siblings[0]["name"]
        assert found == unproven(row["name"], f"an element of `{container}`", policy), edit
    # Review of #368, round 3: `std::allocator` calls `operator new`, which the program may
    # replace, or a library the launch loads first; where Peeky can't tell, neither.
    for replaced, environment, why in (
        (
            True,
            (),
            "and the program replaces `operator new` or `operator delete`, which may keep "
            "where each element is",
        ),
        (None, (), "and Peeky can't tell whether the program replaces `operator new`"),
        (
            False,
            (("LD_PRELOAD", "libspy.so"),),
            "and the program runs with `LD_PRELOAD`, whose library may replace `operator new`",
        ),
        (
            False,
            (("DYLD_INSERT_LIBRARIES", "spy.dylib"),),
            "and the program runs with `DYLD_INSERT_LIBRARIES`, whose library may replace "
            "`operator new`",
        ),
    ):
        session = held()
        session.allocation_replaced = replaced
        session.prepared.manifest.effective_environment = environment
        found = session.bound_element(k, frame, fetch, siblings=[m])
        assert found == unproven("k", "an element of `m`", why), why
    session = held()
    del session.prepared
    found = session.bound_element(k, frame, fetch, siblings=[m])
    assert found == unproven(
        "k", "an element of `m`", "and Peeky can't tell whether the launch loads a library first"
    )
    # Round 4: the host's `/etc/ld.so.preload`, read at launch, naming a library; unread.
    for preloaded, why in (
        (
            True,
            "and the program runs with `/etc/ld.so.preload`, whose library may replace "
            "`operator new`",
        ),
        (None, "and Peeky can't tell whether the launch loads a library first"),
    ):
        session = held()
        session.system_preload = preloaded
        found = session.bound_element(k, frame, fetch, siblings=[m])
        assert found == unproven("k", "an element of `m`", why), why
    session = held()
    del session.system_preload
    found = session.bound_element(k, frame, fetch, siblings=[m])
    assert found == unproven(
        "k", "an element of `m`", "and Peeky can't tell whether the launch loads a library first"
    )


def test_the_hosts_ld_so_preload_is_read_at_launch(tmp_path, monkeypatch):
    """Review of #368, round 4: glibc's loader loads the libraries `/etc/ld.so.preload`
    names into every program first, so one may replace `operator new`. Blank and comment
    lines name none; a file that can't be read whole is unknown; none at all, nothing."""
    import os
    import sys

    from peeky import native
    from peeky.native import NativeSession, system_preload

    preload = tmp_path / "ld.so.preload"
    assert system_preload(preload) is False  # absent
    for text, names in (
        ("", False),
        ("\n  \n\t\n", False),
        ("# a comment\n   # another\n\n", False),
        ("#/usr/lib/libspy.so\n", False),
        ("/usr/lib/libspy.so\n", True),
        ("# spies\n/usr/lib/libspy.so # kept\n", True),
        ("libspy.so", True),
        (": :\n", False),
        ("a:b\n", True),
    ):
        preload.write_text(text)
        assert system_preload(preload) is names, text
    preload.write_bytes(b"#" * (native.MAX_LD_SO_PRELOAD + 1))
    assert system_preload(preload) is None  # too long to read whole
    preload.unlink()
    preload.mkdir()
    assert system_preload(preload) is None  # not a file
    preload.rmdir()
    if os.geteuid() != 0:
        preload.write_text("# nothing\n")
        preload.chmod(0)
        try:
            assert system_preload(preload) is None  # unreadable
        finally:
            preload.chmod(0o600)
    os.mkfifo(tmp_path / "fifo")
    assert system_preload(tmp_path / "fifo") is None  # never blocks on a pipe

    session = NativeSession.__new__(NativeSession)
    session.assert_current = lambda: None
    session.source_path = lambda program: None
    session.binary, session.target = "a.out", SimpleNamespace(arguments=())
    session.prepared = SimpleNamespace(workspace="w")
    monkeypatch.setattr(native, "LD_SO_PRELOAD", preload)
    monkeypatch.setattr(sys, "platform", "linux")
    preload.write_text("/usr/lib/libspy.so\n")
    session.launch_args("p", "r")
    assert session.system_preload is True
    preload.write_text("# off\n")
    session.launch_args("p", "r")
    assert session.system_preload is False
    # Only Linux's loader reads it.
    monkeypatch.setattr(sys, "platform", "darwin")
    preload.write_text("/usr/lib/libspy.so\n")
    session.launch_args("p", "r")
    assert session.system_preload is False


CALLS = """0x0000000b: DW_TAG_compile_unit
              DW_AT_name\t("t.cpp")

0x00000010:   DW_TAG_base_type
                DW_AT_name\t("long int")
                DW_AT_encoding\t(DW_ATE_signed)
                DW_AT_byte_size\t(0x08)

0x00000014:   DW_TAG_structure_type
                DW_AT_name\t("Pt")
                DW_AT_byte_size\t(0x08)

0x00000016:     DW_TAG_member
                  DW_AT_name\t("n")
                  DW_AT_type\t(0x00000010 "long int")
                  DW_AT_data_member_location\t(0x00)

0x00000018:   DW_TAG_base_type
                DW_AT_name\t("char")
                DW_AT_encoding\t(DW_ATE_signed_char)
                DW_AT_byte_size\t(0x01)

0x00000020:   DW_TAG_namespace
                DW_AT_name\t("std")

0x00000024:     DW_TAG_structure_type
                  DW_AT_name\t("pair<long int, long int>")
                  DW_AT_byte_size\t(0x10)

0x00000028:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_T1")
                    DW_AT_type\t(0x00000010 "long int")

0x0000002c:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_T2")
                    DW_AT_type\t(0x00000010 "long int")

0x00000030:       DW_TAG_member
                    DW_AT_name\t("first")
                    DW_AT_type\t(0x00000010 "long int")
                    DW_AT_data_member_location\t(0x00)

0x00000034:       DW_TAG_member
                    DW_AT_name\t("second")
                    DW_AT_type\t(0x00000010 "long int")
                    DW_AT_data_member_location\t(0x08)

0x00000038:     DW_TAG_structure_type
                  DW_AT_name\t("pair<Pt, long int>")
                  DW_AT_byte_size\t(0x10)

0x0000003c:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_T1")
                    DW_AT_type\t(0x00000014 "Pt")

0x00000040:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_T2")
                    DW_AT_type\t(0x00000010 "long int")

0x00000044:       DW_TAG_member
                    DW_AT_name\t("first")
                    DW_AT_type\t(0x00000014 "Pt")
                    DW_AT_data_member_location\t(0x00)

0x00000048:     DW_TAG_structure_type
                  DW_AT_name\t("pair<long int const&, long int>")
                  DW_AT_byte_size\t(0x10)

0x0000004c:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_T1")
                    DW_AT_type\t(0x00000090 "const long int &")

0x00000050:       DW_TAG_member
                    DW_AT_name\t("first")
                    DW_AT_type\t(0x00000090 "const long int &")
                    DW_AT_data_member_location\t(0x00)

0x00000054:     DW_TAG_class_type
                  DW_AT_name\t("tuple<long int, char>")
                  DW_AT_byte_size\t(0x10)

0x00000058:       DW_TAG_GNU_template_parameter_pack
                    DW_AT_name\t("_Elements")

0x0000005c:         DW_TAG_template_type_parameter
                      DW_AT_type\t(0x00000010 "long int")

0x00000060:         DW_TAG_template_type_parameter
                      DW_AT_type\t(0x00000018 "char")

0x00000068:     DW_TAG_structure_type
                  DW_AT_name\t("array<long int, 2>")
                  DW_AT_byte_size\t(0x10)

0x0000006c:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_Tp")
                    DW_AT_type\t(0x00000010 "long int")

0x00000070:       DW_TAG_template_value_parameter
                    DW_AT_name\t("_Nm")
                    DW_AT_type\t(0x00000010 "long int")
                    DW_AT_const_value\t(2)

0x00000074:     DW_TAG_class_type
                  DW_AT_name\t("vector<long int, std::allocator<long int> >")
                  DW_AT_byte_size\t(0x18)

0x00000078:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_Tp")
                    DW_AT_type\t(0x00000010 "long int")

0x0000007a:     DW_TAG_structure_type
                  DW_AT_name\t("pair<long int *, long int>")
                  DW_AT_byte_size\t(0x10)

0x0000007c:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_T1")
                    DW_AT_type\t(0x000000ae "long int *")

0x0000007e:       DW_TAG_template_type_parameter
                    DW_AT_name\t("_T2")
                    DW_AT_type\t(0x00000010 "long int")

0x00000080:   DW_TAG_structure_type
                DW_AT_name\t("View")
                DW_AT_byte_size\t(0x08)

0x00000084:     DW_TAG_member
                  DW_AT_name\t("p")
                  DW_AT_type\t(0x00000088 "Pt *")
                  DW_AT_data_member_location\t(0x00)

0x00000088:   DW_TAG_pointer_type
                DW_AT_type\t(0x00000014 "Pt")

0x00000090:   DW_TAG_reference_type
                DW_AT_type\t(0x00000094 "const long int")

0x00000094:   DW_TAG_const_type
                DW_AT_type\t(0x00000010 "long int")

0x00000098:   DW_TAG_typedef
                DW_AT_name\t("P")
                DW_AT_type\t(0x00000024 "std::pair<long int, long int>")

0x0000009c:   DW_TAG_reference_type
                DW_AT_type\t(0x00000024 "std::pair<long int, long int>")

0x000000a0:   DW_TAG_namespace
                DW_AT_name\t("std")

0x000000a4:     DW_TAG_structure_type
                  DW_AT_name\t("pair<long int, long int>")
                  DW_AT_declaration\t(true)

0x000000a8:   DW_TAG_reference_type
                DW_AT_type\t(0x00000010 "long int")

0x000000ac:   DW_TAG_pointer_type
                DW_AT_type\t(0x00000018 "char")

0x000000ae:   DW_TAG_pointer_type
                DW_AT_type\t(0x00000010 "long int")

0x000000be:   DW_TAG_subprogram
                DW_AT_name\t("pointers")
                DW_AT_type\t(0x0000007a "std::pair<long int *, long int>")

0x000000c0:   DW_TAG_subprogram
                DW_AT_name\t("one")
                DW_AT_type\t(0x00000024 "t")

0x000000c4:   DW_TAG_subprogram
                DW_AT_name\t("alias")
                DW_AT_type\t(0x00000098 "t")

0x000000c8:   DW_TAG_subprogram
                DW_AT_name\t("later")
                DW_AT_type\t(0x000000a4 "t")

0x000000cc:   DW_TAG_subprogram
                DW_AT_name\t("make<long>")
                DW_AT_type\t(0x00000024 "t")

0x000000d0:   DW_TAG_subprogram
                DW_AT_name\t("make<int>")
                DW_AT_type\t(0x00000098 "t")

0x000000d4:   DW_TAG_subprogram
                DW_AT_name\t("tuple3")
                DW_AT_type\t(0x00000054 "t")

0x000000d8:   DW_TAG_subprogram
                DW_AT_name\t("arr")
                DW_AT_type\t(0x00000068 "t")

0x000000dc:   DW_TAG_subprogram
                DW_AT_name\t("pick")
                DW_AT_type\t(0x0000009c "t")

0x000000e0:   DW_TAG_subprogram
                DW_AT_name\t("user")
                DW_AT_type\t(0x00000038 "t")

0x000000e4:   DW_TAG_subprogram
                DW_AT_name\t("refs")
                DW_AT_type\t(0x00000048 "t")

0x000000e8:   DW_TAG_subprogram
                DW_AT_name\t("view")
                DW_AT_type\t(0x00000080 "t")

0x000000ec:   DW_TAG_subprogram
                DW_AT_name\t("vec")
                DW_AT_type\t(0x00000074 "t")

0x000000f0:   DW_TAG_subprogram
                DW_AT_name\t("number")
                DW_AT_type\t(0x00000010 "t")

0x000000f4:   DW_TAG_subprogram
                DW_AT_name\t("nothing")

0x000000f8:   DW_TAG_subprogram
                DW_AT_name\t("twice<long>")
                DW_AT_type\t(0x00000024 "t")

0x000000fc:   DW_TAG_subprogram
                DW_AT_name\t("twice<int>")
                DW_AT_type\t(0x0000009c "t")

0x00000100:   DW_TAG_subprogram
                DW_AT_name\t("fn")
                DW_AT_type\t(0x00000024 "t")

0x00000104:   DW_TAG_subprogram
                DW_AT_name\t("operator()")
                DW_AT_type\t(0x00000024 "t")

0x00000108:   DW_TAG_variable
                DW_AT_name\t("fn")
                DW_AT_type\t(0x00000024 "P")

0x0000010c:   DW_TAG_subprogram
                DW_AT_name\t("use")

0x00000110:     DW_TAG_formal_parameter
                  DW_AT_type\t(0x00000010 "long int")

0x00000114:   DW_TAG_subprogram
                DW_AT_name\t("keep")

0x00000118:     DW_TAG_formal_parameter
                  DW_AT_type\t(0x000000a8 "long int &")

0x0000011c:   DW_TAG_subprogram
                DW_AT_name\t("printf")

0x00000120:     DW_TAG_formal_parameter
                  DW_AT_type\t(0x000000ac "const char *")

0x00000124:     DW_TAG_unspecified_parameters

0x00000128:   DW_TAG_subprogram
                DW_AT_name\t("over")

0x0000012c:     DW_TAG_formal_parameter
                  DW_AT_type\t(0x00000010 "long int")

0x00000130:   DW_TAG_subprogram
                DW_AT_name\t("over")

0x00000134:     DW_TAG_formal_parameter
                  DW_AT_type\t(0x000000a8 "long int &")

0x00000138:   DW_TAG_subprogram
                DW_AT_name\t("member")

0x0000013c:     DW_TAG_formal_parameter
                  DW_AT_type\t(0x00000088 "Pt *")
                  DW_AT_artificial\t(true)

0x00000140:     DW_TAG_formal_parameter
                  DW_AT_type\t(0x00000010 "long int")

0x00000144:   DW_TAG_subprogram
                DW_AT_name\t("number2")

0x00000148:     DW_TAG_formal_parameter
                  DW_AT_name\t("one")
                  DW_AT_type\t(0x00000010 "long int")

0x0000014c:     DW_TAG_variable
                  DW_AT_name\t("alias")
                  DW_AT_type\t(0x00000010 "long int")
"""


ALLOCATION = """0x0000000b: DW_TAG_compile_unit
              DW_AT_name\t("t.cpp")

0x00000010:   DW_TAG_base_type
                DW_AT_name\t("unsigned long")
                DW_AT_encoding\t(DW_ATE_unsigned)
                DW_AT_byte_size\t(0x08)

0x00000014:   DW_TAG_pointer_type

0x00000020:   DW_TAG_subprogram
                DW_AT_name\t("operator new")
                DW_AT_type\t(0x00000014 "void *")
                DW_AT_low_pc\t(0x0000000000001000)

0x00000024:     DW_TAG_formal_parameter
                  DW_AT_type\t(0x00000010 "std::size_t")

0x00000028:     DW_TAG_formal_parameter
                  DW_AT_type\t(0x00000014 "void *")

0x00000030:   DW_TAG_subprogram
                DW_AT_name\t("operator delete")
                DW_AT_low_pc\t(0x0000000000001000)

0x00000034:     DW_TAG_formal_parameter
                  DW_AT_type\t(0x00000014 "void *")

0x00000038:     DW_TAG_formal_parameter
                  DW_AT_type\t(0x00000014 "void *")

0x00000040:   DW_TAG_structure_type
                DW_AT_name\t("Arena")
                DW_AT_byte_size\t(0x01)

0x00000044:     DW_TAG_subprogram
                  DW_AT_name\t("operator new")
                  DW_AT_type\t(0x00000014 "void *")
                  DW_AT_declaration\t(true)

0x00000048:       DW_TAG_formal_parameter
                    DW_AT_type\t(0x00000010 "std::size_t")

0x00000050:   DW_TAG_subprogram
                DW_AT_specification\t(0x00000044 "operator new")
                DW_AT_low_pc\t(0x0000000000001000)

0x00000054:     DW_TAG_formal_parameter
                  DW_AT_type\t(0x00000010 "std::size_t")

0x00000060:   DW_TAG_subprogram
                DW_AT_name\t("operator new")
                DW_AT_type\t(0x00000014 "void *")
                DW_AT_declaration\t(true)

0x00000064:     DW_TAG_formal_parameter
                  DW_AT_type\t(0x00000010 "std::size_t")
"""
REPLACED = """
0x00000070:   DW_TAG_subprogram
                DW_AT_name\t("NAME")
                DW_AT_low_pc\t(0x0000000000001000)

0x00000074:     DW_TAG_formal_parameter
                  DW_AT_type\t(0x00000010 "std::size_t")
"""


def test_a_replaced_operator_new_is_told_from_the_build():
    # Review of #368, round 3: `std::allocator` calls the global `operator new`, which a
    # program may replace (`void *operator new(std::size_t n) { captured = p; ... }`) to
    # keep where each element of a container is.
    from peeky.native import _parse_dies, allocation_replaced

    # The placement forms the standard headers define, a class's own, a declaration.
    assert allocation_replaced(_parse_dies(ALLOCATION)) is False
    for name in ("operator new", "operator new []", "operator delete", "operator delete[]"):
        extra = REPLACED.replace("NAME", name)
        assert allocation_replaced(_parse_dies(ALLOCATION + extra)) is True, name


def test_a_calls_arguments_are_read_from_the_build():
    # Review of #368: a binding's name passed to a call is proven only where every function
    # of the call's name takes that argument by value, as a number or a character.
    from peeky.native import _parse_dies, call_arguments

    dies = _parse_dies(CALLS)
    arguments = call_arguments(dies)
    assert arguments["use"] == [((True,), False)]
    assert arguments["keep"] == [((False,), False)]
    # A pointer is no number: only `printf`'s `...` takes a name by value.
    assert arguments["printf"] == [((False,), True)]
    assert arguments["over"] == [((True,), False), ((False,), False)]
    assert arguments["member"] == [((True,), False)]
    assert arguments["fn"] is None and arguments["first"] is None
    assert arguments["one"] == [((), False)]


def test_the_worker_lists_a_maps_later_pages(tmp_path):
    # #297: a map's entries are listed in pages, for a binding's element to be found.
    from peeky.worker import DebugWorker

    worker = DebugWorker(tmp_path)
    worker.stop_id = "stop1"
    worker.dap = Listing({5: [{"name": "[64]", "value": "1"}]})
    worker.reset_previews()
    assert worker.preview_fetch(5, 1, start=64) == [{"name": "[64]", "value": "1"}]
    assert worker.dap.requests[-1][1] == {"variablesReference": 5, "start": 64, "count": 1}
    with pytest.raises(ValueError):
        worker.preview_fetch(5, 1, start=-1)


def test_an_unordered_container_says_why_no_node_is_read_at_this_stop_and_where_it_reads():
    # Eval 13 prep, findings 2 and 3: below a destructor, or through a reference Peeky
    # can't place (lldb-dap 18), the row gave the debugger's miscount alone and said to
    # print the entries from the program, though they read in the frame that holds them,
    # or once the call returns.
    from peeky.native import NodesUnsettled, Withholding, nodes_withheld

    session = NativeSession.__new__(NativeSession)
    withheld = Withholding(unlisted_reason(0, 3), UNLISTED, "refused")
    withheld.counted, withheld.misread, withheld.name = (
        ("on_hand", 3),
        "the debugger counts 0 of its 3 entries",
        "on_hand",
    )
    one = lambda: 1  # noqa: E731
    session.library_call = lambda *_: ("lifetime", "Movement::~Movement()")
    found = session.unsettled_nodes({"id": 1}, False, [], threads=one)
    assert isinstance(found, NodesUnsettled), found
    said = nodes_withheld(withheld, found.why, found.advice)
    assert said == (
        "the debugger counts 0 of its 3 entries, and Peeky doesn't read them from its nodes "
        "while a constructor, destructor or assignment operator below this frame "
        "(Movement::~Movement) may be part-way through changing it; retry after that call "
        "returns"
    )
    assert said.label == UNLISTED and said.refusal.startswith("`on_hand` cannot be changed: ")
    session.library_call = lambda *_: ("between", "std::vector<int, std::allocator<int> >::clear()")
    found = session.unsettled_nodes({"id": 1}, False, [], threads=one)
    assert "a standard-library call below this frame (std::vector<…>::clear)" in found.why
    session.library_call = lambda *_: None
    assert session.unsettled_nodes({"id": 1}, False, [], threads=one) is None
    found = session.unsettled_nodes({"id": 1}, False, [], threads=lambda: 2)
    assert found.why.endswith(
        "the program runs 2 threads, and a stop pauses every other one "
        "wherever it is, perhaps part-way through changing it"
    ), found.why
    # Through a reference: candidate locals to inspect, which it may refer to; nothing
    # proves which (#393 review).
    session.lldb_major, readable = 18, []
    session._nodes_readable = lambda frame, steps, count: readable.append(count) or True
    way = {"frame": {"id": 1}, "steps": [("stock", "Stock &")]}
    said = session.unreached_container(
        withheld, "stock", "reference", [(1, "main", "on_hand")], **way
    )
    assert readable == [3], readable
    assert said == (
        "the debugger counts 0 of its 3 entries, and Peeky reads its nodes only through a "
        "reference it proves refers to a live object, not `stock`; inspect this candidate "
        "local, `on_hand` in main (`--frame 1`): it may be what `stock` refers to"
    )
    several = [(1, "main", "on_hand"), (2, "load", "spare"), (3, "f", "more")]
    said = session.unreached_container(withheld, "stock", "reference", several, **way)
    assert said.endswith(
        "inspect these candidate locals, `on_hand` in main (`--frame 1`), `spare` in load "
        "(`--frame 2`), and others: one may be what `stock` refers to"
    ), said
    # Long names are each cut; the hedge stays whole, within 300 characters.
    long = [(1, "f" * 90, "l" * 90), (2, "g" * 90, "m" * 90)]
    said = session.unreached_container(withheld, "s" * 90, "reference", long, **way)
    assert len(said) <= 300, said
    assert said.endswith(", and others: one may be what `" + "s" * 11 + "…` refers to"), said
    said = session.unreached_container(withheld, "p", "pointer", [], **way)
    assert said.endswith(
        "a pointer it proves points at a live object, not `p`; print its entries from the program"
    ), said
    # Nothing more is said where no node would be read through a proven one either: its
    # own count unread, or entries of a type Peeky doesn't read (review: a `double`).
    session._nodes_readable = lambda frame, steps, count: False
    assert session.unreached_container(withheld, "p", "pointer", [], **way) is withheld
    withheld.counted = None
    assert session.unreached_container(withheld, "p", "pointer", [], **way) is withheld
    # Within 300 characters, however long the call's name.
    withheld.counted = ("on_hand", 3)
    session.library_call = lambda *_: ("lifetime", "N::" + "x" * 200 + "::~X()")
    found = session.unsettled_nodes({"id": 1}, False, [], threads=one)
    assert len(nodes_withheld(withheld, found.why, found.advice)) <= 300


def test_a_refused_change_says_where_a_captures_or_a_bindings_value_is():
    # Eval 13: a GCC lambda's capture by value was refused as "`tariff` is const", while its
    # rows say `[not settable: capture]`; a structured binding's refusal said to change it
    # "before the call", where no call passed it.
    from peeky.native import _binding_refused, capture_refused

    routed = capture_refused("tariff", ("main", "price", "price.__tariff.cents_per_kwh"))
    assert routed.startswith("`tariff` is a capture of this lambda, so it is reached through")
    assert routed.endswith(
        "Change the lambda's own copy instead, at a stop in main once the lambda is made: "
        "`price.__tariff.cents_per_kwh` is settable there, and is what the lambda reads where "
        "the code calls `price` itself"
    )
    owned = capture_refused("over_peak")
    assert owned.endswith(
        "Change the value where it is owned: Peeky changes only values the frame holds itself"
    )
    ranged = _binding_refused("cars", "by_hour", True)
    assert ranged == (
        "`cars` is a structured binding by reference to an element of `by_hour`, and Peeky "
        "changes no value through a reference: change what fills `by_hour`, before the loop, or "
        "edit the source"
    )
    assert "change `p` itself where it is settable" in _binding_refused("b", "p", False)
    assert "change what it binds before this line" in _binding_refused("t", "make(0)[0]", False)
    for why, hint in (
        (routed, "Change it where the message says"),
        (owned, "Change it where the message says"),
        (ranged, "Change what the binding refers to"),
    ):
        error = classify(
            ValueError(
                f"`x` cannot be changed at a.cpp:3, at any stop of this build: in main, {why}. Refused before the program runs: nothing ran"
            )
        )
        assert error["code"] == "MUTATION_REJECTED" and error["hint"].startswith(hint), error
        assert "call" not in error["hint"] and len(error["hint"]) <= 150, error


def test_describe_marks_static_only_a_local_at_one_fixed_address():
    # Review of #393: any location naming `DW_OP_addr` was static, so a location list that
    # places a local at an address for some code and in a register for the rest was too.
    from peeky.native import _static_names

    def die(tag, parent=None, children=(), **attrs):
        return {"tag": tag, "parent": parent, "attrs": attrs, "children": list(children)}

    dies = {
        1: die("DW_TAG_subprogram", None, (2, 3, 4, 5, 6)),
        2: die("DW_TAG_variable", 1, DW_AT_name='"now"', DW_AT_location="DW_OP_addr 0x4010"),
        3: die(
            "DW_TAG_variable",
            1,
            DW_AT_name='"mixed"',
            DW_AT_location="[0x1000, 0x1010): DW_OP_addr 0x4018\n[0x1010, 0x1020): DW_OP_reg0 W0",
        ),
        4: die("DW_TAG_variable", 1, DW_AT_name='"zero"', DW_AT_location="DW_OP_addr 0x0"),
        5: die(
            "DW_TAG_variable",
            1,
            DW_AT_name='"tls"',
            DW_AT_location="DW_OP_addr 0x8, DW_OP_GNU_push_tls_address",
        ),
        6: die("DW_TAG_variable", 1, DW_AT_name='"seen"', DW_AT_location="DW_OP_fbreg -20"),
    }
    assert _static_names(dies, [1]) == ["now"]
