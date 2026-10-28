"""C and C++ locals declared below the stop are never shown as values (#74)."""

import json
import os
from types import SimpleNamespace

import pytest
from native_compilers import cxx_for, cxx_is_gcc, gcc, is_gcc

from peeky.cli import main

BUCKET = """#include <stdio.h>

struct pt { long x, y; };

static long take(long now_ms, long *last) {
    long elapsed = now_ms - *last;
    *last = now_ms;
    long refill = elapsed / 1000;
    struct pt p = {refill, elapsed};
    static int calls = 0;
    calls++;
    return p.x + p.y + calls;
}

int main(void) {
    long last = 0, total = 0;
    int x = 1;
    for (int i = 1; i <= 3; i++) {
        total += take(i * 600, &last) + x;
        int x = 2;
        total += x;
    }
    printf("TOTAL:%ld\\n", total);
    return 0;
}
"""


@pytest.fixture
def bucket(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "bucket"
    root.mkdir()
    (root / "bucket.c").write_text(BUCKET)
    return root / "bucket.c"


def probe(capsys, *argv):
    code = main(["probe", *map(str, argv), "--json", "--quiet", "--no-save"])
    return code, json.loads(capsys.readouterr().out)


def never_stops_at(capsys, program, line):
    """GCC runs a default member initializer's code on another line, the constructor's or
    the one building the object (#167): `describe` leaves the member's line out, and
    `--at` it never stops."""
    assert main(["describe", str(program), "--json"]) == 0
    lines = json.loads(capsys.readouterr().out)["breakpoint_lines"]
    assert line not in lines[program.name], lines
    code, result = probe(capsys, program, "--at", str(line), "--capture", "x")
    assert code == 1 and result["error"]["code"] == "NO_STOP", result


@pytest.mark.timeout(120)
def test_locals_declared_below_the_stop_are_withheld_until_their_line(bucket, capsys):
    code, result = probe(
        capsys, bucket, "--at", "7", "--capture", "elapsed,refill,p,p.x", "--hits", "2"
    )
    assert code == 0, result.get("error")
    rows = {(row["hit"], row["selector"]): row for row in result["rows"]}
    for hit in (1, 2):
        assert rows[hit, "elapsed"]["value"] == "600"
        # Hit 2 holds hit 1's leftovers in these slots: never shown as values.
        for name, line in (("refill", 8), ("p", 9), ("p.x", 9)):
            row = rows[hit, name]
            assert "value" not in row and f"declared at line {line}, below" in row["error"]
    code, after = probe(capsys, bucket, "--at", "10", "--capture", "refill,p.x")
    assert [row["value"] for row in after["rows"]] == ["0", "0"]


@pytest.mark.timeout(120)
def test_a_shadowed_local_declared_below_is_withheld_and_the_outer_one_shown(bucket, capsys):
    code, result = probe(capsys, bucket, "--at", "19", "--capture", "x", "--hits", "2")
    assert code == 0, result.get("error")
    main(["session", "start", str(bucket), "--at", "19", "--hit", "2"])
    capsys.readouterr()
    try:
        assert main(["session", "inspect", "--json"]) == 0
        snapshot = json.loads(capsys.readouterr().out)
    finally:
        main(["session", "close"])
        capsys.readouterr()
    shown = {v["name"]: v for s in snapshot["scopes"] for v in s["variables"]}
    inner = [v for name, v in shown.items() if name.startswith("x") and name.endswith(":20")]
    outer = [v for name, v in shown.items() if name.startswith("x") and name.endswith(":17")]
    # lldb-dap names both `x @ bucket.c:N` while the inner one is in scope.
    assert inner and inner[0]["unavailable"] and inner[0]["settable"] is False
    assert outer and outer[0]["value"] == "1"


@pytest.mark.timeout(120)
def test_a_local_declared_below_cannot_be_set(bucket, capsys):
    code, result = probe(capsys, bucket, "--at", "7", "--capture", "elapsed", "--set", "refill=5")
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED"
    assert "declared at line 8" in result["error"]["message"]
    # #77: the refusal lists what can be changed at that stop.
    assert result["error"]["message"].endswith("settable here: now_ms, elapsed")
    code, result = probe(capsys, bucket, "--at", "7", "--capture", "elapsed,refill,last")
    settable = {row["selector"]: row.get("settable") for row in result["rows"]}
    assert settable == {"elapsed": True, "refill": False, "last": False}
    code, result = probe(capsys, bucket, "--at", "7", "--capture", "elapsed", "--set", "elapsed=5")
    assert code == 0 and result["interventions"][0]["after"] == "5"


TWICE = """#include <stdio.h>

static long twice(long v) {
    long doubled = v * 2;
    return doubled;
}

int main(void) {
    long seed = 21;
    long result = twice(seed);
    printf("%ld\\n", result);
    return 0;
}
"""


@pytest.mark.timeout(120)
def test_a_callers_local_declared_on_its_calling_line_is_withheld(bucket, capsys):
    # #104: in `long result = twice(seed);`, main stores `result` only after twice
    # returns, so `--frame 1` showed its leftovers (6712212928) as its value.
    path = bucket.parent / "twice.c"
    path.write_text(TWICE)
    bucket.unlink()
    assert main(["session", "start", str(path), "--at", "4"]) == 0
    capsys.readouterr()
    try:
        main(["session", "get", "--frame", "1", "result", "seed", "--json"])
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
        assert rows["seed"]["value"] == "21" and rows["seed"]["frame"] == 1
        result = rows["result"]
        assert "value" not in result and result["unavailable"], result
        assert (
            "declared on line 10, whose code this caller frame has not finished"
            in (result["error"])
        )
        main(["session", "get", "v", "--json"])
        assert json.loads(capsys.readouterr().out)[0]["value"] == "21"
    finally:
        main(["session", "close"])


FRAMES = """#include <stdio.h>

struct pair { long x, y; };

__attribute__((noinline)) static long twice(long v) {
    long w = v * 2;
    return w;
}

__attribute__((noinline)) static void scribble(void) {
    volatile long junk[64];
    for (int i = 0; i < 64; i++) junk[i] = 0x5150 + i;
}

__attribute__((noinline)) static long run(void) {
    long seed = 21;
    long result = twice(seed);
    long multi =
        twice(seed + 1);
    struct pair p = {
        .x = twice(1),
        .y = twice(2),
    };
    printf("%ld %ld %ld %ld\\n", result, multi, p.x, p.y);
    return result;
}

int main(void) {
    scribble();
    return (int)(run() & 1);
}
"""


@pytest.mark.timeout(120)
def test_a_callers_declaration_over_several_lines_is_withheld_until_it_is_stored(bucket, capsys):
    # Review of #104: `long multi =` / `twice(seed + 1);` stores `multi` on line 18 after
    # the call returns, so `--frame 1` showed leftovers (-1575490704436559704), and so did
    # a designated initializer's fields (20878, 20879). Caller values are never settable.
    path = bucket.parent / "frames.c"
    path.write_text(FRAMES)
    bucket.unlink()
    shown = {}
    for hit in (2, 3):
        assert main(["session", "start", str(path), "--at", "7", "--hit", str(hit)]) == 0
        capsys.readouterr()
        try:
            assert main(["session", "inspect", "--frame", "1", "--json"]) == 0
            snapshot = json.loads(capsys.readouterr().out)
        finally:
            main(["session", "close"])
            capsys.readouterr()
        shown[hit] = {v["name"]: v for s in snapshot["scopes"] for v in s["variables"]}
    for hit, name, line in ((2, "multi", 18), (3, "p", 20)):
        variable = shown[hit][name]
        assert variable["unavailable"] and variable.get("value") is None, variable
        assert (
            f"declared on line {line}, whose code this caller frame has not finished"
            in (variable["reason"])
        )
    assert shown[2]["result"]["value"] == "42" and shown[3]["multi"]["value"] == "44"
    assert not any(v.get("settable") for hit in shown for v in shown[hit].values())


AGGREGATES = """#include <stdio.h>

struct pair { long x, y; };
struct outer { struct pair in; };

__attribute__((noinline)) static long twice(long v) {
    long w = v * 2;
    return w;
}

__attribute__((noinline)) static void scribble(void) {
    volatile long junk[96];
    for (int i = 0; i < 96; i++) junk[i] = 0x5150 + i;
}

__attribute__((noinline)) static long run(void) {
    struct outer o = {
        .in = {
            .x = twice(1),
            .y = twice(2),
        },
    };
    struct pair ps[2] = {
        { twice(3), twice(4) },
        { twice(5), twice(6) },
    };
    long grid[2][2] = {
        { twice(7), twice(8) },
        { twice(9), twice(10) },
    };
    long total = 0;
    for (int k = 0; k < 2; k++) {
        total += twice(k);
    }
    printf("%ld %ld %ld %ld %ld\\n", o.in.y, ps[1].y, grid[1][1], grid[0][0], total);
    return o.in.x;
}

int main(void) {
    scribble();
    return (int)(run() & 1);
}
"""


@pytest.mark.timeout(180)
def test_a_callers_aggregate_stored_element_by_element_is_withheld_until_stored(bucket, capsys):
    # Review of #104 (round 2): clang puts no code on the declaration line of an array of
    # structs, a 2-D array or a nested struct, so `--frame 1` showed `o.in.y = 20905`,
    # `ps[1].x = -1530060163171811189` and `grid[1][0] = 20908` while being stored.
    path = bucket.parent / "frames3.c"
    path.write_text(AGGREGATES)
    bucket.unlink()
    shown = {}
    for hit in (2, 5, 9, 11):
        assert main(["session", "start", str(path), "--at", "8", "--hit", str(hit)]) == 0
        capsys.readouterr()
        try:
            assert main(["session", "inspect", "--frame", "1", "--json"]) == 0
            snapshot = json.loads(capsys.readouterr().out)
        finally:
            main(["session", "close"])
            capsys.readouterr()
        shown[hit] = {v["name"]: v for s in snapshot["scopes"] for v in s["variables"]}
    # Each is read once all of its lines' code comes before the call: `o` from hit 5 on,
    # `ps` from hit 9 and `grid` at hit 11 (withheld at every hit on main).
    stored = {2: (), 5: ("o",), 9: ("o", "ps"), 11: ("o", "ps", "grid")}
    values = {
        "o": "{in = {x = 2, y = 4}}",
        "ps": "[{x = 6, y = 8}, {x = 10, y = 12}]",
        "grid": "[[14, 16], [18, 20]]",
    }
    for hit in (2, 5, 9, 11):
        for name, line in (("o", 17), ("ps", 23), ("grid", 27)):
            variable = shown[hit][name]
            if name in stored[hit]:
                assert not variable.get("unavailable"), (hit, variable)
                assert variable["value"] == values[name], (hit, variable)
                continue
            assert variable["unavailable"] and variable.get("value") is None, (hit, variable)
            assert (
                f"declared on line {line}, which has no code of its own" in (variable["reason"])
            ), (hit, variable)
    # A loop's counter while its body calls: the increment follows the body.
    assert shown[11]["total"]["value"] == "0" and shown[11]["k"]["unavailable"]


TRIO = """#include <stdio.h>

struct pair { long x, y; };
struct trio { long x; struct pair p; };

__attribute__((noinline)) static long twice(long v) {
    long w = v * 2;
    return w;
}

__attribute__((noinline)) static void scribble(void) {
    volatile long junk[96];
    for (int i = 0; i < 96; i++) junk[i] = 0x5150 + i;
}

__attribute__((noinline)) static long run(long seed) {
    struct pair q = { .x = seed,
        .y = twice(2) };
    long arr[2] = { seed + 1,
        twice(9) };
    struct trio t = { seed,
        { twice(3),
          twice(4) } };
    long *ptrs[2] = { &q.x,
        (long *)twice(0) };
    printf("%ld %ld %ld %p\\n", q.y, arr[1], t.p.y, (void *)ptrs[1]);
    return q.x;
}

int main(void) {
    scribble();
    return (int)(run(5) & 1);
}
"""


@pytest.mark.timeout(240)
@pytest.mark.parametrize("suffix", ["c", "cpp"])
def test_a_callers_declaration_still_running_on_its_line_is_withheld(bucket, capsys, suffix):
    # Review of #104 (round 3): line 21 has code (`t.x = seed`), all of it before the
    # call, and `t.p` is stored after the calls on lines 22-23 return, so `--frame 1`
    # showed `t.p.x = 20905` and `t.p.y = 20906` at hit 3, and `t.p.y = 20906` at hit 4.
    directory = bucket.parent.with_name(f"trio-{suffix}")
    directory.mkdir()
    path = directory / f"frames5.{suffix}"
    path.write_text(TRIO.replace("<stdio.h>", "<cstdio>") if suffix == "cpp" else TRIO)
    selectors = ["t", "t.p", "t.p.y", "q.y", "arr[1]", "ptrs"]
    rows = {}
    for hit in (3, 4, 5):
        assert main(["session", "start", str(path), "--at", "8", "--hit", str(hit)]) == 0
        capsys.readouterr()
        try:
            main(["session", "get", "--frame", "1", *selectors, "--json"])
            rows[hit] = {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
        finally:
            main(["session", "close"])
            capsys.readouterr()
    if suffix == "cpp" and cxx_is_gcc():
        # GCC gives an aggregate's declaration line no code of its own (#167): its stores
        # carry its elements' lines. Withheld while they run; read once all of their code
        # comes before the call (`q` from line 21 on, `t` at line 25: on main, never).
        for hit in (3, 4):
            for selector in ("t", "t.p", "t.p.y"):
                row = rows[hit][selector]
                assert "value" not in row and (
                    "declared on line 21, which has no code of its own (an aggregate is "
                    "stored element by element)" in row["error"]
                ), (hit, row)
        for hit in (3, 4, 5):
            assert rows[hit]["q.y"]["value"] == "4", rows[hit]["q.y"]
            assert rows[hit]["arr[1]"]["value"] == "18" and rows[hit]["ptrs"]["unavailable"]
        assert rows[5]["t.p.y"]["value"] == "8" and rows[5]["t"].get("value"), rows[5]
        return
    for hit in (3, 4):
        for selector in ("t", "t.p", "t.p.y"):
            row = rows[hit][selector]
            assert "value" not in row and row.get("unavailable", selector != "t"), (hit, row)
            assert (
                "its declaration (lines 21-23) is still running in this caller frame"
                in row["error"]
            ), (hit, row)
        assert rows[hit]["q.y"]["value"] == "4" and rows[hit]["arr[1]"]["value"] == "18"
    assert rows[5]["t.p.y"]["value"] == "8" and rows[5]["ptrs"]["unavailable"]


LOOPED = """#include <stdio.h>

struct pair { long x, y; };
struct trio { long x; struct pair p; };

__attribute__((noinline)) static long twice(long v) {
    long w = v * 2;
    return w;
}

__attribute__((noinline)) static void scribble(void) {
    volatile long junk[96];
    for (int i = 0; i < 96; i++) junk[i] = 0x5150 + i;
}

__attribute__((noinline)) static long run(long seed) {
    long total = 0;
    for (long k = 0; k < 2; k++) {
        total += twice(100 + k);
        struct trio t = { seed,
            { twice(k),
              seed } };
        struct pair ps[2] = {
            { k, seed },
            { seed, k } };
        total += twice(t.p.x + ps[1].y);
    }
    return total;
}

int main(void) {
    scribble();
    printf("%ld\\n", run(5));
    return 0;
}
"""


@pytest.fixture(
    params=[("c", "clang"), ("c", "gcc"), ("cpp", "clang"), ("cpp", "gcc")],
    ids=["c-clang", "c-gcc", "cpp-clang", "cpp-gcc"],
)
def looped(request, tmp_path, monkeypatch):
    """LOOPED, built as C or C++ by clang or GCC (ids `c-gcc`, `cpp-clang`, …)."""
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    language, family = request.param
    if language == "cpp":
        compiler, why = cxx_for(family)
        variable = "PEEKY_NATIVE_CXX"
    else:
        compiler, why = os.environ.get("PEEKY_NATIVE_CC", "clang"), None
        if family == "gcc":
            compiler, why = gcc(), "needs a gcc that is GCC"
        elif is_gcc(compiler):
            compiler, why = None, "this lane builds C with GCC; the clang lanes run the clang case"
        variable = "PEEKY_NATIVE_CC"
    if compiler is None:
        pytest.skip(why)
    monkeypatch.setenv(variable, compiler)
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))

    def write(text, name="looped"):
        path = tmp_path / f"{name}.{language}"
        path.write_text(text.replace("<stdio.h>", "<cstdio>") if language == "cpp" else text)
        return path

    return SimpleNamespace(language=language, family=family, write=write)


@pytest.mark.timeout(300)
def test_a_callers_aggregate_is_read_once_its_lines_have_run_on_this_pass(looped, capsys):
    # g++ gives `struct trio t = { seed,` no code of its own, and g++ and clang give
    # `struct pair ps[2] = {` none: a caller frame withheld them even after their stores,
    # at the call on line 26 (on main). Read once all of their lines' code comes before
    # the call; withheld above them (on the next pass, line 19 sees the last pass's
    # values) and while they run. GCC's C gives both lines code (unchanged).
    selectors = ["t", "t.x", "t.p.x", "t.p.y", "ps", "ps[0].x", "ps[1].y"]
    path = looped.write(LOOPED)
    rows = {hit: _frame_one(capsys, path, 7, hit, *selectors) for hit in range(1, 7)}
    for hit in (1, 2, 4, 5):
        for selector in selectors:
            row = rows[hit][selector]
            line = 20 if selector.startswith("t") else 23
            assert "value" not in row and f"line {line}" in row["error"], (hit, row)
    for hit, k in ((3, 0), (6, 1)):
        values = {selector: rows[hit][selector].get("value") for selector in selectors}
        assert values["t"] is not None and values["ps"] is not None, (hit, rows[hit])
        assert {s: values[s] for s in selectors if "." in s} == {
            "t.x": "5",
            "t.p.x": str(2 * k),
            "t.p.y": "5",
            "ps[0].x": str(k),
            "ps[1].y": str(k),
        }, (hit, rows[hit])


# The same, where a unit may optimize a function beyond -O0 on its own (review of PR #366):
# its code may leave source order, so the line table proves nothing.
OPTIMIZING = {
    "attribute": LOOPED.replace(
        "__attribute__((noinline)) static long run(",
        '__attribute__((noinline, optimize("reorder-blocks"))) static long run(',
    ),
    "pragma": LOOPED.replace(
        "#include <stdio.h>\n\n", '#include <stdio.h>\n#pragma GCC optimize ("reorder-blocks")\n'
    ),
}


@pytest.mark.timeout(300)
@pytest.mark.parametrize("kind", sorted(OPTIMIZING))
def test_a_callers_aggregate_in_a_unit_that_may_optimize_a_function_is_withheld(
    looped, capsys, kind
):
    # On the PR's first head, `ps` was read at line 26 with `optimize("reorder-blocks")` on
    # `run`, or `#pragma GCC optimize` above it: the build's -O0 was taken as proof.
    if looped.language == "c" and looped.family == "gcc":
        pytest.skip("GCC's C gives these declarations' first lines code: the rule is not used")
    path = looped.write(OPTIMIZING[kind])
    withheld = ["ps", "ps[1].y"] + (["t", "t.p.x"] if looped.language == "cpp" else [])
    if looped.family == "clang":
        withheld = withheld[:2]
    for hit in (3, 6):
        rows = _frame_one(capsys, path, 7, hit, *withheld)
        for selector in withheld:
            row = rows[selector]
            line = 20 if selector.startswith("t") else 23
            assert "value" not in row and (
                f"declared on line {line}, which has no code of its own" in row["error"]
            ), (hit, row)


INLINED = """#include <stdio.h>

struct pair { long x, y; };

__attribute__((noinline)) static long twice(long v) {
    long w = v * 2;
    return w;
}

__attribute__((noinline)) static void scribble(void) {
    volatile long junk[96];
    for (int i = 0; i < 96; i++) junk[i] = 0x5150 + i;
}

static inline __attribute__((always_inline)) long lifted(long v) {
    long r = twice(v);
    return r + 1;
}

__attribute__((noinline)) static long run(long seed) {
    struct pair ps[2] = {
        { seed, lifted(1) },
        { seed, seed } };
    long total = lifted(ps[0].y);
    total += twice(ps[1].y);
    return total;
}

int main(void) {
    scribble();
    printf("%ld\\n", run(5));
    return 0;
}
"""


def _run_rows(capsys, path, line, hit, *selectors):
    """`session get` of ``selectors`` in the caller frame that runs `run` (frame 1, or frame
    2 above an inlined call's), at ``hit`` of ``line``: (frame, rows), or (frame, the
    error) where Peeky refuses to read that frame."""
    assert main(["session", "start", str(path), "--at", str(line), "--hit", str(hit)]) == 0
    capsys.readouterr()
    try:
        for frame in (1, 2):
            main(["session", "get", "--frame", str(frame), *selectors, "--json"])
            found = json.loads(capsys.readouterr().out)
            if isinstance(found, dict):
                return frame, found["error"]
            rows = {row["selector"]: row for row in found}
            if all("run" in str(row.get("function")) for row in rows.values()):
                return frame, rows
        raise AssertionError(f"no frame runs `run` at hit {hit}: {rows}")
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(300)
def test_a_callers_aggregate_above_an_inlined_call_is_withheld(looped, capsys):
    # Review of PR #366: LLDB gives the caller of an inlined call (`lifted`, always_inline)
    # the address where that call's code starts, which is no return address. On the PR's
    # first head, `ps` was read there at line 24 by taking it as one. Withheld inside and
    # after its declaration above an inlined call; read in a physical call's caller.
    if looped.language == "c" and looped.family == "gcc":
        pytest.skip("GCC's C gives these declarations' first lines code: the rule is not used")
    path = looped.write(INLINED, "inlined")
    selectors = ("ps", "ps[0].y", "ps[1].y")
    for hit in (1, 2):
        frame, rows = _run_rows(capsys, path, 6, hit, *selectors)
        assert frame == 2, (hit, rows)
        if "code" in rows:
            # LLDB shows the caller of an inlined call at the call's line; where g++ gives
            # that line no code of its own, Peeky refuses the frame (as on main).
            assert rows["code"] == "DEBUGGER_ERROR" and looped.family == "gcc", rows
            assert "stopped line does not match prepared symbols" in rows["message"], rows
            continue
        for selector in selectors:
            row = rows[selector]
            assert "value" not in row and "declared on line 21" in row["error"], (hit, row)
    frame, rows = _run_rows(capsys, path, 6, 3, *selectors)
    assert frame == 1 and rows["ps"]["value"] == "[{x = 5, y = 3}, {x = 5, y = 5}]", rows
    assert rows["ps[0].y"]["value"] == "3" and rows["ps[1].y"]["value"] == "5", rows


HELPERS = """#include <stdio.h>

struct pair { long x, y; };
struct trio { long x; struct pair p; };

__attribute__((noinline)) static long twice(long v) {
    long w = v * 2;
    return w;
}

__attribute__((noinline)) static void scribble(void) {
    volatile long junk[96];
    for (int i = 0; i < 96; i++) junk[i] = 0x5150 + i;
}
"""

INCLUDED = {
    "rows": (
        """
__attribute__((noinline)) static long run(long seed) {
    long head = seed;
    struct pair ps[2] = {
#include "table.h"
    };
    printf("%ld %ld\\n", ps[1].y, head);
    return ps[0].x;
}
""",
        "table.h",
        "        { twice(1), twice(2) },\n        { twice(3), twice(4) },\n",
    ),
    "declaration": (
        """
__attribute__((noinline)) static long run(long seed) {
    long head = seed;
#include "decl.h"
        { twice(1), twice(2) },
        { twice(3), twice(4) },
    };
    printf("%ld %ld\\n", ps[1].y, head);
    return ps[0].x;
}
""",
        "decl.h",
        "    struct pair ps[2] = {\n",
    ),
}
MAIN = """
int main(void) {
    scribble();
    return (int)(run(5) & 1);
}
"""


def _frame_one(capsys, path, line, hit, *selectors):
    assert main(["session", "start", str(path), "--at", str(line), "--hit", str(hit)]) == 0
    capsys.readouterr()
    try:
        main(["session", "get", "--frame", "1", *selectors, "--json"])
        return {row["selector"]: row for row in json.loads(capsys.readouterr().out)}
    finally:
        main(["session", "close"])
        capsys.readouterr()


@pytest.mark.timeout(240)
@pytest.mark.parametrize("kind", sorted(INCLUDED))
def test_a_callers_local_declared_in_another_file_than_its_line_is_withheld(bucket, capsys, kind):
    # Review of #104 (round 4): with an initializer's rows in an `#include`d file,
    # `--frame 1` (at table.h:2) showed `ps[1].x = 429835611285028900`; so does a
    # declaration in an included file, with the frame in the including file.
    body, header, fragment = INCLUDED[kind]
    directory = bucket.parent.with_name(f"inc-{kind}")
    directory.mkdir()
    (directory / header).write_text(fragment)
    (directory / "inc.c").write_text(HELPERS + body + MAIN)
    rows = _frame_one(capsys, directory / "inc.c", 8, 3, "ps[1]", "ps[1].x", "seed")
    declared, line, frame = (
        ("inc.c", 18, "table.h:2") if kind == "rows" else ("decl.h", 1, "inc.c:20")
    )
    assert rows["ps[1].x"]["location"].endswith(frame), rows["ps[1].x"]
    for selector in ("ps[1]", "ps[1].x"):
        assert "value" not in rows[selector], rows[selector]
        assert (
            f"declared on line {line} of {declared}, and this caller frame is in another file "
            f"({frame})" in rows[selector]["error"]
        ), rows[selector]
    assert rows["seed"]["value"] == "5"


DIRECTIVES = (
    HELPERS
    + """
__attribute__((noinline)) static long run(long seed) {
    struct trio t = { seed,
%:if 0
        };
%:endif
        { twice(3),
          twice(4) } };
    struct trio u = { seed,
#\\
if 0
        };
#endif
        { twice(5),
          twice(6) } };
    return t.p.y + u.p.y;
}

int main(void) {
    scribble();
    printf("%ld\\n", run(1));
    return 0;
}
"""
)

DIGRAPHS = """#include <cstdio>

struct Pair { long x, y; };
struct Trio { long x; Pair p; };

__attribute__((noinline)) static long twice(long v) {
    long w = v * 2;
    return w;
}

__attribute__((noinline)) static void scribble() {
    volatile long junk[96];
    for (int i = 0; i < 96; i++) junk[i] = 0x5150 + i;
}

#define INIT_BEGIN {
#define INIT_END }
#define LAMBDA_BEGIN [&]() {
#define LAMBDA_END }()

__attribute__((noinline)) static long run_digraph(long seed) {
    Trio t <% [&]() <% long a = twice(10); return a + seed; %>(),
        <% twice(11),
           twice(12) %> %>;
    return t.p.y;
}

__attribute__((noinline)) static long run_macro(long seed) {
    Trio u = INIT_BEGIN LAMBDA_BEGIN long a = twice(20); return a + seed; LAMBDA_END,
        { twice(21),
          twice(22) } INIT_END;
    return u.p.y;
}

int main() {
    scribble();
    long r = run_digraph(1);
    scribble();
    r += run_macro(2);
    std::printf("%ld\\n", r);
    return 0;
}
"""


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "program, cases",
    [
        # `%:if 0` and `#\` + `if 0` hide `};` from the compiler, not from a text scan.
        ("dirs.c", ((1, "t", 17, None), (3, "u", 23, None))),
        # `<%` `%>` are braces; INIT_BEGIN and LAMBDA_BEGIN open them out of sight.
        ("dig.cpp", ((2, "t", 22, 24), (5, "u", 29, None), (6, "u", 29, None))),
    ],
)
def test_a_declaration_the_compiler_reads_differently_from_its_text_is_withheld(
    bucket, capsys, program, cases
):
    # Review of #104 (round 4): the statement scan found an end too early, so
    # `get t.p --frame 1` showed leftovers.
    directory = bucket.parent.with_name(program.replace(".", "-"))
    directory.mkdir()
    path = directory / program
    path.write_text(DIRECTIVES if program == "dirs.c" else DIGRAPHS)
    for hit, name, declared, end in cases:
        rows = _frame_one(capsys, path, 8, hit, f"{name}.p", f"{name}.p.y")
        for row in rows.values():
            assert "value" not in row, (hit, row)
            assert f"declared on line {declared}, and " in row["error"], (hit, row)
            assert (
                f"(lines {declared}-{end}) is still running"
                if end
                else "cannot tell from the source where its declaration ends"
            ) in row["error"], (hit, row)


@pytest.mark.timeout(240)
def test_a_lone_carriage_return_breaks_a_line_as_the_compiler_counts_it(bucket, capsys):
    # Review of #104 (round 5): with a lone `\r` between `{ twice(3),` and
    # `twice(4) } };`, the scan ended `t` on line 22, so frame 1 at line 23 (hit 4)
    # showed `t.p.y = 20906`. The frame's line and DWARF's declaration lines count it too.
    directory = bucket.parent.with_name("cr")
    directory.mkdir()
    path = directory / "cr.c"
    source = TRIO.replace("{ twice(3),\n", "{ twice(3),\r")
    assert source.count("\r") == 1
    path.write_bytes(source.encode())
    for hit, line in ((3, 22), (4, 23)):
        rows = _frame_one(capsys, path, 8, hit, "t", "t.p.y")
        assert rows["t"]["location"].endswith(f"cr.c:{line}"), rows["t"]
        for row in rows.values():
            assert "value" not in row, (hit, row)
            assert "its declaration (lines 21-23) is still running" in row["error"], (hit, row)
    rows = _frame_one(capsys, path, 8, 5, "t.p.y", "ptrs")
    assert rows["t.p.y"]["value"] == "8" and rows["ptrs"]["location"].endswith("cr.c:25")
    assert "declared on line 24" in rows["ptrs"]["error"], rows["ptrs"]
    # `\n\r` is two breaks to Clang's line table (since Clang 9), not one pair.
    path.write_bytes(TRIO.replace("{ twice(3),\n", "{ twice(3),\n\r").encode())
    rows = _frame_one(capsys, path, 8, 4, "t")
    assert rows["t"]["location"].endswith("cr.c:24"), rows["t"]
    assert "its declaration (lines 21-24) is still running" in rows["t"]["error"], rows["t"]


GOTO = """#include <stdio.h>
int main(void) {
    int i = 0;
again:
    printf("i=%d\\n", i);
    int x = i * 10 + 7;
    i++;
    if (i < 3) goto again;
    printf("x=%d\\n", x);
    return 0;
}
"""

LOCAL_CTOR = """#include <cstdio>
int helper(int a) { return a * 2; }
int main() {
    struct Box {
        int w = helper(3);
        int h;
        Box(int v) : h(v) {
            std::printf("%d\\n", w + h);
        }
    };
    Box b(7);
    int v = b.w + b.h;
    std::printf("%d\\n", v);
    return 0;
}
"""


@pytest.mark.timeout(120)
def test_a_local_a_backward_goto_can_keep_live_above_its_declaration_is_withheld(bucket, capsys):
    # Review of #81: on later passes `x` holds its value from the previous pass, and it
    # was shown, but on the first it holds leftovers, which were shown too (only later
    # passes were checked). Peeky cannot tell the passes apart, so it withholds every
    # one (sixth agent evaluation: never a value it cannot vouch for).
    program = bucket.with_name("goto.c")
    program.write_text(GOTO)
    code, result = probe(capsys, program, "--at", "5", "--capture", "x", "--hits", "3")
    assert code == 0, result.get("error")
    assert len(result["rows"]) == 3, result["rows"]
    for row in result["rows"]:
        assert "value" not in row and "a `goto` return above it" in row["error"], row


@pytest.mark.cpp
@pytest.mark.timeout(120)
def test_a_constructor_parameter_is_never_withheld_for_an_enclosing_functions_local(bucket, capsys):
    # Review of #81: the constructor runs `w = helper(3)` on line 5, above its own
    # declaration; `main` declares another `v` on line 12.
    directory = bucket.parent.with_name("ctor")
    directory.mkdir()
    program = directory / "box.cpp"
    program.write_text(LOCAL_CTOR)
    line = 5
    if cxx_is_gcc():
        never_stops_at(capsys, program, line)
        # The constructor's own body, below main's `v`'s line too.
        line = 8
    code, result = probe(capsys, program, "--at", line, "--capture", "v")
    assert code == 0, result.get("error")
    assert result["rows"][0].get("value") == "7", result["rows"]


ITEMS = """#include <cstdio>
static int default_weight() { return 10; }
struct Item {
    int id;
    int weight = default_weight();
};
int main() {
    long total = 0;
    for (int i = 0; i < 3; i++) {
        Item it{i};
        total += it.id + it.weight;
    }
    std::printf("%ld\\n", total);
    return 0;
}
"""

TRY = """#include <setjmp.h>
#include <stdio.h>
static jmp_buf env;
#define TRY if (setjmp(env) == 0)
int main(void) {
    volatile int tries = 0;
    TRY { printf("first\\n"); }
    else {
        printf("again\\n");
    }
    volatile int x = 40 + tries;
    tries++;
    if (tries < 3) longjmp(env, 1);
    printf("%d\\n", x);
    return 0;
}
"""


@pytest.mark.cpp
@pytest.mark.timeout(120)
def test_a_member_initializer_run_by_its_caller_withholds_nothing(bucket, capsys):
    # Review of #81: clang runs `weight = default_weight()` in main on line 5, inside
    # the code of line 10; main's locals are live there.
    directory = bucket.parent.with_name("items")
    directory.mkdir()
    program = directory / "items.cpp"
    program.write_text(ITEMS)
    line = 5
    if cxx_is_gcc():
        never_stops_at(capsys, program, line)
        # The line building `it`, where GCC runs its initializer.
        line = 10
    code, result = probe(capsys, program, "--at", line, "--capture", "i,total", "--hits", "3")
    assert code == 0, result.get("error")
    values = [(row["selector"], row.get("value")) for row in result["rows"]]
    assert values == [("i", "0"), ("total", "0"), ("i", "1"), ("total", "10")] + [
        ("i", "2"),
        ("total", "21"),
    ], result["rows"]


@pytest.mark.timeout(120)
def test_setjmp_behind_a_macro_withholds_nothing(bucket, capsys):
    # Review of #81: after longjmp, the volatile `x` declared below is live (40, 41).
    program = bucket.with_name("try.c")
    program.write_text(TRY)
    code, result = probe(capsys, program, "--at", "9", "--capture", "x", "--hits", "2")
    assert code == 0, result.get("error")
    assert [row.get("value") for row in result["rows"]] == ["40", "41"], result["rows"]


GEO = """int next_id();
struct Pt { int a = next_id(); int b = 2; };
inline int origin_sum() {
  Pt q{};
  int x = q.a + q.b;
  return x;
}
"""

GEO_MAIN = """#include "geo.hpp"
int counter = 0;
int next_id() { return ++counter; }
int main() {
  int x = 41;
  Pt p{};
  return x + p.a + origin_sum() > 0 ? 0 : 1;
}
"""

FOR = """int main(void) {
  int total = 0;
  for (int i = 0; i < 3; i++) {
    total += i;
  }
  return total;
}
"""


def session_inspect(capsys, program, line, *steps, hit=1):
    assert main(["session", "start", str(program), "--at", str(line), "--hit", str(hit)]) == 0
    capsys.readouterr()
    try:
        for _ in range(steps[0] if steps else 0):
            assert main(["session", "step"]) == 0
        capsys.readouterr()
        assert main(["session", "inspect", "--json"]) == 0
        snapshot = json.loads(capsys.readouterr().out)
    finally:
        main(["session", "close"])
        capsys.readouterr()
    return snapshot, {v["name"]: v for s in snapshot["scopes"] for v in s["variables"]}


@pytest.mark.cpp
@pytest.mark.timeout(120)
def test_a_header_structs_initializer_run_in_main_withholds_nothing(bucket, capsys):
    # Review of #81: stepping from main.cpp:5 lands on geo.hpp:2 in main (clang runs
    # the member initializer there); main's live `x` is not geo.hpp's `x` (line 5).
    directory = bucket.parent.with_name("geo")
    directory.mkdir()
    (directory / "geo.hpp").write_text(GEO)
    (directory / "main.cpp").write_text(GEO_MAIN)
    snapshot, shown = session_inspect(capsys, directory / "main.cpp", 5, 1)
    # GCC runs the initializer on `p`'s own line (#167): the step ends on main's next.
    landed = "main.cpp:6" if cxx_is_gcc() else "geo.hpp:2"
    assert snapshot["location"].endswith(landed), snapshot["location"]
    assert shown["x"].get("value") == "41", shown["x"]


@pytest.mark.timeout(120)
def test_a_live_for_counter_at_a_step_stop_on_its_header_is_shown(bucket, capsys):
    program = bucket.with_name("for.c")
    program.write_text(FOR)
    # Hit 2 of line 4, then step to line 5 and back onto the header (the increment).
    snapshot, shown = session_inspect(capsys, program, 4, 2, hit=2)
    assert snapshot["location"].endswith("for.c:3"), snapshot["location"]
    assert shown["i"]["value"] == "1" and not shown["i"].get("unavailable"), shown["i"]


CONVERT = """#include <stdio.h>
static int left = 3;
static int next(void) { return left--; }
static long long convert(int ms) { long long us = ms * 1000; return us; }
long long spread(int ms) {
    long long us = ms * 1000;
    return us;
}
int main(void) {
    long long total = 0;
    for (int i = 0; i < 3; i++) total += spread(i + 1);
    for (int j = 0; j < 4; j++) { int sq = j * j; total += sq; }
    int x = 0;
    do {
        int y = x * 2;
        total += y + convert(x) + next();
        x++;
    } while (x < 3);
    int i2; for (
        i2 = 10; i2 < 13; i2++) total += i2;
    int sw; switch (
        sw = next()) {
    case 0: total += sw + 1; break;
    default: break;
    }
    printf("TOTAL:%lld\\n", total);
    return 0;
}
"""


@pytest.fixture
def convert(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "convert.c").write_text(CONVERT)
    return tmp_path / "convert.c"


def _rows(capsys, path, line, capture, hits):
    code, result = probe(capsys, path, "--at", line, "--capture", capture, "--hits", hits)
    assert code == 0, result.get("error")
    return {(row["hit"], row["selector"]): row for row in result["rows"]}


def _withheld(row, line):
    return "value" not in row and f"declared on line {line}, which has not run yet" in row["error"]


@pytest.mark.timeout(180)
def test_a_local_declared_on_the_stop_line_is_withheld_where_its_line_starts(convert, capsys):
    # #92 (eval 4, C): `us` showed the previous call's 1000 at its own declaration line.
    rows = _rows(capsys, convert, 6, "us,ms", 3)
    for hit in (1, 2, 3):
        assert _withheld(rows[hit, "us"], 6) and rows[hit, "us"].get("settable") is False
        assert rows[hit, "ms"]["value"] == str(hit)
    code, result = probe(capsys, convert, "--at", "6", "--capture", "ms", "--set", "us=5")
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED"
    assert "declared on line 6" in result["error"]["message"]
    # Review of #99: a one-line function stops after its prologue, not at the line's
    # first row; a braced one-line loop body stops where its own block starts.
    rows = _rows(capsys, convert, 4, "us,ms", 2)
    assert all(_withheld(rows[hit, "us"], 4) for hit in (1, 2))
    rows = _rows(capsys, convert, 12, "sq,j", 5)
    assert _withheld(rows[1, "j"], 12)
    for hit in (2, 3, 4, 5):
        assert _withheld(rows[hit, "sq"], 12)
        assert rows[hit, "j"]["value"] == str(hit - 2)


@pytest.mark.timeout(180)
def test_a_line_a_loop_returns_to_and_mid_line_stops_show_the_value(convert, capsys):
    # A do-while branches back to its body's first line: control re-enters it from the
    # loop, so its start is not a first pass (x86-64 does this for `while` tests too).
    rows = _rows(capsys, convert, 15, "y,x", 3)
    assert all("value" in rows[hit, "y"] for hit in (1, 2, 3))
    # Review of #99: a header over several lines runs its next line's code first (the
    # loop's `i2 = 10`, the switch's `sw = next()`), then its own line, with the local set.
    rows = _rows(capsys, convert, 19, "i2", 3)
    assert [rows[hit, "i2"]["value"] for hit in (1, 2, 3)] == ["10", "11", "12"]
    rows = _rows(capsys, convert, 21, "sw", 1)
    assert rows[1, "sw"]["value"] == "0"
    # A return from a call stops mid-line, where the one-line loop's `i` is live.
    main(["session", "start", str(convert), "--at", "6", "--hit", "2"])
    capsys.readouterr()
    try:
        assert main(["session", "step", "--out"]) == 0
        assert ":11 in main" in capsys.readouterr().out
        assert main(["session", "get", "i", "--json"]) == 0
        rows = json.loads(capsys.readouterr().out)
    finally:
        main(["session", "close"])
        capsys.readouterr()
    assert rows[0]["value"] == "1"
