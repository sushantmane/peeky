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
    """A connection that asks its authorizer what SQLite before 3.41 asks."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.authorizer, self.connected = None, set()

    def set_authorizer(self, callback):
        self.authorizer = callback
        super().set_authorizer(callback)

    def execute(self, sql, *args):
        for name in re.findall(r"\b(json_each|json_tree|pragma_\w+)\s*\(", sql):
            if name not in self.connected and self.authorizer is not None:
                if any(self.authorizer(*asked) != sqlite3.SQLITE_OK for asked in _DECLARE_VTAB):
                    raise sqlite3.OperationalError(f"vtable constructor failed: {name}")
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
    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connect(*a, **k, factory=OldSQLite))


def test_before_3_41_table_valued_functions_work_under_the_authorizer(store, monkeypatch):
    # On SQLite 3.31.1 every canned diff failed with `no such table: json_each`; on 3.40.1
    # with `vtable constructor failed: json_each`.
    old_sqlite(monkeypatch, (3, 40, 1))
    plain = sqlite3.connect(":memory:")
    plain.set_authorizer(evidence._authorize)
    with pytest.raises(sqlite3.OperationalError, match="vtable constructor failed: json_each"):
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
        "Evidence store needs SQLite 3.30 or newer with its JSON functions, but this Python's "
        "sqlite3 module has SQLite 3.29.0; run Peeky with a Python built with a newer SQLite"
    )
    assert not store.parent.exists()
    payload = classify(caught.value, session_closed=False)
    assert payload["code"] == "UNSUPPORTED" and "sqlite3.sqlite_version" in payload["hint"]
    assert len(payload["hint"]) <= 150


def test_an_sqlite_without_json_functions_is_refused(store, monkeypatch):
    evidence.record(store, run("1"), language="c")

    class NoJSON(sqlite3.Connection):
        def execute(self, sql, *args):
            if "json_" in sql:
                raise sqlite3.OperationalError("no such function: json_valid")
            return super().execute(sql, *args)

    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connect(*a, **k, factory=NoJSON))
    with pytest.raises(ValueError, match=r"has SQLite [0-9.]+ without them; run Peeky"):
        evidence.query(store, "SELECT 1")
