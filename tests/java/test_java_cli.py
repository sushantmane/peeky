"""The CLI on a launched Java program: fields through this; commands carry --config."""

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
def test_java_fields_are_read_through_this_and_commands_keep_the_config(counter, capsys):
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
            "this.total,this.step,total,i",
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
    assert (1, "this.total", "0") in values and (1, "this.step", "3") in values
    assert (2, "this.total", "3") in values and (2, "i", "1") in values
    # A bare field name is never read in place of a local; the error names the selector.
    bare = next(row for row in result["rows"] if row["selector"] == "total")
    assert "value" not in bare and "select it as this.total" in bare["error"]
    try:
        assert main(["session", "start", *common, "--at", "demo/Counter.java:11"]) == 0
        assert "in Counter.add(int)" in capsys.readouterr().out
    finally:
        main(["session", "close"])


THREADS = """package demo;
import java.util.concurrent.CountDownLatch;
public class Threads {
  static final CountDownLatch READY = new CountDownLatch(2);
  public static void main(String[] args) throws Exception {
    Thread a = new Thread(() -> work(1));
    Thread b = new Thread(() -> work(2));
    a.start(); b.start(); a.join(); b.join();
    System.out.println("DONE");
  }
  static void work(int id) {
    READY.countDown();
    try { READY.await(); } catch (InterruptedException e) { throw new RuntimeException(e); }
    int x = id * 10;
    System.out.println("W" + id + "=" + (x + id));
  }
}
"""


@pytest.mark.timeout(120)
def test_threads_stopped_separately_at_a_to_line_are_never_numbered(counter, capsys):
    root, config = counter
    (root / "demo" / "Threads.java").write_text(THREADS)
    data = json.loads(config.read_text())
    config.write_text(
        json.dumps({**data, "sources": ["demo/Threads.java"], "main_class": "demo.Threads"})
    )
    common = [str(root), "--config", str(config), "--json"]
    states = []
    try:
        assert main(["session", "start", *common, "--at", "demo/Threads.java:6"]) == 0
        states.append(json.loads(capsys.readouterr().out))
        assert main(["session", "continue", "--to", "14", "--json"]) == 0
        states.append(json.loads(capsys.readouterr().out))
        while states[-1]["state"] == "stopped" and len(states) < 6:
            assert main(["session", "continue", "--json"]) == 0
            states.append(json.loads(capsys.readouterr().out))
    finally:
        main(["session", "close"])
    stops = [(s["location"].rsplit(":", 1)[-1], s.get("hit")) for s in states if s.get("location")]
    # Line 6 is the session's breakpoint; both threads' stops at the --to line 14 are not.
    assert stops[0] == ("6", 1) and all(hit is None for line, hit in stops[1:]), stops
    assert [line for line, _ in stops[1:]] == ["14", "14"], stops
    assert states[-1]["state"] == "terminated"
