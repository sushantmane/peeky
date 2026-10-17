"""The CLI on a launched Java program: fields read without this; commands carry --config."""

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


@pytest.mark.timeout(120)
def test_java_fields_read_without_this_and_commands_keep_the_config(counter, capsys):
    root, config = counter
    common = [str(root), "--config", str(config)]
    assert main(["describe", *common, "--json"]) == 0
    commands = json.loads(capsys.readouterr().out)["commands"]
    assert f"--config {config}" in commands["probe"] and f"--config {config}" in commands["session"]
    code = main(
        [
            "probe",
            *common,
            "--at",
            "demo/Counter.java:11",
            "--capture",
            "total,step,this.total,i",
            "--hits",
            "2",
            "--json",
            "--no-save",
            "--quiet",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    values = [(row["hit"], row["selector"], row.get("value")) for row in result["rows"]]
    assert (1, "total", "0") in values and (1, "step", "3") in values
    assert (2, "total", "3") in values and (2, "this.total", "3") in values
    try:
        assert main(["session", "start", *common, "--at", "demo/Counter.java:11"]) == 0
        assert "in Counter.add(int)" in capsys.readouterr().out
    finally:
        main(["session", "close"])
