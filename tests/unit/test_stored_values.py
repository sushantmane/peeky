"""SQL's elements(value, language) and text(value, language, type) (twelfth evaluation,
desks 1 and rainfall 6): a stored value is its language's notation, never JSON, and a
Python string keeps its quotes, so `json_each(substr(value, instr(value, '[')))` failed
with `malformed JSON` on `int[3]@55 [490, 484, 497]`, and `value = 'Göteborg'` matched
nothing. Review of PR #347: debugpy shows a Python value by its own `__repr__`, which a
class may write as `1` or `'Göteborg'`; a C `long double`'s `1e-4000` read as 0; and a
Java char is bare (`+`)."""

import json
import re
from pathlib import Path

import pytest

from peeky import evidence

DOCS = Path(__file__).resolve().parents[2] / "docs"


def _run(rows, language):
    return {
        "probe": {
            "program": "a.py",
            "line": 8,
            "capture": list(dict.fromkeys(row[1] for row in rows)),
            "set": [],
            "hits": 1,
            "args": [],
        },
        "status": "completed",
        "error": None,
        "hits": max(row[0] for row in rows),
        "seconds": 1.0,
        "pauses_ms": [1.0],
        "output": "",
        "rows": [
            dict(hit=row[0], phase="observed", line=8, selector=row[1], value=row[2])
            | ({"type": row[3]} if len(row) > 3 else {})
            for row in rows
        ],
        "interventions": [],
        "warnings": [],
    }


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    path = tmp_path_factory.mktemp("store") / "evidence" / "evidence.db"
    evidence.record(path, _run([(1, "x", "1")], "python"), language="python")
    return path


def _call(store, function, value, language, kind=None):
    arguments = ":value, :language, :kind" if function == "text" else ":value, :language"
    params = {"value": value, "language": language, "kind": kind}
    [row] = evidence.query(store, f"SELECT {function}({arguments}) AS got", params)["rows"]
    return row["got"]


@pytest.mark.parametrize(
    "value, language, listed",
    [
        # Java: an array (desks: `int[3]@55 [490, 484, 497]`) and an ArrayList, by the count
        # its type or size gives.
        ("int[3]@55 [490, 484, 497]", "java", [490, 484, 497]),
        ("int[3]@55 [490, 484, 497]", "java-attach", [490, 484, 497]),
        ("int[0]@64 []", "java", []),
        ('String[2]@60 ["Göteborg", "say \\"hi\\""]', "java", ["Göteborg", 'say "hi"']),
        ("char[3]@61 ['a', '\\'', '\\u00e9']", "java", ["a", "'", "é"]),
        ('String[1]@62 ["\\ud83d\\ude00 Łódź"]', "java", ["😀 Łódź"]),
        ("boolean[2]@63 [true, false]", "java", [True, False]),
        ("double[2]@64 [1.5, -2.0E-5]", "java", [1.5, -2.0e-5]),
        ('Object[2]@65 [null, "x"]', "java", [None, "x"]),
        ('ArrayList@142 size=2 ["Kraków", "Tromsø"]', "java", ["Kraków", "Tromsø"]),
        # A String element of 38 characters that doesn't end in `…` was not cut.
        ('String[1]@66 ["' + "a" * 38 + '"]', "java", ["a" * 38]),
        ('String[1]@66 ["' + "a" * 36 + '…"]', "java", ["a" * 36 + "…"]),
        # C and C++: an array, a container (by its count), nested, a reference's.
        ("[5, 6, 7]", "c", [5, 6, 7]),
        ("[-1, 0, 2147483647]", "c", [-1, 0, 2147483647]),
        ("[[1, 2, 3], [4, 5, 6]]", "cpp", [[1, 2, 3], [4, 5, 6]]),
        ("size=2 [1, 2]", "cpp", [1, 2]),
        ("size=1 {7}", "cpp", [7]),
        ("size=0 []", "cpp", []),
        ("[size=2 [1, 2], size=0 []]", "cpp", [[1, 2], []]),
        ("0x000000016fdff2c8 size=2 [1.5, 3.1400001]", "cpp", [1.5, 3.1400001]),
        # Decimals a double holds to as many digits as they have.
        ("[0.10000000000000001, 1.0E+10, 5e-324, -0.0]", "c", [0.1, 1e10, 5e-324, -0.0]),
        ('["Zoë", "a\\tb", ""]', "cpp", ["Zoë", "a\tb", ""]),
        ("[L'a', L'\\0', U'é', 'b']", "cpp", ["a", "\0", "é", "b"]),
        ('[u8"Åsa", L"Łódź"]', "cpp", ["Åsa", "Łódź"]),
        ("[true, false]", "c", [True, False]),
        # Rust: a Vec or slice of numbers, chars and bools.
        ("[1, 2]", "rust", [1, 2]),
        ("[]", "rust", []),
        ("[1.0, 1e-7, -0.0]", "rust", [1.0, 1e-7, -0.0]),
        ("['x', 'é', '\\u{301}', '\\'']", "rust", ["x", "é", "\u0301", "'"]),
        ("[-9223372036854775808, 9223372036854775807]", "rust", [-(2**63), 2**63 - 1]),
    ],
)
def test_elements_reads_each_languages_list_exactly(store, value, language, listed):
    shown = _call(store, "elements", value, language)
    assert json.loads(shown) == listed
    # SQLite reads it, and each number stays a number.
    rows = evidence.query(store, "SELECT value, type FROM json_each(:listed)", {"listed": shown})[
        "rows"
    ]
    assert [row["value"] for row in rows if row["type"] in ("integer", "real")] == [
        item for item in listed if type(item) in (int, float)
    ]


@pytest.mark.parametrize(
    "value, language",
    [
        # Cut previews: the first 8 elements, then `…` (Java, C, C++), `...` (Python, Rust).
        ("boolean[12]@171 [false, false, true, true, true, true, false, false, …]", "java"),
        ("[300, 340, 370, 385, 390, 390, 390, 375, …]", "c"),
        ("size=9 [1, 2, 3, 4, 5, 6, 7, 8, …]", "cpp"),
        ("[0, 1, ...]", "python"),
        ("[[...], 1]", "python"),
        ("[...]", "python"),
        ("[1, 2, ...] (first 2 of 300)", "rust"),
        # Python: debugpy shows each element by its own __repr__ (a NumberObject in a list
        # shows `[1]`), and the store keeps no element's type, so no list is read.
        ("[1]", "python"),
        ("['Göteborg', 'Kraków']", "python"),
        ("[1.5, True, None]", "python"),
        ("(5, 6)", "python"),
        ("[]", "python"),
        # A count that disagrees with the elements shown.
        ("int[3]@55 [490, 484]", "java"),
        ("ArrayList@142 size=3 [1, 2]", "java"),
        ("size=3 [1, 2]", "cpp"),
        # Elements that are no number, string, char, boolean, null or list.
        ("Seat[2]@70 [Seat@71, Seat@72]", "java"),
        ("int[2][]@80 [int[3]@81, int[3]@82]", "java"),
        ("Integer[1]@83 [Integer@84]", "java"),
        ("[{'text': 'Brot', 'level': 1}]", "python"),
        ("[<object object at 0x1047a8f30>]", "python"),
        ("[b'ab']", "python"),
        ("[inf, nan]", "python"),
        ("[{x = 1, y = 2}]", "c"),
        ("[{…}, {…}]", "cpp"),
        ("[[…], […]]", "cpp"),
        ("[0x0000000100004000, 0x0]", "c"),
        ("[M1, M2]", "c"),
        ("[<no value>]", "c"),
        ("size=1 {1: 10}", "cpp"),
        ("[Lap { number: 1, start: 0, end: 804 }]", "rust"),
        ("[(1, 2)]", "rust"),
        # Strings that may be cut, or are no text.
        ('String[1]@66 ["' + "a" * 37 + '…"]', "java"),
        ("['" + "a" * 85 + "..." + "a" * 42 + "']", "python"),
        ('["abcdefghijklmnopqrstuvwxyzabcdefghij"...]', "cpp"),
        ("['\\x90']", "c"),
        ('["\\xff"]', "c"),
        ('String[1]@67 ["\\ud83d"]', "java"),
        ("['\\ud800']", "python"),
        # Integers past 64 bits, which SQLite would read as an inexact REAL.
        ("[18446744073709551615]", "rust"),
        # A C or C++ `long double`'s decimals, which a double would read as 0 or 1.
        ("[1e-4000]", "cpp"),
        ("[1.0000000000000000001]", "cpp"),
        ("[3.14159265358979323851]", "c"),
        ("[1.18973149535723176502e+4932]", "c"),
        ("[2e-324]", "c"),
        # Not a list, or not this language's notation.
        ("{7}", "cpp"),
        ("490", "java"),
        ("'Göteborg'", "python"),
        ("(5)", "python"),
        ("[1,2]", "python"),
        ("int[3]@55 [490, 484, 497]", "python"),
        ("['ab']", "rust"),
        ("[1, 2]", "java"),
        ("[1, 2]", None),
        ("[1, 2]", "go"),
        (None, "python"),
        # The worker keeps a value's first 2000 characters, and marks no cut there.
        pytest.param("[" + ", ".join(["1"] * 667) + "]", "python", id="2001-characters"),
    ],
)
def test_elements_is_null_unless_every_element_is_read_exactly(store, value, language):
    assert _call(store, "elements", value, language) is None


@pytest.mark.parametrize(
    "value, language, kind, shown",
    [
        # Python (rainfall: `'Göteborg'` is stored with its quotes), a `str` by its type.
        ("'Göteborg'", "python", "str", "Göteborg"),
        ('"Île-d\'Yeu"', "python", "str", "Île-d'Yeu"),
        ("'a\\nb\\t\\\\'", "python", "str", "a\nb\t\\"),
        ("'\\xe9\\u0142\\U0001f600'", "python", "str", "éł😀"),
        ("''", "python", "str", ""),
        # Java: a String is java-debug's text between quotes, unescaped; a char is bare.
        ('"Málaga"', "java", "String", "Málaga"),
        ('"say "hi" \\n"', "java", "java.lang.String", 'say "hi" \\n'),
        ('""', "java", "String", ""),
        ("+", "java", "char", "+"),
        ("é", "java-attach", "char", "é"),
        ('"', "java", "char", '"'),
        # C and C++: a literal, a wide one, octal NULs, a pointer's or reference's text.
        ('"Zürich"', "c", "char[16]", "Zürich"),
        ('L"Žilina"', "cpp", "const wchar_t *", "Žilina"),
        ('u"Ålesund"', "cpp", "std::u16string", "Ålesund"),
        ('"a\\tb\\000c\\"d"', "c", "char[8]", 'a\tb\0c"d'),
        ('"\\U0001f600"', "cpp", "std::string", "😀"),
        ('0x0000000100003f80 "Nîmes"', "c", "const char *", "Nîmes"),
        ("'A'", "c", "char", "A"),
        ("'\\0'", "cpp", "char", "\0"),
        # Rust: `{:?}` escapes and JSON's, both of which Peeky writes.
        ('"Reykjavík"', "rust", "String", "Reykjavík"),
        ('"say \\"hi\\""', "rust", "&str", 'say "hi"'),
        ('"a\\u{1b}b\\u001bc\\0"', "rust", "String", "a\x1bb\x1bc\0"),
        ("'\\u{e9}'", "rust", "char", "é"),
        ("'\\''", "rust", "char", "'"),
    ],
)
def test_text_decodes_each_languages_string(store, value, language, kind, shown):
    assert _call(store, "text", value, language, kind) == shown


@pytest.mark.parametrize(
    "value, language, kind",
    [
        # A Python value other than a `str`: debugpy shows it by its own __repr__, which a
        # class may write as `'Göteborg'` (`return "'Göteborg'"`), and a str subclass too.
        ("'Göteborg'", "python", "City"),
        ("'Göteborg'", "python", "MyStr"),
        ("'Göteborg'", "python", None),
        ("'Göteborg'", "python", ""),
        # A Java char is bare: a quoted one, or a longer text, is no char's value.
        ("'A'", "java", "char"),
        ("ab", "java", "char"),
        ("+", "java", "int"),
        ('"x"', "java", "char"),
    ],
)
def test_text_needs_the_rows_type(store, value, language, kind):
    assert _call(store, "text", value, language, kind) is None


@pytest.mark.parametrize(
    "value, language",
    [
        # Cut: LLDB's `...` after a C literal; Rust's `(first N of M bytes)`; a Java String
        # the worker cut at 2000 characters, ending inside its text at a `"`.
        ('"abc"...', "c"),
        ('"abc" (first 3 of 9 bytes)', "rust"),
        pytest.param('"' + 'say "hi" ' * 222 + '"', "java", id="2000-characters"),
        ('"abc" (note)', "cpp"),
        ('"abc', "python"),
        ("'abc\\'", "python"),
        # Bytes that are no text, and lone surrogates.
        ('"\\xff"', "c"),
        ('"\\303\\251"', "c"),
        ("'\\x90'", "c"),
        ("'\\ud800'", "python"),
        ("b'ab'", "python"),
        # A char literal holds one character.
        ("'ab'", "java"),
        ("'Göteborg'", "rust"),
        # Not a string, or not this language's notation.
        ("490", "python"),
        ("int[3]@55 [490, 484, 497]", "java"),
        ("['Göteborg']", "python"),
        ("Göteborg", "java"),
        ("0x0000000100003f80", "c"),
        ('0x0000000100003f80 "Maya"', "rust"),
        ("'Göteborg' 'x'", "python"),
        ("'Göteborg'", None),
        (None, "python"),
    ],
)
def test_text_is_null_unless_decoded_exactly(store, value, language):
    kind = {"python": "str", "java": "String"}.get(language, "char *")
    assert _call(store, "text", value, language, kind) is None


cities_shown = ("Lisboa", "Évora", "Faro")


def _recorded(tmp_path):
    path = tmp_path / "evidence" / "evidence.db"
    for _ in range(3):
        evidence.record(path, _run([(1, "start", "1")], "python"), language="python")
    counts = [(1, "counts", "int[4]@61 [3, 0, 7, 2]"), (2, "counts", "int[4]@61 [3, 4, 7, 2]")]
    evidence.record(path, _run(counts, "java"), language="java")
    cities = [(hit, "city", repr(name), "str") for hit, name in enumerate(cities_shown, 1)]
    # A class whose __repr__ reads like a str's: no match.
    cities.append((4, "city", "'Évora'", "Place"))
    evidence.record(path, _run(cities, "python"), language="python")
    return path


def _cookbook_sql(heading):
    text = (DOCS / "COOKBOOK.md").read_text()
    start = text.index(heading)
    section = text[start : text.index("\n## ", start)]
    return re.findall(r'peeky sql "(.*?)"', section, re.S)


def test_the_cookbooks_examples_read_elements_and_text(tmp_path):
    path = _recorded(tmp_path)
    listed, matched = _cookbook_sql("### 4.9 A List's Elements and a String's Text")
    rows = evidence.query(path, listed)["rows"]
    assert [(row["hit"], row["i"], row["value"]) for row in rows] == [
        (1, 0, 3),
        (1, 1, 0),
        (1, 2, 7),
        (1, 3, 2),
    ]
    assert evidence.query(path, matched)["rows"] == [{"hit": 2, "value": "'Évora'"}]
    # Where the value itself matches nothing.
    assert not evidence.query(path, "SELECT 1 FROM evidence WHERE value = 'Évora'")["rows"]
    totals = evidence.query(
        path,
        "SELECT v.hit, SUM(e.value) AS total FROM evidence v, "
        "json_each(elements(v.value, v.language)) e WHERE v.run_id = 4 GROUP BY v.hit",
    )["rows"]
    assert totals == [{"hit": 1, "total": 12}, {"hit": 2, "total": 16}]


def test_a_failing_json_function_keeps_sqlites_message_and_names_elements(tmp_path):
    path = _recorded(tmp_path)
    with pytest.raises(ValueError) as failed:
        evidence.query(
            path,
            "SELECT j.value FROM evidence v, json_each(substr(v.value, instr(v.value, '['))) j "
            "WHERE v.run_id = 4",
        )
    message = str(failed.value)
    assert "malformed JSON" in message and "elements(value, language)" in message
    hint = message[message.index("malformed JSON") + len("malformed JSON") :]
    assert len(hint) <= 150
    for call in ("elements(value)", "text(value, language)"):
        with pytest.raises(ValueError) as failed:
            evidence.query(path, f"SELECT {call} FROM evidence")
        message = str(failed.value)
        assert f"wrong number of arguments to function {call.split('(')[0]}()" in message
        assert "elements(value, language) and text(value, language, type)" in message
    # json_each's own `value` column, beside the view's.
    with pytest.raises(ValueError) as failed:
        evidence.query(
            path,
            "SELECT e.value FROM evidence, json_each(elements(value, language)) e WHERE run_id = 4",
        )
    message = str(failed.value)
    assert "ambiguous column name: value" in message and "v.value" in message


def test_the_schema_lists_each_function_on_one_line(tmp_path):
    path = _recorded(tmp_path)
    described = evidence.schema(path)
    functions = evidence.schema_part_text(evidence.schema_part(described, ["functions"]))
    for name in ("elements(value, language)", "text(value, language, type)"):
        [line] = [line for line in functions.splitlines() if line.startswith(f"  {name}: ")]
        assert "COOKBOOK 4.9" in line
    index = evidence.schema_index(described)
    assert "elements" in index and "text" in index.split("SQL functions Peeky adds:")[1]
    value = described["tables"]["captures"]["columns"]["value"]
    assert "'Évora'" in value and "text(value, language, type)" in value
