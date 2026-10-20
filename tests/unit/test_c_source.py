"""Where a C or C++ declaration statement ends, from its source (#104 review, round 3)."""

import pytest

from peeky.c_source import Statements, blank, macro_hazards, unsafe_macros

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


def test_trigraphs_are_read_as_c_reads_them_and_not_in_cpp():
    # In C (`-std=c11`), `??/` is a backslash: the comment goes on over `};`, which the
    # compiler skips (a splice in the declaration: no end). C++17 has no trigraphs.
    source = "struct trio t = { seed, // first ??/\n    };\n    { twice(3),\n      twice(4) } };\n"
    assert Statements(source, raw_strings=False).end(1, "t") is None
    assert Statements(source, raw_strings=False).blanked.split("\n")[1].strip() == ""
    assert Statements(source).end(1, "t") == 2
    # #126 review: one `"huh??!"` no longer hides the whole file.
    elsewhere = 'long a = f(\n    1);\nconst char *q = "what??!";\n'
    assert Statements(elsewhere, raw_strings=False).end(1, "a") == 2
    assert Statements("int x = a ??( 1 ??) ;\n", raw_strings=False).end(1, "x") == 1
    assert Statements("long y = a ??< 2 ;\n", raw_strings=False).end(1, "y") is None
    assert Statements("long y = a ??< 2 ;\n").end(1, "y") == 1


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
    # Trigraphs as C reads them; C++17 has none.
    assert unsafe_macros(["#define Q(x) ??<x\n#define K 1\n"], raw_strings=False) == {"Q"}
    assert unsafe_macros(["#define Q(x) ??<x\n#define K 1\n"]) == set()


def test_c_has_no_raw_strings():
    source = 'char *s = R"(a";"b)" + f(\n    1);\n'
    assert end(source, 1, "s") == 2
    # As `-std=c11` reads it: `R`, `"(a"`, `;`, `"b)"`.
    assert Statements(source, raw_strings=False).end(1, "s") == 1


# Review of #104 (round 5): lines are counted as Clang's line table (so DWARF) and GCC count
# them: `\r\n`, a lone `\r`, or `\n`.


def test_a_lone_carriage_return_breaks_a_line():
    assert end("struct trio t = { a,\n    { g(1),\r      g(2) } };\n", 1, "t") == 3
    hidden = "  struct trio t = { a,\r#if 0\n };\n#endif\n { g(1),\n g(2) } };\n"
    assert end(hidden, 1, "t") is None
    # It ends a `//` comment too.
    assert end("long t = f(1, // ;\r    2);\n", 1, "t") == 2


@pytest.mark.parametrize(
    "breaks",
    [
        # `\r\n` counts once, a lone `\r` beside it still counts.
        ("\r\n", "\r", "\r\n"),
        # `\r\r\n` is a lone `\r` and a pair.
        ("\r\r\n", "\n", "\n"),
        # `\n\r` is two breaks (Clang 9 and later, GCC), not a pair.
        ("\n\r", "\n", "\n"),
    ],
)
def test_line_break_pairs_count_as_the_compilers_count_them(breaks):
    first, second, last = breaks
    source = f"struct trio t = {{ a,{first}    {{ g(1),{second}      g(2) }} }};{last}"
    assert end(source, 1, "t") == (3 if first == "\r\n" else 4)


def test_a_splice_before_newline_and_carriage_return_is_unreadable():
    assert blank("a;\r\nb;\rc;\n\rd;\r\r\ne;") == "a;\nb;\nc;\n\nd;\n\ne;"
    # Clang's lexer takes `\n\r` after a backslash as one newline, joining the line after
    # the `\r`; GCC does not.
    assert end("long t = f(\n    1);\nlong u = x\\\n\ry;\n", 1, "t") is None
    assert unsafe_macros(["#define M(x) x\\\n\r\n#define N 1\n"]) == {"M", "N"}
    # Each `#define` ends at its line's break.
    assert unsafe_macros(["#define N 1\r#define O {\r\n#define P 2\n\r"]) == {"O"}


# #121: the frame stopped in. A declaration has run once its own part has, so a body's
# lines are past a header's variable; a jump past it leaves it unset.

HEADERS = """int main(void) {
  struct outer o = {
      { g(1),
        g(2) } };
  for (int i = 0; i < 3; i++) {
    total += i;
  }
  for (int j = 0;
       j < 3;
       j++) { total += j;
  }
  if (int n = f(
      1)) {
  } else if (auto p = f(
      2)) {
  }
  while (int k = next()) { int k = 2;
    k++;
  }
  try { x(); } catch (const E& e) {
  }
  case 1: int c = h(
      3);
  lbl: int a = 1, b = g(
      4);
  if (auto it = m.find(k); it != m.end()) {
  }
  for (auto v = get(); auto& x : v) {
  }
"""


@pytest.mark.parametrize(
    "line, name, through",
    [
        (2, "o", 4),
        # A header's variable has run with its part of the header.
        (5, "i", 5),
        (8, "j", 8),
        (12, "n", 13),
        (14, "p", 15),
        (20, "e", 20),
        (26, "it", 26),
        (28, "v", 28),
        (28, "x", 28),
        # Declared again after the header on its line: read whole, to the next `;`.
        (17, "k", 23),
        # After a label; several declarators.
        (22, "c", 23),
        (24, "a", 25),
        (24, "b", 25),
        # Not a statement start: a bracket closes first.
        (10, "j", None),
    ],
)
def test_a_declaration_has_run_once_its_own_part_has(line, name, through):
    assert Statements(HEADERS).declared_through(line, name) == through


SWITCH = """void f(int k) {
  int pre = 1;
  switch (k) {
  case 0:
    int p = 1;
  case 1: {
    int q = 2;
    switch (q) { case 5: break; }
    use(p, q);
  }
  default:
    break;
  }
  char *buf = get();
  if (!buf) goto fail;
  int n = 3;
  if (n) goto fail;
fail:
  release(buf);
  int after = 1;
again:
  after++;
  if (after < 3) goto again;
}
"""


def test_a_label_a_jump_reaches_past_a_declaration_is_found():
    statements = Statements(SWITCH)
    # A `case` of the switch whose body holds the declaration.
    assert statements.jumped_past(5, "p", 9) == 6
    # The switch opens after the declaration, or a nested switch does.
    assert statements.jumped_past(2, "pre", 9) is False
    assert statements.jumped_past(7, "q", 9) is False
    # A `goto` from above the declaration; one only from below it.
    assert statements.jumped_past(16, "n", 19) == 18
    assert statements.jumped_past(14, "buf", 19) is False
    assert statements.jumped_past(20, "after", 22) is False
    # A computed goto reaches any label.
    computed = SWITCH.replace("  if (n) goto fail;", "  goto *where;")
    assert Statements(computed).jumped_past(14, "buf", 19) == 18


def test_a_jump_the_text_cannot_show_is_unknown():
    # A macro that may be a label, or `goto` named before the declaration.
    hazards = macro_hazards(["#define LABEL(x) x:\n#define FAIL goto fail\n"])
    assert hazards["labels"] == {"LABEL"} and hazards["gotos"] == {"FAIL"}
    labelled = SWITCH.replace("  int n = 3;", "  int n = 3;\n  LABEL(mid);")
    assert Statements(labelled, hazards=hazards).jumped_past(16, "n", 20) is None
    via_macro = SWITCH.replace("  if (!buf) goto fail;", "  if (!buf) FAIL;")
    assert Statements(via_macro, hazards=hazards).jumped_past(16, "n", 19) == 18
    assert Statements(via_macro).jumped_past(16, "n", 19) is False
    # An `#include` between them, or the declaration's block closing first.
    included = SWITCH.replace("  int n = 3;", '  int n = 3;\n#include "more.h"')
    assert Statements(included).jumped_past(16, "n", 20) is None
    assert Statements(SWITCH).jumped_past(7, "q", 12) is None
    # A switch Peeky cannot find around the label.
    assert (
        Statements("int f(void) {\n  int p = 1;\n  case 2: p++;\n}\n").jumped_past(2, "p", 3) == 3
    )


def test_macro_hazards_name_what_each_expansion_hides():
    header = (
        "#define OPEN {\n#define SEMI(x) x;\n#define LBL(x) x:\n#define CASE(n) case n:\n"
        "#define JUMP goto out\n#define MAX(a, b) ((a) > (b) ? (a) : (b))\n#define WRAP LBL(w)\n"
    )
    hazards = macro_hazards([header])
    assert hazards["brackets"] == {"OPEN", "SEMI"} and hazards["blocks"] == {"OPEN"}
    assert hazards["labels"] == {"LBL", "CASE", "WRAP"} and hazards["gotos"] == {"JUMP"}


# Review of #126 (round 1).


def test_a_goto_back_into_a_block_from_after_it_reaches_its_label():
    source = """long back(int pass) {
  long out = 0;
  {
    struct pair p = { pass, 8 };
  again:
    out += p.y;
    if (pass > 1) return out;
  }
  if (++pass < 3) goto again;
  for (int i = 0; i < 2; i++) {
  inner:
    out += i;
  }
  if (out < 0) goto inner;
  return out;
}
"""
    statements = Statements(source)
    # C11 6.2.4: re-entering the block starts `p`'s lifetime anew, unset.
    assert statements.jumped_past(4, "p", 6) == 5
    # A `goto` inside the same block, after the declaration, keeps it.
    kept = source.replace("  if (++pass < 3) goto again;\n", "").replace(
        "    if (pass > 1) return out;", "    if (pass++ < 2) goto again;"
    )
    assert Statements(kept).jumped_past(4, "p", 6) is False
    # A header's variable: any `goto` to a label in its loop.
    assert statements.jumped_past(10, "i", 12) == 11


def test_an_include_after_a_declaration_in_its_block_is_found():
    source = 'long run(int k) {\n  long out = 0;\n  switch (k) {\n  case 0:;\n    struct pair p = { 7, 8 };\n#include "cases.h"\n  }\n  return out;\n}\nlong other(void) {\n  long z = 1;\n  return z;\n}\n'
    statements = Statements(source)
    assert statements.included_after(5, "p") is True
    assert statements.included_after(2, "out") is True
    assert statements.included_after(11, "z") is False
    assert statements.included_after(11, "nope") is None


def test_a_header_declaration_stays_one_when_its_body_on_the_line_uses_it():
    source = """int main(void) {
  for (int i = 0; i < 3; i++) total += spread(i + 1);
  for (int j = 0; j < 4; j++) { int sq = j * j; f(a, j); x = y * j; }
  for (int k = 0; k < 3; k++) { int a = 1, k = 2; }
  for (int m = 0; m < 3; m++) { T *m = p; }
  long r = twice(3) + twice(4);
}
"""
    statements = Statements(source)
    assert statements.declaration(2, "i")[2] is True
    assert statements.declaration(3, "j")[2] is True
    # Maybe declared again in the body on the line: read whole, to the next `;`.
    assert statements.declaration(4, "k")[2] is False and statements.declared_through(4, "k") == 6
    assert statements.declaration(5, "m")[2] is False
    assert statements.declaration(6, "r")[2] is False


def test_a_statement_expression_macro_keeps_its_semicolons_inside():
    header = (
        "#define max(a, b) ({ __typeof__(a) _a = (a); __typeof__(b) _b = (b); _a > _b ? _a : _b; })\n"
        "#define END ;\n#define PAIR {1, 2}\n"
    )
    assert unsafe_macros([header]) == {"END"}
    hazards = macro_hazards([header])
    source = "int m = max(n,\n    3);\nlong s = m;\n"
    assert Statements(source, hazards=hazards).declared_through(1, "m") == 2
