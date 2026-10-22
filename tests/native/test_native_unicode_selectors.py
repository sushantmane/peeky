"""#195: C and C++ locals and fields with non-ASCII names, captured, read and set
through real LLDB by the names clang keeps as written: an NFC and an NFD spelling of
`größe` are two variables, and each selector reads only its own."""

import json
import os

import pytest

from peeky.cli import main

NFC, NFD = "gr\u00f6\u00dfe", "gro\u0308\u00dfe"
SOURCE = f"""#include <stdio.h>

struct punkt {{ int zähler; long {NFC}; }};

int main(void) {{
    struct punkt {NFC} = {{ 3, 4 }};
    int {NFD} = 7;
    int ñ = 5;
    printf("BEFORE\\n");  // STOP
    printf("AFTER %d %ld %d %d\\n", {NFC}.zähler, {NFC}.{NFC}, ñ, {NFD});
    return 0;
}}
"""
STOP = next(n for n, text in enumerate(SOURCE.splitlines(), 1) if "STOP" in text)


@pytest.fixture
def directory(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def source(directory, language):
    path = directory / f"names.{'cpp' if language == 'cpp' else 'c'}"
    path.write_text(SOURCE, encoding="utf-8")
    return path


@pytest.mark.timeout(240)
@pytest.mark.parametrize("language", ["c", "cpp"])
def test_a_probe_captures_and_sets_native_names_in_unicode(directory, capsys, language):
    path = source(directory, language)
    captures = (f"{NFC}.zähler", f"{NFC}.{NFC}", NFD, "ñ", f"{NFC}.zahler")
    sets = (f"{NFC}.zähler=30", "ñ=50", f"{NFD}=70")
    code = main(
        ["probe", str(path), "--at", str(STOP), "--capture", ",".join(captures)]
        + [f"--set={each}" for each in sets]
        + ["--json", "--quiet"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert "AFTER 30 4 50 70" in result["output"], result["output"]
    observed = {r["selector"]: r for r in result["rows"] if r["phase"] == "observed"}
    assert [observed[s].get("value") for s in captures[:4]] == ["3", "4", "7", "5"]
    assert "not found" in observed[f"{NFC}.zahler"]["error"]
    after = {r["selector"]: r for r in result["rows"] if r["phase"] == "after-set"}
    assert [after[s]["value"] for s in captures[:4]] == ["30", "4", "70", "50"]
    assert all(after[s]["settable"] is True for s in captures[:4])


@pytest.mark.timeout(240)
@pytest.mark.parametrize("language", ["c", "cpp"])
def test_a_session_marks_reads_and_sets_native_names_in_unicode(directory, capsys, language):
    path = source(directory, language)
    assert main(["session", "start", str(path), "--at", str(STOP)]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "inspect"]) == 0
        inspected = capsys.readouterr().out
        assert f"(children: peeky session get {NFC})" in inspected, inspected
        assert "ñ: int = 5  [settable]" in inspected, inspected
        assert f"{NFD}: int = 7  [settable]" in inspected, inspected
        # The two rows look alike: each says how it is written.
        assert f"{NFD}: int = 7  [settable] [written {ascii(NFD)}: another" in inspected
        assert f"[written {ascii(NFC)}: another name here differs" in inspected, inspected
        assert "not selectable" not in inspected, inspected
        assert main(["session", "get", f"{NFC}.zähler", NFD, "--json"]) == 0
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
        assert rows[f"{NFC}.zähler"]["value"] == "3" and rows[NFD]["value"] == "7"
        assert main(["session", "set", f"{NFC}.{NFC}=40"]) == 0
        assert main(["session", "set", "ñ=6"]) == 0
        capsys.readouterr()
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        assert "AFTER 3 40 6 7" in json.loads(capsys.readouterr().out)["output"]
    finally:
        main(["session", "close"])


# Review of #199, round 2: LLDB names a shadowed local with its file (`t @ my file.c:5`),
# which no selector can write when the file's name holds a space; `t` reads and sets the
# one the code names, as #158 made it, and the row says so.
SHADOWED = """#include <stdio.h>

int main(void) {
    int t = 1;
    {
        int t = 2;
        printf("BEFORE %d\\n", t);  // STOP
        printf("INNER %d\\n", t);
    }
    printf("OUTER %d\\n", t);
    return 0;
}
"""


@pytest.mark.timeout(240)
@pytest.mark.parametrize("file", ["my file.c", "one @ two.c"])
def test_a_shadowed_local_in_a_file_named_with_a_space_is_read_and_set(directory, capsys, file):
    path = directory / file
    path.write_text(SHADOWED)
    stop = next(n for n, text in enumerate(SHADOWED.splitlines(), 1) if "STOP" in text)
    assert main(["session", "start", str(path), "--at", str(stop)]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "inspect"]) == 0
        inspected = capsys.readouterr().out
        # The inner `t`, which `t` selects, is offered and settable; the outer one, which
        # no selector names, is neither.
        assert f"t @ {file}:6: int = 2  [settable]\n" in inspected, inspected
        assert f"t @ {file}:4: int = 1 [not selectable]" in inspected, inspected
        assert main(["session", "get", "t", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)[0]["value"] == "2"
        assert main(["session", "set", "t=20"]) == 0
        assert f"set t @ {file}:6: 2 -> 20" in capsys.readouterr().out
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        output = json.loads(capsys.readouterr().out)["output"]
        assert "INNER 20" in output and "OUTER 1" in output, output
    finally:
        main(["session", "close"])
    capsys.readouterr()
    code = main(
        ["probe", str(path), "--at", str(stop), "--capture", "t", "--set", "t=30", "--json"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert "INNER 30" in result["output"] and "OUTER 1" in result["output"], result["output"]
