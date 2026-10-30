"""CLI text and argument checks."""

import json

import pytest

from peeky import cli
from peeky.cli import main


def test_inspect_marks_names_no_selector_can_read():
    # `(Return Value)` after `step --out` was listed like any local.
    from peeky.cli import _variables_text

    snapshot = {
        "frames": [{"source": {"path": "a.c"}, "line": 3}],
        "scopes": [
            {
                "name": "Locals",
                "variables": [
                    {"name": "(Return Value)", "type": "int", "value": "7"},
                    {"name": "total", "type": "int", "value": "1"},
                ],
            }
        ],
    }
    text = _variables_text(snapshot)
    assert "(Return Value): int = 7 [not selectable]" in text
    assert "total: int = 1\n" in text + "\n" and "total: int = 1 [not" not in text


def test_field_sets_where_no_rule_allows_them_are_refused_before_any_build(tmp_path, capsys):
    # `--set s.end=10` was refused only after a build and a run. Rust,
    # C and C++ change fields too; Python refuses before it runs.
    from peeky.cli import main

    (tmp_path / "a.py").write_text("x = 1\n")
    code = main(["probe", str(tmp_path / "a.py"), "--at", "1", "--set", "s.end=10", "--json"])
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 2 and error["code"] == "MUTATION_REJECTED"
    assert "only Java, Rust, C and C++ targets allow" in error["message"]
    assert "COOKBOOK 6.6" in error["message"]
    assert "only the stopped function's own locals change" in error["message"]
    # Names are stripped as `--set` itself strips them (`n = 3`).
    from peeky.cli import _member_sets

    for language in ("c", "cpp", "rust", "java"):
        _member_sets(["s.end=10", "xs[2] = 1", "t.__0=3"], language)
    _member_sets(["n = 3", " x=1"], "python")


def test_session_set_help_names_every_language_that_changes_fields(capsys):
    # `session set --help` names every language, not "(Java also current.end=10, ...)".
    import pytest

    from peeky.cli import main

    with pytest.raises(SystemExit):
        main(["session", "set", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "Java also" not in text
    assert "(Java, Rust, C, C++: current.end=10, xs[0]=1, t.__0=3)" in text


def test_describe_lists_a_java_block_locals_lines_and_each_type_its_own():
    # `ranking (int[], lines 51-53, 78-79)`; one name two blocks declare with
    # different types is listed once per type, each with its lines.
    from peeky.cli import _describe_text

    text = _describe_text(
        {
            "language": "java",
            "root": "/app",
            "breakpoint_lines": {},
            "scopes": [
                {
                    "scope": "Runoff.count",
                    "source": "app/Runoff.java",
                    "lines": [43, 79],
                    "names": ["n", "b", "ranking", "x", "i", "y", "z"],
                    "types": {"n": "int", "b": "int", "ranking": "int[]", "x": "String or int"},
                    "spans": {
                        "b": [[49, 53], [76, 79]],
                        "ranking": [[51, 53], [78, 79]],
                        "x": [[60, 60], [70, 71]],
                        "i": [[60, 60]],
                        "y": [],
                        "z": [[80, 81]],
                    },
                    "typed_spans": {
                        "x": [
                            {"type": "int", "spans": [[60, 60]]},
                            {"type": "String", "spans": [[70, 71]]},
                        ],
                        # A one-line block's `long z`, and an `int z` to the method's end.
                        "z": [
                            {"type": "long", "spans": []},
                            {"type": "int", "spans": [[80, 81]]},
                        ],
                    },
                }
            ],
            "mutable_types": "int",
            "commands": {},
            "timing": {"advice": "fast"},
        }
    )
    [line] = [line for line in text.splitlines() if line.startswith("names in Runoff.count")]
    assert line == (
        "names in Runoff.count (app/Runoff.java:43-79): n (int), b (int, lines 49-53, 76-79), "
        "ranking (int[], lines 51-53, 78-79), x (int, line 60), x (String, lines 70-71), "
        "i (line 60), y (no breakpoint line), z (long, no breakpoint line), z (int, lines 80-81)"
    )


class _Prepared:
    lines, manifest_id = {}, "m"

    def close(self):
        pass


class _Target:
    target_identity = {}

    def prepare(self, program, root):
        return _Prepared()


def _verify_line(monkeypatch, capsys, *argv):
    from peeky import cli

    monkeypatch.setattr(cli, "adapter_for", lambda language, data: _Target())
    monkeypatch.setattr(cli, "rust_tools", lambda: {})
    assert cli.main(["describe", *map(str, argv), "--json"]) == 0
    return json.loads(capsys.readouterr().out)["commands"]["verify"]


def test_describe_names_verifys_at_only_where_verify_needs_it(tmp_path, monkeypatch, capsys):
    # A one-source Java config and a one-file crate showed
    # `--at`, though verify picks that file without it.
    crate = tmp_path / "crate"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text(
        '[package]\nname = "c"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (crate / "src" / "main.rs").write_text("fn main() {}\n")
    line = _verify_line(monkeypatch, capsys, crate)
    assert line.startswith(f"peeky verify {crate} --check "), line
    # Two sources: verify needs to be told which file the edit changes.
    (crate / "src" / "util.rs").write_text("pub fn f() {}\n")
    line = _verify_line(monkeypatch, capsys, crate)
    assert line.startswith(f"peeky verify {crate} --at src/main.rs --check "), line
    app = tmp_path / "java"
    (app / "app").mkdir(parents=True)
    (app / "app" / "Desks.java").write_text("class Desks {}\n")
    config = app / "peeky.json"
    config.write_text(
        json.dumps({"bundle": "b", "sources": ["app/Desks.java"], "main_class": "app.Desks"})
    )
    line = _verify_line(monkeypatch, capsys, app, "--config", config)
    assert line.startswith(f"peeky verify {app} --config {config} --check "), line
    config.write_text(
        json.dumps(
            {"bundle": "b", "sources": ["app/Desks.java", "app/B.java"], "main_class": "app.Desks"}
        )
    )
    line = _verify_line(monkeypatch, capsys, app, "--config", config)
    assert line.startswith(f"peeky verify {app} --config {config} --at app/Desks.java "), line


CHECKER = """import json, subprocess, sys
print("checking 3 cases")
print("FAIL ['+1', '-']: got 'POPPED:' want 'POPPED:1'")
sys.exit({status})
"""


@pytest.mark.parametrize("status", [1, 3])
def test_a_failed_checks_reason_is_its_last_output_line(tmp_path, monkeypatch, capsys, status):
    # `verify --baseline` gave `reason: SystemExit: 1`; the useful line, the
    # checker's `FAIL ...`, was only in `output`.
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "app.py").write_text("print('POPPED:')\n")
    (tmp_path / "check.py").write_text(CHECKER.format(status=status))
    command = ["verify", str(tmp_path / "app.py"), "--check", str(tmp_path / "check.py")]
    assert main([*command, "--baseline", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    fail = "FAIL ['+1', '-']: got 'POPPED:' want 'POPPED:1'"
    # The status stays where it was: the checker's error and its exit code.
    assert result["checker"]["error"] == f"SystemExit: {status}" and result["exit_code"] == 1
    assert result["reason"] == (fail if status == 1 else f"{fail} (SystemExit: {status})")
    assert main([*command, "--baseline"]) == 1
    text = capsys.readouterr().out
    assert f"reason: {result['reason']}\nchecker exit: 1\n" in text, text
    assert "the checker rejects it, as it should reject the bug" in text, text


def test_a_reason_the_output_cannot_give_stays_the_checkers():
    from peeky.cli import _verify_reason

    def failed(error, **extra):
        return {"passed": False, "stage": "verification", "exit_code": 1,
                "checker": {"error": error}, **extra}  # fmt: skip

    # No output, an assertion's own message, or an exit with no status to explain.
    assert _verify_reason(failed("SystemExit: 1")) == "SystemExit: 1"
    assert (
        _verify_reason(failed("AssertionError: 3 != 4", stdout="x\n")) == "AssertionError: 3 != 4"
    )
    assert _verify_reason(failed("SystemExit: 0", stdout="ok\n")) == "SystemExit: 0"
    # Output cut short may end part-way through a line.
    long = "y" * 16000 + "\nFAIL tail"
    assert _verify_reason(failed("SystemExit: 1", stdout=long)) == "SystemExit: 1"
    # stdout first: its last line, though stderr says FAIL.
    both = failed("SystemExit: 1", stdout="case 1 ok\n", stderr="FAIL case 2\n\n")
    assert _verify_reason(both) == "case 1 ok"
    assert _verify_reason(failed("SystemExit: 1", stderr="FAIL case 2\n")) == "FAIL case 2"
    assert _verify_reason(failed("SystemExit: 1", timed_out=True)) == "checker timed out"


def test_a_reason_is_the_line_that_says_what_failed():
    # `FAIL ...` on stdout and `note: cleanup done` on stderr
    # gave the stderr note.
    from peeky.cli import _verify_reason

    def reason(stdout="", stderr=""):
        result = {"passed": False, "stage": "verification", "exit_code": 1}
        return _verify_reason(
            {**result, "checker": {"error": "SystemExit: 1"}, "stdout": stdout, "stderr": stderr}
        )

    noisy = "case 1: ok\nFAIL ring: got '7 2' want '7 3'\n"
    assert reason(noisy, "note: cleanup done\n") == "FAIL ring: got '7 2' want '7 3'"
    # A failure over several lines: its indented lines, two at most.
    spread = "FAIL first case\n  expected 3\n  got 2\n  (seed 9)\nsummary\n"
    assert reason(spread) == "FAIL first case; expected 3; got 2"
    # A traceback gives its exception, where stdout is empty; stdout comes first.
    trace = 'Traceback (most recent call last):\n  File "c.py", line 3\nValueError: bad\n'
    assert reason("", trace) == "ValueError: bad"
    assert reason("assert got == want\n", trace) == "assert got == want"
    # None says what failed: stdout's last line, then stderr's.
    assert reason("case 1 ok\n", "cleanup\n") == "case 1 ok"
    assert reason("", "cleanup\n") == "cleanup"


@pytest.mark.parametrize(
    "stdout, stderr, said",
    [
        # Unanchored words in passing lines were picked.
        ("ok: empty input raises ValueError\nmismatch: wrap got 6 want 7\n", "",
         "mismatch: wrap got 6 want 7"),
        ("FAIL case 3: got 2 want 3\ncase 4 ok: raises KeyError as expected\n", "",
         "FAIL case 3: got 2 want 3"),
        ("case 1 ok: handles ValueError input\nFAIL case 3: got 2 want 3\nsummary: 1 failed\n",
         "", "FAIL case 3: got 2 want 3"),
        ("FAIL case 3: got 2 want 3\ndebug: retrying with ErrorHandler disabled\n", "",
         "FAIL case 3: got 2 want 3"),
        ("mismatch: got 2 want 3\nasserting cleanup finished\n", "",
         "asserting cleanup finished"),
        ("no Traceback (most recent call last) found: ok\nmismatch: got 2 want 3\n", "",
         "mismatch: got 2 want 3"),
        ("case 1 ok\nmismatch in case 2\nFAILOVER test skipped\n", "", "FAILOVER test skipped"),
        # A handled exception logged on stderr is not the failure stdout reports.
        ("recovered\nmismatch: got 2 want 3\n",
         "Traceback (most recent call last):\n  File \"c.py\", line 4\nKeyError: 'k'\n",
         "mismatch: got 2 want 3"),
        # pytest: its `E` line, before the FAILED summary.
        ("test_x.py ...F [100%]\n>       assert run() == '7 3'\n"
         "E       AssertionError: assert '6 3' == '7 3'\n\ntest_x.py:12: AssertionError\n"
         "FAILED test_x.py::test_wrap - AssertionError: assert '6 3' == '7 3'\n", "",
         "AssertionError: assert '6 3' == '7 3'"),
        # unittest (on stderr): the test and its assertion, not `FAILED (failures=1)`.
        ("", ".F\n======\nFAIL: test_wrap (__main__.T.test_wrap)\n------\n"
         "Traceback (most recent call last):\n  File \"x.py\", line 9, in test_wrap\n"
         "AssertionError: '6 3' != '7 3'\n------\nRan 2 tests in 0.001s\n\n"
         "FAILED (failures=1)\n",
         "FAIL: test_wrap (__main__.T.test_wrap): AssertionError: '6 3' != '7 3'"),
    ],
)  # fmt: skip
def test_the_reason_is_the_first_line_anchored_as_a_failure(stdout, stderr, said):
    from peeky.cli import _verify_reason

    error = "SystemExit: True" if "unittest" in said or "__main__" in said else "SystemExit: 1"
    result = {"passed": False, "stage": "verification", "exit_code": 1}
    reason = _verify_reason(
        {**result, "checker": {"error": error}, "stdout": stdout, "stderr": stderr}
    )
    assert reason == said


def test_a_failing_verify_shows_its_only_output_line_once():
    # The long `FAIL …` line came twice, as `reason:` and under `output:`.
    from peeky.cli import _verify_text

    line = "FAIL ['10-4-3']: got 'RESULTS:9' want 'RESULTS:3'"
    result = {
        "passed": False,
        "stage": "verification",
        "reason": line,
        "exit_code": 1,
        "stdout": line + "\n",
        "stderr": "",
        "diff": "-a\n+b",
    }
    text = _verify_text(result)
    assert (
        text.count(line) == 1
        and "output: the checker printed only the `reason:` line above" in text
    ), text
    # With more output than the reason, the output is shown whole.
    text = _verify_text({**result, "stdout": "case 1 ok\n" + line + "\n"})
    assert text.count(line) == 2 and "case 1 ok" in text, text


SESSIONS = """import sys
from dataclasses import dataclass

MAX_GAP = 30
LIMIT: int = 5


@dataclass
class Session:
    start: int
    last: int


def sessionize(events):
    current = {}
    for event in events:
        session = current.get(event)
        gap = 1 if session else None
        if session is None or gap > MAX_GAP:
            current[event] = Session(start=0, last=0)
        print(len(current), file=sys.stderr)
    return current


def counter():
    global LIMIT
    LIMIT += 1
    return LIMIT
"""


def test_describe_lists_the_module_globals_a_function_reads(tmp_path, capsys):
    # `describe` listed `sessionize`'s locals but not `MAX_GAP`, which a
    # probe there reads (`MAX_GAP [module global]`). Names the module itself assigns:
    # not `sys`, `Session`, `print` or `len`.
    path = tmp_path / "sessions.py"
    path.write_text(SESSIONS)
    assert main(["describe", str(path)]) == 0
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("names in sessionize"))
    assert line.endswith(": current, event, events, gap, session, MAX_GAP (module global)"), line
    line = next(line for line in out.splitlines() if line.startswith("names in counter"))
    assert line.endswith(": LIMIT (module global)"), line
    assert main(["describe", str(path), "--json"]) == 0
    scopes = {s["scope"]: s for s in json.loads(capsys.readouterr().out)["scopes"]}
    assert scopes["sessionize"]["globals"] == ["MAX_GAP"]
    assert "globals" not in scopes["<module>"]


def test_describe_lists_only_globals_the_module_binds_and_the_code_loads(tmp_path, capsys):
    # `globals()["CACHE"] = {}` counted `globals` as a module global a
    # function read (its target names it, but only to call it), a subscript target's
    # name (`counts[0] = 1`) counted as bound, and a name in code the compiler drops
    # (`if False:`) counted as read.
    path = tmp_path / "globs.py"
    path.write_text(
        "from collections import Counter as counts\n"
        "LIMIT = 3\n"
        "QUIET = True\n"
        'globals()["CACHE"] = {}\n'
        "counts[0] = 1\n"
        "\n"
        "def wrapped(f):\n"
        "    return f\n"
        "\n"
        "@wrapped\n"
        "def check(n):\n"
        "    if False:\n"
        "        print(QUIET)\n"
        "    return len(globals()) + counts[0] + n + LIMIT\n"
    )
    assert main(["describe", str(path), "--json"]) == 0
    scopes = {s["scope"]: s for s in json.loads(capsys.readouterr().out)["scopes"]}
    assert scopes["check"].get("globals") == ["LIMIT"], scopes["check"]
    assert scopes["check"]["names"] == ["n", "LIMIT"], scopes["check"]


def test_describe_counts_a_generator_expressions_loads_for_its_function(tmp_path, capsys):
    # A generator expression's code is its own (a
    # comprehension's too, before Python 3.12), so `SCALE` read only there was missed. A
    # lambda is a scope of its own: `OTHER` is its, not `total`'s.
    path = tmp_path / "gens.py"
    path.write_text(
        "SCALE = 2\n"
        "OFFSET = 1\n"
        "OTHER = 3\n"
        "\n"
        "def total(xs):\n"
        "    later = lambda: OTHER\n"
        "    nested = (sum(y + OFFSET for y in row) for row in xs)\n"
        "    return sum(x * SCALE for x in xs), {k for k in nested}, later\n"
    )
    assert main(["describe", str(path), "--json"]) == 0
    scopes = {s["scope"]: s for s in json.loads(capsys.readouterr().out)["scopes"]}
    assert scopes["total"].get("globals") == ["OFFSET", "SCALE"], scopes["total"]
    assert scopes["total.lambda"].get("globals") == ["OTHER"], scopes


def test_a_python_field_set_is_refused_as_other_languages_refuse_it(tmp_path, capsys):
    # `peeky: [MUTATION_REJECTED] …` on stderr, where C, Rust and Java refusals
    # before the run print `error [MUTATION_REJECTED]: …`; and its hint said "rows marked
    # [settable] can be changed here", which Python never shows.
    program = tmp_path / "a.py"
    program.write_text("x = 1\n")
    code = main(["probe", str(program), "--at", "1", "--set", "s.end=10"])
    captured = capsys.readouterr()
    assert code == 2 and captured.err == "", captured.err
    lines = captured.out.splitlines()
    assert lines[0].startswith("error [MUTATION_REJECTED]: --set s.end changes a field"), lines
    assert lines[1] == (
        "hint: Set a local of the stopped function instead, or add one with a candidate edit "
        "(`--old`/`--new`) and set it in the same probe."
    ), lines
    assert "[settable]" not in captured.out
    # `--json` is unchanged: the probe's refusal before launch.
    assert main(["probe", str(program), "--at", "1", "--set", "s.end=10", "--json"]) == 2
    shown = json.loads(capsys.readouterr().out)
    assert shown["status"] == "failed" and shown["refused_before_launch"] is True
    assert shown["error"]["code"] == "MUTATION_REJECTED"
    assert "[settable]" not in shown["error"]["hint"]


def test_verify_takes_a_java_targets_one_source_without_at():
    # `verify` on the seats directory without `--at app/Seats.java` was refused,
    # though its config declares that one source.
    assert cli._default_program(None, {"sources": ["app/Seats.java"]}, "verify") == "app/Seats.java"
    assert cli._default_program("app/A.java", {"sources": ["app/B.java"]}, "verify") == "app/A.java"
    with pytest.raises(ValueError) as refused:
        cli._default_program(None, {"sources": ["a/One.java", "b/Two.java"]}, "verify")
    # The edited file is the one holding --old, whichever of them that is.
    assert str(refused.value) == (
        "verify needs --at <source> (one of: a/One.java, b/Two.java) for this target: pass "
        "`--at <the source containing --old>`, the file the edit changes"
    )
    with pytest.raises(ValueError, match=r"^verify needs --at <source> for this target$"):
        cli._default_program(None, {}, "verify")


def test_describe_names_each_functions_settable_root_locals():
    # `mutable:` named fields and registers per function, never `start` or `total`.
    text = cli._describe_text(
        {
            "language": "rust",
            "root": "/crate",
            "breakpoint_lines": {},
            "scopes": [
                {"scope": "tally::count", "lines": [56, 75], "names": ["car", "lap", "start"]}
                | {"types": {"car": "&str", "lap": "tally::Lap", "start": "u32"}}
                | {"settable_parts": {"lap": ["lap.start"]}},
                {"scope": "tally::show", "lines": [42, 44], "names": ["t"]}
                | {"types": {"t": "u32"}, "never_settable": {"t": "register"}},
                {"scope": "tally::main", "lines": [77, 92], "names": ["arg", "n", "x"]}
                | {"types": {"arg": "alloc::string::String", "n": "usize", "x": "u32"}}
                | {"ambiguous": {"x": 2}},
            ],
            "mutable_types": "integer root locals and fields",
            "commands": {},
            "timing": {"advice": "fast"},
        }
    )
    [line] = [line for line in text.splitlines() if line.startswith("mutable:")]
    assert (
        "integer root locals and fields. Root locals of those types (a stop's rows and `inspect` mark which "
        "can change there): tally::count: start; tally::main: arg, n. Such fields" in line
    ), line
    assert "Never settable, kept in a register" in line and "tally::show: t" in line, line
