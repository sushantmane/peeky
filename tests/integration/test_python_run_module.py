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


def test_only_the_pytest_installed_for_the_programs_interpreter_runs(project, capsys):
    # `--run-module pytest.__main__` ran pytest with its asserts rewritten, and with no
    # warning; and a `pytest.py` in the root ran instead of pytest.
    at = ["probe", project, "--at", "src/shop/cart.py:4", "--capture", "cut", "--no-save"]
    code, out = run(capsys, *at, "--run-module", "pytest.__main__", "--json", "--", *QUIET)
    error = json.loads(out.out)["error"]
    assert code == 2 and "--run-module takes only pytest" in error["message"], error
    shadows = {
        "pytest.py": "it is not the pytest package",
        "pytest/__init__.py": "it is not in a site-packages directory of that interpreter",
    }
    for name, why in shadows.items():
        shadow = project / name
        shadow.parent.mkdir(exist_ok=True)
        shadow.write_text("print('shadow ran')\n")
        code, out = run(capsys, *at, "--run-module", "pytest", "--json", "--", *QUIET)
        result = json.loads(out.out)
        message = result["error"]["message"]
        assert code == 2 and result["refused_before_launch"] is True, result
        assert f"`python -m pytest` would run {shadow.resolve()}: {why}" in message, message
        assert "shadow ran" not in (result.get("output") or "")
        shadow.unlink()


def test_pytest_must_be_installed_for_the_programs_interpreter(project, capsys):
    from python_packages import make_env

    python = make_env(project / ".venv")
    code, out = run(
        capsys,
        *("probe", project, "--at", "src/shop/cart.py:4", "--capture", "cut", "--no-save"),
        *("--run-module", "pytest", "--json", "--", *QUIET),
    )
    error = json.loads(out.out)["error"]
    assert code == 2 and f"interpreter, {python}, and `python -m pytest`" in error["message"]
    assert "finds none" in error["message"], error


def test_pytest_without_its_assertion_plugin_runs_as_it_is(project, capsys):
    # With `-p no:assertion`, pytest rewrites nothing, and refuses --assert (exit 4):
    # Peeky adds nothing, and says why.
    code, out = run(
        capsys,
        *("probe", project, "--at", "tests/test_cart.py:9", "--capture", "d", "--hits", "all"),
        *("--run-module", "pytest", "--json", "--no-save"),
        *("--", *QUIET, "-p", "no:assertion", "tests/test_cart.py::test_total"),
    )
    result = json.loads(out.out)
    assert code == 0 and [row["value"] for row in result["rows"]] == ["0.1", "0.2"], result
    assert "--assert=plain" not in result["program_args"], result["program_args"]
    assert any("never loaded its assertion plugin" in note for note in result["warnings"])


def _stops_at_9(capsys, project, *args):
    """A probe of the assert's line 9 in a loop: its result."""
    code, out = run(
        capsys,
        *("probe", project, "--at", "tests/test_cart.py:9", "--capture", "d", "--hits", "all"),
        *("--run-module", "pytest", "--json", "--no-save"),
        *("--", *QUIET, *args, "tests/test_cart.py::test_total"),
    )
    result = json.loads(out.out)
    assert "status" in result, result
    return result


def _plain(result):
    """Whether Peeky gave pytest --assert=plain, and said so, and nothing says rewriting
    is off for another reason."""
    notes = result.get("warnings") or []
    return (
        result["program_args"][0] == "--assert=plain"
        and any("pytest ran with --assert=plain" in note for note in notes)
        and not any("never loaded its assertion plugin" in note for note in notes)
    )


def test_a_reloaded_assertion_plugin_gets_plain_asserts(project, capsys, monkeypatch):
    # Peeky stopped at the first `-p no:assertion` and added nothing, though a later
    # `-p assertion` loads the plugin again; and `-p no:assertion` in PYTEST_ADDOPTS,
    # which pytest reads after it decides to rewrite, was taken for rewriting off.
    result = _stops_at_9(capsys, project, "-p", "no:assertion", "-p", "assertion")
    assert _plain(result) and [row["value"] for row in result["rows"]] == ["0.1", "0.2"], result
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:assertion")
    result = _stops_at_9(capsys, project)
    assert _plain(result) and [row["value"] for row in result["rows"]] == ["0.1", "0.2"], result
    # Loaded again on the command line: never a second stop per pass. (pytest 9.1.1 itself
    # fails here, "option names {'--assert'} already added", before any test runs.)
    result = _stops_at_9(capsys, project, "-p", "assertion")
    assert _plain(result), result
    assert [row["value"] for row in result["rows"]] in ([], ["0.1", "0.2"]), result


def test_assert_rewrite_from_pytest_addopts_or_config_is_overridden(project, capsys, monkeypatch):
    # Peeky's --assert=plain comes after a config file's addopts and PYTEST_ADDOPTS, so it
    # is the one pytest takes: the line stops once per pass.
    monkeypatch.setenv("PYTEST_ADDOPTS", "--assert=rewrite")
    result = _stops_at_9(capsys, project)
    assert _plain(result) and [row["value"] for row in result["rows"]] == ["0.1", "0.2"], result
    monkeypatch.delenv("PYTEST_ADDOPTS")
    (project / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\naddopts = "--assert=rewrite"\n'
    )
    result = _stops_at_9(capsys, project)
    assert _plain(result) and [row["value"] for row in result["rows"]] == ["0.1", "0.2"], result
    # Without Peeky's, the config's rewriting stops it twice per pass (a runner script).
    (project / "run_tests.py").write_text(RUNNER)
    code, out = run(
        capsys,
        *("probe", project, "--at", "tests/test_cart.py:9", "--capture", "d", "--hits", "all"),
        *("--run", "run_tests.py", "--json", "--no-save", "--", *QUIET),
        "tests/test_cart.py::test_total",
    )
    assert len(json.loads(out.out)["rows"]) == 4, out.out
