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
from dataclasses import dataclass, field
from pathlib import Path

from peeky.processes import without_credentials
from peeky.textlines import split_lines

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
# Standard modules that open no file, start no process and reach no network (`sys` only
# through _PURE_SYS): a script that imports only these (see _self_contained) reaches
# nothing above the copied root.
_PURE_MODULES = frozenset(
    {
        "__future__",
        "abc",
        "array",
        "bisect",
        "cmath",
        "collections",
        "collections.abc",
        "copy",
        "dataclasses",
        "decimal",
        "enum",
        "fractions",
        "functools",
        "heapq",
        "itertools",
        "json",
        "math",
        "numbers",
        "random",
        "re",
        "statistics",
        "string",
        "sys",
        "textwrap",
        "typing",
        "unicodedata",
    }
)
# What a script may use of `sys`: its arguments, standard streams and limits.
_PURE_SYS = frozenset(
    {
        "argv",
        "stdin",
        "stdout",
        "stderr",
        "exit",
        "maxsize",
        "getrecursionlimit",
        "setrecursionlimit",
    }
)
# Names that reach files, code or attributes by text (`open`, `getattr(json, "codecs")`,
# `operator.attrgetter`, `string.Formatter().get_field`), or a frame's globals.
_IMPURE_NAMES = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "help",
        "attrgetter",
        "methodcaller",
        "get_field",
        "get_type_hints",
        "f_globals",
        "f_builtins",
        "f_locals",
        "f_back",
        "gi_frame",
        "cr_frame",
        "ag_frame",
        "tb_frame",
    }
)
# The opening of the note on a candidate's copy, which `probe` shows once.
COPY_NOTE = "This Python candidate ran from a copy of "


def _self_contained(text):
    """True only where the script ``text`` cannot reach anything outside its own file:
    it imports only _PURE_MODULES (none of their private names or submodules, and of
    `sys` only _PURE_SYS) and names nothing in _IMPURE_NAMES, `__file__` or another
    dunder (but `__name__` and `__init__`). False where unsure."""
    import importlib
    import types

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return False

    def public(module, name):
        if name.startswith("_") or name == "*" or name in _IMPURE_NAMES:
            return False
        if module == "sys":
            return name in _PURE_SYS
        try:
            found = getattr(importlib.import_module(module), name, None)
        except ImportError:
            return False
        return not isinstance(found, types.ModuleType)

    modules, bases = {}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in _PURE_MODULES or (alias.asname is None and "." in alias.name):
                    return False
                modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in _PURE_MODULES:
                return False
            for alias in node.names:
                if node.module != "__future__" and not public(node.module, alias.name):
                    return False
        elif isinstance(node, ast.Attribute):
            if node.attr in _IMPURE_NAMES or (
                node.attr.startswith("__") and node.attr not in ("__init__", "__name__")
            ):
                return False
            if isinstance(node.value, ast.Name):
                bases.add(id(node.value))
        elif isinstance(node, ast.Name) and (
            node.id in _IMPURE_NAMES or (node.id.startswith("__") and node.id != "__name__")
        ):
            return False
    for node in ast.walk(tree):
        # A module is used only as `module.public_name`, never passed or rebound.
        if isinstance(node, ast.Name) and node.id in modules and id(node) not in bases:
            return False
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
            and not public(modules[node.value.id], node.attr)
        ):
            return False
    return True


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
        from peeky.build import private_base

        def outside(base):
            if base.resolve().is_relative_to(root):
                raise ValueError(
                    f"A Python candidate is copied outside its target, and {base} is inside "
                    f"{root}: set TMPDIR to a directory only you can write, outside it"
                )

        # Where no other user can write: a program that reaches above its root (`../`,
        # `Path(__file__).parents[2]`) finds nothing of theirs there.
        self.scratch = Path(tempfile.mkdtemp(prefix="peeky-candidate-", dir=private_base(outside)))
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
            # The copy is the prober's, with the originals' mode bits: access that comes
            # from elsewhere (another owner, an ACL, a locked flag) is not copied.
            differ = [
                original
                for original, copy in self._pairs
                if any(
                    os.access(original, mode) != os.access(copy, mode)
                    for mode in (os.R_OK, os.W_OK, os.X_OK)
                )
            ]
            # Root's access ignores owners; a change of owner shows elsewhere (git's
            # `safe.directory`, a program that checks `st_uid`).
            me = os.geteuid()
            foreign = [original for original, _ in self._pairs if os.lstat(original).st_uid != me]
            if foreign:
                named = ", ".join(str(path.relative_to(root)) or "." for path in foreign[:3])
                more = f" and {len(foreign) - 3} more" if len(foreign) > 3 else ""
                self._notes.append(
                    f"This Python candidate's copy is yours, but {named}{more} in your tree "
                    "belong to another user, so a program that checks who owns them (git's "
                    "`safe.directory`) may take another branch there, not because of the edit"
                )
            if differ:
                named = ", ".join(str(path.relative_to(root)) for path in differ[:3])
                more = f" and {len(differ) - 3} more" if len(differ) > 3 else ""
                self._notes.append(
                    f"This Python candidate's copy of {named}{more} has other permissions than "
                    "yours (an owner, an ACL or a locked flag is not copied), so a program that "
                    "uses them may take another branch there, not because of the edit"
                )
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
        # A script that reaches nothing outside its own file needs no note (a candidate
        # runs the --at file itself: launch_args).
        if (above is not None or not project) and not self._self_contained(
            text.replace(edit.old, edit.new, 1)
        ):
            target, at = (above, self.original.relative_to(above)) if above else (None, None)
            self.warnings.append(
                f"{COPY_NOTE}{root}/ only"
                + (
                    f", inside the repository at {above}/"
                    if above
                    else " (no .git, pyproject.toml, setup.py or setup.cfg there)"
                )
                + ". If the program reads, imports or runs anything above it, as git does to "
                "find the repository, give that directory as the target (`peeky probe "
                + (
                    f"{target} --at {at.as_posix()}"
                    if above
                    else "<project> --at <path/to/file.py>"
                )
                + ":<line>`): nothing above was copied, so values may differ for that reason, "
                "not the edit's"
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
        self._folders, self._notes, self._checkouts, self._pairs = [], [], [], []
        self._source, self._destination = source, destination
        status = os.stat(source)
        self._identity = (status.st_dev, status.st_ino)

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
            self._pairs.append((Path(here), folder))
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
                    self._pairs.append((path, folder / name))
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
                self._pairs.append((path, folder / name))
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
        """``real`` relative to the root, or None. A path that spells the root otherwise
        is inside too when one of its directories is the root itself (by device and
        inode): a volume that ignores case or Unicode form (macOS, `/tmp/VE/Proj` typed,
        an editable install's `/tmp/ve/proj` recorded), or a case-sensitive volume
        mounted under such a path (`/Volumes/Dev` named `/Volumes/dev`)."""
        real = Path(real)
        try:
            return real.relative_to(self._source)
        except ValueError:
            pass
        for depth, ancestor in enumerate((real, *real.parents)):
            try:
                status = os.stat(ancestor)
            except OSError:
                continue  # `worktrees/*/gitdir` may name a path that no longer exists.
            if (status.st_dev, status.st_ino) == self._identity:
                return Path(*real.parts[len(real.parts) - depth :]) if depth else Path(".")
        return None

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
            # Git may refuse the user's repository (`safe.directory`: another user owns
            # it) but not the copy, which is the prober's.
            if _git(git, original, "rev-parse", "--git-dir") is None:
                if _git(git, copy, "rev-parse", "--git-dir") is not None:
                    note = (
                        "Git refuses your repository here (another user owns it, which "
                        "`safe.directory` checks) but not this Python candidate's copy, "
                        "which is yours, so a program that runs git gets results it does "
                        "not get in your tree"
                    )
                    if note not in self._notes:
                        self._notes.append(note)
                _git(git, copy, "-c", "core.fsmonitor=false", "update-index", "-q", "--refresh")
                continue
            theirs, ours = _config_values(git, original), _config_values(git, copy)
            if theirs is not None and ours is not None and theirs != ours:
                note = (
                    "This Python candidate's copy of a git repository reads other git "
                    'configuration than yours, since an `includeIf "gitdir:..."` on the '
                    "repository's path does not match the copy: a program that reads git's "
                    "configuration may see other values"
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
        whatever the root offers is looked up by name (`find_spec`, never imported) as
        the program would find it, and a note names each one still served from the
        user's tree (an import hook ahead of every path, as meson-python's editable
        loader is)."""
        script = (
            # `sys` first: `-c` puts the copy's root first on the path, where a module
            # named like the standard library's (`json.py`) would run before the program.
            "import sys\nif sys.path and sys.path[0] == '':\n    del sys.path[0]\n"
            "import json\nmapped = []\nfor finder in sys.meta_path:\n"
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
            # As debugpy: the script's directory first, in place of `-c`'s `''` (none
            # under `PYTHONSAFEPATH`, where the first entry is `PYTHONPATH`'s), and only
            # after the probe's own imports, which must not find the copy's modules.
            "import sys\nif sys.path and sys.path[0] == '':\n    del sys.path[0]\n"
            "import importlib.util, json\n"
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
                    "not the copy, so values from it are not the edit's: check the edit with "
                    "`peeky verify`. An import hook or install, such as meson-python's editable "
                    "loader, serves it ahead of every import path, and may rebuild there"
                )

    def _self_contained(self, text):
        """_self_contained, as the program's interpreter imports the standard modules;
        False where it cannot say."""
        from peeky.python_interpreter import Unavailable

        try:
            return (
                self.adapter.readings("self_contained", text, timeout=self._interpreter_timeout())
                is True
            )
        except (Unavailable, ValueError):
            return False

    def _interpreter_timeout(self):
        if self._deadline is None:
            return 120
        # Leave the debugger's launch its share of the budget.
        return min(120, max(1.0, self._deadline - time.monotonic() - 15))

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
        environment = without_credentials(os.environ)
        environment.pop("PYTHONPATH", None)
        if pythonpath:
            environment["PYTHONPATH"] = pythonpath
        timeout = self._interpreter_timeout()
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

    def entry_lines(self, program, lines):
        """As PythonAdapter.entry_lines, of the edited copy that runs."""
        return self.adapter.entry_lines(self.source_path(program), lines)

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
        try:
            _make_removable(self.scratch)
            shutil.rmtree(self.scratch, ignore_errors=True)
        finally:
            # The launch's readings end with it.
            self.adapter.close()


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
        for key, value in without_credentials(os.environ).items()
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


def _gitdir(path):
    """Where a `.git` file's `gitdir:` line points, resolved; None if it has none."""
    try:
        with open(path, encoding="utf-8") as stream:
            line = split_lines(stream.read(4096))[0]
    except (OSError, UnicodeDecodeError, IndexError):
        return None
    if not line.startswith("gitdir:") or not line[7:].strip():
        return None
    return Path(os.path.realpath(path.parent / line[7:].strip()))


class ViewBudgetExhausted(ValueError):
    """A bounded value view stopped early; distinct from an invalid debugger shape."""


# debugpy's grouping nodes (pydevd's DAPGrouper, typed ""): members of the listing they
# sit in, filed by kind, never values of the program.
PYDEVD_GROUPS = frozenset(
    {"special variables", "function variables", "class variables", "protected variables"}
)
# pydevd's paging nodes for long sequences (`more`, `[100:1100]`), made per listing.
_PYDEVD_PAGES = frozenset({"MoreItems", "MoreItemsRange"})


def locals_only(scopes):
    """The scopes holding the stopped function's own variables."""
    return [
        scope
        for scope in scopes
        if not scope.get("expensive") and scope.get("presentationHint") == "locals"
    ]


# Code that runs in a frame of its own inside a function (a comprehension before Python
# 3.12): python_entry_lines does not watch its calls.
_PYTHON_INNER_SCOPES = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _docstring(statement):
    """Whether a function's first statement is its docstring, which runs no code."""
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _resumed(function):
    """Whether a function's calls are resumed part-way through: an `async def`, or one
    whose own code (not a nested function's, lambda's or class's) yields or awaits."""
    if isinstance(function, ast.AsyncFunctionDef):
        return True
    pending = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await)):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        pending.extend(ast.iter_child_nodes(node))
    return False


def python_entry_lines(program, lines, optimize=0):
    """{line: the first line of the function that holds it}: its first
    statement after any docstring, which each call runs first; the module's first
    statement at module level. A probe watches it, so a value is restored only in
    the call that changed it. ValueError where calls cannot be seen begin there: a
    generator or coroutine (each resumption runs on part-way through it), a lambda,
    comprehension or class body; or, at the program's ``optimize`` level above 0, where
    that line has no code (`-O` leaves out an `assert`)."""
    path = Path(program)
    try:
        # Bytes: the source's own encoding declaration decides how it reads.
        data = path.read_bytes()
        tree = ast.parse(data, str(path))
        coded = None
        if optimize:
            from peeky.python_scopes import code_lines

            coded = code_lines(data, str(path), optimize)
    except (OSError, SyntaxError, ValueError) as exc:
        raise ValueError(f"Peeky could not read {path.name} ({str(exc)[:200]})") from None
    found = {}
    for line in lines:
        holder, inner = tree, None
        for node in ast.walk(tree):
            start, end = getattr(node, "lineno", None), getattr(node, "end_lineno", None)
            if start is None or end is None or not start <= line <= end:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # The body's lines, not a decorator's or the signature's.
                if (
                    node.body
                    and node.body[0].lineno <= line
                    and (holder is tree or node.lineno > holder.lineno)
                ):
                    holder = node
            elif isinstance(node, _PYTHON_INNER_SCOPES) and inner is None:
                inner = node
        if inner is not None:
            raise ValueError(
                f"line {line} is in a {type(inner).__name__.lower()} (line {inner.lineno}), "
                "whose calls Peeky does not watch"
            )
        if isinstance(holder, ast.ClassDef):
            raise ValueError(f"line {line} is in the body of class `{holder.name}`")
        if holder is not tree and _resumed(holder):
            raise ValueError(
                f"line {line} is in `{holder.name}`, a generator or coroutine: each "
                "resumption runs on part-way through it, so Peeky cannot tell its calls apart"
            )
        body = list(holder.body)
        if holder is not tree and body and _docstring(body[0]):
            body = body[1:]
        body = [s for s in body if not isinstance(s, (ast.Global, ast.Nonlocal))]
        if not body:
            raise ValueError(f"Peeky cannot place the first line of the code holding {line}")
        if coded is not None and body[0].lineno not in coded:
            raise ValueError(
                f"the first statement of the code holding line {line} (line "
                f"{body[0].lineno}) has no code at the program's optimization level "
                f"({optimize}: -O leaves out an assert), so Peeky cannot watch its calls"
            )
        found[line] = [body[0].lineno]
    return found


@dataclass(frozen=True)
class PythonAdapter:
    # The program's interpreter (python_interpreter): debugpy's adapter runs on Peeky's own,
    # and runs the program on this one.
    interpreter: str = sys.executable
    # How it was chosen, and its version (python_interpreter.Interpreter.identity): the
    # run's `python` and target identity; None where not identified (an adapter made by
    # hand).
    identity: dict | None = field(default=None, compare=False)
    # debugpy's adapter exits 1 when Peeky ends its input as it answers `disconnect`: its
    # handler goes on to report its sockets (debugpy's own event) on the channel that end
    # closed, and fails. Its stderr (debugpy 1.8.22) shows each of these lines;
    # with all of them, the message it could not send was its own, not the program's.
    provoked_exit = (
        1,
        (
            "couldn't handle #",
            'request "disconnect" from Client',
            "in disconnect_request",
            "in report_sockets",
            "debugpy.common.messaging.NoMoreMessages: No more messages",
            "assert self.response is None",
            "AssertionError",
        ),
    )

    environment = None
    manifest_id = None
    source_manifest = None
    # Candidate probes (`probe --old/--new`) run an edited copy of the target root.
    supports_edits = True

    # A candidate asks the interpreter about imports within the launch's budget.
    supports_deadline = True

    @property
    def readings(self):
        """Its readings of the program's code (python_interpreter.Readings), closed with
        the launch (``close``). Not a field: made at the first reading, where this
        adapter's interpreter is asked, unless ``on`` identified it."""
        found = self.__dict__.get("_readings")
        if found is None:
            from peeky.python_interpreter import Readings

            found = Readings(self.interpreter)
            object.__setattr__(self, "_readings", found)
        return found

    @classmethod
    def on(cls, interpreter):
        """The adapter running a program on ``interpreter`` (a python_interpreter.Interpreter
        identified for this launch), whose readings are made as it was identified."""
        from peeky.python_interpreter import Readings

        made = cls(interpreter.path, interpreter.identity())
        object.__setattr__(made, "_readings", Readings(interpreter.path, interpreter))
        return made

    @property
    def target_identity(self):
        return None if self.identity is None else {"language": "python", **self.identity}

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

    @staticmethod
    def exact_text(variable, **where):
        """Whether the value a change wrote is one that is always the same object: True,
        False or None, as it was before (``variable``, read before the write; a literal
        that reads the same is that object too). Any other value may be a new object
        alike (`1000`, a text)."""
        return isinstance(variable, dict) and (variable.get("type"), variable.get("value")) in (
            ("bool", "True"),
            ("bool", "False"),
            ("NoneType", "None"),
        )

    @staticmethod
    def restore_literal(variable, before, **where):
        """The literal a probe's `@before` writes back: the text read just before
        a change, where it is all of the value: an int, a float, True, False or None
        whose text reads back as that very value (`repr` of what it parses to). Writing
        it makes an equal value; an int past 256 or a float is a new object, which `is`
        tells apart. A text, a container or another object: ValueError."""
        kind = variable.get("type") if isinstance(variable, dict) else None
        if kind not in ("int", "float", "bool", "NoneType"):
            raise ValueError(
                f"it holds {('a ' + kind) if kind else 'a value'}: Peeky writes back only an "
                "int, a float, True, False or None, whose text is all of its value"
            )
        try:
            value = ast.literal_eval(before)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            value = None
            parsed = False
        else:
            parsed = True
        if (
            not parsed
            or type(value).__name__ != kind
            or repr(value) != before
            or (kind == "float" and value != value)
            or (kind == "float" and value in (float("inf"), float("-inf")))
        ):
            raise ValueError(
                f"it read {before[:80]!r}, which is not the whole {kind} as a literal Peeky "
                "can write back"
            )
        return before

    @staticmethod
    def storage_identity(variable, **where):
        """Which storage a change writes, as a `@before` restore compares it: a
        Python function has one local of each name, so its name, in the call proven the
        same. Only locals change in Python."""
        return f"the local `{(variable or {}).get('name')}`"

    def entry_lines(self, program, lines):
        """{line: the first line of the function that holds it} (python_entry_lines), as
        the program's interpreter parses its source; ValueError where calls cannot be
        seen to begin there, or that interpreter could not read it."""
        from peeky.python_interpreter import Unavailable

        try:
            found = self.readings("entry_lines", str(program), list(lines))
        except Unavailable as exc:
            raise ValueError(
                f"Peeky could not read {Path(program).name} in the program's interpreter ({exc})"
            ) from None
        return {line: first for line, first in found}

    def close(self):
        """End the launch's readings (and their helper)."""
        if (found := self.__dict__.get("_readings")) is not None:
            found.close()

    @property
    def command(self):
        # Peeky's own debugpy: the program's interpreter need not have it.
        return (sys.executable, "-m", "debugpy.adapter")

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
            # A debugged run writes no __pycache__ into the root (PROGRAM_ENV).
            "env": {"PYTHONDONTWRITEBYTECODE": "1"},
            # debugpy runs its launcher and the program on it, from Peeky's own install.
            **({"python": [self.interpreter]} if self.interpreter != sys.executable else {}),
        }

    # Listing nodes the worker walks through without counting them as a level of depth.
    variable_groups = PYDEVD_GROUPS
    # Groups debugpy files a value under by its type (a function, a class): a literal
    # `--set` never makes one, so the changed local is listed directly afterwards.
    value_groups = frozenset({"function variables", "class variables"})

    def local_scopes(self, scopes):
        """The frame's Locals, then debugpy's Globals: the module's globals, read-only and
        listed only on request (the worker's `globals`)."""
        return locals_only(scopes) + [scope for scope in scopes if self.global_scope(scope)]

    @staticmethod
    def global_scope(scope):
        return (
            scope.get("name") == "Globals"
            and not scope.get("presentationHint")
            and not scope.get("expensive")
        )

    @staticmethod
    def object_identity(variable):
        """Which object a value is: debugpy's number for it. pydevd numbers each object
        by `id(value)` (one variablesReference per id, never reused for another id while
        debugpy runs), and keeps every object it lists alive while the program is
        stopped. So within one stop, equal numbers are the same object; across stops, an
        equal number may also be a new object at a freed one's address. The number alone
        identifies it (`obj.__class__ = Other` keeps it). Scalars (reference 0) have
        none, nor do debugpy's own grouping and paging nodes."""
        reference, kind = variable.get("variablesReference"), variable.get("type")
        if type(reference) is not int or reference <= 0 or not isinstance(kind, str) or not kind:
            return None
        if kind in _PYDEVD_PAGES or variable.get("name") in PYDEVD_GROUPS:
            return None
        return reference

    # The worker passes when the program started (`loaded_by`, a time.time()) to the
    # rules that read its sources: a source changed after it proves nothing.
    judges_sources = True

    def not_yet_declared(
        self,
        variable,
        frame,
        innermost=True,
        stack=None,
        deeper=None,
        siblings=None,
        stop=None,
        loaded_by=None,
    ):
        """A local shown with a note: one the code of the current pass of the loop
        around the frame's line has not assigned before the line, and assigns on every
        path through the rest of the pass before reading it, so its value is likely an
        earlier pass's (see python_passes); else None. Its string says why a change to
        it is refused. Judged by the program's interpreter (python_interpreter), and none
        where it cannot judge; a breakpoint's stop is where its line begins."""
        from peeky.python_interpreter import Unavailable
        from peeky.python_passes import PassNote

        frame = frame or {}
        try:
            found = self.readings(
                "stale_note",
                (frame.get("source") or {}).get("path"),
                str(frame.get("name") or ""),
                frame.get("line"),
                variable.get("name"),
                loaded_by,
                bool(innermost and stop == "breakpoint"),
            )
        except (Unavailable, ValueError):
            # A note is only ever added: code its interpreter did not read gets none.
            return None
        return None if found is None else PassNote(*found)

    def validate_mutation_target(self, variable, frame, loaded_by=None):
        """Refuse a change the code overwrites before reading it, judged as at any
        stop on the line (a set is never told where the stop is)."""
        if (stale := self.not_yet_declared(variable, frame, loaded_by=loaded_by)) is not None:
            raise ValueError(f"{variable.get('name')!r} cannot be changed at this stop: {stale}")

    def globals_proof(self, frame, listed, names, loaded_by=None):
        """How the frame's function sees each of ``names``, missing from its locals:
        `{"names": {name: null or {"kind", "owner"}}}`, null meaning it reads that name
        from its module's globals (see python_scopes.classify, as the program's
        interpreter makes its scopes); or `{"unproven": reason}`. ``loaded_by``: when the
        program started (a source changed since may not be the code running)."""
        from peeky.python_interpreter import Unavailable

        path, line = (frame.get("source") or {}).get("path"), frame.get("line")
        if not isinstance(path, str) or not path or type(line) is not int:
            return {"unproven": "the debugger gave no source line for this frame"}
        try:
            found, reason = self.readings(
                "classify",
                path,
                str(frame.get("name") or ""),
                line,
                sorted(names),
                sorted(listed),
                loaded_by,
            )
        except (Unavailable, ValueError) as exc:
            return {"unproven": f"the program's interpreter could not read its scopes ({exc})"}
        if found is None:
            return {"unproven": reason}
        return {
            "names": {
                name: None if how is None else {"kind": how[0], "owner": how[1]}
                for name, how in found.items()
            }
        }

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
