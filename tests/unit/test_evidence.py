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
        if name in ("compare", "diff", "outputs"):
            # Nothing to compare: said plainly, never an empty or one-sided comparison.
            with pytest.raises(ValueError, match="comparing needs two saved runs"):
                evidence.run_query(store, name, params)
            continue
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
            "note": None,
        },
        # The outputs differ too: the last row says so.
        {
            "run_a": 1,
            "run_b": 2,
            "hit": None,
            "phase": None,
            "selector": "(output)",
            "value_a": "PAGE: [40]\n",
            "value_b": "PAGE: [10]\n",
            "differs": 1,
            "note": "output differs",
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
        (None, "totals", "only in run a"),
        (None, "zeta", "only in run b"),
    ]
    assert all(r["differs"] == 1 for r in rows)
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
    assert "no_addresses(text)" in evidence.schema(store)["functions"]


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
        (1, "1", "missing in run b"),
        (2, "2", "missing in run b"),
        (None, "completed", "outcome differs"),
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
        (2, "observed", None),
        (1, "after-set", "intervention only in run b"),
    ]


def test_the_text_schema_lists_sql_functions(store):
    evidence._ensure(store).close()
    assert "no_addresses(text)" in evidence.schema_text(evidence.schema(store))


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


def test_diff_ends_with_the_outcome_and_a_trimmed_output_when_they_differ(store):
    first = captured([(1, "observed", "x", "1")])
    first["output"] = "SUM:14\n"
    evidence.record(store, first)
    second = captured([(1, "observed", "x", "1"), (1, "after-set", "x", "2")])
    second.update(output="x" * 300, status="failed")
    evidence.record(store, second)
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(r["selector"], r["note"]) for r in rows] == [
        ("x", "intervention only in run b"),
        ("(status)", "outcome differs"),
        ("(output)", "output differs"),
    ]
    assert rows[-1]["value_a"] == "SUM:14\n" and rows[-1]["value_b"] == "x" * 200 + "…"
    # Same outputs: no output row.
    evidence.record(store, first)
    evidence.record(store, first)
    assert evidence.run_query(store, "diff")["row_count"] == 0
