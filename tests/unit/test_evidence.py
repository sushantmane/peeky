"""The captured-evidence store: record probe results, query them read-only (ADR 0004)."""

import json
import os
import sqlite3
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
    for name in evidence.QUERIES:
        params = {"selector": "start"} if name == "values" else {}
        assert evidence.run_query(store, name, params)["row_count"] == 0
    evidence.record(store, result(starts=("3", "6"), output="PAGE: [40]\n"), label="one")
    evidence.record(store, result(starts=("3", "7"), output="PAGE: [10]\n"), label="two")
    diff = evidence.run_query(store, "diff")
    assert diff["rows"] == [
        {
            "run_a": 1,
            "run_b": 2,
            "hit": 2,
            "phase": "observed",
            "selector": "start",
            "value_a": "6",
            "value_b": "7",
            "differs": 1,
        }
    ]
    assert evidence.run_query(store, "compare", {"a": 2, "b": 2})["rows"][0]["differs"] == 0
    hits = evidence.run_query(store, "hits", {"run": 1, "selector": "start"})["rows"]
    assert [(row["value"], row["changed"]) for row in hits] == [("3", None), ("6", 1)]
    outputs = evidence.run_query(store, "outputs")["rows"]
    assert [row["output"] for row in outputs] == ["PAGE: [40]\n", "PAGE: [10]\n"]
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
