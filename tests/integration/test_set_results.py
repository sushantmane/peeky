"""What a change's result says of the value it wrote, against real debugpy: its `type` after
the write, and `unchanged` only where a write is proven to have left the value as it was."""

import json

import pytest

from peeky.cli import main

PROGRAM = """def run(callback, flag, big):
    total = 0  # STOP
    if callback is not None:
        callback("called")
    return total, flag, big


print(run(print, True, 1000))
"""


@pytest.fixture
def program(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    (tmp_path / "writes.py").write_text(PROGRAM)
    return tmp_path / "writes.py"


def set_json(capsys, assignment):
    assert main(["session", "set", assignment, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.timeout(120)
def test_a_python_write_says_the_type_it_left_and_proves_only_singletons_unchanged(program, capsys):
    assert main(["session", "start", str(program), "--at", "2"]) == 0
    capsys.readouterr()
    try:
        # The type after the write, not the function's it replaced.
        assert set_json(capsys, "callback=None") == {
            "name": "callback",
            "before": "<built-in function print>",
            "after": "None",
            "type": "NoneType",
        }
        # True is always the same object: proven unchanged.
        assert set_json(capsys, "flag=True") == {
            "name": "flag",
            "before": "True",
            "after": "True",
            "type": "bool",
            "unchanged": True,
        }
        # Another 1000 may be another object: the same text proves nothing.
        assert set_json(capsys, "big=1000") == {
            "name": "big",
            "before": "1000",
            "after": "1000",
            "type": "int",
        }
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report"]) == 0
        assert "(0, True, 1000)" in capsys.readouterr().out
    finally:
        main(["session", "close"])
