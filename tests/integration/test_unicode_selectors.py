"""Python locals and attributes with non-ASCII names are captured, read and set by
the name debugpy lists, which is the name Python compiled: NFKC-normalized (an NFD
`größe` in the source is the NFC `größe`; `ﬁx` is `fix`)."""

import json

import pytest

from peeky.cli import main
from peeky.probe import Probe, run_probe

NFC, NFD = "gr\u00f6\u00dfe", "gro\u0308\u00dfe"
# The source spells the local in NFD and the ligature `ﬁ`: Python compiles both NFKC.
SOURCE = (
    "class Größe:\n"
    "    def __init__(self):\n"
    "        self.zähler = 3\n"
    "\n"
    "\n"
    "def main():\n"
    f"    {NFD} = Größe()\n"
    "    ñ = 5\n"
    "    \ufb01x = 9\n"
    f"    print('VALUES', {NFC}.zähler, ñ, fix)  # STOP\n"
    "\n"
    "\n"
    "main()\n"
)
STOP = 10


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "names.py").write_text(SOURCE, encoding="utf-8")
    return tmp_path


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr()


def test_a_probe_captures_and_sets_non_ascii_locals(workspace):
    captures = (f"{NFC}.zähler", "ñ", "fix", NFD, f"{NFC}.zahler", "\ufb01x")
    result = run_probe(workspace, Probe("names.py", STOP, captures, (("ñ", "6"),)))
    assert result["status"] == "completed", result.get("error")
    rows = {r["selector"]: r for r in result["rows"] if r["phase"] == "observed"}
    assert rows[f"{NFC}.zähler"]["value"] == "3"
    assert rows["ñ"]["value"] == "5" and rows["fix"]["value"] == "9"
    # The source's NFD spelling is not what debugpy lists: refused, naming the NFC one.
    assert "only by Unicode normalization" in rows[NFD]["error"]
    assert f"the debugger lists {NFC!r} (written {ascii(NFC)})" in rows[NFD]["error"]
    # A field is matched exactly too: `zahler` is not `zähler`, and nothing is guessed.
    assert "not found in" in rows[f"{NFC}.zahler"]["error"]
    assert "normalization" not in rows[f"{NFC}.zahler"]["error"]
    assert "the debugger lists 'fix' (written 'fix')" in rows["\ufb01x"]["error"]
    after = {r["selector"]: r for r in result["rows"] if r["phase"] == "after-set"}
    assert after["ñ"]["value"] == "6"
    assert result["interventions"][0]["name"] == "ñ"
    assert "VALUES 3 6 9" in result["output"], result["output"]


def test_a_session_inspects_gets_and_sets_non_ascii_locals(workspace, capsys):
    assert run(capsys, "session", "start", workspace / "names.py", "--at", STOP)[0] == 0
    inspected = run(capsys, "session", "inspect")[1].out
    assert f"(children: peeky session get {NFC})" in inspected, inspected
    assert "not selectable" not in inspected.split("[Globals]")[0], inspected
    code, out = run(capsys, "session", "get", f"{NFC}.zähler", "ñ", "--json")
    rows = {row["selector"]: row for row in json.loads(out.out)}
    assert code == 0 and rows[f"{NFC}.zähler"]["value"] == "3" and rows["ñ"]["value"] == "5"
    assert "5 -> 8" in run(capsys, "session", "set", "ñ=8")[1].out
    code, out = run(capsys, "session", "set", f"{NFD}=1", "--json")
    error = json.loads(out.out)["error"]
    assert code == 2 and error["code"] == "NOT_FOUND"
    assert "only by Unicode normalization" in error["message"]
    run(capsys, "session", "continue")
    assert "VALUES 3 8 9" in run(capsys, "session", "report")[1].out
    assert run(capsys, "session", "close")[0] == 0
