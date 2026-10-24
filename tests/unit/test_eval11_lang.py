"""The eleventh agent evaluation's language Lows that need no debugger: Rust reference
types in `describe`, the bindings a `for` loop desugars to, the `for` item an element's
note names, a Rust raw pointer's rows in `queries diff`, and a GCC lambda's captures."""

import sqlite3
from types import SimpleNamespace

import pytest

from peeky import evidence
from peeky.native import _type_name
from peeky.probe import _root
from peeky.rust_symbols import _partial_elements


def die(tag, parent=None, children=(), **attrs):
    return {"tag": tag, "parent": parent, "attrs": attrs, "children": list(children)}


RUST_TYPES = {
    1: die("DW_TAG_compile_unit"),
    2: die("DW_TAG_structure_type", 1, DW_AT_name='"Engineer"'),
    3: die("DW_TAG_pointer_type", 1, DW_AT_name='"&rota::Engineer"', DW_AT_type="0x00000002"),
    4: die("DW_TAG_structure_type", 1, DW_AT_name='"&str"'),
    5: die("DW_TAG_pointer_type", 1, DW_AT_name='"&&str"', DW_AT_type="0x00000004"),
    6: die("DW_TAG_pointer_type", 1, DW_AT_name='"*const u32"', DW_AT_type="0x00000007"),
    7: die("DW_TAG_base_type", 1, DW_AT_name='"u32"'),
    8: die("DW_TAG_pointer_type", 1, DW_AT_name='"&mut rota::Engineer"', DW_AT_type="0x00000002"),
    # C's pointers have no name.
    9: die("DW_TAG_pointer_type", 1, DW_AT_type="0x00000007"),
}


@pytest.mark.parametrize(
    "reference, shown",
    [
        ('0x00000003 "rota::Engineer *"', "&rota::Engineer"),
        ('0x00000005 "&str *"', "&&str"),
        ('0x00000006 "u32 *"', "*const u32"),
        ('0x00000008 "rota::Engineer *"', "&mut rota::Engineer"),
        ('0x00000009 "u32 *"', "u32 *"),
    ],
)
def test_describe_spells_a_rust_reference_as_the_program_does(reference, shown):
    # rota 3: `describe` said `e (rota::Engineer *)` and `w (&str *)`, as dwarfdump writes
    # the pointer's type, though the debug information names it `&rota::Engineer`.
    assert _type_name(RUST_TYPES, reference) == shown


MIR = """// MIR for `plan` after PreCodegen

fn plan(_1: &mut [Engineer], _2: u32) -> u32 {
    debug team => _1;                    // in scope 0 at src/main.rs:1:9: 1:13
    debug weeks => _2;                   // in scope 0 at src/main.rs:1:32: 1:37
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:1:45: 1:48
    let mut _3: std::ops::RangeInclusive<u32>; // in scope 0 at src/main.rs:3:17: 3:26
    let mut _5: u32;                     // in scope 0 at src/main.rs:3:9: 3:13
    let mut _6: std::iter::Enumerate<std::slice::Iter<'_, Engineer>>; // in scope 0 at src/main.rs:4:23: 4:46
    let _7: usize;                       // in scope 0 at src/main.rs:4:14: 4:15
    let _8: &Engineer;                   // in scope 0 at src/main.rs:4:17: 4:18
    let _9: usize;                       // in scope 0 at src/main.rs:8:9: 8:13
    scope 1 {
        debug iter => _3;                // in scope 1 at src/main.rs:3:17: 3:26
        scope 2 {
            debug week => _5;            // in scope 2 at src/main.rs:3:9: 3:13
            scope 3 {
                debug iter => _6;        // in scope 3 at src/main.rs:4:23: 4:46
                scope 4 {
                    debug i => _7;       // in scope 4 at src/main.rs:4:14: 4:15
                    debug e => _8;       // in scope 4 at src/main.rs:4:17: 4:18
                }
            }
        }
    }
    scope 5 {
        debug iter => _9;                // in scope 5 at src/main.rs:8:9: 8:13
    }

    bb0: {
        return;                          // scope 0 at src/main.rs:10:2: 10:2
    }
}
"""
SOURCE = """fn plan(team: &mut [Engineer], weeks: u32) -> u32 {
    let mut n = 0;
    for week in 1..=weeks {
        for (i, e) in team.iter().enumerate() {
            n += week + e.shifts + i as u32;
        }
    }
    let iter = n;
    iter
}
"""


def _loop_iterators(tmp_path, source=SOURCE, mir=MIR):
    from peeky.rust import RustSession
    from peeky.rust_mir import parse_body

    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src/main.rs").write_text(source)
    body = parse_body(mir)
    session = SimpleNamespace(
        lines={"src/main.rs": tuple(range(1, 11))},
        prepared=SimpleNamespace(source_root=tmp_path),
    )
    texts = {}
    return lambda name, line: RustSession._loop_iterators(
        session, [body], "src/main.rs", name, line, texts
    )


def test_a_for_loops_iterator_is_told_by_the_block_after_its_span(tmp_path):
    # Prep 7: `describe` and `session inspect` listed `iter @ main.rs:32` and
    # `iter @ main.rs:34`, the `for` loops' iterators. The MIR's span of each is the loop's
    # iterator expression, which the loop's `{` follows.
    desugared = _loop_iterators(tmp_path)
    assert desugared("iter", 3) and desugared("iter", 4)
    # The program's own `iter` spans its name: listed. So are the loops' own bindings.
    assert not desugared("iter", 8) and not desugared("week", 3) and not desugared("e", 4)


def test_an_iterator_is_listed_where_its_source_says_otherwise(tmp_path):
    # A loop whose block opens on the next line still counts.
    split = SOURCE.replace("for week in 1..=weeks {", "for week in 1..=weeks\n    {").replace(
        "    let iter = n;", "    let iter = n; // moved"
    )
    split_mir = MIR.replace("src/main.rs:8:", "src/main.rs:9:").replace(
        "src/main.rs:4:", "src/main.rs:5:"
    )
    assert _loop_iterators(tmp_path, split, split_mir)("iter", 3)
    # A source whose line 3 holds no loop there (a stale or other file): listed.
    changed = SOURCE.replace("for week in 1..=weeks {", "let week = 1..=weeks; weeks;")
    assert not _loop_iterators(tmp_path, changed)("iter", 3)
    # A binding whose span the MIR doesn't give is listed.
    bare = MIR.replace(
        "// in scope 1 at src/main.rs:3:17: 3:26", "// in scope 1 at src/main.rs:3:17"
    )
    assert not _loop_iterators(tmp_path, SOURCE, bare)("iter", 3)


LAYOUT = {
    "kind": "vec",
    "shape": "struct",
    "shown": "Engineer",
    "element": "rota::Engineer",
    "element_size": 32,
    "fields": [
        ("shifts", 0, "u32"),
        ("leave", 8, "alloc::vec::Vec<u32, alloc::alloc::Global>"),
    ],
}


def _leave(item, holder=None):
    _, children = _partial_elements(LAYOUT, [bytes(32)] * 2, None, item=item, holder=holder)
    return {member["name"]: member for member in children[1]["children"]}["leave"]["reason"]


def test_an_unread_field_names_the_for_loops_item_in_scope():
    # rota 8: `team[1].leave` said to read it "through a reference to the element, such as
    # a for loop's item", while the loop's `e` was in scope, and `e.leave` read. Nothing
    # proves the loop goes over `team` (review of PR #315: `for e in backups.iter()`), nor
    # which element `e` is at a stop, so the note says so.
    assert _leave("e", "team").endswith(
        "read it through a reference to the element: if the `for` loop's item `e` is "
        "`team[1]` at a stop, read `e.leave`; Peeky can't prove it is"
    )
    assert _leave("e").endswith(
        "if the `for` loop's item `e` is `this element` at a stop, "
        "read `e.leave`; Peeky can't prove it is"
    )
    # Without one in scope, the hint stays general.
    assert _leave(None, "team").endswith(
        "read it through a reference to the element, such as a `for` loop's item"
    )


def _typed(rows, language="rust"):
    """A probe result of (hit, selector, type, value, view[, debugger_type]) observed
    rows."""
    return {
        "status": "completed",
        "hits": max(row[0] for row in rows),
        "probe": {
            "program": "src/main.rs",
            "line": 39,
            "capture": list(dict.fromkeys(row[1] for row in rows)),
        },
        "output": "",
        "rows": [
            dict(hit=hit, phase="observed", line=39, selector=s, type=t, value=v, view=view)
            | ({"debugger_type": more[0]} if more else {})
            for hit, s, t, v, view, *more in rows
        ],
    }


POINTS = "it may point at another element too; compare offsets with address() (COOKBOOK 4.8)"


MOVED = "its addresses differ too: it may point at another element; compare offsets with "


def test_a_raw_pointers_address_is_evidence_and_its_rows_fold(tmp_path):
    # Prep 16: rota's candidate stored `l` as a raw pointer, `[unproven]`, and `diff` said
    # `qualified (run b: unproven): not evidence by itself` at each of 6 hits, though its
    # address is the pointer's own value (COOKBOOK 4.8), and gave each hit a row.
    store = tmp_path / "evidence.db"
    for heap, stack, kind, view in (
        (0x1004151E0, 0x16B846B10, "&rota::Engineer", None),
        (0x1046AD1E0, None, "*const rota::Engineer", "unproven"),
    ):
        rows = []
        for hit in range(1, 4):
            e = f"0x{heap + 0x38 * hit:016x}"
            debugger = "rota::Engineer *"
            rows += [
                (hit, "e", "&rota::Engineer", e, None, debugger),
                (hit, "l", kind, f"0x{stack or heap:016x}", view, debugger),
            ]
        evidence.record(store, _typed(rows), language="rust")
    rows = evidence.run_query(store, "diff")["rows"]
    # The edit made `l` a raw pointer: a real difference, first, in one row (review of PR
    # #315: as an address difference, it ranked after later real differences).
    assert [(r["hit"], r["selector"], r["note"]) for r in rows] == [
        (
            1,
            "l",
            "type &rota::Engineer -> *const rota::Engineer at 3 hits (one row per selector: "
            f"its first hit); {MOVED}address() (COOKBOOK 4.8)",
        ),
        (1, "e", "addresses differ at 3 hits (one row per selector: its first hit): " + POINTS),
    ]
    # Both runs' raw pointers: one row too, never qualified.
    for heap in (0x1004151E0, 0x1046AD1E0):
        rows = [(h, "l", "*const u32", f"0x{heap + 4 * h:016x}", "unproven") for h in (1, 2)]
        evidence.record(store, _typed(rows), language="rust")
    [row] = evidence.run_query(store, "diff")["rows"]
    assert row["note"] == "addresses differ at 2 hits (one row per selector: its first hit): " + (
        POINTS
    )
    hits = evidence.run_query(store, "hits")["rows"]
    assert [row["caveat"] for row in hits] == [None, None]


def test_a_pointers_type_change_is_the_first_divergence(tmp_path):
    # Review of PR #315: `p`'s type changed at hit 1 (`int *` -> `long *`), and its values
    # differ only in addresses; `n` differs at hit 2. As an address difference, `p` came
    # after `n`, so the first divergence was hit 2's.
    store = tmp_path / "evidence.db"
    for base, kind, counts in ((0x16B42B028, "int *", "0"), (0x16F527038, "long *", "1")):
        rows = [
            (1, "p", kind, f"0x{base:016x}", None),
            (1, "n", "int", "0", None),
            (2, "p", kind, f"0x{base + 8:016x}", None),
            (2, "n", "int", counts, None),
        ]
        evidence.record(store, _typed(rows), language="c")
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["hit"], r["selector"], r["note"]) for r in rows] == [
        (
            1,
            "p",
            "type int * -> long * at 2 hits (one row per selector: its first hit); "
            f"{MOVED}address() (COOKBOOK 4.8)",
        ),
        (2, "n", None),
    ]
    # A value that differs beyond its address keeps a row per hit.
    for value in ('0x000000016b42b028 "ab"', '0x000000016f527038 "cd"'):
        rows = [
            (h, "s", "char *" if "ab" in value else "const char *", value, None) for h in (1, 2)
        ]
        evidence.record(store, _typed(rows), language="c")
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["hit"], r["note"]) for r in rows] == [
        (1, "type char * -> const char *"),
        (2, "type char * -> const char *"),
    ]


def _as_schema_17(store):
    """The store as schema 17 saved it: no captures.debugger_type."""
    connection = sqlite3.connect(store)
    connection.executescript(
        "DROP VIEW last_run; DROP VIEW evidence; ALTER TABLE captures DROP COLUMN "
        "debugger_type; PRAGMA user_version = 17;"
    )
    connection.close()


UNTOLD = "the older run predates Rust type spellings, so Peeky can't tell whether the type changed"


@pytest.mark.parametrize("kind", ["&rota::Engineer", "*const rota::Engineer"])
def test_a_rust_type_spelled_before_schema_18_is_said_to_be_untold(tmp_path, kind):
    # Review of PR #315: a run saved before Rust references were spelled as Rust spells
    # them stored `rota::Engineer *`, for a reference and a raw pointer alike; a new run
    # `&rota::Engineer`. `diff` said the type changed, though only its spelling may have;
    # then (round 2) it said nothing, though an old raw pointer may now be a reference.
    store = tmp_path / "evidence.db"
    old = []
    for h in (1, 2):
        old += [
            (h, "e", "rota::Engineer *", f"0x{0x1004151E0 + 0x38 * h:016x}", None),
            (h, "n", "u32", "0", None),
        ]
    evidence.record(store, _typed(old), language="rust")
    _as_schema_17(store)
    view = "unproven" if kind.startswith("*") else None
    new = []
    for h in (1, 2):
        new += [
            (h, "e", kind, f"0x{0x1046AD1E0 + 0x38 * h:016x}", view, "rota::Engineer *"),
            (h, "n", "u32", str(h - 1), None, "unsigned int"),
        ]
    evidence.record(store, _typed(new), language="rust")
    connection = sqlite3.connect(store)
    assert connection.execute("PRAGMA user_version").fetchone() == (evidence.SCHEMA_VERSION,)
    connection.close()
    stored = evidence.query(store, "SELECT run_id, type, debugger_type FROM evidence")["rows"]
    assert {(r["run_id"], r["type"], r["debugger_type"]) for r in stored} == {
        (1, "rota::Engineer *", None),
        (1, "u32", None),
        (2, kind, "rota::Engineer *"),
        (2, "u32", "unsigned int"),
    }
    rows = evidence.run_query(store, "diff")["rows"]
    # Visible, right after the real differences: `n` at hit 2 stays the first divergence.
    assert [(r["hit"], r["selector"], r["note"]) for r in rows] == [
        (2, "n", None),
        (
            1,
            "e",
            f"{UNTOLD} (`rota::Engineer *` then, `{kind}` now) at 2 hits (one row per "
            f"selector: its first hit); {MOVED}address() (COOKBOOK 4.8)",
        ),
    ]
    # The other way round, the older run named first as run b.
    rows = evidence.run_query(store, "diff", {"a": 2, "b": 1})["rows"]
    assert rows[1]["note"].startswith(f"{UNTOLD} (`rota::Engineer *` then, `{kind}` now)")
    # Two new runs: a raw pointer where a reference was is a type change.
    other = "*const rota::Engineer" if kind.startswith("&") else "&rota::Engineer"
    raw = [(h, "e", other, f"0x{0x1046AD1E0:016x}", None, "rota::Engineer *") for h in (1, 2)]
    evidence.record(store, _typed(raw), language="rust")
    rows = evidence.run_query(store, "diff", {"a": 2, "b": 3})["rows"]
    assert rows[0]["note"].startswith(f"type {kind} -> {other} at 2 hits")


def test_another_languages_type_change_is_never_taken_for_a_spelling(tmp_path):
    # Round 2 review of PR #315: a schema-17 C++ row of type `long &&` (a structured
    # binding's reference), against a newer row of type `long` whose debugger type is
    # `long &&`, with values 1 and 2, lost its type change.
    store = tmp_path / "evidence.db"
    evidence.record(store, _typed([(1, "t", "long &&", "1", None)]), language="cpp")
    _as_schema_17(store)
    evidence.record(store, _typed([(1, "t", "long", "2", None, "long &&")]), language="cpp")
    [row] = evidence.run_query(store, "diff")["rows"]
    assert (row["selector"], row["note"]) == ("t", "type long && -> long"), row


def test_unproven_text_beyond_an_address_stays_qualified(tmp_path):
    # A Box or reference past its proof shows only its pointer too, but is no raw pointer
    # by its type: still qualified. So is a raw pointer's row whose value is no address.
    store = tmp_path / "evidence.db"
    for heap in (0x1004151E0, 0x1046AD1E0):
        rows = [
            (1, "b", "int *", f"0x{heap:016x}", "unproven"),
            (1, "r", "*const u8", f"0x{heap:016x} junk", "unproven"),
        ]
        evidence.record(store, _typed(rows), language="rust")
    rows = evidence.run_query(store, "diff")["rows"]
    assert all("qualified (run a: unproven, run b: unproven)" in r["note"] for r in rows), rows


def test_a_gcc_lambdas_capture_says_why_it_is_not_found():
    # Prep 8: GCC's lambda read `grade` as `<not found; visible: a, b, ga, gb>`, with no
    # reason; the C cookbook said why.
    snapshot = {
        "scopes": [{"name": "Locals", "variables": [{"name": n} for n in ("a", "b", "ga", "gb")]}],
        "closure_captures": ["grade"],
    }
    with pytest.raises(ValueError) as raised:
        _root(None, {}, snapshot, "grade")
    assert str(raised.value) == (
        "not found: 'grade' is a capture of this lambda, which GCC passes through `__closure`, "
        "and lldb-dap doesn't list it, so Peeky cannot read it here; read it where the lambda "
        "is created, or print it from the program; visible: a, b, ga, gb"
    )
    with pytest.raises(ValueError) as raised:
        _root(None, {}, snapshot, "nosuch")
    assert str(raised.value) == "not found; visible: a, b, ga, gb"


def test_a_gcc_lambdas_captures_are_its_artificial_variables():
    from peeky.native import _gcc_captures

    dies = {
        1: die("DW_TAG_compile_unit"),
        2: die("DW_TAG_subprogram", 1, (3, 4, 5, 6, 7), DW_AT_object_pointer="0x00000003"),
        3: die("DW_TAG_formal_parameter", 2, DW_AT_name='"__closure"', DW_AT_artificial="(true)"),
        4: die("DW_TAG_formal_parameter", 2, DW_AT_name='"a"'),
        5: die("DW_TAG_variable", 2, DW_AT_name='"grade"', DW_AT_artificial="(true)"),
        # lldb-dap lists GCC's `this`.
        6: die("DW_TAG_variable", 2, DW_AT_name='"this"', DW_AT_artificial="(true)"),
        7: die("DW_TAG_variable", 2, DW_AT_name='"ga"'),
        # clang names a lambda's object pointer `this`, and declares no such variables.
        8: die("DW_TAG_subprogram", 1, (9,), DW_AT_object_pointer="0x00000009"),
        9: die("DW_TAG_formal_parameter", 8, DW_AT_name='"this"', DW_AT_artificial="(true)"),
    }
    assert _gcc_captures(dies, {"chain": [2], "bodies": [2]}) == ("grade",)
    assert _gcc_captures(dies, {"chain": [8], "bodies": [8]}) == ()
