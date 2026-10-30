"""`--set-at LINE@all` on a launched JVM (#265): Peeky changes an eligible program-owned
instance field through a Java parameter's reference, so the line reached only when the input
agrees with the accepted level, in `settle`, takes the change at each of its stops: the tenth
evaluation's debounce, in Java, tested in one option."""

import json
import os
from pathlib import Path

import pytest

from peeky.cli import main
from peeky.probe import _table

DEBOUNCE = """package demo;

public class Debounce {
    int stable;
    int candidate;
    int count;
    final int threshold;

    Debounce(int threshold) {
        this.threshold = threshold;
    }

    static boolean settle(Debounce db, int level) {
        if (level == db.stable) {
            return false; // AGREES
        }
        if (level != db.candidate) {
            db.candidate = level;
            db.count = 0;
        }
        db.count++;
        if (db.count < db.threshold) {
            return false;
        }
        db.stable = level;
        db.count = 0;
        return true;
    }

    public static void main(String[] args) {
        Debounce db = new Debounce(Integer.parseInt(args[0]));
        StringBuilder events = new StringBuilder();
        int changes = 0;
        for (int ms = 0; ms < args[1].length(); ms++) {
            if (settle(db, args[1].charAt(ms) - '0')) {
                changes++;
                events.append(events.length() > 0 ? "," : "").append(db.stable == 1 ? 'P' : 'R');
                events.append('@').append(ms);
            }
        }
        System.out.println("DEBOUNCE:" + changes + ":" + events);
    }
}
"""
ARGS = ("3", "00101101111110100100001101100000")


@pytest.fixture
def debounce(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "debounce"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "Debounce.java").write_text(DEBOUNCE)
    config = root / "debounce.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["demo/Debounce.java"],
                "main_class": "demo.Debounce",
                "arguments": [],
            }
        )
    )
    return root, config


def line_of(marker):
    return next(i for i, text in enumerate(DEBOUNCE.splitlines(), 1) if f"// {marker}" in text)


def probe(capsys, root, config, *argv):
    command = ["probe", str(root), "--config", str(config), *argv, "--json", "--no-save"]
    code = main([*command, "--quiet", *(f"--arg={arg}" for arg in ARGS)])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(300)
def test_an_all_resets_a_java_field_wherever_the_input_agrees(debounce, capsys):
    root, config = debounce
    agrees = line_of("AGREES")
    at = ["--at", f"demo/Debounce.java:{agrees}"]
    code, plain = probe(capsys, root, config, *at)
    assert code == 0 and plain["output"].strip() == "DEBOUNCE:4:P@5,R@15,P@23,R@28", plain
    code, result = probe(
        capsys, root, config, *at, "--capture", "level,db.count", "--hits", "all",
        "--set-at", f"{agrees}@all", "db.count=0",
    )  # fmt: skip
    assert code == 0, result
    assert result["output"].strip() == "DEBOUNCE:2:P@9,R@20", result["output"]
    changes = result["interventions"]
    # One change at each stop of the line, each read back, in `settle`.
    assert [change["hit"] for change in changes] == list(range(1, result["hits"] + 1))
    assert all(change.get("all") and change["after"] == "0" for change in changes), changes
    assert {change["function"] for change in changes} == {"Debounce.settle(Debounce,int)"}, changes
    assert [c["before"] for c in changes if c["before"] != "0"], changes
    after = [row for row in result["rows"] if row["phase"] == "after-set"]
    assert {row["hit"] for row in after} == set(range(1, result["hits"] + 1))
    text = _table(result)
    assert f"(--set-at line {agrees}@all, stop 1 of line {agrees}, in " in text, text


CHARS = """package demo;

public class Chars {
    char mark = '+';

    public static void main(String[] args) {
        Chars chars = new Chars();
        char sign = '+';
        int n = 0;
        for (int i = 0; i < 5; i++) {
            n += sign == chars.mark ? 1 : 0; // LOOP
        }
        System.out.println("N:" + n + sign + chars.mark);
    }
}
"""


@pytest.mark.timeout(120)
def test_a_char_write_of_the_value_already_there_is_no_change(tmp_path, monkeypatch, capsys):
    # Review of PR #314: java-debug shows a char bare (`+`), so `set sign: + -> +` was
    # counted as a change. A change carries its value's type, which decides it, for a
    # root local and for a field alike.
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "chars"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "Chars.java").write_text(CHARS)
    config = root / "chars.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["demo/Chars.java"],
                "main_class": "demo.Chars",
            }
        )
    )
    at = next(i for i, text in enumerate(CHARS.splitlines(), 1) if "// LOOP" in text)
    command = ["probe", str(root), "--config", str(config), "--at", f"demo/Chars.java:{at}"]
    command += ["--capture", "sign", "--set-at", f"{at}@all", "sign='+'"]
    command += ["--set-at", f"{at}@all", "chars.mark='+'", "--json", "--quiet", "--no-save"]
    assert main(command) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["output"].strip() == "N:5++", result
    changes = result["interventions"]
    assert {(c["name"], c["before"], c["after"], c["type"]) for c in changes} == {
        ("sign", "+", "+", "char"),
        ("chars.mark", "+", "+", "char"),
    }
    assert len(changes) == 10, changes
    text = _table(result)
    for name in ("sign", "chars.mark"):
        assert (
            f"set {name}: + -> +, no change  (--set-at line {at}@all, stop 1 of line {at}, in "
            in text
        ), text
        assert (
            f"set {name}: 2 more writes by --set-at line {at}@all, at stops 4 to 5 of line {at}, each with no "
            "change"
        ) in text, text
    assert "more changes" not in text, text
