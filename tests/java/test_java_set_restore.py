"""`--set-at LINE@HIT name=@before` on a launched JVM (#305): the guard flip that skips a
statement in two `--set-at`s; a `double`, which java-debug may show rounded, is not written
back."""

import json
import os
from pathlib import Path

import pytest

from peeky.cli import main
from peeky.probe import _table

TRAIL = """package demo;

public class Trail {
    static String findTurns(int[] height) {
        StringBuilder turns = new StringBuilder();
        int turn = 0; // the point where the height last moved
        int trend = 0; // +1 rising, -1 falling, 0 not known yet
        int flat = 0; // points level with the one before
        for (int i = 1; i < height.length; i++) {
            if (height[i] == height[i - 1]) {
                if (trend != 0) { // GUARD
                    turn = i;
                }
                flat++; // FLAT
                continue;
            }
            int now = height[i] > height[i - 1] ? 1 : -1;
            if (now != trend && trend != 0) {
                turns.append(turns.length() > 0 ? "," : "").append(trend == 1 ? 'T' : 'B');
                turns.append('@').append(turn * 100).append(' ').append(height[turn]);
            }
            trend = now;
            turn = i;
        }
        return turns + ";FLAT=" + flat;
    }

    static double mean(int[] height) {
        double sum = 0;
        for (int h : height) {
            sum += h; // ADD
        }
        return sum / height.length;
    }

    public static void main(String[] args) {
        int[] height = new int[args.length];
        for (int i = 0; i < args.length; i++) {
            height[i] = Integer.parseInt(args[i]);
        }
        System.out.println("TRAIL:" + findTurns(height) + ";MEAN=" + mean(height));
    }
}
"""
# `b` refers to another Box after line RENEW: its `n` is another field.
SWAP = """package demo;

public class Swap {
    static class Box {
        int n;

        Box(int n) {
            this.n = n;
        }
    }

    static int swap() {
        Box b = new Box(1);
        b.n += 1; // CHANGE
        b.n += 10; // SAME
        b = new Box(9); // RENEW
        b.n += 1; // RESTORE
        return b.n;
    }

    public static void main(String[] args) {
        System.out.println("SWAP:" + swap());
    }
}
"""
HEIGHTS = ("410", "430", "455", "455", "455", "440", "420", "400", "400", "415", "430")


@pytest.fixture
def trail(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "trail"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "Trail.java").write_text(TRAIL)
    config = root / "trail.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["demo/Trail.java"],
                "main_class": "demo.Trail",
                "arguments": [],
            }
        )
    )
    return root, config


def line_of(marker):
    return next(i for i, text in enumerate(TRAIL.splitlines(), 1) if f"// {marker}" in text)


def probe(capsys, root, config, *argv):
    command = ["probe", str(root), "--config", str(config), *argv, "--json", "--no-save"]
    code = main([*command, "--quiet", *(f"--arg={arg}" for arg in HEIGHTS)])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(300)
def test_the_guard_flip_skips_the_move_with_two_set_ats(trail, capsys):
    root, config = trail
    guard, flat = line_of("GUARD"), line_of("FLAT")
    at = ["--at", f"demo/Trail.java:{guard}"]
    code, plain = probe(capsys, root, config, *at)
    assert code == 0, plain
    assert plain["output"].startswith("TRAIL:T@400 455,B@800 400;FLAT=3;"), plain["output"]
    code, result = probe(
        capsys, root, config, *at, "--capture", "trend", "--hits", "all",
        "--set-at", f"{guard}@all", "trend=0", "--set-at", f"{flat}@all", "trend=@before",
    )  # fmt: skip
    assert code == 0, result
    assert result["output"].startswith("TRAIL:T@200 455,B@700 400;FLAT=3;"), result["output"]
    restored = [(c["before"], c["after"], c["restores"]) for c in result["interventions"][1::2]]
    assert restored == [
        ("0", "1", f"{guard}@1"),
        ("0", "1", f"{guard}@2"),
        ("0", "-1", f"{guard}@3"),
    ]
    assert f"(restored, --set-at line {flat}@all, stop 3 of line {flat}, in " in _table(result)


@pytest.mark.timeout(300)
def test_a_double_is_not_written_back(trail, capsys):
    root, config = trail
    add = line_of("ADD")
    # The change, then the restore, at one stop: the double's text may be rounded.
    code, result = probe(
        capsys, root, config, "--at", f"demo/Trail.java:{add}", "--set-at", f"{add}@1",
        "sum=1.5", "--set-at", f"{add}@1", "sum=@before",
    )  # fmt: skip
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED", result
    assert "it is a double, which java-debug may show rounded" in result["error"]["message"]


@pytest.mark.timeout(300)
def test_a_field_is_restored_only_in_the_object_its_change_wrote(trail, capsys):
    # Review of PR #322: `b.n=0`, then `b = new Box(9)`, then `b.n=@before` wrote 1 into
    # the new Box.
    root, config = trail
    (root / "demo" / "Swap.java").write_text(SWAP)
    config.write_text(
        json.dumps(
            {
                **json.loads(config.read_text()),
                "sources": ["demo/Swap.java"],
                "main_class": "demo.Swap",
            }
        )
    )
    lines = {
        marker: next(i for i, text in enumerate(SWAP.splitlines(), 1) if f"// {marker}" in text)
        for marker in ("CHANGE", "SAME", "RESTORE")
    }
    change = lines["CHANGE"]
    at = ["--at", f"demo/Swap.java:{change}", "--set-at", f"{change}@1", "b.n=0"]
    code, result = probe(
        capsys, root, config, *at, "--set-at", f"{lines['RESTORE']}@1", "b.n=@before"
    )
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED", result
    message = result["error"]["message"]
    assert "`b.n` cannot be changed back here: it is `n` of Swap$Box@" in message, message
    assert ", which the change was made to" in message
    # The same Box: written back, over the 1 the program made of the 0.
    code, result = probe(capsys, root, config, *at, "--set-at", f"{lines['SAME']}@1", "b.n=@before")
    assert code == 0, result
    restore = result["interventions"][1]
    assert (restore["before"], restore["after"], restore["overwrote"]) == ("1", "1", "1")
    assert result["output"].strip() == "SWAP:10", result["output"]
