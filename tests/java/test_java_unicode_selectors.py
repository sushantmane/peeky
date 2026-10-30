"""Java locals and fields with non-ASCII names, captured, read and set through
real java-debug by the names javac keeps as written: an NFC and an NFD spelling of
`größe` are two variables, and each selector reads only its own."""

import json
import os
from pathlib import Path

import pytest

from peeky.cli import main

NFC, NFD = "gr\u00f6\u00dfe", "gro\u0308\u00dfe"
# Unicode escapes keep the file ASCII, whatever javac's default source encoding. The
# class is named in ASCII: javac on macOS writes a class file's name in NFD.
NAMES = r"""package demo;
public class Names {
  static class Box { int z\u00e4hler = 3; }
  void run() {
    Box gr\u00f6\u00dfe = new Box();
    int gro\u0308\u00dfe = 7;
    int \u00f1 = 5;
    System.out.println("before"); // OBSERVE
    System.out.println("AFTER " + gr\u00f6\u00dfe.z\u00e4hler + " " + \u00f1 + " " + gro\u0308\u00dfe);
  }
  public static void main(String[] args) { new Names().run(); }
}
"""


@pytest.fixture
def names(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "names"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "Names.java").write_text(NAMES, encoding="ascii")
    config = root / "names.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["demo/Names.java"],
                "main_class": "demo.Names",
                "arguments": [],
            }
        )
    )
    line = next(i for i, text in enumerate(NAMES.splitlines(), 1) if "OBSERVE" in text)
    return root, config, f"demo/Names.java:{line}"


@pytest.mark.timeout(180)
def test_a_probe_captures_and_sets_java_locals_and_fields_named_in_unicode(names, capsys):
    root, config, at = names
    capture = f"{NFC}.zähler,ñ,{NFD},{NFC}.zahler"
    sets = [f"{NFC}.zähler=9", "ñ=6", f"{NFD}=8"]
    code = main(
        ["probe", str(root), "--config", str(config), "--at", at, "--capture", capture]
        + [f"--set={s}" for s in sets]
        + ["--json", "--quiet"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert "AFTER 9 6 8" in result["output"], result["output"]
    assert [(c["name"], c["before"], c["after"]) for c in result["interventions"]] == [
        (f"{NFC}.zähler", "3", "9"),
        ("ñ", "5", "6"),
        (NFD, "7", "8"),
    ]
    observed = {r["selector"]: r for r in result["rows"] if r["phase"] == "observed"}
    assert observed[f"{NFC}.zähler"]["value"] == "3" and observed["ñ"]["value"] == "5"
    # The NFD local is its own variable, never the NFC object of the same look.
    assert observed[NFD]["value"] == "7"
    assert "not found" in observed[f"{NFC}.zahler"]["error"]
    after = {r["selector"]: r for r in result["rows"] if r["phase"] == "after-set"}
    assert after[f"{NFC}.zähler"]["settable"] is True and after["ñ"]["settable"] is True


@pytest.mark.timeout(180)
def test_a_session_marks_and_sets_java_locals_named_in_unicode(names, capsys):
    root, config, at = names
    try:
        assert main(["session", "start", str(root), "--config", str(config), "--at", at]) == 0
        capsys.readouterr()
        assert main(["session", "inspect"]) == 0
        inspected = capsys.readouterr().out
        assert f"(children: peeky session get {NFC})" in inspected, inspected
        assert "ñ: int = 5  [settable]" in inspected, inspected
        assert f"{NFD}: int = 7  [settable]" in inspected, inspected
        # The two rows look alike: each says how it is written.
        assert f"{NFD}: int = 7  [settable] [written {ascii(NFD)}: another" in inspected
        assert f"[written {ascii(NFC)}: another name here differs" in inspected, inspected
        assert "not selectable" not in inspected, inspected
        assert main(["session", "get", f"{NFC}.zähler", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)[0]["value"] == "3"
        assert main(["session", "set", "ñ=4"]) == 0
        assert "set ñ: 5 -> 4" in capsys.readouterr().out
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        assert "AFTER 3 4 7" in json.loads(capsys.readouterr().out)["output"]
    finally:
        main(["session", "close"])
