"""A selector names a local or field by its Unicode identifier, exactly as the
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


# What the worker takes over HTTP is held to the selector's name policy,
# and only a row a selector can read whole is offered or marked [settable].
REFUSED_NAMES = [
    "a\u200db",  # an invisible character: the CLI refuses it
    "a\u200bb",
    "\u3164",
    "p.\u202ex",
    "x;expr evil",  # punctuation and spaces
    "a b",
    "x.y",
    "s @ a.c:3; rm",
    "ñ" * 129,  # past the 128 characters a selector's name may hold
    "n" * 129,
    "",
]


class Listing:
    """A debugger whose every scope and value lists one variable named ``name``."""

    def __init__(self, name):
        self.name, self.calls = name, []

    def request(self, command, args):
        self.calls.append(command)
        return {"variables": [{"name": self.name, "value": "1", "variablesReference": 0}]}

    def close(self):
        pass


def _stopped(tmp_path, name):
    from peeky.worker import DebugWorker

    worker = DebugWorker(tmp_path)
    worker.dap = Listing(name)
    worker.session, worker.state, worker.stop_id, worker.thread_id = "s", "stopped", "t", 1
    worker.capabilities = {"supports_set_variable": True, "supports_member_set": True}
    worker.references, worker.reference_adapter = {7}, {7: 7}
    return worker


@pytest.mark.parametrize("name", REFUSED_NAMES)
def test_the_worker_never_changes_a_variable_no_selector_can_name(tmp_path, name):
    worker = _stopped(tmp_path, name)
    for target, message in (({"reference": 7}, "a local's name"), ({"handle": 3}, "a field's")):
        with pytest.raises(ValueError, match=f"set_variable's name must be {message}"):
            worker.dispatch(
                "set_variable",
                {"session_id": "s", "stop_id": "t", **target, "name": name, "value": "1"},
            )
    assert worker.dap.calls == []


@pytest.mark.parametrize(
    "target, name",
    [
        ({"reference": 7}, "ñ" * 128),
        ({"reference": 7}, NFD),
        ({"reference": 7}, "größe @ main.rs:3"),
        ({"handle": 3}, "zähler"),
        ({"handle": 3}, "[3]"),  # LLDB's element
        ({"handle": 3}, "3"),  # java-debug's
    ],
)
def test_the_worker_takes_the_names_a_change_can_give(target, name):
    from peeky.worker import DebugWorker

    DebugWorker.validate(
        "set_variable", {"session_id": "s", "stop_id": "t", **target, "name": name, "value": "1"}
    )


@pytest.mark.parametrize("name", ["[03]", "[-1]", "03", "-1", "['k']", "[#0]"])
def test_a_member_change_names_an_element_as_the_debugger_does(tmp_path, name):
    from peeky.worker import DebugWorker

    with pytest.raises(ValueError, match="set_variable's name must be a field's"):
        DebugWorker.validate(
            "set_variable",
            {"session_id": "s", "stop_id": "t", "handle": 3, "name": name, "value": "1"},
        )


@pytest.mark.parametrize("name", REFUSED_NAMES[:-1])
def test_a_launch_change_or_an_inspection_takes_only_selectors(name):
    from peeky.worker import DebugWorker

    if name != "x.y":  # a field's selector: a change's, never an inspection's name
        with pytest.raises(ValueError, match="sets: Invalid selector"):
            DebugWorker.validate("launch", {"program": "a.c", "sets": [{"line": 3, "name": name}]})
    for field in ("names", "globals", "previews"):
        with pytest.raises(ValueError, match=f"{field} must be"):
            DebugWorker.validate("inspect", {"session_id": "s", field: [name]})


def test_http_requests_are_held_to_the_name_policy(tmp_path):
    import threading

    from peeky.client import Client
    from peeky.worker import make_server

    token = "unit-test-secret-token-1234-0123456789abcdef"
    server = make_server(tmp_path, token, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", token)
        for name in ("a\u200db", "x;expr evil", "ñ" * 129):
            with pytest.raises(Exception, match="set_variable's name must be a local's name"):
                client.call(
                    "set_variable", session_id="s", stop_id="t", reference=1, name=name, value="1"
                )
            with pytest.raises(Exception, match="set_variable's name must be a field's"):
                client.call(
                    "set_variable", session_id="s", stop_id="t", handle=1, name=name, value="1"
                )
            with pytest.raises(Exception, match="names must be a list"):
                client.call("inspect", session_id="s", stop_id="t", names=[name])
            with pytest.raises(Exception, match="sets: Invalid selector"):
                client.call("launch", program="a.c", sets=[{"line": 3, "name": name}])
            with pytest.raises(Exception, match="Invalid selector"):
                client.call("probe", at="a.c:3", capture=[name])
            with pytest.raises(Exception, match="Invalid selector"):
                client.call("probe", at="a.c:3", set={name: "1"})
            with pytest.raises(Exception, match="Invalid selector"):
                client.call("probe", at="a.c:3", set_at=[[4, 1, name, "1"]])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


LONG, LONGER = "n" * 128, "n" * 129


def test_a_row_past_128_characters_is_never_offered_or_marked_settable():
    from peeky.cli import _variables_text

    rows = [
        {"name": LONG, "type": "S", "value": "{...}", "variablesReference": 4},
        {"name": LONGER, "type": "S", "value": "{...}", "variablesReference": 5},
        # A file name a selector cannot write (a space) is no selectable place either.
        {"name": "t @ my file.c:3", "type": "int", "value": "1"},
        {"name": "t @ main.c:5", "type": "int", "value": "2"},
    ]
    text = _variables_text(
        {
            "frames": [{"source": {"path": "a.c"}, "line": 3}],
            "scopes": [{"name": "Locals", "variables": rows}],
        }
    )
    assert f"{LONG}: S = {{...}} (children: peeky session get {LONG})" in text
    assert f"{LONGER}: S = {{...}} [not selectable]" in text
    assert "t @ my file.c:3: int = 1 [not selectable]" in text
    assert "t @ main.c:5: int = 2\n" in text + "\n"


@pytest.mark.parametrize(
    "row, changeable, reachable",
    [
        ({"name": LONG}, True, True),
        ({"name": LONGER}, False, False),
        ({"name": f"{LONG} @ main.c:15"}, True, True),
        ({"name": f"{LONGER} @ main.c:15"}, False, False),
        # LLDB's spelling of a shadowed local in a file no selector can write: a change
        # may name it, and a selector reaches it only through its plain name.
        ({"name": "n @ my file.c:15"}, True, False),
        ({"name": "n @ my file.c:15", "selected_as": "n"}, True, True),
        ({"name": "n @ my file.c:15", "selected_as": "m"}, True, False),
        ({"name": "n @ my file.c:15", "selected_as": "n\u200d"}, True, False),
        ({"name": "n\u200d @ my file.c:15", "selected_as": "n\u200d"}, False, False),
        ({"name": "n @ a:b.c:15", "selected_as": "n"}, False, False),
        ({"name": "n @ " + "f" * 256 + ":15", "selected_as": "n"}, False, False),
    ],
)
def test_lldb_integers_are_marked_only_where_a_selector_reaches_them(row, changeable, reachable):
    from peeky.identifiers import reachable as reaches
    from peeky.native import _writable_integer

    variable = {**row, "type": "int", "variablesReference": 0}
    assert _writable_integer(variable) is changeable
    assert reaches(row) is reachable
    if reachable and "selected_as" not in row:
        assert parse_selector(row["name"]) == [row["name"]]


def test_java_locals_and_members_past_128_characters_are_not_settable():
    from peeky.java import JavaSession

    assert JavaSession.settable(None, {"name": LONG, "type": "int"}) is True
    assert JavaSession.settable(None, {"name": LONGER, "type": "int"}) is False
    assert JavaSession.member_settable(None, {"name": LONGER, "type": "int"}) == (
        "no selector names it"
    )


# A shadowed local LLDB names in a file a selector cannot write
# (`t @ my file.c:3`) is read and changed through its plain name.
SHADOWED = {
    "name": "t @ my file.c:5",
    "type": "int",
    "value": "2",
    "variablesReference": 0,
    "selected_as": "t",
}
OUTER = {"name": "t @ my file.c:3", "type": "int", "value": "1", "variablesReference": 0}


def test_a_shadowed_local_in_an_oddly_named_file_is_changed_through_its_plain_name():
    from peeky.probe import apply_set
    from peeky.worker import DebugWorker

    sent = []

    class Worker:
        def dispatch(self, operation, args):
            if operation == "inspect":
                return snapshot(SHADOWED, OUTER)
            assert operation == "set_variable"
            DebugWorker.validate(operation, args)  # the worker's own check
            sent.append(args["name"])
            return {"name": args["name"], "before": "2", "after": "20"}

    assert apply_set(Worker(), {"session_id": "s", "stop_id": "t"}, "t", "20")["after"] == "20"
    assert sent == ["t @ my file.c:5"]


def test_a_shadowed_row_reached_through_its_plain_name_is_selectable():
    from peeky.cli import _variables_text

    text = _variables_text(
        {
            "frames": [{"source": {"path": "my file.c"}, "line": 6}],
            "scopes": [{"name": "Locals", "variables": [{**SHADOWED, "settable": True}, OUTER]}],
        }
    )
    assert "t @ my file.c:5: int = 2  [settable]\n" in text + "\n"
    assert "t @ my file.c:3: int = 1 [not selectable]" in text


def test_the_worker_takes_lldbs_spelling_of_a_shadowed_root_and_nothing_else(tmp_path):
    from peeky.worker import DebugWorker

    def check(name):
        DebugWorker.validate(
            "set_variable",
            {"session_id": "s", "stop_id": "t", "reference": 7, "name": name, "value": "1"},
        )

    for name in ("t @ my file.c:5", "t @ it's [1].c:5", "größe @ ä b.c:12"):
        check(name)
    for name in (
        "t @ a:b.c:5",
        "t @ my file.c:x",
        "t\u200d @ my file.c:5",
        "t @ :5",
        LONGER + " @ a.c:5",
    ):
        with pytest.raises(ValueError, match="set_variable's name must be a local's name"):
            check(name)


# One parser reads LLDB's `name @ file:line`, the base up to the
# first ` @ `, never a greedy prefix: a file named `one @ two.c` reads the same way when
# a change is checked, when the worker names the row's plain name, and when Peeky judges
# its declaration.
@pytest.mark.parametrize(
    "name, parsed, changeable",
    [
        ("t @ main.c:5", ("t", "main.c", 5), True),
        ("t @ my file.c:5", ("t", "my file.c", 5), True),
        ("t @ one @ two.c:5", ("t", "one @ two.c", 5), True),
        ("größe @ a @ b.rs:12", ("größe", "a @ b.rs", 12), True),
        ("a$b @ x.c:3", ("a$b", "x.c", 3), False),  # parsed, though no selector writes it
        # Every spelling the judgments read parses, past a selector's bounds
        # (`#line 1000000000`, a long `#line` file name), which only selecting and
        # changing apply.
        ("t @ virtual.c:1000000000", ("t", "virtual.c", 1000000000), False),
        ("t @ " + "f" * 256 + ":5", ("t", "f" * 256, 5), False),
        ("t @ virtual.c:" + "9" * 30, ("t", "virtual.c", int("9" * 30)), False),
        ("t @ a:b.c:5", None, False),  # looks located, does not parse: withheld
        ("t @ main.c:x", None, False),
        ("t @ :5", None, False),
        ("@ main.c:5", None, False),
        (" @ main.c:5", None, False),
        ("t", None, True),
    ],
)
def test_lldbs_located_names_parse_one_way(name, parsed, changeable):
    from peeky.identifiers import changeable as changes
    from peeky.identifiers import split_located

    assert split_located(name) == parsed
    assert changes(name) is changeable


def test_a_file_named_with_an_at_sign_gives_the_real_local_its_plain_name():
    from peeky.identifiers import changeable, reachable
    from peeky.native import NativeSession, shown_declaration

    inner, outer = "t @ one @ two.c:5", "t @ one @ two.c:3"
    table = [
        {
            "ranges": [(0, 100)],
            "locals": {"t": [("one @ two.c", 3, True), ("one @ two.c", 5, True)]},
            "visible": {
                ("t", "one @ two.c", 3, True): [(0, 100)],
                ("t", "one @ two.c", 5, True): [(40, 60)],
            },
        }
    ]
    session = object.__new__(NativeSession)
    session.declarations = table
    session._function_at = lambda frame: table[0]
    session._block_pc = lambda frame: 50
    session._named_where = lambda name, pc: ("src/one @ two.c", 5) if name == "t" else None
    rows = [{"name": inner}, {"name": outer}]
    assert session.selected_as(rows[0], {}, rows) == "t"
    assert session.selected_as(rows[1], {}, rows) is None
    assert changeable(inner) and reachable({"name": inner, "selected_as": "t"})
    assert not reachable({"name": outer})
    # Each declaration the debugger shows under its name is judged, never "none of it".
    assert shown_declaration(table, inner, 50) == (table[0], ("one @ two.c", 5, True))
    assert shown_declaration(table, outer, 50) == (table[0], ("one @ two.c", 3, True))


# A selector's bounds never decide what the judgments read. A
# local declared under `#line 1000000000` (or a `#line` file name past 255 characters)
# is judged as any other, and a row that looks located but does not parse is withheld.
@pytest.mark.parametrize("file, line", [("virtual.c", 1000000004), ("v" * 300 + ".c", 4)])
def test_a_declaration_past_a_selectors_bounds_is_still_judged(file, line):
    from peeky.identifiers import looks_located, reachable
    from peeky.native import declared_below, shown_declaration

    name = f"t @ {file}:{line}"
    table = [
        {
            "ranges": [(0, 100)],
            "rows": {(file, line - 1): (10, 20), (file, line): (20, 30)},
            "jumps": False,
            "locals": {"t": [(file, line - 3, True), (file, line, True)]},
            "visible": {
                ("t", file, line - 3, True): [(0, 100)],
                ("t", file, line, True): [(15, 60)],
            },
        }
    ]
    assert shown_declaration(table, name, 15) == (table[0], (file, line, True))
    # Declared on the line below the stop: withheld.
    assert declared_below(table, name, file, line - 1, address=15, pc=15) == (line, None)
    assert looks_located(name) and not reachable({"name": name, "selected_as": "t"})


def test_a_row_that_looks_located_and_does_not_parse_is_withheld():
    from peeky.native import NativeSession

    session = object.__new__(NativeSession)
    frame = {"source": {"path": "/w/a:b.c"}, "line": 6}
    for name in ("t @ a:b.c:7", "t @ x:y:z.c:7"):
        reason = session.not_yet_declared({"name": name, "value": "1"}, frame)
        assert "in a form Peeky cannot read" in reason, name
        assert "may hold leftovers" in reason


def test_a_containers_storage_is_judged_by_the_one_parser():
    # `_declared_storages` took `t @ one` for the base of
    # `t @ one @ two.c:5` and found no declaration, so a binding of a reference was
    # not known to be one.
    from peeky.native import NativeSession

    types = {
        "t": frozenset({(0x60, "through", ("one @ two.c", 5)), (0x70, "automatic", ("main.c", 9))})
    }
    assert NativeSession._declared_storages(types, "t @ one @ two.c:5") == {"through"}
    assert NativeSession._declared_storages(types, "t @ main.c:9") == {"automatic"}
