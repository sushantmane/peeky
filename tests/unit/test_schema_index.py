"""Eleventh evaluation: `peeky sql --schema` printed 20 KB in one block, which two agents'
tools cut off, and the canned queries' parameters were hard to find (an agent passed
set_effect `a` and `b`)."""

import json
import re

import pytest

from peeky import evidence
from peeky.cli import main


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr()


@pytest.fixture
def store(tmp_path):
    return tmp_path / "evidence.db"


# What the index lists, in order: the tables, the views and the other parts of the whole
# schema. Spelled out here, not taken from evidence.SCHEMA_PARTS, so that a part left
# out of the code fails these tests (review of PR #307: `joins` was).
NAMES = [
    "runs",
    "captures",
    "interventions",
    "pauses",
    "evidence",
    "last_run",
    "joins",
    "queries",
    "functions",
    "examples",
    "rules",
]
# The whole schema's headings, in order: one line each, unindented.
HEADINGS = [
    "store:",
    "table runs:",
    "table captures:",
    "table interventions:",
    "table pauses:",
    "view evidence:",
    "view last_run:",
    "joins:",
    "canned queries (peeky queries <name> [--param k=v]):",
    "functions:",
    "examples:",
    "rules:",
]


def _tagged(table, column):
    """A column's name as `--schema` shows it: `element_size [C, C++ only]`."""
    only = evidence._COLUMN_LANGUAGES.get((table, column))
    return f"{column} [{only} only]" if only else column


def _main_schema_text(manifest):
    """`peeky sql --schema` on main (8b8c2f2), verbatim: what `--schema all` must still print,
    but for the languages that fill a column, beside its name (twelfth evaluation, desks 8)."""
    lines = [f"store: {manifest['store']} ({manifest['engine']})", ""]
    for name, info in manifest["tables"].items():
        lines.append(f"table {name}: {info['description']}")
        lines.extend(
            f"  {_tagged(name, column)}: {meaning}" for column, meaning in info["columns"].items()
        )
        lines.append("")
    lines.extend(f"view {name}: {meaning}" for name, meaning in manifest["views"].items())
    lines.append(f"joins: {manifest['joins']}")
    lines.append("")
    lines.append("canned queries (peeky queries <name> [--param k=v]):")
    for name, info in manifest["queries"].items():
        params = ", ".join(f"{p}: {d}" for p, d in info["params"].items())
        lines.append(f"  {name}: {info['description']}" + (f" [{params}]" if params else ""))
    lines.append("")
    lines.append("functions:")
    lines.extend(f"  {name}: {meaning}" for name, meaning in manifest["functions"].items())
    lines.append("")
    lines.append("examples:")
    lines.extend(f"  {example}" for example in manifest["examples"])
    lines.append("rules:")
    lines.extend(f"  - {rule}" for rule in manifest["rules"])
    return "\n".join(lines)


def _headings(text):
    return [
        next(h for h in HEADINGS if line.startswith(h))
        for line in text.splitlines()
        if line and not line.startswith(" ")
    ]


def _advertised(index):
    """The names the index lists, each on a line of its own: `  name   what it is`."""
    return [m[1] for line in index.splitlines() if (m := re.match(r"  (\w+) {2,}\S", line))]


def test_schema_alone_is_a_short_index_of_every_table_view_and_part(store, capsys):
    code, out = run(capsys, "sql", "--schema", "--store", store)
    text = out.out
    assert code == 0 and len(text.encode()) < 2048, len(text.encode())
    assert _advertised(text) == NAMES
    lines = text.splitlines()
    assert "  runs           One row per probe run (31 columns)" in lines
    assert (
        "  evidence       captures with some of their run's columns (label, program, status, "
        "args, ...; not output)"
    ) in lines
    manifest = evidence.schema(store)
    assert f"  joins          {manifest['joins']}" in lines
    assert "UTC start time" not in text  # a column's meaning: --schema runs has it
    assert "`peeky queries --help` lists their parameters" in text
    assert lines[-1].startswith("`peeky sql --schema NAME [NAME ...]` shows those in full")
    assert "--schema all shows everything" in lines[-1]


def test_schema_json_stays_the_whole_manifest(store, capsys):
    code, out = run(capsys, "sql", "--schema", "--json", "--store", store)
    assert code == 0 and json.loads(out.out) == json.loads(json.dumps(evidence.schema(store)))


def test_every_name_the_index_lists_together_keeps_the_whole_schema(store, capsys):
    code, out = run(capsys, "sql", "--schema", "--store", store)
    names = _advertised(out.out)
    assert names == NAMES
    code, out = run(capsys, "sql", "--schema", *names, "--store", store)
    assert code == 0, out.err
    whole = _main_schema_text(evidence.schema(store))
    shown = set(out.out.splitlines())
    assert [line for line in whole.splitlines() if line and line not in shown] == []
    assert _headings(out.out) == HEADINGS
    code, out = run(capsys, "sql", "--schema", *names, "--json", "--store", store)
    assert code == 0 and json.loads(out.out) == json.loads(json.dumps(evidence.schema(store)))


def test_schema_joins_prints_the_joins(store, capsys):
    manifest = evidence.schema(store)
    code, out = run(capsys, "sql", "--schema", "joins", "--store", store)
    assert code == 0, out.err
    assert out.out == f"store: {store} ({manifest['engine']})\n\njoins: {manifest['joins']}\n"
    code, out = run(capsys, "sql", "--schema", "joins", "--json", "--store", store)
    assert code == 0 and json.loads(out.out) == {
        "store": str(store),
        "engine": manifest["engine"],
        "joins": manifest["joins"],
    }


def test_the_joins_say_which_run_columns_the_evidence_view_has(store):
    # Review 2 of PR #307: "the evidence view, which already has the run's columns", though
    # it has only some; output, error_code, seconds and warnings need a join with runs.
    manifest = evidence.schema(store)
    joins = manifest["joins"]
    assert "already has the run's columns" not in joins
    assert "The evidence view has some of the run's columns" in joins
    connection = evidence._ensure(store)
    try:
        viewed = {row[1] for row in connection.execute("PRAGMA table_info(evidence)")}
        runs = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    finally:
        connection.close()
    has, _, others = joins.partition("; the others, such as ")
    for column in ("label", "program", "status", "args", "sets", "language"):
        assert column in has and column in viewed and column in runs, column
    for column in ("output", "error_code", "seconds", "warnings"):
        assert column in others and column in runs and column not in viewed, column
    assert others.endswith("need a join with runs")


def test_each_query_summary_names_the_phases_and_runs_its_sql_reads():
    # Review 2 of PR #307: `hits` and `values` read returned values as well as observed
    # ones, and `values` leaves out candidate runs; `join`'s key takes several selectors.
    for name, (_, sql, params, _) in evidence.QUERIES.items():
        summary = evidence.QUERY_SUMMARIES[name]
        if isinstance(sql, str) and "phase IN ('observed', 'returned')" in sql:
            assert "observed and returned values" in summary, name
        if isinstance(sql, str) and "NOT candidate" in sql:
            assert "non-candidate runs" in summary, name
        if "several" in params.get("key", ""):
            assert "one or more key selectors" in summary, name
    assert {
        name
        for name, (_, sql, _, _) in evidence.QUERIES.items()
        if isinstance(sql, str) and "phase IN ('observed', 'returned')" in sql
    } == {"hits", "values"}


def test_outputs_claims_no_outcome_for_an_older_run_that_recorded_none(store, capsys):
    # Review 3 of PR #307: `outputs` said it gives "how they went", but a run saved before
    # schema 16 has no runs.set_outcomes, and with no warning about its changes nothing
    # says how they went. Its summary says "where recorded", and its rows claim nothing.
    import sqlite3

    evidence.record(
        store,
        {
            "probe": {
                "program": "a.c",
                "line": 8,
                "set": [["n", "3"]],
                "set_hit": 2,
                "set_at": [[9, 4, "x", "7"]],
            },
            "status": "completed",
            "warnings": [],
        },
    )
    connection = sqlite3.connect(store)
    connection.executescript("ALTER TABLE runs DROP COLUMN set_outcomes; PRAGMA user_version = 15;")
    connection.close()
    (row,) = evidence.run_query(store, "outputs", {"a": 1, "b": 1})["rows"]
    assert json.loads(row["sets"]) == [
        ["n", "3", "requested at 8@2"],
        ["x", "7", "requested at 9@4"],
    ]
    code, out = run(
        capsys, "queries", "outputs", "--param", "a=1", "--param", "b=1", "--store", store
    )
    assert code == 0, out.err
    text = out.out
    assert "requested at 8@2" in text and "requested at 9@4" in text, text
    for claim in ("made", "refused", "never came", "the cap", "failed"):
        assert claim not in text, claim
    assert "with how they went, where recorded" in evidence.QUERY_SUMMARIES["outputs"]
    assert "with how it went where recorded" in evidence.QUERIES["outputs"][0]


def test_schema_names_print_those_tables_views_and_parts_in_full(store, capsys):
    manifest = evidence.schema(store)
    code, out = run(capsys, "sql", "--schema", "captures", "Last_Run", "rules", "--store", store)
    text = out.out
    assert code == 0, out.err
    assert text.startswith(f"store: {store} (")
    assert _headings(text) == ["store:", "table captures:", "view last_run:", "rules:"]
    assert "table captures: One row per captured selector, per hit and phase." in text
    for column, meaning in manifest["tables"]["captures"]["columns"].items():
        assert f"\n  {_tagged('captures', column)}: {meaning}\n" in text + "\n", column
    assert f"view last_run: {manifest['views']['last_run']}" in text
    assert "rules:\n  - Read-only: one SELECT" in text
    code, out = run(capsys, "sql", "--schema", "captures", "last_run", "--json", "--store", store)
    part = json.loads(out.out)
    assert code == 0 and set(part) == {"store", "engine", "tables", "views"}
    assert part["tables"] == {"captures": manifest["tables"]["captures"]}
    assert part["views"] == {"last_run": manifest["views"]["last_run"]}
    code, out = run(capsys, "sql", "--schema", "queries", "--store", store)
    assert code == 0 and "  set_effect: Each selector before and after" in out.out
    assert "[run: Run id (default: the newest run)]" in out.out


def test_schema_all_prints_what_main_printed(store, capsys):
    code, out = run(capsys, "sql", "--schema", "all", "--store", store)
    assert code == 0 and out.out == _main_schema_text(evidence.schema(store)) + "\n"
    assert _headings(out.out) == HEADINGS
    assert len(out.out.encode()) > 16 * 1024
    code, out = run(capsys, "sql", "--schema", "all", "--json", "--store", store)
    assert json.loads(out.out) == json.loads(json.dumps(evidence.schema(store)))


def test_an_unknown_schema_name_is_refused_with_the_names_there_are(store, capsys):
    code, out = run(capsys, "sql", "--schema", "capture", "runs", "--json", "--store", store)
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "INVALID_QUERY", error
    assert error["message"] == (
        "Evidence query failed: --schema has no `capture`: name a table (runs, captures, "
        "interventions, pauses), a view (evidence, last_run), joins, queries, functions, "
        "examples, rules, or all"
    )
    assert "--schema all" in error["hint"] and len(error["hint"]) <= 150


def test_sql_and_schema_together_are_still_refused(store, capsys):
    for argv in (
        ["SELECT 1", "--schema"],
        ["SELECT 1", "--schema", "runs"],
        ["--schema", "SELECT 1"],
        ["--schema", "runs", "SELECT * FROM runs"],
    ):
        code, out = run(capsys, "sql", *argv, "--json", "--store", store)
        error = json.loads(out.out)["error"]
        assert code == 2 and error["message"] == "Pass either SQL or --schema, not both", argv


# Each canned query's parameters and what it answers, as `peeky queries --help` lists
# them; each summary checked against the query's full description and SQL (review of
# PR #307: `values` leaves out candidate runs and after-set values, and `wide` has a row
# per hit and phase).
HELP = [
    ("runs [count]", "the latest runs (20 unless count), newest first"),
    ("run [run]", "every captured value of one run, in capture order"),
    (
        "hits [run] [selector]",
        "each selector's observed and returned values by hit, with changed (NULL where unknown)",
    ),
    (
        "compare [a] [b]",
        "every value of two runs side by side, paired by hit; first the status of a run that "
        "did not complete, and any requested interventions",
    ),
    (
        "diff [a] [b]",
        "how two runs compare: outcome, then what differs (or may) in output, edit, source, "
        "line, arguments, interventions and captured values",
    ),
    (
        "outputs [a] [b]",
        "arguments, requested interventions (with how they went, where recorded), outcome and "
        "output of two runs",
    ),
    ("set_effect [run]", "values before and after one run's interventions"),
    (
        "values selector",
        "one selector's distinct observed and returned values per program and line, across "
        "non-candidate runs",
    ),
    ("wide [run] [phase]", "one run, a row per hit and phase, a column per selector"),
    (
        "join [a] [b] key [phase] [masked] [unchanged]",
        "two runs' hits paired by the values of one or more key selectors, then the unpaired hits",
    ),
    ("unreadable [run]", "the captures of one run that have no value, with unavailable and error"),
    ("failures [count]", "the latest runs that did not complete (20 unless count), newest first"),
]


def test_queries_help_lists_each_query_with_its_parameters(capsys):
    with pytest.raises(SystemExit):
        main(["queries", "--help"])
    text = capsys.readouterr().out
    _, heading, listing = text.partition(
        "\ncanned queries, each with the --param names it takes ([optional]):\n"
    )
    assert heading, text
    listed = [
        (m[1], m[2])
        for line in listing.splitlines()
        if (m := re.fullmatch(r"  (\w+(?: \[?\w+\]?)*) {2,}(\S.*)", line))
    ]
    assert listed == HELP
    assert "a and b pick two runs (default: the last two); run picks one" in text
    assert set(evidence.QUERY_SUMMARIES) == set(evidence.QUERIES)


@pytest.mark.parametrize(
    "argv, message",
    [
        (
            ["set_effect", "--param", "a=2", "--param", "b=3"],
            "set_effect does not take a, b; it takes: run",
        ),
        (["values"], "values needs --param selector=<value>"),
        (["set_efect"], "no canned query 'set_efect'"),
    ],
)
def test_a_wrong_canned_query_or_parameter_points_to_queries_help(store, capsys, argv, message):
    code, out = run(capsys, "queries", *argv, "--json", "--store", store)
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "INVALID_QUERY" and message in error["message"]
    assert error["hint"].startswith("`peeky queries --help` lists each canned query's --param")
    assert len(error["hint"]) <= 150


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
