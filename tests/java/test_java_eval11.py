"""The eleventh agent evaluation's Lows in a Java session (#306, seats 2 and 6): a step
onto the breakpoint's line is not a hit, and java-debug's next `continue` runs on to the
line's next pass, as the state's note says; `session report` counts hits and steps."""

import json
import os
from pathlib import Path

import pytest

from peeky.cli import main

LOOP = """package demo;

public class Loop {
    public static void main(String[] args) {
        int total = 0;
        for (int i = 0; i < 4; i++) {
            total += i; // LOOP
        }
        System.out.println("TOTAL:" + total);
    }
}
"""


@pytest.fixture
def loop(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "loop"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "Loop.java").write_text(LOOP)
    config = root / "loop.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["demo/Loop.java"],
                "main_class": "demo.Loop",
                "arguments": [],
            }
        )
    )
    return root, config


def line_of(marker):
    return next(i for i, text in enumerate(LOOP.splitlines(), 1) if f"// {marker}" in text)


def run(capsys, *argv):
    assert main([*argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.timeout(120)
def test_a_step_onto_the_breakpoint_line_is_not_a_hit_and_its_pass_gets_none(loop, capsys):
    root, config = loop
    at = line_of("LOOP")
    start = ["session", "start", str(root), "--config", str(config), "--at", f"demo/Loop.java:{at}"]
    assert main(start) == 0
    capsys.readouterr()
    try:
        for _ in range(4):
            state = run(capsys, "session", "step")
            if state["location"].endswith(f":{at}"):
                break
        assert state["location"].endswith(f":{at}") and state["reason"] == "step", state
        assert "hit" not in state, state
        assert state["note"].startswith(
            "not a hit: java-debug reports this stop as the step's, and `session continue` "
            "runs on to the line's next pass"
        ), state
        (row,) = run(capsys, "session", "get", "i")
        assert row["value"] == "1", row
        # The pass with i == 1 gets no number: hit 2 is the next pass's.
        state = run(capsys, "session", "continue")
        assert state["location"].endswith(f":{at}") and state["hit"] == 2, state
        (row,) = run(capsys, "session", "get", "i")
        assert row["value"] == "2", row
        report = run(capsys, "session", "report")
        assert (report["hits"], report["steps"], report["hit"]) == (2, 2, 2), report
    finally:
        main(["session", "close"])
