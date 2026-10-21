"""Java loop headers (#161), on a real JVM: java-debug binds a line breakpoint to the
line's first bytecode in the first method holding it. A counted `for` and a for-each
start with their initialization, which runs once before the loop, so they stop only
there and say so; a `while`, a `for` with no initialization and a `do`'s `while` start
with the condition the loop returns to, so they stop at every pass, with no note."""

import json
import os
from pathlib import Path

import pytest

from peeky.cli import main

LOOPS = """package demo;

public class Loops {
    public static void main(String[] args) {
        int total = 0;
        for (int i = 0; i < 3; i++) { // FOR
            total += i;
        }
        int k = 0;
        while (k < 3) { // WHILE
            k++;
        }
        do {
            total++;
        } while (total < 10); // DO
        for (String a : args) { // FOR_EACH
            total += a.length();
        }
        int j = 0;
        for (; j < 2; j++) { // FOR_NO_INIT
            total += j;
        }
        System.out.println("TOTAL:" + total + " " + k);
    }
}
"""


@pytest.fixture
def loops(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "loops"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "Loops.java").write_text(LOOPS)
    config = root / "loops.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["demo/Loops.java"],
                "main_class": "demo.Loops",
                "arguments": ["a", "bb"],
            }
        )
    )
    return root, config


@pytest.mark.timeout(300)
def test_java_loop_headers_say_when_they_stop_only_before_their_loop(loops, capsys):
    root, config = loops

    def run(marker):
        line = next(i for i, text in enumerate(LOOPS.splitlines(), 1) if f"// {marker}" in text)
        command = ["probe", str(root), "--config", str(config), "--at", f"demo/Loops.java:{line}"]
        assert main([*command, "--capture", "total", "--hits", "all", "--json", "--quiet"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "completed" and "TOTAL:14 3" in result["output"], result
        return line, result["hits"], [w for w in result["warnings"] if "loop header" in w]

    for marker in ("FOR", "FOR_EACH"):
        line, hits, notes = run(marker)
        assert (hits, notes) == (
            1,
            [
                f"Line {line} is a loop header: it stops once each time its loop starts, "
                "before the first pass, never at the passes; put --at (or --set-at) on the "
                "first line of the loop's body for one stop per pass"
            ],
        ), marker
    for marker, passes in (("WHILE", 4), ("DO", 7), ("FOR_NO_INIT", 3)):
        line, hits, notes = run(marker)
        assert (hits, notes) == (passes, []), marker
