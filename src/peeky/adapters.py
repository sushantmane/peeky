"""Runtime-specific DAP configuration selected by trusted worker code.

Adapter commands and launch configuration are not accepted from HTTP clients.
Python runs directly; compiled runtimes require prepared build provenance.
"""

import ast
import os
import re
import shutil
import stat
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


# A Python candidate copies its target root. These directories are linked instead of
# copied: large, and not the program's own code.
_LINKED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".eggs",
        "site-packages",
    }
)
MAX_COPIED_FILES = 20_000
MAX_COPIED_BYTES = 256 * 1024 * 1024
_TOO_LARGE = (
    f"A Python candidate copies the target root, and this one is too large (over "
    f"{MAX_COPIED_FILES} files or {MAX_COPIED_BYTES >> 20} MB): give a smaller target "
    "directory, or check the edit with `peeky verify`"
)
# Code that reaches above its own directory: `..`, `os.pardir`, `parents[1]`, `.parent.parent`.
_ABOVE = re.compile(r"""['"]\.\.['"/\\]|os\.pardir|\.parents\[|\.parent\.parent""")


class PythonCandidate:
    """A Python candidate edit: a private copy of the target root with the edit applied,
    run from there, as a compiled candidate runs from a fresh build. Whatever the script
    reaches through its own path, an import or another process (a subprocess of
    `__file__`, multiprocessing's spawn) is the copy, so all of it runs the edit. Files
    above the root are not copied. Nothing in the user's tree is written."""

    def __init__(self, adapter, program, root, edit):
        self.adapter = adapter
        self.original = Path(program).resolve(strict=True)
        root = Path(root).resolve(strict=True)
        relative = self.original.relative_to(root)
        if relative.as_posix() != edit.path:
            raise ValueError("A Python candidate edits the --at file only")
        text = self.original.read_text()
        if not edit.old or edit.old == edit.new or text.count(edit.old) != 1:
            raise ValueError("--old must match the --at file exactly once and --new must change it")
        self.scratch = Path(tempfile.mkdtemp(prefix="peeky-candidate-"))
        try:
            self.root = self.scratch / (root.name or "root")
            # The copy of the --at file, under the name it has on disk (`--at APP.py`
            # names `app.py` on a case-insensitive volume; imports use the stored name).
            self.staged = self._copy(root, self.root, os.stat(self.original))
            # The edited file replaces its copy, and is never written through a link.
            for parent in self.staged.parents:
                if parent == self.root:
                    break
                if parent.is_symlink() or not parent.is_dir():
                    raise ValueError("A Python candidate's --at path leaves the copied root")
            os.unlink(self.staged)
            handle = os.open(self.staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text.replace(edit.old, edit.new, 1))
        except BaseException:
            shutil.rmtree(self.scratch, ignore_errors=True)
            raise
        self.warnings = (
            [
                "This Python candidate runs from a copy of the target root, and the script "
                "refers to paths above its directory (`..`, `parents[...]`), which are not "
                "copied: those resolve outside the copy. Give the directory that holds them as "
                "the target (`peeky probe <project> --at <path/to/script.py>:<line>`)"
            ]
            if relative.parent == Path(".") and _ABOVE.search(text)
            else []
        )

    @staticmethod
    def _copy(source, destination, program):
        """Copy ``source`` to ``destination``: real directories, and regular files' bytes
        and modes. Links point where the originals do (inside the root, into the copy;
        outside, at the resolved original). _LINKED_DIRS, anything not a regular file
        (sockets, FIFOs) and unreadable files are links to the originals. `__pycache__`
        is left out, so the copy compiles its own. Returns the copy of ``program`` (an
        os.stat_result), by identity, under its stored name."""
        files = size = 0
        staged = None

        def link(target, original):
            real = Path(os.path.realpath(original))
            if real == source or source in real.parents:
                inside = destination / real.relative_to(source)
                target.symlink_to(os.path.relpath(inside, target.parent))
            else:
                target.symlink_to(real)

        for here, directories, names in os.walk(source):
            folder = destination / Path(here).relative_to(source)
            folder.mkdir()
            kept = []
            for name in directories:
                path = Path(here) / name
                if name == "__pycache__":
                    continue
                if path.is_symlink():
                    link(folder / name, path)
                elif name in _LINKED_DIRS:
                    (folder / name).symlink_to(path, target_is_directory=True)
                else:
                    kept.append(name)
            directories[:] = kept
            for name in names:
                path = Path(here) / name
                files += 1
                if files > MAX_COPIED_FILES:
                    raise ValueError(_TOO_LARGE)
                status = path.lstat()
                if path.is_symlink():
                    link(folder / name, path)
                    continue
                if not stat.S_ISREG(status.st_mode) or not os.access(path, os.R_OK):
                    (folder / name).symlink_to(path)
                    continue
                size += status.st_size
                if size > MAX_COPIED_BYTES:
                    raise ValueError(_TOO_LARGE)
                # Bytes and mode only: no flags (a locked file could not be removed).
                shutil.copyfile(path, folder / name, follow_symlinks=False)
                shutil.copymode(path, folder / name, follow_symlinks=False)
                if (status.st_dev, status.st_ino) == (program.st_dev, program.st_ino):
                    staged = folder / name
        if staged is None:
            raise ValueError("A Python candidate's --at file is not in the copied root")
        return staged

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def source_path(self, original):
        return self.staged if Path(original).resolve() == self.original else original

    def launch_args(self, program, root, args=()):
        if Path(program).resolve() != self.original:
            # `--run` would be the user's entry script, outside the copy.
            raise ValueError(
                "A Python candidate runs the edited --at file itself; with --run, the entry "
                "script would import the original module. Probe the edited file directly, "
                "or check the edit with `peeky verify`"
            )
        return self.adapter.launch_args(self.staged, self.root, args)

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
