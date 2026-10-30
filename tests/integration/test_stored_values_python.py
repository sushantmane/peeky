"""SQL's text() and elements() against real debugpy: debugpy names a
value's type by its `__class__` and shows it by its own `__repr__`, so a class can be
stored exactly as a `str` is: the same type, value and identity. Nothing stored proves a
Python value is a builtin `str`, so both functions are NULL for every Python value."""

import pytest

from peeky import evidence
from peeky.cli import main

PROGRAM = """import builtins

str = type("str", (), {"__repr__": lambda self: "'Göteborg'"})


class Disguised:
    __class__ = property(lambda self: builtins.str)

    def __repr__(self):
        return "'Göteborg'"

    def __len__(self):
        return 8


fake, disguised, real = str(), Disguised(), "Göteborg"
print(len(real))  # STOP
"""


@pytest.mark.timeout(120)
def test_a_python_value_that_reads_as_a_str_gets_no_text(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    program = tmp_path / "spoof.py"
    program.write_text(PROGRAM)
    line = PROGRAM.splitlines().index("print(len(real))  # STOP") + 1
    assert main(["probe", str(program), "--at", str(line), "--capture", "fake,disguised,real"]) == 0
    capsys.readouterr()
    rows = evidence.query(
        tmp_path / "state" / "evidence" / "evidence.db",
        "SELECT selector, type, value, identity, text(value, language, type) AS t, "
        "elements(value, language) AS e FROM last_run ORDER BY selector",
    )["rows"]
    # A class named `str`, and one whose `__class__` is `str`, are stored as the real `str`
    # is: the second even without an identity.
    assert {row["selector"]: (row["type"], row["value"]) for row in rows} == {
        "disguised": ("str", "'Göteborg'"),
        "fake": ("str", "'Göteborg'"),
        "real": ("str", "'Göteborg'"),
    }
    by_selector = {row["selector"]: row for row in rows}
    assert by_selector["disguised"]["identity"] is None and by_selector["real"]["identity"] is None
    assert all(row["t"] is None and row["e"] is None for row in rows)
