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


# Review of #126 (round 2).


def test_a_header_variable_is_settled_only_without_a_call_or_a_while():
    # Review of PR #214: a name in the initializer may be a macro that calls (`INIT`,
    # a library's `errno`), so `k = m` and `n = x` are no longer taken as settled.
    source = """int f(void) {
  for (int i = 0; i < 3; i++) g(i);
  for (long i = twice(1); i < 20; i += 7) g(i);
  if (long r = twice(3)) g(r);
  while (long v = n) g(v);
  switch (long k = m) { default: g(k); }
  for (auto x : items) g(x);
  for (Foo f{1}; f.ok(); f.next()) g();
  if (int n = x) g(n);
  for (int i = INIT; i < 3; i++) g(i);
  for (int i = errno; i < 3; i++) g(i);
  for (unsigned long i = 0x10ul << 2; i > -1; i++) g(i);
  for (bool b = true; b; b = false) g();
  for (int i = 0, j = i; i < 3; i++) g(i);
  for (int i; ; ) sink(&i);
  for (int i = 0, j = 1; i < j; i++) g(i);
  for (int j = INIT, i = 0; i < j; i++) g(i);
  for (X j = 0, *i = nullptr; ; ) g(i);
}
"""
    statements = Statements(source)
    settled = {
        (line, name): statements.declaration(line, name)[3]
        for line, name in (
            (2, "i"),
            (3, "i"),
            (4, "r"),
            (5, "v"),
            (6, "k"),
            (7, "x"),
            (8, "f"),
            (9, "n"),
            (10, "i"),
            (11, "i"),
            (12, "i"),
            (13, "b"),
            (14, "i"),
            (14, "j"),
            (15, "i"),
            (16, "i"),
            (16, "j"),
            (17, "i"),
            (18, "i"),
        )
    }
    # Review round 2 of PR #214: each declarator is judged with those before it
    # (`j = i` names `i`, but `i = 0` is set first; `j = INIT` runs before `i = 0`),
    # and one with no initializer (`int i;`) is never set by the header.
    assert settled == {
        (2, "i"): True,
        (3, "i"): False,
        (4, "r"): False,
        (5, "v"): False,
        (6, "k"): False,
        (7, "x"): False,
        (8, "f"): False,
        (9, "n"): False,
        (10, "i"): False,
        (11, "i"): False,
        (12, "i"): True,
        (13, "b"): True,
        (14, "i"): True,
        (14, "j"): False,
        (15, "i"): False,
        (16, "i"): True,
        (16, "j"): False,
        (17, "i"): False,
        (18, "i"): False,
    }
    # Review round 3: in C++ constructing a declarator before it may call (`X j = 0`
    # runs `X::X(int)` before `i` is set), so only the first counts; C keeps each judged
    # with those before it.
    c = Statements(source, raw_strings=False)
    assert [c.declaration(16, "j")[3], c.declaration(14, "j")[3]] == [True, False]
    # A macro of the program's own named `true` is no literal.
    hazards = macro_hazards(["#define true make_true()\n"])
    assert Statements(source, hazards=hazards).declaration(13, "b")[3] is False
    # The keyword of the header that declares a name (for a caller frame's reason).
    assert [statements.header(line, name) for line, name in ((2, "i"), (5, "v"), (9, "n"))] == [
        "for",
        "while",
        "if",
    ]
    assert Statements("int f(void) {\n  int t = 1;\n}\n").header(2, "t") is None


def test_a_jump_into_a_headers_statement_past_its_declaration_is_found():
    # Review round 2 of PR #214: `goto inside;` to a label in a one-line loop's body
    # skips `int i = 0`, so a header's variable is live mid-line only where no jump can
    # enter its statement past the declaration (as `jumped_past` judges a label).
    source = """#define JUMP goto out
#define MARK out:
int plain(int x) {
  for (int i = 0; i < 3; i++) g(&i);
  switch (x) { case 1: g(&x); }
  return 0;
}
int jumps(void) {
  goto inside;
  for (int i = 0; ; ) { inside: g(&i); }
}
int back(void) {
  for (int i = 0; ; ) { again: g(&i); }
  goto again;
}
int switched(int x) {
  switch (x) { for (int i = 0; ; ) { case 1: g(&i); } }
}
int labelled(void) {
  for (int i = 0; i < 3; i++) g(&i);
  done: return 0;
}
int hidden(void) {
  for (int i = 0; i < 3; i++) { MARK g(&i); }
}
int included(void) {
  for (int i = 0; i < 3; i++) {
#include "body.h"
  }
}
int macrojump(void) {
  for (int i = 0; ; ) { out: g(&i); }
  JUMP;
}
"""
    statements = Statements(source, hazards=macro_hazards([source]))
    found = {line: statements.jumped_into(line, "i") for line in (4, 10, 13, 17, 20, 24, 27, 32)}
    # A switch after the loop, and a label no `goto` names, cannot enter it; a macro
    # that may hold a label, or an `#include`, leaves it unknown.
    assert found == {4: False, 10: 10, 13: 13, 17: 17, 20: False, 24: None, 27: None, 32: 32}
    # Review round 3: a GNU `asm goto` lists its labels as operands, which no `goto
    # label` names: any label after the declaration counts then.
    source = """int asmjump(void) {
  for (int i = 0; ; ) { inside: g(&i); }
  __asm__ volatile goto("" : : : : inside);
}
int plain(void) {
  for (int i = 0; ; ) { inside: g(&i); }
}
"""
    assert Statements(source).jumped_into(2, "i") == 2
    assert Statements(source.replace("goto(", "(")).jumped_into(6, "i") is False


def test_includes_and_the_labels_after_one():
    source = 'long run(long seed) {\n  long first = 1;\n#include "decls.h"\n  return first;\n}\n'
    statements = Statements(source)
    assert statements.includes() == [(3, "decls.h")]
    # Unknown with no reader of the included text, then as that text holds a label.
    assert statements.jumped_past(2, "first", 4) is None
    statements.included = lambda line: Statements("  struct pair later = { 5, 6 };\n")
    # (A span the first call did not ask about, so not its cached answer.)
    assert statements.jumped_past(2, "first", 4, (1, 5)) is False
    assert statements.jumped_past_include(3, 4) is False
    labelled = Statements(source)
    labelled.included = lambda line: Statements("  case 1:\n    out = 2;\n")
    assert labelled.jumped_past(2, "first", 4) is None


def test_a_goto_counts_only_within_the_function_s_own_lines():
    source = """namespace app {
long f(long x) {
  if (x < 0) goto out;
out:
  return x;
}
long g(long y) {
  long doubled = y * 2;
out:
  return doubled;
}
}
"""
    statements = Statements(source)
    assert statements.jumped_past(8, "doubled", 10) == 9
    assert statements.jumped_past(8, "doubled", 10, (7, 11)) is False


def test_a_class_extent_runs_from_its_head_to_its_closing_brace():
    # Review of #126 (round 3): a local class's member initializers are not the
    # function's own lines.
    source = """void f(void) {
  struct D : public B<int>, C {
    int a = g(1);
  } d;
  struct Fwd;
  struct alignas(8) [[nodiscard]] S { long x = g(2); };
  union U
  {
    int i;
  };
  struct P p = {1};
  auto h = [&] { return 1; };
}
"""
    statements = Statements(source)
    assert statements.class_extent(2) == (2, 4) and statements.class_extent(6) == (6, 6)
    assert statements.class_extent(7) == (7, 10)
    # A declaration only, an object of a class, a lambda: none.
    for line in (5, 11, 12, 99):
        assert statements.class_extent(line) is None, line


BEGINS = """#include <stdio.h>
struct S { long a; long b; };
static long f(void) { return 1000; }
int main(void) {
    struct S s = { 1, 2 };
    long t = s.a
        + f();
    if (s.a)
        s.b = 3;
    switch (s.a) {
    case 1:
        s.b = 4;
        break;
    default: {
        s.b = 5;
    }
    }
    for (int i = 0;
         i < 3;
         i++) {
        s.a += i;
    }
    long u = g(s.a,
               f());
    struct S w = {
        s.a,
        f() };
    long v = ({ long q = f();
        q; });
#ifdef X
    s.a = 9;
#endif
    x = c ? s.a :
        f();
out:
    s.a = 1;
    auto lam = [&](int v) -> int {
        return v;
    }
    (3);
    return 0;
}
struct Cfg { long a = f();
    long b = f(); };
int Klass::get() const {
    return 1;
}
"""


def test_a_line_continuing_a_statement_is_told_from_one_starting_it():
    # Review of #105: a stop on `+ f();` under `long t = s.a` is part-way through the
    # statement, which already read `s.a`.
    statements = Statements(BEGINS)
    verdicts = {}
    for number, text in enumerate(BEGINS.splitlines(), 1):
        verdicts.setdefault(statements.begins(number), []).append(text.strip())
    assert verdicts[False] == [
        # Outside any function's body: its first line is judged by its rows.
        "struct S { long a; long b; };",
        "static long f(void) { return 1000; }",
        "int main(void) {",
        "+ f();",
        "i < 3;",
        "i++) {",
        "f());",
        "s.a,",
        "f() };",
        "q; });",
        "f();",
        "(3);",
        "struct Cfg { long a = f();",
        "long b = f(); };",
        "int Klass::get() const {",
    ]
    # A directive between the line and the code before it (`#ifdef X`).
    assert verdicts[None] == [
        "#include <stdio.h>",
        "#ifdef X",
        "s.a = 9;",
        "#endif",
        "x = c ? s.a :",
    ]
    for text in ("long t = s.a", "s.b = 3;", "s.b = 4;", "s.b = 5;", "s.a += i;", "s.a = 1;"):
        assert text in verdicts[True]
    assert "return v;" in verdicts[True] and "return 1;" in verdicts[True]  # a lambda's, a method's
    assert statements.statement_span(6) == (6, 7)  # `long t = s.a` / `+ f();`
    assert statements.statement_span(18) == (18, 20)  # a `for` header, to its `)`
    assert statements.statement_span(25) == (25, 27)
    # `t = p.x` / `#if 0` / `;` / `#endif` / `+ g();`: a directive inside the statement.
    hidden = Statements("int f(void) {\n    t = p.x\n#if 0\n    ;\n#endif\n    + g();\n}\n")
    assert hidden.statement_span(2) is None and hidden.begins(6) is None


STATEMENT_EXPRESSIONS = """int main(void) {
    long t = p.x + ({
        long q = 0;
        if (c) {
            q = g();
        }
        for (int i = 0; i < 1; i++) {
            q += g();
        }
        q;
    });
    t = p.x + std::unique_ptr<long[]>(new long[1]{
        g() })[0];
    t = p.x + decltype(W{}){
        g() }.v;
    t = (long)(struct W){
        g() }.v;
    if (t) [[likely]] {
        t = 1;
    }
    FOR_EACH(x) {
        t = 2;
    }
    struct L {
        long m() {
            return 3;
        }
    };
    auto f = [](long v) {
        return v;
    };
    return 0;
}
"""


def test_every_bracket_up_to_the_function_body_must_be_a_block_of_statements():
    # Review of #105 (round 2): `if (c) {` inside `({`, `new long[1]{`, `decltype(W{}){`
    # and `(long)(struct W){` each passed as a block of statements.
    statements = Statements(STATEMENT_EXPRESSIONS, raw_strings=True)
    lines = STATEMENT_EXPRESSIONS.splitlines()

    def verdict(text):
        return statements.begins(lines.index(text) + 1)

    for text in (
        "            q = g();",
        "            q += g();",
        "        long q = 0;",
        "        g() })[0];",
        "        g() }.v;",
    ):
        assert verdict(text) is False, text
    for text in (
        "        t = 1;",  # after `if (t) [[likely]] {`
        "        t = 2;",  # a macro's block (`FOR_EACH(x) {`) in a function's body
        "            return 3;",  # a local class's method
        "        return v;",  # a lambda's body
        "    return 0;",
    ):
        assert verdict(text) is True, text


ROUND3 = """#define REPEAT(i, n) for (int i = 0; i < (n); i++)
extern "C" {
long one(struct P p) { long t =
    p.x + g();
    return t; }
long two(struct P p) { return p.x + g(); }
long none(void) {}
}
namespace app {
template <typename T, int N = 3> struct X : B<T> {
    long m(long v) const {
        return v;
    }
};
}
long old(a, b)
    long a; long b;
{
    long r = a + b;
    return r;
}
int main(void) {
    long t = p.x + ({
        long q = 0;
        REPEAT(k, 1) {
            q += g();
        }
        q;
    });
    auto f = [&](long v) { return v + 1; };
    return 0;
}
"""


def test_a_function_like_brace_is_a_body_only_at_file_scope_or_in_a_class():
    # Review of #105 (round 3): `REPEAT(k, 1) {` inside `({` read as a function's body.
    statements = Statements(ROUND3, raw_strings=True)
    lines = ROUND3.splitlines()

    def line(text):
        return lines.index(text) + 1

    assert statements.begins(line("            q += g();")) is False
    assert statements.begins(line("        long q = 0;")) is False
    # Functions in `extern "C" {`, a namespace and a template class with a default.
    assert statements.begins(line("        return v;")) is True
    assert statements.begins(line("    return 0;")) is True
    # A K&R body is no function's: refused, and told apart.
    assert statements.begins(line("    long r = a + b;")) is False
    assert statements.old_style(line("    long r = a + b;"))
    assert not statements.old_style(line("    return 0;"))


def test_a_function_try_block_and_its_handlers_are_function_bodies():
    # Review of #105 (round 4): `long tryfun(P p) try {` read as a `try` statement's block
    # outside any function, so its statements "continued" one.
    text = (
        "long tryfun(P p) try {\n"
        "    t = p.x + g();\n"
        "} catch (const E &) {\n"
        "    return p.y;\n"
        "} catch (...) {\n"
        "    return 0;\n"
        "}\n"
        "int main() {\n"
        "    try {\n"
        "        f();\n"
        "    } catch (...) {\n"
        "        g();\n"
        "    }\n"
        "}\n"
    )
    statements = Statements(text, raw_strings=True)
    assert [statements.begins(line) for line in (2, 4, 6, 10, 12)] == [True] * 5
    # C has no function-try-block.
    assert Statements(text.replace("try {", "{"), raw_strings=False).begins(2) is True
    assert Statements(text, raw_strings=False).begins(2) is False


def test_a_functions_first_line_holds_the_statements_after_its_body_opens():
    # Review of #105 (round 3): `long one(struct P p) { long t =` / `p.x + g();` ends on
    # the next line; `{ return p.x + g(); }` on its own.
    statements = Statements(ROUND3, raw_strings=True)
    lines = ROUND3.splitlines()

    def line(text):
        return lines.index(text) + 1

    first = line("long one(struct P p) { long t =")
    assert statements.opening_statements(first) == (first, first + 1)
    two = line("long two(struct P p) { return p.x + g(); }")
    assert statements.opening_statements(two) == (two, two)
    lambda_line = line("    auto f = [&](long v) { return v + 1; };")
    assert statements.opening_statements(lambda_line) == (lambda_line, lambda_line)
    # No statement after the body opens, or no body on the line.
    assert statements.opening_statements(line("long none(void) {}")) is None
    assert statements.opening_statements(line("int main(void) {")) is None
    assert statements.opening_statements(line("    return t; }")) is None


def test_begins_is_unknown_where_the_compiler_may_read_the_text_otherwise():
    assert Statements("int f(void) {\n  int a = 1 + \\\n  2;\n}\n").begins(3) is None
    assert Statements("int f(void) {\n  int a = (1;\n}\n").begins(2) is None
    hidden = Statements("int f(void) {\n  OPEN 1;\n  g();\n}\n", macros={"OPEN"})
    assert hidden.begins(3) is None


BLOCK_STORAGE = """int main(void) {
    long q = 1, gcount = 6, r = 3;
    {
        static long a[2] = {1, 2}, q = 3;
        use(q);
    }
    {
        { static long q = 9; }
        use(q);
    }
    {
        struct S { static long q; };
        use(q);
    }
    {
        KEEP(r);
        use(r + q);
    }
    {
        extern long gcount;
        use(gcount);
    }
    {
        use(q); static long q = 4;
    }
    {
        static long other = q + 1;
        use(q);
    }
    return 0;
}
"""


def test_a_blocks_static_or_extern_of_an_outer_locals_name_is_found():
    # Review of PR #140: the code reads the block's own `static`/`extern`, which lldb-dap
    # does not list; the outer local of that name was shown under it.
    hazards = {"declares": {"KEEP"}, "expands": {"KEEP": frozenset({"static", "long", "n"})}}
    statements = Statements(BLOCK_STORAGE, raw_strings=False, hazards=hazards)

    def found(line, name):
        return statements.block_storage(line, name, 2)

    assert found(5, "q") == ("static", 4)  # after an initializer's braces too
    assert found(9, "q") is False  # a block closed before the line
    assert found(13, "q") is False  # a class member
    assert found(17, "r") is None and found(17, "q") is False  # a macro given `r`
    assert found(21, "gcount") == ("extern", 20) and found(21, "q") is False
    assert found(24, "q") == ("static", 24)  # later on the line: counted
    assert found(28, "q") is False  # only named in an initializer
    assert found(30, "q") is False  # the function's own block
    # Declared before the outer local itself: that one is the local shown.
    assert Statements(BLOCK_STORAGE, raw_strings=False).block_storage(5, "q", 5) is False
    cpp = "int main() {\n  long q = 1;\n  {\n    using ns::q;\n    use(q);\n  }\n"
    cpp += "  {\n    using namespace ns;\n    use(q);\n  }\n}\n"
    statements = Statements(cpp, raw_strings=True)
    assert statements.block_storage(5, "q", 2) == ("using", 4)
    assert statements.block_storage(9, "q", 2) is False
    # Nothing in the source declares it so: read cheaply, even where unreadable.
    assert (
        Statements("int f(void) {\n  long q = 1;\n  use(q);\n}\n").block_storage(3, "q", 2) is False
    )


def test_calls_that_may_return_twice_are_found_by_line_and_through_macros():
    from peeky.c_source import macro_hazards, returning_twice

    text = (
        "#define TRY if (setjmp(env) == 0)\n"
        "#define AGAIN TRY\n"
        "int f(void) {\n"
        "  long a = 1;\n"
        "  AGAIN { a = 2; }\n"
        '  puts("setjmp");\n'
        "  if (sigsetjmp(e, 1)) a = 3;\n"
        "  return a;\n"
        "}\n"
    )
    hazards = macro_hazards([text], raw_strings=False)
    assert hazards["setjmp"] == {"TRY", "AGAIN"}
    statements = Statements(text, raw_strings=False, hazards=hazards)
    assert statements.returns_twice(3, 9) == ((5, 7), False)  # not the string's
    assert statements.returns_twice(3, 4) == ((), False)
    assert statements.returns_twice(3, 9, extra={"puts"}) == ((5, 6, 7), False)
    with_directive = Statements(text.replace("  return a;", "#ifdef X\n  return a;\n#endif"))
    assert with_directive.returns_twice(3, 11)[1] is True
    # A library header's TRY, through another macro.
    header = "#define SJ_TRY(b) if (__builtin_setjmp(b) == 0)\n#define TRY SJ_TRY(env)\n"
    assert returning_twice([header, "#define OTHER 1\n"]) == {"SJ_TRY", "TRY"}


def test_a_storage_declaration_of_a_name_is_read_cheaply_but_not_a_parameters():
    from peeky.c_source import storage_named

    assert storage_named("{ static long q = 2; use(q); }", "q")
    assert storage_named("extern long a, gcount;", "gcount")
    assert storage_named("static long (*q)(int) = 0;", "q")
    assert storage_named("using cfg::level;", "level")
    # A static function's parameter, or a name after the statement: not declared so.
    assert not storage_named("static long run(long seed) {\n  long head = seed;", "seed")
    assert not storage_named("static long f(void) {\n  long q = 1;", "q")


MORE_HIDING = """#define ONCE static int once_flag = 0
#define KEEP(x) static long x = 5
int main(void) {
    long q = 1, gcount = 6, use2 = 4;
    { extern long gcount; use(gcount); }
    {
        static struct { long v; } q = { 5 };
        use(q.v);
    }
    {
        enum { q = 7 };
        use(q);
    }
    {
        enum class E { q };
        use(q);
    }
    {
        long use2(long);
        use(use2 != 0);
    }
    {
        v.use2(1);
        use(use2);
    }
    {
#if 0
        static long q = 2;
#endif
        use(q);
    }
    {
#ifdef NOT_DEFINED
        static long q = 2;
#endif
        use(q);
    }
    {
        extern long helper(long q);
        static long (*gcount)(long) = 0;
        use(q);
    }
    {
        ONCE;
        use(q);
    }
    {
        KEEP(q);
        use(q);
    }
    {
        static thread_local long q = 8;
        use(q);
    }
    return 0;
}
"""


def test_names_a_block_declares_that_hide_an_outer_local_are_told_apart():
    # Review of PR #144: a block opening on the stop line, a declarator after a type's
    # body, an enumerator and a function declaration each hide the outer local; `#if 0`
    # text, a prototype's parameter and a macro whose expansion names another do not.
    from peeky.c_source import macro_hazards

    hazards = macro_hazards([MORE_HIDING], raw_strings=True)
    assert hazards["declares"] == {"ONCE", "KEEP"} and "once_flag" in hazards["expands"]["ONCE"]
    statements = Statements(MORE_HIDING, raw_strings=True, hazards=hazards)
    expected = {
        5: ("gcount", ("extern", 5)),  # a block opening on the stop line
        8: ("q", ("static", 7)),  # after a type's body
        12: ("q", ("enumerator", 11)),
        16: ("q", False),  # a scoped enum's enumerator
        20: ("use2", ("function", 19)),
        24: ("use2", False),  # a member's call declares nothing
        30: ("q", False),  # `#if 0`
        36: ("q", None),  # another conditional: Peeky cannot tell
        41: ("q", False),  # a prototype's parameter; another's grouped declarator
        45: ("q", False),  # ONCE's expansion names `once_flag`, not `q`
        49: ("q", None),  # KEEP given `q`
        53: ("q", ("thread_local", 52)),  # said as a thread's own, listed apart
    }
    for line, (name, found) in expected.items():
        assert statements.block_storage(line, name, 4) == found, line
    assert statements.block_storage(41, "gcount", 4) == ("static", 40)


def test_a_cheap_read_finds_declarators_after_braces_enumerators_and_functions():
    from peeky.c_source import storage_named

    assert storage_named("{ static long a[2] = {1, 2}, q = 3; }", "q")
    assert storage_named("{ static struct { long v; } q = { 5 }; }", "q")
    assert storage_named("{ enum { q = 7 }; }", "q")
    assert storage_named("{ long use2(long); }", "use2")
    assert storage_named("{ typedef long q; }", "q")
    assert storage_named("{ struct q { long v; }; }", "q")
    assert not storage_named("{ long r = 1; use(r); }", "q")


# Review of PR #144 (round 2): what Peeky reads is the translation unit as the compiler
# preprocessed it; Clang's output for a function, with a system header, a fragment
# included in a block and a `_Pragma` splitting a line.
PREPROCESSED = b"""# 1 "dir/a.c"
# 1 "<built-in>" 1
# 1 "<built-in>" 3
# 1 "<command line>" 1
# 1 "<built-in>" 2
# 1 "dir/a.c" 2
# 1 "/usr/include/stdio.h" 1 3 4
extern int printf(const char *, ...);

# 2 "dir/a.c" 2
long f(long q) {
    long total = 1;
# 3 "dir/a.c"
#pragma clang diagnostic push
# 3 "dir/a.c"
                     total += 2;
    {
        enum { q = 7 };
# 1 "dir/./frag.inc" 1
total += 9;
# 7 "dir/a.c" 2
        total += q;
    }
# 20 "dir/a.c"
    return total;
}
"""


def test_a_preprocessed_unit_is_read_back_onto_its_sources():
    from peeky.c_source import Preprocessed

    own = {"dir/a.c", "dir/frag.inc"}
    pre = Preprocessed(
        PREPROCESSED.splitlines(keepends=True),
        lambda path: path.replace("/./", "/") if path.replace("/./", "/") in own else None,
    )
    lines = pre.text.splitlines()
    # A system header's run is one `#include` line; the `_Pragma`'s pieces are one line.
    assert lines[pre.line("dir/a.c", 2) - 2] == "#include <another file>"
    assert " ".join(lines[pre.line("dir/a.c", 3) - 1].split()) == "long total = 1; total += 2;"
    assert pre.line("dir/frag.inc", 1) == pre.line("dir/a.c", 5) + 1
    assert lines[pre.line("dir/a.c", 7) - 1].strip() == "total += q;"
    assert lines[pre.line("dir/a.c", 20) - 1].strip() == "return total;"
    assert pre.line("dir/a.c", 12) is None and pre.files == own
    assert pre.origins[pre.line("dir/a.c", 5) - 1] == ("dir/a.c", 5)
    statements = Statements(pre.text, raw_strings=False, trigraphs=False)
    found = statements.block_storage(pre.line("dir/a.c", 7), "q", None, True)
    assert pre.origins[found[1] - 1] == ("dir/a.c", 5) and found[0] == "enumerator"
    # A system header's text inside a block: Peeky cannot tell what it declares.
    inside = PREPROCESSED.replace(b"total += 9;", b'# 1 "/usr/include/x.h" 1 3\nint x;')
    pre = Preprocessed(
        inside.splitlines(keepends=True), lambda path: path if "usr" not in path else None
    )
    statements = Statements(pre.text, raw_strings=False, trigraphs=False)
    assert statements.block_storage(pre.line("dir/a.c", 7), "q", None, True) is None
    # A file the output gives twice: its lines are not singled out.
    twice = PREPROCESSED + b'# 1 "dir/./frag.inc" 1\ntotal += 9;\n'
    pre = Preprocessed(twice.splitlines(keepends=True), lambda path: path.replace("/./", "/"))
    assert pre.line("dir/frag.inc", 1) == 0


NESTED_ENUMS = """long run(void) {
    long q = 1, r = 2, total = 0;
    {
        struct S { enum { q = 3 } kind; long v; } s = { 0, 2 };
        total += q;
    }
    {
        total += (enum { r = 4 })0 + r;
    }
    {
        struct T { long v; } t = { 1 };
        struct q { long v; } u = { 2 };
        total += q;
    }
    {
        struct q;
        total += q;
    }
    return total;
}
"""


def test_c_gives_enumerators_anywhere_in_a_block_its_scope_and_cpp_a_class_name():
    # Review of PR #144 (round 2): in C an enum nested in a struct's body (or a cast)
    # declares its enumerators in the block; in C++ they are the class's. A C++ class a
    # block declares hides the outer local; a C struct's tag does not.
    c = Statements(NESTED_ENUMS, raw_strings=False, trigraphs=False)
    cpp = Statements(NESTED_ENUMS, raw_strings=True)
    assert c.block_storage(5, "q", 2) == ("enumerator", 4)
    assert cpp.block_storage(5, "q", 2) is False
    assert c.block_storage(8, "r", 2) == ("enumerator", 8)
    assert c.block_storage(13, "q", 2) is False
    assert cpp.block_storage(13, "q", 2) == ("class", 12)
    assert cpp.block_storage(17, "q", 2) == ("class", 16)
    # In the local's own block, a class of its name leaves the variable named.
    own = "long run() {\n    long q = 1;\n    struct q { long v; };\n    return q;\n}\n"
    assert Statements(own, raw_strings=True).block_storage(4, "q", 2) is False
    attributed = "long run() {\n    long q = 1;\n    {\n        struct [[maybe_unused]] q;\n"
    attributed += "        enum class alignas(8) E { a };\n        return 0;\n    }\n}\n"
    assert Statements(attributed, raw_strings=True).block_storage(6, "q", 2) == ("class", 4)
    # A function returning a struct it defines: its body does not hide the declaration.
    returning = "long run(void) {\n    long use2 = 1;\n    {\n"
    returning += "        struct S { long v; } use2(long);\n        return 0;\n    }\n}\n"
    for raw_strings in (False, True):
        found = Statements(returning, raw_strings=raw_strings).block_storage(5, "use2", 2)
        assert found == ("function", 4)


SCOPES = """int f(int k) {
    int t = 1;
    if (int a = k; a > 1) {
        return a;
    } else if (k) {
        return 2;
    } else
        return 3;
    for (const int &r : {1, 2}) k += r;
    while (long w = k--) {
        k++;
    }
    switch (short s = k; s) { case 1: break; }
    try { k++; } catch (int &e) { k = e; }
    do {
        long d = k;
    } while (--k);
    return t;
}
"""


def test_a_declarations_scope_ends_with_its_block_or_its_header_statement():
    # Review of PR #159: clang gives a `while`'s or a `switch`'s condition variable no
    # block of its own, so the source says where its scope ends: with its statement.
    statements = Statements(SCOPES, raw_strings=True)
    expected = {
        (2, "t"): (2, 19),  # to the function's `}`
        (3, "a"): (3, 8),  # an `if` with its `else if` and `else`
        (9, "r"): (9, 9),  # a range-`for` with no braces
        (10, "w"): (10, 12),
        (13, "s"): (13, 13),
        (14, "e"): (14, 14),
        (16, "d"): (16, 17),  # a `do` body's block
    }
    for (line, name), scope in expected.items():
        assert statements.scope_lines(line, name) == scope, (line, name)
    assert statements.scope_lines(4, "nothing") is None


SAME_LINE = """int f(int k) {
    int t = 1;
    {
        long t = 11; while (t > 20) t--;
        t += 1;
    }
    {
        long t = 11; { t += 1; }
        t += 1;
    }
    {
        long t = 1; t = 2;
    }
    {
        auto [t, u] = g();
        t += 1;
    }
    {
        long t = 1; { long t = 2; }
    }
    return t;
}
"""


def test_a_scope_is_read_from_the_statement_that_declares_the_name():
    # Review of PR #159 (round 3): a later statement on the line that only names it (a
    # header, a block) is not its declaration; two that declare it tell nothing.
    statements = Statements(SAME_LINE, raw_strings=True)
    assert statements.scope_lines(4, "t") == (4, 6)
    assert statements.scope_lines(8, "t") == (8, 10)
    assert statements.scope_lines(15, "t") == (15, 17)  # a binding, the one naming it
    assert statements.scope_lines(19, "t") is None
    # Where it has run still counts the last statement naming it.
    assert statements.declared_through(12, "t") == 12
    assert statements.declaration(12, "t") != statements.declaration(12, "t", declaring=True)


BODY_SCOPES = """int f(int c) {
    long t = 1;
    while (c) long t = 8;
    do long t = 9; while (0);
    switch (c) case 1: long t = 4;
    while (c)
        long
          t = 2;
    {
        long
          t = 3;
        g(t);
    }
    again: long u = 1;
    return t + u;
}
"""


def test_a_bodys_declaration_without_braces_is_scoped_to_its_statement():
    # Review of PR #159 (round 4): clang gives a `while`'s, a `do`'s or a `switch`'s
    # body no block; a declaration that is the whole body ends with its `;`.
    statements = Statements(BODY_SCOPES, raw_strings=True)
    assert [statements.scope_lines(line, "t") for line in (3, 4, 5, 8, 11)] == [
        (3, 3),
        (4, 4),
        (5, 5),
        (8, 8),
        (11, 13),  # a declaration over lines, in a block
    ]
    assert statements.scope_lines(14, "u") == (14, 16)  # a label in a block


BINDING_SOURCE = """int f() {
    long t = 1;
    for (auto [t, u] : pairs) {
        use(t);
    }
    {
        const auto& [t, b] = s;
        use(t);
    }
    if (auto [t, v] = g(); t) use(t);
    return t;
}
"""


def test_a_structured_binding_hides_an_outer_local_where_it_is_in_scope():
    # Review of PR #159 (round 4): LLDB may list no binding (clang 18 on Linux).
    statements = Statements(BINDING_SOURCE, raw_strings=True)
    assert statements.block_storage(4, "t", 2) == ("binding", 3)
    assert statements.block_storage(8, "t", 2) == ("binding", 7)
    assert statements.block_storage(10, "t", 2) == ("binding", 10)
    assert statements.block_storage(11, "t", 2) is False
    # The binding's own row (declared on line 3) is not hidden by itself.
    assert statements.block_storage(4, "t", 3) is False


LAMBDA_SOURCE = """int f() {
    long t = 1;
    auto lam = [&]() { auto [t, u] = g(); struct t2 {}; return t; };
    use(t);
    { auto [t, u] = g(); use(t); }
    return t;
}
"""


def test_what_braces_in_a_statement_declare_hides_nothing_in_its_block():
    # Review of PR #159 (round 5): a lambda's binding hid the function's `t` after it.
    statements = Statements(LAMBDA_SOURCE, raw_strings=True)
    assert statements.block_storage(4, "t", 2) is False
    assert statements.block_storage(4, "t2", 2) is False
    # A binding with a use on its line declares it, so its scope can be read.
    assert statements.scope_lines(5, "t") == (5, 5)
    assert statements.block_storage(5, "t", 2) == ("binding", 5)


BINDS_SOURCE = """int main() {
    auto [t, u] = make(i)[0];
    auto &[r, s] = held[1]; const auto&& [a, b] = three; // auto [c, d] = no;
    for (const auto &[k, v] : pairs) { use(k); }
    long &&w = std::move(x); auto [w2, z] = y;
    auto lam = [&] { auto [m, n] = g(); return m; }; long &m = x;
    auto [e, f] = make([] { long e = 4; return e; });
    long q = 1, &[[maybe_unused]] p = q;
    use("auto [g, h] = x;");
    auto [first,
          second] = p; long &&third = q();
}
"""


def test_a_line_binds_a_name_only_by_a_structured_bindings_brackets():
    # #291: Apple clang 21 describes `t` of `auto [t, u] = pair;` as a plain `long &&`:
    # only the line tells that it names the element, not a reference the code declares.
    statements = Statements(BINDS_SOURCE, raw_strings=True)
    names = ("t", "u", "r", "s", "a", "b", "c", "d", "k", "v", "w", "w2", "z", "m", "n")
    found = {
        (line, name)
        for line in range(1, 14)
        for name in (*names, "e", "f", "q", "p", "g", "h", "first", "second", "third")
        if statements.binds(line, name)
    }
    assert found == {
        (2, "t"),
        (2, "u"),
        (3, "r"),
        (3, "s"),
        (3, "a"),
        (3, "b"),
        (4, "k"),
        (4, "v"),
        (5, "z"),
        (5, "w2"),
        (7, "e"),
        (7, "f"),
        (10, "first"),
        (11, "second"),
    }
    # A comment's or a string's brackets bind nothing (`c`, `g`), nor does a lambda's
    # body hold the line's own (`m` is the reference `long &m` there); a plain reference
    # (`w`, `p`, `third` after a binding's brackets that closed) is no binding; nor is any
    # of C. Brackets over two lines bind each line's name.
    assert not Statements(BINDS_SOURCE, raw_strings=False).binds(2, "t")
    assert not statements.binds(0, "t") and not statements.binds(99, "t")


RANGE_LAMBDA_SOURCE = """int f() {
    long t = 1;
    for (auto [t, u] : make([&] { long t = 40; return id(t); })) {
        use(t);
    }
    for (auto [t, u] : make([&] { return id(t); })) { use(t); }
    for (auto [t, u] : make([&] { return id(t); }, [&] { long t = 2; return t; })) {
    }
    for (auto [t, u] : c ? a : b) {
    }
    return t;
}
"""


def test_a_range_for_binding_names_nothing_in_its_range_or_a_lambda_there():
    # #169: the header's binding hid a lambda's own `t` in the range's expression.
    statements = Statements(RANGE_LAMBDA_SOURCE, raw_strings=True)
    # In the lambda's frame (a function wholly on the line): only its body counts.
    assert statements.block_storage(3, "t", 3, True, body=True) is False
    assert statements.block_storage(6, "t", None, True, body=True) is False
    # Read as the enclosing function's line, the binding is named there.
    assert statements.block_storage(3, "t", 2, True) == ("binding", 3)
    # Two bodies open on the line: it is read as any other.
    assert statements.block_storage(7, "t", 2, True, body=True) == ("binding", 7)
    # The binding's own row, declared on a line where a lambda declares a `t` too.
    assert statements.block_storage(4, "t", 3, True) is False
    # At the line's first instruction, the range's expression runs first: the binding
    # is not declared yet, unless the loop's body has code on the line, or a `?` makes
    # the `:` uncertain.
    assert statements.block_storage(3, "t", 2, True, entering=True) is False
    assert statements.block_storage(6, "t", 2, True, entering=True) == ("binding", 6)
    assert statements.block_storage(9, "t", 2, True, entering=True) == ("binding", 9)
    # In the loop's body, the binding is named.
    assert statements.block_storage(4, "t", 2, True, entering=True) == ("binding", 3)


RANGE_HEADER_SOURCE = """int f(P p, P *q) {
    long t = 1;
    for (const auto &[t, u] : make(t)) { use(t * u); }
    for (auto [t, u] : v) { use(t); }
    for (auto [t, u] : make(t)) {
    for (auto [t, u] : make(p.t, q->t, n::t)) { use(t); }
    for (auto [t, u] : make(p.template t<0>(), q->template t<1>(), P::template t<2>())) {
    for (auto [t, u] : make(sizeof(t), sizeof t)) { use(t); }
    for (auto [t, u] : make(decltype(t){2}, noexcept(id(t)))) { use(t); }
    for (auto [t, u] : make([&](long t) { return id(t); })) { use(t); }
    for (auto [t, u] : make([&] { return id(t); })) { use(t); }
    for (auto w = make(t); auto [t, u] : w) { use(t); }
    for (auto [a, b] : make(t)) { use(a); }
    for (auto [t, u] = make(t); u < 3; u++) { use(t); }
    for (auto [t, u] : c ? make(t) : v) { use(t); }
    for (const auto &[t, u] :
         make(t)) { use(t * u); }
    for (const auto &[t, u]
         : w) { use(t); }
    }
    }
    return t;
}
"""


def test_a_range_for_header_says_where_it_names_the_outer_local():
    # #283: a range-`for` header naming the outer `t` outside its binding runs that code
    # before the binding exists. A mention is not a read (`sizeof`, `decltype`, a lambda's
    # body, a lambda's own `t`), so each one counts, but no member's or qualified name.
    statements = Statements(RANGE_HEADER_SOURCE, raw_strings=True)
    found = {line: statements.range_header(line, "t") for line in range(3, 21)}
    named = {line: lines for line, lines in found.items() if lines is not None}
    # Lines 3 and 5 (the range), 8 and 9 (operands never evaluated), 10 and 11 (lambdas),
    # 12 (an init-statement) and 15 (a `?` in the range); 16 and 17 hold one header.
    assert named == {
        **{line: (line, line) for line in (3, 5, 8, 9, 10, 11, 12, 15)},
        16: (16, 17),
        17: (16, 17),
    }, named
    # Members, `p.template t<0>()` among them (6, 7); another binding (13); a classic
    # `for` (14); a header over two lines that does not name it (18, 19).
    assert statements.range_header(3, "u") is None
    # Nor is `P::template t<2>()` a declaration of `t`: the binding is line 7's own.
    assert statements.block_storage(7, "t", 7, True) is False
    # C has no structured bindings.
    assert Statements(RANGE_HEADER_SOURCE, raw_strings=False).range_header(3, "t") is None


OWN_STATIC = """long work(long a) {
    long t = a * 3;
    {
        static long t = 50;
        use(t);
        {
            use(t);
            extern long t;
            use(t);
        }
    }
    {
        long t = a; { static long u = 1,
            t = 2;
            use(t);
        }
    }
    long r = 1; { static long r = 2; use(r); }
    {
        typedef long q;
        use(q);
    }
    return t;
}
"""


def test_a_static_kept_as_a_constant_is_its_own_rows_declaration():
    # Review of PR #175: at -O1 lldb-dap lists a block's `static` it keeps as a constant
    # as a local (`t @ oc.c:4`); its own declaration does not hide it.
    statements = Statements(OWN_STATIC, raw_strings=False)
    assert statements.block_storage(5, "t", 4) == ("static", 4)
    assert statements.block_storage(5, "t", 4, own=4) is False
    assert statements.block_storage(7, "t", 4, own=4) is False
    # A later declaration there still hides it.
    assert statements.block_storage(9, "t", 4, own=4) == ("extern", 8)
    # The outer local's row (line 2) is still hidden by the static.
    assert statements.block_storage(5, "t", 2, own=2) == ("static", 4)
    # The static's declarator is on the next line: the row declared on line 13 (the
    # only `t` there) is the block's automatic local, hidden by the static.
    assert statements.block_storage(15, "t", 13, own=13) == ("static", 13)
    # Two declarations of `r` on the line: Peeky cannot tell which the row is.
    assert statements.block_storage(18, "r", 18, own=18) == ("static", 18)
    # A `typedef` declares no variable: never a row's own.
    assert statements.block_storage(21, "q", 20, own=20) == ("typedef", 20)


def test_identifiers_in_any_script_are_read_as_names():
    # #195: `ñ` and an NFD `größe` (a combining mark) are identifiers to Clang.
    from peeky.c_source import storage_named

    nfd = "gro\u0308\u00dfe"
    source = f"int main(void) {{\n  int ñ = 5,\n      k = 1;\n  long {nfd} = ñ;\n  return 0;\n}}\n"
    statements = Statements(source)
    assert statements.declared_through(2, "ñ") == 3
    assert statements.declared_through(4, nfd) == 4
    # A name's bounds take every identifier character, a combining mark too.
    assert storage_named(f"static long {nfd} = 2;", nfd)
    assert not storage_named("static long gro\u0308\u00dfe\u0301 = 2;", nfd)
    assert not storage_named("static long x·ñ = 2;", "ñ")
    # Clang reads U+00A0 as a space, never as part of a name.
    assert (
        Statements("int f(void) {\n  int\u00a0q = 1;\n  return q;\n}\n").declared_through(2, "q")
        == 2
    )
