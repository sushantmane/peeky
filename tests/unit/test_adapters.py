import sys
from unittest.mock import patch

import pytest

from peeky.adapters import PythonAdapter, installed, skipped_lines
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
    library = tmp_path / ".venv" / "lib" / "python3.13" / "site-packages" / "pkg" / "mod.py"
    assert installed(library)
    assert installed("/usr/lib/python3/dist-packages/pkg/mod.py")
    assert not installed(tmp_path / "src" / "pkg" / "mod.py")
    assert not installed(tmp_path / "site-packages.py")
    adapter = PythonAdapter()
    assert adapter.launch_args(library, tmp_path, library=True)["justMyCode"] is False
    assert adapter.launch_args(library, tmp_path)["justMyCode"] is True


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
