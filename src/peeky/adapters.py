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


# Runs the edited copy as `__main__` with the original script's identity: `__file__`,
# `sys.argv[0]` and `sys.path[0]` name the original, so everything the script finds by
# its own path is the user's. Compiled under the copy's path, so breakpoints set on the
# copy bind. Another Python process started on the original file would run the unedited
# code; those starts fail loudly instead.
_CANDIDATE_MAIN = """\
import os
import sys
import tokenize
import types

staged, original = sys.argv[1], sys.argv[2]
sys.argv = [original, *sys.argv[3:]]
sys.path[0] = os.path.dirname(original)
with tokenize.open(staged) as stream:
    code = compile(stream.read(), staged, "exec")
REFUSED = (
    "Peeky candidate probe: this program starts another Python process on "
    + original
    + ", which would run the unedited file; the edit reaches this process only. "
    "Check the edit with `peeky verify`"
)


def _runs_original(args):
    if isinstance(args, (str, bytes, os.PathLike)):
        args = [args]
    for arg in args or ():
        text = os.fsdecode(arg) if isinstance(arg, (bytes, os.PathLike)) else str(arg)
        if os.path.abspath(text) == original or (
            os.path.isfile(text) and os.path.samefile(text, original)
        ):
            return True
    return False


def _guard():
    import subprocess

    popen = subprocess.Popen.__init__

    def guarded(self, args, *rest, **options):
        if _runs_original(args):
            raise RuntimeError(REFUSED)
        popen(self, args, *rest, **options)

    subprocess.Popen.__init__ = guarded
    for name in ("execv", "execve", "posix_spawn", "posix_spawnp"):
        if callable(launch := getattr(os, name, None)):

            def checked(path, args, *rest, _launch=launch, **options):
                if _runs_original([path, *args]):
                    raise RuntimeError(REFUSED)
                return _launch(path, args, *rest, **options)

            setattr(os, name, checked)
    try:
        import multiprocessing.spawn as spawn
    except ImportError:
        return
    prepare = spawn.get_preparation_data

    def data(name):
        found = prepare(name)
        # spawn and forkserver re-run __main__ by its path in each child (fork does not).
        if found.get("init_main_from_path"):
            raise RuntimeError(REFUSED)
        return found

    spawn.get_preparation_data = data


_guard()
main = types.ModuleType("__main__")
main.__file__ = original
main.__builtins__ = __builtins__
sys.modules["__main__"] = main
exec(code, main.__dict__)
"""


class PythonCandidate:
    """A Python candidate edit: the edited program file, run from a private directory as
    if it were the original script (`__file__`, `sys.argv[0]` and the import path are
    the original's), from the original root. Nothing in the user's tree is linked or
    written. A start of another Python process on the original file fails loudly."""

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
        self.staged = self.scratch / "edited" / relative.name
        self.bootstrap = self.scratch / "peeky_candidate_main.py"
        try:
            self.staged.parent.mkdir()
            for path, content in (
                (self.staged, text.replace(edit.old, edit.new, 1)),
                (self.bootstrap, _CANDIDATE_MAIN),
            ):
                # A fresh file in the private directory only, never through a link.
                handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW)
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(content)
        except BaseException:
            shutil.rmtree(self.scratch, ignore_errors=True)
            raise

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
        return self.adapter.launch_args(
            self.bootstrap, root, [str(self.staged), str(self.original), *args]
        )

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
