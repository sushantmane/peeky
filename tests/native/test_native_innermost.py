"""C and C++ values the stopped frame reads while they are being built or after a jump
past their initialization (#121): withheld, never shown as values."""

import json
import os

import pytest
from native_compilers import cxx_is_gcc

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


def never_stops_at(capsys, path, line):
    """GCC runs a default member initializer's code on the line that builds the object
    (#167): `describe` leaves the member's line out, and `--at` it never stops."""
    assert main(["describe", str(path), "--json"]) == 0
    lines = json.loads(capsys.readouterr().out)["breakpoint_lines"]
    assert line not in lines[path.name], lines
    command = ["probe", str(path), "--at", str(line), "--capture", "this"]
    code = main([*command, "--json", "--quiet", "--no-save"])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["error"]["code"] == "NO_STOP", result


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


@pytest.mark.cpp
@pytest.mark.timeout(300)
def test_this_is_withheld_while_its_object_is_being_built(directory, cxx, capsys):
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
    rows = shown_at(capsys, path, line_of(BUILDING, "// lambda in t's"), "this", "this.t", "t")
    if cxx == "gcc":
        # GCC passes a lambda its captures through `__closure` (#361): no `this`, and the
        # capture `t` is withheld as clang's `this` is.
        assert rows["this"]["error"] == "not found; visible: got, t", rows
        assert withheld(rows["t"], "this lambda is defined in the declaration of `t`")
        assert withheld(rows["t"], "what it captures may be under construction: it may not")
    else:
        assert withheld(rows["this"], "this lambda is defined in the declaration of `t`")
    # Built: a member function called later, and a lambda called after its declaration.
    rows = shown_at(capsys, path, line_of(BUILDING, "// called once built"), "this.a", "this.b")
    assert [rows["this.a"]["value"], rows["this.b"]["value"]] == ["6", "13"]
    rows = shown_at(
        capsys, path, line_of(BUILDING, "// lambda called later"), "this.t.p.y", "t.p.y"
    )
    if cxx == "gcc":
        # Before #361: `not found; visible: (none)`.
        assert rows["this.t.p.y"]["error"] == "not found; visible: t, w", rows
        assert rows["t.p.y"]["value"] == "8", rows
    else:
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
    assert withheld(rows["p"], "frame runs cases.h:3, outside its function's own lines")
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


@pytest.mark.cpp
@pytest.mark.timeout(300)
def test_an_object_a_default_member_initializer_builds_is_withheld(directory, cxx, capsys):
    # Review of #126: breaking on `Cfg`'s member line stops in `run()`, which showed
    # `cfg.b = 20907`; so did `run()` as frame 1 of `twice`.
    path = directory / "members.cpp"
    path.write_text(MEMBERS)
    member = line_of(MEMBERS, "// member b")
    if cxx == "gcc":
        never_stops_at(capsys, path, member)
    else:
        rows = shown_at(capsys, path, member, "cfg", "cfg.b", "seed")
        assert rows["cfg"]["function"] == "run()" and rows["seed"]["value"] == "6"
        assert withheld(rows["cfg"], f"frame runs members.cpp:{member}, outside its function's")
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


@pytest.mark.cpp
@pytest.mark.timeout(300)
def test_a_lambda_called_below_the_listed_frames_is_judged_from_more_of_them(
    directory, cxx, capsys
):
    # Review of #126: through `std::function`, `run` (where `cb` is defined) is more than
    # ten frames below the lambda, so its `this` was withheld; up to 64 are read now.
    path = directory / "deep.cpp"
    path.write_text(DEEP)
    line = line_of(DEEP, "// in the lambda")
    if cxx == "gcc":
        # GCC passes the lambda its captures through `__closure` (#361): its capture
        # `total` is judged as clang's `this` is.
        for hit in (1, 2):
            rows = shown_at(capsys, path, line, "total", hit=hit)
            assert "value" in rows["total"], rows
        rows = shown_at(capsys, path, line, "this", "total", hit=3)
        assert rows["this"]["error"] == "not found; visible: doubled, total, v", rows
        assert withheld(rows["total"], "Peeky cannot list every frame below this one")
        return
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


@pytest.mark.cpp
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


@pytest.mark.cpp
@pytest.mark.timeout(300)
def test_a_header_class_initializer_reads_from_the_line_that_runs_it(directory, cxx, capsys):
    # Review of #126 (round 2): stopped in `cfg.hpp`'s member initializer inside `run()`,
    # `after = 20911 [settable]` and a garbage `where` were shown; the stop now reads
    # as `main.cpp:16`, the line whose code runs it.
    (directory / "cfg.hpp").write_text(HEADER_CFG)
    path = directory / "main.cpp"
    path.write_text(HEADER_CFG_MAIN)
    built = line_of(HEADER_CFG_MAIN, "// cfg")
    rows = shown_at(capsys, path, built, "after", "where", "before", "cfg", steps=1)
    if cxx == "gcc":
        # GCC runs the initializers on `cfg`'s own line (#167): the step ends on the next.
        assert rows["before"]["line"] == built + 1 and rows["before"]["value"] == "4", rows
        assert withheld(rows["after"], "which has not run yet")
        assert withheld(rows["where"], "below this stop")
        assert rows["cfg"]["value"] == "{a = 2, b = 4}", rows["cfg"]
        return
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


@pytest.mark.cpp
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


@pytest.mark.cpp
@pytest.mark.timeout(300)
def test_a_caller_frame_on_a_member_initializer_above_its_function_reads_from_its_line(
    directory, cxx, capsys
):
    # Review of #126 (round 3): `run()` as frame 1 on `Cfg::b`'s line showed `x = 20908`
    # and a garbage `px`: the remapping to the line running it was the stopped frame's.
    path = directory / "localcls.cpp"
    path.write_text(LOCAL_CLASS)
    rows = shown_at(
        capsys, path, line_of(LOCAL_CLASS, "return w;"), "x", "px", "cfg", "before", hit=4, frame=1
    )
    # GCC runs the initializers on the line that builds `cfg` (#167).
    marker = "Cfg cfg{};" if cxx == "gcc" else "// Cfg::b"
    assert rows["x"]["line"] == line_of(LOCAL_CLASS, marker), rows["x"]
    assert withheld(rows["x"]) and withheld(rows["px"]) and withheld(rows["cfg"])
    assert rows["before"]["value"] == "1"
    rows = shown_at(capsys, path, line_of(LOCAL_CLASS, "// end"), "loc.b", "x", "px")
    assert [rows["loc.b"]["value"], rows["x"]["value"]] == ["8", "5"] and "value" in rows["px"]


@pytest.mark.cpp
@pytest.mark.timeout(300)
def test_a_local_classes_member_initializer_is_not_its_functions_own_line(directory, cxx, capsys):
    # Review of #126 (round 3): on the initializer of `Local`, defined inside `run()`
    # (within its own lines), `loc.b = 4366860288` was shown, stopped and as frame 1.
    path = directory / "localcls.cpp"
    path.write_text(LOCAL_CLASS)
    built = line_of(LOCAL_CLASS, "// loc")
    rows = shown_at(capsys, path, built, "loc", "after", "before", steps=1)
    if cxx == "gcc":
        # GCC runs the initializers on `loc`'s own line (#167): the step ends on the next.
        assert rows["loc"]["line"] == built + 1 and rows["loc"]["value"] == "{a = 6, b = 8}"
        assert withheld(rows["after"], "which has not run yet")
    else:
        assert rows["loc"]["line"] == line_of(LOCAL_CLASS, "// Local::b"), rows["loc"]
        assert withheld(rows["loc"], "outside its function's own lines")
        assert withheld(rows["after"], "below this stop")
    assert rows["before"]["value"] == "1"
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


@pytest.mark.cpp
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


@pytest.mark.cpp
@pytest.mark.timeout(300)
def test_an_unnamed_structs_constructor_withholds_this(directory, capsys):
    # Review of #126 (round 4): frame 1 `nested(long)::$_0::$_0()` showed `this.t = 2`
    # (a leftover), and frame 2, the other constructor, showed `this` too.
    path = directory / "unnamed.cpp"
    path.write_text(UNNAMED)
    twice = line_of(UNNAMED, "return w;")
    rows = shown_at(capsys, path, twice, "this", hit=4, frame=1)
    # GCC names it `<constructor>` (#167; its C++ builds run only with the test-only hatch).
    constructor = ("::$_0::$_0()", "<constructor>")
    assert rows["this"]["function"].endswith(constructor), rows["this"]
    assert withheld(rows["this"], "the object this constructor"), rows["this"]
    # Apple clang runs the complete constructor through the base one (two frames);
    # Linux clang and GCC alias them (one frame), so frame 2 is `nested` there.
    rows = shown_at(capsys, path, twice, "this", hit=4, frame=2)
    if rows["this"]["function"].endswith(constructor):
        assert withheld(rows["this"], "the object this constructor"), rows["this"]
    else:
        assert rows["this"]["function"] in ("nested(long)", "::nested(long)"), rows["this"]
        assert "value" not in rows["this"], rows["this"]


# Sixth agent evaluation: clang attributes the exception handler destroying line 22's
# `std::string` temporaries to line 22, after line 23's code, and that disabled the
# declared-below rule: `qty` showed leftovers (`6712214884 [settable]`, then the last
# pass's `3`), and `--set qty=99` reported a change line 23 then overwrote.
STOCK = """#include <cstdio>
#include <map>
#include <stdexcept>
#include <string>

static std::string trim_left(const std::string &s) {
    size_t i = 0;
    while (i < s.size() && s[i] == ' ') i++;
    return s.substr(i);
}

static long tried(const std::string &entry) {
    long total = 0;
    try {
        std::string head = entry.substr(0, 1) + "x";  // try
        long n = std::stol(entry.substr(1));
        total += n + (long)head.size();
    } catch (const std::exception &) {
        total = -1;
    }
    return total;
}

int main() {
    std::string input = "apple:3, pear:2";
    std::map<std::string, long> stock;
    size_t pos = 0;
    long extra = 0;
    while (pos <= input.size()) {
        size_t comma = input.find(',', pos);
        if (comma == std::string::npos) comma = input.size();
        std::string entry = input.substr(pos, comma - pos);
        size_t colon = entry.find(':');
        if (colon != std::string::npos) {
            std::string name = trim_left(entry.substr(0, colon));  // name
            long qty = std::stol(entry.substr(colon + 1));
            stock[name] += qty;
            extra += tried(entry.substr(colon));
        }
        pos = comma + 1;
    }
    std::printf("%ld %ld %ld\\n", stock["apple"], stock["pear"], extra);
    return 0;
}
"""

LABELLED = """#include <stdio.h>

__attribute__((noinline)) static long twice(long v) { return v * 2; }

static long labelled(int n) {
    long total = 0;
    int tries = 0;
again:
    total += twice(tries);  // label
    long q = twice(total);  // own line
    if (++tries < n) goto again;
    return total + q;
}

int main(void) {
    printf("%ld\\n", labelled(3));
    return 0;
}
"""


@pytest.mark.cpp
@pytest.mark.timeout(300)
def test_a_local_below_a_line_with_temporaries_is_withheld_and_refused(directory, capsys):
    path = directory / "stock.cpp"
    path.write_text(STOCK)
    line = line_of(STOCK, "// name")
    for hit in (1, 2):
        rows = shown_at(capsys, path, line, "entry", "colon", "name", "qty", hit=hit)
        assert withheld(rows["name"], f"declared on line {line}")
        assert withheld(rows["qty"], f"declared at line {line + 1}, below this stop"), rows
        # `entry` may be unreadable for another reason (a libstdc++ string's summary
        # where memory is not read), never withheld.
        assert not withheld(rows["entry"], "no value yet") and rows["colon"]["value"] == "5"
    # In a `try` block too.
    rows = shown_at(capsys, path, line_of(STOCK, "// try"), "head", "n", "total")
    assert withheld(rows["n"], "below this stop") and rows["total"]["value"] == "0", rows
    assert main(["session", "start", str(path), "--at", str(line)]) == 0
    capsys.readouterr()
    assert main(["session", "set", "qty=99", "--json"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "MUTATION_REJECTED" and "below this stop" in error["message"]
    assert main(["session", "close"]) == 0


@pytest.mark.timeout(300)
def test_a_local_below_the_stop_in_a_function_with_a_label_is_withheld(directory, capsys):
    # A `goto` can return above the declaration (C keeps `q` live then), but on the
    # first pass it holds leftovers: Peeky cannot tell the passes apart, so it withholds.
    path = directory / "labelled.c"
    path.write_text(LABELLED)
    for hit in (1, 2):
        rows = shown_at(capsys, path, line_of(LABELLED, "// label"), "q", "total", hit=hit)
        assert withheld(rows["q"], "a `goto` return above it", "may not be initialized"), rows
        assert "value" in rows["total"], rows
        # On its own line, where control enters it, likewise.
        rows = shown_at(capsys, path, line_of(LABELLED, "// own line"), "q", "total", hit=hit)
        assert withheld(rows["q"], "can return here with it live", "may not be"), rows
        assert "value" in rows["total"], rows


# Shapes like the evaluation's: a declaration below a line with temporaries, in a `try`
# block, a range-`for`, an `if`/`while` condition, after a label, a `cleanup` variable
# and a compound literal. Below the stop: withheld; above it: read.
SHAPES = {
    "temps.cpp": """#include <cstdio>
#include <stdexcept>
#include <string>
#include <vector>

static std::string make(int i) { return std::string("v") + std::to_string(i); }
static long next(long &n) { return n > 0 ? n-- : 0; }

static long temps(const std::vector<std::string> &items) {
    long total = 0;
    for (size_t i = 0; i < items.size(); i++) {
        std::string head = items[i].substr(0, 1) + make((int)i);  // TEMPS
        long q = (long)head.size() + std::stol(items[i].substr(1));
        total += q;
    }
    return total;
}

static long trying(const std::vector<std::string> &items) {
    long total = 0;
    for (const auto &item : items) {
        try {
            std::string t = item + make(1);  // TRY
            long q = std::stol(item.substr(1));
            total += q + (long)t.size();
        } catch (const std::exception &) {
            total -= 1;
        }
    }
    return total;
}

static long ranged(const std::vector<std::string> &items) {
    long total = 0;
    for (const auto &item : items) {  // RANGE
        std::string t = item + "x";  // RANGE_BODY
        long q = (long)t.size();
        total += q;
    }
    return total;
}

static long conditions(long n) {
    long total = 0;
    std::string label = make(2);  // COND_ABOVE
    if (long r = next(n)) {
        total += r;
    }
    while (long v = next(n)) {
        std::string s = make((int)v);  // WHILE_BODY
        long w = (long)s.size();
        total += v + w;
    }
    return total + (long)label.size();
}

int main() {
    std::vector<std::string> items{"a12", "b7", "c3"};
    std::printf("%ld %ld %ld %ld\\n", temps(items), trying(items), ranged(items), conditions(3));
    return 0;
}
""",
    "labels.c": """#include <stdio.h>
#include <stdlib.h>

static void freep(char **p) { free(*p); }
__attribute__((noinline)) static long twice(long v) { return v * 2; }

static long labelled(int n) {
    long total = 0;
    int tries = 0;
again:
    total += twice(tries);  // LABEL_STOP
    long q = twice(total);
    if (++tries < n) goto again;
    return total + q;
}

static long cleaned(int n) {
    long total = 0;
    for (int i = 0; i < n; i++) {
        char *buf __attribute__((cleanup(freep))) = malloc(16);  // CLEANUP
        long q = twice(i) + (buf != NULL);
        total += q;
    }
    return total;
}

static long literal(int n) {
    long total = 0;
    for (int i = 0; i < n; i++) {
        struct { long a, b; } pr = { twice(i), (struct { long z; }){ twice(1) }.z };  // LITERAL
        long q = pr.a + pr.b;
        total += q;
    }
    return total;
}

int main(void) {
    printf("%ld %ld %ld\\n", labelled(3), cleaned(3), literal(3));
    return 0;
}
""",
}


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "name, marker, hit, below, above",
    [
        ("temps.cpp", "TEMPS", 1, ("head", "q"), ("total", "i")),
        ("temps.cpp", "TEMPS", 2, ("head", "q"), ("total", "i")),
        ("temps.cpp", "TRY", 2, ("t", "q"), ("total", "item")),
        ("temps.cpp", "RANGE_BODY", 2, ("t", "q"), ("total", "item")),
        ("temps.cpp", "COND_ABOVE", 1, ("label",), ("total", "n")),
        ("temps.cpp", "WHILE_BODY", 2, ("s", "w"), ("total", "v", "label")),
        ("labels.c", "CLEANUP", 2, ("buf", "q"), ("total", "i")),
        ("labels.c", "LITERAL", 2, ("pr", "q"), ("total", "i")),
    ],
)
def test_locals_below_the_stop_are_withheld_whatever_the_line_runs_after(
    directory, capsys, name, marker, hit, below, above
):
    source = SHAPES[name]
    path = directory / name
    path.write_text(source)
    rows = shown_at(capsys, path, line_of(source, f"// {marker}"), *below, *above, hit=hit)
    if marker == "TEMPS" and cxx_is_gcc():
        # GCC tests a `for` loop at its bottom, which branches back to the body's first
        # instruction (#167): there `head` holds the last pass's string, or leftovers on
        # the first pass, and says so.
        note = "may be the last pass's value, or leftovers on the first pass: this line is "
        assert rows["head"]["note"].startswith(note + "reached again by the loop"), rows
        below = below[1:]
    assert all(withheld(rows[local], "no value yet") for local in below), rows
    # Read, or unreadable for another reason (a libstdc++ string where memory is not
    # read): never withheld.
    assert not any(withheld(rows[local], "no value yet") for local in above), rows
    assert all("value" in rows[local] for local in above if local not in ("item", "label"))


# Shown though Peeky cannot vouch for them: a note on the row, never a bare value.
LOOPED = """#include <stdio.h>

__attribute__((noinline)) static long twice(long v) { return v * 2; }

int main(void) {
    long total = 0;
    long base = 5;
    int x = 0;
    do {
        long y = twice(x);  // DO_BODY
        total += y + base;
        x++;
    } while (x < 3);
    printf("%ld\\n", total);
    return 0;
}
"""

JUMPED = """#include <setjmp.h>
#include <stdio.h>

static jmp_buf env;

__attribute__((noinline)) static long twice(long v) {
    long w = v * 2;  // HELPER
    return w;
}

static long run(int n) {
    long before = twice(3);
    volatile int tries = 0;
    long total = 0;  // TOP
    if (setjmp(env) != 0) {  // CALL
        total += 100;
    }
    long later = twice(tries);  // AFTER
    tries++;
    if (tries < n) longjmp(env, 1);
    return total + later;
}

int main(void) {
    printf("%ld\\n", run(2));
    return 0;
}
"""


def probed(capsys, path, line, capture, hits):
    code = main(
        ["probe", str(path), "--at", str(line), "--capture", capture, "--hits", str(hits)]
        + ["--json", "--quiet", "--no-save"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    return result, {(row["hit"], row["selector"]): row for row in result["rows"]}


@pytest.mark.timeout(300)
def test_a_local_declared_on_a_line_a_loop_returns_to_carries_a_note(directory, capsys):
    # A `do` body's first line: a loop returns to its first instruction, where `y`'s
    # declaration has not run on this pass (its leftovers on the first, the last pass's
    # value on later ones). Shown, with a note; `total` and `x` are live there.
    path = directory / "loop.c"
    path.write_text(LOOPED)
    line = line_of(LOOPED, "// DO_BODY")
    result, rows = probed(capsys, path, line, "y,x,total,base", 2)
    for hit in (1, 2):
        assert "value" in rows[hit, "y"] and rows[hit, "y"]["settable"] is False
        assert "may be the last pass's value" in rows[hit, "y"]["note"], rows[hit, "y"]
        assert "reached again by the loop" in rows[hit, "y"]["note"]
        assert all("note" not in rows[hit, name] for name in ("x", "total", "base")), rows
    assert any(w.startswith("y: May be the last pass's value") for w in result["warnings"])
    assert main(["session", "start", str(path), "--at", str(line), "--hit", "2"]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "inspect"]) == 0
        assert "    note: may be the last pass's value" in capsys.readouterr().out
        # Its declaration runs after the stop: a change would be overwritten.
        assert main(["session", "set", "y=5", "--json"]) == 2
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "MUTATION_REJECTED" and "runs after this stop" in error["message"]
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(300)
def test_locals_of_a_program_using_setjmp_carry_a_note(directory, capsys):
    # Below a setjmp call, a `longjmp` can bring control back past a declaration with
    # the local live, or this is the first pass: the local is shown with a note, and a
    # change is refused (its declaration may run after it). Review of PR #140: only in
    # the function that calls setjmp, below the call, and only for what is declared at
    # or below the stop; elsewhere the usual rules judge it (they withheld nothing in
    # a program using setjmp, and noted every local, `before` and `tries` included).
    path = directory / "jump.c"
    path.write_text(JUMPED)
    capture = "later,total,before,tries,n"
    result, rows = probed(capsys, path, line_of(JUMPED, "// AFTER"), capture, 2)
    for hit in (1, 2):
        note = rows[hit, "later"]["note"]
        assert "value" in rows[hit, "later"] and "calls setjmp (line" in note, rows
        assert rows[hit, "later"]["settable"] is False
        assert all("note" not in rows[hit, name] for name in ("total", "before", "tries", "n"))
    assert rows[1, "before"]["value"] == "6" and rows[2, "total"]["value"] == "100", rows
    assert any("setjmp" in warning for warning in result["warnings"]), result["warnings"]
    # Above the call, the usual rules: `total`, declared on the stop line, is withheld.
    result, rows = probed(capsys, path, line_of(JUMPED, "// TOP"), "total,later,before", 1)
    assert withheld(rows[1, "total"], "declared on line") and "note" not in rows[1, "before"]
    assert withheld(rows[1, "later"], "below this stop"), rows
    # On the call's own line too (review of PR #144, round 2): a `longjmp` returns past
    # the call, never to the line's first instruction, where this stop is.
    result, rows = probed(capsys, path, line_of(JUMPED, "// CALL"), "later,total", 1)
    assert withheld(rows[1, "later"], "below this stop") and "note" not in rows[1, "later"], rows
    assert rows[1, "total"]["value"] == "0", rows
    # A function that makes no setjmp call: judged as usual.
    result, rows = probed(capsys, path, line_of(JUMPED, "// HELPER"), "w,v", 1)
    assert withheld(rows[1, "w"], "declared on line") and "note" not in rows[1, "v"], rows
    assert main(["session", "start", str(path), "--at", str(line_of(JUMPED, "// AFTER"))]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "set", "later=5", "--json"]) == 2
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "MUTATION_REJECTED" and "longjmp" in error["message"], error
        # What is certainly current there can be changed.
        assert main(["session", "set", "before=7", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["after"] == "7"
    finally:
        main(["session", "close"])
        capsys.readouterr()


# #148: a local shown with a note (#140) passed it to nothing read through it: its
# fields and elements read as plain values. A `do` body's first line, and below a
# setjmp call; in C and C++.
NOTED_DO = """#include <stdio.h>

struct pt { long x, y; };
struct pair { struct pt inner; long z; };

__attribute__((noinline)) long use(struct pt p) { return p.x + p.y; }
__attribute__((noinline)) void scribble(void) {
    volatile long junk[64];
    for (int i = 0; i < 64; i++) junk[i] = 7;
}

int main(void) {
    long total = 0;
    int n = 0;
    scribble();
    do {
        struct pt q = { n, n + 1 }; long arr[3] = { n, n * 2, n * 3 }; struct pair p = { { n, n + 5 }, n }; struct pt *sp = &q;  // STOP
        total += use(q) + arr[1] + p.inner.y + sp->y;
        n++;
    } while (n < 3);
    printf("%ld\\n", total);
    return 0;
}
"""
NOTED_JUMP = """#include <setjmp.h>
#include <stdio.h>

struct pt { long x, y; };
struct pair { struct pt inner; long z; };
static jmp_buf env;

__attribute__((noinline)) long use(struct pt p) { return p.x + p.y; }

__attribute__((noinline)) static long run(int n) {
    volatile int tries = 0;
    long total = 0;
    if (setjmp(env) != 0) {
        total += 100;
    }
    long mark = tries;  // STOP
    struct pt q = { tries, tries + 1 };
    long arr[3] = { tries, tries * 2, tries * 3 };
    struct pair p = { { tries, tries + 5 }, tries };
    struct pt *sp = &q;
    total += use(q) + arr[1] + p.inner.y + sp->y + mark;
    tries++;
    if (tries < n) longjmp(env, 1);
    return total;
}

int main(void) {
    printf("%ld\\n", run(2));
    return 0;
}
"""
NOTED = {
    "do.c": NOTED_DO,
    "do.cpp": NOTED_DO.replace("<stdio.h>", "<cstdio>").replace("(void)", "()"),
    "jump.c": NOTED_JUMP,
    "jump.cpp": NOTED_JUMP.replace("<setjmp.h>", "<csetjmp>")
    .replace("<stdio.h>", "<cstdio>")
    .replace("(void)", "()"),
}


@pytest.mark.timeout(300)
@pytest.mark.parametrize("name", sorted(NOTED))
def test_what_is_read_through_a_noted_local_carries_its_note(directory, capsys, name):
    source = NOTED[name]
    path = directory / name
    path.write_text(source)
    line = line_of(source, "// STOP")
    through = ("q.x", "arr[1]", "p.inner.y", "sp[0]", "sp[0].y")
    result, rows = probed(capsys, path, line, ",".join(("q", "arr", "p", "sp", *through)), 2)
    for hit in (1, 2):
        for selector in through:
            row, root = rows[hit, selector], rows[hit, selector.split(".")[0].split("[")[0]]
            # At hit 1, `sp` holds a leftover address, which may point nowhere readable.
            if hit == 2 or not selector.startswith("sp"):
                assert "value" in row and root["note"], (hit, row, root)
            if "value" in row:
                assert row["note"] == f"read through `{root['selector']}`: {root['note']}", row
            assert row.get("settable") is not True, row
    # One warning for a local and what is read through it.
    named = [set(w.split(": ")[0].split(", ")) for w in result["warnings"]]
    for group in ({"q", "q.x"}, {"p", "p.inner.y"}, {"sp", "sp[0]", "sp[0].y"}):
        assert any(group <= names for names in named), result["warnings"]
    assert main(["session", "start", str(path), "--at", str(line)]) == 0
    capsys.readouterr()
    try:
        main(["session", "get", "q", "p.inner", "--json"])
        q, inner = json.loads(capsys.readouterr().out)
        for row in (q, inner):
            assert row["children"] and all(
                child["note"].startswith(f"read through `{row['selector'][0]}`: ")
                and child.get("settable") is False
                for child in row["children"]
            ), row
        main(["session", "get", "arr"])
        text = capsys.readouterr().out
        assert text.count("  [note]") == 3 and "\n  note: " in text, text
        for change in ("q.x=5", "arr[1]=5", "p.inner.y=5"):
            assert main(["session", "set", change, "--json"]) == 2
            error = json.loads(capsys.readouterr().out)["error"]
            assert error["code"] == "MUTATION_REJECTED", error
    finally:
        main(["session", "close"])
        capsys.readouterr()


NOTED_MAP = """#include <cstdio>
#include <map>

struct pt { long x, y; };

int main() {
    long total = 0;
    int n = 0;
    do {
        std::map<long, pt> m = {{n, {n, n + 1}}};  // STOP
        total += m[n].y;
        n++;
    } while (n < 3);
    std::printf("%ld\\n", total);
    return 0;
}
"""


@pytest.mark.cpp
@pytest.mark.timeout(300)
def test_a_noted_maps_entries_carry_its_note_beside_how_to_read_them(directory, capsys):
    # #148: `session get m` replaced the map's own note (#140) with how to read its
    # entries, and its entries carried none.
    path = directory / "map.cpp"
    path.write_text(NOTED_MAP)
    line = line_of(NOTED_MAP, "// STOP")
    assert main(["session", "start", str(path), "--at", str(line), "--hit", "2"]) == 0
    capsys.readouterr()
    try:
        main(["session", "get", "m", "m[0].y", "--json"])
        m, y = json.loads(capsys.readouterr().out)
        assert m["note"].startswith("may be the last pass's value") and "is a C++ map" in m["note"]
        [entry] = m["children"]
        assert entry["selector"] == "m[0]" and entry["note"].startswith("read through `m`: ")
        assert "value" in y and y["note"].startswith("read through `m`: may be"), y
    finally:
        main(["session", "close"])
        capsys.readouterr()


# Review of PR #140: another declaration of the name (an earlier loop's, a static, a
# lambda's parameter) kept the local declared below the stop shown with leftovers, and
# settable: only the declaration the debugger shows is judged.
NAMESAKES = {
    "f1.c": """#include <stdio.h>

__attribute__((noinline)) void scribble(void) {
    volatile long junk[64];
    for (int i = 0; i < 64; i++) junk[i] = 0x5150 + i;
}
__attribute__((noinline)) long use(long v) { return v + 1; }

int main(void) {
    long total = 0;
    for (int k = 0; k < 2; k++) {
        long qty = k * 10;
        total += use(qty);
    }
    scribble();
    total += use(total);
    long qty = 7;
    total += qty;
    printf("T %ld\\n", total);
    return 0;
}
""",
    "f2.cpp": """#include <cstdio>
#include <vector>
#include <algorithm>

__attribute__((noinline)) void scribble() {
    volatile long junk[64];
    for (int i = 0; i < 64; i++) junk[i] = 0x5150 + i;
}

int main() {
    std::vector<long> v{3, 1, 2};
    scribble();
    std::sort(v.begin(), v.end(), [](long a, long b) { return a < b; });
    long b = v[0] + 5;
    auto twice = [&](long x) { return x * 2 + b; };
    long r = twice(b);
    std::printf("R %ld\\n", r);
    return 0;
}
""",
    "f3.c": """#include <stdio.h>

__attribute__((noinline)) void scribble(void) {
    volatile long junk[64];
    for (int i = 0; i < 64; i++) junk[i] = 0x5150 + i;
}
__attribute__((noinline)) long use(long v) { return v + 1; }

int main(void) {
    long total = 0;
    if (total == 0) {
        static long qty = 3;
        total += qty;
    }
    for (int k = 0; k < 2; k++) {
        long n = k;
        total += use(n);
    }
    scribble();
    total += use(total);
    long qty = use(total);
    long n = qty * 2;
    printf("T %ld %ld\\n", total, n);
    return 0;
}
""",
}


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "name, line, withheld_here, shown_here",
    [
        ("f1.c", 16, {"qty": "declared at line 17, below this stop"}, ("total",)),
        ("f2.cpp", 13, {"b": "declared at line 14, below this stop"}, ("v",)),
        (
            "f3.c",
            20,
            {"qty": "declared at line 21, below this stop", "n": "declared at line 22"},
            ("total",),
        ),
        ("f3.c", 21, {"qty": "declared on line 21, which has not run yet"}, ("total",)),
    ],
)
def test_a_local_below_the_stop_is_judged_by_the_declaration_shown(
    directory, capsys, name, line, withheld_here, shown_here
):
    path = directory / name
    path.write_text(NAMESAKES[name])
    rows = shown_at(capsys, path, line, *withheld_here, *shown_here)
    for local, phrase in withheld_here.items():
        assert withheld(rows[local], phrase), rows[local]
    assert all("value" in rows[local] for local in shown_here), rows
    assert main(["session", "start", str(path), "--at", str(line)]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "set", f"{next(iter(withheld_here))}=5", "--json"]) == 2
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "MUTATION_REJECTED"
    finally:
        main(["session", "close"])
        capsys.readouterr()


# Review of PR #140: in a block declaring a `static` or `extern` (or C++ `using`) of an
# outer local's name, the code reads that one, which lldb-dap does not list (clang
# declares a block's static in the function's scope, a block's extern not at all): the
# outer local was shown, and read and changed, under its name.
SHADOWS = {
    "shadow.c": """#include <stdio.h>

long gcount = 1;
#define KEEP(name) static long name = 5
__attribute__((noinline)) long use(long v) { return v + 1; }

int main(void) {
    long gcount = 6;
    long total = 0;
    {
        extern long gcount;
        total += use(gcount);  // EXTERN
    }
    long q = 1;
    {
        static long q = 2;
        total += use(q);  // STATIC
    }
    long r = 3;
    {
        KEEP(r);
        total += use(r);  // MACRO
    }
    total += use(q) + use(gcount) + use(r);  // AFTER
    printf("T %ld %ld %ld %ld\\n", total, gcount, q, r);
    return 0;
}
""",
    "shadow.cpp": """#include <cstdio>

namespace cfg { long level = 3; }
__attribute__((noinline)) long use(long v) { return v + 1; }

int main() {
    long level = 1;
    long total = 0;
    {
        using cfg::level;
        total += use(level);  // USING
    }
    {
        static long hits = 0;
        long kept = hits;
        total += use(kept);  // OTHER
    }
    std::printf("T %ld %ld\\n", total, level);
    return 0;
}
""",
}


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "name, marker, local, phrase",
    [
        ("shadow.c", "EXTERN", "gcount", "names the block's `extern` `gcount`"),
        ("shadow.c", "STATIC", "q", "names the block's `static` `q`"),
        # A macro's static: read as the compiler preprocessed the source (PR #144, round 2).
        ("shadow.c", "MACRO", "r", "names the block's `static` `r`"),
        ("shadow.cpp", "USING", "level", "the block's `using` declaration (line"),
    ],
)
def test_an_outer_local_a_blocks_static_or_extern_hides_is_withheld(
    directory, capsys, name, marker, local, phrase
):
    source = SHADOWS[name]
    path = directory / name
    path.write_text(source)
    line = line_of(source, f"// {marker}")
    rows = shown_at(capsys, path, line, local, "total")
    assert withheld(rows[local], phrase) and "what the code reads" in rows[local]["error"]
    assert "value" in rows["total"], rows
    assert main(["session", "start", str(path), "--at", str(line)]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "set", f"{local}=9", "--json"]) == 2
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "MUTATION_REJECTED" and "cannot be changed here" in error["message"]
    finally:
        main(["session", "close"])
        capsys.readouterr()


STATICS = """#include <stdio.h>
int main(void) {
    static int q = 5;
    int a = 1;
    printf("%d\\n", q + a); // HERE
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_a_static_local_says_why_the_debugger_does_not_list_it(directory, capsys):
    # #167 (B4): lldb-dap lists no frame's static storage, so a function's `static` read
    # "not found; visible: a", as if main had no `q`.
    path = directory / "statics.c"
    path.write_text(STATICS)
    rows = shown_at(capsys, path, line_of(STATICS, "// HERE"), "q", "a")
    assert rows["a"]["value"] == "1", rows
    error = rows["q"]["error"]
    assert error.startswith("not found: main declares 'q' in static storage"), error
    assert error.endswith("Peeky cannot read it here; print it from the program; visible: a")


@pytest.mark.cpp
@pytest.mark.timeout(300)
def test_outer_locals_are_read_outside_the_blocks_that_hide_them(directory, capsys):
    path = directory / "shadow.c"
    path.write_text(SHADOWS["shadow.c"])
    rows = shown_at(capsys, path, line_of(SHADOWS["shadow.c"], "// AFTER"), "q", "gcount", "r")
    assert [rows[local]["value"] for local in ("q", "gcount", "r")] == ["1", "6", "3"], rows
    path = directory / "shadow.cpp"
    path.write_text(SHADOWS["shadow.cpp"])
    rows = shown_at(capsys, path, line_of(SHADOWS["shadow.cpp"], "// OTHER"), "level", "kept")
    assert rows["level"]["value"] == "1" and rows["kept"]["value"] == "0", rows


# Review of PR #144: more shapes that hide an outer local's name, and some that do not.
HIDING = {
    "hide.c": """#include <stdio.h>

long gcount = 1;
#define ONCE static int once_flag = 0
__attribute__((noinline)) long use(long v) { return v + 1; }
__attribute__((noinline)) long helper(long q) { return q * 3; }

__attribute__((noinline)) long same_line(void) {
    long gcount = 6;
    long total = 0;
    { extern long gcount; total += use(gcount); }  // SAME_LINE
    return total;
}
__attribute__((noinline)) long multi(void) {
    long m = 1;
    long total = 0;
    {
        static long a[2] = {1, 2}, m = 3;
        total += use(m) + a[0];  // MULTI
    }
    return total;
}
__attribute__((noinline)) long anon(void) {
    long q = 1;
    long total = 0;
    {
        static struct { long v; } q = { 5 };
        total += use(q.v);  // ANON
    }
    return total;
}
__attribute__((noinline)) long enumer(void) {
    long q = 1;
    long total = 0;
    {
        enum { q = 7 };
        total += use(q);  // ENUM
    }
    return total;
}
__attribute__((noinline)) long fdecl(void) {
    long use2 = 4;
    long total = 0;
    {
        long use2(long);
        total += (use2 != 0);  // FDECL
    }
    return total;
}
__attribute__((noinline)) long ifzero(void) {
    long q = 1;
    long total = 0;
    {
#if 0
        static long q = 2;
#endif
        total += use(q);  // IFZERO
    }
    return total;
}
__attribute__((noinline)) long ifdef(void) {
    long q = 1;
    long total = 0;
    {
#ifdef NOT_DEFINED
        static long q = 2;
#endif
        total += use(q);  // IFDEF
    }
    return total;
}
__attribute__((noinline)) long protoparam(void) {
    long q = 4;
    long total = 0;
    {
        extern long helper(long q);
        total += helper(q);  // PROTO
    }
    return total;
}
__attribute__((noinline)) long once(void) {
    long q = 2;
    long total = 0;
    {
        ONCE;
        total += use(q) + once_flag;  // ONCE
    }
    return total;
}

int main(void) {
    printf("%ld %ld %ld %ld %ld %ld %ld %ld %ld\\n", same_line(), multi(), anon(), enumer(),
           fdecl(), ifzero(), ifdef(), protoparam(), once());
    return 0;
}
long use2(long v) { return v; }
""",
    "hide.cpp": """#include <cstdio>

__attribute__((noinline)) long use(long v) { return v + 1; }

__attribute__((noinline)) long tls() {
    long q = 6;
    long total = 0;
    {
        static thread_local long q = 8;
        total += use(q);  // TLS
    }
    return total;
}

int main() {
    std::printf("%ld\\n", tls());
    return 0;
}
""",
    # Review of PR #144 (round 2): what a macro or a conditional makes of a block, and an
    # enum nested in a struct's body (C gives its enumerators the block's scope).
    "made.c": """#include <stdio.h>

#define MAKE_ENUM enum { q = 7 }
#define DECL_ENUM(n) enum { n = 8 }
#define PROTO(n) long n(long)
#define HAVE_STATIC 1
__attribute__((noinline)) long use(long v) { return v + 1; }

__attribute__((noinline)) long nested(void) {
    long q = 1;
    long total = 0;
    {
        struct S { enum { q = 3 } kind; long v; } s = { 0, 2 };
        total += use(q) + s.v;  // NESTED
    }
    return total;
}
__attribute__((noinline)) long made(void) {
    long q = 1;
    long total = 0;
    {
        MAKE_ENUM;
        total += use(q);  // MADE
    }
    return total;
}
__attribute__((noinline)) long given(void) {
    long q = 1;
    long total = 0;
    {
        DECL_ENUM(q);
        total += use(q);  // GIVEN
    }
    return total;
}
__attribute__((noinline)) long proto(void) {
    long use3 = 1;
    long total = 0;
    {
        PROTO(use3);
        total += (use3 != 0);  // PROTO
    }
    return total;
}
__attribute__((noinline)) long defined(void) {
    long q = 1;
    long total = 0;
    {
#ifdef HAVE_STATIC
        static long q = 2;
#endif
        total += use(q);  // DEFINED
    }
    return total;
}
__attribute__((noinline)) long tag(void) {
    long q = 1;
    long total = 0;
    {
        struct q { long v; } t = { 4 };
        total += use(q) + t.v;  // TAG
    }
    return total;
}

int main(void) {
    printf("%ld %ld %ld %ld %ld %ld\\n", nested(), made(), given(), proto(), defined(), tag());
    return 0;
}
long use3(long v) { return v; }
""",
    # A C++ class a block declares hides the outer local; a class's own enumerator does
    # not.
    "made.cpp": """#include <cstdio>

__attribute__((noinline)) long use(long v) { return v + 1; }

__attribute__((noinline)) long classes() {
    long q = 1;
    long total = 0;
    {
        struct q { long v = 5; };
        total += sizeof(q) + q{}.v;  // CLASS
    }
    {
        struct S { enum { q = 4 }; long v = 2; } s;
        total += use(q) + s.v + S::q;  // MEMBER
    }
    return total;
}

int main() {
    std::printf("%ld\\n", classes());
    return 0;
}
""",
    "multi.c": """#include <stdio.h>

__attribute__((noinline)) long use(long v) { return v + 1; }

__attribute__((noinline)) long multi(void) {
    long q = 1;
    long total = 0;
    {
        static long a[2] = {1, 2}, q = 3;
        total += use(q) + a[0];  // MULTI
    }
    return total;
}

int main(void) {
    printf("%ld\\n", multi());
    return 0;
}
""",
}


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "name, marker, local, phrase",
    [
        # A block opening on the stop line itself.
        ("hide.c", "SAME_LINE", "gcount", "names the block's `extern` `gcount`"),
        # A declarator after an initializer's braces (no other such `q` in the file).
        ("multi.c", "MULTI", "q", "names the block's `static` `q`"),
        # After a type's body; an enumerator; a function a block declares.
        ("hide.c", "ANON", "q", "names the block's `static` `q`"),
        ("hide.c", "ENUM", "q", "names the block's enumerator `q`"),
        ("hide.c", "FDECL", "use2", "names the function `use2` the block declares"),
        # Read as the compiler preprocessed it: an enum in a struct's body (C), a
        # macro's, a function a macro declares, a static a defined `#ifdef` keeps; a
        # C++ class.
        ("made.c", "NESTED", "q", "names the block's enumerator `q` (line 13)"),
        ("made.c", "MADE", "q", "names the block's enumerator `q` (line 22)"),
        ("made.c", "GIVEN", "q", "names the block's enumerator `q` (line 31)"),
        ("made.c", "PROTO", "use3", "names the function `use3` the block declares (line 40)"),
        ("made.c", "DEFINED", "q", "names the block's `static` `q` (line 50)"),
        ("made.cpp", "CLASS", "q", "names the type `q` the block declares (line 9)"),
        # A thread's own static is listed, apart: the row says where.
        ("hide.cpp", "TLS", "q @ hide.cpp:6", "listed separately as `q @ hide.cpp:9`"),
    ],
)
def test_a_name_a_block_declares_otherwise_hides_the_outer_local(
    directory, capsys, name, marker, local, phrase
):
    source = HIDING[name]
    path = directory / name
    path.write_text(source)
    line = line_of(source, f"// {marker}")
    rows = shown_at(capsys, path, line, local)
    assert withheld(rows[local], phrase), rows[local]
    assert main(["session", "start", str(path), "--at", str(line)]) == 0
    capsys.readouterr()
    try:
        assert main(["session", "set", f"{local}=9", "--json"]) == 2
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "MUTATION_REJECTED"
    finally:
        main(["session", "close"])
        capsys.readouterr()


HEADER_HIDING = """#define MAKE_ENUM enum { q = 7 }
__attribute__((noinline)) static long inhead(void) {
    long q = 1;
    long total = 0;
    {
        MAKE_ENUM;
        total += q;  // INHEAD
    }
    return total + q;  // AFTER
}
"""
HEADER_HIDING_MAIN = """#include <stdio.h>
#include "hiding.h"

int main(void) {
    long q = 2;
    printf("%ld %ld\\n", inhead(), q);  // CALL
    return 0;
}
"""


@pytest.mark.timeout(300)
def test_a_header_functions_block_is_read_from_the_unit_including_it(directory, capsys):
    # Review of PR #144 (round 2): a function in a header is read in the translation
    # unit that includes it, as the compiler preprocessed it there.
    (directory / "hiding.h").write_text(HEADER_HIDING)
    path = directory / "main.c"
    path.write_text(HEADER_HIDING_MAIN)
    line = line_of(HEADER_HIDING_MAIN, "// CALL")
    assert main(["session", "start", str(path), "--at", str(line)]) == 0
    capsys.readouterr()
    try:
        for _ in range(3):
            assert main(["session", "step", "--into"]) == 0
        assert f"hiding.h:{line_of(HEADER_HIDING, '// INHEAD')} " in capsys.readouterr().out
        main(["session", "get", "q", "total", "--json"])
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
        assert withheld(rows["q"], "names the block's enumerator `q` (line 6)"), rows
        assert rows["total"]["value"] == "0", rows
        assert main(["session", "set", "q=9", "--json"]) == 2
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "MUTATION_REJECTED"
        assert main(["session", "step"]) == 0
        capsys.readouterr()
        main(["session", "get", "q", "--json"])
        assert json.loads(capsys.readouterr().out)[0]["value"] == "1"
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "name, marker, local, value",
    [
        ("hide.c", "IFZERO", "q", "1"),  # an `#if 0` leaves the static out
        ("hide.c", "IFDEF", "q", "1"),  # so does an `#ifdef` of what is not defined
        ("hide.c", "PROTO", "q", "4"),  # a prototype's parameter declares nothing here
        ("hide.c", "ONCE", "q", "2"),  # a macro declaring a static that does not name `q`
        ("made.c", "TAG", "q", "1"),  # a C struct's tag is no variable's name
        ("made.cpp", "MEMBER", "q", "1"),  # a class's enumerator is the class's
    ],
)
def test_a_name_a_block_does_not_declare_leaves_the_outer_local_read(
    directory, capsys, name, marker, local, value
):
    source = HIDING[name]
    path = directory / name
    path.write_text(source)
    rows = shown_at(capsys, path, line_of(source, f"// {marker}"), local)
    assert rows[local].get("value") == value, rows[local]
