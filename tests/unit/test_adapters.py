import sys
from unittest.mock import patch

import pytest

from peeky.adapters import PythonAdapter
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
    }


def test_scope_selection_is_python_specific():
    scopes = [
        {"name": "Locals", "presentationHint": "locals", "variablesReference": 1},
        {"name": "Globals", "presentationHint": "globals", "variablesReference": 2},
        {"name": "Expensive", "presentationHint": "locals", "expensive": True},
    ]
    assert PythonAdapter().local_scopes(scopes) == [scopes[0]]


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
    assert normalized["supports_conditional_breakpoints"]
    assert normalized["supports_logpoints"]
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


def test_python_accepts_existing_breakpoint_features():
    PythonAdapter().validate_breakpoints(
        [{"line": 7, "condition": "x > 0", "hitCondition": "3", "logMessage": "value={x}"}]
    )


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
