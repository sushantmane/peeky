"""`queries diff` judging whether two runs' hits line up, and `queries join` by several
selectors (#201). The ninth agent evaluation's stores, cut down: wrap (C), whose
`--set-at` changed `lay.width`; roster (Rust), whose fix shifted every `width`; and
rechnung (Java), whose fix made one call fewer, so 20 of `diff`'s 22 rows were at
later hits."""

import json

import pytest

from peeky import evidence


@pytest.fixture
def store(tmp_path):
    return tmp_path / "evidence" / "evidence.db"


def saved(
    store,
    hits,
    *,
    language="c",
    function="fits",
    line=29,
    returned=(),
    after=(),
    set_at=(),
    changes=(),
    edit=None,
    output="",
    label=None,
    stale=(),
):
    """A run of ``hits`` (one {selector: value} per hit, observed), ``returned`` values
    (one per hit), ``after`` (hit, selector, value) after-set rows, ``set_at`` (line,
    hit, name, value) and its ``changes`` (hit, name, before, after, capture hit: how
    many hits of the line came before it[, the function whose variable it was]).
    Selectors in ``stale`` are stale (a view)."""
    rows = [
        dict(hit=hit, phase="observed", line=line, function=function, selector=s, value=v)
        | ({"stale": True} if s in stale else {})
        for hit, values in enumerate(hits, 1)
        for s, v in values.items()
    ]
    rows += [
        dict(hit=hit, phase="returned", line=40, function=function, selector="(return)", value=v)
        for hit, v in enumerate(returned, 1)
    ]
    rows += [
        dict(hit=hit, phase="after-set", line=line, function=function, selector=s, value=v)
        for hit, s, v in after
    ]
    selectors = list(dict.fromkeys(row["selector"] for row in rows))
    result = {
        "probe": {
            "program": "a.c",
            "line": line,
            "capture": selectors,
            "set": [],
            "set_at": [list(entry) for entry in set_at],
            "hits": "all",
        },
        "status": "completed",
        "hits": len(hits),
        "rows": rows,
        "interventions": [
            dict(hit=hit, name=name, before=b, after=a, line=line, capture_hit=c)
            | ({"function": where[0]} if where else {})
            for hit, name, b, a, c, *where in changes
        ],
        "output": output,
        **({"candidate": {"diff": edit, "sha256": "e" * 64}} if edit else {}),
    }
    return evidence.record(store, result, language=language, label=label)


def notes(store, a, b):
    return evidence.run_query(store, "diff", {"a": a, "b": b}).get("notes", [])


def alignment(found):
    return [note for note in found if not note.startswith("labels:")]


# wrap: `fits(word, column, lay)` at line 29, 8 calls; `--set-at 75@1 lay.width=16`
# (run 2) and the fix (run 3) wrap the same way; run 1 is the issue's input.
COLUMN = {1: [3, 9, 15, 19, 5, 10, 14, 19], 2: [3, 9, 15, 3, 9, 14, 3, 8]}
WORD = [5, 5, 3, 5, 4, 3, 4, 3]
RETURNS = {1: [1, 1, 1, 0, 1, 1, 1, 0], 2: [1, 1, 0, 1, 1, 0, 1, 1]}


def wrap(store, columns, width, *, word=True, **options):
    hits = [
        {
            "column": str(column),
            **({"word": str(w)} if word else {}),
            "lay.width": str(width),
            "lay.indent": "4",
        }
        for column, w in zip(COLUMN[columns], WORD)
    ]
    return saved(store, hits, returned=[str(v) for v in RETURNS[columns]], **options)


JOIN = (
    "if so, pair hits by a value that names the call and that the edit leaves alone (an "
    "argument, an index): `peeky queries join --param a={} --param b={} "
    "--param key=<selector>[,<selector>…]`"
)


def test_a_value_an_intervention_changed_is_no_sign_the_hits_do_not_line_up(store):
    # #201, wrap 2 and 3: `lay.width` 16 against 20 at all 8 hits, from run 2's
    # --set-at in main before the first hit (capture hit 0), was taken for "the runs'
    # values differ at 8 of 8 hits"; `column`, `word` and `(return)` are alike at every
    # hit. After an intervention, what follows from it is unknown (review of PR #209):
    # the note says what agrees and what was left out, not that the hits line up.
    wrap(
        store,
        2,
        16,
        set_at=[(75, 1, "lay.width", "16")],
        changes=[(1, "lay.width", "20", "16", 0)],
        label="set-width-16",
    )
    wrap(store, 2, 20, edit="--- a/wrap.c\n+++ b/wrap.c\n", label="candidate")
    assert alignment(notes(store, 1, 2)) == [
        "`column` and `word` agree between the runs at each of the 8 hits, but they read "
        "alike at some hits, which could still trade places; `lay.width` differs after the "
        "edit and the interventions (run 1's on `lay.width`) and may follow from them, so "
        "this check leaves it out"
    ]
    rows = evidence.run_query(store, "diff", {"a": 1, "b": 2})["rows"]
    assert {row["selector"] for row in rows if row["hit"] is not None} == {"lay.width"}


def test_a_value_an_edit_changed_is_no_sign_the_hits_do_not_line_up(store):
    # #201, wrap 1 and 3: `run 3's hit 6 matches run 1's hit 7: 14`, though `word` is
    # alike at every hit. The fix changes `column`, which then lines up better at other
    # hits: a value that differs after an edit may follow from it, so it tells nothing of
    # how the hits pair (tenth evaluation), and the note says it was left out.
    wrap(store, 1, 20, label="issue-input")
    wrap(store, 2, 20, edit="--- a/wrap.c\n+++ b/wrap.c\n", label="candidate")
    assert alignment(notes(store, 1, 2)) == [
        "the hits line up as far as `word` shows: it agrees between the runs at each of the 8 "
        "hits, but it reads alike at some hits, which could still trade places; `column` "
        "differs after the edit and may follow from it, so this check leaves it out"
    ]
    # Without `word`, nothing that changes from hit to hit is left: a short note.
    wrap(store, 1, 20, word=False)
    wrap(store, 2, 20, word=False, edit="--- a/wrap.c\n+++ b/wrap.c\n")
    assert alignment(notes(store, 3, 4)) == [
        "the values cannot show whether the hits line up: `column` differs after the edit and "
        "may follow from it, and no other selector both agrees between the runs and changes "
        "from hit to hit"
    ]


def test_a_value_alike_at_every_hit_never_hides_another_pairing(store):
    # Review of PR #209, 2: `key` 1, 2 in both runs (a counter, or alike by chance) hid
    # `call` A, B against B, A: "the hits line up by `key`".
    saved(store, [{"key": "1", "call": "'A'"}, {"key": "2", "call": "'B'"}])
    saved(store, [{"key": "1", "call": "'B'"}, {"key": "2", "call": "'A'"}])
    (note,) = alignment(notes(store, 1, 2))
    assert note.startswith(
        "`key` agrees between the runs at each of the 2 hits, but runs 1 and 2 saw the "
        "same values of `call` in another order (run 2's hit 1 matches run 1's hit 2: 'B'): "
        "the values do not settle whether a hit is the same moment in both"
    ), note
    assert "line up by" not in note
    # `key` tells the hits apart, so `join` could pair them by it: it is named.
    assert note.endswith("; " + JOIN.format(1, 2)), note
    # After an intervention (on `x`, before the first hit), `call`'s order may follow
    # from it, as `x`'s value does (tenth evaluation): both are left out, and what is
    # left only says what pairing by number amounts to, never that the hits line up.
    saved(
        store, [{"x": "0", "gleich": "1", "call": "'A'"}, {"x": "0", "gleich": "2", "call": "'B'"}]
    )
    saved(
        store,
        [{"x": "5", "gleich": "1", "call": "'B'"}, {"x": "5", "gleich": "2", "call": "'A'"}],
        set_at=[(10, 1, "x", "5")],
        changes=[(1, "x", "0", "5", 0)],
    )
    assert alignment(notes(store, 3, 4)) == [
        "`gleich` agrees between the runs at each of the 2 hits, and its value is different "
        "at every hit, so pairing hits by number pairs them as `queries join` by it would; "
        "`x` and `call` differ after the interventions (run 4's on `x`) and may follow from "
        "them, so this check leaves them out"
    ]
    # A reorder among hits that read alike is said too.
    saved(
        store,
        [{"word": "5", "column": "3"}, {"word": "5", "column": "9"}, {"word": "3", "column": "15"}],
    )
    saved(
        store,
        [{"word": "5", "column": "9"}, {"word": "5", "column": "3"}, {"word": "3", "column": "15"}],
    )
    (note,) = alignment(notes(store, 5, 6))
    assert "but runs 5 and 6 saw the same values of `column` in another order" in note, note


def test_a_qualified_value_tells_nothing_and_matches_nothing(store):
    # Review of PR #209, 1: `stale_key` 1, 2 (a moved Rust local's bytes) at both hits
    # gave "the hits line up by `stale_key`" over `call` A, B against B, A, and `join`
    # paired on it.
    saved(
        store,
        [{"stale_key": "1", "call": "'A'"}, {"stale_key": "2", "call": "'B'"}],
        stale={"stale_key"},
    )
    saved(
        store,
        [{"stale_key": "1", "call": "'B'"}, {"stale_key": "2", "call": "'A'"}],
        stale={"stale_key"},
    )
    (note,) = alignment(notes(store, 1, 2))
    assert note.startswith("runs 1 and 2 saw the same values of `call` in another order"), note
    joined = evidence.run_query(store, "join", {"key": "stale_key"})
    assert [(r["stale_key"], r["hit_a"], r["hit_b"], r["matched"]) for r in joined["rows"]] == [
        (None, 1, None, "only in run a"),
        (None, 2, None, "only in run a"),
        (None, None, 1, "only in run b"),
        (None, None, 2, "only in run b"),
    ]
    assert json.loads(joined["rows"][0]["caveats_a"]) == {"stale_key": "stale"}


def test_values_read_before_an_intervention_still_tell(store):
    # Review of PR #209, 3: `x` A, B, C against B, A, Z, set after run 2's hit 2: every
    # hit of `x` was left out, and the reorder at hits 1 and 2 with it. Only hit 3's is.
    saved(store, [{"x": "'A'"}, {"x": "'B'"}, {"x": "'C'"}])
    saved(
        store,
        [{"x": "'B'"}, {"x": "'A'"}, {"x": "'Z'"}],
        set_at=[(30, 2, "x", "'Z'")],
        changes=[(2, "x", "'C'", "'Z'", 2)],
    )
    (note,) = alignment(notes(store, 1, 2))
    assert note.startswith(
        "runs 1 and 2 saw the same values of `x` in another order (run 2's hit 1 matches run "
        "1's hit 2: 'B')"
    ), note


def test_values_alike_at_every_hit_and_different_at_each_say_the_hits_line_up(store):
    # #201, roster: `title` is alike at every hit and tells them apart.
    titles = ['"Name"', '"City"', '"Pts"']

    def roster(widths, symbol, **options):
        hits = [{"title": t, "width": w} for t, w in zip(titles, widths)]
        return saved(
            store, hits, language="rust", function=f"roster::widest::{symbol}", line=43, **options
        )

    # Nothing else suggests another pairing, and no intervention: the hits line up.
    # `width` differs after the edit, so it is left out.
    edited = (
        "the hits line up by `title`: it agrees between the runs at each of the 3 hits, and "
        "its value is different at every hit, so pairing hits by number pairs them as "
        "`queries join` by it would; `width` differs after the edit and may follow from it, "
        "so this check leaves it out"
    )
    roster(["5", "6", "3"], "he3b8016ca05e3946", edit="--- a/src/main.rs\n")
    roster(["5", "6", "9"], "hb7185e602e967d61")
    assert alignment(notes(store, 1, 2)) == [edited]
    # roster 5 and 6: the fix counts characters, so every name's `width` is one less (6 7
    # 3 -> 5 6 3), which lines up at other hits; #201 took that for a sign the hits do
    # not line up. It may follow from the edit (tenth evaluation): left out.
    roster(["6", "7", "3"], "hb7185e602e967d61", label="original-43")
    assert alignment(notes(store, 1, 3)) == [edited]
    # roster 4: the widths set by --set-at instead, each after its hit's read. Each hit is
    # a new call of `widest`, but a value read after a change may follow from it,
    # whatever its name: `width` is left out from the first change on.
    roster(
        ["6", "7", "3"],
        "h01f9ab3be113ab4d",
        after=[
            (1, "title", '"Name"'),
            (1, "width", "5"),
            (2, "title", '"City"'),
            (2, "width", "6"),
        ],
        set_at=[(43, 1, "width", "5"), (43, 2, "width", "6")],
        changes=[(1, "width", "6", "5", 1), (2, "width", "7", "6", 2)],
    )
    assert alignment(notes(store, 4, 1)) == [
        "`title` agrees between the runs at each of the 3 hits, and its value is different "
        "at every hit, so pairing hits by number pairs them as `queries join` by it would; "
        "`width` differs after the edit and the interventions (run 4's on `width`) and may "
        "follow from them, so this check leaves it out"
    ]
    # Nothing differs: nothing to say.
    roster(["6", "7", "3"], "hb7185e602e967d61")
    assert alignment(notes(store, 3, 5)) == []


# rechnung: `zusammenfassen` compares each pair of items (line 56): the bug compares
# `Integer`s with `==`, so the two Schraube items are not merged; the fix merges them,
# and makes one comparison fewer per later item.
S, M, V = '"Schraube"', '"Mutter"', '"Versand"'
NUMBER = {S: "Integer@60 {value=1001}", M: "Integer@96 {value=42}", V: "null"}
BEFORE = [(S, M), (S, V), (M, V), (S, S), (M, S), (V, S), (S, M), (M, M)]
FIXED = [(S, M), (S, V), (M, V), (S, S), (S, M), (M, M)]


def rechnung(store, pairs, fixed):
    hits = []
    for p, neu in pairs:
        hits.append(
            {
                "p.bezeichnung": p,
                "neu.bezeichnung": neu,
                "p.artikelNr": NUMBER[p],
                "neu.artikelNr": "Integer@106 {value=1001}" if neu == S else NUMBER[neu],
                # `==` on two `Integer`s: true only for small values (Mutter's 42).
                "gleich": "true" if p == neu and (fixed or p == M) else "false",
            }
        )
    output = (
        "RECHNUNG:Schraube*12=270;Mutter*10=90;Versand*1=490;SUMME=850\n"
        if fixed
        else "RECHNUNG:Schraube*4=100;Mutter*10=90;Versand*1=490;Schraube*8=200;SUMME=880\n"
    )
    return saved(
        store,
        hits,
        language="java",
        function="Rechnung.zusammenfassen(List)",
        line=56,
        output=output,
        edit="--- a/app/Rechnung.java\n" if fixed else None,
    )


def test_diff_says_how_many_rows_come_after_the_first_difference(store):
    # #201, rechnung 1: run 1 stopped 8 times, the fix 6; 20 of the 22 rows were at
    # later hits after the first difference (hit 4), comparing other calls.
    rechnung(store, BEFORE, False)
    rechnung(store, FIXED, True)
    result = evidence.run_query(store, "diff")
    assert len(result["rows"]) == 22
    first = min(row["hit"] for row in result["rows"] if row["hit"] is not None)
    assert first == 4 and [r["selector"] for r in result["rows"] if r["hit"] == 4] == ["gleich"]
    count, after = alignment(result["notes"])
    assert count.startswith("pair the hits by a value instead"), count
    assert "Run 1 stopped 8 times at its line and run 2 6" in count, count
    assert (
        "`peeky queries join --param a=1 --param b=2 --param key=<selector>[,<selector>…]`" in count
    ), count
    assert after == (
        "the first difference at a hit is at hit 4; 19 of the 20 rows at a hit come after "
        "it, where pairing by hit number may compare different calls: read those with "
        "`join`"
    )


def test_join_pairs_by_several_selectors_and_says_when_its_key_repeats(store):
    # #201, rechnung 1: `queries join --param key=neu.bezeichnung` gave 16 rows, every
    # comparison with the same second item paired with every other: no one selector
    # names a comparison. Two do, but for the first and seventh.
    rechnung(store, BEFORE, False)
    rechnung(store, FIXED, True)
    one = evidence.run_query(store, "join", {"key": "neu.bezeichnung"})
    assert sum(row["matched"] == "both" for row in one["rows"]) == 16
    note, dropped = one["notes"]
    # `neu.artikelNr` follows from the key, so it agrees at every pair.
    assert dropped == (
        "left out 1 selector that agrees between the runs at every paired hit: "
        "`neu.artikelNr`; add --param unchanged=1 to show it"
    )
    assert note == (
        "the key repeats: add a selector that names the call to the key "
        "(key=neu.bezeichnung,<selector>). Run 1's hits 1, 7 and 8 all have `neu.bezeichnung` "
        '"Mutter", and such a value pairs every hit that has it in one run with every one in '
        "the other (16 rows, from 3 values)"
    )
    two = evidence.run_query(store, "join", {"key": "p.bezeichnung,neu.bezeichnung"})
    assert two["columns"][:4] == ["p.bezeichnung", "neu.bezeichnung", "hit_a", "hit_b"]
    assert "a:p.bezeichnung" not in two["columns"] and "a:gleich" in two["columns"]
    assert "a:p.artikelNr" not in two["columns"] and "b:neu.artikelNr" not in two["columns"]
    paired = [(r["hit_a"], r["hit_b"]) for r in two["rows"] if r["matched"] == "both"]
    assert paired == [(1, 1), (1, 5), (2, 2), (3, 3), (4, 4), (7, 1), (7, 5), (8, 6)]
    unpaired = [
        (r["hit_a"], r["hit_b"], r["matched"]) for r in two["rows"] if r["matched"] != "both"
    ]
    assert unpaired == [(5, None, "only in run a"), (6, None, "only in run a")]
    note, dropped = two["notes"]
    assert dropped == (
        "left out 2 selectors that agree between the runs at every paired hit: "
        "`p.artikelNr` and `neu.artikelNr` (with their values at unpaired hits); add "
        "--param unchanged=1 to show them"
    )
    assert note == (
        "the key repeats: add a selector that names the call to the key "
        "(key=p.bezeichnung,neu.bezeichnung,<selector>). Run 1's hits 1 and 7 both have "
        '`p.bezeichnung` "Schraube" and `neu.bezeichnung` "Mutter", and such a value pairs '
        "every hit that has it in one run with every one in the other (4 rows, from 1 value)"
    ), note
    assert evidence.query(store, two["sql"])["rows"] == two["rows"]


def test_join_keys_split_only_outside_brackets_and_quotes(store):
    saved(store, [{'m["a,b"]': "1", "x": "7"}, {'m["a,b"]': "2", "x": "8"}])
    saved(store, [{'m["a,b"]': "2", "x": "8"}, {'m["a,b"]': "1", "x": "9"}])
    joined = evidence.run_query(store, "join", {"key": 'm["a,b"]'})
    assert [(r["hit_a"], r["hit_b"]) for r in joined["rows"]] == [(1, 2), (2, 1)]
    # A key that never repeats: no note.
    assert "notes" not in joined
    joined = evidence.run_query(store, "join", {"key": ' m["a,b"] , x'})
    assert joined["columns"][:2] == ['m["a,b"]', "x"]
    assert [(r["hit_a"], r["hit_b"], r["matched"]) for r in joined["rows"]] == [
        (2, 1, "both"),
        (1, None, "only in run a"),
        (None, 2, "only in run b"),
    ]
    with pytest.raises(ValueError, match="key names a selector, or several"):
        evidence.run_query(store, "join", {"key": "x,,y"})
    with pytest.raises(ValueError, match="at most 8 selectors"):
        evidence.run_query(store, "join", {"key": ",".join("abcdefghi")})
    with pytest.raises(ValueError, match="run 1 captured no 'y'"):
        evidence.run_query(store, "join", {"key": "x,y"})


def test_values_alike_at_every_hit_say_nothing_where_most_others_differ(store):
    # Review 2 of PR #209, 1: `key` 1, 2 alike in both runs, and `call` A, B against
    # C, D (no value of one at another hit of the other): "the hits line up by `key`",
    # though nothing else agreed.
    saved(store, [{"key": "1", "call": "'A'"}, {"key": "2", "call": "'B'"}])
    saved(store, [{"key": "1", "call": "'C'"}, {"key": "2", "call": "'D'"}])
    (note,) = alignment(notes(store, 1, 2))
    assert note.startswith(
        "`key` agrees between the runs at each of the 2 hits, but `call` differs at 2 of "
        "them: the values do not show that a hit is the same moment in both"
    ), note
    assert "line up by" not in note


def test_an_intervention_in_another_function_is_named_with_it(store):
    # Review 2 of PR #209, 2: a --set-at of `helper`'s local `x`, and the probed line's
    # `main` `x` read A, B against B, A. Whatever its name, a value that differs after a
    # change may follow from it (tenth evaluation), so the reorder is no sign the hits do
    # not line up; the note says so, and names where the change was.
    saved(store, [{"x": "'A'"}, {"x": "'B'"}], function="main")
    saved(
        store,
        [{"x": "'B'"}, {"x": "'A'"}],
        function="main",
        set_at=[(5, 1, "x", "9")],
        changes=[(1, "x", "1", "9", 0, "helper")],
    )
    assert alignment(notes(store, 1, 2)) == [
        "the values cannot show whether the hits line up: `x` differs after the interventions "
        "(run 2's on `x` in helper) and may follow from them, and no other selector both "
        "agrees between the runs and changes from hit to hit"
    ]
    # The same name read alike at every hit after it never says the hits line up.
    saved(store, [{"x": "'A'", "y": "1"}, {"x": "'B'", "y": "1"}], function="main")
    saved(
        store,
        [{"x": "'A'", "y": "2"}, {"x": "'B'", "y": "1"}],
        function="main",
        set_at=[(5, 1, "x", "9")],
        changes=[(1, "x", "1", "9", 0, "helper")],
    )
    found = alignment(notes(store, 3, 4))
    assert not any("line up" in note and "`x`" in note.split(":")[0] for note in found), found


def test_selectors_that_differ_together_withhold_the_claim(store):
    # Review 3 of PR #209: `key` 1 to 6, alike and different at every hit; `p` and `q`
    # each line up best hit by hit, and differ at only 3 of 6 hits, but (p, q) together
    # pair 4 hits elsewhere against 3 hit by hit. Combinations are not checked, so where
    # two selectors differ the claim is withheld.
    pairs_a = ["00", "00", "01", "11", "10", "22"]
    pairs_b = ["11", "00", "01", "10", "00", "22"]
    for pairs in (pairs_a, pairs_b):
        saved(store, [{"key": str(n), "p": pq[0], "q": pq[1]} for n, pq in enumerate(pairs, 1)])
    (note,) = alignment(notes(store, 1, 2))
    assert note == (
        "the hits line up by `key` as far as each selector alone shows: it agrees between the "
        "runs at each of the 6 hits, and its value is different at every hit, and no single "
        "selector's values line up better at other hits; `p` and `q` differ, and how they "
        "line up together was not checked"
    )
    # One differing selector: as before.
    saved(store, [{"key": str(n), "p": pq[0]} for n, pq in enumerate(pairs_a, 1)])
    saved(store, [{"key": str(n), "p": pq[0]} for n, pq in enumerate(pairs_b, 1)])
    (note,) = alignment(notes(store, 3, 4))
    assert note.startswith("the hits line up by `key`: it agrees between the runs"), note
