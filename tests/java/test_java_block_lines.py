"""Java `describe` block lines are the lines whose breakpoint sees the local (#264 review),
through javac's real class files and real probes: java-debug binds a line breakpoint to
the line's first instruction, so a line counts only where that instruction is in the
local's scope."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from peeky.cli import main
from peeky.java_metadata import _class_metadata

LINES = """package demo;

public class Lines {
    static int oneLine(int n) {
        { int x = n + 1; System.out.println("x " + x); }
        return n;
    }

    static int loops(int[] values) {
        int total = 0;
        for (int i = 0; i < 2; i++) { total += i; }
        for (int j = 0; j < values.length; j++) {
            total += values[j];
        }
        for (int v : values) {
            total += v;
        }
        return total;
    }

    static int pattern(Object o) {
        if (o instanceof String s) {
            return s.length();
        }
        return 0;
    }

    static int shadows(boolean flag) {
        if (flag) {
            int value = 1;
            System.out.println("value " + value);
        }
        String value = "later";
        return value.length();
    }

    static int again(boolean flag) {
        if (flag) {
            int count = 1;
            System.out.println("count " + count);
        }
        int count = 2;
        return count;
    }

    public static void main(String[] args) {
        int sum = oneLine(1) + loops(new int[] {3, 4}) + pattern("ab") + shadows(true);
        System.out.println("LINES " + (sum + again(true)));
    }
}
"""


def scopes(classes):
    methods = []
    _class_metadata((Path(classes) / "demo/Lines.class").read_bytes(), scopes=methods)
    return {each[0]: each[5] for each in methods}


def test_block_lines_are_the_lines_whose_breakpoint_sees_the_local(tmp_path):
    javac = shutil.which("javac")
    if not javac:
        pytest.skip("javac is not installed")
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo/Lines.java").write_text(LINES)
    subprocess.run(
        [javac, "-g", "-proc:none", "-d", "out", "demo/Lines.java"], cwd=tmp_path, check=True
    )
    found = scopes(tmp_path / "out")
    # A one-line block, and a one-line `for`: no line's first instruction is in scope.
    assert found["oneLine"]["spans"] == {"x": []}
    # A `for` header has two entries in the LineNumberTable (its start, and the update);
    # its breakpoint binds to the first, before the loop's variable is in scope. So does
    # an enhanced `for`'s, and an `if` whose pattern binds `s`.
    assert found["loops"]["spans"] == {"i": [], "j": [[13, 13]], "v": [[16, 16]]}
    assert found["pattern"]["spans"] == {"s": [[23, 23]]}
    # A block's `int value`, then a `String value` to the method's end: each declaration
    # has its own lines, every type listed with its own.
    assert found["shadows"]["spans"] == {"value": [[31, 31], [34, 34]]}
    assert found["shadows"]["typed_spans"] == {
        "value": [{"type": "int", "spans": [[31, 31]]}, {"type": "String", "spans": [[34, 34]]}]
    }
    # The same with one type: one name, both declarations' lines.
    assert found["again"]["spans"] == {"count": [[40, 40], [43, 43]]}
    assert "typed_spans" not in found["again"]
    # One declaration to the method's end is the method's own: no lines.
    assert "spans" not in found["main"] and "total" not in found["loops"]["spans"]


@pytest.fixture
def lines(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "lines"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "Lines.java").write_text(LINES)
    config = root / "lines.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["demo/Lines.java"],
                "main_class": "demo.Lines",
            }
        )
    )
    return root, config


# (line, local, what a probe there reads: its type, or None for not visible).
PROBES = [
    (5, "x", None),
    (11, "i", None),
    (12, "j", None),
    (13, "j", "int"),
    (15, "v", None),
    (16, "v", "int"),
    (22, "s", None),
    (23, "s", "String"),
    (31, "value", "int"),
    (33, "value", None),
    (34, "value", "String"),
    (40, "count", "int"),
    (42, "count", None),
    (43, "count", "int"),
]


@pytest.mark.timeout(600)
def test_describe_lists_a_line_exactly_where_a_probe_there_sees_the_local(lines, capsys):
    root, config = lines
    common = [str(root), "--config", str(config)]
    assert main(["describe", *common, "--json"]) == 0
    described = {
        scope["scope"].split(".", 1)[1]: scope
        for scope in json.loads(capsys.readouterr().out)["scopes"]
    }

    def listed(name, line):
        """The type `describe` gives ``name`` on ``line``, or None where it lists no such
        line for it."""
        scope = next(
            s
            for s in described.values()
            if name in s.get("spans", {}) and s["lines"][0] <= line <= s["lines"][1]
        )
        entries = scope.get("typed_spans", {}).get(name) or [
            {"type": scope["types"][name], "spans": scope["spans"][name]}
        ]
        return next(
            (e["type"] for e in entries if any(a <= line <= b for a, b in e["spans"])), None
        )

    for line, name, kind in PROBES:
        assert listed(name, line) == kind, (line, name)
        argv = ["probe", *common, "--at", f"demo/Lines.java:{line}", "--capture", name]
        assert (
            main([*argv, "--hits", "1", "--until", "hits", "--json", "--no-save", "--quiet"]) == 0
        )
        [row] = [r for r in json.loads(capsys.readouterr().out)["rows"] if r["phase"] == "observed"]
        if kind is None:
            assert "value" not in row and "not found" in row["error"], (line, row)
        else:
            assert row.get("type") == kind and "error" not in row, (line, row)
    assert main(["describe", *common]) == 0
    text = capsys.readouterr().out
    assert (
        "names in Lines.oneLine (demo/Lines.java:5-6): n (int), x (int, no breakpoint line)" in text
    )
    assert "value (int, line 31), value (String, line 34)" in text, text
    assert "count (int, lines 40, 43)" in text, text
