"""The twelfth agent evaluation's Lows that need no debugger (#346): what probe, query,
session and describe text say, and a candidate's note."""

import json

import pytest

from peeky import cli, evidence
from peeky.probe import _table

LONG_NAME = "laps::summary::h3ba6f5e90fc2267c"


def _result(rows=(), interventions=(), language="rust", **more):
    return {
        "rows": list(rows),
        "interventions": list(interventions),
        "status": "completed",
        "hits": 1,
        "seconds": 1.0,
        "target_identity": {"language": language},
        **more,
    }


def _row(selector, value, phase="observed", hit=1, type="u32", **fields):
    row = {"hit": hit, "phase": phase, "line": 63, "function": "tally::count"}
    return {**row, "selector": selector, "type": type, "value": value, **fields}


def _probe_result(args=(), function=LONG_NAME):
    return {
        "probe": {
            "program": "a.py",
            "line": 1,
            "capture": ["i"],
            "set": [],
            "hits": 1,
            "set_hit": 1,
            "args": list(args),
        },
        "status": "completed",
        "hits": 1,
        "seconds": 0.5,
        "pauses_ms": [1.0],
        "rows": [_row("i", "1", function=function)],
        "by_hit": [],
        "interventions": [],
        "warnings": [],
        "output": "",
        "target_identity": {"language": "python"},
        "source_sha256": "0" * 64,
        "inputs_sha256": "a" * 64,
    }


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """`peeky probe` of a stand-in result, saved to a state directory of its own."""
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "a.py").write_text("x = 1\n")
    shown = {}
    monkeypatch.setattr(cli, "run_probe", lambda *args, **kwargs: dict(shown["result"]))

    def run(result, *extra):
        shown["result"] = result
        return cli.main(["probe", str(tmp_path / "a.py"), "--at", "1", *extra])

    return run


def test_a_query_table_notes_short_frame_names_once_per_state_directory(probe, capsys):
    # laps 6: `note: function shows short frame names; …` came under every query's table.
    note = "note: function shows short frame names"
    assert probe(_probe_result()) == 0
    assert note in capsys.readouterr().out
    for query in (["queries", "run"], ["sql", "SELECT * FROM last_run"], ["queries", "run"]):
        assert cli.main(query) == 0
        out = capsys.readouterr().out
        assert note not in out and "laps::summary " in out, out
    # The first query in a state directory says it, where no probe has yet.
    assert evidence.table(
        {"columns": ["function"], "rows": [{"function": LONG_NAME}], "row_count": 1}
        | {"truncated": False}
    ).count(note)


def test_the_after_set_footer_is_short():
    # laps 6: the footer ran to 190 characters under every probe with a change.
    rows = [_row("week", "6"), _row("week", "6", "after-set")]
    change = {"name": "pick", "before": "1", "after": "2", "hit": 1, "capture_hit": 1}
    (footer,) = [
        line for line in _table(_result(rows, [change])).splitlines() if line.startswith("after")
    ]
    assert footer == (
        "after-set: 1 number, flag or text row equal to the observed value left out; `--json` "
        "has all"
    )
    rows = [_row("free", "3", type="int"), _row("free", "3", "after-set", type="int")]
    text = _table(_result(rows, [change], language="java"))
    assert (
        "after-set: 1 number, flag or char row equal to the observed value left out (a String "
        "stays: it is an object); `--json` has all"
    ) in text, text


def test_outputs_lists_run_a_first_with_short_arguments_whole(probe, capsys):
    # laps 4: `queries outputs --param a=3 --param b=2` listed run 2 first, and cut both
    # runs' 12 reads at 120 characters.
    reads = [f"{car}@{at}.{n}" for car, at, n in zip([7, 12, 33] * 4, range(80, 92), range(12))]
    other = [*reads[:-1], "33@249.9"]
    assert probe(_probe_result(reads)) == 0 and probe(_probe_result(other)) == 0
    capsys.readouterr()
    assert cli.main(["queries", "outputs", "--param", "a=2", "--param", "b=1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith("2 ") and lines[2].startswith("1 "), lines
    assert json.dumps(other, ensure_ascii=False) in lines[1], lines
    assert json.dumps(reads, ensure_ascii=False) in lines[2], lines
    # Past 300 characters, both show from where they differ.
    many = [f"r{n}" for n in range(120)]
    assert probe(_probe_result(many)) == 0 and probe(_probe_result([*many[:-1], "x"])) == 0
    capsys.readouterr()
    assert cli.main(["queries", "outputs"]) == 0
    out = capsys.readouterr().out
    assert '"r118", "r119"]' in out and '"r118", "x"]' in out, out


def test_a_java_threads_mark_counts_the_jvms_own():
    # desks 4: a program of one thread read `other threads are running`.
    from peeky.java import _THREADS_RUNNING

    assert _THREADS_RUNNING.startswith("other threads, the JVM's own included, are running")


def test_a_final_fields_mark_shows_on_its_selectors_first_row_only():
    # desks 5: `p.name … [not settable: final]` on every row, after-set rows too.
    rows = [
        _row("p.name", '"Ana"', hit=hit, phase=phase, type="String", not_settable="final")
        for hit in (1, 2)
        for phase in ("observed", "after-set")
    ]
    rows.append(_row("p.desk", "3", type="int", not_settable="final"))
    rows.append(_row("p.load", "4", type="int", not_settable="a constant javac compiles"))
    rows.append(_row("p.load", "5", hit=2, type="int", not_settable="a constant javac compiles"))
    result = _result(rows, [{"name": "seat", "before": "1", "after": "2", "hit": 1}], "java")
    text = _table(result)
    assert text.count("[not settable: final]") == 2, text
    lines = text.splitlines()
    first = next(line for line in lines if "p.name" in line)
    assert first.endswith("[not settable: final]"), text
    # Other reasons may differ by stop: each row keeps its own.
    assert text.count("[not settable: a constant javac compiles]") == 2, text
    # So does another language's row, and JSON keeps every mark.
    rust = _table(_result([_row("x", "1", not_settable="final", hit=h) for h in (1, 2)]))
    assert rust.count("[not settable: final]") == 2, rust
    assert all(row["not_settable"] for row in rows)


def test_the_schema_marks_the_columns_only_some_languages_fill():
    # desks 8: a Java store's `sql --schema captures` listed C and C++ columns as its own.
    lines = evidence._schema_lines(evidence.schema("store.db"), "captures")
    marked = {line.split(":")[0].strip() for line in lines if " only]" in line.split(":")[0]}
    assert marked == {
        "identity [Python only]",
        "scope [Python only]",
        "instance [Rust only]",
        "stop [C, C++, Rust only]",
        "element_size [C, C++ only]",
        "at_element_of [C, C++ only]",
        "element_index [C, C++ only]",
        "offset_from [C, C++ only]",
        "element_offset [C, C++ only]",
    }
    assert "  value: Value as the debugger displays it" in "\n".join(lines)
    text = evidence.schema_part_text(evidence.schema_part(evidence.schema("s"), ["interventions"]))
    assert "  stop [C, C++, Rust only]: " in text and "  instance [Rust only]: " in text


def test_an_all_change_names_its_own_lines_stop():
    # rainfall 3: `(--set-at line 52@all, hit 1, in summarize)` read as the probe's hit 1.
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
    text = _table(_result(interventions=changes, language="python"))
    assert "set ready: True -> False  (--set-at line 52@all, stop 1 of line 52, in summarize)" in (
        text
    ), text
    assert "(--set-at line 52@all, stop 2 of line 52 at location 2 of 3, in summarize)" in text
    assert "set ready: 3 more changes by --set-at line 52@all, at stops 4 to 6 of line 52" in text
    assert ", hit " not in text and "at hits" not in text, text


def test_many_selectors_at_several_hits_point_at_queries_wide():
    # rainfall 5: 6 selectors at 6 hits, 36 rows, printed no pointer to one line per hit.
    def result(selectors, hits):
        rows = [_row(name, "0", hit=hit) for hit in range(1, hits + 1) for name in selectors]
        return _result(rows, evidence={"run_id": 4, "next": "peeky queries run --param run=4"})

    pointer = (
        "evidence: one line per hit, a column per selector: `peeky queries wide --param run=4`"
    )
    assert pointer in cli._probe_text(result(["name", "day", "mm", "open", "run", "first"], 6))
    assert pointer in cli._probe_text(result(["a", "b", "c", "d"], 3))
    assert pointer not in cli._probe_text(result(["a", "b", "c"], 6))
    assert pointer not in cli._probe_text(result(["a", "b", "c", "d"], 2))


def test_session_get_lists_an_arrays_first_elements():
    # playlist 4: `session get queue` listed all 32 elements, each with its hint.
    row = {
        "selector": "levels",
        "type": "Level[32]",
        "value": "[{n = 1}, …]",
        "children": [
            {"name": f"[{i}]", "selector": f"levels[{i}]", "type": "Level", "value": "{n = 1}"}
            | {"expandable": True}
            for i in range(32)
        ],
    }
    lines = cli._get_lines(row)
    assert len(lines) == 1 + 8 + 1, lines
    assert lines[8].startswith("  levels[7]: Level = {n = 1} (children:"), lines
    assert lines[-1] == (
        "  ... 24 more elements, levels[8] to levels[31]: `peeky session get 'levels[8]'` reads "
        "one; `--json` lists them all"
    )
    assert "--name s --frame 1` reads one" in cli._get_lines(row | {"frame": 1}, "s")[-1]
    # A struct's fields, or a short array, are listed whole.
    fields = {
        **row,
        "children": [{**c, "selector": f"levels.f{n}"} for n, c in enumerate(row["children"])],
    }
    assert len(cli._get_lines(fields)) == 33
    assert len(cli._get_lines({**row, "children": row["children"][:8]})) == 9


def test_a_candidate_that_moves_no_line_says_so():
    # playlist 5: "locations name the candidate's copy, since the line numbers are the
    # candidate's" for an edit that changed one line in place.
    original = "a = 1\nb = 2\nc = 3\n"
    same = {"candidate": {}}
    cli._bind_candidate(same, original, ("b = 2", "b = 3"), "p.py")
    assert same["candidate"]["note"] == cli.CANDIDATE_SAME_LINES
    assert "line numbers" in cli.CANDIDATE_SAME_LINES
    moved = {"candidate": {}}
    cli._bind_candidate(moved, original, ("b = 2\n", "b = 2\nb += 1\n"), "p.py")
    assert moved["candidate"]["note"] == cli.CANDIDATE_LOCATIONS
    # Two edits that add a line and remove one move the lines between them.
    both = {"candidate": {}}
    cli._bind_candidate(both, original, (("a = 1\n", "a = 1\nz = 0\n"), ("c = 3\n", "")), "p.py")
    assert both["candidate"]["note"] == cli.CANDIDATE_LOCATIONS


def test_describe_names_each_functions_settable_root_locals():
    # laps 3: `mutable:` named fields and registers per function, never `start` or `total`.
    text = cli._describe_text(
        {
            "language": "rust",
            "root": "/crate",
            "breakpoint_lines": {},
            "scopes": [
                {"scope": "tally::count", "lines": [56, 75], "names": ["car", "lap", "start"]}
                | {"types": {"car": "&str", "lap": "tally::Lap", "start": "u32"}}
                | {"settable_parts": {"lap": ["lap.start"]}},
                {"scope": "tally::show", "lines": [42, 44], "names": ["t"]}
                | {"types": {"t": "u32"}, "never_settable": {"t": "register"}},
                {"scope": "tally::main", "lines": [77, 92], "names": ["arg", "n", "x"]}
                | {"types": {"arg": "alloc::string::String", "n": "usize", "x": "u32"}}
                | {"ambiguous": {"x": 2}},
            ],
            "mutable_types": "integer root locals and fields",
            "commands": {},
            "timing": {"advice": "fast"},
        }
    )
    [line] = [line for line in text.splitlines() if line.startswith("mutable:")]
    assert (
        "integer root locals and fields. Root locals of those types (a stop's rows and `inspect` mark which "
        "can change there): tally::count: start; tally::main: arg, n. Such fields" in line
    ), line
    assert "Never settable, kept in a register" in line and "tally::show: t" in line, line


def test_a_rust_reference_to_a_struct_says_why_only_its_address_shows():
    # laps 2: `read  &laps::Read  0x…` showed no preview, mark or note, beside `read.car`.
    rows = [
        _row("read", "0x000000010536d900", type="&tally::Read"),
        _row("read.car", '"7"', type="String", view="memory"),
        _row("car", '"7"', type="&str", view="memory"),
        _row("v", "0x000000010536d000", type="&alloc::vec::Vec<u32>"),
        _row("p", "0x000000010536d000", type="*const tally::Read", unproven_pointer="raw"),
    ]
    text = _table(_result(rows))
    assert (
        "note: `read` shows only the address a reference holds: Peeky previews a struct "
        "through a reference only for a reference parameter whose fields are all plain "
        "scalars; capture a field instead, as `read.car`"
    ) in text, text
    assert text.count("note: ") == 1, text
    shown = _table(_result([_row("lap", "0x1000", type="&mut tally::Lap")]))
    assert "`lap` shows only" in shown and "as `lap.<field>`" in shown, shown
    # A C pointer, a previewed reference, and a row with a note get none.
    assert "only the address" not in _table(_result(rows[:1], language="cpp"))
    previewed = _row("read", "Read { at: 7 }", type="&tally::Read", view="memory")
    assert "only the address" not in _table(_result([previewed]))


@pytest.mark.parametrize(
    ("language", "source", "note"),
    [
        ("rust", "            continue; // the same pass", "line 4 is `continue`, not run yet"),
        ("python", "            continue  # no reading", "line 4 is `continue`, not run yet"),
        ("cpp", "        } else {", "line 4 is `} else {`: its code is often the jump"),
        ("java", "        } else if (cmd == '<') {", "line 4 starts `} else if`: its code may"),
        ("python", "        else:", None),
        ("cpp", "            continue_with(x);", None),
        ("cpp", "        if (x) {", None),
    ],
)
def test_a_step_onto_a_jump_line_says_what_it_does(tmp_path, language, source, note):
    # laps 7, playlist 7: a step onto `continue;` or `} else {` came with no word.
    program = tmp_path / "main.src"
    program.write_text("a\nb\nc\n" + source + "\ne\n")
    record = {"root": str(tmp_path), "program": "main.src", "language": language}
    state = {"state": "stopped", "location": f"{program}:4", "reason": "step"}
    said = cli._step_line_note(record, state)
    assert (said is None) if note is None else said.startswith(note), said
    if said:
        assert len(said) <= 150, said
    assert cli._step_line_note(record, {**state, "location": "main.src:4"}) == said
    assert cli._step_line_note(record, {**state, "location": f"{program}:9"}) is None
    assert cli._step_line_note(record, {**state, "state": "terminated"}) is None
