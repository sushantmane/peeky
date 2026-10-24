"""`--set-at LINE@HIT name=@before` on real C and C++ builds (#305): the guard flip that skips
a statement, in two `--set-at`s. Eleventh evaluation, tides: testing "don't move `turn` while
the tide stands" (pointers can't be set) took six `--set-at`s, flipping `trend` to 0 before
the inner `if` and restoring it after, with the restored values (1, 1, -1) copied by hand."""

import json
import os

import pytest

from peeky.cli import main
from peeky.probe import _table

# Like tides, on a trail's height profile: on a flat, line GUARD's branch moves `turn` along
# it, so a top or bottom is reported at the flat's last point instead of its first.
TRAIL = r"""/* trail: the tops and bottoms of a trail's height profile. Each argument is one height in
   metres, measured every 100 m. A top is higher than the points before and after it; where the
   trail is flat at its top, the top is the first point of the flat. A bottom likewise. */
#include <stdio.h>
#include <stdlib.h>

static int find_turns(const int *height, int n, const int **at, char *kind, int *flat)
{
    const int *end = height + n;
    const int *turn = height; /* the point where the height last moved */
    int trend = 0;            /* +1 rising, -1 falling, 0 not known yet */
    int count = 0;
    for (const int *p = height + 1; p < end; p++) {
        if (*p == p[-1]) {
            if (trend != 0) { /* GUARD */
                turn = p;
            }
            *flat += 1; /* FLAT */
            continue;
        }
        int now = *p > p[-1] ? 1 : -1;
        if (now != trend && trend != 0) {
            kind[count] = trend == 1 ? 'T' : 'B';
            at[count] = turn;
            count++;
        }
        trend = now;
        turn = p;
    }
    return count;
}

int main(int argc, char **argv)
{
    int height[64];
    const int *at[64];
    char kind[64];
    int n = 0, flat = 0;
    for (int i = 1; i < argc && n < 64; i++) {
        height[n++] = atoi(argv[i]);
    }
    int count = find_turns(height, n, at, kind, &flat);
    printf("TRAIL:");
    for (int i = 0; i < count; i++) {
        printf("%s%c@%d %d", i ? "," : "", kind[i], (int)(at[i] - height) * 100, *at[i]);
    }
    printf(";FLAT=%d\n", flat);
    return 0;
}
"""
HEIGHTS = ("410", "430", "455", "455", "455", "440", "420", "400", "400", "415", "430")
# Each call of `pass` from the loop has the same function, depth and callers: only the watch
# on its first line tells the second call from the first. `fact` recurses.
AGAIN = r"""#include <stdio.h>

static int pass(int x)
{
    int mode = x * 10;
    if (x == 1) {
        mode += 1; /* CHANGE */
        return mode;
    }
    if (x == 2) {
        return mode; /* RESTORE */
    }
    return 0;
}

static int fact(int n)
{
    int k = n;
    if (n > 1) {
        k = n * fact(n - 1); /* RECURSE */
    }
    return k; /* BACK */
}

int main(void)
{
    int total = 0;
    for (int x = 1; x <= 2; x++) {
        total += pass(x);
    }
    printf("TOTAL=%d FACT=%d\n", total, fact(3));
    return 0;
}
"""


@pytest.fixture
def native(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def write(root, name, text):
    path = root / name
    path.write_text(text)
    return path


def probe(capsys, path, at, *argv, args=()):
    code = main(
        ["probe", str(path), "--at", f"{path.name}:{at}", *argv, "--json", "--quiet", "--no-save"]
        + [f"--arg={arg}" for arg in args]
    )
    return code, json.loads(capsys.readouterr().out)


def line_of(text, marker):
    return next(i for i, line in enumerate(text.splitlines(), 1) if marker in line)


@pytest.mark.timeout(300)
@pytest.mark.parametrize("name", ["trail.c", "trail.cpp"])
def test_the_guard_flip_skips_the_move_with_two_set_ats(native, capsys, name):
    if name.endswith(".cpp") and not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_CXX to build C++")
    path = write(native, name, TRAIL)
    guard, flat = line_of(TRAIL, "/* GUARD */"), line_of(TRAIL, "/* FLAT */")
    code, plain = probe(capsys, path, guard, args=HEIGHTS)
    assert code == 0 and plain["output"].strip() == "TRAIL:T@400 455,B@800 400;FLAT=3", plain
    code, result = probe(
        capsys, path, guard, "--capture", "trend", "--hits", "all",
        "--set-at", f"{guard}@all", "trend=0", "--set-at", f"{flat}@all", "trend=@before",
        args=HEIGHTS,
    )  # fmt: skip
    assert code == 0, result
    # The move is skipped at each of the three flat points: tops and bottoms at the first.
    assert result["output"].strip() == "TRAIL:T@200 455,B@700 400;FLAT=3", result["output"]
    changes = [
        (c["at"], c["before"], c["after"], c.get("restores")) for c in result["interventions"]
    ]
    assert changes == [
        (f"{guard}@1", "1", "0", None),
        (f"{flat}@1", "0", "1", f"{guard}@1"),
        (f"{guard}@2", "1", "0", None),
        (f"{flat}@2", "0", "1", f"{guard}@2"),
        (f"{guard}@3", "-1", "0", None),
        (f"{flat}@3", "0", "-1", f"{guard}@3"),
    ]
    text = _table(result)
    assert f"set trend: 0 -> -1  (restored, --set-at line {flat}@all, hit 3, in find_turns" in text
    assert [o["complete"] for o in result["set_outcomes"]] == [True, True], result


@pytest.mark.timeout(300)
def test_a_restore_in_another_call_or_frame_is_refused(native, capsys):
    path = write(native, "again.c", AGAIN)
    change, restore = line_of(AGAIN, "/* CHANGE */"), line_of(AGAIN, "/* RESTORE */")
    # `pass(1)` changes `mode` and returns; `pass(2)` reaches the restore, from the same call.
    code, result = probe(
        capsys, path, change, "--set-at", f"{change}@1", "mode=5", "--set-at", f"{restore}@1",
        "mode=@before",
    )  # fmt: skip
    assert code == 1 and result["status"] == "failed", result
    message = result["error"]["message"]
    assert result["error"]["code"] == "MUTATION_REJECTED", result["error"]
    assert (
        f"`mode` was changed at line {change}@1 in pass, and the first line of pass ran again "
        "since, at the same stack depth or above"
    ) in message, message
    assert [c["after"] for c in result["interventions"]] == ["5"]
    # `fact(3)` changes `k`; the first stop of BACK is in `fact(1)`, two calls deeper.
    recurse, back = line_of(AGAIN, "/* RECURSE */"), line_of(AGAIN, "/* BACK */")
    code, result = probe(
        capsys, path, recurse, "--set-at", f"{recurse}@1", "k=7", "--set-at", f"{back}@1",
        "k=@before",
    )  # fmt: skip
    assert code == 1, result
    assert "this stop is 2 calls deeper (a recursive call)" in result["error"]["message"], result
