"""The captured-evidence store: record probe results, query them read-only (ADR 0004)."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

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
    assert changes == [{"run_id": 2, "hit": 1, "name": "start", "before": "3", "after": "0"}]
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
    assert "set_hit" in outputs[0]
    values = evidence.run_query(store, "values", {"selector": "start"})["rows"]
    assert values[0] == {
        "program": "a.py",
        "line": 8,
        "value": "3",
        "seen": 2,
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
            "selector": "start",
            "observed": "3",
            "after_set": "0",
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
        (1, "text", "addresses only"),
        (2, "text", "addresses only"),
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


def test_values_changed_by_an_intervention_rank_after_real_differences(store):
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
        (None, None, "the runs made different interventions (--set)"),
        (2, "observed", None),
        (1, "after-set", "intervention only in run b"),
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
        "views",
    ]
    shown = [
        (row["hit"], row["phase"], row["now"], row["w.start"], row["bins[3].hi"], row["no_value"])
        for row in wide["rows"]
    ]
    assert shown == [
        (1, "observed", "00", "01", "02", None),
        (2, "observed", "10", None, "12", "w.start: gone"),
        (2, "after-set", "7", None, None, None),
        (3, "observed", "100", "101", None, None),
    ]
    assert [row["views"] for row in wide["rows"]] == ["bins[3].hi: stale", None, None, None]
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
    with pytest.raises(ValueError, match="phase is observed or after-set"):
        evidence.run_query(store, "wide", {"phase": "later"})
    # A cell holds one value: a selector read twice at a hit must have read the same.
    evidence.record(store, captured([(1, "observed", "v", "1"), (1, "observed", "v", "1")]))
    assert [row["v"] for row in evidence.run_query(store, "wide")["rows"]] == ["1"]
    evidence.record(store, captured([(1, "observed", "v", "1"), (1, "observed", "v", "2")]))
    with pytest.raises(ValueError, match="'v' more than once at hit 1 .*different values"):
        evidence.run_query(store, "wide")
    # 95 selectors fit in 100 columns; more are refused, never cut short.
    many = [(1, "observed", f"s{i}", str(i)) for i in range(96)]
    evidence.record(store, captured(many[:95]))
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
        "views_a",
        "views_b",
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
    assert joined["rows"][4]["no_value_a"] == "v: gone"
    assert evidence.query(store, joined["sql"])["rows"] == joined["rows"]
    with pytest.raises(ValueError, match="join needs --param key=<value>"):
        evidence.run_query(store, "join", {"a": 1, "b": 2})
    with pytest.raises(ValueError, match="run 2 captured no 'bin'"):
        evidence.run_query(store, "join", {"a": 1, "b": 2, "key": "bin"})
    with pytest.raises(ValueError, match="comparing needs two saved runs"):
        evidence.run_query(store, "join", {"a": 1, "b": 7, "key": "v"})
