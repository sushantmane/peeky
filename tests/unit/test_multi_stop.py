"""A line that stops several times per pass (#157): where LLDB placed a breakpoint, the
warning, each stop's place, and where rows and the evidence keep it."""

import sqlite3
from types import SimpleNamespace

from peeky import evidence
from peeky.native import breakpoint_locations
from peeky.probe import _table
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
        "--hits and --set-hit count stops, not passes (as do session hit numbers); each hit's "
        "stop (1 to 3"
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
    # lldb-dap 18 places them before the launch slides the program: the first stop's pc
    # names none of them, so they are asked again, once.
    slid = LISTING.replace("0x00000001000", "0x00000001048")
    worker, runtime, asked = _worker(tmp_path, [LISTING, slid])
    placed = {"breakpoints": [{"id": 1, "line": 29, "verified": True}]}
    worker.note_places(runtime, [{"line": 29}], placed)
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
    assert {row["stop"] for row in evidence.run_query(store, "run")["rows"]} == {None}


def test_a_schema_9_store_gains_stops(tmp_path):
    store = tmp_path / "evidence.db"
    probe = {
        "probe": {"program": "a.c", "line": 8, "capture": ["k"], "set": []},
        "status": "completed",
        "hits": 1,
        "rows": [dict(hit=1, phase="observed", line=8, selector="k", value="1")],
    }
    evidence.record(store, probe, language="c")
    connection = sqlite3.connect(store)
    connection.executescript(
        """
        DROP VIEW last_run; DROP VIEW evidence;
        ALTER TABLE captures DROP COLUMN stop;
        CREATE VIEW evidence AS SELECT c.run_id, r.started_at, r.label, r.program,
            c.line, r.status, c.hit, c.phase, c.function, c.selector, c.type, c.value, c.num,
            c.unavailable, c.error, c.view, c.note, c.identity, c.scope, r.args, r.sets,
            r.artifact_manifest_id, r.source_sha256, r.edit IS NOT NULL AS candidate,
            r.language FROM captures c JOIN runs r ON r.run_id = c.run_id;
        CREATE VIEW last_run AS SELECT * FROM evidence
            WHERE run_id = (SELECT MAX(run_id) FROM runs);
        PRAGMA user_version = 9;
        """
    )
    connection.close()
    assert evidence.query(store, "SELECT selector, stop FROM last_run")["rows"] == [
        {"selector": "k", "stop": None}
    ]
    probe["rows"][0].update(stop=2, stops=2)
    evidence.record(store, probe, language="c")
    assert evidence.query(store, "SELECT selector, stop FROM last_run")["rows"] == [
        {"selector": "k", "stop": 2}
    ]
    connection = sqlite3.connect(store)
    assert connection.execute("PRAGMA user_version").fetchone() == (evidence.SCHEMA_VERSION,)
