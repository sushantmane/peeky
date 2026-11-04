"""The refusals round 2's agents met most before their first stop now take the input or say the
next command (ADR 0009, section 5)."""

import json
from argparse import Namespace

import pytest

from peeky import cli
from peeky.adapters import check_module
from peeky.cli import main
from peeky.errors import Refused


def parsed(monkeypatch, *argv):
    seen = {}
    monkeypatch.setattr("peeky.cli.probe", lambda args: seen.update(vars(args)) or 0)
    main(["probe", ".", "--at", "a.py:5", *argv])
    return seen


def test_set_at_as_one_word_is_its_two(monkeypatch):
    # 9 runs in 8 attempts wrote `--set-at '966@1 url=x'`, an argparse usage error.
    for word, expected in (
        ("966@1 url='http://a'", ["966@1", "url='http://a'"]),
        ("378 vmin=3000000", ["378", "vmin=3000000"]),
        ("lib/x.py:12@all n = 3", ["lib/x.py:12@all", "n = 3"]),
        ('193 name="a b=c"', ["193", 'name="a b=c"']),
    ):
        assert parsed(monkeypatch, "--set-at", word)["set_at"] == [expected], word
    # Two words stay two; a program's word after `--` is the program's.
    seen = parsed(monkeypatch, "--set-at", "9@2", "n=0", "--", "--set-at", "1 x=1")
    assert seen["set_at"] == [["9@2", "n=0"]]
    assert seen["arg"] == ["--set-at", "1 x=1"]


def test_set_at_as_one_word_that_isnt_a_location_and_an_assignment_is_still_refused(
    monkeypatch, capsys
):
    for word in ("a b.py:3 x=1", "12 x", "x=1"):
        with pytest.raises(SystemExit):
            parsed(monkeypatch, "--set-at", word)
        assert "expected 2 arguments" in capsys.readouterr().err


def test_a_java_configs_absolute_paths_inside_the_root_are_taken(tmp_path):
    # 6 attempts wrote attach.json with absolute paths, and were refused.
    (tmp_path / "src").mkdir()
    data = {
        "port": 5005,
        "sources": [str(tmp_path / "src/A.java"), "src/B.java"],
        "class_roots": [str(tmp_path / "target/classes")],
    }
    assert cli._java_paths(data, tmp_path) == {
        "port": 5005,
        "sources": ["src/A.java", "src/B.java"],
        "class_roots": ["target/classes"],
    }
    with pytest.raises(Refused) as caught:
        cli._java_paths({"sources": ["/elsewhere/A.java"]}, tmp_path)
    assert f"outside the target directory {tmp_path}" in str(caught.value)


def test_no_stop_on_an_attached_jvm_names_the_test_form(tmp_path, capsys):
    # 30 probes in 16 attempts ended NO_STOP on a JVM the agent started by hand.
    args = Namespace(no_save=True, json=True, quiet=True, test_command=None)
    args.old = args.new = args.old_file = args.new_file = None
    result = {
        "status": "no-stop",
        "error": {"code": "NO_STOP", "message": "The program ended…", "hint": "Pick a line."},
        "rows": [],
        "warnings": [],
        "interventions": [],
    }
    cli._probe_result(args, tmp_path, result, None, None, "A.java", "java-attach")
    hint = json.loads(capsys.readouterr().out)["error"]["hint"]
    assert hint.startswith("Pick a line. An attached JVM runs what started it")
    assert "`--test -- <the test command>`" in hint


def test_run_module_with_another_module_gives_the_script_to_run_it_from():
    with pytest.raises(ValueError) as caught:
        check_module("timeit")
    text = str(caught.value)
    assert "runpy.run_module('timeit', run_name='__main__', alter_sys=True)" in text
    assert "`--run run.py -- ARGS`" in text


def test_the_help_starts_with_the_first_question(capsys):
    for argv in (["--help"], ["probe", "--help"]):
        with pytest.raises(SystemExit):
            main(argv)
        text = " ".join(capsys.readouterr().out.split())
        assert "--test -- <the command that runs the failing test> (docs/FIRST.md)" in text
