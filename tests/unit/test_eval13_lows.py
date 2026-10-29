"""The thirteenth agent evaluation's CLI Lows that need no debugger (#383, #384, #385 items
4 to 11): the intervention limit's error, `queries diff` of values one run has none of, a
probe table's repeated reasons and container previews, `compare:` after a candidate whose
captures differ, a lambda comparator's type, and lldb-dap 18's notes on every probe."""

import hashlib
import json

import pytest

from peeky import cli, evidence
from peeky.errors import classify
from peeky.native import NativeSession
from peeky.probe import MAX_SETS, Probe, _table

REASON = (
    "not shown: the binding refers to an object Peeky can't prove is still alive: `{}` binds an "
    "element of `by_hour` by reference, and Peeky proves only standard vectors, maps and sets of "
    "numbers, with the default allocator and order. Read it where the code names that object"
)


def test_the_intervention_limit_says_how_many_and_what_to_do():
    # nightlot: 12 `--set-at`s gave `At most 8 interventions (--set and --set-at
    # together)`, with the generic hint `Fix the request arguments; …`.
    changes = tuple((31, hit, "hh", str(hit)) for hit in range(1, 13))
    with pytest.raises(ValueError) as refused:
        Probe("nightlot.cpp", 40, ("hour",), set_at=changes)
    message = str(refused.value)
    assert f"At most {MAX_SETS} interventions" in message, message
    assert "this one has 12" in message, message
    error = classify(refused.value)
    assert error["code"] == "INVALID_REQUEST", error
    assert "several probes" in error["hint"] and "`--old`/`--new`" in error["hint"], error
    assert len(error["hint"]) <= 150, error["hint"]
    # A probe of 8 is still one probe.
    assert len(Probe("nightlot.cpp", 40, ("hour",), set_at=changes[:MAX_SETS]).set_at) == 8


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


@pytest.fixture
def store(tmp_path):
    return tmp_path / "evidence" / "evidence.db"


def test_a_value_one_run_has_none_of_is_no_difference(store, capsys):
    # nightlot: in a candidate whose structured bindings were `not shown`, 18 of 25 `diff`
    # rows said `differs 1` only because one side was withheld, and the note said `hour`
    # and `cars` "differ after the edit".
    evidence.record(store, _run(_bindings(False, [0, 3, 2, 0])), language="cpp")
    evidence.record(
        store, _run(_bindings(True, [0, 1, 2, 5]), candidate=True, output="BUSIEST:5\n")
    )
    rows = evidence.run_query(store, "diff", {"a": 1, "b": 2})["rows"]
    shape = [(row["selector"], row["hit"], row["differs"], row["note"]) for row in rows]
    real = [row for row in shape if row[0] == "inside"]
    assert real == [("inside", 2, 1, None), ("inside", 4, 1, None)], shape
    # After the real differences: one row per selector, its first hit's, saying how many.
    assert shape[-2:] == [
        (
            selector,
            1,
            None,
            "no value in run b at 4 hits (one row per selector: its first hit), so not a value "
            "difference",
        )
        for selector in ("hour", "cars")
    ], shape
    assert [row["selector"] for row in rows].index("hour") > max(
        index for index, row in enumerate(rows) if row["selector"] == "inside"
    )
    notes = evidence.run_query(store, "diff", {"a": 1, "b": 2})["notes"]
    assert (
        "not compared: run 2 has no value for `hour` and `cars` (8 values in all, withheld or "
        "unavailable), so they are no value differences; their rows come after the real "
        "differences, and `queries run --param run=2` says why"
    ) in notes, notes
    # Nothing calls them differences after the edit.
    assert not any("`hour`" in note and "differ after" in note for note in notes), notes
    # `compare` says the same per row, with differs unknown.
    compared = evidence.run_query(store, "compare", {"a": 1, "b": 2})["rows"]
    hour = [row for row in compared if row["selector"] == "hour"]
    assert {(row["differs"], row["note"]) for row in hour} == {
        (None, "no value in run b, so not a value difference")
    }, hour
    # The text table shows the reason's label alone; `--json` keeps it whole.
    _, text = evidence.canned(store, "diff", {"a": 1, "b": 2}, table=True)
    assert "<not shown: …>" in text and "can't prove is still alive" not in text, text
    assert rows[-1]["value_b"].startswith("<not shown: the binding refers"), rows[-1]


def test_both_runs_with_no_value_and_a_qualified_one(store):
    # Each run's reason differs: no value in either run, never a difference.
    first = _bindings(True, [0, 0, 0, 0])
    second = [
        {**row, "error": "no value yet: declared below the stop"} if "error" in row else row
        for row in first
    ]
    evidence.record(store, _run(first))
    evidence.record(store, _run(second))
    rows = evidence.run_query(store, "diff")["rows"]
    assert {row["note"] for row in rows} == {
        "no value in either run at 4 hits (one row per selector: its first hit), so not a value "
        "difference"
    }, rows
    assert {row["differs"] for row in rows} == {None}


def _typed(hit, value=None, kind="int", error=None):
    row = {"hit": hit, "phase": "observed", "line": 40, "selector": "n", "type": kind}
    return {**row, "unavailable": True, "error": error} if error else {**row, "value": value}


OPTIMIZED = "no value here: optimized out or not live (debugger: variable not available)"


@pytest.mark.parametrize("withheld", ["a", "b"])
@pytest.mark.parametrize("error", [REASON.format("n"), OPTIMIZED], ids=["withheld", "optimized"])
def test_a_type_change_stays_a_difference_where_a_run_has_no_value(store, withheld, error):
    # Review of PR #389: `type int -> long` with one side withheld read `no value in run b,
    # so not a value difference`, with differs NULL, which hid a real difference.
    one = [_typed(1, "3"), _typed(2, "4")]
    other = [_typed(1, kind="long", error=error), _typed(2, "4", kind="long")]
    first, second = (other, one) if withheld == "a" else (one, other)
    evidence.record(store, _run(first))
    evidence.record(store, _run(second))
    rows = evidence.run_query(store, "diff")["rows"]
    types = "long -> int" if withheld == "a" else "int -> long"
    assert [(row["hit"], row["differs"], row["note"]) for row in rows] == [
        (1, 1, f"type {types}; no value in run {withheld}, so the values were not compared"),
    ], rows
    notes = evidence.run_query(store, "diff").get("notes", [])
    assert not any(note.startswith("not compared:") for note in notes), notes
    compared = evidence.run_query(store, "compare")["rows"]
    assert (compared[0]["differs"], compared[0]["note"]) == (1, rows[0]["note"]), compared
    # The table shows the missing value's label alone, as for any value a run has none of.
    _, text = evidence.canned(store, "diff", table=True)
    assert error.split(":")[0] + ": …>" in text and error[20:60] not in text, text


def test_a_value_one_run_lacks_beside_a_type_change_elsewhere(store):
    # A type change at one hit leaves the selector's other no-value rows in their tier.
    one = [_typed(1, "3"), _typed(2, "4")]
    other = [_typed(1, kind="long", error=OPTIMIZED), _typed(2, kind="int", error=OPTIMIZED)]
    evidence.record(store, _run(one))
    evidence.record(store, _run(other))
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(row["hit"], row["differs"], row["note"]) for row in rows] == [
        (1, 1, "type int -> long; no value in run b, so the values were not compared"),
        (2, None, "no value in run b, so not a value difference"),
    ], rows


def test_a_type_peeky_cant_tell_changed_is_no_difference_where_a_run_has_no_value(store):
    # Review 2 of PR #389: a Rust run saved before Rust type spellings (no debugger_type)
    # against a newer one whose value is unavailable got differs 1 and ranked first, though
    # its note says Peeky can't tell whether the type changed.
    import sqlite3

    def rust(kind, **value):
        rows = [
            {"hit": 1, "phase": "observed", "line": 40, "selector": "n", "type": "u32", **n}
            for n in ({"value": "0"} if "value" in value else {"value": "1"},)
        ]
        rows.append({"hit": 1, "phase": "observed", "line": 40, "selector": "e", "type": kind})
        rows[-1].update(value)
        return {**_run(rows), "target_identity": {"language": "rust"}}

    evidence.record(store, rust("rota::Engineer *", value="0x00000001004151e0"), language="rust")
    connection = sqlite3.connect(store)
    connection.execute("UPDATE captures SET debugger_type = NULL WHERE run_id = 1")
    connection.commit()
    connection.close()
    evidence.record(
        store,
        rust(
            "&rota::Engineer",
            debugger_type="rota::Engineer *",
            unavailable=True,
            error=OPTIMIZED,
        ),
        language="rust",
    )
    untold = (
        "the older run predates Rust type spellings, so Peeky can't tell whether the type "
        "changed (`rota::Engineer *` then, `&rota::Engineer` now)"
    )
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(row["selector"], row["differs"], row["note"]) for row in rows] == [
        ("n", 1, None),
        ("e", None, f"no value in run b, so the values were not compared; {untold}"),
    ], rows
    compared = {row["selector"]: row for row in evidence.run_query(store, "compare")["rows"]}
    assert (compared["e"]["differs"], compared["e"]["note"]) == (None, rows[1]["note"])
    # A proven type change still is one.
    evidence.record(store, rust("&rota::Engineer", value="0x00000001004151e0"), language="rust")
    evidence.record(
        store, rust("*const rota::Engineer", unavailable=True, error=OPTIMIZED), language="rust"
    )
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(row["selector"], row["differs"], row["note"]) for row in rows] == [
        ("n", 1, None),
        (
            "e",
            1,
            "type &rota::Engineer -> *const rota::Engineer; no value in run b, so the values "
            "were not compared",
        ),
    ], rows


def test_a_note_on_left_out_selectors_says_their_rows_still_show():
    # bill: "`tariff` and `over_peak` differ after the edit and may follow from it, so this
    # check leaves them out" read as contradicting the rows it came under.
    note = evidence._after_change(
        ["wh"],
        ["hits pair by `wh`"],
        None,
        None,
        {1: [], 2: []},
        True,
        ["tariff", "over_peak"],
        None,
        "join",
    )
    assert "leaves" not in note, note
    assert (
        "- `tariff` and `over_peak` differ after the edit and may follow from it, so they say "
        "nothing of how the hits pair (their rows above still show where they differ)"
    ) in note, note


def test_the_output_row_does_not_widen_the_other_rows(store, capsys):
    # depot: every column was padded to the (output) row's 118 characters.
    long = "SHIPPED:CL-17,GS-5,BR-204,AX-1,AX-2,AX-3;REFUSED:HX-88,GS-5,QQ-1,QQ-2;AVERAGE:{}\n"
    evidence.record(store, _run(_bindings(False, [0, 3, 2, 0]), output=long.format("5.00")))
    evidence.record(store, _run(_bindings(False, [0, 1, 2, 5]), output=long.format("6.67")))
    _, text = evidence.canned(store, "diff", table=True)
    lines = text.splitlines()
    (output,) = [line for line in lines if "(output)" in line]
    assert "AVERAGE:5.00\\n  …" in output and output.endswith(
        "6.67\\n  1        output differs at line 1, column 79"
    ), text
    rows = [line for line in lines if "  inside  " in line]
    assert rows and all(len(line) < 100 for line in rows), text


def test_a_probe_table_gives_a_long_reason_once_per_selector():
    # nightlot: the same 300-character reason on every row, 18 times.
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
    # nightlot: `size=9 {…, 3: 1, …}` stopped at 8 entries, with no word on the ninth.
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
def probe(tmp_path, monkeypatch):
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


def test_compare_names_the_run_whose_captures_differ(probe, capsys):
    # depot: `compare: no saved run is this probe without the edit …` until the plain
    # probe was run again with the same captures, one selector apart.
    rows = _bindings(False, [0, 1, 2, 3])
    plain = [row for row in rows if row["selector"] in ("hour", "inside")]
    assert probe(_identified(plain), "--capture", "hour,inside") == 0  # run 1
    capsys.readouterr()
    assert probe(_identified(rows, True), "--capture", "hour,cars,inside", *EDIT) == 0
    text = capsys.readouterr().out
    assert (
        "compare: run 1 is this probe without the edit, but it didn't capture `cars`: `peeky "
        "queries diff --param a=1 --param b=2` compares the selectors both captured (`hour` and "
        "`inside`). To compare them all, run this probe without --old/--new, then `peeky "
        "queries diff`"
    ) in text, text
    assert probe(_identified(rows, True), "--capture", "hour,cars,inside", *EDIT, "--json") == 0
    compare = json.loads(capsys.readouterr().out)["evidence"]["compare"]
    assert compare["run"] is None and "didn't capture `cars`" in compare["hint"], compare
    # A run with the same captures is still the one named.
    assert probe(_identified(rows), "--capture", "hour,cars,inside") == 0  # run 4
    capsys.readouterr()
    assert probe(_identified(rows, True), "--capture", "hour,cars,inside", *EDIT) == 0
    assert "compare: peeky queries diff --param a=4 --param b=5" in capsys.readouterr().out
    # No selector in common: no run is named.
    other = [{**row, "selector": "x"} for row in rows if row["selector"] == "inside"]
    assert probe(_identified(other, True), "--capture", "x", *EDIT) == 0
    assert "compare: no saved run is this probe without the edit" in capsys.readouterr().out


def test_compare_finds_the_run_past_newer_runs_of_other_selectors(store):
    # Review of PR #389: only the newest 20 runs whose captures differed were looked at, so
    # 21 newer runs of other selectors hid the one run sharing a selector.
    def saved(capture, candidate=False):
        rows = [
            {"hit": 1, "phase": "observed", "line": 40, "selector": name, "value": "1"}
            for name in capture
        ]
        result = _identified(rows, candidate)
        if candidate:
            # As the store keeps a candidate: its edit, and the unedited source's digest.
            result["candidate"]["diff"] = "--- a.cpp\n+++ a.cpp\n"
        return evidence.record(store, result, language="cpp", target="/t")

    first = saved(["hour", "inside"])
    for n in range(21):
        saved([f"other{n}"])
    candidate = saved(["hour", "cars", "inside"], candidate=True)
    assert evidence.baseline(store, candidate, "0" * 64) is None
    assert evidence.near_baseline(store, candidate, "0" * 64) == (
        first,
        ["hour", "inside"],
        ["hour", "cars", "inside"],
    )
    # With none sharing a selector, none is named.
    lone = saved(["elsewhere"], candidate=True)
    assert evidence.near_baseline(store, lone, "0" * 64) is None


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


def test_lldb_dap_18s_notes_are_whole_once_per_build(probe, capsys):
    # depot, bill: every GCC probe with lldb-dap 18 repeated a 370-character warning about
    # `_M_local_buf` and the text it sends as it ends.
    rows = _bindings(False, [0, 1, 2, 3])
    assert probe(_noisy(rows), "--capture", "hour,cars,inside") == 0
    first = capsys.readouterr().out
    assert DIAGNOSTIC in first and "its own or the program's still on its way" in first, first
    assert probe(_noisy(rows), "--capture", "hour,cars,inside") == 0
    again = capsys.readouterr().out
    assert DIAGNOSTIC not in again and SHORT in again and SENT in again, again
    # The output's own caveat stays on every probe.
    assert "output so far (the program ended, but its output may still have been arriving)" in (
        again
    ), again
    # JSON keeps them whole.
    assert probe(_noisy(rows), "--capture", "hour,cars,inside", "--json") == 0
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
def test_another_build_of_the_same_target_gets_them_whole(probe, capsys, why):
    # Review of PR #389: the key was the target's names and options alone, which a
    # baseline and its candidate, or two compilers, share. Each is its own build_id (the
    # digest of the build's recipe, staged sources and tools), with the same
    # target_identity, inputs and source digests here.
    rows = _bindings(False, [0, 1, 2, 3])
    assert probe(_noisy(rows), "--capture", "hour,cars,inside") == 0
    assert probe(_noisy(rows), "--capture", "hour,cars,inside") == 0
    assert SHORT in capsys.readouterr().out
    other = _noisy(rows, build=hashlib.sha256(why.encode()).hexdigest())
    assert probe(other, "--capture", "hour,cars,inside") == 0
    out = capsys.readouterr().out
    assert DIAGNOSTIC in out and SHORT not in out and SENT not in out, out
    assert probe(other, "--capture", "hour,cars,inside") == 0
    assert SHORT in capsys.readouterr().out


def test_without_a_build_id_they_are_whole_every_time(probe, capsys):
    # An older worker sends no build_id: nothing proves two probes ran one build.
    rows = _bindings(False, [0, 1, 2, 3])
    for _ in range(2):
        assert probe(_noisy(rows, build=None), "--capture", "hour,cars,inside") == 0
        out = capsys.readouterr().out
        assert DIAGNOSTIC in out and SHORT not in out and SENT not in out, out


def _manifest(**changed):
    from peeky.build import BuildManifest, FileIdentity, ProbeResult, ToolIdentity

    parts = {
        "schema_version": 1,
        "recipe_sha256": "r" * 64,
        "original_inputs": (FileIdentity("a.cpp", "1" * 64, 10),),
        "inputs": (FileIdentity("a.cpp", "1" * 64, 10),),
        "artifacts": (FileIdentity("a.out", "2" * 64, 900),),
        "tools": (ToolIdentity("/usr/bin/g++-13", "3" * 64),),
        "probes": (ProbeResult(("g++", "--version"), "g++ 13.3", ""),),
        "effective_environment": (("PATH", "/usr/bin"),),
    }
    return BuildManifest(**{**parts, **changed})


def test_a_build_id_is_its_sources_and_tools_not_its_artifacts():
    from peeky.build import FileIdentity, ProbeResult, ToolIdentity

    base = _manifest()
    # Every build's binary differs (its debug information names the fresh workspace).
    rebuilt = _manifest(artifacts=(FileIdentity("a.out", "4" * 64, 901),))
    assert rebuilt.build_id == base.build_id and rebuilt.manifest_id != base.manifest_id
    edited = (FileIdentity("a.cpp", "5" * 64, 11),)
    for other in (
        _manifest(inputs=edited),
        _manifest(original_inputs=edited, inputs=edited),
        _manifest(tools=(ToolIdentity("/usr/bin/clang++-18", "6" * 64),)),
        _manifest(probes=(ProbeResult(("g++", "--version"), "g++ 14.1", ""),)),
        _manifest(recipe_sha256="7" * 64),
    ):
        assert other.build_id != base.build_id, other


def test_template_arguments_split_at_their_top_level():
    from peeky.native import _template_split

    assert _template_split("std::map<int, long, type>") == ("std::map", ["int", "long", "type"], "")
    assert _template_split(
        "map<int, long int, main()::<lambda(int, int)>, std::allocator<std::pair<const int, long "
        "int> > >"
    ) == (
        "map",
        [
            "int",
            "long int",
            "main()::<lambda(int, int)>",
            "std::allocator<std::pair<const int, long int> >",
        ],
        "",
    )
    assert _template_split("const std::set<int, type> &") == (
        "const std::set",
        ["int", "type"],
        " &",
    )
    assert _template_split("int") is None and _template_split("a<b") is None


class _Session(NativeSession):
    """A NativeSession with only the build's type graph: ``_root_type`` gives the local's
    type offset in it."""

    def __init__(self, graph, root):
        self.graph, self.root = graph, root

    def _root_type(self, frame, root):
        if root.get("name") != "by_hour":
            raise ValueError("no such local")
        return self.root, True, self.graph


def _record(name):
    return ("record", name, "class", 48, 8, (), (), False, False, True, (), name, (), ())


@pytest.mark.parametrize(
    "shown,declared,named",
    [
        # Apple clang 21, libc++: LLDB names the lambda by `__type_identity<…>::type`.
        (
            "std::map<int, long, type>",
            "map<int, long, (lambda at nightlot.cpp:23:24), std::__1::allocator<std::__1::pair"
            "<const int, long> > >",
            "std::map<int, long, (lambda at nightlot.cpp:23:24)>",
        ),
        # GCC 13, libstdc++: by `key_compare`.
        (
            "std::map<int, long, key_compare>",
            "map<int, long int, main()::<lambda(int, int)>, std::allocator<std::pair<const int, "
            "long int> > >",
            "std::map<int, long, main()::<lambda(int, int)>>",
        ),
        # An unnamed struct's typedef name.
        (
            "std::set<int, Order>",
            "set<int, (unnamed struct at a.cpp:3:9), std::__1::allocator<int> >",
            "std::set<int, (unnamed struct at a.cpp:3:9)>",
        ),
    ],
)
def test_a_lambda_comparator_is_named_as_the_compiler_names_it(shown, declared, named):
    # nightlot: a map ordered by a lambda showed `std::map<int, long, type>`.
    session = _Session({7: ("typedef", "Hours", 8), 8: _record(declared)}, 7)
    variable = {"name": "by_hour", "type": shown}
    assert session.display_type(variable, {}, top=True) == named


def test_a_named_comparator_or_another_local_keeps_lldbs_type():
    session = _Session({8: _record("map<int, long, ByAbs, std::allocator<int> >")}, 8)
    assert (
        session.display_type({"name": "by_hour", "type": "std::map<int, long, ByAbs>"}, {}) is None
    )
    assert session.display_type({"name": "other", "type": "std::map<int, long, type>"}, {}) is None
    # Another class's record: nothing is taken from it.
    lambdas = _Session({8: _record("set<int, (lambda at a.cpp:3:9)>")}, 8)
    assert lambdas.display_type({"name": "by_hour", "type": "std::map<int, type>"}, {}) is None
    # Fields keep LLDB's type.
    assert session.display_type({"name": "m", "type": "std::map<int, type>"}, {}, top=False) is None
