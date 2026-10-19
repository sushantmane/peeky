"""Runtime-specific DAP configuration selected by trusted worker code.

Adapter commands and launch configuration are not accepted from HTTP clients.
Python runs directly; compiled runtimes require prepared build provenance.
"""

import ast
import os
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
            try:
                self.staged = self._copy(root, self.root, relative, os.stat(self.original))
            except OSError as error:
                raise ValueError(
                    f"A Python candidate could not copy the target root ({error}); check the "
                    "edit with `peeky verify`"
                ) from None
            # The edited file replaces its copy, and is never written through a link.
            for parent in self.staged.parents:
                if parent == self.root:
                    break
                if parent.is_symlink() or not parent.is_dir():
                    raise ValueError("A Python candidate's --at path leaves the copied root")
            os.unlink(self.staged)
            handle = os.open(self.staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW)
            os.fchmod(handle, stat.S_IMODE(os.stat(self.original).st_mode))
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text.replace(edit.old, edit.new, 1))
            # Other names of the same file (hard links) are the edited file too, and stay
            # hard links: a symlink would give a script run by that name (`python
            # backup/app.py`) the --at file's directory as `sys.path[0]` and `__file__`.
            for twin in self.twins:
                os.unlink(twin)
                try:
                    os.link(self.staged, twin)
                except OSError as error:
                    raise ValueError(
                        f"A Python candidate could not link {twin.name} to the edited file "
                        f"({error}); check the edit with `peeky verify`"
                    ) from None
        except BaseException:
            shutil.rmtree(self.scratch, ignore_errors=True)
            raise
        # Whatever the program reaches above the copied root is missing in the copy, and no
        # reading of its source can tell whether it does (`dirname(dirname(__file__))`,
        # `HERE.parent`, a helper module's `parents[2]`): say so whenever the --at file's
        # own directory is the root, as it is for a script given as the target.
        self.warnings = (
            [
                f"This Python candidate ran from a copy of {root.name or root}/, the --at "
                "file's own directory; nothing above it was copied, so anything the program "
                "reads or imports from above it is missing there and its values may differ "
                "for that reason, not the edit's. If it uses any, give the directory that "
                "holds them as the target (`peeky probe <project> --at "
                "<path/to/script.py>:<line>`)"
            ]
            if relative.parent == Path(".")
            else []
        )

    def _copy(self, source, destination, relative, program):
        """Copy ``source`` to ``destination``: real directories, and regular files' bytes
        and modes. Links point where the originals do (inside the root, into the copy;
        outside, at the resolved original). _LINKED_DIRS and anything not a regular file
        (sockets, FIFOs) are links to the originals; an unreadable file is an empty one
        with its mode. `__pycache__` is left out, so the copy compiles its own. Returns
        the copy of the --at file (``relative``, confirmed by ``program``'s identity) and
        keeps its other hard-linked names in ``self.twins``."""
        import unicodedata

        files = size = 0
        staged, self.twins = None, []

        def link(target, original):
            real = Path(os.path.realpath(original))
            if real == source or source in real.parents:
                inside = destination / real.relative_to(source)
                target.symlink_to(os.path.relpath(inside, target.parent))
            else:
                target.symlink_to(real)

        def unlistable(error):
            # A directory that cannot be listed would be missing from the copy.
            raise error

        for here, directories, names in os.walk(source, onerror=unlistable):
            folder = destination / Path(here).relative_to(source)
            folder.mkdir()
            # Names that differ only by case or Unicode form are one name on a volume that
            # ignores the difference (the temporary directory may): never merge them.
            seen = {}
            for name in (*directories, *names):
                key = unicodedata.normalize("NFC", name).casefold()
                if key in seen:
                    raise ValueError(
                        f"A Python candidate cannot copy {Path(here) / name}: {seen[key]!r} "
                        "beside it differs only by case or Unicode form; check the edit with "
                        "`peeky verify`"
                    )
                seen[key] = name
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
                if not stat.S_ISREG(status.st_mode):
                    (folder / name).symlink_to(path)
                    continue
                if not os.access(path, os.R_OK):
                    # Unreadable here too, and a write (`--w-------`) stays in the copy.
                    os.close(os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
                    shutil.copymode(path, folder / name, follow_symlinks=False)
                    continue
                size += status.st_size
                if size > MAX_COPIED_BYTES:
                    raise ValueError(_TOO_LARGE)
                # Bytes and mode only: no flags (a locked file could not be removed).
                shutil.copyfile(path, folder / name, follow_symlinks=False)
                shutil.copymode(path, folder / name, follow_symlinks=False)
                if (status.st_dev, status.st_ino) == (program.st_dev, program.st_ino):
                    if path.relative_to(source) == relative:
                        staged = folder / name
                    else:
                        self.twins.append(folder / name)
        if staged is None and len(self.twins) == 1:
            # The --at path as typed in another spelling: its one copy.
            staged, self.twins = self.twins[0], []
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
