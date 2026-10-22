"""The seventh agent evaluation's findings that need no debugger: CLI text, exit codes,
verify's reason and the evidence store's comparisons."""

import json

import pytest

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
def cli(tmp_path, monkeypatch):
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


def test_the_short_name_note_is_said_once_per_state_directory(cli, capsys):
    # Finding 8: every probe repeated `note: function shows short frame names; ...`,
    # even with --quiet.
    note = "note: function shows short frame names"
    assert cli(probe_result(), "--quiet") == 0
    assert note not in capsys.readouterr().out
    assert cli(probe_result()) == 0
    first = capsys.readouterr().out
    assert note in first and "short_function(function) = 'trim_left'" in first, first
    assert cli(probe_result()) == 0
    again = capsys.readouterr().out
    assert note not in again and "trim_left " in again, again
    # The SQL text says it where SQL is used: `peeky sql --help` and query tables.
    from peeky.cli import main

    with pytest.raises(SystemExit):
        main(["sql", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "short_function(function) = 'trim_left'" in help_text, help_text
    assert main(["queries", "run"]) == 0
    assert note in capsys.readouterr().out


def test_the_instance_note_follows_the_same_rule(cli, capsys):
    # Finding 8, after #151: a probe table's instance labels (`instance = '...'`) are part
    # of the same note, said once per state directory and never with --quiet.
    closure = probe_result(function="cz::main::{{closure}}::h0123456789abcdef")
    closure["rows"][0]["instance"] = "cz::main::{closure#0}"
    assert cli(closure, "--quiet") == 0
    assert "note:" not in capsys.readouterr().out
    assert cli(closure) == 0
    first = capsys.readouterr().out
    assert "instance = 'cz::main::{closure#0}'" in first and " cz::main::{closure#0} " in first
    assert cli(closure) == 0
    again = capsys.readouterr().out
    assert "note:" not in again and " cz::main::{closure#0} " in again, again
    # `peeky sql --help` keeps it.
    from peeky.cli import main

    with pytest.raises(SystemExit):
        main(["sql", "--help"])
    assert "instance = 'cz::main::{closure#0}'" in " ".join(capsys.readouterr().out.split())


def test_the_probe_exit_codes():
    # Finding 14 with #151: 2 for a probe refused before it ran, 4 for a completed probe
    # with a change not made, 1 for any other probe that did not complete.
    from peeky.probe import probe_exit_code

    refused = {"status": "failed", "refused_before_launch": True}
    assert probe_exit_code(refused) == 2
    assert probe_exit_code({"status": "completed", "unapplied": [{"name": "x"}]}) == 4
    for status in ("failed", "no-stop", "timeout"):
        assert probe_exit_code({"status": status}) == 1
    assert probe_exit_code({"status": "completed"}) == 0


def test_a_probe_refused_before_launch_shows_no_table_and_exits_2(monkeypatch, capsys):
    # Finding 14: the console script too printed an empty table header and exited 1.
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


CHECKER = """import json, subprocess, sys
print("checking 3 cases")
print("FAIL ['+1', '-']: got 'POPPED:' want 'POPPED:1'")
sys.exit({status})
"""


@pytest.mark.parametrize("status", [1, 3])
def test_a_failed_checks_reason_is_its_last_output_line(tmp_path, monkeypatch, capsys, status):
    # Finding 11: `verify --baseline` gave `reason: SystemExit: 1`; the useful line, the
    # checker's `FAIL ...`, was only in `output`.
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "app.py").write_text("print('POPPED:')\n")
    (tmp_path / "check.py").write_text(CHECKER.format(status=status))
    command = ["verify", str(tmp_path / "app.py"), "--check", str(tmp_path / "check.py")]
    assert main([*command, "--baseline", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    fail = "FAIL ['+1', '-']: got 'POPPED:' want 'POPPED:1'"
    # The status stays where it was: the checker's error and its exit code.
    assert result["checker"]["error"] == f"SystemExit: {status}" and result["exit_code"] == 1
    assert result["reason"] == (fail if status == 1 else f"{fail} (SystemExit: {status})")
    assert main([*command, "--baseline"]) == 1
    text = capsys.readouterr().out
    assert f"reason: {result['reason']}\nchecker exit: 1\n" in text, text
    assert "the checker rejects it, as it should reject the bug" in text, text


def test_a_reason_the_output_cannot_give_stays_the_checkers():
    from peeky.cli import _verify_reason

    def failed(error, **extra):
        return {"passed": False, "stage": "verification", "exit_code": 1,
                "checker": {"error": error}, **extra}  # fmt: skip

    # No output, an assertion's own message, or an exit with no status to explain.
    assert _verify_reason(failed("SystemExit: 1")) == "SystemExit: 1"
    assert (
        _verify_reason(failed("AssertionError: 3 != 4", stdout="x\n")) == "AssertionError: 3 != 4"
    )
    assert _verify_reason(failed("SystemExit: 0", stdout="ok\n")) == "SystemExit: 0"
    # Output cut short may end part-way through a line.
    long = "y" * 16000 + "\nFAIL tail"
    assert _verify_reason(failed("SystemExit: 1", stdout=long)) == "SystemExit: 1"
    # stdout first: its last line, though stderr says FAIL (round 2 of PR #165).
    both = failed("SystemExit: 1", stdout="case 1 ok\n", stderr="FAIL case 2\n\n")
    assert _verify_reason(both) == "case 1 ok"
    assert _verify_reason(failed("SystemExit: 1", stderr="FAIL case 2\n")) == "FAIL case 2"
    assert _verify_reason(failed("SystemExit: 1", timed_out=True)) == "checker timed out"


def test_a_reason_is_the_line_that_says_what_failed():
    # Review of PR #165, Low 5: `FAIL ...` on stdout and `note: cleanup done` on stderr
    # gave the stderr note.
    from peeky.cli import _verify_reason

    def reason(stdout="", stderr=""):
        result = {"passed": False, "stage": "verification", "exit_code": 1}
        return _verify_reason(
            {**result, "checker": {"error": "SystemExit: 1"}, "stdout": stdout, "stderr": stderr}
        )

    noisy = "case 1: ok\nFAIL ring: got '7 2' want '7 3'\n"
    assert reason(noisy, "note: cleanup done\n") == "FAIL ring: got '7 2' want '7 3'"
    # A failure over several lines: its indented lines, two at most.
    spread = "FAIL first case\n  expected 3\n  got 2\n  (seed 9)\nsummary\n"
    assert reason(spread) == "FAIL first case; expected 3; got 2"
    # A traceback gives its exception, where stdout is empty; stdout comes first.
    trace = 'Traceback (most recent call last):\n  File "c.py", line 3\nValueError: bad\n'
    assert reason("", trace) == "ValueError: bad"
    assert reason("assert got == want\n", trace) == "assert got == want"
    # None says what failed: stdout's last line, then stderr's.
    assert reason("case 1 ok\n", "cleanup\n") == "case 1 ok"
    assert reason("", "cleanup\n") == "cleanup"


@pytest.mark.parametrize(
    "stdout, stderr, said",
    [
        # Round 2 of PR #165, Low 3: unanchored words in passing lines were picked.
        ("ok: empty input raises ValueError\nmismatch: wrap got 6 want 7\n", "",
         "mismatch: wrap got 6 want 7"),
        ("FAIL case 3: got 2 want 3\ncase 4 ok: raises KeyError as expected\n", "",
         "FAIL case 3: got 2 want 3"),
        ("case 1 ok: handles ValueError input\nFAIL case 3: got 2 want 3\nsummary: 1 failed\n",
         "", "FAIL case 3: got 2 want 3"),
        ("FAIL case 3: got 2 want 3\ndebug: retrying with ErrorHandler disabled\n", "",
         "FAIL case 3: got 2 want 3"),
        ("mismatch: got 2 want 3\nasserting cleanup finished\n", "",
         "asserting cleanup finished"),
        ("no Traceback (most recent call last) found: ok\nmismatch: got 2 want 3\n", "",
         "mismatch: got 2 want 3"),
        ("case 1 ok\nmismatch in case 2\nFAILOVER test skipped\n", "", "FAILOVER test skipped"),
        # A handled exception logged on stderr is not the failure stdout reports.
        ("recovered\nmismatch: got 2 want 3\n",
         "Traceback (most recent call last):\n  File \"c.py\", line 4\nKeyError: 'k'\n",
         "mismatch: got 2 want 3"),
        # pytest: its `E` line, before the FAILED summary.
        ("test_x.py ...F [100%]\n>       assert run() == '7 3'\n"
         "E       AssertionError: assert '6 3' == '7 3'\n\ntest_x.py:12: AssertionError\n"
         "FAILED test_x.py::test_wrap - AssertionError: assert '6 3' == '7 3'\n", "",
         "AssertionError: assert '6 3' == '7 3'"),
        # unittest (on stderr): the test and its assertion, not `FAILED (failures=1)`.
        ("", ".F\n======\nFAIL: test_wrap (__main__.T.test_wrap)\n------\n"
         "Traceback (most recent call last):\n  File \"x.py\", line 9, in test_wrap\n"
         "AssertionError: '6 3' != '7 3'\n------\nRan 2 tests in 0.001s\n\n"
         "FAILED (failures=1)\n",
         "FAIL: test_wrap (__main__.T.test_wrap): AssertionError: '6 3' != '7 3'"),
    ],
)  # fmt: skip
def test_the_reason_is_the_first_line_anchored_as_a_failure(stdout, stderr, said):
    from peeky.cli import _verify_reason

    error = "SystemExit: True" if "unittest" in said or "__main__" in said else "SystemExit: 1"
    result = {"passed": False, "stage": "verification", "exit_code": 1}
    reason = _verify_reason(
        {**result, "checker": {"error": error}, "stdout": stdout, "stderr": stderr}
    )
    assert reason == said


@pytest.fixture
def store(tmp_path):
    return tmp_path / "evidence" / "evidence.db"


def saved(store, rows, hits=None, requested="all", warnings=(), **probe):
    """Record a run of (hit, phase, selector, value[, stop]) rows that stopped ``hits``
    times (default: its last hit) of the ``requested``; the probe asked for exactly the
    selectors it captured. A row's stop is which of its line's 2 breakpoint locations
    it was at (#157)."""
    from peeky import evidence

    recorded = probe_result()
    recorded["probe"]["hits"] = requested
    recorded["rows"] = [
        dict(hit=hit, phase=phase, line=36, selector=selector, value=value)
        | ({"stop": stop[0], "stops": 2} if stop else {})
        for hit, phase, selector, value, *stop in rows
    ]
    recorded["probe"].update(capture=list(dict.fromkeys(row[2] for row in rows)), **probe)
    recorded["hits"] = hits if hits is not None else max([row[0] for row in rows], default=0)
    recorded["warnings"] = list(warnings)
    return evidence.record(store, recorded, language="c")


def test_diff_leaves_out_after_set_values_the_intervention_left(store):
    # Finding 10: `r.buf`, which `--set r.tail=3` never touched, was listed as a
    # difference (`intervention only in run b`) because only run b has after-set rows.
    from peeky import evidence

    saved(store, [(4, "observed", "r.tail", "0"), (4, "observed", "r.buf", "[1, 2, 3, 0]")])
    saved(
        store,
        [
            (4, "observed", "r.tail", "0"),
            (4, "observed", "r.buf", "[1, 2, 3, 0]"),
            (4, "after-set", "r.tail", "3"),
            (4, "after-set", "r.buf", "[1, 2, 3, 0]"),
        ],
        set=[["r.tail", "3"]],
    )
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["phase"], r["selector"], r["note"]) for r in rows] == [
        (
            None,
            "(sets)",
            "the runs requested different interventions (--set, --set-at), or made different ones",
        ),
        ("after-set", "r.tail", "intervention only in run b"),
    ]
    # A `--set-at` at the captured line's 4th stop (#137) gets the same after-set rows.
    at_rows = [
        (4, "observed", "r.tail", "0"),
        (4, "observed", "r.buf", "[1, 2, 3, 0]"),
        (4, "after-set", "r.tail", "3"),
        (4, "after-set", "r.buf", "[1, 2, 3, 0]"),
    ]
    saved(store, at_rows[:2])
    saved(store, at_rows, set_at=[[36, 4, "r.tail", "3"]])
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["phase"], r["selector"], r["note"]) for r in rows] == [
        (
            None,
            "(sets)",
            "the runs requested different interventions (--set, --set-at), or made different ones",
        ),
        ("after-set", "r.tail", "intervention only in run b"),
    ]
    # Both runs set something: an after-set value equal to its run's observed value is
    # no difference of its own; the observed row has any.
    saved(
        store,
        [
            (4, "observed", "r.tail", "0"),
            (4, "observed", "r.buf", "[1, 2, 3, 0]"),
            (4, "after-set", "r.tail", "3"),
            (4, "after-set", "r.buf", "[1, 2, 3, 0]"),
        ],
        set=[["r.tail", "3"]],
    )
    saved(
        store,
        [
            (4, "observed", "r.tail", "1"),
            (4, "observed", "r.buf", "[1, 2, 3, 4]"),
            (4, "after-set", "r.tail", "3"),
            (4, "after-set", "r.buf", "[1, 2, 3, 4]"),
        ],
        set=[["r.tail", "3"]],
    )
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["phase"], r["selector"], r["note"]) for r in rows] == [
        ("observed", "r.tail", None),
        ("observed", "r.buf", None),
    ]
    # `compare` still shows every value.
    compared = evidence.run_query(store, "compare")["rows"]
    assert ("after-set", "r.buf") in [(r["phase"], r["selector"]) for r in compared]


def test_diff_gives_one_address_only_row_per_selector(store):
    # Finding 9: `queries diff` repeated an address-only row for the same selector once
    # per hit.
    from peeky import evidence

    def vec(address):
        return f"alloc::vec::Vec<long, alloc::alloc::Global> @ 0x{address:012x}"

    for base in (0x16B43EDB0, 0x16D17EDB0):
        rows = [(hit, "observed", "self.values", vec(base)) for hit in (1, 2, 3, 4)]
        saved(store, rows + [(hit, "observed", "v", "3") for hit in (1, 2, 3, 4)])
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["hit"], r["selector"], r["note"]) for r in rows] == [
        (1, "self.values", "addresses only, at 4 hits (one row per selector: its first hit)")
    ]
    assert rows[0]["value_a"] == vec(0x16B43EDB0) and rows[0]["value_b"] == vec(0x16D17EDB0)
    # A single hit keeps the plain note.
    saved(store, [(1, "observed", "p", vec(0x16B43EDB0))])
    saved(store, [(1, "observed", "p", vec(0x16D17EDB0))])
    (row,) = evidence.run_query(store, "diff")["rows"]
    assert row["note"] == "addresses only"


def test_diff_says_when_the_runs_hits_do_not_line_up(store, capsys):
    # Finding 20: diff pairs rows by hit number, so for a sort comparator every hit
    # after the first different answer showed as a difference.
    from peeky import evidence
    from peeky.cli import main

    pairs_a = [("test", "build"), ("deploy", "test"), ("lint", "deploy"), ("lint", "test")]
    pairs_b = [("test", "build"), ("deploy", "test"), ("deploy", "build"), ("lint", "deploy")]
    for pairs in (pairs_a, pairs_b + [("lint", "test")]):
        saved(
            store,
            [
                row
                for hit, (a, b) in enumerate(pairs, 1)
                for row in ((hit, "observed", "a.name", a), (hit, "observed", "b.name", b))
            ],
        )
    result = evidence.run_query(store, "diff")
    note, after = result["notes"]
    assert note.startswith("run 1 stopped 4 times at its line and run 2 5,"), note
    assert "peeky queries join --param a=1 --param b=2 --param key=<selector>" in note
    # #201: how many rows come after the first hit that differs (every row is kept).
    assert after.startswith(
        "the first difference at a hit is at hit 3; 3 of the 5 rows at a hit come after it"
    ), after
    assert len(result["rows"]) == 5
    assert main(["queries", "diff", "--store", str(store)]) == 0
    assert "note: run 1 stopped 4 times" in capsys.readouterr().out
    # The same number of stops: no note.
    saved(store, [(1, "observed", "x", "1")])
    saved(store, [(1, "observed", "x", "2")])
    assert "notes" not in evidence.run_query(store, "diff")
    # Nor where the runs asked for different numbers of hits.
    saved(store, [(1, "observed", "x", "1")])
    saved(store, [(1, "observed", "x", "1"), (2, "observed", "x", "2")], hits=2)
    assert "notes" in evidence.run_query(store, "diff")
    saved(store, [(1, "observed", "x", "1")], requested=1)
    assert "notes" not in evidence.run_query(store, "diff")


def test_diff_pairs_hits_at_the_same_stop_where_both_runs_label_them(store):
    # Merge of #160 (#157): a line with several breakpoint locations stops at each, and
    # a hit at another location than the other run's is another row, not the same
    # moment with another value.
    from peeky import evidence

    saved(store, [(1, "observed", "x", "1", 1), (2, "observed", "x", "2", 2)])
    saved(store, [(1, "observed", "x", "1", 1), (2, "observed", "x", "5", 1)])
    result = evidence.run_query(store, "diff")
    assert [(r["hit"], r["value_a"], r["value_b"], r["note"]) for r in result["rows"]] == [
        (2, "2", None, "missing in run b (run a: stop 2)"),
        (2, None, "5", "missing in run a (run b: stop 1)"),
    ]
    (note,) = result["notes"]
    assert note.startswith("at hit 2 run 1 was at stop 2 of its line and run 2 at stop 1"), note
    assert "peeky queries join --param a=1 --param b=2 --param key=<selector>" in note
    # The same stops pair as before; a run with no labels pairs by hit and says so.
    saved(store, [(1, "observed", "x", "1", 1), (2, "observed", "x", "7", 2)])
    result = evidence.run_query(store, "diff", {"a": 1, "b": 3})
    assert [(r["hit"], r["note"]) for r in result["rows"]] == [(2, None)], result
    assert "notes" not in result, result
    saved(store, [(1, "observed", "x", "1"), (2, "observed", "x", "7")])
    result = evidence.run_query(store, "diff")
    assert result["rows"] == [] and "run 4 at an unlabelled stop" in result["notes"][0]


def test_a_return_pairs_by_its_hits_stop(store):
    # Merge of #162 (#155): a `(return)` row has no stop of its own, so with stop-aware
    # pairing it paired by hit number alone, comparing the returns of calls that began
    # at different stops. It now takes its hit's stop from the hit's observed rows.
    from peeky import evidence

    def calls(second_stop, second_return):
        return [
            (1, "observed", "x", "1", 1),
            (1, "returned", "(return)", "5"),
            (2, "observed", "x", "2", second_stop),
            (2, "returned", "(return)", second_return),
        ]

    saved(store, calls(2, "6"))
    saved(store, calls(1, "9"))
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["hit"], r["selector"], r["note"]) for r in rows] == [
        (2, "x", "missing in run b (run a: stop 2)"),
        (2, "(return)", "missing in run b (run a: stop 2)"),
        (2, "x", "missing in run a (run b: stop 1)"),
        (2, "(return)", "missing in run a (run b: stop 1)"),
    ]
    # At the same stops, the returns pair and differ as values.
    saved(store, calls(2, "7"))
    rows = evidence.run_query(store, "diff", {"a": 1, "b": 3})["rows"]
    assert [(r["hit"], r["selector"], r["value_a"], r["value_b"]) for r in rows] == [
        (2, "(return)", "6", "7")
    ]


def test_returns_alone_on_a_line_that_stops_several_times_are_flagged(store):
    # With `--capture return` alone no observed row says at which stop a call began:
    # where the line stops several times per pass, diff's note says pairing by hit
    # cannot be checked.
    from peeky import evidence

    warning = "Line 36 stops up to 2 times per pass in f (2 breakpoint locations): ..."
    returns = [(1, "returned", "(return)", "5"), (2, "returned", "(return)", "6")]
    saved(store, returns, warnings=[warning])
    saved(store, returns[:1] + [(2, "returned", "(return)", "8")], warnings=[warning])
    (note,) = evidence.run_query(store, "diff")["notes"]
    assert note.startswith("run 1's line stops several times per pass, and its `(return)`")
    assert "peeky queries join --param a=1 --param b=2 --param key=<selector>" in note
    # A line with one location: no note.
    saved(store, returns)
    saved(store, returns)
    assert "notes" not in evidence.run_query(store, "diff")


def test_returns_alone_pair_by_their_own_stop(store):
    # #174: a `(return)` row now has its hit's stop, so returns alone on a line that
    # stops several times per pass pair by stop, and nothing is left to flag.
    from peeky import evidence

    warning = "Line 36 stops up to 2 times per pass in f (2 breakpoint locations): ..."
    saved(
        store,
        [(1, "returned", "(return)", "5", 1), (2, "returned", "(return)", "6", 2)],
        warnings=[warning],
    )
    saved(
        store,
        [(1, "returned", "(return)", "5", 1), (2, "returned", "(return)", "8", 1)],
        warnings=[warning],
    )
    result = evidence.run_query(store, "diff")
    assert [(r["hit"], r["note"]) for r in result["rows"]] == [
        (2, "missing in run b (run a: stop 2)"),
        (2, "missing in run a (run b: stop 1)"),
    ]
    assert not any("rows do not say at which stop" in note for note in result.get("notes", ()))


def test_a_return_whose_hits_rows_disagree_is_not_paired_by_a_guessed_stop(store):
    # Review of PR #198: a `(return)` row saved without its stop took MIN(stop) of its
    # hit's rows. Run 1's hit was observed at stop 1 and set at stop 2, so its return
    # was paired as stop 1 with run 2's and compared (5 against 6), with no note. Rows
    # that disagree give no stop: nothing is paired by a guess, and the note says so.
    from peeky import evidence

    warning = "Line 36 stops up to 2 times per pass in f (2 breakpoint locations): ..."
    saved(
        store,
        [
            (1, "observed", "x", "1", 1),
            (1, "after-set", "x", "1", 2),
            (1, "returned", "(return)", "5"),
        ],
        warnings=[warning],
    )
    saved(
        store,
        [
            (1, "observed", "x", "1", 1),
            (1, "after-set", "x", "1", 1),
            (1, "returned", "(return)", "6"),
        ],
        warnings=[warning],
    )
    result = evidence.run_query(store, "diff")
    returns = [
        (r["value_a"], r["value_b"], r["note"])
        for r in result["rows"]
        if r["selector"] == "(return)"
    ]
    assert returns == [
        ("5", None, "missing in run b"),
        (None, "6", "missing in run a (run b: stop 1)"),
    ]
    assert any(
        note.startswith("run 1's line stops several times per pass, and its `(return)` rows")
        for note in result["notes"]
    ), result["notes"]


def test_diff_collapses_address_only_rows_per_stop(store):
    # Finding 9 with #160's stops: one address-only row per selector and stop.
    from peeky import evidence

    for base in (0x16B43EDB0, 0x16D17EDB0):
        address = f"Vec<long> @ 0x{base:012x}"
        saved(store, [(hit, "observed", "p", address, 2 - hit % 2) for hit in (1, 2, 3, 4)])
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["hit"], r["note"]) for r in rows] == [
        (1, "addresses only, at 2 hits at stop 1 (one row per selector and stop: its first hit)"),
        (2, "addresses only, at 2 hits at stop 2 (one row per selector and stop: its first hit)"),
    ]


def test_compare_names_no_run_whose_hits_are_at_other_stops(cli, capsys):
    # Merge of #160 (#157): an edit that changes how often the `--at` line stops per
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
        return cli(result, "--capture", "i", *edit, "--json")

    assert run() == 0  # run 1
    capsys.readouterr()
    assert run(candidate=True) == 0  # run 2: the same stops
    assert json.loads(capsys.readouterr().out)["evidence"]["compare"]["run"] == 1
    assert run(candidate=True, stop=2) == 0  # run 3: hit 1 at another stop
    compare = json.loads(capsys.readouterr().out)["evidence"]["compare"]
    assert compare["run"] is None, compare
    assert compare["hint"] == (
        "run 1 is this probe without the edit, but at hit 1 it was at stop 1 of the line "
        "and this run at stop 2, so hits do not pair by number: pair them by a value, "
        "`peeky queries join --param a=1 --param b=3 --param key=<selector>`"
    )


def test_the_per_pass_warning_is_said_every_time_the_short_name_note_once(cli, capsys):
    # Finding 8 with #160: the warning that a line stops several times per pass changes
    # what hits count, so it is never once-only or quiet; the note on names is.
    warning = "Line 3 stops up to 2 times per pass in trim_left (2 breakpoint locations)"
    result = probe_result(warnings=[warning])
    result["rows"][0].update(stop=1, stops=2)
    note = "note: function shows short frame names"
    for flags, noted in ((["--quiet"], False), ([], True), ([], False)):
        assert cli(dict(result), *flags) == 0
        out = capsys.readouterr().out
        assert f"warning: {warning}" in out and (note in out) == noted, out
        assert out.startswith("hit  stop  phase") and "\n1    1/2   observed" in out, out


def test_a_candidate_probe_names_the_run_to_compare_it_with(cli, tmp_path, capsys):
    # Finding 12: after a candidate probe, a plain `queries diff` compares it with the
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
        return cli(result, *extra, *flags, *(["--", *args] if args else []))

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
    # A run with a `--set-at` (#137) changed a value too: it is no baseline.
    changed = probe_result()
    changed["probe"].update(program="a.py", line=1, capture=["i"], set_at=[[1, 2, "i", "3"]])
    assert cli(changed, "--capture", "i", "--set-at", "1@2", "i=3") == 0  # run 9
    assert run(("i",), (), (), True) == 0  # run 10
    assert "compare: peeky queries diff --param a=8 --param b=10" in capsys.readouterr().out


def test_the_guide_lists_session_wait_and_program_arguments_up_front():
    # Findings 21 and 22: the command table did not list `session wait`, and passing
    # program arguments (`--`, `--arg`) was explained only for Python projects.
    from pathlib import Path

    docs = Path(__file__).resolve().parents[2] / "docs"
    guide = (docs / "AGENT_GUIDE.md").read_text()
    table = guide.split("## Commands", 1)[1].split("###", 1)[0]
    sessions = [row for row in table.splitlines() if row.startswith("| `peeky session")]
    assert any(
        "/ `wait`" in row and "`wait` waits for a running one to stop" in row for row in sessions
    ), sessions
    probe = next(row for row in table.splitlines() if row.startswith("| `peeky probe <target>"))
    assert "[-- ARGS]" in probe, probe
    # The arguments' syntax is in the commands section, for every language.
    assert "**Program arguments:** after `--` or repeated `--arg`" in table, table
    # Review of PR #165: exit 3 is not only a background job's.
    flat = " ".join(guide.split())
    assert (
        "3: not there yet (a background job still running; a session's `--to`, `--count` or "
        "`--hit` stopped first)."
    ) in flat
    assert "4: a probe change never made" in flat
    # Finding 19: a step can land back on an `if`'s line.
    cookbook = " ".join((docs / "COOKBOOK.md").read_text().split())
    assert "A step can also land back on an `if`'s line after the last line of its block" in (
        cookbook
    )


def test_the_compared_run_has_the_candidates_build_and_unedited_source(cli, capsys):
    # Review of PR #165, Low 4: `compare:` named an -O1 run for an -O0 candidate, and a
    # run of the source as it was before the file changed on disk.
    import hashlib

    def run(candidate=False, optimization="O0", source="0" * 64):
        result = probe_result(source_sha256=source)
        result["target_identity"] = {"language": "c", "optimization": optimization}
        result["probe"].update(program="a.py", line=1, capture=["i"])
        if candidate:
            result["candidate"] = {"sha256": hashlib.sha256(b"x = 2\n").hexdigest()}
        edit = ["--old", "x = 1", "--new", "x = 2"] if candidate else []
        return cli(result, "--capture", "i", *edit)

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


def test_the_compared_run_read_the_same_sources(cli, capsys):
    # Round 2 of PR #165, Low 2: after another source file changed on disk, `compare:`
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
        return cli(result, "--capture", "i", *edit)

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
    from peeky.cli import main

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
