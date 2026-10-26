"""`queries diff` judging whether two runs' hits line up, and `queries join` by several
selectors (#201). The ninth agent evaluation's stores, cut down: wrap (C), whose
`--set-at` changed `lay.width`; roster (Rust), whose fix shifted every `width`; and
rechnung (Java), whose fix made one call fewer, so 20 of `diff`'s 22 rows were at
later hits."""

import json
import re
import shlex
import sqlite3

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
    "if they don't, pair hits by a value that names the call and that the edit leaves alone (an "
    "argument, an index): `peeky queries join --param a={} --param b={} "
    "--param key=<selector>[,<selector>…]`"
)
# The verdict where a selector's values line up better at other hits.
REORDER = "hits may not line up: the calls may have come in another order or number\n- "


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
        "hits pair as far as `column` and `word` show: they agree between the runs at each of "
        "the 8 hits\n"
        "- but they read alike at some hits, which could still trade places\n"
        "- `lay.width` differs after the edit and the interventions (run 1's on `lay.width`) "
        "and may follow from them, so this check leaves it out"
    ]
    rows = evidence.run_query(store, "diff", {"a": 1, "b": 2})["rows"]
    assert {row["selector"] for row in rows if row["hit"] is not None} == {"lay.width"}


def test_values_alike_at_every_hit_and_one_that_moved_are_both_said(store):
    # #201, wrap 1 and 3: `run 3's hit 6 matches run 1's hit 7: 14`, though `word` is
    # alike at every hit. Which of them names the call is not known (review of PR
    # #209): the note says both. `column` differs after the edit, which may account for
    # its values at other hits (tenth evaluation), but they still keep the note from
    # saying the hits line up (review of PR #269).
    wrap(store, 1, 20, label="issue-input")
    wrap(store, 2, 20, edit="--- a/wrap.c\n+++ b/wrap.c\n", label="candidate")
    (note,) = alignment(notes(store, 1, 2))
    assert note == (
        f"{REORDER}runs 1 and 2 saw `column`'s values at other hits (run 2's hit 6 matches run "
        "1's hit 7: 14)\n"
        "- `column` may differ because of the edit, so this does not prove a reorder\n"
        "- `word` agrees between the runs at each of the 8 hits, but it reads alike at some "
        "hits, which could still trade places\n"
        "- no captured selector tells the hits apart, so `queries join` could not pair them"
    )
    assert "hits line up" not in note and "pair by" not in note and "pair as far" not in note
    # Without `word`, the sign alone.
    wrap(store, 1, 20, word=False)
    wrap(store, 2, 20, word=False, edit="--- a/wrap.c\n+++ b/wrap.c\n")
    (note,) = alignment(notes(store, 3, 4))
    assert note.startswith(
        f"{REORDER}runs 3 and 4 saw `column`'s values at other hits (run 4's hit 6 matches run "
        "3's hit 7: 14)\n- `column` may differ because of the edit, so this does not prove a "
        "reorder"
    ), note


def test_a_value_alike_at_every_hit_never_hides_another_pairing(store):
    # Review of PR #209, 2: `key` 1, 2 in both runs (a counter, or alike by chance) hid
    # `call` A, B against B, A: "the hits line up by `key`".
    saved(store, [{"key": "1", "call": "'A'"}, {"key": "2", "call": "'B'"}])
    saved(store, [{"key": "1", "call": "'B'"}, {"key": "2", "call": "'A'"}])
    (note,) = alignment(notes(store, 1, 2))
    assert note.startswith(
        "hits may not line up: the values do not settle how they pair\n"
        "- `key` agrees between the runs at each of the 2 hits\n"
        "- but runs 1 and 2 saw the same values of `call` in another order (run 2's hit 1 "
        "matches run 1's hit 2: 'B')"
    ), note
    assert "line up by" not in note and "pair by" not in note
    # `key` tells the hits apart, so `join` could pair them by it: it is named.
    assert note.endswith("\n- " + JOIN.format(1, 2)), note
    # The same where `key` follows from a value an intervention set (`gleich` from `x`):
    # `call`'s order may follow from it too (tenth evaluation). Nothing says the hits line
    # up (review of PR #269); `gleich` pairs every hit, so `call`'s other order is left out
    # with `x` (twelfth evaluation: it read as a reorder beside a key).
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
        "hits pair by `gleich`: it agrees between the runs at each of the 2 hits and is unique "
        "across these hits\n"
        "- pairing by hit number pairs them as `peeky queries join --param a=3 --param b=4 "
        "--param key=gleich` would, but that does not prove the hits are the same moments\n"
        "- `x` and `call` differ after the interventions (run 4's on `x`) and may follow from "
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
    assert "- but runs 5 and 6 saw the same values of `column` in another order" in note, note


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
    assert note.startswith(f"{REORDER}runs 1 and 2 saw the same values of `call` in another order")
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
        f"{REORDER}runs 1 and 2 saw the same values of `x` in another order (run 2's hit 1 "
        "matches run 1's hit 2: 'B')"
    ), note


def test_values_alike_at_every_hit_and_different_at_each_say_the_hits_line_up(store):
    # #201, roster: `title` is alike at every hit and unique across the hits.
    titles = ['"Name"', '"City"', '"Pts"']

    def roster(widths, symbol, **options):
        hits = [{"title": t, "width": w} for t, w in zip(titles, widths)]
        return saved(
            store, hits, language="rust", function=f"roster::widest::{symbol}", line=43, **options
        )

    # Nothing else suggests another pairing, but `width` differs after the edit and is
    # left out: what agrees is said, never that the hits line up (review of PR #269).
    def titled(a, b):
        return (
            "hits pair by `title`: it agrees between the runs at each of the 3 hits and is "
            "unique across these hits\n- pairing by hit number pairs them as `peeky queries join "
            f"--param a={a} --param b={b} --param key=title` would, but that does not prove the "
            "hits are the same moments"
        )

    roster(["5", "6", "3"], "he3b8016ca05e3946", edit="--- a/src/main.rs\n")
    roster(["5", "6", "9"], "hb7185e602e967d61")
    assert alignment(notes(store, 1, 2)) == [
        f"{titled(1, 2)}\n- `width` differs after the edit and may follow from it, so this check "
        "leaves it out"
    ]
    # roster 5 and 6: the fix counts characters, so every name's `width` is one less (6 7
    # 3 -> 5 6 3), which lines up at other hits. The edit may account for that (tenth
    # evaluation), and `title` pairs every hit, so that is not said (twelfth evaluation:
    # beside a key it read as a reorder); `width` is left out.
    roster(["6", "7", "3"], "hb7185e602e967d61", label="original-43")
    assert alignment(notes(store, 1, 3)) == [
        f"{titled(1, 3)}\n- `width` differs after the edit and may follow from it, so this "
        "check leaves it out"
    ]
    # roster 4: the widths set by --set-at instead, each after its hit's read. A like
    # name is no proof that a later read is of the changed variable (each hit is a new
    # call of `widest`), so `width` is left out, as the change may account for it.
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
        f"{titled(4, 1)}\n- `width` differs after the edit and the interventions (run 4's on "
        "`width`) and may follow from them, so this check leaves it out"
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
        "the first real difference at a hit is at hit 4; 19 of the 20 rows at a hit come after "
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
    (note,) = one["notes"]
    assert note == (
        "the key repeats: add a selector that names the call to the key "
        "(key=neu.bezeichnung,<selector>). Run 1's hits 1, 7 and 8 all have `neu.bezeichnung` "
        '"Mutter", and such a value pairs every hit that has it in one run with every one in '
        "the other (16 rows, from 3 values)"
    )
    two = evidence.run_query(store, "join", {"key": "p.bezeichnung,neu.bezeichnung"})
    assert two["columns"][:4] == ["p.bezeichnung", "neu.bezeichnung", "hit_a", "hit_b"]
    assert "a:p.bezeichnung" not in two["columns"] and "a:gleich" in two["columns"]
    paired = [(r["hit_a"], r["hit_b"]) for r in two["rows"] if r["matched"] == "both"]
    assert paired == [(1, 1), (1, 5), (2, 2), (3, 3), (4, 4), (7, 1), (7, 5), (8, 6)]
    unpaired = [
        (r["hit_a"], r["hit_b"], r["matched"]) for r in two["rows"] if r["matched"] != "both"
    ]
    assert unpaired == [(5, None, "only in run a"), (6, None, "only in run a")]
    (note,) = two["notes"]
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
        "hits may not line up: `call` differs at 2 of the 2 hits\n- `key` agrees between the "
        "runs at each of the 2 hits, but that does not show that a hit is the same moment in "
        "both"
    ), note
    assert "line up by" not in note


def test_an_intervention_on_another_functions_variable_never_hides_a_reorder(store):
    # Review 2 of PR #209, 2: a --set-at of `helper`'s local `x` left every hit of the
    # probed line's `main` `x` out, so its reorder (A, B against B, A) went unsaid. A
    # change may account for it (tenth evaluation), but the reorder is still said, with
    # where the change was (review of PR #269).
    saved(store, [{"x": "'A'"}, {"x": "'B'"}], function="main")
    saved(
        store,
        [{"x": "'B'"}, {"x": "'A'"}],
        function="main",
        set_at=[(5, 1, "x", "9")],
        changes=[(1, "x", "1", "9", 0, "helper")],
    )
    assert alignment(notes(store, 1, 2)) == [
        f"{REORDER}runs 1 and 2 saw the same values of `x` in another order (run 2's hit 1 "
        "matches run 1's hit 2: 'B')\n- `x` may differ because of the interventions (run 2's "
        "on `x` in helper), so this does not prove a reorder\n- " + JOIN.format(1, 2)
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
    assert not any("hits line up" in note for note in found), found


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
        "hits pair by `key`: it agrees between the runs at each of the 6 hits and is unique "
        "across these hits\n"
        "- pairing by hit number pairs them as `peeky queries join --param a=1 --param b=2 "
        "--param key=key` would, but that does not prove the hits are the same moments\n"
        "- no single selector's values line up better at other hits\n"
        "- `p` and `q` differ, and how they line up together was not checked"
    )
    # One differing selector: as before.
    saved(store, [{"key": str(n), "p": pq[0]} for n, pq in enumerate(pairs_a, 1)])
    saved(store, [{"key": str(n), "p": pq[0]} for n, pq in enumerate(pairs_b, 1)])
    (note,) = alignment(notes(store, 3, 4))
    assert note.startswith("hits pair by `key`: it agrees between the runs"), note
    assert "together was not checked" not in note, note


# The eleventh evaluation: three agents could not find the verdict in the note's one long
# sentence, where a key paired every hit and the sign of a reorder was the edit's doing.
# rota (Rust), line 47: `week` 1 to 10; the fix swaps `pick` at weeks 6 and 7. grades
# (C++), line 66: `name` per student; the fix resets `stats.lowest`, and `quiz` 88 comes at
# another hit. seats (Java), line 54: `name` per group; the fix moves `r` and `first`.
PICKS = ["0", "2", "3", "0", "1", "1", "2", "3", "0", "1"]
STUDENTS = ['"Maya"', '"Leon"', '"Ines"', '"Omar"']
# grades' `name` is a `const std::string &`: its address, then its text; the addresses
# differ between the runs (review 2 of PR #308).
NAMED = {
    run: [f"0x000000{base}{n}0 {name}" for n, name in enumerate(STUDENTS, 1)]
    for run, base in (("a", "1010e156"), ("b", "1055715f"))
}
GROUPS = ['"Kim"', '"Okafor"', '"Lund"', '"Ng"', '"Sato"']
EVAL11 = {
    "rota": (
        [{"week": str(week), "pick": pick} for week, pick in enumerate(PICKS, 1)],
        [
            {"week": str(week), "pick": {6: "2", 7: "1"}.get(week, pick)}
            for week, pick in enumerate(PICKS, 1)
        ],
        "week",
        "pick",
    ),
    "grades": (
        [
            {"name": n, "quiz": q, "stats.lowest": "58"}
            for n, q in zip(NAMED["a"], ["87", "88", "90", "95"])
        ],
        [
            {"name": n, "quiz": q, "stats.lowest": low}
            for n, q, low in zip(NAMED["b"], ["87", "84", "87", "88"], ["58", "72", "64", "79"])
        ],
        "name",
        "quiz",
    ),
    "seats": (
        [
            {"name": n, "r": r, "first": f}
            for n, r, f in zip(GROUPS, "22323", ["2", "4", "6", "0", "1"])
        ],
        [
            {"name": n, "r": r, "first": f}
            for n, r, f in zip(GROUPS, "32121", ["3", "0", "0", "5", "5"])
        ],
        "name",
        "first",
    ),
}
VERDICTS = (
    "hits pair by ",
    "hits pair as far as ",
    "hits may not line up: ",
)
# What each case's note says last: every selector the edit may have moved is left out,
# the one whose values line up better at other hits too.
LEFT_OUT = {
    "grades": "- `quiz` and `stats.lowest` differ after the edit and may follow from it, so this "
    "check leaves them out",
    "rota": "- `pick` differs after the edit and may follow from it, so this check leaves it out",
    "seats": "- `r` and `first` differ after the edit and may follow from it, so this check "
    "leaves them out",
}


def short(note):
    """The note's lines, after checking its shape: a verdict, then each detail on a line
    of its own after `- `, in sentences of at most 40 words (inline code is one word, as
    in the docs). None says the hits line up (review of PR #308)."""
    verdict, *details = note.split("\n")
    assert verdict.startswith(VERDICTS), note
    assert "hits line up" not in note and "weak" not in note, note
    assert all(detail.startswith("- ") for detail in details), note
    for line in [verdict, *details]:
        for sentence in re.split(r"(?<=[.:;])\s+", re.sub(r"`[^`]*`", "X", line)):
            assert len(sentence.split()) <= 40, sentence
    return [verdict, *details]


@pytest.mark.parametrize("case", sorted(EVAL11))
def test_diff_leads_with_its_verdict_where_a_key_pairs_every_hit(store, case):
    before, after, key, moved = EVAL11[case]
    saved(store, before, label="original")
    saved(store, after, edit="--- a/x\n+++ b/x\n", label="candidate")
    (note,) = alignment(notes(store, 1, 2))
    verdict, same, *rest = short(note)
    # grades' `name` agrees only with addresses masked, so plain `join` would pair none.
    masked = case == "grades"
    assert verdict == (
        f"hits pair by `{key}`: it agrees between the runs at each of the {len(before)} hits "
        f"{'with addresses masked ' if masked else ''}and is unique across these hits"
    ), note
    # Never that the hits are the same moments; the `join` named pairs them as said.
    assert same == (
        "- pairing by hit number pairs them as `peeky queries join --param a=1 --param b=2 "
        f"--param key={key}{' --param masked=1' if masked else ''}` would, but that does not "
        "prove the hits are the same moments"
    ), note
    assert [(r["hit_a"], r["hit_b"], r["matched"]) for r in suggested_join(store, note)] == [
        (hit, hit, "both") for hit in range(1, len(before) + 1)
    ], note
    assert "hits line up" not in note and "may not line up" not in note, note
    # The key pairs every hit, so a left-out value's alternate alignment is not said
    # (twelfth evaluation: after `hits pair by p.name`, "saw the same values of `desk` in
    # another order … does not settle whether the calls were reordered" read as a
    # reorder); the value is named with the others left out.
    assert rest == [LEFT_OUT[case]], note
    # The text table keeps the details under the verdict's `note:`.
    _, text = evidence.canned(store, "diff", {"a": 1, "b": 2}, table=True)
    lines = text.splitlines()
    first = lines.index(f"note: {verdict}")
    assert lines[first + 1 : first + 1 + len(rest) + 1] == [f"  {same}"] + [
        f"  {line}" for line in rest
    ], text


def test_every_alignment_note_leads_with_its_verdict(store):
    # Each of the notes `diff` gives where both runs stopped as often: the verdict first.
    edit = {"edit": "--- a/x\n+++ b/x\n"}
    cases = [
        # Places don't pair.
        (
            [{"x": "1"}, {"x": "2"}, {"x": "3"}],
            [{"x": "1"}, {"x": "2"}, {"x": "3"}],
            {},
            "hits may not line up: where the runs stopped",
        ),
        # The same values in another order; with a value alike at every hit too.
        ([{"k": "'A'"}, {"k": "'B'"}], [{"k": "'B'"}, {"k": "'A'"}], {}, "hits may not line up"),
        (
            [{"i": "1", "k": "'A'"}, {"i": "2", "k": "'B'"}],
            [{"i": "1", "k": "'B'"}, {"i": "2", "k": "'A'"}],
            {},
            "hits may not line up: the values do not settle how they pair",
        ),
        # Most hits differ.
        (
            [{"v": "1"}, {"v": "2"}, {"v": "3"}],
            [{"v": "1"}, {"v": "7"}, {"v": "8"}],
            {},
            "hits may not line up: the runs' values differ at 2 of 3 hits",
        ),
        # A key, and no change; after an edit; alike hits after an edit; nothing agrees.
        (
            [{"i": "1", "v": "5"}, {"i": "2", "v": "6"}, {"i": "3", "v": "7"}],
            [{"i": "1", "v": "5"}, {"i": "2", "v": "6"}, {"i": "3", "v": "9"}],
            {},
            "hits pair by `i`: it agrees",
        ),
        (
            [{"i": "1", "v": "5"}, {"i": "2", "v": "6"}, {"i": "3", "v": "7"}],
            [{"i": "1", "v": "5"}, {"i": "2", "v": "6"}, {"i": "3", "v": "9"}],
            edit,
            "hits pair by `i`: it agrees",
        ),
        (
            [{"w": "1", "v": "5"}, {"w": "1", "v": "6"}, {"w": "2", "v": "7"}],
            [{"w": "1", "v": "5"}, {"w": "1", "v": "6"}, {"w": "2", "v": "9"}],
            edit,
            "hits pair as far as `w` shows: it agrees",
        ),
        (
            [{"v": "1"}, {"v": "2"}],
            [{"v": "3"}, {"v": "4"}],
            edit,
            "hits may not line up: the values cannot show whether they do",
        ),
    ]
    for run, (before, after, options, verdict) in enumerate(cases):
        a = saved(store, before)
        b = saved(store, after, function="g" if run == 0 else "fits", **options)
        if run == 0:
            # Run b stopped in `g` only at its second hit.
            with sqlite3.connect(store) as db:
                db.execute(
                    "UPDATE captures SET function = 'fits' WHERE run_id = ? AND hit != 2", (b,)
                )
        (note,) = alignment(notes(store, a, b))
        assert short(note)[0].startswith(verdict), (verdict, note)


def test_a_value_unique_at_every_hit_pairs_the_hits_but_never_proves_they_line_up(store):
    # Review of PR #308, 1: with no edit and no intervention, run 1's moments A, B, C, D
    # and run 2's B, A, C, D (other arguments, or a program that runs differently each
    # time) both read `i` 1 to 4, and `x` differs at hit 4 only: "the hits line up by
    # `i`", though the first two moments are swapped. `i` agreeing, and unique across
    # the hits, proves only that hit-number pairing is what `join` by `i` gives.
    saved(store, [{"i": str(i), "x": x} for i, x in zip("1234", "0001")])
    saved(store, [{"i": str(i), "x": x} for i, x in zip("1234", "0002")])
    (note,) = alignment(notes(store, 1, 2))
    assert note == (
        "hits pair by `i`: it agrees between the runs at each of the 4 hits and is unique "
        "across these hits\n"
        "- pairing by hit number pairs them as `peeky queries join --param a=1 --param b=2 "
        "--param key=i` would, but that does not prove the hits are the same moments\n"
        "- no single selector's values line up better at other hits"
    ), note
    # Nor as far as a value that repeats shows.
    saved(store, [{"w": w, "x": x} for w, x in zip("1122", "0001")])
    saved(store, [{"w": w, "x": x} for w, x in zip("1122", "0002")])
    (note,) = alignment(notes(store, 3, 4))
    assert note.startswith("hits pair as far as `w` shows: it agrees between the runs"), note
    assert "hits line up" not in note, note


PEOPLE = ['"Ana"', '"Ben"', '"Cleo"', '"Dev"', '"Eli"', '"Fay"', '"Gus"']
FREE = {
    "a": ["0, 0, 0", "490, 0, 0", "490, 484, 0", "490, 484, 497", "490, 496, 497"]
    + ["499, 496, 497", "499, 505, 497"],
    "b": ["0, 0, 0", "490, 0, 0", "490, 484, 0", "490, 484, 497", "496, 484, 497"]
    + ["496, 499, 497", "505, 499, 497"],
}


def desks(store, side, **options):
    """desks (Java, line 58): `p.name` per person; run b sets `desk` to 0 at hits 4 and 7
    (`--set-at 58@4 desk=0 --set-at 58@7 desk=0`), and `desk` then reads 1, 0 where it
    read 0, 1."""
    seen = {"a": "0121012", "b": "0121102"}[side]
    return saved(
        store,
        [
            {"p.name": name, "desk": desk, "free": f"int[3]@55 [{free}]"}
            for name, desk, free in zip(PEOPLE, seen, FREE[side])
        ],
        language="java",
        line=58,
        function="Desks.main(String[])",
        **options,
    )


def test_a_key_that_pairs_every_hit_leaves_another_order_out(store):
    # Twelfth evaluation (desks, rainfall, laps): after `hits pair by p.name`, "runs 1 and
    # 2 saw the same values of `desk` in another order (…)" and "that alternate alignment
    # does not settle whether the calls were reordered" read as a reorder. The key pairs
    # every hit already: the selector is left out with the others, and the caveat that
    # pairing by number does not prove the hits are the same moments stays.
    desks(store, "a")
    desks(
        store,
        "b",
        set_at=[(58, 4, "desk", "0"), (58, 7, "desk", "0")],
        changes=[(4, "desk", "1", "0", 4), (7, "desk", "2", "0", 7)],
        after=[(4, "desk", "0"), (7, "desk", "0")],
    )
    (note,) = alignment(notes(store, 1, 2))
    assert note == (
        "hits pair by `p.name`: it agrees between the runs at each of the 7 hits and is unique "
        "across these hits\n"
        "- pairing by hit number pairs them as `peeky queries join --param a=1 --param b=2 "
        "--param key=p.name` would, but that does not prove the hits are the same moments\n"
        "- `desk` and `free` differ after the interventions (run 2's on `desk`) and may follow "
        "from them, so this check leaves them out"
    ), note
    # The same after an edit (the candidate's `desk` 0 at hit 4), and after an edit that
    # swaps two calls where a counter-like key (`gleich`) agrees: review of PR #308, 2
    # said a key may be a counter, so the note grades no sign, weak or strong.
    desks(store, "b", edit="--- a/Desks.java\n+++ b/Desks.java\n")
    (note,) = alignment(notes(store, 1, 3))
    assert note.startswith("hits pair by `p.name`: "), note
    assert note.endswith(
        "\n- `desk` and `free` differ after the edit and may follow from it, so this check "
        "leaves them out"
    ), note
    saved(store, [{"gleich": "1", "call": "'A'"}, {"gleich": "2", "call": "'B'"}])
    saved(
        store,
        [{"gleich": "1", "call": "'B'"}, {"gleich": "2", "call": "'A'"}],
        edit="--- a/x\n+++ b/x\n",
    )
    (note,) = alignment(notes(store, 4, 5))
    assert note.endswith(
        "does not prove the hits are the same moments\n- `call` differs after the edit and may "
        "follow from it, so this check leaves it out"
    ), note
    for said in ("saw ", "another order", "reordered", "weak", "strong"):
        assert said not in note, note
    # With no key, the sign is still the verdict: `w` reads alike at hits 1 and 2.
    saved(store, [{"w": "1", "call": "'A'"}, {"w": "1", "call": "'B'"}, {"w": "2", "call": "'C'"}])
    saved(
        store,
        [{"w": "1", "call": "'B'"}, {"w": "1", "call": "'A'"}, {"w": "2", "call": "'C'"}],
        edit="--- a/x\n+++ b/x\n",
    )
    (note,) = alignment(notes(store, 6, 7))
    assert note.startswith(
        "hits may not line up: the calls may have come in another order or number\n- runs 6 "
        "and 7 saw the same values of `call` in another order (run 7's hit 1 matches run 6's "
        "hit 2: 'B')\n- `call` may differ because of the edit, so this does not prove a reorder"
    ), note


def suggested_join(store, note):
    """The rows of the `queries join` a note names, run as the note gives it (the shell's
    words, each `--param` value parsed as the CLI parses it)."""
    (command,) = re.findall(r"`(peeky queries join [^`]*)`", note)
    words = shlex.split(command)[3:]
    assert words[::2] == ["--param"] * (len(words) // 2), command
    return evidence.run_query(store, "join", evidence.parse_params(words[1::2]))["rows"]


def test_the_join_a_pairing_note_names_pairs_the_hits_as_it_says(store):
    # Review 2 of PR #308: alignment compares values with addresses masked, but plain
    # `join` compares the stored text. `name` (a C++ string reference: its address, then
    # its text) agrees at both hits only once addresses are masked, and the note said
    # "pairs them as `queries join` by it would", though `join --param key=name` paired
    # none (grades' `name` too).
    saved(
        store,
        [
            {"name": '0x0000000100003f90 "apple"', "n": "1"},
            {"name": '0x0000000100003fa0 "pear"', "n": "2"},
        ],
    )
    saved(
        store,
        [
            {"name": '0x000000016fdff690 "apple"', "n": "1"},
            {"name": '0x000000016fdff6a0 "pear"', "n": "3"},
        ],
    )
    (note,) = alignment(notes(store, 1, 2))
    verdict, same, *_ = short(note)
    assert verdict == (
        "hits pair by `name`: it agrees between the runs at each of the 2 hits with addresses "
        "masked and is unique across these hits"
    ), note
    assert same == (
        "- pairing by hit number pairs them as `peeky queries join --param a=1 --param b=2 "
        "--param key=name --param masked=1` would, but that does not prove the hits are the same "
        "moments"
    ), note
    rows = suggested_join(store, note)
    assert [(r["hit_a"], r["hit_b"], r["matched"]) for r in rows] == [
        (1, 1, "both"),
        (2, 2, "both"),
    ]
    # Without masked=1, join pairs none: the note must not name that.
    plain = evidence.run_query(store, "join", {"a": 1, "b": 2, "key": "name"})["rows"]
    assert all(r["matched"] != "both" for r in plain)
    # A key alike as stored: plain `join`, as the note names it.
    saved(store, [{"i": "1", "n": "1"}, {"i": "2", "n": "2"}, {"i": "3", "n": "3"}])
    saved(
        store, [{"i": "1", "n": "1"}, {"i": "2", "n": "2"}, {"i": "3", "n": "4"}], edit="--- a/x\n"
    )
    (note,) = alignment(notes(store, 3, 4))
    assert "masked" not in note, note
    assert "`peeky queries join --param a=3 --param b=4 --param key=i` would" in note, note
    rows = suggested_join(store, note)
    assert [(r["hit_a"], r["hit_b"], r["matched"]) for r in rows] == [
        (1, 1, "both"),
        (2, 2, "both"),
        (3, 3, "both"),
    ]


def test_join_guidance_names_masked_where_only_addressed_values_tell_hits_apart(store):
    # Review 2 of PR #308: "if they don't, pair hits by a value …: `peeky queries join …`"
    # where only `p`, which carries addresses, tells the hits apart: plain `join` by `p`
    # pairs none, so the command names masked=1, and pairs every hit.
    saved(store, [{"p": '0x0000000100003f90 "x"'}, {"p": '0x0000000100003fa0 "y"'}])
    saved(store, [{"p": '0x000000016fdff690 "y"'}, {"p": '0x000000016fdff6a0 "x"'}])
    (note,) = alignment(notes(store, 1, 2))
    assert note.startswith(REORDER), note
    assert note.endswith(
        "carry addresses, so match them masked: `peeky queries join --param a=1 --param b=2 "
        "--param key=<selector>[,<selector>…] --param masked=1`"
    ), note
    rows = evidence.run_query(store, "join", {"a": 1, "b": 2, "key": "p", "masked": 1})["rows"]
    assert [(r["hit_a"], r["hit_b"], r["matched"]) for r in rows] == [
        (1, 2, "both"),
        (2, 1, "both"),
    ]
    # A selector free of addresses that tells them apart too: plain `join`.
    saved(
        store,
        [{"p": '0x0000000100003f90 "x"', "k": "1"}, {"p": '0x0000000100003fa0 "y"', "k": "2"}],
    )
    saved(
        store,
        [{"p": '0x000000016fdff690 "y"', "k": "2"}, {"p": '0x000000016fdff6a0 "x"', "k": "1"}],
    )
    (note,) = alignment(notes(store, 3, 4))
    assert note.endswith("--param key=<selector>[,<selector>…]`"), note
    assert "masked" not in note, note


def indicators(count):
    """``count`` + 1 hits: hit i (1 to ``count``) has only `ki` 1, the last every `ki` 0,
    so only all of `k1` to `k<count>` together are unique across the hits; `x` differs
    at the last hit only."""
    return {
        side: [
            {**{f"k{j}": str(int(i == j)) for j in range(1, count + 1)}, "x": "0"}
            for i in range(1, count + 1)
        ]
        + [{**{f"k{j}": "0" for j in range(1, count + 1)}, "x": x}]
        for side, x in (("a", "1"), ("b", "2"))
    }


def test_a_pairing_note_names_only_a_join_that_join_takes(store):
    # Review 3 of PR #308: `join` takes at most 8 selectors, and the note named all nine
    # of `k1` to `k9`, so its command failed ("key names at most 8 selectors"). No 8 of
    # them are unique across the hits: the note names no `join`, and says why.
    hits = indicators(9)
    saved(store, hits["a"])
    saved(store, hits["b"])
    (note,) = alignment(notes(store, 1, 2))
    assert "peeky queries join" not in note, note
    assert short(note) == [
        "hits pair as far as `k1`, `k2`, `k3` and 6 more show: they agree between the runs at "
        "each of the 10 hits and together are unique across these hits",
        f"- but no `queries join` key of {evidence.MAX_KEYS} selectors or fewer among them was "
        "found unique across these hits",
        "- pairing by hit number agrees with their values, but that does not prove the hits are "
        "the same moments",
        "- no single selector's values line up better at other hits",
    ], note
    with pytest.raises(ValueError, match="key names at most 8 selectors"):
        evidence.run_query(
            store, "join", {"a": 1, "b": 2, "key": ",".join(f"k{i}" for i in range(1, 10))}
        )
    # Three are needed, and `join` takes three: the command is named, and pairs as said.
    hits = indicators(3)
    saved(store, hits["a"])
    saved(store, hits["b"])
    (note,) = alignment(notes(store, 3, 4))
    assert "`peeky queries join --param a=3 --param b=4 --param key=k1,k2,k3` would" in note
    assert [(r["hit_a"], r["hit_b"], r["matched"]) for r in suggested_join(store, note)] == [
        (hit, hit, "both") for hit in range(1, 5)
    ]
    # The fewest that are unique, not the first chosen greedily: `g` tells the most hits
    # apart alone, but `g` with `a`, `b` and `c` is one more than `a`, `b` and `c`.
    bits = [(n >> 2 & 1, n >> 1 & 1, n & 1) for n in range(8)]
    for x in "12":
        saved(
            store,
            [
                {"g": str(g), "a": str(i), "b": str(j), "c": str(k), "x": x if g == 3 else "0"}
                for g, (i, j, k) in zip([0, 1, 2, 3, 0, 0, 0, 0], bits)
            ],
        )
    (note,) = alignment(notes(store, 5, 6))
    assert note.startswith("hits pair by `a`, `b` and `c`: they agree"), note
    assert [(r["hit_a"], r["hit_b"], r["matched"]) for r in suggested_join(store, note)] == [
        (hit, hit, "both") for hit in range(1, 9)
    ]
