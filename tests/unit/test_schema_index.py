"""Eleventh evaluation: `peeky sql --schema` printed 20 KB in one block, which two agents'
tools cut off, and the canned queries' parameters were hard to find (an agent passed
set_effect `a` and `b`)."""

import json

import pytest

from peeky import evidence
from peeky.cli import main


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr()


@pytest.fixture
def store(tmp_path):
    return tmp_path / "evidence.db"


def test_schema_alone_is_a_short_index_of_every_table_and_view(store, capsys):
    code, out = run(capsys, "sql", "--schema", "--store", store)
    text = out.out
    assert code == 0 and len(text.encode()) < 2048, len(text.encode())
    manifest = evidence.schema(store)
    lines = text.splitlines()
    for name in [*manifest["tables"], *manifest["views"], *evidence.SCHEMA_PARTS]:
        assert sum(line.startswith(f"  {name} ") for line in lines) == 1, name
    assert "  runs           One row per probe run (31 columns)" in lines
    assert "  evidence       captures joined with their run, so no join is needed" in lines
    assert "UTC start time" not in text  # a column's meaning: --schema runs has it
    assert "`peeky queries --help` lists their parameters" in text
    assert lines[-1].startswith("`peeky sql --schema NAME [NAME ...]` shows those in full")
    assert "--schema all shows everything" in lines[-1]


def test_schema_json_stays_the_whole_manifest(store, capsys):
    code, out = run(capsys, "sql", "--schema", "--json", "--store", store)
    assert code == 0 and json.loads(out.out) == json.loads(json.dumps(evidence.schema(store)))


def test_schema_names_print_those_tables_views_and_parts_in_full(store, capsys):
    manifest = evidence.schema(store)
    code, out = run(capsys, "sql", "--schema", "captures", "Last_Run", "rules", "--store", store)
    text = out.out
    assert code == 0, out.err
    assert text.startswith(f"store: {store} (")
    assert "table captures: One row per captured selector, per hit and phase." in text
    for column, meaning in manifest["tables"]["captures"]["columns"].items():
        assert f"\n  {column}: {meaning}\n" in text + "\n", column
    assert f"view last_run: {manifest['views']['last_run']}" in text
    assert "rules:\n  - Read-only: one SELECT" in text
    assert "table runs:" not in text and "view evidence:" not in text
    assert "functions:" not in text and "canned queries" not in text
    code, out = run(capsys, "sql", "--schema", "captures", "last_run", "--json", "--store", store)
    part = json.loads(out.out)
    assert code == 0 and set(part) == {"store", "engine", "tables", "views"}
    assert part["tables"] == {"captures": manifest["tables"]["captures"]}
    assert part["views"] == {"last_run": manifest["views"]["last_run"]}
    code, out = run(capsys, "sql", "--schema", "queries", "--store", store)
    assert code == 0 and "  set_effect: Each selector before and after" in out.out
    assert "[run: Run id (default: the newest run)]" in out.out


def test_schema_all_prints_everything(store, capsys):
    code, out = run(capsys, "sql", "--schema", "all", "--store", store)
    assert code == 0 and out.out == evidence.schema_text(evidence.schema(store)) + "\n"
    assert len(out.out.encode()) > 16 * 1024
    code, out = run(capsys, "sql", "--schema", "all", "--json", "--store", store)
    assert json.loads(out.out) == json.loads(json.dumps(evidence.schema(store)))


def test_an_unknown_schema_name_is_refused_with_the_names_there_are(store, capsys):
    code, out = run(capsys, "sql", "--schema", "capture", "runs", "--json", "--store", store)
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "INVALID_QUERY", error
    assert error["message"] == (
        "Evidence query failed: --schema has no `capture`: name a table (runs, captures, "
        "interventions, pauses), a view (evidence, last_run), queries, functions, examples, "
        "rules, or all"
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


def test_queries_help_lists_each_query_with_its_parameters(capsys):
    with pytest.raises(SystemExit):
        main(["queries", "--help"])
    text = capsys.readouterr().out
    lines = [line.split("  ")[1] for line in text.splitlines() if line.startswith("  ")]
    for usage in (
        "runs [count]",
        "diff [a] [b]",
        "compare [a] [b]",
        "outputs [a] [b]",
        "set_effect [run]",
        "values selector",
        "wide [run] [phase]",
        "join [a] [b] key [phase] [masked] [unchanged]",
    ):
        assert usage in lines, usage
    for name, summary in evidence.QUERY_SUMMARIES.items():
        assert f"  {evidence.query_usage(name)} " in text and summary in text, name
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
