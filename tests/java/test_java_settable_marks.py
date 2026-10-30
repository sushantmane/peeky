"""A Java row is marked [settable] only where `set` would change it, and says why not
elsewhere, through real java-debug: an array a JDK class's field holds under
another name (a list an unmodifiable view wraps), an array more objects refer to than
the launcher reads, a local the program never reads again, a constant, a final field and
a caller's frame. A refusal names the whole selector, and its hint promises nothing a
row's mark said."""

import json
import os
import time
from pathlib import Path

import pytest

from peeky.cli import main

MARKS = """package vote;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public class Marks {
    static final class Cell {
        final int[] cells;

        Cell(int[] cells) {
            this.cells = cells;
        }
    }

    final int limit = 7;
    int total = 1;
    int[] shared = {4};
    final List<Cell> cells = new ArrayList<>();

    int count(List<int[]> ballots, int[] choice) {
        final int base = 5;
        Object label = "x";
        int counted = 0;
        for (int b = 0; b < ballots.size(); b++) {
            int[] ranking = ballots.get(b);
            counted += ranking[choice[b]];
        }
        int used = counted + base;
        System.out.println("used " + used + " " + label);
        for (int b = 0; b < ballots.size(); b++) {
            int[] ranking = ballots.get(b);
            choice[b] += ranking.length; // STOP
        }
        return total + limit + shared[0] + cells.size();
    }

    public static void main(String[] args) {
        Marks marks = new Marks();
        // More objects refer to `shared` than the launcher reads (64).
        for (int i = 0; i < 70; i++) {
            marks.cells.add(new Cell(marks.shared));
        }
        List<int[]> ballots = new ArrayList<>();
        ballots.add(new int[] {1, 2});
        ballots.add(new int[] {3});
        int[] choice = {0, 0};
        int rounds = 2;
        int result = marks.count(Collections.unmodifiableList(ballots), choice);
        System.out.println("AFTER " + result + " " + rounds + " " + choice[0] + " " + choice[1]
            + " " + ballots.get(0)[0]);
    }
}
"""


RUNNING = "other threads, the JVM's own included, are running; set checks again"


@pytest.fixture
def marks(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "marks"
    (root / "vote").mkdir(parents=True)
    (root / "vote" / "Marks.java").write_text(MARKS)
    config = root / "marks.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["vote/Marks.java"],
                "main_class": "vote.Marks",
            }
        )
    )
    line = next(i for i, text in enumerate(MARKS.splitlines(), 1) if "STOP" in text)
    return root, config, f"vote/Marks.java:{line}"


def get(capsys, *selectors):
    assert main(["session", "get", *selectors, "--json"]) == 0, capsys.readouterr().err
    return json.loads(capsys.readouterr().out)


def rows(capsys, *frame):
    assert main(["session", "inspect", *frame, "--json"]) == 0, capsys.readouterr().err
    snapshot = json.loads(capsys.readouterr().out)
    return {row["name"]: row for scope in snapshot["scopes"] for row in scope["variables"]}


@pytest.mark.timeout(300)
def test_a_settable_mark_promises_only_what_set_does(marks, capsys):
    root, config, at = marks
    try:
        assert main(["session", "start", str(root), "--config", str(config), "--at", at]) == 0
        capsys.readouterr()
        # `ranking` is in a list an unmodifiable view (a JDK class's field) wraps: the
        # launcher refuses a write, so no element is marked, and each row says why.
        ranking = get(capsys, "ranking")[0]
        for child in ranking["children"]:
            assert child["settable"] is False, child
            assert child["not_settable"] == (
                "its array is in a list held by java.util.Collections$UnmodifiableList.list "
                "(the JDK's own state)"
            ), child
        (element,) = get(capsys, "ranking[1]")
        assert element["settable"] is False and "UnmodifiableList" in element["not_settable"]
        # The refusal names the whole selector, and its hint promises nothing of a mark.
        assert main(["session", "set", "ranking[1]=9"]) == 2
        error = capsys.readouterr().err
        assert "[MUTATION_REJECTED] The debugger refused to change `ranking[1]`" in error, error
        assert "held by java.util.Collections$UnmodifiableList.list" in error, error
        assert "change '1'" not in error and "can be changed here" not in error, error
        assert "which no [settable] mark can promise" in error, error
        # More than 64 objects refer to `shared`: the launcher cannot tell who holds it,
        # so the row is not marked, and the write is refused for the same reason.
        (shared,) = get(capsys, "this.shared[0]")
        assert shared["settable"] is False, shared
        assert "more than 64 objects refer to the array" in shared["not_settable"], shared
        assert main(["session", "set", "this.shared[0]=9"]) == 2
        error = capsys.readouterr().err
        assert "`this.shared[0]`" in error and "more than 64 objects refer" in error, error
        # The program's own array changes. A line breakpoint stops only its own thread,
        # and the JVM's other threads run on, so no element is promised.
        for child in get(capsys, "choice")[0]["children"]:
            assert child["settable"] is False and child["maybe_settable"] == RUNNING, child
        assert main(["session", "set", "choice[1]=1"]) == 0
        assert "set choice[1]: 0 -> 1" in capsys.readouterr().out
        # Locals and fields that are not marked say why.
        listed = rows(capsys)
        for name in ("counted", "used"):
            assert listed[name]["settable"] is False, listed[name]
            assert listed[name]["not_settable"] == "not read again after this stop", listed
        assert listed["base"]["not_settable"] == "a constant javac compiles into its uses"
        assert listed["label"]["not_settable"] == "declared Object, not String", listed
        assert listed["b"]["settable"] is True and "not_settable" not in listed["b"]
        fields = {child["name"]: child for child in get(capsys, "this")[0]["children"]}
        assert fields["limit"]["settable"] is False and fields["limit"]["not_settable"] == "final"
        assert fields["total"]["settable"] is True and "not_settable" not in fields["total"]
        # A caller's frame changes nothing: its rows say so.
        caller = rows(capsys, "--frame", "1")
        assert caller["rounds"]["not_settable"] == "in a caller's frame", caller["rounds"]
        assert main(["session", "inspect"]) == 0
        text = capsys.readouterr().out
        assert "counted: int = 4  [not settable: not read again after this stop]" in text, text
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        output = json.loads(capsys.readouterr().out)["output"]
        assert "AFTER 82 2 2 2 1" in output, output
    finally:
        main(["session", "close"])


@pytest.mark.timeout(180)
def test_a_probe_row_marks_a_jdk_held_element_as_its_set_is_refused(marks, capsys):
    root, config, at = marks
    argv = ["probe", str(root), "--config", str(config), "--at", at, "--hits", "1"]
    argv += ["--capture", "ranking[0],choice[0],counted", "--json", "--no-save", "--quiet"]
    assert main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    observed = {row["selector"]: row for row in result["rows"] if row["phase"] == "observed"}
    assert observed["ranking[0]"]["settable"] is False, observed
    assert "UnmodifiableList" in observed["ranking[0]"]["not_settable"], observed
    assert observed["choice[0]"]["maybe_settable"] == RUNNING, observed
    assert observed["counted"]["not_settable"] == "not read again after this stop", observed
    assert main([*argv[:-3], "--set", "ranking[0]=9", "--json", "--no-save", "--quiet"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "MUTATION_REJECTED", result["error"]
    assert "The debugger refused to change `ranking[0]`" in result["error"]["message"]
    assert "can be changed here" not in result["error"]["hint"], result["error"]


RACING = """package vote;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public class Racing {
    static final class Gate {
        volatile boolean open;
        volatile boolean done;
    }

    static final class Wrapper extends Thread {
        final Gate gate;
        final int[] cells;

        Wrapper(Gate gate, int[] cells) {
            this.gate = gate;
            this.cells = cells;
        }

        @Override
        public void run() {
            try {
                while (!gate.open) {
                    Thread.sleep(5);
                }
                // Long enough for a read at the stop to come first.
                Thread.sleep(3000);
            } catch (InterruptedException stopped) {
                return;
            }
            List<int[]> list = new ArrayList<>();
            list.add(cells);
            kept = Collections.unmodifiableList(list);
            gate.done = true;
        }
    }

    static List<int[]> kept;

    public static void main(String[] args) throws Exception {
        int[] target = {1, 2};
        int[] own = {7};
        Gate gate = new Gate();
        Wrapper wrapper = new Wrapper(gate, target);
        wrapper.setDaemon(true);
        wrapper.start();
        System.out.println("before " + target[0] + own[0] + gate.open); // STOP
        wrapper.join();
        System.out.println("AFTER " + target[0] + " " + own[0] + " " + kept.size());
    }
}
"""


@pytest.mark.timeout(300)
def test_another_thread_can_make_a_jdk_class_hold_the_array_after_a_read(
    tmp_path, monkeypatch, capsys
):
    # The verdict was kept for the whole stop, but a line breakpoint stops
    # only its own thread. Here a background thread wraps a list holding `target` in an
    # unmodifiable view after the first read: `get` said settable, and `set` was refused.
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "racing"
    (root / "vote").mkdir(parents=True)
    (root / "vote" / "Racing.java").write_text(RACING)
    config = root / "racing.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["vote/Racing.java"],
                "main_class": "vote.Racing",
            }
        )
    )
    line = next(i for i, text in enumerate(RACING.splitlines(), 1) if "STOP" in text)
    at = f"vote/Racing.java:{line}"
    try:
        assert main(["session", "start", str(root), "--config", str(config), "--at", at]) == 0
        capsys.readouterr()
        # Only app objects hold `target` yet, but the wrapper thread runs: not promised.
        for child in get(capsys, "target")[0]["children"]:
            assert child["settable"] is False and child["maybe_settable"] == RUNNING, child
        # A write checks and writes with every thread suspended, then lets them run on.
        assert main(["session", "set", "own[0]=8"]) == 0
        assert "set own[0]: 7 -> 8" in capsys.readouterr().out
        # Let the wrapper thread wrap the list, 3 s later. A read before then is not
        # promised either; with no change in between, a kept verdict would outlive it.
        assert main(["session", "set", "gate.open=true"]) == 0
        capsys.readouterr()
        target, done = get(capsys, "target", "gate.done")
        if done["value"] == "false":
            for child in target["children"]:
                assert child["maybe_settable"] == RUNNING, child
        deadline = time.monotonic() + 30
        while get(capsys, "gate.done")[0]["value"] != "true":
            assert time.monotonic() < deadline, "the wrapper thread never ran"
            time.sleep(0.2)
        # Read again at the same stop: what the other thread did is seen, never a kept
        # verdict, and the row and the refusal agree.
        for child in get(capsys, "target")[0]["children"]:
            assert child["settable"] is False and "maybe_settable" not in child, child
            assert child["not_settable"].startswith(
                "its array is in a list held by java.util.Collections$Unmodifiable"
            ), child
        assert main(["session", "set", "target[0]=9"]) == 2
        error = capsys.readouterr().err
        assert "The debugger refused to change `target[0]`" in error, error
        assert "in a list held by java.util.Collections$Unmodifiable" in error, error
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        output = json.loads(capsys.readouterr().out)["output"]
        assert "AFTER 1 8 1" in output, output
    finally:
        main(["session", "close"])
