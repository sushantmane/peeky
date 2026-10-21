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

SCHEMA_VERSION = 13
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
            "sets": "JSON list of requested interventions: [name, value] for --set (at "
            "set_hit of line), [name, value, 'LINE@HIT'] for --set-at (at hit HIT of LINE, "
            "counting that line's own stops, in the original's numbering)",
            "set_hit": "Hit at which the --set interventions were applied; NULL without --set",
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
            "max_pause_ms": "Longest pause at one stop (a hit, or a --set-at's), in milliseconds",
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
            "notes_kept": "1 where the run's notes are in captures.note (schema 9 on); NULL "
            "for a run saved before: a value its probe marked with a note shows unmarked",
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
            "function": "Function (frame) name, as the debugger names it (C++: its "
            "signature; Rust: with its symbol hash, and every closure of a function as "
            "`{{closure}}`). short_function(function) drops parameters and hashes",
            "selector": "Selector as requested, e.g. request.page or items[0]",
            "type": "Type as the debugger reports it",
            "value": "Value as the debugger displays it; NULL when there is none",
            "num": "value as a number when it is one, else NULL (use for >, <, AVG): an exact "
            "INTEGER for an integer that fits 64 bits, else REAL. Integer division truncates "
            "(write num * 1.0 / k), and SUM fails past 64 bits (TOTAL(num) does not)",
            "unavailable": "1 when the debugger has no value here (optimized out, not live)",
            "error": "Why there is no value (unknown name, unreadable, ...), else NULL",
            "view": "Qualified value view (e.g. a Rust Option); 'stale' (whatever its view) "
            "for a value in a Rust local that may be moved or dropped (the bytes it left, "
            "#108); else NULL",
            "identity": "Python: which object the value is, an INTEGER (debugpy's number for "
            "the object, not an address; the object keeps it if its class is reassigned). "
            "Within one hit, equal means the same object (an alias), different means "
            "different objects. Across hits of one run, different still means different "
            "objects, but equal means the same object or a new one that took a freed "
            "object's place. Never compare runs. NULL for values without one (numbers, "
            "strings, None) and in other languages (Java: object_ref(value))",
            "scope": "'globals' when a Python selector's first name was read from the "
            "module's globals (it was not a local there); 'unknown' for a row stored before "
            "Peeky recorded this (an early version of schema 6, which could read module "
            "globals); else NULL: the stopped frame's own variables",
            "note": "What Peeky says about the value, which it cannot vouch for or shows "
            "raw (a C/C++ local a loop returns to, a program using setjmp; a Rust value "
            "shown as LLDB's summary, a pointer it does not follow, a moved local): read "
            "the value with it. NULL for a plain value (and for runs saved before schema 8)",
            "instance": "That function as the build names it where the frame's name does not "
            "tell it apart (Rust: a closure, `cz::main::{closure#0}`; a generic's instance "
            "with its type arguments, `cz::gen_apply<i32>`, cut at 300 characters with `…`), "
            "as a probe table's function column shows it; else NULL (and for runs saved before schema 13). short_function(function) "
            "gives every closure of a function, and every instance of a generic, one name: "
            "instance tells them apart",
        },
    ),
    "interventions": (
        "One row per applied --set or --set-at, in the order they were applied.",
        {
            "run_id": "-> runs.run_id",
            "hit": "Hit of its line at which it was applied (that line's own stops)",
            "name": "Local that was changed",
            "before": "Value before",
            "after": "Value after, as the debugger reports it",
            "line": "Line where it was applied (where the program stopped: a candidate "
            "run's own numbering, as captures.line); NULL where an older run did not say",
            "capture_hit": "How many hits of the capture line had been captured when it was "
            "applied (its own hit, at a captured stop): it falls after those values and "
            "before the next hit's",
            "function": "Function (frame) whose variable it changed: a line's stops can be "
            "in several (a lambda or closure on it, a recursive call), and its hit counts "
            "them all; NULL where an older run did not say",
            "instance": "That function as the build names it where the frame's name does not "
            "tell it apart (Rust: a generic's instance with its type arguments, "
            "`gen::tf<gen::A>`, cut at 300 characters with `…`; a closure, "
            "`main::{closure#1}`), else NULL",
        },
    ),
    "pauses": (
        "How long the program was paused at each hit.",
        {"run_id": "-> runs.run_id", "hit": "Hit number", "ms": "Pause in milliseconds"},
    ),
}
_EVIDENCE_COLUMNS = (
    "run_id, started_at, label, program, line, status, hit, phase, function, instance, "
    "selector, type, "
    "value, num, unavailable, error, view, note, identity, scope, args, sets, "
    "artifact_manifest_id, source_sha256, candidate, language"
)
VIEWS = {
    "evidence": f"captures joined with their run, so no join is needed: {_EVIDENCE_COLUMNS} "
    "(candidate is 1 for a run that built an edit with probe --old/--new; language is the "
    "run's, for no_addresses(value, language)). The run's program output is runs.output, "
    "kept out of the view so SELECT * stays small: JOIN runs r USING (run_id) and name "
    "r.output",
    "last_run": "The evidence rows of the newest run",
}
_EVIDENCE_VIEW = """CREATE VIEW evidence AS SELECT c.run_id, r.started_at, r.label, r.program,
        c.line, r.status, c.hit, c.phase, c.function, c.instance, c.selector, c.type, c.value,
        c.num,
        c.unavailable, c.error, c.view, c.note, c.identity, c.scope, r.args, r.sets,
        r.artifact_manifest_id,
        r.source_sha256, r.edit IS NOT NULL AS candidate, r.language
        FROM captures c JOIN runs r ON r.run_id = c.run_id"""
_LAST_RUN_VIEW = (
    "CREATE VIEW last_run AS SELECT * FROM evidence WHERE run_id = (SELECT MAX(run_id) FROM runs)"
)
# NUMERIC keeps an integer exact (a u64 counter, a pointer-sized length); REAL would
# turn 600 into 600.0 and lose integers above 2**53.
_CAPTURES = """CREATE TABLE {} (
        run_id INTEGER NOT NULL, hit INTEGER, phase TEXT, line INTEGER, function TEXT,
        selector TEXT, type TEXT, value TEXT, num NUMERIC, unavailable INTEGER NOT NULL,
        error TEXT, view TEXT, identity INTEGER, scope TEXT, note TEXT, instance TEXT)"""
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
        source_sha256 TEXT, target_identity TEXT, edit TEXT, notes_kept INTEGER)""",
    _CAPTURES.format("captures"),
    "CREATE TABLE interventions (run_id INTEGER NOT NULL, hit INTEGER, name TEXT, "
    "before TEXT, after TEXT, line INTEGER, capture_hit INTEGER, function TEXT, instance TEXT)",
    "CREATE TABLE pauses (run_id INTEGER NOT NULL, hit INTEGER, ms REAL)",
    *_CAPTURE_INDEXES,
    "CREATE INDEX interventions_run ON interventions (run_id)",
    "CREATE INDEX pauses_run ON pauses (run_id)",
    _EVIDENCE_VIEW,
    _LAST_RUN_VIEW,
)

# Older schemas upgraded in place: {version: statements to the next version}; a callable
# takes the connection. A writer upgrades before saving; a reader upgrades a writable store
# before opening it read-only. Version 2 added runs.edit; version 3 rebuilt the views with
# `candidate` (an early version 2 had the column but the old views); version 4 made
# captures.num NUMERIC; version 5 added language to the views; version 6 added
# captures.identity and captures.scope; version 7 makes identity an INTEGER where an
# early version 6 stored it as TEXT (`list@7`) without scope; version 8 adds captures.note
# (earlier runs' notes were not kept: NULL); version 9 adds runs.notes_kept, 1 for a run
# saved with its notes, so queries can say which runs' values may be unmarked; version
# 10 adds interventions.line and capture_hit (an earlier run's --set was at its captured
# stop); version 11 adds interventions.function, version 12 interventions.instance, and
# version 13 captures.instance (a closure's or generic instance's label, NULL before). Views
# name columns of the newest tables, so earlier steps only drop them and the last creates
# them.
_DROP_VIEWS = ("DROP VIEW IF EXISTS last_run", "DROP VIEW IF EXISTS evidence")


def _add_columns(*added, table="captures"):
    def add(connection):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for column, kind in added:
            if column not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    return add


_add_identity = _add_columns(("identity", "INTEGER"), ("scope", "TEXT"))


def _identity_number(value):
    """An identity as an early version 6 stored it (`list@7`), as its number (7)."""
    if type(value) is int:
        return value
    found = re.search(r"@([0-9]{1,18})$", value) if isinstance(value, str) else None
    return int(found[1]) if found else None


def _integer_identity(connection):
    types = {
        row[1]: str(row[2]).upper() for row in connection.execute("PRAGMA table_info(captures)")
    }
    if types.get("identity", "INTEGER") == "INTEGER":
        _add_identity(connection)
        return
    # Rebuilt in rowid order (capture order), with the number alone as the identity. That
    # early version could read module globals without recording it: their scope is unknown.
    columns = (
        "run_id, hit, phase, line, function, selector, type, value, num, unavailable, error, view"
    )
    connection.execute(_CAPTURES.format("captures_v7"))
    connection.execute(
        f"INSERT INTO captures_v7 ({columns}, identity, scope) SELECT {columns}, "
        "peeky_identity(identity), 'unknown' FROM captures ORDER BY rowid"
    )
    connection.execute("DROP TABLE captures")
    connection.execute("ALTER TABLE captures_v7 RENAME TO captures")
    for statement in _CAPTURE_INDEXES:
        connection.execute(statement)


_MIGRATIONS = {
    1: ("ALTER TABLE runs ADD COLUMN edit TEXT",),
    2: _DROP_VIEWS,
    # Rebuilt in rowid order, which is capture order; `num` is recomputed from the
    # exact `value` text (a REAL above 2**53 had already lost digits).
    3: (
        *_DROP_VIEWS,
        _CAPTURES.format("captures_v4"),
        "INSERT INTO captures_v4 (run_id, hit, phase, line, function, selector, type, value, "
        "num, unavailable, error, view) SELECT run_id, hit, phase, line, function, selector, "
        "type, value, CASE WHEN num IS NULL THEN NULL ELSE peeky_number(value) END, "
        "unavailable, error, view FROM captures ORDER BY rowid",
        "DROP TABLE captures",
        "ALTER TABLE captures_v4 RENAME TO captures",
        *_CAPTURE_INDEXES,
    ),
    4: _DROP_VIEWS,
    5: (*_DROP_VIEWS, _add_identity),
    6: (*_DROP_VIEWS, _integer_identity),
    7: (*_DROP_VIEWS, _add_columns(("note", "TEXT"))),
    8: (*_DROP_VIEWS, _add_columns(("notes_kept", "INTEGER"), table="runs")),
    9: (
        *_DROP_VIEWS,
        _add_columns(("line", "INTEGER"), ("capture_hit", "INTEGER"), table="interventions"),
        # Every --set before version 10 was applied at a captured stop of the run's line.
        "UPDATE interventions SET capture_hit = hit, line = COALESCE((SELECT MIN(c.line) "
        "FROM captures c WHERE c.run_id = interventions.run_id AND c.hit = interventions.hit "
        "AND c.phase = 'after-set'), (SELECT r.line FROM runs r WHERE r.run_id = "
        "interventions.run_id)) WHERE capture_hit IS NULL",
    ),
    10: (*_DROP_VIEWS, _add_columns(("function", "TEXT"), table="interventions")),
    11: (*_DROP_VIEWS, _add_columns(("instance", "TEXT"), table="interventions")),
    12: (*_DROP_VIEWS, _add_columns(("instance", "TEXT")), _EVIDENCE_VIEW, _LAST_RUN_VIEW),
}

_LATEST = "(SELECT MAX(run_id) FROM runs)"
_RUN = f"COALESCE(:run, {_LATEST})"
_B = f"COALESCE(:b, {_LATEST})"
_A = f"COALESCE(:a, (SELECT MAX(run_id) FROM runs WHERE run_id < {_B}))"


# An intervention (`i`) made at a captured stop: that stop has after-set captures.
_AT_CAPTURE = (
    "EXISTS (SELECT 1 FROM captures c WHERE c.run_id = i.run_id AND c.phase = 'after-set' "
    "AND c.hit = i.hit AND c.line IS i.line)"
)


# Why a value is NULL; never shown in the value column, so no reason reads as a value.
_ERROR = "CASE WHEN value IS NULL THEN COALESCE(error, 'no value') END"


def _caveat(alias=""):
    """What qualifies a row's value, so it is not evidence by itself: its view where that
    is not just how it was read (`memory`, `active-variant`): stale, unproven, opaque,
    budget-exhausted or one added later; else `note` where the row has a note (a C/C++
    local a loop returns to, a Rust summary). NULL for a plain value, or none."""
    prefix = f"{alias}." if alias else ""
    return (
        f"CASE WHEN {prefix}value IS NULL THEN NULL WHEN {prefix}view NOT IN ('memory', "
        f"'active-variant') THEN {prefix}view WHEN {prefix}note IS NOT NULL THEN 'note' END"
    )


def _shown(alias=""):
    prefix = f"{alias}." if alias else ""
    return f"COALESCE({prefix}value, '<' || COALESCE({prefix}error, 'no value') || '>')"


# Selectors a run asked for (runs.capture), whether or not it reached a capture.
_ASKED = "(SELECT value FROM json_each((SELECT capture FROM runs WHERE run_id = {})))"
_LANGUAGE = "(SELECT language FROM runs WHERE run_id = {})"
# An after-set value equal to the same run's observed value at that hit (the
# intervention did not touch it): `same` is 1 (seventh evaluation, finding 10).
_SAME = (
    "s.phase = 'after-set' AND {shown} IS (SELECT {observed} FROM captures o WHERE "
    "o.run_id = s.run_id AND o.hit = s.hit AND o.selector = s.selector AND o.phase = "
    "'observed' ORDER BY o.rowid LIMIT 1)"
).format(shown=_shown("s"), observed=_shown("o"))
_PAIR = f"""WITH a AS (SELECT hit, phase, selector, {_shown("s")} AS v, type AS t, rowid AS pos,
    {_caveat("s")} AS c, {_SAME} AS same FROM captures s WHERE run_id = {_A}),
  b AS (SELECT hit, phase, selector, {_shown("s")} AS v, type AS t, rowid AS pos,
    {_caveat("s")} AS c, {_SAME} AS same FROM captures s WHERE run_id = {_B}),
  k AS (SELECT hit, phase, selector FROM a UNION SELECT hit, phase, selector FROM b),
  kinds AS (SELECT k.hit, k.phase, k.selector, a.v AS value_a, b.v AS value_b,
    a.v IS NOT b.v AS differs, COALESCE(a.pos, b.pos) AS pos,
    CASE
      WHEN k.selector NOT IN {_ASKED.format(_B)} THEN 'only in run a'
      WHEN k.selector NOT IN {_ASKED.format(_A)} THEN 'only in run b'
      WHEN k.phase = 'after-set' AND b.pos IS NULL THEN 'intervention only in run a'
      WHEN k.phase = 'after-set' AND a.pos IS NULL THEN 'intervention only in run b'
      WHEN b.pos IS NULL THEN 'missing in run b'
      WHEN a.pos IS NULL THEN 'missing in run a'
      WHEN a.v IS NOT b.v AND a.t IS NOT NULL AND b.t IS NOT NULL AND a.t IS NOT b.t
        THEN 'type ' || a.t || ' -> ' || b.t
      WHEN a.v IS NOT b.v AND no_addresses(a.v, {_LANGUAGE.format(_A)})
        IS no_addresses(b.v, {_LANGUAGE.format(_B)}) THEN 'addresses only'
    END AS kind,
    CASE WHEN a.c IS NOT NULL OR b.c IS NOT NULL THEN 'qualified (' || CASE
      WHEN a.c IS NOT NULL AND b.c IS NOT NULL THEN 'run a: ' || a.c || ', run b: ' || b.c
      WHEN a.c IS NOT NULL THEN 'run a: ' || a.c ELSE 'run b: ' || b.c END
      || '): not evidence by itself; queries run shows why' END AS qualified,
    k.phase = 'after-set' AND COALESCE(a.same, 1) AND COALESCE(b.same, 1) AS unchanged
    FROM k LEFT JOIN a USING (hit, phase, selector) LEFT JOIN b USING (hit, phase, selector)),
  -- Whatever else a row says, a qualified value says so too (review 3 of #139).
  pair AS (SELECT *, CASE WHEN qualified IS NULL THEN kind WHEN kind IS NULL THEN qualified
    ELSE kind || '; ' || qualified END AS note FROM kinds)"""
_PAIR_COLUMNS = (
    f"{_A} AS run_a, {_B} AS run_b, hit, phase, selector, value_a, value_b, differs, note"
)
# Capture order within a hit: the order the probe recorded (its --capture order).
_PAIR_ORDER = " ORDER BY hit, phase = 'after-set', pos"
# The runs' outcomes and outputs first, when they differ, each output shown from just
# before the first character that differs (an intervention's effect shows there), and
# what else told the runs apart: their edits, source, stop lines, arguments and
# interventions (sixth evaluation, finding 9: the decisive (output) row came after rows
# for selectors only one run captured). Then real divergences, in hit and capture order
# (a hit only one run reached is one); then values changed by an intervention only one
# run made; then values a view or a note qualifies in either run (a C local's leftovers
# on a `do` body's first line, a moved Rust local's bytes: review of #139), whatever else
# their note says (`missing in run b; qualified (run a: note)`); then values
# that differ only in memory addresses; last, one row per selector only one run asked
# for. Nothing is dropped: an address-only or qualified difference may still be real;
# but an after-set value equal to its run's observed value at that hit, in each run
# with one, is no difference of its own (the observed row has any: seventh evaluation,
# finding 10), and one selector's address-only rows are one row, its first hit's, that
# says how many hits there were (finding 9).
_DIFF = f"""{_PAIR}
SELECT {_PAIR_COLUMNS} FROM (
  SELECT 10 AS rank, hit, phase, selector, value_a, value_b, differs, note, pos FROM pair
    WHERE differs AND qualified IS NULL AND NOT unchanged
    AND (kind IS NULL OR kind LIKE 'missing%' OR kind LIKE 'type %')
  UNION ALL SELECT 11, hit, phase, selector, value_a, value_b, differs, note, pos FROM pair
    WHERE qualified IS NULL AND kind LIKE 'intervention only%' AND NOT unchanged
  UNION ALL SELECT 12, hit, phase, selector, value_a, value_b, differs, note, pos FROM pair
    WHERE differs AND qualified IS NOT NULL AND (kind IS NULL OR kind NOT LIKE 'only in%')
    AND NOT unchanged
  -- hit and the values are those of the MIN(pos) row (SQLite's bare columns).
  UNION ALL SELECT 13, hit, phase, selector, value_a, value_b, differs,
    CASE WHEN COUNT(*) > 1 THEN 'addresses only, at ' || COUNT(*) || ' hits (one row per '
      || 'selector: its first hit)' ELSE note END, MIN(pos) FROM pair
    WHERE qualified IS NULL AND kind = 'addresses only' AND NOT unchanged
    GROUP BY selector, phase
  -- hit, the values and qualified are those of the MIN(pos) row (SQLite's bare columns).
  UNION ALL SELECT 14, hit, phase, selector, value_a, value_b, 1,
    kind || ' (one row per selector: its first hit)' || COALESCE('; ' || qualified, ''),
    MIN(pos) FROM pair WHERE kind LIKE 'only in%' GROUP BY selector, kind
  UNION ALL SELECT 0, NULL, NULL, '(status)', ra.status, rb.status, 1, 'outcome differs', 0
    FROM runs ra, runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B}
    AND ra.status IS NOT rb.status
  UNION ALL SELECT 1, NULL, NULL, '(output)', output_window(ra.output, rb.output),
    output_window(rb.output, ra.output), 1,
    'output differs at ' || difference_position(ra.output, rb.output), 0
    FROM runs ra, runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B}
    AND ra.output IS NOT rb.output
  UNION ALL SELECT 2, NULL, NULL, '(edit)',
    CASE WHEN ra.edit IS NULL THEN 'none' ELSE 'candidate' END,
    CASE WHEN rb.edit IS NULL THEN 'none' ELSE 'candidate' END, 1,
    'the runs built different edits (runs.edit has each diff)', 0
    FROM runs ra, runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B}
    AND ra.edit IS NOT rb.edit
  UNION ALL SELECT 2, NULL, NULL, '(source)', ra.target || '/' || ra.program,
    rb.target || '/' || rb.program, 1,
    'the runs ran different source text, neither a candidate edit (source_sha256 differs)', 0
    FROM runs ra, runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B}
    AND ra.edit IS NULL AND rb.edit IS NULL AND ra.source_sha256 IS NOT NULL
    AND rb.source_sha256 IS NOT NULL AND ra.source_sha256 != rb.source_sha256
  UNION ALL SELECT 3, NULL, NULL, '(line)', ra.program || ':' || ra.line,
    rb.program || ':' || rb.line, 1,
    'the runs stopped at different lines: values also differ by where they were read', 0
    FROM runs ra, runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B}
    AND (ra.program, ra.line) IS NOT (rb.program, rb.line)
  UNION ALL SELECT 4, NULL, NULL, '(args)', ra.args, rb.args, 1,
    'the runs had different program arguments: values also differ by input', 0
    FROM runs ra, runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B}
    AND ra.args IS NOT rb.args
  UNION ALL SELECT 5, NULL, NULL, '(sets)', COALESCE(ra.sets, '[]'), COALESCE(rb.sets, '[]'), 1,
    'the runs made different interventions (--set, --set-at)', 0
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
# Quoted text in any language's value (a C string after its pointer, a char array, a
# Rust or Python string): the program's data, where hex is never an address. A quote
# left open (a value cut short) runs to the end.
_TEXT = re.compile(r"""("(?:[^"\\]|\\.)*(?:"|$)|'(?:[^'\\]|\\.)*(?:'|$))""")


def _no_addresses(value, language=None):
    """`value` with heap and stack addresses masked, for comparing runs.

    Registered as the SQL function no_addresses(text[, language]): an address that
    changes on every run (a C string, a Python object repr) is not a difference in the
    program. With language java or java-attach, so is a Java object's `@143`.
    """
    if not isinstance(value, str):
        return value
    if language in _JAVA_LANGUAGES:
        if value.startswith('"'):
            # A String value is java-debug's unescaped text: data, never an object
            # number or an address.
            return value
        value = "".join(
            part if part[:1] in ('"', "'") else _JAVA_ID.sub("@…", part)
            for part in _QUOTED.split(value)
        )
    # Only outside quoted text: `0x16fd… "0x00000001deadbeef"` masks the pointer alone.
    return "".join(
        part if part[:1] in ('"', "'") else _ADDRESS.sub("0x…", part) for part in _TEXT.split(value)
    )


# The identity at the start of a value java-debug wrote: `Merge$Interval@143`,
# `int[3]@144`, `Class (Foo)@145`, with or without Peeky's preview after it.
_OBJECT_REF = re.compile(r"(?:[A-Za-z_$][\w$.]*(?:\[[0-9]*\])*|Class \([^()]*\))@[0-9]+(?=\s|$)")


def _short_function(value):
    from peeky.names import short_function

    return short_function(value) if isinstance(value, str) else None


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
        "Every captured value of one run, in capture order; error says why a value is NULL; "
        "view and note qualify a value (read it with them)",
        f"SELECT hit, phase, line, function, selector, type, value, {_ERROR} AS error, view, "
        f"note FROM captures WHERE run_id = {_RUN} ORDER BY rowid",
        _RUN_PARAM,
        (),
    ),
    "hits": (
        "Each selector across hits, with changed = 1 where its value differs from the "
        "previous hit's; changed is NULL when either hit has no value (error says why) or "
        "a qualified one (caveat names its view, or `note`: queries run shows it), which "
        "is not evidence by itself",
        f"""SELECT selector, hit, line, value, {_ERROR} AS error, {_caveat()} AS caveat,
  CASE WHEN value IS NULL OR LAG(value) OVER w IS NULL OR {_caveat()} IS NOT NULL
    OR LAG({_caveat()}) OVER w IS NOT NULL THEN NULL
    ELSE value IS NOT LAG(value) OVER w END AS changed
FROM captures
WHERE run_id = {_RUN} AND phase = 'observed' AND (:selector IS NULL OR selector = :selector)
WINDOW w AS (PARTITION BY selector ORDER BY hit) ORDER BY selector, hit""",
        {**_RUN_PARAM, "selector": "Only this selector (default: all)"},
        (),
    ),
    "compare": (
        "Every value of two runs side by side, in hit and capture order; note says when a "
        "value is missing from a run, qualified in either (a view or a note: not evidence "
        "by itself) or differs only in memory addresses",
        f"{_PAIR}\nSELECT {_PAIR_COLUMNS} FROM pair{_PAIR_ORDER}",
        _PAIR_PARAMS,
        (),
    ),
    "diff": (
        "Only the values that differ between two runs. First (status), (output), (edit), "
        "(source), (line), (args) and (sets) rows when the runs' outcomes, outputs, edits, "
        "source, stop lines, program arguments or interventions differ; then real "
        "differences in hit and capture order (the first is the first divergence); then "
        "values only one run's intervention changed (an after-set value equal to its "
        "hit's observed one is left out); then values a view or a note qualifies in "
        "either run (note: qualified (run a: note), not evidence by itself: leftovers, a "
        "moved Rust local's bytes); then address-only differences, one row per selector; "
        "last, selectors only one run captured. Hits pair by number: where the runs "
        "stopped a different number of times, a note names `join`, which pairs by value",
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
        "Each selector before and after the run's interventions, in the order they were "
        "applied, with the changes made at that stop (line, and hit: that line's own); "
        "caveat_observed and caveat_after_set name a view or `note` that qualifies a value. "
        "A change at a stop with no capture (a --set-at line) is one row: selector is what "
        "it changed, observed and after_set its value before and after (the set's own "
        "read-back), function whose variable it was",
        f"""SELECT hit, line, function, selector, observed, after_set, caveat_observed,
  caveat_after_set, interventions FROM (
  SELECT o.hit, o.line, o.function, o.selector, {_shown("o")} AS observed,
    {_shown("s")} AS after_set,
    {_caveat("o")} AS caveat_observed, {_caveat("s")} AS caveat_after_set,
    (SELECT group_concat(name || ': ' || before || ' -> ' || after, '; ') FROM interventions i
      WHERE i.run_id = o.run_id AND i.hit = o.hit AND i.line IS o.line) AS interventions,
    (SELECT MIN(rowid) FROM interventions i
      WHERE i.run_id = o.run_id AND i.hit = o.hit AND i.line IS o.line) AS seq, o.rowid AS pos
  FROM captures o JOIN captures s ON s.run_id = o.run_id AND s.hit = o.hit
    AND s.selector = o.selector AND s.phase = 'after-set'
  WHERE o.run_id = {_RUN} AND o.phase = 'observed'
  UNION ALL
  SELECT i.hit, i.line, i.function, i.name, i.before, i.after, NULL, NULL,
    i.name || ': ' || i.before || ' -> ' || i.after, i.rowid, 0
  FROM interventions i WHERE i.run_id = {_RUN} AND NOT {_AT_CAPTURE})
ORDER BY seq, pos""",
        _RUN_PARAM,
        (),
    ),
    "values": (
        "Distinct observed values of one selector across all runs, most frequent first "
        "(candidate runs, from probe --old/--new, are left out); qualified counts the "
        "sightings a view or a note qualifies (leftovers, a moved Rust local's bytes: not "
        "evidence by themselves), and values are ordered by their other sightings",
        f"SELECT program, line, value, COUNT(*) AS seen, SUM(({_caveat()}) IS NOT NULL) AS "
        "qualified, COUNT(DISTINCT run_id) AS runs, MIN(run_id) AS first_run, MAX(run_id) "
        "AS last_run FROM evidence WHERE selector = :selector AND phase = 'observed' AND "
        "NOT candidate GROUP BY program, line, value ORDER BY seen - qualified DESC, seen "
        "DESC, value",
        {"selector": "Selector, e.g. request.page (required)"},
        ("selector",),
    ),
    "wide": (
        "One row per hit (and phase) of one run, a column per selector in capture order "
        "(a name SQLite would take for an earlier column, ignoring case, gets ` (2)`), each "
        "cell the value as stored. no_value is a JSON object naming the cells with no value "
        "(NULL: unavailable, withheld, an error) and why; a selector not captured at that "
        "hit is NULL and not named. caveats names the cells whose value is qualified: "
        "{selector: view} for stale, unproven, opaque or budget-exhausted, else "
        "{selector: 'note'} for a row with a note (queries run shows it): read those with "
        "it, never as plain values. --json gives the generated SQL too (`sql`), to refine "
        "with peeky sql",
        lambda path, params: _wide(path, params),
        {**_RUN_PARAM, "phase": "observed or after-set (default: both, after-set after)"},
        (),
    ),
    "join": (
        "Two runs joined by one selector's value instead of by hit: each pair of hits, one "
        "per run, where key has the same value (as stored text; a value at several hits "
        "pairs every hit of a with every hit of b), with each run's other selectors "
        "(columns a:<selector>, b:<selector>); then the hits of each run whose key value "
        "the other run never had, or that have none (matched: only in run a / b). "
        "no_value_a/b and caveats_a/b are wide's, per run (the key's own included). A key "
        "whose values carry addresses gets a note; masked=1 matches them masked. --json "
        "gives the generated SQL too (`sql`)",
        lambda path, params: _join(path, params),
        {
            **_PAIR_PARAMS,
            "key": "Selector whose value links the runs, e.g. v (required)",
            "phase": "observed (default) or after-set",
            "masked": "1: match key values with addresses masked (no_addresses), for a "
            "pointer or a C string; any address then matches any other",
        },
        ("key",),
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
    "peeky queries wide --param run=3",
    "peeky queries join --param a=3 --param b=4 --param key=v",
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


def _set_line(result, hit):
    """Where a --set from a worker before #137 was applied: its hit's after-set rows'."""
    return next(
        (
            row["line"]
            for row in result.get("rows") or []
            if row.get("phase") == "after-set"
            and row.get("hit") == hit
            and type(row.get("line")) is int
        ),
        None,
    )


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
    connection.create_function("peeky_identity", 1, _identity_number, deterministic=True)
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
                        if callable(statement):
                            statement(connection)
                        else:
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
        # --set as [name, value]; --set-at as [name, value, "LINE@HIT"] (#137).
        "sets": json.dumps(
            [list(change) for change in probe.get("set") or []]
            + [
                [name, value, f"{line}@{hit}"]
                for line, hit, name, value in probe.get("set_at") or []
            ]
        ),
        # Eval 5: a run without --set has no intervention hit (the probe's default is 1).
        "set_hit": probe.get("set_hit") if probe.get("set") else None,
        "hits_requested": probe.get("hits"),
        "args": _program_args(result, probe, language),
        "entry": probe.get("entry"),
        "worker": worker,
        "status": result.get("status"),
        "error_code": error.get("code"),
        "error_message": error.get("message"),
        "hits": result.get("hits"),
        "seconds": seconds,
        "max_pause_ms": max(stops)
        if (stops := [*pauses, *(result.get("set_pauses_ms") or [])])
        else None,
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
        "notes_kept": 1,
    }
    connection = _ensure(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        run_id = connection.execute(
            f"INSERT INTO runs ({', '.join(run)}) VALUES ({', '.join('?' * len(run))})",
            tuple(run.values()),
        ).lastrowid
        connection.executemany(
            "INSERT INTO captures (run_id, hit, phase, line, function, selector, type, value, "
            "num, unavailable, error, view, identity, scope, note, instance) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    # Stale wins: a moved local's `Some(40)` (active-variant) is the bytes
                    # it left, whatever view decoded them.
                    _text("stale" if row.get("stale") else row.get("view")),
                    row["identity"] if type(row.get("identity")) is int else None,
                    "globals" if row.get("scope") == "globals" else None,
                    _text(row.get("note")),
                    _text(row.get("instance")),
                )
                for row in result.get("rows") or []
            ],
        )
        connection.executemany(
            "INSERT INTO interventions (run_id, hit, name, before, after, line, capture_hit, "
            "function, instance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    # A worker before #137 says neither: its --set was at set_hit.
                    change.get("hit", probe.get("set_hit")),
                    change.get("name"),
                    _text(change.get("before")),
                    _text(change.get("after")),
                    change.get("line")
                    if type(change.get("line")) is int
                    else _set_line(result, change.get("hit", probe.get("set_hit"))),
                    change.get("capture_hit", change.get("hit", probe.get("set_hit"))),
                    _text(change.get("function")),
                    _text(change.get("instance")),
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


def baseline(path, run_id):
    """The newest run before ``run_id`` that is the same probe without its edit (the
    same target, file, line, captures, program arguments and Python entry; no edit, no
    `--set`), to compare a candidate run with (seventh evaluation, finding 12); else
    None."""
    found = query(
        path,
        "SELECT b.run_id FROM runs c JOIN runs b ON b.run_id < c.run_id AND b.target IS "
        "c.target AND b.language IS c.language AND b.program IS c.program AND b.line IS "
        "c.line AND b.capture IS c.capture AND b.args IS c.args AND b.entry IS c.entry AND "
        "b.edit IS NULL AND COALESCE(b.sets, '[]') = '[]' WHERE c.run_id = :run "
        "ORDER BY b.run_id DESC LIMIT 1",
        {"run": run_id},
    )["rows"]
    return found[0]["run_id"] if found else None


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
    connection.create_function("short_function", 1, bounded(_short_function), deterministic=True)
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
        elif "ambiguous column name" in message:
            message += (
                " (the evidence and last_run views already carry their run's columns: "
                f"{_EVIDENCE_COLUMNS}; drop the JOIN, or prefix the column with its table, "
                "e.g. r.output)"
            )
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


def _literal(text):
    """A string as an SQL literal (the generated SQL is shown, to run with peeky sql)."""
    return "'" + str(text).replace("'", "''") + "'"


def _identifier(name):
    return '"' + str(name).replace('"', '""') + '"'


_PHASES = ("observed", "after-set")
# Each run's cells: names whose value is NULL, with why, and names whose value something
# qualifies, with what: JSON objects, so no reason or selector text can be misread as a
# separator. NULL where there are none.
_NO_VALUE = "NULLIF(json_group_object(selector, reason) FILTER (WHERE reason IS NOT NULL), '{}')"
_CAVEATS = "NULLIF(json_group_object(selector, caveat) FILTER (WHERE caveat IS NOT NULL), '{}')"


def _cells(where):
    """SQL for one row per hit, phase and selector of the captures matching ``where``:
    ``value``; ``reason``, why it has none; ``caveat``, what qualifies a value (a view
    other than how it was read, else `note` where the row has a note). A selector
    captured twice at a hit read the same both times (``_captured`` checks): one cell."""
    return (
        "SELECT hit, phase, selector, MIN(line) AS line, MAX(value) AS value,\n"
        "    CASE WHEN MAX(value) IS NULL THEN COALESCE(MAX(error), 'no value') END AS reason,\n"
        f"    MAX({_caveat()}) AS caveat\n"
        f"  FROM captures WHERE {where} GROUP BY hit, phase, selector"
    )


def _phase(params, default=None):
    phase = params.get("phase", default)
    if phase is not None and phase not in _PHASES:
        raise ValueError(f"Evidence query failed: phase is observed or after-set, not {phase!r}")
    return phase


def _run_id(path, run):
    """The run ``run`` names (default: the newest), which must exist."""
    found = query(
        path,
        f"SELECT {_RUN} AS run_id, (SELECT COUNT(*) FROM runs WHERE run_id = {_RUN}) AS n",
        {"run": run},
    )["rows"][0]
    if found["run_id"] is None or not found["n"]:
        raise ValueError(
            f"Evidence query failed: no saved run {run if run is not None else ''}".rstrip()
            + "; `peeky queries runs` lists them, and --param run=<run> picks one"
        )
    return found["run_id"]


def _captured(path, run, phase):
    """The selectors run ``run`` captured (in ``phase``, or either), in capture order.
    A selector captured twice at one hit (`--capture v,v`) must have read the same both
    times, as a cell holds one value."""
    bound = {"run": run, "phase": phase}
    where = "run_id = :run AND (:phase IS NULL OR phase = :phase)"
    rows = query(
        path,
        f"SELECT selector FROM captures WHERE {where} GROUP BY selector ORDER BY MIN(rowid)",
        bound,
        limit=MAX_ROWS,
    )["rows"]
    differing = query(
        path,
        f"SELECT hit, phase, selector FROM captures WHERE {where} GROUP BY hit, phase, "
        "selector HAVING COUNT(DISTINCT value) > 1 OR MIN(value IS NULL) != MAX(value IS NULL) "
        "LIMIT 1",
        bound,
    )["rows"]
    if differing:
        first = differing[0]
        raise ValueError(
            f"Evidence query failed: run {run} captured {first['selector']!r} more than once "
            f"at hit {first['hit']} ({first['phase']}), with different values; use `peeky "
            "queries run`"
        )
    return [row["selector"] for row in rows]


def _columns(selectors, taken, prefix="", fixed=frozenset()):
    """A result column name for each selector: the selector itself (after ``prefix``);
    `<name> (selector)` where that is one of the query's ``fixed`` columns; and
    `<name> (2)`, `(3)`, ... where SQLite would take it for a column before it (its
    identifiers ignore ASCII case: `x` and `X` would be one column). ``taken`` holds the
    lowercased names given so far, and gains these."""
    names = []
    for selector in selectors:
        name = f"{prefix}{selector}"
        if name.lower() in fixed:
            name = f"{name} (selector)"
        column, number = name, 2
        while column.lower() in taken:
            column, number = f"{name} ({number})", number + 1
        taken.add(column.lower())
        names.append(column)
    return names


def _too_wide(count, run, name):
    if count > MAX_COLUMNS:
        raise ValueError(
            f"Evidence query failed: {name} would have {count} columns, more than "
            f"{MAX_COLUMNS} (run {run} captured too many selectors); use `peeky queries run`, "
            "or peeky sql with the selectors you need"
        )


def _picked(selectors, names, indent):
    return "".join(
        f"\n{indent}MAX(CASE WHEN selector = {_literal(selector)} THEN value END) AS "
        f"{_identifier(name)},"
        for selector, name in zip(selectors, names)
    )


def _wide(path, params):
    """(SQL, params, notes) of `queries wide`: one row per hit and phase of a run, a
    column per selector in capture order. Generated per run: SQL cannot pivot alone."""
    run, phase = _run_id(path, params.get("run")), _phase(params)
    selectors = _captured(path, run, phase)
    fixed = {"hit", "phase", "line", "no_value", "caveats", "sets"}
    names = _columns(selectors, set(fixed), fixed=fixed)
    _too_wide(len(names) + len(fixed), run, "wide")
    where = f"run_id = {run}" + (f" AND phase = {_literal(phase)}" if phase else "")
    # The interventions made since the previous row's values were read (#137): at this
    # captured stop for its after-set row; else, at other stops after the previous hit
    # and before this one, for its observed row.
    sets = (
        "(SELECT NULLIF(json_group_array(name || ': ' || COALESCE(before, '?') || ' -> ' || "
        "COALESCE(after, '?') || ' (line ' || COALESCE(line, '?') || ', hit ' || hit || "
        "COALESCE(', in ' || COALESCE(instance, short_function(function)), '') || ')'), "
        f"'[]') FROM (SELECT * FROM interventions i WHERE i.run_id = {run} AND CASE WHEN "
        f"{_AT_CAPTURE} THEN w.phase = 'after-set' AND i.hit = w.hit ELSE w.phase = "
        "'observed' AND i.capture_hit = w.hit - 1 END ORDER BY i.rowid))"
    )
    sql = (
        f"SELECT w.*, {sets} AS sets FROM (\n"
        f"SELECT hit, phase, MIN(line) AS line,{_picked(selectors, names, '  ')}\n"
        f"  {_NO_VALUE} AS no_value,\n  {_CAVEATS} AS caveats\n"
        f"FROM ({_cells(where)})\nGROUP BY hit, phase) w ORDER BY w.hit, w.phase = 'after-set'"
    )
    return sql, {}, []


def _join(path, params):
    """(SQL, params, notes) of `queries join`: two runs' hits paired where ``key`` has
    the same value (stored text; with ``masked``, addresses masked), every pair where it
    repeats; then each run's unpaired hits."""
    _require_runs(path, params)
    found = query(
        path,
        f"SELECT {_A} AS a, {_B} AS b, (SELECT language FROM runs WHERE run_id = {_A}) AS la, "
        f"(SELECT language FROM runs WHERE run_id = {_B}) AS lb",
        {"a": params.get("a"), "b": params.get("b")},
    )["rows"][0]
    a, b = found["a"], found["b"]
    key, phase = params["key"], _phase(params, "observed")
    if params.get("masked") not in (None, 0, 1):
        raise ValueError("Evidence query failed: masked is 1 (or 0)")
    masked = params.get("masked") == 1
    fixed = {"hit_a", "hit_b", "no_value_a", "no_value_b", "caveats_a", "caveats_b", "matched"}
    taken = set(fixed)
    (key_name,) = _columns([key], taken, fixed=fixed)
    sides = {}
    for name, run, language in (("a", a, found["la"]), ("b", b, found["lb"])):
        captured = _captured(path, run, phase)
        if key not in captured:
            raise ValueError(
                f"Evidence query failed: run {run} captured no {key!r} ({phase}); it "
                f"captured: {', '.join(captured[:20]) or 'nothing'}"
            )
        # Masked, each run's own key value is a column too: the masked text is the match.
        others = [selector for selector in captured if masked or selector != key]
        matched = "value" if not masked else f"no_addresses(value, {_literal(language)})"
        sides[name] = (run, others, _columns(others, taken, f"{name}:"), matched)
    _too_wide(len(fixed) + 1 + len(sides["a"][1]) + len(sides["b"][1]), f"{a} and {b}", "join")
    notes = []
    if not masked:
        carried = query(
            path,
            "SELECT c.value FROM captures c JOIN runs r USING (run_id) WHERE c.run_id IN "
            "(:a, :b) AND c.selector = :key AND c.phase = :phase AND c.value IS NOT NULL "
            "AND no_addresses(c.value, r.language) IS NOT c.value LIMIT 1",
            {"a": a, "b": b, "key": key, "phase": phase},
        )["rows"]
        if carried:
            notes.append(
                f"{key}'s values carry addresses ({carried[0]['value'][:60]}), which "
                "differ between runs even where what they point at is equal, so such "
                "hits rarely pair: --param masked=1 matches them with addresses masked "
                "(no_addresses: any address then matches any other)"
            )

    def side(name):
        run, others, names, matched = sides[name]
        where = f"run_id = {run} AND phase = {_literal(phase)}"
        return (
            f"{name} AS (SELECT hit, MAX(CASE WHEN selector = {_literal(key)} THEN {matched} "
            f"END) AS k,{_picked(others, names, '    ')}\n    {_NO_VALUE} AS no_value,\n"
            f"    {_CAVEATS} AS caveats\n  FROM ({_cells(where)}) GROUP BY hit)"
        )

    def columns(name, present):
        return [
            f"{name}.{_identifier(column)}" if present else f"NULL AS {_identifier(column)}"
            for column in sides[name][2]
        ]

    def row(present_a, present_b, matched):
        parts = [
            f"{'a' if present_a else 'b'}.k AS {_identifier(key_name)}",
            "a.hit AS hit_a" if present_a else "NULL AS hit_a",
            "b.hit AS hit_b" if present_b else "NULL AS hit_b",
            *columns("a", present_a),
            *columns("b", present_b),
            "a.no_value AS no_value_a" if present_a else "NULL AS no_value_a",
            "b.no_value AS no_value_b" if present_b else "NULL AS no_value_b",
            "a.caveats AS caveats_a" if present_a else "NULL AS caveats_a",
            "b.caveats AS caveats_b" if present_b else "NULL AS caveats_b",
            f"{_literal(matched)} AS matched",
        ]
        return "SELECT " + ", ".join(parts)

    sql = (
        f"WITH {side('a')},\n{side('b')}\nSELECT * FROM (\n"
        f"{row(True, True, 'both')} FROM a JOIN b ON a.k = b.k\nUNION ALL\n"
        f"{row(True, False, 'only in run a')} FROM a "
        "WHERE NOT EXISTS (SELECT 1 FROM b WHERE b.k = a.k)\nUNION ALL\n"
        f"{row(False, True, 'only in run b')} FROM b "
        "WHERE NOT EXISTS (SELECT 1 FROM a WHERE a.k = b.k))\n"
        "ORDER BY matched != 'both', hit_a IS NULL, hit_a, hit_b"
    )
    return sql, {}, notes


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
    bound = {**dict.fromkeys(accepted), **params}
    if callable(sql):
        # Built for the runs named (a column per selector): shown, to refine with peeky sql.
        built, extra, notes = sql(path, params)
        result = {**query(path, built, extra, limit=limit), "sql": built}
    else:
        result, notes = query(path, sql, bound, limit=limit), []
    notes = _unmarked(path, name, bound) + notes
    if name == "diff":
        notes += _unaligned(path, bound)
    return {**result, "notes": notes} if notes else result


def _unaligned(path, params):
    """For `diff`, which pairs hits by number: a note where the two runs stopped at
    their line a different number of times, so that their hits need not line up (a sort's
    comparator called on other pairs: seventh evaluation, finding 20), naming `join`,
    which pairs them by a value instead."""
    found = query(
        path,
        f"SELECT ra.run_id AS a, rb.run_id AS b, ra.hits AS ha, rb.hits AS hb FROM runs ra, "
        f"runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B} AND ra.program IS rb.program "
        "AND ra.line IS rb.line AND ra.hits_requested IS rb.hits_requested",
        {"a": params.get("a"), "b": params.get("b")},
    )["rows"]
    if not found or not found[0]["ha"] or not found[0]["hb"] or found[0]["ha"] == found[0]["hb"]:
        return []
    run = found[0]
    return [
        f"run {run['a']} stopped {run['ha']} times at its line and run {run['b']} "
        f"{run['hb']}, so a hit need not be the same moment in both (a sort's comparator "
        "is called on other pairs once one answer differs), and pairing by hit number can "
        "show every later hit as a difference: pair them by a value instead, `peeky queries "
        f"join --param a={run['a']} --param b={run['b']} --param key=<selector>`"
    ]


# The runs each canned query shows values of, for `_unmarked`.
_SHOWN_RUNS = {
    **dict.fromkeys(("run", "wide", "hits", "set_effect"), _RUN),
    **dict.fromkeys(("compare", "diff", "join"), f"{_A}, {_B}"),
    "values": "SELECT run_id FROM captures WHERE selector = :selector",
}


def _unmarked(path, name, params):
    """A note naming the runs this query shows values of that were saved before Peeky
    kept notes (runs.notes_kept NULL): a value their probe marked is unmarked here."""
    if name not in _SHOWN_RUNS:
        return []
    runs = [
        row["run_id"]
        for row in query(
            path,
            "SELECT run_id FROM runs WHERE notes_kept IS NULL AND run_id IN "
            f"({_SHOWN_RUNS[name]}) ORDER BY run_id",
            params,
            limit=MAX_ROWS,
        )["rows"]
    ]
    if not runs:
        return []
    named = ", ".join(map(str, runs[:10])) + (", ..." if len(runs) > 10 else "")
    return [
        f"run{'s' if len(runs) > 1 else ''} {named} {'were' if len(runs) > 1 else 'was'} "
        "saved before Peeky kept notes: a value its probe marked with a note (a C/C++ "
        "local's leftovers, a Rust value shown raw) may be unmarked here"
    ]


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
            "short_function(function)": "A frame name without a C++ signature's parameters, "
            "qualifiers and return type (`trim_left`) or a Rust symbol hash; NULL for NULL. "
            "Overloads share it (`operator+`, `G::get` for `get() &` and `get() &&`), and so "
            "do a function's closures (`cz::main::{{closure}}`) and a generic's instances "
            "(`cz::gen_apply` for `gen_apply<i32>` and `gen_apply<i16>`), so GROUP BY or a "
            "join on it combines them; captures.instance and interventions.instance tell "
            "closures and instances apart (`instance = 'cz::main::{closure#0}'`). A table "
            "shows it, except that overloads in one table keep their parameters there, and "
            "a probe table shows a short instance instead",
            "object_ref(text)": "The Java object a value is (`Merge$Interval@143`), else NULL: "
            "two values of one run with the same object_ref are the same object (aliasing); "
            "the numbers differ between runs. Python values carry theirs in "
            "captures.identity (an integer), not in their text: within one hit equal means "
            "the same object; across hits equal may also be a new object in a freed one's "
            "place, while different always means different objects",
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
    """A plain-text table of a query result. A `function` column shows each frame's
    short name (see ``names.function_labels``), with a note: the result keeps it whole."""
    from peeky.names import function_labels, shortened_note

    def text(value):
        value = "NULL" if value is None else str(value).replace("\n", "\\n")
        return value if len(value) <= width else value[: width - 3] + "..."

    columns = result["columns"]
    shortened = {}
    if "function" in columns:
        labels = function_labels(row["function"] for row in result["rows"])
        shortened = {name: label for name, label in labels.items() if label != name}
    cells = [
        [
            text(shortened.get(row[column], row[column]) if column == "function" else row[column])
            for column in columns
        ]
        for row in result["rows"]
    ]
    widths = [max(len(item[i]) for item in [columns, *cells]) for i in range(len(columns))]
    lines = [
        "  ".join(c.ljust(w) for c, w in zip(item, widths)).rstrip() for item in [columns, *cells]
    ]
    count = result["row_count"]
    if note := shortened_note(shortened):
        lines.append(note)
    lines.extend(f"note: {note}" for note in result.get("notes", ()))
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
