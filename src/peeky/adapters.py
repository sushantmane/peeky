"""Runtime-specific DAP configuration selected by trusted worker code.

Adapter commands and launch configuration are not accepted from HTTP clients.
Python runs directly; compiled runtimes require prepared build provenance.
"""

import ast
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PYTHON_LITERAL_NODES = (
    ast.Expression,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.UnaryOp,
    ast.USub,
    ast.UAdd,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Load,
)


def validate_python_literal(value):
    # debugpy evaluates the value inside the debuggee, so only inert literal syntax
    # (no names, calls, attributes or subscripts) may reach it.
    message = (
        "Python --set values must be a literal (number, string, bytes, True/False/None, "
        "or a list/tuple/dict/set of literals); expressions are not evaluated"
    )
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(message)
    try:
        tree = ast.parse(value.strip(), mode="eval")
        if not all(isinstance(node, PYTHON_LITERAL_NODES) for node in ast.walk(tree)):
            raise ValueError(message)
        ast.literal_eval(tree)
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        raise ValueError(message) from None


class PythonCandidate:
    """A Python candidate edit: the edited program file alone, at its own relative path
    in a private directory. It runs with the original root as its working directory, and
    its own directory's sibling modules still import from the originals (that directory
    follows on PYTHONPATH). The program's file is never written."""

    def __init__(self, adapter, program, root, edit):
        self.adapter = adapter
        self.original = Path(program).resolve(strict=True)
        relative = self.original.relative_to(Path(root).resolve(strict=True))
        if relative.as_posix() != edit.path:
            raise ValueError("A Python candidate edits the --at file only")
        text = self.original.read_text()
        if not edit.old or edit.old == edit.new or text.count(edit.old) != 1:
            raise ValueError("--old must match the --at file exactly once and --new must change it")
        self.scratch = Path(tempfile.mkdtemp(prefix="peeky-candidate-"))
        self.staged = self.scratch / relative
        self.staged.parent.mkdir(parents=True, exist_ok=True)
        self.staged.write_text(text.replace(edit.old, edit.new, 1))

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def source_path(self, original):
        return self.staged if Path(original).resolve() == self.original else original

    def launch_args(self, program, root, args=()):
        if Path(program).resolve() != self.original:
            # `--run` imports the --at module by name, from the original directory first.
            raise ValueError(
                "A Python candidate runs the edited --at file itself; with --run, the entry "
                "script would import the original module. Probe the edited file directly, "
                "or check the edit with `peeky verify`"
            )
        launch = self.adapter.launch_args(self.staged, root, args)
        paths = [str(self.original.parent), os.environ.get("PYTHONPATH", "")]
        launch["env"] = {"PYTHONPATH": os.pathsep.join(path for path in paths if path)}
        return launch

    def close(self):
        shutil.rmtree(self.scratch, ignore_errors=True)


class ViewBudgetExhausted(ValueError):
    """A bounded value view stopped early; distinct from an invalid debugger shape."""


@dataclass(frozen=True)
class PythonAdapter:
    interpreter: str = sys.executable

    environment = None
    manifest_id = None
    source_manifest = None
    target_identity = None
    # Candidate probes (`probe --old/--new`) run an edited copy of the program file.
    supports_edits = True

    def prepare(self, program, root, edit=None):
        return self if edit is None else PythonCandidate(self, program, root, edit)

    def source_path(self, original):
        return original

    def assert_current(self):
        pass

    def validate_stop(self, frame):
        pass

    def validate_value(self, value):
        validate_python_literal(value)

    def close(self):
        pass

    @property
    def command(self):
        return (self.interpreter, "-m", "debugpy.adapter")

    def initialize_args(self):
        return {
            "adapterID": "python",
            "clientID": "peeky",
            "pathFormat": "path",
            "linesStartAt1": True,
            "columnsStartAt1": True,
        }

    def launch_args(self, program, root, args=()):
        return {
            "program": str(program),
            "args": list(args),
            "cwd": str(root),
            "console": "internalConsole",
            "redirectOutput": True,
            "justMyCode": True,
            "subProcess": False,
        }

    def local_scopes(self, scopes):
        return [
            scope
            for scope in scopes
            if not scope.get("expensive") and scope.get("presentationHint") == "locals"
        ]

    def normalize_capabilities(self, raw):
        # Advertise the intersection of backend support and the worker operations.
        return {
            "supports_set_variable": raw.get("supportsSetVariable") is True,
            # Conditions, hit conditions and logpoints are expressions the debuggee would
            # evaluate; Peeky never evaluates agent-supplied code (ADR 0004).
            "supports_conditional_breakpoints": False,
            "supports_hit_conditional_breakpoints": False,
            "supports_logpoints": False,
            "supports_evaluate": False,
            "supports_attach": False,
        }

    def validate_breakpoints(self, points):
        if not isinstance(points, list) or len(points) > 100:
            raise ValueError("breakpoints must be a list of at most 100 entries")
        for point in points:
            if (
                not isinstance(point, dict)
                or type(point.get("line")) is not int
                or point["line"] < 1
            ):
                raise ValueError("breakpoint line must be a positive integer")
            if set(point) != {"line"}:
                raise ValueError(
                    "Only unconditional line breakpoints are supported: conditions and "
                    "logpoints would run code in the program"
                )
