"""Captured-evidence store: capture once, query with SQL (ADR 0004, step 4).

`peeky probe` records every run not refused before launch in a local SQLite database (standard library, no
server). Questions about what was already captured are ordinary SQL over that
database, as heappy queries an exported heap. A new question about the running
program is a new probe. Queries are read-only, one statement at a time, and bounded
in rows, time and value size; SQL never reaches a debugger or a debuggee.
"""

import contextlib
import contextvars
import itertools
import json
import math
import os
import re
import shlex
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

SCHEMA_VERSION = 17
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
            "counting that line's own stops, in the original's numbering), or "
            "[name, value, 'LINE@all'] for one made at every stop of LINE",
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
            "output_complete": "1 only when the debugger reported the program's end and the "
            "adapter then ended its stream (all output read); else 0 (--until hits, a "
            "timeout, an exit Peeky inferred)",
            "warnings": "JSON list of the probe's warnings",
            "artifact_manifest_id": "Identity of the build the values came from",
            "source_sha256": "SHA-256 of the breakpoint's source file as it ran: for a candidate "
            "run, the edited text",
            "target_identity": "JSON target identity (toolchain, flags), with inputs_sha256: "
            "a digest of every source and header the run read (C/C++: the build's sources "
            "and headers; Rust: the crate's sources, Cargo.toml and Cargo.lock; Java: its "
            "sources; Python: the root's .py files), as they were on disk, a candidate's "
            "before its edit; without it for a run saved before Peeky recorded it",
            "edit": "For a candidate run (probe --old/--new), the unified diff of the edit the "
            "build had; NULL otherwise. Values never come from your files when this is set",
            "notes_kept": "1 where the run's notes are in captures.note (schema 9 on); NULL "
            "for a run saved before: a value its probe marked with a note shows unmarked",
            "set_outcomes": "JSON list, one per entry of sets, in its order: how each "
            "requested change went, {option, at, name, value, made: the stops it was made "
            "at, ended: made (a change at a hit, made), exit (the program ended), "
            "until_hits (the probe ended after its last hit), cap (a LINE@all's 100 "
            "stops), refused or failed (at hit: its own change there), timeout or failed "
            "(the probe's, first), or lost_stop (the debugger lost a stop, which may have "
            "run without it), complete: carried out in full}; NULL for a run saved before "
            "schema 16, or with no interventions. Queries read it only where every entry "
            "matches its request (option, at, name, value, each key of its type); else "
            "they read the run's warnings",
        },
    ),
    "captures": (
        "One row per captured selector, per hit and phase.",
        {
            "run_id": "-> runs.run_id",
            "hit": "Hit number, from 1",
            "phase": "observed, after-set (the value after that hit's interventions), or "
            "returned (the `(return)` row: what the call returned, read where it returned "
            "to; line is that line, function the function that returned)",
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
            "(write num * 1.0 / k), and SUM fails past 64 bits (TOTAL(num) does not). A hex "
            "address is NULL here: address(value) reads it",
            "unavailable": "1 when the debugger has no value here (optimized out, not live)",
            "error": "Why there is no value (unknown name, unreadable, ...), else NULL",
            "view": "Qualified value view (e.g. a Rust Option); 'stale' (whatever its view) "
            "for bytes that are not a trustworthy value: a Rust local's that may be moved or "
            "dropped (the bytes it left), or a caller's container a call may be changing; "
            "else NULL",
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
            "raw (a C/C++ local a loop returns to, a program using setjmp; a Python local "
            "a loop's pass has not assigned yet; a Rust value shown as LLDB's summary, a "
            "pointer it does not follow, a moved local): read "
            "the value with it. NULL for a plain value (and for runs saved before schema 8)",
            "instance": "That function as the build names it where the frame's name does not "
            "tell it apart (Rust: a closure, `cz::main::{closure#0}`; a generic's instance "
            "with its type arguments, `cz::gen_apply<i32>`, cut at 300 characters with `…`), "
            "as a probe table's function column shows it; else NULL (and for runs saved before schema 13). short_function(function) "
            "gives every closure of a function, and every instance of a generic, one name: "
            "instance tells them apart",
            "stop": "Which of the line's breakpoint locations in its function the hit was at, "
            "1 to N in address order, where the line has several there (a macro's or a "
            "block's own code, a loop header's): a pass stops at each, so hits count stops, "
            "not passes. A `(return)` row has its hit's (where its call was stopped, not the "
            "line it returned to; from schema 15, filled in for earlier runs from the hit's "
            "other rows where each has one and all agree). NULL for a line with one (and for "
            "runs saved before schema 14, and an earlier run's `(return)` row whose hit's "
            "other rows give no stop or disagree)",
            "element_size": "C and C++: the size in bytes of what a pointer points at, or of "
            "one element of an array of a constant length, from the build's debug "
            "information; NULL for other values, other languages and runs saved before "
            "schema 17. (address(p.value) - address(q.value)) / p.element_size is how many "
            "elements apart two pointers of one type are",
            "at_element_of": "C and C++: the selector of the one array (or other object) "
            "captured at the same hit, phase and frame at one of whose elements' addresses "
            "this pointer is: the pointer points at the array's element type, and its "
            "address is a whole number of elements from the array's first, at most one past "
            "its end. An address fact: another object can share that address (a union's "
            "other member, the next array, reused storage), so it never says which object "
            "the pointer designates. NULL where any of that is not proven (the array is a "
            "pointer itself, such as a parameter), where several captured bases match, and "
            "for runs saved before schema 17",
            "element_index": "With at_element_of: which element's address, from 0. The "
            "array's length (1 for another object) is the address one past its end, which C "
            "lets a pointer hold but never read; else NULL",
            "offset_from": "C and C++, a pointer with no at_element_of: the selector of another "
            "pointer to the same type, captured at the same hit, phase and frame, that its "
            "address is counted from by address arithmetic alone: not proven to point into "
            "one array, so no element is named. The base is the first such pointer that is "
            "a parameter of the function (an array a caller passed), else the first "
            "captured; it has none itself. NULL where the difference is not a whole number "
            "of elements or over 2**20 of them, and for runs saved before schema 17",
            "element_offset": "With offset_from: how many elements (element_size bytes each) "
            "past that pointer's address this one's is; negative where it is before. The "
            "addresses differ by element_offset * element_size bytes. Else NULL",
        },
    ),
    "interventions": (
        "One row per applied --set or --set-at, in the order they were applied: a "
        "--set-at LINE@all has one per stop it was made at.",
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
            "stop": "Which of its line's breakpoint locations in that function the stop was "
            "at, 1 to N in address order, where the line has several there: hit counts "
            "every one, so @HIT names a stop, not a pass. NULL for a line with one (and for "
            "runs saved before schema 14)",
        },
    ),
    "pauses": (
        "How long the program was paused at each hit.",
        {"run_id": "-> runs.run_id", "hit": "Hit number", "ms": "Pause in milliseconds"},
    ),
}
_EVIDENCE_COLUMNS = (
    "run_id, started_at, label, program, line, status, hit, stop, phase, function, "
    "instance, selector, type, "
    "value, num, unavailable, error, view, note, identity, scope, element_size, "
    "at_element_of, element_index, offset_from, element_offset, args, sets, "
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
        c.line, r.status, c.hit, c.stop, c.phase, c.function, c.instance, c.selector,
        c.type, c.value, c.num,
        c.unavailable, c.error, c.view, c.note, c.identity, c.scope, c.element_size,
        c.at_element_of, c.element_index, c.offset_from, c.element_offset, r.args, r.sets,
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
        error TEXT, view TEXT, identity INTEGER, scope TEXT, note TEXT, instance TEXT,
        stop INTEGER, element_size INTEGER, at_element_of TEXT, element_index INTEGER,
        offset_from TEXT, element_offset INTEGER)"""
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
        source_sha256 TEXT, target_identity TEXT, edit TEXT, notes_kept INTEGER, set_outcomes TEXT)""",
    _CAPTURES.format("captures"),
    "CREATE TABLE interventions (run_id INTEGER NOT NULL, hit INTEGER, name TEXT, "
    "before TEXT, after TEXT, line INTEGER, capture_hit INTEGER, function TEXT, instance TEXT, "
    "stop INTEGER)",
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
# version 13 captures.instance (a closure's or generic instance's label, NULL before);
# version 14 adds captures.stop and interventions.stop (#157), which of a line's breakpoint
# locations a hit or a change was at; version 15 gives a `(return)` row its hit's stop
# (#174), from that hit's other rows where they agree (NULL where they give none, as with
# `--capture return` alone, or disagree); version 16 adds runs.set_outcomes, how each
# requested change went (NULL for earlier runs); version 17 adds captures.element_size,
# at_element_of and element_index (#302), at which element's address of a captured array
# a C or C++ pointer is, and offset_from and element_offset, how far it is from another
# pointer by address alone (NULL for earlier runs).
# Views name columns of the newest tables, so earlier steps only drop them and the
# last creates them.
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


def _one_stop(column="stop"):
    """SQL aggregate: the one stop of a group of rows, only where each has one and all
    agree, else NULL (none labelled, or contradictory evidence): a stop is never picked
    from several (#174)."""
    return (
        f"CASE WHEN COUNT(DISTINCT {column}) = 1 AND COUNT({column}) = COUNT(*) THEN "
        f"MIN({column}) END"
    )


def _hits_stop(row):
    """SQL: the stop of the hit of the captures row aliased ``row``, as its rows other
    than `(return)` rows give it (`_one_stop`)."""
    return (
        f"(SELECT {_one_stop('o.stop')} FROM captures o WHERE o.run_id = {row}.run_id AND "
        f"o.hit = {row}.hit AND o.phase != 'returned')"
    )


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
    12: (*_DROP_VIEWS, _add_columns(("instance", "TEXT"))),
    13: (
        *_DROP_VIEWS,
        _add_columns(("stop", "INTEGER")),
        _add_columns(("stop", "INTEGER"), table="interventions"),
    ),
    # Every row of a hit is from one stop: its observed and after-set rows say which,
    # where they agree.
    14: (
        *_DROP_VIEWS,
        f"UPDATE captures SET stop = {_hits_stop('captures')} WHERE phase = 'returned' AND "
        "stop IS NULL",
    ),
    15: (*_DROP_VIEWS, _add_columns(("set_outcomes", "TEXT"), table="runs")),
    16: (
        *_DROP_VIEWS,
        _add_columns(
            ("element_size", "INTEGER"),
            ("at_element_of", "TEXT"),
            ("element_index", "INTEGER"),
            ("offset_from", "TEXT"),
            ("element_offset", "INTEGER"),
        ),
        _EVIDENCE_VIEW,
        _LAST_RUN_VIEW,
    ),
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
    "o.run_id = s.run_id AND o.hit = s.hit AND o.stop IS s.stop AND o.selector = "
    "s.selector AND o.phase = 'observed' ORDER BY o.rowid LIMIT 1)"
).format(shown=_shown("s"), observed=_shown("o"))
# Where both runs label their stops (#157: a line with several breakpoint locations in a
# function), a hit is also its stop: the same hit at another stop is another row.
_STOPPED = "EXISTS (SELECT 1 FROM captures WHERE run_id = {} AND stop IS NOT NULL)"


def _requested(alias, outcomes=True):
    """SQL: a run's requested interventions (runs.sets), each as [name, value, "requested
    at LINE@HIT"], a --set at its probe line's set_hit too (ninth evaluation: `diff`'s
    `(sets)` row and `outputs` did not say which hit a --set was for). With ``outcomes``,
    each says how it went, from runs.set_outcomes where the run recorded it (`_RECORDED`;
    `_set_outcome`: review of PR #278, a `LINE@all` made at some stops only read like one
    made at all). A run that did not (saved before schema 16, or with an outcome that does
    not match its request) says what the probe's own warning did: ", not made: that hit
    never came" (", not made: its line never stopped" for a `LINE@all`), or ", made only
    at the first N stops: the cap" (the warning for a run's --sets names them all, as they
    share their hit; a --set-at's names its LINE@HIT, name and value; review of PR #214);
    else nothing, which claims nothing of how it was applied. Without ``outcomes``: the
    requests alone, which say whether two runs asked for the same changes."""
    at = (
        f"CASE WHEN json_array_length(s.value) = 2 THEN {alias}.line || '@' || "
        f"COALESCE({alias}.set_hit, 1) ELSE json_extract(s.value, '$[2]') END"
    )
    if not outcomes:
        return (
            "(SELECT json_group_array(json_array(json_extract(s.value, '$[0]'), "
            f"json_extract(s.value, '$[1]'), 'requested at ' || {at})) FROM "
            f"json_each(COALESCE({alias}.sets, '[]')) s)"
        )
    warned = f"json_each(COALESCE({alias}.warnings, '[]')) w"
    asked = (
        f"':' || {at} || ' ' || json_extract(s.value, '$[0]') || '=' || "
        "json_extract(s.value, '$[1]')"
    )
    unmade = (
        f"EXISTS (SELECT 1 FROM {warned} WHERE "
        "CASE WHEN json_array_length(s.value) = 2 THEN w.value LIKE '--set %' AND "
        "instr(w.value, ' not applied: the program stopped ') > 0 ELSE w.value LIKE "
        f"'--set-at %' AND instr(w.value, {asked} || ' was not applied: ') > 0 END)"
    )
    first = f"{asked} || ' was made only at the first '"
    # How many stops an @all cut short at its cap was made at, from its warning.
    capped = (
        "(SELECT substr(rest, 1, instr(rest, ' stops of line ') - 1) FROM (SELECT "
        f"substr(w.value, instr(w.value, {first}) + length({first})) AS rest FROM {warned} "
        "WHERE json_array_length(s.value) = 3 AND w.value LIKE '--set-at %' AND "
        f"instr(w.value, {first}) > 0 LIMIT 1))"
    )
    warning = (
        f"CASE WHEN {unmade} THEN ', not made: ' || CASE WHEN {at} LIKE '%@all' THEN 'its "
        f"line never stopped' ELSE 'that hit never came' END WHEN {capped} IS NOT NULL THEN "
        f"', made only at the first ' || {capped} || ' stops: the cap' ELSE '' END"
    )
    outcome = (
        f"CASE WHEN {_RECORDED.format(r=alias)} THEN set_outcome(json_extract("
        f"{alias}.set_outcomes, '$[' || s.key || ']'), s.value, {alias}.line, "
        f"{alias}.set_hit) END"
    )
    return (
        "(SELECT json_group_array(json_array(json_extract(s.value, '$[0]'), "
        f"json_extract(s.value, '$[1]'), 'requested at ' || {at} || COALESCE({outcome}, "
        f"{warning}))) FROM json_each(COALESCE({alias}.sets, '[]')) s)"
    )


# Whether a run recorded how each of its requested changes was applied: a runs.set_outcomes
# array with, for each entry of runs.sets in its order, an outcome `_set_outcome` accepts
# for that request (schema 16 on). Its outcomes are read only then; two runs whose requests
# are the same were made alike only where both recorded it (reviews 2 and 3 of PR #278).
_RECORDED = (
    "(CASE WHEN {r}.set_outcomes IS NULL OR NOT json_valid({r}.set_outcomes) THEN 0 WHEN "
    "json_type({r}.set_outcomes) IS NOT 'array' THEN 0 ELSE json_array_length("
    "{r}.set_outcomes) = json_array_length(COALESCE({r}.sets, '[]')) AND NOT EXISTS (SELECT "
    "1 FROM json_each({r}.set_outcomes) o WHERE set_outcome(o.value, json_extract({r}.sets, "
    "'$[' || o.key || ']'), {r}.line, {r}.set_hit) IS NULL) END)"
)
# What `ended` may say (probe.py's `_set_outcomes`), and which need the `hit` they name.
_ENDED = {"made", "exit", "until_hits", "cap", "refused", "failed", "timeout", "lost_stop"}
_OUTCOME_KEYS = {"option", "at", "name", "value", "made", "ended", "complete"}


def _checked_outcome(text, entry, line, set_hit):
    """The outcome in ``text`` (a runs.set_outcomes entry) where it is one for the request
    ``entry`` (its runs.sets entry: [name, value] for a --set at ``line``@``set_hit``,
    [name, value, "LINE@HIT"] for a --set-at): every key, of its type, with the request's
    option, location, name and value, and what such a request can end with (review 3 of
    PR #278: an outcome without `at`, another request's, or entries swapped read as
    recorded). Else None."""
    try:
        outcome, request = json.loads(text), json.loads(entry)
    except (TypeError, ValueError):
        return None
    if not isinstance(outcome, dict) or not isinstance(request, list):
        return None
    if not _OUTCOME_KEYS <= set(outcome) or set(outcome) - _OUTCOME_KEYS - {"hit"}:
        return None
    made, ended, hit = outcome["made"], outcome["ended"], outcome.get("hit")
    # `ended` is checked as text first: a list or an object is no set member.
    if type(made) is not int or made < 0 or type(ended) is not str or ended not in _ENDED:
        return None
    if type(outcome["complete"]) is not bool:
        return None
    if ("hit" in outcome) != (ended == "refused" or (ended == "failed" and "hit" in outcome)):
        return None
    if "hit" in outcome and (type(hit) is not int or hit < 1):
        return None
    if len(request) == 2 and type(set_hit) in (int, type(None)) and type(line) is int:
        option, at = "--set", f"{line}@{1 if set_hit is None else set_hit}"
    elif len(request) == 3 and isinstance(request[2], str):
        option, at = "--set-at", request[2]
    else:
        return None
    if [outcome["option"], outcome["at"], outcome["name"], outcome["value"]] != [
        option,
        at,
        *request[:2],
    ]:
        return None
    every = option == "--set-at" and at.endswith("@all")
    # A change at one hit is refused or fails only at that hit (review 4 of PR #278).
    if not every and "hit" in outcome and str(hit) != at.rpartition("@")[2]:
        return None
    if every:
        # Each stop before the one it ended at was changed; a cap ends after some.
        if ended == "made" or (ended == "cap" and not made) or (hit and made != hit - 1):
            return None
    elif ended in ("cap", "lost_stop") or made not in (0, 1) or (made == 1) != (ended == "made"):
        return None
    if outcome["complete"] != (ended == "made" or (ended == "exit" and every and made > 0)):
        return None
    return outcome


def _set_outcome(text, entry, line, set_hit):
    """One requested change's outcome (a runs.set_outcomes entry, probe.py's
    `_set_outcomes`), checked against its request (`_checked_outcome`), as `outputs`,
    `compare` and `diff` say it after "requested at LINE@HIT": nothing for a change at a
    hit that was made; else how many stops it was made at and why no more were (`, made
    at 6 stops; refused at hit 7`). None where ``text`` is no outcome of that request."""
    outcome = _checked_outcome(text, entry, line, set_hit)
    if outcome is None:
        return None
    made, ended, hit = outcome["made"], outcome["ended"], outcome.get("hit")
    every = outcome["at"].endswith("@all") and outcome["option"] == "--set-at"
    stops = f"{made} stop{'' if made == 1 else 's'}"
    if ended == "made":
        return ""
    if hit is not None:
        what = f"{ended} at hit {hit}" if every else f"{ended} at that hit"
        return f", made at {stops}; {what}" if made else f", {what}"
    if not made:
        never = "its line never stopped" if every else "that hit never came"
        return {
            "exit": f", not made: {never}",
            "until_hits": f", not made: {never} before --until hits ended the probe",
            "lost_stop": f", not made: {never}, but the debugger lost a stop",
            "timeout": ", not made: the probe timed out first",
            "failed": ", not made: the probe failed first",
        }.get(ended)
    return {
        "exit": f", made at {stops}, to the program's end",
        "until_hits": f", made at {stops} before --until hits ended the probe",
        "cap": f", made at the first {stops}: the cap",
        "lost_stop": f", made at {stops}; the debugger lost a stop, which may have run without it",
        "timeout": f", made at {stops} before the probe timed out",
        "failed": f", made at {stops} before the probe failed",
    }.get(ended)


# A run's outcome: its status, and the error it ended with.
_OUTCOME = (
    "{r}.status || COALESCE(' [' || {r}.error_code || ']', '') || "
    "COALESCE(': ' || {r}.error_message, '')"
)
_OUTCOMES = (_OUTCOME.format(r="ra"), _OUTCOME.format(r="rb"))
# Run-level rows `diff` and `compare` share, each (hit, phase, selector, value_a, value_b,
# differs, note, pos): `(status)` where the runs' outcomes differ or either did not
# complete, with its error (review of PR #278: two runs refused at different hits read
# alike), and `(sets)`.
_STATUS_ROW = f"""SELECT NULL, NULL, '(status)', {_OUTCOMES[0]}, {_OUTCOMES[1]},
    {_OUTCOMES[0]} IS NOT {_OUTCOMES[1]}, CASE WHEN {_OUTCOMES[0]} IS NOT {_OUTCOMES[1]}
    THEN 'outcome differs' ELSE 'neither run completed, and both ended the same way' END, -2
    FROM runs ra, runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B}
    AND ({_OUTCOMES[0]} IS NOT {_OUTCOMES[1]} OR ra.status IS NOT 'completed'
    OR rb.status IS NOT 'completed')"""
# `(sets)`: each run's requests with how they went (`va`, `vb`), told apart by the requests
# alone (`qa`, `qb`) first (review 3 of PR #278: a schema-15 run against a schema-16 one
# with the same request read as different requests), then by how they went, only where
# both runs recorded it (`kept`). Callers add to its WHERE: `asked`, how many changes run
# a requested; `unknown`, whether the requests are the same but not both recorded.
_SETS_ROW = f"""SELECT NULL, NULL, '(sets)', va, vb, va IS NOT vb, CASE
    WHEN qa IS NOT qb THEN 'the runs requested different interventions (--set, --set-at), '
      || 'or made different ones'
    WHEN NOT kept THEN 'the same requested interventions; how they were applied wasn''t '
      || 'recorded for one or both older runs'
    WHEN va IS NOT vb THEN 'the runs requested different interventions (--set, --set-at), '
      || 'or made different ones'
    ELSE 'the same interventions, made alike' END, -1
    FROM (SELECT {_requested("ra")} AS va, {_requested("rb")} AS vb,
      {_requested("ra", outcomes=False)} AS qa, {_requested("rb", outcomes=False)} AS qb,
      {_RECORDED.format(r="ra")} AND {_RECORDED.format(r="rb")} AS kept,
      json_array_length(COALESCE(ra.sets, '[]')) AS asked_a,
      json_array_length(COALESCE(rb.sets, '[]')) AS asked_b
      FROM runs ra, runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B})
    WHERE 1"""
# The same requests, but how they went was not recorded for both runs: said, never taken
# for "made alike".
_SETS_UNKNOWN = "(qa IS qb AND asked_a > 0 AND NOT kept)"


_BOTH_STOPPED = f"({_STOPPED.format(_A)} AND {_STOPPED.format(_B)})"
# A row's stop, or its hit's: a `(return)` row has its hit's own since schema 15 (#174);
# one saved without it (#155) takes it from the other rows of its hit where they agree,
# as a store's upgrade to 15 does, else has none.
_HIT_STOP = f"COALESCE(s.stop, {_hits_stop('s')})"

# A value that differs only in addresses, where an address is a pointer's or a value's
# place, which can be another element's in the other run (#292: a pointer into an array
# held element 6's address in one run and element 4's in the other, and `diff` called it
# `addresses only`). That is a pointer or a reference row: its type ends in `*`, `&` or
# `&&`, after any `const`, `volatile` or `restrict` (rtrim drops their letters), or starts
# with Rust's `&` or `*` (`&[T]`, `*const T`). Or any value of a C, C++ or Rust run: a
# preview shows the pointers in it as addresses only, and LLDB's `T @ 0x...` is where the
# value is. Elsewhere, a Python repr's address or a Java object number, the masking
# stands: `addresses only`.
_POINTER_TYPE = "(rtrim({t}, ' _aceilnorstv') GLOB '*[*&]' OR {t} GLOB '[*&]*')"
_NATIVE_LANGUAGES = "('c', 'cpp', 'rust')"
# Each says what to do; `diff` puts `at N hits (...)` after `addresses differ`.
_POINTS_ELSEWHERE = (
    "addresses differ: it may point at another element too; compare offsets with address() "
    "(COOKBOOK 4.8)"
)
_HOLDS_ELSEWHERE = (
    "addresses differ: an address in it may be another element's too; compare offsets with "
    "address() (COOKBOOK 4.8)"
)
# A C or C++ pointer or reference that shows what it points at after its address, with
# no other address in it (a string's text, a struct's plain members: `0x… "Maya"`), and
# reads the same in both runs: it may still be another element that reads the same, but
# no offset is needed to tell that much (eleventh evaluation: a `const std::string &` was
# sent to address()). It ranks with the address-only values.
_READS_ALIKE = (
    "addresses only: what it points at reads the same in both runs, though it may be "
    "another element that reads the same"
)
_C_LANGUAGES = "('c', 'cpp')"
_SHOWS_POINTEE = (
    "(SELECT m LIKE '0x… %' AND instr(substr(m, 5), '0x…') = 0 AND trim(substr(m, 5)) != '' "
    "FROM (SELECT no_addresses(a.v, {}) AS m))"
)
_ADDRESS_KIND = f"""CASE
          WHEN ({_POINTER_TYPE.format(t="a.t")} OR {_POINTER_TYPE.format(t="b.t")})
            AND {_LANGUAGE.format(_A)} IN {_C_LANGUAGES} AND {_LANGUAGE.format(_B)} IN
            {_C_LANGUAGES} AND {_SHOWS_POINTEE.format(_LANGUAGE.format(_A))}
            THEN '{_READS_ALIKE}'
          WHEN {_POINTER_TYPE.format(t="a.t")} OR {_POINTER_TYPE.format(t="b.t")}
            THEN '{_POINTS_ELSEWHERE.replace("'", "''")}'
          WHEN {_LANGUAGE.format(_A)} IN {_NATIVE_LANGUAGES}
            OR {_LANGUAGE.format(_B)} IN {_NATIVE_LANGUAGES}
            THEN '{_HOLDS_ELSEWHERE.replace("'", "''")}'
          ELSE 'addresses only' END"""


# A C or C++ pointer at an element's address of the same captured array in both runs
# (captures.at_element_of, #302), each element's by its index. Another element's address is
# an address fact, ranked first among the addresses that differ, never a real difference
# (review 2 of PR #312): the pointer may not have changed while the array moved (a stale
# pointer, storage reused, the allocator's layout), so the index can change by layout
# alone. The same element's address, with addresses that differ, is no more than that.
_ELSEWHERE = "a.pi IS NOT NULL AND a.pi IS b.pi AND a.ei IS NOT b.ei"
_AT_ELEMENTS = """'the address of `' || a.pi || '[' || a.ei || ']` in run a, of `' || b.pi || '['
        || b.ei || ']` in run b; by address alone: the pointer or the array may have moved'"""


def _from(run):
    """SQL: `level + 6` (`level - 2`), ``run``'s offset by address from its base pointer."""
    return (
        f"'`' || {run}.ofs || CASE WHEN {run}.eo < 0 THEN ' - ' || -{run}.eo ELSE ' + ' || "
        f"{run}.eo END || '`'"
    )


# A C or C++ pointer counted from the same captured pointer in both runs by address alone
# (captures.offset_from): its offsets are shown, but rank with the addresses that differ
# (review of PR #312). Two pointers of one type may point into separate blocks within the
# cap (`src`, `dst`), whose distance is the allocator's layout, not the program's, so
# another offset need not be another element; and the base itself may point elsewhere.
_OFFSETS = f"""'addresses differ: by address, ' || CASE WHEN a.eo IS b.eo
      THEN {_from("a")} || ' in both runs' ELSE {_from("a")} || ' in run a and ' || {_from("b")}
      || ' in run b' END || '; nothing proves they point into one array, so it may or may not '
      || 'be another element'"""


_PAIR = f"""WITH a AS (SELECT hit, phase, selector, {_shown("s")} AS v, type AS t, rowid AS pos,
    {_caveat("s")} AS c, {_SAME} AS same, CASE WHEN {_BOTH_STOPPED} THEN {_HIT_STOP} END AS st,
    at_element_of AS pi, element_index AS ei, offset_from AS ofs, element_offset AS eo
    FROM captures s WHERE run_id = {_A}),
  b AS (SELECT hit, phase, selector, {_shown("s")} AS v, type AS t, rowid AS pos,
    {_caveat("s")} AS c, {_SAME} AS same, CASE WHEN {_BOTH_STOPPED} THEN {_HIT_STOP} END AS st,
    at_element_of AS pi, element_index AS ei, offset_from AS ofs, element_offset AS eo
    FROM captures s WHERE run_id = {_B}),
  k AS (SELECT hit, phase, selector, st FROM a UNION SELECT hit, phase, selector, st FROM b),
  kinds AS (SELECT k.hit, k.phase, k.selector, k.st, a.v AS value_a, b.v AS value_b,
    a.v IS NOT b.v AS differs, COALESCE(a.pos, b.pos) AS pos,
    CASE
      WHEN k.selector NOT IN {_ASKED.format(_B)} THEN 'only in run a'
      WHEN k.selector NOT IN {_ASKED.format(_A)} THEN 'only in run b'
      WHEN k.phase = 'after-set' AND b.pos IS NULL THEN 'intervention only in run a'
        || COALESCE(' (run a: stop ' || k.st || ')', '')
      WHEN k.phase = 'after-set' AND a.pos IS NULL THEN 'intervention only in run b'
        || COALESCE(' (run b: stop ' || k.st || ')', '')
      WHEN b.pos IS NULL THEN 'missing in run b' || COALESCE(' (run a: stop ' || k.st || ')', '')
      WHEN a.pos IS NULL THEN 'missing in run a' || COALESCE(' (run b: stop ' || k.st || ')', '')
      WHEN a.v IS NOT b.v AND a.t IS NOT NULL AND b.t IS NOT NULL AND a.t IS NOT b.t
        THEN 'type ' || a.t || ' -> ' || b.t
      WHEN a.v IS NOT b.v AND no_addresses(a.v, {_LANGUAGE.format(_A)})
        IS no_addresses(b.v, {_LANGUAGE.format(_B)}) THEN CASE
          WHEN {_ELSEWHERE} THEN {_AT_ELEMENTS}
          WHEN a.pi IS NOT NULL AND a.pi IS b.pi
            THEN 'addresses only: the address of the same element of `' || a.pi
              || '` in both runs'
          WHEN a.ofs IS NOT NULL AND a.ofs IS b.ofs THEN {_OFFSETS}
          ELSE {_ADDRESS_KIND} END
      -- The same address in both runs, at another element's of an array that moved: said
      -- (`compare`), but no difference (`diff` leaves it out).
      WHEN a.v IS b.v AND {_ELSEWHERE} THEN {_AT_ELEMENTS}
    END AS kind,
    CASE WHEN a.c IS NOT NULL OR b.c IS NOT NULL THEN 'qualified (' || CASE
      WHEN a.c IS NOT NULL AND b.c IS NOT NULL THEN 'run a: ' || a.c || ', run b: ' || b.c
      WHEN a.c IS NOT NULL THEN 'run a: ' || a.c ELSE 'run b: ' || b.c END
      || '): not evidence by itself; queries run shows why' END AS qualified,
    k.phase = 'after-set' AND COALESCE(a.same, 1) AND COALESCE(b.same, 1) AS unchanged
    FROM k LEFT JOIN a ON a.hit = k.hit AND a.phase = k.phase AND a.selector = k.selector
      AND a.st IS k.st
    LEFT JOIN b ON b.hit = k.hit AND b.phase = k.phase AND b.selector = k.selector
      AND b.st IS k.st),
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
# for selectors only one run captured). Then values changed by an intervention only one
# run made, before what may follow from them (tenth evaluation: they came after 70 rows
# of its effects); then real divergences, in hit and capture order (a hit only one run
# reached is one); then pointers whose addresses differ: first a C or C++ pointer at
# another element's address of an array both runs captured, in hit order (`_AT_ELEMENTS`,
# #302), then those that may point at another element (`_ADDRESS_KIND`, and `_OFFSETS`,
# by address from another pointer): not with the real divergences, since the stack and
# heap move
# between runs, so most pointers differ whether or not they point at another element,
# and the first real difference stays the first divergence; then values a view or a note
# qualifies in either run (a C local's leftovers
# on a `do` body's first line, a moved Rust local's bytes: review of #139), whatever else
# their note says (`missing in run b; qualified (run a: note)`); then other values
# that differ only in memory addresses (a C or C++ pointer at the same element's address,
# or one whose text after its address reads the same: `_READS_ALIKE`); last, one row per
# selector only one run asked
# for. Nothing is dropped: an address-only or qualified difference may still be real;
# but an after-set value equal to its run's observed value at that hit, in each run
# with one, is no difference of its own (the observed row has any: seventh evaluation,
# finding 10), and one selector's address-only rows are one row, its first hit's, that
# says how many hits there were (finding 9), one for each stop where both runs label
# them; so are its rows whose addresses differ.
# The tiers' ranks that code reads too: the real divergences (`_after_first` finds the
# first among them, never an address or a qualified value: review of PR #294), and the
# selectors only one run captured, which are at no hit of their own.
_REAL_RANK, _ONLY_IN_RANK = 10, 15
# Every row of `diff` with its rank (the tier it is in) and pos (its capture order).
_RANKED = f"""(
  SELECT {_REAL_RANK} AS rank, hit, phase, selector, value_a, value_b, differs, note, pos
    FROM pair WHERE differs AND qualified IS NULL AND NOT unchanged
    AND (kind IS NULL OR kind LIKE 'missing%' OR kind LIKE 'type %')
  -- More than 3 alike (a --set-at LINE@all's, #265) are one row, its first hit's: hit and
  -- the values are those of the MIN(pos) row (SQLite's bare columns).
  UNION ALL SELECT 6, hit, phase, selector, value_a, value_b, differs,
    CASE WHEN alike > 3 THEN note || ', at ' || alike || ' hits (one row per selector and '
      || 'value: its first hit)' ELSE note END, MIN(pos)
    FROM (SELECT *, COUNT(*) OVER (PARTITION BY selector, note, value_a, value_b) AS alike
      FROM pair WHERE qualified IS NULL AND kind LIKE 'intervention only%' AND NOT unchanged)
    GROUP BY selector, note, value_a, value_b, CASE WHEN alike > 3 THEN NULL ELSE pos END
  -- First among the addresses that differ, each at its own hit: another element's address
  -- of the same captured array, which an address alone may explain (#302). With no
  -- qualified value, note is kind.
  UNION ALL SELECT 11, hit, phase, selector, value_a, value_b, differs, kind, pos FROM pair
    WHERE differs AND qualified IS NULL AND NOT unchanged AND kind LIKE 'the address of %'
  -- As rank 14 below, but the count goes after `addresses differ`, before what to do. With
  -- no qualified value, note is kind: named once, the GROUP BY's terms stay well within
  -- SQLite's limit, the column limit Peeky sets (#302).
  UNION ALL SELECT 12, hit, phase, selector, value_a, value_b, differs,
    CASE WHEN COUNT(*) > 1 THEN 'addresses differ at ' || COUNT(*) || ' hits'
      || COALESCE(' at stop ' || st || ' (one row per selector and stop', ' (one row per '
      || 'selector') || ': its first hit)' || substr(kind, length('addresses differ') + 1)
      ELSE kind END, MIN(pos) FROM pair
    WHERE qualified IS NULL AND kind LIKE 'addresses differ:%' AND NOT unchanged
    GROUP BY selector, phase, st, kind
  UNION ALL SELECT 13, hit, phase, selector, value_a, value_b, differs, note, pos FROM pair
    WHERE differs AND qualified IS NOT NULL AND (kind IS NULL OR kind NOT LIKE 'only in%')
    AND NOT unchanged
  -- hit and the values are those of the MIN(pos) row (SQLite's bare columns).
  UNION ALL SELECT 14, hit, phase, selector, value_a, value_b, differs,
    CASE WHEN COUNT(*) > 1 THEN 'addresses only, at ' || COUNT(*) || ' hits'
      || COALESCE(' at stop ' || st || ' (one row per selector and stop', ' (one row per '
      || 'selector') || ': its first hit)' || substr(kind, length('addresses only') + 1)
      ELSE kind END, MIN(pos) FROM pair
    WHERE qualified IS NULL AND (kind = 'addresses only' OR kind LIKE 'addresses only:%')
    AND NOT unchanged
    GROUP BY selector, phase, st, kind
  -- hit, the values and qualified are those of the MIN(pos) row (SQLite's bare columns).
  UNION ALL SELECT {_ONLY_IN_RANK}, hit, phase, selector, value_a, value_b, 1,
    kind || ' (one row per selector: its first hit)' || COALESCE('; ' || qualified, ''),
    MIN(pos) FROM pair WHERE kind LIKE 'only in%' GROUP BY selector, kind
  UNION ALL SELECT 0, * FROM ({_STATUS_ROW})
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
  UNION ALL SELECT 5, * FROM ({_SETS_ROW} AND (va IS NOT vb OR {_SETS_UNKNOWN})))"""
_DIFF = f"""{_PAIR}
SELECT {_PAIR_COLUMNS} FROM {_RANKED}
ORDER BY rank, hit, phase = 'after-set', pos"""
# The same rows' tiers and hits, for `_after_first`.
_DIFF_RANKS = f"""{_PAIR}
SELECT rank, hit FROM {_RANKED} WHERE hit IS NOT NULL"""
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


# A hex token address() reads: 0x and hex digits, a whole word. Unlike _ADDRESS (which
# masks only what is surely an address), any length counts, for the caller asked for an
# address: a non-PIE Linux program's brk heap is at 0x4052a0 (review of PR #277).
_HEX_TOKEN = re.compile(r"\b0x([0-9a-fA-F]+)\b")


def _address(value):
    """The first `0x` token outside quoted text, as an INTEGER, else NULL.

    Registered as the SQL function address(text), to compare addresses across runs by
    their offset from a base each run has (eval 10: kitty's agent joined runs on
    `substr(value, -3)`). It is lexical, not typed: in a struct's preview it reads the
    first hex field, whatever it is, so it is for a pointer's own selector. NULL where
    that first token is zero (a null pointer) or longer than 16 hex digits, and for a
    string's text (a value that starts with a quote; a Java String is java-debug's
    unescaped text) and a Java object (object_ref). SQLite's integers are signed 64-bit,
    so from 2**63 on it is the two's complement, a negative number: two such addresses
    still subtract and order correctly, and printf('0x%x', ...) gives the hex back.
    """
    if not isinstance(value, str) or value[:1] in ('"', "'") or _OBJECT_REF.match(value):
        return None
    for part in _TEXT.split(value):
        if part[:1] not in ('"', "'") and (match := _HEX_TOKEN.search(part)):
            if len(match[1]) > 16 or not (number := int(match[1], 16)):
                return None
            return number - (1 << 64) if number >= 1 << 63 else number
    return None


# A run's output without its final line ends (`queries runs`' output_end; eleventh
# evaluation: output_start cut `…;FREE=39` and `…;FREE=32` alike, at `…;FREE`).
_OUTPUT_BODY = "rtrim(output, char(13, 10))"

_RUN_PARAM = {"run": "Run id (default: the newest run)"}
_PAIR_PARAMS = {
    "a": "First run id (default: the run before b)",
    "b": "Second run id (default: the newest run)",
}

# name -> (description, SQL, {param: description}, required params)
QUERIES = {
    "runs": (
        "Recent runs, newest first: where, inputs, outcome; output_start is the output's first "
        "60 characters, and output_end its last 40 where it is longer (NULL where it is not; "
        "a final newline left out), since outputs often differ at their end",
        "SELECT run_id, started_at, label, program || ':' || line || "
        "CASE WHEN edit IS NULL THEN '' ELSE ' (candidate)' END AS at, args, sets, status, "
        "hits, seconds, error_code, substr(output, 1, 60) AS output_start, "
        f"CASE WHEN length({_OUTPUT_BODY}) > 60 THEN '…' || substr({_OUTPUT_BODY}, -40) END "
        "AS output_end FROM runs ORDER BY run_id DESC LIMIT COALESCE(:count, 20)",
        {"count": "How many runs (default 20)"},
        (),
    ),
    "run": (
        "Every captured value of one run, in capture order; error says why a value is NULL; "
        "view and note qualify a value (read it with them); stop says which of the line's "
        "breakpoint locations a hit was at, where it has several",
        f"SELECT hit, stop, phase, line, function, selector, type, value, {_ERROR} AS error, view, "
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
WHERE run_id = {_RUN} AND phase IN ('observed', 'returned')
  AND (:selector IS NULL OR selector = :selector)
WINDOW w AS (PARTITION BY selector ORDER BY hit) ORDER BY selector, hit""",
        {**_RUN_PARAM, "selector": "Only this selector (default: all)"},
        (),
    ),
    "compare": (
        "Every value of two runs side by side, in hit and capture order; note says when a "
        "value is missing from a run, qualified in either (a view or a note: not evidence "
        "by itself) or differs only in memory addresses (`addresses differ: ...` where an "
        "address may be another element's: a pointer or a reference, or any C, C++ or Rust "
        "value; else `addresses only`); a C or C++ pointer at an element's address of an array "
        "both runs captured says which in each (`the address of ...`, `addresses only: the "
        "address of the same element of ...`), and one counted from the same pointer by "
        "address its offsets (`addresses differ: by address, ...`). First a (status) row "
        "where either "
        "run did not complete (its error too), and a (sets) row where either requested "
        'interventions: each [name, value, "requested at LINE@HIT"] with how it went, as '
        "`outputs` says it; requests that read alike are said to be made alike only where "
        "both runs recorded how (runs.set_outcomes)",
        f"{_PAIR}\nSELECT {_PAIR_COLUMNS} FROM (SELECT hit, phase, selector, value_a, "
        f"value_b, differs, note, pos FROM pair UNION ALL SELECT * FROM ({_STATUS_ROW}) "
        f"UNION ALL SELECT * FROM ({_SETS_ROW} AND (asked_a > 0 OR asked_b > 0))){_PAIR_ORDER}",
        _PAIR_PARAMS,
        (),
    ),
    "diff": (
        "Only the values that differ between two runs. First (status), (output), (edit), (source), "
        "(line), (args) and (sets) rows when the runs' outcomes, outputs, edits, source, stop "
        "lines, program arguments or interventions differ ((status), with each run's error, also "
        "where either run did not complete; (sets) says how each change went, as `outputs` does); "
        "then values only one run's intervention changed (an after-set value equal to its hit's "
        "observed one is left out; more than 3 alike are one row, its first hit's); then real "
        "differences in hit and capture order (the first is the first divergence); then C or C++ "
        "pointers at another element's address of an array both runs captured (`the address "
        "of ...`, by address alone: the pointer or the array may have moved), in hit order; "
        "then other values "
        "whose addresses differ where an address may be another element's (a pointer or a "
        "reference, or any C, C++ or Rust value: note `addresses differ: ...`, which says to "
        "compare offsets with address(), or shows a C or C++ pointer's offsets by address from "
        "another), one row per selector (and stop); then values a "
        "view or a note qualifies in either run (note: qualified (run a: note), not evidence by "
        "itself: leftovers, a moved Rust local's bytes); then other address-only differences (a "
        "Python repr, a Java object number, a C or C++ pointer at the same element's address, "
        "or one whose text after its address reads the same), one row "
        "per selector (and stop); last, selectors only one run captured. Hits pair by number, and "
        "by stop where both runs label them (a line with several breakpoint locations): where the "
        "runs stopped a different number of times, or a hit at another stop, a note names `join`, "
        "which pairs by value (and, for the first case, says how many rows come after the first "
        "hit with a real difference, if any); where they stopped as often, a note says what suggests the hits do not "
        "line up, or what plain values agree between the runs at every hit (where they are "
        "unique across the hits, that pairing by hit number is what join by the fewest of them "
        "(at most 8) gives, named whole, with masked=1 where they agree only with addresses "
        "masked; never that the hits are the same moments). A selector that differs after an "
        "intervention or an edit may "
        "follow from it, so it is left out of what agrees; where its values line up better at "
        "other hits, the note says so, that the change may account for it, and that the hits are "
        "not said to line up. Such a note's first line is its verdict (hits pair by, hits pair "
        "as far as, or hits may not line up), then each detail on a line of its own after `- `. "
        "The text table shows a long value from just before where it first "
        "differs from the other run's, and (sets) as a count and the first change that differs; "
        "--json has them whole",
        _DIFF,
        _PAIR_PARAMS,
        (),
    ),
    "outputs": (
        'Inputs, requested interventions (sets: each [name, value, "requested at '
        'LINE@HIT"], a --set\'s too, with how it went where recorded: ", not made: that hit '
        'never came", ", made at 6 stops; refused at hit 7", ", made at 1 stop before --until '
        'hits ended the probe"), outcome and program output of two runs',
        f"SELECT run_id, label, args, {_requested('r')} AS sets, status, error_code, output "
        f"FROM runs r WHERE run_id IN ({_A}, {_B}) ORDER BY run_id",
        _PAIR_PARAMS,
        (),
    ),
    "set_effect": (
        "Each selector before and after the run's interventions, in the order they were "
        "applied, with the changes made at that stop (line, and hit: that line's own); "
        "caveat_observed and caveat_after_set name a view or `note` that qualifies a value. "
        "A change at a stop with no capture (a --set-at line) is one row: selector is what "
        "it changed, observed and after_set its value before and after (the set's own "
        "read-back), function whose variable it was. stop says which of the line's breakpoint "
        "locations the stop was at, where it has several (hit counts each). The text table "
        "shows a long value, and each change in interventions, from just before where its "
        "two values first differ; --json has them whole",
        f"""SELECT hit, stop, line, function, selector, observed, after_set, caveat_observed,
  caveat_after_set, interventions FROM (
  SELECT o.hit, o.stop, o.line, o.function, o.selector, {_shown("o")} AS observed,
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
  SELECT i.hit, i.stop, i.line, i.function, i.name, i.before, i.after, NULL, NULL,
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
        "AS last_run FROM evidence WHERE selector = :selector AND phase IN ('observed', "
        "'returned') AND "
        "NOT candidate GROUP BY program, line, value ORDER BY seen - qualified DESC, seen "
        "DESC, value",
        {"selector": "Selector, e.g. request.page (required)"},
        ("selector",),
    ),
    "wide": (
        "One row per hit (and phase) of one run (with stop, which of the line's breakpoint "
        "locations it was at, for a line with several), a column per selector in capture order "
        "(a name SQLite would take for an earlier column, ignoring case, gets ` (2)`), each "
        "cell the value as stored. no_value is a JSON object naming the cells with no value "
        "(NULL: unavailable, withheld, an error) and why; a selector not captured at that "
        "hit is NULL and not named. caveats names the cells whose value is qualified: "
        "{selector: view} for stale, unproven, opaque or budget-exhausted, else "
        "{selector: 'note'} for a row with a note (queries run shows it): read those with "
        "it, never as plain values. --json gives the generated SQL too (`sql`), to refine "
        "with peeky sql",
        lambda path, params: _wide(path, params),
        {
            **_RUN_PARAM,
            "phase": "observed, after-set or returned (default: all, in that order)",
        },
        (),
    ),
    "join": (
        "Two runs joined by a selector's value instead of by hit (or several selectors', "
        "key=a,b): each pair of hits, one per run, where the key has the same value (as "
        "stored text; a value at several hits pairs every hit of a with every hit of b, "
        "and a note says so; a key value a view or a note qualifies matches nothing), with "
        "each run's other selectors "
        "(columns a:<selector>, b:<selector>; the text table leaves out those that agree "
        "between the runs at every paired hit, value, no value and caveat alike, and a "
        "note names them; --json has them all); then the hits of each run whose key value "
        "the other run never had, or that have none (matched: only in run a / b). "
        "no_value_a/b and caveats_a/b are wide's, per run (the key's own included). A key "
        "whose values carry addresses gets a note; masked=1 matches them masked. --json "
        "gives the generated SQL too (`sql`)",
        lambda path, params: _join(path, params),
        {
            **_PAIR_PARAMS,
            "key": "Selector whose value links the runs, e.g. v, or several separated by "
            "commas, e.g. a,b, whose values together do (required)",
            "phase": "observed (default), after-set or returned",
            "masked": "1: match key values with addresses masked (no_addresses), for a "
            "pointer or a C string; any address then matches any other",
            "unchanged": "1: the text table keeps the selectors that agree between the runs "
            "at every paired hit (by default it leaves them out, and a note names them; "
            "--json always has them)",
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
# Each canned query in a few words, for `peeky queries --help` (eleventh evaluation: an
# agent passed set_effect a and b, since only the 20 KB schema said which takes which).
QUERY_SUMMARIES = {
    "runs": "the latest runs (20 unless count), newest first",
    "run": "every captured value of one run, in capture order",
    "hits": "each selector's observed and returned values by hit, with changed (NULL where "
    "unknown)",
    "compare": "every value of two runs side by side, paired by hit; first the status of a run "
    "that did not complete, and any requested interventions",
    "diff": "how two runs compare: outcome, then what differs (or may) in output, edit, source, "
    "line, arguments, interventions and captured values",
    "outputs": "arguments, requested interventions (with how they went, where recorded), "
    "outcome and output of two runs",
    "set_effect": "values before and after one run's interventions",
    "values": "one selector's distinct observed and returned values per program and line, "
    "across non-candidate runs",
    "wide": "one run, a row per hit and phase, a column per selector",
    "join": "two runs' hits paired by the values of one or more key selectors, then the "
    "unpaired hits",
    "unreadable": "the captures of one run that have no value, with unavailable and error",
    "failures": "the latest runs that did not complete (20 unless count), newest first",
}


def query_usage(name):
    """Canned query ``name`` with the parameters it takes: `diff [a] [b]`, `values selector`."""
    _, _, accepted, required = QUERIES[name]
    return " ".join([name, *(p if p in required else f"[{p}]" for p in accepted)])


def queries_help():
    """`peeky queries --help`'s list: each canned query, its parameters and what it answers."""
    usages = {name: query_usage(name) for name in QUERIES}
    width = max(map(len, usages.values())) + 2
    return "\n".join(
        [
            "canned queries, each with the --param names it takes ([optional]):",
            *(f"  {usages[name].ljust(width)}{QUERY_SUMMARIES[name]}" for name in QUERIES),
            "",
            "a and b pick two runs (default: the last two); run picks one (default: the newest).",
            "e.g. peeky queries set_effect --param run=3; peeky queries diff --param a=3 "
            "--param b=4",
            "`peeky queries` with no name describes each query and parameter in full.",
        ]
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


def _points_into(row, key="at_element_of", number="index"):
    """(at_element_of, element_index) of a probe row's `at_element_of` (#302), or with
    ``key`` "offset_from" and ``number`` "offset", (offset_from, element_offset); else
    NULLs."""
    into = row.get(key)
    if not isinstance(into, dict) or type(into.get(number)) is not int:
        return None, None
    base = into.get("selector")
    return (base, into[number]) if isinstance(base, str) and base else (None, None)


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
        # With the digest of every source the run read (a candidate's before its edit).
        "target_identity": _text(
            {**(result.get("target_identity") or {}), "inputs_sha256": result["inputs_sha256"]}
            if isinstance(result.get("inputs_sha256"), str)
            and isinstance(result.get("target_identity") or {}, dict)
            else result.get("target_identity")
        ),
        "edit": candidate.get("diff") if edited else None,
        "notes_kept": 1,
        # How each requested change went, in the order of sets.
        "set_outcomes": json.dumps(result["set_outcomes"])
        if isinstance(result.get("set_outcomes"), list)
        else None,
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
            "num, unavailable, error, view, identity, scope, note, instance, stop, "
            "element_size, at_element_of, element_index, offset_from, element_offset) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    row["stop"] if type(row.get("stop")) is int else None,
                    row["element_size"] if type(row.get("element_size")) is int else None,
                    *_points_into(row),
                    *_points_into(row, "offset_from", "offset"),
                )
                for row in result.get("rows") or []
            ],
        )
        connection.executemany(
            "INSERT INTO interventions (run_id, hit, name, before, after, line, capture_hit, "
            "function, instance, stop) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    change["stop"] if type(change.get("stop")) is int else None,
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


def baseline(path, run_id, source_sha256):
    """The newest run before ``run_id`` that is the same probe without its edit, to
    compare a candidate run with (seventh evaluation, finding 12): the same target,
    file, line, captures, program arguments and Python entry; the same build and the
    same sources (its target identity: compiler options, optimization, and the digest of
    every source and header it read, the candidate's before its edit); the `--at`
    file's unedited text (``source_sha256``); no edit, no `--set` or `--set-at`. None
    where none is, or the candidate's sources are not known."""
    found = query(
        path,
        "SELECT b.run_id FROM runs c JOIN runs b ON b.run_id < c.run_id AND b.target IS "
        "c.target AND b.language IS c.language AND b.program IS c.program AND b.line IS "
        "c.line AND b.capture IS c.capture AND b.args IS c.args AND b.entry IS c.entry AND "
        "b.target_identity = c.target_identity AND b.source_sha256 = :source AND "
        "b.edit IS NULL AND COALESCE(b.sets, '[]') = '[]' WHERE c.run_id = :run AND "
        "json_valid(c.target_identity) AND "
        "json_extract(c.target_identity, '$.inputs_sha256') IS NOT NULL "
        "ORDER BY b.run_id DESC LIMIT 1",
        {"run": run_id, "source": source_sha256},
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


def _reader(path, *, snapshot=False):
    """(connection, clock): a read-only connection to the store, bounded as queries are,
    whose SQL functions and progress handler stop at ``clock["deadline"]`` (each query
    sets its own). With ``snapshot``, it holds one read transaction from here on."""
    connection = _open_read_only(path)
    # Each value, each row's width and the whole result are bounded.
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MAX_COLUMNS)
    clock = {"deadline": time.monotonic() + QUERY_SECONDS}

    def no_addresses(value, language=None):
        # SQLite checks the deadline only between VM steps, never inside a Python
        # function: a query calling it many times per row checks here too.
        if time.monotonic() > clock["deadline"]:
            raise sqlite3.OperationalError("interrupted: query deadline")
        return _no_addresses(value, language)

    connection.create_function("no_addresses", 1, no_addresses, deterministic=True)
    connection.create_function("no_addresses", 2, no_addresses, deterministic=True)

    def bounded(function):
        def call(*values):
            if time.monotonic() > clock["deadline"]:
                raise sqlite3.OperationalError("interrupted: query deadline")
            return function(*values)

        return call

    connection.create_function(
        "first_difference", 2, bounded(_first_difference), deterministic=True
    )
    connection.create_function("output_window", 2, bounded(_output_window), deterministic=True)
    connection.create_function("short_function", 1, bounded(_short_function), deterministic=True)
    connection.create_function("object_ref", 1, bounded(_object_ref), deterministic=True)
    connection.create_function("set_outcome", 4, bounded(_set_outcome), deterministic=True)
    connection.create_function("address", 1, bounded(_address), deterministic=True)
    connection.create_function(
        "difference_position", 2, bounded(_difference_position), deterministic=True
    )
    if snapshot:
        try:
            # Before the authorizer, which allows reads only. The store keeps SQLite's
            # rollback journal: the first read takes a shared lock, held until the
            # connection closes, so no save commits meanwhile (it waits, as for any
            # reader), and every read sees the store as it was then.
            connection.execute("BEGIN")
            connection.execute("SELECT COUNT(*) FROM runs").fetchall()
        except BaseException:
            connection.close()
            raise
    connection.set_authorizer(_authorize)
    connection.set_progress_handler(lambda: time.monotonic() > clock["deadline"], 10000)
    return connection, clock


# The connection a canned query reads through (path, connection, clock): one read
# transaction for its rows, its notes and its text table (review 3 of PR #269: a probe
# that saved run 501 evicted run 1 between `diff`'s rows and its notes, and the
# alignment note vanished).
_SNAPSHOT = contextvars.ContextVar("peeky_evidence_snapshot", default=None)


@contextlib.contextmanager
def _snapshot(path):
    """Every ``query`` on ``path`` in this block reads one snapshot of the store."""
    path = Path(path).absolute()
    held = _SNAPSHOT.get()
    if held is not None and held[0] == path:
        yield
        return
    connection, clock = _reader(path, snapshot=True)
    token = _SNAPSHOT.set((path, connection, clock))
    try:
        yield
    finally:
        _SNAPSHOT.reset(token)
        # Closing ends the read transaction.
        connection.close()


def query(path, sql, params=None, *, limit=DEFAULT_ROWS, seconds=QUERY_SECONDS):
    """Run one read-only SQL statement over the store; rows are capped at ``limit``.
    Inside ``_snapshot``, it reads that snapshot."""
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("Evidence query failed: the SQL is empty")
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"Evidence query failed: --limit must be within 1..{MAX_ROWS}")
    held = _SNAPSHOT.get()
    if held is not None and held[0] == Path(path).absolute():
        _, connection, clock = held
        owned = False
    else:
        connection, clock = _reader(path)
        owned = True
    deadline = clock["deadline"] = time.monotonic() + seconds
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
        if owned:
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


_PHASES = ("observed", "after-set", "returned")
# Each run's cells: names whose value is NULL, with why, and names whose value something
# qualifies, with what: JSON objects, so no reason or selector text can be misread as a
# separator. NULL where there are none.
_NO_VALUE = "NULLIF(json_group_object(selector, reason) FILTER (WHERE reason IS NOT NULL), '{}')"
_CAVEATS = "NULLIF(json_group_object(selector, caveat) FILTER (WHERE caveat IS NOT NULL), '{}')"


def _cells(where):
    """SQL for one row per hit, phase and selector of the captures matching ``where``:
    ``value``; ``reason``, why it has none; ``caveat``, what qualifies a value (a view
    other than how it was read, else `note` where the row has a note); ``stop``, where
    its rows agree on one (`_one_stop`). A selector captured twice at a hit read the same
    both times (``_captured`` checks): one cell."""
    return (
        f"SELECT hit, phase, selector, MIN(line) AS line, {_one_stop()} AS stop, "
        "MAX(value) AS value,\n"
        "    CASE WHEN MAX(value) IS NULL THEN COALESCE(MAX(error), 'no value') END AS reason,\n"
        f"    MAX({_caveat()}) AS caveat\n"
        f"  FROM captures WHERE {where} GROUP BY hit, phase, selector"
    )


def _phase(params, default=None):
    phase = params.get("phase", default)
    if phase is not None and phase not in _PHASES:
        raise ValueError(
            f"Evidence query failed: phase is observed, after-set or returned, not {phase!r}"
        )
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
    # Which of the line's breakpoint locations each hit was at (#157), for a run whose
    # line has several in one function: NULL where its cells do not agree on one (#174).
    stops = query(
        path,
        "SELECT 1 AS n FROM captures WHERE run_id = :run AND stop IS NOT NULL LIMIT 1",
        {"run": run},
    )["rows"]
    fixed = {"hit", *(("stop",) if stops else ()), "phase", "line", "no_value", "caveats", "sets"}
    names = _columns(selectors, set(fixed), fixed=fixed)
    _too_wide(len(names) + len(fixed), run, "wide")
    where = f"run_id = {run}" + (f" AND phase = {_literal(phase)}" if phase else "")
    # The interventions made since the previous row's values were read (#137): at this
    # captured stop for its after-set row; else, at other stops after the previous hit
    # and before this one, for its observed row.
    sets = (
        "(SELECT NULLIF(json_group_array(name || ': ' || COALESCE(before, '?') || ' -> ' || "
        "COALESCE(after, '?') || ' (line ' || COALESCE(line, '?') || ', hit ' || hit || "
        "COALESCE(', stop ' || stop, '') || "
        "COALESCE(', in ' || COALESCE(instance, short_function(function)), '') || ')'), "
        f"'[]') FROM (SELECT * FROM interventions i WHERE i.run_id = {run} AND CASE WHEN "
        f"{_AT_CAPTURE} THEN w.phase = 'after-set' AND i.hit = w.hit ELSE w.phase = "
        "'observed' AND i.capture_hit = w.hit - 1 END ORDER BY i.rowid))"
    )
    sql = (
        f"SELECT w.*, {sets} AS sets FROM (\n"
        f"SELECT hit,{f' {_one_stop()} AS stop,' if stops else ''} phase, MIN(line) AS line,"
        f"{_picked(selectors, names, '  ')}\n"
        f"  {_NO_VALUE} AS no_value,\n  {_CAVEATS} AS caveats\n"
        f"FROM ({_cells(where)})\nGROUP BY hit, phase) w ORDER BY w.hit, "
        "CASE w.phase WHEN 'after-set' THEN 1 WHEN 'returned' THEN 2 ELSE 0 END"
    )
    return sql, {}, []


MAX_KEYS = 8


def _keys(text):
    """The selectors ``key`` names: one, or several separated by commas (#201), split
    only outside brackets, parentheses and quotes (`m["a,b"]` is one)."""
    keys, depth, quote, start = [], 0, None, 0
    for i, char in enumerate(text):
        if quote:
            quote = None if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            keys.append(text[start:i])
            start = i + 1
    keys = list(dict.fromkeys(key.strip() for key in [*keys, text[start:]]))
    if not all(keys):
        raise ValueError(
            f"Evidence query failed: key names a selector, or several separated by commas "
            f"(key=a,b), not {text!r}"
        )
    if len(keys) > MAX_KEYS:
        raise ValueError(f"Evidence query failed: key names at most {MAX_KEYS} selectors")
    return keys


def _join_layout(path, params):
    """`queries join`'s runs a and b, keys, phase, whether ``masked``, its fixed columns,
    the keys' column names, each side's (run, other selectors, their column names, key
    match expression) by side name, and the SQL that pairs two hits."""
    _require_runs(path, params)
    found = query(
        path,
        f"SELECT {_A} AS a, {_B} AS b, (SELECT language FROM runs WHERE run_id = {_A}) AS la, "
        f"(SELECT language FROM runs WHERE run_id = {_B}) AS lb",
        {"a": params.get("a"), "b": params.get("b")},
    )["rows"][0]
    a, b = found["a"], found["b"]
    keys, phase = _keys(params["key"]), _phase(params, "observed")
    for flag in ("masked", "unchanged"):
        if params.get(flag) not in (None, 0, 1):
            raise ValueError(f"Evidence query failed: {flag} is 1 (or 0)")
    masked = params.get("masked") == 1
    fixed = {"hit_a", "hit_b", "no_value_a", "no_value_b", "caveats_a", "caveats_b", "matched"}
    taken = set(fixed)
    key_names = _columns(keys, taken, fixed=fixed)
    sides = {}
    for name, run, language in (("a", a, found["la"]), ("b", b, found["lb"])):
        captured = _captured(path, run, phase)
        for key in keys:
            if key not in captured:
                raise ValueError(
                    f"Evidence query failed: run {run} captured no {key!r} ({phase}); it "
                    f"captured: {', '.join(captured[:20]) or 'nothing'}"
                )
        # Masked, each run's own key value is a column too: the masked text is the match.
        others = [selector for selector in captured if masked or selector not in keys]
        matched = "value" if not masked else f"no_addresses(value, {_literal(language)})"
        sides[name] = (run, others, _columns(others, taken, f"{name}:"), matched)
    same = " AND ".join(f"a.k{i} = b.k{i}" for i in range(len(keys)))
    return a, b, keys, phase, masked, fixed, key_names, sides, same


def _join(path, params):
    """(SQL, params, notes) of `queries join`: two runs' hits paired where ``key`` has
    the same value (stored text; with ``masked``, addresses masked), every pair where it
    repeats; then each run's unpaired hits. ``key`` may name several selectors (`a,b`):
    hits pair where each has the same value (#201)."""
    a, b, keys, phase, masked, fixed, key_names, sides, same = _join_layout(path, params)
    notes = []
    _too_wide(
        len(fixed) + len(keys) + len(sides["a"][1]) + len(sides["b"][1]), f"{a} and {b}", "join"
    )
    for key in keys if not masked else ():
        carried = query(
            path,
            "SELECT c.value FROM captures c JOIN runs r USING (run_id) WHERE c.run_id IN "
            "(:a, :b) AND c.selector = :key AND c.phase = :phase AND c.value IS NOT NULL "
            "AND no_addresses(c.value, r.language) IS NOT c.value LIMIT 1",
            {"a": a, "b": b, "key": key, "phase": phase},
        )["rows"]
        if carried:
            notes.append(
                f"add --param masked=1 to match {key}'s values with addresses masked, so that "
                f"any address matches any other: they carry addresses "
                f"({carried[0]['value'][:60]}), which differ between runs even where what they "
                "point at is equal, so such hits rarely pair"
            )

    def side(name):
        run, others, names, matched = sides[name]
        where = f"run_id = {run} AND phase = {_literal(phase)}"
        picked = "".join(f"{column}," for column in _key_cells(keys, matched))
        return (
            f"{name} AS (SELECT hit, {picked}{_picked(others, names, '    ')}\n"
            f"    {_NO_VALUE} AS no_value,\n"
            f"    {_CAVEATS} AS caveats\n  FROM ({_cells(where)}) GROUP BY hit)"
        )

    def columns(name, present):
        return [
            f"{name}.{_identifier(column)}" if present else f"NULL AS {_identifier(column)}"
            for column in sides[name][2]
        ]

    def row(present_a, present_b, matched):
        parts = [
            *(
                f"{'a' if present_a else 'b'}.k{i} AS {_identifier(key_name)}"
                for i, key_name in enumerate(key_names)
            ),
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
        f"{row(True, True, 'both')} FROM a JOIN b ON {same}\nUNION ALL\n"
        f"{row(True, False, 'only in run a')} FROM a "
        f"WHERE NOT EXISTS (SELECT 1 FROM b WHERE {same})\nUNION ALL\n"
        f"{row(False, True, 'only in run b')} FROM b "
        f"WHERE NOT EXISTS (SELECT 1 FROM a WHERE {same}))\n"
        "ORDER BY matched != 'both', hit_a IS NULL, hit_a, hit_b"
    )
    notes += _repeated(path, f"WITH {side('a')},\n{side('b')}", keys, a, b)
    return sql, {}, notes


def _key_cells(keys, matched):
    """A join side's key columns, k0, k1, ...: each key's value (``matched``: as stored,
    or masked). A key value a view or a note qualifies matches nothing (review of PR
    #209): the hit is unmatched, and caveats names why."""
    return [
        f"MAX(CASE WHEN selector = {_literal(key)} AND caveat IS NULL THEN {matched} END) AS k{i}"
        for i, key in enumerate(keys)
    ]


def _unchanged(path, keys, phase, sides, same):
    """(selectors, unpaired) for `join`: the selectors both runs show, keys aside, whose
    cell (its value, why it has none, and its caveat) is the same in run a's and run b's
    hit of every pair the key makes, which `join`'s table leaves out (tenth evaluation:
    runoff's join was 400 columns wide, each unchanged selector twice); none where no
    hits pair. ``unpaired`` counts the hits that pair with none, whose values of them go
    too."""
    shared = [s for s in sides["a"][1] if s in sides["b"][1] and s not in keys]
    if not shared or len(keys) + len(shared) + 1 > MAX_COLUMNS:
        return [], 0

    def side(name):
        run, _, _, matched = sides[name]
        where = f"run_id = {run} AND phase = {_literal(phase)}"
        cells = [
            f"MAX(CASE WHEN selector = {_literal(selector)} THEN json_array(value, reason, "
            f"caveat) END) AS s{i}"
            for i, selector in enumerate(shared)
        ]
        return (
            f"{name} AS (SELECT hit, {', '.join([*_key_cells(keys, matched), *cells])} "
            f"FROM ({_cells(where)}) GROUP BY hit)"
        )

    found = query(
        path,
        f"WITH {side('a')},\n{side('b')}\nSELECT "
        + "".join(f"MIN(a.s{i} IS b.s{i}) AS s{i}, " for i in range(len(shared)))
        + f"(SELECT COUNT(*) FROM a WHERE NOT EXISTS (SELECT 1 FROM b WHERE {same})) + "
        f"(SELECT COUNT(*) FROM b WHERE NOT EXISTS (SELECT 1 FROM a WHERE {same})) AS unpaired "
        f"FROM a JOIN b ON {same}",
    )["rows"][0]
    dropped = [selector for i, selector in enumerate(shared) if found[f"s{i}"] == 1]
    return dropped, found["unpaired"]


def _repeated(path, sides, keys, a, b):
    """A note where the key's values repeat in a run and the other run has them too, so
    that each such hit pairs with every hit of the other run that has them (#201:
    rechnung's `neu.bezeichnung`, 16 rows): naming the first, and how many rows they
    make, so that a selector can be added to the key."""
    columns = ", ".join(f"k{i}" for i in range(len(keys)))
    present = " AND ".join(f"k{i} IS NOT NULL" for i in range(len(keys)))
    groups = {}
    for name, run in (("a", a), ("b", b)):
        for row in query(
            path,
            f"{sides} SELECT {columns}, COUNT(*) AS n, group_concat(hit) AS hits FROM "
            f"(SELECT * FROM {name} ORDER BY hit) WHERE {present} GROUP BY {columns}",
            {},
            limit=MAX_ROWS,
        )["rows"]:
            value = tuple(row[f"k{i}"] for i in range(len(keys)))
            groups.setdefault(value, {})[run] = (row["n"], row["hits"])
    crossed = {
        value: seen
        for value, seen in groups.items()
        if len(seen) == 2 and (seen[a][0] > 1 or seen[b][0] > 1)
    }
    if not crossed:
        return []
    rows = sum(seen[a][0] * seen[b][0] for seen in crossed.values())
    value, seen = next(iter(crossed.items()))
    run = a if seen[a][0] > 1 else b
    hits = seen[run][1].split(",")
    if len(hits) > 5:
        hits = [*hits[:4], f"{len(hits) - 4} more"]
    shown = _listed([f"`{key}` {str(part)[:60]}" for key, part in zip(keys, value)])
    many = len(crossed) > 1
    return [
        f"the key repeats: add a selector that names the call to the key "
        f"(key={','.join(keys)},<selector>). Run {run}'s hits {_listed(hits)} "
        f"{'both' if len(hits) == 2 else 'all'} have {shown}, and such a value pairs every "
        f"hit that has it in one run with every one in the other ({rows} rows, from "
        f"{len(crossed)} value{'s' if many else ''})"
    ]


def _listed(items):
    """a; a and b; a, b and c."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _checked(name, params):
    """``params`` for canned query ``name``, checked before the store is read."""
    if name not in QUERIES:
        raise ValueError(
            f"Evidence query failed: no canned query {name!r}; one of: {', '.join(QUERIES)}"
        )
    _, _, accepted, required = QUERIES[name]
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
    return params


def prepare(path, name, params=None):
    """``params`` checked for canned query ``name``, with the runs it reads named: `run`
    (default: the newest), or `a` and `b` (default: the run before b, and the newest).
    Each is resolved once, so that the query, its notes and its text table read the same
    runs, though a probe may save a newer one meanwhile (review 2 of PR #269: `join`'s
    table compared runs 2 and 3 for a result of runs 1 and 2). A run that does not
    resolve stays unnamed, so the query says why."""
    params = _checked(name, params)
    accepted = QUERIES[name][2]
    with _snapshot(path):
        if name in _PAIRS:
            _require_runs(path, params)
        if {"a", "b"} <= set(accepted):
            named = {"a": _A, "b": _B}
        elif "run" in accepted:
            named = {"run": _RUN}
        else:
            return params
        if all(params.get(run) is not None for run in named):
            return params
        found = query(
            path,
            "SELECT " + ", ".join(f"{sql} AS {run}" for run, sql in named.items()),
            {**dict.fromkeys(named), **params},
        )["rows"][0]
    return {
        **params,
        **{run: found[run] for run in named if params.get(run) is None and found[run] is not None},
    }


def _present(path, runs):
    """Which of ``runs`` (run ids) the store holds."""
    if not runs:
        return []
    found = query(path, f"SELECT run_id FROM runs WHERE run_id IN ({', '.join(map(str, runs))})")[
        "rows"
    ]
    held = {row["run_id"] for row in found}
    return [run for run in runs if run in held]


# What a query checks after reading its rows, for the note where a run went meanwhile.
_CHECKS = {
    "diff": "alignment, labels and older-run warnings",
    "compare": "labels and older-run warnings",
}


def run_query(path, name, params=None, *, limit=DEFAULT_ROWS):
    """Run a canned query by name with optional parameters (see ``prepare``): its rows
    and notes from one snapshot of the store."""
    params = _checked(name, params)
    with _snapshot(path):
        return _run(path, name, params, limit)[0]


def canned(path, name, params=None, *, limit=DEFAULT_ROWS, table=False):
    """(result, text): ``run_query``'s result and, with ``table``, its text table
    (``query_table``), all read in one snapshot of the store."""
    params = _checked(name, params)
    with _snapshot(path):
        result, params = _run(path, name, params, limit)
        text = query_table(path, {**result, "query": name}, params) if table else None
    return result, text


def _run(path, name, params, limit):
    """(result, params): a canned query's rows and notes, and the params naming its
    runs. Run inside ``_snapshot``, which keeps every run it reads; should a run it
    named go before its notes are read all the same, a note says what they could not
    check, never nothing (review 3 of PR #269)."""
    params = prepare(path, name, params)
    _, sql, accepted, _ = QUERIES[name]
    bound = {**dict.fromkeys(accepted), **params}
    named = list(
        dict.fromkeys(params[run] for run in ("a", "b", "run") if type(params.get(run)) is int)
    )
    held = _present(path, named)
    if callable(sql):
        # Built for the runs named (a column per selector): shown, to refine with peeky sql.
        built, extra, notes = sql(path, params)
        result = {**query(path, built, extra, limit=limit), "sql": built}
    else:
        result, notes = query(path, sql, bound, limit=limit), []
    notes = _unmarked(path, name, bound) + notes
    if name in ("compare", "diff"):
        notes += _labels(path, bound)
    if name == "diff":
        unaligned, counted = _unaligned(path, bound)
        notes += unaligned
        if counted:
            notes += _after_first(path, bound)
    if gone := [run for run in held if run not in _present(path, held)]:
        one = len(gone) == 1
        notes.append(
            f"{_CHECKS.get(name, 'older-run warnings')} could not be checked: "
            f"run{'' if one else 's'} {_listed([str(run) for run in gone])} "
            f"{'was' if one else 'were'} removed after the rows were read; run the query again"
        )
    if "stop" in result["columns"] and all(row.get("stop") is None for row in result["rows"]):
        # No row was at one of several breakpoint locations of its line (Python and Java
        # never are): the column would be empty (eighth evaluation, L7).
        result = {
            **result,
            "columns": [column for column in result["columns"] if column != "stop"],
            "rows": [{k: v for k, v in row.items() if k != "stop"} for row in result["rows"]],
        }
    return ({**result, "notes": notes} if notes else result), params


def _labels(path, params):
    """The two runs' labels, where either has one (eighth evaluation, L17): a pair's rows
    name runs by number only."""
    rows = query(
        path,
        f"SELECT run_id, label FROM runs WHERE run_id IN ({_A}, {_B}) ORDER BY run_id",
        {"a": params.get("a"), "b": params.get("b")},
    )["rows"]
    if len(rows) != 2 or not any(row["label"] for row in rows):
        return []
    named = [
        f"run {row['run_id']} is {row['label']!r}"
        if row["label"]
        else f"run {row['run_id']} has no label"
        for row in rows
    ]
    return ["labels: " + "; ".join(named)]


# How `join` is named where hits may not pair: one selector, or several together (#201).
_JOIN_KEY = "--param key=<selector>[,<selector>…]"


def _unaligned(path, params):
    """For `diff`, which pairs hits by number: (notes, counted). A note where the two
    runs stopped at their line a different number of times (``counted``), so that their
    hits need not line up (a sort's comparator called on other pairs: seventh
    evaluation, finding 20), naming `join`, which pairs them by a value instead; or where
    they stopped as often but something says a hit may not be the same moment in both,
    or how they pair (#201)."""
    found = query(
        path,
        f"SELECT ra.run_id AS a, rb.run_id AS b, ra.hits AS ha, rb.hits AS hb FROM runs ra, "
        f"runs rb WHERE ra.run_id = {_A} AND rb.run_id = {_B} AND ra.program IS rb.program "
        "AND ra.line IS rb.line AND ra.hits_requested IS rb.hits_requested",
        {"a": params.get("a"), "b": params.get("b")},
    )["rows"]
    if not found or not found[0]["ha"] or not found[0]["hb"]:
        return [], False
    run = found[0]
    if run["ha"] == run["hb"]:
        apart = stops_apart(path, run["a"], run["b"])
        if apart is None:
            notes, suspect = _structure_apart(path, run)
            if suspect:
                return notes, False
            return notes + _unplaced_returns(path, run), False
        return [
            "pair the hits by a value instead: `peeky queries join "
            f"--param a={run['a']} --param b={run['b']} {_JOIN_KEY}`. At hit {apart['hit']}, "
            f"run {run['a']} was at {stop_text(apart['stop_a'])} of its line and run "
            f"{run['b']} at {stop_text(apart['stop_b'])}, so a hit need not be the same moment "
            "in both"
        ], False
    return [
        "pair the hits by a value instead: `peeky queries join "
        f"--param a={run['a']} --param b={run['b']} {_JOIN_KEY}`. Run {run['a']} stopped "
        f"{run['ha']} times at its line and run {run['b']} {run['hb']}, so a hit need not be "
        "the same moment in both, and pairing by hit number can show every later hit as a "
        "difference"
    ], True


def _after_first(path, params):
    """Where the runs stopped a different number of times (``_unaligned``): how many of
    `diff`'s rows at a hit come after the first hit with a real difference, where pairing
    by number may compare different calls (#201: 20 of rechnung's 22 rows were). Every
    row is kept. The first is among the real divergences only, by their tier
    (``_REAL_RANK``): a pointer whose address differs, an address-only value and a
    qualified one rank after them, and may be no difference (review of PR #294: a
    pointer's row at hit 1 made the note say hit 1, though the rows led with hit 2).
    Where no hit has a real difference, there is no note."""
    rows = query(path, _DIFF_RANKS, params, limit=MAX_ROWS)["rows"]
    hits = [row["hit"] for row in rows if row["rank"] != _ONLY_IN_RANK]
    real = [row["hit"] for row in rows if row["rank"] == _REAL_RANK]
    if not real:
        return []
    first = min(real)
    later = sum(1 for hit in hits if hit > first)
    if not later:
        return []
    return [
        f"the first difference at a hit is at hit {first}; {later} of the {len(hits)} rows "
        "at a hit come after it, where pairing by hit number may compare different calls: "
        "read those with `join`"
    ]


def _changes(path, a, b):
    """{run: [(name, after, where)]}: what each run's interventions (--set, --set-at)
    changed, after how many hits of its line had been captured (#201), and in which
    function (its instance where it has one). A value read at a later hit may follow
    from the change, whatever its name; one read at that hit or before cannot (a stop's
    observed rows come before its own changes)."""
    found = {a: [], b: []}
    for row in query(
        path,
        "SELECT run_id, name, MIN(COALESCE(capture_hit, hit, 0)) AS after, "
        "COALESCE(instance, short_function(function)) AS place FROM interventions WHERE "
        "run_id IN (:a, :b) GROUP BY run_id, name, function, instance ORDER BY MIN(rowid)",
        {"a": a, "b": b},
        limit=MAX_ROWS,
    )["rows"]:
        if isinstance(row["name"], str) and row["run_id"] in found:
            after = row["after"] if type(row["after"]) is int else 0
            found[row["run_id"]].append((row["name"], after, row["place"]))
    return found


def _quoted(selectors):
    """`a`, `a` and `b`, `a`, `b` and `c` (and N more, past three)."""
    named = [f"`{selector}`" for selector in selectors[:3]]
    if len(selectors) > 3:
        named.append(f"{len(selectors) - 3} more")
    return _listed(named)


def _edited(path, a, b):
    """Whether runs ``a`` and ``b`` ran different code: a candidate edit in one of them,
    or different edits, or (neither a candidate) different source text."""
    found = query(
        path,
        "SELECT ra.edit IS NOT rb.edit OR (ra.edit IS NULL AND rb.edit IS NULL AND "
        "ra.source_sha256 IS NOT NULL AND rb.source_sha256 IS NOT NULL AND "
        "ra.source_sha256 != rb.source_sha256) AS edited FROM runs ra, runs rb "
        "WHERE ra.run_id = :a AND rb.run_id = :b",
        {"a": a, "b": b},
    )["rows"]
    return bool(found and found[0]["edited"])


def _structure_apart(path, run):
    """(notes, suspect) where two runs stopped as often (eighth evaluation, M6; #201).

    Notes where their hits may not be the same moments, so that pairing by hit number
    may compare different calls (``suspect``), the first found of:

    - where each run stopped (function, instance, line) does not pair one to one across
      hits (a renamed function, or lines an edit moved, pair consistently);
    - a selector's values, addresses and Java object numbers masked, line up better
      other than hit by hit (`[A, B, C]` against `[A, C, D]`; the same values in another
      order), each selector on its own: where others agree between the runs at every
      hit, the note says both, and that the values do not settle it;
    - the hits' values differ at most of them, so they cannot show that the hits pair.

    Only plain values tell (#201): a value a view or a note qualifies is no evidence
    either way. A value that differs between the runs after a change, an edit (in either
    run: from the first hit) or an intervention (after the hits captured before the
    first), may follow from it (tenth evaluation: debounce's reset count moved
    `db.stable`'s values, which then lined up better at other hits). Such a selector is
    left out from then on of what agrees and of the hits that differ, and the note names
    it; its values before still tell. Where its values line up better at other hits,
    the note says so, and that the change may account for it: that still vetoes saying
    the hits line up (review of PR #269).

    Where nothing above is found and some values agree between the runs at every hit,
    the note says what agrees, and what pairing by number amounts to: where they are
    unique across the hits, what `join` by them gives. Never that the hits line up
    (review of PR #308: `i` 1 to 4 in both runs, the first two moments swapped): Peeky
    cannot tell whether a value names the call. `join` is named only where some captured
    selectors together tell each run's hits apart, so that it could pair them.

    Each note leads with its verdict on a line of its own (``_note``; eleventh
    evaluation: three agents could not find it in one long sentence): `hits pair by`,
    `hits pair as far as`, or `hits may not line up:`. Each detail follows on its own
    line."""
    rows = query(
        path,
        "SELECT c.run_id, c.hit, c.phase, c.line, c.function, c.instance, c.selector, "
        f"c.value, {_caveat('c')} AS caveat, r.language FROM captures c JOIN runs r ON "
        "r.run_id = c.run_id WHERE c.run_id IN (:a, :b) ORDER BY c.run_id, c.hit, c.rowid",
        {"a": run["a"], "b": run["b"]},
        limit=MAX_ROWS,
    )["rows"]
    a, b = run["a"], run["b"]
    changes, edited = _changes(path, a, b), _edited(path, a, b)
    # Values read after this many hits may follow from a change.
    since = 0 if edited else min((n for r in (a, b) for _, n, _ in changes[r]), default=None)
    places, values, captured = {a: {}, b: {}}, {a: {}, b: {}}, {a: [], b: []}
    # Every value as read, of any phase: only whether anything differs at all. And the
    # observed values that cannot show alignment: qualified (in no check at all), or
    # differing after a change (below).
    cells, untold = {a: {}, b: {}}, {a: set(), b: set()}
    for row in rows:
        here = row["run_id"]
        masked = None if row["value"] is None else _no_addresses(row["value"], row["language"])
        cells[here][(row["hit"], row["phase"], row["selector"])] = masked
        if row["phase"] != "observed":
            continue
        places[here].setdefault(row["hit"], (row["function"], row["instance"], row["line"]))
        if row["selector"] not in captured[here]:
            captured[here].append(row["selector"])
        if masked is None:
            continue
        if row["caveat"] is not None:
            untold[here].add((row["selector"], row["hit"]))
            continue
        values[here].setdefault(row["selector"], {})[row["hit"]] = (masked, row["value"])
    common = sorted(set(places[a]) & set(places[b]))
    selectors = [selector for selector in captured[a] if selector in captured[b]]
    missing, skipped = object(), object()

    def seen(run, selector, hit):
        if (selector, hit) in untold[run]:
            return skipped
        return values[run].get(selector, {}).get(hit, (missing,))[0]

    def stored(run, selector, hit):
        """The value as stored: what `join` matches without `--param masked=1`."""
        return values[run].get(selector, {}).get(hit, (missing, missing))[1]

    def apart(chosen):
        """Whether ``chosen``'s values, addresses masked, tell each run's hits apart."""
        return bool(chosen) and all(
            len({tuple(seen(r, s, hit) for s in chosen) for hit in common}) == len(common)
            for r in (a, b)
        )

    # Whether `join` could pair the hits: the plain values of the selectors both runs
    # read at every hit tell each run's hits apart, taken together (tenth evaluation:
    # debounce's `settle` has no value that names its call, so `join` cannot help).
    # Where only values that carry addresses do, `join` matches them only with
    # `--param masked=1` (review 2 of PR #308: each run's addresses differ).
    plain = [
        s
        for s in selectors
        if all(seen(r, s, hit) not in (missing, skipped) for r in (a, b) for hit in common)
    ]
    carries = {
        s
        for s in plain
        if any(seen(r, s, hit) != stored(r, s, hit) for r in (a, b) for hit in common)
    }
    keyable = apart(plain)
    join_masked = keyable and not apart([s for s in plain if s not in carries])
    pair_by = (
        "pair hits by a value that names the call and that the edit leaves alone (an "
        "argument, an index)"
        + (
            "; the values that tell the hits apart carry addresses, so match them masked"
            if join_masked
            else ""
        )
        + f": `peeky queries join --param a={a} --param b={b} {_JOIN_KEY}"
        + (" --param masked=1`" if join_masked else "`")
    )
    join = (
        f"if they don't, {pair_by}"
        if keyable
        else "no captured selector tells the hits apart, so `queries join` could not pair them"
    )
    # Each place of one run pairs with one place of the other, and the pair's first hit.
    forward, backward = {}, {}
    for hit in common:
        here_a, here_b = places[a][hit], places[b][hit]
        if (
            forward.get(here_a, (here_b,))[0] == here_b
            and backward.get(here_b, (here_a,))[0] == here_a
        ):
            forward.setdefault(here_a, (here_b, hit))
            backward.setdefault(here_b, (here_a, hit))
            continue
        if here_a in forward:
            then, earlier = (here_a, forward[here_a][0]), forward[here_a][1]
        else:
            then, earlier = (backward[here_b][0], here_b), backward[here_b][1]
        return [
            _note(
                "hits may not line up: where the runs stopped does not pair the same way at "
                "every hit",
                f"at hit {hit} run {a} stopped {_place(here_a)} and run {b} {_place(here_b)}",
                f"but at hit {earlier} run {a} {_place(then[0])} paired with run {b} "
                f"{_place(then[1])}",
                join,
            )
        ], True

    def unlike(selector, hit):
        """Read differently at ``hit``, both values telling (no value is one)."""
        one, other = seen(a, selector, hit), seen(b, selector, hit)
        return skipped not in (one, other) and one != other

    # Every selector's plain values as read, before any is left out (review of PR #269:
    # a value a change may have moved still vetoes saying the hits line up).
    whole = {r: {selector: dict(seen) for selector, seen in values[r].items()} for r in (a, b)}
    # A selector that differs after a change may differ because of it: from then on
    # its values tell nothing of how the hits pair.
    left_out = [
        selector
        for selector in selectors
        if since is not None and any(unlike(selector, hit) for hit in common if hit > since)
    ]
    for selector in left_out:
        for r in (a, b):
            for hit in [hit for hit in values[r].get(selector, {}) if hit > since]:
                del values[r][selector][hit]
                untold[r].add((selector, hit))
    # Plain at every hit in both runs, and alike; `shown` those that vary.
    agreeing = [
        selector
        for selector in selectors
        if all(
            seen(a, selector, hit) not in (missing, skipped)
            and seen(a, selector, hit) == seen(b, selector, hit)
            for hit in common
        )
    ]
    shown = [s for s in agreeing if len({seen(a, s, hit) for hit in common}) > 1]
    # Alike only once addresses are masked (review 2 of PR #308: a C++ string reference's
    # address, then its text): the note says so, and names `join --param masked=1`.
    masked_only = {
        s for s in agreeing if any(stored(a, s, hit) != stored(b, s, hit) for hit in common)
    }
    alike_masked = bool(set(shown) & masked_only)

    def moved_in(found, among):
        """(selector, what it saw) for the first of ``among`` whose values in ``found``
        line up better other than hit by hit, else None."""
        for selector in among:
            seen_a, seen_b = found[a].get(selector), found[b].get(selector)
            if not seen_a or not seen_b:
                continue
            hits_a, hits_b = sorted(seen_a), sorted(seen_b)
            order_a = [seen_a[hit][0] for hit in hits_a]
            order_b = [seen_b[hit][0] for hit in hits_b]
            if len(set(order_a)) < 2 or len(set(order_b)) < 2:
                continue
            same = sum(1 for hit in set(hits_a) & set(hits_b) if seen_a[hit][0] == seen_b[hit][0])
            pairs = _aligned(order_a, order_b)
            if len(pairs) <= same:
                continue
            i, j = next((i, j) for i, j in pairs if hits_a[i] != hits_b[j])
            hit_a, hit_b = hits_a[i], hits_b[j]
            if order_a != order_b and sorted(order_a) == sorted(order_b):
                text = f"runs {a} and {b} saw the same values of `{selector}` in another order"
            else:
                text = f"runs {a} and {b} saw `{selector}`'s values at other hits"
            return selector, (
                f"{text} (run {b}'s hit {hit_b} matches run {a}'s hit {hit_a}: {seen_b[hit_b][1]})"
            )
        return None

    # Every selector on its own, whatever others say (review of PR #209: a value alike
    # at every hit by chance must not hide another pairing that a differing selector
    # suggests). First on the values no change can have moved.
    moved = moved_in(values, selectors)
    if moved and shown:
        return [
            _note(
                "hits may not line up: the values do not settle how they pair",
                _agrees(shown, len(common), masked=alike_masked),
                f"but {moved[1]}",
                join,
            )
        ], True
    if moved:
        return [_note(f"hits may not line up: {_REORDER}", moved[1], join)], True
    # Then on those left out: the change may account for it, but it still vetoes saying
    # the hits line up (review of PR #269: `call` A, B, C against B, A, C after an edit,
    # beside `key` 1, 2, 3 in both, gave "the hits line up by `key`").
    hedged = moved_in(whole, left_out) if left_out else None
    both = {key[2] for key in cells[a]} & {key[2] for key in cells[b]}
    differs = any(
        cells[a].get(key, missing) != cells[b].get(key, missing)
        for key in set(cells[a]) | set(cells[b])
        if key[2] in both
    )
    # Each selector's value at a hit, by name: `left=A, right=B` against `left=B, right=A`
    # differs (review 2 of PR #189). Only selectors both runs captured (`diff` lists the
    # others once), plain and not left out; one with no such value at a hit is
    # `missing`, which no text equals. Checked before anything agreeing is said (review 2
    # of PR #209: `key` 1, 2 in both runs said nothing about `call` A, B against C, D).
    differ = [hit for hit in common if any(unlike(selector, hit) for selector in selectors)]
    if len(differ) >= 2 and 2 * len(differ) > len(common):
        calls = f"if the edit changed which calls reach the line, {pair_by}" if keyable else join
        if shown:
            apart = [
                selector for selector in selectors if any(unlike(selector, hit) for hit in common)
            ]
            return [
                _note(
                    f"hits may not line up: {_quoted(apart)} "
                    f"{'differ' if len(apart) > 1 else 'differs'} at {len(differ)} of the "
                    f"{len(common)} hits",
                    f"{_agrees(shown, len(common), masked=alike_masked)}, but that does not "
                    "show that a hit is the same moment in both",
                    calls,
                )
            ], True
        return [
            _note(
                f"hits may not line up: the runs' values differ at {len(differ)} of "
                f"{len(common)} hits",
                "so they cannot show that a hit is the same moment in both",
                calls,
            )
        ], True
    if len(common) < 2 or not differs:
        return [], False
    rows = [[seen(a, s, hit) for s in shown] for hit in common]
    keys = _join_key(shown, rows)
    paired = keys and _paired(keys, len(common), (a, b), bool(set(keys) & masked_only))
    # Without a key: the values read alike at some hits, or only more of them together
    # than `join` takes are unique (review 3 of PR #308).
    loose = agreed = None
    if shown and not keys:
        wide = len({tuple(row) for row in rows}) == len(rows)
        one = len(shown) == 1
        why = (
            f"no `queries join` key of {MAX_KEYS} selectors or fewer among them was found "
            "unique across these hits"
            if wide
            else f"{'it reads' if one else 'they read'} alike at some hits, which could still "
            "trade places"
        )
        loose = [
            f"hits pair as far as {_quoted(shown)} {'shows' if one else 'show'}: "
            f"{_agrees(shown, len(common), pronoun=True, apart=wide, masked=alike_masked)}",
            f"but {why}",
            "pairing by hit number agrees with their values, but that does not prove the hits "
            "are the same moments"
            if wide
            else None,
        ]
        agreed = f"{_agrees(shown, len(common), masked=alike_masked)}, but {why}"
    if left_out:
        return [
            _after_change(shown, paired, loose, agreed, changes, edited, left_out, hedged, join)
        ], hedged is not None
    if not shown:
        return [], False
    differing = [
        selector
        for selector in selectors
        if any(
            cells[a].get((hit, "observed", selector), missing)
            != cells[b].get((hit, "observed", selector), missing)
            for hit in common
        )
    ]
    return [_agreement(paired or loose, changes, differing, (a, b))], False


def _cause(changes, edited, detail=True):
    """`the edit`, `the interventions (run 2's on `x` in main)`, or both: what changed
    between two runs, with what each run's interventions changed where ``detail``."""
    reached = [run for run in changes if changes[run]]
    causes = ["the edit"] if edited else []
    if reached:
        causes.append(
            f"the interventions ({_interventions(changes, reached)})"
            if detail
            else "the interventions"
        )
    return " and ".join(causes)


def _after_change(shown, paired, loose, agreed, changes, edited, left_out, hedged, join):
    """The note where a change left selectors out (tenth evaluation): what agrees, never
    that the hits line up (review of PR #269); what was left out, and why; and a sign
    that the hits may not line up among those left out (``hedged``: the selector and
    what it saw), which the change may account for. Where some values that agree are
    unique across the hits, the verdict is that the hits pair by them, and such a sign
    comes last (eleventh evaluation: grades' `quiz` 88 at another hit, beside `name`):
    it does not settle whether the calls were reordered, and is never graded (review of
    PR #308: a key may be a counter, which is no evidence against a reorder). Else the
    sign is the verdict. ``paired`` is `_paired`'s lines where some values that agree are
    unique across the hits, as a `join` key; else ``loose`` is the verdict that they pair
    as far as ``shown`` show, and ``agreed`` says what agrees and why it is no key."""
    it = "them" if any(changes.values()) else "it"
    others = [selector for selector in left_out if not hedged or selector != hedged[0]]

    def leaves(detail):
        if not others:
            return None
        one = len(others) == 1
        return (
            f"{_quoted(others)} {'differs' if one else 'differ'} after "
            f"{_cause(changes, edited, detail)} and may follow from {it}, so this check leaves "
            f"{'it' if one else 'them'} out"
        )

    if not shown and not hedged:
        one = len(left_out) == 1
        return _note(
            "hits may not line up: the values cannot show whether they do",
            f"{_quoted(left_out)} {'differs' if one else 'differ'} after "
            f"{_cause(changes, edited)} and may follow from {it}",
            "no other selector both agrees between the runs and changes from hit to hit",
        )
    if paired:
        lines = [*paired, leaves(True)]
        if hedged:
            selector, saw = hedged
            lines += [
                saw,
                "that alternate alignment does not settle whether the calls were reordered: "
                f"`{selector}` may differ because of {_cause(changes, edited, not others)}, "
                "or match by chance",
            ]
        return _note(*lines)
    if not hedged:
        return _note(*loose, leaves(True))
    selector, saw = hedged
    return _note(
        f"hits may not line up: {_REORDER}",
        saw,
        f"`{selector}` may differ because of {_cause(changes, edited)}, so this does not prove "
        "a reorder",
        agreed if shown else None,
        leaves(False),
        join,
    )


_REORDER = "the calls may have come in another order or number"


def _note(verdict, *details):
    """An alignment note: its verdict, then each detail (none where None) on a line of its
    own after `- ` (eleventh evaluation: three agents could not find the verdict in one
    long sentence). The text table indents the details under `note:`."""
    return "\n".join([verdict, *(f"- {detail}" for detail in details if detail)])


def _interventions(changes, reached):
    """`run 5's on `total` in main`: what each run's interventions changed, and in which
    function (a name alone is not the variable: `x` in `helper` is not `main`'s)."""
    named = []
    for run in reached:
        made = dict.fromkeys(
            f"`{name}`" + (f" in {place}" if place else "") for name, _, place in changes[run]
        )
        shown = [*list(made)[:3], *([f"{len(made) - 3} more"] if len(made) > 3 else [])]
        named.append(f"run {run}'s on {_listed(shown)}")
    return _listed(named)


def _agrees(selectors, count, *, pronoun=False, apart=False, masked=False):
    """`` `i` agrees between the runs at each of the 4 hits`` (or `it agrees …`), with
    ``masked`` ` with addresses masked` (alike only so: review 2 of PR #308), and with
    ``apart`` ` and is unique across these hits` (`and together are unique …`)."""
    one = len(selectors) == 1
    subject = ("it" if one else "they") if pronoun else _quoted(selectors)
    text = f"{subject} {'agrees' if one else 'agree'} between the runs at each of the {count} hits"
    if masked:
        text += " with addresses masked"
    if apart:
        text += f" and {'is' if one else 'together are'} unique across these hits"
    return text


def _paired(keys, count, runs, masked):
    """The verdict where ``keys`` agree between the runs at every hit and are unique across
    them, and what it amounts to: hit-number pairing is what `join` by them gives, which
    does not prove the hits are the same moments (review of PR #308: Peeky cannot tell
    whether a value names the call, so it never says the hits line up). The `join` is
    given whole, with `--param masked=1` where a key agrees only with addresses masked:
    plain `join` compares the stored text (review 2 of PR #308)."""
    a, b = runs
    command = (
        f"peeky queries join --param a={a} --param b={b} --param "
        f"{shlex.quote('key=' + ','.join(keys))}" + (" --param masked=1" if masked else "")
    )
    return [
        f"hits pair by {_quoted(keys)}: "
        f"{_agrees(keys, count, pronoun=True, apart=True, masked=masked)}",
        f"pairing by hit number pairs them as `{command}` would, but that does not prove the "
        "hits are the same moments",
    ]


def _agreement(verdict, changes, differing, runs):
    """What agrees where nothing suggests another pairing and nothing was left out
    (#201), as `_structure_apart` says it: its ``verdict`` lines, that the hits pair by
    what agrees (a `join` key) or as far as it shows, never that they line up (review of
    PR #308); then, with no intervention in either run, that no single selector suggests
    another pairing; else what differs (``differing``, as read) and which interventions
    there were."""
    single = "no single selector's values line up better at other hits"
    reached = [run for run in runs if changes[run]]
    if not reached:
        # Selectors are checked one at a time: two that differ could line up better
        # together (review 3 of PR #209), which is not checked.
        return _note(
            *verdict,
            single,
            f"{_quoted(differing)} differ, and how they line up together was not checked"
            if len(differing) > 1
            else None,
        )
    # After an intervention, what follows from it is unknown (review of PR #209): what
    # agrees, what does not, and no more.
    return _note(
        *verdict,
        single,
        f"{_quoted(differing)} {'differs' if len(differing) == 1 else 'differ'}"
        if differing
        else None,
        f"the interventions ({_interventions(changes, reached)}) can change other values and "
        "which calls reach the line, so this does not show that each hit is the same moment in "
        "both",
    )


def _join_key(selectors, rows):
    """The fewest of ``selectors`` (in their order) whose values, ``rows`` (one list per
    hit, in ``selectors`` order), together are unique across the hits, as a `queries
    join` key: at most ``MAX_KEYS`` of them, as `join` takes no more (review 3 of PR
    #308: nine named together made the command fail). None where all of them together
    are not unique, or where no such key is found.

    One that is unique alone is the first such. Else each is chosen in turn to tell the
    most hits apart, the first of equals; then smaller sets are tried, in order, while
    the work stays within ``_KEY_SEARCH`` values read."""
    if not rows or not selectors or len({tuple(row) for row in rows}) < len(rows):
        return None
    columns = [tuple(row[i] for row in rows) for i in range(len(selectors))]

    def unique(chosen):
        return len(set(zip(*(columns[i] for i in chosen)))) == len(rows)

    chosen = []
    while not chosen or not unique(chosen):
        chosen.append(
            max(
                (i for i in range(len(selectors)) if i not in chosen),
                key=lambda i: len(set(zip(*(columns[j] for j in [*chosen, i])))),
            )
        )
    budget = _KEY_SEARCH // len(rows)
    for size in range(2, min(len(chosen), MAX_KEYS + 1)):
        for smaller in itertools.combinations(range(len(selectors)), size):
            budget -= size
            if budget < 0:
                break
            if unique(smaller):
                return [selectors[i] for i in smaller]
    return [selectors[i] for i in sorted(chosen)] if len(chosen) <= MAX_KEYS else None


# How many values `_join_key` may read trying smaller keys.
_KEY_SEARCH = 2_000_000


def _place(place):
    """Where a stop was, for a note: `in f at line 24`, an instance by its label."""
    function, instance, line = place
    return f"in {instance or function} at line {line}"


def _aligned(a, b):
    """A longest common subsequence of sequences ``a`` and ``b``: their index pairs."""
    lengths = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) - 1, -1, -1):
        for j in range(len(b) - 1, -1, -1):
            lengths[i][j] = (
                lengths[i + 1][j + 1] + 1
                if a[i] == b[j]
                else max(lengths[i + 1][j], lengths[i][j + 1])
            )
    pairs, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            pairs.append((i, j))
            i, j = i + 1, j + 1
        elif lengths[i + 1][j] >= lengths[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _unplaced_returns(path, run):
    """A note where a run's line stops several times per pass (#157's warning) and a
    `(return)` row (#155) has no stop, and its hit's other rows give none, or disagree
    (`--capture return` alone, before schema 15 gave a `(return)` row its hit's stop,
    #174; no stop recorded; contradictory evidence): pairing them by hit number cannot
    be checked."""
    found = query(
        path,
        "SELECT r.run_id FROM runs r WHERE r.run_id IN (:a, :b) AND r.warnings LIKE "
        "'%times per pass%' AND EXISTS (SELECT 1 FROM captures c WHERE c.run_id = r.run_id "
        f"AND c.phase = 'returned' AND c.stop IS NULL AND {_hits_stop('c')} IS NULL) "
        "ORDER BY r.run_id LIMIT 1",
        {"a": run["a"], "b": run["b"]},
    )["rows"]
    if not found:
        return []
    return [
        "capture a selector with `return` to pair the hits by stop, or pair them by a value: "
        f"`peeky queries join --param a={run['a']} --param b={run['b']} --param "
        f"key=<selector>`. Run {found[0]['run_id']}'s line stops several times per pass, and "
        "its `(return)` rows do not say at which stop their call began, so a hit need not be "
        "the same call in both runs"
    ]


def stops_apart(path, a, b):
    """{hit, stop_a, stop_b} for the first hit both runs ``a`` and ``b`` captured whose
    stop labels (#157: which of the line's breakpoint locations it was at) differ, a
    label missing in one included (a run saved before Peeky labelled stops), else None."""
    found = query(
        path,
        "SELECT o.hit, x.stop AS stop_a, o.stop AS stop_b FROM captures o JOIN captures x "
        "ON x.run_id = :a AND x.hit = o.hit AND x.phase = 'observed' WHERE o.run_id = :b "
        "AND o.phase = 'observed' AND x.stop IS NOT o.stop ORDER BY o.hit LIMIT 1",
        {"a": a, "b": b},
    )["rows"]
    return found[0] if found else None


def stop_text(stop):
    """`stop 2`, or for a hit with no label (a line with one breakpoint location, or a
    run saved before Peeky labelled stops) `an unlabelled stop`."""
    return f"stop {stop}" if stop is not None else "an unlabelled stop"


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
        "joins": "captures, interventions and pauses join runs on run_id. The evidence view "
        "has some of the run's columns (label, program, status, args, sets, language and a "
        "few more); the others, such as output, error_code, seconds and warnings, need a "
        "join with runs",
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
            "address(text)": "The first `0x` token outside quoted text (1 to 16 hex digits), "
            "as an INTEGER, else NULL: none, a zero one (a null pointer), a string's text, a "
            "Java object and a Python identity (`[id 6]`) give NULL. It is lexical, not "
            "typed: call it on a pointer's own selector (`h.p`), since in a preview (`{x = "
            "0x1234567890, p = 0x...}`) it reads the first hex field. For a pointer it is the "
            "address the pointer holds; in LLDB's `T @ 0x...` it is where the value itself is "
            "stored. Addresses differ between runs: join runs' observed rows on each address "
            "minus a base, the same element's address in each run (COOKBOOK 4.8). From 2^63 on "
            "it is the two's complement, a negative INTEGER: printf('0x%x', address(value)) "
            "gives its hex back (and 0x0 for NULL)",
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


# The columns a canned query compares, each of a pair shown in text from just before
# where it first differs from the other (tenth evaluation: `diff` cut `…{'text':
# 'Café', ...` against `…'lev...`, before the difference). `--json` keeps them whole.
_PAIRED = {
    "compare": {"value_a": "value_b", "value_b": "value_a"},
    "diff": {"value_a": "value_b", "value_b": "value_a"},
    "set_effect": {"observed": "after_set", "after_set": "observed"},
}
# Characters shown before a long value's first difference.
_LEAD = 40
# A set_effect change's values in text: each at most this long, and from this many
# characters before where they differ.
_CHANGE_WIDTH, _CHANGE_LEAD = 50, 30


def _window(value, other, width, lead=_LEAD):
    """``value`` in at most ``width`` characters, with `…` at each cut. Where the pair
    differs and the usual cut (from the start) of the longer would hide where they first
    differ, or show little after that, the window starts ``lead`` characters before that
    place: one offset for both values, so their windows line up."""
    start = 0
    if other is not None and other != value and max(len(value), len(other)) > width:
        first = _first_difference(value, other)
        if first + lead // 2 > width - 1:
            start = first - lead
    if not start:
        return value if len(value) <= width else value[: width - 1] + "…"
    if len(value) - start <= width - 1:
        return "…" + value[start:]
    return "…" + value[start : start + width - 2] + "…"


def _sets_text(value, other=None):
    """A `diff` or `compare` `(sets)` cell in text: how many changes, one of them, and `…`
    for the others (tenth evaluation: six `--set-at`s made every line of the table 230
    columns wide). The one shown is the first that differs from the ``other`` run's, so
    that a difference in a later change's outcome shows (review of PR #278); else the
    first. Where one is a `LINE@all` (#265), made at every stop of its line, they are
    counted as requests. None where the cell is not such a list."""

    def parsed(text):
        try:
            sets = json.loads(text)
        except (TypeError, ValueError):
            return None
        if not isinstance(sets, list) or not all(
            isinstance(item, list) and len(item) == 3 for item in sets
        ):
            return None
        return sets

    sets, others = parsed(value), parsed(other) or []
    if sets is None:
        return None
    if not sets:
        return "none"
    index = next((i for i, item in enumerate(sets) if i >= len(others) or others[i] != item), 0)
    name, set_value, where = (str(part) for part in sets[index])
    if len(set_value) > _CHANGE_WIDTH:
        set_value = set_value[: _CHANGE_WIDTH - 1] + "…"
    many = len(sets) > 1
    noun = "request" if any(re.search(r"@all\b", str(item[2])) for item in sets) else "change"
    return (
        f"{len(sets)} {noun}{'s' if many else ''}: "
        + ("…, " if index else "")
        + f"{name}={set_value} ({where})"
        + (", …" if index < len(sets) - 1 else "")
    )


def _change_text(change, shown):
    """``name: before -> after``, each value windowed against the other."""
    before, after = shown(change["before"]), shown(change["after"])
    return (
        f"{change['name']}: {_window(before, after, _CHANGE_WIDTH, _CHANGE_LEAD)} -> "
        f"{_window(after, before, _CHANGE_WIDTH, _CHANGE_LEAD)}"
    )


def _changes_text(row, changes, shown):
    """A set_effect row's `interventions` cell with each change's values from just before
    where they differ; None where ``changes`` (the run's, in the order made) do not
    give the cell's text, which then shows as stored. The cell lists the changes at the
    row's stop (its hit and line), or one change made at a stop with no capture."""
    cell = row.get("interventions")
    if cell is None:
        return None
    here = [
        change
        for change in changes
        if change["hit"] == row.get("hit")
        and change["line"] == row.get("line")
        and None not in (change["name"], change["before"], change["after"])
    ]
    pieces = [f"{c['name']}: {c['before']} -> {c['after']}" for c in here]
    if cell == "; ".join(pieces):
        made = here
    else:
        made = [change for change, piece in zip(here, pieces) if piece == cell][:1]
    return "; ".join(_change_text(change, shown) for change in made) if made else None


def table(result, width=120, changes=None):
    """A plain-text table of a query result. A `function` column shows each frame's
    short name (see ``names.function_labels``), with a note: the result keeps it whole.
    For a canned query (``result["query"]``) that compares values, a long value shows
    from just before where it first differs from its pair (`_PAIRED`); `diff`'s
    `(sets)` row shows how many changes and the first; set_effect's `interventions`
    shows each change that way, given the run's ``changes``. Their `note` is never cut."""
    from peeky.names import function_labels, shortened_note

    def shown(value):
        return "NULL" if value is None else str(value).replace("\n", "\\n")

    def text(value):
        value = shown(value)
        return value if len(value) <= width else value[: width - 1] + "…"

    columns = result["columns"]
    query_name = result.get("query")
    paired = _PAIRED.get(query_name, {})
    shortened = {}
    if "function" in columns:
        labels = function_labels(row["function"] for row in result["rows"])
        shortened = {name: label for name, label in labels.items() if label != name}

    def cell(row, column):
        value = row[column]
        if column == "function":
            return text(shortened.get(value, value))
        if query_name in ("diff", "compare") and row.get("selector") == "(sets)":
            if column in paired and (summary := _sets_text(value, row.get(paired[column]))):
                return text(summary)
        if column in paired and value is not None and row.get(paired[column]) is not None:
            return _window(shown(value), shown(row[paired[column]]), width)
        if query_name == "set_effect" and column == "interventions" and changes:
            if (made := _changes_text(row, changes, shown)) is not None:
                return text(made)
        if query_name in ("diff", "compare") and column == "note":
            # Whole: it says what to do last (a pointer's `address()` and COOKBOOK 4.8),
            # and as the last column it widens only its own line.
            return shown(value)
        return text(value)

    cells = [[cell(row, column) for column in columns] for row in result["rows"]]
    widths = [max(len(item[i]) for item in [columns, *cells]) for i in range(len(columns))]
    lines = [
        "  ".join(c.ljust(w) for c, w in zip(item, widths)).rstrip() for item in [columns, *cells]
    ]
    count = result["row_count"]
    if note := shortened_note(shortened):
        lines.append(note)
    # A note of several lines (`diff`'s verdict on how hits pair, then its details) keeps
    # its details under its `note:`.
    lines.extend(f"note: {note}".replace("\n", "\n  ") for note in result.get("notes", ()))
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


def _compact_join(path, result, params):
    """`join`'s ``result`` without the columns of selectors that agree between the runs
    at every paired hit, and a note naming them: for its text table, which showed each
    twice (tenth evaluation: runoff's, 430 columns). `--json` keeps every column (review
    of PR #269). ``params`` name the result's runs (``prepare``); where they no longer
    give its layout (a run deleted since), the table shows every column."""
    try:
        _, _, keys, phase, _, _, _, sides, same = _join_layout(path, params)
        dropped, unpaired = _unchanged(path, keys, phase, sides, same)
    except ValueError:
        return result
    if not dropped:
        return result
    named = {name: dict(zip(sides[name][1], sides[name][2])) for name in sides}
    gone = {named[name][selector] for name in named for selector in dropped}
    many = len(dropped) > 1
    note = (
        f"left out {len(dropped)} selector{'s' if many else ''} that "
        f"{'agree' if many else 'agrees'} between the runs at every paired hit: "
        f"{_quoted(dropped)}"
        + (f" (with {'their' if many else 'its'} values at unpaired hits)" if unpaired else "")
        + f"; --param unchanged=1 or --json shows {'them' if many else 'it'}"
    )
    return {
        **result,
        "columns": [column for column in result["columns"] if column not in gone],
        "rows": [{k: v for k, v in row.items() if k not in gone} for row in result["rows"]],
        "notes": [*result.get("notes", []), note],
    }


def query_table(path, result, params=None):
    """`table` for a canned query's ``result`` (``result["query"]`` names it), run with
    ``params`` from ``prepare``, which name its runs: read again by run id, never as "the
    newest", which a probe may have saved since. For set_effect, with the run's changes,
    read whole, to show each from where its values differ; for join, without the
    selectors that agree at every paired hit, unless ``params`` has unchanged=1."""
    params = params or {}
    if result.get("query") == "join" and params.get("unchanged") != 1:
        result = _compact_join(path, result, params)
    changes = None
    if result.get("query") == "set_effect" and "interventions" in result["columns"]:
        changes = query(
            path,
            "SELECT hit, line, name, before, after FROM interventions WHERE run_id = :run "
            "ORDER BY rowid",
            {"run": params.get("run")},
            limit=MAX_ROWS,
        )["rows"]
    return table(result, changes=changes)


# What `peeky sql --schema NAME` takes besides a table or view, in the whole text's order.
SCHEMA_PARTS = ("joins", "queries", "functions", "examples", "rules")
# Each view in a few words, for `peeky sql --schema`'s index.
_VIEW_SUMMARIES = {
    "evidence": "captures with some of their run's columns (label, program, status, args, ...; "
    "not output)",
    "last_run": "The evidence rows of the newest run",
}


def _schema_lines(manifest, name):
    """The text of one table, view or other part (``SCHEMA_PARTS``) of ``manifest``."""
    if name in manifest.get("tables", {}):
        info = manifest["tables"][name]
        return [
            f"table {name}: {info['description']}",
            *(f"  {column}: {meaning}" for column, meaning in info["columns"].items()),
        ]
    if name in manifest.get("views", {}):
        return [f"view {name}: {manifest['views'][name]}"]
    if name == "joins":
        return [f"joins: {manifest['joins']}"]
    if name == "queries":
        lines = ["canned queries (peeky queries <name> [--param k=v]):"]
        for query_name, info in manifest["queries"].items():
            params = ", ".join(f"{p}: {d}" for p, d in info["params"].items())
            lines.append(
                f"  {query_name}: {info['description']}" + (f" [{params}]" if params else "")
            )
        return lines
    if name == "functions":
        return [
            "functions:",
            *(f"  {f}: {meaning}" for f, meaning in manifest["functions"].items()),
        ]
    if name == "examples":
        return ["examples:", *(f"  {example}" for example in manifest["examples"])]
    return ["rules:", *(f"  - {rule}" for rule in manifest["rules"])]


def schema_text(manifest):
    """The whole schema as text: `peeky sql --schema all`."""
    lines = [f"store: {manifest['store']} ({manifest['engine']})", ""]
    for name in manifest["tables"]:
        lines.extend([*_schema_lines(manifest, name), ""])
    for name in [*manifest["views"], "joins"]:
        lines.extend(_schema_lines(manifest, name))
    lines.append("")
    for name in ("queries", "functions"):
        lines.extend([*_schema_lines(manifest, name), ""])
    lines.extend(_schema_lines(manifest, "examples"))
    lines.extend(_schema_lines(manifest, "rules"))
    return "\n".join(lines)


def schema_index(manifest):
    """`peeky sql --schema`'s short form: a line for each table, view and other part, and
    how to see them in full (eleventh evaluation: the whole is 20 KB, and two agents'
    tools cut it off)."""
    tables, views = manifest["tables"], manifest["views"]
    width = max(map(len, [*tables, *views, *SCHEMA_PARTS])) + 2
    functions = ", ".join(name.split("(")[0] for name in manifest["functions"])
    parts = {
        "joins": manifest["joins"],
        "queries": f"{len(manifest['queries'])} canned queries, each described in full; "
        "`peeky queries --help` lists their parameters",
        "functions": f"SQL functions Peeky adds: {functions}",
        "examples": f"{len(manifest['examples'])} example commands",
        "rules": f"Read-only, one SELECT (or WITH ... SELECT) per call; at most {DEFAULT_ROWS} "
        f"rows unless --limit (up to {MAX_ROWS}), {QUERY_SECONDS} s per query, and other bounds; "
        ":name takes --param name=value",
    }
    return "\n".join(
        [
            f"store: {manifest['store']} ({manifest['engine']})",
            "",
            "tables:",
            *(
                f"  {name.ljust(width)}{info['description'].rstrip('.')} "
                f"({len(info['columns'])} columns)"
                for name, info in tables.items()
            ),
            "views:",
            *(f"  {name.ljust(width)}{_VIEW_SUMMARIES[name]}" for name in views),
            "other parts:",
            *(f"  {name.ljust(width)}{text}" for name, text in parts.items()),
            "",
            "`peeky sql --schema NAME [NAME ...]` shows those in full (e.g. --schema captures "
            "functions); --schema all shows everything.",
        ]
    )


def schema_part(manifest, names):
    """The tables, views and other parts of ``manifest`` that ``names`` name, in its
    shape (with its store and engine), for `peeky sql --schema NAME ...`; the whole
    manifest for `all`. An unknown name is refused, with the names there are."""
    tables, views = manifest["tables"], manifest["views"]
    wanted = list(dict.fromkeys(name.strip().lower() for name in names))
    if unknown := [n for n in wanted if n != "all" and n not in [*tables, *views, *SCHEMA_PARTS]]:
        raise ValueError(
            f"Evidence query failed: --schema has no {_quoted(unknown)}: name a table "
            f"({', '.join(tables)}), a view ({', '.join(views)}), "
            f"{', '.join(SCHEMA_PARTS)}, or all"
        )
    if "all" in wanted:
        return manifest
    part = {"store": manifest["store"], "engine": manifest["engine"]}
    for name in wanted:
        if name in tables:
            part.setdefault("tables", {})[name] = tables[name]
        elif name in views:
            part.setdefault("views", {})[name] = views[name]
        else:
            part[name] = manifest[name]
    return part


def schema_part_text(part):
    """``schema_part``'s text: each table, view or other part in full, a blank line apart."""
    names = [*part.get("tables", {}), *part.get("views", {})]
    names += [name for name in SCHEMA_PARTS if name in part]
    blocks = [[f"store: {part['store']} ({part['engine']})"]]
    blocks += [_schema_lines(part, name) for name in names]
    return "\n\n".join("\n".join(block) for block in blocks)
