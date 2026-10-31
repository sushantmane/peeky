"""A pytest test run as a module (`--run-module pytest`), against real debugpy and pytest.

To stop in a project's code while one of its pytest tests ran, an agent wrote a runner
script for `--run`, which takes only a `.py` file. And pytest, run that way, rewrote the
test files' asserts: code Peeky's readings of a test file don't see (a line in a loop
stopped twice per pass). `--run-module pytest` runs it as `python -m pytest` does, with
`--assert=plain`, so the test files run as Peeky reads them.
"""

import json
import sqlite3

import pytest

from peeky.cli import main

CART = """\
def total(prices, discount):
    subtotal = sum(prices)
    cut = subtotal * discount
    return subtotal - cut
"""
TEST = """\
from shop.cart import total


def test_total():
    for d in (0.1, 0.2):
        got = total([10, 20], d)
        assert (
            got
            > 0
        )


def test_other():
    assert total([1], 0) == 1
"""
RUNNER = "import sys, pytest\nsys.exit(pytest.main(sys.argv[1:]))\n"
# pytest's own options for these runs: no cache written into the project.
QUIET = ("-q", "-p", "no:cacheprovider")


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A small pytest project: its code in src/, its tests in tests/. It has no `.venv`,
    so the tests run on Peeky's own interpreter, which has pytest."""
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("PYTHONOPTIMIZE", raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    root = tmp_path / "shop"
    (root / "src" / "shop").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "shop" / "__init__.py").write_text("")
    (root / "src" / "shop" / "cart.py").write_text(CART)
    (root / "tests" / "test_cart.py").write_text(TEST)
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["src"]\n')
    return root


def run(capsys, *argv):
    code = main([str(arg) for arg in argv])
    return code, capsys.readouterr()


def test_a_probe_runs_a_pytest_test_as_a_module(project, capsys):
    code, out = run(
        capsys,
        *("probe", project, "--at", "src/shop/cart.py:4", "--capture", "cut,1:d,1:got"),
        *("--hits", "all", "--run-module", "pytest", "--json", "--no-save"),
        *("--", *QUIET, "tests/test_cart.py::test_total"),
    )
    result = json.loads(out.out)
    assert code == 0 and result["status"] == "completed", result
    values = [(row["hit"], row["selector"], row.get("value")) for row in result["rows"]]
    assert values == [
        (1, "cut", "3.0"),
        (1, "1:d", "0.1"),
        (1, "1:got", None),
        (2, "cut", "6.0"),
        (2, "1:d", "0.2"),
        (2, "1:got", "27.0"),
    ], values
    # The test's own frame follows the loop's rules: last pass's value, with its note.
    assert "this pass's code has not assigned it" in result["rows"][-1]["note"]
    assert result["probe"]["module"] == "pytest" and result["probe"]["entry"] is None
    assert result["program_args"] == ["--assert=plain", *QUIET, "tests/test_cart.py::test_total"]
    assert any("pytest ran with --assert=plain" in note for note in result["warnings"])
    assert "1 passed" in result["output"], result["output"]


def test_a_test_files_line_stops_once_per_pass_as_peeky_reads_it(project, capsys):
    # Rewritten, the assert's line 9 has code twice per pass: four stops, not two.
    at = ["probe", project, "--at", "tests/test_cart.py:9", "--capture", "d", "--hits", "all"]
    code, out = run(capsys, *at, "--run-module", "pytest", "--json", "--no-save", "--", *QUIET)
    result = json.loads(out.out)
    assert code == 0 and [row["value"] for row in result["rows"]] == ["0.1", "0.2"], result
    # A runner script lets pytest rewrite them, and says so.
    (project / "run_tests.py").write_text(RUNNER)
    code, out = run(capsys, *at, "--run", "run_tests.py", "--json", "--no-save", "--", *QUIET)
    result = json.loads(out.out)
    assert code == 0 and len(result["rows"]) > 2, result["rows"]
    assert any("Use --run-module pytest instead" in note for note in result["warnings"])


def test_what_would_rewrite_asserts_is_refused_before_the_run(project, capsys, monkeypatch):
    at = ["probe", project, "--at", "src/shop/cart.py:4", "--capture", "cut", "--no-save"]
    code, out = run(capsys, *at, "--run-module", "pytest", "--json", "--", "--assert=rewrite")
    result = json.loads(out.out)
    assert code == 2 and result["refused_before_launch"] is True, result
    assert "Leave --assert out" in result["error"]["message"], result["error"]
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    code, out = run(capsys, *at, "--run-module", "pytest", "--json", "--", *QUIET)
    result = json.loads(out.out)
    assert code == 2 and "Unset PYTHONOPTIMIZE" in result["error"]["message"], result
    monkeypatch.delenv("PYTHONOPTIMIZE")
    code, out = run(capsys, *at, "--run-module", "pytest", "--run", "x.py")
    assert code == 2 and "Pass --run or --run-module, not both" in out.err, out
    old = ["--old", "cut = subtotal * discount", "--new", "cut = 0"]
    code, out = run(capsys, *at, *old, "--run-module", "pytest", "--json", "--", *QUIET)
    error = json.loads(out.out)["error"]
    assert code == 2 and "with --run-module, the module would import" in error["message"], error


def test_a_session_runs_a_module_and_the_evidence_names_it(project, capsys):
    start = ["session", "start", project, "--at", "src/shop/cart.py:4", "--run-module", "pytest"]
    code, out = run(capsys, *start, "--", *QUIET, "tests/test_cart.py::test_other")
    try:
        assert code == 0, out
        assert "pytest ran with --assert=plain" in out.out, out.out
        code, out = run(capsys, "session", "get", "subtotal", "--json")
        assert code == 0 and json.loads(out.out)[0]["value"] == "1", out.out
    finally:
        run(capsys, "session", "close")
    code, out = run(
        capsys,
        *("probe", project, "--at", "src/shop/cart.py:4", "--capture", "cut"),
        *("--run-module", "pytest", "--json", "--", *QUIET, "tests/test_cart.py::test_other"),
    )
    store = json.loads(out.out)["evidence"]["store"]
    with sqlite3.connect(store) as connection:
        entry, args = connection.execute("SELECT entry, args FROM runs").fetchone()
    assert entry == "-m pytest" and json.loads(args)[0] == "--assert=plain", (entry, args)
