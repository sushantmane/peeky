"""A line that stops several times per pass (#157): where LLDB placed a breakpoint, the
warning, each stop's place, and where rows and the evidence keep it."""

import sqlite3
from types import SimpleNamespace

import pytest

from peeky import evidence
from peeky.native import breakpoint_locations
from peeky.probe import _by_hit, _table
from peeky.worker import DebugWorker

# lldb-dap's `breakpoint list 1 2 3 4` (Rust's legacy hash, an inlined copy, a C++
# signature with commas and `operator+`), with an unresolved location and another id.
LISTING = """Current breakpoints:
1: file = '/w/src/main.rs', line = 29, exact_match = 0, locations = 3, resolved = 3, hit count = 0
  Names:
    dap

  1.1: where = movavg`movavg::main::hde4c1b8035d61132 + 1000 at main.rs:29:18, address = 0x0000000100000edc, resolved, hit count = 0 
  1.2: where = movavg`movavg::main::hde4c1b8035d61132 + 1008 at main.rs:29:18, address = 0x0000000100000ee4, resolved, hit count = 0 
  1.3: where = movavg`movavg::main::hde4c1b8035d61132 + 1036 at main.rs:29:18, address = 0x0000000100000f00, resolved, hit count = 0 

2: file = '/w/m.c', line = 3, exact_match = 0, locations = 2, resolved = 2, hit count = 0
  2.1: where = m`main + 236 [inlined] inc at m.c:3:70, address = 0x0000000102a1054c 
  2.2: where = m`main + 256 [inlined] inc at m.c:3:70, address = 0x0000000102a10560 

3: file = '/w/v.cpp', line = 7, exact_match = 0, locations = 3, resolved = 2, hit count = 0
  3.1: where = v`operator+(V, V) + 12 at v.cpp:7:5, address = 0x0000000100003f0c, resolved, hit count = 0 
  3.2: where = v`std::map<int, int>::at(int const&) at v.cpp:7:9, address = 0x0000000100004000, resolved, hit count = 0 
  3.3: where = v`gone + 4 at v.cpp:7:9, unresolved, hit count = 0 

4: file = '/w/other.c', line = 9, exact_match = 0, locations = 1, resolved = 1, hit count = 0
  4.1: where = o`f + 4 at other.c:9:3, address = 0x0000000100000004, resolved, hit count = 0 
"""


def test_lldb_lists_each_location_with_the_code_holding_it():
    found = breakpoint_locations(LISTING, {1, 2, 3})
    assert [(f["id"], f["line"], f["address"], f["function"], f["place"]) for f in found] == [
        (1, 29, 0x100000EDC, "movavg::main::hde4c1b8035d61132", "0x100000af4"),
        (1, 29, 0x100000EE4, "movavg::main::hde4c1b8035d61132", "0x100000af4"),
        (1, 29, 0x100000F00, "movavg::main::hde4c1b8035d61132", "0x100000af4"),
        # Each inlined copy is its own place: two copies are two calls.
        (2, 3, 0x102A1054C, "inc", "inlined at 0x102a1054c"),
        (2, 3, 0x102A10560, "inc", "inlined at 0x102a10560"),
        (3, 7, 0x100003F0C, "operator+(V, V)", "0x100003f00"),
        (3, 7, 0x100004000, "std::map<int, int>::at(int const&)", "0x100004000"),
    ]
    # An unresolved location has no address; another breakpoint's are not asked for.
    assert breakpoint_locations("nothing here", {1}) == []
    many = "".join(
        f"  1.{n}: where = a`f + {n} at a.c:3:1, address = 0x{4096 + n:x}, resolved\n"
        for n in range(300)
    )
    # Past the bound a count would fall short: none rather than too few.
    assert breakpoint_locations(many, {1}) is None


class _Dap:
    def __init__(self):
        self.asked = []


def _worker(tmp_path, listings):
    """A worker whose runtime answers `breakpoint list` from ``listings`` in turn."""
    asked = []

    def locations(dap, ids):
        asked.append(sorted(ids))
        return breakpoint_locations(listings[min(len(asked), len(listings)) - 1], set(ids))

    worker = DebugWorker(tmp_path)
    worker.dap = _Dap()
    runtime = SimpleNamespace(breakpoint_locations=locations)
    return worker, runtime, asked


def test_a_line_with_several_locations_in_one_function_warns_and_names_each_stop(tmp_path):
    worker, runtime, asked = _worker(tmp_path, [LISTING])
    placed = {
        "breakpoints": [
            {"id": 1, "line": 29, "verified": True},
            {"id": 2, "line": 3, "verified": True},
            {"id": 4, "line": 9, "verified": False},
        ]
    }
    worker.note_places(runtime, [{"line": 29}, {"line": 3}, {"line": 9}], placed)
    # Only verified breakpoints are asked about.
    assert asked == [[1, 2]]
    (warning,) = worker.place_warnings()
    assert warning.startswith(
        "Line 29 stops up to 3 times per pass in movavg::main (3 breakpoint locations): "
        "--hits, --set-hit and --set-at's @HIT count stops, not passes (as do session hit "
        "numbers); each hit's stop (1 to 3"
    ), warning
    worker.stop_reason = "breakpoint"

    def at(line, pc):
        return worker.place_of({"line": line, "instructionPointerReference": pc})

    assert [at(29, pc) for pc in ("0x100000EDC", "0x100000ee4", "0x100000F00")] == [
        (1, 3),
        (2, 3),
        (3, 3),
    ]
    # An inlined copy's one location, a line with no places: nothing to tell apart.
    assert at(3, "0x102a1054c") is None and at(9, "0x100000004") is None
    # A step's stop is no breakpoint location.
    worker.stop_reason = "step"
    assert at(29, "0x100000edc") is None
    # No breakpoint list (Java, Python): no places, no warning.
    worker.note_places(SimpleNamespace(), [{"line": 29}], placed)
    assert worker.places == {} and worker.place_warnings() == []


def test_locations_placed_before_the_program_loaded_are_asked_again_once(tmp_path):
    # Review of #160: before the program loads, LLDB lists each location at the module's
    # address (`address = movavg[0x…], unresolved`, as `breakpoint list` prints it then),
    # which was left out, so nothing was asked again. The count holds already; the first
    # stop's pc names none of them, so they are asked again, once.
    unloaded = LISTING.replace("address = 0x", "address = movavg[0x").replace(
        ", resolved, hit count", "], unresolved, hit count"
    )
    assert "address = movavg[0x0000000100000edc], unresolved, hit count = 0" in unloaded
    slid = LISTING.replace("0x00000001000", "0x00000001048")
    worker, runtime, asked = _worker(tmp_path, [unloaded, slid])
    placed = {"breakpoints": [{"id": 1, "line": 29, "verified": True}]}
    worker.note_places(runtime, [{"line": 29}], placed)
    assert worker.place_warnings()[0].startswith("Line 29 stops up to 3 times per pass")
    worker.stop_reason = "breakpoint"
    frame = {"line": 29, "instructionPointerReference": "0x104800ee4"}
    assert worker.place_of(frame) == (2, 3) and len(asked) == 2
    assert worker.place_of({**frame, "instructionPointerReference": "0x104800f00"}) == (3, 3)
    # A pc none of them names, even after asking again: no place, no more asks.
    assert worker.place_of({**frame, "instructionPointerReference": "0x200000000"}) is None
    assert len(asked) == 2


def test_the_table_has_a_stop_column_only_where_a_line_has_several(tmp_path):
    base = {
        "rows": [],
        "interventions": [],
        "status": "completed",
        "hits": 2,
        "seconds": 1,
        "warnings": [],
        "output": "",
        "pauses_ms": [],
        "probe": {"hits": "all"},
    }
    row = {"phase": "observed", "line": 29, "function": "main", "selector": "mean", "value": "3"}
    plain = _table({**base, "rows": [{**row, "hit": 1}, {**row, "hit": 2}]})
    assert plain.splitlines()[0].split()[:3] == ["hit", "phase", "line"]
    rows = [{**row, "hit": 1, "stop": 1, "stops": 3}, {**row, "hit": 2, "stop": 2, "stops": 3}]
    shown = _table({**base, "rows": rows}).splitlines()
    assert shown[0].split()[:3] == ["hit", "stop", "phase"]
    assert shown[1].split()[:2] == ["1", "1/3"] and shown[2].split()[:2] == ["2", "2/3"]


def test_the_evidence_keeps_each_hits_stop(tmp_path):
    store = tmp_path / "evidence.db"
    probe = {
        "probe": {"program": "src/main.rs", "line": 29, "capture": ["mean"], "set": []},
        "status": "completed",
        "hits": 3,
        "rows": [
            dict(hit=h, phase="observed", line=29, selector="mean", value=v, stop=s, stops=3)
            for h, s, v in ((1, 1, "3.0"), (2, 2, "3.0"), (3, 1, "4.5"))
        ],
    }
    evidence.record(store, probe, language="rust")
    rows = evidence.query(store, "SELECT hit, stop, value FROM last_run")["rows"]
    assert [(row["hit"], row["stop"]) for row in rows] == [(1, 1), (2, 2), (3, 1)]
    assert [row["stop"] for row in evidence.run_query(store, "run")["rows"]] == [1, 2, 1]
    wide = evidence.run_query(store, "wide")
    assert wide["columns"][:4] == ["hit", "stop", "phase", "line"]
    assert [(row["hit"], row["stop"], row["mean"]) for row in wide["rows"]] == [
        (1, 1, "3.0"),
        (2, 2, "3.0"),
        (3, 1, "4.5"),
    ]
    # A run whose line stops once per pass keeps wide's columns as they were.
    for row in probe["rows"]:
        del row["stop"], row["stops"]
    evidence.record(store, probe, language="rust")
    assert evidence.run_query(store, "wide")["columns"][:3] == ["hit", "phase", "line"]
    # No stop labels: `queries run` leaves the empty column out (eighth evaluation, L7).
    assert "stop" not in evidence.run_query(store, "run")["columns"]


@pytest.mark.parametrize("version", [13, 9])
def test_an_older_store_gains_stops(tmp_path, version):
    # Schema 14 adds captures.stop: a store of #151's schema 13, or of 9 through the chain.
    store = tmp_path / "evidence.db"
    probe = {
        "probe": {"program": "a.c", "line": 8, "capture": ["k"], "set": []},
        "status": "completed",
        "hits": 1,
        "rows": [dict(hit=1, phase="observed", line=8, selector="k", value="1")],
    }
    evidence.record(store, probe, language="c")
    connection = sqlite3.connect(store)
    dropped = ["captures DROP COLUMN stop", "interventions DROP COLUMN stop"]
    if version == 9:
        dropped += ["captures DROP COLUMN instance"] + [
            f"interventions DROP COLUMN {column}"
            for column in ("line", "capture_hit", "function", "instance")
        ]
    instance = "c.instance, " if version == 13 else ""
    connection.executescript(
        "DROP VIEW last_run; DROP VIEW evidence;"
        + "".join(f"ALTER TABLE {change};" for change in dropped)
        + f"""
        CREATE VIEW evidence AS SELECT c.run_id, r.started_at, r.label, r.program,
            c.line, r.status, c.hit, c.phase, c.function, {instance}c.selector, c.type,
            c.value, c.num, c.unavailable, c.error, c.view, c.note, c.identity, c.scope,
            r.args, r.sets, r.artifact_manifest_id, r.source_sha256,
            r.edit IS NOT NULL AS candidate, r.language
            FROM captures c JOIN runs r ON r.run_id = c.run_id;
        CREATE VIEW last_run AS SELECT * FROM evidence
            WHERE run_id = (SELECT MAX(run_id) FROM runs);
        PRAGMA user_version = {version};
        """
    )
    connection.close()
    assert evidence.query(store, "SELECT selector, stop FROM last_run")["rows"] == [
        {"selector": "k", "stop": None}
    ]
    probe["rows"][0].update(stop=2, stops=2)
    evidence.record(store, probe, language="c")
    assert evidence.query(store, "SELECT selector, stop, instance FROM last_run")["rows"] == [
        {"selector": "k", "stop": 2, "instance": None}
    ]
    # interventions.stop too (review 2 of #160).
    assert evidence.query(store, "SELECT COUNT(stop) AS n FROM interventions")["rows"] == [{"n": 0}]
    connection = sqlite3.connect(store)
    assert connection.execute("PRAGMA user_version").fetchone() == (evidence.SCHEMA_VERSION,)
    assert evidence.SCHEMA_VERSION == 18


def test_return_rows_keep_their_hits_stop_and_older_ones_gain_it(tmp_path):
    # #174: a `(return)` row stored no stop (NULL), so `diff` had to infer it from the
    # hit's observed rows. Schema 15 records it, and fills it in for earlier runs.
    store = tmp_path / "evidence.db"
    probe = {
        "probe": {"program": "a.c", "line": 8, "capture": ["k", "return"], "set": []},
        "status": "completed",
        "hits": 2,
        "rows": [
            dict(hit=1, phase="observed", line=8, selector="k", value="1", stop=1, stops=2),
            dict(hit=1, phase="returned", line=12, selector="(return)", value="5", stop=1, stops=2),
            dict(hit=2, phase="returned", line=12, selector="(return)", value="6", stop=2, stops=2),
        ],
    }
    evidence.record(store, probe, language="c")
    stored = "SELECT hit, phase, stop FROM last_run ORDER BY hit, phase"
    expected = [
        {"hit": 1, "phase": "observed", "stop": 1},
        {"hit": 1, "phase": "returned", "stop": 1},
        {"hit": 2, "phase": "returned", "stop": 2},
    ]
    assert evidence.query(store, stored)["rows"] == expected
    # The same run saved by schema 14 (no stop on its `(return)` rows): the upgrade
    # takes each from its hit's other rows; one whose hit has none stays NULL.
    connection = sqlite3.connect(store)
    connection.executescript(
        "UPDATE captures SET stop = NULL WHERE phase = 'returned'; PRAGMA user_version = 14;"
    )
    connection.close()
    expected[2]["stop"] = None
    assert evidence.query(store, stored)["rows"] == expected
    connection = sqlite3.connect(store)
    assert connection.execute("PRAGMA user_version").fetchone() == (evidence.SCHEMA_VERSION,)


def test_an_older_return_gains_no_stop_from_rows_that_disagree(tmp_path):
    # Review of PR #198: the upgrade took MIN(stop) of the hit's rows, so a schema-14
    # hit observed at stop 1 with an after-set row at stop 2 had its return placed at
    # stop 1, a guess. A stop is taken only where every other row has one and all agree.
    store = tmp_path / "evidence.db"

    def row(hit, phase, stop, value="1"):
        selector = "(return)" if phase == "returned" else "k"
        return dict(hit=hit, phase=phase, line=8, selector=selector, value=value) | (
            {"stop": stop, "stops": 2} if stop else {}
        )

    probe = {
        "probe": {"program": "a.c", "line": 8, "capture": ["k", "return"], "set": []},
        "status": "completed",
        "hits": 3,
        "rows": [
            row(1, "observed", 1),
            row(1, "after-set", 2),
            row(1, "returned", None, "5"),
            row(2, "observed", 2),
            row(2, "after-set", 2),
            row(2, "returned", None, "6"),
            row(3, "observed", None),
            row(3, "after-set", 1),
            row(3, "returned", None, "7"),
        ],
    }
    evidence.record(store, probe, language="c")
    connection = sqlite3.connect(store)
    connection.execute("PRAGMA user_version = 14")
    connection.close()
    returned = "SELECT hit, stop FROM last_run WHERE phase = 'returned' ORDER BY hit"
    assert evidence.query(store, returned)["rows"] == [
        {"hit": 1, "stop": None},
        {"hit": 2, "stop": 2},
        {"hit": 3, "stop": None},
    ]


# lldb-dap 18's listing of `format!("[{}]", v)` in an `#[inline(always)]` function that
# `uses_inl` calls twice (review of #160): each copy's offset follows `[inlined] …`.
INLINED = """1: file = '/w/src/main.rs', line = 15, exact_match = 0, locations = 6, resolved = 6, hit count = 0
  1.1: where = r1-e54d9bd28bd92cce`r1::uses_inl::h26e77cb29e85dc8e + 60 [inlined] r1::fmt_inl::h7b23c0d2657465aa at main.rs:15:5, address = 0x0000aaaaaaaa9180, resolved, hit count = 0 
  1.2: where = r1-e54d9bd28bd92cce`r1::uses_inl::h26e77cb29e85dc8e + 72 [inlined] r1::fmt_inl::h7b23c0d2657465aa + 12 at main.rs:15:5, address = 0x0000aaaaaaaa918c, resolved, hit count = 0 
  1.3: where = r1-e54d9bd28bd92cce`r1::uses_inl::h26e77cb29e85dc8e + 92 [inlined] r1::fmt_inl::h7b23c0d2657465aa + 32 at main.rs:15:5, address = 0x0000aaaaaaaa91a0, resolved, hit count = 0 
  1.4: where = r1-e54d9bd28bd92cce`r1::uses_inl::h26e77cb29e85dc8e + 168 [inlined] r1::fmt_inl::h7b23c0d2657465aa at main.rs:15:5, address = 0x0000aaaaaaaa91ec, resolved, hit count = 0 
  1.5: where = r1-e54d9bd28bd92cce`r1::uses_inl::h26e77cb29e85dc8e + 176 [inlined] r1::fmt_inl::h7b23c0d2657465aa + 8 at main.rs:15:5, address = 0x0000aaaaaaaa91f4, resolved, hit count = 0 
  1.6: where = r1-e54d9bd28bd92cce`r1::uses_inl::h26e77cb29e85dc8e + 204 [inlined] r1::fmt_inl::h7b23c0d2657465aa + 36 at main.rs:15:5, address = 0x0000aaaaaaaa9210, resolved, hit count = 0 
"""


def test_each_inlined_copy_is_a_call_of_its_own_with_its_own_stops(tmp_path):
    # Review of #160: each location was taken for a copy of its own, so `format!` inlined
    # twice stopped 3 times per call (6 for 2 calls) with no warning and no stop.
    places = {f["place"] for f in breakpoint_locations(INLINED, {1})}
    assert places == {"inlined at 0xaaaaaaaa9180", "inlined at 0xaaaaaaaa91ec"}
    worker, runtime, _ = _worker(tmp_path, [INLINED])
    placed = {"breakpoints": [{"id": 1, "line": 15, "verified": True}]}
    worker.note_places(runtime, [{"line": 15}], placed)
    # Both copies read alike: said once.
    (warning,) = worker.place_warnings()
    assert warning.startswith("Line 15 stops up to 3 times per pass in r1::fmt_inl (3 breakpoint")
    worker.stop_reason = "breakpoint"
    stops = [
        worker.place_of({"line": 15, "instructionPointerReference": pc})
        for pc in ("0xaaaaaaaa9180", "0xaaaaaaaa918c", "0xaaaaaaaa91a0", "0xaaaaaaaa91f4")
    ]
    assert stops == [(1, 3), (2, 3), (3, 3), (2, 3)]


def test_generic_instances_are_named_apart(tmp_path):
    # Review of #160: two instances of `gener` each warned as `r1::gener`, alike.
    text = "".join(
        f"  1.{n}: where = r1`r1::gener::h{hashed} + {4 * n} at main.rs:8:9, "
        f"address = 0x{base + 4 * n:x}, resolved, hit count = 0\n"
        for hashed, base in (("aaaaaaaaaaaaaaaa", 0x1000), ("bbbbbbbbbbbbbbbb", 0x2000))
        for n in (1, 2, 3)
    )
    worker, runtime, _ = _worker(tmp_path, [text])
    instances = {"aaaaaaaaaaaaaaaa": "r1::gener<i32>", "bbbbbbbbbbbbbbbb": "r1::gener<&str>"}
    worker.runtime = SimpleNamespace(
        instance_name=lambda frame: instances[frame["name"].rsplit("::h", 1)[1]]
    )
    placed = {"breakpoints": [{"id": 1, "line": 8, "verified": True}]}
    worker.note_places(runtime, [{"line": 8}], placed)
    warnings = worker.place_warnings()
    assert [w.split(" (")[0] for w in warnings] == [
        "Line 8 stops up to 3 times per pass in r1::gener<i32>",
        "Line 8 stops up to 3 times per pass in r1::gener<&str>",
    ]
    # Where the build cannot name them, the debugger's whole names tell them apart.
    worker.runtime = None
    assert [w.split(" (")[0].rsplit(" ", 1)[1] for w in worker.place_warnings()] == [
        "r1::gener::haaaaaaaaaaaaaaaa",
        "r1::gener::hbbbbbbbbbbbbbbbb",
    ]


def test_a_listing_from_before_load_never_labels_a_stop(tmp_path):
    # Review 2 of #160: places listed before the program loaded kept the module's
    # addresses, and were used while one matched the stop's pc. Once the program is slid,
    # a pc can equal another location's module address: here the stop is at the slid
    # first location, 0x100000ee4, which is the second's module address.
    unloaded = LISTING.replace("address = 0x", "address = movavg[0x").replace(
        ", resolved, hit count", "], unresolved, hit count"
    )
    slid = (
        LISTING.replace("0x0000000100000f00", "0x0000000100000f08")
        .replace("0x0000000100000ee4", "0x0000000100000eec")
        .replace("0x0000000100000edc", "0x0000000100000ee4")
    )
    assert all(not f["resolved"] for f in breakpoint_locations(unloaded, {1}))
    assert all(f["resolved"] for f in breakpoint_locations(slid, {1}))
    worker, runtime, asked = _worker(tmp_path, [unloaded, slid])
    placed = {"breakpoints": [{"id": 1, "line": 29, "verified": True}]}
    worker.note_places(runtime, [{"line": 29}], placed)
    worker.stop_reason = "breakpoint"
    frame = {"line": 29, "instructionPointerReference": "0x100000ee4"}
    # Asked again at the first stop, though an address matched: the first, not the second.
    assert worker.place_of(frame) == (1, 3) and len(asked) == 2
    # Still unresolved after asking again (a library not loaded yet): never a label.
    worker, runtime, asked = _worker(tmp_path, [unloaded, unloaded])
    worker.note_places(runtime, [{"line": 29}], placed)
    worker.stop_reason = "breakpoint"
    assert worker.place_of(frame) is None and len(asked) == 2
    # Its count still warns before the first stop.
    assert worker.place_warnings()[0].startswith("Line 29 stops up to 3 times per pass")


def test_a_change_says_which_stop_it_was(tmp_path):
    # Review 2 of #160: a --set-at's record said only its line's hit (`9@2`).
    store = tmp_path / "evidence.db"
    probe = {
        "probe": {"program": "src/main.rs", "line": 9, "capture": ["x"], "set": []},
        "status": "completed",
        "hits": 3,
        "rows": [
            dict(hit=h, phase=phase, line=9, selector="x", value=v, stop=h, stops=3)
            for h, phase, v in ((1, "observed", "0"), (2, "observed", "0"), (2, "after-set", "100"))
        ],
        "interventions": [
            {"name": "x", "before": "0", "after": "100", "line": 9, "hit": 2, "capture_hit": 2}
            | {"function": "s1::main::h0123456789abcdef", "at": "9@2", "stop": 2, "stops": 3}
        ],
        "warnings": [],
        "seconds": 1,
        "interventions_note": None,
    }
    evidence.record(store, probe, language="rust")
    stored = evidence.query(store, "SELECT hit, stop FROM interventions")["rows"]
    assert stored == [{"hit": 2, "stop": 2}]
    sets = [row["sets"] for row in evidence.run_query(store, "wide")["rows"] if row["sets"]]
    assert sets == ['["x: 0 -> 100 (line 9, hit 2, stop 2, in s1::main)"]']
    effect = evidence.run_query(store, "set_effect")["rows"]
    assert [(row["hit"], row["stop"]) for row in effect] == [(2, 2)]
    shown = _table({**probe, "rows": [], "pauses_ms": [], "output": ""}).splitlines()
    assert "set x: 0 -> 100  (--set-at line 9@2, stop 2 of 3, in s1::main)" in shown
    # A plain --set at a stop of several says which too.
    plain = {key: value for key, value in probe["interventions"][0].items() if key != "at"}
    shown = _table({**probe, "rows": [], "interventions": [plain], "pauses_ms": [], "output": ""})
    assert "set x: 0 -> 100  (stop 2 of 3)" in shown.splitlines()


def test_wide_gives_a_hit_no_stop_its_rows_disagree_on(tmp_path):
    # Review 2 of PR #198: `queries wide` took MIN(stop) for each hit and phase, so `x`
    # at stop 1 and `y` at stop 2 of one hit showed as stop 1, as did a selector captured
    # twice alike at stops 1 and 2, and a hit with a row at no stop showed the other's.
    store = tmp_path / "evidence.db"

    def row(hit, selector, stop):
        labelled = {"stop": stop, "stops": 2} if stop else {}
        value = "1" if selector == "x" else "2"
        return dict(hit=hit, phase="observed", line=29, selector=selector, value=value) | labelled

    probe = {
        "probe": {"program": "a.c", "line": 29, "capture": ["x", "y"], "set": []},
        "status": "completed",
        "hits": 4,
        "rows": [
            row(1, "x", 1),
            row(1, "y", 1),
            row(2, "x", 1),
            row(2, "y", 2),
            row(3, "x", 1),
            row(3, "x", 2),
            row(3, "y", 1),
            row(4, "x", 2),
            row(4, "y", None),
        ],
    }
    evidence.record(store, probe, language="c")
    wide = evidence.run_query(store, "wide")
    assert [(r["hit"], r["stop"], r["x"], r["y"]) for r in wide["rows"]] == [
        (1, 1, "1", "2"),
        (2, None, "1", "2"),
        (3, None, "1", "2"),
        (4, None, "1", "2"),
    ]
    # A probe's own per-hit summary takes no stop from rows that disagree either.
    by_hit = {entry["hit"]: entry for entry in _by_hit(probe["rows"])}
    assert by_hit[1]["stop"] == 1
    assert not any("stop" in by_hit[hit] or "stops" in by_hit[hit] for hit in (2, 3, 4))
