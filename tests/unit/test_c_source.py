"""Where a C or C++ declaration statement ends, from its source (#104 review, round 3)."""

from peeky.c_source import Statements, blank, unsafe_macros

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


# Review of #104 (round 4): text the compiler reads differently from the scan gives no end.


def test_a_line_splice_gives_no_end():
    # GCC and Clang also splice a backslash followed by spaces: line 2 is the comment.
    continued = "long x = f(1, // ; \\ \n    ; still a comment\n    2);\n"
    assert blank(continued).split("\n")[1].strip() == ""
    assert end(continued, 1, "x") is None
    assert end("long t = f(1, \\\n    2);\n", 1, "t") is None
    # A splice inside a token (here a string's) anywhere in the file.
    assert end('char *s = "abc\\\ndef";\nlong t = f(\n    1);\n', 3, "t") is None
    # A macro's own splices elsewhere change nothing.
    macro = "#define TWICE(x) \\\n    ((x) * 2)\nlong t = TWICE(\n    1);\n"
    assert end(macro, 3, "t") == 4


def test_a_trigraph_anywhere_in_the_file_gives_no_end():
    # `??/` is a backslash: the comment goes on over `};`, which the compiler skips.
    source = "struct trio t = { seed, // first ??/\n    };\n    { twice(3),\n      twice(4) } };\n"
    assert end(source, 1, "t") is None
    assert end('long a = f(\n    1);\nconst char *q = "what??!";\n', 1, "a") is None


def test_a_directive_from_the_declaration_to_its_end_gives_no_end():
    alternative = (
        "struct trio t = { seed,\n%:if 0\n    };\n%:endif\n    { twice(3),\n      twice(4) } };\n"
    )
    spliced = (
        "struct trio u = { seed,\n#\\\nif 0\n    };\n#endif\n    { twice(5),\n      twice(6) } };\n"
    )
    included = 'struct pair ps[2] = {\n#include "table.h"\n};\n'
    pragma = "long v = f(1,\n#pragma pack()\n    2);\n"
    for source, name in ((alternative, "t"), (spliced, "u"), (included, "ps"), (pragma, "v")):
        assert end(source, 1, name) is None, source
    # `#line` and a line marker renumber what follows, anywhere in the file.
    for directive in ("#line 40", '# 7 "x.c"', "%:line 40"):
        assert end(f"{directive}\nlong w = f(1,\n    2);\n", 2, "w") is None


def test_digraphs_are_brackets():
    source = (
        "Trio t <% [&]() <% long a = twice(10); return a + seed; %>(),\n"
        "    <% twice(11),\n"
        "       twice(12) %> %>;\n"
    )
    assert end(source, 1, "t") == 3
    assert end("long v = arr<: <:&:>() <% return 0; %>() :> + g(\n    1);\n", 1, "v") == 2
    # C++11 reads `<::` as `<` and `::`, C as `<:` and `:`.
    assert end("std::vector<::Item> items = make(\n    1);\n", 1, "items") is None


def test_a_macro_hiding_a_bracket_or_a_semicolon_gives_no_end():
    header = (
        "#define INIT_BEGIN {\n#define INIT_END }\n#define LAMBDA_BEGIN [&]() {\n"
        "#define LAMBDA_END }()\n#define FINE(x) ((x) + 1)\n#define WRAP INIT_BEGIN\n"
        "#define SEMI(x) x;\n#define CAT(a, b) a##b\n#define OPEN \\\n    (\n"
        "/* #define COMMENTED { */\n%:define SPACED (x) {\n"
    )
    unsafe = unsafe_macros([header])
    assert unsafe == {
        *("INIT_BEGIN", "INIT_END", "LAMBDA_BEGIN", "LAMBDA_END"),
        *("WRAP", "SEMI", "CAT", "OPEN", "SPACED"),
    }
    source = (
        "Trio u = INIT_BEGIN LAMBDA_BEGIN long a = twice(20); return a + seed; LAMBDA_END,\n"
        "    { twice(21),\n"
        "      twice(22) } INIT_END;\n"
    )
    # The text alone ends it at `twice(20);`.
    assert Statements(source).end(1, "u") == 1
    assert Statements(source, macros=unsafe).end(1, "u") is None
    assert Statements("long k = FINE(\n    2);\n", macros=unsafe).end(1, "k") == 2
    # A source with a trigraph: all of its macros.
    assert unsafe_macros(["#define Q(x) ??<x??>\n#define K 1\n"]) == {"Q", "K"}


def test_c_has_no_raw_strings():
    source = 'char *s = R"(a";"b)" + f(\n    1);\n'
    assert end(source, 1, "s") == 2
    # As `-std=c11` reads it: `R`, `"(a"`, `;`, `"b)"`.
    assert Statements(source, raw_strings=False).end(1, "s") == 1
