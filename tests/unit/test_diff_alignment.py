"""`queries diff` judging whether two runs' hits line up, and `queries join` by several
selectors (#201). The ninth agent evaluation's stores, cut down: wrap (C), whose
`--set-at` changed `lay.width`; roster (Rust), whose fix shifted every `width`; and
rechnung (Java), whose fix made one call fewer, so 20 of `diff`'s 22 rows were at
later hits."""

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
):
    """A run of ``hits`` (one {selector: value} per hit, observed), ``returned`` values
    (one per hit), ``after`` (hit, selector, value) after-set rows, ``set_at`` (line,
    hit, name, value) and its ``changes`` (hit, name, before, after)."""
    rows = [
        dict(hit=hit, phase="observed", line=line, function=function, selector=s, value=v)
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
            dict(hit=hit, name=name, before=b, after=a, line=line, capture_hit=hit)
            for hit, name, b, a in changes
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


def test_a_value_an_intervention_changed_is_no_sign_the_hits_do_not_line_up(store):
    # #201, wrap 2 and 3: `lay.width` 16 against 20 at all 8 hits, from run 2's
    # --set-at, was taken for "the runs' values differ at 8 of 8 hits, so they cannot
    # show that a hit is the same moment in both"; `column`, `word` and `(return)` are
    # alike at every hit.
    wrap(
        store,
        2,
        16,
        set_at=[(75, 1, "lay.width", "16")],
        changes=[(1, "lay.width", "20", "16")],
        label="set-width-16",
    )
    wrap(store, 2, 20, edit="--- a/wrap.c\n+++ b/wrap.c\n", label="candidate")
    found = alignment(notes(store, 1, 2))
    assert not any("differ at 8 of 8 hits" in note for note in found), found
    assert found == [
        "the hits line up as far as `column` and `word` show: they read the same at each "
        "of the 8 hits in both runs, and no value lines up better at other hits; but they "
        "read alike at some hits, which could still trade places"
    ]
    rows = evidence.run_query(store, "diff", {"a": 1, "b": 2})["rows"]
    assert {row["selector"] for row in rows if row["hit"] is not None} == {"lay.width"}


def test_values_alike_at_every_hit_outweigh_one_that_moved(store):
    # #201, wrap 1 and 3: `run 3's hit 6 matches run 1's hit 7: 14` though `word` is
    # alike at every hit: the fix moved `column`, not the calls. Read against `word`,
    # no other pairing of `column`'s values holds.
    wrap(store, 1, 20, label="issue-input")
    wrap(store, 2, 20, edit="--- a/wrap.c\n+++ b/wrap.c\n", label="candidate")
    found = alignment(notes(store, 1, 2))
    assert not any("at other hits (run" in note for note in found), found
    assert found[0].startswith("the hits line up as far as `word` shows: it reads the same")
    # Without `word`, nothing outweighs `column`: the warning stays.
    wrap(store, 1, 20, word=False)
    wrap(store, 2, 20, word=False, edit="--- a/wrap.c\n+++ b/wrap.c\n")
    (note,) = alignment(notes(store, 3, 4))
    assert note.startswith(
        "runs 3 and 4 saw `column`'s values at other hits (run 4's hit 6 matches run 3's hit 7: 14)"
    ), note


def test_a_real_reorder_is_still_said_where_values_alike_cannot_rule_it_out(store):
    # Never hidden: hits 1 and 2 read alike in `word` (5), and `column` traded places
    # there, which `word` cannot rule out.
    saved(
        store,
        [{"word": "5", "column": "3"}, {"word": "5", "column": "9"}, {"word": "3", "column": "15"}],
    )
    saved(
        store,
        [{"word": "5", "column": "9"}, {"word": "5", "column": "3"}, {"word": "3", "column": "15"}],
    )
    (note,) = alignment(notes(store, 1, 2))
    assert note.startswith("runs 1 and 2 saw the same values of `column` in another order"), note
    # Nor where values the runs did not change differ at most hits, beside one an
    # intervention changed.
    saved(store, [{"x": "1", "w": "20"}, {"x": "2", "w": "20"}, {"x": "3", "w": "20"}])
    saved(
        store,
        [{"x": "1", "w": "16"}, {"x": "7", "w": "16"}, {"x": "8", "w": "16"}],
        set_at=[(75, 1, "w", "16")],
        changes=[(1, "w", "20", "16")],
    )
    (note,) = alignment(notes(store, 3, 4))
    assert note.startswith("the runs' values differ at 2 of 3 hits"), note


def test_values_alike_at_every_hit_and_different_at_each_say_the_hits_line_up(store):
    # #201, roster 5 and 6: the fix counts characters, not bytes, so every name's
    # `width` is one less (6 7 3 -> 5 6 3): `run 6's hit 1 matches run 5's hit 2: 6 …
    # the calls may have come in another order`, naming `key=<selector>`, though
    # `title` is alike at every hit and tells them apart.
    titles = ['"Name"', '"City"', '"Pts"']

    def roster(widths, symbol, **options):
        hits = [{"title": t, "width": w} for t, w in zip(titles, widths)]
        return saved(
            store, hits, language="rust", function=f"roster::widest::{symbol}", line=43, **options
        )

    roster(
        ["5", "6", "3"], "he3b8016ca05e3946", edit="--- a/src/main.rs\n", label="candidate-chars"
    )
    roster(["6", "7", "3"], "hb7185e602e967d61", label="original-43")
    (note,) = alignment(notes(store, 1, 2))
    assert note == (
        "the hits line up by `title`: it reads the same at each of the 3 hits in both runs, "
        "and differently at every hit, so pairing hits by number pairs them as `queries "
        "join` by it would"
    )
    # roster 4: the same widths set by --set-at instead (after-set rows only there).
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
        changes=[(1, "width", "6", "5"), (2, "width", "7", "6")],
    )
    (note,) = alignment(notes(store, 3, 1))
    assert note.startswith("the hits line up by `title`"), note
    # Nothing differs: nothing to say.
    roster(["6", "7", "3"], "hb7185e602e967d61")
    assert alignment(notes(store, 2, 4)) == []


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
    assert count.startswith("run 1 stopped 8 times at its line and run 2 6"), count
    assert count.endswith(
        "`peeky queries join --param a=1 --param b=2 --param key=<selector>[,<selector>…]`"
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
    (note,) = one["notes"]
    assert note == (
        'the key repeats: run 1\'s hits 1, 7 and 8 all have `neu.bezeichnung` "Mutter", and '
        "such a value pairs every hit that has it in one run with every one in the other "
        "(16 rows, from 3 values): to tell those hits apart, add a selector that names the "
        "call to the key (key=neu.bezeichnung,<selector>)"
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
    assert note.startswith(
        'the key repeats: run 1\'s hits 1 and 7 both have `p.bezeichnung` "Schraube" and '
        '`neu.bezeichnung` "Mutter", and such a value pairs every hit that has it in one run '
        "with every one in the other (4 rows, from 1 value)"
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
