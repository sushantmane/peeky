import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from peeky.adapters import PythonAdapter, check_module, skipped_lines, under
from peeky.dap import DAP


def test_python_launch_configuration_preserves_existing_behavior(tmp_path):
    adapter = PythonAdapter()
    assert adapter.command == (sys.executable, "-m", "debugpy.adapter")
    assert adapter.initialize_args()["adapterID"] == "python"
    assert adapter.launch_args(tmp_path / "main.py", tmp_path) == {
        "program": str(tmp_path / "main.py"),
        "args": [],
        "cwd": str(tmp_path),
        "console": "internalConsole",
        "redirectOutput": True,
        "justMyCode": True,
        "subProcess": False,
        # A debugged run writes no __pycache__ into the root.
        "env": {"PYTHONDONTWRITEBYTECODE": "1"},
    }


def test_installed_code_is_launched_for_debugpy_to_stop_in(tmp_path):
    # debugpy skips installed code (`justMyCode`): a breakpoint there never stopped.
    adapter = PythonAdapter()
    library = tmp_path / "pkg.py"
    assert adapter.launch_args(library, tmp_path, library=True)["justMyCode"] is False
    assert adapter.launch_args(library, tmp_path)["justMyCode"] is True


def _site(tmp_path, *parts):
    """A file at ``parts`` under tmp_path, with its directories."""
    path = tmp_path.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n")
    return path


def test_installed_is_what_the_programs_interpreter_reports(tmp_path):
    # A path component named site-packages decided it: an ordinary directory of that name
    # was installed code, and a checkout under one got justMyCode false.
    site = tmp_path / "env" / "lib" / "python3.13" / "site-packages"
    mine = _site(tmp_path, "env", "lib", "python3.13", "site-packages", "pkg", "mod.py")
    ordinary = _site(tmp_path, "work", "site-packages", "tool.py")
    checkout = _site(tmp_path, "site-packages", "proj", "app.py")
    debian = _site(tmp_path, "usr", "lib", "python3", "dist-packages", "pkg", "mod.py")
    assert under(mine, [site]) and under(debian, [debian.parent.parent])
    assert not under(ordinary, [site]) and not under(checkout, [site])
    assert not under(debian, [site]) and not under(mine, [tmp_path / "missing"])
    # Through a link (`lib64` to `lib`), either way.
    (tmp_path / "env" / "lib64").symlink_to("lib")
    linked = tmp_path / "env" / "lib64" / "python3.13" / "site-packages"
    assert under(mine, [linked])
    assert under(linked / "pkg" / "mod.py", [site])
    # Spelled in another case, on a volume that ignores case (macOS by default).
    upper = Path(str(mine.parent).upper()) / mine.name
    if upper.exists():
        assert under(upper, [site]) and under(mine, [Path(str(site).upper())])


def test_installed_asks_the_programs_interpreter_and_assumes_nothing(tmp_path):
    from peeky.python_interpreter import Unavailable

    site = tmp_path / "site-packages"
    mine = _site(tmp_path, "site-packages", "pkg", "mod.py")
    adapter = PythonAdapter()
    asked = []

    def readings(reading, *args):
        asked.append(reading)
        return [str(site)]

    object.__setattr__(adapter, "_readings", readings)
    assert adapter.installed(mine) and asked == ["installed_dirs"]

    def unavailable(reading, *args):
        raise Unavailable("it ended")

    object.__setattr__(adapter, "_readings", unavailable)
    assert not adapter.installed(mine)


def test_debugpys_word_that_it_skips_a_file_is_recognized():
    skipped = {
        "verified": False,
        "line": 4,
        "message": "Breakpoint in file excluded by filters.\nNote: may be excluded because of "
        '"justMyCode" option (default == true).',
    }
    assert skipped_lines({"breakpoints": [{"verified": True, "line": 2}, skipped]}) == [4]
    assert skipped_lines({"breakpoints": [{"verified": False, "line": 3}]}) == []
    assert skipped_lines(None) == []


def test_a_module_run_is_launched_as_python_minus_m(tmp_path):
    adapter = PythonAdapter(identity={"optimize": 0})
    launch = adapter.launch_args(tmp_path / "x.py", tmp_path, ["-q"], module="pytest")
    assert launch["module"] == "pytest" and "program" not in launch, launch
    assert launch["args"] == ["-q"] and launch["cwd"] == str(tmp_path)
    for bad in ("py test", "pytest.", "-m", "", "a" * 201, "tests/test_x.py"):
        with pytest.raises(ValueError, match="--run-module takes a module's name"):
            check_module(bad)
    assert check_module("app.cli") == "app.cli"


def test_pytest_runs_with_plain_asserts_or_is_refused():
    # pytest rewrites test files' asserts: their code is then not the source Peeky reads.
    adapter = PythonAdapter(identity={"optimize": 0})
    args, notes = adapter.module_arguments("pytest", ["-q", "tests/test_x.py::test_y"])
    assert args == ["--assert=plain", "-q", "tests/test_x.py::test_y"]
    assert notes and "pytest ran with --assert=plain, which Peeky added" in notes[0]
    assert adapter.module_arguments("pytest", ["--assert=plain"])[0][-1] == "--assert=plain"
    # After `--`, an argument is pytest's file name, not an option.
    assert adapter.module_arguments("pytest", ["--", "--assert=rewrite"])[0][0] == "--assert=plain"
    assert adapter.module_arguments("unittest", ["-v"]) == (["-v"], [])
    for given in (["--assert=rewrite"], ["--assert", "rewrite"], ["-q", "--assert"]):
        with pytest.raises(ValueError, match="Leave --assert out"):
            adapter.module_arguments("pytest", given)
    with pytest.raises(ValueError, match="Unset PYTHONOPTIMIZE"):
        PythonAdapter(identity={"optimize": 1}).module_arguments("py.test", [])


def test_a_script_that_imports_pytest_is_noted(tmp_path):
    script = tmp_path / "run.py"
    script.write_text("import sys, pytest\nsys.exit(pytest.main(sys.argv[1:]))\n")
    assert "Use --run-module pytest instead" in PythonAdapter.script_notes(script)[0]
    for text in ("import pytest_mock\n", "# import pytest\n", "x = 'import pytest'\n"):
        script.write_text(text)
        assert PythonAdapter.script_notes(script) == []
    assert PythonAdapter.script_notes(tmp_path / "missing.py") == []


def test_scope_selection_is_python_specific():
    scopes = [
        {"name": "Locals", "presentationHint": "locals", "variablesReference": 1},
        {"name": "Globals", "presentationHint": "globals", "variablesReference": 2},
        {"name": "Expensive", "presentationHint": "locals", "expensive": True},
    ]
    assert PythonAdapter().local_scopes(scopes) == [scopes[0]]
    # debugpy's own Globals scope (no presentation hint) is listed after the locals.
    debugpy = [*scopes, {"name": "Globals", "variablesReference": 3}]
    assert PythonAdapter().local_scopes(debugpy) == [scopes[0], debugpy[-1]]


def test_capability_normalization_does_not_overclaim_operations():
    adapter = PythonAdapter()
    raw = {
        "supportsSetVariable": True,
        "supportsConditionalBreakpoints": True,
        "supportsLogPoints": True,
        "supportsEvaluateForHovers": True,
        "supportsStepBack": True,
    }
    normalized = adapter.normalize_capabilities(raw)
    assert normalized["supports_set_variable"]
    # Conditions and logpoints are code the debuggee would run: never offered.
    assert not normalized["supports_conditional_breakpoints"]
    assert not normalized["supports_logpoints"]
    assert not normalized["supports_hit_conditional_breakpoints"]
    assert not normalized["supports_evaluate"]
    assert not normalized["supports_attach"]
    assert "supportsStepBack" not in normalized
    assert not any(adapter.normalize_capabilities({}).values())
    assert not adapter.normalize_capabilities({"supportsSetVariable": "true"})[
        "supports_set_variable"
    ]


@pytest.mark.parametrize("command", [[], "python -m debugpy.adapter", [None], [""], ["x\x00y"]])
def test_dap_rejects_non_argv_configuration_before_spawning(command):
    with patch("peeky.dap.subprocess.Popen") as spawn:
        with pytest.raises(ValueError, match="argument vector"):
            DAP(command)
        spawn.assert_not_called()


@pytest.mark.parametrize("command", [None, ("/trusted/java", "-cp", "path with spaces", "Shim")])
def test_dap_launches_only_configured_argument_vector(command):
    # Abort at the spawn boundary: the assertion covers exact argv, no shell.
    with patch("peeky.dap.subprocess.Popen", side_effect=OSError("test spawn boundary")) as spawn:
        with pytest.raises(OSError, match="test spawn boundary"):
            DAP(command)
        assert spawn.call_args.args[0] == (command or PythonAdapter().command)
        assert not spawn.call_args.kwargs.get("shell", False)


@pytest.mark.parametrize(
    "points", [[{"line": True}], [{"line": 1, "condition": []}], [{"line": 1, "unknown": "x"}]]
)
def test_python_rejects_invalid_breakpoint_shape(points):
    with pytest.raises(ValueError):
        PythonAdapter().validate_breakpoints(points)


@pytest.mark.parametrize(
    "field", [{"condition": "x > 0"}, {"hitCondition": "3"}, {"logMessage": "value={x}"}]
)
def test_python_breakpoints_never_carry_code(field):
    PythonAdapter().validate_breakpoints([{"line": 7}])
    with pytest.raises(ValueError, match="would run code in the program") as caught:
        PythonAdapter().validate_breakpoints([{"line": 7, **field}])
    from peeky.errors import classify

    assert classify(caught.value)["code"] == "INVALID_BREAKPOINT"


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "-3",
        "1.5e3",
        "'x'",
        "b'x'",
        "None",
        "True",
        "[]",
        "[1, 'a', (2,)]",
        "{'k': {1}}",
        "1+2j",
    ],
)
def test_python_literals_are_accepted(value):
    PythonAdapter().validate_value(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "x",
        "set()",
        "float('-inf')",
        "__import__('os').system('true')",
        "[x]",
        "a.b",
        "a[0]",
        "1; 2",
        "lambda: 0",
        "(yield)",
        "1" * 5000,
        "[" * 1000 + "]" * 1000,
    ],
)
def test_python_expressions_are_rejected_before_reaching_debugpy(value):
    with pytest.raises(ValueError, match="must be a literal"):
        PythonAdapter().validate_value(value)
