"""`queries diff`, `set_effect` and `join` as the tenth agent evaluation read them: long
values cut before their difference, a `(sets)` row 120 characters wide, intervention rows
after their effects, an alignment note over values a change moved, and `join` showing
unchanged selectors twice. Synthetic stores cut down from outline (Python), debounce (C),
kitty (Rust) and runoff (Java)."""

import contextlib
import json
import threading

import pytest

from peeky import evidence
from peeky.cli import main


@pytest.fixture
def store(tmp_path):
    return tmp_path / "evidence" / "evidence.db"


def saved(store, hits, *, line=66, function="undo", after=(), set_at=(), changes=(), **run):
    """A run of ``hits`` (one {selector: value} per hit, observed), ``after`` (hit,
    selector, value) after-set rows, ``set_at`` (line, hit, name, value) requests and
    their ``changes`` (hit, name, before, after, capture hit[, line]). ``run`` takes
    ``edit``, ``output`` and ``language``."""
    rows = [
        dict(hit=hit, phase="observed", line=line, function=function, selector=s, value=v)
        for hit, values in enumerate(hits, 1)
        for s, v in values.items()
    ]
    rows += [
        dict(hit=hit, phase="after-set", line=line, function=function, selector=s, value=v)
        for hit, s, v in after
    ]
    result = {
        "probe": {
            "program": "a.py",
            "line": line,
            "capture": list(dict.fromkeys(row["selector"] for row in rows)),
            "set": [],
            "set_at": [list(entry) for entry in set_at],
            "hits": "all",
        },
        "status": "completed",
        "hits": len(hits),
        "rows": rows,
        "interventions": [
            dict(
                hit=hit,
                name=name,
                before=b,
                after=a,
                line=where[0] if where else line,
                capture_hit=c,
                function="main" if where else function,
            )
            for hit, name, b, a, c, *where in changes
        ],
        "output": run.get("output", ""),
        **({"candidate": {"diff": run["edit"], "sha256": "e" * 64}} if run.get("edit") else {}),
    }
    return evidence.record(store, result, language=run.get("language", "python"))


def text(capsys, store, *argv):
    assert main(["queries", *argv, "--store", str(store)]) == 0
    return capsys.readouterr().out


# outline: `previous` at line 66, two `u` commands; Café's level is the difference.
ITEMS = "[{'text': 'Einkäufe', 'level': 0}, {'text': 'Milch', 'level': 1}, {'text': 'Brot', "
CAFE = ITEMS + "'level': 1}, {'text': 'Café', 'level': %d}]"


def test_diff_shows_long_values_from_just_before_they_differ(store, capsys):
    # outline 1: `…{'text': 'Café', ...` against `…{'text': 'Café', 'lev...`: both cut
    # before the level, the one difference.
    saved(store, [{"previous": CAFE % 1, "i": "1"}])
    saved(store, [{"previous": CAFE % 0, "i": "1"}])
    shown = text(capsys, store, "diff")
    assert "{'text': 'Café', 'level': 1}]" in shown, shown
    assert "{'text': 'Café', 'level': 0}]" in shown, shown
    (row,) = [line for line in shown.splitlines() if " previous " in line]
    assert row.count("… 'level': 1}, {'text': 'Café', 'level': ") == 2, row
    # --json keeps both values whole.
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["value_a"], r["value_b"]) for r in rows] == [(CAFE % 1, CAFE % 0)]
    # Values that differ early show from their start, as before.
    saved(store, [{"previous": "[0" + CAFE % 1}])
    saved(store, [{"previous": "[1" + CAFE % 1}])
    (row,) = [line for line in text(capsys, store, "diff").splitlines() if " previous " in line]
    assert "[0[{'text': 'Einkäufe'" in row and "[1[{'text': 'Einkäufe'" in row, row
    assert row.count("…") == 2 and "..." not in row, row


def test_both_values_of_a_pair_start_at_one_offset():
    # Review of PR #269: `a` fits, `b` is cut, and their first difference is at 110: each
    # started at its own offset, `a` at 0 and `b` at 70.
    a = "a" * 110
    b = a + "B" * 90
    result = {
        "query": "diff",
        "columns": ["selector", "value_a", "value_b"],
        "rows": [{"selector": "s", "value_a": a, "value_b": b}],
        "row_count": 1,
        "truncated": False,
    }
    row = evidence.table(result).splitlines()[1]
    assert row.startswith("s         …" + "a" * 40 + "  …" + "a" * 40 + "B" * 78 + "…"), row
    assert "..." not in evidence.table(result) and len(row.split("  ")[-1]) <= 120


def test_set_effect_shows_each_change_where_it_differs(store, capsys):
    # outline: `set_effect` hid `level` 1 -> 0 in both value columns and in
    # `interventions`.
    saved(
        store,
        [{"previous": CAFE % 1}],
        after=[(1, "previous", CAFE % 0)],
        set_at=[(66, 1, "previous", CAFE % 0)],
        changes=[(1, "previous", CAFE % 1, CAFE % 0, 1)],
    )
    shown = text(capsys, store, "set_effect")
    (row,) = [line for line in shown.splitlines() if " previous " in line]
    assert "'Café', 'level': 1}]  …" in row and "'Café', 'level': 0}]  NULL" in row, row
    assert row.endswith(
        "previous: …1}, {'text': 'Café', 'level': 1}] -> …1}, {'text': 'Café', 'level': 0}]"
    ), row
    (json_row,) = evidence.run_query(store, "set_effect")["rows"]
    assert json_row["interventions"] == f"previous: {CAFE % 1} -> {CAFE % 0}"


# debounce: `settle` at line 35, one hit per sample; run b resets `db.count` at line 80
# of `main` before hits 5, 8, 16, 19, 26 and 29 (six --set-at requests).
LEVEL = "00101101111110100100001101100000"
STABLE = {"a": "00000111111111111110000000011111", "b": "00000000011111111111000000000000"}
# `db.count`, alike until the first reset (after hit 4).
COUNT = {"a": "01201201201201201201201201201201", "b": "01202012012012012012012012012012"}
SETS = [(80, hit, "db.count", "0") for hit in (5, 8, 16, 19, 26, 29)]


def debounce(store, side, *, after=(), edit=None):
    hits = [
        {"level": level, "db.stable": stable, "db.count": count}
        for level, stable, count in zip(LEVEL, STABLE[side], COUNT[side])
    ]
    return saved(
        store,
        hits,
        line=35,
        function="settle",
        language="c",
        output=f"DEBOUNCE:{side}\n",
        edit=edit,
        **(
            {
                "set_at": SETS,
                "changes": [(hit, "db.count", "1", "0", hit - 1, 80) for _, hit, _, _ in SETS],
                "after": after,
            }
            if side == "b" and edit is None
            else {}
        ),
    )


def test_the_sets_row_is_short_and_interventions_come_first(store, capsys):
    # debounce 1: the `(sets)` cell was about 120 characters, padding every line to about
    # 230 columns, and the intervention rows came after about 70 effect rows.
    debounce(store, "a")
    debounce(store, "b", after=[(5, "db.count", "0"), (8, "db.count", "0")])
    shown = text(capsys, store, "diff")
    header, *table = [line for line in shown.splitlines() if not line.startswith(("note:", "("))]
    (sets,) = [line for line in table if " (sets) " in line]
    assert "  none  " in sets and "6 changes: db.count=0 (requested at 80@5), …" in sets, sets
    # Every line was padded to the (sets) cell's width before its `differs` column.
    assert header.index("differs") < 110, header
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["selector"], r["hit"], r["note"]) for r in rows[2:4]] == [
        ("db.count", 5, "intervention only in run b"),
        ("db.count", 8, "intervention only in run b"),
    ], rows[:5]
    assert all(r["note"] != "intervention only in run b" for r in rows[4:])
    # --json keeps every requested change.
    assert len(json.loads(rows[1]["value_b"])) == 6


def test_values_a_change_moved_tell_nothing_of_alignment(store):
    # debounce: the reset count moves `db.stable`, whose 0s and 1s then line up better at
    # other hits: "the values do not settle whether a hit is the same moment in both …
    # pair hits by a value that names the call", and `settle` has none. The change may
    # account for that, so the note says so; the sign still stands (review of PR #269).
    debounce(store, "a")
    debounce(store, "b")
    debounce(store, "b", edit="--- a/debounce.c\n")
    for b, start, cause in (
        (2, "`level` agrees", "the interventions (run 2's on `db.count` in main) and"),
        (3, "`level` agrees", "the edit and"),
    ):
        (note,) = evidence.run_query(store, "diff", {"a": 1, "b": b})["notes"]
        assert "do not settle" not in note and "join --param" not in note, note
        assert "hits line up" not in note and "pair by" not in note, note
        short = cause.split(" (")[0].removesuffix(" and")
        full = cause.removesuffix(" and")
        assert note == (
            "hits may not line up: the calls may have come in another order or number\n"
            f"- runs 1 and {b} saw `db.stable`'s values at other hits (run {b}'s hit 10 matches "
            "run 1's hit 9: 1)\n"
            f"- `db.stable` may differ because of {full}, so this does not prove a reorder\n"
            f"- {start} between the runs at each of the 32 hits, but it reads alike at some "
            "hits, which could still trade places\n"
            f"- `db.count` differs after {short} and may follow from "
            f"{'them' if b == 2 else 'it'}, so this check leaves it out\n"
            "- no captured selector tells the hits apart, so `queries join` could not pair them"
        )


def test_a_selector_the_change_left_alone_shows_what_pairing_by_number_does(store):
    # debounce, probed at line 80 of `main`: `ms` is alike in both runs and different at
    # every hit, so pairing by number is what `join` by it would give.
    hits = {
        side: [{"ms": str(n), "db.stable": s} for n, s in enumerate(STABLE[side])] for side in "ab"
    }
    saved(store, hits["a"], line=80, function="main")
    saved(
        store,
        hits["b"],
        line=80,
        function="main",
        set_at=SETS,
        changes=[(hit, "db.count", "1", "0", hit - 1, 80) for _, hit, _, _ in SETS],
    )
    assert evidence.run_query(store, "diff")["notes"] == [
        "hits pair by `ms`: it agrees between the runs at each of the 32 hits and tells them "
        "apart\n"
        "- pairing by hit number pairs them as `queries join` by it would, but that does not "
        "prove the hits are the same moments\n"
        "- runs 1 and 2 saw `db.stable`'s values at other hits (run 2's hit 10 matches run 1's "
        "hit 9: 1)\n"
        "- with `ms` pairing every hit, that is a weak sign of a reorder: `db.stable` may "
        "differ because of the interventions (run 2's on `db.count` in main), or match by chance"
    ]


def test_a_reorder_after_an_edit_keeps_the_hits_from_lining_up(store):
    # Review of PR #269: with `call` left out after the edit, `key` alone said "the hits
    # line up by `key`", though `call` shows the first two calls swapped.
    saved(store, [{"key": k, "call": c} for k, c in zip("123", ["'A'", "'B'", "'C'"])])
    saved(
        store,
        [{"key": k, "call": c} for k, c in zip("123", ["'B'", "'A'", "'C'"])],
        edit="--- a/a.py\n",
    )
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert "hits line up" not in note, note
    assert note == (
        "hits pair by `key`: it agrees between the runs at each of the 3 hits and tells them "
        "apart\n"
        "- pairing by hit number pairs them as `queries join` by it would, but that does not "
        "prove the hits are the same moments\n"
        "- runs 1 and 2 saw the same values of `call` in another order (run 2's hit 1 matches "
        "run 1's hit 2: 'B')\n"
        "- with `key` pairing every hit, that is a weak sign of a reorder: `call` may differ "
        "because of the edit, or match by chance"
    )


def test_a_candidate_that_changes_every_value_gets_a_short_note(store):
    # outline's candidate: "the runs' values differ at 2 of 2 hits, so they cannot show
    # that a hit is the same moment in both; … pair hits by a value that names the call".
    for edit, name in ((None, "Brötchen"), ("--- a/outline.py\n", "Brot")):
        saved(
            store,
            [
                {"previous": f"[{{'text': '{name}'}}]", "self.items[2]": f"'{name}'"},
                {"previous": f"[{{'text': '{name}'}}, {{'text': 'Café'}}]"},
            ],
            edit=edit,
        )
    assert evidence.run_query(store, "diff")["notes"] == [
        "hits may not line up: the values cannot show whether they do\n"
        "- `previous` and `self.items[2]` differ after the edit and may follow from it\n"
        "- no other selector both agrees between the runs and changes from hit to hit"
    ]


def test_agreement_is_said_as_agreement_between_the_runs(store):
    # kitty 5: "`e.cents` reads the same at each of the 3 hits in both runs, but `payer`
    # differs …" sounded like one constant value; it means the runs agree hit by hit.
    cents = ["4500", "1000", "900"]
    for payers, edit in ((["0", "2", "1"], None), (["3", "2", "0"], None)):
        saved(store, [{"payer": p, "e.cents": c} for p, c in zip(payers, cents)], line=72)
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith(
        "hits may not line up: `payer` differs at 2 of the 3 hits\n- `e.cents` agrees between "
        "the runs at each of the 3 hits, but that does not show that a hit is the same moment "
        "in both"
    ), note
    # kitty's own candidate (settle before the sort): `payer` follows from the edit.
    saved(
        store,
        [{"payer": p, "e.cents": c} for p, c in zip(["3", "2", "0"], cents)],
        line=72,
        edit="--- a/src/main.rs\n",
    )
    assert evidence.run_query(store, "diff", {"a": 1, "b": 3})["notes"] == [
        "hits pair by `e.cents`: it agrees between the runs at each of the 3 hits and tells "
        "them apart\n"
        "- pairing by hit number pairs them as `queries join` by it would, but that does not "
        "prove the hits are the same moments\n"
        "- `payer` differs after the edit and may follow from it, so this check leaves it out"
    ]


def test_join_is_named_only_where_a_selector_could_key_it(store):
    # Calls in another order, and only `op`, which repeats: `join` could not pair them.
    for ops in ("---+-", "----+"):
        saved(store, [{"op": f"'{op}'"} for op in ops], line=24)
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert "join --param" not in note, note
    assert note.endswith(
        "no captured selector tells the hits apart, so `queries join` could not pair them"
    )
    # With the token's position, which tells the hits apart: named.
    for ops, positions in (("---+-", "12345"), ("----+", "12354")):
        saved(store, [{"op": f"'{o}'", "pos": p} for o, p in zip(ops, positions)], line=24)
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert "`peeky queries join --param a=3 --param b=4 --param key=" in note, note


# runoff: `count` at line 56, one hit per round; the fix changes round 3's tally.
NAMES = 'ArrayList@64 size=4 ["Mira", "Lena", "Kofi", "Jonas"]'
TALLY = {"a": ["[4, 2, 0, 3]", "[4, 2, 0, 3]", "[4, 0, 2, 3]", "[4, 0, 3, 0]"]}
TALLY["b"] = ["[4, 2, 0, 3]", "[4, 2, 0, 3]", "[4, 0, 0, 5]"]


def runoff(store, side):
    hits = [
        {"round": str(n), "counted": "9", "tally": t, "this.names": NAMES}
        for n, t in enumerate(TALLY[side], 1)
    ]
    return saved(store, hits, line=56, function="Runoff.count", language="java")


def test_join_leaves_out_selectors_that_agree_at_every_paired_hit(store, capsys):
    # runoff 7: `queries join --param key=round` was about 400 columns wide, every
    # unchanged selector shown for both runs. Only the text table leaves them out;
    # `--json` keeps every column (review of PR #269).
    runoff(store, "a")
    runoff(store, "b")
    every = ["a:counted", "a:tally", "a:this.names", "b:counted", "b:tally", "b:this.names"]
    joined = evidence.run_query(store, "join", {"key": "round"})
    assert joined["columns"][3:9] == every and "notes" not in joined
    assert evidence.query(store, joined["sql"])["rows"] == joined["rows"]
    assert main(["queries", "join", "--param", "key=round", "--store", str(store), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["columns"][3:9] == every
    header, *_, note, count = text(capsys, store, "join", "--param", "key=round").splitlines()
    assert header.split() == [
        "round",
        "hit_a",
        "hit_b",
        "a:tally",
        "b:tally",
        "no_value_a",
        "no_value_b",
        "caveats_a",
        "caveats_b",
        "matched",
    ]
    assert note == (
        "note: left out 2 selectors that agree between the runs at every paired hit: `counted` "
        "and `this.names` (with their values at unpaired hits); --param unchanged=1 or --json "
        "shows them"
    )
    # unchanged=1 keeps them in the table, with no note.
    kept = text(capsys, store, "join", "--param", "key=round", "--param", "unchanged=1")
    assert "a:this.names" in kept and "b:counted" in kept and "note:" not in kept
    assert evidence.run_query(store, "join", {"key": "round", "unchanged": 1}) == joined
    with pytest.raises(ValueError, match="unchanged is 1"):
        evidence.run_query(store, "join", {"key": "round", "unchanged": "yes"})
    assert "unchanged" in evidence.schema(store)["queries"]["join"]["params"]


# Review 2 of PR #269: the text table re-read "the newest two runs" after the query ran,
# so a probe saved meanwhile changed what it compared. Review 3: the rows, notes and
# table now read one snapshot, so a probe's save waits for them (a store's rollback
# journal: its commit waits for the reader's shared lock). Each test saves from another
# thread, as a probe would, and gives it a second to commit before reading on.
def probe_meanwhile(save):
    """Start ``save`` in another thread and wait up to a second for it: it commits at
    once without a snapshot, and only after the query's reads with one."""
    thread = threading.Thread(target=save)
    thread.start()
    thread.join(timeout=1)
    return thread


def test_join_table_reads_the_runs_its_result_came_from(store, capsys, monkeypatch):
    for xs in (["7", "8"], ["7", "9"]):
        saved(store, [{"k": k, "x": x, "y": "4"} for k, x in zip("12", xs)], line=9)
    table, saving = evidence.query_table, []

    def after_a_probe(*args, **kwargs):
        # Runs 2 and 3 agree on `x`; runs 1 and 2 do not.
        rows = [{"k": k, "x": x, "y": "4"} for k, x in zip("12", ["7", "9"])]
        saving.append(probe_meanwhile(lambda: saved(store, rows, line=9)))
        return table(*args, **kwargs)

    monkeypatch.setattr(evidence, "query_table", after_a_probe)
    header, *rows, note, count = text(capsys, store, "join", "--param", "key=k").splitlines()
    saving[0].join()
    assert header.split()[:5] == ["k", "hit_a", "hit_b", "a:x", "b:x"], header
    assert rows[1].split()[:5] == ["2", "2", "2", "8", "9"], rows
    assert note == (
        "note: left out 1 selector that agrees between the runs at every paired hit: `y`; "
        "--param unchanged=1 or --json shows it"
    )
    # The probe saved run 3 once the query was done; prepare names it now.
    assert evidence.prepare(store, "join", {"key": "k", "a": 1}) == {"key": "k", "a": 1, "b": 3}


def test_set_effect_table_reads_its_own_runs_changes(store, capsys, monkeypatch):
    saved(
        store,
        [{"previous": CAFE % 1}],
        after=[(1, "previous", CAFE % 0)],
        set_at=[(66, 1, "previous", CAFE % 0)],
        changes=[(1, "previous", CAFE % 1, CAFE % 0, 1)],
    )
    table, saving = evidence.query_table, []

    def after_a_probe(*args, **kwargs):
        saving.append(probe_meanwhile(lambda: saved(store, [{"previous": CAFE % 1}])))
        return table(*args, **kwargs)

    monkeypatch.setattr(evidence, "query_table", after_a_probe)
    shown = text(capsys, store, "set_effect")
    saving[0].join()
    (row,) = [line for line in shown.splitlines() if " previous " in line]
    assert row.endswith(
        "previous: …1}, {'text': 'Café', 'level': 1}] -> …1}, {'text': 'Café', 'level': 0}]"
    ), row


def test_diff_notes_read_the_runs_its_rows_came_from(store, monkeypatch):
    for calls in (["'A'", "'B'"], ["'B'", "'A'"]):
        saved(store, [{"key": k, "call": c} for k, c in zip("12", calls)], line=10)
    unaligned, saving = evidence._unaligned, []

    def after_a_probe(path, params):
        # Runs 2 and 3 are alike: notes on them would say nothing.
        rows = [{"key": k, "call": c} for k, c in zip("12", ["'B'", "'A'"])]
        saving.append(probe_meanwhile(lambda: saved(store, rows, line=10)))
        return unaligned(path, params)

    monkeypatch.setattr(evidence, "_unaligned", after_a_probe)
    result = evidence.run_query(store, "diff")
    saving[0].join()
    assert {(row["run_a"], row["run_b"]) for row in result["rows"]} == {(1, 2)}
    (note,) = result["notes"]
    assert note.startswith(
        "hits may not line up: the values do not settle how they pair\n- `key` agrees between "
        "the runs at each of the 2 hits\n- but runs 1 and 2 saw the same values of `call` in "
        "another order"
    ), note


def full_store(store):
    """Runs 1 and 2, `call` A, B against B, A (another order, no change between them),
    then runs 3 to 500: the next save, run 501, evicts run 1 (MAX_RUNS)."""
    assert evidence.MAX_RUNS == 500
    for calls in (["'A'", "'B'"], ["'B'", "'A'"]):
        saved(store, [{"key": k, "call": c} for k, c in zip("12", calls)], line=10)
    connection = evidence._ensure(store)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "WITH RECURSIVE n(i) AS (SELECT 3 UNION ALL SELECT i + 1 FROM n WHERE i < 500) "
            "INSERT INTO runs (run_id, started_at, program, line, status, hits) "
            "SELECT i, started_at, program, line, status, hits FROM n, runs WHERE run_id = 2"
        )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return [{"key": k, "call": "'C'"} for k in "12"]


def test_a_run_evicted_after_the_rows_never_silences_the_alignment_note(store, monkeypatch):
    # Review 3 of PR #269: `diff --param a=1 --param b=2` read its rows; a probe saved run
    # 501, evicting run 1; `_unaligned` then found no pair and said nothing, so the
    # reorder warning vanished.
    other = full_store(store)
    unaligned, saving = evidence._unaligned, []

    def after_a_probe(path, params):
        saving.append(probe_meanwhile(lambda: saved(store, other, line=10)))
        return unaligned(path, params)

    monkeypatch.setattr(evidence, "_unaligned", after_a_probe)
    result = evidence.run_query(store, "diff", {"a": 1, "b": 2})
    saving[0].join()
    (note,) = result["notes"]
    assert note.startswith(
        "hits may not line up: the values do not settle how they pair\n- `key` agrees between "
        "the runs at each of the 2 hits\n- but runs 1 and 2 saw the same values of `call` in "
        "another order"
    ), note
    # The probe did save run 501, and it evicted run 1, once the query was done.
    runs = evidence.query(store, "SELECT MIN(run_id) AS first, MAX(run_id) AS last FROM runs")
    assert runs["rows"] == [{"first": 2, "last": 501}]


def test_a_run_gone_before_the_notes_is_said_never_silent(store, monkeypatch):
    # The backstop, without the snapshot: the run goes between the rows and the notes,
    # and the note says what could not be checked.
    other = full_store(store)
    unaligned = evidence._unaligned

    def after_a_probe(path, params):
        saved(store, other, line=10)
        return unaligned(path, params)

    monkeypatch.setattr(evidence, "_unaligned", after_a_probe)
    monkeypatch.setattr(evidence, "_snapshot", lambda path: contextlib.nullcontext(), raising=False)
    result = evidence.run_query(store, "diff", {"a": 1, "b": 2})
    assert result["notes"] == [
        "alignment, labels and older-run warnings could not be checked: run 1 was removed "
        "after the rows were read; run the query again"
    ]
