"""C and C++ values the stopped frame reads while they are being built or after a jump
past their initialization (#121): withheld, never shown as values."""

import json
import os

import pytest

from peeky.cli import main

HELPERS = """
__attribute__((noinline)) static long twice(long v) {
    long w = v * 2;
    return w;
}

__attribute__((noinline)) static void scribble(void) {
    volatile long junk[96];
    for (int i = 0; i < 96; i++) junk[i] = 0x5150 + i;
}
"""

AGGREGATES = (
    """#include <stdio.h>

struct pair { long x, y; };
struct outer { struct pair in; };
struct trio { long x; struct pair p; };
"""
    + HELPERS
    + """
__attribute__((noinline)) static long run(long seed) {
    long head = seed;
    struct outer o = {
        .in = {
            .x = twice(1),  // o.in.x
            .y = twice(2),  // o.in.y
        },
    };
    struct pair ps[2] = {  // ps
        { twice(3), twice(4) },
        { twice(5), twice(6) },  // ps[1]
    };
    struct trio t = { seed,  // t
        { twice(7),
          twice(8) } };  // t.p.y
    for (int i = 0; i < 2; i++) {
        head += twice(i);  // loop body
    }
    printf("%ld %ld %ld %ld\\n", o.in.y, ps[1].y, t.p.y, head);
    return o.in.x;
}

int main(void) {
    scribble();
    return (int)(run(5) & 1);
}
"""
)

# The same in C++, without designated initializers (C++20).
AGGREGATES_CPP = (
    AGGREGATES.replace("<stdio.h>", "<cstdio>")
    .replace("(void)", "()")
    .replace(".in = {", "{")
    .replace(".x = twice(1)", "twice(1)")
    .replace(".y = twice(2)", "twice(2)")
)


@pytest.fixture
def directory(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def line_of(source, marker):
    return next(n for n, text in enumerate(source.splitlines(), 1) if marker in text)


def shown_at(capsys, path, line, *selectors, hit=1, steps=0, frame=0):
    """The rows `session get` gives for ``selectors`` at ``path:line``."""
    assert main(["session", "start", str(path), "--at", str(line), "--hit", str(hit)]) == 0
    capsys.readouterr()
    try:
        for _ in range(steps):
            assert main(["session", "step"]) == 0
        capsys.readouterr()
        main(["session", "get", "--frame", str(frame), *selectors, "--json"])
        return {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
    finally:
        main(["session", "close"])
        capsys.readouterr()


def withheld(row, *phrases):
    return "value" not in row and all(phrase in row.get("error", "") for phrase in phrases)


@pytest.mark.timeout(300)
@pytest.mark.parametrize("suffix", ["c", "cpp"])
def test_an_aggregate_is_withheld_while_its_initializer_runs_in_the_stopped_frame(
    directory, capsys, suffix
):
    # #121: clang puts no code on the declaration line of a nested struct, an array of
    # structs or a 2-D array (or only part of it), so a stop inside the initializer read
    # leftovers (`o.in.y = 20905`, `ps[1].x = -1530060163171811189`).
    source = AGGREGATES if suffix == "c" else AGGREGATES_CPP
    path = directory / f"aggregates.{suffix}"
    path.write_text(source)
    o, ps, t = (line_of(source, marker) for marker in ("struct outer o", "// ps", "// t"))
    o_end, ps_end, t_end = o + 5, ps + 3, t + 2
    rows = shown_at(capsys, path, line_of(source, "// o.in.y"), "o", "o.in.y", "ps", "t", "head")
    assert withheld(rows["o"], f"declaration (lines {o}-{o_end}) is still running at this stop")
    assert withheld(rows["o.in.y"], "'o' has no value here")
    # Below the stop, with no code on the declaration line: placed by its elements' code.
    assert withheld(rows["ps"], f"declared at line {ps}, below this stop")
    assert withheld(rows["t"], f"declared at line {t}, below this stop")
    assert rows["head"]["value"] == "5"
    rows = shown_at(capsys, path, line_of(source, "// ps[1]"), "o.in.y", "ps", "ps[1].x")
    assert rows["o.in.y"]["value"] == "4"
    assert withheld(rows["ps"], f"(lines {ps}-{ps_end}) is still running")
    assert withheld(rows["ps[1].x"], "'ps' has no value here")
    rows = shown_at(capsys, path, line_of(source, "// t.p.y"), "t", "ps[1].x")
    assert withheld(rows["t"], f"(lines {t}-{t_end}) is still running")
    assert rows["ps[1].x"]["value"] == "10"
    # A step from the first member's line onto the next one.
    rows = shown_at(capsys, path, line_of(source, "// o.in.x"), "o", steps=1)
    assert withheld(rows["o"], f"(lines {o}-{o_end}) is still running")
    # Once its declaration has run, and a loop counter in its body, read as ever.
    rows = shown_at(capsys, path, line_of(source, "// loop body"), "t.p.y", "i", "head", hit=2)
    assert [rows[name]["value"] for name in ("t.p.y", "i", "head")] == ["16", "1", "5"]


BUILDING = (
    """#include <cstdio>

struct Pair { long x, y; };
struct Trio { long x; Pair p; };
"""
    + HELPERS.replace("(void)", "()")
    + """
struct Widget {
    long a, b;
    explicit Widget(long x) : a(twice(x)), b(first() + 1) {
        long total = a + b;  // constructor body
        b = total;
    }
    long first() const {
        return a;  // called by the constructor
    }
    long sum() const {
        return a + b;  // called once built
    }
};

__attribute__((noinline)) static long run(long seed) {
    Trio t{ seed, { [&]() {
                      long got = twice(t.x);  // lambda in t's initializer
                      return got;
                    }(),
                    twice(4) } };
    Widget w(seed);
    auto later = [&]() {
        return w.sum() + t.p.y;  // lambda called later
    };
    return later();
}

int main() {
    scribble();
    std::printf("%ld\\n", run(3));
    return 0;
}
"""
)


@pytest.mark.timeout(300)
def test_this_is_withheld_while_its_object_is_being_built(directory, capsys):
    # #121: the stopped `Widget::Widget` showed `this.a = 20908` and `this.b = 20909`, and
    # a lambda called inside `t`'s initializer showed `this.t.p` (its capture) unset.
    path = directory / "building.cpp"
    path.write_text(BUILDING)
    rows = shown_at(capsys, path, line_of(BUILDING, "// constructor body"), "this", "a", "x")
    assert withheld(rows["this"], "the object this constructor (Widget::Widget(long)) is building")
    assert withheld(rows["a"], "if the code means a member, this has no value here: the object")
    assert rows["x"]["value"] == "3"
    rows = shown_at(capsys, path, line_of(BUILDING, "// called by the constructor"), "this")
    assert withheld(rows["this"], "a constructor (Widget::Widget(long)) called this frame")
    # The constructor as a caller frame (while `twice` runs for `a`).
    rows = shown_at(capsys, path, line_of(BUILDING, "return w;"), "this", hit=3, frame=1)
    assert rows["this"]["frame"] == 1 and withheld(rows["this"], "is building")
    rows = shown_at(capsys, path, line_of(BUILDING, "// lambda in t's"), "this", "this.t")
    assert withheld(rows["this"], "this lambda is defined in the declaration of `t`")
    # Built: a member function called later, and a lambda called after its declaration.
    rows = shown_at(capsys, path, line_of(BUILDING, "// called once built"), "this.a", "this.b")
    assert [rows["this.a"]["value"], rows["this.b"]["value"]] == ["6", "13"]
    rows = shown_at(capsys, path, line_of(BUILDING, "// lambda called later"), "this.t.p.y")
    assert rows["this.t.p.y"]["value"] == "8"


JUMPS = (
    """#include <stdio.h>

struct pair { long x, y; };
"""
    + HELPERS
    + """
__attribute__((noinline)) static long run(int k) {
    long out = 0;
    switch (k) {
    case 0:
        out = 1;
        struct pair p = { 7, 8 };
    case 1:  // case 1
        out += twice(p.x);  // after case 1
        break;
    }
    long before = twice(1);
    if (k > 0)
        goto skip;
    long mid = twice(2);
skip:  // skip
    out += before + k;  // after skip
    long after = 3;
again:
    out += after;  // after again
    if (out < 0)
        goto again;
    return out;
}

int main(void) {
    scribble();
    printf("%ld\\n", run(1));
    return 0;
}
"""
)

# C++ forbids jumping past an initialization, but not past a declaration without one.
JUMPS_CPP = (
    JUMPS.replace("<stdio.h>", "<cstdio>")
    .replace("(void)", "()")
    .replace("struct pair p = { 7, 8 };", "struct pair p;\n        p.x = 7;")
    .replace("long mid = twice(2);", "long mid;\n    mid = twice(2);")
)


@pytest.mark.timeout(300)
@pytest.mark.parametrize("suffix", ["c", "cpp"])
def test_a_local_a_jump_can_skip_is_withheld_past_the_label(directory, capsys, suffix):
    # #121: `case 1` skips `struct pair p = { 7, 8 };`, and `p` was listed with the
    # stack's contents; a `goto` from above a declaration skips it too.
    source = JUMPS if suffix == "c" else JUMPS_CPP
    path = directory / f"jumps.{suffix}"
    path.write_text(source)
    rows = shown_at(capsys, path, line_of(source, "// after case 1"), "p", "out", "k")
    label = line_of(source, "// case 1")
    assert withheld(rows["p"], f"can reach the label on line {label}")
    assert [rows["out"]["value"], rows["k"]["value"]] == ["0", "1"]
    rows = shown_at(capsys, path, line_of(source, "// after skip"), "mid", "before")
    assert withheld(rows["mid"], f"can reach the label on line {line_of(source, '// skip')}")
    # Jumped to only from after it, or not at all: declared before the label.
    assert rows["before"]["value"] == "2"
    rows = shown_at(capsys, path, line_of(source, "// after again"), "after", "before")
    assert [rows["after"]["value"], rows["before"]["value"]] == ["3", "2"]
