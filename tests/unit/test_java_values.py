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


def test_a_jdk_classs_field_is_never_settable_and_says_whose_it_is():
    # #204: a String's cached hash; the launcher names each field's declaring class.
    session = owners()
    hash_ = {"name": "hash", "type": "int", "value": "0", "peekyOwner": "java.lang.String"}
    reason = session.internal_member(hash_, parent="String")
    assert "declared by java.lang.String, a JDK class" in reason
    assert reason.row == "declared by java.lang.String (the JDK's own state)"
    # Inherited from a JDK class by one of the program's, and of another JDK package.
    for owner in ("java.util.AbstractList", "javax.swing.JList", "jdk.internal.Foo", "sun.x.Y"):
        assert session.internal_member({"name": "f", "type": "int", "peekyOwner": owner})
    assert session.internal_member({"name": "f", "type": "int", "peekyOwner": "com.sun.x.Y"})
    # The program's own fields keep their rules, whatever their class is named.
    for owner in ("app.Rechnung$Posten", "javalike.Box", "com.sunny.Box"):
        assert session.internal_member({"name": "f", "type": "int", "peekyOwner": owner}) is None
    # An object-typed field is refused too, with no row note (no change is offered).
    value = {"name": "value", "type": "byte[]", "peekyOwner": "java.lang.String"}
    assert session.internal_member(value).row is None


def test_an_element_is_judged_by_the_field_that_holds_its_array():
    session = owners()
    root = {"name": "a", "type": "Rechnung$Posten"}
    text = {"name": "bezeichnung", "type": "String", "peekyOwner": "app.Rechnung$Posten"}
    value = {"name": "value", "type": "byte[]", "peekyOwner": "java.lang.String"}
    byte = {"name": "0", "type": "byte", "value": "83"}
    reason = session.internal_member(byte, parent="byte[]", trail=[root, text, value])
    assert "in an array that `value` holds, a field the JDK class java.lang.String" in reason
    # The root local's own array, and an array inside it, are the program's.
    grid = {"name": "grid", "type": "int[][]"}
    row = {"name": "0", "type": "int[]"}
    assert session.internal_member(byte, parent="int[]", trail=[grid, row]) is None
    assert session.internal_member(row, parent="int[][]", trail=[grid]) is None
    # An array inside one a JDK field holds is the JDK's too.
    data = {"name": "elementData", "type": "Object[]", "peekyOwner": "java.util.ArrayList"}
    list_ = {"name": "xs", "type": "ArrayList"}
    assert session.internal_member(byte, parent="int[]", trail=[list_, data, row])
    # Without the way to it, nothing is offered.
    assert "cannot tell" in session.internal_member(byte, parent="byte[]", trail=[])


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
