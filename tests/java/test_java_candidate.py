"""Probing a candidate fix (`probe --old/--new`) on a launched Java program."""

import json
import os
from pathlib import Path

import pytest

from peeky.cli import main

COUNTER = """package demo;

public class Counter {
    private int total;
    private final int step;

    Counter(int step) { this.step = step; }

    void add(int times) {
        for (int i = 0; i < times; i++) {
            total += step;
        }
    }

    public static void main(String[] args) {
        Counter counter = new Counter(3);
        counter.add(2);
        System.out.println("TOTAL:" + counter.total);
    }
}
"""


@pytest.fixture
def counter(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "counter"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "Counter.java").write_text(COUNTER)
    config = root / "counter.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["demo/Counter.java"],
                "main_class": "demo.Counter",
                "arguments": [],
            }
        )
    )
    return root, config


@pytest.mark.timeout(180)
def test_a_java_candidate_is_compiled_and_probed_without_touching_the_source(counter, capsys):
    root, config = counter
    source = root / "demo/Counter.java"
    before = source.read_bytes()
    code = main(
        [
            "probe",
            str(root),
            "--config",
            str(config),
            "--at",
            "demo/Counter.java:11",
            "--capture",
            "this.step,this.total",
            "--hits",
            "2",
            "--old",
            "new Counter(3)",
            "--new",
            "new Counter(5)",
            "--json",
            "--quiet",
            "--no-save",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    steps = [row["value"] for row in result["rows"] if row["selector"] == "this.step"]
    assert steps == ["5", "5"] and "TOTAL:10" in result["output"]
    assert source.read_bytes() == before
