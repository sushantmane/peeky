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
    # Rows say which values --set can change: a final field, a String and a list's
    # boxed element cannot.
    assert {s: after[s]["settable"] for s in capture.split(",")} == {
        "current.end": True,
        "current.id": False,
        "current.label": False,
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
        ("current.label=1", "MUTATION_REJECTED", "'label' cannot be changed: it is String"),
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
