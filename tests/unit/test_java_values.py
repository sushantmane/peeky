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
