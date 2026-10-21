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
    assert "(from hit 4: '+' and '-'): the calls may have come in another order" in note, note
    assert "peeky queries join --param a=1 --param b=2 --param key=<selector>" in note
    # The same values in the same order, or other values: no note.
    saved(store, [(n, "op", v) for n, v in enumerate(["'-'", "'-'", "'-'", "'-'", "'+'"], 1)])
    assert "notes" not in evidence.run_query(store, "diff")
    saved(store, [(n, "op", v) for n, v in enumerate(["'*'", "'-'", "'-'", "'-'", "'+'"], 1)])
    assert "notes" not in evidence.run_query(store, "diff")


def test_diff_says_when_a_hit_is_in_another_function_or_line(store):
    # M6: the same hit number at another function, or at another line relative to the
    # run's first hit, is another moment.
    from peeky import evidence

    saved(store, [(1, "x", "1", "f", 24), (2, "x", "2", "f", 24)])
    saved(store, [(1, "x", "1", "f", 24), (2, "x", "3", "g", 24)])
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith("at hit 2 run 1 stopped in f at line 24 and run 2 in g at line 24"), note
    # A candidate's lines shift as a whole (line 25 for 24): the same moments.
    saved(store, [(1, "x", "1", "f", 25), (2, "x", "3", "g", 25)])
    assert "notes" not in evidence.run_query(store, "diff")
    saved(store, [(1, "x", "1", "f", 25), (2, "x", "3", "g", 30)])
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert "at hit 2 run 3 stopped in g at line 25 and run 4 in g at line 30" in note, note


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
