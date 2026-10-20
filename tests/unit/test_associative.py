"""C++ maps and sets: `[N]` is never LLDB's Nth entry (#119).

LLDB lists a map's or set's entries as [0], [1], ... in iteration order, where C++'s
`m[k]` is the value for key k. These scripted listings are shaped as lldb-dap shows
them (libc++ and libstdc++ alike); the real builds are in tests/native.
"""

import pytest

from peeky.probe import Probe, _children, _resolve, parse_selector


class Lldb:
    """A scripted worker serving each handle's children 64 at a time (or `count`), with
    the worker's node budget, counting the expansions."""

    def __init__(self, tree, budget=512):
        self.tree, self.budget, self.expands = tree, budget, 0

    def dispatch(self, operation, args):
        assert operation == "expand"
        self.expands += 1
        if self.budget <= 0:
            raise ValueError("Inspection node budget exhausted; inspect again")
        children = self.tree[args["handle"]]
        start = args.get("start", 0)
        page = children[start : start + min(args.get("count", 64), self.budget)]
        self.budget -= len(page)
        return {
            "variables": page,
            "truncated": start + len(page) < len(children),
            "remaining_nodes": self.budget,
        }


def entries(handle, pairs, kind="std::pair<const int, int>", value_type="int"):
    """{handle: entries, each entry's handle: [first, second]} for a map's pairs."""
    tree = {handle: []}
    for position, (key, value) in enumerate(pairs):
        child = handle * 1000 + position + 1
        tree[handle].append(
            {
                "name": f"[{position}]",
                "type": kind,
                "value": f"{kind} @ 0x1",
                "variablesReference": child,
            }
        )
        key_type = kind.removeprefix("std::pair<").split(",")[0]
        tree[child] = [
            {"name": "first", "type": key_type, "value": key, "variablesReference": 0},
            {"name": "second", "type": value_type, "value": value, "variablesReference": 0},
        ]
    return tree


def elements(handle, values, kind="int"):
    return {
        handle: [
            {"name": f"[{i}]", "type": kind, "value": value, "variablesReference": 0}
            for i, value in enumerate(values)
        ]
    }


def local(name, kind, handle, size, **more):
    return {
        "name": name,
        "type": kind,
        "value": f"size={size}",
        "variablesReference": handle,
        **more,
    }


def snapshot(*variables, whole=True):
    """A stop's locals. ``whole``: the worker says LLDB's std::string summaries show all
    of each string (libc++)."""
    shown = {"scopes": [{"name": "Locals", "variables": list(variables)}]}
    return {**shown, "whole_string_summaries": True} if whole else shown


# The issue's map, `std::map<int, int> m{{2, 20}, {1, 10}, {5, 50}}`, as LLDB lists it.
MAP = local("m", "std::map<int, int>", 1, 3)
TREE = {
    **entries(1, [("1", "10"), ("2", "20"), ("5", "50")]),
    **entries(2, [("5", "50"), ("1", "10"), ("2", "20")]),
    **elements(3, ["1", "2", "3"]),
    **entries(4, [("1", "10"), ("1", "11"), ("2", "20")]),
    **elements(5, ["7", "8", "9"]),
    **entries(
        6,
        [('"a"', "1"), ('"b\\"q"', "2")],
        kind="std::pair<const std::basic_string<char>, int>",
    ),
    **entries(7, [("0.5", "1"), ("1.5", "2")], kind="std::pair<const double, int>"),
    **entries(8, [("1", "10"), ("2", "20")], kind="std::pair<const int, int>"),
}
LOCALS = snapshot(
    MAP,
    local("um", "std::unordered_map<int, int>", 2, 3),
    local("s", "const std::set<int> &", 3, 3),
    local("mm", "std::__1::multimap<int, int>", 4, 3),
    local("v", "std::vector<int>", 5, 3),
    local("sm", "std::map<std::string, int>", 6, 2),
    local("dm", "std::map<double, int>", 7, 2),
    # Typedefs: one the build's debug information resolves, one it does not.
    local("im", "Index", 8, 2, aliased_type="std::map<int, int, std::__1::less<int> >"),
    local("ux", "Index", 8, 2),
    local("is", "IntSet", 3, 3, aliased_type="std::set<int, std::less<int>, std::allocator<int> >"),
    {
        "name": "mp",
        "type": "std::map<int, int> *",
        "value": "0x16b8 size=3",
        "variablesReference": 1,
    },
)


def read(selector, tree=TREE, scopes=LOCALS, pointers=True):
    return _resolve(Lldb(tree), {}, scopes, parse_selector(selector), pointers)


def refused(selector, tree=TREE, scopes=LOCALS, pointers=True):
    with pytest.raises(ValueError) as caught:
        read(selector, tree, scopes, pointers)
    return str(caught.value)


@pytest.mark.parametrize(
    "selector, value",
    [
        # In C++, m[1] is the value for key 1 (10), never the second entry's (20).
        ("m[1]", "10"),
        ("m[2]", "20"),
        ("m[5]", "50"),
        ("um[1]", "10"),
        ("um[5]", "50"),
        ('sm["a"]', "1"),
        ("sm['a']", "1"),
        ("im[1]", "10"),
        # Through a pointer, as C++'s (*mp)[1].
        ("mp[0][1]", "10"),
        # [#N] is the Nth entry in iteration order: a pair, or a set's element.
        ("m[#1].first", "2"),
        ("m[#1].second", "20"),
        ("um[#0].first", "5"),
        ("mm[#1].second", "11"),
        ("s[#1]", "2"),
        ("is[#2]", "3"),
        ("ux[#0].second", "10"),
        ("mp[0][#2].second", "50"),
        # Sequences index as before.
        ("v[1]", "8"),
    ],
)
def test_a_maps_index_is_its_key_and_a_hash_its_position(selector, value):
    assert read(selector)["value"] == value


@pytest.mark.parametrize(
    "selector, message",
    [
        ("m[3]", "[3] not found: m has no key 3 (keys: 1, 2, 5); in C++, m[3] would insert"),
        ("m[-1]", "m has no key -1 (keys: 1, 2, 5)"),
        ("m[#3]", "[#3] not found: m has 3 entries: m[#0] to m[#2]"),
        ("s[1]", "s (const std::set<int> &) is a C++ set, which has no [] in C++"),
        ("s[1]", "select an element by position with s[#N]"),
        ("is[0]", "is (IntSet) is a C++ set"),
        ("mm[1]", "a C++ multimap, which has no [] in C++ (a key can repeat)"),
        ("mm['a']", "mm[#N]"),
        # An unresolved typedef listing pairs with a const key: a key or a position?
        ("ux[1]", "under a type name the build's debug information does not resolve"),
        ("sm[0]", 'sm\'s keys are strings; select one as sm["key"]'),
        ("m['a']", "m's keys are int; select one as m[N]"),
        ('sm["zz"]', 'sm has no key "zz" (keys: "a", "b\\"q")'),
        ('sm["b\\"q"]', "Peeky compares a string key only when it is printable ASCII"),
        ("dm[0]", "dm's keys are double, which Peeky does not compare"),
        ("v[#1]", "[#N] is the Nth entry of a C++ map or set, and v (std::vector<int>) is not"),
        ("mp[#0]", "mp (std::map<int, int> *) is a pointer; mp[0][#0] is that entry"),
        ("mp[1]", "read mp[0][1], its value for key 1"),
        # The value read by key is C++'s m[1], an int: the hint names the entry.
        ("m[1].second", "m[1] is the value for that key, as in C++ (an entry's key and value are"),
    ],
)
def test_what_cxx_would_not_read_is_refused_with_what_to_select(selector, message):
    assert message in refused(selector)


def test_positions_are_only_for_cxx_and_never_for_python_or_java():
    message = refused("m[#0]", pointers=False)
    assert "[#N] is the Nth entry of a C++ map or set" in message


def test_a_key_outside_its_types_range_is_not_converted():
    shorts = {**entries(9, [("1", "10")], kind="std::pair<const short, int>")}
    scopes = snapshot(local("sh", "std::map<short, int>", 9, 1))
    assert "70000 is outside the range of sh's key type" in refused("sh[70000]", shorts, scopes)
    unsigned = {**entries(9, [("1", "10")], kind="std::pair<const unsigned int, int>")}
    scopes = snapshot(local("u", "std::map<unsigned int, int>", 9, 1))
    assert "outside the range" in refused("u[-1]", unsigned, scopes)
    assert read("u[1]", unsigned, scopes)["value"] == "10"


def test_a_key_shown_as_anything_but_a_plain_integer_is_not_compared():
    odd = {**entries(9, [("1", "10"), ("<error: unreadable>", "20")])}
    scopes = snapshot(local("m", "std::unordered_map<int, int>", 9, 2))
    assert read("m[1]", odd, scopes)["value"] == "10"
    assert "shows the key of m[#1] as '<error: unreadable>'" in refused("m[2]", odd, scopes)


def test_an_empty_map_has_no_key():
    scopes = snapshot(local("m", "std::map<int, int>", 9, 0))
    assert "m is empty; in C++, m[1] would insert" in refused("m[1]", {9: []}, scopes)


def test_a_map_listed_by_its_own_members_selects_no_entry():
    raw = {9: [{"name": "__tree_", "type": "tree", "value": "{}", "variablesReference": 0}]}
    scopes = snapshot(local("m", "std::map<int, int>", 9, 1))
    assert "shows m's own members (__tree_, ...), not its entries" in refused("m[#0]", raw, scopes)
    assert "not its entries" in refused("m[1]", raw, scopes)


def test_a_map_listing_fewer_entries_than_its_size_never_says_absent():
    scopes = snapshot(local("m", "std::unordered_map<int, int>", 1, 4))
    assert "the debugger lists 3 of m's 4 entries" in refused("m[7]", TREE, scopes)


@pytest.mark.parametrize(
    "kind, order",
    [
        ("std::map<int, int>", 1),
        ("std::map<int, int, std::greater<int> >", -1),
        ("std::__1::map<int, int, std::__1::less<int>, std::__1::allocator<int> >", 1),
        ("std::map<int, int, ByLastDigit>", None),
        ("std::unordered_map<int, int>", None),
    ],
)
def test_a_sorted_map_is_bisected_and_any_other_read_whole(kind, order):
    size = 400
    keys = [i * 2 for i in range(size)]
    if order == -1:
        keys.reverse()
    pairs = [(str(k), str(k + 1)) for k in keys]
    tree = entries(9, pairs)
    scopes = snapshot(local("m", kind, 9, size))
    if not order:
        # Read entry by entry, 400 entries and their keys exceed one inspection's 512
        # nodes: refused before any is read, wherever the key is.
        for selector in ("m[398]", "m[3]", "m[0]"):
            worker = Lldb(tree)
            with pytest.raises(ValueError, match="beyond the bounded inspection window"):
                _resolve(worker, {}, scopes, parse_selector(selector), True)
            assert worker.expands < 5, worker.expands
        return
    worker = Lldb(tree)
    found = _resolve(worker, {}, scopes, parse_selector("m[398]"), True)
    assert found["value"] == "399"
    # Bisection: a handful of entries, never the 2 x 400 nodes a full read takes.
    assert worker.expands < 40, worker.expands
    worker = Lldb(tree)
    with pytest.raises(ValueError, match="m has no key 3 .its 400 keys are in"):
        _resolve(worker, {}, scopes, parse_selector("m[3]"), True)
    assert worker.expands < 40, worker.expands


def test_a_maps_children_are_listed_by_key_and_a_sets_by_position():
    worker = Lldb(TREE)
    shown = _children(worker, {}, "m", MAP, pointers=True)
    assert [(c["selector"], c["type"], c["value"]) for c in shown["children"]] == [
        ("m[1]", "int", "10"),
        ("m[2]", "int", "20"),
        ("m[5]", "int", "50"),
    ]
    assert "m[K] is the value for key K, as in C++" in shown["note"]
    for name, selectors in (
        ("s", ["s[#0]", "s[#1]", "s[#2]"]),
        ("mm", ["mm[#0]", "mm[#1]", "mm[#2]"]),
        ("ux", ["ux[#0]", "ux[#1]"]),
        ("dm", ["dm[#0]", "dm[#1]"]),
        ("sm", ['sm["a"]', "sm[#1]"]),
        ("mp", ["mp[0][1]", "mp[0][2]", "mp[0][5]"]),
        ("v", ["v[0]", "v[1]", "v[2]"]),
    ):
        variable = next(v for v in LOCALS["scopes"][0]["variables"] if v["name"] == name)
        shown = _children(Lldb(TREE), {}, name, variable, pointers=True, whole_strings=True)
        assert [child["selector"] for child in shown["children"]] == selectors, shown
        # Every selector listed reads the value its row shows.
        for child in shown["children"]:
            assert read(child["selector"])["value"] == child["value"], child
    shown = _children(Lldb(TREE), {}, "dm", LOCALS["scopes"][0]["variables"][6], pointers=True)
    assert "keys Peeky does not compare" in shown["note"]


def test_a_vector_of_const_pairs_is_a_sequence():
    pairs = entries(9, [("1", "10"), ("2", "20")])
    scopes = snapshot(local("vp", "std::vector<std::pair<const int, int> >", 9, 2))
    assert read("vp[1].second", pairs, scopes)["value"] == "20"


@pytest.mark.parametrize(
    "text, segments",
    [("m[#0]", ["m", "[#0]"]), ("m[#12].first", ["m", "[#12]", "first"])],
)
def test_positional_entries_parse(text, segments):
    assert parse_selector(text) == segments


@pytest.mark.parametrize("text", ["m[#]", "m[#-1]", "m[# 1]", "m[#a]", "m#[0]"])
def test_malformed_positional_entries_are_rejected(text):
    with pytest.raises(ValueError, match="a\\[#0\\] is a C\\+\\+ map's or set's first entry"):
        parse_selector(text)


def test_a_maps_entries_cannot_be_set():
    from peeky.errors import Refused

    with pytest.raises(Refused, match="map and dict entries cannot be changed"):
        Probe(program="m.cpp", line=3, capture=(), set=(("m[#0]", "1"),))


@pytest.mark.parametrize(
    "entry, map_like",
    [
        ("std::pair<const int, int>", True),
        # GCC's debug information puts the qualifier after the type.
        ("std::pair<int const, int>", True),
        ("std::pair<const std::basic_string<char>, Point>", True),
        ("std::__1::__value_type<int, int>", True),
        ("std::pair<int, int>", False),
        ("std::pair<int, const int>", False),
        ("int", False),
    ],
)
def test_an_unresolved_typedef_listing_map_entries_is_neither_keyed_nor_indexed(entry, map_like):
    tree = entries(9, [("1", "10"), ("2", "20")], kind=entry)
    tree[9][1]["type"] = entry
    scopes = snapshot(local("t", "Registry::Table", 9, 2))
    if map_like:
        assert "may be a map read by key" in refused("t[1]", tree, scopes)
        assert read("t[#1]", tree, scopes)["value"] == f"{entry} @ 0x1"
    else:
        assert read("t[1]", tree, scopes)["value"] == f"{entry} @ 0x1"
        assert "is not one; index a sequence with [N]" in refused("t[#1]", tree, scopes)


def test_a_pointer_to_a_typedef_reads_by_the_class_behind_it():
    pointer = {
        "name": "isp",
        "type": "const IntSet *",
        "value": "0x16b8 size=3",
        "variablesReference": 3,
        "pointee_aliased_type": "std::set<int, std::less<int>, std::allocator<int> >",
    }
    scopes = snapshot(pointer)
    assert "is a C++ set" in refused("isp[0][1]", TREE, scopes)
    assert read("isp[0][#1]", TREE, scopes)["value"] == "2"
    assert "its entries are isp[0][#N]" in refused("isp[1]", TREE, scopes)
    shown = _children(Lldb(TREE), {}, "isp", pointer, pointers=True)
    assert [c["selector"] for c in shown["children"]] == ["isp[0][#0]", "isp[0][#1]", "isp[0][#2]"]


def strings(handle, keys, depth_limited=False):
    """A std::map<std::string, int> as lldb-dap shows libstdc++'s: each key's summary is
    its C string, up to the first NUL, and `_M_string_length` its length."""
    tree = {handle: []}
    for position, (text, length, value) in enumerate(keys):
        child = handle * 1000 + position + 1
        kind = "std::pair<const std::basic_string<char>, int>"
        tree[handle].append(
            {"name": f"[{position}]", "type": kind, "value": kind, "variablesReference": child}
        )
        string = 0 if depth_limited else child * 10
        tree[child] = [
            {
                "name": "first",
                "type": "const std::basic_string<char>",
                "value": f'"{text}"',
                "variablesReference": string,
                **({"depth_limited": True} if depth_limited else {}),
            },
            {"name": "second", "type": "int", "value": value, "variablesReference": 0},
        ]
        if string:
            tree[string] = [
                {
                    "name": "_M_dataplus",
                    "type": "_Alloc_hider",
                    "value": "",
                    "variablesReference": 0,
                },
                {"name": "_M_string_length", "type": "size_type", "value": str(length)},
                {"name": "", "type": "(anonymous union)", "value": "", "variablesReference": 0},
            ]
    return tree


# Review of #119 (PR #127): {"a\0b": 3, "zz": 9}, {"a": 1, "a\0": 2}.
NULS = {**strings(9, [("a", 3, "3"), ("zz", 2, "9")]), **strings(8, [("a", 1, "1"), ("a", 2, "2")])}
LIBSTDCXX = snapshot(
    local("sso", "std::map<std::string, int>", 9, 2),
    local("both", "std::map<std::string, int>", 8, 2),
    whole=False,
)


def test_a_string_key_matches_only_a_string_of_its_own_length():
    # libstdc++'s summary of "a\0b" is "a": `sso["a"]` read 3 where C++ has no key "a".
    message = refused('sso["a"]', NULS, LIBSTDCXX)
    assert 'sso has no key "a" (keys: "a" (3 bytes), "zz"; each as the debugger shows' in message
    assert read('sso["zz"]', NULS, LIBSTDCXX)["value"] == "9"
    assert read('both["a"]', NULS, LIBSTDCXX)["value"] == "1"
    shown = _children(Lldb(NULS), {}, "both", LIBSTDCXX["scopes"][0]["variables"][1], True)
    assert [c["selector"] for c in shown["children"]] == ['both["a"]', "both[#1]"]


def test_a_string_key_whose_length_cannot_be_read_is_refused():
    deep = strings(9, [("k", 3, "2"), ("k", 1, "1")], depth_limited=True)
    message = refused(
        'm["k"]', deep, snapshot(local("m", "std::map<std::string, int>", 9, 2), whole=False)
    )
    assert "cannot read this key's length" in message and "m[#N]" in message
    variable = local("m", "std::map<std::string, int>", 9, 2)
    shown = _children(Lldb(deep), {}, "m", variable, True)
    assert [c["selector"] for c in shown["children"]] == ["m[#0]", "m[#1]"]
    # Where the worker says the summaries show whole strings (libc++: "k\0x" is shown so),
    # they are compared as they are.
    libcxx = strings(9, [("k", 1, "1"), ("k\\0x", 3, "2")], depth_limited=True)
    whole = snapshot(variable, whole=True)
    assert read('m["k"]', libcxx, whole)["value"] == "1"
    shown = _children(Lldb(libcxx), {}, "m", variable, True, whole_strings=True)
    assert [c["selector"] for c in shown["children"]] == ['m["k"]', "m[#1]"]
    # Two entries showing one key are never both given its label.
    shown = _children(Lldb(deep), {}, "m", variable, True, whole_strings=True)
    assert [c["selector"] for c in shown["children"]] == ["m[#0]", "m[#1]"]


def test_libcxx_string_members_say_the_summary_is_whole():
    from peeky.probe import _string_bytes

    libcxx = Lldb(
        {5: [{"name": "", "type": "(anonymous struct)", "value": "", "variablesReference": 0}]}
    )
    first = {
        "name": "first",
        "type": "const std::basic_string<char>",
        "value": '"a"',
        "variablesReference": 5,
    }
    assert _string_bytes(libcxx, {}, first, False) is True
    old_abi = Lldb(
        {5: [{"name": "_M_dataplus", "type": "_Alloc_hider", "value": "", "variablesReference": 0}]}
    )
    assert _string_bytes(old_abi, {}, first, False) is None
    assert _string_bytes(old_abi, {}, {**first, "variablesReference": 0}, True) is True


@pytest.mark.parametrize(
    "kind, why",
    [
        ("std::map<int, int>", None),
        ("std::map<int, int, std::greater<int> >", None),
        ("std::map<int, int, std::less<>, MyAlloc<std::pair<const int, int> > >", None),
        ("std::unordered_map<int, int>", None),
        (
            "std::__1::unordered_map<int, int, std::__1::hash<int>, std::__1::equal_to<int>, "
            "std::__1::allocator<std::__1::pair<const int, int> > >",
            None,
        ),
        ("std::map<int, int, ByAbs>", "comparator (ByAbs) is not std::less or std::greater"),
        (
            "std::unordered_map<std::string, int, LowerHash, LowerEq>",
            "hash or equality (LowerHash, LowerEq) is not std::hash and std::equal_to",
        ),
        ("Holder<int>::M", "comparator (or hash and equality) cannot be told from its type"),
    ],
)
def test_only_the_standard_comparison_says_which_key_cxx_reads(kind, why):
    from peeky.probe import _comparison

    assert _comparison({"type": kind}) == why


def test_a_custom_comparison_never_says_a_key_is_missing():
    # Review of #119: under `ByAbs`, C++'s am[1] reads key -1's -10.
    tree = entries(9, [("-1", "-10"), ("2", "20")])
    scopes = snapshot(local("am", "std::map<int, int, ByAbs>", 9, 2))
    message = refused("am[1]", tree, scopes)
    assert "would insert" not in message
    assert "no key equals 1 exactly (keys of am: -1, 2), but am's comparator (ByAbs)" in message
    assert read("am[-1]", tree, scopes)["value"] == "-10"
    shown = _children(Lldb(tree), {}, "am", scopes["scopes"][0]["variables"][0], True)
    assert "only a key equal to K reads" in shown["note"]
    # A map by a template's name only (`Holder<T>::M`): its comparator is not known.
    scopes = snapshot(local("hm", "Holder<int>::M", 9, 2, aliased_type="std::map"))
    assert "hm's comparator (or hash and equality) cannot be told" in refused("hm[1]", tree, scopes)
    assert read("hm[2]", tree, scopes)["value"] == "20"
