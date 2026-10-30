"""The interpreter a Python program runs on, and Peeky's readings of its code there.

A Python program runs on its project's interpreter, so that it imports what its project
installs. That is `--python PATH`; else the root's virtual environment, `.venv` then
`venv` (a directory holding `pyvenv.cfg`); else the active one, `$VIRTUAL_ENV`, unless it
is Peeky's own; else the interpreter running Peeky. Only the root itself is looked in,
never a directory above it. `--python peeky` keeps Peeky's own. A run names the one that
ran it (`python`, and its `target_identity`). The interpreter is the project's code, as
the program is: it runs with no credentials in its environment, as the program does.
debugpy runs the program on it from Peeky's own install (its launch's `python`), so the
project needs no debugpy; debugpy's compiled speedups are built for Peeky's Python
version only, and another version runs its pure-Python tracer instead.

Peeky reads a program's code without running it: the scopes a name is read from
(python_scopes), the locals a loop's pass has not assigned yet (python_passes), the lines
and scopes `describe` lists, the first lines a probe watches for a call's start, the
code on a line that the debugger names alike. What the compiler makes of a source differs
between Python versions, builds and optimization levels, so each is read by the
program's interpreter, at the level the program runs at: its `sys.flags.optimize` asked
with the program's own environment (`PYTHONOPTIMIZE`), and passed to every compile. In
this process where the interpreter reports this one's compiler (implementation, exact
version and build, ABI flags, cache tag, magic number); else in a helper process of it,
which imports this Peeky by absolute path (`-I`) and reports its compiler first. No
reading made on one version is applied to another.

An interpreter is identified once per launch: what it reports, and where its path leads
(the file, its inode, size and mtime). Each reading checks the file again, and a reading
that can't be made is not remembered: the next one asks again. A launch's readings, and
their helper, are its adapter's, closed with the launch. The interpreter must be CPython
3.11 or later, as Peeky is; another one is refused before anything runs.
"""

import ast
import atexit
import importlib.util
import json
import os
import select
import shutil
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path

from peeky.processes import without_credentials
from peeky.textlines import split_lines

# `--python peeky`: the interpreter running Peeky.
OWN = "peeky"
MINIMUM = (3, 11)
# The root's virtual environments, in the order they are looked for.
ENVIRONMENTS = (".venv", "venv")
# How the interpreter was chosen (`chosen`), as the text names it.
CHOSEN = {
    "--python": "--python",
    ".venv": "the root's .venv",
    "venv": "the root's venv",
    "VIRTUAL_ENV": "the active virtual environment, $VIRTUAL_ENV",
    "peeky": "Peeky's own",
}
# What a launch adds to the program's environment (PythonAdapter.launch_args).
PROGRAM_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}
IDENTIFY_S = 15
# A reading the helper does not answer within this long is given up, and so is the helper.
READ_S = 30
# A request carries a source's text at most (python_scopes reads at most 1 MB of one).
MAX_REQUEST = 4 * 1024 * 1024
MAX_REPLY = 16 * 1024 * 1024
# What the interpreter says of itself, run as the program is (no -I, the program's
# environment) but importing nothing: any Python can run it, so a refusal names it.
_IDENTIFY = (
    "import sys\n"
    "i = getattr(sys, 'implementation', None)\n"
    "m = getattr(sys.modules.get('_frozen_importlib_external'), 'MAGIC_NUMBER', None)\n"
    "print(repr([getattr(i, 'name', 'cpython'), sys.version.split()[0], "
    "list(sys.version_info[:2]), sys.version, getattr(sys, 'abiflags', ''), "
    "getattr(i, 'cache_tag', None), m.hex() if m else '', sys.flags.optimize]))\n"
)
_SERVE = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from peeky.python_interpreter import serve; sys.exit(serve())"
)


def own_compiler():
    """This interpreter's compiler: implementation, exact version and build, ABI flags,
    cache tag and bytecode magic number."""
    return [
        sys.implementation.name,
        sys.version,
        getattr(sys, "abiflags", ""),
        sys.implementation.cache_tag,
        importlib.util.MAGIC_NUMBER.hex(),
    ]


def fingerprint(path):
    """Where ``path`` leads, and that file's device, inode, size and mtime."""
    real = os.path.realpath(path)
    status = os.stat(real)
    return [real, status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns]


@dataclass(frozen=True)
class Interpreter:
    """``path``: what runs (absolute, its links not followed: a virtual environment's
    `python` is a link to the one it was made from); ``chosen``: a key of CHOSEN;
    ``version``: `3.12.4`; ``optimize``: the program's `sys.flags.optimize`;
    ``compiler``: what it reported (own_compiler); ``fingerprint``: its file when it was
    asked; ``unused``: a virtual environment above the root, which is never used, named
    where Peeky's own runs the program; ``environment``: Peeky's own virtual environment,
    where that is the one above the root."""

    path: str
    chosen: str
    version: str
    optimize: int = 0
    compiler: tuple = ()
    fingerprint: tuple = ()
    unused: str | None = None
    environment: str | None = None

    @property
    def same(self):
        """Whether it compiles as this interpreter does (at an optimization level given
        to each compile)."""
        return list(self.compiler) == own_compiler()

    def current(self):
        """Whether its path still leads to the file it was asked as."""
        try:
            return fingerprint(self.path) == list(self.fingerprint)
        except OSError:
            return False

    def identity(self):
        """The run's `python`: the interpreter, its version and optimization level, how
        it was chosen, and where its links lead (reported only)."""
        found = {
            "interpreter": self.path,
            "version": self.version,
            "optimize": self.optimize,
            "chosen": self.chosen,
        }
        if (resolved := os.path.realpath(self.path)) != self.path:
            found["resolved"] = resolved
        if self.unused:
            found["unused"] = self.unused
        if self.environment:
            found["environment"] = self.environment
        return found


def text(identity):
    """`/p/.venv/bin/python, Python 3.12.4 (the root's .venv)` for a run's `python`."""
    if not isinstance(identity, dict) or not identity.get("interpreter"):
        return None
    how = CHOSEN.get(identity.get("chosen"), str(identity.get("chosen")))
    if identity.get("environment"):
        how += f", the virtual environment {identity['environment']}"
    if identity.get("unused"):
        how += (
            f"; {identity['unused']} is above the root, where Peeky does not look: "
            f"--python {identity['unused']}/bin/python uses it"
        )
    level = identity.get("optimize")
    flags = f" -{'O' * level}" if type(level) is int and 0 < level <= 2 else ""
    return f"{identity['interpreter']}, Python {identity.get('version')}{flags} ({how})"


def _environment_python(directory):
    python = Path(directory) / "bin" / "python"
    if python.is_file() and os.access(python, os.X_OK):
        return str(python)
    return None


def _peekys(directory):
    return os.path.realpath(directory) == os.path.realpath(sys.prefix)


def choose(root, option=None, environ=None):
    """``(path, chosen, unused, environment)``: which interpreter runs a program under
    ``root`` (see the module's docstring, and Interpreter); nothing is run. ValueError
    for a ``--python`` that names no executable file."""
    environ = os.environ if environ is None else environ
    if option == OWN:
        return sys.executable, "peeky", None, None
    if option is not None:
        given = os.path.expanduser(str(option))
        found = given
        if os.sep not in given and not os.path.exists(given):
            # A command name on PATH: `--python python3.12`.
            found = shutil.which(given, path=environ.get("PATH")) or given
        found = os.path.abspath(found)
        if not os.path.isfile(found) or not os.access(found, os.X_OK):
            raise ValueError(
                f"--python {option}: no executable file there. Pass the program's Python "
                "interpreter (such as .venv/bin/python), or --python peeky for Peeky's own"
            )
        return found, "--python", None, None
    root = Path(root)
    for name in ENVIRONMENTS:
        if (root / name / "pyvenv.cfg").is_file() and (found := _environment_python(root / name)):
            return found, name, None, None
    active = environ.get("VIRTUAL_ENV")
    if (
        active
        and os.path.isabs(active)
        and not _peekys(active)
        and (found := _environment_python(active))
    ):
        return found, "VIRTUAL_ENV", None, None
    # The nearest environment above the root is named, never used; where it is Peeky's
    # own (Peeky run from a checkout's .venv), it is the one that runs the program.
    for parent in root.resolve().parents:
        for name in ENVIRONMENTS:
            if (parent / name / "pyvenv.cfg").is_file() and _environment_python(parent / name):
                if _peekys(parent / name):
                    return sys.executable, "peeky", None, str(parent / name)
                return sys.executable, "peeky", str(parent / name), None
    return sys.executable, "peeky", None, None


def own_version():
    import platform

    return platform.python_version()


def program_environment():
    """The environment the program gets, as far as what it compiles depends on it."""
    return {**without_credentials(os.environ), **PROGRAM_ENV}


def identify(path, chosen, unused=None, environment=None):
    """The Interpreter at ``path``: asked, as the program will be run (with its
    environment, which has no credentials), for its implementation, version, compiler
    and optimization level. ValueError where it cannot be asked, changes while asked,
    or is not CPython 3.11 or later."""
    shown = f"{path} ({CHOSEN.get(chosen, chosen)})"

    def unrunnable(why):
        return ValueError(
            f"Peeky could not run the Python interpreter {shown}: {why}. Pass the "
            "program's interpreter with --python, or --python peeky for Peeky's own"
        )

    try:
        before = fingerprint(path)
        ran = subprocess.run(
            [path, "-c", _IDENTIFY],
            capture_output=True,
            text=True,
            timeout=IDENTIFY_S,
            check=False,
            stdin=subprocess.DEVNULL,
            cwd=os.path.dirname(os.path.realpath(path)),
            env=program_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise unrunnable(exc) from None
    try:
        said = ast.literal_eval(split_lines(ran.stdout.strip())[-1]) if ran.returncode == 0 else 0
        implementation, version, (major, minor), build, flags, tag, magic, level = said
        found = Interpreter(
            path,
            chosen,
            str(version),
            int(level),
            (str(implementation), str(build), str(flags), tag, str(magic)),
            tuple(before),
            unused,
            environment,
        )
        release = (int(major), int(minor))
    except (ValueError, TypeError, IndexError, SyntaxError, MemoryError, RecursionError):
        said = split_lines((ran.stderr or ran.stdout or "").strip())
        raise ValueError(
            f"Peeky could not ask the Python interpreter {shown} its version"
            + (f" ({said[-1][:200]})" if said else f" (exit status {ran.returncode})")
            + ". Pass the program's interpreter with --python, or --python peeky for "
            "Peeky's own"
        ) from None
    if implementation != "cpython" or release < MINIMUM:
        name = "CPython" if implementation == "cpython" else implementation
        raise ValueError(
            f"Peeky runs Python programs on CPython {MINIMUM[0]}.{MINIMUM[1]} or later, and "
            f"{shown} is {name} {version}: Peeky reads the program's code in its "
            f"interpreter, and reads only what CPython {MINIMUM[0]}.{MINIMUM[1]} and later "
            f"compile. Pass --python with CPython {MINIMUM[0]}.{MINIMUM[1]} or later, or "
            f"--python peeky for Peeky's own (Python {own_version()})"
        )
    if not found.current():
        raise unrunnable("it changed while Peeky asked it its version; start again")
    return found


def find(root, option=None, environ=None):
    """The Interpreter that runs a program under ``root`` (``choose``, then
    ``identify``)."""
    return identify(*choose(root, option, environ))


def absolute_option(option):
    """A ``--python`` as another process with another working directory reads it:
    `peeky`, or the absolute path ``choose`` finds."""
    if option is None or option == OWN:
        return option
    return choose(".", option)[0]


# The readings, as plain JSON values both here and in a helper; each takes the program's
# optimization level first.


def _stale_note(optimize, path, function, line, name, loaded_by, entered):
    from peeky.python_passes import stale_note

    found = stale_note(path, function, line, name, loaded_by, entered, optimize)
    return None if found is None else [str(found), found.note]


def _classify(optimize, path, function, line, names, listed, loaded_by):
    from peeky.python_scopes import classify

    # Scopes are the compiler's symbol table, which no optimization level changes.
    found, reason = classify(path, function, line, names, listed, loaded_by=loaded_by)
    if found is None:
        return [None, reason]
    return [{name: None if how is None else list(how) for name, how in found.items()}, None]


def _entry_lines(optimize, program, lines):
    from peeky.adapters import python_entry_lines

    found = python_entry_lines(program, lines, optimize)
    return [[line, first] for line, first in found.items()]


def _lines(optimize, path):
    from peeky.python_scopes import breakable_lines

    return breakable_lines(Path(path), optimize)


def _scopes(optimize, path):
    from peeky.python_scopes import function_scopes

    return function_scopes(Path(path), optimize)


def _self_contained(optimize, text):
    from peeky.adapters import _self_contained

    return _self_contained(text)


def _anonymous(optimize, text, lines):
    from peeky.python_scopes import anonymous_code

    return [[line, counts] for line, counts in anonymous_code(text, lines, optimize).items()]


_READINGS = {
    "stale_note": _stale_note,
    "classify": _classify,
    "entry_lines": _entry_lines,
    "lines": _lines,
    "scopes": _scopes,
    "self_contained": _self_contained,
    "anonymous": _anonymous,
}


class Unavailable(Exception):
    """The program's interpreter could not make a reading: nothing is read instead."""


def serve():
    """The helper: first its compiler, `{"id": 0, "compiler"}`; then one reply per
    request, each a JSON line: `{"id", "reading", "optimize", "args"}` on stdin;
    `{"id", "result"}`, `{"id", "error"}` (the reading's own refusal, a ValueError) or
    `{"id", "failed"}` on stdout. Ends at the end of its input."""
    replies = sys.stdout
    # Whatever a reading or the environment's startup prints is not a reply.
    sys.stdout = sys.stderr
    replies.write(json.dumps({"id": 0, "compiler": own_compiler()}) + "\n")
    replies.flush()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            number = request["id"]
            reading, optimize = _READINGS[request["reading"]], int(request["optimize"])
        except (ValueError, TypeError, KeyError):
            continue
        try:
            reply = {"id": number, "result": reading(optimize, *request["args"])}
        except ValueError as exc:
            reply = {"id": number, "error": str(exc)}
        except Exception as exc:
            reply = {"id": number, "failed": f"{type(exc).__name__}: {exc}"[:500]}
        replies.write(json.dumps(reply) + "\n")
        replies.flush()
    return 0


_OPEN = weakref.WeakSet()


class Readings:
    """One launch's readings of a program's code by its ``interpreter`` (identified for
    the launch; else asked at the first reading, and again after ``close``), one at a
    time; its helper, if any, ends at ``close``."""

    def __init__(self, path, interpreter=None):
        self.path = path
        self.given = interpreter
        self.found = None
        self.lock = threading.Lock()
        self.process = None
        self.buffer = b""
        self.asked = 0
        _OPEN.add(self)

    def __call__(self, reading, *args, timeout=READ_S):
        """The reading's JSON value; ValueError for its own refusal; Unavailable where
        the interpreter could not make it within ``timeout`` seconds. Nothing failed is
        remembered: the next reading asks again."""
        deadline = time.monotonic() + timeout
        with self.lock:
            interpreter = self.given or self.found
            if interpreter is None:
                try:
                    interpreter = self.found = identify(self.path, "--python")
                except ValueError as exc:
                    raise Unavailable(str(exc)) from None
            if not interpreter.current():
                self._stop()
                raise Unavailable(f"{self.path} is not the interpreter Peeky asked at launch")
            if interpreter.same:
                return json.loads(json.dumps(_READINGS[reading](interpreter.optimize, *args)))
            try:
                return self._ask(interpreter, reading, list(args), deadline)
            except Unavailable:
                self._stop()
                raise

    def _start(self, interpreter, deadline):
        import peeky

        try:
            self.process = subprocess.Popen(
                [self.path, "-I", "-B", "-c", _SERVE]
                + [str(Path(peeky.__file__).resolve().parents[1])],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=without_credentials(os.environ),
            )
        except OSError as exc:
            raise Unavailable(f"it could not be started: {exc}") from None
        os.set_blocking(self.process.stdin.fileno(), False)
        said = self._reply(0, deadline)
        if said != list(interpreter.compiler):
            raise Unavailable(
                f"{self.path} is not the interpreter Peeky asked at launch (its helper "
                f"reports {str(said)[:200]})"
            )

    def _ask(self, interpreter, reading, args, deadline):
        self.asked += 1
        number = self.asked
        request = {"id": number, "reading": reading, "optimize": interpreter.optimize}
        request = (json.dumps({**request, "args": args}) + "\n").encode()
        if len(request) > MAX_REQUEST:
            raise Unavailable(f"what it would read is over {MAX_REQUEST >> 20} MiB")
        if self.process is None:
            self._start(interpreter, deadline)
        self._write(request, deadline)
        return self._reply(number, deadline)

    def _write(self, request, deadline):
        descriptor, view = self.process.stdin.fileno(), memoryview(request)
        while view:
            left = deadline - time.monotonic()
            if left <= 0:
                raise Unavailable("it did not take the request in time")
            _, ready, _ = select.select([], [descriptor], [], left)
            if not ready:
                continue
            try:
                view = view[os.write(descriptor, view) :]
            except BlockingIOError:
                continue
            except OSError:
                raise Unavailable("it ended") from None

    def _reply(self, number, deadline):
        """The value of reply ``number`` (0: the helper's compiler)."""
        while True:
            line, newline, rest = self.buffer.partition(b"\n")
            if newline:
                self.buffer = rest
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(reply, dict) or reply.get("id") != number:
                    continue
                if number == 0:
                    return reply.get("compiler")
                if "result" in reply:
                    return reply["result"]
                if "error" in reply:
                    raise ValueError(str(reply["error"]))
                raise Unavailable(str(reply.get("failed"))[:300])
            if len(self.buffer) > MAX_REPLY:
                raise Unavailable("its reply was too long")
            left = deadline - time.monotonic()
            if left <= 0:
                raise Unavailable("it did not answer in time")
            ready, _, _ = select.select([self.process.stdout], [], [], left)
            if not ready:
                continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                try:
                    status = self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    raise Unavailable("it closed its output") from None
                raise Unavailable(f"it ended (exit status {status})")
            self.buffer += chunk

    def _stop(self):
        process, self.process, self.buffer = self.process, None, b""
        if process is None:
            return
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def close(self):
        """End the helper; an interpreter not given is asked again at the next reading."""
        with self.lock:
            self._stop()
            self.found = None


@atexit.register
def close_all():
    # Best effort at exit: a reading still running in another thread keeps its helper.
    for each in list(_OPEN):
        if each.lock.acquire(timeout=1):
            try:
                each._stop()
            finally:
                each.lock.release()
