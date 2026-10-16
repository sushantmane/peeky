"""Captured-evidence store: capture once, query with SQL (ADR 0004, step 4).

`peeky probe` records every run in a local SQLite database (standard library, no
server). Questions about what was already captured are ordinary SQL over that
database, as heappy queries an exported heap. A new question about the running
program is a new probe. Queries are read-only, one statement at a time, and bounded
in rows, time and value size; SQL never reaches a debugger or a debuggee.
"""

import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

SCHEMA_VERSION = 1
MAX_RUNS = 500
DEFAULT_ROWS = 200
MAX_ROWS = 10000
QUERY_SECONDS = 10
MAX_VALUE_BYTES = 1_000_000
MAX_RESULT_BYTES = 16_000_000
MAX_COLUMNS = 100
_NUMBER = re.compile(r"[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?")
_PARAM = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,31}")
_INTEGER = re.compile(r"-?[0-9]{1,18}")

TABLES = {
    "runs": (
        "One row per probe run.",
        {
            "run_id": "Run number (the probe result's evidence.run_id); newest is MAX(run_id)",
            "started_at": "UTC start time, ISO 8601",
            "label": "The probe's --label, or NULL",
            "language": "python, rust, c, cpp, java or java-attach",
            "target": "Target root directory",
            "program": "Source file of the breakpoint, relative to target",
            "line": "Requested breakpoint line",
            "capture": "JSON list of requested selectors",
            "sets": "JSON list of requested [name, value] interventions",
            "set_hit": "Hit at which the interventions were applied",
            "hits_requested": "The probe's --hits",
            "args": "JSON list of program arguments (--arg)",
            "entry": "Python --run script, or NULL",
            "worker": "Remote worker URL, or NULL for a local run",
            "status": "completed, no-stop, failed or timeout",
            "error_code": "Error code when the probe did not complete (see ERRORS.md)",
            "error_message": "Error message, or NULL",
            "hits": "Hits that actually occurred",
            "seconds": "Wall time of the probe",
            "max_pause_ms": "Longest pause at one hit, in milliseconds",
            "optimized": "Optimization level of an optimized build, else NULL",
            "output": "Program output (bounded)",
            "output_truncated": "1 when the output was truncated",
            "output_complete": "0 when the probe ended before the program did",
            "warnings": "JSON list of the probe's warnings",
            "artifact_manifest_id": "Identity of the build the values came from",
            "source_sha256": "SHA-256 of the breakpoint's source file",
            "target_identity": "JSON target identity (toolchain, flags)",
        },
    ),
    "captures": (
        "One row per captured selector, per hit and phase.",
        {
            "run_id": "-> runs.run_id",
            "hit": "Hit number, from 1",
            "phase": "observed, or after-set (the value after that hit's interventions)",
            "line": "Line where the program actually stopped",
            "function": "Function (frame) name",
            "selector": "Selector as requested, e.g. request.page or items[0]",
            "type": "Type as the debugger reports it",
            "value": "Value as the debugger displays it; NULL when there is none",
            "num": "value as a number when it is one, else NULL (use for >, <, SUM, AVG)",
            "unavailable": "1 when the debugger has no value here (optimized out, not live)",
            "error": "Why there is no value (unknown name, unreadable, ...), else NULL",
            "view": "Qualified value view (e.g. a Rust Option), else NULL",
        },
    ),
    "interventions": (
        "One row per applied --set.",
        {
            "run_id": "-> runs.run_id",
            "hit": "Hit at which it was applied",
            "name": "Local that was changed",
            "before": "Value before",
            "after": "Value after, as the debugger reports it",
        },
    ),
    "pauses": (
        "How long the program was paused at each hit.",
        {"run_id": "-> runs.run_id", "hit": "Hit number", "ms": "Pause in milliseconds"},
    ),
}
_EVIDENCE_COLUMNS = (
    "run_id, started_at, label, program, line, status, hit, phase, function, selector, type, "
    "value, num, unavailable, error, view, args, sets, artifact_manifest_id, source_sha256"
)
VIEWS = {
    "evidence": f"captures joined with their run, so no join is needed: {_EVIDENCE_COLUMNS}",
    "last_run": "The evidence rows of the newest run",
}
_DDL = (
    """CREATE TABLE runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, label TEXT,
        language TEXT, target TEXT, program TEXT, line INTEGER, capture TEXT, sets TEXT,
        set_hit INTEGER, hits_requested INTEGER, args TEXT, entry TEXT, worker TEXT,
        status TEXT, error_code TEXT, error_message TEXT, hits INTEGER, seconds REAL,
        max_pause_ms REAL, optimized TEXT, output TEXT, output_truncated INTEGER,
        output_complete INTEGER, warnings TEXT, artifact_manifest_id TEXT,
        source_sha256 TEXT, target_identity TEXT)""",
    """CREATE TABLE captures (
        run_id INTEGER NOT NULL, hit INTEGER, phase TEXT, line INTEGER, function TEXT,
        selector TEXT, type TEXT, value TEXT, num REAL, unavailable INTEGER NOT NULL,
        error TEXT, view TEXT)""",
    "CREATE TABLE interventions (run_id INTEGER NOT NULL, hit INTEGER, name TEXT, "
    "before TEXT, after TEXT)",
    "CREATE TABLE pauses (run_id INTEGER NOT NULL, hit INTEGER, ms REAL)",
    "CREATE INDEX captures_run ON captures (run_id, hit)",
    "CREATE INDEX captures_selector ON captures (selector)",
    "CREATE INDEX interventions_run ON interventions (run_id)",
    "CREATE INDEX pauses_run ON pauses (run_id)",
    """CREATE VIEW evidence AS SELECT c.run_id, r.started_at, r.label, r.program, c.line,
        r.status, c.hit, c.phase, c.function, c.selector, c.type, c.value, c.num,
        c.unavailable, c.error, c.view, r.args, r.sets, r.artifact_manifest_id,
        r.source_sha256 FROM captures c JOIN runs r ON r.run_id = c.run_id""",
    "CREATE VIEW last_run AS SELECT * FROM evidence WHERE run_id = (SELECT MAX(run_id) FROM runs)",
)

_LATEST = "(SELECT MAX(run_id) FROM runs)"
_RUN = f"COALESCE(:run, {_LATEST})"
_B = f"COALESCE(:b, {_LATEST})"
_A = f"COALESCE(:a, (SELECT MAX(run_id) FROM runs WHERE run_id < {_B}))"


def _shown(alias=""):
    prefix = f"{alias}." if alias else ""
    return f"COALESCE({prefix}value, '<' || COALESCE({prefix}error, 'no value') || '>')"


_PAIR = f"""WITH a AS (SELECT hit, phase, selector, {_shown()} AS v FROM captures
    WHERE run_id = {_A}),
  b AS (SELECT hit, phase, selector, {_shown()} AS v FROM captures WHERE run_id = {_B}),
  k AS (SELECT hit, phase, selector FROM a UNION SELECT hit, phase, selector FROM b)
SELECT {_A} AS run_a, {_B} AS run_b, k.hit, k.phase, k.selector, a.v AS value_a,
  b.v AS value_b, a.v IS NOT b.v AS differs
FROM k LEFT JOIN a USING (hit, phase, selector) LEFT JOIN b USING (hit, phase, selector)"""
_PAIR_ORDER = " ORDER BY k.hit, k.phase = 'after-set', k.selector"
_RUN_PARAM = {"run": "Run id (default: the newest run)"}
_PAIR_PARAMS = {
    "a": "First run id (default: the run before b)",
    "b": "Second run id (default: the newest run)",
}

# name -> (description, SQL, {param: description}, required params)
QUERIES = {
    "runs": (
        "Recent runs, newest first: where, inputs, outcome",
        "SELECT run_id, started_at, label, program || ':' || line AS at, args, sets, status, "
        "hits, seconds, error_code FROM runs ORDER BY run_id DESC LIMIT COALESCE(:count, 20)",
        {"count": "How many runs (default 20)"},
        (),
    ),
    "run": (
        "Every captured value of one run, in capture order",
        f"SELECT hit, phase, line, function, selector, type, {_shown()} AS value, view "
        f"FROM captures WHERE run_id = {_RUN} ORDER BY rowid",
        _RUN_PARAM,
        (),
    ),
    "hits": (
        "Each selector across hits, with changed = 1 where it differs from the previous hit",
        f"""SELECT selector, hit, line, v AS value,
  CASE WHEN LAG(hit) OVER w IS NULL THEN NULL ELSE v IS NOT LAG(v) OVER w END AS changed
FROM (SELECT selector, hit, line, {_shown()} AS v FROM captures
  WHERE run_id = {_RUN} AND phase = 'observed' AND (:selector IS NULL OR selector = :selector))
WINDOW w AS (PARTITION BY selector ORDER BY hit) ORDER BY selector, hit""",
        {**_RUN_PARAM, "selector": "Only this selector (default: all)"},
        (),
    ),
    "compare": (
        "Two runs side by side (e.g. two inputs, or with and without --set)",
        _PAIR + _PAIR_ORDER,
        _PAIR_PARAMS,
        (),
    ),
    "diff": (
        "Only the values that differ between two runs; the first row is the first divergence",
        _PAIR + " WHERE a.v IS NOT b.v" + _PAIR_ORDER,
        _PAIR_PARAMS,
        (),
    ),
    "outputs": (
        "Inputs, interventions, outcome and program output of two runs",
        f"SELECT run_id, label, args, sets, status, error_code, output FROM runs "
        f"WHERE run_id IN ({_A}, {_B}) ORDER BY run_id",
        _PAIR_PARAMS,
        (),
    ),
    "set_effect": (
        "Each selector before and after the run's interventions, with the change applied",
        f"""SELECT o.hit, o.selector, {_shown("o")} AS observed, {_shown("s")} AS after_set,
  (SELECT group_concat(name || ': ' || before || ' -> ' || after, '; ') FROM interventions i
    WHERE i.run_id = o.run_id AND i.hit = o.hit) AS interventions
FROM captures o JOIN captures s ON s.run_id = o.run_id AND s.hit = o.hit
  AND s.selector = o.selector AND s.phase = 'after-set'
WHERE o.run_id = {_RUN} AND o.phase = 'observed' ORDER BY o.hit, o.rowid""",
        _RUN_PARAM,
        (),
    ),
    "values": (
        "Distinct observed values of one selector across all runs, most frequent first",
        "SELECT program, line, value, COUNT(*) AS seen, COUNT(DISTINCT run_id) AS runs, "
        "MIN(run_id) AS first_run, MAX(run_id) AS last_run FROM evidence "
        "WHERE selector = :selector AND phase = 'observed' "
        "GROUP BY program, line, value ORDER BY seen DESC, value",
        {"selector": "Selector, e.g. request.page (required)"},
        ("selector",),
    ),
    "unreadable": (
        "Selectors of one run that have no value, and why",
        f"SELECT hit, phase, selector, type, unavailable, error FROM captures "
        f"WHERE run_id = {_RUN} AND value IS NULL ORDER BY rowid",
        _RUN_PARAM,
        (),
    ),
    "failures": (
        "Runs that did not complete, newest first",
        "SELECT run_id, started_at, program || ':' || line AS at, status, error_code, "
        "error_message FROM runs WHERE status != 'completed' ORDER BY run_id DESC "
        "LIMIT COALESCE(:count, 20)",
        {"count": "How many runs (default 20)"},
        (),
    ),
}
EXAMPLES = (
    'peeky sql "SELECT hit, selector, value FROM last_run"',
    "peeky sql \"SELECT hit, value FROM evidence WHERE run_id = :run AND selector = 'start' "
    'AND num > 0" --param run=3',
    'peeky sql "SELECT selector, COUNT(DISTINCT value) AS distinct_values FROM last_run '
    'GROUP BY selector"',
    "peeky queries diff --param a=3 --param b=4",
)


def _number(value):
    if isinstance(value, str) and _NUMBER.fullmatch(value.strip()):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _text(value):
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def _connect(path, timeout=10):
    return sqlite3.connect(path, timeout=timeout, isolation_level=None)


def _check_version(connection, path, version):
    if version == SCHEMA_VERSION:
        return
    if version == 0 and not connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]:
        raise ValueError(
            f"Evidence store {path} is empty: no probe has been saved there yet. Run `peeky "
            "probe` first, or check --store"
        )
    if version == 0:
        raise ValueError(
            f"Evidence store {path} is a SQLite database that Peeky did not create; pass "
            "--store with another path"
        )
    raise ValueError(
        f"Evidence store {path} has schema version {version}; this Peeky reads version "
        f"{SCHEMA_VERSION}. Move it aside (a new one is created) or pass --store"
    )


def _ensure(path):
    """A writable connection to a store with the current schema, created if absent."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"Evidence store must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(descriptor)
    connection = _connect(path)
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            # Rechecked under the write lock: another process may have just created it.
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if (
                version == 0
                and not connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
            ):
                for statement in _DDL:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                _check_version(connection, path, version)
            connection.execute("COMMIT")
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise ValueError(
            f"Evidence store {path} is unusable ({exc}); move it aside (a new one is created) "
            "or pass --store"
        ) from exc
    except BaseException:
        # Closing rolls back an open transaction.
        connection.close()
        raise
    return connection


def record(path, result, *, language=None, target=None, label=None, worker=None):
    """Store one probe result; returns its run_id. Keeps the newest MAX_RUNS runs."""
    if label is not None and (not isinstance(label, str) or len(label) > 200):
        raise ValueError("--label is at most 200 characters")
    probe = result.get("probe") or {}
    error = result.get("error") or {}
    pauses = result.get("pauses_ms") or []
    seconds = result.get("seconds") or 0
    started = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    output, cut = result.get("output"), False
    if isinstance(output, str) and len(encoded := output.encode()) > MAX_VALUE_BYTES:
        # Stored values stay readable under the query connection's length limit.
        output, cut = encoded[:MAX_VALUE_BYTES].decode(errors="ignore"), True
    run = {
        "started_at": started.isoformat(timespec="seconds"),
        "label": label,
        "language": language,
        "target": None if target is None else str(target),
        "program": probe.get("program"),
        "line": probe.get("line"),
        "capture": json.dumps(list(probe.get("capture") or [])),
        "sets": json.dumps([list(change) for change in probe.get("set") or []]),
        "set_hit": probe.get("set_hit"),
        "hits_requested": probe.get("hits"),
        "args": json.dumps(list(probe.get("args") or [])),
        "entry": probe.get("entry"),
        "worker": worker,
        "status": result.get("status"),
        "error_code": error.get("code"),
        "error_message": error.get("message"),
        "hits": result.get("hits"),
        "seconds": seconds,
        "max_pause_ms": max(pauses) if pauses else None,
        "optimized": result.get("optimized"),
        "output": output,
        "output_truncated": result.get("output_truncated") or cut,
        "output_complete": result.get("output_complete"),
        "warnings": json.dumps(result.get("warnings") or []),
        "artifact_manifest_id": result.get("artifact_manifest_id"),
        "source_sha256": _text(result.get("source_sha256")),
        "target_identity": _text(result.get("target_identity")),
    }
    connection = _ensure(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        run_id = connection.execute(
            f"INSERT INTO runs ({', '.join(run)}) VALUES ({', '.join('?' * len(run))})",
            tuple(run.values()),
        ).lastrowid
        connection.executemany(
            "INSERT INTO captures VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    row.get("hit"),
                    row.get("phase"),
                    row.get("line"),
                    row.get("function"),
                    row.get("selector"),
                    row.get("type"),
                    _text(row.get("value")),
                    _number(row.get("value")),
                    1 if row.get("unavailable") else 0,
                    row.get("error"),
                    _text(row.get("view")),
                )
                for row in result.get("rows") or []
            ],
        )
        connection.executemany(
            "INSERT INTO interventions VALUES (?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    probe.get("set_hit"),
                    change.get("name"),
                    _text(change.get("before")),
                    _text(change.get("after")),
                )
                for change in result.get("interventions") or []
            ],
        )
        connection.executemany(
            "INSERT INTO pauses VALUES (?, ?, ?)",
            [(run_id, hit, ms) for hit, ms in enumerate(pauses, 1)],
        )
        oldest = run_id - MAX_RUNS
        for table in ("captures", "interventions", "pauses", "runs"):
            connection.execute(f"DELETE FROM {table} WHERE run_id <= ?", (oldest,))
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    return run_id


_ALLOWED = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}
# Schema introspection through table-valued pragma functions (SELECT ... FROM pragma_x).
_READ_PRAGMAS = {"table_info", "table_xinfo", "table_list", "index_list", "index_info"}


def _authorize(action, first, second, database, trigger):
    if action == sqlite3.SQLITE_FUNCTION and str(second).lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and str(first).lower() in _READ_PRAGMAS:
        # Schema introspection only (`pragma_table_info('runs')`); their argument is a
        # table or index name. Every other pragma is refused.
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def parse_params(items):
    """['run=3', 'selector=a.b'] -> {'run': 3, 'selector': 'a.b'}."""
    params = {}
    for item in items or ():
        name, separator, value = item.partition("=")
        name = name.strip()
        if not separator or not _PARAM.fullmatch(name):
            raise ValueError(f"--param expects name=value with a plain name, got {item!r}")
        params[name] = int(value) if _INTEGER.fullmatch(value.strip()) else value
    return params


def _cell(value):
    if isinstance(value, bytes):
        return "x'" + value.hex() + "'"
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _size(value):
    return len(value) if isinstance(value, (str, bytes)) else 8


def _open_read_only(path):
    """A read-only connection to an existing store; never creates or changes anything."""
    path = Path(path).absolute()
    if path.is_symlink():
        raise ValueError(f"Evidence store must not be a symlink: {path}")
    if not path.is_file():
        raise ValueError(
            f"Evidence store {path} does not exist: no probe has been saved there yet. Run "
            "`peeky probe` first, or check --store"
        )
    connection = None
    try:
        connection = sqlite3.connect(
            f"file:{quote(str(path), safe='/')}?mode=ro",
            uri=True,
            timeout=10,
            isolation_level=None,
        )
        _check_version(connection, path, connection.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise ValueError(
            f"Evidence store {path} cannot be read ({exc}); check that it is a readable Peeky "
            "store, or pass --store"
        ) from exc
    except BaseException:
        if connection is not None:
            connection.close()
        raise
    return connection


def query(path, sql, params=None, *, limit=DEFAULT_ROWS, seconds=QUERY_SECONDS):
    """Run one read-only SQL statement over the store; rows are capped at ``limit``."""
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("Evidence query failed: the SQL is empty")
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"Evidence query failed: --limit must be within 1..{MAX_ROWS}")
    connection = _open_read_only(path)
    # Each value, each row's width and the whole result are bounded.
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MAX_COLUMNS)
    connection.set_authorizer(_authorize)
    deadline = time.monotonic() + seconds
    connection.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
    rows, size, cut = [], 0, None
    try:
        cursor = connection.execute(sql, params or {})
        if cursor.description is None:
            raise ValueError(
                "Evidence query failed: only a SELECT (or WITH ... SELECT) returns rows"
            )
        columns, seen = [], {}
        for description in cursor.description:
            name = description[0]
            seen[name] = seen.get(name, 0) + 1
            columns.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
        for row in cursor:
            if len(rows) == limit:
                cut = "rows"
                break
            weight = sum(map(_size, row))
            if size + weight > MAX_RESULT_BYTES:
                if not rows:
                    raise ValueError(
                        f"Evidence query failed: one row exceeds {MAX_RESULT_BYTES} bytes; select "
                        "fewer or shorter columns (e.g. substr(output, 1, 2000))"
                    )
                cut = "size"
                break
            size += weight
            rows.append(row)
    except sqlite3.Error as exc:
        if time.monotonic() > deadline:
            raise ValueError(
                f"Evidence query failed: it ran longer than {seconds} s; narrow it with WHERE "
                "run_id = ... or LIMIT"
            ) from exc
        message = str(exc)
        if "not authorized" in message or "denied" in message:
            message += " (queries are read-only: one SELECT; no writes, ATTACH or settings)"
        raise ValueError(f"Evidence query failed: {message}") from exc
    finally:
        connection.close()
    shaped = {
        "columns": columns,
        "rows": [dict(zip(columns, map(_cell, row))) for row in rows],
        "row_count": len(rows),
        "truncated": cut is not None,
    }
    if cut:
        shaped["truncated_by"] = cut
    return shaped


def run_query(path, name, params=None, *, limit=DEFAULT_ROWS):
    """Run a canned query by name with optional parameters."""
    if name not in QUERIES:
        raise ValueError(
            f"Evidence query failed: no canned query {name!r}; one of: {', '.join(QUERIES)}"
        )
    _, sql, accepted, required = QUERIES[name]
    params = dict(params or {})
    if unknown := sorted(set(params) - set(accepted)):
        raise ValueError(
            f"Evidence query failed: {name} does not take {', '.join(unknown)}; it takes: "
            + (", ".join(accepted) or "no parameters")
        )
    if missing := [param for param in required if params.get(param) is None]:
        raise ValueError(
            f"Evidence query failed: {name} needs "
            + " ".join(f"--param {param}=<value>" for param in missing)
        )
    return query(path, sql, {**dict.fromkeys(accepted), **params}, limit=limit)


def schema(path):
    """The manifest an agent reads before writing SQL: tables, views, canned queries."""
    return {
        "store": str(path),
        "engine": f"SQLite {sqlite3.sqlite_version}",
        "tables": {
            name: {"description": description, "columns": columns}
            for name, (description, columns) in TABLES.items()
        },
        "views": VIEWS,
        "joins": "captures, interventions and pauses join runs on run_id; or use the "
        "evidence view, which already has the run's columns",
        "queries": {
            name: {"description": description, "params": params}
            for name, (description, _, params, _) in QUERIES.items()
        },
        "examples": list(EXAMPLES),
        "rules": [
            "Read-only: one SELECT or WITH ... SELECT per call; writes, PRAGMA statements "
            "and ATTACH are refused",
            f"At most --limit rows (default {DEFAULT_ROWS}, max {MAX_ROWS}); `truncated` "
            "says when more matched",
            f"{QUERY_SECONDS} s per query; values up to {MAX_VALUE_BYTES} bytes, at most "
            f"{MAX_COLUMNS} columns, results up to {MAX_RESULT_BYTES} bytes "
            "(`truncated_by` says which bound stopped it)",
            "Named parameters (:name) take --param name=value; integers are passed as numbers",
            f"The newest {MAX_RUNS} runs are kept",
            "This is evidence already captured; a new question about the program is a new probe",
        ],
    }


def table(result, width=120):
    """A plain-text table of a query result."""

    def text(value):
        value = "NULL" if value is None else str(value).replace("\n", "\\n")
        return value if len(value) <= width else value[: width - 3] + "..."

    columns = result["columns"]
    cells = [[text(row[column]) for column in columns] for row in result["rows"]]
    widths = [max(len(item[i]) for item in [columns, *cells]) for i in range(len(columns))]
    lines = [
        "  ".join(c.ljust(w) for c, w in zip(item, widths)).rstrip() for item in [columns, *cells]
    ]
    count = result["row_count"]
    lines.append(
        f"({count} row{'s' if count != 1 else ''}"
        + (
            f"; stopped at {MAX_RESULT_BYTES} bytes of results: select fewer or shorter columns)"
            if result.get("truncated_by") == "size"
            else "; more matched: add WHERE/LIMIT or raise --limit)"
            if result["truncated"]
            else ")"
        )
    )
    return "\n".join(lines)


def schema_text(manifest):
    lines = [f"store: {manifest['store']} ({manifest['engine']})", ""]
    for name, info in manifest["tables"].items():
        lines.append(f"table {name}: {info['description']}")
        lines.extend(f"  {column}: {meaning}" for column, meaning in info["columns"].items())
        lines.append("")
    lines.extend(f"view {name}: {meaning}" for name, meaning in manifest["views"].items())
    lines.append(f"joins: {manifest['joins']}")
    lines.append("")
    lines.append("canned queries (peeky queries <name> [--param k=v]):")
    for name, info in manifest["queries"].items():
        params = ", ".join(f"{p}: {d}" for p, d in info["params"].items())
        lines.append(f"  {name}: {info['description']}" + (f" [{params}]" if params else ""))
    lines.append("")
    lines.append("examples:")
    lines.extend(f"  {example}" for example in manifest["examples"])
    lines.append("rules:")
    lines.extend(f"  - {rule}" for rule in manifest["rules"])
    return "\n".join(lines)
