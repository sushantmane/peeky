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


def shown_at(capsys, path, line, *selectors, hit=1, steps=0, frame=0, out=False):
    """The rows `session get` gives for ``selectors`` at ``path:line`` (after a `step
    --out` with ``out``, then ``steps`` steps)."""
    assert main(["session", "start", str(path), "--at", str(line), "--hit", str(hit)]) == 0
    capsys.readouterr()
    try:
        if out:
            assert main(["session", "step", "--out"]) == 0
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


# Review of #126 (round 1).

STEP_OUT = (
    """#include <stdio.h>

struct pair { long x, y; };
"""
    + HELPERS
    + """
__attribute__((noinline)) static long run(long seed) {
    struct pair p = { twice(1), twice(2) };  // p
    long r = twice(3) + twice(4);  // r
    long q = r + p.y;
    return q + seed;
}

int main(void) {
    scribble();
    printf("%ld\\n", run(1));
    return 0;
}
"""
)


@pytest.mark.timeout(300)
def test_a_local_declared_on_the_line_a_call_returns_to_is_withheld(directory, capsys):
    # Review of #126: back from `twice` (`step --out`), `p` showed leftovers and `r`
    # showed `20911 [settable]`, part-way through their own lines.
    path = directory / "stepout.c"
    path.write_text(STEP_OUT)
    twice = line_of(STEP_OUT, "return w;")
    rows = shown_at(capsys, path, twice, "p", "p.y", out=True)
    assert rows["p"]["line"] == line_of(STEP_OUT, "// p"), rows["p"]
    assert withheld(rows["p"], "this stop is part-way through that line")
    rows = shown_at(capsys, path, twice, "r", "p.y", hit=3, out=True)
    assert rows["r"]["line"] == line_of(STEP_OUT, "// r"), rows["r"]
    assert withheld(rows["r"], "part-way through") and rows["r"]["settable"] is False
    assert rows["p.y"]["value"] == "4"


SWITCH_INCLUDED = (
    """#include <stdio.h>

struct pair { long x, y; };
"""
    + HELPERS
    + """
__attribute__((noinline)) static long run(int k) {
    long out = 0;
    switch (k) {
    case 0:;
        struct pair p = { 7, 8 };
        out = p.x;
#include "cases.h"
    }
    return out;
}

int main(void) {
    scribble();
    printf("%ld\\n", run(1));
    return 0;
}
"""
)
CASES = "    case 1:\n        out = twice(p.x);\n        out += p.y;  // read\n        break;\n"


@pytest.mark.timeout(300)
def test_a_local_read_in_an_included_case_is_withheld(directory, capsys):
    # Review of #126: `case 1:` in `cases.h` skips `p`'s initializer in `sw.c`, and
    # `get p` there showed the stack's contents.
    (directory / "cases.h").write_text(CASES)
    path = directory / "sw.c"
    path.write_text(SWITCH_INCLUDED)
    # Back from `twice` on the case's first line, then a step to the next.
    rows = shown_at(
        capsys, path, line_of(SWITCH_INCLUDED, "return w;"), "p", "k", out=True, steps=1
    )
    assert rows["p"]["line"] == line_of(CASES, "// read"), rows["p"]
    assert withheld(rows["p"], "this frame runs cases.h:3, outside its function's own lines")
    assert rows["k"]["value"] == "1"


MEMBERS = """#include <cstdio>

__attribute__((noinline)) static long twice(long v) {
    long w = v * 2;
    return w;
}

__attribute__((noinline)) static void scribble() {
    volatile long junk[96];
    for (int i = 0; i < 96; i++) junk[i] = 0x5150 + i;
}

struct Cfg {
    long a = twice(1);
    long b = twice(2);  // member b
};

__attribute__((noinline)) static long run() {
    long seed = twice(3);
    Cfg cfg{};
    long e = cfg.b + seed;  // after
    return e;
}

int main() {
    scribble();
    std::printf("%ld\\n", run());
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_an_object_a_default_member_initializer_builds_is_withheld(directory, capsys):
    # Review of #126: breaking on `Cfg`'s member line stops in `run()`, which showed
    # `cfg.b = 20907`; so did `run()` as frame 1 of `twice`.
    path = directory / "members.cpp"
    path.write_text(MEMBERS)
    member = line_of(MEMBERS, "// member b")
    rows = shown_at(capsys, path, member, "cfg", "cfg.b", "seed")
    assert rows["cfg"]["function"] == "run()" and rows["seed"]["value"] == "6"
    assert withheld(rows["cfg"], f"this frame runs members.cpp:{member}, outside its function's")
    assert withheld(rows["cfg.b"], "'cfg' has no value here")
    rows = shown_at(capsys, path, line_of(MEMBERS, "return w;"), "cfg", "seed", hit=3, frame=1)
    assert withheld(rows["cfg"]) and rows["seed"]["value"] == "6"
    rows = shown_at(capsys, path, line_of(MEMBERS, "// after"), "cfg.b")
    assert rows["cfg.b"]["value"] == "4"


BACK_INTO = (
    """#include <stdio.h>

struct pair { long x, y; };
"""
    + HELPERS
    + """
__attribute__((noinline)) static long back_into(int k) {
    long out = 0;
    int pass = 0;
    {
        struct pair p = { twice(k + pass), 8 };
        out += p.x;
    again:
        out += p.y;  // again
        if (pass > 1)
            return out;
    }
    pass++;
    if (pass < 3)
        goto again;
    return out;
}

int main(void) {
    scribble();
    printf("%ld\\n", back_into(1));
    return 0;
}
"""
)


@pytest.mark.timeout(300)
def test_a_backward_goto_into_a_block_from_after_it_withholds_its_local(directory, capsys):
    # Review of #126: `goto again` re-enters the block from after it, so `p` is
    # indeterminate on the second and third passes (C11 6.2.4).
    path = directory / "back.c"
    path.write_text(BACK_INTO)
    label = line_of(BACK_INTO, "    again:")
    for hit in (2, 3):
        rows = shown_at(capsys, path, line_of(BACK_INTO, "// again"), "p", "pass", hit=hit)
        assert withheld(rows["p"], f"can reach the label on line {label}"), rows["p"]
        assert rows["pass"]["value"] == str(hit - 1)


ORDINARY = {
    # Review of #126: one `"huh??!"` withheld every local above the stop (C reads the
    # trigraph as `|`; C++17 has none).
    "tri.c": (
        """#include <stdio.h>
static long run(int n) {
    long total = 0;
    for (int i = 0; i < n; i++) {
        total += i;  // stop
    }
    printf("%ld huh??!\\n", total);
    return total;
}
int main(void) { return (int)(run(3) & 1); }
""",
        ("total", "i", "n"),
    ),
    "tri.cpp": (
        """#include <cstdio>
static long run(int n) {
    long total = 0;
    const char *what = "huh??!";
    for (int i = 0; i < n; i++) {
        total += i;  // stop
    }
    std::printf("%ld %s\\n", total, what);
    return total;
}
int main() { return (int)(run(3) & 1); }
""",
        ("total", "i", "n"),
    ),
    # Review of #126: a statement expression's `;` kept `m` withheld for the rest of
    # the function.
    "stmtexpr.c": (
        """#include <stdio.h>
#define max(a, b) ({ __typeof__(a) _a = (a); __typeof__(b) _b = (b); _a > _b ? _a : _b; })
static long run(int n) {
    long best = 0;
    int m = max(n, 3);
    long scaled = m * 10L;
    for (int i = 0; i < m; i++) {
        best = max(best, (long)i * scaled);  // stop
    }
    printf("%ld\\n", best);
    return best;
}
int main(void) { return (int)(run(5) & 1); }
""",
        ("m", "scaled", "best"),
    ),
}


@pytest.mark.timeout(300)
@pytest.mark.parametrize("name", sorted(ORDINARY))
def test_ordinary_code_reads_every_local_above_the_stop(directory, capsys, name):
    source, names = ORDINARY[name]
    path = directory / name
    path.write_text(source)
    rows = shown_at(capsys, path, line_of(source, "// stop"), *names, hit=2)
    assert all("value" in rows[local] for local in names), rows


DEEP = """#include <cstdio>
#include <functional>

__attribute__((noinline)) static long twice(long v) {
    long w = v * 2;
    return w;
}

__attribute__((noinline)) static long dispatch(const std::function<long(long)> &f, int depth) {
    if (depth == 0)
        return f(depth + 1);
    return dispatch(f, depth - 1);
}

static long run() {
    long total = 0;
    auto cb = [&](long v) {
        total += v;  // in the lambda
        long doubled = twice(v);
        return doubled;
    };
    std::function<long(long)> f = cb;
    long a = dispatch(f, 1);
    long b = dispatch(f, 8);
    long c = dispatch(f, 80);
    return a + b + c + total;
}

int main() {
    std::printf("%ld\\n", run());
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_a_lambda_called_below_the_listed_frames_is_judged_from_more_of_them(directory, capsys):
    # Review of #126: through `std::function`, `run` (where `cb` is defined) is more than
    # ten frames below the lambda, so its `this` was withheld; up to 64 are read now.
    path = directory / "deep.cpp"
    path.write_text(DEEP)
    line = line_of(DEEP, "// in the lambda")
    for hit in (1, 2):
        rows = shown_at(capsys, path, line, "this", hit=hit)
        assert "value" in rows["this"], rows
    # Past 64 frames nothing below is known: withheld.
    rows = shown_at(capsys, path, line, "this", hit=3)
    assert withheld(rows["this"], "Peeky cannot list every frame below this one")


# Review of #126 (round 2).

HEADER_CALLS_C = (
    """#include <stdio.h>
"""
    + HELPERS
    + """
__attribute__((noinline)) static long run(long seed) {
    long total = seed;
    for (long i = twice(1); i < 20; i += 7) total += i;  // for
    for (int j = 0; j < 2; j++) total += twice(j + 5);  // counter
    return total;
}

int main(void) {
    scribble();
    printf("%ld\\n", run(1));
    return 0;
}
"""
)

HEADER_CALLS_CPP = (
    """#include <cstdio>
#include <vector>
"""
    + HELPERS.replace("(void)", "()")
    + """
__attribute__((noinline)) static long next(long *n) {
    *n -= 1;  // next
    return twice(*n);
}

__attribute__((noinline)) static std::vector<long> pair_of(long v) {
    return {v, v + 1};  // pair_of
}

__attribute__((noinline)) static long run(long seed) {
    long out = seed, n = 3;
    if (long r = twice(3)) out += r;  // if
    while (long v = next(&n)) out += v;  // while
    switch (long k = twice(2)) { default: out += k; }  // switch
    for (long x : pair_of(4)) out += x;  // range
    return out;
}

int main() {
    scribble();
    std::printf("%ld\\n", run(1));
    return 0;
}
"""
)


@pytest.mark.timeout(400)
def test_a_header_variable_whose_initializer_calls_is_withheld_after_the_call(directory, capsys):
    # Review of #126 (round 2): back from the call in a header's own initializer
    # (`step --out`), `i`, `r` and `k` were leftovers and `[settable]`; a `while`'s `v`
    # was garbage, then the last pass's value.
    path = directory / "headers.c"
    path.write_text(HEADER_CALLS_C)
    twice = line_of(HEADER_CALLS_C, "return w;")
    rows = shown_at(capsys, path, twice, "i", "total", out=True)
    assert rows["i"]["line"] == line_of(HEADER_CALLS_C, "// for"), rows["i"]
    assert withheld(rows["i"], "part-way through that line") and rows["i"]["settable"] is False
    # A counter its header sets with no call reads mid-line, as before.
    rows = shown_at(capsys, path, twice, "j", hit=2, out=True)
    assert rows["j"]["line"] == line_of(HEADER_CALLS_C, "// counter") and rows["j"]["value"] == "0"
    path = directory / "headers.cpp"
    path.write_text(HEADER_CALLS_CPP)
    twice = line_of(HEADER_CALLS_CPP, "return w;")
    for hit, name, marker in ((1, "r", "// if"), (5, "k", "// switch")):
        rows = shown_at(capsys, path, twice, name, hit=hit, out=True)
        assert rows[name]["line"] == line_of(HEADER_CALLS_CPP, marker), rows[name]
        assert withheld(rows[name], "part-way through that line"), rows[name]
    for hit in (1, 2, 3):
        rows = shown_at(capsys, path, line_of(HEADER_CALLS_CPP, "// next"), "v", hit=hit, out=True)
        assert rows["v"]["line"] == line_of(HEADER_CALLS_CPP, "// while"), rows["v"]
        assert withheld(rows["v"], "part-way through that line"), (hit, rows["v"])
    # Back from a range-`for`'s range call, its variable is not in scope yet.
    rows = shown_at(capsys, path, line_of(HEADER_CALLS_CPP, "// pair_of"), "x", out=True)
    assert rows["x"]["line"] == line_of(HEADER_CALLS_CPP, "// range") and "value" not in rows["x"]


DECLARED_BY_INCLUDE = (
    """#include <stdio.h>

struct pair { long x, y; };
"""
    + HELPERS
    + """
__attribute__((noinline)) static long run(long seed) {
    long first = twice(seed);  // first
#include "decls.h"
    return first + later.y;  // after
}

int main(void) {
    scribble();
    printf("%ld\\n", run(1));
    return 0;
}
"""
)


@pytest.mark.timeout(300)
def test_a_local_an_include_below_the_stop_declares_is_withheld(directory, capsys):
    # Review of #126 (round 2): `decls.h` declares `later`, included below line 16, and
    # `--at 16` showed its leftovers.
    (directory / "decls.h").write_text("    struct pair later = { twice(5), twice(6) };\n")
    path = directory / "rev.c"
    path.write_text(DECLARED_BY_INCLUDE)
    site = line_of(DECLARED_BY_INCLUDE, '#include "decls.h"')
    rows = shown_at(capsys, path, line_of(DECLARED_BY_INCLUDE, "// first"), "later")
    assert withheld(rows["later"], f"which the `#include` on line {site} brings in, below")
    # After it, both read: the included text holds no label a jump could use.
    rows = shown_at(capsys, path, line_of(DECLARED_BY_INCLUDE, "// after"), "later.y", "first")
    assert [rows["later.y"]["value"], rows["first"]["value"]] == ["12", "2"]


HEADER_CFG = (
    "long twice(long v);\nstruct Cfg {\n    long a = twice(1);\n    long b = twice(2);\n};\n"
)
HEADER_CFG_MAIN = """#include <cstdio>
#include "cfg.hpp"

__attribute__((noinline)) long twice(long v) {
    long w = v * 2;
    return w;
}

__attribute__((noinline)) static void scribble() {
    volatile long junk[96];
    for (int i = 0; i < 96; i++) junk[i] = 0x5150 + i;
}

__attribute__((noinline)) static long run() {
    long before = 4;
    Cfg cfg{};  // cfg
    long after = cfg.b + 1;
    long *where = &after;
    return before + after + *where;
}

int main() {
    scribble();
    std::printf("%ld\\n", run());
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_a_header_class_initializer_reads_from_the_line_that_runs_it(directory, capsys):
    # Review of #126 (round 2): stopped in `cfg.hpp`'s member initializer inside `run()`,
    # `after = 20911 [settable]` and a garbage `where` were shown; the stop now reads
    # as `main.cpp:16`, the line whose code runs it.
    (directory / "cfg.hpp").write_text(HEADER_CFG)
    path = directory / "main.cpp"
    path.write_text(HEADER_CFG_MAIN)
    rows = shown_at(
        capsys, path, line_of(HEADER_CFG_MAIN, "// cfg"), "after", "where", "before", "cfg", steps=1
    )
    assert rows["before"]["line"] in (3, 4), rows["before"]
    assert withheld(rows["after"], "below this stop") and withheld(rows["where"], "below this stop")
    assert rows["before"]["value"] == "4" and withheld(
        rows["cfg"], "outside its function's own lines"
    )


NAMESPACED = """#include <cstdio>

namespace app {

__attribute__((noinline)) long f(long x) {
    if (x < 0)
        goto out;
    x *= 2;
out:
    return x;
}

__attribute__((noinline)) long g(long y) {
    long doubled = y * 2;
    long tripled = y * 3;
out:
    long sum = doubled + tripled;  // sum
    return sum;
}

}  // namespace app

int main() {
    std::printf("%ld\\n", app::f(3) + app::g(4));
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_a_goto_in_another_function_of_a_namespace_reaches_no_label_here(directory, capsys):
    # Review of #126 (round 2): `app::f`'s `goto out;` counted for `app::g`'s own `out:`
    # (the namespace's braces bounded the search), withholding `doubled` and `tripled`.
    path = directory / "ns.cpp"
    path.write_text(NAMESPACED)
    rows = shown_at(capsys, path, line_of(NAMESPACED, "// sum"), "doubled", "tripled")
    assert [rows["doubled"]["value"], rows["tripled"]["value"]] == ["8", "12"]


# Review of #126 (round 3).

LOCAL_CLASS = """#include <cstdio>

__attribute__((noinline)) long twice(long v) {
    long w = v * 2;
    return w;
}

__attribute__((noinline)) static void scribble() {
    volatile long junk[96];
    for (int i = 0; i < 96; i++) junk[i] = 0x5150 + i;
}

struct Cfg {
    long a = twice(1);
    long b = twice(2);  // Cfg::b
};

__attribute__((noinline)) static long run() {
    struct Local {
        long a = twice(3);
        long b = twice(4);  // Local::b
    };
    long before = 1;
    Local loc{};  // loc
    long after = loc.b;
    Cfg cfg{}; long x = cfg.b + 1; long *px = &x;
    return before + after + x + *px;  // end
}

int main() {
    scribble();
    std::printf("%ld\\n", run());
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_a_caller_frame_on_a_member_initializer_above_its_function_reads_from_its_line(
    directory, capsys
):
    # Review of #126 (round 3): `run()` as frame 1 on `Cfg::b`'s line showed `x = 20908`
    # and a garbage `px`: the remapping to the line running it was the stopped frame's.
    path = directory / "localcls.cpp"
    path.write_text(LOCAL_CLASS)
    rows = shown_at(
        capsys, path, line_of(LOCAL_CLASS, "return w;"), "x", "px", "cfg", "before", hit=4, frame=1
    )
    assert rows["x"]["line"] == line_of(LOCAL_CLASS, "// Cfg::b"), rows["x"]
    assert withheld(rows["x"]) and withheld(rows["px"]) and withheld(rows["cfg"])
    assert rows["before"]["value"] == "1"
    rows = shown_at(capsys, path, line_of(LOCAL_CLASS, "// end"), "loc.b", "x", "px")
    assert [rows["loc.b"]["value"], rows["x"]["value"]] == ["8", "5"] and "value" in rows["px"]


@pytest.mark.timeout(300)
def test_a_local_classes_member_initializer_is_not_its_functions_own_line(directory, capsys):
    # Review of #126 (round 3): on the initializer of `Local`, defined inside `run()`
    # (within its own lines), `loc.b = 4366860288` was shown, stopped and as frame 1.
    path = directory / "localcls.cpp"
    path.write_text(LOCAL_CLASS)
    rows = shown_at(capsys, path, line_of(LOCAL_CLASS, "// loc"), "loc", "after", "before", steps=1)
    assert rows["loc"]["line"] == line_of(LOCAL_CLASS, "// Local::b"), rows["loc"]
    assert withheld(rows["loc"], "outside its function's own lines")
    assert withheld(rows["after"], "below this stop") and rows["before"]["value"] == "1"
    rows = shown_at(
        capsys, path, line_of(LOCAL_CLASS, "return w;"), "loc", "after", "before", hit=2, frame=1
    )
    assert withheld(rows["loc"]) and withheld(rows["after"]) and rows["before"]["value"] == "1"


# Review of #126 (round 4).

UNNAMED = """#include <cstdio>

__attribute__((noinline)) long twice(long v) {
    long w = v * 2;
    return w;
}

__attribute__((noinline)) static void scribble() {
    volatile long junk[128];
    for (int i = 0; i < 128; i++) junk[i] = 0x5150 + i;
}

struct Agg {
    long a = twice(1);
    long b = [this] {
        long got = twice(a + 1);  // lambda
        return got;
    }();
};

__attribute__((noinline)) static long nested(long seed) {
    struct {
        long s = twice(13);
        long t = twice(14);
    } anon;
    return anon.t + seed;
}

__attribute__((noinline)) static long run() {
    long before = 1;
    Agg agg{};
    long after = agg.b + before + nested(3);
    return after;  // end
}

int main() {
    scribble();
    std::printf("%ld\\n", run());
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_a_member_initializer_lambda_capturing_this_withholds_it(directory, capsys):
    # Review of #126 (round 4): `Agg` has no constructor, so its initializers run in
    # `run()`, and its lambda's `this.this.b` showed leftovers, stopped and as frame 1.
    path = directory / "unnamed.cpp"
    path.write_text(UNNAMED)
    rows = shown_at(capsys, path, line_of(UNNAMED, "// lambda"), "this", "this.this.b")
    assert withheld(rows["this"], "defined in a class's default member initializer")
    assert withheld(rows["this.this.b"], "'this' has no value here")
    rows = shown_at(capsys, path, line_of(UNNAMED, "return w;"), "this", hit=2, frame=1)
    assert withheld(rows["this"], "default member initializer")
    rows = shown_at(capsys, path, line_of(UNNAMED, "// end"), "agg.b", "after")
    assert [rows["agg.b"]["value"], rows["after"]["value"]] == ["6", "38"]


@pytest.mark.timeout(300)
def test_an_unnamed_structs_constructor_withholds_this(directory, capsys):
    # Review of #126 (round 4): frame 1 `nested(long)::$_0::$_0()` showed `this.t = 2`
    # (a leftover), and frame 2, the other constructor, showed `this` too.
    path = directory / "unnamed.cpp"
    path.write_text(UNNAMED)
    twice = line_of(UNNAMED, "return w;")
    for frame in (1, 2):
        rows = shown_at(capsys, path, twice, "this", hit=4, frame=frame)
        assert rows["this"]["function"].endswith("::$_0::$_0()"), rows["this"]
        assert withheld(rows["this"], "the object this constructor"), rows["this"]
