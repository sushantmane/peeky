"""`--set-at LINE@HIT name=@before` against real debugpy: the guard flip that skips a
statement in two `--set-at`s, in a function and at module level; another call of the same
function, and a generator, are refused."""

import json

import pytest

from peeky.cli import main
from peeky.probe import _table

TRAIL = """import sys


def find_turns(height):
    turns = []  # FIRST
    turn = 0  # the point where the height last moved
    trend = 0  # +1 rising, -1 falling, 0 not known yet
    flat = 0  # points level with the one before
    for i in range(1, len(height)):
        if height[i] == height[i - 1]:
            if trend != 0:  # GUARD
                turn = i  # MOVE
            flat += 1  # FLAT
            continue
        now = 1 if height[i] > height[i - 1] else -1
        if now != trend and trend != 0:
            turns.append(("T" if trend == 1 else "B") + "@%d %d" % (turn * 100, height[turn]))
        trend = now
        turn = i
    return ",".join(turns) + ";FLAT=%d" % flat


print("TRAIL:" + find_turns([int(arg) for arg in sys.argv[1:]]))
"""
# The same loop at module level: its frame is entered once.
TOP = """import sys

height = [int(arg) for arg in sys.argv[1:]]
turns = []
turn = 0
trend = 0
flat = 0
for i in range(1, len(height)):
    if height[i] == height[i - 1]:
        if trend != 0:  # GUARD
            turn = i
        flat += 1  # FLAT
        continue
    now = 1 if height[i] > height[i - 1] else -1
    if now != trend and trend != 0:
        turns.append(("T" if trend == 1 else "B") + "@%d %d" % (turn * 100, height[turn]))
    trend = now
    turn = i
print("TRAIL:" + ",".join(turns) + ";FLAT=%d" % flat)
"""
HEIGHTS = ("410", "430", "455", "455", "455", "440", "420", "400", "400", "415", "430")
AGAIN = """def step(x):
    mode = x * 10
    if x == 1:
        mode += 1  # CHANGE
        return mode
    if x == 2:
        return mode  # RESTORE
    return 0


def walk(items):
    total = 0
    for item in items:
        total += item  # ADD
        yield total  # GIVE


print(sum(step(x) for x in (1, 2)), list(walk([1, 2])))
"""


def line_of(text, marker):
    return next(i for i, line in enumerate(text.splitlines(), 1) if f"# {marker}" in line)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    return tmp_path


def probe(capsys, root, program, *argv, args=()):
    code = main(
        ["probe", str(root / program), *argv, "--json", "--quiet", "--no-save"]
        + [f"--arg={arg}" for arg in args]
    )
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(240)
@pytest.mark.parametrize("source", [TRAIL, TOP], ids=["function", "module"])
def test_the_guard_flip_skips_the_move_with_two_set_ats(state, capsys, source):
    (state / "trail.py").write_text(source)
    guard, flat = line_of(source, "GUARD"), line_of(source, "FLAT")
    at = ["--at", f"trail.py:{guard}"]
    code, plain = probe(capsys, state, "trail.py", *at, args=HEIGHTS)
    assert code == 0 and plain["output"].strip() == "TRAIL:T@400 455,B@800 400;FLAT=3", plain
    code, result = probe(
        capsys, state, "trail.py", *at, "--capture", "trend", "--hits", "all",
        "--set-at", f"{guard}@all", "trend=0", "--set-at", f"{flat}@all", "trend=@before",
        args=HEIGHTS,
    )  # fmt: skip
    assert code == 0, result
    assert result["output"].strip() == "TRAIL:T@200 455,B@700 400;FLAT=3", result["output"]
    restored = [(c["before"], c["after"], c["restores"]) for c in result["interventions"][1::2]]
    assert restored == [
        ("0", "1", f"{guard}@1"),
        ("0", "1", f"{guard}@2"),
        ("0", "-1", f"{guard}@3"),
    ]
    text = _table(result)
    assert (
        f"set trend: 0 -> -1  (restored, --set-at line {flat}@all, stop 3 of line {flat}, in "
        in text
    ), text


@pytest.mark.timeout(240)
def test_another_call_or_a_generator_is_refused(state, capsys):
    (state / "again.py").write_text(AGAIN)
    change, restore = line_of(AGAIN, "CHANGE"), line_of(AGAIN, "RESTORE")
    code, result = probe(
        capsys, state, "again.py", "--at", f"again.py:{change}", "--set-at", f"{change}@1",
        "mode=5", "--set-at", f"{restore}@1", "mode=@before",
    )  # fmt: skip
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED", result
    assert "and the first line of step ran again since" in result["error"]["message"], result
    add, give = line_of(AGAIN, "ADD"), line_of(AGAIN, "GIVE")
    code, result = probe(
        capsys, state, "again.py", "--at", f"again.py:{add}", "--set-at", f"{add}@1",
        "total=5", "--set-at", f"{give}@1", "total=@before",
    )  # fmt: skip
    # Refused before the run: nothing ran.
    assert code == 2 and result["error"]["code"] == "MUTATION_REJECTED", result
    assert f"line {give} is in `walk`, a generator or coroutine" in result["error"]["message"]


@pytest.mark.timeout(240)
def test_until_hits_waits_for_the_restore_of_its_last_change(state, capsys):
    # With `--hits 2 --until hits`, the probe ended after the guard's
    # second change, before the restore that undoes it.
    (state / "trail.py").write_text(TRAIL)
    guard, flat = line_of(TRAIL, "GUARD"), line_of(TRAIL, "FLAT")
    code, result = probe(
        capsys, state, "trail.py", "--at", f"trail.py:{guard}", "--hits", "2", "--until", "hits",
        "--set-at", f"{guard}@all", "trend=0", "--set-at", f"{flat}@all", "trend=@before",
        args=HEIGHTS,
    )  # fmt: skip
    assert code == 0 and result["ended"] == "hits", result
    assert [(c["at"], c.get("restores")) for c in result["interventions"]] == [
        (f"{guard}@1", None),
        (f"{flat}@1", f"{guard}@1"),
        (f"{guard}@2", None),
        (f"{flat}@2", f"{guard}@2"),
    ]
    assert "unapplied" not in result


@pytest.mark.timeout(240)
def test_a_restore_line_that_never_runs_names_each_write_it_never_restored(state, capsys):
    # The guard is flipped at each hit and restored at the line it guards, which then never
    # runs. None of the writes is restored, and the run names each, not one change left in
    # place, the latest. Hit 2's writes the 0 already there, a write of the same text (Python
    # doesn't prove an int unchanged).
    (state / "trail.py").write_text(TRAIL)
    guard, move = line_of(TRAIL, "GUARD"), line_of(TRAIL, "MOVE")
    code, result = probe(
        capsys, state, "trail.py", "--at", f"trail.py:{guard}", "--capture", "trend",
        "--hits", "all", "--set-at", f"{guard}@all", "trend=0", "--set-at", f"{move}@all",
        "trend=@before", args=HEIGHTS,
    )  # fmt: skip
    assert code == 4 and result["status"] == "completed", result
    text = _table(result)
    assert (
        "status: completed (1 requested change not made; 3 writes had no matching restore: 2 "
        "changes and 1 with the same text, not proven unchanged "
        f"(line {guard}, hits 1 to 3))"
    ) in text, text
    # trend stays 0 from the first flat point on, so no top or bottom is found.
    assert result["output"].strip() == "TRAIL:;FLAT=3", result["output"]
    assert [(c["at"], c["before"], c["after"]) for c in result["interventions"]] == [
        (f"{guard}@1", "1", "0"),
        (f"{guard}@2", "0", "0"),
        (f"{guard}@3", "-1", "0"),
    ]
    ats = [f"{guard}@1", f"{guard}@2", f"{guard}@3"]
    assert result["unapplied"] == [
        {
            "option": "--set-at",
            "at": f"{move}@all",
            "name": "trend",
            "value": "@before",
            "stops": 0,
            "unrestored": f"{guard}@3",
            "unrestored_all": ats,
            "unrestored_kinds": ["change", "alike", "change"],
        }
    ]
    assert result["set_outcomes"][1]["unrestored_all"] == ats
    assert any(
        f"made no restore for 3 writes (line {guard}, hits 1 to 3): 2 changes and 1 with the "
        f"same text, not proven unchanged. Line {move} stopped 0 time(s)" in w
        for w in result["warnings"]
    ), result["warnings"]
