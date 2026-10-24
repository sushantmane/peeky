"""Which SQLite the evidence store needs: 3.30 with JSON functions, checked where it opens;
table-valued functions work before 3.41 with the read-only authorizer unchanged."""

import re
import sqlite3

import pytest

from peeky import evidence
from peeky.errors import classify

# What SQLite 3.29 to 3.40 pass the authorizer when a connection first uses a
# table-valued function (checked against builds of 3.29.0, 3.31.1 and 3.40.1): an UPDATE
# of sqlite_master that sqlite3_declare_vtab compiles and never runs. 3.41 asks nothing.
_DECLARE_VTAB = [
    (sqlite3.SQLITE_UPDATE, "sqlite_master", column, "main", None)
    for column in ("type", "name", "tbl_name", "rootpage", "sql")
]


class OldSQLite(sqlite3.Connection):
    """A connection that asks its authorizer what SQLite before 3.41 asks, for the
    table-valued functions in ``tables``. A denied one fails as builds of 3.37.2 and 3.40.1
    say (``failed``, ``pragma_failed``); 3.30 to 3.36 say `no such table` for both."""

    tables = (
        "json_each|json_tree|fts3tokenize|pragma_compile_options|pragma_table_info|"
        "pragma_table_xinfo|pragma_table_list|pragma_index_list|pragma_index_info"
    )
    failed, pragma_failed = "vtable constructor failed: {name}", "not authorized"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.authorizer, self.connected = None, set()

    def set_authorizer(self, callback):
        self.authorizer = callback
        super().set_authorizer(callback)

    def execute(self, sql, *args):
        for name in re.findall(rf"\b({self.tables})\b", sql):
            if name not in self.connected and self.authorizer is not None:
                if any(self.authorizer(*asked) != sqlite3.SQLITE_OK for asked in _DECLARE_VTAB):
                    failed = self.pragma_failed if name.startswith("pragma_") else self.failed
                    raise sqlite3.DatabaseError(failed.format(name=name))
            self.connected.add(name)
        return super().execute(sql, *args)


def run(value):
    return {
        "probe": {"program": "a.c", "line": 1, "capture": ["x"], "set": []},
        "status": "completed",
        "hits": 1,
        "output": value,
        "rows": [{"hit": 1, "phase": "observed", "selector": "x", "value": value}],
    }


@pytest.fixture
def store(tmp_path):
    return tmp_path / "evidence" / "evidence.db"


def old_sqlite(monkeypatch, version):
    monkeypatch.setattr(sqlite3, "sqlite_version", ".".join(map(str, version)))
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    if version < (3, 37):
        monkeypatch.setattr(OldSQLite, "failed", "no such table: {name}")
        monkeypatch.setattr(OldSQLite, "pragma_failed", "no such table: {name}")
    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connect(*a, **k, factory=OldSQLite))


def test_before_3_41_table_valued_functions_work_under_the_authorizer(store, monkeypatch):
    # On SQLite 3.31.1 every canned diff failed with `no such table: json_each`; on 3.40.1
    # with `vtable constructor failed: json_each`.
    old_sqlite(monkeypatch, (3, 40, 1))
    plain = sqlite3.connect(":memory:")
    plain.set_authorizer(evidence._authorize)
    with pytest.raises(sqlite3.DatabaseError, match="vtable constructor failed: json_each"):
        plain.execute("SELECT value FROM json_each('[1]')")
    plain.close()
    for value in ("1", "2"):
        evidence.record(store, run(value), language="c")
    rows = evidence.run_query(store, "diff")["rows"]
    assert [(row["selector"], row["value_a"], row["value_b"]) for row in rows] == [
        ("(output)", "1", "2"),
        ("x", "1", "2"),
    ]
    assert evidence.query(store, "SELECT value FROM json_each('[7]')")["rows"] == [{"value": 7}]
    assert evidence.query(store, "SELECT COUNT(*) AS n FROM json_tree('[7]')")["rows"] == [{"n": 2}]
    names = evidence.query(store, "SELECT name FROM pragma_table_info('runs') LIMIT 1")["rows"]
    assert names == [{"name": "run_id"}]


@pytest.mark.parametrize("asked", _DECLARE_VTAB)
def test_the_authorizer_still_refuses_what_old_sqlite_asks(asked):
    # The old SQLite's question is never answered yes: no write to sqlite_master is allowed.
    assert evidence._authorize(*asked) == sqlite3.SQLITE_DENY


def test_old_sqlite_queries_still_cannot_write(store, monkeypatch):
    old_sqlite(monkeypatch, (3, 31, 1))
    evidence.record(store, run("1"), language="c")
    for sql in ("UPDATE runs SET label = 'x'", "DELETE FROM captures", "PRAGMA writable_schema=1"):
        with pytest.raises(ValueError, match="not authorized"):
            evidence.query(store, sql)
    assert evidence.query(store, "SELECT label FROM runs")["rows"] == [{"label": None}]
    assert evidence.query(store, "SELECT COUNT(*) AS n FROM captures")["rows"] == [{"n": 1}]


@pytest.mark.parametrize("save", [True, False])
def test_an_sqlite_before_3_30_is_refused_where_the_store_opens(store, monkeypatch, save):
    old_sqlite(monkeypatch, (3, 29, 0))
    with pytest.raises(ValueError) as caught:
        if save:
            evidence.record(store, run("1"), language="c")
        else:
            evidence.query(store, "SELECT 1")
    assert str(caught.value) == (
        "Evidence store needs SQLite 3.30 or newer with the JSON functions, json_each, json_tree "
        "and window functions, but this Python's sqlite3 module has SQLite 3.29.0; run Peeky "
        "with a Python built with a newer SQLite"
    )
    assert not store.parent.exists()
    payload = classify(caught.value, session_closed=False)
    assert payload["code"] == "UNSUPPORTED" and "sqlite3.sqlite_version" in payload["hint"]
    assert len(payload["hint"]) <= 150


def without(monkeypatch, *names, error):
    """SQLite with no table or function by those names, as a build can leave them out."""

    class Without(sqlite3.Connection):
        def execute(self, sql, *args):
            for name in names:
                if re.search(rf"\b{name}\b", sql):
                    raise sqlite3.OperationalError(f"{error}: {name}")
            return super().execute(sql, *args)

    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connect(*a, **k, factory=Without))


def test_an_sqlite_without_json_functions_is_refused(store, monkeypatch):
    # As a real build of 3.31.1 without JSON1 says.
    evidence.record(store, run("1"), language="c")
    without(monkeypatch, "json_valid", "json_each", "json_tree", error="no such function")
    with pytest.raises(ValueError) as caught:
        evidence.query(store, "SELECT 1")
    assert str(caught.value).endswith(
        f"has SQLite {sqlite3.sqlite_version} without the JSON functions (no such function: "
        "json_valid); run Peeky with a Python built with a newer SQLite"
    )


@pytest.mark.parametrize("missing", ["json_each", "json_tree"])
def test_scalar_json_without_its_table_functions_is_refused(store, monkeypatch, missing):
    # SQLITE_OMIT_VIRTUALTABLE keeps json_valid() but has no json_each: every canned query
    # would fail with `no such table: json_each`.
    for value in ("1", "2"):
        evidence.record(store, run(value), language="c")
    without(monkeypatch, missing, error="no such table")
    with pytest.raises(ValueError) as caught:
        evidence.run_query(store, "diff")
    assert f"without {missing} (no such table: {missing}); run Peeky" in str(caught.value)
    assert classify(caught.value, session_closed=False)["code"] == "UNSUPPORTED"


@pytest.mark.parametrize("version", [(3, 31, 1), (3, 40, 1)])
def test_before_3_41_another_table_function_says_which_peeky_sql_can_use(
    store, monkeypatch, version
):
    old_sqlite(monkeypatch, version)
    evidence.record(store, run("1"), language="c")
    with pytest.raises(ValueError) as caught:
        evidence.query(store, "SELECT * FROM fts3tokenize LIMIT 0")
    assert str(caught.value) == (
        f"Evidence query failed: {OldSQLite.failed.format(name='fts3tokenize')} (before SQLite "
        "3.41, peeky sql can use only the table-valued functions json_each, json_tree, "
        "pragma_index_info, pragma_index_list, pragma_table_info, pragma_table_list, "
        f"pragma_table_xinfo; this Python's sqlite3 module has SQLite {sqlite3.sqlite_version}, "
        "so use those, or a Python with SQLite 3.41 or newer)"
    )
    # A table that is not there is only that, on every SQLite.
    with pytest.raises(ValueError) as caught:
        evidence.query(store, "SELECT * FROM nosuch")
    assert str(caught.value) == "Evidence query failed: no such table: nosuch"


@pytest.mark.parametrize("version", [(3, 31, 1), (3, 40, 1)])
@pytest.mark.parametrize("name", ["rtree", "fts5", "pragma_nosuch", "nosuch"])
def test_before_3_41_a_name_that_is_no_table_function_gets_no_hint(
    store, monkeypatch, version, name
):
    # rtree and fts5 are modules, but no table-valued function; pragma_nosuch is no pragma.
    # Each is `no such table` on every SQLite, 3.41 or newer too.
    old_sqlite(monkeypatch, version)
    evidence.record(store, run("1"), language="c")
    with pytest.raises(ValueError) as caught:
        evidence.query(store, f"SELECT * FROM {name}")
    assert str(caught.value) == f"Evidence query failed: no such table: {name}"


def test_before_3_37_another_pragma_says_which_pragmas_peeky_sql_reads(store, monkeypatch):
    # Refused on every SQLite (3.37 and newer say `not authorized`): no newer one is advised.
    old_sqlite(monkeypatch, (3, 31, 1))
    evidence.record(store, run("1"), language="c")
    with pytest.raises(ValueError) as caught:
        evidence.query(store, "SELECT * FROM pragma_compile_options")
    assert str(caught.value) == (
        "Evidence query failed: no such table: pragma_compile_options (peeky sql reads only the "
        "pragma functions pragma_index_info, pragma_index_list, pragma_table_info, "
        "pragma_table_list, pragma_table_xinfo)"
    )
