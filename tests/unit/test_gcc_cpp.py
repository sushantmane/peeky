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
    UNLISTED,
    NativeSession,
    count_path,
    formatter_count,
    local_buffer,
    raw_count,
    unlisted_reason,
)
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
    # Where the counts agree (clang's, and an empty one), the debugger's listing stands.
    agreeing = Counts({"us._M_h._M_element_count": "0"})
    assert session.unlisted_container(shown, {"id": 1}, [("us", shown["type"])], agreeing) is None
    listed = variable("us", "std::unordered_set<int>", "size=1")
    one = Counts({"us._M_h._M_element_count": "1"})
    assert session.unlisted_container(listed, {"id": 1}, [("us", listed["type"])], one) is None


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


def string_graph(buffer=(16,), offsets=(0, 8, 16), element="char"):
    """fields.type_graph of libstdc++'s `std::string` as GCC describes it."""
    record = ("record", "basic_string<char>", "class", 32, None)
    members = (
        ("_M_dataplus", 2, offsets[0], False, None, None),
        ("_M_string_length", 3, offsets[1], False, None, None),
        (None, 4, offsets[2], False, None, None),
    )
    union = (
        ("_M_local_buf", 5, None, False, None, None),
        ("_M_allocated_capacity", 3, None, False, None, None),
    )
    return {
        1: (*record, members, (2, 3, 4), False, False, "std"),
        3: ("base", "long unsigned int", "DW_ATE_unsigned", 8),
        4: ("record", "", "union", 16, None, union, (5, 3), False, False, "std"),
        5: ("array", "", 6, buffer, False),
        6: ("base", element, "DW_ATE_signed_char", 1),
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
    ):
        assert local_buffer(other, other[1]) is None


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
    assert warning == (
        "The debugger printed 3 lines about the build's debug information, which Peeky left "
        "out of the program's output (debugger_diagnostics in --json): " + DWARF.rstrip("\n")
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
