"""Setting a C `char` array to a text (#137 part 2), on real C builds: the text and a
NUL, if both fit in the array; never past it, and never through a pointer."""

import json
import os
import sys

import pytest

from peeky.cli import main

TEXTS = """#include <stdio.h>
#include <string.h>
struct Rec { char tag[8]; int n; };
int main(void) {
  char name[16] = "apple :4";
  char code[6] = "ab";
  const char *label = "literal";
  struct Rec rec = {"rec", 1};
  int count = 2;
  printf("NAME:%s CODE:%s LABEL:%s TAG:%s LEN:%zu N:%d\\n", name, code, label, rec.tag, strlen(name), count); // OBSERVE
  return 0;
}
"""
OBSERVE = next(i for i, text in enumerate(TEXTS.splitlines(), 1) if "// OBSERVE" in text)


@pytest.fixture
def texts(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "texts"
    root.mkdir()
    (root / "texts.c").write_text(TEXTS)
    return root


def probe(capsys, root, *argv):
    at = ["probe", str(root), "--at", f"texts.c:{OBSERVE}", "--capture", "name,code"]
    code = main([*at, *argv, "--json", "--quiet"])
    return code, json.loads(capsys.readouterr().out)


def refused(capsys, root, *argv):
    code, result = probe(capsys, root, *argv)
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED", result
    assert "NAME:" not in (result.get("output") or ""), result["output"]
    return result["error"]["message"]


@pytest.mark.timeout(240)
def test_a_char_array_takes_a_text_and_a_nul_that_fit(texts, capsys):
    # #137 part 2: `--set name="apple"` was refused ("Native mutations require bounded
    # decimal integer literals"), so a text could only be changed by editing the source.
    code, result = probe(capsys, texts, "--set", 'name="apple"', "--set", 'code="12345"')
    assert code == 0, result.get("error")
    assert "NAME:apple CODE:12345 LABEL:literal TAG:rec LEN:5" in result["output"], result
    # The text and its NUL are written; the bytes after the NUL are left as they were.
    assert [(c["name"], c["before"], c["after"]) for c in result["interventions"]] == [
        ("name", '"apple :4"', '"apple\\0:4"'),
        ("code", '"ab"', '"12345"'),
    ]
    after = {row["selector"]: row for row in result["rows"] if row["phase"] == "after-set"}
    assert after["code"]["value"] == '"12345"' and after["code"]["settable"] is True
    # The evidence keeps both, as for any change.
    assert main(["sql", "SELECT name, before, after FROM interventions", "--json"]) == 0
    stored = json.loads(capsys.readouterr().out)["rows"]
    assert stored[1] == {"name": "code", "before": '"ab"', "after": '"12345"'}
    # Non-ASCII text: written whole where the debugger writes memory (lldb-dap 21);
    # one character at a time elsewhere, which Peeky does for printable ASCII only.
    code, result = probe(capsys, texts, "--set", 'name="caf\\u00e9"')
    if sys.platform == "darwin":
        assert code == 0 and "NAME:café " in result["output"], result
    else:
        assert code == 1 and "printable ASCII" in result["error"]["message"], result
    # `session set` takes the same values.
    assert main(["session", "start", str(texts / "texts.c"), "--at", str(OBSERVE)]) == 0
    try:
        assert main(["session", "set", 'name="pear"']) == 0
        assert 'set name: "apple :4" -> "pear\\0 :4"' in capsys.readouterr().out
        assert main(["session", "continue", "--to-exit"]) == 0
        main(["session", "report"])
        assert "NAME:pear CODE:ab" in capsys.readouterr().out
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(240)
def test_a_text_is_refused_where_it_does_not_fit_or_is_not_a_char_array(texts, capsys):
    # #137 part 2: never past a buffer, and nothing changed by a refusal.
    message = refused(capsys, texts, "--set", 'code="123456"')
    assert (
        "the text does not fit: `code` is a char[6], which holds 5 bytes of text and a NUL, and the text is 6 bytes"
        in message
    ), message
    message = refused(capsys, texts, "--set", 'label="x"')
    assert "`label` cannot be changed to a text: it is a const char *" in message, message
    assert "cannot prove the memory it points to is writable" in message, message
    message = refused(capsys, texts, "--set", 'count="2"')
    assert "a text changes a char array (char[N]) or a std::string" in message, message
    message = refused(capsys, texts, "--set", "name=5")
    assert "requires a text in double quotes for 'name', a char[16]" in message, message
    message = refused(capsys, texts, "--set", 'name="a\\u0000b"')
    assert "a NUL in the text would end the C string" in message, message
    # A field's text is refused before the program runs.
    code, result = probe(capsys, texts, "--set", 'rec.tag="x"')
    assert code == 1 and result.get("refused_before_launch") is True, result
    assert "not into a field or element" in result["error"]["message"], result
    # A value that is not a JSON string is the request's error.
    code, result = probe(capsys, texts, "--set", 'name="open')
    assert code == 1 and result["error"]["code"] == "INVALID_REQUEST", result
    assert "write text in double quotes as JSON writes a string" in result["error"]["message"]
