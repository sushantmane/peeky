"""C++ built by GCC (#167): what LLDB misreads in its debug information is withheld or
said, never shown as the program's. Unordered containers LLDB counts as empty, a
structured binding of a bitfield with no location, and LLDB's own errors about the
debug information, which lldb-dap 18 sends among the program's output."""

import collections
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

0x00000050:   DW_TAG_subprogram
                DW_AT_name\t("main")

0x00000054:     DW_TAG_variable
                  DW_AT_name\t("t")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(14)
                  DW_AT_type\t(0x00000040 "type &&")
                  DW_AT_location\t(DW_OP_fbreg -200)

0x00000058:     DW_TAG_variable
                  DW_AT_name\t("pt")
                  DW_AT_type\t(0x00000044 "type &")
                  DW_AT_location\t(DW_OP_fbreg -192)

0x0000005c:     DW_TAG_variable
                  DW_AT_name\t("lr")
                  DW_AT_type\t(0x00000048 "long int &")
                  DW_AT_location\t(DW_OP_fbreg -184)

0x00000060:     DW_TAG_variable
                  DW_AT_name\t("t")
                  DW_AT_decl_file\t("/w/t.cpp")
                  DW_AT_decl_line\t(20)
                  DW_AT_type\t(0x00000048 "long int &")
                  DW_AT_location\t(DW_OP_fbreg -176)

0x00000064:     DW_TAG_variable
                  DW_AT_name\t("u")
                  DW_AT_type\t(0x00000040 "type &&")
                  DW_AT_location\t(DW_OP_fbreg -168)

0x00000068:     DW_TAG_variable
                  DW_AT_name\t("w")
                  DW_AT_type\t(0x00000040 "type &&")
                  DW_AT_location\t(DW_OP_fbreg -160)
"""


def bound(where=None, shadowed=False):
    """A NativeSession over BOUND's `main` (with ``shadowed``, also its plain `long &t` on
    line 20), the debugger showing the declaration at ``where``."""
    from peeky.fields import CPP_LIBRARIES, local_types, type_graph
    from peeky.native import _parse_dies

    dies = _parse_dies(BOUND)
    types = local_types(dies, [0x50], through=True)
    if not shadowed:
        types["t"] = frozenset(each for each in types["t"] if each[2] == ("t.cpp", 14))
    graph = type_graph(dies, {0x40, 0x44, 0x48}, CPP_LIBRARIES)
    for offset, node in type_graph(dies, {0x28, 0x30}, CPP_LIBRARIES).items():
        graph.setdefault(offset, node)
    session = native(function={"types": types}, fields=graph)
    session._shown_where = lambda name, pc: where
    session._unslid = lambda frame: 0x1000
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
    assert bound(("t.cpp", 14, True), shadowed=True).bound_element(t, frame, fetch)["variable"]
    assert bound(("t.cpp", 20, True), shadowed=True).bound_element(t, frame, fetch) is None
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

    source = "int main() {\n    auto &[t, u] = held[1];\n    long &w = x;\n}\n"
    session = bound()
    plain = {(0x48, "automatic", ("t.cpp", 2))}
    session._function_at = lambda frame: {
        "types": {"t": frozenset(plain), "w": frozenset({(0x48, "automatic", ("t.cpp", 3))})},
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
