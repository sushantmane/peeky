"""What `peeky probe` prints: its table's rows, marks and notes, the changes it lists,
the run a candidate is compared with, and its exit code."""

import hashlib
import json

import pytest

import peeky.probe as probe_module
from peeky import cli
from peeky.cli import _probe_text, main
from peeky.probe import _table

WHOLE = (
    "trim_left(std::__1::basic_string<char, std::__1::char_traits<char>, "
    "std::__1::allocator<char>> const&)"
)


def probe_result(function=WHOLE, **extra):
    return {
        "probe": {
            "program": "a.cpp",
            "line": 3,
            "capture": ["i"],
            "set": [],
            "hits": 1,
            "set_hit": 1,
            "args": [],
        },
        "status": "completed",
        "hits": 1,
        "seconds": 0.5,
        "pauses_ms": [1.0],
        "rows": [
            {
                "hit": 1,
                "phase": "observed",
                "line": 3,
                "function": function,
                "selector": "i",
                "type": "int",
                "value": "1",
            }
        ],
        "by_hit": [],
        "interventions": [],
        "warnings": [],
        "output": "",
        # The build and the source as it ran (a candidate's: the unedited source's hash).
        "target_identity": {"language": "c", "optimization": "O0"},
        "source_sha256": "0" * 64,
        # Every source the build read (a candidate's before its edit).
        "inputs_sha256": "a" * 64,
        **extra,
    }


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """`peeky probe` on a fake run (``set_result``) in a fresh state directory."""
    import peeky.cli as cli_module

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "a.py").write_text("x = 1\n")
    shown = {}
    monkeypatch.setattr(cli_module, "run_probe", lambda *args, **kwargs: dict(shown["result"]))

    def probe(result, *extra):
        shown["result"] = result
        return cli_module.main(["probe", str(tmp_path / "a.py"), "--at", "1", *extra])

    return probe


def test_the_short_name_note_is_said_once_per_state_directory(probe, capsys):
    # Every probe repeated `note: function shows short frame names; ...`,
    # even with --quiet.
    note = "note: function shows short frame names"
    assert probe(probe_result(), "--quiet") == 0
    assert note not in capsys.readouterr().out
    assert probe(probe_result()) == 0
    first = capsys.readouterr().out
    assert note in first and "short_function(function) = 'trim_left'" in first, first
    assert probe(probe_result()) == 0
    again = capsys.readouterr().out
    assert note not in again and "trim_left " in again, again
    # The SQL text says it where SQL is used: `peeky sql --help` always, and a query's
    # table of a run that has not had it, once per run. Run 3's probe left it
    # out.

    with pytest.raises(SystemExit):
        main(["sql", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "short_function(function) = 'trim_left'" in help_text, help_text
    assert main(["queries", "run"]) == 0
    assert note in capsys.readouterr().out
    assert main(["queries", "run"]) == 0
    assert note not in capsys.readouterr().out


def test_the_instance_note_follows_the_same_rule(probe, capsys):
    # A probe table's instance labels (`instance = '...'`) are part
    # of the same note, said once per state directory and never with --quiet.
    closure = probe_result(function="cz::main::{{closure}}::h0123456789abcdef")
    closure["rows"][0]["instance"] = "cz::main::{closure#0}"
    assert probe(closure, "--quiet") == 0
    assert "note:" not in capsys.readouterr().out
    assert probe(closure) == 0
    first = capsys.readouterr().out
    assert "instance = 'cz::main::{closure#0}'" in first and " cz::main::{closure#0} " in first
    assert probe(closure) == 0
    again = capsys.readouterr().out
    assert "note:" not in again and " cz::main::{closure#0} " in again, again
    # `peeky sql --help` keeps it.

    with pytest.raises(SystemExit):
        main(["sql", "--help"])
    assert "instance = 'cz::main::{closure#0}'" in " ".join(capsys.readouterr().out.split())


def test_the_probe_exit_codes():
    # 2 for a probe refused before it ran, 4 for a completed probe
    # with a change not made, 1 for any other probe that did not complete.
    from peeky.probe import probe_exit_code

    refused = {"status": "failed", "refused_before_launch": True}
    assert probe_exit_code(refused) == 2
    assert probe_exit_code({"status": "completed", "unapplied": [{"name": "x"}]}) == 4
    for status in ("failed", "no-stop", "timeout"):
        assert probe_exit_code({"status": status}) == 1
    assert probe_exit_code({"status": "completed"}) == 0


def test_a_probe_refused_before_launch_shows_no_table_and_exits_2(monkeypatch, capsys):
    # The console script too printed an empty table header and exited 1.
    import peeky.probe as probe_module

    refused = probe_result(
        status="failed",
        rows=[],
        hits=0,
        refused_before_launch=True,
        error={
            "code": "MUTATION_REJECTED",
            "message": "`r.tail` cannot be changed. Refused before the program runs",
            "hint": "Change the value where it is owned.",
        },
    )
    monkeypatch.setattr(probe_module, "run_probe", lambda *args, **kwargs: refused)
    assert probe_module.main(["--at", "a.py:1", "--set", "x=1"]) == 2
    text = capsys.readouterr().out
    assert text.splitlines() == [
        "error [MUTATION_REJECTED]: `r.tail` cannot be changed. Refused before the program runs",
        "hint: Change the value where it is owned.",
    ]


def test_compare_names_no_run_whose_hits_are_at_other_stops(probe, capsys):
    # An edit that changes how often the `--at` line stops per
    # pass moves every later hit to another breakpoint location; such a run pairs by
    # value, not by `diff`'s hit number.
    import hashlib

    def run(candidate=False, stop=1):
        result = probe_result()
        result["probe"].update(program="a.py", line=1, capture=["i"])
        result["rows"][0].update(stop=stop, stops=2)
        if candidate:
            result["candidate"] = {"sha256": hashlib.sha256(b"x = 2\n").hexdigest()}
        edit = ["--old", "x = 1", "--new", "x = 2"] if candidate else []
        return probe(result, "--capture", "i", *edit, "--json")

    assert run() == 0  # run 1
    capsys.readouterr()
    assert run(candidate=True) == 0  # run 2: the same stops
    assert json.loads(capsys.readouterr().out)["evidence"]["compare"]["run"] == 1
    assert run(candidate=True, stop=2) == 0  # run 3: hit 1 at another stop
    compare = json.loads(capsys.readouterr().out)["evidence"]["compare"]
    assert compare["run"] is None, compare
    assert compare["hint"] == (
        "pair the hits by a value: `peeky queries join --param a=1 --param b=3 --param "
        "key=<selector>`. Run 1 is this probe without the edit, but at hit 1 it was at stop 1 "
        "of the line and this run at stop 2, so hits do not pair by number"
    )


def test_the_per_pass_warning_is_said_every_time_the_short_name_note_once(probe, capsys):
    # The warning that a line stops several times per pass changes
    # what hits count, so it is never once-only or quiet; the note on names is.
    warning = "Line 3 stops up to 2 times per pass in trim_left (2 breakpoint locations)"
    result = probe_result(warnings=[warning])
    result["rows"][0].update(stop=1, stops=2)
    note = "note: function shows short frame names"
    for flags, noted in ((["--quiet"], False), ([], True), ([], False)):
        assert probe(dict(result), *flags) == 0
        out = capsys.readouterr().out
        assert f"warning: {warning}" in out and (note in out) == noted, out
        assert out.startswith("hit  stop  phase") and "\n1    1/2   observed" in out, out


def test_a_candidate_probe_names_the_run_to_compare_it_with(probe, tmp_path, capsys):
    # After a candidate probe, a plain `queries diff` compares it with the
    # latest run, which may have other arguments or a `--set`; nothing named the run
    # that is the same probe without the edit.
    import hashlib

    def run(capture=("i",), args=(), sets=(), candidate=False, *flags):
        result = probe_result()
        result["probe"].update(
            program="a.py", line=1, capture=list(capture), args=list(args), set=list(sets)
        )
        if candidate:
            result["candidate"] = {"sha256": hashlib.sha256(b"x = 2\n").hexdigest()}
        extra = ["--capture", ",".join(capture), *[f"--set={'='.join(s)}" for s in sets]]
        extra += ["--old", "x = 1", "--new", "x = 2"] if candidate else []
        return probe(result, *extra, *flags, *(["--", *args] if args else []))

    assert run() == 0  # run 1: the baseline
    assert run(("i",), (), [("i", "3")]) == 0  # run 2: an intervention
    assert run(("i",), ["other"]) == 0  # run 3: another input
    capsys.readouterr()
    assert run(("i",), (), (), True) == 0  # run 4
    text = capsys.readouterr().out
    assert (
        "compare: peeky queries diff --param a=1 --param b=4 (run 1: this probe without the edit)"
    ) in text, text
    assert run(("i",), ["other"], (), True) == 0  # run 5: run 3 is its baseline
    assert "compare: peeky queries diff --param a=3 --param b=5" in capsys.readouterr().out
    assert run(("j",), (), (), True) == 0  # run 6: none matches
    text = capsys.readouterr().out
    assert "compare: no saved run is this probe without the edit" in text, text
    assert "run it without --old/--new, then `peeky queries diff`" in text, text
    # JSON says the same, and a probe without an edit says nothing.
    assert run(("i",), (), (), True, "--json") == 0
    compare = json.loads(capsys.readouterr().out)["evidence"]["compare"]
    assert compare == {"run": 1, "command": "peeky queries diff --param a=1 --param b=7"}
    assert run() == 0  # run 8
    assert "compare:" not in capsys.readouterr().out
    # A run with a `--set-at` changed a value too: it is no baseline.
    changed = probe_result()
    changed["probe"].update(program="a.py", line=1, capture=["i"], set_at=[[1, 2, "i", "3"]])
    assert probe(changed, "--capture", "i", "--set-at", "1@2", "i=3") == 0  # run 9
    assert run(("i",), (), (), True) == 0  # run 10
    assert "compare: peeky queries diff --param a=8 --param b=10" in capsys.readouterr().out


def test_the_compared_run_has_the_candidates_build_and_unedited_source(probe, capsys):
    # `compare:` named an -O1 run for an -O0 candidate, and a
    # run of the source as it was before the file changed on disk.
    import hashlib

    def run(candidate=False, optimization="O0", source="0" * 64):
        result = probe_result(source_sha256=source)
        result["target_identity"] = {"language": "c", "optimization": optimization}
        result["probe"].update(program="a.py", line=1, capture=["i"])
        if candidate:
            result["candidate"] = {"sha256": hashlib.sha256(b"x = 2\n").hexdigest()}
        edit = ["--old", "x = 1", "--new", "x = 2"] if candidate else []
        return probe(result, "--capture", "i", *edit)

    assert run() == 0  # run 1: the baseline
    assert run(optimization="O1") == 0  # run 2: another build
    assert run(source="1" * 64) == 0  # run 3: the file as it was before an edit on disk
    capsys.readouterr()
    assert run(candidate=True) == 0  # run 4
    assert "compare: peeky queries diff --param a=1 --param b=4" in capsys.readouterr().out
    # An -O1 candidate has no -O1 run of this source to compare with.
    assert run(candidate=True, optimization="O1", source="2" * 64) == 0
    text = capsys.readouterr().out
    assert "compare: no saved run is this probe without the edit" in text, text
    assert "build, of the sources as they are now" in text, text


def test_the_compared_run_read_the_same_sources(probe, capsys):
    # After another source file changed on disk, `compare:`
    # still named a run of the old file, and `diff` credited the change to the edit.
    import hashlib

    def run(candidate=False, inputs="a" * 64):
        result = probe_result(inputs_sha256=inputs)
        result["probe"].update(program="a.py", line=1, capture=["i"])
        if candidate:
            result["candidate"] = {"sha256": hashlib.sha256(b"x = 2\n").hexdigest()}
        if inputs is None:
            del result["inputs_sha256"]
        edit = ["--old", "x = 1", "--new", "x = 2"] if candidate else []
        return probe(result, "--capture", "i", *edit)

    assert run() == 0  # run 1: main.c as it was
    capsys.readouterr()
    assert run(candidate=True, inputs="b" * 64) == 0  # run 2: main.c changed on disk
    text = capsys.readouterr().out
    assert "compare: no saved run is this probe without the edit" in text, text
    assert run(inputs="b" * 64) == 0  # run 3
    capsys.readouterr()
    assert run(candidate=True, inputs="b" * 64) == 0  # run 4
    assert "compare: peeky queries diff --param a=3 --param b=4" in capsys.readouterr().out
    # The store keeps the digest with the build's identity, for SQL too.

    sql = "SELECT json_extract(target_identity, '$.inputs_sha256') AS i FROM runs WHERE run_id = 4"
    assert main(["sql", sql, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == [{"i": "b" * 64}]
    # Sources Peeky could not read (a large Python tree): no run is named.
    import peeky.cli as cli_module

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli_module, "_python_inputs", lambda root: None)
        assert run(candidate=True, inputs=None) == 0
    text = capsys.readouterr().out
    assert "compare: Peeky cannot tell which saved run read the same sources" in text, text


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


def test_unchanged_after_set_rows_are_left_out_of_the_table():
    # After-set rows repeated values the change never touched (`i`).
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
    assert "after-set: 1 number, flag or char row equal to the observed value left out" in text, (
        text
    )


def test_after_set_rows_that_may_hide_a_change_stay():
    # `s` showed its first fields only, so `s` after `--set
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
    assert "after-set: 2 number, flag or char rows equal to the observed value left out" in text, (
        text
    )


def test_after_set_objects_stay_where_their_text_cannot_show_them_unchanged():
    # With `b = a`, `a[0]="x"` makes a new Java String, which `b[0]`
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
    # After a candidate probe with --set (of a variable only the edit declares), the
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


VISIBLE = "count, e, expenses, friends, iter @ main.rs:64, iter @ main.rs:74, k, name, payer, share"


def _observed_row(hit, selector, **fields):
    return {"hit": hit, "phase": "observed", "line": 76, "selector": selector, **fields}


def test_a_probe_table_lists_the_visible_names_once():
    # At line 76, every stop in `settle` (not the closure) restated the frame's
    # ten names for `f` and again for `f.name`, 16 times in a 28-hit probe.
    missing = {"function": "kitty::settle", "error": f"not found; visible: {VISIBLE}"}
    rows = [
        _observed_row(1, "f", **missing),
        _observed_row(1, "f.name", **missing),
        _observed_row(1, "name", function="kitty::settle", type="&str *", value='"Åsa"'),
        _observed_row(6, "f", **missing),
        _observed_row(6, "f.name", **missing),
    ]
    result = {"rows": rows, "interventions": [], "status": "completed", "hits": 6, "seconds": 1}
    text = _table(result, short_names=False)
    assert text.count(VISIBLE) == 1, text
    assert text.count("<not found; visible: as listed at hit 1>") == 3, text
    # The rows themselves, as JSON and the store keep them, are unchanged.
    assert all(row["error"].endswith(VISIBLE) for row in rows if "error" in row)


def test_a_different_list_or_hint_is_shown_whole():
    other = "not found; visible: a, b"
    hinted = f"not found; visible: {VISIBLE}; select it as listed if that is the one you mean"
    rows = [
        _observed_row(1, "x", error=f"not found; visible: {VISIBLE}"),
        _observed_row(1, "y", error=other),
        _observed_row(2, "z", error=hinted),
        _observed_row(3, "x", error=f"not found; visible: {VISIBLE}"),
    ]
    text = _table(
        {"rows": rows, "interventions": [], "status": "completed", "hits": 3, "seconds": 1}
    )
    assert other in text and hinted in text, text
    assert "<not found; visible: as listed at hit 1>" in text, text


def _long_result(selectors, hits):
    rows = [
        {"hit": hit, "phase": "observed", "line": 80, "selector": name, "type": "int", "value": "0"}
        for hit in range(1, hits + 1)
        for name in selectors
    ]
    return {
        "rows": rows,
        "interventions": [],
        "status": "completed",
        "hits": hits,
        "seconds": 1,
        "evidence": {"run_id": 7, "next": "peeky queries run --param run=7"},
    }


def test_a_long_probe_table_points_at_one_line_per_hit():
    # 5 selectors at 32 hits printed 160 lines; `queries wide` gives 32.
    pointer = (
        "evidence: one line per hit, a column per selector: `peeky queries wide --param run=7`"
    )
    long = _probe_text(_long_result(["ms", "db.stable", "db.candidate", "db.count", "changes"], 32))
    assert long.endswith(pointer), long[-300:]
    # A short table, or one selector, needs none.
    assert pointer not in _probe_text(_long_result(["ms", "db.count"], 3))
    assert pointer not in _probe_text(_long_result(["ms"], 60))


def _result(rows=(), interventions=(), language="cpp", **more):
    return {
        "rows": list(rows),
        "interventions": list(interventions),
        "status": "completed",
        "hits": 1,
        "seconds": 1.0,
        "target_identity": {"language": language},
        **more,
    }


def _plan_row(selector, value, phase="observed", hit=1, **fields):
    row = {"hit": hit, "phase": phase, "line": 47, "function": "rota::plan"}
    return {**row, "selector": selector, "type": "u32", "value": value, **fields}


def _all(name, pairs, line=26, function="main"):
    """An @all's changes at hits 1, 2, …: (before, after) each."""
    return [
        {
            "name": name,
            "before": before,
            "after": after,
            "line": line,
            "hit": hit,
            "capture_hit": 1,
            "function": function,
            "at": f"{line}@{hit}",
            "all": True,
        }
        for hit, (before, after) in enumerate(pairs, 1)
    ]


def test_the_after_set_line_says_only_scalar_rows_are_left_out():
    # `after-set: 1 row equal to the hit's observed value left out`, while `team`'s
    # after-set row, equal to its observed one, stayed: only whole scalars are left out.
    team = '[Engineer { name: "Ana", shifts: 1, .. }, …]'
    rows = [_plan_row("week", "6"), _plan_row("team", team)]
    rows += [_plan_row("week", "6", "after-set"), _plan_row("team", team, "after-set")]
    change = {"name": "pick", "before": "1", "after": "2", "hit": 1, "capture_hit": 1}
    text = _table(_result(rows, [change], language="rust"))
    assert "after-set  47    rota::plan  team" in text, text
    assert "after-set  47    rota::plan  week" not in text, text
    assert (
        "after-set: 1 number, flag, null or text row equal to the observed value left out; `--json` "
        "has all"
    ) in text, text


def test_the_after_set_line_says_why_an_equal_java_string_stays():
    # desks: `p.name  String  "Dev"` stayed among the after-set rows, equal to its
    # observed one, under a line that said whole texts go.
    rows = [_plan_row("free", "3", type="int"), _plan_row("p.name", '"Dev"', type="String")]
    rows += [
        _plan_row("free", "3", "after-set", type="int"),
        _plan_row("p.name", '"Dev"', "after-set", type="String"),
    ]
    change = {"name": "seat", "before": "1", "after": "2", "hit": 1, "capture_hit": 1}
    text = _table(_result(rows, [change], language="java"))
    assert "after-set  47    rota::plan  p.name" in text, text
    assert "after-set  47    rota::plan  free" not in text, text
    assert (
        "after-set: 1 number, flag or char row equal to the observed value left out (a String "
        "stays: it is an object); `--json` has all"
    ) in text, text


def test_one_change_past_the_first_few_is_shown_whole():
    # `set stats.lowest: 1 more change by --set-at line 61@all, at hits 4
    # to 4`, where the change itself fits on that line.
    changes = _all("stats.lowest", [("101", "101"), ("58", "101"), ("72", "101"), ("64", "101")])
    text = _table(_result(interventions=changes))
    assert (
        "set stats.lowest: 64 -> 101  (--set-at line 26@all, stop 4 of line 26, in main)" in text
    ), text
    assert "more change" not in text and "more writes" not in text, text
    # Two or more past the first few are counted; at one hit, by its number.
    more = _all("n", [("1", "2")] * 5)
    text = _table(_result(interventions=more))
    assert "set n: 2 more changes by --set-at line 26@all, at stops 4 to 5 of line 26 (" in text, (
        text
    )
    more[4]["hit"] = 4
    text = _table(_result(interventions=more))
    assert "set n: 2 more changes by --set-at line 26@all, at stop 4 of line 26 (" in text, text


def _proven(changes):
    """The worker's proof that a write left the value as it was (worker._written)."""
    return [
        {**change, "unchanged": True} if change["before"] == change["after"] else change
        for change in changes
    ]


def test_a_write_of_the_value_already_there_is_marked_and_counted_apart():
    # `set free: 0 -> 0` counted as a change; 35 of the @all's 42 writes were.
    pairs = [("3", "0"), ("0", "0"), ("3", "0"), ("0", "0"), ("2", "0"), ("0", "0"), ("1", "0")]
    changes = _proven(_all("free", pairs, function="Seats.block(boolean[],int)"))
    text = _table(_result(interventions=changes, language="java"))
    assert "set free: 3 -> 0  (--set-at line 26@all, stop 1 of line 26, in Seats.block(" in text, (
        text
    )
    assert (
        "set free: 0 -> 0, no change  (--set-at line 26@all, stop 2 of line 26, in Seats.block("
        in text
    )
    assert (
        "set free: 4 more writes by --set-at line 26@all, at stops 4 to 7 of line 26: 2 changes and 2 with "
        "no change (`--json` lists each)"
    ) in text, text
    every = _proven(_all("free", [("0", "0")] * 5))
    text = _table(_result(interventions=every, language="java"))
    assert (
        "set free: 2 more writes by --set-at line 26@all, at stops 4 to 5 of line 26, each with no"
        in text
    )
    # java-debug shows a char bare, so five `+ -> +` writes were "2 more
    # changes". The worker's proof decides it, for a root local (`sign`) and a field alike.
    for name in ("sign", "chars.mark"):
        chars = _proven(_all(name, [("+", "+")] * 5))
        text = _table(_result(interventions=chars, language="java"))
        assert (
            f"set {name}: + -> +, no change  (--set-at line 26@all, stop 3 of line 26, in main)"
            in text
        )
        assert (
            f"set {name}: 2 more writes by --set-at line 26@all, at stops 4 to 5 of line 26, each with no "
            "change (`--json` lists each)"
        ) in text, text
    # The text alone is never proof (a Java String, a Python `1000`, a float, or
    # an older worker's result). Nor are such writes changes; each kind is
    # counted apart, as NO_STOP counts them.
    alike = _all("x", [("1000", "1000")] * 5, line=9)
    text = _table(_result(interventions=alike, language="python"))
    assert (
        "set x: 1000 -> 1000, same text, not proven unchanged  (--set-at line 9@all, stop 1 of line 9, in "
        "main)"
    ) in text, text
    assert (
        "set x: 2 more writes by --set-at line 9@all, at stops 4 to 5 of line 9, each with the same text, "
        "not proven unchanged (`--json` lists each)"
    ) in text, text
    assert "more change" not in text, text
    pairs = [("1", "1")] * 3 + [("1", "2"), ("1", "1"), ("2", "2"), ("3", "4"), ("5", "5")]
    mixed = _all("x", pairs)
    for index in (4, 5):
        mixed[index]["unchanged"] = True
    text = _table(_result(interventions=mixed))
    assert (
        "set x: 5 more writes by --set-at line 26@all, at stops 4 to 8 of line 26: 2 changes, 2 with no "
        "change and 1 with the same text, not proven unchanged (`--json` lists each)"
    ) in text, text
    assert not probe_module.unchanged({"before": "1", "after": "1"})
    assert not probe_module.unchanged({"before": "1", "after": "2", "unchanged": True})
    assert probe_module.unchanged({"before": "1", "after": "1", "unchanged": True})
    # One change, not an @all's, is marked too.
    single = [{"name": "x", "before": "5", "after": "5", "hit": 1, "unchanged": True}]
    assert "set x: 5 -> 5, no change" in _table(_result(interventions=single))


def test_a_return_row_names_the_line_it_returned_to():
    # `returned 49 Seats.block(...)`, where line 49 is in `book`, the caller.
    rows = [
        {
            "hit": 1,
            "phase": "returned",
            "line": 49,
            "function": "Seats.block(boolean[],int)",
            "selector": "(return)",
            "type": "int",
            "value": "2",
        }
    ]
    text = _table(_result(rows, language="java"))
    assert "1    returned  to 49  Seats.block(boolean[],int)  (return)  int   2" in text, text


def _tally_result(rows=(), interventions=(), language="rust", **more):
    return {
        "rows": list(rows),
        "interventions": list(interventions),
        "status": "completed",
        "hits": 1,
        "seconds": 1.0,
        "target_identity": {"language": language},
        **more,
    }


def _tally_row(selector, value, phase="observed", hit=1, type="u32", **fields):
    row = {"hit": hit, "phase": phase, "line": 63, "function": "tally::count"}
    return {**row, "selector": selector, "type": type, "value": value, **fields}


def test_the_after_set_footer_is_short():
    # The footer ran to 190 characters under every probe with a change.
    rows = [_tally_row("week", "6"), _tally_row("week", "6", "after-set")]
    change = {"name": "pick", "before": "1", "after": "2", "hit": 1, "capture_hit": 1}
    (footer,) = [
        line
        for line in _table(_tally_result(rows, [change])).splitlines()
        if line.startswith("after")
    ]
    assert footer == (
        "after-set: 1 number, flag, null or text row equal to the observed value left out; `--json` "
        "has all"
    )
    rows = [_tally_row("free", "3", type="int"), _tally_row("free", "3", "after-set", type="int")]
    text = _table(_tally_result(rows, [change], language="java"))
    assert (
        "after-set: 1 number, flag or char row equal to the observed value left out (a String "
        "stays: it is an object); `--json` has all"
    ) in text, text


def test_a_java_threads_mark_counts_the_jvms_own():
    # A program of one thread read `other threads are running`.
    from peeky.java import _THREADS_RUNNING

    assert _THREADS_RUNNING.startswith("other threads, the JVM's own included, are running")


def test_a_final_fields_mark_shows_on_its_selectors_first_row_only():
    # `p.name … [not settable: final]` on every row, after-set rows too.
    rows = [
        _tally_row("p.name", '"Ana"', hit=hit, phase=phase, type="String", not_settable="final")
        for hit in (1, 2)
        for phase in ("observed", "after-set")
    ]
    rows.append(_tally_row("p.desk", "3", type="int", not_settable="final"))
    rows.append(_tally_row("p.load", "4", type="int", not_settable="a constant javac compiles"))
    rows.append(
        _tally_row("p.load", "5", hit=2, type="int", not_settable="a constant javac compiles")
    )
    result = _tally_result(rows, [{"name": "seat", "before": "1", "after": "2", "hit": 1}], "java")
    text = _table(result)
    assert text.count("[not settable: final]") == 2, text
    lines = text.splitlines()
    first = next(line for line in lines if "p.name" in line)
    assert first.endswith("[not settable: final]"), text
    # Other reasons may differ by stop: each row keeps its own.
    assert text.count("[not settable: a constant javac compiles]") == 2, text
    # So does another language's row, and JSON keeps every mark.
    rust = _table(
        _tally_result([_tally_row("x", "1", not_settable="final", hit=h) for h in (1, 2)])
    )
    assert rust.count("[not settable: final]") == 2, rust
    assert all(row["not_settable"] for row in rows)


def test_an_all_change_names_its_own_lines_stop():
    # `(--set-at line 52@all, hit 1, in summarize)` read as the probe's hit 1.
    changes = [
        {
            "name": "ready",
            "before": "True",
            "after": "False",
            "line": 52,
            "hit": hit,
            "capture_hit": hit,
            "function": "summarize",
            "at": f"52@{hit}",
            "all": True,
        }
        for hit in range(1, 7)
    ]
    changes[1].update(stop=2, stops=3)
    text = _table(_tally_result(interventions=changes, language="python"))
    assert "set ready: True -> False  (--set-at line 52@all, stop 1 of line 52, in summarize)" in (
        text
    ), text
    assert "(--set-at line 52@all, stop 2 of line 52 at location 2 of 3, in summarize)" in text
    assert "set ready: 3 more changes by --set-at line 52@all, at stops 4 to 6 of line 52" in text
    assert ", hit " not in text and "at hits" not in text, text


def test_many_selectors_at_several_hits_point_at_queries_wide():
    # 6 selectors at 6 hits, 36 rows, printed no pointer to one line per hit.
    def result(selectors, hits):
        rows = [_tally_row(name, "0", hit=hit) for hit in range(1, hits + 1) for name in selectors]
        return _tally_result(
            rows, evidence={"run_id": 4, "next": "peeky queries run --param run=4"}
        )

    pointer = (
        "evidence: one line per hit, a column per selector: `peeky queries wide --param run=4`"
    )
    assert pointer in cli._probe_text(result(["name", "day", "mm", "open", "run", "first"], 6))
    assert pointer in cli._probe_text(result(["a", "b", "c", "d"], 3))
    assert pointer not in cli._probe_text(result(["a", "b", "c"], 6))
    assert pointer not in cli._probe_text(result(["a", "b", "c", "d"], 2))


def test_a_rust_reference_to_a_struct_says_why_only_its_address_shows():
    # `read  &laps::Read  0x…` showed no preview, mark or note, beside `read.car`.
    rows = [
        _tally_row("read", "0x000000010536d900", type="&tally::Read"),
        _tally_row("read.car", '"7"', type="String", view="memory"),
        _tally_row("car", '"7"', type="&str", view="memory"),
        _tally_row("v", "0x000000010536d000", type="&alloc::vec::Vec<u32>"),
        _tally_row("p", "0x000000010536d000", type="*const tally::Read", unproven_pointer="raw"),
    ]
    text = _table(_tally_result(rows))
    assert (
        "note: `read` shows only the address a reference holds: Peeky previews a struct "
        "through a reference only for a reference parameter whose fields are all plain "
        "scalars; its fields read by name, as `read.car`"
    ) in text, text
    assert text.count("note: ") == 1, text
    # `&app::State` may be an enum, with no field to name; a field
    # selector is named only where one read.
    for more in ([], [_tally_row("state.mode", "", error="not found: 'mode'")]):
        shown = _table(
            _tally_result([_tally_row("state", "0x1000", type="&mut app::State"), *more])
        )
        assert (
            "note: `state` shows only the address a reference holds; its row reads nothing "
            "through it"
        ) in shown, shown
        assert "field" not in shown and "struct" not in shown, shown
    two = [_tally_row(name, "0x1000", type="&app::State") for name in ("a", "b")]
    assert "`a`, `b` show only the address a reference holds; no row reads through them" in (
        _table(_tally_result(two))
    )
    # A C pointer, a previewed reference, and a row with a note get none.
    assert "only the address" not in _table(_tally_result(rows[:1], language="cpp"))
    previewed = _tally_row("read", "Read { at: 7 }", type="&tally::Read", view="memory")
    assert "only the address" not in _table(_tally_result([previewed]))


REASON = (
    "not shown: the binding refers to an object Peeky can't prove is still alive: `{}` binds an "
    "element of `by_hour` by reference, and Peeky proves only standard vectors, maps and sets of "
    "numbers, with the default allocator and order. Read it where the code names that object"
)


def _run(rows, *, candidate=False, output="BUSIEST:3\n"):
    return {
        "probe": {
            "program": "nightlot.cpp",
            "line": 40,
            "capture": list(dict.fromkeys(row["selector"] for row in rows)),
            "set": [],
            "hits": "all",
            "set_hit": 1,
            "args": ["18:30+"],
        },
        "status": "completed",
        "hits": max(row["hit"] for row in rows),
        "seconds": 1.0,
        "pauses_ms": [1.0],
        "output": output,
        "rows": rows,
        "interventions": [],
        "warnings": [],
        "target_identity": {"language": "cpp"},
        **({"candidate": {"diff": "--- a\n+++ b\n", "sha256": "1" * 64}} if candidate else {}),
    }


def _bindings(withheld, inside):
    rows = []
    for hit, (hour, cars) in enumerate([(0, 3), (1, -1), (2, -2), (18, 1)], 1):
        for selector, value in (("hour", hour), ("cars", cars)):
            row = {"hit": hit, "phase": "observed", "line": 40, "selector": selector}
            if withheld:
                row.update(type="const int", unavailable=True, error=REASON.format(selector))
            else:
                row.update(type="const int", value=str(value))
            rows.append(row)
        rows.append(
            {
                "hit": hit,
                "phase": "observed",
                "line": 40,
                "selector": "inside",
                "type": "long",
                "value": str(inside[hit - 1]),
            }
        )
    return rows


def test_a_probe_table_gives_a_long_reason_once_per_selector():
    # The same 300-character reason on every row, 18 times.
    rows = _bindings(True, [0, 1, 2, 5])
    text = _table({**_run(rows), "hits": 4}, short_names=False)
    for selector in ("hour", "cars"):
        assert text.count(REASON.format(selector)) == 1, text
    assert text.count("<not shown: same as at hit 1>") == 6, text
    # Short reasons, and the rows themselves, are kept whole.
    assert all(row["error"].startswith("not shown: the binding") for row in rows if "error" in row)
    short = [
        {"hit": hit, "phase": "observed", "line": 4, "selector": "x", "error": "no value yet"}
        for hit in (1, 2)
    ]
    assert _table(_run(short), short_names=False).count("<no value yet>") == 2


def test_a_cut_preview_says_how_to_read_the_rest():
    # `size=9 {…, 3: 1, …}` stopped at 8 entries, with no word on the ninth.
    row = {"hit": 1, "phase": "observed", "line": 46, "function": "main"}
    rows = [
        {
            **row,
            "selector": "by_hour",
            "type": "std::map<int, long, (lambda at nightlot.cpp:23:24)>",
            "value": "size=9 {18: 1, 20: 2, 22: 1, 23: 2, 0: 5, 1: 4, 2: 2, 3: 1, …}",
        },
        {**row, "selector": "v", "type": "std::vector<int>", "value": "size=20 [1, 2, …]"},
        {**row, "selector": "s", "type": "std::set<int>", "value": "size=3 {1, 2, 3}"},
        {**row, "selector": "n", "type": "Nest", "value": "size=1 {1: size=9 {1, 2, …}}"},
        {**row, "selector": "t", "type": "Ids", "value": "size=30 {1, 2, 3, …}"},
    ]
    text = _table(_run(rows), short_names=False)
    assert (
        "preview: `by_hour` holds 9 entries, more than its preview shows; read one with "
        "`by_hour[K]` (key K) or `by_hour[#N]` (the Nth), or list them in a session: "
        "`peeky session get by_hour`"
    ) in text, text
    assert "preview: `v` holds 20 elements" in text and "`v[N]` (index N)" in text, text
    # A typedef Peeky can't name: by position, and by key for a map.
    assert "`t[#N]` (the Nth; a map's `[K]`, key K)" in text, text
    assert "`s`" not in text.split("preview:", 1)[1] and "preview: `n`" not in text, text
    # Not for other languages' previews.
    python = {**_run(rows), "target_identity": {"language": "python"}}
    assert "preview:" not in _table(python, short_names=False)


@pytest.fixture
def cpp_probe(tmp_path, monkeypatch):
    """`peeky probe` of a stand-in result, saved to a state directory of its own."""
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "a.cpp").write_text("int main() { return 0; }\n")
    shown = {}
    monkeypatch.setattr(cli, "run_probe", lambda *args, **kwargs: dict(shown["result"]))

    def run(result, *extra):
        shown["result"] = result
        return cli.main(["probe", str(tmp_path / "a.cpp"), "--at", "1", *extra])

    return run


def _identified(rows, candidate=False):
    result = _run(rows)
    result["probe"]["program"] = "a.cpp"
    result.update(
        target_identity={"language": "cpp", "optimization": "O0"},
        source_sha256="0" * 64,
        inputs_sha256="a" * 64,
    )
    if candidate:
        # The edited text's digest, as the build that ran it reports it.
        result["candidate"] = {"sha256": hashlib.sha256(b"int main() { return 1; }\n").hexdigest()}
    return result


EDIT = ("--old", "return 0;", "--new", "return 1;")


def test_compare_names_the_run_whose_captures_differ(cpp_probe, capsys):
    # `compare: no saved run is this probe without the edit …` until the plain
    # probe was run again with the same captures, one selector apart.
    rows = _bindings(False, [0, 1, 2, 3])
    plain = [row for row in rows if row["selector"] in ("hour", "inside")]
    assert cpp_probe(_identified(plain), "--capture", "hour,inside") == 0  # run 1
    capsys.readouterr()
    assert cpp_probe(_identified(rows, True), "--capture", "hour,cars,inside", *EDIT) == 0
    text = capsys.readouterr().out
    assert (
        "compare: run 1 is this probe without the edit, but it didn't capture `cars`: `peeky "
        "queries diff --param a=1 --param b=2` compares the selectors both captured (`hour` and "
        "`inside`). To compare them all, run this probe without --old/--new, then `peeky "
        "queries diff`"
    ) in text, text
    assert cpp_probe(_identified(rows, True), "--capture", "hour,cars,inside", *EDIT, "--json") == 0
    compare = json.loads(capsys.readouterr().out)["evidence"]["compare"]
    assert compare["run"] is None and "didn't capture `cars`" in compare["hint"], compare
    # A run with the same captures is still the one named.
    assert cpp_probe(_identified(rows), "--capture", "hour,cars,inside") == 0  # run 4
    capsys.readouterr()
    assert cpp_probe(_identified(rows, True), "--capture", "hour,cars,inside", *EDIT) == 0
    assert "compare: peeky queries diff --param a=4 --param b=5" in capsys.readouterr().out
    # No selector in common: no run is named.
    other = [{**row, "selector": "x"} for row in rows if row["selector"] == "inside"]
    assert cpp_probe(_identified(other, True), "--capture", "x", *EDIT) == 0
    assert "compare: no saved run is this probe without the edit" in capsys.readouterr().out


DIAGNOSTIC = (
    "error: program 0x00000100: DW_TAG_member '_M_local_buf' refers to type "
    "0x0000000000009a09 which extends beyond the bounds of 0x000000f7"
)


def _noisy(rows, build="b" * 64):
    from peeky.probe import debugger_diagnostics

    result = _identified(rows)
    source = {"diagnostic_count": 1, "debugger_diagnostics": [DIAGNOSTIC]}
    result["warnings"] = [debugger_diagnostics(result, source)]
    result.update(
        output_complete=False, program_ended=True, output_after_disconnect="free(): invalid\n"
    )
    if build is not None:
        result["build_id"] = build
    return result


SHORT = (
    "warning: `stderr` line of LLDB's about the build's debug information was moved out of the "
    "output again (debugger_diagnostics in --json); a program line just like one would be too"
)


SENT = "the debugger again sent more text as Peeky closed the session (output_after_disconnect"


def test_lldb_dap_18s_notes_are_whole_once_per_build(cpp_probe, capsys):
    # Every GCC probe with lldb-dap 18 repeated a 370-character warning about
    # `_M_local_buf` and the text it sends as it ends.
    rows = _bindings(False, [0, 1, 2, 3])
    assert cpp_probe(_noisy(rows), "--capture", "hour,cars,inside") == 0
    first = capsys.readouterr().out
    assert DIAGNOSTIC in first and "its own or the program's still on its way" in first, first
    assert cpp_probe(_noisy(rows), "--capture", "hour,cars,inside") == 0
    again = capsys.readouterr().out
    assert DIAGNOSTIC not in again and SHORT in again and SENT in again, again
    # The output's own caveat stays on every probe.
    assert "output so far (the program ended, but its output may still have been arriving)" in (
        again
    ), again
    # JSON keeps them whole.
    assert cpp_probe(_noisy(rows), "--capture", "hour,cars,inside", "--json") == 0
    shown = json.loads(capsys.readouterr().out)
    assert any(DIAGNOSTIC in warning for warning in shown["warnings"]), shown["warnings"]


@pytest.mark.parametrize(
    "why",
    [
        "the same target with its source changed",
        "a candidate edit",
        "another compiler",
    ],
)
def test_another_build_of_the_same_target_gets_them_whole(cpp_probe, capsys, why):
    # The key was the target's names and options alone, which a
    # baseline and its candidate, or two compilers, share. Each is its own build_id (the
    # digest of the build's recipe, staged sources and tools), with the same
    # target_identity, inputs and source digests here.
    rows = _bindings(False, [0, 1, 2, 3])
    assert cpp_probe(_noisy(rows), "--capture", "hour,cars,inside") == 0
    assert cpp_probe(_noisy(rows), "--capture", "hour,cars,inside") == 0
    assert SHORT in capsys.readouterr().out
    other = _noisy(rows, build=hashlib.sha256(why.encode()).hexdigest())
    assert cpp_probe(other, "--capture", "hour,cars,inside") == 0
    out = capsys.readouterr().out
    assert DIAGNOSTIC in out and SHORT not in out and SENT not in out, out
    assert cpp_probe(other, "--capture", "hour,cars,inside") == 0
    assert SHORT in capsys.readouterr().out


def test_without_a_build_id_they_are_whole_every_time(cpp_probe, capsys):
    # An older worker sends no build_id: nothing proves two probes ran one build.
    rows = _bindings(False, [0, 1, 2, 3])
    for _ in range(2):
        assert cpp_probe(_noisy(rows, build=None), "--capture", "hour,cars,inside") == 0
        out = capsys.readouterr().out
        assert DIAGNOSTIC in out and SHORT not in out and SENT not in out, out
