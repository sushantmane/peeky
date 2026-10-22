"""#195: a selector names a local or field by its Unicode identifier, exactly as the
debugger lists it (never normalized), and never through an invisible character."""

import re
import string

import pytest

from peeky.identifiers import is_located, is_name, name_end, twin_hint
from peeky.probe import parse_selector

NFC, NFD = "gr\u00f6\u00dfe", "gro\u0308\u00dfe"


@pytest.mark.parametrize(
    "text, segments",
    [
        ("größe", ["größe"]),
        ("zähler.ñ", ["zähler", "ñ"]),
        ("p.größe[0].ñ['k']", ["p", "größe", "[0]", "ñ", "['k']"]),
        ("_ünder.名前", ["_ünder", "名前"]),
        ("नाम.मान", ["नाम", "मान"]),  # Devanagari vowel signs are combining marks
        # Shadowed roots, with or without spaces around `@`.
        ("größe @ main.rs:81.a", ["größe @ main.rs:81", "a"]),
        ("größe@main.rs:81.zähler", ["größe @ main.rs:81", "zähler"]),
        # Kept as written: an NFD spelling stays NFD, a ligature a ligature.
        (NFD, [NFD]),
        (f"p.{NFD}", ["p", NFD]),
        ("\ufb01x", ["\ufb01x"]),
    ],
)
def test_unicode_names_are_selected_as_written(text, segments):
    assert parse_selector(text) == segments


@pytest.mark.parametrize(
    "text, invisible",
    [
        ("a\u200bb", "U+200B"),  # zero width space
        ("a\u200db", "U+200D"),  # zero width joiner: str.isidentifier() accepts it
        ("a\u200cb", "U+200C"),
        ("p.a\u200d", "U+200D"),
        ("\u3164", "U+3164"),  # Hangul filler: str.isidentifier() accepts it
        ("a\ufe0fb", "U+FE0F"),  # variation selector
        ("a\u034fb", "U+034F"),  # combining grapheme joiner
        ("p.\u202ex", "U+202E"),  # right-to-left override
        ("a\u2066b", "U+2066"),  # left-to-right isolate
        ("a\u00adb", "U+00AD"),  # soft hyphen
        ("\ufeffa", "U+FEFF"),
    ],
)
def test_invisible_characters_are_never_part_of_a_name(text, invisible):
    with pytest.raises(ValueError, match=re.escape(f"it contains {invisible}, an invisible")):
        parse_selector(text)


@pytest.mark.parametrize(
    "text",
    ["x²", "€uro", "a€", "p.x²", "größe+1", "größe()", "p.größe(1)", "a\u3000b", "１a", "a b"],
)
def test_unicode_operators_spaces_and_non_identifiers_are_refused(text):
    with pytest.raises(ValueError, match="Invalid selector"):
        parse_selector(text)


def _old_name_end(text, start=0):
    match = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}").match(text, start)
    return match.end() if match else start


def test_ascii_names_end_exactly_where_the_ascii_grammar_ended():
    alphabet = [chr(code) for code in range(128)]
    texts = [a + b for a in alphabet for b in alphabet]
    texts += ["a" * 127, "a" * 128, "a" * 129, "_" * 200 + ".b", string.printable]
    for text in texts:
        for start in range(len(text)):
            assert name_end(text, start) == _old_name_end(text, start), (text, start)
        ascii_name = bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text))
        assert is_name(text, limit=None) == ascii_name, text


def test_located_names_follow_the_place_pattern():
    assert is_located("größe @ main.c:12", r" @ [^\s:@]+:[0-9]+")
    assert not is_located("größe @ main.c:x", r" @ [^\s:@]+:[0-9]+")
    assert not is_located("a\u200db @ main.c:1", r" @ [^\s:@]+:[0-9]+")
    assert not is_located("(Return Value) @ a.c:1", r" @ .+")


class Tree:
    def __init__(self, tree):
        self.tree = tree

    def dispatch(self, operation, args):
        assert operation == "expand"
        return {"variables": self.tree[args["handle"]], "truncated": False}


def snapshot(*variables):
    return {"scopes": [{"name": "Locals", "reference": 7, "variables": list(variables)}]}


def test_two_locals_whose_names_differ_only_by_normalization_are_never_conflated():
    # javac and clang keep `größe` (NFC) and `größe` (NFD) as two variables.
    from peeky.probe import _resolve

    rows = (
        {"name": NFC, "type": "int", "value": "1", "variablesReference": 0},
        {"name": NFD, "type": "int", "value": "2", "variablesReference": 0},
    )
    assert _resolve(Tree({}), {}, snapshot(*rows), parse_selector(NFC))["value"] == "1"
    assert _resolve(Tree({}), {}, snapshot(*rows), parse_selector(NFD))["value"] == "2"


@pytest.mark.parametrize(
    "selector, listed",
    [
        (NFD, NFC),  # Python and Rust list the NFC spelling of an NFD source name
        (NFC, NFD),
        ("fix", "\ufb01x"),  # Rust and clang keep a ligature; Python applies NFKC
        ("\ufb01x", "fix"),
    ],
)
def test_a_spelling_that_differs_only_by_normalization_is_refused_with_the_listed_one(
    selector, listed
):
    from peeky.probe import _resolve

    row = {"name": listed, "type": "int", "value": "1", "variablesReference": 0}
    with pytest.raises(ValueError) as caught:
        _resolve(Tree({}), {}, snapshot(row), parse_selector(selector))
    message = str(caught.value)
    assert message.startswith("not found; visible: ")
    assert f"the debugger lists {listed!r} (written {ascii(listed)})" in message
    assert f"differs from {ascii(selector)} only by Unicode normalization" in message


def test_a_field_spelled_otherwise_is_refused_with_the_listed_spelling():
    from peeky.probe import _resolve

    holder = {"name": "p", "type": "Point", "value": "{...}", "variablesReference": 1}
    fields = [{"name": NFC, "type": "int", "value": "3", "variablesReference": 0}]
    found = _resolve(Tree({1: fields}), {}, snapshot(holder), parse_selector(f"p.{NFC}"))
    assert found["value"] == "3"
    with pytest.raises(ValueError) as caught:
        _resolve(Tree({1: fields}), {}, snapshot(holder), parse_selector(f"p.{NFD}"))
    assert f"not found in 'p' (Point); children: {NFC}" in str(caught.value)
    assert f"the debugger lists {NFC!r} (written {ascii(NFC)})" in str(caught.value)


def test_twins_are_only_normalization_variants_never_other_names():
    assert twin_hint("größe", ["größe @ a.c:3", "grösse", "GRÖSSE", "größer"]) == ""
    assert twin_hint("t", ["t @ two.c:18"]) == ""
    hint = twin_hint(NFD, [f"{NFC} @ a.c:3", f"{NFC} @ a.c:5"])
    assert "differ from" in hint and "select one as listed" in hint


def test_a_set_of_a_root_spelled_otherwise_names_the_listed_spelling():
    from peeky.errors import Refused
    from peeky.probe import apply_set

    class Worker:
        def dispatch(self, operation, args):
            assert operation == "inspect"
            return snapshot({"name": NFC, "type": "int", "value": "1", "variablesReference": 0})

    with pytest.raises(Refused) as caught:
        apply_set(Worker(), {}, NFD, "5")
    assert caught.value.code == "NOT_FOUND"
    assert f"the debugger lists {NFC!r} (written {ascii(NFC)})" in str(caught.value)


def test_children_of_a_unicode_name_are_offered_with_their_selector():
    from peeky.probe import _child_selector

    assert _child_selector("p", "größe") == "p.größe"
    assert _child_selector("größe", "ñ") == "größe.ñ"
    assert _child_selector("p", "a\u200db") is None


def test_inspect_offers_unicode_names_and_marks_invisible_ones_not_selectable():
    from peeky.cli import _variables_text

    text = _variables_text(
        {
            "frames": [{"source": {"path": "a.c"}, "line": 3}],
            "scopes": [
                {
                    "name": "Locals",
                    "variables": [
                        {"name": "größe", "type": "S", "value": "{...}", "variablesReference": 4},
                        {"name": "zähler", "type": "int", "value": "1", "settable": True},
                        {"name": "ñ @ a.c:9", "type": "int", "value": "2"},
                        {"name": "a\u200db", "type": "int", "value": "3"},
                    ],
                }
            ],
        }
    )
    assert "größe: S = {...} (children: peeky session get größe)" in text
    assert "zähler: int = 1  [settable]\n" in text + "\n"
    assert "ñ @ a.c:9: int = 2\n" in text + "\n"
    assert "a\u200db: int = 3 [not selectable]" in text


@pytest.mark.parametrize(
    "name, writable",
    [
        ("zähler", True),
        ("zähler @ main.c:15", True),
        (NFD, True),
        ("a\u200bb", False),
        ("a\u200db", False),
        ("(Return Value)", False),
    ],
)
def test_lldb_integers_with_unicode_names_are_writable(name, writable):
    from peeky.native import _writable_integer

    variable = {"name": name, "type": "int", "variablesReference": 0}
    assert _writable_integer(variable) is writable


def test_the_worker_accepts_the_names_the_cli_selects():
    from peeky.worker import _name_list

    assert _name_list(["größe", NFD, "ñ @ main.rs:3", "x"])
    assert not _name_list(["a\u200db"])
    assert not _name_list(["\u3164"])
    assert not _name_list(["größe @ main.rs:x"])
    for selector in ("größe", "größe @ main.rs:81.a", f"p.{NFD}"):
        assert _name_list([parse_selector(selector)[0]])


@pytest.mark.parametrize(
    "variable, settable",
    [
        ({"name": "zähler", "type": "int"}, True),
        ({"name": "a\u200db", "type": "int"}, False),
    ],
)
def test_java_locals_with_unicode_names_are_settable(variable, settable):
    from peeky.java import JavaSession

    assert JavaSession.settable(None, variable) is settable
    member = JavaSession.member_settable(None, variable)
    assert (member is None) is settable


def test_inspect_says_which_spelling_each_look_alike_row_is():
    from peeky.cli import _variables_text
    from peeky.identifiers import look_alike

    rows = [
        {"name": NFC, "type": "Box", "value": "Box@1", "variablesReference": 2},
        {"name": NFD, "type": "int", "value": "7", "settable": True},
        {"name": "t @ a.c:3", "type": "int", "value": "1"},
        {"name": "t @ a.c:5", "type": "int", "value": "2"},
    ]
    assert look_alike([row["name"] for row in rows]) == {NFC, NFD}
    text = _variables_text(
        {
            "frames": [{"source": {"path": "A.java"}, "line": 3}],
            "scopes": [{"name": "Local", "variables": rows}],
        }
    )
    assert (
        f"{NFC}: Box = Box@1 (children: peeky session get {NFC}) [written {ascii(NFC)}: "
        "another name here differs from it only by Unicode normalization]"
    ) in text
    assert f"{NFD}: int = 7  [settable] [written {ascii(NFD)}: another" in text
    assert "t @ a.c:3: int = 1\n" in text
