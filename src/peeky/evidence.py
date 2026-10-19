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

SCHEMA_VERSION = 4
MAX_RUNS = 500
DEFAULT_ROWS = 200
MAX_ROWS = 10000
QUERY_SECONDS = 10
MAX_VALUE_BYTES = 1_000_000
# Stored program output stays well below MAX_VALUE_BYTES, so sorting or de-duplicating
# rows that carry it never builds a record over the query length limit.
MAX_OUTPUT_BYTES = 256_000
MAX_RESULT_BYTES = 16_000_000
MAX_COLUMNS = 100
_NUMBER = re.compile(r"[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?")
_PARAM = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,31}")
_INTEGER = re.compile(r"-?[0-9]{1,18}")
_WHOLE = re.compile(r"[-+]?[0-9]{1,20}")

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
            "hits_requested": "The probe's --hits: a number, or 'all'",
            "args": "JSON list of the arguments the program received (--arg, else the "
            "configured ones); NULL for an attached JVM, whose arguments are unknown",
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
            "source_sha256": "SHA-256 of the breakpoint's source file as it ran: for a candidate "
            "run, the edited text",
            "target_identity": "JSON target identity (toolchain, flags)",
            "edit": "For a candidate run (probe --old/--new), the unified diff of the edit the "
            "build had; NULL otherwise. Values never come from your files when this is set",
        },
    ),
    "captures": (
        "One row per captured selector, per hit and phase.",
        {
            "run_id": "-> runs.run_id",
            "hit": "Hit number, from 1",
            "phase": "observed, or after-set (the value after that hit's interventions)",
            "line": "Line where the program actually stopped (a candidate run's own numbering; "
            "runs.line is the requested line in the original file)",
            "function": "Function (frame) name",
            "selector": "Selector as requested, e.g. request.page or items[0]",
            "type": "Type as the debugger reports it",
            "value": "Value as the debugger displays it; NULL when there is none",
            "num": "value as a number when it is one, else NULL (use for >, <, AVG): an exact "
            "INTEGER for an integer that fits 64 bits, else REAL. Integer division truncates "
            "(write num * 1.0 / k), and SUM fails past 64 bits (TOTAL(num) does not)",
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
    "value, num, unavailable, error, view, args, sets, artifact_manifest_id, source_sha256, "
    "candidate"
)
VIEWS = {
    "evidence": f"captures joined with their run, so no join is needed: {_EVIDENCE_COLUMNS} "
    "(candidate is 1 for a run that built an edit with probe --old/--new)",
    "last_run": "The evidence rows of the newest run",
}
_EVIDENCE_VIEW = """CREATE VIEW evidence AS SELECT c.run_id, r.started_at, r.label, r.program,
        c.line, r.status, c.hit, c.phase, c.function, c.selector, c.type, c.value, c.num,
        c.unavailable, c.error, c.view, r.args, r.sets, r.artifact_manifest_id,
        r.source_sha256, r.edit IS NOT NULL AS candidate
        FROM captures c JOIN runs r ON r.run_id = c.run_id"""
_LAST_RUN_VIEW = (
    "CREATE VIEW last_run AS SELECT * FROM evidence WHERE run_id = (SELECT MAX(run_id) FROM runs)"
)
# NUMERIC keeps an integer exact (a u64 counter, a pointer-sized length); REAL would
# turn 600 into 600.0 and lose integers above 2**53.
_CAPTURES = """CREATE TABLE {} (
        run_id INTEGER NOT NULL, hit INTEGER, phase TEXT, line INTEGER, function TEXT,
        selector TEXT, type TEXT, value TEXT, num NUMERIC, unavailable INTEGER NOT NULL,
        error TEXT, view TEXT)"""
_CAPTURE_INDEXES = (
    "CREATE INDEX captures_run ON captures (run_id, hit)",
    "CREATE INDEX captures_selector ON captures (selector)",
)
_DDL = (
    """CREATE TABLE runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, label TEXT,
        language TEXT, target TEXT, program TEXT, line INTEGER, capture TEXT, sets TEXT,
        set_hit INTEGER, hits_requested INTEGER, args TEXT, entry TEXT, worker TEXT,
        status TEXT, error_code TEXT, error_message TEXT, hits INTEGER, seconds REAL,
        max_pause_ms REAL, optimized TEXT, output TEXT, output_truncated INTEGER,
        output_complete INTEGER, warnings TEXT, artifact_manifest_id TEXT,
        source_sha256 TEXT, target_identity TEXT, edit TEXT)""",
    _CAPTURES.format("captures"),
    "CREATE TABLE interventions (run_id INTEGER NOT NULL, hit INTEGER, name TEXT, "
    "before TEXT, after TEXT)",
    "CREATE TABLE pauses (run_id INTEGER NOT NULL, hit INTEGER, ms REAL)",
    *_CAPTURE_INDEXES,
    "CREATE INDEX interventions_run ON interventions (run_id)",
    "CREATE INDEX pauses_run ON pauses (run_id)",
    _EVIDENCE_VIEW,
    _LAST_RUN_VIEW,
)

# Older schemas upgraded in place: {version: statements to the next version}. A writer
# upgrades before saving; a reader upgrades a writable store before opening it read-only.
# Version 2 added runs.edit; version 3 rebuilt the views with `candidate` (an early
# version 2 had the column but the old views); version 4 made captures.num NUMERIC.
_MIGRATIONS = {
    1: ("ALTER TABLE runs ADD COLUMN edit TEXT",),
    2: (
        "DROP VIEW IF EXISTS last_run",
        "DROP VIEW IF EXISTS evidence",
        _EVIDENCE_VIEW,
        _LAST_RUN_VIEW,
    ),
    # Rebuilt in rowid order, which is capture order; `num` is recomputed from the
    # exact `value` text (a REAL above 2**53 had already lost digits).
    3: (
        "DROP VIEW IF EXISTS last_run",
        "DROP VIEW IF EXISTS evidence",
        _CAPTURES.format("captures_v4"),
        "INSERT INTO captures_v4 (run_id, hit, phase, line, function, selector, type, value, "
        "num, unavailable, error, view) SELECT run_id, hit, phase, line, function, selector, "
        "type, value, CASE WHEN num IS NULL THEN NULL ELSE peeky_number(value) END, "
        "unavailable, error, view FROM captures ORDER BY rowid",
        "DROP TABLE captures",
        "ALTER TABLE captures_v4 RENAME TO captures",
        *_CAPTURE_INDEXES,
        _EVIDENCE_VIEW,
        _LAST_RUN_VIEW,
    ),
}

_LATEST = "(SELECT MAX(run_id) FROM runs)"
_RUN = f"COALESCE(:run, {_LATEST})"
_B = f"COALESCE(:b, {_LATEST})"
_A = f"COALESCE(:a, (SELECT MAX(run_id) FROM runs WHERE run_id < {_B}))"


# Why a value is NULL; never shown in the value column, so no reason reads as a value.
_ERROR = "CASE WHEN value IS NULL THEN COALESCE(error, 'no value') END"


def _shown(alias=""):
    prefix = f"{alias}." if alias else ""
    return f"COALESCE({prefix}value, '<' || COALESCE({prefix}error, 'no value') || '>')"


# Selectors a run asked for (runs.capture), whether or not it reached a capture.
_ASKED = "(SELECT value FROM json_each((SELECT capture FROM runs WHERE run_id = {})))"
_LANGUAGE = "(SELECT language FROM runs WHERE run_id = {})"
_PAIR = f"""WITH a AS (SELECT hit, phase, selector, {_shown()} AS v, rowid AS pos FROM captures
    WHERE run_id = {_A}),
  b AS (SELECT hit, phase, selector, {_shown()} AS v, rowid AS pos FROM captures
    WHERE run_id = {_B}),
  k AS (SELECT hit, phase, selector FROM a UNION SELECT hit, phase, selector FROM b),
  pair AS (SELECT k.hit, k.phase, k.selector, a.v AS value_a, b.v AS value_b,
    a.v IS NOT b.v AS differs, COALESCE(a.pos, b.pos) AS pos,
    CASE
      WHEN k.selector NOT IN {_ASKED.format(_B)} THEN 'only in run a'
      WHEN k.selector NOT IN {_ASKED.format(_A)} THEN 'only in run b'
      WHEN k.phase = 'after-set' AND b.pos IS NULL THEN 'intervention only in run a'
      WHEN k.phase = 'after-set' AND a.pos IS NULL THEN 'intervention only in run b'
      WHEN b.pos IS NULL THEN 'missing in run b'
      WHEN a.pos IS NULL THEN 'missing in run a'
      WHEN a.v IS NOT b.v AND no_addresses(a.v, {_LANGUAGE.format(_A)})
        IS no_addresses(b.v, {_LANGUAGE.format(_B)}) THEN 'addresses only'
    END AS note
    FROM k LEFT JOIN a USING (hit, phase, selector) LEFT JOIN b USING (hit, phase, selector))"""
_PAIR_COLUMNS = (
    f"{_A} AS run_a, {_B} AS run_b, hit, phase, selector, value_a, value_b, differs, note"
)
# Capture order within a hit: the order the probe recorded (its --capture order).
_PAIR_ORDER = " ORDER BY hit, phase = 'after-set', pos"
# Real divergences first, in hit and capture order (a hit only one run reached is
# one); then values changed by an intervention only one run made; then values that
# differ only in memory addresses; then one row per selector only one run asked for.
# Nothing is dropped: an address-only difference may still be a real one. Last, the
# runs' outcomes and outputs when they differ, each output shown from just before the
# first character that differs (an intervention's effect shows there).
_DIFF = f"""{_PAIR}
SELECT {_PAIR_COLUMNS} FROM (
  SELECT 0 AS rank, hit, phase, selector, value_a, value_b, differs, note, pos FROM pair
    WHERE differs AND (note IS NULL OR note LIKE 'missing%')
  UNION ALL SELECT 1, hit, phase, selector, value_a, value_b, differs, note, pos FROM pair
    WHERE note LIKE 'intervention only%'
  UNION ALL SELECT 2, hit, phase, selector, value_a, value_b, differs, note, pos FROM pair
    WHERE note = 'addresses only'
  UNION ALL SELECT 3, NULL, NULL, selector, NULL, NULL, 1, note, MIN(pos) FROM pair
    WHERE note LIKE 'only in%' GROUP BY selector, note
  UNION ALL SELECT 4, NULL, NULL, '(status)', ra.status, rb.status, 1, 'outcome differs', 0
    FROM runs ra, runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B}
    AND ra.status IS NOT rb.status
  UNION ALL SELECT 5, NULL, NULL, '(output)', output_window(ra.output, rb.output),
    output_window(rb.output, ra.output), 1,
    'output differs at ' || difference_position(ra.output, rb.output), 0
    FROM runs ra, runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B}
    AND ra.output IS NOT rb.output
  UNION ALL SELECT 6, NULL, NULL, '(edit)',
    CASE WHEN ra.edit IS NULL THEN 'none' ELSE 'candidate' END,
    CASE WHEN rb.edit IS NULL THEN 'none' ELSE 'candidate' END, 1,
    'the runs built different edits (runs.edit has each diff)', 0
    FROM runs ra, runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B}
    AND ra.edit IS NOT rb.edit
  UNION ALL SELECT 7, NULL, NULL, '(line)', ra.program || ':' || ra.line,
    rb.program || ':' || rb.line, 1,
    'the runs stopped at different lines: values also differ by where they were read', 0
    FROM runs ra, runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B}
    AND (ra.program, ra.line) IS NOT (rb.program, rb.line)
  UNION ALL SELECT 8, NULL, NULL, '(args)', ra.args, rb.args, 1,
    'the runs had different program arguments: values also differ by input', 0
    FROM runs ra, runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B}
    AND ra.args IS NOT rb.args
  UNION ALL SELECT 9, NULL, NULL, '(sets)', COALESCE(ra.sets, '[]'), COALESCE(rb.sets, '[]'), 1,
    'the runs made different interventions (--set)', 0
    FROM runs ra, runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B}
    AND COALESCE(ra.sets, '[]') IS NOT COALESCE(rb.sets, '[]'))
ORDER BY rank, hit, phase = 'after-set', pos"""
# A heap or stack address: 0x, optional leading zeros, then 9-16 significant hex
# digits that are not all f. NULL, small sentinels, all-ones (-1, MAP_FAILED) and
# 8-digit data (0xdeadbeef, flags) are values, never masked. One regex, no callback.
_ADDRESS = re.compile(r"\b0x(?=[0-9a-fA-F]{1,16}\b)0*(?![fF]+\b)[1-9a-fA-F][0-9a-fA-F]{8,15}\b")


def _first_difference(a, b):
    """0-based offset of the first character where two outputs differ."""
    a, b = a or "", b or ""
    for index, (left, right) in enumerate(zip(a, b)):
        if left != right:
            return index
    return min(len(a), len(b))


def _difference_position(a, b):
    """Where two texts first differ, as `line L, column C` counted from 1."""
    a = a or ""
    offset = _first_difference(a, b)
    line = a.count("\n", 0, offset) + 1
    column = offset - (a.rfind("\n", 0, offset) + 1) + 1
    return f"line {line}, column {column}"


def _output_window(value, other):
    """Up to 200 characters of ``value`` from shortly before where it first differs from
    ``other``, with `…` where text is cut."""
    if value is None:
        return None
    start = max(0, _first_difference(value, other) - 60)
    window = value[start : start + 200]
    return ("…" if start else "") + window + ("…" if start + 200 < len(value) else "")


# java-debug's object identity numbers (`Merge$Interval@143`, `int[3]@144`,
# `Class (Foo)@145`) follow the order the debugger first touched objects, so like
# addresses they differ between runs. Only in a Java run's values: other languages'
# text (a Python `Node@1` repr) is the program's own.
_JAVA_LANGUAGES = ("java", "java-attach")
# After a name, `]`, `)` or a preview's cut (`…`).
_JAVA_ID = re.compile(r"(?<=[\w$\])…])@[0-9]+\b")
# A preview's String and char literals (Peeky escapes both), never masked.
_QUOTED = re.compile(r"""("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\(?:u[0-9a-f]{4}|.))')""")


def _no_addresses(value, language=None):
    """`value` with heap and stack addresses masked, for comparing runs.

    Registered as the SQL function no_addresses(text[, language]): an address that
    changes on every run (a C string, a Python object repr) is not a difference in the
    program. With language java or java-attach, so is a Java object's `@143`.
    """
    if not isinstance(value, str):
        return value
    # A String value is java-debug's unescaped text: data, never an object number.
    if language in _JAVA_LANGUAGES and not value.startswith('"'):
        value = "".join(
            part if part[:1] in ('"', "'") else _JAVA_ID.sub("@…", part)
            for part in _QUOTED.split(value)
        )
    return _ADDRESS.sub("0x…", value)


# The identity at the start of a value java-debug wrote: `Merge$Interval@143`,
# `int[3]@144`, `Class (Foo)@145`, with or without Peeky's preview after it.
_OBJECT_REF = re.compile(r"(?:[A-Za-z_$][\w$.]*(?:\[[0-9]*\])*|Class \([^()]*\))@[0-9]+(?=\s|$)")


def _object_ref(value):
    """The object a Java value is (`Merge$Interval@143`), else NULL: equal within one run
    means the same object; across runs the numbers differ (see no_addresses)."""
    if not isinstance(value, str) or not (match := _OBJECT_REF.match(value)):
        return None
    return match[0]


_RUN_PARAM = {"run": "Run id (default: the newest run)"}
_PAIR_PARAMS = {
    "a": "First run id (default: the run before b)",
    "b": "Second run id (default: the newest run)",
}

# name -> (description, SQL, {param: description}, required params)
QUERIES = {
    "runs": (
        "Recent runs, newest first: where, inputs, outcome",
        "SELECT run_id, started_at, label, program || ':' || line || "
        "CASE WHEN edit IS NULL THEN '' ELSE ' (candidate)' END AS at, args, sets, status, "
        "hits, seconds, error_code, substr(output, 1, 60) AS output_start FROM runs "
        "ORDER BY run_id DESC LIMIT COALESCE(:count, 20)",
        {"count": "How many runs (default 20)"},
        (),
    ),
    "run": (
        "Every captured value of one run, in capture order; error says why a value is NULL",
        f"SELECT hit, phase, line, function, selector, type, value, {_ERROR} AS error, view "
        f"FROM captures WHERE run_id = {_RUN} ORDER BY rowid",
        _RUN_PARAM,
        (),
    ),
    "hits": (
        "Each selector across hits, with changed = 1 where its value differs from the "
        "previous hit's; changed is NULL when either hit has no value (error says why)",
        f"""SELECT selector, hit, line, value, {_ERROR} AS error,
  CASE WHEN value IS NULL OR LAG(value) OVER w IS NULL THEN NULL
    ELSE value IS NOT LAG(value) OVER w END AS changed
FROM captures
WHERE run_id = {_RUN} AND phase = 'observed' AND (:selector IS NULL OR selector = :selector)
WINDOW w AS (PARTITION BY selector ORDER BY hit) ORDER BY selector, hit""",
        {**_RUN_PARAM, "selector": "Only this selector (default: all)"},
        (),
    ),
    "compare": (
        "Every value of two runs side by side, in hit and capture order; note says when a "
        "value is missing from a run or differs only in memory addresses",
        f"{_PAIR}\nSELECT {_PAIR_COLUMNS} FROM pair{_PAIR_ORDER}",
        _PAIR_PARAMS,
        (),
    ),
    "diff": (
        "Only the values that differ between two runs. Real differences come first, in hit and "
        "capture order (the first row is the first divergence); then address-only "
        "differences; then selectors only one run captured; then (status), (output), "
        "(edit), (line), (args) and (sets) rows when the runs' outcomes, outputs, edits, "
        "stop lines, program arguments or interventions differ",
        _DIFF,
        _PAIR_PARAMS,
        (),
    ),
    "outputs": (
        "Inputs, interventions (and the hit they were applied at), outcome and program output "
        "of two runs",
        f"SELECT run_id, label, args, sets, CASE WHEN sets = '[]' OR sets IS NULL THEN NULL "
        f"ELSE set_hit END AS set_hit, status, error_code, output FROM runs "
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
        "Distinct observed values of one selector across all runs, most frequent first "
        "(candidate runs, from probe --old/--new, are left out)",
        "SELECT program, line, value, COUNT(*) AS seen, COUNT(DISTINCT run_id) AS runs, "
        "MIN(run_id) AS first_run, MAX(run_id) AS last_run FROM evidence "
        "WHERE selector = :selector AND phase = 'observed' AND NOT candidate "
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
    """A numeric value: an exact int when it is an integer that fits 64 bits."""
    if not isinstance(value, str) or not _NUMBER.fullmatch(text := value.strip()):
        return None
    if _WHOLE.fullmatch(text) and -(2**63) <= (whole := int(text)) < 2**63:
        return whole
    number = float(text)
    return number if math.isfinite(number) else None


def _text(value):
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def _program_args(result, probe, language):
    """The arguments the program received, as reported by the worker that launched it.

    A probe that failed before launch falls back to the requested arguments; an
    attached JVM's own arguments are unknown, so none are claimed.
    """
    actual = result.get("program_args")
    if isinstance(actual, list) and all(isinstance(arg, str) for arg in actual):
        return json.dumps(actual)
    if language == "java-attach":
        return None
    return json.dumps(list(probe.get("args") or []))


def _connect(path, timeout=10):
    return sqlite3.connect(path, timeout=timeout, isolation_level=None)


def _check_version(connection, path, version):
    if version == SCHEMA_VERSION or version in _MIGRATIONS:
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
    # Migrations recompute numbers exactly as a new run would.
    connection.create_function("peeky_number", 1, _number, deterministic=True)
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
            elif version in _MIGRATIONS:
                while version != SCHEMA_VERSION:
                    for statement in _MIGRATIONS[version]:
                        connection.execute(statement)
                    version += 1
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                _check_version(connection, path, version)
            connection.execute("COMMIT")
    except sqlite3.DatabaseError as exc:
        connection.close()
        if "readonly" in str(exc) and not os.access(path.parent, os.W_OK):
            raise ValueError(
                f"Evidence store {path} needs a one-time upgrade, and SQLite writes a journal "
                f"beside it, but {path.parent} is not writable; make it writable or pass --store"
            ) from exc
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
    candidate = result.get("candidate") or {}
    edited = bool(candidate)
    seconds = result.get("seconds") or 0
    started = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    output, cut = result.get("output"), False
    if isinstance(output, str) and len(encoded := output.encode()) > MAX_OUTPUT_BYTES:
        output, cut = encoded[:MAX_OUTPUT_BYTES].decode(errors="ignore"), True
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
        "args": _program_args(result, probe, language),
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
        # A candidate's values came from the edited text, never from the file on disk.
        "source_sha256": _text(candidate.get("sha256") if edited else result.get("source_sha256")),
        "target_identity": _text(result.get("target_identity")),
        "edit": candidate.get("diff") if edited else None,
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
    # As emitted: --json escapes non-ASCII text and blobs become hex.
    return len(json.dumps(_cell(value)))


def _read_only(path):
    return sqlite3.connect(
        f"file:{quote(str(path), safe='/')}?mode=ro", uri=True, timeout=10, isolation_level=None
    )


def _open_read_only(path):
    """A read-only connection to an existing store; never creates it or changes its data.

    A store with an older schema is upgraded first (as the next save would), so canned
    queries can rely on the current columns.
    """
    path = Path(path).absolute()
    try:
        if path.is_symlink():
            raise ValueError(f"Evidence store must not be a symlink: {path}")
        exists = path.is_file()
    except OSError as exc:
        raise ValueError(
            f"Evidence store {path} cannot be read ({exc}); check the directory's permissions, "
            "or pass --store"
        ) from exc
    if not exists:
        raise ValueError(
            f"Evidence store {path} does not exist: no probe has been saved there yet. Run "
            "`peeky probe` first, or check --store"
        )
    connection = None
    try:
        connection = _read_only(path)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error as exc:
            if getattr(exc, "sqlite_errorname", "") != "SQLITE_READONLY_ROLLBACK":
                raise
            # A save was interrupted (process killed) and left a hot journal. Restoring the
            # last committed state needs a writable connection; mode=rw never creates files.
            connection.close()
            connection = None
            if not os.access(path, os.W_OK):
                raise
            recovery = sqlite3.connect(
                f"file:{quote(str(path), safe='/')}?mode=rw", uri=True, timeout=10
            )
            try:
                recovery.execute("PRAGMA user_version").fetchone()
            finally:
                recovery.close()
            connection = _read_only(path)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        _check_version(connection, path, version)
        if version in _MIGRATIONS:
            connection.close()
            connection = None
            if not os.access(path, os.W_OK):
                raise ValueError(
                    f"Evidence store {path} has schema version {version}, which this Peeky "
                    "upgrades in place, but the file is read-only; make it writable or pass "
                    "--store"
                )
            _ensure(path).close()
            connection = _read_only(path)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            _check_version(connection, path, version)
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
    deadline = time.monotonic() + seconds

    def no_addresses(value, language=None):
        # SQLite checks the deadline only between VM steps, never inside a Python
        # function: a query calling it many times per row checks here too.
        if time.monotonic() > deadline:
            raise sqlite3.OperationalError("interrupted: query deadline")
        return _no_addresses(value, language)

    connection.create_function("no_addresses", 1, no_addresses, deterministic=True)
    connection.create_function("no_addresses", 2, no_addresses, deterministic=True)

    def bounded(function):
        def call(*values):
            if time.monotonic() > deadline:
                raise sqlite3.OperationalError("interrupted: query deadline")
            return function(*values)

        return call

    connection.create_function(
        "first_difference", 2, bounded(_first_difference), deterministic=True
    )
    connection.create_function("output_window", 2, bounded(_output_window), deterministic=True)
    connection.create_function("object_ref", 1, bounded(_object_ref), deterministic=True)
    connection.create_function(
        "difference_position", 2, bounded(_difference_position), deterministic=True
    )
    connection.set_authorizer(_authorize)
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


_PAIRS = ("compare", "diff", "outputs")


def _require_runs(path, params):
    """Two-run queries name two runs that exist, never a silent half comparison."""
    found = query(
        path,
        f"SELECT {_A} AS a, {_B} AS b, (SELECT COUNT(*) FROM runs WHERE run_id IN ({_A}, {_B})) "
        "AS n",
        {"a": params.get("a"), "b": params.get("b")},
    )["rows"][0]
    a, b, count = found["a"], found["b"], found["n"]
    # Comparing a run with itself is allowed; otherwise both runs must exist.
    if a is not None and b is not None and count == (1 if a == b else 2):
        return
    raise ValueError(
        f"Evidence query failed: comparing needs two saved runs (run a = {a}, run b = {b}); "
        "`peeky queries runs` lists them, and --param a=<run> --param b=<run> picks two"
    )


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
    if name in _PAIRS:
        _require_runs(path, params)
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
        "functions": {
            "no_addresses(text[, language])": "The text with heap and stack addresses (0x and "
            "at least 9 significant hex digits; never NULL, all-ones or shorter values) masked, "
            "to compare values across runs; with the run's language (java or java-attach), Java "
            "object numbers (`Type@143`) too",
            "first_difference(a, b)": "Offset, from 0, of the first character where two texts "
            "differ",
            "difference_position(a, b)": "Where two texts first differ, as `line L, column C` "
            "(from 1)",
            "output_window(a, b)": "Up to 200 characters of a from just before where it first "
            "differs from b",
            "object_ref(text)": "The Java object a value is (`Merge$Interval@143`), else NULL: "
            "two values of one run with the same object_ref are the same object (aliasing); "
            "the numbers differ between runs",
        },
        "examples": list(EXAMPLES),
        "rules": [
            "Read-only: one SELECT or WITH ... SELECT per call; writes, PRAGMA statements "
            "and ATTACH are refused",
            f"At most --limit rows (default {DEFAULT_ROWS}, max {MAX_ROWS}); `truncated` "
            "says when more matched",
            f"{QUERY_SECONDS} s per query; values up to {MAX_VALUE_BYTES} bytes, at most "
            f"{MAX_COLUMNS} columns, results up to {MAX_RESULT_BYTES} bytes of JSON "
            "(`truncated_by` says which bound stopped it)",
            f"Program output is stored up to {MAX_OUTPUT_BYTES} bytes (output_truncated = 1 "
            "when cut)",
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
    lines.append("functions:")
    lines.extend(f"  {name}: {meaning}" for name, meaning in manifest["functions"].items())
    lines.append("")
    lines.append("examples:")
    lines.extend(f"  {example}" for example in manifest["examples"])
    lines.append("rules:")
    lines.extend(f"  - {rule}" for rule in manifest["rules"])
    return "\n".join(lines)
