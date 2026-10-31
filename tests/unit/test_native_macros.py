"""native_macros: a unit's probes proven by the build's own record of its macros."""

from peeky.native_macros import (
    END,
    build_records,
    conditionals,
    difference,
    lone,
    replay_record,
    unsettled,
)

DUMP = """src/prog:\tfile format elf64-littleaarch64

.debug_macro contents:
0x00000000:
macro header: version = 0x0005, flags = 0x02, format = DWARF32, debug_line_offset = 0x00000000
DW_MACRO_start_file - lineno: 0 filenum: 0
  DW_MACRO_start_file - lineno: 3 filenum: 1
    DW_MACRO_define_strx - lineno: 1 macro: A_H
    DW_MACRO_define_strx - lineno: 2 macro: MAX(a,b) ((a) > (b) ? (a) : (b))
    DW_MACRO_undef_strx - lineno: 4 macro: B
  DW_MACRO_end_file
DW_MACRO_end_file
DW_MACRO_define_strx - lineno: 0 macro: __clang__ 1

0x00000040:
macro header: version = 0x0005, flags = 0x02, format = DWARF32, debug_line_offset = 0x00000100
DW_MACRO_start_file - lineno: 0 filenum: 0
DW_MACRO_end_file
"""

REPLAYED = """# 1 "src/main.c"
# 1 "<built-in>" 1
# 1 "<built-in>" 3
#define __clang__ 1
# 1 "<command line>" 1
# 1 "<built-in>" 2
# 1 "src/main.c" 2


# 1 "src/a.h" 1
#define A_H 
#define MAX(a,b) ((a) > (b) ? (a) : (b))

#undef B
# 4 "src/main.c" 2
int main(void) { return 0; }
"""


def test_the_build_s_record_of_macros_is_read_by_unit(tmp_path):
    main, header = str(tmp_path / "src/main.c"), str(tmp_path / "src/a.h")
    records = build_records(DUMP.splitlines(), {0: {0: main, 1: header}})
    assert records == {
        0: [
            ("start", 0, main),
            ("start", 3, header),
            ("define", 1, "A_H"),
            ("define", 2, "MAX(a,b) ((a) > (b) ? (a) : (b))"),
            ("undef", 4, "B"),
            END,
            END,
            ("define", 0, "__clang__ 1"),
        ]
    }
    # What Peeky can't read, it says.
    assert build_records(DUMP.splitlines(), {0: {0: main}})[0] == (
        "its record of macros names file 1, which its line table doesn't"
    )
    odd = DUMP.replace("DW_MACRO_undef_strx - lineno: 4 macro: B", "DW_MACRO_import - offset: 0x10")
    assert build_records(odd.splitlines(), {0: {0: main, 1: header}})[0] == (
        "its record of macros holds DW_MACRO_import, which Peeky doesn't read"
    )
    twice = DUMP.replace("debug_line_offset = 0x00000100", "debug_line_offset = 0x00000000")
    assert build_records(twice.splitlines(), {0: {0: main, 1: header}})[0] == (
        "its line table has more than one record of macros"
    )


def test_a_replay_with_dd_leaves_the_record_the_build_does(tmp_path):
    (tmp_path / "src").mkdir()
    main, header = str(tmp_path / "src/main.c"), str(tmp_path / "src/a.h")
    replayed, entered = replay_record(REPLAYED.splitlines(True), str(tmp_path))
    build = build_records(DUMP.splitlines(), {0: {0: main, 1: header}})[0]
    assert replayed == build
    assert {main, header, "src/main.c", "src/a.h"} <= entered
    shown = lambda path: path.rsplit("/", 1)[-1]  # noqa: E731
    assert difference(build, replayed, shown) is None
    moved = [("define", 2, "A_H") if event == ("define", 1, "A_H") else event for event in build]
    assert difference(moved, replayed, shown) == (
        "at a.h, the build recorded #define A_H on line 2, the replay #define A_H on line 1"
    )
    assert (
        difference(build[:-1], replayed, shown)
        == "only the replay recorded #define __clang__ on line 0"
    )
    # A return to a file never entered: Peeky can't follow it.
    odd = REPLAYED.replace('# 4 "src/main.c" 2', '# 4 "src/other.c" 2')
    assert replay_record(odd.splitlines(True), str(tmp_path))[0] is None


STDATOMIC = """#if __STDC_HOSTED__ && \\
    __has_include_next(<stdatomic.h>)
# include_next <stdatomic.h>
#else
#include <stddef.h>
#define ATOMIC_BOOL_LOCK_FREE 2
#endif
"""


def test_a_group_is_settled_where_at_most_one_branch_may_leave_no_record():
    # clang's stdatomic.h: its #else defines macros; its include of the system's may leave
    # none.
    where = conditionals(STDATOMIC)
    group, index = where[2]
    assert where[1] == where[2] and index == 0
    assert [each.kind for each in group.branches] == ["if", "else"]
    assert unsettled(group) is None

    def first(text):
        return unsettled(conditionals(text)[1][0])

    silent = "two of its group's branches may leave no record of macros"
    # Code alone on both sides: unproven.
    assert first("#if __has_include(<x.h>)\nint x = 1;\n#else\nint x = 2;\n#endif\n").startswith(
        silent
    )
    # A #define, and an absent #else, which leaves none.
    assert first("#if __has_include(<any>) && CPP17\n#define HAS_ANY 1\n#endif\n") is None
    # An #include may leave none: a file under `#pragma once` or a guard is skipped.
    assert first("#if __has_include(<x.h>)\n#include <x.h>\n#endif\n").startswith(silent)
    # An #error fails the build: no way past it leaves no record.
    error = '#if __has_include(<x.h>)\nint x;\n#else\n#error "no x.h"\n#endif\n'
    assert first(error) is None
    # Comments and literals are no directives; an unclosed group is none.
    assert first("#if __has_include(<x.h>)\n/*\n#define X 1\n*/\n#endif\n").startswith(silent)
    assert conditionals("#if __has_include(<x.h>)\n#define X 1\n") == {}


def test_a_group_within_a_branch_leaves_a_record_where_each_way_through_it_does():
    # Each branch of the inner group defines X, and it has an #else: the outer branch
    # surely leaves a record.
    nested = (
        "#if __has_include(<x.h>)\n#  if A\n#    define X 1\n#  else\n#    define X 2\n"
        "#  endif\n#endif\n"
    )
    assert unsettled(conditionals(nested)[1][0]) is None
    # No #else, or a branch defining nothing: a way through it leaves none.
    for inner in (
        "#  if A\n#    define X 1\n#  endif\n",
        "#  if A\n#    define X 1\n#  else\n#  endif\n",
    ):
        text = f"#if __has_include(<x.h>)\n{inner}#endif\n"
        assert unsettled(conditionals(text)[1][0]) is not None, inner
    # A way through it that ends in an #error is none the build took.
    errors = "#if __has_include(<x.h>)\n#  if A\n#    define X 1\n#  else\n#    error no\n#  endif\n#endif\n"
    assert unsettled(conditionals(errors)[1][0]) is None


def test_a_group_holding_a_line_directive_is_unsettled():
    # `#line 100` in each branch: both define M on line 100, the same record, but their
    # code differs.
    said = "its group holds a #line or a line marker"
    lined = (
        '#if __has_include("gone.h")\n#line 100\n#define M 1\nint x = 1;\n#else\n'
        "#line 100\n#define M 1\nint x = 2;\n#endif\n"
    )
    assert unsettled(conditionals(lined)[1][0]).startswith(said)
    marker = lined.replace("#line 100", '# 100 "a.h"')
    assert unsettled(conditionals(marker)[1][0]).startswith(said)
    # In a group within it too.
    inner = (
        "#if __has_include(<x.h>)\n#define X 1\n#if A\n#line 7\n#endif\n#else\n"
        "#define X 2\n#endif\n"
    )
    assert unsettled(conditionals(inner)[1][0]).startswith(said)


def test_a_branch_asking_for_a_file_alone_is_known():
    def first(condition):
        return lone(conditionals(f"#if {condition}\n#endif\n")[1][0].branches[0])

    assert first("__has_include(<bits/types/mbstate_t.h>)") == "__has_include"
    assert first('( __has_include_next("x.h") )') == "__has_include_next"
    assert first("__has_include(<a.h>) && A") is None
    assert first("__has_include(<a.h>) || __has_include(<b.h>)") is None
    assert first("!__has_include(<a.h>)") is None
