"""Runtime-specific DAP configuration selected by trusted worker code.

Adapter commands and launch configuration are not accepted from HTTP clients.
Python runs directly; compiled runtimes require prepared build provenance.
"""

import ast
import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
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
    `__file__`, multiprocessing's spawn, an editable install's path) is the copy, so all
    of it runs the edit. Files above the root are not copied. The program's writes stay in
    the copy, except inside an environment directory or through a link that leaves the
    root, which reach the originals."""

    def __init__(self, adapter, program, root, edit, deadline=None):
        self.adapter = adapter
        self._deadline = deadline
        self.original = Path(program).resolve(strict=True)
        root = Path(root).resolve(strict=True)
        relative = self.original.relative_to(root)
        for parent in self.original.parents:
            if parent == root:
                break
            if _is_git_directory(parent):
                # Copied whole as a private repository, never walked as files.
                raise ValueError(
                    "A Python candidate's --at file is inside a git repository's own "
                    f"directory ({parent.relative_to(root)}); give that directory as the "
                    f"target (`peeky probe {parent} --at "
                    f"{self.original.relative_to(parent).as_posix()}:<line>`)"
                )
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
            # Before the edit, so the copied repositories see the copy as unchanged.
            self._finish_git()
            self._pythonpath = self._redirect_imports()
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
        held = _is_git_directory(root) or any(
            os.path.lexists(root / marker) for marker in (".git", ".hg")
        )
        above = None if held else _repository_above(root)
        # A repository's own directory (a bare one, `.git` given as the target, as the
        # refusal of an --at file inside one suggests) is its own project.
        project = _is_git_directory(root) or any(
            os.path.lexists(root / marker) for marker in _PROJECT_MARKERS
        )
        if above is not None or not project:
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
        self._folders, self._notes, self._checkouts = [], [], []
        self._source, self._destination = source, destination
        self._folded = [_fold(part) for part in source.parts]
        swapped = Path(str(source).swapcase())
        try:
            self._folding = swapped != source and os.path.samefile(source, swapped)
        except OSError:
            self._folding = False

        def link(target, original):
            real = Path(os.path.realpath(original))
            if self._inside(real):
                target.symlink_to(os.path.relpath(destination / self._within(real), target.parent))
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
                    if self._repository(path, folder / name) and name == ".git":
                        self._checkouts.append((folder, Path(here)))
                elif _is_git_directory(path) and not path.is_symlink():
                    # A git directory under another name (a bare repository, `.bare` that
                    # `gitdir: ./.bare` names, one worktrees name through `commondir`):
                    # its `worktrees/*/gitdir` name worktrees `git worktree repair` writes.
                    self._repository(path, folder / name, ".git")
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
                    copied = os.path.relpath(destination / self._within(target), folder)
                    (folder / name).write_text(f"gitdir: {copied}\n")
                    self._checkouts.append((folder, Path(here)))
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
        if _is_git_directory(source):
            # The root is a (bare) repository itself: copied as files, which cannot be left
            # out, so its pointers are moved to the copy or the candidate is refused.
            try:
                self._own_git(source, destination)
            except (_NotPrivate, subprocess.SubprocessError) as error:
                raise ValueError(
                    f"A Python candidate cannot copy this git repository privately ({error}); "
                    "check the edit with `peeky verify`"
                ) from None
        if staged is None and len(self.twins) == 1:
            # The --at path as typed in another spelling: its one copy.
            staged, self.twins = self.twins[0], []
        if staged is None:
            raise ValueError("A Python candidate's --at file is not in the copied root")
        return staged

    def _inside(self, real):
        return self._within(real) is not None

    def _within(self, real):
        """``real`` relative to the root, or None. On a volume that ignores case and
        Unicode form (macOS), a path spelled differently (`/tmp/VE/Proj` typed, an
        editable install's `/tmp/ve/proj` recorded) is inside too."""
        real = Path(real)
        try:
            return real.relative_to(self._source)
        except ValueError:
            pass
        if not self._folding:
            return None
        count = len(self._source.parts)
        if [_fold(part) for part in real.parts[:count]] != self._folded:
            return None
        return Path(*real.parts[count:]) if len(real.parts) > count else Path(".")

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

    def _repository(self, original, copy, kind=None):
        """A private whole copy of repository metadata, following its links (`git-new-
        workdir` links `refs` and `config` to another repository's); else left out with a
        note. Git's `core.worktree` inside the root is moved to the copy's."""
        name = kind or original.name
        mark = len(self._folders)
        try:
            budget = [MAX_COPIED_FILES, MAX_COPIED_BYTES]
            self._copy_whole(original, copy, budget, set())
            if name == ".git":
                self._own_git(original, copy)
            elif name == ".hg" and (copy / "sharedpath").exists():
                raise _NotPrivate("it shares another repository's store (`sharedpath`)")
            return True
        except (_NotPrivate, OSError, subprocess.SubprocessError) as error:
            del self._folders[mark:]
            _make_removable(copy)
            shutil.rmtree(copy, ignore_errors=True)
            reason = (
                str(error) if isinstance(error, _NotPrivate) else f"copying it failed ({error})"
            )
            self._leave_out(original, _REPOSITORIES[name], reason)
            return False

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

    def _own_git(self, original, copy):
        """Refuse a git directory that would still reach another (objects borrowed through
        `alternates`, a `core.worktree` outside the root), and move what names places
        inside the root to the copy: each `core.worktree`, and each linked worktree's
        `gitdir` (`git worktree repair` writes the worktree it names). A linked worktree
        outside the root is named by a path that does not exist, with a note."""
        git = shutil.which("git")
        for gitdir in _git_directories(copy):
            # Where this git directory is in the user's tree, links not yet followed.
            logical = original / gitdir.relative_to(copy)
            alternates = gitdir / "objects" / "info" / "alternates"
            if alternates.is_file() and alternates.read_text(errors="replace").strip():
                raise _NotPrivate("it borrows objects from another repository (`alternates`)")
            for config in (gitdir / "config", gitdir / "config.worktree"):
                if config.is_file():
                    self._move_core_worktree(git, config, logical)
            pointer = gitdir / "gitdir"
            if gitdir.parent.name == "worktrees" and pointer.is_file():
                value = pointer.read_text(errors="surrogateescape").strip()
                target = Path(os.path.realpath(logical / value))
                if self._inside(target):
                    moved = self._destination / self._within(target)
                else:
                    moved = self.scratch / "outside-worktrees" / gitdir.name / ".git"
                    note = (
                        "This Python candidate's copy of a git repository lists its linked "
                        "worktrees outside the root at paths that do not exist there (`git "
                        "worktree list` marks them prunable), so a program that reads them "
                        "sees that, not yours"
                    )
                    if note not in self._notes:
                        self._notes.append(note)
                pointer.write_text(f"{moved}\n", errors="surrogateescape")

    def _move_core_worktree(self, git, config, logical):
        if git is None:
            # Only git reads git's config reliably (quoting, `[core] worktree = ...`).
            if re.search(r"(?i)worktree", config.read_text(errors="replace")):
                raise _NotPrivate("its config may set `core.worktree`, and git is not installed")
            return
        values = _git_config(git, config, "--null", "--get-all", "core.worktree")
        if not values:
            return
        moved = []
        for value in values:
            real = Path(os.path.realpath(logical / value))
            if not self._inside(real):
                raise _NotPrivate("its `core.worktree` is outside the root")
            moved.append(str(self._destination / self._within(real)))
        _git_config(git, config, "--unset-all", "core.worktree")
        for value in moved:
            _git_config(git, config, "--add", "core.worktree", value)

    def _finish_git(self):
        """For each checkout with a private git repository: the copy's index records the
        copied files' new identities (`git diff-index` would call every file changed),
        and a note says when its configuration differs from the user's (a conditional
        include on the repository's path, `includeIf "gitdir:..."`)."""
        git = shutil.which("git")
        if git is None:
            return
        for copy, original in self._checkouts:
            theirs, ours = _config_values(git, original), _config_values(git, copy)
            if theirs is not None and ours is not None and theirs != ours:
                note = (
                    "This Python candidate's copy of a git repository reads different git "
                    "configuration than yours (a conditional include on the repository's "
                    'path, `includeIf "gitdir:..."`, does not match the copy), so a program '
                    "that reads git's configuration may see different values"
                )
                if note not in self._notes:
                    self._notes.append(note)
            _git(git, copy, "-c", "core.fsmonitor=false", "update-index", "-q", "--refresh")

    def _redirect_imports(self):
        """The `PYTHONPATH` that puts the copy first wherever the interpreter would import
        from inside the root by another way than the script's directory: `PYTHONPATH`
        entries (relative ones mean the user's root, the program's working directory),
        and an editable install's `.pth` path or finder (`pip install -e`, `uv sync`).
        Without it, `import pkg` would load the user's unedited module. Environment and
        site directories are links to the user's files either way: never moved. Then
        whatever the root offers is imported by name as the program would find it, and
        a note names each one still served from the user's tree (an import hook ahead of
        every path, as meson-python's editable loader is)."""
        script = (
            "import json, sys\nmapped = []\nfor finder in sys.meta_path:\n"
            "    try:\n"
            "        module = sys.modules.get(getattr(finder, '__module__', None) or '')\n"
            "        for table in (getattr(module, 'MAPPING', None),"
            " getattr(finder, '_redirections', None)):\n"
            "            if isinstance(table, dict):\n"
            "                mapped += [[k, v] for k, v in table.items()"
            " if isinstance(k, str) and isinstance(v, str)]\n"
            "    except Exception:\n"
            "        pass\n"
            "print(json.dumps({'path': sys.path[1:], 'mapped': mapped}))\n"
        )
        found = self._interpreter(script, os.environ.get("PYTHONPATH"))
        if found is None:
            return None
        try:
            entries = [str(entry) for entry in found["path"]]
            mapped = [(str(name), str(path)) for name, path in found["mapped"]]
        except (KeyError, TypeError, ValueError):
            return None

        def given_entry(entry):
            if not entry:
                return entry  # The working directory: the copy's root, as the user's root.
            absolute = entry if os.path.isabs(entry) else os.path.join(self._source, entry)
            moved = self._project_copy(absolute)
            if moved is not None:
                return moved
            return entry if os.path.isabs(entry) else os.path.realpath(absolute)

        raw = os.environ.get("PYTHONPATH", "")
        given = raw.split(os.pathsep) if raw else []
        redirected = [given_entry(entry) for entry in given]
        later, unmoved = [], set()
        for entry in entries:
            if os.path.isabs(entry) and (moved := self._project_copy(entry)) is not None:
                later.append(moved)
        for name, path in mapped:
            module = Path(path).parent if Path(path).name == "__init__.py" else Path(path)
            real = Path(os.path.realpath(module))
            if not self._inside(real):
                continue
            moved = self._project_copy(real.parent) if name in (real.name, real.stem) else None
            if moved is None:
                unmoved.add(name)
                self._notes.append(
                    f"This Python candidate cannot move an editable install's `{name}` (it "
                    f"maps to {module}) to the copy, so `import {name}` loads your unedited "
                    "module; check the edit with `peeky verify`"
                )
                continue
            later.append(moved)
        for moved in later:
            if moved in redirected:
                continue
            redirected.append(moved)
            # On the user's path these come after the standard library; here before it.
            names = _module_names(Path(moved)) & set(getattr(sys, "stdlib_module_names", ()))
            if names:
                self._notes.append(
                    f"This Python candidate puts {moved} ahead of the standard library on "
                    f"the import path (yours has it later), and it holds {', '.join(sorted(names))}, "
                    "which the standard library has too: an import of those may load another "
                    "module than in your run"
                )
        pythonpath = os.pathsep.join(redirected) if redirected != given else None
        self._check_imports(
            pythonpath if pythonpath is not None else raw or None, redirected, unmoved
        )
        return pythonpath

    def _check_imports(self, pythonpath, redirected, unmoved):
        """Find what each top-level name the root offers imports as, the way the program
        will (its script's directory first, its `PYTHONPATH`), and note each one served
        from the user's tree."""
        names = (
            _module_names(self._source) | _module_names(self._source / "src") | {self.original.stem}
        )
        # Only the copy's directories: names from elsewhere are not the user's tree.
        for entry in redirected:
            if entry and Path(entry).is_relative_to(self.root):
                names |= _module_names(Path(entry))
        package = self.original.parent
        while package != self._source and (package / "__init__.py").is_file():
            names.add(package.name)
            package = package.parent
        # Top-level names only: `find_spec("pkg.bak")` would import and run `pkg`.
        names = sorted(name for name in names - unmoved if name.isidentifier())
        script = (
            "import importlib.util, json, sys\n"
            # As debugpy: the script's directory first, in place of `-c`'s `''` (none
            # under `PYTHONSAFEPATH`, where the first entry is `PYTHONPATH`'s).
            "if sys.path and sys.path[0] == '':\n    del sys.path[0]\n"
            "sys.path.insert(0, sys.argv[1])\nfound = {}\n"
            "for name in sys.argv[2:]:\n"
            "    try:\n        spec = importlib.util.find_spec(name)\n"
            "    except Exception:\n        continue\n"
            "    if spec is None:\n        continue\n"
            "    places = [spec.origin] if isinstance(spec.origin, str) else []\n"
            "    places += [p for p in (spec.submodule_search_locations or [])"
            " if isinstance(p, str)]\n"
            "    found[name] = places\n"
            "print(json.dumps(found))\n"
        )
        found = self._interpreter(script, pythonpath, str(self.staged.parent), *names)
        for name, places in (found or {}).items():
            served = next(
                (
                    place
                    for place in places
                    if isinstance(place, str)
                    and os.path.isabs(place)
                    and self._project_copy(place) is not None
                ),
                None,
            )
            if served is not None:
                self._notes.append(
                    f"This Python candidate's `import {name}` loads {served} from your tree, "
                    "not the copy (an import hook or install serves it ahead of every import "
                    "path, as meson-python's editable loader does, and may rebuild there), so "
                    "values from it are not the edit's; check the edit with `peeky verify`"
                )

    def _project_copy(self, entry):
        """The copy of an absolute path inside the root and not in an environment or
        site directory (those are links to the user's files either way); else None."""
        within = self._within(Path(os.path.realpath(entry)))
        if within is None:
            return None
        if any(
            part in _LINKED_DIRS or part in ("site-packages", "dist-packages")
            for part in within.parts
        ):
            return None
        return str(self.root / within)

    def _interpreter(self, script, pythonpath, *args):
        """The JSON ``script`` prints, run by the program's interpreter in the copy with
        ``pythonpath``; None (with a note) if it cannot run."""
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        if pythonpath:
            environment["PYTHONPATH"] = pythonpath
        timeout = 120
        if self._deadline is not None:
            # Leave the debugger's launch its share of the budget.
            timeout = min(timeout, max(1.0, self._deadline - time.monotonic() - 15))
        try:
            return json.loads(
                subprocess.run(
                    [self.adapter.interpreter, "-c", script, *args],
                    cwd=self.root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=True,
                ).stdout
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            note = (
                "This Python candidate could not ask its interpreter where imports come from, "
                "so if the project is installed in editable mode, `import` by package name may "
                "load your unedited modules"
            )
            if note not in self._notes:
                self._notes.append(note)
            return None

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
        launch = self.adapter.launch_args(self.staged, self.root, args)
        if self._pythonpath is not None:
            launch["env"] = {**launch.get("env", {}), "PYTHONPATH": self._pythonpath}
        return launch

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


_GIT_DATA = frozenset({"refs", "logs", "objects", "hooks", "info", "lfs", "rr-cache"})


def _git_directories(top):
    """The git directories in a copied `.git`: itself, `modules/...` and `worktrees/...`
    (each holds `HEAD`), never a ref directory (`refs/remotes/origin/` holds `HEAD` too)."""
    found = []

    def visit(directory, inside_git):
        gitdir = (directory / "HEAD").is_file() and not inside_git
        if gitdir:
            found.append(directory)
        for entry in sorted(directory.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                visit(entry, gitdir and entry.name in _GIT_DATA or inside_git and not gitdir)

    visit(top, False)
    return found


def _git_environment():
    # Git variables that would point git at another repository than the one asked for.
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
        or key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")
    }


def _git(git, where, *args):
    """Git's output in ``where``, or None if it fails."""
    try:
        done = subprocess.run(
            [git, "-C", str(where), *args],
            capture_output=True,
            env=_git_environment(),
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return os.fsdecode(done.stdout) if done.returncode == 0 else None


def _git_config(git, config, *args):
    """`git config --file` on one file; ``--get-all`` gives its values ([] if none)."""
    done = subprocess.run(
        [git, "config", "--file", str(config), *args],
        capture_output=True,
        env=_git_environment(),
        timeout=60,
    )
    if "--get-all" in args:
        if done.returncode == 1:
            return []
        if done.returncode != 0:
            raise _NotPrivate("git could not read its config")
        return [os.fsdecode(value) for value in done.stdout.split(b"\0")[:-1]]
    if done.returncode not in (0, 5):
        raise _NotPrivate("git could not rewrite its config")
    return None


def _config_values(git, where):
    """Git's configuration entries in ``where``, but `core.worktree` (the copy's is moved),
    or None."""
    listed = _git(git, where, "config", "--list", "-z")
    if listed is None:
        return None
    return [
        entry
        for entry in listed.split("\0")
        if entry and entry.split("\n", 1)[0].lower() != "core.worktree"
    ]


def _fold(name):
    import unicodedata

    return unicodedata.normalize("NFC", name).casefold()


def _is_git_directory(path):
    """Whether ``path`` holds a git directory's own files (a bare repository's)."""
    try:
        return (
            (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir()
        )
    except OSError:
        return False


def _module_names(directory):
    """The top-level modules a directory on the import path offers: `.py` files,
    extension modules and packages (a directory without `__init__.py` never wins over a
    standard-library module, which comes first)."""
    try:
        entries = list(directory.iterdir()) if directory.is_dir() else []
    except OSError:
        return set()
    names = set()
    for entry in entries:
        if entry.suffix == ".py" or entry.suffix in (".so", ".pyd"):
            names.add(entry.name.split(".")[0])
        elif os.path.isfile(entry / "__init__.py"):
            names.add(entry.name)
    return names


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

    # A candidate asks the interpreter about imports within the launch's budget.
    supports_deadline = True

    def prepare(self, program, root, edit=None, *, deadline=None):
        if edit is None:
            return self
        return PythonCandidate(self, program, root, edit, deadline)

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
