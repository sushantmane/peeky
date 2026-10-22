"""`queries diff`, `set_effect` and `join` as the tenth agent evaluation read them: long
values cut before their difference, a `(sets)` row 120 characters wide, intervention rows
after their effects, an alignment note over values a change moved, and `join` showing
unchanged selectors twice. Synthetic stores cut down from outline (Python), debounce (C),
kitty (Rust) and runoff (Java)."""

import json

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
        assert "the hits line up as" not in note and "line up by" not in note, note
        short = cause.split(" (")[0].removesuffix(" and")
        assert note == (
            f"{start} between the runs at each of the 32 hits, but it reads alike at some "
            f"hits, which could still trade places; `db.count` differs after {cause} may "
            f"follow from {'them' if b == 2 else 'it'}, so this check leaves it out; but runs "
            f"1 and {b} saw `db.stable`'s values at other hits (run {b}'s hit 10 matches run 1's "
            f"hit 9: 1): `db.stable` may differ because of {short}, so this does not prove a "
            "reorder, but it keeps Peeky from saying the hits line up; no captured selector "
            "tells the hits apart, so `queries join` could not pair them"
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
        "`ms` agrees between the runs at each of the 32 hits, and its value is different at "
        "every hit, so pairing hits by number pairs them as `queries join` by it would, though "
        "that does not prove the hits are the same moments; but runs 1 and 2 saw "
        "`db.stable`'s values at other hits (run 2's hit 10 matches run 1's hit 9: 1): "
        "`db.stable` may differ because of the interventions (run 2's on `db.count` in main), "
        "so this does not prove a reorder, but it keeps Peeky from saying the hits line up"
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
    assert "the hits line up by" not in note and "the hits line up as" not in note, note
    assert note == (
        "`key` agrees between the runs at each of the 3 hits, and its value is different at "
        "every hit, so pairing hits by number pairs them as `queries join` by it would, though "
        "that does not prove the hits are the same moments; but runs 1 and 2 saw the same "
        "values of `call` in another order (run 2's hit 1 matches run 1's hit 2: 'B'): `call` "
        "may differ because of the edit, so this does not prove a reorder, but it keeps Peeky "
        "from saying the hits line up"
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
        "the values cannot show whether the hits line up: `previous` and `self.items[2]` "
        "differ after the edit and may follow from it, and no other selector both agrees "
        "between the runs and changes from hit to hit"
    ]


def test_agreement_is_said_as_agreement_between_the_runs(store):
    # kitty 5: "`e.cents` reads the same at each of the 3 hits in both runs, but `payer`
    # differs …" sounded like one constant value; it means the runs agree hit by hit.
    cents = ["4500", "1000", "900"]
    for payers, edit in ((["0", "2", "1"], None), (["3", "2", "0"], None)):
        saved(store, [{"payer": p, "e.cents": c} for p, c in zip(payers, cents)], line=72)
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith(
        "`e.cents` agrees between the runs at each of the 3 hits, but `payer` differs at 2 "
        "of them: the values do not show that a hit is the same moment in both"
    ), note
    # kitty's own candidate (settle before the sort): `payer` follows from the edit.
    saved(
        store,
        [{"payer": p, "e.cents": c} for p, c in zip(["3", "2", "0"], cents)],
        line=72,
        edit="--- a/src/main.rs\n",
    )
    assert evidence.run_query(store, "diff", {"a": 1, "b": 3})["notes"] == [
        "`e.cents` agrees between the runs at each of the 3 hits, and its value is different "
        "at every hit, so pairing hits by number pairs them as `queries join` by it would, "
        "though that does not prove the hits are the same moments; `payer` differs after the "
        "edit and may follow from it, so this check leaves it out"
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
