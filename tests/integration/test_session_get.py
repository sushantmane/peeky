"""`session get` against real debugpy: a failed get's summary line comes after the rows it
calls "above", and a value at the inspection's depth limit says why it lists no children."""

import json
import os
import subprocess
import sys
from pathlib import Path

import peeky
from peeky.cli import main

# The subprocess runs the Peeky under test.
SRC = Path(peeky.__file__).resolve().parents[1]
OUTLINE = """class Outline:
    def __init__(self):
        self.items = [{"text": "Brot", "level": 1}]
        self.history = [list(self.items)]

    def rename(self, text):
        self.items[0]["text"] = text  # RENAME
        return self


print(Outline().rename("Brötchen").items)
"""


def _line(marker):
    return next(i for i, text in enumerate(OUTLINE.splitlines(), 1) if marker in text)


def test_a_failed_gets_summary_comes_after_its_rows(tmp_path):
    # With stdout and stderr on one pipe, which buffers stdout, `peeky: no value for
    # ranking[1] (each row above says why)…` still comes after the rows.
    program = tmp_path / "outline.py"
    program.write_text(OUTLINE)
    env = {
        **os.environ,
        "PEEKY_STATE_DIR": str(tmp_path / "state"),
        "PYTHONPATH": str(SRC),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    def peeky(*argv):
        return subprocess.run(
            [sys.executable, "-m", "peeky.cli", *argv],
            env=env,
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
        )

    started = peeky("session", "start", str(program), "--at", str(_line("RENAME")))
    assert started.returncode == 0, started.stdout
    try:
        got = peeky("session", "get", "text", "nosuch")
        assert got.returncode == 1, got.stdout
        lines = got.stdout.splitlines()
        assert lines[0] == "text: str = 'Brötchen'", lines
        assert lines[-1] == (
            "peeky: no value for nosuch (each row above says why), so the exit status is 1"
        ), lines
    finally:
        peeky("session", "close")


def test_a_value_at_the_depth_limit_says_why_it_lists_no_children(tmp_path, monkeypatch, capsys):
    # `session get 'self.history[-1][2]' 'self.items[2]'` lists the dict's children only
    # for `self.items[2]`: each selector is read on its own, and the first is 4 levels below
    # `self`, the inspection's depth limit. It says so.
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    program = tmp_path / "outline.py"
    program.write_text(OUTLINE)
    assert main(["session", "start", str(program), "--at", str(_line("RENAME"))]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "get", "self.history[-1][0]", "self.items[0]", "--json"]) == 0
        deep, shallow = json.loads(capsys.readouterr().out)
        assert deep["identity"] == shallow["identity"], (deep, shallow)
        assert "children" not in deep and deep["children_error"] == (
            "none listed: this value is 4 levels below a local, the inspection's depth limit; "
            "read its parts through a shorter path to the same object, if there is one"
        ), deep
        assert [child["selector"] for child in shallow["children"][:2]] == [
            "self.items[0]['text']",
            "self.items[0]['level']",
        ], shallow
        assert main(["session", "get", "self.history[-1][0]"]) == 0
        text = capsys.readouterr().out
        assert "<children: none listed: this value is 4 levels below a local" in text, text
    finally:
        main(["session", "close"])
        capsys.readouterr()
