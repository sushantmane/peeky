"""Java mutations must fit the declared type exactly (java-debug narrows silently)."""

import pytest

from peeky.errors import classify
from peeky.java import JavaSession


def check(kind, value):
    session = JavaSession.__new__(JavaSession)
    return session.validate_variable({"name": "x", "type": kind}, value)


@pytest.mark.parametrize(
    "kind, value",
    [
        ("int", "0"),
        ("int", "-0"),
        ("int", "+7"),
        ("int", "-2147483648"),
        ("int", "2147483647"),
        ("byte", "-128"),
        ("short", "32767"),
        ("long", "9223372036854775807"),
        ("boolean", "true"),
        ("boolean", "false"),
        ("float", "3.4028235e38"),
        ("float", "-1.5"),
        ("float", "0.0"),
        ("double", "1e308"),
        ("double", "-0"),
    ],
)
def test_values_that_fit_are_accepted(kind, value):
    check(kind, value)


@pytest.mark.parametrize(
    "kind, value",
    [
        ("int", "2147483648"),
        ("int", "010"),  # Long.decode reads a leading 0 as octal
        ("int", "09"),
        ("int", "1.5"),
        ("int", "true"),
        ("byte", "128"),
        ("short", "-32769"),
        ("long", "9223372036854775808"),
        ("boolean", "1"),
        ("boolean", "TRUE"),
        ("float", "3.5e38"),
        ("float", "1e-50"),  # would round to zero
        ("double", "1e400"),
        ("double", "1e-400"),
        ("double", "true"),
        ("char", "65"),
        ("String", "123"),
        ("int[]", "1"),
        ("Integer", "1"),
    ],
)
def test_values_that_would_change_are_refused(kind, value):
    with pytest.raises(ValueError) as caught:
        check(kind, value)
    assert classify(caught.value)["code"] == "MUTATION_REJECTED"


def charred(value, char_set=True):
    session = JavaSession.__new__(JavaSession)
    session.char_set, session.text_set = char_set, True
    return session


@pytest.mark.parametrize(
    "value, char",
    [
        ("'+'", "+"),
        ("'\\n'", "\n"),
        ("'\\''", "'"),
        ("'\\\\'", "\\"),
        ("'\\u0041'", "A"),
        ("'\\101'", "A"),
        ("43", "+"),
        ("0", "\x00"),
        ("65535", "\uffff"),
    ],
)
def test_a_char_takes_a_char_literal_or_a_code_point(value, char):
    # #183: java-debug sets a char; Peeky sends it the char itself between single quotes.
    session = charred(value)
    variable = {"name": "op", "type": "char"}
    session.validate_variable(variable, value)
    assert session.mutation_literal(value, variable=variable) == f"'{char}'"
    session.confirm_mutation("op", value, "-", char, variable=variable)
    with pytest.raises(RuntimeError):
        session.confirm_mutation("op", value, "-", "?" if char != "?" else "!", variable=variable)


@pytest.mark.parametrize("value", ["65536", "-1", "'ab'", "'\U0001f600'", "1.5", "true", '"+"'])
def test_a_char_refuses_what_is_no_single_char(value):
    with pytest.raises(ValueError) as caught:
        charred(value).validate_variable({"name": "op", "type": "char"}, value)
    assert classify(caught.value)["code"] == "MUTATION_REJECTED"


def test_the_type_is_judged_before_the_value_and_names_the_rule():
    # #183: `op='+'` on a char was refused as "requires a numeric or boolean scalar
    # literal" (with a hint about C, C++ and Rust); the real rule came only on a retry.
    session = charred("'+'", char_set=False)
    with pytest.raises(ValueError) as caught:
        session.validate_variable({"name": "op", "type": "char"}, "'+'")
    message = str(caught.value)
    assert message.startswith("op is char; only byte, short, int, long, boolean"), message
    assert "a char too with an adapter bundle built by this version" in message
    assert "Rebuild the Java adapter bundle" in classify(caught.value)["hint"]
    # A type no bundle changes says only what can change.
    with pytest.raises(ValueError) as caught:
        charred("1").validate_variable({"name": "b", "type": "java.lang.StringBuilder"}, "1")
    assert str(caught.value) == (
        "b is java.lang.StringBuilder; only byte, short, int, long, char, boolean, float, "
        "double and String (to a text) values can be changed"
    )
    # A literal of another type names the variable's own.
    with pytest.raises(ValueError, match=r"requires a literal of left's type \(long\)"):
        charred("'+'").validate_variable({"name": "left", "type": "long"}, "'+'")


def owners(field_owners=True):
    session = JavaSession.__new__(JavaSession)
    session.field_owners = field_owners
    return session


def field(name, kind, owner, jdk):
    return {"name": name, "type": kind, "peekyOwner": owner, "peekyJdkOwner": jdk}


def test_a_jdk_classs_field_is_never_settable_and_says_whose_it_is():
    # #204: a String's cached hash; the launcher names each field's declaring class, and
    # whether the JDK's own class loaders loaded it.
    session = owners()
    reason = session.internal_member(field("hash", "int", "java.lang.String", True), "String")
    assert "declared by java.lang.String, a JDK class" in reason
    assert reason.row == "declared by java.lang.String (the JDK's own state)"
    assert session.internal_member(field("size", "int", "java.util.AbstractList", True))
    # The program's own fields keep their rules, whatever their package looks like.
    for owner in ("app.Rechnung$Posten", "javax.foo.Box", "jdk.app.Box", "com.sun.app.Box"):
        assert session.internal_member(field("f", "int", owner, False)) is None
    # An object-typed field is refused too, with no row note (no change is offered).
    assert session.internal_member(field("value", "byte[]", "java.lang.String", True)).row is None
    # A launcher that names the class but not who loaded it tells nothing.
    named = {"name": "f", "type": "int", "peekyOwner": "javax.foo.Box"}
    assert "cannot tell which class declares it" in session.internal_member(named)


def test_an_element_is_judged_by_the_field_that_holds_its_array():
    session = owners()
    root = {"name": "a", "type": "Rechnung$Posten"}
    text = field("bezeichnung", "String", "app.Rechnung$Posten", False)
    value = field("value", "byte[]", "java.lang.String", True)
    byte = {"name": "0", "type": "byte", "value": "83"}
    reason = session.internal_member(byte, parent="byte[]", trail=[root, text, value])
    assert "in an array that `value` holds, a field the JDK class java.lang.String" in reason
    # The root local's own array, and an array inside it, are the program's.
    grid = {"name": "grid", "type": "int[][]"}
    row = {"name": "0", "type": "int[]"}
    assert session.internal_member(byte, parent="int[]", trail=[grid, row]) is None
    assert session.internal_member(row, parent="int[][]", trail=[grid]) is None
    # A row of an array of arrays a JDK field holds is the JDK's too ...
    table = field("table", "int[][]", "java.util.Fake", True)
    holder = {"name": "x", "type": "Fake"}
    assert session.internal_member(byte, parent="int[]", trail=[holder, table, row])
    # ... and so is one inside an Object[] a JDK field holds (a cache's): an Object[] is
    # no proof of whose its elements are (PR #210 review round 2) ...
    cache = field("cache", "Object[]", "sun.util.resources.Fake", True)
    assert session.internal_member(byte, parent="int[]", trail=[holder, cache, row])
    # ... but for a JDK list's backing array, whose elements are the program's values.
    list_ = {"name": "xs", "type": "ArrayList"}
    for owner, name in (
        ("java.util.ArrayList", "elementData"),
        ("java.util.Vector", "elementData"),
        ("java.util.ArrayDeque", "elements"),
        ("java.util.Arrays$ArrayList", "a"),
    ):
        data = field(name, "Object[]", owner, True)
        assert session.internal_member(byte, parent="int[]", trail=[list_, data, row]) is None
        # The backing array's own slots are the list's.
        assert session.internal_member(row, parent="Object[]", trail=[list_, data])
    # A list the JDK itself holds is no proof: its elements are its holder's state.
    box = {"name": "box", "type": "Box"}
    mine = field("items", "ArrayList", "app.Box", False)
    theirs = field("items", "ArrayList", "java.util.Fake", True)
    assert session.internal_member(byte, "int[]", trail=[box, mine, data, row]) is None
    reason = session.internal_member(byte, "int[]", trail=[box, theirs, data, row])
    assert "inside what `items` holds, a field the JDK class java.util.Fake" in reason
    # Rows of rows of a list's element are the program's too.
    deeper = {"name": "0", "type": "int[][]"}
    data = field("elementData", "Object[]", "java.util.ArrayList", True)
    assert session.internal_member(byte, "int[]", trail=[list_, data, deeper, row]) is None
    # Without the way to it, nothing is offered.
    assert "cannot tell" in session.internal_member(byte, parent="byte[]", trail=[])


def test_before_the_run_only_classes_that_may_be_the_value_count():
    # #204 review: an unrelated class's `size` does not make an ArrayList's the program's.
    from peeky.java import _may_be, _may_declare

    declared = {
        "app/Other": {"super": "java/lang/Object", "interfaces": [], "fields": {"size": "I"}},
        "app/Shape": {"super": "java/lang/Object", "interfaces": [], "fields": {}},
        "app/Square": {"super": "app/Base", "interfaces": ["app/Shape"], "fields": {}},
        "app/Base": {"super": "java/lang/Object", "interfaces": [], "fields": {"size": "I"}},
        "app/Mine": {"super": "java/util/AbstractList", "interfaces": [], "fields": {"n": "I"}},
        "app/Sized": {"super": "app/Mine", "interfaces": [], "fields": {"size": "I"}},
    }
    assert not _may_be(declared, "app/Other", "java/util/ArrayList")
    assert not _may_declare(declared, "java/util/ArrayList", "hash")
    # A subclass of a JDK class may be any JDK type: its `size` defers to the stop.
    assert _may_be(declared, "app/Sized", "java/util/ArrayList")
    assert _may_declare(declared, "java/util/List", "size")
    # A program type: only its own subtypes, through the program's classes.
    assert _may_be(declared, "app/Square", "app/Shape")
    assert _may_declare(declared, "app/Shape", "size")
    assert not _may_be(declared, "app/Other", "app/Shape")
    assert not _may_be(declared, "app/Sized", "app/Shape")


def test_a_launcher_that_names_no_declaring_class_changes_no_field():
    field = {"name": "end", "type": "int", "value": "5"}
    reason = owners(field_owners=False).internal_member(field, parent="Interval")
    assert "predates" in reason and "scripts/setup_java_adapter.py" in reason
    assert not hasattr(reason, "row")
    assert "cannot tell which class declares it" in owners().internal_member(field)
    # A root local's array needs no declaring class.
    element = {"name": "0", "type": "int"}
    trail = [{"name": "arr", "type": "int[]"}]
    assert owners(field_owners=False).internal_member(element, "int[]", trail) is None


@pytest.mark.parametrize(
    "type_, value, whole",
    [
        ("String", '"Schraube"', True),
        ("java.lang.String", '""', True),
        ("Integer", "Integer@57", True),
        ("java.lang.Long", "Long@12", True),
        ("Character", "Character@3", True),
        ("Boolean", "Boolean@4", True),
        # null, an object of another class, and a user class named like none of these.
        ("String", "null", False),
        ("Integer", "null", False),
        ("Rechnung$Posten", "Rechnung$Posten@54", False),
        ("ArrayList", "ArrayList@3", False),
    ],
)
def test_a_string_or_boxed_number_is_shown_whole(type_, value, whole):
    session = JavaSession.__new__(JavaSession)
    assert session.whole_value({"type": type_, "value": value}) is whole


class Launcher:
    """A launcher's answers to peekyArrayHolder (#261)."""

    def __init__(self, body=None, refusal=None):
        self.body, self.refusal, self.calls = body, refusal, []

    def request(self, command, arguments=None, **_):
        from peeky.dap import RequestFailed

        self.calls.append((command, arguments))
        if self.refusal:
            raise RequestFailed(self.refusal)
        return self.body


def test_an_elements_mark_asks_the_launcher_what_holds_its_array():
    # #261: `ranking[1]` was marked [settable] by its path, then the write was refused.
    session = owners()
    session.array_holders = True
    array = {"name": "ranking", "type": "int[]", "value": "int[3]@59", "variablesReference": 7}
    held = Launcher({"holder": "in a list held by java.util.Collections$UnmodifiableList.list"})
    assert session.array_holder(held, array, 4500.4) == (
        "its array is in a list held by java.util.Collections$UnmodifiableList.list "
        "(the JDK's own state)"
    )
    assert held.calls == [("peekyArrayHolder", {"variablesReference": 7, "peekyPauseLeftMs": 4500})]
    assert not getattr(session.array_holder(held, array), "unproven", False)
    assert session.array_holder(Launcher({}), array) is None
    # Where the launcher cannot tell, a write is refused too, and the row says why.
    many = Launcher({"unknown": "more than 64 objects refer to the array", "timedOut": False})
    assert session.array_holder(many, array) == "more than 64 objects refer to the array"
    assert not getattr(session.array_holder(many, array), "unproven", False)
    # Where a longer search, as a write makes, may yet tell, neither mark is promised.
    late = Launcher({"unknown": "Peeky could not tell within 500 ms", "timedOut": True})
    reason = session.array_holder(late, array)
    assert reason == "Peeky could not tell within 500 ms; `set` searches again, for up to 2 s"
    assert reason.unproven is True
    reason = session.array_holder(Launcher(refusal="unsupported command"), array)
    assert "can't tell whether a JDK class holds" in reason and reason.unproven is True
    # A launcher that cannot search before a change: neither, and it says to rebuild it.
    session.array_holders = False
    reason = session.array_holder(Launcher({}), array)
    assert "rebuild it with scripts/setup_java_adapter.py" in reason and reason.unproven is True


def test_a_row_set_does_not_change_says_why():
    # #261: `counted: int = 9` showed neither [settable] nor why not.
    session = owners()
    session.text_set = session.char_set = session.field_set = True
    locals_ = {
        "base": [("I", False)],
        "label": [("Ljava/lang/Object;", True)],
        "text": [("Ljava/lang/String;", True)],
        "n": [("I", True)],
    }
    session.declared_local = lambda frame, name: locals_.get(name)
    frame = {"id": 1}
    row = session.unsettable_row
    assert row({"name": "base", "type": "int"}, frame) == "a constant javac compiles into its uses"
    assert row({"name": "label", "type": "String"}, frame) == "declared Object, not String"
    assert row({"name": "n", "type": "int"}, frame, unread=True) == "not read again after this stop"
    assert row({"name": "text", "type": "String"}, frame, innermost=False) == "in a caller's frame"
    assert row({"name": "n", "type": "int"}, frame) is None
    # An object or an array: its type says it.
    assert row({"name": "xs", "type": "int[]"}, frame) is None
    attributes = {"presentationHint": {"attributes": ["readOnly"]}}
    final = {**field("limit", "int", "app.Vote", False), **attributes}
    assert row(final, frame, parent="Vote", member=True) == "final"
    shared = {
        **field("count", "int", "app.Vote", False),
        "presentationHint": {"attributes": ["static"]},
    }
    assert row(shared, frame, parent="Vote", member=True) == "static"
    # An Object[]'s element holding a String takes no text.
    element = {"name": "0", "type": "String", "value": '"x"'}
    assert row(element, frame, parent="Object[]", member=True) == "declared Object, not String"
    own = field("total", "int", "app.Vote", False)
    assert row(own, frame, parent="Vote", member=True, innermost=False) == "in a caller's frame"
    session.field_set = False
    assert "predates field and element changes" in row(own, frame, parent="Vote", member=True)
