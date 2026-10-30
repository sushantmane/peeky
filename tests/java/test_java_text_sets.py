"""Setting a Java String to a text: java-debug makes a new String of it,
and the variable (a local, a field or an array element declared String) refers to it;
other references keep the old String. A bundle whose launcher predates it refuses."""

import json
import os
from pathlib import Path

import pytest

from peeky.cli import main


def _refused_code(result):
    # A probe refused before it ran exits 2; a refusal at a stop, 1.
    return 2 if result.get("refused_before_launch") else 1


GREET = """package demo;

public class Greet {
    private String label = "old";
    private Object held = "held";
    private int count = 1;

    public static void main(String[] args) {
        String name = "apple ";
        String alias = name;
        Greet g = new Greet();
        String[] names = {"a", "b"};
        Object o = "obj";
        CharSequence cs = "cs";
        Object[] objs = {"x"};
        final String fc = "const";
        final int fk = 5;
        String cap = "captured";
        java.util.function.Supplier<String> lam = () -> cap;
        System.out.println("NAME:[" + name + "] ALIAS:[" + alias + "] " + g.label + " " + names[1] + " " + o + cs + objs[0] + g.held + " FC:" + fc + " FK:" + fk + " LAM:" + lam.get());
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
    # A bundle whose launcher predates String changes has no marker for them.
    launcher = Path(bundle) / "classes" / "PeekyJavaAdapter.class"
    return root, config, b"peekyTextSet" in launcher.read_bytes()


def probe(capsys, greet, *argv):
    root, config, _ = greet
    at = ["probe", str(root), "--config", str(config), "--at", f"demo/Greet.java:{OBSERVE}"]
    capture = "name,alias,g.label,o,cs,objs[0],g.held,fc,fk,cap"
    code = main([*at, "--capture", capture, *argv, "--json", "--no-save", "--quiet"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(180)
def test_a_string_local_field_or_element_takes_a_text(greet, capsys):
    # `--set name="apple"` changes the String, not refused as "Java mutation requires a
    # numeric or boolean scalar literal".
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
    assert code == _refused_code(result) and result["error"]["code"] == "MUTATION_REJECTED", result
    expected = (
        "predates String changes" if not greet[2] else "text in double quotes changes a String"
    )
    assert expected in result["error"]["message"], result["error"]
    code, result = probe(capsys, greet, "--set", "name=5")
    assert code == _refused_code(result) and result["error"]["code"] == "MUTATION_REJECTED", result
    assert "requires text in double quotes for String name" in result["error"]["message"]


@pytest.mark.timeout(180)
def test_marks_follow_the_declared_type_and_a_constant_local_is_never_changed(greet, capsys):
    # `Object o = "obj"` and `CharSequence cs` were marked [settable] from
    # the type of the String they hold, though the launcher refuses them (and javac's
    # constant locals, `final String fc = "const"` and `final int fk = 5`, were changed
    # and reported, while the program printed the constants it had compiled in).
    code, result = probe(capsys, greet)
    assert code == 0, result.get("error")
    marks = {row["selector"]: row.get("settable") for row in result["rows"]}
    assert marks == {
        "name": greet[2],
        "alias": greet[2],
        "g.label": greet[2],
        "o": False,
        "cs": False,
        "objs[0]": False,
        "g.held": False,
        "fc": False,
        "fk": False,
        "cap": False,
    }, marks
    for assignment in ('fc="x"', "fk=9"):
        code, result = probe(capsys, greet, "--set", assignment)
        assert code == _refused_code(result) and result["error"]["code"] == "MUTATION_REJECTED", (
            result
        )
        message = result["error"]["message"]
        # Judged from the class files before the program runs, any bundle.
        assert result.get("refused_before_launch") is True, result
        assert "javac compiles a constant local into its uses" in message, message
        assert "FC:const FK:5" not in (result.get("output") or ""), result
    if greet[2]:
        for assignment, declared in (("g.held", "java.lang.Object"), ("objs[0]", "Object")):
            code, result = probe(capsys, greet, "--set", f'{assignment}="y"')
            assert (
                code == _refused_code(result) and result["error"]["code"] == "MUTATION_REJECTED"
            ), result
            assert f"it is declared {declared} and holds a String" in result["error"]["message"]
        code, result = probe(capsys, greet, "--set", 'o="y"')
        assert code == 2 and "`o` is declared Object; only" in result["error"]["message"], result


@pytest.mark.timeout(180)
def test_a_local_the_code_never_reads_after_the_stop_is_never_changed(greet, capsys):
    # `cap` was captured by a lambda before the stop, and nothing reads
    # it after: the change was reported, and `lam.get()` still returned `captured`.
    code, result = probe(capsys, greet, "--set", 'cap="X"')
    assert code == _refused_code(result) and result["error"]["code"] == "MUTATION_REJECTED", result
    message = result["error"]["message"]
    # No read follows the line's start, judged before the program runs.
    assert result.get("refused_before_launch") is True, result
    assert "is never read after a stop at this line's start (a lambda or inner class" in message
    assert "LAM:" not in (result.get("output") or ""), result
    # A local read after the stop is changed as before.
    if greet[2]:
        code, result = probe(capsys, greet, "--set", 'alias="b"')
        assert code == 0 and "ALIAS:[b]" in result["output"], result
