"""Java interventions with their real reasons (#183, eighth evaluation's `calc`): what
the class files' declared types rule out is refused before the program runs, a char
takes a quoted char or a code point, and `describe` gives each name's declared type."""

import json
import os
from pathlib import Path

import pytest

from peeky.cli import main

CALC = """package app;

public class Calc {
    private final String src;
    private int pos;

    Calc(String expression) {
        this.src = expression.replace(" ", "");
    }

    private boolean at(char c) {
        return pos < src.length() && src.charAt(pos) == c;
    }

    long parseExpr() {
        long left = parseTerm();
        if (at('+') || at('-')) {
            char op = src.charAt(pos++);
            long right = parseExpr();
            return op == '+' ? left + right : left - right; // OP
        }
        return left;
    }

    long parseTerm() {
        long left = parseFactor();
        while (at('*') || at('/')) {
            char op = src.charAt(pos++);
            long right = parseFactor();
            left = op == '*' ? left * right : left / right;
        }
        return left;
    }

    long parseFactor() {
        if (at('-')) {
            pos++;
            return -parseFactor();
        }
        int start = pos;
        while (pos < src.length() && Character.isDigit(src.charAt(pos))) {
            pos++;
        }
        return Long.parseLong(src.substring(start, pos));
    }

    public static void main(String[] args) {
        StringBuilder results = new StringBuilder();
        for (String expression : args) {
            if (results.length() > 0) {
                results.append(',');
            }
            results.append(new Calc(expression).parseExpr()); // APPEND
        }
        System.out.println("RESULTS:" + results);
    }
}
"""


def line_of(marker):
    return next(i for i, text in enumerate(CALC.splitlines(), 1) if f"// {marker}" in text)


@pytest.fixture
def calc(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "calc"
    (root / "app").mkdir(parents=True)
    (root / "app" / "Calc.java").write_text(CALC)
    config = root / "calc.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["app/Calc.java"],
                "main_class": "app.Calc",
                "arguments": ["10-4-3"],
            }
        )
    )
    # A bundle built before #183's launcher has no marker for char changes.
    launcher = Path(bundle) / "classes" / "PeekyJavaAdapter.class"
    return root, config, b"peekyCharSet" in launcher.read_bytes()


def probe(capsys, calc, line, *argv):
    root, config, _ = calc
    at = ["probe", str(root), "--config", str(config), "--at", f"app/Calc.java:{line}"]
    code = main([*at, "--capture", "op", *argv, "--json", "--no-save", "--quiet"])
    return code, json.loads(capsys.readouterr().out)


def refused_before_launch(code, result, *texts):
    assert code == 2 and result.get("refused_before_launch") is True, result
    assert result["hits"] == 0, result
    for text in texts:
        assert text in result["error"]["message"], result["error"]


@pytest.mark.timeout(300)
def test_what_the_declared_type_rules_out_is_refused_before_the_run(calc, capsys):
    # `left='+'` on a long, a StringBuilder, and a name no method there declares: each
    # ran to the stop before, then refused with "requires a numeric or boolean scalar
    # literal" and a hint about C, C++ and Rust.
    code, result = probe(capsys, calc, line_of("OP"), "--set", "left='+'")
    refused_before_launch(code, result, "requires a literal of left's type (long)")
    assert result["error"]["code"] == "MUTATION_REJECTED"
    code, result = probe(capsys, calc, line_of("APPEND"), "--set", "results=1")
    refused_before_launch(code, result, "`results` is declared StringBuilder; only byte")
    code, result = probe(capsys, calc, line_of("OP"), "--set", "results=1")
    refused_before_launch(code, result, "no method with code on line", "locals there: left")
    assert result["error"]["code"] == "NOT_FOUND"
    code, result = probe(capsys, calc, line_of("OP"), "--set", "op=70000")
    refused_before_launch(code, result, "a code point from 0 to 65535")
    # What fits is made: 10 - (4 - 3) at the inner `4 - 3` (hit 1) became 10 - (5 - 3).
    code, result = probe(capsys, calc, line_of("OP"), "--set", "left=5")
    assert code == 0 and "RESULTS:8" in result["output"], result


@pytest.mark.timeout(300)
def test_a_char_takes_a_quoted_char_or_a_code_point(calc, capsys):
    _, _, char_set = calc
    for value in ("'+'", "43"):
        code, result = probe(capsys, calc, line_of("OP"), "--set", f"op={value}")
        if char_set:
            assert code == 0, result.get("error")
            [change] = result["interventions"]
            assert (change["before"], change["after"]) == ("-", "+"), change
            # 10-4-3 parsed right-recursively: 10 - (4 - 3) at the inner `4 - 3` (hit 1)
            # became 10 - (4 + 3).
            assert "RESULTS:3" in result["output"], result
        else:
            # The real rule, not the literal's shape, and how to get char changes.
            assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED", result
            message = result["error"]["message"]
            assert message.startswith("op is char; only byte, short, int, long, boolean"), message
            assert "a char too with an adapter bundle built by this version" in message
            assert "Rebuild the Java adapter bundle" in result["error"]["hint"]


@pytest.mark.timeout(300)
def test_describe_gives_declared_types_and_the_settable_kinds(calc, capsys):
    root, config, _ = calc
    assert main(["describe", str(root), "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "names in Calc.parseExpr (app/Calc.java:" in out
    assert "): this (app.Calc), left (long), op (char), right (long)" in out
    assert "args (String[]), results (StringBuilder), expression (String)" in out
    [mutable] = [line for line in out.splitlines() if line.startswith("mutable:")]
    assert "of type byte, short, int, long, char, boolean, float or double" in mutable
    assert "a char takes a char in single quotes ('+') or a code point (43)" in mutable


OVER = """package app;

public class Over {
    static String f(String x) { return x.trim(); } static int f(int x) { return x + 1; } // STRING_FIRST
    static int g(int x) { return x + 1; } static String g(String x) { return x.trim(); } // INT_FIRST

    public static void main(String[] args) {
        System.out.println("F:" + f(" a ") + f(1) + " G:" + g(1) + g(" b "));
    }
}
"""


@pytest.mark.timeout(300)
def test_only_the_method_that_gets_the_breakpoint_judges_a_set(calc, capsys):
    # #192 review: java-debug binds a line two overloads share to the first in the class
    # file only. `f(int)` took `x=2`, so the probe ran, and `f(String)`, where it
    # stopped, refused it there.
    root, config, _ = calc
    (root / "app" / "Over.java").write_text(OVER)
    over = json.loads(config.read_text()) | {"sources": ["app/Over.java"], "main_class": "app.Over"}
    config.write_text(json.dumps(over))

    def run(marker, *argv):
        line = next(i for i, text in enumerate(OVER.splitlines(), 1) if f"// {marker}" in text)
        at = ["probe", str(root), "--config", str(config), "--at", f"app/Over.java:{line}"]
        code = main([*at, "--capture", "x", *argv, "--json", "--no-save", "--quiet"])
        return code, json.loads(capsys.readouterr().out)

    code, result = run("STRING_FIRST", "--set", "x=2")
    refused_before_launch(code, result, "requires text in double quotes for String x")
    # Where the int overload comes first, it gets the stop, and the change is made.
    code, result = run("INT_FIRST", "--set", "x=2")
    assert code == 0, result.get("error")
    assert "G:3b" in result["output"], result
