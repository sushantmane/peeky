"""A C character array shows its text up to its first NUL, and a note of what follows it
(thirteenth evaluation): `char name[32]` holding a name, its NUL and leftovers showed
`"Adeyemi\\0\\x80\\xf8\\xe6\\xef\\U00000001..."`, so the leftovers read as part of the text,
a byte had two spellings, and SQL's `text()` gave NULL."""

import json
import os

import pytest

from peeky.cli import main

SOURCE = """#include <stdio.h>
#include <string.h>

struct badge { char label[12]; int id; };

int main(void) {
    char name[32];
    memset(name, 1, sizeof name);
    memcpy(name, "Okafor", 7);
    char full[4];
    memcpy(full, "abcd", 4);
    char first[6] = {0, 'x', 'y'};
    char city[16] = "Malm\\xc3\\xb6";
    unsigned char raw[6] = {'o', 'k', 0x80, 0x01, 0, 0x7f};
    signed char tiny[3] = "hi";
    struct badge b = {"desk", 7};
    memset(b.label + 5, '#', 6);
    printf("%s %.4s %s %s %s %s %s %d\\n", name, full, first, city, (char *)raw, (char *)tiny,
           b.label, b.id); /* SEE */
    return 0;
}
"""
SELECTORS = "name,full,first,city,raw,tiny,b,b.label"


@pytest.fixture
def arrays(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "arrays" / "arrays.c"
    path.parent.mkdir()
    path.write_text(SOURCE)
    return path


@pytest.mark.timeout(240)
def test_a_char_array_shows_its_text_up_to_its_first_nul(arrays, capsys):
    line = next(i for i, text in enumerate(SOURCE.splitlines(), 1) if "/* SEE */" in text)
    code = main(
        ["probe", str(arrays), "--at", f"arrays.c:{line}", "--capture", SELECTORS, "--json"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    rows = {row["selector"]: row for row in result["rows"]}
    shown = {selector: rows[selector].get("value") for selector in SELECTORS.split(",")}
    assert shown == {
        # Leftovers after the NUL that are not NULs: counted, never shown as text.
        "name": '"Okafor" (then a NUL and 25 bytes not shown)',
        # No NUL: every byte, and it says so.
        "full": '"abcd" (no NUL in its 4 bytes)',
        # A NUL first: no text.
        "first": '"" (then a NUL and 5 bytes not shown)',
        # UTF-8 shows as its characters; every byte after the text is a NUL.
        "city": '"Malmö" (then 10 NUL bytes)',
        # Every byte that is no printable character is `\\xNN`, never `\\U00000001`.
        "raw": '"ok\\x80\\x01" (then a NUL and 1 byte not shown)',
        "tiny": '"hi" (then 1 NUL byte)',
        "b": '{label = "desk" (then a NUL and 7 bytes not shown), id = 7}',
        "b.label": '"desk" (then a NUL and 7 bytes not shown)',
    }, shown
    # `--json` keeps every byte.
    assert rows["name"]["buffer"] == '"Okafor\\0' + "\\x01" * 25 + '"', rows["name"]
    assert rows["full"]["buffer"] == '"abcd"' and rows["first"]["buffer"] == '"\\0xy\\0\\0\\0"'
    assert rows["raw"]["buffer"] == '"ok\\x80\\x01\\0\\x7f"', rows["raw"]
    assert "buffer" not in rows["b"], rows["b"]
    assert "Okafor abcd" in result["output"], result["output"]
    # SQL's text() gives the text up to the NUL; NULL where its bytes are no UTF-8 text.
    query = (
        "SELECT selector, text(value, language, type) AS t FROM evidence WHERE run_id = 1 "
        "ORDER BY selector"
    )
    assert main(["sql", query, "--json"]) == 0
    texts = {row["selector"]: row["t"] for row in json.loads(capsys.readouterr().out)["rows"]}
    assert texts == {
        "b": None,
        "b.label": "desk",
        "city": "Malmö",
        "first": "",
        "full": "abcd",
        "name": "Okafor",
        "raw": None,
        "tiny": "hi",
    }, texts
