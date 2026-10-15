"""Corpus of varied bugs (issue #7): each checker rejects the original and an overfit
candidate and accepts the general fix; every observation line is actually reached."""

import json
import shutil
from pathlib import Path

import pytest

from peeky.agent import verify
from peeky.cli import main
from peeky.probe import Probe, run_probe

CORPUS = Path(__file__).resolve().parents[2] / "examples/corpus"
CASES = sorted(path.name for path in CORPUS.iterdir() if (path / "case.json").is_file())


def test_corpus_has_ten_varied_bugs():
    assert len(CASES) >= 10


@pytest.mark.parametrize("name", CASES)
def test_corpus_case_rejects_original_and_overfit_and_accepts_fix(name, tmp_path, capsys):
    shutil.copytree(CORPUS / name, tmp_path / name)
    root = tmp_path / name
    case = json.loads((root / "case.json").read_text())
    program, check = root / "program.py", root / "check.py"
    original = program.read_text()
    assert verify(check, program, root)["passed"] is False
    result = run_probe(root, Probe("program.py", case["observe_line"], tuple(case["capture"])))
    assert result["status"] == "completed" and result["hits"] == 1, result.get("error")
    assert all("value" in row for row in result["rows"]), result["rows"]
    for kind, expected in (("fix", 0), ("overfit", 1)):
        argv = ["verify", str(program), "--check", str(check), "--json"]
        argv += ["--old", case[kind]["old"], "--new", case[kind]["new"]]
        assert main(argv) == expected, (kind, capsys.readouterr())
        outcome = json.loads(capsys.readouterr().out)
        assert outcome["passed"] is (expected == 0)
    assert program.read_text() == original
