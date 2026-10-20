"""Where a C or C++ declaration statement ends, from its source (#104 review, round 3)."""

from peeky.c_source import Statements, blank

TRIO = """long run(long seed) {
    struct trio t = { seed,
        { twice(3),
          twice(4) } };
    return t.x;
}
"""


def end(source, line, name):
    return Statements(source).end(line, name)


def test_nested_braces_end_at_the_outer_semicolon():
    assert end(TRIO, 2, "t") == 4
    assert end(TRIO, 5, "t") == 5


def test_a_semicolon_in_a_string_or_a_character_ends_nothing():
    source = "char *s = f(\"a;b\\\";\",\n    ';', '\\'',\n    2);\n"
    assert end(source, 1, "s") == 3


def test_a_semicolon_in_a_comment_ends_nothing():
    source = "long x = f(1, /* ; */\n    2, // ;\n    3); // ;\nlong y = 4;\n"
    assert end(source, 1, "x") == 3
    continued = "long x = f(1, // ; \\\n    ; still a comment\n    2);\n"
    assert end(continued, 1, "x") == 3


def test_a_raw_string_or_a_digit_separator_is_not_a_literal_to_skip_past():
    source = "auto r = R\"d(a;)\" b)d\", n = h(\n    1'000, ';');\n"
    assert end(source, 1, "n") == 2
    assert blank('fooR"(;)"').startswith("fooR ")


def test_a_for_header_runs_to_the_end_of_the_loop():
    braces = "void f(void) {\n    for (int i = 0; i < 3; i++) {\n        g(i);\n    }\n"
    assert end(braces + "    long after = 1;\n}\n", 2, "i") == 5
    # The loop is the block's last statement: nothing ends it.
    assert end(braces + "}\n", 2, "i") is None
    assert end("for (int i = 0; i < 3; i++)\n    g(i);\n", 1, "i") == 2


def test_several_declarators_end_together():
    source = "int a = f(), b = g(\n    2);\nint c = 3;\n"
    assert end(source, 1, "a") == 2 and end(source, 1, "b") == 2
    # A statement before it on the same line.
    assert end("x = 1; long y = f(\n    2);\n", 1, "y") == 2


def test_an_end_that_cannot_be_found_is_none():
    assert end("long y = f(1,\n    2;\n", 1, "y") is None
    assert end("long y = f(1));\n", 1, "y") is None
    assert end("long y = 1;\n", 2, "y") is None and end("long y = 1;\n", 9, "y") is None
    assert end("long y = 1;\n", 1, "z") is None
    assert end("/* long y = 1;\n", 1, "y") is None
    conditional = "long y = f(1,\n#ifdef A\n    2);\n#else\n    3);\n#endif\n"
    assert end(conditional, 1, "y") is None


def test_blanking_keeps_lines_and_columns():
    source = "a = \"x;\\\ny\"; /* ;\n; */ b = ';'; // ;\nc;\n"
    blanked = blank(source)
    assert len(blanked) == len(source)
    assert blanked == "a =     \n  ;     \n     b =    ;     \nc;\n"
