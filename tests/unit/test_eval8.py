"""The eighth agent evaluation's findings that need no debugger: the evidence store's
comparisons, and CLI and probe text."""

import pytest


def recorded(rows, label=None, **probe):
    """A probe result of (hit, selector, value[, function[, line]]) observed rows."""
    hits = max([row[0] for row in rows], default=0)
    return {
        "probe": {
            "program": "Calc.java",
            "line": 24,
            "capture": list(dict.fromkeys(row[1] for row in rows)),
            "set": [],
            "hits": "all",
            "set_hit": 1,
            "args": [],
            **probe,
        },
        "status": "completed",
        "hits": hits,
        "seconds": 0.5,
        "pauses_ms": [1.0],
        "rows": [
            {
                "hit": hit,
                "phase": "observed",
                "line": rest[1] if len(rest) > 1 else 24,
                "function": rest[0] if rest else "Calc.parseExpr()",
                "selector": selector,
                "type": "char",
                "value": value,
            }
            for hit, selector, value, *rest in rows
        ],
        "by_hit": [],
        "interventions": [],
        "warnings": [],
        "output": "",
        "target_identity": {"language": "java"},
        "source_sha256": "0" * 64,
    }


@pytest.fixture
def store(tmp_path):
    return tmp_path / "evidence" / "evidence.db"


def saved(store, rows, label=None, **probe):
    from peeky import evidence

    return evidence.record(store, recorded(rows, **probe), label=label, language="java")


def test_diff_says_when_the_same_values_came_in_another_order(store):
    # M6: an edit changed how parseExpr's calls nest; both runs stopped 5 times, so no
    # note came, and `op '+' -> '-'` at hit 4 read as if the edit changed an operator.
    from peeky import evidence

    saved(store, [(n, "op", v) for n, v in enumerate(["'-'", "'-'", "'-'", "'+'", "'-'"], 1)])
    saved(store, [(n, "op", v) for n, v in enumerate(["'-'", "'-'", "'-'", "'-'", "'+'"], 1)])
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith("runs 1 and 2 saw the same values of `op` in another order"), note
    assert "(run 2's hit 4 matches run 1's hit 5: '-'): the calls may have come" in note, note
    assert "peeky queries join --param a=1 --param b=2 --param key=<selector>" in note
    # Review of M6 (PR #189): join by a value the edit leaves alone, not `op`.
    assert "a value that names the call and that the edit leaves alone" in note, note
    # The same values in the same order, or other values: no note.
    saved(store, [(n, "op", v) for n, v in enumerate(["'-'", "'-'", "'-'", "'-'", "'+'"], 1)])
    assert "notes" not in evidence.run_query(store, "diff")
    saved(store, [(n, "op", v) for n, v in enumerate(["'*'", "'-'", "'-'", "'-'", "'+'"], 1)])
    assert "notes" not in evidence.run_query(store, "diff")


def test_diff_says_when_a_hit_is_in_another_function_or_line(store):
    # M6: the same hit number at another function or line is another moment, unless the
    # places pair one to one across hits (review of M6, PR #189: a renamed function, lines
    # an edit moved).
    from peeky import evidence

    saved(store, [(1, "x", "1", "f", 24), (2, "x", "2", "f", 24), (3, "x", "5", "f", 24)])
    saved(store, [(1, "x", "1", "f", 24), (2, "x", "3", "g", 24), (3, "x", "5", "f", 24)])
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith("at hit 2 run 1 stopped in f at line 24 and run 2 in g at line 24"), note
    saved(store, [(1, "x", "1", "f", 24), (2, "x", "3", "g", 24), (3, "x", "5", "g", 24)])
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith(
        "at hit 3 run 2 stopped in f at line 24 and run 3 in g at line 24, but at hit 1 run 2 "
        "in f at line 24 paired with run 3 in f at line 24"
    ), note
    # Renamed and moved, the same way at every hit: the same moments.
    saved(store, [(1, "x", "1", "f2", 30), (2, "x", "3", "g", 26), (3, "x", "5", "g", 26)])
    assert "notes" not in evidence.run_query(store, "diff")


def test_diff_pairs_places_by_instance_too(store):
    # Review of M6 (PR #189), (a): Rust closures and generics differ by instance only.
    from peeky import evidence

    rows = [(1, "x", "1", "f", 24), (2, "x", "2", "f", 24), (3, "x", "3", "f", 24)]
    saved(store, rows)
    run = saved(store, rows)
    import sqlite3

    with sqlite3.connect(store) as db:
        db.execute("UPDATE captures SET instance = 'f::<u8>' WHERE run_id = ? AND hit = 3", (run,))
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith("at hit 3 run 1 stopped in f at line 24 and run 2 in f::<u8>"), note


def test_diff_compares_orders_without_addresses_or_object_numbers(store):
    # Review of M6 (PR #189), (b): values carrying addresses or Java object numbers
    # never matched, so their reorder went unseen.
    from peeky import evidence

    saved(store, [(1, "s", '0x000000016fdff2a0 "x"'), (2, "s", '0x000000016fdff2c0 "y"')])
    saved(store, [(1, "s", '0x000000016fdff3a0 "y"'), (2, "s", '0x000000016fdff3c0 "x"')])
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith("runs 1 and 2 saw the same values of `s` in another order"), note
    saved(store, [(1, "o", "Item@57 {k=1}"), (2, "o", "Item@58 {k=2}")])
    saved(store, [(1, "o", "Item@61 {k=2}"), (2, "o", "Item@62 {k=1}")])
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith("runs 3 and 4 saw the same values of `o` in another order"), note


def test_diff_says_when_values_line_up_at_other_hits(store):
    # Review of M6 (PR #189), (c): a call left out shifts the rest, values changing too.
    from peeky import evidence

    saved(store, [(1, "k", "'A'"), (2, "k", "'B'"), (3, "k", "'C'")])
    saved(store, [(1, "k", "'A'"), (2, "k", "'C'"), (3, "k", "'D'")])
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith(
        "runs 1 and 2 saw `k`'s values at other hits (run 2's hit 2 matches run 1's hit 3: "
        "'C'): the calls may have come in another order or number"
    ), note


def test_diff_says_when_values_cannot_show_the_hits_pair(store):
    # Review of M6 (PR #189): where most hits' values differ and nothing lines them up,
    # the note says so plainly rather than nothing.
    from peeky import evidence

    saved(store, [(1, "v", "1"), (2, "v", "2"), (3, "v", "3")])
    saved(store, [(1, "v", "1"), (2, "v", "7"), (3, "v", "8")])
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith(
        "the runs' values differ at 2 of 3 hits, so they cannot show that a hit is the same "
        "moment in both; if the edit changed which calls reach the line, pair hits by a value"
    ), note
    # One hit of three: no note.
    saved(store, [(1, "v", "1"), (2, "v", "7"), (3, "v", "9")])
    assert "notes" not in evidence.run_query(store, "diff")


def test_diff_compares_each_hits_values_by_selector(store):
    # Review 2 of PR #189: the fallback compared each hit's values as one multiset, so
    # `left=A, right=B` against `left=B, right=A` at every hit gave no note; and a value
    # missing from a hit counted as the text "None".
    from peeky import evidence

    saved(store, [(n, s, v) for n in (1, 2, 3) for s, v in (("left", "'A'"), ("right", "'B'"))])
    saved(store, [(n, s, v) for n in (1, 2, 3) for s, v in (("left", "'B'"), ("right", "'A'"))])
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith("the runs' values differ at 3 of 3 hits"), note
    saved(store, [(1, "v", "1"), (2, "v", "1"), (2, "x", "None"), (3, "v", "1")])
    saved(store, [(1, "v", "1"), (2, "v", "1"), (3, "v", "1"), (3, "x", "None")])
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith("the runs' values differ at 2 of 3 hits"), note
    # A selector only one run captured is not a difference at every hit (review 3).
    saved(store, [(1, "v", "1"), (2, "v", "1"), (3, "v", "1")])
    saved(store, [(n, s, v) for n in (1, 2, 3) for s, v in (("v", "1"), ("w", "5"))])
    assert "notes" not in evidence.run_query(store, "diff")
    # The same values in the same order at other hits are not "in another order".
    saved(store, [(1, "x", "2"), (2, "x", "3"), (3, "v", "1")])
    saved(store, [(1, "v", "1"), (2, "x", "2"), (3, "x", "3")])
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith("runs 7 and 8 saw `x`'s values at other hits"), note


def test_diff_and_compare_name_the_runs_labels(store):
    # L17: `queries diff` named runs by number only.
    from peeky import evidence

    saved(store, [(1, "x", "1")], label="mapped-public")
    saved(store, [(1, "x", "2")], label="mapped-private")
    for name in ("diff", "compare"):
        notes = evidence.run_query(store, name)["notes"]
        assert "labels: run 1 is 'mapped-public'; run 2 is 'mapped-private'" in notes, notes
    saved(store, [(1, "x", "3")])
    notes = evidence.run_query(store, "diff")["notes"]
    assert "labels: run 2 is 'mapped-private'; run 3 has no label" in notes, notes
    saved(store, [(1, "x", "4")])
    assert "notes" not in evidence.run_query(store, "diff")


def test_a_query_leaves_out_a_stop_column_no_row_fills(store):
    # L7: `queries set_effect` showed an empty `stop` column for a Python run.
    from peeky import evidence

    result = recorded([(1, "key", "'hello-world-'")], set=[["key", "'hello-world'"]])
    result["rows"].append({**result["rows"][0], "phase": "after-set", "value": "'hello-world'"})
    result["interventions"] = [
        {"hit": 1, "name": "key", "before": "'hello-world-'", "after": "'hello-world'"}
    ]
    evidence.record(store, result, language="python")
    for name in ("set_effect", "run"):
        assert "stop" not in evidence.run_query(store, name)["columns"], name


def test_session_hints_name_the_session():
    # L1: hints in `session get` children and state lines omitted `--name X`, so a pasted
    # one acted on the `default` session.
    from peeky.cli import _get_lines, _state_text

    running = _state_text({"state": "running"}, "calc")
    assert "`peeky session wait --name calc`" in running, running
    noted = _state_text(
        {
            "state": "stopped",
            "location": "Calc.java:24",
            "note": "stopped at the session's breakpoint (line 24, hit 2) before reaching "
            "line 30; `session continue --to 30` again",
        },
        "calc",
    )
    assert "`session continue --to 30 --name calc` again" in noted, noted
    assert "--name" not in _state_text({"state": "running"}), "default keeps the short hint"
    row = {"selector": "xs", "type": "int[]", "value": "[1, 2]"}
    row["children"] = [{"selector": "xs[0]", "name": "[0]", "value": "1", "expandable": True}]
    (line, child) = _get_lines(row, "calc")
    assert child.endswith("(children: peeky session get 'xs[0]' --name calc)"), child


def test_a_failing_verify_shows_its_only_output_line_once():
    # L5: the long `FAIL …` line came twice, as `reason:` and under `output:`.
    from peeky.cli import _verify_text

    line = "FAIL ['10-4-3']: got 'RESULTS:9' want 'RESULTS:3'"
    result = {
        "passed": False,
        "stage": "verification",
        "reason": line,
        "exit_code": 1,
        "stdout": line + "\n",
        "stderr": "",
        "diff": "-a\n+b",
    }
    text = _verify_text(result)
    assert text.count(line) == 1 and "output: the reason above, its only line" in text, text
    # With more output than the reason, the output is shown whole.
    text = _verify_text({**result, "stdout": "case 1 ok\n" + line + "\n"})
    assert text.count(line) == 2 and "case 1 ok" in text, text


def test_unchanged_after_set_rows_are_left_out_of_the_table():
    # L16: after-set rows repeated values the change never touched (`i`).
    from peeky.probe import _table

    result = recorded([(1, "path", '"/users/42/"'), (1, "i", "2")], set=[["path", "x"]])
    for selector, value in (("path", '"/users/42"'), ("i", "2")):
        result["rows"].append(
            {**result["rows"][0], "phase": "after-set", "selector": selector, "value": value}
        )
    result["interventions"] = [
        {
            "hit": 1,
            "capture_hit": 1,
            "name": "path",
            "before": '"/users/42/"',
            "after": '"/users/42"',
        }
    ]
    text = _table(result)
    assert "after-set  24    Calc.parseExpr()  path" in text, text
    assert "after-set  24    Calc.parseExpr()  i" not in text, text
    assert "after-set: 1 row equal to the hit's observed value left out" in text, text


def test_after_set_rows_that_may_hide_a_change_stay():
    # Review of L16 (PR #189): `s` showed its first fields only, so `s` after `--set
    # s.ninth=2` looked unchanged and was left out. A row whose selector holds a changed
    # value, or whose value is not shown whole (a pointer, a preview, a cut text, a note),
    # stays; only a whole scalar or text equal to before is left out.
    from peeky.probe import _table

    observed = [
        (1, "s", "{a = 1, b = 2, c = 3, ...}"),
        (1, "p", "0x000000016fdff2a0"),
        (1, "name", '"a long name…"'),
        (1, "n", "7"),
        (1, "word", "'apple'"),
    ]
    result = recorded(observed)
    for _, selector, value in observed:
        result["rows"].append(
            {**result["rows"][0], "phase": "after-set", "selector": selector, "value": value}
        )
    result["interventions"] = [
        {"hit": 1, "capture_hit": 1, "name": "s.ninth", "before": "0", "after": "2"}
    ]
    text = _table(result)
    for selector in ("s", "p", "name"):
        assert f"after-set  24    Calc.parseExpr()  {selector} " in text, text
    for selector in ("n", "word"):
        assert f"after-set  24    Calc.parseExpr()  {selector} " not in text, text
    assert "after-set: 2 rows equal to the hit's observed value left out" in text, text


def test_after_set_objects_stay_where_their_text_cannot_show_them_unchanged():
    # Review 2 of PR #189: with `b = a`, `a[0]="x"` makes a new Java String, which `b[0]`
    # shows as the same text: a Java object row stays, a primitive equal to before is
    # left out. A Python value whose identity changed stays too.
    from peeky.probe import _table

    observed = [(1, "b[0]", '"x"'), (1, "n", "7")]
    result = recorded(observed)
    result["rows"][0]["type"], result["rows"][1]["type"] = "String", "int"
    for row in list(result["rows"]):
        result["rows"].append({**row, "phase": "after-set"})
    result["interventions"] = [
        {"hit": 1, "capture_hit": 1, "name": "a[0]", "before": '"x"', "after": '"x"'}
    ]
    text = _table(result)
    assert "after-set  24    Calc.parseExpr()  b[0]" in text, text
    assert "after-set  24    Calc.parseExpr()  n " not in text, text

    result = recorded([(1, "t", "'abc'")], target_identity=None)
    result["target_identity"] = None
    result["rows"][0]["identity"] = 6
    result["rows"].append({**result["rows"][0], "phase": "after-set", "identity": 9})
    result["interventions"] = [
        {"hit": 1, "capture_hit": 1, "name": "s", "before": "1", "after": "2"}
    ]
    assert "after-set  24    Calc.parseExpr()  t" in _table(result)


def test_compare_hint_leaves_out_a_candidates_set(tmp_path, monkeypatch, capsys):
    # L21: after a candidate probe with --set (of a variable only the edit declares), the
    # hint said to run it without --old/--new, which kept the impossible --set.
    import hashlib

    import peeky.cli as cli_module

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "a.py").write_text("x = 1\n")
    result = recorded([(1, "x", "1")], set=[["fresh", "3"]])
    result["probe"].update(program="a.py", line=1, capture=["x"])
    result["candidate"] = {"sha256": hashlib.sha256(b"x = 2\n").hexdigest()}
    result["inputs_sha256"] = "a" * 64
    monkeypatch.setattr(cli_module, "run_probe", lambda *args, **kwargs: dict(result))
    argv = ["probe", str(tmp_path / "a.py"), "--at", "1", "--capture", "x", "--set", "fresh=3"]
    assert cli_module.main([*argv, "--old", "x = 1", "--new", "x = 2"]) == 0
    text = capsys.readouterr().out
    assert "run it without --old/--new and without its --set/--set-at, then" in text, text


def test_an_inferred_exit_says_what_it_costs_and_what_to_do(monkeypatch):
    # L4: "Peeky inferred it because no threads were left (java-debug, issue #27)" left
    # the agent wondering whether to act.
    import peeky.probe as probe_module
    from peeky.probe import Probe

    class Ended:
        waited = False

        def dispatch(self, operation, args):
            if operation == "launch":
                return {
                    "session_id": "s",
                    "breakpoints": [{"id": 1, "verified": True, "line": 8}],
                    "artifact_manifest_id": None,
                    "target_identity": None,
                    "source_sha256": "x",
                }
            if operation == "wait":
                if self.waited:
                    return {"state": "terminated", "exit_inferred": True}
                self.waited = True
                return {
                    "state": "stopped",
                    "stop_id": "t",
                    "event": {"body": {"reason": "breakpoint"}},
                }
            if operation == "inspect":
                return {
                    "frames": [{"source": {"path": "/w/main.py"}, "line": 8}],
                    "scopes": [
                        {
                            "name": "Locals",
                            "reference": 1,
                            "variables": [{"name": "x", "value": "1", "variablesReference": 0}],
                        }
                    ],
                }
            if operation == "report":
                return {"output": "", "output_truncated": False}
            return {}

        def close(self):
            pass

    monkeypatch.setattr(probe_module, "DebugWorker", lambda *args, **kwargs: Ended())
    result = probe_module.run_probe("/w", Probe("main.py", 8, ("x",)))
    assert result["status"] == "completed", result
    (warning,) = [w for w in result["warnings"] if "never reported" in w]
    assert "exit code is unknown" in warning and "nothing to do" in warning, warning


def test_a_python_candidates_copy_note_is_short_once_and_quiet(tmp_path, monkeypatch, capsys):
    # L6: a one-file, standard-library-only script got a 70-word note on its copied root,
    # printed twice (as progress and with the result), once even with --quiet.
    import json

    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    case = tmp_path / "case"
    case.mkdir()
    alone = case / "words.py"
    alone.write_text(
        "import re\nimport sys\n\n\ndef slug(t):\n    return '-'.join(re.split(r'\\W+', t))\n\n\n"
        "total = 0\nfor arg in sys.argv[1:]:\n    total += len(slug(arg))\nprint(total)\n"
    )
    probe = ["probe", str(alone), "--at", "11", "--capture", "total", "--json"]
    edit = ["--old", "total = 0", "--new", "total = 1"]
    assert main([*probe, *edit, "--", "a b"]) == 0
    result = json.loads(capsys.readouterr().out)
    # It reaches nothing outside its own file: no note.
    assert result["status"] == "completed" and not result.get("warnings"), result

    # One that does (`__file__`) keeps it: in text once, then not again for this root.
    reaching = case / "conf.py"
    reaching.write_text(
        "from pathlib import Path\nhere = Path(__file__).parent\ntotal = 0\nprint(total, here)\n"
    )
    probe = ["probe", str(reaching), "--at", "4", "--capture", "total"]
    edit = ["--old", "total = 0", "--new", "total = 1"]
    assert main([*probe, *edit]) == 0
    captured = capsys.readouterr()
    shown = captured.out + captured.err
    assert shown.count("ran from a copy") == 1 and len(shown.split("(no .git")) == 2, shown
    assert main([*probe, *edit]) == 0
    captured = capsys.readouterr()
    assert "ran from a copy" not in captured.out + captured.err, captured
    assert main([*probe, "--json", *edit]) == 0
    assert any("ran from a copy" in w for w in json.loads(capsys.readouterr().out)["warnings"])
    # --quiet never shows it, even the first time.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "quiet"))
    assert main([*probe, "--quiet", *edit]) == 0
    captured = capsys.readouterr()
    assert "ran from a copy" not in captured.out + captured.err, captured
