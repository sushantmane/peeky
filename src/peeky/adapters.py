"""Runtime-specific DAP configuration selected by trusted worker code.

Adapter commands and launch configuration are not accepted from HTTP clients.
Python runs directly; compiled runtimes require prepared build provenance.
"""

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class PythonAdapter:
    interpreter: str = sys.executable

    environment = None
    manifest_id = None

    def prepare(self, program, root):
        return self

    def source_path(self, original):
        return original

    def assert_current(self):
        pass

    def validate_stop(self, frame):
        pass

    def validate_value(self, value):
        pass

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

    def launch_args(self, program, root):
        return {
            "program": str(program),
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
            "supports_conditional_breakpoints": raw.get("supportsConditionalBreakpoints") is True,
            "supports_hit_conditional_breakpoints": raw.get("supportsHitConditionalBreakpoints")
            is True,
            "supports_logpoints": raw.get("supportsLogPoints") is True,
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
            if set(point) - {"line", "condition", "hitCondition", "logMessage"}:
                raise ValueError("Unsupported breakpoint field")
            if any(not isinstance(value, str) for key, value in point.items() if key != "line"):
                raise ValueError("Breakpoint expressions must be strings")
