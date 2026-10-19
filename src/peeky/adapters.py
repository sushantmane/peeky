"""Runtime-specific DAP configuration selected by trusted worker code.

Adapter commands and launch configuration are not accepted from HTTP clients.
Python runs directly; compiled runtimes require prepared build provenance.
"""

import ast
import contextlib
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


# A Python candidate copies its target root. Environments are not copied: large, not the
# program's own code, and the interpreter uses them from either tree. Each is a directory
# of links to the original's entries (a link itself would not match `venv/` in an ignore
# file).
_LINKED_DIRS = frozenset(
    {"node_modules", ".venv", "venv", ".tox", ".nox", ".eggs", "site-packages"}
)
# Caches whose tools rebuild them with the same results are left out (a link would let the
# program write the user's). `.pytest_cache` changes what pytest runs (`--lf`): copied.
_SKIPPED_DIRS = frozenset({"__pycache__", ".mypy_cache", ".ruff_cache"})
# Repositories are copied whole, following their links, so whatever the program does with
# them (`git add`, a rewritten index, new objects, even a touched object's mtime) stays in
# the copy; one that cannot be is left out, with a note.
_REPOSITORIES = {".git": "git", ".hg": "Mercurial (`hg`)", ".svn": "Subversion (`svn`)"}
# A root holding one of these is a project's: the candidate does not note what is above it.
_PROJECT_MARKERS = (".git", ".hg", "pyproject.toml", "setup.py", "setup.cfg")
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
            # Created now, so its mtime is the edit's, as a saved edit's would be.
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
            # Directories last, deepest first: their writes are done, and a read-only one
            # (`r-x`) stays read-only for the program.
            for folder, status in reversed(self._folders):
                os.utime(folder, ns=(status.st_atime_ns, status.st_mtime_ns))
                os.chmod(folder, stat.S_IMODE(status.st_mode))
        except BaseException:
            self.close()
            raise
        # Whatever the program reaches above the copied root is missing in the copy, and no
        # reading of its source can tell whether it does (`dirname(dirname(__file__))`,
        # `HERE.parent`, a helper module's `parents[2]`, git finding a repository above):
        # say so unless the root is a project's own, with its own repository if any.
        self.warnings = self._notes
        held = any(os.path.lexists(root / marker) for marker in (".git", ".hg"))
        above = None if held else _repository_above(root)
        if above is not None or not any(
            os.path.lexists(root / marker) for marker in _PROJECT_MARKERS
        ):
            target, at = (above, self.original.relative_to(above)) if above else (None, None)
            self.warnings.append(
                f"This Python candidate ran from a copy of {root}/ only"
                + (
                    f", inside the repository at {above}/"
                    if above
                    else ", which is not a "
                    "project root (it holds no .git, pyproject.toml, setup.py or setup.cfg)"
                )
                + ". Nothing above it was copied, so anything the program reads, imports or "
                "runs from above it (git finding the repository) is missing there, and its "
                "values may differ for that reason, not the edit's. If it uses any, give "
                "the directory that holds them as the target (`peeky probe "
                + (
                    f"{target} --at {at.as_posix()}"
                    if above
                    else "<project> --at <path/to/file.py>"
                )
                + ":<line>`)"
            )

    def _copy(self, source, destination, relative, program):
        """Copy ``source`` to ``destination``: real directories, and regular files' bytes,
        modes and times (directories' are set by the caller, from ``self._folders``).
        Links point where the originals do (inside the root, into the copy; outside, at
        the resolved original). Anything not a regular file (sockets, FIFOs) is a link to
        the original, and _LINKED_DIRS are directories of links to theirs; an unreadable
        file is an empty one with its mode, and a directory that can be neither listed
        nor entered an empty one. _SKIPPED_DIRS are left out, so the copy compiles its
        own bytecode. _REPOSITORIES are private whole copies (`_repository`), or left out
        with a note in ``self._notes``. Returns the copy of the --at file (``relative``,
        confirmed by ``program``'s identity) and keeps its other hard-linked names in
        ``self.twins``."""
        import unicodedata

        files = size = 0
        staged, self.twins = None, []
        self._folders, self._notes = [], []
        self._source, self._destination = source, destination

        def link(target, original):
            real = Path(os.path.realpath(original))
            if self._inside(real):
                target.symlink_to(
                    os.path.relpath(destination / real.relative_to(source), target.parent)
                )
            else:
                target.symlink_to(real)

        def unlistable(error):
            # A directory that cannot be listed would be missing from the copy.
            raise error

        for here, directories, names in os.walk(source, onerror=unlistable):
            folder = destination / Path(here).relative_to(source)
            folder.mkdir()
            self._folders.append((folder, os.lstat(here)))
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
                if name in _SKIPPED_DIRS:
                    continue
                if name in _REPOSITORIES:
                    self._repository(path, folder / name)
                elif path.is_symlink():
                    link(folder / name, path)
                elif name in _LINKED_DIRS:
                    self._link_entries(path, folder / name)
                elif not os.access(path, os.R_OK) and not os.access(path, os.X_OK):
                    # The program can neither list nor enter it either (another user's
                    # `pgdata`, `lost+found`): an empty one with its mode stands in.
                    (folder / name).mkdir()
                    self._folders.append((folder / name, path.lstat()))
                else:
                    kept.append(name)
            directories[:] = kept
            for name in names:
                path = Path(here) / name
                files += 1
                if files > MAX_COPIED_FILES:
                    raise ValueError(_TOO_LARGE)
                status = path.lstat()
                if name == ".git":
                    # A worktree's or submodule's `gitdir:` pointer (or a link to one):
                    # kept only when it names git metadata inside the root, which the copy
                    # holds privately.
                    target = _gitdir(path)
                    if target is None or not self._inside(target):
                        self._leave_out(path, "git", "it names git metadata outside the root")
                        continue
                    copied = os.path.relpath(destination / target.relative_to(source), folder)
                    (folder / name).write_text(f"gitdir: {copied}\n")
                    status = os.stat(path)
                elif path.is_symlink():
                    link(folder / name, path)
                    continue
                elif not stat.S_ISREG(status.st_mode):
                    (folder / name).symlink_to(path)
                    continue
                elif not os.access(path, os.R_OK):
                    # Unreadable here too, and a write (`--w-------`) stays in the copy.
                    os.close(os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
                else:
                    size += status.st_size
                    if size > MAX_COPIED_BYTES:
                        raise ValueError(_TOO_LARGE)
                    # Bytes, mode and times: no flags (a locked file could not be removed).
                    shutil.copyfile(path, folder / name, follow_symlinks=False)
                shutil.copymode(path, folder / name, follow_symlinks=False)
                os.utime(folder / name, ns=(status.st_atime_ns, status.st_mtime_ns))
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

    def _inside(self, real):
        return real == self._source or self._source in real.parents

    def _leave_out(self, path, what, reason):
        note = (
            f"This Python candidate's copy has no {what} repository "
            f"({path.relative_to(self._source)}): {reason}, so it has no private copy, and "
            "a link would let the program write yours. A program that runs it in the copy "
            "finds no repository there, and its values may differ for that reason"
        )
        if note not in self._notes:
            self._notes.append(note)

    def _link_entries(self, original, copy):
        """An environment: a directory whose entries are links to the original's."""
        try:
            entries = sorted(os.listdir(original))
        except OSError:
            copy.symlink_to(original, target_is_directory=True)
            return
        copy.mkdir()
        self._folders.append((copy, original.lstat()))
        for name in entries:
            (copy / name).symlink_to(original / name)

    def _repository(self, original, copy):
        """A private whole copy of repository metadata, following its links (`git-new-
        workdir` links `refs` and `config` to another repository's); else left out with a
        note. Git's `core.worktree` inside the root is moved to the copy's."""
        name = original.name
        mark = len(self._folders)
        try:
            budget = [MAX_COPIED_FILES, MAX_COPIED_BYTES]
            self._copy_whole(original, copy, budget, set())
            if name == ".git":
                self._own_git(copy)
            elif name == ".hg" and (copy / "sharedpath").exists():
                raise _NotPrivate("it shares another repository's store (`sharedpath`)")
        except (_NotPrivate, OSError) as error:
            del self._folders[mark:]
            _make_removable(copy)
            shutil.rmtree(copy, ignore_errors=True)
            reason = (
                str(error) if isinstance(error, _NotPrivate) else f"copying it failed ({error})"
            )
            self._leave_out(original, _REPOSITORIES[name], reason)

    def _copy_whole(self, original, copy, budget, ancestors):
        status = os.stat(original)
        if (status.st_dev, status.st_ino) in ancestors:
            raise _NotPrivate("its links form a loop")
        ancestors = ancestors | {(status.st_dev, status.st_ino)}
        copy.mkdir()
        self._folders.append((copy, status))
        for entry in sorted(os.listdir(original)):
            path, target = original / entry, copy / entry
            try:
                inner = os.stat(path)
            except FileNotFoundError:
                continue  # A broken link: missing here too.
            if stat.S_ISDIR(inner.st_mode):
                self._copy_whole(path, target, budget, ancestors)
                continue
            if not stat.S_ISREG(inner.st_mode):
                continue  # A socket (`fsmonitor--daemon.ipc`) would reach the user's daemon.
            budget[0] -= 1
            budget[1] -= inner.st_size
            if budget[0] < 0 or budget[1] < 0:
                raise _NotPrivate(
                    f"it is over {MAX_COPIED_FILES} files or {MAX_COPIED_BYTES >> 20} MB"
                )
            shutil.copyfile(path, target)
            shutil.copymode(path, target)
            os.utime(target, ns=(inner.st_atime_ns, inner.st_mtime_ns))

    def _own_git(self, copy):
        """Refuse a git directory that would still reach another (objects borrowed through
        `alternates`, a `core.worktree` outside the root), and move a `core.worktree`
        inside the root to the copy's."""
        for here, _, names in os.walk(copy):
            for name in names:
                path = Path(here) / name
                if name == "alternates" and path.parent.name == "info" and path.read_text().strip():
                    raise _NotPrivate("it borrows objects from another repository (`alternates`)")
                if name not in ("config", "config.worktree"):
                    continue
                lines = path.read_text(errors="surrogateescape").splitlines(keepends=True)
                for number, value in _core_worktrees(lines):
                    if not os.path.isabs(value):
                        continue
                    real = Path(os.path.realpath(value))
                    if not self._inside(real):
                        raise _NotPrivate("its `core.worktree` is outside the root")
                    moved = self._destination / real.relative_to(self._source)
                    lines[number] = f"\tworktree = {moved}\n"
                    path.write_text("".join(lines), errors="surrogateescape")

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
        _make_removable(self.scratch)
        shutil.rmtree(self.scratch, ignore_errors=True)


class _NotPrivate(Exception):
    """Repository metadata the candidate cannot copy privately (the reason)."""


def _make_removable(top):
    # Copied directories keep the originals' modes: make each removable before removing.
    for here, directories, _ in os.walk(top):
        for name in directories:
            path = os.path.join(here, name)
            if not os.path.islink(path):
                with contextlib.suppress(OSError):
                    os.chmod(path, 0o700)


def _repository_above(root):
    """The nearest directory above ``root`` holding a repository, if any."""
    for parent in root.parents:
        if any(os.path.lexists(parent / marker) for marker in (".git", ".hg")):
            return parent
    return None


def _core_worktrees(lines):
    """(line number, value) of each `worktree` key in a git config's `[core]` sections."""
    section = None
    for number, line in enumerate(lines):
        text = line.strip()
        if text.startswith("["):
            section = text[1 : text.find("]")].strip().lower() if "]" in text else None
            continue
        key, equals, value = text.partition("=")
        if section == "core" and equals and key.strip().lower() == "worktree":
            value = value.split(" #")[0].split(" ;")[0].strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            yield number, value


def _gitdir(path):
    """Where a `.git` file's `gitdir:` line points, resolved; None if it has none."""
    try:
        with open(path, encoding="utf-8") as stream:
            line = stream.read(4096).splitlines()[0]
    except (OSError, UnicodeDecodeError, IndexError):
        return None
    if not line.startswith("gitdir:") or not line[7:].strip():
        return None
    return Path(os.path.realpath(path.parent / line[7:].strip()))


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
