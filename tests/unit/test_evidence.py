"""The captured-evidence store: record probe results, query them read-only (ADR 0004)."""

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from peeky import evidence
from peeky.errors import classify


def result(starts=("3", "6"), output="PAGE: [10]\n", sets=(), status="completed", error=None):
    rows = []
    for hit, start in enumerate(starts, 1):
        rows.append(
            dict(hit=hit, phase="observed", line=8, function="f", selector="start", value=start)
        )
        rows[-1]["type"] = "int"
        rows.append(dict(hit=hit, phase="observed", line=8, selector="gone", error="not visible"))
    changes = []
    if sets:
        rows.append(dict(hit=1, phase="after-set", line=8, selector="start", value=sets[0][1]))
        changes = [{"name": sets[0][0], "before": starts[0], "after": sets[0][1]}]
    return {
        "probe": {
            "program": "a.py",
            "line": 8,
            "capture": ["start", "gone"],
            "set": [list(change) for change in sets],
            "hits": len(starts),
            "set_hit": 1,
            "args": ["x"],
        },
        "status": status,
        "error": error,
        "hits": len(starts),
        "seconds": 1.5,
        "pauses_ms": [2.5] * len(starts),
        "output": output,
        "rows": rows,
        "interventions": changes,
        "warnings": ["w"],
        "artifact_manifest_id": "m1",
        "source_sha256": {"a.py": "ab" * 32},
        "target_identity": {"language": "python"},
    }


@pytest.fixture
def store(tmp_path):
    return tmp_path / "evidence" / "evidence.db"


def test_a_recorded_probe_is_queryable_with_numbers_and_identity(store):
    first = evidence.record(store, result(), language="python", target="/t", label="base")
    second = evidence.record(store, result(sets=[("start", "0")]), worker="http://127.0.0.1:1")
    assert (first, second) == (1, 2)
    assert store.stat().st_mode & 0o777 == 0o600
    rows = evidence.query(
        store,
        "SELECT run_id, hit, selector, value, num, error FROM evidence "
        "ORDER BY run_id, hit, selector DESC",
    )["rows"]
    assert rows[0] == {
        "run_id": 1,
        "hit": 1,
        "selector": "start",
        "value": "3",
        "num": 3.0,
        "error": None,
    }
    assert rows[1]["value"] is None and rows[1]["error"] == "not visible"
    run = evidence.query(store, "SELECT * FROM runs WHERE run_id = 1")["rows"][0]
    assert run["label"] == "base" and run["status"] == "completed" and run["max_pause_ms"] == 2.5
    assert json.loads(run["args"]) == ["x"] and run["artifact_manifest_id"] == "m1"
    assert json.loads(run["source_sha256"]) == {"a.py": "ab" * 32}
    latest = evidence.query(store, "SELECT DISTINCT run_id FROM last_run")["rows"]
    assert latest == [{"run_id": 2}]
    changes = evidence.query(store, "SELECT * FROM interventions")["rows"]
    # A result without the line (a worker before #137): its after-set rows' line.
    assert changes == [
        {
            "run_id": 2,
            "hit": 1,
            "name": "start",
            "before": "3",
            "after": "0",
            "line": 8,
            "capture_hit": 1,
            "function": None,
            "instance": None,
            "stop": None,
        }
    ]
    assert evidence.query(store, "SELECT worker FROM runs WHERE run_id = 2")["rows"] == [
        {"worker": "http://127.0.0.1:1"}
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO runs (started_at) VALUES ('x')",
        "UPDATE captures SET value = '0'",
        "DELETE FROM runs",
        "DROP VIEW last_run",
        "CREATE TEMP TABLE t (x)",
        "ATTACH 'other.db' AS other",
        "PRAGMA user_version = 7",
        "PRAGMA journal_mode",
        "PRAGMA writable_schema = ON",
        "VACUUM",
        "SELECT load_extension('x')",
        "SELECT 1; DELETE FROM runs",
        "SELECT breakpoint FROM runs",
        "",
    ],
)
def test_queries_are_read_only_single_selects_and_classified(store, sql):
    evidence.record(store, result())
    with pytest.raises(ValueError, match="^Evidence query failed") as caught:
        evidence.query(store, sql)
    payload = classify(caught.value, session_closed=False)
    assert payload["code"] == "INVALID_QUERY" and "--schema" in payload["hint"]
    connection = sqlite3.connect(store)
    assert connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)
    assert connection.execute("PRAGMA user_version").fetchone() == (evidence.SCHEMA_VERSION,)
    assert not (store.parent / "other.db").exists()


def test_schema_introspection_is_allowed(store):
    evidence.record(store, result())
    columns = evidence.query(store, "SELECT name FROM pragma_table_info('captures')")["rows"]
    assert [row["name"] for row in columns][:3] == ["run_id", "hit", "phase"]
    names = evidence.query(store, "SELECT name FROM sqlite_master WHERE type = 'view'")["rows"]
    assert {row["name"] for row in names} == {"evidence", "last_run"}


def test_queries_are_bounded_in_time_size_and_rows(store):
    evidence.record(store, result(starts=("1", "2", "3")))
    started = time.monotonic()
    with pytest.raises(ValueError, match="ran longer than 0.3 s"):
        evidence.query(
            store,
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
            "SELECT COUNT(*) FROM c",
            seconds=0.3,
        )
    assert time.monotonic() - started < 3
    with pytest.raises(ValueError, match="too big"):
        evidence.query(store, "SELECT zeroblob(2000000)")
    capped = evidence.query(store, "SELECT * FROM captures", limit=2)
    assert capped["row_count"] == 2 and capped["truncated"] is True
    assert evidence.query(store, "SELECT * FROM captures", limit=6)["truncated"] is False
    for limit in (0, evidence.MAX_ROWS + 1, 2.5):
        with pytest.raises(ValueError, match="--limit"):
            evidence.query(store, "SELECT 1", limit=limit)


def test_results_are_strict_json_with_unique_columns(store):
    evidence.record(store, result())
    shaped = evidence.query(store, "SELECT 1 AS a, 2 AS a, 1e999 AS big, x'00ff' AS raw")
    assert shaped["columns"] == ["a", "a_2", "big", "raw"]
    assert shaped["rows"] == [{"a": 1, "a_2": 2, "big": "inf", "raw": "x'00ff'"}]
    json.dumps(shaped, allow_nan=False)
    assert "1 row)" in evidence.table(shaped)


def test_only_the_newest_runs_are_kept(store, monkeypatch):
    monkeypatch.setattr(evidence, "MAX_RUNS", 3)
    for _ in range(5):
        evidence.record(store, result(sets=[("start", "0")]))
    connection = sqlite3.connect(store)
    for table in ("runs", "captures", "interventions", "pauses"):
        kept = {row[0] for row in connection.execute(f"SELECT run_id FROM {table}")}
        assert kept == {3, 4, 5}, table


@pytest.mark.parametrize("kind", ["foreign", "newer", "garbage", "symlink"])
def test_unusable_stores_are_refused_without_touching_them(store, kind):
    store.parent.mkdir(parents=True)
    if kind == "foreign":
        sqlite3.connect(store).execute("CREATE TABLE mine (x)")
    elif kind == "newer":
        sqlite3.connect(store).execute("PRAGMA user_version = 99")
    elif kind == "garbage":
        store.write_bytes(b"not a database" * 100)
    else:
        target = store.parent / "real.db"
        evidence.record(target, result())
        os.symlink(target, store)
    before = store.read_bytes() if kind != "symlink" else None
    for call in (
        lambda: evidence.record(store, result()),
        lambda: evidence.query(store, "SELECT 1"),
    ):
        with pytest.raises(ValueError, match="^Evidence store") as caught:
            call()
        payload = classify(caught.value, session_closed=False)
        assert payload["code"] == "INVALID_REQUEST" and "--store" in payload["hint"]
    if before is not None:
        assert store.read_bytes() == before


def test_concurrent_probes_record_into_a_new_store(store):
    ids, failures = [], []

    def save():
        try:
            ids.append(evidence.record(store, result()))
        except Exception as exc:  # pragma: no cover - reported below
            failures.append(exc)

    threads = [threading.Thread(target=save) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures and sorted(ids) == list(range(1, 9))


def test_canned_queries_answer_comparisons(store):
    evidence._ensure(store).close()
    for name in evidence.QUERIES:
        params = {"selector": "start"} if name == "values" else {}
        params = {"key": "start"} if name == "join" else params
        if name == "wide":
            # Its columns come from a run: with none, it says so.
            with pytest.raises(ValueError, match="no saved run;"):
                evidence.run_query(store, name, params)
            continue
        if name in ("compare", "diff", "outputs", "join"):
            # Nothing to compare: said plainly, never an empty or one-sided comparison.
            with pytest.raises(ValueError, match="comparing needs two saved runs"):
                evidence.run_query(store, name, params)
            continue
        assert evidence.run_query(store, name, params)["row_count"] == 0
    evidence.record(store, result(starts=("3", "6"), output="PAGE: [40]\n"), label="one")
    evidence.record(store, result(starts=("3", "7"), output="PAGE: [10]\n"), label="two")
    diff = evidence.run_query(store, "diff")
    assert diff["rows"] == [
        # The outputs differ too: the first row says so (sixth evaluation, finding 9).
        {
            "run_a": 1,
            "run_b": 2,
            "hit": None,
            "phase": None,
            "selector": "(output)",
            "value_a": "PAGE: [40]\n",
            "value_b": "PAGE: [10]\n",
            "differs": 1,
            "note": "output differs at line 1, column 8",
        },
        {
            "run_a": 1,
            "run_b": 2,
            "hit": 2,
            "phase": "observed",
            "selector": "start",
            "value_a": "6",
            "value_b": "7",
            "differs": 1,
            "note": None,
        },
    ]
    assert evidence.run_query(store, "compare", {"a": 2, "b": 2})["rows"][0]["differs"] == 0
    hits = evidence.run_query(store, "hits", {"run": 1, "selector": "start"})["rows"]
    assert [(row["value"], row["changed"]) for row in hits] == [("3", None), ("6", 1)]
    outputs = evidence.run_query(store, "outputs")["rows"]
    assert [row["output"] for row in outputs] == ["PAGE: [40]\n", "PAGE: [10]\n"]
    assert "set_hit" not in outputs[0] and "sets" in outputs[0]
    values = evidence.run_query(store, "values", {"selector": "start"})["rows"]
    assert values[0] == {
        "program": "a.py",
        "line": 8,
        "value": "3",
        "seen": 2,
        "qualified": 0,
        "runs": 2,
        "first_run": 1,
        "last_run": 2,
    }
    unreadable = evidence.run_query(store, "unreadable")["rows"]
    assert {row["error"] for row in unreadable} == {"not visible"}
    evidence.record(store, result(sets=[("start", "0")]))
    effect = evidence.run_query(store, "set_effect")["rows"]
    assert effect == [
        {
            "hit": 1,
            "line": 8,
            "function": "f",
            "selector": "start",
            "observed": "3",
            "after_set": "0",
            "caveat_observed": None,
            "caveat_after_set": None,
            "interventions": "start: 3 -> 0",
        }
    ]
    evidence.record(store, result(status="failed", error={"code": "TIMEOUT", "message": "slow"}))
    failures = evidence.run_query(store, "failures")["rows"]
    assert failures[0]["run_id"] == 4 and failures[0]["error_code"] == "TIMEOUT"


def test_canned_query_parameters_are_checked(store):
    for name, params, message in (
        ("values", {}, "needs --param selector"),
        ("run", {"selector": "x"}, "does not take selector"),
        ("nope", {}, "no canned query 'nope'"),
    ):
        with pytest.raises(ValueError, match=message) as caught:
            evidence.run_query(store, name, params)
        assert classify(caught.value)["code"] == "INVALID_QUERY"
    assert evidence.parse_params(["run=3", "selector=a.b", "n=-2", "s=0x1"]) == {
        "run": 3,
        "selector": "a.b",
        "n": -2,
        "s": "0x1",
    }
    for bad in ("run", "1x=2", "a-b=1"):
        with pytest.raises(ValueError, match="--param"):
            evidence.parse_params([bad])
    evidence._ensure(store).close()
    with pytest.raises(ValueError, match="binding parameter :run"):
        evidence.query(store, "SELECT * FROM runs WHERE run_id = :run")


def test_schema_manifest_lists_every_table_view_and_query(store):
    manifest = evidence.schema(store)
    connection = evidence._ensure(store)
    try:
        for table, info in manifest["tables"].items():
            actual = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            assert actual == list(info["columns"]), table
        views = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
        }
    finally:
        connection.close()
    assert views == set(manifest["views"])
    evidence_columns = [
        column["name"]
        for column in evidence.query(store, "SELECT name FROM pragma_table_info('evidence')")[
            "rows"
        ]
    ]
    assert ", ".join(evidence_columns) == evidence._EVIDENCE_COLUMNS
    assert set(manifest["queries"]) == set(evidence.QUERIES)
    text = evidence.schema_text(manifest)
    assert "table captures" in text and "peeky queries" in text


def test_queries_never_create_or_write_the_store(store):
    for name in ("run", "runs"):
        with pytest.raises(ValueError, match="does not exist: no probe has been saved") as caught:
            evidence.run_query(store, name)
        payload = classify(caught.value, session_closed=False)
        assert payload["code"] == "INVALID_REQUEST" and "--store" in payload["hint"]
    assert not store.parent.exists()
    store.parent.mkdir()
    store.touch()
    with pytest.raises(ValueError, match="is empty: no probe has been saved"):
        evidence.query(store, "SELECT 1")
    assert store.read_bytes() == b""
    store.unlink()
    evidence.record(store, result())
    store.chmod(0o444)
    store.parent.chmod(0o500)
    try:
        assert evidence.query(store, "SELECT COUNT(*) AS n FROM runs")["rows"] == [{"n": 1}]
    finally:
        store.parent.chmod(0o700)
        store.chmod(0o600)
    store.chmod(0o000)
    try:
        if not os.access(store, os.R_OK):  # root can read anything
            with pytest.raises(ValueError, match="^Evidence store .* cannot be read"):
                evidence.query(store, "SELECT 1")
    finally:
        store.chmod(0o600)


def test_large_program_output_is_stored_with_room_to_sort_and_group(store):
    big = result(output="x" * 2_000_000)
    big["output_truncated"] = False
    evidence.record(store, big)
    evidence.record(store, result(output="y" * 2_000_000))
    run = evidence.query(store, "SELECT length(output) AS n, output_truncated FROM runs")["rows"]
    assert run[0] == {"n": evidence.MAX_OUTPUT_BYTES, "output_truncated": 1}
    for sql in (
        "SELECT run_id, output FROM runs ORDER BY seconds",
        "SELECT * FROM runs ORDER BY started_at DESC",
        "SELECT DISTINCT output FROM runs",
        "SELECT output, COUNT(*) FROM runs GROUP BY output",
        "SELECT output, output, output FROM runs ORDER BY label",
    ):
        assert evidence.query(store, sql)["row_count"] == 2, sql
    assert evidence.run_query(store, "outputs")["row_count"] == 2


def test_the_whole_result_and_its_width_are_bounded(store, monkeypatch):
    evidence.record(store, result())
    monkeypatch.setattr(evidence, "MAX_RESULT_BYTES", 2500)
    wide = (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c LIMIT 50) "
        "SELECT x, printf('%.*c', 1000, 'y') AS pad FROM c"
    )
    shaped = evidence.query(store, wide, limit=50)
    assert shaped["row_count"] == 2 and shaped["truncated_by"] == "size"
    assert "stopped at 2500 bytes" in evidence.table(shaped)
    assert evidence.query(store, "SELECT * FROM captures", limit=1)["truncated_by"] == "rows"
    with pytest.raises(ValueError, match="one row exceeds 2500 bytes"):
        evidence.query(store, "SELECT printf('%.*c', 3000, 'y')")
    # Counted as emitted JSON: 250 characters here are 3000 escaped bytes.
    with pytest.raises(ValueError, match="one row exceeds 2500 bytes"):
        evidence.query(store, "SELECT replace(printf('%.*c', 250, 'x'), 'x', '\U0001f600')")
    assert evidence.query(store, "SELECT x'00ff' AS raw")["rows"] == [{"raw": "x'00ff'"}]
    columns = ", ".join(f"{i} AS c{i}" for i in range(evidence.MAX_COLUMNS + 1))
    with pytest.raises(ValueError, match="too many columns"):
        evidence.query(store, f"SELECT {columns}")


def test_a_save_killed_midway_is_recovered_by_the_next_query(store):
    evidence.record(store, result())
    killer = (
        "import os, sqlite3, sys\n"
        "c = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "c.execute('PRAGMA cache_size = 1')\n"
        "c.execute('BEGIN IMMEDIATE')\n"
        "for i in range(3000):\n"
        "    c.execute(\"INSERT INTO runs (started_at, output) VALUES ('x', ?)\", ('z' * 500,))\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", killer, str(store)], check=True)
    assert store.with_name(store.name + "-journal").stat().st_size > 0
    assert evidence.query(store, "SELECT COUNT(*) AS n FROM runs")["rows"] == [{"n": 1}]
    assert evidence.record(store, result()) == 2


def test_an_unsearchable_store_directory_is_a_store_error(store):
    evidence.record(store, result())
    store.parent.chmod(0o000)
    try:
        if os.access(store.parent, os.X_OK):  # root can search anything
            pytest.skip("running as root")
        with pytest.raises(ValueError, match="^Evidence store .* cannot be read") as caught:
            evidence.query(store, "SELECT 1")
        assert classify(caught.value, session_closed=False)["code"] == "INVALID_REQUEST"
    finally:
        store.parent.chmod(0o700)


@pytest.mark.parametrize(
    "reported, language, stored",
    [
        # The worker's account of what the program received wins: configured
        # arguments used without --arg are recorded, not an empty list.
        (["cfg"], "c", ["cfg"]),
        ([], "python", []),
        # A failure before launch has no account: the requested arguments stand.
        (None, "rust", ["x"]),
        ("not-a-list", "java", ["x"]),
        ([1], "java", ["x"]),
        # An attached JVM's own arguments are unknown.
        (None, "java-attach", None),
    ],
)
def test_runs_record_the_arguments_the_program_received(store, reported, language, stored):
    recorded = result()
    if reported is not None:
        recorded["program_args"] = reported
    evidence.record(store, recorded, language=language)
    args = evidence.query(store, "SELECT args FROM runs")["rows"][0]["args"]
    assert (None if args is None else json.loads(args)) == stored


def captured(rows, **probe):
    """A probe result with explicit (hit, phase, selector, value) rows; by default it
    asked for exactly the selectors it captured."""
    recorded = result()
    recorded["rows"] = [
        dict(hit=hit, phase=phase, line=8, selector=selector, value=value)
        for hit, phase, selector, value in rows
    ]
    recorded["probe"]["capture"] = list(dict.fromkeys(row[2] for row in rows))
    recorded["probe"].update(probe)
    return recorded


def test_diff_puts_the_first_real_divergence_first(store):
    """Capture order, not alphabetical; addresses and one-sided selectors come last."""
    text_a = '0x000000016bb97aae "the quick brown fox"'
    text_b = '0x000000016d2c3aae "the quick brown fox"'
    evidence.record(
        store,
        captured(
            [
                (1, "observed", "words", "3"),
                (1, "observed", "in_word", "1"),
                (1, "observed", "text", text_a),
                (1, "observed", "totals", "[]"),
                (2, "observed", "words", "4"),
                (2, "observed", "in_word", "0"),
                (2, "observed", "text", text_a),
                (2, "observed", "totals", "[]"),
                (3, "observed", "words", "5"),
                (3, "observed", "in_word", "0"),
                (3, "observed", "text", text_a),
                (3, "observed", "totals", "[]"),
            ]
        ),
    )
    evidence.record(
        store,
        captured(
            [
                (1, "observed", "words", "3"),
                (1, "observed", "in_word", "1"),
                (1, "observed", "text", text_b),
                (1, "observed", "zeta", "9"),
                (2, "observed", "words", "4"),
                (2, "observed", "in_word", "1"),
                (2, "observed", "text", text_b),
                (2, "observed", "zeta", "9"),
            ]
        ),
    )
    rows = evidence.run_query(store, "diff")["rows"]
    shape = [(r["hit"], r["selector"], r["note"]) for r in rows]
    assert shape == [
        (2, "in_word", None),
        (3, "words", "missing in run b"),
        (3, "in_word", "missing in run b"),
        (3, "text", "missing in run b"),
        # Seventh evaluation, finding 9: one row per selector, saying at how many hits.
        (1, "text", "addresses only, at 2 hits (one row per selector: its first hit)"),
        # Eval 5 (C): these rows showed NULL values, which read as "no value"; each
        # now shows its selector's first hit.
        (1, "totals", "only in run a (one row per selector: its first hit)"),
        (1, "zeta", "only in run b (one row per selector: its first hit)"),
    ]
    assert all(r["differs"] == 1 for r in rows)
    only = {
        r["selector"]: (r["value_a"], r["value_b"])
        for r in rows
        if r["selector"] in ("totals", "zeta")
    }
    assert only["zeta"] == (None, "9") and only["totals"][1] is None and only["totals"][0]
    compared = evidence.run_query(store, "compare")["rows"]
    # Every row of both runs, in capture order within each hit.
    assert [r["selector"] for r in compared if r["hit"] == 1] == [
        "words",
        "in_word",
        "text",
        "totals",
        "zeta",
    ]
    assert {(r["selector"], r["note"]) for r in compared if r["hit"] == 1} >= {
        ("words", None),
        ("text", "addresses only"),
        ("totals", "only in run a"),
        ("zeta", "only in run b"),
    }


@pytest.mark.parametrize(
    "value, masked",
    [
        ("0x000000016bb97aae", "0x…"),
        ('0x16fc5ef38 "hi"', '0x… "hi"'),
        ("<O object at 0x104a3b2c0>", "<O object at 0x…>"),
        ("0x00005555deadbeef", "0x…"),
        # NULL, all-ones, sentinels and short hex are values, never addresses: they
        # still count as differences.
        ("0x0000000000000000", "0x0000000000000000"),
        ("0xffffffffffffffff", "0xffffffffffffffff"),
        ("0x00000000deadbeef", "0x00000000deadbeef"),
        ("flags=0x80000000", "flags=0x80000000"),
        ("0x1234", "0x1234"),
        ("10", "10"),
        (None, None),
    ],
)
def test_only_pointer_sized_addresses_are_masked(value, masked):
    assert evidence._no_addresses(value) == masked


def test_no_addresses_is_available_to_ad_hoc_sql(store):
    evidence.record(store, captured([(1, "observed", "p", "0x000000016bb97aae")]))
    rows = evidence.query(store, "SELECT no_addresses(value) AS v FROM last_run")["rows"]
    assert rows == [{"v": "0x…"}]
    assert "no_addresses(text[, language])" in evidence.schema(store)["functions"]


def test_runs_show_the_start_of_each_output(store):
    evidence.record(store, result(output="CHARGED: 566 ORDERS: 30\n" + "x" * 100))
    [run] = evidence.run_query(store, "runs")["rows"]
    assert run["output_start"] == ("CHARGED: 566 ORDERS: 30\n" + "x" * 100)[:60]


def test_a_null_pointer_is_a_real_divergence(store):
    """NULL against a live pointer is the first divergence, never 'addresses only'."""
    evidence.record(
        store,
        captured(
            [
                (1, "observed", "p.next", "0x00000001051a8660"),
                (2, "observed", "p.next", "0x00000001051a8aa0"),
                (3, "observed", "p.next", "0x0000000000000000"),
            ]
        ),
    )
    evidence.record(
        store,
        captured(
            [
                (1, "observed", "p.next", "0x00000001056bc660"),
                (2, "observed", "p.next", "0x0000000000000000"),
            ]
        ),
    )
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["hit"], r["note"]) for r in rows] == [
        (2, None),
        (3, "missing in run b"),
        (1, "addresses only"),
    ]


def test_a_run_that_never_stopped_is_missing_hits_not_selectors(store):
    evidence.record(store, captured([(1, "observed", "item", "1"), (2, "observed", "item", "2")]))
    no_stop = captured([], capture=["item"])
    no_stop["status"] = "no-stop"
    evidence.record(store, no_stop)
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["hit"], r["value_a"], r["note"]) for r in rows] == [
        (None, "completed", "outcome differs"),
        (1, "1", "missing in run b"),
        (2, "2", "missing in run b"),
    ]


def test_comparisons_need_two_runs_that_exist(store):
    evidence.record(store, captured([(1, "observed", "x", "1")]))
    with pytest.raises(ValueError, match="run a = None, run b = 1"):
        evidence.run_query(store, "diff")
    evidence.record(store, captured([(1, "observed", "x", "2")]))
    with pytest.raises(ValueError, match="run a = 7"):
        evidence.run_query(store, "compare", {"a": 7})
    assert evidence.run_query(store, "compare", {"a": 2, "b": 2})["row_count"] == 1
    assert evidence.run_query(store, "diff", {"a": 1, "b": 2})["row_count"] == 1


def test_values_changed_by_an_intervention_rank_before_real_differences(store):
    # Tenth evaluation (debounce): the intervention's own rows came after 70 rows of
    # what may follow from it.
    evidence.record(store, captured([(1, "observed", "x", "1"), (2, "observed", "x", "5")]))
    evidence.record(
        store,
        captured(
            [
                (1, "observed", "x", "1"),
                (1, "after-set", "x", "0"),
                (2, "observed", "x", "6"),
            ],
            set=[["x", "0"]],
        ),
    )
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["hit"], r["phase"], r["note"]) for r in rows] == [
        (
            None,
            None,
            "the runs requested different interventions (--set, --set-at), or made different ones",
        ),
        (1, "after-set", "intervention only in run b"),
        (2, "observed", None),
    ]


def test_the_text_schema_lists_sql_functions(store):
    evidence._ensure(store).close()
    assert "no_addresses(text[, language])" in evidence.schema_text(evidence.schema(store))


def test_no_addresses_cannot_outrun_the_query_deadline(store):
    evidence.record(store, captured([(1, "observed", "x", "1")]))
    calls = ", ".join(f"length(no_addresses(s)) AS c{i}" for i in range(20))
    sql = (
        "WITH RECURSIVE big(s) AS MATERIALIZED (SELECT substr(replace(printf('%.*c', 249000, "
        "'a'), 'a', '0x1 '), 1, 999000)), r(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM r "
        f"WHERE i < 100000) SELECT {calls} FROM r, big"
    )
    started = time.monotonic()
    with pytest.raises(ValueError, match="ran longer than"):
        evidence.query(store, sql, seconds=0.5)
    assert time.monotonic() - started < 3


def test_diff_starts_with_the_outcome_and_a_trimmed_output_when_they_differ(store):
    first = captured([(1, "observed", "x", "1")])
    first["output"] = "SUM:14\n"
    evidence.record(store, first)
    second = captured([(1, "observed", "x", "1"), (1, "after-set", "x", "2")])
    second.update(output="x" * 300, status="failed")
    evidence.record(store, second)
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["selector"], r["note"]) for r in rows] == [
        ("(status)", "outcome differs"),
        ("(output)", "output differs at line 1, column 1"),
        ("x", "intervention only in run b"),
    ]
    assert rows[1]["value_a"] == "SUM:14\n" and rows[1]["value_b"] == "x" * 200 + "…"
    # Same outputs: no output row.
    evidence.record(store, first)
    evidence.record(store, first)
    assert evidence.run_query(store, "diff")["row_count"] == 0


def test_the_output_row_shows_where_long_outputs_first_differ(store):
    prefix = "".join(f"log line {n}\n" for n in range(40))
    first, second = captured([(1, "observed", "x", "1")]), captured([(1, "observed", "x", "1")])
    first["output"], second["output"] = prefix + "RESULT: 14\n", prefix + "RESULT: 15\n"
    evidence.record(store, first)
    evidence.record(store, second)
    [row] = evidence.run_query(store, "diff")["rows"]
    assert row["note"] == f"output differs at line 41, column {len('RESULT: 1') + 1}"
    assert row["value_a"].startswith("…") and row["value_a"].endswith("RESULT: 14\n")
    assert row["value_b"].endswith("RESULT: 15\n") and row["value_a"] != row["value_b"]


def test_hits_and_run_keep_an_error_out_of_the_value_column(store):
    # A selector that has no value at one hit (Java's `current.end` while `current` is
    # null) is not a value, so it is never compared as one.
    recorded = result(starts=("10", "10", "3"))
    recorded["rows"][0] = dict(
        hit=1, phase="observed", line=8, selector="start", error="'current' is null"
    )
    evidence.record(store, recorded)
    hits = evidence.run_query(store, "hits", {"run": 1, "selector": "start"})["rows"]
    assert [(row["value"], row["error"], row["changed"]) for row in hits] == [
        (None, "'current' is null", None),
        ("10", None, None),
        ("3", None, 1),
    ]
    rows = evidence.run_query(store, "run", {"run": 1})["rows"]
    assert [(row["selector"], row["value"], row["error"]) for row in rows[:2]] == [
        ("start", None, "'current' is null"),
        ("gone", None, "not visible"),
    ]


def test_diff_says_when_the_runs_stopped_at_different_lines(store):
    evidence.record(store, captured([(1, "observed", "tokens", "0")]))
    evidence.record(store, captured([(1, "observed", "tokens", "1")], line=22))
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["selector"], r["value_a"], r["value_b"]) for r in rows] == [
        ("(line)", "a.py:8", "a.py:22"),
        ("tokens", "0", "1"),
    ]
    assert rows[0]["note"].startswith("the runs stopped at different lines")
    evidence.record(store, captured([(1, "observed", "tokens", "1")], program="b.py", line=22))
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["value_a"], r["value_b"]) for r in rows if r["selector"] == "(line)"] == [
        ("a.py:22", "b.py:22")
    ]


def test_integers_are_exact_in_num(store):
    # Eval 3 (#75): `num` was REAL, so a long showed as 600.0 and a u64 above 2**53 lost
    # digits.
    values = ("600", "9007199254740993", "-9223372036854775808", "18446744073709551615")
    evidence.record(store, result(starts=(*values, "1.5", "10.0", "inf")))
    rows = evidence.query(
        store, "SELECT value, num, typeof(num) AS kind FROM captures WHERE selector = 'start'"
    )["rows"]
    shown = [(row["value"], row["num"], row["kind"]) for row in rows]
    assert shown == [
        ("600", 600, "integer"),
        ("9007199254740993", 9007199254740993, "integer"),
        ("-9223372036854775808", -(2**63), "integer"),
        # Beyond 64 bits: approximate; `value` keeps the digits.
        ("18446744073709551615", 1.8446744073709552e19, "real"),
        ("1.5", 1.5, "real"),
        ("10.0", 10, "integer"),
        ("inf", None, "null"),
    ]


def test_a_version_3_store_upgrades_num_in_capture_order(store):
    store.parent.mkdir(parents=True)
    connection = sqlite3.connect(store)
    for statement in evidence._DDL:
        connection.execute(statement.replace("num NUMERIC", "num REAL"))
    connection.execute(
        "INSERT INTO runs (run_id, started_at, program, line, status) "
        "VALUES (1, 't', 'a.py', 8, 'completed')"
    )
    for value in ("600", "1.5", "7", "9007199254740993", "None"):
        connection.execute(
            "INSERT INTO captures (run_id, hit, phase, selector, value, num, unavailable) "
            "VALUES (1, 1, 'observed', 'x', ?, ?, 0)",
            (value, None if value == "None" else float(value)),
        )
    connection.execute("PRAGMA user_version = 3")
    connection.commit()
    connection.close()
    rows = evidence.query(store, "SELECT value, num, typeof(num) AS kind FROM evidence")["rows"]
    assert [(row["value"], row["num"], row["kind"]) for row in rows] == [
        ("600", 600, "integer"),
        ("1.5", 1.5, "real"),
        ("7", 7, "integer"),
        # Review of #82: recomputed from `value`, not carried over from the lossy REAL.
        ("9007199254740993", 9007199254740993, "integer"),
        ("None", None, "null"),
    ]
    connection = sqlite3.connect(store)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == evidence.SCHEMA_VERSION
    indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert {"captures_run", "captures_selector", "evidence", "last_run"} <= indexes
    assert "captures_v4" not in indexes
    connection.close()
    evidence.record(store, result())
    assert (
        evidence.run_query(store, "hits", {"run": 2, "selector": "start"})["rows"][1]["value"]
        == "6"
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_an_upgrade_blocked_by_a_read_only_directory_says_so(store):
    evidence.record(store, result())
    connection = sqlite3.connect(store)
    connection.execute("PRAGMA user_version = 3")
    connection.commit()
    connection.close()
    store.parent.chmod(0o500)
    try:
        with pytest.raises(ValueError, match="needs a one-time upgrade.*not writable"):
            evidence.query(store, "SELECT 1")
    finally:
        store.parent.chmod(0o700)


def test_java_object_numbers_differ_between_runs_like_addresses(store):
    # Review of #87: JDWP numbers objects in the order the debugger first touches them,
    # so the same object is `@143` in one run and `@144` in the next.
    a = captured([(1, "observed", "current", 'P@143 {label="a@5", next=P@161}')])
    b = captured([(1, "observed", "current", 'P@144 {label="a@5", next=P@162}')])
    c = captured([(1, "observed", "current", 'P@144 {label="a@6", next=P@162}')])
    d = captured([(1, "observed", "kind", "Class (Foo)@12")])
    e = captured([(1, "observed", "kind", "Class (Foo)@13")])
    for run in (a, b, c, d, e):
        evidence.record(store, run, language="java")
    rows = evidence.run_query(store, "diff", {"a": 1, "b": 2})["rows"]
    assert [row["note"] for row in rows] == ["addresses only"]
    # A String field's text is data, never an object number.
    rows = evidence.run_query(store, "diff", {"a": 2, "b": 3})["rows"]
    assert [row["note"] for row in rows] == [None]
    rows = evidence.run_query(store, "diff", {"a": 4, "b": 5})["rows"]
    assert [row["note"] for row in rows] == ["addresses only"]
    # Another language's `Type@N` is the program's own text (a Python repr).
    for value in ("Node@1", "Node@2"):
        evidence.record(store, captured([(1, "observed", "n", value)]), language="python")
    rows = evidence.run_query(store, "diff", {"a": 6, "b": 7})["rows"]
    assert [row["note"] for row in rows] == [None]
    masked = evidence.query(store, "SELECT no_addresses('User@1') AS v")["rows"]
    assert masked == [{"v": "User@1"}]
    masked = evidence.query(store, "SELECT no_addresses('User@1', 'java') AS v")["rows"]
    assert masked == [{"v": "User@…"}]


def test_diff_puts_the_outcome_rows_before_selectors_only_one_run_captured(store):
    # Sixth evaluation, finding 9: `only in run a` rows came before the decisive (output)
    # and (edit) rows.
    first = captured([(1, "observed", "lo", "0"), (1, "observed", "hi", "6")])
    first["output"] = "HISTO:2,2,2,1\n"
    evidence.record(store, first)
    second = captured([(1, "observed", "hi", "8")], capture=["hi"])
    second.update(output="HISTO:2,2,2,2\n", candidate={"sha256": "e", "diff": "--- h.c\n"})
    evidence.record(store, second)
    rows = evidence.run_query(store, "diff")["rows"]
    assert [r["selector"] for r in rows] == ["(output)", "(edit)", "hi", "lo"]
    assert rows[-1]["note"].startswith("only in run a")


def test_diff_says_when_the_runs_had_different_inputs_or_interventions(store):
    # Eval 4 (Rust): a candidate compared with a run on other input showed only the edit,
    # so the input's effect was easy to credit to the edit.
    evidence.record(store, captured([(1, "observed", "count", "2")], args=["aaabb"]))
    evidence.record(store, captured([(1, "observed", "count", "1")], args=["aaab"]))
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["selector"], r["value_a"], r["value_b"]) for r in rows] == [
        ("(args)", '["aaabb"]', '["aaab"]'),
        ("count", "2", "1"),
    ]
    assert rows[0]["note"].startswith("the runs had different program arguments")
    evidence.record(store, result(sets=(("start", "0"),)))
    evidence.record(store, result())
    rows = evidence.run_query(store, "diff")["rows"]
    sets = [r for r in rows if r["selector"] == "(sets)"]
    assert len(sets) == 1 and sets[0]["value_b"] == "[]" and "start" in sets[0]["value_a"]


def test_object_ref_says_which_values_are_the_same_java_object(store):
    # Eval 4 (Java): whether `best` was the list's worker or a copy took substr/instr SQL.
    evidence.record(
        store,
        captured(
            [
                (1, "observed", "best", 'Balance$Worker@56 {load=0, name="a"}'),
                (1, "observed", "workers[1]", 'Balance$Worker@100 {load=0, name="a"}'),
                (2, "observed", "best", 'Balance$Worker@100 {load=5, name="a"}'),
                (2, "observed", "workers[1]", 'Balance$Worker@100 {load=5, name="a"}'),
                (2, "observed", "kind", "Class (Balance)@7"),
                (2, "observed", "load", "5"),
                (2, "observed", "label", '"Worker@3"'),
            ]
        ),
        language="java",
    )
    rows = evidence.query(
        store,
        "SELECT b.hit, object_ref(b.value) = object_ref(w.value) AS same FROM last_run b "
        "JOIN last_run w ON w.hit = b.hit AND w.selector = 'workers[1]' "
        "WHERE b.selector = 'best' ORDER BY b.hit",
    )["rows"]
    assert rows == [{"hit": 1, "same": 0}, {"hit": 2, "same": 1}]
    refs = evidence.query(
        store, "SELECT selector, object_ref(value) AS ref FROM last_run WHERE hit = 2"
    )["rows"]
    assert {r["selector"]: r["ref"] for r in refs} == {
        "best": "Balance$Worker@100",
        "workers[1]": "Balance$Worker@100",
        "kind": "Class (Balance)@7",
        "load": None,
        "label": None,
    }


@pytest.mark.parametrize(
    "value, address",
    [
        # LLDB's pointers in C, C++ and Rust, with or without a type before them.
        ("0x00000001028f9800", 0x1028F9800),
        ("kitty::Friend * 0x0000000102ec5800", 0x102EC5800),
        ("(debounce *) 0x000000016fdfe8c0", 0x16FDFE8C0),
        # LLDB's text for a value with no value or summary of its own: where it is stored.
        ("&str @ 0x104afcf50", 0x104AFCF50),
        ("alloc::vec::Vec<&str, alloc::alloc::Global> @ 0x16b5e6a80", 0x16B5E6A80),
        # A C string: the pointer's address. Hex in the text is the program's data.
        ('0x000000016d78b298 "apple"', 0x16D78B298),
        ('0x16fc5ef38 "0x00000001deadbeef"', 0x16FC5EF38),
        # Two addresses in one value: the first.
        ("{next = 0x0000600000c04000, prev = 0x0000600000c04040}", 0x600000C04000),
        ("[<Node object at 0x104a5b2c0>, <Node object at 0x104a5b300>]", 0x104A5B2C0),
        # Linux: a PIE program's heap, and the stack.
        ("0x000055555556a2c0", 0x55555556A2C0),
        ("0x00007fffffffe3a8", 0x7FFFFFFFE3A8),
        # SQLite's integers are signed 64-bit: from 2**63 on, the two's complement.
        ("0x7fffffffffffffff", 2**63 - 1),
        ("0x8000000000000000", -(2**63)),
        ("0xffff800000001000", 0xFFFF800000001000 - 2**64),
        ("0xfffffffffffffffe", -2),
        # No address: what no_addresses leaves (NULL, all-ones, 8 significant digits or
        # fewer), a string's text, and Java and Python object numbers.
        (None, None),
        ("", None),
        ("4500", None),
        ("0x0000000000000000", None),
        ("0xffffffffffffffff", None),
        ("0x00000000deadbeef", None),
        ("0x10", None),
        ("0x…", None),
        ("<not shown: may point at freed memory>", None),
        ('"0x0000000102ec5850"', None),
        ("'0x0000000102ec5850'", None),
        # java-debug shows a String's text unescaped.
        ('"say "0x0000000102ec5850" twice"', None),
        ("Holder@12 {raw=0x0000000102ec5850}", None),
        ("int[3]@64 [1, 2, 3]", None),
        ("['a']  [id 6]", None),
    ],
)
def test_address_reads_the_first_address_in_a_value(value, address):
    # Eval 10 (kitty, Rust): heap addresses differ per run, so the agent matched pointers
    # across three runs by `substr(value, -3)`.
    assert evidence._address(value) == address


def test_address_is_in_ad_hoc_sql_where_large_addresses_subtract_and_order(store):
    evidence.record(
        store,
        captured(
            [
                (1, "observed", "p", "0x0000000102ec5850"),
                (1, "observed", "q", "0x0000000102ec5800"),
                (1, "observed", "hi", "0xffff800000001100"),
                (1, "observed", "lo", "0xffff800000001000"),
                (1, "observed", "s", '"Bo"'),
            ]
        ),
        language="rust",
    )
    at = "(SELECT address(value) FROM last_run WHERE selector = '{}')".format
    [row] = evidence.query(
        store,
        f"SELECT {at('p')} - {at('q')} AS near, {at('hi')} - {at('lo')} AS far, "
        f"typeof({at('hi')} - {at('lo')}) AS kind, {at('hi')} > {at('lo')} AS ordered, "
        f"printf('0x%x', {at('hi')}) AS hex, {at('s')} AS text",
    )["rows"]
    assert row == {
        "near": 0x50,
        "far": 0x100,
        "kind": "integer",
        "ordered": 1,
        "hex": "0xffff800000001100",
        "text": None,
    }
    described = evidence.schema(store)
    assert "COOKBOOK 4.8" in described["functions"]["address(text)"]
    assert "address(text)" in evidence.schema_text(described)


DOCS = Path(__file__).resolve().parents[2] / "docs"


def _documented_sql(page, heading):
    """The SQL of the first `peeky sql "..."` after ``heading`` on a docs page."""
    text = (DOCS / page).read_text()
    return re.search(r'peeky sql "(.*?)"', text[text.index(heading) :], re.S)[1]


def test_the_cookbooks_address_recipe_pairs_pointers_across_runs(store):
    # Kitty (eval 10): `parse` keeps each payer as a raw pointer into `trip.friends`; `main`
    # sorts the friends by name, so each pointer now finds whoever sits in its old slot.
    # Each run's heap starts elsewhere, and here its last hex digits differ too.
    payers = evidence.record(
        store,
        captured(
            [
                (1, "observed", "payer @ main.rs:47", "<not shown: may point at freed memory>"),
                (1, "observed", "payer @ main.rs:54", "0x0000000100e4d800"),
                (1, "observed", "cents @ main.rs:49", "4500"),
                (2, "observed", "payer @ main.rs:47", "<not shown: may point at freed memory>"),
                (2, "observed", "payer @ main.rs:54", "0x0000000100e4d850"),
                (2, "observed", "cents @ main.rs:49", "1000"),
                (3, "observed", "payer @ main.rs:47", "<not shown: may point at freed memory>"),
                (3, "observed", "payer @ main.rs:54", "0x0000000100e4d828"),
                (3, "observed", "cents @ main.rs:49", "900"),
            ]
        ),
        language="rust",
    )
    slots = evidence.record(
        store,
        captured(
            [
                (hit, "observed", selector, value)
                for hit, (at, name) in enumerate(
                    [(0xC40, "Bo"), (0xC68, "Chloé"), (0xC90, "Zoë"), (0xCB8, "Åsa")], 1
                )
                for selector, value in (("f", f"0x0000000100b51{at:x}"), ("f.name", f'"{name}"'))
            ]
        ),
        language="rust",
    )
    sql = _documented_sql("lang/rust-cookbook.md", "### Find who sits at a pointer's old slot")
    rows = evidence.query(store, sql, {"payers": payers, "slots": slots})["rows"]
    assert rows == [
        {"expense": 1, "cents": "4500", "from_base": 0, "credited": '"Bo"'},
        {"expense": 2, "cents": "1000", "from_base": 80, "credited": '"Zoë"'},
        {"expense": 3, "cents": "900", "from_base": 40, "credited": '"Chloé"'},
    ]
    # Neither the addresses nor their last three hex digits match between the runs.
    assert not evidence.query(
        store,
        "SELECT 1 FROM evidence p JOIN evidence f ON substr(f.value, -3) = substr(p.value, -3) "
        "WHERE p.run_id = :payers AND f.run_id = :slots",
        {"payers": payers, "slots": slots},
    )["rows"]
    # COOKBOOK 4.8's general form, with each run's lowest address as its base or, the
    # other base it names, the address at its first hit.
    sql = _documented_sql("COOKBOOK.md", "### 4.8 Compare Addresses Across Runs")
    lowest = "MIN(address(value)) OVER (PARTITION BY run_id, selector)"
    first = "FIRST_VALUE(address(value)) OVER (PARTITION BY run_id, selector ORDER BY hit)"
    assert lowest in sql
    for base in (lowest, first):
        documented = sql.replace("'p'", "'payer @ main.rs:54'").replace("'q'", "'f'")
        documented = documented.replace(lowest, base)
        rows = evidence.query(store, documented, {"a": payers, "b": slots})["rows"]
        assert rows == [
            {"hit_a": 1, "hit_b": 1, "from_base": 0},
            {"hit_a": 2, "hit_b": 3, "from_base": 80},
            {"hit_a": 3, "hit_b": 2, "from_base": 40},
        ]


def test_the_evidence_view_has_language_and_output_stays_one_join_away(store):
    # #93 (eval 4, Rust): comparing values with output needed a join, where `label`
    # was then ambiguous; Java masking needed runs.language.
    evidence.record(store, result(output="RLE:3a\n"), language="rust")
    rows = evidence.query(store, "SELECT selector, language FROM last_run")["rows"]
    assert rows[0] == {"selector": "start", "language": "rust"}
    # Review of #97: output on every row made SELECT * repeat it per capture and hit the
    # result cap; it stays in runs, and an ambiguous join column says why.
    loud = result(starts=tuple(map(str, range(100))), output="x" * 200_000)
    evidence.record(store, loud, language="python")
    everything = evidence.query(store, "SELECT * FROM last_run", limit=10000)
    assert everything["row_count"] == 200 and not everything["truncated"]
    assert "output" not in everything["columns"]
    joined = evidence.query(
        store,
        "SELECT e.selector, length(r.output) AS n FROM last_run e JOIN runs r USING (run_id)",
    )
    assert joined["rows"][0] == {"selector": "start", "n": 200_000}
    with pytest.raises(ValueError, match="ambiguous column name: label.*already carry.*r.output"):
        evidence.query(store, "SELECT label FROM evidence JOIN runs USING (run_id)")


def test_a_schema_4_store_gains_the_new_view_columns(store):
    evidence.record(store, result(), language="python")
    connection = sqlite3.connect(store)
    connection.executescript(
        "DROP VIEW last_run; DROP VIEW evidence; "
        "CREATE VIEW evidence AS SELECT c.run_id, c.selector FROM captures c; "
        "CREATE VIEW last_run AS SELECT * FROM evidence; PRAGMA user_version = 4;"
    )
    connection.close()
    rows = evidence.query(store, "SELECT language FROM last_run")["rows"]
    assert rows[0] == {"language": "python"}


def test_diff_notes_a_type_change_and_runs_of_different_source(store):
    # Eval 5 (C): the fix changed `total` from unsigned int to long long, and the diff
    # did not say so. Eval 5 (Python): runs of two different files compared silently.
    first = result(starts=("4294967294",))
    first["rows"][0]["type"] = "unsigned int"
    evidence.record(store, first, language="c")
    second = result(starts=("-2",))
    second["rows"][0]["type"] = "long long"
    second["source_sha256"] = "other"
    evidence.record(store, second, language="c")
    rows = {r["selector"]: r for r in evidence.run_query(store, "diff")["rows"]}
    assert rows["start"]["note"] == "type unsigned int -> long long"
    assert rows["start"]["hit"] == 1 and rows["start"]["value_b"] == "-2"
    assert "different source text" in rows["(source)"]["note"]
    # Review of #109: a run that failed before launch has no source hash; that is not
    # a different source.
    failed = result(starts=())
    failed["source_sha256"] = None
    evidence.record(store, failed, language="c")
    rows = {r["selector"] for r in evidence.run_query(store, "diff")["rows"]}
    assert "(source)" not in rows


def test_a_run_without_set_has_no_intervention_hit(store):
    # Eval 5 (Python): runs.set_hit said 1 for runs without --set.
    evidence.record(store, result())
    evidence.record(store, result(sets=(("start", "0"),)))
    rows = evidence.query(store, "SELECT sets, set_hit FROM runs ORDER BY run_id")["rows"]
    assert rows[0]["set_hit"] is None and rows[1]["set_hit"] == 1


def test_python_identities_and_scopes_are_stored_and_a_schema_5_store_gains_them(store):
    # #104: whether `seen` is the same list on every call needs Python object identity.
    # Its review: the number alone is the identity (an object keeps it when its class is
    # reassigned), and a row says whether a module global was read.
    run = captured(
        [
            (1, "observed", "seen", "['a']"),
            (1, "observed", "key", "'a'"),
            (2, "observed", "seen", "['a', 'b']"),
            (2, "observed", "LIMIT", "[1]"),
        ]
    )
    for row, identity in zip(run["rows"], (7, None, 7, 9)):
        if identity:
            row["identity"] = identity
    run["rows"][-1]["scope"] = "globals"
    evidence.record(store, run, language="python")
    connection = sqlite3.connect(store)
    connection.executescript(
        "DROP VIEW last_run; DROP VIEW evidence; ALTER TABLE captures DROP COLUMN identity; "
        "ALTER TABLE captures DROP COLUMN scope; "
        "CREATE VIEW evidence AS SELECT c.run_id, c.selector FROM captures c; "
        "CREATE VIEW last_run AS SELECT * FROM evidence; PRAGMA user_version = 5;"
    )
    connection.close()
    rows = evidence.query(store, "SELECT selector, identity, scope FROM last_run")["rows"]
    assert rows == [
        {"selector": s, "identity": None, "scope": None} for s in ("seen", "key", "seen", "LIMIT")
    ]
    evidence.record(store, run, language="python")
    rows = evidence.query(
        store,
        "SELECT hit, selector, identity, typeof(identity) AS kind, scope FROM last_run "
        "WHERE identity IS NOT NULL ORDER BY hit, selector",
    )["rows"]
    assert rows == [
        {"hit": 1, "selector": "seen", "identity": 7, "kind": "integer", "scope": None},
        {"hit": 2, "selector": "LIMIT", "identity": 9, "kind": "integer", "scope": "globals"},
        {"hit": 2, "selector": "seen", "identity": 7, "kind": "integer", "scope": None},
    ]
    described = evidence.schema(store)
    columns = described["tables"]["captures"]["columns"]
    assert "Within one hit, equal means the same object" in columns["identity"]
    assert "its class is reassigned" in columns["identity"]
    assert "'globals'" in columns["scope"]
    assert "captures.identity" in described["functions"]["object_ref(text)"]


def test_a_store_from_the_first_schema_6_gains_an_integer_identity_and_scope(store):
    # Review of #104 (round 2): its first build wrote schema 6 with `identity TEXT`
    # (`list@7`) and no `scope`, and later code skipped the migration ("table captures
    # has 13 columns but 14 values").
    run = captured([(1, "observed", "seen", "['a']"), (1, "observed", "key", "'a'")])
    evidence.record(store, run, language="python")
    connection = sqlite3.connect(store)
    connection.executescript(
        """
        DROP VIEW last_run; DROP VIEW evidence;
        CREATE TABLE early (
            run_id INTEGER NOT NULL, hit INTEGER, phase TEXT, line INTEGER, function TEXT,
            selector TEXT, type TEXT, value TEXT, num NUMERIC, unavailable INTEGER NOT NULL,
            error TEXT, view TEXT, identity TEXT);
        INSERT INTO early SELECT run_id, hit, phase, line, function, selector, type, value,
            num, unavailable, error, view,
            CASE WHEN selector = 'seen' THEN 'list@7' END FROM captures;
        DROP TABLE captures;
        ALTER TABLE early RENAME TO captures;
        CREATE VIEW evidence AS SELECT c.run_id, r.started_at, r.label, r.program,
            c.line, r.status, c.hit, c.phase, c.function, c.selector, c.type, c.value, c.num,
            c.unavailable, c.error, c.view, c.identity, r.args, r.sets,
            r.artifact_manifest_id, r.source_sha256, r.edit IS NOT NULL AS candidate,
            r.language FROM captures c JOIN runs r ON r.run_id = c.run_id;
        CREATE VIEW last_run AS SELECT * FROM evidence
            WHERE run_id = (SELECT MAX(run_id) FROM runs);
        PRAGMA user_version = 6;
        """
    )
    connection.close()
    rows = evidence.query(
        store, "SELECT selector, identity, typeof(identity) AS kind, scope FROM last_run"
    )["rows"]
    # #104 review (round 3): that version could read module globals without saying so.
    assert rows == [
        {"selector": "seen", "identity": 7, "kind": "integer", "scope": "unknown"},
        {"selector": "key", "identity": None, "kind": "null", "scope": "unknown"},
    ]
    assert (
        "'unknown' for a row stored before"
        in (evidence.schema(store)["tables"]["captures"]["columns"]["scope"])
    )
    run["rows"][0].update(identity=7, scope="globals")
    evidence.record(store, run, language="python")
    rows = evidence.query(store, "SELECT selector, identity, scope FROM last_run")["rows"]
    assert rows[0] == {"selector": "seen", "identity": 7, "scope": "globals"}
    connection = sqlite3.connect(store)
    assert connection.execute("PRAGMA user_version").fetchone() == (evidence.SCHEMA_VERSION,)
    indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert {"captures_run", "captures_selector"} <= indexes and "captures_v7" not in indexes
    connection.close()


def test_a_stale_rust_value_is_stored_as_stale(store):
    # #108: a field of a moved or dropped Rust local is the bytes it left behind; the
    # store says so in `view`, as a row's note does.
    recorded = captured([(1, "observed", "h.n", "9"), (1, "observed", "f.n", "3")])
    recorded["rows"][0]["stale"] = True
    evidence.record(store, recorded)
    rows = evidence.query(store, "SELECT selector, value, view FROM captures ORDER BY selector")
    assert rows["rows"] == [
        {"selector": "f.n", "value": "3", "view": None},
        {"selector": "h.n", "value": "9", "view": "stale"},
    ]


def test_a_table_shows_short_frame_names_and_sql_compares_whole_ones(store):
    # Eval 6, finding 16: a C++ signature doubled the table's width. The text shows the
    # short name and says so; the result, and what SQL compares, stay whole.
    whole = (
        "trim_left(std::__1::basic_string<char, std::__1::char_traits<char>, "
        "std::__1::allocator<char>> const&)"
    )
    probe = result()
    for row in probe["rows"]:
        row["function"] = whole
    evidence.record(store, probe, language="cpp")
    shaped = evidence.query(store, "SELECT DISTINCT function FROM captures")
    assert shaped["rows"] == [{"function": whole}]
    text = evidence.table(shaped)
    assert "trim_left\n" in text and "basic_string" not in text
    assert "short_function(function) = 'trim_left'" in text
    sql = "SELECT count(*) AS n FROM captures WHERE short_function(function) = 'trim_left'"
    assert evidence.query(store, sql)["rows"] == [{"n": 4}]
    assert evidence.query(store, "SELECT short_function(NULL) AS f")["rows"] == [{"f": None}]
    # Nothing shortened, no note.
    assert "note:" not in evidence.table(evidence.query(store, "SELECT 'main' AS function"))


def test_the_notes_example_matches_overloads_as_sql_does(store):
    # Review of the eval-6 fixes: with overloads the example read
    # `short_function(function) = 'twice(int)'`, which matches no row.
    probe = result()
    names = ["twice(int)", "twice(std::__1::basic_string<char> const&)"]
    for row, name in zip(probe["rows"], names * 2):
        row["function"] = name
    evidence.record(store, probe, language="cpp")
    text = evidence.table(evidence.query(store, "SELECT DISTINCT function FROM captures"))
    assert "twice(int)" in text and "short_function(function) = 'twice'" in text, text
    sql = "SELECT count(*) AS n FROM captures WHERE short_function(function) = 'twice'"
    assert evidence.query(store, sql)["rows"] == [{"n": 4}]


def test_closures_and_instances_a_table_shows_are_found_by_instance(store):
    # #151 review 4: the table showed `cz::main::{closure#0}` and `{closure#1}`, and its
    # note said to use short_function(function), which gives both `cz::main::{{closure}}`.
    probe = result()
    labels = ["cz::main::{closure#0}", None, "cz::main::{closure#1}", None]
    names = ["cz::main::{{closure}}::h1111111111111111", "cz::main::h3333333333333333"]
    for row, label in zip(probe["rows"], labels):
        row["function"] = names[0] if label else names[1]
        if label:
            row["instance"] = label
    evidence.record(store, probe, language="rust")
    sql = "SELECT instance, COUNT(*) AS n FROM captures GROUP BY instance ORDER BY instance"
    assert evidence.query(store, sql)["rows"] == [
        {"instance": None, "n": 2},
        {"instance": "cz::main::{closure#0}", "n": 1},
        {"instance": "cz::main::{closure#1}", "n": 1},
    ]
    sql = "SELECT DISTINCT instance FROM evidence WHERE short_function(function) = ?"
    merged = evidence.query(store, sql.replace("?", "'cz::main::{{closure}}'"))["rows"]
    assert len(merged) == 2, merged
    one = "SELECT COUNT(*) AS n FROM last_run WHERE instance = 'cz::main::{closure#0}'"
    assert evidence.query(store, one)["rows"] == [{"n": 1}]


def test_a_schema_12_store_gains_capture_instances(store):
    probe = result()
    probe["rows"][0]["instance"] = "gen::tf<gen::A>"
    evidence.record(store, probe)
    connection = sqlite3.connect(store)
    connection.executescript(
        "DROP VIEW last_run; DROP VIEW evidence; ALTER TABLE captures DROP COLUMN instance; "
        "PRAGMA user_version = 12;"
    )
    connection.close()
    rows = evidence.query(store, "SELECT DISTINCT instance FROM evidence")["rows"]
    assert rows == [{"instance": None}]
    connection = sqlite3.connect(store)
    assert connection.execute("PRAGMA user_version").fetchone() == (evidence.SCHEMA_VERSION,)
    connection.close()
    evidence.record(store, probe)
    sql = "SELECT run_id, instance FROM captures WHERE instance IS NOT NULL"
    assert evidence.query(store, sql)["rows"] == [{"run_id": 2, "instance": "gen::tf<gen::A>"}]


def test_wide_gives_one_row_per_hit_with_a_column_per_selector(store):
    # #139 (sixth evaluation): one row per hit took three-way self-joins on captures.
    selectors = ("now", "w.start", "bins[3].hi", 'ages["al"]', "d['k']", "line")
    rows = []
    for hit, now in enumerate(("0", "1", "10"), 1):
        rows += [(hit, "observed", selector, f"{now}{i}") for i, selector in enumerate(selectors)]
    rows.append((2, "after-set", "now", "7"))
    recorded = captured(rows, set=[["now", "7"]])
    # No value at hit 2 (withheld: never shown as one); none captured at hit 3; stale.
    recorded["rows"][7] = dict(hit=2, phase="observed", line=8, selector="w.start", error="gone")
    del recorded["rows"][14]
    recorded["rows"][2]["view"] = "stale"
    recorded["rows"][1]["view"] = "memory"
    evidence.record(store, recorded)
    evidence.record(store, captured([(1, "observed", "x", "1")]))
    wide = evidence.run_query(store, "wide", {"run": 1})
    assert wide["columns"] == [
        "hit",
        "phase",
        "line",
        *selectors[:-1],
        "line (selector)",
        "no_value",
        "caveats",
        "sets",
    ]
    shown = [
        (row["hit"], row["phase"], row["now"], row["w.start"], row["bins[3].hi"], row["no_value"])
        for row in wide["rows"]
    ]
    assert shown == [
        (1, "observed", "00", "01", "02", None),
        (2, "observed", "10", None, "12", '{"w.start":"gone"}'),
        (2, "after-set", "7", None, None, None),
        (3, "observed", "100", "101", None, None),
    ]
    assert [row["caveats"] for row in wide["rows"]] == ['{"bins[3].hi":"stale"}', None, None, None]
    assert wide["rows"][0]['ages["al"]'] == "03" and wide["rows"][0]["d['k']"] == "04"
    assert wide["rows"][0]["line (selector)"] == "05" and wide["rows"][0]["line"] == 8
    # The SQL it ran, to refine with peeky sql: the same rows.
    assert evidence.query(store, wide["sql"])["rows"] == wide["rows"]
    observed = evidence.run_query(store, "wide", {"run": 1, "phase": "observed"})["rows"]
    assert [row["phase"] for row in observed] == ["observed"] * 3
    # The newest run by default.
    assert evidence.run_query(store, "wide")["columns"][3] == "x"
    with pytest.raises(ValueError, match="no saved run 9"):
        evidence.run_query(store, "wide", {"run": 9})
    with pytest.raises(ValueError, match="phase is observed, after-set or returned"):
        evidence.run_query(store, "wide", {"phase": "later"})
    # A cell holds one value: a selector read twice at a hit must have read the same.
    evidence.record(store, captured([(1, "observed", "v", "1"), (1, "observed", "v", "1")]))
    assert [row["v"] for row in evidence.run_query(store, "wide")["rows"]] == ["1"]
    evidence.record(store, captured([(1, "observed", "v", "1"), (1, "observed", "v", "2")]))
    with pytest.raises(ValueError, match="'v' more than once at hit 1 .*different values"):
        evidence.run_query(store, "wide")
    # 94 selectors fit in 100 columns; more are refused, never cut short.
    many = [(1, "observed", f"s{i}", str(i)) for i in range(95)]
    evidence.record(store, captured(many[:94]))
    assert len(evidence.run_query(store, "wide")["columns"]) == 100
    evidence.record(store, captured(many))
    with pytest.raises(ValueError, match="wide would have 101 columns, more than 100"):
        evidence.run_query(store, "wide")


def test_join_links_two_runs_by_a_selectors_value(store):
    # #139: linking run 3 to run 4 by `v` took a correlated subquery; diff pairs by hit.
    first = captured(
        [
            (1, "observed", "v", "7"),
            (1, "observed", "bin", "3"),
            (2, "observed", "v", "2"),
            (2, "observed", "bin", "1"),
            (3, "observed", "v", "5"),
            (3, "observed", "bin", "2"),
            (4, "observed", "v", "9"),
            (4, "observed", "bin", "4"),
        ]
    )
    first["rows"][6] = dict(hit=4, phase="observed", line=8, selector="v", error="gone")
    evidence.record(store, first)
    second = captured(
        [
            (1, "observed", "v", "2"),
            (1, "observed", "tests", "1"),
            (2, "observed", "v", "7"),
            (2, "observed", "tests", "4"),
            (3, "observed", "v", "7"),
            (3, "observed", "tests", "5"),
            (4, "observed", "v", "8"),
            (4, "observed", "tests", "2"),
        ]
    )
    evidence.record(store, second)
    joined = evidence.run_query(store, "join", {"key": "v"})
    assert joined["columns"] == [
        "v",
        "hit_a",
        "hit_b",
        "a:bin",
        "b:tests",
        "no_value_a",
        "no_value_b",
        "caveats_a",
        "caveats_b",
        "matched",
    ]
    shown = [
        (row["v"], row["hit_a"], row["hit_b"], row["a:bin"], row["b:tests"], row["matched"])
        for row in joined["rows"]
    ]
    # A value at several hits pairs each of them; then each run's unpaired hits, a hit
    # whose key has no value among them (no_value_a says why).
    assert shown == [
        ("7", 1, 2, "3", "4", "both"),
        ("7", 1, 3, "3", "5", "both"),
        ("2", 2, 1, "1", "1", "both"),
        ("5", 3, None, "2", None, "only in run a"),
        (None, 4, None, "4", None, "only in run a"),
        ("8", None, 4, None, "2", "only in run b"),
    ]
    assert json.loads(joined["rows"][4]["no_value_a"]) == {"v": "gone"}
    assert evidence.query(store, joined["sql"])["rows"] == joined["rows"]
    with pytest.raises(ValueError, match="join needs --param key=<value>"):
        evidence.run_query(store, "join", {"a": 1, "b": 2})
    with pytest.raises(ValueError, match="run 2 captured no 'bin'"):
        evidence.run_query(store, "join", {"a": 1, "b": 2, "key": "bin"})
    with pytest.raises(ValueError, match="comparing needs two saved runs"):
        evidence.run_query(store, "join", {"a": 1, "b": 7, "key": "v"})


def test_wide_and_join_name_every_qualified_cell_and_never_merge_columns(store):
    # Review of #139 (PR #145, round 1): a C/C++ row's note (a `do` body's first line, a
    # program using setjmp) was not stored, so wide showed `k=1` plain; a stale value
    # that also had a view (`Some(40)`, active-variant) was stored with the view alone;
    # `x` and `X` were one column to SQLite (join's `a:X:1` held x's values); no_value
    # joined reasons with `; `, which reasons contain.
    note = "may be the last pass's value, or leftovers on the first pass; read it on the next"
    recorded = captured(
        [
            (1, "observed", "k", "1"),
            (1, "observed", "x", "2"),
            (1, "observed", "X", "101"),
            (1, "observed", "h.o", "Some(40)"),
            (1, "observed", "s", "[1, 2]"),
            (1, "observed", "gone; here", None),
        ]
    )
    rows = recorded["rows"]
    rows[0]["note"] = note
    rows[3].update(view="active-variant", stale=True, note="This is (or is inside) …")
    rows[4]["view"] = "memory"
    rows[5].update(error="no value yet: a; b", unavailable=True)
    evidence.record(store, recorded)
    stored = evidence.run_query(store, "run")["rows"]
    assert [(row["selector"], row["view"], row["note"]) for row in stored] == [
        ("k", None, note),
        ("x", None, None),
        ("X", None, None),
        ("h.o", "stale", "This is (or is inside) …"),
        ("s", "memory", None),
        ("gone; here", None, None),
    ]
    wide = evidence.run_query(store, "wide")
    assert wide["columns"] == [
        "hit", "phase", "line", "k", "x", "X (2)", "h.o", "s", "gone; here", "no_value", "caveats",
        "sets",
    ]  # fmt: skip
    (row,) = wide["rows"]
    assert (row["x"], row["X (2)"]) == ("2", "101")
    assert json.loads(row["caveats"]) == {"k": "note", "h.o": "stale"}
    assert json.loads(row["no_value"]) == {"gone; here": "no value yet: a; b"}
    # Refined with peeky sql, each name is its own column.
    refined = evidence.query(store, f'SELECT "x", "X (2)" FROM ({wide["sql"]})')["rows"]
    assert refined == [{"x": "2", "X (2)": "101"}]

    other = captured(
        [(1, "observed", "k", "1"), (1, "observed", "x", "5"), (1, "observed", "X", "104")]
    )
    evidence.record(store, other)
    joined = evidence.run_query(store, "join", {"key": "k"})
    assert joined["columns"][:9] == [
        "k", "hit_a", "hit_b", "a:x", "a:X (2)", "a:h.o", "a:s", "a:gone; here", "b:x"
    ]  # fmt: skip
    assert "b:X (2)" in joined["columns"]
    # The key's own caveat too: a value Peeky cannot vouch for pairs nothing (review of
    # PR #209), so the hit is unmatched, and caveats_a says why.
    shown = [(row["k"], row["hit_a"], row["hit_b"], row["matched"]) for row in joined["rows"]]
    assert shown == [(None, 1, None, "only in run a"), ("1", None, 1, "only in run b")]
    row, other_row = joined["rows"]
    assert (row["a:x"], row["a:X (2)"], other_row["b:x"], other_row["b:X (2)"]) == (
        "2",
        "101",
        "5",
        "104",
    )
    assert json.loads(row["caveats_a"]) == {"k": "note", "h.o": "stale"}
    assert other_row["caveats_b"] is None and "notes" not in joined


def test_join_says_when_its_key_carries_addresses_and_can_mask_them(store):
    # Review of #139 (low): `const char *name` is `0x… "apple"`, whose address differs
    # between runs, so nothing paired and nothing said why.
    first = captured(
        [
            (1, "observed", "name", '0x0000000100003f90 "apple"'),
            (1, "observed", "n", "1"),
            (2, "observed", "name", '0x0000000100003fa0 "pear"'),
            (2, "observed", "n", "2"),
        ]
    )
    second = captured(
        [
            (1, "observed", "name", '0x000000016fdff6a0 "pear"'),
            (1, "observed", "n", "3"),
            (2, "observed", "name", "0x0000000000000000"),
            (2, "observed", "n", "4"),
        ]
    )
    evidence.record(store, first, language="c")
    evidence.record(store, second, language="c")
    exact = evidence.run_query(store, "join", {"key": "name"})
    assert [row["matched"] for row in exact["rows"]] == ["only in run a"] * 2 + [
        "only in run b"
    ] * 2
    (note,) = exact["notes"]
    assert "name's values with addresses masked" in note and "--param masked=1" in note
    assert "they carry addresses" in note
    assert "note: add --param masked=1 to match name's values" in evidence.table(exact)
    masked = evidence.run_query(store, "join", {"key": "name", "masked": 1})
    assert "notes" not in masked
    shown = [
        (row["name"], row["a:name"], row["b:name"], row["a:n"], row["b:n"], row["matched"])
        for row in masked["rows"]
    ]
    # NULL is not an address: it is never masked, so it pairs only with NULL.
    assert shown == [
        ('0x… "pear"', '0x0000000100003fa0 "pear"', '0x000000016fdff6a0 "pear"', "2", "3", "both"),
        ('0x… "apple"', '0x0000000100003f90 "apple"', None, "1", None, "only in run a"),
        ("0x0000000000000000", None, "0x0000000000000000", None, "4", "only in run b"),
    ]
    assert evidence.query(store, masked["sql"])["rows"] == masked["rows"]
    with pytest.raises(ValueError, match="masked is 1"):
        evidence.run_query(store, "join", {"key": "name", "masked": "yes"})


def test_a_schema_7_store_gains_notes(store):
    # Review of #139: rows' notes are kept from schema 8; earlier runs have none.
    evidence.record(store, captured([(1, "observed", "k", "1")]))
    connection = sqlite3.connect(store)
    if "notes_kept" in {row[1] for row in connection.execute("PRAGMA table_info(runs)")}:
        connection.execute("ALTER TABLE runs DROP COLUMN notes_kept")
    connection.executescript(
        """
        DROP VIEW last_run; DROP VIEW evidence;
        ALTER TABLE captures DROP COLUMN note;
        CREATE VIEW evidence AS SELECT c.run_id, r.started_at, r.label, r.program,
            c.line, r.status, c.hit, c.phase, c.function, c.selector, c.type, c.value, c.num,
            c.unavailable, c.error, c.view, c.identity, c.scope, r.args, r.sets,
            r.artifact_manifest_id, r.source_sha256, r.edit IS NOT NULL AS candidate,
            r.language FROM captures c JOIN runs r ON r.run_id = c.run_id;
        CREATE VIEW last_run AS SELECT * FROM evidence
            WHERE run_id = (SELECT MAX(run_id) FROM runs);
        PRAGMA user_version = 7;
        """
    )
    connection.close()
    assert evidence.query(store, "SELECT selector, note FROM last_run")["rows"] == [
        {"selector": "k", "note": None}
    ]
    recorded = captured([(1, "observed", "k", "2")])
    recorded["rows"][0]["note"] = "may be the last pass's value"
    evidence.record(store, recorded)
    assert evidence.query(store, "SELECT selector, note FROM last_run")["rows"] == [
        {"selector": "k", "note": "may be the last pass's value"}
    ]
    # Review 2 of #139: run 1's values may be unmarked, and each query showing them says
    # so (its probe's notes were not kept); run 2's were.
    for name, params in (("run", {"run": 1}), ("wide", {"run": 1}), ("hits", {"run": 1})):
        (note,) = evidence.run_query(store, name, params)["notes"]
        assert note.startswith("run 1 was saved before Peeky kept notes"), (name, note)
    for name in ("diff", "compare"):
        assert evidence.run_query(store, name)["notes"][0].startswith("run 1 was saved")
    joined = evidence.run_query(store, "join", {"key": "k"})
    assert joined["notes"][0].startswith("run 1 was saved before Peeky kept notes")
    assert "notes" not in evidence.run_query(store, "wide", {"run": 2})
    assert "note: run 1 was saved" in evidence.table(evidence.run_query(store, "run", {"run": 1}))
    values = evidence.run_query(store, "values", {"selector": "k"})
    assert values["notes"][0].startswith("run 1 was saved")
    connection = sqlite3.connect(store)
    assert connection.execute("PRAGMA user_version").fetchone() == (evidence.SCHEMA_VERSION,)
    connection.close()


def test_qualified_values_are_never_counted_as_divergences_changes_or_sightings(store):
    # Review 2 of #139 (PR #145): on a C `do` body's first line, two runs' leftovers in
    # `k` and `v` came first in diff (`k 1927662298 vs 547973539`, note NULL) although
    # the computation and output were the same; hits computed `changed` from them and
    # values counted them.
    leftover = "may be the last pass's value, or leftovers on the first pass"
    runs = (("1927662298", "1005", "7"), ("547973539", "4005", "8"))
    for k, v, r in runs:
        recorded = captured(
            [
                (1, "observed", "k", k),
                (1, "observed", "v", v),
                (2, "observed", "k", "2"),
                (2, "observed", "v", "3"),
                (2, "observed", "r", r),
                (3, "observed", "v", "3"),
                (3, "observed", "h.n", "9"),
            ]
        )
        for row in recorded["rows"][:2]:
            row["note"] = leftover
        recorded["rows"][6]["stale"] = True
        recorded["rows"][6]["value"] = "9" if r == "7" else "12"
        evidence.record(store, recorded, language="c")
    diff = evidence.run_query(store, "diff")["rows"]
    shown = [(row["hit"], row["selector"], row["note"]) for row in diff]
    qualified = (
        "qualified (run a: note, run b: note): not evidence by itself; queries run shows why"
    )
    # The real divergence first; then values a note or a view qualifies.
    assert shown == [
        (2, "r", None),
        (1, "k", qualified),
        (1, "v", qualified),
        (3, "h.n", qualified.replace("a: note, run b: note", "a: stale, run b: stale")),
    ], shown
    compare = evidence.run_query(store, "compare")["rows"]
    assert {row["selector"]: row["note"] for row in compare if row["hit"] == 2} == {
        "k": None,
        "v": None,
        "r": None,
    }
    hits = evidence.run_query(store, "hits", {"run": 1, "selector": "v"})["rows"]
    assert [(row["value"], row["caveat"], row["changed"]) for row in hits] == [
        ("1005", "note", None),
        ("3", None, None),
        ("3", None, 0),
    ]
    values = evidence.run_query(store, "values", {"selector": "v"})["rows"]
    assert [(row["value"], row["seen"], row["qualified"]) for row in values] == [
        ("3", 4, 0),
        ("1005", 1, 1),
        ("4005", 1, 1),
    ]


def test_masking_leaves_quoted_text_alone(store):
    # Review 2 of #139: masked=1 paired `"0x00000001deadbeef"` with `"0x00000002cafebabe"`
    # (a char[32], no address), and the address note fired for it.
    first = captured(
        [
            (1, "observed", "buf", '"0x00000001deadbeef"'),
            (1, "observed", "name", '0x000000016fdff6a0 "0x00000001deadbeef"'),
        ]
    )
    second = captured(
        [
            (1, "observed", "buf", '"0x00000002cafebabe"'),
            (1, "observed", "name", '0x000000016fdff6b0 "0x00000002cafebabe"'),
        ]
    )
    evidence.record(store, first, language="c")
    evidence.record(store, second, language="c")
    exact = evidence.run_query(store, "join", {"key": "buf"})
    assert "notes" not in exact
    masked = evidence.run_query(store, "join", {"key": "buf", "masked": 1})
    assert [row["matched"] for row in masked["rows"]] == ["only in run a", "only in run b"]
    masked = evidence.run_query(store, "join", {"key": "name", "masked": 1})
    assert [row["matched"] for row in masked["rows"]] == ["only in run a", "only in run b"]
    # diff: a different text is a real difference, not an address-only one.
    diff = {row["selector"]: row["note"] for row in evidence.run_query(store, "diff")["rows"]}
    assert diff == {"buf": None, "name": None}
    assert evidence._no_addresses('0x000000016fdff6a0 "0x00000001deadbeef"') == (
        '0x… "0x00000001deadbeef"'
    )
    assert evidence._no_addresses("'0x00000001deadbeef'") == "'0x00000001deadbeef'"
    assert evidence._no_addresses("<Node object at 0x104a5b2c0>") == "<Node object at 0x…>"
    assert evidence._no_addresses('"x at 0x00000001deadbeef"', "java") == (
        '"x at 0x00000001deadbeef"'
    )


def test_a_qualified_value_says_so_whatever_else_its_diff_row_says(store):
    # Review 3 of #139 (PR #145): one note per row, and `missing in run b`, `intervention
    # only in run a` and `type …` won over `qualified`, so a noted leftover read plain
    # (and came first).
    def run(hits, extra=False, typed="int", sets=False):
        rows = []
        for hit in range(1, hits + 1):
            k = str(hit * 7 + (typed == "long"))
            rows += [(hit, "observed", "n", str(hit)), (hit, "observed", "k", k)]
        if extra:
            rows.append((1, "observed", "extra", "5"))
        if sets:
            rows += [(2, "after-set", "n", "1"), (2, "after-set", "k", "0")]
        recorded = captured(rows, set=[["n", "1"]] if sets else [])
        for row in recorded["rows"]:
            row["type"] = typed if row["selector"] == "k" else "int"
            if row["selector"] in ("k", "extra"):
                row["note"] = "may be the last pass's value, or leftovers on the first pass"
        return recorded

    evidence.record(store, run(3, extra=True))
    evidence.record(store, run(2, typed="long"))
    qualified = "qualified (run a: note): not evidence by itself; queries run shows why"
    diff = evidence.run_query(store, "diff")["rows"]
    shown = [(row["hit"], row["selector"], row["note"]) for row in diff]
    assert shown == [
        (3, "n", "missing in run b"),
        (1, "k", "type int -> long; " + qualified.replace("a: note", "a: note, run b: note")),
        (2, "k", "type int -> long; " + qualified.replace("a: note", "a: note, run b: note")),
        (3, "k", "missing in run b; " + qualified),
        (1, "extra", "only in run a (one row per selector: its first hit); " + qualified),
    ], shown
    compare = evidence.run_query(store, "compare")["rows"]
    assert {(row["hit"], row["selector"]): row["note"] for row in compare}[3, "k"] == (
        "missing in run b; " + qualified
    )

    evidence.record(store, run(2, sets=True))
    evidence.record(store, run(2))
    diff = evidence.run_query(store, "diff")["rows"]
    shown = [(row["hit"], row["phase"], row["selector"], row["note"]) for row in diff]
    assert shown == [
        (
            None,
            None,
            "(sets)",
            "the runs requested different interventions (--set, --set-at), or made different ones",
        ),
        (2, "after-set", "n", "intervention only in run a"),
        (2, "after-set", "k", "intervention only in run a; " + qualified),
    ], shown


def test_interventions_at_their_own_stops_are_stored_shown_and_placed(store):
    # #137: --set-at changes at other lines and hits. Captured at line 8 (3 hits); a
    # change at line 8's hit 2 (captured: after-set rows), one at line 9's hit 2 (after
    # hit 2, before hit 3), and one after the last captured hit.
    rows = [(hit, "observed", "w", str(hit)) for hit in (1, 2, 3)]
    rows.insert(2, (2, "after-set", "w", "50"))
    recorded = captured(rows)
    recorded["probe"]["set_at"] = [[8, 2, "w", "50"], [9, 2, "w", "2"], [9, 4, "w", "0"]]
    recorded["interventions"] = [
        {"name": "w", "before": "2", "after": "50", "line": 8, "hit": 2, "capture_hit": 2},
        {"name": "w", "before": "50", "after": "2", "line": 9, "hit": 2, "capture_hit": 2},
        {"name": "w", "before": "3", "after": "0", "line": 9, "hit": 4, "capture_hit": 3},
    ]
    recorded["set_pauses_ms"] = [7.5]
    evidence.record(store, recorded)
    run = evidence.query(store, "SELECT sets, set_hit, max_pause_ms FROM runs")["rows"][0]
    assert json.loads(run["sets"]) == [["w", "50", "8@2"], ["w", "2", "9@2"], ["w", "0", "9@4"]]
    assert run["set_hit"] is None and run["max_pause_ms"] == 7.5
    effect = evidence.run_query(store, "set_effect")["rows"]
    assert [
        (row["hit"], row["line"], row["selector"], row["observed"], row["after_set"])
        for row in effect
    ] == [(2, 8, "w", "2", "50"), (2, 9, "w", "50", "2"), (4, 9, "w", "3", "0")]
    assert [row["interventions"] for row in effect] == ["w: 2 -> 50", "w: 50 -> 2", "w: 3 -> 0"]
    wide = evidence.run_query(store, "wide")
    sets = [(row["hit"], row["phase"], row["sets"]) for row in wide["rows"]]
    # Each change on the row it comes before: at a captured stop, its after-set row;
    # else the next hit's observed row; none after the last.
    assert sets == [
        (1, "observed", None),
        (2, "observed", None),
        (2, "after-set", '["w: 2 -> 50 (line 8, hit 2)"]'),
        (3, "observed", '["w: 50 -> 2 (line 9, hit 2)"]'),
    ]
    assert evidence.query(store, wide["sql"])["rows"] == wide["rows"]


def test_a_schema_9_store_gains_intervention_lines(store):
    # Every --set before #137 was made at a captured stop of the run's line.
    evidence.record(store, result(sets=[("start", "0")]))
    connection = sqlite3.connect(store)
    connection.executescript(
        """
        ALTER TABLE interventions DROP COLUMN line;
        ALTER TABLE interventions DROP COLUMN capture_hit;
        PRAGMA user_version = 9;
        """
    )
    connection.close()
    rows = evidence.query(store, "SELECT hit, line, capture_hit FROM interventions")["rows"]
    assert rows == [{"hit": 1, "line": 8, "capture_hit": 1}]
    assert evidence.run_query(store, "set_effect")["rows"][0]["interventions"] == "start: 3 -> 0"


def test_a_schema_10_store_gains_intervention_functions(store):
    # #151 review: a --set-at line's stops can be in several functions; each change keeps
    # the one whose variable it changed.
    recorded = result(sets=[("start", "0")])
    recorded["interventions"][0]["function"] = "main::$_0::operator()(long) const"
    evidence.record(store, recorded)
    rows = evidence.query(store, "SELECT function FROM interventions")["rows"]
    assert rows == [{"function": "main::$_0::operator()(long) const"}]
    assert evidence.run_query(store, "wide")["rows"][1]["sets"] == (
        '["start: 3 -> 0 (line 8, hit 1, in main::$_0::operator())"]'
    )
    connection = sqlite3.connect(store)
    connection.executescript(
        "ALTER TABLE interventions DROP COLUMN function; PRAGMA user_version = 10;"
    )
    connection.close()
    rows = evidence.query(store, "SELECT function FROM interventions")["rows"]
    assert rows == [{"function": None}]


def test_a_schema_11_store_gains_intervention_instances(store):
    # #151 review 3: `queries wide` said `in gen::tf` for both instances.
    recorded = result(sets=[("start", "0")])
    recorded["interventions"][0].update(
        function="gen::tf::h0000000000000002", instance="gen::tf<gen::B>"
    )
    evidence.record(store, recorded)
    assert evidence.run_query(store, "wide")["rows"][1]["sets"] == (
        '["start: 3 -> 0 (line 8, hit 1, in gen::tf<gen::B>)"]'
    )
    connection = sqlite3.connect(store)
    connection.executescript(
        "ALTER TABLE interventions DROP COLUMN instance; PRAGMA user_version = 11;"
    )
    connection.close()
    rows = evidence.query(store, "SELECT function, instance FROM interventions")["rows"]
    assert rows == [{"function": "gen::tf::h0000000000000002", "instance": None}]
