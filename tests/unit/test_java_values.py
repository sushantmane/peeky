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
