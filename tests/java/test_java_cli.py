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
    # #91 (eval 4): Java lists each method's lines and the names it sees, as C and Rust do.
    assert main(["describe", *common, "--json"]) == 0
    scopes = {s["scope"]: s for s in json.loads(capsys.readouterr().out)["scopes"]}
    assert scopes["Counter.add"]["names"] == ["this", "times", "i"]
    assert scopes["Counter.add"]["source"] == "demo/Counter.java"
    assert scopes["Counter.add"]["lines"] == [10, 13]
    assert scopes["Counter.main"]["names"] == ["args", "counter"]
    # #264: a local declared only in a block gives the block's lines, as for C and Rust.
    assert scopes["Counter.add"]["spans"] == {"i": [[11, 11]]}
    assert "spans" not in scopes["Counter.main"]
    assert main(["describe", *common]) == 0
    # #183: with each name's declared type, as C, C++ and Rust give them; #264: its lines.
    assert (
        "names in Counter.add (demo/Counter.java:10-13): this (demo.Counter), times (int), "
        "i (int, line 11)" in capsys.readouterr().out
    )
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


LAMBDAS = """package demo;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.Stream;
public class Lambdas {
  static final List<Integer> LOW = Stream.of(1, 2, 3).filter(x -> x < 3).collect(Collectors.toList());
  public static void main(String[] args) {
    List<Integer> stock = List.of(5, 2, 9);
    List<Integer> low = stock.stream().filter(s -> s < 6).collect(Collectors.toList());
    List<Integer> doubled = stock.stream().map(s -> {
      return s * 2;
    }).collect(Collectors.toList());
    stock.forEach(a ->
        List.of(1, 2).forEach(b -> System.out.print("")));
    System.out.println("LOW:" + low + " DOUBLED:" + doubled + " " + LOW);
  }
}
"""


@pytest.mark.timeout(180)
def test_a_shared_line_warns_which_method_stops_and_which_never_does(counter, capsys):
    root, config = counter
    (root / "demo" / "Lambdas.java").write_text(LAMBDAS)
    data = json.loads(config.read_text())
    config.write_text(
        json.dumps({**data, "sources": ["demo/Lambdas.java"], "main_class": "demo.Lambdas"})
    )
    common = [str(root), "--config", str(config)]
    probe = ["probe", *common, "--hits", "all", "--json", "--no-save", "--quiet"]

    def run(line):
        assert main([*probe, "--at", f"demo/Lambdas.java:{line}"]) == 0
        result = json.loads(capsys.readouterr().out)
        notes = [w for w in result["warnings"] if "runs in several methods" in w]
        functions = {row["function"] for row in result["rows"]} if result["rows"] else set()
        return result["hits"], notes, functions

    # main's line: main stops once; the filter lambda's three calls never do.
    hits, [note], _ = run(9)
    assert hits == 1 and note.startswith(
        "Line 9 runs in several methods; the debugger stops only in main there, never in "
        "lambda$main$"
    )
    # A static initializer's line: its lambda stops (javac lists it first), never <clinit>.
    hits, [note], _ = run(6)
    assert hits == 3 and "stops only in lambda$static$" in note
    assert "never in the static initializer" in note
    # Nested lambdas: the outer one stops; the inner one never does.
    hits, [note], _ = run(14)
    assert hits == 3 and note.count("lambda$main$") == 2
    # A line of a lambda's own: it stops, with no warning.
    hits, notes, _ = run(11)
    assert hits == 3 and not notes
    try:
        assert main(["session", "start", *common, "--at", "demo/Lambdas.java:9"]) == 0
        assert "warning: Line 9 runs in several methods" in capsys.readouterr().out
    finally:
        main(["session", "close"])


@pytest.mark.timeout(120)
def test_rows_say_which_java_locals_set_can_change(counter, capsys):
    # #77 (eval 3, Java): which locals take --set was only in the docs.
    root, config = counter
    common = ["probe", str(root), "--config", str(config), "--at", "demo/Counter.java:17"]
    code = main([*common, "--capture", "counter,args", "--json", "--no-save", "--quiet"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert {row["selector"]: row["settable"] for row in result["rows"]} == {
        "counter": False,
        "args": False,
    }
    code = main([*common, "--set", "args=1", "--json", "--no-save", "--quiet"])
    result = json.loads(capsys.readouterr().out)
    # #183: the declared type rules it out before the program runs.
    assert code == 2 and result["error"]["code"] == "MUTATION_REJECTED"
    assert result.get("refused_before_launch") is True, result
    assert (
        "`args` is declared String[]; only byte, short, int, long, char"
        in (result["error"]["message"])
    )
    at = ["probe", str(root), "--config", str(config), "--at", "demo/Counter.java:11"]
    code = main([*at, "--capture", "i,times", "--json", "--no-save", "--quiet"])
    result = json.loads(capsys.readouterr().out)
    assert {row["selector"]: row["settable"] for row in result["rows"]} == {
        "i": True,
        "times": True,
    }
