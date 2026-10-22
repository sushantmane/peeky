"""The tenth agent evaluation's Lows that need no debugger: CLI text, refusals and their
hints, selector errors, and which Rust bindings `describe` leaves out."""

import json
from types import SimpleNamespace

import pytest

from peeky.cli import _probe_text, main
from peeky.errors import classify
from peeky.probe import _table, parse_selector

VISIBLE = "count, e, expenses, friends, iter @ main.rs:64, iter @ main.rs:74, k, name, payer, share"


def _row(hit, selector, **fields):
    return {"hit": hit, "phase": "observed", "line": 76, "selector": selector, **fields}


def test_a_probe_table_lists_the_visible_names_once():
    # kitty 6: at line 76, every stop in `settle` (not the closure) restated the frame's
    # ten names for `f` and again for `f.name`, 16 times in a 28-hit probe.
    missing = {"function": "kitty::settle", "error": f"not found; visible: {VISIBLE}"}
    rows = [
        _row(1, "f", **missing),
        _row(1, "f.name", **missing),
        _row(1, "name", function="kitty::settle", type="&str *", value='"Åsa"'),
        _row(6, "f", **missing),
        _row(6, "f.name", **missing),
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
        _row(1, "x", error=f"not found; visible: {VISIBLE}"),
        _row(1, "y", error=other),
        _row(2, "z", error=hinted),
        _row(3, "x", error=f"not found; visible: {VISIBLE}"),
    ]
    text = _table(
        {"rows": rows, "interventions": [], "status": "completed", "hits": 3, "seconds": 1}
    )
    assert other in text and hinted in text, text
    assert "<not found; visible: as listed at hit 1>" in text, text


def test_session_get_lists_the_visible_names_once():
    from peeky.cli import _repeats_shortened

    rows = [
        {"selector": "nosuch", "error": f"not found: 'nosuch' …; visible: {VISIBLE}"},
        {"selector": "nosuch2", "error": f"not found: 'nosuch2' …; visible: {VISIBLE}"},
        {"selector": "k", "type": "usize", "value": "1"},
    ]
    shown = _repeats_shortened(rows)
    assert shown[0] == rows[0] and shown[2] == rows[2]
    assert shown[1]["error"] == "not found: 'nosuch2' …; visible: as listed above"
    assert rows[1]["error"].endswith(VISIBLE)


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
    # debounce 3: 5 selectors at 32 hits printed 160 lines; `queries wide` gives 32.
    pointer = (
        "evidence: one line per hit, a column per selector: `peeky queries wide --param run=7`"
    )
    long = _probe_text(_long_result(["ms", "db.stable", "db.candidate", "db.count", "changes"], 32))
    assert long.endswith(pointer), long[-300:]
    # A short table, or one selector, needs none.
    assert pointer not in _probe_text(_long_result(["ms", "db.count"], 3))
    assert pointer not in _probe_text(_long_result(["ms"], 60))


@pytest.mark.parametrize(
    ("selector", "advice"),
    [
        ("self.items[at]", "capture `at` first, then use its value, as self.items[2]"),
        ("xs[i + 1]", "capture what it is computed from first, then use its value, as xs[2]"),
        ("d[k]", "capture `k` first, then use its value, as d[2]"),
    ],
)
def test_an_index_named_by_a_variable_says_to_read_it_first(selector, advice):
    # outline 4: `self.items[at]` was refused, saying only that selectors are paths.
    with pytest.raises(ValueError) as refused:
        parse_selector(selector)
    message = str(refused.value)
    assert "an index or key is a literal (a number, or text in quotes)" in message, message
    assert message.endswith(advice), message


def test_a_too_long_number_gets_no_such_advice():
    with pytest.raises(ValueError) as refused:
        parse_selector("xs[1234567]")
    assert "capture" not in str(refused.value)


def test_a_python_field_set_is_refused_as_other_languages_refuse_it(tmp_path, capsys):
    # prep 4: `peeky: [MUTATION_REJECTED] …` on stderr, where C, Rust and Java refusals
    # before the run print `error [MUTATION_REJECTED]: …`; and its hint said "rows marked
    # [settable] can be changed here", which Python never shows.
    program = tmp_path / "a.py"
    program.write_text("x = 1\n")
    code = main(["probe", str(program), "--at", "1", "--set", "s.end=10"])
    captured = capsys.readouterr()
    assert code == 2 and captured.err == "", captured.err
    lines = captured.out.splitlines()
    assert lines[0].startswith("error [MUTATION_REJECTED]: --set s.end changes a field"), lines
    assert lines[1] == (
        "hint: Set a local of the stopped function instead, or add one with a candidate edit "
        "(`--old`/`--new`) and set it in the same probe."
    ), lines
    assert "[settable]" not in captured.out
    # `--json` is unchanged: the probe's refusal before launch.
    assert main(["probe", str(program), "--at", "1", "--set", "s.end=10", "--json"]) == 2
    shown = json.loads(capsys.readouterr().out)
    assert shown["status"] == "failed" and shown["refused_before_launch"] is True
    assert shown["error"]["code"] == "MUTATION_REJECTED"
    assert "[settable]" not in shown["error"]["hint"]


def test_a_python_sessions_field_set_hint_names_no_settable_rows():
    # The same refusal in a session, from the worker.
    error = classify(
        ValueError(
            "Only the stopped function's own locals can be changed for this language "
            "(fields and elements cannot)"
        )
    )
    assert error["code"] == "MUTATION_REJECTED" and "[settable]" not in error["hint"], error
    # A native debugger too old for field changes keeps the hint: its rows are marked.
    old = classify(
        ValueError(
            "Only the stopped function's own locals can be changed for this language with "
            "this debugger, which cannot change fields and elements"
        )
    )
    assert "[settable]" in old["hint"], old


def test_a_local_pointers_refusal_gives_no_call_advice():
    # prep 9: `--set e.payer=0` on a `for` item over a slice said "change the value where
    # it is owned, before the call that passed it", and its hint named `--at <call line>`.
    from peeky.fields import local_pointer_advice

    message = (
        "`e.payer` cannot be changed: `e` is a pointer: Peeky does not write through a "
        f"pointer, since what it reaches may be freed or moved; {local_pointer_advice('e')}"
    )
    assert "before the call" not in message and "no call passed it" in message
    error = classify(ValueError(message))
    assert error["code"] == "MUTATION_REJECTED", error
    assert "call" not in error["hint"] and "--old" in error["hint"], error
    assert len(error["hint"]) <= 150


MIR = """// MIR for `parse` after PreCodegen

fn parse(_1: &str) -> Result<i64, ParseIntError> {
    debug text => _1;                    // in scope 0 at src/main.rs:1:10: 1:14
    let mut _0: std::result::Result<i64, std::num::ParseIntError>; // return place in scope 0 at src/main.rs:1:25: 1:61
    let _2: i64;                         // in scope 0 at src/main.rs:2:9: 2:14
    let _9: std::result::Result<std::convert::Infallible, std::num::ParseIntError>; // in scope 0 at src/main.rs:2:41: 2:42
    let _12: i64;                        // in scope 0 at src/main.rs:2:22: 2:42
    scope 1 {
        debug cents => _2;               // in scope 1 at src/main.rs:2:9: 2:14
        let _13: i64;                    // in scope 1 at src/main.rs:3:9: 3:12
        scope 6 {
            debug val => _13;            // in scope 6 at src/main.rs:3:9: 3:12
        }
    }
    scope 2 {
        debug residual => _9;            // in scope 2 at src/main.rs:2:41: 2:42
        scope 3 {
        }
    }
    scope 4 {
        debug val => _12;                // in scope 4 at src/main.rs:2:22: 2:42
        scope 5 {
        }
    }

    bb0: {
        return;                          // scope 0 at src/main.rs:5:2: 5:2
    }
}
"""
SOURCE = """fn parse(text: &str) -> Result<i64, std::num::ParseIntError> {
    let cents: i64 = text.trim().parse()?;
    let val = cents + 1;
    Ok(val)
}
"""


def _try_bindings(tmp_path, source=SOURCE, mir=MIR):
    from peeky.rust import RustSession
    from peeky.rust_mir import parse_body

    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src/main.rs").write_text(source)
    body = parse_body(mir)
    session = SimpleNamespace(
        lines={"src/main.rs": (1, 2, 3, 4, 5)},
        prepared=SimpleNamespace(source_root=tmp_path),
    )
    texts = {}
    return lambda name, line: RustSession._try_bindings(
        session, [body], "src/main.rs", name, line, texts
    )


def test_a_questions_marks_bindings_are_told_by_where_their_spans_end(tmp_path):
    # prep 7: `describe` listed `residual @ main.rs:47` and `val @ main.rs:47` beside the
    # program's own names. The DWARF doesn't mark them artificial; the MIR's span of each
    # ends at the `?`.
    desugared = _try_bindings(tmp_path)
    assert desugared("residual", 2) and desugared("val", 2)
    # The program's own `val`, and its other names, end with their names: listed.
    assert not desugared("val", 3) and not desugared("cents", 2) and not desugared("text", 1)


def test_a_binding_is_listed_where_its_source_says_otherwise(tmp_path):
    # The same spans over a source whose line 2 has no `?` there (a stale or other file).
    changed = SOURCE.replace("parse()?;", "parse().unwrap();")
    assert not _try_bindings(tmp_path, source=changed)("residual", 2)
    # A binding whose span the MIR doesn't give is listed.
    bare = MIR.replace(
        "// in scope 2 at src/main.rs:2:41: 2:42", "// in scope 2 at src/main.rs:2:41"
    )
    assert not _try_bindings(tmp_path, mir=bare)("residual", 2)
