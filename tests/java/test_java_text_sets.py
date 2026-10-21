"""Setting a Java String to a text (#137 part 2): java-debug makes a new String of it,
and the variable (a local, a field or an array element declared String) refers to it;
other references keep the old String. A bundle whose launcher predates it refuses."""

import json
import os
from pathlib import Path

import pytest

from peeky.cli import main

GREET = """package demo;

public class Greet {
    private String label = "old";
    private int count = 1;

    public static void main(String[] args) {
        String name = "apple ";
        String alias = name;
        Greet g = new Greet();
        String[] names = {"a", "b"};
        System.out.println("NAME:[" + name + "] ALIAS:[" + alias + "] " + g.label + " " + names[1]);
    }
}
"""
OBSERVE = next(i for i, text in enumerate(GREET.splitlines(), 1) if "NAME:[" in text)


@pytest.fixture
def greet(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "greet"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "Greet.java").write_text(GREET)
    config = root / "greet.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["demo/Greet.java"],
                "main_class": "demo.Greet",
                "arguments": [],
            }
        )
    )
    # A bundle built before #137's launcher has no marker for String changes.
    launcher = Path(bundle) / "classes" / "PeekyJavaAdapter.class"
    return root, config, b"peekyTextSet" in launcher.read_bytes()


def probe(capsys, greet, *argv):
    root, config, _ = greet
    at = ["probe", str(root), "--config", str(config), "--at", f"demo/Greet.java:{OBSERVE}"]
    code = main([*at, "--capture", "name,alias,g.label", *argv, "--json", "--no-save", "--quiet"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(180)
def test_a_string_local_field_or_element_takes_a_text(greet, capsys):
    # #137 part 2: `--set name="apple"` was refused ("Java mutation requires a numeric or
    # boolean scalar literal").
    sets = ["--set", 'name="apple"', "--set", 'g.label="new \\"quoted\\""', "--set", 'names[1]="z"']
    code, result = probe(capsys, greet, *sets)
    if not greet[2]:
        assert code == 1 and "predates String changes" in result["error"]["message"], result
        return
    assert code == 0, result.get("error")
    # The local refers to a new String; `alias` keeps the old one.
    assert 'NAME:[apple] ALIAS:[apple ] new "quoted" z' in result["output"], result["output"]
    assert [(c["name"], c["before"], c["after"]) for c in result["interventions"]] == [
        ("name", '"apple "', '"apple"'),
        ("g.label", '"old"', '"new "quoted""'),
        ("names[1]", '"b"', '"z"'),
    ]
    rows = {row["selector"]: row for row in result["rows"] if row["phase"] == "observed"}
    assert rows["name"]["settable"] is True, rows


@pytest.mark.timeout(180)
def test_a_text_changes_only_a_string(greet, capsys):
    code, result = probe(capsys, greet, "--set", 'g.count="2"')
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED", result
    expected = (
        "predates String changes" if not greet[2] else "text in double quotes changes a String"
    )
    assert expected in result["error"]["message"], result["error"]
    code, result = probe(capsys, greet, "--set", "name=5")
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED", result
    assert "requires text in double quotes for String name" in result["error"]["message"]
