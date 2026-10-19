"""C and C++ locals declared below the stop are never shown as values (#74)."""

import json
import os

import pytest

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
def test_a_backward_goto_keeps_the_local_live_above_its_declaration(bucket, capsys):
    # Review of #81: on later passes `x` holds its value from the previous pass.
    program = bucket.with_name("goto.c")
    program.write_text(GOTO)
    code, result = probe(capsys, program, "--at", "5", "--capture", "x", "--hits", "3")
    assert code == 0, result.get("error")
    assert [row.get("value") for row in result["rows"]][1:] == ["7", "17"]


@pytest.mark.timeout(120)
def test_a_constructor_parameter_is_never_withheld_for_an_enclosing_functions_local(bucket, capsys):
    # Review of #81: the constructor runs `w = helper(3)` on line 5, above its own
    # declaration; `main` declares another `v` on line 12.
    directory = bucket.parent.with_name("ctor")
    directory.mkdir()
    program = directory / "box.cpp"
    program.write_text(LOCAL_CTOR)
    code, result = probe(capsys, program, "--at", "5", "--capture", "v")
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


@pytest.mark.timeout(120)
def test_a_member_initializer_run_by_its_caller_withholds_nothing(bucket, capsys):
    # Review of #81: clang runs `weight = default_weight()` in main on line 5, inside
    # the code of line 10; main's locals are live there.
    directory = bucket.parent.with_name("items")
    directory.mkdir()
    program = directory / "items.cpp"
    program.write_text(ITEMS)
    code, result = probe(capsys, program, "--at", "5", "--capture", "i,total", "--hits", "3")
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


@pytest.mark.timeout(120)
def test_a_header_structs_initializer_run_in_main_withholds_nothing(bucket, capsys):
    # Review of #81: stepping from main.cpp:5 lands on geo.hpp:2 in main (clang runs
    # the member initializer there); main's live `x` is not geo.hpp's `x` (line 5).
    directory = bucket.parent.with_name("geo")
    directory.mkdir()
    (directory / "geo.hpp").write_text(GEO)
    (directory / "main.cpp").write_text(GEO_MAIN)
    snapshot, shown = session_inspect(capsys, directory / "main.cpp", 5, 1)
    assert snapshot["location"].endswith("geo.hpp:2"), snapshot["location"]
    assert shown["x"].get("value") == "41", shown["x"]


@pytest.mark.timeout(120)
def test_a_live_for_counter_at_a_step_stop_on_its_header_is_shown(bucket, capsys):
    program = bucket.with_name("for.c")
    program.write_text(FOR)
    # Hit 2 of line 4, then step to line 5 and back onto the header (the increment).
    snapshot, shown = session_inspect(capsys, program, 4, 2, hit=2)
    assert snapshot["location"].endswith("for.c:3"), snapshot["location"]
    assert shown["i"]["value"] == "1" and not shown["i"].get("unavailable"), shown["i"]
