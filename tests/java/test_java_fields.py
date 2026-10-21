"""--set on Java fields and array elements through real java-debug (#86)."""

import json
import os
from pathlib import Path

import pytest

from peeky.cli import main

FIELDS = """package demo;
import java.util.ArrayList;
import java.util.List;
public class Fields {
  static class Interval {
    int start; int end; final int id; String label = "x";
    Interval(int start, int end, int id) { this.start = start; this.end = end; this.id = id; }
  }
  private int count = 3;
  void run() {
    Interval current = new Interval(1, 5, 7);
    int[] arr = {1, 2, 3};
    List<Integer> xs = new ArrayList<>();
    xs.add(4);
    System.out.println("before"); // OBSERVE
    System.out.println("AFTER " + current.end + " " + arr[0] + " " + arr[2] + " " + count);
  }
  public static void main(String[] args) { new Fields().run(); }
}
"""


@pytest.fixture
def fields(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "fields"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "Fields.java").write_text(FIELDS)
    config = root / "fields.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["demo/Fields.java"],
                "main_class": "demo.Fields",
                "arguments": [],
            }
        )
    )
    line = next(i for i, text in enumerate(FIELDS.splitlines(), 1) if "OBSERVE" in text)
    return root, config, f"demo/Fields.java:{line}"


def texts(fields):
    """Whether the bundle's launcher changes a String to a text (#137)."""
    bundle = json.loads(fields[1].read_text())["bundle"]
    return b"peekyTextSet" in (Path(bundle) / "classes" / "PeekyJavaAdapter.class").read_bytes()


def probe(capsys, root, config, at, *options):
    code = main(
        ["probe", str(root), "--config", str(config), "--at", at, *options, "--json", "--quiet"]
    )
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(180)
def test_set_changes_java_fields_and_array_elements_the_program_then_uses(fields, capsys):
    root, config, at = fields
    sets = ["current.end=10", "this.count=9", "arr[0]=11", "arr[-1]=30"]
    capture = "current.end,current.id,current.label,this.count,arr[0],arr[2],xs[0]"
    code, result = probe(
        capsys, root, config, at, "--capture", capture, *[f"--set={s}" for s in sets]
    )
    assert code == 0, result.get("error")
    assert "AFTER 10 11 30 9" in result["output"]
    assert [(c["name"], c["before"], c["after"]) for c in result["interventions"]] == [
        ("current.end", "5", "10"),
        ("this.count", "3", "9"),
        ("arr[0]", "1", "11"),
        ("arr[-1]", "3", "30"),
    ]
    after = {r["selector"]: r for r in result["rows"] if r["phase"] == "after-set"}
    assert [after[s]["value"] for s in ("current.end", "this.count", "arr[0]", "arr[2]")] == [
        "10",
        "9",
        "11",
        "30",
    ]
    # Rows say which values --set can change: a final field and a list's boxed element
    # cannot; a String can, to a text (#137), where the adapter's launcher allows it.
    assert {s: after[s]["settable"] for s in capture.split(",")} == {
        "current.end": True,
        "current.id": False,
        "current.label": texts(fields),
        "this.count": True,
        "arr[0]": True,
        "arr[2]": True,
        "xs[0]": False,
    }
    # Evidence names the field as the selector that reached it.
    run = result["evidence"]["run_id"]
    store = root.parent / "state" / "evidence" / "evidence.db"
    code = main(["queries", "set_effect", "--param", f"run={run}", "--store", str(store), "--json"])
    effect = json.loads(capsys.readouterr().out)
    assert code == 0, effect
    assert "current.end: 5 -> 10" in effect["rows"][0]["interventions"]


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    "assignment, code, message",
    [
        ("current.id=1", "MUTATION_REJECTED", "'id' cannot be changed: it is final"),
        ("current.label=1", "MUTATION_REJECTED", "label"),
        ("xs[0]=1", "MUTATION_REJECTED", "an ArrayList's elements are objects (boxed)"),
        ("current.end=1.5", "MUTATION_REJECTED", "requires an integer literal for int end"),
        ("current.end=3000000000", "MUTATION_REJECTED", "outside the int range for end"),
        ("current.nope=1", "NOT_FOUND", "'nope' is not a field or element"),
        ("arr[3]=1", "INVALID_REQUEST", "[3] is out of range: the array has 3 elements"),
    ],
)
def test_set_refuses_java_members_it_cannot_change_exactly(
    fields, capsys, assignment, code, message
):
    root, config, at = fields
    status, result = probe(capsys, root, config, at, "--set", assignment, "--no-save")
    assert status == 1 and result["error"]["code"] == code, result["error"]
    if message == "label":
        # A String takes a text (#137), not a number; a bundle predating that, nothing.
        message = (
            "requires text in double quotes for String label"
            if texts(fields)
            else "'label' cannot be changed: it is String"
        )
    assert message in result["error"]["message"]


@pytest.mark.timeout(180)
def test_session_set_changes_a_java_field(fields, capsys):
    root, config, at = fields
    try:
        assert main(["session", "start", str(root), "--config", str(config), "--at", at]) == 0
        capsys.readouterr()
        assert main(["session", "set", "current.end=12"]) == 0
        assert "set current.end: 5 -> 12" in capsys.readouterr().out
        assert main(["session", "get", "current.end", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)[0]["value"] == "12"
        assert main(["session", "set", "current['end']=1"]) == 2
        error = capsys.readouterr().err
        assert "[MUTATION_REJECTED]" in error and "map and dict entries cannot be changed" in error
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        assert "AFTER 12 1 3 3" in json.loads(capsys.readouterr().out)["output"]
    finally:
        main(["session", "close"])


@pytest.mark.timeout(180)
def test_the_cookbook_merge_example_is_fixed_by_setting_current_end(tmp_path, monkeypatch, capsys):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    examples = Path(__file__).resolve().parents[2] / "examples" / "java"
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "MergeBug.java").write_text((examples / "demo/MergeBug.java").read_text())
    config = json.loads((examples / "merge.json").read_text())
    (tmp_path / "merge.json").write_text(json.dumps({**config, "bundle": bundle}))
    common = ["--capture", "current", "--hits", "2", "--no-save"]
    code, result = probe(
        capsys, tmp_path, tmp_path / "merge.json", "demo/MergeBug.java:26", *common
    )
    assert code == 0 and "MERGED:[[1,5], [12,14]]" in result["output"]
    code, result = probe(
        capsys,
        tmp_path,
        tmp_path / "merge.json",
        "demo/MergeBug.java:26",
        *common,
        "--set",
        "current.end=10",
        "--set-hit",
        "2",
    )
    assert code == 0 and "MERGED:[[1,10], [12,14]]" in result["output"]


HIDE = """package demo;
public class Hide {
  static class Base { int x = 1; }
  static class Sub extends Base { static int x = 100; }
  public static void main(String[] args) {
    Sub s = new Sub();
    System.out.println("before"); // OBSERVE
    System.out.println("AFTER " + ((Base) s).x + " " + Sub.x);
  }
}
"""


@pytest.mark.timeout(180)
@pytest.mark.parametrize("value", ["5", "1"])
def test_a_field_a_static_field_hides_is_never_written(tmp_path, monkeypatch, capsys, value):
    # Review of #86: java-debug writes through fieldByName, which resolves `x` to the
    # subclass's hiding static while its listing shows the inherited instance field.
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "Hide.java").write_text(HIDE)
    config = tmp_path / "hide.json"
    config.write_text(
        json.dumps(
            {
                "bundle": bundle,
                "sources": ["demo/Hide.java"],
                "main_class": "demo.Hide",
                "arguments": [],
            }
        )
    )
    line = next(i for i, text in enumerate(HIDE.splitlines(), 1) if "OBSERVE" in text)
    at = f"demo/Hide.java:{line}"
    code, result = probe(capsys, tmp_path, config, at, "--set", f"s.x={value}", "--no-save")
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED", result["error"]
    assert "hidden by, or shares its name with, another field" in result["error"]["message"]
    # Nothing was written: the session goes on and the program prints both fields as they were.
    try:
        assert main(["session", "start", str(tmp_path), "--config", str(config), "--at", at]) == 0
        assert main(["session", "set", f"s.x={value}"]) == 2
        assert "[MUTATION_REJECTED]" in capsys.readouterr().err
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        assert "AFTER 1 100" in json.loads(capsys.readouterr().out)["output"]
    finally:
        main(["session", "close"])


@pytest.mark.timeout(180)
def test_the_cookbook_balance_example_tells_a_copy_from_the_lists_element(
    tmp_path, monkeypatch, capsys
):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    examples = Path(__file__).resolve().parents[2] / "examples" / "java"
    (tmp_path / "demo").mkdir()
    source = (examples / "demo/BalanceBug.java").read_text()
    (tmp_path / "demo" / "BalanceBug.java").write_text(source)
    config = json.loads((examples / "balance.json").read_text())
    (tmp_path / "balance.json").write_text(json.dumps({**config, "bundle": bundle}))
    line = next(i for i, text in enumerate(source.splitlines(), 1) if "best.load += task" in text)
    at = f"demo/BalanceBug.java:{line}"
    code, result = probe(
        capsys,
        tmp_path,
        tmp_path / "balance.json",
        at,
        "--capture",
        "best,workers[1]",
        "--hits",
        "2",
    )
    assert code == 0 and "ORDER:a a a a" in result["output"]
    store = tmp_path / "state" / "evidence" / "evidence.db"
    sql = (
        "SELECT object_ref(b.value) = object_ref(w.value) AS same FROM last_run b JOIN last_run w "
        "ON w.hit = b.hit AND w.selector = 'workers[1]' WHERE b.selector = 'best' ORDER BY b.hit"
    )
    assert main(["sql", sql, "--store", str(store), "--json"]) == 0
    assert [row["same"] for row in json.loads(capsys.readouterr().out)["rows"]] == [0, 0]
    code, result = probe(
        capsys,
        tmp_path,
        tmp_path / "balance.json",
        at,
        "--set",
        "workers[1].load=5",
        "--hits",
        "4",
        "--no-save",
    )
    assert code == 0 and "ORDER:a b b b" in result["output"]
